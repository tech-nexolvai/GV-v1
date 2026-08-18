"""Database contract for match candidates, approvals and the review trail (#197, C1.7).

The boundary this schema exists to keep: a *candidate* is a proposal that two items correspond, an
*approved match* is an assertion that they do. A rule may read the second and must never be able to
read the first. So the tests that matter are the refusals — most of all the one that stops a
vector-similarity guess being written with the authority of an exact identifier match.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Base, Immutable
from app.db.session import session_factory, unit_of_work
from app.models import (
    ApprovalSource,
    ApprovedMatch,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    MatchCandidate,
    MatchReviewEvent,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    SourceArtifact,
)
from app.models.matching import DETERMINISTIC_LANES
from retrieval.candidate import Lane

pytest_plugins = ("tests.app.postgres_fixture",)

MATCHING_TABLES = {"match_candidates", "approved_matches", "match_review_events"}
HASH = "d" * 64
BOX = {"space": "pdf_points", "polygon": [0, 0, 100, 100]}


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _two_items(session: Session) -> tuple[DrawingItem, DrawingItem]:
    """Staged flushes order the inserts: these models use plain ForeignKey columns, so SQLAlchemy has
    no dependency graph to sort a single `add_all` by."""
    project = Project(name=f"GV Matching Test {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/m.pdf", sha256=HASH, size=1, backend_version_id=None
    )
    document = Document(package_revision_id=revision.id, kind=DocumentKind.SHOP)
    session.add_all((artifact, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=HASH, page_count=1
    )
    session.add(version)
    session.flush()
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
        sheet_number="A-101",
        page_type=None,
        revision_label=None,
    )
    session.add(page)
    session.flush()
    view = DrawingView(page_id=page.id, tag="D", region=BOX)
    session.add(view)
    session.flush()
    left = DrawingItem(drawing_view_id=view.id, item_type="CT001", extent=BOX)
    right = DrawingItem(drawing_view_id=view.id, item_type="CT002", extent=BOX)
    session.add_all((left, right))
    session.flush()
    return left, right


def _candidate(session: Session, lane: Lane = Lane.EXACT) -> MatchCandidate:
    left, right = _two_items(session)
    candidate = MatchCandidate(
        left_item_id=left.id, right_item_id=right.id, lane=lane.value, score=Decimal("0.9")
    )
    session.add(candidate)
    session.flush()
    return candidate


# ---------------------------------------------------------------------------
# The boundary, without a database
# ---------------------------------------------------------------------------


def test_all_three_tables_are_registered() -> None:
    assert MATCHING_TABLES <= set(Base.metadata.tables)


def test_candidates_and_approvals_are_separate_tables() -> None:
    """Not one table with an `approved` flag. Promotion has to be an insert naming its source — a
    column update is one careless `UPDATE ... SET` away from turning every similarity guess in the
    database into a fact."""
    assert "approved" not in Base.metadata.tables["match_candidates"].columns
    assert "approval_source" in Base.metadata.tables["approved_matches"].columns


def test_every_matching_record_is_immutable() -> None:
    for model in (MatchCandidate, ApprovedMatch, MatchReviewEvent):
        assert issubclass(model, Immutable)


def test_only_the_exact_and_alias_lanes_may_auto_approve() -> None:
    """`docs/DESIGN_EXTRACTION.md` §8: lanes 1–2 may auto-approve, 3–8 are candidate-only."""
    assert DETERMINISTIC_LANES == {Lane.EXACT, Lane.ALIAS}


def test_there_is_no_third_way_to_approve() -> None:
    """A confidence score is neither a deterministic check nor a named human. The absence of a
    `MODEL` or `SCORE` member is the control."""
    assert {source.value for source in ApprovalSource} == {"deterministic", "human"}


def test_the_migration_lane_list_matches_the_live_enum() -> None:
    """The migration spells the lanes out rather than importing them, so that it keeps saying what it
    said the day it ran. This is what catches the two drifting apart."""
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0010_matching_plane.py"
    ).read_text(encoding="utf-8")
    for lane in Lane:
        assert f"'{lane.value}'" in migration, f"{lane.value} is missing from the migration"


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


def test_a_dense_candidate_cannot_be_approved_deterministically(postgres_engine: Engine) -> None:
    """**The constraint this story is for.** A dense-vector proposal written as `deterministic` would
    be indistinguishable from an exact-identifier match thereafter, and a rule reading it could not
    tell that a similarity guess had become a fact."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        candidate = _candidate(session, Lane.DENSE)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=Lane.DENSE.value,
                approval_source=ApprovalSource.DETERMINISTIC.value,
                approved_by="exact_match",
            )
        )


@pytest.mark.parametrize("lane", sorted(DETERMINISTIC_LANES))
def test_an_exact_or_alias_candidate_may_be_approved_deterministically(
    postgres_engine: Engine, lane: Lane
) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session, lane)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=lane.value,
                approval_source=ApprovalSource.DETERMINISTIC.value,
                approved_by="exact_match",
            )
        )
    with unit_of_work(factory) as session:
        assert session.scalars(select(ApprovedMatch)).one().lane == lane.value


@pytest.mark.parametrize("lane", [Lane.GEOMETRY, Lane.TRIGRAM, Lane.DENSE, Lane.FUSION])
def test_a_candidate_only_lane_may_still_be_approved_by_a_human(
    postgres_engine: Engine, lane: Lane
) -> None:
    """The lanes are candidate-only for *automatic* approval. A reviewer looking at the drawing may
    still say yes, and the record then names them rather than a check."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session, lane)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=lane.value,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="anant",
            )
        )
    with unit_of_work(factory) as session:
        assert session.scalars(select(ApprovedMatch)).one().approved_by == "anant"


def test_an_approval_lane_must_match_its_candidate(postgres_engine: Engine) -> None:
    """Asserted, not constrained — and that gap is deliberate and stated in the model.

    A CHECK cannot reach another table, so the denormalised lane can drift from the candidate's. This
    asserts the writer keeps them equal, which is weaker than a constraint. Closing it needs a
    trigger, which is `C1.12`'s territory.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session, Lane.EXACT)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source=ApprovalSource.DETERMINISTIC.value,
                approved_by="exact_match",
            )
        )
    with unit_of_work(factory) as session:
        approval = session.scalars(select(ApprovedMatch)).one()
        stored = session.scalars(
            select(MatchCandidate).where(MatchCandidate.id == approval.match_candidate_id)
        ).one()
        assert approval.lane == stored.lane


def test_an_unknown_approval_source_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source="model_said_so",
                approved_by="a-model",
            )
        )


def test_an_approval_must_name_who_decided(postgres_engine: Engine) -> None:
    """An approval nobody signed is one nobody can be asked about."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="",
            )
        )


def test_one_candidate_cannot_be_approved_twice(postgres_engine: Engine) -> None:
    """Two approvals, possibly from different sources, would leave "who decided this?" with two
    answers."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="anant",
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        existing = session.scalars(select(MatchCandidate)).one()
        session.add(
            ApprovedMatch(
                match_candidate_id=existing.id,
                lane=existing.lane,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="keyur",
            )
        )


def test_a_revocation_must_be_explained(postgres_engine: Engine) -> None:
    """A revocation with no reason is a fact nobody can act on, and the row is kept precisely so
    somebody can understand a past finding."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="anant",
                revoked_at=datetime.now(UTC),
                revoked_reason=None,
            )
        )


def test_an_item_cannot_be_matched_to_itself(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        left, _ = _two_items(session)
        session.add(
            MatchCandidate(
                left_item_id=left.id, right_item_id=left.id, lane=Lane.EXACT.value, score=None
            )
        )


def test_one_pair_may_be_proposed_by_several_lanes(postgres_engine: Engine) -> None:
    """Two lanes agreeing is information. Collapsing them would lose which routes found it, and that
    is how we learn which lanes are worth trusting."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        left, right = _two_items(session)
        session.add_all(
            (
                MatchCandidate(
                    left_item_id=left.id,
                    right_item_id=right.id,
                    lane=Lane.EXACT.value,
                    score=None,
                ),
                MatchCandidate(
                    left_item_id=left.id,
                    right_item_id=right.id,
                    lane=Lane.TRIGRAM.value,
                    score=Decimal("0.7"),
                ),
            )
        )
    with unit_of_work(factory) as session:
        assert {row.lane for row in session.scalars(select(MatchCandidate))} == {"exact", "trigram"}


def test_the_same_pair_and_lane_is_recorded_once(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        left, right = _two_items(session)
        session.add(
            MatchCandidate(
                left_item_id=left.id, right_item_id=right.id, lane=Lane.EXACT.value, score=None
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        existing = session.scalars(select(MatchCandidate)).one()
        session.add(
            MatchCandidate(
                left_item_id=existing.left_item_id,
                right_item_id=existing.right_item_id,
                lane=Lane.EXACT.value,
                score=Decimal("0.5"),
            )
        )


def test_a_review_event_records_what_the_reviewer_did(postgres_engine: Engine) -> None:
    """Append-only, like the correction ledger: the record of what we proposed and a human rejected
    is exactly what somebody would be tempted to tidy."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            MatchReviewEvent(
                match_candidate_id=candidate.id,
                action="rejected",
                reviewer="anant",
                note="different cabinet run",
            )
        )
    with unit_of_work(factory) as session:
        event = session.scalars(select(MatchReviewEvent)).one()
        assert (event.action, event.reviewer) == ("rejected", "anant")


def test_a_candidate_cannot_be_deleted_while_it_is_referenced(postgres_engine: Engine) -> None:
    """RESTRICT. Removing the candidate would erase what an approval was an approval *of*."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        candidate = _candidate(session)
        session.add(
            ApprovedMatch(
                match_candidate_id=candidate.id,
                lane=candidate.lane,
                approval_source=ApprovalSource.HUMAN.value,
                approved_by="anant",
            )
        )
    with pytest.raises(IntegrityError), unit_of_work(factory) as session:
        session.delete(session.scalars(select(MatchCandidate)).one())
