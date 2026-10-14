"""A reviewer's wall answer that a newer reading cannot carry holds the new row's walls (#1241).

"Read the unread pages" writes a new reading of every page. A reviewer's wall answer
(`SlotRowReviewDecision`) is carried to the new row only when it is provably the same row
(`app/lifecycle/carry_forward.carry_row_answers_within_revision`). Before this change an answer that
could not be carried was only audited: the new row's walls were decided again, and when code's wall
ink and both AIs agreed on walls at both ends, that automatic wall was used with no click, so the
reviewer's answer was replaced silently.

Now the answer holds the walls of the page's row in the newest reading (`SlotRow.wall_hold`,
derived by `workflow/slot_row_scope.slot_rows_and_unchosen_pages`, no new table): the walls are asked
again, the width result waits for the reviewer and blocks sign-off, and the row says why in plain
words. It only ever holds; it never passes more.

Synthetic drawings and fake readers (`tests/workflow/test_separate_architect_production_chain.py`):
three pieces 24", 48" and 24" with an overall of 96", which is right only with no wall at either
end (a 1" field cut per wall end). Code's wall ink is seen at both ends and both AIs say walls at
both ends, so the automatic walls (both ends) apply and the width FAILs with no click. No network,
no model. Every value is invented.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.slot_rows import SlotRowReviewIn, list_slot_rows, review_slot_row
from app.auth.roles import Principal, Role
from app.db.session import session_factory
from app.lifecycle.carry_forward import carry_row_answers_within_revision
from app.models import ObservationCandidate, Package, PackageRevision
from app.models.document import PackageRevisionDocument
from app.models.evidence import SlotRowReviewDecision
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.models.verdicts import CheckRun, Finding
from app.review.approval import approval_readiness
from app.review.carry_over import fingerprint_bodies
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.workflow import test_separate_architect_production_chain as chain
from tests.workflow.test_slot_reader import _to_walls, _with_wall_ink
from tests.workflow.test_stages import _publish_rulebook
from vocabulary.blank_page import BLANK_PAGE_RULE
from workflow.slot_row_evidence import slot_row_check
from workflow.slot_row_scope import (
    BLANK_PROOF_RULES,
    latest_slot_reader_run,
    slot_rows,
    slot_rows_and_unchosen_pages,
)
from workflow.stages import DatabaseStages, _SpendMeter

pytest_plugins = ("tests.app.postgres_fixture",)

REVIEWER = "reviewer (synthetic)"
NEWER = "slot-reader-v1-newer"
#: Right only with no end walls: 24 + 48 + 24 = 96 (a 1" field cut per wall end).
OVERALL = '96"'
NOT_CARRIED = "could not be carried"


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


@pytest.fixture(autouse=True)
def automatic_walls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Code's wall ink at both row ends, as `wall_pictures` would report it."""
    _with_wall_ink(monkeypatch, True, True)


def _slot(store: LocalStore, readers: Any) -> DatabaseStages:
    plain = replace(chain.runtime(readers, claude_row_reader=True), question_packets=True)
    runtime = replace(plain, form=replace(plain.form, rates=chain.PricedRates()))
    slot = DatabaseStages(
        store,
        dpi=150,
        ocr_engine=chain._SilentOcr(),
        missing_space=chain.MISSING_SPACE,
        form_reader=runtime.form,
        slot_reader=runtime,
    )
    slot._meter = _SpendMeter(cap_micros=1_000_000_000)
    return slot


def _read(
    session: Session,
    slot: DatabaseStages,
    revision: Any,
    readers: Any,
    *,
    rank: int = 1,
    reader_version: str | None = None,
) -> ObservationCandidate:
    """One reading of the vendor page, its chosen row recorded at `rank` (the readers picked another
    of the page's rows, or code ranked them differently) and under `reader_version` when given.
    Returns the new row's anchor."""
    from workflow import stages as stages_module
    from workflow.slot_reader import persist_slot_readings as original

    def at_rank(target: Session, *, results: Any, **kwargs: Any) -> int:
        moved = [
            (
                result
                if result.plan.row is None
                else replace(
                    result, plan=replace(result.plan, row=replace(result.plan.row, rank=rank))
                )
            )
            for result in results
        ]
        return original(target, results=moved, **kwargs)

    saved = dict(chain.TEXTS)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(stages_module, "persist_slot_readings", at_rank)
        if reader_version is not None:
            patch.setattr(stages_module, "SLOT_READER_VERSION", reader_version)
        chain.TEXTS[None] = OVERALL
        try:
            chain._read_slots(session, slot, revision, readers)
        finally:
            chain.TEXTS.clear()
            chain.TEXTS.update(saved)
    return chain._anchor(session)


def _first_reading(
    session: Session, store: LocalStore
) -> tuple[PackageRevision, DatabaseStages, Any, ObservationCandidate]:
    revision = chain._upload(session, store, chain.VENDOR)
    chain._stages(store).extract_pages(session, revision.id)
    session.commit()
    readers = chain.Readers()
    readers.counter_break = _to_walls
    slot = _slot(store, readers)
    anchor = _read(session, slot, revision, readers)
    _publish_rulebook(session)
    session.commit()
    return revision, slot, readers, anchor


def _answer(
    session: Session, anchor: ObservationCandidate, wall: str | None = "back_and_left"
) -> SlotRowReviewDecision:
    """The reviewer's own answer on the row (the API writes the same record)."""
    decision = SlotRowReviewDecision(
        row_candidate_id=anchor.id,
        supersedes_id=None,
        wall_config=wall,
        measurements={},
        confirmed_by=REVIEWER,
    )
    session.add(decision)
    session.commit()
    return decision


def _re_read(
    session: Session,
    slot: DatabaseStages,
    revision: Any,
    readers: Any,
    *,
    rank: int = 2,
    reader_version: str | None = NEWER,
) -> tuple[ObservationCandidate, int]:
    """A newer reading and the carry "Read the unread pages" makes from the reading before it.
    By default the readers chose another row of the page, read by a newer reader: nothing proves it
    is the row the reviewer answered, so the answer is not carried."""
    earlier = latest_slot_reader_run(session, revision.id)
    assert earlier is not None
    anchor = _read(session, slot, revision, readers, rank=rank, reader_version=reader_version)
    carried = carry_row_answers_within_revision(session, revision.id, earlier)
    session.commit()
    return anchor, carried.decisions


def _width(session: Session, store: LocalStore, revision: Any, anchor: Any) -> Finding:
    """The live width result (CT-WIDTH-001) on the row, after a check run."""
    DatabaseStages(store).run_checks(session, revision.id)
    session.commit()
    return session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == "CT-WIDTH-001",
            Finding.scope_row_candidate_id == anchor.id,
        )
    ).one()


def _principal(session: Session, revision: Any) -> tuple[Principal, Package]:
    package = session.get_one(Package, revision.package_id)
    principal = Principal(
        id=REVIEWER, roles=frozenset({Role.REVIEWER}), projects=frozenset({package.project_id})
    )
    return principal, package


def _assert_held(session: Session, store: LocalStore, revision: Any, anchor: Any) -> None:
    """The page's new row: its walls held naming the reviewer's answer, the width result waits for
    the reviewer, and sign-off is blocked."""
    finding = _width(session, store, revision, anchor)
    assert finding.outcome == "REVIEW_REQUIRED", (finding.outcome, finding.reason)
    assert NOT_CARRIED in (finding.reason or ""), finding.reason
    assert finding.id in approval_readiness(session, revision.id).blocking_finding_ids
    (row,) = slot_rows(session, revision.id)
    assert row.anchor.id == anchor.id and row.decision is None
    hold = getattr(row, "wall_hold", None)
    assert hold is not None, "the walls are held, naming the earlier answer"
    assert REVIEWER in hold and "back wall and left end" in hold and "page 1" in hold
    assert NOT_CARRIED in hold and "choose the walls again" in hold.lower()
    assert row.wall_confirmation_allowed, "the reviewer can answer at once"
    check = slot_row_check(session, row)
    assert (check.eligible, check.wall_config, check.wall_reason) == (False, None, hold)


# --- 1. not carried: held ----------------------------------------------------------------------------


def test_the_automatic_walls_decide_the_row_before_any_answer(
    session: Session, store: LocalStore
) -> None:
    """The baseline the hold changes: with no reviewer answer, code's ink and both AIs set walls at
    both ends with no click, and the width is decided (wrong by the 2" the two field cuts add)."""
    revision, _slot_stage, _readers, anchor = _first_reading(session, store)
    finding = _width(session, store, revision, anchor)
    assert finding.outcome == "FAIL", (finding.outcome, finding.reason)
    (row,) = slot_rows(session, revision.id)
    assert getattr(row, "wall_hold", None) is None


def test_an_answer_the_new_reading_cannot_carry_holds_the_new_rows_walls(
    session: Session, store: LocalStore
) -> None:
    """Before: the new row took the automatic walls at both ends and was decided (FAIL) with no
    click; the reviewer's "back wall and left end" was only written in the audit trail."""
    revision, slot, readers, first = _first_reading(session, store)
    decision = _answer(session, first)

    anchor, carried = _re_read(session, slot, revision, readers)
    assert carried == 0, "another row chosen by a newer reader is not provably the same row"

    _assert_held(session, store, revision, anchor)
    finding = _width(session, store, revision, anchor)
    # Carry-over compares what the check used: no wall on the held row, never the automatic one.
    body = fingerprint_bodies(session, [finding])[finding.id]
    assert body["row"]["wall_layout"] is None
    # The screen: the reason is on the row, above the wall choices; no automatic proposal is shown.
    principal, package = _principal(session, revision)
    (listed,) = list_slot_rows(principal, session, package.project_id, package.id).rows
    (row,) = slot_rows(session, revision.id)
    assert getattr(listed, "wall_hold_reason", None) == row.wall_hold
    assert listed.wall_reason == row.wall_hold
    assert listed.wall_proposal is None and listed.wall_config is None
    assert decision.id != listed.decision_id


@pytest.mark.parametrize(
    "third",
    [
        pytest.param({"rank": 2, "reader_version": NEWER}, id="the-same-new-line-again"),
        pytest.param({"rank": 1, "reader_version": None}, id="back-to-the-answered-line"),
    ],
)
def test_the_hold_survives_a_third_reading(
    session: Session, store: LocalStore, third: dict[str, Any]
) -> None:
    """Carrying only looks one reading back: reading 3 carries from reading 2, whose row has no
    answer. Even when reading 3 picks the very line the reviewer answered on reading 1, the answer
    is not recovered; it is held and asked again, never silent."""
    revision, slot, readers, first = _first_reading(session, store)
    _answer(session, first)
    _re_read(session, slot, revision, readers)

    anchor, carried = _re_read(session, slot, revision, readers, **third)

    assert carried == 0
    _assert_held(session, store, revision, anchor)


# --- 2. answered again, or carried: not held -------------------------------------------------------


def test_the_reviewers_answer_on_the_new_row_clears_the_hold_and_is_used(
    session: Session, store: LocalStore
) -> None:
    revision, slot, readers, first = _first_reading(session, store)
    _answer(session, first)
    anchor, _carried = _re_read(session, slot, revision, readers)
    principal, package = _principal(session, revision)

    review_slot_row(
        principal,
        principal,
        session,
        package.project_id,
        package.id,
        anchor.id,
        SlotRowReviewIn(wall_config="back_only"),
    )
    session.commit()

    (row,) = slot_rows(session, revision.id)
    assert row.decision is not None and row.wall_hold is None
    assert slot_row_check(session, row).wall_config == "back_only"
    finding = _width(session, store, revision, anchor)
    assert finding.outcome == "PASS", (finding.outcome, finding.reason)
    (listed,) = list_slot_rows(principal, session, package.project_id, package.id).rows
    assert listed.wall_hold_reason is None

    # The next reading of the same row carries the new answer: still nothing held.
    again, carried = _re_read(session, slot, revision, readers)
    assert carried == 1
    (row,) = slot_rows(session, revision.id)
    assert row.anchor.id == again.id and row.wall_hold is None
    assert row.decision is not None and row.decision.wall_config == "back_only"


def test_an_answer_with_no_wall_on_the_new_row_keeps_the_hold(
    session: Session, store: LocalStore
) -> None:
    """Only a wall answer on the new row clears the hold: a save with no wall in it (typed widths
    only) leaves the walls asked again, never automatic."""
    revision, slot, readers, first = _first_reading(session, store)
    _answer(session, first)
    anchor, _carried = _re_read(session, slot, revision, readers)

    _answer(session, anchor, wall=None)

    (row,) = slot_rows(session, revision.id)
    assert row.decision is not None and row.decision.wall_config is None
    assert row.wall_hold is not None and NOT_CARRIED in row.wall_hold
    check = slot_row_check(session, row)
    assert (check.eligible, check.wall_config, check.wall_reason) == (False, None, row.wall_hold)
    finding = _width(session, store, revision, anchor)
    assert finding.outcome == "REVIEW_REQUIRED", (finding.outcome, finding.reason)


def test_the_same_row_at_another_rank_carries_the_answer_and_holds_nothing(
    session: Session, store: LocalStore
) -> None:
    """#1210: the same physical row recorded at another rank keeps the reviewer's answer."""
    revision, slot, readers, first = _first_reading(session, store)
    decision = _answer(session, first)

    anchor, carried = _re_read(session, slot, revision, readers, reader_version=None)

    assert carried == 1
    (row,) = slot_rows(session, revision.id)
    assert row.anchor.id == anchor.id and row.wall_hold is None
    assert row.decision is not None and row.decision.carried_from_id == decision.id
    assert slot_row_check(session, row).wall_config == "back_and_left"


def test_a_typed_widths_only_answer_holds_no_walls(session: Session, store: LocalStore) -> None:
    """An answer with no wall in it has no wall to lose: the new row's walls are decided as before
    (the answer itself is still audited as not carried)."""
    revision, slot, readers, first = _first_reading(session, store)
    _answer(session, first, wall=None)

    anchor, carried = _re_read(session, slot, revision, readers)

    assert carried == 0
    (row,) = slot_rows(session, revision.id)
    assert row.wall_hold is None
    assert _width(session, store, revision, anchor).outcome == "FAIL", "automatic, as before"


def test_an_answer_on_another_revision_never_holds_this_one(
    session: Session, store: LocalStore
) -> None:
    """Revision 2 shares revision 1's unchanged drawing (the same pages). An answer given on
    revision 1 carries only by the revision rule (#1180); it never holds a row of revision 2."""
    revision, slot, readers, first = _first_reading(session, store)
    _answer(session, first)
    second = PackageRevision(
        package_id=revision.package_id,
        revision_number=revision.revision_number + 1,
        state="UPLOADING",
        supersedes_id=revision.id,
    )
    session.add(second)
    session.flush()
    for link in session.scalars(
        select(PackageRevisionDocument).where(
            PackageRevisionDocument.package_revision_id == revision.id
        )
    ).all():
        session.add(
            PackageRevisionDocument(
                package_revision_id=second.id,
                package_id=link.package_id,
                document_id=link.document_id,
                document_version_id=link.document_version_id,
            )
        )
    session.commit()

    anchor = _read(session, slot, second, readers, rank=2, reader_version=NEWER)

    (row,) = slot_rows(session, second.id)
    assert row.anchor.id == anchor.id and row.anchor.page_id == first.page_id
    assert row.wall_hold is None


# --- 3. no row on the page now ----------------------------------------------------------------------


def _blank_page_reading(session: Session, revision: Any, page_id: Any, version_id: Any) -> None:
    """A newer slot-reader reading in which code proved the page blank (#1195): no row on it."""
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="slot-reader",
        extractor_version=f"{NEWER}-ink-v1",
        config_hash="synthetic",
        dpi=150,
    )
    session.add(run)
    session.flush()
    proof = [
        f"blank-proof:{name}={BLANK_PAGE_RULE if name == 'rule' else 0}"
        for name in BLANK_PROOF_RULES[BLANK_PAGE_RULE]
    ]
    session.add(
        ObservationCandidate(
            document_version_id=version_id,
            page_id=page_id,
            extraction_run_id=run.id,
            raw_text="",
            polygon=[[0, 0], [100, 0], [100, 100], [0, 100]],
            coordinate_space="image",
            ambiguity_flags=["slot-reader-row-choice", "no-row:blank-page", *proof],
            review_reason="Synthetic: code found nothing drawn on this page.",
        )
    )
    session.commit()


def test_a_page_with_no_row_now_names_the_answer_and_waits_for_the_reviewer(
    session: Session, store: LocalStore
) -> None:
    """The newer reading has no row on the answered page (here: proven blank, which alone would
    need no click). The page is listed with the earlier answer named, and waits for the reviewer."""
    revision, _slot_stage, _readers, first = _first_reading(session, store)
    _answer(session, first)
    _blank_page_reading(session, revision, first.page_id, first.document_version_id)

    rows, (page,) = slot_rows_and_unchosen_pages(session, revision.id)

    assert rows == ()
    assert page.blank_proof is not None, "a full proof: alone it would need no answer"
    hold = getattr(page, "wall_hold", None)
    assert hold is not None and REVIEWER in hold and NOT_CARRIED in hold
    assert hold in page.reason
    assert page.doubtful and page.needs_answer
