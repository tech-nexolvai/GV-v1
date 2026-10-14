"""A candidate Pariksha key case drawn from one reviewer decision, and the file that holds them (#1232).

The design is in `eval/smriti/__init__.py`. This module holds the shapes, the per-agent and per-table
counts, and the candidates file (`smriti-candidates/1`): written once, never overwritten, and
carrying the hash of its contents so an edited file is refused (the same rules as a Pariksha
scorecard, `eval/pariksha/scorecard.py`).

Source: issue #1232 · Verification: `tests/eval/test_smriti_export.py`,
`tests/eval/test_smriti_confirm.py`
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any, Final

__all__ = [
    "AGENT_ORDER",
    "CANDIDATES_FORMAT",
    "EARLIER_KINDS",
    "NEEDS_CONFIRMATION",
    "Candidate",
    "CandidateExport",
    "CandidatesFileError",
    "DecisionCount",
    "agent_counts",
    "agent_line",
    "read_candidates",
    "render_counts",
    "table_counts",
    "write_candidates",
]

CANDIDATES_FORMAT: Final = "smriti-candidates/1"
#: Every candidate's status. Nothing in Smriti ever changes it: a confirmed case lives in the key a
#: person made from it, never as an edit of the candidate.
NEEDS_CONFIRMATION: Final = "needs-confirmation"
#: The agents in the order a report lists them; `none` is a decision no agent's judgment preceded
#: (a page the AI budget stopped before anything read it).
AGENT_ORDER: Final = ("akshara", "rekha", "milan", "bala", "vastu", "none")
#: Kinds of record that are not a current decision: a person's earlier answer they later changed,
#: and a decision copied from an earlier revision (the original is its own candidate).
EARLIER_KINDS: Final = frozenset({"superseded", "carried"})


class CandidatesFileError(ValueError):
    """The file is not a `smriti-candidates/1` file, or it was changed after it was written."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """One reviewer decision (or one part of one), as a key case a person may confirm."""

    candidate_id: str
    """`<table>:<decision id>`, with `:<part>` when one decision holds several (walls, widths)."""
    table: str
    decision_id: str
    agent: str
    """`akshara`, `rekha`, `milan`, `bala`, `vastu` or `none`."""
    corrects: str
    """What the decision is about, in plain words."""
    kind: str
    """`corrected` (changed the machine's answer), `filled` (gave a value the readers held),
    `confirmed` (agreed with it), `decided` (the record does not say which), or `superseded` /
    `carried` (not a current decision)."""
    decided_by: str
    decided_at: str
    page_sha256: str | None
    page_number: int | None
    """1-based, as a person counts pages."""
    recorded: Mapping[str, Any]
    """What the reviewer recorded, for the person confirming."""
    question: Mapping[str, str] | None
    """The Pariksha question identity (`family`, `page_sha256`, `question_id`, and `slot_box` on a
    label or `picture_sha256` on a row), or `None` when it cannot be built safely."""
    answer: Mapping[str, Any] | None
    """The `pariksha-key/1` answer when the decision states it exactly, else `None`."""
    answer_needed: str | None
    """What the person must state when confirming, when the decision does not give the answer."""
    not_convertible: str | None
    """Why this decision cannot become a key case; `None` = it can."""
    status: str = NEEDS_CONFIRMATION

    @property
    def convertible(self) -> bool:
        return self.not_convertible is None

    @property
    def current(self) -> bool:
        return self.kind not in EARLIER_KINDS


@dataclass(frozen=True, slots=True)
class CandidateExport:
    database: str
    exported_at: str
    code_version: str
    """The commit of the exporter, or `unknown`."""
    candidates: tuple[Candidate, ...]


@dataclass(frozen=True, slots=True)
class DecisionCount:
    """Reviewer decisions about one agent (or in one table), as exact counts."""

    decisions: int = 0
    """Current reviewer decisions (not superseded, not carried)."""
    corrected: int = 0
    filled: int = 0
    confirmed: int = 0
    decided: int = 0
    ready: int = 0
    """Convertible with the answer the decision states: a person only confirms."""
    convertible: int = 0
    """Ready ones plus those whose identity is built and whose answer the person states."""
    not_convertible: int = 0
    earlier: int = 0
    """Superseded or carried records, not counted as decisions."""


def _count(candidates: Iterable[Candidate], key: str) -> dict[str, DecisionCount]:
    tallies: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for candidate in candidates:
        tally = tallies[getattr(candidate, key)]
        if not candidate.current:
            tally["earlier"] += 1
            continue
        tally["decisions"] += 1
        tally[candidate.kind] += 1
        if candidate.convertible:
            tally["convertible"] += 1
            if candidate.answer is not None:
                tally["ready"] += 1
        else:
            tally["not_convertible"] += 1
    return {name: DecisionCount(**tally) for name, tally in tallies.items()}


def agent_counts(candidates: Iterable[Candidate]) -> dict[str, DecisionCount]:
    """Per agent: reviewer decisions, how many changed its answer, and how many can become cases."""
    return _count(candidates, "agent")


def table_counts(candidates: Iterable[Candidate]) -> dict[str, DecisionCount]:
    return _count(candidates, "table")


def _line(name: str, count: DecisionCount) -> str:
    return (
        f"{name}: {count.decisions} decisions ({count.corrected} corrected, {count.filled} filled "
        f"a held value, {count.confirmed} confirmed, {count.decided} other decisions); "
        f"convertible {count.convertible} (ready {count.ready}, "
        f"{count.convertible - count.ready} need the printed text), "
        f"not convertible {count.not_convertible}; earlier records {count.earlier}"
    )


def _ordered(names: Iterable[str], order: tuple[str, ...]) -> list[str]:
    return sorted(
        names, key=lambda name: (order.index(name) if name in order else len(order), name)
    )


def render_counts(export: CandidateExport) -> str:
    """The counts per agent and per table, for a person; every candidate needs their confirmation."""
    agents = agent_counts(export.candidates)
    tables = table_counts(export.candidates)
    lines = [
        (
            f"Smriti candidates from {export.database} (code {export.code_version}): "
            f"{len(export.candidates)} records; every one needs a person's confirmation"
        ),
        "  by agent:",
        *(f"    {_line(name, agents[name])}" for name in _ordered(agents, AGENT_ORDER)),
        "  by table:",
        *(f"    {_line(name, tables[name])}" for name in sorted(tables)),
    ]
    if not export.candidates:
        lines.append("  no reviewer decisions are stored in this database")
    return "\n".join(lines)


def agent_line(export: CandidateExport, agent: str) -> str:
    """One line for Pariksha's scorecard report: the reviewer decisions about this agent."""
    count = agent_counts(export.candidates).get(agent, DecisionCount())
    return (
        f"  reviewer decisions on {agent}: {count.decisions} in {export.database} "
        f"({count.corrected} corrected, {count.filled} filled a held value, {count.confirmed} "
        f"confirmed, {count.decided} other); {count.convertible} could become key cases once a "
        "person confirms them (Smriti)"
    )


# --- the file -------------------------------------------------------------------------------------


def _digest(document: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _counts_document(counts: Mapping[str, DecisionCount]) -> dict[str, dict[str, int]]:
    return {
        name: {field.name: getattr(count, field.name) for field in fields(DecisionCount)}
        for name, count in counts.items()
    }


def _candidate_document(candidate: Candidate) -> dict[str, Any]:
    return {field.name: getattr(candidate, field.name) for field in fields(Candidate)}


def write_candidates(export: CandidateExport, path: Path) -> None:
    """Write the candidates once; an existing file is never overwritten."""
    document: dict[str, Any] = {
        "format": CANDIDATES_FORMAT,
        "database": export.database,
        "exported_at": export.exported_at,
        "code_version": export.code_version,
        "candidates": [_candidate_document(candidate) for candidate in export.candidates],
        "counts": {
            "by_agent": _counts_document(agent_counts(export.candidates)),
            "by_table": _counts_document(table_counts(export.candidates)),
        },
    }
    document["sha256"] = _digest(document)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(document, handle, indent=1, ensure_ascii=False)
        handle.write("\n")


def read_candidates(path: Path) -> CandidateExport:
    """Read a candidates file, refusing another format or a file changed after it was written."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CandidatesFileError(f"{path.name} is not JSON: {error}") from error
    if not isinstance(document, dict) or document.get("format") != CANDIDATES_FORMAT:
        raise CandidatesFileError(f"{path.name} is not a {CANDIDATES_FORMAT} file")
    stored = document.pop("sha256", None)
    if stored != _digest(document):
        raise CandidatesFileError(
            f"{path.name} was changed after it was written (its hash no longer matches); export "
            "the candidates again instead of editing them, and confirm in a confirmations file"
        )
    names = {field.name for field in fields(Candidate)}
    try:
        candidates = tuple(
            Candidate(**{name: entry[name] for name in names}) for entry in document["candidates"]
        )
        return CandidateExport(
            database=document["database"],
            exported_at=document["exported_at"],
            code_version=document["code_version"],
            candidates=candidates,
        )
    except (KeyError, TypeError) as error:
        raise CandidatesFileError(f"{path.name} is missing a candidate field: {error}") from error
