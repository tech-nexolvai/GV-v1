"""Let a run record that a drawing was not the drawing that was uploaded (#523).

Revision ID: 0032_digest_mismatch_failure
Revises: 0031_output_artifacts

`ingest` (#517) checks every document against the SHA-256 recorded when it was uploaded and reports a
mismatch in its stage payload. Nothing acted on the report: `extract_pages` ran next and read the file
regardless, so a document truncated or replaced in storage was read and its dimensions became
candidates indistinguishable from readings of the drawing somebody actually submitted.

`extract_pages` now verifies before it reads. This migration gives the refusal somewhere to be
recorded, beside the two failures that already exist rather than in a table of its own — one query
should answer "which drawings did this run decline to read, and why".

**Two constraints, not one.** `extraction_failure_reason` lists the permitted values, and
`extraction_failure_scope` pairs each value with whether a page index is present. Widening only the
first would leave the new reason permitted by one constraint and refused by the other, which is the
shape of bug `ModelInvocationOutcome.FAILED` was: an enum member the database rejected for as long as
nobody re-read the `CHECK`.

A digest mismatch is a document-level fact, so it takes `page_index IS NULL` — the same pairing
`document_unreadable` has.

Source: issue #523. Verification: tests/db/test_extraction_failures.py, tests/workflow/test_extract_pages.py.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0032_digest_mismatch_failure"
down_revision: str | None = "0031_output_artifacts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Every reason after this migration. Written out, because a migration has to keep saying what it
#: said the day it ran — the same reason 0013 lists its tables rather than importing them.
REASONS: tuple[str, ...] = ("document_unreadable", "page_unreadable", "document_digest_mismatch")

_PREVIOUS_REASONS: tuple[str, ...] = ("document_unreadable", "page_unreadable")

_SCOPE = (
    "(reason = 'document_unreadable' AND page_index IS NULL) "
    "OR (reason = 'document_digest_mismatch' AND page_index IS NULL) "
    "OR (reason = 'page_unreadable' AND page_index IS NOT NULL)"
)

_PREVIOUS_SCOPE = (
    "(reason = 'document_unreadable' AND page_index IS NULL) "
    "OR (reason = 'page_unreadable' AND page_index IS NOT NULL)"
)


def _values(reasons: tuple[str, ...]) -> str:
    return ", ".join(f"'{reason}'" for reason in reasons)


def upgrade() -> None:
    op.drop_constraint("extraction_failure_reason", "extraction_failures", type_="check")
    op.create_check_constraint(
        "extraction_failure_reason", "extraction_failures", f"reason IN ({_values(REASONS)})"
    )
    op.drop_constraint("extraction_failure_scope", "extraction_failures", type_="check")
    op.create_check_constraint("extraction_failure_scope", "extraction_failures", _SCOPE)


def downgrade() -> None:
    """Narrows the vocabulary again, and will refuse if a row already uses the new reason.

    Deliberately not preceded by a delete. A recorded refusal to read a drawing is a fact about a
    package, and a downgrade that quietly removed it would leave the package looking like one where
    every document was read.
    """
    op.drop_constraint("extraction_failure_scope", "extraction_failures", type_="check")
    op.create_check_constraint("extraction_failure_scope", "extraction_failures", _PREVIOUS_SCOPE)
    op.drop_constraint("extraction_failure_reason", "extraction_failures", type_="check")
    op.create_check_constraint(
        "extraction_failure_reason",
        "extraction_failures",
        f"reason IN ({_values(_PREVIOUS_REASONS)})",
    )
