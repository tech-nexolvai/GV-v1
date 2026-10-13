"""A reviewer's architect pairing survives "Read the unread pages", or holds its row (#1206).

Verification for `app/lifecycle/carry_forward.carry_row_answers_within_revision` (the pairing half)
as `workflow/stages.DatabaseStages.read_unread_pages` calls it after a newer reading of the same
revision. The final review of #1209 found that a reviewer's pairing, tied to the old row's anchor,
quietly gave way to the new row's automatic code+ais pairing. Synthetic drawings and fake readers
(`tests/workflow/test_separate_architect_production_chain.py`); no network, no model.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.architect_matches import pick_architect_view
from app.audit.events import AuditEvent
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.lifecycle.carry_forward import carry_row_answers_within_revision
from app.models import Package
from app.models.evidence import ArchitectPairingRecord
from app.schemas.architect_matches import ArchitectViewPickIn
from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow import test_separate_architect_production_chain as chain
from workflow.architect_pairing_records import (
    DecidedPair,
    architect_spans_for_row,
    latest_architect_pairing,
    latest_record,
    record_reviewer_pairing,
)
from workflow.slot_row_scope import latest_slot_reader_run, slot_rows
from workflow.stages import DatabaseStages, _SpendMeter

pytest_plugins = ("tests.app.postgres_fixture",)

REVIEWER = "reviewer (synthetic)"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _slot(store: LocalStore, readers: Any) -> DatabaseStages:
    plain = chain.runtime(readers, claude_row_reader=True)
    runtime = replace(plain, form=replace(plain.form, rates=chain.PricedRates()))
    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=chain._SilentOcr(),
        missing_space=chain.MISSING_SPACE,
        form_reader=runtime.form,
        slot_reader=runtime,
        architect_reader=MEASURED_ARCHITECT_SETTINGS,
    )


def _reviewed_row(
    session: Session, store: LocalStore, *, pairs: str
) -> tuple[Any, DatabaseStages, Any, ArchitectPairingRecord]:
    """The vendor row read, the reviewer's pick of the architect view, then the reviewer's own
    pairing: `pairs` "none" (nothing comparable) or "chosen" (the first comparable span with the
    first piece)."""
    revision = chain._upload(session, store, chain.VENDOR)
    chain._add_architectural(session, store, revision, chain._architect_file())
    reading = chain._stages(store)
    reading.extract_pages(session, revision.id)
    session.commit()
    readers = chain.Readers()
    slot = _slot(store, readers)
    slot._meter = _SpendMeter(cap_micros=1_000_000_000)
    slot._architect_pages = dict(reading._architect_pages)
    slot._architect_view_crops = dict(reading._architect_view_crops)
    chain._read_slots(session, slot, revision, readers)
    first = chain._anchor(session)
    automatic = chain._match(session, first)
    package = session.get_one(Package, revision.package_id)
    principal = Principal(
        id=REVIEWER, roles=frozenset({Role.REVIEWER}), projects=frozenset({package.project_id})
    )
    pick_architect_view(
        principal,
        principal,
        session,
        package.project_id,
        package.id,
        first.id,
        ArchitectViewPickIn(
            view_id=UUID(str(automatic.candidates[1]["view_id"])),
            expected_record_id=automatic.id,
        ),
    )
    chain._read_slots(session, slot, revision, readers)
    anchor = chain._anchor(session)
    row = next(row for row in slot_rows(session, revision.id) if row.anchor.id == anchor.id)
    chosen: list[DecidedPair] = []
    if pairs == "chosen":
        current = latest_record(session, anchor.id)
        span = next(
            span for span in architect_spans_for_row(session, anchor, current) if span.comparable
        )
        chosen = [DecidedPair("piece", span.candidate.id, (0,))]
    decision = record_reviewer_pairing(
        session,
        anchor=anchor,
        package_revision_id=revision.id,
        piece_count=row.piece_count,
        pairs=chosen,
        note=None,
        actor=REVIEWER,
    )
    session.commit()
    mine = latest_architect_pairing(session, anchor.id)
    assert mine is not None and (mine.source, mine.record_id) == ("reviewer", decision.id)
    return revision, slot, readers, decision


def _newer_reading(
    session: Session, slot: DatabaseStages, revision: Any, readers: Any
) -> tuple[Any, Any]:
    """A newer reading of the same revision, then the carry "Read the unread pages" makes."""
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None
    chain._read_slots(session, slot, revision, readers)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    return chain._anchor(session), carried


def test_a_reviewers_none_comparable_survives_a_newer_reading(
    session: Session,
    store: LocalStore,
) -> None:
    """Before: the new row took a fresh code+ais pairing with 3 pairs, and nothing asked again."""
    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none")

    anchor, carried = _newer_reading(session, slot, revision, readers)

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None
    assert (now.source, now.pairs) == ("reviewer", ())
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.row_anchor_candidate_id == anchor.id != decision.row_anchor_candidate_id
    assert (record.decided_by, record.details["carried_from_id"]) == (REVIEWER, str(decision.id))
    assert (carried.pairings, carried.pairings_held) == (1, 0)
    audit = session.scalars(select(AuditEvent).where(AuditEvent.target_id == record.id)).one()
    assert audit.actor == "read the unread pages (#1206)"


def test_a_reviewers_chosen_pairs_survive_a_newer_reading(
    session: Session,
    store: LocalStore,
) -> None:
    revision, slot, readers, decision = _reviewed_row(session, store, pairs="chosen")

    anchor, carried = _newer_reading(session, slot, revision, readers)

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None and now.source == "reviewer"
    assert [(pair.architect_candidate_id, pair.vendor_slot_indices) for pair in now.pairs] == [
        (UUID(str(decision.pairs[0]["architect_candidate_id"])), (0,))
    ]
    assert (carried.pairings, carried.pairings_held) == (1, 0)


def test_a_row_the_newer_reading_reads_differently_is_held_naming_the_reviewers_decision(
    session: Session,
    store: LocalStore,
) -> None:
    """The newer reading reads a different value on the row, so it is not provably the row the
    reviewer paired. Its pairing is held, naming the earlier decision, never the automatic one."""
    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none")
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None
    chain_texts = dict(chain.TEXTS)
    chain.TEXTS[0] = '25"'  # the first piece now reads differently: the row's values change
    try:
        chain._read_slots(session, slot, revision, readers)
    finally:
        chain.TEXTS.clear()
        chain.TEXTS.update(chain_texts)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    anchor = chain._anchor(session)

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None
    assert (now.source, now.pairs) == ("none", ())
    assert any(
        reason.startswith(f"{REVIEWER} paired this row on the earlier reading")
        and "Pair it again" in reason
        for reason in now.reasons
    )
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["earlier_reviewer_record_id"] == str(decision.id)
    assert (carried.pairings, carried.pairings_held) == (0, 1)


def test_through_read_the_unread_pages_the_reviewers_pairing_still_counts(
    session: Session,
    store: LocalStore,
) -> None:
    """The stage's own path: the first reading stops at the page's stone-end question (the page is
    kept, held for the budget), the reviewer picks the architect view and states that nothing is
    comparable, then "Read the unread pages" reads the architect's drawings and the page again. The
    reviewer's pairing is still the one that counts on the new row."""
    from extraction.slot_reader.anthropic import SpendCapExceeded
    from tests.workflow.test_budget_stop import STONE_ENDS, _asks
    from tests.workflow.test_slot_reader import claude_crops_to_texts

    class Readers(chain.Readers):
        def __init__(self, *, stop: bool) -> None:
            super().__init__()
            self.stop = stop

        def converse(self, **kwargs: Any) -> Any:
            if self.stop and _asks(kwargs, STONE_ENDS):
                raise SpendCapExceeded("Claude reader batch spend cap would be exceeded")
            return super().converse(**kwargs)

    def stages(readers: Readers) -> DatabaseStages:
        from tests.workflow.test_budget_stop import _stages

        built = _stages(store, readers, architect_reader=MEASURED_ARCHITECT_SETTINGS)
        original = built._slot_page

        def slot_page(*args: Any, **kwargs: Any) -> Any:
            page = original(*args, **kwargs)
            if not isinstance(page, str):
                readers.lookup = claude_crops_to_texts(page, chain.TEXTS)
            return page

        built._slot_page = slot_page  # type: ignore[method-assign]
        return built

    revision = chain._upload(session, store, chain.VENDOR)
    chain._add_architectural(session, store, revision, chain._architect_file())
    stages(Readers(stop=True)).extract_pages(session, revision.id)
    session.commit()
    first = chain._anchor(session)
    automatic = chain._match(session, first)
    package = session.get_one(Package, revision.package_id)
    principal = Principal(
        id=REVIEWER, roles=frozenset({Role.REVIEWER}), projects=frozenset({package.project_id})
    )
    pick_architect_view(
        principal,
        principal,
        session,
        package.project_id,
        package.id,
        first.id,
        ArchitectViewPickIn(
            view_id=UUID(str(automatic.candidates[1]["view_id"])),
            expected_record_id=automatic.id,
        ),
    )
    row = next(row for row in slot_rows(session, revision.id) if row.anchor.id == first.id)
    decision = record_reviewer_pairing(
        session,
        anchor=first,
        package_revision_id=revision.id,
        piece_count=row.piece_count,
        pairs=[],
        note=None,
        actor=REVIEWER,
    )
    session.commit()

    result = stages(Readers(stop=False)).read_unread_pages(session, revision.id)
    session.commit()

    anchor = chain._anchor(session)
    assert anchor.id != first.id
    now = latest_architect_pairing(session, anchor.id)
    assert now is not None and (now.source, now.pairs) == ("reviewer", ())
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["carried_from_id"] == str(decision.id)
    assert (result["pairings_carried"], result["pairings_held"]) == (1, 0)
    # The reviewer's pick of the architect view is carried by #1166's own rule.
    assert chain._match(session, anchor).source == "carried"
