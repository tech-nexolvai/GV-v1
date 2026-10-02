"""Strict Amazon Bedrock Nova 2 Lite tool-call adapter.

The model may report only a raw observation candidate. It cannot produce evidence or
a verdict, and ordinary model text is never treated as structured output.

Source: ``docs/DESIGN_AI.md`` section 4.1 and issue #249.
Verification: ``tests/extraction/models/test_nova.py``.
"""

from __future__ import annotations

import logging
import struct
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic_ns
from typing import Any, Final, Literal, Protocol, cast

from evidence.candidate import ObservationCandidate
from extraction.glyph_bands import FractionLayout
from extraction.models.context import AssembledContext
from extraction.models.sanitisation import CoordinateInstruction, InjectionAttempt, prepare_prompt
from extraction.models.validation import (
    CandidateContext,
    CoordinateMode,
    CropSize,
    NovaToolPayload,
    RejectionRecorder,
    ValidationRejection,
    validate_payload,
)

TOOL_NAME = "report_drawing_reading"
DIMENSION_READER_MAX_TOKENS = 1024
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

#: The region Nova is invoked in unless a deployment says otherwise.
#:
#: Not a guess: it is where this account's models are enabled, and Bedrock model access is granted
#: per region — a model enabled in one is `AccessDenied` in another, with an error that reads like a
#: permissions problem rather than a geography one.
DEFAULT_REGION = "us-east-1"

#: The model invoked unless a deployment says otherwise, and the one whose behaviour is recorded in
#: `data/exploration/`. Named in full, version included: "which model said this" is not answerable
#: from a family name once the family has moved on.
DEFAULT_MODEL_ID = "amazon.nova-lite-v1:0"

#: Phase C's production vision readers, chosen from what this account can actually invoke (#668).
#:
#: **Claude Haiku is configured and disabled rather than removed.** It cannot be invoked at all —
#: Anthropic's first-time-use form has never been submitted for this account (#665) — and leaving it
#: enabled cost 46 zero-token failures per extraction run while the agreement lane stayed empty.
#: Keeping the definition means #665 landing is a change to `GV_BEDROCK_VISION_READERS`, not to code.
NOVA_PRO_MODEL_ID = "amazon.nova-pro-v1:0"
NOVA_2_LITE_MODEL_ID = "amazon.nova-2-lite-v1:0"
MINISTRAL_3_3B_MODEL_ID = "mistral.ministral-3-3b-instruct"
MISTRAL_LARGE_3_MODEL_ID = "mistral.mistral-large-3-675b-instruct"
CLAUDE_HAIKU_4_5_MODEL_ID = "anthropic.claude-haiku-4-5-20251001-v1:0"
NOVA_PRO_EXTRACTOR = "bedrock-nova-pro"
NOVA_2_LITE_EXTRACTOR = "bedrock-nova-2-lite"
MINISTRAL_3_3B_EXTRACTOR = "bedrock-ministral-3-3b"
MISTRAL_LARGE_3_EXTRACTOR = "bedrock-mistral-large-3"
CLAUDE_HAIKU_4_5_EXTRACTOR = "bedrock-claude-haiku-4-5"

#: What turns a foundation-model id into a cross-region inference profile id.
#:
#: **Measured, not read off a document.** On this account the plain `amazon.nova-lite-v1:0` is
#: refused — `AccessDeniedException: Your account is currently being verified` — while
#: `us.amazon.nova-lite-v1:0` answers normally. AWS also documents a second way to reach the same
#: wall, `ValidationException: on-demand throughput isn't supported for this model`, which is what a
#: model published only as an inference profile returns.
#:
#: The two errors have different codes and different messages and mean the same operational thing:
#: *invoke this through the profile instead*. So the adapter tries the id it was given and falls back
#: once, rather than making an operator read a permissions error and guess a prefix.
INFERENCE_PROFILE_PREFIX = "us."

#: Error codes that can mean "not directly invocable — use the inference profile".
#:
#: `AccessDeniedException` is deliberately included even though it is ambiguous: it is also what a
#: genuine permissions failure returns. Retrying once costs one refused call, which is recorded, and
#: the alternative is an operator staring at "access denied" for a model they were told they had.
_PROFILE_HINT_CODES = frozenset({"AccessDeniedException", "ValidationException"})


@dataclass(frozen=True, slots=True)
class NovaConfig:
    """Explicit model identity and transport bounds; this type has no guessed defaults."""

    model_id: str
    prompt_id: str
    template_id: str
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_attempts: int
    region_name: str | None = None
    extractor: str = "nova"
    coordinate_mode: CoordinateMode = CoordinateMode.PIXELS
    """Which space this model answers its rectangle in — **measured, never inferred**.

    **The default is the one that fails loudly.** The two mistakes are not symmetric. A model that
    answers on the 0-1000 grid, read as pixels, returns values larger than the crop and the bounds
    check refuses it — a visible refusal naming the crop size. A model that answers in pixels, read
    as a grid, is divided by a thousand, lands near the origin, and *passes* that same check, because
    a small number is in range. One costs a rejected reading; the other records a reading pointing at
    the wrong part of the drawing and says nothing. So an unstated mode gets the first.

    It was inferred once, from whether `"nova"` appeared in the model id, and that is wrong for
    `amazon.nova-2-lite-v1:0`: it carries the word and answers in pixels. A pixel value read as a
    0-1000 grid value is divided by a thousand, lands near the origin, and *passes* the bounds check
    #664 added, because a small number is in range. The result is a reading pointing at the wrong
    place that nothing downstream can question — which is the exact failure #664 exists to prevent,
    reintroduced by its own fix.

    So it is a field, supplied per reader from a measured run, and a model's name says nothing about
    it. `docs/NEXT_BUILD_PLAN.md` records where the current values came from.
    """

    def __post_init__(self) -> None:
        for name in ("model_id", "prompt_id", "template_id", "extractor"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("connect_timeout_seconds", "read_timeout_seconds", "max_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.region_name is not None and (
            not isinstance(self.region_name, str) or not self.region_name.strip()
        ):
            raise ValueError("region_name must be a non-empty string or None")
        if not isinstance(self.coordinate_mode, CoordinateMode):
            raise TypeError("coordinate_mode must be a CoordinateMode")


def config_from_environment(
    *,
    prompt_id: str = "dimension-reader-v1",
    template_id: str = "bounded-crop-v1",
) -> NovaConfig:
    """The Bedrock configuration a deployment states, read from the environment.

    `GV_BEDROCK_MODEL` and `GV_BEDROCK_REGION` are how a provider swap stays a configuration change:
    the adapter interface does not move, and nothing about the model identity is compiled in. Both
    have defaults because both are operational facts with a known right answer for this account,
    unlike the empirical numbers elsewhere in this project that must never acquire one.

    **No credentials are read here, ever.** `NovaAdapter.from_environment` builds a boto3 client,
    which resolves the provider chain — environment, shared config, instance role — so a key never
    passes through this repository's own configuration and cannot be logged by it.

    The timeouts are bounded and small on purpose. A vision call that has not answered in two minutes
    is not going to, and an unbounded read timeout turns one slow region into a stalled worker.
    """
    import os

    return NovaConfig(
        model_id=os.environ.get("GV_BEDROCK_MODEL", DEFAULT_MODEL_ID),
        prompt_id=prompt_id,
        template_id=template_id,
        connect_timeout_seconds=int(os.environ.get("GV_BEDROCK_CONNECT_TIMEOUT", "10")),
        read_timeout_seconds=int(os.environ.get("GV_BEDROCK_READ_TIMEOUT", "120")),
        max_attempts=1,
        region_name=os.environ.get("GV_BEDROCK_REGION", DEFAULT_REGION),
    )


#: Which readers a deployment runs, as a comma-separated list of `_ReaderDefinition.key`. Unset
#: means every reader marked enabled below.
VISION_READER_KEYS_ENV: Final = "GV_BEDROCK_VISION_READERS"


@dataclass(frozen=True, slots=True)
class _ReaderDefinition:
    """One candidate vision reader and the measured facts about how it answers."""

    key: str
    model_id: str
    extractor: str
    coordinate_mode: CoordinateMode
    coordinate_measurement: str
    enabled: bool
    disabled_reason: str | None = None
    coordinate_measured: bool = True
    """Whether the coordinate space was measured on this account, as opposed to documented.

    Separate from `enabled` because the two answer different questions: Nova Pro is switched off for
    accuracy (#751) but its space was measured, so the bake-off can still test it; Claude Haiku's has
    never been measured (#665), so nothing may read its coordinates as though it had."""

    def __post_init__(self) -> None:
        for name in ("key", "model_id", "extractor", "coordinate_measurement"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.coordinate_mode, CoordinateMode):
            raise TypeError("coordinate_mode must be a CoordinateMode")
        if not isinstance(self.enabled, bool):
            raise TypeError("enabled must be True or False")
        if self.disabled_reason is not None and (
            not isinstance(self.disabled_reason, str) or not self.disabled_reason.strip()
        ):
            raise ValueError("disabled_reason must be a non-empty string or None")
        if not self.enabled and self.disabled_reason is None:
            raise ValueError("disabled readers must say why they are disabled")
        if not isinstance(self.coordinate_measured, bool):
            raise TypeError("coordinate_measured must be True or False")
        if self.enabled and not self.coordinate_measured:
            raise ValueError(
                "an enabled reader must have a measured coordinate space: a guessed one reads a "
                "rectangle in the wrong units and can still pass the bounds check (#664)"
            )

    @property
    def model_env(self) -> str:
        """The variable that overrides this reader's model id, derived rather than hand-written.

        Hand-writing it invited the pair to drift: a key renamed without its variable leaves an
        override that silently stops applying, and an override that stops applying is a deployment
        running a model it believes it replaced.
        """
        return f"GV_BEDROCK_{self.key.upper().replace('-', '_')}_MODEL"


#: The readers Phase C runs, and the one it keeps switched off.
#:
#: **Every enabled coordinate mode here was measured**, and the measurement is kept on the reader
#: definition instead of hidden in a nearby comment. A model's name is not evidence of its answer
#: space; issue #668 records the original coordinate-mode table, and issue #699 records the Nova Pro
#: recheck after it returned coordinate-shaped values instead of dimensions on the demo run.
#:
#: **Only two vendors answer at all.** Google, Meta, Moonshot, Qwen, xAI, Writer and Nvidia all
#: refuse forced tool use with an image, so the independence available to the agreement lane is
#: narrower than we would like. Widening it is what #665 buys: Claude would be a third vendor.
VISION_READERS: Final[tuple[_ReaderDefinition, ...]] = (
    _ReaderDefinition(
        key="nova-pro",
        model_id=NOVA_PRO_MODEL_ID,
        extractor=NOVA_PRO_EXTRACTOR,
        coordinate_mode=CoordinateMode.NOVA_GRID,
        coordinate_measurement=(
            "#668 recorded Nova Pro as 0-1000 grid; #699 rechecked a generated crop reading "
            '24 1/2" on 2026-09-29 and the raw tool response returned that value.'
        ),
        # **Off for accuracy, not for want of a measurement (#751).** On the 51-crop human-read key
        # (#641) it read 8 of 35 right, the fewest, and accepted 12 wrong readings, at $1.43 per 1,000
        # crops against Ministral 3B's $0.08. The pair left running, Nova 2 Lite + Ministral 3B,
        # agreed on no wrong value there. `GV_BEDROCK_VISION_READERS` can still switch it back on.
        enabled=False,
        disabled_reason=(
            "#751: least accurate reader on the human-read key (8/35 right, 12 accepted wrong) and "
            "the most expensive; the recommended pair is Nova 2 Lite + Ministral 3B (#641)."
        ),
    ),
    # A different vendor, which is the strongest independence on offer here. Also the cheapest and
    # fastest of the seven that conform — 1,430 tokens and 3.0s against Nova Pro's 5,374 and 4.7s.
    _ReaderDefinition(
        key="ministral-3-3b",
        model_id=MINISTRAL_3_3B_MODEL_ID,
        extractor=MINISTRAL_3_3B_EXTRACTOR,
        coordinate_mode=CoordinateMode.PIXELS,
        coordinate_measurement="#668 recorded Ministral 3 3B as pixel coordinates.",
        enabled=True,
    ),
    # Same vendor as Nova Pro and a different answer space, which is the clearest evidence available
    # that the two were trained separately rather than sharing a lineage.
    _ReaderDefinition(
        key="nova-2-lite",
        model_id=NOVA_2_LITE_MODEL_ID,
        extractor=NOVA_2_LITE_EXTRACTOR,
        coordinate_mode=CoordinateMode.PIXELS,
        coordinate_measurement="#668 recorded Nova 2 Lite as pixel coordinates.",
        enabled=True,
    ),
    # **The reading agent's escalation reader (#757 D-A2, the admin's decision of 2026-10-01)**, the
    # best single reader on the human-read key (#641: 16 of 35). Off for the vision route, which reads
    # every region with every enabled reader: it is the same vendor as Ministral 3B, and #641 found
    # same-vendor pairs agree on wrong values most. The agent asks it by name, only after the
    # primary (`workflow/reading_agent.py`).
    _ReaderDefinition(
        key="mistral-large-3",
        model_id=MISTRAL_LARGE_3_MODEL_ID,
        extractor=MISTRAL_LARGE_3_EXTRACTOR,
        coordinate_mode=CoordinateMode.PIXELS,
        coordinate_measurement="#641 measured Mistral Large 3 as pixel coordinates (2026-09-29).",
        enabled=False,
        disabled_reason=(
            "#757 D-A2: the reading agent's escalation reader, asked by name after the primary; not "
            "a vision-route reader, because it shares a vendor with Ministral 3B."
        ),
    ),
    # Off until #665. Its space is unmeasured because it has never returned a reading on this
    # account; Anthropic documents absolute pixels, and that stays a claim until a run confirms it.
    _ReaderDefinition(
        key="claude-haiku-4-5",
        model_id=CLAUDE_HAIKU_4_5_MODEL_ID,
        extractor=CLAUDE_HAIKU_4_5_EXTRACTOR,
        coordinate_mode=CoordinateMode.PIXELS,
        coordinate_measurement=(
            "Unmeasured on this account; Anthropic documents absolute pixels, but #665 must land "
            "before this can be verified."
        ),
        enabled=False,
        disabled_reason="#665: Anthropic first-time-use form has not been submitted for this account.",
        coordinate_measured=False,
    ),
)


def vision_configs_from_environment(
    *,
    prompt_id: str = "dimension-reader-v1",
    template_id: str = "bounded-crop-v1",
) -> tuple[NovaConfig, ...]:
    """The vision readers this deployment runs, in `VISION_READERS` order.

    Returns a variable-length tuple because the set is configuration: `GV_BEDROCK_VISION_READERS`
    names the keys to run, and a deployment that has resolved #665 adds `claude-haiku-4-5` without a
    code change.

    Extractor names stay distinct and stable. `evidence/corroborate.py` counts reader independence by
    extractor, so two readers sharing a name would agree with themselves and manufacture the
    corroboration the gate exists to require.
    """
    import os

    connect_timeout_seconds = int(os.environ.get("GV_BEDROCK_CONNECT_TIMEOUT", "10"))
    read_timeout_seconds = int(os.environ.get("GV_BEDROCK_READ_TIMEOUT", "120"))
    region_name = os.environ.get("GV_BEDROCK_REGION", DEFAULT_REGION)

    requested = os.environ.get(VISION_READER_KEYS_ENV, "").strip()
    if requested:
        wanted = {key.strip() for key in requested.split(",") if key.strip()}
        known = {reader.key for reader in VISION_READERS}
        unknown = sorted(wanted - known)
        if unknown:
            raise ValueError(
                f"unknown vision reader key(s): {unknown}. Known keys: {sorted(known)}"
            )
        chosen = tuple(reader for reader in VISION_READERS if reader.key in wanted)
    else:
        chosen = tuple(reader for reader in VISION_READERS if reader.enabled)

    return tuple(
        _config_for(
            reader,
            prompt_id=prompt_id,
            template_id=template_id,
            connect_timeout_seconds=connect_timeout_seconds,
            read_timeout_seconds=read_timeout_seconds,
            region_name=region_name,
        )
        for reader in chosen
    )


def _config_for(
    reader: _ReaderDefinition,
    *,
    prompt_id: str,
    template_id: str,
    connect_timeout_seconds: int,
    read_timeout_seconds: int,
    region_name: str,
) -> NovaConfig:
    import os

    return NovaConfig(
        model_id=os.environ.get(reader.model_env, reader.model_id),
        prompt_id=prompt_id,
        template_id=template_id,
        connect_timeout_seconds=connect_timeout_seconds,
        read_timeout_seconds=read_timeout_seconds,
        max_attempts=1,
        region_name=region_name,
        extractor=reader.extractor,
        coordinate_mode=reader.coordinate_mode,
    )


def vision_config_for_extractor(
    extractor: str,
    *,
    prompt_id: str = "dimension-reader-v1",
    template_id: str = "bounded-crop-v1",
) -> NovaConfig | None:
    """The configuration of the defined reader named `extractor`, enabled or not; `None` if none is.

    For a reader asked **by name** rather than run on every region — the reading agent's escalation
    reader (#757 D-A2). Only a reader with a measured coordinate space is returned: one without
    would read a rectangle in units nobody has confirmed (#664).
    """
    import os

    for reader in VISION_READERS:
        if reader.extractor == extractor and reader.coordinate_measured:
            return _config_for(
                reader,
                prompt_id=prompt_id,
                template_id=template_id,
                connect_timeout_seconds=int(os.environ.get("GV_BEDROCK_CONNECT_TIMEOUT", "10")),
                read_timeout_seconds=int(os.environ.get("GV_BEDROCK_READ_TIMEOUT", "120")),
                region_name=os.environ.get("GV_BEDROCK_REGION", DEFAULT_REGION),
            )
    return None


class InferenceProfileRoutes:
    """Which models this process has found answer only through their inference profile (#702).

    **Measured on this account: every Nova 2 Lite call was two round trips.** The plain id was
    refused every time, and the `us.` profile answered every time: 39 refused attempts with zero
    tokens, then 39 that answered, on one run. The fallback in `NovaAdapter.extract` was doing
    exactly its job — it had simply become the only path that ever worked.

    So the first time a model needs its profile, the process remembers it, and later calls go
    straight there. **Learned, not configured**: nothing writes `us.` into a model id, so an account
    or region where the plain id works keeps using it, and a model never tried still starts from its
    plain id with the fallback behind it. Keyed by region as well, because a profile is.

    Per process, because the production reader builds a fresh adapter for every crop
    (`workflow.stages.BedrockVisionReader`), so an adapter's own memory would last one call.
    """

    def __init__(self) -> None:
        self._needed: set[tuple[str, str]] = set()
        self._lock = threading.Lock()

    def knows(self, region_name: str, model_id: str) -> bool:
        with self._lock:
            return (region_name, model_id) in self._needed

    def learn(self, region_name: str, model_id: str) -> bool:
        """Remember it; `True` the first time, so the caller can say so exactly once."""
        with self._lock:
            if (region_name, model_id) in self._needed:
                return False
            self._needed.add((region_name, model_id))
            return True

    def forget(self) -> None:
        with self._lock:
            self._needed.clear()


#: The process-wide memory every `NovaAdapter` shares unless it is handed its own.
PROFILE_ROUTES: Final = InferenceProfileRoutes()

logger = logging.getLogger(__name__)


def needs_inference_profile(error: BaseException, model_id: str) -> bool:
    """Whether this failure means "invoke the inference profile instead".

    Two different AWS errors mean it — see `INFERENCE_PROFILE_PREFIX` — and neither says so in words
    an operator can act on. A model id that already carries the prefix is never a candidate: a second
    prefix would produce `us.us.amazon…`, and the retry would fail for a new reason that looked like
    the old one.
    """
    if model_id.startswith(INFERENCE_PROFILE_PREFIX):
        return False
    # `__cause__` is typed `BaseException | None`, so the narrowing happens here rather than at the
    # call site: a `KeyboardInterrupt` is not a Bedrock error and must not be read as one.
    if not isinstance(error, Exception):
        return False
    return _error_code(error) in _PROFILE_HINT_CODES


@dataclass(frozen=True, slots=True)
class NovaRequest:
    """One bounded crop request carrying the provenance needed for its candidate."""

    candidate_id: str
    page: int
    crop: bytes
    image_format: Literal["jpeg", "png"]
    context: AssembledContext
    bound_pt: Decimal
    stacked_label: bool
    """Whether the crop shows a stacked fraction, as the sheet's geometry says (#735). Handed to
    `validate_payload`, which abstains on any reading of such a crop. **No default**: a request that
    could not say is written `False` where it is built, so the gap is visible in that code."""
    stacked_layouts: tuple[FractionLayout, ...]
    """Where the parts of each stacked label in the crop were drawn (#834), for `validate_payload` to
    refuse a reading they contradict. **No default**, as for `stacked_label`: empty where the crop
    shows none, or only ones set in text."""

    def __post_init__(self) -> None:
        if not isinstance(self.stacked_label, bool):
            raise TypeError("stacked_label must be a bool")
        if not isinstance(self.stacked_layouts, tuple) or not all(
            isinstance(layout, FractionLayout) for layout in self.stacked_layouts
        ):
            raise TypeError("stacked_layouts must be a tuple of FractionLayout")
        if self.stacked_layouts and not self.stacked_label:
            raise ValueError("a crop with stacked layouts shows a stacked label")
        if not isinstance(self.candidate_id, str) or not self.candidate_id.strip():
            raise ValueError("candidate_id must be a non-empty string")
        if isinstance(self.page, bool) or not isinstance(self.page, int) or self.page < 0:
            raise ValueError("page must be a non-negative integer")
        if not isinstance(self.crop, bytes) or not self.crop:
            raise ValueError("crop must be non-empty bytes")
        if self.image_format not in {"jpeg", "png"}:
            raise ValueError("image_format must be 'jpeg' or 'png'")
        if (
            not isinstance(self.bound_pt, Decimal)
            or not self.bound_pt.is_finite()
            or self.bound_pt < 0
        ):
            raise ValueError("bound_pt must be a finite, non-negative Decimal")


class NovaInvocationOutcome(StrEnum):
    """Closed outcomes recorded for every attempted Bedrock call."""

    OK = "ok"
    TIMEOUT = "timeout"
    RETRYABLE_ERROR = "retryable_error"
    REFUSED = "refused"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class NovaInvocation:
    """Audit metadata for one call attempt, including failed attempts."""

    model_id: str
    prompt_id: str
    template_id: str
    attempt: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    outcome: NovaInvocationOutcome
    request_id: str | None
    context: AssembledContext
    bound_pt: Decimal
    injection_attempts: tuple[InjectionAttempt, ...]
    rejection_reason: str | None = None


class InvocationRecorder(RejectionRecorder, Protocol):
    """Persistence boundary supplied by the caller."""

    def record(self, invocation: NovaInvocation) -> None:
        """Persist one immutable invocation record."""


class BedrockRuntimeClient(Protocol):
    """Small Bedrock surface used by the adapter and replaceable in unit tests."""

    def converse(self, **kwargs: object) -> Mapping[str, Any]:
        """Invoke a messages-capable model."""


class NovaAdapterError(Exception):
    """Base class for explicit Nova adapter failures."""


class NovaTimeoutError(NovaAdapterError):
    """A Bedrock attempt exceeded a configured transport timeout."""


class NovaRetryExhaustedError(NovaAdapterError):
    """All configured attempts failed with temporary errors."""


class NovaProtocolError(NovaAdapterError):
    """Bedrock returned no single call to the required tool."""


class NovaPayloadRejectedError(NovaAdapterError):
    """The tool call did not satisfy the strict local payload contract."""

    def __init__(self, rejection: ValidationRejection) -> None:
        super().__init__(f"Nova tool payload was rejected: {rejection.reason}")
        self.rejection = rejection


class NovaRefusalError(NovaAdapterError):
    """Bedrock refused or filtered the request."""


class NovaServiceError(NovaAdapterError):
    """Bedrock failed in a way that must not be retried by this adapter."""


def _milliseconds_since(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)


def _usage(response: Mapping[str, Any] | None) -> tuple[int, int]:
    if response is None:
        return 0, 0
    usage = response.get("usage")
    if not isinstance(usage, Mapping):
        return 0, 0
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)
    if not isinstance(input_tokens, int) or isinstance(input_tokens, bool):
        input_tokens = 0
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
        output_tokens = 0
    return max(0, input_tokens), max(0, output_tokens)


def _request_id(response: Mapping[str, Any] | None) -> str | None:
    if response is None:
        return None
    metadata = response.get("ResponseMetadata")
    if not isinstance(metadata, Mapping):
        return None
    value = metadata.get("RequestId")
    return value if isinstance(value, str) and value else None


def _error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    details = response.get("Error")
    if not isinstance(details, Mapping):
        return None
    code = details.get("Code")
    return code if isinstance(code, str) else None


def _rejection_reason(error: NovaProtocolError | NovaPayloadRejectedError) -> str:
    """The reason to store on a locally rejected invocation."""

    if isinstance(error, NovaPayloadRejectedError):
        return error.rejection.reason
    text = str(error).strip()
    return f"protocol_error: {text}"[:500] if text else "protocol_error"


def _is_timeout(error: Exception) -> bool:
    return isinstance(error, TimeoutError) or error.__class__.__name__ in {
        "ConnectTimeoutError",
        "ReadTimeoutError",
    }


def _is_retryable(error: Exception) -> bool:
    if _is_timeout(error):
        return True
    if error.__class__.__name__ == "EndpointConnectionError":
        return True
    return _error_code(error) in {
        "InternalServerException",
        "ModelNotReadyException",
        "ServiceUnavailableException",
        "ThrottlingException",
    }


def _crop_size(data: bytes, image_format: Literal["jpeg", "png"]) -> CropSize:
    """Read dimensions from the exact image bytes sent to Bedrock."""

    if image_format == "png":
        if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
            raise ValueError("PNG crop has no readable IHDR dimensions")
        width, height = struct.unpack(">II", data[16:24])
        return CropSize(width, height)

    offset = 2
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        raise ValueError("JPEG crop has no readable SOI marker")
    while offset < len(data):
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(data):
            break
        segment_length = struct.unpack(">H", data[offset : offset + 2])[0]
        if segment_length < 2 or offset + segment_length > len(data):
            break
        if marker in {
            0xC0,
            0xC1,
            0xC2,
            0xC3,
            0xC5,
            0xC6,
            0xC7,
            0xC9,
            0xCA,
            0xCB,
            0xCD,
            0xCE,
            0xCF,
        }:
            if segment_length < 7:
                break
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return CropSize(width, height)
        offset += segment_length
    raise ValueError("JPEG crop has no readable frame dimensions")


def _coordinate_mode(config: NovaConfig) -> CoordinateMode:
    """The space this reader answers in, as its configuration states it.

    A function rather than an attribute read at each call site, because that is what the three call
    sites already use and because the indirection is where the old substring guess lived. Deleting
    the guess without deleting the seam keeps the diff honest about what changed.
    """
    return config.coordinate_mode


def _coordinate_instruction(mode: CoordinateMode) -> CoordinateInstruction:
    return (
        CoordinateInstruction.NOVA_GRID
        if mode is CoordinateMode.NOVA_GRID
        else CoordinateInstruction.PIXELS
    )


def _bedrock_tool_schema() -> dict[str, object]:
    schema = dict(NovaToolPayload.model_json_schema())
    for unsupported in ("title", "description", "additionalProperties"):
        schema.pop(unsupported, None)
    return schema


class NovaAdapter:
    """Invoke Nova through one forced tool and return only an uncertain candidate."""

    def __init__(
        self,
        config: NovaConfig,
        client: BedrockRuntimeClient,
        recorder: InvocationRecorder,
        profile_routes: InferenceProfileRoutes | None = None,
    ) -> None:
        self._config = config
        self._client = client
        self._recorder = recorder
        self._routes = PROFILE_ROUTES if profile_routes is None else profile_routes

    @classmethod
    def from_environment(cls, config: NovaConfig, recorder: InvocationRecorder) -> NovaAdapter:
        """Create the sole credential-aware model client using AWS's provider chain."""

        import boto3  # type: ignore[import-untyped]
        from botocore.config import Config  # type: ignore[import-untyped]

        transport = Config(
            connect_timeout=config.connect_timeout_seconds,
            read_timeout=config.read_timeout_seconds,
            retries={"max_attempts": 1, "mode": "standard"},
        )
        client = boto3.client(
            "bedrock-runtime",
            region_name=config.region_name,
            config=transport,
        )
        return cls(config, cast(BedrockRuntimeClient, client), recorder)

    def extract(self, request: NovaRequest) -> ObservationCandidate:
        """Call the required tool, validating locally and failing explicitly.

        One fallback, and only for the case AWS reports two different ways: a model id that cannot be
        invoked directly and must be reached through its cross-region inference profile. See
        `INFERENCE_PROFILE_PREFIX` for the two errors and why the retry exists — measured on this
        account, where the plain id answers `AccessDeniedException` and the prefixed one answers
        normally.

        **The refused attempt stays recorded.** It happened, it took time, and a record showing only
        the id that worked would misstate what this call did — and hide from the next operator that
        the configured id needs changing.

        **And it happens once per process, not once per call (#702).** When the profile answers, that
        is remembered (`InferenceProfileRoutes`), and later calls for the same model and region go to
        the profile first — so the refusal is recorded the first time and not paid for again.
        """
        model_id = self._config.model_id
        # `None` is boto3's own default region, remembered as such rather than as a region name.
        region = self._config.region_name or "(default region)"
        profile_id = f"{INFERENCE_PROFILE_PREFIX}{model_id}"
        if not model_id.startswith(INFERENCE_PROFILE_PREFIX) and self._routes.knows(
            region, model_id
        ):
            return self._attempt(request, profile_id)
        try:
            return self._attempt(request, model_id)
        except NovaServiceError as error:
            cause = error.__cause__
            if cause is None or not needs_inference_profile(cause, model_id):
                raise
        try:
            candidate = self._attempt(request, profile_id)
        except (NovaPayloadRejectedError, NovaProtocolError, NovaRefusalError):
            # The profile answered — its answer was refused, which says nothing against the route.
            self._remember_profile(region, model_id)
            raise
        self._remember_profile(region, model_id)
        return candidate

    def _remember_profile(self, region: str, model_id: str) -> None:
        if self._routes.learn(region, model_id):
            logger.info(
                "Bedrock model answers only through its inference profile on this account; later "
                "calls in this process go there first (#702)",
                extra={"model_id": model_id, "region": region, "profile": INFERENCE_PROFILE_PREFIX},
            )

    def _attempt(self, request: NovaRequest, model_id: str) -> ObservationCandidate:
        """One model id, with its own bounded retry loop and its own records."""

        last_error: Exception | None = None
        coordinate_mode = _coordinate_mode(self._config)
        prepared = prepare_prompt(
            request.context,
            coordinate_instruction=_coordinate_instruction(coordinate_mode),
        )
        for attempt in range(1, self._config.max_attempts + 1):
            started_ns = monotonic_ns()
            response: Mapping[str, Any] | None = None
            outcome = NovaInvocationOutcome.ERROR
            rejection_reason: str | None = None
            try:
                response = self._client.converse(**self._request(request, model_id))
                candidate = self._candidate(response, request)
                outcome = NovaInvocationOutcome.OK
                return candidate
            except NovaRefusalError:
                outcome = NovaInvocationOutcome.REFUSED
                raise
            except (NovaProtocolError, NovaPayloadRejectedError) as error:
                outcome = NovaInvocationOutcome.REJECTED
                rejection_reason = _rejection_reason(error)
                raise
            except Exception as error:
                last_error = error
                if not _is_retryable(error):
                    outcome = NovaInvocationOutcome.ERROR
                    raise NovaServiceError("Bedrock invocation failed without retry") from error
                outcome = (
                    NovaInvocationOutcome.TIMEOUT
                    if _is_timeout(error)
                    else NovaInvocationOutcome.RETRYABLE_ERROR
                )
                if attempt == self._config.max_attempts:
                    if _is_timeout(error):
                        raise NovaTimeoutError(
                            f"Nova timed out after {attempt} configured attempts"
                        ) from error
                    raise NovaRetryExhaustedError(
                        f"Nova failed after {attempt} configured attempts"
                    ) from error
            finally:
                input_tokens, output_tokens = _usage(response)
                self._recorder.record(
                    NovaInvocation(
                        # The id actually invoked, not the one configured. When the fallback fires
                        # these differ, and the record has to say which model answered.
                        model_id=model_id,
                        prompt_id=self._config.prompt_id,
                        template_id=self._config.template_id,
                        attempt=attempt,
                        latency_ms=_milliseconds_since(started_ns),
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        outcome=outcome,
                        request_id=_request_id(response),
                        context=request.context,
                        bound_pt=request.bound_pt,
                        injection_attempts=prepared.injection_attempts,
                        rejection_reason=rejection_reason,
                    )
                )
        raise NovaRetryExhaustedError("Nova retry loop ended unexpectedly") from last_error

    def _request(self, request: NovaRequest, model_id: str) -> dict[str, object]:
        coordinate_mode = _coordinate_mode(self._config)
        prepared = prepare_prompt(
            request.context,
            coordinate_instruction=_coordinate_instruction(coordinate_mode),
        )
        return {
            "modelId": model_id,
            "system": [{"text": prepared.system_instruction}],
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "image": {
                                "format": request.image_format,
                                "source": {"bytes": request.crop},
                            }
                        },
                        {"text": prepared.user_task},
                        {"text": prepared.drawing_data},
                    ],
                }
            ],
            "inferenceConfig": {"temperature": 0, "maxTokens": DIMENSION_READER_MAX_TOKENS},
            "additionalModelRequestFields": {"inferenceConfig": {"topK": 1}},
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": TOOL_NAME,
                            "description": "Report one visible dimension reading and rectangle.",
                            "inputSchema": {"json": _bedrock_tool_schema()},
                        }
                    }
                ],
                "toolChoice": {"tool": {"name": TOOL_NAME}},
            },
        }

    def _candidate(self, response: Mapping[str, Any], request: NovaRequest) -> ObservationCandidate:
        stop_reason = response.get("stopReason")
        if stop_reason in {"content_filtered", "guardrail_intervened"}:
            raise NovaRefusalError(f"Bedrock stopped the request: {stop_reason}")
        output = response.get("output")
        message = output.get("message") if isinstance(output, Mapping) else None
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            raise NovaProtocolError("Bedrock response has no tool content")
        tool_calls = [
            block.get("toolUse")
            for block in content
            if isinstance(block, Mapping) and isinstance(block.get("toolUse"), Mapping)
        ]
        if len(tool_calls) != 1 or len(content) != 1:
            # Which of three things came back, so a run can count them apart (#792). On AI_Set_2 one
            # shared sentence covered 383 of Mistral Large 3's refusals; replayed, they were two or
            # three tool calls for a crop holding two labels, not the prose the sentence implied.
            if not tool_calls:
                returned = "no tool call"
            elif len(tool_calls) > 1:
                returned = f"{len(tool_calls)} tool calls"
            else:
                returned = "model text beside its tool call"
            raise NovaProtocolError(
                f"Bedrock must return exactly one tool call and no model text; it returned {returned}"
            )
        tool_call = cast(Mapping[str, Any], tool_calls[0])
        if tool_call.get("name") != TOOL_NAME:
            raise NovaProtocolError(f"Bedrock called an unexpected tool: {tool_call.get('name')!r}")
        coordinate_mode = _coordinate_mode(self._config)
        outcome = validate_payload(
            tool_call.get("input"),
            context=CandidateContext(
                candidate_id=request.candidate_id,
                extractor_version=self._config.model_id,
                page=request.page,
                extractor=self._config.extractor,
            ),
            crop_size=_crop_size(request.crop, request.image_format),
            coordinate_mode=coordinate_mode,
            recorder=self._recorder,
            stacked_label=request.stacked_label,
            stacked_layouts=request.stacked_layouts,
        )
        if isinstance(outcome, ValidationRejection):
            raise NovaPayloadRejectedError(outcome)
        return outcome
