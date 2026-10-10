"""Read the architect's dimensions on one combined sheet, with two judgments for every decision (#1052).

On the client's sheets the architect's drawing is pasted beside the vendor's, and its dimensions are
**real text** inside the pasted drawing (`4' - 2"`), so code reads them exactly, at no cost and with
no AI. This module is that reading, for one page, with nothing stored:

1. **Which drawing is the architect's** — the heading and the drawing's own content, both
   (`views.py`); on a page that prints no heading at all, the content of both drawings, each clearly
   one side's (`views.decide_without_headings`). Only a drawing with an architect's heading or
   architect-like content is read; a value read in a drawing whose role is not decided by code as
   the architect's is reported but **held**. A pasted drawing is a `/Stamp` or a `/Square` holding
   a drawing (`stamp_text.carries_drawing`); a pasted picture holds nothing code can read and is
   listed with that reason (`ArchitectPage.pictures`).
2. **The architect's dimension rows** — the same row finder the vendor's rows come from
   (`extraction/geometry/rows.py`): its ticks, and the spans between them. Each printed label is
   given to the one span and row it is printed on (`text.py`).
3. **Each number, two judgments.** (A) the label parses exactly (`labels.py`); (B) the length drawn
   between the span's ticks, through the drawing's own scale, agrees with it within a stated band.
   The scale is witnessed twice: (i) the median, over the drawing's *other* labels, of drawn length
   per printed inch, and (ii) the printed scale note times the factor the drawing was pasted at
   (its `/Rect` against its appearance's `/BBox` through its `/Matrix`). A span is counted only when
   every available witness agrees with its label; with none, or with any disagreeing, it is held
   with the reason.
4. **Whether a span runs between casework edges** or to a centre line (`outline.py`), so a fixture's
   centre-line dimension is never paired with a cabinet; nor a span over hatched material, nor one
   from one thing to another (a clearance).

**The architect's own file (#1163).** When the architect's drawings are uploaded as their own PDF,
the caller says so (`on_architect_file`). A drawing pasted there is read as above, its role from
the document's kind and its content agreeing (`views.judge_by_document`, ADR-0020 decision 3). A
page with nothing pasted — how an architect issues a set — is read from its own content: its views
are found from their printed titles, scale notes and bubbles (`page_views.py`), each is read inside
its extent exactly as a pasted drawing is, at the page's own size (a paste factor of exactly 1), and
its role is again the document's kind and its content agreeing. Each view is named by its page-local
number. A view not clearly apart from another is read with every value held.

**Any page origin.** A page whose media box does not start at (0, 0) is read in a frame moved to
its visible corner (`_ZeroOrigin`) and the result moved back to pdfplumber's frame (#1163).

**What it never does.** It never reads coloured ink (GV's markup) as text, a tick or an edge; never
rounds; never fills in a value it could not read; and never decides anything it was not given two
judgments for. A held span's inches are `None`: whoever uses the result cannot take its number by
mistake.

Pure: PDF bytes in, plain values out, no database. Source: issues #1052, #1163 · Verification:
`tests/extraction/architect/test_reader.py`, `tests/extraction/architect/test_architect_sheet.py`
"""

from __future__ import annotations

import io
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
from typing import Any, Final
from uuid import UUID

import pdfplumber
import pikepdf

from evidence.coordinates import PdfPoint, StoredPoint
from extraction.annotations import read_markup_layer
from extraction.architect.labels import Qualifier
from extraction.architect.outline import (
    LineWork,
    OutlineSettings,
    end_witness,
    hatched_regions,
    line_work,
    one_object,
    slanted_strokes,
    span_on_outline,
)
from extraction.architect.page_views import (
    NO_VIEW_FOUND,
    PageNote,
    PageViewSettings,
    survey_page_views,
)
from extraction.architect.text import Orientation as TextOrientation
from extraction.architect.text import (
    PrintedDimension,
    TextChar,
    TextSettings,
    find_printed,
    orientation_of,
)
from extraction.architect.views import (
    ContentJudgment,
    Role,
    ViewJudgment,
    count_labels,
    decide_without_headings,
    drawn_architectural_scale,
    judge_by_document,
    judge_content,
    judge_view,
)
from extraction.geometry.rows import (
    MEASURED_SETTINGS,
    Box,
    CountertopRowCandidate,
    PageInk,
    RowSettings,
    TickSource,
    build_rows,
)
from extraction.panels import PanelRoleProposal, printed_headings, propose_panel_roles
from extraction.reader import UnreadablePdf, page_frame, pixel_placement
from extraction.rows import ink_from_page
from extraction.stamp_text import carries_drawing, drawing_ink, pasted_picture, stamps_only

__all__ = [
    "MEASURED_ARCHITECT_SETTINGS",
    "ArchitectPage",
    "ArchitectPicture",
    "ArchitectRow",
    "ArchitectSettings",
    "ArchitectSpan",
    "ArchitectView",
    "read_architect_page",
]

_TWO: Final = Decimal(2)

#: The kind of note for a stamp that holds no drawing (`app.models.evidence.ArchitectPageNote`).
STAMP_NOT_DRAWING: Final = "stamp_not_drawing"
_POINTS_PER_INCH: Final = Decimal(72)

#: A random but fixed id: the annotation reader asks for a document version only to label what it
#: returns, and nothing here is stored.
_UNSTORED: Final = UUID("00000000-0000-0000-0000-000000001052")


@dataclass(frozen=True, slots=True)
class ArchitectSettings:
    """Every threshold the architect reader uses. No defaults: `MEASURED_ARCHITECT_SETTINGS` is the
    one set measured on the client's two sets (#1052)."""

    rows: RowSettings
    content_rows: RowSettings
    """The row finder's thresholds for a view drawn as the page's own content (#1163): drawn at the
    sheet's full size, so its tick slashes are larger than a pasted, shrunk drawing's."""
    text: TextSettings
    outline: OutlineSettings
    label_reach_pt: Decimal
    """A label belongs to a row when its middle is this close to the row's line."""
    label_slack_pt: Decimal
    """...and its middle is over the span, or this far past the row's outer ticks."""
    witness_band: Decimal
    """(B) agrees with (A) when the drawn length through the scale is within this fraction of the
    printed value..."""
    witness_floor_in: Decimal
    """...or within this many inches, whichever is larger (a short span's ticks are a large share of
    its drawn length)."""
    scale_agreement: Decimal
    """The two scale witnesses of a drawing agree when within this fraction of each other."""
    leave_one_out_minimum: int
    """Witness (i) needs at least this many other labelled spans in the drawing."""
    tick_pair_pt: Decimal
    """Two ticks this close with nothing printed between them are one tick drawn twice (a heavy
    slash's two edges): they become one, at their middle."""
    views: PageViewSettings
    """How views drawn as the page's own content are found on the architect's file (#1163)."""


@dataclass(frozen=True, slots=True)
class ArchitectView:
    """One drawing on the page, both judgments of its role, and its scale."""

    annotation_index: int
    """For a pasted drawing, its `/Annots` index; for a view drawn as the page's own content
    (`source == "content"`, #1163), its page-local view number (`page_views.PageView.number`)."""
    judgment: ViewJudgment
    box: Box
    """The drawing's rectangle in pdfplumber's frame (page points, `top` downward)."""
    stored_points: tuple[tuple[Decimal, Decimal], ...]
    """Its region in stored coordinates, as `DrawingView.region` keeps it."""
    read: bool
    """Whether its rows were read: a heading, its content or its labels point to the architect."""
    scale_note: str | None
    scale_paper_per_real: Fraction | None
    paste_factor: Decimal | None
    """Page points per point of the pasted sheet; `None` when it cannot be worked out exactly."""
    absolute_points_per_inch: Decimal | None
    """Witness (ii): the printed scale times the paste factor, in page points per real inch."""
    median_points_per_inch: Decimal | None
    """Witness (i) over all the drawing's labelled spans."""
    points_per_inch: Decimal | None
    """The drawing's scale for a caller that converts drawn lengths (the pairing): (i) when at least
    three spans agree on it and (ii) does not contradict it, else (ii); `None` when neither."""
    scale_reason: str
    source: str = "pasted"
    """`pasted` (a `/Stamp` or a drawing `/Square`) or `content` (the page's own content, #1163)."""
    title: str | None = None
    """The view title printed under a content view (`SAMPLE ROOM ELEVATION`)."""
    bubble: str | None = None
    """What a content view's bubble prints: its view number and sheet reference."""
    separated: bool = True
    """Whether the view stands clearly apart from every other view on the page
    (`page_views.PageView.separated`, #1166); a pasted drawing always does."""


@dataclass(frozen=True, slots=True)
class ArchitectSpan:
    """One span of an architect's row: where it is drawn, what is printed on it, what may be used."""

    index: int
    x0_pt: Decimal
    x1_pt: Decimal
    """The span's ticks, in page points (pdfplumber's frame)."""
    text: str | None
    """The label as printed, its pieces joined; `None` when nothing is printed on the span."""
    qualifiers: frozenset[Qualifier]
    printed_inches: Fraction | None
    """What the label parses to, exactly, for a person reading the report."""
    inches: Fraction | None
    """The value, only when nothing holds it: parsed exactly, witnessed by the drawn length, in a
    drawing both judgments give as the architect's. `None` whenever `held_reason` is set."""
    held_reason: str | None
    witness_inches: Decimal | None
    """The drawn length through the scale witness used, for the report."""
    on_outline: bool | None
    outline_reason: str
    label_box: Box | None
    label_pixels: tuple[int, int, int, int] | None
    """The label's box in the page's pixels at the dpi asked for, `(left, top, right, bottom)`."""
    span_pixels: tuple[int, int, int, int]
    """The span between its ticks, in the same pixels."""

    @property
    def drawn_pt(self) -> Decimal:
        return self.x1_pt - self.x0_pt


@dataclass(frozen=True, slots=True)
class ArchitectRow:
    """One of the architect's dimension rows: its line, ticks and spans."""

    view_annotation_index: int
    rank: int
    """Its place among the page's architect rows, top to bottom: `arch-row:<rank>`."""
    y: Decimal
    ticks: tuple[Decimal, ...]
    tick_source: TickSource
    spans: tuple[ArchitectSpan, ...]
    points_per_inch: Decimal | None
    """The drawing's scale (`ArchitectView.points_per_inch`), repeated for the pairing."""
    view_source: str = "pasted"
    """Its view's `ArchitectView.source`: with `view_annotation_index`, which view it is in."""


@dataclass(frozen=True, slots=True)
class ArchitectPicture:
    """A drawing pasted as a picture: an image with no text or line-work, so code reads nothing in
    it. Listed so a missing reading has its reason; a person reads it."""

    annotation_index: int
    box: Box
    reason: str


@dataclass(frozen=True, slots=True)
class ArchitectPage:
    page_index: int
    views: tuple[ArchitectView, ...]
    rows: tuple[ArchitectRow, ...]
    pictures: tuple[ArchitectPicture, ...] = ()
    notes: tuple[PageNote, ...] = ()
    """What was left out of every view on the page and why, and why no view was found (#1163):
    never silent."""


#: Measured on both client sets (#1052, report `1052-report.md`): the row finder's E1 thresholds,
#: the text and outline lengths below, and the scale band that accepted every right value on both
#: sets and refuses a value off by one bay.
MEASURED_ARCHITECT_SETTINGS = ArchitectSettings(
    # The architect's tick slashes are drawn larger than the vendor's (over 4 pt on the client's
    # pasted drawings): the vendor's E1 thresholds with a larger slash. The vendor's rows are never built
    # with these.
    rows=replace(MEASURED_SETTINGS, slash_maximum_pt=Decimal(6)),
    # An architect's drawing at its own size draws its slashes up to about 6.5 pt across (measured
    # locally on a test split of one keyed set: its pasted architect drawings put back on pages of
    # their own at full size, #1163); the pasted drawings above were shrunk to 0.7–0.9 of that.
    # **Tuned on that test split only: to be proved again on the architect's real PDF.**
    # Only marks drawn at about 45 degrees are slashes there: a larger size limit must not let in an
    # arrowhead lying along its row (7 by 3 pt), which a slash never is.
    content_rows=replace(
        MEASURED_SETTINGS, slash_maximum_pt=Decimal(8), slash_squareness_fraction=Decimal("0.35")
    ),
    text=TextSettings(
        same_line_em=Decimal("0.3"),
        space_em=Decimal("0.15"),
        phrase_em=Decimal("1.0"),
        duplicate_em=Decimal("0.2"),
        same_size_fraction=Decimal("0.1"),
    ),
    outline=OutlineSettings(
        straightness_pt=Decimal("0.3"),
        tolerance_pt=Decimal("1.0"),
        row_clearance_pt=Decimal("1.0"),
        join_gap_pt=Decimal("0.05"),
        solid_minimum_pt=Decimal(12),
        dash_maximum_pt=Decimal(6),
        dash_minimum_pieces=3,
        mark_reach_pt=Decimal(4),
        link_pt=Decimal(4),
        toe_kick_in=Decimal(6),
        hatch_parallel_sine=Decimal("0.02"),
        hatch_spacing_pt=Decimal(6),
        hatch_minimum_lines=5,
    ),
    label_reach_pt=Decimal(12),
    label_slack_pt=Decimal(2),
    witness_band=Decimal("0.04"),
    witness_floor_in=Decimal(1),
    scale_agreement=Decimal("0.04"),
    leave_one_out_minimum=2,
    tick_pair_pt=Decimal(2),
    # Chosen on synthetic sheets laid out as an architect's set prints one (a title over its scale
    # note, a bubble to their left, a border and a title block, notes beside it); checked locally on
    # a test split of one keyed set — its 13 pasted architect drawings put back on pages of their
    # own, one drawing a page, with made-up title blocks — where it found every view and nothing
    # else (#1163). **Not proved on an architect's real issued PDF: to be proved again on one.**
    views=PageViewSettings(
        title_gap_em=Decimal("1.5"),
        title_reach_em=Decimal(2),
        title_minimum_letters=3,
        bubble_reach_em=Decimal(3),
        bubble_maximum_em=Decimal(4),
        bubble_squareness=Decimal("0.2"),
        join_pt=Decimal(3),
        label_slack_pt=Decimal(6),
        below_reach_em=Decimal(4),
        border_strokes=12,
        side_reach_em=Decimal(3),
        minimum_drawing_strokes=12,
        frame_margin_em=Decimal(2),
    ),
)


def _decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        raise TypeError("a coordinate cannot be a bool")
    if isinstance(value, (int, float, str)):
        return Decimal(str(value))
    raise TypeError(f"not a number: {value!r}")


def _box(obj: dict[str, Any]) -> Box:
    x0, x1 = _decimal(obj["x0"]), _decimal(obj["x1"])
    top, bottom = _decimal(obj["top"]), _decimal(obj["bottom"])
    return Box(min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom))


def _inside(inner: Box, outer: Box) -> bool:
    return (
        outer.x0 <= inner.x0
        and inner.x1 <= outer.x1
        and outer.top <= inner.top
        and inner.bottom <= outer.bottom
    )


def _number(value: object) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None


def _paste_factor(annotation: Any) -> Decimal | None:
    """Page points per point of the pasted sheet, from the stamp's `/Rect`, `/BBox` and `/Matrix`.

    PDF 32000-1 §12.5.5: the appearance's `/BBox` is taken through its `/Matrix`, and the box that
    gives is fitted onto `/Rect`. The factor along the page's x is the matrix's own scale on that
    axis times the fit. Only an unrotated or a quarter-turned matrix is worked out, and only when
    the factor is the same on both axes; anything else is `None`, never an approximation.
    """
    try:
        rect = [_number(value) for value in annotation["/Rect"]]
        appearance = annotation["/AP"]["/N"]
        bbox = [_number(value) for value in appearance["/BBox"]]
        raw_matrix = appearance.get("/Matrix")
        matrix = (
            [Decimal(1), Decimal(0), Decimal(0), Decimal(1), Decimal(0), Decimal(0)]
            if raw_matrix is None
            else [_number(value) for value in raw_matrix]
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    if any(value is None for value in (*rect, *bbox, *matrix)) or len(rect) != 4:
        return None
    r = [value for value in rect if value is not None]
    b = [value for value in bbox if value is not None]
    a, bb, c, d = (value for value in matrix[:4] if value is not None)
    width, height = abs(b[2] - b[0]), abs(b[3] - b[1])
    rect_width, rect_height = abs(r[2] - r[0]), abs(r[3] - r[1])
    if not (width and height and rect_width and rect_height):
        return None
    if bb == 0 and c == 0 and a and d:
        across, down = abs(a) * width, abs(d) * height
        factor_x = abs(a) * rect_width / across
        factor_y = abs(d) * rect_height / down
    elif a == 0 and d == 0 and bb and c:
        across, down = abs(c) * height, abs(bb) * width
        factor_x = abs(c) * rect_width / across
        factor_y = abs(bb) * rect_height / down
    else:
        return None
    if abs(factor_x - factor_y) > factor_x / 100:
        return None
    return factor_x


def _page_box(annotation: Any, left_edge: Decimal, top_edge: Decimal) -> Box:
    x0, y0, x1, y1 = (_decimal(value) for value in annotation["/Rect"])
    return Box(
        min(x0, x1) - left_edge,
        top_edge - max(y0, y1),
        max(x0, x1) - left_edge,
        top_edge - min(y0, y1),
    )


def _stamps(
    data: bytes, page_index: int
) -> tuple[dict[int, tuple[Box, Decimal | None]], tuple[ArchitectPicture, ...]]:
    """Each pasted drawing's rectangle in pdfplumber's frame and its paste factor, by `/Annots`
    index (`stamp_text.carries_drawing`: every `/Stamp`, and a `/Square` holding a drawing); and
    every pasted picture, which holds nothing code can read."""
    found: dict[int, tuple[Box, Decimal | None]] = {}
    pictures: list[ArchitectPicture] = []
    with pikepdf.open(io.BytesIO(data)) as pdf:
        page = pdf.pages[page_index]
        media = [_decimal(value) for value in page.obj.get("/MediaBox", [0, 0, 612, 792])]
        crop = [_decimal(value) for value in page.obj.get("/CropBox", media)]
        top_edge = crop[3]
        left_edge = crop[0]
        for index, annotation in enumerate(page.obj.get("/Annots") or ()):
            if pasted_picture(annotation):
                pictures.append(
                    ArchitectPicture(
                        annotation_index=index,
                        box=_page_box(annotation, left_edge, top_edge),
                        reason=(
                            "a drawing pasted as a picture (an image, no text or line-work): code "
                            "cannot read it, a person does"
                        ),
                    )
                )
                continue
            if not carries_drawing(annotation):
                continue
            found[index] = (_page_box(annotation, left_edge, top_edge), _paste_factor(annotation))
    return found, tuple(pictures)


def _pattern_fill(colour: object) -> bool:
    """Whether a fill colour is a pattern (its name, or a colour with a pattern's name) rather than
    plain components."""
    if colour is None or isinstance(colour, (int, float)):
        return False
    if isinstance(colour, (tuple, list)):
        return any(not isinstance(part, (int, float)) for part in colour)
    return True


def _pattern_fills(page: Any) -> tuple[Box, ...]:
    """The boxes of every path filled with a pattern: hatching drawn as a fill, not as strokes."""
    return tuple(
        _box(obj)
        for obj in (*page.rects, *page.curves)
        if obj.get("fill") and _pattern_fill(obj.get("non_stroking_color"))
    )


def _median(values: Sequence[Decimal]) -> Decimal:
    return Decimal(statistics.median(values))


def _as_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


@dataclass
class _Draft:
    """A span while it is being read: everything mutable until it is frozen into `ArchitectSpan`."""

    x0: Decimal
    x1: Decimal
    label: PrintedDimension | None = None
    held: list[str] | None = None
    witness_inches: Decimal | None = None
    on_outline: bool | None = None
    outline_reason: str = ""

    def hold(self, reason: str) -> None:
        if self.held is None:
            self.held = []
        if reason not in self.held:
            self.held.append(reason)

    @property
    def value(self) -> Fraction | None:
        return None if self.label is None else self.label.reading.inches


@dataclass(frozen=True, slots=True)
class _Line:
    """One row of the drawing as the architect reader takes it: its line and its ticks."""

    y: Decimal
    ticks: tuple[Decimal, ...]
    source: TickSource

    @property
    def x0(self) -> Decimal:
        return self.ticks[0]

    @property
    def x1(self) -> Decimal:
        return self.ticks[-1]


def _paired(ticks: Sequence[Decimal], within: Decimal) -> tuple[Decimal, ...]:
    """Ticks with every pair closer than `within` made one, at the pair's middle."""
    merged: list[Decimal] = []
    index = 0
    while index < len(ticks):
        if index + 1 < len(ticks) and ticks[index + 1] - ticks[index] < within:
            merged.append((ticks[index] + ticks[index + 1]) / _TWO)
            index += 2
        else:
            merged.append(ticks[index])
            index += 1
    return tuple(merged)


def _rows_in(
    rows: Sequence[CountertopRowCandidate], view: Box, settings: ArchitectSettings
) -> list[_Line]:
    """The rows lying inside one drawing, each once, with doubled ticks made one."""
    seen: set[tuple[Decimal, tuple[Decimal, ...]]] = set()
    kept: list[_Line] = []
    for row in sorted(rows, key=lambda row: (row.y, row.x0)):
        if row.tick_source is TickSource.GAP:
            # Ticks from breaks in a line are table rules, hatching, or the edges of the white
            # mask a CAD program sets behind a label — never trusted for an architect's span.
            continue
        if not (view.x0 <= row.x0 and row.x1 <= view.x1 and view.top <= row.y <= view.bottom):
            continue
        ticks = _paired(row.ticks, settings.tick_pair_pt)
        if len(ticks) < 2:
            continue
        key = (row.y, ticks)
        if key in seen:
            continue
        seen.add(key)
        kept.append(_Line(y=row.y, ticks=ticks, source=row.tick_source))
    return kept


def _assign(
    rows: Sequence[_Line],
    labels: Sequence[PrintedDimension],
    settings: ArchitectSettings,
) -> dict[tuple[int, int], list[PrintedDimension]]:
    """Each upright label to the one span of the nearest row it is printed on."""
    placed: dict[tuple[int, int], list[PrintedDimension]] = {}
    for label in labels:
        if label.orientation is not TextOrientation.UPRIGHT:
            continue
        centre_x = (label.box.x0 + label.box.x1) / _TWO
        centre_y = (label.box.top + label.box.bottom) / _TWO
        options: list[tuple[Decimal, int, int]] = []
        for row_number, row in enumerate(rows):
            distance = abs(centre_y - row.y)
            if distance > settings.label_reach_pt:
                continue
            if not (
                row.x0 - settings.label_slack_pt <= centre_x <= row.x1 + settings.label_slack_pt
            ):
                continue
            span = next(
                (
                    index
                    for index, (left, right) in enumerate(
                        zip(row.ticks, row.ticks[1:], strict=False)
                    )
                    if left <= centre_x < right
                ),
                0 if centre_x < row.x0 else len(row.ticks) - 2,
            )
            options.append((distance, row_number, span))
        if not options:
            continue
        nearest = min(distance for distance, _row, _span in options)
        for distance, row_number, span in options:
            if distance == nearest:
                placed.setdefault((row_number, span), []).append(label)
    return placed


def _absolute_scale(note: tuple[str, Fraction] | None, paste: Decimal | None) -> Decimal | None:
    """Witness (ii): page points per real inch from the printed scale and the paste factor."""
    if note is None or paste is None:
        return None
    return _POINTS_PER_INCH * _as_decimal(note[1]) * paste


def _witness(
    drafts: Sequence[_Draft],
    absolute: Decimal | None,
    settings: ArchitectSettings,
    *,
    on_page: bool = False,
) -> tuple[Decimal | None, Decimal | None, str]:
    """Judgment (B) for every labelled span of one drawing; returns the drawing's scale.

    `on_page`: the drawing is the page's own content (#1163), so witness (ii) is the printed scale
    alone, and labels agreeing with each other but not with it say the sheet was printed at another
    size (a reduced print), which holds every value with that reason.
    """
    absolute_name = "the printed scale" if on_page else "the printed scale and paste factor"
    labelled = [draft for draft in drafts if draft.value is not None and draft.x1 > draft.x0]
    ratios = {
        id(draft): (draft.x1 - draft.x0) / _as_decimal(draft.value)
        for draft in labelled
        if draft.value
    }
    median = _median(list(ratios.values())) if ratios else None
    for draft in labelled:
        value = draft.value
        assert value is not None
        printed = _as_decimal(value)
        drawn = draft.x1 - draft.x0
        others = [ratio for key, ratio in ratios.items() if key != id(draft)]
        witnesses: list[tuple[str, Decimal]] = []
        if len(others) >= settings.leave_one_out_minimum:
            witnesses.append(("the drawing's other labels", _median(others)))
        if absolute is not None:
            witnesses.append((absolute_name, absolute))
        if not witnesses:
            draft.hold(
                "no scale witness: the drawing has too few other labels and no usable scale note"
            )
            continue
        allowed = max(settings.witness_band * printed, settings.witness_floor_in)
        for name, points_per_inch in witnesses:
            expected = drawn / points_per_inch
            if draft.witness_inches is None:
                draft.witness_inches = expected
            if abs(expected - printed) > allowed:
                draft.hold(
                    f"the drawn length through {name} is {expected:.2f} in, the label says "
                    f"{printed.normalize()} in"
                )
    agreeing = (
        []
        if median is None
        else [
            ratio
            for ratio in ratios.values()
            if abs(ratio - median) <= settings.scale_agreement * median
        ]
    )
    if (
        on_page
        and absolute is not None
        and len(agreeing) >= 3
        and 2 * len(agreeing) > len(ratios)
        and abs(_median(agreeing) - absolute) > settings.scale_agreement * absolute
    ):
        # Most labels agree with each other, and not with the printed scale: the page is not at
        # its stated size (a reduced print), so neither scale can be trusted for a value.
        labels_scale = _median(agreeing)
        printed_at = (labels_scale / absolute * 100).quantize(Decimal("0.1"))
        reason = (
            f"the labels agree with each other at {labels_scale:.4f} pt per inch but the printed "
            f"scale gives {absolute:.4f}: the sheet appears printed at {printed_at}% of its stated "
            "scale, so no scale is given and every value is held"
        )
        for draft in labelled:
            draft.hold(f"the sheet appears printed at {printed_at}% of its stated scale")
        return median, None, reason
    witnessed = [ratios[id(draft)] for draft in labelled if not draft.held]
    if len(witnessed) >= 3:
        chosen = _median(witnessed)
        if absolute is not None and abs(chosen - absolute) > settings.scale_agreement * absolute:
            return (
                median,
                None,
                (
                    f"the labels give {chosen:.4f} pt per inch and the scale note {absolute:.4f}: "
                    "they disagree, so no scale is given"
                ),
            )
        return median, chosen, f"{len(witnessed)} witnessed labels give {chosen:.4f} pt per inch"
    if absolute is not None:
        return (
            median,
            absolute,
            f"{absolute_name} gives {absolute:.4f} pt per inch",
        )
    return median, None, "no scale: too few witnessed labels and no usable scale note"


class _ZeroOrigin:
    """A pdfplumber page's objects moved so the visible page's top-left corner is (0, 0) (#1163).

    pdfplumber keeps a page's own coordinates: on a page whose media box or crop box does not start
    at (0, 0) — a sheet cut out of a larger set — every `x` is offset by the visible box's left edge
    and every `top` by its top, and the row finder (which keeps its strips inside `0..width` and
    `0..height`) refused the page with "a Box's x1 must not be left of x0". **One origin for
    everything: the crop box's top-left corner**, the visible page, which is also where the pasted
    drawings' rectangles are measured from (`_page_box`). The page's objects are moved to that
    frame here, those lying wholly outside the visible page are left out and those reaching past it
    held to it (nobody sees what is outside), the page is read there, and the result moved back
    (`_moved`). `exclude` (in the moved frame) leaves
    out every object whose middle lies in one of its boxes.
    """

    def __init__(self, page: Any, exclude: Sequence[Box] = ()) -> None:
        x0, top, x1, bottom = (_decimal(value) for value in page.cropbox)
        self.dx = x0
        self.dtop = top
        self.width = x1 - x0
        self.height = bottom - top
        self._exclude = tuple(exclude)
        self.lines = self._kept(page.lines)
        self.rects = self._kept(page.rects)
        self.curves = self._kept(page.curves)
        self.chars = self._kept(page.chars)

    def _kept(self, objects: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        kept: list[dict[str, Any]] = []
        for obj in objects:
            moved = self._move(obj)
            box = _box(moved)
            if box.x1 < 0 or box.x0 > self.width or box.bottom < 0 or box.top > self.height:
                continue
            middle_x, middle_y = box.centre_x, box.centre_y
            if any(
                area.x0 <= middle_x <= area.x1 and area.top <= middle_y <= area.bottom
                for area in self._exclude
            ):
                continue
            kept.append(self._clamped(moved, box))
        return kept

    def _clamped(self, obj: dict[str, Any], box: Box) -> dict[str, Any]:
        """An object reaching past the visible page, held to it: what lies outside is not seen (a
        background rectangle larger than a cropped sheet would otherwise give rows off the page)."""
        if box.x0 >= 0 and box.top >= 0 and box.x1 <= self.width and box.bottom <= self.height:
            return obj

        def x(value: object) -> Decimal:
            return min(max(_decimal(value), Decimal(0)), self.width)

        def top(value: object) -> Decimal:
            return min(max(_decimal(value), Decimal(0)), self.height)

        clamped = dict(obj)
        clamped["x0"], clamped["x1"] = x(obj["x0"]), x(obj["x1"])
        clamped["top"], clamped["bottom"] = top(obj["top"]), top(obj["bottom"])
        if obj.get("pts"):
            clamped["pts"] = [(x(px), top(pt)) for px, pt in obj["pts"]]
        return clamped

    def _move(self, obj: dict[str, Any]) -> dict[str, Any]:
        if not self.dx and not self.dtop:
            return obj
        moved = dict(obj)
        for key in ("x0", "x1"):
            if key in moved:
                moved[key] = _decimal(moved[key]) - self.dx
        for key in ("top", "bottom"):
            if key in moved:
                moved[key] = _decimal(moved[key]) - self.dtop
        if moved.get("pts"):
            moved["pts"] = [
                (_decimal(x) - self.dx, _decimal(top) - self.dtop) for x, top in moved["pts"]
            ]
        return moved


def _moved_box(box: Box, dx: Decimal, dtop: Decimal) -> Box:
    return Box(box.x0 + dx, box.top + dtop, box.x1 + dx, box.bottom + dtop)


def _moved(page: ArchitectPage, dx: Decimal, dtop: Decimal) -> ArchitectPage:
    """The reading back in pdfplumber's own frame, the frame every other reading of the page uses."""
    if not dx and not dtop:
        return page
    return replace(
        page,
        views=tuple(replace(view, box=_moved_box(view.box, dx, dtop)) for view in page.views),
        rows=tuple(
            replace(
                row,
                y=row.y + dtop,
                ticks=tuple(tick + dx for tick in row.ticks),
                spans=tuple(
                    replace(
                        span,
                        x0_pt=span.x0_pt + dx,
                        x1_pt=span.x1_pt + dx,
                        label_box=(
                            None if span.label_box is None else _moved_box(span.label_box, dx, dtop)
                        ),
                    )
                    for span in row.spans
                ),
            )
            for row in page.rows
        ),
        pictures=tuple(
            replace(picture, box=_moved_box(picture.box, dx, dtop)) for picture in page.pictures
        ),
    )


@dataclass(frozen=True, slots=True)
class _Page:
    """Everything read once from a page, in the zero-origin frame, for reading its drawings."""

    chars: tuple[TextChar, ...]
    ink: PageInk
    every_row: tuple[CountertopRowCandidate, ...]
    work: LineWork
    fills: tuple[Box, ...]
    place: Callable[[Decimal, Decimal], StoredPoint]
    pixels: Callable[[Box], tuple[int, int, int, int]]
    dx: Decimal
    dtop: Decimal


def _open_page(
    data: bytes,
    page_index: int,
    *,
    drawing_boxes: Sequence[Box],
    rows: RowSettings,
    settings: ArchitectSettings,
    dpi: int,
    exclude: Sequence[Box] = (),
) -> _Page:
    """The page's black and grey text and ink, its rows and line-work, and where things land;
    `exclude` leaves out what lies in those boxes (pasted drawings, read on their own)."""
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            page = document.pages[page_index]
            transform, height = page_frame(page, dpi)
            moved = _ZeroOrigin(page, exclude)
            ink = ink_from_page(moved, drawing_boxes=tuple(drawing_boxes))
            pixel = pixel_placement(page, dpi)
            fills = _pattern_fills(moved)
            chars = tuple(
                TextChar(
                    text=str(char["text"]),
                    box=_box(char),
                    orientation=orientation_of(char.get("matrix"), char.get("upright")),
                )
                for char in moved.chars
                if drawing_ink(char) and str(char.get("text", "")).strip()
            )
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index}'s drawings could not be read: {error}") from error
    dx, dtop = moved.dx, moved.dtop

    def place(x: Decimal, top: Decimal) -> StoredPoint:
        return transform.to_stored(transform.to_image(PdfPoint(x=x + dx, y=height - (top + dtop))))

    def pixels(box: Box) -> tuple[int, int, int, int]:
        first = pixel(box.x0 + dx, box.top + dtop)
        second = pixel(box.x1 + dx, box.bottom + dtop)
        return (
            min(first.x, second.x),
            min(first.y, second.y),
            max(first.x, second.x),
            max(first.y, second.y),
        )

    all_rows = build_rows(ink, rows, place=place)
    return _Page(
        chars=chars,
        ink=ink,
        every_row=(*all_rows.candidates, *all_rows.rejected),
        work=line_work(ink, settings.outline),
        fills=fills,
        place=place,
        pixels=pixels,
        dx=dx,
        dtop=dtop,
    )


#: One drawing still being read: where its view sits in the list, its rows and their spans.
_Pending = tuple[int, list[_Line], list[list[_Draft]]]


def _read_drawing(
    index: int,
    box: Box,
    paste: Decimal | None,
    judge: Callable[[ContentJudgment], ViewJudgment],
    stored_points: tuple[tuple[Decimal, Decimal], ...],
    page: _Page,
    settings: ArchitectSettings,
    *,
    always_read: bool = False,
    source: str = "pasted",
    title: str | None = None,
    bubble: str | None = None,
) -> tuple[ArchitectView, tuple[list[_Line], list[list[_Draft]]] | None]:
    """Read one drawing: its role's two judgments, its scale, and every span of its rows."""
    inside = [
        char
        for char in page.chars
        if box.x0 <= (char.box.x0 + char.box.x1) / _TWO <= box.x1
        and box.top <= (char.box.top + char.box.bottom) / _TWO <= box.bottom
    ]
    printed = find_printed(inside, settings.text)
    content = judge_content(count_labels(printed.dimensions, printed.phrases, printed.scales))
    judgment = judge(content)
    read = always_read or Role.ARCH in (judgment.heading_role, content.role, content.labels_lean)
    architectural = [note for note in printed.scales if note.kind.value == "architectural"]
    note = (
        (architectural[0].text, architectural[0].paper_per_real)
        if len(architectural) == 1 and len(printed.scales) == 1
        else None
    )
    absolute = _absolute_scale(note, paste)
    if not read:
        return (
            ArchitectView(
                annotation_index=index,
                judgment=judgment,
                box=box,
                stored_points=stored_points,
                read=False,
                scale_note=None if note is None else note[0],
                scale_paper_per_real=None if note is None else note[1],
                paste_factor=paste,
                absolute_points_per_inch=absolute,
                median_points_per_inch=None,
                points_per_inch=None,
                scale_reason="not read: nothing points to the architect",
                source=source,
                title=title,
                bubble=bubble,
            ),
            None,
        )
    view_rows = _rows_in(page.every_row, box, settings)
    placed = _assign(view_rows, printed.dimensions, settings)
    drafts_by_row: list[list[_Draft]] = []
    for row_number, row in enumerate(view_rows):
        drafts: list[_Draft] = []
        for span, (left, right) in enumerate(zip(row.ticks, row.ticks[1:], strict=False)):
            draft = _Draft(x0=left, x1=right)
            labels = placed.get((row_number, span), [])
            if len(labels) == 1:
                draft.label = labels[0]
                if labels[0].reading.held_reason:
                    draft.hold(labels[0].reading.held_reason)
            elif len(labels) > 1:
                draft.label = min(
                    labels,
                    key=lambda label: abs((label.box.top + label.box.bottom) / _TWO - row.y),
                )
                draft.hold("more than one label is printed on this span")
            else:
                draft.hold("nothing is printed on this span")
            drafts.append(draft)
        drafts_by_row.append(drafts)
    median, chosen, scale_reason = _witness(
        [
            draft
            for drafts in drafts_by_row
            for draft in drafts
            if draft.label is not None and not draft.held
        ],
        absolute,
        settings,
        on_page=source == "content",
    )
    outline_scale = chosen if chosen is not None else absolute
    # The drawing's labelled dimension lines: a row stacked between a tick and the drawing is
    # not an outline of the drawing.
    dimension_lines = tuple(
        (row.y, row.x0, row.x1)
        for row, drafts in zip(view_rows, drafts_by_row, strict=True)
        if any(draft.label is not None for draft in drafts)
    )
    hatched = hatched_regions(
        slanted_strokes(page.ink, box, settings.outline),
        page.fills,
        view=box,
        settings=settings.outline,
    )
    for row, drafts in zip(view_rows, drafts_by_row, strict=True):
        for draft in drafts:
            ends = [
                end_witness(
                    x,
                    row.y,
                    view=box,
                    work=page.work,
                    centre_marks=printed.centre_marks,
                    points_per_inch=outline_scale,
                    settings=settings.outline,
                    dimension_lines=dimension_lines,
                    hatched=hatched,
                )
                for x in (draft.x0, draft.x1)
            ]
            draft.on_outline = span_on_outline(ends[0], ends[1])
            draft.outline_reason = f"left: {ends[0].reason}; right: {ends[1].reason}"
            if draft.on_outline and not one_object(
                ends[0],
                ends[1],
                draft.x0,
                draft.x1,
                work=page.work,
                view=box,
                dimension_lines=dimension_lines,
                settings=settings.outline,
            ):
                draft.on_outline = False
                draft.outline_reason = (
                    "measures a clearance between different things: both ends are edges, but "
                    "no outline runs from one to the other as one object's top and bottom; "
                    + draft.outline_reason
                )
    return (
        ArchitectView(
            annotation_index=index,
            judgment=judgment,
            box=box,
            stored_points=stored_points,
            read=True,
            scale_note=None if note is None else note[0],
            scale_paper_per_real=None if note is None else note[1],
            paste_factor=paste,
            absolute_points_per_inch=absolute,
            median_points_per_inch=median,
            points_per_inch=chosen,
            scale_reason=scale_reason,
            source=source,
            title=title,
            bubble=bubble,
        ),
        (view_rows, drafts_by_row),
    )


def _rows_of(
    views: Sequence[ArchitectView],
    pending: Sequence[_Pending],
    page: _Page,
    settings: ArchitectSettings,
) -> list[tuple[Decimal, Decimal, ArchitectRow]]:
    """Every read drawing's labelled rows, unranked, with where they sit; a span in a drawing whose
    role is not the architect's by code is held with the reason."""
    rows: list[tuple[Decimal, Decimal, ArchitectRow]] = []
    for position, view_rows, drafts_by_row in pending:
        view = views[position]
        judgment = view.judgment
        if judgment.agreed is not Role.ARCH:
            for drafts in drafts_by_row:
                for draft in drafts:
                    draft.hold(f"this drawing's role is not decided by code: {judgment.reason}")
        for row, drafts in zip(view_rows, drafts_by_row, strict=True):
            if not any(draft.label is not None for draft in drafts):
                continue
            spans = tuple(
                ArchitectSpan(
                    index=index,
                    x0_pt=draft.x0,
                    x1_pt=draft.x1,
                    text=None if draft.label is None else draft.label.reading.text,
                    qualifiers=(
                        frozenset() if draft.label is None else draft.label.reading.qualifiers
                    ),
                    printed_inches=(
                        None if draft.label is None else draft.label.reading.printed_inches
                    ),
                    inches=draft.value if not draft.held else None,
                    held_reason=None if not draft.held else "; ".join(draft.held),
                    witness_inches=draft.witness_inches,
                    on_outline=draft.on_outline,
                    outline_reason=draft.outline_reason,
                    label_box=None if draft.label is None else draft.label.box,
                    label_pixels=None if draft.label is None else page.pixels(draft.label.box),
                    span_pixels=page.pixels(
                        Box(
                            draft.x0,
                            row.y - settings.label_reach_pt,
                            draft.x1,
                            row.y + settings.label_reach_pt,
                        )
                    ),
                )
                for index, draft in enumerate(drafts)
            )
            rows.append(
                (
                    row.y,
                    row.x0,
                    ArchitectRow(
                        view_annotation_index=view.annotation_index,
                        rank=0,
                        y=row.y,
                        ticks=row.ticks,
                        tick_source=row.source,
                        spans=spans,
                        points_per_inch=view.points_per_inch,
                        view_source=view.source,
                    ),
                )
            )
    return rows


def _ranked(rows: Sequence[tuple[Decimal, Decimal, ArchitectRow]]) -> tuple[ArchitectRow, ...]:
    """The page's rows ranked top to bottom (then left to right): `arch-row:<rank>`."""
    ordered = sorted(rows, key=lambda item: (item[0], item[1]))
    return tuple(replace(row, rank=rank) for rank, (_y, _x, row) in enumerate(ordered, start=1))


@dataclass(frozen=True, slots=True)
class _Reading:
    """One page's drawings read on one opened page, rows not yet ranked."""

    views: list[ArchitectView]
    rows: list[tuple[Decimal, Decimal, ArchitectRow]]
    notes: list[PageNote]
    page: _Page


def _read_page_content(
    data: bytes,
    page_index: int,
    *,
    settings: ArchitectSettings,
    dpi: int,
    exclude: Sequence[Box] = (),
    hold_reason: str | None = None,
) -> _Reading:
    """The architect's views drawn as the page's own content, on the architect's own file (#1163).

    Views come from what is printed (`page_views.survey_page_views`): each is read inside its extent
    exactly as a pasted drawing is, at the page's own scale (a paste factor of exactly 1: nothing is
    pasted, so the printed scale note holds on the page). The role is the document's kind and the
    view's content agreeing (`views.judge_by_document`). A view not clearly apart from another is
    read and every value in it held, with the reason. `exclude`: pasted drawings' boxes, read on
    their own and never twice. `hold_reason`: no view here gets a role, each is held with it.
    """
    page = _open_page(
        data,
        page_index,
        drawing_boxes=(),
        rows=settings.content_rows,
        settings=settings,
        dpi=dpi,
        exclude=exclude,
    )
    survey = survey_page_views(page.chars, page.ink, text=settings.text, settings=settings.views)
    views: list[ArchitectView] = []
    pending: list[_Pending] = []
    for found_view in survey.views:
        extent = found_view.extent
        corners = (
            page.place(extent.x0, extent.top),
            page.place(extent.x1, extent.top),
            page.place(extent.x1, extent.bottom),
            page.place(extent.x0, extent.bottom),
        )

        def judge(content: ContentJudgment, number: int = found_view.number) -> ViewJudgment:
            return judge_by_document(number, content)

        view, drafted = _read_drawing(
            found_view.number,
            extent,
            Decimal(1),
            judge,
            tuple((point.x, point.y) for point in corners),
            page,
            settings,
            always_read=True,
            source="content",
            title=found_view.title,
            bubble=found_view.bubble,
        )
        if not found_view.separated:
            judgment = replace(
                view.judgment,
                agreed=None,
                by_document_kind=False,
                reason=f"this view is not clearly apart from another: {found_view.reason}",
            )
            view = replace(view, judgment=judgment, separated=False)
        elif hold_reason is not None:
            view = replace(
                view,
                judgment=replace(
                    view.judgment, agreed=None, by_document_kind=False, reason=hold_reason
                ),
            )
        if drafted is not None:
            pending.append((len(views), *drafted))
        views.append(view)
    return _Reading(views, _rows_of(views, pending, page, settings), list(survey.notes), page)


def _read_pasted(
    data: bytes,
    page_index: int,
    *,
    settings: ArchitectSettings,
    dpi: int,
    on_architect_file: bool,
) -> tuple[_Reading, tuple[ArchitectPicture, ...]]:
    """The drawings pasted on the page, each role from its heading and its content (#1052).

    On the architect's own file (#1163) a stamp is a pasted drawing only when it holds a dimension
    label (feet and inches, inches, or millimetres with inches) or a scale note: an approval stamp,
    a seal or a reviewer's mark holds none, is left to the page's content reader, and is noted. The role rule is the same everywhere: a combined set uploaded in the
    architect's slot as other bytes is still decided by its headings and content, never by the
    slot.
    """
    layers = read_markup_layer(data, page_index, document_version_id=_UNSTORED, dpi=dpi)
    stamps, pictures = _stamps(data, page_index)
    proposals = {
        proposal.annotation_index: proposal
        for proposal in propose_panel_roles(layers.vendor_stamps, layers.markup)
    }
    drawings = [
        stamp
        for stamp in layers.vendor_stamps
        if stamp.annotation_index in stamps
        and not any(
            other != stamp.annotation_index
            and _inside(stamps[stamp.annotation_index][0], box)
            and stamps[stamp.annotation_index][0] != box
            for other, (box, _factor) in stamps.items()
        )
    ]
    page = _open_page(
        stamps_only(data, page_index),
        page_index,
        drawing_boxes=tuple(stamps[stamp.annotation_index][0] for stamp in drawings),
        rows=settings.rows,
        settings=settings,
        dpi=dpi,
    )
    notes: list[PageNote] = []
    if on_architect_file:
        holding: list[Any] = []
        for stamp in drawings:
            box = stamps[stamp.annotation_index][0]
            inside = [
                char
                for char in page.chars
                if box.x0 <= char.box.centre_x <= box.x1
                and box.top <= char.box.centre_y <= box.bottom
            ]
            printed = find_printed(inside, settings.text)
            counts = count_labels(printed.dimensions, printed.phrases, printed.scales)
            if printed.scales or counts.feet_and_inches + counts.vendor_style + counts.small_inches:
                holding.append(stamp)
            else:
                notes.append(
                    PageNote(
                        f"a stamp pasted on the page (annotation {stamp.annotation_index}) holds "
                        "no dimension label and no scale (an approval stamp, a seal or a mark): "
                        "not read as a drawing",
                        STAMP_NOT_DRAWING,
                    )
                )
        drawings = holding
    views: list[ArchitectView] = []
    pending: list[_Pending] = []
    for stamp in drawings:
        box, paste = stamps[stamp.annotation_index]
        proposal = proposals[stamp.annotation_index]

        def judge(content: ContentJudgment, proposal: PanelRoleProposal = proposal) -> ViewJudgment:
            return judge_view(proposal, content)

        view, drafted = _read_drawing(
            stamp.annotation_index,
            box,
            paste,
            judge,
            tuple((point.x, point.y) for point in stamp.extent.points),
            page,
            settings,
        )
        if drafted is not None:
            pending.append((len(views), *drafted))
        views.append(view)
    if not printed_headings(layers.markup):
        # A page printing no heading at all: both roles from the content of both drawings, only
        # when that is clear on both sides (`views.decide_without_headings`).
        drawn = {
            view.annotation_index: drawn_architectural_scale(
                view.points_per_inch, view.paste_factor, settings.scale_agreement
            )
            for view in views
            if view.read and view.scale_note is None and view.absolute_points_per_inch is None
        }
        decided = decide_without_headings([view.judgment for view in views], drawn)
        views = [
            replace(view, judgment=judgment) for view, judgment in zip(views, decided, strict=True)
        ]
    return _Reading(views, _rows_of(views, pending, page, settings), notes, page), pictures


def read_architect_page(
    data: bytes,
    page_index: int,
    *,
    settings: ArchitectSettings,
    dpi: int,
    on_architect_file: bool = False,
) -> ArchitectPage:
    """The architect's views, rows and spans on one page of `data`, every decision with its reason.

    Pasted drawings are read everywhere, each role from its heading and its content
    (`views.judge_view`, `decide_without_headings`). `on_architect_file` says the page belongs to
    a file uploaded as the architect's drawings, apart from the vendor's (#1163): there the page's
    own content is read too, view by view (`_read_page_content`), outside the pasted drawings, each
    view's role from the document's kind and its content agreeing (`views.judge_by_document`); a
    stamp holding no dimension label is not a drawing there; and every page says why nothing was
    read when nothing was (`ArchitectPage.notes`).

    Raises `UnreadablePdf` when the page cannot be read; a page with no architect's drawing returns
    no rows, never a refusal.
    """
    pasted, pictures = _read_pasted(
        data, page_index, settings=settings, dpi=dpi, on_architect_file=on_architect_file
    )
    if not on_architect_file:
        return _moved(
            ArchitectPage(
                page_index=page_index,
                views=tuple(pasted.views),
                rows=_ranked(pasted.rows),
                pictures=pictures,
            ),
            pasted.page.dx,
            pasted.page.dtop,
        )
    # A page whose pasted drawing is decided by its printed heading is a combined sheet (a combined
    # set uploaded again in the architect's slot as other bytes): its own drawings may be the
    # vendor's, so the upload slot gives them no role (#1163 review).
    combined = [
        view
        for view in pasted.views
        if view.judgment.agreed is not None and view.judgment.heading_role is not None
    ]
    content = _read_page_content(
        data,
        page_index,
        settings=settings,
        dpi=dpi,
        exclude=tuple(view.box for view in pasted.views),
        hold_reason=(
            None
            if not combined
            else "this page also holds a pasted drawing decided by its printed heading (a "
            "combined sheet), so the upload slot gives the page's own drawings no role; a person "
            "decides"
        ),
    )
    views = (*pasted.views, *content.views)
    notes = [*pasted.notes, *content.notes]
    if pasted.views and not content.views:
        # The content reader's "no view found" is no news on a page whose drawings are pasted.
        notes = [note for note in notes if note.kind != NO_VIEW_FOUND]
    if not views and not any(note.kind == NO_VIEW_FOUND for note in notes):
        notes.append(
            PageNote("no view found: nothing on this page is a drawing view", NO_VIEW_FOUND)
        )
    return _moved(
        ArchitectPage(
            page_index=page_index,
            views=views,
            rows=_ranked([*pasted.rows, *content.rows]),
            pictures=pictures,
            notes=tuple(notes),
        ),
        content.page.dx,
        content.page.dtop,
    )
