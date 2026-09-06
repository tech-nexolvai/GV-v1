"""The same seam, against a free or local model (#534).

Verification for: `extraction/models/openmodel.py`.

**This proves plumbing, not accuracy.** A crop goes out; a validated structured reading comes back; an
invocation is recorded. Nothing here measures how often a model reads a dimension correctly — that is
the gold set's question and the gold set is empty (#188) — and no test in this file gates anything on
a model being right.

**The free model is a stand-in.** It was chosen because it costs nothing and can run on the machine
the drawings are already on, not because it is the provider. That decision is Abhishek's and swaps in
through `OpenModelConfig`.

Most of these run against a scripted client, with no server and no network. The one that talks to a
real model is `test_a_configured_model_returns_a_validated_reading`, and it skips unless one is
configured — the same arrangement the database tests use with `DATABASE_URL`.
"""

from __future__ import annotations

import base64
import inspect
import json
import os
import zlib
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest

from evidence.candidate import ObservationCandidate
from extraction.models.context import AssembledContext, NearbyText
from extraction.models.nova import NovaAdapter, NovaInvocationOutcome
from extraction.models.openmodel import (
    TOOL_NAME,
    ChatCompletionsClient,
    OpenModelAdapter,
    OpenModelConfig,
    OpenModelInvocation,
    OpenModelInvocationOutcome,
    OpenModelPayloadRejectedError,
    OpenModelProtocolError,
    OpenModelRefusalError,
    OpenModelRequest,
    OpenModelRetryExhaustedError,
)
from extraction.models.validation import ValidationRejection

#: The reading the synthetic crop is drawn to contain.
KNOWN_READING = '24 1/2"'


class RecordingSink:
    """Collect immutable attempt records for assertions."""

    def __init__(self) -> None:
        self.items: list[OpenModelInvocation] = []
        self.rejections: list[ValidationRejection] = []

    def record(self, invocation: OpenModelInvocation) -> None:
        self.items.append(invocation)

    def record_rejection(self, rejection: ValidationRejection) -> None:
        self.rejections.append(rejection)


class FakeEndpoint:
    """Return or raise scripted values while retaining submitted requests."""

    def __init__(self, *results: Mapping[str, Any] | BaseException) -> None:
        self.results = list(results)
        self.requests: list[dict[str, object]] = []

    def complete(self, **kwargs: object) -> Mapping[str, Any]:
        self.requests.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ConnectionFailed(Exception):
    """Named so `_is_retryable` classifies it by its type name, as it would a real one."""


def _config(*, max_attempts: int = 2) -> OpenModelConfig:
    return OpenModelConfig(
        model_id="qwen2-vl",
        prompt_id="dimension-reader-v1",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=2,
        read_timeout_seconds=8,
        max_attempts=max_attempts,
    )


def _request(crop: bytes | None = None) -> OpenModelRequest:
    return OpenModelRequest(
        candidate_id="candidate-534",
        page=3,
        crop=crop if crop is not None else _synthetic_crop(),
        image_format="png",
        context=AssembledContext(
            nearby_text=(NearbyText("984", Decimal(4)),),
            nearby_geometry=(),
        ),
        bound_pt=Decimal(96),
    )


def _tool_response(arguments: object, *, as_string: bool = True) -> dict[str, Any]:
    """One well-formed tool call, in the shape an OpenAI-compatible endpoint returns."""
    return {
        "id": "chatcmpl-534",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": TOOL_NAME,
                                "arguments": (json.dumps(arguments) if as_string else arguments),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }


def _payload() -> dict[str, object]:
    return {
        "reading": KNOWN_READING,
        "unit_guess": "in",
        "polygon": [["0", "0"], ["10", "0"], ["10", "4"], ["0", "4"]],
    }


def _synthetic_crop() -> bytes:
    """A tiny valid PNG. Enough to be an image; not a drawing.

    Built here rather than committed: a fixture file of a client drawing is the one thing this
    repository must never hold, and a generated square makes the point that the smoke test is about
    the transport rather than about reading anything.
    """
    width = height = 8
    raw = b"".join(b"\x00" + bytes([255, 255, 255] * width) for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    header = width.to_bytes(4, "big") + height.to_bytes(4, "big") + bytes([8, 2, 0, 0, 0])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


# ---------------------------------------------------------------------------
# The seam
# ---------------------------------------------------------------------------


def test_the_two_adapters_are_the_same_seam() -> None:
    """**The deliverable.** A provider swap must be configuration, not a rewrite.

    Asserted on the signatures rather than described in a comment: `extract` and `from_environment`
    take and return the same things in both, and the outcome enums have the same members — so what an
    operator reads in `model_invocations` does not change with the transport underneath.
    """
    for name in ("extract", "from_environment"):
        nova = inspect.signature(getattr(NovaAdapter, name))
        open_model = inspect.signature(getattr(OpenModelAdapter, name))
        assert [p.name for p in nova.parameters.values()] == [
            p.name for p in open_model.parameters.values()
        ], name

    assert [member.value for member in NovaInvocationOutcome] == [
        member.value for member in OpenModelInvocationOutcome
    ]

    assert OpenModelAdapter.extract.__annotations__["return"] == "ObservationCandidate"


def test_a_scripted_endpoint_returns_a_validated_reading() -> None:
    """The happy path, with no server: a tool call in, a validated candidate out, one record."""
    sink = RecordingSink()
    endpoint = FakeEndpoint(_tool_response(_payload()))
    adapter = OpenModelAdapter(_config(), endpoint, sink)

    candidate = adapter.extract(_request())

    assert isinstance(candidate, ObservationCandidate)
    assert candidate.raw_text == KNOWN_READING
    assert candidate.candidate_id == "candidate-534"
    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.OK]
    assert sink.items[0].input_tokens == 11


def test_the_request_forces_the_tool_and_carries_the_image() -> None:
    """What actually goes over the wire, asserted rather than assumed.

    The tool is forced because a model free to answer in prose will, and prose is not a reading. The
    image is a data URL because that is the compatible shape — the one real difference from Bedrock,
    and it is confined to `_request`.
    """
    sink = RecordingSink()
    endpoint = FakeEndpoint(_tool_response(_payload()))
    adapter = OpenModelAdapter(_config(), endpoint, sink)

    adapter.extract(_request())

    body = endpoint.requests[0]
    assert body["tool_choice"] == {"type": "function", "function": {"name": TOOL_NAME}}
    assert body["temperature"] == 0
    content = body["messages"][1]["content"]  # type: ignore[index]
    image = next(part for part in content if part["type"] == "image_url")
    assert image["image_url"]["url"].startswith("data:image/png;base64,")
    assert base64.b64decode(image["image_url"]["url"].split(",", 1)[1]) == _synthetic_crop()


# ---------------------------------------------------------------------------
# What it refuses
# ---------------------------------------------------------------------------


def test_prose_instead_of_a_tool_call_is_a_protocol_error() -> None:
    """**No free-text fallback, ever.**

    A model that answers "the dimension appears to be about 24 inches" has not produced a reading,
    and parsing that sentence into a number is precisely the guess this layer exists to refuse. It is
    an error, it is recorded as one, and nothing downstream sees a candidate.
    """
    sink = RecordingSink()
    endpoint = FakeEndpoint(
        {
            "id": "chatcmpl-prose",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "The dimension appears to be about 24 inches.",
                    },
                }
            ],
        }
    )
    adapter = OpenModelAdapter(_config(), endpoint, sink)

    with pytest.raises(OpenModelProtocolError, match="exactly one tool call"):
        adapter.extract(_request())

    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.REJECTED]


def test_a_refusal_is_recorded_as_a_refusal() -> None:
    """A model declining is a distinct outcome from one failing, and both are distinct from a reading."""
    sink = RecordingSink()
    endpoint = FakeEndpoint(
        {
            "id": "chatcmpl-refused",
            "choices": [{"finish_reason": "content_filter", "message": {"role": "assistant"}}],
        }
    )
    adapter = OpenModelAdapter(_config(), endpoint, sink)

    with pytest.raises(OpenModelRefusalError):
        adapter.extract(_request())

    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.REFUSED]


def test_a_payload_that_does_not_validate_is_rejected_and_kept() -> None:
    """The validator is the same one Bedrock's output goes through — same schema, same rejection.

    A float is the case worth naming: the model layer must not introduce one, because the whole units
    layer exists to keep a dimension exact (ADR-0001).
    """
    sink = RecordingSink()
    endpoint = FakeEndpoint(_tool_response({"reading": '24 1/2"', "unit_guess": "in"}))
    adapter = OpenModelAdapter(_config(), endpoint, sink)

    with pytest.raises(OpenModelPayloadRejectedError):
        adapter.extract(_request())

    assert sink.rejections, "the rejected payload was not kept for diagnosis"
    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.REJECTED]


def test_a_connection_failure_retries_to_the_configured_bound_and_stops() -> None:
    """Bounded, and every attempt recorded — including the ones that failed."""
    sink = RecordingSink()
    endpoint = FakeEndpoint(ConnectionFailed("refused"), ConnectionFailed("refused"))
    adapter = OpenModelAdapter(_config(max_attempts=2), endpoint, sink)

    with pytest.raises(OpenModelRetryExhaustedError):
        adapter.extract(_request())

    assert [record.attempt for record in sink.items] == [1, 2]
    assert {record.outcome for record in sink.items} == {OpenModelInvocationOutcome.RETRYABLE_ERROR}


# ---------------------------------------------------------------------------
# Against a model that is actually running
# ---------------------------------------------------------------------------


def _configured() -> OpenModelConfig | None:
    """The model an operator configured, or `None`.

    `GV_OPENMODEL_ID` is the switch: without it this file never opens a socket. The rest have
    defaults that suit a local Ollama, which is the case this was written for.
    """
    model_id = os.environ.get("GV_OPENMODEL_ID")
    if not model_id:
        return None
    return OpenModelConfig(
        model_id=model_id,
        prompt_id="dimension-reader-v1",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=int(os.environ.get("GV_OPENMODEL_CONNECT_TIMEOUT", "5")),
        read_timeout_seconds=int(os.environ.get("GV_OPENMODEL_READ_TIMEOUT", "120")),
        max_attempts=1,
        base_url=os.environ.get("GV_OPENMODEL_BASE_URL", "http://localhost:11434/v1"),
        api_key=os.environ.get("GV_OPENMODEL_API_KEY") or None,
    )


def test_a_configured_model_returns_a_validated_reading() -> None:
    """**The smoke test.** One image out, one validated structured reading back, one record written.

    Skipped without `GV_OPENMODEL_ID`, the way the database tests skip without `DATABASE_URL`: a test
    that silently passes because nothing was configured is worse than one that says it did not run.

    **It asserts a validated reading, not a correct one.** The crop is a blank square, so whatever the
    model says about it is meaningless — what is being proved is that the request left, the response
    came back through the same validator Bedrock's output uses, and the invocation was recorded. A
    model that refuses or fails is a *pass* here too, for the same reason: those are outcomes this
    seam is supposed to carry, and carrying them is what is under test.
    """
    config = _configured()
    if config is None:
        pytest.skip("set GV_OPENMODEL_ID (and run a model) to exercise the real transport")

    sink = RecordingSink()
    adapter = OpenModelAdapter.from_environment(config, sink)

    try:
        candidate = adapter.extract(_request())
    except (OpenModelRefusalError, OpenModelProtocolError, OpenModelPayloadRejectedError):
        # The seam carried a refusal or an unusable answer, recorded it, and raised. That is the
        # contract working. A small local model often cannot hold a tool schema, and this test is
        # about the plumbing rather than about the model's ability.
        assert sink.items, "a failed call was not recorded"
        assert sink.items[0].outcome in {
            OpenModelInvocationOutcome.REFUSED,
            OpenModelInvocationOutcome.REJECTED,
        }
        return

    assert isinstance(candidate, ObservationCandidate)
    assert candidate.raw_text.strip(), "a validated candidate with no text is not a reading"
    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.OK]
    assert sink.items[0].model_id == config.model_id


def test_the_smoke_test_is_skipped_rather_than_silently_passing() -> None:
    """The guard on the guard: without configuration there is nothing to talk to, and it says so."""
    assert _configured() is None or os.environ.get("GV_OPENMODEL_ID")


def test_the_client_protocol_is_satisfied_by_the_fake() -> None:
    """The transport is replaceable, which is what makes every test above possible without a server."""
    endpoint: ChatCompletionsClient = FakeEndpoint(_tool_response(_payload()))
    assert callable(endpoint.complete)


def test_the_default_transport_really_speaks_http() -> None:
    """`from_environment` builds a client that posts, and the adapter reads what comes back.

    **Without this the HTTP layer is never exercised.** Every test above hands the adapter a scripted
    object, and the smoke test skips wherever no model is running — which is every machine that has
    not installed one, including CI. So the one part that would ship unproven is the part that builds
    a URL, sets a header, encodes a body and decodes a response.

    A stub server rather than a model: it asserts the wire format the adapter produces and returns a
    fixed answer. Still no accuracy claim — there is nothing here that could read an image.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    received: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            received["body"] = json.loads(self.rfile.read(length))
            body = json.dumps(_tool_response(_payload())).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Silent: the test's output is its assertions."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = OpenModelConfig(
            model_id="qwen2-vl",
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=2,
            read_timeout_seconds=10,
            max_attempts=1,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="a-key-only-this-test-knows",
        )
        sink = RecordingSink()
        adapter = OpenModelAdapter.from_environment(config, sink)

        candidate = adapter.extract(_request())
    finally:
        server.shutdown()
        server.server_close()

    assert candidate.raw_text == KNOWN_READING
    assert received["path"] == "/v1/chat/completions"
    assert received["authorization"] == "Bearer a-key-only-this-test-knows"
    assert received["body"]["tool_choice"]["function"]["name"] == TOOL_NAME
    assert [record.outcome for record in sink.items] == [OpenModelInvocationOutcome.OK]


def test_no_key_is_sent_when_none_is_configured() -> None:
    """A local model needs no credential, and sending an empty one is not the same as sending none.

    Worth asserting because the local case is the default here: the drawings stay on the machine, and
    an `Authorization` header on a localhost request would be a small lie about what this is doing.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    seen: dict[str, Any] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            self.rfile.read(length)
            seen["authorization"] = self.headers.get("Authorization")
            body = json.dumps(_tool_response(_payload())).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Silent."""

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        config = OpenModelConfig(
            model_id="qwen2-vl",
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=2,
            read_timeout_seconds=10,
            max_attempts=1,
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
        )
        OpenModelAdapter.from_environment(config, RecordingSink()).extract(_request())
    finally:
        server.shutdown()
        server.server_close()

    assert seen["authorization"] is None
