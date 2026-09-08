"""Attaching a reading to the line it annotates, in the pipeline.

Verification for: `DatabaseStages._associate_page` in `workflow/stages.py`, `workflow/association.py`,
`app/evidence/record.py:record_associations` and migration 0039 (#545).

**The geometry here is arithmetic, not a guess.** The PDFs are hand-built, and the appearance stream's
own coordinate system maps into page space by a stated `/BBox`, `/Matrix` and `/Rect` — for these
files, `page = (appearance_x - 50, appearance_y - 450)`. So a line drawn at appearance `y = 500` sits
at page `y = 50`, which on a 300-point page is stored `y = 0.8333`, and a note whose box spans page
`y = 40..60` has its centre exactly there. Every distance in these tests can be checked by hand,
which is the only way a test about proximity means anything.

The two tests to read first are `test_a_reading_between_two_equally_close_lines_is_refused`, because
refusing is the deliverable rather than the fallback, and
`test_a_rotated_note_is_matched_against_the_axis_it_reads_along`, because the alternative to reading
the rotation is attaching a number to a line running the wrong way.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator, Sequence
from decimal import Decimal
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
    ObservationAssociation,
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
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from tests.workflow.test_markup_route import _SilentOcr
from workflow.association import AssociationSettings, dimension_texts
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION, PageResult
from workflow.stages import ASSOCIATION_EXTRACTOR, DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: The five lengths, chosen so the arithmetic in this file is checkable rather than plausible.
#:
#: `proximity_limit` of 0.05 in stored units is fifteen points on a 300-point page, so a note whose
#: centre sits on a line is well inside it and one fifty points away is not.
#:
#: **`ambiguity_margin` is 0.005 because stored coordinates are quantised, and this is where that
#: shows.** Stored space is reached through integer image pixels, so at 150 dpi a 300-point page is
#: 625 pixels tall and no distance is finer than 1/625 ≈ 0.0016. Two lines placed five points either
#: side of a note therefore come out 0.0165 and 0.0171 away — not equal, because they cannot be. A
#: margin of 0.001 is below the resolution of the coordinate system and so cannot express "too close
#: to call" at all; it decided in favour of the line that happened to round nearer. A margin has to
#: be wider than a pixel to mean anything, and this one is three.
SETTINGS = AssociationSettings(
    line_minimum_pt=Decimal(50),
    glyph_maximum_pt=Decimal(10),
    glyph_gap_pt=Decimal(4),
    proximity_limit=Decimal("0.05"),
    ambiguity_margin=Decimal("0.005"),
)

#: One horizontal line at page y=50, running page x=50..150. The note below sits on it.
ONE_LINE = _pdf(
    annotations=[_free_text('185 1/4"', rect=b"[40 40 120 60]"), _stamp(appearance_object=7)],
    extra_objects=[_appearance(b"1 w 100 500 m 200 500 l S\n")],
)

#: Two horizontal lines at page y=75 and y=85, with a note centred at page y=80 — exactly
#: equidistant from both, which is the case `associate` refuses rather than guesses.
#:
#: Both inside the stamp's `/Rect`, whose bottom is page y=50. A first version of this fixture put
#: one line at page y=40 and the reader clipped it away, correctly: an appearance stream may draw
#: past its box and the box is what a viewer clips it to, so a line outside it is not on the sheet
#: anybody saw. The remaining line then attached and the test's premise had quietly disappeared.
TWO_LINES = _pdf(
    annotations=[_free_text('185 1/4"', rect=b"[40 70 120 90]"), _stamp(appearance_object=7)],
    extra_objects=[_appearance(b"1 w 100 525 m 200 525 l S\n1 w 100 535 m 200 535 l S\n")],
)

#: A vertical line at page x=100 and a horizontal one at page y=150, each the same distance from a
#: note at page (100, 150). Only the rotation can decide which one the note reads along.
CROSSED = _pdf(
    annotations=[
        _free_text('102"', rect=b"[90 140 110 160]", rotation=b" /Rotation 270"),
        _stamp(appearance_object=7),
    ],
    extra_objects=[_appearance(b"1 w 150 550 m 150 650 l S\n1 w 100 600 m 200 600 l S\n")],
)

#: A note twenty-five points from the only line on the page: outside a 0.05 stored limit.
FAR_FROM_THE_LINE = _pdf(
    annotations=[_free_text('185 1/4"', rect=b"[40 90 120 110]"), _stamp(appearance_object=7)],
    extra_objects=[_appearance(b"1 w 100 500 m 200 500 l S\n")],
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


def _revision(session: Session, store: LocalStore, *, data: bytes) -> PackageRevision:
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name="association test")
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


def _stages(store: LocalStore, *, association: AssociationSettings | None = SETTINGS):
    return DatabaseStages(
        store=store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        association=association,
    )


def _extract(
    session: Session,
    store: LocalStore,
    *,
    data: bytes = ONE_LINE,
    association: AssociationSettings | None = SETTINGS,
) -> tuple[PackageRevision, Sequence[PageResult]]:
    revision = _revision(session, store, data=data)
    session.commit()
    result = _stages(store, association=association).extract_pages(session, revision.id)
    session.commit()
    return revision, result


def _associations(session: Session) -> list[ObservationAssociation]:
    return list(session.execute(select(ObservationAssociation)).scalars())


def _text_of(session: Session, row: ObservationAssociation) -> str:
    candidate = session.get(ObservationCandidate, row.candidate_id)
    assert candidate is not None
    return candidate.raw_text


# ---------------------------------------------------------------------------
# Attaching
# ---------------------------------------------------------------------------


def test_a_reading_on_a_line_is_attached_to_it(session: Session, store: LocalStore) -> None:
    """Input: a note whose centre sits on the page's only line. Outcome: one attachment.

    **The first association this system has ever recorded.** `associate` has existed since the
    geometry work with nobody to call it, because both its numbers are required and there were no
    real drawings to set them against.
    """
    _extract(session, store)

    rows = _associations(session)
    attached = [row for row in rows if row.refusal_reason is None]

    assert len(attached) == 1
    assert _text_of(session, attached[0]) == '185 1/4"'
    # Page y=50 on a 300-point page: stored 0.8333, and the line runs page x=50..150.
    assert attached[0].start_y == attached[0].end_y
    assert Decimal(attached[0].start_y or "0") == pytest.approx(
        Decimal("0.8333"), abs=Decimal("0.002")
    )


def test_an_attachment_records_why_it_was_made(session: Session, store: LocalStore) -> None:
    """Outcome: non-empty signals, in plain English.

    `TextAssociation` refuses to be built without them: *"an association nobody can audit is
    indistinguishable from a guess"*. The database refuses an attachment without them too.
    """
    _extract(session, store)

    attached = next(row for row in _associations(session) if row.refusal_reason is None)

    assert attached.signals
    assert all(isinstance(signal, str) and signal.strip() for signal in attached.signals)


def test_the_coordinates_are_stored_exactly(session: Session, store: LocalStore) -> None:
    """Outcome: text, not floats. These numbers decided which line the reading belongs to.

    The same choice `pages.media_box` makes, for the same reason: a JSON float would carry binary
    rounding into a coordinate a reviewer is later shown as evidence.
    """
    attached = None
    _extract(session, store)
    attached = next(row for row in _associations(session) if row.refusal_reason is None)

    for value in (attached.start_x, attached.start_y, attached.end_x, attached.end_y):
        assert isinstance(value, str)
        Decimal(value)  # raises if it is not an exact decimal string


def test_a_rotated_note_is_matched_against_the_axis_it_reads_along(
    session: Session, store: LocalStore
) -> None:
    """**Input: a note at `/Rotation 270`, equidistant from a vertical and a horizontal line.**

    Only the rotation can decide. `associate` filters candidates by whether a line runs the way the
    text does, so a note read as upright would be attached to the horizontal line — a wrong
    association, which is the failure `text_association` exists to prevent, and one nothing
    downstream could detect because the row would be perfectly well-formed.

    The rotation comes from the annotation's own `/Rotation`, never inferred from the box: a box
    around `102"` is taller than it is wide, and so is a box around an unrotated `2"`.
    """
    _extract(session, store, data=CROSSED)

    rows = _associations(session)
    attached = [row for row in rows if row.refusal_reason is None]

    assert len(attached) == 1, [row.refusal_reason for row in rows]
    # The vertical line: page x=100, running page y=100..200. Constant x, varying y.
    assert attached[0].start_x == attached[0].end_x
    assert attached[0].start_y != attached[0].end_y


# ---------------------------------------------------------------------------
# Refusing, which is the deliverable
# ---------------------------------------------------------------------------


def test_a_reading_between_two_equally_close_lines_is_refused(
    session: Session, store: LocalStore
) -> None:
    """**Input: a note exactly midway between two parallel lines. Outcome: a refusal, recorded.**

    Two lines equally close to one number is the ordinary case on a dimensioned elevation, not an
    edge case, and the answer is *no* association rather than the nearest guess. Recorded as a row
    because an unattached number is the list a reviewer has to look at — dropping it would turn "we
    could not tell" into silence.
    """
    _extract(session, store, data=TWO_LINES)

    refused = [row for row in _associations(session) if row.refusal_reason is not None]

    assert len(refused) == 1
    assert refused[0].refusal_reason
    assert refused[0].signals == []
    assert refused[0].start_x is None


def test_a_refusal_records_what_the_choice_was_between(session: Session, store: LocalStore) -> None:
    """Outcome: the candidate lines come with the refusal.

    A reviewer told only that an association could not be made cannot check the geometry. Shown the
    candidates, they can see whether the drawing is ambiguous or the reader is.
    """
    _extract(session, store, data=TWO_LINES)

    refused = next(row for row in _associations(session) if row.refusal_reason is not None)

    assert refused.candidate_lines
    assert len(refused.candidate_lines) >= 2
    assert all(len(line) == 4 for line in refused.candidate_lines)


def test_a_reading_with_no_line_near_it_is_refused_and_says_so(
    session: Session, store: LocalStore
) -> None:
    """Input: a note fifty points from the only line, past a 0.05 stored limit. Outcome: refusal.

    Either it is not a dimension label — most notes on a real sheet are titles and tag callouts — or
    it labels a line nobody detected. Both are for a reviewer to see.
    """
    _extract(session, store, data=FAR_FROM_THE_LINE)

    refused = [row for row in _associations(session) if row.refusal_reason is not None]

    assert len(refused) == 1
    assert "is within" in (refused[0].refusal_reason or "")


def test_every_reading_gets_exactly_one_decision(session: Session, store: LocalStore) -> None:
    """Outcome: as many association rows as there were readings, attached or refused.

    `AssociationResult` refuses to be built if a reading went missing from both halves, and this is
    the same guarantee one layer down: a reading with no decision recorded would be a number nobody
    ever looked at, indistinguishable from one that was looked at and matched nothing.
    """
    _extract(session, store, data=TWO_LINES)

    candidates = list(session.execute(select(ObservationCandidate)).scalars())
    rows = _associations(session)

    assert len(rows) == len(candidates)
    assert {row.candidate_id for row in rows} == {candidate.id for candidate in candidates}


# ---------------------------------------------------------------------------
# Not running is a different fact from finding nothing
# ---------------------------------------------------------------------------


def test_without_settings_the_step_does_not_run(session: Session, store: LocalStore) -> None:
    """**Input: no thresholds. Outcome: no rows, no run, and `associations: None` in the payload.**

    `None` rather than zero, because the two are different facts: zero means the step ran and had
    nothing to decide, `None` means nobody asked it to. A pipeline that reported them the same way
    would let an unconfigured deployment read as a drawing with no readings on it.
    """
    _, result = _extract(session, store, association=None)

    assert _associations(session) == []
    assert (
        session.execute(
            select(ExtractionRun).where(ExtractionRun.extractor == ASSOCIATION_EXTRACTOR)
        ).all()
        == []
    )
    assert [page.payload["associations"] for page in result] == [None]


def test_with_settings_the_page_reports_how_many_decisions_it_made(
    session: Session, store: LocalStore
) -> None:
    """Outcome: a count in the payload, so a caller can see the step ran."""
    _, result = _extract(session, store)

    assert [page.payload["associations"] for page in result] == [1]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


def test_the_association_run_carries_its_thresholds(session: Session, store: LocalStore) -> None:
    """**Outcome: every one of the five lengths is in the run's `config_hash`.**

    A run is keyed on its configuration, so this is what makes a re-association under a different
    proximity limit a different run. Without it the second pass would reuse the first run and its
    rows would record numbers that did not produce them — the provenance bug #487 fixed for dpi,
    which matters more here because these numbers decide which line a dimension belongs to.
    """
    _extract(session, store)

    run = session.execute(
        select(ExtractionRun).where(ExtractionRun.extractor == ASSOCIATION_EXTRACTOR)
    ).scalar_one()

    for value in ("50", "10", "4", "0.05", "0.005"):
        assert value in run.config_hash, run.config_hash
    assert "dpi=150" in run.config_hash


def test_the_association_run_is_its_own(session: Session, store: LocalStore) -> None:
    """Outcome: a third run beside the reading routes, not a reuse of one.

    An association is not a reading, and the thresholds that produced it are not the thresholds that
    produced the reading. Sharing a run would put both in one `config_hash` or leave one out.
    """
    _extract(session, store)

    extractors = {run.extractor for run in session.execute(select(ExtractionRun)).scalars()}

    assert ASSOCIATION_EXTRACTOR in extractors
    assert len(extractors) >= 2


def test_running_the_stage_twice_records_one_decision_per_reading(
    session: Session, store: LocalStore
) -> None:
    """Outcome: a redelivery does not double the rows.

    A killed worker reclaims its task run and reuses its extraction run. Two answers for one reading
    would be two associations nothing downstream could choose between — and the unique constraint on
    `(candidate_id, extraction_run_id)` refuses it at the database as well as here.
    """
    revision, _ = _extract(session, store)
    first = len(_associations(session))

    _stages(store).extract_pages(session, revision.id)
    session.commit()

    assert len(_associations(session)) == first


# ---------------------------------------------------------------------------
# The settings themselves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "line_minimum_pt",
        "glyph_maximum_pt",
        "glyph_gap_pt",
        "proximity_limit",
        "ambiguity_margin",
    ],
)
def test_a_float_length_is_refused(name: str) -> None:
    """Input: a float. Outcome: `TypeError`.

    A float would make which line a number belongs to depend on binary rounding, and the wrong
    answer would look exactly like the right one.
    """
    values = {
        "line_minimum_pt": Decimal(50),
        "glyph_maximum_pt": Decimal(10),
        "glyph_gap_pt": Decimal(4),
        "proximity_limit": Decimal("0.05"),
        "ambiguity_margin": Decimal("0.005"),
    }
    values[name] = 0.05  # type: ignore[assignment]

    with pytest.raises(TypeError, match="never a float"):
        AssociationSettings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [Decimal(0), Decimal(-1)])
def test_a_length_that_admits_nothing_is_refused(value: Decimal) -> None:
    """Input: zero or negative. Outcome: `ValueError` rather than a step that decides nothing."""
    with pytest.raises(ValueError, match="greater than zero"):
        AssociationSettings(
            line_minimum_pt=value,
            glyph_maximum_pt=Decimal(10),
            glyph_gap_pt=Decimal(4),
            proximity_limit=Decimal("0.05"),
            ambiguity_margin=Decimal("0.005"),
        )


def test_pairing_readings_with_the_wrong_number_of_rows_raises() -> None:
    """**Input: three readings and two rows. Outcome: `ValueError`, not a silent misalignment.**

    Pairing is by position — both candidate writers return what they wrote in the order they wrote
    it — and that coupling is real. A mismatch would attach every reading to the line belonging to a
    different one, and every row would look perfectly well-formed.
    """
    with pytest.raises(ValueError, match="cannot be paired"):
        dimension_texts([object(), object(), object()], [uuid4(), uuid4()])  # type: ignore[list-item]


def _attach_second(
    session: Session, store: LocalStore, revision: PackageRevision, *, data: bytes
) -> None:
    """A second single-page document on the same revision, so one run covers two pages."""
    digest = hashlib.sha256(data).hexdigest()
    package_id = session.execute(
        select(PackageRevision.package_id).where(PackageRevision.id == revision.id)
    ).scalar_one()
    document = Document(package_id=package_id, kind="shop")
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
            package_id=package_id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    session.flush()
    store.put(key, io.BytesIO(data), content_type="application/pdf")


def test_every_page_gets_its_own_decisions(session: Session, store: LocalStore) -> None:
    """**The bug a single-page test cannot see.** Input: two pages in one stage run.

    One extraction run covers every page of every document in a stage execution. The first version
    of `record_associations` guarded idempotency on the run alone, which answers "has anything been
    associated yet" rather than "has *this* reading been" — so on a real seventeen-page set, page
    one's four decisions were written and every later page was handed those four back instead of
    recording its own. Found by running it over a real document, and this is the shape of it.
    """
    revision = _revision(session, store, data=ONE_LINE)
    _attach_second(session, store, revision, data=TWO_LINES)
    session.commit()
    _stages(store).extract_pages(session, revision.id)
    session.commit()

    rows = _associations(session)
    candidates = list(session.execute(select(ObservationCandidate)).scalars())

    # One reading per page, each with its own decision: the first attached, the second refused.
    assert len(rows) == len(candidates) == 2
    assert {row.refusal_reason is None for row in rows} == {True, False}
    assert len({row.candidate_id for row in rows}) == 2
