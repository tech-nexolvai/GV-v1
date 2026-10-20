"""The set's proof ledger: what a chat question proved, kept for the set's next full check (#1805).

A reviewer's question in a chat often proves a value the full check could not: the paid round of
2026-10-20 had a chat prove a vendor string with `verify_value` on every part while the full check
left the same string Not compared, and the chat's proof reached nothing. Here every proof a chat
makes is kept in the set pack (`chat_proofs.json`, next to `read_number`'s `readings.json`), where
every chat and full check of the same set finds it:

- **values**: a `verify_value` match, or a `read_number` reading, at a spot (file, page, side, the
  box or label, the value in inches, the route). Only answers code read are kept: a "cannot read"
  or a mismatch proves nothing about a value and is never kept (a mismatch is read again at its
  spot by the full check's own proving step anyway).
- **strings and readings**: each `record_string` / `record_reading` a chat recorded, as the tool
  input it was (code adds a string up again when it replays it; it never trusts the old sum).

**How a full check uses it.** At its start the reading ledger replays the chat's strings and
readings (`by: chat`), so code's sums and cross-view checks see them. The roster's proving step
(`full_check_roster._prove_values`) takes a kept value for a side its own tools could not read
when the kept spot lies at the finding's spot and prints the same inches, and says so on the
finding ("proved in a chat question"). Nothing is decided here: code's proof rules judge the item
exactly as they judge their own readings.

Pure files, no model, no PDF. One lock per process; the file is written whole and replaced.
Verification: `tests/review/assistant/test_set_proofs.py`.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Final

__all__ = [
    "PROOFS_FILE",
    "keep_ledger_entry",
    "keep_value",
    "ledger_input",
    "load",
    "proved_at",
    "value_entry",
]

_log = logging.getLogger(__name__)

PROOFS_FILE: Final = "chat_proofs.json"
_VERSION: Final = 1
#: The ledger keeps at most this many of each kind (the newest).
MAX_VALUES: Final = 2000
MAX_LEDGER: Final = 2000
#: How far (points) a kept spot may reach outside a finding's box and still lie at its spot.
_SLACK_PT: Final = 2.0
_LOCK = threading.Lock()


def _readers() -> int:
    """The readers' version a kept value was proved with (`read_number.READER_VERSION`): a value
    kept by older readers is never used once they change (their own cache does the same)."""
    from app.review.assistant.read_number import READER_VERSION

    return int(READER_VERSION)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _empty() -> dict[str, Any]:
    return {"version": _VERSION, "values": [], "ledger": []}


def load(folder: Path | None) -> dict[str, Any]:
    """The set's kept proofs (empty when there is no pack or no file, or it cannot be read)."""
    if folder is None:
        return _empty()
    try:
        found = json.loads((Path(folder) / PROOFS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(found, Mapping) or found.get("version") != _VERSION:
        return _empty()
    return {
        "version": _VERSION,
        "values": [v for v in found.get("values") or [] if isinstance(v, Mapping)],
        "ledger": [e for e in found.get("ledger") or [] if isinstance(e, Mapping)],
    }


def _write(folder: Path, data: Mapping[str, Any]) -> None:
    path = Path(folder) / PROOFS_FILE
    scratch = path.with_suffix(f".{os.getpid()}.{threading.get_ident()}.partial")
    scratch.write_text(json.dumps(data, indent=1), encoding="utf-8")
    scratch.replace(path)


def _inches(answer: Mapping[str, Any]) -> str | None:
    from app.review.assistant.finding_checks import to_inches

    for key in ("value",):
        raw = answer.get(key)
        if raw is None:
            continue
        try:
            return format(Decimal(str(raw)).normalize(), "f")
        except (InvalidOperation, ValueError):
            pass
    for key in ("read", "text"):
        text = answer.get(key)
        found = to_inches(str(text)) if text else None
        if found is not None:
            return format(found.normalize(), "f")
    return None


def value_entry(proof: Mapping[str, Any], chat: str | None = None) -> dict[str, Any] | None:
    """The ledger entry for one proving tool's answer (`ChatTools.note_proof`), or `None` when it
    proved nothing (cannot read, a mismatch, several spots, no spot)."""
    answer = proof.get("answer") or {}
    tool = proof.get("tool")
    if tool == "verify_value":
        if answer.get("result") != "match":
            return None
        inches = _inches({"read": answer.get("read") or answer.get("value")})
        route = str(answer.get("route") or "")
    elif tool == "read_number":
        if answer.get("outcome") != "read":
            return None
        inches = _inches(answer)
        route = "+".join(str(a) for a in answer.get("agreed") or ()) or "read_number"
    else:
        return None
    box = proof.get("box")
    if tool == "read_number" and answer.get("box"):
        box = answer.get("box")
    if inches is None or not isinstance(box, Sequence) or len(box) != 4:
        return None
    return {
        "tool": tool,
        "file": str(proof.get("file") or ""),
        "page": int(proof.get("page") or 0),
        "side": str(proof.get("side") or "vendor"),
        "box": [round(float(v), 2) for v in box],
        "label": answer.get("label") or proof.get("label"),
        "inches": inches,
        "route": route,
        "readers": _readers(),
        "chat": chat,
        "at": _now(),
    }


def keep_value(folder: Path | None, proof: Mapping[str, Any], chat: str | None = None) -> bool:
    """Keeps a proving tool's answer when it proved a value; True when kept. Never raises."""
    if folder is None:
        return False
    entry = value_entry(proof, chat)
    if entry is None:
        return False
    try:
        with _LOCK:
            data = load(folder)
            same = [
                v
                for v in data["values"]
                if not all(v.get(k) == entry[k] for k in ("file", "page", "side", "box", "inches"))
            ]
            data["values"] = [*same, entry][-MAX_VALUES:]
            _write(folder, data)
    except OSError as failed:
        _log.warning("assistant set proofs: value not kept (%s)", type(failed).__name__)
        return False
    return True


def ledger_input(raw: Mapping[str, Any]) -> dict[str, Any]:
    """A `record_string` / `record_reading` input as kept: without `replaces` (a chat-local id)."""
    given = {k: v for k, v in raw.items() if k != "replaces"}
    return dict(json.loads(json.dumps(given)))


def keep_ledger_entry(
    folder: Path | None,
    tool: str,
    raw: object,
    chat: str | None = None,
    replaced: Mapping[str, Any] | None = None,
) -> bool:
    """Keeps a `record_string` / `record_reading` a chat recorded (its tool input); True when
    kept. `replaced`: the kept input of the entry this one re-reads (`replaces`), dropped, so a
    misread the chat corrected never comes back in a later check. Never raises."""
    if folder is None or tool not in ("record_string", "record_reading"):
        return False
    if not isinstance(raw, Mapping):
        return False
    try:
        entry = {"tool": tool, "input": ledger_input(raw), "chat": chat, "at": _now()}
    except (TypeError, ValueError):
        return False
    gone = [entry["input"], *([dict(replaced)] if replaced is not None else [])]
    try:
        with _LOCK:
            data = load(folder)
            kept = [e for e in data["ledger"] if e.get("input") not in gone]
            data["ledger"] = [*kept, entry][-MAX_LEDGER:]
            _write(folder, data)
    except (OSError, TypeError, ValueError) as failed:
        _log.warning("assistant set proofs: ledger entry not kept (%s)", type(failed).__name__)
        return False
    return True


def proved_at(
    proofs: Mapping[str, Any],
    *,
    file: str,
    page: int,
    side: str,
    box: Sequence[float],
    inches: Decimal,
) -> dict[str, Any] | None:
    """A kept value proof at this spot of these inches, on this file's page and side, by today's
    readers; the newest when several. `None` when none. The kept box (the spot the chat's tool
    read: a `verify_value` match says only that the value is printed somewhere inside it) must
    lie wholly inside `box`, so the value it proved is printed at the finding's own spot."""
    x0, y0, x1, y1 = (float(v) for v in box)
    found = None
    readers = _readers()
    for entry in proofs.get("values") or []:
        if entry.get("file") != file or entry.get("page") != page or entry.get("side") != side:
            continue
        if entry.get("readers") != readers:
            continue
        try:
            if Decimal(str(entry.get("inches"))) != inches:
                continue
            ex0, ey0, ex1, ey1 = (float(v) for v in entry.get("box") or ())
        except (InvalidOperation, TypeError, ValueError):
            continue
        if (
            ex0 >= x0 - _SLACK_PT
            and ey0 >= y0 - _SLACK_PT
            and ex1 <= x1 + _SLACK_PT
            and ey1 <= y1 + _SLACK_PT
        ):
            found = dict(entry)
    return found
