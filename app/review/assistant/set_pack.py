"""The set pack: what code prepares once per drawing set, so the chat agent starts reading at once (#1497).

A whole-set answer used to spend its first turns on set-up (a page map built by hand, a
drawings-only copy, every page rendered) and its helpers on one `pdftoppm` crop per call. Code does
that set-up once per upload, in the background, and the agent sees it read-only at
`/mnt/user-data/set/`:

- `<name>_drawings_only.pdf`: a copy of each PDF that keeps only the pasted drawings (`/Stamp`
  annotations), so GV's markup is hidden (SKILL.md section 5). A page with no stamps is its own
  page content, without annotations.
- `pages/<name>/p-001.png`: every page at 100 dpi, from the drawings-only copy.
- `contact/<name>-01.png`: contact sheets, nine pages to a picture, each page number stamped.
- `annotations.json`: every annotation (file, page, kind, author, text, box), GV's headings and
  notes included: the drawings-only copy drops them.
- `page_map.json`: per page and per drawing on the sheet: whose drawing it is (architect / vendor
  / brand_prototype / general) from the heading GV typed above or below it
  (`vocabulary/panel_headings.py`), its round ("first / second set of comments"), the area or title
  GV typed ("2. Buffet"; carried to the next pages until another), and sheet numbers printed on
  the page. It is a hint for the agent, read by code from GV's labels: the drawings decide.
- `dims.json` (#1558): every printed dimension on every page, numbered per page (`D17`), with its
  value, inches, box, whose drawing it is on, and its dimension and extension lines when found
  (`dim_labels.py`). The `zoom` tool draws these ids on a crop; findings name them. Written as
  it grows, page by page, after the pictures; a page not in it yet is read when it is asked for.
  Every page's text labels come first; then the vendor's drawn numbers (pen strokes, read by two
  OCR engines) are added page by page within `DIMS_DRAWN_BUDGET_S`, after the text ids.
- `scales.json`: each drawing's scale measured from its own printed dimensions
  (`view_scales.py`; printed scale notes are wrong where GV resized a pasted drawing), written
  with `dims.json`. `measure_on_drawing` uses it when no calibration is given.
- `notes.json`: each drawing's printed tags and notes (`drawing_notes.py`, from the phrases
  `dims.json` keeps), and the architect's tags the vendor's drawing does not print.
- `pairs.json`: architect against vendor dimensions paired by drawn position (`dim_pairs.py`,
  wrapping `extraction/architect/pairing.py`) or by anchors, what both ends land on (`dim_anchors.py`:
  the same wall, floor or outlet on both drawings, no scale used), each `same` or `different`.
- `status.json`: `building`, `ready` or `failed`, and the steps done. The agent can work before
  the pack is ready (the first message never waits); the `zoom` tool makes the drawings-only copy
  it needs at once if the pack has not made it yet.

**Made once per upload.** The pack's folder is named by a hash of the uploads' names and SHA-256
(`set-packs/<key>` under the chat root), so every chat on the same PDFs reuses it. Each file is
written to a temporary name and renamed, so a reader never sees half a file.

**Boxes** in `page_map.json` and `annotations.json` are in PDF points of the page as shown (its
rotation applied), origin top-left: the units of `pdftotext -bbox`, `report_finding` and `zoom`.

**Loaded by name, never imported**: this module draws with pypdfium2 and Pillow, which `app/api/`
must not reach by import (`tests/api/test_no_heavy_work.py`); `chat_service` and the `zoom` tool
load it with `import_module`. pdfium is not thread-safe: every use holds `chat_drawing`'s lock.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject

from app.review.assistant import page_cache
from app.review.assistant.chat_drawing import PDFIUM_LOCK
from app.review.assistant.page_pictures import fitting_dpi
from vocabulary.panel_headings import HeadingMeaning, heading_meaning

__all__ = [
    "HIRES",
    "PACK_VERSION",
    "Crop",
    "build_pack",
    "crop_picture",
    "drawings_only",
    "ensure_pack",
    "pack_folder",
    "pack_key",
    "trim_hires",
]

_log = logging.getLogger(__name__)

#: Changes when what the pack holds changes: an older pack is built again.
PACK_VERSION: Final = 8
RENDER_DPI: Final = 100
#: Seconds the dimension labels' drawn-ink route (OCR) may spend on one set, after every page's
#: text labels (`GV_DIM_LABELS_DRAWN_BUDGET_S`; 0 turns it off). It reads the vendor's drawn
#: numbers label by label in the labels' own low-priority process (#1571); pages it does not reach
#: stay `not_run`, and their drawn values keep `verify_value`.
DIMS_DRAWN_BUDGET_S: Final = 1200.0
#: The hi-res page cache's folder inside the pack (`page_cache`, #1548): 300-dpi tiles of each page
#: a crop was cut from, made on first need.
HIRES: Final = "hires"
#: A set no chat uses any more keeps its hi-res cache while a page of it was used this recently
#: (seconds): a chat changing its PDFs right now is not raced (#1575).
UNUSED_SET_GRACE_S: Final = 600.0
#: Contact sheets: pages per picture (3 x 3) and each tile's size, so a sheet is at most 2576 px.
CONTACT_COLUMNS: Final = 3
CONTACT_ROWS: Final = 3
CONTACT_TILE: Final = (850, 620)
_LABEL_PX: Final = 44
#: A zoom crop: the longest side the model reads unresized, and the dpi range.
MAX_CROP_EDGE_PX: Final = 2576
MIN_CROP_DPI: Final = 72
MAX_CROP_DPI: Final = 600
_CROP_PNG_LEVEL: Final = 3
#: GV types its headings and round captions at 24 pt, its notes at 12 pt or less.
_LABEL_MIN_PT: Final = 14
#: A section title on a divider page (36 pt).
_SECTION_MIN_PT: Final = 30
#: A round caption belongs to a drawing when it is this close to it (points).
_CAPTION_PT: Final = 60
_ROUNDS: Final = re.compile(
    r"\b(first|second|third|fourth|fifth|1st|2nd|3rd|4th|5th)\s+(?:set\s+of\s+comments|round|"
    r"submittal|submission)\b",
    re.IGNORECASE,
)
_ROUND_NUMBER: Final = {
    "first": 1,
    "1st": 1,
    "second": 2,
    "2nd": 2,
    "third": 3,
    "3rd": 3,
    "fourth": 4,
    "4th": 4,
    "fifth": 5,
    "5th": 5,
}
_AREA: Final = re.compile(r"^\s*\d{1,3}\s*[.)]\s*\S")
_FONT_SIZE: Final = re.compile(r"(\d+(?:\.\d+)?)\s+Tf")
_SHEET: Final = re.compile(
    r"\b(?:SHEET|DWG|DRAWING)\s*(?:NO\.?|NUMBER|#)\s*[:.]?\s*([A-Z]{0,4}-?\d{1,4}(?:\.\d{1,3})?)\b"
    r"|\b((?:A|ID|SD|SH|MW|M|D)-?\d{3}(?:\.\d{1,2})?[A-Z]?)\b"
)
_ROLES: Final = {
    HeadingMeaning.ARCH: "architect",
    HeadingMeaning.SHOP: "vendor",
    HeadingMeaning.REFERENCE: "brand_prototype",
}
_SAFE: Final = re.compile(r"[^A-Za-z0-9._-]+")

#: Builds running in this process, by pack folder (a second chat on the same set waits for none).
_running: dict[Path, threading.Thread] = {}
_running_lock: Final = threading.Lock()
#: One lock per drawings-only copy, so the build and a `zoom` never write it at once.
_copy_locks: dict[Path, threading.Lock] = {}

type Box = tuple[float, float, float, float]


# ---- where the pack lives ------------------------------------------------------------------------


def pack_key(listing: Sequence[Mapping[str, Any]]) -> str:
    """The pack's name: a hash of the uploads' names and SHA-256, in order. A file the front door
    changed (`working_sha256`, #1724) adds its working copy's, so its pack is made from that copy;
    a file it left as it was keeps the name its pack always had."""
    text = "\n".join(
        f"{item.get('name')}:{item.get('sha256')}"
        + (f":{item['working_sha256']}" if item.get("working_sha256") else "")
        for item in listing
    )
    return hashlib.sha256(f"v{PACK_VERSION}\n{text}".encode()).hexdigest()[:32]


def pack_folder(chat_root: Path, listing: Sequence[Mapping[str, Any]]) -> Path:
    return chat_root / "set-packs" / pack_key(listing)


def trim_hires(
    chat_root: Path,
    live_keys: frozenset[str] | None,
    *,
    set_bytes: int,
    total_bytes: int,
) -> page_cache.Trimmed:
    """Keep the hi-res page caches under the chat root bounded (#1575).

    1. A pack no chat or plan check points at any more (`live_keys`: its chats were removed, its
       PDFs replaced, or the pack format changed) loses its whole `hires/` folder, unless a page of
       it was used in the last `UNUSED_SET_GRACE_S`. `live_keys` is `None` when they could not all
       be read: then no set's cache is dropped this time.
    2. Each set's cache (a pack's, or a chat's own `set-local/` one) is trimmed to `set_bytes`,
    3. then all of them together to `total_bytes`, the pages used longest ago first.

    Only the cache is touched: anything removed is drawn again on its next crop."""
    caches: list[Path] = []
    pages, freed = 0, 0
    now = datetime.now(UTC).timestamp()
    try:
        packs = sorted(path for path in (chat_root / "set-packs").iterdir() if path.is_dir())
    except OSError:
        packs = []
    for pack in packs:
        cache = pack / HIRES
        if not cache.is_dir() or cache.is_symlink():
            continue
        if live_keys is not None and pack.name not in live_keys:
            used = page_cache.last_used(cache)
            if used is None or now - used > UNUSED_SET_GRACE_S:
                gone = page_cache.remove(cache)
                pages, freed = pages + gone.pages, freed + gone.freed
                continue
        caches.append(cache)
    caches += sorted(path for path in chat_root.glob(f"*/set-local/{HIRES}") if path.is_dir())
    for cache in caches:
        done = page_cache.trim([cache], set_bytes)
        pages, freed = pages + done.pages, freed + done.freed
    done = page_cache.trim(caches, total_bytes)
    return page_cache.Trimmed(pages + done.pages, freed + done.freed, done.held)


def stem(name: str) -> str:
    """An upload's name as the pack's file names use it ("AI_Set_3.pdf" -> "AI_Set_3")."""
    return _SAFE.sub("_", Path(name).stem).strip("._") or "file"


def _write_bytes(path: Path, data: bytes) -> None:
    """Written whole, then renamed into place; readable by the sandbox's unprivileged user."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o755)
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def _write_json(path: Path, value: object) -> None:
    _write_bytes(path, json.dumps(value, ensure_ascii=False, indent=1).encode("utf-8"))


def read_status(folder: Path) -> dict[str, Any]:
    try:
        status = json.loads((folder / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return status if isinstance(status, dict) else {}


def _set_status(folder: Path, state: str, done: Sequence[str], **more: Any) -> None:
    _write_json(
        folder / "status.json",
        {
            "state": state,
            "version": PACK_VERSION,
            "done": list(done),
            "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
            **more,
        },
    )


# ---- the drawings-only copy ----------------------------------------------------------------------


def drawings_only(folder: Path, uploads: Path, name: str) -> Path:
    """The drawings-only copy of upload `name`, made now if the pack has not made it yet."""
    out = folder / f"{stem(name)}_drawings_only.pdf"
    with _running_lock:
        lock = _copy_locks.setdefault(out, threading.Lock())
    with lock:
        if not out.is_file():
            source = uploads / name
            if not source.is_file():
                raise FileNotFoundError(name)
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            writer = PdfWriter(clone_from=str(source))
            for page in writer.pages:
                if "/Annots" in page:
                    annotations: Any = page["/Annots"]
                    keep = [a for a in annotations if a.get_object().get("/Subtype") == "/Stamp"]
                    page[NameObject("/Annots")] = ArrayObject(keep)
                _hide_gv_marks(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            _write_bytes(out, buffer.getvalue())
    return out


def _hide_gv_marks(page: Any) -> None:
    """Empty GV Review's own overlays on a page of the drawings-only copy (`extraction/gv_marks`:
    signed with `/PieceInfo /GVReview`, or by an older overlay's reserved name)."""
    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else None
    xobjects = resources.get("/XObject") if resources is not None else None
    xobjects = xobjects.get_object() if xobjects is not None else None
    if xobjects is None:
        return
    for name in list(xobjects.keys()):
        form = xobjects[name].get_object()
        piece = form.get("/PieceInfo")
        signed = piece is not None and "/GVReview" in piece.get_object()
        if form.get("/Subtype") == "/Form" and (signed or name in _GV_RESERVED):
            form.set_data(b"")


#: The resource names GV's overlays used before they were signed (`gv_marks.RESERVED_NAMES`).
_GV_RESERVED: Final = frozenset({"/GVCloudDrawing", "/GVRedline939"})


# ---- annotations and the page map ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Geometry:
    """A page's boxes and rotation, to turn its drawing-space boxes into shown boxes."""

    llx: float
    lly: float
    urx: float
    ury: float
    rotation: int

    def shown(self, x: float, y: float) -> tuple[float, float]:
        if self.rotation == 90:
            return y - self.lly, x - self.llx
        if self.rotation == 180:
            return self.urx - x, y - self.lly
        if self.rotation == 270:
            return self.ury - y, self.urx - x
        return x - self.llx, self.ury - y

    def box(self, rect: Sequence[float]) -> Box:
        x0, y0, x1, y1 = (float(v) for v in rect)
        a, b = self.shown(x0, y0), self.shown(x1, y1)
        return (
            round(min(a[0], b[0]), 1),
            round(min(a[1], b[1]), 1),
            round(max(a[0], b[0]), 1),
            round(max(a[1], b[1]), 1),
        )


def _geometry(page: Any) -> _Geometry:
    box = page.cropbox
    return _Geometry(
        float(box.left),
        float(box.bottom),
        float(box.right),
        float(box.top),
        int(page.get("/Rotate", 0) or 0) % 360,
    )


def _font_size(annotation: Mapping[str, Any]) -> float | None:
    found = _FONT_SIZE.search(str(annotation.get("/DA") or ""))
    return float(found.group(1)) if found else None


def _annotations(name: str, reader: PdfReader) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for number, page in enumerate(reader.pages, start=1):
        geometry = _geometry(page)
        for raw in page.get("/Annots") or []:
            try:
                annotation = raw.get_object()
                rect = annotation.get("/Rect")
                box = geometry.box(rect) if rect is not None and len(rect) == 4 else None
            except (AttributeError, TypeError, ValueError):
                continue
            kind = str(annotation.get("/Subtype") or "").lstrip("/")
            text = str(annotation.get("/Contents") or "").replace("\r", "\n").strip()
            out.append(
                {
                    "file": name,
                    "page": number,
                    "kind": kind,
                    "author": str(annotation.get("/T") or "") or None,
                    "text": text[:500] or None,
                    "font_pt": _font_size(annotation),
                    "box": box,
                }
            )
    return out


def _gap(a: Box, b: Box) -> tuple[float, float]:
    """Vertical and horizontal gaps between two boxes (0 where they overlap)."""
    vertical = max(0.0, max(a[1], b[1]) - min(a[3], b[3]))
    horizontal = max(0.0, max(a[0], b[0]) - min(a[2], b[2]))
    return vertical, horizontal


def _nearest(drawing: Box, labels: Sequence[dict[str, Any]], side: str) -> dict[str, Any] | None:
    """The label nearest a drawing on one side: `above` it (or inside its upper margin), or
    `below` it as a caption (or inside its lower margin)."""
    x0, y0, x1, y1 = drawing
    margin = (y1 - y0) / 4
    best: tuple[float, dict[str, Any]] | None = None
    for label in labels:
        lx0, ly0, lx1, ly1 = label["box"]
        if side == "above" and ly0 > y0 + margin:
            continue
        if side == "below" and ly1 < y1 - margin:
            continue
        vertical = max(0.0, y0 - ly1) if side == "above" else max(0.0, ly0 - y1)
        horizontal = max(0.0, max(x0, lx0) - min(x1, lx1))
        score = vertical + horizontal / 2
        if best is None or score < best[0]:
            best = (score, label)
    return None if best is None else best[1]


def _headings(
    stamps: Sequence[dict[str, Any]], labels: Sequence[dict[str, Any]]
) -> list[dict[str, Any] | None]:
    """Each drawing's own heading. A page puts its headings above its drawings or below them as
    captions (`extraction/panels.py`): the side that gives more drawings a heading wins (above
    on a tie)."""
    above = [_nearest(stamp["box"], labels, "above") for stamp in stamps]
    below = [_nearest(stamp["box"], labels, "below") for stamp in stamps]
    count = sum(1 for found in above if found)
    return above if count >= sum(1 for found in below if found) else below


def _round_of(text: str) -> int | None:
    found = _ROUNDS.search(text)
    return _ROUND_NUMBER[found.group(1).lower()] if found else None


def _page_entries(
    name: str, annotations: Sequence[dict[str, Any]], texts: Sequence[str], page_count: int
) -> list[dict[str, Any]]:
    by_page: dict[int, list[dict[str, Any]]] = {}
    for item in annotations:
        by_page.setdefault(int(item["page"]), []).append(item)
    entries: list[dict[str, Any]] = []
    area: str | None = None
    area_page: int | None = None
    section: str | None = None
    for number in range(1, page_count + 1):
        items = by_page.get(number, [])
        stamps = [a for a in items if a["kind"] == "Stamp" and a["box"]]
        labels = [
            a
            for a in items
            if a["kind"] == "FreeText"
            and a["text"]
            and a["box"]
            and (a["font_pt"] or 0) >= _LABEL_MIN_PT
        ]
        role_labels = [
            {**a, "role": _ROLES[meaning]}
            for a in labels
            if (meaning := heading_meaning(str(a["text"]))) is not None
        ]
        round_labels = [
            {**a, "round": found} for a in labels if (found := _round_of(str(a["text"])))
        ]
        titled = False
        for label in labels:
            text = " ".join(str(label["text"]).split())
            if heading_meaning(text) is not None or _round_of(text) is not None:
                continue
            if (label["font_pt"] or 0) >= _SECTION_MIN_PT and not stamps:
                section, area, area_page, titled = text, None, None, True
            elif _AREA.match(text) and not titled:
                area, area_page, titled = text, number, True
        halves: list[dict[str, Any]] = []
        ordered = sorted(stamps, key=lambda a: (a["box"][1], a["box"][0]))
        one_round = {label["round"] for label in round_labels}
        for stamp, heading in zip(ordered, _headings(ordered, role_labels), strict=True):
            role = heading["role"] if heading else "general"
            # A round belongs to the vendor's drawings (never the architect's or a prototype):
            # its caption right under or over the drawing, or the page's only round.
            found = None
            if role in ("vendor", "general"):
                caption = _headings([stamp], round_labels)[0]
                gap = None if caption is None else _gap(stamp["box"], caption["box"])
                if caption is not None and gap is not None and gap[0] <= _CAPTION_PT and not gap[1]:
                    found = caption["round"]
                elif len(one_round) == 1:
                    found = next(iter(one_round))
            halves.append(
                {
                    "box": stamp["box"],
                    "role": role,
                    "heading": " ".join(str(heading["text"]).split()) if heading else None,
                    "round": found,
                }
            )
        roles = sorted({half["role"] for half in halves} - {"general"})
        rounds = sorted({half["round"] for half in halves if half["round"]})
        if not halves and round_labels:
            rounds = sorted({label["round"] for label in round_labels})
        sheets: list[str] = []
        for printed in _SHEET.finditer(texts[number - 1] if number - 1 < len(texts) else ""):
            sheet = printed.group(1) or printed.group(2)
            if sheet and sheet not in sheets:
                sheets.append(sheet)
        entries.append(
            {
                "file": name,
                "page": number,
                "role": "+".join(roles) if roles else "general",
                "halves": halves,
                "rounds": rounds,
                "section": section,
                "area": area,
                "area_from_page": area_page if area is not None and area_page != number else None,
                "sheet_numbers": sheets[:4],
                "headings": [
                    " ".join(str(a["text"]).split())
                    for a in labels
                    if heading_meaning(str(a["text"])) is not None
                ],
            }
        )
    return entries


# ---- pictures ------------------------------------------------------------------------------------


def _page_texts(pdf: Path) -> list[str]:
    """Each page's text, the pasted drawings' own text included (flattened in memory)."""
    texts: list[str] = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            for index in range(len(document)):
                page = document[index]
                try:
                    pdfium.raw.FPDFPage_Flatten(page.raw, pdfium.raw.FLAT_NORMALDISPLAY)
                    page.close()
                    page = document[index]
                    textpage = page.get_textpage()
                    texts.append(textpage.get_text_range())
                    textpage.close()
                except Exception:  # noqa: BLE001 - a page with no readable text has none
                    texts.append("")
                finally:
                    page.close()
        finally:
            document.close()
    return texts


def _render_pages(pdf: Path, out: Path, dpi: int) -> list[Path]:
    paths: list[Path] = []
    with PDFIUM_LOCK:
        count = len(pdfium.PdfDocument(str(pdf)))
    for index in range(count):
        with PDFIUM_LOCK:
            document = pdfium.PdfDocument(str(pdf))
            try:
                picture = document[index].render(scale=dpi / 72, may_draw_forms=True).to_pil()
            finally:
                document.close()
        buffer = io.BytesIO()
        picture.convert("RGB").save(buffer, format="PNG", optimize=True)
        path = out / f"p-{index + 1:03d}.png"
        _write_bytes(path, buffer.getvalue())
        paths.append(path)
    return paths


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # an older Pillow without sizes
        return ImageFont.load_default()


def _contact_sheets(name: str, pages: Sequence[Path], out: Path) -> list[Path]:
    per_sheet = CONTACT_COLUMNS * CONTACT_ROWS
    tile_w, tile_h = CONTACT_TILE
    font = _font(32)
    made: list[Path] = []
    for start in range(0, len(pages), per_sheet):
        chunk = pages[start : start + per_sheet]
        rows = -(-len(chunk) // CONTACT_COLUMNS)
        sheet = Image.new("RGB", (tile_w * CONTACT_COLUMNS, tile_h * rows), "white")
        draw = ImageDraw.Draw(sheet)
        for n, path in enumerate(chunk):
            x, y = (n % CONTACT_COLUMNS) * tile_w, (n // CONTACT_COLUMNS) * tile_h
            with Image.open(path) as opened:
                thumb = opened.convert("RGB")
            thumb.thumbnail((tile_w - 12, tile_h - _LABEL_PX - 8))
            sheet.paste(thumb, (x + (tile_w - thumb.width) // 2, y + _LABEL_PX))
            draw.rectangle((x + 2, y + 2, x + tile_w - 3, y + tile_h - 3), outline=(160, 160, 160))
            draw.rectangle((x + 4, y + 4, x + 150, y + _LABEL_PX - 2), fill=(200, 0, 0))
            draw.text((x + 12, y + 6), f"p {start + n + 1}", fill="white", font=font)
        buffer = io.BytesIO()
        sheet.save(buffer, format="PNG", optimize=True)
        last = start + len(chunk)
        path = out / f"{stem(name)}-p{start + 1:03d}-{last:03d}.png"
        _write_bytes(path, buffer.getvalue())
        made.append(path)
    return made


# ---- building ------------------------------------------------------------------------------------

_README: Final = """\
The set pack: made by code from the uploads, read-only. Start here instead of building your own.

page_map.json          per page: whose drawing each pasted drawing is (from GV's typed headings),
                       its round, the area or title, sheet numbers. A hint: the drawings decide.
annotations.json       every annotation: file, page, kind, author, text, box (GV's markup and
                       headings included; the drawings-only copies drop them).
*_drawings_only.pdf    each PDF with only the pasted drawings kept: GV's markup hidden.
pages/<name>/p-NNN.png every page at 100 dpi (drawings only). Zoom to read numbers.
                       <name> is the upload's name without ".pdf" (A.pdf: pages/A/p-003.png);
                       NNN is the page with three digits.
contact/*.png          nine pages per picture, page numbers stamped: the whole set at a glance.
dims.json              every printed dimension code found, numbered per page (D17): value,
                       inches, box, side, dimension line. zoom marks: true draws the ids.
                       route "drawn": numbers drawn as pen strokes (the vendor's, and an
                       architect's hand lettering such as 2'-6 1/2") read by two OCR engines
                       (added later; check one with zoom before you rely on it).
                       "derived": an EQ part, numbered last: the printed overall (less
                       the chain's printed parts) shared equally, by code; "from" names
                       the overall (36" wall to wall marked EQ / EQ is 18" each side).
scales.json            each drawing's scale measured from its own printed dimensions (labels
                       that agree within 1%), its box, and the printed scale note beside it
                       (note_agrees false: GV resized the drawing; trust the measured scale).
                       Per label: drawn_in, off_by, drawn_to_value.
notes.json             per drawing: the tags (FN-01, FX-101-CUST) and notes it prints, and per
                       page the architect's tags the vendor's drawing does not print.
pairs.json             architect against vendor dimensions paired by drawn position (both
                       ends on each other's ticks at each drawing's measured scale, never by
                       value) or by anchors (method anchors: both ends land on the same wall,
                       floor or outlet on both drawings, no scale used), each same or different;
                       and why a row could not be paired.
status.json            building / ready / failed.
hires/                 code's 300-dpi tiles of the pages zoom has cut from (not for reading:
                       zoom gives you any area sharp).

Boxes are PDF points of the page as displayed (rotation applied), origin top-left, y down
(as pdftotext -bbox and zoom use).
"""


def build_pack(folder: Path, uploads: Path, listing: Sequence[Mapping[str, Any]]) -> None:
    """Build the whole pack into `folder` (status at each step). Raises on a failure, after
    `status.json` says `failed`."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    names = [str(item["name"]) for item in listing]
    done: list[str] = []
    _set_status(folder, "building", done, files=names)
    _write_bytes(folder / "README.txt", _README.encode("utf-8"))
    try:
        copies = {name: drawings_only(folder, uploads, name) for name in names}
        done.append("drawings_only")
        _set_status(folder, "building", done, files=names)
        annotations: list[dict[str, Any]] = []
        page_map: list[dict[str, Any]] = []
        for name in names:
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            reader = PdfReader(str(uploads / name))
            found = _annotations(name, reader)
            annotations += found
            page_map += _page_entries(name, found, _page_texts(copies[name]), len(reader.pages))
        _write_json(folder / "annotations.json", annotations)
        _write_json(
            folder / "page_map.json",
            {"version": PACK_VERSION, "files": names, "pages": page_map},
        )
        done.append("page_map")
        _set_status(folder, "building", done, files=names)
        for name in names:
            pages = _render_pages(copies[name], folder / "pages" / stem(name), RENDER_DPI)
            _contact_sheets(name, pages, folder / "contact")
        done += ["pages", "contact_sheets"]
        _set_status(folder, "building", done, files=names)
        try:
            _dims(folder, uploads, names, page_map)
            done.append("dims")
        except Exception as failed:  # noqa: BLE001 - the pack works without labels
            _log.warning("assistant set pack labels failed: %s", type(failed).__name__)
    except Exception as failed:
        _set_status(folder, "failed", done, files=names, error=type(failed).__name__)
        raise
    _set_status(folder, "ready", done, files=names)


def _drawn_budget() -> float:
    try:
        return max(0.0, float(os.environ.get("GV_DIM_LABELS_DRAWN_BUDGET_S", DIMS_DRAWN_BUDGET_S)))
    except ValueError:
        return DIMS_DRAWN_BUDGET_S


#: The most seconds the labels' own process may take for one set: the text route and the drawn
#: route's budget, with room to spare.
DIMS_TIMEOUT_S: Final = 2400


#: What the labels' own process gets of the server's environment: enough to run Python, find this
#: code and tune the labels, never a key, a token or the database's address. That process parses the
#: set's PDFs, so a crafted PDF that broke a parser there would find nothing to steal (security
#: audit 2026-10-18).
_CHILD_VARIABLES: Final = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "TZ")
_CHILD_PREFIXES: Final = ("GV_DIM_LABELS_", "GV_ASSISTANT_PAGE_CACHE_")
_SECRET_WORDS: Final = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|_URL$", re.IGNORECASE)


def child_environment(root: Path, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of the labels' own process: an allow-list of the server's (`root` is put
    first on `PYTHONPATH`)."""
    source = os.environ if environ is None else environ
    kept = {
        name: value
        for name, value in source.items()
        if (name in _CHILD_VARIABLES or name.startswith(_CHILD_PREFIXES))
        and not _SECRET_WORDS.search(name)
    }
    kept["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root), source.get("PYTHONPATH"))))
    kept.setdefault("PATH", "/usr/bin:/bin")
    return kept


def _dims(
    folder: Path, uploads: Path, names: Sequence[str], page_map: Sequence[dict[str, Any]]
) -> None:
    """`dims.json`: the numbered dimension labels (`dim_labels.build_dims`), written as it grows.

    Read in a process of its own, at low priority: reading every page's text is minutes of pure
    Python for a 60-page set, and in this process it would hold the interpreter's lock that the
    chat's own threads need (a fake full check ran a third slower with it here)."""
    if os.environ.get("GV_DIM_LABELS_IN_PROCESS") == "1":
        write_dims(folder, uploads, names, page_map)
        return
    root = Path(__file__).resolve().parents[3]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "app.review.assistant.dim_labels",
            str(folder),
            str(uploads),
            *names,
        ],
        cwd=root,
        env=child_environment(root),
        check=True,
        timeout=DIMS_TIMEOUT_S,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if not (folder / "dims.json").is_file():
        raise RuntimeError("the labels' process wrote no dims.json")


def write_dims(
    folder: Path,
    uploads: Path,
    names: Sequence[str],
    page_map: Sequence[Mapping[str, Any]],
    *,
    parent: int | None = None,
) -> None:
    """`dim_labels.build_dims` into `folder / dims.json`, written after each page. `parent`: the
    server's process id; the labels' own process stops when the server is gone."""
    labels = import_module("app.review.assistant.dim_labels")

    scales = import_module("app.review.assistant.view_scales")
    notes = import_module("app.review.assistant.drawing_notes")
    pairs = import_module("app.review.assistant.dim_pairs")
    anchors = import_module("app.review.assistant.dim_anchors")
    pages = {"pages": list(page_map)}
    line_work: dict[tuple[str, int], Any] = {}
    anchored_memo: dict[Any, Any] = {}

    def segments_of(name: str, page: int) -> Any:
        if (name, page) not in line_work:
            line_work[(name, page)] = anchors.page_segments(uploads / name, page - 1)
        return line_work[(name, page)]

    def written(document: dict[str, Any]) -> None:
        if parent is not None and os.getppid() != parent:
            raise SystemExit(0)
        _write_json(folder / "dims.json", document)
        # Each drawing's scale, measured from its printed dimensions (a few milliseconds).
        measured = scales.set_scales(document)
        _write_json(folder / scales.SCALES_FILE, measured)
        # Architect against vendor dimensions paired by what both ends land on (walls, the floor,
        # the same outlet), where no single scale holds; never allowed to stop the pack.
        try:
            anchored = anchors.set_anchor_pairs(document, pages, segments_of, cache=anchored_memo)
        except Exception:  # noqa: BLE001 - a fact the pack can do without
            _log.exception("anchor pairing failed; pairs.json keeps the positional pairs only")
            anchored = {}
        # Architect against vendor dimensions paired by drawn position, compared exactly.
        _write_json(folder / pairs.PAIRS_FILE, pairs.set_pairs(document, measured, anchored))
        # Each drawing's printed notes and tags (from the phrases dims.json keeps).
        _write_json(folder / notes.NOTES_FILE, notes.set_notes(document, pages))

    labels.build_dims(
        {name: uploads / name for name in names},
        pages,
        drawn_budget_s=_drawn_budget(),
        on_page=written,
    )


def ensure_pack(
    folder: Path,
    uploads: Path,
    listing: Sequence[Mapping[str, Any]],
    *,
    background: bool = True,
) -> threading.Thread | None:
    """Start building the pack unless it is ready or being built in this process. Returns the
    building thread (`None` when there is nothing to do, or `background` is false)."""
    status = read_status(folder)
    if status.get("state") == "ready" and status.get("version") == PACK_VERSION:
        return None
    with _running_lock:
        running = _running.get(folder)
        if running is not None and running.is_alive():
            return running
        if not listing:
            return None
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
        _set_status(folder, "building", [], files=[str(item["name"]) for item in listing])

        def run() -> None:
            try:
                build_pack(folder, uploads, listing)
            except Exception as failed:  # noqa: BLE001 - the chat works without the pack
                _log.warning("assistant set pack failed: %s", type(failed).__name__)
            finally:
                with _running_lock:
                    _running.pop(folder, None)

        if not background:
            _running.pop(folder, None)
        else:
            thread = threading.Thread(target=run, name="gv-set-pack", daemon=True)
            _running[folder] = thread
            thread.start()
            return thread
    run()
    return None


# ---- zoom crops ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Crop:
    png: bytes
    width: int
    height: int
    dpi: int
    box: Box
    """The box as drawn: clamped to the page."""


def crop_dpi(box: Box, wanted: int | None) -> int:
    """The sharpest dpi at which the box fits the model's picture limit (`fitting_dpi`: up to 300
    for a small box), or the dpi asked when that is lower."""
    width, height = box[2] - box[0], box[3] - box[1]
    fits = fitting_dpi(Decimal(str(width)), Decimal(str(height)))
    if wanted is not None:
        fits = min(fits, max(MIN_CROP_DPI, min(MAX_CROP_DPI, wanted)))
    return fits


def _clamp(box: Box, width: float, height: float, page_number: int) -> Box:
    x0, y0, x1, y1 = box
    left, top = max(0.0, min(x0, x1)), max(0.0, min(y0, y1))
    right, bottom = min(width, max(x0, x1)), min(height, max(y0, y1))
    if right - left < 2 or bottom - top < 2:
        raise ValueError(
            f"the box is not on page {page_number} (the page is "
            f"{round(width)} x {round(height)} points, origin top-left)"
        )
    return left, top, right, bottom


def _png_crop(picture: Image.Image, clamped: Box, scale: float) -> Crop:
    if max(picture.size) > MAX_CROP_EDGE_PX:
        picture.thumbnail((MAX_CROP_EDGE_PX, MAX_CROP_EDGE_PX))
    buffer = io.BytesIO()
    # Plain zlib, not `optimize`: the model reads pixels, and an optimised PNG of a 2576 px crop
    # took longer to write than to cut (#1548).
    picture.save(buffer, format="PNG", compress_level=_CROP_PNG_LEVEL)
    return Crop(
        png=buffer.getvalue(),
        width=picture.width,
        height=picture.height,
        dpi=round(scale * 72),
        box=(
            round(clamped[0], 1),
            round(clamped[1], 1),
            round(clamped[2], 1),
            round(clamped[3], 1),
        ),
    )


def crop_picture(
    pdf: Path, page_number: int, box: Box, dpi: int | None = None, *, cache: Path | None = None
) -> Crop:
    """The area `box` (points, origin top-left, as shown) of a page as a PNG. Raises `ValueError`
    with a plain reason when the page or the box is not on the page.

    With `cache` (the pack's `hires/` folder, #1548) a crop of 300 dpi or less is cut from the
    page's 300-dpi picture, drawn once on first need (`page_cache`); only a sharper one renders."""
    if cache is not None:
        try:
            sizes = page_cache.page_sizes(cache, pdf)
        except OSError:
            sizes = []
        if sizes:
            if not 1 <= page_number <= len(sizes):
                raise ValueError(f"there is no page {page_number} (the file has {len(sizes)})")
            size = sizes[page_number - 1]
            clamped = _clamp(box, size.width, size.height, page_number)
            left, top, right, bottom = clamped
            chosen = crop_dpi(
                (round(left, 1), round(top, 1), round(right, 1), round(bottom, 1)), dpi
            )
            scale = min(chosen / 72, MAX_CROP_EDGE_PX / max(right - left, bottom - top))
            picture = page_cache.cut(cache, pdf, page_number, clamped, scale)
            if picture is not None:
                return _png_crop(picture, clamped, scale)
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            if not 1 <= page_number <= len(document):
                raise ValueError(f"there is no page {page_number} (the file has {len(document)})")
            page = document[page_number - 1]
            width, height = page.get_size()
            clamped = _clamp(box, width, height, page_number)
            left, top, right, bottom = clamped
            chosen = crop_dpi(
                (round(left, 1), round(top, 1), round(right, 1), round(bottom, 1)), dpi
            )
            scale = min(chosen / 72, MAX_CROP_EDGE_PX / max(right - left, bottom - top))
            bitmap = page.render(
                scale=scale,
                crop=(left, height - bottom, width - right, top),
                may_draw_forms=True,
            )
            picture = bitmap.to_pil().convert("RGB")
        finally:
            document.close()
    return _png_crop(picture, clamped, scale)
