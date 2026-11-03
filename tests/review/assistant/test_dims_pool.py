"""The set pack's labels read a page at a time in worker processes (#1872).

A 188-page upload ran out of the labels step's 40 minutes before its drawn pass started; and a
page that crashes the PDF engine must cost that page, never the set. These tests hold:

- the pool gives exactly the document one process gives (every page, the same labels, in order);
- a page that crashes its worker twice says "page N could not be read: the PDF reader crashed"
  and every other page is read;
- finished pages are kept, so a stopped run resumes without reading them again.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Mapping
from importlib import import_module
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
        {"pictures": 0, "drawn": 0, "ocr": 0, "sweep": 0},
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
        {"pictures": 0, "drawn": 60, "ocr": 0, "sweep": 0},
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
        {"pictures": 0, "drawn": 60, "ocr": 0, "sweep": 0},
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
    # The cores are shared out at the threads each worker's libraries may use.
    monkeypatch.setattr(os, "cpu_count", lambda: 10)
    monkeypatch.delenv(dims_pool.THREADS_SETTING, raising=False)
    assert dims_pool.threads() == dims_pool.THREADS_PER_WORKER == 1
    assert dims_pool.workers() == min(dims_pool.MOST_WORKERS, 10 - dims_pool.SPARE_CORES)
    monkeypatch.setenv(dims_pool.THREADS_SETTING, "4")
    assert dims_pool.workers() == 2
    monkeypatch.setenv(dims_pool.THREADS_SETTING, "0")  # the libraries choose: as before
    assert dims_pool.threads() == 0
    assert dims_pool.workers() == min(dims_pool.MOST_WORKERS, 10 - dims_pool.SPARE_CORES)
    monkeypatch.setattr(os, "cpu_count", lambda: 2)
    assert dims_pool.workers() == 1


def library_threads(task: dims_pool.PageTask, given: Mapping[str, float]) -> Any:
    """What a worker's libraries were given, instead of the page's labels."""
    try:
        rapid = import_module("rapidocr_onnxruntime").RapidOCR
    except ImportError:  # the OCR extra is not installed
        rapid = None
    return {
        "page": task.number,
        "math": [os.environ.get(name) for name in dims_pool._MATH_THREADS],
        "rapidocr": getattr(rapid, "gv_threads", None),
    }


def _no_math_variables(monkeypatch: Any) -> None:
    """The math libraries' thread variables unset, and as they were after the test."""
    for name in dims_pool._MATH_THREADS:
        monkeypatch.setenv(name, "x")
        monkeypatch.delenv(name)


def _has_rapidocr() -> bool:
    try:
        import_module("rapidocr_onnxruntime")
    except ImportError:
        return False
    return True


def test_each_worker_starts_with_its_libraries_at_one_thread(
    tmp_path: Path, monkeypatch: Any
) -> None:
    monkeypatch.delenv(dims_pool.THREADS_SETTING, raising=False)
    _no_math_variables(monkeypatch)
    pdf = _set(tmp_path, 2)
    tasks = [dims_pool.PageTask("set.pdf", str(pdf), n) for n in (1, 2)]
    results: dict[int, Any] = {}
    table = dims_pool.Table(tasks)
    dims_pool.read_pages(
        tasks,
        {},
        library_threads,
        results.__setitem__,
        unreadable=lambda task, why: why,
        count=2,
        table=table,
    )
    for index in (0, 1):
        assert results[index] == {
            "page": index + 1,
            "math": ["1"] * len(dims_pool._MATH_THREADS),
            "rapidocr": 1 if _has_rapidocr() else None,
        }
    # This process's own environment is as it was.
    assert all(name not in os.environ for name in dims_pool._MATH_THREADS)
    # Each page's seconds are in the reading table.
    rows = table.snapshot()["pages"]
    assert all(row["state"] == dims_pool.READY and row["seconds"] >= 0 for row in rows)


def test_threads_0_leaves_the_libraries_as_they_choose(tmp_path: Path, monkeypatch: Any) -> None:
    monkeypatch.setenv(dims_pool.THREADS_SETTING, "0")
    _no_math_variables(monkeypatch)
    pdf = _set(tmp_path, 1)
    results: dict[int, Any] = {}
    dims_pool.read_pages(
        [dims_pool.PageTask("set.pdf", str(pdf), 1)],
        {},
        library_threads,
        results.__setitem__,
        unreadable=lambda task, why: why,
        count=2,
    )
    assert results[0]["math"] == [None] * len(dims_pool._MATH_THREADS)
    assert results[0]["rapidocr"] is None


def test_rapidocr_is_made_with_the_threads_asked(monkeypatch: Any) -> None:
    made: list[dict[str, Any]] = []

    class Fake:
        def __init__(self, config_path: str | None = None, **kwargs: Any) -> None:
            made.append(kwargs)

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = Fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)
    # OpenCV on macOS runs on Grand Central Dispatch, whose `getNumThreads` always says the cores.
    opencv: list[int] = []
    monkeypatch.setitem(sys.modules, "cv2", types.SimpleNamespace(setNumThreads=opencv.append))
    _no_math_variables(monkeypatch)
    dims_pool.limit_threads(2)
    fake.RapidOCR()
    dims_pool.limit_threads(1)  # asked again: the package's own class, limited once
    fake.RapidOCR(intra_op_num_threads=3)
    dims_pool.limit_threads(0)  # 0: nothing changes
    assert opencv == [2, 1]
    assert made == [
        {"intra_op_num_threads": 2, "inter_op_num_threads": 1},
        {"intra_op_num_threads": 3, "inter_op_num_threads": 1},
    ]
    assert fake.RapidOCR.__mro__[1] is Fake
    assert os.environ["VECLIB_MAXIMUM_THREADS"] == "1"
