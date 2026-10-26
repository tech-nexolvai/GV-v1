"""Claim rules: GV's settled rules and code's own facts on every open claim (2026-10-25).

The client-meeting job's answer key found four kinds of claim that are wrong by GV's rules or by
what the drawings themselves print, whoever raised them (the whole-drawing look or a page helper).
This step runs in the escalation before the ladder (`escalation._before_ladder`), on every item
that compares the vendor with the reference and is still open (never on one the reviewer decided
or one already recorded). It only ever takes a claim away or makes it Not compared: it never makes
anything red or a Likely problem.

1. **Notches** (DECIDED by GV in the client meeting, 2026-10-23): unusual notches in a countertop
   are cut on site and ignored in the comparison: never red, never a Likely problem. An item about
   a notch is **withdrawn**; its reason says GV's rule (the note the reviewer sees when it drops).
2. **Unit counts across the regular and reversed sheets**: a vendor that draws one item on two
   sheets ("REGULAR UNITS" and "REVERSED UNITS") lists each sheet's own units, while the reference
   lists the item's total ("AT-BV // AT-BV-Reverse, 4 units"). Code reads both lists from the
   pages' text, adds the vendor's two sheets (the neighbouring sheet with the other heading whose
   units all belong to the reference's list) and compares per unit type. Every type the same: the
   count claim is **withdrawn** ("2 + 2 = 4"). Code cannot add them up (no other sheet, two
   candidates, a unit code cannot place): **Not compared**. Different per type after adding up,
   but the same total, every vendor unit on the reference's list and the regular and reversed
   counts fitting it (2026-10-26: the vendor names "X-R 3" where the reference splits "X-Reverse
   2, Y-Reverse 1"): every unit on the list uses this one top, so the claim is **withdrawn**.
   Else unchanged.
3. **A wall outline is not the countertop**: code reads the reference's line work in the claim's
   spot (`OutlineReader`): an L-shaped outline drawn at the wall's weight (the heaviest line on
   the page) or solid-filled (a wall's poche) is a wall or pony wall, never the top: the claim
   that the reference's top is L-shaped is **withdrawn**. An L drawn lighter (a top), or nothing
   code can tell: unchanged here; the like-for-like step (`escalation._wall_return_claim`, fix 3)
   has already dropped it where the reference's top sizes stop at a wall, and keeps it where sizes
   both ways may be a real L's two legs.
4. **A side splash is not a mitred piece**: the vendor's legend names its mitred edge ("M1.5 =
   MITERED EDGE": the edge built up to 1 1/2"). A claim about mitred pieces stands only when the
   piece's own size is that build-up; a 4" piece standing at a wall end is a side splash. When the
   legend gives no mitre code, code cannot tell them apart: **withdrawn** (no claim).
5. **Which end, on a unit drawn both ways round** (2026-10-26): a whole-drawing look's claim that
   something sits at the other end or side (its own words, not a size code paired) is
   **withdrawn** when code knows the unit is drawn both ways round and the claim is the same once
   the vendor's side is flipped left to right (its left / right named the other way, a size from
   an end the same size from the flipped end; a size claim's numbers all the same). Code knows a
   unit is reversed from (a) the vendor's neighbouring sheet with the other REGULAR / REVERSED
   UNITS heading drawn as its mirror image (`mirrored`: the sizes at mirrored places; when the
   vendor has such a sheet, only this proof counts: two sheets drawn alike may hold a real
   mistake), or (b) for a vendor sheet with no such neighbour, a title or unit list naming a
   reversed unit ("X-R", "X-Reverse", REVERSED, OPP. HAND) on the vendor's page or the
   reference's. Nothing tells code: the claim stays; never a guess.

Pure code except the outline reader (pdfium, under the one lock; its pages kept per process).
Verification: `tests/review/assistant/test_claim_rules.py` (made-up drawings and values).
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from importlib import import_module
from itertools import pairwise
from pathlib import Path
from typing import Any, Final, Literal

from app.review.assistant import likely_problems, proof
from app.review.assistant.classification import internal

__all__ = [
    "FIELD",
    "Change",
    "OutlineReader",
    "changes",
    "held",
    "mirrored",
    "mitre_build_up",
    "notch_item",
    "reversed_unit",
    "unit_counts",
]

_log = logging.getLogger(__name__)

#: The finding's field this step writes: {"rule", "outcome", "why"}.
FIELD: Final = "claim_rule"

type Outcome = Literal["withdrawn", "not_compared"]
type Rule = Literal["notch", "unit_counts", "wall_outline", "side_splash", "reversed_unit"]
type Box = tuple[float, float, float, float]
#: (file, page, box) -> "wall", "top", or None when code cannot tell.
type Outlines = Callable[[str, int, Box], str | None]


@dataclass(frozen=True, slots=True)
class Change:
    """What this step does to one item."""

    outcome: Outcome
    rule: Rule
    why: str

    def record(self) -> dict[str, str]:
        return {"rule": self.rule, "outcome": self.outcome, "why": self.why}


def held(finding: Mapping[str, Any]) -> str | None:
    """Why this step made the item Not compared (never a Likely problem then), else `None`."""
    record = finding.get(FIELD)
    if isinstance(record, Mapping) and record.get("outcome") == "not_compared":
        return str(record.get("why") or "") or None
    return None


# ---- which items -----------------------------------------------------------------------------


def _open(finding: Mapping[str, Any]) -> bool:
    """An item this step may change: compared with the reference, still open, nobody decided."""
    if internal(finding) or finding.get("decision") or finding.get("record_id"):
        return False
    if finding.get(FIELD):
        return False  # already done (a second escalation pass)
    one_card = import_module("app.review.assistant.one_card")
    if not one_card.compares_architect(finding):
        return False
    return proof.proof_group(finding) not in ("right", "dismissed")


def _proved_wrong(finding: Mapping[str, Any]) -> bool:
    return proof.proof_group(finding) == "clouded" or (
        finding.get("status") == "different" and proof.code_proved(finding)
    )


def _flat(value: object) -> str:
    return " ".join(str(value or "").split())


def _kind(finding: Mapping[str, Any]) -> str | None:
    mark = finding.get(likely_problems.FIELD)
    return str(mark.get("kind")) if isinstance(mark, Mapping) and mark.get("kind") else None


# ---- 1. notches -------------------------------------------------------------------------------

_NOTCH: Final = re.compile(r"\bnotch(?:es|ed|ing)?\b", re.IGNORECASE)
NOTCH_WHY: Final = (
    "GV's rule (client meeting, 2026-10-23): unusual notches in a countertop are cut on site, so "
    "they are not compared with the reference"
)


#: An item that also names another subject (a sink, a tag, a material, a splash) is never taken
#: away by a rule about its shape: the other subject would go with it.
_OTHER_SUBJECT: Final = re.compile(
    r"\b(?:sinks?|faucets?|models?|materials?|finish(?:es)?|tags?|outlets?|cut-?outs?|"
    r"field cut|splash(?:es)?)\b",
    re.IGNORECASE,
)


def _only_shape(finding: Mapping[str, Any]) -> bool:
    return not _OTHER_SUBJECT.search(_flat(finding.get("item")))


#: A size of something other than the notch itself ("Countertop depth at the column notch"): the
#: notch's own size ("notch depth") is part of the notch.
_OTHER_SIZE: Final = re.compile(
    r"(?<!notch )\b(?:depth|width|length|height|overhang|size|thickness)\b", re.IGNORECASE
)


def notch_item(finding: Mapping[str, Any]) -> bool:
    """The item is about a notch (its own name says so) and nothing else: no other subject, no
    size of anything but the notch."""
    item = _flat(finding.get("item"))
    return bool(_NOTCH.search(item)) and _only_shape(finding) and not _OTHER_SIZE.search(item)


# ---- 2. unit counts ----------------------------------------------------------------------------

_COUNT_ITEM: Final = re.compile(
    r"\bunits?\b.*\b(?:quantit\w*|counts?|qty)\b|\b(?:quantit\w*|counts?|qty)\b.*\bunits?\b",
    re.IGNORECASE,
)
_CODE: Final = r"[A-Z0-9]+(?:-[A-Za-z0-9]+)+"
#: A vendor's unit line: "AT-BV - 2 UNITS", "AS2-PH: 1 UNIT".
_VENDOR_LINE: Final = re.compile(
    rf"^\s*({_CODE})\s*[-–:]\s*(\d{{1,4}})\s*UNITS?\.?\s*$", re.IGNORECASE
)
#: The reference's unit line: "AT-BV // AT-BV-Reverse (ADA), 4 units;".
_REF_LINE: Final = re.compile(r"^\s*(.+?),\s*(\d{1,4})\s*units?\s*;?\s*$", re.IGNORECASE)
_HEADING: Final = re.compile(r"^\s*(REGULAR|REVERSED?)\s+UNITS\s*:?\s*$", re.IGNORECASE)


def _norm(name: str) -> str:
    found = re.sub(r"\(.*?\)", "", name).strip().upper()
    found = re.sub(r"\s+", "", found)
    return re.sub(r"-REVERSED?$", "-R", found)


def _lines(text: str) -> list[str]:
    return [line for line in re.split(r"[\r\n]+", text or "") if line.strip()]


def _vendor_units(text: str) -> tuple[dict[str, int], str | None]:
    """A vendor sheet's unit list {code: count} and its heading ("regular" / "reversed")."""
    units: dict[str, int] = {}
    heading: str | None = None
    for line in _lines(text):
        head = _HEADING.match(line)
        if head:
            heading = "regular" if head.group(1).upper() == "REGULAR" else "reversed"
            continue
        found = _VENDOR_LINE.match(line)
        if found:
            name = _norm(found.group(1))
            units[name] = units.get(name, 0) + int(found.group(2))
    return units, heading


def _reference_units(text: str) -> list[tuple[frozenset[str], int]]:
    """The reference's unit list: each entry's unit codes and its count."""
    out: list[tuple[frozenset[str], int]] = []
    for line in _lines(text):
        found = _REF_LINE.match(line)
        if not found:
            continue
        names = [part.strip() for part in found.group(1).split("//")]
        codes = [_norm(n) for n in names if re.fullmatch(rf"{_CODE}(?:\s*\(.*\))?", n.strip())]
        if codes and len(codes) == len(names):
            out.append((frozenset(codes), int(found.group(2))))
    return out


def _place(
    units: Mapping[str, int], entries: Sequence[tuple[frozenset[str], int]]
) -> dict[int, int] | None:
    """The vendor's counts per reference entry, or None when a unit belongs to no entry."""
    sums: dict[int, int] = {}
    for name, count in units.items():
        at = [i for i, (codes, _) in enumerate(entries) if name in codes]
        if len(at) != 1:
            return None
        sums[at[0]] = sums.get(at[0], 0) + count
    return sums


def _hand(name: str) -> str:
    """A unit code's hand: "reversed" for an "-R" code (`_norm` writes "-Reverse" so), else
    "regular"."""
    return "reversed" if name.endswith("-R") else "regular"


def _hands_fit(hands: Mapping[str, int], entries: Sequence[tuple[frozenset[str], int]]) -> bool:
    """The vendor's regular and reversed counts fit the reference's list: each hand at least the
    reference's entries of that hand only, and at most those plus the entries that hold both
    ("X // X-Reverse")."""
    for hand in ("regular", "reversed"):
        only = sum(c for codes, c in entries if {_hand(n) for n in codes} == {hand})
        both = sum(c for codes, c in entries if len({_hand(n) for n in codes}) == 2)
        if not only <= hands.get(hand, 0) <= only + both:
            return False
    return True


def _sibling_sheet(page: int, texts: Mapping[int, str]) -> tuple[str, list[int]] | None:
    """The vendor page's heading ("regular" / "reversed") and its neighbouring sheets with the
    other heading (the same item drawn the other way round), or None when the page has no
    REGULAR / REVERSED UNITS heading."""
    _, heading = _vendor_units(texts.get(page, ""))
    if heading is None:
        return None
    other = "reversed" if heading == "regular" else "regular"
    near = [
        p
        for p in (page - 1, page + 1)
        if (found := _vendor_units(texts.get(p, "")))[1] == other and found[0]
    ]
    return heading, near


def unit_counts(
    page: int,
    reference: str,
    texts: Mapping[int, str],
) -> tuple[str, str] | None:
    """The unit-count rule on vendor `page` (`texts`: the vendor file's page texts) against the
    reference page's `reference` text: ("same" | "unsure", why), or None when it does not apply
    or the counts still differ after adding up.

    Per unit type first (#1931); then, when only the unit names differ, the totals: the same
    total, every vendor unit in the reference's list and the regular and reversed counts fitting
    it are the same tops under other unit names ("same")."""
    entries = _reference_units(reference)
    units, heading = _vendor_units(texts.get(page, ""))
    if not entries or not units:
        return None
    sheets = [page]
    hands: Counter[str] = Counter()
    if heading is None:
        for name, count in units.items():
            hands[_hand(name)] += count
    else:
        hands[heading] += sum(units.values())
        other = "reversed" if heading == "regular" else "regular"
        found = []
        for near in (page - 1, page + 1):
            near_units, near_heading = _vendor_units(texts.get(near, ""))
            if near_heading == other and near_units and _place(near_units, entries) is not None:
                found.append((near, near_units))
        if len(found) != 1:
            return (
                "unsure",
                (
                    f"the vendor lists only its {heading} units on page {page}; code could not "
                    "find the one sheet with the other units of this item, so the counts were "
                    "not added up"
                ),
            )
        near, near_units = found[0]
        sheets = sorted([page, near])
        units = dict(Counter(units) + Counter(near_units))
        hands[other] += sum(near_units.values())
    placed = _place(units, entries)
    if placed is None:
        return (
            ("unsure", "the vendor lists a unit type code could not place in the reference's list")
            if heading is not None
            else None
        )
    total = sum(count for _, count in entries)
    pages = " + ".join(f"page {p}" for p in sheets)
    if any(placed.get(i, 0) != count for i, (_, count) in enumerate(entries)):
        if sum(units.values()) != total or not _hands_fit(hands, entries):
            return None
        return (
            "same",
            (
                f"code adds up the vendor's unit lists ({pages}): {total} units, the same total "
                f"as the reference's list, and the regular and reversed units fit that list "
                f"({hands['regular']} regular, {hands['reversed']} reversed). Every unit on the "
                "list uses this one top, so only the unit names differ, not the number of tops"
            ),
        )
    return (
        "same",
        (
            f"code adds up the vendor's unit lists ({pages}) and every unit type matches the "
            f"reference's list ({total} units in all)"
        ),
    )


# ---- 3. a wall outline is not the countertop ------------------------------------------------------

_L_SHAPE: Final = re.compile(r"\bL[- ]?shap(?:e|ed)\b", re.IGNORECASE)


def _l_claim(finding: Mapping[str, Any]) -> bool:
    return bool(_L_SHAPE.search(_flat(finding.get("architect")))) and not _L_SHAPE.search(
        _flat(finding.get("vendor"))
    )


@dataclass(frozen=True, slots=True)
class _Path:
    width: float
    stroked: bool
    filled: bool
    corners: int
    box: Box


#: An L-shaped outline turns at least this many times (a rectangle turns 4).
_L_CORNERS: Final = 6
#: A wall is drawn at least this much heavier than the page's usual line, and near its heaviest.
_WALL_OVER_USUAL: Final = 2.0
_WALL_OF_HEAVIEST: Final = 0.75
#: How far round the claim's spot an outline may reach (points).
_SLACK_PT: Final = 12.0
_KEPT: Final = 32


@dataclass(slots=True)
class OutlineReader:
    """The reference's line work at a spot: "wall" when an L-shaped outline there is drawn at the
    wall's weight or solid-filled, "top" when one is drawn lighter, else None (see the module)."""

    pdf_of: Callable[[str], Path | None]
    _pages: OrderedDict[tuple[Any, ...], tuple[list[_Path], float, float] | None] = field(
        default_factory=OrderedDict
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_pack(cls, pack: Path | None, uploads: Path | None = None) -> OutlineReader:
        numbers = import_module("app.review.assistant.read_number")
        return cls(numbers.reader_for_pack(pack, uploads).pdf_of)

    def __call__(self, file: str, page: int, box: Box) -> str | None:
        pdf = self.pdf_of(file)
        if pdf is None:
            return None
        try:
            stat = pdf.stat()
        except OSError:
            return None
        key = (str(pdf), stat.st_mtime_ns, stat.st_size, page)
        with self._lock:
            known = key in self._pages
            read = self._pages.get(key)
        if not known:
            try:
                read = _read_paths(pdf, page)
            except Exception:  # noqa: BLE001 - a page that cannot be read gives no evidence
                _log.warning("claim rules: line work of page %s not read", page, exc_info=True)
                read = None
            with self._lock:
                self._pages[key] = read
                while len(self._pages) > _KEPT:
                    self._pages.popitem(last=False)
        return _judge(read, box) if read is not None else None


def _read_paths(pdf: Path, page: int) -> tuple[list[_Path], float, float] | None:
    """Every path on the page: its weight, fill, turns and box (top-left page points), with the
    page's usual and heaviest stroke widths. None for a rotated page (its boxes would not match)."""
    guard = import_module("extraction.pdfium_guard")
    raw = guard.pdfium_raw
    paths: list[_Path] = []
    with guard.PDFIUM_LOCK:
        document = guard.pdfium.PdfDocument(str(pdf))
        try:
            sheet = document[page - 1]
            try:
                if sheet.get_rotation():
                    return None
                _, height = sheet.get_size()
                for obj in sheet.get_objects(max_depth=3):
                    if obj.type != raw.FPDF_PAGEOBJ_PATH:
                        continue
                    width = ctypes.c_float()
                    raw.FPDFPageObj_GetStrokeWidth(obj.raw, ctypes.byref(width))
                    fill, stroke = ctypes.c_int(), ctypes.c_int()
                    raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill), ctypes.byref(stroke))
                    left, bottom, right, top = obj.get_bounds()
                    paths.append(
                        _Path(
                            float(width.value),
                            bool(stroke.value),
                            bool(fill.value),
                            _turns(raw, obj.raw),
                            (left, height - top, right, height - bottom),
                        )
                    )
            finally:
                sheet.close()
        finally:
            document.close()
    widths = Counter(round(p.width, 2) for p in paths if p.stroked and p.width > 0)
    if not widths:
        return paths, 0.0, 0.0
    usual = widths.most_common(1)[0][0]
    return paths, float(usual), float(max(widths))


def _turns(raw: Any, path: Any) -> int:
    """The most right-angle turns along one of the path's pieces (curves count as none)."""
    best = 0
    points: list[tuple[float, float]] = []
    closed = False

    def piece() -> int:
        steps: list[str] = []
        ring = points + ([points[0]] if closed and points else [])
        for (x0, y0), (x1, y1) in pairwise(ring):
            dx, dy = abs(x1 - x0), abs(y1 - y0)
            if dx < 0.5 and dy < 0.5:
                continue
            if dx >= 0.5 and dy >= 0.5:
                return 0  # a slanted piece: not an orthogonal outline
            steps.append("h" if dx >= 0.5 else "v")
        if closed and steps and steps[0] == steps[-1] and len(steps) > 1:
            steps = steps[1:]  # the closing piece runs on along the first
        turns = sum(1 for a, b in pairwise(steps) if a != b)
        return turns + (1 if closed and len(steps) > 1 and steps[0] != steps[-1] else 0)

    for at in range(raw.FPDFPath_CountSegments(path)):
        segment = raw.FPDFPath_GetPathSegment(path, at)
        kind = raw.FPDFPathSegment_GetType(segment)
        x, y = ctypes.c_float(), ctypes.c_float()
        raw.FPDFPathSegment_GetPoint(segment, ctypes.byref(x), ctypes.byref(y))
        if kind == raw.FPDF_SEGMENT_MOVETO:
            best = max(best, piece())
            points, closed = [], False
        elif kind == raw.FPDF_SEGMENT_BEZIERTO:
            return 0
        points.append((float(x.value), float(y.value)))
        if raw.FPDFPathSegment_GetClose(segment):
            closed = True
    return max(best, piece())


def _share_inside(inner: Box, outer: Box) -> float:
    w = min(inner[2], outer[2]) - max(inner[0], outer[0])
    h = min(inner[3], outer[3]) - max(inner[1], outer[1])
    area = max((inner[2] - inner[0]) * (inner[3] - inner[1]), 1e-6)
    return max(w, 0.0) * max(h, 0.0) / area


def _judge(read: tuple[list[_Path], float, float], box: Box) -> str | None:
    paths, usual, heaviest = read
    spot = (box[0] - _SLACK_PT, box[1] - _SLACK_PT, box[2] + _SLACK_PT, box[3] + _SLACK_PT)
    wall = top = False
    for path in paths:
        if path.corners < _L_CORNERS or _share_inside(path.box, spot) < 0.6:
            continue
        heavy = (
            path.stroked
            and usual > 0
            and path.width >= _WALL_OVER_USUAL * usual
            and path.width >= _WALL_OF_HEAVIEST * heaviest
        )
        if heavy or path.filled:
            wall = True
        elif path.stroked:
            top = True
    if wall and not top:
        return "wall"
    if top and not wall:
        return "top"
    return None


# ---- 4. a side splash is not a mitred piece -----------------------------------------------------

_MITRE_ITEM: Final = re.compile(r"\bmit(?:re|er)(?:ed|d)?\b", re.IGNORECASE)
_PIECE: Final = re.compile(r"\b(?:pieces?|returns?|ends?|aprons?|strips?)\b", re.IGNORECASE)
_LEGEND: Final = re.compile(r"\bM(\d+(?:\.\d+)?)\s*=\s*MIT(?:RE|ER)(?:ED|D)?\s+EDGE", re.IGNORECASE)
_INCH: Final = re.compile(r"(\d+(?:\.\d+)?(?:[\s-]+\d+/\d+)?|\d+/\d+)\s*(?:\"|''|”|in\b)")


def mitre_build_up(text: str) -> tuple[str, Fraction] | None:
    """The mitred edge's code and build-up the vendor's legend gives ("M1.5 = MITERED EDGE":
    ("M1.5", 1 1/2"))."""
    found = _LEGEND.search(text or "")
    return (f"M{found.group(1)}", Fraction(found.group(1))) if found else None


def _inches(text: str) -> set[Fraction]:
    out: set[Fraction] = set()
    for found in _INCH.finditer(text or ""):
        whole, _, part = re.sub(r"[\s-]+", " ", found.group(1)).partition(" ")
        try:
            out.add(Fraction(whole) + (Fraction(part) if part else 0))
        except (ValueError, ZeroDivisionError):
            continue
    return out


def _inch_words(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    return f'{whole} {rest}"' if whole and rest else f'{rest or whole}"'


# ---- 5. which end, on a unit drawn both ways round ----------------------------------------------

#: A claim about which end or side something is at.
_END_CLAIM: Final = re.compile(
    r"\b(?:left|right|opposite|other|wrong) (?:end|side|hand)\b|\b(?:on|at) the (?:left|right)\b|"
    r"\bwhich (?:end|side)\b|\bmirror(?:ed|s)?\b|\brevers(?:ed|e)\b|\bflipped\b|\bswapped\b",
    re.IGNORECASE,
)
#: The claim's own words say only the end differs (no left / right named on either side).
_ONLY_THE_HAND: Final = re.compile(
    r"\b(?:opposite|other|wrong) (?:end|side|hand)\b|\bmirror(?:ed|s)?\b|\brevers(?:ed|e)\b|"
    r"\bflipped\b|\bswapped\b",
    re.IGNORECASE,
)
_SIDE_WORD: Final = re.compile(r"\b(left|right)\b", re.IGNORECASE)
#: A size measured from one end ("14 1/4" from the right end").
_FROM_END: Final = re.compile(
    r"(\d+(?:[\s-]+\d+/\d+)?|\d+/\d+)\s*(?:\"|''|”)?\s*(?:from|off) the (left|right)\b",
    re.IGNORECASE,
)
_ANY_NUMBER: Final = re.compile(r"\d+(?:[\s-]+\d+/\d+)?|\d+/\d+")
#: A sheet or unit title that marks a unit drawn the other way round.
_HAND_TITLE: Final = re.compile(
    r"\bOPP(?:OSITE|\.)?\s*HAND(?:ED)?\b|\bMIRROR(?:ED)?\s+IMAGE\b|\bREVERSED?\b", re.IGNORECASE
)
#: A title is a short line (a note that mentions "reverse" in passing is not one).
_TITLE_CHARS: Final = 48
#: Two labels sit at mirrored places within this (points).
_MIRROR_PT: Final = 2.0
#: The labels that must sit at mirrored places before code calls a sheet the other's mirror.
_MIRROR_MIN: Final = 3


def _flip(side: str) -> str:
    return "left" if side.lower() == "right" else "right"


def _centre(label: Mapping[str, Any]) -> tuple[float, float] | None:
    box = label.get("box")
    try:
        x0, y0, x1, y1 = (float(v) for v in box)  # type: ignore[union-attr]
    except (TypeError, ValueError):
        return None
    return (x0 + x1) / 2, (y0 + y1) / 2


def mirrored(first: Sequence[Mapping[str, Any]], second: Sequence[Mapping[str, Any]]) -> bool:
    """Code's proof that `second` (a sheet's labels) is `first` drawn as a mirror image: the
    labels that move between the two sheets (a label at the same place on both is the sheet's
    title block or legend) sit at mirrored places, left to right about one line, at least
    `_MIRROR_MIN` of them and nearly all, and more of them than a plain shift would place."""

    def spots(labels: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[float, float]]]:
        out: dict[str, list[tuple[float, float]]] = {}
        for label in labels:
            centre = _centre(label)
            text = _flat(label.get("text"))
            if centre is not None and text:
                out.setdefault(f"{text}|{label.get('orientation') or ''}", []).append(centre)
        return out

    mine, theirs = spots(first), spots(second)
    moving = [
        (key, (x, y))
        for key, places in mine.items()
        for x, y in places
        if not any(
            abs(x - bx) <= _MIRROR_PT and abs(y - by) <= _MIRROR_PT
            for bx, by in theirs.get(key, [])
        )
    ]
    if len(moving) < _MIRROR_MIN:
        return False

    def fit(mirror: bool) -> int:
        best = 0
        for key, (x, y) in moving:
            for bx, by in theirs.get(key, []):
                line, dy = (x + bx if mirror else bx - x), by - y
                placed = sum(
                    1
                    for k2, (x2, y2) in moving
                    if any(
                        abs((line - x2 if mirror else x2 + line) - bx2) <= _MIRROR_PT
                        and abs(by2 - y2 - dy) <= _MIRROR_PT
                        for bx2, by2 in theirs.get(k2, [])
                    )
                )
                best = max(best, placed)
        return best

    flipped = fit(True)
    return flipped >= _MIRROR_MIN and flipped >= 0.8 * len(moving) and flipped > fit(False)


def _hand_title(text: str) -> str | None:
    """A reversed unit named in a sheet's title or unit list: the REVERSED UNITS heading, a unit
    code drawn reversed ("QR-ST-R", "MN-OP-Reverse"), or a short title line saying REVERSE /
    OPP. HAND / MIRROR IMAGE."""
    units, heading = _vendor_units(text)
    if heading == "reversed":
        return "REVERSED UNITS"
    named = [n for n in units if _hand(n) == "reversed"]
    named += [
        n for codes, _ in _reference_units(text) for n in sorted(codes) if _hand(n) == "reversed"
    ]
    if named:
        return named[0]
    for line in _lines(text):
        found = _HAND_TITLE.search(line)
        if found and len(line.strip()) <= _TITLE_CHARS:
            return found.group(0)
    return None


def _reversal(
    finding: Mapping[str, Any],
    texts: Mapping[tuple[str, int], str],
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    vendor_file: str,
    main_file: str | None,
) -> str | None:
    """How code knows the item's unit is drawn both ways round (its words), else None: never a
    guess."""
    page = int(finding["page"])
    sheets = {p: t for (f, p), t in texts.items() if f == vendor_file}
    sibling = _sibling_sheet(page, sheets)
    if sibling is not None and sibling[1]:
        # The vendor draws the unit on a regular and a reversed sheet: only code's mirror proof
        # between them explains which end (two sheets drawn alike may hold a real mistake).
        heading, near = sibling
        mine = labels.get((vendor_file, page), [])
        flipped = [p for p in near if mirrored(mine, labels.get((vendor_file, p), []))]
        if len(flipped) != 1:
            return None
        other = "reversed" if heading == "regular" else "regular"
        return (
            f"the vendor draws this unit both ways round: its page {flipped[0]} ({other} "
            f"units) is page {page} drawn as a mirror image (code finds its sizes at "
            "mirrored places)"
        )
    found = _hand_title(sheets.get(page, ""))
    if found is not None:
        return f"the vendor's page {page} names a reversed unit ({found})"
    reference = _reference_text(finding, texts, main_file)
    found = _hand_title(reference) if reference else None
    if found is not None:
        return (
            f"the reference's page {finding.get('vs_page')} names a reversed unit ({found}): "
            "its drawing stands for both hands"
        )
    return None


def _same_flipped(finding: Mapping[str, Any]) -> str | None:
    """The end the claim's two sides agree on once the vendor's side is flipped left to right
    ("left" / "right"; "" when neither side names one and the claim itself says only the end
    differs), else None: the claim says more than which end."""
    vendor, architect = _flat(finding.get("vendor")), _flat(finding.get("architect"))
    words = f"{_flat(finding.get('item'))} {_flat(finding.get('why'))}"
    if not _END_CLAIM.search(f"{words} {vendor} {architect}"):
        return None
    v_sides: set[str] = {str(s).lower() for s in _SIDE_WORD.findall(vendor)}
    a_sides: set[str] = {str(s).lower() for s in _SIDE_WORD.findall(architect)}
    if v_sides or a_sides:
        if len(v_sides) != 1 or len(a_sides) != 1 or v_sides == a_sides:
            return None
        end = next(iter(a_sides))
    elif _ONLY_THE_HAND.search(words):
        end = ""
    else:
        return None
    # A size measured from an end must be the same size from the same end once flipped.
    v_from = {(" ".join(n.split()), _flip(e)) for n, e in _FROM_END.findall(vendor)}
    a_from = {(" ".join(n.split()), e.lower()) for n, e in _FROM_END.findall(architect)}
    if v_from and a_from and v_from != a_from:
        return None
    if _kind(finding) == "size" and sorted(_ANY_NUMBER.findall(vendor)) != sorted(
        _ANY_NUMBER.findall(architect)
    ):
        return None  # a size claim: other numbers may be a size difference, never explained
    return end


def reversed_unit(
    finding: Mapping[str, Any],
    texts: Mapping[tuple[str, int], str],
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
    vendor_file: str,
    main_file: str | None,
) -> Change | None:
    """A whole-drawing look's claim about which end or side something is on, on a unit code knows
    is drawn both ways round, that is the same once flipped: withdrawn (see the module)."""
    mark = finding.get(likely_problems.FIELD)
    if not isinstance(mark, Mapping) or mark.get("kind") not in ("layout", "size"):
        return None
    if mark.get("proof") not in (None, "likely_only"):
        return None  # code paired printed labels (or a helper's item): not this rule's
    end = _same_flipped(finding)
    if end is None:
        return None
    how = _reversal(finding, texts, labels, vendor_file, main_file)
    if how is None:
        return None  # code cannot tell the unit is reversed: the claim stays
    where = f" ({end})" if end else ""
    return Change(
        "withdrawn",
        "reversed_unit",
        f"{how[0].upper()}{how[1:]}. Flipped left to right, the vendor's drawing puts it at the "
        f"same end as the reference{where}: this is the reversed unit, not a difference",
    )


# ---- the step ---------------------------------------------------------------------------------


def _file_of(finding: Mapping[str, Any], side: str, fallback: str | None) -> str | None:
    box = finding.get("vendor_box" if side == "vendor" else "arch_box")
    if isinstance(box, Mapping) and box.get("file"):
        return str(box["file"])
    return fallback


def _arch_box(finding: Mapping[str, Any]) -> Box | None:
    box = finding.get("arch_box")
    if not isinstance(box, Mapping):
        return None
    try:
        return tuple(float(box[k]) for k in ("x0", "y0", "x1", "y1"))  # type: ignore[return-value]
    except (KeyError, TypeError, ValueError):
        return None


def _reference_text(
    finding: Mapping[str, Any], texts: Mapping[tuple[str, int], str], main_file: str | None
) -> str:
    page = finding.get("vs_page")
    if not isinstance(page, int):
        return ""
    file = _file_of(finding, "architect", None)
    if file is not None:
        return texts.get((file, page), "")
    found = [t for (f, p), t in texts.items() if p == page and f != main_file]
    return found[0] if len(found) == 1 else ""


def changes(
    live: Iterable[Mapping[str, Any]],
    *,
    page_texts: Mapping[tuple[str, int], str],
    main_file: str | None,
    outlines: Outlines | None = None,
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]] | None = None,
) -> list[tuple[str, Change]]:
    """What the rules do, item by item (see the module): withdrawn, or Not compared. `labels`:
    each page's printed labels (`dims.json`), for the mirror proof between a unit's sheets."""
    out: list[tuple[str, Change]] = []
    for finding in live:
        if not _open(finding):
            continue
        change = _change(finding, page_texts, main_file, outlines, labels or {})
        if change is not None:
            out.append((str(finding["id"]), change))
    return out


def _change(
    finding: Mapping[str, Any],
    texts: Mapping[tuple[str, int], str],
    main_file: str | None,
    outlines: Outlines | None,
    labels: Mapping[tuple[str, int], Sequence[Mapping[str, Any]]],
) -> Change | None:
    if notch_item(finding):
        return Change("withdrawn", "notch", NOTCH_WHY)
    if _proved_wrong(finding):
        return None  # the other rules never take away a Wrong code proved
    page = finding.get("page")
    vendor_file = _file_of(finding, "vendor", main_file)
    if not isinstance(page, int) or vendor_file is None:
        return None
    item = _flat(finding.get("item"))
    if _COUNT_ITEM.search(item) or (_kind(finding) == "count" and re.search(r"\bunits?\b", item)):
        reference = _reference_text(finding, texts, main_file)
        sheets = {p: t for (f, p), t in texts.items() if f == vendor_file}
        found = unit_counts(page, reference, sheets) if reference else None
        if found is not None:
            outcome, why = found
            return Change("withdrawn" if outcome == "same" else "not_compared", "unit_counts", why)
        return None
    flipped = reversed_unit(finding, texts, labels, vendor_file, main_file)
    if flipped is not None:
        return flipped
    if (
        _kind(finding) in ("extra_piece", "missing_piece", "layout")
        and _MITRE_ITEM.search(item)
        and _PIECE.search(item)
        and _only_shape(finding)
    ):
        legend = mitre_build_up(texts.get((vendor_file, page), ""))
        sizes = _inches(f"{item} {_flat(finding.get('vendor'))}")
        if legend is not None and legend[1] in sizes:
            return None  # the piece is the legend's mitred edge: the claim stands
        if legend is None:
            why = (
                "the vendor's legend names no mitred edge, so code cannot tell a mitred piece "
                "from a side splash standing at a wall end: no claim"
            )
        else:
            why = (
                f"the vendor's legend marks its mitred edge {legend[0]} (built up to "
                f"{_inch_words(legend[1])}); a piece of another size standing at a wall end is a "
                "side splash, not a mitred piece"
            )
        return Change("withdrawn", "side_splash", why)
    if _l_claim(finding) and _only_shape(finding):
        box = _arch_box(finding)
        file = _file_of(finding, "architect", None)
        vs_page = finding.get("vs_page")
        seen = (
            outlines(file, vs_page, box)
            if outlines is not None and box is not None and file and isinstance(vs_page, int)
            else None
        )
        if seen != "wall":
            return None  # a top, or code cannot tell: the like-for-like step decides
        return Change(
            "withdrawn",
            "wall_outline",
            "the L-shaped outline on the reference's plan is drawn at the wall's weight (the "
            "heaviest line on the page): it is a wall or pony wall, not the countertop",
        )
    return None
