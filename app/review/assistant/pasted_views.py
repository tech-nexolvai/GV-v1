"""The reference's pasted views, read by code ($0, no AI): sections and elevations pasted on a sheet.

**Why** (the client-meeting job, 2026-10-24): the reference's reviewer pasted the architect's
lavatory sections onto each casework sheet as snapshots (`/Stamp` annotations). Each snapshot is
drawn in pen strokes only: its words (`LAVATORY SECTION`, `KOLHER K-2214 UNDER-COUNTER SINK`,
`2'-1" COUNTERTOP`) have no text layer, so the set index could not confirm their titles and dropped
them, the splash step (`splash_views`, #1918) never looked in them, and the tag reader's two OCR
engines did not agree on `KOLHER K-2214` against `KOLHERK-2214`. Every lavatory section draws the
solid-surface splash, names the sink model and prints the countertop depth, and code saw none of it.

**What code reads, per pasted drawing** (a `/Stamp` on an architect page, or on an architect half of
a sheet; never a vendor's, a brand prototype's or one whose side is not known):

- **its words**: the snapshot drawn alone (`stamp_text.stamps_only`: no page content, no reviewer
  layer), its coloured ink set to white (a drawing is plotted in black or grey; colour is somebody's
  markup), then every line RapidOCR finds is read again, alone, by Tesseract. A line counts only when
  the two engines read the same characters, spaces aside (`KOLHERK-2214` is `KOLHER K-2214`); the
  reading with the word breaks is kept. A line they read differently is kept apart, never used as a
  value (`unagreed`).
- **its title and kind**: the largest agreed line that names a view kind (`... - SECTION`,
  `FRONT ELEVATION`, `PLAN`, `DETAIL`); a section or detail counts as a section.
- **a splash drawn in it** (`drawn_splashes`, geometry, black and grey ink only): a closed piece
  filled with the same stipple as a long thin filled piece (the top), standing on that top's upper
  edge, flush with one of its ends, at least twice as tall as the top is thick and taller than it is
  wide: the splash at the wall, as a section draws it. Nothing else counts (a piece with another
  fill, a piece off the top's end, a top not found).

**What it never does**: decide anything. `View` is evidence; the escalation's step
(`escalation._pasted_step`) compares like for like and says what it read in its words. A value is
read only from agreed words, never from a dimension line's length, the snapshot's printed scale
(wrong wherever a snapshot is resized) or one engine alone.

`PastedReader` reads a page once (an in-process cache); the OCR engines are passed in (the tests
use stubs) or loaded once by name. Verification: `tests/review/assistant/test_pasted_views.py`.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Protocol

__all__ = [
    "Line",
    "OcrEngines",
    "PastedReader",
    "Shape",
    "View",
    "agreed",
    "architect_stamps",
    "countertop_depths",
    "drawn_splashes",
    "model_tags",
    "splash_words",
    "squash",
]

_log = logging.getLogger(__name__)

type Box = tuple[float, float, float, float]

#: How finely a snapshot is drawn for the OCR engines (pixels per point: 432 dpi), and the most
#: pixels one snapshot is drawn with (a whole pasted sheet is drawn coarser, never bigger).
SCALE: Final = 6.0
MAX_PIXELS: Final = 12_000_000
#: A pixel whose colour components are this far apart is coloured (markup): set to white.
COLOUR_SPREAD: Final = 60
#: Room round a line's box when Tesseract reads it alone (pixels).
_PAD_PX: Final = 4
#: Pages read, kept per process.
_KEPT: Final = 48


# ---- what one view holds ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Line:
    """One line of words both engines read the same."""

    text: str
    box: Box
    """PDF points, origin top left."""


@dataclass(frozen=True, slots=True)
class View:
    """One pasted drawing on an architect page, as code read it."""

    file: str
    page: int
    box: Box
    title: str = ""
    """The agreed title line (`LAVATORY SECTION`), or "" when none was agreed."""
    kind: str = "drawing"
    """`section` (a section or detail), `elevation`, `plan`, or `drawing` (no kind read)."""
    number: str = ""
    """The view's number beside its title (`A3`), when agreed."""
    lines: tuple[Line, ...] = ()
    unagreed: tuple[tuple[str, str, Box], ...] = ()
    """Lines the two engines read differently: (one reading, the other, box). Never a value."""
    splashes: tuple[Box, ...] = ()
    """Splash pieces drawn standing on the top (`drawn_splashes`)."""
    why: str = ""
    """Why nothing was read, when nothing was."""

    @property
    def words(self) -> str:
        """`the section A3 'LAVATORY SECTION' (page 17)`."""
        return f"the {self.name} (page {self.page})"

    @property
    def name(self) -> str:
        """`section A3 'LAVATORY SECTION'`."""
        kind = self.kind if self.kind != "drawing" else "pasted drawing"
        number = f" {self.number}" if self.number else ""
        title = f" '{self.title}'" if self.title else ""
        return f"{kind}{number}{title}"


# ---- agreement ----------------------------------------------------------------------------------

_QUOTES: Final = str.maketrans(
    {"’": "'", "‘": "'", "′": "'", "”": '"', "“": '"', "″": '"', "—": "-", "–": "-"}
)


def squash(text: object) -> str:
    """A reading as the two engines are compared: upper case, only letters, digits, inch and foot
    marks, slashes and dashes, with the leader dashes and stray marks at its ends left off."""
    upper = str(text or "").translate(_QUOTES).upper()
    kept = re.sub(r"[^A-Z0-9'\"/\-]", "", upper)
    return kept.strip("-'\"/")


def _clean(text: str) -> str:
    words = str(text).translate(_QUOTES).upper()
    words = re.sub(r"[^A-Z0-9'\"/\-.,:=#&() ]", " ", words)
    return " ".join(words.split()).strip(" -.,:")


def agreed(first: str, second: str) -> str | None:
    """The line both engines read the same (spaces and stray marks aside), as the reading with the
    word breaks prints it; `None` when they differ or read nothing."""
    a, b = squash(first), squash(second)
    if not a or a != b or not re.search(r"[A-Z0-9]", a):
        return None
    one, other = _clean(first), _clean(second)
    return one if one.count(" ") >= other.count(" ") else other


# ---- the OCR engines ----------------------------------------------------------------------------


class OcrEngines(Protocol):
    """The two engines: one finds and reads every line of a picture, the other reads one line."""

    def lines(self, picture: Any) -> list[tuple[Box, str]]:
        """Every line found: (box in pixels, its reading)."""
        ...

    def line(self, picture: Any) -> str:
        """One line's reading."""
        ...


@dataclass(slots=True)
class _Engines:
    """RapidOCR (detection and recognition) and Tesseract (one line), loaded once."""

    _rapid: Any = None
    _tesseract: Any = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _load(self) -> None:
        if self._rapid is None:
            self._rapid = import_module("rapidocr_onnxruntime").RapidOCR()
        if self._tesseract is None:
            vv = import_module("app.review.assistant.verify_value")
            self._tesseract = vv.lenient_tesseract(import_module("pytesseract"))
            self._tesseract.get_tesseract_version()

    def lines(self, picture: Any) -> list[tuple[Box, str]]:
        with self._lock:
            self._load()
            result, _elapsed = self._rapid(picture)
        out: list[tuple[Box, str]] = []
        for item in result or ():
            points, text = item[0], str(item[1])
            xs = [float(p[0]) for p in points]
            ys = [float(p[1]) for p in points]
            out.append(((min(xs), min(ys), max(xs), max(ys)), text))
        return out

    def line(self, picture: Any) -> str:
        with self._lock:
            self._load()
        return " ".join(
            str(self._tesseract.image_to_string(picture, config="--psm 7", timeout=30)).split()
        )


_DEFAULT_ENGINES: _Engines | None = None
_DEFAULT_LOCK: Final = threading.Lock()


def _default_engines() -> _Engines:
    global _DEFAULT_ENGINES
    with _DEFAULT_LOCK:
        if _DEFAULT_ENGINES is None:
            _DEFAULT_ENGINES = _Engines()
        return _DEFAULT_ENGINES


# ---- the drawn splash ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Shape:
    """A closed piece of a drawing (black or grey ink), in points, origin top left."""

    box: Box


#: A mark this small (points, each way) is one dot of a fill pattern (stipple).
DOT_PT: Final = 0.35
#: A piece is filled when it holds this many dots, this densely (dots per square point).
FILL_DOTS: Final = 20
FILL_DENSITY: Final = 2.0
#: A top is at least this many times as long as it is thick.
TOP_ASPECT: Final = 3.0
#: A splash is at least this many times as tall as the top is thick, and taller than it is wide
#: by this much.
SPLASH_OVER_TOP: Final = 2.0
SPLASH_ASPECT: Final = 2.0
#: Two fills are the same pattern when their densities are within this ratio.
SAME_FILL: Final = 2.0


def _count_inside(box: Box, dots: Sequence[tuple[float, float]]) -> int:
    return sum(1 for x, y in dots if box[0] < x < box[2] and box[1] < y < box[3])


def drawn_splashes(shapes: Sequence[Shape], dots: Sequence[tuple[float, float]]) -> list[Box]:
    """The splash pieces a section draws (see the module): each filled piece standing on a filled
    top's upper edge, flush with one of its ends, with the top's own fill. Pure."""
    filled: list[tuple[Box, float]] = []
    for shape in shapes:
        x0, y0, x1, y1 = shape.box
        area = (x1 - x0) * (y1 - y0)
        if area <= 0:
            continue
        count = _count_inside(shape.box, dots)
        if count >= FILL_DOTS and count / area >= FILL_DENSITY:
            filled.append((shape.box, count / area))
    tops = [(b, d) for b, d in filled if (b[2] - b[0]) >= TOP_ASPECT * (b[3] - b[1])]
    out: list[Box] = []
    for box, density in filled:
        width, height = box[2] - box[0], box[3] - box[1]
        if height < SPLASH_ASPECT * width:
            continue
        for top, top_density in tops:
            thick = top[3] - top[1]
            slack = max(0.6, 0.25 * thick)
            on_top = abs(box[3] - top[1]) <= slack
            within = box[0] >= top[0] - slack and box[2] <= top[2] + slack
            flush = abs(box[0] - top[0]) <= slack or abs(box[2] - top[2]) <= slack
            same = 1 / SAME_FILL <= density / top_density <= SAME_FILL
            if on_top and within and flush and same and height >= SPLASH_OVER_TOP * thick:
                out.append(box)
                break
    return out


# ---- what the words say -------------------------------------------------------------------------

_KIND_WORDS: Final = (
    ("section", re.compile(r"\b(?:DETAILS?|SECTIONS?)\b")),
    ("plan", re.compile(r"\b(?:PLANS?|RCP)\b")),
    ("elevation", re.compile(r"\b(?:ELEVATIONS?|ELEV)\b")),
)
_SCALE_LINE: Final = re.compile(r"\bSCALE\b|=")
_VIEW_NUMBER: Final = re.compile(r"^[A-Z]{0,2}\d{1,3}[A-Z]?$")
#: A line that names a splash, and one that says there is none.
_SPLASH: Final = re.compile(r"\b(?:BACK\s*-?\s*|SIDE\s*-?\s*)?SPLASH(?:ES)?\b")
_NO_SPLASH: Final = re.compile(r"\b(?:NO|NONE|WITHOUT|OMIT\w*|DELETE\w*)\b")


def _kind_of(title: str) -> str:
    for kind, pattern in _KIND_WORDS:
        if pattern.search(title.upper()):
            return kind
    return "drawing"


def _title(lines: Sequence[Line]) -> tuple[str, str, str]:
    """(title, kind, number): the largest agreed line naming a view kind, never a scale line."""
    titled = [
        line
        for line in lines
        if _kind_of(line.text) != "drawing" and not _SCALE_LINE.search(line.text)
    ]
    if not titled:
        return "", "drawing", ""
    best = max(titled, key=lambda one: (one.box[3] - one.box[1], one.box[3]))
    height = best.box[3] - best.box[1]
    number = ""
    for line in lines:
        middle = (line.box[1] + line.box[3]) / 2
        if (
            _VIEW_NUMBER.fullmatch(line.text.replace(" ", ""))
            and line.box[2] <= best.box[0] + height
            and best.box[0] - line.box[2] <= 6 * height
            and best.box[1] - 2 * height <= middle <= best.box[3] + 2 * height
        ):
            number = line.text.replace(" ", "")
            break
    return best.text, _kind_of(best.text), number


def splash_words(view: View) -> list[Line]:
    """The agreed lines of a view that name a splash and do not say there is none."""
    return [
        line
        for line in view.lines
        if _SPLASH.search(line.text) is not None and _NO_SPLASH.search(line.text) is None
    ]


def model_tags(view: View, near_words: re.Pattern[str]) -> list[tuple[str, Line]]:
    """The product tags (`K-2214`) the view's agreed lines print beside words `near_words`
    matches (on the same line, or a line just above or below it, overlapping it across)."""
    tags = import_module("app.review.assistant.tag_reader")
    out: list[tuple[str, Line]] = []
    for line in view.lines:
        keys = tags.tag_keys(line.text)
        if not keys:
            continue
        height = max(line.box[3] - line.box[1], 1.0)
        beside = near_words.search(line.text) is not None or any(
            near_words.search(other.text) is not None
            and other.box[0] < line.box[2]
            and line.box[0] < other.box[2]
            and abs((other.box[1] + other.box[3]) / 2 - (line.box[1] + line.box[3]) / 2)
            <= 2.5 * height
            for other in view.lines
            if other is not line
        )
        if beside:
            out += [(key, line) for key in keys]
    return out


#: `2'-1" COUNTERTOP`: a size printed right before the word for the top (`TOP` alone is not
#: enough: `TOP OF ...` lines are heights).
_DEPTH_LINE: Final = re.compile(
    r"^(?P<size>[0-9][0-9'\"\- /]*[\"'])\s*(?:COUNTER\s*TOP|C\.?T\.?)\b"
)


def countertop_depths(view: View) -> tuple[list[tuple[Decimal, Line]], list[tuple[str, str, Box]]]:
    """The countertop depths a section's agreed words print (`2'-1" COUNTERTOP`: 25"), and the
    lines naming a countertop with a size that the two engines read differently (never a value)."""
    checks = import_module("app.review.assistant.finding_checks")
    read: list[tuple[Decimal, Line]] = []
    for line in view.lines:
        found = _DEPTH_LINE.match(line.text)
        if found is None:
            continue
        value = checks.to_inches(found.group("size").strip())
        if value is not None and value > 0:
            read.append((value, line))
    unread = [
        one
        for one in view.unagreed
        if all(
            re.search(r"COUNTER\s*TOP", side.upper()) and re.search(r"\d", side) for side in one[:2]
        )
    ]
    return read, unread


# ---- reading a page -----------------------------------------------------------------------------


def architect_stamps(
    annotations: Iterable[Mapping[str, Any]], page_map: Mapping[str, Any] | None
) -> dict[tuple[str, int], list[Box]]:
    """The pasted drawings (`/Stamp`s) on the architect's side, by (file, page): on a page whose
    halves the page map gives, only a stamp whose middle lies in an architect half; a page the
    page map does not give, or a half of another role, never."""
    halves: dict[tuple[str, int], list[tuple[Box, str]]] = {}
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        key = (str(entry.get("file")), int(entry["page"]))
        for half in entry.get("halves") or []:
            box = _box(half.get("box")) if isinstance(half, Mapping) else None
            if box is not None:
                halves.setdefault(key, []).append((box, str(half.get("role") or "")))
    out: dict[tuple[str, int], list[Box]] = {}
    for mark in annotations:
        if not isinstance(mark, Mapping) or mark.get("kind") != "Stamp":
            continue
        box = _box(mark.get("box"))
        page = mark.get("page")
        if box is None or not isinstance(page, int):
            continue
        key = (str(mark.get("file")), page)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        roles = [
            role
            for half, role in halves.get(key, [])
            if half[0] <= cx <= half[2] and half[1] <= cy <= half[3]
        ]
        if roles and all(role == "architect" for role in roles):
            out.setdefault(key, []).append(box)
    return out


def _box(value: object) -> Box | None:
    if not isinstance(value, Sequence) or isinstance(value, str) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def _drawing_ink(colour: object) -> bool:
    stamp_text = import_module("extraction.stamp_text")
    return bool(stamp_text.drawing_ink({"non_stroking_color": colour}))


def _ink(path: Mapping[str, Any]) -> bool:
    """A path drawn in black or grey (its line if stroked, its fill if filled)."""
    shown = []
    if path.get("stroke"):
        shown.append(path.get("stroking_color"))
    if path.get("fill"):
        shown.append(path.get("non_stroking_color"))
    return bool(shown) and all(_drawing_ink(c) for c in shown)


def _closed(path: Mapping[str, Any]) -> bool:
    if path.get("object_type") == "rect":
        return True
    points = path.get("pts") or []
    if len(points) < 4:
        return False
    (ax, ay), (bx, by) = points[0], points[-1]
    return abs(float(ax) - float(bx)) < 0.05 and abs(float(ay) - float(by)) < 0.05


def _geometry(plumber_page: Any) -> tuple[list[Shape], list[tuple[float, float]]]:
    shapes: list[Shape] = []
    dots: list[tuple[float, float]] = []
    for kind in ("lines", "curves", "rects"):
        for path in getattr(plumber_page, kind, ()) or ():
            if not _ink(path):
                continue
            x0, x1 = float(path["x0"]), float(path["x1"])
            top, bottom = float(path["top"]), float(path["bottom"])
            if x1 - x0 < DOT_PT and bottom - top < DOT_PT:
                dots.append(((x0 + x1) / 2, (top + bottom) / 2))
            elif x1 - x0 > 0.8 and bottom - top > 0.8 and _closed(path):
                shapes.append(Shape((x0, top, x1, bottom)))
    return shapes, dots


def _uncoloured(picture: Any) -> Any:
    np = import_module("numpy")
    rgb = np.array(picture.convert("RGB"))
    spread = rgb.max(axis=2).astype(int) - rgb.min(axis=2).astype(int)
    rgb[spread > COLOUR_SPREAD] = 255
    return rgb


def read_view(
    file: str,
    page: int,
    box: Box,
    picture: Any,
    scale: float,
    engines: OcrEngines,
    geometry: tuple[Sequence[Shape], Sequence[tuple[float, float]]],
) -> View:
    """One pasted drawing: its agreed words, title and drawn splashes (`picture`: the snapshot
    drawn alone at `scale` pixels per point, coloured ink already white)."""
    lines: list[Line] = []
    unagreed: list[tuple[str, str, Box]] = []
    height, width = picture.shape[0], picture.shape[1]
    for (px0, py0, px1, py1), first in engines.lines(picture):
        crop = picture[
            max(0, int(py0) - _PAD_PX) : min(height, int(py1) + _PAD_PX),
            max(0, int(px0) - _PAD_PX) : min(width, int(px1) + _PAD_PX),
        ]
        if crop.size == 0:
            continue
        second = engines.line(crop)
        at = (
            box[0] + px0 / scale,
            box[1] + py0 / scale,
            box[0] + px1 / scale,
            box[1] + py1 / scale,
        )
        text = agreed(first, second)
        if text is None:
            if squash(first) or squash(second):
                unagreed.append((" ".join(first.split()), " ".join(second.split()), at))
            continue
        lines.append(Line(text, at))
    title, kind, number = _title(lines)
    shapes, dots = geometry
    inside = [s for s in shapes if _within(s.box, box)]
    mine = [d for d in dots if box[0] <= d[0] <= box[2] and box[1] <= d[1] <= box[3]]
    return View(
        file=file,
        page=page,
        box=box,
        title=title,
        kind=kind,
        number=number,
        lines=tuple(lines),
        unagreed=tuple(unagreed),
        splashes=tuple(drawn_splashes(inside, mine)),
    )


def _within(inner: Box, outer: Box) -> bool:
    return (
        outer[0] - 0.5 <= inner[0]
        and inner[2] <= outer[2] + 0.5
        and outer[1] - 0.5 <= inner[1]
        and inner[3] <= outer[3] + 0.5
    )


@dataclass(slots=True)
class PastedReader:
    """The pasted drawings of an architect page, read once per process (see the module)."""

    pdf_of: Callable[[str], Path | None]
    """The PDF a file's pages are read from (the set pack's markup-free copy, else the upload)."""
    stamps: Mapping[tuple[str, int], Sequence[Box]]
    """The architect's pasted drawings by (file, page) (`architect_stamps`)."""
    engines: OcrEngines | None = None
    _kept: OrderedDict[tuple[str, int, str], tuple[View, ...]] = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_pack(
        cls,
        pack: Path | None,
        uploads: Path | None = None,
        *,
        page_map: Mapping[str, Any] | None = None,
        engines: OcrEngines | None = None,
    ) -> PastedReader:
        """A reader for a set pack: its `annotations.json` stamps on the architect's side (the
        page map's), each file read from the pack's markup-free copy, else from `uploads`."""
        numbers = import_module("app.review.assistant.read_number")
        pdf_of = numbers.reader_for_pack(pack, uploads).pdf_of
        marks: Any = None
        if pack is not None:
            marks = _read_json(pack / "annotations.json")
            if page_map is None:
                found = _read_json(pack / "page_map.json")
                page_map = found if isinstance(found, Mapping) else None
        stamps = architect_stamps(marks if isinstance(marks, list) else [], page_map)
        return cls(pdf_of, stamps, engines)

    def __call__(self, file: str, page: int) -> tuple[View, ...]:
        boxes = list(self.stamps.get((file, page)) or ())
        if not boxes:
            return ()
        pdf = self.pdf_of(file)
        if pdf is None:
            return ()
        key = (str(pdf), page, file)
        with self._lock:
            if key in self._kept:
                self._kept.move_to_end(key)
                return self._kept[key]
        try:
            views = self._read(pdf, file, page, boxes)
        except Exception:  # noqa: BLE001 - a page that cannot be read gives no evidence
            _log.warning("pasted views: page %s of %s not read", page, file, exc_info=True)
            views = ()
        with self._lock:
            self._kept[key] = views
            while len(self._kept) > _KEPT:
                self._kept.popitem(last=False)
        return views

    def _read(self, pdf: Path, file: str, page: int, boxes: Sequence[Box]) -> tuple[View, ...]:
        stamp_text = import_module("extraction.stamp_text")
        pdfium = import_module("pypdfium2")
        pdfplumber = import_module("pdfplumber")
        engines = self.engines or _default_engines()
        flattened = stamp_text.stamps_only(pdf.read_bytes(), page - 1)
        with pdfplumber.open(io.BytesIO(flattened)) as document:
            geometry = _geometry(document.pages[page - 1])
        drawn = pdfium.PdfDocument(flattened)
        try:
            sheet = drawn[page - 1]
            width, height = sheet.get_size()
            out: list[View] = []
            for box in boxes:
                area = max((box[2] - box[0]) * (box[3] - box[1]), 1.0)
                scale = min(SCALE, (MAX_PIXELS / area) ** 0.5)
                bitmap = sheet.render(
                    scale=scale,
                    crop=(box[0], height - box[3], width - box[2], box[1]),
                )
                picture = _uncoloured(bitmap.to_pil())
                out.append(read_view(file, page, box, picture, scale, engines, geometry))
            return tuple(out)
        finally:
            drawn.close()


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
