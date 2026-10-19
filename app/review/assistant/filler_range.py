"""GV's filler range on the vendor's own fillers, $0, no AI: a filler outside it is Wrong.

GV's settled rule: fillers are at least 1" and at most 2" wide (the client's written rule,
changeable per project). Until now it ran only inside the cabinet run distribution check
(CAB-FILLER-001), which needs the architect's wall-to-wall and a site width typed in; a vendor's
4" filler on a set with neither was never caught. This check needs neither:

- **Which labels are fillers.** Only a printed dimension label whose own words say it is a filler:
  the value and `FILLER(S)`, or a filler tag (`F`, `F1`, `F-2`), with at most a side or "typical"
  word beside it (`3/4" FILLER`, `FILLER 2" TYP`, `4" F1`). The words are the label's own text (a
  note) or the one printed phrase the label sits in (a dimension's value with `FILLER` printed after
  it on the same row; `dims.json` keeps the page's phrases). Any other word (`OVERALL`, `HEIGHT`,
  `PANEL`, `W/`) makes it another dimension, never judged; so is a value with no filler word, and a
  value read from drawn ink (OCR alone never makes a red).
- **The limits** are the settings in force for the set (`rules.md`, `filler_min` / `filler_max`:
  the rulebook's default or the project's own value). A set whose settings do not give both (no
  cabinet in the check's products) is not checked.
- **The architect governs** (DECIDED 2026-10-18). For each vendor filler label, the architect's
  filler labels of the same area (or page; every one when either has no area) are looked at:
  - the architect gives no filler size: GV's range decides: outside it is **Wrong** (red, the
    green words `1" to 2" (GV filler rule)`); inside it nothing is added (a Right needs both
    drawings looked at, `chat_trust.not_compared_why`, and a GV rule has no architect side);
  - the architect gives this filler's size and the vendor prints the same: never red; when the
    size is outside GV's range it is **Not compared** ("the architect specifies 3/4\", outside
    GV's 1\"-2\" filler range"), else nothing is added (the architect comparison says Right);
  - the vendor prints another size than the architect's: the architect comparison decides, not
    this rule (nothing is added).
- **One item once.** A label the answer already has an item for is left to it, except a "same"
  item on a filler the architect sizes outside GV's range: that one becomes the Not-compared note
  (the comparison found them equal, but GV's rule says the size is not GV's).
- **Product scope**: every item is filed under `cabinet`; the finding book records it only when
  cabinets are in the check's scope.

The Wrong items carry `settled: gv_limits` and GV's rule witness (`checks.rule`), the shape
the resolver gives a filler GV's limits settle (`decision_resolver._filler_limits`), so the proof
rules (`proof.code_proved`, `proof.should_be`) treat them alike: no pairing needed, the green words
are GV's limits. The resolver and the escalation leave this check's items alone
(`OWN_ITEM`), and the simple check does not list a filler this check decided a second time.

Verification: `tests/review/assistant/test_filler_range.py` (made-up labels).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ARCHITECT_OUTSIDE",
    "CATEGORY",
    "CREATED_BY",
    "Limits",
    "add_to_answer",
    "covered_labels",
    "findings_from",
    "is_filler_label",
    "limits_from",
    "own_item",
]

CREATED_BY: Final = "filler_range"
CATEGORY: Final = "cabinet"
#: The Not-compared reason of a filler the architect sizes outside GV's range
#: (`classification`: "the architect's size is outside a GV rule").
ARCHITECT_OUTSIDE: Final = "architect_outside_gv_rule"
#: Two printed values closer than this are the same (a sixteenth: the drawings' own step).
TOLERANCE: Final = Fraction(1, 16)
#: Where the limits come from, in the resolver's words (`proof._LIMITS` reads "limits are ... (").
_WHERE: Final = "rules.md, the settings in force for this set"

#: A printed size: `3/4"`, `2"`, `1 1/2"`, `4 in`, `2'-0"`.
_SIZE: Final = re.compile(
    r"\d+\s*'\s*-?\s*\d+(?:\s+\d+/\d+)?\s*\"?|\d+\s+\d+/\d+\s*(?:\"|in\b)?|\d+/\d+\s*(?:\"|in\b)?"
    r"|\d+(?:\.\d+)?\s*(?:\"|in\b)",
    re.IGNORECASE,
)
_FILLER_WORD: Final = re.compile(r"^fillers?$", re.IGNORECASE)
_FILLER_TAG: Final = re.compile(r"^F-?\d{0,2}$")
#: Words that may stand beside a filler's size and the word FILLER without making it another
#: dimension: a side, "typical", "each", "end", "scribe".
_BESIDE: Final = frozenset(
    {"l", "r", "lt", "rt", "left", "right", "typ", "typical", "ea", "each", "end", "scribe"}
)
_PUNCTUATION: Final = re.compile(r"[().,:;=]+")


@dataclass(frozen=True, slots=True)
class Limits:
    """GV's filler range in force for the set (inches)."""

    low: Fraction
    high: Fraction

    def words(self) -> str:
        return f"{_inches(self.low)} to {_inches(self.high)}"

    def dash(self) -> str:
        return f"{_inches(self.low)}-{_inches(self.high)}"

    def outside(self, value: Fraction) -> bool:
        return value < self.low or value > self.high


def _inches(value: Fraction) -> str:
    return str(import_module("app.review.assistant.ledger").format_inches(value))


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def limits_from(settings: Mapping[str, str]) -> Limits | None:
    """GV's filler range from the settings in force (`decision_resolver.settings_in_force`), or
    `None` when either end is not set."""
    to_inches = import_module("app.review.assistant.finding_checks").to_inches
    low_text, high_text = settings.get("filler_min"), settings.get("filler_max")
    if low_text is None or high_text is None:
        return None
    low, high = _fraction(to_inches(low_text)), _fraction(to_inches(high_text))
    if low is None or high is None or low > high:
        return None
    return Limits(low, high)


def is_filler_label(text: object) -> bool:
    """Whether a printed label's own words say it is a filler's size: one size, a filler word or
    tag, and nothing else but a side or "typical" word (see the module)."""
    words = " ".join(str(text or "").split())
    sizes = _SIZE.findall(words)
    if len(sizes) != 1:
        return False
    rest = _PUNCTUATION.sub(" ", _SIZE.sub(" ", words)).split()
    named = [w for w in rest if _FILLER_WORD.match(w) or _FILLER_TAG.match(w)]
    if len(named) != 1:
        return False
    return all(w in named or w.lower() in _BESIDE for w in rest)


# ---- reading the pack ----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Filler:
    file: str
    page: int
    label: Mapping[str, Any]
    value: Fraction
    area: str | None
    words: str
    """The printed words that say it is a filler (the label's own, or the phrase it sits in)."""

    @property
    def ref(self) -> str:
        return f"p{self.page}.{self.label.get('id')}"

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.file, self.page, str(self.label.get("id")))


def _areas(page_map: Mapping[str, Any] | None) -> dict[tuple[str, int], str]:
    """Each page's area as GV typed it (the page map carries it over the area's pages)."""
    out: dict[tuple[str, int], str] = {}
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        area = entry.get("area")
        if isinstance(area, str) and area.strip():
            out[(str(entry.get("file")), entry["page"])] = " ".join(area.split())
    return out


def _centre(box: object) -> tuple[float, float] | None:
    if not isinstance(box, Sequence) or isinstance(box, str) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return None
    return (x0 + x1) / 2, (y0 + y1) / 2


def _inside(point: tuple[float, float], box: object) -> bool:
    if not isinstance(box, Sequence) or isinstance(box, str) or len(box) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(v) for v in box)
    except (TypeError, ValueError):
        return False
    return min(x0, x1) <= point[0] <= max(x0, x1) and min(y0, y1) <= point[1] <= max(y0, y1)


def _words(label: Mapping[str, Any], phrases: Sequence[Mapping[str, Any]]) -> str | None:
    """The words that say what a label is: its own text when it names a filler (a note such as
    `3/4" FILLER`), else the one printed phrase it sits in (a dimension's value with `FILLER`
    printed after it on the same row: `4" FILLER`), else `None`."""
    own = " ".join(str(label.get("text") or "").split())
    if is_filler_label(own):
        return own
    centre = _centre(label.get("box"))
    if centre is None:
        return None
    holding = [
        " ".join(str(phrase.get("text") or "").split())
        for phrase in phrases
        if isinstance(phrase, Mapping) and _inside(centre, phrase.get("box"))
    ]
    if len(holding) == 1 and is_filler_label(holding[0]):
        return holding[0]
    return None


def _fillers(
    dims: Mapping[str, Any], areas: Mapping[tuple[str, int], str], side: str
) -> list[_Filler]:
    out: list[_Filler] = []
    for entry in dims.get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        file, page = str(entry.get("file")), int(entry["page"])
        phrases = [p for p in entry.get("phrases") or [] if isinstance(p, Mapping)]
        for label in entry.get("labels") or []:
            if not isinstance(label, Mapping) or label.get("side") != side or not label.get("id"):
                continue
            words = _words(label, phrases)
            if words is None:
                continue
            value = _fraction(label.get("inches"))
            if value is None or value <= 0:
                continue
            out.append(_Filler(file, page, label, value, areas.get((file, page)), words))
    return out


def _same_place(vendor: _Filler, architect: _Filler) -> bool:
    """The architect's filler belongs to the vendor's drawing: the same page (a combined sheet),
    the same area, or either has no area (then every architect filler counts: it can only keep an
    item from turning red)."""
    if (vendor.file, vendor.page) == (architect.file, architect.page):
        return True
    if vendor.area is None or architect.area is None:
        return True
    return vendor.area.casefold() == architect.area.casefold()


def _provenance(filler: _Filler) -> dict[str, Any]:
    label = filler.label
    return {
        "id": label.get("id"),
        "page": filler.page,
        "file": filler.file,
        "text": label.get("text"),
        "inches": label.get("inches"),
        "route": label.get("route") or "text",
        "side": label.get("side"),
        "box": list(label.get("box") or []),
    }


def _box(filler: _Filler) -> dict[str, Any] | None:
    box = filler.label.get("box")
    if not isinstance(box, Sequence) or len(box) != 4:
        return None
    x0, y0, x1, y1 = (float(v) for v in box)
    return {"page": filler.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": filler.file}


def _item(filler: _Filler) -> str:
    area = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", filler.area or "").strip()
    return f"{area}: filler width" if area else "Filler width"


def _settled(limits: Limits, value: Fraction) -> dict[str, Any]:
    how = (
        f"GV's filler limits are {limits.words()} ({_WHERE}); the vendor draws "
        f"{_inches(value)} fillers."
    )
    return {"outcome": "wrong", "method": "gv_limits", "how": how}


def _ruled(filler: _Filler, limits: Limits) -> dict[str, Any] | None:
    """A vendor filler the architect gives no size for, outside GV's range: Wrong."""
    if str(filler.label.get("route") or "text") != "text":
        return None  # a value read from drawn ink alone is never proof
    value = filler.value
    if not limits.outside(value):
        return None
    vendor = _inches(value)
    return {
        "page": filler.page,
        "item": _item(filler),
        "category": CATEGORY,
        "architect": limits.words(),
        "vendor": vendor,
        "status": "different",
        "why": (
            f"GV's filler range is {limits.words()} (GV rule); the vendor's drawing prints a "
            f"{vendor} filler, and the architect's drawing gives no size for it."
        ),
        "verified": False,
        "source": "rules",
        "vendor_box": _box(filler),
        "dims": {"vendor": _provenance(filler)},
        "created_by": CREATED_BY,
        "ledger_ids": [filler.ref],
        # Laid over the recorded finding (`add_to_answer`): the book keeps only its own fields.
        "settled": _settled(limits, value),
        "checks": {"rule": "agrees"},
    }


def _architect_note(filler: _Filler, architect: _Filler, limits: Limits) -> dict[str, Any]:
    """The architect sizes this filler outside GV's range and the vendor follows the architect:
    never red (the architect governs); a GV note on the Not-compared list."""
    size = _inches(architect.value)
    note = f"the architect specifies {size}, outside GV's {limits.dash()} filler range"
    finding: dict[str, Any] = {
        "page": filler.page,
        "item": _item(filler),
        "category": CATEGORY,
        "architect": architect.words or size,
        "vendor": filler.words or _inches(filler.value),
        "status": "check",
        "why": f"The vendor prints the architect's filler size; {note}.",
        "verified": False,
        "source": "architect",
        "arch_box": _box(architect),
        "vendor_box": _box(filler),
        "dims": {"vendor": _provenance(filler), "architect": _provenance(architect)},
        "created_by": CREATED_BY,
        "ledger_ids": [filler.ref, architect.ref],
        "keep_reason": ARCHITECT_OUTSIDE,
        "gv_note": note,
    }
    if architect.page != filler.page:
        finding["vs_page"] = architect.page
    return finding


def findings_from(
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None,
    limits: Limits | None,
) -> list[dict[str, Any]]:
    """Every vendor filler label of the set judged by GV's range, the architect first (see the
    module): the Wrong ones, then the Not-compared notes."""
    if not isinstance(dims, Mapping) or limits is None:
        return []
    areas = _areas(page_map)
    architects = _fillers(dims, areas, "architect")
    out: list[dict[str, Any]] = []
    for filler in _fillers(dims, areas, "vendor"):
        given = [a for a in architects if _same_place(filler, a)]
        if not given:
            made = _ruled(filler, limits)
            if made is not None:
                out.append(made)
            continue
        matching = [a for a in given if abs(a.value - filler.value) <= TOLERANCE]
        if not matching:
            continue  # the vendor differs from the architect: the architect comparison decides
        if limits.outside(filler.value):
            out.append(_architect_note(filler, matching[0], limits))
    order = {"different": 0, "check": 1}
    return sorted(out, key=lambda f: (order[f["status"]], f["page"]))


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---- the answer's own items ----------------------------------------------------------------------


def own_item(finding: Mapping[str, Any]) -> bool:
    """One of this check's items: the resolver, the escalation and the simple check's own listing
    leave it as it is."""
    return finding.get("created_by") == CREATED_BY


def _label_key(finding: Mapping[str, Any], main_file: str | None) -> tuple[str, int, str] | None:
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    vendor = (dims or {}).get("vendor")
    if not isinstance(vendor, Mapping) or not vendor.get("id"):
        return None
    try:
        page = int(vendor.get("page") or finding.get("page") or 0)
    except (TypeError, ValueError):
        return None
    return (str(vendor.get("file") or main_file or ""), page, str(vendor["id"]))


def covered_labels(
    live: Sequence[Mapping[str, Any]], main_file: str | None = None
) -> set[tuple[str, int, str]]:
    """The vendor labels this check made an item for: `(file, page, label id)`."""
    out: set[tuple[str, int, str]] = set()
    for finding in live:
        if own_item(finding):
            key = _label_key(finding, main_file)
            if key is not None:
                out.add(key)
    return out


def add_to_answer(engine: Any) -> dict[str, int] | None:
    """The engine's hook (a full check, at its finish, $0): each judged filler added through the
    finding book (its checks, trust rule and product scope), then GV's limits laid over it. A label
    the answer already has an item for is left to it, except a "same" item the architect's
    out-of-range size turns into the Not-compared note. Returns how many were added and replaced."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    folder = workspace.set_pack
    if folder is None:
        return None
    try:
        rules = (workspace.skills / "rules.md").read_text(encoding="utf-8")
    except (OSError, AttributeError, TypeError):
        rules = None
    settings = import_module("app.review.assistant.decision_resolver").settings_in_force(rules)
    found = findings_from(
        _read(Path(folder) / "dims.json"),
        _read(Path(folder) / "page_map.json"),
        limits_from(settings),
    )
    if not found:
        return None
    trust = import_module("app.review.assistant.chat_trust")
    book = engine.book
    main_file = getattr(book, "main_file", None)
    counts = {"added": 0, "replaced": 0}
    with engine.finding_lock:
        live = book.live()
        by_label: dict[tuple[str, int, str], Mapping[str, Any]] = {}
        for other in live:
            key = _label_key(other, main_file)
            if key is not None and not own_item(other):
                by_label.setdefault(key, other)
        for finding in found:
            key = _label_key(finding, main_file)
            existing = by_label.get(key) if key is not None else None
            if existing is not None:
                if finding["status"] != "check" or existing.get("status") != "same":
                    continue
                reason = f"Settled by GV Review: {finding['gv_note']}."
                if book.absorb(str(existing["id"])):
                    engine._show_dropped(str(existing["id"]), reason)
                    counts["replaced"] += 1
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is None:
                continue
            extra: dict[str, Any] = {
                k: finding[k] for k in ("settled", "keep_reason", "gv_note") if k in finding
            }
            if finding.get("checks"):
                extra["checks"] = {**(added.get("checks") or {}), **finding["checks"]}
            updated = trust.judged({**added, **extra})
            if book.replace(updated):
                added = updated
            engine._show_finding(added)
            counts["added"] += 1
    return counts
