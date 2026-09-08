"""Who wrote the annotation a candidate was read from (#543).

Revision ID: 0038_candidate_source_author
Revises: 0037_page_transform

The first real client set carries the reviewer's corrections as `/FreeText` annotations, and each one
names its author in `/T` — `GVI-007` on every note of `AI_Set 2` page 3. That is worth keeping: on
page 3 the vendor's drawing states one overall width and the annotation over it states another, and
*who* wrote the correction is part of why the correction outranks the drawing.

**Why a column and not a run.** Which reading *route* produced a candidate is already recorded, one
level up: `open_extraction_run` keys a run on extractor, version and configuration, and the OCR route
opens its own so that "a reviewer can tell a scanned reading from a vector one without inspecting the
text". The markup route does the same. An author cannot go there — it varies per annotation, and two
notes on one page may be written by different people, so a run-level field would have to pick one.

Nullable, and null is the ordinary case: a number read off a drawing has no author at all. Nothing is
backfilled. A candidate recorded before this migration came from a route that did not know the
concept, and writing anything into those rows would assert an authorship nobody stated.

Blank is refused with the regex 0035 settled on rather than `btrim`, which strips only spaces —
see the comment on the constraint.

Two hundred characters because a PDF `/T` is free text — it holds a name, an initials-and-number
code, or a whole email address depending on who configured the tool that wrote it.

Source: issue #543. Verification: tests/db/test_evidence_models.py, tests/workflow/test_markup_route.py.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0038_candidate_source_author"
down_revision: str | None = "0037_page_transform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "observation_candidates",
        sa.Column("source_author", sa.String(length=200), nullable=True),
    )
    # Empty is not a name. A `/T` present but blank is a tool that wrote the key without a value,
    # and storing it would make "nobody said" and "somebody said nothing" the same row — where the
    # second reads as an attribution.
    #
    # **The regex, not `btrim`.** This was written as `length(btrim(source_author)) > 0` first, and a
    # test with a tab in it failed immediately: PostgreSQL's `btrim` strips **spaces** and nothing
    # else, so a tab-only author passed a constraint whose whole purpose was to refuse blanks. That
    # is the same defect 0035 fixed in `audit_events`, where it had stood since 0023 — written here
    # the way 0035 corrected it, so the two agree and nobody has to find it a third time.
    op.create_check_constraint(
        "candidate_source_author_not_blank",
        "observation_candidates",
        "source_author IS NULL OR source_author !~ '^[[:space:]]*$'",
    )


def downgrade() -> None:
    """Drops the column, and with it every attribution the markup route recorded."""
    op.drop_constraint("candidate_source_author_not_blank", "observation_candidates", type_="check")
    op.drop_column("observation_candidates", "source_author")
