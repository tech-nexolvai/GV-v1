"""Type 1 with the architect's drawings as their own file, end to end on made-up drawings (#1167).

A two-file package: the vendor's shop sheet (no architect drawing on it) and the architect's own
PDF (`tests/extraction/architect/architect_sheet.py`: a bubble, a title, a scale note and a cabinet
outline whose two widths, `3' - 4"` and `2' - 2"`, are printed as real text on ticks that sit on the
outline). The real architect reader reads the architect file (Phase 1); the vendor's countertop row
is stored as the slot reader stores it; the row's match with a view of the architect file is a fake
matcher returning #1166's contract values; both AIs are a fake `ask` keyed `(key, model)`. Then the
real pairing step (`ArchitectPairing`), the real records and the real check stage decide.

Proved here:

* (a) code and both AIs matched the view → paired against that view only → exact PASS when the
  widths are equal, FAIL when one differs;
* (b) twin views → the reviewer chooses (nothing paired, the row asks) → the reviewer picks →
  code's pairing for that view → the checks run again → compared, and only with that view;
* (c) "none of these" → not compared, said plainly;
* the read and verdict guards never let an architect value from another view count;
* (d) a combined sheet never reaches the matcher, is asked `arch-pair-v3` and decides as before.

No model is called. Every value is invented; no client value appears here.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    DrawingView,
    MeasurementProposal,
    ObservationCandidate,
    PackageRevision,
    Page,
)
from app.models.evidence import (
    ArchitectPairingRecord,
    ArchitectViewIndexEntry,
    ArchitectViewMatchRecord,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from evidence.crop import RenderedPage, encode_png
from extraction.ink import InkClass
from extraction.slot_reader.bedrock import (
    ARCH_PAIR_2PANEL_PROMPT_ID,
    ARCH_PAIR_PROMPT_ID,
    ArchPairAnswer,
    CropJob,
    job_prompt_id,
)
from extraction.slot_reader.seal import LabelState
from storage.local import LocalStore
from tests.api.test_slot_rows import _reader_support
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.architect_sheet import architect_sheet
from tests.extraction.architect.combined_sheet import combined_sheet
from tests.workflow.test_architect_file_reader import _add_architectural
from tests.workflow.test_architect_reader import _stages, _upload
from tests.workflow.test_stages import _publish_rulebook
from units.measurement import Measurement, Unit
from verdict.outcomes import Outcome
from workflow.architect_match_contract import (
    EffectiveMatch,
    MatchedView,
    RowMatch,
    compared_with_text,
    restrict_to_view,
)
from workflow.architect_pairing import (
    MEASURED_PAIRING_SETTINGS,
    ArchitectPageInput,
    ArchitectPairing,
    ReviewerPairingRefused,
    pair_by_code,
    persist_architect_pairings,
    record_code_pairing_for_view,
    record_reviewer_pairing,
    vendor_row_input,
)
from workflow.architect_pairing_records import DecidedPair, latest_architect_pairing
from workflow.architect_row_evidence import architect_candidate_refusal
from workflow.architect_row_plan import (
    ARCHITECT_CHECK_RULE_ID,
    CHOOSE_ARCHITECT_VIEW,
    NO_ARCHITECT_VIEW_MATCHES,
    NOTHING_PAIRED_ON_REVISION,
    SEPARATE_ARCHITECT_FILE_NOT_COMPARED,
    Disposition,
    architect_file_indexed,
    plan_architect_row,
)
from workflow.slot_reader import PageSlotResult, SlotPage
from workflow.slot_row_scope import slot_rows_and_unchosen_pages
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

OPUS = "anthropic.claude-opus-5-5"
SONNET = "anthropic.claude-sonnet-5-5"
FILE_NAME = "synthetic-architect.pdf"
#: The vendor's drawing: 2 page points per real inch, the row starting at x = 100.
VENDOR_PT = 2
VENDOR_START = 100
#: Inside the vendor's sheet, in its 150 dpi image (only stored with each reading).
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


# --- the package -----------------------------------------------------------------------------------


@dataclass
class Package:
    """A two-file package read by the real architect reader, with its views indexed."""

    revision: PackageRevision
    vendor_page: Page
    architect_page: Page
    pages: Mapping[UUID, ArchitectPageInput]
    views: dict[int, MatchedView] = field(default_factory=dict)


def _two_file_package(session: Session, store: LocalStore, *, views: int = 1) -> Package:
    revision = _upload(session, store, architect_sheet(vendor=True))
    architect = _add_architectural(session, store, revision, architect_sheet(views=views))
    stages = _stages(store)
    stages.extract_pages(session, revision.id)
    session.commit()
    _publish_rulebook(session)
    vendor_page = session.scalars(
        select(Page).where(Page.document_version_id != architect.id)
    ).one()
    architect_page = session.scalars(
        select(Page).where(Page.document_version_id == architect.id)
    ).one()
    package = Package(revision, vendor_page, architect_page, dict(stages._architect_pages))
    package.views = _index(session, package)
    session.commit()
    return package


def _index(session: Session, package: Package) -> dict[int, MatchedView]:
    """The architect file's view index (#1166), as its stage would write it: one row per view of
    the reader's run; an existing row (once #1166's stage writes them) is used as it is."""
    page = package.architect_page
    run_id = session.scalars(
        select(ObservationCandidate.extraction_run_id).where(
            ObservationCandidate.page_id == page.id
        )
    ).first()
    assert run_id is not None, "the architect reader stored the architect's values"
    found: dict[int, MatchedView] = {}
    sheet = package.pages[page.id]
    for drawing in session.scalars(
        select(DrawingView).where(DrawingView.page_id == page.id).order_by(DrawingView.tag)
    ):
        number = int(drawing.tag.removeprefix("view-"))
        entry = session.scalars(
            select(ArchitectViewIndexEntry).where(
                ArchitectViewIndexEntry.page_id == page.id,
                ArchitectViewIndexEntry.view_number == number,
            )
        ).first()
        if entry is None:
            box = sheet.view_extents[number]
            entry = ArchitectViewIndexEntry(
                extraction_run_id=run_id,
                document_version_id=page.document_version_id,
                page_id=page.id,
                view_number=number,
                view_tag=drawing.tag,
                drawing_view_id=drawing.id,
                sheet_number="X-101",
                bubble=None,
                title="SYNTHETIC ELEVATION",
                scale_note='1/4" = 1\'-0"',
                points_per_inch="1.5",
                extent={
                    "x0": str(box.x0),
                    "top": str(box.top),
                    "x1": str(box.x1),
                    "bottom": str(box.bottom),
                },
                separated=True,
                role_confirmed=True,
                row_count=sum(row.view_annotation_index == number for row in sheet.rows),
                reason="test index",
            )
            session.add(entry)
            session.flush()
        found[number] = MatchedView(
            view_id=entry.id,
            document_version_id=page.document_version_id,
            page_id=page.id,
            page_number=page.index + 1,
            view_number=number,
            view_tag=entry.view_tag,
            title=entry.title,
            bubble=entry.bubble,
            sheet_number=entry.sheet_number,
            scale_note=entry.scale_note,
            file_name=FILE_NAME,
            separated=entry.separated,
        )
    return found


# --- the vendor's row, as the slot reader stores and pairs it ---------------------------------------


def _vendor_row(
    session: Session, revision: PackageRevision, page: Page, printed: Sequence[int]
) -> UUID:
    """A sealed vendor countertop row, stored as the slot reader stores one; its anchor."""
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
        *(
            (str(index), "SHOP:countertop_piece_width", value)
            for index, value in enumerate(printed)
        ),
        ("overall", "SHOP:countertop_overall_width", sum(printed)),
    ]
    for slot, field_key, value in slots:
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
                f"row-slot-count:{len(printed)}",
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
                field_key=field_key,
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


def _slot_result(
    page: Page, anchor: UUID, drawn: Sequence[int], printed: Sequence[int]
) -> PageSlotResult:
    """The slot reader's result for that row: pieces drawn `drawn` inches wide at 2 pt per inch,
    each sealed at its `printed` value in the vendor's ink."""
    owners: list[Any] = []
    x = VENDOR_START
    for index, (width, value) in enumerate(zip(drawn, printed, strict=True)):
        x0, x1 = x, x + width * VENDOR_PT
        owners.append(
            SimpleNamespace(
                owner=SimpleNamespace(index=index, x0=Decimal(x0), x1=Decimal(x1)),
                outcome=SimpleNamespace(
                    state=LabelState.SEALED,
                    value=Measurement(Fraction(value), Unit.INCH, None),
                    label_index=0,
                ),
                labels=(SimpleNamespace(outcome=SimpleNamespace(ink=InkClass.VENDOR)),),
                band_px=(x0, 300, x1, 320),
            )
        )
        x = x1
    plan = SimpleNamespace(
        row=object(),
        slots=tuple(owner.owner for owner in owners),
        overall=SimpleNamespace(index=None, x0=owners[0].owner.x0, x1=owners[-1].owner.x1),
    )
    return PageSlotResult(
        page_index=0,
        page_id=page.id,
        document_version_id=page.document_version_id,
        plan=plan,  # type: ignore[arg-type]
        slots=tuple(owners),
        overall=None,
        mapping=None,  # type: ignore[arg-type]
        owner_candidate_ids={"slot:0": anchor},
    )


def _slot_page(page: Page) -> SlotPage:
    width, height = 600, 400
    rendered = RenderedPage(
        document_version_id=page.document_version_id,
        page_index=0,
        page_content_hash="1" * 64,
        rotation=0,
        render_failed=False,
        width_px=width,
        height_px=height,
        dpi=72,
        rgb_bytes=bytes([255]) * (width * height * 3),
    )
    return SlotPage(
        page_index=0,
        page_id=page.id,
        document_version_id=page.document_version_id,
        rendered=rendered,
        rows=SimpleNamespace(to_pixels=lambda x, top: (int(x), int(top))),  # type: ignore[arg-type]
        ink=None,
    )


@dataclass(frozen=True)
class ViewPicture:
    """The view index's picture of one view (#1166): a plain stand-in, framed on the view."""

    png: bytes
    box_px: tuple[int, int, int, int]
    dpi: int


def _pictures(package: Package) -> dict[UUID, ViewPicture]:
    found: dict[UUID, ViewPicture] = {}
    for number, view in package.views.items():
        box = package.pages[package.architect_page.id].view_extents[number]
        pixels = (
            int(box.x0 * Decimal(150) / 72),
            int(box.top * Decimal(150) / 72),
            int(box.x1 * Decimal(150) / 72),
            int(box.bottom * Decimal(150) / 72),
        )
        width, height = pixels[2] - pixels[0], pixels[3] - pixels[1]
        assert width > 0 and height > 0
        found[view.view_id] = ViewPicture(
            encode_png(width, height, bytes([255]) * (width * height * 3)), pixels, 150
        )
    return found


# --- the fakes: #1166's matcher and both AIs ---------------------------------------------------------


@dataclass
class FakeMatcher:
    """Returns one contract `RowMatch` per row it is given, and remembers which rows it saw."""

    status: str
    chosen: MatchedView | None
    candidates: tuple[Mapping[str, object], ...] = ()
    seen: list[UUID] = field(default_factory=list)

    def match(
        self, results: Sequence[PageSlotResult], pages: Sequence[SlotPage], **_: object
    ) -> dict[int, RowMatch]:
        self.seen.extend(result.page_id for result in results)
        return {
            result.page_index: RowMatch(
                status=self.status,  # type: ignore[arg-type]
                source="carried" if self.status == "carried_over" else "automatic",
                chosen=self.chosen,
                code=SimpleNamespace(verdict="geometry_clear", pick=None, ranked=(), reasons=()),
                ai_picks=(),
                candidate_json=self.candidates,
                reasons=("A synthetic match.",),
                question_packet=None,
            )
            for result in results
        }


def _truthful_ais(session: Session) -> tuple[Any, list[CropJob]]:
    """Both AIs, answering what the drawings show: V1 is the architect's `3' - 4"` cabinet and V2
    its `2' - 2"` cabinet; nothing measures V3 or the whole run."""
    asked: list[CropJob] = []

    def ask(jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        asked.extend(jobs)
        answers: dict[tuple[str, str], object] = {}
        for job in jobs:
            packet = job.question_packet or {}
            ids = [UUID(str(value)) for value in packet.get("architect_candidate_ids", [])]  # type: ignore[attr-defined]
            texts = [
                session.get_one(ObservationCandidate, value).raw_text.replace("’", "'")
                for value in ids
            ]
            number = {text: k for k, text in enumerate(texts, start=1)}
            pieces = (number.get("3' - 4\"", 0), number.get("2' - 2\"", 0), 0)
            answers[(job.key, job.model_id)] = ArchPairAnswer(
                job.model_id,
                0,
                pieces,
                "the same cabinets",
                tuple("single_cabinet" for _ in texts),
            )
        return answers

    return ask, asked


def _pair(
    session: Session,
    package: Package,
    matcher: FakeMatcher | None,
    *,
    drawn: Sequence[int],
    printed: Sequence[int],
) -> tuple[UUID, PageSlotResult, list[CropJob]]:
    """Store the vendor row, run the real pairing step beside the fake matcher, and persist what
    #1166's stage and this stage persist."""
    anchor = _vendor_row(session, package.revision, package.vendor_page, printed)
    result = _slot_result(package.vendor_page, anchor, drawn, printed)
    ask, asked = _truthful_ais(session)
    (paired,) = ArchitectPairing(
        MEASURED_PAIRING_SETTINGS,
        package.pages,
        matcher=matcher,
        crops=_pictures(package),
    ).pair(
        [result],
        [_slot_page(package.vendor_page)],
        ask=ask,
        readers=(OPUS, SONNET),
        ask_the_ais=True,
        store=None,
        effort="high",
    )
    run_id = session.get_one(ObservationCandidate, anchor).extraction_run_id
    if paired.architect_match is not None:
        _store_match(session, package, anchor, run_id, paired.architect_match)
    persist_architect_pairings(
        session,
        package_revision_id=package.revision.id,
        extraction_run_id=run_id,
        results=[paired],
    )
    session.commit()
    return anchor, paired, asked


# --- #1166's records, as its stage and API write them (written here until they exist) ------------


def _store_match(
    session: Session, package: Package, anchor: UUID, run_id: UUID, match: RowMatch
) -> ArchitectViewMatchRecord:
    record = ArchitectViewMatchRecord(
        package_revision_id=package.revision.id,
        vendor_page_id=package.vendor_page.id,
        row_anchor_candidate_id=anchor,
        extraction_run_id=run_id,
        source=match.source,
        status=match.status,
        matched_view_id=None if match.chosen is None else match.chosen.view_id,
        ai_picks=[dict(pick) for pick in match.ai_picks],
        candidates=[dict(candidate) for candidate in match.candidate_json],
        vendor_references=[],
        reasons=list(match.reasons),
        details={},
        carried_from_id=None,
    )
    session.add(record)
    session.flush()
    return record


def _reviewer_pick(
    session: Session, anchor: UUID, view: MatchedView | None
) -> ArchitectViewMatchRecord:
    """A reviewer's pick (or "none of these"), superseding the row's latest match record."""
    current = _latest_match_record(session, anchor)
    assert current is not None
    record = ArchitectViewMatchRecord(
        package_revision_id=current.package_revision_id,
        vendor_page_id=current.vendor_page_id,
        row_anchor_candidate_id=anchor,
        extraction_run_id=None,
        source="reviewer",
        status="none_matches" if view is None else "reviewer_confirmed",
        matched_view_id=None if view is None else view.view_id,
        ai_picks=current.ai_picks,
        candidates=current.candidates,
        vendor_references=[],
        reasons=["A reviewer chose."],
        details={},
        supersedes_id=current.id,
        decided_by="reviewer@example.com",
    )
    session.add(record)
    session.flush()
    return record


def _latest_match_record(session: Session, anchor: UUID) -> ArchitectViewMatchRecord | None:
    return session.scalars(
        select(ArchitectViewMatchRecord)
        .where(ArchitectViewMatchRecord.row_anchor_candidate_id == anchor)
        .order_by(ArchitectViewMatchRecord.created_at.desc(), ArchitectViewMatchRecord.id.desc())
        .limit(1)
    ).first()


def _views_by_id(session: Session) -> dict[UUID, MatchedView]:
    found: dict[UUID, MatchedView] = {}
    for entry in session.scalars(select(ArchitectViewIndexEntry)):
        page = session.get_one(Page, entry.page_id)
        found[entry.id] = MatchedView(
            view_id=entry.id,
            document_version_id=entry.document_version_id,
            page_id=entry.page_id,
            page_number=page.index + 1,
            view_number=entry.view_number,
            view_tag=entry.view_tag,
            title=entry.title,
            bubble=entry.bubble,
            sheet_number=entry.sheet_number,
            scale_note=entry.scale_note,
            file_name=FILE_NAME,
            separated=entry.separated,
        )
    return found


def stored_match(session: Session, anchor: UUID) -> EffectiveMatch | None:
    """The row's effective match, read from the stored records (#1166's read path, restated)."""
    record = _latest_match_record(session, anchor)
    if record is None:
        return None
    view = None if record.matched_view_id is None else _views_by_id(session)[record.matched_view_id]
    return EffectiveMatch(
        record_id=record.id,
        status=record.status,  # type: ignore[arg-type]
        source=record.source,  # type: ignore[arg-type]
        matched=view,
        needs_reviewer=record.status == "needs_reviewer",
        reasons=tuple(record.reasons),
        decided_by=record.decided_by,
    )


def stored_matches(session: Session, anchors: Any) -> dict[UUID, EffectiveMatch | None]:
    return {anchor: stored_match(session, anchor) for anchor in anchors}


def _pairing(session: Session, anchor: UUID) -> Any:
    return latest_architect_pairing(session, anchor, matches=stored_matches)


def _check(session: Session, store: LocalStore, package: Package) -> dict[str, Any]:
    """Run the real check stage; the architect check's findings, by row and for the package."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from app.models.verdicts import CheckRun, Finding

    DatabaseStages(store, architect_pairing=_pairing, architect_match=stored_match).run_checks(
        session, package.revision.id
    )
    session.commit()
    findings = session.scalars(
        select(Finding)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == package.revision.id,
            CheckRun.superseded_at.is_(None),
            RuleDefinition.rule_id == ARCHITECT_CHECK_RULE_ID,
        )
    ).all()
    return {
        "row": [f for f in findings if f.scope_row_candidate_id is not None],
        "package": [f for f in findings if f.scope_row_candidate_id is None],
    }


def _candidates_in_view(session: Session, page: Page, number: int) -> set[UUID]:
    return {
        candidate.id
        for candidate in session.scalars(
            select(ObservationCandidate).where(ObservationCandidate.page_id == page.id)
        )
        if f"arch-view:{number}" in (candidate.ambiguity_flags or ())
    }


def _code_pairing_json(
    package: Package, number: int, drawn: Sequence[int], printed: Sequence[int], anchor: UUID
) -> dict[str, object]:
    """What #1166 stores per candidate view: code's pairing against that view alone."""
    vendor = vendor_row_input(_slot_result(package.vendor_page, anchor, drawn, printed))
    assert vendor is not None
    sheet = package.pages[package.architect_page.id]
    outcome, _raw = pair_by_code(
        vendor,
        restrict_to_view(sheet, number, sheet.view_extents[number]),
        MEASURED_PAIRING_SETTINGS,
    )
    return {
        "status": outcome.status,
        "source": "code",
        "pairs": [pair.as_json() for pair in outcome.pairs],
        "details": dict(outcome.details),
    }


EQUAL = ((40, 26, 30), (40, 26, 30))
"""Drawn and printed: the vendor's first two pieces are the architect's two cabinets."""
ONE_OFF = ((40, 26, 30), (40, 27, 30))
"""The vendor prints 27 on the piece drawn (and drawn by the architect) as 26."""


# --- (a) automatic match → pairing → exact PASS and FAIL ---------------------------------------


@pytest.mark.parametrize(("widths", "expected"), [(EQUAL, "PASS"), (ONE_OFF, "FAIL")])
def test_an_automatic_match_is_paired_by_code_and_both_ais_and_decided_exactly(
    session: Session, store: LocalStore, widths: tuple[tuple[int, ...], ...], expected: str
) -> None:
    package = _two_file_package(session, store)
    view = package.views[1]
    matcher = FakeMatcher("auto_matched", view)

    anchor, paired, asked = _pair(session, package, matcher, drawn=widths[0], printed=widths[1])

    assert matcher.seen == [package.vendor_page.id], "the vendor page has no architect view"
    assert paired.architect_match is not None and paired.architect_match.chosen == view
    assert [job_prompt_id(job, None) for job in asked] == [ARCH_PAIR_2PANEL_PROMPT_ID] * 2
    pairing = paired.architect_pairing
    assert pairing is not None and (pairing.source, pairing.status) == ("code+ais", "paired")
    assert pairing.details["architect_view_id"] == str(view.view_id)
    record = session.scalars(select(ArchitectPairingRecord)).one()
    assert record.details["match_record_id"] == str(_latest_match_record(session, anchor).id)  # type: ignore[union-attr]
    in_view = _candidates_in_view(session, package.architect_page, 1)
    assert {pair.architect_candidate_id for pair in pairing.pairs} <= in_view

    found = _check(session, store, package)

    (finding,) = found["row"]
    assert finding.outcome == expected, finding.reason
    assert finding.notes[0] == compared_with_text(view)
    assert FILE_NAME in finding.notes[0] and "view 1" in finding.notes[0]
    assert found["package"] == [], "a row was compared: no package line"
    assert architect_file_indexed(session, package.revision.id)


# --- (b) twin views → the reviewer picks → the checks run again → compared --------------------------


def test_twin_views_wait_for_the_reviewers_pick_then_are_compared_with_that_view_only(
    session: Session, store: LocalStore
) -> None:
    package = _two_file_package(session, store, views=2)
    first, second = package.views[1], package.views[2]
    candidates = tuple(
        {
            "view_id": str(view.view_id),
            "rank": rank,
            "code_pairing": None,
            "remembered": False,
        }
        for rank, view in enumerate((first, second), start=1)
    )
    matcher = FakeMatcher("needs_reviewer", None, candidates)

    anchor, paired, asked = _pair(session, package, matcher, drawn=EQUAL[0], printed=EQUAL[1])

    assert paired.architect_pairing is None, "nothing is paired before the reviewer picks"
    assert asked == []
    assert session.scalars(select(ArchitectPairingRecord)).all() == []
    found = _check(session, store, package)
    (waiting,) = found["row"]
    assert (waiting.outcome, waiting.reason) == ("REVIEW_REQUIRED", CHOOSE_ARCHITECT_VIEW)

    # #1166 stores code's pairing for every candidate view; the reviewer picks the second twin.
    automatic = _latest_match_record(session, anchor)
    assert automatic is not None
    with_pairings: list[dict[str, object]] = [
        {**candidate, "code_pairing": _code_pairing_json(package, number, *EQUAL, anchor)}
        for number, candidate in zip((1, 2), candidates, strict=True)
    ]
    automatic_with_pairings = ArchitectViewMatchRecord(
        package_revision_id=automatic.package_revision_id,
        vendor_page_id=automatic.vendor_page_id,
        row_anchor_candidate_id=anchor,
        extraction_run_id=None,
        source="reviewer",
        status="reviewer_confirmed",
        matched_view_id=second.view_id,
        ai_picks=[],
        candidates=with_pairings,
        vendor_references=[],
        reasons=["A reviewer chose."],
        details={},
        supersedes_id=automatic.id,
        decided_by="reviewer@example.com",
    )
    session.add(automatic_with_pairings)
    session.flush()
    anchor_row = session.get_one(ObservationCandidate, anchor)
    appended = record_code_pairing_for_view(
        session,
        anchor=anchor_row,
        package_revision_id=package.revision.id,
        match_record=automatic_with_pairings,
    )
    session.commit()

    assert appended is not None and (appended.source, appended.status) == ("code", "paired")
    pairing = _pairing(session, anchor)
    assert pairing is not None and pairing.source == "code"
    second_view = _candidates_in_view(session, package.architect_page, 2)
    assert pairing.pairs and {p.architect_candidate_id for p in pairing.pairs} <= second_view

    found = _check(session, store, package)
    (compared,) = found["row"]
    # One judgment (code's) on the pairing: compared, and the reviewer confirms the pairing.
    assert compared.outcome == "REVIEW_REQUIRED"
    assert "Only code paired these" in (compared.reason or "")
    assert any("The engine's comparison" in note and "PASS" in note for note in compared.notes)
    assert compared.notes[1] == compared_with_text(second), compared.notes

    # The reviewer confirms the pairing: a span of the other twin is refused; then a PASS.
    other = sorted(_candidates_in_view(session, package.architect_page, 1))
    with pytest.raises(ReviewerPairingRefused):
        record_reviewer_pairing(
            session,
            anchor=anchor_row,
            package_revision_id=package.revision.id,
            piece_count=3,
            pairs=[DecidedPair("piece", other[0], (0,))],
            note=None,
            actor="reviewer@example.com",
            match_lookup=stored_match,
        )
    record_reviewer_pairing(
        session,
        anchor=anchor_row,
        package_revision_id=package.revision.id,
        piece_count=3,
        pairs=[
            DecidedPair(pair.kind, pair.architect_candidate_id, pair.vendor_slot_indices)
            for pair in pairing.pairs
        ],
        note=None,
        actor="reviewer@example.com",
        match_lookup=stored_match,
    )
    session.commit()

    found = _check(session, store, package)
    (decided,) = found["row"]
    assert decided.outcome == "PASS", decided.reason
    assert decided.notes[0] == compared_with_text(second)


# --- (c) "none of these" → not compared, said plainly ------------------------------------------------


def test_none_of_these_is_not_compared_and_says_so(session: Session, store: LocalStore) -> None:
    package = _two_file_package(session, store, views=2)
    anchor, _paired, _asked = _pair(
        session, package, FakeMatcher("needs_reviewer", None), drawn=EQUAL[0], printed=EQUAL[1]
    )
    _reviewer_pick(session, anchor, None)
    session.commit()

    found = _check(session, store, package)

    assert found["row"] == [], "nothing compared: no finding for the row"
    (line,) = found["package"]
    assert (line.outcome, line.reason) == ("NO_APPLICABLE_RULE", NOTHING_PAIRED_ON_REVISION)
    (row,) = slot_rows_and_unchosen_pages(session, package.revision.id)[0]
    plan = plan_architect_row(
        session,
        row,
        _pairing(session, anchor),
        separate_architect_file=True,
        match=stored_match(session, anchor),
        architect_file_indexed=True,
    )
    assert (plan.disposition, plan.reason) == (Disposition.NOT_COMPARED, NO_ARCHITECT_VIEW_MATCHES)


# --- the guards: never a value from outside the matched view ----------------------------------------


def test_a_pairing_for_a_view_that_is_no_longer_the_match_never_counts(
    session: Session, store: LocalStore
) -> None:
    package = _two_file_package(session, store, views=2)
    first, second = package.views[1], package.views[2]
    anchor, paired, _asked = _pair(
        session, package, FakeMatcher("auto_matched", first), drawn=EQUAL[0], printed=EQUAL[1]
    )
    assert paired.architect_pairing is not None
    assert _pairing(session, anchor) is not None

    # The reviewer moves the match to the other twin; no pairing exists for it yet.
    _reviewer_pick(session, anchor, second)
    session.commit()

    assert _pairing(session, anchor) is None, "the first view's pairing no longer counts"
    found = _check(session, store, package)
    (waiting,) = found["row"]
    assert waiting.outcome == "REVIEW_REQUIRED"
    assert (waiting.reason or "").startswith(f"Matched with {FILE_NAME}, page 1, view 2: ")
    anchor_row = session.get_one(ObservationCandidate, anchor)
    with pytest.raises(ReviewerPairingRefused, match="has changed"):
        record_reviewer_pairing(
            session,
            anchor=anchor_row,
            package_revision_id=package.revision.id,
            piece_count=3,
            pairs=[],
            note=None,
            actor="reviewer@example.com",
            match_lookup=stored_match,
        )
    rows, _unchosen = slot_rows_and_unchosen_pages(session, package.revision.id)
    candidate = session.get_one(
        ObservationCandidate, min(_candidates_in_view(session, package.architect_page, 1))
    )
    assert architect_candidate_refusal(session, rows[0], candidate, matched=second) == (
        "it is not on the vendor's sheet or the architect view matched with this row."
    )
    assert architect_candidate_refusal(session, rows[0], candidate) == (
        "it is on a different sheet from the vendor's row."
    ), "without a match the refusal is word for word as before"


def test_the_verdict_guard_refuses_an_architect_value_from_another_view(
    session: Session, store: LocalStore
) -> None:
    """The values lie in view 1. Recorded with view 1 as the row's match they are accepted; named
    against view 2, or with no match at all, the same values are refused."""
    from app.verdicts.record import EvidenceMissing, record_finding
    from app.verdicts.rulebook import snapshot_store
    from verdict.finding import Finding
    from workflow.architect_row_evidence import architect_row_operands
    from workflow.review import ENGINE_VERSION

    package = _two_file_package(session, store, views=2)
    first, second = package.views[1], package.views[2]
    anchor, _paired, _asked = _pair(
        session, package, FakeMatcher("auto_matched", first), drawn=EQUAL[0], printed=EQUAL[1]
    )
    rows, _unchosen = slot_rows_and_unchosen_pages(session, package.revision.id)
    pairing = _pairing(session, anchor)
    plan = plan_architect_row(
        session,
        rows[0],
        pairing,
        separate_architect_file=True,
        match=stored_match(session, anchor),
        architect_file_indexed=True,
    )
    assert plan.matched == first
    built = architect_row_operands(session, rows[0], plan)
    assert built.eligible, built.reason
    session.commit()
    snapshot = snapshot_store(session).latest(ARCHITECT_CHECK_RULE_ID)
    assert snapshot is not None
    finding = Finding(
        rule_id=ARCHITECT_CHECK_RULE_ID,
        outcome=Outcome.REVIEW_REQUIRED,
        severity=snapshot.rule.severity,
        reason="test",
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )

    def record(matched: MatchedView | None) -> None:
        record_finding(
            session,
            package_revision_id=package.revision.id,
            finding=finding,
            operands=built.operands,
            parameter_set_ids={},
            scope_row_candidate_id=anchor,
            scope_label="row · architect",
            architect_pairing=pairing,
            architect_matched_view=matched,
        )

    with session.begin_nested():
        record(first)
    for wrong, why in (
        (second, "does not belong to this row's sheet"),
        (None, "belongs to a different row or page"),
    ):
        with pytest.raises(EvidenceMissing, match=why), session.begin_nested():
            record(wrong)
    session.rollback()


# --- (d) a combined sheet is unchanged -----------------------------------------------------------------


def test_a_combined_sheet_never_reaches_the_matcher_and_is_asked_v3(
    session: Session, store: LocalStore
) -> None:
    revision = _upload(session, store, combined_sheet())
    stages = _stages(store)
    stages.extract_pages(session, revision.id)
    session.commit()
    page = session.scalars(select(Page)).one()
    pages = dict(stages._architect_pages)
    assert pages[page.id].has_architect_view
    anchor = _vendor_row(session, revision, page, EQUAL[1])
    result = _slot_result(page, anchor, *EQUAL)
    outcomes = []
    for matcher in (None, FakeMatcher("auto_matched", None)):
        ask, asked = _truthful_ais(session)
        (paired,) = ArchitectPairing(MEASURED_PAIRING_SETTINGS, pages, matcher=matcher).pair(
            [result],
            [_slot_page(page)],
            ask=ask,
            readers=(OPUS, SONNET),
            ask_the_ais=True,
            store=None,
            effort="high",
        )
        assert paired.architect_match is None
        assert {job_prompt_id(job, None) for job in asked} <= {ARCH_PAIR_PROMPT_ID}
        assert not any(job.arch_pair_two_panel for job in asked)
        outcomes.append((paired.architect_pairing, [job.question_packet for job in asked]))
        if matcher is not None:
            assert matcher.seen == [], "a page with its own architect view is never matched"
    assert outcomes[0] == outcomes[1], "the matcher changes nothing on a combined sheet"
    assert not architect_file_indexed(session, revision.id)


def test_a_separate_file_that_produced_no_index_keeps_the_package_line(
    session: Session, store: LocalStore
) -> None:
    """The architect reader did not run, so the file has no view index: #1161's line, unchanged,
    and no match is ever looked up."""
    revision = _upload(session, store, architect_sheet(vendor=True))
    architect = _add_architectural(session, store, revision, architect_sheet())
    _stages(store, enabled=False).extract_pages(session, revision.id)
    session.commit()
    _publish_rulebook(session)
    shop_page = session.scalars(select(Page).where(Page.document_version_id != architect.id)).one()
    _vendor_row(session, revision, shop_page, EQUAL[1])
    session.commit()
    asked: list[UUID] = []

    def no_match(_session: Session, row: UUID) -> EffectiveMatch | None:
        asked.append(row)
        return None

    DatabaseStages(
        store, architect_pairing=lambda _s, _a: None, architect_match=no_match
    ).run_checks(session, revision.id)
    found = _check(session, store, Package(revision, shop_page, shop_page, {}))

    assert asked == [], "no index: no match is ever looked up"
    assert not architect_file_indexed(session, revision.id)
    (line,) = found["package"]
    assert (line.outcome, line.reason) == ("REVIEW_REQUIRED", SEPARATE_ARCHITECT_FILE_NOT_COMPARED)
