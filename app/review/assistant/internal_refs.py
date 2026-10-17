"""Internal references taken out of the text the chat agent reads (#1520).

The rulebook's notes and descriptions are written for the team and carry the team's ticket numbers
("awaiting his confirmation (#674)", "(1.2.0, #1138)", "(PR #809)"). The agent quoted one in an
answer to a reviewer, who has no use for it. Text exported to the agent (`rules.md`, the recorded
results it reads) goes through these first.

- `without_ticket_refs`: the careful one, for data that may hold the client's own text (the
  recorded results): only a ticket number in brackets, or one named "PR" / "issue", is taken out.
  A bare "#2" (a typical unit, a hinge size) is left alone.
- `without_internal_refs`: for text the team wrote (`rules.md`): every "#<number>" goes too.

Both also turn the notes' "awaiting his confirmation" into plain words a reviewer can use.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["without_internal_refs", "without_ticket_refs"]

_REF: Final = r"(?:(?:PRs?|issues?|pull request)\s*)?#\d+"
#: A bracket holding only ticket numbers: " (#674)", " (PR #809, #812)".
_ONLY_REFS: Final = re.compile(rf"\s*\(\s*{_REF}(?:\s*(?:,|and|/)\s*{_REF})*\s*\)", re.IGNORECASE)
#: A ticket number beside other words in a bracket: "(1.2.0, #1138)" -> "(1.2.0)".
_LISTED_REF: Final = re.compile(rf"\s*[,;]\s*{_REF}(?=[^()]*\))", re.IGNORECASE)
#: A ticket number first in a bracket with other words: "(#67, see AGENTS.md)" -> "(see AGENTS.md)".
_LEADING_REF: Final = re.compile(rf"\(\s*{_REF}\s*[,;]\s*", re.IGNORECASE)
_NAMED_REF: Final = re.compile(r"\s*\b(?:PRs?|issues?|pull request)\s*#\d+", re.IGNORECASE)
_BARE_REF: Final = re.compile(r"(?:(?<=[\s(])|^)#\d+\b[ \t]?|[ \t]+#\d+\b", re.MULTILINE)
_EMPTY_BRACKETS: Final = re.compile(r"\s*\(\s*[,;]?\s*\)")
_WAITING: Final = re.compile(
    r"\bawaiting (?:his|their|the client'?s?) confirmation\b", re.IGNORECASE
)
_PLAIN_WAITING: Final = "the client has not yet confirmed which is right"


def without_ticket_refs(text: str) -> str:
    """`text` without ticket numbers in brackets or named "PR" / "issue"."""
    out = _ONLY_REFS.sub("", text)
    out = _LISTED_REF.sub("", out)
    out = _LEADING_REF.sub("(", out)
    out = _NAMED_REF.sub("", out)
    out = _EMPTY_BRACKETS.sub("", out)
    return _WAITING.sub(_PLAIN_WAITING, out)


def without_internal_refs(text: str) -> str:
    """`text` with no "#<number>" left at all (team-written text only)."""
    out = _BARE_REF.sub("", without_ticket_refs(text))
    return _EMPTY_BRACKETS.sub("", out)
