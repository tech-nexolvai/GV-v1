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
   candidates, a unit code cannot place): **Not compared**. Different after adding up: unchanged.
3. **A wall outline is not the countertop**: a claim that the reference draws an L-shaped top
   stands only when code finds that L drawn as a countertop. Code reads the reference's line work
   in the claim's spot (`OutlineReader`): an L-shaped outline drawn at the wall's weight (the
   heaviest line on the page) or solid-filled (a wall's poche) is a wall or pony wall, never the
   top. Anything else code cannot confirm: **withdrawn** (no claim).
4. **A side splash is not a mitred piece**: the vendor's legend names its mitred edge ("M1.5 =
   MITERED EDGE": the edge built up to 1 1/2"). A claim about mitred pieces stands only when the
   piece's own size is that build-up; a 4" piece standing at a wall end is a side splash. When the
   legend gives no mitre code, code cannot tell them apart: **withdrawn** (no claim).

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
    "mitre_build_up",
    "notch_item",
    "unit_counts",
]

_log = logging.getLogger(__name__)

#: The finding's field this step writes: {"rule", "outcome", "why"}.
FIELD: Final = "claim_rule"

type Outcome = Literal["withdrawn", "not_compared"]
type Rule = Literal["notch", "unit_counts", "wall_outline", "side_splash"]
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


def notch_item(finding: Mapping[str, Any]) -> bool:
    """The item is about a notch (its own name says so) and nothing else."""
    return bool(_NOTCH.search(_flat(finding.get("item")))) and _only_shape(finding)


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


def unit_counts(
    page: int,
    reference: str,
    texts: Mapping[int, str],
) -> tuple[str, str] | None:
    """The unit-count rule on vendor `page` (`texts`: the vendor file's page texts) against the
    reference page's `reference` text: ("same" | "unsure", why), or None when it does not apply
    or the counts still differ after adding up."""
    entries = _reference_units(reference)
    units, heading = _vendor_units(texts.get(page, ""))
    if not entries or not units:
        return None
    sheets = [page]
    if heading is not None:
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
    placed = _place(units, entries)
    if placed is None:
        return (
            ("unsure", "the vendor lists a unit type code could not place in the reference's list")
            if heading is not None
            else None
        )
    if any(placed.get(i, 0) != count for i, (_, count) in enumerate(entries)):
        return None
    total = sum(count for _, count in entries)
    pages = " + ".join(f"page {p}" for p in sheets)
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
_INCH: Final = re.compile(r"(\d+(?:\s+\d+/\d+)?|\d+/\d+)\s*(?:\"|''|”|in\b)")


def mitre_build_up(text: str) -> tuple[str, Fraction] | None:
    """The mitred edge's code and build-up the vendor's legend gives ("M1.5 = MITERED EDGE":
    ("M1.5", 1 1/2"))."""
    found = _LEGEND.search(text or "")
    return (f"M{found.group(1)}", Fraction(found.group(1))) if found else None


def _inches(text: str) -> set[Fraction]:
    out: set[Fraction] = set()
    for found in _INCH.finditer(text or ""):
        whole, _, part = found.group(1).partition(" ")
        try:
            out.add(Fraction(whole) + (Fraction(part) if part else 0))
        except (ValueError, ZeroDivisionError):
            continue
    return out


def _inch_words(value: Fraction) -> str:
    whole, rest = divmod(value, 1)
    return f'{whole} {rest}"' if whole and rest else f'{rest or whole}"'


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
) -> list[tuple[str, Change]]:
    """What the rules do, item by item (see the module): withdrawn, or Not compared."""
    out: list[tuple[str, Change]] = []
    for finding in live:
        if not _open(finding):
            continue
        change = _change(finding, page_texts, main_file, outlines)
        if change is not None:
            out.append((str(finding["id"]), change))
    return out


def _change(
    finding: Mapping[str, Any],
    texts: Mapping[tuple[str, int], str],
    main_file: str | None,
    outlines: Outlines | None,
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
    if _kind(finding) == "count" or _COUNT_ITEM.search(item):
        reference = _reference_text(finding, texts, main_file)
        sheets = {p: t for (f, p), t in texts.items() if f == vendor_file}
        found = unit_counts(page, reference, sheets) if reference else None
        if found is not None:
            outcome, why = found
            return Change("withdrawn" if outcome == "same" else "not_compared", "unit_counts", why)
        return None
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
        if seen == "top":
            return None
        why = (
            "the L-shaped outline on the reference's plan is drawn at the wall's weight (the "
            "heaviest line on the page): it is a wall or pony wall, not the countertop"
            if seen == "wall"
            else "code could not find an L-shaped countertop outline on the reference's plan "
            "(a wall or pony wall outline is not the top): no claim"
        )
        return Change("withdrawn", "wall_outline", why)
    return None
