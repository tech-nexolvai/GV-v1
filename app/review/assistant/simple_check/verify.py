"""Code verifies the compare call's answer: the simple full check's red / Right / Not compared.

The simple full check (CONTRACT-SIMPLE, 2026-10-19; Anant: "AI decides, code verifies") asks one
model call per vendor drawing to pair every vendor dimension label code numbered with the architect
label that measures the same thing (`schema.CompareResult`). The model's verdict is advice. This
module is pure code: it reads the set pack's own facts (`dims.json`, `page_map.json`, `pairs.json`,
`page_texts.json`) and decides each item, with its reason, in the finding shape the existing
outputs read (the cloud drawing, the package, the Not-compared PDF and workbook, the tracker).

**A pair is a red candidate** only when ALL of these hold (each one a named `Check`):

- the model says `different`, both labels exist and code read their values (`dims.json`: printed
  text, or the vendor's drawn ink read by two OCR engines), and they differ by more than 1/16";
- `roles`: the vendor label is on a vendor drawing, the architect label on the architect's (a brand
  prototype is reference only, R1);
- `area`: the architect label is on one of the work item's architect drawings, in the same area of
  the page map; `view`: the same view type where the work list knows both;
- `axis`: both run the same way (a width against a width);
- `round`: the vendor label is on the newest round of the area, or the newest round prints the same
  value the same way (the difference is still on the vendor's current drawing);
- `units`: a dual label's millimetres agree with its inches; `slip`: the difference is not a unit
  slip (10x, 12x) or one drawn digit easy to misread;
- `gv_ink`: the vendor value's OWN ink is not GV's markup. This replaces the old GV-note box check,
  which blocked a label that merely sat inside a GV note's box: it stopped four true items and no
  false one (PIPELINE-AUDIT.md). A label code read from the vendor's own ink (the text and drawn
  routes: every colour on an ordinary upload, black or grey on GV's combined review sheets, never
  GV's markup) is the vendor's, whatever GV drew over it; any other label's own spot is looked at
  (`run.py` measures it on the markup-free copy);
- `trade`: no end runs to the ceiling, a soffit or bulkhead, a door or another trade's work;
- `ends`: the model's "from / to" ends name the same two points on both drawings;
- `geometry`: code's drawn extents (`full_check_advocate.same_extent`) do not show different
  points. "Cannot tell" never blocks: it is extra evidence only.

A candidate turns red only when the blind second look agrees (`with_blind`, `blind.py`): it picks
the same architect label, says the two measure the same thing and names the same ends on both
drawings.

**Everything else**, with its reason (`classification`'s reasons, so the Not-compared list words it):

- `different_points` -> Not compared ("the two dimensions measure different points", R8);
- `unsure` -> Not compared ("not settled from the drawings"); with no architect label, a row of the
  Not-compared list with its own reason ("the AI was unsure and no printed value was found"), never
  "architect gives no size";
- `no_architect_value` -> a row of the Not-compared list ("architect gives no size", R7), not a
  finding (a set has hundreds);
- `same` with both values verified equal -> Right; `same` with values that differ -> Not compared,
  never Right; `different` with values code reads as equal -> Right;
- a vendor label code paired by itself (`pairs.json`) -> covered: code's own finding decides it;
- `missing_on_vendor` -> a red candidate only when code finds the architect's value or tag printed
  at the architect box, no vendor sheet of the area prints it in any round (labels, printed phrases,
  vendor pages' text; every sheet must have a readable text layer: `architect_items`' absence rule),
  AND code's own scope gate says it is millwork: the model's `millwork` flag is not trusted alone
  (11 room-context absences passed every other rule when the model said millwork). The item's words,
  or the architect's printed words beside it, must name a product in scope (the product registry's
  words and hints), and no room-context word (ceiling, soffit, door head, artwork, switch, column);
- `dimension_without_label` -> a red candidate only when code reads BOTH values at their boxes
  (`verify_value`: the printed text, then the drawn ink by two OCR engines; the caller hands the
  reader in), both boxes run the same way, the vendor box is on the newest round and the two pages
  are one area; else Not compared ("value could not be read by code");
- any other item (a layout count, a tag, a note, an added item) -> Not compared with its reason. An
  AI item never makes a string red: vendor strings are code's chains (`vendor_consistency`).

Pure code: JSON in, plain values out; no model call, no PDF. What needs the PDF (a vendor value's
ink, `verify_value` at a box) is measured by the caller and handed in.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant.architect_items import MIN_TEXT, tag_codes
from app.review.assistant.classification import PROTOTYPE_REFERENCE, PROTOTYPE_REFERENCE_WORDS
from app.review.assistant.dim_index import parse_ref
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.full_check_advocate import Check, code_pairs, is_slip, same_extent
from app.review.assistant.full_check_plan import read_page_map
from app.review.assistant.product_scope import ProductScope, named_products
from app.review.assistant.read_number import Reading, Request
from app.review.assistant.simple_check.set_pages import SetPages, set_pages

__all__ = [
    "AI_UNSURE",
    "CREATED_BY",
    "DIFFERENT_POINTS",
    "GV_INK_SHARE",
    "NO_SIZE",
    "TOLERANCE",
    "UNREAD",
    "UNSURE",
    "BlindAnswer",
    "Item",
    "Pack",
    "Region",
    "ValueReader",
    "Verification",
    "Verified",
    "end_kind",
    "ends_match",
    "verify",
    "verify_answer",
    "verify_pair",
    "with_blind",
]

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "simple_check"
#: Two values further apart than this differ (a sixteenth: the drawings' own step).
TOLERANCE: Final = Fraction(1, 16)
#: A value found printed on a vendor sheet matches within this (inches).
_FOUND_IN: Final = Fraction(1, 32)
#: A vendor value whose own ink is more than this share in colour is GV's markup.
GV_INK_SHARE: Final = 0.25
#: A page-map drawing smaller than this (square points) is an artefact, not a drawing.
_MIN_DRAWING_PT2: Final = 2500.0
#: A vendor sheet with fewer labels than this has no label layer to prove an absence with.
_MIN_LABELS: Final = 3
#: How far outside a box a label's middle may sit and still be in it (points).
_PAD_PT: Final = 2.0
#: How far from a missing item's box the architect's printed words count as "beside it" (points).
_NEAR_PT: Final = 60.0
#: The most of a not-the-form blind reply kept in its record (#1811).
_MAX_KEPT_REPLY: Final = 4000
#: The finding book's limits for an item name and a why.
_ITEM_CHARS: Final = 150
_WHY_CHARS: Final = 590

type Outcome = Literal["candidate", "red", "right", "not_compared", "listed", "covered"]
type Box = tuple[float, float, float, float]
type Ink = Callable[[Mapping[str, Any]], tuple[int, float] | None]
type ValueReader = Callable[[str, int, Box, str], Mapping[str, Any] | None]
#: `read_number` bound to the set pack (`read_number.NumberReader`): a number code or the AI could
#: not read, read again by independent routes (#1746).
type NumberReader = Callable[[Request], Reading]
"""`verify_value` at a spot: (file, page, box in PDF points, expected value) -> its `as_dict()`
(`result` match / mismatch / cannot_read, `route` text / drawn, `read`, `reason`), or None."""

#: The model said the two run between different points (R8).
DIFFERENT_POINTS: Final = "the two dimensions measure different points"
#: The model was not sure.
UNSURE: Final = "not settled from the drawings"
#: An answer that is unsure and names no architect label: the architect may well print the size,
#: so it is never "architect gives no size" (a $0 app run, 2026-10-19).
AI_UNSURE: Final = "the AI was unsure and no printed value was found"
#: The architect prints no size for it (R7).
NO_SIZE: Final = "architect gives no size"
#: Code could not read a value at its box.
UNREAD: Final = "value could not be read by code"


# ---- where each end lands ------------------------------------------------------------------------

_ROOM: Final = (
    r"ceiling|soffit|bulkhead|box above|\bdoor ?(?:head|frame|jamb|opening|way)|doorway"
    r"|entry door|window|art ?work|\bart\b|picture|mirror|light switch|\bswitch|thermostat"
    r"|column|chandelier|sconce|light fixture|sprinkler|grille|diffuser|\btv\b|television"
    r"|wall ?covering|\bpaint|\btile|by others|by g\.?c\b|\bn\.?i\.?c\b|corner guard|crown"
)
_ROOM_WORDS: Final = re.compile(_ROOM, re.IGNORECASE)
#: An end's kind, from the words the model (or the blind look) writes, in this order: the first
#: that matches wins. `room` is room context or another trade's work. The compare prompt's end words
#: (`schema.END_WORDS`, `top_of_counter`) read as words.
_END_KINDS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (kind, re.compile(pattern, re.IGNORECASE))
    for kind, pattern in (
        ("room", _ROOM),
        (
            "outlet_edge",
            (
                r"(?:edge|top|bottom|side)\s+of\s+(?:the\s+|an?\s+)?(?:\w+\s+)?"
                r"(?:outlet|receptacle|j-?box|junction box|data|plug)"
            ),
        ),
        ("outlet", r"outlet|receptacle|j-?box|junction box|\bdata\b|\bplug|electrical box"),
        ("sink", r"\bsink|faucet|\bbowl\b"),
        ("splash_top", r"(?:top|t\.?o\.?s\.?)\s+of\s+(?:the\s+)?(?:back\s*)?splash|splash top"),
        ("upper_under", r"(?:underside|bottom|u/s)\s+of\s+(?:the\s+)?(?:upper|wall\s*cab)"),
        (
            "counter_under",
            (
                r"(?:underside|bottom|u/s)\s+of\s+(?:the\s+)?(?:counter|countertop|top|stone"
                r"|slab|vanity|solid surface)"
            ),
        ),
        ("upper_top", r"top\s+of\s+(?:the\s+)?(?:upper|wall)\s*cab"),
        ("unit_top", r"top\s+of\s+(?:the\s+)?(?:\w+\s+)?(?:unit|tall|cabinet|tower|base)"),
        (
            "counter_top",
            (
                r"(?:top|t\.?o\.?c\.?)\s+of\s+(?:the\s+)?(?:counter|countertop|stone|slab"
                r"|vanity|top|solid surface)|counter\s*top|\btoc\b"
            ),
        ),
        ("floor", r"floor|\ba\.?f\.?f\b"),
        ("toe", r"toe ?kick|plinth"),
        ("blocking", r"blocking"),
        ("opening", r"opening"),
        (
            "cabinet",
            r"cabinet|\bcab\b|panel|gable|carcass|\bcase\b|filler|\bunit\b|end of (?:the )?run",
        ),
        ("wall", r"\bwall"),
        (
            "appliance",
            r"fridge|refrigerator|\brange\b|cooktop|oven|dishwasher|microwave|appliance|ice machine",
        ),
        ("centre", r"centre|center|\bc/?l\b"),
    )
)
#: Ends a careful reader never confuses: two of these that differ are different points (R8). Any
#: other mismatch (a cabinet edge against a wall, a centre line against an appliance) is a doubt.
_STRICT: Final = frozenset(
    {
        "floor",
        "counter_top",
        "counter_under",
        "splash_top",
        "upper_under",
        "upper_top",
        "unit_top",
        "wall",
        "outlet",
        "outlet_edge",
        "sink",
    }
)
#: A centre line named alone may be the centre of any of these.
_CENTRED: Final = frozenset({"centre", "outlet", "sink", "appliance"})
_SIDE: Final = re.compile(r"\b(left|right)\b", re.IGNORECASE)
_NOT_WORD: Final = re.compile(r"[^a-z0-9]+")

type Ends = Literal["same", "different", "unclear", "room"]
type End = tuple[str, str | None]


def end_kind(text: object) -> End:
    """(kind, `left` / `right` or None) of one end as words name it: kind `other` when no word
    code knows is there, `none` when nothing is written."""
    words = " ".join(str(text or "").replace("_", " ").split())
    if not words:
        return "none", None
    side = _SIDE.search(words)
    named = side.group(1).lower() if side else None
    for kind, pattern in _END_KINDS:
        if pattern.search(words):
            return kind, named
    return "other", named


def _plain(text: object) -> str:
    return " ".join(_NOT_WORD.sub(" ", str(text or "").lower().replace("_", " ")).split())


def _end_same(a: End, b: End, a_words: str, b_words: str) -> str:
    """`same`, `different` or `unclear` for one end on each side."""
    (kind_a, side_a), (kind_b, side_b) = a, b
    if side_a and side_b and side_a != side_b:
        return "different"
    if "other" in (kind_a, kind_b):
        # Words code does not know: the same only when written the same.
        return "same" if a_words == b_words else "unclear"
    if kind_a == kind_b:
        return "same"
    if {kind_a, kind_b} <= _CENTRED and "centre" in (kind_a, kind_b):
        return "same"
    if kind_a in _STRICT and kind_b in _STRICT:
        return "different"
    return "unclear"


def ends_match(
    vendor_from: object, vendor_to: object, architect_from: object, architect_to: object
) -> tuple[Ends, str]:
    """Whether two dimensions' ends name the same two points (in either order), and why: `room`
    when an end is room context or another trade's work, `different` when they name points a reader
    never confuses (the top against the underside of the counter, the left wall against the right
    one), `unclear` when code cannot tell from the words (or an end is not named)."""
    raw = [vendor_from, vendor_to, architect_from, architect_to]
    kinds = [end_kind(text) for text in raw]
    plain = [_plain(text) for text in raw]
    words = f"{plain[0]} to {plain[1]}"
    theirs = f"{plain[2]} to {plain[3]}"
    for text, (kind, _side) in zip(plain, kinds, strict=True):
        if kind == "room":
            return "room", f"an end is room context or another trade's work ({text})"
    if any(kind == "none" for kind, _side in kinds):
        return "unclear", "an end is not named"
    unclear = False
    for a, b in ((2, 3), (3, 2)):
        one = _end_same(kinds[0], kinds[a], plain[0], plain[a])
        two = _end_same(kinds[1], kinds[b], plain[1], plain[b])
        if one == "same" and two == "same":
            return "same", f"both run {words}"
        if "different" not in (one, two):
            unclear = True
    if unclear:
        return "unclear", f"code cannot tell that {words} and {theirs} are the same points"
    return "different", f"the vendor's runs {words}, the architect's {theirs}"


# ---- the set pack's facts ------------------------------------------------------------------------


def _box(raw: object) -> Box | None:
    if isinstance(raw, Mapping):
        raw = [raw.get(k) for k in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    if not (x1 > x0 and y1 > y0):
        return None
    return x0, y0, x1, y1


def _centre_in(box: Box | None, area: Box | None, pad: float = _PAD_PT) -> bool:
    if box is None or area is None:
        return False
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] - pad <= cx <= area[2] + pad and area[1] - pad <= cy <= area[3] + pad


def _size(box: Box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ArithmeticError):
        return None


def _inches_of(text: object) -> Fraction | None:
    if not isinstance(text, str) or not text.strip():
        return None
    value = to_inches(text)
    return None if value is None else Fraction(value)


@dataclass(frozen=True, slots=True)
class _Half:
    role: str
    round: int | None
    box: Box


@dataclass(frozen=True)
class Pack:
    """The set pack's facts verification reads: each page's labels and printed phrases, the page
    map, code's own pairs and each page's whole text (every colour)."""

    main_file: str
    entries: Mapping[tuple[str, int], Mapping[str, Any]]
    labels: Mapping[tuple[str, int], tuple[dict[str, Any], ...]]
    phrases: Mapping[tuple[str, int], tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    texts: Mapping[tuple[str, int], str] = field(default_factory=dict)
    pairs: Mapping[str, str] = field(default_factory=dict)
    """{vendor label ref: the architect label ref code paired it with} (`pairs.json`)."""
    pages: SetPages | None = None
    """The set's page numbering (#1722): every fact above is keyed by `(file, set page)`; a
    separate file's labels also carry their own `file_page`."""

    rounds: Mapping[tuple[str, int], str] = field(default_factory=dict)
    """{(file, set page): group} for the vendor drawings of separate files that are rounds of one
    drawing (#1728, `WorkList.round_groups`): their area for the round checks."""

    def with_rounds(self, groups: Iterable[Sequence[Any]]) -> Pack:
        """The pack with the work list's round groups (`WorkList.round_groups`)."""
        found = {(str(g[0]), int(g[2])): str(g[3]) for g in groups if len(g) >= 4}
        return replace(self, rounds=found) if found else self

    def own_page(self, file: str, page: int) -> int:
        """The file's own page of set page `page` (what a PDF is read at)."""
        return page if self.pages is None else self.pages.own_page(file, page)

    @classmethod
    def from_documents(
        cls,
        *,
        dims: Mapping[str, Any] | None,
        page_map: Mapping[str, Any] | None,
        pairs: object = None,
        page_texts: Mapping[str, Any] | None = None,
        main_file: str | None = None,
    ) -> Pack:
        files = [str(f) for f in (dims or {}).get("files") or (page_map or {}).get("files") or []]
        main = main_file or (files[0] if files else "")
        numbering = set_pages(page_map, main)

        def key_of(entry: Mapping[str, Any]) -> tuple[str, int]:
            name = str(entry.get("file") or main)
            return name, numbering.to_set(name, int(entry["page"]))

        entries: dict[tuple[str, int], Mapping[str, Any]] = {}
        for entry in (page_map or {}).get("pages") or []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                entries[key_of(entry)] = entry
        labels: dict[tuple[str, int], tuple[dict[str, Any], ...]] = {}
        phrases: dict[tuple[str, int], tuple[Mapping[str, Any], ...]] = {}
        for entry in (dims or {}).get("pages") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                continue
            key = key_of(entry)
            own = {"file_page": int(entry["page"])} if key[1] != int(entry["page"]) else {}
            labels[key] = tuple(
                {**label, "file": key[0], "page": key[1], **own}
                for label in entry.get("labels") or []
                if isinstance(label, Mapping)
            )
            phrases[key] = tuple(p for p in entry.get("phrases") or [] if isinstance(p, Mapping))
        texts: dict[tuple[str, int], str] = {}
        for entry in (page_texts or {}).get("pages") or []:
            if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                texts[key_of(entry)] = str(entry.get("text") or "")
        return cls(main, entries, labels, phrases, texts, code_pairs(pairs), numbering)

    @classmethod
    def from_folder(cls, folder: Path, main_file: str | None = None) -> Pack:
        """The pack's JSON files (a missing one reads as empty); the page map with the plan
        check's answers and code's assumptions laid over it, as the work list reads it."""

        def read(name: str) -> Any:
            try:
                return json.loads((Path(folder) / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

        page_map = read_page_map(Path(folder), assume=True, main_file=main_file)
        return cls.from_documents(
            dims=read("dims.json"),
            page_map=page_map if page_map is not None else read("page_map.json"),
            pairs=read("pairs.json"),
            page_texts=read("page_texts.json"),
            main_file=main_file,
        )

    # -- pages and drawings --

    def on_page(self, file: str, page: int) -> tuple[dict[str, Any], ...]:
        return self.labels.get((file, page), ())

    def label(self, ref: object, file: str, page: int) -> dict[str, Any] | None:
        """The label `ref` names (`D17` on `page`, or `p23.D17`), or None."""
        parsed = parse_ref(ref)
        if parsed is None:
            return None
        named, number = parsed
        for label in self.on_page(file, named or page):
            if label.get("id") == f"D{number}":
                return label
        return None

    def halves(self, file: str, page: int) -> list[_Half]:
        entry = self.entries.get((file, page)) or {}
        out: list[_Half] = []
        for half in entry.get("halves") or []:
            box = _box(half.get("box")) if isinstance(half, Mapping) else None
            if box is None or _size(box) < _MIN_DRAWING_PT2:
                continue
            number = half.get("round")
            out.append(
                _Half(
                    str(half.get("role") or "unknown"),
                    int(number) if isinstance(number, int) and number > 0 else None,
                    box,
                )
            )
        return out

    def half_at(self, file: str, page: int, box: Box | None) -> _Half | None:
        holding = [h for h in self.halves(file, page) if _centre_in(box, h.box, 0.0)]
        return min(holding, key=lambda h: _size(h.box)) if holding else None

    def half_of(self, label: Mapping[str, Any]) -> _Half | None:
        file = str(label.get("file") or self.main_file)
        return self.half_at(file, int(label.get("page") or 0), _box(label.get("box")))

    def area_key(self, file: str, page: int) -> tuple[str, int] | None:
        """The page's area: the page that names it (the page map's `area_from_page` chain); a
        separate file's vendor drawing, its round group (#1728)."""
        group = self.rounds.get((file, page))
        if group is not None:
            return (f"rounds of {group}", 0)
        entry = self.entries.get((file, page))
        if entry is None or entry.get("skip") or not str(entry.get("area") or "").strip():
            return None
        start = entry.get("area_from_page")
        return (file, int(start) if isinstance(start, int) else page)

    def area_name(self, file: str, page: int) -> str:
        entry = self.entries.get((file, page)) or {}
        return re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", str(entry.get("area") or "")).strip()

    def area_pages(self, key: tuple[str, int]) -> list[tuple[str, int]]:
        return sorted(k for k in self.entries if self.area_key(*k) == key)

    def vendor_drawings(self, key: tuple[str, int]) -> list[tuple[str, int, _Half]]:
        return [
            (file, page, half)
            for file, page in self.area_pages(key)
            for half in self.halves(file, page)
            if half.role == "vendor"
        ]

    def newest_round(self, key: tuple[str, int] | None) -> int | None:
        if key is None:
            return None
        rounds = [h.round for _f, _p, h in self.vendor_drawings(key) if h.round is not None]
        return max(rounds) if rounds else None

    def newer_same(self, label: Mapping[str, Any], newest: int) -> str | None:
        """A label of the area's newest vendor round printing the same value the same way."""
        file, page = str(label.get("file") or self.main_file), int(label.get("page") or 0)
        key = self.area_key(file, page)
        want = _fraction(label.get("inches"))
        if key is None or want is None:
            return None
        for other_file, other_page, half in self.vendor_drawings(key):
            if half.round != newest:
                continue
            for other in self.on_page(other_file, other_page):
                if (
                    other.get("side") == "vendor"
                    and other.get("orientation") == label.get("orientation")
                    and _fraction(other.get("inches")) == want
                    and _centre_in(_box(other.get("box")), half.box, 0.0)
                ):
                    return f"p{other_page}.{other.get('id')}"
        return None


# ---- the work item --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Region:
    """A drawing on a page: its file, page (from 1), box in PDF points (None: the page)."""

    file: str
    page: int
    box: Box | None = None
    round: int | None = None
    view: str | None = None
    file_page: int | None = None
    """Its page in its own file when `page` is a separate file's set page (#1722)."""
    sheet: str | None = None
    """Its file's kind (`schema.Drawing.sheet`: `combined` / `ordinary`), for the prompts' colour
    words; `None`: not known."""


_VIEWS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("section", r"\bsection\b|\bdetail\b"),
        ("plan", r"\bplan\b"),
        ("elevation", r"\belevation\b|\bview\b"),
    )
)


def _view_of(titles: Iterable[object]) -> str | None:
    """A drawing's view type from its printed titles, when exactly one is named."""
    named = {
        name
        for title in titles
        for name, pattern in _VIEWS
        if isinstance(title, str) and pattern.search(title)
    }
    return next(iter(named)) if len(named) == 1 else None


@dataclass(frozen=True, slots=True)
class Item:
    """What verification needs of one work item (`schema.WorkItem`, or derived from the pack)."""

    id: str
    vendor: Region
    architect: tuple[Region, ...] = ()
    vendor_labels: tuple[str, ...] = ()
    """The vendor label refs code numbered on the drawing (`p23.D17`); empty: worked out here."""
    architect_labels: tuple[str, ...] = ()
    """The label refs of its architect drawings (empty: each drawing's box decides)."""
    products: tuple[str, ...] = ()
    """The products the drawings' printed words name (the work list's)."""
    prototype_reference: bool = False
    """R1 (Anant, 2026-10-20): the area has no architect drawing, so `architect` is its brand
    prototype, the reference (`schema.WorkItem.prototype_reference`)."""

    @property
    def category(self) -> str:
        return self.products[0] if len(self.products) == 1 else "other"

    @classmethod
    def of(cls, work: Any) -> Item:
        """An `Item` from the work list's `schema.WorkItem` (read by attribute)."""
        vendor = work.vendor
        return cls(
            id=str(work.id),
            vendor=Region(
                str(vendor.file),
                int(vendor.page),
                _box(getattr(vendor, "crop_box", None)) or _box(vendor.box),
                vendor.round,
                _view_of(getattr(vendor, "titles", ()) or ()),
                sheet=getattr(vendor, "sheet", None),
            ),
            architect=tuple(
                Region(
                    str(d.file),
                    int(d.page),
                    _box(getattr(d, "crop_box", None)) or _box(d.box),
                    None,
                    _view_of(getattr(d, "titles", ()) or ()),
                    getattr(d, "file_page", None),
                    getattr(d, "sheet", None),
                )
                for d in work.architect
            ),
            vendor_labels=tuple(label.ref for label in vendor.labels),
            architect_labels=tuple(
                label.ref for drawing in work.architect for label in drawing.labels
            ),
            products=tuple(getattr(work, "products", ()) or ()),
            prototype_reference=bool(getattr(work, "prototype_reference", False)),
        )

    @classmethod
    def derived(cls, work_id: str, pack: Pack) -> Item | None:
        """The work item an id names, worked out from the pack when the work list is not at hand
        (the $0 harnesses): `p23.v1` is page 23's vendor drawing of round 1, `p11.v1.2` the second
        such drawing (top to bottom, left to right); its architect drawings are the architect
        drawings of its area."""
        found = re.fullmatch(r"p(\d+)\.v(\d+)(?:\.(\d+))?", work_id.strip())
        if found is None:
            return None
        page, number, nth = int(found.group(1)), int(found.group(2)), int(found.group(3) or 1)
        file = pack.main_file
        vendor = sorted(
            (h for h in pack.halves(file, page) if h.role == "vendor"),
            key=lambda h: (h.round or 0, h.box[1], h.box[0]),
        )
        same = [h for h in vendor if (h.round or 0) == number]
        if len(same) < nth:
            return None
        half = same[nth - 1]
        key = pack.area_key(file, page)
        architect = tuple(
            Region(f, p, h.box)
            for f, p in (pack.area_pages(key) if key is not None else [(file, page)])
            for h in pack.halves(f, p)
            if h.role == "architect"
        )
        return cls(work_id, Region(file, page, half.box, half.round), architect)


def _ref(label: Mapping[str, Any]) -> str:
    return f"p{label.get('page')}.{label.get('id')}"


def vendor_labels(item: Item, pack: Pack) -> list[dict[str, Any]]:
    """The vendor labels of the item's drawing: those it names, else the vendor labels whose
    middle is inside its drawing's box (each in the smallest vendor drawing holding it)."""
    file, page = item.vendor.file, item.vendor.page
    if item.vendor_labels:
        found = [pack.label(ref, file, page) for ref in item.vendor_labels]
        return [label for label in found if label is not None]
    out = []
    for label in pack.on_page(file, page):
        if label.get("side") != "vendor":
            continue
        half = pack.half_of(label)
        if item.vendor.box is not None and (half is None or half.box != item.vendor.box):
            continue
        out.append(label)
    return out


# ---- the answer -----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BlindAnswer:
    """The blind second look's answer (`blind.py`), normalised.

    A pair: `pick` is the architect label it chose (`p23.D4`, None: none), the ends it names on
    each side, `verdict` `same_thing` / `different_points` / `unsure`. Two values without labels:
    the same, with no pick. An absence: `pick` is the vendor label it found measuring the same thing
    (None: none), `verdict` `absent` / `shown` / `unsure`, and whether the item is millwork."""

    pick: str | None
    verdict: str
    vendor_from: str = ""
    vendor_to: str = ""
    architect_from: str = ""
    architect_to: str = ""
    vendor_ink_black: str = "unsure"
    millwork: bool | None = None
    note: str = ""
    model: str = ""

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> BlindAnswer:
        """A saved answer (`as_dict`) read back (a replay)."""
        vendor = [*list(raw.get("vendor_ends") or []), "", ""]
        architect = [*list(raw.get("architect_ends") or []), "", ""]
        pick = str(raw.get("pick") or "none")
        millwork = raw.get("millwork")
        return cls(
            pick=None if pick == "none" else pick,
            verdict=str(raw.get("verdict") or "unsure"),
            vendor_from=str(vendor[0]),
            vendor_to=str(vendor[1]),
            architect_from=str(architect[0]),
            architect_to=str(architect[1]),
            vendor_ink_black=str(raw.get("vendor_ink_black") or "unsure"),
            millwork=millwork if isinstance(millwork, bool) else None,
            note=str(raw.get("note") or ""),
            model=str(raw.get("model") or ""),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "pick": self.pick or "none",
            "verdict": self.verdict,
            "vendor_ends": [self.vendor_from, self.vendor_to],
            "architect_ends": [self.architect_from, self.architect_to],
            "vendor_ink_black": self.vendor_ink_black,
            "millwork": self.millwork,
            "note": self.note,
            "model": self.model,
        }


#: An outcome as the eval harness and the work-item block say it.
_VERDICT_WORDS: Final[Mapping[str, str]] = {
    "candidate": "wrong",
    "red": "wrong",
    "right": "right",
    "not_compared": "not_compared",
    "listed": "not_compared",
    "covered": "covered",
}


@dataclass(frozen=True, slots=True)
class Verified:
    """One item of the answer, decided by code."""

    key: str
    """`<work item>:<vendor label>` for a pair, `<work item>:item<n>` for another item."""
    kind: str
    """`pair`, or the other item's kind (`missing_on_vendor`, `layout_count`, ...)."""
    outcome: Outcome
    reason: str = ""
    """Why it is not red, in plain words; empty for a red or Right item."""
    reason_code: str | None = None
    """The Not-compared reason (`classification.REASONS`)."""
    checks: tuple[Check, ...] = ()
    vendor: Mapping[str, Any] | None = None
    architect: Mapping[str, Any] | None = None
    finding: Mapping[str, Any] = field(default_factory=dict)
    """The chat finding's fields (no id), for the finding book (`run.py`); empty: no finding."""
    blind: str | None = None
    """What the blind look checks for a candidate: `pair`, `values` or `absence`."""
    ends: Mapping[str, str] = field(default_factory=dict)
    reads: Mapping[str, Any] = field(default_factory=dict)
    """`verify_value`'s reading of each side, for an item without labels."""
    about: Mapping[str, Any] = field(default_factory=dict)
    """A listed item's name and product (`page`, `item`, `category`): what files a Not-compared row
    that is not a finding under the product scope (`product_scope.scoped_gaps`)."""
    resolve: tuple[Mapping[str, Any], ...] = ()
    """The resolve loop's rounds on this item (`resolve.py`): each round's failed check, the
    evidence the AI named (label ids), code's verdict on it. Empty when the loop did not run."""

    @property
    def verdict(self) -> str:
        return _VERDICT_WORDS[self.outcome]

    @property
    def needs_blind(self) -> bool:
        return self.outcome == "candidate"

    def record(self) -> dict[str, Any]:
        """What the finding keeps of code's verification (its `simple` field)."""
        return {
            "key": self.key,
            "kind": self.kind,
            "outcome": self.outcome,
            "reason": self.reason,
            "checks": [[c.name, c.outcome, c.why] for c in self.checks],
            "ends": dict(self.ends),
            **({"resolve": [dict(r) for r in self.resolve]} if self.resolve else {}),
        }

    def row(self) -> dict[str, Any]:
        """One line for the eval harness: the verdict, whether it waits for the blind look."""
        return {
            "key": self.key,
            "kind": self.kind,
            "verdict": self.verdict,
            "outcome": self.outcome,
            "needs_blind": self.needs_blind,
            "reason": self.reason,
            "reason_code": self.reason_code,
            "vendor_label": _ref(self.vendor) if self.vendor is not None else None,
            "architect_label": _ref(self.architect) if self.architect is not None else None,
            "checks": [[c.name, c.outcome, c.why] for c in self.checks],
        }


@dataclass(slots=True)
class Verification:
    """One work item's answer, verified."""

    item: Item
    items: list[Verified] = field(default_factory=list)
    missing_labels: list[str] = field(default_factory=list)
    """Vendor labels of the drawing the answer neither paired nor named unaccounted (code's
    accounting: the summary names the gap)."""
    unaccounted: list[str] = field(default_factory=list)
    unknown_labels: list[str] = field(default_factory=list)
    """Label refs the answer names that are not vendor labels of this drawing."""

    def counts(self) -> dict[str, int]:
        found = Counter(v.outcome for v in self.items)
        return {
            name: found.get(name, 0)
            for name in ("candidate", "red", "right", "not_compared", "listed", "covered")
        }


# ---- checks ---------------------------------------------------------------------------------------


def _differ(vendor: Fraction, architect: Fraction) -> bool:
    return abs(vendor - architect) > TOLERANCE


def _label_words(label: Mapping[str, Any]) -> str:
    return str(label.get("text") or label.get("inches") or "")


def _round_check(pack: Pack, item: Item, label: Mapping[str, Any] | None = None) -> Check:
    """The vendor drawing is the newest round of its area, or (for a label) the newest round
    prints the same value the same way."""
    own = item.vendor.round
    newest = pack.newest_round(pack.area_key(item.vendor.file, item.vendor.page))
    if own is None or newest is None or own >= newest:
        return Check("round", "pass")
    same = pack.newer_same(label, newest) if label is not None else None
    if same is not None:
        return Check("round", "pass", f"round {newest} prints the same value ({same})")
    return Check("round", "fail", f"round {own}: round {newest} is the vendor's current drawing")


def _area_check(
    item: Item, pack: Pack, a_file: str, a_page: int, a_box: Box | None, a_ref: str | None = None
) -> Check:
    """The architect value is on one of the work item's architect drawings (the work list paired
    them by the page map's area, code's own pairing); without a work list, both pages are in one
    area of the page map."""
    if item.architect:
        regions = [r for r in item.architect if r.file == a_file and r.page == a_page]
        if not regions:
            return Check(
                "area", "fail", f"page {a_page} is not one of this item's architect drawings"
            )
        if a_ref is not None and item.architect_labels:
            if a_ref in item.architect_labels:
                return Check("area", "pass")
            return Check("area", "fail", f"{a_ref} is not on this item's architect drawings")
        boxed = [r for r in regions if r.box is not None]
        if boxed and not any(_centre_in(a_box, r.box) for r in boxed):
            return Check("area", "fail", "the architect value is outside this item's drawings")
        return Check("area", "pass")
    v_key = pack.area_key(item.vendor.file, item.vendor.page)
    a_key = pack.area_key(a_file, a_page)
    if v_key is None or a_key is None:
        return Check("area", "fail", "the page map does not place both pages in one area")
    if v_key != a_key:
        return Check("area", "fail", f"pages {item.vendor.page} and {a_page} are other areas")
    return Check("area", "pass")


def _view_check(item: Item, a_file: str, a_page: int) -> Check:
    """The view types, as information only. A drawing's printed titles do not say which view a
    dimension is in: on Set 3 a vendor drawing titled only "SECTION AT" and an architect drawing
    titled "ELEVATION" both hold the plan whose top width and depth are compared (s3-48, s3-49),
    so a title-based block stops true items, and a title-based pass is luck. What two dimensions
    measure is checked by their ends (`ends`) and the blind look's own ends instead."""
    theirs = next(
        (r.view for r in item.architect if r.file == a_file and r.page == a_page and r.view), None
    )
    ours = item.vendor.view
    if ours and theirs and ours != theirs:
        return Check("view", "pass", f"titled {ours} and {theirs}: the ends decide")
    return Check("view", "pass")


def _roles_check(
    vendor: Mapping[str, Any], architect: Mapping[str, Any], prototype: bool = False
) -> Check:
    """A vendor label against an architect label; against the brand prototype's only for an item
    whose area has no architect elevation (`prototype`: the prototype is the reference, R1)."""
    if vendor.get("side") != "vendor":
        return Check("roles", "fail", f"the vendor value is on the {vendor.get('side')}'s drawing")
    side = str(architect.get("side") or "unknown")
    if side == "brand" and prototype:
        return Check("roles", "pass", PROTOTYPE_REFERENCE_WORDS)
    if side == "brand":
        return Check("roles", "fail", "the reference is the brand prototype (reference only)")
    if side != "architect":
        return Check("roles", "fail", f"the reference is on the {side}'s drawing")
    return Check("roles", "pass")


#: Label routes whose characters code read from the drawing's own ink (`dim_labels`): on an
#: ordinary upload every colour, on GV's combined review sheets only black or grey (the colour rule,
#: 2026-10-19); GV's markup is never read as a label either way.
_VENDOR_INK_ROUTES: Final = frozenset({"text", "drawn"})


def _ink_check(label: Mapping[str, Any], ink: Ink | None) -> Check:
    """Whether the vendor value is GV's markup: only by the label's OWN ink. A label code read
    from the vendor's own ink (`_VENDOR_INK_ROUTES`) is the vendor's, whatever GV drew over or
    around it (a yellow note covering it, a note box round it: the old box check stopped four true
    items). Any other label's own spot is looked at (`ink`, its coloured share)."""
    if label.get("route") in _VENDOR_INK_ROUTES:
        return Check("gv_ink", "pass", "read from the vendor's own ink, never GV's markup")
    measured = ink(label) if ink is not None else None
    if measured is None:
        return Check("gv_ink", "unknown", "the vendor value's own ink could not be looked at")
    if measured[1] > GV_INK_SHARE:
        return Check("gv_ink", "fail", "the vendor value's own ink is GV's colour (GV's markup)")
    return Check("gv_ink", "pass")


def _trade_check(measures: object, ends: tuple[Ends, str]) -> Check:
    if ends[0] == "room":
        return Check("trade", "fail", ends[1])
    if _ROOM_WORDS.search(str(measures or "")):
        return Check(
            "trade", "fail", f"it measures room context or another trade ({_plain(measures)})"
        )
    return Check("trade", "pass")


#: The check that records code's answer to the AI's own "different points" (`objection`): it
#: passes when code's evidence contradicts it, so the pair is verified like any other.
OBJECTION: Final = "objection"


def _contradicts(pair: Mapping[str, Any], ends: Mapping[str, str]) -> bool:
    """Whether the AI's own answer contradicts its "different points" (#1773 review): it writes
    the very same end words on both drawings (in either order, every end named) and gives no note.
    Ends of one rough kind are not enough (two outlets' centre lines, two units' tops are
    different points), and the note is where the AI says why the points differ."""
    if str(pair.get("note") or "").strip():
        return False
    vendor = [_plain(ends["vendor_from"]), _plain(ends["vendor_to"])]
    architect = [_plain(ends["architect_from"]), _plain(ends["architect_to"])]
    if not all(vendor + architect):
        return False
    return vendor in (architect, architect[::-1])


def _objection(matched: tuple[Ends, str], differ: bool) -> Check | None:
    """Code's answer to the AI's "different points" (DeepSeekMath-V2: check the "no" as well as
    the "yes"): a passed `objection` check when the AI's own end words are the same words on both
    drawings with no note (`_contradicts`, checked first), code's end words agree and code reads
    two different values; None otherwise (the AI's "no" stands). A pair it lets through still
    needs every code check and the blind look to be red.

    Code's drawn geometry (`same_extent`) is never evidence against the "no": it allows the two
    lines' ends to differ by the values' own difference, which is exactly what two dimensions to
    different points look like (a top against an underside), so it only ever blocks."""
    if not differ or matched[0] != "same":
        return None
    return Check(
        OBJECTION,
        "pass",
        f"the AI said {DIFFERENT_POINTS}, but its own end words are the same on both drawings "
        f"({matched[1]})",
    )


def _geometry_check(vendor: Mapping[str, Any], architect: Mapping[str, Any], pack: Pack) -> Check:
    def side(label: Mapping[str, Any], wanted: str) -> list[dict[str, Any]]:
        file = str(label.get("file") or pack.main_file)
        on = pack.on_page(file, int(label.get("page") or 0))
        return [x for x in on if x.get("side") == wanted]

    state, why = same_extent(
        vendor,
        architect,
        side(vendor, "vendor"),
        # The brand prototype's own labels when it is the reference (R1).
        side(architect, "brand" if architect.get("side") == "brand" else "architect"),
    )
    if state == "different_extent":
        return Check("geometry", "fail", f"code's geometry shows different points: {why}")
    return Check("geometry", "pass", why if state == "same_extent" else f"cannot tell: {why}")


def _slip_check(vendor: Fraction, architect: Fraction, drawn: bool) -> Check:
    slip = is_slip(
        Decimal(vendor.numerator) / Decimal(vendor.denominator),
        Decimal(architect.numerator) / Decimal(architect.denominator),
        drawn=drawn,
    )
    return Check("slip", "fail", f"check the reading: {slip}") if slip else Check("slip", "pass")


#: The order a candidate's checks are told in: the first that fails is its reason.
_ORDER: Final = (
    "roles",
    "area",
    "read",
    "view",
    "axis",
    "round",
    "units",
    "flags",
    "slip",
    "gv_ink",
    "trade",
    "ends",
    "geometry",
    OBJECTION,
)


def _first_failure(checks: Sequence[Check]) -> Check | None:
    by_name = {check.name: check for check in checks}
    for name in _ORDER:
        check = by_name.get(name)
        if check is not None and check.outcome != "pass":
            return check
    return None


# ---- the finding ----------------------------------------------------------------------------------


def _category(item: Item, what: object) -> str:
    """The product an item of the answer is filed under: the one its OWN words name (what it
    measures: "vanity top width" is a countertop's even in an area called "Standard Vanity"), by
    the product registry's words and hints ("top of counter", "upper outlet"); else, on a drawing
    of one product, that product; else "other": the finding book files it under every product
    its page shows (`product_scope.filed_under`, the inclusive rule of 2026-10-19: an outlet or a
    string on a drawing of several products counts for each of them)."""
    named = named_products(str(what or ""), hints=True, every=True)
    if len(named) == 1:
        return next(iter(named))
    if named:
        return "other"
    return item.category


def _name(pack: Pack, item: Item, what: object, fallback: str) -> str:
    area = pack.area_name(item.vendor.file, item.vendor.page)
    words = " ".join(str(what or "").split()) or fallback
    words = words[:1].upper() + words[1:]
    name = f"{area}: {words}" if area and not words.lower().startswith(area.lower()) else words
    return name[:_ITEM_CHARS]


def _about(item: Item, pack: Pack, what: object, fallback: str) -> dict[str, Any]:
    """A listed item's page, name and product (`Verified.about`)."""
    return {
        "page": item.vendor.page,
        "item": _name(pack, item, what, fallback),
        "category": _category(item, what),
    }


def _about_other(base: Mapping[str, Any]) -> dict[str, Any]:
    return {key: base.get(key) for key in ("page", "item", "category")}


def _spot(box: Box | None, file: str, page: object) -> dict[str, Any] | None:
    if box is None or not isinstance(page, int) or page < 1:
        return None
    return {"page": page, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3], "file": file}


def _label_spot(label: Mapping[str, Any], pack: Pack) -> dict[str, Any] | None:
    file = str(label.get("file") or pack.main_file)
    return _spot(_box(label.get("box")), file, int(label.get("page") or 0))


def _provenance(label: Mapping[str, Any], pack: Pack) -> dict[str, Any]:
    return {
        "id": label.get("id"),
        "page": label.get("page"),
        "file": str(label.get("file") or pack.main_file),
        "text": label.get("text"),
        "inches": label.get("inches"),
        "route": label.get("route"),
        "side": label.get("side"),
        "box": list(label.get("box") or []),
    }


#: Not-compared reasons as the finding's `keep_reason` makes `classification` say them.
_KEEP: Final[Mapping[str, str]] = {
    "different_reference": "measures_something_else",
    "nothing_to_compare": "architect_prints_nothing",
    "unreadable": "unreadable",
    "layout": "layout",
}


def _sentence(words: str) -> str:
    words = words.strip()
    text = words[:1].upper() + words[1:]
    return (text if text.endswith(".") else text + ".")[:_WHY_CHARS]


def _pair_finding(
    item: Item,
    pack: Pack,
    measures: object,
    vendor: Mapping[str, Any],
    architect: Mapping[str, Any] | None,
    status: str,
    why: str,
    reason_code: str | None = None,
) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "page": int(vendor.get("page") or item.vendor.page),
        "item": _name(pack, item, measures, f"dimension {_label_words(vendor)}"),
        "category": _category(item, measures),
        "architect": _label_words(architect) if architect is not None else "no size printed",
        "vendor": _label_words(vendor),
        "status": status,
        "why": _sentence(why),
        "verified": False,
        "source": "architect",
        "vendor_box": _label_spot(vendor, pack),
        "dims": {"vendor": _provenance(vendor, pack)},
        "round": item.vendor.round,
    }
    if architect is not None:
        finding["arch_box"] = _label_spot(architect, pack)
        finding["dims"]["architect"] = _provenance(architect, pack)
        if architect.get("page") != vendor.get("page"):
            finding["vs_page"] = int(architect.get("page") or 0)
        if architect.get("side") == "brand":
            finding["source"] = "brand_prototype"
    if item.prototype_reference:
        # R1: no architect elevation of the area; the brand prototype is the reference.
        finding["source"] = "brand_prototype"
        finding["reference"] = PROTOTYPE_REFERENCE
    if status == "check" and reason_code is not None and reason_code in _KEEP:
        finding["keep_reason"] = _KEEP[reason_code]
    return finding


# ---- one pair -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Pair:
    key: str
    item: Item
    pack: Pack
    raw: Mapping[str, Any]
    vendor: dict[str, Any]
    architect: dict[str, Any] | None
    ends: Mapping[str, str]

    def grey(self, reason: str, code: str, checks: Sequence[Check] = ()) -> Verified:
        finding = _pair_finding(
            self.item,
            self.pack,
            self.raw.get("measures"),
            self.vendor,
            self.architect,
            "check",
            reason,
            code,
        )
        return Verified(
            self.key,
            "pair",
            "not_compared",
            reason,
            code,
            tuple(checks),
            self.vendor,
            self.architect,
            finding,
            ends=self.ends,
        )


def verify_pair(
    pair: Mapping[str, Any],
    item: Item,
    pack: Pack,
    ink: Ink | None = None,
    *,
    architect: Mapping[str, Any] | None = None,
    number: NumberReader | None = None,
) -> Verified:
    """One pair verified from scratch, every check, no shortcut: the resolve loop's re-verification
    of a pair it amended (`resolve.py`). `architect`: the architect's value when it is not one label
    (code's proved chain of the architect's printed parts, owner decision 2026-10-19), with
    `area_ref` naming one of its labels for the area check. `number`: `read_number`, for a label
    whose value code cannot read (`_read_label`)."""
    return _verify_pair(pair, item, pack, ink, architect_override=architect, number=number)


def _read_label(
    label: dict[str, Any], side: str, pack: Pack, number: NumberReader | None
) -> Fraction | None:
    """A label's inches when code cannot read its value (#1781): `read_number` at the label (its
    id and box, on its file's own page), used only when the tool says read (its two-route
    agreement rule). The pack's copy of the label takes the reading (`inches` and `read_number`),
    so the resolve loop's re-verification and the finding see the value and how it was read;
    `dims.json` is never changed. `None` when it is not read (the label keeps the tool's answer)."""
    done = label.get("read_number")
    if isinstance(done, Mapping):
        return _fraction(label.get("inches")) if done.get("outcome") == "read" else None
    if number is None:
        return None
    file = str(label.get("file") or pack.main_file)
    page = int(label.get("page") or 0)
    reading = number(
        Request(
            file,
            pack.own_page(file, page),
            side,
            label=str(label.get("id") or "") or None,
            box=_box(label.get("box")),
        )
    )
    label["read_number"] = reading.as_dict()
    value = reading.inches if reading.outcome == "read" else None
    if value is None or value <= 0:
        return None
    label["inches"] = format(value.normalize(), "f")
    return Fraction(value)


def _verify_pair(
    pair: Mapping[str, Any],
    item: Item,
    pack: Pack,
    ink: Ink | None,
    *,
    architect_override: Mapping[str, Any] | None = None,
    number: NumberReader | None = None,
) -> Verified:
    vref = str(pair.get("vendor_label") or "")
    key = f"{item.id}:{vref}"
    vendor = pack.label(vref, item.vendor.file, item.vendor.page)
    verdict = str(pair.get("verdict") or "unsure").strip().lower()
    ends = {
        name: str(pair.get(name) or "")
        for name in ("vendor_from", "vendor_to", "architect_from", "architect_to")
    }
    if vendor is None:
        return Verified(key, "pair", "covered", f"code has no vendor label {vref}")
    aref = pair.get("architect_label")
    architect = (
        dict(architect_override)
        if architect_override is not None
        else _architect_label(aref, item, pack) if isinstance(aref, str) and aref.strip() else None
    )
    about = _about(item, pack, pair.get("measures"), f"dimension {_label_words(vendor)}")
    if verdict == "no_architect_value":
        return Verified(
            key,
            "pair",
            "listed",
            NO_SIZE,
            "nothing_to_compare",
            vendor=vendor,
            ends=ends,
            about=about,
        )
    code = pack.pairs.get(_ref(vendor))
    if code is not None:
        # Code paired this vendor label by itself (`pair_findings`): its own finding decides it.
        said = "the same" if architect is not None and code == _ref(architect) else "another"
        why = f"code paired {_ref(vendor)} with {code} by itself ({said} architect label)"
        return Verified(key, "pair", "covered", why, vendor=vendor, architect=architect, ends=ends)
    one = _Pair(key, item, pack, pair, vendor, architect, ends)
    if architect is None:
        if verdict in ("same", "different", "different_points"):
            return one.grey(
                f"not proved: the answer names no architect label code knows ({aref or 'none'})",
                "not_proved",
            )
        # Unsure (or a verdict the form does not know) with no architect label: its own reason.
        return Verified(
            key, "pair", "listed", AI_UNSURE, "ai_unsure", vendor=vendor, ends=ends, about=about
        )
    objected = verdict == "different_points"
    if not objected and verdict not in ("same", "different"):
        note = str(pair.get("note") or "").strip()
        return one.grey(UNSURE + (f": {note}" if note else ""), "not_proved")
    if objected and not _contradicts(pair, ends):
        # Nothing in the AI's own answer contradicts its "different points": it stands.
        return one.grey(DIFFERENT_POINTS, "different_reference")
    v_in, a_in = _fraction(vendor.get("inches")), _fraction(architect.get("inches"))
    if v_in is None:
        v_in = _read_label(vendor, "vendor", pack, number)
    if a_in is None and architect_override is None:
        a_in = _read_label(architect, "architect", pack, number)
    if v_in is None or a_in is None:
        if objected:
            return one.grey(DIFFERENT_POINTS, "different_reference")
        tool = [
            str(label["read_number"].get("reason") or "")
            for label in (vendor, architect)
            if isinstance(label.get("read_number"), Mapping)
            and label["read_number"].get("outcome") != "read"
        ]
        said = f" (the read-number tool: {tool[0]})" if tool and tool[0] else ""
        return one.grey(f"code could not read the value as a size{said}", "unreadable")
    a_file = str(architect.get("file") or pack.main_file)
    a_page = int(architect.get("page") or 0)
    matched = ends_match(
        ends["vendor_from"], ends["vendor_to"], ends["architect_from"], ends["architect_to"]
    )
    same_axis = vendor.get("orientation") == architect.get("orientation")
    disagree = "disagrees" in (vendor.get("units"), architect.get("units"))
    checks = [
        _roles_check(vendor, architect, item.prototype_reference),
        _area_check(
            item,
            pack,
            a_file,
            a_page,
            _box(architect.get("box")),
            str(architect.get("area_ref") or _ref(architect)),
        ),
        _view_check(item, a_file, a_page),
        Check(
            "axis",
            "pass" if same_axis else "fail",
            "" if same_axis else "one dimension is a width and the other a height",
        ),
        Check(
            "units",
            "fail" if disagree else "pass",
            "its millimetres and inches disagree" if disagree else "",
        ),
        _trade_check(pair.get("measures"), matched),
        Check("ends", "pass" if matched[0] == "same" else "fail", matched[1]),
        _geometry_check(vendor, architect, pack),
    ]
    if objected:
        objection = _objection(matched, _differ(v_in, a_in))
        if objection is None:
            return one.grey(DIFFERENT_POINTS, "different_reference", checks)
        checks.append(objection)
    if verdict == "same" or not _differ(v_in, a_in):
        return _right(one, checks, matched, v_in, a_in, said_different=verdict == "different")
    checks.append(_round_check(pack, item, vendor))
    checks.append(_slip_check(v_in, a_in, "drawn" in (vendor.get("route"), architect.get("route"))))
    checks.append(_ink_check(vendor, ink))
    checks.append(_flags_check(vendor, architect))
    failed = _first_failure(checks)
    if failed is not None:
        if objected:
            # The AI's "different points" was not supported, but another check stops it: the
            # AI's own reason stays the item's reason.
            return one.grey(DIFFERENT_POINTS, "different_reference", checks)
        return _blocked(one, failed, matched, checks)
    why = (
        f"The vendor prints {_label_words(vendor)}; the "
        + ("brand prototype" if item.prototype_reference else "architect")
        + f" prints {_label_words(architect)} for the same dimension "
        f"({_plain(ends['vendor_from'])} to {_plain(ends['vendor_to'])}). Code read both values "
        "at their labels"
        + (
            f" ({PROTOTYPE_REFERENCE_WORDS}: no architect elevation)"
            if item.prototype_reference
            else ""
        )
    )
    finding = _pair_finding(item, pack, pair.get("measures"), vendor, architect, "different", why)
    return Verified(
        key,
        "pair",
        "candidate",
        checks=tuple(checks),
        vendor=vendor,
        architect=architect,
        finding=finding,
        blind="pair",
        ends=ends,
    )


#: A label's notes that make its value no proof of a mistake (#1778): verify in field (the size is
#: taken on site) and plus or minus (the size is approximate).
_NOT_PROOF_FLAGS: Final = {"vif": "verify in field (VIF)", "approx": "plus or minus (approximate)"}


def _flags_check(vendor: Mapping[str, Any], architect: Mapping[str, Any]) -> Check:
    """`flags`: neither value is marked verify in field or plus or minus (`dim_labels`, #1778);
    such a value is read, but a difference against it is never red."""
    for side, label in (("vendor", vendor), ("architect", architect)):
        for flag in label.get("flags") or ():
            if flag in _NOT_PROOF_FLAGS:
                return Check("flags", "fail", f"the {side} marks it {_NOT_PROOF_FLAGS[flag]}")
    return Check("flags", "pass")


def _blocked(
    one: _Pair, failed: Check, matched: tuple[Ends, str], checks: Sequence[Check]
) -> Verified:
    why = failed.why or f"the {failed.name} check could not be made"
    if failed.name == "ends" and matched[0] == "different":
        return one.grey(f"{DIFFERENT_POINTS}: {why}", "different_reference", checks)
    if failed.name == "geometry":
        return one.grey(why, "different_reference", checks)
    if failed.name == "roles" and "brand" in why:
        return one.grey(why, "prototype", checks)
    return one.grey(f"not proved: {why}", "not_proved", checks)


def _right(
    one: _Pair,
    checks: Sequence[Check],
    matched: tuple[Ends, str],
    v_in: Fraction,
    a_in: Fraction,
    *,
    said_different: bool,
) -> Verified:
    """A pair called the same (or values code reads as equal): Right only with code's agreement:
    the values equal, the same item, the same way, not different points."""
    assert one.architect is not None
    if _differ(v_in, a_in):
        return one.grey(
            f"called the same, but code reads {_label_words(one.vendor)} against "
            f"{_label_words(one.architect)}",
            "not_proved",
            checks,
        )
    by_name = {check.name: check for check in checks}
    for name in ("roles", "area", "view", "axis", "units", "trade", "geometry"):
        if by_name[name].outcome == "fail":
            code = "prototype" if "brand" in by_name[name].why else "not_proved"
            return one.grey(f"called the same, but {by_name[name].why}", code, checks)
    if matched[0] == "different":
        return one.grey(f"{DIFFERENT_POINTS}: {matched[1]}", "different_reference", checks)
    why = "The vendor's value is the architect's: code read both labels" + (
        " (the answer said different; code reads them equal)" if said_different else ""
    )
    finding = _pair_finding(
        one.item, one.pack, one.raw.get("measures"), one.vendor, one.architect, "same", why
    )
    return Verified(
        one.key,
        "pair",
        "right",
        checks=tuple(checks),
        vendor=one.vendor,
        architect=one.architect,
        finding=finding,
        ends=one.ends,
    )


def _architect_label(ref: str, item: Item, pack: Pack) -> dict[str, Any] | None:
    """The architect label a ref names: on the page it names (a bare `D4` only when the item has
    one architect page), in the file of the item's architect drawing on that page."""
    parsed = parse_ref(ref)
    if parsed is None:
        return None
    named, _number = parsed
    pages = sorted({(r.file, r.page) for r in item.architect if named is None or r.page == named})
    if named is None and len(pages) != 1:
        return None
    file, page = pages[0] if pages else (item.vendor.file, named or item.vendor.page)
    return pack.label(ref, file, page)


# ---- other items ----------------------------------------------------------------------------------

#: A value printed in a phrase: feet-inches, inches with a fraction, a fraction, millimetres.
_NUM: Final = re.compile(
    r"(\d+)\s*['’]\s*-?\s*(\d+(?:\s+\d+/\d+)?)?\s*\"?|(\d+(?:\.\d+)?)(?:\s+(\d+)/(\d+))?"
    r"|(\d+)/(\d+)"
)


def values_in(text: str) -> set[Fraction]:
    """Every number a text prints, as inches: feet-inches, inches, fractions, and millimetres as
    inches (a vendor printing 457 [18], or 457 alone, prints 18")."""
    out: set[Fraction] = set()
    for found in _NUM.finditer(text or ""):
        if found.group(1) is not None:
            inches = to_inches(found.group(0).replace("’", "'"))
            if inches is not None:
                out.add(Fraction(inches))
        elif found.group(3) is not None:
            whole = Fraction(Decimal(found.group(3)))
            if found.group(4) and int(found.group(5)) != 0:
                whole += Fraction(int(found.group(4)), int(found.group(5)))
            out.add(whole)
            out.add(whole / Fraction(254, 10))
        elif int(found.group(7)) != 0:
            out.add(Fraction(int(found.group(6)), int(found.group(7))))
    return out


def _tag_keys(text: str) -> set[str]:
    return {key for _printed, _family, key in tag_codes(text)}


def _readable(phrases: Iterable[tuple[str, Box]]) -> bool:
    return sum(1 for text, _box in phrases if re.search(r"[A-Za-z]{2,}", text)) >= MIN_TEXT


def absence(
    pack: Pack, area: tuple[str, int], *, value: Fraction | None, tag: str | None
) -> tuple[bool, str]:
    """Whether no vendor sheet of the area prints the value (or the tag) in any round, and why
    (`architect_items`' rule: every vendor sheet must have a readable text layer). (True, the
    sheets searched) when absent; (False, why not proved) otherwise."""
    drawings = pack.vendor_drawings(area)
    if not drawings:
        return False, "no vendor sheet of the area to search"
    searched: list[str] = []
    for file, page, half in drawings:
        words = [
            (str(p.get("text") or ""), b)
            for p in pack.phrases.get((file, page), ())
            if (b := _box(p.get("box"))) is not None and _centre_in(b, half.box, 0.0)
        ]
        mine = [
            x
            for x in pack.on_page(file, page)
            if x.get("side") == "vendor" and _centre_in(_box(x.get("box")), half.box, 0.0)
        ]
        where = f"page {page}" + (f" round {half.round}" if half.round else "")
        if not _readable(words) or len(mine) < _MIN_LABELS:
            return False, f"the vendor's sheet ({where}) has no readable text layer to search"
        searched.append(where)
        vendor_only = all(h.role == "vendor" for h in pack.halves(file, page))
        text = pack.texts.get((file, page), "") if vendor_only else ""
        if value is not None:
            for label in mine:
                inches = _fraction(label.get("inches"))
                if inches is not None and abs(inches - value) <= _FOUND_IN:
                    return False, f"the vendor prints it on {where} ({label.get('text')})"
            for phrase, _b in words:
                if any(abs(v - value) <= _FOUND_IN for v in values_in(phrase)):
                    return False, f"the vendor prints it on {where} ({phrase[:30]})"
            if any(abs(v - value) <= _FOUND_IN for v in values_in(text)):
                return False, f"the vendor's {where} prints it"
        if tag is not None and (
            any(tag in _tag_keys(phrase) for phrase, _b in words) or tag in _tag_keys(text)
        ):
            return False, f"the vendor prints {tag} on {where}"
    return True, "searched " + ", ".join(searched)


def _scope_gate(
    entry: Mapping[str, Any], item: Item, pack: Pack, products: Sequence[str], a_file: str
) -> str | None:
    """Code's own millwork gate for a missing item (the model's `millwork` flag is not trusted
    alone): why it is not shown to be millwork, or None. Its words, or the architect's printed
    words beside it, must name a product in scope (the registry's words and hints), and none of
    its words is room context."""
    words = f"{entry.get('what') or ''} {entry.get('architect') or ''}"
    room = _ROOM_WORDS.search(words)
    if room is not None:
        return f"room context or another trade's work ({room.group(0).lower()}), not millwork"
    scope = set(products)
    if named_products(words, hints=True, every=True) & scope:
        return None
    page = entry.get("architect_page")
    box = _box(entry.get("architect_box"))
    if isinstance(page, int) and box is not None:
        near = (box[0] - _NEAR_PT, box[1] - _NEAR_PT, box[2] + _NEAR_PT, box[3] + _NEAR_PT)
        beside = " ".join(
            str(p.get("text") or "")
            for p in pack.phrases.get((a_file, page), ())
            if _centre_in(_box(p.get("box")), near, 0.0)
        )
        if named_products(beside, hints=True, every=True) & scope and not _ROOM_WORDS.search(
            beside
        ):
            return None
    return "code finds no millwork word for it (in its words or the drawing's words beside it)"


def _architect_printed(
    entry: Mapping[str, Any], pack: Pack, a_file: str
) -> tuple[dict[str, Any] | None, str | None, Fraction | None, str]:
    """(the architect label printing the missing item's value inside its box, or the tag key
    printed there, the value, why not) for an item missing on the vendor."""
    page = entry.get("architect_page")
    box = _box(entry.get("architect_box"))
    text = str(entry.get("architect") or "")
    if not isinstance(page, int) or box is None:
        return None, None, None, "the answer gives no spot on the architect's drawing"
    value = _inches_of(text)
    if value is not None:
        hits = [
            x
            for x in pack.on_page(a_file, page)
            if x.get("side") in ("architect", "brand")
            and _fraction(x.get("inches")) == value
            and _centre_in(_box(x.get("box")), box, 6.0)
        ]
        if not hits:
            return None, None, value, f"code finds no {text} printed at the architect's spot"
        return hits[0], None, value, ""
    keys = _tag_keys(text)
    if len(keys) == 1:
        tag = next(iter(keys))
        if any(
            tag in _tag_keys(str(p.get("text") or "")) and _centre_in(_box(p.get("box")), box, 6.0)
            for p in pack.phrases.get((a_file, page), ())
        ):
            return None, tag, None, ""
        return None, None, None, f"code finds no {text} printed at the architect's spot"
    return None, None, None, "the architect's side is not one size or tag code can read"


#: An other item's Not-compared reason and words, by kind.
_ITEM_REASONS: Final[Mapping[str, tuple[str, str]]] = {
    "layout_count": ("layout", "the layout differs by the AI's reading; code cannot count it"),
    "tag": (
        "not_proved",
        "the tags differ by the AI's reading; code's finish check did not prove it",
    ),
    "note": ("not_proved", "a note the AI read; code cannot check it"),
    "added_on_vendor": (
        "not_proved",
        "the vendor shows it and the architect does not, by the AI's reading",
    ),
    "dimension_without_label": ("unreadable", UNREAD),
    "missing_on_vendor": (
        "not_proved",
        "the architect shows it; the vendor's drawing does not, by the AI's reading",
    ),
}


@dataclass(frozen=True, slots=True)
class _Other:
    key: str
    kind: str
    entry: Mapping[str, Any]
    base: Mapping[str, Any]

    def grey(
        self,
        reason: str,
        code: str,
        checks: Sequence[Check] = (),
        reads: Mapping[str, Any] | None = None,
        **fields: Any,
    ) -> Verified:
        finding = {**self.base, "status": "check", "why": _sentence(reason), **fields}
        keep = _KEEP.get(code)
        if keep is not None:
            finding["keep_reason"] = keep
        out_code = "not_proved" if code == "layout" else code
        return Verified(
            self.key,
            self.kind,
            "not_compared",
            reason,
            out_code,
            tuple(checks),
            finding=finding,
            reads=dict(reads or {}),
        )


def _verify_other(
    entry: Mapping[str, Any],
    at: int,
    item: Item,
    pack: Pack,
    read: ValueReader | None,
    products: Sequence[str],
    number: NumberReader | None = None,
) -> Verified:
    kind = str(entry.get("kind") or "note").strip().lower()
    verdict = str(entry.get("verdict") or "unsure").strip().lower()
    key = f"{item.id}:item{at}"
    a_page = entry.get("architect_page")
    a_file = next((r.file for r in item.architect if r.page == a_page), item.vendor.file)
    vendor_words = str(entry.get("vendor") or "").strip()
    architect_words = str(entry.get("architect") or "").strip()
    base: dict[str, Any] = {
        "page": item.vendor.page,
        "item": _name(pack, item, entry.get("what"), kind.replace("_", " ")),
        "category": _category(item, entry.get("what")),
        "architect": architect_words or "not shown on the architect's drawing",
        "vendor": vendor_words or "not shown on the vendor's drawing",
        "verified": False,
        "source": "architect",
        "vendor_box": _spot(_box(entry.get("vendor_box")), item.vendor.file, item.vendor.page),
        "arch_box": _spot(_box(entry.get("architect_box")), a_file, a_page),
    }
    if item.prototype_reference:
        # R1: no architect elevation of the area; the brand prototype is the reference.
        base["source"] = "brand_prototype"
        base["reference"] = PROTOTYPE_REFERENCE
    if isinstance(a_page, int) and a_page != item.vendor.page:
        base["vs_page"] = a_page
    other = _Other(key, kind, entry, base)
    if verdict == "same":
        return Verified(key, kind, "covered", "the AI reads it the same; code cannot check it")
    if verdict == "no_architect_value":
        return Verified(
            key, kind, "listed", NO_SIZE, "nothing_to_compare", about=_about_other(base)
        )
    if verdict == "unsure":
        return other.grey(UNSURE, "not_proved")
    if kind == "missing_on_vendor":
        return _verify_missing(other, item, pack, products, a_file)
    if kind == "dimension_without_label":
        return _verify_unlabelled(other, item, pack, read, a_file, number)
    code, words = _ITEM_REASONS.get(kind, ("not_proved", "code cannot check it"))
    return other.grey(words, code)


def _verify_missing(
    other: _Other, item: Item, pack: Pack, products: Sequence[str], a_file: str
) -> Verified:
    entry = other.entry
    if entry.get("millwork") is not True:
        return Verified(
            other.key,
            other.kind,
            "listed",
            "room context, not millwork, by the AI's reading: not a vendor item",
            "not_proved",
            about=_about_other(other.base),
        )
    gate = _scope_gate(entry, item, pack, products, a_file)
    if gate is not None:
        return Verified(
            other.key, other.kind, "listed", gate, "not_proved", about=_about_other(other.base)
        )
    label, tag, value, why = _architect_printed(entry, pack, a_file)
    vendor = "not proved missing"
    if label is None and tag is None:
        return other.grey(f"not proved: {why}", "not_proved", vendor=vendor)
    if label is not None and label.get("side") == "brand" and not item.prototype_reference:
        return other.grey(
            "the reference is the brand prototype (reference only)", "prototype", vendor=vendor
        )
    page = int(entry.get("architect_page") or 0)
    area = pack.area_key(item.vendor.file, item.vendor.page)
    area_check = _area_check(
        item,
        pack,
        a_file,
        page,
        _box(label.get("box")) if label else _box(entry.get("architect_box")),
    )
    if area is None or area_check.outcome != "pass":
        return other.grey(
            f"not proved: {area_check.why or 'the page map gives no area'}",
            "not_proved",
            vendor=vendor,
        )
    absent, how = absence(pack, area, value=value, tag=tag)
    if not absent:
        return other.grey(f"not proved missing: {how}", "not_proved", vendor=f"{vendor} ({how})")
    printed = str(label.get("text")) if label is not None else str(tag)
    finding: dict[str, Any] = {
        **other.base,
        "architect": printed,
        "vendor": "not shown on the vendor's drawings",
        "status": "different",
        "why": _sentence(
            f"The architect's drawing prints {printed} (page {page}); no vendor sheet of this "
            f"area prints it in any round ({how}), read by code from the drawings' own text and "
            "labels"
        ),
        "paired_by": "code",
        "text_proof": {
            "kind": "missed",
            "code": printed,
            "architect": [{"page": page, "text": printed}],
            "vendor": [{"page": p, "round": h.round} for _f, p, h in pack.vendor_drawings(area)],
            "pairing": "the work list pairs the drawings by the page map's area",
        },
    }
    if label is not None:
        finding["arch_box"] = _label_spot(label, pack)
        finding["dims"] = {"architect": _provenance(label, pack)}
    return Verified(
        other.key,
        other.kind,
        "candidate",
        checks=(Check("scope", "pass"), Check("absence", "pass", how)),
        architect=label,
        finding=finding,
        blind="absence",
    )


#: End kinds that only a height runs to, and those only a width runs to (`_ends_axis`).
_VERTICAL_ENDS: Final = frozenset(
    {
        "floor",
        "counter_top",
        "counter_under",
        "splash_top",
        "upper_under",
        "upper_top",
        "unit_top",
        "toe",
    }
)
_HORIZONTAL_ENDS: Final = frozenset({"wall", "sink"})


def _ends_axis(start: object, end: object) -> str | None:
    """`v` when the ends are heights (the floor, the top of the counter), `h` when they are walls or
    a sink's centre line, None when they do not say (or say both)."""
    kinds = {end_kind(start)[0], end_kind(end)[0]}
    vertical, horizontal = bool(kinds & _VERTICAL_ENDS), bool(kinds & _HORIZONTAL_ENDS)
    if vertical == horizontal:
        return None
    return "v" if vertical else "h"


def _label_axis(pack: Pack, file: str, page: int, box: Box, value: Fraction) -> str | None:
    """The way a value runs when code labelled it at that spot (`h` / `v`), else None (a box's
    shape does not say: a drawn label's box is often wider than it runs)."""
    for label in pack.on_page(file, page):
        if _fraction(label.get("inches")) == value and _centre_in(_box(label.get("box")), box, 4.0):
            orientation = label.get("orientation")
            return str(orientation) if orientation in ("h", "v") else None
    return None


def _newer_value(pack: Pack, item: Item, value: Fraction, axis: str | None) -> str | None:
    """A label of the area's newest vendor round printing the value (the same way, when known)."""
    key = pack.area_key(item.vendor.file, item.vendor.page)
    newest = pack.newest_round(key)
    if key is None or newest is None:
        return None
    for file, page, half in pack.vendor_drawings(key):
        if half.round != newest:
            continue
        for label in pack.on_page(file, page):
            if (
                label.get("side") == "vendor"
                and _fraction(label.get("inches")) == value
                and (axis is None or label.get("orientation") == axis)
                and _centre_in(_box(label.get("box")), half.box, 0.0)
            ):
                return f"p{page}.{label.get('id')}"
    return None


def _read_unread(
    other: _Other,
    number: NumberReader | None,
    sides: Sequence[tuple[str, str, int, Box | None]],
    texts: dict[str, str],
    found: dict[str, Any],
) -> _Other:
    """The sides the AI could not read (no value, `unreadable`), read by `read_number` at their
    box (#1746): a value code reads by two routes replaces the AI's none."""
    if number is None:
        return other
    base = dict(other.base)
    for side, file, page, box in sides:
        if box is None or _inches_of(texts[side]) is not None or not isinstance(page, int):
            continue
        reading = number(Request(file, page, side, box=box))
        found[f"read_number_{side}"] = reading.as_dict()
        if reading.outcome == "read" and reading.text:
            texts[side] = reading.text
            base[side] = reading.text
    return _Other(other.key, other.kind, other.entry, base)


def _confirm(
    reads: dict[str, Any],
    number: NumberReader | None,
    sides: Sequence[tuple[str, str, int, Box, str]],
) -> None:
    """Each side `verify_value` could not read at its box, read again by `read_number` (#1746):
    its answer, in `verify_value`'s shape, replaces the failed one (a match only when two routes
    of different kinds read the claimed value). A side code read as another value stays so."""
    if number is None:
        return
    for side, file, page, box, text in sides:
        key = f"verify_{side}"
        if isinstance(reads.get(key), Mapping) and reads[key].get("result") in (
            "match",
            "mismatch",  # code read another value there: never read over
        ):
            continue
        reads[key] = number(Request(file, page, side, box=box, claim=text)).as_verify(text)


def _verify_unlabelled(
    other: _Other,
    item: Item,
    pack: Pack,
    read: ValueReader | None,
    a_file: str,
    number: NumberReader | None = None,
) -> Verified:
    """A dimension code has no label for: red only when code reads both values at their boxes
    (`verify_value`, then `read_number` for a value it could not confirm or the AI could not
    read), both run the same way, the vendor box is on the newest round, the pages are one area,
    and the blind look agrees."""
    entry = other.entry
    v_box, a_box = _box(entry.get("vendor_box")), _box(entry.get("architect_box"))
    a_page = entry.get("architect_page")
    texts = {
        "vendor": str(entry.get("vendor") or ""),
        "architect": str(entry.get("architect") or ""),
    }
    found_first: dict[str, Any] = {}
    other = _read_unread(
        other,
        number,
        (
            # Read at each file's own page (a separate file's set page is not its page, #1722).
            ("vendor", item.vendor.file, pack.own_page(item.vendor.file, item.vendor.page), v_box),
            (
                "architect",
                a_file,
                pack.own_page(a_file, a_page) if isinstance(a_page, int) else -1,
                a_box,
            ),
        ),
        texts,
        found_first,
    )
    v_text, a_text = texts["vendor"], texts["architect"]
    v_in, a_in = _inches_of(v_text), _inches_of(a_text)
    if v_in is None or a_in is None:
        why = "; ".join(
            str(r.get("reason")) for r in found_first.values() if r.get("outcome") != "read"
        )
        return other.grey(
            f"{UNREAD}: a side is not one size code can read" + (f" ({why})" if why else ""),
            "unreadable",
            reads=found_first,
        )
    if v_box is None or a_box is None or not isinstance(a_page, int):
        return other.grey(f"{UNREAD}: the answer gives no spot on both drawings", "unreadable")
    if _ROOM_WORDS.search(f"{entry.get('what') or ''}"):
        return other.grey(
            f"not proved: it measures room context or another trade ({_plain(entry.get('what'))})",
            "not_proved",
        )
    v_half = pack.half_at(item.vendor.file, item.vendor.page, v_box)
    a_half = pack.half_at(a_file, a_page, a_box)
    if v_half is None or v_half.role != "vendor":
        roles = Check("roles", "fail", "the vendor box is not on a vendor drawing")
    elif a_half is not None and a_half.role == "brand_prototype" and item.prototype_reference:
        roles = Check("roles", "pass", PROTOTYPE_REFERENCE_WORDS)  # no architect elevation (R1)
    elif a_half is not None and a_half.role == "brand_prototype":
        roles = Check("roles", "fail", "the reference is the brand prototype (reference only)")
    elif a_half is None or a_half.role != "architect":
        roles = Check("roles", "fail", "the architect box is not on an architect drawing")
    else:
        roles = Check("roles", "pass")
    checks = [roles, _area_check(item, pack, a_file, a_page, a_box)]
    reads: dict[str, Any] = dict(found_first)
    if read is not None and all(c.outcome == "pass" for c in checks):
        # Read at each file's own page (a separate file's set page is not its page, #1722).
        reads["verify_vendor"] = read(
            item.vendor.file, pack.own_page(item.vendor.file, item.vendor.page), v_box, v_text
        )
        reads["verify_architect"] = read(a_file, pack.own_page(a_file, a_page), a_box, a_text)
        _confirm(
            reads,
            number,
            (
                (
                    "vendor",
                    item.vendor.file,
                    pack.own_page(item.vendor.file, item.vendor.page),
                    v_box,
                    v_text,
                ),
                ("architect", a_file, pack.own_page(a_file, a_page), a_box, a_text),
            ),
        )
    unread = [
        side
        for side in ("vendor", "architect")
        if not isinstance(reads.get(f"verify_{side}"), Mapping)
        or reads[f"verify_{side}"].get("result") != "match"
    ]
    why_unread = "; ".join(
        str(reads[f"verify_{side}"].get("reason"))
        for side in unread
        if isinstance(reads.get(f"verify_{side}"), Mapping)
        and reads[f"verify_{side}"].get("read_number")
    )
    checks.append(
        Check(
            "read",
            "fail" if unread else "pass",
            (
                (
                    f"code could not confirm the {' and '.join(unread)} value at its box"
                    + (f": {why_unread}" if why_unread else "")
                )
                if unread
                else ""
            ),
        )
    )
    axes = (
        _label_axis(pack, item.vendor.file, item.vendor.page, v_box, v_in),
        _label_axis(pack, a_file, a_page, a_box, a_in),
    )
    known = {axis for axis in axes if axis is not None}
    if len(known) > 1:
        checks.append(Check("axis", "fail", "one value is a width and the other a height"))
    else:
        checks.append(
            Check(
                "axis",
                "pass",
                (
                    f"code's labels run {next(iter(known))}"
                    if len(axes) == 2 and None not in axes
                    else "the blind look's ends decide which way they run"
                ),
            )
        )
    rounds = _round_check(pack, item)
    if rounds.outcome == "fail":
        same = _newer_value(pack, item, v_in, axes[0])
        if same is not None:
            newest = pack.newest_round(pack.area_key(item.vendor.file, item.vendor.page))
            rounds = Check("round", "pass", f"round {newest} prints the same value ({same})")
    checks.append(rounds)
    if _differ(v_in, a_in):
        drawn = any(isinstance(r, Mapping) and r.get("route") == "drawn" for r in reads.values())
        checks.append(_slip_check(v_in, a_in, drawn))
    failed = _first_failure(checks)
    if failed is not None:
        if failed.name == "read":
            return other.grey(f"{UNREAD} ({failed.why})", "unreadable", checks, reads)
        if failed.name == "roles" and "brand" in failed.why:
            return other.grey(failed.why, "prototype", checks, reads)
        return other.grey(f"not proved: {failed.why}", "not_proved", checks, reads)
    if not _differ(v_in, a_in):
        return Verified(
            other.key,
            other.kind,
            "covered",
            "code reads the two values as equal; no label, so not listed as Right",
            checks=tuple(checks),
            reads=reads,
        )
    finding = {
        **other.base,
        "status": "different",
        "why": _sentence(
            f"The vendor prints {v_text}; the architect prints {a_text} for the same dimension "
            f"({_plain(entry.get('what'))}). Code read both values at their spots"
        ),
    }
    return Verified(
        other.key,
        other.kind,
        "candidate",
        checks=tuple(checks),
        finding=finding,
        blind="values",
        ends={"axis": next(iter(known)) if known else ""},
        reads=reads,
    )


# ---- the whole answer -----------------------------------------------------------------------------


def _plain_data(value: Any) -> Any:
    """A pydantic model (`schema.CompareResult`) as plain data; plain data as it is."""
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value


def verify_answer(
    result: Any,
    item: Item,
    pack: Pack,
    *,
    ink: Ink | None = None,
    read: ValueReader | None = None,
    products: Sequence[str] | None = None,
    number: NumberReader | None = None,
) -> Verification:
    """Every pair and other item of one work item's `CompareResult`, decided by code (see the
    module). `ink(label)`: the vendor value's own ink (`full_check_advocate.ink_colour` of its box
    on the markup-free copy), asked only for a red candidate; None blocks red. `read`: `verify_value`
    at a spot, for an item without labels; None: such an item is never red. `products`: the check's
    product scope (None: every active product). `number`: `read_number` (#1746), called by itself
    for a value without a label that `read` could not confirm or the AI could not read, and for a
    labelled value whose inches code cannot read (#1781)."""
    data = _plain_data(result)
    if not isinstance(data, Mapping):
        data = {}
    scope = tuple(products) if products else ProductScope.of(None).products
    out = Verification(item)
    labels = vendor_labels(item, pack)
    own = {_ref(label) for label in labels}
    pairs = [p for p in data.get("pairs") or [] if isinstance(p, Mapping)]
    named = Counter(str(p.get("vendor_label") or "") for p in pairs)
    told_twice: set[str] = set()
    for pair in pairs:
        vref = str(pair.get("vendor_label") or "")
        label = pack.label(vref, item.vendor.file, item.vendor.page)
        if label is None or _ref(label) not in own:
            out.unknown_labels.append(vref)
            continue
        if named[vref] > 1:
            if vref not in told_twice:
                told_twice.add(vref)
                one = _Pair(f"{item.id}:{vref}", item, pack, pair, label, None, {})
                out.items.append(one.grey(f"the answer pairs {vref} more than once", "not_proved"))
            continue
        out.items.append(_verify_pair(pair, item, pack, ink, number=number))
    for at, entry in enumerate(data.get("other_items") or [], start=1):
        if isinstance(entry, Mapping):
            out.items.append(_verify_other(entry, at, item, pack, read, scope, number))
    told = {str(r) for r in data.get("unaccounted_labels") or []}
    out.unaccounted = sorted((r for r in own if r in told), key=_order)
    paired = {
        _ref(lab)
        for p in pairs
        if (lab := pack.label(p.get("vendor_label"), item.vendor.file, item.vendor.page))
    }
    out.missing_labels = sorted(own - paired - told, key=_order)
    return out


def verify(
    compare_result: Any,
    pack: Pack | Path | str,
    item: Item | None = None,
    *,
    ink: Ink | None = None,
    read: ValueReader | None = None,
    products: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """The eval harnesses' entry (`verify(compare_result, pack_dir)`): one row per item, with its
    `verdict` (wrong / right / not_compared / covered) and whether it waits for the blind look
    (`needs_blind`). Without `item`, the work item is worked out from the result's id and the pack
    (`Item.derived`)."""
    facts = pack if isinstance(pack, Pack) else Pack.from_folder(Path(pack))
    data = _plain_data(compare_result)
    work = item
    if work is None:
        work_id = str((data or {}).get("work_item") or "") if isinstance(data, Mapping) else ""
        work = Item.derived(work_id, facts)
    if work is None:
        return [
            {
                "key": str(data.get("work_item") if isinstance(data, Mapping) else ""),
                "kind": "work_item",
                "verdict": "not_compared",
                "outcome": "not_compared",
                "needs_blind": False,
                "reason": "code does not know this work item",
                "reason_code": "not_proved",
            }
        ]
    found = verify_answer(data, work, facts, ink=ink, read=read, products=products)
    return [v.row() for v in found.items]


def _order(ref: str) -> tuple[int, int]:
    parsed = parse_ref(ref)
    return (parsed[0] or 0, parsed[1]) if parsed is not None else (0, 0)


# ---- the blind second look ------------------------------------------------------------------------


def with_blind(
    verified: Verified, answer: BlindAnswer | None, failed: str = "", *, reply: str = ""
) -> Verified:
    """A red candidate after the blind second look: red when it agrees (see the module), else Not
    compared with the reason. `answer` None: the look was not made (`failed` says why: it failed,
    the check was stopped, the money ran out). `reply`: the model's reply when it was not the
    form, kept in the record (never read as an answer) for diagnosis and a $0 replay."""
    if verified.outcome != "candidate":
        return verified
    finding = dict(verified.finding)
    record: dict[str, Any] = (
        answer.as_dict() if answer is not None else {"failed": failed or "not made"}
    )
    if answer is None and reply:
        record["reply"] = reply[:_MAX_KEPT_REPLY]
    if verified.blind == "absence":
        reason = _absence_disagrees(answer, failed)
    else:
        reason = _pair_disagrees(verified, answer, failed)
    checks = (*verified.checks, Check("blind", "fail" if reason else "pass", reason or ""))
    pairing = {
        "vendor": _ref(verified.vendor) if verified.vendor is not None else None,
        "architect": _ref(verified.architect) if verified.architect is not None else None,
        "mode": "simple",
        "advocate": record,
        "checks": [[c.name, c.outcome, c.why] for c in checks],
    }
    if reason is None:
        answers = {c.name: str(c.outcome) for c in checks}
        answers["pairing"] = "code" if verified.blind == "absence" else "proved"
        finding["critic"] = {
            "result": "passed",
            "answers": answers,
            "note": "",
            "model": "devil's advocate",
        }
        finding["pairing"] = pairing
        return replace(verified, outcome="red", checks=checks, finding=finding)
    code = "different_reference" if DIFFERENT_POINTS in reason else "not_proved"
    finding.update(status="check", why=_sentence(reason), pairing=pairing)
    finding.pop("paired_by", None)
    finding.pop("text_proof", None)
    if verified.blind == "absence":
        finding["vendor"] = f"not proved missing ({reason})"[:200]
    if code in _KEEP:
        finding["keep_reason"] = _KEEP[code]
    return replace(
        verified,
        outcome="not_compared",
        reason=reason,
        reason_code=code,
        checks=checks,
        finding=finding,
    )


def _pick(answer: BlindAnswer, page: object) -> str | None:
    if answer.pick is None:
        return None
    parsed = parse_ref(answer.pick)
    if parsed is None:
        return answer.pick
    return f"p{parsed[0] or page}.D{parsed[1]}"


def _pair_disagrees(verified: Verified, answer: BlindAnswer | None, failed: str) -> str | None:
    if answer is None:
        return f"not proved: the blind second look was not made ({failed or 'not made'})"
    if verified.blind == "pair":
        assert verified.architect is not None
        want = _ref(verified.architect)
        pick = _pick(answer, verified.architect.get("page"))
        if pick != want:
            said = "no architect dimension" if pick is None else pick
            return f"not proved: a blind second look chose {said}, not {want}"
    if verified.blind == "chain":
        assert verified.architect is not None
        chain = {str(ref) for ref in verified.architect.get("chain") or ()}
        page = verified.architect.get("page")
        picked = {
            f"p{parsed[0] or page}.D{parsed[1]}"
            for part in re.split(r"[+,\s]+", answer.pick or "")
            if (parsed := parse_ref(part)) is not None
        }
        if not chain or picked != chain:
            said = " + ".join(sorted(picked)) if picked else "no architect dimensions"
            return (
                f"not proved: a blind second look chose {said}, not the architect's parts "
                f"{' + '.join(sorted(chain))}"
            )
    if answer.verdict == "different_points":
        return f"a blind second look says {DIFFERENT_POINTS}"
    if answer.verdict != "same_thing":
        return "not proved: a blind second look is not sure the two measure the same thing"
    state, why = ends_match(
        answer.vendor_from, answer.vendor_to, answer.architect_from, answer.architect_to
    )
    if state == "different":
        return f"a blind second look says {DIFFERENT_POINTS} ({why})"
    if state != "same":
        return f"not proved: by a blind second look, {why}"
    if verified.resolve:
        # A candidate the resolve loop made: the blind look must also name the ends the loop
        # claimed, on both drawings (it never saw them; RESOLVE-LOOP-RESEARCH.md §3b).
        for side in ("vendor", "architect"):
            claimed = (verified.ends.get(f"{side}_from"), verified.ends.get(f"{side}_to"))
            seen = (getattr(answer, f"{side}_from"), getattr(answer, f"{side}_to"))
            state, _why = ends_match(*claimed, *seen)
            if state != "same":
                return (
                    "not proved: a blind second look names other ends on the "
                    f"{side}'s drawing ({_plain(seen[0])} to {_plain(seen[1])}) than the resolve "
                    f"step ({_plain(claimed[0])} to {_plain(claimed[1])})"
                )
    if verified.blind == "values":
        # No code label says which way an unlabelled value runs: the ends must, the same way on
        # both drawings (and as code's label says, when one side has one).
        axes = {
            _ends_axis(answer.vendor_from, answer.vendor_to),
            _ends_axis(answer.architect_from, answer.architect_to),
        }
        code = verified.ends.get("axis") or None
        if None in axes or len(axes) != 1 or (code is not None and axes != {code}):
            return "not proved: code cannot tell both values run the same way"
    return None


def _absence_disagrees(answer: BlindAnswer | None, failed: str) -> str | None:
    if answer is None:
        return f"not proved: the blind second look was not made ({failed or 'not made'})"
    if answer.pick is not None or answer.verdict == "shown":
        found = f" ({answer.pick})" if answer.pick else ""
        return f"not proved: a blind second look finds it on the vendor's drawing{found}"
    if answer.verdict != "absent":
        return "not proved: a blind second look is not sure the vendor leaves it out"
    if answer.millwork is not True:
        return "not proved: by a blind second look it is not millwork"
    for end in (answer.architect_from, answer.architect_to):
        if end_kind(end)[0] == "room":
            return f"not proved: by a blind second look it runs to {_plain(end)} (room context)"
    return None
