from uuid import uuid4

import pytest

from workflow.slot_row_scope import (
    SlotRowReading,
    effective_row_wall,
    qualify_slot_row,
)


def _reading(position: int | None, **changes: object) -> SlotRowReading:
    values: dict[str, object] = {
        "candidate_id": uuid4(),
        "position": position,
        "value_numerator": 10,
        "value_denominator": 1,
        "unit": "in",
        "status": "CORROBORATED",
        "lane": "SECOND_READER",
        "flags": frozenset({"reader-id:kimi", "reader-id:qwen"}),
    }
    values.update(changes)
    return SlotRowReading(**values)  # type: ignore[arg-type]


def test_only_complete_same_row_values_qualify() -> None:
    result = qualify_slot_row(
        (_reading(None), _reading(0), _reading(1)),
        expected_piece_count=2,
        held_reason=None,
        shop_document=True,
    )
    assert result.eligible
    assert result.reason is None


@pytest.mark.parametrize(
    ("readings", "expected", "held", "shop"),
    [
        ((_reading(None), _reading(0)), 2, None, True),  # missing piece
        ((_reading(0), _reading(1)), 2, None, True),  # missing overall
        ((_reading(None), _reading(0), _reading(0)), 1, None, True),  # duplicate position
        ((_reading(None), _reading(0)), 1, "VIF", True),
        ((_reading(None, status="RAW_CANDIDATE"), _reading(0)), 1, None, True),
        ((_reading(None, lane=""), _reading(0)), 1, None, True),
        ((_reading(None, flags=frozenset({"row-partial"})), _reading(0)), 1, None, True),
        ((_reading(None), _reading(0)), 1, None, False),
        ((_reading(None, flags=frozenset({"reader-id:kimi"})), _reading(0)), 1, None, True),
    ],
)
def test_incomplete_held_or_unqualified_row_never_gets_scope(
    readings: tuple[SlotRowReading, ...], expected: int, held: str | None, shop: bool
) -> None:
    result = qualify_slot_row(
        readings,
        expected_piece_count=expected,
        held_reason=held,
        shop_document=shop,
    )
    assert not result.eligible
    assert result.reason


def test_drawing_clue_wall_needs_no_click() -> None:
    assert effective_row_wall(
        layout="back_left_right",
        source="vendor-drawing-clues",
        held=False,
        reviewer_confirmed=None,
    ) == ("back_left_right", None)


def test_readers_wall_is_only_a_prefill_until_this_row_is_confirmed() -> None:
    assert effective_row_wall(
        layout="back_left_right", source="readers", held=False, reviewer_confirmed=None
    ) == (None, "Confirm the proposed wall layout for this countertop row.")
    assert effective_row_wall(
        layout="back_left_right",
        source="readers",
        held=False,
        reviewer_confirmed="back_left_right",
    ) == ("back_left_right", None)


def test_held_wall_cannot_be_overridden_by_a_stale_proposal() -> None:
    layout, reason = effective_row_wall(
        layout="back_left_right",
        source="vendor-drawing-clues",
        held=True,
        reviewer_confirmed=None,
    )
    assert layout is None
    assert reason


def test_person_can_resolve_a_held_wall_for_this_row() -> None:
    assert effective_row_wall(
        layout=None,
        source=None,
        held=True,
        reviewer_confirmed="back_left_right",
    ) == ("back_left_right", None)


def test_person_saved_value_can_replace_an_unsealed_stacked_proposal_on_this_row() -> None:
    readings = (
        _reading(None),
        _reading(
            0,
            status="HUMAN_CONFIRMED",
            lane="HUMAN",
            flags=frozenset({"human-saved-for-row", "stacked"}),
        ),
    )
    result = qualify_slot_row(
        readings,
        expected_piece_count=1,
        held_reason=None,
        shop_document=True,
    )
    assert result.eligible


def test_unsealed_suggestion_is_not_a_row_operand_without_human_save() -> None:
    result = qualify_slot_row(
        (_reading(None), _reading(0, status="RAW_CANDIDATE")),
        expected_piece_count=1,
        held_reason=None,
        shop_document=True,
    )
    assert not result.eligible
    assert result.reason == "A value in this row is not sealed by both readers."
