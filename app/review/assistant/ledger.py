"""The reading ledger (chat, "smarter than claude.ai" items 1-3): what the AI read, checked by code.

claude.ai reads a drawing like a person skimming it: one picture at a time, sums in its head, and no
record of what it read. Here every number the AI reads goes into a per-message **ledger**, and code
checks the ledger against itself. Each check catches the vendor's mistakes AND checks that the AI
read the number right. All of it is code: $0, no model, the same answer every time.

- **Vendor arithmetic.** `record_string` holds one dimension string: its printed parts and its
  printed overall. Code adds the parts up exactly with the repo's chain check
  (`extraction/chains.py` `validate_closure`, loaded by name) after `finding_checks.to_inches` reads
  each value. A vendor string that does not add up becomes a `vendor_arithmetic` finding.
- **Same item, different views; typical units.** `record_reading` holds one value: the page, the
  view (plan, elevation, section, detail, schedule), the side (vendor or architect), the item as the
  AI names it, what the value is (width, depth, height, centre line, tag, model, count, other) and
  the value as printed. Code groups readings by item, kind and side (item names normalised, so
  "Base B-36" and "b36 base cabinet" are one item) across views and pages, and compares them: sizes
  in inches, tags and models as normalised words. GV's settled view roles decide which view governs
  ("Settled with Raj": plan for the cut-out position, offsets and wall-to-wall; elevation for
  widths, fillers and tags; section for the overhang). A group that disagrees is a `cross_view`
  finding.
- **Completeness both ways.** From the readings, the answer's findings and its page coverage: items
  the architect shows that the vendor never does, and vendor-only items (`completeness` findings).
- **Never drop a recorded result.** `recorded_results_check` lists the code failures and the
  evaluator-checked AI page review differences recorded for the set (the `review_results` document)
  that an answer's findings leave out, so the engine can send the lead back once.

Nothing here reads a PDF or calls a model, and `extraction/` is loaded by name at the first sum:
`app/api/` must not reach `extraction/` by import (`tests/api/test_no_heavy_work.py`).

Source: `~/gv-local/research/agents/chat-first/RESEARCH-smarter-than-claude.md` items 1-3,
CONTRACT2 (finding shape), CONTRACT3 (trust block). Verification:
`tests/review/assistant/test_reading_ledger.py`.
"""

from __future__ import annotations

import re
import threading
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from typing import Any, Final, Literal

from app.review.assistant.finding_checks import to_inches

__all__ = [
    "KINDS",
    "LEDGER_TOOLS",
    "LEDGER_TOOL_NAMES",
    "RECORD_READING",
    "RECORD_STRING",
    "SIDES",
    "VIEWS",
    "LedgerRefused",
    "Reading",
    "ReadingLedger",
    "StringEntry",
    "format_inches",
    "governing_view",
    "item_key",
    "recorded_results_check",
]

VIEWS: Final = ("plan", "elevation", "section", "detail", "schedule")
SIDES: Final = ("vendor", "architect")
KINDS: Final = ("width", "depth", "height", "cl", "tag", "model", "count", "other")
#: Kinds compared in inches; tags and models are compared as normalised words.
_SIZE_KINDS: Final = frozenset({"width", "depth", "height", "cl"})
_WORD_KINDS: Final = frozenset({"tag", "model"})

MAX_STRINGS: Final = 600
MAX_READINGS: Final = 3000
MAX_PARTS: Final = 40
_MAX_VALUE: Final = 60
_MAX_ITEM: Final = 160

type StringResult = Literal["adds_up", "does_not_add_up", "not_a_number"]


class LedgerRefused(ValueError):
    """A tool input the ledger cannot record: the model reads the reason as an error result."""


# ---- tool schemas --------------------------------------------------------------------------------

_BOX_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["x0", "y0", "x1", "y1"],
    "properties": {
        "page": {
            "type": "integer",
            "minimum": 1,
            "description": "Page of that file (from 1); defaults to the reading's page",
        },
        "x0": {"type": "number"},
        "y0": {"type": "number"},
        "x1": {"type": "number"},
        "y1": {"type": "number"},
        "file": {
            "type": "string",
            "description": "The upload's file name, only when the set has more than one PDF",
        },
    },
    "description": (
        "Where it is printed: PDF points of the original upload page as shown, origin top-left "
        "(points = pixels x 72 / dpi). The app crops the evidence here."
    ),
}
_PAGE: Final[Mapping[str, Any]] = {"type": "integer", "minimum": 1, "description": "Page (from 1)"}
_SIDE: Final[Mapping[str, Any]] = {
    "type": "string",
    "enum": list(SIDES),
    "description": "Whose drawing: the vendor's shop drawing or the architect's",
}
_REPLACES: Final[Mapping[str, Any]] = {
    "type": "string",
    "description": "Only when you re-read an entry you recorded before: its id (S3, R12)",
}

RECORD_STRING: Final[Mapping[str, Any]] = {
    "name": "record_string",
    "description": (
        "Record one dimension string as printed: its parts in order and the printed overall they "
        "should add up to. Code adds them up exactly and tells you if they don't. Record every "
        "string you read, on both sides; never add them up yourself."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "side", "parts", "printed_total"],
        "properties": {
            "page": _PAGE,
            "view": {"type": "string", "enum": list(VIEWS)},
            "side": _SIDE,
            "parts": {
                "type": "array",
                "items": {"type": "string", "maxLength": _MAX_VALUE},
                "minItems": 2,
                "maxItems": MAX_PARTS,
                "description": 'The parts as printed, in order (e.g. ["12", "15 1/2\\""])',
            },
            "printed_total": {
                "type": "string",
                "maxLength": _MAX_VALUE,
                "description": "The overall printed for the string, as printed",
            },
            "box": _BOX_SCHEMA,
            "replaces": _REPLACES,
        },
    },
}
RECORD_READING: Final[Mapping[str, Any]] = {
    "name": "record_reading",
    "description": (
        "Record one value you read on a drawing: the item, what the value is and the view it is "
        "in. Name the same item the same way on every page and view ('B36 base', 'Vanity top'; "
        "add '#2' for the second of a typical unit). Code compares every view and every typical "
        "unit of the item, and the architect's items with the vendor's."
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "view", "side", "item", "kind", "value"],
        "properties": {
            "page": _PAGE,
            "view": {"type": "string", "enum": list(VIEWS)},
            "side": _SIDE,
            "item": {"type": "string", "maxLength": _MAX_ITEM, "description": "The item, short"},
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "cl = a centre line or cut-out position",
            },
            "value": {
                "type": "string",
                "maxLength": _MAX_VALUE,
                "description": 'As printed (3\'-1", 36 1/2", B36, 4)',
            },
            "box": _BOX_SCHEMA,
            "replaces": _REPLACES,
        },
    },
}
LEDGER_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (RECORD_STRING, RECORD_READING)
LEDGER_TOOL_NAMES: Final = frozenset(tool["name"] for tool in LEDGER_TOOLS)


# ---- inches, words and item names ----------------------------------------------------------------


def format_inches(value: Decimal | Fraction) -> str:
    """Exact inches as a drawing writes them: `39 5/8"`, `36"`, `-1/8"`."""
    exact = Fraction(value)
    sign = "-" if exact < 0 else ""
    exact = abs(exact)
    whole, rest = divmod(exact.numerator, exact.denominator)
    if rest == 0:
        return f'{sign}{whole}"'
    fraction = f"{rest}/{exact.denominator}"
    return f'{sign}{whole} {fraction}"' if whole else f'{sign}{fraction}"'


_JOINERS: Final = re.compile(r"[-_./']")
_NOT_WORD: Final = re.compile(r"[^0-9a-z]+")
_INSTANCE: Final = re.compile(r"(?:#\s*(\d+)|\((\d+)\)|\b(?:instance|unit|no\.?)\s*(\d+))\s*$")
#: Words that never tell two items apart.
_FILLER_WORDS: Final = frozenset(
    {"the", "a", "an", "of", "typ", "typical", "cab", "cabinet", "cabinets", "unit", "units"}
    # What is measured is the reading's kind, not part of the item ("Vanity top depth").
    | {"width", "depth", "height", "length", "overall", "size", "dim", "dimension", "dimensions"}
    | {"cl", "centerline", "centreline"}
)


def _words(text: str) -> list[str]:
    lowered = _JOINERS.sub("", text.casefold().replace("&", " and "))
    return [word for word in _NOT_WORD.sub(" ", lowered).split() if word]


def item_key(item: str) -> tuple[str, int | None]:
    """An item's name as code compares it, and its typical-unit instance number.

    Case, punctuation, word order and filler words do not matter ("Base B-36", "b36 base cabinet"
    and "B 36 Base" are one item). A trailing "#2", "(2)" or "no. 2" is the instance of a typical
    unit, kept apart from the name so all instances group together.
    """
    text = item.strip().casefold()
    instance: int | None = None
    found = _INSTANCE.search(text)
    if found is not None:
        instance = int(next(group for group in found.groups() if group is not None))
        text = text[: found.start()]
    words = [word for word in _words(text) if word not in _FILLER_WORDS]
    joined: list[str] = []
    for word in words:
        # "b 36" is the tag b36: a short letter prefix followed by its number.
        if joined and word.isdigit() and joined[-1].isalpha() and len(joined[-1]) <= 3:
            joined[-1] += word
        else:
            joined.append(word)
    return " ".join(sorted(joined)), instance


def _word_value(value: str) -> str:
    """A tag or model as code compares it: case, spaces and joiners do not matter."""
    return "".join(_words(value))


# ---- which view governs (settled with Raj) -------------------------------------------------------

#: "Which view each check reads: plan (cut-out position, offsets, wall-to-wall), elevation (cabinet
#: widths, fillers, tags), section (overhang)." Item words first, then the kind of value.
_ITEM_VIEW_ROLES: Final[tuple[tuple[tuple[str, ...], str], ...]] = (
    (("overhang",), "section"),
    (("offset", "walltowall", "cutout", "wall to wall", "cut out"), "plan"),
    (("filler",), "elevation"),
)
_KIND_VIEW_ROLES: Final[Mapping[str, str]] = {
    "width": "elevation",
    "tag": "elevation",
    "cl": "plan",
}
_VIEW_ROLE_WORDS: Final[Mapping[str, str]] = {
    "plan": "the plan governs cut-out positions, offsets and wall-to-wall",
    "elevation": "the elevation governs widths, fillers and tags",
    "section": "the section governs the overhang",
}


def governing_view(item: str, kind: str) -> str | None:
    """The view GV reads this value from (settled with Raj), or `None` when no view is settled."""
    words = " ".join(_words(item))
    squashed = words.replace(" ", "")
    for needles, view in _ITEM_VIEW_ROLES:
        if any(needle in words or needle.replace(" ", "") in squashed for needle in needles):
            return view
    return _KIND_VIEW_ROLES.get(kind)


# ---- entries -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Box:
    page: int
    x0: float
    y0: float
    x1: float
    y1: float
    file: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "page": self.page,
            "x0": self.x0,
            "y0": self.y0,
            "x1": self.x1,
            "y1": self.y1,
        }
        if self.file:
            out["file"] = self.file
        return out


@dataclass(frozen=True, slots=True)
class StringEntry:
    """One recorded dimension string and code's sum of it."""

    id: str
    page: int
    view: str | None
    side: str
    parts: tuple[str, ...]
    printed_total: str
    box: Box | None
    by: str
    result: StringResult
    parts_sum: Decimal | None
    """The parts added up by code, in inches; `None` when a part is not a number."""
    off_by: Decimal | None
    """The printed total minus the parts' sum, in inches (as `finding_checks.arithmetic_off_by`)."""
    rereads: int = 0

    def sum_text(self) -> str:
        """`12 + 15 1/2 + 12 1/8 = 39 5/8" ≠ 39 1/2" printed (off 1/8")`, all written by code."""
        parts = " + ".join(self.parts)
        if self.result == "not_a_number" or self.parts_sum is None or self.off_by is None:
            return f"{parts} vs {self.printed_total} printed: not all are numbers code can read"
        total = format_inches(self.parts_sum + self.off_by)
        added = format_inches(self.parts_sum)
        if self.result == "adds_up":
            return f"{parts} = {added} = {total} printed"
        return f"{parts} = {added} ≠ {total} printed (off {format_inches(abs(self.off_by))})"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "view": self.view,
            "side": self.side,
            "parts": list(self.parts),
            "printed_total": self.printed_total,
            "box": None if self.box is None else self.box.as_dict(),
            "by": self.by,
            "result": self.result,
            "parts_sum": None if self.parts_sum is None else str(self.parts_sum),
            "off_by": None if self.off_by is None else str(self.off_by),
            "rereads": self.rereads,
            "sum_text": self.sum_text(),
        }


@dataclass(frozen=True, slots=True)
class Reading:
    """One recorded value."""

    id: str
    page: int
    view: str
    side: str
    item: str
    key: str
    instance: int | None
    kind: str
    value: str
    inches: Decimal | None
    box: Box | None
    by: str
    rereads: int = 0

    @property
    def compare_value(self) -> str | None:
        """What code compares: exact inches for sizes and counts, normalised words for tags and
        models, either for `other`. `None` when a size is not a number code can read."""
        if self.kind in _WORD_KINDS:
            return _word_value(self.value) or None
        if self.inches is not None:
            return f"in:{self.inches}"
        if self.kind in _SIZE_KINDS or self.kind == "count":
            return None
        return _word_value(self.value) or None

    def shown(self) -> str:
        return format_inches(self.inches) if self.inches is not None else self.value

    def where(self) -> str:
        unit = f" #{self.instance}" if self.instance is not None else ""
        return f"{self.view} p.{self.page}{unit}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "page": self.page,
            "view": self.view,
            "side": self.side,
            "item": self.item,
            "item_key": self.key,
            "instance": self.instance,
            "kind": self.kind,
            "value": self.value,
            "inches": None if self.inches is None else str(self.inches),
            "box": None if self.box is None else self.box.as_dict(),
            "by": self.by,
            "rereads": self.rereads,
        }


# ---- input checks --------------------------------------------------------------------------------


def _mapping(raw: object, allowed: frozenset[str], required: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise LedgerRefused("the input must be an object")
    unknown = sorted(str(key) for key in raw if key not in allowed)
    if unknown:
        raise LedgerRefused(f"unknown field(s): {', '.join(unknown)}")
    missing = sorted(name for name in required if raw.get(name) is None)
    if missing:
        raise LedgerRefused(f"missing field(s): {', '.join(missing)}")
    return raw


def _page(value: object, page_count: int | None, name: str = "page") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise LedgerRefused(f"{name} must be a whole number from 1")
    if page_count and value > page_count:
        raise LedgerRefused(f"{name} {value} is not in this set (pages 1 to {page_count})")
    return value


def _text(value: object, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LedgerRefused(f"{name} must be non-empty text")
    text = " ".join(value.split())
    if len(text) > limit:
        raise LedgerRefused(f"{name} is longer than {limit} characters")
    return text


def _choice(value: object, name: str, allowed: Sequence[str]) -> str:
    if value not in allowed:
        raise LedgerRefused(f"{name} must be one of: {', '.join(allowed)}")
    assert isinstance(value, str)
    return value


def _box(raw: object, page: int, page_count: int | None) -> Box | None:
    if raw is None:
        return None
    box = _mapping(
        raw,
        frozenset({"page", "x0", "y0", "x1", "y1", "file"}),
        frozenset({"x0", "y0", "x1", "y1"}),
    )
    numbers: list[float] = []
    for name in ("x0", "y0", "x1", "y1"):
        value = box[name]
        if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
            raise LedgerRefused(f"box {name} must be a number of points from 0")
        numbers.append(float(value))
    if numbers[2] <= numbers[0] or numbers[3] <= numbers[1]:
        raise LedgerRefused("box x1/y1 must be right of and below x0/y0")
    box_page = page if box.get("page") is None else _page(box["page"], page_count, "box page")
    file = box.get("file")
    if file is not None and (not isinstance(file, str) or not file.strip()):
        raise LedgerRefused("box file must be an upload's file name")
    return Box(box_page, numbers[0], numbers[1], numbers[2], numbers[3], file or None)


def _add_up(
    parts: Sequence[str], printed_total: str
) -> tuple[StringResult, Decimal | None, Decimal | None]:
    """The repo's exact chain check (`extraction/chains.py`) over the parts read as inches."""
    values = [to_inches(part) for part in parts]
    total = to_inches(printed_total)
    if total is None or any(value is None for value in values):
        return "not_a_number", None, None
    chains = import_module("extraction.chains")
    measurement = import_module("units.measurement")

    def inch(value: Decimal | None, raw: str) -> Any:
        assert value is not None
        return measurement.Measurement(Fraction(value), measurement.Unit.INCH, raw)

    chain = chains.DimensionChain(
        tuple(inch(value, part) for value, part in zip(values, parts, strict=True)),
        inch(total, printed_total),
    )
    closure = chains.validate_closure(chain)
    if closure.status == chains.ClosureStatus.NOT_VERIFIABLE or closure.actual is None:
        return "not_a_number", None, None
    # Every value is an exact decimal (`to_inches`), so their sum is too.
    parts_sum = sum((value for value in values if value is not None), Decimal(0))
    off_by = total - parts_sum
    result: StringResult = (
        "adds_up" if closure.status == chains.ClosureStatus.CLOSED else ("does_not_add_up")
    )
    return result, parts_sum, off_by


# ---- the ledger ----------------------------------------------------------------------------------

_COUNTERTOP_WORDS: Final = ("countertop", "counter", "top", "backsplash", "splash", "overhang")
_CABINET_WORDS: Final = ("cabinet", "base", "wall", "tall", "vanity", "drawer", "door", "filler")


def _category(item: str) -> str:
    words = set(_words(item))
    if words & set(_COUNTERTOP_WORDS) or "ct" in words:
        return "countertop"
    if words & set(_CABINET_WORDS) or any(re.fullmatch(r"(?:b|w|sb|t|v)\d+", w) for w in words):
        return "cabinet"
    return "other"


@dataclass
class _Group:
    key: str
    kind: str
    side: str
    readings: list[Reading] = field(default_factory=list)


_SHOWN_ABSENT: Final = re.compile(
    r"^(?:|-+|—|n/?a|none|not shown|not drawn|missing|absent|no .*)$", re.IGNORECASE
)


class ReadingLedger:
    """One answer's ledger: the strings and readings the lead and its helpers recorded.

    Thread-safe: helpers record from their own threads. `run_tool` is the engine's one entry point
    for a tool call; `ledger_findings` and `ledger_report` are read when the answer finishes.
    """

    def __init__(self, *, page_count: int | None = None) -> None:
        self.page_count = page_count
        self._lock = threading.Lock()
        self._strings: dict[str, StringEntry] = {}
        self._readings: dict[str, Reading] = {}
        self._string_count = 0
        self._reading_count = 0

    # -- recording --

    @property
    def strings(self) -> tuple[StringEntry, ...]:
        with self._lock:
            return tuple(self._strings.values())

    @property
    def readings(self) -> tuple[Reading, ...]:
        with self._lock:
            return tuple(self._readings.values())

    def run_tool(self, name: str, raw: object, *, by: str = "lead") -> tuple[str, bool]:
        """One ledger tool call: `(result text for the model, is_error)`. Never raises."""
        try:
            if name == "record_string":
                return self.record_string(raw, by=by), False
            if name == "record_reading":
                return self.record_reading(raw, by=by), False
            raise LedgerRefused(f"{name} is not a ledger tool")
        except LedgerRefused as refused:
            return f"Not recorded: {refused}.", True

    def record_string(self, raw: object, *, by: str = "lead") -> str:
        fields = _mapping(
            raw,
            frozenset({"page", "view", "side", "parts", "printed_total", "box", "replaces"}),
            frozenset({"page", "side", "parts", "printed_total"}),
        )
        page = _page(fields["page"], self.page_count)
        view = None if fields.get("view") is None else _choice(fields["view"], "view", VIEWS)
        side = _choice(fields["side"], "side", SIDES)
        parts_raw = fields["parts"]
        if isinstance(parts_raw, str) or not isinstance(parts_raw, Sequence):
            raise LedgerRefused("parts must be a list of values as printed")
        if not 2 <= len(parts_raw) <= MAX_PARTS:
            raise LedgerRefused(f"parts must hold 2 to {MAX_PARTS} values")
        parts = tuple(_text(part, "each part", _MAX_VALUE) for part in parts_raw)
        printed_total = _text(fields["printed_total"], "printed_total", _MAX_VALUE)
        box = _box(fields.get("box"), page, self.page_count)
        result, parts_sum, off_by = _add_up(parts, printed_total)
        with self._lock:
            replaced = self._replaced(fields.get("replaces"), self._strings, "S")
            if replaced is None and len(self._strings) >= MAX_STRINGS:
                raise LedgerRefused(f"the ledger holds at most {MAX_STRINGS} strings")
            if replaced is None:
                self._string_count += 1
            entry = StringEntry(
                id=replaced.id if replaced else f"S{self._string_count}",
                page=page,
                view=view,
                side=side,
                parts=parts,
                printed_total=printed_total,
                box=box,
                by=by,
                result=result,
                parts_sum=parts_sum,
                off_by=off_by,
                rereads=replaced.rereads + 1 if replaced else 0,
            )
            self._strings[entry.id] = entry
        return self._string_result(entry)

    def record_reading(self, raw: object, *, by: str = "lead") -> str:
        fields = _mapping(
            raw,
            frozenset({"page", "view", "side", "item", "kind", "value", "box", "replaces"}),
            frozenset({"page", "view", "side", "item", "kind", "value"}),
        )
        page = _page(fields["page"], self.page_count)
        view = _choice(fields["view"], "view", VIEWS)
        side = _choice(fields["side"], "side", SIDES)
        item = _text(fields["item"], "item", _MAX_ITEM)
        kind = _choice(fields["kind"], "kind", KINDS)
        value = _text(fields["value"], "value", _MAX_VALUE)
        box = _box(fields.get("box"), page, self.page_count)
        key, instance = item_key(item)
        if not key:
            raise LedgerRefused("item must name the item (e.g. 'B36 base', 'Vanity top')")
        inches = None if kind in _WORD_KINDS else to_inches(value)
        with self._lock:
            replaced = self._replaced(fields.get("replaces"), self._readings, "R")
            if replaced is None and len(self._readings) >= MAX_READINGS:
                raise LedgerRefused(f"the ledger holds at most {MAX_READINGS} readings")
            if replaced is None:
                self._reading_count += 1
            reading = Reading(
                id=replaced.id if replaced else f"R{self._reading_count}",
                page=page,
                view=view,
                side=side,
                item=item,
                key=key,
                instance=instance,
                kind=kind,
                value=value,
                inches=inches,
                box=box,
                by=by,
                rereads=replaced.rereads + 1 if replaced else 0,
            )
            self._readings[reading.id] = reading
        return self._reading_result(reading)

    @staticmethod
    def _replaced[T](replaces: object, entries: Mapping[str, T], prefix: str) -> T | None:
        if replaces is None:
            return None
        if not isinstance(replaces, str) or not replaces.startswith(prefix):
            raise LedgerRefused(f"replaces must be the id of an earlier entry ({prefix}1, ...)")
        if replaces not in entries:
            raise LedgerRefused(f"{replaces} is not in this answer's ledger")
        return entries[replaces]

    # -- what the model reads back --

    def _string_result(self, entry: StringEntry) -> str:
        head = (
            f"{entry.id} recorded ({entry.side}, p.{entry.page}). Code's sum: {entry.sum_text()}."
        )
        if entry.result == "does_not_add_up":
            if entry.rereads == 0:
                tail = (
                    f" Re-read this string once on the crop; record it again with replaces "
                    f'"{entry.id}" if you misread it. Do not report it yourself: code does.'
                )
            elif entry.side == "vendor":
                tail = " Still off after your re-read: code reports it as the vendor's arithmetic."
            else:
                tail = (
                    " Still off: the architect's own string; tell the reviewer, never a vendor "
                    "error."
                )
        elif entry.result == "not_a_number":
            tail = ' Write each part as printed (3\'-1", 15 1/2"); metric alone is not read.'
        else:
            tail = ""
        return f"{head}{tail}\n{self.ledger_summary()}"

    def _reading_result(self, reading: Reading) -> str:
        head = (
            f"{reading.id} recorded: {reading.item} {reading.kind} = {reading.value} "
            f"({reading.side}, {reading.where()})."
        )
        if reading.compare_value is None:
            tail = " Code cannot read it as a number; write it as printed."
            return f"{head}{tail}\n{self.ledger_summary()}"
        others = [
            other
            for other in self.readings
            if other.id != reading.id
            and (other.key, other.kind, other.side) == (reading.key, reading.kind, reading.side)
            and other.compare_value is not None
        ]
        differ = [other for other in others if other.compare_value != reading.compare_value]
        if differ:
            listed = "; ".join(f"{other.where()} {other.shown()}" for other in differ[:6])
            tail = (
                f' Differs from {listed}. Re-read once (replaces "{reading.id}" if misread); '
                "code reports what still differs."
            )
        elif others:
            tail = f" Agrees with {len(others)} other reading(s) of it."
        else:
            tail = ""
        return f"{head}{tail}\n{self.ledger_summary()}"

    def ledger_summary(self) -> str:
        """One line the model reads after each ledger tool call (and the lead at finish)."""
        report = self.ledger_report()
        strings = report["strings"]
        views = report["views"]
        unreadable = f", {strings['not_a_number']} unreadable" if strings["not_a_number"] else ""
        return (
            f"Ledger: {_count(strings['total'], 'string')} ({strings['add_up']} add up, "
            f"{strings['off']} off{unreadable}) · {_count(len(self.readings), 'reading')}, "
            f"{_count(views['groups'], 'item')} seen more than once "
            f"({views['inconsistent']} disagree)."
        )

    # -- groups --

    def _groups(self) -> list[_Group]:
        groups: dict[tuple[str, str, str], _Group] = {}
        for reading in self.readings:
            group = groups.setdefault(
                (reading.key, reading.kind, reading.side),
                _Group(reading.key, reading.kind, reading.side),
            )
            group.readings.append(reading)
        return [group for group in groups.values() if len(group.readings) >= 2]

    @staticmethod
    def _compared(group: _Group) -> list[Reading]:
        return [reading for reading in group.readings if reading.compare_value is not None]

    # -- findings --

    def ledger_findings(
        self,
        findings: Iterable[Mapping[str, Any]] = (),
        coverage: Mapping[str, Any] | None = None,
        architect_items: Iterable[Mapping[str, Any]] = (),
    ) -> list[dict[str, Any]]:
        """Code's findings from the ledger, in the chat finding shape (CONTRACT2), without ids.

        - `vendor_arithmetic` (status `different`): a vendor string that does not add up.
        - `cross_view` (status `check`): one item's value differs across views, pages or typical
          units; the governing view's value (settled view roles) is the reference when it agrees
          with itself, else the value most readings share.
        - `completeness` (status `check`): an item one side shows and the other never does.

        For `cross_view` the `architect` field holds the REFERENCE value (the governing view of the
        same side) and `reference_side` says whose drawing it is on; boxes keep each reading's own
        page and file.
        """
        out: list[dict[str, Any]] = []
        for entry in self.strings:
            if entry.side != "vendor" or entry.result != "does_not_add_up":
                continue
            assert entry.parts_sum is not None and entry.off_by is not None
            item = f"Dimension string ({entry.view})" if entry.view else "Dimension string"
            finding: dict[str, Any] = {
                "page": entry.page,
                "item": item,
                "category": "other",
                "architect": format_inches(entry.parts_sum),
                "vendor": entry.printed_total,
                "status": "different",
                "why": f"Vendor's parts don't add up: {entry.sum_text()}",
                "verified": entry.rereads > 0,
                "source": "vendor_arithmetic",
                "parts": list(entry.parts),
                "printed_total": entry.printed_total,
                "created_by": "ledger",
                "ledger_ids": [entry.id],
            }
            if entry.box is not None:
                finding["vendor_box"] = entry.box.as_dict()
            out.append(finding)
        for group in self._groups():
            out.extend(self._group_findings(group))
        out.extend(self._completeness(findings, coverage, architect_items)["findings"])
        return out

    def _group_findings(self, group: _Group) -> list[dict[str, Any]]:
        compared = self._compared(group)
        values = {reading.compare_value for reading in compared}
        if len(values) < 2:
            return []
        name = group.readings[0].item
        typical = len({reading.view for reading in compared}) == 1
        governs = governing_view(name, group.kind)
        reference, reason = self._reference(compared, governs)
        label = "typical units" if typical else "same item, different views"
        shown = " · ".join(
            f"{reading.where()} {reading.shown()}" for reading in sorted(compared, key=_order)
        )
        out: list[dict[str, Any]] = []
        ordered = _by_count(compared)
        if reference is None:
            # No reference: the first value seen most is the pair's left side, and `why` names all.
            reference = ordered[0]
            reason = "no view governs and no value is shared by most readings"
        odd_values = [value for value in ordered if value != reference]
        ref_readings = [reading for reading in compared if reading.compare_value == reference]
        for odd in odd_values:
            odd_readings = [reading for reading in compared if reading.compare_value == odd]
            first_ref, first_odd = ref_readings[0], odd_readings[0]
            finding: dict[str, Any] = {
                "page": first_odd.page,
                "item": f"{name} {group.kind} ({label})",
                "category": _category(name),
                "architect": first_ref.value,
                "vendor": first_odd.value,
                "status": "check",
                "why": (
                    f"The {group.side}'s drawings disagree ({label}): {shown}. Reference "
                    f"{first_ref.where()}: {reason}."
                ),
                "verified": False,
                "source": "cross_view",
                "reference_side": group.side,
                "created_by": "ledger",
                "ledger_ids": [reading.id for reading in [*ref_readings, *odd_readings]],
            }
            if first_ref.page != first_odd.page:
                finding["vs_page"] = first_ref.page
            if first_ref.box is not None:
                finding["arch_box"] = first_ref.box.as_dict()
            if first_odd.box is not None:
                finding["vendor_box"] = first_odd.box.as_dict()
            out.append(finding)
        return out

    @staticmethod
    def _reference(compared: Sequence[Reading], governs: str | None) -> tuple[str | None, str]:
        if governs is not None:
            in_view = {r.compare_value for r in compared if r.view == governs}
            if len(in_view) == 1:
                return next(iter(in_view)), _VIEW_ROLE_WORDS[governs]
        counts = Counter(reading.compare_value for reading in compared)
        (top, top_count), *rest = counts.most_common()
        if top is not None and (not rest or rest[0][1] < top_count):
            return top, f"{top_count} of {len(compared)} readings show it"
        return None, ""

    # -- completeness --

    def _completeness(
        self,
        findings: Iterable[Mapping[str, Any]],
        coverage: Mapping[str, Any] | None,
        architect_items: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        architect: dict[str, _Seen] = {}
        vendor: dict[str, _Seen] = {}

        def see(book: dict[str, _Seen], item: str, page: object, box: Box | None = None) -> None:
            key, _instance = item_key(item)
            if not key:
                return
            seen = book.setdefault(key, _Seen(item))
            if isinstance(page, int) and not isinstance(page, bool):
                seen.pages.add(page)
            if seen.box is None and box is not None:
                seen.box = box

        for reading in self.readings:
            see(
                architect if reading.side == "architect" else vendor,
                reading.item,
                reading.page,
                reading.box,
            )
        for listed in architect_items:
            if isinstance(listed.get("item"), str):
                see(architect, listed["item"], listed.get("page"))
        for finding in findings:
            item = finding.get("item")
            # Only the lead's architect-vs-vendor findings name items both sides show.
            if not isinstance(item, str) or finding.get("source") not in (None, "architect"):
                continue
            if _present(finding.get("architect")):
                see(architect, item, finding.get("vs_page") or finding.get("page"))
            if _present(finding.get("vendor")):
                see(vendor, item, finding.get("page"))
        excused = _excused_pages(coverage)
        result: dict[str, Any] = {
            "compared": bool(architect) and bool(vendor),
            "architect_items": len(architect),
            "vendor_items": len(vendor),
            "missing_on_vendor": [],
            "extra_on_vendor": [],
            "not_looked_at": [],
            "findings": [],
        }
        if not result["compared"]:
            return result
        for key, seen in sorted(architect.items()):
            if key in vendor:
                continue
            if seen.pages and seen.pages <= excused:
                result["not_looked_at"].append(seen.name)
                continue
            result["missing_on_vendor"].append(seen.name)
            result["findings"].append(
                _completeness_finding(
                    seen,
                    "architect",
                    "The architect shows this item; no vendor reading or finding has it.",
                )
            )
        for key, seen in sorted(vendor.items()):
            if key in architect:
                continue
            result["extra_on_vendor"].append(seen.name)
            result["findings"].append(
                _completeness_finding(
                    seen,
                    "vendor",
                    "The vendor shows this item; the architect's drawings read so far do not.",
                )
            )
        return result

    # -- the report for the trust block --

    def ledger_report(
        self,
        findings: Iterable[Mapping[str, Any]] = (),
        coverage: Mapping[str, Any] | None = None,
        architect_items: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """Counts for the trust block (CONTRACT3), all by code."""
        strings = self.strings
        groups = self._groups()
        inconsistent = [
            group for group in groups if len({r.compare_value for r in self._compared(group)}) > 1
        ]
        not_compared = [group for group in groups if len(self._compared(group)) < 2]
        completeness = self._completeness(findings, coverage, architect_items)
        completeness.pop("findings")
        return {
            "strings": {
                "total": len(strings),
                "add_up": sum(entry.result == "adds_up" for entry in strings),
                "off": sum(entry.result == "does_not_add_up" for entry in strings),
                "not_a_number": sum(entry.result == "not_a_number" for entry in strings),
                "vendor_off": sum(
                    entry.result == "does_not_add_up" and entry.side == "vendor"
                    for entry in strings
                ),
                "architect_off": sum(
                    entry.result == "does_not_add_up" and entry.side == "architect"
                    for entry in strings
                ),
                "off_list": [
                    entry.as_dict() for entry in strings if entry.result == "does_not_add_up"
                ],
            },
            "views": {
                "groups": len(groups),
                "consistent": len(groups) - len(inconsistent) - len(not_compared),
                "inconsistent": len(inconsistent),
                "not_compared": len(not_compared),
                "typical_groups": sum(
                    len({r.view for r in group.readings}) == 1 for group in groups
                ),
            },
            "completeness": completeness,
        }

    # -- storage --

    def as_dict(self) -> dict[str, Any]:
        """Everything recorded, for the message's stored blocks."""
        return {
            "strings": [entry.as_dict() for entry in self.strings],
            "readings": [reading.as_dict() for reading in self.readings],
        }


def _count(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


@dataclass
class _Seen:
    name: str
    pages: set[int] = field(default_factory=set)
    box: Box | None = None


def _order(reading: Reading) -> tuple[int, str, int]:
    return reading.page, reading.view, reading.instance or 0


def _by_count(readings: Sequence[Reading]) -> list[str]:
    """Distinct compare values, most readings first (first seen breaks ties)."""
    counts = Counter(reading.compare_value for reading in readings if reading.compare_value)
    return [value for value, _count in counts.most_common()]


def _present(value: object) -> bool:
    return isinstance(value, str) and _SHOWN_ABSENT.match(value.strip()) is None


def _excused_pages(coverage: Mapping[str, Any] | None) -> set[int]:
    """Pages the coverage map says were not opened or not comparable (and never compared): an item
    seen only there is "not looked at", not missing."""
    if not isinstance(coverage, Mapping):
        return set()
    looked: set[int] = set()
    excused: set[int] = set()
    for row in coverage.get("pages") or []:
        if not isinstance(row, Mapping):
            continue
        pages = {p for p in (row.get("page"), row.get("pair_page")) if isinstance(p, int)}
        if row.get("status") in ("same", "different"):
            looked |= pages
        elif row.get("status") in ("not_opened", "not_comparable"):
            excused |= pages
    return excused - looked


def _completeness_finding(seen: _Seen, side: str, why: str) -> dict[str, Any]:
    page = min(seen.pages) if seen.pages else 1
    finding: dict[str, Any] = {
        "page": page,
        "item": seen.name,
        "category": _category(seen.name),
        "architect": "shown" if side == "architect" else "not found",
        "vendor": "shown" if side == "vendor" else "not found",
        "status": "check",
        "why": why,
        "verified": False,
        "source": "completeness",
        "created_by": "ledger",
    }
    if seen.box is not None:
        finding["arch_box" if side == "architect" else "vendor_box"] = seen.box.as_dict()
    return finding


# ---- never drop a recorded result ----------------------------------------------------------------

_FAIL_OUTCOMES: Final = frozenset({"FAIL", "Needs correction"})


@dataclass(frozen=True, slots=True)
class _Recorded:
    key: str
    kind: Literal["code_fail", "ai_review_difference"]
    pages: frozenset[int]
    what: str
    architect: str | None = None
    vendor: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "pages": sorted(self.pages),
            "what": self.what,
            "architect": self.architect,
            "vendor": self.vendor,
        }


def _recorded(payload: Mapping[str, Any]) -> list[_Recorded]:
    out: dict[str, _Recorded] = {}
    records = payload.get("records")
    if isinstance(records, Mapping):
        for item in records.get("countertops") or []:
            if isinstance(item, Mapping) and item.get("outcome") in _FAIL_OUTCOMES:
                key = str(item.get("id"))
                page = item.get("page")
                out[key] = _Recorded(
                    key,
                    "code_fail",
                    frozenset({page} if isinstance(page, int) else set()),
                    f"countertop {item.get('label') or key}",
                    architect=_str(item.get("needed_overall")),
                    vendor=_str(item.get("printed_overall")),
                )
        for item in records.get("other_checks") or []:
            if isinstance(item, Mapping) and item.get("outcome") in _FAIL_OUTCOMES:
                key = str(item.get("id"))
                pages = frozenset(p for p in item.get("pages") or [] if isinstance(p, int))
                out[key] = _Recorded(key, "code_fail", pages, str(item.get("check") or key))
        review_list = records.get("review_list")
        if isinstance(review_list, Mapping):
            for item in review_list.get("failures") or []:
                if not isinstance(item, Mapping) or str(item.get("id")) in out:
                    continue
                key = str(item.get("id"))
                page = item.get("page")
                out[key] = _Recorded(
                    key,
                    "code_fail",
                    frozenset({page} if isinstance(page, int) else set()),
                    str(item.get("what") or item.get("row") or key),
                )
    for sheet in payload.get("ai_page_review") or []:
        if not isinstance(sheet, Mapping) or not isinstance(sheet.get("page"), int):
            continue
        for row in [*(sheet.get("different") or []), *(sheet.get("rows") or [])]:
            if not isinstance(row, Mapping) or row.get("result") != "different":
                continue
            if row.get("verified_by") != "evaluator":
                continue
            key = str(row.get("key"))
            out[key] = _Recorded(
                key,
                "ai_review_difference",
                frozenset({sheet["page"]}),
                str(row.get("measures") or key),
                architect=_str(row.get("architect_text")),
                vendor=_str(row.get("vendor_text")),
            )
    return list(out.values())


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _same_value(left: object, right: str | None) -> bool | None:
    """Whether two written values are the same (inches by code, else normalised words); `None`
    when either is missing."""
    if not isinstance(left, str) or right is None:
        return None
    a, b = to_inches(left), to_inches(right)
    if a is not None and b is not None:
        return a == b
    return _word_value(left) == _word_value(right) != ""


def _finding_pages(finding: Mapping[str, Any]) -> set[int]:
    pages = {finding.get("page"), finding.get("vs_page")}
    for box in (finding.get("arch_box"), finding.get("vendor_box")):
        if isinstance(box, Mapping):
            pages.add(box.get("page"))
    return {page for page in pages if isinstance(page, int) and not isinstance(page, bool)}


def _includes(record: _Recorded, finding: Mapping[str, Any]) -> bool:
    if finding.get("rule_check_id") == record.key:
        return True
    if not record.pages & _finding_pages(finding):
        return False
    vendor = _same_value(finding.get("vendor"), record.vendor)
    architect = _same_value(finding.get("architect"), record.architect)
    if record.kind == "ai_review_difference":
        return vendor is True and architect is not False
    # A code failure: the finding is about GV's rules on that page, or names its values.
    return finding.get("source") == "rules" or (vendor is True and architect is not False)


def recorded_results_check(
    review_results_payload: Mapping[str, Any] | None,
    findings: Iterable[Mapping[str, Any]],
    dismissed: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Which recorded results the answer's findings leave out ("never drop a recorded result").

    `review_results_payload` is the `review_results` document for the whole set (`records` from
    `prompt_records` plus `ai_page_review`); a page's document works too. Recorded = every code
    FAIL ("Needs correction": countertops, other checks, the review list's failures) and every AI
    page review `different` row the evaluator confirmed. A finding includes one when it names it
    (`rule_check_id` = its id or row key), or is on its page and states its vendor value (and does
    not contradict its architect value), or, for a code FAIL, is a `rules` finding on its page.
    `dismissed` maps a recorded key to the lead's reason for leaving it out.

    Returns `{recorded, included, dismissed, missing: [{key, kind, pages, what, architect, vendor}],
    send_back: str | None}`; `send_back` is the note for the lead when anything is missing.
    """
    if not isinstance(review_results_payload, Mapping):
        return {"recorded": 0, "included": 0, "dismissed": 0, "missing": [], "send_back": None}
    live = [finding for finding in findings if isinstance(finding, Mapping)]
    reasons = {key: reason for key, reason in (dismissed or {}).items() if str(reason).strip()}
    recorded = _recorded(review_results_payload)
    included = 0
    left_out = 0
    missing: list[dict[str, Any]] = []
    for record in recorded:
        if any(_includes(record, finding) for finding in live):
            included += 1
        elif record.key in reasons:
            left_out += 1
        else:
            missing.append(record.as_dict())
    send_back = None
    if missing:
        listed = "; ".join(
            f"{item['key']} ({'p.' + ','.join(map(str, item['pages'])) if item['pages'] else 'set'}"
            f": {item['what']})"
            for item in missing[:20]
        )
        send_back = (
            f"Recorded-results check: GV Review's own checks recorded {len(missing)} result(s) "
            f"your answer does not include: {listed}. Report each (report_finding with "
            "rule_check_id = its id) or say why it does not apply."
        )
    return {
        "recorded": len(recorded),
        "included": included,
        "dismissed": left_out,
        "missing": missing,
        "send_back": send_back,
    }
