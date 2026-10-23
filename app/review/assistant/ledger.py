"""The reading ledger (chat, "smarter than claude.ai" items 1-3): what the AI read, checked by code.

claude.ai reads a drawing like a person skimming it: one picture at a time, sums in its head, and no
record of what it read. Here every number the AI reads goes into a per-message **ledger**, and code
checks the ledger against itself. Each check catches the vendor's mistakes AND checks that the AI
read the number right. All of it is code: $0, no model, the same answer every time.

- **Vendor arithmetic.** `record_string` holds one dimension string: its printed parts and its
  printed overall. Code adds the parts up exactly with the repo's chain check
  (`extraction/chains.py` `validate_closure`, loaded by name) after `finding_checks.to_inches` reads
  each value. A vendor string that does not add up becomes a `vendor_arithmetic` finding.
  **Which part is likely wrong** (#1545): code tries each single part's (and the overall's) common
  misreads and typos (±1/8", ±1/4", the fraction dropped, two digits swapped, digits that look
  alike) and names the ONE change that makes the string add up exactly, and whether the same
  printed part makes another vendor string (e.g. the other round's) add up with the same change.
  A hint for the reviewer only: the recorded value is never changed.
- **Same item, different views; typical units.** `record_reading` holds one value: the page, the
  view (plan, elevation, section, detail, schedule), the side (vendor or architect), the item as the
  AI names it, what the value is (width, depth, height, centre line, tag, model, count, other) and
  the value as printed. Code groups readings by item, kind and side (item names normalised, so
  "Base B-36" and "b36 base cabinet" are one item) across views and pages, and compares them: sizes
  in inches, tags and models as normalised words. GV's settled view roles decide which view governs
  ("Settled with the client": plan for the cut-out position, offsets and wall-to-wall; elevation for
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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from importlib import import_module
from typing import Any, Final, Literal

from app.review.assistant.finding_checks import to_inches
from app.review.assistant.product_packs import item_product
from app.review.assistant.tool_schema import SIDE_WORDS, box_schema

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
    "add_up",
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

_BOX_SCHEMA: Final[Mapping[str, Any]] = box_schema(
    "Where it is printed, a little larger than the value. The app crops the evidence here.",
    page="Page of that file (from 1); defaults to the reading's page",
    file="The upload's file name, only when the set has more than one PDF",
)
_PAGE: Final[Mapping[str, Any]] = {"type": "integer", "minimum": 1, "description": "Page (from 1)"}
_VIEW: Final[Mapping[str, Any]] = {
    "type": "string",
    "enum": list(VIEWS),
    "description": "The kind of view the value is printed in",
}
_SIDE: Final[Mapping[str, Any]] = {
    "type": "string",
    "enum": list(SIDES),
    "description": f"Whose drawing it is printed on: {SIDE_WORDS}",
}
_REPLACES: Final[Mapping[str, Any]] = {
    "type": "string",
    "description": "Only when you re-read an entry you recorded before: its id (S3, R12)",
}

RECORD_STRING: Final[Mapping[str, Any]] = {
    "name": "record_string",
    "description": (
        "Record one dimension string (a chain with a printed overall): its parts in order and "
        "the overall, by label (part_dims, total_dim: code fills the values) or, unlabelled, as "
        "printed (parts, printed_total). Use it for every string you read, on "
        "either side: code adds it exactly, and a vendor string that does not add up becomes a "
        "finding; re-read one that does not add up once (replaces). Returns the entry id and "
        "code's sum ('Code's sum: 12 + 18 = 30 = 30 printed'), or 'Not recorded: <why>'. "
        'Example: {"page": 7, "side": "vendor", "part_dims": ["D12", "D13", "D14"], '
        '"total_dim": "D15"}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "side"],
        "properties": {
            "page": _PAGE,
            "view": _VIEW,
            "side": _SIDE,
            "parts": {
                "type": "array",
                "items": {
                    "type": "string",
                    "maxLength": _MAX_VALUE,
                    "description": "One part, as printed",
                },
                "minItems": 2,
                "maxItems": MAX_PARTS,
                "description": 'The parts as printed, in order (e.g. ["12", "15 1/2\\""]); notes '
                "such as EQ or TYP left out of the numbers",
            },
            "part_dims": {
                "type": "array",
                "items": {"type": "string", "description": "A label id"},
                "minItems": 2,
                "maxItems": MAX_PARTS,
                "description": "Instead of parts: their label ids, in order",
            },
            "printed_total": {
                "type": "string",
                "maxLength": _MAX_VALUE,
                "description": "The overall printed for the string, as printed",
            },
            "total_dim": {
                "type": "string",
                "description": "Instead of printed_total: the overall's label id",
            },
            "box": _BOX_SCHEMA,
            "replaces": _REPLACES,
        },
    },
}
RECORD_READING: Final[Mapping[str, Any]] = {
    "name": "record_reading",
    "description": (
        "Record one value you read on a drawing: the item, what kind of value it is, the view "
        "and the side. Use it for every value you read, on both sides, so code can compare the "
        "same item across views, typical units and the two sides (disagreements and items only "
        "one side shows become findings). Name an item the same way on every page and view, "
        "starting with its area ('Coffee station: B36 base'; add '#2' for the second of a "
        "typical unit). Returns the entry id (R12) and anything code already sees disagree, or "
        "'Not recorded: <why>'. "
        'Example: {"page": 7, "view": "elevation", "side": "vendor", "item": "Coffee station: '
        'counter height", "kind": "height", "value": "2\'-10\\""}'
    ),
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["page", "view", "side", "item", "kind", "value"],
        "properties": {
            "page": _PAGE,
            "view": _VIEW,
            "side": _SIDE,
            "item": {
                "type": "string",
                "maxLength": _MAX_ITEM,
                "description": "The item, short, starting with its area",
            },
            "kind": {
                "type": "string",
                "enum": list(KINDS),
                "description": "What the value is; cl = a centre line or cut-out position",
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
    """Exact inches as a drawing writes them: `34 3/8"`, `36"`, `-1/8"`."""
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


# ---- which view governs (settled with the client) -------------------------------------------------------

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


#: What each view governs, in the words GV's question to the architect uses.
_VIEW_GOVERNS: Final[Mapping[str, str]] = {
    "plan": "cut-out positions, offsets and wall-to-wall sizes",
    "elevation": "widths, fillers and tags",
    "section": "the overhang",
}


def governing_view(item: str, kind: str) -> str | None:
    """The view GV reads this value from (settled with the client), or `None` when no view is settled."""
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
    dims: tuple[str, ...] = ()
    """The labels the parts and the overall came from (`p7.D12`, the overall last), when the
    string was recorded by label: code read every value, none was typed. Kept on the entry for
    code's proof; the stored trust block's shape is unchanged."""

    def sum_text(self) -> str:
        """`10 + 14 1/4 + 10 1/8 = 34 3/8" ≠ 34 1/4" printed (off 1/8")`, all written by code."""
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


#: Looks up a numbered dimension label: (ref, page, side) -> the label (`id`, `page`, `text`,
#: `inches`, `box`, `file`), or `LookupError` with a plain reason.
type LabelLookup = Callable[[object, int, str], Mapping[str, Any]]


def _label_value(label: Mapping[str, Any]) -> str:
    """A label's value as the ledger records a part: its text as printed when code reads that text
    as the label's inches, else the label's inches written out (a mm-first label, say)."""
    text = " ".join(str(label.get("text") or "").split())
    try:
        inches = Decimal(str(label.get("inches")))
    except (ArithmeticError, ValueError):
        raise LedgerRefused(f"{label.get('id')} holds no number code can read") from None
    if text and to_inches(text) == inches:
        return text
    return f'{format_inches(inches)}"'


def _labels_box(labels: Sequence[Mapping[str, Any]], page: int) -> Box | None:
    """The box round the labels on the string's page (the evidence crop of the whole string)."""
    boxes = [
        [float(v) for v in label["box"]]
        for label in labels
        if label.get("page") == page and isinstance(label.get("box"), Sequence)
    ]
    if not boxes:
        return None
    file = labels[0].get("file")
    return Box(
        page,
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
        str(file) if file else None,
    )


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


#: The chain check for other code that adds up strings (`vendor_consistency`).
add_up = _add_up


# ---- which part is likely misread (#1545) --------------------------------------------------------

_EIGHTH: Final = Decimal("0.125")
#: A plain ±1/8" or ±1/4": it fits any part of a string off by that much, so alone it names none.
_OFF_BY: Final = "off by a small fraction"
#: Digits a drawing's pen or a reader easily takes for each other.
_LOOK_ALIKE: Final[Mapping[str, str]] = {
    "1": "7",
    "7": "1",
    "3": "8",
    "8": "36",
    "6": "85",
    "5": "6",
    "0": "8",
    "4": "9",
    "9": "4",
}


def _whole_variants(whole: int) -> set[int]:
    """The whole-inch number with two neighbouring digits swapped, or one digit taken for a
    look-alike (13 -> 31, 18 -> 13)."""
    digits = str(whole)
    out: set[int] = set()
    for i in range(len(digits) - 1):
        swapped = digits[:i] + digits[i + 1] + digits[i] + digits[i + 2 :]
        out.add(int(swapped))
    for i, digit in enumerate(digits):
        for other in _LOOK_ALIKE.get(digit, ""):
            out.add(int(digits[:i] + other + digits[i + 1 :]))
    out.discard(whole)
    return out


def misread_values(value: Decimal) -> dict[Decimal, str]:
    """What a printed value may really be, each with how it was misread: ±1/8", ±1/4", the
    fraction dropped (13 1/8 -> 13), two digits swapped or a look-alike digit (fraction kept)."""
    whole = int(value)
    fraction = value - whole
    out: dict[Decimal, str] = {}
    # The telling misreads first: a value both ways keeps the telling words.
    if fraction:
        out[Decimal(whole)] = "its fraction dropped"
    for other in _whole_variants(whole):
        out.setdefault(other + fraction, "a digit misread or swapped")
    for step in (_EIGHTH, 2 * _EIGHTH):
        for change in (step, -step):
            out.setdefault(value + change, _OFF_BY)
    return {
        candidate: how for candidate, how in out.items() if candidate > 0 and candidate != value
    }


@dataclass(frozen=True, slots=True)
class Misread:
    """One change that makes a string add up exactly: part `index` (-1: the printed overall)."""

    index: int
    printed: str
    likely: Decimal
    how: str


def string_misreads(parts: Sequence[str], printed_total: str) -> list[Misread]:
    """Every single change (one part, or the overall) from `misread_values` that makes the string
    add up exactly. Empty when a value is not a number or the string already adds up."""
    values = [to_inches(part) for part in parts]
    total = to_inches(printed_total)
    if total is None or any(v is None for v in values):
        return []
    known = [v for v in values if v is not None]
    parts_sum = sum(known, Decimal(0))
    if parts_sum == total:
        return []
    found: list[Misread] = []
    for index, value in enumerate(known):
        for candidate, how in misread_values(value).items():
            if parts_sum - value + candidate == total:
                found.append(Misread(index, parts[index], candidate, how))
    for candidate, how in misread_values(total).items():
        if candidate == parts_sum:
            found.append(Misread(-1, printed_total, candidate, how))
    return found


def _same_elsewhere(
    entry: StringEntry, misread: Misread, others: Sequence[StringEntry]
) -> list[StringEntry]:
    """Other strings on the same side, also off, that hold the same printed part and add up
    exactly with the same change (the same value drawn wrong in another round or view)."""
    if misread.index < 0:
        return []
    value = to_inches(misread.printed)
    out: list[StringEntry] = []
    for other in others:
        if other.id == entry.id or other.side != entry.side or other.result != "does_not_add_up":
            continue
        for fix in string_misreads(other.parts, other.printed_total):
            if fix.index >= 0 and to_inches(fix.printed) == value and fix.likely == misread.likely:
                out.append(other)
                break
    return out


def _misread_words(entry: StringEntry, others: Sequence[StringEntry]) -> str | None:
    """The hint for one vendor string that does not add up: the ONE change that makes it add up
    exactly, preferring a change another string confirms (the same part, off the same way), then
    a telling misread (a dropped fraction, swapped or look-alike digits) over a plain ±1/8" that
    would fit any part. `None` when no change, or more than one equally good, fits."""
    found = string_misreads(entry.parts, entry.printed_total)
    confirmed = {(m.printed, m.likely): m for m in found if _same_elsewhere(entry, m, others)}
    telling = {(m.printed, m.likely): m for m in found if m.how != _OFF_BY}
    chosen = next(
        (next(iter(group.values())) for group in (confirmed, telling) if len(group) == 1), None
    )
    if chosen is None:
        if not found:
            return None
        return (
            f"{len(found)} different single changes would make it add up: code cannot tell "
            "which value is misread."
        )
    what = "the overall" if chosen.index < 0 else "the part"
    total = chosen.likely if chosen.index < 0 else to_inches(entry.printed_total)
    words = (
        f"Likely misread or typo: {what} {chosen.printed} ({chosen.how}); as "
        f"{format_inches(chosen.likely)} the string adds up exactly"
        f"{f' to {format_inches(total)}' if total is not None else ''}."
    )
    same = _same_elsewhere(entry, chosen, others)
    if same:
        listed = ", ".join(f"p.{o.page} (to {o.printed_total})" for o in same)
        words += (
            f" The same {chosen.printed} is off the same way on {listed}: likely one value drawn "
            "wrong every time."
        )
    return words + " A hint only: the value is not changed."


# ---- the ledger ----------------------------------------------------------------------------------


def _category(item: str) -> str:
    """The product a reading's item files under: each active product's pack `item_words` and
    `item_codes` (#1776; countertop: "top", "splash", "ct"; cabinet: "base", "door", B36), in the
    registry's order; else `other`."""
    return item_product(_words(item))


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

    def __init__(self, *, page_count: int | None = None, labels: LabelLookup | None = None) -> None:
        self.page_count = page_count
        #: Looks up a numbered dimension label (`record_string`'s part_dims / total_dim).
        self.labels = labels
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
            frozenset(
                {
                    "page",
                    "view",
                    "side",
                    "parts",
                    "part_dims",
                    "printed_total",
                    "total_dim",
                    "box",
                    "replaces",
                }
            ),
            frozenset({"page", "side"}),
        )
        page = _page(fields["page"], self.page_count)
        view = None if fields.get("view") is None else _choice(fields["view"], "view", VIEWS)
        side = _choice(fields["side"], "side", SIDES)
        labelled = [fields.get(name) is not None for name in ("part_dims", "total_dim")]
        if fields.get("parts") is not None and labelled[0]:
            raise LedgerRefused("give parts or part_dims, not both")
        if fields.get("printed_total") is not None and labelled[1]:
            raise LedgerRefused("give printed_total or total_dim, not both")
        found: list[Mapping[str, Any]] = []
        if labelled[0]:
            parts_raw = fields["part_dims"]
            if isinstance(parts_raw, str) or not isinstance(parts_raw, Sequence):
                raise LedgerRefused("part_dims must be a list of label ids (D12)")
            if not 2 <= len(parts_raw) <= MAX_PARTS:
                raise LedgerRefused(f"part_dims must hold 2 to {MAX_PARTS} labels")
            labels = [self._label(ref, page, side) for ref in parts_raw]
            found += labels
            parts = tuple(_label_value(label) for label in labels)
        else:
            parts_raw = fields.get("parts")
            if parts_raw is None:
                raise LedgerRefused("missing field(s): parts (or part_dims)")
            if isinstance(parts_raw, str) or not isinstance(parts_raw, Sequence):
                raise LedgerRefused("parts must be a list of values as printed")
            if not 2 <= len(parts_raw) <= MAX_PARTS:
                raise LedgerRefused(f"parts must hold 2 to {MAX_PARTS} values")
            parts = tuple(_text(part, "each part", _MAX_VALUE) for part in parts_raw)
        if labelled[1]:
            total_label = self._label(fields["total_dim"], page, side)
            found.append(total_label)
            printed_total = _label_value(total_label)
        elif fields.get("printed_total") is None:
            raise LedgerRefused("missing field(s): printed_total (or total_dim)")
        else:
            printed_total = _text(fields["printed_total"], "printed_total", _MAX_VALUE)
        box = _box(fields.get("box"), page, self.page_count)
        if box is None and found:
            box = _labels_box(found, page)
        # Recorded wholly by label: every value was read by code, none typed.
        dims = tuple(f"p{label['page']}.{label['id']}" for label in found) if all(labelled) else ()
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
                dims=dims,
            )
            self._strings[entry.id] = entry
        return self._string_result(entry)

    def _label(self, ref: object, page: int, side: str) -> Mapping[str, Any]:
        """The numbered label `ref` names on `page` (or `p23.D5`), on this side's drawing."""
        if self.labels is None:
            raise LedgerRefused(
                "labels cannot be looked up here: give parts and printed_total as printed"
            )
        try:
            return self.labels(ref, page, side)
        except LookupError as missing:
            raise LedgerRefused(str(missing).rstrip(".")) from None

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
            hint = _misread_words(entry, self.strings)
            if hint:
                head += f" {hint}"
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
                "why": " ".join(
                    part
                    for part in (
                        f"Vendor's parts don't add up: {entry.sum_text()}.",
                        _misread_words(entry, self.strings),
                    )
                    if part
                ),
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
            if group.side == "architect":
                # Which of the architect's views is which, for GV's question to the architect
                # when code proves both values at their spots (`rule_notes.views_note_of`).
                finding["dims"] = {
                    "views": {
                        "governing": first_ref.view,
                        "other": first_odd.view,
                        "governing_page": first_ref.page,
                        "other_page": first_odd.page,
                        "governs": _VIEW_GOVERNS.get(governs or "", ""),
                    }
                }
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
        # Items a finding already reports as shown on one side only: that finding is the item
        # (with its spots and pictures); a completeness copy of it would carry neither (#1650).
        reported: set[str] = set()
        for finding in findings:
            item = finding.get("item")
            # Only the lead's architect-vs-vendor findings name items both sides show.
            if not isinstance(item, str) or finding.get("source") not in (None, "architect"):
                continue
            shown_architect = _present(finding.get("architect"))
            shown_vendor = _present(finding.get("vendor"))
            if shown_architect:
                see(architect, item, finding.get("vs_page") or finding.get("page"))
            if shown_vendor:
                see(vendor, item, finding.get("page"))
            if shown_architect != shown_vendor:
                reported.add(item_key(item)[0])
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
            if key in reported:
                continue
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
            if key in reported:
                continue
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
