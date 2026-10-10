"""The architect's own file indexed view by view, and the matcher wired into the reading (#1166).

Verification for `workflow/architect_view_index.record_architect_view_index` as the extraction stage
calls it, and for `DatabaseStages._read_slots` building the matcher (`ArchitectPairing(matcher=...)`)
only when the architect's file was indexed. Combined sheets keep today's path: no index row, no
matcher, no match question, no match record. Invented sheets only; no client value.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    DocumentVersion,
    DrawingView,
    ExtractionRun,
    PackageRevision,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import (
    ArchitectViewIndexEntry,
    ArchitectViewMatchRecord,
    ObservationCandidate,
)
from app.models.runs import ModelInvocation
from extraction.slot_reader.bedrock import ARCH_MATCH_PROMPT_ID, ArchMatchAnswer, CropJob
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.architect.architect_sheet import architect_sheet
from tests.extraction.architect.combined_sheet import combined_sheet
from tests.workflow.test_architect_file_reader import _add_architectural, _page
from tests.workflow.test_architect_matching import result as matched_result
from tests.workflow.test_architect_pairing import _slot_page
from workflow.architect_matching import ArchitectMatcher
from workflow.architect_pairing import ArchitectPairing

pytest_plugins = ("tests.app.postgres_fixture",)


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


def _extract(
    session: Session, store: LocalStore, *, architect: bytes | None, same_file: bool = False
) -> tuple[Any, PackageRevision, DocumentVersion | None]:
    from tests.workflow.test_architect_reader import _stages, _upload

    shop = combined_sheet()
    revision = _upload(session, store, shop)
    version = None
    if architect is not None:
        version = _add_architectural(session, store, revision, architect)
    if same_file:
        version = _add_architectural(session, store, revision, shop)
    stages = _stages(store)
    stages.extract_pages(session, revision.id)
    session.commit()
    return stages, revision, version


def _index(session: Session) -> list[ArchitectViewIndexEntry]:
    return list(
        session.scalars(
            select(ArchitectViewIndexEntry).order_by(
                ArchitectViewIndexEntry.page_id, ArchitectViewIndexEntry.view_number
            )
        )
    )


def test_every_view_of_the_architects_file_is_indexed_with_its_picture(
    session: Session, store: LocalStore
) -> None:
    stages, _revision, version = _extract(session, store, architect=architect_sheet())
    assert version is not None

    (entry,) = _index(session)
    page = _page(session, version)
    (drawing_view,) = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id))
    assert (entry.document_version_id, entry.page_id) == (version.id, page.id)
    assert (entry.view_number, entry.view_tag, entry.drawing_view_id) == (
        1,
        "view-1",
        drawing_view.id,
    )
    assert entry.sheet_number == "X-101"
    assert entry.title == "SYNTHETIC ELEVATION"
    assert entry.bubble is not None and entry.bubble.startswith("3")
    assert entry.scale_note is not None
    assert entry.scale_note.replace("\u2019", "'") == '1/4" = 1\'-0"'
    assert entry.points_per_inch == "1.5"
    assert entry.separated and entry.role_confirmed and entry.row_count >= 1
    assert entry.extent["frame"] == "pdfplumber-points" and len(entry.extent["stored_points"]) == 4
    assert entry.picture_storage_key is not None and entry.picture_sha256 is not None
    assert entry.picture_storage_key.startswith(f"architect-views/{version.id}/pages/0/view-1-")
    with store.get(entry.picture_storage_key) as stored:
        assert hashlib.sha256(stored.read()).hexdigest() == entry.picture_sha256
    (crop,) = stages._architect_view_crops.values()
    assert crop.view_id == entry.id and crop.png is not None
    assert crop.indexed.facts.key == str(entry.id) and crop.indexed.facts.rows


def test_views_not_clearly_apart_are_indexed_and_say_so(
    session: Session, store: LocalStore
) -> None:
    _extract(session, store, architect=architect_sheet(views=2, crowded=True))

    entries = _index(session)
    assert [entry.view_tag for entry in entries] == ["view-1", "view-2"]
    assert not any(entry.separated or entry.role_confirmed for entry in entries)
    assert all("not clearly apart" in entry.reason for entry in entries)


@pytest.mark.parametrize("same_file", [False, True], ids=["combined-sheet", "same-file-twice"])
def test_a_combined_sheet_indexes_nothing(
    session: Session, store: LocalStore, same_file: bool
) -> None:
    stages, _revision, _version = _extract(session, store, architect=None, same_file=same_file)

    assert _index(session) == [] and stages._architect_view_crops == {}


def test_a_redelivered_stage_writes_its_index_once(session: Session, store: LocalStore) -> None:
    stages, revision, _version = _extract(session, store, architect=architect_sheet())
    first = _index(session)
    stages.extract_pages(session, revision.id)
    session.commit()

    assert [entry.id for entry in _index(session)] == [entry.id for entry in first]
    (crop,) = stages._architect_view_crops.values()
    assert crop.view_id == first[0].id


# --- the reading builds the matcher only beside an indexed architect file ---------------------------


def _slot_stages_like(stages: Any, store: LocalStore) -> Any:
    from tests.workflow.test_architect_pairing_records import _slot_stages

    slot = _slot_stages(store, architect=True)
    slot._architect_pages = dict(stages._architect_pages)
    slot._architect_view_crops = dict(stages._architect_view_crops)
    return slot


def _read(
    session: Session,
    store: LocalStore,
    monkeypatch: pytest.MonkeyPatch,
    stages: Any,
    revision: PackageRevision,
    *,
    vendor_page: bool,
    answer: Any = None,
    readers: tuple[str, ...] = ("reader",),
) -> tuple[Any, list[CropJob], ExtractionRun]:
    shop_page = session.scalars(
        select(Page)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .where(
            DocumentVersion.sha256 == hashlib.sha256(combined_sheet()).hexdigest(),
            PackageRevisionDocument.package_revision_id == revision.id,
        )
    ).one()
    run = session.scalars(select(ExtractionRun).limit(1)).one()
    anchor = ObservationCandidate(
        document_version_id=shop_page.document_version_id,
        page_id=shop_page.id,
        extraction_run_id=run.id,
        raw_text='30"',
        polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
        coordinate_space="image",
        ambiguity_flags=["slot-reader", "slot:0"],
    )
    session.add(anchor)
    session.flush()
    slot = _slot_stages_like(stages, store)
    if not vendor_page and shop_page.id in slot._architect_pages:
        # Without the page handed over, the row stands for a vendor sheet with no architect drawing
        # of its own: one rule for the pairing step and the matcher (#1167).
        slot._architect_pages[shop_page.id] = replace(
            slot._architect_pages[shop_page.id], rows=(), confirmed_views=frozenset()
        )
    seen: dict[str, Any] = {}
    asked: list[CropJob] = []

    def read(pages: Sequence[Any], **options: Any) -> tuple[Any, ...]:
        seen.update(options)
        row = replace(
            matched_result(page_id=shop_page.id), owner_candidate_ids={"slot:0": anchor.id}
        )

        def ask(jobs: Sequence[CropJob]) -> dict[tuple[str, str], object]:
            asked.extend(jobs)
            return {} if answer is None else {(job.key, job.model_id): answer(job) for job in jobs}

        architect = options["architect"]
        if architect is None:
            return (row,)
        return architect.pair(
            [row],
            pages,
            ask=ask,
            readers=readers,
            ask_the_ais=bool(pages),
            store=None,
            effort=None,
        )

    monkeypatch.setattr("workflow.stages.read_slot_pages", read)
    monkeypatch.setattr("workflow.stages.persist_slot_readings", lambda *_a, **_k: 0)
    slot._read_slots(
        session,
        package_revision_id=revision.id,
        pages=[_slot_page(shop_page.id)] if vendor_page else [],
        task_run_id=run.task_run_id,
        data=combined_sheet(),
    )
    session.flush()
    newest = session.scalars(
        select(ExtractionRun).order_by(ExtractionRun.created_at.desc()).limit(1)
    ).one()
    return seen["architect"], asked, newest


def _match_rows(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(ArchitectViewMatchRecord)) or 0


def test_beside_an_indexed_architect_file_every_row_gets_one_match_record(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages, revision, _version = _extract(session, store, architect=architect_sheet())

    handed, _asked, run = _read(session, store, monkeypatch, stages, revision, vendor_page=False)

    assert isinstance(handed, ArchitectPairing)
    assert isinstance(handed.matcher, ArchitectMatcher)
    assert len(handed.matcher.views) == 1
    assert ARCH_MATCH_PROMPT_ID not in run.config_hash, "a digest, but a different one"
    (record,) = session.scalars(select(ArchitectViewMatchRecord)).all()
    assert record.source == "automatic" and record.status == "needs_reviewer"
    assert record.reasons, "never silent"
    assert [candidate["view_id"] for candidate in record.candidates] == [
        str(entry.id) for entry in _index(session)
    ]


def test_a_combined_sheet_never_invokes_the_matcher(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages, revision, _version = _extract(session, store, architect=None)
    invocations = session.scalar(select(func.count()).select_from(ModelInvocation))

    handed, asked, _run = _read(session, store, monkeypatch, stages, revision, vendor_page=True)

    assert type(handed) is ArchitectPairing
    assert _match_rows(session) == 0 and _index(session) == []
    assert not any(job.arch_match_question for job in asked)
    assert session.scalar(select(func.count()).select_from(ModelInvocation)) == invocations


def test_a_row_on_a_page_with_its_own_architect_drawing_is_never_matched(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A combined sheet uploaded beside a separate architect file: the row's own page holds the
    architect's drawing, so today's same-sheet path stands and nothing is matched."""
    stages, revision, _version = _extract(session, store, architect=architect_sheet())

    handed, asked, _run = _read(session, store, monkeypatch, stages, revision, vendor_page=True)

    assert isinstance(handed, ArchitectPairing)
    assert _match_rows(session) == 0
    assert not any(job.arch_match_question for job in asked)


def test_the_run_identity_says_the_matcher_ran(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages, revision, _version = _extract(session, store, architect=architect_sheet())
    plain_stages, plain_revision, _ = _extract(session, store, architect=None)

    _handed, _asked, matched_run = _read(
        session, store, monkeypatch, stages, revision, vendor_page=False
    )
    _handed, _asked, plain_run = _read(
        session, store, monkeypatch, plain_stages, plain_revision, vendor_page=False
    )

    assert matched_run.config_hash != plain_run.config_hash
    assert len(matched_run.config_hash) <= 200


def test_the_ais_are_asked_through_the_reading_and_their_answers_kept(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review item 9: the AI path end to end through `_read_slots`, with a fake asker, on a vendor
    page that holds no architect drawing of its own (a reviewer said the pasted one is the
    vendor's)."""
    from app.models import ViewRole
    from workflow.view_roles import confirm_view_role

    stages, revision, _version = _extract(session, store, architect=architect_sheet())
    shop_views = session.scalars(
        select(DrawingView)
        .join(Page, Page.id == DrawingView.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == Page.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == revision.id,
            DrawingView.role == ViewRole.ARCH.value,
            DrawingView.tag.like("panel-%"),
        )
    ).all()
    for view in shop_views:
        confirm_view_role(session, view=view, role=ViewRole.SHOP, actor="reviewer@example.com")
    session.commit()
    # The run reads the roles as they stand (#1167: the pairing step and the matcher share the one
    # "own architect view" flag the architect reader computes), so the reading runs again.
    stages.extract_pages(session, revision.id)
    session.commit()
    readers = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")

    def answer(job: CropJob) -> object:
        if not job.arch_match_question:
            return None
        count = job.architect_candidates or 0
        return ArchMatchAnswer(job.model_id, 1, ("yes",) + ("no",) * (count - 1), "drawn alike")

    _handed, asked, _run = _read(
        session,
        store,
        monkeypatch,
        stages,
        revision,
        vendor_page=True,
        answer=answer,
        readers=readers,
    )

    match_jobs = [job for job in asked if job.arch_match_question]
    assert {job.model_id for job in match_jobs} == set(readers)
    assert all(job.question_packet is not None for job in match_jobs)
    (record,) = session.scalars(select(ArchitectViewMatchRecord)).all()
    assert record.status == "needs_reviewer", "code has no pick on this sheet: never automatic"
    assert {pick["model_id"] for pick in record.ai_picks} == set(readers)
    (entry,) = _index(session)
    (candidate,) = record.candidates
    assert candidate["view_id"] == str(entry.id)
    assert sorted(candidate["ai_picked_by"]) == sorted(readers)
    assert "both AIs: yes" in candidate["evidence"]


def test_a_pasted_drawing_and_a_page_view_of_one_number_are_two_index_rows(
    session: Session, store: LocalStore
) -> None:
    """`panel-<n>` and `view-<n>` share a number on one page: each view is its own row, by tag."""
    _extract(session, store, architect=architect_sheet(pasted_with_heading=True))

    entries = _index(session)
    tags = sorted(entry.view_tag for entry in entries)
    assert len(tags) == len(set(tags)) >= 2
    assert {tag.split("-")[0] for tag in tags} == {"panel", "view"}
