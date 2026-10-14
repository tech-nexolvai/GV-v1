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

Names are compared without case or surrounding spaces. A `code:` name on the list is refused: code
is never a reviewer.

Source: issue #1267 · Verification: `tests/eval/test_smriti_actors.py`
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "CODE",
    "CODE_ACTOR_PREFIX",
    "MACHINE_SOURCES",
    "REVIEWER",
    "REVIEWERS_ENV",
    "REVIEWERS_FORMAT",
    "UNCHECKED",
    "UNKNOWN",
    "Reviewers",
    "ReviewersFileError",
    "is_code_actor",
    "is_machine_source",
    "load_reviewers",
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

#: A candidate's `actor_status`: a name on the list; a name not on it (or no list); code (never a
#: candidate, so only seen when asking); or a v1 file, made before anyone was checked.
REVIEWER: Final = "reviewer"
UNKNOWN: Final = "unknown"
CODE: Final = "code"
UNCHECKED: Final = "unchecked"


class ReviewersFileError(ValueError):
    """The real-reviewer list cannot be used: missing, malformed, or inside a repository."""


def is_code_actor(actor: str | None) -> bool:
    """Whether this decider is code. Case and surrounding spaces do not hide it."""
    return isinstance(actor, str) and actor.strip().casefold().startswith(CODE_ACTOR_PREFIX)


def is_machine_source(source: str | None) -> bool:
    """Whether a record's `source` names the machine (code, AIs, a carried copy), not a person."""
    return isinstance(source, str) and source.strip().casefold() in MACHINE_SOURCES


def _folded(name: str) -> str:
    return name.strip().casefold()


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


def load_reviewers(path: Path) -> Reviewers:
    """Read the local real-reviewer list, refusing one inside a repository or not in the format."""
    resolved = path.expanduser().resolve()
    if _inside_git(resolved.parent):
        raise ReviewersFileError(
            f"{resolved} is inside a git working tree; the real-reviewer list holds people's names "
            "and stays local (under ~/gv-local)"
        )
    try:
        document = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ReviewersFileError(f"the real-reviewer list {resolved} does not exist") from error
    except json.JSONDecodeError as error:
        raise ReviewersFileError(f"{resolved.name} is not JSON: {error}") from error
    if not isinstance(document, dict) or document.get("format") != REVIEWERS_FORMAT:
        raise ReviewersFileError(f"{resolved.name} is not a {REVIEWERS_FORMAT} file")
    extra = sorted(set(document) - {"format", "reviewers"})
    if extra:
        raise ReviewersFileError(f"unknown field(s) in {resolved.name}: {', '.join(extra)}")
    names = document.get("reviewers")
    if not isinstance(names, list) or not all(
        isinstance(name, str) and name.strip() for name in names
    ):
        raise ReviewersFileError(f"{resolved.name}: reviewers is a list of non-empty names")
    coded = [name for name in names if is_code_actor(name)]
    if coded:
        raise ReviewersFileError(
            f"{resolved.name} lists code as a reviewer ({', '.join(coded)}); code never is one"
        )
    return Reviewers(names=frozenset(names), source=str(resolved))


def reviewers_from_environment() -> Reviewers:
    """The list `GV_SMRITI_REVIEWERS` names; with the setting unset, no list (nobody is known)."""
    configured = os.environ.get(REVIEWERS_ENV, "").strip()
    if not configured:
        return Reviewers(names=None, source=None)
    return load_reviewers(Path(configured))
