"""GV rules that decide an item instead of a size on the architect's drawing: code, $0, no AI.

Some sizes on GV's countertop drawings are set by GV's own rulebook, not copied from the
architect's (or the cabinet maker's) drawing (settled with GV):

- **the countertop's depth** = the cabinet depth + the overhang (CT-DEPTH-001);
- **the sink cut-out's width** = the sink's inside width - 2 x the clearance
  (CT-SINK-CUTOUT-WIDTH-001), and its **depth** the same from the sink's inside depth
  (CT-SINK-CUTOUT-DEPTH-001). The cut-out comes from the sink entered for the job, never from the
  architect (`sink_offset`).

A reading often sets such a size against whatever the reference prints near it: a top's depth
against the depth the plan prints from the back wall to the wall's return (made-up: 26" against
24 1/4"), or a cut-out's width against a filler beside it. Code then cannot prove the two measure the same points, and the
item used to fall to "not directly comparable (different reference points)". That is true, but it
is not what would settle the item: GV's rule would, and it cannot run because the job's values
(the cabinet depth, the overhang, the sink's inside size) were never entered. GV's sink plan check
(`sink_offset`) runs these rules wherever the values are set and code proves the dimension on the
vendor's plan.

`mark` names the rule and the job values it is missing on an item the escalation could not settle
(`escalation`, `FIELD`), only when the chat's `rules.md` lists each value and says it is not set;
`classification` then files the item as "a job value or GV setting is missing", with the rule and
the values to enter, instead of "different reference points". It never changes an outcome: the
item stays Not compared, never red and never Right (zero false reds: only a code-proved rule check
is red, `sink_offset`). An item code compared and decided keeps its outcome.

Pure: plain values in, plain values out. Verification:
`tests/review/assistant/test_job_value_rules.py`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = ["FIELD", "RULES", "JobRule", "mark", "missing_values", "rule_of", "words"]

#: The finding field `mark` writes: the rule and the job values it is missing.
FIELD: Final = "job_value_rule"


@dataclass(frozen=True, slots=True)
class JobRule:
    """One GV rule that sets an item's size from the job's values."""

    id: str
    name: str
    formula: str
    settings: tuple[tuple[str, str], ...]
    """Each job value the rule needs: (`rules.md` key, plain words)."""
    item: re.Pattern[str]
    """The item's own words that name what the rule sets."""
    categories: frozenset[str]
    """The item categories the rule's size belongs to (a sink cut-out may be filed under the
    sink cabinet when countertops are not in the check's scope)."""


#: Words that make an item something other than the top's own depth or the cut-out's own size.
_NOT_TOP_DEPTH: Final = re.compile(
    r"\b(?:\w*splash\w*|incl\w*|with|plus|cut\s*-?\s*outs?|sinks?|cabinets?|carcass\w*|drawers?|thick\w*|offsets?|"
    r"aprons?|legs?|knee\w*|toe\w*|panels?|fillers?|returns?|shel(?:f|ves)|bowls?|basins?|"
    r"faucets?|holes?|edges?|nosing|overhangs?)\b",
    re.IGNORECASE,
)
_NOT_CUTOUT_SIZE: Final = re.compile(
    r"\b(?:offsets?|radius|radii|corners?|holes?|faucets?|cent(?:re|er)\s*-?\s*lines?|"
    r"cent(?:re|er)s?|from)\b",
    re.IGNORECASE,
)

#: The rules, each with the item words that name what it sets.
RULES: Final[tuple[JobRule, ...]] = (
    JobRule(
        "CT-DEPTH-001",
        "countertop depth",
        "countertop depth = cabinet depth + overhang",
        (("cabinet_depth", "cabinet depth"), ("countertop_overhang", "countertop overhang")),
        re.compile(r"\b(?:counter\s*-?\s*)?top\s+depth\b", re.IGNORECASE),
        frozenset({"countertop"}),
    ),
    JobRule(
        "CT-SINK-CUTOUT-WIDTH-001",
        "sink cut-out width",
        "sink cut-out width = sink inside width - 2 x clearance",
        (
            ("sink_interior_width", "sink inside width"),
            ("sink_cutout_clearance", "sink cut-out clearance"),
        ),
        re.compile(r"\bsink\s+cut\s*-?\s*out\s+width\b", re.IGNORECASE),
        frozenset({"countertop", "cabinet"}),
    ),
    JobRule(
        "CT-SINK-CUTOUT-DEPTH-001",
        "sink cut-out depth",
        "sink cut-out depth = sink inside depth - 2 x clearance",
        (
            ("sink_interior_depth", "sink inside depth"),
            ("sink_cutout_clearance", "sink cut-out clearance"),
        ),
        re.compile(r"\bsink\s+cut\s*-?\s*out\s+depth\b", re.IGNORECASE),
        frozenset({"countertop", "cabinet"}),
    ),
)


def _flat(value: object) -> str:
    return " ".join(str(value or "").split())


def rule_of(item: object, category: object = "countertop") -> JobRule | None:
    """The rule that sets the item named by `item` (a finding's `item` words) of `category`, else
    `None`. The words after the area ("Bath vanity: top depth (right end)") must name the size
    itself, and no word of the whole name may make it another part's (a splash, the cabinet, an
    offset: "Base cabinet: top depth" is the cabinet's)."""
    text = _flat(item)
    what = text.rsplit(":", 1)[-1] if ":" in text else text
    found = [rule for rule in RULES if rule.item.search(what)]
    if len(found) != 1 or str(category or "") not in found[0].categories:
        return None
    rule = found[0]
    if rule.id == "CT-DEPTH-001":
        return None if _NOT_TOP_DEPTH.search(text) else rule
    return None if _NOT_CUTOUT_SIZE.search(what) else rule


def missing_values(rules_md: str | None, rule: JobRule) -> list[str] | None:
    """The plain words of each value `rule` needs that the chat's `rules.md` says is not set;
    `None` when `rules.md` does not list every one of them (nothing is known, nothing is said)."""
    rows: dict[str, str] = {}
    for line in (rules_md or "").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        for key, _words in rule.settings:
            if f"(`{key}`)" in cells[0]:
                rows[key] = cells[1]
    if any(key not in rows for key, _words in rule.settings):
        return None
    return [words for key, words in rule.settings if rows[key].lower().startswith("not set")]


def mark(finding: Mapping[str, Any], rules_md: str | None) -> dict[str, Any] | None:
    """The `FIELD` record for an item GV's rule sets whose job values are missing, else `None`."""
    rule = rule_of(finding.get("item"), finding.get("category"))
    if rule is None:
        return None
    missing = missing_values(rules_md, rule)
    if not missing:
        return None
    return {"rule": rule.id, "name": rule.name, "formula": rule.formula, "missing": missing}


def words(record: Mapping[str, Any]) -> tuple[str, str]:
    """(why, what would settle it) for an item `mark` named, in plain words."""
    missing = [str(m) for m in record.get("missing") or () if m]
    values = (
        " and ".join(missing)
        if len(missing) <= 2
        else ", ".join(missing[:-1]) + f" and {missing[-1]}"
    )
    verb = "is" if len(missing) == 1 else "are"
    why = (
        f"GV's rule for this size: {record.get('formula')} ({record.get('rule')}); the job's "
        f"{values} {verb} not set, so code could not run it (the architect's size it was set "
        "against was not shown to measure the same points)"
    )
    return why, f"the job's {values} (job values), so GV's rule {record.get('rule')} can run"
