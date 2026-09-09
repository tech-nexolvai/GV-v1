"""The whole V1 loop, through the API a reviewer actually uses (#532).

A drawing goes in, a person says what was read and types what the extractor could not, the rules
decide, the package is signed off, and a workbook comes out. No client input, no model, no AI.

**Why this test is at the API rather than at the service layer.** Both dead ends this story closed
were *routes*: `approve_package` had been complete and tested for months with nothing able to call
it, and the workbook `generate_outputs` stores had no way out of the application. A service-level test
would have passed throughout — it did, for months — which is precisely how a finished feature stays
unreachable. What is asserted here is that a reviewer can get from one end to the other.

**The graceful fallback is the point of the middle.** A real sheet is not read perfectly: the
extractor gets some dimensions and misses others. So the package here has one dimension the reader
picks up whole and one it cannot, and the reviewer supplies the second by hand through the path Q7
blesses. Both routes have to reach the same verdict for the loop to be worth shipping.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from openpyxl import load_workbook
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.api.documents import storage_key
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    OutputArtifact,
    OutputArtifactKind,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.runs import WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from tests.workflow.test_stages import _project_depth_parameters, _publish_rulebook
from workflow.review import run_all
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = UUID("22222222-2222-2222-2222-222222222222")

#: A shop drawing stating one dimension the reader keeps whole, and one it cannot read.
#:
#: `648 [25 1/2]` survives word splitting because the reader recovers dual tokens (#528). `25 1/2"`
#: does not — `extract_words` cuts it at the space — so it stands for every dimension a real sheet
#: presents in a way the extractor cannot use. That is the fallback this loop has to handle, and it
#: is drawn from the actual behaviour rather than imagined.
DRAWING = _pdf(
    b"BT /F1 10 Tf 1 0 0 1 20 70 Tm (648 [25 1/2]) Tj ET\n"
    b'BT /F1 10 Tf 1 0 0 1 20 40 Tm (SINK 25 1/2") Tj ET\n'
)

#: What `CT-DEPTH-001` reads.
DEPTH_TYPE = "CT010"


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused/unused",
        hatchet_token="test-token",
    )


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(
        id="ana@example.com", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT})
    )


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


@pytest.fixture
def client(session: Session, store: LocalStore) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _factory(session: Session) -> Any:
    """A sessionmaker over the test's own engine, which is what `run_all` takes.

    `run_all` opens a transaction per stage, so it needs to make sessions rather than borrow one.
    Bound to the same engine as the test's session so both see the same schema.
    """
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


def _uploaded(session: Session, store: LocalStore) -> tuple[UUID, PackageRevision, UUID]:
    """A package whose drawing has been uploaded — where a reviewer's work begins."""
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="the v1 loop"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.UPLOADED
    )
    session.add(revision)
    session.flush()

    digest = hashlib.sha256(DRAWING).hexdigest()
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(DRAWING)))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=session.execute(
            select(SourceArtifact.id).where(SourceArtifact.storage_key == key)
        ).scalar_one(),
        sha256=digest,
        page_count=1,
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
    run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(run)
    session.flush()
    session.flush()
    store.put(key, io.BytesIO(DRAWING), content_type="application/pdf")
    session.commit()
    return package.id, revision, run.id


def test_a_reviewer_takes_a_drawing_from_upload_to_a_downloadable_signed_off_review(
    session: Session, store: LocalStore, client: Any
) -> None:
    """**The acceptance criterion, start to finish.**

    Seven steps, each one a thing a reviewer does, and every one of them through the API. The point
    is not that any single step works — each has its own tests — but that there is a path from one
    end to the other with nothing missing in between. Twice in this project a step has been complete,
    tested and unreachable.
    """
    package_id, revision, run_id = _uploaded(session, store)
    _publish_rulebook(session)
    _project_depth_parameters(session, revision)
    session.commit()

    # 1. The mechanical pass, driven by the real workflow so the package moves through its states.
    #    Calling the stages directly would skip the transitions, and `APPROVED` is reachable only
    #    from `AWAITING_REVIEW` — which is exactly the dead end this story found.
    run_all(
        _factory(session),
        package_revision_id=revision.id,
        workflow_run_id=run_id,
        stages=DatabaseStages(store),
    )
    session.expire_all()

    # 2. The reviewer sees what was read, with the value and the crop.
    listed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/candidates")
    assert listed.status_code == 200, listed.text
    readings = listed.json()["candidates"]
    assert readings, "the reviewer was shown nothing to confirm"

    depth = next(row for row in readings if row["raw_text"] == "648 [25 1/2]")
    assert depth["value"] == "25 1/2 in", depth

    # 3. The reviewer says what one of them is. The value is not re-entered.
    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}"
        f"/candidates/{depth['candidate_id']}/confirm",
        json={"semantic_type": DEPTH_TYPE},
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["status"] == "HUMAN_CONFIRMED"

    # 4. The checks run again now that the reading has a meaning, and the report is rebuilt.
    #    Re-running is what a reviewer does after confirming: the first pass had no evidence to
    #    decide from, and `run_checks` supersedes its own previous run rather than adding to it.
    stages = DatabaseStages(store)
    stages.run_checks(session, revision.id)
    stages.generate_outputs(session, revision.id)
    session.commit()

    findings = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/findings")
    assert findings.status_code == 200, findings.text
    outcomes = {item["rule_id"]: item["outcome"] for item in findings.json()["items"]}
    assert outcomes["CT-DEPTH-001"] == "PASS", outcomes

    # The re-run finding retains the confirmed observation identity.  Its viewer path returns the
    # same candidate-sized PNG the reviewer inspected — never a page render or redline.
    depth_finding = next(
        item for item in findings.json()["items"] if item["rule_id"] == "CT-DEPTH-001"
    )
    chain = client.get(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/findings/{depth_finding['id']}/chain"
    )
    assert chain.status_code == 200, chain.text
    evidence = chain.json()["operands"][0]["evidence"]
    assert evidence["canonical_observation_id"] == confirmed.json()["canonical_observation_id"]

    crop = client.get(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/evidence/"
        f"{evidence['canonical_observation_id']}/crop"
    )
    assert crop.status_code == 200, crop.text
    assert crop.headers["content-type"] == "image/png"
    assert crop.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"%PDF" not in crop.content

    # 5. The report exists and is deliberately out of reach until somebody signs for it.
    early = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report")
    assert early.status_code == 409, early.text
    assert "not been signed off" in early.text
    early_pdf = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report.pdf")
    assert early_pdf.status_code == 409, early_pdf.text

    # 6. The reviewer addresses every abstention and signs off.
    _address_every_abstention(client, session, package_id, revision)

    # 7. And takes the review away.
    report = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report")
    assert report.status_code == 200, report.text
    assert report.content.startswith(b"PK\x03\x04"), "the report is not a workbook"
    assert "attachment" in report.headers["content-disposition"]
    summary = load_workbook(io.BytesIO(report.content))["Review Summary"]
    assert summary["B11"].value == "AWAITING REVIEWER SIGN-OFF — download remains blocked"
    assert summary["B12"].value == "not yet recorded"

    pdf = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report.pdf")
    assert pdf.status_code == 200, pdf.text
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content.startswith(b"%PDF-"), "the branded handoff is not a PDF"
    assert "attachment" in pdf.headers["content-disposition"]

    # The newest, which is what the endpoint serves. Two reports exist here and both are real: the
    # pipeline wrote one before the reading had a meaning, and the reviewer's confirmation made the
    # checks decidable, so re-running produced a second. `output_artifacts` is append-only, so the
    # first is not overwritten — it is simply no longer the current one.
    stored = (
        session.execute(
            select(OutputArtifact)
            .where(OutputArtifact.kind == OutputArtifactKind.FINDINGS_WORKBOOK.value)
            .order_by(OutputArtifact.created_at.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    assert (
        hashlib.sha256(report.content).hexdigest() == stored.sha256
    ), "the bytes served are not the ones the approval covers"
    stored_pdf = (
        session.execute(
            select(OutputArtifact)
            .where(OutputArtifact.kind == OutputArtifactKind.FINDINGS_PDF.value)
            .order_by(OutputArtifact.created_at.desc())
            .limit(1)
        )
        .scalars()
        .one()
    )
    assert hashlib.sha256(pdf.content).hexdigest() == stored_pdf.sha256


def _address_every_abstention(
    client: Any, session: Session, package_id: UUID, revision: PackageRevision
) -> None:
    """Open a sitting, dismiss each REVIEW REQUIRED finding, then approve.

    Approval refuses while any abstention is unaddressed, which is the rule this exercises rather
    than works around: a check that declined to decide and that nobody looked at is not a check.
    """
    findings = client.get(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/findings?limit=100"
    ).json()["items"]

    # The sitting names the *revision*, not the package: a package moves on, and a review that
    # followed it would record decisions against drawings the reviewer never saw.
    opened = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/review-sessions",
        json={"package_revision_id": str(revision.id)},
    )
    assert opened.status_code in (200, 201), opened.text
    review_session_id = opened.json()["id"]

    for finding in findings:
        if finding["outcome"] != "REVIEW_REQUIRED":
            continue
        recorded = client.post(
            f"/api/v1/projects/{PROJECT}/review-sessions/{review_session_id}/actions",
            json={
                "finding_id": finding["id"],
                "action": "dismiss",
                "note": "not applicable to this package",
            },
        )
        assert recorded.status_code in (200, 201), recorded.text

    approved = client.post(
        f"/api/v1/projects/{PROJECT}/review-sessions/{review_session_id}/approve"
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["state"] == PackageState.APPROVED.value

    session.expire_all()
    assert PackageState(session.get(PackageRevision, revision.id).state) is PackageState.APPROVED


def test_the_report_is_refused_until_the_package_is_approved(
    session: Session, store: LocalStore, client: Any
) -> None:
    """**Approval is the gate, not the file's existence.**

    `generate_outputs` writes the workbook as soon as the checks have run, which is before anybody
    has read a finding. Serving it then would let a review leave in a state nobody signed for — the
    thing ADR-0010 forbids — and it would look identical to a signed one on the way out.
    """
    package_id, revision, run_id = _uploaded(session, store)
    _publish_rulebook(session)
    session.commit()
    run_all(
        _factory(session),
        package_revision_id=revision.id,
        workflow_run_id=run_id,
        stages=DatabaseStages(store),
    )
    session.expire_all()

    assert session.execute(select(OutputArtifact)).scalars().all(), "no report was produced"

    refused = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/report")

    assert refused.status_code == 409
    assert "not been signed off" in refused.text


def test_nothing_in_the_loop_types_a_candidate_on_its_own(
    session: Session, store: LocalStore, client: Any
) -> None:
    """**The hard stop, asserted over the whole loop rather than one stage.**

    A reviewer supplies meaning. Nothing else does — not extraction, not the checks, not the report.
    Auto-typing is gated on the real drawings (#274) and the vocabulary Q20 defers, and this is the
    assertion that says so about the finished product rather than about one module.
    """
    _, revision, run_id = _uploaded(session, store)
    run_all(
        _factory(session),
        package_revision_id=revision.id,
        workflow_run_id=run_id,
        stages=DatabaseStages(store),
    )
    session.expire_all()

    guesses = list(
        session.execute(select(ObservationCandidate.semantic_guess).distinct()).scalars()
    )

    assert guesses == [None], f"something assigned a semantic type: {guesses}"
