"""Missed, changed and added finishes against the architect, by code: $0, no AI (#1623).

The owner (2026-10-18): the architect's drawings are the source of truth, and the review must catch
that the vendor missed something, changed the design or used the wrong material. Sizes are compared
elsewhere; this module compares what the drawings print as text, area by area:

1. **Missed finish.** A finish or material code the architect's drawing of an area prints
   (`FN-1`, `QZ-12`) that no vendor sheet of that area prints, in any round.
2. **Finish changed.** The same, when the family has one code on each side (the architect `FN-1`,
   the vendor's newest round `FN-3`): the vendor tags with another code of the same family.
3. **Added finish.** A code the vendor's newest round prints in a family the architect uses for the
   area, which no architect (or brand prototype) drawing of the area prints.

Only what code can defend is a Wrong (a red cloud on the zero-click drawing, `proof.py`):

- **The area pairing is proved.** GV's typed headings put the drawings in one area (the set pack's
  page map), and code finds the two sides share at least one finish code, or at least
  `MIN_SHARED_DIMS` printed dimensions of 12" or more. Without that, the area is "Not compared".
- **Absence is proved** on every vendor sheet and round of the area: each vendor drawing has
  readable text (`MIN_TEXT` phrases; a sheet drawn as pen strokes proves nothing), and the page's
  whole text (`page_texts.json`, every colour) holds no other copy of the code.
- **Codes are compared only in a family the vendor uses too.** A vendor naming finishes in its own
  system (`SL-2` where the architect writes `QZ-12`) is "Not compared" until a printed legend links
  them (the resolver's rule); so is a product the vendor marks as supplied by GV or others, a code
  printed on another vendor sheet, and a code only the brand prototype prints (R1: the prototype is
  reference only).
- **Only product families count.** The product registry lists each product's tag families
  (`tag_families` in `vocabulary/products.yaml`), and each finding gets that product's category.
  Other trades' tags (tile, paint, wall covering: `decision_resolver.OTHER_TRADE_FAMILIES`) and
  families the registry does not list are left out (counted in the result).

**Notes and symbols.** An architect note naming a millwork part ("PROVIDE (3) FIXED SHELVES") with
no word for it on any vendor sheet is listed under "Not compared" with a pinpointed question, never
a Wrong: a vendor often draws a part without writing its name, so text alone cannot prove it is
missing. Outlet and junction-box symbols are not read here: their drawing differs from one office
to the next, so absence on the vendor's sheet cannot be proved (the drawn-things helper looks).

Every finding is in the chat finding shape (CONTRACT2, `created_by: "architect_items"`,
`paired_by: "code"`) with `text_proof` (what code read, where, and the pages it searched); the trust
rule counts it as the `text` witness (`witness.py`). Pure code: JSON in, plain values out.
Verification: `tests/review/assistant/test_architect_items.py` (made-up sets).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from time import monotonic
from typing import Any, Final

from app.review.assistant.decision_resolver import OTHER_TRADE_FAMILIES
from vocabulary.product_registry import Product, registry

__all__ = [
    "CREATED_BY",
    "MIN_SHARED_DIMS",
    "MIN_TEXT",
    "ItemsResult",
    "add_to_answer",
    "check_pack",
    "check_set",
    "summary_line",
    "tag_codes",
]

_log = logging.getLogger(__name__)

#: Who made the findings (the chat finding's `created_by`).
CREATED_BY: Final = "architect_items"
#: A drawing has readable text when it prints at least this many phrases with words.
MIN_TEXT: Final = 3
#: Printed dimensions both sides of an area share (12" or more each) that prove the pairing.
MIN_SHARED_DIMS: Final = 2
_SHARED_DIM_MIN_IN: Final = Decimal(12)
#: The most findings and Not compared items one set gives.
MAX_FINDINGS: Final = 60
MAX_UNKNOWNS: Final = 80

#: A finish or material code: letters, an optional dash, digits, an optional letter, an optional
#: suffix (`FN-1`, `FN01`, `QZ-12B-CUST`).
_CODE: Final = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,4})(?: ?- ?)?(\d{1,4})([A-Z]?)(?: ?- ?([A-Z]{2,8}))?(?![A-Za-z0-9])"
)
_LETTERS: Final = re.compile(r"[A-Za-z]{2,}")
#: "by GV", "by others", "by GC/Owner", "provided by GV", "N.I.C.": supplied by someone else.
_BY_OTHERS: Final = re.compile(
    r"\b(?:by|provided by|supplied by)\s+(?:GV|G\.V\.?|others|owner|G\.?C\.?|client|"
    r"general contractor)\b|\bN\.?\s?I\.?\s?C\.?(?![A-Z])|\bnot in contract\b",
    re.IGNORECASE,
)
#: Words that name a product's material without a code (the vendor wrote it out).
_MATERIAL_WORDS: Final[Mapping[str, re.Pattern[str]]] = {
    "countertop": re.compile(
        r"\b(?:stone|quartz|granite|marble|solid surface|countertop|counter top)\b", re.IGNORECASE
    ),
    "cabinet": re.compile(
        r"\b(?:laminate|plam|veneer|melamine|wood|lacquer|thermofoil)\b", re.IGNORECASE
    ),
}
#: Millwork parts an architect note may ask for: (the part, the architect's words, the words that
#: count as the vendor naming it).
_PARTS: Final[tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]] = (
    (
        "drawers",
        re.compile(r"\bDRAWERS?\b|\bPULL[- ]?OUTS?\b", re.IGNORECASE),
        re.compile(r"\bDRAWERS?\b|\bPULL[- ]?OUTS?\b|\bDWRS?\b", re.IGNORECASE),
    ),
    (
        "shelves",
        re.compile(r"\bSHEL(?:F|VES|VING)\b", re.IGNORECASE),
        re.compile(r"\bSHEL(?:F|VES|VING)\b|\bSHLF\b", re.IGNORECASE),
    ),
    (
        "pulls",
        re.compile(r"\bPULLS?\b(?![- ]?OUT)", re.IGNORECASE),
        re.compile(r"\bPULLS?\b(?![- ]?OUT)|\bHANDLES?\b|\bKNOBS?\b", re.IGNORECASE),
    ),
    (
        "hinges",
        re.compile(r"\bHINGES?\b", re.IGNORECASE),
        re.compile(r"\bHINGES?\b|\bHINGED\b", re.IGNORECASE),
    ),
    (
        "grommets",
        re.compile(r"\bGROMMETS?\b", re.IGNORECASE),
        re.compile(r"\bGROMMETS?\b", re.IGNORECASE),
    ),
    ("locks", re.compile(r"\bLOCKS?\b", re.IGNORECASE), re.compile(r"\bLOCKS?\b", re.IGNORECASE)),
    (
        "bins",
        re.compile(r"\b(?:TRASH|WASTE|RECYCL\w*)\b|\bBINS?\b", re.IGNORECASE),
        re.compile(r"\b(?:TRASH|WASTE|RECYCL\w*)\b|\bBINS?\b", re.IGNORECASE),
    ),
    (
        "outlets",
        re.compile(r"\bOUTLETS?\b|\bRECEPTACLES?\b|\bJ-?BOX(?:ES)?\b|\bGFC?I\b", re.IGNORECASE),
        re.compile(
            r"\bOUTLETS?\b|\bRECEPTACLES?\b|\bJ-?BOX(?:ES)?\b|\bGFC?I\b|\bDUPLEX\b|\bPOWER\b",
            re.IGNORECASE,
        ),
    ),
)
#: An architect note is a view title, not a requirement ("KITCHENETTE ELEVATION").
_TITLE: Final = re.compile(r"\b(?:ELEVATION|PLAN|SECTION|DETAIL|VIEW)S?\b\s*(?:-.*)?$")
#: Notes: a line joins the one above when this close below it (in line heights) and aligned.
_NOTE_GAP: Final = 0.9
_NOTE_ALIGN_PT: Final = 4.0
#: The most notes listed per area.
_NOTES_PER_AREA: Final = 8

type Box = tuple[float, float, float, float]


# ---- what the drawings print ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Code:
    """One finish or material code as a drawing prints it."""

    text: str
    """As printed (`FN-01-CUST`)."""
    family: str
    key: str
    """The code without its spelling: family, number, letter (`FN-1`). `FN01`, `FN-01` and
    `FN-001-CUST` are one code."""
    phrase: str
    """The whole printed phrase it is in (`(FN-1) LAMINATE DOOR`)."""
    box: Box


def tag_codes(text: str) -> list[tuple[str, str, str]]:
    """Every code in a printed phrase: (as printed, family, key)."""
    out: list[tuple[str, str, str]] = []
    for found in _CODE.finditer(text or ""):
        family, number, letter = found.group(1), found.group(2), found.group(3)
        out.append((found.group(0), family, f"{family}-{int(number)}{letter}"))
    return out


@dataclass(slots=True)
class _Drawing:
    file: str
    page: int
    role: str
    """`architect`, `vendor` or `brand` (the brand prototype)."""
    round: int
    """The vendor's round (1 when not known); 0 on the other sides."""
    box: Box
    phrases: list[tuple[str, Box]] = field(default_factory=list)
    codes: list[Code] = field(default_factory=list)
    dims: set[Decimal] = field(default_factory=set)

    @property
    def readable(self) -> bool:
        """Prints enough words for its text to prove an absence (pen strokes print none)."""
        return sum(1 for text, _box in self.phrases if _LETTERS.search(text)) >= MIN_TEXT

    def code_keys(self) -> set[str]:
        return {code.key for code in self.codes}

    def where(self) -> str:
        return f"p.{self.page}" + (f" round {self.round}" if self.role == "vendor" else "")


@dataclass(slots=True)
class _Area:
    name: str
    pages: list[int] = field(default_factory=list)
    drawings: list[_Drawing] = field(default_factory=list)

    def side(self, role: str) -> list[_Drawing]:
        return [d for d in self.drawings if d.role == role]

    @property
    def latest(self) -> int:
        return max((d.round for d in self.side("vendor")), default=0)


_ROLES: Final = {"architect": "architect", "vendor": "vendor", "brand_prototype": "brand"}
_AREA_NUMBER: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*")


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _inside(box: Box, area: Box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return area[0] <= cx <= area[2] and area[1] <= cy <= area[3]


def _size(box: Box) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _areas(page_map: Mapping[str, Any] | None) -> list[_Area]:
    """The set's areas from GV's typed headings (as `vendor_consistency`): an area starts on a
    page that names it and runs on the pages carried from it (`area_from_page`)."""
    areas: dict[tuple[str, int], _Area] = {}
    pages = page_map.get("pages") if isinstance(page_map, Mapping) else None
    for entry in pages or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        name = entry.get("area")
        if entry.get("skip") or not isinstance(name, str) or not name.strip():
            continue
        file, page = str(entry.get("file")), int(entry["page"])
        start = entry.get("area_from_page")
        key = (file, int(start) if isinstance(start, int) else page)
        area = areas.setdefault(key, _Area(_AREA_NUMBER.sub("", name).strip() or name.strip()))
        area.pages.append(page)
        for half in entry.get("halves") or []:
            if not isinstance(half, Mapping):
                continue
            role, box = _ROLES.get(str(half.get("role"))), _box(half.get("box"))
            if role is None or box is None:
                continue
            number = half.get("round")
            round_ = int(number) if isinstance(number, int) and number > 0 else 1
            area.drawings.append(_Drawing(file, page, role, round_ if role == "vendor" else 0, box))
    return [areas[key] for key in sorted(areas)]


def _fill(areas: Sequence[_Area], dims: Mapping[str, Any] | None) -> None:
    """Each drawing's printed phrases, codes and dimension values (`dims.json`): a phrase belongs
    to the smallest drawing whose box holds its middle."""
    by_page: dict[tuple[str, int], list[_Drawing]] = {}
    for area in areas:
        for each in area.drawings:
            by_page.setdefault((each.file, each.page), []).append(each)
    for entry in (dims or {}).get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        drawings = by_page.get((str(entry.get("file")), int(entry.get("page") or 0)))
        if not drawings:
            continue

        def owner(box: Box, drawings: list[_Drawing] = drawings) -> _Drawing | None:
            holding = [d for d in drawings if _inside(box, d.box)]
            return min(holding, key=lambda d: _size(d.box)) if holding else None

        for phrase in entry.get("phrases") or []:
            box = _box(phrase.get("box")) if isinstance(phrase, Mapping) else None
            text = str(phrase.get("text") or "") if isinstance(phrase, Mapping) else ""
            drawing = owner(box) if box is not None else None
            if drawing is None or box is None or not text.strip():
                continue
            drawing.phrases.append((text, box))
            for printed, family, key in tag_codes(text):
                drawing.codes.append(Code(printed, family, key, text, box))
        for label in entry.get("labels") or []:
            box = _box(label.get("box")) if isinstance(label, Mapping) else None
            drawing = owner(box) if box is not None else None
            if drawing is None or not isinstance(label, Mapping):
                continue
            try:
                inches = Decimal(str(label.get("inches")))
            except (InvalidOperation, ValueError):
                continue
            if inches >= _SHARED_DIM_MIN_IN:
                drawing.dims.add(inches)


# ---- the result ----------------------------------------------------------------------------------


@dataclass(slots=True)
class ItemsResult:
    """What the check found in one set."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    """Chat findings (CONTRACT2 shape, without ids): Wrong items code proved."""
    unknowns: list[dict[str, Any]] = field(default_factory=list)
    """"Not compared" items (`chat_unknowns` shape, without ids, `scope: set`)."""
    areas: int = 0
    """Areas with an architect and a vendor drawing."""
    paired: int = 0
    """Of them, areas whose pairing code proved."""
    compared: int = 0
    """Architect codes compared (in a product's families)."""
    matched: int = 0
    """Of them, printed on the vendor's sheets too."""
    left_out: dict[str, int] = field(default_factory=dict)
    """Architect codes not compared: `other_trades`, `unknown_family`."""
    notes_open: int = 0
    """Architect notes naming a millwork part no vendor sheet names."""
    seconds: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "findings": len(self.findings),
            "unknowns": len(self.unknowns),
            "areas": self.areas,
            "paired": self.paired,
            "compared": self.compared,
            "matched": self.matched,
            "left_out": dict(self.left_out),
            "notes_open": self.notes_open,
            "seconds": self.seconds,
        }


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def summary_line(result: ItemsResult) -> str:
    """One plain line for the full check's summary."""
    if not result.areas:
        return "Architect's finishes (by code, $0): no area has both drawings to compare."
    bits = [
        f"{_plural(result.compared, 'architect finish code', 'architect finish codes')} compared "
        f"in {_plural(result.paired, 'area', 'areas')}"
        + (f" ({result.areas - result.paired} not paired)" if result.paired < result.areas else "")
    ]
    wrong = len(result.findings)
    bits.append(
        _plural(wrong, "missed or changed finish", "missed or changed finishes")
        if wrong
        else "none missed or changed"
    )
    open_items = len(result.unknowns)
    if open_items:
        bits.append(_plural(open_items, "item", "items") + " not compared (see Not compared)")
    return "Architect's finishes (by code, $0): " + "; ".join(bits) + "."


# ---- the check -----------------------------------------------------------------------------------


def _unknown(page: int, what: str, why: str, needs: str) -> dict[str, Any]:
    return {"page": page, "what": what, "why": why, "needs": needs, "scope": "set"}


def _box_dict(drawing: _Drawing, box: Box) -> dict[str, Any]:
    x0, y0, x1, y1 = box
    return {"page": drawing.page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": drawing.file}


def _codes_words(codes: Iterable[Code]) -> str:
    return ", ".join(dict.fromkeys(code.text for code in codes))


def _pages_words(drawings: Iterable[_Drawing]) -> str:
    return ", ".join(dict.fromkeys(d.where() for d in drawings))


def _pairing(area: _Area, keys: set[str]) -> str | None:
    """How code proved the architect's and the vendor's drawings show the same thing (plain
    words), else `None`: a finish code both print, or `MIN_SHARED_DIMS` printed dimensions."""
    architect, vendor = area.side("architect"), area.side("vendor")
    shared = set().union(*(d.code_keys() for d in architect)) & set().union(
        *(d.code_keys() for d in vendor)
    )
    printed = sorted({c.text for d in architect for c in d.codes if c.key in shared & keys})
    if printed:
        return f"both sides print {', '.join(printed)}"
    shared_dims = set().union(*(d.dims for d in architect)) & set().union(*(d.dims for d in vendor))
    if len(shared_dims) >= MIN_SHARED_DIMS:
        return f"both sides print {len(shared_dims)} of the same dimensions"
    return None


def _count(key: str, text: str) -> int:
    return sum(1 for _printed, _family, found in tag_codes(text) if found == key)


def _elsewhere_on_pages(
    key: str,
    pages: Iterable[tuple[str, int]],
    expected: Mapping[tuple[str, int], int],
    texts: Mapping[tuple[str, int], str] | None,
) -> list[int]:
    """The pages whose whole text (every colour) prints the code more often than the drawings
    code read account for: a copy code did not place (coloured text, outside the drawings)."""
    if texts is None:
        return []
    return sorted(
        {
            page
            for file, page in pages
            if _count(key, texts.get((file, page), "")) > expected.get((file, page), 0)
        }
    )


def _expected(key: str, drawings: Iterable[_Drawing]) -> dict[tuple[str, int], int]:
    out: dict[tuple[str, int], int] = {}
    for drawing in drawings:
        found = sum(_count(key, text) for text, _box in drawing.phrases)
        place = (drawing.file, drawing.page)
        out[place] = out.get(place, 0) + found
    return out


def _says(drawings: Iterable[_Drawing], pattern: re.Pattern[str]) -> str | None:
    """The first phrase of these drawings the pattern finds."""
    for drawing in drawings:
        for text, _box in drawing.phrases:
            if pattern.search(text):
                return text
    return None


def _by_others(drawings: Sequence[_Drawing], product: Product) -> str | None:
    """A vendor phrase saying this product is supplied by GV or others (`STONE TOP by GV`)."""
    words = _MATERIAL_WORDS.get(product.id)
    for drawing in drawings:
        lines = [text for text, _box in drawing.phrases]
        for at, text in enumerate(lines):
            # A callout often breaks its line: "(SL-2) SLAB TOP / by GV".
            joined = " ".join(lines[at : at + 2])
            names = words is not None and words.search(joined)
            if names and _BY_OTHERS.search(joined):
                return " ".join(joined.split())
    return None


@dataclass(slots=True)
class _Context:
    area: _Area
    product: Product
    pairing: str
    texts: Mapping[tuple[str, int], str] | None
    vendor_set: Sequence[_Drawing]
    result: ItemsResult


def _absence(ctx: _Context, key: str) -> str | None:
    """Why the code's absence on the area's vendor sheets is not proved, or `None` when it is."""
    area = ctx.area
    vendors = area.side("vendor")
    blind = [d for d in vendors if not d.readable]
    if blind:
        return f"the vendor's sheet {_pages_words(blind)} has no readable text"
    pages = {(d.file, d.page) for d in vendors}
    others = [d for d in area.drawings if (d.file, d.page) in pages and d.role != "vendor"]
    extra = _elsewhere_on_pages(key, pages, _expected(key, others), ctx.texts)
    if extra:
        return f"page {', '.join(str(p) for p in extra)} prints it outside the drawings code read"
    return None


def _missing(ctx: _Context, code: Code, drawing: _Drawing, same: list[Code]) -> dict[str, Any]:
    """A Wrong: the architect's code is on no vendor sheet of the area (`same`: the vendor's
    newest-round codes of the same family, for the words)."""
    area, product = ctx.area, ctx.product
    vendors = area.side("vendor")
    latest = [d for d in vendors if d.round == area.latest]
    page = min(d.page for d in latest)
    one_each = (
        len({c.key for c in same}) == 1
        and len({c.key for d in area.side("architect") for c in d.codes if c.family == code.family})
        == 1
    )
    searched = _pages_words(vendors)
    why = (
        f"The architect's drawing of {area.name} ({drawing.where()}) prints {code.text}; no vendor "
        f"sheet of this area prints it ({searched}), read by code from the drawings' own text."
    )
    if same:
        why += f" The vendor tags this family {_codes_words(same)} instead."
    why += f" Code paired the two: {ctx.pairing}."
    finding: dict[str, Any] = {
        "page": page,
        "item": (
            f"{area.name}: {product.label.lower()} finish {code.text} "
            + ("changed" if one_each else "missed")
        ),
        "category": product.id,
        "architect": code.text,
        "vendor": (
            f"{same[0].text} (no {code.text} on any vendor sheet)"
            if one_each
            else "not on the vendor's sheets"
        ),
        "status": "different",
        "why": why,
        "verified": False,
        "source": "architect",
        "arch_box": _box_dict(drawing, code.box),
        "paired_by": "code",
        "created_by": CREATED_BY,
        "text_proof": {
            "kind": "changed" if one_each else "missed",
            "code": code.key,
            "architect": [{"page": drawing.page, "text": code.text, "phrase": code.phrase}],
            "vendor": [{"page": d.page, "round": d.round} for d in vendors],
            "vendor_codes": [c.text for c in same],
            "pairing": ctx.pairing,
        },
    }
    if drawing.page != page:
        finding["vs_page"] = drawing.page
    if one_each:
        holder = next(d for d in latest if same[0] in d.codes)
        finding["vendor_box"] = _box_dict(holder, same[0].box)
    return finding


def _added(ctx: _Context, code: Code, drawing: _Drawing, theirs: list[Code]) -> dict[str, Any]:
    """A Wrong: the vendor's code in a family the architect uses here, on no architect drawing."""
    area, product = ctx.area, ctx.product
    architect = area.side("architect")
    why = (
        f"The vendor's sheet {drawing.where()} of {area.name} prints {code.text}; the architect's "
        f"drawings of this area ({_pages_words(architect)}) tag this family "
        f"{_codes_words(theirs)} and never {code.text}, read by code from the drawings' own text."
    )
    wanted = {c.key for c in theirs}
    earlier = [
        (d, c)
        for d in area.side("vendor")
        if d.round < drawing.round
        for c in d.codes
        if c.key in wanted
    ]
    if earlier:
        where = _pages_words(d for d, _c in earlier)
        why += f" An earlier round printed {_codes_words(c for _d, c in earlier)} ({where})."
    why += f" Code paired the two: {ctx.pairing}."
    finding: dict[str, Any] = {
        "page": drawing.page,
        "item": f"{area.name}: {product.label.lower()} finish {code.text} not on the architect",
        "category": product.id,
        "architect": f"not on the architect's drawings (they tag {_codes_words(theirs)})",
        "vendor": code.text,
        "status": "different",
        "why": why,
        "verified": False,
        "source": "architect",
        "vendor_box": _box_dict(drawing, code.box),
        "paired_by": "code",
        "created_by": CREATED_BY,
        "text_proof": {
            "kind": "added",
            "code": code.key,
            "vendor": [{"page": drawing.page, "text": code.text, "phrase": code.phrase}],
            "architect": [{"page": d.page} for d in architect],
            "architect_codes": [c.text for c in theirs],
            "pairing": ctx.pairing,
        },
    }
    first = min(d.page for d in architect)
    if first != drawing.page:
        finding["vs_page"] = first
    return finding


def _compare(ctx: _Context) -> None:
    """One area and one product: the architect's codes of the product's families against the
    vendor's (see the module's rules)."""
    area, product, result = ctx.area, ctx.product, ctx.result
    families = set(product.tag_families)
    architect, vendors = area.side("architect"), area.side("vendor")
    latest = [d for d in vendors if d.round == area.latest]
    theirs = [(d, c) for d in architect for c in d.codes if c.family in families]
    ours_all = {c.key for d in vendors for c in d.codes}
    ours_latest = [c for d in latest for c in d.codes if c.family in families]
    vendor_families = {c.family for c in ours_latest}
    prototype = {c.key for d in area.side("brand") for c in d.codes}
    seen: set[str] = set()
    changed: set[str] = set()
    for drawing, code in theirs:
        if code.key in seen:
            continue
        seen.add(code.key)
        result.compared += 1
        if code.key in ours_all:
            result.matched += 1
            continue
        if code.family not in vendor_families:
            if ours_latest:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: architect's {code.text} against the vendor's "
                        f"{_codes_words(ours_latest)}",
                        "not_comparable",
                        "a printed legend, or the vendor's confirmation, that their code is the "
                        f"architect's {code.text} (the two use different tag systems)",
                    )
                )
                continue
            supplied = _by_others(latest, product)
            if supplied is not None:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: architect's {code.text}; the vendor's sheet says "
                        f'"{supplied[:60]}"',
                        "not_comparable",
                        f"whoever supplies the {product.plural.lower()} confirms {code.text}",
                    )
                )
                continue
            named = _says(latest, _MATERIAL_WORDS.get(product.id, re.compile(r"(?!)")))
            if named is not None:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: architect's {code.text}; the vendor names the material "
                        f'in words ("{named[:60]}")',
                        "not_comparable",
                        f"the vendor's confirmation that it is {code.text}",
                    )
                )
                continue
            other = [d for d in ctx.vendor_set if code.key in d.code_keys() and d not in vendors]
            if other:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: architect's {code.text} is on another vendor sheet "
                        f"({_pages_words(other[:3])}), not on this area's",
                        "not_comparable",
                        f"the vendor's confirmation that {code.text} applies here",
                    )
                )
                continue
        why_not = _absence(ctx, code.key)
        if why_not is not None:
            result.unknowns.append(
                _unknown(
                    drawing.page,
                    f"{area.name}: architect's {code.text} not found on the vendor's sheets",
                    "unreadable",
                    f"a person checks the vendor's sheets for {code.text}: {why_not}",
                )
            )
            continue
        same = [c for c in ours_latest if c.family == code.family]
        finding = _missing(ctx, code, drawing, same)
        if finding["text_proof"]["kind"] == "changed":
            changed |= {c.key for c in same}
        result.findings.append(finding)
    # Added: the vendor's newest-round codes in a family the architect uses here.
    architect_keys = {c.key for d in architect for c in d.codes}
    architect_families = {c.family for _d, c in theirs}
    reported: set[str] = set()
    for drawing in latest:
        for code in drawing.codes:
            if (
                code.family not in families
                or code.family not in architect_families
                or code.key in architect_keys
                or code.key in reported
                or code.key in changed
            ):
                continue
            reported.add(code.key)
            if code.key in prototype:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: vendor's {code.text} is on the brand prototype only",
                        "not_comparable",
                        f"the architect's confirmation that {code.text} applies (the prototype "
                        "is reference only)",
                    )
                )
                continue
            pages = {(d.file, d.page) for d in architect}
            others = [
                d for d in area.drawings if (d.file, d.page) in pages and d.role != "architect"
            ]
            extra = _elsewhere_on_pages(code.key, pages, _expected(code.key, others), ctx.texts)
            if extra:
                result.unknowns.append(
                    _unknown(
                        drawing.page,
                        f"{area.name}: vendor's {code.text} may be on the architect's page "
                        f"{', '.join(str(p) for p in extra)}",
                        "unreadable",
                        f"a person checks the architect's drawing for {code.text}",
                    )
                )
                continue
            theirs_family = [c for _d, c in theirs if c.family == code.family]
            result.findings.append(_added(ctx, code, drawing, theirs_family))


def _notes(lines: Sequence[tuple[str, Box]]) -> list[tuple[str, Box]]:
    """A drawing's printed lines joined into notes: a line joins the note above it when it starts
    (or ends) where that note does and sits right under it."""
    notes: list[tuple[list[str], Box]] = []
    for text, box in sorted(lines, key=lambda line: (line[1][1], line[1][0])):
        height = box[3] - box[1]
        for words, (x0, y0, x1, y1) in reversed(notes):
            gap = box[1] - y1
            aligned = abs(box[0] - x0) <= _NOTE_ALIGN_PT or abs(box[2] - x1) <= _NOTE_ALIGN_PT
            if aligned and -0.5 * height <= gap <= _NOTE_GAP * height:
                words.append(text)
                notes.remove((words, (x0, y0, x1, y1)))
                notes.append((words, (min(x0, box[0]), y0, max(x1, box[2]), box[3])))
                break
        else:
            notes.append(([text], box))
    return [(" ".join(" ".join(words).split()), box) for words, box in notes]


def _open_notes(area: _Area, texts: Mapping[tuple[str, int], str] | None) -> list[dict[str, Any]]:
    """The architect's notes that name a millwork part no vendor sheet of the area names."""
    vendors = area.side("vendor")
    if not vendors:
        return []
    vendor_text = " ".join(text for d in vendors for text, _box in d.phrases)
    vendor_only = {(d.file, d.page) for d in vendors} - {
        (d.file, d.page) for d in area.drawings if d.role != "vendor"
    }
    if texts is not None:
        vendor_text += " " + " ".join(texts.get(place, "") for place in sorted(vendor_only))
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for drawing in area.side("architect"):
        for note, _box in _notes(drawing.phrases):
            if len(note.split()) < 3 or _BY_OTHERS.search(note) or _TITLE.search(note):
                continue
            parts = [name for name, asked, _named in _PARTS if asked.search(note)]
            lacking = [
                name
                for name, asked, named in _PARTS
                if name in parts and not named.search(vendor_text)
            ]
            if not lacking or note.upper() in seen:
                continue
            seen.add(note.upper())
            short = note if len(note) <= 90 else note[:89].rstrip() + "…"
            out.append(
                _unknown(
                    drawing.page,
                    f'{area.name}: architect\'s note "{short}" ({drawing.where()})',
                    "not_comparable",
                    f"a look at the vendor's drawing for the {' and '.join(lacking)}: its sheets "
                    f"of this area ({_pages_words(vendors)}) print no word for them, and text "
                    "alone cannot show whether they are drawn",
                )
            )
    return out[:_NOTES_PER_AREA]


def _prototype_only(area: _Area, families: set[str]) -> list[dict[str, Any]]:
    """Codes only the brand prototype prints for a product the architect's drawings of the area
    tag nothing of, and no vendor sheet prints: the architect gives no information (R1: the
    prototype is reference only), so they are Not compared, with the reason."""
    tagged = {c.family for d in area.side("architect") for c in d.codes if c.family in families}
    products = {f: p for p in registry().products for f in p.tag_families}
    architect_products = {products[f].id for f in tagged if f in products}
    vendor_keys = {c.key for d in area.side("vendor") for c in d.codes}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for drawing in area.side("brand"):
        for code in drawing.codes:
            product = products.get(code.family)
            if (
                code.family not in families
                or product is None
                or product.id in architect_products
                or code.key in vendor_keys
                or code.key in seen
            ):
                continue
            seen.add(code.key)
            out.append(
                _unknown(
                    drawing.page,
                    f"{area.name}: {code.text} is on the brand prototype only ({drawing.where()})",
                    "not_comparable",
                    f"the architect's {product.label.lower()} finish: the architect's drawings of "
                    "this area tag none, and the prototype is reference only",
                )
            )
    return out


def _products(products: Sequence[str] | None) -> list[Product]:
    """The products in scope with tag families: the given active ids, else every active one."""
    known = registry()
    active = [p for p in known.active() if p.tag_families]
    wanted = {pid for pid in products or () if known.get(pid) is not None}
    return [p for p in active if p.id in wanted] if wanted else active


def check_set(
    dims: Mapping[str, Any] | None,
    page_map: Mapping[str, Any] | None,
    *,
    texts: Mapping[tuple[str, int], str] | None = None,
    products: Sequence[str] | None = None,
    clock: Callable[[], float] = monotonic,
) -> ItemsResult:
    """The check over one set's printed phrases (`dims.json`) and page map (see the module).
    `texts`: each page's whole text (every colour), for the absence proof; `products`: the
    products in scope (none: every active one)."""
    started = clock()
    result = ItemsResult()
    areas = _areas(page_map)
    _fill(areas, dims)
    scope = _products(products)
    families = {f for p in scope for f in p.tag_families}
    all_families = {f for p in registry().products for f in p.tag_families}
    vendor_set = [d for a in areas for d in a.side("vendor")]
    left_out = {"other_trades": 0, "unknown_family": 0}
    for area in areas:
        architect, vendors = area.side("architect"), area.side("vendor")
        if not architect or not vendors:
            continue
        result.areas += 1
        for code in {c.key: c for d in architect for c in d.codes if "-" in c.text}.values():
            if code.family in OTHER_TRADE_FAMILIES:
                left_out["other_trades"] += 1
            elif code.family not in all_families:
                left_out["unknown_family"] += 1
        result.unknowns += _prototype_only(area, families)
        in_scope = {c.key for d in area.drawings for c in d.codes if c.family in families}
        pairing = _pairing(area, in_scope)
        if pairing is None:
            asked = sorted({c.text for d in architect for c in d.codes if c.family in families})
            if asked:
                result.unknowns.append(
                    _unknown(
                        min(d.page for d in architect),
                        f"{area.name}: architect's {', '.join(asked[:6])} not compared",
                        "pairing_uncertain",
                        "code could not prove the vendor's sheets show the same items (no "
                        "finish code or dimensions on both); a person compares them",
                    )
                )
            open_notes = _open_notes(area, texts)
            result.notes_open += len(open_notes)
            result.unknowns += open_notes
            continue
        result.paired += 1
        for product in scope:
            _compare(_Context(area, product, pairing, texts, vendor_set, result))
        open_notes = _open_notes(area, texts)
        result.notes_open += len(open_notes)
        result.unknowns += open_notes
    result.left_out = left_out
    result.findings = result.findings[:MAX_FINDINGS]
    result.unknowns = sorted(result.unknowns, key=lambda u: u["page"])[:MAX_UNKNOWNS]
    result.seconds = round(clock() - started, 3)
    return result


# ---- the set pack --------------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_pack(
    folder: Path | None, *, products: Sequence[str] | None = None, read_texts: bool = True
) -> ItemsResult | None:
    """The check over a set pack's `dims.json` (its phrases) and page map (with the reviewer's
    plan-check answers laid over it); the pages' whole text from `page_texts.json` (read once per
    set by the vendor consistency check). None when the pack has no phrases yet."""
    if folder is None:
        return None
    dims = _read_json(folder / "dims.json")
    if not isinstance(dims, Mapping) or not isinstance(dims.get("pages"), list):
        return None
    plan = import_module("app.review.assistant.full_check_plan")
    page_map = plan.read_page_map(folder)
    files = [str(f) for f in (page_map or {}).get("files") or dims.get("files") or []]
    texts = None
    if read_texts and files:
        consistency = import_module("app.review.assistant.vendor_consistency")
        texts = consistency.page_texts(folder, files) or None
    return check_set(dims, page_map, texts=texts, products=products)


# ---- the full check's hook -----------------------------------------------------------------------


def _already(finding: Mapping[str, Any], live: Sequence[Mapping[str, Any]]) -> bool:
    """The answer already has this item: a finding on one of its pages naming its code."""
    proof = finding.get("text_proof") or {}
    key = str(proof.get("code") or "")
    pages = {finding.get("page"), finding.get("vs_page")} - {None}
    for other in live:
        if not pages & ({other.get("page"), other.get("vs_page")} - {None}):
            continue
        words = " ".join(str(other.get(name) or "") for name in ("item", "architect", "vendor"))
        if key and any(found == key for _printed, _family, found in tag_codes(words)):
            return True
    return False


def add_to_answer(engine: Any) -> ItemsResult | None:
    """The engine's hook (a full check, at its finish, $0): the check over the set pack; its Wrong
    items added through the finding book (the same checks and trust rule), unless the answer
    already has them; its Not compared items kept with the set's other gaps
    (`engine.consistency_gaps`); one line for the summary (`engine.items_line`)."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return None
    workspace = engine.tools.workspace
    products = getattr(engine.settings, "products", None)
    result = check_pack(workspace.set_pack, products=products)
    if result is None:
        return None
    book = engine.book
    with engine.finding_lock:
        live = book.live()
        for finding in result.findings:
            if _already(finding, live):
                continue
            added = book.add_code_finding(finding, created_by=CREATED_BY)
            if added is not None:
                engine._show_finding(added)
                live.append(added)
    engine.consistency_gaps = [*getattr(engine, "consistency_gaps", []), *result.unknowns]
    engine.items_line = summary_line(result)
    return result
