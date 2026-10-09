"""A dual label agreed by more than one pass in one region (#928).

**The case.** The first pass's two readers agree on `914 [36]`; later, the reading agent's two
readers in the same region agree on `915 [36]`. Each pair agreed within itself, so each holds the
second-reader lane, and automatic typing gathers every agreeing reading of the region with the same
inches — four readings, all 36" — and could seal the region on 36". The millimetres differ between
the groups, which shows one of them misread; #924 already refuses that within one pair.

**After #928** a dual label is sealed only where every agreeing group read the same millimetres and
the same inches. Otherwise the region is a conflict, and a reviewer decides:

- the agent's contradiction check (`DatabaseStages._mark_regions_the_agent_contradicted`) marks the
  new readings `CONFLICTING`, which holds the whole region (`automatic_typing` refuses a region with
  a conflicting reading, #790);
- automatic typing itself (`_second_reader_candidate_ids`) refuses such a region, which protects a
  run stored before this check existed.

Same millimetres and inches across the groups still seal; one group is unchanged.

The readings are made up here. No model is called and no client drawing is read.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.evidence.automatic_typing import _second_reader_candidate_ids
from app.evidence.record import open_extraction_run
from app.models import ExtractionRun, ObservationCandidate
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_cross_route_corroboration import _HoldsNothingBack, _revision, _upgrade
from workflow.stages import DatabaseStages, _MixedFractionGuard

pytest_plugins = ("tests.app.postgres_fixture",)

REGION = ((10, 10), (20, 10), (20, 20), (10, 20))

#: The first pass's pair (#907) and the reading agent's pair: four readers, every pair two vendors.
QWEN = ("bedrock-qwen3-vl-235b", "qwen.qwen3-vl-235b-a22b")
NOVA_TAUGHT = ("bedrock-nova-2-lite-taught", "us.amazon.nova-2-lite-v1:0")
AGENT_PRIMARY = ("bedrock-agent-primary", "mistral.ministral-3-14b-instruct")
AGENT_ESCALATION = ("bedrock-agent-escalation", "google.gemma-3-27b-it")


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
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


class _Region:
    """One region of a made-up page, and readings of it by any of the four readers."""

    def __init__(self, session: Session, store: LocalStore) -> None:
        revision = _revision(session, store)
        DatabaseStages(store, missing_space=MISSING_SPACE).extract_pages(session, revision.id)
        template = session.execute(select(ObservationCandidate)).scalars().one()
        first = session.get(ExtractionRun, template.extraction_run_id)
        assert first is not None
        self.session = session
        self.document_version_id: UUID = template.document_version_id
        self.page_id: UUID = template.page_id
        self.runs = {
            extractor: open_extraction_run(
                session,
                task_run_id=first.task_run_id,
                extractor=extractor,
                extractor_version=model_id,
                config_hash="test",
                dpi=first.dpi,
            )
            for extractor, model_id in (QWEN, NOVA_TAUGHT, AGENT_PRIMARY, AGENT_ESCALATION)
        }

    def reading(self, reader: tuple[str, str], text: str, inches: int = 36) -> ObservationCandidate:
        row = ObservationCandidate(
            document_version_id=self.document_version_id,
            page_id=self.page_id,
            extraction_run_id=self.runs[reader[0]].id,
            raw_text=text,
            value_numerator=inches,
            value_denominator=1,
            unit="in",
            unit_guess="in",
            semantic_guess=None,
            polygon=[list(point) for point in REGION],
            coordinate_space="image",
            confidence=None,
            ambiguity_flags=[],
        )
        self.session.add(row)
        return row

    def agree(self, candidates: list[ObservationCandidate]) -> None:
        """The agreement gate, every guard letting the agreement through: only the readings decide."""
        DatabaseStages._apply_cross_route_corroboration(
            self.session,
            page_index=0,
            candidates=candidates,
            gv_mark=_HoldsNothingBack(),  # type: ignore[arg-type]
            cut_label=_HoldsNothingBack(),  # type: ignore[arg-type]
            mixed_fraction=_MixedFractionGuard(),
        )


def _two_passes(
    session: Session, store: LocalStore, first_label: str, agent_label: str, *, saved_first: bool
) -> tuple[list[ObservationCandidate], list[ObservationCandidate]]:
    """The page as the stage leaves it: the first pass's pair agreed, then the agent's pair read
    the same region, was agreed, and the agent's contradiction check ran.

    `saved_first`: the first readings were saved before the agent ran, as a real run's panel step
    saves them (#790), so only the agent's readings can still be marked.
    """
    region = _Region(session, store)
    first = [region.reading(QWEN, first_label), region.reading(NOVA_TAUGHT, first_label)]
    region.agree(first)
    assert {row.corroboration_lane for row in first} == {"SECOND_READER"}, "the first pair agreed"
    if saved_first:
        session.flush()
    agent = [
        region.reading(AGENT_PRIMARY, agent_label),
        region.reading(AGENT_ESCALATION, agent_label),
    ]
    page_rows = first + agent
    region.agree(page_rows)
    assert {row.corroboration_lane for row in agent} == {"SECOND_READER"}, "the agent pair agreed"
    DatabaseStages._mark_regions_the_agent_contradicted(
        session, page_index=0, candidates=page_rows, agent_rows=agent
    )
    session.flush()
    return first, agent


# ---------------------------------------------------------------------------
# The issue's case: not sealed, a conflict for a reviewer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("saved_first", [False, True], ids=["unsaved", "first-pass-saved"])
def test_two_groups_agreeing_on_the_inches_but_not_the_millimetres_are_a_conflict(
    session: Session, store: LocalStore, saved_first: bool
) -> None:
    """**#928.** The first pair agreed on `914 [36]`, the agent's pair on `915 [36]`. Outcome: the
    readings the stage can still mark are `CONFLICTING`, and no reading of the region can be
    sealed — where before each pair kept its agreement and the region could be sealed on 36"."""
    first, agent = _two_passes(session, store, "914 [36]", "915 [36]", saved_first=saved_first)

    markable = agent if saved_first else first + agent
    assert {(row.corroboration_status, row.corroboration_lane) for row in markable} == {
        ("CONFLICTING", "SECOND_READER")
    }
    assert all(_second_reader_candidate_ids(session, row) == () for row in first + agent)


def test_a_stored_region_holding_both_groups_is_not_sealed(
    session: Session, store: LocalStore
) -> None:
    """**A run stored before this check.** Both pairs' readings are saved holding their own
    agreement, with nothing marked conflicting. Outcome: automatic typing still refuses every one of
    them — the seal would rest on four readings that agree on the inches but not the millimetres."""
    region = _Region(session, store)
    rows = [
        region.reading(QWEN, "914 [36]"),
        region.reading(NOVA_TAUGHT, "914 [36]"),
        region.reading(AGENT_PRIMARY, "915 [36]"),
        region.reading(AGENT_ESCALATION, "915 [36]"),
    ]
    for row in rows:
        row.corroboration_status = "RAW_CANDIDATE"
        row.corroboration_lane = "SECOND_READER"
    session.flush()

    assert all(_second_reader_candidate_ids(session, row) == () for row in rows)


# ---------------------------------------------------------------------------
# The neighbours: what must not change
# ---------------------------------------------------------------------------


def test_two_groups_agreeing_on_the_same_millimetres_and_inches_still_seal(
    session: Session, store: LocalStore
) -> None:
    """The control. Both pairs agreed on `914 [36]`. Outcome: nothing is conflicting and every
    reading of the region is offered to the seal, as before."""
    first, agent = _two_passes(session, store, "914 [36]", "914 [36]", saved_first=True)

    rows = first + agent
    assert all(row.corroboration_status != "CONFLICTING" for row in rows)
    expected = {row.id for row in rows}
    assert all(set(_second_reader_candidate_ids(session, row)) == expected for row in rows)


def test_one_group_is_unchanged(session: Session, store: LocalStore) -> None:
    """One pair agreed on `914 [36]` and nothing else read the region. Outcome: its two readings are
    offered to the seal, as before."""
    region = _Region(session, store)
    rows = [region.reading(QWEN, "914 [36]"), region.reading(NOVA_TAUGHT, "914 [36]")]
    region.agree(rows)
    session.flush()

    assert {(row.corroboration_status, row.corroboration_lane) for row in rows} == {
        ("RAW_CANDIDATE", "SECOND_READER")
    }
    expected = {row.id for row in rows}
    assert all(set(_second_reader_candidate_ids(session, row)) == expected for row in rows)


def test_two_groups_agreeing_on_plain_inches_are_unchanged(
    session: Session, store: LocalStore
) -> None:
    """No dual label anywhere: both pairs agreed on `36"`. Outcome: as before — nothing conflicting,
    every reading offered to the seal. The rule is about a dual label's millimetres."""
    first, agent = _two_passes(session, store, '36"', '36"', saved_first=True)

    rows = first + agent
    assert all(row.corroboration_status != "CONFLICTING" for row in rows)
    expected = {row.id for row in rows}
    assert all(set(_second_reader_candidate_ids(session, row)) == expected for row in rows)
