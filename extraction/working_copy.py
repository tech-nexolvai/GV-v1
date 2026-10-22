"""The front door: every uploaded PDF becomes one standard working copy, once, at intake (#1724).

**Why.** A PDF can say how its page is shown in ways each reader handles differently: `/Rotate`
turns the page, a `/CropBox` or `/MediaBox` that does not start at `(0, 0)` moves it, `/UserUnit`
scales it, and a page may inherit any of these from the page tree. pdfplumber applies `/Rotate`
itself; GV's own code applied it again, and every box on a rotated page landed off the spot. Rather
than teach each reader every variation, the set's PDFs are rewritten once into the shape every
reader already handles: each page shown exactly as before, with

1. its inherited attributes (`/Rotate`, `/MediaBox`, `/CropBox`, `/Resources`) written onto it;
2. its visible area (the crop box clipped into the media box) made its media box;
3. its rotation baked into its content by qpdf (`Page.flatten_rotation`), which moves the page
   boxes and the annotations with it (pypdf's `transfer_rotation_to_content` leaves annotations
   behind, pypdf #2592);
4. its corner moved to `(0, 0)` and its `/UserUnit` applied, in one matrix (`q U 0 0 U -x0U -y0U cm
   ... Q`), the page boxes and each annotation's `/Rect` and point lists moved with it;
5. the file rewritten whole, without encryption (a file whose owner password locks nothing a
   reader needs), repaired where qpdf had to repair it. Slight damage alone (a duplicated key, a
   broken index qpdf rebuilds on opening) is no reason to rewrite a file the readers already read.

Nothing else is touched: fonts, layers, Form XObjects, transparency, annotation content. Nothing is
flattened.

**Every page of a changed file is checked by its picture** (`_verify`): the original as displayed
(PDFium applies `/Rotate` and the crop box itself and ignores `/UserUnit`, so it is drawn at `U`
times the scale) against the working copy, annotations drawn, and their counts compared. A page
whose pictures do not agree is replaced by a blank page that says why: no reader ever places
anything on a page whose working copy is not exact, and the unchanged page is never used instead.
So is a page with no visible area. The check runs in a process of its own (`verify_isolated`,
#1869): a page that crashes the PDF engine is such a page too ("the PDF reader crashed on it"), and
the rest are still checked; the server never goes down with it.

**A file that needs none of this comes back byte for byte**, so a set already in this shape (every
GV set so far) reads exactly as before and keeps its set pack.

Source: issue #1724; the front-door research note (local, 2026-10-19) §4 ·
Verification: `tests/extraction/test_working_copy.py`
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import numpy as np
import pikepdf

from extraction.pdfium_guard import pdfium

__all__ = [
    "MAX_SIDE_PT",
    "VERIFY_DPI",
    "WORKING_COPY_VERSION",
    "WorkingCopy",
    "WorkingCopyRefused",
    "working_copy",
]

#: Changes when what the front door writes changes: a chat's uploads are made again.
WORKING_COPY_VERSION: Final = 1

#: The longest side a PDF page may have in points (PDF 32000-1, annex C). A page whose `/UserUnit`
#: would take it past this keeps the unit, which is noted: every reader ignores `/UserUnit`, so its
#: positions still agree; only its real size needs the factor.
MAX_SIDE_PT: Final = Decimal(14400)

#: The picture check (`_verify`): the dpi both pages are drawn at, how far a grey level may differ
#: and still be the same (anti-aliasing on an edge), the share of pixels that may differ, and how
#: many pixels across a patch of difference may be (a moved mark is bigger than an edge).
VERIFY_DPI: Final = 72
_VERIFY_LEVEL: Final = 32
_VERIFY_SHARE: Final = 0.001
_VERIFY_BLOB_PX: Final = 4

#: Annotation keys that hold page points, as flat `x y x y ...` lists.
_POINT_LISTS: Final = ("/QuadPoints", "/Vertices", "/L", "/CL")
#: The page boxes other than the media and crop boxes.
_OTHER_BOXES: Final = ("/BleedBox", "/TrimBox", "/ArtBox")

type Rect = tuple[Decimal, Decimal, Decimal, Decimal]
type _Matrix = tuple[Decimal, Decimal, Decimal, Decimal, Decimal, Decimal]


class WorkingCopyRefused(ValueError):
    """The file cannot be opened to make its working copy; the message says why, plainly."""


@dataclass(frozen=True, slots=True)
class WorkingCopy:
    """A PDF in the standard shape, and what was done to make it."""

    data: bytes
    """The working copy: the original's bytes when nothing needed changing."""
    changed: bool
    """Whether `data` differs from the original."""
    changes: tuple[str, ...] = ()
    """What was done, in plain words, one line per kind of change."""
    not_read: tuple[tuple[int, str], ...] = ()
    """Pages (from 1) left blank because they could not be put in shape exactly, each with why."""
    facts: dict[str, Any] = field(default_factory=dict)
    """What the original said, for the health check (`page_health`): `revisions` (saves in
    stages), `signed`, `producer`, and per page (`pages`, keyed by the page number as text) its
    `rotation`, `user_unit` and whether its visible area was `moved`. Plain JSON."""

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


class _PageRefused(ValueError):
    """One page cannot be put in shape; the message is the plain reason."""


#: What `_normalise_page` reports, and the words for it.
_DONE: Final = {
    "turned": "rotation baked into",
    "upright": "a rotation that is not a quarter turn shown upright (as viewers show it) on",
    "moved": "visible area moved to the corner on",
    "scaled": "user unit applied on",
    "kept unit": "large-page user unit kept (sizes need its factor) on",
}


def working_copy(data: bytes) -> WorkingCopy:
    """`data` in the standard shape (see the module docstring). Raises `WorkingCopyRefused` for a
    file that cannot be opened (damaged beyond repair, or locked by a password)."""
    try:
        pdf = pikepdf.open(io.BytesIO(data))
    except pikepdf.PasswordError as error:
        raise WorkingCopyRefused(
            "this file is locked with a password, so it could not be read"
        ) from error
    except pikepdf.PdfError as error:
        raise WorkingCopyRefused("this file is damaged and could not be opened") from error
    with pdf:
        not_read: list[tuple[int, str]] = []
        done: dict[str, list[int]] = {kind: [] for kind in _DONE}
        facts = _original_facts(pdf, data)
        for number, page in enumerate(pdf.pages, start=1):
            try:
                for kind in _normalise_page(page):
                    done[kind].append(number)
            except _PageRefused as refused:
                why = f"page {number} could not be read: {refused}"
                _blank(pdf, page, why)
                not_read.append((number, why))
        changes = [f"{_DONE[kind]} {_pages(pages)}" for kind, pages in done.items() if pages]
        if pdf.is_encrypted:
            changes.append("encryption removed (the file needed no password to open)")
        if not (changes or not_read):
            # Slight damage qpdf repairs on opening (a duplicated key, a missing index) is no
            # reason to rewrite a file every reader has read as it is: it stays byte for byte.
            return WorkingCopy(data=data, changed=False, facts=facts)
        if pdf.get_warnings():
            changes.append("the file was slightly damaged and was repaired")
        out = io.BytesIO()
        pdf.save(out, encryption=False, deterministic_id=True)
    working = out.getvalue()
    failed = verify_isolated(data, working, skip={page for page, _ in not_read})
    if failed:
        working, more = _quarantine(working, failed)
        not_read = sorted([*not_read, *more])
    return WorkingCopy(
        data=working,
        changed=True,
        changes=tuple(changes),
        not_read=tuple(not_read),
        facts=facts,
    )


def _original_facts(pdf: pikepdf.Pdf, data: bytes) -> dict[str, Any]:
    """What the original file says about itself, before anything is changed (`WorkingCopy.facts`).
    Read defensively: a fact that cannot be read is left out."""
    facts: dict[str, Any] = {
        "revisions": max(1, data.count(b"%%EOF") - (1 if pdf.is_linearized else 0))
    }
    try:
        form = pdf.Root.get("/AcroForm")
        facts["signed"] = bool(
            isinstance(form, pikepdf.Dictionary) and int(form.get("/SigFlags", 0) or 0) & 1
        )
        producer = pdf.docinfo.get("/Producer") if pdf.docinfo is not None else None
        if producer is not None:
            facts["producer"] = str(producer)[:120]
    except (pikepdf.PdfError, TypeError, ValueError):
        pass
    pages: dict[str, dict[str, Any]] = {}
    for number, page in enumerate(pdf.pages, start=1):
        try:
            rotation, odd = _rotation(page)
            media = _rect(_inherited(page, "/MediaBox"))
            crop = _rect(_inherited(page, "/CropBox")) or media
            unit_raw = page.obj.get("/UserUnit")
            pages[str(number)] = {
                "rotation": rotation,
                "odd_rotation": odd,
                "user_unit": float(_number(unit_raw)) if unit_raw is not None else 1.0,
                "moved": bool(media is not None and (crop != media or media[0] or media[1])),
            }
        except (_PageRefused, pikepdf.PdfError, TypeError, ValueError):
            continue
    facts["pages"] = pages
    return facts


def _pages(numbers: list[int]) -> str:
    return ("page " if len(numbers) == 1 else "pages ") + ", ".join(str(n) for n in numbers)


# ---- one page ------------------------------------------------------------------------------------


def _inherited(page: pikepdf.Page, key: str) -> Any:
    """A page attribute, from the page or the nearest page-tree node above it that sets it."""
    node: Any = page.obj
    seen: set[tuple[int, int]] = set()
    while isinstance(node, pikepdf.Dictionary):
        if key in node:
            return node[key]
        parent = node.get("/Parent")
        if not isinstance(parent, pikepdf.Dictionary) or parent.objgen in seen:
            return None
        seen.add(parent.objgen)
        node = parent
    return None


def _number(value: Any) -> Decimal:
    """A PDF number as an exact Decimal (through its text, never a float's binary value)."""
    if isinstance(value, bool):
        raise _PageRefused("its page box holds a value that is not a number")
    if isinstance(value, Decimal | int):
        return Decimal(value)
    try:
        return Decimal(str(value))
    except ArithmeticError as error:
        raise _PageRefused("its page box holds a value that is not a number") from error


def _rect(value: Any) -> Rect | None:
    if not isinstance(value, pikepdf.Array) or len(value) != 4:
        return None
    x0, y0, x1, y1 = (_number(v) for v in value)
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _array(rect: Rect) -> pikepdf.Array:
    return pikepdf.Array(list(rect))


def _rotation(page: pikepdf.Page) -> tuple[int, bool]:
    """The page's turn as viewers show it (a quarter turn, else none), and whether `/Rotate` said
    something else (a turn that is not a quarter turn)."""
    raw = _inherited(page, "/Rotate")
    try:
        rotate = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0, True
    if rotate % 90 != 0:
        return 0, True
    return rotate % 360, False


def _normalise_page(page: pikepdf.Page) -> list[str]:
    """Put one page in shape, in place; returns what was done (the keys of `_DONE`). Raises
    `_PageRefused` for a page with no visible area."""
    obj = page.obj
    media = _rect(_inherited(page, "/MediaBox"))
    if media is None or media[2] <= media[0] or media[3] <= media[1]:
        raise _PageRefused("its page box is missing or broken, so it has no visible area")
    crop = _rect(_inherited(page, "/CropBox")) or media
    visible = (
        max(crop[0], media[0]),
        max(crop[1], media[1]),
        min(crop[2], media[2]),
        min(crop[3], media[3]),
    )
    if visible[2] <= visible[0] or visible[3] <= visible[1]:
        raise _PageRefused("its visible area is empty (its page box is broken)")
    rotation, odd = _rotation(page)
    unit_raw = obj.get("/UserUnit")
    unit = _number(unit_raw) if unit_raw is not None else Decimal(1)
    if unit <= 0:
        unit = Decimal(1)

    done: list[str] = []
    if rotation:
        done.append("turned")
    if odd:
        done.append("upright")
    if visible != media or media[0] != 0 or media[1] != 0:
        done.append("moved")
    if unit != 1:
        done.append("scaled")
    if not done:
        return done

    # 1. Inherited attributes onto the page, so the page tree above it no longer matters (qpdf's
    # flattenRotation reads only the page's own /Rotate).
    resources = _inherited(page, "/Resources")
    if "/Resources" not in obj and resources is not None:
        obj["/Resources"] = resources
    # 2. The visible area is the page.
    obj["/MediaBox"] = _array(visible)
    obj["/CropBox"] = _array(visible)
    for key in _OTHER_BOXES:
        box = _rect(obj.get(key))
        if box is not None:
            obj[key] = _array(_clipped(box, visible))
    annotations = [a for a in obj.get("/Annots") or [] if isinstance(a, pikepdf.Dictionary)]
    points = [_point_lists(annotation) for annotation in annotations]
    # 3. The corner to (0, 0) and the user unit applied, in one matrix, first: qpdf turns a page
    # whose media box starts at the origin consistently (with an offset box it moved annotations
    # by a different amount from the content, measured).
    if max(visible[2] - visible[0], visible[3] - visible[1]) * unit > MAX_SIDE_PT:
        unit = Decimal(1)
        done = [kind for kind in done if kind != "scaled"] + ["kept unit"]
    _shift(page, annotations, visible[0], visible[1], unit)
    if unit != 1 and "/UserUnit" in obj:
        del obj["/UserUnit"]
    # 4. The rotation baked in (content, page boxes, annotations' `/Rect` and appearances), by
    # qpdf. qpdf does not carry annotations' point lists (`/InkList`, `/QuadPoints`...): those
    # are carried below, through the whole page's matrix.
    obj["/Rotate"] = rotation
    if rotation:
        page.flatten_rotation()
        # qpdf may write the annotations it turned as new objects: carry on with those.
        annotations = [a for a in obj.get("/Annots") or [] if isinstance(a, pikepdf.Dictionary)]
        if len(annotations) != len(points):
            raise _PageRefused("its annotations could not be turned with it")
    obj["/Rotate"] = 0
    box = _rect(obj.get("/MediaBox"))
    if box is None:
        raise _PageRefused("its page box could not be turned")
    if box[0] or box[1]:
        _shift(page, annotations, box[0], box[1], Decimal(1))
    whole = _display_matrix(rotation, visible, unit)
    for annotation, lists in zip(annotations, points, strict=True):
        for key, values in lists.items():
            if key == "/InkList":
                annotation[key] = pikepdf.Array(
                    [pikepdf.Array(_through(whole, stroke)) for stroke in values]
                )
            else:
                annotation[key] = pikepdf.Array(_through(whole, values))
    return done


def _shift(
    page: pikepdf.Page,
    annotations: list[pikepdf.Dictionary],
    left: Decimal,
    bottom: Decimal,
    unit: Decimal,
) -> None:
    """Move the page's content, boxes and annotation rectangles so `(left, bottom)` is the origin,
    scaled by `unit` (`q U 0 0 U -left*U -bottom*U cm ... Q`)."""
    if not left and not bottom and unit == 1:
        return
    obj = page.obj
    matrix = (unit, Decimal(0), Decimal(0), unit, -left * unit, -bottom * unit)
    page.contents_add(b"q " + " ".join(_text(v) for v in matrix).encode() + b" cm\n", prepend=True)
    page.contents_add(b"\nQ\n", prepend=False)
    for key in ("/MediaBox", "/CropBox", *_OTHER_BOXES):
        old = _rect(obj.get(key))
        if old is not None:
            obj[key] = _array(_moved(old, left, bottom, unit))
    for annotation in annotations:
        rect = _rect(annotation.get("/Rect"))
        if rect is not None:
            annotation["/Rect"] = _array(_moved(rect, left, bottom, unit))


def _point_lists(annotation: pikepdf.Dictionary) -> dict[str, Any]:
    """An annotation's page points, as numbers, before its page is changed."""
    out: dict[str, Any] = {}
    for key in _POINT_LISTS:
        values = annotation.get(key)
        if isinstance(values, pikepdf.Array) and len(values) % 2 == 0:
            out[key] = [_number(v) for v in values]
    ink = annotation.get("/InkList")
    if isinstance(ink, pikepdf.Array) and all(
        isinstance(stroke, pikepdf.Array) and len(stroke) % 2 == 0 for stroke in ink
    ):
        out["/InkList"] = [[_number(v) for v in stroke] for stroke in ink]
    return out


def _display_matrix(rotation: int, visible: Rect, unit: Decimal) -> _Matrix:
    """Where the working copy puts a point of the original page: the visible area's corner to the
    origin, the page turned clockwise by `rotation` as viewers show it, then scaled by `unit`.

    Worked from where a viewer shows a point (`evidence.coordinates.PageTransform.to_image`): at
    90 a point `across` from the left and `up` from the bottom shows `up` across and `across` down,
    so on the new page (y up, its height the old width) it is at `(up, width - across)`.
    """
    left, bottom, right, top = visible
    width, height = right - left, top - bottom
    a, b, c, d, e, f = {
        0: (1, 0, 0, 1, Decimal(0), Decimal(0)),
        90: (0, -1, 1, 0, Decimal(0), width),
        180: (-1, 0, 0, -1, width, height),
        270: (0, 1, -1, 0, height, Decimal(0)),
    }[rotation]
    da, db, dc, dd = Decimal(a), Decimal(b), Decimal(c), Decimal(d)
    return (
        unit * da,
        unit * db,
        unit * dc,
        unit * dd,
        unit * (e - da * left - dc * bottom),
        unit * (f - db * left - dd * bottom),
    )


def _through(matrix: _Matrix, values: list[Decimal]) -> list[Decimal]:
    a, b, c, d, e, f = matrix
    out: list[Decimal] = []
    for x, y in zip(values[::2], values[1::2], strict=True):
        out.extend((a * x + c * y + e, b * x + d * y + f))
    return out


def _clipped(box: Rect, frame: Rect) -> Rect:
    clipped = (
        max(box[0], frame[0]),
        max(box[1], frame[1]),
        min(box[2], frame[2]),
        min(box[3], frame[3]),
    )
    return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else frame


def _moved(box: Rect, left: Decimal, bottom: Decimal, unit: Decimal) -> Rect:
    return (
        (box[0] - left) * unit,
        (box[1] - bottom) * unit,
        (box[2] - left) * unit,
        (box[3] - bottom) * unit,
    )


def _text(value: Decimal) -> str:
    """A number as PDF writes it: no exponent, no needless zeros."""
    text = format(value.normalize(), "f")
    return "0" if text in ("-0", "") else text


# ---- the picture check ---------------------------------------------------------------------------


#: The picture check's own process (`verify_isolated`): the most pages it may crash on before the
#: file is refused, and its time limit (a start, plus a page's worst time on a busy machine, each).
MAX_CRASHED_PAGES: Final = 3
_VERIFY_START_S: Final = 60.0
_VERIFY_PAGE_S: Final = 10.0
CRASHED: Final = "the PDF reader crashed on it"
#: Runs the picture check in this process (tests that change how it judges a page).
IN_PROCESS_SETTING: Final = "GV_FRONT_DOOR_IN_PROCESS"
#: What the picture check's process gets of this one's environment: enough to run Python and find
#: this code, never a key, a token or the database's address (it reads an uploaded file).
_CHILD_VARIABLES: Final = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")


def verify_command(original: Path, working: Path, out: Path, skip: Collection[int]) -> list[str]:
    """The picture check's own process (`main`)."""
    pages = ",".join(str(page) for page in sorted(skip))
    return [
        sys.executable,
        "-m",
        "extraction.working_copy",
        "verify",
        *map(str, (original, working, out)),
        pages,
    ]


def _child_environment() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    kept = {name: os.environ[name] for name in _CHILD_VARIABLES if name in os.environ}
    kept["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root), os.environ.get("PYTHONPATH"))))
    kept.setdefault("PATH", "/usr/bin:/bin")
    return kept


def verify_isolated(original: bytes, working: bytes, *, skip: set[int]) -> dict[int, str]:
    """`_verify` in a process of its own (the PDF engine draws every page twice there): a page that
    crashes the engine is failed (`CRASHED`, so it is left blank like any page that did not agree)
    and the check goes on without it, never stopping this process. Raises `WorkingCopyRefused`
    after `MAX_CRASHED_PAGES` such pages, or when the check cannot finish."""
    if os.environ.get(IN_PROCESS_SETTING) == "1":
        return _verify(original, working, skip=skip)
    crashed: dict[int, str] = {}
    with tempfile.TemporaryDirectory(prefix="gv-front-door-") as scratch:
        folder = Path(scratch)
        before, after, out = folder / "original.pdf", folder / "working.pdf", folder / "out"
        before.write_bytes(original)
        after.write_bytes(working)
        pages = _page_count(working)
        while True:
            for stale in (out.with_suffix(".json"), out.with_suffix(".progress")):
                stale.unlink(missing_ok=True)
            try:
                done = subprocess.run(
                    verify_command(before, after, out, skip | set(crashed)),
                    cwd=Path(__file__).resolve().parents[1],
                    env=_child_environment(),
                    timeout=_VERIFY_START_S + _VERIFY_PAGE_S * pages,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    check=False,
                )
            except subprocess.TimeoutExpired as late:
                raise WorkingCopyRefused(
                    "checking this file's pages took too long, so it could not be read"
                ) from late
            if done.returncode == 0:
                found = json.loads(out.with_suffix(".json").read_text(encoding="utf-8"))
                return {**{int(k): str(v) for k, v in found.items()}, **crashed}
            page = _last_page(out.with_suffix(".progress"))
            if done.returncode > 0 or page is None or page in crashed:
                raise WorkingCopyRefused(
                    "this file's pages could not be checked, so it was not read"
                )
            crashed[page] = CRASHED
            if len(crashed) > MAX_CRASHED_PAGES:
                raise WorkingCopyRefused(
                    f"the PDF reader crashed on more than {MAX_CRASHED_PAGES} of this file's pages,"
                    " so it was not read"
                )


def _page_count(data: bytes) -> int:
    with pikepdf.open(io.BytesIO(data)) as pdf:
        return len(pdf.pages)


def _last_page(progress: Path) -> int | None:
    """The page the picture check's process started last (`None` before the first)."""
    try:
        lines = progress.read_text(encoding="utf-8").split()
    except OSError:
        return None
    return int(lines[-1]) if lines and lines[-1].isdigit() else None


def main(arguments: Sequence[str]) -> int:
    """`python -m extraction.working_copy verify <original> <working> <out> <pages to skip>`:
    `_verify` in a process of its own; `<out>.json` the failed pages, `<out>.progress` each page's
    number as it is started."""
    if len(arguments) != 5 or arguments[0] != "verify":
        print("usage: working_copy verify <original.pdf> <working.pdf> <out> <pages,to,skip>")
        return 2
    original, working, out = (Path(value) for value in arguments[1:4])
    skip = {int(page) for page in arguments[4].split(",") if page.strip()}
    with out.with_suffix(".progress").open("a", encoding="utf-8") as progress:

        def started(page: int) -> None:
            progress.write(f"{page}\n")
            progress.flush()

        failed = _verify(original.read_bytes(), working.read_bytes(), skip=skip, started=started)
    out.with_suffix(".json").write_text(json.dumps({str(k): v for k, v in failed.items()}))
    return 0


def _verify(
    original: bytes,
    working: bytes,
    *,
    skip: set[int],
    started: Callable[[int], None] | None = None,
) -> dict[int, str]:
    """Each page of `working` against `original` as displayed: the same picture (at `VERIFY_DPI`,
    the original at its user unit's scale) and the same number of annotations. Returns the pages
    (from 1) that do not agree, with why. `started`: told each page's number before it is drawn."""
    units, counts_before = _page_facts(original)
    kept, counts_after = _page_facts(working)
    before = pdfium.PdfDocument(original)
    after = pdfium.PdfDocument(working)
    failed: dict[int, str] = {}
    try:
        for index in range(len(after)):
            number = index + 1
            if number in skip:
                continue
            if index >= len(before) or counts_before.get(number) != counts_after.get(number):
                failed[number] = "its annotations did not all come through"
                continue
            if started is not None:
                started(number)
            scale = VERIFY_DPI / 72
            # The unit applied (the original's over any the working copy kept) is drawn as scale.
            applied = units.get(number, Decimal(1)) / kept.get(number, Decimal(1))
            a = _grey(before[index], scale * float(applied))
            b = _grey(after[index], scale)
            if a.shape != b.shape or not _same(a, b):
                failed[number] = "its working copy did not look exactly like the original"
    finally:
        before.close()
        after.close()
    return failed


def _page_facts(data: bytes) -> tuple[dict[int, Decimal], dict[int, int]]:
    """Each page's user unit (1 when unset or unreadable) and number of annotations."""
    units: dict[int, Decimal] = {}
    counts: dict[int, int] = {}
    with pikepdf.open(io.BytesIO(data)) as pdf:
        for number, page in enumerate(pdf.pages, start=1):
            raw = page.obj.get("/UserUnit")
            try:
                unit = _number(raw) if raw is not None else Decimal(1)
            except _PageRefused:
                unit = Decimal(1)
            units[number] = unit if unit > 0 else Decimal(1)
            counts[number] = len(page.obj.get("/Annots") or [])
    return units, counts


def _grey(page: Any, scale: float) -> np.ndarray:
    picture = page.render(scale=scale, may_draw_forms=True, grayscale=True).to_pil().convert("L")
    return np.asarray(picture).astype(np.int16)


def _same(a: np.ndarray, b: np.ndarray) -> bool:
    """At most `_VERIFY_SHARE` of the pixels differ by more than `_VERIFY_LEVEL`, and no patch of
    difference is `_VERIFY_BLOB_PX` pixels across both ways: an edge drawn a pixel over is the same
    page, a mark in another place is not."""
    differ = np.abs(a - b) > _VERIFY_LEVEL
    if not differ.any():
        return True
    if differ.mean() > _VERIFY_SHARE:
        return False
    import cv2

    grown = cv2.dilate(differ.astype(np.uint8), np.ones((3, 3), np.uint8))
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(grown)
    size = _VERIFY_BLOB_PX + 2  # the dilation adds a pixel each side
    return all(
        stats[i, cv2.CC_STAT_WIDTH] < size or stats[i, cv2.CC_STAT_HEIGHT] < size
        for i in range(1, count)
    )


def _quarantine(working: bytes, failed: dict[int, str]) -> tuple[bytes, list[tuple[int, str]]]:
    """The working copy with each failed page blank and saying why (never the unchanged page)."""
    said: list[tuple[int, str]] = []
    with pikepdf.open(io.BytesIO(working)) as pdf:
        for number, reason in sorted(failed.items()):
            why = f"page {number} could not be read: {reason}, so nothing is read or drawn on it"
            _blank(pdf, pdf.pages[number - 1], why)
            said.append((number, why))
        out = io.BytesIO()
        pdf.save(out, encryption=False, deterministic_id=True)
    return out.getvalue(), said


def _blank(pdf: pikepdf.Pdf, page: pikepdf.Page, why: str) -> None:
    """Make a page blank at its shown size (Letter where that is unknown), with the reason printed
    at its top: whoever looks at it (a reviewer, the model, a reader) is told why."""
    width, height = _shown_size(page)
    for key in ("/UserUnit", "/Annots", "/BleedBox", "/TrimBox", "/ArtBox"):
        if key in page.obj:
            del page.obj[key]
    page.obj["/MediaBox"] = pikepdf.Array([0, 0, width, height])
    page.obj["/CropBox"] = pikepdf.Array([0, 0, width, height])
    page.obj["/Rotate"] = 0
    font = pdf.make_indirect(
        pikepdf.Dictionary(
            Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
        )
    )
    page.obj["/Resources"] = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    words = why.encode("latin-1", "replace")
    words = words.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    top = _text(max(height - 36, Decimal(12)))
    page.obj["/Contents"] = pdf.make_stream(
        b"BT /F1 10 Tf 36 " + top.encode() + b" Td (" + words + b") Tj ET"
    )


def _shown_size(page: pikepdf.Page) -> tuple[Decimal, Decimal]:
    """The page's size as shown, from what it says (Letter where that is unreadable)."""
    try:
        media = _rect(_inherited(page, "/MediaBox"))
        crop = _rect(_inherited(page, "/CropBox")) or media
    except _PageRefused:
        crop = None
    if crop is None or crop[2] <= crop[0] or crop[3] <= crop[1]:
        return Decimal(612), Decimal(792)
    width, height = crop[2] - crop[0], crop[3] - crop[1]
    rotation, _ = _rotation(page)
    return (height, width) if rotation in (90, 270) else (width, height)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
