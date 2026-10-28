"""A count of drawn things is never sent to the readers (2026-10-28). $0, made-up findings.

"7 outlets" against "3 outlets" has no printed number: a reading, asked to copy the printed value,
came back with nothing on every such item of a real upload. Such an item is not read; a would-be
red is marked "not double-read" with the reason instead, so it is never red without a reading
(it could not be red from a reading anyway). Every other item is read as before.
"""

from __future__ import annotations

import pytest

from app.review.assistant import proof, reading_routes
from app.review.assistant.chat_engine import COUNT_WHY, _count_only, could_go_red
from tests.review.assistant.test_double_read_hold import _proved
from tests.review.assistant.test_second_reads_first import _engine, _finding, _queue


def _count(id_: str, architect: str = "7 outlets", vendor: str = "3 outlets") -> dict:
    return _finding(
        id_,
        "check",
        item="Main buffet: number of outlets along the open middle zone",
        architect=architect,
        vendor=vendor,
        inches={"architect": "7", "vendor": "3"},
    )


@pytest.mark.parametrize(
    ("architect", "vendor", "count"),
    [
        ("7 outlets", "3 outlets", True),
        ("6 pendant lights", "5 pendant lights", True),
        ("2 outlets", "0 outlets", True),
        ("3 duplex outlets", "1 outlet", True),
        ('9"', "254 [10]", False),
        ("7 outlets", '3"', False),  # one side is a dimension: read as before
        ("2 UNITS", "3 UNITS", False),  # a unit line is printed: a reading can copy it
        ("36 AFF", '36"', False),
    ],
)
def test_a_count_is_a_number_of_drawn_things_on_every_side_compared(
    architect: str, vendor: str, count: bool
) -> None:
    assert _count_only(_count("F1", architect, vendor)) is count


def test_a_count_is_never_read_and_a_would_be_red_count_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    red = _count("F1")
    assert could_go_red(red)  # its numbers count as inches: it could go red but for this
    plain = _count("F2", "2 outlets", "0 outlets") | {"inches": None}
    assert not could_go_red(plain)
    size = _finding("F3", "different", inches={"vendor": "31", "architect": "30"})
    assert _queue(engine, [red, plain, size], final=True) == ["F3"]
    assert engine.started == ["second_reads"]
    # The engine's own `_not_double_read` marks only would-be reds (here every call is listed).
    assert ("F1", COUNT_WHY) in engine.unread


def test_a_count_marked_not_double_read_is_never_red() -> None:
    finding = {**_proved(second_read="not_run"), reading_routes.NOT_DOUBLE_READ: COUNT_WHY}
    assert proof.proof_group(finding) == "likely_problem"
    assert reading_routes.held(finding) == f"not double-read: {COUNT_WHY}"
