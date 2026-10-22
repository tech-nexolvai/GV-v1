"""pdfium is entered by one thread at a time, through one door (`extraction/pdfium_guard.py`).

The live API died on 2026-10-22 with heap corruption inside pdfium: two threads were drawing pages
at once (the plan check's copy of a big set and a new chat's copy of the same set, both through the
front door, which held no lock). These tests hold the rule that stops it:

- every pdfium function holds the one lock, so a render waits while another thread is inside;
- many threads drawing and reading text at once get exactly what one thread gets;
- an object the garbage collector closes never waits for the lock (no deadlock), and is closed
  under it later;
- **no module imports `pypdfium2` but the guard**, so new code cannot go around it.
"""

from __future__ import annotations

import ast
import gc
import hashlib
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from reportlab.lib.pagesizes import letter  # type: ignore[import-untyped]
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from extraction import pdfium_guard
from extraction.pdfium_guard import PDFIUM_LOCK, pdfium, pdfium_raw

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Folders of code that runs (not tests): none may import pypdfium2 but the guard.
_CODE_FOLDERS = (
    "app",
    "eval",
    "evidence",
    "extraction",
    "reports",
    "retrieval",
    "rules",
    "scripts",
    "storage",
    "units",
    "verdict",
    "vocabulary",
    "workflow",
)
_GUARD = REPO_ROOT / "extraction" / "pdfium_guard.py"


def _pdf(pages: int = 3, seed: int = 0) -> bytes:
    """A small drawing-like PDF: lines, arcs and text in two fonts on each page."""
    buffer = io.BytesIO()
    drawing = canvas.Canvas(buffer, pagesize=letter)
    for page in range(pages):
        for line in range(40):
            drawing.line(20 + line * 13, 30, 600 - line * 7, 760 - (line + seed) % 50)
        drawing.circle(300, 400, 50 + page * 10 + seed)
        drawing.setFont("Helvetica", 9)
        for row in range(30):
            drawing.drawString(40, 700 - row * 20, f"{page}-{row}-{seed} 2'-6 1/2\" CABINET")
        drawing.setFont("Courier-Bold", 14)
        drawing.drawString(300, 100, f"SHEET A{page + seed}")
        drawing.showPage()
    drawing.save()
    return buffer.getvalue()


def _draw_and_read(data: bytes, *, close_pages: bool) -> list[tuple[str, str]]:
    """Each page's picture's hash and its text, as one thread reads them."""
    document = pdfium.PdfDocument(data)
    out: list[tuple[str, str]] = []
    try:
        for index in range(len(document)):
            page = document[index]
            picture = page.render(scale=1.5, may_draw_forms=True).to_pil()
            textpage = page.get_textpage()
            text = textpage.get_text_range()
            out.append((hashlib.sha256(picture.tobytes()).hexdigest(), text))
            if close_pages:
                textpage.close()
                page.close()
            # else: left to the garbage collector, from whichever thread collects it
    finally:
        document.close()
    return out


def test_every_pdfium_function_holds_the_lock() -> None:
    names = pdfium_guard.wrapped_functions()
    assert len(names) > 400
    for name in ("FPDF_RenderPageBitmap", "FPDF_LoadMemDocument64", "FPDFText_LoadPage"):
        assert name in names
        assert pdfium_guard.guarded(getattr(pdfium_raw, name))
    # pypdfium2's helpers call the raw module, so they go through the same wrappers.
    assert pdfium.raw is pdfium_raw
    bare = [
        name
        for name, value in vars(pdfium_raw).items()
        if isinstance(value, pdfium_guard._C_FUNCTION) and not isinstance(value, type)
    ]
    assert bare == []


def test_a_render_waits_while_another_thread_is_inside_pdfium() -> None:
    data = _pdf(1)
    finished = threading.Event()

    def render() -> None:
        _draw_and_read(data, close_pages=True)
        finished.set()

    with PDFIUM_LOCK:
        worker = threading.Thread(target=render)
        worker.start()
        assert not finished.wait(0.5), "a render ran while another thread held pdfium"
    worker.join(timeout=30)
    assert finished.is_set()


def test_only_one_thread_is_ever_inside_pdfium(monkeypatch: pytest.MonkeyPatch) -> None:
    """A probe inside the wrapped render counts the threads in it at once: never more than one."""
    real = pdfium_raw.FPDF_RenderPageBitmap
    inside = 0
    most = 0
    count = threading.Lock()

    def probe(*args: Any) -> Any:
        nonlocal inside, most
        with count:
            inside += 1
            most = max(most, inside)
        time.sleep(0.002)  # long enough for another thread to arrive if it could
        try:
            return real.__wrapped__(*args)
        finally:
            with count:
                inside -= 1

    probed = pdfium_guard._wrap("FPDF_RenderPageBitmap", probe)
    monkeypatch.setattr(pdfium_raw, "FPDF_RenderPageBitmap", probed)
    files = [_pdf(2, seed) for seed in range(4)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda i: _draw_and_read(files[i % 4], close_pages=i % 2 == 0), range(24)))
    assert most == 1


def test_many_threads_drawing_and_reading_get_what_one_thread_gets() -> None:
    files = [_pdf(3, seed) for seed in range(6)]
    alone = [_draw_and_read(data, close_pages=True) for data in files]
    jobs = [(i % len(files), i % 3 == 0) for i in range(48)]
    with ThreadPoolExecutor(max_workers=16) as pool:
        together = list(
            pool.map(lambda job: _draw_and_read(files[job[0]], close_pages=job[1]), jobs)
        )
    assert together == [alone[index] for index, _ in jobs]
    gc.collect()
    pdfium.PdfDocument(files[0]).close()  # any call makes the closes the collector queued
    assert pdfium_guard.queued_closes() == 0


def test_a_collected_object_never_waits_for_the_lock() -> None:
    """The garbage collector may close a page from a thread that holds another lock (a log
    handler's): it must not wait for pdfium's lock there. It is queued, then closed under the lock
    by the next pdfium call, the page before its document."""
    data = _pdf(2)
    document = pdfium.PdfDocument(data)
    page = document[0]
    bitmap = page.render(scale=0.5)
    holding = threading.Event()
    release = threading.Event()

    def hold() -> None:
        with PDFIUM_LOCK:
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert holding.wait(5)
    try:
        started = time.monotonic()
        del bitmap, page
        gc.collect()
        assert time.monotonic() - started < 1.0  # it did not wait for the holder
        assert pdfium_guard.queued_closes() >= 2
    finally:
        release.set()
        holder.join(timeout=10)
    document.close()  # drains the queue first: the page is closed before its document
    assert pdfium_guard.queued_closes() == 0


def _imports_pypdfium2(tree: ast.AST) -> list[int]:
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0].startswith("pypdfium2") for alias in node.names):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0].startswith("pypdfium2"):
                lines.append(node.lineno)
        elif isinstance(node, ast.Call) and node.args:
            name = ast.unparse(node.func)
            first = node.args[0]
            if (
                name.split(".")[-1] in ("import_module", "__import__")
                and isinstance(first, ast.Constant)
                and isinstance(first.value, str)
                and first.value.split(".")[0].startswith("pypdfium2")
            ):
                lines.append(node.lineno)
    return lines


def test_no_module_imports_pypdfium2_but_the_guard() -> None:
    """New code reaches pdfium only through `extraction.pdfium_guard` (`pdfium`, `pdfium_raw`),
    so it can never call it without the lock, nor before the lock is in place."""
    offenders: list[str] = []
    for folder in _CODE_FOLDERS:
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            if path == _GUARD:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            offenders += [
                f"{path.relative_to(REPO_ROOT)}:{line}" for line in _imports_pypdfium2(tree)
            ]
    assert offenders == [], (
        "import pdfium through extraction.pdfium_guard (`from extraction.pdfium_guard import "
        f"pdfium`), never pypdfium2 itself: {offenders}"
    )


def test_the_guard_test_sees_every_way_of_importing() -> None:
    caught = _imports_pypdfium2(
        ast.parse(
            "import pypdfium2\n"
            "import pypdfium2.raw as r\n"
            "from pypdfium2 import PdfDocument\n"
            "from pypdfium2_raw import bindings\n"
            "x = import_module('pypdfium2')\n"
            "y = importlib.import_module('pypdfium2.raw')\n"
            "z = __import__('pypdfium2')\n"
            "fine = import_module('extraction.pdfium_guard')\n"
        )
    )
    assert caught == [1, 2, 3, 4, 5, 6, 7]


def test_the_assistant_lock_is_the_one_lock() -> None:
    from app.review.assistant import chat_drawing

    assert chat_drawing.PDFIUM_LOCK is PDFIUM_LOCK
    assert chat_drawing._PDFIUM_LOCK is PDFIUM_LOCK
