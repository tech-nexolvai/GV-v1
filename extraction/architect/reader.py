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

**What it never does.** It never reads coloured ink (GV's markup) as text, a tick or an edge; never
rounds; never fills in a value it could not read; and never decides anything it was not given two
judgments for. A held span's inches are `None`: whoever uses the result cannot take its number by
mistake.

Pure: PDF bytes in, plain values out, no database. Source: issue #1052 · Verification:
`tests/extraction/architect/test_reader.py`
"""

from __future__ import annotations

import io
import statistics
from collections.abc import Sequence
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
    OutlineSettings,
    end_witness,
    hatched_regions,
    line_work,
    one_object,
    slanted_strokes,
    span_on_outline,
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
    Role,
    ViewJudgment,
    count_labels,
    decide_without_headings,
    drawn_architectural_scale,
    judge_content,
    judge_view,
)
from extraction.geometry.rows import (
    MEASURED_SETTINGS,
    Box,
    CountertopRowCandidate,
    RowSettings,
    TickSource,
    build_rows,
)
from extraction.panels import printed_headings, propose_panel_roles
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
_POINTS_PER_INCH: Final = Decimal(72)

#: A random but fixed id: the annotation reader asks for a document version only to label what it
#: returns, and nothing here is stored.
_UNSTORED: Final = UUID("00000000-0000-0000-0000-000000001052")


@dataclass(frozen=True, slots=True)
class ArchitectSettings:
    """Every threshold the architect reader uses. No defaults: `MEASURED_ARCHITECT_SETTINGS` is the
    one set measured on the client's two sets (#1052)."""

    rows: RowSettings
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


@dataclass(frozen=True, slots=True)
class ArchitectView:
    """One pasted drawing on the page, both judgments of its role, and its scale."""

    annotation_index: int
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


#: Measured on both client sets (#1052, report `1052-report.md`): the row finder's E1 thresholds,
#: the text and outline lengths below, and the scale band that accepted every right value on both
#: sets and refuses a value off by one bay.
MEASURED_ARCHITECT_SETTINGS = ArchitectSettings(
    # The architect's tick slashes are drawn larger than the vendor's (over 4 pt on the client's
    # pasted drawings): the vendor's E1 thresholds with a larger slash. The vendor's rows are never built
    # with these.
    rows=replace(MEASURED_SETTINGS, slash_maximum_pt=Decimal(6)),
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
) -> tuple[Decimal | None, Decimal | None, str]:
    """Judgment (B) for every labelled span of one drawing; returns the drawing's scale."""
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
            witnesses.append(("the printed scale and paste factor", absolute))
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
            f"the printed scale and paste factor give {absolute:.4f} pt per inch",
        )
    return median, None, "no scale: too few witnessed labels and no usable scale note"


def read_architect_page(
    data: bytes,
    page_index: int,
    *,
    settings: ArchitectSettings,
    dpi: int,
) -> ArchitectPage:
    """The architect's views, rows and spans on one page of `data`, every decision with its reason.

    Raises `UnreadablePdf` when the page cannot be read; a page with no architect's drawing returns
    no rows, never a refusal.
    """
    # A page with pasted drawings and no notes is "unreadable" to the markup reader (it has no
    # markup), and still has its drawings: only the stamps and headings are used here.
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
    flattened = stamps_only(data, page_index)
    try:
        with pdfplumber.open(io.BytesIO(flattened)) as document:
            page = document.pages[page_index]
            transform, height = page_frame(page, dpi)
            ink = ink_from_page(
                page, drawing_boxes=tuple(stamps[stamp.annotation_index][0] for stamp in drawings)
            )
            pixel = pixel_placement(page, dpi)
            fills = _pattern_fills(page)
            chars = [
                TextChar(
                    text=str(char["text"]),
                    box=_box(char),
                    orientation=orientation_of(char.get("matrix"), char.get("upright")),
                )
                for char in page.chars
                if drawing_ink(char) and str(char.get("text", "")).strip()
            ]
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be read: {error}"
        ) from error

    def place(x: Decimal, top: Decimal) -> StoredPoint:
        return transform.to_stored(transform.to_image(PdfPoint(x=x, y=height - top)))

    def pixels(box: Box) -> tuple[int, int, int, int]:
        first, second = pixel(box.x0, box.top), pixel(box.x1, box.bottom)
        return (
            min(first.x, second.x),
            min(first.y, second.y),
            max(first.x, second.x),
            max(first.y, second.y),
        )

    all_rows = build_rows(ink, settings.rows, place=place)
    every_row = (*all_rows.candidates, *all_rows.rejected)
    work = line_work(ink, settings.outline)
    views: list[ArchitectView] = []
    rows: list[tuple[Decimal, Decimal, ArchitectRow]] = []
    pending: list[tuple[int, list[_Line], list[list[_Draft]]]] = []
    for stamp in drawings:
        box, paste = stamps[stamp.annotation_index]
        inside = [
            char
            for char in chars
            if box.x0 <= (char.box.x0 + char.box.x1) / _TWO <= box.x1
            and box.top <= (char.box.top + char.box.bottom) / _TWO <= box.bottom
        ]
        printed = find_printed(inside, settings.text)
        content = judge_content(count_labels(printed.dimensions, printed.phrases, printed.scales))
        proposal = proposals[stamp.annotation_index]
        judgment = judge_view(proposal, content)
        read = Role.ARCH in (judgment.heading_role, content.role, content.labels_lean)
        architectural = [note for note in printed.scales if note.kind.value == "architectural"]
        note = (
            (architectural[0].text, architectural[0].paper_per_real)
            if len(architectural) == 1 and len(printed.scales) == 1
            else None
        )
        absolute = _absolute_scale(note, paste)
        stored_points = tuple((point.x, point.y) for point in stamp.extent.points)
        if not read:
            views.append(
                ArchitectView(
                    annotation_index=stamp.annotation_index,
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
                )
            )
            continue
        view_rows = _rows_in(every_row, box, settings)
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
            slanted_strokes(ink, box, settings.outline), fills, view=box, settings=settings.outline
        )
        for row, drafts in zip(view_rows, drafts_by_row, strict=True):
            for draft in drafts:
                ends = [
                    end_witness(
                        x,
                        row.y,
                        view=box,
                        work=work,
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
                    work=work,
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
        pending.append((len(views), view_rows, drafts_by_row))
        views.append(
            ArchitectView(
                annotation_index=stamp.annotation_index,
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
            )
        )
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
                    label_pixels=None if draft.label is None else pixels(draft.label.box),
                    span_pixels=pixels(
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
                    ),
                )
            )
    ordered = sorted(rows, key=lambda item: (item[0], item[1]))
    ranked = tuple(
        ArchitectRow(
            view_annotation_index=row.view_annotation_index,
            rank=rank,
            y=row.y,
            ticks=row.ticks,
            tick_source=row.tick_source,
            spans=row.spans,
            points_per_inch=row.points_per_inch,
        )
        for rank, (_y, _x, row) in enumerate(ordered, start=1)
    )
    return ArchitectPage(page_index=page_index, views=tuple(views), rows=ranked, pictures=pictures)
