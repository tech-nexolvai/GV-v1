"""A countertop checked by hand says exactly what to check, from the stored records only (#1411).

Each test builds a synthetic row (made-up numbers) and calls the item builder directly: no database,
no model. The block lists every piece (what was read, or why it could not be), the printed overall
with the plain rule, and the walls note only when one was recorded. GV's markup is never shown.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.review.review_items import ArchitectRefOut, ReviewItemOut, _width_items
from app.review.row_location import RowLocation
from app.schemas.visual_ui import (
    AgreementFactsOut,
    CountertopPieceOut,
    CountertopResultOut,
    ExactValueOut,
    HoldOut,
    WallLayoutOut,
)
from verdict.outcomes import Outcome

PAGE_ID = uuid4()
DOC_ID = uuid4()


def _box(left: str, top: str, right: str, bottom: str) -> RowLocation:
    return RowLocation(
        page_id=PAGE_ID,
        document_version_id=DOC_ID,
        page_number=4,
        polygon=[[left, top], [right, top], [right, bottom], [left, bottom]],
    )


ROW_BOX = _box("0.30", "0.70", "0.60", "0.74")
OVERALL_BOX = _box("0.40", "0.66", "0.48", "0.69")


def _exact(whole: int) -> ExactValueOut:
    return ExactValueOut(numerator=str(whole), denominator="1", display=f'{whole}"')


def _piece(index: int, value: int | None = None, kind: str | None = None) -> CountertopPieceOut:
    return CountertopPieceOut(
        index=index,
        value=None if value is None else _exact(value),
        source="missing" if value is None else "sealed",
        kind=kind,
        location=_box(f"0.{30 + 5 * index}", "0.70", f"0.{34 + 5 * index}", "0.74"),
    )


def _result(
    pieces: list[CountertopPieceOut],
    *,
    overall: int | None = None,
    field_cut: int | None = None,
    walls_line: str | None = None,
) -> CountertopResultOut:
    return CountertopResultOut(
        finding_id=uuid4(),
        row_id=uuid4(),
        page_number=4,
        label="Countertop row on page 4",
        row_location=ROW_BOX,
        outcome=Outcome.REVIEW_REQUIRED,
        reviewer_decision=None,
        needs_decision=True,
        printed_overall=None if overall is None else _exact(overall),
        printed_overall_location=None if overall is None else OVERALL_BOX,
        pieces=tuple(pieces),
        field_cut_per_end=None if field_cut is None else _exact(field_cut),
        field_cut_count=None if field_cut is None else 2,
        expected_total=None,
        delta=None,
        wall_layout=WallLayoutOut(
            config=None, label=None, source="not established", line=walls_line
        ),
        agreement=AgreementFactsOut(
            both_readers_agreed_on_row=None, values_agreed=(), code_clue_used=False
        ),
        hold=HoldOut(code="row-held", reason="This row is incomplete."),
    )


def _held(slot: str, flags: list[str], value: int | None = None, reason: str = "") -> Any:
    return SimpleNamespace(
        id=uuid4(),
        page_id=PAGE_ID,
        ambiguity_flags=["slot-reader", f"slot:{slot}", *flags],
        value_numerator=value,
        value_denominator=None if value is None else 1,
        unit=None if value is None else "in",
        review_reason=reason or None,
        corroboration_status="PENDING",
        corroboration_lane=None,
    )


def _items(result: CountertopResultOut, held: list[Any]) -> list[ReviewItemOut]:
    return _width_items(
        result,
        SimpleNamespace(candidates=held),  # type: ignore[arg-type]
        finding=SimpleNamespace(id=result.finding_id or uuid4()),  # type: ignore[arg-type]
        why="A person must check this row.",
        state="open",
        decided_by=None,
        decided_at=None,
        actions=lambda kind, state: [],
        zoom=lambda candidate: f"/zoom/{candidate.id}",
        architect=ArchitectRefOut(),
        layouts=lambda: (),
    )


def _by_hand(items: list[ReviewItemOut]) -> ReviewItemOut:
    found = [item for item in items if item.kind == "countertop_by_hand"]
    assert len(found) == 1
    return found[0]


def _lines(item: ReviewItemOut) -> list[tuple[int | None, str | None, str | None, str | None]]:
    block = item.what_to_check
    assert block is not None
    return [(piece.position, piece.kind, piece.value, piece.note) for piece in block.pieces]


def test_each_piece_says_what_was_read_or_why_not_with_the_overall_rule() -> None:
    """A read filler, an equal-share label, two readers who disagree and a GV-marked piece."""
    result = _result(
        [_piece(0, 2, kind="filler"), _piece(1), _piece(2), _piece(3)],
        overall=96,
    )
    held = [
        _held(
            "1",
            ["ink:vendor", "kind:cabinet", 'reader:model-a:90"(5 EQ)', 'reader:model-b:90"(5 EQ)'],
        ),
        _held("2", ["ink:vendor", "reader:model-a:20", "reader:model-b:28", "readers-differ"]),
        _held("3", ["ink:gv", "reviewer-markup", "reader:model-a:31"], reason="reviewer wrote 31"),
    ]
    item = _by_hand(_items(result, held))

    assert _lines(item) == [
        (1, "filler", '2"', None),
        (2, "cabinet", None, 'written as 90"(5 EQ) — 5 equal pieces in one number'),
        (3, None, None, 'two readers disagree: 20" vs 28"'),
        (4, None, None, "under GV markup — check by hand"),
    ]
    block = item.what_to_check
    assert block is not None
    assert block.overall == '96"'
    assert block.rule == 'The pieces should add up to the overall: 96".'
    assert block.walls_note is None, "no walls or field cut was recorded"
    # GV's markup is never shown as a reading, anywhere in the block.
    assert "31" not in block.model_dump_json()


def test_no_stored_overall_says_so_and_the_walls_note_only_when_recorded() -> None:
    result = _result(
        [_piece(0), _piece(1, 30)],
        field_cut=1,
        walls_line="walls at both ends — chosen by a reviewer",
    )
    held = [_held("0", ["no-label", "kind:unknown", "reader:model-a:", "reader:model-b:"])]
    item = _by_hand(_items(result, held))

    assert _lines(item) == [
        (1, None, None, "no dimension label was found for this piece"),
        (2, None, '30"', None),
    ]
    block = item.what_to_check
    assert block is not None
    assert block.overall is None
    assert "overall" in block.rule and "not stored" in block.rule
    assert block.walls_note is not None
    assert "walls at both ends" in block.walls_note and '1"' in block.walls_note


def test_a_held_overall_with_no_reading_says_why_it_was_not_read() -> None:
    result = _result([_piece(0, 20), _piece(1)])
    held = [
        _held("overall", ["no-label"]),
        _held("1", ["no-label"]),
    ]
    block = _by_hand(_items(result, held)).what_to_check
    assert block is not None
    assert block.overall is None
    assert block.overall_note == "no dimension label was found for the overall"


def test_a_row_of_unread_pieces_shows_the_row_strip_not_the_first_pieces_close_up() -> None:
    """Several pieces with no label: the close-up of piece 1 would show whatever sits in its span
    (a cabinet tag), so there is no close-up and the spot is the whole row's dimension line."""
    result = _result([_piece(0), _piece(1), _piece(2)])
    held = [_held(str(index), ["no-label"]) for index in range(3)]
    item = _by_hand(_items(result, held))

    assert item.zoom_ref is None
    assert item.vendor_ref == ROW_BOX


def test_one_labelled_piece_by_hand_keeps_its_own_close_up() -> None:
    result = _result([_piece(0, 20), _piece(1)], overall=50)
    held = [_held("1", ["ink:gv", "reviewer-markup"])]
    item = _by_hand(_items(result, held))

    assert item.zoom_ref == f"/zoom/{held[0].id}"
    assert item.vendor_ref == result.pieces[1].location


def test_a_whole_row_hold_lists_the_pieces_too() -> None:
    """A row held as a whole (not just its numbers) still lists what was stored for it."""
    result = _result([_piece(0, 20), _piece(1, 22)], overall=42).model_copy(
        update={"hold": HoldOut(code="counter-break", reason="A tall appliance may break it.")}
    )
    item = _by_hand(_items(result, []))

    assert _lines(item) == [(1, None, '20"', None), (2, None, '22"', None)]
    assert item.what_to_check is not None
    assert item.what_to_check.rule == 'The pieces should add up to the overall: 42".'
    assert item.vendor_ref == ROW_BOX


def test_other_items_carry_no_block() -> None:
    result = _result([_piece(0, 20), _piece(1)], overall=50)
    held = [_held("1", ["ink:vendor", "reader:pdf-text-layer:30", "reader:model-a:30"], value=30)]
    items = _items(result, held)
    number = [item for item in items if item.kind == "number"]
    assert number and all(item.what_to_check is None for item in number)


# --- a row held as a whole: its reason once, the pieces' stored values "not used" (#1413) ----------

COUNTER_BREAK = HoldOut(
    code="counter-break",
    reason=(
        "This row includes a tall appliance or range bay; it may be the wall-to-wall line, not "
        "the countertop."
    ),
)
ROW_FLAGS = ["row-hold:counter-break", "row-partial", "ink:vendor"]


def _row_held(
    pieces: list[CountertopPieceOut], hold: HoldOut = COUNTER_BREAK
) -> CountertopResultOut:
    return _result(pieces).model_copy(update={"hold": hold})


def _block(item: ReviewItemOut) -> Any:
    assert item.what_to_check is not None
    return item.what_to_check


def test_a_row_hold_is_said_once_at_the_top_never_on_every_piece() -> None:
    result = _row_held([_piece(0), _piece(1), _piece(2)])
    held = [
        _held(str(index), [*ROW_FLAGS, "one-reader-missing"], reason="only one reader")
        for index in range(3)
    ]
    block = _block(_by_hand(_items(result, held)))

    assert block.row_reason == (
        "This line includes a tall appliance (fridge or range), so it may measure wall to wall, "
        "not the countertop. The pieces below are not used for the check."
    )
    notes = [piece.note for piece in block.pieces]
    assert all(note and "tall appliance" not in note for note in notes), notes
    assert all(note and "row is incomplete" not in note for note in notes), notes
    # Nothing stored for them: each says so in its own words.
    assert [piece.value for piece in block.pieces] == [None, None, None]
    assert notes == ["only one AI reader ran, and no number was recorded for this piece"] * 3
    # The overall line stays as it was.
    assert "not stored" in block.rule


def test_a_row_hold_shows_each_stored_value_tagged_not_used() -> None:
    """Made-up labels: a stored inch value, an `mm [in]` label with nothing stored (its inches are
    the bracket's), a piece with nothing read, and one under GV markup (never shown)."""
    result = _row_held([_piece(0), _piece(1), _piece(2), _piece(3), _piece(4, 30)])
    held = [
        _held("0", [*ROW_FLAGS, "kind:filler", "reader:model-a:254 [10]"], value=10),
        _held("1", [*ROW_FLAGS, "kind:cabinet", "reader:model-a:597 [23 1/2]"]),
        _held("2", [*ROW_FLAGS, "no-label"]),
        _held("3", ["row-hold:counter-break", "ink:gv", "reviewer-markup", "reader:model-a:41"]),
    ]
    block = _block(_by_hand(_items(result, held)))

    assert [
        (piece.position, piece.kind, piece.value, piece.mm, piece.not_used, piece.note)
        for piece in block.pieces
    ] == [
        (1, "filler", '10"', "254 mm", True, None),
        (2, "cabinet", '23 1/2"', "597 mm", True, None),
        (3, None, None, None, False, "no dimension label was found for this piece"),
        (4, None, None, None, False, "under GV markup — check by hand"),
        (5, None, '30"', None, True, None),
    ]
    assert "41" not in block.model_dump_json(), "GV's markup is never shown"


def test_a_label_with_no_inch_mark_is_not_shown_as_inches() -> None:
    """`457` alone could be millimetres: only a stored inch value, an `mm [in]` bracket or an inch
    mark is shown as inches."""
    result = _row_held([_piece(0)])
    held = [_held("0", [*ROW_FLAGS, "reader:model-a:457"])]
    piece = _block(_by_hand(_items(result, held))).pieces[0]
    assert piece.value is None
    assert piece.not_used is False


def test_other_row_holds_are_said_once_in_their_own_words() -> None:
    result = _row_held(
        [_piece(0, 20)],
        HoldOut(code="stone-into-walls", reason="the stone runs into the walls (pockets)"),
    )
    block = _block(_by_hand(_items(result, [])))
    assert block.row_reason == (
        "The stone runs into the walls (pockets). The pieces below are not used for the check."
    )
    assert block.pieces[0].not_used is True


def test_a_number_level_hold_has_no_row_reason_and_keeps_its_pieces_in_use() -> None:
    result = _result([_piece(0, 20), _piece(1)], overall=50)
    held = [_held("1", ["no-label"])]
    block = _block(_by_hand(_items(result, held)))
    assert block.row_reason is None
    assert all(piece.not_used is False for piece in block.pieces)
