"""Rule definitions, their published snapshots, and what each snapshot applies to.

`rules/snapshot.py` already builds content-addressed snapshots in memory. This gives them a home
without changing what they are: the same `sha256:…` identity, the same canonical JSON, the same
"one content hash per version" rule. Nothing here re-decides anything — a row is a place to keep a
snapshot, not a second opinion about what a snapshot is.

**Why the canonical JSON is stored beside the hash.** An identifier you cannot check is a promise
rather than a fact. Keeping the exact bytes that were hashed means any reader can recompute the
digest and find out whether the row still says what it said when it was published. Storing only the
hash would make tampering undetectable precisely where detection matters most.

**Why applicability is a child table rather than columns.** The obvious schema gives each
discriminator its own column — `wall_config`, and later `material`, and later `mount_type`.
ADR-0007 considered exactly that and rejected it: `Applicability.discriminator` is a free string, so
a rule keyed on something new would need the shape changed before it could be stored at all. In a
database that means a migration per discriminator, and `AGENTS.md` forbids editing a shipped one. A
row per discriminator stores any of them without schema change, which is the same reason
`CheckContext` carries a mapping instead of keyword arguments.

**What is deliberately absent.** There is no project column, on either table. ADR-0006 settles that
rules are GV's own standards and ADR-0007 carries project scope through the resolver "never used to
filter". A project column would quietly recreate per-project rule sets — project variation belongs
in parameter sets (ADR-0009), which is a different table answering a different question.

Source: backend proposal §10.1; `AGENTS.md` §2.7, §6 · corrections in
`docs/decisions/C1_8_APPLICABILITY_SCOPE.md` · Verification: `tests/db/test_rule_models.py`
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from rules.schema import RESERVED_DISCRIMINATORS

#: The reserved names, rendered for a SQL `NOT IN`. Built from `rules/schema.py` rather than retyped
#: here: two lists of forbidden names drift, and the copy that drifts is the one nobody is looking at
#: when a vendor-keyed rule gets written. `tests/db/test_rule_models.py` asserts they still agree.
_RESERVED_SQL = ", ".join(f"'{name}'" for name in sorted(RESERVED_DISCRIMINATORS))


class RuleDefinition(Base, TimestampedUUID):
    """A rule's identity, stable across every version of it.

    `CT-WIDTH-001` is one row here and many rows in `rule_snapshots`. Separating them is what lets a
    finding cite the exact snapshot it used while a reader still asks "how has this check changed?"
    and gets an answer.

    No project column: see the module docstring. A rule belongs to GV, not to a project.
    """

    __tablename__ = "rule_definitions"

    rule_id: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    """The authored identifier, e.g. `CT-WIDTH-001`. Unique — two definitions sharing one id would
    make "the latest version of this rule" ambiguous, and version selection depends on it."""

    __table_args__ = (CheckConstraint("rule_id <> ''", name="definition_rule_id_present"),)


class RuleSnapshot(Base, TimestampedUUID, Immutable):
    """One published version of a rule, identified by the hash of its own content.

    `Immutable` for the reason ADR-0004 gives: a snapshot that can be edited is not a snapshot, and
    every finding in the system cites one of these to explain the decision it made. Publishing a
    change creates a new row; nothing is ever amended in place.
    """

    __tablename__ = "rule_snapshots"

    rule_definition_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_definitions.id", ondelete="RESTRICT"), index=True
    )

    snapshot_id: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    """The content hash, stored exactly as `rules/snapshot.py` emits it — `sha256:<hex>`.

    The prefix is kept rather than stripped. It names the algorithm, so a future change of hash
    stays legible instead of producing two indistinguishable families of identifier.
    """

    version: Mapped[str] = mapped_column(String(50))
    """Semver. `(rule_definition_id, version)` is unique because ADR-0006 selects the effective rule
    by highest version — which is only well defined if a version names one thing."""

    canonical_json: Mapped[str] = mapped_column(String())
    """The exact RFC 8785 bytes that were hashed, so `snapshot_id` is re-derivable from the row.

    Not a convenience copy. Without it the identifier is unverifiable at rest: a row could drift
    from its own hash and nothing could tell.
    """

    product_type: Mapped[str] = mapped_column(String(100), index=True)
    """Which category of thing this rule checks. `NOT NULL` — a rule always has one, and
    `CheckContext.product_type` is non-optional. Values come from the `ProductType` vocabulary;
    ADR-0007 made it controlled so a typo cannot publish cleanly and then match nothing."""

    check_type: Mapped[str] = mapped_column(String(100))
    """Which documents the check reads. An attribute of the rule, **not** a resolver key — it
    decides what loads, not which rule applies to an item (`C1_8_APPLICABILITY_SCOPE.md` D4)."""

    unconfirmed_tolerance_count: Mapped[int] = mapped_column()
    """How many of this snapshot's tolerances are still unconfirmed.

    Stored rather than derived so an unreleasable rule is findable by query. A rule with an
    unconfirmed tolerance publishes — authoring is allowed to run ahead of the client — but it can
    only ever return REVIEW REQUIRED, and the release gate has to be able to see that without
    parsing every snapshot's JSON.
    """

    __table_args__ = (
        UniqueConstraint(
            "rule_definition_id", "version", name="uq_rule_snapshots_definition_version"
        ),
        CheckConstraint("snapshot_id LIKE 'sha256:%'", name="snapshot_id_is_prefixed"),
        CheckConstraint("canonical_json <> ''", name="snapshot_canonical_json_present"),
        CheckConstraint("version <> ''", name="snapshot_version_present"),
        CheckConstraint("product_type <> ''", name="snapshot_product_type_present"),
        CheckConstraint(
            "unconfirmed_tolerance_count >= 0", name="snapshot_unconfirmed_count_not_negative"
        ),
    )


class RuleApplicabilityScope(Base, TimestampedUUID, Immutable):
    """One discriminator value a snapshot covers — `wall_config = back_left_right`.

    A row per discriminator, not a column per discriminator. See the module docstring; the short
    version is that ADR-0007 rejected the column form because a rule keyed on something new could
    not be stored until somebody changed the shape.

    `Immutable`, like the snapshot it belongs to: what a published rule applies to is part of what
    was published.
    """

    __tablename__ = "rule_applicability_scopes"

    rule_snapshot_id: Mapped[UUID] = mapped_column(
        ForeignKey("rule_snapshots.id", ondelete="RESTRICT"), index=True
    )

    discriminator: Mapped[str] = mapped_column(String(100), index=True)
    """The name the rule declares, e.g. `wall_config`. Free-form by design — but never a name that
    identifies who submitted the drawing. `RESERVED_DISCRIMINATORS` in `rules/schema.py` bans those
    at authoring time, and `reserved_discriminator_names` below keeps this table from becoming a way
    around it."""

    value: Mapped[str] = mapped_column(String(200), index=True)
    """The variant's `when` — the value of the discriminator this row covers."""

    __table_args__ = (
        UniqueConstraint(
            "rule_snapshot_id",
            "discriminator",
            "value",
            name="uq_rule_applicability_scopes_snapshot_discriminator_value",
        ),
        CheckConstraint("discriminator <> ''", name="scope_discriminator_present"),
        # ADR-0006 forbids keying a rule on who submitted the drawing. That is enforced at authoring
        # time in `rules/schema.py`, but this table would otherwise be a way in behind it — a row
        # written by any other path, now or later, would carry a vendor-keyed rule that the resolver
        # would honour. The database refuses instead.
        CheckConstraint(
            f"discriminator NOT IN ({_RESERVED_SQL})",
            name="scope_discriminator_not_reserved",
        ),
        CheckConstraint("value <> ''", name="scope_value_present"),
        Index("ix_rule_applicability_scopes_lookup", "discriminator", "value"),
    )
