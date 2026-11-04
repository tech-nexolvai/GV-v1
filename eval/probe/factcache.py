"""The fact cache: read a page once, ask questions of it for nothing (Phase 1).

Today a check does both halves at once. One agent looks at a page and decides, in the same breath,
what the page says and whether anything is wrong with it. So changing a *question* — a new rule, a
different wording, an anti-pattern, another verdict row — costs a full re-read of the drawing, at
about $0.90 a page. That is why nobody iterates on the thing that matters most.

Separating them makes the question side free:

    read this page -> facts                (paid once, cached here)
    ask questions of those facts -> findings   (free, seconds, repeatable)

**The key.** A page's facts are keyed by the content hash of the page image, the extraction
prompt's version, and the model id — exactly the three things that change what was read. The image
hash is the bytes of the pack's own rendered page, so a redrawn sheet gets a fresh key by itself
and a pack rebuilt at a new `PACK_VERSION` reuses everything it can.

**Where it lives.** `~/gv-local/exam/factcache/`, sharded two characters deep like the pack's
`spot_reads/`, which lets parallel writers work without ever rewriting one big file. **Local only:
these facts derive from the client's drawings and must never enter the repository.**

**The limit, stated plainly.** A cached page answers a new question only if somebody already
extracted the fact it needs. A question about something nobody wrote down — the lettering on a
detail that was never read, a pasted view nobody typed out — still needs a paid read. `facts.py`
measures how much of the evidence the saved runs already hold; the report says what that is worth.

The machinery here is the repo's own: the key is built the way `pick_cache.pick_key` builds one
(sorted JSON, the model and the prompt folded in), and the store is sharded the way
`verify_value`'s kept reads are, with the same temp-file-then-replace write.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_ROOT",
    "FACT_VERSION",
    "ROOT_ENV",
    "Entry",
    "key_for",
    "load",
    "page_digest",
    "page_digest_of",
    "root",
    "stats",
    "store",
]

#: The shape of a stored entry. Bump when the fields change, which retires every entry at once.
FACT_VERSION: Final = 1

#: Local only. These facts derive from the client's drawings: never in the repository, never in a
#: pull request.
DEFAULT_ROOT: Final = Path.home() / "gv-local" / "exam" / "factcache"

ROOT_ENV: Final = "GV_PROBE_FACTCACHE"


def root(given: Path | None = None) -> Path:
    """Where the cache lives: the argument, else `GV_PROBE_FACTCACHE`, else the default."""
    if given is not None:
        return Path(given).expanduser()
    from_env = os.environ.get(ROOT_ENV)
    return Path(from_env).expanduser() if from_env else DEFAULT_ROOT


def page_digest(image: bytes) -> str:
    """The content hash of one page image: sha256 of the rendered bytes, 32 hex."""
    return hashlib.sha256(image).hexdigest()[:32]


def page_digest_of(images: list[bytes]) -> str:
    """The content hash of a crop set, length-framed so two crops cannot be confused for one.

    The same framing `read_spots.crop_hash` uses, for the same reason: without a length in front
    of each part, two different splits of the same bytes hash alike.
    """
    digest = hashlib.sha256()
    for image in images:
        digest.update(str(len(image)).encode("ascii"))
        digest.update(b":")
        digest.update(image)
    return digest.hexdigest()[:32]


def key_for(*, digest: str, prompt_version: str, model: str) -> str:
    """The cache key: the page's content, the extraction prompt's version, and the model.

    Nothing else goes in. The pack version is deliberately out: a pack rebuilt at a new version
    from the same drawing renders the same page bytes, and re-reading it would be paying twice for
    one answer.
    """
    text = json.dumps(
        {
            "fact_version": FACT_VERSION,
            "page": digest,
            "prompt_version": prompt_version,
            "model": model,
        },
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Entry:
    """One page's facts, with everything needed to know what made them."""

    key: str
    facts: dict[str, Any]
    digest: str
    prompt_version: str
    model: str
    asked_at: str
    cost_usd: float = 0.0
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "fact_version": FACT_VERSION,
            "key": self.key,
            "page_digest": self.digest,
            "prompt_version": self.prompt_version,
            "model": self.model,
            "asked_at": self.asked_at,
            "cost_usd": self.cost_usd,
            "note": self.note,
            "facts": self.facts,
        }


def _path(where: Path, key: str) -> Path:
    return where / key[:2] / f"{key}.json"


def load(key: str, *, where: Path | None = None) -> Entry | None:
    """The facts stored under `key`, or None. A damaged or outdated entry reads as a miss."""
    path = _path(root(where), key)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if int(raw.get("fact_version") or 0) != FACT_VERSION:
        return None
    facts = raw.get("facts")
    if not isinstance(facts, dict):
        return None
    return Entry(
        key=str(raw.get("key") or key),
        facts=facts,
        digest=str(raw.get("page_digest") or ""),
        prompt_version=str(raw.get("prompt_version") or ""),
        model=str(raw.get("model") or ""),
        asked_at=str(raw.get("asked_at") or ""),
        cost_usd=float(raw.get("cost_usd") or 0.0),
        note=str(raw.get("note") or ""),
    )


def store(
    facts: dict[str, Any],
    *,
    digest: str,
    prompt_version: str,
    model: str,
    where: Path | None = None,
    cost_usd: float = 0.0,
    note: str = "",
) -> Entry:
    """Keep one page's facts. Written to a temp file and renamed, so a reader never sees a half."""
    key = key_for(digest=digest, prompt_version=prompt_version, model=model)
    entry = Entry(
        key=key,
        facts=facts,
        digest=digest,
        prompt_version=prompt_version,
        model=model,
        asked_at=datetime.now(UTC).isoformat(timespec="seconds"),
        cost_usd=cost_usd,
        note=note,
    )
    path = _path(root(where), key)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            json.dump(entry.as_dict(), out, indent=1, sort_keys=True)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return entry


@dataclass(frozen=True)
class Stats:
    """What the cache holds."""

    entries: int
    bytes: int
    models: dict[str, int]
    prompt_versions: dict[str, int]
    paid_usd: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "entries": self.entries,
            "bytes": self.bytes,
            "models": self.models,
            "prompt_versions": self.prompt_versions,
            "paid_usd": round(self.paid_usd, 2),
        }


def stats(where: Path | None = None) -> Stats:
    """Count what is cached, and what it cost to put there."""
    folder = root(where)
    entries = 0
    size = 0
    models: dict[str, int] = {}
    versions: dict[str, int] = {}
    paid = 0.0
    for path in sorted(folder.glob("*/*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        entries += 1
        size += path.stat().st_size
        models[str(raw.get("model") or "?")] = models.get(str(raw.get("model") or "?"), 0) + 1
        version = str(raw.get("prompt_version") or "?")
        versions[version] = versions.get(version, 0) + 1
        paid += float(raw.get("cost_usd") or 0.0)
    return Stats(
        entries=entries, bytes=size, models=models, prompt_versions=versions, paid_usd=paid
    )
