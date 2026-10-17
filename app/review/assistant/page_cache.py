"""The hi-res page cache: each page drawn once at 300 dpi, every later crop cut from it (#1548).

claude.ai, on a 60-page set, rendered every page once at 300 dpi and then cut each crop it wanted
from those pictures with PIL: a crop was instant, nothing was rendered again. Our `zoom`, evidence
crops and `verify_value` rendered the page again for every crop (pdfium parses the whole page's
drawing each time, however small the crop).

**How.** The first crop on a page (or a `prefetch` of it) draws the whole page at `CACHE_DPI` in a
small thread pool and saves it on disk as square PNG tiles (`TILE_PX`), so a later crop decodes only
the few tiles it touches. A crop that needs `CACHE_DPI` or less is cut from the tiles and, when it
wants fewer dots, resized; a sharper crop is not this module's: the caller renders it as before.

**Where.** `<folder>/<pdf key>/p-NNN/` with `meta.json` (page size in points, picture size in
pixels, tile size, dpi) written last: a page folder is renamed into place whole, so a reader never
sees half of one. The key is the PDF's SHA-256 (the same bytes give the same pictures, whichever
chat or copy asks); page sizes are kept per key in `sizes.json`. The set pack keeps it at
`<pack>/hires/` (`set_pack.HIRES`).

**Thread-safe.** One render per page at a time (later askers wait for it); pdfium itself is held
under `chat_drawing.PDFIUM_LOCK` (it is not thread-safe), the tiles are written outside the lock.

**Loaded by name, never imported** by `app/api/` (it draws with pypdfium2 and Pillow).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import threading
from collections import OrderedDict
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import numpy as np
import pypdfium2 as pdfium  # type: ignore[import-untyped]
from PIL import Image

from app.review.assistant.chat_drawing import PDFIUM_LOCK

__all__ = [
    "CACHE_DPI",
    "TILE_PX",
    "PageSize",
    "cut",
    "page_sizes",
    "pdf_key",
    "prefetch",
    "render_page",
]

_log = logging.getLogger(__name__)

#: The dpi every page is drawn at once; a crop at this dpi or less is cut from it.
CACHE_DPI: Final = 300
#: The side of one stored tile (pixels).
TILE_PX: Final = 1024
#: A page whose 300-dpi picture would be bigger than this (pixels) is not cached (a 36 x 24 in
#: sheet is 78 million): its crops render as before.
MAX_PAGE_PIXELS: Final = 120_000_000
#: Pages drawn at once (`GV_ASSISTANT_PAGE_CACHE_WORKERS`). pdfium draws one page at a time; the
#: workers overlap one page's drawing with others' tile writing.
_DEFAULT_WORKERS: Final = 3
_SLACK: Final = 0.01
#: A page whose colours differ by no more than this between channels is stored grey.
_GREY_SPREAD: Final = 12
#: Decoded tiles kept in memory (`GV_ASSISTANT_PAGE_CACHE_MEMORY_MB`): a crop on a page looked at
#: a moment ago is cut without decoding anything.
_DEFAULT_MEMORY_MB: Final = 256

_pool_lock: Final = threading.Lock()
_pool: ThreadPoolExecutor | None = None
_jobs: dict[tuple[Path, str, int], Future[Path | None]] = {}
_keys: dict[tuple[str, int, int], str] = {}
_sizes_lock: Final = threading.Lock()
_memory: OrderedDict[tuple[str, int, int], Image.Image] = OrderedDict()
_held = 0


@dataclass(frozen=True, slots=True)
class PageSize:
    """A page's shown size in points (rotation applied)."""

    width: float
    height: float


def _workers() -> int:
    try:
        value = int(os.environ.get("GV_ASSISTANT_PAGE_CACHE_WORKERS", _DEFAULT_WORKERS))
    except ValueError:
        value = _DEFAULT_WORKERS
    return max(1, min(8, value))


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="gv-page-cache")
        return _pool


def pdf_key(pdf: Path) -> str:
    """The PDF's SHA-256 (hex, 24 characters), remembered per path, size and change time."""
    stat = pdf.stat()
    marker = (str(pdf.resolve()), stat.st_mtime_ns, stat.st_size)
    with _pool_lock:
        known = _keys.get(marker)
    if known is not None:
        return known
    digest = hashlib.sha256()
    with pdf.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    key = digest.hexdigest()[:24]
    with _pool_lock:
        _keys[marker] = key
    return key


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex[:8]}.tmp")
    temporary.write_bytes(data)
    temporary.chmod(0o644)
    os.replace(temporary, path)


def page_sizes(folder: Path, pdf: Path) -> list[PageSize]:
    """Every page's shown size (points), read once per PDF and kept beside its pictures."""
    where = folder / pdf_key(pdf)
    stored = where / "sizes.json"
    try:
        listed = json.loads(stored.read_text(encoding="utf-8"))
        return [PageSize(float(w), float(h)) for w, h in listed]
    except (OSError, ValueError, TypeError):
        pass
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            sizes = [PageSize(*document[n].get_size()) for n in range(len(document))]
        finally:
            document.close()
    with _sizes_lock:
        where.mkdir(parents=True, exist_ok=True)
        _write_atomic(stored, json.dumps([[s.width, s.height] for s in sizes]).encode())
    return sizes


def _page_folder(folder: Path, pdf: Path, page: int) -> Path:
    return folder / pdf_key(pdf) / f"p-{page:03d}"


def _render(folder: Path, pdf: Path, page: int) -> Path | None:
    """Draw page `page` (from 1) at `CACHE_DPI` and store its tiles; `None` when it is too big."""
    final = _page_folder(folder, pdf, page)
    if (final / "meta.json").is_file():
        return final
    sizes = page_sizes(folder, pdf)
    if not 1 <= page <= len(sizes):
        raise ValueError(f"there is no page {page} (the file has {len(sizes)})")
    size = sizes[page - 1]
    scale = CACHE_DPI / 72
    if size.width * scale * size.height * scale > MAX_PAGE_PIXELS:
        return None
    with PDFIUM_LOCK:
        document = pdfium.PdfDocument(str(pdf))
        try:
            picture = document[page - 1].render(scale=scale, may_draw_forms=True).to_pil()
        finally:
            document.close()
    picture = _plain(picture)
    building = final.with_name(f".{final.name}.{uuid4().hex[:8]}")
    building.mkdir(parents=True)
    columns = -(-picture.width // TILE_PX)
    rows = -(-picture.height // TILE_PX)
    for row in range(rows):
        for column in range(columns):
            left, top = column * TILE_PX, row * TILE_PX
            tile = picture.crop(
                (left, top, min(picture.width, left + TILE_PX), min(picture.height, top + TILE_PX))
            )
            tile.save(building / f"t-{row:03d}-{column:03d}.png", format="PNG", compress_level=1)
    meta = {
        "mode": picture.mode,
        "dpi": CACHE_DPI,
        "tile": TILE_PX,
        "width_px": picture.width,
        "height_px": picture.height,
        "width_pt": size.width,
        "height_pt": size.height,
    }
    (building / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    try:
        os.replace(building, final)
    except OSError:
        # Another process stored it first: theirs is the same picture.
        shutil.rmtree(building, ignore_errors=True)
    return final


def _plain(picture: Image.Image) -> Image.Image:
    """The page as grey when nothing on it is coloured (most drawings: a third of the bytes to
    store, decode and send), else as RGB."""
    rgb = picture.convert("RGB")
    channels = np.asarray(rgb)
    spread = channels.max(axis=2).astype(np.int16) - channels.min(axis=2)
    if int(spread.max(initial=0)) <= _GREY_SPREAD:
        return rgb.convert("L")
    return rgb


def _tile(stored: Path, row: int, column: int) -> Image.Image:
    """One tile, decoded once and kept in memory while it is used (`_MEMORY_BYTES` in all)."""
    global _held
    key = (str(stored), row, column)
    with _pool_lock:
        found = _memory.get(key)
        if found is not None:
            _memory.move_to_end(key)
            return found
    with Image.open(stored / f"t-{row:03d}-{column:03d}.png") as opened:
        opened.load()
        tile = opened.copy()
    size = tile.width * tile.height * len(tile.getbands())
    with _pool_lock:
        if key not in _memory:
            _memory[key] = tile
            _held += size
        while _held > _memory_bytes() and len(_memory) > 1:
            _, old = _memory.popitem(last=False)
            _held -= old.width * old.height * len(old.getbands())
    return tile


def _memory_bytes() -> int:
    try:
        megabytes = int(os.environ.get("GV_ASSISTANT_PAGE_CACHE_MEMORY_MB", _DEFAULT_MEMORY_MB))
    except ValueError:
        megabytes = _DEFAULT_MEMORY_MB
    return max(16, megabytes) * 1_000_000


def render_page(folder: Path, pdf: Path, page: int) -> Path | None:
    """The page's tile folder, drawn now (in the pool) unless it is stored; waits for it."""
    return _submit(folder, pdf, page).result()


def _submit(folder: Path, pdf: Path, page: int) -> Future[Path | None]:
    stored = _page_folder(folder, pdf, page)
    if (stored / "meta.json").is_file():
        done: Future[Path | None] = Future()
        done.set_result(stored)
        return done
    job_key = (folder.resolve(), pdf_key(pdf), page)
    with _pool_lock:
        running = _jobs.get(job_key)
        if running is not None:
            return running
    job = _executor().submit(_render, folder, pdf, page)
    with _pool_lock:
        running = _jobs.setdefault(job_key, job)

    def forget(_: Future[Path | None]) -> None:
        with _pool_lock:
            if _jobs.get(job_key) is job:
                del _jobs[job_key]

    job.add_done_callback(forget)
    return running


def prefetch(folder: Path, pdf: Path, pages: Iterable[int]) -> None:
    """Start drawing these pages (each at most once); returns at once."""
    for page in pages:
        try:
            _submit(folder, pdf, page)
        except OSError:
            return


def cut(
    folder: Path,
    pdf: Path,
    page: int,
    box: tuple[float, float, float, float],
    scale: float,
) -> Image.Image | None:
    """The area `box` (left, top, right, bottom: points of the page as shown, inside the page) at
    `scale` pixels per point, cut from the page's stored picture; `None` when the cache cannot give
    it (sharper than `CACHE_DPI`, or a page too big to cache): the caller renders it."""
    if scale * 72 > CACHE_DPI + _SLACK:
        return None
    stored = render_page(folder, pdf, page)
    if stored is None:
        return None
    meta: dict[str, Any] = json.loads((stored / "meta.json").read_text(encoding="utf-8"))
    ratio = float(meta["dpi"]) / 72
    tile = int(meta["tile"])
    left, top, right, bottom = box
    x0 = max(0, int(left * ratio))
    y0 = max(0, int(top * ratio))
    x1 = min(int(meta["width_px"]), max(x0 + 1, round(right * ratio)))
    y1 = min(int(meta["height_px"]), max(y0 + 1, round(bottom * ratio)))
    canvas = Image.new(str(meta.get("mode") or "RGB"), (x1 - x0, y1 - y0), "white")
    for row in range(y0 // tile, (y1 - 1) // tile + 1):
        for column in range(x0 // tile, (x1 - 1) // tile + 1):
            canvas.paste(_tile(stored, row, column), (column * tile - x0, row * tile - y0))
    width = max(1, round((right - left) * scale))
    height = max(1, round((bottom - top) * scale))
    if (width, height) != canvas.size:
        canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    return canvas
