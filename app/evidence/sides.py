"""Which side of a comparison a reading is on — the architect's drawing or the vendor's (#795).

ADR-0020 §Decision 2: `Document.kind` "stops being the thing that decides which side of a comparison a
reading is on". The client's sheets put the architect's `ID SET ELEVATION` beside the vendor's shop
elevation on one page, so an upload's kind says nothing about which half a number sits in — and a
reading from the architect's half checked as the vendor's can pass a vendor's wrong drawing on the
architect's right number.

**One rule, for every place a side is decided.** A reviewer's label (`app/evidence/confirm.py`), the
exact-tag lane (`app/evidence/automatic_typing.py`), the fields the Measure page offers a reading
for (`app/api/confirmations.py`), the readings the form-filler may use (`workflow/propose.py`) and
the passages a setting may be cited from (`workflow/parameter_citations.py`) all ask
`ReadingSides.of`. A second rule anywhere would be the hole the first one closed.

- **A reviewer's markup has no side at all** (#802). The markup route records the reviewer's
  `/FreeText` notes, and on the client's sheets those are mostly boxes writing the architect's number
  over the vendor's. Given the drawing's side, a boxed correction became the vendor's reading, so a
  check could PASS the drawing the reviewer had just marked wrong.
- **A page with no drawing views** — a genuine two-PDF package — takes its side from `Document.kind`,
  exactly as before.
- **A page with views** takes it from the one view whose region holds the reading, once a person has
  confirmed that view's role (`workflow/view_roles.confirm_view_role`). A reading no single view
  holds has no side, and says why.
- **An unconfirmed view** has the upload's side only when the package has both an architectural and
  a shop document *and* the page holds that one drawing (admin, 2026-10-01, #795): a genuine two-PDF
  package, whose drawings sit in `/Stamp`s and so are views, fills its form without a confirmation
  per drawing; a page with two drawings side by side never takes the upload's word for either.
  Otherwise it has no side until somebody says. For a setting's citation, an unconfirmed view never
  takes the upload's side (`confirmed_views_only`, #849).

**No extraction imports**, like `workflow/view_roles.py`: the API reaches this, and
`tests/api/test_no_heavy_work.py` keeps `app/api/` away from anything that renders or reads a PDF.
Containment is `evidence.polygon.Polygon.contains`, in the stored coordinate space both a candidate's
transformed polygon and a view's region are kept in.

Source: issue #795. Verification: tests/evidence/test_sides.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DrawingView, ViewRole
from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from evidence.coordinates import ImagePoint, PageTransform, StoredPoint
from evidence.polygon import Polygon
from vocabulary.semantic_types import DocumentRole

__all__ = [
    "MARKUP_ROUTE",
    "ReadingSides",
    "SideRefusal",
    "SideRefusalReason",
    "reading_transform",
]

#: The route the reviewer's markup is recorded under — `workflow.stages.MARKUP_EXTRACTOR`, restated
#: because that module reads PDFs and the API may not import it (`tests/api/test_no_heavy_work.py`).
#: `tests/evidence/test_sides.py` fails if the two part.
MARKUP_ROUTE: Final = "extraction.annotations"


class SideRefusalReason(StrEnum):
    """Why a reading has no side. Each is a fact about the drawing or its record, never a guess."""

    NOT_COMPARED = "not_compared"
    MARKUP = "markup"
    NO_TRANSFORM = "no_transform"
    NOT_IN_ONE_VIEW = "not_in_one_view"
    VIEW_ROLE_UNCONFIRMED = "view_role_unconfirmed"
    SAME_FILE_BOTH_SIDES = "same_file_both_sides"


@dataclass(frozen=True, slots=True)
class SideRefusal:
    """A reading with no side, and the reason in words a reviewer can act on."""

    reason: SideRefusalReason
    detail: str


_KIND_SIDE = {
    DocumentKind.ARCHITECTURAL.value: DocumentRole.ARCH,
    DocumentKind.SHOP.value: DocumentRole.SHOP,
}
_VIEW_SIDE = {ViewRole.ARCH.value: DocumentRole.ARCH, ViewRole.SHOP.value: DocumentRole.SHOP}


def reading_transform(page: Page, run: ExtractionRun) -> PageTransform | None:
    """The transform the reading was made under, rebuilt exactly — or `None` if it was not recorded.

    `None` rather than a reconstruction from the page's size. The conversion normalises by the crop
    box, and assuming the crop box starts at the origin and fills the page is right for most PDFs and
    silently wrong for the rest. Being silently wrong here does not lose evidence; it places evidence
    on a region of the drawing nobody wrote, which a reviewer would have no way to notice.
    """
    if page.media_box is None or page.crop_box is None or run.dpi is None:
        return None
    try:
        return PageTransform(
            dpi=run.dpi,
            rotation=page.rotation,
            media_box=tuple(Decimal(value) for value in page.media_box),  # type: ignore[arg-type]
            crop_box=tuple(Decimal(value) for value in page.crop_box),  # type: ignore[arg-type]
        )
    except (ArithmeticError, TypeError, ValueError):
        return None


def _region(view: DrawingView, page: Page) -> Polygon | None:
    """A view's stored region as a polygon, or `None` if the stored region will not rebuild.

    `None` costs the view its readings — none can be shown to lie inside it — rather than costing
    the reviewer the page.
    """
    region = view.region
    if not isinstance(region, dict) or region.get("space") != "stored":
        return None
    corners = region.get("points")
    if not isinstance(corners, list):
        return None
    try:
        points = tuple(StoredPoint(x=Decimal(str(x)), y=Decimal(str(y))) for x, y in corners)
        return Polygon(
            points=points,
            space="stored",
            document_version_id=page.document_version_id,
            page=page.index,
        )
    except (ArithmeticError, KeyError, TypeError, ValueError):
        return None


class ReadingSides:
    """Decides readings' sides for one session, loading each page's views and kind once."""

    def __init__(self, session: Session) -> None:
        self._session = session
        self._views: dict[UUID, tuple[tuple[DrawingView, Polygon | None], ...]] = {}
        self._kinds: dict[UUID, str | None] = {}
        self._both_kinds: dict[UUID, bool] = {}
        self._same_file: dict[UUID, bool] = {}

    def _page_views(self, page: Page) -> tuple[tuple[DrawingView, Polygon | None], ...]:
        if page.id not in self._views:
            views = self._session.execute(
                select(DrawingView).where(DrawingView.page_id == page.id).order_by(DrawingView.tag)
            ).scalars()
            self._views[page.id] = tuple((view, _region(view, page)) for view in views)
        return self._views[page.id]

    def _kind(self, document_version_id: UUID) -> str | None:
        if document_version_id not in self._kinds:
            kind = self._session.execute(
                select(Document.kind)
                .join(DocumentVersion, DocumentVersion.document_id == Document.id)
                .where(DocumentVersion.id == document_version_id)
            ).scalar_one_or_none()
            self._kinds[document_version_id] = None if kind is None else str(kind)
        return self._kinds[document_version_id]

    def _membership_rows(self, document_version_id: UUID) -> list[tuple[UUID, str, str]]:
        """(revision, kind, sha256) for every drawing in every revision this version is in."""
        revisions = select(PackageRevisionDocument.package_revision_id).where(
            PackageRevisionDocument.document_version_id == document_version_id
        )
        rows = self._session.execute(
            select(
                PackageRevisionDocument.package_revision_id, Document.kind, DocumentVersion.sha256
            )
            .join(Document, Document.id == PackageRevisionDocument.document_id)
            .join(
                DocumentVersion, DocumentVersion.id == PackageRevisionDocument.document_version_id
            )
            .where(PackageRevisionDocument.package_revision_id.in_(revisions))
        ).all()
        return [(revision_id, str(kind), str(sha256)) for revision_id, kind, sha256 in rows]

    def _same_file_as_both_sides(self, document_version_id: UUID) -> bool:
        """Whether this version's bytes are uploaded as BOTH the architect's and the shop drawing in
        some revision it is in (#963). Such a file is one combined set, never two sides."""
        if document_version_id not in self._same_file:
            sha = self._session.execute(
                select(DocumentVersion.sha256).where(DocumentVersion.id == document_version_id)
            ).scalar_one_or_none()
            kinds_of_these_bytes: dict[UUID, set[str]] = {}
            for revision_id, kind, row_sha in self._membership_rows(document_version_id):
                if row_sha == sha:
                    kinds_of_these_bytes.setdefault(revision_id, set()).add(kind)
            wanted = {DocumentKind.ARCHITECTURAL.value, DocumentKind.SHOP.value}
            self._same_file[document_version_id] = any(
                wanted <= kinds for kinds in kinds_of_these_bytes.values()
            )
        return self._same_file[document_version_id]

    def same_file_as_both_sides(self, document_version_id: UUID) -> bool:
        """Whether these bytes were uploaded as both the architect's and the vendor's drawing (#963).

        Public for the architect check (#1054), which compares nothing on such a file: one combined
        set uploaded twice has no separate architect side to compare with.
        """
        return self._same_file_as_both_sides(document_version_id)

    def _in_two_pdf_packages(self, document_version_id: UUID) -> bool:
        """Whether every revision this document is in also holds the other drawing's document.

        Every, not any: a revision this file is the only drawing of is one where its unconfirmed
        drawings could be either, and an answer that held only in some revisions would be wrong in
        the others. **Two different files** (#963): the same bytes uploaded as both drawings is one
        combined set, and its slot must never decide a side.
        """
        if document_version_id not in self._both_kinds:
            kinds: dict[UUID, set[str]] = {}
            for revision_id, kind, _sha in self._membership_rows(document_version_id):
                kinds.setdefault(revision_id, set()).add(kind)
            wanted = {DocumentKind.ARCHITECTURAL.value, DocumentKind.SHOP.value}
            self._both_kinds[document_version_id] = (
                bool(kinds)
                and all(wanted <= found for found in kinds.values())
                and not self._same_file_as_both_sides(document_version_id)
            )
        return self._both_kinds[document_version_id]

    def drawings_holding(self, page: Page, reading: Polygon) -> tuple[DrawingView, ...] | None:
        """The drawings on `page` whose region contains `reading`, or `None` when it has none (#826).

        `reading` is in the stored space a canonical observation already carries. `None` and an empty
        tuple are different answers: a plain one-drawing PDF has no views, so the page is the drawing;
        a page that has views but none holding the reading cannot say which drawing it is on.
        """
        views = self._page_views(page)
        if not views:
            return None
        return tuple(
            view for view, region in views if region is not None and region.contains(reading)
        )

    def unconfirmed_side(self, page: Page) -> DocumentRole | None:
        """The side the upload gives an unconfirmed drawing on `page`, or `None` when it gives none.

        Only a page holding one drawing, in a package with both an architectural and a shop
        document (admin, 2026-10-01). A confirmation always wins over this; it is asked only for a
        drawing nobody has confirmed.
        """
        if len(self._page_views(page)) != 1 or not self._in_two_pdf_packages(
            page.document_version_id
        ):
            return None
        return _KIND_SIDE.get(self._kind(page.document_version_id) or "")

    def of(
        self, row: ObservationCandidate, *, confirmed_views_only: bool = False
    ) -> DocumentRole | SideRefusal:
        """`row`'s side, or why it has none.

        `confirmed_views_only` withholds the upload's side from a drawing nobody has confirmed, so a
        reading on one is refused even in a two-PDF package (#849). On a sheet with drawings, a
        setting is cited only from one a person has said is the architect's: the admin's fallback of
        2026-10-01 was decided for filling the form, and is not extended to settings. A page with no
        drawings still takes its document's kind, because it has no drawing for anybody to confirm.
        """
        run = self._session.get(ExtractionRun, row.extraction_run_id)
        if run is not None and run.extractor == MARKUP_ROUTE:
            return SideRefusal(
                SideRefusalReason.MARKUP,
                "this is a reviewer's markup, not the architect's or the vendor's drawing, so it "
                "cannot be either side's reading",
            )
        page = self._session.get(Page, row.page_id)
        views = () if page is None else self._page_views(page)
        if not views:
            if self._same_file_as_both_sides(row.document_version_id):
                return SideRefusal(
                    SideRefusalReason.SAME_FILE_BOTH_SIDES,
                    "this file was uploaded as both the architect's and the vendor's drawing, so its "
                    "upload slot cannot say whose drawing this page is; keep one copy and confirm "
                    "the drawings on it",
                )
            side = _KIND_SIDE.get(self._kind(row.document_version_id) or "")
            if side is None:
                return SideRefusal(
                    SideRefusalReason.NOT_COMPARED,
                    "this document is neither architectural nor shop, so a reading from it takes no "
                    "part in a comparison",
                )
            return side

        assert page is not None
        transform = None if run is None else reading_transform(page, run)
        if transform is None:
            return SideRefusal(
                SideRefusalReason.NO_TRANSFORM,
                "this page was read before its transform was recorded, so the reading cannot be "
                "placed on the drawing; re-run extraction for this document",
            )
        try:
            reading = Polygon(
                points=tuple(
                    transform.to_stored(ImagePoint(x=int(x), y=int(y))) for x, y in row.polygon
                ),
                space="stored",
                document_version_id=row.document_version_id,
                page=page.index,
            )
        except (ArithmeticError, TypeError, ValueError):
            return SideRefusal(
                SideRefusalReason.NOT_IN_ONE_VIEW,
                "this reading cannot be placed on the sheet, so which drawing it belongs to is "
                "unknown",
            )

        holding = [
            view for view, region in views if region is not None and region.contains(reading)
        ]
        if len(holding) != 1:
            return SideRefusal(
                SideRefusalReason.NOT_IN_ONE_VIEW,
                "this reading is not inside exactly one drawing on the sheet, so whether it is the "
                "architect's or the vendor's is unknown",
            )
        (view,) = holding
        if view.role is not None:
            side = _VIEW_SIDE.get(view.role)
        elif confirmed_views_only:
            side = None
        else:
            side = self.unconfirmed_side(page)
        if side is None:
            return SideRefusal(
                SideRefusalReason.VIEW_ROLE_UNCONFIRMED,
                "confirm whether this drawing is the architect's or the vendor's first; until a "
                "person says which, a reading on it can be neither",
            )
        return side
