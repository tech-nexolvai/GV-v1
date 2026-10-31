"""Each check's track record: a kind of check may make reds only once it has earned it (2026-10-24).

GV's rule is zero false reds: a red cloud goes to the vendor with nobody checking it. Code proves
every red (`proof.code_proved`), but a proof is only as good as the check behind it, and some checks
have been tried on very few real drawings. So each **kind of check** that can turn an item red keeps
a record, measured on GV's own answer keys (Set 1, 2 and 3, the template and cabinet test sheets, the
four-hotel set, the client-meeting job):

- **keyed cases**: the decisions of that kind the keys can judge: a red the key confirms, a red the
  key says is not a mistake, or a known not-a-mistake trap of that kind (the false-red wall, a
  key's "must not flag" item) that the check kept non-red;
- **false reds**: reds of that kind the key says are not mistakes.

A kind **may make reds** only while its record is clean (no false red) on at least `MIN_CASES`
keyed cases (the orchestrator's default, 2026-10-24: 30; Anant may change it). Until then its
would-be reds are shown as a **Likely problem** ("this kind of check is still being proven"): never
clouded, never on the letter as an error, "please check and confirm" instead. A reviewer's Agree
still clouds any item (the reviewer is the witness).

The record is data (`check_record.json`, counts only: never a client value). The counts come from
the local test bank and false-red gate, which hold GV's real drawings and keys and never leave the
owner's machine; whoever re-measures them updates the file in a PR. Since plan change #10 they also
come from the GV-rule checks run on the real uploads (judged only by keys checked by eye) and from
the reviewer's Agree / Not a problem clicks a person accepted (`click_cases`), under a frozen
split: a case whose job taught a rule never certifies that rule's kind (the leakage guard). A false
red counts wherever it is.

**Planted cases** (`planted_mistakes`, `planted_traps`) are made-up drawings with a planted mistake
or look-alike, each a test the CI runs (`tests/review/assistant/test_planted_kinds.py`). They show
each check works; they **never** count towards `keyed_cases` or the licence.

**The accuracy line** (`record_line`, plan change #10): what the answer's accuracy click-through
says was measured comes from this record, never from a hand-edited constant: each kind that may
make reds with its clean keyed cases and the most its false-red share can be at 95% confidence
with none seen in that many cases (exact: 1 - 0.05^(1/n), the "rule of three"; rounded up to a
whole percent: 51 cases say "at most about 6%", 31 "at most about 10%").

Settings (environment, read on each call so a test can change them):
`GV_ASSISTANT_CHECK_RECORD` `on` (default) / `off` (every kind may make reds, as before);
`GV_ASSISTANT_CHECK_RECORD_MIN_CASES` (default 30).

Pure code: no AI, no network; reads only its own data file.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from app.review.assistant import witness

__all__ = [
    "DEFAULT_MIN_CASES",
    "HELD_WORDS",
    "KINDS",
    "MIN_CASES_ENV",
    "RECORD_FILE",
    "SHORT_NAMES",
    "SWITCH_ENV",
    "Record",
    "at_most_percent",
    "cleared",
    "held_reason",
    "kind_of",
    "min_cases",
    "record_line",
    "record_of",
    "records",
]

#: The record (counts only), kept in the repo beside this module.
RECORD_FILE: Final = Path(__file__).with_name("check_record.json")
SWITCH_ENV: Final = "GV_ASSISTANT_CHECK_RECORD"
MIN_CASES_ENV: Final = "GV_ASSISTANT_CHECK_RECORD_MIN_CASES"
#: The orchestrator's default (2026-10-24), Anant may change it.
DEFAULT_MIN_CASES: Final = 30
#: What a held-back red says, after "Please check and confirm".
HELD_WORDS: Final = "this kind of check is still being proven"

#: Every kind of check that can turn an item red, in plain words. `kind_of` names one per finding.
KINDS: Final[Mapping[str, str]] = {
    "vendor_string": "the vendor's own dimension string does not add up",
    "vendor_span": "the vendor prints one span twice, in two sizes",
    "vendor_pages": "the vendor prints one item in two sizes on two pages",
    "filler": "a filler outside GV's filler range",
    "sink_offset": "a sink offset against GV's standard",
    "sink_centre": "a sink's centre against the sink base's centre",
    "run_fit": "a cabinet run does not fit the wall",
    "splash": "a back or side splash against the architect's",
    "missing_item": "a millwork item the architect shows is missing on the vendor's drawing",
    "finish_code": "a finish or material code differs from the architect's",
    "tag": "a tag or model number differs from the architect's",
    "formula_depth": "GV's formula: countertop depth = cabinet depth + front overhang",
    "formula_length": "GV's formula: countertop length = the cabinet run + field cuts and end "
    "overhangs",
    "formula_splash": "GV's formula: back splash length = the top's run from wall to wall",
    "formula_chain": "GV's formula: top width = the sum of the reference's chain of sizes",
    "gv_rule": "another GV rule check",
    "size_vs_architect": "a size differs from the architect's",
    "schedule": "a vendor's material row differs from GV's project schedule",
}

#: Each kind in a few plain words, for the accuracy line (`record_line`).
SHORT_NAMES: Final[Mapping[str, str]] = {
    "vendor_string": "the vendor's own dimension strings",
    "vendor_span": "one span printed twice by the vendor",
    "vendor_pages": "one item in two sizes on the vendor's pages",
    "filler": "fillers against GV's range",
    "sink_offset": "sink offsets against GV's standard",
    "sink_centre": "sink centre against the sink base",
    "run_fit": "cabinet runs against the wall",
    "splash": "splashes against the architect's",
    "missing_item": "millwork missing on the vendor's drawing",
    "finish_code": "finish and material codes",
    "tag": "tags and model numbers",
    "formula_depth": "countertop depth by GV's formula",
    "formula_length": "countertop length by GV's formula",
    "formula_splash": "back splash length by GV's formula",
    "formula_chain": "top width by the reference's chain",
    "gv_rule": "other GV rules",
    "size_vs_architect": "sizes against the architect's",
    "schedule": "material rows against the project schedule",
}
#: The confidence of the bound the accuracy line states (95%).
_BOUND_RISK: Final = 0.05

#: The finding creators each kind's code check stamps (`created_by`; the modules' `CREATED_BY`).
_CREATED_BY: Final[Mapping[str, str]] = {
    "filler_range": "filler",
    "sink_offset": "sink_offset",
    "sink_centre": "sink_centre",
    "run_fit": "run_fit",
    "splash_views": "splash",
    "architect_items": "finish_code",
    "project_schedule": "schedule",
}
#: `missing_items.KIND`, stamped in a missing item's `text_proof`.
_MISSING_KIND: Final = "missing_item"
#: `same_item_pages.FIELD`.
_SAME_ITEM_FIELD: Final = "same_item_pages"
#: `formulas.FIELD`: the settled formula that compared the item, with its kind.
_FORMULA_FIELD: Final = "formula"
#: GV's countertop depth rule (`sink_offset.TOP`): the depth formula's kind.
_DEPTH_RULE: Final = "CT-DEPTH-001"


@dataclass(frozen=True, slots=True)
class Record:
    """One kind's record: keyed cases judged and false reds among them."""

    kind: str
    keyed_cases: int
    false_reds: int

    def clean_on(self, needed: int) -> bool:
        return self.false_reds == 0 and self.keyed_cases >= needed


def kind_of(finding: Mapping[str, Any]) -> str:
    """The kind of check that would make this finding red (one of `KINDS`)."""
    source = finding.get("source")
    created_by = str(finding.get("created_by") or "")
    formula = finding.get(_FORMULA_FIELD)
    if isinstance(formula, Mapping) and formula.get("kind") in KINDS:
        return str(formula["kind"])  # GV's settled formula that compared it (`formulas`)
    standard = finding.get("gv_standard")
    if (
        created_by == "sink_offset"
        and isinstance(standard, Mapping)
        and standard.get("rule") == _DEPTH_RULE
    ):
        return "formula_depth"  # GV's depth formula on the vendor's plan (`sink_offset`)
    if source == "vendor_arithmetic":
        return "vendor_string"
    if source == "cross_view" and finding.get("reference_side") == "vendor":
        return "vendor_span"
    same_item = finding.get(_SAME_ITEM_FIELD)
    if isinstance(same_item, Mapping) and isinstance(same_item.get("pages"), list):
        return "vendor_pages"
    if created_by in _CREATED_BY:
        return _CREATED_BY[created_by]
    settled = finding.get("settled")
    if isinstance(settled, Mapping) and settled.get("method") == "gv_limits":
        return "filler"  # GV's filler limits settled it (`proof.gv_limits`)
    proof_of_text = finding.get("text_proof")
    if isinstance(proof_of_text, Mapping) and proof_of_text.get("kind") == _MISSING_KIND:
        return "missing_item"
    if finding.get("kind") == "missing_on_vendor":
        return "missing_item"  # the simple full check's absence
    if isinstance(finding.get("tag_read"), Mapping):
        return "tag"
    found = _witnesses(finding)
    if "text" in found:
        return "finish_code"
    if "rule" in found or source == "rules" or finding.get("rule_check_id"):
        # Any GV-rule item (code's own check, or a helper naming one): never "a size differs from
        # the architect's", whose record says nothing about GV's rules.
        return "gv_rule"
    return "size_vs_architect"


def _witnesses(finding: Mapping[str, Any]) -> list[str]:
    stored = finding.get("witnesses")
    if isinstance(stored, list):
        return [str(w) for w in stored]
    checks = finding.get("checks")
    if not isinstance(checks, Mapping):
        return []
    status = finding.get("status") if isinstance(finding.get("status"), str) else None
    return witness.witnesses(checks, status)


@lru_cache(maxsize=4)
def _load(path: str, stamp: float) -> dict[str, Record]:
    del stamp  # only part of the cache key: a changed file is read again
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    out: dict[str, Record] = {}
    for kind, row in (data.get("kinds") or {}).items():
        if kind in KINDS and isinstance(row, Mapping):
            out[kind] = Record(
                kind, int(row.get("keyed_cases") or 0), int(row.get("false_reds") or 0)
            )
    return out


def records(path: Path | None = None) -> dict[str, Record]:
    """Every kind's record as the data file states it (a kind it does not list has none)."""
    path = path or RECORD_FILE
    try:
        return _load(str(path), path.stat().st_mtime)
    except (OSError, ValueError, TypeError):
        return {}


def record_of(kind: str, path: Path | None = None) -> Record:
    return records(path).get(kind) or Record(kind, 0, 0)


def min_cases() -> int:
    """Keyed cases a kind needs (`GV_ASSISTANT_CHECK_RECORD_MIN_CASES`, default 30)."""
    raw = os.environ.get(MIN_CASES_ENV, "").strip()
    try:
        return max(0, int(raw)) if raw else DEFAULT_MIN_CASES
    except ValueError:
        return DEFAULT_MIN_CASES


def _on() -> bool:
    return os.environ.get(SWITCH_ENV, "on").strip().lower() != "off"


def cleared(kind: str) -> bool:
    """Whether this kind of check may make reds now: its record is clean on enough keyed cases (or
    the record is switched off)."""
    if not _on():
        return True
    return record_of(kind).clean_on(min_cases())


def held_reason(finding: Mapping[str, Any]) -> str | None:
    """Why a red this finding's check would make is held back as a Likely problem, in one plain
    line; `None` when its kind may make reds. Says only what the record is, never a client value."""
    kind = kind_of(finding)
    if cleared(kind):
        return None
    record = record_of(kind)
    needed = min_cases()
    if record.false_reds:
        state = f"it made {record.false_reds} false red(s) on GV's answer keys"
    else:
        state = f"it is clean on {record.keyed_cases} of the {needed} keyed cases it needs"
    return f"Code found a difference, but {HELD_WORDS} ({KINDS[kind]}: {state})."


def at_most_percent(keyed_cases: int) -> int | None:
    """The most a kind's false-red share can be, in whole percent rounded up, at 95% confidence
    when it made no false red in `keyed_cases` (exact: 1 - 0.05^(1/n)); `None` with no cases."""
    if keyed_cases <= 0:
        return None
    share = 1.0 - float(_BOUND_RISK ** (1 / keyed_cases))
    return math.ceil(round(share * 100, 6))


def record_line(path: Path | None = None) -> str:
    """What was measured, in plain words, from the record (the answer's accuracy click-through):
    each kind that may make reds with its keyed cases and bound, then the rest in one sentence.
    Counts only; never a client value."""
    if not _on():
        return (
            "Each check's track record is switched off on this server: every kind of check may "
            "make reds."
        )
    needed = min_cases()
    found = records(path)
    clean: list[str] = []
    failed: list[str] = []
    waiting = 0
    for kind in KINDS:
        record = found.get(kind) or Record(kind, 0, 0)
        name = SHORT_NAMES[kind]
        if record.false_reds:
            reds = "false red" if record.false_reds == 1 else "false reds"
            failed.append(f"{name}: {record.false_reds} {reds} in {record.keyed_cases} keyed cases")
        elif record.clean_on(needed):
            bound = at_most_percent(record.keyed_cases)
            about = f" (at most about {bound}%)" if bound is not None else ""
            clean.append(f"{name}: 0 false reds in {record.keyed_cases} keyed cases{about}")
        else:
            waiting += 1
    if clean:
        line = "Measured on GV's answer keys: " + "; ".join(clean) + "."
    else:
        line = f"Measured on GV's answer keys: no kind of check has {needed} clean keyed cases yet."
    if failed:
        line += " Held back after a false red: " + "; ".join(failed) + "."
    if waiting:
        line += (
            f" {'Every other kind' if clean or failed else 'Every kind'} of check has fewer than "
            f"{needed} clean keyed cases, so its reds wait as Likely problems."
        )
    return line
