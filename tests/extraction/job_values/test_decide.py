"""The job values reader's six core cases, with mocked readers and made-up drawings (#1407).

No model is called: the readers are stubs, and the budget case proves no call is made."""

from __future__ import annotations

import io
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from PIL import Image

from app.runs.rates import load_model_rates
from extraction.architect.text import Orientation, TextChar
from extraction.geometry.rows import Box
from extraction.ink import InkClass
from extraction.job_values.decide import (
    Outcome,
    Panel,
    ReaderReading,
    combine,
    decide_panel,
    form_suggestion,
)
from extraction.job_values.read import (
    CLAUDE_PAIR,
    MEASURED_JOB_VALUES_SETTINGS,
    PreparedPanel,
    read_job_values,
)
from extraction.slot_reader.anthropic import BatchSpendGuard

RATES = Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
OPUS, SONNET = CLAUDE_PAIR


def _line(text: str, x: int, y: int, *, width: int = 5, height: int = 6) -> list[TextChar]:
    return [
        TextChar(
            ch,
            Box(
                Decimal(x + i * width),
                Decimal(y),
                Decimal(x + (i + 1) * width),
                Decimal(y + height),
            ),
            Orientation.UPRIGHT,
        )
        for i, ch in enumerate(text)
        if ch != " "
    ]


def _panel(*, ink: InkClass | None = InkClass.VENDOR, role: str = "arch") -> Panel:
    # A made-up architect section: a note stating the overhang at (100, 200), in points on a
    # 1000 x 1000 drawing, so a reader's 0-1000 box is the same numbers.
    return Panel(
        "doc-1",
        3,
        1,
        role,  # type: ignore[arg-type]
        Box(Decimal(0), Decimal(0), Decimal(1000), Decimal(1000)),
        tuple(_line('1 1/2" OVERHANG', 100, 200)),
        lambda box: ink,
    )


def _printed(reader: str, text: str, *, unit: str = "inch") -> ReaderReading:
    return ReaderReading(reader, "printed", text, unit, "OVERHANG", (99, 199, 136, 207))


def test_both_readers_agree_and_code_finds_it_printed_fills_the_value() -> None:
    panel = _panel()
    decision = decide_panel(
        panel, "countertop_overhang", _printed(OPUS, '1 1/2"'), _printed(SONNET, '1 1/2"')
    )
    assert decision.outcome is Outcome.AUTO
    result = combine("countertop_overhang", [decision], [])
    assert result.outcome is Outcome.AUTO and result.value == Fraction(3, 2)
    shown = form_suggestion(result)
    assert shown is not None
    assert shown["filled"] is True and shown["value"] == '1 1/2"' and shown["page"] == 4
    assert shown["source"] == "ai-pair"


def test_readers_disagree_is_a_suggestion_listing_both() -> None:
    decision = decide_panel(
        _panel(), "countertop_overhang", _printed(OPUS, '1 1/2"'), _printed(SONNET, '1"')
    )
    assert decision.outcome is Outcome.SUGGESTION
    result = combine("countertop_overhang", [decision], [])
    assert result.outcome is Outcome.SUGGESTION and result.value is None
    shown = form_suggestion(result)
    assert shown is not None and shown["filled"] is False
    choices = shown["choices"]
    assert isinstance(choices, list)
    assert {choice["value"] for choice in choices} == {'1 1/2"', '1"'}


def test_millimetres_only_are_refused() -> None:
    decision = decide_panel(
        _panel(),
        "countertop_overhang",
        _printed(OPUS, "38 mm", unit="mm"),
        _printed(SONNET, "38mm", unit="inch"),
    )
    assert decision.outcome is Outcome.REFUSED
    assert all("millimetres" in reason for reason in decision.reasons)
    result = combine("countertop_overhang", [decision], [])
    assert result.outcome is Outcome.REFUSED and form_suggestion(result) is None


def test_a_value_under_gv_markup_is_refused() -> None:
    decision = decide_panel(
        _panel(ink=InkClass.GV),
        "countertop_overhang",
        _printed(OPUS, '1 1/2"'),
        _printed(SONNET, '1 1/2"'),
    )
    assert decision.outcome is Outcome.REFUSED
    assert all("GV's coloured markup" in reason for reason in decision.reasons)
    assert form_suggestion(combine("countertop_overhang", [decision], [])) is None


def test_not_printed_is_nothing() -> None:
    nothing = ReaderReading(OPUS, "not_printed"), ReaderReading(SONNET, "not_printed")
    decision = decide_panel(_panel(), "cabinet_depth", *nothing)
    assert decision.outcome is Outcome.NOTHING
    result = combine("cabinet_depth", [decision], [])
    assert result.outcome is Outcome.NOTHING and result.value is None
    assert form_suggestion(result) is None


class _NeverCalled:
    calls = 0

    def converse(self, **_: object) -> dict[str, object]:
        type(self).calls += 1
        raise AssertionError("no call may be sent past the budget")


def test_the_budget_stops_the_reading_before_any_call() -> None:
    picture = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(picture, format="PNG")
    blank = Panel(
        "doc-1",
        0,
        1,
        "arch",
        Box(Decimal(0), Decimal(0), Decimal(9), Decimal(9)),
        (),
        lambda box: InkClass.VENDOR,
    )
    prepared = PreparedPanel(blank, picture.getvalue(), 0, "a" * 64)
    attempts: list[object] = []
    reading = read_job_values(
        [prepared],
        clients={OPUS: _NeverCalled(), SONNET: _NeverCalled()},
        settings=MEASURED_JOB_VALUES_SETTINGS,
        record_attempt=attempts.append,
        spend_guard=BatchSpendGuard(Decimal("0.01"), load_model_rates(RATES)),
    )
    assert _NeverCalled.calls == 0 and attempts == []
    assert reading.stopped == "budget"
    for result in reading.results.values():
        assert result.outcome is Outcome.NOTHING
        assert any("budget" in reason for reason in result.reasons)
