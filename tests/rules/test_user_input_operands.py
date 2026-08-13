"""`USER_INPUT` is the one operand a human types in.

The field wall-to-wall dimension is on no drawing: someone measures the room and enters the
number. That makes it the operand least protected by the evidence pipeline — no extractor read
it, no second route corroborated it — so what matters here is that it is always attributable and
can never be produced by a model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from fractions import Fraction

import pytest

from rules.parameters import (
    HUMAN_PROVENANCES,
    ParameterLayer,
    ParameterSet,
    Provenance,
    Quantity,
    UserInputError,
    is_user_input,
    resolve,
    user_input,
    user_input_set,
)
from rules.semantic_types import OperandSource
from units.measurement import Unit

PROJECT = "GV-2026-ABC"
WHEN = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


def _field_dimension(mm: str = "6012") -> Quantity:
    return Quantity(value=mm, unit=Unit.MM)


# ---------------------------------------------------------------------------
# A user input is attributable
# ---------------------------------------------------------------------------


def test_a_user_input_records_who_supplied_it_and_when() -> None:
    value = user_input(_field_dimension(), set_by="site team", set_at=WHEN)
    assert value.provenance is Provenance.MEASURED
    assert value.set_by == "site team"
    assert value.set_at == WHEN


def test_an_unattributed_user_input_is_rejected() -> None:
    """An unattributed value cannot be questioned later, which is the reason provenance is
    recorded at all."""
    with pytest.raises(ValueError, match="set_by"):
        user_input(_field_dimension(), set_by="   ", set_at=WHEN)


def test_the_value_stays_exact() -> None:
    value = user_input(Quantity(value="84 1/2", unit=Unit.INCH), set_by="Raj", set_at=WHEN)
    assert value.value.exact_value == Fraction(169, 2)


def test_a_float_cannot_be_typed_in() -> None:
    """ADR-0001 — the exactness rule applies to hand-entered numbers too."""
    with pytest.raises(ValueError, match="float"):
        user_input(Quantity(value=84.5, unit=Unit.INCH), set_by="Raj", set_at=WHEN)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# A model cannot supply one
# ---------------------------------------------------------------------------


def test_provenance_has_no_member_a_model_could_claim() -> None:
    """The structural guarantee behind "it cannot be supplied by any model or retrieval path".

    A model-sourced value is not merely discouraged, it is inexpressible: the vocabulary has no
    word for it. This test fails if someone later adds one, which would otherwise look like a
    harmless enum addition.
    """
    members = {p.value.lower() for p in Provenance}
    for forbidden in ("model", "ai", "vlm", "ocr", "extracted", "retrieval", "inferred", "auto"):
        assert not any(forbidden in m for m in members), (
            f"Provenance gained a member mentioning {forbidden!r}. A USER_INPUT operand must "
            "come from a person; anything a model produced is evidence and belongs in the "
            "canonical observation path where the evidence gate can qualify it."
        )


def test_provenance_is_exactly_the_three_human_or_standard_sources() -> None:
    assert {p.value for p in Provenance} == {
        "G.C / Client",
        "Company standard",
        "Measured",
    }


def test_a_company_standard_is_not_a_user_input() -> None:
    """A standard is a policy someone wrote down once, not a value measured for this review.
    Accepting it here would let a default masquerade as a measurement."""
    with pytest.raises(UserInputError, match="not a human source"):
        user_input(
            _field_dimension(), set_by="Raj", set_at=WHEN, provenance=Provenance.COMPANY_STANDARD
        )


def test_rules_cannot_reach_extraction_or_retrieval() -> None:
    """The other half of the structural guarantee: even if the vocabulary allowed it, this
    package has no route to a model's output. Asserted transitively by
    tests/test_verdict_isolation.py; pinned here because it is what makes this issue's
    acceptance criterion true.

    Parsed rather than string-matched. Searching the source text for "import extraction" also
    matches the module docstring explaining that it does not import extraction — the check
    would pass or fail on prose rather than on code.
    """
    import ast

    import rules.parameters as module

    source = module.__file__
    assert source is not None
    tree = ast.parse(Path(source).read_text(encoding="utf-8"), filename=source)

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"extraction", "retrieval", "boto3", "requests", "httpx", "langchain"}
    assert not (imported & forbidden), f"rules.parameters imports {sorted(imported & forbidden)}"


# ---------------------------------------------------------------------------
# It belongs to the run, not the project
# ---------------------------------------------------------------------------


def test_user_inputs_form_a_run_layer_set() -> None:
    """The room was that width on the day somebody stood in it. Recording it as a project
    setting would imply it applies to every later review of the same project."""
    parameters = user_input_set(
        PROJECT,
        1,
        {"field_dimension": user_input(_field_dimension(), set_by="site team", set_at=WHEN)},
    )
    assert parameters.layer is ParameterLayer.RUN
    assert parameters.project_id == PROJECT


def test_a_run_set_refuses_a_value_no_person_supplied() -> None:
    from rules.parameters import ParameterValue

    standard = ParameterValue(
        value=_field_dimension(),
        provenance=Provenance.COMPANY_STANDARD,
        set_by="policy",
        set_at=WHEN,
    )
    with pytest.raises(UserInputError, match="not a human source"):
        user_input_set(PROJECT, 1, {"field_dimension": standard})


def test_a_user_input_wins_over_the_project_and_global_layers() -> None:
    """The measured number is the one the check must use — that is the point of the run layer."""
    from rules.parameters import ParameterValue

    def _standard(mm: str) -> ParameterValue:
        return ParameterValue(
            value=Quantity(value=mm, unit=Unit.MM),
            provenance=Provenance.COMPANY_STANDARD,
            set_by="GV",
            set_at=WHEN,
        )

    resolved = resolve(
        "field_dimension",
        ParameterSet(None, ParameterLayer.GLOBAL, 1, {"field_dimension": _standard("6000")}),
        user_input_set(
            PROJECT,
            1,
            {"field_dimension": user_input(_field_dimension("6012"), set_by="site", set_at=WHEN)},
        ),
    )
    assert resolved.value.value.exact_value == Fraction(6012)
    assert resolved.layer is ParameterLayer.RUN


# ---------------------------------------------------------------------------
# It is distinguishable in a finding
# ---------------------------------------------------------------------------


def test_a_user_input_is_distinguishable_from_a_standard() -> None:
    """A reviewer checking a failed filler check needs to see at a glance that the field
    dimension was typed in, not read off the drawing — it is the number most likely to be
    wrong or out of date."""
    from rules.parameters import ParameterValue

    measured = resolve(
        "field_dimension",
        user_input_set(
            PROJECT,
            1,
            {"field_dimension": user_input(_field_dimension(), set_by="site", set_at=WHEN)},
        ),
    )
    standard = resolve(
        "door_thickness",
        ParameterSet(
            None,
            ParameterLayer.GLOBAL,
            1,
            {
                "door_thickness": ParameterValue(
                    value=Quantity(value="3/4", unit=Unit.INCH),
                    provenance=Provenance.COMPANY_STANDARD,
                    set_by="GV",
                    set_at=WHEN,
                )
            },
        ),
    )
    assert is_user_input(measured)
    assert not is_user_input(standard)


def test_explain_names_the_person_who_supplied_it() -> None:
    line = resolve(
        "field_dimension",
        user_input_set(
            PROJECT,
            1,
            {"field_dimension": user_input(_field_dimension(), set_by="site team", set_at=WHEN)},
        ),
    ).explain()
    assert "site team" in line
    assert "Measured" in line


# ---------------------------------------------------------------------------
# The enum the rule schema selects on
# ---------------------------------------------------------------------------


def test_user_input_is_an_operand_source_a_rule_can_name() -> None:
    assert OperandSource.USER_INPUT.value == "USER_INPUT"


def test_human_provenances_are_the_two_a_person_can_give() -> None:
    assert HUMAN_PROVENANCES == frozenset({Provenance.MEASURED, Provenance.GC_CLIENT})
