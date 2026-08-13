"""A parameter no layer supplies is NOT FOUND. It is never a default.

`AGENTS.md` §2.4 — never invent a value. The reason this deserves its own test file rather than
a line in another one is the failure mode: a defaulted parameter does not crash and does not
look wrong. It produces a confident PASS on a check that was never really performed, which is
the single most damaging thing this codebase can do.

These tests are therefore mostly about proving an absence — that no fallback path exists
anywhere in resolution, and that a missing value cannot reach a verdict.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from rules.parameters import (
    ParameterLayer,
    ParameterMissingError,
    ParameterSet,
    ParameterValue,
    Provenance,
    Quantity,
    outcome_for_missing_parameter,
    resolve,
    resolve_required,
    seed_company_standards,
)
from units.measurement import Unit
from verdict.outcomes import Outcome, is_decision

PROJECT = "GV-2026-ABC"
WHEN = datetime(2026, 8, 13, 9, 30, tzinfo=UTC)


def _standard(amount: str, unit: Unit = Unit.INCH) -> ParameterValue:
    return ParameterValue(
        value=Quantity(value=amount, unit=unit),
        provenance=Provenance.COMPANY_STANDARD,
        set_by="GV",
        set_at=WHEN,
    )


def _project(**parameters: ParameterValue) -> ParameterSet:
    return ParameterSet(PROJECT, ParameterLayer.PROJECT, 1, parameters)


# ---------------------------------------------------------------------------
# Missing yields NOT FOUND
# ---------------------------------------------------------------------------


def test_a_missing_parameter_maps_to_not_found() -> None:
    assert outcome_for_missing_parameter() is Outcome.NOT_FOUND


def test_resolve_required_returns_not_found_rather_than_a_value() -> None:
    assert resolve_required("filler_width_max", _project()) is Outcome.NOT_FOUND


def test_not_found_is_chosen_over_review_required() -> None:
    """The two say different things to a reviewer. NOT FOUND means a required input is absent,
    which sends them to supply it. REVIEW REQUIRED means the inputs conflict or need judgement,
    which sends them to adjudicate. A missing parameter is the former."""
    assert outcome_for_missing_parameter() is not Outcome.REVIEW_REQUIRED


def test_resolve_required_still_returns_the_value_when_one_exists() -> None:
    resolved = resolve_required("door_thickness", _project(door_thickness=_standard("3/4")))
    assert not isinstance(resolved, Outcome)
    assert resolved.value.value.exact_value == Fraction(3, 4)


# ---------------------------------------------------------------------------
# A missing parameter cannot produce PASS
# ---------------------------------------------------------------------------


def test_a_missing_parameter_can_never_be_counted_as_a_verdict() -> None:
    """The engine does not exist yet (#47), so this is proven at the boundary: the outcome a
    missing parameter produces is not a decision, whatever the engine later does with it."""
    outcome = outcome_for_missing_parameter()
    assert not is_decision(outcome)
    assert outcome is not Outcome.PASS
    assert outcome is not Outcome.FAIL


def test_no_resolution_path_returns_pass() -> None:
    """Every route out of resolution is either a real value or an abstention."""
    missing = resolve_required("nothing_sets_this", _project())
    assert missing is Outcome.NOT_FOUND
    assert not is_decision(missing)


# ---------------------------------------------------------------------------
# No fallback exists anywhere in the resolution path
# ---------------------------------------------------------------------------


def _resolution_source() -> ast.Module:
    import rules.parameters as module

    source = module.__file__
    assert source is not None
    return ast.parse(Path(source).read_text(encoding="utf-8"), filename=source)


def test_resolution_never_calls_get_with_a_fallback() -> None:
    """`mapping.get(name, something)` is how a default sneaks in — it reads as a lookup and
    behaves as an invention.

    Parsed rather than grepped: the docstrings in this module discuss defaults at length, so a
    text search would match the explanation of the rule rather than a breach of it.
    """
    offenders = [
        f"line {node.lineno}"
        for node in ast.walk(_resolution_source())
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get"
        and len(node.args) > 1
    ]
    assert not offenders, (
        f"a defaulted lookup in the resolution path at {offenders}. A missing parameter must "
        "become NOT_FOUND, not a plausible number — AGENTS.md §2.4."
    )


def test_a_lookup_is_never_given_an_or_style_fallback() -> None:
    """`lookup(name) or DEFAULT` is the other shape a default arrives in, and it is worse than
    a defaulted `.get`: it also replaces a legitimate zero.

    Narrowed to *lookups* deliberately. An earlier version flagged any `or "constant"` and
    caught three false positives — a `None`-guard, a display label and an error message. A guard
    that cries wolf gets deleted, so it checks the shape that actually invents a value: a
    retrieval whose empty result is silently replaced.
    """
    offenders: list[str] = []
    for node in ast.walk(_resolution_source()):
        if not (isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)):
            continue
        for value in node.values[:-1]:
            is_lookup = isinstance(value, ast.Call) and (
                (isinstance(value.func, ast.Attribute) and value.func.attr in {"get", "resolve"})
                or (isinstance(value.func, ast.Name) and value.func.id in {"resolve", "get"})
            )
            if is_lookup:
                offenders.append(f"line {node.lineno}")
    assert not offenders, (
        f"a lookup with an 'or default' fallback at {offenders}. A missing parameter must "
        "become NOT_FOUND, not a plausible number — AGENTS.md §2.4."
    )


def test_the_missing_error_names_where_it_looked() -> None:
    """A reviewer told only "missing" cannot tell whether the layer they expected was consulted."""
    with pytest.raises(ParameterMissingError, match="global, project"):
        resolve(
            "field_cut",
            ParameterSet(None, ParameterLayer.GLOBAL, 1, {}),
            _project(),
        )


# ---------------------------------------------------------------------------
# "Typical" values are seeded standards a human confirmed, not silent fallbacks
# ---------------------------------------------------------------------------


def test_the_clients_typical_values_are_seeded_as_attributed_standards() -> None:
    """The distinction the acceptance criterion is really about.

    As a code default, a typical value is applied silently to every project and nobody can tell
    afterwards whether a number was chosen or merely assumed. As a seeded standard it carries
    provenance, names who set it, and sits in a layer a project can override and a reviewer can
    see. Same numbers, entirely different accountability.
    """
    standards = seed_company_standards(
        {
            "door_thickness": _standard("3/4"),
            "field_cut": _standard("1"),
            "sink_front_offset": _standard("4"),
        }
    )
    assert standards.layer is ParameterLayer.GLOBAL
    for name in standards.names():
        value = standards.get(name)
        assert value is not None
        assert value.provenance is Provenance.COMPANY_STANDARD
        assert value.set_by


def test_a_seeded_standard_is_still_overridable_by_a_project() -> None:
    """Which is what makes it a standard rather than a hard-coded constant."""
    resolved = resolve(
        "field_cut",
        seed_company_standards({"field_cut": _standard("1")}),
        _project(
            field_cut=ParameterValue(
                value=Quantity(value="2", unit=Unit.INCH),
                provenance=Provenance.GC_CLIENT,
                set_by="Raj",
                set_at=WHEN,
            )
        ),
    )
    assert resolved.value.value.exact_value == Fraction(2)
    assert resolved.overrides_a_company_standard


def test_seeding_is_not_a_way_to_avoid_the_missing_case() -> None:
    """A parameter absent from the standards is still missing. Seeding records what GV decided;
    it does not paper over what nobody decided."""
    assert resolve_required("filler_width_max", seed_company_standards({})) is Outcome.NOT_FOUND


def test_the_global_layer_refuses_a_value_that_is_not_a_standard() -> None:
    """A measured or client-specific value arriving as a company standard would hide that
    nobody set it as policy."""
    measured = ParameterValue(
        value=Quantity(value="6012", unit=Unit.MM),
        provenance=Provenance.MEASURED,
        set_by="site",
        set_at=WHEN,
    )
    with pytest.raises(ValueError, match="company standards"):
        seed_company_standards({"field_dimension": measured})
