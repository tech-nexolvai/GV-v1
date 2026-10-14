"""The depth and sink reader: two AI judgments and code's, per value (#1265, design §4, §9).

**What it reads.** Per vendor view: the countertop's depth (CT010) on a plan or a section, and on a
plan the sink cut-out's width and front-to-back size (CT012, CT008) and the strip in front of it
(CT007). Nothing else: settings (overhang, backsplash, cabinet depth, the sink's inside size) are
never read off the vendor's drawing.

**Behind `GV_DEPTH_READER`, off by default, and wired into nothing.** No check reads what this
module returns (that is part 4). `scripts/depth_read.py` runs it on stored pages to measure it.

**How each value is decided.** A value is usable only when **both** judgments agree; anything else
holds with a plain reason, and a hold never moves toward PASS.

1. **The view's kind.** Judgment 1: both readers name the kind from a picture of the view with
   every heading on the panel painted out (`view-kind-v1`). Judgment 2: code, from the heading's
   words and, for a plan or a front view, the wall band (`view_kind.code_view_kind`). A reader
   missing or unsure, the readers disagreeing, code unable to say, or code saying another kind:
   nothing on that view is read, and every value it could hold says why.
2. **Which dimension plays which role.** Both readers pick the same numbered dimension for a role
   (`depth-span-role-v1`), and for the cut-out the same lettered outline, and for the strip the same
   start (the cabinet face). A reader missing, unsure, saying none, or the two picking differently:
   held.
3. **The number.** Both readers read the agreed dimension's label, and it seals only through the
   width reader's own two-reader agreement (`slot_reader.seal.seal_label`, the Claude pair): one
   reader missing, unsure, a stacked fraction, words, an odd fraction, millimetres that do not
   corroborate the inches, or the reviewer's ink on the close-up each hold. **Inches only**: a
   label with only millimetres (or no unit) holds; millimetres in brackets only corroborate.
4. **Code's judgment of the value** (`spans.py`, #1256):
   * the dimension's place: a plan's depth runs top to bottom from the wall band to the stone's
     front edge; a section's runs left to right; a cut-out's two dimensions lie on the edges of the
     outline both readers chose, and code allows that outline as the hole only when printed words
     name the cut-out or one outline is drawn (`cutout_judgment.code_allows`); the strip in front of
     the hole runs along the depth, ends on the hole's front edge, and stops short of the stone's
     front edge (`front_offset_start.code_allows`), so **a strip measured from the stone's edge is
     never CT007**;
   * the number: the vector text at the dimension equals the sealed reading exactly, or (where the
     number is pen strokes) its drawn length agrees within 1/10 at the view's own scale, taken from
     a different dimension (the view's horizontal overall). With neither, it holds: no code check
     confirmed the number. Any check that disagrees holds, as does a chain whose parts do not sum to
     its overall;
   * a depth printed on two views of one panel that disagree holds on both.

**Calls.** Every question goes through the shared route (`slot_reader.bedrock.read_crops_parallel`
via `CropJob.depth`, under the three pinned prompt ids), so it is paced, capped by the spend guard,
re-asked once when malformed, and every attempt is handed to the caller's recorder (the shared
`CallLedger` in the worker; a local ledger in the measuring script). A stored answer to the
identical question is replayed instead of paid for again (`replay_stored_answer`).

Source: issue #1265 · Verification: `tests/extraction/depth/test_read.py`
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from fractions import Fraction
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal
from uuid import UUID

from pydantic import ValidationError

from evidence.crop import RenderedPage
from extraction.depth.output import (
    DepthReply,
    SpanRoleAnswer,
    ViewKindAnswer,
    depth_number_answer,
    span_role_answer,
    view_kind_answer,
)
from extraction.depth.pictures import (
    MarkedSpan,
    end_marks_picture,
    lettered_outlines_picture,
    numbered_spans_picture,
    view_picture,
)
from extraction.depth.prompts import (
    DEPTH_SPAN_PROMPT_ID,
    DEPTH_SPAN_SCHEMA,
    SPAN_ROLE_PROMPT_ID,
    VIEW_KIND_PROMPT,
    VIEW_KIND_PROMPT_ID,
    VIEW_KIND_SCHEMA,
    DepthQuestion,
    SpanDirection,
    depth_span_prompt,
    span_role_prompt,
    span_role_schema,
)
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
    MM_ONLY,
    CutoutJudgment,
    EndKind,
    Outline,
    SinkOutlines,
    Span,
    SpanSettings,
    TextLane,
    ViewScale,
    ViewSpans,
    closure_check,
    cutout_judgment,
    drawn_length_check,
    find_spans,
    front_offset_start,
    overall_horizontal,
    read_depth_label,
    sink_outlines,
    span_on_outline,
    text_lanes,
    view_scale,
)
from extraction.depth.view_kind import (
    MEASURED_VIEW_SETTINGS,
    CodeViewKind,
    VendorView,
    ViewFinderSettings,
    ViewKind,
    code_view_kind,
    kind_from_heading,
)
from extraction.form_reader.bedrock import (
    AttemptUsage,
    ConverseClient,
    MalformedFormAnswer,
    _response_text,
    _response_usage,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, PageInk
from extraction.ink import InkAt
from extraction.slot_reader.claude_output import (
    DEFAULT_CLAUDE_EFFORT,
    OUTPUT_CONFIG_KEY,
    ClaudeEffort,
    claude_answer,
    is_claude_model,
    output_config,
)
from extraction.slot_reader.runs import Lane, PlannedLabel
from extraction.slot_reader.seal import LabelState, ReaderAnswer, normalise_text, seal_label
from extraction.vector_first import upright_png
from units.measurement import Unit
from vocabulary.semantic_types import SemanticType

if TYPE_CHECKING:
    from extraction.architect.text import TextChar

__all__ = [
    "CLAUDE_PAIR",
    "DEPTH_READER_ENV",
    "MEASURED_DEPTH_READER_SETTINGS",
    "Asked",
    "Asker",
    "DepthReaderSettings",
    "DepthReading",
    "NoAnswer",
    "PreparedPage",
    "PreparedView",
    "ReaderSays",
    "ValueReading",
    "ViewReading",
    "ask_depth_question",
    "build_depth_request",
    "decide_view_kind",
    "depth_reader_enabled",
    "prepare_pdf_page",
    "prepare_view",
    "question_packet",
    "read_depth",
]

#: The switch. Off unless set to a yes word; anything unrecognised is refused, never read as on.
DEPTH_READER_ENV: Final = "GV_DEPTH_READER"
#: The decided Claude pair (phase 3), the same two readers as the width reader's.
CLAUDE_PAIR: Final = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")

CT010: Final = SemanticType.CT010.value
CT012: Final = SemanticType.CT012.value
CT008: Final = SemanticType.CT008.value
CT007: Final = SemanticType.CT007.value

_TWO: Final = Decimal(2)


def depth_reader_enabled(environ: Mapping[str, str]) -> bool:
    """Whether `GV_DEPTH_READER` switches the reader on. Unset or empty is off (the default)."""
    raw = environ.get(DEPTH_READER_ENV, "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ValueError(f"{DEPTH_READER_ENV} must be true or false, not {raw!r}")


@dataclass(frozen=True, slots=True)
class DepthReaderSettings:
    """Every size the reader uses. No defaults: `MEASURED_DEPTH_READER_SETTINGS` is the one set."""

    dpi: int
    """The page render every picture is cut from (the width reader's label pictures: 300)."""
    view_max_side: int
    """A whole-view picture is shrunk by a whole factor to at most this many pixels a side: every
    such picture then fits Claude's high-resolution limits unresized (65 x 65 tiles at most)."""
    closeup_max_side: int
    view_pad_pt: Decimal
    """Room round a view's extent, so its outermost dimensions show whole."""
    closeup_along_pt: Decimal
    closeup_across_pt: Decimal
    """A close-up runs past each end of its dimension by `along`, and `across` either side of it."""
    badge_px: int
    thickness_px: int
    """Sizes of the numbers and lines drawn, in the final (shrunk) picture's pixels."""
    end_mark_reach_px: int
    max_tokens: int
    effort: ClaudeEffort
    spans: SpanSettings
    views: ViewFinderSettings


MEASURED_DEPTH_READER_SETTINGS: Final = DepthReaderSettings(
    dpi=300,
    view_max_side=1800,
    closeup_max_side=1600,
    view_pad_pt=Decimal(6),
    closeup_along_pt=Decimal(12),
    closeup_across_pt=Decimal(24),
    badge_px=22,
    thickness_px=3,
    end_mark_reach_px=30,
    # Claude thinks before it answers: the width reader found 512 cut replies off (3000 there).
    max_tokens=3000,
    effort=DEFAULT_CLAUDE_EFFORT,
    spans=MEASURED_SPAN_SETTINGS,
    views=MEASURED_VIEW_SETTINGS,
)


# ---------------------------------------------------------------------------
# A page and its views, prepared by code at no cost
# ---------------------------------------------------------------------------

type PixelBox = tuple[int, int, int, int]
type PixelsOf = Callable[[Box], PixelBox]


@dataclass(frozen=True, slots=True)
class PreparedView:
    """One vendor view with everything code finds in it (judgment 2's pieces)."""

    page_index: int
    panel: int
    view: VendorView
    found: ViewSpans
    code_kind: CodeViewKind
    lanes: Mapping[int, TextLane]
    sinks: tuple[tuple[SinkOutlines, CutoutJudgment], ...]
    panel_headings: tuple[Box, ...]
    """Every heading printed on the view's panel: each is painted out of every picture."""

    @property
    def key(self) -> str:
        return f"p{self.page_index}:v{self.panel}.{self.view.number}"

    def span(self, number: int) -> Span:
        (found,) = [span for span in self.found.spans if span.number == number]
        return found


@dataclass(frozen=True, slots=True)
class PreparedPage:
    page_index: int
    page_sha256: str
    """The page's content hash (`source_page_sha256`), as the answer keys name pages."""
    document_version_id: str
    """What names the document in a question packet (a stored version id, or the file's hash)."""
    rendered: RenderedPage
    """The vendor's drawing only (`render_page(vendor_only=True)`), at `settings.dpi`."""
    pixels: PixelsOf
    views: tuple[PreparedView, ...]
    ink_at: Callable[[PixelBox], InkAt] | None
    """Whose ink a pixel box holds (`extraction.ink.PageInk.at`); `None` where it is unknown,
    which holds every number on the page ("couldn't check for reviewer markup")."""
    notes: tuple[str, ...] = ()


def _inside(x: Decimal, y: Decimal, box: Box) -> bool:
    return box.x0 <= x <= box.x1 and box.top <= y <= box.bottom


def prepare_view(
    *,
    ink: PageInk,
    chars: Sequence[TextChar],
    panel_phrase_chars: Sequence[TextChar],
    view: VendorView,
    page_index: int,
    panel: int,
    panel_headings: Sequence[Box],
    settings: DepthReaderSettings,
) -> PreparedView:
    """Code's half for one view: its spans on both axes, its kind from the heading and the wall
    band, each span's vector text, and each sink's outlines with code's cut-out judgment."""
    from extraction.architect.text import find_phrases

    found = find_spans(ink, view.extent, settings.spans)
    judged = code_view_kind(kind_from_heading(view.heading), found.bands)
    inside = [char for char in chars if _inside(char.box.centre_x, char.box.centre_y, view.extent)]
    phrases = find_phrases(inside, settings.views.text)
    lanes = text_lanes(found.spans, phrases, settings.spans, chars=inside)
    panel_phrases = find_phrases(list(panel_phrase_chars), settings.views.text)
    sinks = tuple(
        (sink, cutout_judgment(sink, panel_phrases))
        for sink in sink_outlines(ink, view.extent, phrases, settings.spans)
    )
    return PreparedView(
        page_index=page_index,
        panel=panel,
        view=view,
        found=found,
        code_kind=judged,
        lanes=lanes,
        sinks=sinks,
        panel_headings=tuple(panel_headings),
    )


#: The largest page render a reader picture is cut from (a 36" x 24" sheet at 300 dpi is 78 MP).
_MAXIMUM_PIXELS: Final = 100_000_000
#: The annotation reader and the render ask for a document version only to label what they return;
#: nothing here is stored.
_UNSTORED: Final = UUID(int=0)


def prepare_pdf_page(
    data: bytes,
    page_index: int,
    *,
    page_sha256: str,
    document_version_id: str,
    vendor_file: bool,
    settings: DepthReaderSettings,
) -> PreparedPage:
    """One page of a PDF, prepared by code at no cost: its vendor panels, each panel's views, and
    code's half for each view (`prepare_view`); the vendor-only render the pictures are cut from;
    and whose ink each place is.

    A vendor panel is a pasted drawing GV's heading names the vendor's (`extraction/panels.py`), or,
    with `vendor_file` (the vendor's own file, uploaded in the vendor's slot), every pasted drawing
    not inside another. Read-only: the PDF is never changed.
    """
    from extraction.annotations import read_markup_layer
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS, open_page, page_stamps
    from extraction.depth.view_kind import find_vendor_views
    from extraction.ink import read_page_ink
    from extraction.panels import propose_panel_roles
    from extraction.rasterise import render_page
    from extraction.reader import UnreadablePdf
    from extraction.stamp_text import stamps_only

    layers = read_markup_layer(data, page_index, document_version_id=_UNSTORED, dpi=settings.dpi)
    stamps, _pictures = page_stamps(data, page_index)
    if vendor_file:
        boxes = [box for box, _factor in stamps.values()]
        panels = [
            box
            for box in boxes
            if not any(
                other != box
                and other.x0 <= box.x0
                and box.x1 <= other.x1
                and other.top <= box.top
                and box.bottom <= other.bottom
                for other in boxes
            )
        ]
    else:
        panels = [
            stamps[proposal.annotation_index][0]
            for proposal in propose_panel_roles(layers.vendor_stamps, layers.markup)
            if proposal.role == "shop" and proposal.annotation_index in stamps
        ]
    rendered = render_page(
        data,
        page_index,
        document_version_id=_UNSTORED,
        page_content_hash=page_sha256,
        dpi=settings.dpi,
        maximum_pixels=_MAXIMUM_PIXELS,
        vendor_only=True,
    )
    notes: list[str] = []
    if not panels:
        notes.append("no vendor panel on this page")
        return PreparedPage(
            page_index,
            page_sha256,
            document_version_id,
            rendered,
            lambda box: (0, 0, 0, 0),
            (),
            None,
            tuple(notes),
        )
    page = open_page(
        stamps_only(data, page_index),
        page_index,
        drawing_boxes=tuple(box for box, _factor in stamps.values()),
        rows=MEASURED_ARCHITECT_SETTINGS.rows,
        settings=MEASURED_ARCHITECT_SETTINGS,
        dpi=settings.dpi,
    )
    ink_at: Callable[[PixelBox], InkAt] | None
    try:
        ink_at = read_page_ink(data, page_index, dpi=settings.dpi).at
    except UnreadablePdf as error:
        ink_at = None
        notes.append(f"whose ink each label is could not be read, so every number holds: {error}")
    views: list[PreparedView] = []
    for number, panel in enumerate(panels, start=1):
        survey = find_vendor_views(page.chars, page.ink, panel, settings.views)
        notes.extend(f"panel {number}: {note}" for note in survey.notes)
        panel_chars = [
            char for char in page.chars if _inside(char.box.centre_x, char.box.centre_y, panel)
        ]
        headings = [view.heading_box for view in survey.views if view.heading_box is not None]
        views.extend(
            prepare_view(
                ink=page.ink,
                chars=page.chars,
                panel_phrase_chars=panel_chars,
                view=view,
                page_index=page_index,
                panel=number,
                panel_headings=headings,
                settings=settings,
            )
            for view in survey.views
        )
    return PreparedPage(
        page_index=page_index,
        page_sha256=page_sha256,
        document_version_id=document_version_id,
        rendered=rendered,
        pixels=page.pixels,
        views=tuple(views),
        ink_at=ink_at,
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# What the reader returns
# ---------------------------------------------------------------------------

type Says = Literal["value", "kind", "unsure", "held", "invalid"]


@dataclass(frozen=True, slots=True)
class ReaderSays:
    """One reader's answer about one value, in the scorer's words (`eval/pariksha/depth.py`)."""

    model_id: str
    says: Says
    text: str = ""
    """The value's printed text, the kind, or the reason for a hold or no answer."""
    start_edge: str | None = None
    outline: str | None = None


@dataclass(frozen=True, slots=True)
class ValueReading:
    """One value on one view: usable only when both judgments agree."""

    field: str
    span_number: int | None
    readers: tuple[ReaderSays, ...]
    usable: bool
    inches: Fraction | None
    """Only when usable."""
    text: str | None
    reason: str
    start_edge: str | None = None
    outline: str | None = None
    checks: tuple[str, ...] = ()
    """What code checked and found agreeing, in plain words."""


@dataclass(frozen=True, slots=True)
class ViewReading:
    page_index: int
    page_sha256: str
    panel: int
    view_number: int
    extent: Box
    heading: str | None
    code_kind: CodeViewKind
    kinds: tuple[ReaderSays, ...]
    kind: ViewKind | None
    """Settled only when both readers and code name the same kind."""
    kind_reason: str
    values: tuple[ValueReading, ...]
    """Empty where the kind is not settled, or the view is not a plan or a section."""
    values_reason: str


@dataclass(frozen=True, slots=True)
class DepthReading:
    views: tuple[ViewReading, ...]
    questions_asked: int


# ---------------------------------------------------------------------------
# Asking: one question, and many through the shared route
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NoAnswer:
    """A reader gave no accepted answer to one question, and why."""

    reason: str


@dataclass(frozen=True, slots=True)
class Asked:
    key: str
    model_id: str
    page_index: int
    question: DepthQuestion
    packet: Mapping[str, object]


type Answers = Mapping[tuple[str, str], DepthReply | NoAnswer]
type Asker = Callable[[Sequence[Asked]], Answers]


def build_depth_request(
    question: DepthQuestion, *, model_id: str, max_tokens: int, claude_effort: ClaudeEffort
) -> dict[str, Any]:
    """The pictures in order, then the question, with its strict answer shape and stated effort.

    Claude readers only: the strict answer shape is sent as the request's structured output, which
    only the Claude adapters carry (`OUTPUT_CONFIG_KEY`)."""
    if not is_claude_model(model_id):
        raise ValueError("the depth reader asks only the Claude readers (their fixed answer shape)")
    if isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    content: list[dict[str, Any]] = [
        {"image": {"format": "png", "source": {"bytes": picture}}} for picture in question.pictures
    ]
    content.append({"text": question.prompt})
    return {
        "modelId": model_id,
        "messages": [{"role": "user", "content": content}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
        OUTPUT_CONFIG_KEY: output_config(question.schema, claude_effort),
    }


def _parse(question: DepthQuestion, data: Mapping[str, object], model_id: str) -> DepthReply:
    if question.prompt_id == VIEW_KIND_PROMPT_ID:
        return view_kind_answer(data, model_id=model_id)
    if question.prompt_id == SPAN_ROLE_PROMPT_ID:
        return span_role_answer(
            data, model_id=model_id, spans=question.spans, outlines=question.outlines
        )
    return depth_number_answer(data, model_id=model_id)


def ask_depth_question(
    question: DepthQuestion,
    client: ConverseClient,
    *,
    model_id: str,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None,
    claude_effort: ClaudeEffort,
) -> DepthReply:
    """Ask once, re-ask only a malformed answer; malformed twice raises `MalformedFormAnswer` and
    the reader abstains. Every attempt, answered or not, is recorded under the question's pinned id.
    """
    prompt_id = question.prompt_id
    for attempt in range(2):
        request = build_depth_request(
            question, model_id=model_id, max_tokens=max_tokens, claude_effort=claude_effort
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            answer = _parse(question, claude_answer(response, question.schema), model_id)
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    f"{prompt_id} answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                prompt_id,
                prompt_id,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return answer
    raise AssertionError("unreachable")


def question_packet(
    page: PreparedPage, key: str, question: DepthQuestion, effort: ClaudeEffort
) -> dict[str, object]:
    """What the question shows and asks, signed: the identity a stored answer is reused by
    (`workflow/reader_reuse.question_identity`)."""
    body: dict[str, object] = {
        "reader": "depth-reader",
        "document_version_id": page.document_version_id,
        "page_index": page.page_index,
        "source_page_sha256": page.page_sha256,
        "question_id": key,
        "prompt_id": question.prompt_id,
        "effort": effort,
        "spans": question.spans,
        "outlines": question.outlines,
        "images": {
            f"picture_{position}": {"sha256": hashlib.sha256(picture).hexdigest()}
            for position, picture in enumerate(question.pictures, start=1)
        },
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


# ---------------------------------------------------------------------------
# Pictures
# ---------------------------------------------------------------------------


def _pad(box: Box, pad: Decimal) -> Box:
    return Box(box.x0 - pad, box.top - pad, box.x1 + pad, box.bottom + pad)


def _factor(box: PixelBox, max_side: int) -> int:
    return max(1, -(-max(box[2] - box[0], box[3] - box[1]) // max_side))


def _marked(page: PreparedPage, span: Span) -> MarkedSpan:
    def at(along: Decimal) -> tuple[int, int]:
        x, y = span.point(along)
        left, top, _right, _bottom = page.pixels(Box(x, y, x, y))
        return left, top

    return MarkedSpan(span.number, span.axis, at(span.start_pt), at(span.end_pt))


def _view_crop(page: PreparedPage, view: PreparedView, settings: DepthReaderSettings) -> PixelBox:
    return page.pixels(_pad(view.view.extent, settings.view_pad_pt))


def _headings(page: PreparedPage, view: PreparedView) -> list[PixelBox]:
    return [page.pixels(box) for box in view.panel_headings]


def _view_kind_question(
    page: PreparedPage, view: PreparedView, settings: DepthReaderSettings
) -> DepthQuestion:
    crop = _view_crop(page, view, settings)
    factor = _factor(crop, settings.view_max_side)
    picture = view_picture(
        page.rendered,
        crop,
        _headings(page, view),
        pad=settings.thickness_px * factor,
        max_side=settings.view_max_side,
    )
    return DepthQuestion(VIEW_KIND_PROMPT_ID, (picture,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)


def _the_sink(view: PreparedView) -> tuple[SinkOutlines, CutoutJudgment] | None:
    return view.sinks[0] if len(view.sinks) == 1 else None


def _span_role_question(
    page: PreparedPage, view: PreparedView, kind: ViewKind, settings: DepthReaderSettings
) -> DepthQuestion:
    crop = _view_crop(page, view, settings)
    factor = _factor(crop, settings.view_max_side)
    headings = _headings(page, view)
    spans_png = numbered_spans_picture(
        page.rendered,
        crop,
        [_marked(page, span) for span in view.found.spans],
        headings=headings,
        thickness=settings.thickness_px * factor,
        badge=settings.badge_px * factor,
        max_side=settings.view_max_side,
    )
    pictures: tuple[bytes, ...] = (spans_png,)
    sink = _the_sink(view) if kind is ViewKind.PLAN else None
    outlines = 0
    if sink is not None and 0 < len(sink[0].outlines) <= 26:
        outlines = len(sink[0].outlines)
        pictures += (
            lettered_outlines_picture(
                page.rendered,
                crop,
                [(outline.letter, page.pixels(outline.box)) for outline in sink[0].outlines],
                headings=headings,
                thickness=settings.thickness_px * factor,
                badge=settings.badge_px * factor,
                max_side=settings.view_max_side,
            ),
        )
    spans = len(view.found.spans)
    return DepthQuestion(
        SPAN_ROLE_PROMPT_ID,
        pictures,
        span_role_prompt(spans=spans, outlines=outlines, plan=kind is ViewKind.PLAN),
        span_role_schema(spans, outlines),
        spans=spans,
        outlines=outlines,
    )


def _closeup_box(span: Span, settings: DepthReaderSettings) -> Box:
    along, across = settings.closeup_along_pt, settings.closeup_across_pt
    if span.axis is Axis.HORIZONTAL:
        return Box(
            span.start_pt - along,
            span.offset_pt - across,
            span.end_pt + along,
            span.offset_pt + across,
        )
    return Box(
        span.offset_pt - across,
        span.start_pt - along,
        span.offset_pt + across,
        span.end_pt + along,
    )


def _number_question(
    page: PreparedPage,
    view: PreparedView,
    span: Span,
    kind: ViewKind,
    settings: DepthReaderSettings,
) -> DepthQuestion:
    crop = _view_crop(page, view, settings)
    factor = _factor(crop, settings.view_max_side)
    marked = _marked(page, span)
    whole = numbered_spans_picture(
        page.rendered,
        crop,
        [marked],
        headings=_headings(page, view),
        thickness=settings.thickness_px * factor,
        badge=settings.badge_px * factor,
        max_side=settings.view_max_side,
    )
    close_box = page.pixels(_closeup_box(span, settings))
    close_factor = _factor(close_box, settings.closeup_max_side)
    close = end_marks_picture(
        page.rendered,
        close_box,
        marked,
        thickness=settings.thickness_px * close_factor,
        reach=settings.end_mark_reach_px * close_factor,
        max_side=settings.closeup_max_side,
    )
    turned = span.axis is Axis.VERTICAL
    pictures: tuple[bytes, ...] = (whole, close)
    if turned:
        pictures += (
            upright_png(close, label_rotation_degrees=90),
            upright_png(close, label_rotation_degrees=270),
        )
    direction: SpanDirection
    if kind is ViewKind.SECTION:
        direction = "across_the_section"
    elif span.axis is Axis.VERTICAL:
        direction = "down_the_plan"
    else:
        direction = "across_the_plan"
    return DepthQuestion(
        DEPTH_SPAN_PROMPT_ID,
        pictures,
        depth_span_prompt(direction=direction, turned=turned),
        DEPTH_SPAN_SCHEMA,
    )


# ---------------------------------------------------------------------------
# Judgment 1 + judgment 2: the view's kind
# ---------------------------------------------------------------------------

_AI_KIND: Final[Mapping[str, ViewKind]] = {
    "front": ViewKind.ELEVATION,
    "plan": ViewKind.PLAN,
    "section_or_side": ViewKind.SECTION,
    "detail": ViewKind.DETAIL,
    "other": ViewKind.OTHER,
}
_KIND_WORDS: Final[Mapping[ViewKind, str]] = {
    ViewKind.PLAN: "plan (top view)",
    ViewKind.SECTION: "section or side view",
    ViewKind.ELEVATION: "front view",
    ViewKind.DETAIL: "detail",
    ViewKind.OTHER: "drawing of another kind",
}
_NOTHING_READ: Final = "so nothing on this view is read"


def _kind_says(answer: DepthReply | NoAnswer | None, model_id: str) -> ReaderSays:
    if not isinstance(answer, ViewKindAnswer):
        reason = answer.reason if isinstance(answer, NoAnswer) else "no accepted answer"
        return ReaderSays(model_id, "invalid", reason)
    if answer.kind is None:
        return ReaderSays(model_id, "unsure")
    return ReaderSays(model_id, "kind", answer.kind)


def decide_view_kind(
    code: CodeViewKind, answers: Sequence[ViewKindAnswer | None]
) -> tuple[ViewKind | None, str]:
    """The view's kind when both readers and code name the same one; otherwise `None` and why."""
    if len(answers) != 2 or any(answer is None for answer in answers):
        return None, f"a reader gave no accepted answer about this view's kind, {_NOTHING_READ}"
    present = [answer for answer in answers if answer is not None]
    if any(answer.kind is None for answer in present):
        return None, f"a reader is unsure what kind of drawing this view is, {_NOTHING_READ}"
    named = {answer.kind for answer in present}
    if len(named) != 1:
        shown = " and ".join(sorted(_KIND_WORDS[_AI_KIND[str(kind)]] for kind in named))
        return None, f"the two readers disagree about this view's kind ({shown}), {_NOTHING_READ}"
    (word,) = named
    seen = _AI_KIND[str(word)]
    if code.kind is None:
        return None, (
            f"both readers see a {_KIND_WORDS[seen]}, but code cannot say ({code.reason}), "
            f"{_NOTHING_READ}"
        )
    if code.kind is not seen:
        return None, (
            f"the drawing's label says {_KIND_WORDS[code.kind]}; both readers see a "
            f"{_KIND_WORDS[seen]}: {_NOTHING_READ}"
        )
    return seen, f"both readers and code agree it is a {_KIND_WORDS[seen]} ({code.reason})"


# ---------------------------------------------------------------------------
# Judgment 1: which dimension plays which role
# ---------------------------------------------------------------------------

_ROLE_OF: Final[Mapping[str, str]] = {
    CT010: "depth",
    CT012: "cutout_width",
    CT008: "cutout_depth",
    CT007: "front_offset",
}
_ROLE_WORDS: Final[Mapping[str, str]] = {
    "depth": "the stone's full depth",
    "cutout_width": "the sink hole's width",
    "cutout_depth": "the sink hole's front-to-back size",
    "front_offset": "the strip in front of the sink hole",
}
_STONE_EDGE: Final = (
    "the vendor measures this strip from the stone's front edge; GV's check measures it from the "
    "cabinet front, so it is never the front offset: held for a person"
)


def _agreed_span(answers: Sequence[SpanRoleAnswer | None], role: str) -> tuple[int | None, str]:
    """The span both readers picked for `role`, or `None` and why not."""
    words = _ROLE_WORDS[role]
    if any(answer is None for answer in answers):
        return None, f"a reader gave no accepted answer about which dimension is {words}"
    picks = [answer.pick(role) for answer in answers if answer is not None]
    if "unsure" in picks:
        return None, f"a reader is unsure which dimension is {words}"
    if all(pick == "none" for pick in picks):
        return None, f"both readers say no dimension drawn is {words}"
    if "none" in picks:
        return None, f"one reader says no dimension drawn is {words}; the other picked one"
    numbers = {pick for pick in picks if isinstance(pick, int)}
    if len(numbers) != 1:
        shown = " and ".join(str(number) for number in sorted(numbers))
        return None, f"the readers picked different dimensions for {words} ({shown})"
    (number,) = numbers
    return number, f"both readers picked dimension {number} as {words}"


def _agreed_hole(answers: Sequence[SpanRoleAnswer | None]) -> tuple[str | None, str]:
    if any(answer is None for answer in answers):
        return None, "a reader gave no accepted answer about which outline is the hole"
    letters = {answer.hole_outline for answer in answers if answer is not None}
    if "unsure" in letters:
        return None, "a reader is unsure which outline is the hole cut in the stone"
    if "none" in letters:
        return None, "a reader names no outline as the hole cut in the stone"
    if len(letters) != 1:
        return (
            None,
            "the readers name different outlines as the hole (" + ", ".join(sorted(letters)) + ")",
        )
    (letter,) = letters
    return letter, f"both readers name outline {letter} as the hole"


# ---------------------------------------------------------------------------
# The number: the shared two-reader agreement, then inches only
# ---------------------------------------------------------------------------

#: The span's label as the shared agreement sees it: drawn (the readers are its two sources; the
#: file's text, where there is any, is code's check, judgment 2), alone in its close-up.
_LABEL: Final = PlannedLabel(
    box=Box(Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
    crop=Box(Decimal(0), Decimal(0), Decimal(0), Decimal(0)),
    lane=Lane.GLYPHS,
    text=None,
    text_stacked=False,
    has_digit=True,
    touches_edge=False,
    ambiguous_slot=False,
    crowded=False,
    ticks_in_crop=True,
    path_boxes=(),
)


@dataclass(frozen=True, slots=True)
class _Number:
    inches: Fraction | None
    text: str | None
    reason: str


def _seal(answers: Sequence[ReaderAnswer], ink: InkAt | None, settings: SpanSettings) -> _Number:
    outcome = seal_label(
        _LABEL,
        answers=answers,
        ink=ink,
        stacked_by_bar=False,
        allow_stacked=False,
        row_ambiguity=None,
        allow_claude_pair=True,
    )
    if outcome.state is LabelState.NOT_A_DIMENSION:
        return _Number(None, None, "both readers say this dimension has no printed label")
    if outcome.state is LabelState.REVIEW:
        agreed = outcome.sealed_text
        if (
            agreed is not None
            and read_depth_label(agreed, mm_tolerance=settings.mm_corroboration_mm).held_reason
            == MM_ONLY
        ):
            return _Number(None, agreed, MM_ONLY)
        return _Number(None, agreed, f"the two readers' reading is held: {outcome.reason}")
    measured = outcome.value if outcome.state is LabelState.SEALED else outcome.suggestion
    agreed = outcome.sealed_text
    if measured is None or agreed is None:
        return _Number(None, agreed, "the two readers' reading gives no value")
    if measured.unit is not Unit.INCH:
        return _Number(None, agreed, MM_ONLY)
    label = read_depth_label(agreed, mm_tolerance=settings.mm_corroboration_mm)
    if label.inches != measured.exact:
        return _Number(
            None,
            agreed,
            "the label's inches cannot be read exactly"
            + (f": {label.held_reason}" if label.held_reason else ""),
        )
    return _Number(measured.exact, agreed, f"both readers read {agreed} exactly")


# ---------------------------------------------------------------------------
# Judgment 2: code's checks of a value
# ---------------------------------------------------------------------------


def _depth_place(kind: ViewKind, span: Span) -> str | None:
    """Why code does not allow this span as the stone's depth, or `None` when it does."""
    if kind is ViewKind.SECTION:
        if span.axis is not Axis.HORIZONTAL:
            return "in a section the depth runs left to right across the stone; this one does not"
        return None
    if span.axis is not Axis.VERTICAL:
        return "on a plan the depth runs top to bottom; this dimension runs left to right"
    ends = {span.low_end.kind, span.high_end.kind}
    if ends != {EndKind.WALL_BAND, EndKind.OUTLINE}:
        return (
            "code does not see this dimension run from the wall band to the stone's front edge "
            f"(its ends meet: {span.low_end.kind.value}; {span.high_end.kind.value})"
        )
    return None


def _cutout_place(
    view: PreparedView, span: Span, axis: Axis, hole: str | None, hole_reason: str
) -> tuple[Outline | None, str | None]:
    """The hole outline both readers named, when code allows it; or why not."""
    if not view.sinks:
        return None, "code finds no sink outline in this view"
    sink = _the_sink(view)
    if sink is None:
        return None, f"code finds {len(view.sinks)} sinks in this view: a person picks"
    outlines, judgment = sink
    if not judgment.code_allows:
        return None, judgment.reason
    if hole is None:
        return None, hole_reason
    chosen = [outline for outline in outlines.outlines if outline.letter == hole]
    if not chosen:
        return None, f"outline {hole} is not one code drew"
    (outline,) = chosen
    if span.axis is not axis:
        return None, (
            "a cut-out's width runs left to right and its depth top to bottom on a plan; "
            "this dimension does not"
        )
    if not span_on_outline(span, outline, MEASURED_SPAN_SETTINGS):
        return None, f"this dimension's ends do not lie on outline {hole}'s edges"
    return outline, None


def _strip_place(
    span: Span,
    depth: Span | None,
    depth_reason: str,
    hole: Outline | None,
    hole_reason: str,
    settings: SpanSettings,
) -> str | None:
    """Why code does not allow this span as the strip in front of the hole (CT007), or `None`."""
    if depth is None:
        return f"the stone's front edge is not known ({depth_reason}): held"
    start = front_offset_start(span, depth, settings)
    if not start.code_allows:
        return start.reason
    if hole is None:
        return f"the sink hole is not settled ({hole_reason}), so where the strip ends is not known"
    front = depth.end_pt if depth.low_end.kind is EndKind.WALL_BAND else depth.start_pt
    edges = (hole.box.top, hole.box.bottom)
    hole_front = min(edges, key=lambda edge: abs(edge - front))
    if min(abs(span.start_pt - hole_front), abs(span.end_pt - hole_front)) > settings.end_meet_pt:
        return "the strip does not end on the sink hole's front edge"
    return None


@dataclass(frozen=True, slots=True)
class _Scale:
    scale: ViewScale | None
    reason: str


def _number_checks(
    view: PreparedView,
    span: Span,
    inches: Fraction,
    scale: _Scale,
    known: Mapping[int, Fraction],
    settings: SpanSettings,
) -> tuple[str | None, tuple[str, ...]]:
    """Code's checks of a sealed number: any that disagrees holds, and at least one must agree."""
    lane = view.lanes.get(span.number)
    agreed: list[str] = []
    if lane is not None and lane.printed and lane.inches is None:
        return f"the printed text at this dimension cannot be read exactly: {lane.reason}", ()
    if lane is not None and lane.inches is not None:
        if lane.inches != inches:
            return (
                f"the printed text at this dimension says {lane.printed[0]!r}, not the readers' "
                "reading: held"
            ), ()
        agreed.append("the vector text equals the two readers' reading exactly")
    if scale.scale is not None and scale.scale.overall_number != span.number:
        drawn = drawn_length_check(
            span, inches, scale.scale, tolerance=settings.drawn_length_tolerance
        )
        if not drawn.agrees:
            return drawn.reason, ()
        agreed.append(drawn.reason)
    for closure in view.found.closures:
        if span is closure.overall or span in closure.parts:
            checked = closure_check(closure, known)
            if checked.closes is False:
                return f"the chain this dimension is part of does not close: {checked.reason}", ()
            if checked.closes:
                agreed.append("the chain it is part of sums exactly to its overall")
    if not agreed:
        scale_note = (
            "the view's scale comes from this very dimension"
            if scale.scale is not None
            else scale.reason
        )
        return (
            "no code check could confirm the number: no printed text at this dimension, and no "
            f"independent drawn-length check ({scale_note})"
        ), ()
    return None, tuple(agreed)


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


@dataclass
class _ViewState:
    page: PreparedPage
    view: PreparedView
    kinds: tuple[ReaderSays, ...] = ()
    kind: ViewKind | None = None
    kind_reason: str = ""
    roles: tuple[SpanRoleAnswer | None, ...] | None = None
    roles_reason: str = ""
    role_says: tuple[ReaderSays, ...] = ()
    numbers: dict[int, tuple[DepthReply | NoAnswer | None, ...]] = field(default_factory=dict)
    overall: int | None = None


def _number_says(
    model_id: str, answer: DepthReply | NoAnswer | None
) -> tuple[ReaderSays, ReaderAnswer | None]:
    if not isinstance(answer, ReaderAnswer):
        reason = answer.reason if isinstance(answer, NoAnswer) else "no accepted answer"
        return ReaderSays(model_id, "invalid", reason), None
    if answer.belongs.value == "unsure" or (answer.belongs.value == "yes" and not answer.readable):
        return ReaderSays(model_id, "unsure"), answer
    if answer.says_no_dimension or not answer.text.strip():
        return ReaderSays(model_id, "held", "the reader finds no printed label for it"), answer
    return ReaderSays(model_id, "value", normalise_text(answer.text)), answer


def _role_pick_says(
    model_id: str, answer: SpanRoleAnswer | None, role: str, field_name: str
) -> ReaderSays | None:
    """What a reader said before any number: `None` when it picked a span (its number decides)."""
    words = _ROLE_WORDS[role]
    if answer is None:
        return ReaderSays(model_id, "invalid", f"no accepted answer about {words}")
    pick = answer.pick(role)
    if pick == "unsure":
        return ReaderSays(model_id, "unsure")
    if pick == "none":
        return ReaderSays(model_id, "held", f"the reader says no dimension drawn is {words}")
    if field_name == CT007:
        if answer.front_offset_start == "stone_front_edge":
            return ReaderSays(model_id, "held", _STONE_EDGE)
        if answer.front_offset_start == "unsure":
            return ReaderSays(model_id, "unsure")
    return None


def _scale(state: _ViewState, sealed: Mapping[int, _Number]) -> _Scale:
    overall = overall_horizontal(state.view.found.spans)
    if overall is None:
        return _Scale(None, "the view has no horizontal dimension to take its scale from")
    lane = state.view.lanes.get(overall.number)
    if lane is not None and lane.inches is not None:
        return _Scale(view_scale(overall, lane.inches), "")
    number = sealed.get(overall.number)
    if number is not None and number.inches is not None:
        return _Scale(view_scale(overall, number.inches), "")
    return _Scale(
        None,
        f"the view's horizontal overall (dimension {overall.number}) has no exact value",
    )


def _values(
    state: _ViewState,
    readers: tuple[str, str],
    settings: DepthReaderSettings,
) -> tuple[ValueReading, ...]:
    """Every value the view's kind can hold, each decided from both judgments."""
    view, page = state.view, state.page
    assert state.kind is not None and state.roles is not None
    fields = (CT010,) if state.kind is ViewKind.SECTION else (CT010, CT012, CT008, CT007)
    spans_settings = settings.spans
    ink: dict[int, InkAt | None] = {}
    sealed: dict[int, _Number] = {}
    says: dict[int, tuple[ReaderSays, ...]] = {}
    for asked_number, answers in state.numbers.items():
        pairs = [
            _number_says(model, answer) for model, answer in zip(readers, answers, strict=True)
        ]
        says[asked_number] = tuple(pair[0] for pair in pairs)
        present = [pair[1] for pair in pairs if pair[1] is not None]
        span = view.span(asked_number)
        box = page.pixels(_closeup_box(span, settings))
        ink[asked_number] = None if page.ink_at is None else page.ink_at(box)
        sealed[asked_number] = _seal(present, ink[asked_number], spans_settings)
    scale = _scale(state, sealed)
    known: dict[int, Fraction] = {
        key: lane.inches for key, lane in view.lanes.items() if lane.inches is not None
    }
    known.update({key: got.inches for key, got in sealed.items() if got.inches is not None})

    depth_number, depth_reason = _agreed_span(state.roles, "depth")
    depth_span = None
    if depth_number is not None:
        if _depth_place(state.kind, view.span(depth_number)) is None:
            depth_span = view.span(depth_number)
        else:
            depth_reason = _depth_place(state.kind, view.span(depth_number)) or depth_reason
    hole_letter, hole_reason = _agreed_hole(state.roles)
    hole_outline: Outline | None = None

    readings: list[ValueReading] = []
    for field_name in fields:
        role = _ROLE_OF[field_name]
        number, agreed_reason = _agreed_span(state.roles, role)
        pre = [
            _role_pick_says(model, answer, role, field_name)
            for model, answer in zip(readers, state.roles, strict=True)
        ]
        if number is None or any(item is not None for item in pre):
            raw = tuple(
                (
                    item
                    if item is not None
                    else ReaderSays(
                        model,
                        "held",
                        f"picked dimension {_pick_of(answer, role)}; {agreed_reason}",
                    )
                )
                for model, answer, item in zip(readers, state.roles, pre, strict=True)
            )
            reason = agreed_reason
            if field_name == CT007 and any(
                item is not None and item.text == _STONE_EDGE for item in pre
            ):
                reason = _STONE_EDGE
            elif number is not None:
                reason = next(
                    (
                        (
                            "a reader is unsure where the strip starts"
                            if item.says == "unsure"
                            else item.text
                        )
                        for item in pre
                        if item is not None
                    ),
                    agreed_reason,
                )
            readings.append(ValueReading(field_name, number, raw, False, None, None, reason))
            continue
        span = view.span(number)
        reader_says = says.get(
            number,
            tuple(ReaderSays(model, "invalid", "the number was not asked") for model in readers),
        )
        extra_edge = "cabinet_face" if field_name == CT007 else None
        outline_letter: str | None = None
        place: str | None
        if field_name == CT010:
            place = _depth_place(state.kind, span)
        elif field_name in (CT012, CT008):
            axis = Axis.HORIZONTAL if field_name == CT012 else Axis.VERTICAL
            chosen, place = _cutout_place(view, span, axis, hole_letter, hole_reason)
            if chosen is not None:
                outline_letter = chosen.letter
                hole_outline = chosen if hole_outline is None else hole_outline
        else:
            place = None
        if field_name == CT007:
            if hole_outline is None and hole_letter is not None:
                sink = _the_sink(view)
                if sink is not None and sink[1].code_allows:
                    found = [o for o in sink[0].outlines if o.letter == hole_letter]
                    hole_outline = found[0] if found else None
            hole_note = hole_reason
            sink = _the_sink(view)
            if sink is not None and not sink[1].code_allows:
                hole_note = sink[1].reason
            place = _strip_place(
                span, depth_span, depth_reason, hole_outline, hole_note, spans_settings
            )
        reader_says = tuple(
            replace_says(
                item, start_edge=extra_edge, outline=hole_letter if outline_letter else None
            )
            for item in reader_says
        )
        found_number = sealed.get(number)
        if found_number is None:
            readings.append(
                ValueReading(
                    field_name, number, reader_says, False, None, None, "the number was not asked"
                )
            )
            continue
        if found_number.inches is None:
            readings.append(
                ValueReading(
                    field_name,
                    number,
                    reader_says,
                    False,
                    None,
                    found_number.text,
                    found_number.reason,
                    start_edge=extra_edge,
                    outline=outline_letter,
                )
            )
            continue
        if place is not None:
            readings.append(
                ValueReading(
                    field_name,
                    number,
                    reader_says,
                    False,
                    None,
                    found_number.text,
                    f"both readers agree, but code holds it: {place}",
                    start_edge=extra_edge,
                    outline=outline_letter,
                )
            )
            continue
        objection, checks = _number_checks(
            view, span, found_number.inches, scale, known, spans_settings
        )
        if objection is not None:
            readings.append(
                ValueReading(
                    field_name,
                    number,
                    reader_says,
                    False,
                    None,
                    found_number.text,
                    f"both readers agree, but code holds it: {objection}",
                    start_edge=extra_edge,
                    outline=outline_letter,
                )
            )
            continue
        readings.append(
            ValueReading(
                field_name,
                number,
                reader_says,
                True,
                found_number.inches,
                found_number.text,
                f"{agreed_reason}; {found_number.reason}; code agrees: " + "; ".join(checks),
                start_edge=extra_edge,
                outline=outline_letter,
                checks=checks,
            )
        )
    return tuple(readings)


def replace_says(says: ReaderSays, *, start_edge: str | None, outline: str | None) -> ReaderSays:
    """A reader's value with its start edge (CT007) and outline (a cut-out) beside it."""
    if says.says != "value":
        return says
    return ReaderSays(says.model_id, says.says, says.text, start_edge, outline)


def _pick_of(answer: SpanRoleAnswer | None, role: str) -> str:
    return "nothing" if answer is None else str(answer.pick(role))


def _depths_disagree(views: list[ViewReading]) -> list[ViewReading]:
    """A depth usable on two views of one panel with different values holds on both."""
    by_panel: dict[tuple[int, int], list[tuple[int, ValueReading]]] = {}
    for index, reading in enumerate(views):
        for value in reading.values:
            if value.field == CT010 and value.usable:
                by_panel.setdefault((reading.page_index, reading.panel), []).append((index, value))
    out = list(views)
    for found in by_panel.values():
        if len({value.inches for _index, value in found}) <= 1:
            continue
        for index, _value in found:
            reading = out[index]
            out[index] = replace(
                reading,
                values=tuple(
                    (
                        replace(
                            value,
                            usable=False,
                            inches=None,
                            checks=(),
                            reason="the depth printed on two views of this drawing disagree: "
                            "held on both",
                        )
                        if value.field == CT010 and value.usable
                        else value
                    )
                    for value in reading.values
                ),
            )
    return out


def read_depth(
    pages: Sequence[PreparedPage],
    *,
    ask: Asker,
    readers: tuple[str, str] = CLAUDE_PAIR,
    settings: DepthReaderSettings = MEASURED_DEPTH_READER_SETTINGS,
) -> DepthReading:
    """Read every prepared view in three rounds of questions (kind, roles, numbers), each round
    asked at once through `ask`, and decide every value from both judgments."""
    if tuple(readers) != CLAUDE_PAIR:
        raise ValueError("the depth reader is read by the decided Claude pair only")
    asked = 0
    states = [_ViewState(page, view) for page in pages for view in page.views]

    def round_of(questions: list[tuple[_ViewState, str, DepthQuestion]]) -> Answers:
        nonlocal asked
        jobs = [
            Asked(
                key,
                model,
                state.page.page_index,
                question,
                question_packet(state.page, key, question, settings.effort),
            )
            for state, key, question in questions
            for model in readers
        ]
        asked += len(jobs)
        return ask(jobs) if jobs else {}

    kind_questions = [
        (state, f"{state.view.key}:kind", _view_kind_question(state.page, state.view, settings))
        for state in states
    ]
    answers = round_of(kind_questions)
    for state, key, _question in kind_questions:
        got = [answers.get((key, model)) for model in readers]
        state.kinds = tuple(_kind_says(answer, model) for answer, model in zip(got, readers))
        state.kind, state.kind_reason = decide_view_kind(
            state.view.code_kind,
            [answer if isinstance(answer, ViewKindAnswer) else None for answer in got],
        )

    role_questions = []
    for state in states:
        if state.kind not in (ViewKind.PLAN, ViewKind.SECTION):
            continue
        if not state.view.found.spans:
            state.roles_reason = "code found no dimension line on this view"
            continue
        role_questions.append(
            (
                state,
                f"{state.view.key}:roles",
                _span_role_question(state.page, state.view, state.kind, settings),
            )
        )
    answers = round_of(role_questions)
    for state, key, _question in role_questions:
        state.roles = tuple(
            answer if isinstance(answer, SpanRoleAnswer) else None
            for answer in (answers.get((key, model)) for model in readers)
        )

    number_questions = []
    for state in states:
        if state.roles is None or state.kind is None:
            continue
        roles = ("depth",) if state.kind is ViewKind.SECTION else tuple(_ROLE_OF.values())
        wanted: list[int] = []
        for role in roles:
            number, _reason = _agreed_span(state.roles, role)
            if number is None:
                continue
            if role == "front_offset" and any(
                answer is None or answer.front_offset_start != "cabinet_face"
                for answer in state.roles
            ):
                continue
            wanted.append(number)
        if not wanted:
            continue
        overall = overall_horizontal(state.view.found.spans)
        if (
            overall is not None
            and overall.number not in wanted
            and state.view.lanes[overall.number].inches is None
            and any(state.view.lanes[number].inches is None for number in wanted)
        ):
            wanted.append(overall.number)
            state.overall = overall.number
        for number in wanted:
            number_questions.append(
                (
                    state,
                    f"{state.view.key}:n{number}",
                    _number_question(
                        state.page, state.view, state.view.span(number), state.kind, settings
                    ),
                )
            )
    answers = round_of(number_questions)
    for state, key, _question in number_questions:
        number = int(key.rsplit(":n", 1)[1])
        state.numbers[number] = tuple(answers.get((key, model)) for model in readers)

    views: list[ViewReading] = []
    for state in states:
        if state.kind is None:
            values: tuple[ValueReading, ...] = ()
            values_reason = state.kind_reason
        elif state.kind not in (ViewKind.PLAN, ViewKind.SECTION):
            values = ()
            values_reason = (
                f"this view is a {_KIND_WORDS[state.kind]}: depths and sinks are read only on a "
                "plan or a section"
            )
        elif state.roles is None:
            values = ()
            values_reason = state.roles_reason
        else:
            values = _values(state, readers, settings)
            values_reason = ""
        views.append(
            ViewReading(
                page_index=state.page.page_index,
                page_sha256=state.page.page_sha256,
                panel=state.view.panel,
                view_number=state.view.view.number,
                extent=state.view.view.extent,
                heading=state.view.view.heading,
                code_kind=state.view.code_kind,
                kinds=state.kinds,
                kind=state.kind,
                kind_reason=state.kind_reason,
                values=values,
                values_reason=values_reason,
            )
        )
    return DepthReading(views=tuple(_depths_disagree(views)), questions_asked=asked)
