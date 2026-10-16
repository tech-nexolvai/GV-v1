"""The page a reviewer asks about, as pictures the assistant can look at (#1431).

The owner (2026-10-16): people can chat about anything on the two drawings. So a question about a
page sends the model that page's two halves, each as its own sharp picture: the architect's drawing
and the vendor's drawing, as the panels' stored regions place them (`DrawingView.role` `arch` is
the architect's; every other view is the vendor's). A page with no stored views is sent whole.

**Vendor-only.** The pictures are drawn with GV's markup removed (`render_region(vendor_only=True)`,
which keeps only the pasted drawings): GV's markup is the answer key, never something a model reads
from.

**As sharp as the model reads unresized.** Each half is drawn at the highest dpi (at most 300) whose
picture fits Claude's high-resolution limits (2576 px a side, 4784 visual tokens), so the readers'
own size check (`require_picture_fits`) never refuses it and the API never shrinks it.

**Made once per page.** Rendering is the costly part, so the pictures are cached per revision and
page, in this process, for as long as the reviewer keeps asking (a small LRU). The AI page review
rows are read fresh each question, since a person may answer one between questions.

TODO(#1431): when the AI page review backend lands its render helper in `extraction/ai_review/`,
draw the halves with it instead of `extraction.rasterise.render_region` here, so both send the same
pictures. The AI page review rows come from `app.review.ai_review.ai_review_for_revision`, read only
when that module exists.

**Why `extraction` is imported by name at the first picture.** `app/api/` must not reach
`extraction/` by import (`tests/api/test_no_heavy_work.py`); the renderer is loaded only when a
question about a page is asked, as `model.py` loads the readers' route.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from typing import Any, Final, Literal, Protocol
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from app.evidence.sides import page_transform_at
from app.models import Page
from app.models.document import Document, DocumentKind, DocumentVersion, PackageRevisionDocument
from app.models.drawing import DrawingView
from evidence.coordinates import StoredPoint

__all__ = [
    "PageLook",
    "PagePicture",
    "PagePictureSource",
    "RevisionPagePictures",
    "cached_pictures",
    "fitting_dpi",
]

_log = logging.getLogger(__name__)

type Role = Literal["architect", "vendor", "page"]

#: Claude's high-resolution picture limits (`extraction/slot_reader/claude_output.py`).
_MAX_EDGE_PX: Final = 2576
_MAX_VISUAL_TOKENS: Final = 4784
_TILE_PX: Final = 28
_MAX_DPI: Final = 300
_MIN_DPI: Final = 72
#: Pixels of slack for the renderer's rounding of the box edges.
_SLACK_PX: Final = 2
#: One whole page at 300 dpi of an ANSI E sheet is far over this; a half is well under it.
_MAX_RENDER_PIXELS: Final = 40_000_000
#: Pages whose pictures are kept, across all revisions, in this process.
_CACHE_PAGES: Final = 8


@dataclass(frozen=True, slots=True)
class PagePicture:
    role: Role
    png: bytes
    width_px: int
    height_px: int


@dataclass(frozen=True, slots=True)
class PageLook:
    """What the assistant is shown about one page: its pictures and its AI page review rows."""

    page_number: int
    pictures: tuple[PagePicture, ...]
    ai_review_rows: tuple[Mapping[str, Any], ...] = field(default=())


class PagePictureSource(Protocol):
    """Where a page's look comes from. Tests pass a fake."""

    def for_page(self, page_number: int) -> PageLook | None: ...


_cache: OrderedDict[tuple[UUID, int], tuple[PagePicture, ...]] = OrderedDict()
_cache_lock = threading.Lock()


def cached_pictures(
    revision_id: UUID, page_number: int, render: Callable[[], tuple[PagePicture, ...]]
) -> tuple[PagePicture, ...]:
    """The page's pictures, made by `render` the first time and reused after."""
    key = (revision_id, page_number)
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    pictures = render()
    with _cache_lock:
        _cache[key] = pictures
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_PAGES:
            _cache.popitem(last=False)
    return pictures


def _fits(width_px: int, height_px: int) -> bool:
    tiles_w = -(-width_px // _TILE_PX)
    tiles_h = -(-height_px // _TILE_PX)
    return (
        tiles_w * _TILE_PX <= _MAX_EDGE_PX
        and tiles_h * _TILE_PX <= _MAX_EDGE_PX
        and tiles_w * tiles_h <= _MAX_VISUAL_TOKENS
    )


def fitting_dpi(width_pt: Decimal, height_pt: Decimal) -> int:
    """The highest dpi (72 to 300) at which a box of this size in points fits unresized."""
    for dpi in range(_MAX_DPI, _MIN_DPI - 1, -1):
        width = int(width_pt * dpi / 72) + _SLACK_PX
        height = int(height_pt * dpi / 72) + _SLACK_PX
        if _fits(width, height):
            return dpi
    return _MIN_DPI


def _corners(view: DrawingView) -> list[StoredPoint] | None:
    region = view.region
    if not isinstance(region, dict) or region.get("space") != "stored":
        return None
    points = region.get("points")
    if not isinstance(points, list) or not points:
        return None
    try:
        return [StoredPoint(x=Decimal(str(x)), y=Decimal(str(y))) for x, y in points]
    except (ArithmeticError, TypeError, ValueError):
        return None


def _clamp(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(0), value))


def _union(boxes: Sequence[list[StoredPoint]]) -> tuple[StoredPoint, StoredPoint]:
    xs = [point.x for corners in boxes for point in corners]
    ys = [point.y for corners in boxes for point in corners]
    return (
        StoredPoint(x=_clamp(min(xs)), y=_clamp(min(ys))),
        StoredPoint(x=_clamp(max(xs)), y=_clamp(max(ys))),
    )


@dataclass(frozen=True, slots=True)
class RevisionPagePictures:
    """Pictures of one revision's pages, drawn from the stored PDF and the panels' regions."""

    session: Session
    store: Any
    revision_id: UUID

    def for_page(self, page_number: int) -> PageLook | None:
        page = self._page(page_number)
        if page is None:
            return None
        pictures = cached_pictures(self.revision_id, page_number, lambda: self._render(page))
        return PageLook(
            page_number=page_number,
            pictures=pictures,
            ai_review_rows=self._ai_review_rows(page_number),
        )

    def _page(self, page_number: int) -> Page | None:
        """Page `page_number` (1-based); the shop drawing's when two documents have that page."""
        return self.session.scalars(
            select(Page)
            .join(
                PackageRevisionDocument,
                PackageRevisionDocument.document_version_id == Page.document_version_id,
            )
            .join(Document, Document.id == PackageRevisionDocument.document_id)
            .where(
                PackageRevisionDocument.package_revision_id == self.revision_id,
                Page.index == page_number - 1,
            )
            .order_by(case((Document.kind == DocumentKind.SHOP.value, 0), else_=1), Document.id)
            .limit(1)
        ).first()

    def _pdf(self, page: Page) -> bytes | None:
        version = self.session.get(DocumentVersion, page.document_version_id)
        if version is None:
            return None
        from app.api.documents import storage_key  # the one place the key is derived

        with self.store.get(storage_key(version.document_id, version.sha256)) as stored:
            data: bytes = stored.read()
        return data if hashlib.sha256(data).hexdigest() == version.sha256 else None

    def _render(self, page: Page) -> tuple[PagePicture, ...]:
        data = self._pdf(page)
        transform = page_transform_at(page, 72)
        if data is None or transform is None:
            return ()
        views = self.session.scalars(
            select(DrawingView).where(DrawingView.page_id == page.id).order_by(DrawingView.tag)
        ).all()
        architect = [c for v in views if v.role == "arch" and (c := _corners(v)) is not None]
        vendor = [c for v in views if v.role != "arch" and (c := _corners(v)) is not None]
        halves: list[tuple[Role, tuple[StoredPoint, StoredPoint]]] = []
        if architect:
            halves.append(("architect", _union(architect)))
        if vendor:
            halves.append(("vendor", _union(vendor)))
        if not halves:
            halves.append(
                ("page", (StoredPoint(Decimal(0), Decimal(0)), StoredPoint(Decimal(1), Decimal(1))))
            )
        rasterise = import_module("extraction.rasterise")
        pictures: list[PagePicture] = []
        for role, (top_left, bottom_right) in halves:
            a, b = transform.from_stored(top_left), transform.from_stored(bottom_right)
            dpi = fitting_dpi(Decimal(b.x - a.x), Decimal(b.y - a.y))
            at_dpi = page_transform_at(page, dpi)
            if at_dpi is None:
                continue
            x0, y0 = at_dpi.from_stored(top_left)
            x1, y1 = at_dpi.from_stored(bottom_right)
            if x1 <= x0 or y1 <= y0:
                continue
            png = rasterise.render_region(
                data,
                page.index,
                box_px=(x0, y0, x1, y1),
                dpi=dpi,
                maximum_pixels=_MAX_RENDER_PIXELS,
                vendor_only=True,
            )
            pictures.append(PagePicture(role=role, png=png, width_px=x1 - x0, height_px=y1 - y0))
        return tuple(pictures)

    def _ai_review_rows(self, page_number: int) -> tuple[Mapping[str, Any], ...]:
        try:
            module = import_module("app.review.ai_review")
        except ModuleNotFoundError:
            return ()  # the AI page review backend has not landed yet
        review = module.ai_review_for_revision(self.session, self.revision_id)
        for page in getattr(review, "pages", ()):
            if getattr(page, "page", None) == page_number:
                return tuple(
                    row.model_dump(mode="json") if hasattr(row, "model_dump") else dict(row)
                    for row in getattr(page, "rows", ())
                )
        return ()
