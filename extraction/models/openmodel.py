"""The same model seam, against a free or local model, to prove the plumbing (#534).

`NovaAdapter` is the only implementation of this contract and it cannot be run: Bedrock needs a
credential, and the provider decision has not been made. So the seam has never carried a real model
call, and we would have found out whether it holds on the day we were also paying for it. This
adapter carries one now, for nothing, against a model that can run on the machine the drawings are
already on.

**This is not the provider decision.** The model here is a stand-in chosen because it is free and
local, and nothing about that choice is a recommendation. Abhishek decides the provider, and it swaps
in through `OpenModelConfig` — the pipeline seam is `extract(request) -> ObservationCandidate` either
way, which is the whole point of writing a second one.

**This makes no accuracy claim.** It proves that a crop goes out and a validated structured reading
comes back, and nothing further. How *often* a model reads a dimension correctly is a question for
the gold set, which is empty (#188), and this module adds no gate and reports no rate. A free model
getting a synthetic crop right says nothing about a real scan.

**Nothing here is wired into the pipeline.** Semantic typing is gated on the real drawings (#274) and
the vocabulary Q20 defers, and no module under `app/` or `workflow/` imports this.

**The transport is OpenAI-compatible chat completions**, which Ollama, OpenRouter and Google's
compatibility endpoint all speak. One shape therefore covers the local case and the free-tier case,
and the difference between them is a base URL and whether a key is present.

Source: issue #534. Verification: ``tests/extraction/models/test_openmodel.py``.
"""

from __future__ import annotations

import json
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

#: The one function the model may call. Named identically to Nova's, because the two adapters are
#: deliberately the same seam and a reader comparing them should find nothing that differs by accident.
TOOL_NAME = "report_drawing_reading"

#: Where a local Ollama listens. A default rather than a guess: it is the address Ollama binds by
#: default, and every other setting has to be given explicitly.
DEFAULT_BASE_URL = "http://localhost:11434/v1"


@dataclass(frozen=True, slots=True)
class OpenModelConfig:
    """Explicit model identity and transport bounds; no guessed defaults except the local address.

    Mirrors `NovaConfig` field for field, plus the two things a non-AWS endpoint needs: where it is,
    and a key when it wants one. `api_key` is `None` for a local model, and that is the ordinary case
    rather than a degraded one.
    """

    model_id: str
    prompt_id: str
    template_id: str
    connect_timeout_seconds: int
    read_timeout_seconds: int
    max_attempts: int
    base_url: str = DEFAULT_BASE_URL
    api_key: str | None = None

    def __post_init__(self) -> None:
        for name in ("model_id", "prompt_id", "template_id", "base_url"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        for name in ("connect_timeout_seconds", "read_timeout_seconds", "max_attempts"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.api_key is not None and (
            not isinstance(self.api_key, str) or not self.api_key.strip()
        ):
            raise ValueError("api_key must be a non-empty string or None")


@dataclass(frozen=True, slots=True)
class OpenModelRequest:
    """One bounded crop request carrying the provenance needed for its candidate.

    The same fields as `NovaRequest`, and for the same reasons — see that type. Kept as its own class
    rather than imported so neither adapter can quietly change the other's contract.
    """

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


class OpenModelInvocationOutcome(StrEnum):
    """Closed outcomes recorded for every attempted call.

    The same members as `NovaInvocationOutcome`, asserted by
    `test_the_two_adapters_are_the_same_seam`: a provider swap must not change what an operator reads
    in `model_invocations`.
    """

    OK = "ok"
    TIMEOUT = "timeout"
    RETRYABLE_ERROR = "retryable_error"
    REFUSED = "refused"
    REJECTED = "rejected"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class OpenModelInvocation:
    """Audit metadata for one call attempt, including failed attempts."""

    model_id: str
    prompt_id: str
    template_id: str
    attempt: int
    latency_ms: int
    input_tokens: int
    output_tokens: int
    outcome: OpenModelInvocationOutcome
    request_id: str | None
    context: AssembledContext
    bound_pt: Decimal
    injection_attempts: tuple[InjectionAttempt, ...]


class InvocationRecorder(RejectionRecorder, Protocol):
    """Persistence boundary supplied by the caller."""

    def record(self, invocation: OpenModelInvocation) -> None:
        """Persist one immutable invocation record."""


class ChatCompletionsClient(Protocol):
    """The small surface this adapter uses, replaceable in unit tests.

    One method, like Nova's `converse`, so a test can hand over a recorded response without a server
    and without the network.
    """

    def complete(self, **kwargs: object) -> Mapping[str, Any]:
        """Invoke a messages-capable model and return its decoded response."""


class OpenModelAdapterError(Exception):
    """Base class for every failure this adapter raises."""


class OpenModelTimeoutError(OpenModelAdapterError):
    """The endpoint did not answer within the configured budget."""


class OpenModelRetryExhaustedError(OpenModelAdapterError):
    """Every configured attempt failed retryably."""


class OpenModelProtocolError(OpenModelAdapterError):
    """The response was not the single forced tool call this adapter requires."""


class OpenModelPayloadRejectedError(OpenModelAdapterError):
    """The tool call did not validate; the rejection is recorded and carried."""

    def __init__(self, rejection: ValidationRejection) -> None:
        super().__init__(rejection.reason)
        self.rejection = rejection


class OpenModelRefusalError(OpenModelAdapterError):
    """The model declined to answer."""


class OpenModelServiceError(OpenModelAdapterError):
    """The endpoint failed in a way retrying cannot help."""


def _milliseconds_since(started_ns: int) -> int:
    return max(0, (monotonic_ns() - started_ns) // 1_000_000)


def _usage(response: Mapping[str, Any] | None) -> tuple[int, int]:
    """Token counts when the endpoint reports them, zeroes when it does not.

    A local model often reports nothing, and zero is the honest reading of that: no tokens were
    *reported*, and the column is diagnostic. Nothing decides anything from it.
    """
    usage = response.get("usage") if isinstance(response, Mapping) else None
    if not isinstance(usage, Mapping):
        return 0, 0
    prompt = usage.get("prompt_tokens")
    completion = usage.get("completion_tokens")
    return (
        prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else 0,
        completion if isinstance(completion, int) and not isinstance(completion, bool) else 0,
    )


def _request_id(response: Mapping[str, Any] | None) -> str | None:
    identifier = response.get("id") if isinstance(response, Mapping) else None
    return identifier if isinstance(identifier, str) and identifier.strip() else None


def _is_timeout(error: Exception) -> bool:
    """Whether this failure was the endpoint not answering in time.

    Matched on the exception's own type name rather than by importing a transport's classes: this
    adapter is meant to work behind several of them, and importing one to classify its errors would
    make it depend on the transport it is trying to stay independent of.
    """
    return "timeout" in type(error).__name__.casefold()


def _is_retryable(error: Exception) -> bool:
    """A timeout or a connection failure. Everything else is raised without a second attempt.

    Deliberately narrow. Retrying a malformed response or a refusal spends time and produces the same
    answer, and retrying something we have not classified is how a bounded call becomes an unbounded
    one.
    """
    name = type(error).__name__.casefold()
    return "timeout" in name or "connection" in name


class OpenModelAdapter:
    """Invoke a free or local model through one forced tool and return only an uncertain candidate.

    Every meaningful line here has a counterpart in `NovaAdapter`. That is the deliverable: two
    transports, one seam, so the provider decision is a configuration change rather than a rewrite.
    """

    def __init__(
        self,
        config: OpenModelConfig,
        client: ChatCompletionsClient,
        recorder: InvocationRecorder,
    ) -> None:
        self._config = config
        self._client = client
        self._recorder = recorder

    @classmethod
    def from_environment(
        cls, config: OpenModelConfig, recorder: InvocationRecorder
    ) -> OpenModelAdapter:
        """Build the client from configuration, with no dependency beyond the standard library.

        `urllib` rather than an HTTP library, because this adapter exists to prove a seam and adding
        a dependency to do it would be a poor trade — `httpx` is a development dependency here, not a
        runtime one, and `boto3` is Nova's alone.
        """
        return cls(config, _UrllibChatClient(config), recorder)

    def extract(self, request: OpenModelRequest) -> ObservationCandidate:
        """Call the required tool, validating locally and failing explicitly.

        The structure mirrors `NovaAdapter.extract` exactly, including which outcome each failure
        records and which exceptions are re-raised rather than retried.
        """
        last_error: Exception | None = None
        prepared = prepare_prompt(request.context)
        for attempt in range(1, self._config.max_attempts + 1):
            started_ns = monotonic_ns()
            response: Mapping[str, Any] | None = None
            outcome = OpenModelInvocationOutcome.ERROR
            try:
                response = self._client.complete(**self._request(request))
                candidate = self._candidate(response, request)
                outcome = OpenModelInvocationOutcome.OK
                return candidate
            except OpenModelRefusalError:
                outcome = OpenModelInvocationOutcome.REFUSED
                raise
            except (OpenModelProtocolError, OpenModelPayloadRejectedError):
                outcome = OpenModelInvocationOutcome.REJECTED
                raise
            except Exception as error:
                last_error = error
                if not _is_retryable(error):
                    outcome = OpenModelInvocationOutcome.ERROR
                    raise OpenModelServiceError(
                        "the model endpoint failed without retry"
                    ) from error
                outcome = (
                    OpenModelInvocationOutcome.TIMEOUT
                    if _is_timeout(error)
                    else OpenModelInvocationOutcome.RETRYABLE_ERROR
                )
                if attempt == self._config.max_attempts:
                    if _is_timeout(error):
                        raise OpenModelTimeoutError(
                            f"the model timed out after {attempt} configured attempts"
                        ) from error
                    raise OpenModelRetryExhaustedError(
                        f"the model failed after {attempt} configured attempts"
                    ) from error
            finally:
                input_tokens, output_tokens = _usage(response)
                self._recorder.record(
                    OpenModelInvocation(
                        model_id=self._config.model_id,
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
        raise OpenModelRetryExhaustedError("the retry loop ended unexpectedly") from last_error

    def _request(self, request: OpenModelRequest) -> dict[str, object]:
        """The chat-completions body, with the tool forced and the schema the validator enforces.

        The image is a data URL because that is what the OpenAI-compatible shape takes, where Bedrock
        takes raw bytes. It is the one real difference between the two adapters and it is confined to
        this method.
        """
        import base64

        schema = NovaToolPayload.model_json_schema()
        prepared = prepare_prompt(request.context)
        encoded = base64.b64encode(request.crop).decode("ascii")
        return {
            "model": self._config.model_id,
            "messages": [
                {"role": "system", "content": prepared.system_instruction},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/{request.image_format};base64,{encoded}"
                            },
                        },
                        {"type": "text", "text": prepared.user_task},
                        {"type": "text", "text": prepared.drawing_data},
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": TOOL_NAME,
                        "description": "Report one visible dimension reading and polygon.",
                        "parameters": schema,
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": TOOL_NAME}},
            # Zero, because the same crop should produce the same reading twice. A model that varies
            # its answer between identical calls is one nothing downstream could corroborate.
            "temperature": 0,
        }

    def _candidate(
        self, response: Mapping[str, Any], request: OpenModelRequest
    ) -> ObservationCandidate:
        """The one tool call, validated — or an explicit failure.

        **No free-text path, deliberately.** A model that answers in prose instead of calling the
        tool has not produced a reading, and parsing prose into a dimension is exactly the guess this
        whole layer exists to refuse. It is a protocol error and it is raised.
        """
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise OpenModelProtocolError("the response did not carry exactly one choice")
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise OpenModelProtocolError("the response choice is not an object")

        if choice.get("finish_reason") in {"content_filter", "refusal"}:
            raise OpenModelRefusalError(
                f"the model declined the request: {choice.get('finish_reason')}"
            )

        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise OpenModelProtocolError("the response choice has no message")
        if message.get("refusal"):
            raise OpenModelRefusalError("the model returned a refusal")

        calls = message.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise OpenModelProtocolError("the model must return exactly one tool call and no prose")
        call = calls[0]
        function = call.get("function") if isinstance(call, Mapping) else None
        if not isinstance(function, Mapping):
            raise OpenModelProtocolError("the tool call has no function")
        if function.get("name") != TOOL_NAME:
            raise OpenModelProtocolError(
                f"the model called an unexpected tool: {function.get('name')!r}"
            )

        arguments = function.get("arguments")
        if isinstance(arguments, str):
            # The compatible shape sends arguments as a JSON string; Bedrock sends an object.
            # `parse_float=Decimal` matters: the validator rejects floats outright, and letting
            # `json` produce one here would turn a valid reading into a rejection.
            try:
                arguments = json.loads(arguments, parse_float=Decimal)
            except json.JSONDecodeError as error:
                raise OpenModelProtocolError("the tool arguments are not valid JSON") from error

        outcome = validate_payload(
            arguments,
            context=CandidateContext(
                candidate_id=request.candidate_id,
                extractor_version=self._config.model_id,
                page=request.page,
                # Names the transport this reading came through, so an operator reading
                # `model_invocations` can tell a Bedrock reading from a local one.
                extractor="openmodel",
            ),
            recorder=self._recorder,
        )
        if isinstance(outcome, ValidationRejection):
            raise OpenModelPayloadRejectedError(outcome)
        return outcome


class _UrllibChatClient:
    """The default transport: one POST, standard library only."""

    def __init__(self, config: OpenModelConfig) -> None:
        self._config = config

    def complete(self, **kwargs: object) -> Mapping[str, Any]:
        import urllib.error
        import urllib.request

        body = json.dumps(kwargs).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._config.api_key is not None:
            headers["Authorization"] = f"Bearer {self._config.api_key}"

        request = urllib.request.Request(
            f"{self._config.base_url.rstrip('/')}/chat/completions",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self._config.read_timeout_seconds) as response:
            decoded = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        if not isinstance(decoded, Mapping):
            raise OpenModelProtocolError("the endpoint did not return a JSON object")
        return cast(Mapping[str, Any], decoded)
