"""Pariksha over the key library: judge a change against every registered key of an agent (#1268).

**What it answers.** "May this change to a prompt, a model, a picture or the code ship?" Both
stored runs, the one BEFORE the change and the one AFTER it, are scored against **every** key the
library (`eval/smriti/library.py`) holds for the agent, through Pariksha's own loaders and scorers,
and each key is judged by that agent's own compare (`eval/pariksha/compare.py`; walls and piece
kinds by theirs, which know their extra reasons to reject). No model is called and both runs are
only read, in read-only transactions.

**The rule: better somewhere, worse nowhere** (Anant's "better with 0 new wrong", across a library;
decided 2026-10-15). **Per key**, one of:

* **ACCEPT**: the key's own compare accepts (better, 0 new wrong).
* **NEUTRAL**: nothing got worse and nothing got better: 0 new wrong, nothing no longer asked, no
  fall in `asked`, no metric worse. A key the change did not touch says this, so a change aimed at
  one key (the walls question, say) is not rejected by the rows keys it left alone.
* **REJECT**: any new wrong, anything no longer asked, a fall in `asked`, the agent's own extra
  reasons (new wrong wall ink, a new wrong automatic layout), or not better with any metric worse
  (named). A case the older run never asked counts: its first answer can be the new wrong one. So
  a run that read nothing of this kind is scored as one that asked nothing, and a run that stopped
  reading a page is rejected: not asking is never a way to stop being wrong.
* **NOT MEASURED, never a pass**: neither run read any of the key's pages (the pages needed are
  listed), neither run stored anything of its kind, the pages were read but none of its questions
  asked, or the key is scored from an answers file rather than a stored run (the phase-5 depths).
  A measured key whose other pages neither run read lists those pages as not measured too.
* **REFUSED**: the compare will not judge the two (unreadable stored answers, another document).

**Overall**, exit code in brackets: **REJECT** (1) when any key rejects; else **REFUSED** (2) when
any key was refused; else **NOT MEASURED** (3) when no key was measured; else **ACCEPT** (0) when at
least one measured key is better (the rest unchanged); else **NEUTRAL** (4): nothing better, nothing
worse, so no reason to switch. The keys and pages that were not measured are named beside every
verdict. The verdict is printed; nothing here writes a setting, a prompt id or an environment
value, and no product code imports this (`tests/eval/test_smriti_library_guard.py`). Switching
stays a person's decision.

Source: issue #1268 · Verification: `tests/eval/test_smriti_library_compare.py`,
`tests/scripts/test_pariksha_library.py`
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from eval.pariksha.compare import Comparison, compare_scorecards
from eval.pariksha.depth import DEPTH_AGENTS, DepthKey
from eval.pariksha.keys import AnswerKey, ArchitectAnswerKey
from eval.pariksha.kinds import (
    KINDS_AGENT,
    NOT_SCORED,
    PRODUCT,
    ROW_UNVERIFIED,
    KindsKey,
    compare_kinds,
    load_stored_pieces,
    score_kinds,
)
from eval.pariksha.score import (
    NOT_ASKED,
    CaseScore,
    Scorecard,
    score_akshara,
    score_rekha,
    score_vastu,
)
from eval.pariksha.stored import StoredRunError, load_architect_run, load_stored_run
from eval.pariksha.walls import (
    KEY_UNSURE,
    WALLS_AGENT,
    WallsKey,
    compare_walls,
    load_stored_walls,
    score_walls,
)
from eval.regression import IncomparableRuns
from eval.smriti.library import LibraryEntry, LoadedKey, key_pages

__all__ = [
    "ACCEPT",
    "COMPARES",
    "EXIT_CODES",
    "NEUTRAL",
    "NOT_MEASURED",
    "REFUSED",
    "REJECT",
    "KeyResult",
    "LibraryVerdict",
    "Side",
    "StoredRunRef",
    "asked",
    "judge_key",
    "judge_library",
    "measure",
    "nothing_stored",
    "unmeasurable",
]

ACCEPT: Final = "ACCEPT"
REJECT: Final = "REJECT"
NOT_MEASURED: Final = "NOT MEASURED"
REFUSED: Final = "REFUSED"
NEUTRAL: Final = "NEUTRAL"
EXIT_CODES: Final[Mapping[str, int]] = {
    ACCEPT: 0,
    REJECT: 1,
    REFUSED: 2,
    NOT_MEASURED: 3,
    NEUTRAL: 4,
}

#: Each agent's own compare: walls and piece kinds have extra reasons to reject that the general
#: compare does not know (new wrong wall ink, a new wrong automatic layout, an unverified row).
COMPARES: Final[Mapping[str, Callable[[Scorecard, Scorecard], Comparison]]] = {
    "akshara": compare_scorecards,
    "rekha": compare_scorecards,
    "vastu": compare_scorecards,
    WALLS_AGENT: compare_walls,
    KINDS_AGENT: compare_kinds,
}
#: Outcomes that say the case was not measured in this run (not asked, not settled in the key,
#: a row that could not be verified, a key word that names no kind).
_UNMEASURED: Final = frozenset({NOT_ASKED, KEY_UNSURE, ROW_UNVERIFIED, NOT_SCORED})
#: What Pariksha's loaders say when a run stored nothing of the kind asked for
#: (`eval/pariksha/stored._choose_run`); every other refusal stays a refusal.
_NOTHING_STORED: Final = "holds no stored"
_FROM_ANSWERS: Final = (
    "scored from a pariksha-depth-answers/1 file (scripts/pariksha_scorecard.py score --answers), "
    "not from a stored run: reading its answers from a stored run waits for the reader"
)
_SHOWN_PAGES: Final = 6
#: The one reason Pariksha's compare gives when nothing got worse and nothing got better.
_NOT_BETTER: Final = "REJECT (not better:"


def nothing_stored(error: StoredRunError) -> bool:
    """The run stored nothing of this kind: the key is unread there, not a refusal."""
    return _NOTHING_STORED in str(error)


def asked(card: Scorecard) -> int:
    """The cases this card measured: everything but not asked, unsettled or unverified."""
    return sum(1 for case in card.cases if case.outcome not in _UNMEASURED)


def _nothing_asked(card: Scorecard, note: str) -> Scorecard:
    """The card of a run that read nothing of this kind, shaped like the other run's card."""
    return replace(
        card,
        metrics={},
        cases=tuple(
            # As Pariksha's own "not asked" case: no readers, no cost.
            CaseScore(
                case_id=case.case_id,
                question=case.question,
                expected=case.expected,
                outcome=NOT_ASKED,
                note=f"not asked: {note}",
            )
            for case in card.cases
        ),
    )


@dataclass(frozen=True, slots=True)
class Side:
    """What one stored run gave for one key."""

    card: Scorecard | None
    """`None` when the run stored nothing of this kind (see `note`)."""
    pages: frozenset[str]
    """The key's pages this run read."""
    note: str = ""


@dataclass(frozen=True, slots=True)
class KeyResult:
    entry: LibraryEntry
    status: str
    reason: str
    key_pages: frozenset[str]
    unread: tuple[str, ...]
    """The key's pages neither run read: not measured, and needed to measure them."""
    comparison: Comparison | None = None

    @property
    def measured(self) -> bool:
        return self.status in (ACCEPT, REJECT, NEUTRAL)


def _pages(pages: Sequence[str]) -> str:
    shown = ", ".join(page[:12] for page in pages[:_SHOWN_PAGES])
    more = f", and {len(pages) - _SHOWN_PAGES} more" if len(pages) > _SHOWN_PAGES else ""
    return shown + more


def judge_key(
    entry: LibraryEntry,
    pages: frozenset[str],
    before: Side,
    after: Side,
    *,
    unmeasurable: str = "",
) -> KeyResult:
    """One key's verdict on the change: ACCEPT, REJECT, NOT MEASURED or REFUSED."""
    unread = tuple(sorted(pages - before.pages - after.pages))
    if unmeasurable:
        return KeyResult(entry, NOT_MEASURED, unmeasurable, pages, tuple(sorted(pages)))
    if before.card is None and after.card is None:
        why = "; ".join(sorted({note for note in (before.note, after.note) if note}))
        return KeyResult(
            entry,
            NOT_MEASURED,
            f"neither run stored anything of its kind ({why}); pages needed: {_pages(unread)}",
            pages,
            unread,
        )
    if before.card is None:
        assert after.card is not None
        before_card, after_card = _nothing_asked(after.card, before.note), after.card
    elif after.card is None:
        before_card, after_card = before.card, _nothing_asked(before.card, after.note)
    else:
        before_card, after_card = before.card, after.card
    if asked(before_card) == 0 and asked(after_card) == 0:
        if unread == tuple(sorted(pages)):
            reason = (
                f"neither run read any of its {len(pages)} page(s); pages needed: {_pages(unread)}"
            )
        else:
            reason = (
                "its pages were read, but none of its questions asked in either run (another "
                "row, picture or document)"
                + (f"; pages needed: {_pages(unread)}" if unread else "")
            )
        return KeyResult(entry, NOT_MEASURED, reason, pages, unread)
    try:
        comparison = COMPARES[entry.agent](before_card, after_card)
    except IncomparableRuns as error:
        return KeyResult(entry, REFUSED, str(error), pages, unread)
    if comparison.accepted:
        return KeyResult(entry, ACCEPT, comparison.verdict, pages, unread, comparison)
    worse = [delta.metric for delta in comparison.metrics if delta.regressed]
    only_not_better = comparison.verdict.startswith(_NOT_BETTER) and "; " not in comparison.verdict
    if only_not_better and not worse:
        # 0 new wrong, nothing no longer asked, no fall in `asked`, no metric worse: not a loss.
        reason = "NEUTRAL (" + comparison.verdict.removeprefix("REJECT (")
        return KeyResult(entry, NEUTRAL, reason, pages, unread, comparison)
    reason = comparison.verdict
    if only_not_better:
        reason = f"{reason[:-1]}; worse: {', '.join(worse)})"
    return KeyResult(entry, REJECT, reason, pages, unread, comparison)


@dataclass(frozen=True, slots=True)
class LibraryVerdict:
    agent: str
    results: tuple[KeyResult, ...]
    status: str
    verdict: str

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    def render(self, *, detail: bool = False) -> str:
        lines = [f"Pariksha library compare: {self.agent} ({len(self.results)} registered key(s))"]
        for result in self.results:
            entry = result.entry
            lines += [
                "",
                f"[{result.status}] {entry}, {entry.cases} case(s)",
                f"  key: confirmed by {entry.confirmed_by}; {entry.label}",
                f"  {result.reason}",
            ]
            if result.measured and result.unread:
                lines.append(
                    f"  NOT MEASURED on {len(result.unread)} of {len(result.key_pages)} page(s) "
                    f"neither run read; pages needed: {_pages(result.unread)}"
                )
            if detail and result.comparison is not None:
                lines += ["", *("    " + line for line in result.comparison.render().splitlines())]
        lines += ["", self.verdict]
        return "\n".join(lines)


def judge_library(agent: str, results: Sequence[KeyResult]) -> LibraryVerdict:
    """The change's verdict over every key: REJECT, REFUSED, NOT MEASURED or ACCEPT."""
    measured = [result for result in results if result.measured]
    unmeasured = [result for result in results if result.status == NOT_MEASURED]
    pages_unread = sum(len(result.unread) for result in measured)
    rejecting = [result for result in results if result.status == REJECT]
    refused = [result for result in results if result.status == REFUSED]
    named = "; ".join
    if rejecting:
        status = REJECT
        verdict = (
            f"REJECT ({len(rejecting)} key(s) reject: "
            + named(
                f"{result.entry.name} {result.entry.version}: {result.reason}"
                for result in rejecting
            )
            + ")"
        )
    elif refused:
        status = REFUSED
        verdict = (
            f"REFUSED ({len(refused)} key(s) could not be judged: "
            + named(
                f"{result.entry.name} {result.entry.version}: {result.reason}" for result in refused
            )
            + ")"
        )
    elif not results:
        status = NOT_MEASURED
        verdict = f"NOT MEASURED (the library holds no key for {agent})"
    elif not measured:
        status = NOT_MEASURED
        verdict = (
            f"NOT MEASURED (none of the {len(results)} key(s) was measured by these runs: "
            "never a pass)"
        )
    else:
        better = [result for result in measured if result.status == ACCEPT]
        neutral = len(measured) - len(better)
        unmeasured_note = (
            f"; NOT MEASURED: {len(unmeasured)} key(s) and {pages_unread} page(s) of measured "
            "keys, listed above, never counted as a pass"
            if unmeasured or pages_unread
            else ""
        )
        if better:
            status = ACCEPT
            verdict = (
                f"ACCEPT (better on {len(better)} key(s), worse on none: {len(better)} accept "
                f"and {neutral} unchanged of {len(measured)} measured, 0 new wrong; "
                f"{len(measured)} of {len(results)} keys measured{unmeasured_note})"
            )
        else:
            status = NEUTRAL
            verdict = (
                f"NEUTRAL (nothing better, nothing worse: all {len(measured)} measured key(s) "
                f"unchanged; {len(measured)} of {len(results)} keys measured{unmeasured_note}). "
                "Not a reason to switch"
            )
    return LibraryVerdict(agent=agent, results=tuple(results), status=status, verdict=verdict)


# --- reading and scoring the stored runs ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredRunRef:
    """One side of the change: its database, its extraction run(s) and its code version."""

    database: str
    code_version: str
    runs: Mapping[str, UUID]
    """Key agent → the extraction run to read; the empty name is every other agent's run. An
    agent with none lets the loader pick the database's only run (or refuse, listing them)."""

    def run_for(self, agent: str) -> UUID | None:
        return self.runs.get(agent, self.runs.get(""))


def measure(
    session: Session,
    entry: LibraryEntry,
    key: LoadedKey,
    ref: StoredRunRef,
    *,
    kind_source: str = PRODUCT,
    scorer_version: str = "unknown",
) -> Side:
    """One stored run scored against one key, read-only; a run with nothing of its kind is a
    side with no card (any other refusal is raised)."""
    run = ref.run_for(entry.agent)
    pages = key_pages(key)
    common = {"code_version": ref.code_version, "scorer_version": scorer_version}
    try:
        if isinstance(key, ArchitectAnswerKey):
            spans = load_architect_run(
                session,
                database=ref.database,
                extraction_run_id=run,
                document_sha256=key.document_sha256,
            )
            # The architect reader reads a whole document: every keyed page of it was read.
            return Side(card=score_vastu(key, spans, **common), pages=pages)
        if isinstance(key, WallsKey):
            walls = load_stored_walls(session, database=ref.database, extraction_run_id=run)
            read = pages & {question.identity.page_sha256 for question in walls.questions}
            return Side(card=score_walls(key, walls, **common), pages=read)
        if isinstance(key, KindsKey):
            pieces = load_stored_pieces(session, database=ref.database, extraction_run_id=run)
            read = pages & {piece.page_sha256 for piece in pieces.pieces}
            return Side(card=score_kinds(key, pieces, source=kind_source, **common), pages=read)
        if isinstance(key, DepthKey):
            raise TypeError(f"a {key.agent} key is {_FROM_ANSWERS}")
        assert isinstance(key, AnswerKey)
        stored = load_stored_run(
            session, agent=key.agent, database=ref.database, extraction_run_id=run
        )
        read = pages & {question.identity.page_sha256 for question in stored.questions}
        scorer = score_akshara if key.agent == "akshara" else score_rekha
        return Side(card=scorer(key, stored, **common), pages=read)
    except StoredRunError as error:
        if not nothing_stored(error):
            raise
        return Side(card=None, pages=frozenset(), note=str(error))
    finally:
        session.rollback()


def unmeasurable(entry: LibraryEntry) -> str:
    """Why this key can never be measured from a stored run, or "" when it can."""
    return _FROM_ANSWERS if entry.agent in DEPTH_AGENTS else ""
