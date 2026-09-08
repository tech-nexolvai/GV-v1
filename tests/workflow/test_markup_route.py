"""A reviewer's annotations, recorded as candidates by the pipeline.

Verification for: `DatabaseStages._read_page_markup` in `workflow/stages.py`,
`app/evidence/record.py:record_markup_candidates` and migration 0038 (#543).

**The drawing here is the shape of the real client set, built by hand.** An all-but-empty page
content stream with the sheet in a `/Stamp` annotation and the reviewer's corrections in `/FreeText`
ones — which is what `AI_Set 2` is, and what made every page of it route to OCR while twenty exact
strings sat unread in the file. The PDFs come from `tests/extraction/test_annotations.py`, so nothing
about a client drawing is in this repository.

The two tests to read first are `test_the_markup_route_does_not_displace_the_pixel_route`, which is
the whole reason this is additive, and `test_a_note_that_looks_like_a_dimension_gets_no_meaning`,
which is the hard stop.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.documents import storage_key
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
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_annotations import (
    KNOWN_AUTHOR,
    KNOWN_MARKUP,
    _appearance,
    _free_text,
    _pdf,
    _stamp,
)
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.stages import (
    MARKUP_EXTRACTOR,
    MARKUP_EXTRACTOR_VERSION,
    DatabaseStages,
)

pytest_plugins = ("tests.app.postgres_fixture",)

#: A note that is a dimension, a note that is a vendor tag, and a note that is neither — the three
#: kinds a real sheet's markup holds, and all three must be recorded.
ANNOTATED = _pdf(
    annotations=[
        _free_text(KNOWN_MARKUP, rect=b"[40 40 120 60]"),
        _free_text("LA-002-CUST", rect=b"[150 40 250 60]"),
        _free_text("Scribe to fit as required on site", rect=b"[40 200 300 220]"),
        _stamp(appearance_object=9),
    ],
    extra_objects=[_appearance()],
)

#: The same sheet with nothing written on it: a vendor drawing and no reviewer.
UNANNOTATED = _pdf(annotations=[_stamp(appearance_object=6)], extra_objects=[_appearance()])


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


def _revision(session: Session, store: LocalStore, *, data: bytes) -> PackageRevision:
    """A package revision with one annotated document, and its bytes in the store."""
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name="markup route test")
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
    artifact = SourceArtifact(storage_key=key, sha256=digest, size=len(data))
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
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


class _SilentOcr:
    """An OCR engine that finds nothing, so the markup rows are unambiguous.

    **It is called**, on every page here: these sheets have no vector text, so they take the OCR
    route — which is the situation this whole issue is about. Returning nothing is the engine's own
    documented behaviour for a page with no legible text, so the route runs exactly as it does in
    production and simply produces no rows to confuse with the markup ones.

    Not a stub standing in for something untested: `tests/extraction/test_ocr.py` covers the real
    engine. This avoids loading ONNX models to prove a point about annotations.
    """

    name = "silent-ocr"
    version = "silent/1"

    def read(self, rgb: bytes, *, width: int, height: int) -> tuple[()]:
        del rgb, width, height
        return ()


def _stages(store: LocalStore) -> DatabaseStages:
    return DatabaseStages(store=store, dpi=150, ocr_engine=_SilentOcr())  # type: ignore[arg-type]


def _extract(session: Session, store: LocalStore, *, data: bytes = ANNOTATED) -> PackageRevision:
    revision = _revision(session, store, data=data)
    session.commit()
    _stages(store).extract_pages(session, revision.id)
    session.commit()
    return revision


def _candidates(session: Session) -> list[ObservationCandidate]:
    return list(session.execute(select(ObservationCandidate)).scalars())


def _markup_rows(session: Session) -> list[ObservationCandidate]:
    """Candidates from the markup run, found the way a reviewer would: through the run."""
    runs = {
        run.id
        for run in session.execute(
            select(ExtractionRun).where(ExtractionRun.extractor == MARKUP_EXTRACTOR)
        ).scalars()
    }
    return [row for row in _candidates(session) if row.extraction_run_id in runs]


# ---------------------------------------------------------------------------
# The reviewer's text reaches the database
# ---------------------------------------------------------------------------


def test_every_reviewer_note_becomes_a_candidate(session: Session, store: LocalStore) -> None:
    """Input: a sheet with three `/FreeText` notes. Outcome: three candidates, exact text.

    **The gap this closes.** These pages have no vector text, so they route to OCR, and until now
    the reviewer's corrections were rasterised and guessed at while sitting in the file as strings.
    """
    _extract(session, store)

    rows = _markup_rows(session)

    assert sorted(row.raw_text for row in rows) == sorted(
        [KNOWN_MARKUP, "LA-002-CUST", "Scribe to fit as required on site"]
    )


def test_a_note_carrying_its_own_unit_gets_a_value(session: Session, store: LocalStore) -> None:
    """Input: a note reading `185 1/4"`. Outcome: an exact value, from the marked inch.

    The parsing rule does not soften for exact text: a token carrying its own unit gets a value and
    a bare number does not. Exact characters are not the same thing as a known unit.
    """
    _extract(session, store)

    row = next(item for item in _markup_rows(session) if item.raw_text == KNOWN_MARKUP)

    assert row.value_numerator == 741
    assert row.value_denominator == 4
    assert row.unit == "in"


def test_a_note_that_is_not_a_dimension_is_recorded_without_a_value(
    session: Session, store: LocalStore
) -> None:
    """Input: a vendor tag and a site instruction. Outcome: rows with no value.

    Recorded rather than filtered, which is `record_candidates`' stated rule: which text on a drawing
    is a dimension is decided later, and a reader that pre-filtered would make that decision by guess
    and leave no trace of it. `LA-002-CUST` is also exactly what the alias layer (#167) will want.
    """
    _extract(session, store)

    tag = next(item for item in _markup_rows(session) if item.raw_text == "LA-002-CUST")

    assert tag.value_numerator is None
    assert tag.unit is None
    assert tag.ambiguity_flags


def test_the_author_is_recorded(session: Session, store: LocalStore) -> None:
    """Input: notes whose `/T` names their author. Outcome: `source_author` on every row.

    Kept because a reviewer's correction of a vendor's dimension outranks it partly by virtue of who
    wrote it, and the file is the only place that fact exists.
    """
    _extract(session, store)

    assert {row.source_author for row in _markup_rows(session)} == {KNOWN_AUTHOR}


def test_a_reading_from_a_drawing_has_no_author(session: Session, store: LocalStore) -> None:
    """Outcome: candidates from the pixel routes leave `source_author` null.

    Null is the ordinary case: a number read off a drawing has no author at all, and a value here
    would be an authorship nobody stated.
    """
    _extract(session, store)

    markup = {row.id for row in _markup_rows(session)}
    others = [row for row in _candidates(session) if row.id not in markup]

    assert all(row.source_author is None for row in others)


# ---------------------------------------------------------------------------
# Additive, not a third branch
# ---------------------------------------------------------------------------


def test_the_markup_route_does_not_displace_the_pixel_route(
    session: Session, store: LocalStore
) -> None:
    """**Why this is additive.** Outcome: the page still takes its own route, and both are recorded.

    The vendor's outlined numbers still need a reader; the markup route cannot read them and must not
    stand in for whatever does. Asserted through the runs, because that is how the two are told
    apart: each route opens its own, exactly as `_read_page_by_ocr` does.
    """
    _extract(session, store)

    extractors = {
        run.extractor: run.extractor_version
        for run in session.execute(select(ExtractionRun)).scalars()
    }

    assert MARKUP_EXTRACTOR in extractors
    assert extractors[MARKUP_EXTRACTOR] == MARKUP_EXTRACTOR_VERSION
    # The page's own route opened a run too — the markup route did not replace it.
    assert len(extractors) > 1, extractors


def test_the_markup_run_records_the_resolution_it_read_at(
    session: Session, store: LocalStore
) -> None:
    """Outcome: `dpi` and a matching `config_hash` on the markup run.

    The same dpi decides the stored geometry these rows carry, and `open_extraction_run` treats a
    different configuration as a different run — without that, a re-read at another resolution would
    reuse the first run and the rows would describe a configuration that did not produce them.
    """
    _extract(session, store)

    run = session.execute(
        select(ExtractionRun).where(ExtractionRun.extractor == MARKUP_EXTRACTOR)
    ).scalar_one()

    assert run.dpi == 150
    assert run.config_hash == "dpi=150"


def test_a_page_with_no_markup_opens_no_run(session: Session, store: LocalStore) -> None:
    """Input: a sheet nobody annotated. Outcome: no markup run and no markup rows.

    Most sheets in most sets carry no markup. A run opened for a page with no notes would be a row
    claiming work that did not happen.
    """
    _extract(session, store, data=UNANNOTATED)

    assert _markup_rows(session) == []
    assert (
        session.execute(
            select(ExtractionRun).where(ExtractionRun.extractor == MARKUP_EXTRACTOR)
        ).all()
        == []
    )


def test_running_the_stage_twice_does_not_double_the_notes(
    session: Session, store: LocalStore
) -> None:
    """Outcome: a redelivery records the same rows, not a second copy of them.

    A killed worker is redelivered and reclaims the same task run. The rows would each be correct,
    which is what makes duplication dangerous: nothing downstream can tell one note recorded twice
    from two notes that happen to read the same, and the same text appears twice on a real sheet.
    """
    revision = _extract(session, store)
    first = len(_markup_rows(session))

    _stages(store).extract_pages(session, revision.id)
    session.commit()

    assert len(_markup_rows(session)) == first


# ---------------------------------------------------------------------------
# The hard stop
# ---------------------------------------------------------------------------


def test_a_note_that_looks_like_a_dimension_gets_no_meaning(
    session: Session, store: LocalStore
) -> None:
    """**The hard stop, at this seam too.** Outcome: no semantic type on any markup row.

    `185 1/4"` on a countertop elevation is almost certainly an overall width, and this route must
    not say so. Semantic typing is gated on reviewed answers (#274, Q20); a note is a string with a
    rectangle and an author until a person says otherwise.
    """
    _extract(session, store)

    rows = _markup_rows(session)

    assert rows, "nothing was recorded, so this proves nothing"
    assert {row.semantic_guess for row in rows} == {None}


def test_a_markup_candidate_is_not_evidence(session: Session, store: LocalStore) -> None:
    """Outcome: nothing is promoted past `RAW_CANDIDATE` by being exact.

    Exact text is not corroborated text. `evidence/gate.py:seal` accepts `CORROBORATED` or
    `HUMAN_CONFIRMED`, and a single reading of a single layer is neither however certain its
    characters are — it reaches a verdict through the confirmation bridge (#530) like everything else.
    """
    _extract(session, store)

    assert all(row.corroboration_status in (None, "RAW_CANDIDATE") for row in _markup_rows(session))


def test_the_two_layers_are_not_reconciled(session: Session, store: LocalStore) -> None:
    """Outcome: both layers' rows exist and nothing compares them.

    Where a vendor's dimension and a reviewer's correction of it disagree, that disagreement is the
    review signal. Recording one and dropping the other, or recording a resolution of the two, would
    destroy the only thing on the sheet worth escalating.
    """
    _extract(session, store)

    assert _markup_rows(session)
    assert not hasattr(ObservationCandidate, "supersedes")
    assert not hasattr(ObservationCandidate, "agrees_with")
