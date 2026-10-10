"""The signed PDF's countertop picture is the screen's picture (#1140).

The fixtures are the synthetic rows of the frontend's strip tests
(``frontend/main/tests/vitest/countertop-strip.test.tsx``); the expected geometry is worked out
here from the shared spec (vault "V1 backend", section 4) the same way the frontend's
``stripLayout`` places it. The PDF side is checked twice: the drawing calls on a reportlab canvas,
and the vector operations in the written PDF read back with pypdf. No pixel tests.

Synthetic values only: nothing here comes from a client drawing.
"""

from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from app.schemas.visual_ui import (
    AgreementFactsOut,
    CountertopPieceOut,
    CountertopResultOut,
    ExactValueOut,
    HoldOut,
    WallLayoutOut,
)
from reports import findings_pdf
from reports.findings_pdf import FindingsPdfInput, write_findings_pdf
from reports.spreadsheet import StoredFinding
from verdict.outcomes import Outcome

if TYPE_CHECKING:
    # Imported inside each test, so on main every test fails on its own assertion.
    from reports.countertop_strip import StripDrawn


def _x(numerator: str, denominator: str, display: str) -> ExactValueOut:
    return ExactValueOut(numerator=numerator, denominator=denominator, display=display)


def _piece(
    index: int, value: ExactValueOut | None, kind: str | None = "cabinet"
) -> CountertopPieceOut:
    return CountertopPieceOut(
        index=index, value=value, kind=kind, source="sealed" if value else "missing"
    )


_BOTH_ENDS = WallLayoutOut(
    config="back_left_right", label="back wall and both ends", source="drawing clues"
)


def _row(**changes: object) -> CountertopResultOut:
    base = CountertopResultOut(
        finding_id=UUID(int=1),
        row_id=UUID(int=2),
        page_number=1,
        label="Synthetic countertop",
        row_location=None,
        outcome=Outcome.FAIL,
        reviewer_decision=None,
        needs_decision=False,
        printed_overall=_x("42", "1", '42"'),
        pieces=(
            _piece(0, _x("2", "1", '2"'), "filler"),
            _piece(1, _x("101", "8", '12 5/8"')),
            _piece(2, _x("89", "4", '22 1/4"')),
            _piece(3, _x("2", "1", '2"'), "filler"),
        ),
        field_cut_per_end=_x("1", "1", '1"'),
        field_cut_count=2,
        expected_total=_x("331", "8", '41 3/8"'),
        delta=_x("5", "8", '5/8"'),
        wall_layout=_BOTH_ENDS,
        agreement=AgreementFactsOut(
            both_readers_agreed_on_row=True,
            values_agreed=(True, True, True, True, True),
            code_clue_used=True,
        ),
        hold=None,
    )
    return base.model_copy(update=changes)


OVER = _row()
SHORT = _row(printed_overall=_x("40", "1", '40"'), delta=_x("-11", "8", '-1 3/8"'))
PANELS = _row(
    outcome=Outcome.PASS,
    printed_overall=_x("55", "1", '55"'),
    pieces=(
        _piece(0, _x("5", "1", '5"'), "filler"),
        _piece(1, _x("45", "1", '45"')),
        _piece(2, _x("5", "1", '5"'), "filler"),
    ),
    field_cut_count=0,
    expected_total=_x("55", "1", '55"'),
    delta=_x("0", "1", '0"'),
    wall_layout=WallLayoutOut(
        config="back_only",
        label="back wall only; no field cut at the ends",
        source="between panels",
    ),
)
MISSING = _row(
    outcome=Outcome.REVIEW_REQUIRED,
    needs_decision=True,
    printed_overall=None,
    expected_total=None,
    delta=None,
    pieces=(_piece(0, None), _piece(1, _x("36", "1", '36"')), _piece(2, None)),
    hold=HoldOut(code="row-incomplete", reason="This row is incomplete."),
)
NO_PIECES = _row(pieces=())

# OVER in inches, left to right: cap 1, pieces 2 + 12 5/8 + 22 1/4 + 2, cap 1; printed 42 is the
# longest thing drawn, so it sets the one scale.
_OVER_INCHES = (2.0, 12.625, 22.25, 2.0)


# ── The layout, against the spec ────────────────────────────────────────────────────────────────


def test_pieces_caps_and_brackets_share_one_scale_from_the_wall_face() -> None:
    from reports.countertop_strip import StripDrawn, strip_layout

    layout = strip_layout(OVER, 1000)

    assert isinstance(layout, StripDrawn) and layout.to_scale
    scale = 1000 / 42
    assert layout.cap_left is not None and layout.cap_right is not None
    assert (layout.cap_left.x, layout.cap_left.w) == pytest.approx((0, 1 * scale))
    left = 1.0
    for piece, inches in zip(layout.pieces, _OVER_INCHES, strict=True):
        assert (piece.x, piece.w) == pytest.approx((left * scale, inches * scale))
        left += inches
    assert (layout.cap_right.x, layout.cap_right.w) == pytest.approx((left * scale, 1 * scale))
    assert layout.printed is not None and layout.needed is not None
    assert (layout.printed.x, layout.printed.w) == pytest.approx((0, 1000))
    assert (layout.needed.x, layout.needed.w) == pytest.approx((0, 41.375 * scale))


def test_labels_are_the_exact_api_text() -> None:
    from reports.countertop_strip import StripDrawn, strip_layout

    layout = strip_layout(OVER, 1000)

    assert isinstance(layout, StripDrawn)
    assert [piece.label for piece in layout.pieces] == ['2"', '12 5/8"', '22 1/4"', '2"']
    assert layout.printed is not None and layout.printed.label == '42"'
    assert layout.needed is not None and layout.needed.label == '41 3/8"'
    assert layout.cap_left is not None and layout.cap_left.label == '+1"'


def test_a_shortfall_is_drawn_to_its_length() -> None:
    from reports.countertop_strip import StripDrawn, strip_layout

    layout = strip_layout(SHORT, 1000)

    assert isinstance(layout, StripDrawn)
    assert layout.printed is not None and layout.needed is not None
    assert layout.printed.x == 0 and layout.needed.x == 0
    assert layout.printed.w / layout.needed.w == pytest.approx(40 / 41.375)
    # Needed is now the longest thing drawn, so it spans the drawing.
    assert layout.needed.w == pytest.approx(1000)


def test_caps_only_at_ends_with_a_wall() -> None:
    from reports.countertop_strip import StripDrawn, Walls, _layout, strip_layout

    panels = strip_layout(PANELS, 1000)
    assert isinstance(panels, StripDrawn)
    assert (panels.cap_left, panels.cap_right) == (None, None)
    island = strip_layout(
        _row(wall_layout=WallLayoutOut(config="island", label="island", source="reviewer")), 1000
    )
    assert isinstance(island, StripDrawn) and (island.cap_left, island.cap_right) == (None, None)
    unknown = strip_layout(
        _row(wall_layout=WallLayoutOut(config=None, label=None, source="not established")), 1000
    )
    assert isinstance(unknown, StripDrawn) and unknown.walls is None
    assert (unknown.cap_left, unknown.cap_right) == (None, None)

    # A wall at one end only: one cap, at that end, and the run starts after it.
    one_end = _layout(OVER, 1000, Walls(back=True, left=False, right=True))
    assert isinstance(one_end, StripDrawn)
    assert one_end.cap_left is None and one_end.cap_right is not None
    assert one_end.pieces[0].x == 0
    assert one_end.cap_right.x == pytest.approx(one_end.pieces[-1].x + one_end.pieces[-1].w)


def test_missing_widths_draw_equal_boxes_not_to_scale() -> None:
    from reports.countertop_strip import MIN_CAP, StripDrawn, strip_layout

    layout = strip_layout(MISSING, 1000)

    assert isinstance(layout, StripDrawn) and not layout.to_scale
    assert [piece.label for piece in layout.pieces] == [None, '36"', None]
    inner = 1000 - 2 * (MIN_CAP * 2)
    assert [piece.w for piece in layout.pieces] == pytest.approx([inner / 3] * 3)
    assert layout.held


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (NO_PIECES, "Pieces not read"),
        (
            _row(pieces=(_piece(0, _x("1.5", "1", '1.5"')),)),
            "A width is not an exact positive number",
        ),
        (_row(pieces=(_piece(0, _x("3", "0", "?")),)), "A width is not an exact positive number"),
        (
            _row(pieces=(_piece(0, _x("-3", "1", '-3"')),)),
            "A width is not an exact positive number",
        ),
        (_row(printed_overall=_x("1e3", "1", "x")), "A total is not an exact number"),
    ],
)
def test_refuses_rather_than_guesses(row: CountertopResultOut, reason: str) -> None:
    from reports.countertop_strip import StripRefused, strip_layout

    assert strip_layout(row, 1000) == StripRefused(reason)


# ── The drawing calls, against the layout ───────────────────────────────────────────────────────


class _Recording(Canvas):  # type: ignore[misc]
    """A real reportlab canvas that also notes each rectangle, line and string it draws."""

    def __init__(self) -> None:
        super().__init__(BytesIO())
        self.dashed = False
        self._saved: list[bool] = []
        self.rects: list[tuple[float, float, float, float, bool]] = []
        self.lines: list[tuple[float, float, float, float]] = []
        self.strings: list[str] = []

    def setDash(self, array: object = (), phase: float = 0) -> None:
        self.dashed = array not in ((), [], None)
        super().setDash(array, phase)

    def saveState(self) -> None:
        self._saved.append(self.dashed)
        super().saveState()

    def restoreState(self) -> None:
        self.dashed = self._saved.pop()
        super().restoreState()

    def rect(self, x: float, y: float, width: float, height: float, **kwargs: object) -> None:
        self.rects.append((x, y, width, height, self.dashed))
        super().rect(x, y, width, height, **kwargs)

    def line(self, x1: float, y1: float, x2: float, y2: float) -> None:
        self.lines.append((x1, y1, x2, y2))
        super().line(x1, y1, x2, y2)

    def drawString(self, x: float, y: float, text: str, *args: object, **kwargs: object) -> None:
        self.strings.append(text)
        super().drawString(x, y, text, *args, **kwargs)

    def drawCentredString(
        self, x: float, y: float, text: str, *args: object, **kwargs: object
    ) -> None:
        self.strings.append(text)
        super().drawCentredString(x, y, text, *args, **kwargs)

    def drawRightString(
        self, x: float, y: float, text: str, *args: object, **kwargs: object
    ) -> None:
        self.strings.append(text)
        super().drawRightString(x, y, text, *args, **kwargs)


_WIDTH = 420.0
_RUN_Y = 7.0  # the run's bottom edge above the strip's baseline (y=0)


def _draw(row: CountertopResultOut) -> _Recording:
    canvas = _Recording()
    findings_pdf._draw_countertop_strip(canvas, row, x=0, y=0, width=_WIDTH)
    return canvas


def _layout_for_pdf(row: CountertopResultOut) -> StripDrawn:
    from reports.countertop_strip import StripDrawn, strip_layout

    layout = strip_layout(row, findings_pdf._strip_draw_width(_WIDTH))
    assert isinstance(layout, StripDrawn)
    return layout


def _run_boxes(canvas: _Recording) -> list[tuple[float, float, bool]]:
    """The boxes in the run (pieces and caps), left to right: (left, width, dashed)."""
    return sorted(
        (x, w, dashed)
        for x, y, w, h, dashed in canvas.rects
        if y == pytest.approx(_RUN_Y) and h == pytest.approx(16)
    )


def _horizontal(canvas: _Recording) -> list[tuple[float, float]]:
    return [(x1, x2 - x1) for x1, y1, x2, y2 in canvas.lines if y1 == y2 and x2 > x1]


@pytest.mark.parametrize(
    "row", [OVER, SHORT, PANELS, MISSING], ids=["over", "short", "panels", "missing"]
)
def test_pdf_draws_the_strip_geometry(row: CountertopResultOut) -> None:
    layout = _layout_for_pdf(row)
    x0 = findings_pdf._STRIP_WALL
    expected: list[tuple[float, float, bool]] = []
    if layout.cap_left is not None:
        expected.append((x0 + layout.cap_left.x, layout.cap_left.w, True))
    for piece in layout.pieces:
        expected.append((x0 + piece.x, piece.w, piece.label is None or piece.kind == "appliance"))
    if layout.cap_right is not None:
        expected.append((x0 + layout.cap_right.x, layout.cap_right.w, True))

    canvas = _draw(row)

    drawn = _run_boxes(canvas)
    assert [(x, w) for x, w, _ in drawn] == pytest.approx([(x, w) for x, w, _ in expected])
    assert [dashed for _, _, dashed in drawn] == [dashed for _, _, dashed in expected]
    spans = _horizontal(canvas)
    for bracket in (layout.printed, layout.needed):
        if bracket is not None:
            assert any(
                (x, w) == pytest.approx((x0 + bracket.x, bracket.w)) for x, w in spans
            ), bracket


def test_pdf_brackets_show_the_shortfall_to_length() -> None:
    canvas = _draw(SHORT)
    printed = next(text for text in canvas.strings if text.startswith("Printed"))
    assert printed == 'Printed 40"'
    lengths = sorted(
        w for x, w in _horizontal(canvas) if x == pytest.approx(findings_pdf._STRIP_WALL)
    )
    draw_width = findings_pdf._strip_draw_width(_WIDTH)
    assert draw_width * 40 / 41.375 == pytest.approx(lengths[0])
    assert draw_width == pytest.approx(lengths[-1])


def test_pdf_missing_pieces_are_dashed_question_marks_and_not_to_scale() -> None:
    canvas = _draw(MISSING)

    assert canvas.strings.count("?") == 2
    assert "Not to scale: some widths are missing." in canvas.strings
    assert '36"' in canvas.strings


def test_pdf_caps_follow_the_walls_not_the_count() -> None:
    # A field cut count with no known wall end draws no cap: the end is never guessed.
    unknown = _row(wall_layout=WallLayoutOut(config=None, label=None, source="not established"))
    assert not any(dashed for _, _, dashed in _run_boxes(_draw(unknown)))
    assert '+1"' not in _draw(unknown).strings
    assert _draw(OVER).strings.count('+1"') == 2


# ── The written PDF, read back with pypdf ───────────────────────────────────────────────────────


def _stored() -> StoredFinding:
    return StoredFinding(
        rule_id="CT-WIDTH-001",
        outcome="PASS",
        severity="CRITICAL",
        snapshot_id="snapshot",
        engine_version="test",
        trace={},
    )


def _pdf(*rows: CountertopResultOut) -> bytes:
    return write_findings_pdf(
        FindingsPdfInput(
            package_revision_id=UUID(int=11),
            revision_number=1,
            vendor=None,
            findings=(_stored(),),
            countertop_results=rows,
        )
    )


def _text(data: bytes) -> str:
    return " ".join(
        " ".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages).split()
    )


def _operations(data: bytes, page_index: int) -> Iterator[tuple[str, list[float], bool]]:
    """Each path operation on one page with its operands and whether a dash pattern is set."""
    reader = PdfReader(BytesIO(data))
    page = reader.pages[page_index]
    content = ContentStream(page.get_contents(), reader)
    dashed = False
    saved: list[bool] = []
    for operands, operator in content.operations:
        name = operator.decode()
        if name == "d":
            dashed = bool(operands[0])
        elif name == "q":
            saved.append(dashed)
        elif name == "Q":
            dashed = saved.pop() if saved else False
        else:
            yield name, [
                float(value) for value in operands if isinstance(value, int | float)
            ], dashed


def _countertop_page(data: bytes) -> int:
    pages = PdfReader(BytesIO(data)).pages
    return next(i for i, page in enumerate(pages) if "COUNTERTOPS" in (page.extract_text() or ""))


def test_written_pdf_draws_piece_boxes_to_scale() -> None:
    data = _pdf(OVER)
    boxes = sorted(
        (operands[0], operands[2], dashed)
        for name, operands, dashed in _operations(data, _countertop_page(data))
        if name == "re" and len(operands) == 4 and operands[3] == pytest.approx(16)
    )
    layout = _layout_for_pdf(OVER)
    # The card draws the strip in the content width; widths, not positions, are compared here.
    draw_width = findings_pdf._strip_draw_width(findings_pdf._CONTENT_WIDTH - 16)
    scale = draw_width / 42

    solid = [w for _, w, dashed in boxes if not dashed]
    caps = [w for _, w, dashed in boxes if dashed]
    assert solid == pytest.approx([inches * scale for inches in _OVER_INCHES])
    assert caps == pytest.approx([1 * scale, 1 * scale])
    assert len(layout.pieces) == len(solid)


def test_written_pdf_marks_missing_pieces_and_says_not_to_scale() -> None:
    data = _pdf(MISSING)
    text = _text(data)
    dashed_boxes = [
        operands
        for name, operands, dashed in _operations(data, _countertop_page(data))
        if name == "re" and dashed and operands[3] == pytest.approx(16)
    ]

    assert "Not to scale" in text
    assert "?" in text
    # Two missing pieces and the two field-cut caps.
    assert len(dashed_boxes) == 4


@pytest.mark.parametrize(
    ("row", "line"),
    [
        (NO_PIECES, "No picture: pieces not read"),
        (
            _row(pieces=(_piece(0, _x("1.5", "1", '1.5"')),)),
            "No picture: a width is not an exact positive number",
        ),
    ],
)
def test_written_pdf_says_why_there_is_no_picture(row: CountertopResultOut, line: str) -> None:
    data = _pdf(row)

    assert line in _text(data)
    assert not any(
        name == "re" and operands[3] == pytest.approx(16)
        for name, operands, _ in _operations(data, _countertop_page(data))
        if len(operands) == 4
    )
