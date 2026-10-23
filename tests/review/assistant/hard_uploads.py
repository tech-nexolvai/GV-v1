"""Hard made-up uploads for the $0 pipeline (no client file, no client value): small PDFs drawn with
reportlab (and Pillow for the scanned page) that each break one assumption a real upload breaks.

Every sheet is a plain CAD-like page: an elevation of a cabinet run (rectangles), a chain of widths
and the overall on two dimension lines (ticks, extension lines, the value as text, or drawn as pen
strokes, or millimetres with [inches]). `run_pipeline` puts the files through the same $0 steps a
chat does before any AI: the front door (`chat_service.sync_uploads`), the set pack (reading,
labels, page map, reading coverage), the work list with code's own pairing (no page matcher), the
plan check, and the vendor-string check. It never makes a network call: `no_network` refuses every
internet connection for the test.
"""

from __future__ import annotations

import hashlib
import io
import json
import socket
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

PAGE = (1224.0, 792.0)
SHOP = (
    "the vendor's shop drawings (a sheet may carry the architect's drawing beside the vendor's, "
    "with GV's markup on top)"
)
ARCHITECT = "the architect's drawings"
PREVIOUS_ROUND = "the vendor's previous round of shop drawings (round {round})"

type Number = int | float | Fraction
type Labeller = Callable[[Number], str]


def inches_text(value: Number) -> str:
    """24" / 25 1/2" / 3/4"."""
    whole = int(value)
    rest = Fraction(value).limit_denominator(64) - whole
    if not rest:
        return f'{whole}"'
    return f'{whole} {rest.numerator}/{rest.denominator}"' if whole else f'{rest}"'


def mm_text(value: Number) -> str:
    """Millimetres with the inches in brackets, as metric vendors print: 610 [24"]."""
    return f"{round(float(value) * 25.4)} [{inches_text(value)}]"


@dataclass(frozen=True)
class Run:
    """One cabinet run drawn as an elevation: widths (inches) at `scale` points per inch, its left
    end at `x`, its floor at `y` (PDF points, y up). `printed` overrides what each label says (the
    chain first, the overall last): a vendor mistake or a disagreeing millimetre value."""

    widths: Sequence[Number]
    x: float = 200.0
    y: float = 200.0
    scale: float = 6.0
    height_in: float = 34.5
    labeller: Labeller = inches_text
    printed: Sequence[str] | None = None
    strokes: bool = False
    drawn_scale: Sequence[float] | None = None
    """Each part's drawn length as a share of its value (not to scale: "NTS")."""


@dataclass(frozen=True)
class TitledView:
    """One titled view of a sheet, as an architect prints it: a view-number bubble left of its
    title, the scale under the title, and the view's drawing above it (its runs, and its notes:
    `(text, x, y)` in PDF points, y up; `strokes`: the same drawn as pen strokes, no text)."""

    number: str
    title: str
    x: float
    y: float
    scale: str = '1/2" = 1\'-0"'
    notes: Sequence[tuple[str, float, float]] = ()
    strokes: Sequence[tuple[str, float, float]] = ()
    runs: Sequence[Run] = ()


@dataclass(frozen=True)
class Sheet:
    """One page: a title (none: no title at all), notes, runs, plan outlines, a table, its /Rotate."""

    title: str | None = None
    words: Sequence[str] = ()
    runs: Sequence[Run] = ()
    plans: Sequence[tuple[float, float, float, float, str]] = ()
    """Floor-plan outlines with no dimension: (x, y, width, height) in points and a room name."""
    table: Sequence[Sequence[str]] = ()
    rotate: int = 0
    title_size: int = 18
    views: Sequence[TitledView] = ()
    """Titled views (a plan and a section on one sheet, as an architect's sheet holds them)."""
    sheet_number: str | None = None
    """The sheet's own number, printed large in its title block (bottom right)."""


# ---- pen strokes: digits drawn as lines (no text), as some CAD exports print them ------------------

_SEGMENTS = {
    # 7-segment digits: a top, b upper right, c lower right, d bottom, e lower left, f upper left, g middle
    "0": "abcdef", "1": "bc", "2": "abged", "3": "abgcd", "4": "fgbc", "5": "afgcd",
    "6": "afgedc", "7": "abc", "8": "abcdefg", "9": "abcdfg",
}  # fmt: skip


def _stroke_digit(page: canvas.Canvas, x: float, y: float, digit: str, w: float, h: float) -> None:
    half = h / 2
    lines = {
        "a": (x, y + h, x + w, y + h), "b": (x + w, y + h, x + w, y + half),
        "c": (x + w, y + half, x + w, y), "d": (x, y, x + w, y),
        "e": (x, y, x, y + half), "f": (x, y + half, x, y + h), "g": (x, y + half, x + w, y + half),
    }  # fmt: skip
    for segment in _SEGMENTS[digit]:
        page.line(*lines[segment])


def stroke_text(page: canvas.Canvas, centre_x: float, y: float, text: str, size: float = 9) -> None:
    """Digits and an inch mark drawn as pen strokes (paths, no font): what a text reader cannot
    read."""
    w, h, gap = size * 0.45, size * 0.8, size * 0.25
    digits = [c for c in text if c.isdigit()]
    total = len(digits) * (w + gap) + gap * 2
    x = centre_x - total / 2
    page.setLineWidth(0.6)
    for c in text:
        if c.isdigit():
            _stroke_digit(page, x, y, c, w, h)
            x += w + gap
        elif c == '"':
            page.line(x, y + h, x, y + h * 0.65)
            page.line(x + gap, y + h, x + gap, y + h * 0.65)
    page.setLineWidth(1)


# ---- drawing --------------------------------------------------------------------------------------


def _label(page: canvas.Canvas, run: Run, x: float, y: float, text: str) -> None:
    if run.strokes:
        stroke_text(page, x, y, text)
    else:
        page.drawCentredString(x, y, text)


def draw_run(page: canvas.Canvas, run: Run) -> None:
    s = run.scale
    shares = run.drawn_scale or [1.0] * len(run.widths)
    edges = [run.x]
    for w, share in zip(run.widths, shares, strict=True):
        edges.append(edges[-1] + float(w) * s * share)
    floor, top = run.y, run.y + run.height_in * s
    page.setLineWidth(1)
    for left, right in pairwise(edges):
        page.rect(left, floor, right - left, top - floor)
    chain_y, overall_y = top + 40, top + 80
    page.setLineWidth(0.3)
    for edge in edges:
        page.line(edge, top + 5, edge, chain_y + 6)
    for edge in (edges[0], edges[-1]):
        page.line(edge, chain_y + 6, edge, overall_y + 6)
    page.setFont("Helvetica", 9)
    total = sum(Fraction(w).limit_denominator(64) for w in run.widths)
    texts = list(run.printed) if run.printed else [run.labeller(w) for w in run.widths]
    if not run.printed:
        texts.append(run.labeller(total))
    for left, right, text in zip(edges, edges[1:], texts, strict=False):
        page.line(left, chain_y, right, chain_y)
        page.line(left - 3, chain_y - 3, left + 3, chain_y + 3)
        page.line(right - 3, chain_y - 3, right + 3, chain_y + 3)
        _label(page, run, (left + right) / 2, chain_y + 3, text)
    page.line(edges[0], overall_y, edges[-1], overall_y)
    page.line(edges[0] - 3, overall_y - 3, edges[0] + 3, overall_y + 3)
    page.line(edges[-1] - 3, overall_y - 3, edges[-1] + 3, overall_y + 3)
    _label(page, run, (edges[0] + edges[-1]) / 2, overall_y + 3, texts[len(run.widths)])
    page.setLineWidth(1)


def _draw_sheet(page: canvas.Canvas, sheet: Sheet) -> None:
    _width, height = PAGE
    if sheet.title:
        page.setFont("Helvetica-Bold", sheet.title_size)
        page.drawString(60, 60, sheet.title)
    page.setFont("Helvetica", 10)
    for n, words in enumerate(sheet.words):
        page.drawString(60, height - 80 - 14 * n, words)
    for x, y, w, h, room in sheet.plans:
        page.setLineWidth(2)
        page.rect(x, y, w, h)
        page.setLineWidth(1)
        page.setFont("Helvetica", 8)
        page.drawCentredString(x + w / 2, y + h / 2, room)
    if sheet.table:
        page.setFont("Helvetica", 9)
        top, row_h, col_w = height - 160, 18.0, 150.0
        for r, row in enumerate(sheet.table):
            for c, cell in enumerate(row):
                page.rect(60 + c * col_w, top - (r + 1) * row_h, col_w, row_h)
                page.drawString(64 + c * col_w, top - (r + 1) * row_h + 5, cell)
    for run in sheet.runs:
        draw_run(page, run)
    for view in sheet.views:
        draw_view(page, view)
    if sheet.sheet_number:
        page.setFont("Helvetica-Bold", 30)
        page.drawString(PAGE[0] - 160, 40, sheet.sheet_number)


def draw_view(page: canvas.Canvas, view: TitledView) -> None:
    """A titled view: its bubble, title and scale, its notes (text or pen strokes) and runs."""
    page.setFont("Helvetica-Bold", 12)
    page.drawString(view.x, view.y, view.title)
    page.setFont("Helvetica", 10)
    page.drawRightString(view.x - 14, view.y, view.number)
    page.circle(view.x - 19, view.y + 3.5, 9)
    page.setFont("Helvetica", 8)
    page.drawString(view.x, view.y - 13, view.scale)
    page.setFont("Helvetica", 8)
    for text, x, y in view.notes:
        page.drawString(x, y, text)
    for text, x, y in view.strokes:
        stroke_text(page, x, y, text, size=8)
    for run in view.runs:
        draw_run(page, run)


def pdf(sheets: Sequence[Sheet]) -> bytes:
    """One page per sheet. A sheet's `rotate` is written as the page's /Rotate flag."""
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=PAGE)
    for sheet in sheets:
        _draw_sheet(page, sheet)
        page.showPage()
    page.save()
    data = out.getvalue()
    if any(sheet.rotate for sheet in sheets):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
        for written, sheet in zip(writer.pages, sheets, strict=True):
            if sheet.rotate:
                written.rotate(sheet.rotate)
        rotated = io.BytesIO()
        writer.write(rotated)
        data = rotated.getvalue()
    return data


def scanned_pdf(sheet: Sheet, dpi: int = 100) -> bytes:
    """The sheet as a scan: one picture covering the page, no text and no vector at all. Drawn
    with Pillow (lines and the labels as pixels), so no PDF engine is involved."""
    scale = dpi / 72
    width, height = (round(v * scale) for v in PAGE)
    picture = Image.new("L", (width, height), 255)
    pen = ImageDraw.Draw(picture)
    font = ImageFont.load_default(size=round(10 * scale))

    def at(x: float, y: float) -> tuple[float, float]:
        return x * scale, (PAGE[1] - y) * scale

    for run in sheet.runs:
        edges = [run.x]
        for w in run.widths:
            edges.append(edges[-1] + float(w) * run.scale)
        floor, top = run.y, run.y + run.height_in * run.scale
        for left, right in pairwise(edges):
            pen.rectangle([*at(left, top), *at(right, floor)], outline=0, width=2)
        chain_y = top + 40
        pen.line([*at(edges[0], chain_y), *at(edges[-1], chain_y)], fill=0, width=1)
        for left, right, w in zip(edges, edges[1:], run.widths, strict=False):
            x, y = at((left + right) / 2, chain_y + 14)
            pen.text((x, y), inches_text(w), fill=0, font=font, anchor="mm")
    if sheet.title:
        pen.text(at(60, 60), sheet.title, fill=0, font=font)
    raw = io.BytesIO()
    picture.save(raw, format="PNG")
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=PAGE)
    page.drawImage(ImageReader(io.BytesIO(raw.getvalue())), 0, 0, *PAGE)
    page.showPage()
    page.save()
    return out.getvalue()


def with_markup(
    data: bytes, page_index: int, box: tuple[float, float, float, float], text: str
) -> bytes:
    """GV's markup over a vendor value: a yellow FreeText note with red text, as GV's reviewer adds
    it (an annotation: never the vendor's ink)."""
    from pypdf import PdfReader, PdfWriter
    from pypdf.annotations import FreeText

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    note = FreeText(text=text, rect=box, font_size="9pt", font_color="ff0000",
                    background_color="ffff00")  # fmt: skip
    writer.add_annotation(page_number=page_index, annotation=note)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def locked_pdf(data: bytes, password: str = "made-up-test-password") -> bytes:
    """The same PDF locked with a user password (it cannot be opened without it)."""
    from pypdf import PdfReader, PdfWriter

    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(data)))
    writer.encrypt(user_password=password, owner_password=password)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def damaged_pdf(data: bytes) -> bytes:
    """A PDF cut short in the middle, with its cross-reference table gone (a broken download)."""
    return data[: len(data) // 3]


def blank_pages(count: int, *, numbered: bool = False) -> bytes:
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=PAGE)
    for n in range(count):
        if numbered:
            page.setFont("Helvetica", 8)
            page.drawString(PAGE[0] - 80, 30, f"SHEET {n + 1}")
        page.showPage()
    page.save()
    return out.getvalue()


# ---- the $0 pipeline ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Upload:
    name: str
    data: bytes
    kind: str = SHOP


@dataclass
class Outcome:
    """What the $0 steps made of a set, and how long they took."""

    listing: list[dict[str, Any]]
    pack: Path
    uploads: Path
    status: dict[str, Any]
    seconds: float
    page_map: dict[str, Any] | None = None
    dims: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None
    work: Any = None
    work_error: str | None = None
    doubts: list[Any] = field(default_factory=list)
    summary: str | None = None
    vendor: Any = None

    def labels(self, file: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in (self.dims or {}).get("pages") or []:
            if file is None or page.get("file") == file:
                out += [dict(label, page=page.get("page"), file=page.get("file"))
                        for label in page.get("labels") or []]  # fmt: skip
        return out

    def label_inches(self, file: str | None = None) -> list[Fraction]:
        return [Fraction(str(label["inches"])).limit_denominator(64)
                for label in self.labels(file) if label.get("inches") is not None]  # fmt: skip

    def roles(self) -> list[tuple[str, int, str, list[str]]]:
        return [(str(e["file"]), int(e["page"]), str(e.get("role")),
                 [str(h.get("role")) for h in e.get("halves") or []])
                for e in (self.page_map or {}).get("pages") or []]  # fmt: skip

    def across(self) -> list[Any]:
        return list(getattr(self.work, "across", ()) or ())

    def paired(self) -> dict[int, tuple[str, ...]]:
        """Vendor page -> the architect items code paired it with (across the files)."""
        out: dict[int, tuple[str, ...]] = {}
        for pair in self.across():
            page = int(getattr(pair.vendor, "own_page", None) or pair.vendor.page)
            out[page] = tuple(out.get(page, ())) + tuple(pair.chosen)
        return out


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No internet for the test (no paid AI can be reached): every connection is refused, and no
    API key is set."""
    real = socket.socket.connect

    def refuse(self: socket.socket, address: Any) -> None:
        if self.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError(f"network refused in this test ({address!r})")
        real(self, address)

    monkeypatch.setattr(socket.socket, "connect", refuse)
    for name in ("ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    yield


def run_pipeline(
    tmp_path: Path, uploads: Sequence[Upload], *, main_file: str | None = None
) -> Outcome:
    """The uploads through the front door, the set pack and every $0 step after it."""
    from app.review.assistant import plan_check, reading_coverage, set_pack
    from app.review.assistant import vendor_consistency as vc
    from app.review.assistant.chat_service import UploadSource, sync_uploads
    from app.review.assistant.sandbox import ChatWorkspace
    from app.review.assistant.simple_check.worklist import build_worklist

    started = time.monotonic()
    workspace = ChatWorkspace(tmp_path / "chat")
    sources = []
    for upload in uploads:
        data = upload.data
        sources.append(
            UploadSource(
                name=upload.name,
                sha256=hashlib.sha256(data).hexdigest(),
                kind=upload.kind,
                pages=1,
                read=lambda data=data: data,  # type: ignore[misc]
            )
        )
    listing, _changed = sync_uploads(workspace, sources)
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, workspace.uploads, listing, background=False)
    outcome = Outcome(
        listing=listing,
        pack=folder,
        uploads=workspace.uploads,
        status=set_pack.read_status(folder),
        seconds=0.0,
    )

    def load(name: str) -> Any:
        try:
            return json.loads((folder / name).read_text())
        except (OSError, ValueError):
            return None

    outcome.page_map = load("page_map.json")
    outcome.dims = load("dims.json")
    outcome.coverage = reading_coverage.load(folder)
    main = main_file or next(
        (u.name for u in uploads if u.kind == SHOP),
        next((u.name for u in uploads if u.kind != ARCHITECT), uploads[0].name),
    )
    if outcome.page_map is not None:
        try:
            outcome.work = build_worklist(folder, main)
        except Exception as failed:  # noqa: BLE001 - the test says what broke
            outcome.work_error = f"{type(failed).__name__}: {failed}"
        outcome.doubts = plan_check.doubts(outcome.page_map, main, limit=None)
        outcome.summary = plan_check.plan_summary(outcome.page_map, main)
        outcome.vendor = vc.check_pack(folder, workspace.uploads)
    outcome.seconds = time.monotonic() - started
    return outcome
