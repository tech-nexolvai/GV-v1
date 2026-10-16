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
whose checks include no countertop — cabinets today, and any product added later
(`app.review.no_countertop.countertops_checked`, the same test sign-off uses) — is not given to the
AI readers (`workflow/stages.py`, the extraction stage). That is recorded as an extraction run of its
own (`AI_READING_SKIPPED`), so the package's record says it, and the results say it in one plain line.

**What the skip loses, stated plainly (accepted by Anant, 2026-10-13):**

* the slot reader's per-slot reading candidates. A person can link one as a part's width on
  Measurements (#913), and a confirmed run of parts is how `CAB-FILLER-001` gets its vendor lists.
  On a drawing whose numbers are pen strokes (no text to read by code), these readings may be the
  only width readings there are; without them the widths are entered on Measurements;
* the rare cabinet- and filler-width suggestions the AI's row offered the Measurements form.

**What still runs for a cabinet package**, because it feeds a cabinet input and costs no model call:
the page manifest and the drawing's own text, the drawing roles and the architect's dimensions read
by code (they decide which side a reviewer's labelled reading is on), and the part suggestions a
person confirms on Measurements (a confirmed run gives `CAB-FILLER-001` its vendor lists).

**Unless `GV_CABINET_AI_READING` is on** (cabinets e2e, off by default because reading is paid):
then a cabinet package is read by the same readers, and the simple review proposes its cabinet run
from the widths read (`app/review/cabinet_run_proposal.py`) for a person to confirm.

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
from app.review.no_countertop import countertops_checked
from vocabulary.semantic_types import ProductType

__all__ = [
    "AI_READING_SKIPPED",
    "AI_READING_SKIPPED_VERSION",
    "CABINET_LISTS_LINE",
    "CABINET_NOT_READ_NOTE",
    "OTHER_PRODUCT_LINE",
    "OTHER_PRODUCT_NOT_READ_NOTE",
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

    Exactly where countertops are checked: a package with no product (made before #994) runs every
    check, so it is read as before; a countertop package is read; a cabinet package, or any product
    added later, is not, because the reading asks countertop questions. See the module note.
    """
    return countertops_checked(None if product is None else product.value)


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


#: The same two lines for a product added later, which has no cabinet lists to name.
OTHER_PRODUCT_NOT_READ_NOTE: Final = (
    "This package's checks use the values entered on Measurements; the drawings were not read by "
    "the AI for this package."
)
OTHER_PRODUCT_LINE: Final = "This package's checks use the values entered on Measurements."


def results_note(session: Session, package_revision_id: UUID, product: str | None) -> str | None:
    """The line the results show instead of countertop wording, or `None` for a countertop package.

    One statement, and only for a package whose checks include no countertop.
    """
    if countertops_checked(product):
        return None
    cabinet = product == ProductType.CABINET.value
    if ai_reading_skipped(session, package_revision_id):
        return CABINET_NOT_READ_NOTE if cabinet else OTHER_PRODUCT_NOT_READ_NOTE
    return CABINET_LISTS_LINE if cabinet else OTHER_PRODUCT_LINE
