from fractions import Fraction
from uuid import uuid4

import pytest

from workflow.slot_row_evidence import _verified_reader_answer


@pytest.mark.parametrize(
    ("text", "stacked", "combined", "expected"),
    [
        ("127 [5]", True, False, Fraction(5)),
        ('13 1/8"', True, False, Fraction(105, 8)),
        ('3"+2"Filler', False, True, Fraction(5)),
        ("3/4", True, False, None),
        ('55"', False, False, Fraction(55)),
        ('56"', False, False, None),
    ],
)
def test_saved_reader_text_must_parse_to_the_sealed_exact_value(
    text: str, stacked: bool, combined: bool, expected: Fraction | None
) -> None:
    answer = {
        "belongs": True,
        "text": text,
        "stacked": stacked,
        "combined": combined,
        "readable": True,
        "no_dimension": False,
    }

    expected_value = (
        Fraction(105, 8)
        if text.startswith("13 ")
        else Fraction(55) if text == '55"' else Fraction(5)
    )
    result = _verified_reader_answer(answer, expected_value)

    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert result.exact == expected


def test_sealed_exact_stacked_reading_is_eligible_for_its_row() -> None:
    from workflow.slot_row_scope import SlotRowReading, qualify_slot_row

    def reading(position: int | None, flags: frozenset[str] = frozenset()) -> SlotRowReading:
        return SlotRowReading(
            candidate_id=uuid4(),
            position=position,
            value_numerator=13,
            value_denominator=1,
            unit="in",
            status="CORROBORATED",
            lane="SECOND_READER",
            flags=flags,
        )

    result = qualify_slot_row(
        (
            reading(None),
            reading(0, frozenset({"reader-id:opus", "reader-id:sonnet", "stacked"})),
        ),
        expected_piece_count=1,
        held_reason=None,
        shop_document=True,
    )

    assert result.eligible
