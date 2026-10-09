"""A page whose countertop line the AIs did not agree on never vanishes from the review (#1093).

Anant decided (2026-10-09): a split page becomes one "needs you" result that blocks sign-off and is
listed in the signed report; a page where both AIs say "no countertop line" is listed with the AI's
reason and blocks nothing. Synthetic values only.
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
from app.api.visual_countertops import _countertop_results_for_revision
from app.db.session import session_factory
from app.models import ObservationCandidate, PackageRevision, PackageRevisionDocument
from app.models.document import Page
from app.models.review import ReviewActionKind
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding
from app.review.approval import approval_readiness
from app.review.session import open_session, record_action
from app.schemas.visual_ui import CountertopResultsOut
from storage.local import LocalStore
from tests.api.test_slot_rows import _package_rows, _run_current_checks, _save_all_row_widths
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_part_operands import Assembly, _slot_reader_extraction_run
from workflow.slot_row_scope import latest_slot_reader_run
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

NOTE = "TEST ONLY synthetic reviewer note"
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


def _live(session: Session, revision_id: UUID) -> list[Finding]:
    return list(
        session.scalars(
            select(Finding)
            .join(CheckRun, CheckRun.id == Finding.check_run_id)
            .where(Finding.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .order_by(Finding.created_at, Finding.id)
        )
    )


def _decide(session: Session, revision_id: UUID, *, skip: UUID | None = None) -> None:
    """Confirm every FAIL and dismiss every abstention except `skip`, each with a TEST ONLY note."""
    review = open_session(session, package_revision_id=revision_id, reviewer="anant")
    for finding in _live(session, revision_id):
        if finding.scope_row_candidate_id is not None and finding.scope_row_candidate_id == skip:
            continue
        if finding.outcome == "FAIL":
            action = ReviewActionKind.CONFIRM
        elif finding.outcome in {"REVIEW_REQUIRED", "NOT_FOUND"}:
            action = ReviewActionKind.DISMISS
        else:
            continue
        record_action(
            session,
            review_session_id=review.id,
            finding_id=finding.id,
            action=action,
            actor="anant",
            note=f"{NOTE} for {finding.outcome}",
        )
    session.flush()


def _results(session: Session, package_id: UUID) -> CountertopResultsOut:
    revision = _revision_of(session, package_id)
    return _countertop_results_for_revision(session, package_id, revision)


def _decided_rows(session: Session) -> tuple[UUID, UUID]:
    """Two checked rows (pages 1 and 2) with their widths saved, so they need no input."""
    project_id, package_id, anchors = _package_rows(
        session, unsealed_all=True, wall_source="vendor-drawing-clues"
    )
    for anchor in anchors.values():
        _save_all_row_widths(session, project_id, package_id, anchor)
    return project_id, package_id


def test_a_split_page_is_one_blocking_item_until_decided_and_the_decision_carries_over(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session, revision.id, page_index=2, flags=SPLIT_PICKS, reason=CODE_SPLIT_WHY
    )

    _run_current_checks(session, package_id, tmp_path)
    split = [f for f in _live(session, revision.id) if f.scope_row_candidate_id == record_id]
    assert len(split) == 1
    (finding,) = split
    assert finding.outcome == "REVIEW_REQUIRED"
    assert finding.scope_label == "Countertop row on page 3"
    assert "line 2" in (finding.reason or "") and "line 3" in (finding.reason or "")
    assert "nothing on it was read" in (finding.reason or "")

    results = _results(session, package_id)
    (item,) = [item for item in results.items if item.row_id == record_id]
    assert item.page_number == 3
    assert item.finding_id == finding.id
    assert item.outcome is not None and item.outcome.value == "REVIEW_REQUIRED"
    assert item.hold is not None and item.hold.code == "row-choice-split"
    assert item.hold.reason == finding.reason
    assert item.pieces == ()
    assert item.wall_layout.source == "not established"
    assert item.architect.outcome is None and item.architect.not_compared_reason
    assert item.needs_decision
    assert [item.page_number for item in results.items] == [1, 2, 3]
    assert results.pages_without_countertop == ()

    _decide(session, revision.id, skip=record_id)
    readiness = approval_readiness(session, revision.id)
    assert readiness.blocking_finding_ids == (finding.id,), readiness.reason
    _decide(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0
    assert not next(
        i for i in _results(session, package_id).items if i.row_id == record_id
    ).needs_decision

    # An unchanged re-run: the reviewer's decision on the split page carries over (#1073).
    _run_current_checks(session, package_id, tmp_path)
    (again,) = [f for f in _live(session, revision.id) if f.scope_row_candidate_id == record_id]
    assert again.id != finding.id
    assert approval_readiness(session, revision.id).blocking_findings == 0
    (item,) = [item for item in _results(session, package_id).items if item.row_id == record_id]
    assert item.reviewer_decision is not None and item.reviewer_decision.carried_over
    assert not item.needs_decision


def test_a_both_none_page_is_listed_and_blocks_nothing(session: Session, tmp_path: Path) -> None:
    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session, revision.id, page_index=2, flags=NONE_PICKS, reason=NONE_WHY
    )

    _run_current_checks(session, package_id, tmp_path)
    assert all(f.scope_row_candidate_id != record_id for f in _live(session, revision.id))
    _decide(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0

    results = _results(session, package_id)
    assert all(item.row_id != record_id for item in results.items)
    assert [(page.page_number, page.reason) for page in results.pages_without_countertop] == [
        (3, NONE_WHY)
    ]


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


def test_a_record_without_picks_is_classified_from_its_code_authored_reason(
    session: Session, tmp_path: Path
) -> None:
    """Records written before #1093 carry no `row-pick:` flags."""
    from workflow.slot_row_scope import unchosen_row_pages

    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    split_id = _unchosen_record(session, revision.id, page_index=2, flags=(), reason=CODE_SPLIT_WHY)
    missing_id = _unchosen_record(
        session,
        revision.id,
        page_index=3,
        flags=(),
        reason="1 reader(s) gave no row answer (opus-5-5 row 2); the reviewer chooses",
    )
    none_id = _unchosen_record(session, revision.id, page_index=4, flags=(), reason=NONE_WHY)

    kinds = {page.record.id: page.kind for page in unchosen_row_pages(session, revision.id)}
    assert kinds == {split_id: "split", missing_id: "split", none_id: "none"}

    _run_current_checks(session, package_id, tmp_path)
    scoped = {f.scope_row_candidate_id for f in _live(session, revision.id)}
    assert {split_id, missing_id} <= scoped
    assert none_id not in scoped
    results = _results(session, package_id)
    assert [page.page_number for page in results.pages_without_countertop] == [5]


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


def test_pages_without_countertop_is_optional_in_the_published_schema() -> None:
    """Existing typed clients stay valid: the new list is published but not required."""
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
    )
    published = create_app(settings).openapi()["components"]["schemas"]["CountertopResultsOut"]

    assert "pages_without_countertop" in published["properties"]
    assert "pages_without_countertop" not in published.get("required", [])


# #1108: since `slot-row-choice-v3` each reader also says what its 0 meant (`row-kind:` flags).
NOT_BOXED_KINDS = ("row-kind:opus-5-5:not_among_boxes", "row-kind:sonnet-5-5:not_among_boxes")
NO_COUNTERTOP_KINDS = ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:no_countertop")
NOT_BOXED_WHY = (
    "opus-5-5: a countertop that none of the numbered lines measures (synthetic: unboxed); "
    "sonnet-5-5: a countertop that none of the numbered lines measures (synthetic: plan only); "
    "the reviewer chooses"
)


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
        # Records without `row-kind:` flags classify exactly as before (#1093).
        (NONE_PICKS, "none"),
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


def test_a_countertop_none_of_the_boxes_measures_is_one_blocking_item_with_both_reasons(
    session: Session, tmp_path: Path
) -> None:
    """Both AIs said a countertop is drawn but no numbered line is its row (#1108). It used to be
    listed as "no countertop" and never checked; now it needs the reviewer, like a split page."""
    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session, revision.id, page_index=2, flags=NONE_PICKS + NOT_BOXED_KINDS, reason=NOT_BOXED_WHY
    )

    _run_current_checks(session, package_id, tmp_path)
    (finding,) = [f for f in _live(session, revision.id) if f.scope_row_candidate_id == record_id]
    assert finding.outcome == "REVIEW_REQUIRED"
    reason = finding.reason or ""
    assert "found a countertop on this page that none of the numbered lines measures" in reason
    assert "synthetic: unboxed" in reason and "synthetic: plan only" in reason
    assert "nothing on it was read or checked" in reason
    assert not reason.endswith("the reviewer chooses")

    results = _results(session, package_id)
    (item,) = [item for item in results.items if item.row_id == record_id]
    assert item.hold is not None and item.hold.code == "row-choice-split"
    assert item.hold.reason == reason
    assert item.needs_decision
    assert results.pages_without_countertop == ()

    _decide(session, revision.id, skip=record_id)
    assert approval_readiness(session, revision.id).blocking_finding_ids == (finding.id,)
    _decide(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0


def test_an_unsure_reader_makes_one_blocking_item(session: Session, tmp_path: Path) -> None:
    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session,
        revision.id,
        page_index=2,
        flags=NONE_PICKS + ("row-kind:opus-5-5:no_countertop", "row-kind:sonnet-5-5:unsure"),
        reason="opus-5-5: no countertop on the sheet; sonnet-5-5: not sure which line; "
        "the reviewer chooses",
    )

    _run_current_checks(session, package_id, tmp_path)
    (finding,) = [f for f in _live(session, revision.id) if f.scope_row_candidate_id == record_id]
    assert finding.outcome == "REVIEW_REQUIRED"
    assert "not sure which line on this page is the countertop line" in (finding.reason or "")
    assert finding.id in approval_readiness(session, revision.id).blocking_finding_ids
    assert _results(session, package_id).pages_without_countertop == ()


def test_both_no_countertop_kinds_are_listed_and_block_nothing(
    session: Session, tmp_path: Path
) -> None:
    _project_id, package_id = _decided_rows(session)
    revision = _revision_of(session, package_id)
    record_id = _unchosen_record(
        session, revision.id, page_index=2, flags=NONE_PICKS + NO_COUNTERTOP_KINDS, reason=NONE_WHY
    )

    _run_current_checks(session, package_id, tmp_path)
    assert all(f.scope_row_candidate_id != record_id for f in _live(session, revision.id))
    _decide(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0
    results = _results(session, package_id)
    assert [(page.page_number, page.reason) for page in results.pages_without_countertop] == [
        (3, NONE_WHY)
    ]


def test_a_second_countertop_row_on_a_read_page_is_listed_not_checked_and_never_blocks(
    session: Session, tmp_path: Path
) -> None:
    """A reader named a second countertop's row on page 1 (#1108). V1 reads one row per page, so it
    is listed as not checked; it adds no result and blocks nothing."""
    project_id, package_id, anchors = _package_rows(
        session,
        unsealed_all=True,
        wall_source="vendor-drawing-clues",
        slot_flags=lambda page_index, _offset, _slot: (
            ["row-also:opus-5-5:3", "row-also:sonnet-5-5:3", "row-also:sonnet-5-5:4"]
            if page_index == 0
            else []
        ),
    )
    for anchor in anchors.values():
        _save_all_row_widths(session, project_id, package_id, anchor)
    revision = _revision_of(session, package_id)

    _run_current_checks(session, package_id, tmp_path)
    live = _live(session, revision.id)
    _decide(session, revision.id)
    assert approval_readiness(session, revision.id).blocking_findings == 0

    results = _results(session, package_id)
    assert [item.page_number for item in results.items] == [1, 2]
    assert len(live) == len(_live(session, revision.id))
    (listed,) = results.rows_not_checked
    assert listed.page_number == 1
    assert "second countertop" in listed.reason
    assert "numbered line 3, named by opus-5-5 and sonnet-5-5" in listed.reason
    assert "numbered line 4, named by sonnet-5-5" in listed.reason
    assert "not checked" in listed.reason


def test_rows_not_checked_is_optional_in_the_published_schema() -> None:
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
    )
    published = create_app(settings).openapi()["components"]["schemas"]["CountertopResultsOut"]

    assert "rows_not_checked" in published["properties"]
    assert "rows_not_checked" not in published.get("required", [])
