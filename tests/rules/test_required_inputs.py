"""What the rulebook asks for, derived rather than listed.

The property this file exists to protect is the one the reviewer form depends on: **no check can
abstain because of a field the form never offered.** A hand-written list of fields cannot give that,
so the list is derived — and these tests are what make the derivation trustworthy.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rules.required_inputs import BLOCKED_PARAMETERS, required_inputs
from rules.schema import Rule

RULEBOOK = pathlib.Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _rules() -> list[Rule]:
    return [
        Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in sorted(RULEBOOK.glob("*.yaml"))
    ]


def test_every_declared_input_is_offered_somewhere() -> None:
    """**The acceptance property, asserted against the real rulebook.**

    Walk every rule's inputs and check each one is reachable through some quantity's consumers. A
    rule input missing here is a field the form will never show, and the check it belongs to then
    abstains for a reason the reviewer cannot act on — which looks identical to a genuine missing
    dimension.
    """
    rules = _rules()
    needs = required_inputs(rules)

    offered = {
        (consumer.rule_id, consumer.input_name)
        for quantity in needs.quantities
        for consumer in quantity.consumers
    }
    declared = {(rule.id, input_name) for rule in rules for input_name in (rule.inputs or {})}

    assert declared - offered == set(), (
        "these rule inputs are declared but reachable through no quantity, so the form cannot ask "
        f"for them: {sorted(declared - offered)}"
    )


def test_every_declared_parameter_is_offered_somewhere() -> None:
    """The same property for parameters, which abstain just as silently when unset."""
    rules = _rules()
    needs = required_inputs(rules)

    offered = {parameter.name for parameter in needs.parameters}
    declared = {name for rule in rules for name in (rule.parameters or {})}

    assert declared - offered == set(), f"parameters nothing asks for: {sorted(declared - offered)}"


def test_one_measurement_feeding_several_rules_is_asked_for_once() -> None:
    """**The reviewer measures the sink's front offset once.**

    `CT-SINK-OFFSET-FRONT-001` calls it `front_offset` and `CT-BACK-OFFSET-MIN-001` calls it
    `front_offset` too; the same physical dimension also reaches `CT-BACK-OFFSET-MIN-001.sink_depth`
    under a different name as CT008. Asking per rule input would have somebody type the same number
    three times and invite three different answers.
    """
    needs = required_inputs(_rules())

    front_offset = [q for q in needs.quantities if q.key == "SHOP:CT007"]
    assert len(front_offset) == 1, "the front offset is asked for more than once"
    assert {c.rule_id for c in front_offset[0].consumers} == {
        "CT-SINK-OFFSET-FRONT-001",
        "CT-BACK-OFFSET-MIN-001",
    }


def test_the_same_type_on_two_drawings_stays_two_quantities(session: None = None) -> None:
    """**A cabinet width on the architectural sheet is not a cabinet width on the shop sheet.**

    They are the same kind of measurement and emphatically not the same number —
    `CAB-ARCH-VS-SHOP-001` exists precisely to compare them. Merging them by semantic type alone
    would make that check compare a value with itself and pass every time, which is the worst
    possible failure: a check that cannot fail.
    """
    needs = required_inputs(_rules())

    keys = {q.key for q in needs.quantities}
    assert "ARCH:cabinet_width" in keys
    assert "SHOP:cabinet_width" in keys

    arch = next(q for q in needs.quantities if q.key == "ARCH:cabinet_width")
    shop = next(q for q in needs.quantities if q.key == "SHOP:cabinet_width")
    assert {c.input_name for c in arch.consumers} == {"architectural_cabinets"}
    assert "shop_cabinets" in {c.input_name for c in shop.consumers}


@pytest.mark.parametrize(
    "key", ["SHOP:cabinet_width", "SHOP:filler_width", "ARCH:cabinet_width", "ARCH:filler_width"]
)
def test_the_multi_valued_quantities_are_marked_as_lists(key: str) -> None:
    """A run has several cabinets and several fillers, and the form needs a repeatable field.

    A single-value input here would let a reviewer enter one cabinet width for a run of four, and
    `sum_within_tolerance` would then compare a total against a quarter of the cabinets.
    """
    needs = required_inputs(_rules())

    quantity = next(q for q in needs.quantities if q.key == key)
    assert quantity.many, f"{key} is a list in the rulebook and is not marked as one"


def test_the_vendor_value_nobody_has_is_reported_as_blocked() -> None:
    """`back_offset_minimum` must not appear as a field somebody can fill in.

    `CT-BACK-OFFSET-MIN-001` says in its own text that the minimum has no default because the vendor
    value is pending. A form offering it would invite an invented safety threshold, and the check
    abstaining is the correct outcome rather than a gap to close.
    """
    needs = required_inputs(_rules())

    blocked = next(p for p in needs.parameters if p.name == "back_offset_minimum")
    assert blocked.blocked
    assert blocked.declared_default is None, "a blocked parameter must not carry a value to accept"
    assert BLOCKED_PARAMETERS == {
        "back_offset_minimum"
    }, "the blocked set changed; every member is a value nobody may invent and needs its own reason"


def test_a_declared_default_is_reported_but_not_treated_as_confirmed() -> None:
    """The rulebook's defaults are a rule author's stand-in, not a client answer.

    `CLIENT_FACTS` Q21 has the filler maximum at 2" in one place and 3-4" in another, so a form that
    presented the authored 2" as fact would be stating something the client has not agreed.
    """
    needs = required_inputs(_rules())

    filler_max = next(p for p in needs.parameters if p.name == "filler_max")
    assert filler_max.declared_default is not None
    assert not filler_max.blocked
    # Reported as authored text rather than converted: converting here would put a second numeric
    # interpretation beside the one `units/` owns.
    assert isinstance(filler_max.declared_default, str)


def test_a_parameter_two_rules_share_is_listed_once_naming_both() -> None:
    """`sink_cutout_clearance` is read by both sink-cutout checks and is entered once."""
    needs = required_inputs(_rules())

    clearance = [p for p in needs.parameters if p.name == "sink_cutout_clearance"]
    assert len(clearance) == 1
    assert set(clearance[0].rule_ids) == {
        "CT-SINK-CUTOUT-DEPTH-001",
        "CT-SINK-CUTOUT-WIDTH-001",
    }


def test_the_order_is_stable() -> None:
    """A form whose fields reorder between loads is one a reviewer loses their place in."""
    first = required_inputs(_rules())
    second = required_inputs(list(reversed(_rules())))

    assert [q.key for q in first.quantities] == [q.key for q in second.quantities]
    assert [p.name for p in first.parameters] == [p.name for p in second.parameters]


def test_an_empty_rulebook_asks_for_nothing() -> None:
    """Nothing published means nothing to enter — an empty form, not a crash."""
    needs = required_inputs([])

    assert needs.quantities == ()
    assert needs.parameters == ()


def _rule_reading(rule_id: str, input_name: str, cardinality: str) -> Rule:
    """A minimal rule that reads `SHOP:CT010` with the given cardinality, for the refusal below."""
    source = yaml.safe_load((RULEBOOK / "ct_depth_001.yaml").read_text(encoding="utf-8"))
    source["id"] = rule_id
    source["inputs"] = {
        input_name: {
            "source": "SHOP",
            "semantic_type": "CT010",
            "scope": "same_assembly",
            "cardinality": cardinality,
        }
    }
    source["operation"] = {
        "type": "equals",
        "operands": {"actual": input_name, "expected": input_name},
    }
    source["derivations"] = []
    return Rule.model_validate(source)


def test_one_quantity_read_both_singly_and_as_a_list_is_refused() -> None:
    """**A contradiction the form could not satisfy, refused where it is caused.**

    The tempting handling is "a list if any rule reads it as one". That is wrong: the engine checks
    arity, so a tuple handed to a scalar input raises — the field would satisfy one rule and break
    the other, and the breakage would surface as an authoring error at verdict time, far from the
    rulebook that caused it.

    Nothing in the rulebook does this today, which is exactly why it is asserted: the `or` that would
    paper over it passed every other test in this file, and was found by mutation.
    """
    contradictory = [
        _rule_reading("SYNTH-ONE", "depth", "one"),
        _rule_reading("SYNTH-MANY", "depths", "many"),
    ]

    with pytest.raises(ValueError, match="cannot be both a single value and a list"):
        required_inputs(contradictory)


def test_two_rules_agreeing_on_cardinality_are_merged() -> None:
    """The positive control: a refusal that fired on every pair would be useless.

    `SHOP:filler_width` really is read as a list by two rules, and they must merge into one field.
    """
    agreeing = [
        _rule_reading("SYNTH-A", "depth", "one"),
        _rule_reading("SYNTH-B", "also_depth", "one"),
    ]

    needs = required_inputs(agreeing)

    quantity = next(q for q in needs.quantities if q.key == "SHOP:CT010")
    assert {c.rule_id for c in quantity.consumers} == {"SYNTH-A", "SYNTH-B"}
    assert not quantity.many
