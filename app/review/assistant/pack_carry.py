"""The saved pairing answers carried over a set pack's version bump (2026-10-27).

A set pack's folder is named by its uploads and the pack's version (`set_pack.pack_key`), so a
code change that bumps `PACK_VERSION` builds a new pack for the very same PDFs. The paid AI answers
the pack keeps were then lost and asked again: the set index's page reads (`set_index.json`), the
page matcher's answers (`page_match.json`) and the devil's advocate's blind picks
(`blind_picks.json`). Asked again, the AI may answer differently: on a real upload, three vendor
pages that had paired went "pairing unsure" after a bump, though not one byte the AI is shown had
changed. So when a pack is built, the answers of the newest earlier version's pack of the same
uploads are carried over, but only where what the AI was shown is provably the same:

- nothing at all unless every drawings-only copy (`<name>_drawings_only.pdf`, the PDFs every
  picture is drawn from) is byte for byte the same in both packs;
- a set index page read only when its question is the same (`set_index.PROMPT_VERSION`) and the
  page's picture, its printed text, its role and its size are the same in both packs;
- the page matcher's answers only when every page picture, the annotations (GV's typed titles)
  and the page map's pages are the same; each answer is still checked against today's candidate
  list before it is used (`page_match._still_good`), so a drawing whose candidates changed is
  asked again;
- the blind picks under the same picture check as the matcher's answers: each is keyed by the
  labels and crop areas it was shown (`pick_cache.pick_key`), not by the pixels.

A real drawing change still asks again: a changed upload has another pack name (no earlier pack
is found), and a changed picture or text fails the checks above. Nothing is paired that code's
agreement rule would not pair (the answers are the ones it already judged), so no new red can come
from this; it only stops a version bump from re-rolling answers, and saves their cost.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final

__all__ = ["CARRIED_FROM", "LOOK_BACK", "CarryReport", "carry_over", "earlier_pack"]

_log = logging.getLogger(__name__)

#: How many earlier pack versions are looked for.
LOOK_BACK: Final = 5
#: Set on a carried set index entry: the earlier pack's name.
CARRIED_FROM: Final = "carried_from"
_INDEX_FILE: Final = "set_index.json"
_MATCH_FILE: Final = "page_match.json"
_PICKS_FILE: Final = "blind_picks.json"


@dataclass(slots=True)
class CarryReport:
    """What was carried over, from which earlier pack (`None`: none found or none usable)."""

    source: str | None = None
    index_pages: int = 0
    match_answers: int = 0
    picks: int = 0
    skipped: list[str] = field(default_factory=list)
    """Why a whole file was not carried."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.carry.{os.getpid()}")
    temporary.write_text(json.dumps(value, indent=1, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def earlier_pack(folder: Path, listing: Sequence[Mapping[str, Any]]) -> Path | None:
    """The newest earlier version's pack of the same uploads that was finished (`ready`), next
    to `folder`; `None` when there is none."""
    pack = import_module("app.review.assistant.set_pack")
    for version in range(pack.PACK_VERSION - 1, pack.PACK_VERSION - 1 - LOOK_BACK, -1):
        if version < 1:
            break
        other: Path = folder.parent / str(pack.pack_key(listing, version=version))
        if other == folder or not other.is_dir():
            continue
        if pack.read_status(other).get("state") == "ready":
            return other
    return None


def _same_drawings(old: Path, new: Path, names: Sequence[str]) -> bool:
    pack = import_module("app.review.assistant.set_pack")
    for name in names:
        file = f"{pack.stem(name)}_drawings_only.pdf"
        one, other = _digest(old / file), _digest(new / file)
        if one is None or one != other:
            return False
    return True


def _picture(folder: Path, file: str, page: int) -> str | None:
    pack = import_module("app.review.assistant.set_pack")
    return _digest(folder / "pages" / pack.stem(file) / f"p-{page:03d}.png")


def _same_pictures(old: Path, new: Path) -> bool:
    pages_old, pages_new = old / "pages", new / "pages"
    if not pages_old.is_dir() or not pages_new.is_dir():
        return False
    found_old = sorted(p.relative_to(pages_old) for p in pages_old.rglob("*.png"))
    found_new = sorted(p.relative_to(pages_new) for p in pages_new.rglob("*.png"))
    if not found_old or found_old != found_new:
        return False
    return all(_digest(pages_old / p) == _digest(pages_new / p) for p in found_old)


def _same_map(old: Path, new: Path) -> bool:
    one, other = _read_json(old / "page_map.json"), _read_json(new / "page_map.json")
    if not isinstance(one, Mapping) or not isinstance(other, Mapping):
        return False
    return all(one.get(k) == other.get(k) for k in ("files", "upload_roles", "pages"))


def _carry_index(old: Path, new: Path, source: str) -> int:
    """The earlier pack's page reads whose page shows the AI exactly the same, into `new`'s set
    index (a page `new` already has is kept)."""
    index = import_module("app.review.assistant.set_index")
    saved = index.load(old)
    if not saved:
        return 0
    now = index.load(new)
    then_pages = {(p.file, p.page): p for p in index.pages_of(old)}
    carried = 0
    for page in index.pages_of(new):
        key = f"{page.file}#{page.page}"
        entry = saved.get(key)
        if key in now or entry is None or entry.get("prompt_version") != index.PROMPT_VERSION:
            continue
        before = then_pages.get((page.file, page.page))
        if before is None or (before.role, before.shown, before.printed, before.size) != (
            page.role,
            page.shown,
            page.printed,
            page.size,
        ):
            continue
        picture = _picture(new, page.file, page.page)
        if picture is None or picture != _picture(old, page.file, page.page):
            continue
        now[key] = {**entry, CARRIED_FROM: source}
        carried += 1
    if carried:
        _write_json(new / _INDEX_FILE, {"version": 1, "pages": now})
    return carried


def _carry_matches(old: Path, new: Path) -> int:
    saved = _read_json(old / _MATCH_FILE)
    answers = saved.get("answers") if isinstance(saved, Mapping) else None
    if not isinstance(answers, Mapping) or not answers:
        return 0
    found = _read_json(new / _MATCH_FILE)
    now = dict(found.get("answers") or {}) if isinstance(found, Mapping) else {}
    added = {str(k): dict(v) for k, v in answers.items() if k not in now and isinstance(v, Mapping)}
    if added:
        _write_json(new / _MATCH_FILE, {"version": 1, "answers": {**added, **now}})
    return len(added)


def _carry_picks(old: Path, new: Path) -> int:
    saved = _read_json(old / _PICKS_FILE)
    if not isinstance(saved, Mapping) or not saved:
        return 0
    found = _read_json(new / _PICKS_FILE)
    now = dict(found) if isinstance(found, Mapping) else {}
    added = {str(k): v for k, v in saved.items() if k not in now and isinstance(v, Mapping)}
    if added:
        _write_json(new / _PICKS_FILE, {**added, **now})
    return len(added)


def carry_over(folder: Path, listing: Sequence[Mapping[str, Any]]) -> CarryReport:
    """Carry the earlier version's saved answers into the pack `folder` (see the module). Never
    raises: a pack without them only asks again."""
    report = CarryReport()
    try:
        old = earlier_pack(folder, listing)
        if old is None:
            return report
        names = [str(item.get("name")) for item in listing]
        if not _same_drawings(old, folder, names):
            report.skipped.append("the drawings-only copies differ")
            return report
        report.source = old.name
        report.index_pages = _carry_index(old, folder, old.name)
        if _same_pictures(old, folder) and _same_map(old, folder):
            # The blind picks' key holds the labels and crop areas, not the pixels: they are
            # carried only when every page picture is byte for byte the same (review, 27 Oct).
            report.picks = _carry_picks(old, folder)
            same_notes = _digest(old / "annotations.json") == _digest(folder / "annotations.json")
            if same_notes:
                report.match_answers = _carry_matches(old, folder)
            else:
                report.skipped.append("the annotations differ")
        else:
            report.skipped.append("the page pictures or the page map differ")
    except Exception:  # noqa: BLE001 - the pack works without them: they are asked again
        _log.exception("set pack: the saved answers could not be carried over")
        return report
    _log.info(
        "set pack: carried %d index pages, %d matcher answers, %d blind picks from %s",
        report.index_pages,
        report.match_answers,
        report.picks,
        report.source,
    )
    return report
