"""Who decided, and whether it can ever become a key case: Smriti's one set of exclusion rules (#1267).

**Why one place.** A key case is the ruler a model change is measured with, so it must be a
person's word about the drawing, never the machine's own answer coming back as truth. Every table
Smriti reads names its decider differently (`confirmed_by`, `answered_by`, `decided_by`, a `source`
column, `approval_source`); the rules that tell a person from code live here, once, and every
reader in `eval/smriti/export.py` asks them.

* **Code is never a candidate.** An actor beginning `code:` (`workflow.parts.CODE_ACTOR_PREFIX`;
  every `workflow.view_roles.CODE_CONFIRMERS` and Bala's `BALA_CONFIRMER` begin with it, and a test
  holds that true) is code deciding by itself. So is a record whose `source` is one of
  `MACHINE_SOURCES`. Such a record is the machine's answer: it is counted as skipped, and a later
  person's decision that replaces it is listed as **overturned**, never the other way round.
* **A carried copy is never ready.** It repeats a decision made on an earlier revision; the
  original is its own candidate. (The exporter marks it `carried`.)
* **A batch click is never ready.** "Confirm these N pages" is one click about many pages; each
  page needs its own look before it can become a case. (The exporter withholds its answer.)
* **Only a known reviewer counts.** Most "person" rows in our copies were written by scripts (`ui
  reviewer`, `proof-person@local`, replays). A name not on the local list of real reviewers is
  shown and counted, never convertible. With no list given, nobody is known, so nothing converts.

**The list stays local.** It holds real people's names, so it is a file under `~/gv-local`, never
in a repository: a path inside a git working tree is refused before it is read, and there is no
default path. It is named by the `GV_SMRITI_REVIEWERS` setting (an environment variable), or given
to `export_candidates` directly. Its format:

    {"format": "smriti-reviewers/1", "reviewers": ["<name as the product records it>", ...]}

Names are compared without case or surrounding spaces. A name that is not a plain person's name is
refused (`not_a_person`), on both lists and as the confirmer typed on the review page:

* a `code:` name;
* **anything but letters** with single spaces, hyphens or apostrophes between them (#1287 review):
  no digit, dot, underscore, "@" or non-ASCII letter, so "c1aude", "G.P.T" or a Cyrillic look-alike
  cannot pass for another name;
* **an AI or model name.** The name is first normalised: the usual swaps are undone (0→o, 1→l or i,
  3→e, 4→a, 5→s, 7→t, @→a), everything but letters is dropped, and it is lower-cased; runs of single
  letters ("c l a u d e", "A.I.") are joined. Then the long, distinctive names (`_AI_NAMES`:
  "claude", "anthropic", "openai", "gemini", "sonnet", "deepseek", ...) are refused anywhere, even
  inside a word ("claudebot", "ClaudeCode", "cla-ude"); "gpt" is also matched inside a word
  ("gptreviewer"), since no person's name holds it. Short tokens (`_MACHINE_WORDS`: "ai", "bot",
  "llm", "glm", "kimi", "agent", ...) are refused only as whole words (split at spaces, hyphens and
  capitals: "ReviewBot", "AIReviewer"), so "Allman", "Hillman", "Kai" and "Abbott" pass.

**Known cost:** a few real names hold a distinctive model name and are refused: "Claudette",
"Haikuo", "Gemma", "Kimi". Such a person is listed under a plain-letters variant of their name (for
example a nickname, or a surname alone), the same one they type on the review page.

**Who confirms is a separate list (#1282).** Reviewers DECIDE in the app; a confirmer CONFIRMS a
decision into a key case on the Smriti review page. Anant decided (2026-10-15) that he confirms,
never Claude. The confirmers list is its own local file, named by `GV_SMRITI_CONFIRMERS`, with no
default and refused inside a git working tree, like the reviewers list:

    {"format": "smriti-confirmers/1",
     "confirmers": ["<name typed on the review page>",
                    {"name": "<name typed on the review page>",
                     "app_names": ["<a name the product records for this person>", ...]}]}

The same name check applies to each `name`. Being on one list says nothing about the other, and
nobody confirms a case they decided themselves (`eval/smriti/confirm.py`). A person may sign in to
the app under other names (an e-mail, a short id): `app_names` lists them, so a decision recorded
under any of them is still theirs (`Confirmers.decided`; compared without case or spaces). The
app names only ever refuse, so they are not name-checked.

Source: issues #1267, #1282 · Verification: `tests/eval/test_smriti_actors.py`
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

__all__ = [
    "CODE",
    "CODE_ACTOR_PREFIX",
    "CONFIRMERS_ENV",
    "CONFIRMERS_FORMAT",
    "MACHINE_SOURCES",
    "REVIEWER",
    "REVIEWERS_ENV",
    "REVIEWERS_FORMAT",
    "UNCHECKED",
    "UNKNOWN",
    "Confirmers",
    "ConfirmersFileError",
    "Reviewers",
    "ReviewersFileError",
    "confirmers_from_environment",
    "is_code_actor",
    "is_machine_source",
    "load_confirmers",
    "load_reviewers",
    "not_a_person",
    "reviewers_from_environment",
]

#: The product's mark of a decision code made by itself (`workflow.parts.CODE_ACTOR_PREFIX`).
CODE_ACTOR_PREFIX: Final = "code:"
#: The `source` values that name the machine, never a person (design §2, circularity rule 3):
#: architect pairings (`code+ais`, `code`, `both-ais`, `none`), view matches (`automatic`,
#: `carried`) and approved matches (`deterministic`).
MACHINE_SOURCES: Final = frozenset(
    {"code", "code+ais", "both-ais", "none", "automatic", "carried", "deterministic"}
)
#: The setting that names the local real-reviewer list. No default: unset means nobody is known.
REVIEWERS_ENV: Final = "GV_SMRITI_REVIEWERS"
REVIEWERS_FORMAT: Final = "smriti-reviewers/1"
#: The setting that names the local confirmers list. No default: unset means nobody confirms.
CONFIRMERS_ENV: Final = "GV_SMRITI_CONFIRMERS"
CONFIRMERS_FORMAT: Final = "smriti-confirmers/1"

#: AI and model names, refused anywhere in the normalised name, even inside a word ("claudebot",
#: "ClaudeCode", "gptreviewer"). Each is long or distinctive enough that no plain person's name
#: holds it by chance (the few that do are listed under a variant; see the module docstring).
_AI_NAMES: Final = (
    "anthropic",
    "argon",
    "chatgpt",
    "claude",
    "codex",
    "copilot",
    "deepseek",
    "fable",
    "gemini",
    "gemma",
    "gpt",
    "grok",
    "haiku",
    "llama",
    "mistral",
    "mythos",
    "openai",
    "opus",
    "qwen",
    "sonnet",
)
#: Short tokens that name an AI, an agent or a program, refused as whole words only (after the
#: normalisation; split at spaces, hyphens, apostrophes and capitals: "ReviewBot", "AIReviewer"),
#: since they sit inside real names ("Allman", "Hillman", "Kai", "Abbott").
_MACHINE_WORDS: Final = frozenset(
    {"agent", "ai", "assistant", "bot", "glm", "kimi", "llm", "model", "robot", "script"}
)
#: A plain person's name: letters, with single spaces, hyphens or apostrophes between them.
_PLAIN_NAME: Final = re.compile(r"[A-Za-z]+(?:[ '-][A-Za-z]+)*")
#: The usual digit and symbol swaps for letters ("c1aude", "0pus", "4I"); "1" is tried as l and i.
_SWAPS: Final = str.maketrans({"0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a"})
_WORDS: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+")

#: A candidate's `actor_status`: a name on the list; a name not on it (or no list); code (never a
#: candidate, so only seen when asking); or a v1 file, made before anyone was checked.
REVIEWER: Final = "reviewer"
UNKNOWN: Final = "unknown"
CODE: Final = "code"
UNCHECKED: Final = "unchecked"


class ReviewersFileError(ValueError):
    """The real-reviewer list cannot be used: missing, malformed, or inside a repository."""


class ConfirmersFileError(ReviewersFileError):
    """The confirmers list cannot be used: missing, malformed, or inside a repository."""


def is_code_actor(actor: str | None) -> bool:
    """Whether this decider is code. Case and surrounding spaces do not hide it."""
    return isinstance(actor, str) and actor.strip().casefold().startswith(CODE_ACTOR_PREFIX)


def is_machine_source(source: str | None) -> bool:
    """Whether a record's `source` names the machine (code, AIs, a carried copy), not a person."""
    return isinstance(source, str) and source.strip().casefold() in MACHINE_SOURCES


def _folded(name: str) -> str:
    return name.strip().casefold()


def _spellings(name: str) -> set[str]:
    """The name with the usual swaps undone; each "1" as an "l" and as an "i" (every mix, up to a
    bound beyond which both uniform readings are tried)."""
    base = name.translate(_SWAPS)
    ones = [index for index, char in enumerate(base) if char == "1"]
    if not ones:
        return {base}
    if len(ones) > 8:
        return {base.replace("1", "l"), base.replace("1", "i")}
    spellings = set()
    for mask in range(1 << len(ones)):
        chars = list(base)
        for bit, index in enumerate(ones):
            chars[index] = "i" if mask >> bit & 1 else "l"
        spellings.add("".join(chars))
    return spellings


def _words(spelling: str) -> set[str]:
    """The whole words of one spelling, lower-cased, with runs of single letters joined ("c l a u
    d e", "G.P.T", "A.I." each read as one word)."""
    tokens = [token.casefold() for token in _WORDS.findall(spelling)]
    found = set(tokens)
    run: list[str] = []
    for token in [*tokens, ""]:
        if len(token) == 1:
            run.append(token)
            continue
        if len(run) > 1:
            found.add("".join(run))
        run = []
    return found


def _ai_inside(name: str) -> list[str]:
    """The AI, model or machine names this name holds after the normalisation, sorted."""
    found: set[str] = set()
    for spelling in _spellings(name):
        compact = re.sub(r"[^a-z]", "", spelling.casefold())
        found.update(ai for ai in _AI_NAMES if ai in compact)
        found.update(_words(spelling) & _MACHINE_WORDS)
    return sorted(found)


def not_a_person(name: str) -> str | None:
    """Why this name is not a plain person's name, or `None` when it is.

    Refused: a `code:` name; an AI, model or machine name, once the usual swaps are undone and
    everything but letters is dropped (long, distinctive names anywhere, even inside a word; short
    tokens as whole words only); and anything but letters with single spaces, hyphens or
    apostrophes between them (a digit, a symbol, a look-alike or invisible letter could pass for
    another name)."""
    if is_code_actor(name):
        return "names code"
    inside = _ai_inside(name)
    if inside:
        return f"names an AI, a model or a program ({', '.join(inside)})"
    if _PLAIN_NAME.fullmatch(name.strip()) is None:
        return (
            "is not a plain name: only letters, with single spaces, hyphens or apostrophes between "
            "them (no digits or other characters, so a look-alike cannot pass for another name)"
        )
    return None


@dataclass(frozen=True, slots=True)
class Reviewers:
    """The names that count as real reviewers, or `None` when no list was given."""

    names: frozenset[str] | None
    source: str | None
    """Where the list came from, for the record (`None` with no list)."""

    def status(self, actor: str | None) -> str:
        if is_code_actor(actor):
            return CODE
        if self.names is None or not isinstance(actor, str):
            return UNKNOWN
        known = {_folded(name) for name in self.names}
        return REVIEWER if _folded(actor) in known else UNKNOWN

    def why_not(self, actor: str | None) -> str | None:
        """Why this decider's decision can never become a key case, or `None` when it can."""
        status = self.status(actor)
        if status == REVIEWER:
            return None
        if status == CODE:
            return f"decided by code ({actor}); code's decision is never a key case"
        if self.names is None:
            return (
                f"decided by {actor!r}, and no list of real reviewers was given (set "
                f"{REVIEWERS_ENV} to a local file under ~/gv-local); shown, never a key case"
            )
        return (
            f"decided by {actor!r}, who is not on the local list of real reviewers; shown, never "
            "a key case"
        )


def _inside_git(path: Path) -> bool:
    return any((folder / ".git").exists() for folder in (path, *path.parents))


def _app_names(entry: dict[str, object], where: str, error: type[ReviewersFileError]) -> list[str]:
    extra = sorted(set(entry) - {"name", "app_names"})
    if extra:
        raise error(f"{where}: unknown field(s) in a confirmer: {', '.join(extra)}")
    others = entry.get("app_names", [])
    if not isinstance(others, list) or not all(
        isinstance(other, str) and other.strip() for other in others
    ):
        raise error(f"{where}: app_names is a list of non-empty names")
    return list(others)


def _load_names(
    path: Path,
    *,
    fmt: str,
    key: str,
    what: str,
    error: type[ReviewersFileError],
    with_app_names: bool = False,
) -> tuple[frozenset[str], dict[str, frozenset[str]], str]:
    """Read one local list of people's names, refusing one inside a repository, not in the
    format, or naming anyone who is not plainly a person (`not_a_person`). With `with_app_names`,
    an entry may also be `{"name": ..., "app_names": [...]}` (the confirmers list)."""
    resolved = path.expanduser().resolve()
    if _inside_git(resolved.parent):
        raise error(
            f"{resolved} is inside a git working tree; the {what} holds people's names and stays "
            "local (under ~/gv-local)"
        )
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as missing:
        raise error(f"the {what} {resolved} does not exist") from missing
    except (json.JSONDecodeError, UnicodeDecodeError) as damaged:
        raise error(f"{resolved.name} is not JSON: {damaged}") from damaged
    if not isinstance(document, dict) or document.get("format") != fmt:
        raise error(f"{resolved.name} is not a {fmt} file")
    extra = sorted(set(document) - {"format", key})
    if extra:
        raise error(f"unknown field(s) in {resolved.name}: {', '.join(extra)}")
    entries = document.get(key)
    if not isinstance(entries, list):
        raise error(f"{resolved.name}: {key} is a list of non-empty names")
    names: list[str] = []
    app_names: dict[str, frozenset[str]] = {}
    for entry in entries:
        others: list[str] = []
        if with_app_names and isinstance(entry, dict):
            name = entry.get("name")
            others = _app_names(entry, resolved.name, error)
        else:
            name = entry
        if not isinstance(name, str) or not name.strip():
            raise error(f"{resolved.name}: {key} is a list of non-empty names")
        names.append(name)
        if others:
            app_names[_folded(name)] = frozenset(others)
    coded = [name for name in names if is_code_actor(name)]
    if coded:
        raise error(
            f"{resolved.name} lists code in the {what} ({', '.join(coded)}); code is never a person"
        )
    for name in names:
        why = not_a_person(name)
        if why is not None:
            raise error(
                f"{resolved.name}: {name!r} {why}; only a plain person's name belongs in the {what}"
            )
    return frozenset(names), app_names, str(resolved)


def load_reviewers(path: Path) -> Reviewers:
    """Read the local real-reviewer list, refusing one inside a repository or not in the format."""
    names, _none, source = _load_names(
        path,
        fmt=REVIEWERS_FORMAT,
        key="reviewers",
        what="real-reviewer list",
        error=ReviewersFileError,
    )
    return Reviewers(names=names, source=source)


def reviewers_from_environment() -> Reviewers:
    """The list `GV_SMRITI_REVIEWERS` names; with the setting unset, no list (nobody is known)."""
    configured = os.environ.get(REVIEWERS_ENV, "").strip()
    if not configured:
        return Reviewers(names=None, source=None)
    return load_reviewers(Path(configured))


def _spaceless(name: str) -> str:
    return "".join(name.split()).casefold()


@dataclass(frozen=True, slots=True)
class Confirmers:
    """The names that may confirm a case into a key, or `None` when no list was given."""

    names: frozenset[str] | None
    source: str | None
    """Where the list came from, for the record (`None` with no list)."""
    app_names: Mapping[str, frozenset[str]] = field(default_factory=dict)
    """Per confirmer (by name, without case or surrounding spaces): the other names the product
    records for this person in the app. They only ever refuse (`decided`)."""

    def knows(self, name: str | None) -> bool:
        """Whether this name is on the list (without case or surrounding spaces)."""
        if self.names is None or not isinstance(name, str):
            return False
        return _folded(name) in {_folded(known) for known in self.names}

    def decided(self, confirmer: str, decided_by: str | None) -> bool:
        """Whether `decided_by` is this confirmer: their own name or one of their app names,
        compared without case or any spaces. Such a decision is never theirs to confirm."""
        if not isinstance(decided_by, str):
            return False
        own = {confirmer, *self.app_names.get(_folded(confirmer), frozenset())}
        return _spaceless(decided_by) in {_spaceless(name) for name in own}


def load_confirmers(path: Path) -> Confirmers:
    """Read the local confirmers list, refusing one inside a repository or not in the format."""
    names, app_names, source = _load_names(
        path,
        fmt=CONFIRMERS_FORMAT,
        key="confirmers",
        what="confirmers list",
        error=ConfirmersFileError,
        with_app_names=True,
    )
    return Confirmers(names=names, source=source, app_names=app_names)


def confirmers_from_environment() -> Confirmers:
    """The list `GV_SMRITI_CONFIRMERS` names; with the setting unset, no list (nobody confirms)."""
    configured = os.environ.get(CONFIRMERS_ENV, "").strip()
    if not configured:
        return Confirmers(names=None, source=None)
    return load_confirmers(Path(configured))
