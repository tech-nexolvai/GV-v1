"""Architect pairings stored append-only, and the read path the rule uses (#1053).

Verification for `persist_architect_pairings`, `latest_architect_pairing`,
`record_reviewer_pairing` and migration `0076_architect_pairing_records`. Invented values only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import DrawingView, ViewRole
from app.models.evidence import ArchitectPairingRecord, ObservationCandidate
from app.models.runs import ExtractionRun, ModelInvocation
from extraction.slot_reader.bedrock import ARCH_PAIR_PROMPT_ID, ArchPairAnswer, CropJob
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_architect_pairing import (
    OPUS,
    SONNET,
    WIDTHS,
    _result,
    _sealed_owners,
    _slot_page,
    arch_row,
)
from tests.workflow.test_slot_reader import _scaffold
from workflow.architect_pairing import (
    MEASURED_PAIRING_SETTINGS,
    ArchitectPageInput,
    ArchitectPairing,
    ArchitectRowInput,
    DecidedPair,
    ReviewerPairingRefused,
    architect_candidate_ids,
    latest_architect_pairing,
    persist_architect_pairings,
    record_reviewer_pairing,
)
from workflow.architect_pairing_contract import EffectivePair
from workflow.architect_reader import ARCHITECT_EXTRACTOR, ARCHITECT_EXTRACTOR_VERSION
from workflow.view_roles import confirm_view_role

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


class Stored:
    """A revision, its slot-reader row anchor, and the architect's stored spans on the same page."""

    def __init__(
        self,
        session: Session,
        row: ArchitectRowInput,
        *,
        held: Mapping[int, str] | None = None,
        outline: Sequence[str] | None = None,
    ) -> None:
        self.revision, self.version, self.page, self.run = _scaffold(session)
        self.anchor = ObservationCandidate(
            document_version_id=self.version.id,
            page_id=self.page.id,
            extraction_run_id=self.run.id,
            raw_text='30"',
            polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
            coordinate_space="image",
            ambiguity_flags=["slot-reader", "slot:0", "row-rank:1", "row-slot-count:3"],
        )
        session.add(self.anchor)
        self.architect_run = ExtractionRun(
            task_run_id=self.run.task_run_id,
            extractor=ARCHITECT_EXTRACTOR,
            extractor_version=ARCHITECT_EXTRACTOR_VERSION,
            config_hash="test",
            dpi=150,
        )
        session.add(self.architect_run)
        session.flush()
        view = DrawingView(
            page_id=self.page.id,
            tag="panel-1",
            region={"space": "stored", "points": [[0, 0], [1, 0], [1, 1], [0, 1]]},
        )
        session.add(view)
        session.flush()
        confirm_view_role(session, view=view, role=ViewRole.ARCH, actor="reviewer-1")
        self.view = view
        holds = held or {}
        outlines = list(outline) if outline is not None else ["yes"] * len(row.spans)
        self.candidates: list[ObservationCandidate] = []
        for span in row.spans:
            width = int((span.x1_pt - span.x0_pt) / 3 * 2)
            candidate = ObservationCandidate(
                document_version_id=self.version.id,
                page_id=self.page.id,
                extraction_run_id=self.architect_run.id,
                raw_text=f"{width} in",
                value_numerator=None if span.index in holds else width,
                value_denominator=None if span.index in holds else 1,
                unit=None if span.index in holds else "in",
                polygon=[[0, 0], [1, 0], [1, 1], [0, 1]],
                coordinate_space="image",
                ambiguity_flags=[
                    "architect-reader",
                    "arch-view:1",
                    f"arch-row:{row.rank}",
                    f"arch-slot:{span.index}",
                    f"arch-ticks-on-outline:{outlines[span.index]}",
                    *([f"arch-held:{holds[span.index]}"] if span.index in holds else []),
                ],
            )
            session.add(candidate)
            self.candidates.append(candidate)
        session.flush()
        ids = architect_candidate_ids(session, self.architect_run.id)[self.page.id]
        self.row = replace(
            row,
            spans=tuple(
                replace(
                    span,
                    candidate_id=ids[(1, row.rank, span.index)],
                    on_outline={"yes": True, "no": False}.get(outlines[span.index]),
                    held_reason=holds.get(span.index),
                )
                for span in row.spans
            ),
        )
        self.input = ArchitectPageInput(
            page_id=self.page.id,
            architect_run_id=self.architect_run.id,
            rows=(self.row,),
            view_boxes=(),
        )

    def pair(
        self,
        session: Session,
        *,
        answers: Mapping[str, ArchPairAnswer | None] | None = None,
    ) -> int:
        result = replace(
            _result(_sealed_owners()),
            page_id=self.page.id,
            owner_candidate_ids={"slot:0": self.anchor.id},
        )
        packets: list[Mapping[str, object]] = []

        def ask(jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
            packets.extend(job.question_packet or {} for job in jobs)
            return {(job.key, job.model_id): (answers or {}).get(job.model_id) for job in jobs}

        (paired,) = ArchitectPairing(MEASURED_PAIRING_SETTINGS, {self.page.id: self.input}).pair(
            [result],
            [_slot_page(self.page.id)],
            ask=ask,
            readers=(OPUS, SONNET),
            ask_the_ais=True,
            store=None,
            effort="high",
        )
        for packet, model in zip(packets, (OPUS, SONNET), strict=False):
            session.add(
                ModelInvocation(
                    extraction_run_id=self.run.id,
                    model_id=model,
                    prompt_id=ARCH_PAIR_PROMPT_ID,
                    template_id=ARCH_PAIR_PROMPT_ID,
                    input_tokens=1,
                    output_tokens=1,
                    latency_ms=1,
                    outcome="ok",
                    reader_page_index=0,
                    reader_attempt_number=1,
                    reader_question_packet=dict(packet),
                )
            )
        return persist_architect_pairings(
            session,
            package_revision_id=self.revision.id,
            extraction_run_id=self.run.id,
            results=[paired],
        )


def _records(session: Session) -> list[ArchitectPairingRecord]:
    return list(
        session.scalars(select(ArchitectPairingRecord).order_by(ArchitectPairingRecord.created_at))
    )


def test_code_pairing_is_stored_once_per_row_and_read_back_exactly(session: Session) -> None:
    stored = Stored(session, arch_row(1, WIDTHS))

    assert stored.pair(session) == 1

    (record,) = _records(session)
    assert (record.source, record.status, record.row_anchor_candidate_id) == (
        "code",
        "paired",
        stored.anchor.id,
    )
    assert record.details["architect_run_id"] == str(stored.architect_run.id)
    effective = latest_architect_pairing(session, stored.anchor.id)
    assert effective is not None
    assert (effective.source, effective.status) == ("code", "paired")
    assert effective.pairs == tuple(
        EffectivePair("piece", candidate.id, (k,)) for k, candidate in enumerate(stored.candidates)
    )


def test_records_cannot_be_edited_or_deleted(session: Session) -> None:
    stored = Stored(session, arch_row(1, WIDTHS))
    stored.pair(session)
    session.commit()

    for statement in (
        "UPDATE architect_pairing_records SET status = 'ambiguous'",
        "DELETE FROM architect_pairing_records",
    ):
        with pytest.raises(DBAPIError):
            session.execute(text(statement))
        session.rollback()


def test_both_ais_pairing_keeps_both_answers_and_their_invocations(session: Session) -> None:
    stored = Stored(session, arch_row(1, (30,)))

    stored.pair(
        session,
        answers={
            OPUS: ArchPairAnswer(OPUS, 0, (1, 0, 0), "A1 is the first cabinet"),
            SONNET: ArchPairAnswer(SONNET, 0, (1, 0, 0), "same cabinet"),
        },
    )

    (record,) = _records(session)
    assert (record.source, record.status) == ("both-ais", "paired")
    ai = record.details["ai"]
    assert isinstance(ai, dict)
    assert [answer["why"] for answer in ai["answers"]] == [
        "A1 is the first cabinet",
        "same cabinet",
    ]
    assert set(ai["invocation_ids"]) == {OPUS, SONNET}
    effective = latest_architect_pairing(session, stored.anchor.id)
    assert effective is not None and effective.source == "both-ais"
    assert effective.pairs == (EffectivePair("piece", stored.candidates[0].id, (0,)),)


def test_disagreeing_ais_leave_the_row_unpaired_for_the_reviewer(session: Session) -> None:
    stored = Stored(session, arch_row(1, (30,)))

    stored.pair(
        session,
        answers={
            OPUS: ArchPairAnswer(OPUS, 0, (1, 0, 0), "x"),
            SONNET: ArchPairAnswer(SONNET, 0, (0, 1, 0), "y"),
        },
    )

    effective = latest_architect_pairing(session, stored.anchor.id)
    assert effective is not None
    assert (effective.source, effective.status, effective.pairs) == ("none", "ais-disagree", ())


def test_a_reviewer_pairing_supersedes_and_wins(session: Session) -> None:
    stored = Stored(session, arch_row(1, (30,)))
    stored.pair(session, answers={OPUS: None, SONNET: None})
    automatic = _records(session)[0]

    first = record_reviewer_pairing(
        session,
        anchor=stored.anchor,
        package_revision_id=stored.revision.id,
        piece_count=3,
        pairs=[DecidedPair("piece", stored.candidates[0].id, (0,))],
        note="the first cabinet",
        actor="reviewer-1",
    )
    session.flush()
    second = record_reviewer_pairing(
        session,
        anchor=stored.anchor,
        package_revision_id=stored.revision.id,
        piece_count=3,
        pairs=[],
        note="nothing comparable after all",
        actor="reviewer-2",
    )
    session.flush()

    assert first.supersedes_id == automatic.id and second.supersedes_id == first.id
    effective = latest_architect_pairing(session, stored.anchor.id)
    assert effective is not None
    assert (effective.record_id, effective.source, effective.status, effective.pairs) == (
        second.id,
        "reviewer",
        "reviewer",
        (),
    )


def test_two_reviewers_superseding_the_same_record_cannot_both_land(session: Session) -> None:
    stored = Stored(session, arch_row(1, (30,)))
    stored.pair(session, answers={OPUS: None, SONNET: None})
    automatic = _records(session)[0]
    session.commit()
    for actor in ("reviewer-1", "reviewer-2"):
        session.add(
            ArchitectPairingRecord(
                package_revision_id=stored.revision.id,
                page_id=stored.page.id,
                row_anchor_candidate_id=stored.anchor.id,
                source="reviewer",
                status="reviewer",
                pairs=[],
                details={},
                supersedes_id=automatic.id,
                decided_by=actor,
            )
        )
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.mark.parametrize(
    ("held", "outline", "why"),
    [
        ({0: "the drawn length disagrees"}, None, "held"),
        (None, ["no"], "centre line"),
        (None, ["unknown"], "outline"),
    ],
)
def test_the_reviewer_cannot_pair_a_held_or_centre_line_span(
    session: Session, held: Mapping[int, str] | None, outline: list[str] | None, why: str
) -> None:
    stored = Stored(session, arch_row(1, (30,)), held=held, outline=outline)
    stored.pair(session)

    with pytest.raises(ReviewerPairingRefused, match=why):
        record_reviewer_pairing(
            session,
            anchor=stored.anchor,
            package_revision_id=stored.revision.id,
            piece_count=3,
            pairs=[DecidedPair("piece", stored.candidates[0].id, (0,))],
            note=None,
            actor="reviewer-1",
        )


@pytest.mark.parametrize(
    ("pairs", "why"),
    [
        (lambda c: [DecidedPair("piece", c[0].id, (0, 2))], "next to each other"),
        (lambda c: [DecidedPair("piece", c[0].id, (3,))], "own pieces"),
        (lambda c: [DecidedPair("overall", c[0].id, (0,))], "whole row"),
        (
            lambda c: [DecidedPair("piece", c[0].id, (0,)), DecidedPair("piece", c[1].id, (0,))],
            "only once",
        ),
        (lambda c: [DecidedPair("piece", uuid4(), (0,))], "not on this row's page"),
    ],
)
def test_the_reviewer_cannot_pair_against_the_rows_shape(
    session: Session, pairs: Any, why: str
) -> None:
    stored = Stored(session, arch_row(1, WIDTHS))
    stored.pair(session)

    with pytest.raises(ReviewerPairingRefused, match=why):
        record_reviewer_pairing(
            session,
            anchor=stored.anchor,
            package_revision_id=stored.revision.id,
            piece_count=3,
            pairs=pairs(stored.candidates),
            note=None,
            actor="reviewer-1",
        )


def test_a_span_from_another_page_is_refused(session: Session) -> None:
    here = Stored(session, arch_row(1, WIDTHS))
    here.pair(session)
    elsewhere = Stored(session, arch_row(1, WIDTHS))

    with pytest.raises(ReviewerPairingRefused, match="page"):
        record_reviewer_pairing(
            session,
            anchor=here.anchor,
            package_revision_id=here.revision.id,
            piece_count=3,
            pairs=[DecidedPair("piece", elsewhere.candidates[0].id, (0,))],
            note=None,
            actor="reviewer-1",
        )


def test_the_read_path_drops_a_pair_whose_drawing_is_no_longer_the_architects(
    session: Session,
) -> None:
    stored = Stored(session, arch_row(1, WIDTHS))
    stored.pair(session)
    confirm_view_role(session, view=stored.view, role=ViewRole.SHOP, actor="reviewer-1")
    session.flush()

    effective = latest_architect_pairing(session, stored.anchor.id)

    assert effective is not None
    assert effective.pairs == ()
    assert any("no longer confirmed" in reason for reason in effective.reasons)


def test_the_read_path_never_returns_a_held_span_even_if_a_record_names_it(
    session: Session,
) -> None:
    stored = Stored(session, arch_row(1, WIDTHS), held={1: "label disagrees"})
    stored.pair(session)
    (record,) = _records(session)
    session.add(
        ArchitectPairingRecord(
            package_revision_id=stored.revision.id,
            page_id=stored.page.id,
            row_anchor_candidate_id=stored.anchor.id,
            source="reviewer",
            status="reviewer",
            pairs=[
                {
                    "kind": "piece",
                    "architect_candidate_id": str(stored.candidates[1].id),
                    "vendor_slot_indices": [1],
                }
            ],
            details={},
            supersedes_id=record.id,
            decided_by="reviewer-1",
        )
    )
    session.flush()

    effective = latest_architect_pairing(session, stored.anchor.id)

    assert effective is not None and effective.source == "reviewer"
    assert effective.pairs == ()


def test_a_row_with_no_record_has_no_pairing(session: Session) -> None:
    stored = Stored(session, arch_row(1, WIDTHS))

    assert latest_architect_pairing(session, stored.anchor.id) is None
    assert latest_architect_pairing(session, UUID(int=0)) is None


# --- the stage hands the pairing exactly what the architect reader stored --------------------------


def test_the_stage_links_every_span_to_the_candidate_stored_for_it(
    session: Session, tmp_path: Any
) -> None:
    from storage.local import LocalStore
    from tests.extraction.architect.combined_sheet import combined_sheet
    from tests.workflow.test_architect_reader import _stages, _upload

    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    stages = _stages(store)
    revision = _upload(session, store, combined_sheet())

    stages.extract_pages(session, revision.id)

    (page_input,) = stages._architect_pages.values()
    stored = {
        candidate.id: candidate
        for candidate in session.scalars(
            select(ObservationCandidate)
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(ExtractionRun.extractor == ARCHITECT_EXTRACTOR)
        )
    }
    linked = [span for row in page_input.rows for span in row.spans if span.candidate_id]
    assert {span.candidate_id for span in linked} == set(stored)
    for row in page_input.rows:
        assert row.view_annotation_index == 1, "only the architect's drawing"
        for span in row.spans:
            if span.candidate_id is None:
                assert span.held_reason is not None, "an unstored span is never comparable"
                continue
            flags = set(stored[span.candidate_id].ambiguity_flags)
            assert f"arch-row:{row.rank}" in flags and f"arch-slot:{span.index}" in flags
            outline = {True: "yes", False: "no", None: "unknown"}[span.on_outline]
            assert f"arch-ticks-on-outline:{outline}" in flags
            assert (span.held_reason is None) == (
                stored[span.candidate_id].value_numerator is not None
            )
    assert any(span.comparable for span in linked)


def _slot_stages(store: Any, *, architect: bool) -> Any:
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
    from tests.extraction.test_reader import MISSING_SPACE
    from tests.workflow.test_markup_route import _SilentOcr
    from tests.workflow.test_slot_reader import FakeReaders, runtime
    from workflow.stages import DatabaseStages

    slot_runtime = runtime(FakeReaders(lambda _model, _png: ""))
    return DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),
        missing_space=MISSING_SPACE,
        form_reader=slot_runtime.form,
        slot_reader=slot_runtime,
        architect_reader=MEASURED_ARCHITECT_SETTINGS if architect else None,
    )


@pytest.mark.parametrize("architect", [False, True])
def test_the_slot_reader_pairs_only_beside_the_architect_reader(
    session: Session, tmp_path: Any, monkeypatch: pytest.MonkeyPatch, architect: bool
) -> None:
    from storage.local import LocalStore

    revision, _version, page, run = _scaffold(session)
    store = LocalStore(root=tmp_path, ticket_secret=b"a secret only this test knows")
    stages = _slot_stages(store, architect=architect)
    stages._architect_pages = {
        page.id: ArchitectPageInput(page_id=page.id, architect_run_id=None, rows=(), view_boxes=())
    }
    seen: dict[str, object] = {}

    def capture(pages: Sequence[object], **options: object) -> tuple[()]:
        seen.update(options)
        return ()

    monkeypatch.setattr("workflow.stages.read_slot_pages", capture)

    stages._read_slots(
        session, package_revision_id=revision.id, pages=[], task_run_id=run.task_run_id
    )

    handed = seen["architect"]
    if architect:
        assert isinstance(handed, ArchitectPairing)
        assert handed.settings == MEASURED_PAIRING_SETTINGS
        assert set(handed.pages) == {page.id}
    else:
        assert handed is None
    newest = session.scalars(
        select(ExtractionRun).order_by(ExtractionRun.created_at.desc()).limit(1)
    ).one()
    plain = stages._slot_reader.config_hash
    assert (newest.config_hash != f"dpi=150;{plain}") is architect, "the run says it paired"
    assert len(newest.config_hash) <= 200
