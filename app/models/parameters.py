"""Parameter sets and their values, persisted (#303, C1.13).

A7 built parameter sets **in memory only**: `rules/parameters.py` has `ParameterSet`, `ParameterValue` and
`ParameterSetStore`, and nothing wrote them down. No C1 story covered them, which is how a table the whole
resolver depends on came to be missing.

**Why it could not wait.** ADR-0016 requires a finding to pin the exact parameter-set version that judged
it, and `Finding.parameter_set_ids` already records content hashes. While the sets live only in memory,
those hashes point at nothing after a restart — the numbers behind a six-month-old verdict would be
unrecoverable, which is precisely the failure `AGENTS.md` §2.7 exists to prevent. A finding that cites
parameters nobody can reproduce is not a defensible finding.

**Exact values only, stored as a normalised pair.** `Quantity.value` is a `Fraction`, and `1/8` has to come
back as `Fraction(1, 8)` rather than `0.125` — the whole point of the units layer. So a value is stored as
`numerator` and `denominator` integers, never as a float and never as a decimal string that would have to be
parsed back. `Fraction` normalises on construction, so `2/16` and `1/8` store identically and two logically
equal sets cannot hash differently. `AGENTS.md` §2.4: a float in the decision path is the bug this avoids.

**The content hash is stored, not recomputed.** `set_id` is `rules.parameters.ParameterSet.set_id` — the
in-memory hash — written into the row. A test round-trips a set through the database and asserts the
recomputed hash still equals the stored one, which is the only way to know the two definitions agree.
Recomputing on read instead would hide a storage bug: the value would always match whatever was read.

**Immutable, so a changed value is a new set.** Both tables carry `Immutable`, and the migration attaches
the append-only trigger. Editing a parameter in place would change the numbers behind every finding that
already cited that set — silently, and after the fact.

Source: backend proposal §10.1; ADR-0006, ADR-0016 · Design: `docs/DESIGN.md` §3.9, §3.12;
`docs/DESIGN_PLATFORM.md` §3.1, §3.3 · Verification: `tests/db/test_parameter_models.py`
"""

from __future__ import annotations

from datetime import datetime
from fractions import Fraction
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID, UTCDateTime
from rules.parameters import ParameterLayer, Provenance
from rules.parameters import ParameterSet as InMemoryParameterSet
from rules.parameters import ParameterValue as InMemoryParameterValue
from rules.schema import Quantity
from units.measurement import Unit

__all__ = [
    "ParameterSet",
    "ParameterValue",
    "from_rows",
    "to_rows",
]

#: The layers a stored set may claim, rendered for a SQL `IN`.
#:
#: Built from `rules/parameters.py` rather than retyped, following `app/models/rules.py`'s treatment of the
#: reserved discriminators: two lists of allowed names drift, and the copy that drifts is the one nobody is
#: looking at when a set is written under a layer the resolver does not know.
_LAYER_SQL = ", ".join(
    f"'{layer.value}'" for layer in sorted(ParameterLayer, key=lambda m: m.value)
)

#: Same argument for provenance. A value whose provenance the resolver cannot read is a value whose
#: authority is unknown, and `rules/parameters.py` distinguishes human-supplied from derived.
_PROVENANCE_SQL = ", ".join(f"'{p.value}'" for p in sorted(Provenance, key=lambda m: m.value))


class ParameterSet(Base, TimestampedUUID, Immutable):
    """One version of one layer's parameters, identified by its content.

    Named the same as `rules.parameters.ParameterSet` on purpose, following `Finding`, which is a domain
    object in `verdict/` and a row in `app/models/verdicts.py`. The row is not the value object: this one
    has a surrogate key, a creation time and foreign keys, and lives in a package `rules/` may not import.
    """

    __tablename__ = "parameter_sets"

    set_id: Mapped[str] = mapped_column(String(71), index=True)
    """The content hash from `rules.parameters.ParameterSet.set_id`, as `sha256:<64 hex>`.

    Unique, because two rows sharing a content hash would mean the same numbers stored twice — and a
    finding citing that hash could not say which row it meant. 71 characters is the prefixed form's exact
    length, matching `model_invocations.node_invocation_key`."""

    project_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    """`NULL` for a global layer — the defaults belong to no project.

    `RESTRICT` rather than `CASCADE`: deleting a project must not delete the parameters that judged its
    findings, because the findings outlive the project record and still cite them."""

    layer: Mapped[str] = mapped_column(String(50), index=True)
    version: Mapped[int] = mapped_column()

    __table_args__ = (
        UniqueConstraint(
            "project_id", "layer", "version", name="uq_parameter_sets_project_layer_version"
        ),
        # Named explicitly rather than written as `unique=True` on the column. `unique=True` plus
        # `index=True` produces one *unique index*; migration 0027 created a unique *constraint* and
        # a separate plain index, and alembic compares those as different objects — which is what put
        # `main` red. The database is the authority here, so the model says what the database has.
        UniqueConstraint("set_id", name="uq_parameter_sets_set_id"),
        CheckConstraint(f"layer IN ({_LAYER_SQL})", name="layer_in_vocabulary"),
        CheckConstraint("version >= 1", name="version_positive"),
        CheckConstraint("set_id ~ '^sha256:[0-9a-f]{64}$'", name="set_id_is_a_digest"),
        # A global layer has no project; a project layer must name one. Either mistake makes the
        # resolver's precedence order silently wrong for one project.
        CheckConstraint(
            "(layer = 'global') = (project_id IS NULL)",
            name="global_has_no_project",
        ),
        Index("ix_parameter_sets_project_layer", "project_id", "layer"),
    )


class ParameterValue(Base, TimestampedUUID, Immutable):
    """One named parameter inside one set, with its exact value and who set it."""

    __tablename__ = "parameter_values"

    parameter_set_id: Mapped[UUID] = mapped_column(
        ForeignKey("parameter_sets.id", ondelete="RESTRICT"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), index=True)

    numerator: Mapped[int] = mapped_column(BigInteger())
    denominator: Mapped[int] = mapped_column(BigInteger())
    """The value as an exact fraction. **Two integers, never a float.**

    `1/8` is `1` over `8` and returns as `Fraction(1, 8)`. A `float` column would store `0.125` and lose
    the authored form; a `NUMERIC` would keep the decimal but not the fraction, and `1/3` has no decimal
    form at all. `AGENTS.md` §2.4 forbids a float in the decision path, and a parameter is squarely in it."""

    unit: Mapped[str] = mapped_column(String(20))
    provenance: Mapped[str] = mapped_column(String(50))
    set_by: Mapped[str] = mapped_column(String(200))
    """Who set it. A parameter with no author is a number nobody can be asked about."""

    set_at: Mapped[datetime] = mapped_column(UTCDateTime())
    """When — and this is inside the content hash, unlike a rule snapshot's publication time.

    `rules/parameters.py` explains why: here the timestamp is authored data, so two sets recording the same
    number measured on different days are genuinely different records."""

    __table_args__ = (
        UniqueConstraint("parameter_set_id", "name", name="uq_parameter_values_set_name"),
        CheckConstraint("denominator > 0", name="denominator_positive"),
        CheckConstraint("name <> ''", name="name_present"),
        CheckConstraint("set_by <> ''", name="set_by_present"),
        CheckConstraint(f"provenance IN ({_PROVENANCE_SQL})", name="provenance_in_vocabulary"),
    )

    @property
    def exact_value(self) -> Fraction:
        """The stored pair as a `Fraction` — normalised, because `Fraction` normalises."""
        return Fraction(self.numerator, self.denominator)


def to_rows(parameters: InMemoryParameterSet) -> tuple[ParameterSet, list[ParameterValue]]:
    """Turn an in-memory set into the rows that store it, keeping its content hash.

    The hash is taken from the value object rather than recomputed here. One definition of "what these
    numbers are" — a second implementation would be a second answer, and the two would disagree the first
    time either changed.

    Reading back is `from_rows`, and `tests/db/test_parameter_models.py` round-trips a set through
    PostgreSQL and asserts the recomputed hash still equals the stored one.
    """
    stored = ParameterSet(
        set_id=parameters.set_id,
        project_id=UUID(parameters.project_id) if parameters.project_id is not None else None,
        layer=parameters.layer.value,
        version=parameters.version,
    )
    values = [
        ParameterValue(
            parameter_set_id=stored.id,
            name=name,
            numerator=value.value.exact_value.numerator,
            denominator=value.value.exact_value.denominator,
            unit=str(value.value.unit),
            provenance=value.provenance.value,
            set_by=value.set_by,
            set_at=value.set_at,
        )
        # Sorted so two runs insert in one order. The hash does not depend on it — `canonical_json`
        # sorts — but a diff of two migrations' output should not depend on dictionary order either.
        for name, value in sorted(parameters.parameters.items())
    ]
    return stored, values


def from_rows(stored: ParameterSet, values: list[ParameterValue]) -> InMemoryParameterSet:
    """Rebuild the in-memory set from its rows.

    Rebuilt through the real constructors, so every invariant `rules/parameters.py` enforces is enforced
    again on the way out. A row that somehow violated one fails here rather than becoming a value object
    the resolver trusts.
    """
    return InMemoryParameterSet(
        project_id=str(stored.project_id) if stored.project_id is not None else None,
        layer=ParameterLayer(stored.layer),
        version=stored.version,
        parameters={
            value.name: InMemoryParameterValue(
                # `Unit(...)` rather than the raw string: `Quantity` takes a `Unit`, and letting a
                # stored string through would defer the failure to whoever did arithmetic with it.
                value=Quantity(value=value.exact_value, unit=Unit(value.unit)),
                provenance=Provenance(value.provenance),
                set_by=value.set_by,
                set_at=value.set_at,
            )
            for value in values
        },
    )
