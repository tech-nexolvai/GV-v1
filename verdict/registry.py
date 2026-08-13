"""Fixed registry for reviewed deterministic verdict operations.

Rules provide an operation name, never executable text. This module resolves that
name through a dictionary and validates operand shapes before callers perform any
arithmetic. It deliberately has no execution helper; orchestration belongs to the
verdict engine.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction
from types import MappingProxyType
from typing import Final

from units.measurement import Measurement
from verdict.outcomes import Outcome


class UnknownOperationError(LookupError):
    """Raised when a rule names an operation that is not registered."""


class RuleAuthoringError(ValueError):
    """Raised when an operation declaration or operand shape is invalid."""


class Arity(StrEnum):
    """Whether an operation input accepts one value or multiple values."""

    SCALAR = "scalar"
    LIST = "list"


@dataclass(frozen=True, slots=True)
class OperationResult:
    """Calculation facts returned by a verdict-producing operation.

    The engine combines these facts with sealed operand provenance and version metadata
    to build the final calculation trace. Operations receive resolved values rather than
    evidence records, so they cannot truthfully construct that trace themselves.
    """

    outcome: Outcome
    delta: Measurement | None
    intermediates: tuple[tuple[str, object], ...]
    comparison: str
    tolerance: Measurement | None


@dataclass(frozen=True, slots=True)
class DerivationResult:
    """An exact value produced for a later verdict operation."""

    value: Measurement | Fraction | int | str
    intermediates: tuple[tuple[str, object], ...]
    expression: str


class OperationKind(StrEnum):
    """Whether an operation decides a verdict or derives an intermediate value."""

    VERDICT = "verdict"
    DERIVATION = "derivation"


OperationFunction = Callable[..., OperationResult | DerivationResult]


@dataclass(frozen=True, slots=True)
class OperationSpec:
    """A reviewed operation's stable name, version, signature, and function."""

    name: str
    version: str
    operands: Mapping[str, Arity]
    fn: OperationFunction
    kind: OperationKind = OperationKind.VERDICT

    def __post_init__(self) -> None:
        """Validate and defensively freeze registry metadata."""

        if not self.name:
            raise RuleAuthoringError("operation name must not be empty")
        if not self.version:
            raise RuleAuthoringError(f"operation {self.name!r} must declare a version")
        if not self.operands:
            raise RuleAuthoringError(f"operation {self.name!r} must declare operands")
        if not callable(self.fn):
            raise RuleAuthoringError(f"operation {self.name!r} function must be callable")
        if not isinstance(self.kind, OperationKind):
            raise RuleAuthoringError(f"operation {self.name!r} has invalid kind")

        frozen_operands: dict[str, Arity] = {}
        for operand_name, arity in self.operands.items():
            if not operand_name:
                raise RuleAuthoringError(f"operation {self.name!r} contains an empty operand name")
            if not isinstance(arity, Arity):
                raise RuleAuthoringError(
                    f"operation {self.name!r} operand {operand_name!r} has invalid arity"
                )
            frozen_operands[operand_name] = arity
        object.__setattr__(self, "operands", MappingProxyType(frozen_operands))


REGISTRY: Final[dict[str, OperationSpec]] = {}


def register(spec: OperationSpec) -> None:
    """Register one reviewed operation without silently replacing another."""

    if spec.name in REGISTRY:
        raise RuleAuthoringError(f"operation {spec.name!r} is already registered")
    REGISTRY[spec.name] = spec


def resolve(name: str) -> OperationSpec:
    """Return a registered operation or raise; there is never a fallback."""

    try:
        return REGISTRY[name]
    except KeyError as error:
        raise UnknownOperationError(f"unknown operation: {name!r}") from error


def _is_multiple(value: object) -> bool:
    """Return whether a value has list arity, including identifier-keyed mappings."""

    return isinstance(value, Mapping) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))
    )


def validate_operands(spec: OperationSpec, operands: Mapping[str, object]) -> None:
    """Reject missing, extra, or wrong-arity operands before arithmetic runs."""

    expected_names = set(spec.operands)
    supplied_names = set(operands)
    missing = sorted(expected_names - supplied_names)
    unexpected = sorted(supplied_names - expected_names)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append(f"missing {missing!r}")
        if unexpected:
            details.append(f"unexpected {unexpected!r}")
        raise RuleAuthoringError(
            f"operation {spec.name!r} operands do not match its signature: " + "; ".join(details)
        )

    for name, arity in spec.operands.items():
        is_multiple = _is_multiple(operands[name])
        if arity is Arity.LIST and not is_multiple:
            raise RuleAuthoringError(
                f"operation {spec.name!r} operand {name!r} must have list arity"
            )
        if arity is Arity.SCALAR and is_multiple:
            raise RuleAuthoringError(
                f"operation {spec.name!r} operand {name!r} must have scalar arity"
            )
