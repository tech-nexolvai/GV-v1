"""Escalation (#1619): every item code could not compare at first is tried again, cheapest first.

The architect's drawings are the source of truth and nobody at GV is asked to decide anything
(Anant, 2026-10-18; the rules are `classification.py`). Before an item falls to "Not compared",
inside the full check and its fixed price, it climbs a ladder, the biggest items first
(`impact.by_impact`), and stops at the first step that settles it:

0. **The reference is the architect's** (Anant, 2026-10-22; `round_reference`, $0): an item
   compared only with the vendor's own drawing (its round 1) is never Right; code records the
   round match as information ("Unchanged since round 1") and compares the item with the architect
   instead where it links the vendor's label to an architect label (another item comparing the
   same label, code's pairs, the earlier round's own item); else it stays Not compared.
1. **Free code steps** (this module, $0):
   - `labels`: the drawn-label reader (`dims.json`, set pack v3): code's numbered label inside the
     item's box that prints the item's value is a code reading of it (`printed_<side>`), and a
     vendor value nobody could read becomes the label's value;
   - `under_markup`: the drawing's own text under GV's markup (`under_markup.read_under_markup`),
     for a vendor value hidden by GV's boxes;
   - `same_item`: the same item on the vendor's other views or rounds: a copy that is already a
     proven Wrong carries it, and several open copies become one item;
   - `vendor_sums`: a string of the vendor's own that code already proved does not add up
     (`vendor_consistency`) carries the item;
   - `pairs`: code's own pairing of the two labels by drawn position (`pairs.json`): a proof that
     both numbers measure the same thing (`paired_by: code`);
   - `vendor_search` (accuracy step 1): an item the AI says the vendor's drawing does not show
     (`classification.missing_on_vendor`): code searches every vendor drawing of its area (the
     page map) for the architect's printed value (`spot_locate.search_area`) and records what it
     found (`vendor_search`), so the Not-compared reason says it. A dimension the vendor does
     not print does not by itself prove the item is left out (code does not recognise every
     symbol, #1648), and the same value printed elsewhere does not prove it is the same
     dimension. Only a **millwork item** code proves missing is red (Anant, 2026-10-22,
     `missing_items.judge`: the architect's code-read value, millwork by its words and not a room
     feature, every vendor drawing of the area read and none printing it or naming the item);
     every other item keeps its outcome, its reason saying why it is not red. Copies of the same
     architect label fold into the red.
   Missed, changed and added finishes (R4) are code's own finish check (`architect_items`, #1623),
   which runs at the finish of every full check; its findings are proved there, never here.
2. **One deeper AI look** per item that is left (the roster, `full_check_roster`): a sharper crop
   through the existing verifier and devil's advocate, under the pairing-proof setting, and only
   with the money left under the fixed price.
3. **Whatever is still open** is Not compared, with its reason and what would settle it
   (`classification`), listed in the separate Not-compared PDF, never on the cloud drawing.

Code decides every outcome, with the same witness rules as the red clouds: a step only adds code's
readings (a label, the text under the markup, code's pairing) or folds a copy into
the item that already carries it; a Right needs both values read by code and equal; a red cloud
still needs `proof.code_proved`. Every step tried is kept on the finding (`escalation`), so the
reviewer and the replay can see what each item went through.

`escalate` is pure (its readers are passed in); `escalate_answer(engine)` is the engine's hook.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import classification, missing_items, proof
from app.review.assistant.chat_trust import inch_words, judged, round_item_key
from app.review.assistant.full_check_advocate import code_pairs
from app.review.assistant.impact import by_impact
from app.review.assistant.round_reference import applies as compares_drawings
from app.review.assistant.round_reference import compare_with_architect
from app.review.assistant.spot_locate import VENDOR_SEARCH, search_area, search_words

__all__ = [
    "FREE_STEPS",
    "PRECISION",
    "Escalation",
    "EscalationContext",
    "context_from_pack",
    "escalate",
    "escalate_answer",
    "label_provenance",
    "undecided",
]

_log = logging.getLogger(__name__)

type StepName = Literal[
    "labels",
    "under_markup",
    "same_item",
    "vendor_sums",
    "pairs",
    "equal_parts",
    "read_number",
    "vendor_search",
]
#: The free code steps, cheapest first.
FREE_STEPS: Final[tuple[StepName, ...]] = (
    "labels",
    "under_markup",
    "same_item",
    "vendor_sums",
    "pairs",
    "vendor_search",
)
#: Two printed values this close are the same (the drawings' smallest step, as the resolver).
PRECISION: Final = Decimal("0.0625")
#: How far round a finding's box a label may sit and still be read as the box's value (points).
_SLACK_PT: Final = 8.0
#: A tag code as drawings print them (CT-001, ST-002-CUST, WD-01).

#: (file, page, box) -> the drawing's own words under GV's markup in that box (text, colour).
type UnderReader = Callable[[str, int, tuple[float, float, float, float]], list[dict[str, Any]]]
#: The read-number tool bound to the set pack (`read_number.NumberReader`): a `Request` in, a
#: `Reading` out.
type NumberReader = Callable[[Any], Any]
#: A side that prints only equal-parts marks (EQ / EQ), no number.
_EQ: Final = re.compile(r"\b(?:EQ|EQUAL)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class EscalationContext:
    """What the free steps read: the set pack's facts and the drawing's text under the markup."""

    main_file: str | None
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    page_map: Mapping[str, Any] | None = None
    pairs: Mapping[str, str] = field(default_factory=dict)
    under: UnderReader | None = None
    reread: NumberReader | None = None
    """The read-number tool, for a vendor value that has no spot (#1746)."""
    phrases: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    """Each page's text-layer phrases (`dims.json`), for a missing item's words and search."""
    page_texts: Mapping[tuple[str, int], str] = field(default_factory=dict)
    """Each page's whole text (`page_texts.json`)."""
    pictures: Mapping[tuple[str, int], float] = field(default_factory=dict)
    """Each page's share covered by pictures (`health.json`)."""
    annotations: Sequence[Mapping[str, Any]] = ()
    """GV's markup (`annotations.json`): its typed notes are never the architect's value."""
    unopened: frozenset[int] | None = None
    """The main file's pages the check never opened (its coverage); None: not known (none)."""


@dataclass(slots=True)
class Escalation:
    """What the free steps did to one answer's findings."""

    changed: list[dict[str, Any]] = field(default_factory=list)
    absorbed: dict[str, str] = field(default_factory=dict)
    """Folded copies: {finding id: the reason, naming the item that carries it}."""
    tried: int = 0
    settled: dict[str, int] = field(default_factory=dict)
    """How many items each step settled (an outcome: wrong, right or folded)."""
    log: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    """Per finding id: each step tried, its outcome and what code found."""
    round_matches: int = 0
    """Items compared only with the vendor's own drawing (`round_reference`)."""
    linked: dict[str, str] = field(default_factory=dict)
    """Per finding id: how code compared such an item with the architect instead."""

    def block(self) -> dict[str, Any]:
        return {
            "tried": self.tried,
            "settled": dict(self.settled),
            "absorbed": len(self.absorbed),
            "round_matches": self.round_matches,
            "linked": len(self.linked),
        }


# ---- small readers ------------------------------------------------------------------------------


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _box(raw: object) -> tuple[str | None, int, tuple[float, float, float, float]] | None:
    if not isinstance(raw, Mapping):
        return None
    try:
        page = int(raw["page"])
        x0, y0, x1, y1 = (float(raw[key]) for key in ("x0", "y0", "x1", "y1"))
    except (KeyError, TypeError, ValueError):
        return None
    name = raw.get("file")
    return (
        (None if name is None else str(name)),
        page,
        (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)),
    )


def _centre(box: Sequence[float]) -> tuple[float, float]:
    return (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2


def _inside(point: tuple[float, float], box: Sequence[float], slack: float = _SLACK_PT) -> bool:
    return (
        box[0] - slack <= point[0] <= box[2] + slack
        and box[1] - slack <= point[1] <= box[3] + slack
    )


def undecided(finding: Mapping[str, Any]) -> bool:
    """An item the escalation works on: Not compared today (a Right item not looked at on both
    sides aside), never one the reviewer decided, nor GV's filler-range note (`filler_range`), site-fit item
    (`run_fit`) or sink front offset item (`sink_offset`)."""
    if finding.get("created_by") == "filler_range":
        return False  # GV's filler range decided it, the architect first: nothing to climb
    if finding.get("created_by") == "run_fit":
        return False  # GV's site-fit rule decided it by its own arithmetic: nothing to climb
    if finding.get("created_by") == "sink_offset":
        return False  # GV's sink front offset: code proved it or said why not (#1807)
    return proof.proof_group(finding) == "not_compared" and finding.get("status") != "same"


def _labels_in(
    ctx: EscalationContext, finding: Mapping[str, Any], key: str, side: str
) -> list[Mapping[str, Any]]:
    where = _box(finding.get(key))
    if where is None:
        return []
    name, page, box = where
    file = name or ctx.main_file or ""
    return [
        dict(label, page=page, file=file)
        for label in ctx.labels.get((file, page), [])
        if label.get("side") == side and _inside(_centre(label.get("box") or (0, 0, 0, 0)), box)
    ]


def _same(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= PRECISION / 2


def _provenance(label: Mapping[str, Any]) -> dict[str, Any]:
    """A label as a finding's `dims` side keeps it (where its value came from)."""
    return {
        "id": str(label.get("id")),
        "page": int(label.get("page") or 0),
        "file": label.get("file"),
        "text": str(label.get("text") or ""),
        "inches": str(label.get("inches")),
        "route": "drawn" if label.get("route") == "drawn" else "text",
        "side": label.get("side"),
        "orientation": label.get("orientation"),
        "box": [float(v) for v in label.get("box") or (0, 0, 0, 0)],
    }


label_provenance = _provenance


def _ref(label: Mapping[str, Any]) -> str:
    return f"p{label.get('page')}.{label.get('id')}"


# ---- the steps ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Item:
    finding: dict[str, Any]
    steps: list[dict[str, str]] = field(default_factory=list)
    changed: bool = False
    outcome: str | None = None
    """wrong / right / folded: settled by a step (None: still undecided)."""

    def note(self, step: StepName, outcome: str, how: str) -> None:
        self.steps.append({"step": step, "outcome": outcome, "how": how})


def _check(item: _Item, key: str, value: str) -> None:
    checks = dict(item.finding.get("checks") or {})
    if checks.get(key) != value:
        checks[key] = value
        item.finding["checks"] = checks
        item.changed = True


def _settle_right(item: _Item, step: StepName, how: str) -> None:
    was = str(item.finding.get("status"))
    item.finding.update(
        status="same",
        settled={"outcome": "right", "method": f"escalation_{step}", "how": how, "was": was},
        compared=True,
    )
    item.finding.pop("keep_reason", None)
    item.finding.pop("question", None)
    item.changed = True
    item.outcome = "right"


def _equal_parts(item: _Item, ctx: EscalationContext) -> None:
    """A vendor side that prints only EQ marks: its code label is an equal part code worked out
    (`dims.json`'s derived label: the printed overall, less the chain's printed parts, shared
    equally), so its value is that part's, said as code's ("EQ (20")"), and compared like any
    value code read. Any other label (or none) leaves the item as it is."""
    finding = item.finding
    text = " ".join(str(finding.get("vendor") or "").split())
    if not _EQ.search(text) or _claim(text) is not None:
        return
    dims = dict(finding.get("dims") or {}) if isinstance(finding.get("dims"), Mapping) else {}
    label: Mapping[str, Any] | None = _label_of(ctx, dims.get("vendor"))
    if label is None:
        boxed = [
            lab for lab in _labels_in(ctx, finding, "vendor_box", "vendor") if lab.get("derived")
        ]
        label = boxed[0] if len(boxed) == 1 else None
    derived = label.get("derived") if label is not None else None
    value = _decimal(label.get("inches")) if label is not None else None
    if not isinstance(derived, Mapping) or derived.get("rule") != "equal_parts" or not value:
        return
    assert label is not None
    inches = dict(finding.get("inches") or {})
    inches["vendor"] = str(value)
    words = inch_words(value)
    how = (
        f"the EQ marks share {derived.get('from')}'s printed overall equally in "
        f"{derived.get('parts')} parts: {words} each (code)"
    )
    finding.update(inches=inches, vendor=f"EQ ({words})")
    dims["vendor"] = {**_provenance(label), "derived": dict(derived)}
    finding["dims"] = dims
    checks = dict(finding.get("checks") or {})
    checks["verify_vendor"] = {
        "result": "match",
        "route": "drawn" if label.get("route") == "drawn" else "text",
        "read": f"EQ ({words})",
        "reads": [f"{_ref(label)}: {how}"],
        "reason": how,
        "value": words,
        "pending": False,
        "words": f"Code worked out the equal part: {how}",
    }
    finding["checks"] = checks
    item.changed = True
    _check(item, "printed_vendor", "match")
    item.note("equal_parts", "read", f"{_ref(label)}: {how}")
    _printed_compare(item, "equal_parts")


def _label_of(ctx: EscalationContext, side: object) -> dict[str, Any] | None:
    """The set pack's label a finding's `dims` side names (its file, page and id)."""
    if not isinstance(side, Mapping) or not side.get("id"):
        return None
    file = str(side.get("file") or ctx.main_file or "")
    page = int(side.get("page") or 0)
    for label in ctx.labels.get((file, page), []):
        if label.get("id") == side.get("id"):
            return dict(label, page=page, file=file)
    return None


def _claim(text: str) -> str | None:
    checks = import_module("app.review.assistant.finding_checks")
    claim = checks.claim_value(text)
    return claim if claim is not None and checks.to_inches(claim) is not None else None


def _reread(item: _Item, ctx: EscalationContext) -> None:
    """A vendor value of a difference between two drawings that has no spot (no box, no label):
    the read-number tool looks for it on the page (#1746). Read with the same value: its spot and
    code's reading go on the item (the code checks and the pairing proof still decide any red).
    Unread: its reason goes on the item, which stays listed as Not compared, never red and never
    dropped for having no spot (`spot_guard`)."""
    finding = item.finding
    if ctx.reread is None or finding.get("status") not in ("different", "check"):
        return
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if finding.get("vendor_box") or (dims or {}).get("vendor"):
        return
    claim = _claim(" ".join(str(finding.get("vendor") or "").split()))
    page = finding.get("page")
    if claim is None or not isinstance(page, int) or isinstance(page, bool) or page >= 1000:
        return  # a set's page of a separate file (1001...) has no file of its own here
    file = str(ctx.main_file or "")
    numbers = import_module("app.review.assistant.read_number")
    try:
        reading = ctx.reread(numbers.Request(file, page, "vendor", claim=claim))
    except Exception as failed:  # noqa: BLE001 - a tool that cannot run settles nothing
        item.note("read_number", "no_change", f"the read-number tool could not run ({failed})")
        return
    checks = dict(finding.get("checks") or {})
    checks["verify_vendor"] = reading.as_verify(claim)
    finding["checks"] = checks
    item.changed = True
    box = reading.box
    if not reading.matches(claim) or not box or len(box) != 4:
        item.note("read_number", "no_change", f"not read on page {page}: {reading.reason}")
        return
    finding["vendor_box"] = {
        "page": page,
        "x0": float(box[0]),
        "y0": float(box[1]),
        "x1": float(box[2]),
        "y1": float(box[3]),
        "file": file or None,
    }
    if reading.label:
        label = _label_of(ctx, {"id": reading.label, "page": page, "file": file})
        if label is not None:
            finding["dims"] = {**(dims or {}), "vendor": _provenance(label)}
    item.note("read_number", "read", f"read on page {page}: {reading.reason}")


def _labels_step(item: _Item, ctx: EscalationContext) -> None:
    """Code's label in each box that prints the item's value is a code reading of it; a vendor
    value nobody could read becomes the only vendor label in its box. Before that, a vendor side
    of EQ marks takes its equal part's value (`_equal_parts`) and a vendor value with no spot is
    looked for by the read-number tool (`_reread`)."""
    _equal_parts(item, ctx)
    _reread(item, ctx)
    finding = item.finding
    inches = dict(finding.get("inches") or {})
    dims = dict(finding.get("dims") or {}) if isinstance(finding.get("dims"), Mapping) else {}
    found: list[str] = []
    for side, key in (("vendor", "vendor_box"), ("architect", "arch_box")):
        labels = _labels_in(ctx, finding, key, side)
        wanted = _decimal(inches.get(side))
        hits = [lab for lab in labels if _same(_decimal(lab.get("inches")), wanted)]
        if wanted is None and side == "vendor" and finding.get("keep_reason") == "unreadable":
            # Nobody could read the vendor's value: the one vendor label in its box is it.
            readable = [lab for lab in labels if _decimal(lab.get("inches")) is not None]
            if len(readable) == 1:
                hits = readable
                value = _decimal(readable[0].get("inches"))
                inches["vendor"] = str(value)
                finding["vendor"] = str(readable[0].get("text") or "") or (
                    inch_words(value) if value is not None else ""
                )
                finding["inches"] = inches
                item.changed = True
        if len(hits) != 1:
            continue
        label = hits[0]
        if side not in dims:
            dims[side] = _provenance(label)
            finding["dims"] = dims
            item.changed = True
        _check(item, f"printed_{side}", "match")
        found.append(f"{side} {label.get('text')} ({_ref(label)})")
    if not found:
        item.note("labels", "no_change", "no code label prints the item's value in its box")
        return
    item.note("labels", "read", "code read " + "; ".join(found))
    _printed_compare(item, "labels")


def _printed_compare(item: _Item, step: StepName) -> None:
    """A "Your decision" item whose two values code now read at their spots: equal is Right
    (the resolver's printed-against-printed rule); different is a difference code read, which
    still needs a pairing proof to be clouded."""
    finding = item.finding
    if finding.get("status") != "check":
        return
    checks = finding.get("checks") or {}
    if checks.get("printed_vendor") != "match" or checks.get("printed_architect") != "match":
        return
    dims = finding.get("dims") or {}
    vendor, architect = dims.get("vendor") or {}, dims.get("architect") or {}
    if vendor.get("orientation", "h") != architect.get("orientation", "h"):
        return
    inches = finding.get("inches") or {}
    a, v = _decimal(inches.get("architect")), _decimal(inches.get("vendor"))
    if a is None or v is None:
        return
    if _same(a, v):
        _settle_right(
            item,
            step,
            f"code read {inch_words(v)} at the vendor's spot and {inch_words(a)} at the "
            "architect's: the same",
        )
        return
    finding["status"] = "different"
    item.changed = True


def _under_markup_step(item: _Item, ctx: EscalationContext) -> None:
    """The drawing's own text under GV's markup, inside the vendor's box: one size there is the
    vendor's value."""
    finding = item.finding
    if ctx.under is None or finding.get("keep_reason") != "unreadable":
        return
    if _decimal((finding.get("inches") or {}).get("vendor")) is not None:
        return
    where = _box(finding.get("vendor_box"))
    if where is None:
        return
    name, page, box = where
    checks = import_module("app.review.assistant.finding_checks")
    try:
        words = ctx.under(name or ctx.main_file or "", page, box)
    except Exception as failed:  # noqa: BLE001 - a step that cannot run settles nothing
        item.note("under_markup", "no_change", f"could not read under the markup ({failed})")
        return
    # The drawing's own ink only (coloured text may be GV's), joined line by line so "33 1/4\""
    # is one size, not two.
    lines: dict[int, list[Mapping[str, Any]]] = {}
    for word in words:
        if not word.get("coloured"):
            lines.setdefault(round(float(word["box"][1]) / 4), []).append(word)
    sizes: dict[Decimal, str] = {}
    for row in lines.values():
        text = " ".join(str(w.get("text") or "") for w in sorted(row, key=lambda w: w["box"][0]))
        for token in checks.dimension_tokens(text):
            value = checks.to_inches(token)
            if value is not None:
                sizes.setdefault(value, token)
    if len(sizes) != 1:
        item.note(
            "under_markup",
            "no_change",
            "no single size under the markup" if not sizes else "more than one size there",
        )
        return
    value, text = next(iter(sizes.items()))
    inches = dict(finding.get("inches") or {})
    inches["vendor"] = str(value)
    finding.update(inches=inches, vendor=text)
    _check(item, "printed_vendor", "match")
    item.changed = True
    item.note("under_markup", "read", f"code read {text} under GV's markup")
    _printed_compare(item, "under_markup")


def _carrier(
    finding: Mapping[str, Any], others: Sequence[dict[str, Any]], areas: Mapping[int, str]
) -> dict[str, Any] | None:
    """Another copy of the same item (the same area, the same item words, the same vendor value)
    that already carries it: a proven Wrong first, else the newest undecided round."""
    key = round_item_key(finding)
    area = areas.get(int(finding.get("page") or 0))
    value = _decimal((finding.get("inches") or {}).get("vendor"))
    if not key or area is None:
        return None
    copies = [
        other
        for other in others
        if other.get("id") != finding.get("id")
        and round_item_key(other) == key
        and areas.get(int(other.get("page") or 0)) == area
        and (value is None or _same(_decimal((other.get("inches") or {}).get("vendor")), value))
    ]
    proven = [c for c in copies if proof.proof_group(c) == "clouded"]
    if proven:
        return proven[0]
    rounds = [c for c in copies if undecided(c)]
    if not rounds:
        return None
    newest = max(rounds, key=lambda c: (int(c.get("round") or 1), int(c.get("page") or 0)))
    mine = (int(finding.get("round") or 1), int(finding.get("page") or 0))
    later = (int(newest.get("round") or 1), int(newest.get("page") or 0))
    return newest if later > mine else None


def _vendor_sum_carrier(
    finding: Mapping[str, Any], others: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """A vendor string code proved does not add up (the consistency check's), on the same page,
    whose printed overall is this item's vendor value."""
    if finding.get("keep_reason") != "vendor_disagrees" and finding.get("source") != (
        "vendor_arithmetic"
    ):
        return None
    value = _decimal((finding.get("inches") or {}).get("vendor"))
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    for other in others:
        if (
            other.get("id") == finding.get("id")
            or other.get("page") != finding.get("page")
            or other.get("created_by") != "consistency"
            or proof.proof_group(other) != "clouded"
        ):
            continue
        total = to_inches(str(other.get("printed_total") or other.get("vendor") or ""))
        if value is not None and _same(total, value):
            return other
    return None


def _fold(item: _Item, carrier: dict[str, Any], step: StepName, result: Escalation) -> None:
    """The item joins the copy that carries it: listed on it (`also_on`, as the resolver's), so the
    drawing marks this page too; dropped from the answer with the reason."""
    reason = (
        f"Same item as {carrier.get('id')} (page {carrier.get('page')}), which carries it: "
        f"{'a proven Wrong' if proof.proof_group(carrier) == 'clouded' else 'one question'}."
    )
    finding = item.finding
    also = list(carrier.get("also_on") or [])
    if not any(entry.get("finding_id") == finding["id"] for entry in also):
        also.append(
            {
                key: finding.get(source)
                for key, source in (
                    ("finding_id", "id"),
                    ("page", "page"),
                    ("vs_page", "vs_page"),
                    ("round", "round"),
                    ("item", "item"),
                    ("vendor", "vendor"),
                    ("architect", "architect"),
                    ("vendor_box", "vendor_box"),
                )
            }
        )
        carrier["also_on"] = also
        if not any(f is carrier for f in result.changed):
            result.changed.append(carrier)
    result.absorbed[str(finding["id"])] = reason
    item.note(step, "folded", reason)
    item.outcome = "folded"


def _pairs_step(item: _Item, ctx: EscalationContext) -> None:
    """Code paired the item's two labels itself, by drawn position: the pairing is proved."""
    finding = item.finding
    if finding.get("status") != "different" or finding.get("paired_by") == "code":
        return
    dims = finding.get("dims") or {}
    vendor, architect = dims.get("vendor"), dims.get("architect")
    if not isinstance(vendor, Mapping) or not isinstance(architect, Mapping):
        item.note("pairs", "no_change", "the item's values are not both on code's labels")
        return
    paired = ctx.pairs.get(_ref(vendor))
    if paired is None:
        item.note("pairs", "no_change", f"code made no pairing for {_ref(vendor)}")
        return
    if paired != _ref(architect):
        item.note("pairs", "no_change", f"code pairs {_ref(vendor)} with {paired}, not this one")
        return
    finding["paired_by"] = "code"
    item.changed = True
    item.note("pairs", "proved", f"code paired {_ref(vendor)} with {paired} by drawn position")
    if proof.proof_group(judged(finding)) == "clouded":
        item.outcome = "wrong"


def _vendor_drawings(
    ctx: EscalationContext, page: int
) -> list[tuple[int, Sequence[float], Sequence[Mapping[str, Any]]]]:
    """The vendor's drawings of `page`'s area (every round): (page, the drawing's box, the page's
    labels), from the page map. Empty when the page has no area there."""
    file = ctx.main_file or ""
    entries = [
        e
        for e in (ctx.page_map or {}).get("pages") or []
        if isinstance(e, Mapping) and e.get("file") in (None, file)
    ]
    own = next((e for e in entries if e.get("page") == page), None)
    area = None if own is None else (own.get("area") or own.get("section"))
    if not area:
        return []
    found: list[tuple[int, Sequence[float], Sequence[Mapping[str, Any]]]] = []
    for entry in entries:
        if (entry.get("area") or entry.get("section")) != area:
            continue
        number = entry.get("page")
        if not isinstance(number, int) or isinstance(number, bool):
            continue
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if half.get("role") == "vendor" and isinstance(box, Sequence) and len(box) == 4:
                found.append((number, box, ctx.labels.get((file, number), [])))
    return found


def _vendor_search_step(
    item: _Item, ctx: EscalationContext, live: Sequence[dict[str, Any]] = ()
) -> None:
    """An item the AI says the vendor's drawing does not show: code searches the vendor's drawings
    of its area for the architect's printed value and keeps what it found on the item (see the
    module). A millwork item code proves missing (`missing_items.judge`) is red; any other item
    keeps its outcome, and the search says why it is not red."""
    finding = item.finding
    if not classification.missing_on_vendor(finding):
        return
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    architect = (dims or {}).get("architect")
    label = _label_of(ctx, architect) if isinstance(architect, Mapping) else None
    claim = label.get("text") if label is not None else finding.get("architect")
    page = (label or {}).get("page") or finding.get("vs_page") or finding.get("page")
    if not isinstance(page, int) or isinstance(page, bool):
        return
    drawings = _vendor_drawings(ctx, page) or _vendor_drawings(ctx, int(finding.get("page") or 0))
    found = search_area(claim, (label or {}).get("orientation"), drawings)
    if found.found == "unknown":
        item.note("vendor_search", "no_change", "no value or no vendor drawing to search")
        return
    search = found.as_dict()
    carrier = _missing_carrier(finding, live)
    if found.found == "none" and carrier is not None:
        # The same architect label is already a proved missing item: this copy folds into it.
        search["not_red"] = f"the same item as {carrier.get('id')}, which carries it"
    elif found.found == "none":
        verdict = _judge_missing(finding, label, ctx, page, live)
        if verdict.red:
            red = _missing_red(finding, verdict, search)
            if proof.proof_group(red) == "clouded":
                finding.clear()
                finding.update(red)
                item.changed = True
                item.note("vendor_search", "proved", verdict.why)
                return
            search["not_red"] = "code's proof did not pass"
        else:
            search["not_red"] = verdict.why
    finding[VENDOR_SEARCH] = search
    item.changed = True
    item.note("vendor_search", "searched", search_words(finding[VENDOR_SEARCH]) or "")


def _drawing_role(ctx: EscalationContext, label: Mapping[str, Any]) -> tuple[str | None, list[Any]]:
    """The role of the page-map drawing that holds `label` (the smallest one), and its box."""
    file = str(label.get("file") or ctx.main_file or "")
    holding: list[tuple[float, str, list[Any]]] = []
    for entry in (ctx.page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != label.get("page"):
            continue
        if entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if not (isinstance(box, Sequence) and len(box) == 4):
                continue
            if _inside(_centre(label.get("box") or (0, 0, 0, 0)), box, 0.0):
                size = (float(box[2]) - float(box[0])) * (float(box[3]) - float(box[1]))
                holding.append((size, str(half.get("role") or "unknown"), list(box)))
    if not holding:
        return None, []
    _size, role, box = min(holding, key=lambda h: h[0])
    return role, box


def _judge_missing(
    finding: Mapping[str, Any],
    label: Mapping[str, Any] | None,
    ctx: EscalationContext,
    page: int,
    live: Sequence[Mapping[str, Any]],
) -> missing_items.Judged:
    """`missing_items.judge` on an item code found printed nowhere on the vendor's drawings."""
    file = ctx.main_file or ""
    role, holder = _drawing_role(ctx, label) if label is not None else (None, [])

    def inside(raw: Mapping[str, Any]) -> bool:
        return bool(holder) and _inside(_centre(raw.get("box") or (0, 0, 0, 0)), holder, 0.0)

    where = (str((label or {}).get("file") or file), int((label or {}).get("page") or page))
    architect_labels = [
        dict(x, page=where[1], file=where[0])
        for x in ctx.labels.get(where, [])
        if x.get("side") == "architect" and inside(x)
    ]
    architect_phrases = [p for p in ctx.phrases.get(where, []) if inside(p)]
    notes = [
        a
        for a in ctx.annotations
        if a.get("page") == where[1]
        and a.get("kind") == "FreeText"
        and a.get("file") in (None, where[0])
    ]
    entries = [
        e
        for e in (ctx.page_map or {}).get("pages") or []
        if isinstance(e, Mapping) and e.get("file") in (None, file)
    ]
    drawings: list[missing_items.VendorDrawing] = []
    for number, box, labels in _vendor_drawings(ctx, page) or _vendor_drawings(
        ctx, int(finding.get("page") or 0)
    ):
        key = (file, number)
        halves: list[Mapping[str, Any]] = [
            h
            for e in entries
            if e.get("page") == number
            for h in e.get("halves") or []
            if isinstance(h, Mapping)
        ]
        roles = {str(h.get("role")) for h in halves}
        half = next((h for h in halves if h.get("role") == "vendor" and h.get("box") == box), {})
        number_round = half.get("round")
        x0, y0, x1, y1 = (float(v) for v in box)
        drawings.append(
            missing_items.VendorDrawing(
                page=number,
                round=number_round if isinstance(number_round, int) else None,
                box=(x0, y0, x1, y1),
                labels=labels,
                phrases=ctx.phrases.get(key, []),
                text=ctx.page_texts.get(key, "") if roles == {"vendor"} else "",
                picture_share=ctx.pictures.get(key),
                opened=ctx.unopened is None or number not in ctx.unopened,
            )
        )
    return missing_items.judge(
        finding,
        label=label,
        label_role=role,
        architect_labels=architect_labels,
        architect_phrases=architect_phrases,
        notes=notes,
        drawings=drawings,
        others=[f for f in live if f.get("id") != finding.get("id")],
        colour=_colour_reader(ctx),
    )


def _colour_reader(ctx: EscalationContext) -> missing_items.ColourReader | None:
    """Whether an architect label is set in GV's colour, read from the drawing's own text at its
    box (`under_markup`'s words carry their colour); None when it cannot be looked at."""
    under = ctx.under
    if under is None:
        return None

    def coloured(label: Mapping[str, Any]) -> bool | None:
        try:
            x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
            words = under(
                str(label.get("file") or ctx.main_file or ""),
                int(label.get("page") or 0),
                (x0 - 1, y0 - 1, x1 + 1, y1 + 1),
            )
        except Exception:  # noqa: BLE001 - a look that fails proves nothing
            return None
        printed = [w for w in words if re.search(r"\d", str(w.get("text") or ""))]
        if not printed:
            return None
        return any(bool(w.get("coloured")) for w in printed)

    return coloured


def _missing_red(
    finding: Mapping[str, Any], verdict: missing_items.Judged, search: Mapping[str, Any]
) -> dict[str, Any]:
    """The item as a red: a missing millwork item code proved (`missing_items.judge`)."""
    first = verdict.values[0]
    inches = _decimal(first.get("inches"))
    checks = dict(finding.get("checks") or {})
    checks["text"] = "agrees"
    red = dict(finding)
    red.update(
        status="different",
        architect=str(first.get("text") or ""),
        vendor="not shown on the vendor's drawings",
        why=f"{verdict.why}.",
        paired_by="code",
        checks=checks,
        inches={"architect": None if inches is None else str(inches), "vendor": None},
        keep_reason=None,
        check_reason=None,
        text_proof={
            "kind": missing_items.KIND,
            "item": verdict.kind,
            "code": str(first.get("text") or ""),
            "architect": [
                {
                    "page": v.get("page"),
                    "id": v.get("id"),
                    "text": str(v.get("text") or ""),
                    "orientation": v.get("orientation"),
                }
                for v in verdict.values
            ],
            "vendor": [{"page": page, "round": number} for page, number in verdict.rounds],
            "searched": verdict.searched,
            "pairing": "the page map's area pairs the drawings",
        },
    )
    red[VENDOR_SEARCH] = dict(search)
    if first.get("page") != finding.get("page"):
        red["vs_page"] = first.get("page")
    return judged(red)


def _missing_reds(live: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{architect label ref: the proved missing millwork item that rests on it}."""
    reds: dict[str, dict[str, Any]] = {}
    for finding in live:
        proof_ = finding.get("text_proof")
        if (
            isinstance(proof_, Mapping)
            and proof_.get("kind") == missing_items.KIND
            and proof.proof_group(finding) == "clouded"
        ):
            for value in proof_.get("architect") or []:
                reds.setdefault(f"p{value.get('page')}.{value.get('id')}", finding)
    return reds


def _missing_carrier(
    finding: Mapping[str, Any], live: Sequence[dict[str, Any]]
) -> dict[str, Any] | None:
    """The proved missing millwork item that rests on `finding`'s architect label, if any."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    architect = (dims or {}).get("architect")
    if not isinstance(architect, Mapping):
        return None
    carrier = _missing_reds(live).get(f"p{architect.get('page')}.{architect.get('id')}")
    return None if carrier is None or carrier is finding else carrier


def _fold_missing_copies(live: Sequence[dict[str, Any]], result: Escalation) -> None:
    """An item the AI says the vendor does not show that rests on the same architect label as a
    proved missing millwork item is that item: it folds into the red."""
    if not _missing_reds(live):
        return
    for finding in live:
        if str(finding["id"]) in result.absorbed or finding.get("text_proof"):
            continue
        if not undecided(finding) or not classification.missing_on_vendor(finding):
            continue
        carrier = _missing_carrier(finding, live)
        if carrier is None:
            continue
        item = _Item(finding, steps=list((finding.get("escalation") or {}).get("steps") or []))
        _fold(item, carrier, "vendor_search", result)
        finding["escalation"] = {"outcome": "folded", "steps": item.steps}
        result.settled["vendor_search"] = result.settled.get("vendor_search", 0) + 1
        if not any(f is finding for f in result.changed):
            result.changed.append(finding)


def escalate(findings: Sequence[Mapping[str, Any]], ctx: EscalationContext) -> Escalation:
    """The free code steps on every undecided item of `findings` (an answer's live findings),
    the biggest first (see the module). Pure: returns what changed."""
    result = Escalation()
    live = [dict(f) for f in findings]
    # First (Anant, 2026-10-22): an item compared only with the vendor's own earlier round is
    # never Right; code compares it with the architect instead where it can (`round_reference`).
    linked = compare_with_architect(
        live, labels=ctx.labels, page_map=ctx.page_map, pairs=ctx.pairs, main_file=ctx.main_file
    )
    result.round_matches = linked.marked
    result.linked = dict(linked.linked)
    result.absorbed.update(linked.absorbed)
    relinked = {str(f["id"]): judged(f) for f in linked.changed}
    live = [relinked.get(str(f["id"]), f) for f in live if str(f["id"]) not in linked.absorbed]
    result.changed.extend(relinked.values())
    by_id = {str(f["id"]): f for f in live}
    areas = _areas(ctx)
    for finding in by_impact(live):
        if not undecided(finding):
            continue
        result.tried += 1
        item = _Item(by_id[str(finding["id"])])
        for step in FREE_STEPS:
            before = proof.proof_group(judged(item.finding) if item.changed else item.finding)
            if step == "labels":
                _labels_step(item, ctx)
            elif step == "under_markup":
                _under_markup_step(item, ctx)
            elif step == "same_item":
                others = [f for f in live if str(f["id"]) not in result.absorbed]
                carrier = _carrier(item.finding, others, areas)
                if carrier is not None:
                    _fold(item, carrier, step, result)
            elif step == "vendor_sums":
                carrier = _vendor_sum_carrier(item.finding, live)
                if carrier is not None:
                    _fold(item, carrier, step, result)
            elif step == "vendor_search":
                _vendor_search_step(item, ctx, live)
            else:
                _pairs_step(item, ctx)
            if item.changed:
                item.finding.update(judged(item.finding))
            after = proof.proof_group(item.finding)
            if item.outcome is None and before != after and after in ("clouded", "right"):
                item.outcome = "wrong" if after == "clouded" else "right"
            if item.outcome is not None:
                result.settled[step] = result.settled.get(step, 0) + 1
                break
        item.finding["escalation"] = {
            "outcome": item.outcome or "undecided",
            "steps": item.steps,
        }
        result.log[str(item.finding["id"])] = item.steps
        result.changed.append(item.finding)
    _fold_missing_copies(live, result)
    # One copy of each changed finding, its last state.
    last = {str(f["id"]): f for f in result.changed}
    result.changed = list(last.values())
    return result


def _areas(ctx: EscalationContext) -> dict[int, str]:
    resolver = import_module("app.review.assistant.decision_resolver")
    return dict(resolver.page_areas(ctx.page_map, ctx.main_file))


# ---- loading the facts --------------------------------------------------------------------------


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def context_from_pack(
    pack: Path | None,
    main_file: str | None,
    *,
    page_map: Mapping[str, Any] | None = None,
    under: UnderReader | None = None,
    reread: NumberReader | None = None,
    unopened: frozenset[int] | None = None,
) -> EscalationContext:
    """The free steps' facts from a set pack (`dims.json`, `pairs.json`, and for a missing item
    `page_texts.json`, `health.json`, `annotations.json`). `unopened`: the pages the check never
    opened."""
    labels: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    phrases: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    texts: dict[tuple[str, int], str] = {}
    pictures: dict[tuple[str, int], float] = {}
    notes: list[Mapping[str, Any]] = []
    pairs: dict[str, str] = {}
    if pack is not None:
        dims = _read(pack / "dims.json")
        for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                key = (str(entry.get("file")), int(entry["page"]))
                labels[key] = [
                    label for label in entry.get("labels") or [] if isinstance(label, Mapping)
                ]
                phrases[key] = [p for p in entry.get("phrases") or [] if isinstance(p, Mapping)]
        for name, into in (("page_texts.json", texts), ("health.json", pictures)):
            found = _read(pack / name)
            for entry in (found or {}).get("pages") or [] if isinstance(found, Mapping) else []:
                if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                    continue
                key = (str(entry.get("file")), int(entry["page"]))
                if into is texts:
                    texts[key] = str(entry.get("text") or "")
                else:
                    share = _decimal((entry.get("facts") or {}).get("picture_share"))
                    if share is not None:
                        pictures[key] = float(share)
        marks = _read(pack / "annotations.json")
        notes = [a for a in marks if isinstance(a, Mapping)] if isinstance(marks, list) else []
        pairs = code_pairs(_read(pack / "pairs.json"))
        if page_map is None:
            page_map = _read(pack / "page_map.json")
    return EscalationContext(
        main_file=main_file,
        labels=labels,
        page_map=page_map if isinstance(page_map, Mapping) else None,
        pairs=pairs,
        under=under,
        reread=reread,
        phrases=phrases,
        page_texts=texts,
        pictures=pictures,
        annotations=notes,
        unopened=unopened,
    )


def _under_reader(pdf_paths: Mapping[str, Path]) -> UnderReader:
    def read(file: str, page: int, box: tuple[float, float, float, float]) -> list[dict[str, Any]]:
        path = pdf_paths.get(file)
        if path is None or not path.is_file():
            return []
        module = import_module("app.review.assistant.under_markup")
        found = module.read_under_markup(path, page, box)
        return [dict(word) for word in getattr(found, "words", []) or []]

    return read


def escalate_answer(engine: Any) -> Escalation | None:
    """The engine's hook (whole-set work, after the resolver, before the trust report): the free
    steps on the answer's undecided items, each change shown, folded copies dropped with their
    reason. `None` when the answer has no set pack."""
    workspace = engine.tools.workspace
    book = engine.book
    if workspace.set_pack is None or not any(
        undecided(f) or compares_drawings(f) for f in book.live()
    ):
        return None
    plan = import_module("app.review.assistant.full_check_plan")
    page_map = plan.read_page_map(workspace.set_pack, assume=True, main_file=book.main_file)
    numbers = import_module("app.review.assistant.read_number")
    ctx = context_from_pack(
        workspace.set_pack,
        book.main_file,
        page_map=page_map,
        under=_under_reader(book.pdf_paths()),
        reread=numbers.reader_for_pack(workspace.set_pack, workspace.uploads),
        unopened=frozenset(
            int(entry["page"])
            for entry in book.coverage_map()["pages"]
            if entry.get("status") == "not_opened"
        ),
    )
    with engine.finding_lock:
        result = escalate(book.live(), ctx)
        for finding_id, reason in result.absorbed.items():
            if book.absorb(finding_id):
                engine._show_dropped(finding_id, reason)
        for finding in result.changed:
            if finding["id"] in result.absorbed:
                continue
            if book.replace(finding):
                engine._show_finding(finding)
    _log.info("assistant escalation: %s", result.block())
    return result
