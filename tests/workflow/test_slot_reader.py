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
from extraction.slot_reader.mapping import (
    CABINET_FIELD,
    FILLER_FIELD,
    OVERALL_FIELD,
    PIECE_FIELD,
)
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


UNSURE_WALLS = {"left": "unsure", "right": "unsure", "behind": "unsure", "view": "elevation"}
BOTH_WALLS = {"left": "yes", "right": "yes", "behind": "yes", "view": "elevation"}


class FakeReaders:
    """Answers each crop with `read(model, crop_png)` and each row's wall question with
    `walls(model)`; records every request."""

    def __init__(
        self,
        read: Callable[[str, bytes], str],
        walls: Callable[[str], Mapping[str, str]] = lambda _model: UNSURE_WALLS,
    ) -> None:
        self.read = read
        self.walls = walls
        self.requests: list[tuple[str, bytes]] = []
        self.wall_requests: list[tuple[str, bytes, bytes]] = []
        self.lock = threading.Lock()

    def for_current_thread(self) -> FakeReaders:
        return self

    def converse(self, **kwargs: Any) -> Mapping[str, Any]:
        model = kwargs["modelId"]
        content = kwargs["messages"][0]["content"]
        pictures = [part["image"]["source"]["bytes"] for part in content if "image" in part]
        if len(pictures) == 2:
            with self.lock:
                self.wall_requests.append((model, pictures[0], pictures[1]))
            return {
                "output": {"message": {"content": [{"text": json.dumps(dict(self.walls(model)))}]}},
                "usage": {"inputTokens": 20, "outputTokens": 9},
            }
        png = pictures[0]
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
    assert len(attempts) == len(readers.requests) + len(readers.wall_requests)
    return result


#: Drawn to scale, as a shop drawing is: the sheet's slots are 50, 100 and 50 points long, so at
#: 0.24" a point they print 12", 24" and 12", and the 200-point overall 48".
PIECES = ('12"', '24"', '12"')
OVERALL = '48"'
TEXTS: dict[int | None, str] = {0: '12"', 1: '24"', 2: '12"', None: '48"'}


def text_labels() -> bytes:
    return sheets.text_labels(PIECES, OVERALL)


def named_sheet(extra: bytes = b"") -> bytes:
    """Text labels with the vendor's words beside them: `Filler`, a cabinet tag, `Filler`."""
    drawing = text_labels().replace(
        b'1 0 0 1 318.00 604.00 Tm (12") Tj', b'1 0 0 1 333.00 604.00 Tm (12") Tj'
    )
    drawing += sheets.text(190, sheets.CHAIN_Y + 4, "Filler")
    drawing += sheets.text(265, sheets.CHAIN_Y + 4, "B24")
    drawing += sheets.text(302, sheets.CHAIN_Y + 4, "Filler")
    return sheets.sheet(drawing + extra)


def test_text_labels_seal_on_the_file_and_one_reader_and_unnamed_pieces_still_fill_the_width() -> (
    None
):
    page = slot_page(sheets.sheet(text_labels()))
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png])

    result = read(page, readers)

    assert {model for model, _ in readers.requests} == {QWEN}, "a text label needs one reader"
    assert result.overall is not None and result.overall.outcome.state is LabelState.SEALED
    assert all(slot.outcome.state is LabelState.SEALED for slot in result.slots)
    # #992: a fully sealed row offers every piece, whatever its kind; unnamed pieces still keep
    # the cabinet and filler fields back.
    assert [(p.field_key, p.position, p.slot_index) for p in result.mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
    ]


def test_a_named_sealed_chain_fills_the_form_left_to_right() -> None:
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert [slot.kind.kind.value for slot in result.slots if slot.kind] == [
        "filler",
        "cabinet",
        "filler",
    ]
    # Piece widths only: CT-WIDTH-001 (#993) sends piece widths beside a cabinet or filler list to
    # review, so a named row is offered whole, not twice.
    assert [(p.field_key, p.position, p.slot_index) for p in result.mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
    ]
    keys = {p.field_key for p in result.mapping.proposals}
    assert CABINET_FIELD not in keys and FILLER_FIELD not in keys


def test_a_reader_that_differs_from_the_file_holds_the_piece_and_the_chain() -> None:
    page = slot_page(named_sheet())
    lookup = crops_to_texts(page, TEXTS | {1: '21"'})
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.slots[1].outcome.reason_code == "readers-differ"
    assert result.mapping.proposals == ()


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
    assert result.mapping.proposals == ()


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


def test_the_run_identity_fits_its_column() -> None:
    """`extraction_runs.config_hash` holds 200 characters; the full settings are many times that."""
    configured = runtime(FakeReaders(lambda _m, _p: ""))
    assert len(f"dpi=300;{configured.config_hash}") <= 200
    assert len(configured.config_detail) > 200


# ---------------------------------------------------------------------------------------------
# The admin's three rules (#992): drawn-length veto, labels from agreed text, walls by agreement
# ---------------------------------------------------------------------------------------------


def glyph_page() -> SlotPage:
    return slot_page(sheets.sheet(sheets.glyph_labels()))


def agreed(page: SlotPage, texts: Mapping[int | None, str], **options: Any) -> PageSlotResult:
    """Both readers print `texts` for each label: whatever they agree on, right or wrong."""
    lookup = crops_to_texts(page, texts)
    return read(page, FakeReaders(lambda _model, png: lookup[png], **options))


def test_a_misread_both_readers_agree_on_is_rejected_by_the_drawn_length() -> None:
    page = glyph_page()
    result = agreed(page, TEXTS | {2: '120"'})

    misread = result.slots[2]
    assert misread.outcome.state is LabelState.REVIEW
    assert misread.outcome.reason == "doesn't match the drawn length"
    assert misread.outcome.value is None
    label = misread.labels[misread.outcome.label_index or 0].outcome
    assert label.suggestion is not None and label.suggestion.exact == 120, "kept, never changed"
    assert 2 in result.vetoed
    assert result.mapping.proposals == (), "a vetoed piece holds the whole row"


def test_correct_readings_drawn_to_scale_are_never_vetoed() -> None:
    result = agreed(glyph_page(), TEXTS)
    assert result.vetoed == ()
    assert [p.field_key for p in result.mapping.proposals].count(PIECE_FIELD) == 3


def test_an_agreed_sum_is_one_piece_and_fills_the_width_in_order() -> None:
    result = agreed(glyph_page(), TEXTS | {0: '10"+2" Filler'})

    first = result.slots[0]
    assert first.outcome.state is LabelState.SEALED
    assert first.outcome.value is not None and first.outcome.value.exact == 12
    assert first.kind is not None and first.kind.kind.value == "filler"
    pieces = [p for p in result.mapping.proposals if p.field_key == PIECE_FIELD]
    assert [(p.position, p.slot_index) for p in pieces] == [(0, 0), (1, 1), (2, 2)]


def test_a_text_label_with_a_sum_is_shown_to_its_reader_and_seals_on_the_same_text() -> None:
    page = slot_page(sheets.sheet(sheets.text_labels(('10"+2"', '24"', '12"'), OVERALL)))
    lookup = crops_to_texts(page, TEXTS | {0: '10"+2"'})
    readers = FakeReaders(lambda _model, png: lookup[png])

    result = read(page, readers)

    assert len(readers.requests) == 4, "the summed label is read too"
    assert result.slots[0].outcome.state is LabelState.SEALED
    assert result.slots[0].outcome.value is not None
    assert result.slots[0].outcome.value.exact == 12


def test_an_agreed_equal_share_count_is_one_entry_of_its_total() -> None:
    result = agreed(glyph_page(), TEXTS | {1: '24"(2EQ)'})
    middle = result.slots[1]
    assert middle.outcome.state is LabelState.SEALED
    assert middle.outcome.value is not None and middle.outcome.value.exact == 24
    assert PIECE_FIELD in {p.field_key for p in result.mapping.proposals}


@pytest.mark.parametrize(
    ("texts", "code", "reason"),
    [
        (
            {None: '48" (INCLUDING FIELD CUT)'},
            "field-cut-included",
            "width already includes the field cut",
        ),
        ({1: '24" VIF'}, "vif", "VIF: provisional, verify in field"),
    ],
)
def test_field_cut_or_vif_anywhere_holds_the_whole_countertop(
    texts: Mapping[int | None, str], code: str, reason: str
) -> None:
    result = agreed(glyph_page(), TEXTS | texts)

    assert result.row_hold is not None and result.row_hold.code == code
    assert result.mapping.proposals == ()
    held = dict(result.mapping.held)
    assert held, "every sealed reading says why it waits"
    assert all(why == reason for why in held.values())


def test_one_reader_seeing_vif_is_enough_to_hold_the_row() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)

    def kimi_sees_vif(model: str, png: bytes) -> str:
        text = lookup[png]
        return f"{text} VIF" if model == KIMI and text == '24"' else text

    result = read(page, FakeReaders(kimi_sees_vif))
    assert result.row_hold is not None and result.row_hold.code == "vif"
    assert result.mapping.proposals == ()


@pytest.mark.parametrize(
    "word",
    [
        "REF",
        "refrigerator",
        "FRIDGE",
        "DW",
        "DISHWASHER",
        "RANGE",
        "OVEN",
        "COOKTOP",
        "MW",
        "MICROWAVE",
        "W/D",
        "WASHER",
        "DRYER",
        "ICE",
        "WINE",
    ],
)
def test_vendor_appliance_word_inside_a_slot_span_holds_the_whole_row(word: str) -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, word)))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is not None and result.row_hold.code == "appliance-space"
    assert result.row_hold.reason == (
        "this row includes an appliance space; it may be the wall-to-wall line, not the countertop"
    )
    assert result.mapping.proposals == ()
    assert result.mapping.held


def test_appliance_word_in_a_slot_label_holds_the_whole_row() -> None:
    page = slot_page(sheets.sheet(sheets.text_labels(('12"', '24" RANGE', '12"'), OVERALL)))
    lookup = crops_to_texts(page, TEXTS | {1: '24" RANGE'})
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is not None and result.row_hold.code == "appliance-space"
    assert result.mapping.proposals == ()


def test_a_gv_appliance_word_does_not_hold_an_otherwise_sealed_vendor_row() -> None:
    page = slot_page(
        sheets.sheet(
            text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "RANGE", colour="1 0 0 rg")
        )
    )
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_a_substring_is_not_an_appliance_word() -> None:
    page = slot_page(sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "NICE")))
    lookup = crops_to_texts(page, TEXTS)
    result = read(page, FakeReaders(lambda _model, png: lookup[png]))

    assert result.row_hold is None
    assert len(result.mapping.proposals) == 4


def test_other_words_still_go_to_the_person() -> None:
    result = agreed(glyph_page(), TEXTS | {0: '12" Panel'})
    assert result.slots[0].outcome.reason_code == "not-plain"
    assert result.mapping.proposals == ()


def test_walls_are_asked_once_per_row_of_both_readers_and_seal_on_agreement() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(lambda _model, png: lookup[png], walls=lambda _model: BOTH_WALLS)

    result = read(page, readers)

    asked = [model for model, _row, _view in readers.wall_requests]
    assert len(asked) == 2 and set(asked) == {KIMI, QWEN}
    row_png, view_png = readers.wall_requests[0][1], readers.wall_requests[0][2]
    assert row_png.startswith(b"\x89PNG") and view_png.startswith(b"\x89PNG")
    assert result.walls is not None
    assert result.walls.outcome.config == "back_left_right"
    assert len(result.walls.answers) == 2


def test_walls_the_readers_do_not_agree_on_go_to_the_person() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    readers = FakeReaders(
        lambda _model, png: lookup[png],
        walls=lambda model: BOTH_WALLS if model == QWEN else UNSURE_WALLS,
    )
    result = read(page, readers)
    assert result.walls is not None and result.walls.outcome.config is None
    assert result.walls.outcome.reason


def test_the_wall_answer_never_changes_a_reading_or_a_proposal() -> None:
    page = glyph_page()
    lookup = crops_to_texts(page, TEXTS)
    sealed = read(page, FakeReaders(lambda _m, png: lookup[png], walls=lambda _m: BOTH_WALLS))
    unsure = read(page, FakeReaders(lambda _m, png: lookup[png]))
    assert sealed.mapping == unsure.mapping
    assert [slot.outcome for slot in sealed.slots] == [slot.outcome for slot in unsure.slots]


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


def _scaffold(session: Any) -> tuple[Any, Any, Any, Any]:
    """A revision with one shop page and an extraction run: (revision, version, page, run)."""
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
    from app.models.runs import ExtractionRun, TaskRun, WorkflowRun

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
    return revision, version, page_row, run


def _read_persisted(
    session: Any, data: bytes, readers: Callable[[SlotPage], FakeReaders], **persist: Any
) -> tuple[Any, Any, PageSlotResult, int]:
    from workflow.slot_reader import persist_slot_readings

    revision, version, page_row, run = _scaffold(session)
    page = slot_page(data)
    page = SlotPage(
        page_index=0,
        page_id=page_row.id,
        document_version_id=version.id,
        rendered=page.rendered,
        rows=page.rows,
        ink=page.ink,
    )
    (result,) = read_slot_pages(
        [page], runtime=runtime(readers(page)), record_attempt=lambda _a: None
    )
    count = persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(KIMI, QWEN),
        results=[result],
        **persist,
    )
    return revision, run, result, count


def _by_slot(session: Any, run: Any) -> dict[str, Any]:
    from sqlalchemy import select

    from app.models.evidence import ObservationCandidate

    rows = session.scalars(
        select(ObservationCandidate).where(ObservationCandidate.extraction_run_id == run.id)
    ).all()
    found: dict[str, Any] = {}
    for row in rows:
        if "wall-reader" in row.ambiguity_flags:
            found["walls"] = row
            continue
        found[next(flag for flag in row.ambiguity_flags if flag.startswith("slot:"))] = row
    return found


def test_appliance_row_persists_the_reason_but_no_form_proposals(session: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, result, _count = _read_persisted(
        session,
        sheets.sheet(text_labels() + sheets.text(230, sheets.CHAIN_Y - 40, "RANGE")),
        readers,
    )

    assert result.row_hold is not None and result.row_hold.code == "appliance-space"
    for candidate in _by_slot(session, run).values():
        if "wall-reader" not in candidate.ambiguity_flags:
            assert "row-hold:appliance-space" in candidate.ambiguity_flags
            assert candidate.review_reason == result.row_hold.reason
    assert (
        session.scalars(
            select(MeasurementProposal).where(
                MeasurementProposal.package_revision_id == revision.id
            )
        ).all()
        == []
    )


def test_persisted_candidates_carry_what_the_screen_needs_and_only_offered_ones_are_linked(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, _result, count = _read_persisted(
        session, named_sheet(sheets.yellow_box(241, sheets.CHAIN_Y + 3, 13, 7)), readers
    )

    assert count == 5  # three slots, the overall and the row's walls
    by_slot = _by_slot(session, run)
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
    assert overall.corroboration_status is None and overall.value_numerator == 48
    assert overall.review_reason is not None and overall.review_reason.startswith("held back")

    proposals = session.scalars(
        select(MeasurementProposal).where(MeasurementProposal.package_revision_id == revision.id)
    ).all()
    assert proposals == [], "a held chain links nothing to the form"


def test_a_fully_sealed_row_links_every_piece_in_order_and_a_veto_is_kept_as_a_suggestion(
    session: Any,
) -> None:
    from sqlalchemy import select

    from app.models.evidence import MeasurementProposal

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, _result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), readers
    )
    by_slot = _by_slot(session, run)
    links = session.scalars(
        select(MeasurementProposal)
        .where(
            MeasurementProposal.package_revision_id == revision.id,
            MeasurementProposal.field_key == PIECE_FIELD,
        )
        .order_by(MeasurementProposal.position)
    ).all()
    assert [link.candidate_id for link in links] == [
        by_slot["slot:0"].id,
        by_slot["slot:1"].id,
        by_slot["slot:2"].id,
    ]

    def misreaders(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS | {2: '120"'})
        return FakeReaders(lambda _model, png: lookup[png])

    revision, run, _result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), misreaders
    )
    vetoed = _by_slot(session, run)["slot:2"]
    assert "drawn-length" in vetoed.ambiguity_flags
    assert vetoed.review_reason == "doesn't match the drawn length"
    assert vetoed.corroboration_status is None
    assert vetoed.value_numerator == 120, "the agreed text stays, as a suggestion for the person"
    assert (
        session.scalars(
            select(MeasurementProposal).where(
                MeasurementProposal.package_revision_id == revision.id
            )
        ).all()
        == []
    )


def test_a_sealed_wall_layout_is_kept_with_its_pictures_and_proposed(
    session: Any, tmp_path: Any
) -> None:
    from sqlalchemy import select

    from app.models.evidence import EvidenceArtifact, LayoutProposal
    from storage.local import LocalStore
    from workflow.layout_proposals import reader_sealed_wall_config

    store = LocalStore(tmp_path / "artifacts")

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(lambda _model, png: lookup[png], walls=lambda _model: BOTH_WALLS)

    revision, run, result, _count = _read_persisted(
        session, sheets.sheet(sheets.glyph_labels()), readers, store=store
    )

    walls = _by_slot(session, run)["walls"]
    assert "walls-sealed:back_left_right" in walls.ambiguity_flags
    assert sum(flag.startswith("wall-reader:") for flag in walls.ambiguity_flags) == 2
    assert walls.value_numerator is None, "a wall answer is never a value"
    artifacts = session.scalars(
        select(EvidenceArtifact).where(EvidenceArtifact.candidate_id == walls.id)
    ).all()
    assert len(artifacts) == 2
    assert result.walls is not None
    stored = {store.get(artifact.storage_key).read() for artifact in artifacts}
    assert stored == {result.walls.row_png, result.walls.view_png}, "exactly what the readers saw"
    proposal = session.scalars(
        select(LayoutProposal).where(LayoutProposal.package_revision_id == revision.id)
    ).one()
    assert proposal.discriminator_name == "wall_config"
    assert proposal.proposed_value == "back_left_right"
    assert proposal.prompt_id == "slot-walls-v1"
    assert proposal.model_id == f"{KIMI} + {QWEN}"
    sealed = reader_sealed_wall_config(session, revision.id)
    assert sealed is not None and sealed.value == "back_left_right"


def test_walls_the_readers_do_not_settle_propose_nothing(session: Any, tmp_path: Any) -> None:
    from sqlalchemy import select

    from app.models.evidence import LayoutProposal
    from storage.local import LocalStore
    from workflow.layout_proposals import reader_sealed_wall_config

    def readers(page: SlotPage) -> FakeReaders:
        lookup = crops_to_texts(page, TEXTS)
        return FakeReaders(
            lambda _model, png: lookup[png],
            walls=lambda model: BOTH_WALLS if model == QWEN else UNSURE_WALLS,
        )

    revision, run, _result, _count = _read_persisted(
        session,
        sheets.sheet(sheets.glyph_labels()),
        readers,
        store=LocalStore(tmp_path / "artifacts"),
    )

    walls = _by_slot(session, run)["walls"]
    assert any(flag.startswith("walls-held:") for flag in walls.ambiguity_flags)
    assert walls.review_reason
    assert (
        session.scalars(
            select(LayoutProposal).where(LayoutProposal.package_revision_id == revision.id)
        ).all()
        == []
    )
    assert reader_sealed_wall_config(session, revision.id) is None
