"""GV's stickers: the notes GV's earlier review left over the vendor's ink (#1859), by code, $0.

GV's reviewer marks the vendor's sheet (a yellow box with red text, "25-1/2"" over the vendor's
"610 [24"]"), flattens the marks, and pastes the marked sheet into the next round's set as one
drawing. The marks are then part of the pasted drawing: no annotation of their own, so they are not
in the markup list (`annotations.json`) and the drawings-only copy kept them. On the large test set
an AI read three of GV's stickers as the vendor's cabinet widths. **GV's values are never the
vendor's**, so code finds the stickers, hides them from what the AI and code's readers see, and
refuses any vendor value claimed inside one (`escalation`'s sticker step).

**What a sticker is** (the colour rule GV's other markup is read by: `extraction.stamp_text` never
reads coloured text, `under_markup` hides coloured boxes; drawings are plotted in black):

- **a note inside a pasted drawing**: a small sub-drawing (form XObject, at most `SMALL_SHARE` of
  the drawing) whose every mark is in colour (a yellow or coloured box, coloured lines, coloured
  text; a white box is allowed) and that shows text. One black or grey mark, a shading or a
  picture, and it is the vendor's, kept;
- **a picture of one**: a small picture (pasted as a comment, or inside a pasted drawing) whose
  pixels are mostly GV's light yellow.

Only pasted drawings are searched (`extraction.stamp_text.carries_drawing`, and pasted pictures):
GV's other markup is its own annotations, which the drawings-only copy already drops.

Each sticker is listed with its box (PDF points of the page as shown, origin top-left, as
`pdftotext -bbox` and `report_finding`), its page and its text. `hide` empties them in an open
document (a copy: the upload is never changed).

**Loaded by name, never imported** by `app/api/` (pikepdf, Pillow).
Verification: `tests/review/assistant/test_gv_stickers.py` (made-up stickers).
"""

from __future__ import annotations

import io
import json
import logging
import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pikepdf

__all__ = [
    "SMALL_SHARE",
    "STICKERS_SUFFIX",
    "Sticker",
    "find",
    "hide",
    "read_folder",
    "sticker_at",
    "stickers_of",
    "write_file",
]

_log = logging.getLogger(__name__)

#: A sub-drawing is a sticker only when it covers at most this share of its pasted drawing (a
#: whole-drawing fill is the vendor's background, never a note).
SMALL_SHARE: Final = 0.08
#: A colour is "in colour" when its channels differ by more than this (0..1): yellow, red, green.
_COLOURED: Final = 0.25
#: A colour this light in every channel is white (a note's white box).
_WHITE: Final = 0.95
#: Sub-drawings searched this deep.
_MAX_DEPTH: Final = 4
#: A picture is GV's yellow note when at least this share of its pixels is light yellow.
_YELLOW_SHARE: Final = 0.5
#: The largest pasted picture taken for a sticker (points squared, about 4 x 3 inches).
_PICTURE_MAX_PT2: Final = 300.0 * 220.0
#: The set pack's file per upload: `<stem>_stickers.json`.
STICKERS_SUFFIX: Final = "_stickers.json"

_FILL: Final = frozenset({"f", "F", "f*"})
_STROKE: Final = frozenset({"S", "s"})
_BOTH: Final = frozenset({"B", "B*", "b", "b*"})
_SHOW: Final = frozenset({"Tj", "TJ", "'", '"'})
#: Operators that paint nothing and change no colour: state, paths, clipping, text set-up.
_QUIET: Final = frozenset(
    {"q", "Q", "cm", "w", "J", "j", "M", "d", "ri", "i", "gs", "m", "l", "c", "v", "y", "h"}
    | {"re", "W", "W*", "n", "BT", "ET", "Tf", "Tm", "Td", "TD", "T*", "Tc", "Tw", "Tz", "TL"}
    | {"Ts", "BX", "EX", "MP", "DP", "BMC", "BDC", "EMC", "d0", "d1"}
)
_SPACES: Final[Mapping[str, int]] = {"/DeviceGray": 1, "/DeviceRGB": 3, "/DeviceCMYK": 4}

type Box = tuple[float, float, float, float]
type Matrix = tuple[float, float, float, float, float, float]

_IDENTITY: Final[Matrix] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class Sticker:
    """One of GV's stickers on a page."""

    page: int
    """The page (from 1)."""
    box: Box
    """Points of the page as shown, origin top-left."""
    text: str
    """Its text as set (empty for a picture)."""
    kind: str
    """`note` (a coloured note inside a pasted drawing) or `picture`."""

    def as_dict(self, file: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "page": self.page,
            "box": [round(v, 1) for v in self.box],
            "text": self.text,
            "kind": self.kind,
        }
        if file is not None:
            out["file"] = file
        return out


# ---- the colour rule -----------------------------------------------------------------------------


def _tone(numbers: Sequence[float]) -> str:
    """`colour`, `white` or `grey` (black and grey: the drawing's own ink)."""
    if len(numbers) == 1:
        return "white" if numbers[0] >= _WHITE else "grey"
    if len(numbers) == 3:
        if max(numbers) - min(numbers) > _COLOURED:
            return "colour"
        return "white" if min(numbers) >= _WHITE else "grey"
    if len(numbers) == 4:  # CMYK: no ink at all is white; cyan, magenta or yellow ink is colour
        if max(numbers) <= 1 - _WHITE:
            return "white"
        if max(numbers[:3]) - min(numbers[:3]) > _COLOURED and numbers[3] < 0.5:
            return "colour"
        return "grey"
    return "unknown"


def _numbers(operands: Sequence[Any]) -> list[float] | None:
    out: list[float] = []
    for value in operands:
        try:
            out.append(float(value))
        except (TypeError, ValueError):
            return None  # a pattern's name: not a plain colour
    return out


@dataclass(slots=True)
class _State:
    fill: str = "grey"
    stroke: str = "grey"
    fill_space: int | None = 1
    stroke_space: int | None = 1
    render: int = 0


@dataclass(slots=True)
class _Marks:
    """What one sub-drawing paints, by the colour rule."""

    ink: bool = False
    """Something black or grey, a shading, a picture or an unknown mark: the vendor's."""
    text: list[str] | None = None
    coloured_text: int = 0

    def words(self) -> str:
        return " ".join(" ".join(self.text or []).split())


def _shown_text(operands: Sequence[Any], name: str) -> str:
    def decoded(value: Any) -> str:
        if isinstance(value, pikepdf.String):
            return bytes(value).decode("latin-1", "replace")
        return ""

    if name == "TJ" and operands and isinstance(operands[0], pikepdf.Array):
        parts: list[str] = []
        for value in operands[0]:
            if isinstance(value, pikepdf.String):
                parts.append(decoded(value))
            else:
                try:
                    if float(value) < -200:
                        parts.append(" ")
                except (TypeError, ValueError):
                    continue
        return "".join(parts)
    return decoded(operands[-1]) if operands else ""


def _walk(form: Any, state: _State, marks: _Marks, depth: int, seen: set[tuple[int, int]]) -> None:
    """Every mark of `form` (and the sub-drawings it draws) against the colour rule."""
    key = form.objgen
    if key != (0, 0):
        if key in seen:
            return
        seen.add(key)
    try:
        instructions = pikepdf.parse_content_stream(form)
    except (pikepdf.PdfError, TypeError, ValueError):
        marks.ink = True
        return
    resources = form.get("/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    stack: list[_State] = []
    for instruction in instructions:
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            marks.ink = True
            continue
        operands, name = list(instruction.operands), str(instruction.operator)
        if name == "q":
            stack.append(_State(**{f: getattr(state, f) for f in _State.__slots__}))
        elif name == "Q":
            if stack:
                state = stack.pop()
        elif name in ("rg", "k", "g"):
            found = _numbers(operands)
            state.fill = _tone(found) if found is not None else "unknown"
        elif name in ("RG", "K", "G"):
            found = _numbers(operands)
            state.stroke = _tone(found) if found is not None else "unknown"
        elif name in ("cs", "CS"):
            count = _SPACES.get(str(operands[0])) if operands else None
            tone = "grey" if count is not None else "unknown"
            if name == "cs":
                state.fill, state.fill_space = tone, count
            else:
                state.stroke, state.stroke_space = tone, count
        elif name in ("sc", "scn", "SC", "SCN"):
            found = _numbers(operands)
            space = state.fill_space if name in ("sc", "scn") else state.stroke_space
            tone = (
                _tone(found)
                if found is not None and space is not None and len(found) == space
                else "unknown"
            )
            if name in ("sc", "scn"):
                state.fill = tone
            else:
                state.stroke = tone
        elif name == "Tr":
            try:
                state.render = int(operands[0])
            except (IndexError, TypeError, ValueError):
                state.render = 0
        elif name in _FILL:
            if state.fill not in ("colour", "white"):
                marks.ink = True
        elif name in _STROKE:
            if state.stroke not in ("colour", "white"):
                marks.ink = True
        elif name in _BOTH:
            if state.fill not in ("colour", "white") or state.stroke not in ("colour", "white"):
                marks.ink = True
        elif name in _SHOW:
            mode = state.render % 4
            tones = (
                []
                if mode == 3
                else (
                    [state.fill]
                    if mode == 0
                    else [state.stroke] if mode == 1 else [state.fill, state.stroke]
                )
            )
            if any(tone != "colour" for tone in tones):
                marks.ink = True
            elif tones:
                marks.coloured_text += 1
                marks.text = [*(marks.text or []), _shown_text(operands, name)]
        elif name == "Do":
            target = xobjects.get(str(operands[0])) if xobjects is not None and operands else None
            if (
                isinstance(target, pikepdf.Stream)
                and target.get("/Subtype") == "/Form"
                and depth < _MAX_DEPTH
            ):
                inner = _State(**{f: getattr(state, f) for f in _State.__slots__})
                _walk(target, inner, marks, depth + 1, seen)
            else:
                marks.ink = True  # a picture, or a form too deep to follow
        elif name not in _QUIET:
            marks.ink = True  # a shading or an operator this rule does not know: the vendor's
        if marks.ink:
            return


def note_form(form: Any) -> str | None:
    """The text of a sub-drawing that is one of GV's notes by the colour rule (every mark in
    colour, white boxes allowed, and coloured text shown), else `None`. Size is the caller's."""
    marks = _Marks()
    _walk(form, _State(), marks, 0, set())
    if marks.ink or not marks.coloured_text:
        return None
    return marks.words()


def _bbox_area(form: Any) -> float:
    try:
        x0, y0, x1, y1 = (float(v) for v in form.get("/BBox", [0, 0, 0, 0]))
    except (TypeError, ValueError):
        return 0.0
    return abs(x1 - x0) * abs(y1 - y0)


# ---- pictures ------------------------------------------------------------------------------------


def _yellow_picture(image: Any) -> bool:
    """A picture whose pixels are mostly GV's light yellow (a note pasted as a picture)."""
    try:
        picture = pikepdf.PdfImage(image).as_pil_image().convert("RGB")
    except Exception:  # noqa: BLE001 - a picture code cannot decode is the vendor's
        return False
    picture.thumbnail((64, 64))
    pixels = list(picture.getdata())
    if not pixels:
        return False
    yellow = sum(1 for r, g, b in pixels if r >= 230 and g >= 215 and b <= 180)
    return yellow / len(pixels) >= _YELLOW_SHARE


# ---- geometry ------------------------------------------------------------------------------------


def _multiply(a: Matrix, b: Matrix) -> Matrix:
    """`a` then `b` (PDF row-vector order: a point is transformed by `a`, then by `b`)."""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    return m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]


def _matrix(raw: Any) -> Matrix:
    try:
        values = tuple(float(v) for v in raw)
    except (TypeError, ValueError):
        return _IDENTITY
    if len(values) != 6:
        return _IDENTITY
    return (values[0], values[1], values[2], values[3], values[4], values[5])


def _bounds(m: Matrix, box: Box) -> Box:
    corners = [_apply(m, x, y) for x in (box[0], box[2]) for y in (box[1], box[3])]
    xs, ys = [c[0] for c in corners], [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox(form: Any) -> Box | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in form.get("/BBox"))
    except (TypeError, ValueError):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _appearance_to_page(annotation: Any, normal: Any) -> Matrix | None:
    """The appearance's space to the page's (PDF 32000 12.5.5: its box, through its matrix, is
    fitted to the annotation's rectangle)."""
    box = _bbox(normal)
    try:
        rect = [float(v) for v in annotation.get("/Rect")]
    except (TypeError, ValueError):
        return None
    if box is None or len(rect) != 4:
        return None
    own = _matrix(normal.get("/Matrix", _IDENTITY))
    x0, y0, x1, y1 = _bounds(own, box)
    if x1 - x0 <= 0 or y1 - y0 <= 0:
        return None
    rx0, ry0, rx1, ry1 = (
        min(rect[0], rect[2]),
        min(rect[1], rect[3]),
        max(rect[0], rect[2]),
        max(rect[1], rect[3]),
    )
    sx, sy = (rx1 - rx0) / (x1 - x0), (ry1 - ry0) / (y1 - y0)
    fit: Matrix = (sx, 0.0, 0.0, sy, rx0 - x0 * sx, ry0 - y0 * sy)
    return _multiply(own, fit)


def _shown(page: Any) -> Callable[[float, float], tuple[float, float]]:
    """PDF user space to the page as shown (points, origin top-left, rotation applied)."""
    raw = page.obj.get("/CropBox", page.mediabox)
    box = [float(v) for v in raw]
    llx, lly, urx, ury = (
        min(box[0], box[2]),
        min(box[1], box[3]),
        max(box[0], box[2]),
        max(box[1], box[3]),
    )
    rotation = int(page.obj.get("/Rotate", 0) or 0) % 360

    def shown(x: float, y: float) -> tuple[float, float]:
        if rotation == 90:
            return y - lly, x - llx
        if rotation == 180:
            return urx - x, y - lly
        if rotation == 270:
            return ury - y, urx - x
        return x - llx, ury - y

    return shown


def _shown_box(shown: Callable[[float, float], tuple[float, float]], box: Box) -> Box:
    a, b = shown(box[0], box[1]), shown(box[2], box[3])
    return min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])


# ---- finding them --------------------------------------------------------------------------------


@dataclass(slots=True)
class _Found:
    sticker: Sticker
    hide: Callable[[], None]


def _in_drawing(
    number: int, annotation: Any, shown: Callable[[float, float], tuple[float, float]]
) -> list[_Found]:
    """GV's stickers inside one pasted drawing's appearance (its own sub-drawings, and pictures it
    draws), each with how to hide it."""
    appearance = annotation.get("/AP")
    normal = appearance.get("/N") if isinstance(appearance, pikepdf.Dictionary) else None
    if not isinstance(normal, pikepdf.Stream):
        return []
    to_page = _appearance_to_page(annotation, normal)
    resources = normal.get("/Resources")
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    if to_page is None or not isinstance(xobjects, pikepdf.Dictionary):
        return []
    limit = max(_bbox_area(normal), 1.0) * SMALL_SHARE
    try:
        instructions = list(pikepdf.parse_content_stream(normal))
    except (pikepdf.PdfError, TypeError, ValueError):
        return []
    found: list[_Found] = []
    hidden_images: set[int] = set()
    ctm: Matrix = _IDENTITY
    stack: list[Matrix] = []
    for index, instruction in enumerate(instructions):
        if isinstance(instruction, pikepdf.ContentStreamInlineImage):
            continue
        operands, name = list(instruction.operands), str(instruction.operator)
        if name == "q":
            stack.append(ctm)
        elif name == "Q":
            ctm = stack.pop() if stack else _IDENTITY
        elif name == "cm":
            ctm = _multiply(_matrix(operands), ctm)
        elif name == "Do" and operands:
            target = xobjects.get(str(operands[0]))
            if not isinstance(target, pikepdf.Stream):
                continue
            if target.get("/Subtype") == "/Form":
                box = _bbox(target)
                area = _bbox_area(target) * abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])
                if box is None or not 0 < area <= limit:
                    continue
                text = note_form(target)
                if text is None:
                    continue
                inner = _multiply(
                    _multiply(_matrix(target.get("/Matrix", _IDENTITY)), ctm), to_page
                )
                where = _shown_box(shown, _bounds(inner, box))

                def empty(form: Any = target) -> None:
                    form.write(b"")

                found.append(_Found(Sticker(number, where, text, "note"), empty))
            elif target.get("/Subtype") == "/Image":
                area = abs(ctm[0] * ctm[3] - ctm[1] * ctm[2])  # an image is the unit square
                if not 0 < area <= limit or not _yellow_picture(target):
                    continue
                where = _shown_box(shown, _bounds(_multiply(ctm, to_page), (0.0, 0.0, 1.0, 1.0)))
                hidden_images.add(index)
                found.append(_Found(Sticker(number, where, "", "picture"), lambda: None))
    if hidden_images:
        kept = [i for at, i in enumerate(instructions) if at not in hidden_images]

        def drop_pictures(form: Any = normal, kept: list[Any] = kept) -> None:
            form.write(pikepdf.unparse_content_stream(kept))

        found.append(_Found(found[-1].sticker, drop_pictures))
    return found


def _picture_comment(
    number: int, annotation: Any, shown: Callable[[float, float], tuple[float, float]]
) -> _Found | None:
    """A small picture pasted as a comment whose pixels are mostly GV's yellow."""
    stamp_text = _stamp_text()
    if not stamp_text.pasted_picture(annotation):
        return None
    try:
        rect = [float(v) for v in annotation.get("/Rect")]
    except (TypeError, ValueError):
        return None
    if len(rect) != 4 or abs(rect[2] - rect[0]) * abs(rect[3] - rect[1]) > _PICTURE_MAX_PT2:
        return None
    normal = annotation.get("/AP", {}).get("/N")
    resources = normal.get("/Resources") if isinstance(normal, pikepdf.Stream) else None
    xobjects = resources.get("/XObject") if isinstance(resources, pikepdf.Dictionary) else None
    held = [x for _k, x in xobjects.items()] if isinstance(xobjects, pikepdf.Dictionary) else []
    images = [x for x in held if isinstance(x, pikepdf.Stream) and x.get("/Subtype") == "/Image"]
    if not images or not all(_yellow_picture(image) for image in images):
        return None
    box = (
        min(rect[0], rect[2]),
        min(rect[1], rect[3]),
        max(rect[0], rect[2]),
        max(rect[1], rect[3]),
    )

    def empty(form: Any = normal) -> None:
        form.write(b"")

    return _Found(Sticker(number, _shown_box(shown, box), "", "picture"), empty)


def _stamp_text() -> Any:
    from importlib import import_module

    return import_module("extraction.stamp_text")


def _find(pdf: pikepdf.Pdf) -> list[_Found]:
    stamp_text = _stamp_text()
    found: list[_Found] = []
    for number, page in enumerate(pdf.pages, start=1):
        shown = _shown(page)
        for annotation in page.obj.get("/Annots") or []:
            try:
                if stamp_text.carries_drawing(annotation):
                    found += _in_drawing(number, annotation, shown)
                else:
                    picture = _picture_comment(number, annotation, shown)
                    if picture is not None:
                        found.append(picture)
            except (pikepdf.PdfError, TypeError, ValueError, AttributeError, KeyError) as failed:
                _log.warning("GV stickers: page %s not searched: %s", number, failed)
    return found


def _unique(found: Iterable[_Found]) -> list[Sticker]:
    out: list[Sticker] = []
    for each in found:
        if each.sticker not in out:
            out.append(each.sticker)
    return out


def find(pdf: pikepdf.Pdf) -> list[Sticker]:
    """GV's stickers in an open document (nothing changed)."""
    return _unique(_find(pdf))


def hide(pdf: pikepdf.Pdf) -> list[Sticker]:
    """GV's stickers in an open document, each emptied in it (a copy: never the upload)."""
    found = _find(pdf)
    for each in found:
        each.hide()
    return _unique(found)


def stickers_of(source: Path) -> list[Sticker]:
    """GV's stickers in a PDF file (read only). None found when the file cannot be read."""
    try:
        with pikepdf.open(str(source)) as pdf:
            return find(pdf)
    except Exception as failed:  # noqa: BLE001 - no stickers known: the readers still run
        _log.warning("GV stickers not searched: %s", type(failed).__name__)
        return []


def hidden_copy(source: Path | io.BytesIO) -> tuple[io.BytesIO | Path, list[Sticker]]:
    """`source` with GV's stickers emptied (in memory), and the stickers; `source` itself when it
    has none or cannot be read."""
    try:
        if isinstance(source, io.BytesIO):
            source.seek(0)
        with pikepdf.open(source if isinstance(source, io.BytesIO) else str(source)) as pdf:
            found = hide(pdf)
            if not found:
                if isinstance(source, io.BytesIO):
                    source.seek(0)
                return source, []
            buffer = io.BytesIO()
            pdf.save(buffer)
    except Exception as failed:  # noqa: BLE001 - the copy is made with them, as before
        _log.warning("GV stickers not hidden: %s", type(failed).__name__)
        if isinstance(source, io.BytesIO):
            source.seek(0)
        return source, []
    buffer.seek(0)
    return buffer, found


# ---- the set pack's file -------------------------------------------------------------------------


def write_file(path: Path, file: str, stickers: Sequence[Sticker]) -> None:
    """`<stem>_stickers.json`: the upload's stickers (written whole, renamed into place)."""
    body = json.dumps(
        {"file": file, "stickers": [s.as_dict() for s in stickers]}, ensure_ascii=False, indent=1
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    temporary.write_text(body, encoding="utf-8")
    temporary.chmod(0o644)
    os.replace(temporary, path)


def read_folder(folder: Path | None) -> dict[tuple[str, int], list[dict[str, Any]]] | None:
    """Every upload's stickers in a set pack, by (file, page); `None` when the pack lists none
    (an older pack: the caller may search the uploads)."""
    if folder is None or not folder.is_dir():
        return None
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    files = sorted(folder.glob(f"*{STICKERS_SUFFIX}"))
    if not files:
        return None
    for path in files:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        file = str(raw.get("file") or "") if isinstance(raw, Mapping) else ""
        for row in raw.get("stickers") or [] if isinstance(raw, Mapping) else []:
            if isinstance(row, Mapping) and isinstance(row.get("page"), int):
                out.setdefault((file, int(row["page"])), []).append(dict(row, file=file))
    return out


def by_page(stickers: Iterable[Sticker], file: str) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Stickers as `read_folder` gives them."""
    out: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for sticker in stickers:
        out.setdefault((file, sticker.page), []).append(sticker.as_dict(file))
    return out


def sticker_at(
    stickers: Sequence[Mapping[str, Any]], box: Sequence[float], slack: float = 2.0
) -> Mapping[str, Any] | None:
    """The sticker a value's box sits in: its middle inside the sticker (with `slack` points), or
    half of the value's box under it. The biggest overlap when several."""
    x0, y0, x1, y1 = (float(v) for v in box)
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    area = max((x1 - x0) * (y1 - y0), 1e-6)
    best: tuple[float, Mapping[str, Any]] | None = None
    for sticker in stickers:
        try:
            sx0, sy0, sx1, sy1 = (float(v) for v in sticker.get("box") or ())
        except (TypeError, ValueError):
            continue
        overlap = max(0.0, min(x1, sx1) - max(x0, sx0)) * max(0.0, min(y1, sy1) - max(y0, sy0))
        middle = sx0 - slack <= cx <= sx1 + slack and sy0 - slack <= cy <= sy1 + slack
        if (middle or overlap / area >= 0.5) and (best is None or overlap > best[0]):
            best = (overlap, sticker)
    return best[1] if best is not None else None
