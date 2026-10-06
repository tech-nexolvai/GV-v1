"""Opt-in, content-free timing records for reading-pipeline measurements.

The recorder is observational: its clock, serialization, and local file failures never affect the
operation being measured. A configured path is intended for a local measurement run, not as a
database or product audit record.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

__all__ = ["TimingRecorder", "timing_recorder_from_environment"]


class TimingRecorder:
    """Append small timing rows to a local JSONL file and mirror them to the worker log."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()

    @contextmanager
    def measure(
        self, operation: str, *, run_id: str, page_index: int | None = None
    ) -> Iterator[None]:
        """Measure an operation and preserve both its return/exception behavior and result."""
        started_at = datetime.now(UTC)
        started_ns = time.perf_counter_ns()
        status = "ok"
        try:
            yield
        except BaseException:
            status = "error"
            raise
        finally:
            elapsed_ms = (time.perf_counter_ns() - started_ns) / 1_000_000
            self.record(
                operation=operation,
                run_id=run_id,
                page_index=page_index,
                started_at=started_at.isoformat(),
                elapsed_ms=elapsed_ms,
                status=status,
            )

    def record(
        self,
        *,
        operation: str,
        run_id: str,
        page_index: int | None,
        started_at: str,
        elapsed_ms: float,
        status: str,
    ) -> None:
        """Best-effort write of a schema-limited row; never raise into measured work."""
        row: dict[str, Any] = {
            "operation": operation,
            "run_id": run_id,
            "page_index": page_index,
            "started_at": started_at,
            "elapsed_ms": max(0.0, elapsed_ms),
            "status": status,
        }
        try:
            encoded = json.dumps(row, separators=(",", ":"), sort_keys=True)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self._lock, self.path.open("a", encoding="utf-8") as stream:
                    stream.write(encoded + "\n")
                    stream.flush()
            print(f"TIMING {encoded}", flush=True)
        except Exception:  # noqa: BLE001 - measurement must never change workflow behavior
            return


def timing_recorder_from_environment() -> TimingRecorder | None:
    """Enable local timing output only when the operator names its scratch JSONL path."""
    stated = os.environ.get("GV_TIMING_RECORD_PATH", "").strip()
    if not stated:
        return None
    return TimingRecorder(Path(stated).expanduser())
