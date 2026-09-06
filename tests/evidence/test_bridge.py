"""The read-to-decide bridge: an extracted reading becomes a verdict (#530).

Verification for: `app/evidence/confirm.py`, `workflow/evidence_operands.py`, and the evidence path
through `DatabaseStages.run_checks`.

**This is the link that was missing.** The system could read a drawing and it could decide, and the
two had never been connected: extraction stopped at untyped candidates, and every verdict anyone had
seen came from a reviewer typing numbers into a form. `evidence/normalize.py` and `evidence/gate.py`
had no production caller at all.

The test that matters is `test_a_confirmed_reading_becomes_a_verdict_without_anyone_retyping_it`.
Everything else here guards a way of getting that wrong.

**The type comes from a human.** Nothing in this file — or in the code it exercises — infers a
semantic type from the drawing. That inference is gated on the real drawings (#274) and the vocabulary
Q20 defers, and `test_nothing_types_a_candidate_on_its_own` is the guard that it stays absent.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
from app.audit.events import AuditEvent
from app.db.session import session_factory
from app.evidence.confirm import ConfirmationRefused, RefusalReason, confirm_candidate_type
from app.models import (
    CanonicalObservation,
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
from app.models.runs import TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_reader import _pdf
from tests.workflow.test_stages import (
    _outcome_for,
    _project_depth_parameters,
    _publish_rulebook,
)
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: What `CT-DEPTH-001` reads: a shop countertop depth, semantic type `CT010`.
#:
#: 25 1/2" against a 24" cabinet and a 1 1/2" overhang is exactly right, so a confirmed reading of it
#: must PASS — and a quarter inch out must FAIL. Those two are the whole point: the verdict has to
#: follow the number on the drawing, not merely appear.
DEPTH_TYPE = "CT010"

#: Written the way a GV drawing writes a dimension: millimetres with the inches in brackets.
#:
#: Not `25 1/2"`, and the reason is worth knowing. `extract_words` splits at spaces, so that
#: token reaches the recorder as `25` and `1/2"` — and the only fragment carrying a unit is the
#: half inch. A test built on it confirms a reading of half an inch and gets a FAIL that looks
#: like the bridge working and is the bridge reading half a number. The dual token is kept whole
#: by the reader (#528), which is the shape a real sheet uses anyway.
EXACT_DEPTH = "648 [25 1/2]"
WRONG_DEPTH = "641 [25 1/4]"


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


def _revision(session: Session, store: LocalStore, *, token: str) -> PackageRevision:
    """A revision whose one shop drawing states one dimension."""
    data = _pdf(f"BT /F1 10 Tf 1 0 0 1 20 70 Tm ({token}) Tj ET\n".encode())
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name=f"bridge {uuid4()}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.EXTRACTING
    )
    session.add(revision)
    session.flush()
    document = Document(package_id=package.id, kind="shop")
    session.add(document)
    session.flush()
    key = storage_key(document.id, digest)
    session.add(SourceArtifact(storage_key=key, sha256=digest, size=len(data)))
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
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    session.add(
        TaskRun(
            workflow_run_id=workflow_run.id,
            idempotency_key=stage_idempotency_key(
                package_revision_id=revision.id,
                stage="extract_pages",
                engine_version=ENGINE_VERSION,
            ),
            task_type="extract_pages",
            attempt=1,
            outcome="claimed",
        )
    )
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")
    return revision


def _extract(session: Session, store: LocalStore, *, token: str) -> tuple[PackageRevision, UUID]:
    """Read the drawing and return the revision with the candidate carrying its dimension."""
    revision = _revision(session, store, token=token)
    DatabaseStages(store).extract_pages(session, revision.id)
    candidate = (
        session.execute(
            select(ObservationCandidate).where(ObservationCandidate.value_numerator.is_not(None))
        )
        .scalars()
        .one()
    )
    return revision, candidate.id


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------


def test_a_confirmed_reading_becomes_a_verdict_without_anyone_retyping_it(
    session: Session, store: LocalStore
) -> None:
    """**The acceptance criterion.** Drawing in, reviewer names the quantity, verdict out.

    Note what is *not* passed to `DatabaseStages`: any operands. Every previous test that produced a
    decision handed them in, which meant a person had typed the number. Here the only human input is
    the string `CT010` — what the reading *is* — and the value comes from the drawing.
    """
    revision, candidate_id = _extract(session, store, token=EXACT_DEPTH)
    _publish_rulebook(session)
    _project_depth_parameters(session, revision)

    confirmed = confirm_candidate_type(
        session,
        candidate_id=candidate_id,
        semantic_type=DEPTH_TYPE,
        confirmed_by="a reviewer",
    )
    assert not isinstance(confirmed, ConfirmationRefused), confirmed

    DatabaseStages(store).run_checks(session, revision.id)

    assert _outcome_for(session, revision, "CT-DEPTH-001") == "PASS"


def test_the_verdict_follows_the_drawing_and_not_the_confirmation(
    session: Session, store: LocalStore
) -> None:
    """A quarter inch out is a FAIL, and the reviewer said exactly the same thing as before.

    Without this the test above would pass on a bridge that always produced PASS — the failure mode
    that matters most here, because a system that decided before reading would look identical on a
    drawing that happens to be correct.
    """
    revision, candidate_id = _extract(session, store, token=WRONG_DEPTH)
    _publish_rulebook(session)
    _project_depth_parameters(session, revision)

    confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type=DEPTH_TYPE, confirmed_by="a reviewer"
    )
    DatabaseStages(store).run_checks(session, revision.id)

    assert _outcome_for(session, revision, "CT-DEPTH-001") == "FAIL"


def test_without_a_confirmation_the_check_still_abstains(
    session: Session, store: LocalStore
) -> None:
    """The reading alone is not evidence. Somebody has to say what it is.

    This is the state the system was in before #530, and it must remain the state until a human acts:
    a candidate that nobody has typed cannot reach a rule, however unambiguous it looks.
    """
    revision, _ = _extract(session, store, token=EXACT_DEPTH)
    _publish_rulebook(session)
    _project_depth_parameters(session, revision)

    DatabaseStages(store).run_checks(session, revision.id)

    assert _outcome_for(session, revision, "CT-DEPTH-001") != "PASS"


def test_the_confirmation_names_who_made_it(session: Session, store: LocalStore) -> None:
    """A reading enters the verdict path because a person put their name to it.

    In the same transaction as the observation, so there is no window in which evidence exists and
    the record of who admitted it does not.
    """
    _, candidate_id = _extract(session, store, token=EXACT_DEPTH)

    observation = confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type=DEPTH_TYPE, confirmed_by="ana@example.com"
    )
    assert isinstance(observation, CanonicalObservation)

    event = session.execute(select(AuditEvent)).scalars().one()
    assert event.actor == "ana@example.com"
    assert event.target_id == observation.id
    assert event.target_type == "canonical_observation"


def test_the_candidate_itself_is_never_edited(session: Session, store: LocalStore) -> None:
    """`observation_candidates` is append-only, so a confirmation cites the reading rather than
    changing it. What the extractor said stays exactly as it said it, including having no type."""
    _, candidate_id = _extract(session, store, token=EXACT_DEPTH)

    confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type=DEPTH_TYPE, confirmed_by="a reviewer"
    )

    candidate = session.get(ObservationCandidate, candidate_id)
    assert candidate is not None
    assert candidate.semantic_guess is None, "the confirmation edited the extractor's own record"


# ---------------------------------------------------------------------------
# The line the bridge does not cross
# ---------------------------------------------------------------------------


def test_nothing_types_a_candidate_on_its_own(session: Session, store: LocalStore) -> None:
    """**The hard stop.** Reading a drawing produces no semantic types, ever.

    Auto-typing is the one thing gated on the real drawings (#274) and the vocabulary Q20 defers.
    `semantic_guess` is the seam it will fill; until then the only thing that fills it is a person,
    and a heuristic that quietly began guessing would look like the bridge working better.
    """
    _extract(session, store, token=EXACT_DEPTH)

    guesses = list(
        session.execute(select(ObservationCandidate.semantic_guess).distinct()).scalars()
    )
    assert guesses == [None], f"extraction assigned a semantic type: {guesses}"
    assert session.execute(select(CanonicalObservation)).scalars().all() == []


def test_an_unknown_type_is_refused(session: Session, store: LocalStore) -> None:
    """A reviewer cannot invent a quantity the rulebook has never heard of.

    The vocabulary is `rules/semantic_types.py`, and a free-text type would be a reading nothing
    could ever match to a rule input — evidence in name only.
    """
    _, candidate_id = _extract(session, store, token=EXACT_DEPTH)

    refused = confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type="COUNTERTOP_DEPTH", confirmed_by="a"
    )

    assert isinstance(refused, ConfirmationRefused)
    assert refused.reason is RefusalReason.UNKNOWN_TYPE


def test_confirming_the_same_reading_twice_is_refused(session: Session, store: LocalStore) -> None:
    """One reading is one observation.

    Confirming twice would put two observations of one dimension in front of a rule that asked for it
    once, which reads as the drawing stating it twice — and `operands_from_evidence` would then
    decline to choose, so the check would abstain for a reason nobody could see on the sheet.
    """
    _, candidate_id = _extract(session, store, token=EXACT_DEPTH)
    first = confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type=DEPTH_TYPE, confirmed_by="a reviewer"
    )
    assert isinstance(first, CanonicalObservation)

    second = confirm_candidate_type(
        session, candidate_id=candidate_id, semantic_type=DEPTH_TYPE, confirmed_by="a reviewer"
    )

    assert isinstance(second, ConfirmationRefused)
    assert second.reason is RefusalReason.ALREADY_CONFIRMED


def test_a_reading_from_a_page_with_no_recorded_transform_is_refused(
    session: Session, store: LocalStore
) -> None:
    """A reading that cannot be placed on the drawing is not evidence.

    Pages read before #530 have no boxes, and the conversion from image pixels to stored geometry
    normalises by the crop box. Guessing `(0, 0, width, height)` is right for most PDFs and silently
    wrong for any page whose crop box is inset — and being silently wrong here would put a finding's
    evidence on a region of the drawing nobody wrote.

    The old page is built rather than edited: `pages` carries `Immutable` and the trigger refuses an
    UPDATE, which is how the first version of this test failed. That refusal is the schema working,
    so the row is inserted in the shape an older run genuinely left behind.
    """
    from app.models import Page
    from app.models.runs import ExtractionRun

    _, candidate_id = _extract(session, store, token=EXACT_DEPTH)
    original = session.get(ObservationCandidate, candidate_id)
    assert original is not None

    older = Page(
        document_version_id=original.document_version_id,
        index=99,
        content_hash="c" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        media_box=None,
        crop_box=None,
    )
    session.add(older)
    session.flush()
    run = session.get(ExtractionRun, original.extraction_run_id)
    assert run is not None
    stale = ObservationCandidate(
        document_version_id=original.document_version_id,
        page_id=older.id,
        extraction_run_id=run.id,
        raw_text=original.raw_text,
        value_numerator=original.value_numerator,
        value_denominator=original.value_denominator,
        unit=original.unit,
        unit_guess=original.unit_guess,
        semantic_guess=None,
        polygon=original.polygon,
        coordinate_space="image",
        confidence=None,
        ambiguity_flags=[],
    )
    session.add(stale)
    session.flush()

    refused = confirm_candidate_type(
        session, candidate_id=stale.id, semantic_type=DEPTH_TYPE, confirmed_by="a reviewer"
    )

    assert isinstance(refused, ConfirmationRefused)
    assert refused.reason is RefusalReason.NO_TRANSFORM
