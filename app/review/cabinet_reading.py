"""A cabinet package is not read by the AI, and says so once (#1205).

**Why.** The AI reading asks countertop questions: which row of dimensions is the countertop's, and
which widths are on it (`extraction/slot_reader/bedrock.py`, `extraction/slot_reader/walls.py`).
Neither cabinet check can use the answer:

* `CAB-FILLER-001` and `CAB-ARCH-VS-SHOP-001` compare whole runs in wall order, or cabinet by cabinet
  by tag, and readings carry neither the order nor the tag, so `workflow/evidence_operands.py`
  withholds every reading from those inputs by design (#794, #833);
* their other inputs are the reviewer's own entries on Measurements (the site width, which cabinets
  are equipment cabinets, the cabinet lists) and the project's six cabinet width limits.

AI Set 3's cabinet package paid for a full countertop reading that no cabinet check used. So a package
whose product is cabinets is not given to the AI readers (`workflow/stages.py`, the extraction stage),
and that is recorded as an extraction run of its own (`AI_READING_SKIPPED`), so the package's record
says it, and the results say it in one plain line.

**What still runs for a cabinet package**, because it feeds a cabinet input and costs no model call:
the page manifest and the drawing's own text, the drawing roles and the architect's dimensions read
by code (they decide which side a reviewer's labelled reading is on), and the part suggestions a
person confirms on Measurements (a confirmed run gives `CAB-FILLER-001` its vendor lists).

**Countertop questions are not asked of a cabinet package** (`app/review/no_countertop.py`): a page
listed with no countertop never blocks its sign-off, including on a package read before this change.

Read-only here; no extraction imports (the API reaches this, `tests/api/test_no_heavy_work.py`).

Source: issue #1205. Verification: `tests/workflow/test_cabinet_reading.py`.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from vocabulary.semantic_types import ProductType

__all__ = [
    "AI_READING_SKIPPED",
    "AI_READING_SKIPPED_VERSION",
    "CABINET_LISTS_LINE",
    "CABINET_NOT_READ_NOTE",
    "ai_reading_skipped",
    "ai_reading_used",
    "results_note",
]

#: The extraction run that records "the AI did not read this package", and why.
AI_READING_SKIPPED: Final = "extraction.ai_reading_skipped"
AI_READING_SKIPPED_VERSION: Final = "cabinet-no-reading-v1"

#: The one visible note on a cabinet package the AI was not asked to read.
CABINET_NOT_READ_NOTE: Final = (
    "Cabinet checks use the cabinet lists entered on Measurements; the drawings were not read by "
    "the AI for this package."
)

#: The plain line in place of the countertop wording on any cabinet package, including one whose
#: drawings were read before #1205: no cabinet check uses that reading.
CABINET_LISTS_LINE: Final = "Cabinet checks use the cabinet lists on Measurements."


def ai_reading_used(product: ProductType | None) -> bool:
    """Whether a check in scope for `product` can use the AI's reading of the drawings.

    A package with no product (made before #994) runs every check, so it is read as before. A
    countertop package is read. A cabinet package is not: see the module note.
    """
    return product is not ProductType.CABINET


def ai_reading_skipped(session: Session, package_revision_id: UUID) -> bool:
    """Whether an extraction of this revision recorded that the AI was not asked to read it."""
    return bool(
        session.scalar(
            select(
                exists()
                .where(ExtractionRun.extractor == AI_READING_SKIPPED)
                .where(TaskRun.id == ExtractionRun.task_run_id)
                .where(WorkflowRun.id == TaskRun.workflow_run_id)
                .where(WorkflowRun.package_revision_id == package_revision_id)
            )
        )
    )


def results_note(session: Session, package_revision_id: UUID, product: str | None) -> str | None:
    """The line the results show instead of countertop wording, or `None` for a countertop package.

    One statement, and only for a cabinet package.
    """
    if product != ProductType.CABINET.value:
        return None
    if ai_reading_skipped(session, package_revision_id):
        return CABINET_NOT_READ_NOTE
    return CABINET_LISTS_LINE
