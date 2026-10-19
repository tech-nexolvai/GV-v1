"""`read_number`: the tool that reads a number code or the AI could not read, $0, no AI (#1746).

The owner's rule (2026-10-19): a vendor number that could not be read is GV's fault, not the
vendor's. This tool is called automatically whenever a value has no checked spot or the AI says
it cannot read one (the simple full check's verify and finish, the chat's `read_number` tool, and
the resolve loop's hook), and it says either the value with the routes that agree, or exactly why
it stays unread. Code's answer stands: the AI cannot override it.

**1. Find the spot.** A label id names it (the set pack's `dims.json`). Otherwise a claimed value
is looked up among the side's labels on the page (`spot_locate.locate`): one spot, or the same
value on each round's copy (the newest round's). Several spots: the result lists them as numbered
candidates and **the AI picks a label id; code never guesses**. A box with no label in it is read
at its ink (a number the page scan missed: a dashed line through a digit).

**2. Read it by independent routes**, each a different kind of evidence:

- **text**: the drawing's own characters (exact; a value in millimetres and inches must agree);
- **ocr**: the pen strokes re-typeset on one line (`ink_labels`) and read by two OCR engines,
  RapidOCR and Tesseract, which must agree (`dim_labels._drawn_value`, with all its guards); the
  pair counts as one family;
- **shape**: each character's centre-line matched to known fonts' digits (`digit_shapes`);
- **ruler**: the label's own dimension line times its drawing's scale, measured from the drawing's
  own printed dimensions (`view_scales`). **The ruler never supplies a value**: it seconds a
  reading or blocks it.

Crossing line work is stripped first: three or more straight pieces on one line (a dashed door
swing, hatch) running through the number are not part of it.

**3. The agreement rule** (`decide`, the research's measured rule):

- the text layer prints it: read (the mm and inches agreeing);
- the OCR pair and the shape matcher give the same value: read; they give different values:
  unread ("the readers disagree");
- one reader only: read only when the ruler seconds it (within 1/4" or 1.5%); a ruler more than
  1/2" and 3% off blocks it; otherwise unread ("one route only");
- a number whose strokes stop on its drawing's clipped edge is cut off in the file: unread.

**4. Stored in the set pack** (`readings.json`, by the request), so a check never pays twice.

Measured ($0, local, two person-checked keys and Set 3, client values kept local): see the PR.
Source: `READ-HANDDRAWN-RESEARCH.md` (local, 2026-10-19) · Verification:
`tests/review/assistant/test_read_number.py`
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal
from uuid import uuid4

from app.review.assistant.finding_checks import claim_value, to_inches
from app.review.assistant.spot_locate import locate

__all__ = [
    "READER_VERSION",
    "READINGS_FILE",
    "NumberReader",
    "Reading",
    "Request",
    "Route",
    "decide",
    "reader_for_pack",
    "read_number",
]

_log = logging.getLogger(__name__)

#: Changes when what a reading holds or how it is decided changes (old cache entries then miss).
READER_VERSION: Final = 1
#: The set pack's cache of readings.
READINGS_FILE: Final = "readings.json"
#: The ruler seconds a value within max(1/4", 1.5%) and blocks one more than max(1/2", 3%) off.
_RULER_AGREES: Final = (0.25, 0.015)
_RULER_BLOCKS: Final = (0.5, 0.03)
#: How far (points) round a spot the ink is gathered, and how close a glyph must come to its
#: drawing's edge to be cut off by it.
_GROW_PT: Final = 14.0
_EDGE_PT: Final = 0.35
#: Straight pieces on one line that make line work (a dashed line, hatch), never a character.
_LINE_PIECES: Final = 3
_THIN_PT: Final = 0.15
#: Pages parsed and kept in this process.
_PAGES_KEPT: Final = 6

type Outcome = Literal["read", "unread", "choose"]
type Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Request:
    """What to read: a page of a file, the side's drawing, and a label id, a box or a claim."""

    file: str
    page: int
    side: str = "vendor"
    label: str | None = None
    """A label id of the page (`D17`)."""
    box: Box | None = None
    """A spot (PDF points as shown, origin top-left)."""
    claim: str | None = None
    """The value someone says is printed there (`46"`), to find its spot and check it."""

    def key(self) -> str:
        box = None if self.box is None else [round(v, 1) for v in self.box]
        raw = json.dumps(
            [READER_VERSION, self.file, self.page, self.side, self.label, box, self.claim]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Route:
    """One route's answer."""

    name: str
    """text, ocr, shape or ruler."""
    value: str | None
    """Inches as a decimal string (the ruler's measured inches), or none."""
    words: str


@dataclass(frozen=True, slots=True)
class Reading:
    """The tool's answer."""

    outcome: Outcome
    value: str | None = None
    """Inches, a decimal string (read only)."""
    text: str | None = None
    """The value as a drawing writes it (`46"`, `794 [31 1/4]`)."""
    label: str | None = None
    """The label id read, when the spot is one of the pack's labels."""
    box: tuple[float, ...] | None = None
    routes: tuple[Route, ...] = ()
    agreed: tuple[str, ...] = ()
    reason: str = ""
    """Plain words: how it was read, or exactly why not."""
    code: str = ""
    """The machine reason: text, ocr_shape, ocr_ruler; cut_off, readers_disagree, one_route,
    no_reader, ruler_blocks, units_disagree, no_ink, no_spot, several_spots, no_page."""
    candidates: tuple[Mapping[str, Any], ...] = ()
    """Several spots print the claim: each `{id, text, box}` (the AI picks one)."""
    page: int | None = None
    side: str = "vendor"

    @property
    def inches(self) -> Decimal | None:
        try:
            return None if self.value is None else Decimal(self.value)
        except (InvalidOperation, ValueError):
            return None

    def matches(self, claim: object) -> bool:
        """Read, and the same inches as `claim`."""
        text = claim_value(claim)
        want = to_inches(text) if text is not None else None
        return self.outcome == "read" and want is not None and self.inches == want

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "value": self.value,
            "text": self.text,
            "label": self.label,
            "box": None if self.box is None else [round(v, 2) for v in self.box],
            "routes": [{"name": r.name, "value": r.value, "words": r.words} for r in self.routes],
            "agreed": list(self.agreed),
            "reason": self.reason,
            "code": self.code,
            "candidates": [dict(c) for c in self.candidates],
            "page": self.page,
            "side": self.side,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Reading:
        box = raw.get("box")
        return cls(
            outcome=raw["outcome"],
            value=raw.get("value"),
            text=raw.get("text"),
            label=raw.get("label"),
            box=None if not box else tuple(float(v) for v in box),
            routes=tuple(
                Route(str(r.get("name")), r.get("value"), str(r.get("words") or ""))
                for r in raw.get("routes") or ()
            ),
            agreed=tuple(str(a) for a in raw.get("agreed") or ()),
            reason=str(raw.get("reason") or ""),
            code=str(raw.get("code") or ""),
            candidates=tuple(dict(c) for c in raw.get("candidates") or ()),
            page=raw.get("page"),
            side=str(raw.get("side") or "vendor"),
        )

    def routes_words(self) -> str:
        return "; ".join(f"{r.name}: {r.words}" for r in self.routes)

    def as_verify(self, claim: object) -> dict[str, Any]:
        """The answer in `verify_value`'s shape (a finding's `verify_<side>` check)."""
        expected = claim_value(claim)
        if self.outcome == "read":
            result = "match" if self.matches(claim) else "mismatch"
        else:
            result = "cannot_read"
        words = {
            "match": f"Read by code by {' and '.join(self.agreed)}: {self.text}",
            "mismatch": f"The drawing prints {self.text} here, not {expected}",
            "cannot_read": f"Code could not read this spot: {self.reason}",
        }[result]
        return {
            "result": result,
            "route": "text" if self.agreed == ("text",) else "drawn",
            "read": self.text,
            "reads": [f"read_number {r.name}: {r.words}" for r in self.routes],
            "reason": self.reason,
            "value": expected,
            "pending": False,
            "words": words,
            "label": self.label,
            "read_number": self.code,
        }


# ---- the agreement rule ----------------------------------------------------------------------------


def _ruler_says(value: Decimal, ruler: float | None) -> str:
    """`agrees`, `blocks` or `silent`: the ruler against one value."""
    if ruler is None:
        return "silent"
    off = abs(ruler - float(value))
    if off <= max(_RULER_AGREES[0], _RULER_AGREES[1] * float(value)):
        return "agrees"
    if off > max(_RULER_BLOCKS[0], _RULER_BLOCKS[1] * float(value)):
        return "blocks"
    return "silent"


@dataclass(frozen=True, slots=True)
class Decision:
    value: Decimal | None
    agreed: tuple[str, ...]
    code: str
    reason: str


def _inch_words(value: Decimal) -> str:
    from app.review.assistant import verify_value as vv

    return str(vv._inches_text(value))


def decide(
    *,
    text: Decimal | None = None,
    ocr: Decimal | None = None,
    shape: Decimal | None = None,
    ruler: float | None = None,
) -> Decision:
    """The research's agreement rule: two families of different kinds agree and no reader
    disagrees; the ruler seconds one reader or blocks it, and never supplies a value."""
    if text is not None:
        return Decision(text, ("text",), "text", "printed as text")
    readers = {name: value for name, value in (("ocr", ocr), ("shape", shape)) if value is not None}
    values = set(readers.values())
    if len(values) > 1:
        said = " and ".join(f"{_inch_words(v)} ({n})" for n, v in readers.items())
        return Decision(None, (), "readers_disagree", f"code read it two different ways: {said}")
    if not values:
        return Decision(None, (), "no_reader", "neither reader could read the strokes")
    value = values.pop()
    ruler_says = _ruler_says(value, ruler)
    if len(readers) == 2:
        # Two readers of different kinds agree; the ruler seconds them or stays silent (a short
        # label in a chain often sits on another dimension's line: measured, never a block).
        agreed = ("ocr", "shape", "ruler") if ruler_says == "agrees" else ("ocr", "shape")
        return Decision(value, agreed, "ocr_shape", "two readers agree")
    if ruler_says == "agrees":
        return Decision(
            value, (*readers, "ruler"), f"{next(iter(readers))}_ruler", "a reader and the ruler"
        )
    only = next(iter(readers))
    if ruler_says == "blocks":
        return Decision(
            None,
            (),
            "ruler_blocks",
            f"the {only} reading {_inch_words(value)} is not what its dimension line measures "
            f"({ruler:.2f}\" at the drawing's scale)",
        )
    return Decision(
        None,
        (),
        "one_route",
        f"only the {only} reading gives a value ({_inch_words(value)}); a second route is needed",
    )


# ---- the ink at a spot ------------------------------------------------------------------------------


def strip_line_work(paths: Sequence[Any]) -> list[Any]:
    """Paths without line work running through a number: straight flat or upright pieces three or
    more on one line (a dashed line, hatch). A piece alone on its line is kept (a `1`, a dash)."""
    lines: dict[tuple[str, float], list[int]] = {}
    for index, path in enumerate(paths):
        x0, y0, x1, y1 = path.box
        if y1 - y0 <= _THIN_PT < x1 - x0:
            lines.setdefault(("h", round((y0 + y1) / 2, 1)), []).append(index)
        elif x1 - x0 <= _THIN_PT < y1 - y0:
            lines.setdefault(("v", round((x0 + x1) / 2, 1)), []).append(index)
    drop = {i for pieces in lines.values() if len(pieces) >= _LINE_PIECES for i in pieces}
    return [path for index, path in enumerate(paths) if index not in drop]


def _near(page: Any, box: Box, grow: float) -> list[Any]:
    x0, y0, x1, y1 = box
    return [
        path
        for path in page.paths
        if path.box[0] >= x0 - grow
        and path.box[2] <= x1 + grow
        and path.box[1] >= y0 - grow
        and path.box[3] <= y1 + grow
    ]


def ink_label(page: Any, spot: Box) -> Any | None:
    """The drawn label at `spot`, without line work through it: the label nearest the spot's
    middle among the ink gathered well round it (so one the spot clips is whole)."""
    ink_labels = import_module("app.review.assistant.ink_labels")
    reach = _GROW_PT + ink_labels.GATHER_PT
    found = ink_labels.labels(strip_line_work(_near(page, spot, reach)), spot)
    if not found:
        return None
    cx, cy = (spot[0] + spot[2]) / 2, (spot[1] + spot[3]) / 2

    def distance(label: Any) -> float:
        b = label.box
        return float(abs((b[0] + b[2]) / 2 - cx) + abs((b[1] + b[3]) / 2 - cy))

    return min(found, key=distance)


def cut_off(label: Any, halves: Sequence[Mapping[str, Any]]) -> bool:
    """Whether two or more of the label's glyphs stop on one edge of the drawing it is on: the
    drawing is clipped there, so its strokes are cut off in the file."""
    x0, y0, x1, y1 = label.box
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    for half in halves:
        try:
            hx0, hy0, hx1, hy1 = (float(v) for v in half.get("box") or ())
        except (TypeError, ValueError):
            continue
        if not (hx0 - 2 <= cx <= hx1 + 2 and hy0 - 2 <= cy <= hy1 + 2):
            continue
        for at, edge in ((0, hx0), (1, hy0), (2, hx1), (3, hy1)):
            touching = sum(1 for glyph in label.glyphs if abs(glyph.box[at] - edge) <= _EDGE_PT)
            if touching >= 2:
                return True
    return False


# ---- the routes -------------------------------------------------------------------------------------


@dataclass(slots=True)
class _Pages:
    """Pages parsed once (`dim_labels._parse`), a few kept."""

    kept: OrderedDict[tuple[str, int], Any] = field(default_factory=OrderedDict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, pdf: Path, page: int) -> Any:
        key = (str(pdf), page)
        with self.lock:
            found = self.kept.get(key)
            if found is not None:
                self.kept.move_to_end(key)
                return found
        parsed = import_module("app.review.assistant.dim_labels")._parse(pdf, page - 1)
        with self.lock:
            self.kept[key] = parsed
            while len(self.kept) > _PAGES_KEPT:
                self.kept.popitem(last=False)
        return parsed


_PAGES: Final = _Pages()


def _turns(label: Any) -> tuple[int, ...]:
    dim_labels = import_module("app.review.assistant.dim_labels")
    ink_labels = import_module("app.review.assistant.ink_labels")
    return tuple(dim_labels._marked_turns(label) or ink_labels.turns(label))


def _ocr(label: Any, engines: Sequence[Any]) -> tuple[Decimal | None, str]:
    """The OCR pair: two engines read the re-typeset label the same, with the drawn route's
    guards (`dim_labels._drawn_value`)."""
    if len(engines) < 2:
        return None, "the second OCR engine is not installed"
    dim_labels = import_module("app.review.assistant.dim_labels")
    reads = dim_labels._label_reads(label, engines, None)
    value = dim_labels._drawn_value(reads, lambda v: dim_labels._vector_ones(label, v, None))
    lines = sorted({line for read in reads for line in read.lines})[:6]
    if value is None:
        return None, f"the two engines did not agree on one value (read {', '.join(lines) or 'nothing'})"
    return value, f"{_inch_words(value)} by two engines"


def _shape(label: Any) -> tuple[Decimal | None, str]:
    """The shape matcher, in each way the label may read; ways that spell different values give
    none."""
    digit_shapes = import_module("app.review.assistant.digit_shapes")
    values: set[Decimal] = set()
    why = "no character matched"
    for turn in _turns(label):
        found = digit_shapes.shape_read(label, turn)
        if found.value is not None:
            values.add(found.value)
        else:
            why = found.why
    if len(values) == 1:
        value = values.pop()
        return value, f"{_inch_words(value)}, every character sure"
    if len(values) > 1:
        return None, "the reading directions spell different values"
    return None, why


def _scale(scales: Sequence[Any], box: Sequence[float], side: str) -> float | None:
    view_scales = import_module("app.review.assistant.view_scales")
    found = view_scales.scale_at(scales, box, side=side)
    return None if found is None else float(found.in_per_pt)


def _ruler_of_label(
    label: Mapping[str, Any], scales: Sequence[Any], side: str
) -> tuple[float | None, str]:
    span = label.get("span_pt")
    if not label.get("dim_line") or not isinstance(span, (int, float)):
        return None, "no dimension line of its own"
    scale = _scale(scales, label.get("box") or (), side)
    if scale is None:
        return None, f"its line is {span} pt but the drawing's scale is not known"
    return round(float(span) * scale, 2), f"{float(span) * scale:.2f}\" ({span} pt at the drawing's scale)"


def _ruler_of_ink(
    parsed: Any, label: Any, scales: Sequence[Any], side: str
) -> tuple[float | None, str]:
    dim_labels = import_module("app.review.assistant.dim_labels")
    for turn in _turns(label):
        try:
            found = dim_labels._line_of(parsed, dim_labels._Part(label=label, turn=turn))
        except Exception:  # noqa: BLE001 - no line: the ruler is silent
            found = None
        if found is None:
            continue
        line, _orientation = found
        span = float(line["span_pt"])
        scale = _scale(scales, label.box, side)
        if scale is None:
            return None, f"its line is {span} pt but the drawing's scale is not known"
        return round(span * scale, 2), f"{span * scale:.2f}\" ({span} pt at the drawing's scale)"
    return None, "no dimension line of its own"


# ---- the tool ---------------------------------------------------------------------------------------


def _label_box(label: Mapping[str, Any]) -> Box | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in label.get("box") or ())
    except (TypeError, ValueError):
        return None
    return x0, y0, x1, y1


def _inside(label: Mapping[str, Any], box: Box, slack: float = 2.0) -> bool:
    found = _label_box(label)
    if found is None:
        return False
    cx, cy = (found[0] + found[2]) / 2, (found[1] + found[3]) / 2
    return box[0] - slack <= cx <= box[2] + slack and box[1] - slack <= cy <= box[3] + slack


def _find(
    request: Request, labels: Sequence[Mapping[str, Any]] | None, halves: Sequence[Mapping[str, Any]]
) -> tuple[Mapping[str, Any] | None, Reading | None]:
    """Step 1: the pack's label to read, or the reading that ends here (several spots, none)."""
    mine = [label for label in labels or () if label.get("side") == request.side]
    if request.label:
        for label in labels or ():
            if str(label.get("id")) == request.label:
                return label, None
        return None, Reading(
            "unread",
            reason=f"page {request.page} has no label {request.label}",
            code="no_spot",
            page=request.page,
            side=request.side,
        )
    if request.box is not None:
        held = [label for label in mine if _inside(label, request.box)]
        if request.claim is not None and len(held) > 1:
            held = [label for label in held if locate([label], request.side, request.claim).label]
        if len(held) == 1:
            return held[0], None
        if len(held) > 1:
            return None, _choose(request, held)
        return None, None  # read the ink at the box
    if request.claim is None:
        return None, Reading(
            "unread",
            reason="no label, spot or value given",
            code="no_spot",
            page=request.page,
            side=request.side,
        )
    found = locate(labels, request.side, request.claim, halves)
    if found.found == "one" and found.label is not None:
        return found.label, None
    if found.found == "several":
        return None, _choose(request, found.candidates)
    return None, Reading(
        "unread",
        reason="the value has no spot on the drawing",
        code="no_spot",
        page=request.page,
        side=request.side,
    )


def _choose(request: Request, labels: Sequence[Mapping[str, Any]]) -> Reading:
    ids = ", ".join(str(label.get("id")) for label in labels)
    return Reading(
        "choose",
        reason=f"several spots print it ({ids}): name the label id",
        code="several_spots",
        candidates=tuple(
            {"id": label.get("id"), "text": label.get("text"), "box": list(label.get("box") or ())}
            for label in labels
        ),
        page=request.page,
        side=request.side,
    )


def read_number(
    request: Request,
    pdf: Path | None,
    *,
    labels: Sequence[Mapping[str, Any]] | None = None,
    halves: Sequence[Mapping[str, Any]] = (),
    scales: Sequence[Any] = (),
    engines: Sequence[Any] | None = None,
) -> Reading:
    """Read one number (see the module). `pdf`: the page's PDF (the set pack's markup-free copy);
    `labels`, `halves`, `scales`: the page's labels (`dims.json`), drawing halves (the page map)
    and measured scales (`scales.json`); `engines`: the OCR engines (default: the drawn route's)."""
    label, ended = _find(request, labels, halves)
    if ended is not None:
        return ended
    routes: list[Route] = []
    box: Box | None = request.box
    ocr: Decimal | None = None
    ruler: float | None = None
    if label is not None:
        box = _label_box(label)
        value = to_inches(str(label.get("inches") or "")) if label.get("inches") is not None else None
        if value is None:
            try:
                value = Decimal(str(label.get("inches")))
            except (InvalidOperation, ValueError):
                value = None
        if label.get("derived"):
            return Reading(
                "unread",
                label=str(label.get("id")),
                box=box,
                reason="an equal part code worked out is not a printed number",
                code="no_spot",
                page=request.page,
                side=request.side,
            )
        if label.get("route") != "drawn" and value is not None:
            if label.get("units") == "disagrees":
                return Reading(
                    "unread",
                    label=str(label.get("id")),
                    box=box,
                    routes=(Route("text", str(value), str(label.get("text"))),),
                    reason="its millimetres and inches disagree",
                    code="units_disagree",
                    page=request.page,
                    side=request.side,
                )
            return Reading(
                "read",
                value=str(value),
                text=str(label.get("text")),
                label=str(label.get("id")),
                box=box,
                routes=(Route("text", str(value), f"printed as text: {label.get('text')}"),),
                agreed=("text",),
                reason="printed as text",
                code="text",
                page=request.page,
                side=request.side,
            )
        if value is not None:
            # The set pack's own OCR pair read this label (two engines agreeing).
            ocr = value
            routes.append(Route("ocr", str(value), f"{label.get('text')} by two engines (set pack)"))
        ruler, said = _ruler_of_label(label, scales, request.side)
        routes.append(Route("ruler", None if ruler is None else str(ruler), said))
    if box is None or pdf is None:
        return Reading(
            "unread", reason="no page to read", code="no_page", page=request.page, side=request.side
        )
    try:
        parsed = _PAGES.get(pdf, request.page)
    except Exception:  # noqa: BLE001 - a page that cannot be read: said so
        _log.warning("read_number: page %s not read", request.page)
        return Reading(
            "unread",
            box=box,
            reason="the page could not be read",
            code="no_page",
            page=request.page,
            side=request.side,
        )
    ink = ink_label(parsed.page, box)
    if ink is None:
        if ocr is not None:
            decided = decide(ocr=ocr, ruler=ruler)
            return _reading(request, label, box, routes, decided)
        return Reading(
            "unread",
            box=box,
            routes=tuple(routes),
            reason="no drawn number at the spot",
            code="no_ink",
            page=request.page,
            side=request.side,
        )
    if label is None:
        box = tuple(float(v) for v in ink.box)  # type: ignore[assignment]
    if cut_off(ink, halves):
        return Reading(
            "unread",
            label=None if label is None else str(label.get("id")),
            box=box,
            routes=tuple(routes),
            reason=(
                "the number's strokes stop on the edge of its drawing: it is cut off in the file"
            ),
            code="cut_off",
            page=request.page,
            side=request.side,
        )
    if label is None:
        ocr, said = _ocr(ink, list(engines) if engines is not None else _engines())
        routes.append(Route("ocr", None if ocr is None else str(ocr), said))
        ruler, said = _ruler_of_ink(parsed, ink, scales, request.side)
        routes.append(Route("ruler", None if ruler is None else str(ruler), said))
    shape, said = _shape(ink)
    routes.append(Route("shape", None if shape is None else str(shape), said))
    return _reading(request, label, box, routes, decide(ocr=ocr, shape=shape, ruler=ruler))


def _reading(
    request: Request,
    label: Mapping[str, Any] | None,
    box: Sequence[float] | None,
    routes: Sequence[Route],
    decided: Decision,
) -> Reading:
    order = {"text": 0, "ocr": 1, "shape": 2, "ruler": 3}
    ordered = tuple(sorted(routes, key=lambda r: order.get(r.name, 9)))
    common = {
        "label": None if label is None else str(label.get("id")),
        "box": None if box is None else tuple(float(v) for v in box),
        "routes": ordered,
        "page": request.page,
        "side": request.side,
    }
    if decided.value is None:
        return Reading("unread", reason=decided.reason, code=decided.code, **common)  # type: ignore[arg-type]
    text = str(label.get("text")) if label is not None else _inch_words(decided.value)
    return Reading(
        "read",
        value=str(decided.value),
        text=text,
        agreed=decided.agreed,
        reason=f"read by {' and '.join(decided.agreed)}",
        code=decided.code,
        **common,  # type: ignore[arg-type]
    )


_ENGINES: list[Any] | None = None
_ENGINES_LOCK: Final = threading.Lock()


def _engines() -> list[Any]:
    global _ENGINES
    with _ENGINES_LOCK:
        if _ENGINES is None:
            _ENGINES = list(import_module("app.review.assistant.dim_labels").drawn_readers())
        return list(_ENGINES)


# ---- the set pack: reader and cache -------------------------------------------------------------------


@dataclass(slots=True)
class NumberReader:
    """`read_number` bound to one set pack: its labels, halves and scales, and its cache
    (`readings.json`). Thread-safe. The simple full check, the chat tool and the resolve loop
    each call it the same way: `reader(Request(file, page, side, label=, box=, claim=))`."""

    pack: Path | None
    pdf_of: Callable[[str], Path | None]
    """The PDF a file's pages are read from (the set pack's markup-free copy)."""
    engines: Sequence[Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    _cache: dict[str, dict[str, Any]] | None = None

    def __call__(self, request: Request) -> Reading:
        key = request.key()
        cached = self._cached().get(key)
        if cached is not None:
            try:
                return Reading.from_dict(cached)
            except (KeyError, TypeError, ValueError):
                pass
        dim_index = import_module("app.review.assistant.dim_index")
        view_scales = import_module("app.review.assistant.view_scales")
        labels_of = dim_index.labels_for(self.pack, self.pack or Path("."))
        labels = labels_of.on_page(request.file, request.page, read_now=False)
        halves = labels_of.halves(request.file, request.page)
        scales = view_scales.scales_on(self.pack, request.file, request.page)
        found = read_number(
            request,
            self.pdf_of(request.file),
            labels=labels,
            halves=halves,
            scales=scales,
            engines=self.engines,
        )
        if found.code != "no_page":
            self._store(key, found)
        return found

    def check(self, file: str, page: int, box: Sequence[float], claim: str, side: str) -> dict[str, Any]:
        """A value at its box in `verify_value`'s shape (the simple check's hook)."""
        spot = (float(box[0]), float(box[1]), float(box[2]), float(box[3]))
        return self(Request(file, page, side, box=spot, claim=claim)).as_verify(claim)

    def _cached(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            if self._cache is None:
                self._cache = {}
                if self.pack is not None:
                    try:
                        document = json.loads((self.pack / READINGS_FILE).read_text("utf-8"))
                        if document.get("version") == READER_VERSION:
                            self._cache = dict(document.get("readings") or {})
                    except (OSError, ValueError, AttributeError):
                        self._cache = {}
            return self._cache

    def _store(self, key: str, found: Reading) -> None:
        cache = self._cached()
        with self.lock:
            cache[key] = found.as_dict()
            if self.pack is None or not self.pack.is_dir():
                return
            path = self.pack / READINGS_FILE
            held = path.with_name(f".{path.name}.{uuid4().hex[:6]}")
            try:
                held.write_text(
                    json.dumps({"version": READER_VERSION, "readings": cache}), encoding="utf-8"
                )
                held.replace(path)
            except OSError:
                _log.warning("read_number: the readings could not be stored in the set pack")


def reader_for_pack(
    pack: Path | None, uploads: Path | None = None, *, engines: Sequence[Any] | None = None
) -> NumberReader:
    """A `NumberReader` for a set pack: each file is read from the pack's markup-free copy (GV's
    markup left out), else from `uploads`. The resolve loop's hook."""

    def pdf_of(file: str) -> Path | None:
        if pack is not None:
            stem = import_module("app.review.assistant.set_pack").stem(file)
            copy = pack / f"{stem}_drawings_only.pdf"
            if copy.is_file():
                return copy
        if uploads is not None and (uploads / file).is_file():
            return uploads / file
        return None

    return NumberReader(pack, pdf_of, engines)
