"""Pariksha over the key library: judge a change against every registered key of an agent (#1268).

**What it answers.** "May this change to a prompt, a model, a picture or the code ship?" Both
stored runs, the one BEFORE the change and the one AFTER it, are scored against **every** key the
library (`eval/smriti/library.py`) holds for the agent, through Pariksha's own loaders and scorers,
and each key is judged by that agent's own compare (`eval/pariksha/compare.py`; walls and piece
kinds by theirs, which know their extra reasons to reject). No model is called and both runs are
only read, in read-only transactions.

**The rule: better somewhere, worse nowhere** (Anant's "better with 0 new wrong", across a library;
decided 2026-10-15). **Per key**, one of:

* **ACCEPT**: the key's own compare raised no blocking reason (no new wrong, nothing no longer
  asked, none of its own extra reasons), the change is **better by the library's counts** (more
  right and no more wrong, or as many right and fewer wrong) and nothing is worse by them. A
  key's own compare counts only its own right and wrong (the walls compare: the ends, not the
  layouts), so both "better" and "worse" are the library's: a layout gain alone is accepted,
  and a layout, an automatic result, the ink or the site answer lost beside a gain rejects.
  The library is the stricter gate on purpose: a SEALED_WRONG walls layout counts as WRONG here,
  though #1250's own compare only shows it (Anant's rule is 0 new wrong; decided 2026-10-15).
* **NEUTRAL**: nothing got worse and nothing got better. A key the change did not touch says
  this, so a change aimed at one key (the walls question, say) is not rejected by the rows keys it
  left alone.
* **REJECT**: any new wrong, anything no longer asked, a fall in `asked`, the agent's own extra
  reasons (new wrong wall ink, a new wrong automatic layout), or anything worse.
  **Worse is judged on counts, never rates** (asking more questions with the same right answers
  moves every rate and is no loss): any case newly wrong (per case, whatever the totals say),
  fewer right, more wrong, fewer asked, a case no longer asked, fewer automatic results right, the walls' site and ink counts, more held or unsure on
  the cases both runs asked (unless wrong fell), or a higher cost per question when nothing got
  better (named in the reason). A case the older run never asked counts: its first answer can
  be the new wrong one. So
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
from fractions import Fraction
from typing import Final
from uuid import UUID

from sqlalchemy.orm import Session

from eval.pariksha import depth as pariksha_depth
from eval.pariksha import kinds as pariksha_kinds
from eval.pariksha import walls as pariksha_walls
from eval.pariksha.compare import Comparison, compare_scorecards
from eval.pariksha.depth import DEPTH_AGENTS, DepthKey
from eval.pariksha.keys import AnswerKey, ArchitectAnswerKey
from eval.pariksha.kinds import (
    KINDS_AGENT,
    PRODUCT,
    KindsKey,
    compare_kinds,
    load_stored_pieces,
    score_kinds,
)
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    EXTRA_HELD,
    EXTRA_USABLE,
    FOUND,
    HELD,
    MISSING,
    NOT_ASKED,
    SPLIT,
    CaseScore,
    Scorecard,
    score_akshara,
    score_rekha,
    score_vastu,
)
from eval.pariksha.stored import StoredRunError, load_architect_run, load_stored_run
from eval.pariksha.walls import (
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
    "OUTCOME_CLASSES",
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
    "unknown_outcomes",
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
#: How each scorecard type's case outcomes count when the library asks "worse?": right, wrong,
#: held (asked, neither right nor wrong: held, split, unsure, missing) or unmeasured (not asked,
#: not settled in the key, a row that could not be verified). Every type is listed explicitly;
#: an outcome missing here makes the key REFUSED, never quietly "held" (a new outcome word
#: could otherwise hide a loss).
RIGHT_CLASS: Final = "right"
WRONG_CLASS: Final = "wrong"
HELD_CLASS: Final = "held"
UNMEASURED_CLASS: Final = "unmeasured"
_PAIR_CLASSES: Final[Mapping[str, str]] = {
    AGREED_RIGHT: RIGHT_CLASS,
    AGREED_WRONG: WRONG_CLASS,
    NOT_ASKED: UNMEASURED_CLASS,
}
_DEPTH_CLASSES: Final[Mapping[str, str]] = {
    **_PAIR_CLASSES,
    pariksha_depth.HELD_AS_KEY: RIGHT_CLASS,  # the key expects a hold: the depth compare's right
    pariksha_depth.HELD: HELD_CLASS,
    pariksha_depth.DISAGREED: HELD_CLASS,
    pariksha_depth.ONE_UNSURE: HELD_CLASS,
    pariksha_depth.BOTH_UNSURE: HELD_CLASS,
    pariksha_depth.NO_ANSWER: HELD_CLASS,
    pariksha_depth.KEY_SINGLE_READ: UNMEASURED_CLASS,
    pariksha_depth.KEY_OPEN: UNMEASURED_CLASS,
}
OUTCOME_CLASSES: Final[Mapping[str, Mapping[str, str]]] = {
    "akshara": {**_PAIR_CLASSES, HELD: HELD_CLASS},
    "rekha": {**_PAIR_CLASSES, SPLIT: HELD_CLASS},
    "vastu": {
        FOUND: RIGHT_CLASS,
        EXTRA_USABLE: WRONG_CLASS,
        HELD: HELD_CLASS,
        MISSING: HELD_CLASS,
        EXTRA_HELD: HELD_CLASS,
        NOT_ASKED: UNMEASURED_CLASS,
    },
    WALLS_AGENT: {
        **_PAIR_CLASSES,
        pariksha_walls.DISAGREED: HELD_CLASS,
        pariksha_walls.ONE_UNSURE: HELD_CLASS,
        pariksha_walls.BOTH_UNSURE: HELD_CLASS,
        pariksha_walls.NO_ANSWER: HELD_CLASS,
        pariksha_walls.KEY_UNSURE: UNMEASURED_CLASS,
        # each row's layout, against the site layout
        pariksha_walls.AUTOMATIC_RIGHT: RIGHT_CLASS,
        pariksha_walls.SEALED_RIGHT: RIGHT_CLASS,
        pariksha_walls.AUTOMATIC_WRONG: WRONG_CLASS,
        pariksha_walls.SEALED_WRONG: WRONG_CLASS,
        pariksha_walls.HELD_LAYOUT: HELD_CLASS,
        "layout_not_keyed": UNMEASURED_CLASS,  # `score_walls`: the key gives no site layout
    },
    KINDS_AGENT: {
        pariksha_kinds.RIGHT: RIGHT_CLASS,
        pariksha_kinds.WRONG: WRONG_CLASS,
        pariksha_kinds.UNKNOWN: HELD_CLASS,
        pariksha_kinds.ROW_UNVERIFIED: UNMEASURED_CLASS,
        pariksha_kinds.NOT_SCORED: UNMEASURED_CLASS,
        NOT_ASKED: UNMEASURED_CLASS,
    },
    pariksha_depth.VIEW_AGENT: _DEPTH_CLASSES,
    pariksha_depth.DEPTH_AGENT: _DEPTH_CLASSES,
}
#: Counts a scorecard keeps only in its metrics, judged by count: (metric, worse when it ...).
#: The walls card scores each end against what the vendor drew (the case outcome) and, beside it,
#: against the site (`@site`), and code's wall ink as its own judgment.
_COUNTED_METRICS: Final[Mapping[str, tuple[tuple[str, str], ...]]] = {
    WALLS_AGENT: (
        ("pair_agreed_right@site", "fell"),
        ("pair_agreed_wrong@site", "rose"),
        ("ink_right", "fell"),
        ("ink_wrong", "rose"),
        ("ink_right@site", "fell"),
        ("ink_wrong@site", "rose"),
    ),
}
#: What Pariksha's loaders say when a run stored nothing of the kind asked for
#: (`eval/pariksha/stored._choose_run`); every other refusal stays a refusal.
_NOTHING_STORED: Final = "holds no stored"
_FROM_ANSWERS: Final = (
    "scored from a pariksha-depth-answers/1 file (scripts/pariksha_scorecard.py score --answers), "
    "not from a stored run: reading its answers from a stored run waits for the reader"
)
_SHOWN_PAGES: Final = 6
_LISTED_CASES: Final = 10
#: The one reason Pariksha's compare gives when nothing got worse and nothing got better.
_NOT_BETTER: Final = "REJECT (not better:"
#: The one rate judged for "worse": the cost per question, and only when it rises.
_COST: Final = "cost_micros_per_question"


def nothing_stored(error: StoredRunError) -> bool:
    """The run stored nothing of this kind: the key is unread there, not a refusal."""
    return _NOTHING_STORED in str(error)


def asked(card: Scorecard) -> int:
    """The cases this card measured: everything but not asked, unsettled or unverified."""
    classes = OUTCOME_CLASSES.get(card.agent, {})
    return sum(1 for case in card.cases if classes.get(case.outcome) != UNMEASURED_CLASS)


def unknown_outcomes(card: Scorecard) -> set[str]:
    """Outcomes this library cannot place as right, wrong, held or unmeasured."""
    classes = OUTCOME_CLASSES.get(card.agent, {})
    return {case.outcome for case in card.cases if case.outcome not in classes}


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


def _cost(card: Scorecard) -> Fraction | None:
    metric = card.metrics.get(_COST)
    return None if metric is None else metric.value


def _counted(card: Scorecard, metric: str) -> int | None:
    found = card.metrics.get(metric)
    return (
        None if found is None or found.value is None and found.denominator == 0 else found.numerator
    )


def _settled(agent: str, card: Scorecard) -> tuple[int, int]:
    """The card's right and wrong counts by the library's mapping (every outcome a type gives)."""
    classes = OUTCOME_CLASSES[agent]
    right = sum(1 for case in card.cases if classes[case.outcome] == RIGHT_CLASS)
    wrong = sum(1 for case in card.cases if classes[case.outcome] == WRONG_CLASS)
    return right, wrong


def _better(agent: str, before: Scorecard, after: Scorecard) -> bool:
    """Better by the library's counts: more right and no more wrong, or as many right and fewer
    wrong. A key's own compare may count less (the walls compare counts only the ends)."""
    (right_before, wrong_before), (right_after, wrong_after) = (
        _settled(agent, before),
        _settled(agent, after),
    )
    return (right_after > right_before and wrong_after <= wrong_before) or (
        right_after == right_before and wrong_after < wrong_before
    )


def _worse(agent: str, before: Scorecard, after: Scorecard, *, better: bool) -> list[str]:
    """What got worse, judged on COUNTS, never on rates (decided 2026-10-15).

    Asking more questions with the same right answers is not a loss, yet it moves every rate
    over `asked`; so a rate is never "worse" here. Worse is: fewer right, more wrong, fewer
    asked, a case no longer asked, any case newly wrong (per case: a right answer that became
    wrong, or a held one that did, even when the totals hold), more held or unsure on the cases
    both runs asked (not judged
    when the wrong count fell: wrong -> held is a real improvement), fewer automatic results
    right (walls: an automatic layout that became a sealed proposal or a hold is a loss even when
    it stays right), a fall or rise in the counts a card keeps only in its metrics (walls: against
    the site, and code's wall ink), or a higher cost per question when nothing got better (cost is
    the one rate judged, and only up). `better`: the change is better by the library's counts."""
    classes = OUTCOME_CLASSES[agent]
    was = {c.case_id: c.outcome for c in before.cases if classes[c.outcome] != UNMEASURED_CLASS}
    now = {c.case_id: c.outcome for c in after.cases if classes[c.outcome] != UNMEASURED_CLASS}

    def count(outcomes: dict[str, str], wanted: str) -> int:
        return sum(1 for outcome in outcomes.values() if classes[outcome] == wanted)

    found: list[str] = []
    # Anant's rule is per case: no case may become wrong, whatever the totals say (one wrong
    # fixed and another made wrong keeps "wrong 1 -> 1" while a case is newly wrong). A case the
    # older run never asked counts: its first answer can be the new wrong one.
    newly_wrong = sorted(
        case
        for case, outcome in now.items()
        if classes[outcome] == WRONG_CLASS
        and (case not in was or classes[was[case]] != WRONG_CLASS)
    )
    if newly_wrong:
        shown = ", ".join(newly_wrong[:_LISTED_CASES])
        more = (
            f", and {len(newly_wrong) - _LISTED_CASES} more"
            if len(newly_wrong) > _LISTED_CASES
            else ""
        )
        found.append(f"{len(newly_wrong)} newly wrong: {shown}{more}")
    right_before, right_after = count(was, RIGHT_CLASS), count(now, RIGHT_CLASS)
    if right_after < right_before:
        found.append(f"right {right_before} -> {right_after}")
    wrong_before, wrong_after = count(was, WRONG_CLASS), count(now, WRONG_CLASS)
    if wrong_after > wrong_before:
        found.append(f"wrong {wrong_before} -> {wrong_after}")
    if len(now) < len(was):
        found.append(f"asked {len(was)} -> {len(now)}")
    gone = sorted(set(was) - set(now))
    if gone:
        found.append(f"{len(gone)} no longer asked")
    both = set(was) & set(now)
    held_before = sum(1 for case in both if classes[was[case]] == HELD_CLASS)
    held_after = sum(1 for case in both if classes[now[case]] == HELD_CLASS)
    if held_after > held_before and wrong_after >= wrong_before:
        found.append(f"held or unsure on the same asked cases {held_before} -> {held_after}")
    if agent == WALLS_AGENT:
        automatic = pariksha_walls.AUTOMATIC_RIGHT
        auto_before = sum(1 for outcome in was.values() if outcome == automatic)
        auto_after = sum(1 for outcome in now.values() if outcome == automatic)
        if auto_after < auto_before:
            found.append(f"automatic layouts right {auto_before} -> {auto_after}")
    for metric, direction in _COUNTED_METRICS.get(agent, ()):
        old, new = _counted(before, metric), _counted(after, metric)
        if old is None or new is None:
            continue
        if (direction == "fell" and new < old) or (direction == "rose" and new > old):
            found.append(f"{metric} {old} -> {new}")
    cost_before, cost_after = _cost(before), _cost(after)
    if (
        not better
        and cost_before is not None
        and cost_after is not None
        and cost_after > cost_before
    ):
        found.append("cost per question rose with nothing better")
    return found


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
    unknown = sorted(unknown_outcomes(before_card) | unknown_outcomes(after_card))
    if unknown:
        return KeyResult(
            entry,
            REFUSED,
            f"outcome(s) {', '.join(unknown)} of a {entry.agent} scorecard are not known to the "
            "library as right, wrong, held or unmeasured: it will not guess (OUTCOME_CLASSES)",
            pages,
            unread,
        )
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
    # The key's own compare may block: any new wrong or anything no longer asked (its structured
    # result), or another reason it gives (a fall in `asked`, walls' new wrong ink or automatic
    # layout), which exists only in its verdict. So only its ACCEPT, or its exact "not better"
    # verdict alone, leaves the decision to the library's counts; any other wording stays REJECT
    # (fail safe; the wording is pinned by a test).
    only_not_better = (
        not comparison.accepted
        and not comparison.new_wrong
        and not comparison.dropped
        and comparison.verdict.startswith(_NOT_BETTER)
        and "; " not in comparison.verdict
    )
    if not comparison.accepted and not only_not_better:
        return KeyResult(entry, REJECT, comparison.verdict, pages, unread, comparison)
    # "Better" and "worse" both by the library's counts, for every scorecard type: a key's own
    # compare counts only its own right and wrong (the walls compare: the ends, not the layouts).
    better = _better(entry.agent, before_card, after_card)
    worse = _worse(entry.agent, before_card, after_card, better=better)
    right_before, wrong_before = _settled(entry.agent, before_card)
    right_after, wrong_after = _settled(entry.agent, after_card)
    counts = f"right {right_before} -> {right_after}, wrong {wrong_before} -> {wrong_after}"
    if worse:
        if comparison.accepted:
            reason = (
                f"REJECT (its own compare says {comparison.verdict}, but by the library's "
                f"counts: worse: {'; '.join(worse)})"
            )
        else:
            reason = f"{comparison.verdict[:-1]}; worse: {'; '.join(worse)})"
        return KeyResult(entry, REJECT, reason, pages, unread, comparison)
    if better:
        reason = (
            comparison.verdict
            if comparison.accepted
            else f"ACCEPT (better by the library's counts: {counts}; 0 new wrong; its own "
            f"compare: {comparison.verdict})"
        )
        return KeyResult(entry, ACCEPT, reason, pages, unread, comparison)
    if comparison.accepted:
        reason = (
            f"NEUTRAL (its own compare says {comparison.verdict}, but by the library's counts "
            f"nothing is better: {counts})"
        )
    else:
        # 0 new wrong, nothing no longer asked, no fall in `asked`, nothing worse: not a loss.
        reason = "NEUTRAL (" + comparison.verdict.removeprefix("REJECT (")
    return KeyResult(entry, NEUTRAL, reason, pages, unread, comparison)


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
