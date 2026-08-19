"""Match candidates, approved matches, and the trail of who decided.

The matching boundary, stored so that it cannot be blurred. A candidate is a *proposal* that two
items correspond; an approved match is an assertion that they do. Those are different claims, and the
whole safety argument depends on a rule never being able to read the first as the second.

**Two tables, not a status column.** The same reasoning as `C1.5`: a single table with
`approved BOOLEAN` makes promotion a column update, and a column update is one careless
`UPDATE ... SET` away from turning every similarity guess in the database into a fact. Approval
requires writing a row that names its source.

**The lane is stored because a trigram hit is not an exact-ID hit.** `docs/DESIGN_EXTRACTION.md` §8
gives eight lanes and only the first two — exact identifier and alias — may auto-approve. A geometry
or dense-vector proposal is a candidate and nothing else. That is enforced here rather than trusted:
see `DETERMINISTIC_LANES`.

**`score` grants no authority.** It is diagnostic metadata for ranking and review. The in-memory
`retrieval.candidate.MatchCandidate` says the same thing and has no approval field at all; this table
keeps the property by keeping approval somewhere else.

**Revocation is recorded, never deletion.** A match that turned out to be wrong is part of the audit
trail of a review that has already happened. Deleting the row would erase the reason a past finding
said what it said.

Source: backend proposal §10.1 · Design: `docs/DESIGN_PLATFORM.md` §3.2, `docs/DESIGN_EXTRACTION.md`
§8 · Verification: `tests/db/test_matching_models.py`
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum, StrEnum
from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime
from vocabulary.lanes import Lane

#: Imported from ``vocabulary/`` rather than from ``retrieval.candidate``, which is where it used to
#: come from. ``DESIGN_PLATFORM.md`` §2 says ``app/models/`` must never import ``retrieval/``, and it
#: did — which put every module reaching ``app.models``, including the whole of ``app/api/``, one hop
#: from the retrieval package. Naming a lane needs no retrieval code, so the name lives where names
#: live. Same enum, same values, same SQL. See ``tests/api/test_no_heavy_work.py``.


def _sql_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


class ApprovalSource(StrEnum):
    """How a match came to be approved. There is no third way in.

    `DETERMINISTIC` means a check decided it by exact arithmetic on identifiers — the exact and alias
    lanes. `HUMAN` means a named reviewer did. A confidence score is neither, which is the point: the
    absence of a `MODEL` or `SCORE` member is the control.
    """

    DETERMINISTIC = "deterministic"
    HUMAN = "human"


#: The only lanes whose output a deterministic check may approve, per `DESIGN_EXTRACTION.md` §8.
#:
#: Lanes 3–8 — metadata, geometry, trigram, lexical, dense, fusion — are candidate-only. A dense
#: match auto-approved as "deterministic" would put a vector-similarity guess into the verdict path
#: carrying the authority of an exact identifier match, and nothing downstream could tell them apart.
DETERMINISTIC_LANES: frozenset[Lane] = frozenset({Lane.EXACT, Lane.ALIAS})

LANE_VALUES = _sql_values(Lane)
APPROVAL_SOURCE_VALUES = _sql_values(ApprovalSource)
DETERMINISTIC_LANE_VALUES = ", ".join(f"'{lane.value}'" for lane in sorted(DETERMINISTIC_LANES))


class MatchCandidate(Base, TimestampedUUID, Immutable):
    """A proposal that two items correspond. Advisory, always.

    `Immutable`, and with no approval column of any kind. A candidate that could be edited into an
    approved match is the blurring this schema exists to prevent.
    """

    __tablename__ = "match_candidates"

    left_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )
    right_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )

    lane: Mapped[str] = mapped_column(String(32), index=True)
    """Which of the eight lanes proposed it. Stored because the lanes carry different authority, and
    a schema that forgot the lane would make an exact-identifier hit indistinguishable from a
    vector-similarity one."""

    score: Mapped[Decimal | None] = mapped_column(Numeric(18, 9), default=None)
    """Diagnostic only. `NUMERIC` rather than `DOUBLE PRECISION` for consistency with every other
    stored number, though nothing decides anything from it — a score that granted authority would be
    a confidence threshold, and `AGENTS.md` §2.1 has none."""

    __table_args__ = (
        CheckConstraint(f"lane IN ({LANE_VALUES})", name="match_candidate_lane"),
        CheckConstraint("left_item_id <> right_item_id", name="match_candidate_distinct_items"),
        UniqueConstraint(
            "left_item_id", "right_item_id", "lane", name="uq_match_candidates_pair_lane"
        ),
        Index("ix_match_candidates_pair", "left_item_id", "right_item_id"),
    )


class ApprovedMatch(Base, TimestampedUUID, Immutable):
    """An assertion that two items correspond, and the record of who or what decided.

    A separate table from the candidate, so approval is an insert rather than a flag. The insert
    cannot happen without naming a source.
    """

    __tablename__ = "approved_matches"

    match_candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("match_candidates.id", ondelete="RESTRICT"), unique=True, index=True
    )
    """One approval per candidate. Approving the same proposal twice, possibly from two different
    sources, would leave "who decided this?" with two answers."""

    lane: Mapped[str] = mapped_column(String(32))
    """Copied from the candidate so the constraint below can see it.

    Denormalised deliberately, and it is the one place in this schema where that is right: a CHECK
    cannot reach another table, and the rule it enforces — only exact and alias may be approved
    deterministically — is the safety property this story exists for.

    The copy can therefore drift from the candidate's lane, and the database cannot stop it. That is
    a real limit, not a solved problem: closing it needs a trigger, which is `C1.12`'s territory.
    `test_an_approval_lane_must_match_its_candidate` asserts the writer keeps them equal, which is
    weaker than a constraint and is stated here so nobody mistakes it for one.
    """

    approval_source: Mapped[str] = mapped_column(String(32), index=True)
    approved_by: Mapped[str] = mapped_column(String(200))
    """A named human, or the check that decided. Never blank: an approval nobody signed is one
    nobody can be asked about."""

    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), default=None)
    """Set when a match is later found wrong. The row stays — it explains what a past finding was
    based on, and deleting it would erase the reason a decision looked right at the time."""

    revoked_reason: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (
        CheckConstraint(
            f"approval_source IN ({APPROVAL_SOURCE_VALUES})", name="match_approval_source"
        ),
        CheckConstraint(f"lane IN ({LANE_VALUES})", name="match_approval_lane"),
        # The safety property. DESIGN_EXTRACTION §8 permits auto-approval on the exact and alias
        # lanes only; the other six are candidate-only. Without this a dense-vector proposal could be
        # written as `deterministic` and would then be indistinguishable from an exact-ID match.
        CheckConstraint(
            f"approval_source <> 'deterministic' OR lane IN ({DETERMINISTIC_LANE_VALUES})",
            name="match_approval_deterministic_lane_only",
        ),
        CheckConstraint("approved_by <> ''", name="match_approval_approved_by_present"),
        # A revocation without a reason is a fact nobody can act on.
        CheckConstraint(
            "(revoked_at IS NULL AND revoked_reason IS NULL)"
            " OR (revoked_at IS NOT NULL AND revoked_reason IS NOT NULL AND revoked_reason <> '')",
            name="match_approval_revocation_is_explained",
        ),
    )


class MatchReviewEvent(Base, TimestampedUUID, Immutable):
    """What a reviewer did to a proposed match, kept forever.

    Append-only, like the correction ledger and for the same reason: the record of what we proposed
    and a human rejected is exactly what somebody would be tempted to tidy, and it is how we learn
    which lanes are worth trusting.
    """

    __tablename__ = "match_review_events"

    match_candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("match_candidates.id", ondelete="RESTRICT"), index=True
    )
    action: Mapped[str] = mapped_column(String(32))
    """`approved`, `rejected`, `deferred` — what the reviewer did, not what the system wanted."""

    reviewer: Mapped[str] = mapped_column(String(200))
    note: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (
        CheckConstraint("action <> ''", name="match_review_event_action_present"),
        CheckConstraint("reviewer <> ''", name="match_review_event_reviewer_present"),
        Index("ix_match_review_events_candidate_created", "match_candidate_id", "created_at"),
    )
