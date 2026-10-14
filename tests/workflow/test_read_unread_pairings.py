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


def _slot(store: LocalStore, readers: Any, *, packets: bool = False) -> DatabaseStages:
    """`packets`: as production builds it, the Claude readers store their question packets (#1225),
    so the row's evidence is complete and the check stage compares it (#1210's sign-off test)."""
    plain = replace(chain.runtime(readers, claude_row_reader=True), question_packets=packets)
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
    session: Session, store: LocalStore, *, pairs: str, packets: bool = False
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
    slot = _slot(store, readers, packets=packets)
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


# --- #1210: a newer reading that puts the row at another rank --------------------------------------


def _read_at_rank(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    slot: DatabaseStages,
    revision: Any,
    readers: Any,
    *,
    rank: int,
    texts: dict[int | None, str] | None = None,
    reader_version: str | None = None,
    dx_px: int = 0,
) -> None:
    """A newer reading whose chosen row is recorded at `rank` (the readers picked another of the
    page's ranked rows, or code ranked the page's rows differently), read with `texts` and recorded
    under `reader_version` when given. `dx_px` moves every piece and label of the row sideways by
    that many pixels: an identical run drawn beside the paired one, at the same height (#1210
    review: mirrored elevations side by side)."""
    from workflow import stages as stages_module
    from workflow.slot_reader import persist_slot_readings as original

    if reader_version is not None:
        monkeypatch.setattr(stages_module, "SLOT_READER_VERSION", reader_version)

    def beside(owner: Any) -> Any:
        if owner is None or not dx_px:
            return owner

        def moved(box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
            return (box[0] + dx_px, box[1], box[2] + dx_px, box[3])

        return replace(
            owner,
            band_px=moved(owner.band_px),
            labels=tuple(
                replace(label, box_px=moved(label.box_px), crop_px=moved(label.crop_px))
                for label in owner.labels
            ),
        )

    def at_rank(target: Session, *, results: Any, **kwargs: Any) -> int:
        moved = [
            (
                result
                if result.plan.row is None
                else replace(
                    result,
                    plan=replace(result.plan, row=replace(result.plan.row, rank=rank)),
                    slots=tuple(beside(owner) for owner in result.slots),
                    overall=beside(result.overall),
                )
            )
            for result in results
        ]
        return original(target, results=moved, **kwargs)

    saved = dict(chain.TEXTS)
    monkeypatch.setattr(stages_module, "persist_slot_readings", at_rank)
    if texts is not None:
        chain.TEXTS.update(texts)
    try:
        chain._read_slots(session, slot, revision, readers)
    finally:
        monkeypatch.undo()
        chain.TEXTS.clear()
        chain.TEXTS.update(saved)


def _rank_of(anchor: Any) -> str:
    return str(
        next(
            flag.removeprefix("row-rank:")
            for flag in anchor.ambiguity_flags
            if flag.startswith("row-rank:")
        )
    )


def test_the_same_row_at_another_rank_keeps_the_reviewers_pairing_and_wall_answer(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same physical row (same page bytes, line, pieces and values) recorded at another rank:
    the reviewer's pairing and wall answer follow the row, not the rank (#1210). Before, the
    pairing found no row at its old rank and was dropped with nothing held."""
    from app.models.evidence import SlotRowReviewDecision

    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none")
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None
    old = chain._anchor(session)
    wall = SlotRowReviewDecision(
        row_candidate_id=old.id,
        supersedes_id=None,
        wall_config="back_only",
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(wall)
    session.commit()

    _read_at_rank(monkeypatch, session, slot, revision, readers, rank=2)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    anchor = chain._anchor(session)
    assert (_rank_of(old), _rank_of(anchor)) == ("1", "2")

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None and (now.source, now.pairs) == ("reviewer", ())
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["carried_from_id"] == str(decision.id)
    (row,) = slot_rows(session, revision.id)
    assert row.decision is not None
    assert (row.decision.wall_config, row.decision.carried_from_id) == ("back_only", wall.id)
    assert (carried.decisions, carried.pairings, carried.pairings_held) == (1, 1, 0)


@pytest.mark.parametrize(
    ("packets", "newer"),
    [
        pytest.param(False, {"texts": {None: '97"'}}, id="read-to-other-values"),
        pytest.param(True, {"reader_version": "slot-reader-v1-newer"}, id="by-a-newer-reader"),
    ],
)
def test_another_row_chosen_holds_the_reviewers_pairing_and_blocks_sign_off(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    packets: bool,
    newer: dict[str, Any],
) -> None:
    """The newer reading chose another row of the page: another rank, and nothing proves it is the
    row the reviewer paired (other values read, or a newer reader). No row is left at the paired
    rank. The pairing is never dropped: the page's chosen row is held, naming the earlier decision
    and saying the reading picked a different line, and the vendor-vs-architect result on it waits
    for the reviewer, so sign-off is blocked (#1210).

    Before, the new row kept its automatic code+ais pairing, and nothing said a reviewer's pairing
    had been left behind: by a newer reader (question packets on, as production reads), the row
    compared to a PASS and nothing blocked sign-off."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding
    from app.review.approval import approval_readiness
    from tests.workflow.test_stages import _publish_rulebook
    from workflow.architect_row_plan import ARCHITECT_CHECK_RULE_ID

    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none", packets=packets)
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None

    _read_at_rank(monkeypatch, session, slot, revision, readers, rank=2, **newer)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    anchor = chain._anchor(session)
    assert _rank_of(anchor) == "2"

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None
    assert (now.source, now.pairs) == ("none", ())
    assert any(
        reason.startswith(f"{REVIEWER} paired the countertop row on page 1 on the earlier reading")
        and "picked a different line" in reason
        and "Pair this row again" in reason
        for reason in now.reasons
    ), now.reasons
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["earlier_reviewer_record_id"] == str(decision.id)
    assert (carried.pairings, carried.pairings_held) == (0, 1)
    audit = session.scalars(select(AuditEvent).where(AuditEvent.target_id == record.id)).one()
    assert audit.actor == "read the unread pages (#1206)"

    _publish_rulebook(session)
    DatabaseStages(store).run_checks(session, revision.id)
    session.commit()
    (finding,) = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == ARCHITECT_CHECK_RULE_ID,
            Finding.scope_row_candidate_id == anchor.id,
        )
    ).all()
    assert finding.outcome == "REVIEW_REQUIRED", (finding.reason, finding.notes)
    readiness = approval_readiness(session, revision.id)
    assert not readiness.can_approve
    assert finding.id in readiness.blocking_finding_ids


def test_a_page_with_no_row_now_counts_the_pairing_as_not_placed(
    session: Session,
    store: LocalStore,
) -> None:
    """The newer reading chose no countertop row on the page at all: there is no row to hold, and
    the page is listed for the reviewer's own answer. The pairing is counted, never lost from the
    count (#1210)."""
    from app.lifecycle.carry_forward import _carry_reviewer_pairings

    revision, _slot_stage, _readers, _decision = _reviewed_row(session, store, pairs="none")
    earlier = slot_rows(session, revision.id)
    assert len(earlier) == 1

    assert _carry_reviewer_pairings(session, revision.id, earlier, ()) == (0, 0, 1)


# --- #1210 review: a run beside it, a second reading, answers with no row --------------------------


def _wall_answer(session: Session, anchor: Any) -> Any:
    from app.models.evidence import SlotRowReviewDecision

    wall = SlotRowReviewDecision(
        row_candidate_id=anchor.id,
        supersedes_id=None,
        wall_config="back_only",
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(wall)
    session.commit()
    return wall


def _not_carried(session: Session, target_id: Any) -> list[str]:
    """The plain reasons the audit trail gives for an answer that could not be carried."""
    return [
        event.actor
        for event in session.scalars(select(AuditEvent).where(AuditEvent.target_id == target_id))
        if "not carried" in event.actor
    ]


def test_an_identical_run_beside_the_paired_one_is_another_line(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mirrored elevations side by side: the newer reading chose an identical run (same values,
    pieces and height) drawn far to the side, at another rank. It is not the row the reviewer
    answered: the pairing holds the new row ("picked a different line"), the wall answer is not
    carried (the wall is asked again) and the audit trail says why. Before the review fix both
    answers landed on the other line, silently."""
    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none")
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None
    old = chain._anchor(session)
    wall = _wall_answer(session, old)

    _read_at_rank(monkeypatch, session, slot, revision, readers, rank=2, dx_px=900)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    anchor = chain._anchor(session)
    assert min(p[0] for p in anchor.polygon) > max(p[0] for p in old.polygon) + 500

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None and (now.source, now.pairs) == ("none", ())
    assert any("picked a different line" in reason for reason in now.reasons), now.reasons
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["earlier_reviewer_record_id"] == str(decision.id)
    (row,) = slot_rows(session, revision.id)
    assert row.decision is None
    assert (carried.decisions, carried.pairings, carried.pairings_held) == (0, 0, 1)
    (reason,) = _not_carried(session, wall.id)
    assert "page 1" in reason and "asked again" in reason


@pytest.mark.parametrize("second_dx", [0, 900], ids=["same-row-again", "another-line-again"])
def test_a_hold_survives_a_second_read_of_the_unread_pages(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    second_dx: int,
) -> None:
    """A second "Read the unread pages" after the hold: the hold is still in force on the newest
    row, naming the reviewer's original decision, and the result still waits for the reviewer.
    Before the review fix only `source == reviewer` was carried, so the hold (source `none`) gave
    way to the new automatic pairing and the result was a PASS."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding
    from app.review.approval import approval_readiness
    from tests.workflow.test_stages import _publish_rulebook
    from workflow.architect_row_plan import ARCHITECT_CHECK_RULE_ID

    revision, slot, readers, decision = _reviewed_row(session, store, pairs="none", packets=True)
    newer = "slot-reader-v1-newer"
    first = latest_slot_reader_run(session, revision.id)
    assert first is not None
    _read_at_rank(monkeypatch, session, slot, revision, readers, rank=2, reader_version=newer)
    assert carry_row_answers_within_revision(session, revision.id, first).pairings_held == 1
    session.commit()
    held_anchor = chain._anchor(session)

    second = latest_slot_reader_run(session, revision.id)
    assert second is not None
    _read_at_rank(
        monkeypatch,
        session,
        slot,
        revision,
        readers,
        rank=2,
        reader_version=newer,
        dx_px=second_dx,
    )
    carried = carry_row_answers_within_revision(session, revision.id, second)
    session.commit()
    anchor = chain._anchor(session)
    assert anchor.id != held_anchor.id

    now = latest_architect_pairing(session, anchor.id)
    assert now is not None and (now.source, now.pairs) == ("none", ())
    record = session.get_one(ArchitectPairingRecord, now.record_id)
    assert record.details["earlier_reviewer_record_id"] == str(decision.id)
    assert any(reason.startswith(f"{REVIEWER} paired") for reason in now.reasons), now.reasons
    assert (carried.pairings, carried.pairings_held) == (0, 1)

    if second_dx:
        return  # a run moved sideways has no matching evidence crop; the hold is what is tested
    _publish_rulebook(session)
    DatabaseStages(store).run_checks(session, revision.id)
    session.commit()
    (finding,) = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == ARCHITECT_CHECK_RULE_ID,
            Finding.scope_row_candidate_id == anchor.id,
        )
    ).all()
    assert finding.outcome == "REVIEW_REQUIRED", (finding.reason, finding.notes)
    assert finding.id in approval_readiness(session, revision.id).blocking_finding_ids


def test_answers_with_no_row_left_are_audited_with_a_plain_reason(
    session: Session,
    store: LocalStore,
) -> None:
    """No countertop row on the page in the new reading: the reviewer's pairing and wall answer
    cannot be placed anywhere. Each gets an audit event naming it and the page, with the reason."""
    from app.lifecycle.carry_forward import _carry_reviewer_pairings, _carry_row_answers

    revision, _slot_stage, _readers, decision = _reviewed_row(session, store, pairs="none")
    wall = _wall_answer(session, chain._anchor(session))
    earlier = slot_rows(session, revision.id)

    assert _carry_reviewer_pairings(session, revision.id, earlier, ()) == (0, 0, 1)
    assert _carry_row_answers(session, earlier, (), within_revision=True) == 0
    session.commit()
    (pairing_reason,) = _not_carried(session, decision.id)
    assert "page 1" in pairing_reason and "no countertop row" in pairing_reason
    (wall_reason,) = _not_carried(session, wall.id)
    assert "page 1" in wall_reason and "no countertop row" in wall_reason


def test_a_row_is_held_once_however_many_pairings_point_at_it(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.lifecycle.carry_forward import _carry_reviewer_pairings

    revision, slot, readers, _decision = _reviewed_row(session, store, pairs="none")
    earlier = slot_rows(session, revision.id)
    _read_at_rank(monkeypatch, session, slot, revision, readers, rank=2, dx_px=900)
    current = slot_rows(session, revision.id)

    assert _carry_reviewer_pairings(session, revision.id, (*earlier, *earlier), current) == (
        0,
        1,
        0,
    )
    (row,) = current
    holds = session.scalars(
        select(ArchitectPairingRecord).where(
            ArchitectPairingRecord.row_anchor_candidate_id == row.anchor.id,
            ArchitectPairingRecord.source == "none",
        )
    ).all()
    assert [hold.details.get("earlier_reviewer_record_id") for hold in holds] == [str(_decision.id)]
