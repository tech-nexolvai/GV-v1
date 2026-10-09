"""The architect check on the architect reader's own output (#1052 + #1054), with a fake pairing.

The invented combined sheet of `tests/extraction/architect/combined_sheet.py` is read by the real
architect reader (`GV_ARCHITECT_READER_ENABLED` on): it confirms the two drawings' roles by code and
stores the architect's `3' - 4"` (40"), `2' - 2"` (26"), the centre-line `1' - 5"` and the held
`2' - 7"`. A vendor countertop row is then stored on the same sheet as the slot reader stores it, and
the pairing (#1053, built in parallel) is a fake. No client value appears here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import MeasurementProposal, ObservationCandidate, PackageRevision, Page
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.api.test_slot_rows import _reader_support
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.combined_sheet import combined_sheet
from tests.workflow.test_architect_reader import _architect_rows, _by_text, _extract, _upload
from tests.workflow.test_architect_row_evidence import ARCH_RULE, _overall, _pairing, _piece
from tests.workflow.test_stages import _publish_rulebook
from workflow.architect_pairing_contract import EffectivePairing
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

#: Inside the vendor's drawing (`VENDOR_RECT`), in the page's 150 dpi image.
VENDOR_BOX = [[300, 1000], [400, 1000], [400, 1030], [300, 1030]]


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


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


def _vendor_row(
    session: Session, revision: PackageRevision, *, pieces: tuple[int, ...], overall: int
) -> UUID:
    """A sealed vendor countertop row on the sheet, as the slot reader stores one."""
    page = session.scalars(select(Page)).one()
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id,
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-test",
        config_hash="synthetic-test",
        dpi=150,
    )
    session.add(run)
    session.flush()
    anchor: UUID | None = None
    slots = [
        *((str(index), "SHOP:countertop_piece_width", value) for index, value in enumerate(pieces)),
        ("overall", "SHOP:countertop_overall_width", overall),
    ]
    for slot, field, value in slots:
        candidate = ObservationCandidate(
            document_version_id=page.document_version_id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text=f"{value} inch",
            value_numerator=value,
            value_denominator=1,
            unit="in",
            polygon=VENDOR_BOX,
            coordinate_space="image",
            ambiguity_flags=[
                "slot-reader",
                f"slot:{slot}",
                "row-rank:1",
                f"row-slot-count:{len(pieces)}",
                "ink:vendor",
            ],
            corroboration_status="CORROBORATED",
            corroboration_lane="SECOND_READER",
        )
        session.add(candidate)
        session.flush()
        _reader_support(session, candidate)
        session.add(
            MeasurementProposal(
                package_revision_id=revision.id,
                page_number=1,
                proposal_id=uuid4(),
                field_key=field,
                position=0 if slot == "overall" else int(slot),
                candidate_id=candidate.id,
                placement_verified=True,
                model_id="synthetic-reader-pair",
                prompt_id="synthetic-slot-test",
            )
        )
        if slot == "0":
            anchor = candidate.id
    session.add(
        ObservationCandidate(
            document_version_id=page.document_version_id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text="walls: back_only",
            polygon=VENDOR_BOX,
            coordinate_space="image",
            ambiguity_flags=[
                "wall-reader",
                "row-rank:1",
                "walls-sealed:back_only",
                "wall-source:vendor-drawing-clues",
            ],
        )
    )
    session.flush()
    assert anchor is not None
    return anchor


def _check(
    session: Session,
    store: LocalStore,
    revision: PackageRevision,
    anchor: UUID,
    pairing: EffectivePairing,
) -> tuple[str, str | None]:
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding

    DatabaseStages(
        store, architect_pairing=lambda _s, row: pairing if row == anchor else None
    ).run_checks(session, revision.id)
    finding = session.execute(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == ARCH_RULE,
        )
    ).scalar_one()
    return finding.outcome, finding.reason


def _sheet(session: Session, store: LocalStore) -> PackageRevision:
    revision = _upload(session, store, combined_sheet())
    _extract(session, store, revision)
    _publish_rulebook(session)
    return revision


def test_the_architects_own_text_matches_an_equal_vendor_overall(
    session: Session, store: LocalStore
) -> None:
    revision = _sheet(session, store)
    cabinet = _by_text(_architect_rows(session))["3' - 4\""]
    anchor = _vendor_row(session, revision, pieces=(38,), overall=40)

    outcome, reason = _check(session, store, revision, anchor, _pairing(_overall(cabinet)))

    assert outcome == "PASS", reason


def test_the_architects_own_text_flags_a_vendor_overall_an_inch_off(
    session: Session, store: LocalStore
) -> None:
    revision = _sheet(session, store)
    cabinet = _by_text(_architect_rows(session))["3' - 4\""]
    anchor = _vendor_row(session, revision, pieces=(39,), overall=41)

    outcome, _reason = _check(session, store, revision, anchor, _pairing(_overall(cabinet)))

    assert outcome == "FAIL"


@pytest.mark.parametrize(("label", "why"), [("1' - 5\"", "casework"), ("2' - 7\"", "held")])
def test_a_centre_line_or_held_architect_span_is_never_compared(
    session: Session, store: LocalStore, label: str, why: str
) -> None:
    revision = _sheet(session, store)
    span = _by_text(_architect_rows(session))[label]
    anchor = _vendor_row(session, revision, pieces=(17,), overall=17)

    outcome, reason = _check(session, store, revision, anchor, _pairing(_piece(span, 0)))

    assert outcome == "REVIEW_REQUIRED"
    assert why in (reason or "")
