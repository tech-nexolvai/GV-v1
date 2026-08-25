"""One trace from package to finding — the shared span helper, so there is only ever one convention
(#259, F2.1).

`docs/DESIGN_CONTROLS.md` §3.1: *"`package → workflow → task → model call → finding`, carrying
`package_id`, `document_version_id`, `workflow_run_id`, `task_run_id`, page/region, extractor version and
rule snapshot."* `AGENTS.md` §6 adds the constraint that matters most here: *"Never log full drawings or
sensitive crops into traces — store references/hashes."*

**Why this module exists rather than a span at each call site.** A trace is only useful if every span
carries the same attribute names. Two spellings of `package_id` is a trace that cannot be joined, and the
second spelling is always an accident rather than a decision. So the attribute set is declared once, in
`SPAN_ATTRS`, and `traced()` refuses a name that is not in it. A typo fails loudly at the call site instead
of quietly producing an attribute nobody queries.

**The drawing-content guard is a refusal, not a filter.** §6 forbids drawing bytes in a trace, and a helper
that silently dropped an offending value would let a caller believe it had recorded something. `traced()`
raises, because the caller has passed the wrong thing and needs to pass a reference or a hash instead.

**No exporter is configured here, deliberately.** Where traces are shipped — an OTLP collector, a hosted
backend, nothing at all — is an operations decision with cost and data-residency consequences, and it is not
this module's to make. Without an exporter, spans still carry real ids, so `trace_id` correlation across
logs works today and choosing a backend later changes configuration rather than code.

**Crossing the workflow boundary.** A Hatchet task runs in another process, minutes later, and carries
none of this automatically. `carrier()` and `incoming_context()` are the two halves: the outbox captures
the context in the caller's transaction — so it is the *request's* context, not the dispatcher's — and the
worker resumes from it. See `workflow/outbox.py`; the column exists because telemetry is not a start
argument and does not belong in a workflow's input contract.

Source: `AGENTS.md` §6 · Design: `docs/DESIGN_CONTROLS.md` §3.1 ·
Verification: `tests/telemetry/test_tracing.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Final

from opentelemetry import propagate, trace
from opentelemetry.context import Context
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace import Span, format_trace_id

__all__ = [
    "INSTRUMENTATION_NAME",
    "MAX_ATTR_LENGTH",
    "SPAN_ATTRS",
    "TRACE_CONTEXT_FIELDS",
    "TRACE_ID_HEADER",
    "TRACE_ID_STATE",
    "DrawingContentInTrace",
    "UnknownSpanAttribute",
    "carrier",
    "configure_tracing",
    "current_trace_id",
    "incoming_context",
    "traced",
]

#: The attribute names a span may carry, declared once — `docs/DESIGN_CONTROLS.md` §3.1.
#:
#: Closed rather than advisory: `traced()` rejects anything else. The point of a fixed set is that a trace
#: can be joined on these names, and one call site spelling it `packageId` breaks that silently.
SPAN_ATTRS: Final = (
    "package_id",
    "document_version_id",
    "workflow_run_id",
    "task_run_id",
    "page_index",
    "extractor_version",
    "rule_snapshot_id",
    # Not in the design's list, and added rather than smuggled past the guard. §3.1 names the attributes
    # that connect the *pipeline*; the request id is what connects a trace to a person's complaint, and the
    # request span has to carry it or "quote your request id" and "find the trace" are separate searches.
    #
    # The alternative was what the first version did: set it with `span.set_attribute` outside `traced()`.
    # That made the very first call site the one that stepped around the vocabulary this module exists to
    # enforce — and because the value is caller-supplied, it also meant an inbound header could put content
    # on a span that `_checked` would have refused. Declaring it is the honest fix.
    "request_id",
    # The two ends of the chain the design asks for. §3.1 names the attributes that identify *where* in
    # the pipeline a span sits; these name *what it produced*, and without them a finding and the model
    # call behind it are two spans in one trace with nothing joining them to the rows they wrote.
    #
    # Both are row ids, not content: `model_invocations.id` and `findings.id`. What the model was asked
    # and what it answered stay in the database, where they are already recorded and access-controlled —
    # putting a prompt or a crop on a span is exactly what §6 forbids.
    "model_invocation_id",
    "finding_id",
    # The evidence region a reading came from, as a reference. Not the crop: `evidence/gate.py` writes
    # the polygon in normalised stored space and the crop is a stored artifact, so a span carries the
    # artifact's key or hash and a reader follows it. §3.1 asks for "page/region"; `page_index` is the
    # page and this is the region.
    "evidence_ref",
)

#: The only carrier fields this project propagates — W3C trace context, and nothing else.
#:
#: `propagate.inject` will also emit `baggage`, which is arbitrary caller-set data. `carrier()` is
#: persisted in `outbox_entries` and handed to the workflow engine as metadata, so anything in baggage
#: would be written to a database and shipped onwards — the exact shape of leak `AGENTS.md` §6 forbids.
#: These two are ids and flags: a `traceparent` is a fixed-length hex string and a `tracestate` is
#: vendor routing data, and neither can carry a crop.
TRACE_CONTEXT_FIELDS: Final = ("traceparent", "tracestate")

#: The response header carrying the trace id, so a caller can quote it when reporting a problem.
#:
#: Same reasoning as `X-Request-ID` in `app/errors.py`: an id nobody outside the process can see is an id
#: that cannot appear in a bug report. 32 lowercase hex, the W3C form.
TRACE_ID_HEADER: Final = "X-Trace-Id"

#: Where the request's trace id is parked on `request.state`.
#:
#: Kept on the request rather than read from the active span when the response is built, because an
#: unhandled exception is handled *outside* the middleware that opened the span — by then the span has
#: ended and `current_trace_id()` is `None`. `app/errors.py` already learned this for the request id; the
#: first version of the trace header repeated the same mistake, so the error case — the one a person is
#: actually reporting — came back with no trace id at all.
TRACE_ID_STATE: Final = "trace_id"

#: The instrumentation name recorded on every span this project creates.
INSTRUMENTATION_NAME: Final = "gv"

#: The longest attribute value `traced()` will accept.
#:
#: Every legitimate value in `SPAN_ATTRS` is an id, a version or an index: a UUID is 36 characters, a bare
#: sha256 is 64, and the `sha256:`-prefixed form is 71. 256 is well clear of all of them while still being
#: far too small for a drawing, a crop or a base64 payload — so a value that trips this is a caller passing
#: content where a reference belongs, which is exactly what §6 forbids. A documented maximum, not a silent
#: truncation: exceeding it raises.
MAX_ATTR_LENGTH: Final = 256

#: Markers that mean a value carries file content rather than a reference to it.
#:
#: Not a complete test for "is this a drawing" — no such test exists — but these catch the realistic
#: mistakes: pasting a data URI, a base64 blob, or the first bytes of a PDF or PNG into an attribute.
_CONTENT_MARKERS: Final = ("%PDF", "\x89PNG", "data:", ";base64", "JVBERi0")

#: A run of characters long enough to be an encoded payload rather than an identifier.
_BASE64_RUN: Final = re.compile(r"[A-Za-z0-9+/]{120,}={0,2}")

_configured = False


class UnknownSpanAttribute(ValueError):
    """A span was given an attribute name outside `SPAN_ATTRS`."""


class DrawingContentInTrace(ValueError):
    """A span attribute carried file content rather than a reference to it — `AGENTS.md` §6."""


def configure_tracing() -> None:
    """Install a real tracer provider, once, if nothing has installed one already.

    Without this, OpenTelemetry's default provider is a no-op whose spans have an all-zero, invalid trace
    id — so `current_trace_id()` would return `None` everywhere and the correlation this module exists for
    would silently not happen. Idempotent, because it is called from both the request path and `traced()`,
    and because replacing a provider mid-process would split one trace in two.

    An existing provider is left alone: a deployment that has already configured exporters knows more about
    where traces should go than this function does.
    """
    global _configured
    if _configured:
        return
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        trace.set_tracer_provider(TracerProvider())
    _configured = True


def current_trace_id() -> str | None:
    """The active trace id as 32 lowercase hex characters, or `None` if there is no valid trace.

    `None` rather than a zero id: "00000000000000000000000000000000" in a log line looks like a trace you
    could go and look up, and there is nothing to look up. Absent is honest.
    """
    context = trace.get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return format_trace_id(context.trace_id)


def carrier() -> dict[str, str]:
    """The active trace context as a W3C carrier, for handing to something that runs later.

    The mirror of `incoming_context`. A `traceparent` is a short ASCII string, so it travels anywhere a
    string does — an HTTP header, a queue message, a JSONB column — and it is what makes a workflow
    started ten seconds from now part of the trace of the request that asked for it.

    Empty when there is no active span. That is a real answer, not a failure: work enqueued by a cron job
    or a test has no request behind it, and inventing a trace id would connect it to nothing.

    **Captured where the work is decided, not where it is dispatched.** `workflow/outbox.py` calls this
    inside the caller's transaction, so the context stored is the request's. Capturing it in the
    dispatcher instead would produce a trace that begins at a background poll — technically a trace, and
    useless for the question this exists to answer, which is *what asked for this?*

    **Trace context only, never baggage.** `propagate.inject` also emits a `baggage` header, which is
    arbitrary caller-set key/value data — and this carrier is *persisted*, in `outbox_entries`. Anything
    a call site had put in baggage would be written to a database column and shipped to a workflow
    engine's metadata, which is precisely the §6 route this project closes everywhere else. Filtered to
    the W3C trace-context fields, which are ids and flags and cannot carry content.
    """
    into: dict[str, str] = {}
    propagate.inject(into)
    return {key: value for key, value in into.items() if key in TRACE_CONTEXT_FIELDS}


def incoming_context(headers: Mapping[str, str]) -> Context | None:
    """The trace context a caller sent, or `None` if it sent none.

    Standard W3C `traceparent` extraction, so a request that is already part of a trace continues it rather
    than starting a second one — the same reasoning as accepting an inbound `X-Request-ID` instead of always
    generating one. Returning `None` for "nothing to continue" keeps the decision at the call site.
    """
    extracted = propagate.extract(dict(headers))
    span_context = trace.get_current_span(extracted).get_span_context()
    return extracted if span_context.is_valid else None


def _checked(name: str, value: object) -> str | int:
    """One attribute, or a refusal naming which rule it broke."""
    if name not in SPAN_ATTRS:
        raise UnknownSpanAttribute(
            f"{name!r} is not a span attribute this project records. The set is fixed so a trace can be "
            f"joined on it: {', '.join(SPAN_ATTRS)}. Add it to SPAN_ATTRS if it belongs there."
        )

    # `bytes` first and unconditionally: a bytes attribute is file content by construction, and there is no
    # length at which that becomes acceptable.
    if isinstance(value, bytes):
        raise DrawingContentInTrace(
            f"{name} was given {len(value)} bytes. A trace records references and hashes, never file "
            "content — AGENTS.md §6. Pass an id or a sha256 instead."
        )

    if isinstance(value, int) and not isinstance(value, bool):
        return value

    text = str(value)
    if len(text) > MAX_ATTR_LENGTH:
        raise DrawingContentInTrace(
            f"{name} is {len(text)} characters, over the {MAX_ATTR_LENGTH} allowed. Every attribute in "
            "SPAN_ATTRS is an id, a version or an index; a value this long is content, not a reference."
        )
    for marker in _CONTENT_MARKERS:
        if marker in text:
            raise DrawingContentInTrace(
                f"{name} contains {marker!r}, which means it carries file content rather than a reference "
                "to it — AGENTS.md §6 forbids drawings and crops in a trace."
            )
    if _BASE64_RUN.search(text):
        raise DrawingContentInTrace(
            f"{name} contains a long run of base64 characters, so it is an encoded payload rather than an "
            "identifier. A trace records references and hashes only — AGENTS.md §6."
        )
    return text


@contextmanager
def traced(name: str, *, parent: Context | None = None, **attrs: object) -> Iterator[Span]:
    """Run a block inside a span carrying `attrs`, refusing anything the trace must not hold.

    Every attribute name must be in `SPAN_ATTRS`, and every value must be a reference rather than content.
    Both refusals raise before the span starts: a span that exists with the wrong attributes is worse than
    no span, because it looks like evidence.

    `parent` continues a trace a caller already started — pass what `incoming_context()` returned. It is
    keyword-only and takes a `Context` rather than a header mapping so that the extraction stays in one
    place; `None` starts a new trace.

    **This is the only way to open a span in this project.** The first version of the request middleware
    called the tracer directly and set an attribute with `span.set_attribute`, which meant the one span
    every request creates was also the one span that never met these checks. `parent` exists so that no
    call site has a reason to reach past this function again.
    """
    configure_tracing()
    checked = {key: _checked(key, value) for key, value in attrs.items()}
    tracer = trace.get_tracer(INSTRUMENTATION_NAME)
    with tracer.start_as_current_span(name, context=parent, attributes=checked) as span:
        yield span
