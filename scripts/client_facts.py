"""Read `docs/CLIENT_FACTS.md` — the authority on what the client has and has not told us.

`issue_gate.py` uses this to answer *"is this story actually blocked?"* without anyone reconciling
four documents by hand. That reconciliation is what made decisions slow, and it is what produced two
real errors: `CT011`–`CT013` recorded as undefined when a diagram defines them, and `±1/8″` treated
as a client tolerance when it is our own placeholder.

The important field is `blocks`:

- `formula` — the answer changes what gets computed. A rule authored now would compute the wrong
  quantity and pass confidently. The story is blocked, and no argument makes it otherwise.
- `value` — the answer supplies a number. The rule can be authored with `TOLERANCE_UNCONFIRMED`,
  which returns REVIEW REQUIRED for everything and cannot reach production (ADR-0011). The story is
  workable and ships provisional.
- `nothing` — informational.

Kept dependency-free on purpose. Every guard in this project that needed an optional package has, at
some point, failed to run because of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

FACTS_PATH = Path(__file__).resolve().parent.parent / "docs" / "CLIENT_FACTS.md"


class Status(StrEnum):
    ANSWERED = "ANSWERED"
    OPEN = "OPEN"


class Blocks(StrEnum):
    FORMULA = "formula"
    VALUE = "value"
    NOTHING = "nothing"


@dataclass(frozen=True, slots=True)
class ClientFact:
    """One question, and what we actually know about it."""

    ref: str
    title: str
    status: Status
    blocks: Blocks
    issue: str
    answer: str
    source: str

    @property
    def settled(self) -> bool:
        return self.status is Status.ANSWERED

    @property
    def stops_work(self) -> bool:
        """Whether a story depending on this must not be started.

        An open `formula` question stops work: the rule would compute the wrong thing. An open
        `value` question does not — that is what the UNCONFIRMED sentinel is for, and treating the
        two alike is what cost three round trips of argument.
        """
        return not self.settled and self.blocks is Blocks.FORMULA

    def gate_line(self) -> str:
        if self.settled:
            return f"{self.ref} is ANSWERED — {self.answer.strip()}"
        if self.blocks is Blocks.FORMULA:
            return (
                f"{self.ref} is OPEN and changes the formula. A rule written now would compute the "
                f"wrong quantity and pass confidently. ({self.issue})"
            )
        if self.blocks is Blocks.VALUE:
            return (
                f"{self.ref} is OPEN but supplies a value only — build it, ship it PROVISIONAL. "
                f"The check returns REVIEW REQUIRED until the number arrives. ({self.issue})"
            )
        return f"{self.ref} is OPEN and blocks nothing. ({self.issue})"


class ClientFactsError(Exception):
    """The facts file could not be parsed. Loud, because an unparsed authority is no authority."""


_ENTRY = re.compile(
    r"^## (?P<ref>Q\d+) — (?P<title>.+?)$\n(?P<body>.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL
)
# Fields wrap. A value continues onto any following indented line, up to the next field or the end
# of the entry — capturing only the first line silently truncated the evidence, and `source` is the
# field where that matters most: Q1's warning about the 5" cabinet filler lives on line four.
_FIELD = re.compile(
    r"^(?P<key>status|blocks|issue|answer|source):[ \t]*(?P<value>.*(?:\n[ \t]+(?!\w+:).*)*)",
    re.MULTILINE,
)


def load(path: Path = FACTS_PATH) -> dict[str, ClientFact]:
    """Parse the facts file into `{"Q1": ClientFact, ...}`."""
    text = path.read_text(encoding="utf-8")
    try:
        body = text.split("<!-- CLIENT FACTS START -->", 1)[1].split(
            "<!-- CLIENT FACTS END -->", 1
        )[0]
    except IndexError as error:
        raise ClientFactsError(
            f"{path} has no CLIENT FACTS START/END markers, so nothing would be read and every "
            "lookup would silently report 'unknown question'."
        ) from error

    facts: dict[str, ClientFact] = {}
    for entry in _ENTRY.finditer(body):
        fields = {
            m.group("key"): " ".join(m.group("value").split())
            for m in _FIELD.finditer(entry.group("body"))
        }
        missing = {"status", "blocks", "issue", "answer", "source"} - fields.keys()
        if missing:
            raise ClientFactsError(
                f"{entry.group('ref')} is missing: {', '.join(sorted(missing))}. Every field is "
                "required — a fact without a source is an assertion."
            )
        # A mistyped `status` or `blocks` would otherwise raise ValueError, which `issue_gate.py`
        # does not catch — the gate would exit with a traceback instead of its controlled stop, and
        # a traceback is the one output nobody reads as "you may not start this yet".
        try:
            status = Status(fields["status"])
            blocks = Blocks(fields["blocks"])
        except ValueError as error:
            raise ClientFactsError(
                f"{entry.group('ref')} has an invalid field: {error}. `status` is ANSWERED or OPEN; "
                "`blocks` is formula, value or nothing."
            ) from error

        facts[entry.group("ref")] = ClientFact(
            ref=entry.group("ref"),
            title=entry.group("title").strip(),
            status=status,
            blocks=blocks,
            issue=fields["issue"],
            answer=fields["answer"],
            source=fields["source"],
        )

    if not facts:
        raise ClientFactsError(
            f"no entries parsed from {path}. Either the format changed or the file is empty, and "
            "either way every dependency lookup would wrongly report the question as unknown."
        )
    return facts


def lookup(ref: str, facts: dict[str, ClientFact] | None = None) -> ClientFact | None:
    """Find a question by its `Qn` reference, tolerating `Q5 (#13)` style text."""
    facts = facts if facts is not None else load()
    match = re.search(r"\bQ\d+\b", ref)
    return facts.get(match.group(0)) if match else None
