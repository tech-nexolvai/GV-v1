"""Why each finding can be believed (chat v3, CONTRACT3-trust): checks, trust level, trust report.

Every finding carries `checks`, run by code (and one cheap independent AI read the engine makes):

- `printed_architect`, `printed_vendor`: is the stated value printed inside its box on the PDF's
  text layer (`match` / `not_found` / `no_text` / `no_box` / `not_a_number`)?
- `verify_architect`, `verify_vendor` (#1531): code's reading of each side's value at its box
  (`verify_value`): `match` / `mismatch` / `cannot_read`, by the drawing's text or, after the lead's
  answer (`drawn_ink_checks`), by two OCR engines on the vendor's drawn ink, with what it read and
  why in plain words. A drawn-ink match is the `ocr_ink` witness; a mismatch disagrees.
- `arithmetic`: for `source: vendor_arithmetic`, do the `parts` add up to `printed_total`?
- `rule`: for `source: rules` naming a GV check code ran (`rule_check_id`), does the recorded result
  agree (`agrees` / `disagrees` / `not_run`)? Code's result wins.
- `second_read`: a fresh model shown only the two crops (`agrees` / `disagrees` / `unsure` /
  `not_run`); run by the engine after the lead finishes.
- `lead_checked`: the lead read both values on the drawing itself (`verified`).
- `adjudication` (#1498): when the second read disagrees or is unsure, a third read by a stronger
  model on sharper crops; the majority of the three readings (`agrees_after_adjudication` /
  `disagrees` / `unsure`) stands in for the second read (`read_adjudication.py`).
- `critic` (#1498, whole-set answers): an adversarial model's look at the finding (`passed` /
  `objects` / `unsure` / `not_run`); `objects` makes it To check, `passed` is one more AI witness
  (`finding_critic.py`).

`inches` are code's numbers (`to_inches`), with the difference (vendor minus architect); the screen
never shows the AI's arithmetic. `trust_level`: `confirmed` / `likely` / `to_check`.

**Words (#1513).** The API values stay; people read `TRUST_WORDS` and `STATUS_WORDS`. The trust
level `to_check` reads "AI reading only" (no independent check confirmed the reading), and the
status `check` reads "Needs your decision" (the AI cannot call it right or wrong; a person decides),
so the two never share the words "To check".

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
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from time import monotonic
from types import ModuleType
from typing import Any, Final

from app.review.assistant import witness
from app.review.assistant.chat_findings import live_findings, page_ranges
from app.review.assistant.dim_findings import label_checks

__all__ = [
    "ACCURACY_CALIBRATION_LINE",
    "ACCURACY_MEASURED_LINE",
    "ACCURACY_POINTS",
    "CHECKS_MODULE",
    "CITE_POINTS",
    "CLOUD_WORDS",
    "MIN_SEEN_DPI",
    "SECOND_READ_MODULE",
    "STATUS_WORDS",
    "TRUST_WORDS",
    "Recorded",
    "accuracy",
    "apply_checks",
    "cite_evidence_kind",
    "cloud_kind",
    "drawn_ink_checks",
    "findings_csv",
    "judged",
    "ledger_witness",
    "load_module",
    "mark_compared",
    "needs_text",
    "read_result",
    "recorded_from",
    "recorded_missing",
    "rescored",
    "round_statuses",
    "states_drawing_values",
    "trust_level",
    "trust_report",
    "with_drawn_ink",
    "with_ledger",
]

_log = logging.getLogger(__name__)

CHECKS_MODULE: Final = "app.review.assistant.finding_checks"
SECOND_READ_MODULE: Final = "app.review.assistant.second_read"
REVISION_DIFF_MODULE: Final = "app.review.assistant.revision_diff"
#: The outcome word GV Review's records use for a code FAIL (`workflow.findings_composer`).
_FAIL_WORDS: Final = frozenset({"Needs correction"})
_PASS_WORDS: Final = frozenset({"Looks right"})
#: A code check that could not run because a job value is missing (`NOT_FOUND`).
_WAITING_WORDS: Final = frozenset({"Waiting on a value"})
LEVELS: Final = ("confirmed", "likely", "to_check")
#: What people read for each trust level (#1513); the API values above stay.
TRUST_WORDS: Final[Mapping[str, str]] = {
    "confirmed": "Confirmed",
    "likely": "Likely",
    "to_check": "AI reading only",
}
#: What people read for each finding status (#1513): `check` is a person's decision.
STATUS_WORDS: Final[Mapping[str, str]] = {
    "different": "Wrong",
    "check": "Needs your decision",
    "same": "Right",
}
#: The cloud drawing's and check report's words for each `cloud_kind`.
CLOUD_WORDS: Final[Mapping[str | None, str]] = {
    "wrong": "Wrong",
    "check": "Your decision",
    None: "Right",
}


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
        waiting: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.differences = [dict(item) for item in differences]
        self.results = dict(results or {})
        self.readable = readable
        self.waiting = [dict(item) for item in waiting]
        """Code checks that could not run for a missing job value: `{record_id, page, what}`."""


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
    waiting: list[dict[str, Any]] = []
    results: dict[str, str] = {}
    records = document.get("records")
    if isinstance(records, Mapping):
        for item in records.get("countertops") or []:
            if not isinstance(item, Mapping) or item.get("id") is None:
                continue
            results[str(item["id"])] = str(item.get("outcome") or "")
            if item.get("outcome") in _WAITING_WORDS:
                waiting.append(
                    {
                        "record_id": str(item["id"]),
                        "page": item.get("page"),
                        "what": str(item.get("label") or "countertop"),
                    }
                )
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
            if item.get("outcome") in _WAITING_WORDS:
                pages = [p for p in item.get("pages") or [] if isinstance(p, int)]
                waiting.append(
                    {
                        "record_id": str(item["id"]),
                        "page": pages[0] if pages else None,
                        "what": str(item.get("check") or "GV check"),
                    }
                )
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
    return Recorded(differences, results, waiting=waiting)


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


def trust_level(checks: Mapping[str, Any], status: object = None) -> str:
    """The trust rule (`witness.py`): Confirmed needs a non-AI witness and an AI reading that
    agrees; two AI readings alone are Likely at best; any disagreement is To check."""
    return witness.trust_level(checks, status if isinstance(status, str) else None)


#: The read's result (the third read's majority when one was made, #1498).
read_result = witness.read_result


def judged(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The finding with its trust level, evidence kind and non-AI witnesses worked out again from
    its checks (after any check changed)."""
    out = dict(finding)
    checks = finding.get("checks") or {}
    status = finding.get("status") if isinstance(finding.get("status"), str) else None
    out["trust_level"] = trust_level(checks, status)
    out["evidence_kind"] = witness.evidence_kind(checks, status)
    out["witnesses"] = witness.witnesses(checks, status)
    return out


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
        "ledger": _ledger_made(finding),
        "units": None,
        "adjudication": None,
        "critic": None,
        "verify_architect": None,
        "verify_vendor": None,
    }
    module = load_module(CHECKS_MODULE)
    if module is not None:
        try:
            found = module.check_finding(finding, pdf_paths).as_dict()
            for key in (
                "printed_architect",
                "printed_vendor",
                "arithmetic",
                "units",
                "verify_architect",
                "verify_vendor",
            ):
                if key in found:
                    checks[key] = found[key]
        except Exception as failed:  # noqa: BLE001 - a check that fails is "not run", never a crash
            _log.warning("assistant chat finding check failed: %s", type(failed).__name__)
    # A side built from a numbered dimension label was read by code (#1558).
    checks = label_checks(finding, checks)
    architect, vendor = _inches(finding.get("architect")), _inches(finding.get("vendor"))
    out["checks"] = checks
    # The third read and the critic start again too (#1498): they come after the lead's answer.
    out["reads"] = None
    out["critic"] = None
    # Nobody has decided on a finding being written (#1521): the reviewer's decision is added when
    # the stored answer is read.
    out["decision"] = None
    out["inches"] = {
        "architect": _number(architect),
        "vendor": _number(vendor),
        "difference": (
            None if architect is None or vendor is None else _number(vendor - architect)
        ),
    }
    # Set for a "same" item at the finish (`mark_compared`): not seen is not right.
    out.setdefault("compared", None)
    out.setdefault("not_compared_why", None)
    return judged(out)


def _ledger_made(finding: Mapping[str, Any]) -> str | None:
    """A cross-view finding the reading ledger made: code compared the views (derived)."""
    if finding.get("created_by") == "ledger" and finding.get("source") == "cross_view":
        return "agrees"
    return None


#: The most seconds the drawn-ink pass spends on one answer: a side not reached stays unread.
DRAWN_INK_BUDGET_S: Final = 120.0


def drawn_ink_checks(
    findings: Sequence[Mapping[str, Any]],
    pdf_paths: Mapping[str, Path],
    *,
    budget_s: float = DRAWN_INK_BUDGET_S,
    stop: Callable[[], bool] = lambda: False,
) -> dict[str, dict[str, Any]]:
    """The drawn-ink route (#1531) for every finding side its text route left `pending`, in the
    order a reviewer acts on them (differences first), within `budget_s` seconds. `{finding id:
    {verify_<side>: answer, ...}}`, with the values and boxes they were read for (`with_drawn_ink`
    applies an answer only to a finding that still claims them). $0, no network."""
    module = load_module(CHECKS_MODULE)
    run = getattr(module, "drawn_checks", None) if module is not None else None
    if run is None:
        return {}
    waiting = [
        f
        for f in findings
        if any(
            isinstance((f.get("checks") or {}).get(key), Mapping)
            and (f.get("checks") or {})[key].get("pending")
            for key in ("verify_architect", "verify_vendor")
        )
    ]
    waiting.sort(key=lambda f: {"different": 0, "check": 1}.get(str(f.get("status")), 2))
    out: dict[str, dict[str, Any]] = {}
    started = monotonic()
    for finding in waiting:
        if stop() or monotonic() - started > budget_s:
            break
        try:
            found = dict(run(finding, pdf_paths))
        except Exception as failed:  # noqa: BLE001 - an unread side stays as it was
            _log.warning("assistant chat drawn-ink check failed: %s", type(failed).__name__)
            continue
        if found:
            found["_claims"] = _claims(finding)
            out[str(finding.get("id"))] = found
    return out


def _claims(finding: Mapping[str, Any]) -> list[Any]:
    return [finding.get(k) for k in ("architect", "vendor", "arch_box", "vendor_box")]


def with_drawn_ink(
    findings: Sequence[Mapping[str, Any]], found: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The findings `drawn_ink_checks` read, with their sides' new readings and trust level (a
    finding whose values or boxes changed since is left as it is)."""
    out: list[dict[str, Any]] = []
    for finding in findings:
        answer = found.get(str(finding.get("id")))
        if not answer or answer.get("_claims") != _claims(finding):
            continue
        checks = dict(finding.get("checks") or {})
        for key in ("verify_architect", "verify_vendor"):
            if isinstance(answer.get(key), Mapping):
                checks[key] = dict(answer[key])
        out.append(judged({**finding, "checks": checks}))
    return out


def with_second_read(finding: Mapping[str, Any], result: str) -> dict[str, Any]:
    out = dict(finding)
    out["checks"] = {**(finding.get("checks") or {}), "second_read": result}
    return judged(out)


def ledger_witness(finding: Mapping[str, Any], ledger: Mapping[str, Any] | None) -> str | None:
    """Does the reading ledger (`ReadingLedger.as_dict()`) back the finding's values? `agrees` when
    every side with a number is backed by code: a recorded string on that side and page whose
    printed overall is the value and adds up, or two or more readings of the same item on that
    side (other views) that all give the value. `None` when the ledger says nothing about it.

    A ledger that only disagrees is not used here: the ledger reports those as its own
    `cross_view` findings."""
    if not isinstance(ledger, Mapping):
        return None
    module = load_module("app.review.assistant.ledger")
    key_of = getattr(module, "item_key", None) if module is not None else None
    item_key: str | None = None
    if key_of is not None:
        try:
            item_key = str(key_of(str(finding.get("item") or ""))[0]) or None
        except Exception:  # noqa: BLE001 - an item code cannot key: no ledger witness
            item_key = None
    backed: list[bool] = []
    for side, page in (
        ("vendor", (finding.get("vendor_box") or {}).get("page") or finding.get("page")),
        ("architect", (finding.get("arch_box") or {}).get("page") or finding.get("vs_page")),
    ):
        value = _inches(finding.get(side))
        if value is None:
            continue
        strings = any(
            entry.get("side") == side
            and entry.get("page") == page
            and entry.get("result") == "adds_up"
            and _inches(entry.get("printed_total")) == value
            for entry in ledger.get("strings") or []
            if isinstance(entry, Mapping)
        )
        views = [
            reading
            for reading in ledger.get("readings") or []
            if isinstance(reading, Mapping)
            and item_key
            and reading.get("item_key") == item_key
            and reading.get("side") == side
        ]
        agree = len(views) >= 2 and all(
            reading.get("inches") is not None and Decimal(str(reading["inches"])) == value
            for reading in views
        )
        backed.append(strings or agree)
    return "agrees" if backed and all(backed) else None


def with_ledger(finding: Mapping[str, Any], ledger: Mapping[str, Any] | None) -> dict[str, Any]:
    """The finding with the reading ledger's witness recorded (for the engine's finish, once the
    ledger is complete)."""
    checks = dict(finding.get("checks") or {})
    if checks.get("ledger") is None:
        checks["ledger"] = ledger_witness(finding, ledger)
    return judged({**finding, "checks": checks})


# ---- not seen is not right (P5) ------------------------------------------------------------------


#: The least sharpness at which a zoom crop counts as seeing a box: a picture coarser than this
#: (a whole sheet, the set pack's 100 dpi page pictures) is too blurry to read a dimension by.
MIN_SEEN_DPI: Final = 150

_Seen = tuple[str | None, int, tuple[float, float, float, float] | None]
"""One sharp enough look: (file or None, page, box in PDF points or None for the whole page)."""


def _corners(raw: object) -> tuple[float, float, float, float] | None:
    if not isinstance(raw, Mapping):
        return None
    values = [raw.get(key) for key in ("x0", "y0", "x1", "y1")]
    numbers = [float(v) for v in values if isinstance(v, int | float) and not isinstance(v, bool)]
    if len(numbers) != 4:
        return None
    x0, y0, x1, y1 = numbers
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _seen_entries(seen: Iterable[Any]) -> list[_Seen]:
    """The seen log's sharp enough looks: a crop below `MIN_SEEN_DPI` is left out."""
    out: list[_Seen] = []
    for entry in seen:
        box = None
        if isinstance(entry, Mapping):
            file, page = entry.get("file"), entry.get("page")
            dpi = entry.get("dpi")
            if isinstance(dpi, int | float) and not isinstance(dpi, bool) and dpi < MIN_SEEN_DPI:
                continue
            box = _corners(entry.get("box"))
        elif isinstance(entry, tuple) and len(entry) == 2:
            file, page = entry
        else:
            file, page = None, entry
        if isinstance(page, int) and not isinstance(page, bool):
            out.append((file if isinstance(file, str) else None, page, box))
    return out


def _viewed(box: object, page: object, seen: Sequence[_Seen]) -> bool:
    """Whether a side was looked at: a sharp enough look at its page (and its file, when both
    name one) whose crop holds the middle of the side's box (when both have a box)."""
    file = box.get("file") if isinstance(box, Mapping) else None
    if isinstance(box, Mapping) and isinstance(box.get("page"), int):
        page = box["page"]
    if not isinstance(page, int):
        return False
    corners = _corners(box)
    for seen_file, seen_page, crop in seen:
        if seen_page != page or (file is not None and seen_file not in (None, file)):
            continue
        if corners is None or crop is None:
            return True
        middle_x, middle_y = (corners[0] + corners[2]) / 2, (corners[1] + corners[3]) / 2
        if crop[0] <= middle_x <= crop[2] and crop[1] <= middle_y <= crop[3]:
            return True
    return False


def not_compared_why(
    finding: Mapping[str, Any],
    coverage: Mapping[str, Any] | None,
    seen: Iterable[Any] | None = None,
) -> str | None:
    """Why a "What's right" (`same`) finding cannot count as right: `not_viewed` (both sides were
    not actually looked at: the zoom log when there is one, each side's box inside a crop of at
    least `MIN_SEEN_DPI`; otherwise the lead's own check),
    or `pair_not_covered` (its page pair is not compared in the coverage map, or it has no
    architect side). `None`: it counts (or it is not a `same` finding)."""
    if finding.get("status") != "same":
        return None
    if seen is not None:
        pages = _seen_entries(seen)
        if not (
            _viewed(finding.get("vendor_box"), finding.get("page"), pages)
            and _viewed(finding.get("arch_box"), finding.get("vs_page"), pages)
        ):
            return "not_viewed"
    elif not (finding.get("checks") or {}).get("lead_checked", finding.get("verified")):
        return "not_viewed"
    if not (finding.get("arch_box") or finding.get("vs_page")):
        return "pair_not_covered"
    if coverage:
        entry = next(
            (e for e in coverage.get("pages") or [] if e.get("page") == finding.get("page")), None
        )
        if entry is None or entry.get("status") not in ("same", "different"):
            return "pair_not_covered"
    return None


def mark_compared(
    findings: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    seen: Iterable[Any] | None = None,
) -> list[dict[str, Any]]:
    """The `same` findings whose `compared` / `not_compared_why` changed (to store and show): a
    `same` item counts as right only when both sides were seen and its page pair is covered;
    otherwise it moves to "Not compared"."""
    seen_list = None if seen is None else list(seen)
    changed: list[dict[str, Any]] = []
    for finding in findings:
        if finding.get("status") != "same":
            continue
        why = not_compared_why(finding, coverage, seen_list)
        compared = why is None
        if finding.get("compared") is compared and finding.get("not_compared_why") == why:
            continue
        changed.append({**finding, "compared": compared, "not_compared_why": why})
    return changed


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
    unknowns: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The answer's trust report, built by code from its findings and coverage. `dismissed`:
    recorded differences the lead set aside, with its reason. `unknowns`: what it did not check
    (`chat_unknowns.unknown_records`), counted by why."""
    counts = {level: 0 for level in LEVELS}
    evidence = {kind: 0 for kind in witness.EVIDENCE_KINDS}
    not_compared = 0
    by_category: dict[str, dict[str, int]] = {}
    printed = {k: 0 for k in ("match", "not_found", "no_text", "not_a_number", "no_box", "not_run")}
    second = {k: 0 for k in ("agrees", "disagrees", "unsure", "not_run")}
    adjudication = {k: 0 for k in ("agrees_after_adjudication", "disagrees", "unsure")}
    critic = {k: 0 for k in ("passed", "objects", "unsure", "not_run")}
    third_not_run = 0
    arithmetic = {k: 0 for k in ("adds_up", "does_not_add_up", "not_a_number")}
    rules: list[dict[str, Any]] = []
    lead_checked = 0
    pen_pages: set[int] = set()
    for finding in findings:
        if finding.get("compared") is False:
            not_compared += 1
            continue
        level = str(finding.get("trust_level") or "to_check")
        counts[level] = counts.get(level, 0) + 1
        kind = str(finding.get("evidence_kind") or "inferred")
        evidence[kind] = evidence.get(kind, 0) + 1
        category = by_category.setdefault(
            str(finding.get("category") or "other"), {k: 0 for k in LEVELS}
        )
        category[level] = category.get(level, 0) + 1
        checks = finding.get("checks") or {}
        state = _printed(checks)
        printed[state] += 1
        if any(
            checks.get(f"printed_{side}") == "no_text"
            and (checks.get(f"verify_{side}") or {}).get("result") != "match"
            for side in ("architect", "vendor")
        ):
            pen_pages.add(int(finding.get("page") or 0))
        second[str(checks.get("second_read") or "not_run")] = (
            second.get(str(checks.get("second_read") or "not_run"), 0) + 1
        )
        if checks.get("arithmetic") in arithmetic:
            arithmetic[checks["arithmetic"]] += 1
        if checks.get("adjudication") in adjudication:
            adjudication[checks["adjudication"]] += 1
        elif checks.get("second_read") in ("disagrees", "unsure"):
            third_not_run += 1
        if checks.get("critic") in critic:
            critic[checks["critic"]] += 1
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
    if third_not_run:
        not_checked.append(
            f"Third read not run on {third_not_run} findings whose second read disagreed or was "
            "unsure (limit, cap or no box)."
        )
    if critic["not_run"]:
        not_checked.append(f"Critic not run on {critic['not_run']} findings (limit or cap).")
    if recorded is None or not recorded.readable:
        not_checked.append("GV Review's own recorded checks could not be read for this answer.")
    by_why: dict[str, int] = {}
    for unknown in unknowns:
        by_why[str(unknown.get("why"))] = by_why.get(str(unknown.get("why")), 0) + 1
    return {
        "counts": {**counts, "total": len(findings) - not_compared},
        "evidence": evidence,
        "not_compared": not_compared,
        "unknowns": {"total": len(unknowns), "by_why": by_why},
        "by_category": by_category,
        "checks": {
            "printed_at_spot": printed,
            "second_read": second,
            "adjudication": adjudication,
            "critic": critic,
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
    """`wrong` (red), `check` (amber: needs your decision), or `None` (right: never clouded): the
    finding's status, as the screen counts it (status `different` is Wrong whatever its trust; the
    cloud drawing's note then carries the trust words, #1541). The same count everywhere: screen,
    progress line, summary, cloud drawing and check report."""
    status = finding.get("status")
    if status == "same":
        return None
    return "wrong" if status == "different" else "check"


def needs_text(finding: Mapping[str, Any]) -> str:
    """What the vendor's value needs to be: code's inches when it read the architect's value."""
    inches = (finding.get("inches") or {}).get("architect")
    return f'{inches}"' if inches else str(finding.get("architect"))


# ---- the accuracy score (CONTRACT5) -------------------------------------------------------------

#: What the eval kit measured on GV's test sets (the orchestrator updates this line; never invented).
ACCURACY_MEASURED_LINE: Final = (
    "Measured on GV's test sets: 53 of 55 checkable findings right in a full check."
)

#: Points per finding, by trust level and evidence kind: the share of such findings expected to be
#: right. CALIBRATED from the strict eval on GV's test sets (the orchestrator updates this table
#: and the line below from each eval run; never invented). A finding a check disagrees with always
#: scores `ACCURACY_DISAGREES` (0), and a "What's right" item nobody compared is not scored.
ACCURACY_POINTS: Final[Mapping[str, Mapping[str, float]]] = {
    "confirmed": {"observed": 1.0, "derived": 1.0, "inferred": 1.0},
    "likely": {"observed": 0.95, "derived": 0.95, "inferred": 0.95},
    "to_check": {"observed": 0.9, "derived": 0.9, "inferred": 0.9},
}
ACCURACY_DISAGREES: Final = 0.0
#: Points per cited value, by the printed-at-spot check (pen strokes: like an AI reading only).
CITE_POINTS: Final[Mapping[str, float]] = {
    "match": 1.0,
    "no_text": 0.9,
    "not_found": 0.3,
}
_CITE_OTHER: Final = 0.3
#: How the points were calibrated (the click-through says it).
ACCURACY_CALIBRATION_LINE: Final = (
    "Points are the share of such claims expected to be right: in a full check on GV's test sets, "
    "Confirmed 6 of 6, Likely 4 of 4 and AI reading only 43 of 45 checkable findings were right."
)

_KIND_WORDS: Final = {
    "observed": "printed on the drawing",
    "derived": "worked out by code",
    "inferred": "AI reading only",
}


def _how(finding: Mapping[str, Any], level: str, kind: str) -> str:
    found = [
        witness.WITNESS_WORDS[w]
        for w in finding.get("witnesses") or []
        if w in witness.WITNESS_WORDS
    ]
    level_words = TRUST_WORDS.get(level, level.replace("_", " "))
    kind_words = _KIND_WORDS.get(kind, kind)
    words = level_words if kind_words == level_words else f"{level_words}, {kind_words}"
    return f"{words} ({'; '.join(found)})" if found else words


def cite_evidence_kind(cite: Mapping[str, Any]) -> str:
    """A cited value: `observed` (printed at that spot), `derived` (a fact about the file, checked
    by code), or `inferred` (the AI's reading only)."""
    if cite.get("printed") == "match":
        return "derived" if cite.get("fact") else "observed"
    return "inferred"


def accuracy(
    findings: Sequence[Mapping[str, Any]],
    cites: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    unknowns: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The share of the answer's checkable claims expected to be right, by code.

    A finding scores by its trust level and evidence kind (`ACCURACY_POINTS`, calibrated on GV's
    test sets); any check that disagrees: 0; GV's recorded result quoted as recorded: 1. A "What's
    right" item that was not compared (`compared: false`) is not a claim: it is counted apart. A
    cited value: by its printed-at-spot check (`CITE_POINTS`); a fact about the file checked by
    code 1 (0 when it is wrong). Coverage: a whole-set answer is multiplied by the share of pages
    accounted for, and every answer by its claims over claims plus item unknowns. No claim to
    check: n/a."""
    breakdown: list[dict[str, Any]] = []
    not_compared = 0
    for finding in findings:
        if finding.get("compared") is False:
            not_compared += 1
            continue
        checks = finding.get("checks") or {}
        status = finding.get("status") if isinstance(finding.get("status"), str) else None
        claim = f"{finding.get('id')} page {finding.get('page')}: {finding.get('item')}"
        level = str(finding.get("trust_level") or trust_level(checks, status))
        kind = str(finding.get("evidence_kind") or witness.evidence_kind(checks, status))
        against = witness.disagreement(checks, status)
        note = str((finding.get("critic") or {}).get("note") or "").strip()
        if against == "the critic objects" and note:
            against = f"the critic objects: {note}"
        if against:
            points, how = ACCURACY_DISAGREES, against
        elif finding.get("created_by") == "gv_review" and finding.get("status") != "same":
            points, how = 1.0, "GV Review's own recorded result"
        elif "rule" in witness.witnesses(checks, status) or "arithmetic" in witness.witnesses(
            checks, status
        ):
            # Code decides the whole claim (GV's rule, or the vendor's own sum, the reading
            # ledger's included): it scores as confirmed (CONTRACT5).
            points, how = 1.0, (
                "code (the vendor's own arithmetic, from the reading ledger)"
                if finding.get("created_by") == "ledger"
                else "code (GV's rule or the vendor's own arithmetic)"
            )
        else:
            points = ACCURACY_POINTS.get(level, ACCURACY_POINTS["to_check"]).get(kind, 0.0)
            how = _how(finding, level, kind)
        breakdown.append({"claim": claim, "how": how, "points": points, "evidence_kind": kind})
    for cite in cites:
        claim = f"{cite.get('id')} {cite.get('value')}"
        check = str(cite.get("printed") or "not_run")
        kind = str(cite.get("evidence_kind") or cite_evidence_kind(cite))
        if cite.get("fact"):
            points = 1.0 if check == "match" else 0.0
            how = "checked on the file by code" if points else "the file says otherwise"
        else:
            points = CITE_POINTS.get(check, _CITE_OTHER)
            how = {
                "match": "printed at that spot",
                "no_text": "drawn as strokes: not checkable by the page's text",
                "not_found": "not found at that spot",
            }.get(check, "could not be checked at a spot")
        breakdown.append({"claim": claim, "how": how, "points": points, "evidence_kind": kind})
    item_unknowns = sum(1 for u in unknowns if u.get("scope") != "page")
    base: dict[str, Any] = {
        "measured": ACCURACY_MEASURED_LINE,
        "calibration": ACCURACY_CALIBRATION_LINE,
        "not_compared": not_compared,
        "unknowns": len(unknowns),
    }
    if not breakdown:
        return {
            "score": None,
            "level": "n/a",
            "claims": 0,
            "breakdown": [],
            "coverage_factor": None,
            **base,
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
    if item_unknowns:
        factor = (1.0 if factor is None else factor) * (
            len(breakdown) / (len(breakdown) + item_unknowns)
        )
    if factor is not None:
        mean *= factor
    score = round(mean * 100)
    level = "green" if score >= 80 else "orange" if score >= 60 else "yellow"
    return {
        "score": score,
        "level": level,
        "claims": len(breakdown),
        "breakdown": breakdown,
        "coverage_factor": None if factor is None else round(factor, 3),
        **base,
    }


def rescored(
    stored: Mapping[str, Any] | None, blocks: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any] | None:
    """An answer's stored accuracy, scored again from its stored findings with today's points
    table (#1513): answers made before a recalibration otherwise keep their old badge. Nothing is
    written (the tables are append-only); `rescored: true` when the score changed. The claims'
    words are today's either way.

    The findings, cited values, page coverage and unknowns come from the answer's blocks. A whole
    set answer whose page coverage was never recorded keeps its stored coverage factor (the page
    count is not in the blocks). `None` stays `None`; an answer that cannot be scored again keeps
    its stored score."""
    if not isinstance(stored, Mapping):
        return None
    out = dict(stored)
    blocks = [b for b in blocks or [] if isinstance(b, Mapping)]
    try:
        findings = live_findings(blocks)
        cites = [dict(b["cite"]) for b in blocks if b.get("type") == "cite" and b.get("cite")]
        coverage = next(
            (dict(b["coverage"]) for b in reversed(blocks) if b.get("type") == "coverage"), None
        )
        unknowns = next(
            (list(b["unknowns"]) for b in reversed(blocks) if b.get("type") == "unknowns"), []
        )
        scope = next((b.get("scope") for b in reversed(blocks) if b.get("type") == "scope"), None)
        if isinstance(scope, Mapping) and scope.get("kind") != "whole_set":
            coverage = None  # a page or area answer is scored without the page penalty (#1520)
        factor = stored.get("coverage_factor")
        if coverage is None and factor is not None:
            fresh = accuracy(findings, cites, None)
            if fresh["score"] is not None:
                mean = sum(item["points"] for item in fresh["breakdown"]) / fresh["claims"]
                fresh["score"] = round(mean * float(factor) * 100)
                score = fresh["score"]
                fresh["level"] = "green" if score >= 80 else "orange" if score >= 60 else "yellow"
                fresh["coverage_factor"] = factor
            fresh["unknowns"] = stored.get("unknowns", fresh["unknowns"])
        else:
            fresh = accuracy(findings, cites, coverage, unknowns)
    except Exception:  # noqa: BLE001 - an old answer the scoring cannot read keeps its score
        _log.exception("assistant chat accuracy could not be scored again")
        return out
    # Today's words and claims either way; `rescored` only when the number itself moved.
    return {**fresh, "rescored": fresh["score"] != stored.get("score")}


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
    "Evidence",
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
    "Decision",
    "Decision note",
    "Decided by",
    "Decided at",
)
#: How the reviewer's decision (#1521) reads in the CSV.
_DECISION_WORDS: Final = {"agree": "Agreed", "not_a_problem": "Not a problem"}
_FORMULA_START: Final = ("=", "+", "-", "@", "\t", "\r")


def _cell(value: object) -> str:
    """A text cell that a spreadsheet never runs as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


def _decision_cells(finding: Mapping[str, Any]) -> list[str]:
    """Decision, its note, who and when; "Not yet decided" for an undecided wrong or
    your-decision item (a right item needs no decision)."""
    decision = finding.get("decision")
    if not isinstance(decision, Mapping):
        return ["" if cloud_kind(finding) is None else "Not yet decided", "", "", ""]
    at = decision.get("at")
    return [
        _DECISION_WORDS.get(str(decision.get("decision")), str(decision.get("decision"))),
        _cell(decision.get("note")),
        _cell(decision.get("by")),
        at.isoformat() if isinstance(at, datetime) else str(at or ""),
    ]


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
                STATUS_WORDS.get(str(finding.get("status")), finding.get("status") or ""),
                TRUST_WORDS.get(str(finding.get("trust_level")), ""),
                _KIND_WORDS.get(str(finding.get("evidence_kind")), ""),
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
                *_decision_cells(finding),
            ]
        )
    return "﻿" + out.getvalue()
