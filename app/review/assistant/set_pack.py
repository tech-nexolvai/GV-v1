"""The set pack: what code prepares once per drawing set, so the chat agent starts reading at once (#1497).

A whole-set answer used to spend its first turns on set-up (a page map built by hand, a
drawings-only copy, every page rendered) and its helpers on one `pdftoppm` crop per call. Code does
that set-up once per upload, in the background, and the agent sees it read-only at
`/mnt/user-data/set/`:

- `<name>_drawings_only.pdf`: a copy of each PDF that keeps only the pasted drawings (`/Stamp`
  annotations, and drawings pasted as pictures, #1825), so GV's markup is hidden (SKILL.md section 5). A page with no stamps is its own
  page content, without annotations. GV's stickers baked inside a pasted drawing (a coloured
  note over the vendor's ink, #1859) are hidden too, and listed in `<name>_stickers.json`
  (`gv_stickers`). AutoCAD's SHX text comments are kept as invisible text at
  their rectangles (#1772), as `verify_value` reads them.
- `pages/<name>/p-001.png`: every page at 100 dpi, from the drawings-only copy.
- `contact/<name>-01.png`: contact sheets, nine pages to a picture, each page number stamped.
- `annotations.json`: every annotation (file, page, kind, author, text, box), GV's headings and
  notes included: the drawings-only copy drops them.
- `page_map.json`: per page and per drawing on the sheet: whose drawing it is (architect / vendor
  / brand_prototype / general) from the heading GV typed above or below it
  (`vocabulary/panel_headings.py`), its round ("first / second set of comments"), the area or title
  GV typed ("2. Buffet"; carried to the next pages until another), and sheet numbers printed on
  the page. It is a hint for the agent, read by code from GV's labels: the drawings decide.
  **Separate files** (#1719): a file uploaded as the architect's drawings (not the vendor's file
  itself) gives its pages the architect role: a pasted drawing with no GV heading is the
  architect's, and a page with no pasted drawing (a normal CAD sheet) is one whole-page architect
  drawing. A vendor file's pages are read the same way (whole-page vendor drawings). Decided page
  by page (#1772): a stamp on one page (an approval stamp) never switches off the file's other
  pages, and never decides its own page. A page is a drawing when its own content prints
  dimensions, or, failing that, when its labels from every route (printed text, AutoCAD's SHX
  text, the vendor's pen strokes) do (`settle_candidates`). A page that prints no dimension (a
  cover, a notes sheet) stays general. Each such drawing says `from_upload`, and `upload_roles`
  names the files read so. GV's combined sheets (drawings GV pasted and headed) are read exactly
  as before. **A sheet with no GV heading** (#1825): the roles of its pasted drawings are decided
  from the content of both drawings, only when clear on both sides (`content_sides`, the rule of
  #1052), `role_by: "content"`; anything less stays general. Drawings pasted as pictures (a
  rectangle comment holding an image) are kept in the drawings-only copy and listed as the
  page's `pictures`: the agents see them, and code reads their numbers with two OCR engines
  (`picture_labels`, #1830; a label only when both read the same value). **A picture GV headed**
  (#1835, decided by Anant 2026-10-21) is also a drawing of the page (`picture: true`, after the
  pasted drawings), its role from the heading over it by the same rule (above, or below as a
  caption), so the labels read in it take that side; a picture with no heading stays nobody's
  (its labels' side `unknown`), never by a guess from its content. On a whole-page drawing
  (#1798) no picture is a drawing.
- `dims.json` (#1558): every printed dimension on every page, numbered per page (`D17`), with its
  value, inches, box, whose drawing it is on, and its dimension and extension lines when found
  (`dim_labels.py`). The `zoom` tool draws these ids on a crop; findings name them. Written as
  it grows, page by page, after the pictures; a page not in it yet is read when it is asked for.
  Every page's text labels come first; then the numbers in pasted pictures (two OCR engines must
  agree, #1830); then the vendor's drawn numbers (pen strokes, read by two OCR engines) are added
  page by page within `DIMS_DRAWN_BUDGET_S`, after the text ids.
- `scales.json`: each drawing's scale measured from its own printed dimensions
  (`view_scales.py`; printed scale notes are wrong where GV resized a pasted drawing), written
  with `dims.json`. `measure_on_drawing` uses it when no calibration is given.
- `notes.json`: each drawing's printed tags and notes (`drawing_notes.py`, from the phrases
  `dims.json` keeps), and the architect's tags the vendor's drawing does not print.
- `pairs.json`: architect against vendor dimensions paired by drawn position (`dim_pairs.py`,
  wrapping `extraction/architect/pairing.py`) or by anchors, what both ends land on (`dim_anchors.py`:
  the same wall, floor or outlet on both drawings, no scale used), each `same` or `different`.
- `reading_coverage.json` (#1862): how many printed numbers the labels read, per set and page,
  and every unread one with its box and why (`reading_coverage.py`, `reading_ink.py`). Counts
  only; never shown to the agent (not in the README) and never a verdict's input.
- `status.json`: `building`, `ready`, `ready_without_labels` (the labels step broke or ran out of
  time) or `failed`, and the steps done. The agent can work before
  the pack is ready (the first message never waits); the `zoom` tool makes the drawings-only copy
  it needs at once if the pack has not made it yet.

**Made once per upload.** The pack's folder is named by a hash of the uploads' names and SHA-256
(`set-packs/<key>` under the chat root), so every chat on the same PDFs reuses it. Each file is
written to a temporary name and renamed, so a reader never sees half a file.

**Boxes** in `page_map.json` and `annotations.json` are in PDF points of the page as shown (its
rotation applied), origin top-left: the units of `pdftotext -bbox`, `report_finding` and `zoom`.

**Loaded by name, never imported**: this module draws with pypdfium2 and Pillow, which `app/api/`
must not reach by import (`tests/api/test_no_heavy_work.py`); `chat_service` and the `zoom` tool
load it with `import_module`. pdfium is not thread-safe: every call holds its one lock
(`extraction/pdfium_guard.py`), and a whole step holds `chat_drawing.PDFIUM_LOCK` (the same lock).

**Built in a process of its own** (`ensure_pack` -> `_build_in_child`, #1869): the set's pages are
read and drawn there, so a page that crashes the PDF engine fails this pack (`status.json`:
`renderer_crashed` and a `reason` naming the page, from `progress.json`) and never the server.
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
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter
from pypdf.generic import ArrayObject, NameObject

from app.review.assistant import page_cache
from app.review.assistant.chat_drawing import PDFIUM_LOCK
from app.review.assistant.page_pictures import fitting_dpi
from extraction.pdfium_guard import pdfium
from vocabulary.panel_headings import HeadingMeaning, heading_meaning, page_title_role

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
    "upload_roles",
    "upload_rounds",
]

_log = logging.getLogger(__name__)

#: Changes when what the pack holds changes: an older pack is built again.
PACK_VERSION: Final = 16
#: `status.json`'s state when the labels step broke or ran out of time (#1768): the page map and
#: pictures are made, `dims.json` is missing or partial. Never "ready".
READY_WITHOUT_LABELS: Final = "ready_without_labels"
RENDER_DPI: Final = 100
#: Seconds the dimension labels' drawn-ink route (OCR) may spend on ONE page (#1872: a limit per
#: page, read in worker processes; `GV_DIM_LABELS_DRAWN_BUDGET_S`; 0 turns it off). It reads the
#: vendor's drawn numbers label by label (#1571); a page it does not finish is `partial`, and its
#: drawn values keep `verify_value`.
DIMS_DRAWN_BUDGET_S: Final = 300.0
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
#: A printed dimension in a page's text, read whole (`24"`, `3'-0"`, `3'-10 1/2"`, `35 3/4"`): a
#: page of a separate file shows a drawing when it prints at least `_DRAWING_DIMS` of them (#1719);
#: a title block or a notes sheet prints one or two at most.
_DIMENSION: Final = re.compile(
    r"\d+\s*['\u2019]\s*-?\s*\d+(?:\s+\d+/\d+)?\s*(?:\"|\u201d)?"
    r"|\d+(?:\s+\d+/\d+)?\s*(?:\"|''|\u201d)"
)
_DRAWING_DIMS: Final = 3

#: Builds running in this process, by pack folder (a second chat on the same set waits for none).
_running: dict[Path, threading.Thread] = {}
_running_lock: Final = threading.Lock()
#: One lock per drawings-only copy, so the build and a `zoom` never write it at once.
_copy_locks: dict[Path, threading.Lock] = {}

type Box = tuple[float, float, float, float]


# ---- where the pack lives ------------------------------------------------------------------------


def pack_key(listing: Sequence[Mapping[str, Any]]) -> str:
    """The pack's name: a hash of the uploads' names and SHA-256, in order. A separate architect
    file adds its role (#1719): its pages are read as the architect's, so a pack made before that
    is made again; a set with no separate architect file keeps its name. A file the front door
    changed (`working_sha256`, #1724) adds its working copy's, so its pack is made from that copy;
    a file it left as it was keeps the name its pack always had."""
    roles = upload_roles(listing)
    text = "\n".join(
        f"{item.get('name')}:{item.get('sha256')}"
        + (":architect" if roles.get(str(item.get("name"))) == "architect" else "")
        + (f":{item['working_sha256']}" if item.get("working_sha256") else "")
        for item in listing
    )
    return hashlib.sha256(f"v{PACK_VERSION}\n{text}".encode()).hexdigest()[:32]


def _is_architect_kind(item: Mapping[str, Any]) -> bool:
    return str(item.get("kind") or "").strip().lower().startswith("the architect")


def _is_vendor_kind(item: Mapping[str, Any]) -> bool:
    kind = str(item.get("kind") or "").strip().lower()
    return kind.startswith("the vendor") or "shop drawing" in kind


#: An earlier round of the vendor's shop drawings uploaded beside its re-issued ones (#1728:
#: `app/api/assistant_chats.PREVIOUS_ROUND_WORDS`): its round.
_PREVIOUS_ROUND: Final = re.compile(
    r"^the vendor's previous round\b.*\bround (\d+)\b", re.IGNORECASE
)


def upload_rounds(listing: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    """The round each upload's kind gives its vendor drawings (#1728): an earlier round uploaded
    beside the vendor's re-issued file says its own number, and then the re-issued file is the next
    round. A set with no earlier file gives none (its drawings' rounds come from GV's captions)."""
    earlier: dict[str, int] = {}
    for item in listing:
        found = _PREVIOUS_ROUND.match(str(item.get("kind") or "").strip())
        if found is not None:
            earlier[str(item.get("name"))] = int(found.group(1))
    if not earlier:
        return {}
    latest = max(earlier.values()) + 1
    rounds = dict(earlier)
    for item in listing:
        name = str(item.get("name"))
        if name not in earlier and _is_vendor_kind(item):
            rounds[name] = latest
    return rounds


def upload_roles(listing: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    """The role each upload's kind gives its drawings (#1719): `architect` for a file uploaded as
    the architect's drawings that is not also uploaded as the vendor's (the same combined file in
    both places is read once, by its GV headings), `vendor` for a file uploaded as the vendor's.
    `_page_entries` uses a vendor file's role only when the file has no pasted drawing at all."""
    others = {str(item.get("sha256")) for item in listing if not _is_architect_kind(item)}
    roles: dict[str, str] = {}
    for item in listing:
        name = str(item.get("name"))
        if _is_architect_kind(item):
            if str(item.get("sha256")) not in others:
                roles[name] = "architect"
        elif _is_vendor_kind(item):
            roles[name] = "vendor"
    return roles


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
            pictures = picture_annotations(source)
            # GV's stickers baked into a pasted drawing are GV's markup too (#1859): hidden, and
            # listed for the readers (`gv_stickers`), so GV's values are never the vendor's.
            stickers = import_module("app.review.assistant.gv_stickers")
            copy, found = stickers.hidden_copy(_with_shx_text(source))
            try:
                stickers.write_file(folder / f"{stem(name)}{stickers.STICKERS_SUFFIX}", name, found)
            except OSError as failed:  # the copy still hides them; the readers search the upload
                _log.warning("assistant set pack stickers not listed: %s", type(failed).__name__)
            writer = PdfWriter(clone_from=copy)
            for number, page in enumerate(writer.pages, start=1):
                if "/Annots" in page:
                    annotations: Any = page["/Annots"]
                    keep = [
                        a
                        for index, a in enumerate(annotations)
                        if a.get_object().get("/Subtype") == "/Stamp" or (number, index) in pictures
                    ]
                    page[NameObject("/Annots")] = ArrayObject(keep)
                _hide_gv_marks(page)
            buffer = io.BytesIO()
            writer.write(buffer)
            _write_bytes(out, buffer.getvalue())
    return out


def picture_annotations(source: Path) -> frozenset[tuple[int, int]]:
    """`(page, index in /Annots)` of each pasted picture in `source` (#1825): a rectangle comment
    holding an image and no text (`extraction.stamp_text.pasted_picture`), as a viewer's "paste
    image" leaves a raster snapshot of a drawing. The drawings-only copy keeps them, so the
    agents see a drawing pasted that way; `picture_labels` reads its numbers (#1830)."""
    stamp_text = import_module("extraction.stamp_text")
    pikepdf = import_module("pikepdf")
    found: set[tuple[int, int]] = set()
    try:
        with pikepdf.open(str(source)) as pdf:
            for number, page in enumerate(pdf.pages, start=1):
                for index, annotation in enumerate(page.obj.get("/Annots") or []):
                    if stamp_text.pasted_picture(annotation):
                        found.add((number, index))
    except Exception as failed:  # noqa: BLE001 - the copy is made without them, as before
        _log.warning("assistant set pack pictures not kept: %s", type(failed).__name__)
        return frozenset()
    return frozenset(found)


def _with_shx_text(source: Path) -> Path | io.BytesIO:
    """The upload, with AutoCAD's SHX text comments (`PDFSHX=1`) set as invisible text at their
    rectangles (#1772), as `verify_value` reads them (`visible_text.shx_comment_text`): they are
    the vendor's text, so the drawings-only copy keeps them when it drops every other comment,
    and the page's text (its drawing gate, `page_texts.json`) sees them. A file without one is
    the upload itself."""
    visible = import_module("extraction.visible_text")
    pikepdf = import_module("pikepdf")
    try:
        with pikepdf.open(str(source)) as pdf:
            added = 0
            for page in pdf.pages:
                comments = [a for a in page.obj.get("/Annots") or [] if visible.is_shx_comment(a)]
                if comments:
                    added += visible.shx_comment_text(pdf, page, comments)
            if not added:
                return source
            buffer = io.BytesIO()
            pdf.save(buffer)
    except Exception as failed:  # noqa: BLE001 - the copy is made without them, as before
        _log.warning("assistant set pack SHX text not kept: %s", type(failed).__name__)
        return source
    buffer.seek(0)
    return buffer


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


def _annotations(
    name: str, reader: PdfReader, pictures: frozenset[tuple[int, int]] = frozenset()
) -> list[dict[str, Any]]:
    """Every annotation of `name`; one of `pictures` (`picture_annotations`) says so."""
    out: list[dict[str, Any]] = []
    for number, page in enumerate(reader.pages, start=1):
        geometry = _geometry(page)
        for index, raw in enumerate(page.get("/Annots") or []):
            try:
                annotation = raw.get_object()
                rect = annotation.get("/Rect")
                box = geometry.box(rect) if rect is not None and len(rect) == 4 else None
            except (AttributeError, TypeError, ValueError):
                continue
            kind = str(annotation.get("/Subtype") or "").lstrip("/")
            text = str(annotation.get("/Contents") or "").replace("\r", "\n").strip()
            item: dict[str, Any] = {
                "file": name,
                "page": number,
                "kind": kind,
                "author": str(annotation.get("/T") or "") or None,
                "text": text[:500] or None,
                "font_pt": _font_size(annotation),
                "box": box,
            }
            if (number, index) in pictures:
                item["picture"] = True
            out.append(item)
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


def _picture_headings(
    stamps: Sequence[dict[str, Any]],
    pictures: Sequence[Sequence[float]],
    labels: Sequence[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The pasted pictures GV headed, each with its heading (#1835, decided by Anant 2026-10-21):
    the heading the drawings' rule gives it (`_headings` over every drawing of the page, so the
    page's own side wins, above or below as a caption), kept only when it lies over the picture
    itself: across its width and within `_CAPTION_PT` of its edge, as a round's caption must. A
    heading over another drawing only is never a picture's (a picture pasted beside a headed
    drawing, or deep inside one, is nobody's): a picture's side is never guessed."""
    shown: list[dict[str, Any]] = [
        {"box": (float(box[0]), float(box[1]), float(box[2]), float(box[3]))}
        for box in sorted(pictures, key=lambda b: (b[1], b[0]))
    ]
    if not shown:
        return []
    found = _headings([*stamps, *shown], labels)[len(stamps) :]
    out: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for picture, heading in zip(shown, found, strict=True):
        if heading is None:
            continue
        vertical, horizontal = _gap(picture["box"], heading["box"])
        if vertical <= _CAPTION_PT and not horizontal:
            out.append((picture, heading))
    return out


def _round_of(text: str) -> int | None:
    found = _ROUNDS.search(text)
    return _ROUND_NUMBER[found.group(1).lower()] if found else None


def _shown_size(page: Any) -> tuple[float, float]:
    """A page's width and height as shown (its rotation applied), in points."""
    geometry = _geometry(page)
    width, height = geometry.urx - geometry.llx, geometry.ury - geometry.lly
    return (height, width) if geometry.rotation in (90, 270) else (width, height)


def _whole_page_role(upload_role: str, text: str) -> str:
    """Whose drawing a whole page of a separate file is (#1719): the upload's kind first (every
    page of the architect's file is the architect's); on the vendor's file, a page whose printed
    title words name the architect alone ("ARCH LAYOUT") is the architect's, any other page the
    vendor's."""
    if upload_role == "architect":
        return "architect"
    return "architect" if page_title_role(text) == "architect" else "vendor"


def _page_entries(
    name: str,
    annotations: Sequence[dict[str, Any]],
    texts: Sequence[str],
    page_count: int,
    *,
    upload_role: str | None = None,
    sizes: Sequence[tuple[float, float]] = (),
    upload_round: int | None = None,
    own_texts: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    """Each page's entry of `page_map.json`. `upload_role` (#1719): the role the upload's kind
    gives (`upload_roles`). In the architect's file a pasted drawing with no GV heading is the
    architect's. A page of such a file is one whole-page drawing (`sizes`: each page's shown
    size), its role from `_whole_page_role`, when it is a drawing by itself: decided page by page
    (#1772), never for the whole file, and stamps never decide it (an approval stamp on one page
    of a CAD file is not a pasted drawing). A page with no pasted drawing GV headed is a whole-page
    drawing when its own content (`own_texts`: its text without its pasted drawings) prints at
    least `_DRAWING_DIMS` dimensions; any other such page is a `candidate` whole-page drawing
    while the labels are read (so the drawn route reads its pen strokes, and AutoCAD's SHX text
    counts), decided from its labels by `settle_candidates`. `upload_round` (#1728): the round the
    upload's kind gives such a whole-page vendor drawing (`upload_rounds`)."""
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
        text = texts[number - 1] if number - 1 < len(texts) else ""
        whole: str | None = None
        candidate: dict[str, Any] | None = None
        headed = bool(stamps) and any(h for h in _headings(stamps, role_labels))
        if upload_role is not None and number - 1 < len(sizes):
            own = _own_text(own_texts, number, text, stamps)
            width, height = sizes[number - 1]
            page_box = (0.0, 0.0, round(width, 1), round(height, 1))
            if len(_DIMENSION.findall(own)) >= _DRAWING_DIMS:
                # A page that prints dimensions by itself: one whole-page drawing, whatever
                # stamps sit on it, GV's heading over them too (#1798: GV pastes reference
                # snapshots on the vendor's own CAD pages and heads the page "VENDOR SHOP
                # DRAWING"; the snapshots are GV's, the page is the vendor's drawing).
                stamps = [{"box": page_box}]
                whole = _whole_page_role(upload_role, text)
            elif not headed:
                candidate = _candidate(
                    page_box, _whole_page_role(upload_role, text), upload_role, upload_round
                )
        ordered = sorted(stamps, key=lambda a: (a["box"][1], a["box"][0]))
        one_round = {label["round"] for label in round_labels}
        pictures = [list(a["box"]) for a in items if a.get("picture") and a["box"]]
        # Pasted pictures GV headed (#1835): drawings of the page map, after the pasted ones
        # (whose headings and indices never change), marked `picture`. On a whole-page drawing
        # (#1798) no picture is a drawing.
        headed_pictures = (
            [] if whole is not None else _picture_headings(ordered, pictures, role_labels)
        )
        drawn = [
            *zip(ordered, _headings(ordered, role_labels), [False] * len(ordered), strict=True),
            *((picture, heading, True) for picture, heading in headed_pictures),
        ]
        for stamp, heading, is_picture in drawn:
            role = heading["role"] if heading else "general"
            from_upload = False
            if is_picture:
                pass  # a picture's side is its heading's, never its upload's
            elif whole is not None:
                role, from_upload, heading = whole, True, None
            elif heading is None and upload_role == "architect":
                # The architect's own file: a drawing GV gave no heading is the architect's.
                role, from_upload = "architect", True
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
            if found is None and from_upload and role == "vendor" and upload_round is not None:
                found = upload_round
            half: dict[str, Any] = {
                "box": stamp["box"],
                "role": role,
                "heading": " ".join(str(heading["text"]).split()) if heading else None,
                "round": found,
            }
            if from_upload:
                half["from_upload"] = True
            if is_picture:
                half["box"] = list(stamp["box"])
                half[PICTURE] = True
            halves.append(half)
        roles = sorted({half["role"] for half in halves} - {"general"})
        rounds = sorted({half["round"] for half in halves if half["round"]})
        if not halves and round_labels:
            rounds = sorted({label["round"] for label in round_labels})
        sheets: list[str] = []
        for printed in _SHEET.finditer(text):
            sheet = printed.group(1) or printed.group(2)
            if sheet and sheet not in sheets:
                sheets.append(sheet)
        entry: dict[str, Any] = {
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
        if pictures:
            # Pasted pictures (#1825): a drawing the agents see; `dims.json` holds the numbers
            # two OCR engines agree on in it (#1830). Every one is listed here, headed or not;
            # a headed one is also a drawing (`halves`, `picture: true`, #1835).
            entry["pictures"] = pictures
        if candidate is not None:
            entry[CANDIDATE] = candidate
        entries.append(entry)
    return entries


def _own_text(
    own_texts: Sequence[str] | None, number: int, text: str, stamps: Sequence[Any]
) -> str:
    """Page `number`'s own text (without its pasted drawings): from `own_texts` when given, else
    the whole text of a page with nothing pasted on it."""
    if own_texts is not None:
        return own_texts[number - 1] if number - 1 < len(own_texts) else ""
    return "" if stamps else text


def _candidate(box: Box, role: str, upload_role: str, upload_round: int | None) -> dict[str, Any]:
    """A page's candidate whole-page drawing (#1772): its box and role, as a drawing would hold
    them, while the labels are read, and the role its upload's kind gives (`upload`)."""
    return {
        "box": box,
        "role": role,
        "heading": None,
        "round": upload_round if role == "vendor" else None,
        "from_upload": True,
        "upload": upload_role,
    }


#: The page map's key for a page's candidate whole-page drawing, until its labels decide it.
CANDIDATE: Final = "candidate"
#: A page-map drawing's key when it is a pasted picture GV headed (#1835).
PICTURE: Final = "picture"


def _inside(box: Sequence[float], halves: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a label's centre lies inside one of a page's drawings."""
    x, y = (float(box[0]) + float(box[2])) / 2, (float(box[1]) + float(box[3])) / 2
    for half in halves:
        hb = half.get("box")
        if isinstance(hb, Sequence) and len(hb) == 4:
            x0, y0, x1, y1 = (float(v) for v in hb)
            if x0 <= x <= x1 and y0 <= y <= y1:
                return True
    return False


def settle_candidates(
    page_map: dict[str, Any], dims: Mapping[str, Any] | None
) -> tuple[list[tuple[str, int]], list[tuple[str, int]]]:
    """Decide each candidate whole-page drawing from its page's labels (#1772), in place: a page
    whose labels outside its pasted drawings (every route: printed text, AutoCAD's SHX text, the
    vendor's pen strokes, and its millimetre-only values; never a derived `EQ` part) number at
    least `_DRAWING_DIMS` is one
    whole-page drawing (its stamps are not drawings); any other stays as it was. Returns the
    pages that had a candidate (their labels' sides are set again from their drawings) and those
    that became drawings."""
    by_page: dict[tuple[str, int], list[Mapping[str, Any]]] = {}
    for entry in (dims or {}).get("pages") or []:
        if isinstance(entry, Mapping):
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            # Its labels, and the millimetre-only values it prints (`metric`: never labels, but
            # dimensions all the same).
            by_page[key] = [
                x
                for x in [*(entry.get("labels") or []), *(entry.get("metric") or [])]
                if isinstance(x, Mapping)
            ]
    found: list[tuple[str, int]] = []
    promoted: list[tuple[str, int]] = []
    used = page_map.setdefault("upload_roles", {})
    for entry in page_map.get("pages") or []:
        candidate = entry.pop(CANDIDATE, None)
        if not isinstance(candidate, Mapping):
            continue
        key = (str(entry.get("file")), int(entry.get("page") or 0))
        found.append(key)
        # Its pasted drawings, never a headed picture (#1835): which labels lie outside them is
        # counted as before, and a page that becomes one drawing keeps no picture drawing (as on
        # a whole-page drawing, #1798).
        real = [
            h for h in entry.get("halves") or [] if isinstance(h, Mapping) and not h.get(PICTURE)
        ]
        outside = [
            label
            for label in by_page.get(key, [])
            if not label.get("derived") and not _inside(label.get("box") or (), real)
        ]
        if len(outside) < _DRAWING_DIMS:
            continue
        half = {k: v for k, v in candidate.items() if k != "upload"}
        half["box"] = list(candidate["box"])
        entry["halves"] = [half]
        entry["role"] = half["role"]
        entry["rounds"] = [half["round"]] if half.get("round") else []
        used.setdefault(key[0], str(candidate.get("upload") or half["role"]))
        promoted.append(key)
    return found, promoted


# ---- pictures ------------------------------------------------------------------------------------


def _page_texts(pdf: Path) -> list[str]:
    """Each page's text, the pasted drawings' own text included (flattened in memory)."""
    return _texts(pdf)[0]


#: The pack's last page started, by step and file (`_note`): what a crash of the pack's own process
#: names (`_build_in_child`).
PROGRESS_FILE: Final = "progress.json"

type Progress = Callable[[int], None]


def _note(folder: Path, step: str, name: str) -> Progress:
    """Each page's number written to the pack's `PROGRESS_FILE` before `step` reads it."""

    def page(number: int) -> None:
        try:
            _write_json(folder / PROGRESS_FILE, {"step": step, "file": name, "page": number})
        except OSError:  # only a crash's reason is less exact
            pass

    return page


def _texts(pdf: Path, progress: Progress | None = None) -> tuple[list[str], list[str]]:
    """Each page's text, the pasted drawings' own text included (flattened in memory), and each
    page's own text: its content alone, without its pasted drawings (#1772: whether a page of a
    separate upload is a drawing by itself, whatever is pasted on it)."""
    texts: list[str] = []
    own: list[str] = []
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            for index in range(len(document)):
                if progress is not None:
                    progress(index + 1)
                page = document[index]
                try:
                    textpage = page.get_textpage()
                    own.append(textpage.get_text_range())
                    textpage.close()
                except Exception:  # noqa: BLE001 - a page with no readable text has none
                    own.append("")
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
    return texts, own


def _render_pages(pdf: Path, out: Path, dpi: int, progress: Progress | None = None) -> list[Path]:
    paths: list[Path] = []
    with PDFIUM_LOCK:
        count = len(pdfium.PdfDocument(str(pdf)))
    for index in range(count):
        if progress is not None:
            progress(index + 1)
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
                       A separate plain file (no pasted drawings): each page printing dimensions
                       is one whole-page drawing, its role from the upload (the architect's file)
                       or its title words; from_upload: true marks them.
                       A sheet with no GV heading: the roles code decided from both drawings'
                       own content (feet-and-inches labels and scale against the vendor's
                       inch/millimetre labels and ratio scale); role_by: "content" marks them.
                       Compare its architect drawing with its vendor drawing on the same sheet.
                       sides_unknown (e.g. "code timed out"): code could not decide whose
                       drawing is whose on that page; look at both drawings before you
                       compare, and say which one you took for the architect's.
                       pictures: boxes of drawings pasted as pictures (code lists only the
                       numbers two OCR engines agree on in them; look at them). A picture GV
                       headed is also a drawing (picture: true) with its heading's role; one
                       with no heading is nobody's (its numbers have no side).
annotations.json      every annotation: file, page, kind, author, text, box (GV's markup and
                       headings included; the drawings-only copies drop them).
*_drawings_only.pdf    each PDF with only the pasted drawings kept: GV's markup hidden, and
                       GV's stickers baked inside a pasted drawing (coloured notes) too.
*_stickers.json        GV's stickers code hid: page, box, text. A value inside one is GV's,
                       never the vendor's.
pages/<name>/p-NNN.png every page at 100 dpi (drawings only). Zoom to read numbers.
                       <name> is the upload's name without ".pdf" (A.pdf: pages/A/p-003.png);
                       NNN is the page with three digits.
contact/*.png          nine pages per picture, page numbers stamped: the whole set at a glance.
health.json            per page: what kind of page it is and what was or was not read (typed or
                       drawn lettering, hidden text not used, AutoCAD SHX text used, layers off,
                       scanned, straightened, markups not read), with a plain sentence each.
dims.json              every printed dimension code found, numbered per page (D17): value,
                       inches, box, side, dimension line. zoom marks: true draws the ids.
                       route "drawn": numbers drawn as pen strokes (the vendor's, and an
                       architect's hand lettering such as 2'-6 1/2") read by two OCR engines
                       (added later; check one with zoom before you rely on it).
                       "origin": "picture": read inside a pasted picture by two OCR engines
                       (same trust as "drawn"; no dimension line); its side is the role of
                       GV's heading over the picture, else unknown. A number read there that
                       the engines did not agree on is never a label: the page's "pictures"
                       lists it as unconfirmed.
                       "derived": an EQ part: the printed overall (less the chain's
                       printed parts) shared equally, by code; "from" names the overall
                       (36" wall to wall marked EQ / EQ is 18" each side).
                       "origin": "ocr": a number no label reader read, read at its spot
                       by two OCR engines that agree (numbered last; same trust as
                       "picture": no dimension line). What one engine read alone is
                       never a label: the page's "ocr" lists it as unconfirmed.
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
status.json            building / ready / ready_without_labels (the labels step broke or ran
                       out of time: dims.json is missing or partial) / failed.
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
    _write_json(folder / _LISTING_FILE, [dict(item) for item in listing])
    try:
        copies = {}
        for name in names:
            _note(folder, "copying the drawings of", name)(0)
            copies[name] = drawings_only(folder, uploads, name)
        done.append("drawings_only")
        _set_status(folder, "building", done, files=names)
        annotations: list[dict[str, Any]] = []
        page_map: list[dict[str, Any]] = []
        roles = upload_roles(listing)
        rounds = upload_rounds(listing)
        used: dict[str, str] = {}
        for name in names:
            logging.getLogger("pypdf").setLevel(logging.ERROR)
            reader = PdfReader(str(uploads / name))
            found = _annotations(name, reader, picture_annotations(uploads / name))
            annotations += found
            texts, own = _texts(copies[name], _note(folder, "reading the text of", name))
            entries = _page_entries(
                name,
                found,
                texts,
                len(reader.pages),
                upload_role=roles.get(name),
                sizes=[_shown_size(page) for page in reader.pages],
                upload_round=rounds.get(name),
                own_texts=own,
            )
            if any(h.get("from_upload") for e in entries for h in e["halves"]):
                used[name] = roles[name]
            # A sheet with no GV heading (#1825): its drawings' roles from their own content.
            _content_sides(uploads / name, entries, folder.parent / SIDES_CACHE)
            page_map += entries
        _write_json(folder / "annotations.json", annotations)
        _write_json(
            folder / "page_map.json",
            {"version": PACK_VERSION, "files": names, "upload_roles": used, "pages": page_map},
        )
        done.append("page_map")
        _set_status(folder, "building", done, files=names)
        # The labels (with each page's health check) in their own process, its pages read at once
        # by worker processes (#1872); the page pictures drawn here meanwhile.
        labels_error: str | None = None
        drawn: list[str] = []
        reading = threading.Thread(
            target=_pictures, args=(folder, names, copies, drawn), name="gv-set-pack-pictures"
        )
        reading.start()
        labelled = False
        try:
            _dims(folder, uploads, names, page_map)
            labelled = True
        except Exception as failed:  # noqa: BLE001 - the pack works without labels, said so
            _log.warning("assistant set pack labels failed: %s", type(failed).__name__)
            labels_error = (
                "timed out"
                if isinstance(failed, subprocess.TimeoutExpired)
                else type(failed).__name__
            )
        reading.join()
        done += drawn
        if labelled:
            done.append("dims")
        if (folder / "health.json").is_file():
            done.append("health")
        if "pages" not in done:
            raise RuntimeError("the page pictures could not be drawn")
        # The labels' process decides the candidate drawings; if it stopped first, they are
        # decided here from the labels it wrote (never left undecided in a ready pack).
        _settle_folder(folder)
    except Exception as failed:
        _set_status(folder, "failed", done, files=names, error=type(failed).__name__)
        raise
    if labels_error is not None:
        # Never "ready" with no or partial labels (#1768): the simple check refuses to compare on
        # it, and the next message builds the pack again.
        _set_status(folder, READY_WITHOUT_LABELS, done, files=names, labels_error=labels_error)
        return
    _set_status(folder, "ready", done, files=names)


def _pictures(
    folder: Path, names: Sequence[str], copies: Mapping[str, Path], done: list[str]
) -> None:
    """Each page's picture and the contact sheets (`done` gets `pages`, `contact_sheets`)."""
    try:
        for name in names:
            pages = _render_pages(
                copies[name],
                folder / "pages" / stem(name),
                RENDER_DPI,
                _note(folder, "drawing", name),
            )
            _contact_sheets(name, pages, folder / "contact")
    except Exception as failed:  # noqa: BLE001 - said by the pack's step list
        _log.warning("assistant set pack pictures failed: %s", type(failed).__name__)
        return
    done += ["pages", "contact_sheets"]


def _content_sides(pdf: Path, entries: list[dict[str, Any]], cache: Path) -> None:
    """The roles of the drawings on `pdf`'s pages that print no GV heading, decided by the content
    of both drawings (`content_sides`, #1825), set on `entries` in place. Read in a process of its
    own, as the labels are (`GV_DIM_LABELS_IN_PROCESS=1`: here), with a time limit that grows with
    the pages to read. A page read before (`cache`: the answers kept beside the packs, by the
    PDF's bytes and the reader's code) is never read again (#1866). A page the process did not
    reach in time, or could not read, keeps every drawing as it was and says its sides are unknown
    (`content_sides.SIDES_UNKNOWN`), said in the log too."""
    sides = import_module("app.review.assistant.content_sides")
    pages = sides.unheaded_pages(entries)
    if not pages:
        return
    key = sides.cache_key(pdf)
    kept = sides.read_cache(cache, key)
    read: dict[int, list[dict[str, Any]] | None] = {p: kept[p] for p in pages if p in kept}
    todo = [p for p in pages if p not in read]
    why = sides.NOT_READ
    if todo:
        # Each page in a worker process of its own (#1872: a 188-page set had a 3-hour limit for
        # one process reading page after page); a page past its limit, or one that crashes
        # the reader twice, keeps its drawings as they were, its sides unknown.
        pool = import_module("app.review.assistant.dims_pool")
        pool_pages: dict[int, Any] = {}
        in_process = os.environ.get("GV_DIM_LABELS_IN_PROCESS") == "1"
        try:
            pool.read_pages(
                [pool.PageTask(pdf.name, str(pdf), page) for page in todo],
                {},
                sides.read_side_page,
                pool_pages.__setitem__,
                unreadable=lambda task, error: sides.SidePage(task.number, None, error),
                count=1 if in_process else pool.workers(),
                page_limit_s=float(SIDES_TIMEOUT_S + SIDES_PAGE_S),
            )
        except Exception as failed:  # noqa: BLE001 - the pages read so far are kept
            _log.warning("assistant set pack content sides failed: %s", type(failed).__name__)
        for result in pool_pages.values():
            if result.decided is None and result.error and "longer than" in result.error:
                why = sides.TIMED_OUT  # not reached in time: said so, never kept
                continue
            read[result.page] = result.decided
        sides.write_cache(
            cache, key, {p: d for p, d in read.items() if p in todo and d is not None}
        )
    # A page the reader could not read says so; one the process never reached, why it stopped.
    unknown = {p: sides.NOT_READ if p in read else why for p in pages if read.get(p) is None}
    changed = sides.apply_sides(entries, {p: d for p, d in read.items() if d})
    sides.mark_unknown(entries, unknown)
    _log.warning(
        "assistant set pack: %d of %d pages with no heading decided by their drawings' content"
        " (%d kept from before, %d sides unknown)",
        len(changed),
        len(pages),
        len(pages) - len(todo),
        len(unknown),
    )


def sides_timeout(pages: int) -> float:
    """The content-sides process's time limit for `pages` pages (#1866): a fixed start (Python
    and the reader's imports) plus a page's worst time on a busy machine, each."""
    return float(SIDES_TIMEOUT_S + SIDES_PAGE_S * max(pages, 1))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _settle_folder(
    folder: Path,
    document: dict[str, Any] | None = None,
    side: Any = None,
) -> bool:
    """Decide the pack's candidate whole-page drawings (`settle_candidates`) from `document` (the
    labels; default: `dims.json`), set those pages' labels' and phrases' sides again from their
    drawings (`side`: `dim_labels._side`), and write `page_map.json` (and `dims.json` when it was
    read here). Returns whether there was a candidate."""
    page_map = _read_json(folder / "page_map.json")
    if not isinstance(page_map, dict) or not any(
        isinstance(e, Mapping) and CANDIDATE in e for e in page_map.get("pages") or []
    ):
        return False
    own = document is None
    dims = _read_json(folder / "dims.json") if own else document
    decided, _promoted = settle_candidates(page_map, dims if isinstance(dims, Mapping) else None)
    if isinstance(dims, dict):
        if side is None:
            side = import_module("app.review.assistant.dim_labels")._side
        halves = {
            (str(e.get("file")), int(e.get("page") or 0)): e.get("halves") or []
            for e in page_map.get("pages") or []
            if isinstance(e, Mapping)
        }
        pasted = {
            (str(e.get("file")), int(e.get("page") or 0)): e.get("pictures") or []
            for e in page_map.get("pages") or []
            if isinstance(e, Mapping)
        }
        for entry in dims.get("pages") or []:
            key = (str(entry.get("file")), int(entry.get("page") or 0))
            if key not in decided:
                continue
            for item in [*(entry.get("labels") or []), *(entry.get("phrases") or [])]:
                if isinstance(item, dict) and item.get("box"):
                    if item.get("origin") == "picture" and pasted.get(key):
                        # A label read in a pasted picture: its heading's side or none (#1835).
                        item["side"] = side(tuple(item["box"]), halves.get(key, []), pasted[key])
                    else:
                        item["side"] = side(tuple(item["box"]), halves.get(key, []))
        if own:
            _write_json(folder / "dims.json", dims)
    _write_json(folder / "page_map.json", page_map)
    return True


def _drawn_budget() -> float:
    try:
        return max(0.0, float(os.environ.get("GV_DIM_LABELS_DRAWN_BUDGET_S", DIMS_DRAWN_BUDGET_S)))
    except ValueError:
        return DIMS_DRAWN_BUDGET_S


#: The most seconds the labels' own process may take for one set: the text route and the drawn
#: route's budget, with room to spare; plus `DIMS_PAGE_S` for each page past the first 60 (#1872:
#: a 188-page set ran out of the fixed 40 minutes).
DIMS_TIMEOUT_S: Final = 2400
DIMS_PAGE_S: Final = 20


def dims_timeout(pages: int) -> float:
    """The labels' process's time limit for a set of `pages` pages."""
    return float(DIMS_TIMEOUT_S + DIMS_PAGE_S * max(0, pages - 60))


#: The most seconds the content-sides process (#1825) may take: this, plus `SIDES_PAGE_S` for each
#: page it reads (the reader takes a few seconds a page).
SIDES_TIMEOUT_S: Final = 90
SIDES_PAGE_S: Final = 60
#: The content-sides answers kept beside the packs (`content_sides.read_cache`, #1866): a folder
#: of the chat root's `set-packs/`, never a pack (no `hires/` in it).
SIDES_CACHE: Final = ".content-sides"


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
        timeout=dims_timeout(len(page_map)),
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    if not (folder / "dims.json").is_file():
        raise RuntimeError("the labels' process wrote no dims.json")


#: The reading table (`dims_pool.Table`): each page's state (`waiting`, `reading`, `ready`,
#: `failed` with why), `total`, `ready`, `failed`, and `complete` once the set's labels are settled.
#: A full check starts on a page only when it is ready (#1872).
READING_FILE: Final = "reading.json"
#: The most often (seconds) the labels' facts are written while pages finish.
WRITE_EVERY_S: Final = 10.0


def read_pack_page(task: Any, given: Mapping[str, float]) -> Any:
    """One page as the set pack reads it, in a worker process (#1872): its health check
    (`extraction/page_health`, #1745) and its labels (`dim_labels.read_page`)."""
    labels = import_module("app.review.assistant.dim_labels")
    health: dict[str, Any] | None = None
    if task.health is not None and task.upload:
        try:
            checker = import_module("extraction.page_health")
            data = Path(task.upload).read_bytes()
            health = checker.page_health(data, task.name, task.number, dict(task.health))
        except Exception as failed:  # noqa: BLE001 - the page is read without its health check
            _log.warning("assistant set pack health check failed: %s", type(failed).__name__)
    read = labels.read_page(task, given)
    read.health = health
    return read


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

    table: dict[str, Any] = {}
    last = [0.0]

    def written(document: dict[str, Any], *, now: bool = False) -> None:
        if parent is not None and os.getppid() != parent:
            raise SystemExit(0)
        # A big set's pages finish every second or two: its facts are written at most every
        # `WRITE_EVERY_S` seconds (they are worked out from the whole document), and at the end.
        if not now and time.monotonic() - last[0] < WRITE_EVERY_S:
            return
        last[0] = time.monotonic()
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
        # The reading table last: a page it says is ready has its labels in what was just written.
        if table:
            _write_json(folder / READING_FILE, {**table, "complete": False})

    listing = _read_json(folder / _LISTING_FILE)
    items = {
        str(item.get("name")): item
        for item in (listing if isinstance(listing, list) else [{"name": n} for n in names])
        if isinstance(item, dict)
    }
    ink: dict[tuple[str, int], Any] = {}
    reads: dict[int, Any] = {}
    pool = import_module("app.review.assistant.dims_pool")
    in_process = os.environ.get("GV_DIM_LABELS_IN_PROCESS") == "1"
    document = labels.build_dims(
        {name: uploads / name for name in names},
        pages,
        drawn_budget_s=_drawn_budget(),
        on_page=written,
        ink=ink,
        # Pages read at once in worker processes (#1872); tests read in this process unless set.
        workers=(1 if in_process and not os.environ.get(pool.WORKERS_SETTING) else pool.workers()),
        keep=folder / pool.PAGES_FOLDER,
        reader=read_pack_page,
        health={name: items.get(name) or {"name": name} for name in names},
        uploads={name: uploads / name for name in names},
        on_status=table.update,
        reads=reads,
    )
    written(document, now=True)
    # Each page's health check (#1745), read with its labels (#1872).
    checked = [reads[i].health for i in sorted(reads) if reads[i].health is not None]
    if checked:
        _write_json(folder / "health.json", {"version": PACK_VERSION, "pages": checked})
    # The candidate whole-page drawings, decided from every route's labels (#1772); the pack's
    # facts are written again from the settled page map.
    if _settle_folder(folder, document, labels._side):
        settled = _read_json(folder / "page_map.json") or {}
        pages["pages"] = list(settled.get("pages") or [])
        anchored_memo.clear()
        written(document, now=True)
    _write_coverage(folder, document, ink)
    if table:
        _write_json(folder / READING_FILE, {**table, "complete": True})


def _write_coverage(
    folder: Path, document: Mapping[str, Any], ink: Mapping[tuple[str, int], Any]
) -> None:
    """`reading_coverage.json` (#1862): how many printed numbers the labels read, per page, and
    where the rest are. Counts only; a failure leaves the pack without it, said in the log."""
    try:
        coverage = import_module("app.review.assistant.reading_coverage")
        reading_ink = import_module("app.review.assistant.reading_ink")
        started = time.process_time()
        pages = {
            (str(e.get("file")), int(e.get("page") or 0)): ink.get(
                (str(e.get("file")), int(e.get("page") or 0))
            )
            for e in document.get("pages") or []
            if isinstance(e, Mapping)
        }
        spots = reading_ink.unread_spots(pages)
        annotations = _read_json(folder / "annotations.json")
        seconds = sum(float(getattr(page, "seconds", 0.0)) for page in ink.values())
        seconds += time.process_time() - started
        coverage.write(
            folder,
            coverage.build(
                document,
                spots,
                annotations if isinstance(annotations, list) else [],
                seconds=seconds,
            ),
        )
    except Exception:  # noqa: BLE001 - the pack works without its reading coverage
        _log.exception("reading coverage failed; the pack has no reading_coverage.json")


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

        def run(child: bool) -> None:
            try:
                if child:
                    _build_in_child(folder, uploads, listing)
                else:
                    build_pack(folder, uploads, listing)
            except Exception as failed:  # noqa: BLE001 - the chat works without the pack
                _log.warning("assistant set pack failed: %s", failed)
            finally:
                with _running_lock:
                    _running.pop(folder, None)

        if not background:
            _running.pop(folder, None)
        else:
            thread = threading.Thread(
                target=run, args=(not pack_in_process(),), name="gv-set-pack", daemon=True
            )
            _running[folder] = thread
            thread.start()
            return thread
    run(False)
    return None


# ---- the pack's own process ---------------------------------------------------------------------

#: Builds the pack in this process instead of its own (tests; `GV_DIM_LABELS_IN_PROCESS=1` too).
PACK_IN_PROCESS_SETTING: Final = "GV_ASSISTANT_PACK_IN_PROCESS"
#: The most seconds the pack's own process may take: its labels' own limit, plus an hour for the
#: rest (the text, the health check and the pictures of a few hundred pages).
PACK_TIMEOUT_S: Final = DIMS_TIMEOUT_S + 3600
#: And for each page past the first 60, what the labels get (`dims_timeout`) and a minute for the
#: rest.
PACK_PAGE_S: Final = DIMS_PAGE_S + 60
#: The listing the pack's own process reads (in the pack's folder).
_LISTING_FILE: Final = ".listing.json"


def pack_in_process() -> bool:
    """Whether a background pack is built in the server's process (a setting), not its own."""
    return any(
        os.environ.get(name) == "1"
        for name in (PACK_IN_PROCESS_SETTING, "GV_DIM_LABELS_IN_PROCESS")
    )


def pack_command(folder: Path, uploads: Path, listing_file: Path) -> list[str]:
    """The command of the pack's own process (`main`)."""
    module = "app.review.assistant.set_pack"
    return [sys.executable, "-m", module, "build", str(folder), str(uploads), str(listing_file)]


def crash_reason(folder: Path, signal_number: int | None) -> str:
    """Why the pack's own process stopped, in plain words, naming the page it was reading."""
    progress = _read_json(folder / PROGRESS_FILE)
    how = "the PDF reader crashed" if signal_number is not None else "the reading stopped"
    if isinstance(progress, dict) and progress.get("file"):
        page = int(progress.get("page") or 0)
        where = f"page {page} of {progress['file']}" if page else str(progress["file"])
        step = str(progress.get("step") or "reading")
        return f"{where} could not be read: {how} while {step} it"
    return f"the drawing set could not be read: {how}"


def _build_in_child(folder: Path, uploads: Path, listing: Sequence[Mapping[str, Any]]) -> None:
    """`build_pack` in a process of its own: the PDF engine reads and draws every page of the set
    there, so a page that crashes it fails this pack, said in `status.json` (`error`
    `renderer_crashed`, `reason` naming the page), and never the server. Raises then."""
    root = Path(__file__).resolve().parents[3]
    folder.mkdir(parents=True, exist_ok=True)
    listing_file = folder / _LISTING_FILE
    _write_json(listing_file, [dict(item) for item in listing])
    (folder / PROGRESS_FILE).unlink(missing_ok=True)
    names = [str(item["name"]) for item in listing]
    pages = sum(int(item.get("pages") or 0) for item in listing)
    limit = PACK_TIMEOUT_S + PACK_PAGE_S * max(0, pages - 60)
    try:
        done = subprocess.run(
            pack_command(folder, uploads, listing_file),
            cwd=root,
            env=child_environment(root),
            timeout=limit,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired as late:
        reason = f"the drawing set took longer than {round(limit / 60)} minutes to read"
        status = read_status(folder)
        _set_status(
            folder,
            "failed",
            status.get("done") or [],
            files=names,
            error="timed_out",
            reason=reason,
        )
        raise RuntimeError(reason) from late
    if done.returncode == 0:
        return
    status = read_status(folder)
    if done.returncode > 0 and status.get("state") == "failed":
        raise RuntimeError(f"the pack's process failed: {status.get('error')}")
    signal_number = -done.returncode if done.returncode < 0 else None
    reason = crash_reason(folder, signal_number)
    _set_status(
        folder,
        "failed",
        status.get("done") or [],
        files=names,
        error="renderer_crashed" if signal_number is not None else "stopped",
        reason=reason,
    )
    raise RuntimeError(reason)


def main(arguments: Sequence[str]) -> int:
    """`python -m app.review.assistant.set_pack build <pack folder> <uploads folder> <listing>`:
    the set pack built in a process of its own (`_build_in_child`). It stops when the server that
    started it is gone."""
    if len(arguments) != 4 or arguments[0] != "build":
        print("usage: set_pack build <pack folder> <uploads folder> <listing.json>")
        return 2
    logging.basicConfig(
        level=logging.WARNING, format="%(asctime)s %(levelname)s:  %(name)s: %(message)s"
    )
    parent = os.getppid()

    def watch() -> None:
        while os.getppid() == parent:
            time.sleep(2.0)
        os._exit(0)

    threading.Thread(target=watch, name="gv-set-pack-parent", daemon=True).start()
    folder, uploads, listing_file = Path(arguments[1]), Path(arguments[2]), Path(arguments[3])
    listing = json.loads(listing_file.read_text(encoding="utf-8"))
    # The module by its name (this file also runs as `__main__`): one copy of its state.
    pack = import_module("app.review.assistant.set_pack")
    try:
        pack.build_pack(folder, uploads, listing)
    except Exception as failed:  # noqa: BLE001 - said in status.json and here
        _log.warning("assistant set pack failed: %s", type(failed).__name__)
        return 1
    return 0


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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
