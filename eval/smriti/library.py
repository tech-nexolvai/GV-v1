"""One shelf for every answer key: the local list Pariksha judges a change against (#1268).

**Why.** Answer keys were scattered over proof folders: the number reader's and the row choice's
(#1219), the walls and piece kinds (#1250), the architect reader's, the phase-5 depths. A change to
a prompt, a model or a picture was checked against whichever key someone remembered. Anant decided
on 2026-10-15: **one local list of all of them**. Any future AI change is checked against every key
of the agent it touches, accepted only with 0 new wrong, and the list never switches anything.

**What it is.** One JSON file (`pariksha-library/1`) that *registers* keys; nothing moves. Each
entry names a key file by its absolute path and records:

* `agent` and `kind`: whose key it is (`akshara` labels, `rekha` rows, `vastu` architect,
  `rekha-walls` walls, `bala-kinds` piece kinds, `rekha-view` view kinds, `akshara-depth` depths);
* `name`, `version`, `sha256` and `cases`, as the key's own loader reads them (`sha256` is the
  hash every scorecard records, so a key edited after it was registered is refused when used);
* `confirmed_by` and `label`: who keyed or confirmed it, and how far to trust it (for example
  "Claude double-read, not human-confirmed"). Both are required: a verdict is only as good as its
  key, so every verdict prints them.

    {"format": "pariksha-library/1",
     "entries": [{"agent": "akshara", "kind": "labels", "path": "/abs/key.json", "name": "...",
                  "version": "...", "sha256": "<64 hex>", "cases": 12,
                  "confirmed_by": "...", "label": "...", "source": "..."}]}

**Local only.** The keys and the list hold client values (what drawings print), so the list's path
is given each time (`--library`) or by the `GV_KEY_LIBRARY` setting, with no default in the
repository, and a path inside any git working tree is refused.

**Conflicts are refused**, never merged silently:

* the same file registered again with other contents (a key is never edited in place: a new
  version is a new file), or again with another label;
* the same key (same hash) at a second path;
* one name and version with other answers;
* two keys of one agent giving one question different answers. An answer the key leaves
  unsettled (a wall end `key_unsure`, an `open` depth, a piece word that names no kind) is no
  answer, so it never conflicts.

Source: issue #1268 · Verification: `tests/eval/test_smriti_library.py`
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Final

from eval.pariksha.depth import DEPTH_AGENTS, DepthKey, load_depth_key
from eval.pariksha.keys import (
    KEY_FORMAT,
    AnswerKey,
    ArchitectAnswerKey,
    KeyFormatError,
    LabelTruth,
    load_key,
)
from eval.pariksha.kinds import KINDS_AGENT, KindsKey, load_kinds_key
from eval.pariksha.walls import KEY_UNSURE, WALLS_AGENT, WallsKey, load_walls_key

__all__ = [
    "KEY_KINDS",
    "LIBRARY_FORMAT",
    "LIBRARY_SETTING",
    "Library",
    "LibraryEntry",
    "LibraryError",
    "LoadedKey",
    "describe",
    "key_agent",
    "key_answers",
    "key_pages",
    "library_path",
    "load_entry_key",
    "outside_git",
    "read_any_key",
    "read_library",
    "register",
    "write_library",
]

LIBRARY_FORMAT: Final = "pariksha-library/1"
#: The setting naming the list when `--library` is not given. There is no default.
LIBRARY_SETTING: Final = "GV_KEY_LIBRARY"
#: Each key agent's kind of key.
KEY_KINDS: Final[Mapping[str, str]] = {
    "akshara": "labels",
    "rekha": "rows",
    "vastu": "architect",
    WALLS_AGENT: "walls",
    KINDS_AGENT: "piece-kinds",
    "rekha-view": "view-kinds",
    "akshara-depth": "depths",
}
assert set(DEPTH_AGENTS) <= set(KEY_KINDS)

LoadedKey = AnswerKey | ArchitectAnswerKey | WallsKey | KindsKey | DepthKey

_SHA256_LENGTH: Final = 64
_HEADER: Final = frozenset({"format", "entries"})
_LISTED: Final = 5


class LibraryError(ValueError):
    """The list, an entry or a key cannot be used as asked; the message says why."""


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    agent: str
    kind: str
    path: str
    """The key file's absolute path. The file stays where it is."""
    name: str
    version: str
    sha256: str
    """The key's canonical hash, as its loader computes it and every scorecard records it."""
    cases: int
    confirmed_by: str
    """Who keyed or confirmed the key (free text, for a person)."""
    label: str
    """How far to trust it, printed beside every verdict (e.g. "not human-confirmed")."""
    source: str = ""
    """Where the key came from (free text, optional)."""

    def matches(self, agent: str) -> bool:
        """`rekha` names every Rekha key (rows, walls, views); `rekha-walls` names only walls."""
        return agent in (self.agent, self.agent.split("-", 1)[0])

    def __str__(self) -> str:
        return f"{self.agent} {self.kind}: {self.name} {self.version} ({self.sha256[:12]})"


_ENTRY_FIELDS: Final = frozenset(field.name for field in fields(LibraryEntry))


@dataclass(frozen=True, slots=True)
class Library:
    path: Path
    entries: tuple[LibraryEntry, ...]

    def for_agent(self, agent: str) -> tuple[LibraryEntry, ...]:
        return tuple(entry for entry in self.entries if entry.matches(agent))


# --- where the list lives ------------------------------------------------------------------


def library_path(given: str | Path | None, environ: Mapping[str, str] | None = None) -> Path:
    """The list's path: the one given, else the `GV_KEY_LIBRARY` setting. There is no default."""
    if given is not None and str(given).strip():
        return Path(given).expanduser()
    setting = (os.environ if environ is None else environ).get(LIBRARY_SETTING, "").strip()
    if setting:
        return Path(setting).expanduser()
    raise LibraryError(
        f"no library path: give --library or set {LIBRARY_SETTING}. There is no default, so the "
        "list (which names keys holding client values) never lands in a repository"
    )


def _inside_git_working_tree(path: Path) -> bool:
    existing = path
    while not existing.exists() and existing != existing.parent:
        existing = existing.parent
    if existing.is_file():
        existing = existing.parent
    try:
        answer = subprocess.run(
            ["git", "-C", str(existing), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return answer.returncode == 0 and answer.stdout.strip() == "true"


def outside_git(path: Path, what: str = "the key library") -> Path:
    """The path, resolved, or a refusal when it is inside a git working tree (this one or any)."""
    resolved = path.expanduser().resolve()
    repository = Path(__file__).resolve().parents[2]
    if (
        resolved == repository
        or repository in resolved.parents
        or _inside_git_working_tree(resolved.parent)
    ):
        raise LibraryError(
            f"{path} is inside a git working tree; {what} names keys that hold client values, so "
            "write it outside any repository (for example under ~/gv-local/)"
        )
    return resolved


# --- keys of every kind --------------------------------------------------------------------


def read_any_key(path: Path) -> LoadedKey:
    """A `pariksha-key/1` file of any agent, read by that agent's own loader."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError as error:
        raise LibraryError(f"no key file at {path}") from error
    except json.JSONDecodeError as error:
        raise LibraryError(f"{path.name} is not JSON: {error}") from error
    if not isinstance(data, Mapping) or data.get("format") != KEY_FORMAT:
        raise LibraryError(f"{path} is not a {KEY_FORMAT} key")
    agent = data.get("agent")
    try:
        if agent == WALLS_AGENT:
            return load_walls_key(data)
        if agent == KINDS_AGENT:
            return load_kinds_key(data)
        if agent in DEPTH_AGENTS:
            return load_depth_key(data)
        return load_key(data)
    except KeyFormatError as error:
        raise LibraryError(f"{path}: {error}") from error


def key_agent(key: LoadedKey) -> str:
    if isinstance(key, ArchitectAnswerKey):
        return "vastu"
    if isinstance(key, WallsKey):
        return WALLS_AGENT
    if isinstance(key, KindsKey):
        return KINDS_AGENT
    return key.agent


def _cases(key: LoadedKey) -> int:
    if isinstance(key, ArchitectAnswerKey):
        return sum(len(page.widths) for page in key.pages)
    if isinstance(key, DepthKey):
        return len(key.values)
    return len(key.cases)


def architect_page(document: str | None, page: int) -> str:
    """How an architect key's page is named: its document's hash (when the key names one) and the
    page number, since the architect reader's spans are stored per document, not per page hash."""
    return f"{document or 'any-document'}#p{page}"


def key_pages(key: LoadedKey) -> frozenset[str]:
    """The pages the key's cases are on: page content hashes (architect: document and number)."""
    if isinstance(key, ArchitectAnswerKey):
        return frozenset(architect_page(key.document_sha256, page.page) for page in key.pages)
    if isinstance(key, DepthKey):
        return frozenset(value.question.page_sha256 for value in key.values)
    if isinstance(key, KindsKey):
        return frozenset(case.page_sha256 for case in key.cases)
    return frozenset(case.question.page_sha256 for case in key.cases)


def key_answers(key: LoadedKey) -> dict[str, str]:
    """Each settled answer the key gives, by the question's identity, to find conflicts."""
    answers: dict[str, str] = {}
    if isinstance(key, AnswerKey):
        for case in key.cases:
            question = case.question
            # Label cases are matched by their box, row cases by their picture: one question id
            # with another box or picture is another question, not a conflict.
            guard = case.slot_box or case.picture_sha256 or ""
            identity = f"{question.family}@{question.page_sha256}/{question.question_id}@{guard}"
            truth = case.truth
            shown = " | ".join(sorted(truth.texts)) if isinstance(truth, LabelTruth) else str(truth)
            answers[identity] = shown or str(truth)
    elif isinstance(key, ArchitectAnswerKey):
        for page in key.pages:
            widths = ", ".join(str(width) for width in sorted(page.widths))
            answers[f"vastu@{architect_page(key.document_sha256, page.page)}"] = widths
    elif isinstance(key, WallsKey):
        for wall in key.cases:
            box = ",".join(str(edge) for edge in wall.row_box)
            base = f"walls@{wall.question.page_sha256}/{wall.question.question_id}@{box}"
            for side in ("left", "right"):
                end = wall.end(side)
                for field, said in (("drawn", end.drawn), ("site", end.site)):
                    if said != KEY_UNSURE:
                        answers[f"{base}#{side}.{field}"] = said
            if wall.site_layout is not None:
                answers[f"{base}#site_layout"] = wall.site_layout
    elif isinstance(key, KindsKey):
        for piece in key.cases:
            if piece.truth.kinds:
                identity = f"kinds@{piece.page_sha256}/{piece.question_id}#{piece.pieces}"
                answers[identity] = str(piece.truth)
    else:
        for value in key.values:
            if value.truth is not None:
                answers[f"{key.agent}@{value.question.page_sha256}/{value.case_id}"] = str(
                    value.truth
                )
    return answers


# --- entries -------------------------------------------------------------------------------


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LibraryError(f"{field} must be a non-empty text, not {value!r}")
    return value.strip()


def describe(
    path: Path, *, confirmed_by: str, label: str, source: str = ""
) -> tuple[LibraryEntry, LoadedKey]:
    """The entry a key file would get, read through its own loader, and the key itself."""
    confirmed_by = _text(confirmed_by, "confirmed_by")
    label = _text(label, "label")
    resolved = path.expanduser().resolve()
    key = read_any_key(resolved)
    agent = key_agent(key)
    entry = LibraryEntry(
        agent=agent,
        kind=KEY_KINDS[agent],
        path=str(resolved),
        name=key.name,
        version=key.version,
        sha256=key.sha256,
        cases=_cases(key),
        confirmed_by=confirmed_by,
        label=label,
        source=source.strip(),
    )
    return entry, key


def load_entry_key(entry: LibraryEntry) -> LoadedKey:
    """The entry's key, refused when the file no longer holds the key that was registered."""
    key = read_any_key(Path(entry.path))
    if key.sha256 != entry.sha256 or key_agent(key) != entry.agent:
        raise LibraryError(
            f"{entry.path} has changed since it was registered (registered {entry.sha256[:12]}, "
            f"now {key.sha256[:12]}): a key is never edited in place; write the new version to a "
            "new file and register that"
        )
    return key


def _entry(data: object, index: int) -> LibraryEntry:
    where = f"entry {index + 1}"
    if not isinstance(data, Mapping):
        raise LibraryError(f"{where} is not an object")
    extra = sorted(set(data) - _ENTRY_FIELDS)
    if extra:
        raise LibraryError(f"unknown field(s) in {where}: {', '.join(extra)}")
    agent = _text(data.get("agent"), f"{where} agent")
    if agent not in KEY_KINDS:
        raise LibraryError(f"{where}: agent {agent!r} is not one of {tuple(KEY_KINDS)}")
    kind = _text(data.get("kind"), f"{where} kind")
    if kind != KEY_KINDS[agent]:
        raise LibraryError(
            f"{where}: the kind of a {agent} key is {KEY_KINDS[agent]!r}, not {kind!r}"
        )
    path = _text(data.get("path"), f"{where} path")
    if not Path(path).is_absolute():
        raise LibraryError(f"{where}: path must be absolute, not {path!r}")
    sha256 = _text(data.get("sha256"), f"{where} sha256")
    if len(sha256) != _SHA256_LENGTH or any(c not in "0123456789abcdef" for c in sha256):
        raise LibraryError(f"{where}: sha256 must be 64 lower-case hex characters")
    cases = data.get("cases")
    if isinstance(cases, bool) or not isinstance(cases, int) or cases < 1:
        raise LibraryError(f"{where}: cases must be a whole number from 1, not {cases!r}")
    source = data.get("source", "")
    if not isinstance(source, str):
        raise LibraryError(f"{where}: source must be text")
    return LibraryEntry(
        agent=agent,
        kind=kind,
        path=path,
        name=_text(data.get("name"), f"{where} name"),
        version=_text(data.get("version"), f"{where} version"),
        sha256=sha256,
        cases=cases,
        confirmed_by=_text(data.get("confirmed_by"), f"{where} confirmed_by"),
        label=_text(data.get("label"), f"{where} label"),
        source=source,
    )


def _identity_conflict(entry: LibraryEntry, known: LibraryEntry) -> str | None:
    """Why two entries cannot both be on the list, or `None` when they can."""
    if entry.path == known.path and entry.sha256 != known.sha256:
        return (
            f"{entry.path} has changed since it was registered (registered {known.sha256[:12]}, "
            f"now {entry.sha256[:12]}): a key is never edited in place; write the new version to "
            "a new file and register that"
        )
    if entry.path == known.path:
        return f"{entry.path} is already registered as {known}, with other details ({known.label})"
    if entry.sha256 == known.sha256:
        return f"the same key ({entry.sha256[:12]}) is already registered at {known.path}"
    if (entry.agent, entry.name, entry.version) == (known.agent, known.name, known.version):
        return (
            f"a {entry.agent} key named {entry.name!r} version {entry.version!r} is already "
            f"registered with other answers ({known.sha256[:12]}): one name and version is one key"
        )
    return None


def read_library(path: Path, *, missing_ok: bool = False) -> Library:
    """The list, strictly: unknown fields, a wrong kind or two clashing entries are refused."""
    resolved = path.expanduser()
    if not resolved.exists():
        if missing_ok:
            return Library(path=resolved, entries=())
        raise LibraryError(f"no library at {path}: register a key first")
    try:
        data = json.loads(resolved.read_text())
    except json.JSONDecodeError as error:
        raise LibraryError(f"{path} is not JSON: {error}") from error
    if not isinstance(data, Mapping):
        raise LibraryError("a library is one JSON object")
    if data.get("format") != LIBRARY_FORMAT:
        raise LibraryError(f"format must be {LIBRARY_FORMAT!r}, not {data.get('format')!r}")
    extra = sorted(set(data) - _HEADER)
    if extra:
        raise LibraryError(f"unknown field(s) in the library: {', '.join(extra)}")
    raw = data.get("entries")
    if not isinstance(raw, list):
        raise LibraryError("entries must be a list")
    entries: list[LibraryEntry] = []
    for index, item in enumerate(raw):
        entry = _entry(item, index)
        for known in entries:
            if _identity_conflict(entry, known) is not None:
                raise LibraryError(
                    f"the library holds one key twice or two keys that clash: {known} and {entry}"
                )
        entries.append(entry)
    return Library(path=resolved, entries=tuple(entries))


def register(library: Library, entry: LibraryEntry, key: LoadedKey) -> Library:
    """The list with the entry added; the same entry again changes nothing; a conflict is refused.

    The registered keys of the same agent are read again (each refused if it changed since it was
    registered) and compared answer by answer with the new one."""
    for known in library.entries:
        if known == entry:
            return library
        clash = _identity_conflict(entry, known)
        if clash is not None:
            raise LibraryError(clash)
    answers = key_answers(key)
    for known in library.entries:
        if known.agent != entry.agent:
            continue
        theirs = key_answers(load_entry_key(known))
        differ = sorted(
            question
            for question in set(answers) & set(theirs)
            if answers[question] != theirs[question]
        )
        if differ:
            listed = "; ".join(
                f"{question}: {theirs[question]!r} there, {answers[question]!r} here"
                for question in differ[:_LISTED]
            )
            more = f", and {len(differ) - _LISTED} more" if len(differ) > _LISTED else ""
            raise LibraryError(
                f"{entry.name} gives {len(differ)} question(s) a different answer than the "
                f"registered {known}: {listed}{more}. A person settles which is right before "
                "either judges a change"
            )
    return replace(library, entries=(*library.entries, entry))


def _document(library: Library) -> dict[str, object]:
    return {
        "format": LIBRARY_FORMAT,
        "entries": [
            {
                name: value
                for name, value in asdict(entry).items()
                if value != "" or name != "source"
            }
            for entry in library.entries
        ],
    }


def write_library(library: Library) -> None:
    """Write the list (outside any git working tree), replacing the old file in one step."""
    target = outside_git(library.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(_document(library), indent=1, ensure_ascii=False) + "\n"
    temporary = target.with_name(
        f".{target.name}.{hashlib.sha256(text.encode()).hexdigest()[:12]}.tmp"
    )
    temporary.write_text(text)
    os.replace(temporary, target)
