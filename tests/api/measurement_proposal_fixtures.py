"""The endpoint that asks a model which reading fills which field, and what it streams (#591).

**What is verified here is the wiring and the honesty of the display, not the model.** Whether the
model chooses well is `tests/workflow/test_assignment_bedrock.py`'s question and the gold set's;
what matters at this boundary is that the context handed over carries the facts the guard needs —
the sheet, the attachment, the chain and the position along it — and that a reviewer never receives
a proposal that has not passed every one of those checks.

Every model here is a stub. The provider is never called: an API test that needed AWS credentials
would skip in CI, which is where this boundary most needs watching.

Source: issue #591 · Verified module: `app/api/measurements.py`
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.document import DocumentKind, Page
from app.models.evidence import ObservationAssociation
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()


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


def _package_with_readings(
    session: Session,
    *,
    readings: tuple[tuple[str, int, int, str | None, int | None], ...],
    kind: str = DocumentKind.SHOP.value,
    attached: bool = True,
    state: PackageState = PackageState.RUNNING_CHECKS,
) -> tuple[UUID, dict[str, UUID]]:
    """A package whose shop drawing has one page of readings, each attached to a dimension line.

    `readings` is `(raw_text, numerator, denominator, chain_key, chain_position)`. A `None` chain is
    a line the detector found standing alone, which is most lines; a chain key groups the readings
    the drawing draws end to end, which is the only thing that makes an ordered run checkable.

    `attached=False` records the other kind of association row: a refusal, which is what
    `text_association` writes when it cannot say which line a number annotates. Written as a row
    from the start rather than substituted afterwards, because the table is append-only — a
    correction there is a new row, and a test that deletes one is testing a database we do not run.
    """
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="proposal tests"))
        session.flush()

    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=state)
    session.add(revision)

    artifact = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="0" * 64, size=1)
    session.add(artifact)
    session.flush()
    document = Document(package_id=package.id, kind=kind)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256="0" * 64, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash="0" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()

    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task_run)
    session.flush()
    run = ExtractionRun(
        task_run_id=task_run.id,
        extractor="test",
        extractor_version="1",
        config_hash="test",
    )
    session.add(run)
    session.flush()

    by_text: dict[str, UUID] = {}
    for position, (raw_text, numerator, denominator, chain_key, chain_position) in enumerate(
        readings
    ):
        candidate = ObservationCandidate(
            document_version_id=version.id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text=raw_text,
            value_numerator=numerator,
            value_denominator=denominator,
            unit="in",
            polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
            ambiguity_flags=[],
        )
        session.add(candidate)
        session.flush()
        by_text[raw_text] = candidate.id
        session.add(
            ObservationAssociation(
                candidate_id=candidate.id,
                extraction_run_id=run.id,
                start_x=f"0.{position}" if attached else None,
                start_y="0.5" if attached else None,
                end_x=f"0.{position + 1}" if attached else None,
                end_y="0.5" if attached else None,
                signals=["nearest line"] if attached else [],
                refusal_reason=None if attached else "two lines were equally close",
                chain_key=chain_key if attached else None,
                chain_position=chain_position if attached else None,
            )
        )
    session.commit()
    return package.id, by_text


# ---------------------------------------------------------------------------
# The required-inputs form contract
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# What the model is given
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# An empty form is not yet an answer about the drawings (#606)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# #1221: the call is recorded, committed, and counted on the Usage page
# ---------------------------------------------------------------------------

RECORDED_MODEL = "test-assignment-model"


@pytest.fixture
def rates(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "rates.json"
    path.write_text(
        json.dumps(
            {
                "source": "test prices, authored",
                "retrieved": "2026-10-14",
                "currency": "USD",
                "rates": {
                    RECORDED_MODEL: {"input_per_1k_tokens": "1", "output_per_1k_tokens": "2"}
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("GV_MODEL_RATES_FILE", str(path))
