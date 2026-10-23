"""Code's free pairing estimate for the pre-flight report, worked out once in the background. $0.

With a separate architect's file, code pairs each vendor drawing by its own ranking before any
money is spent; the AI page matcher (paid) is asked only after Run (`page_match.py`). Building the
work list takes seconds to half a minute on a big set (about 25 s on a 188-page upload), too slow
for the Run popover's request, so it runs once in a background thread and its answer is kept in
the set pack (`preflight_pairs.json`) with a fingerprint of the files it was made from: the
popover shows "working out the pairs" meanwhile and asks again.

Each vendor page (with a vendor drawing) is:

- **paired**: code's own clear pick, or a saved matcher answer that agrees (`pairing.agree`);
- **to ask**: some drawing on it has candidates the matcher will be shown, no saved answer;
- **none possible**: code rules out every architect drawing for every drawing on it, or there is
  none to rule in: no matcher answer can pair it (certain).

`confirmed`: no drawing waits for a matcher answer, so the numbers are final, not an estimate.

Loaded by name (`importlib`): the work list reaches drawing code the API never imports
(`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from app.review.assistant.preflight import PairEstimate

__all__ = ["FILE", "INPUTS", "cached", "estimate", "estimate_from", "start"]

_log = logging.getLogger(__name__)

FILE: Final = "preflight_pairs.json"
#: The pack's files the estimate depends on (a change in any makes it again).
INPUTS: Final = (
    "page_map.json",
    "dims.json",
    "scales.json",
    "notes.json",
    "annotations.json",
    "page_match.json",
    "plan_answers.jsonl",
    "set_index.json",
    "architect_index.json",
    "status.json",
)
_RUNNING: set[str] = set()
_LOCK = threading.Lock()


def fingerprint(folder: Path) -> str:
    parts = []
    for name in INPUTS:
        try:
            stat = (folder / name).stat()
        except OSError:
            parts.append(f"{name}:-")
            continue
        parts.append(f"{name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def estimate_from(work: Any, saved_pending: set[str]) -> PairEstimate:
    """The estimate from a work list (`worklist.build_worklist`) and the vendor drawings (refs)
    the matcher would still be asked about (`page_match.to_ask`)."""
    from app.review.assistant.full_check_plan import CrossPairs

    cross = CrossPairs.of(work)
    main = getattr(work, "file", None)
    asked_pages: set[int] = set()
    for pair in getattr(work, "across", ()):
        if pair.vendor.file != main:
            continue
        if pair.asked and pair.vendor.ref in saved_pending:
            asked_pages.add(pair.vendor.own_page)
    vendor_pages = set(cross.pairs) | set(cross.unpaired) | asked_pages
    for pair in getattr(work, "across", ()):
        if pair.vendor.file == main:
            vendor_pages.add(pair.vendor.own_page)
    paired = set(cross.pairs)
    to_ask = asked_pages - paired
    none_possible = tuple(sorted(vendor_pages - paired - to_ask))
    return PairEstimate(
        vendor_pages=len(vendor_pages),
        paired=len(paired),
        to_ask=len(to_ask),
        none_possible=none_possible,
        confirmed=not saved_pending,
    )


def estimate(folder: Path, main_file: str | None) -> PairEstimate | None:
    """Code's pairing estimate for the pack in `folder` (builds the work list: slow)."""
    from importlib import import_module

    worklist = import_module("app.review.assistant.simple_check.worklist")
    page_match = import_module("app.review.assistant.page_match")
    work = worklist.build_worklist(folder, main_file)
    if not getattr(work, "across", ()):
        return None
    asked = [pair for pair in work.across if pair.asked]
    pending = {pair.vendor.ref for pair in page_match.to_ask(asked, folder)[0]}
    return estimate_from(work, pending)


def _as_dict(found: PairEstimate | None) -> dict[str, Any] | None:
    if found is None:
        return None
    return {
        "vendor_pages": found.vendor_pages,
        "paired": found.paired,
        "to_ask": found.to_ask,
        "none_possible": list(found.none_possible),
        "confirmed": found.confirmed,
    }


def _of(saved: Mapping[str, Any]) -> PairEstimate:
    return PairEstimate(
        vendor_pages=int(saved.get("vendor_pages") or 0),
        paired=int(saved.get("paired") or 0),
        to_ask=int(saved.get("to_ask") or 0),
        none_possible=tuple(int(p) for p in saved.get("none_possible") or []),
        confirmed=bool(saved.get("confirmed")),
    )


def cached(folder: Path) -> tuple[str, PairEstimate | None]:
    """(`ready` | `stale` | `none`, the kept estimate): `ready` when it was made from the pack's
    files as they are now."""
    try:
        saved = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "none", None
    if not isinstance(saved, Mapping):
        return "none", None
    found = saved.get("estimate")
    estimate_now = _of(found) if isinstance(found, Mapping) else None
    state = "ready" if saved.get("key") == fingerprint(folder) else "stale"
    return state, estimate_now


def _work(folder: Path, main_file: str | None) -> None:
    key = fingerprint(folder)
    try:
        found = estimate(folder, main_file)
    except Exception as failed:  # noqa: BLE001 - the report then says nothing of the pairs
        _log.warning("pre-flight pairing estimate failed: %s", type(failed).__name__)
        found = None
    try:
        temporary = folder / f".{FILE}.{threading.get_ident()}"
        temporary.write_text(
            json.dumps({"key": key, "estimate": _as_dict(found)}), encoding="utf-8"
        )
        temporary.replace(folder / FILE)
    except OSError:
        _log.warning("pre-flight pairing estimate could not be kept")
    finally:
        with _LOCK:
            _RUNNING.discard(str(folder))


def start(folder: Path, main_file: str | None) -> bool:
    """Starts working out the estimate in the background (once per pack at a time). Returns
    whether it is being worked out now."""
    with _LOCK:
        if str(folder) in _RUNNING:
            return True
        _RUNNING.add(str(folder))
    threading.Thread(
        target=_work, args=(folder, main_file), name="gv-preflight-pairs", daemon=True
    ).start()
    return True
