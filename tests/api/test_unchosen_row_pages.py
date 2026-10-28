"""A page whose countertop line the AIs did not agree on never vanishes from the review (#1093).

Anant decided (2026-10-09): a split page becomes one "needs you" result that blocks sign-off and is
listed in the signed report; a page where both AIs say "no countertop line" is listed with the AI's
reason. Since #1191 (2026-10-12, option c) a listed page needs one reviewer confirmation before
sign-off (`tests/review/test_no_countertop_answers.py`), and an older reading that does not say what
its "no line" meant is blocking. Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision, PackageRevisionDocument
from app.models.document import Page
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.review.approval import approval_readiness
from app.review.countertop_results import _countertop_results_for_revision
from app.schemas.visual_ui import CountertopResultsOut
from storage.local import LocalStore
from tests.api.test_slot_rows import (
    _package_rows,
    _row_holds,
    _run_current_checks,
)
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_part_operands import Assembly, _slot_reader_extraction_run
from workflow.slot_row_scope import latest_slot_reader_run
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

SPLIT_PICKS = ("row-pick:opus-5-5:2", "row-pick:sonnet-5-5:3")
NONE_PICKS = ("row-pick:opus-5-5:0", "row-pick:sonnet-5-5:0")
NONE_WHY = "synthetic: this page shows only a wall elevation, no countertop dimension line"
CODE_SPLIT_WHY = (
    "the readers chose different rows (opus-5-5 row 2, sonnet-5-5 row 3); the reviewer chooses"
)


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


def _revision_of(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def _unchosen_record(
    session: Session,
    revision_id: UUID,
    *,
    page_index: int,
    flags: tuple[str, ...],
    reason: str,
    page: Page | None = None,
) -> UUID:
    """The reader's "row not chosen" record for one (new, unless given) vendor page."""
    run_id = latest_slot_reader_run(session, revision_id)
    assert run_id is not None
    if page is None:
        version_id = session.scalars(
            select(PackageRevisionDocument.document_version_id).where(
                PackageRevisionDocument.package_revision_id == revision_id
            )
        ).one()
        page = Page(
            document_version_id=version_id,
            index=page_index,
            content_hash=f"{page_index + 100:064x}",
            width_pt=Decimal(612),
            height_pt=Decimal(792),
            rotation=0,
            has_vector_text=True,
            media_box=["0", "0", "612", "792"],
            crop_box=["0", "0", "612", "792"],
        )
        session.add(page)
        session.flush()
    record = ObservationCandidate(
        document_version_id=page.document_version_id,
        page_id=page.id,
        extraction_run_id=run_id,
        raw_text="",
        polygon=[[0, 0], [612, 0], [612, 792], [0, 792]],
        coordinate_space="image",
        ambiguity_flags=[
            "slot-reader-row-choice",
            "row-choice:0",
            *flags,
            f"row-candidate:1:{uuid4()}",
            f"row-candidate:2:{uuid4()}",
            f"row-candidate:3:{uuid4()}",
        ],
        review_reason=reason,
    )
    session.add(record)
    session.flush()
    return record.id


def _all_live(session: Session, revision_id: UUID) -> list[Finding]:
    return list(
        session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .order_by(Finding.created_at, Finding.id)
        )
    )


def _live(session: Session, revision_id: UUID) -> list[Finding]:
    """The live results, less each countertop row's own depth and sink holds (#1201)."""
    holds = _row_holds(session, revision_id)
    return [f for f in _all_live(session, revision_id) if f.id not in holds]


def _results(session: Session, package_id: UUID) -> CountertopResultsOut:
    revision = _revision_of(session, package_id)
    return _countertop_results_for_revision(session, package_id, revision)


def test_a_split_page_blocks_even_when_no_row_was_chosen_anywhere(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id, anchors = _package_rows(session, row_pages=())
    assert anchors == {}
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session, revision.id, page_index=2, flags=SPLIT_PICKS, reason=CODE_SPLIT_WHY
    )

    _run_current_checks(session, package_id, tmp_path)
    live = _live(session, revision.id)
    (split,) = [f for f in live if f.scope_row_candidate_id == record_id]
    assert split.outcome == "REVIEW_REQUIRED"
    assert split.id in approval_readiness(session, revision.id).blocking_finding_ids
    # The split page is the width check's only subject; no revision-wide width result beside it.
    width = session.scalars(
        select(Finding.id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == "CT-WIDTH-001",
        )
    ).all()
    assert width == [split.id]
    assert [item.row_id for item in _results(session, package_id).items] == [record_id]


def test_a_reviewer_owned_page_gets_no_split_result(session: Session, tmp_path: Path) -> None:
    store = LocalStore(root=tmp_path, ticket_secret=b"synthetic-test-only")
    assembly = Assembly(session, store)
    _slot_reader_extraction_run(session, assembly.revision.id)
    owned_id = _unchosen_record(
        session,
        assembly.revision.id,
        page_index=0,
        flags=SPLIT_PICKS,
        reason=CODE_SPLIT_WHY,
        page=assembly.page,
    )
    other_id = _unchosen_record(
        session, assembly.revision.id, page_index=1, flags=SPLIT_PICKS, reason=CODE_SPLIT_WHY
    )

    DatabaseStages(store, operands={}).run_checks(session, assembly.revision.id)
    scoped = {f.scope_row_candidate_id for f in _live(session, assembly.revision.id)}
    assert owned_id not in scoped
    assert other_id in scoped
    package_id = assembly.revision.package_id
    rows = {item.row_id for item in _results(session, package_id).items}
    assert owned_id not in rows and other_id in rows


# #1108: since `slot-row-choice-v3` each reader also says what its 0 meant (`row-kind:` flags).
NOT_BOXED_KINDS = ("row-kind:opus-5-5:not_among_boxes", "row-kind:sonnet-5-5:not_among_boxes")
NO_COUNTERTOP_KINDS = ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:no_countertop")


@pytest.mark.parametrize(
    ("flags", "kind"),
    [
        (NONE_PICKS + NO_COUNTERTOP_KINDS, "none"),
        (NONE_PICKS + NOT_BOXED_KINDS, "split"),
        (NONE_PICKS + ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:unsure"), "split"),
        (
            NONE_PICKS + ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:not_among_boxes"),
            "split",
        ),
        (NONE_PICKS + ("row-kind:opus-5-5:unsure", "row-kind:sonnet-5-5:unsure"), "split"),
        # One reader's kind is missing: doubt never lists a page.
        (NONE_PICKS + ("row-kind:opus-5-5:no_countertop",), "split"),
        # A kind that is not one of the four, or a reader with two kinds: doubt again.
        (NONE_PICKS + ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:maybe"), "split"),
        (
            NONE_PICKS + NO_COUNTERTOP_KINDS + ("row-kind:sonnet-5-5:not_among_boxes",),
            "split",
        ),
        # Records without `row-kind:` flags never say what an all-0 answer meant: "older" (#1191).
        (NONE_PICKS, "older"),
        (SPLIT_PICKS, "split"),
    ],
)
def test_only_no_countertop_from_every_reader_is_listed_without_blocking(
    session: Session, flags: tuple[str, ...], kind: str
) -> None:
    from workflow.slot_row_scope import unchosen_row_pages

    _project_id, package_id, _anchors = _package_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(session, revision.id, page_index=2, flags=flags, reason=NONE_WHY)

    (page,) = unchosen_row_pages(session, revision.id)
    assert page.record.id == record_id
    assert page.kind == kind
    if kind == "none":
        assert page.reason == NONE_WHY
