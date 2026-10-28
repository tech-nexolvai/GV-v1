"""Revision 2 of a job through the API (#1180): start it, keep revision 1 read-only, carry forward
what is unchanged, and say what changed.

The real routes against a real database. Every file, vendor and value here is made up; no paid call
is made (nothing reads a drawing: `/extract` only queues the work).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth import Principal, Role
from app.db.session import session_factory
from app.lifecycle.carry_forward import carry_after_reading
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingView,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    SlotRowReviewDecision,
    SourceArtifact,
    ViewRoleConfirmation,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.api.test_packages import PROJECT, _new_package, _upgrade
from workflow.architect_match_records import remembered_matches
from workflow.slot_row_scope import slot_rows
from workflow.view_roles import CODE_CONFIRMER

pytest_plugins = ("tests.app.postgres_fixture",)

SHOP_ONE = b"%PDF-1.7 synthetic shop drawing, first issue\n"
SHOP_TWO = b"%PDF-1.7 synthetic shop drawing, second issue\n"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(root=tmp_path / "artifacts", ticket_secret=b"synthetic-test-secret")


def _revision(session: Session, package_id: UUID, number: int) -> PackageRevision:
    session.expire_all()
    found = session.scalar(
        select(PackageRevision).where(
            PackageRevision.package_id == package_id, PackageRevision.revision_number == number
        )
    )
    assert found is not None
    return found


# --- starting a revision ----------------------------------------------------------------------------


# --- read-only revision 1 -----------------------------------------------------------------------------


# --- listing ---------------------------------------------------------------------------------------


# --- auth and isolation --------------------------------------------------------------------------------


# --- run-scope values carried, measurements not ---------------------------------------------------------


# --- carry forward after reading: walls and roles -------------------------------------------------------


def _version(session: Session, document: Document, data: bytes) -> DocumentVersion:
    digest = _sha(data)
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=2
    )
    session.add(version)
    session.flush()
    return version


def _pages(session: Session, version: DocumentVersion, hashes: tuple[str, ...]) -> list[Page]:
    pages = [
        Page(
            document_version_id=version.id,
            index=index,
            content_hash=content_hash,
            width_pt=Decimal(612),
            height_pt=Decimal(792),
            rotation=0,
            has_vector_text=True,
        )
        for index, content_hash in enumerate(hashes)
    ]
    session.add_all(pages)
    session.flush()
    return pages


def _reader_run(session: Session, revision_id: UUID, version: str = "slot-reader-v1-test") -> UUID:
    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
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
        extractor="extraction.form_reader",
        extractor_version=version,
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(run)
    session.flush()
    return run.id


def _row(
    session: Session,
    run_id: UUID,
    page: Page,
    *,
    rank: str,
    widths: tuple[int, ...],
    top: int = 0,
) -> UUID:
    anchor: UUID | None = None
    slots = [(str(i), width) for i, width in enumerate(widths)] + [("overall", sum(widths))]
    for slot, numerator in slots:
        candidate = ObservationCandidate(
            document_version_id=page.document_version_id,
            page_id=page.id,
            extraction_run_id=run_id,
            raw_text=f"{numerator} inch",
            value_numerator=numerator,
            value_denominator=1,
            unit="in",
            polygon=[[0, top], [10, top], [10, top + 10], [0, top + 10]],
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader",
                f"slot:{slot}",
                f"row-rank:{rank}",
                f"row-slot-count:{len(widths)}",
            ],
        )
        session.add(candidate)
        session.flush()
        if slot == "0":
            anchor = candidate.id
    assert anchor is not None
    return anchor


def _two_revisions(session: Session) -> tuple[PackageRevision, PackageRevision, Document]:
    """Revision 1 and its successor, each holding its own issue of one shop drawing whose first
    page is byte-identical and whose second page changed."""
    package_id = _new_package(session)
    one = _revision(session, package_id, 1)
    document = Document(package_id=package_id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.flush()
    first = _version(session, document, SHOP_ONE)
    second = _version(session, document, SHOP_TWO)
    session.add(
        PackageRevisionDocument(
            package_revision_id=one.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=first.id,
        )
    )
    session.flush()
    one.state = PackageState.NEEDS_INPUT.value
    two = PackageRevision(
        package_id=package_id, revision_number=2, state="UPLOADED", supersedes_id=one.id
    )
    session.add(two)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=two.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=second.id,
        )
    )
    session.flush()
    return one, two, document


def test_wall_answers_carry_only_to_the_same_row_on_an_unchanged_page(session: Session) -> None:
    one, two, document = _two_revisions(session)
    versions = {
        version.id: version
        for version in session.scalars(
            select(DocumentVersion).where(DocumentVersion.document_id == document.id)
        )
    }
    first, second = (
        next(v for v in versions.values() if v.sha256 == _sha(SHOP_ONE)),
        next(v for v in versions.values() if v.sha256 == _sha(SHOP_TWO)),
    )
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "3" * 64))
    run_one = _reader_run(session, one.id)
    same_old = _row(session, run_one, old_pages[0], rank="1", widths=(30, 40))
    moved_old = _row(session, run_one, old_pages[1], rank="2", widths=(30, 40))
    for anchor, wall in ((same_old, "back_only"), (moved_old, "island")):
        session.add(
            SlotRowReviewDecision(
                row_candidate_id=anchor,
                wall_config=wall,
                measurements={"countertop_width": "70"},
                confirmed_by="synthetic-reviewer",
            )
        )
    run_two = _reader_run(session, two.id)
    same_new = _row(session, run_two, new_pages[0], rank="1", widths=(30, 40))
    changed_new = _row(session, run_two, new_pages[1], rank="2", widths=(30, 40))
    session.commit()

    report = carry_after_reading(session, two.id)
    again = carry_after_reading(session, two.id)

    assert report.wall_answers == 1 and again.wall_answers == 0
    rows = {row.anchor.id: row for row in slot_rows(session, two.id)}
    carried = rows[same_new].decision
    assert carried is not None
    assert (carried.wall_config, carried.confirmed_by) == ("back_only", "synthetic-reviewer")
    assert carried.carried_from_id is not None
    assert rows[changed_new].decision is None


def test_a_row_read_differently_is_asked_again(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    anchor = _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40))
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            wall_config="back_only",
            measurements={},
            confirmed_by="synthetic-reviewer",
        )
    )
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 41))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


def test_a_persons_drawing_role_carries_to_the_identical_page_only(session: Session) -> None:
    _one, two, document = _two_revisions(session)
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "3" * 64))
    old_views = [
        DrawingView(page_id=page.id, tag="A", region={}, role="arch") for page in old_pages
    ]
    coded = DrawingView(page_id=old_pages[0].id, tag="B", region={}, role="shop")
    session.add_all((*old_views, coded))
    session.flush()
    session.add_all(
        [
            ViewRoleConfirmation(
                drawing_view_id=view.id, role="arch", confirmed_by="synthetic-reviewer"
            )
            for view in old_views
        ]
        + [ViewRoleConfirmation(drawing_view_id=coded.id, role="shop", confirmed_by=CODE_CONFIRMER)]
    )
    new_views = [DrawingView(page_id=page.id, tag="A", region={}) for page in new_pages]
    new_coded = DrawingView(page_id=new_pages[0].id, tag="B", region={})
    session.add_all((*new_views, new_coded))
    session.commit()

    report = carry_after_reading(session, two.id)

    assert report.drawing_roles == 1
    session.expire_all()
    assert session.get(DrawingView, new_views[0].id).role == "arch"  # type: ignore[union-attr]
    assert session.get(DrawingView, new_views[1].id).role is None  # type: ignore[union-attr]
    assert session.get(DrawingView, new_coded.id).role is None  # type: ignore[union-attr]
    carried = session.scalars(
        select(ViewRoleConfirmation).where(ViewRoleConfirmation.drawing_view_id == new_views[0].id)
    ).one()
    assert carried.confirmed_by == "synthetic-reviewer" and carried.carried_from_id is not None


def test_a_persons_architect_view_pick_on_revision_one_is_remembered_on_revision_two(
    session: Session,
) -> None:
    """#1166's own carry (D3) walks `supersedes_id`: a reviewer's pick on revision 1 is what
    revision 2's matching remembers for the same vendor item."""
    from app.models import ArchitectViewMatchRecord

    one, two, document = _two_revisions(session)
    first = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).first()
    assert first is not None
    page = _pages(session, first, ("1" * 64,))[0]
    anchor = _row(session, _reader_run(session, one.id), page, rank="1", widths=(30, 40))
    item_key = {"page_content_hash": "1" * 64, "page_index": 0, "pieces": 2, "row_y_pt": "2.4"}
    pick = ArchitectViewMatchRecord(
        package_revision_id=one.id,
        vendor_page_id=page.id,
        row_anchor_candidate_id=anchor,
        source="reviewer",
        status="none_matches",
        ai_picks=[],
        candidates=[],
        vendor_references=[],
        reasons=[],
        details={"vendor_item_key": item_key, "architect_files": ["b" * 64]},
        decided_by="synthetic-reviewer",
    )
    session.add(pick)
    session.commit()

    remembered = remembered_matches(session, two.id)

    assert [(m.record_id, m.revision_number, m.status) for m in remembered] == [
        (pick.id, 1, "none_matches")
    ]
    assert remembered[0].vendor_item_key["page_content_hash"] == "1" * 64


# --- what changed ------------------------------------------------------------------------------------


def test_the_carry_forward_migration_is_the_single_head() -> None:
    from alembic.script import ScriptDirectory

    from tests.app.postgres_fixture import alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision("0089_revision_carry_forward")
    assert found is not None and found.down_revision == "0088_outbox_abandoned"
    # #1191 added 0090 on top, #1193 0091, #1195 0092, #1206 0093, #1215 0094 and #1236 0095;
    # 0089 is still in the chain below them.
    # Later migrations (#1238's 0096) may follow, on one line of history to the single head.
    (head,) = script.get_heads()
    assert "0095_part_proposal_rows" in {
        rev.revision for rev in script.walk_revisions("base", head)
    }
    above = script.get_revision("0090_no_countertop_answers")
    assert above is not None and above.down_revision == found.revision
    reference = script.get_revision("0091_reference_view_suggestion")
    assert reference is not None and reference.down_revision == above.revision
    batches = script.get_revision("0092_no_countertop_batches")
    assert batches is not None and batches.down_revision == reference.revision
    refusals = script.get_revision("0093_model_invocation_refusals")
    assert refusals is not None and refusals.down_revision == batches.revision
    failures = script.get_revision("0094_invocation_failure_reason")
    assert failures is not None and failures.down_revision == refusals.revision
    top = script.get_revision("0095_part_proposal_rows")
    assert top is not None and top.down_revision == failures.revision


# --- carry-forward: the identity is strict ----------------------------------------------------------


def _versions(session: Session, document: Document) -> tuple[DocumentVersion, DocumentVersion]:
    first, second = session.scalars(
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.created_at)
    ).all()
    return first, second


def _answer(session: Session, anchor: UUID, wall: str = "back_only") -> None:
    session.add(
        SlotRowReviewDecision(
            row_candidate_id=anchor,
            wall_config=wall,
            measurements={},
            confirmed_by="synthetic-reviewer",
        )
    )


def test_two_identical_earlier_rows_carry_nothing(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    run_one = _reader_run(session, one.id)
    # Two earlier rows indistinguishable by every part of the identity.
    _answer(session, _row(session, run_one, old_page, rank="1", widths=(30, 40)), "back_only")
    _answer(session, _row(session, run_one, old_page, rank="1", widths=(30, 40)), "island")
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


@pytest.mark.parametrize("unknown", ["", hashlib.sha256(b"").hexdigest()])
def test_a_page_whose_content_is_unknown_never_carries(session: Session, unknown: str) -> None:
    from app.lifecycle.carry_forward import known_content, row_identity

    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    empty = hashlib.sha256(b"").hexdigest()
    old_page = _pages(session, first, (empty,))[0]
    new_page = _pages(session, second, (empty,))[0]
    _answer(
        session,
        _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40)),
    )
    _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0
    assert not known_content(unknown)
    assert row_identity(slot_rows(session, two.id)[0], unknown, "slot-reader-v1-test") is None


def test_another_reader_or_another_line_carries_nothing(session: Session) -> None:
    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_pages = _pages(session, first, ("1" * 64, "2" * 64))
    new_pages = _pages(session, second, ("1" * 64, "2" * 64))
    run_one = _reader_run(session, one.id)
    _answer(session, _row(session, run_one, old_pages[0], rank="1", widths=(30, 40)))
    _answer(session, _row(session, run_one, old_pages[1], rank="2", widths=(30, 40), top=100))
    # Page 1 read by another reader version; page 2's row line 50 px (24 pt at 150 dpi) lower.
    _row(
        session,
        _reader_run(session, two.id, version="slot-reader-v2-test"),
        new_pages[0],
        rank="1",
        widths=(30, 40),
    )
    _row(session, _reader_run(session, two.id), new_pages[1], rank="2", widths=(30, 40), top=150)
    session.commit()

    assert carry_after_reading(session, two.id).wall_answers == 0


# --- carried answers are visible ----------------------------------------------------------------------


# --- the worker hook ------------------------------------------------------------------------------------


def test_the_worker_carries_answers_right_after_reading_the_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.drain_outbox as worker
    import workflow.review as review_module
    from app.lifecycle import side_states
    from workflow import sutradhar

    order: list[str] = []

    class Outcome:
        def __init__(self) -> None:
            self.payload: dict[str, object] = {}

    def run_stage(_session: object, *, stage: str, **_kwargs: object) -> Outcome:
        order.append(stage)
        return Outcome()

    monkeypatch.setattr(worker, "_stages", lambda **_kwargs: object())
    # The flow's steps live in `workflow/sutradhar.py` since #1227, so they are replaced there.
    monkeypatch.setattr(sutradhar, "ensure_workflow_run", lambda *args: None)
    monkeypatch.setattr(review_module, "run_stage", run_stage)
    monkeypatch.setattr(
        sutradhar, "carry_forward", lambda *_args: order.append("carry") or {"wall_answers": 0}
    )
    monkeypatch.setattr(sutradhar, "build_package_text", lambda *_args: {})
    monkeypatch.setattr(sutradhar, "hunt_package_values", lambda *_args: {})
    monkeypatch.setattr(sutradhar, "propose_measurements", lambda *_args: {})
    monkeypatch.setattr(side_states, "enter_needs_input", lambda *_args, **_kwargs: None)
    from workflow import after_reading

    monkeypatch.setattr(
        after_reading,
        "queue_after_reading",
        lambda *_args: after_reading.AfterReading(False, False, "nothing (stubbed)"),
    )
    from app.runs import reading_progress

    monkeypatch.setattr(reading_progress, "side_engine", lambda *_args: None)
    monkeypatch.setattr(reading_progress, "record_progress", lambda *_args, **_kwargs: None)

    class StageSession:
        """Each stage commits as it ends (#1178); the commits are recorded in the same order."""

        def commit(self) -> None:
            order.append("commit")

        def rollback(self) -> None:
            order.append("rollback")

        def get_bind(self) -> None:
            return None

    results = worker._extract_package(StageSession(), uuid4(), str(uuid4()))

    # Carried inside the reading's own transaction: the reading and its carried answers commit
    # together, so a crash cannot leave a read revision without the answers it should have taken.
    assert order == [
        "commit",
        "ingest",
        "commit",
        "extract_pages",
        "carry",
        "commit",
        "match",
        "commit",
        "validate_evidence",
    ]
    assert results["carry_forward"] == {"wall_answers": 0}


def test_a_failed_carry_never_fails_the_reading(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.lifecycle.carry_forward as carry
    import scripts.drain_outbox as worker

    def broken(_session: object, _revision_id: UUID) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(carry, "carry_after_reading", broken)
    one, two, _document = _two_revisions(session)

    reported = worker._carry_forward(session, two.id)

    assert reported == {"ran": False, "reason": "carrying answers forward failed: RuntimeError"}
    # The session is still usable: the savepoint was rolled back, not the reading.
    assert session.get(PackageRevision, one.id) is not None


# --- what changed: pairing -------------------------------------------------------------------------------


# --- sign-off: carried answers are confirmed by the signer, on the server ----------------------------


def _scoped_finding(
    session: Session, revision: PackageRevision, row_id: UUID, rule_id: str | None = None
) -> None:
    from app.models import CheckRun, Finding, RuleDefinition, RuleSnapshot

    definition = RuleDefinition(rule_id=rule_id or f"CT-{uuid4().hex[:6]}")
    session.add(definition)
    session.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id, rule_snapshot_id=snapshot.id, engine_version="test"
    )
    session.add(run)
    session.flush()
    session.add(
        Finding(
            check_run_id=run.id,
            package_revision_id=revision.id,
            outcome="PASS",
            severity="FLAG",
            trace={},
            parameter_set_versions={},
            scope_row_candidate_id=row_id,
            scope_label="Synthetic countertop row",
        )
    )
    session.flush()


def _ready_to_sign(
    session: Session, *, carried: bool, rule_id: str | None = None
) -> tuple[PackageRevision, UUID]:
    """Revision 2, checked and waiting for sign-off; its one countertop result rests on a wall
    answer carried from revision 1 (or, with `carried=False`, on one given on revision 2)."""
    from app.review.session import open_session

    one, two, document = _two_revisions(session)
    first, second = _versions(session, document)
    old_page = _pages(session, first, ("1" * 64,))[0]
    new_page = _pages(session, second, ("1" * 64,))[0]
    old_row = _row(session, _reader_run(session, one.id), old_page, rank="1", widths=(30, 40))
    new_row = _row(session, _reader_run(session, two.id), new_page, rank="1", widths=(30, 40))
    _answer(session, old_row if carried else new_row)
    session.commit()
    carry_after_reading(session, two.id)
    _scoped_finding(session, two, new_row, rule_id)
    two.state = PackageState.AWAITING_REVIEW.value
    session.flush()
    review = open_session(session, package_revision_id=two.id, reviewer="synthetic-reviewer")
    session.commit()
    return two, review.id


def test_a_row_s_depth_hold_rests_on_no_carried_wall_answer(session: Session) -> None:
    """Review of #1201: a countertop row's own depth or sink hold rests on no wall answer, so it
    never asks the signer to confirm the row's carried one; a width result does (as before)."""
    from app.lifecycle.carry_forward import carried_answers_driving_results

    held, _review = _ready_to_sign(session, carried=True, rule_id="CT-DEPTH-001")
    assert carried_answers_driving_results(session, held.id) == ()


def _signer() -> Principal:
    return Principal(
        id="synthetic-signer", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT})
    )


def test_a_confirmed_sign_off_records_who_confirmed_in_the_signed_record(
    session: Session,
) -> None:
    from app.models.signed_exports import ApprovalExportSnapshot
    from app.review.approval import approve_package
    from app.review.signed_exports import load_snapshot

    two, review_id = _ready_to_sign(session, carried=True)

    decision = approve_package(
        session, principal=_signer(), review_session_id=review_id, carried_answers_confirmed=True
    )
    session.commit()

    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    review = load_snapshot(session, snapshot).review
    assert review.carried_answers_confirmation is not None
    assert "confirmed to still apply by synthetic-signer" in review.carried_answers_confirmation
    assert review.carried_answers_confirmation in review.signoff
    assert session.get(PackageRevision, two.id).state == PackageState.APPROVED  # type: ignore[union-attr]


def test_nothing_carried_needs_no_confirmation(session: Session) -> None:
    from app.models.signed_exports import ApprovalExportSnapshot
    from app.review.approval import approve_package
    from app.review.signed_exports import load_snapshot

    _two, review_id = _ready_to_sign(session, carried=False)

    decision = approve_package(session, principal=_signer(), review_session_id=review_id)
    session.commit()

    snapshot = session.scalars(
        select(ApprovalExportSnapshot).where(
            ApprovalExportSnapshot.approval_id == decision.approval.id
        )
    ).one()
    assert load_snapshot(session, snapshot).review.carried_answers_confirmation is None
