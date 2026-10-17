"""Findings with code-made evidence, page coverage, and the marked-up PDF (chat v2, CONTRACT2).

What makes a GV Review answer different from plain Claude's:

- **Every difference is a finding** the agent records with `report_finding` (page, item, what the
  architect shows, what the vendor shows, status, boxes). The app crops the evidence itself, from
  the ORIGINAL upload page at each box (`chat_drawing.crop_png`, 170 dpi, padded 24 points), into
  the chat's pictures folder, and the screen shows the two crops side by side. A box that is not
  given, or not on its page, simply has no picture: never an error to the reviewer.
- **Coverage** (`set_coverage`): page by page, same / different / not comparable / not opened. The
  map always lists every page of the vendor's file; a page never recorded is "not opened".
- **The marked-up PDF** is drawn by code from an answer's findings (`make_markup_file`): no AI call,
  the same file every time for the same answer.

Helpers report findings too: they are marked `created_by: helper:<n>` and `verified: false`, and
the lead re-reports or updates what it checked itself (`update_finding`, `drop_finding`).

**Which file a box is on.** A box may name its `file` (a name in /mnt/user-data/uploads). Without
one, an architect box is on the architect's file when the set has one, else on the vendor's file
(a combined sheet carries both drawings); a vendor box is on the vendor's file.

Nothing here imports a renderer: `chat_drawing` is loaded by name when it is first needed
(`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from app.review.assistant.sandbox import UPLOADS, ChatWorkspace, SandboxRefused
from app.review.assistant.tool_schema import box_schema

__all__ = [
    "CHECK_REASONS",
    "CITE",
    "CODE_SOURCES",
    "COVERAGE_CHECK_PREFIX",
    "DECISION_LIKELY",
    "DISMISS_RECORDED",
    "DROP_FINDING",
    "FINDING_TOOL_NAMES",
    "HELPER_FINDING_TOOL_NAMES",
    "REPORT_FINDING",
    "SET_COVERAGE",
    "UPDATE_FINDING",
    "FindingBook",
    "MarkupFile",
    "boxless_findings",
    "boxless_note",
    "coverage_note",
    "decision_help_lines",
    "decision_help_note",
    "helpless_decisions",
    "live_findings",
    "main_upload",
    "make_markup_file",
    "make_report_file",
    "markup_base",
    "markup_name",
    "page_ranges",
    "place_findings",
    "printed_conflict",
    "products_words",
    "read_listing",
]

_log = logging.getLogger(__name__)

CATEGORIES: Final = ("countertop", "cabinet", "other")
STATUSES: Final = ("different", "check", "same")
SOURCES: Final = ("architect", "brand_prototype", "rules", "vendor_arithmetic", "gv_review")
#: Sources only code makes (the reading ledger): never in the tool's enum.
CODE_SOURCES: Final = ("cross_view", "completeness")
COVERAGE_STATUSES: Final = ("same", "different", "not_comparable", "not_opened")
MAX_FINDINGS: Final = 300
_MAX_ITEM: Final = 160
_MAX_VALUE: Final = 200
_MAX_WHY: Final = 600
_MAX_NOTE: Final = 300
_MAX_COVERAGE_PAGES: Final = 2000
_MAX_HELP: Final = 300
#: How likely a "Your decision" item is to be fine, in the reviewer's words (`decision_help`).
DECISION_LIKELY: Final = ("probably fine", "probably wrong", "can't tell")
#: Why a person must decide an item whose two printed values differ (`check_reason`): without one,
#: two printed values that differ are Wrong, not "Your decision" (`printed_conflict`).
#: `different_extent`: the architect's printed dimension spans more than the unit (a box, soffit,
#: blocking or wall beyond it: `measure.py`'s extent check), so it measures something else.
CHECK_REASONS: Final = (
    "different_extent",
    "which_governs",
    "pairing_not_certain",
    "scaled",
    "site_verify",
)
#: How the engine's "pages missing from coverage" note starts (so a test server can tell it apart
#: from a reviewer's question).
COVERAGE_CHECK_PREFIX: Final = "Coverage check:"

_BOX_FILE: Final = (
    "Only when the set has 2+ PDFs and the box is not on its default file (an architect box: the "
    "architect's file; a vendor box: the vendor's)"
)
_BOX_SCHEMA: Final[Mapping[str, Any]] = box_schema(
    "Where the value is printed, a little larger than it; the app crops the evidence here.",
    page="Page of that file (from 1); required",
    page_required=True,
    file=_BOX_FILE,
)
_DECISION_HELP_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["measures", "estimate", "likely", "confirm"],
    "properties": {
        "measures": {
            "type": "string",
            "description": "What the dimension measures, from where to where (e.g. 'from the "
            "wall to the end of the 6\" blocking, not to the counter's end')",
        },
        "estimate": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["value_in", "plus_minus_in", "method"],
            "properties": {
                "value_in": {"type": "number", "description": "Your best estimate, in inches"},
                "plus_minus_in": {
                    "type": "number",
                    "description": "How far off the estimate may be, in inches (0 when exact)",
                },
                "method": {
                    "type": "string",
                    "description": "How you got it (a string added up, measure_on_drawing's "
                    "answer, an overall less its fillers)",
                },
            },
            "description": "Your best estimate with its method, or null when there is none",
        },
        "likely": {
            "type": "string",
            "enum": list(DECISION_LIKELY),
            "description": "Your honest guess at the answer, in the reviewer's words",
        },
        "confirm": {
            "type": "string",
            "description": "What the reviewer should confirm, and with whom (e.g. 'confirm on "
            "site', 'ask the architect which governs')",
        },
    },
    "description": "Required on every status 'check' (Your decision) finding: the reviewer's "
    "reasoning in short plain words. Not needed on 'different' or 'same'.",
}
_FINDING_FIELDS: Final[Mapping[str, Any]] = {
    "page": {"type": "integer", "minimum": 1, "description": "The vendor's page (from 1)"},
    "vs_page": {
        "type": "integer",
        "minimum": 1,
        "description": "The architect's page, only when it is not the same page",
    },
    "item": {
        "type": "string",
        "description": "What it is, short, starting with its area (e.g. 'Coffee station: top "
        "depth'); the same name on every page, view and round",
    },
    "category": {
        "type": "string",
        "enum": list(CATEGORIES),
        "description": "The product the item belongs to",
    },
    "architect": {
        "type": "string",
        "description": "What the source shows (the architect's drawing, the brand prototype, GV's "
        "rule, or the printed overall for vendor_arithmetic), as printed, e.g. '3\\'-2 1/2\"'",
    },
    "vendor": {
        "type": "string",
        "description": "What the vendor's shop drawing shows, as printed (or 'not shown on the "
        "vendor')",
    },
    "status": {
        "type": "string",
        "enum": list(STATUSES),
        "description": "different = Wrong (the vendor does not match the source); check = Your "
        "decision (a person must decide: give decision_help, and check_reason when both values "
        "are printed and differ); same = Right (they match)",
    },
    "why": {"type": "string", "description": "Why it matters, one short line"},
    "verified": {
        "type": "boolean",
        "description": "True only when you read both values on the drawing yourself (a helper "
        "always sends false; the lead sets true after checking)",
    },
    "arch_box": box_schema(
        "Where the source's value is printed (page required).",
        page="Page of that file (from 1)",
        page_required=True,
        file=_BOX_FILE,
    ),
    "vendor_box": box_schema(
        "Where the vendor's value is printed (page required).",
        page="Page of that file (from 1)",
        page_required=True,
        file=_BOX_FILE,
    ),
    "source": {
        "type": "string",
        "enum": list(SOURCES),
        "description": "What the vendor is compared with: architect (the design drawing); "
        "brand_prototype (a brand's typical sheet, its own group); rules (a GV rule: give "
        "rule_check_id); vendor_arithmetic (a vendor dimension string that does not add up: give "
        "parts and printed_total); gv_review (a difference GV Review already recorded: give "
        "record_id)",
    },
    "parts": {
        "type": "array",
        "items": {"type": "string", "description": "One part, as printed"},
        "maxItems": 40,
        "description": "vendor_arithmetic only: the printed parts of the string, in order",
    },
    "printed_total": {
        "type": "string",
        "description": "vendor_arithmetic only: the printed overall the parts should add up to",
    },
    "rule_check_id": {
        "type": "string",
        "description": "rules only: the id of the GV check in review_results this finding is about",
    },
    "record_id": {
        "type": "string",
        "description": "The id of a difference GV Review already recorded that this finding is "
        "(a code check's id, or ai-review:p<page>:<key>)",
    },
    "round": {
        "type": "integer",
        "minimum": 1,
        "description": "Which round of the set (1 = first submittal) when the set has rounds",
    },
    "decision_help": _DECISION_HELP_SCHEMA,
    "check_reason": {
        "type": "string",
        "enum": list(CHECK_REASONS),
        "description": "Status check only, when both values are printed and differ: why a person "
        "must decide (without one, code makes it different: printed against printed is Wrong). "
        "different_extent = measure_on_drawing showed the architect's dimension spans more than "
        "the unit; which_governs = the ID set and the brand prototype conflict; "
        "pairing_not_certain = not sure the two drawings show the same item; scaled = one value "
        "is measured, not printed; site_verify = the drawing says verify in field",
    },
}
_REQUIRED: Final = frozenset(
    {"page", "item", "category", "architect", "vendor", "status", "verified", "source"}
)

REPORT_FINDING: Final[Mapping[str, Any]] = {
    "name": "report_finding",
    "description": (
        "Record one compared item: the vendor's value against the source's (the architect's "
        "drawing, a brand prototype, a GV rule, or the vendor's own arithmetic), with its status "
        "Wrong (different), Your decision (check) or Right (same). Use it for every item you "
        "compare that the question is about, instead of writing a table: the app crops the "
        "evidence at your boxes, checks the printed values, scores how far to trust it "
        "(Confirmed / Likely / AI reading only) and shows it to the reviewer. Values as printed, "
        'inches. Give each value its box (page included). Returns "Finding F3 recorded." with '
        'any notes from code\'s checks of the values, or "Refused: <why>" (fix and call again). '
        'Example: {"page": 7, "item": "Vanity: top depth", "category": "countertop", '
        '"architect": "22\\"", "vendor": "24\\"", "status": "different", "verified": true, '
        '"source": "architect", "arch_box": {"page": 6, "x0": 410, "y0": 300, "x1": 440, '
        '"y1": 312}, "vendor_box": {"page": 7, "x0": 515, "y0": 288, "x1": 545, "y1": 300}, '
        '"vs_page": 6}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": sorted(_REQUIRED),
        "properties": dict(_FINDING_FIELDS),
    },
}
UPDATE_FINDING: Final[Mapping[str, Any]] = {
    "name": "update_finding",
    "description": (
        "Change fields of a finding already recorded in this answer. Use it after you check a "
        "lead (verified true, or the values as printed), to add a missing box or decision_help, "
        "or when the app sends you back about a finding. Give finding_id and only the fields "
        'that change; the field meanings are report_finding\'s. Returns "Finding F3 updated." '
        'with any notes from code\'s checks, or "Refused: <why>". '
        'Example: {"finding_id": "F3", "verified": true, "vendor": "45 1/2\\""}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["finding_id"],
        "properties": {
            "finding_id": {"type": "string", "description": "The finding's id (F1, F2...)"},
            **{
                name: (
                    field
                    if field.get("type") == "object"
                    else {**field, "description": "As in report_finding"}
                )
                for name, field in _FINDING_FIELDS.items()
            },
        },
    },
}
DROP_FINDING: Final[Mapping[str, Any]] = {
    "name": "drop_finding",
    "description": (
        "Remove a finding of this answer. Use it when you checked the finding on the drawing and "
        "found it wrong (a misread, the wrong pairing, GV's markup taken for a value); use "
        'update_finding instead when only a value or a box needs correcting. Returns "Finding F4 dropped." or '
        '"Refused: <why>". '
        'Example: {"finding_id": "F4", "reason": "The 46\\" is GV\'s red note; the vendor prints '
        '37\\""}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["finding_id", "reason"],
        "properties": {
            "finding_id": {"type": "string", "description": "The finding's id (F1, F2...)"},
            "reason": {"type": "string", "description": "Why, one short line"},
        },
    },
}
SET_COVERAGE: Final[Mapping[str, Any]] = {
    "name": "set_coverage",
    "description": (
        "Record which pages of the vendor's file you compared and how they came out, for the "
        "page grid the reviewer sees. Use it for every page you were asked about; a whole-set "
        "answer or a full check accounts for every page (a page never recorded shows as Not "
        "compared, and the app sends you back once). Status per page: same (all Right), "
        "different (at least one Wrong), not_comparable (nothing to compare, e.g. a divider or a "
        "cover: note why), not_opened (note why). Calls add up. Returns the counts so far and the "
        "pages not accounted for yet. "
        'Example: {"pages": [{"page": 7, "pair_page": 6, "status": "different"}, {"page": 1, '
        '"status": "not_comparable", "note": "Cover sheet"}]}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["pages"],
        "properties": {
            "pages": {
                "type": "array",
                "minItems": 1,
                "description": "One entry per page",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["page", "status"],
                    "properties": {
                        "page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "A page of the vendor's file (from 1)",
                        },
                        "pair_page": {
                            "type": "integer",
                            "minimum": 1,
                            "description": "The architect's page it was compared with, when "
                            "another page",
                        },
                        "status": {
                            "type": "string",
                            "enum": list(COVERAGE_STATUSES),
                            "description": "How the page came out (see the tool description)",
                        },
                        "note": {
                            "type": "string",
                            "description": "Why; required for not_comparable and not_opened",
                        },
                    },
                },
            }
        },
    },
}
DISMISS_RECORDED: Final[Mapping[str, Any]] = {
    "name": "dismiss_recorded",
    "description": (
        "Set aside a difference GV Review already recorded (listed by review_results, or in the "
        "app's list of recorded differences). Use it when you checked it on the drawing and "
        "found it is not a difference. Every recorded difference in scope comes back either as a finding (with "
        "its record_id) or through this tool; the app sends you back once for any left out. "
        'Returns "Recorded difference <id> dismissed" or "Refused: <why>". '
        'Example: {"record_id": "ai-review:p8:filler_left", "reason": "The 3\\" is the field '
        'cut, not a filler"}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["record_id", "reason"],
        "properties": {
            "record_id": {
                "type": "string",
                "description": "The recorded difference's id, as review_results gives it",
            },
            "reason": {"type": "string", "description": "Why, one short line"},
        },
    },
}
CITE: Final[Mapping[str, Any]] = {
    "name": "cite",
    "description": (
        "Back a value you state in your answer with where it is printed, so the app checks it "
        "and shows the evidence. Use it for each value you state that is not in a finding (a "
        "size, a count read off a drawing, a page count). Give page and box for a value on a drawing, or fact for a fact about a "
        'file itself (its page count or name). Returns "Cited C2: match" (found printed there), '
        '"not found" or "not run". '
        'Example: {"value": "36\\"", "page": 12, "box": {"page": 12, "x0": 220, "y0": 410, '
        '"x1": 246, "y1": 422}}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["value"],
        "properties": {
            "value": {"type": "string", "description": "The value as you state it"},
            "page": {
                "type": "integer",
                "minimum": 1,
                "description": "The page it is printed on (from 1), with box",
            },
            "box": _BOX_SCHEMA,
            "fact": {
                "type": "string",
                "enum": ["page_count", "file_name"],
                "description": "Instead of page and box: a fact about the file (its page count, "
                "or its name)",
            },
            "file": {
                "type": "string",
                "description": "The upload's file name, when the set has more than one PDF",
            },
        },
    },
}
FINDING_TOOL_NAMES: Final = frozenset(
    {"report_finding", "update_finding", "drop_finding", "set_coverage", "dismiss_recorded", "cite"}
)
HELPER_FINDING_TOOL_NAMES: Final = frozenset({"report_finding", "set_coverage"})


_PRODUCT_WORDS: Final = {"countertop": "Countertops", "cabinet": "Cabinets"}


def products_words(products: Sequence[str]) -> str:
    """["countertop", "cabinet"] as "Countertops and Cabinets"."""
    return " and ".join(_PRODUCT_WORDS.get(p, p) for p in products) or "All products"


# ---- uploads -------------------------------------------------------------------------------------


def read_listing(workspace: ChatWorkspace) -> list[dict[str, Any]]:
    """The chat's uploads as last copied in (`uploads.json`), or none."""
    manifest = workspace.root / "uploads.json"
    try:
        listing = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [item for item in listing if isinstance(item, dict)] if isinstance(listing, list) else []


def _is_architect(item: Mapping[str, Any]) -> bool:
    return str(item.get("kind") or "").startswith("the architect")


def main_upload(listing: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    """The vendor's file (the shop drawings), else the first file."""
    for item in listing:
        if not _is_architect(item):
            return item
    return listing[0] if listing else None


def _architect_upload(listing: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    return next((item for item in listing if _is_architect(item)), None) or main_upload(listing)


def page_ranges(pages: Sequence[int]) -> str:
    """[3, 4, 5, 9] as "3-5, 9"."""
    out: list[str] = []
    ordered = sorted(set(pages))
    start = previous = None
    for page in [*ordered, None]:
        if page is not None and previous is not None and page == previous + 1:
            previous = page
            continue
        if start is not None:
            out.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = page
    return ", ".join(out)


def _states(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def boxless_findings(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The model's findings that state a drawing value with no box where it is printed: a vendor
    value needs `vendor_box`; an architect value needs `arch_box` when it is read off the
    architect's drawing (`source: architect`; a GV rule, a brand prototype or the vendor's own sum
    is not on the architect's drawing). Code's own findings (GV Review's records, the ledger) are
    left out. Each: `{id, page, item, missing: ["vendor_box", ...]}`."""
    out: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("created_by") in ("gv_review", "ledger"):
            continue
        missing: list[str] = []
        if _states(finding.get("vendor")) and not finding.get("vendor_box"):
            missing.append("vendor_box")
        if (
            finding.get("source", "architect") == "architect"
            and _states(finding.get("architect"))
            and not finding.get("arch_box")
        ):
            missing.append("arch_box")
        if missing:
            out.append(
                {
                    "id": finding.get("id"),
                    "page": finding.get("page"),
                    "item": finding.get("item"),
                    "missing": missing,
                }
            )
    return out


def boxless_note(boxless: Sequence[Mapping[str, Any]]) -> str:
    """The send-back line naming each finding with a value and no box."""
    listed = "; ".join(
        f"{item['id']} (page {item.get('page')}: {item.get('item')}; needs "
        f"{' and '.join(item['missing'])})"
        for item in boxless[:30]
    )
    more = f" and {len(boxless) - 30} more" if len(boxless) > 30 else ""
    return (
        f"{COVERAGE_CHECK_PREFIX} show the evidence: these findings state a drawing value with no "
        f"box where it is printed: {listed}{more}. Give each its box with update_finding (for a GV "
        "rule, the vendor_box of the value the rule read); findings about one rule on one page "
        "may be merged into one finding listing the values (drop the others)."
    )


def coverage_note(gaps: Sequence[int], file_name: str | None) -> str:
    where = f" of {file_name}" if file_name else ""
    return (
        f"{COVERAGE_CHECK_PREFIX} pages {page_ranges(gaps)}{where} are not accounted for in your "
        "coverage map (missing, or not opened with no note). Compare them, or record each with "
        "set_coverage and a note saying why not; then give your answer."
    )


# ---- "Your decision" items -------------------------------------------------------------------------

#: Findings code made itself (GV Review's records, the reading ledger): their words are code's, so
#: the lead is never sent back for their decision help.
_CODE_MADE: Final = ("gv_review", "ledger")


def helpless_decisions(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The model's "Your decision" findings (status check) with no `decision_help`: each
    `{id, page, item}`."""
    return [
        {"id": f.get("id"), "page": f.get("page"), "item": f.get("item")}
        for f in findings
        if f.get("status") == "check"
        and f.get("created_by") not in _CODE_MADE
        and not f.get("decision_help")
    ]


def decision_help_note(items: Sequence[Mapping[str, Any]], *, prefixed: bool = True) -> str:
    """The send-back line naming each "Your decision" finding with no decision help."""
    listed = "; ".join(
        f"{item['id']} (page {item.get('page')}: {item.get('item')})" for item in items[:30]
    )
    more = f" and {len(items) - 30} more" if len(items) > 30 else ""
    lead = f"{COVERAGE_CHECK_PREFIX} " if prefixed else ""
    return (
        f"{lead}help the reviewer decide: these Your-decision findings have no decision_help: "
        f"{listed}{more}. For each, update_finding with decision_help (what it measures; your "
        "best estimate with its method, e.g. a string added up or measure_on_drawing, or null; "
        "likely probably fine / probably wrong / can't tell; what to confirm), in short plain "
        "words."
    )


def _inches_text(value: float) -> str:
    """185.5 as "185 1/2", to the nearest 1/16 inch."""
    sixteenths = round(value * 16)
    whole, rest = divmod(sixteenths, 16)
    if not rest:
        return str(whole)
    fraction = Fraction(rest, 16)
    part = f"{fraction.numerator}/{fraction.denominator}"
    return f"{whole} {part}" if whole else part


def decision_help_lines(finding: Mapping[str, Any]) -> list[str]:
    """A finding's decision help in the reviewer's words, one line each (notes pages, report):
    What it measures / Our best estimate / Likely / What to confirm. None when it has none."""
    help_ = finding.get("decision_help")
    if not isinstance(help_, Mapping):
        return []
    lines: list[str] = []
    if help_.get("measures"):
        lines.append(f"What it measures: {help_['measures']}")
    estimate = help_.get("estimate")
    if isinstance(estimate, Mapping) and isinstance(estimate.get("value_in"), int | float):
        line = f'Our best estimate: about {_inches_text(float(estimate["value_in"]))}"'
        plus_minus = estimate.get("plus_minus_in")
        if isinstance(plus_minus, int | float) and plus_minus > 0:
            line += f' (\u00b1{_inches_text(float(plus_minus))}")'
        if estimate.get("method"):
            line += f": {estimate['method']}"
        lines.append(line)
    elif help_.get("measures") or help_.get("likely"):
        lines.append("Our best estimate: none")
    if help_.get("likely"):
        lines.append(f"Likely: {help_['likely']}")
    if help_.get("confirm"):
        lines.append(f"What to confirm: {help_['confirm']}")
    return lines


#: Words that say a value was measured off the drawing, not printed: such a value never makes a
#: finding Wrong by itself.
_SCALED: Final = re.compile(
    r"scal(?:e|ed|es|ing)\b|\babout\b|approx|\best(?:imate|\.)|~|\u2248|\bnot (?:printed|"
    r"dimensioned|shown)\b|\bnone\b|\bno (?:size|dimension)\b",
    re.IGNORECASE,
)


def _printed_inches(text: object) -> Decimal | None:
    """The one printed value a finding's side states, in inches (code's parser); `None` when it
    is scaled, not one value, or not a value."""
    if not isinstance(text, str) or not text.strip() or _SCALED.search(text):
        return None
    checks = import_module("app.review.assistant.finding_checks")
    exact = checks.to_inches(text)
    if exact is None:
        tokens = checks.dimension_tokens(text)
        exact = checks.to_inches(tokens[0]) if len(tokens) == 1 else None
    return exact if isinstance(exact, Decimal) else None


def printed_conflict(finding: Mapping[str, Any]) -> tuple[Decimal, Decimal] | None:
    """(vendor, architect) inches when a "Your decision" finding is really Wrong: the model's
    finding against the architect, status check with no `check_reason`, both sides one printed
    value (not scaled) that code reads, and the two differ. `None` otherwise."""
    if (
        finding.get("status") != "check"
        or finding.get("check_reason")
        or finding.get("source", "architect") != "architect"
        or finding.get("created_by") in _CODE_MADE
    ):
        return None
    vendor = _printed_inches(finding.get("vendor"))
    architect = _printed_inches(finding.get("architect"))
    if vendor is None or architect is None or vendor == architect:
        return None
    return vendor, architect


def _wrong_when_printed(fields: dict[str, Any]) -> str | None:
    """Printed against printed is Wrong: a finding `printed_conflict` names becomes status
    different, and the lead is told why (and how to keep it a decision)."""
    conflict = printed_conflict(fields)
    if conflict is None:
        return None
    vendor, architect = conflict
    fields["status"] = "different"
    if not fields.get("why"):
        fields["why"] = "Both values are printed on the drawings and they differ."
    return (
        f"code made it status different (Wrong): the vendor prints {fields.get('vendor')} "
        f"({vendor} in) and the architect prints {fields.get('architect')} ({architect} in), both "
        "printed, so they differ. Keep it a decision only with check_reason (which_governs, "
        "different_extent, pairing_not_certain, scaled or site_verify) via update_finding"
    )


def _extent_for(
    finding: Mapping[str, Any], extents: Sequence[Mapping[str, Any]]
) -> Mapping[str, Any] | None:
    """The newest extent check (`measure_on_drawing` with a `dimension`) that showed the finding's
    architect value, on its page (or the architect's page), spans more than the unit."""
    architect = _printed_inches(finding.get("architect"))
    if architect is None:
        return None
    pages = {finding.get("page"), finding.get("vs_page")} - {None}
    box = finding.get("arch_box")
    box_file = box.get("file") if isinstance(box, Mapping) else None
    for extent in reversed(extents):
        if not extent.get("covers_more") or extent.get("page") not in pages:
            continue
        if box_file and extent.get("file") not in (None, box_file):
            continue
        if Decimal(str(extent.get("printed_in"))) == architect:
            found: Mapping[str, Any] = extent
            return found
    return None


def _decision_when_wider(
    fields: dict[str, Any], extents: Sequence[Mapping[str, Any]]
) -> str | None:
    """The extent check: an architect dimension measured as spanning more than the unit (a box or
    soffit above it, blocking, a wall) measures something else, so a vendor value that differs from
    it is Your decision (check_reason different_extent), never Wrong. Code fills the decision help
    from the measurement when the finding has none."""
    if (
        fields.get("status") == "same"
        or fields.get("source", "architect") != "architect"
        or fields.get("created_by") in _CODE_MADE
        or (fields.get("status") == "check" and fields.get("check_reason") == "different_extent")
    ):
        return None
    extent = _extent_for(fields, extents)
    if extent is None:
        return None
    was_wrong = fields.get("status") == "different"
    fields["status"] = "check"
    fields["check_reason"] = "different_extent"
    if not fields.get("decision_help"):
        unit = extent.get("unit_in")
        vendor = _printed_inches(fields.get("vendor"))
        plus_minus = float(extent.get("plus_minus_in") or 0)
        likely = "can't tell"
        if isinstance(unit, int | float) and vendor is not None:
            likely = (
                "probably fine" if abs(float(vendor) - unit) <= plus_minus else "probably wrong"
            )
        fields["decision_help"] = {
            "measures": str(extent.get("measures") or "more than the unit itself")[:_MAX_HELP],
            "estimate": (
                {
                    "value_in": round(float(unit), 4),
                    "plus_minus_in": plus_minus,
                    "method": "the unit itself, scaled by code against the printed dimensions",
                }
                if isinstance(unit, int | float)
                else None
            ),
            "likely": likely,
            "confirm": "what the extra part is (e.g. is the box above by others?) and the unit's "
            "own size",
        }
    if not was_wrong:
        return (
            "code set check_reason different_extent (the architect's dimension spans more "
            "than the unit)"
        )
    return (
        "code made it status check (Your decision), not Wrong: your measure_on_drawing showed the "
        f"architect's {fields.get('architect')} spans more than the unit ({extent.get('words')}). "
        "Its decision_help says what it measures and the unit's scaled size"
    )


def _status_rules(fields: dict[str, Any], extents: Sequence[Mapping[str, Any]]) -> list[str]:
    """Code's status rules on a model finding, in order: the extent check first (a dimension that
    measures more than the unit is Your decision), then printed against printed is Wrong."""
    return [
        note
        for note in (_decision_when_wider(fields, extents), _wrong_when_printed(fields))
        if note
    ]


# ---- validation ----------------------------------------------------------------------------------


def _text(given: Mapping[str, Any], name: str, limit: int, *, empty: bool = False) -> str:
    value = given.get(name)
    if not isinstance(value, str):
        raise SandboxRefused(f"{name} must be text")
    value = " ".join(value.split())
    if not value and not empty:
        raise SandboxRefused(f"{name} is empty")
    if len(value) > limit:
        raise SandboxRefused(f"{name} is longer than {limit} characters")
    return value


def _whole(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SandboxRefused(f"{name} must be a whole number from 1")
    return value


def _choice(value: object, name: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise SandboxRefused(f"{name} must be one of {', '.join(allowed)}")
    return str(value)


def _help_text(given: Mapping[str, Any], name: str) -> str:
    return _text(given, name, _MAX_HELP).rstrip()


def _decision_help(raw: object) -> dict[str, Any] | None:
    """A "Your decision" item's help, checked: what it measures, the best estimate (or null),
    how likely it is fine, what to confirm."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SandboxRefused("decision_help must be an object")
    unknown = set(raw) - {"measures", "estimate", "likely", "confirm"}
    if unknown:
        raise SandboxRefused(f"decision_help: unknown field {min(unknown)!r}")
    for name in ("measures", "likely", "confirm"):
        if name not in raw:
            raise SandboxRefused(f"decision_help needs {name!r}")
    help_: dict[str, Any] = {
        "measures": _help_text(raw, "measures"),
        "estimate": None,
        "likely": _choice(raw.get("likely"), "decision_help.likely", DECISION_LIKELY),
        "confirm": _help_text(raw, "confirm"),
    }
    estimate = raw.get("estimate")
    if estimate is not None:
        if not isinstance(estimate, Mapping):
            raise SandboxRefused("decision_help.estimate must be an object or null")
        extra = set(estimate) - {"value_in", "plus_minus_in", "method"}
        if extra:
            raise SandboxRefused(f"decision_help.estimate: unknown field {min(extra)!r}")
        numbers: list[float] = []
        for name in ("value_in", "plus_minus_in"):
            value = estimate.get(name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SandboxRefused(f"decision_help.estimate.{name} must be a number (inches)")
            if not math.isfinite(value) or value < 0 or value > 100_000:
                raise SandboxRefused(f"decision_help.estimate.{name} is not a length in inches")
            numbers.append(round(float(value), 4))
        help_["estimate"] = {
            "value_in": numbers[0],
            "plus_minus_in": numbers[1],
            "method": _help_text(estimate, "method"),
        }
    return help_


def _stated_count(value: str) -> int | None:
    """The one whole number a stated count holds ("60", "60 pages", "60 pp."), else `None`."""
    numbers = re.findall(r"\d+", value.replace(",", ""))
    return int(numbers[0]) if len(numbers) == 1 else None


@dataclass(slots=True)
class FindingBook:
    """One message's findings and coverage. Thread-safe (helpers report from their threads)."""

    workspace: ChatWorkspace
    listing: Sequence[Mapping[str, Any]] = ()
    page_count: int = 0
    """The pages of the vendor's file (the coverage map's pages); 0 when unknown."""
    crop: Callable[[Path, int, tuple[float, float, float, float]], bytes | None] | None = None
    checker: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    """Adds code's checks, inches and trust level to a finding (`chat_trust.apply_checks`)."""
    findings: dict[str, dict[str, Any]] = field(default_factory=dict)
    dropped: dict[str, str] = field(default_factory=dict)
    coverage: dict[int, dict[str, Any]] = field(default_factory=dict)
    coverage_used: bool = False
    scope_pages: frozenset[int] | None = None
    """A page or area answer's pages (`chat_scope`): the coverage map lists only those and the
    pages recorded; `None` (the whole set): every page of the vendor's file."""
    dismissed: dict[str, str] = field(default_factory=dict)
    """Recorded differences the lead set aside: record id -> its reason."""
    record_ids: frozenset[str] = frozenset()
    extents: Sequence[Mapping[str, Any]] = ()
    """This message's extent checks (`measure_on_drawing` with a `dimension`, `ChatTools.extents`):
    each `{file, page, printed_in, covers_more, unit_in, plus_minus_in, measures, words}`."""
    cites: list[dict[str, Any]] = field(default_factory=list)
    _cites: int = 0
    """The recorded differences this answer must account for (set by the engine)."""
    _count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- files and boxes ---------------------------------------------------------------------------

    @property
    def main_file(self) -> str | None:
        item = main_upload(self.listing)
        return None if item is None else str(item["name"])

    def _box(self, raw: object, name: str, *, architect: bool) -> dict[str, Any] | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise SandboxRefused(f"{name} must be an object")
        unknown = set(raw) - {"page", "x0", "y0", "x1", "y1", "file"}
        if unknown:
            raise SandboxRefused(f"{name}: unknown field {min(unknown)!r}")
        page = _whole(raw.get("page"), f"{name}.page")
        coords: list[float] = []
        for key in ("x0", "y0", "x1", "y1"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise SandboxRefused(f"{name}.{key} must be a number")
            if not math.isfinite(value) or abs(value) > 100_000:
                raise SandboxRefused(f"{name}.{key} is not a page position")
            coords.append(float(value))
        x0, y0, x1, y1 = coords
        file_name = raw.get("file")
        names = [str(item["name"]) for item in self.listing]
        if file_name is not None:
            if not isinstance(file_name, str):
                raise SandboxRefused(f"{name}.file must be text")
            short = file_name.strip().removeprefix(UPLOADS + "/")
            if names and short not in names:
                raise SandboxRefused(f"{name}.file: there is no upload {short!r}")
            chosen: str | None = short
        else:
            item = _architect_upload(self.listing) if architect else main_upload(self.listing)
            chosen = None if item is None else str(item["name"])
        return {
            "page": page,
            "x0": round(min(x0, x1), 2),
            "y0": round(min(y0, y1), 2),
            "x1": round(max(x0, x1), 2),
            "y1": round(max(y0, y1), 2),
            "file": chosen,
        }

    def _picture(self, box: Mapping[str, Any] | None) -> tuple[str | None, str | None]:
        """A crop of the box's area as a chat picture: (picture id, why there is none)."""
        if box is None:
            return None, "no box given"
        if not box.get("file"):
            return None, "the set's files are not known here"
        pdf = self.workspace.uploads / str(box["file"])
        if not pdf.is_file():
            return None, f"{box['file']} is not in the uploads"
        crop = self.crop or _default_crop
        try:
            png = crop(pdf, int(box["page"]), (box["x0"], box["y0"], box["x1"], box["y1"]))
        except Exception as failed:  # noqa: BLE001 - no picture, never an error to the reviewer
            _log.warning("assistant chat evidence crop failed: %s", type(failed).__name__)
            return None, "the page could not be drawn"
        if png is None:
            return None, f"the box is not on page {box['page']} of {box['file']}"
        picture_id = str(uuid4())
        self.workspace.pictures.mkdir(parents=True, exist_ok=True)
        (self.workspace.pictures / f"{picture_id}.png").write_bytes(png)
        return picture_id, None

    # ---- findings ----------------------------------------------------------------------------------

    def _fields(
        self, given: Mapping[str, Any], *, partial: bool, sources: Sequence[str] = SOURCES
    ) -> dict[str, Any]:
        """The finding fields given, checked (`partial`: an update; null clears an optional one)."""
        out: dict[str, Any] = {}
        present = set(given)
        if not partial:
            missing = _REQUIRED - present
            if missing:
                raise SandboxRefused(f"missing argument {min(missing)!r}")
        if "page" in present:
            out["page"] = _whole(given["page"], "page")
            if self.page_count and out["page"] > self.page_count:
                raise SandboxRefused(
                    f"page {out['page']} is not in the vendor's file (pages 1 to {self.page_count})"
                )
        if "vs_page" in present:
            vs = given["vs_page"]
            out["vs_page"] = None if vs is None else _whole(vs, "vs_page")
        if "item" in present:
            out["item"] = _text(given, "item", _MAX_ITEM)
        if "category" in present:
            out["category"] = _choice(given["category"], "category", CATEGORIES)
        for name in ("architect", "vendor"):
            if name in present:
                out[name] = _text(given, name, _MAX_VALUE)
        if "status" in present:
            out["status"] = _choice(given["status"], "status", STATUSES)
        if "why" in present:
            out["why"] = None if given["why"] is None else _text(given, "why", _MAX_WHY, empty=True)
        if "verified" in present:
            if not isinstance(given["verified"], bool):
                raise SandboxRefused("verified must be true or false")
            out["verified"] = given["verified"]
        if "source" in present:
            out["source"] = _choice(given["source"], "source", sources)
        for name, architect in (("arch_box", True), ("vendor_box", False)):
            if name in present:
                out[name] = self._box(given[name], name, architect=architect)
        if "parts" in present:
            parts = given["parts"]
            if parts is None:
                out["parts"] = None
            elif not isinstance(parts, list) or len(parts) > 40:
                raise SandboxRefused("parts must be a list of at most 40 values")
            else:
                out["parts"] = [_text({"part": p}, "part", _MAX_VALUE) for p in parts]
        for name in ("printed_total", "rule_check_id", "record_id"):
            if name in present:
                out[name] = None if given[name] is None else _text(given, name, _MAX_VALUE)
        if "round" in present:
            out["round"] = None if given["round"] is None else _whole(given["round"], "round")
        if "decision_help" in present:
            out["decision_help"] = _decision_help(given["decision_help"])
        if "check_reason" in present:
            reason = given["check_reason"]
            out["check_reason"] = (
                None if reason is None else _choice(reason, "check_reason", CHECK_REASONS)
            )
        return out

    def report(self, raw: object, created_by: str) -> tuple[dict[str, Any], list[str]]:
        """A new finding (id, crops made), and the reasons any box has no picture."""
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - set(_FINDING_FIELDS)
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        fields = self._fields(raw, partial=False)
        if created_by != "lead":
            fields["verified"] = False
        fields["created_by"] = created_by
        changed = _status_rules(fields, list(self.extents))
        with self._lock:
            if len(self.findings) >= MAX_FINDINGS:
                raise SandboxRefused(f"at most {MAX_FINDINGS} findings in one answer")
            self._count += 1
            finding_id = f"F{self._count}"
        arch_picture, arch_why = self._picture(fields.get("arch_box"))
        vendor_picture, vendor_why = self._picture(fields.get("vendor_box"))
        finding = {
            "id": finding_id,
            "page": fields["page"],
            "vs_page": fields.get("vs_page"),
            "item": fields["item"],
            "category": fields["category"],
            "architect": fields["architect"],
            "vendor": fields["vendor"],
            "status": fields["status"],
            "why": fields.get("why"),
            "verified": fields["verified"],
            "arch_box": fields.get("arch_box"),
            "vendor_box": fields.get("vendor_box"),
            "source": fields["source"],
            "parts": fields.get("parts"),
            "printed_total": fields.get("printed_total"),
            "rule_check_id": fields.get("rule_check_id"),
            "record_id": fields.get("record_id"),
            "round": fields.get("round"),
            "arch_picture_id": arch_picture,
            "vendor_picture_id": vendor_picture,
            "created_by": created_by,
            "decision_help": fields.get("decision_help"),
            "check_reason": fields.get("check_reason"),
        }
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            self.findings[finding_id] = finding
        notes = [
            f"{side} box: no picture ({why})"
            for side, why, given in (
                ("architect", arch_why, fields.get("arch_box")),
                ("vendor", vendor_why, fields.get("vendor_box")),
            )
            if given is not None and why is not None
        ]
        notes.extend(changed)
        return dict(finding), notes

    def add_code_finding(self, given: Mapping[str, Any]) -> dict[str, Any] | None:
        """A finding code made (the reading ledger's: `created_by: "ledger"`), added like a lead
        finding (id, crops, checks). Its `ledger_ids` and `reference_side` are kept. A box that is
        not on an upload is left out; a finding that still does not fit is skipped (None)."""
        fields_given = {k: v for k, v in given.items() if k in _FINDING_FIELDS}
        for name, limit in (("item", _MAX_ITEM), ("why", _MAX_WHY)):
            value = fields_given.get(name)
            if isinstance(value, str) and len(" ".join(value.split())) > limit:
                fields_given[name] = " ".join(value.split())[: limit - 1] + "\u2026"
        side = given.get("reference_side")
        if given.get("source") == "cross_view" and side in ("vendor", "architect"):
            # Both of a cross-view finding's boxes are on the reference side's own file.
            item = (
                main_upload(self.listing) if side == "vendor" else _architect_upload(self.listing)
            )
            for name in ("arch_box", "vendor_box"):
                box = fields_given.get(name)
                if isinstance(box, Mapping) and not box.get("file") and item is not None:
                    fields_given[name] = {**box, "file": str(item["name"])}
        for name in ("arch_box", "vendor_box"):
            box = fields_given.get(name)
            if isinstance(box, Mapping) and box.get("file") is None:
                fields_given[name] = {k: v for k, v in box.items() if k != "file"}
        try:
            fields = self._fields(fields_given, partial=False, sources=(*SOURCES, *CODE_SOURCES))
        except SandboxRefused:
            for name in ("arch_box", "vendor_box"):
                fields_given.pop(name, None)
            if self.page_count and int(fields_given.get("page") or 0) > self.page_count:
                # An architect's page: the item has no vendor page.
                fields_given = {**fields_given, "vs_page": fields_given["page"], "page": 1}
            try:
                fields = self._fields(
                    fields_given, partial=False, sources=(*SOURCES, *CODE_SOURCES)
                )
            except SandboxRefused as refused:
                _log.warning("assistant chat ledger finding skipped: %s", refused)
                return None
        with self._lock:
            if len(self.findings) >= MAX_FINDINGS:
                return None
            self._count += 1
            finding_id = f"F{self._count}"
        arch_picture, _why = self._picture(fields.get("arch_box"))
        vendor_picture, _why = self._picture(fields.get("vendor_box"))
        finding: dict[str, Any] = {
            "id": finding_id,
            "page": fields["page"],
            "vs_page": fields.get("vs_page"),
            "item": fields["item"],
            "category": fields["category"],
            "architect": fields["architect"],
            "vendor": fields["vendor"],
            "status": fields["status"],
            "why": fields.get("why"),
            "verified": bool(fields.get("verified")),
            "arch_box": fields.get("arch_box"),
            "vendor_box": fields.get("vendor_box"),
            "source": fields["source"],
            "parts": fields.get("parts"),
            "printed_total": fields.get("printed_total"),
            "rule_check_id": None,
            "record_id": None,
            "round": None,
            "arch_picture_id": arch_picture,
            "vendor_picture_id": vendor_picture,
            "created_by": "ledger",
            "ledger_ids": [str(i) for i in given.get("ledger_ids") or []],
            "reference_side": side if side in ("vendor", "architect") else None,
            "sum_text": str(given["sum_text"]) if given.get("sum_text") else None,
        }
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            self.findings[finding_id] = finding
        return dict(finding)

    def _known(self, raw: Mapping[str, Any]) -> str:
        finding_id = raw.get("finding_id")
        if not isinstance(finding_id, str) or not finding_id.strip():
            raise SandboxRefused("finding_id must be text")
        finding_id = finding_id.strip()
        with self._lock:
            if finding_id in self.dropped:
                raise SandboxRefused(f"finding {finding_id} was dropped")
            if finding_id not in self.findings:
                raise SandboxRefused(
                    f"there is no finding {finding_id} in this answer (report it again)"
                )
        return finding_id

    def update(self, raw: object) -> tuple[dict[str, Any], list[str]]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - set(_FINDING_FIELDS) - {"finding_id"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        finding_id = self._known(raw)
        changes = self._fields({k: v for k, v in raw.items() if k != "finding_id"}, partial=True)
        if not changes:
            raise SandboxRefused("give at least one field to change")
        notes: list[str] = []
        for box, picture, side in (
            ("arch_box", "arch_picture_id", "architect"),
            ("vendor_box", "vendor_picture_id", "vendor"),
        ):
            if box in changes:
                changes[picture], why = self._picture(changes[box])
                if changes[box] is not None and why is not None:
                    notes.append(f"{side} box: no picture ({why})")
        with self._lock:
            finding = {**self.findings[finding_id], **changes}
        notes.extend(_status_rules(finding, list(self.extents)))
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            if finding_id in self.findings:
                self.findings[finding_id] = finding
        return dict(finding), notes

    def drop(self, raw: object) -> tuple[str, str]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"finding_id", "reason"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        finding_id = self._known(raw)
        reason = _text(raw, "reason", _MAX_NOTE)
        with self._lock:
            dropped = self.findings.pop(finding_id, None) or {}
            self.dropped[finding_id] = reason
            if dropped.get("record_id"):
                # Dropping a recorded difference's finding dismisses it, with the same reason.
                self.dismissed[str(dropped["record_id"])] = reason
        return finding_id, reason

    def absorb(self, finding_id: str) -> bool:
        """A finding code folded into another (a helper's reading of a recorded difference, merged
        into the finding the app made for it): removed, never dismissed. False when it is gone."""
        with self._lock:
            return self.findings.pop(finding_id, None) is not None

    def dismiss(self, raw: object) -> tuple[str, str]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"record_id", "reason"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        record_id = _text(raw, "record_id", _MAX_VALUE)
        reason = _text(raw, "reason", _MAX_NOTE)
        if self.record_ids and record_id not in self.record_ids:
            raise SandboxRefused(f"there is no recorded difference {record_id!r}")
        with self._lock:
            self.dismissed[record_id] = reason
        return record_id, reason

    def cite(
        self, raw: object, printed: Callable[[Path, int, Mapping[str, Any], str], str]
    ) -> dict[str, Any]:
        """A value the answer states, backed by where it is printed (checked by `printed`) or by
        a fact about the file (checked here)."""
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"value", "page", "box", "fact", "file"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        value = _text(raw, "value", _MAX_VALUE)
        fact = raw.get("fact")
        names = [str(item["name"]) for item in self.listing]
        file_name = raw.get("file")
        if file_name is not None:
            if not isinstance(file_name, str):
                raise SandboxRefused("file must be text")
            file_name = file_name.strip().removeprefix(UPLOADS + "/")
            if names and file_name not in names:
                raise SandboxRefused(f"there is no upload {file_name!r}")
        chosen = file_name or self.main_file
        with self._lock:
            self._cites += 1
            cite_id = f"C{self._cites}"
        cite: dict[str, Any] = {
            "id": cite_id,
            "value": value,
            "fact": None,
            "page": None,
            "box": None,
            "file": chosen,
            "picture_id": None,
            "printed": "not_run",
            "evidence_kind": "inferred",
        }
        if fact is not None:
            cite["fact"] = _choice(fact, "fact", ("page_count", "file_name"))
            item = next((i for i in self.listing if str(i["name"]) == chosen), None)
            if cite["fact"] == "file_name":
                ok = value.removeprefix(UPLOADS + "/") in names
            else:
                ok = item is not None and _stated_count(value) == item.get("pages")
            cite["printed"] = "match" if ok else "not_found"
            cite["evidence_kind"] = "derived" if ok else "inferred"
            with self._lock:
                self.cites.append(cite)
            return dict(cite)
        given_box = raw.get("box")
        if isinstance(given_box, Mapping) and file_name and "file" not in given_box:
            given_box = {**given_box, "file": file_name}
        box = None if given_box is None else self._box(given_box, "box", architect=False)
        page = raw.get("page")
        if box is not None:
            page = box["page"]
        elif page is not None:
            page = _whole(page, "page")
        cite["page"], cite["box"] = page, box
        if box is None:
            cite["printed"] = "no_box"
            cite["evidence_kind"] = "inferred"
            with self._lock:
                self.cites.append(cite)
            return dict(cite)
        cite["picture_id"], _why = self._picture(box)
        cite["evidence_kind"] = "inferred"
        pdf = self.workspace.uploads / str(box["file"])
        try:
            cite["printed"] = printed(pdf, int(box["page"]), box, value)
        except Exception as failed:  # noqa: BLE001 - a check that cannot run is "not run"
            _log.warning("assistant chat cite check failed: %s", type(failed).__name__)
            cite["printed"] = "not_run"
        if cite["printed"] == "match":
            cite["evidence_kind"] = "observed"
        with self._lock:
            self.cites.append(cite)
        return dict(cite)

    def seed(self, record: Mapping[str, Any]) -> dict[str, Any] | None:
        """A finding made by code from a difference GV Review recorded (full check): the lead
        checks it (boxes, verified) or drops it with a reason; it is never silently lost."""
        page = record.get("page")
        if not isinstance(page, int) or page < 1:
            return None
        with self._lock:
            self._count += 1
            finding_id = f"F{self._count}"
        code = record.get("source") == "code"
        finding: dict[str, Any] = {
            "id": finding_id,
            "page": page,
            "vs_page": None,
            "item": " ".join(str(record.get("what") or "Recorded difference").split())[:_MAX_ITEM],
            "category": record.get("category") if record.get("category") in CATEGORIES else "other",
            "architect": str(record.get("architect") or "see GV Review's record")[:_MAX_VALUE],
            "vendor": str(record.get("vendor") or "see GV Review's record")[:_MAX_VALUE],
            "status": "different",
            "why": (str(record["why"])[:_MAX_WHY] if record.get("why") else None),
            "verified": False,
            "arch_box": None,
            "vendor_box": None,
            "source": "rules" if code else "gv_review",
            "parts": None,
            "printed_total": None,
            "rule_check_id": record.get("id") if code else None,
            "record_id": record.get("record_id"),
            "round": None,
            "arch_picture_id": None,
            "vendor_picture_id": None,
            "created_by": "gv_review",
        }
        if self.checker is not None:
            finding = self.checker(finding)
        with self._lock:
            self.findings[finding_id] = finding
        return dict(finding)

    def by(self, created_by: str) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(f) for f in self.findings.values() if f["created_by"] == created_by]

    def live(self) -> list[dict[str, Any]]:
        """The findings not dropped, in the order they were reported."""
        with self._lock:
            return [dict(f) for f in self.findings.values()]

    def replace(self, finding: dict[str, Any]) -> bool:
        """A finding changed by code (a check's result); False when it was dropped meanwhile."""
        with self._lock:
            if finding["id"] not in self.findings:
                return False
            self.findings[finding["id"]] = finding
            return True

    def pdf_paths(self) -> dict[str, Path]:
        return {
            str(item["name"]): self.workspace.uploads / str(item["name"]) for item in self.listing
        }

    # ---- coverage ----------------------------------------------------------------------------------

    def set_coverage(self, raw: object, created_by: str) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise SandboxRefused("the tool's input is not an object")
        unknown = set(raw) - {"pages"}
        if unknown:
            raise SandboxRefused(f"unknown argument {min(unknown)!r}")
        pages = raw.get("pages")
        if not isinstance(pages, list) or not pages:
            raise SandboxRefused("pages must be a list of pages")
        if len(pages) > _MAX_COVERAGE_PAGES:
            raise SandboxRefused(f"at most {_MAX_COVERAGE_PAGES} pages at once")
        entries: list[dict[str, Any]] = []
        for item in pages:
            if not isinstance(item, Mapping):
                raise SandboxRefused("each page must be an object")
            extra = set(item) - {"page", "pair_page", "status", "note"}
            if extra:
                raise SandboxRefused(f"unknown page field {min(extra)!r}")
            page = _whole(item.get("page"), "page")
            if self.page_count and page > self.page_count:
                raise SandboxRefused(
                    f"page {page} is not in the vendor's file (pages 1 to {self.page_count})"
                )
            pair = item.get("pair_page")
            note = item.get("note")
            entries.append(
                {
                    "page": page,
                    "pair_page": None if pair is None else _whole(pair, "pair_page"),
                    "status": _choice(item.get("status"), "status", COVERAGE_STATUSES),
                    "note": (
                        None
                        if note is None
                        else (_text(item, "note", _MAX_NOTE, empty=True) or None)
                    ),
                    "by": created_by,
                }
            )
        with self._lock:
            for entry in entries:
                self.coverage[entry["page"]] = entry
            self.coverage_used = True
        return self.coverage_map()

    def note_coverage(self, page: int, note: str) -> dict[str, Any] | None:
        """Code's note added to a page's recorded coverage (e.g. a helper's checklist gap); `None`
        when the page has no coverage recorded (it stays not opened)."""
        with self._lock:
            entry = self.coverage.get(page)
            if entry is None:
                return None
            joined = "; ".join(part for part in (entry.get("note"), note) if part)
            self.coverage[page] = {**entry, "note": joined[:_MAX_NOTE]}
        return self.coverage_map()

    def coverage_map(self) -> dict[str, Any]:
        """Every page of the vendor's file (a page never recorded: not opened), and the counts. A
        page or area answer (`scope_pages`): only its pages and the pages recorded."""
        with self._lock:
            recorded = dict(self.coverage)
            scope = self.scope_pages
        if scope is None:
            numbers = list(range(1, max(self.page_count, max(recorded, default=0)) + 1))
        else:
            numbers = sorted(set(scope) | set(recorded))
        pages = [
            recorded.get(
                page,
                {"page": page, "pair_page": None, "status": "not_opened", "note": None, "by": None},
            )
            for page in numbers
        ]
        counts = {"total": len(pages), **{status: 0 for status in COVERAGE_STATUSES}}
        for entry in pages:
            counts[entry["status"]] += 1
        return {"pages": [dict(entry) for entry in pages], "counts": counts}

    def coverage_gaps(self) -> list[int]:
        """Pages not accounted for: never recorded, or not opened with no note."""
        return [
            entry["page"]
            for entry in self.coverage_map()["pages"]
            if entry["status"] == "not_opened" and not entry["note"]
        ]


def _default_crop(pdf: Path, page: int, box: tuple[float, float, float, float]) -> bytes | None:
    drawing = import_module("app.review.assistant.chat_drawing")
    png: bytes | None = drawing.crop_png(pdf, page, box)
    return png


# ---- the marked-up PDF ---------------------------------------------------------------------------


def live_findings(blocks: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """An answer's findings as stored, in order, without the dropped ones."""
    return [
        dict(block["finding"])
        for block in blocks or []
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]


def markup_name(source_name: str, kind: str = "AI markup") -> str:
    stem = source_name[:-4] if source_name.lower().endswith(".pdf") else source_name
    return f"{stem} - {kind}.pdf"[:255]


@dataclass(frozen=True, slots=True)
class MarkupFile:
    name: str
    size: int
    sha256: str


def markup_base(workspace: ChatWorkspace) -> Any:
    """A writable copy of the vendor's file for `make_markup_file(base=...)`, made ahead of time
    (the slow part of the cloud drawing). Raises `LookupError` when the chat has no copy."""
    main = main_upload(read_listing(workspace))
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    drawing = import_module("app.review.assistant.chat_drawing")
    return drawing.clone_pdf(workspace.uploads / str(main["name"]))


def make_markup_file(
    workspace: ChatWorkspace,
    findings: Sequence[Mapping[str, Any]],
    out: Path,
    kind: str = "AI markup",
    base: Any = None,
    *,
    cloud_decisions: bool = False,
) -> MarkupFile:
    """The marked-up copy of the vendor's file, written to `out` (`chat_drawing.markup_pdf`): a
    tight red cloud, a numbered marker and a note at each Wrong finding's vendor box (else its
    architect box) when that box is on the vendor's file, a numbered amber marker at each one
    needing your decision (`cloud_decisions`: an amber cloud and note too), the draft stamp on page
    1, and every finding listed again on the notes pages. Raises `LookupError` when the chat has no
    copy of the set's PDFs. `base`: a `markup_base` copy to draw on (used once)."""
    name, placed = place_findings(workspace, findings)
    drawing = import_module("app.review.assistant.chat_drawing")
    drawing.markup_pdf(workspace.uploads / name, placed, out, base, cloud_decisions=cloud_decisions)
    data = out.read_bytes()
    return MarkupFile(markup_name(name, kind), len(data), hashlib.sha256(data).hexdigest())


type Placed = tuple[int, Mapping[str, Any], int | None, tuple[float, ...] | None, str]


def place_findings(
    workspace: ChatWorkspace, findings: Sequence[Mapping[str, Any]]
) -> tuple[str, list[Placed]]:
    """The vendor's file name, and each finding numbered with the page and box to mark on it (its
    vendor box, else its architect box, when that box is on the vendor's file), or `None` and why
    not. Raises `LookupError` when the chat has no copy of the set's PDFs."""
    listing = read_listing(workspace)
    main = main_upload(listing)
    if main is None or not (workspace.uploads / str(main["name"])).is_file():
        raise LookupError("no uploads")
    name = str(main["name"])
    placed: list[Placed] = []
    for number, finding in enumerate(findings, start=1):
        box = finding.get("vendor_box") or finding.get("arch_box")
        if not isinstance(box, Mapping):
            placed.append((number, finding, None, None, "Not marked on the drawing: no box."))
            continue
        if box.get("file") not in (None, name):
            placed.append(
                (number, finding, None, None, f"Not marked: its box is on {box.get('file')}.")
            )
            continue
        coords = tuple(float(box[key]) for key in ("x0", "y0", "x1", "y1"))
        where = f"Marked on page {box['page']}."
        placed.append((number, finding, int(box["page"]), coords, where))
    return name, placed


def make_report_file(
    workspace: ChatWorkspace,
    summary: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    not_checked: Sequence[str],
    out: Path,
) -> MarkupFile:
    """The full check's report PDF (`chat_drawing.check_report_pdf`), written to `out`."""
    main = main_upload(read_listing(workspace))
    name = "drawing set" if main is None else str(main["name"])
    drawing = import_module("app.review.assistant.chat_drawing")
    drawing.check_report_pdf(out, summary, findings, not_checked)
    data = out.read_bytes()
    return MarkupFile(
        markup_name(name, "AI check report"), len(data), hashlib.sha256(data).hexdigest()
    )
