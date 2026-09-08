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


class StructuredOutputStrategy(StrEnum):
    """How the model is made to answer in a shape the validator can read.

    **The adapter's contract is a validated structured reading, not a tool call.** Bedrock's Nova is
    made to answer by forcing one function; many vision models cannot hold a tool schema at all, and
    those are exactly the self-hosted open-source ones a privacy-conscious deployment would prefer —
    `minicpm-v` reads a dimension correctly and reports `capabilities: ['completion', 'vision']`.

    So the shape is a strategy behind the interface. Both routes end at `validate_payload` with the
    same schema and produce the same `ObservationCandidate`; nothing outside this module can tell
    which was used, which is the point.
    """

    TOOL = "tool"
    """Force one function call. What Nova does, and what a tool-capable endpoint should do."""

    SCHEMA = "schema"
    """Constrain the reply to a JSON schema. For a model with vision and no tools."""

    AUTO = "auto"
    """Ask the endpoint what the model can do, and fall back on what it says when it refuses."""


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
    strategy: StructuredOutputStrategy = StructuredOutputStrategy.AUTO
    """Left to `AUTO` unless an operator knows better than the endpoint does."""

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

    def capabilities(self, model_id: str) -> frozenset[str]:
        """What the endpoint says this model can do, or an empty set when it will not say.

        Empty means *unknown*, never *nothing*: an endpoint that does not publish capabilities is
        not an endpoint whose models have none, and the difference decides whether the adapter may
        pick a strategy from the answer or has to find out by being refused.
        """


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


class OpenModelEndpointError(OpenModelAdapterError):
    """The endpoint answered with an HTTP error, carrying its own explanation.

    Its own class because the body distinguishes cases a caller must tell apart: a model that cannot
    hold a tool schema is a fact about that model, where a malformed request is a fact about us. Both
    arrive as a 400, and only the message separates them.
    """

    def __init__(self, message: str, *, status: int, body: str) -> None:
        super().__init__(message)
        self.status = status
        self.body = body

    @property
    def lacks_tool_support(self) -> bool:
        """Whether the endpoint said this model cannot use tools.

        Read from the endpoint's own words rather than inferred from the status, because a 400 covers
        both that and a request we built wrongly — and those call for opposite responses: choose a
        different model, or fix the adapter.
        """
        return "does not support tools" in self.body.casefold()


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
        """Return one validated candidate, whichever way this model can be made to answer.

        The structure mirrors `NovaAdapter.extract`, including which outcome each failure records and
        which exceptions are re-raised rather than retried. What it adds is the choice of strategy —
        and one fallback, for the case the endpoint only reveals by refusing.
        """
        strategy = self._strategy()
        try:
            return self._attempt(request, strategy)
        except OpenModelServiceError as error:
            cause = error.__cause__
            if (
                strategy is not StructuredOutputStrategy.TOOL
                or not isinstance(cause, OpenModelEndpointError)
                or not cause.lacks_tool_support
            ):
                raise
            # **The endpoint just told us what it could not tell us before.** An endpoint that
            # publishes no capabilities gets the stricter contract first, and a model that cannot
            # hold a tool schema says so in the body of a 400. Retrying once with the schema route
            # is not a guess: it is the answer we were given.
            #
            # The refused attempt stays recorded. It happened, it cost something, and a record that
            # showed only the successful shape would misstate what this call did.
            return self._attempt(request, StructuredOutputStrategy.SCHEMA)

    def _strategy(self) -> StructuredOutputStrategy:
        """The configured strategy, or the one the endpoint's own answer implies.

        An operator's choice wins: a non-Ollama endpoint may know its model's abilities better than
        any probe of ours. Otherwise the capabilities decide, and silence means the stricter contract
        is tried first — being refused is more informative than assuming the weaker one.
        """
        if self._config.strategy is not StructuredOutputStrategy.AUTO:
            return self._config.strategy
        try:
            published = self._client.capabilities(self._config.model_id)
        except Exception:  # noqa: BLE001 - a probe that fails must not stop the call it precedes
            published = frozenset()
        if not published:
            return StructuredOutputStrategy.TOOL
        return (
            StructuredOutputStrategy.TOOL
            if "tools" in published
            else StructuredOutputStrategy.SCHEMA
        )

    def _attempt(
        self, request: OpenModelRequest, strategy: StructuredOutputStrategy
    ) -> ObservationCandidate:
        """One strategy, with its own bounded retry loop and its own records."""
        last_error: Exception | None = None
        prepared = prepare_prompt(request.context)
        for attempt in range(1, self._config.max_attempts + 1):
            started_ns = monotonic_ns()
            response: Mapping[str, Any] | None = None
            outcome = OpenModelInvocationOutcome.ERROR
            try:
                response = self._client.complete(**self._request(request, strategy))
                candidate = self._candidate(response, request, strategy)
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

    def _request(
        self, request: OpenModelRequest, strategy: StructuredOutputStrategy
    ) -> dict[str, object]:
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
            # Zero, because the same crop should produce the same reading twice. A model that varies
            # its answer between identical calls is one nothing downstream could corroborate.
            "temperature": 0,
            **self._constraint(schema, strategy),
        }

    def _constraint(
        self, schema: Mapping[str, object], strategy: StructuredOutputStrategy
    ) -> dict[str, object]:
        """The part of the body that forces a shape, which is all the two strategies differ by.

        One schema, two ways of insisting on it. `strict` is set on the schema route because a
        schema the endpoint treats as advice is not a constraint, and an unconstrained reply is the
        free text this adapter refuses.
        """
        if strategy is StructuredOutputStrategy.SCHEMA:
            return {
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {"name": TOOL_NAME, "schema": schema, "strict": True},
                }
            }
        return {
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
        }

    def _candidate(
        self,
        response: Mapping[str, Any],
        request: OpenModelRequest,
        strategy: StructuredOutputStrategy,
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

        arguments = (
            self._from_content(message)
            if strategy is StructuredOutputStrategy.SCHEMA
            else self._from_tool_call(message)
        )
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

    def _from_content(self, message: Mapping[str, Any]) -> object:
        """The schema route's payload: the reply itself, which must be JSON.

        **`parse_float=Decimal`, and it is not optional.** A JSON number without a decimal point is
        an `int`, but `112.0` is a `float`, and `validate_payload` rejects floats outright — a
        dimension that went through binary floating point is one the units layer can no longer call
        exact (ADR-0001). Measured, not anticipated: `minicpm-v` returns its polygon as `112.0`.

        Prose is a protocol error, not something to salvage. A model describing the dimension in a
        sentence has not produced a reading, and reading a number out of that sentence is the guess
        this whole layer exists to refuse.
        """
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise OpenModelProtocolError("the schema route returned no content to validate")
        try:
            return json.loads(content, parse_float=Decimal)
        except json.JSONDecodeError as error:
            raise OpenModelProtocolError(
                "the model answered with text rather than the requested JSON"
            ) from error

    def _from_tool_call(self, message: Mapping[str, Any]) -> object:
        """The tool route's payload: exactly one forced call, and no prose beside it."""
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
            # `parse_float=Decimal` for the reason `_from_content` gives at length.
            try:
                return json.loads(arguments, parse_float=Decimal)
            except json.JSONDecodeError as error:
                raise OpenModelProtocolError("the tool arguments are not valid JSON") from error
        return arguments


class _UrllibChatClient:
    """The default transport: one POST, standard library only."""

    def __init__(self, config: OpenModelConfig) -> None:
        self._config = config

    def capabilities(self, model_id: str) -> frozenset[str]:
        """What Ollama says this model can do, from `/api/show`.

        An empty set for anything that is not an Ollama — the endpoint is asked, and one that does
        not answer that question has not said its models can do nothing. `AUTO` reads the difference:
        a published list decides the strategy, silence means try the stricter contract and learn
        from the refusal.

        The URL drops the `/v1` the chat route carries, because `/api/show` is Ollama's own surface
        rather than the compatibility one.
        """
        import urllib.error
        import urllib.request

        root = self._config.base_url.rstrip("/").removesuffix("/v1")
        request = urllib.request.Request(
            f"{root}/api/show",
            data=json.dumps({"model": model_id}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self._config.connect_timeout_seconds
            ) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
            return frozenset()
        published = decoded.get("capabilities") if isinstance(decoded, Mapping) else None
        if not isinstance(published, list):
            return frozenset()
        return frozenset(item for item in published if isinstance(item, str))

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
        try:
            with urllib.request.urlopen(
                request, timeout=self._config.read_timeout_seconds
            ) as response:
                decoded = json.loads(response.read().decode("utf-8"), parse_float=Decimal)
        except urllib.error.HTTPError as error:
            # **The body is where the reason is, and discarding it loses the diagnosis.** Ollama
            # answers `400 {"error":{"message":"... does not support tools"}}` for a model that
            # cannot hold a tool schema — a fact about that model, and unrecoverable from the status
            # alone, which says only that something about the request was wrong.
            try:
                detail = error.read().decode("utf-8", errors="replace")[:500]
            except Exception:  # noqa: BLE001 - a body we cannot read must not replace the error
                detail = ""
            raise OpenModelEndpointError(
                f"the endpoint refused the request with HTTP {error.code}"
                + (f": {detail}" if detail else ""),
                status=error.code,
                body=detail,
            ) from error
        if not isinstance(decoded, Mapping):
            raise OpenModelProtocolError("the endpoint did not return a JSON object")
        return cast(Mapping[str, Any], decoded)
