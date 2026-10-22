"""The set pack's labels read a page at a time in worker processes (#1871).

A 188-page upload ran out of the labels step's 40 minutes before its drawn pass started; and a
page that crashes the PDF engine must cost that page, never the set. These tests hold:

- the pool gives exactly the document one process gives (every page, the same labels, in order);
- a page that crashes its worker twice says "page N could not be read: the PDF reader crashed"
  and every other page is read;
- finished pages are kept, so a stopped run resumes without reading them again.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.review.assistant import dim_labels as dl
from app.review.assistant import dims_pool
from tests.review.assistant.test_dim_labels import HALVES, _agreeing, _page_one, _page_two, _pdf

#: The page `crashing_read` crashes on (a worker process dies the way pdfium kills it: a signal).
CRASH_PAGE = 2


def crashing_read(task: dims_pool.PageTask, given: Mapping[str, float]) -> Any:
    """`dim_labels.read_page`, but the worker process dies on `CRASH_PAGE`."""
    if task.number == CRASH_PAGE:
        os.abort()
    return dl.read_page(task, given)


def _set(tmp_path: Path, pages: int) -> Path:
    path = tmp_path / "set.pdf"
    path.write_bytes(_pdf([_page_one() if n % 2 == 0 else _page_two() for n in range(pages)]))
    return path


def _map(pages: int) -> dict[str, Any]:
    return {
        "pages": [{"file": "set.pdf", "page": n, "halves": HALVES} for n in range(1, pages + 1)]
    }


def test_the_pool_reads_what_one_process_reads(tmp_path: Path) -> None:
    pdf = _set(tmp_path, 5)
    alone = dl.build_dims({"set.pdf": pdf}, _map(5), workers=1)
    seen: list[int] = []
    together = dl.build_dims(
        {"set.pdf": pdf},
        _map(5),
        workers=3,
        on_page=lambda document: seen.append(len(document["pages"])),
    )
    assert together == alone
    assert [entry["page"] for entry in together["pages"]] == [1, 2, 3, 4, 5]
    assert sorted(seen)[:5] == [1, 2, 3, 4, 5]  # written as each page finished


def test_a_page_that_crashes_its_worker_twice_is_unreadable_and_the_rest_is_read(
    tmp_path: Path,
) -> None:
    pdf = _set(tmp_path, 4)
    tasks = [dims_pool.PageTask("set.pdf", str(pdf), n, tuple(HALVES)) for n in range(1, 5)]
    results: dict[int, Any] = {}
    dims_pool.read_pages(
        tasks,
        dims_pool.Budgets(0, 0, 0, 0),
        crashing_read,
        results.__setitem__,
        unreadable=dl.unreadable_page,
        count=2,
    )
    assert sorted(results) == [0, 1, 2, 3]
    crashed = results[CRASH_PAGE - 1].entry
    assert crashed["labels"] == [] and crashed["drawn"] == "failed"
    assert crashed["error"] == "page 2 could not be read: the PDF reader crashed"
    alone = dl.build_dims({"set.pdf": pdf}, _map(4), workers=1)
    for index in (0, 2, 3):
        assert results[index].entry == alone["pages"][index]


def test_finished_pages_are_kept_and_a_run_resumes_without_reading_them_again(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.setattr(dl, "drawn_readers", _agreeing)
    pdf = _set(tmp_path, 3)
    keep = tmp_path / "kept"
    first = dl.build_dims({"set.pdf": pdf}, _map(3), keep=keep, drawn_budget_s=60)
    # A page the drawn pass did not reach is never kept (it is read again).
    assert dl.build_dims({"set.pdf": pdf}, _map(3), keep=tmp_path / "none")["pages"]
    assert not list((tmp_path / "none").glob("*.pkl"))
    assert len(list(keep.glob("*.pkl"))) == 3
    calls: list[int] = []
    tasks = [dims_pool.PageTask("set.pdf", str(pdf), n, tuple(HALVES)) for n in range(1, 4)]

    def counting(task: dims_pool.PageTask, given: Mapping[str, float]) -> Any:
        calls.append(task.number)
        return dl.read_page(task, given)

    results: dict[int, Any] = {}
    dims_pool.read_pages(
        tasks,
        dims_pool.Budgets(0, 60, 0, 0),
        counting,
        results.__setitem__,
        unreadable=dl.unreadable_page,
        keep=keep,
        version=f"labels {dl.LABELS_VERSION}",
        count=1,
    )
    assert calls == []  # every page came back from what was kept
    assert [results[i].entry for i in range(3)] == first["pages"]
    # A changed file is read again (the key holds the file's bytes).
    pdf.write_bytes(_pdf([_page_two(), _page_two(), _page_two()]))
    dims_pool.read_pages(
        tasks,
        dims_pool.Budgets(0, 60, 0, 0),
        counting,
        results.__setitem__,
        unreadable=dl.unreadable_page,
        keep=keep,
        version=f"labels {dl.LABELS_VERSION}",
        count=1,
    )
    assert calls == [1, 2, 3]


def test_how_many_workers(monkeypatch: Any) -> None:
    monkeypatch.setenv(dims_pool.WORKERS_SETTING, "3")
    assert dims_pool.workers() == 3
    monkeypatch.delenv(dims_pool.WORKERS_SETTING)
    assert 1 <= dims_pool.workers() <= dims_pool.MOST_WORKERS
