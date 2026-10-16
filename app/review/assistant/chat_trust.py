"""Why each finding can be believed (chat v3, CONTRACT3-trust): checks, trust level, trust report.

Every finding carries `checks`, run by code (and one cheap independent AI read the engine makes):

- `printed_architect`, `printed_vendor`: is the stated value printed inside its box on the PDF's
  text layer (`match` / `not_found` / `no_text` / `no_box` / `not_a_number`)?
- `arithmetic`: for `source: vendor_arithmetic`, do the `parts` add up to `printed_total`?
- `rule`: for `source: rules` naming a GV check code ran (`rule_check_id`), does the recorded result
  agree (`agrees` / `disagrees` / `not_run`)? Code's result wins.
- `second_read`: a fresh model shown only the two crops (`agrees` / `disagrees` / `unsure` /
  `not_run`); run by the engine after the lead finishes.
- `lead_checked`: the lead read both values on the drawing itself (`verified`).

`inches` are code's numbers (`to_inches`), with the difference (vendor minus architect); the screen
never shows the AI's arithmetic. `trust_level`: `confirmed` / `likely` / `to_check`.

The checks themselves live in `finding_checks.py` and `second_read.py` (another builder's modules,
signatures agreed in CONTRACT3-trust.md "## Backend changes"). They read PDFs, so they are loaded by
name (`tests/api/test_no_heavy_work.py`); until they exist the checks are recorded as `not_run`.

**Rounds.** A set reviewed in rounds (first and second set of comments): the agent gives each
finding its `round`; code links the same item (its words, case and spacing aside) across rounds:
an earlier round's item is `fixed_in_later_round` (the later round finds it the same),
`still_wrong` (still different), or `not_in_later_round`; a later round's item with no earlier one
is `new_in_later_round`; a set with one round: `single_round`.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from types import ModuleType
from typing import Any, Final

from app.review.assistant.chat_findings import page_ranges

__all__ = [
    "ACCURACY_MEASURED_LINE",
    "CHECKS_MODULE",
    "SECOND_READ_MODULE",
    "Recorded",
    "accuracy",
    "apply_checks",
    "cloud_kind",
    "findings_csv",
    "load_module",
    "needs_text",
    "recorded_from",
    "recorded_missing",
    "round_statuses",
    "states_drawing_values",
    "trust_level",
    "trust_report",
]

_log = logging.getLogger(__name__)

CHECKS_MODULE: Final = "app.review.assistant.finding_checks"
SECOND_READ_MODULE: Final = "app.review.assistant.second_read"
REVISION_DIFF_MODULE: Final = "app.review.assistant.revision_diff"
#: The outcome word GV Review's records use for a code FAIL (`workflow.findings_composer`).
_FAIL_WORDS: Final = frozenset({"Needs correction"})
_PASS_WORDS: Final = frozenset({"Looks right"})
LEVELS: Final = ("confirmed", "likely", "to_check")


def load_module(name: str) -> ModuleType | None:
    """A checks module, or `None` while it is not on this server yet."""
    try:
        return import_module(name)
    except ModuleNotFoundError as missing:
        if missing.name != name:
            _log.warning("assistant chat checks module could not load: %s", missing.name)
        return None


# ---- what GV Review's own checks recorded --------------------------------------------------------


class Recorded:
    """The differences GV Review's pipeline recorded for the set (code FAILs and the AI page
    review's `different` rows), and each code check's result by its id."""

    def __init__(
        self,
        differences: Sequence[Mapping[str, Any]] = (),
        results: Mapping[str, str] | None = None,
        readable: bool = True,
    ) -> None:
        self.differences = [dict(item) for item in differences]
        self.results = dict(results or {})
        self.readable = readable


def _evaluated(row: Mapping[str, Any]) -> bool:
    """An AI page review row the evaluator checked (or that our cloud PDF will cloud)."""
    keys = ("verified_by", "evaluator_reason", "will_be_clouded_on_our_cloud_pdf")
    if not any(key in row for key in keys):
        return True
    return any(bool(row.get(key)) for key in keys)


def recorded_from(document: Mapping[str, Any] | None) -> Recorded:
    """`review_results`' whole-set document, read for what it recorded. Each recorded difference
    has a `record_id`: the code record's id, or `ai-review:p<page>:<key>` for an AI page review
    row (those have no id of their own)."""
    if not isinstance(document, Mapping):
        return Recorded(readable=False)
    differences: list[dict[str, Any]] = []
    results: dict[str, str] = {}
    records = document.get("records")
    if isinstance(records, Mapping):
        for item in records.get("countertops") or []:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            results[str(item["id"])] = str(item.get("outcome") or "")
            if item.get("outcome") in _FAIL_WORDS:
                differences.append(
                    {
                        "record_id": str(item["id"]),
                        "source": "code",
                        "id": str(item["id"]),
                        "page": item.get("page"),
                        "what": str(item.get("label") or "countertop"),
                        "category": "countertop",
                        "architect": item.get("needed_overall"),
                        "vendor": item.get("printed_overall"),
                        "why": item.get("difference") and f"Difference {item['difference']}",
                    }
                )
        for item in records.get("other_checks") or []:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            results[str(item["id"])] = str(item.get("outcome") or "")
            if item.get("outcome") in _FAIL_WORDS:
                pages = [p for p in item.get("pages") or [] if isinstance(p, int)]
                values = [str(v) for v in item.get("values") or []]
                differences.append(
                    {
                        "record_id": str(item["id"]),
                        "source": "code",
                        "id": str(item["id"]),
                        "page": pages[0] if pages else None,
                        "what": str(item.get("check") or "GV check"),
                        "category": "other",
                        "architect": item.get("comparison"),
                        "vendor": "; ".join(values) or None,
                        "why": item.get("reason"),
                    }
                )
    for sheet in document.get("ai_page_review") or []:
        if not isinstance(sheet, Mapping):
            continue
        for row in sheet.get("different") or []:
            if isinstance(row, Mapping) and _evaluated(row):
                key = str(row.get("key") or row.get("measures") or "item")
                differences.append(
                    {
                        "record_id": f"ai-review:p{sheet.get('page')}:{key}",
                        "source": "ai_page_review",
                        "id": None,
                        "page": sheet.get("page"),
                        "what": key,
                        "category": "other",
                        "architect": row.get("architect_text"),
                        "vendor": row.get("vendor_text"),
                        "why": row.get("why"),
                    }
                )
    return Recorded(differences, results)


def rule_result(finding: Mapping[str, Any], recorded: Recorded | None) -> str | None:
    """The recorded GV check a `source: rules` finding names: does it agree?"""
    if finding.get("source") != "rules":
        return None
    check_id = finding.get("rule_check_id")
    if not check_id or recorded is None:
        return "not_run"
    outcome = recorded.results.get(str(check_id))
    if outcome in _FAIL_WORDS:
        return "agrees" if finding.get("status") in ("different", "check") else "disagrees"
    if outcome in _PASS_WORDS:
        return "agrees" if finding.get("status") == "same" else "disagrees"
    return "not_run"


def recorded_missing(
    findings: Sequence[Mapping[str, Any]], recorded: Recorded, dismissed: Mapping[str, str]
) -> list[str]:
    """The recorded differences an answer neither reports nor dismissed (with a reason): a finding
    carries the record's id (`record_id`, or `rule_check_id` for a code check), or, for a record
    without an id of its own, a finding on its page names its item."""
    ids = {
        str(f.get(key)) for f in findings for key in ("record_id", "rule_check_id") if f.get(key)
    }
    on_page = {
        (f.get("page"), _key(f)) for f in findings if f.get("status") in ("different", "check")
    }
    missing: list[str] = []
    for item in recorded.differences:
        record_id = item["record_id"]
        if record_id in ids or record_id in dismissed:
            continue
        if item.get("id") is None and (item.get("page"), _key({"item": item["what"]})) in on_page:
            continue
        missing.append(record_id)
    return missing


# ---- checks and trust --------------------------------------------------------------------------


def _arithmetic_agrees(checks: Mapping[str, Any], status: object) -> bool | None:
    """Whether code's sum backs the finding: a sum that does not add up backs a difference."""
    arithmetic = checks.get("arithmetic")
    if arithmetic == "does_not_add_up":
        return status != "same"
    if arithmetic == "adds_up":
        return status == "same"
    return None


def trust_level(checks: Mapping[str, Any], status: object = None) -> str:
    """CONTRACT3's rule (the checks module's own `trust_level` is used when it is present)."""
    module = load_module(CHECKS_MODULE)
    if module is not None and hasattr(module, "trust_level"):
        level = str(module.trust_level(checks, status=status if isinstance(status, str) else None))
        if level in LEVELS:
            return level
    arithmetic = _arithmetic_agrees(checks, status)
    if (
        checks.get("second_read") == "disagrees"
        or checks.get("rule") == "disagrees"
        or arithmetic is False
    ):
        return "to_check"
    printed = (
        (checks.get("printed_architect") == "match" and checks.get("printed_vendor") == "match")
        or checks.get("rule") == "agrees"
        or arithmetic is True
    )
    second = checks.get("second_read") == "agrees"
    if printed and second:
        return "confirmed"
    if sum((printed, second, bool(checks.get("lead_checked")))) >= 2:
        return "likely"
    return "to_check"


def _inches(text: object) -> Decimal | None:
    module = load_module(CHECKS_MODULE)
    if module is None or not isinstance(text, str):
        return None
    try:
        value = module.to_inches(text)
    except Exception:  # noqa: BLE001 - not a number, shown as written
        return None
    return value if isinstance(value, Decimal) else None


def _number(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text if text != "-0" else "0"


def apply_checks(
    finding: Mapping[str, Any],
    pdf_paths: Mapping[str, Path],
    recorded: Recorded | None,
    *,
    second_read: str = "not_run",
) -> dict[str, Any]:
    """The finding with code's checks, inches and trust level (the second read as given)."""
    out = dict(finding)
    checks: dict[str, Any] = {
        "printed_architect": "not_run",
        "printed_vendor": "not_run",
        "second_read": second_read,
        "rule": rule_result(finding, recorded),
        "arithmetic": None,
        "lead_checked": bool(finding.get("verified")),
    }
    module = load_module(CHECKS_MODULE)
    if module is not None:
        try:
            found = module.check_finding(finding, pdf_paths).as_dict()
            for key in ("printed_architect", "printed_vendor", "arithmetic"):
                if key in found:
                    checks[key] = found[key]
        except Exception as failed:  # noqa: BLE001 - a check that fails is "not run", never a crash
            _log.warning("assistant chat finding check failed: %s", type(failed).__name__)
    architect, vendor = _inches(finding.get("architect")), _inches(finding.get("vendor"))
    out["checks"] = checks
    out["inches"] = {
        "architect": _number(architect),
        "vendor": _number(vendor),
        "difference": (
            None if architect is None or vendor is None else _number(vendor - architect)
        ),
    }
    out["trust_level"] = trust_level(checks, finding.get("status"))
    return out


def with_second_read(finding: Mapping[str, Any], result: str) -> dict[str, Any]:
    out = dict(finding)
    checks = {**(finding.get("checks") or {}), "second_read": result}
    out["checks"] = checks
    out["trust_level"] = trust_level(checks, finding.get("status"))
    return out


# ---- rounds ------------------------------------------------------------------------------------


def _key(finding: Mapping[str, Any]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(finding.get("item") or "").lower()).strip()


def round_statuses(findings: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """Each finding's `round_status`, linking the same item across rounds."""
    rounds = {int(f.get("round") or 1) for f in findings}
    if len(rounds) <= 1:
        return {str(f["id"]): "single_round" for f in findings}
    by_key: dict[str, list[Mapping[str, Any]]] = {}
    for finding in findings:
        by_key.setdefault(_key(finding), []).append(finding)
    out: dict[str, str] = {}
    for group in by_key.values():
        seen = {int(f.get("round") or 1) for f in group}
        if len(seen) > 1:
            latest = max(group, key=lambda f: int(f.get("round") or 1))
            status = "fixed_in_later_round" if latest.get("status") == "same" else "still_wrong"
            out.update({str(f["id"]): status for f in group})
        elif seen == {min(rounds)}:
            out.update({str(f["id"]): "not_in_later_round" for f in group})
        else:
            out.update({str(f["id"]): "new_in_later_round" for f in group})
    return out


def _rounds_summary(findings: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    rounds = sorted({int(f.get("round") or 1) for f in findings})
    if len(rounds) <= 1:
        return None
    first = rounds[0]
    earlier = [
        f for f in findings if int(f.get("round") or 1) == first and f.get("status") != "same"
    ]
    counts = {
        status: sum(1 for f in earlier if f.get("round_status") == status)
        for status in ("fixed_in_later_round", "still_wrong", "not_in_later_round")
    }
    new = sum(1 for f in findings if f.get("round_status") == "new_in_later_round")
    line = (
        f"Vendor fixed {counts['fixed_in_later_round']} of {len(earlier)} items from round "
        f"{first}; {counts['still_wrong']} still wrong"
    )
    if counts["not_in_later_round"]:
        line += f"; {counts['not_in_later_round']} not found in a later round"
    if new:
        line += f"; {new} new in a later round"
    return {
        "rounds": rounds,
        "earlier_items": len(earlier),
        "fixed": counts["fixed_in_later_round"],
        "still_wrong": counts["still_wrong"],
        "not_in_later_round": counts["not_in_later_round"],
        "new": new,
        "line": line,
    }


# ---- the trust report ----------------------------------------------------------------------------


def _printed(checks: Mapping[str, Any]) -> str:
    sides = (checks.get("printed_architect"), checks.get("printed_vendor"))
    if all(side == "match" for side in sides):
        return "match"
    for word in ("not_found", "no_text", "not_a_number", "no_box"):
        if word in sides:
            return word
    return "not_run"


def trust_report(
    findings: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    recorded: Recorded | None,
    *,
    second_reads_skipped: int = 0,
    dismissed: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The answer's trust report, built by code from its findings and coverage. `dismissed`:
    recorded differences the lead set aside, with its reason."""
    counts = {level: 0 for level in LEVELS}
    by_category: dict[str, dict[str, int]] = {}
    printed = {k: 0 for k in ("match", "not_found", "no_text", "not_a_number", "no_box", "not_run")}
    second = {k: 0 for k in ("agrees", "disagrees", "unsure", "not_run")}
    arithmetic = {k: 0 for k in ("adds_up", "does_not_add_up", "not_a_number")}
    rules: list[dict[str, Any]] = []
    lead_checked = 0
    pen_pages: set[int] = set()
    for finding in findings:
        level = str(finding.get("trust_level") or "to_check")
        counts[level] = counts.get(level, 0) + 1
        category = by_category.setdefault(
            str(finding.get("category") or "other"), {k: 0 for k in LEVELS}
        )
        category[level] = category.get(level, 0) + 1
        checks = finding.get("checks") or {}
        state = _printed(checks)
        printed[state] += 1
        if "no_text" in (checks.get("printed_architect"), checks.get("printed_vendor")):
            pen_pages.add(int(finding.get("page") or 0))
        second[str(checks.get("second_read") or "not_run")] = (
            second.get(str(checks.get("second_read") or "not_run"), 0) + 1
        )
        if checks.get("arithmetic") in arithmetic:
            arithmetic[checks["arithmetic"]] += 1
        if checks.get("rule") is not None:
            rules.append(
                {
                    "finding_id": finding.get("id"),
                    "rule_check_id": finding.get("rule_check_id"),
                    "result": checks.get("rule"),
                }
            )
        lead_checked += bool(checks.get("lead_checked"))
    gv_checks: dict[str, Any] | None = None
    if recorded is not None and recorded.readable:
        missing = set(recorded_missing(findings, recorded, dismissed or {}))
        left_out = [
            {k: item.get(k) for k in ("record_id", "source", "id", "page", "what")}
            for item in recorded.differences
            if item["record_id"] in missing
        ]
        gv_checks = {
            "recorded": len(recorded.differences),
            "included": len(recorded.differences) - len(left_out) - len(dismissed or {}),
            "dismissed": [
                {"record_id": record_id, "reason": reason}
                for record_id, reason in sorted((dismissed or {}).items())
            ],
            "left_out": left_out,
        }
    not_checked: list[str] = []
    pages_not_opened: list[int] = []
    if coverage:
        pages_not_opened = [
            int(entry["page"])
            for entry in coverage.get("pages") or []
            if entry.get("status") == "not_opened"
        ]
    if pen_pages - {0}:
        not_checked.append(
            "Numbers drawn as strokes (no text layer) on pages "
            f"{page_ranges(sorted(pen_pages - {0}))}: checked by a second read, not by the "
            "page's text."
        )
    if pages_not_opened:
        not_checked.append(f"Pages not opened: {page_ranges(pages_not_opened)}.")
    if second_reads_skipped:
        not_checked.append(
            f"Second read not run on {second_reads_skipped} findings (limit or cap)."
        )
    if recorded is None or not recorded.readable:
        not_checked.append("GV Review's own recorded checks could not be read for this answer.")
    return {
        "counts": {**counts, "total": len(findings)},
        "by_category": by_category,
        "checks": {
            "printed_at_spot": printed,
            "second_read": second,
            "rules": rules,
            "arithmetic": arithmetic,
            "lead_checked": lead_checked,
        },
        "coverage": (
            None
            if not coverage
            else {
                "counts": dict(coverage.get("counts") or {}),
                "pages_not_opened": pages_not_opened,
            }
        ),
        "gv_checks": gv_checks,
        "rounds": _rounds_summary(findings),
        "not_checked": not_checked,
    }


def cloud_kind(finding: Mapping[str, Any]) -> str | None:
    """`wrong` (red: a difference its checks do not put in doubt), `check` (amber: to check), or
    `None` (right: never clouded)."""
    if finding.get("status") == "same":
        return None
    if finding.get("status") == "different" and finding.get("trust_level") != "to_check":
        return "wrong"
    return "check"


def needs_text(finding: Mapping[str, Any]) -> str:
    """What the vendor's value needs to be: code's inches when it read the architect's value."""
    inches = (finding.get("inches") or {}).get("architect")
    return f'{inches}"' if inches else str(finding.get("architect"))


# ---- the accuracy score (CONTRACT5) -------------------------------------------------------------

#: What the eval kit measured on GV's test sets (the orchestrator updates this line; never invented).
ACCURACY_MEASURED_LINE: Final = "Measured on GV's test sets: 23 of 24 claims right."
_LEVEL_POINTS: Final = {"confirmed": 1.0, "likely": 0.75, "to_check": 0.4}
_CITE_POINTS: Final = {"match": 1.0, "no_text": 0.5, "not_found": 0.2}


def _disagrees(finding: Mapping[str, Any]) -> str | None:
    checks = finding.get("checks") or {}
    if checks.get("second_read") == "disagrees":
        return "the second read disagrees"
    if checks.get("rule") == "disagrees":
        return "GV's recorded check disagrees"
    if _arithmetic_agrees(checks, finding.get("status")) is False:
        return "code's arithmetic disagrees"
    return None


def accuracy(
    findings: Sequence[Mapping[str, Any]],
    cites: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """The share of the answer's checkable claims that independent checks back, by code.

    A finding: confirmed 1, likely 0.75, to check 0.4, any check that disagrees 0; a code rule or
    code arithmetic that agrees (the reading ledger's sums included), or GV's recorded result
    quoted as recorded, 1; the ledger's cross-view and completeness findings score by their level. A cited value:
    printed there 1, pen strokes 0.5, not found 0.2; a fact about the file checked by code 1 (0
    when it is wrong). A whole-set answer is multiplied by the share of pages accounted for. No
    claim to check: n/a."""
    breakdown: list[dict[str, Any]] = []
    for finding in findings:
        checks = finding.get("checks") or {}
        claim = f"{finding.get('id')} page {finding.get('page')}: {finding.get('item')}"
        against = _disagrees(finding)
        if against:
            points, how = 0.0, against
        elif finding.get("created_by") == "gv_review" and finding.get("status") != "same":
            points, how = 1.0, "GV Review's own recorded result"
        elif (
            finding.get("created_by") == "ledger"
            and finding.get("source") == "vendor_arithmetic"
            and _arithmetic_agrees(checks, finding.get("status"))
        ):
            # The reading ledger's sum: code arithmetic, so it scores as confirmed.
            points, how = 1.0, "code (the vendor's own arithmetic, from the reading ledger)"
        elif checks.get("rule") == "agrees" or _arithmetic_agrees(checks, finding.get("status")):
            points, how = 1.0, "code (GV's rule or the vendor's own arithmetic)"
        else:
            level = str(finding.get("trust_level") or "to_check")
            points, how = _LEVEL_POINTS.get(level, 0.4), f"trust level: {level.replace('_', ' ')}"
        breakdown.append({"claim": claim, "how": how, "points": points})
    for cite in cites:
        claim = f"{cite.get('id')} {cite.get('value')}"
        check = str(cite.get("printed") or "not_run")
        if cite.get("fact"):
            points = 1.0 if check == "match" else 0.0
            how = "checked on the file by code" if points else "the file says otherwise"
        else:
            points = _CITE_POINTS.get(check, 0.2)
            how = {
                "match": "printed at that spot",
                "no_text": "drawn as strokes: not checkable by the page's text",
                "not_found": "not found at that spot",
            }.get(check, "could not be checked at a spot")
        breakdown.append({"claim": claim, "how": how, "points": points})
    if not breakdown:
        return {
            "score": None,
            "level": "n/a",
            "claims": 0,
            "breakdown": [],
            "coverage_factor": None,
            "measured": ACCURACY_MEASURED_LINE,
        }
    mean = sum(item["points"] for item in breakdown) / len(breakdown)
    factor = None
    if coverage:
        counts = coverage.get("counts") or {}
        total = int(counts.get("total") or 0)
        gaps = sum(
            1
            for entry in coverage.get("pages") or []
            if entry.get("status") == "not_opened" and not entry.get("note")
        )
        if total:
            factor = (total - gaps) / total
            mean *= factor
    score = round(mean * 100)
    level = "green" if score >= 80 else "orange" if score >= 60 else "yellow"
    return {
        "score": score,
        "level": level,
        "claims": len(breakdown),
        "breakdown": breakdown,
        "coverage_factor": None if factor is None else round(factor, 3),
        "measured": ACCURACY_MEASURED_LINE,
    }


#: A drawing value stated in words: inches or feet-and-inches (37", 3'-1", 25 1/2 in).
_STATED_VALUE: Final = re.compile(
    r"\d+\s*'\s*-?\s*\d+(?:\s+\d+/\d+)?\s*\"|\d+(?:\s+\d+/\d+|\.\d+)?\s*(?:\"|\u201d|in\b|inches\b)"
)


def states_drawing_values(text: str) -> bool:
    """Whether an answer's words state a size read off a drawing."""
    return bool(_STATED_VALUE.search(text or ""))


# ---- the CSV export ----------------------------------------------------------------------------

CSV_COLUMNS: Final = (
    "AI #",
    "Finding id",
    "Page",
    "Architect page",
    "Category",
    "Item",
    "Architect (as printed)",
    "Vendor (as printed)",
    "Architect (in)",
    "Vendor (in)",
    "Difference (in)",
    "Status",
    "Trust",
    "Printed at spot: architect",
    "Printed at spot: vendor",
    "Second read",
    "GV rule check",
    "Arithmetic",
    "Checked on drawing by Claude",
    "Source",
    "Round",
    "Round status",
    "Why",
    "Reported by",
)
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """A text cell that a spreadsheet never runs as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def findings_csv(findings: Sequence[Mapping[str, Any]]) -> str:
    """The findings with their trust columns, one row each (numbers as code computed them)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(CSV_COLUMNS)
    for number, finding in enumerate(findings, start=1):
        checks = finding.get("checks") or {}
        inches = finding.get("inches") or {}
        writer.writerow(
            [
                number,
                finding.get("id"),
                finding.get("page"),
                finding.get("vs_page") or "",
                _cell(finding.get("category")),
                _cell(finding.get("item")),
                _cell(finding.get("architect")),
                _cell(finding.get("vendor")),
                inches.get("architect") or "",
                inches.get("vendor") or "",
                inches.get("difference") or "",
                finding.get("status"),
                finding.get("trust_level") or "",
                checks.get("printed_architect") or "",
                checks.get("printed_vendor") or "",
                checks.get("second_read") or "",
                checks.get("rule") or "",
                checks.get("arithmetic") or "",
                "yes" if checks.get("lead_checked") else "no",
                finding.get("source"),
                finding.get("round") or "",
                finding.get("round_status") or "",
                _cell(finding.get("why")),
                finding.get("created_by"),
            ]
        )
    return "﻿" + out.getvalue()
