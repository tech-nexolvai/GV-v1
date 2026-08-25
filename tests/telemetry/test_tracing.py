"""The shared span helper: one attribute vocabulary, and no drawing content in a trace (#259, F2.1).

The guard tests matter more than the happy path here. A span with a misspelled attribute is invisible —
nothing fails, the trace simply cannot be joined on it — and a span carrying a crop is a `AGENTS.md` §6
breach that no runtime check would otherwise catch.

Source: `AGENTS.md` §6 · Design: `docs/DESIGN_CONTROLS.md` §3.1 · Verification: this file
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

import pytest

from app.telemetry.tracing import (
    MAX_ATTR_LENGTH,
    SPAN_ATTRS,
    DrawingContentInTrace,
    UnknownSpanAttribute,
    carrier,
    configure_tracing,
    current_trace_id,
    incoming_context,
    traced,
)

TRACE_ID = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# There is a trace at all
# ---------------------------------------------------------------------------


def test_a_span_produces_a_real_trace_id() -> None:
    """Without a configured provider OpenTelemetry's default is a no-op whose trace id is all zeros.

    That failure is quiet in the worst way: `current_trace_id()` returns something, logs carry it, and
    every event in the system shares one meaningless id. So this asserts the shape *and* that it is not the
    invalid all-zero value.
    """
    with traced("unit-test"):
        trace_id = current_trace_id()

    assert trace_id is not None
    assert TRACE_ID.fullmatch(trace_id), f"not a W3C trace id: {trace_id!r}"
    assert set(trace_id) != {"0"}, "an all-zero trace id means no provider was installed"


def test_outside_a_span_there_is_no_trace_id_rather_than_a_zero_one() -> None:
    """`None`, not `"000…0"`.

    A zero id in a log line looks like something an operator could paste into a backend and find. Nothing
    is there. Absent says what is true.
    """
    configure_tracing()
    assert current_trace_id() is None


def test_one_trace_id_is_shared_by_nested_spans() -> None:
    """The whole point of the story: package → workflow → task → finding is *one* trace.

    Nested spans get their own span ids and share the trace id. If this ever fails, the pipeline produces a
    separate trace per stage and "trace a finding back to the model call" becomes impossible.
    """
    with traced("outer"):
        outer = current_trace_id()
        with traced("inner", package_id="p-1"):
            inner = current_trace_id()

    assert outer is not None
    assert inner == outer, "a nested span started a new trace, so the story is no longer connected"


def test_a_caller_s_trace_is_continued_rather_than_restarted() -> None:
    """An inbound `traceparent` is honoured, so a distributed call is one trace and not two.

    The same reasoning as accepting an inbound `X-Request-ID`: an id that changes at our boundary means the
    caller and we are each holding half a story.
    """
    upstream = "4bf92f3577b34da6a3ce929d0e0e4736"
    headers = {"traceparent": f"00-{upstream}-00f067aa0ba902b7-01"}

    context = incoming_context(headers)
    assert context is not None, "a valid traceparent was not extracted"

    from opentelemetry import trace as otel

    with otel.get_tracer("test").start_as_current_span("child", context=context):
        assert current_trace_id() == upstream


def test_headers_without_a_traceparent_continue_nothing() -> None:
    """`None` rather than an empty context, so the caller decides what "no parent" means."""
    assert incoming_context({}) is None
    assert incoming_context({"x-request-id": "abc"}) is None


# ---------------------------------------------------------------------------
# One attribute vocabulary
# ---------------------------------------------------------------------------


def test_every_declared_attribute_is_accepted() -> None:
    """The declared set is usable — a vocabulary that rejects its own names would be worse than none."""
    values: dict[str, object] = {name: "x" for name in SPAN_ATTRS}
    values["page_index"] = 3
    with traced("all-attributes", **values):
        pass


def test_an_undeclared_attribute_is_refused() -> None:
    """A typo fails at the call site instead of producing a column nobody queries.

    This is the failure the fixed set exists to prevent: `packageId` alongside `package_id` does not break
    anything visibly, it just means half the spans cannot be joined.
    """
    with (
        pytest.raises(UnknownSpanAttribute, match="packageId"),
        traced("typo", packageId="p-1"),
    ):  # type: ignore[arg-type]
        pass


def test_the_refusal_lists_the_names_that_are_allowed() -> None:
    """An error that says only "no" makes the caller go and read the source."""
    with pytest.raises(UnknownSpanAttribute) as raised, traced("typo", nonsense="x"):
        pass

    message = str(raised.value)
    for name in SPAN_ATTRS:
        assert name in message, f"{name} is allowed but the refusal does not mention it"


# ---------------------------------------------------------------------------
# Never the drawings themselves — AGENTS.md §6
# ---------------------------------------------------------------------------


def test_bytes_are_refused_whatever_their_length() -> None:
    """A bytes attribute is file content by construction; there is no size at which that is acceptable."""
    with (
        pytest.raises(DrawingContentInTrace, match="never file content"),
        traced("leak", document_version_id=b"\x89PNG\r\n\x1a\n"),
    ):
        pass


@pytest.mark.parametrize(
    "value",
    [
        "%PDF-1.7 ...",
        "\x89PNG\r\n\x1a\n",
        "data:image/png;base64,iVBORw0KGgo=",
        "JVBERi0xLjcK",
        "x" * (MAX_ATTR_LENGTH + 1),
        base64.b64encode(b"a drawing" * 40).decode(),
        # **The case that reaches the base64 guard, which nothing did before.** Review pointed out that
        # every value above is caught earlier — by a content marker or by the length check — so deleting
        # `_BASE64_RUN` and its branch failed nothing. I confirmed it: 18 tests passed with the guard
        # removed. It is a guard with no alarm on it, in the file whose docstring says the guard tests
        # matter most.
        #
        # 90 bytes encode to 120 base64 characters: long enough to trip the run, short enough to pass the
        # length check, and with no marker in it. That is the leak the regex actually exists for.
        base64.b64encode(b"x" * 90).decode(),
    ],
)
def test_content_shaped_values_are_refused(value: str) -> None:
    """The realistic mistakes: a data URI, a base64 blob, a magic number, or something simply too long.

    Not a complete test for "is this a drawing" — no such test exists — but each of these is a caller
    passing content where §6 requires a reference or a hash.
    """
    with pytest.raises(DrawingContentInTrace), traced("leak", extractor_version=value):
        pass


def test_the_guard_refuses_rather_than_dropping_the_value() -> None:
    """Raising, not filtering.

    A helper that silently discarded an offending value would leave the caller believing it had recorded
    something, and the span would look like evidence of a check that never happened. The exception is the
    only outcome that makes the caller fix the call.
    """
    with (
        pytest.raises(DrawingContentInTrace),
        traced("leak", package_id="data:application/pdf;base64,JVBERi0="),
    ):
        pass


def test_real_identifiers_are_not_mistaken_for_content() -> None:
    """The guard has to let through everything legitimate, or it will be removed rather than fixed.

    A UUID, a bare sha256 and the `sha256:`-prefixed form are the three shapes this project actually
    records. All are far inside the limit, and a hex digest has no long base64 run — but "hex is a subset
    of base64" is exactly the kind of thing that makes a guard reject valid input, so it is asserted rather
    than assumed.
    """
    digest = "a" * 64
    for value in (
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        digest,
        f"sha256:{digest}",
        "extractor-1.4.2",
    ):
        with traced("legitimate", rule_snapshot_id=value):
            pass


def test_an_integer_index_survives_as_an_integer() -> None:
    """`page_index` is a number, and stringifying it would make range queries impossible in a backend."""
    with traced("page", page_index=7) as span:
        recorded = getattr(span, "attributes", {}) or {}
    assert recorded.get("page_index") == 7


def test_a_value_at_exactly_the_length_limit_is_allowed() -> None:
    """The other side of the bound: `MAX_ATTR_LENGTH` characters is permitted, one more is not.

    Only the refusing side was asserted, and `_checked` uses `>` — so a change to `>=` would start
    rejecting a legitimate value at exactly the limit with nothing failing. The same boundary-both-sides
    point the export cap was corrected for.

    The value deliberately contains separators. A 256-character run of `x` would be refused for a different
    reason — it is an unbroken base64-shaped run — and a test that passed for the wrong reason would tell me
    nothing about the length check.
    """
    at_limit = ("abcdefghij-" * 24)[:MAX_ATTR_LENGTH]
    assert len(at_limit) == MAX_ATTR_LENGTH

    with traced("boundary", rule_snapshot_id=at_limit):
        pass

    with pytest.raises(DrawingContentInTrace), traced("boundary", rule_snapshot_id=at_limit + "x"):
        pass


def test_all_lists_the_module_s_public_names() -> None:
    """`__all__` has to describe the surface, in the module whose argument is that names are declared once.

    It omitted `INSTRUMENTATION_NAME` and `MAX_ATTR_LENGTH` while `app/main.py` and this file were already
    importing them. Nothing broke — which is why this needs a test rather than a careful reader.

    Read from the source rather than from `vars()`: the module's own namespace also holds everything it
    imports, and `re` or `Final` appearing in `__all__` would be wrong. What is *assigned or defined at
    module level here* is the honest definition of "this module's surface".
    """
    import ast

    from app.telemetry import tracing

    tree = ast.parse(Path(tracing.__file__).read_text())
    defined: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            defined.add(node.target.id)
        elif isinstance(node, ast.Assign):
            defined.update(t.id for t in node.targets if isinstance(t, ast.Name))

    public = {name for name in defined if not name.startswith("_")} - {"__all__"}
    declared = set(tracing.__all__)

    assert (
        public - declared == set()
    ), f"defined here but missing from __all__: {sorted(public - declared)}"
    assert (
        declared - public == set()
    ), f"__all__ names things not defined here: {sorted(declared - public)}"


# ---------------------------------------------------------------------------
# The chain from a finding back to the model call and the page region
# ---------------------------------------------------------------------------


def test_a_finding_traces_back_to_the_model_call_and_the_page_region() -> None:
    """The acceptance criterion, asserted as one trace rather than as three spans.

    A finding, the model call that read the dimension, and the page region it was read from are
    three spans in different processes at different times. What makes them one story is that they
    share a trace id and each carries the id of the row it wrote — so "why does this finding say
    that?" is a query, not an investigation.
    """
    configure_tracing()
    model_call = "8f14e45f-ea8f-4b0a-9c1a-000000000001"
    finding = "8f14e45f-ea8f-4b0a-9c1a-000000000002"
    region = '{"document_version_id":"11111111-1111-1111-1111-111111111111","page":3}'

    with traced("package.review", package_id="pkg-1") as package_span:
        trace_id = package_span.get_span_context().trace_id
        with traced(
            "model.read",
            model_invocation_id=model_call,
            page_index=3,
            evidence_ref=region,
            extractor_version="nova2-lite-2026-08",
        ) as model_span:
            assert model_span.get_span_context().trace_id == trace_id
        with traced(
            "verdict.check",
            finding_id=finding,
            rule_snapshot_id="sha256:" + "a" * 64,
        ) as finding_span:
            assert finding_span.get_span_context().trace_id == trace_id


def test_the_rule_snapshot_id_is_a_declared_attribute() -> None:
    """So a behaviour change is attributable to a rule change rather than to "something changed".

    Two runs of the same package disagreeing is either a bug or a new rule version, and the snapshot
    id on the span is what tells them apart without anybody guessing.
    """
    assert "rule_snapshot_id" in SPAN_ATTRS

    configure_tracing()
    with traced("verdict.check", rule_snapshot_id="sha256:" + "b" * 64):
        pass


def test_a_crop_cannot_be_put_on_a_span_as_a_region() -> None:
    """`evidence_ref` is where somebody would reach for when they mean "the region", and the region
    is a reference — the crop itself is a stored artifact. §6 forbids the bytes."""
    configure_tracing()

    with (
        pytest.raises(DrawingContentInTrace),
        traced("model.read", evidence_ref=b"\x89PNG\r\n\x1a\n" + b"0" * 40),
    ):
        pass

    with (
        pytest.raises(DrawingContentInTrace),
        traced("model.read", evidence_ref="data:image/png;base64,iVBORw0KGgo="),
    ):
        pass


# ---------------------------------------------------------------------------
# Handing the context to something that runs later
# ---------------------------------------------------------------------------


def test_a_carrier_round_trips_the_active_trace() -> None:
    """The two halves of the workflow boundary, checked against each other.

    A carrier that could be produced but not consumed would look like propagation and connect
    nothing, and nothing downstream would report an error.
    """
    configure_tracing()

    with traced("request") as span:
        expected = span.get_span_context().trace_id
        handed_on = carrier()

    assert "traceparent" in handed_on

    resumed = incoming_context(handed_on)
    assert resumed is not None
    with traced("workflow.task", parent=resumed) as later:
        assert later.get_span_context().trace_id == expected


def test_a_carrier_taken_outside_a_span_is_empty() -> None:
    """A real answer, not a failure. Work enqueued by a cron job has no request behind it, and a
    fabricated trace id would connect it to nothing while looking like it connected it to
    something."""
    assert carrier() == {}
