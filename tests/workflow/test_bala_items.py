"""Bala suggests each countertop the readers found, and its pieces, and writes no item (#1236).

Verification for `workflow/bala_items.py`, its call at the top of `DatabaseStages.match`, the
Measure page's list of Bala's suggestions (`app/evidence/parts.py`) and migration 0095's
`part_proposal_rows`.

**Every value here is synthetic.** Rows are slot-reader records as `workflow/slot_reader.py` writes
them: one reading per piece (`slot:<i>`) and one for the overall (`slot:overall`), each with its
`slot-box:` in the page picture's pixels at the run's 150 dpi, on a 612 x 792 pt page (1275 x 1650
pixels). The real-sheet picture and part-finder case uses `tests/workflow/test_part_pictures.py`'s
hand-built sheet.

**The outcome that matters most is a count of zero:** however many suggestions Bala files,
`drawing_items` and `item_identifiers` stay empty. Only a person's confirmation makes an item.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator, Sequence
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.evidence.parts import confirm_listed_part, revision_parts
from app.models import (
    Document,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ItemIdentifier,
    ObservationCandidate,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Page,
    PartConfirmation,
    PartPicture,
    PartProposal,
    PartProposalRow,
    Project,
    SourceArtifact,
)
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from storage.local import LocalStore
from tests.workflow.test_association import SETTINGS
from tests.workflow.test_part_pictures import _extract, _stages
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from workflow.bala_items import (
    BALA_SOURCE,
    BALA_VERSION,
    FROM_THE_COUNTERTOP_LINE,
    plan_items,
    propose_items,
)
from workflow.part_pictures import CUT_PART_PICTURES_WORKFLOW
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

WIDTH_PX = 1275
HEIGHT_PX = 1650
#: The drawing every synthetic row sits in: most of the page.
REGION: dict[str, object] = {
    "space": "stored",
    "points": [["0.05", "0.05"], ["0.95", "0.05"], ["0.95", "0.95"], ["0.05", "0.95"]],
}


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


# -- a synthetic revision with slot-reader rows ---------------------------------------------------


class _Revision:
    def __init__(self, session: Session, *, role: str | None = "shop", pages: int = 2) -> None:
        self.session = session
        project = Project(name="bala test")
        session.add(project)
        session.flush()
        package = Package(project_id=project.id, vendor="Synthetic vendor")
        session.add(package)
        session.flush()
        self.revision = PackageRevision(package_id=package.id, revision_number=1, state="MATCHING")
        source = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="b" * 64, size=1)
        document = Document(package_id=package.id, kind="shop")
        session.add_all((self.revision, source, document))
        session.flush()
        self.version = DocumentVersion(
            document_id=document.id, source_artifact_id=source.id, sha256="b" * 64, page_count=pages
        )
        session.add(self.version)
        session.flush()
        session.add(
            PackageRevisionDocument(
                package_revision_id=self.revision.id,
                package_id=package.id,
                document_id=document.id,
                document_version_id=self.version.id,
            )
        )
        self.pages: list[Page] = []
        self.views: list[DrawingView] = []
        for index in range(pages):
            page = Page(
                document_version_id=self.version.id,
                index=index,
                content_hash=f"{index + 7:064x}",
                width_pt=Decimal(612),
                height_pt=Decimal(792),
                rotation=0,
                has_vector_text=False,
                media_box=["0", "0", "612", "792"],
                crop_box=["0", "0", "612", "792"],
            )
            session.add(page)
            session.flush()
            view = DrawingView(page_id=page.id, tag="panel-1", region=REGION, role=role)
            session.add(view)
            self.pages.append(page)
            self.views.append(view)
        self.workflow = WorkflowRun(
            package_revision_id=self.revision.id, engine_run_id=str(uuid4())
        )
        session.add(self.workflow)
        session.flush()

    def run(self) -> ExtractionRun:
        """A new slot-reader run, as a re-read makes."""
        task = TaskRun(
            workflow_run_id=self.workflow.id,
            idempotency_key=str(uuid4()),
            task_type="extract",
            attempt=1,
            outcome="SUCCEEDED",
        )
        self.session.add(task)
        self.session.flush()
        run = ExtractionRun(
            task_run_id=task.id,
            extractor="extraction.form_reader",
            extractor_version="slot-reader-v1-test",
            config_hash="synthetic-test",
            dpi=150,
        )
        self.session.add(run)
        self.session.flush()
        return run

    def row(
        self,
        run: ExtractionRun,
        *,
        page: int = 0,
        pieces: int = 3,
        overall: bool = True,
        chosen: bool = True,
        extra: Sequence[str] = (),
        kinds: dict[int, tuple[str, str]] | None = None,
        sealed: bool = True,
        shift: int = 0,
        gap: int = 0,
    ) -> UUID:
        """One countertop row; returns its anchor (`slot:0`) reading's id. `shift` moves every box
        that many pixels right, as a re-read at the same resolution may; `gap` leaves that many
        pixels between one piece's box and the next."""
        anchor: UUID | None = None
        slots: list[str] = [str(index) for index in range(pieces)]
        if overall:
            slots.append("overall")
        for slot in slots:
            box = _overall_box(pieces) if slot == "overall" else _piece_box(int(slot))
            step = 0 if slot == "overall" else gap * int(slot)
            box = (box[0] + shift + step, box[1], box[2] + shift + step, box[3])
            kind = (kinds or {}).get(int(slot)) if slot != "overall" else None
            flags = [
                "slot-reader",
                f"slot:{slot}",
                f"slot-box:{box[0]},{box[1]},{box[2]},{box[3]}",
                "row-rank:1",
                f"row-slot-count:{pieces}",
                *(["row-choice:1"] if chosen else []),
                *(() if kind is None else (f"kind:{kind[0]}", f"kind-evidence:{kind[1]}")),
                *extra,
            ]
            candidate = ObservationCandidate(
                document_version_id=self.version.id,
                page_id=self.pages[page].id,
                extraction_run_id=run.id,
                raw_text="20",
                value_numerator=20 if sealed else None,
                value_denominator=1 if sealed else None,
                unit="in" if sealed else None,
                polygon=[[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]],
                coordinate_space="image",
                ambiguity_flags=flags,
                corroboration_status="CORROBORATED" if sealed else None,
                corroboration_lane="SECOND_READER" if sealed else None,
                review_reason=(
                    "a synthetic hold" if any(f.startswith("row-hold:") for f in extra) else None
                ),
            )
            self.session.add(candidate)
            self.session.flush()
            if slot == "0":
                anchor = candidate.id
        assert anchor is not None
        return anchor

    def split_page(self, run: ExtractionRun, *, page: int = 1) -> None:
        """A page whose row the two readers did not agree on: only its row-choice record."""
        self.session.add(
            ObservationCandidate(
                document_version_id=self.version.id,
                page_id=self.pages[page].id,
                extraction_run_id=run.id,
                raw_text="",
                polygon=[[0, 0], [WIDTH_PX, 0], [WIDTH_PX, HEIGHT_PX], [0, HEIGHT_PX]],
                coordinate_space="image",
                ambiguity_flags=[
                    "slot-reader-row-choice",
                    "row-choice:0",
                    "row-pick:opus:1",
                    "row-pick:sonnet:2",
                ],
                review_reason="The readers named different rows; the reviewer chooses.",
            )
        )
        self.session.flush()


def _piece_box(index: int) -> tuple[int, int, int, int]:
    left = 200 + 200 * index
    return left, 800, left + 200, 860


def _overall_box(pieces: int) -> tuple[int, int, int, int]:
    return 190, 700, 210 + 200 * pieces, 760


def _stored(box: tuple[int, int, int, int]) -> list[list[str]]:
    left = Decimal(box[0]) / WIDTH_PX
    top = Decimal(box[1]) / HEIGHT_PX
    right = Decimal(box[2]) / WIDTH_PX
    bottom = Decimal(box[3]) / HEIGHT_PX
    return [
        [str(x), str(y)] for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
    ]


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _links(session: Session) -> dict[tuple[UUID, int | None], PartProposal]:
    rows = session.execute(
        select(PartProposalRow, PartProposal).join(
            PartProposal, PartProposal.id == PartProposalRow.part_proposal_id
        )
    ).all()
    return {(link.row_candidate_id, link.slot_index): proposal for link, proposal in rows}


# -- one row: a countertop and its pieces, linked, and no item --------------------------------------


def test_a_row_gives_one_countertop_and_a_part_per_piece_linked_and_no_item(
    session: Session,
) -> None:
    built = _Revision(session)
    run = built.run()
    anchor = built.row(run, pieces=3, kinds={2: ("filler", 'printed word "FILLER"')})

    outcome = propose_items(session, built.revision.id)

    links = _links(session)
    assert sorted(links, key=lambda key: -1 if key[1] is None else key[1]) == [
        (anchor, None),
        (anchor, 0),
        (anchor, 1),
        (anchor, 2),
    ]
    countertop = links[(anchor, None)]
    assert countertop.kind == PartKind.COUNTERTOP.value
    # Its own outline: the box code drew round the printed overall, never the pieces' union.
    assert countertop.extent == {"space": "stored", "points": _stored(_overall_box(3))}
    assert [links[(anchor, index)].kind for index in range(3)] == ["cabinet", "cabinet", "filler"]
    for index in range(3):
        assert links[(anchor, index)].extent == {
            "space": "stored",
            "points": _stored(_piece_box(index)),
        }
    for proposal in links.values():
        assert proposal.source == BALA_SOURCE and proposal.source_version == BALA_VERSION
        assert proposal.reason.startswith(FROM_THE_COUNTERTOP_LINE)
        assert proposal.drawing_view_id == built.views[0].id
        assert proposal.code_as_printed is None
    assert all(
        link.extraction_run_id == run.id for link in session.scalars(select(PartProposalRow))
    )
    assert outcome.written == 4
    # The one outcome that matters most.
    assert _count(session, DrawingItem) == 0
    assert _count(session, ItemIdentifier) == 0


def test_a_piece_nothing_names_is_a_cabinet_and_says_a_person_names_it(session: Session) -> None:
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2, kinds={1: ("appliance_space", 'printed word "DW"')})

    propose_items(session, built.revision.id)

    links = _links(session)
    assert "nothing printed names its kind" in links[(anchor, 0)].reason
    assert links[(anchor, 1)].kind == "cabinet"
    assert "appliance space" in links[(anchor, 1)].reason


def test_a_row_with_no_printed_overall_is_outlined_by_its_own_line_and_says_so(
    session: Session,
) -> None:
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2, overall=False)

    propose_items(session, built.revision.id)

    countertop = _links(session)[(anchor, None)]
    left, top = Decimal(200) / WIDTH_PX, Decimal(800) / HEIGHT_PX
    right, bottom = Decimal(600) / WIDTH_PX, Decimal(860) / HEIGHT_PX
    assert countertop.extent["points"] == [
        [str(x), str(y)] for x, y in ((left, top), (right, top), (right, bottom), (left, bottom))
    ]
    assert "no overall length is printed" in countertop.reason


# -- held, code-chosen and split: still suggested, with the reason --------------------------------


def test_a_held_row_is_still_suggested_with_its_reason(session: Session) -> None:
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2, extra=("row-hold:field-cut",))

    outcome = propose_items(session, built.revision.id)

    links = _links(session)
    assert len(links) == 3
    assert all("Held:" in proposal.reason for proposal in links.values())
    assert outcome.as_payload()["held_or_code_chosen"]
    (row,) = outcome.plan.rows
    assert not row.automatic.countertop
    assert "the row is held" in row.automatic.countertop_why_not
    assert anchor in {key[0] for key in links}


def test_a_row_code_chose_without_the_readers_is_suggested_saying_so(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2, chosen=False)

    outcome = propose_items(session, built.revision.id)

    links = _links(session)
    assert len(links) == 3
    assert all("code did" in proposal.reason for proposal in links.values())
    (row,) = outcome.plan.rows
    assert "the AI readers did not both name this row" in row.automatic.countertop_why_not


def test_a_split_page_has_no_row_to_outline_and_is_listed_with_the_readers_reason(
    session: Session,
) -> None:
    built = _Revision(session)
    run = built.run()
    built.row(run, page=0, pieces=1)
    built.split_page(run, page=1)

    outcome = propose_items(session, built.revision.id)

    payload = outcome.as_payload()
    (listed,) = payload["pages_without_a_row"]  # type: ignore[misc]
    assert listed.startswith("Countertop row on page 2: The two AIs did not agree")
    assert {proposal.drawing_view_id for proposal in _links(session).values()} == {
        built.views[0].id
    }


def test_a_row_no_drawing_holds_is_not_suggested_and_says_why(session: Session) -> None:
    built = _Revision(session)
    built.views[0].region = {
        "space": "stored",
        "points": [["0.6", "0.6"], ["0.9", "0.6"], ["0.9", "0.9"], ["0.6", "0.9"]],
    }
    session.flush()
    built.row(built.run(), pieces=1)

    outcome = propose_items(session, built.revision.id)

    assert _count(session, PartProposal) == 0
    assert outcome.as_payload()["not_proposed"] == [
        "Countertop row on page 1: no drawing on page 1 holds the whole row"
    ]


# -- re-runs ---------------------------------------------------------------------------------------


def test_asking_again_for_the_same_reading_files_nothing_twice(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    propose_items(session, built.revision.id)
    before = {proposal.id for proposal in _links(session).values()}

    again = propose_items(session, built.revision.id)

    assert again.written == 0 and again.found == 3
    assert {proposal.id for proposal in _links(session).values()} == before
    assert _count(session, PartProposal) == 3


def _decide(session: Session, revision: UUID, proposal: PartProposal) -> None:
    confirmed = confirm_listed_part(
        session,
        package_revision_id=revision,
        proposal_id=proposal.id,
        kind=PartKind.CABINET,
        code=None,
        actor="reviewer",
    )
    assert isinstance(confirmed, PartConfirmation)


def _listed(session: Session, revision: UUID) -> set[UUID]:
    return {
        part.proposal.id for drawing in revision_parts(session, revision) for part in drawing.parts
    }


def test_a_new_reading_files_new_suggestions_and_the_list_keeps_only_decided_old_ones(
    session: Session,
) -> None:
    built = _Revision(session)
    first = built.row(built.run(), pieces=2)
    propose_items(session, built.revision.id)
    decided = _links(session)[(first, 0)]
    _decide(session, built.revision.id, decided)

    second = built.row(built.run(), pieces=2)
    outcome = propose_items(session, built.revision.id)

    links = _links(session)
    newest = {links[key].id for key in links if key[0] == second}
    assert _listed(session, built.revision.id) == newest | {decided.id}
    assert outcome.written == len(newest)


def test_a_part_a_person_decided_is_not_suggested_again_after_a_re_read(session: Session) -> None:
    """Review of #1243: the same piece, read again on the same drawing with the same outline, is
    the part the person already decided. Suggesting it again would ask twice, and confirming both
    would make two live items for one cabinet."""
    built = _Revision(session)
    first = built.row(built.run(), pieces=2)
    propose_items(session, built.revision.id)
    decided = _links(session)[(first, 0)]
    _decide(session, built.revision.id, decided)

    second = built.row(built.run(), pieces=2)
    outcome = propose_items(session, built.revision.id)

    links = _links(session)
    assert (second, 0) not in links
    assert {key for key in links if key[0] == second} == {(second, None), (second, 1)}
    assert outcome.written == 2 and outcome.already_decided == 1
    assert outcome.as_payload()["already_decided"] == 1
    assert decided.id in _listed(session, built.revision.id)


def test_the_same_outline_is_found_within_the_tolerance(session: Session) -> None:
    """A re-read may draw every box a pixel to the right: within the finder's witness tolerance it
    is still the outline the person decided."""
    built = _Revision(session)
    first = built.row(built.run(), pieces=1)
    propose_items(session, built.revision.id)
    _decide(session, built.revision.id, _links(session)[(first, 0)])

    second = built.row(built.run(), pieces=1, shift=1)
    outcome = propose_items(session, built.revision.id, same_outline_tolerance=Decimal("0.004"))

    assert (second, 0) not in _links(session) and outcome.already_decided == 1


def test_a_part_outlined_elsewhere_is_suggested_again_even_after_a_decision(
    session: Session,
) -> None:
    """Only the same outline is the same part: a box that moved further than the tolerance is new."""
    built = _Revision(session)
    first = built.row(built.run(), pieces=1)
    propose_items(session, built.revision.id)
    _decide(session, built.revision.id, _links(session)[(first, 0)])

    second = built.row(built.run(), pieces=1, shift=60)
    outcome = propose_items(session, built.revision.id, same_outline_tolerance=Decimal("0.004"))

    assert (second, 0) in _links(session) and outcome.already_decided == 0


def test_a_newer_reading_with_no_suggestions_yet_hides_nothing(session: Session) -> None:
    """Review of #1243: between a new slot-reader run and its match stage (or for ever, if that run
    fails before match), the suggestions already listed stay listed."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    propose_items(session, built.revision.id)
    before = _listed(session, built.revision.id)
    assert len(before) == 3

    built.row(built.run(), pieces=2)  # read, but Bala has not run on it yet

    assert _listed(session, built.revision.id) == before


def test_the_links_cannot_be_changed_or_doubled(session: Session) -> None:
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=1)
    propose_items(session, built.revision.id)
    session.commit()
    link = session.scalars(select(PartProposalRow).where(PartProposalRow.slot_index == 0)).one()

    link.slot_index = 3
    with pytest.raises(IntegrityError, match="append-only"):
        session.flush()
    session.rollback()

    other = session.scalars(select(PartProposal)).first()
    assert other is not None
    session.add(
        PartProposalRow(
            part_proposal_id=other.id,
            row_candidate_id=anchor,
            slot_index=None,
            extraction_run_id=link.extraction_run_id,
            kind_named=True,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


# -- the match stage: Bala first, pictures, and nothing matching sees changes ---------------------


def test_match_suggests_the_items_first_and_writes_no_item(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)

    result = DatabaseStages().match(session, built.revision.id)

    bala = result["bala"]
    assert isinstance(bala, dict)
    assert bala["countertops"] == 1 and bala["pieces"] == 2 and bala["items_written"] == 0
    # The pictures are the worker's next job, not match's (review of #1243).
    assert bala["pictures"] == {"queued": True}
    (job,) = session.scalars(select(OutboxEntry)).all()
    assert job.workflow == CUT_PART_PICTURES_WORKFLOW
    assert job.payload == {"package_revision_id": str(built.revision.id)}
    # Asked again while that job waits: one job, not two.
    DatabaseStages().match(session, built.revision.id)
    assert _count(session, OutboxEntry) == 1
    # Meanwhile the list says each picture is being made.
    parts = [
        part for drawing in revision_parts(session, built.revision.id) for part in drawing.parts
    ]
    assert parts and all(part.picture_pending and not part.has_picture for part in parts)
    # Matching itself saw no item, as before.
    assert result["items"] == 0 and result["candidates"] == 0
    assert _count(session, DrawingItem) == 0


def test_on_a_real_sheet_every_suggestion_gets_its_picture_and_code_agrees_on_the_pieces(
    session: Session, store: LocalStore
) -> None:
    """The sheet of `tests/workflow/test_part_pictures.py`, 400 x 300 pt: two cabinets drawn end to
    end, page x 100..200 and 200..300 pt at 100 pt from the bottom, under an overall at 190 pt. The
    row's boxes are where the reader would have drawn them at 150 dpi (833 x 625 pixels)."""
    revision, _ = _extract(session, store)
    page = session.scalars(select(Page)).one()
    view = session.scalars(select(DrawingView).where(DrawingView.page_id == page.id)).one()
    view.role = "shop"
    task = TaskRun(
        workflow_run_id=session.scalars(select(WorkflowRun)).first().id,  # type: ignore[union-attr]
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
    boxes = {"0": (208, 400, 417, 435), "1": (417, 400, 625, 435), "overall": (208, 210, 625, 250)}
    for slot, box in boxes.items():
        session.add(
            ObservationCandidate(
                document_version_id=page.document_version_id,
                page_id=page.id,
                extraction_run_id=run.id,
                raw_text="",
                polygon=[[box[0], box[1]], [box[2], box[1]], [box[2], box[3]], [box[0], box[3]]],
                coordinate_space="image",
                ambiguity_flags=[
                    "slot-reader",
                    f"slot:{slot}",
                    f"slot-box:{box[0]},{box[1]},{box[2]},{box[3]}",
                    "row-rank:1",
                    "row-slot-count:2",
                    "row-choice:1",
                ],
            )
        )
    session.commit()
    stages = DatabaseStages(
        store=store,
        dpi=150,
        part_pictures=_stages(store)._part_pictures,
        missing_space=_stages(store)._missing_space,
        part_finder=SETTINGS,
    )

    result = stages.match(session, revision.id)

    bala = result["bala"]
    assert isinstance(bala, dict)
    assert bala["written"] == 3 and bala["code_part_finder"] == [], str(bala)
    bala_ids = {proposal.id for proposal in _links(session).values()}
    # Match queues the pictures; the worker's job cuts them, every one.
    assert bala["pictures"] == {"queued": True}
    assert not bala_ids & set(session.scalars(select(PartPicture.part_proposal_id)))
    session.commit()
    assert stages.cut_part_pictures(session, revision.id)["ran"] is True
    pictured = set(session.scalars(select(PartPicture.part_proposal_id)))
    assert bala_ids <= pictured
    (row,) = plan_items(session, revision.id, code_parts=None).rows
    assert row.view_id == view.id
    # Judgment 2, in memory: code's own finder finds a cabinet with each piece's ends.
    planned = plan_items(
        session,
        revision.id,
        code_parts=lambda page: stages._code_parts_on(session, page, _keys(session, revision), {}),
    )
    assert planned.rows[0].automatic.pieces_code_agrees == (True, True)
    assert _count(session, DrawingItem) == 0


def test_a_part_finder_that_fails_is_reported_and_the_match_goes_on(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review of #1243: in PR 1 the finder only reports, so nothing it raises may fail the stage."""
    from workflow import stages as stages_module

    built = _Revision(session)
    built.row(built.run(), pieces=1)

    def store_down(*_arguments: object) -> bytes:
        raise RuntimeError("the store is down")

    monkeypatch.setattr(stages_module, "_fetch", store_down)

    result = DatabaseStages(store=store, part_finder=SETTINGS).match(session, built.revision.id)

    bala = result["bala"]
    assert isinstance(bala, dict) and bala["countertops"] == 1
    assert bala["code_part_finder"] == [
        "code's part finder failed: RuntimeError: the store is down"
    ]


def test_each_link_says_whether_the_drawing_names_the_kind(session: Session) -> None:
    """So the Measure page offers one click only where printed words name the piece (review of
    #1243): the countertop is named by its row; a piece only by a printed kind."""
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2, kinds={1: ("filler", 'printed word "FILLER"')})
    propose_items(session, built.revision.id)

    named = {
        link.slot_index: link.kind_named
        for link in session.scalars(
            select(PartProposalRow).where(PartProposalRow.row_candidate_id == anchor)
        )
    }
    assert named == {None: True, 0: False, 1: True}


def _keys(session: Session, revision: PackageRevision) -> dict[UUID, str]:
    from workflow.stages import _document_records_for

    return {version: key for version, key, _, _ in _document_records_for(session, revision.id)}


def test_without_the_finder_s_settings_its_judgment_is_reported_not_run(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=1)
    stages = DatabaseStages()

    planned = plan_items(
        session,
        built.revision.id,
        code_parts=lambda page: stages._code_parts_on(session, page, {}, {}),
    )

    (row,) = planned.rows
    assert row.code_finder == "code's part finder is off: its settings are not stated"
    assert row.automatic.pieces_code_agrees == (None,)


def test_the_finder_reads_exactly_the_line_work_the_page_layers_hold() -> None:
    """`read_drawing_segments` skips the text regions and fractions, never a line: on the sheet,
    its segments are the layers' own, in the same order."""
    from uuid import uuid4 as fresh

    from extraction.annotations import read_annotation_layers, read_drawing_segments
    from tests.workflow.test_part_proposals_route import SHEET

    version = fresh()
    layers = read_annotation_layers(
        SHEET,
        0,
        document_version_id=version,
        dpi=150,
        line_minimum_pt=SETTINGS.line_minimum_pt,
        glyph_maximum_pt=SETTINGS.glyph_maximum_pt,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        fraction_bar=SETTINGS.fraction_bar,
    )
    segments = read_drawing_segments(
        SHEET, 0, document_version_id=version, dpi=150, line_minimum_pt=SETTINGS.line_minimum_pt
    )
    assert layers.drawing_segments and segments == layers.drawing_segments
