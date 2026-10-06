from __future__ import annotations

import json
from pathlib import Path

import pytest

from workflow.stages import DatabaseStages
from workflow.timing import TimingRecorder


def test_records_success_and_failure_without_changing_operation_result(tmp_path: Path) -> None:
    recorder = TimingRecorder(tmp_path / "timings.jsonl")

    with recorder.measure("read.page.text", run_id="run-1", page_index=2):
        result = "unchanged"
    assert result == "unchanged"

    with (
        pytest.raises(RuntimeError, match="read failed"),
        recorder.measure("read.page.ocr", run_id="run-1", page_index=2),
    ):
        raise RuntimeError("read failed")

    rows = [json.loads(line) for line in (tmp_path / "timings.jsonl").read_text().splitlines()]
    assert [row["operation"] for row in rows] == ["read.page.text", "read.page.ocr"]
    assert [row["status"] for row in rows] == ["ok", "error"]
    assert all(row["run_id"] == "run-1" for row in rows)
    assert all(row["elapsed_ms"] >= 0 for row in rows)
    assert rows[0]["page_index"] == 2


def test_timing_recording_failure_never_changes_work_result(tmp_path: Path) -> None:
    recorder = TimingRecorder(tmp_path / "missing" / "timings.jsonl")
    with recorder.measure("read.page.text", run_id="run-1", page_index=0):
        result = 42
    assert result == 42


def test_jsonl_and_worker_log_receive_the_same_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    recorder = TimingRecorder(tmp_path / "timings.jsonl")
    with recorder.measure("worker.stage.extract_pages", run_id="run-2", page_index=None):
        pass

    file_row = json.loads((tmp_path / "timings.jsonl").read_text().splitlines()[0])
    log_line = capsys.readouterr().out.strip()
    assert log_line.startswith("TIMING ")
    assert json.loads(log_line.removeprefix("TIMING ")) == file_row


def test_stage_timing_wrapper_preserves_the_stage_result_and_error(tmp_path: Path) -> None:
    stages = object.__new__(DatabaseStages)
    stages._timings = TimingRecorder(tmp_path / "stage.jsonl")

    result = stages._timed_call(
        "extraction.page.vector_text_and_candidates",
        run_id="run-3",
        page_index=4,
        function=lambda: {"candidate_count": 7},
    )
    assert result == {"candidate_count": 7}

    def broken_stage() -> None:
        raise RuntimeError("stage failure")

    with pytest.raises(RuntimeError, match="stage failure"):
        stages._timed_call(
            "extraction.page.full_page_ocr",
            run_id="run-3",
            page_index=4,
            function=broken_stage,
        )

    rows = [json.loads(line) for line in (tmp_path / "stage.jsonl").read_text().splitlines()]
    assert [row["status"] for row in rows] == ["ok", "error"]
    assert all(row["page_index"] == 4 for row in rows)


def test_timing_rows_exclude_document_content(tmp_path: Path) -> None:
    recorder = TimingRecorder(tmp_path / "timings.jsonl")
    with recorder.measure("read.page.text", run_id="run-1", page_index=0):
        pass

    row = json.loads((tmp_path / "timings.jsonl").read_text().splitlines()[0])
    assert set(row) == {"operation", "run_id", "page_index", "started_at", "elapsed_ms", "status"}
