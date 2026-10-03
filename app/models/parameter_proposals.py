"""Where a package states a setting: a pointer to a passage, never the number (#849).

A setting the rules need, such as an overhang or a backsplash thickness, could only be typed. Step
3.2 of the plan on #798 lets the app point at the passage in the package that states it. One row
here names the setting, the phrase (`text_phrases`), which of the phrase's runs hold the number, the
source that passage would make the value, and the code that proposed it.

**There is no value column, and that is the design.** It follows `measurement_proposals` (0045): a
pointer cannot be mistaken for a setting, cannot reach a check, and cannot drift from a number it
copied. The number stays on the runs. The guard in `workflow/parameter_citations.py` reads it only
to check that it is one inch dimension, to compare it with any other passage, and to compare it with
the number a person typed, and keeps it nowhere. Confirming it is a person's job: step 3.3 of #798
(#866) has them type it without seeing it.

**No side column either.** Whether a passage is the architect's is decided each time a pointer is
made or read back, by `app.evidence.sides.ReadingSides`. A drawing's confirmed role can change after
the row is written, and a stored side would not change with it.

**Append-only.** A changed proposal for a setting is a new row. The newest is the one that
counts, and only while it still holds (`workflow.parameter_proposals.current_parameter_proposals`);
the older rows are the record of what it replaced.

**Not readable by the verdict role.** `app/db/roles.py` grants `gv_verdict` an allowlist, and this
table is not on it.

Source: issue #849, plan step 3.2 on #798.
Verification: `tests/workflow/test_parameter_proposals.py`, `tests/db/test_roles.py`.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from rules.parameter_sources import CITABLE_SIDES

__all__ = ["CLAIMABLE_SOURCES_SQL", "ParameterProposal"]

#: The sources a pointer may claim, rendered for a SQL `IN`: those `CITABLE_SIDES` lets cite a side
#: of a package at all. Derived rather than retyped, so the database refuses a company standard for
#: the same reason the proposer does; migration 0057 writes the list out, and a test holds the two
#: to each other.
CLAIMABLE_SOURCES_SQL: Final = ", ".join(
    f"'{source.value}'"
    for source in sorted(CITABLE_SIDES, key=lambda source: source.value)
    if CITABLE_SIDES[source]
)


class ParameterProposal(Base, TimestampedUUID, Immutable):
    """One passage proposed as the place a package states one setting. A pointer, never a value."""

    __tablename__ = "parameter_proposals"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )

    setting_name: Mapped[str] = mapped_column(String(200))
    """The setting as the rulebook names it, such as `countertop_overhang`."""

    phrase_id: Mapped[UUID] = mapped_column(
        ForeignKey("text_phrases.id", ondelete="RESTRICT"), index=True
    )
    """The passage. Its runs, and the text and polygon of each, stay where they are."""

    first_member: Mapped[int]
    last_member: Mapped[int]
    """The runs that hold the number: `text_phrase_members.position` from `first_member` to
    `last_member`, both included. The span is stored so a reviewer is shown the number's own runs,
    not the whole line."""

    claimed_source: Mapped[str] = mapped_column(String(50))
    """The `Provenance` the passage would give the value: `G.C / Client` for the architect's
    drawings. The database accepts only a source that may cite a package at all."""

    proposer: Mapped[str] = mapped_column(String(200))
    proposer_version: Mapped[str] = mapped_column(String(100))
    """Which code proposed it, and which version of its rule for choosing the runs, so a pointer a
    reviewer disagrees with can be traced to the code that made it."""

    __table_args__ = (
        CheckConstraint("first_member >= 0", name="parameter_proposal_first_member_not_negative"),
        CheckConstraint("last_member >= first_member", name="parameter_proposal_span_not_reversed"),
        CheckConstraint(
            f"claimed_source IN ({CLAIMABLE_SOURCES_SQL})",
            name="parameter_proposal_claimed_source",
        ),
        # The regex rather than `btrim`, which strips spaces and nothing else (0035).
        CheckConstraint(
            "setting_name !~ '^[[:space:]]*$'", name="parameter_proposal_setting_not_blank"
        ),
        CheckConstraint(
            "proposer !~ '^[[:space:]]*$'", name="parameter_proposal_proposer_not_blank"
        ),
        CheckConstraint(
            "proposer_version !~ '^[[:space:]]*$'",
            name="parameter_proposal_proposer_version_not_blank",
        ),
    )
