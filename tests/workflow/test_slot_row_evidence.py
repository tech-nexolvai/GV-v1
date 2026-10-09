from fractions import Fraction
from uuid import uuid4

import pytest

from workflow.slot_row_evidence import _verified_reader_answer


@pytest.mark.parametrize(
    ("text", "stacked", "combined", "expected"),
    [
        ("127 [5]", True, False, Fraction(5)),
        ('17 5/8"', True, False, Fraction(141, 8)),
        ('4"+1"Filler', False, True, Fraction(5)),
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
        Fraction(141, 8)
        if text.startswith("17 ")
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


@pytest.mark.parametrize(
    ("belongs", "supports"),
    [
        (True, True),  # a stored v1 to v3 answer
        ("yes", True),  # claude-slot-span-v4 (#1110)
        (False, False),
        ("no", False),
        ("unsure", False),
        (1, False),
        ("true", False),
        (None, False),
    ],
)
def test_only_a_stored_yes_supports_a_value_in_either_answer_shape(
    belongs: object, supports: bool
) -> None:
    answer = {
        "belongs": belongs,
        "text": '17 3/4"',
        "stacked": False,
        "combined": False,
        "readable": True,
        "no_dimension": False,
    }

    result = _verified_reader_answer(answer, Fraction(71, 4))

    assert (result is not None) is supports


def test_only_a_pass_on_unwitnessed_readings_is_sent_to_the_reviewer() -> None:
    """#1107 (3). Input: a PASS and a FAIL, each with and without readings whose drawn length was
    not checked. Outcome: only the PASS on unwitnessed readings becomes REVIEW_REQUIRED, with the
    engine's PASS in the notes, no decided trace, and the unchecked readings named."""
    from verdict.finding import Finding
    from verdict.outcomes import Outcome, Severity
    from verdict.trace import CalculationTrace
    from vocabulary.drawn_length import NO_WITNESS_REASON
    from workflow.slot_row_evidence import confirm_unwitnessed_pass

    def decided(outcome: Outcome) -> Finding:
        trace = CalculationTrace(
            operation="synthetic",
            operands=(),
            intermediates=(),
            comparison="synthetic",
            tolerance=None,
            arithmetic_unit=None,
            outcome=outcome,
            engine_version="test",
            operation_version="test",
        )
        return Finding(
            rule_id="CT-WIDTH-001",
            outcome=outcome,
            severity=Severity.CRITICAL,
            reason="synthetic comparison",
            snapshot_id="snapshot",
            engine_version="test",
            trace=trace,
            notes=("Wall layout: synthetic.",),
        )

    passed = decided(Outcome.PASS)
    failed = decided(Outcome.FAIL)
    assert confirm_unwitnessed_pass(passed, ()) is passed
    assert confirm_unwitnessed_pass(failed, (0, None)) is failed

    held = confirm_unwitnessed_pass(passed, (None, 1, 0))
    assert held.outcome is Outcome.REVIEW_REQUIRED
    assert held.reason == NO_WITNESS_REASON
    assert held.trace is None
    assert held.notes == (
        (
            "The engine's result, which counts only once a person confirms the values: "
            "PASS — synthetic comparison"
        ),
        "Drawn length not checked (no scale): piece 1, piece 2, the overall.",
        "Wall layout: synthetic.",
    )
