"""The gate reader goes first; the other vision readers read only where it read a value (#787).

Verification for: `workflow/stages.py` (`_read_page_by_vision`, the `vision_gate` setting).

The one that matters most is `test_a_gated_reader_is_asked_only_where_the_gate_read_a_value`: Nova 2
Lite's quota is 20 requests a minute on this account (#716), so every crop it is spared is time a
package does not wait. Spared only where nothing can be confirmed: a confirmation is two readers'
values agreeing (#775), and the gate read none there.

The page is the two-region content page of `tests/workflow/test_association.py`; the readers stand in
for models and record what they were asked. No model is called.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from extraction.models.context import AssembledContext
from extraction.models.nova import (
    NovaConfig,
    NovaInvocation,
    NovaInvocationOutcome,
    NovaRequest,
)
from storage.local import LocalStore
from tests.workflow.test_association import (
    SETTINGS,
    VISION_SOURCE_REGIONS,
    _revision,
    _upgrade,
)
from tests.workflow.test_markup_route import _SilentOcr
from units.measurement import Unit
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

GATE, OTHER = "gate-reader", "other-reader"


def _config(extractor: str) -> NovaConfig:
    return NovaConfig(
        model_id=f"{extractor}/1",
        prompt_id="dimension-reader-v1",
        template_id="bounded-crop-v1",
        connect_timeout_seconds=1,
        read_timeout_seconds=1,
        max_attempts=1,
        extractor=extractor,
    )


@dataclass
class _Reader:
    """Answers in order from `readings`, recording each request it is asked."""

    config: NovaConfig
    readings: list[str]
    asked: list[NovaRequest] = field(default_factory=list)

    def extract(self, request: NovaRequest, recorder: object) -> DomainCandidate:
        self.asked.append(request)
        recorder.record(  # type: ignore[attr-defined]
            NovaInvocation(
                model_id=self.config.model_id,
                prompt_id=self.config.prompt_id,
                template_id=self.config.template_id,
                attempt=1,
                latency_ms=1,
                input_tokens=10,
                output_tokens=2,
                outcome=NovaInvocationOutcome.OK,
                request_id=f"request-{request.candidate_id}",
                context=AssembledContext(nearby_text=(), nearby_geometry=()),
                bound_pt=Decimal(9),
                injection_attempts=(),
            )
        )
        return DomainCandidate(
            candidate_id=request.candidate_id,
            extractor=self.config.extractor,
            extractor_version=self.config.model_id,
            raw_text=self.readings[len(self.asked) - 1],
            parsed_value=None,
            unit_guess=Unit.INCH,
            semantic_guess=None,
            page=request.page,
            polygon=(ImagePoint(0, 0), ImagePoint(1, 0), ImagePoint(1, 1), ImagePoint(0, 1)),
            confidence=None,
            ambiguity_flags=(),
        )


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    from app.db.session import session_factory

    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _stages(store: LocalStore, readers: tuple[_Reader, ...], gate: str | None) -> DatabaseStages:
    return DatabaseStages(
        store,
        dpi=150,
        association=SETTINGS,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        vision_readers=readers,
        vision_gate=gate,
    )


def _readers() -> tuple[_Reader, _Reader]:
    """The other reader is listed first, so the gate going first is the stage's doing."""
    other = _Reader(_config(OTHER), readings=['24"', '24"'])
    gate = _Reader(_config(GATE), readings=['24"', "LED"])
    return other, gate


def test_a_gated_reader_is_asked_only_where_the_gate_read_a_value(
    session: Session, store: LocalStore
) -> None:
    """**The point.** Two regions; the gate reads `24"` in one and `LED`, no value, in the other.
    Outcome: the other reader is asked about the first only, and the page says one was held back."""
    other, gate = _readers()
    revision = _revision(session, store, data=VISION_SOURCE_REGIONS)
    session.commit()

    (result,) = _stages(store, (other, gate), GATE).extract_pages(session, revision.id)
    session.commit()

    assert len(gate.asked) == 2
    assert len(other.asked) == 1
    assert result.payload["vision_gate_held_back"] == 1


def test_without_a_gate_every_reader_reads_every_region(
    session: Session, store: LocalStore
) -> None:
    other, gate = _readers()
    revision = _revision(session, store, data=VISION_SOURCE_REGIONS)
    session.commit()

    (result,) = _stages(store, (other, gate), None).extract_pages(session, revision.id)
    session.commit()

    assert (len(gate.asked), len(other.asked)) == (2, 2)
    assert result.payload["vision_gate_held_back"] is None


def test_the_gated_run_names_the_gate_and_a_rerun_reads_nothing_twice(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the gated reader's run says it was gated, and a redelivered stage asks nobody again —
    the gate's recorded rows decide which regions the gated reader had."""
    other, gate = _readers()
    revision = _revision(session, store, data=VISION_SOURCE_REGIONS)
    session.commit()
    stages = _stages(store, (other, gate), GATE)

    stages.extract_pages(session, revision.id)
    session.commit()
    stages.extract_pages(session, revision.id)
    session.commit()

    assert (len(gate.asked), len(other.asked)) == (2, 1)
    runs = {run.extractor: run for run in session.execute(select(ExtractionRun)).scalars()}
    assert f";gate={GATE}" in runs[OTHER].config_hash
    assert ";gate=" not in runs[GATE].config_hash
    other_rows = [
        row
        for row in session.execute(select(ObservationCandidate)).scalars()
        if row.extraction_run_id == runs[OTHER].id
    ]
    assert len(other_rows) == 1


def test_a_gate_that_is_not_a_configured_reader_is_refused(store: LocalStore) -> None:
    with pytest.raises(ValueError, match="nobody"):
        _stages(store, _readers(), "nobody")
