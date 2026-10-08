"""Finding a dimension row's ticks, slots, overall and labels from the drawing's own lines (#980).

Verification for: `extraction/geometry/rows.py` (the builder) through `extraction/rows.py`'s
`ink_from_page` (the pdfplumber adapter), so that what is tested is what a real page goes through.

**Every page here is written by hand, from a literal content stream**, the way
`tests/extraction/test_reader.py` builds its pages and for the same reasons: no new dependency, and
a fixture that states exactly what it draws. No client drawing is read and no dimension off one
appears in this file. The numbers in the labels are arbitrary.

The tests worth reading first: `test_a_tick_slash_is_the_tick_not_the_label` — the prototype's
most frequent wrong label was the tick itself — and
`test_the_overall_wider_than_its_pieces_is_a_finding_not_a_loss`, which is the case a closure
detector that only reports exact tilings throws away.
"""

from __future__ import annotations

import io
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from enum import StrEnum

import pdfplumber
import pytest

from evidence.coordinates import StoredPoint
from extraction.geometry.rows import (
    LABEL_IS_STACKED,
    LABEL_TOUCHES_EDGE,
    MEASURED_SETTINGS,
    PIECES_DO_NOT_TILE,
    Box,
    CountertopRowCandidate,
    LabelKind,
    PageRows,
    RowSettings,
    TickSource,
    Tiling,
    build_rows,
)
from extraction.rows import ink_from_page
from tests.extraction.test_reader import _pdf

#: The page every fixture draws on, in PDF points. pdfplumber measures `top` down from 300.
WIDTH, HEIGHT = Decimal(400), Decimal(300)


def _place(x: Decimal, top: Decimal) -> StoredPoint:
    """The simplest placement: the page itself is the frame."""
    return StoredPoint(x / WIDTH, top / HEIGHT)


def _rows(
    content: bytes,
    *,
    settings: RowSettings = MEASURED_SETTINGS,
    drawing_boxes: tuple[Box, ...] = (),
) -> PageRows:
    with pdfplumber.open(io.BytesIO(_pdf(content, box=b"[0 0 400 300]"))) as document:
        ink = ink_from_page(document.pages[0], drawing_boxes=drawing_boxes)
    return build_rows(ink, settings, place=_place)


def _slash(x: float, y: float) -> bytes:
    """A tick slash as the client's CAD draws it: a tiny filled parallelogram across the line."""
    return (
        f"{x - 1:.2f} {y - 1:.2f} m {x + 1:.2f} {y + 1:.2f} l {x + 1.4:.2f} {y + 1:.2f} l "
        f"{x - 0.6:.2f} {y - 1:.2f} l h f\n"
    ).encode()


def _diagonal(x: float, y: float) -> bytes:
    """A tick slash as a plain two-point diagonal stroke, the other style seen."""
    return f"{x - 1:.2f} {y - 1:.2f} m {x + 1:.2f} {y + 1:.2f} l S\n".encode()


def _line(x0: float, y: float, x1: float) -> bytes:
    return f"0.3 w {x0:.2f} {y:.2f} m {x1:.2f} {y:.2f} l S\n".encode()


def _vertical(x: float, y0: float, y1: float) -> bytes:
    return f"0.3 w {x:.2f} {y0:.2f} m {x:.2f} {y1:.2f} l S\n".encode()


def _text(x: float, y: float, text: str, *, size: int = 6) -> bytes:
    return f"BT /F1 {size} Tf 1 0 0 1 {x:.2f} {y:.2f} Tm ({text}) Tj ET\n".encode()


#: A three-slot row at y=100 (top 200) from 50 to 350, ticks at 50, 100, 200 and 350, labelled
#: `12`, `24`, `36` above each slot, and a coincident overall 15 pt below labelled `72`.
ROW_Y = 100.0
TICKS = (50.0, 100.0, 200.0, 350.0)
CHAIN = _line(50, ROW_Y, 350) + b"".join(_slash(x, ROW_Y) for x in TICKS)
LABELS = _text(70, ROW_Y + 4, "12") + _text(145, ROW_Y + 4, "24") + _text(270, ROW_Y + 4, "36")
OVERALL_Y = 85.0
OVERALL = _line(50, OVERALL_Y, 350) + _slash(50, OVERALL_Y) + _slash(350, OVERALL_Y)
OVERALL_LABEL = _text(195, OVERALL_Y + 4, "72")
SHEET = CHAIN + LABELS + OVERALL + OVERALL_LABEL


def _only_candidate(rows: PageRows) -> CountertopRowCandidate:
    assert len(rows.candidates) == 1, [c.rejected_because for c in rows.rejected]
    return rows.candidates[0]


# ---------------------------------------------------------------------------
# Ticks and slots
# ---------------------------------------------------------------------------


def test_slashes_on_the_line_give_the_ticks_and_the_slots() -> None:
    """Input: a row with four filled slash marks. Outcome: three slots at the slashes' centres."""
    row = _only_candidate(_rows(SHEET))

    assert row.tick_source is TickSource.SLASH
    assert [t.quantize(Decimal("0.1")) for t in row.ticks] == [
        Decimal("50.2"),
        Decimal("100.2"),
        Decimal("200.2"),
        Decimal("350.2"),
    ]
    assert [slot.width_pt for slot in row.slots] == [Decimal(50), Decimal(100), Decimal(150)]
    assert row.y == Decimal(200)  # pdfplumber's frame: 300 - 100


def test_diagonal_line_ticks_are_found_too() -> None:
    """Input: the same row with two-point diagonal strokes for ticks. Outcome: the same slots."""
    sheet = _line(50, ROW_Y, 350) + b"".join(_diagonal(x, ROW_Y) for x in TICKS) + LABELS
    row = _only_candidate(_rows(sheet))

    assert row.tick_source is TickSource.SLASH
    assert [slot.width_pt for slot in row.slots] == [Decimal(50), Decimal(100), Decimal(150)]


def test_witness_lines_are_the_fallback_and_a_dashed_line_is_never_one() -> None:
    """Input: no slashes; witness lines cross the row at four places, and a dashed centre line
    crosses it at a fifth. Outcome: four ticks, from the witnesses; the dashed line is not one."""
    witnesses = b"".join(_vertical(x, ROW_Y - 6, ROW_Y + 6) for x in TICKS)
    dashed = b"".join(_vertical(150, y, y + 8) for y in range(40, 170, 14))
    row = _only_candidate(_rows(_line(50, ROW_Y, 350) + witnesses + dashed + LABELS))

    assert row.tick_source is TickSource.WITNESS
    assert len(row.slots) == 3
    assert all(abs(t - Decimal(150)) > 1 for t in row.ticks)


def test_a_long_vertical_is_a_cabinet_edge_not_a_witness() -> None:
    """Input: witness lines at the ends, and a 100 pt vertical crossing the row in the middle.
    Outcome: the long stroke is not a tick."""
    witnesses = _vertical(50, ROW_Y - 6, ROW_Y + 6) + _vertical(350, ROW_Y - 6, ROW_Y + 6)
    edge = _vertical(200, ROW_Y - 50, ROW_Y + 50)
    sheet = _line(50, ROW_Y, 350) + witnesses + edge + _text(195, ROW_Y + 4, "72")
    rows = _rows(sheet)

    spans = [row for row in rows.rejected if row.tick_source is TickSource.WITNESS]
    assert len(spans) == 1 and len(spans[0].ticks) == 2


def test_a_row_whose_ticks_come_only_from_its_breaks_is_kept_but_not_a_candidate() -> None:
    """Input: three collinear pieces with narrow breaks and nothing else. Outcome: a gap-mode row
    with three slots, returned among the rejected with a plain reason."""
    sheet = _line(50, ROW_Y, 148) + _line(152, ROW_Y, 250) + _line(254, ROW_Y, 350) + LABELS
    rows = _rows(sheet)

    assert rows.candidates == ()
    (row,) = [row for row in rows.rejected if len(row.slots) == 3]
    assert row.tick_source is TickSource.GAP
    assert row.rejected_because is not None and "breaks" in row.rejected_because


def test_the_tick_merge_is_scale_aware_so_a_narrow_slot_survives() -> None:
    """Input: a 160 pt row with a 1.2 pt slot in it. Outcome: the slot survives — and with the
    merge floored at the old fixed 2 pt it would not have."""
    ticks = (50.0, 100.0, 150.0, 151.2, 210.0)
    sheet = _line(50, ROW_Y, 210) + b"".join(_slash(x, ROW_Y) for x in ticks)
    sheet += _text(70, ROW_Y + 4, "1") + _text(120, ROW_Y + 4, "2")
    sheet += _text(148, ROW_Y + 8, "3") + _text(175, ROW_Y + 4, "4")

    row = _only_candidate(_rows(sheet))
    assert len(row.slots) == 4
    assert min(slot.width_pt for slot in row.slots) == Decimal("1.2")

    fixed = replace(MEASURED_SETTINGS, tick_merge_floor_pt=Decimal(2))
    merged = _only_candidate(_rows(sheet, settings=fixed))
    assert len(merged.slots) == 3


def test_a_label_stroke_touching_the_line_is_not_a_tick() -> None:
    """Input: the row's slashes plus a tiny stroke of a different size touching the line, as the
    foot of a letter does. Outcome: only the row's modal slash size counts."""
    stray = b"124 99.5 m 127.5 101.5 l S\n"  # 3.5 x 2: slash-sized, not the row's size
    row = _only_candidate(_rows(SHEET + stray))

    assert len(row.slots) == 3


# ---------------------------------------------------------------------------
# The overall
# ---------------------------------------------------------------------------


def test_the_overall_with_coincident_ends_is_found() -> None:
    row = _only_candidate(_rows(SHEET))

    assert row.overall is not None
    assert row.overall.tiling is Tiling.COINCIDENT
    assert row.overall.left_excess_pt == 0 and row.overall.right_excess_pt == 0
    assert row.overall.y == Decimal(300) - Decimal(85)
    assert [label.text for label in row.overall.labels] == ["72"]
    assert row.findings == ()


def test_the_overall_wider_than_its_pieces_is_a_finding_not_a_loss() -> None:
    """Input: the overall reaches 10 pt past the chain on each side. Outcome: it is still the
    chain's overall, with `pieces do not tile the overall` and the excess per side."""
    overall = _line(40, OVERALL_Y, 360) + _slash(40, OVERALL_Y) + _slash(360, OVERALL_Y)
    row = _only_candidate(_rows(CHAIN + LABELS + overall + OVERALL_LABEL))

    assert row.overall is not None
    assert row.overall.tiling is Tiling.PIECES_DO_NOT_TILE
    assert row.overall.left_excess_pt.quantize(Decimal("0.1")) == Decimal(10)
    assert row.overall.right_excess_pt.quantize(Decimal("0.1")) == Decimal(10)
    assert PIECES_DO_NOT_TILE in row.findings


def test_a_labelled_overall_beats_a_nearer_unlabelled_outline_with_the_same_ends() -> None:
    """Input: the printed overall sits 18 pt above the chain with its label; the countertop's
    own outline, ticked at the same two ends, sits 10 pt below with no label. Outcome: the
    labelled line is the overall. A client page (proof run 2026-10-08) had exactly this and the
    nearer outline won, so the overall's label was never read."""
    printed = _line(50, ROW_Y + 18, 350) + _slash(50, ROW_Y + 18) + _slash(350, ROW_Y + 18)
    outline = _line(50, ROW_Y - 10, 350) + _vertical(50, ROW_Y - 13, ROW_Y - 7)
    outline += _vertical(350, ROW_Y - 13, ROW_Y - 7)
    row = _only_candidate(_rows(CHAIN + LABELS + printed + _text(195, ROW_Y + 22, "72") + outline))

    assert row.overall is not None
    assert row.overall.y == Decimal(300) - Decimal(ROW_Y + 18)
    assert row.overall.labels[0].text == "72"


def test_with_no_labelled_candidate_the_nearest_coincident_line_is_still_the_overall() -> None:
    """Unchanged behaviour when nothing is labelled: the nearest line with the chain's ends."""
    near = _line(50, ROW_Y - 10, 350) + _slash(50, ROW_Y - 10) + _slash(350, ROW_Y - 10)
    farther = _line(50, ROW_Y + 18, 350) + _slash(50, ROW_Y + 18) + _slash(350, ROW_Y + 18)
    row = _only_candidate(_rows(CHAIN + LABELS + near + farther))

    assert row.overall is not None
    assert row.overall.y == Decimal(300) - Decimal(ROW_Y - 10)


def test_a_coincident_line_far_from_the_chain_is_not_its_overall() -> None:
    """Input: the two-tick line sits 60 pt from the chain. Outcome: no overall — a run's outline
    or a wall-to-wall line, which E1 saw matched by its ends alone."""
    far = _line(50, ROW_Y - 60, 350) + _slash(50, ROW_Y - 60) + _slash(350, ROW_Y - 60)
    row = _only_candidate(_rows(CHAIN + LABELS + far))

    assert row.overall is None


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def test_each_slot_reads_the_text_printed_over_it() -> None:
    row = _only_candidate(_rows(SHEET))

    assert [slot.label.text if slot.label else None for slot in row.slots] == ["12", "24", "36"]
    assert all(slot.label is not None and slot.label.kind is LabelKind.TEXT for slot in row.slots)
    assert row.labelled == 3


def test_a_tick_slash_is_the_tick_not_the_label() -> None:
    """Input: the row drawn without any text. Outcome: no slot is labelled by its own tick marks,
    even though each is a small path sitting exactly on the line."""
    rows = _rows(CHAIN + OVERALL)

    (row,) = [row for row in rows.rejected if len(row.slots) == 3]
    assert row.labelled == 0
    assert all(slot.labels == () for slot in row.slots)


def test_mm_and_inch_lines_that_interleave_along_x_come_out_as_two_labels() -> None:
    """Input: `762` set 3.5 pt above `[30]`, starting half a point apart so their characters
    interleave along x. Outcome: two labels, `762` and `[30]`, each copied as printed — not the
    jumble a word built by x alone gives, and not one label either: pairing them is notation."""
    sheet = _line(50, ROW_Y, 150) + _slash(50, ROW_Y) + _slash(100, ROW_Y) + _slash(150, ROW_Y)
    sheet += _text(70, ROW_Y + 7.5, "762", size=3) + _text(70.5, ROW_Y + 4, "[30]", size=3)
    sheet += _text(120, ROW_Y + 4, "8", size=3)
    row = _only_candidate(_rows(sheet))

    labels = row.slots[0].labels
    assert {label.text for label in labels} == {"762", "[30]"}
    assert all(label.kind is LabelKind.TEXT and not label.stacked for label in labels)
    assert all(len(label.lines) == 1 for label in labels)


def test_a_stacked_fraction_has_no_text_and_is_a_finding() -> None:
    """Input: a numerator set small above a denominator, overlapping the whole number. Outcome:
    the label's lines are kept, its `text` is `None`, and the row says why (#762)."""
    stacked = _text(140, ROW_Y + 4, "2", size=6) + _text(143, ROW_Y + 6.5, "1", size=3)
    stacked += _text(143, ROW_Y + 3.5, "2", size=3)
    sheet = CHAIN + _text(70, ROW_Y + 4, "12") + stacked + _text(270, ROW_Y + 4, "36")
    row = _only_candidate(_rows(sheet))

    label = row.slots[1].label
    assert label is not None and label.stacked and label.text is None
    assert len(label.lines) == 2
    assert LABEL_IS_STACKED in row.findings


def test_glyph_paths_are_located_and_never_read() -> None:
    """Input: a label drawn as a run of small strokes, not text. Outcome: a glyph label with a box
    and a stroke count and no text."""
    glyphs = b"".join(
        f"{x} 104 m {x + 0.8} 107 l {x} 110 l S\n".encode() for x in (142, 144, 146, 148)
    )
    sheet = CHAIN + _text(70, ROW_Y + 4, "12") + glyphs + _text(270, ROW_Y + 4, "36")
    row = _only_candidate(_rows(sheet))

    label = row.slots[1].label
    assert label is not None and label.kind is LabelKind.GLYPHS
    assert label.text is None and label.lines == () and label.strokes == 4
    assert label.box.x0 == Decimal(142) and label.box.x1 == Decimal("148.8")


def test_a_label_at_the_page_edge_is_flagged() -> None:
    """Input: the row drawn 10 pt below the top of the page, its first label running off the top.
    Outcome: that label touches the edge and the row carries the finding."""
    y = 290.0
    sheet = _line(50, y, 350) + b"".join(_slash(x, y) for x in TICKS)
    sheet += _text(70, y + 8, "12") + _text(145, y + 4, "24") + _text(270, y + 4, "36")
    row = _only_candidate(_rows(sheet))

    first, second = row.slots[0].label, row.slots[1].label
    assert first is not None and first.touches_edge
    assert second is not None and not second.touches_edge
    assert LABEL_TOUCHES_EDGE in row.findings


def test_a_label_cut_by_the_drawings_edge_is_flagged() -> None:
    """Input: the pasted drawing's box ends through the first label. Outcome: flagged; the others,
    inside the box, are not."""
    frame = Box(Decimal(72), Decimal(150), Decimal(380), Decimal(250))
    row = _only_candidate(_rows(SHEET, drawing_boxes=(frame,)))

    assert [slot.label.touches_edge for slot in row.slots if slot.label] == [True, False, False]


def test_the_label_nearest_the_slots_centre_leads() -> None:
    """Input: a cabinet tag sits closer to the line than the dimension text, but off to one side.
    Outcome: the centred dimension text leads and the tag is still listed."""
    tag = _text(210, ROW_Y + 2, "B36")
    row = _only_candidate(_rows(SHEET + tag))

    labels = row.slots[2].labels
    assert [label.text for label in labels] == ["36", "B36"]


# ---------------------------------------------------------------------------
# Whose ink
# ---------------------------------------------------------------------------


def test_coloured_ink_is_never_a_row_a_tick_or_a_label() -> None:
    """Input: the sheet plus a red row with red ticks and red text, and a red tick on the vendor's
    row. Outcome: nothing red is found."""
    red = b"1 0 0 RG 1 0 0 rg\n" + _line(50, 60, 350) + _slash(50, 60) + _slash(350, 60)
    red += _slash(300, ROW_Y) + _text(195, 64, "99") + _text(295, ROW_Y + 4, "7")
    rows = _rows(SHEET + red)

    row = _only_candidate(rows)
    assert len(row.slots) == 3
    assert [slot.label.text if slot.label else None for slot in row.slots] == ["12", "24", "36"]
    assert all(abs(row.y - Decimal(240)) > 1 for row in rows.rejected)


# ---------------------------------------------------------------------------
# Never a silent pick
# ---------------------------------------------------------------------------


def test_every_row_is_returned_and_the_candidates_are_ranked() -> None:
    """Input: the sheet plus a second labelled chain with no overall. Outcome: both are candidates,
    the one with an overall ranked first; the overall's own two-tick row is among the rejected."""
    second_y = 200.0
    second = _line(50, second_y, 350) + b"".join(_slash(x, second_y) for x in (50, 200, 350))
    second += _text(120, second_y + 4, "5") + _text(270, second_y + 4, "6")
    rows = _rows(SHEET + second)

    assert [c.rank for c in rows.candidates] == [1, 2]
    assert rows.candidates[0].overall is not None and rows.candidates[1].overall is None
    assert rows.candidates[0].y == Decimal(200) and rows.candidates[1].y == Decimal(100)
    reasons = {row.rejected_because for row in rows.rejected}
    assert any(reason and "span" in reason for reason in reasons)
    assert rows.rows_found == 3


def test_an_unlabelled_row_is_not_a_candidate_and_says_so() -> None:
    rows = _rows(CHAIN + _text(70, ROW_Y + 4, "12") + OVERALL)

    assert rows.candidates == ()
    (row,) = [row for row in rows.rejected if len(row.slots) == 3]
    assert row.rejected_because == "only 1 of 3 slots have a label"


# ---------------------------------------------------------------------------
# Contracts
# ---------------------------------------------------------------------------


def _leaves(value: object) -> list[object]:
    if isinstance(value, (str, bytes, bool, int, Decimal, StrEnum)) or value is None:
        return [value]
    if isinstance(value, (tuple, list)):
        return [leaf for item in value for leaf in _leaves(item)]
    if is_dataclass(value) and not isinstance(value, type):
        return [leaf for field in fields(value) for leaf in _leaves(getattr(value, field.name))]
    if isinstance(value, StoredPoint):
        return [value.x, value.y]
    return [value]


def test_the_output_holds_no_float() -> None:
    """Every number in the result is a Decimal or an int: nothing here may hand a float onward."""
    rows = _rows(SHEET)

    leaves = _leaves(rows)
    assert leaves, "the walk found nothing"
    assert not any(isinstance(leaf, float) for leaf in leaves)
    assert all(
        isinstance(leaf, (str, bool, int, Decimal, StrEnum)) or leaf is None for leaf in leaves
    )


def test_stored_boxes_are_placed_through_the_callers_placement() -> None:
    row = _only_candidate(_rows(SHEET))

    slot = row.slots[0]
    assert slot.stored.left == slot.box.x0 / WIDTH
    assert slot.stored.right == slot.box.x1 / WIDTH
    assert slot.stored.top == slot.box.top / HEIGHT
    assert Decimal(0) <= slot.stored.top < slot.stored.bottom <= Decimal(1)


def test_the_same_page_gives_the_same_rows_twice() -> None:
    assert _rows(SHEET) == _rows(SHEET)


def test_settings_refuse_a_float() -> None:
    with pytest.raises(TypeError):
        replace(MEASURED_SETTINGS, same_row_pt=0.6)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        replace(MEASURED_SETTINGS, slash_maximum_points=8.0)  # type: ignore[arg-type]


def test_a_box_refuses_a_float_and_an_inside_out_shape() -> None:
    with pytest.raises(TypeError):
        Box(0.0, Decimal(0), Decimal(1), Decimal(1))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Box(Decimal(2), Decimal(0), Decimal(1), Decimal(1))


def test_the_builder_imports_nothing_from_the_verdict() -> None:
    """The one rule: geometry may inform a crop, never a verdict, and the verdict must not reach
    back. `tests/test_verdict_isolation.py` holds the other direction."""
    import ast

    import extraction.geometry.rows as builder
    import extraction.rows as adapter

    for module in (builder, adapter):
        assert module.__file__ is not None
        with open(module.__file__, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        imported = [
            name
            for node in ast.walk(tree)
            for name in (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
        ]
        assert imported and not any(name.split(".")[0] == "verdict" for name in imported)


def test_a_row_whose_labels_are_all_words_is_not_a_candidate() -> None:
    """Input: a chain labelled `EQ`, `EQ` (a tap's centring line). Outcome: not a candidate, and
    it says why; the numbered chain on the same page still is."""
    centring_y = ROW_Y + 40
    centring = _line(50, centring_y, 350) + b"".join(
        _slash(x, centring_y) for x in (50.0, 200.0, 350.0)
    )
    words = _text(120, centring_y + 4, "EQ") + _text(270, centring_y + 4, "EQ")
    rows = _rows(SHEET + centring + words)

    rejected = [r for r in rows.rejected if r.y == Decimal(300) - Decimal(centring_y)]
    assert len(rejected) == 1
    assert rejected[0].rejected_because == (
        "no label has a number: a centring or note line, not a row of widths"
    )
    assert any(c.y == Decimal(300) - Decimal(ROW_Y) for c in rows.candidates)


def test_a_row_with_one_numbered_label_among_words_stays_a_candidate() -> None:
    centring_y = ROW_Y + 40
    chain = _line(50, centring_y, 350) + b"".join(
        _slash(x, centring_y) for x in (50.0, 200.0, 350.0)
    )
    labels = _text(120, centring_y + 4, "EQ") + _text(270, centring_y + 4, "24")
    rows = _rows(SHEET + chain + labels)

    assert any(c.y == Decimal(300) - Decimal(centring_y) for c in rows.candidates)
