"""GV's rule for a slot, decided by the isolated verdict engine with the published rulebook (Step 4).

GV's rules were written down four times (the rulebook YAML, `ai_maths._rule`, `sink_offset`,
`field_cut`). A slot adds no fifth copy: it hands the values code read at the named marks to
`verdict.engine.execute` with the rule's own snapshot from `rules/rulebook/`, and takes its outcome.
The engine stays isolated: this module imports it; it never imports anything of the assistant.

- **The rule** is read from its YAML file and published in memory (`rules.snapshot.publish`), the
  way the rule tests load it; its snapshot id is the hash of its text.
- **The values** become `VerdictOperand`s in inches. A value code read itself (`slot_maths`
  qualifies it) is `CORROBORATED`; any other is `RAW_CANDIDATE`, and the engine then abstains
  (REVIEW_REQUIRED) before any arithmetic: an unqualified value never enters a GV rule.
- **The settings** (the field cut, the front offset, the clearance, the sink's inside size) come from
  the job's `rules.md` table when the reviewer entered one, else the rule's own published default,
  else nothing, and the engine says NOT_FOUND: a missing job value is a missing fact, never a pass.
- **The layout** of a top (which ends meet a wall) is the engine's `wall_config`; an end the answer
  could not name leaves it unknown, and the engine abstains rather than guess.

Pure apart from reading the rulebook files once. Verification:
tests/review/assistant/test_slot_rules.py (made-up values).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from fractions import Fraction
from functools import cache
from pathlib import Path
from typing import Final, Literal

from rules.parameters import ParameterLayer, ParameterValue, Provenance, ResolvedParameter
from rules.schema import Quantity, Rule
from rules.snapshot import RuleSnapshot, publish
from units.measurement import Measurement, Unit
from verdict.engine import execute
from verdict.operands import EvidenceStatus, VerdictOperand
from verdict.outcomes import Outcome

__all__ = ["RULEBOOK", "RuleResult", "Value", "run_rule", "snapshot", "wall_config"]

#: Where the published rules are authored.
RULEBOOK: Final = Path(__file__).resolve().parents[3] / "rules" / "rulebook"
#: `set_by` of a rule's own default, as `app.models.parameters.declared_defaults` writes it.
_DEFAULT_BY: Final = "rulebook default"

type Result = Literal["same", "differs", "missing", "review"]


@dataclass(frozen=True, slots=True)
class Value:
    """One input's value(s) in exact inches, where they were read and whether code read them."""

    inches: tuple[Fraction, ...]
    evidence: str
    qualified: bool
    many: bool = False


@dataclass(frozen=True, slots=True)
class RuleResult:
    """The engine's answer, in the slot's terms."""

    rule_id: str
    result: Result
    words: str
    """The engine's own reason (`61 in != 62 in`, or why it abstained)."""
    missing: str | None = None
    """`missing`: the reason a value is absent (`missing_setting`, `nothing_to_compare`)."""
    snapshot_id: str = ""
    outcome: str = ""
    notes: tuple[str, ...] = ()


@cache
def _snapshots() -> dict[str, RuleSnapshot]:
    import yaml  # type: ignore[import-untyped]  # loaded here so the module stays light

    out: dict[str, RuleSnapshot] = {}
    for path in sorted(RULEBOOK.glob("*.yaml")):
        rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
        out[rule.id] = publish(rule)
    return out


def snapshot(rule_id: str) -> RuleSnapshot | None:
    """The published rule `rule_id`, or `None` when the rulebook holds none."""
    return _snapshots().get(rule_id)


def _registered() -> None:
    from verdict.operations import register_all
    from verdict.registry import REGISTRY

    if not REGISTRY:
        register_all()


def wall_config(ends: Mapping[str, str]) -> str | None:
    """The engine's `wall_config` from which ends of a top meet a wall; `None` when an end is not
    named (wall or free), so the engine abstains instead of guessing."""
    left, right = ends.get("left"), ends.get("right")
    if left not in ("wall", "free") or right not in ("wall", "free"):
        return None
    return {
        ("wall", "wall"): "back_left_right",
        ("wall", "free"): "back_and_left",
        ("free", "wall"): "back_and_right",
        ("free", "free"): "back_only",
    }[(left, right)]


def _setting(rules_md: str | None, name: str) -> tuple[Fraction, str] | None:
    from app.review.assistant.sink_offset import setting_from

    found = setting_from(rules_md, name, name)
    return (found.value, found.origin) if found is not None else None


def _parameters(rule: Rule, rules_md: str | None, when: datetime) -> dict[str, ResolvedParameter]:
    out: dict[str, ResolvedParameter] = {}
    for name, declared in sorted(rule.parameters.items()):
        entered = _setting(rules_md, name)
        if entered is not None:
            value, origin = entered
            company = origin == "GV standard"
            out[name] = ResolvedParameter(
                name=name,
                value=ParameterValue(
                    value=Quantity(value=value, unit=Unit.INCH),
                    provenance=Provenance.COMPANY_STANDARD if company else Provenance.GC_CLIENT,
                    set_by="rules.md",
                    set_at=when,
                    reference=f"rules.md: {origin}",
                ),
                layer=ParameterLayer.GLOBAL if company else ParameterLayer.PROJECT,
            )
        elif declared.default is not None:
            out[name] = ResolvedParameter(
                name=name,
                value=ParameterValue(
                    value=declared.default,
                    provenance=Provenance.COMPANY_STANDARD,
                    set_by=f"{_DEFAULT_BY} ({rule.id})",
                    set_at=when,
                ),
                layer=ParameterLayer.GLOBAL,
            )
    return out


def _operand(name: str, value: Value) -> VerdictOperand:
    measured = tuple(Measurement(inches, Unit.INCH, None) for inches in value.inches)
    return VerdictOperand(
        name=name,
        value=measured if value.many else measured[0],
        status=EvidenceStatus.CORROBORATED if value.qualified else EvidenceStatus.RAW_CANDIDATE,
        source="SHOP",
        evidence_ref=value.evidence,
    )


def run_rule(
    rule_id: str,
    values: Mapping[str, Value],
    *,
    rules_md: str | None,
    when: datetime,
    ends: Mapping[str, str] | None = None,
) -> RuleResult:
    """GV's rule `rule_id` on the named values (input name -> `Value`), by the engine.

    `same` (PASS), `differs` (FAIL), `missing` (NOT_FOUND or NO_APPLICABLE_RULE: a setting or a
    value is absent) or `review` (the engine abstained: an unqualified value, an unknown layout)."""
    found = snapshot(rule_id)
    if found is None:
        return RuleResult(rule_id, "missing", f"no published rule {rule_id}", "nothing_to_compare")
    _registered()
    rule = found.rule
    operands = {name: _operand(name, value) for name, value in values.items() if value.inches}
    discriminators: dict[str, str] = {}
    config = wall_config(ends or {})
    if config is not None:
        discriminators["wall_config"] = config
    decided = execute(
        found,
        operands,
        _parameters(rule, rules_md, when),
        discriminators=discriminators,
    )
    outcome = decided.outcome
    words = " ".join(decided.reason.split())
    base = {
        "snapshot_id": found.snapshot_id,
        "outcome": outcome.value,
        "notes": tuple(decided.notes),
    }
    if outcome is Outcome.PASS:
        return RuleResult(rule_id, "same", words, **base)  # type: ignore[arg-type]
    if outcome is Outcome.FAIL:
        return RuleResult(rule_id, "differs", words, **base)  # type: ignore[arg-type]
    if outcome in (Outcome.NOT_FOUND, Outcome.NO_APPLICABLE_RULE):
        reason = "missing_setting" if _unset(rule, rules_md) else "nothing_to_compare"
        return RuleResult(rule_id, "missing", words, reason, **base)  # type: ignore[arg-type]
    return RuleResult(rule_id, "review", words, **base)  # type: ignore[arg-type]


def _unset(rule: Rule, rules_md: str | None) -> Sequence[str]:
    """The rule's settings with neither a job value nor a published default."""
    return [
        name
        for name, declared in rule.parameters.items()
        if declared.default is None and _setting(rules_md, name) is None
    ]
