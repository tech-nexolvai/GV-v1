"""Store what the architect reader read on a combined sheet, as architect-side candidates (#1052).

`extraction/architect/reader.py` reads each page and decides nothing it was not given two judgments
for. This module writes its result down, behind `GV_ARCHITECT_READER_ENABLED`:

* **The drawing's role, by code, only where both judgments agree.** The exact printed heading and
  the drawing's own content giving the same role is recorded as a code confirmation
  (`workflow/view_roles.confirm_view_role_by_code`, decision D2). On a page that prints no heading
  at all, the content of both drawings — one clearly the architect's, another clearly the vendor's
  — is recorded the same way, naming `CODE_CONTENT_CONFIRMER`. A drawing with an earlier
  confirmation — a person's above all — is left exactly as it is.
* **One candidate per printed span, only in a drawing confirmed by code as the architect's.**
  `raw_text` is the label as printed; the value is stored **only when nothing holds it** (exact,
  witnessed by its drawn length, in an architect's drawing). A held span is stored with no value and
  its reason, so a person sees it and nothing can use it. Its polygon is the label's box, so
  `app/evidence/sides.py` places it inside the architect's drawing and gives it the ARCH side.
* Flags carry what the pairing needs (#1052 T2): `arch-row:<rank>`, `arch-slot:<i>`,
  `arch-ticks:<x0>:<x1>` in page points, `arch-scale:<pt per inch>|none`,
  `arch-ticks-on-outline:yes|no|unknown`, `arch-view:<annotation>`, `arch-qualifier:<word>`, and
  `arch-held:<reason>` for a held span.

**What it never does.** It stores nothing read from coloured ink (the reader never reads it); never
reads a same-file package (the same bytes uploaded as both drawings is one combined set, #963); and
never writes a role, a value or a candidate twice for one stage run.

Source: issue #1052 · Verification: `tests/workflow/test_architect_reader.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
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
from workflow.view_roles import (
    CODE_CONFIRMER,
    CODE_CONTENT_CONFIRMER,
    confirm_view_role_by_code,
    panel_tag,
    record_panel_view,
)

__all__ = [
    "ARCHITECT_EXTRACTOR",
    "ARCHITECT_EXTRACTOR_VERSION",
    "ArchitectCounts",
    "persist_architect_pages",
]

#: The route the architect's values are recorded under. Text read by code: never a model.
ARCHITECT_EXTRACTOR: Final = "architect-text"
ARCHITECT_EXTRACTOR_VERSION: Final = "architect-text-v1"

_ROLES: Final = {Role.ARCH: ViewRole.ARCH, Role.SHOP: ViewRole.SHOP}
_REASON_LIMIT: Final = 300
_FLAG_REASON_LIMIT: Final = 160


@dataclass
class ArchitectCounts:
    """What one document's architect reading wrote, for the stage's payload."""

    pages: int = 0
    views_confirmed_by_code: int = 0
    candidates: int = 0
    usable: int = 0
    held: int = 0


def _view(session: Session, page: Page, view: ArchitectView) -> DrawingView:
    """The page's drawing view for this pasted drawing, found or created with its suggestion."""
    existing = session.execute(
        select(DrawingView).where(
            DrawingView.page_id == page.id, DrawingView.tag == panel_tag(view.annotation_index)
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing
    judgment = view.judgment
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
) -> ArchitectCounts:
    """Confirm agreed roles by code and store the architect's spans, page by page."""
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
        architect_views: set[int] = set()
        for view in reading.views:
            agreed = view.judgment.agreed
            if agreed is None:
                continue
            row = _view(session, page, view)
            if (
                confirm_view_role_by_code(
                    session,
                    view=row,
                    role=_ROLES[agreed],
                    reason=view.judgment.reason,
                    confirmed_by=(
                        CODE_CONTENT_CONFIRMER if view.judgment.by_content_alone else CODE_CONFIRMER
                    ),
                )
                is not None
            ):
                counts.views_confirmed_by_code += 1
            if agreed is Role.ARCH:
                architect_views.add(view.annotation_index)
        for architect_row in reading.rows:
            if architect_row.view_annotation_index not in architect_views:
                continue
            for span in architect_row.spans:
                if span.text is None:
                    continue
                flags = [
                    "architect-reader",
                    f"arch-view:{architect_row.view_annotation_index}",
                    f"arch-row:{architect_row.rank}",
                    f"arch-slot:{span.index}",
                    f"arch-ticks:{span.x0_pt}:{span.x1_pt}",
                    _scale_flag(architect_row.points_per_inch),
                    "arch-ticks-on-outline:"
                    + {True: "yes", False: "no", None: "unknown"}[span.on_outline],
                    *(f"arch-qualifier:{qualifier.value}" for qualifier in sorted(span.qualifiers)),
                ]
                if span.held_reason is not None:
                    flags.append(f"arch-held:{span.held_reason[:_FLAG_REASON_LIMIT]}")
                value = span.inches if span.held_reason is None else None
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
                            None if span.held_reason is None else span.held_reason[:_REASON_LIMIT]
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
