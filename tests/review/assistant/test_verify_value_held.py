"""A big set's code checks read pages without reading the whole file again (#1874), $0.

- **The file held open.** A page is cut from the PDF held open (one per file version), not from a
  fresh copy of the whole file each time: the same one-page bytes as before.
- **Worker processes.** With `use_parse_processes`, a page is parsed in a worker process: the same
  characters and paths as on the caller's thread.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.review.assistant import verify_value as vv
from tests.review.assistant.test_colour_rule import BLUE, _dimension, _sheet


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    vv._PAGE_CACHE.clear()
    vv._SOURCES.clear()
    yield
    vv.use_parse_processes(0)
    vv._PAGE_CACHE.clear()
    vv._SOURCES.clear()


def test_a_page_cut_from_the_file_held_open_is_the_same_page(tmp_path: Path) -> None:
    path = _sheet(tmp_path / "a.pdf", _dimension(100, 300, 200, '24"', BLUE))
    assert vv.vendor_page_at(path, 0) == vv._vendor_page(path.read_bytes(), 0)
    assert vv.vendor_page_at(path, 0) == vv.vendor_page_at(path, 0)
    assert len(vv._SOURCES) == 1  # opened once, kept
    assert vv.combined_sheet_at(path) is vv.combined_sheet(path.read_bytes())


def test_a_page_parsed_in_a_worker_process_reads_the_same(tmp_path: Path) -> None:
    path = _sheet(tmp_path / "b.pdf", _dimension(100, 300, 200, '24"', BLUE))
    here = vv._read_page(path, 0)
    vv._PAGE_CACHE.clear()
    vv.use_parse_processes(1)
    there = vv._read_page(path, 0)
    assert there.chars == here.chars and there.paths == here.paths
    assert there.size == here.size and there.pictures == here.pictures
    assert any(char.text for char in there.chars)
