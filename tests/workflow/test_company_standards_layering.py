"""A company standard sits on top of the rulebook's defaults; it never removes them (#812).

Verification for: `workflow/stages.py` (`_layered`, `_cited_sets`).

The one that matters most is `test_one_company_standard_leaves_every_rulebook_default_in_force`: the
first #674 trap. Before #812 any stored company set replaced the rulebook defaults wholesale, so
recording GV's usual cabinet depth silently removed the 2.5" back-offset minimum, the 4" front
offset, the 1/4" clearance and the 1"/2" filler bounds — and each of those checks went NOT_FOUND.
"""

from __future__ import annotations

from datetime import UTC, datetime
from fractions import Fraction

import yaml

from app.models.parameters import declared_defaults
from rules.parameters import (
    ParameterLayer,
    ParameterSet,
    ParameterValue,
    Provenance,
    is_rulebook_default,
    resolve_all,
)
from rules.schema import Quantity, Rule
from tests.workflow.test_stages import RULEBOOK
from units.measurement import Unit
from workflow.stages import _cited_sets, _layered

WHEN = datetime(2026, 10, 2, 9, 30, tzinfo=UTC)


def _defaults() -> ParameterSet:
    rules = [
        Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(RULEBOOK.glob("*.yaml"))
    ]
    return declared_defaults(rules, when=WHEN)


def _company(**values: int | Fraction) -> ParameterSet:
    return ParameterSet(
        project_id=None,
        layer=ParameterLayer.GLOBAL,
        version=3,
        parameters={
            name: ParameterValue(
                value=Quantity(value=value, unit=Unit.INCH),
                provenance=Provenance.COMPANY_STANDARD,
                set_by="an admin",
                set_at=WHEN,
            )
            for name, value in values.items()
        },
    )


def test_one_company_standard_leaves_every_rulebook_default_in_force() -> None:
    """**The point.** Outcome: the company's cabinet depth is in force, and so is every rulebook
    default it did not name — each still marked as the rulebook's."""
    defaults = _defaults()

    resolved = resolve_all(*_layered(defaults, [_company(cabinet_depth=24)]))

    assert resolved["cabinet_depth"].value.value.exact_value == 24
    assert not is_rulebook_default(resolved["cabinet_depth"].value)
    for name in defaults.parameters:
        assert name in resolved, name
        assert is_rulebook_default(resolved[name].value), name


def test_a_company_standard_replaces_the_rulebook_default_of_the_same_name() -> None:
    """Outcome: GV's own back-offset minimum wins over the rule author's 2.5"."""
    resolved = resolve_all(*_layered(_defaults(), [_company(back_offset_minimum=Fraction(19, 8))]))

    assert resolved["back_offset_minimum"].value.value.exact_value == Fraction(19, 8)
    assert not is_rulebook_default(resolved["back_offset_minimum"].value)


def test_a_finding_cites_the_stored_company_set_not_the_merge() -> None:
    """The merged set is resolution's input; no row holds its hash. Outcome: the GLOBAL entry a
    finding records is the stored company set's own hash, and the defaults' set only where none
    was stored."""
    defaults, company = _defaults(), _company(cabinet_depth=24)

    assert _cited_sets(defaults, [company]) == {"global": company.set_id}
    assert _cited_sets(defaults, []) == {"global": defaults.set_id}


def test_with_no_company_set_the_defaults_are_the_whole_global_layer() -> None:
    defaults = _defaults()

    assert _layered(defaults, []) == (defaults,)
