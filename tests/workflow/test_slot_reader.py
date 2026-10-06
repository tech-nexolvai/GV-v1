"""Slot reads in the worker, end to end on hand-built sheets with fake readers (#987).

Verification for `workflow/slot_reader.py`. The fake reader "reads" a crop by looking up which
label it was cut for, so these tests exercise the real planning, cropping, ink check, sealing,
naming and mapping. No network, no client drawing, no client value.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Mapping
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest

from app.config import Settings
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import MEASURED_SETTINGS
from extraction.ink import read_page_ink
from extraction.rasterise import render_page
from extraction.rows import page_rows_and_ink
from extraction.slot_reader.mapping import CABINET_FIELD, FILLER_FIELD, OVERALL_FIELD, WHAT_IS_IT
from extraction.slot_reader.runs import E2_CROP_SETTINGS, plan_slots
from extraction.slot_reader.seal import LabelState
from tests.extraction.slot_reader import sheets
from workflow.form_reader import FormReaderRuntime
from workflow.slot_reader import (
    FRACTION_BAR_ENV,
    PageSlotResult,
    SlotPage,
    SlotReaderRuntime,
    _crop_png,
    _pixels,
    configured_slot_reader,
    read_slot_pages,
)

QWEN = "qwen.qwen3-vl-235b-a22b"
KIMI = "us.moonshotai.kimi-k3"
DPI = 150
FRACTION_ENV = {
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "0.3",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "1",
    "GV_READER_FRACTION_REACH_PT": "3",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "1",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "12",
    "GV_READER_FRACTION_PROPORTION_MAX": "2.5",
    "GV_READER_FRACTION_CHARACTER_GAP_PT": "4",
    "GV_READER_FRACTION_TURNED_ASPECT_MIN": "1.1",
}


class Rates:
    def rate_for(self, model_id: str) -> object | None:
        return object()


class FakeReaders:
    """Answers each crop with `read(model, crop_png)`; records every request."""

    def __init__(self, read: Callable[[str, bytes], str]) -> None:
        self.read = read
        self.requests: list[tuple[str, bytes]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> FakeReaders:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        model = kwargs["modelId"]
        png = kwargs["messages"][0]["content"][0]["image"]["source"]["bytes"]
        with self.lock:
            self.requests.append((model, png))
        text = self.read(model, png)
        payload = {
            "text": text,
            "stacked": False,
            "combined": False,
            "readable": True,
            "no_dimension": not text,
        }
        return {
            "output": {"message": {"content": [{"text": json.dumps(payload)}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


def runtime(clients: FakeReaders, *, allow_stacked: bool = False) -> SlotReaderRuntime:
    from workflow.slot_reader import fraction_bar_from_environment

    form = FormReaderRuntime(
        reader_ids=(KIMI, QWEN),
        clients=clients,
        rates=Rates(),  # type: ignore[arg-type]
        calls_per_minute={KIMI: 6000, QWEN: 6000},
        max_concurrent_calls=4,
        max_tokens=400,
        max_throttle_retries=0,
        retry_backoff_seconds=0.001,
        prompt=None,  # type: ignore[arg-type]
    )
    return SlotReaderRuntime(
        form=form,
        crop_settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
        fraction_bar=fraction_bar_from_environment(FRACTION_ENV),
        allow_stacked=allow_stacked,
    )


def slot_page(data: bytes) -> SlotPage:
    return SlotPage(
        page_index=0,
        page_id=uuid4(),
        document_version_id=uuid4(),
        rendered=render_page(
            data,
            0,
            document_version_id=uuid4(),
            page_content_hash="1" * 64,
            dpi=DPI,
            maximum_pixels=10_000_000,
            vendor_only=True,
        ),
        rows=page_rows_and_ink(data, 0, dpi=DPI, settings=MEASURED_SETTINGS),
        ink=read_page_ink(data, 0, dpi=DPI),
    )


def crops_to_texts(page: SlotPage, texts: Mapping[int | None, str]) -> dict[bytes, str]:
    """Which crop was cut for which slot's dimension label: what a perfect reader would say."""
    plan = plan_slots(
        page.rows.candidates.rows,
        page.rows.ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    found: dict[bytes, str] = {}
    for owner in (*plan.slots, plan.overall):
        assert owner is not None
        for label in owner.labels:
            png = _crop_png(page.rendered, _pixels(page.rows, label.crop, page.rendered))
            if label.text is None or any(ch.isdigit() for ch in label.text):
                found[png] = texts.get(owner.index, "")
    return found


def read(page: SlotPage, readers: FakeReaders, **options: Any) -> PageSlotResult:
    attempts: list[AttemptUsage] = []
    (result,) = read_slot_pages(
        [page], runtime=runtime(readers, **options), record_attempt=attempts.append
    )
    assert len(attempts) == len(readers.requests)
    return result


TEXTS: dict[int | None, str] = {0: '12"', 1: '24"', 2: '36"', None: '72"'}


def named_sheet(extra: bytes = b"") -> bytes:
    """Text labels with the vendor's words beside them: `Filler`, a cabinet tag, `Filler`."""
    drawing = sheets.text_labels().replace(
        b'1 0 0 1 318.00 604.00 Tm (36") Tj', b'1 0 0 1 333.00 604.00 Tm (36") Tj'
    )
    drawing += sheets.text(190, sheets.CHAIN_Y + 4, "Filler")
    drawing += sheets.text(265, sheets.CHAIN_Y + 4, "B24")
    drawing += sheets.text(302, sheets.CHAIN_Y + 4, "Filler")
    return sheets.sheet(drawing + extra)


def test_text_labels_seal_on_the_file_and_one_reader_and_unnamed_pieces_ask_what_they_are() -> None:
    page = slot_page(sheets.sheet(sheets.text_labels()))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])

    result = read(page, readers)

    assert {model for model, _ in readers.requests} == {QWEN}, "a text label needs one reader"
    assert result.overall is not None and result.overall.outcome.state is LabelState.SEALED
    assert all(slot.outcome.state is LabelState.SEALED for slot in result.slots)
    assert [p.field_key for p in result.mapping.proposals] == [OVERALL_FIELD]
    assert all(reason.startswith(WHAT_IS_IT) for _, reason in result.mapping.held)


def test_a_named_sealed_chain_fills_the_form_left_to_right() -> None:
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert [slot.kind.kind.value for slot in result.slots if slot.kind] == [
        "filler",
        "cabinet",
        "filler",
    ]
    assert [(p.field_key, p.position, p.slot_index) for p in result.mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (FILLER_FIELD, 0, 0),
        (CABINET_FIELD, 0, 1),
        (FILLER_FIELD, 1, 2),
    ]


def test_a_reader_that_differs_from_the_file_holds_the_piece_and_the_chain() -> None:
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS | {1: '21"'})
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.slots[1].outcome.reason_code == "readers-differ"
    assert [p.field_key for p in result.mapping.proposals] == [OVERALL_FIELD]


def test_a_label_under_the_reviewers_yellow_box_is_never_read_or_sealed() -> None:
    """Yellow painted over the middle label, as the reviewer does: covered, no value, no call."""
    page = slot_page(named_sheet(sheets.yellow_box(241, sheets.CHAIN_Y + 3, 13, 7)))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup.get(png, '99"'))

    result = read(page, readers)

    middle = result.slots[1]
    assert middle.outcome.state is LabelState.REVIEW
    assert middle.outcome.reason is not None
    assert middle.outcome.reason.startswith("covered by reviewer markup")
    assert middle.outcome.value is None
    assert len(readers.requests) == 3, "the covered label costs no call"
    assert [p.field_key for p in result.mapping.proposals] == [OVERALL_FIELD]


def test_glyph_labels_need_two_makers_to_agree() -> None:
    page = slot_page(sheets.sheet(sheets.glyph_labels()))
    lookup = crops_to_texts(page, TEXTS)

    agreed = read(page, FakeReaders(lambda _model, png: lookup[png]))
    assert all(slot.outcome.state is LabelState.SEALED for slot in agreed.slots)
    assert agreed.overall is not None and agreed.overall.outcome.state is LabelState.SEALED

    def kimi_misreads(model: str, png: bytes) -> str:
        return '17"' if model == KIMI and lookup[png] == '12"' else lookup[png]

    differ = read(page, FakeReaders(kimi_misreads))
    assert differ.slots[0].outcome.reason_code == "readers-differ"
    assert differ.slots[0].outcome.value is None


def test_a_reader_whose_answer_stays_malformed_abstains_and_the_reading_waits() -> None:
    """Kimi's answer is not JSON, twice: it abstains on every crop; Qwen alone never seals."""
    page = slot_page(sheets.sheet(sheets.glyph_labels()))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])
    answer_properly = readers.converse

    def converse(**kwargs: Any) -> Mapping[str, Any]:
        if kwargs["modelId"] == KIMI:
            return {"output": {"message": {"content": [{"text": "not json"}]}}}
        return answer_properly(**kwargs)

    readers.converse = converse  # type: ignore[method-assign]
    (result,) = read_slot_pages([page], runtime=runtime(readers), record_attempt=lambda _a: None)

    assert all(slot.outcome.reason_code == "one-reader-missing" for slot in result.slots)
    assert result.mapping.proposals == ()


def test_the_slot_reader_is_off_unless_switched_on() -> None:
    settings = Settings(database_url="postgresql+psycopg://x@localhost/x")
    assert settings.slot_reader_enabled is False
    assert settings.slot_reader_stacked_agreement is False
    assert configured_slot_reader(settings, None) is None


def test_switching_it_on_needs_the_form_reader_and_the_fraction_bar_lengths() -> None:
    with pytest.raises(ValueError, match="GV_FORM_READER_ENABLED"):
        Settings(database_url="postgresql+psycopg://x@localhost/x", slot_reader_enabled=True)

    class On:
        slot_reader_enabled = True
        slot_reader_stacked_agreement = False

    with pytest.raises(ValueError, match="GV_FORM_READER_ENABLED"):
        configured_slot_reader(On(), None, environ=FRACTION_ENV)
    form = runtime(FakeReaders(lambda _m, _p: "")).form
    with pytest.raises(ValueError, match="fraction-bar"):
        configured_slot_reader(On(), form, environ={})
    configured = configured_slot_reader(On(), form, environ=FRACTION_ENV)
    assert configured is not None and configured.allow_stacked is False
    assert set(FRACTION_BAR_ENV) == set(FRACTION_ENV)


# ---------------------------------------------------------------------------------------------
# Persistence (PostgreSQL; skipped without DATABASE_URL, run in CI)
# ---------------------------------------------------------------------------------------------

pytest_plugins = ("tests.app.postgres_fixture",)


@pytest.fixture
def session(postgres_engine: Any) -> Iterator[Any]:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def test_persisted_candidates_carry_what_the_screen_needs_and_only_offered_ones_are_linked(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models import (
        Document,
        DocumentVersion,
        Package,
        PackageRevision,
        PackageRevisionDocument,
        PackageState,
        Project,
        SourceArtifact,
    )
    from app.models.document import DocumentKind, Page
    from app.models.evidence import MeasurementProposal, ObservationCandidate
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
    from workflow.slot_reader import persist_slot_readings

    project = Project(id=uuid4(), name="slot reader tests")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Example Millwork")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.NEEDS_INPUT
    )
    source = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="1" * 64, size=1)
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add_all([revision, source, document])
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=source.id, sha256="1" * 64, page_count=1
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
    page_row = Page(
        document_version_id=version.id,
        index=0,
        content_hash="1" * 64,
        width_pt=Decimal(400),
        height_pt=Decimal(300),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page_row)
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
        extractor="extraction.form_reader",
        extractor_version="slot-reader-v1-ink-v1",
        config_hash="test",
    )
    session.add(run)
    session.flush()

    data = named_sheet(sheets.yellow_box(241, sheets.CHAIN_Y + 3, 13, 7))
    page = slot_page(data)
    page = SlotPage(
        page_index=0,
        page_id=page_row.id,
        document_version_id=version.id,
        rendered=page.rendered,
        rows=page.rows,
        ink=page.ink,
    )
    lookup = crops_to_texts(page, TEXTS)
    (result,) = read_slot_pages(
        [page],
        runtime=runtime(FakeReaders(lambda _model, png: lookup[png])),
        record_attempt=lambda _a: None,
    )

    count = persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(KIMI, QWEN),
        results=[result],
    )

    assert count == 4  # three slots and the overall
    rows = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run.id)
    ).all()
    by_slot = {
        next(flag for flag in row.ambiguity_flags if flag.startswith("slot:")): row for row in rows
    }
    covered = by_slot["slot:1"]
    assert covered.value_numerator is None and covered.corroboration_status is None
    assert covered.review_reason is not None and covered.review_reason.startswith("covered by")
    assert covered.semantic_guess is None
    assert "ink:covered" in covered.ambiguity_flags
    first = by_slot["slot:0"]
    assert "kind:filler" in first.ambiguity_flags
    assert any(flag.startswith("slot-box:") for flag in first.ambiguity_flags)
    assert any(flag.startswith("crop-box:") for flag in first.ambiguity_flags)
    assert first.corroboration_status is None, "sealed but held back: not offered"
    assert first.value_numerator == 12 and first.review_reason is not None
    overall = by_slot["slot:overall"]
    assert overall.corroboration_status == "CORROBORATED" and overall.value_numerator == 72

    proposals = session.scalars(
        select(MeasurementProposal).where(MeasurementProposal.package_revision_id == revision.id)
    ).all()
    assert [(p.field_key, p.candidate_id) for p in proposals] == [(OVERALL_FIELD, overall.id)]
