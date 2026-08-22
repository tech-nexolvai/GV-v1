"""Authored derivation steps and their backward-only dependency validation.

A derivation names a registry operation and binds that operation's keyword operands to
rule inputs, parameters, or earlier derivations.  It contains no expression language and
does not execute anything.

Source: issue #54, ADR-0003 as amended by ADR-0008.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

type DerivationBinding = str | tuple[str, ...]


class Derivation(BaseModel):
    """A named intermediate computed from explicitly named operation operands.

    A string binds one value. A tuple binds an ordered sequence for operations such as
    ``sum``; duplicate references are intentionally preserved. Bindings are never
    flattened implicitly, so the eventual executor can distinguish scalar and list-valued
    operands without guessing.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    operation: str
    operands: Mapping[str, DerivationBinding]

    @field_validator("name")
    @classmethod
    def _name_is_present(cls, value: str) -> str:
        if not value:
            raise ValueError("a derivation name cannot be empty")
        return value

    @field_validator("operation")
    @classmethod
    def _looks_like_an_operation_name(cls, value: str) -> str:
        if not value or not value.replace("_", "").isalnum():
            raise ValueError(
                "operation must be a registry name such as 'sum', "
                f"got {value!r}. Derivations never contain executable text."
            )
        return value

    @model_validator(mode="after")
    def _bindings_are_present(self) -> Derivation:
        if not self.operands:
            raise ValueError(f"derivation {self.name!r} has no operands")
        for operand, binding in self.operands.items():
            if not operand:
                raise ValueError(f"derivation {self.name!r} has an empty operand name")
            references = binding_references(binding)
            if not references:
                raise ValueError(f"derivation {self.name!r} operand {operand!r} has no references")
            if any(not reference for reference in references):
                raise ValueError(
                    f"derivation {self.name!r} operand {operand!r} contains an empty reference"
                )
        return self


def binding_references(binding: DerivationBinding) -> tuple[str, ...]:
    """Return the references in one binding without changing their order or multiplicity."""
    return (binding,) if isinstance(binding, str) else binding


def validate_derivation_references(
    derivations: Sequence[Derivation],
    *,
    inputs: Collection[str],
    parameters: Collection[str],
    applicability_values: Collection[str] = (),
) -> None:
    """Validate a derivation graph by allowing references to known earlier names only.

    Backward-only authoring makes a cycle impossible, so there is no execution-time cycle
    detector. An unresolved reference names the offending derivation, keyword operand and
    reference to make a broken rule straightforward to repair.
    """
    known = set(inputs) | set(parameters) | set(applicability_values)
    for derivation in derivations:
        if derivation.name in known:
            raise ValueError(f"derivation {derivation.name!r} redefines an existing name")
        for operand, binding in derivation.operands.items():
            for reference in binding_references(binding):
                if reference not in known:
                    raise ValueError(
                        f"derivation {derivation.name!r} operand {operand!r} references "
                        f"{reference!r}, which is not an input, a parameter, an applicability "
                        "value, or an earlier derivation. Derivations may only look backwards, "
                        "which is what keeps the graph acyclic."
                    )
        known.add(derivation.name)
