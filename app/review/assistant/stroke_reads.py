"""The pen-stroke numbers code could not read, read by the pipeline's proven two-maker reader.

**Why** (rebuild plan v1, Step 2, LOCKED 2026-11-05). On a real job the architect's typed numbers
were read 94% of the time and the ones drawn as pen strokes 5 of 2,398: sideways, stacked
fractions, lines through them, millimetre rows, crowding. A problem that rests on such a number can
never be proved. The original pipeline already reads drawn labels safely (`extraction/slot_reader`:
code cuts the crop, two readers of different makers must print identical text, the drawing's scale
rejects a misread; E2: 27 agreed-right, 0 agreed-wrong). This module moves those parts across into
the review assistant. It adds no new way of reading.

**What it reads.** The unread pen-stroke numbers the set pack counted (`reading_coverage.json`,
made by `reading_ink.unread_spots`), every one or only those on the pages given (`Scope`: the
paired views first, plan 2E). Each gets an id `p<page>.S<n>` (n from 1, in the coverage's order).

**How, for each one** (every step reused; see the names):

1. **The crop is code's** (`slot_reader.runs`' method, E2's settings): the ink's box padded by
   `padding_pt`, reaching its own dimension line when code found one, clipped short of every other
   label and unread spot on the page (`runs.clip_short_of`), kept on the page (`runs.crop_on_page`),
   cut at 300 dpi from the pack's page cache (`set_pack.crop_picture`, never past 2576 px).
2. **Upright copies**: turned clockwise by each turn the ink may read in (`reading_ink.spot_turns`,
   as the count decided them; a sideways spot code cannot place reads both 90 and 270). An upright
   spot is read upright only.
3. **Two readers of different makers** read each copy (`StrokeReader`; in production
   `slot_reader.bedrock.read_crops_parallel` with its request builder, pacer, concurrency cap, one
   re-ask and spend guard, inside a cap). A sideways spot read at one turn that did not seal and
   fit is read at the other turn too (plan 2B step 2). A sideways spot with no drawn length or no
   view scale is not read at all: it could never seal, so no money goes on it.
4. **Seal, parse, scale, turn** (`slot_reader.orientation`): `seal.seal_label` per turn (stacked
   fractions never, #762; code's own stacked finding counts; GV's sticker or coloured ink holds),
   the sealed text's inches by `units/`, the drawn length against the view's fit
   (`scale_fit.StrokeLines`, `slot_reader.view_scale`: ±10%), and the upside-down guard.
5. **Witnessed only as decision B counts a drawn length** (`drawn_agrees`): the ±10% band only
   rejects misreads; a sealed value is `sealed_witnessed` only where its drawn length also agrees
   as a printed value's must (`scale_fit`'s tolerance), else `sealed_unwitnessed` with the reason.

**Output** (`FILE`, `SCHEMA`): every label planned, with each reader's text per turn and its state:
`sealed_witnessed` (two makers, one turn, the drawing agrees: two different kinds of reading, as
decision B counts them), `sealed_unwitnessed` (an upright label with no scale: a please-check at
most, never red), `held` (both turns fit, or the drawing rejects the agreed value), `unread`.

**Off by default** (`MODE_ENV`: `off` | `paired` | `all`). It never makes a finding, never changes a
label in `dims.json`, and never decides: `verify_value`'s sealed route reads it, and the proof rules
(`reading_routes`, `witness`, `proof`) decide what it may back.

Verification: `tests/review/assistant/test_stroke_reads.py` (fake readers and pages; no paid call).
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Protocol

from extraction.geometry.rows import Box as PtBox
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.orientation import (
    NO_SCALE_SIDEWAYS,
    GuardOutcome,
    GuardState,
    TurnReading,
    decide_turn,
    read_turn,
)
from extraction.slot_reader.runs import (
    E2_CROP_SETTINGS,
    Lane,
    PlannedLabel,
    clip_short_of,
    crop_on_page,
    overlaps,
)
from extraction.slot_reader.seal import ReaderAnswer, _maker

__all__ = [
    "CAP_ENV",
    "DEFAULT_CAP_USD",
    "DEFAULT_READERS",
    "FILE",
    "MODES",
    "MODE_ENV",
    "NOT_CLOSE",
    "READERS_ENV",
    "SCHEMA",
    "CropAsk",
    "LineFound",
    "PackPages",
    "ReadBudget",
    "ReadResult",
    "ReaderInfo",
    "Scope",
    "Spot",
    "StrokeReader",
    "StrokeReadsRun",
    "cap_from_environment",
    "drawn_agrees",
    "ensure_stroke_reads",
    "full_check_reads",
    "inches_text",
    "load",
    "mode_from_environment",
    "spots_of",
    "turned",
]

_log = logging.getLogger(__name__)

FILE: Final = "stroke_reads.json"
SCHEMA: Final = "gv-stroke-reads/1"
#: The setting (`app.config.Settings.assistant_stroke_reads`), read from the environment as flag
#: mode is (`full_check_plan.flag_mode`), so every full check in the process reads alike.
MODE_ENV: Final = "GV_ASSISTANT_STROKE_READS"
MODES: Final = ("off", "paired", "all")
#: The most one full check may spend on these reads (`Settings.assistant_stroke_reads_cap_usd`);
#: never more than the money the check's fixed price leaves for it (`full_check_roster`).
CAP_ENV: Final = "GV_ASSISTANT_STROKE_READS_CAP_USD"
DEFAULT_CAP_USD: Final = Decimal(2)
#: The two readers (`Settings.assistant_stroke_readers`): Claude Sonnet and Qwen, two makers.
READERS_ENV: Final = "GV_ASSISTANT_STROKE_READERS"
DEFAULT_READERS: Final = ("anthropic.claude-sonnet-5-5", "qwen.qwen3-vl-235b-a22b")
#: The dpi a crop is cut at: E2's crops were cut from 300-dpi pages.
CROP_DPI: Final = 300
#: The count's reasons (`reading_ink.REASON_ORDER`) this module acts on.
SIDEWAYS: Final = "sideways"
STACKED: Final = "stacked_fraction"
STICKER: Final = "gv_sticker"
CROWDED: Final = "crowded"
#: How far (points) a claimed box may miss a label's centre and still be at it (`verify_value`'s
#: `_NEAR_PT`: a box drawn tight round a dimension often clips it).
NEAR_PT: Final = 4.0
_STICKER_WORDS: Final = "GV's sticker or markup over it"
#: A label within this of the page's edge is cut by it (`rows.MEASURED_SETTINGS.edge_margin_pt`).
_EDGE_MARGIN_PT: Final = Decimal("0.5")
_MICROS: Final = 1_000_000

type Box = tuple[float, float, float, float]
type PageKey = tuple[str, int]


# ---- the setting ---------------------------------------------------------------------------------


def mode_from_environment(environ: Mapping[str, str] = os.environ) -> str:
    """`off` (the default, also for anything not a mode), `paired` or `all`."""
    mode = environ.get(MODE_ENV, "off").strip().lower()
    return mode if mode in MODES else "off"


def cap_from_environment(environ: Mapping[str, str] = os.environ) -> Decimal:
    """The cap in dollars (`DEFAULT_CAP_USD` when unset); a value that is not a finite amount of
    zero or more is zero: nothing is asked rather than an unknown sum spent."""
    raw = environ.get(CAP_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_CAP_USD
    try:
        value = Decimal(raw.strip())
    except InvalidOperation:
        return Decimal(0)
    return value if value.is_finite() and value >= 0 else Decimal(0)


# ---- what is read --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Spot:
    """One unread pen-stroke number the set pack counted."""

    id: str
    file: str
    page: int
    side: str
    box: Box
    reasons: tuple[str, ...]

    @property
    def sideways(self) -> bool:
        return SIDEWAYS in self.reasons

    @property
    def stacked(self) -> bool:
        """Code found a stacked fraction in its strokes (`reading_ink`): never sealed (#762)."""
        return STACKED in self.reasons

    @property
    def key(self) -> PageKey:
        return (self.file, self.page)


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def spots_of(coverage: Mapping[str, Any] | None) -> list[Spot]:
    """Every pen-stroke spot of a pack's reading coverage, in its order, with its id."""
    out: list[Spot] = []
    for entry in (coverage or {}).get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        file, page = str(entry.get("file") or ""), int(entry.get("page") or 0)
        number = 0
        for spot in entry.get("not_read") or []:
            if not isinstance(spot, Mapping) or spot.get("kind") != "strokes":
                continue
            box = _box(spot.get("box"))
            if box is None:
                continue
            number += 1
            reasons = [str(spot.get("reason") or "other")]
            reasons += [str(r) for r in spot.get("also") or [] if str(r) not in reasons]
            out.append(
                Spot(f"p{page}.S{number}", file, page, str(spot.get("side")), box, tuple(reasons))
            )
    return out


@dataclass(frozen=True, slots=True)
class Scope:
    """Which spots are read: `pages` (`None`: every page) and, within a page, only spots whose
    centre lies in one of its `views` boxes when the page has any listed."""

    pages: frozenset[PageKey] | None = None
    views: Mapping[PageKey, tuple[Box, ...]] = field(default_factory=dict)

    def holds(self, spot: Spot) -> bool:
        if self.pages is not None and spot.key not in self.pages:
            return False
        boxes = self.views.get(spot.key)
        if not boxes:
            return True
        cx, cy = (spot.box[0] + spot.box[2]) / 2, (spot.box[1] + spot.box[3]) / 2
        return any(b[0] <= cx <= b[2] and b[1] <= cy <= b[3] for b in boxes)


# ---- the page's geometry and pictures ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LineFound:
    """A spot's own dimension line (`scale_fit.Drawn`): `drawn` its length in points, exactly."""

    axis: str
    row: float
    a: float
    b: float

    @property
    def drawn(self) -> Fraction:
        return Fraction(Decimal(repr(self.b))) - Fraction(Decimal(repr(self.a)))


class PackPages(Protocol):
    """What code knows of a spot's page ($0): its turns, line, scale, neighbours, picture and ink."""

    def turns(self, spot: Spot) -> tuple[int, ...] | None: ...

    def line(self, spot: Spot) -> LineFound | None: ...

    def scale(self, spot: Spot, line: LineFound) -> Fraction | None: ...

    def others(self, spot: Spot) -> Sequence[Box]: ...

    def page_size(self, spot: Spot) -> tuple[float, float] | None: ...

    def picture(self, spot: Spot, crop: Box) -> bytes | None: ...

    def ink(self, spot: Spot, crop: Box) -> InkAt | None: ...


# ---- the readers ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CropAsk:
    """One upright copy to read: `key` is the caller's (`<label id>@<turn>`)."""

    key: str
    page_index: int
    png: bytes


@dataclass(frozen=True, slots=True)
class ReaderInfo:
    model: str
    maker: str


@dataclass(slots=True)
class ReadBudget:
    """One run's money, shared by every batch of it: `cap_usd`, and the spend guard and stop the
    production reader makes on its first batch (`guard`, `stop`), so a second batch (the other
    turn) never spends past the first's cap."""

    cap_usd: Decimal
    guard: Any = None
    stop: Any = None


@dataclass(frozen=True, slots=True)
class ReadResult:
    """A batch's answers by `(key, model)` (`None`: that reader abstained or was not asked), what
    it cost (millionths of $), each attempt's usage, and why the reading stopped, if it did."""

    answers: Mapping[tuple[str, str], ReaderAnswer | None]
    cost_micros: int = 0
    attempts: tuple[Any, ...] = ()
    stopped: str | None = None


class StrokeReader(Protocol):
    """Two readers of different makers that read a batch of crops (batch-friendly: one call per
    batch, so a contact sheet can replace single crops later without changing the caller)."""

    @property
    def readers(self) -> tuple[ReaderInfo, ...]: ...

    def read(self, asks: Sequence[CropAsk], budget: ReadBudget) -> ReadResult: ...


# ---- planning ------------------------------------------------------------------------------------


def _d(value: float) -> Decimal:
    return Decimal(repr(float(value)))


def _pt_box(box: Box) -> PtBox:
    return PtBox(_d(box[0]), _d(box[1]), _d(box[2]), _d(box[3]))


def _float_box(box: PtBox) -> Box:
    return (float(box.x0), float(box.top), float(box.x1), float(box.bottom))


def turned(png: bytes, turn: int) -> bytes:
    """The picture turned clockwise by `turn` degrees (0, 90 or 270; `ink_labels.Turn`'s meaning:
    turned so, ink reading up the page at 90 or down it at 270 stands upright)."""
    from PIL import Image

    if turn == 0:
        return png
    with Image.open(io.BytesIO(png)) as opened:
        opened.load()
        picture = opened.transpose(
            Image.Transpose.ROTATE_270 if turn == 90 else Image.Transpose.ROTATE_90
        )
    out = io.BytesIO()
    picture.save(out, format="PNG", compress_level=3)
    return out.getvalue()


@dataclass(slots=True)
class _Planned:
    spot: Spot
    label: PlannedLabel
    png: bytes
    ink: InkAt | None
    sideways: bool
    first: tuple[int, ...]
    """The turns read first."""
    flip: tuple[int, ...]
    """The turns read only when the first did not seal and fit (a sideways spot's other turn)."""
    drawn: Fraction | None
    scale: Fraction | None
    readings: dict[int, TurnReading] = field(default_factory=dict)
    answers: list[dict[str, Any]] = field(default_factory=list)


def _crop(
    spot: Spot, line: LineFound | None, others: Sequence[Box], size: tuple[float, float]
) -> tuple[PtBox, bool]:
    """The code-cut crop (`runs`' method): padded, down to the line, clipped short of the others,
    kept on the page; and whether another box overlaps the spot (crowded)."""
    settings = E2_CROP_SETTINGS
    box = _pt_box(spot.box)
    pad = settings.padding_pt
    x0, y0, x1, y1 = box.x0 - pad, box.top - pad, box.x1 + pad, box.bottom + pad
    if line is not None:
        place = _d(line.row)
        if line.axis == "h":
            y0, y1 = min(y0, place - pad), max(y1, place + pad)
        else:
            x0, x1 = min(x0, place - pad), max(x1, place + pad)
    neighbours = [_pt_box(other) for other in others]
    crowded = any(overlaps(other, box) for other in neighbours)
    reach = clip_short_of(box, (x0, y0, x1, y1), neighbours, settings)
    return crop_on_page(box, reach, _d(size[0]), _d(size[1])), crowded


def _turns(spot: Spot, pages: PackPages) -> tuple[bool, tuple[int, ...], tuple[int, ...]]:
    """Whether the spot is sideways, the turns read first, and the turn read only if needed."""
    try:
        found = pages.turns(spot)
    except Exception:  # noqa: BLE001 - not placed: the count's own word decides
        found = None
    turns = tuple(t for t in (found or ()) if t in (0, 90, 270))
    sideways = spot.sideways or (bool(turns) and all(t != 0 for t in turns))
    if not sideways:
        return False, (0,), ()
    own = tuple(t for t in turns if t != 0) or (90, 270)
    return True, own, tuple(t for t in (90, 270) if t not in own)


def _unread(spot: Spot, why: str) -> dict[str, Any]:
    return _entry(spot, [], [], GuardOutcome(GuardState.UNREAD, why, None, None, None))


def _plan(spot: Spot, pages: PackPages) -> _Planned | dict[str, Any]:
    """One spot planned, or its `unread` entry when code cannot cut its picture."""
    size = pages.page_size(spot)
    if size is None:
        return _unread(spot, "the page cannot be read")
    line = pages.line(spot)
    scale = pages.scale(spot, line) if line is not None else None
    drawn = line.drawn if line is not None and line.drawn > 0 else None
    crop, crowded = _crop(spot, line, pages.others(spot), size)
    crop_box = _float_box(crop)
    png = pages.picture(spot, crop_box)
    if png is None:
        return _unread(spot, "code could not cut its picture")
    ink = (
        InkAt(InkClass.GV, _STICKER_WORDS) if STICKER in spot.reasons else pages.ink(spot, crop_box)
    )
    margin = float(_EDGE_MARGIN_PT)
    edge = (
        spot.box[0] <= margin
        or spot.box[1] <= margin
        or spot.box[2] >= size[0] - margin
        or spot.box[3] >= size[1] - margin
    )
    label = PlannedLabel(
        box=_pt_box(spot.box),
        crop=crop,
        lane=Lane.GLYPHS,
        text=None,
        text_stacked=False,
        has_digit=True,
        touches_edge=edge,
        ambiguous_slot=False,
        crowded=crowded or CROWDED in spot.reasons,
        ticks_in_crop=False,
        path_boxes=(),
    )
    sideways, first, flip = _turns(spot, pages)
    return _Planned(spot, label, png, ink, sideways, first, flip, drawn, scale)


# ---- reading -------------------------------------------------------------------------------------


def _ask_key(spot: Spot, turn: int) -> str:
    return f"{spot.id}@{turn}"


def _read_round(
    work: Sequence[tuple[_Planned, int]],
    reader: StrokeReader,
    budget: ReadBudget,
    models: Sequence[str],
) -> ReadResult:
    """One batch: every (label, turn) copy read by both readers; each turn sealed and checked."""
    asks = [
        CropAsk(_ask_key(item.spot, turn), item.spot.page - 1, turned(item.png, turn))
        for item, turn in work
    ]
    result = reader.read(asks, budget) if asks else ReadResult({})
    for item, turn in work:
        key = _ask_key(item.spot, turn)
        got = [result.answers.get((key, model)) for model in models]
        item.answers += [
            {"model": model, "turn": turn, "text": None if answer is None else answer.text}
            for model, answer in zip(models, got, strict=True)
        ]
        present = [answer for answer in got if answer is not None]
        item.readings[turn] = read_turn(
            item.label,
            turn,
            present,
            ink=item.ink,
            stacked_by_code=item.spot.stacked,
            drawn=item.drawn,
            scale=item.scale,
        )
    return result


#: Why a sealed value its drawing did not veto still is not witnessed (`_as_b_counts`).
NOT_CLOSE: Final = (
    "its drawn length is inside the misread band but not close enough to count as a second reading"
)


def drawn_agrees(value: Fraction, drawn: Fraction, scale: Fraction) -> bool:
    """Whether a value agrees with its drawn length as decision B's drawn-length route counts it
    (`scale_fit`'s check: within `AGREE_IN` inches, `AGREE_SHARE` of the value, or `AGREE_PT` drawn
    points, whichever is larger), exactly: the same rule a printed value's drawn length is held to,
    tighter than the ±10% band that only rejects misreads."""
    fit = import_module("app.review.assistant.scale_fit")
    allowed = max(
        Fraction(str(fit.AGREE_IN)),
        Fraction(str(fit.AGREE_SHARE)) * value,
        Fraction(str(fit.AGREE_PT)) * scale,
    )
    return abs(drawn * scale - value) <= allowed


def _as_b_counts(outcome: GuardOutcome, item: _Planned) -> GuardOutcome:
    """A `sealed_witnessed` reading is two kinds of reading (decision B) only when its drawn length
    agrees as a printed value's must (`drawn_agrees`); one the ±10% band let through but that rule
    does not is `sealed_unwitnessed`, said why (`NOT_CLOSE`): a please-check at most, never red."""
    if (
        outcome.state is not GuardState.SEALED_WITNESSED
        or outcome.inches is None
        or item.drawn is None
        or item.scale is None
        or drawn_agrees(outcome.inches, item.drawn, item.scale)
    ):
        return outcome
    return GuardOutcome(
        GuardState.SEALED_UNWITNESSED, NOT_CLOSE, outcome.turn, outcome.text, outcome.inches
    )


def inches_text(value: Fraction | None) -> str | None:
    """An exact inch value as the file writes it: `"69/2"`, or `"68"` for a whole number."""
    if value is None:
        return None
    return (
        str(value.numerator) if value.denominator == 1 else f"{value.numerator}/{value.denominator}"
    )


def _entry(
    spot: Spot,
    tried: Sequence[int],
    answers: Sequence[Mapping[str, Any]],
    outcome: GuardOutcome,
) -> dict[str, Any]:
    return {
        "id": spot.id,
        "file": spot.file,
        "page": spot.page,
        "side": spot.side,
        "box": [round(v, 2) for v in spot.box],
        "reasons": list(spot.reasons),
        "turns_tried": list(tried),
        "answers": [dict(a) for a in answers],
        "state": outcome.state.value,
        "why": outcome.why,
        "turn": outcome.turn,
        "text": outcome.text,
        "inches": inches_text(outcome.inches),
    }


@dataclass(frozen=True, slots=True)
class StrokeReadsRun:
    """What one run did: the file's document, the money and calls it took, and why it stopped."""

    document: dict[str, Any]
    cost_micros: int
    attempts: tuple[Any, ...]
    stopped: str | None

    @property
    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for label in self.document.get("labels") or []:
            out[label["state"]] = out.get(label["state"], 0) + 1
        return out


def _pack_version(pack: Path) -> int:
    try:
        status = json.loads((pack / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    version = status.get("version") if isinstance(status, Mapping) else None
    return version if isinstance(version, int) and not isinstance(version, bool) else 0


def ensure_stroke_reads(
    pack: Path,
    scope: Scope | None,
    reader: StrokeReader,
    cap_usd: Decimal,
    *,
    pages: PackPages | None = None,
    uploads: Path | None = None,
    write: bool = True,
) -> StrokeReadsRun:
    """Read the pack's unread pen-stroke numbers in `scope` (`None`: all) with `reader`, within
    `cap_usd`, and write `FILE` in the pack (see the module). `pages`: the page geometry and
    pictures (default: the pack's own, `PdfPackPages`, with the uploads in `uploads` when given).

    A reading that stops (the cap, the account) keeps what was read: a label no answer reached is
    `unread`, said why. Raises nothing for a label; only a broken pack (no coverage) reads none."""
    pack = Path(pack)
    readers = tuple(reader.readers)
    models = [info.model for info in readers]
    if len(models) != 2 or len({_maker(model) for model in models}) != 2:
        raise ValueError("stroke reads need exactly two readers of different makers")
    coverage = _read_json(pack / "reading_coverage.json")
    spots = [s for s in spots_of(coverage) if scope is None or scope.holds(s)]
    geometry = pages if pages is not None else PdfPackPages(pack, uploads, spots)
    entries: dict[str, dict[str, Any]] = {}
    planned: list[_Planned] = []
    for spot in spots:
        try:
            found = _plan(spot, geometry)
        except Exception as failed:  # noqa: BLE001 - one spot never stops the others
            _log.warning("stroke reads: %s not planned: %s", spot.id, type(failed).__name__)
            found = _unread(spot, "code could not cut its picture")
        if isinstance(found, dict):
            entries[spot.id] = found
        else:
            planned.append(found)
    # A sideways spot with no drawn length or no scale can never seal (plan 2B step 5): it is
    # not read at all, so no money goes on an answer that cannot count.
    blind = [
        item for item in planned if item.sideways and (item.drawn is None or item.scale is None)
    ]
    for item in blind:
        entries[item.spot.id] = _entry(
            item.spot, [], [], GuardOutcome(GuardState.UNREAD, NO_SCALE_SIDEWAYS, None, None, None)
        )
    planned = [item for item in planned if item not in blind]
    budget = ReadBudget(cap_usd)
    cost, attempts, stopped = 0, [], None
    first = [(item, turn) for item in planned for turn in item.first]
    for number in (1, 2):
        if number == 2:
            # The other turn, only for a sideways spot whose first turn(s) did not seal and fit.
            first = [
                (item, turn)
                for item in planned
                if item.flip
                and decide_turn(list(item.readings.values()), sideways=True).state
                is not GuardState.SEALED_WITNESSED
                for turn in item.flip
            ]
        if not first or stopped:
            break
        try:
            result = _read_round(first, reader, budget, models)
        except Exception as failed:  # noqa: BLE001 - what was read is kept; the rest is unread
            _log.warning("stroke reads: reading stopped: %s", type(failed).__name__)
            stopped = type(failed).__name__
            break
        cost += int(result.cost_micros)
        attempts += list(result.attempts)
        stopped = result.stopped
    for item in planned:
        tried = [turn for turn in (*item.first, *item.flip) if turn in item.readings]
        outcome = decide_turn([item.readings[t] for t in tried], sideways=item.sideways)
        if not tried:
            outcome = GuardOutcome(
                GuardState.UNREAD,
                f"not read: {stopped}" if stopped else "not read",
                None,
                None,
                None,
            )
        entries[item.spot.id] = _entry(item.spot, tried, item.answers, _as_b_counts(outcome, item))
    document = {
        "schema": SCHEMA,
        "pack_version": _pack_version(pack),
        "readers": [{"model": info.model, "maker": info.maker} for info in readers],
        "labels": [entries[spot.id] for spot in spots if spot.id in entries],
    }
    if write:
        _write(pack, document)
    return StrokeReadsRun(document, cost, tuple(attempts), stopped)


def full_check_reads(
    *,
    pack: Path | None,
    uploads: Path | None,
    pages: frozenset[PageKey] | None,
    room_micros: int,
    reader_factory: Callable[[], StrokeReader] | None = None,
    environ: Mapping[str, str] = os.environ,
    geometry: PackPages | None = None,
) -> StrokeReadsRun | None:
    """The full check's stroke reads (`full_check_roster._stroke_reads`), or `None` when they do not
    run. **Off** (`MODE_ENV` unset or `off`): returns at once; no reader is made, nothing is read,
    spent or written. **On**: `paired` reads the spots on `pages` (the paired comparisons' pages),
    `all` every spot; the cap is the setting's (`CAP_ENV`), at most `room_micros` (the money the
    fixed price leaves for it); no money or no reader (a missing key, price or pacing) reads none.
    `reader_factory`: the readers (default `reader_from_environment`); `geometry`: the pages
    (default the pack's own, `PdfPackPages`)."""
    mode = mode_from_environment(environ)
    if mode == "off" or pack is None:
        return None
    cap_micros = min(int(cap_from_environment(environ) * _MICROS), max(0, int(room_micros)))
    if cap_micros <= 0:
        _log.warning("stroke reads: no money left for them under the fixed price; none read")
        return None
    try:
        reader = (reader_factory or (lambda: reader_from_environment(environ)))()
    except Exception as failed:  # noqa: BLE001 - no readers: nothing read, nothing spent
        _log.warning("stroke reads not run: %s", failed)
        return None
    scope = None if mode == "all" else Scope(pages=pages or frozenset())
    return ensure_stroke_reads(
        Path(pack),
        scope,
        reader,
        Decimal(cap_micros) / _MICROS,
        pages=geometry,
        uploads=uploads,
    )


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(pack: Path, document: Mapping[str, Any]) -> None:
    """`FILE` in the pack, written to a temporary name and renamed (as `reading_coverage.write`)."""
    target = pack / FILE
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(document, indent=1), encoding="utf-8")
    temporary.replace(target)


#: The last stroke reads loaded per file, by its change time and size (`load`): every proving read
#: of a check asks for them, and the file changes only when a check writes it.
_LOADED: dict[str, tuple[int, int, dict[str, Any] | None]] = {}


def load(pack: Path | None) -> dict[str, Any] | None:
    """A pack's stroke reads, or `None` (no pack, none made, or another schema). Read once per
    version of the file; the document returned is shared, never to be changed by the caller."""
    if pack is None:
        return None
    path = Path(pack) / FILE
    try:
        stat = path.stat()
    except OSError:
        return None
    key = str(path.resolve())
    kept = _LOADED.get(key)
    if kept is not None and kept[:2] == (stat.st_mtime_ns, stat.st_size):
        return kept[2]
    found = _read_json(path)
    document = found if isinstance(found, dict) and found.get("schema") == SCHEMA else None
    _LOADED[key] = (stat.st_mtime_ns, stat.st_size, document)
    return document


# ---- production: the pack's own pages ------------------------------------------------------------


class PdfPackPages:
    """A pack's pages for `PackPages`, $0: each page parsed once (`dim_labels._parse`) from the
    upload when `uploads` holds it, else from the pack's drawings-only copy; turns as the count
    decided them (`reading_ink.spot_turns`); lines and fits from the page's read labels and line
    work (`scale_fit.StrokeLines`); pictures cut from the drawings-only copy through the page
    cache (`set_pack.crop_picture`, `hires/`); ink by colour (`extraction.ink.read_page_ink`)."""

    def __init__(self, pack: Path, uploads: Path | None, spots: Iterable[Spot]) -> None:
        self.pack = Path(pack)
        self.uploads = uploads
        self._spots: dict[PageKey, list[Spot]] = {}
        for spot in spots:
            self._spots.setdefault(spot.key, []).append(spot)
        dims = _read_json(self.pack / "dims.json") or {}
        coverage = _read_json(self.pack / "reading_coverage.json") or {}
        self._labels: dict[PageKey, list[Mapping[str, Any]]] = {}
        for entry in dims.get("pages") or [] if isinstance(dims, Mapping) else []:
            if isinstance(entry, Mapping):
                key = (str(entry.get("file")), int(entry.get("page") or 0))
                self._labels[key] = [
                    la for la in entry.get("labels") or [] if isinstance(la, Mapping)
                ]
        self._unread: dict[PageKey, list[Box]] = {}
        for entry in coverage.get("pages") or [] if isinstance(coverage, Mapping) else []:
            if isinstance(entry, Mapping):
                key = (str(entry.get("file")), int(entry.get("page") or 0))
                self._unread[key] = [
                    box
                    for spot in entry.get("not_read") or []
                    if isinstance(spot, Mapping) and (box := _box(spot.get("box"))) is not None
                ]
        self._parsed: dict[PageKey, Any] = {}
        self._turns: dict[PageKey, dict[Box, tuple[int, ...]]] = {}
        self._lines: dict[PageKey, Any] = {}
        self._ink: dict[PageKey, Any] = {}

    def _copy(self, file: str) -> Path:
        stem = import_module("app.review.assistant.set_pack").stem
        return self.pack / f"{stem(file)}_drawings_only.pdf"

    def _source(self, file: str) -> Path:
        if self.uploads is not None and (self.uploads / file).is_file():
            return self.uploads / file
        return self._copy(file)

    def _page(self, key: PageKey) -> Any:
        if key not in self._parsed:
            try:
                labels = import_module("app.review.assistant.dim_labels")
                self._parsed[key] = labels._parse(self._source(key[0]), key[1] - 1)
            except Exception as failed:  # noqa: BLE001 - no geometry: no turn, line or scale
                _log.warning("stroke reads: page not parsed: %s", type(failed).__name__)
                self._parsed[key] = None
        return self._parsed[key]

    def turns(self, spot: Spot) -> tuple[int, ...] | None:
        if spot.key not in self._turns:
            parsed = self._page(spot.key)
            ink = import_module("app.review.assistant.reading_ink")
            self._turns[spot.key] = (
                {}
                if parsed is None
                else ink.spot_turns(
                    parsed,
                    self._labels.get(spot.key, []),
                    [s.box for s in self._spots.get(spot.key, [])],
                )
            )
        x0, y0, x1, y1 = (round(v, 2) for v in spot.box)
        found = self._turns[spot.key].get((x0, y0, x1, y1))
        return None if found is None else tuple(found)

    def _strokes(self, key: PageKey) -> Any:
        if key not in self._lines:
            parsed = self._page(key)
            fit = import_module("app.review.assistant.scale_fit")
            self._lines[key] = (
                None
                if parsed is None
                else fit.StrokeLines(self._labels.get(key, []), parsed.segments)
            )
        return self._lines[key]

    def line(self, spot: Spot) -> LineFound | None:
        strokes = self._strokes(spot.key)
        span = None if strokes is None else strokes.line(spot.box)
        return None if span is None else LineFound(span.axis, span.row, span.a, span.b)

    def scale(self, spot: Spot, line: LineFound) -> Fraction | None:
        strokes = self._strokes(spot.key)
        if strokes is None:
            return None
        fit = import_module("app.review.assistant.scale_fit")
        span = fit.Drawn("unread", line.axis, line.row, line.a, line.b, Fraction(0), "marks")
        found: Fraction | None = strokes.scale(spot.side, span)
        return found

    def others(self, spot: Spot) -> Sequence[Box]:
        boxes = [b for la in self._labels.get(spot.key, []) if (b := _box(la.get("box")))]
        boxes += [b for b in self._unread.get(spot.key, []) if b != spot.box]
        return [b for b in boxes if b != spot.box]

    def page_size(self, spot: Spot) -> tuple[float, float] | None:
        try:
            cache = import_module("app.review.assistant.page_cache")
            sizes = cache.page_sizes(self.pack / "hires", self._copy(spot.file))
        except Exception:  # noqa: BLE001 - the page cannot be read
            return None
        if not 1 <= spot.page <= len(sizes):
            return None
        return (float(sizes[spot.page - 1].width), float(sizes[spot.page - 1].height))

    def picture(self, spot: Spot, crop: Box) -> bytes | None:
        try:
            pack = import_module("app.review.assistant.set_pack")
            found = pack.crop_picture(
                self._copy(spot.file), spot.page, crop, CROP_DPI, cache=self.pack / "hires"
            )
        except Exception as failed:  # noqa: BLE001 - no picture: the spot stays unread
            _log.warning("stroke reads: picture not cut: %s", type(failed).__name__)
            return None
        png: bytes = found.png
        return png

    def ink(self, spot: Spot, crop: Box) -> InkAt | None:
        if spot.key not in self._ink:
            try:
                from extraction.ink import read_page_ink

                data = self._copy(spot.file).read_bytes()
                self._ink[spot.key] = read_page_ink(data, spot.page - 1, dpi=CROP_DPI)
            except Exception as failed:  # noqa: BLE001 - unknown ink holds the reading back
                _log.info("stroke reads: ink not read: %s", type(failed).__name__)
                self._ink[spot.key] = None
        page = self._ink[spot.key]
        if page is None:
            return None

        def px(box: Box) -> tuple[int, int, int, int]:
            ratio = CROP_DPI / 72
            return (
                int(box[0] * ratio),
                int(box[1] * ratio),
                round(box[2] * ratio),
                round(box[3] * ratio),
            )

        found: InkAt = page.at(px(crop), label=px(spot.box))
        return found


# ---- production: the two readers -----------------------------------------------------------------


class _Routed:
    """One thread's client that sends each request to its model's own route: a Claude model through
    the Claude adapter, any other through Bedrock (`ConverseClient`'s one method)."""

    def __init__(self, claude: Any, bedrock: Any) -> None:
        self._claude = claude
        self._bedrock = bedrock

    def converse(self, **request: Any) -> Mapping[str, Any]:
        output = import_module("extraction.slot_reader.claude_output")
        provider = (
            self._claude if output.is_claude_model(str(request.get("modelId"))) else self._bedrock
        )
        if provider is None:
            raise ValueError("no Claude client is configured for a Claude reader")
        answer: Mapping[str, Any] = provider.for_current_thread().converse(**request)
        return answer


class _RoutedClients:
    def __init__(self, claude: Any, bedrock: Any) -> None:
        self._client = _Routed(claude, bedrock)

    def for_current_thread(self) -> _Routed:
        return self._client


class SlotPairReader:
    """The production `StrokeReader`: `slot_reader.bedrock.read_crops_parallel` (the crop request
    builder and its generic prompt, the per-model pacer, the concurrency cap, the throttle back-off,
    one re-ask then abstain, the Claude picture-size refusal, and the spend guard with its clean
    stop), each answer's cost from the stated prices (`app.runs.rates.call_cost_micros`)."""

    def __init__(
        self,
        *,
        readers: tuple[str, str],
        clients: Any,
        rates: Any,
        calls_per_minute: Mapping[str, int],
        max_concurrent_calls: int = 8,
        max_tokens: int = 3000,
        max_throttle_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
    ) -> None:
        from extraction.form_reader.pricing import require_priced_readers
        from extraction.form_reader.runner import ModelPacer

        if len({_maker(model) for model in readers}) != 2:
            raise ValueError("the two stroke readers must be of different makers")
        require_priced_readers(readers, rates)
        missing = [model for model in readers if model not in calls_per_minute]
        if missing:
            raise ValueError(f"stroke readers need a pacing limit for: {', '.join(missing)}")
        self.models = readers
        self.clients = clients
        self.rates = rates
        self.calls_per_minute = dict(calls_per_minute)
        self.max_concurrent_calls = max_concurrent_calls
        self.max_tokens = max_tokens
        self.max_throttle_retries = max_throttle_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.pacer = ModelPacer(self.calls_per_minute)

    @property
    def readers(self) -> tuple[ReaderInfo, ...]:
        return tuple(ReaderInfo(model, _maker(model)) for model in self.models)

    def read(self, asks: Sequence[CropAsk], budget: ReadBudget) -> ReadResult:
        import threading

        from app.runs.rates import call_cost_micros
        from extraction.slot_reader.anthropic import BatchSpendGuard
        from extraction.slot_reader.bedrock import CropJob, read_crops_parallel
        from extraction.slot_reader.stop import ReadingStop

        if budget.guard is None:
            budget.guard = BatchSpendGuard(budget.cap_usd, self.rates)
            budget.stop = ReadingStop()
        jobs = [
            CropJob(key=ask.key, model_id=model, page_index=ask.page_index, png=ask.png)
            for ask in asks
            for model in self.models
        ]
        attempts: list[Any] = []
        lock = threading.Lock()

        def record(attempt: Any) -> None:
            with lock:
                attempts.append(attempt)

        try:
            found = read_crops_parallel(
                jobs,
                clients=self.clients,
                rates=self.rates,
                calls_per_minute=self.calls_per_minute,
                max_concurrent_calls=self.max_concurrent_calls,
                max_tokens=self.max_tokens,
                max_throttle_retries=self.max_throttle_retries,
                retry_backoff_seconds=self.retry_backoff_seconds,
                record_attempt=record,
                spend_guard=budget.guard,
                pacer=self.pacer,
                stop=budget.stop,
            )
            stopped = None
        except Exception as failed:  # noqa: BLE001 - the account refused: what was read is kept
            found = {}
            stopped = type(failed).__name__
        cost = 0
        for attempt in attempts:
            cost += (
                call_cost_micros(
                    self.rates,
                    attempt.model_id,
                    int(attempt.input_tokens or 0),
                    int(attempt.output_tokens or 0),
                )
                or 0
            )
        cause = getattr(budget.stop, "cause", None)
        answers = {
            key: answer if isinstance(answer, ReaderAnswer) else None
            for key, answer in found.items()
        }
        return ReadResult(
            answers, cost, tuple(attempts), stopped or (None if cause is None else str(cause))
        )


def reader_from_environment(
    environ: Mapping[str, str] = os.environ, settings_factory: Callable[[], Any] | None = None
) -> SlotPairReader:
    """The production reader from the deployment's settings: the two readers (`READERS_ENV`, else
    `DEFAULT_READERS`), the Claude one through OpenRouter or Anthropic (`claude_reader_provider`,
    pinned as the slot reader's adapters pin it) and the other through Bedrock, the stated prices
    (`MODEL_RATES_ENV`) and each reader's pacing (`claude_reader_model_rpm`,
    `form_reader_model_rpm`). Raises with a plain reason when any of them is missing: then nothing
    is read and nothing is spent."""
    from app.runs.rates import rates_from_environment
    from extraction.slot_reader.claude_output import is_claude_model

    if settings_factory is None:
        from app.config import Settings

        settings: Any = Settings()  # type: ignore[call-arg]
    else:
        settings = settings_factory()
    raw = environ.get(READERS_ENV, "")
    named = tuple(part.strip() for part in raw.split(",") if part.strip())
    readers = named if len(named) == 2 else DEFAULT_READERS
    claude = [model for model in readers if is_claude_model(model)]
    if len(claude) > 1:
        raise ValueError("two Claude readers are one maker: name a reader of another maker")
    rates = rates_from_environment()
    if rates is None:
        raise ValueError("stroke reads need the stated model prices (GV_MODEL_RATES_FILE)")
    rpm = {
        **dict(getattr(settings, "form_reader_model_rpm", {}) or {}),
        **dict(getattr(settings, "claude_reader_model_rpm", {}) or {}),
    }
    claude_clients: Any = None
    if claude:
        provider = getattr(settings, "claude_reader_provider", "openrouter")
        key = getattr(settings, f"{provider}_api_key", None)
        value = key.get_secret_value() if key is not None else ""
        if not value:
            raise ValueError(f"stroke reads need the {provider} key for {claude[0]}")
        timeout = int(getattr(settings, "claude_reader_timeout_seconds", 180))
        if provider == "openrouter":
            from extraction.slot_reader.openrouter import ThreadLocalOpenRouterClients

            claude_clients = ThreadLocalOpenRouterClients(value, timeout_seconds=timeout)
        else:
            from extraction.slot_reader.anthropic import ThreadLocalAnthropicClients

            claude_clients = ThreadLocalAnthropicClients(value, timeout_seconds=timeout)
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config  # type: ignore[import-untyped]

    from extraction.form_reader.runner import ThreadLocalConverseClients

    region = str(settings.bedrock_region)
    connect, read = int(settings.bedrock_connect_timeout), int(settings.bedrock_read_timeout)
    bedrock = ThreadLocalConverseClients(
        lambda: boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(connect_timeout=connect, read_timeout=read),
        )
    )
    return SlotPairReader(
        readers=(readers[0], readers[1]),
        clients=_RoutedClients(claude_clients, bedrock),
        rates=rates,
        calls_per_minute={model: int(rpm[model]) for model in readers if model in rpm},
    )
