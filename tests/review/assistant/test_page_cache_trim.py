"""The hi-res page cache stays bounded (#1575): least-used pages go first, per set and for all sets,
and a set no chat uses any more loses its cache. $0, no network.

The PDFs are synthetic (drawn in the test), never a client file.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.config import Settings
from app.review.assistant import chat_service, page_cache, set_pack

WIDTH, HEIGHT = 842.0, 595.0
BOX = (80.0, 280.0, 260.0, 320.0)


def _drawing(path: Path, pages: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    page = canvas.Canvas(str(path), pagesize=(WIDTH, HEIGHT))
    for number in range(pages):
        page.setLineWidth(0.6)
        for step in range(0, 800, 20):
            page.line(20 + step, 40, 20 + step, 560)
        page.setFont("Helvetica", 9)
        page.drawString(100, 300, f'PAGE {number + 1}  3\'-1"  46"  {path.stem}')
        page.showPage()
    page.save()
    return path


@pytest.fixture(autouse=True)
def _fresh_memory() -> None:
    page_cache._memory.clear()
    page_cache._held = 0


def _stored(cache: Path, pdf: Path, page: int) -> Path:
    return cache / page_cache.pdf_key(pdf) / f"p-{page:03d}"


def _draw_pages(cache: Path, pdf: Path, pages: list[int]) -> None:
    for page in pages:
        assert page_cache.render_page(cache, pdf, page) is not None


def _used(stored: Path, seconds_ago: float) -> None:
    when = time.time() - seconds_ago
    os.utime(stored / "meta.json", (when, when))


def _bytes(folder: Path) -> int:
    return sum(item.stat().st_size for item in folder.rglob("*") if item.is_file())


# ---- page_cache: the mechanism --------------------------------------------------------------------


def test_trim_removes_the_pages_used_longest_ago_first(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf")
    cache = tmp_path / "hires"
    _draw_pages(cache, pdf, [1, 2, 3])
    for page, ago in ((1, 300), (2, 100), (3, 200)):
        _used(_stored(cache, pdf, page), ago)
    keep = _bytes(_stored(cache, pdf, 2)) + _bytes(_stored(cache, pdf, 3))

    done = page_cache.trim([cache], keep)

    assert done.pages == 1 and done.held == keep
    assert not _stored(cache, pdf, 1).exists()
    assert (_stored(cache, pdf, 2) / "meta.json").is_file()
    assert (_stored(cache, pdf, 3) / "meta.json").is_file()
    # Nothing renamed away is left behind.
    assert not list((cache / page_cache.pdf_key(pdf)).glob(".p-*"))

    page_cache.trim([cache], keep - 1)
    assert not _stored(cache, pdf, 3).exists() and _stored(cache, pdf, 2).exists()


def test_the_total_is_counted_across_all_the_sets_together(tmp_path: Path) -> None:
    old_pdf, new_pdf = (
        _drawing(tmp_path / "a" / "old.pdf", 2),
        _drawing(tmp_path / "b" / "new.pdf", 2),
    )
    old_cache, new_cache = tmp_path / "a" / "hires", tmp_path / "b" / "hires"
    _draw_pages(old_cache, old_pdf, [1, 2])
    _draw_pages(new_cache, new_pdf, [1, 2])
    for page in (1, 2):
        _used(_stored(old_cache, old_pdf, page), 3600)
        _used(_stored(new_cache, new_pdf, page), 60)

    done = page_cache.trim([old_cache, new_cache], _bytes(new_cache))

    assert done.pages == 2
    assert not list(old_cache.glob("*/p-*"))
    assert len(list(new_cache.glob("*/p-*/meta.json"))) == 2


def test_a_crop_marks_its_page_used(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf", 1)
    cache = tmp_path / "hires"
    assert page_cache.cut(cache, pdf, 1, BOX, 2) is not None
    stored = _stored(cache, pdf, 1)
    _used(stored, 3600)
    assert page_cache.cut(cache, pdf, 1, BOX, 2) is not None
    assert time.time() - (stored / "meta.json").stat().st_mtime < 60


def test_a_page_cleaned_away_mid_read_is_drawn_directly(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf", 1)
    cache = tmp_path / "hires"
    _draw_pages(cache, pdf, [1])
    # The clean-up took the tiles after the reader found the page stored.
    for tile in _stored(cache, pdf, 1).glob("t-*.png"):
        tile.unlink()

    assert page_cache.cut(cache, pdf, 1, BOX, 2) is None
    crop = set_pack.crop_picture(pdf, 1, BOX, cache=cache)
    direct = set_pack.crop_picture(pdf, 1, BOX)
    assert (crop.dpi, crop.box, crop.width, crop.height) == (
        direct.dpi,
        direct.box,
        direct.width,
        direct.height,
    )


def test_a_removed_page_is_drawn_again_on_its_next_crop(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf", 1)
    cache = tmp_path / "hires"
    assert page_cache.cut(cache, pdf, 1, BOX, 2) is not None
    page_cache.trim([cache], 0)
    assert not _stored(cache, pdf, 1).exists()
    assert page_cache.cut(cache, pdf, 1, BOX, 2) is not None
    assert (_stored(cache, pdf, 1) / "meta.json").is_file()


def test_half_written_pages_of_a_render_that_died_are_removed(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf", 1)
    cache = tmp_path / "hires"
    _draw_pages(cache, pdf, [1])
    key = cache / page_cache.pdf_key(pdf)
    dead, running = key / ".p-002.deadbeef", key / ".p-003.cafef00d"
    for folder in (dead, running):
        folder.mkdir()
        (folder / "t-000-000.png").write_bytes(b"x" * 100)
    old = time.time() - 7200
    os.utime(dead, (old, old))

    done = page_cache.trim([cache], 10**12)

    assert done.pages == 1 and not dead.exists()
    assert running.exists() and _stored(cache, pdf, 1).exists()


def test_remove_drops_a_whole_cache(tmp_path: Path) -> None:
    pdf = _drawing(tmp_path / "set.pdf", 2)
    cache = tmp_path / "pack" / "hires"
    _draw_pages(cache, pdf, [1, 2])
    done = page_cache.remove(cache)
    assert done.pages == 2 and done.freed > 0
    assert not cache.exists() and not list(cache.parent.iterdir())
    assert page_cache.remove(cache).pages == 0


# ---- set_pack.trim_hires: the policy --------------------------------------------------------------


def _pack_cache(chat_root: Path, key: str, ago: float, pages: int = 2) -> Path:
    cache = chat_root / "set-packs" / key / set_pack.HIRES
    pdf = _drawing(chat_root / "pdfs" / f"{key}.pdf", pages)
    _draw_pages(cache, pdf, list(range(1, pages + 1)))
    for stored in cache.glob("*/p-*"):
        _used(stored, ago)
    when = time.time() - ago
    for folder in (cache, *cache.glob("*")):
        os.utime(folder, (when, when))
    return cache


def test_a_set_no_chat_uses_loses_its_cache(tmp_path: Path) -> None:
    live = _pack_cache(tmp_path, "live", 7200)
    gone = _pack_cache(tmp_path, "gone", 7200)

    done = set_pack.trim_hires(tmp_path, frozenset({"live"}), set_bytes=10**12, total_bytes=10**12)

    assert done.pages == 2
    assert not gone.exists() and (tmp_path / "set-packs" / "gone").is_dir()
    assert len(list(live.glob("*/p-*/meta.json"))) == 2


def test_an_unused_set_read_a_moment_ago_or_unknown_users_keep_the_cache(tmp_path: Path) -> None:
    recent = _pack_cache(tmp_path, "recent", 30)
    old = _pack_cache(tmp_path, "old", 7200)
    # Who uses which set could not be read: no set's cache is dropped.
    set_pack.trim_hires(tmp_path, None, set_bytes=10**12, total_bytes=10**12)
    assert recent.exists() and old.exists()
    # A set used within the grace period is kept even when no chat points at it.
    set_pack.trim_hires(tmp_path, frozenset(), set_bytes=10**12, total_bytes=10**12)
    assert recent.exists() and not old.exists()


def test_a_new_set_whose_first_page_is_being_drawn_is_not_taken_for_a_gone_one(
    tmp_path: Path,
) -> None:
    pdf = _drawing(tmp_path / "pdfs" / "new.pdf", 1)
    key = tmp_path / "set-packs" / "new" / set_pack.HIRES / page_cache.pdf_key(pdf)
    (key / ".p-001.0123abcd").mkdir(parents=True)  # the first render, still running
    set_pack.trim_hires(tmp_path, frozenset(), set_bytes=10**12, total_bytes=10**12)
    assert (key / ".p-001.0123abcd").is_dir()


def test_each_set_is_capped_then_all_sets_together(tmp_path: Path) -> None:
    first = _pack_cache(tmp_path, "first", 7200, pages=3)
    second = _pack_cache(tmp_path, "second", 60, pages=3)
    local = tmp_path / "chat-1" / "set-local" / set_pack.HIRES
    _draw_pages(local, _drawing(tmp_path / "pdfs" / "local.pdf", 1), [1])
    one_page = max(_bytes(page) for page in second.glob("*/p-*"))

    # Per set: two pages' room each, so every set keeps its two most recently used pages.
    set_pack.trim_hires(
        tmp_path,
        frozenset({"first", "second"}),
        set_bytes=2 * one_page + 1000,
        total_bytes=10**12,
    )
    assert len(list(first.glob("*/p-*"))) == 2
    assert len(list(second.glob("*/p-*"))) == 2
    assert len(list(local.glob("*/p-*"))) == 1

    # In all: the oldest set's pages go first.
    held = _bytes(second) + _bytes(local)
    done = set_pack.trim_hires(
        tmp_path, frozenset({"first", "second"}), set_bytes=10**12, total_bytes=held
    )
    assert not list(first.glob("*/p-*"))
    assert len(list(second.glob("*/p-*"))) == 2 and done.held <= held


# ---- chat_service: who uses which set, and when the clean-up runs --------------------------------


def _listing(name: str) -> list[dict[str, Any]]:
    return [{"name": f"{name}.pdf", "sha256": name * 8, "pages": 2, "kind": "the vendor's"}]


def test_the_sets_in_use_are_read_from_the_chats_and_plan_checks(tmp_path: Path) -> None:
    (tmp_path / "chat-1").mkdir()
    (tmp_path / "chat-1" / "uploads.json").write_text(json.dumps(_listing("a")))
    (tmp_path / "chat-2").mkdir()  # no PDFs copied in yet
    plan = tmp_path / chat_service.PLAN_CHECK_FOLDER / "package-1"
    plan.mkdir(parents=True)
    (plan / "uploads.json").write_text(json.dumps(_listing("b")))

    keys = chat_service.live_pack_keys(tmp_path, set_pack.pack_key)

    assert keys == frozenset({set_pack.pack_key(_listing("a")), set_pack.pack_key(_listing("b"))})
    # The keys are the pack folders' names.
    assert set_pack.pack_folder(tmp_path, _listing("a")).name in keys

    (tmp_path / "chat-2" / "uploads.json").write_text('[{"name": "half')
    assert chat_service.live_pack_keys(tmp_path, set_pack.pack_key) is None


def test_the_clean_up_keeps_the_set_a_chat_uses(tmp_path: Path) -> None:
    listing = _listing("a")
    (tmp_path / "chat-1").mkdir()
    (tmp_path / "chat-1" / "uploads.json").write_text(json.dumps(listing))
    used = _pack_cache(tmp_path, set_pack.pack_key(listing), 7200)
    gone = _pack_cache(tmp_path, "0" * 32, 7200)

    chat_service.trim_page_caches(tmp_path, set_mb=300, total_mb=1000)

    assert used.exists() and not gone.exists()


def test_the_clean_up_starts_in_the_background_at_most_once_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(chat_service, "_trim_started", None)
    monkeypatch.setattr(
        chat_service,
        "trim_page_caches",
        lambda root, *, set_mb, total_mb: ran.append((root, set_mb, total_mb)),
    )

    first = chat_service.start_page_cache_trim(tmp_path, set_mb=300, total_mb=1000)
    assert first is not None
    first.join(5)
    assert chat_service.start_page_cache_trim(tmp_path, set_mb=300, total_mb=1000) is None
    assert ran == [(tmp_path, 300, 1000)]


def test_every_message_ends_by_starting_the_clean_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[tuple[Path, int, int]] = []
    monkeypatch.setattr(
        chat_service,
        "start_page_cache_trim",
        lambda root, *, set_mb, total_mb: started.append((root, set_mb, total_mb)),
    )

    def fails(*_: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(chat_service, "_run_message", fails)
    sized: list[tuple[int, int]] = []
    monkeypatch.setattr(
        chat_service,
        "configure_page_cache",
        lambda *, workers, memory_mb: sized.append((workers, memory_mb)),
    )
    released: list[object] = []
    job = SimpleNamespace(
        chat_root=tmp_path,
        page_cache_set_mb=50,
        page_cache_total_mb=500,
        page_cache_workers=2,
        page_cache_memory_mb=64,
        registry=SimpleNamespace(release=released.append),
    )
    run = SimpleNamespace(
        feed=SimpleNamespace(publish=lambda *_: None, close=lambda: None),
        checkpoint=None,
    )

    chat_service.run_message(cast(Any, job), cast(Any, run))

    assert released == [run]
    assert sized == [(2, 64)]
    assert started == [(tmp_path, 50, 500)]


def test_the_cache_sizes_are_known_settings_even_in_the_env_file(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "GV_DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv\n"
        "GV_ASSISTANT_PAGE_CACHE_SET_MB=200\n"
        "GV_ASSISTANT_PAGE_CACHE_TOTAL_MB=800\n"
        "GV_ASSISTANT_PAGE_CACHE_WORKERS=2\n"
        "GV_ASSISTANT_PAGE_CACHE_MEMORY_MB=64\n"
    )
    settings = Settings(_env_file=env)  # type: ignore[call-arg]
    assert (settings.assistant_page_cache_set_mb, settings.assistant_page_cache_total_mb) == (
        200,
        800,
    )
    assert (settings.assistant_page_cache_workers, settings.assistant_page_cache_memory_mb) == (
        2,
        64,
    )
    defaults = Settings(_env_file=None, database_url="postgresql+psycopg://gv:gv@h/gv")  # type: ignore[call-arg]
    assert (defaults.assistant_page_cache_set_mb, defaults.assistant_page_cache_total_mb) == (
        300,
        1000,
    )


def test_a_size_out_of_range_is_refused_not_quietly_clamped(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "GV_DATABASE_URL=postgresql+psycopg://gv:gv@localhost:5433/gv\n"
        "GV_ASSISTANT_PAGE_CACHE_WORKERS=9\n"
    )
    with pytest.raises(ValidationError, match="assistant_page_cache_workers"):
        Settings(_env_file=env)  # type: ignore[call-arg]


def test_the_sizes_from_settings_beat_the_environment_and_resize_the_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Start unconfigured: a chat message run earlier in this process (another test) has already
    # handed the cache its sizes, which beat the environment (the CI failure: 3, not 5).
    for name in ("_set_workers", "_set_memory_mb", "_pool"):
        monkeypatch.setattr(page_cache, name, None)
    monkeypatch.setattr(page_cache, "_pool_size", 0)
    monkeypatch.setenv("GV_ASSISTANT_PAGE_CACHE_WORKERS", "5")
    monkeypatch.setenv("GV_ASSISTANT_PAGE_CACHE_MEMORY_MB", "999")
    assert page_cache._workers() == 5

    page_cache.configure(workers=2, memory_mb=32)
    assert (page_cache._workers(), page_cache._memory_bytes()) == (2, 32_000_000)
    first = page_cache._executor()
    assert page_cache._pool_size == 2

    page_cache.configure(workers=2, memory_mb=64)
    assert page_cache._executor() is first  # same size: the pool is kept

    page_cache.configure(workers=4, memory_mb=64)
    second = page_cache._executor()
    assert second is not first and page_cache._pool_size == 4
    assert second.submit(lambda: 7).result(5) == 7
    second.shutdown(wait=True)
