"""A page code read no countertop row on is recorded with its reason, never dropped (#1193).

Verification for `workflow/unread_pages.py` and its wiring in `workflow/stages.py`: the reasons,
the record, and a whole extraction of a synthetic combined file whose pages hold no vendor drawing
to read. Synthetic headings and drawings only; no reader is asked anything.
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from dataclasses import replace
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
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.runs import TaskRun, WorkflowRun
from evidence.coordinates import ImagePoint, StoredPoint
from evidence.polygon import Polygon
from extraction.annotations import DrawingLayer, MarkupNote, VendorStamp
from extraction.form_reader.prompt_v5 import BUILT_IN_PROMPT
from extraction.panels import read_panels
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_annotations import KNOWN_AUTHOR, _appearance, _stamp
from tests.extraction.test_reader import MISSING_SPACE
from tests.workflow.test_markup_route import _SilentOcr
from tests.workflow.test_slot_reader import FakeReaders, claude_runtime
from workflow.idempotency import stage_idempotency_key
from workflow.review import ENGINE_VERSION
from workflow.slot_row_scope import unchosen_row_pages
from workflow.stages import DatabaseStages
from workflow.unread_pages import unread_page_reason

pytest_plugins = ("tests.app.postgres_fixture",)

DOCUMENT = uuid4()
PT = Decimal(24)


def _polygon(top: str, bottom: str, left: str = "0.3", right: str = "0.9") -> Polygon:
    t, b, lft, r = (Decimal(v) for v in (top, bottom, left, right))
    return Polygon(
        points=(StoredPoint(lft, t), StoredPoint(r, t), StoredPoint(r, b), StoredPoint(lft, b)),
        space="stored",
        document_version_id=DOCUMENT,
        page=0,
    )


def _drawing(index: int, top: str, bottom: str) -> VendorStamp:
    return VendorStamp(index, _polygon(top, bottom), (ImagePoint(0, 0),))


def _heading(text: str, top: str, bottom: str) -> MarkupNote:
    return MarkupNote(
        layer=DrawingLayer.REVIEWER_MARKUP,
        subtype="/FreeText",
        text=text,
        author="REVIEWER-1",
        extent=_polygon(top, bottom, left="0.03", right="0.3"),
        image_extent=(ImagePoint(0, 0),),
        rotation_degrees=0,
        annotation_index=90,
        font_size=PT,
    )


UPPER, LOWER = _drawing(0, "0.07", "0.42"), _drawing(1, "0.47", "0.95")


def _reason(*notes: MarkupNote, confirmed: dict[int, str | None] | None = None) -> tuple[str, str]:
    return unread_page_reason(
        read_panels([UPPER, LOWER], notes), confirmed or {}, has_drawings_elsewhere=True
    )


# --- the reasons ----------------------------------------------------------------------------------


def test_an_unknown_heading_over_the_lower_drawing_is_named_in_the_reason() -> None:
    code, reason = _reason(
        _heading("ID SET ELEVATION", "0.03", "0.06"), _heading("VENDR SHOP DWG", "0.43", "0.46")
    )

    assert code == "no-vendor-drawing"
    assert reason.startswith("No vendor drawing on this page: ")
    assert "the label above the lower drawing ('VENDR SHOP DWG') is not one we know" in reason
    assert "the architect's drawing ('ID SET ELEVATION')" in reason


def test_only_reference_drawings_say_so() -> None:
    code, reason = _reason(
        _heading("BRAND PROTOTYPE", "0.03", "0.06"),
        _heading("BRAND PROTOTYPE ELEVATION", "0.43", "0.46"),
    )

    assert code == "reference-only"
    assert reason.startswith("Only reference drawings (brand prototype) are on this page")
    assert "'BRAND PROTOTYPE'" in reason and "'BRAND PROTOTYPE ELEVATION'" in reason


def test_the_architect_s_and_a_reference_drawing_are_each_named() -> None:
    code, reason = _reason(
        _heading("ARCH SET ELEVATION", "0.03", "0.06"),
        _heading("BRAND PROTOTYPE ELEVATION", "0.43", "0.46"),
    )

    assert code == "no-vendor-drawing"
    assert "the architect's drawing ('ARCH SET ELEVATION')" in reason
    assert "the reference drawing ('BRAND PROTOTYPE ELEVATION'), never checked" in reason


def test_a_vendor_drawing_with_no_row_says_code_found_no_row_on_it() -> None:
    code, reason = _reason(
        _heading("ID SET ELEVATION", "0.03", "0.06"),
        _heading("VENDOR'S SHOP DRAWING", "0.43", "0.46"),
    )

    assert code == "no-row-found"
    assert "no row of dimensions on the vendor's drawing" in reason


def test_a_reviewer_s_confirmation_outranks_the_heading() -> None:
    code, reason = _reason(
        _heading("ID SET ELEVATION", "0.03", "0.06"),
        _heading("VENDOR'S SHOP DRAWING", "0.43", "0.46"),
        confirmed={1: "arch"},
    )

    assert code == "no-vendor-drawing"
    assert "a reviewer confirmed the lower drawing as the architect's" in reason


def test_a_page_with_no_drawing_in_a_combined_file_is_a_title_or_divider_page() -> None:
    assert unread_page_reason(read_panels([], []), {}, has_drawings_elsewhere=True)[0] == (
        "no-drawing"
    )
    assert unread_page_reason(read_panels([], []), {}, has_drawings_elsewhere=False)[0] == (
        "no-row-found"
    )
    assert unread_page_reason(None, {}, has_drawings_elsewhere=True)[0] == "unreadable"


def test_an_unlabelled_page_with_drawings_says_code_found_no_row_on_the_page() -> None:
    code, reason = unread_page_reason(
        read_panels([UPPER, LOWER], []), {}, has_drawings_elsewhere=True
    )

    assert code == "no-row-found"
    assert "no row of dimensions on this page" in reason


def test_a_reason_always_fits_its_column() -> None:
    long = "VERY LONG UNKNOWN HEADING WORDS HERE"
    drawings = [
        VendorStamp(i, _polygon("0.1", "0.4", left=f"0.{i}", right=f"0.{i}5"), (ImagePoint(0, 0),))
        for i in range(1, 9)
    ]
    notes = [
        MarkupNote(
            layer=DrawingLayer.REVIEWER_MARKUP,
            subtype="/FreeText",
            text=f"{long} {chr(65 + i)}",
            author="REVIEWER-1",
            extent=_polygon("0.02", "0.05", left=f"0.{i}", right=f"0.{i}5"),
            image_extent=(ImagePoint(0, 0),),
            rotation_degrees=0,
            annotation_index=50 + i,
            font_size=PT,
        )
        for i in range(1, 9)
    ]

    _code, reason = unread_page_reason(
        read_panels(drawings, notes), {}, has_drawings_elsewhere=True
    )

    assert len(reason) <= 300


# --- through the extraction stage -----------------------------------------------------------------


def _free_text(text: str, rect: bytes) -> bytes:
    return (
        b"<< /Type /Annot /Subtype /FreeText /Rect "
        + rect
        + b" /Contents ("
        + text.encode("latin-1")
        + b") /DA (/Helv 24 Tf 0 g) /T ("
        + KNOWN_AUTHOR.encode("latin-1")
        + b") >>"
    )


def _combined_file(pages: list[list[bytes] | None]) -> bytes:
    """A multi-page combined file: each page is two headings over two pasted drawings (`None`: a
    divider page with nothing pasted). 400 x 300 pages, like `test_view_roles._combined_sheet`."""
    count = len(pages)
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    page_numbers: list[int] = []
    for headings in pages:
        page_number = len(objects) + 1
        page_numbers.append(page_number)
        content = page_number + 1
        if headings is None:
            objects += [
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Contents "
                + str(content).encode()
                + b" 0 R >>",
                b"<< /Length 0 >>\nstream\n\nendstream",
            ]
            continue
        first = page_number + 2
        appearance = first + len(headings) + 2
        annotations = [
            *(_free_text(text, rect) for text, rect in zip(headings, _RECTS, strict=False)),
            _stamp(rect=b"[60 160 350 290]", appearance_object=appearance),
            _stamp(rect=b"[60 20 350 140]", appearance_object=appearance + 1),
        ]
        refs = b" ".join(f"{first + i} 0 R".encode() for i in range(len(annotations)))
        objects += [
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 300] /Annots ["
            + refs
            + b"] /Contents "
            + str(content).encode()
            + b" 0 R >>",
            b"<< /Length 0 >>\nstream\n\nendstream",
            *annotations,
            _appearance(),
            _appearance(),
        ]
    kids = b" ".join(f"{number} 0 R".encode() for number in page_numbers)
    objects[1] = b"<< /Type /Pages /Kids [" + kids + b"] /Count " + str(count).encode() + b" >>"
    out = bytearray(b"%PDF-1.7\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


#: The heading over the upper drawing, then the one over the lower drawing (a divider).
_RECTS = (b"[10 285 150 298]", b"[10 142 220 156]")


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


def _revision(session: Session, store: LocalStore, data: bytes, pages: int) -> PackageRevision:
    digest = hashlib.sha256(data).hexdigest()
    project = Project(name="unread pages test")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Synthetic Stone")
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
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=pages
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


def test_pages_with_no_vendor_drawing_are_recorded_with_their_reason_not_dropped(
    session: Session, store: LocalStore
) -> None:
    """**The AI Set 3 silent pages.** A divider page, a page of the architect's and a brand
    prototype, and a page whose vendor heading is a wording nobody listed: before #1193 each was
    skipped with no record. Each is now listed with code's reason, and no reader is asked."""
    data = _combined_file(
        [
            None,
            ["ARCH SET ELEVATION", "BRAND PROTOTYPE ELEVATION"],
            ["ID SET ELEVATION", "VENDR SHOP DWG"],
        ]
    )
    revision = _revision(session, store, data, pages=3)
    session.commit()
    readers = FakeReaders(lambda _model, _png: '2"')
    slot = claude_runtime(readers, question_packets=True)
    stages = DatabaseStages(
        store,
        dpi=150,
        ocr_engine=_SilentOcr(),  # type: ignore[arg-type]
        missing_space=MISSING_SPACE,
        form_reader=replace(slot.form, prompt=BUILT_IN_PROMPT),
        slot_reader=slot,
    )

    stages.extract_pages(session, revision.id)
    session.commit()

    assert not readers.requests and not readers.row_requests and not readers.wall_requests
    pages = unchosen_row_pages(session, revision.id)
    assert [(page.page_number, page.kind, page.found_by_code) for page in pages] == [
        (1, "none", True),
        (2, "none", True),
        (3, "none", True),
    ]
    divider, references, unknown = (page.reason for page in pages)
    assert divider.startswith("No drawing is pasted on this page (a title or divider page)")
    assert "the architect's drawing ('ARCH SET ELEVATION')" in references
    assert "the reference drawing ('BRAND PROTOTYPE ELEVATION'), never checked" in references
    assert "the label above the lower drawing ('VENDR SHOP DWG') is not one we know" in unknown
    flags = {
        flag
        for record in session.scalars(
            select(ObservationCandidate).where(
                ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"])
            )
        )
        for flag in record.ambiguity_flags
        if flag.startswith("no-row:")
    }
    assert flags == {"no-row:no-drawing", "no-row:no-vendor-drawing"}
