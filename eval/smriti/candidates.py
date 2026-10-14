"""A candidate Pariksha key case drawn from one reviewer decision, and the file that holds them (#1232).

The design is in `eval/smriti/__init__.py`. This module holds the shapes, the per-agent and per-table
counts, the "overturned automatic" listing, and the candidates file (`smriti-candidates/2`; a
`smriti-candidates/1` file is still read): written once, never overwritten, and carrying the hash
of its contents so an edited file is refused (the same rules as a Pariksha scorecard,
`eval/pariksha/scorecard.py`).

**v2 (#1267)** adds to each candidate `overturned` (the automatic answer a person's decision
replaced, or `None`), `mistake_needed` (`unanswered` on a correction or a typed value until a person
says "AI misread" or "the vendor drew it wrong"; `None` where it is not asked), `actor_status`
(`reviewer`, `unknown`, or `unchecked` in a v1 file) and `answer_source` (`decision`, `pdf-text`
or `ai-reading`), and to the file the records left out as
code's or the machine's own (`skipped`, per table and why) and which reviewer list was used.

Source: issues #1232, #1267 · Verification: `tests/eval/test_smriti_export.py`,
`tests/eval/test_smriti_confirm.py`
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Final

from eval.smriti.actors import UNCHECKED

__all__ = [
    "AGENT_ORDER",
    "CANDIDATES_FORMAT",
    "EARLIER_KINDS",
    "MISTAKE_ANSWERS",
    "MISTAKE_UNANSWERED",
    "NEEDS_CONFIRMATION",
    "READABLE_FORMATS",
    "UNCHECKED_ACTOR",
    "Candidate",
    "CandidateExport",
    "CandidatesFileError",
    "DecisionCount",
    "agent_counts",
    "agent_line",
    "overturned_listing",
    "read_candidates",
    "render_counts",
    "table_counts",
    "write_candidates",
]

CANDIDATES_FORMAT: Final = "smriti-candidates/2"
#: The formats `read_candidates` accepts: v1 files (#1232) read with the v2 fields at their defaults.
READABLE_FORMATS: Final = ("smriti-candidates/1", CANDIDATES_FORMAT)
#: Every candidate's status. Nothing in Smriti ever changes it: a confirmed case lives in the key a
#: person made from it, never as an edit of the candidate.
NEEDS_CONFIRMATION: Final = "needs-confirmation"
#: The agents in the order a report lists them; `none` is a decision no agent's judgment preceded
#: (a page the AI budget stopped before anything read it).
AGENT_ORDER: Final = ("akshara", "rekha", "milan", "bala", "vastu", "khoj", "kautilya", "none")
#: "AI misread, or the vendor drew it wrong?", asked on every correction and typed value (#1267):
#: unanswered until a person answers it with one of `MISTAKE_ANSWERS` when confirming. A vendor
#: error whose printed text the AI read correctly is a right case for the reader, not a wrong one.
MISTAKE_UNANSWERED: Final = "unanswered"
MISTAKE_ANSWERS: Final = ("ai_misread", "vendor_drew_it", "unsure")
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
    question: Mapping[str, Any] | None
    """The Pariksha question identity (`family`, `page_sha256`, `question_id`, and `slot_box` on a
    label, `picture_sha256` on a row, `row_box` on a walls row, `pieces` on a piece kind), or
    `None` when it cannot be built safely. The family names the key: `claude-slot-span` Akshara,
    `slot-row-choice` Rekha, `slot-walls` Rekha's walls, `piece-kind` Bala's kinds."""
    answer: Mapping[str, Any] | None
    """The `pariksha-key/1` answer when the decision states it exactly, else `None`."""
    answer_needed: str | None
    """What the person must state when confirming, when the decision does not give the answer."""
    not_convertible: str | None
    """Why this decision cannot become a key case; `None` = it can."""
    status: str = NEEDS_CONFIRMATION
    overturned: Mapping[str, Any] | None = None
    """The automatic result this decision replaced (`what`, `by`, `answer`, `record`): code's
    item, view role, pairing or wall layout, a page both AIs (or code) said has no countertop, or a
    reading both readers agreed on. `None` when it replaced nothing automatic. Report only."""
    mistake_needed: str | None = None
    """`unanswered` on a correction or a typed value (see `MISTAKE_ANSWERS`); `None` elsewhere."""
    actor_status: str = UNCHECKED
    """`reviewer` (on the local list), `unknown` (not on it, or no list), `unchecked` (a v1 file)."""
    answer_source: str | None = None
    """Where `answer` came from, so the confirming page can say so and show the picture first:
    `decision` (the reviewer's decision states it), `pdf-text` (the file's own text under the
    span) or `ai-reading` (the stored reading's text, on a confirmed reading with no file text).
    `None` when there is no answer."""

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
    skipped: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    """Records read and left out as the machine's own, per table: `code` (a `code:` actor) and
    `machine source` (an automatic, carried or deterministic record). Never silent."""
    reviewer_list: str | None = None
    """The local real-reviewer list used (its path), or `None` when none was given."""


@dataclass(frozen=True, slots=True)
class DecisionCount:
    """Reviewer decisions about one agent (or in one table), as exact counts."""

    decisions: int = 0
    """Current reviewer decisions (not superseded, not carried), each counted once."""
    corrected: int = 0
    filled: int = 0
    confirmed: int = 0
    decided: int = 0
    ready: int = 0
    """Cases convertible with the answer proposed: a person only confirms."""
    convertible: int = 0
    """Cases: ready ones plus those whose identity is built and whose answer the person states."""
    not_convertible: int = 0
    """Cases that can never become key cases."""
    earlier: int = 0
    """Superseded or carried records (decisions), not counted as decisions."""
    overturned: int = 0
    """Current decisions that replaced an automatic result (report only)."""


def _count(candidates: Iterable[Candidate], key: str) -> dict[str, DecisionCount]:
    """Decisions are counted once each, however many parts one has (a row's walls and widths, a
    run and its walls); convertible, ready and not convertible count cases, one per part."""
    tallies: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    parts: dict[tuple[str, str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        parts[(getattr(candidate, key), candidate.table, candidate.decision_id)].append(candidate)
    for (name, _table, _decision), group in parts.items():
        tally = tallies[name]
        current = [candidate for candidate in group if candidate.current]
        if not current:
            tally["earlier"] += 1
            continue
        tally["decisions"] += 1
        tally[current[0].kind] += 1
        if any(candidate.overturned is not None for candidate in current):
            tally["overturned"] += 1
        for candidate in current:
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
        f"convertible cases {count.convertible} (ready {count.ready}, "
        f"{count.convertible - count.ready} need a person's answer), "
        f"not convertible cases {count.not_convertible}; earlier records {count.earlier}; "
        f"overturned an automatic result {count.overturned}"
    )


def _ordered(names: Iterable[str], order: tuple[str, ...]) -> list[str]:
    return sorted(
        names, key=lambda name: (order.index(name) if name in order else len(order), name)
    )


def overturned_listing(export: CandidateExport) -> tuple[Candidate, ...]:
    """Every current decision that overturned an automatic result, newest first (report only).

    In production the only proof that an automatic result was wrong (an "agreed-wrong", which must
    stay 0) is a person undoing it, so each one is a line for a person to read. Nothing here
    switches anything off; that stays a human call, like switching on."""
    found = [c for c in export.candidates if c.current and c.overturned is not None]
    return tuple(sorted(found, key=lambda c: (c.decided_at, c.candidate_id), reverse=True))


def _overturn_line(candidate: Candidate) -> str:
    overturned = candidate.overturned or {}
    page = "page ?" if candidate.page_number is None else f"page {candidate.page_number}"
    return (
        f"    {candidate.decided_at[:10]} {candidate.agent}, {page}: {candidate.decided_by} "
        f"({candidate.actor_status}) overturned {overturned.get('what')} by "
        f"{overturned.get('by')} (it said {json.dumps(overturned.get('answer'))}); "
        f"{candidate.candidate_id}"
    )


def render_counts(export: CandidateExport) -> str:
    """The counts per agent and per table, for a person; every candidate needs their confirmation."""
    agents = agent_counts(export.candidates)
    tables = table_counts(export.candidates)
    reviewers = (
        f"real reviewers from {export.reviewer_list}"
        if export.reviewer_list is not None
        else "no real-reviewer list given, so no decision is convertible"
    )
    lines = [
        (
            f"Smriti candidates from {export.database} (code {export.code_version}): "
            f"{len(export.candidates)} records; every one needs a person's confirmation; "
            f"{reviewers}"
        ),
        "  by agent:",
        *(f"    {_line(name, agents[name])}" for name in _ordered(agents, AGENT_ORDER)),
        "  by table:",
        *(f"    {_line(name, tables[name])}" for name in sorted(tables)),
    ]
    if not export.candidates:
        lines.append("  no reviewer decisions are stored in this database")
    if export.skipped:
        lines.append("  left out as the machine's own decisions (never candidates):")
        lines.extend(
            f"    {table}: "
            + ", ".join(f"{why} {count}" for why, count in sorted(export.skipped[table].items()))
            for table in sorted(export.skipped)
        )
    overturned = overturned_listing(export)
    lines.append(
        f"  overturned automatic results (report only; nothing is switched off): {len(overturned)}"
    )
    lines.extend(_overturn_line(candidate) for candidate in overturned)
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
        name: {item.name: getattr(count, item.name) for item in fields(DecisionCount)}
        for name, count in counts.items()
    }


#: The candidate fields v2 added, absent from a v1 file.
_V2_FIELDS: Final = frozenset({"overturned", "mistake_needed", "actor_status", "answer_source"})


def _candidate_document(candidate: Candidate) -> dict[str, Any]:
    return {item.name: getattr(candidate, item.name) for item in fields(Candidate)}


#: Why a candidate whose decider was never checked against the real-reviewer list cannot convert.
UNCHECKED_ACTOR: Final = (
    "its decider was never checked against the local real-reviewer list (a file made before the "
    "list existed); export the candidates again"
)


def _unchecked(candidate: Candidate) -> Candidate:
    """A candidate read with no recorded reviewer check (a v1 file, or a v2 file without
    `actor_status`) is never convertible: who decided it may be a test script."""
    if candidate.actor_status != UNCHECKED or not candidate.convertible:
        return candidate
    return replace(candidate, not_convertible=UNCHECKED_ACTOR)


def write_candidates(export: CandidateExport, path: Path) -> None:
    """Write the candidates once; an existing file is never overwritten."""
    document: dict[str, Any] = {
        "format": CANDIDATES_FORMAT,
        "database": export.database,
        "exported_at": export.exported_at,
        "code_version": export.code_version,
        "reviewer_list": export.reviewer_list,
        "candidates": [_candidate_document(candidate) for candidate in export.candidates],
        "skipped": {table: dict(why) for table, why in export.skipped.items()},
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
    if not isinstance(document, dict) or document.get("format") not in READABLE_FORMATS:
        raise CandidatesFileError(f"{path.name} is not a {CANDIDATES_FORMAT} file")
    stored = document.pop("sha256", None)
    if stored != _digest(document):
        raise CandidatesFileError(
            f"{path.name} was changed after it was written (its hash no longer matches); export "
            "the candidates again instead of editing them, and confirm in a confirmations file"
        )
    # A v1 file has no v2 fields: they take their defaults (`actor_status` is then `unchecked`).
    required = {item.name for item in fields(Candidate) if item.name not in _V2_FIELDS}
    try:
        candidates = tuple(
            _unchecked(
                Candidate(
                    **{name: entry[name] for name in required},
                    **{name: entry[name] for name in _V2_FIELDS if name in entry},
                )
            )
            for entry in document["candidates"]
        )
        return CandidateExport(
            database=document["database"],
            exported_at=document["exported_at"],
            code_version=document["code_version"],
            candidates=candidates,
            skipped=document.get("skipped", {}),
            reviewer_list=document.get("reviewer_list"),
        )
    except (KeyError, TypeError) as error:
        raise CandidatesFileError(f"{path.name} is missing a candidate field: {error}") from error
