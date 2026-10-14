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

Names are compared without case or surrounding spaces. A name that is not a person's is refused
(`not_a_person`): a `code:` name; an AI or agent name, even inside a word ("claudebot",
"ClaudeCode", "gptreviewer", "opus"); and a name that is not plain ASCII, where a look-alike letter
could pass for another name.

**Who confirms is a separate list (#1282).** Reviewers DECIDE in the app; a confirmer CONFIRMS a
decision into a key case on the Smriti review page. Anant decided (2026-10-15) that he confirms,
never Claude. The confirmers list is its own local file, named by `GV_SMRITI_CONFIRMERS`, with no
default and refused inside a git working tree, like the reviewers list:

    {"format": "smriti-confirmers/1", "confirmers": ["<name typed on the review page>", ...]}

The same name check applies. Being on one list says nothing about the other, and nobody confirms
a case they decided themselves (`eval/smriti/confirm.py`).

Source: issues #1267, #1282 · Verification: `tests/eval/test_smriti_actors.py`
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
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

#: AI and agent names, refused anywhere in a name, even inside a word ("claudebot",
#: "ClaudeCode", "gptreviewer"). None of them is part of a plain person's name.
_AI_NAMES: Final = (
    "anthropic",
    "chatgpt",
    "claude",
    "codex",
    "copilot",
    "gemini",
    "gpt",
    "haiku",
    "llm",
    "openai",
    "opus",
    "sonnet",
)
#: Words that name an AI, an agent or a program, refused as whole words only (split at spaces,
#: punctuation and capitals: "ReviewBot", "AIReviewer"), since they sit inside real names
#: ("Abbott", "Kailash").
_MACHINE_WORDS: Final = frozenset({"ai", "agent", "assistant", "bot", "model", "robot", "script"})
_WORDS: Final = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

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


def not_a_person(name: str) -> str | None:
    """Why this name is not a plain person's name, or `None` when it is.

    Refused: a `code:` name; a name that is not plain printable ASCII (a look-alike letter, such
    as a Cyrillic "а", or an invisible character could pass for another name); an AI or agent
    name anywhere in it, even inside a word; and a machine word standing as a word of its own."""
    if is_code_actor(name):
        return "names code"
    if not name.isascii() or not name.isprintable():
        return (
            "is not plain ASCII letters (a look-alike or invisible letter could pass for another "
            "name)"
        )
    folded = name.casefold()
    inside = sorted(ai for ai in _AI_NAMES if ai in folded)
    if inside:
        return f"names an AI or an agent ({', '.join(inside)})"
    words = {word.casefold() for word in _WORDS.findall(name)} & _MACHINE_WORDS
    if words:
        return f"names an AI, an agent or a program ({', '.join(sorted(words))})"
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


def _load_names(
    path: Path, *, fmt: str, field: str, what: str, error: type[ReviewersFileError]
) -> tuple[frozenset[str], str]:
    """Read one local list of people's names, refusing one inside a repository, not in the
    format, or naming anyone who is not plainly a person (`not_a_person`)."""
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
    extra = sorted(set(document) - {"format", field})
    if extra:
        raise error(f"unknown field(s) in {resolved.name}: {', '.join(extra)}")
    names = document.get(field)
    if not isinstance(names, list) or not all(
        isinstance(name, str) and name.strip() for name in names
    ):
        raise error(f"{resolved.name}: {field} is a list of non-empty names")
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
    return frozenset(names), str(resolved)


def load_reviewers(path: Path) -> Reviewers:
    """Read the local real-reviewer list, refusing one inside a repository or not in the format."""
    names, source = _load_names(
        path,
        fmt=REVIEWERS_FORMAT,
        field="reviewers",
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


@dataclass(frozen=True, slots=True)
class Confirmers:
    """The names that may confirm a case into a key, or `None` when no list was given."""

    names: frozenset[str] | None
    source: str | None
    """Where the list came from, for the record (`None` with no list)."""

    def knows(self, name: str | None) -> bool:
        """Whether this name is on the list (without case or surrounding spaces)."""
        if self.names is None or not isinstance(name, str):
            return False
        return _folded(name) in {_folded(known) for known in self.names}


def load_confirmers(path: Path) -> Confirmers:
    """Read the local confirmers list, refusing one inside a repository or not in the format."""
    names, source = _load_names(
        path,
        fmt=CONFIRMERS_FORMAT,
        field="confirmers",
        what="confirmers list",
        error=ConfirmersFileError,
    )
    return Confirmers(names=names, source=source)


def confirmers_from_environment() -> Confirmers:
    """The list `GV_SMRITI_CONFIRMERS` names; with the setting unset, no list (nobody confirms)."""
    configured = os.environ.get(CONFIRMERS_ENV, "").strip()
    if not configured:
        return Confirmers(names=None, source=None)
    return load_confirmers(Path(configured))
