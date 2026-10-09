"""The one-time re-check of the "shows GV markup" flag on crops cut before #1078 (#1141).

"Before #1078" is reproduced, not imagined: the crops are cut by the real crop stage with the check
it used to ask (`crop_shows_a_gv_mark` alone, GV's marks baked into the vendor's drawing), on the
invented sheet of `tests/workflow/test_evidence_crop_gv_marks.py`, where a solid red reviewer note
covers one vendor label and a second label sits well away from it. Then the job runs with the
corrected check, as it will on a real database.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from uuid import UUID

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

import workflow.stages as stages_module
from app.db.session import session_factory
from app.evidence.crop_marks import current_crop_marks
from app.models import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceMarkRecheck,
    Finding,
    ObservationCandidate,
    OutboxEntry,
    PackageRevision,
)
from app.models.evidence import EvidenceSupportingCandidate
from extraction.annotations import MarkupNote
from storage.local import LocalStore
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_evidence_crop_gv_marks import CLEAN, REVIEWED
from tests.workflow.test_pipeline_end_to_end import _revision, _upgrade, store
from workflow.crop_mark_rechecks import (
    CROP_MARK_CHECK_VERSION,
    CropMarkRecheck,
    recheck_crop_marks,
)
from workflow.stages import ColouredMarkup, DatabaseStages, crop_shows_a_gv_mark

__all__ = ["store"]  # the fixture, re-exported so pytest finds it here

pytest_plugins = ("tests.app.postgres_fixture",)

RUN_BY = "admin@example.com"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _old_check(
    crop_box: tuple[int, int, int, int],
    markup: ColouredMarkup | None,
    reviewer_layer: Sequence[MarkupNote] | None,
) -> bool | None:
    """The question the crop stage asked before #1078: GV's marks in the vendor's drawing only."""
    del reviewer_layer
    return None if markup is None else crop_shows_a_gv_mark(crop_box, markup)


def _cut(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    sheet: bytes,
    *,
    before_1078: bool = True,
) -> PackageRevision:
    revision = _revision(session, store, data=sheet)
    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    stages.extract_pages(session, revision.id)
    with monkeypatch.context() as patched:
        if before_1078:
            patched.setattr(stages_module, "crop_mark_state", _old_check)
        stages.validate_evidence(session, revision.id)
    session.commit()
    return revision


def _run(
    session: Session, store: LocalStore, *, dry_run: bool = False, dpi: int | None = None
) -> CropMarkRecheck:
    stages = (
        DatabaseStages(store, missing_space=MISSING_SPACE)
        if dpi is None
        else DatabaseStages(store, missing_space=MISSING_SPACE, dpi=dpi)
    )
    outcome = recheck_crop_marks(session, stages, run_by=RUN_BY, dry_run=dry_run)
    session.commit()
    return outcome


def _crops(session: Session) -> dict[str, EvidenceArtifact]:
    """Each reading's crop, by the reading's text."""
    rows = session.execute(
        select(ObservationCandidate.raw_text, EvidenceArtifact)
        .join(EvidenceArtifact, EvidenceArtifact.candidate_id == ObservationCandidate.id)
        .where(EvidenceArtifact.kind == "crop")
    ).all()
    return {raw_text: artifact for raw_text, artifact in rows}


def _shown(session: Session) -> dict[str, bool | None]:
    """Each reading's crop flag as a reader is told it."""
    crops = _crops(session)
    marks = current_crop_marks(session, {crop.id: crop.shows_gv_marks for crop in crops.values()})
    return {raw_text: marks[crop.id] for raw_text, crop in crops.items()}


def _covered(flags: dict[str, bool | None]) -> list[str]:
    covered = [raw_text for raw_text in flags if raw_text.startswith("648")]
    assert covered, f"the covered label was not read, so this test proves nothing: {flags}"
    return covered


def _rechecks(session: Session) -> list[EvidenceMarkRecheck]:
    return list(session.scalars(select(EvidenceMarkRecheck)))


def _snapshot(session: Session) -> dict[str, object]:
    """Every row the job must not touch: counts, and each crop exactly as stored."""
    counts = {
        model.__name__: int(session.scalar(select(func.count()).select_from(model)) or 0)
        for model in (
            ObservationCandidate,
            CanonicalObservation,
            EvidenceArtifact,
            EvidenceSupportingCandidate,
            Finding,
            OutboxEntry,
        )
    }
    crops = sorted(
        (str(a.id), a.storage_key, a.sha256, a.shows_gv_marks, a.created_at.isoformat())
        for a in session.scalars(select(EvidenceArtifact))
    )
    return {"counts": counts, "crops": crops}


# ---------------------------------------------------------------------------
# The old crop is corrected; the right one is left alone
# ---------------------------------------------------------------------------


def test_an_old_crop_under_a_gv_note_is_corrected(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, REVIEWED)
    before = _shown(session)
    covered = _covered(before)
    assert {before[text] for text in covered} == {False}, "the old flag was not reproduced"

    outcome = _run(session, store)

    after = _shown(session)
    assert {after[text] for text in covered} == {True}
    rows = _rechecks(session)
    assert outcome.changed == len(rows) > 0
    assert {row.shows_gv_marks for row in rows} == {True}
    assert {row.check_version for row in rows} == {CROP_MARK_CHECK_VERSION}
    assert {row.run_by for row in rows} == {RUN_BY}
    assert {row.run_id for row in rows} == {outcome.run_id}
    assert outcome.changes == {"False -> True": outcome.changed}
    # The crop rows themselves still say what they said: the correction is a row of its own.
    assert {_crops(session)[text].shows_gv_marks for text in covered} == {False}


def test_a_crop_whose_flag_was_already_right_gets_no_row(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The far label: the old answer "no GV markup" was right, and stays the stored one."""
    _cut(session, store, monkeypatch, REVIEWED)

    _run(session, store)

    far = {text: crop for text, crop in _crops(session).items() if text.startswith("100")}
    assert far, "the far label was not read, so this test proves nothing"
    rechecked = {row.crop_artifact_id for row in _rechecks(session)}
    assert not rechecked & {crop.id for crop in far.values()}
    assert {_shown(session)[text] for text in far} == {False}


def test_a_sheet_with_no_gv_note_is_left_entirely_alone(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, CLEAN)

    outcome = _run(session, store)

    assert outcome.crops > 0, "no crop was cut, so this test proves nothing"
    assert outcome.rechecked == outcome.unchanged == outcome.crops
    assert outcome.changed == 0
    assert _rechecks(session) == []


def test_crops_cut_after_1078_are_already_right(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """New crops carry the corrected answer, so the job finds nothing to write for them."""
    _cut(session, store, monkeypatch, REVIEWED, before_1078=False)

    outcome = _run(session, store)

    assert outcome.rechecked == outcome.crops > 0
    assert outcome.changed == 0
    assert _rechecks(session) == []


# ---------------------------------------------------------------------------
# Idempotent, dry run, and nothing else touched
# ---------------------------------------------------------------------------


def test_running_it_again_changes_nothing(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, REVIEWED)
    first = _run(session, store)
    rows = {(row.id, row.crop_artifact_id, row.shows_gv_marks) for row in _rechecks(session)}
    shown = _shown(session)

    second = _run(session, store)

    assert first.changed > 0
    assert second.changed == 0
    assert second.already_rechecked == first.changed
    assert {
        (row.id, row.crop_artifact_id, row.shows_gv_marks) for row in _rechecks(session)
    } == rows
    assert _shown(session) == shown


def test_a_dry_run_counts_what_a_run_would_change_and_writes_nothing(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, REVIEWED)
    shown = _shown(session)

    dry = _run(session, store, dry_run=True)

    assert dry.dry_run is True
    assert dry.changed > 0
    assert _rechecks(session) == []
    assert _shown(session) == shown

    real = _run(session, store)
    assert (real.changed, real.changes, real.rechecked) == (dry.changed, dry.changes, dry.rechecked)


def test_no_crop_reading_finding_or_other_evidence_changes(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, REVIEWED)
    before = _snapshot(session)

    _run(session, store)

    assert _snapshot(session) == before


def test_the_rechecks_are_append_only(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _cut(session, store, monkeypatch, REVIEWED)
    _run(session, store)
    assert _rechecks(session), "nothing was written, so this test proves nothing"

    for statement in (
        "UPDATE evidence_mark_rechecks SET shows_gv_marks = false",
        "DELETE FROM evidence_mark_rechecks",
    ):
        with pytest.raises(DBAPIError, match="append-only"):
            session.execute(text(statement))
        session.rollback()


# ---------------------------------------------------------------------------
# Safe sides
# ---------------------------------------------------------------------------


def test_a_crop_already_marked_is_never_lowered(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Whatever a re-check says, a crop shown as "shows GV markup" keeps it: the flag only offers
    the vendor-only view, and taking that away is the unsafe direction."""
    _cut(session, store, monkeypatch, REVIEWED, before_1078=False)
    marked = [text for text, flag in _shown(session).items() if flag is True]
    assert marked, "no crop was marked, so this test proves nothing"

    monkeypatch.setattr(stages_module, "crop_mark_state", lambda *_: False)
    outcome = _run(session, store)

    assert outcome.kept_marked == len(marked)
    assert outcome.changed == 0
    assert _rechecks(session) == []


def test_a_crop_whose_pixels_do_not_come_back_identical_is_left_alone(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stages at another dpi cut other pixels: nothing says they are the stored crop's, so the
    job does not answer for that crop."""
    _cut(session, store, monkeypatch, REVIEWED)
    default_dpi = DatabaseStages(store, missing_space=MISSING_SPACE)._dpi

    outcome = _run(session, store, dpi=default_dpi * 2)

    assert outcome.crops > 0
    assert outcome.not_same_pixels == outcome.crops
    assert outcome.rechecked == outcome.changed == 0
    assert _rechecks(session) == []


def test_it_refuses_to_run_without_a_name_or_a_store(session: Session, store: LocalStore) -> None:
    with pytest.raises(ValueError, match="run_by"):
        recheck_crop_marks(
            session, DatabaseStages(store, missing_space=MISSING_SPACE), run_by=" ", dry_run=True
        )
    with pytest.raises(ValueError, match="artifact store"):
        recheck_crop_marks(
            session, DatabaseStages(missing_space=MISSING_SPACE), run_by=RUN_BY, dry_run=True
        )


def test_one_revision_can_be_rechecked_alone(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _cut(session, store, monkeypatch, REVIEWED)
    _cut(session, store, monkeypatch, REVIEWED)

    stages = DatabaseStages(store, missing_space=MISSING_SPACE)
    outcome = recheck_crop_marks(
        session, stages, run_by=RUN_BY, dry_run=False, package_revision_id=first.id
    )
    session.commit()

    everything = _run(session, store, dry_run=True)
    assert outcome.changed > 0
    # The other revision's crops are still waiting; this one's are done.
    assert everything.already_rechecked == outcome.changed
    assert everything.changed == outcome.changed
    assert all(isinstance(row.crop_artifact_id, UUID) for row in _rechecks(session))
