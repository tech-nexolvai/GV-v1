"""What the AI readers may spend on one drawing set (#757; the admin's $3, decided 2026-10-01).

Verification for: the budget in `workflow/stages.py` — `ai_budget_from_environment`, `_SpendMeter`,
and the check before every model call on the vision route and in the reading agent.

The money here is made up and stated in a price file the test writes: one call of 1,000 tokens in and
1,000 out at $2 per 1,000 costs $4, which is over a $3 cap by itself.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from app.runs.rates import MODEL_RATES_ENV
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from extraction.models.nova import (
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
)
from storage.local import LocalStore
from tests.workflow.test_association import _revision
from tests.workflow.test_glyph_route import (  # noqa: F401 - fixtures
    SHEET,
    _glyph_rows,
    _settings,
    _stages,
    _templates,
    session,
    store,
)
from units.measurement import Unit
from workflow.glyph_route import GlyphRoute
from workflow.stages import (
    AI_BUDGET_ENV,
    DEFAULT_AI_BUDGET_USD,
    _SpendMeter,
    ai_budget_from_environment,
)

# ---------------------------------------------------------------------------
# The setting
# ---------------------------------------------------------------------------


def test_the_admins_three_dollars_is_the_cap_unless_a_deployment_states_another() -> None:
    assert DEFAULT_AI_BUDGET_USD == Decimal(3)
    assert ai_budget_from_environment({}) == Decimal(3)
    assert ai_budget_from_environment({AI_BUDGET_ENV: "5.50"}) == Decimal("5.50")


@pytest.mark.parametrize("stated", ["three", "0", "-1", "NaN", "Infinity"])
def test_a_cap_that_is_not_an_amount_is_refused(stated: str) -> None:
    with pytest.raises(ValueError, match=AI_BUDGET_ENV):
        ai_budget_from_environment({AI_BUDGET_ENV: stated})


# ---------------------------------------------------------------------------
# The meter
# ---------------------------------------------------------------------------


def test_the_meter_is_reached_at_the_cap_not_after_it() -> None:
    meter = _SpendMeter(cap_micros=3_000_000)

    meter.add(2_999_999)
    assert not meter.reached
    meter.add(1)
    assert meter.reached
    assert "$3.00" in meter.reason


def test_a_call_without_a_price_is_counted_not_hidden() -> None:
    """A model with no stated price has no cost to add (#700), so it is counted by number."""
    meter = _SpendMeter(cap_micros=3_000_000)

    meter.add(None)
    meter.add(None)

    assert meter.unpriced_calls == 2
    assert meter.as_payload() == {
        "cap_usd": "3",
        "spent_usd": "0",
        "calls_without_a_price": 2,
        "reached": False,
    }


# ---------------------------------------------------------------------------
# On a page
# ---------------------------------------------------------------------------


class _PricedReader:
    """A vision reader whose every call is recorded at 1,000 tokens each way, and counted."""

    def __init__(self, extractor: str, reading: str) -> None:
        self.config = NovaConfig(
            model_id=extractor,
            prompt_id="dimension-reader-v1",
            template_id="bounded-crop-v1",
            connect_timeout_seconds=1,
            read_timeout_seconds=1,
            max_attempts=1,
            extractor=extractor,
        )
        self._reading = reading
        self.calls = 0

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        self.calls += 1
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=1000,
                output_tokens=1000,
                outcome=NovaInvocationOutcome.OK,
                request_id=None,
                context=request.context,
                bound_pt=request.bound_pt,
                injection_attempts=(),
            )
        )
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text=self._reading,
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(10, 0), ImagePoint(10, 5), ImagePoint(0, 5)),
            confidence=None,
            ambiguity_flags=(),
        )


def test_once_a_set_has_spent_its_cap_no_more_models_are_asked(
    session: Session,  # noqa: F811 - fixture
    store: LocalStore,  # noqa: F811 - fixture
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The cap at work.** The first reader's one call costs $4, over the $3 cap by itself. Outcome:
    the second reader is never asked, the page says why, and it reports the spend."""
    prices = tmp_path / "prices.json"
    prices.write_text(
        json.dumps(
            {
                "source": "made up for this test",
                "retrieved": "2026-10-01",
                "currency": "USD",
                "rates": {
                    name: {"input_per_1k_tokens": "2", "output_per_1k_tokens": "2"}
                    for name in ("priced-reader-a", "priced-reader-b")
                },
            }
        )
    )
    monkeypatch.setenv(MODEL_RATES_ENV, str(prices))
    first = _PricedReader("priced-reader-a", '12"')
    second = _PricedReader("priced-reader-b", '12"')
    revision = _revision(session, store, data=SHEET)
    session.commit()

    stages = _stages(store, GlyphRoute(_templates(), _settings()), (first, second))
    (result,) = stages.extract_pages(session, revision.id)
    session.commit()

    assert first.calls == 1
    assert second.calls == 0
    assert result.payload["ai_budget"] == {
        "cap_usd": "3",
        "spent_usd": "4",
        "calls_without_a_price": 0,
        "reached": True,
    }
    assert any("spent its AI budget of $3.00" in why for why in result.payload["vision_refusals"])
    # The glyph reading is still recorded: the cap withholds model readings, never the file's own.
    assert len(_glyph_rows(session)) == 1


def test_the_reading_agent_asks_no_model_once_the_cap_is_reached() -> None:
    """Outcome: the agent's read fails with the reason — a failure its graph turns into a label
    handed to a reviewer — and the reader is never asked."""
    from extraction.agent.graph import RetryableToolFailure
    from extraction.agent.tools import VlmReadingArguments, VlmRole
    from workflow.stages import _AgentReads

    reader = _PricedReader("priced-reader-a", '12"')
    meter = _SpendMeter(cap_micros=3_000_000, spent_micros=3_000_000)
    reads = _AgentReads(
        session=None,  # type: ignore[arg-type] - never reached: the cap refuses first
        page_index=0,
        crops=None,  # type: ignore[arg-type]
        readers={VlmRole.PRIMARY: reader},  # type: ignore[dict-item]
        open_run=lambda _reader: None,  # type: ignore[arg-type,return-value]
        meter=meter,
    )

    outcome = reads.read(
        VlmReadingArguments(region_id="r1", crop_artifact_id="c1", role=VlmRole.PRIMARY)
    )

    assert isinstance(outcome, RetryableToolFailure)
    assert "AI budget" in str(outcome)
    assert reader.calls == 0
