"""What a published rulebook needs before it can decide anything.

**Why this is derived rather than listed.** The acceptance test for the reviewer form is that no check
abstains because of a field the form never offered. A hand-written list of fields cannot give that
property: it is correct on the day it is written and silently wrong the first time a rule gains an
input. So the form is built from this, and this is built from the rules themselves.

**Physical quantities, not rule inputs.** Three rules read the sink's front offset and two read the
countertop depth; `CT-SINK-CUTOUT-DEPTH-001` calls it `cutout_depth` while `CT-BACK-OFFSET-MIN-001`
calls the same measurement `sink_depth`. A reviewer measures each of those once and must be asked
once. What identifies "the same measurement" is the pair a rule declares — its semantic type and the
drawing it is read from — so that pair is the key, and the rule inputs it feeds hang off it.

The `source` matters as much as the type: a cabinet width on the architectural drawing and a cabinet
width on the shop drawing are the same *kind* of measurement and emphatically not the same *number* —
`CAB-ARCH-VS-SHOP-001` exists to compare them.

**Nothing here decides a value.** It reports what is wanted, including which parameters the rulebook
declares a default for. A default is a rule author's stand-in until somebody records a real one, not a
client-confirmed number — `CLIENT_FACTS` Q21 has the filler maximum at 2" in one place and 3-4" in
another — so it is returned as a *suggestion* alongside the field rather than filled in as fact.

Verification: `tests/rules/test_required_inputs.py`
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from rules.schema import Cardinality, Rule

__all__ = [
    "DiscriminatorNeed",
    "ParameterNeed",
    "QuantityNeed",
    "RequiredInputs",
    "required_inputs",
]


@dataclass(frozen=True, slots=True)
class Consumer:
    """One rule input a measured quantity feeds."""

    rule_id: str
    input_name: str


@dataclass(frozen=True, slots=True)
class QuantityNeed:
    """One physical measurement a reviewer must read off a drawing.

    `key` is what a caller sends back, and it is `SOURCE:semantic_type` rather than a rule input name
    precisely because one measurement can feed several differently-named inputs.
    """

    key: str
    semantic_type: str
    source: str
    many: bool
    consumers: tuple[Consumer, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ParameterNeed:
    """One setting a reviewer supplies or confirms.

    `declared_default` is the rulebook's own stand-in where it has one, rendered as the authored text
    so nothing here converts it. `blocked` marks a parameter no default exists for and no reviewer can
    supply — today only `back_offset_minimum`, which is a vendor value nobody has given.
    """

    name: str
    scope: str
    rule_ids: tuple[str, ...]
    declared_default: str | None
    blocked: bool


@dataclass(frozen=True, slots=True)
class DiscriminatorNeed:
    """A judgement about the drawing that decides which variant of a rule applies.

    Neither an input nor a parameter, and it was the gap that made this class necessary: a rule with a
    discriminator nobody stated abstains with REVIEW_REQUIRED whatever measurements are supplied, so a
    form offering every input and every parameter still could not drive `CT-WIDTH-001` to a verdict.

    `choices` is closed. The resolver matches the stated value against the declared variants and finds
    nothing if it is misspelled — `NO_APPLICABLE_RULE`, which reads as "this rule does not apply here"
    rather than "you typed the layout wrongly". So a caller must choose from these, and the API
    refuses anything else.
    """

    name: str
    rule_ids: tuple[str, ...]
    choices: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RequiredInputs:
    """Everything the published rulebook wants, grouped so a form can render it."""

    quantities: tuple[QuantityNeed, ...]
    parameters: tuple[ParameterNeed, ...]
    discriminators: tuple[DiscriminatorNeed, ...]


#: Parameters that no reviewer may supply, and why.
#:
#: `back_offset_minimum` is `scope: global` and has no declared default: `CT-BACK-OFFSET-MIN-001` says
#: in its own text that "the minimum has no default because the vendor value is still pending". A form
#: that offered it would invite somebody to invent a safety threshold, so it is reported as blocked and
#: the check goes on abstaining, which is the correct outcome rather than a gap.
BLOCKED_PARAMETERS = frozenset({"back_offset_minimum"})


def _default_text(spec: object) -> str | None:
    """The authored default as text, or `None`.

    Text rather than a number: this is passed to a form and shown to a person, and converting it here
    would put a second numeric interpretation beside the one `units/` already owns.
    """
    default = getattr(spec, "default", None)
    if default is None:
        return None
    value = getattr(default, "value", default)
    unit = getattr(default, "unit", None)
    unit_text = getattr(unit, "value", unit)
    return f"{value} {unit_text}".strip() if unit_text else str(value)


def required_inputs(rules: Iterable[Rule]) -> RequiredInputs:
    """Group every declared input and parameter across a rulebook.

    Ordered deterministically — by key and by name — because this drives a form, and a form whose
    fields reorder between loads is one a reviewer loses their place in.
    """
    quantities: dict[str, tuple[str, str, bool, list[Consumer]]] = {}
    parameters: dict[str, tuple[str, list[str], str | None]] = {}
    discriminators: dict[str, tuple[list[str], list[str]]] = {}

    for rule in rules:
        for input_name, selector in (rule.inputs or {}).items():
            source = getattr(selector.source, "value", str(selector.source))
            semantic = getattr(selector.semantic_type, "value", str(selector.semantic_type))
            many = selector.cardinality is Cardinality.MANY
            key = f"{source}:{semantic}"
            if key not in quantities:
                quantities[key] = (semantic, source, many, [])
            existing_semantic, existing_source, existing_many, consumers = quantities[key]
            # **Two rules reading one quantity with different cardinality is a contradiction, and it
            # is refused rather than merged.** The tempting fix is "a list if any rule reads it as
            # one", and it is wrong: the engine checks arity, so a tuple handed to a scalar input
            # raises `RuleAuthoringError`. That form field would satisfy one rule and break the
            # other, and the breakage would surface as an authoring error at verdict time — far from
            # the rulebook that caused it.
            #
            # No quantity in the rulebook does this today. Raising keeps it that way loudly, where an
            # `or` would let the first such rule through and produce an unfillable form. Found by
            # mutation: the `or` passed every other test in this file.
            if existing_many != many:
                raise ValueError(
                    f"{key} is read with cardinality many={many} by {rule.id}.{input_name} and "
                    f"many={existing_many} by {consumers[0].rule_id}.{consumers[0].input_name}. "
                    "One physical measurement cannot be both a single value and a list: the engine "
                    "checks arity, so no entry could satisfy both rules."
                )
            quantities[key] = (
                existing_semantic,
                existing_source,
                existing_many,
                [*consumers, Consumer(rule_id=rule.id, input_name=input_name)],
            )

        for parameter_name, spec in (rule.parameters or {}).items():
            scope = getattr(spec.scope, "value", str(spec.scope))
            if parameter_name not in parameters:
                parameters[parameter_name] = (scope, [], _default_text(spec))
            existing_scope, rule_ids, declared = parameters[parameter_name]
            parameters[parameter_name] = (
                existing_scope,
                [*rule_ids, rule.id],
                # The first declared default wins, and two rules declaring different defaults for one
                # name would be a rulebook contradiction rather than something to average here.
                declared if declared is not None else _default_text(spec),
            )

        # A rule with no layout discriminator declares `GlobalApplicability` explicitly rather than
        # omitting the field (ADR-0007), so absence here is a real answer and not a missing key.
        applicability = rule.applicability
        name = getattr(applicability, "discriminator", None)
        if name:
            choices = [
                str(variant.when) for variant in (getattr(applicability, "variants", ()) or ())
            ]
            if name not in discriminators:
                discriminators[name] = ([], list(choices))
            known_rules, known_choices = discriminators[name]
            # Written as a membership test and an index rather than a defaulted `get`, matching the
            # two loops above — and `.semgrep/gv-rules.yaml` forbids a fallback lookup anywhere in
            # `rules/` on the grounds that a value must never be invented. This one accumulates rather
            # than resolving a parameter, but the rule is deliberately about the shape, and a broad
            # guard that everybody suppresses stops guarding anything.
            #
            # Two rules sharing a discriminator name must agree on its vocabulary. They do not today,
            # and if they ever disagreed the form would offer a choice one of them rejects — so the
            # union is taken and the disagreement would surface as a variant that resolves for one
            # rule and not the other, which the finding then states.
            discriminators[name] = (
                [*known_rules, rule.id],
                [*known_choices, *(c for c in choices if c not in known_choices)],
            )

    return RequiredInputs(
        quantities=tuple(
            QuantityNeed(
                key=key,
                semantic_type=semantic,
                source=source,
                many=many,
                consumers=tuple(sorted(consumers, key=lambda c: (c.rule_id, c.input_name))),
            )
            for key, (semantic, source, many, consumers) in sorted(quantities.items())
        ),
        parameters=tuple(
            ParameterNeed(
                name=name,
                scope=scope,
                rule_ids=tuple(sorted(set(rule_ids))),
                declared_default=declared,
                blocked=name in BLOCKED_PARAMETERS,
            )
            for name, (scope, rule_ids, declared) in sorted(parameters.items())
        ),
        discriminators=tuple(
            DiscriminatorNeed(
                name=name,
                rule_ids=tuple(sorted(set(rule_ids))),
                choices=tuple(choices),
            )
            for name, (rule_ids, choices) in sorted(discriminators.items())
        ),
    )


def consumers_of(needs: Sequence[QuantityNeed], key: str) -> tuple[Consumer, ...]:
    """The rule inputs one quantity feeds, for a caller fanning a single typed value out."""
    for need in needs:
        if need.key == key:
            return need.consumers
    return ()
