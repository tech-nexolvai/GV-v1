"""The database pipeline's front door: every stage reads the drawing's working copy (#1750).

The chat already reads each upload as its standard working copy (`extraction/working_copy.py`,
#1724: rotation baked into the page, the visible area at the corner, the user unit applied). The
pipeline's stages read the stored original, and each reader had to undo the page's own rotation,
crop offset and unit itself. This gives the pipeline the same front door:

- **Made once, at intake.** The `ingest` stage, the first to open a revision's files, makes each
  file's working copy after its digest is checked (`take_in`), and stores it beside the original
  with a small record of what was done. A later stage, worker or retry reads the stored copy; it is
  never made per reader. A stage that runs before any intake (a revision ingested before this
  existed, or a stage called directly) makes it then, the same way, once.
- **Every reader reads it** (`reading_copy`): the verified original's bytes go in, the working
  copy comes out. A file already in the standard shape (the front door changes nothing) has no
  stored copy and comes back as its own bytes, so it reads exactly as before.
- **The original is never changed.** Its digest stays the authority for the file; the working copy
  is checked against the digest its record names.

Keys are derived from the original's key (`working_key`, `record_key`), so retention can remove a
working copy with its original without a lookup (`app/retention/policy.py`).

**Light on purpose.** The front door itself (pikepdf, PDFium) is loaded by name when a copy is
made, so a module that only reads stored copies does not reach the PDF code.

Source: issue #1750 item 1 · Verification: `tests/workflow/test_working_copies.py`
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib import import_module
from io import BytesIO
from typing import Any, Final

from storage.hashing import ArtifactCorrupt
from storage.store import ArtifactConflict, ArtifactStore

__all__ = [
    "Intake",
    "reading_copy",
    "record_key",
    "take_in",
    "working_copy_keys",
    "working_key",
]

#: Where working copies live, apart from the uploads (`documents/...`).
PREFIX: Final = "working-copies"
_PDF: Final = "application/pdf"
_JSON: Final = "application/json"


@dataclass(frozen=True, slots=True)
class Intake:
    """What the front door did to one stored file (its record)."""

    changed: bool
    """Whether the working copy differs from the original (only then is a copy stored)."""
    working_sha256: str | None = None
    changes: tuple[str, ...] = ()
    """What was done, in plain words (`WorkingCopy.changes`)."""
    pages_not_read: tuple[tuple[int, str], ...] = ()
    """Pages (from 1) left blank because they could not be put in shape exactly, each with why."""
    refused: str | None = None
    """Why the file could not be opened to make its copy; its readers refuse it with their own
    reasons, as they always did."""

    def as_json(self) -> dict[str, Any]:
        return {
            "changed": self.changed,
            "working_sha256": self.working_sha256,
            "changes": list(self.changes),
            "pages_not_read": [[page, why] for page, why in self.pages_not_read],
            "refused": self.refused,
        }


def _version() -> int:
    version: int = import_module("extraction.working_copy").WORKING_COPY_VERSION
    return version


def working_key(key: str, version: int | None = None) -> str:
    """Where the working copy of the original stored at `key` lives (only when it changed)."""
    return f"{PREFIX}/v{_version() if version is None else version}/{key}"


def record_key(key: str, version: int | None = None) -> str:
    """Where the record of what the front door did to the original at `key` lives."""
    return f"{working_key(key, version)}.json"


def working_copy_keys(key: str) -> tuple[str, ...]:
    """Every key a working copy of the original at `key` may have used, any front-door version:
    what retention removes with the original. Each record comes before its copy, so a removal cut
    short never leaves a record naming a copy that is gone (a copy left without its record is made
    again, byte for byte, when it is next read)."""
    return tuple(
        found
        for version in range(1, _version() + 1)
        for found in (record_key(key, version), working_key(key, version))
    )


def _stored(store: ArtifactStore, key: str) -> Intake | None:
    found = record_key(key)
    if not store.exists(found):
        return None
    with store.get(found) as stored:
        raw = json.loads(stored.read().decode("utf-8"))
    return Intake(
        changed=bool(raw["changed"]),
        working_sha256=raw.get("working_sha256"),
        changes=tuple(str(line) for line in raw.get("changes") or ()),
        pages_not_read=tuple(
            (int(page), str(why)) for page, why in raw.get("pages_not_read") or ()
        ),
        refused=raw.get("refused"),
    )


def take_in(store: ArtifactStore, key: str, data: bytes) -> Intake:
    """The front door's record for the original stored at `key` (whose verified bytes are `data`),
    made and stored the first time it is asked for, read back every time after.

    The copy is stored before its record, so a record always names a copy that is there. Two
    workers making the same copy at once write the same bytes (the front door is deterministic);
    if their records ever differ, the one stored first wins and is the one returned."""
    found = _stored(store, key)
    if found is not None:
        return found
    front_door = import_module("extraction.working_copy")
    try:
        copy = front_door.working_copy(data)
    except front_door.WorkingCopyRefused as refused:
        intake = Intake(changed=False, refused=str(refused))
    else:
        intake = Intake(
            changed=copy.changed,
            working_sha256=copy.sha256 if copy.changed else None,
            changes=tuple(copy.changes),
            pages_not_read=tuple(copy.not_read),
        )
        if copy.changed:
            store.put(working_key(key), BytesIO(copy.data), content_type=_PDF)
    record = json.dumps(intake.as_json(), sort_keys=True, separators=(",", ":")).encode()
    try:
        store.put(record_key(key), BytesIO(record), content_type=_JSON)
    except ArtifactConflict:
        stored = _stored(store, key)
        if stored is None:
            raise
        return stored
    return intake


def reading_copy(store: ArtifactStore, key: str, data: bytes) -> bytes:
    """What every reader reads of the original stored at `key`, whose verified bytes are `data`:
    its working copy, or `data` itself when the front door changed nothing (or could not open it).

    Raises `ArtifactCorrupt` when the stored copy is not the one its record names, as a corrupt
    original does: a stage must never read a copy nobody made."""
    intake = take_in(store, key, data)
    if not intake.changed:
        return data
    with store.get(working_key(key)) as stored:
        working = stored.read()
    if hashlib.sha256(working).hexdigest() != intake.working_sha256:
        raise ArtifactCorrupt(
            "a drawing's working copy does not match the digest recorded when it was made"
        )
    return working
