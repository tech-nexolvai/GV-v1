"""Store what the architect reader read on a combined sheet, as architect-side candidates (#1052).

`extraction/architect/reader.py` reads each page and decides nothing it was not given two judgments
for. This module writes its result down, behind `GV_ARCHITECT_READER_ENABLED`:

* **The drawing's role, by code, only where both judgments agree.** The exact printed heading and
  the drawing's own content giving the same role is recorded as a code confirmation
  (`workflow/view_roles.confirm_view_role_by_code`, decision D2). On a page that prints no heading
  at all, the content of both drawings — one clearly the architect's, another clearly the vendor's
  — is recorded the same way, naming `CODE_CONTENT_CONFIRMER`. On the architect's own file (an
  ARCHITECTURAL upload whose bytes are not the shop file's, #1163) the document's kind and the
  drawing's content agreeing is recorded naming `CODE_DOCUMENT_CONFIRMER`; a view drawn as the
  page's own content is the view `view-<number>` (`view_roles.content_view_tag`) and its values
  also carry `arch-view-tag:view-<number>`. A drawing with an earlier
  confirmation — a person's above all — is left exactly as it is.
* **One candidate per printed span, only in a drawing confirmed by code as the architect's.**
  `raw_text` is the label as printed; the value is stored **only when nothing holds it** (exact,
  witnessed by its drawn length, in an architect's drawing). A held span is stored with no value and
  its reason, so a person sees it and nothing can use it. Its polygon is the label's box, so
  `app/evidence/sides.py` places it inside the architect's drawing and gives it the ARCH side.
* Flags carry what the pairing needs (#1052 T2): `arch-row:<rank>`, `arch-slot:<i>`,
  `arch-ticks:<x0>:<x1>` and `arch-line:<y>` (the height of the row's dimension line, #1068) in page
  points in pdfplumber's frame, `arch-scale:<pt per inch>|none`,
  `arch-ticks-on-outline:yes|no|unknown`, `arch-view:<annotation>`, `arch-qualifier:<word>`, and
  `arch-held:<reason>` for a held span.

**What it never does.** It stores nothing read from coloured ink (the reader never reads it); never
reads a same-file package (the same bytes uploaded as both drawings is one combined set, #963); and
never writes a role, a value or a candidate twice for one stage run.

Source: issue #1052 · Verification: `tests/workflow/test_architect_reader.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DrawingView, Page, ViewRole
from app.models.evidence import ObservationCandidate
from extraction.architect.reader import ArchitectPage, ArchitectView
from extraction.architect.views import Role
from units.measurement import Unit
from workflow.architect_pairing_records import ARCHITECT_EXTRACTOR
from workflow.view_roles import (
    CODE_CONFIRMER,
    CODE_CONTENT_CONFIRMER,
    CODE_DOCUMENT_CONFIRMER,
    confirm_view_role_by_code,
    content_view_tag,
    panel_tag,
    record_content_view,
    record_panel_view,
)

__all__ = [
    "ARCHITECT_EXTRACTOR",
    "ARCHITECT_EXTRACTOR_VERSION",
    "SAME_REGION",
    "ArchitectCounts",
    "persist_architect_pages",
    "view_tag",
]

#: The route the architect's values are recorded under (`ARCHITECT_EXTRACTOR`) lives in
#: `workflow/architect_pairing_records.py`, which the API may import; re-exported here.
ARCHITECT_EXTRACTOR_VERSION: Final = "architect-text-v1"

_ROLES: Final = {Role.ARCH: ViewRole.ARCH, Role.SHOP: ViewRole.SHOP}
_REASON_LIMIT: Final = 300
_FLAG_REASON_LIMIT: Final = 160


#: A view drawn as page content is the same view on a re-read only when every corner of its stored
#: region is within this of the stored one (stored space: the visible page is 0..1 across and down).
#: Two percent of the page: a re-read of the same bytes gives the same corners exactly; a view that
#: was renumbered sits somewhere else entirely.
SAME_REGION: Final = Decimal("0.02")


@dataclass
class ArchitectCounts:
    """What one document's architect reading wrote, for the stage's payload. Never silent: every
    view found without a role, every view refused, and every page note is listed with its reason."""

    pages: int = 0
    views_confirmed_by_code: int = 0
    candidates: int = 0
    usable: int = 0
    held: int = 0
    views_without_role: list[dict[str, object]] = field(default_factory=list)
    refused_views: list[dict[str, object]] = field(default_factory=list)
    page_notes: list[dict[str, object]] = field(default_factory=list)


def view_tag(source: str, number: int) -> str:
    """The `DrawingView.tag` of a reader's view: `view-<n>` drawn as content, else `panel-<n>`."""
    return content_view_tag(number) if source == "content" else panel_tag(number)


def _tag(view: ArchitectView) -> str:
    return view_tag(view.source, view.annotation_index)


def _same_region(stored: object, points: Sequence[tuple[Decimal, Decimal]]) -> bool:
    if not isinstance(stored, dict) or stored.get("space") != "stored":
        return False
    corners = stored.get("points")
    if not isinstance(corners, list) or len(corners) != len(points):
        return False
    try:
        return all(
            abs(Decimal(str(old[0])) - x) <= SAME_REGION
            and abs(Decimal(str(old[1])) - y) <= SAME_REGION
            for old, (x, y) in zip(corners, points, strict=True)
        )
    except (ArithmeticError, IndexError, TypeError, ValueError):
        return False


def _view(session: Session, page: Page, view: ArchitectView) -> DrawingView | None:
    """The page's drawing view for this drawing, found or created with its suggestion: a pasted
    drawing's `panel-<annotation>`, or a view drawn as the page's content `view-<number>` (#1163).

    `None` — refused — when a view drawn as content finds a stored view of its number somewhere else
    on the page (`SAME_REGION`): the numbers moved, and reusing it would hang another drawing's
    role on this one.
    """
    existing = session.execute(
        select(DrawingView).where(DrawingView.page_id == page.id, DrawingView.tag == _tag(view))
    ).scalar_one_or_none()
    if existing is not None:
        if view.source == "content" and not _same_region(existing.region, view.stored_points):
            return None
        return existing
    judgment = view.judgment
    if view.source == "content":
        return record_content_view(
            session,
            page_id=page.id,
            number=view.annotation_index,
            stored_points=view.stored_points,
            title=" ".join(part for part in (view.bubble, view.title) if part) or None,
            reason=judgment.reason,
        )
    return record_panel_view(
        session,
        page_id=page.id,
        annotation_index=view.annotation_index,
        stored_points=view.stored_points,
        proposed_role=None if judgment.heading_role is None else judgment.heading_role.value,
        heading=judgment.heading,
        reason=judgment.heading_reason,
    )


def _rectangle(box: tuple[int, int, int, int]) -> list[list[int]]:
    left, top, right, bottom = box
    return [[left, top], [right, top], [right, bottom], [left, bottom]]


def _scale_flag(points_per_inch: Decimal | None) -> str:
    if points_per_inch is None:
        return "arch-scale:none"
    return f"arch-scale:{points_per_inch.quantize(Decimal('0.0001'))}"


def persist_architect_pages(
    session: Session,
    *,
    document_version_id: UUID,
    extraction_run_id: UUID,
    pages: Sequence[tuple[Page, ArchitectPage]],
    architect_document: bool = False,
) -> ArchitectCounts:
    """Confirm agreed roles by code and store the architect's spans, page by page.

    On the architect's own file (`architect_document`, #1163) nothing found is dropped silently:
    every view is recorded with its suggestion and reason even when it gets no role, its spans are
    stored **held** (no value) with the reason, and every page note (`ArchitectPage.notes`: why no
    view was found, what was left out) is listed in the counts. On the vendor's file only the
    architect's drawings are stored, as before.
    """
    counts = ArchitectCounts()
    already = session.execute(
        select(ObservationCandidate.id)
        .where(
            ObservationCandidate.extraction_run_id == extraction_run_id,
            ObservationCandidate.document_version_id == document_version_id,
        )
        .limit(1)
    ).scalar_one_or_none()
    if already is not None:
        # A redelivered stage has already written this document's reading under this run.
        return counts
    for page, reading in pages:
        counts.pages += 1
        if architect_document:
            for note in reading.notes:
                counts.page_notes.append({"page_index": page.index, "note": note})
        stored_views: set[str] = set()
        architect_tags: set[str] = set()
        for view in reading.views:
            agreed = view.judgment.agreed
            if agreed is None and not architect_document:
                continue
            row = _view(session, page, view)
            if row is None:
                counts.refused_views.append(
                    {
                        "page_index": page.index,
                        "view": _tag(view),
                        "reason": (
                            "a view of this number was stored before somewhere else on the page: "
                            "the views were renumbered, so its role and values are not reused or "
                            "stored; a person checks the page"
                        ),
                    }
                )
                continue
            stored_views.add(_tag(view))
            if agreed is None:
                counts.views_without_role.append(
                    {"page_index": page.index, "view": _tag(view), "reason": view.judgment.reason}
                )
                continue
            if (
                confirm_view_role_by_code(
                    session,
                    view=row,
                    role=_ROLES[agreed],
                    reason=view.judgment.reason,
                    confirmed_by=(
                        CODE_DOCUMENT_CONFIRMER
                        if view.judgment.by_document_kind
                        else (
                            CODE_CONTENT_CONFIRMER
                            if view.judgment.by_content_alone
                            else CODE_CONFIRMER
                        )
                    ),
                )
                is not None
            ):
                counts.views_confirmed_by_code += 1
            if agreed is Role.ARCH:
                architect_tags.add(_tag(view))
        for architect_row in reading.rows:
            tag = view_tag(architect_row.view_source, architect_row.view_annotation_index)
            if tag not in architect_tags and not (architect_document and tag in stored_views):
                continue
            for span in architect_row.spans:
                if span.text is None:
                    continue
                held_reason = span.held_reason
                if tag not in architect_tags and held_reason is None:
                    held_reason = "this drawing is not the architect's by code"
                flags = [
                    "architect-reader",
                    f"arch-view:{architect_row.view_annotation_index}",
                    f"arch-row:{architect_row.rank}",
                    f"arch-slot:{span.index}",
                    f"arch-ticks:{span.x0_pt}:{span.x1_pt}",
                    f"arch-line:{architect_row.y}",
                    _scale_flag(architect_row.points_per_inch),
                    "arch-ticks-on-outline:"
                    + {True: "yes", False: "no", None: "unknown"}[span.on_outline],
                    *(f"arch-qualifier:{qualifier.value}" for qualifier in sorted(span.qualifiers)),
                ]
                if architect_row.view_source == "content":
                    flags.append(f"arch-view-tag:{tag}")
                if held_reason is not None:
                    flags.append(f"arch-held:{held_reason[:_FLAG_REASON_LIMIT]}")
                value = span.inches if held_reason is None else None
                session.add(
                    ObservationCandidate(
                        document_version_id=document_version_id,
                        page_id=page.id,
                        extraction_run_id=extraction_run_id,
                        raw_text=span.text,
                        value_numerator=None if value is None else value.numerator,
                        value_denominator=None if value is None else value.denominator,
                        unit=None if value is None else Unit.INCH.value,
                        semantic_guess=None,
                        polygon=_rectangle(span.label_pixels or span.span_pixels),
                        coordinate_space="image",
                        confidence=None,
                        ambiguity_flags=flags,
                        review_reason=(
                            None if held_reason is None else held_reason[:_REASON_LIMIT]
                        ),
                    )
                )
                counts.candidates += 1
                if value is None:
                    counts.held += 1
                else:
                    counts.usable += 1
    session.flush()
    return counts
