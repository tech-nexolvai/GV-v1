"""Type 1 with the architect's drawings as their own file: said plainly, asked once (#1161).

This version compares the vendor's countertops only with architect views pasted on the vendor's own
sheets. When the architect's drawings came as a separate file (an ARCHITECTURAL document whose bytes
differ from the shop document's), every countertop not compared says that file was not compared,
and the revision gets one package-level CT-ARCH-WIDTH-001 REVIEW_REQUIRED line that blocks sign-off
until the reviewer decides it. The same file in both slots, a package with no architect document,
and rows compared from pasted views are unchanged.

Every value is invented; the sheet is `tests/workflow/test_architect_row_evidence._sealed_rows`.

Verification for: `app/evidence/sides.has_separate_architect_file`,
`workflow/architect_row_plan.plan_architect_row`, the architect block of
`workflow/stages.DatabaseStages.run_checks` and `app/api/visual_countertops.py`.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.evidence.sides import has_separate_architect_file
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    PackageRevision,
    PackageRevisionDocument,
    SourceArtifact,
)
from app.models.review import ReviewActionKind
from app.review.approval import approval_readiness
from app.review.session import open_session, record_action
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_row_evidence import (
    ARCH_RULE,
    _architect_drawing,
    _architect_value,
    _overall,
    _pairing,
    _row,
    _run,
    _sealed_rows,
)
from workflow.architect_row_plan import (
    NOTHING_PAIRED_ON_REVISION,
    SEPARATE_ARCHITECT_FILE_NOT_COMPARED,
    SEPARATE_ARCHITECT_FILE_ROW,
    Disposition,
    plan_architect_row,
)

pytest_plugins = ("tests.app.postgres_fixture",)

PACKAGE = UUID(int=0)
"""`_run` files a finding with no scope row under this key."""


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


def _revision(session: Session, package_id: UUID) -> PackageRevision:
    return session.query(PackageRevision).filter_by(package_id=package_id).one()


def add_architect_file(session: Session, package_id: UUID, *, same_bytes_as: UUID | None) -> None:
    """An ARCHITECTURAL document in the package's revision: the shop file's bytes, or its own."""
    revision = _revision(session, package_id)
    sha = (
        session.get_one(DocumentVersion, same_bytes_as).sha256
        if same_bytes_as is not None
        else "e" * 64
    )
    document = Document(package_id=package_id, kind="architectural")
    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=sha, size=1)
    session.add_all((document, artifact))
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=sha, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()


def _shop_version(session: Session, anchor: UUID) -> UUID:
    return session.get_one(ObservationCandidate, anchor).document_version_id


def _confirm(session: Session, revision_id: UUID, finding_id: UUID) -> None:
    review = open_session(session, package_revision_id=revision_id, reviewer="reviewer")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=finding_id,
        action=ReviewActionKind.CONFIRM,
        actor="reviewer",
        note="TEST ONLY: compared by hand",
    )
    session.flush()


# ---------------------------------------------------------------------------
# Which packages have a separate architect file
# ---------------------------------------------------------------------------


def test_only_different_bytes_make_a_separate_architect_file(session: Session) -> None:
    no_architect, anchors = _sealed_rows(session)
    assert not has_separate_architect_file(session, _revision(session, no_architect).id)

    add_architect_file(session, no_architect, same_bytes_as=_shop_version(session, anchors[0]))
    assert not has_separate_architect_file(session, _revision(session, no_architect).id)

    add_architect_file(session, no_architect, same_bytes_as=None)
    assert has_separate_architect_file(session, _revision(session, no_architect).id)


# ---------------------------------------------------------------------------
# A two-file package
# ---------------------------------------------------------------------------


def test_a_two_file_package_asks_once_and_every_row_names_the_file(
    session: Session, tmp_path: Path
) -> None:
    """Input: two rows, a separate architect file, nothing paired. Outcome: one package-level
    REVIEW_REQUIRED line that blocks sign-off until decided; each row says the file was not
    compared; no row-scoped architect finding."""
    package_id, anchors = _sealed_rows(session)
    add_architect_file(session, package_id, same_bytes_as=None)
    revision = _revision(session, package_id)

    findings = _run(session, package_id, tmp_path)

    architect = findings[ARCH_RULE]
    assert set(architect) == {PACKAGE}
    line = architect[PACKAGE]
    assert line.outcome == "REVIEW_REQUIRED"
    assert line.reason == SEPARATE_ARCHITECT_FILE_NOT_COMPARED
    assert line.scope_row_candidate_id is None
    assert line.id in approval_readiness(session, revision.id).blocking_finding_ids
    for anchor in anchors.values():
        plan = plan_architect_row(
            session, _row(session, package_id, anchor), None, separate_architect_file=True
        )
        assert plan.disposition is Disposition.NOT_COMPARED
        assert plan.reason == SEPARATE_ARCHITECT_FILE_ROW

    _confirm(session, revision.id, line.id)
    assert line.id not in approval_readiness(session, revision.id).blocking_finding_ids


def test_the_decision_carries_over_an_unchanged_rerun(session: Session, tmp_path: Path) -> None:
    """#1073: the same package-level line on a re-run keeps the reviewer's decision."""
    package_id, _anchors = _sealed_rows(session)
    add_architect_file(session, package_id, same_bytes_as=None)
    revision = _revision(session, package_id)
    first = _run(session, package_id, tmp_path)[ARCH_RULE][PACKAGE]
    _confirm(session, revision.id, first.id)

    again = _run(session, package_id, tmp_path)[ARCH_RULE][PACKAGE]

    assert again.id != first.id
    assert again.outcome == "REVIEW_REQUIRED"
    assert again.id not in approval_readiness(session, revision.id).blocking_finding_ids


@pytest.mark.parametrize("status", ["nothing_comparable", "no_scale", "none"])
def test_a_not_compared_row_names_the_separate_file_whatever_the_sheet_said(
    session: Session, status: str
) -> None:
    package_id, anchors = _sealed_rows(session)
    pairing = _pairing(status=status, source="none", reasons=("synthetic: nothing here",))

    plan = plan_architect_row(
        session,
        _row(session, package_id, anchors[0]),
        pairing,
        separate_architect_file=True,
    )

    assert plan.disposition is Disposition.NOT_COMPARED
    assert plan.reason == SEPARATE_ARCHITECT_FILE_ROW


def test_a_row_compared_from_a_pasted_view_is_unchanged_in_a_two_file_package(
    session: Session, tmp_path: Path
) -> None:
    package_id, anchors = _sealed_rows(session)
    run = _architect_drawing(session, anchors[0])
    overall = _architect_value(session, run, anchors[0], "3' - 7\"")
    pairings = {anchors[0]: _pairing(_overall(overall))}
    before = _run(session, package_id, tmp_path, pairings)[ARCH_RULE][anchors[0]]
    add_architect_file(session, package_id, same_bytes_as=None)

    after = _run(session, package_id, tmp_path, pairings)[ARCH_RULE]

    compared = after[anchors[0]]
    assert (compared.outcome, compared.reason, compared.notes, compared.scope_label) == (
        before.outcome,
        before.reason,
        before.notes,
        before.scope_label,
    )
    assert compared.outcome == "PASS", compared.reason
    # The architect's own file was still never compared: the package asks once.
    assert after[PACKAGE].outcome == "REVIEW_REQUIRED"
    assert after[PACKAGE].reason == SEPARATE_ARCHITECT_FILE_NOT_COMPARED
    assert set(after) == {anchors[0], PACKAGE}


# ---------------------------------------------------------------------------
# Unchanged
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("architect", ["none", "same file"])
def test_no_architect_file_or_the_same_file_keeps_the_line_that_blocks_nothing(
    session: Session, tmp_path: Path, architect: str
) -> None:
    package_id, anchors = _sealed_rows(session)
    if architect == "same file":
        add_architect_file(session, package_id, same_bytes_as=_shop_version(session, anchors[0]))
    revision = _revision(session, package_id)

    findings = _run(session, package_id, tmp_path)

    line = findings[ARCH_RULE][PACKAGE]
    assert set(findings[ARCH_RULE]) == {PACKAGE}
    assert line.outcome == "NO_APPLICABLE_RULE"
    assert line.reason == NOTHING_PAIRED_ON_REVISION
    assert line.id not in approval_readiness(session, revision.id).blocking_finding_ids
    plan = plan_architect_row(session, _row(session, package_id, anchors[1]), None)
    assert plan.reason == "No architect dimension is paired with this row."
