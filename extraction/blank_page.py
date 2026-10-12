"""Proving from the PDF that a page has nothing drawn on it: a title or divider page (#1195).

**Why.** Every page code reads no countertop row on is listed under "Pages not checked" and, since
#1191, needs a reviewer's answer before sign-off. On a combined review set most of those pages are
GV's own title and divider sheets: a page number and a project name, or a section label, typed on an
otherwise empty sheet. A reviewer clicking "no countertop here" on each one adds nothing. A page
this module **proves** blank is still listed, with its reason, but needs no click.

**What "blank" means, exactly.** All of these hold, or the page is not proven blank and still needs
the reviewer:

1. **No pasted drawing.** No `/Stamp` annotation (the vendor's and the architect's drawings are
   pasted in as stamps, `extraction/annotations.py`).
2. **No picture.** No image anywhere: no image XObject, no inline image, in the page's content, in a
   form it draws, or in a text box's appearance.
3. **No other markup.** No annotation except typed text boxes (`/FreeText`) and their pop-ups
   (`/Popup`, which draw nothing). A line, a cloud, a pen stroke or a shape annotation is ink; so is
   a text box with a callout line (`/CL`, `/IT /FreeTextCallout`) or with no appearance of its own
   (a viewer draws it however it likes).
4. **No drawn ink beyond the page frame and the typed title.** In the page's content (and every form
   it draws), each painted path is one of:
   - **the page frame:** one closed, stroked, axis-aligned rectangle (no fill, no curves) that runs
     along the page edges, covering at least 90% of the page's width and height;
   - **white paint:** a path filled, stroked or both only in pure white (gray 1, RGB 1 1 1 or CMYK
     0 0 0 0) under a plain graphics state: on white paper it leaves no mark. Flattened text boxes
     carry one as their background or border. A blend mode other than Normal, a soft mask, a
     transfer function or overprint makes white count as drawn; a graphics state that cannot be
     found makes the page unreadable.
   Anything else painted — a line, even one underlining the title, a stroke that is not the frame,
   a coloured fill, a curve, a shading, a pattern — is drawn ink. A clip (`W n`) paints nothing.
   Inside a typed text box's appearance, only its text and at most two rectangles (its background
   and border) lying within the box's `/Rect` are allowed.
5. **Only a title's worth of typed text.** Text drawn with a font (in a `/FreeText` box, or in the
   page's content, as flattened text boxes do) is typed text, not drawing, but only in a font that
   can be found and is not a Type3 font (a Type3 glyph is a drawing): otherwise the page is not
   proven blank. At most
   `MAX_TITLE_CHARACTERS` characters in all: a title or a section label, not a page of notes.
   Text converted to outlines is paths, so it is ink (rule 4) and the page needs a click.

**Why the page frame counts as "nothing drawn".** GV's sheet template draws the same single border
rectangle on every page of a combined set, drawing pages included. It encloses the sheet and measures
nothing: it carries no tick, no number and no extension line, and a countertop row needs dimension
lines and their numbers. Requiring the rectangle to run along the page edges (at least 90% of the
page's width and height) keeps a drawn box anywhere else on the page from passing as a frame.

**Fail-safe.** Any part of the page that cannot be read (a malformed stream, an unknown XObject, a
missing graphics state or font, forms nested deeper than `_MAX_FORM_DEPTH`) means "not proven": the
page keeps needing a reviewer's answer. Resources and the page box are looked up through the page
tree, as a viewer inherits them. Dictionary values and content-stream operators only: no rendering, no
model, $0.

Source: issue #1195. Verification: `tests/extraction/test_blank_page.py`.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

import pikepdf

from vocabulary.blank_page import BLANK_PAGE_RULE, MAX_TITLE_CHARACTERS

__all__ = [
    "BLANK_PAGE_RULE",
    "MAX_TITLE_CHARACTERS",
    "BlankPageFacts",
    "prove_blank_pages",
]


#: How much of the page's width and height the frame rectangle must cover.
_FRAME_COVER: Final = 0.9

#: How far a frame corner may be from a rectangle's, in points (float noise in a written path).
_CORNER_SLACK: Final = 0.5

#: Forms nested deeper than this are not followed: the page is not proven blank.
_MAX_FORM_DEPTH: Final = 12

_TEXT_SHOWS: Final = frozenset({"Tj", "TJ", "'", '"'})
_STROKES: Final = frozenset({"S", "s"})
_FILLS: Final = frozenset({"f", "F", "f*"})
_FILL_AND_STROKE: Final = frozenset({"B", "B*", "b", "b*"})
_NORMAL_BLENDS: Final = frozenset({"/Normal", "/Compatible"})
_DEVICE_SPACES: Final = {"g": "/DeviceGray", "rg": "/DeviceRGB", "k": "/DeviceCMYK"}
_COLOUR_OPERATORS: Final = frozenset(
    {"g", "rg", "k", "cs", "sc", "scn", "G", "RG", "K", "CS", "SC", "SCN"}
)
#: Annotations that draw nothing but typed text (and a pop-up's note, which is never drawn).
_TEXT_ANNOTATIONS: Final = frozenset({"/FreeText", "/Popup"})
#: A typed text box's own rectangles: its background and its border.
_TEXT_BOX_RECTANGLES: Final = 2
#: How far up the page tree an inherited attribute is looked for.
_MAX_TREE_DEPTH: Final = 32

type Matrix = tuple[float, float, float, float, float, float]
_IDENTITY: Final[Matrix] = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(frozen=True, slots=True)
class BlankPageFacts:
    """What was checked on one page, and what was found. `blank` only when every count allows it."""

    stamps: int = 0
    """Pasted drawings (`/Stamp` annotations)."""
    other_annotations: int = 0
    """Annotations other than typed text boxes: lines, clouds, shapes, pen strokes."""
    images: int = 0
    """Pictures: image XObjects and inline images, anywhere on the page."""
    drawn_paths: int = 0
    """Painted paths that are neither the page frame nor white paint (and shadings)."""
    frames: int = 0
    """Page-frame rectangles (allowed)."""
    white_paint: int = 0
    """Paths painted only in pure white (allowed): they leave no mark."""
    typed_texts: int = 0
    """Pieces of typed text: text-showing operators in the page's content and in text boxes."""
    typed_characters: int = 0
    """Their characters, counted as stored bytes (two-byte fonts count double: the safe way)."""
    unreadable: str | None = None
    """Why part of the page could not be read; the page is then not proven blank."""

    @property
    def blank(self) -> bool:
        return (
            self.unreadable is None
            and self.stamps == 0
            and self.other_annotations == 0
            and self.images == 0
            and self.drawn_paths == 0
            and self.typed_characters <= MAX_TITLE_CHARACTERS
        )


@dataclass(slots=True)
class _Counts:
    images: int = 0
    drawn_paths: int = 0
    frames: int = 0
    white_paint: int = 0
    rectangles: int = 0
    typed_texts: int = 0
    typed_characters: int = 0


@dataclass(slots=True)
class _State:
    """The graphics state that decides whether paint leaves a mark: where, and in what colour."""

    ctm: Matrix = _IDENTITY
    fill_white: bool = False
    fill_space: str = "/DeviceGray"
    stroke_white: bool = False
    stroke_space: str = "/DeviceGray"
    white_may_mark: bool = False
    """A graphics state was set under which white paint may still leave a mark (a blend mode, a
    soft mask, a transfer function, overprint): white then counts as drawn."""
    font: str | None = None
    """The font set by `Tf`, by its resource name."""

    def copy(self, ctm: Matrix | None = None) -> _State:
        return _State(
            self.ctm if ctm is None else ctm,
            self.fill_white,
            self.fill_space,
            self.stroke_white,
            self.stroke_space,
            self.white_may_mark,
            self.font,
        )

    def set_colour(self, operator: str, operands: Sequence[object]) -> None:
        """`g rg k cs sc scn` set the fill colour; `G RG K CS SC SCN` the stroke colour."""
        stroke = operator[0].isupper()
        kind = operator.lower()
        space = self.stroke_space if stroke else self.fill_space
        if kind in _DEVICE_SPACES:
            space = _DEVICE_SPACES[kind]
            white = _white(space, _numbers(operands, len(operands)))
        elif kind == "cs":
            space = str(operands[0]) if operands else ""
            white = False
        else:  # sc, scn: a pattern's name, or numbers in the current space
            white = all(isinstance(operand, int | Decimal) for operand in operands) and _white(
                space, [float(operand) for operand in operands]  # type: ignore[arg-type]
            )
        if stroke:
            self.stroke_space, self.stroke_white = space, white
        else:
            self.fill_space, self.fill_white = space, white


class _NotReadable(Exception):
    """Part of the page could not be read: it is not proven blank."""


@dataclass(slots=True)
class _Path:
    """The path being built, in page space: its subpaths, and whether any segment is a curve."""

    subpaths: list[list[tuple[float, float]]] = field(default_factory=list)
    closed: list[bool] = field(default_factory=list)
    curved: bool = False

    def start(self, point: tuple[float, float]) -> None:
        self.subpaths.append([point])
        self.closed.append(False)

    def add(self, point: tuple[float, float]) -> None:
        if not self.subpaths:
            raise _NotReadable("a path segment with no starting point")
        self.subpaths[-1].append(point)

    def close(self) -> None:
        if self.subpaths:
            self.closed[-1] = True


def _multiply(first: Matrix, second: Matrix) -> Matrix:
    a, b, c, d, e, f = first
    a2, b2, c2, d2, e2, f2 = second
    return (
        a * a2 + b * c2,
        a * b2 + b * d2,
        c * a2 + d * c2,
        c * b2 + d * d2,
        e * a2 + f * c2 + e2,
        e * b2 + f * d2 + f2,
    )


def _apply(matrix: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = matrix
    return (a * x + c * y + e, b * x + d * y + f)


def _numbers(operands: Iterable[object], count: int) -> list[float]:
    values = [float(operand) for operand in operands]  # type: ignore[arg-type]
    if len(values) != count:
        raise _NotReadable(f"expected {count} numbers, found {len(values)}")
    return values


def _rectangle(path: _Path) -> tuple[float, float, float, float] | None:
    """`(left, bottom, right, top)` when the path is one closed axis-aligned rectangle, else None."""
    if path.curved or len(path.subpaths) != 1:
        return None
    points = list(path.subpaths[0])
    if len(points) > 1 and _near(points[0], points[-1]):
        points.pop()
    elif not path.closed[0]:
        return None
    if len(points) != 4:
        return None
    xs = sorted({round(x, 1) for x, _ in points})
    ys = sorted({round(y, 1) for _, y in points})
    left, right = min(x for x, _ in points), max(x for x, _ in points)
    bottom, top = min(y for _, y in points), max(y for _, y in points)
    corners = [(left, bottom), (right, bottom), (right, top), (left, top)]
    if not all(any(_near(point, corner) for point in points) for corner in corners):
        return None
    # Each side runs along one axis: consecutive corners share an x or a y.
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1], strict=True):
        if abs(x1 - x2) > _CORNER_SLACK and abs(y1 - y2) > _CORNER_SLACK:
            return None
    if len(xs) < 2 or len(ys) < 2:
        return None
    return left, bottom, right, top


def _near(first: tuple[float, float], second: tuple[float, float]) -> bool:
    return abs(first[0] - second[0]) <= _CORNER_SLACK and abs(first[1] - second[1]) <= _CORNER_SLACK


def _white(space: str, values: list[float]) -> bool:
    if space == "/DeviceGray" and len(values) == 1:
        return values[0] >= 1
    if space == "/DeviceRGB" and len(values) == 3:
        return all(value >= 1 for value in values)
    if space == "/DeviceCMYK" and len(values) == 4:
        return all(value <= 0 for value in values)
    return False


def _string_length(operand: object) -> int:
    if isinstance(operand, pikepdf.String):
        return len(bytes(operand))
    if isinstance(operand, pikepdf.Array):
        return sum(_string_length(item) for item in operand)
    return 0


def _within(
    inner: tuple[float, float, float, float], outer: tuple[float, float, float, float]
) -> bool:
    return (
        inner[0] >= outer[0] - _CORNER_SLACK
        and inner[1] >= outer[1] - _CORNER_SLACK
        and inner[2] <= outer[2] + _CORNER_SLACK
        and inner[3] <= outer[3] + _CORNER_SLACK
    )


def _white_may_mark(resources: pikepdf.Dictionary | None, operands: Sequence[object]) -> bool:
    """Whether the named graphics state lets white paint leave a mark: a blend mode other than
    Normal, a soft mask, a transfer function or overprint. A state that cannot be found is not
    read at all: the page is not proven blank."""
    states = None if resources is None else resources.get("/ExtGState")
    if not operands or not isinstance(states, pikepdf.Dictionary):
        raise _NotReadable("a graphics state used with no graphics states to find it in")
    graphics = states.get(str(operands[0]))
    if not isinstance(graphics, pikepdf.Dictionary):
        raise _NotReadable(f"a graphics state that is not there: {operands[0]}")
    if "/TR" in graphics or "/TR2" in graphics:
        return True
    mask = graphics.get("/SMask")
    if mask is not None and str(mask) != "/None":
        return True
    if any(bool(graphics.get(key)) for key in ("/OP", "/op")):
        return True
    blend = graphics.get("/BM")
    if blend is None:
        return False
    if isinstance(blend, pikepdf.Array):
        return any(str(mode) not in _NORMAL_BLENDS for mode in blend)
    return str(blend) not in _NORMAL_BLENDS


def _check_typed_font(resources: pikepdf.Dictionary | None, font: str | None) -> None:
    """Text counts as typed only in a font that can be found and is not a Type3 font, whose
    glyphs are drawings of their own; anything else and the page is not proven blank."""
    fonts = None if resources is None else resources.get("/Font")
    found = fonts.get(font) if isinstance(fonts, pikepdf.Dictionary) and font is not None else None
    if not isinstance(found, pikepdf.Dictionary):
        raise _NotReadable(f"text in a font that cannot be found: {font}")
    if str(found.get("/Subtype")) == "/Type3":
        raise _NotReadable("text in a Type3 font: its glyphs are drawings")


class _Walker:
    """Counts what one content stream (and the forms it draws) paints."""

    def __init__(
        self,
        page_box: tuple[float, float, float, float] | None,
        *,
        text_box: tuple[float, float, float, float] | None = None,
    ):
        """`text_box`: walking a typed text box's appearance, whose own rectangles (background
        and border) must lie within this box, the annotation's `/Rect`, in page space."""
        self.page_box = page_box
        self.text_box = text_box
        self.counts = _Counts()

    def walk(self, stream: object, resources: pikepdf.Dictionary | None, ctm: Matrix) -> None:
        self._walk(stream, resources, _State(ctm=ctm), depth=0)

    def _walk(
        self, stream: object, resources: pikepdf.Dictionary | None, state: _State, depth: int
    ) -> None:
        if depth > _MAX_FORM_DEPTH:
            raise _NotReadable("forms nested too deep")
        try:
            instructions = pikepdf.parse_content_stream(stream)  # type: ignore[arg-type]
        except (pikepdf.PdfError, TypeError, ValueError) as error:
            raise _NotReadable(f"content stream: {error}") from error
        stack: list[_State] = []
        path = _Path()
        for instruction in instructions:
            operator = str(instruction.operator)
            operands = list(instruction.operands)
            if operator == "INLINE IMAGE":
                self.counts.images += 1
            elif operator == "q":
                stack.append(state.copy())
            elif operator == "Q":
                if stack:
                    state = stack.pop()
            elif operator == "cm":
                state.ctm = _multiply(tuple(_numbers(operands, 6)), state.ctm)  # type: ignore[arg-type]
            elif operator == "gs":
                state.white_may_mark = state.white_may_mark or _white_may_mark(resources, operands)
            elif operator == "Tf":
                state.font = str(operands[0]) if operands else None
            elif operator in _COLOUR_OPERATORS:
                state.set_colour(operator, operands)
            elif operator == "m":
                x, y = _numbers(operands, 2)
                path.start(_apply(state.ctm, x, y))
            elif operator == "l":
                x, y = _numbers(operands, 2)
                path.add(_apply(state.ctm, x, y))
            elif operator in ("c", "v", "y"):
                numbers = _numbers(operands, 6 if operator == "c" else 4)
                path.curved = True
                path.add(_apply(state.ctm, numbers[-2], numbers[-1]))
            elif operator == "h":
                path.close()
            elif operator == "re":
                x, y, width, height = _numbers(operands, 4)
                path.start(_apply(state.ctm, x, y))
                path.add(_apply(state.ctm, x + width, y))
                path.add(_apply(state.ctm, x + width, y + height))
                path.add(_apply(state.ctm, x, y + height))
                path.close()
            elif operator in _STROKES or operator in _FILLS or operator in _FILL_AND_STROKE:
                self._paint(operator, path, state)
                path = _Path()
            elif operator == "n":
                path = _Path()
            elif operator == "sh":
                self.counts.drawn_paths += 1
            elif operator in _TEXT_SHOWS:
                _check_typed_font(resources, state.font)
                self.counts.typed_texts += 1
                self.counts.typed_characters += sum(_string_length(o) for o in operands)
            elif operator == "Do":
                self._draw(operands, resources, state, depth)

    def _paint(self, operator: str, path: _Path, state: _State) -> None:
        rectangle = _rectangle(path)
        if self.text_box is not None:
            # A text box's own background and border: at most two rectangles, inside its box.
            if (
                rectangle is None
                or self.counts.rectangles >= _TEXT_BOX_RECTANGLES
                or not _within(rectangle, self.text_box)
            ):
                self.counts.drawn_paths += 1
            else:
                self.counts.rectangles += 1
            return
        if not state.white_may_mark and (
            (operator in _FILLS and state.fill_white)
            or (operator in _STROKES and state.stroke_white)
            or (operator in _FILL_AND_STROKE and state.fill_white and state.stroke_white)
        ):
            self.counts.white_paint += 1
            return
        if operator in _STROKES and rectangle is not None and self._is_frame(rectangle):
            self.counts.frames += 1
            return
        self.counts.drawn_paths += 1

    def _is_frame(self, rectangle: tuple[float, float, float, float]) -> bool:
        if self.page_box is None:
            return False
        left, bottom, right, top = self.page_box
        page_width, page_height = right - left, top - bottom
        if page_width <= 0 or page_height <= 0:
            return False
        r_left, r_bottom, r_right, r_top = rectangle
        inside = (
            r_left >= left - _CORNER_SLACK
            and r_bottom >= bottom - _CORNER_SLACK
            and r_right <= right + _CORNER_SLACK
            and r_top <= top + _CORNER_SLACK
        )
        return (
            inside
            and (r_right - r_left) >= _FRAME_COVER * page_width
            and (r_top - r_bottom) >= _FRAME_COVER * page_height
        )

    def _draw(
        self,
        operands: Sequence[object],
        resources: pikepdf.Dictionary | None,
        state: _State,
        depth: int,
    ) -> None:
        objects = None if resources is None else resources.get("/XObject")
        if not operands or not isinstance(objects, pikepdf.Dictionary):
            raise _NotReadable("an XObject drawn with no resources")
        xobject = objects.get(str(operands[0]))
        if not isinstance(xobject, pikepdf.Stream):
            raise _NotReadable("an XObject that is not a stream")
        subtype = str(xobject.get("/Subtype"))
        if subtype == "/Image":
            self.counts.images += 1
            return
        if subtype != "/Form":
            raise _NotReadable(f"an XObject of kind {subtype}")
        matrix = xobject.get("/Matrix")
        form_matrix: Matrix = (
            tuple(_numbers(matrix, 6)) if matrix is not None else _IDENTITY  # type: ignore[assignment]
        )
        inner = xobject.get("/Resources")
        self._walk(
            xobject,
            inner if isinstance(inner, pikepdf.Dictionary) else resources,
            state.copy(_multiply(form_matrix, state.ctm)),
            depth + 1,
        )


def _inherited(page: pikepdf.Page, key: str) -> object | None:
    """A page attribute, from the page or the nearest `/Pages` node above it (PDF 7.7.3.4)."""
    node: object = page.obj
    for _ in range(_MAX_TREE_DEPTH):
        if not isinstance(node, pikepdf.Dictionary):
            return None
        if key in node:
            return node.get(key)
        node = node.get("/Parent")
    return None


def _bounds(values: object) -> tuple[float, float, float, float] | None:
    try:
        box = [float(value) for value in values]  # type: ignore[attr-defined]
    except (TypeError, ValueError, pikepdf.PdfError):
        return None
    if len(box) != 4:
        return None
    return min(box[0], box[2]), min(box[1], box[3]), max(box[0], box[2]), max(box[1], box[3])


def _page_box(page: pikepdf.Page) -> tuple[float, float, float, float] | None:
    return _bounds(_inherited(page, "/MediaBox"))


def _appearance_matrix(stream: pikepdf.Stream, rect: tuple[float, float, float, float]) -> Matrix:
    """The matrix that places an annotation's appearance on the page (PDF 12.5.5): its `/Matrix`,
    then the transformed `/BBox` scaled and moved onto the annotation's `/Rect`."""
    bbox = _bounds(stream.get("/BBox"))
    if bbox is None:
        raise _NotReadable("an appearance with no bounding box")
    raw = stream.get("/Matrix")
    matrix: Matrix = _IDENTITY if raw is None else tuple(_numbers(raw, 6))  # type: ignore[assignment]
    corners = [_apply(matrix, x, y) for x in (bbox[0], bbox[2]) for y in (bbox[1], bbox[3])]
    left, right = min(x for x, _ in corners), max(x for x, _ in corners)
    bottom, top = min(y for _, y in corners), max(y for _, y in corners)
    if right - left <= 0 or top - bottom <= 0:
        raise _NotReadable("an appearance with an empty bounding box")
    scale_x = (rect[2] - rect[0]) / (right - left)
    scale_y = (rect[3] - rect[1]) / (top - bottom)
    onto_rect: Matrix = (
        scale_x,
        0.0,
        0.0,
        scale_y,
        rect[0] - left * scale_x,
        rect[1] - bottom * scale_y,
    )
    return _multiply(matrix, onto_rect)


def _contents_length(annotation: pikepdf.Dictionary) -> int:
    contents = annotation.get("/Contents")
    return len(bytes(contents)) if isinstance(contents, pikepdf.String) else 0


def _appearances(annotation: pikepdf.Dictionary) -> list[pikepdf.Stream]:
    """The annotation's normal appearance streams (one, or one per state)."""
    appearance = annotation.get("/AP")
    if not isinstance(appearance, pikepdf.Dictionary):
        return []
    normal = appearance.get("/N")
    if isinstance(normal, pikepdf.Stream):
        return [normal]
    if isinstance(normal, pikepdf.Dictionary):
        return [value for value in normal.values() if isinstance(value, pikepdf.Stream)]
    return []


def _facts(page: pikepdf.Page) -> BlankPageFacts:
    box = _page_box(page)
    stamps = other = 0
    untyped_characters = 0
    content = _Walker(box)
    boxes: list[_Walker] = []
    try:
        annotations = page.obj.get("/Annots")
        for annotation in annotations if isinstance(annotations, pikepdf.Array) else ():
            if not isinstance(annotation, pikepdf.Dictionary):
                raise _NotReadable("an annotation that is not a dictionary")
            subtype = str(annotation.get("/Subtype"))
            if subtype == "/Stamp":
                stamps += 1
            elif subtype not in _TEXT_ANNOTATIONS:
                other += 1
            elif subtype == "/FreeText":
                appearances = _appearances(annotation)
                callout = "/CL" in annotation or str(annotation.get("/IT")) == "/FreeTextCallout"
                if callout or not appearances:
                    # A callout points at something; a box with no appearance draws whatever a
                    # viewer makes of it. Neither is a plain typed title.
                    other += 1
                    untyped_characters += _contents_length(annotation)
                    continue
                rect = _bounds(annotation.get("/Rect"))
                if rect is None:
                    raise _NotReadable("a text box with no rectangle")
                for stream in appearances:
                    walker = _Walker(box, text_box=rect)
                    resources = stream.get("/Resources")
                    walker.walk(
                        stream,
                        resources if isinstance(resources, pikepdf.Dictionary) else None,
                        _appearance_matrix(stream, rect),
                    )
                    boxes.append(walker)
        page_resources = _inherited(page, "/Resources")
        content.walk(
            page.obj,
            page_resources if isinstance(page_resources, pikepdf.Dictionary) else None,
            _IDENTITY,
        )
    except _NotReadable as error:
        return BlankPageFacts(stamps=stamps, other_annotations=other, unreadable=str(error))
    except (pikepdf.PdfError, TypeError, ValueError, KeyError) as error:
        return BlankPageFacts(stamps=stamps, other_annotations=other, unreadable=str(error))
    walkers = [content, *boxes]
    return BlankPageFacts(
        stamps=stamps,
        other_annotations=other,
        images=sum(walker.counts.images for walker in walkers),
        drawn_paths=sum(walker.counts.drawn_paths for walker in walkers),
        frames=content.counts.frames,
        white_paint=content.counts.white_paint,
        typed_texts=sum(walker.counts.typed_texts for walker in walkers),
        typed_characters=untyped_characters
        + sum(walker.counts.typed_characters for walker in walkers),
    )


def prove_blank_pages(data: bytes, page_indexes: Iterable[int]) -> dict[int, BlankPageFacts]:
    """The facts for each asked page (0-based), read from the PDF's bytes. A page whose file or
    content cannot be read gets facts with `unreadable` set: never blank."""
    wanted = list(dict.fromkeys(page_indexes))
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except (pikepdf.PdfError, ValueError) as error:
        return {index: BlankPageFacts(unreadable=f"file: {error}") for index in wanted}
    with pdf:
        facts: dict[int, BlankPageFacts] = {}
        for index in wanted:
            if not 0 <= index < len(pdf.pages):
                facts[index] = BlankPageFacts(unreadable="no such page")
                continue
            facts[index] = _facts(pdf.pages[index])
        return facts
