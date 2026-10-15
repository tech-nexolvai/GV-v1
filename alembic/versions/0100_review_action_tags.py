"""Tag a review action that is an answer about a held reading, never a decision (#1367).

Revision ID: 0100_review_action_tags
Revises: 0099_draft_redline_output

**Why.** A person can confirm the AI's own reading of a number the checks held back (#1339). That
is an answer about one reading: it is counted by the correction rate, and the next check run uses
the value. It must never read as the person deciding the finding it was recorded on. Before this,
such a `confirm` was written only while its result waited for a person (with no note, so sign-off
never counted it), and not at all through a result already decided (PASS or FAIL), where a
`confirm` reads as the person standing behind that result; so the correction rate missed that case.
And "the drawn length disagreed" had no place to go except the note, which sign-off reads as the
person deciding the result (Anant, 2026-10-16: "Separate tag").

**`review_actions.tags`**, a JSON array, empty for every existing row and every decision:

* `held-reading-answer`: the action is an answer about a held reading. Every reader of review
  actions as decisions skips it (`app/models/review.ReviewAction.is_decision`);
* `drawn-length-disagreed`: on such an answer, the label's drawn length did not fit the number both
  AI readers agreed on. Shown in the history; it decides nothing.

The CHECK allows only these two values, and any tag only on a `confirm` of a reading that carries
`held-reading-answer`. `review_actions` stays append-only (0013): adding a column with a default
fires no row trigger, and the roles' grants are on the table, so they cover the new column.

**A downgrade with a tagged action stored refuses**: without the tag, an answer about a held
reading would read as the person's decision on its finding (a `confirm` of a FAIL would stand
behind that FAIL), and the table's trigger forbids deleting it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0100_review_action_tags"
down_revision: str | None = "0099_draft_redline_output"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "review_actions"
COLUMN = "tags"
CONSTRAINT = "review_action_tags"

#: Written out, not imported from the model: a migration describes one fixed state.
#: `tests/review/test_review_action_tags.py` compares it with `app.models.review.TAGS_CHECK`.
TAGS_CHECK = (
    "jsonb_typeof(tags) = 'array' "
    'AND tags <@ \'["held-reading-answer", "drawn-length-disagreed"]\'::jsonb '
    "AND (tags = '[]'::jsonb OR (action = 'confirm' AND original_observation_id IS NOT NULL "
    "AND tags @> '[\"held-reading-answer\"]'::jsonb))"
)


def upgrade() -> None:
    """Add the tags (every existing action gets none) and their CHECK."""
    # Written literally: `tests/app/test_migration_matches_models.py` reads migrations as text.
    op.add_column(
        "review_actions",
        sa.Column(
            "tags",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.create_check_constraint(CONSTRAINT, TABLE, TAGS_CHECK)


def downgrade() -> None:
    """Drop the CHECK and the column, refused while any action keeps a tag."""
    stored = op.get_bind().scalar(
        sa.text(f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} <> '[]'::jsonb")
    )
    if stored:
        raise RuntimeError(
            f"Cannot downgrade the review-action tags: {stored} action(s) are answers about a "
            "held reading, which without their tag would read as decisions on their findings, and "
            "review_actions is append-only. Preserve these records."
        )
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, COLUMN)
