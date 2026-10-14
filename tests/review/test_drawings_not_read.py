"""A vendor drawing whose countertop row was not read is listed, never silent (#1197 phase 1).

V1 reads one countertop row per page. On a sheet holding two vendor drawings (a first-round and a
second-round drawing side by side, or a second vendor drawing a reviewer confirmed), the row read lies
in one of them and the other is not read. Before this it was listed only while it had no role; once
its role was the vendor's it dropped off "Drawings not read" and went silent.

Listed, never blocking: these tests call `drawings_not_read` alone, which adds names to a list and
touches no finding. Which round counts is not decided here (#1197 phase 2 waits for that decision).
Synthetic values only.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import (
    CountertopRunDecision,
    Document,
    DocumentKind,
    DocumentVersion,
    DrawingItem,
    DrawingView,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    Project,
    SourceArtifact,
    ViewRoleProposal,
)
from app.models.document import Page
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from app.review.drawings_not_read import drawings_not_read
from tests.app.postgres_fixture import alembic_config
from vocabulary.part_kinds import PartKind

pytest_plugins = ("tests.app.postgres_fixture",)

#: A US-letter page rendered at the run's 150 dpi is 1275 by 1650 pixels.
DPI = 150
WIDTH_PX, HEIGHT_PX = 1275, 1650

ONE_PER_PAGE = "only one countertop is read per page for now"


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


class Sheet:
    """One synthetic revision: a vendor file of four pages and one slot-reader run."""

    def __init__(self, session: Session) -> None:
        self.session = session
        project = Project(name="drawings not read tests")
        session.add(project)
        session.flush()
        package = Package(
            project_id=project.id, vendor="Synthetic vendor", product_type="countertop"
        )
        session.add(package)
        session.flush()
        self.revision = PackageRevision(
            package_id=package.id, revision_number=1, state="NEEDS_INPUT"
        )
        source = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256="b" * 64, size=1)
        document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
        session.add_all((self.revision, source, document))
        session.flush()
        self.version = DocumentVersion(
            document_id=document.id, source_artifact_id=source.id, sha256="b" * 64, page_count=4
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
        self.pages: dict[int, Page] = {}
        for index in range(4):
            page = Page(
                document_version_id=self.version.id,
                index=index,
                content_hash=f"{index + 500:064x}",
                width_pt=Decimal(612),
                height_pt=Decimal(792),
                rotation=0,
                has_vector_text=True,
                media_box=["0", "0", "612", "792"],
                crop_box=["0", "0", "612", "792"],
            )
            session.add(page)
            self.pages[index + 1] = page
        session.flush()
        self.run = self.new_run()

    def new_run(self, dpi: int | None = DPI) -> UUID:
        workflow_run = WorkflowRun(package_revision_id=self.revision.id, engine_run_id=str(uuid4()))
        self.session.add(workflow_run)
        self.session.flush()
        task = TaskRun(
            workflow_run_id=workflow_run.id,
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
            dpi=dpi,
        )
        self.session.add(run)
        self.session.flush()
        return run.id

    def drawing(
        self,
        page: int,
        tag: str,
        box: tuple[str, str, str, str],
        *,
        role: str | None = None,
        suggested: str | None = None,
        why: str = "synthetic: its own vendor heading",
    ) -> DrawingView:
        """A pasted drawing at `box` (left, top, right, bottom, from 0 to 1 of the page), with a
        role a person confirmed (`role`) and the role its heading suggests (`suggested`)."""
        left, top, right, bottom = box
        view = DrawingView(
            page_id=self.pages[page].id,
            tag=tag,
            region={
                "space": "stored",
                "points": [[left, top], [right, top], [right, bottom], [left, bottom]],
            },
            role=role,
        )
        self.session.add(view)
        self.session.flush()
        self.session.add(
            ViewRoleProposal(
                drawing_view_id=view.id,
                proposed_role=suggested,
                heading=None,
                reason=why,
                source="test",
            )
        )
        self.session.flush()
        return view

    def reviewer_countertop(self, view: DrawingView) -> None:
        """A countertop a reviewer marked by hand in `view`, with a decision on its run: the page is
        then reviewer-owned (`workflow.part_operands.reviewer_owned_pages`), and the check uses the
        reviewer's countertops there, never the row an AI read. A withdrawal is enough to own it."""
        item = DrawingItem(
            drawing_view_id=view.id,
            item_type=PartKind.COUNTERTOP.item_type.value,
            extent=view.region,
        )
        self.session.add(item)
        self.session.flush()
        self.session.add(
            CountertopRunDecision(
                countertop_item_id=item.id, decision="withdrawn", confirmed_by="synthetic reviewer"
            )
        )
        self.session.flush()

    def row(
        self, page: int, at: tuple[float, float], *, run: UUID | None = None, rank: str = "1"
    ) -> None:
        """The countertop row read on `page`: two boxes around `at` (x, y from 0 to 1)."""
        x = int(at[0] * WIDTH_PX)
        y = int(at[1] * HEIGHT_PX)
        for slot, dx in (("0", -40), ("overall", 20)):
            self.session.add(
                ObservationCandidate(
                    document_version_id=self.version.id,
                    page_id=self.pages[page].id,
                    extraction_run_id=run or self.run,
                    raw_text="30 inch",
                    value_numerator=30,
                    value_denominator=1,
                    unit="in",
                    polygon=[
                        [x + dx, y - 8],
                        [x + dx + 20, y - 8],
                        [x + dx + 20, y + 8],
                        [x + dx, y + 8],
                    ],
                    coordinate_space="image",
                    ambiguity_flags=[
                        "slot-reader",
                        f"slot:{slot}",
                        f"row-rank:{rank}",
                        "row-slot-count:1",
                    ],
                )
            )
        self.session.flush()

    def listed(self, read_pages: set[int]) -> list[tuple[int, str, str]]:
        return [
            (drawing.page_number, drawing.drawing, drawing.reason)
            for drawing in drawings_not_read(self.session, self.revision.id, read_pages)
        ]


def _not_read(place: str, read_in: str) -> str:
    return (
        f"The {place} drawing is a vendor drawing that was not read for a countertop: "
        f"{ONE_PER_PAGE}, and the one read is in the {read_in} drawing."
    )


LEFT = ("0.02", "0.1", "0.48", "0.9")
RIGHT = ("0.52", "0.1", "0.98", "0.9")


def test_a_second_vendor_drawing_beside_the_one_read_is_listed_with_the_drawing_read(
    session: Session,
) -> None:
    """Both drawings confirmed as the vendor's; the row read is in the left one. The right one was
    not read, and is listed with the drawing that was."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop", suggested="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop", why="synthetic: a person confirmed it")
    sheet.row(1, (0.25, 0.6))

    assert sheet.listed({1}) == [
        (
            1,
            "the right drawing",
            _not_read("right", "left"),
        )
    ]


def test_the_drawing_listed_is_whichever_one_the_row_read_is_not_in(session: Session) -> None:
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.75, 0.4))

    assert sheet.listed({1}) == [
        (
            1,
            "the left drawing",
            _not_read("left", "right"),
        )
    ]


def test_a_drawing_only_its_heading_says_is_the_vendors_is_listed_too(session: Session) -> None:
    """Nobody confirmed either drawing; both headings say vendor. The reader keeps the rows in a
    drawing its heading says is the vendor's as well (`workflow/stages.py`
    `_architect_candidate_filter_boxes_by_page`), so the same gap."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", ("0.05", "0.02", "0.95", "0.46"), suggested="shop")
    sheet.drawing(1, "panel-2", ("0.05", "0.54", "0.95", "0.98"), suggested="shop")
    sheet.row(1, (0.5, 0.8))

    assert sheet.listed({1}) == [
        (
            1,
            "the upper drawing",
            _not_read("upper", "lower"),
        )
    ]


def test_a_page_with_one_vendor_drawing_or_the_other_the_architects_lists_nothing(
    session: Session,
) -> None:
    sheet = Sheet(session)
    # Page 1: the vendor's drawing alone.
    sheet.drawing(1, "panel-1", ("0.05", "0.05", "0.95", "0.95"), role="shop")
    sheet.row(1, (0.5, 0.5))
    # Page 2: the vendor's drawing beside the architect's (confirmed, and suggested).
    sheet.drawing(2, "panel-1", LEFT, role="shop")
    sheet.drawing(2, "panel-2", RIGHT, role="arch", suggested="arch")
    sheet.row(2, (0.25, 0.5))
    # Page 3: the architect's drawing only suggested, never confirmed.
    sheet.drawing(3, "panel-1", LEFT, suggested="shop")
    sheet.drawing(3, "panel-2", RIGHT, suggested="arch")
    sheet.row(3, (0.25, 0.5))

    assert sheet.listed({1, 2, 3}) == []


def test_a_page_with_no_row_read_lists_no_vendor_drawing(session: Session) -> None:
    """A read page where no row was read (every AI said "no countertop", or the readers split) is
    already listed on its own with the pages with no countertop; its drawings are not repeated."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")

    assert sheet.listed({1}) == []


def test_only_the_pages_read_and_the_newest_reading_count(session: Session) -> None:
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.drawing(2, "panel-1", LEFT, role="shop")
    sheet.drawing(2, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.75, 0.5))  # the older reading read the right drawing
    newer = sheet.new_run()
    sheet.row(1, (0.25, 0.5), run=newer)  # the newer one reads the left drawing
    sheet.row(2, (0.25, 0.5), run=newer)

    assert sheet.listed({1}) == [
        (
            1,
            "the right drawing",
            _not_read("right", "left"),
        )
    ]


def test_a_row_read_outside_every_drawing_lists_each_vendor_drawing_when_there_are_two(
    session: Session,
) -> None:
    """Which drawing the row read belongs to cannot be said, but with two vendor drawings at least
    one was not read: both are listed, so neither is silent. With one, nothing is."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.5, 0.95))
    sheet.drawing(2, "panel-1", LEFT, role="shop")
    sheet.row(2, (0.75, 0.5))

    reason = (
        "{} is a vendor drawing that may not have been read for a countertop: "
        f"{ONE_PER_PAGE}, and the one read could not be placed in any vendor drawing on the page."
    )
    assert sheet.listed({1, 2}) == [
        (1, "the left drawing", reason.format("The left drawing")),
        (1, "the right drawing", reason.format("The right drawing")),
    ]


def test_a_row_read_that_cannot_be_placed_lists_each_vendor_drawing_when_there_are_two(
    session: Session,
) -> None:
    """A reading whose run recorded no dpi cannot be placed on the sheet: the same as a row that
    lies in no drawing, never a guess at which drawing was read."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.25, 0.5), run=sheet.new_run(dpi=None))

    assert [(page, drawing) for page, drawing, _reason in sheet.listed({1})] == [
        (1, "the left drawing"),
        (1, "the right drawing"),
    ]


def test_a_drawing_with_no_role_keeps_its_own_reason_beside_a_vendor_drawing_not_read(
    session: Session,
) -> None:
    """The #1193 listing is unchanged: a drawing with no role is listed with its heading's reason,
    and a vendor drawing not read is listed with this one, in the page's order."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", ("0.02", "0.1", "0.3", "0.9"), role="shop")
    sheet.drawing(1, "panel-2", ("0.35", "0.1", "0.65", "0.9"), why="synthetic: no label")
    sheet.drawing(1, "panel-3", ("0.7", "0.1", "0.98", "0.9"), suggested="shop")
    sheet.row(1, (0.15, 0.5))

    assert sheet.listed({1}) == [
        (
            1,
            "the middle drawing",
            "The middle drawing was not read for a countertop: synthetic: no label.",
        ),
        (
            1,
            "the right drawing",
            _not_read("right", "left"),
        ),
    ]


def _may_not(place: str, why: str) -> str:
    return (
        f"The {place} drawing is a vendor drawing that may not have been read for a countertop: "
        f"{ONE_PER_PAGE}, and {why}."
    )


def test_a_page_a_reviewer_took_over_lists_no_vendor_drawing_by_the_ais_row(
    session: Session,
) -> None:
    """Review of #1252 (Medium). Where a reviewer marked the countertops by hand, the check uses only
    theirs and ignores the AI's row (`workflow/stages.py`, the reviewer-owned pages), so naming the
    drawing the AI's row lies in would be wrong: here the reviewer's countertop is in the right
    drawing, which is the one checked, yet the AI's row is in the left one. Nothing is listed by
    the AI's row on that page, as for the rows not checked; another page still is."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    right = sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.25, 0.5))
    sheet.reviewer_countertop(right)
    sheet.drawing(2, "panel-1", LEFT, role="shop")
    sheet.drawing(2, "panel-2", RIGHT, role="shop")
    sheet.row(2, (0.25, 0.5))

    assert sheet.listed({1, 2}) == [(2, "the right drawing", _not_read("right", "left"))]


def test_a_page_where_a_reviewer_marked_both_drawings_lists_neither(session: Session) -> None:
    """Both drawings hold a countertop the reviewer marked: both are checked, so neither is "not
    read", whichever drawing the AI's row lies in."""
    sheet = Sheet(session)
    left = sheet.drawing(1, "panel-1", LEFT, role="shop")
    right = sheet.drawing(1, "panel-2", RIGHT, role="shop")
    sheet.row(1, (0.25, 0.5))
    sheet.reviewer_countertop(left)
    sheet.reviewer_countertop(right)

    assert sheet.listed({1}) == []


def test_a_drawing_with_no_role_the_row_lies_in_is_never_named_as_the_one_read(
    session: Session,
) -> None:
    """Review of #1252 (Low 1). A drawing with no role is listed as not read (#1193), so it is never
    named as the drawing read too: the report would say both. With two vendor drawings beside it,
    the drawing read cannot be named, and each is listed with "may not have been read"; with one,
    nothing is, as for a row that lies in no drawing."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", ("0.02", "0.1", "0.3", "0.9"), why="synthetic: no label")
    sheet.drawing(1, "panel-2", ("0.35", "0.1", "0.65", "0.9"), role="shop")
    sheet.drawing(1, "panel-3", ("0.7", "0.1", "0.98", "0.9"), role="shop")
    sheet.row(1, (0.15, 0.5))
    sheet.drawing(2, "panel-1", LEFT, why="synthetic: no label")
    sheet.drawing(2, "panel-2", RIGHT, role="shop")
    sheet.row(2, (0.25, 0.5))

    unplaced = "the one read could not be placed in any vendor drawing on the page"
    no_label = "was not read for a countertop: synthetic: no label."
    assert sheet.listed({1, 2}) == [
        (1, "the left drawing", f"The left drawing {no_label}"),
        (1, "the middle drawing", _may_not("middle", unplaced)),
        (1, "the right drawing", _may_not("right", unplaced)),
        (2, "the left drawing", f"The left drawing {no_label}"),
    ]


def test_a_row_read_across_two_drawings_lists_each_vendor_drawing(session: Session) -> None:
    """Review of #1252 (Low 2). The row read has one box in each drawing: which one was read cannot
    be said, so both are listed with "may not have been read", never neither."""
    sheet = Sheet(session)
    sheet.drawing(1, "panel-1", LEFT, role="shop")
    sheet.drawing(1, "panel-2", RIGHT, role="shop")
    # The row's two boxes sit 60 pixels apart, either side of the gap between the drawings.
    sheet.row(1, (0.5, 0.5))

    across = "the one read lies across more than one drawing on the page"
    assert sheet.listed({1}) == [
        (1, "the left drawing", _may_not("left", across)),
        (1, "the right drawing", _may_not("right", across)),
    ]
