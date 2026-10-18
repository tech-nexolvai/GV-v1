"""Escalation (#1619): every item code could not compare at first is tried again, cheapest first.

The architect's drawings are the source of truth and nobody at GV is asked to decide anything
(Anant, 2026-10-18; the rules are `classification.py`). Before an item falls to "Not compared",
inside the full check and its fixed price, it climbs a ladder, the biggest items first
(`impact.by_impact`), and stops at the first step that settles it:

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
   - `notes`: the tags each drawing prints (`notes.json`): an architect's tag that the vendor's
     drawings of the same area never print is a missed item code proved by text search (R4: red);
   - `pairs`: code's own pairing of the two labels by drawn position (`pairs.json`): a proof that
     both numbers measure the same thing (`paired_by: code`).
2. **One deeper AI look** per item that is left (the roster, `full_check_roster`): a sharper crop
   through the existing verifier and devil's advocate, under the pairing-proof setting, and only
   with the money left under the fixed price.
3. **Whatever is still open** is Not compared, with its reason and what would settle it
   (`classification`), listed in the separate Not-compared PDF, never on the cloud drawing.

Code decides every outcome, with the same witness rules as the red clouds: a step only adds code's
readings (a label, the text under the markup, code's pairing, a text search) or folds a copy into
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

from app.review.assistant import proof
from app.review.assistant.chat_trust import inch_words, judged, round_item_key
from app.review.assistant.full_check_advocate import code_pairs
from app.review.assistant.impact import by_impact

__all__ = [
    "FREE_STEPS",
    "PRECISION",
    "Escalation",
    "EscalationContext",
    "context_from_pack",
    "escalate",
    "escalate_answer",
    "undecided",
]

_log = logging.getLogger(__name__)

type StepName = Literal["labels", "under_markup", "same_item", "vendor_sums", "notes", "pairs"]
#: The free code steps, cheapest first.
FREE_STEPS: Final[tuple[StepName, ...]] = (
    "labels",
    "under_markup",
    "same_item",
    "vendor_sums",
    "notes",
    "pairs",
)
#: Two printed values this close are the same (the drawings' smallest step, as the resolver).
PRECISION: Final = Decimal("0.0625")
#: How far round a finding's box a label may sit and still be read as the box's value (points).
_SLACK_PT: Final = 8.0
#: A tag code as drawings print them (CT-001, ST-002-CUST, WD-01).
_TAG: Final = re.compile(r"\b[A-Z]{1,4}-\d{1,4}(?:-[A-Z]{2,6})?\b")
_TAG_ITEM: Final = re.compile(r"\b(?:tags?|material|finish)\b", re.IGNORECASE)

#: (file, page, box) -> the drawing's own words under GV's markup in that box (text, colour).
type UnderReader = Callable[[str, int, tuple[float, float, float, float]], list[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class EscalationContext:
    """What the free steps read: the set pack's facts and the drawing's text under the markup."""

    main_file: str | None
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] = field(default_factory=dict)
    page_map: Mapping[str, Any] | None = None
    notes: Mapping[tuple[str, int], Mapping[str, Any]] = field(default_factory=dict)
    pairs: Mapping[str, str] = field(default_factory=dict)
    under: UnderReader | None = None


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

    def block(self) -> dict[str, Any]:
        return {
            "tried": self.tried,
            "settled": dict(self.settled),
            "absorbed": len(self.absorbed),
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
    sides aside), never one the reviewer decided."""
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


def _labels_step(item: _Item, ctx: EscalationContext) -> None:
    """Code's label in each box that prints the item's value is a code reading of it; a vendor
    value nobody could read becomes the only vendor label in its box."""
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


def _tags_on(entry: Mapping[str, Any] | None, role: str) -> set[str]:
    """The tags the drawings of one role print on a page (`notes.json`)."""
    found: set[str] = set()
    for drawing in (entry or {}).get("drawings") or []:
        if isinstance(drawing, Mapping) and drawing.get("role") == role:
            found |= {str(t.get("text")) for t in drawing.get("tags") or [] if t.get("text")}
    return found


def _area_text(ctx: EscalationContext, pages: Sequence[int]) -> str:
    """Every tag and note the vendor's drawings print on `pages`, as one searchable string."""
    file = ctx.main_file or ""
    words: list[str] = []
    for page in pages:
        for drawing in (ctx.notes.get((file, page)) or {}).get("drawings") or []:
            if not isinstance(drawing, Mapping) or drawing.get("role") != "vendor":
                continue
            for entry in [*(drawing.get("tags") or []), *(drawing.get("notes") or [])]:
                if isinstance(entry, Mapping) and entry.get("text"):
                    words.append(str(entry["text"]))
    return _squash(" ".join(words))


def _squash(text: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", text.upper())


def _notes_step(item: _Item, ctx: EscalationContext, areas: Mapping[int, str]) -> None:
    """R4, missed something: an architect's tag (code read it on the architect's drawing) that
    no vendor drawing of the same area prints, by text search, is a Wrong code proved."""
    finding = item.finding
    if not _TAG_ITEM.search(str(finding.get("item") or "")):
        return
    page = int(finding.get("page") or 0)
    where = _box(finding.get("arch_box"))
    arch_page = where[1] if where is not None else int(finding.get("vs_page") or page)
    file = ctx.main_file or ""
    vendor_entry, arch_entry = ctx.notes.get((file, page)), ctx.notes.get((file, arch_page))
    if vendor_entry is None or arch_entry is None:
        item.note("notes", "no_change", "no notes and tags read on these pages")
        return
    architect = _tags_on(arch_entry, "architect")
    named = set(_TAG.findall(str(finding.get("architect") or "")))
    wanted = sorted(architect & named if named else architect)
    if not wanted:
        item.note("notes", "no_change", "code read no architect's tag for this item")
        return
    area = areas.get(page)
    pages = sorted({p for p, a in areas.items() if a == area} if area else {page}) or [page]
    printed = _area_text(ctx, pages)
    if not printed:
        item.note("notes", "no_change", "code read no vendor tag or note in this area")
        return
    # A tag counts as printed when its code (with or without a suffix such as -CUST) is there.
    left_out = [
        tag for tag in wanted if _squash(tag) not in printed and _squash(_base(tag)) not in printed
    ]
    if not left_out:
        item.note("notes", "no_change", "the vendor's drawings print the architect's tags")
        return
    shown = ", ".join(left_out[:4])
    where_pages = ", ".join(str(p) for p in pages)
    finding.update(
        status="different",
        missing_tags=left_out[:4],
        settled={
            "outcome": "wrong",
            "method": "escalation_notes",
            "how": (
                f"code searched every tag and note on the vendor's drawings of this area (page"
                f"{'s' if len(pages) > 1 else ''} {where_pages}) for the architect's {shown}: "
                "not printed"
            ),
            "was": str(item.finding.get("status") or "check"),
        },
    )
    finding.pop("keep_reason", None)
    item.changed = True
    item.outcome = "wrong"
    item.note("notes", "proved", f"the vendor's drawings of this area do not print {shown}")


def _base(tag: str) -> str:
    """A tag without its trailing word suffix (ML-003-CUST: ML-003)."""
    return re.sub(r"-[A-Z]{2,6}$", "", tag)


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


def escalate(findings: Sequence[Mapping[str, Any]], ctx: EscalationContext) -> Escalation:
    """The free code steps on every undecided item of `findings` (an answer's live findings),
    the biggest first (see the module). Pure: returns what changed."""
    result = Escalation()
    live = [dict(f) for f in findings]
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
            elif step == "notes":
                _notes_step(item, ctx, areas)
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
) -> EscalationContext:
    """The free steps' facts from a set pack (`dims.json`, `notes.json`, `pairs.json`)."""
    labels: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    notes: dict[tuple[str, int], Mapping[str, Any]] = {}
    pairs: dict[str, str] = {}
    if pack is not None:
        dims = _read(pack / "dims.json")
        for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                labels[(str(entry.get("file")), int(entry["page"]))] = [
                    label for label in entry.get("labels") or [] if isinstance(label, Mapping)
                ]
        found = _read(pack / "notes.json")
        for entry in (found or {}).get("pages") or [] if isinstance(found, Mapping) else []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                notes[(str(entry.get("file")), int(entry["page"]))] = entry
        pairs = code_pairs(_read(pack / "pairs.json"))
        if page_map is None:
            page_map = _read(pack / "page_map.json")
    return EscalationContext(
        main_file=main_file,
        labels=labels,
        page_map=page_map if isinstance(page_map, Mapping) else None,
        notes=notes,
        pairs=pairs,
        under=under,
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
    if workspace.set_pack is None or not any(undecided(f) for f in book.live()):
        return None
    plan = import_module("app.review.assistant.full_check_plan")
    page_map = plan.read_page_map(workspace.set_pack, assume=True, main_file=book.main_file)
    ctx = context_from_pack(
        workspace.set_pack,
        book.main_file,
        page_map=page_map,
        under=_under_reader(book.pdf_paths()),
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
