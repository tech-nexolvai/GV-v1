"""A width FAIL in plain words (Set 3 QA). Synthetic values only.

The engine's comparison reads "70 in != 62 in". The finding's message (what the screen, the PDFs
and the Assistant show) now says what was needed and what the vendor wrote, like the wall-case
wording. Nothing is worked out again: every number is the engine's own (trace) or a sealed
operand, the outcome is unchanged, and the engine's words are kept in the notes.
"""

from __future__ import annotations

from tests.workflow.test_wall_cases import (  # noqa: F401  (the operations fixture)
    _field_cut,
    _operands,
    _registered_operations,
    _snapshot,
)
from verdict.engine import execute
from verdict.outcomes import Outcome
from workflow.wall_cases import plain_width_failure


def _run(overall: int, pieces: tuple[int, ...], walls: str, cut: int = 1) -> object:
    return execute(
        _snapshot(),
        _operands(overall, pieces),
        _field_cut(cut),
        discriminators={"wall_config": walls},
    )


def test_a_width_fail_says_what_was_needed_and_what_the_vendor_wrote() -> None:
    engine = _run(70, (40, 11, 11), "back_left_right")
    assert engine.outcome is Outcome.FAIL
    assert "!=" in engine.reason
    plain = plain_width_failure(engine, _operands(70, (40, 11, 11)))
    assert plain.outcome is Outcome.FAIL
    assert plain.trace == engine.trace
    assert plain.reason == (
        'Needed 64" (40 + 11 + 11 + 1" field cut at each wall end); the vendor wrote 70".'
    )
    assert any(engine.reason in note for note in plain.notes)


def test_one_wall_end_and_no_wall_ends_are_said_too() -> None:
    one = plain_width_failure(_run(70, (40, 20), "back_and_left"), _operands(70, (40, 20)))
    assert (
        one.reason
        == 'Needed 61" (40 + 20 + 1" field cut at the one wall end); the vendor wrote 70".'
    )
    none = plain_width_failure(_run(70, (40, 20), "back_only"), _operands(70, (40, 20)))
    assert none.reason == 'Needed 60" (40 + 20, no field cut); the vendor wrote 70".'


def test_a_pass_is_left_as_it_is() -> None:
    engine = _run(62, (40, 20), "back_left_right")
    assert engine.outcome is Outcome.PASS
    assert plain_width_failure(engine, _operands(62, (40, 20))) is engine
