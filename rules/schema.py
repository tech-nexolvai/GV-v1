"""The typed model of a rule, and the validation that rejects a malformed one.

A rule is a YAML file a human authors describing one check. This module defines the shape
those files must conform to; it does not execute anything.

**Why the strictness here is a safety control rather than a style choice.** The dangerous
input is not a malformed rule that crashes — it is a *nearly* valid one that runs. If an
author writes ``tolerence:`` instead of ``tolerance:`` and the model quietly ignores unknown
fields, the rule executes with no tolerance at all and passes everything. That is a false
PASS created by a typo, and false PASS is this project's primary safety metric. Hence
``extra="forbid"`` on every model.

Every model is frozen. A rule that could be mutated after validation is a rule that could be
different at execution time from the one that was reviewed.

See `docs/RULE_ENGINE_SPEC.md` §3 and `docs/DESIGN.md` §3.8.
"""

from __future__ import annotations

from enum import StrEnum
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Measurement, Unit, to_exact_fraction
from verdict.outcomes import Outcome, Severity

# ---------------------------------------------------------------------------
# Enumerations used by the selectors
# ---------------------------------------------------------------------------


class Cardinality(StrEnum):
    """How many observations an input is expected to resolve to."""

    ONE = "one"
    MANY = "many"


class Scope(StrEnum):
    """How far the resolver may look when gathering an input."""

    SAME_ASSEMBLY = "same_assembly"
    SAME_VIEW = "same_view"
    PACKAGE = "package"


class CheckType(StrEnum):
    """Where a rule's two sides come from (`RULE_ENGINE_SPEC.md` §1)."""

    INTERNAL = "internal"
    """Both operands from the shop drawing, e.g. countertop against its own cabinets."""

    ARCH_VS_SHOP = "arch_vs_shop"
    """Expected from the approved architectural set, actual from the shop drawing."""

    GLOBAL = "global"
    """Actual from the shop drawing against a fixed standard, with no second document."""


class ParameterScope(StrEnum):
    """Which layer may set a parameter (`RULE_ENGINE_SPEC.md` §3b)."""

    PROJECT = "project"
    RUN = "run"


#: Sentinel for a tolerance the client has not yet supplied.
#:
#: No tolerance value appears anywhere in the client material (see issue #10). A rule may be
#: authored with this so the structure can be reviewed and tested, but it can never produce
#: PASS or FAIL — the engine must return REVIEW_REQUIRED instead.
#:
#: An unset tolerance is **not** zero. Zero fails everything; a guessed one passes the wrong
#: things. Both are worse than abstaining.
TOLERANCE_UNCONFIRMED = "UNCONFIRMED"


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class Quantity(BaseModel):
    """An authored value with its unit, e.g. ``{value: "1/8", unit: in}``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Fraction
    unit: Unit

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: object) -> Fraction:
        return to_exact_fraction(value)

    def as_measurement(self) -> Measurement:
        """Return this quantity as a :class:`Measurement`, preserving the authored unit."""
        return Measurement(self.exact_value, self.unit, None)

    @property
    def exact_value(self) -> Fraction:
        return self.value


class Tolerance(BaseModel):
    """An allowed error, or an explicit admission that we do not know it yet."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: Fraction | Literal["UNCONFIRMED"]
    unit: Unit | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _coerce_value(cls, value: object) -> Fraction | str:
        if value == TOLERANCE_UNCONFIRMED:
            return TOLERANCE_UNCONFIRMED
        return to_exact_fraction(value)

    @model_validator(mode="after")
    def _unit_required_unless_unconfirmed(self) -> Tolerance:
        if self.is_confirmed and self.unit is None:
            raise ValueError("a confirmed tolerance must state its unit")
        return self

    @property
    def is_confirmed(self) -> bool:
        """False when the client has not yet supplied this tolerance."""
        return self.value != TOLERANCE_UNCONFIRMED

    def as_measurement(self) -> Measurement:
        """Return the tolerance as a Measurement.

        Raises if the tolerance is unconfirmed: an unconfirmed tolerance has no value, and
        substituting one would be inventing a number the client never gave us.
        """
        if not self.is_confirmed or self.unit is None:
            raise ValueError(
                "tolerance is unconfirmed; the check must return REVIEW_REQUIRED "
                "rather than compare against a guessed value"
            )
        assert isinstance(self.value, Fraction)
        return Measurement(self.value, self.unit, None)


# ---------------------------------------------------------------------------
# Rule parts
# ---------------------------------------------------------------------------


class InputSelector(BaseModel):
    """Where an operand comes from and how many are expected (§3a)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: OperandSource
    semantic_type: SemanticType
    scope: Scope = Scope.SAME_ASSEMBLY
    cardinality: Cardinality = Cardinality.ONE


class Parameter(BaseModel):
    """A project-tunable value that appears on no drawing (§3b).

    The default is a value a human confirms, never a silent fallback. `AGENTS.md` §2.4 — a
    missing parameter becomes NOT_FOUND rather than a plausible number.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    default: Quantity | None = None
    scope: ParameterScope = ParameterScope.PROJECT


class ApplicabilityVariant(BaseModel):
    """One branch of a discriminator, carrying its own tolerance and extras (§3c)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    when: str
    tolerance: Tolerance
    extras: dict[str, int] = Field(default_factory=dict)


class Applicability(BaseModel):
    """The discriminator that selects a variant, e.g. ``wall_config``.

    If the discriminator cannot be established from the drawing or the reviewer, the check
    abstains — we never guess which layout a drawing shows.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    discriminator: str
    variants: tuple[ApplicabilityVariant, ...]

    @model_validator(mode="after")
    def _variants_are_present_and_distinct(self) -> Applicability:
        if not self.variants:
            raise ValueError("applicability must declare at least one variant")
        seen = [v.when for v in self.variants]
        duplicates = {w for w in seen if seen.count(w) > 1}
        if duplicates:
            raise ValueError(f"duplicate applicability variant(s): {sorted(duplicates)}")
        return self

    def variant_for(self, value: str) -> ApplicabilityVariant | None:
        """Return the matching variant, or None when no branch covers the value.

        None means REVIEW_REQUIRED, never a default branch. A rule that silently fell back
        to its first variant would apply the wrong tolerance to the wrong layout.
        """
        return next((v for v in self.variants if v.when == value), None)


class GlobalApplicability(BaseModel):
    """An explicit declaration that a rule applies to every item of its product type.

    A rule with no layout discriminator — a minimum filler width, say — must still *say* that
    it applies unconditionally. It cannot simply omit its applicability (ADR-0007).

    Leaving the field out would be absence silently becoming a positive, which is the one
    thing this project refuses everywhere else: a missing second reading is
    ``NOT_CORROBORATED`` rather than consistent, a missing rounding token raises rather than
    defaults, and a changed rule needs a version bump rather than an inferred one. Here the
    stake is higher than usual — a forgotten discriminator read as "applies to everything"
    would apply one layout's tolerance to every layout, rather than to none.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["global"]


class Derivation(BaseModel):
    """A named intermediate computed by a typed operation (ADR-0003).

    Inputs may reference an input, a parameter, or an *earlier* derivation. Acyclicity is
    validated at publish time by :meth:`Rule._derivations_are_acyclic`, never at execution.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    operation: str
    inputs: tuple[str, ...]

    @model_validator(mode="after")
    def _has_inputs(self) -> Derivation:
        if not self.inputs:
            raise ValueError(f"derivation {self.name!r} has no inputs")
        return self


class OperationRef(BaseModel):
    """The operation to execute, named rather than supplied as code.

    `AGENTS.md` §2.2 — a rule selects from the typed registry by name. There is deliberately
    no field here that could carry an expression.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: str
    operands: dict[str, str] = Field(default_factory=dict)
    tolerance: Tolerance | None = None

    @field_validator("type")
    @classmethod
    def _looks_like_an_operation_name(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError(
                f"operation must be a registry name such as 'sum_within_tolerance', "
                f"got {value!r}. Rules never contain executable text."
            )
        return value


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

_SEMVER = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]


class Rule(BaseModel):
    """One published check.

    Frozen and strict: unknown fields are an authoring error, not something to ignore.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    version: _SEMVER
    product_type: ProductType
    check_type: CheckType
    severity: Severity
    arithmetic_unit: Unit

    cross_unit_allowance: Quantity | None = None
    """How much two renderings of the same number may differ before this check refuses to
    combine them (ADR-0011).

    Deliberately **not** the tolerance. A tolerance is how much the drawing may be *wrong*; this
    is how much the drawing's own mm and inch renderings of one dimension may *disagree*. On the
    real GV drawing that noise reaches 1.600 mm against a 1/16 inch tolerance of 1.5875 mm, so
    letting a tolerance absorb it would pass a drawing that is out of tolerance.

    ``None`` means mixing is refused: the check returns REVIEW REQUIRED rather than converting
    silently. The safe default is abstention, so a rule that says nothing gets it.
    """

    name: str = ""
    description: str = ""

    inputs: dict[str, InputSelector] = Field(default_factory=dict)
    parameters: dict[str, Parameter] = Field(default_factory=dict)
    derivations: tuple[Derivation, ...] = ()
    applicability: Applicability | GlobalApplicability
    operation: OperationRef

    on_missing: Outcome = Outcome.NOT_FOUND
    on_ambiguous: Outcome = Outcome.REVIEW_REQUIRED

    # -- validation -------------------------------------------------------

    @field_validator("on_missing", "on_ambiguous")
    @classmethod
    def _abstention_outcomes_only(cls, value: Outcome) -> Outcome:
        """A missing or ambiguous input may never be configured to PASS.

        `AGENTS.md` §2.4 — never turn a missing or uncertain value into a pass. Allowing a
        rule to declare ``on_missing: PASS`` would let a single YAML line defeat the whole
        evidence gate.
        """
        if value in (Outcome.PASS, Outcome.FAIL):
            raise ValueError(
                f"on_missing/on_ambiguous must abstain, got {value.value}. "
                "A missing or ambiguous input is not a decision."
            )
        return value

    @model_validator(mode="after")
    def _derivations_are_acyclic(self) -> Rule:
        """Reject a derivation graph that cannot be evaluated.

        Checked at publish time rather than at execution: a cycle is an authoring error, and
        discovering it mid-verdict would mean failing a package for a bug in our rulebook.
        """
        known = set(self.inputs) | set(self.parameters)
        for derivation in self.derivations:
            unknown = [i for i in derivation.inputs if i not in known]
            if unknown:
                raise ValueError(
                    f"derivation {derivation.name!r} references {unknown!r}, which is not an "
                    "input, a parameter, or an earlier derivation. Derivations may only look "
                    "backwards, which is what keeps the graph acyclic."
                )
            if derivation.name in known:
                raise ValueError(f"derivation {derivation.name!r} redefines an existing name")
            known.add(derivation.name)
        return self

    @model_validator(mode="after")
    def _operands_resolve(self) -> Rule:
        """Every operand the operation names must exist somewhere in the rule."""
        known = set(self.inputs) | set(self.parameters) | {d.name for d in self.derivations}
        unknown = sorted({ref for ref in self.operation.operands.values() if ref not in known})
        if unknown:
            raise ValueError(
                f"operation references unknown operand(s) {unknown}. "
                f"Known names: {sorted(known)}"
            )
        return self

    # -- helpers ----------------------------------------------------------

    @property
    def has_confirmed_tolerance(self) -> bool:
        """True when every tolerance in this rule carries a real client-supplied value.

        A rule without one is publishable for development but must never produce PASS or
        FAIL. The release gate uses this to keep unconfirmed rules out of production.
        """
        tolerances = (
            [v.tolerance for v in self.applicability.variants]
            if isinstance(self.applicability, Applicability)
            else []
        )
        if self.operation.tolerance is not None:
            tolerances.append(self.operation.tolerance)
        return bool(tolerances) and all(t.is_confirmed for t in tolerances)


def rule_json_schema() -> dict[str, object]:
    """Return the JSON Schema for a rule, generated from the models.

    Generated rather than hand-written so the schema and the validation cannot drift — a
    hand-maintained copy would eventually permit something the models reject, or vice versa.
    """
    return Rule.model_json_schema()
