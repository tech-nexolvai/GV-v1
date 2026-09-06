"""What the deliverable table refuses (#519).

Verification for: `app/models/verdicts.py:OutputArtifact` and migration `0031_output_artifacts`.

The constraints here are the ones that keep a recorded deliverable honest: it names a real revision,
it names a kind the code can actually produce, its digest looks like a digest, and the same file is
not recorded twice. The append-only trigger is asserted too — a deliverable that could be edited in
place would let the file a vendor was sent differ from the record of what was sent.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    OutputArtifact,
    OutputArtifactKind,
    Package,
    PackageRevision,
    PackageState,
    Project,
)
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

DIGEST = "e" * 64


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine):
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _revision(session: Session) -> PackageRevision:
    project = Project(name=f"outputs {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.flush()
    return revision


def _artifact(revision: PackageRevision, **overrides: object) -> OutputArtifact:
    fields: dict[str, object] = {
        "package_revision_id": revision.id,
        "kind": OutputArtifactKind.FINDINGS_WORKBOOK.value,
        "storage_key": f"outputs/{revision.id}/report.xlsx",
        "sha256": DIGEST,
        "media_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "findings": 8,
    }
    fields.update(overrides)
    return OutputArtifact(**fields)  # type: ignore[arg-type]


def test_a_deliverable_records_what_was_produced(session: Session) -> None:
    """The ordinary case, so the refusals below are refusals of something otherwise valid."""
    revision = _revision(session)
    session.add(_artifact(revision))
    session.flush()

    stored = session.execute(select(OutputArtifact)).scalar_one()

    assert stored.kind == "findings_workbook"
    assert stored.findings == 8


def test_a_kind_the_code_cannot_produce_is_refused(session: Session) -> None:
    """`redline` is the one somebody will try, and it is exactly the one to refuse.

    An annotated drawing needs each finding tied to a region of the sheet, which needs semantic
    typing — deliberately absent until the real drawings (#274) and the vocabulary Q20 defers. The
    constraint gains the value on the day something can honestly write one, the same lesson as
    `ModelInvocationOutcome.FAILED`, which the database rejected for as long as the enum had a member
    the `CHECK` did not.
    """
    revision = _revision(session)
    session.add(_artifact(revision, kind="redline"))

    with pytest.raises(IntegrityError, match="output_artifact_kind"):
        session.flush()


def test_the_same_file_cannot_be_recorded_twice(session: Session) -> None:
    """The key is content-addressed, so regenerating unchanged findings produces the same pair.

    Two rows would claim two deliverables where there is one, and a reader counting them would
    overstate what was sent.
    """
    revision = _revision(session)
    session.add(_artifact(revision))
    session.flush()
    session.add(_artifact(revision))

    with pytest.raises(IntegrityError, match="uq_output_artifacts_key_sha"):
        session.flush()


@pytest.mark.parametrize(
    ("field", "value", "constraint"),
    [
        ("sha256", "not-a-digest", "output_artifact_sha256"),
        ("storage_key", "", "output_artifact_storage_key"),
        ("media_type", "", "output_artifact_media_type"),
        ("findings", -1, "output_artifact_findings"),
    ],
)
def test_a_malformed_deliverable_is_refused(
    session: Session, field: str, value: object, constraint: str
) -> None:
    """Each column that would make the row unusable if it were wrong."""
    revision = _revision(session)
    session.add(_artifact(revision, **{field: value}))

    with pytest.raises(IntegrityError, match=constraint):
        session.flush()


def test_a_deliverable_cannot_name_a_revision_that_does_not_exist(session: Session) -> None:
    """A file about nothing is not a record of anything."""
    revision = _revision(session)
    session.add(_artifact(revision, package_revision_id=uuid4()))

    with pytest.raises(IntegrityError):
        session.flush()


def test_a_recorded_deliverable_cannot_be_edited(session: Session) -> None:
    """Append-only, enforced by the trigger 0031 installs rather than by the marker alone.

    Until 0013 the `Immutable` marker was enforced by nothing and an `UPDATE` simply succeeded. This
    is the table somebody would most want to tidy after the fact: it says what was sent.
    """
    revision = _revision(session)
    artifact = _artifact(revision)
    session.add(artifact)
    session.flush()
    identifier = artifact.id
    session.commit()

    with pytest.raises((IntegrityError, ProgrammingError)):
        session.execute(
            text("UPDATE output_artifacts SET findings = 99 WHERE id = :id"), {"id": identifier}
        )
        session.flush()
