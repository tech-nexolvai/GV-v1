"""Strict Amazon Bedrock Nova 2 Lite tool-call adapter.

The model may report only a raw observation candidate. It cannot produce evidence or
a verdict, and ordinary model text is never treated as structured output.

Source: ``docs/DESIGN_AI.md`` section 4.1 and issue #249.
Verification: ``tests/extraction/models/test_nova.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from time import monotonic_ns
from typing import Any, Literal, Protocol, cast

from evidence.candidate import ObservationCandidate
from extraction.models.context import AssembledContext
from extraction.models.sanitisation import InjectionAttempt, prepare_prompt
from extraction.models.validation import (
    CandidateContext,
    NovaToolPayload,
    RejectionRecorder,
    ValidationRejection,
    validate_payload,
)

TOOL_NAME = "report_drawing_reading"

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

    def __post_init__(self) -> None:
        for name in ("model_id", "prompt_id", "template_id"):
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


def _needs_inference_profile(error: BaseException, model_id: str) -> bool:
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

    def __post_init__(self) -> None:
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


class NovaAdapter:
    """Invoke Nova through one forced tool and return only an uncertain candidate."""

    def __init__(
        self,
        config: NovaConfig,
        client: BedrockRuntimeClient,
        recorder: InvocationRecorder,
    ) -> None:
        self._config = config
        self._client = client
        self._recorder = recorder

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
        """
        try:
            return self._attempt(request, self._config.model_id)
        except NovaServiceError as error:
            cause = error.__cause__
            if cause is None or not _needs_inference_profile(cause, self._config.model_id):
                raise
            return self._attempt(request, f"{INFERENCE_PROFILE_PREFIX}{self._config.model_id}")

    def _attempt(self, request: NovaRequest, model_id: str) -> ObservationCandidate:
        """One model id, with its own bounded retry loop and its own records."""

        last_error: Exception | None = None
        prepared = prepare_prompt(request.context)
        for attempt in range(1, self._config.max_attempts + 1):
            started_ns = monotonic_ns()
            response: Mapping[str, Any] | None = None
            outcome = NovaInvocationOutcome.ERROR
            try:
                response = self._client.converse(**self._request(request, model_id))
                candidate = self._candidate(response, request)
                outcome = NovaInvocationOutcome.OK
                return candidate
            except NovaRefusalError:
                outcome = NovaInvocationOutcome.REFUSED
                raise
            except (NovaProtocolError, NovaPayloadRejectedError):
                outcome = NovaInvocationOutcome.REJECTED
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
                    )
                )
        raise NovaRetryExhaustedError("Nova retry loop ended unexpectedly") from last_error

    def _request(self, request: NovaRequest, model_id: str) -> dict[str, object]:
        schema = NovaToolPayload.model_json_schema()
        prepared = prepare_prompt(request.context)
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
            "toolConfig": {
                "tools": [
                    {
                        "toolSpec": {
                            "name": TOOL_NAME,
                            "description": "Report one visible dimension reading and polygon.",
                            "inputSchema": {"json": schema},
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
            raise NovaProtocolError("Bedrock must return exactly one tool call and no model text")
        tool_call = cast(Mapping[str, Any], tool_calls[0])
        if tool_call.get("name") != TOOL_NAME:
            raise NovaProtocolError(f"Bedrock called an unexpected tool: {tool_call.get('name')!r}")
        outcome = validate_payload(
            tool_call.get("input"),
            context=CandidateContext(
                candidate_id=request.candidate_id,
                extractor_version=self._config.model_id,
                page=request.page,
            ),
            recorder=self._recorder,
        )
        if isinstance(outcome, ValidationRejection):
            raise NovaPayloadRejectedError(outcome)
        return outcome
