"""The hi-res page cache (#1548): each page drawn once at 300 dpi, crops cut from it. $0, no network.

The PDFs are synthetic (drawn in the test), never a client file.
"""

from __future__ import annotations

import io
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import page_cache, set_pack
from app.review.assistant.chat_drawing import crop_png
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.sandbox import ChatWorkspace
from tests.review.assistant.chat_fakes import FakeSandbox

WIDTH, HEIGHT = 842.0, 595.0


def _drawing(path: Path, pages: int = 3, *, colour: bool = False) -> Path:
    page = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    for number in range(pages):
        page.setLineWidth(0.6)
        for step in range(0, 800, 20):
            page.line(20 + step, 40, 20 + step, 560)
        page.setFont("Helvetica", 9)
        page.drawString(100, 300, f'PAGE {number + 1}  3\'-1"  46"')
        if colour:
            page.setFillColorRGB(0.9, 0.1, 0.1)
            page.rect(400, 200, 60, 30, fill=1, stroke=0)
        page.showPage()
    page.save()
    return path


def _pixels(png: bytes) -> np.ndarray:
    with Image.open(io.BytesIO(png)) as picture:
        return np.asarray(picture.convert("L"), dtype=np.int16)


def _alike(first: bytes, second: bytes) -> bool:
    """The same picture to a pixel's rounding: compared on their common area."""
    a, b = _pixels(first), _pixels(second)
    if abs(a.shape[0] - b.shape[0]) > 1 or abs(a.shape[1] - b.shape[1]) > 1:
        return False
    rows, columns = min(a.shape[0], b.shape[0]), min(a.shape[1], b.shape[1])
    return float(np.abs(a[:rows, :columns] - b[:rows, :columns]).mean()) < 12


@pytest.fixture(autouse=True)
def _fresh_memory() -> None:
    page_cache._memory.clear()
    page_cache._held = 0


def _count_renders(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    drawn: list[int] = []
    real = page_cache._render

    def counting(folder: Path, pdf: Path, page: int) -> Path | None:
        drawn.append(page)
        return real(folder, pdf, page)

    monkeypatch.setattr(page_cache, "_render", counting)
    return drawn


def test_a_page_is_drawn_once_and_every_later_crop_is_cut_from_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    drawn = _count_renders(monkeypatch)
    boxes = [(80.0, 280.0, 260.0, 320.0), (300.0, 100.0, 500.0, 200.0), (10.0, 10.0, 60.0, 60.0)]
    for box in boxes:
        cached = set_pack.crop_picture(pdf, 2, box, cache=cache)
        direct = set_pack.crop_picture(pdf, 2, box)
        # Same dpi and box, the same size to a pixel (pdfium rounds a crop's edge its own way);
        # the picture itself is the same drawing.
        assert (cached.dpi, cached.box) == (direct.dpi, direct.box)
        assert abs(cached.width - direct.width) <= 1 and abs(cached.height - direct.height) <= 1
        assert _alike(cached.png, direct.png)
    assert drawn == [2]
    stored = cache / page_cache.pdf_key(pdf) / "p-002"
    assert (stored / "meta.json").is_file()
    assert sorted(p.name for p in stored.glob("t-*.png"))


def test_a_lower_dpi_is_resized_from_the_300_dpi_picture(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    crop = set_pack.crop_picture(pdf, 1, (80, 280, 260, 320), dpi=150, cache=tmp_path / "hires")
    assert crop.dpi == 150
    assert abs(crop.width - 180 * 150 / 72) <= 1 and abs(crop.height - 40 * 150 / 72) <= 1


def test_a_crop_sharper_than_300_dpi_renders_the_page_again(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    assert page_cache.cut(cache, pdf, 1, (10, 10, 50, 50), 600 / 72) is None
    assert not (cache / page_cache.pdf_key(pdf) / "p-001").exists()


def test_a_page_that_is_not_there_is_told_plainly(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    with pytest.raises(ValueError, match="no page 9"):
        set_pack.crop_picture(pdf, 9, (10, 10, 50, 50), cache=tmp_path / "hires")
    with pytest.raises(ValueError, match="not on page 1"):
        set_pack.crop_picture(pdf, 1, (2000, 2000, 2100, 2100), cache=tmp_path / "hires")


def test_grey_pages_are_stored_grey_and_coloured_ones_in_colour(tmp_path: Path) -> None:
    cache = tmp_path / "hires"
    grey = _drawing(tmp_path / "grey.pdf", 1)
    coloured = _drawing(tmp_path / "colour.pdf", 1, colour=True)
    assert page_cache.cut(cache, grey, 1, (0, 0, 100, 100), 2).mode == "L"  # type: ignore[union-attr]
    picture = page_cache.cut(cache, coloured, 1, (390, 360, 470, 400), 2)
    assert picture is not None and picture.mode == "RGB"
    red = np.asarray(picture)
    assert ((red[:, :, 0] > 180) & (red[:, :, 1] < 80)).any()


def test_two_askers_at_once_draw_the_page_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    drawn = _count_renders(monkeypatch)
    results: list[Any] = []

    def ask() -> None:
        results.append(page_cache.cut(cache, pdf, 3, (100, 100, 300, 300), 2))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and all(r is not None for r in results)
    assert drawn == [3]


def test_askers_racing_to_start_a_page_draw_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Looking for a running drawing and starting one are one step: with the pool slow to hand
    out (as a busy machine is), four askers at once still draw the page once. They used to look,
    all find none, and all start one (CI saw [3, 3, 3, 3])."""
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    drawn = _count_renders(monkeypatch)
    for name in ("_executor", "_executor_locked"):
        real = getattr(page_cache, name, None)
        if real is None:
            continue

        def slow(real: Any = real) -> Any:
            time.sleep(0.05)
            return real()

        monkeypatch.setattr(page_cache, name, slow)
    start = threading.Barrier(4)
    results: list[Any] = []

    def ask() -> None:
        start.wait()
        results.append(page_cache.cut(cache, pdf, 3, (100, 100, 300, 300), 2))

    threads = [threading.Thread(target=ask) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 4 and all(r is not None for r in results)
    assert drawn == [3]


def test_prefetch_draws_the_pages_in_the_background(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    page_cache.prefetch(cache, pdf, [1, 3])
    for page in (1, 3):
        assert page_cache.render_page(cache, pdf, page) is not None
    assert not (cache / page_cache.pdf_key(pdf) / "p-002").exists()


def test_the_evidence_crop_is_cut_from_the_cache(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    cached = crop_png(pdf, 1, (80, 280, 260, 320), cache=cache)
    direct = crop_png(pdf, 1, (80, 280, 260, 320))
    assert cached is not None and direct is not None
    assert _alike(cached, direct)
    assert (cache / page_cache.pdf_key(pdf) / "p-001" / "meta.json").is_file()
    # Outside the page: no picture, as before.
    assert crop_png(pdf, 1, (2000, 2000, 2100, 2100), cache=cache) is None


def _workspace(tmp_path: Path) -> ChatWorkspace:
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=tmp_path / "pack")
    (tmp_path / "pack").mkdir(exist_ok=True)
    _drawing(workspace.uploads / "set.pdf")
    return workspace


def test_zoom_and_finding_pictures_use_the_packs_cache(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    listing = [{"name": "set.pdf", "pages": 3, "kind": "the vendor's"}]
    tools = ChatTools(workspace=workspace, sandbox=FakeSandbox(workspace), uploads=listing)
    result = tools.zoom(
        {"crops": [{"page": 2, "box": {"x0": 80, "y0": 280, "x1": 260, "y1": 320}}]},
        new_step("z1", "zoom", {}),
    )
    assert result.step["ok"] is True
    assert list((tmp_path / "pack" / "hires").glob("*/p-002/meta.json"))
    book = FindingBook(workspace=workspace, listing=listing, page_count=3)
    picture, why = book._picture(
        {"page": 3, "x0": 80, "y0": 280, "x1": 260, "y1": 320, "file": "set.pdf"}
    )
    assert picture is not None and why is None
    original = page_cache.pdf_key(workspace.uploads / "set.pdf")
    assert (tmp_path / "pack" / "hires" / original / "p-003" / "meta.json").is_file()


def test_verify_values_ink_picture_is_cut_from_the_cache_when_300_dpi_or_less(
    tmp_path: Path,
) -> None:
    vv = __import__("app.review.assistant.verify_value", fromlist=["_read_page"])
    pdf = _drawing(tmp_path / "set.pdf", 1)
    page = vv._read_page(pdf, 0)
    big = (60.0, 60.0, 700.0, 520.0)  # drawn at about 3 px a point: under 300 dpi
    direct = vv._picture(page, big)
    token = vv._CACHE_FOLDER.set(tmp_path / "hires")
    try:
        cached = vv._picture(page, big)
        small = vv._picture(page, (90.0, 290.0, 130.0, 310.0))  # 10 px a point: rendered
    finally:
        vv._CACHE_FOLDER.reset(token)
    assert direct is not None and cached is not None and small is not None
    assert abs(direct.shape[0] - cached.shape[0]) <= 1
    assert abs(direct.shape[1] - cached.shape[1]) <= 1
    rows, columns = min(direct.shape[0], cached.shape[0]), min(direct.shape[1], cached.shape[1])
    assert np.abs(direct[:rows, :columns].astype(int) - cached[:rows, :columns]).mean() < 12
    layers = list((tmp_path / "hires" / "layers").glob("*.pdf"))
    assert len(layers) == 1
    assert list((tmp_path / "hires").glob("*/p-001/meta.json"))
