"""Smriti's learner report: per agent, what its AIs answered and what people later decided (#1272).

**Why.** Smriti's exporter (`eval/smriti/export.py`) lists every person decision as a candidate key
case. This report puts those decisions beside the agent they judge, so a person can see, per agent:

* **the AI answers on record**: the calls in `model_invocations`, per prompt family;
* **the person decisions** about it (the exporter's current decisions; code's are never a person's);
* **corrections**, split into "the AI misread it" and "the vendor drew it wrong" (and those not
  answered yet: the exporter asks every correction, and the answer comes when a person confirms);
* **overturned automatic results**: a person undoing what code or both AIs decided by themselves;
* **`eval/correction_rate.py`'s reviewed-reading rates**, mapped from the reader to its agent;
* **gate usefulness**: for each item an inspector stopped (`reading_events`), what the person did
  later on that item. Corrected it: a useful stop. Confirmed the AI's answer: a needless stop.

**Only real reviewers count (#1272 review).** Rates, overturned counts and gate verdicts use only
decisions by names on the local real-reviewer list (`actor_status` `reviewer`, from
`GV_SMRITI_REVIEWERS`). Most "person" rows in our copies were written by test scripts, so every
other name is listed apart and never counted; with no list given, every rate is NOT MEASURED and
says so. **A batch click is never counted:** a page answered in one "confirm these N pages" click
is listed apart (each page still needs its own look), never as N confirms or N needless stops.

**Which stops are an inspector's.** A page the AI budget left unread (`budget-reached`) or a
question the budget never asked (`budget-not-asked`) is the budget's stop, not an inspector's, and a
page code proved blank waits for nobody: both are counted apart and never judged. A decision is
matched to a stop by the stop's file (document version) and page, never by page content alone (two
files may hold the same page).

**NOT MEASURED, never 0%.** A rate whose denominator is 0 says `NOT MEASURED` and why. So does the
"AI misread" rate while any correction still waits for its misread-or-vendor answer: an exact rate
or none. A measured rate always shows its numerator and denominator.

**Read-only, $0.** The exporter makes the transaction read-only before its first query, and every
query here runs after it in the same transaction. No model is called. Nothing is switched on or
off: what to do about a number stays a person's call.

**Built on the exporter's public output.** Which decisions are current (a later correction replaces
an earlier confirm; superseded and carried records never count), who decided, and what each
decision is (`kind`, `overturned`, `mistake_needed`) all come from `export_candidates`, so a fix
there flows through here. This module only counts and joins.

Source: issue #1272; design `phase6-smriti-design.md` §4C, §4D (kept local) · Verification:
`tests/eval/test_smriti_report.py`
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final
from uuid import UUID

from sqlalchemy import Row, func, select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import (
    CanonicalObservation,
    NoCountertopAnswer,
    ObservationCandidate,
    SlotRowReviewDecision,
)
from app.models.review import CorrectionLedgerEntry, ReviewAction
from app.models.runs import READING_FINISHED_KIND, ModelInvocation, ReadingEvent
from app.models.verdicts import Finding
from eval.correction_rate import CorrectionRateKey, correction_rate
from eval.metrics import MetricResult
from eval.pariksha.stored import prompt_family
from eval.smriti.actors import REVIEWER, Reviewers, is_code_actor
from eval.smriti.candidates import (
    AGENT_ORDER,
    MISTAKE_UNANSWERED,
    Candidate,
    CandidateExport,
    DecisionCount,
    agent_counts,
    overturned_listing,
)
from eval.smriti.export import export_candidates

__all__ = [
    "AGENT_FAMILIES",
    "EXTRACTOR_AGENTS",
    "NEEDLESS",
    "NOT_MEASURED",
    "REPORT_AGENTS",
    "UNDETERMINED",
    "USEFUL",
    "WAITING",
    "AgentReport",
    "AiAnswers",
    "Corrections",
    "GateOutcome",
    "LearnerReport",
    "Rate",
    "StoppedItem",
    "build_report",
    "classify_stop",
    "learner_report",
    "render_report",
    "stopped_items",
]

NOT_MEASURED: Final = "NOT MEASURED"

#: Every agent the report lists, in the order of the review line, always: an agent with nothing on
#: record is shown as NOT MEASURED, never left out. `none` holds answers on pages the AI budget
#: stopped before any agent judged them.
REPORT_AGENTS: Final = (
    "bala",
    "rekha",
    "akshara",
    "vastu",
    "milan",
    "kautilya",
    "khoj",
    "vani",
    "sahay",
    "none",
)
ROLES: Final[Mapping[str, str]] = {
    "bala": "the page sorter",
    "rekha": "the line finder",
    "akshara": "the number reader",
    "vastu": "the architect reader",
    "milan": "the matcher",
    "kautilya": "the planner",
    "khoj": "the value finder",
    "vani": "the writer",
    "sahay": "the assistant",
    "none": "no agent: pages the AI budget stopped before any agent judged them",
}

#: Which agent asks each prompt family (the prompt id without its version, `prompt_family`). Only
#: the families the agent stack names; any other family is listed as "no agent named", never
#: guessed into one. The architect reader (Vastu) and the planner (Kautilya) ask no AI today.
AGENT_FAMILIES: Final[Mapping[str, frozenset[str]]] = {
    "akshara": frozenset({"claude-slot-span", "slot-crop", "depth-span"}),
    "rekha": frozenset(
        {
            "slot-row-choice",
            "slot-walls",
            "slot-walls-drawing-clues",
            "claude-counter-break",
            "view-kind",
        }
    ),
    "milan": frozenset({"arch-pair", "arch-pair-2panel", "arch-view-match"}),
    "vani": frozenset({"findings-composer"}),
    "sahay": frozenset({"review-assistant", "reviewer-chat"}),
}
_FAMILY_AGENT: Final[Mapping[str, str]] = {
    family: agent for agent, families in AGENT_FAMILIES.items() for family in families
}

#: Which agent's reader each extractor of `eval/correction_rate.py` is: the slot reader runs under
#: the form-reader extractor (Akshara), the architect reader under `architect-text` (Vastu). Other
#: extractors (the file's own text, OCR, older readers) are listed apart, never guessed.
EXTRACTOR_AGENTS: Final[Mapping[str, str]] = {
    "extraction.form_reader": "akshara",
    "architect-text": "vastu",
}

#: What the person's later decision says about a stop.
USEFUL: Final = "useful"
"""The person corrected the item: the AI's answer was wrong, and the stop kept it from a result."""
NEEDLESS: Final = "needless"
"""The person confirmed the AI's answer: it was right, and the stop only cost the person a look."""
UNDETERMINED: Final = "decided, not judged"
"""The person decided the item, but the decision does not say whether the AI was right (a typed
value, which may be the site value, not the printed one; a decision on a split page's result)."""
WAITING: Final = "no later decision"
"""No person decided this item after the stop (yet)."""
OFF_LIST: Final = "decided by a name not on the reviewer list, not counted"
"""The newest decision is by a name not on the local real-reviewer list (or no list was given)."""
BATCH: Final = "confirmed in a batch, needs its own look"
"""The newest decision is a page answered in one "confirm these N pages" click."""
GATE_VERDICTS: Final = (USEFUL, NEEDLESS, UNDETERMINED, WAITING, OFF_LIST, BATCH)

#: The page-level items an inspector stops (`workflow.slot_reader._row_key`, the page sorter's
#: `p<i>:page`): the person answers them on the page, in the no-countertop answers or on the page's
#: result, which carry no question id of their own.
_PAGE_ITEMS: Final = (":row-choice", ":page")
_PAGE_TABLES: Final = frozenset({"no_countertop_answers", "review_actions"})
#: Whose candidates may answer an agent's stop: the page sorter's unread pages are answered as
#: Bala's (a page code listed) or nobody's (a page the budget stopped), as the exporter names them.
_ANSWERING_AGENTS: Final[Mapping[str, frozenset[str]]] = {
    "bala": frozenset({"bala", "none"}),
}
#: A page code proved blank from the PDF (`app.runs.reading_activity.PROVEN_BLANK`): its `stopped`
#: event waits for nobody, so no decision is expected. Counted apart, never judged.
_PROVEN_BLANK: Final = "blank-page"
#: A stop the AI budget made, not an inspector (`workflow.unread_pages.BUDGET_REACHED`,
#: `workflow.slot_reader.BUDGET_NOT_ASKED`): every such code begins `budget`. Counted apart.
_BUDGET_PREFIX: Final = "budget"
#: The line finder's stop when its AIs said the page has no countertop line
#: (`workflow.slot_reader._line_stop_code`): only then does a reviewer's "no countertop" confirm
#: the AIs. After any other row-choice stop (the AIs split, unsure, ...) it judges nothing.
_LINE_SAID_NONE: Final = "no-countertop"
NO_LIST: Final = "no real-reviewer list was given (GV_SMRITI_REVIEWERS), so no decision is counted"
_TRY_HELPED: Final = "helped"


@dataclass(frozen=True, slots=True)
class Rate:
    """An exact rate with both its counts, or NOT MEASURED with the reason."""

    numerator: int
    denominator: int
    empty: str
    """Why there is nothing to measure when the denominator is 0, in plain words."""
    unknown: str | None = None
    """Why the rate is not knowable exactly although the denominator is not 0 (an answer missing)."""

    @property
    def measured(self) -> bool:
        return self.denominator > 0 and self.unknown is None

    def show(self) -> str:
        if self.unknown is not None:
            return f"{NOT_MEASURED} ({self.unknown})"
        if self.denominator == 0:
            return f"{NOT_MEASURED} ({self.empty})"
        percent = 100 * self.numerator / self.denominator
        return f"{self.numerator} of {self.denominator} ({percent:.0f}%)"


@dataclass(frozen=True, slots=True)
class AiAnswers:
    """The calls on record for an agent (or a family): every call, and those answered (`ok`)."""

    calls: int = 0
    answered: int = 0
    families: tuple[tuple[str, int], ...] = ()


@dataclass(frozen=True, slots=True)
class Corrections:
    """An agent's corrections by the person's answer to "AI misread, or the vendor drew it?"."""

    ai_misread: int = 0
    vendor_drew_it: int = 0
    unsure: int = 0
    unanswered: int = 0
    not_asked: int = 0
    """Corrections the question is not asked about (a page's countertop, a part, a match)."""

    @property
    def total(self) -> int:
        return (
            self.ai_misread + self.vendor_drew_it + self.unsure + self.unanswered + self.not_asked
        )


@dataclass(frozen=True, slots=True)
class StoppedItem:
    """One item an inspector stopped in a finished reading, newest stop per item."""

    reading_id: str
    document_version_id: str | None
    page_number: int
    page_sha256: str | None
    """The page's content hash, the identity the exporter's candidates carry."""
    agent: str
    """The agent, lower case, as the candidates name it (`akshara`, `rekha`, `bala`)."""
    item: str
    code: str
    stopped_at: datetime


@dataclass(frozen=True, slots=True)
class GateOutcome:
    stop: StoppedItem
    verdict: str
    decision: str | None
    """The candidate id of the person's decision that judged it, if any."""


@dataclass(frozen=True, slots=True)
class AgentReport:
    agent: str
    ai: AiAnswers
    decisions: DecisionCount
    """The exporter's own counts of this agent's current person decisions."""
    off_list: int
    """Current decisions by names not on the local real-reviewer list: listed, never counted."""
    batch: int
    """Pages answered in a batch click: listed, never counted (each needs its own look)."""
    corrections: Corrections
    correction_rate: Rate
    """Corrected / (corrected or confirmed): how often a person who judged the agent's answer
    changed it. Typed values and other decisions do not judge it, so they are not in it."""
    misread_rate: Rate
    """AI misread / (corrected or confirmed), once every correction is answered."""
    reviewed: tuple[tuple[CorrectionRateKey, MetricResult], ...]
    """`eval/correction_rate.py`'s rate per reader of this agent."""
    gates: Mapping[str, int]
    """Stops by verdict."""
    gate_usefulness: Rate
    """Useful / (useful or needless)."""


@dataclass(frozen=True, slots=True)
class LearnerReport:
    database: str
    window: str
    export: CandidateExport
    agents: tuple[AgentReport, ...]
    unmapped_families: Mapping[str, AiAnswers]
    unmapped_readers: tuple[tuple[CorrectionRateKey, MetricResult], ...]
    gates: tuple[GateOutcome, ...]
    proven_blank: int = 0
    """`stopped` events on pages code proved blank: nothing waits for a person there."""
    budget_stops: int = 0
    """Stops the AI budget made (`budget-...` codes): not an inspector's, so never judged."""
    unfinished_stops: int = 0
    """Stops in readings that never finished: never shown to a person, so not judged."""


# --- judging a stop -------------------------------------------------------------------------------


def _when(candidate: Candidate) -> datetime:
    return datetime.fromisoformat(candidate.decided_at)


def _is_person(candidate: Candidate) -> bool:
    """A current decision by anybody but code (shown; counted only when `_counts`)."""
    return candidate.current and not is_code_actor(candidate.decided_by)


def _is_batch(candidate: Candidate) -> bool:
    """A page answered in one "confirm these N pages" click (the exporter records `batch`)."""
    return candidate.recorded.get("batch") is True


def _counts(candidate: Candidate) -> bool:
    """Whether a decision counts: a real reviewer's (on the local list), and not a batch click."""
    return (
        not is_code_actor(candidate.decided_by)
        and candidate.actor_status == REVIEWER
        and not _is_batch(candidate)
    )


def _same_question(stop: StoppedItem, candidate: Candidate) -> bool:
    question = candidate.question
    return (
        question is not None
        and question.get("question_id") == stop.item
        and question.get("page_sha256") == stop.page_sha256
    )


def _same_page(stop: StoppedItem, candidate: Candidate) -> bool:
    return (
        stop.item.endswith(_PAGE_ITEMS)
        and candidate.table in _PAGE_TABLES
        and candidate.page_sha256 == stop.page_sha256
        and candidate.page_number == stop.page_number
    )


def classify_stop(
    stop: StoppedItem, candidates: Iterable[Candidate], versions: Mapping[str, str]
) -> GateOutcome:
    """What the person did about this stopped item afterwards, from their newest current decision.

    The decision must be a person's (never code's), current (a superseded or carried record never
    counts), about the same item in the same file (`versions`: candidate id → the document version
    its decision is about; a candidate missing from it never matches) and page (its question id;
    for a page-level stop, the person's answer about that page), and made after the stop. A
    decision by a name off the reviewer list, or a batch click, judges nothing: it is said so."""
    if stop.page_sha256 is None or stop.document_version_id is None:
        return GateOutcome(stop, WAITING, None)
    answering = _ANSWERING_AGENTS.get(stop.agent, frozenset({stop.agent}))
    later = [
        candidate
        for candidate in candidates
        if _is_person(candidate)
        and candidate.agent in answering
        and versions.get(candidate.candidate_id) == stop.document_version_id
        and _when(candidate) >= stop.stopped_at
    ]
    # The exact question first; a page-level answer only when nothing names the question itself.
    matched = [c for c in later if _same_question(stop, c)] or [
        c for c in later if _same_page(stop, c)
    ]
    if not matched:
        return GateOutcome(stop, WAITING, None)
    newest = max(matched, key=lambda c: (_when(c), c.candidate_id))
    if _is_batch(newest):
        return GateOutcome(stop, BATCH, newest.candidate_id)
    if not _counts(newest):
        return GateOutcome(stop, OFF_LIST, newest.candidate_id)
    verdict = {"corrected": USEFUL, "confirmed": NEEDLESS}.get(newest.kind, UNDETERMINED)
    if (
        verdict == NEEDLESS
        and newest.table == "no_countertop_answers"
        and stop.agent == "rekha"
        and stop.code != _LINE_SAID_NONE
    ):
        # "No countertop" confirms the AIs only where they said so; after a split or an unsure
        # answer it does not say the AIs were right.
        verdict = UNDETERMINED
    return GateOutcome(stop, verdict, newest.candidate_id)


# --- building the report --------------------------------------------------------------------------


def _ai_answers(
    invocations: Iterable[tuple[str, str, int]],
) -> tuple[dict[str, AiAnswers], dict[str, AiAnswers]]:
    calls: dict[str, Counter[str]] = defaultdict(Counter)
    answered: Counter[str] = Counter()
    for prompt_id, outcome, count in invocations:
        # A prompt id may carry a `+...` suffix (the product context, a teaching hash): its
        # family is the id before it, without the version.
        family = prompt_family(prompt_id.split("+", 1)[0])
        calls[family]["calls"] += count
        if outcome == "ok":
            answered[family] += count
    per_agent: dict[str, list[str]] = defaultdict(list)
    unmapped: dict[str, AiAnswers] = {}
    for family in sorted(calls):
        agent = _FAMILY_AGENT.get(family)
        if agent is None:
            unmapped[family] = AiAnswers(
                calls=calls[family]["calls"],
                answered=answered[family],
                families=((family, calls[family]["calls"]),),
            )
        else:
            per_agent[agent].append(family)
    agents = {
        agent: AiAnswers(
            calls=sum(calls[family]["calls"] for family in families),
            answered=sum(answered[family] for family in families),
            families=tuple((family, calls[family]["calls"]) for family in families),
        )
        for agent, families in per_agent.items()
    }
    return agents, unmapped


def _decisions(candidates: Sequence[Candidate]) -> list[list[Candidate]]:
    """The current candidates grouped into decisions, as the exporter's `agent_counts` counts them:
    one decision however many parts it has (a row's walls and widths, a run and its walls)."""
    grouped: dict[tuple[str, str], list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        grouped[(candidate.table, candidate.decision_id)].append(candidate)
    return list(grouped.values())


def _mistake(parts: Sequence[Candidate]) -> str:
    """The person's misread-or-vendor answer on one corrected decision (`not_asked` if never asked)."""
    answers = [part.mistake_needed for part in parts if part.mistake_needed is not None]
    if not answers:
        return "not_asked"
    given = sorted({answer for answer in answers if answer != MISTAKE_UNANSWERED})
    if not given:
        return "unanswered"
    return given[0] if len(given) == 1 else "unsure"


def _corrections(decisions: Sequence[Sequence[Candidate]]) -> Corrections:
    """An agent's corrected decisions by the answer to "AI misread, or the vendor drew it?". A
    decision is corrected when its kind is (the kind `agent_counts` counts: its first part's)."""
    found: Counter[str] = Counter(
        _mistake(parts) for parts in decisions if parts[0].kind == "corrected"
    )
    return Corrections(
        ai_misread=found["ai_misread"],
        vendor_drew_it=found["vendor_drew_it"],
        unsure=found["unsure"],
        unanswered=found["unanswered"],
        not_asked=found["not_asked"],
    )


def _misread_rate(corrections: Corrections, judged: int, empty: str) -> Rate:
    waiting = corrections.unanswered + corrections.unsure
    unknown = None
    if judged and waiting:
        unknown = (
            f"{waiting} of {corrections.total} corrections still wait for a person's answer to "
            "'AI misread, or the vendor drew it?' (or were answered unsure)"
        )
    elif judged and corrections.not_asked:
        unknown = (
            f"{corrections.not_asked} corrections are of a kind the misread-or-vendor question is "
            "not asked about"
        )
    return Rate(
        numerator=corrections.ai_misread,
        denominator=judged,
        empty=empty,
        unknown=unknown,
    )


def build_report(
    *,
    export: CandidateExport,
    invocations: Iterable[tuple[str, str, int]],
    stops: Iterable[StoppedItem],
    reviewed: Mapping[CorrectionRateKey, MetricResult],
    window: str,
    versions: Mapping[str, str] | None = None,
    proven_blank: int = 0,
    unfinished_stops: int = 0,
) -> LearnerReport:
    """Count and join, with nothing read: the exporter's candidates, the calls per prompt
    (`(prompt_id, outcome, count)`), the stopped items, `correction_rate`'s results and each
    candidate's document version (`versions`; without it no stop is matched to a decision)."""
    people = [candidate for candidate in export.candidates if _is_person(candidate)]
    # Only a real reviewer's decisions are counted (`agent_counts` also lists their earlier
    # records); every other name and every batch click is listed apart.
    counts = agent_counts(c for c in export.candidates if _counts(c))
    ai, unmapped = _ai_answers(invocations)
    inspected = [stop for stop in stops if not stop.code.startswith(_BUDGET_PREFIX)]
    budget_stops = sum(1 for stop in stops if stop.code.startswith(_BUDGET_PREFIX))
    gates = tuple(
        classify_stop(stop, people, versions or {})
        for stop in sorted(inspected, key=lambda s: (s.agent, s.page_number, s.item, s.reading_id))
    )
    no_list = export.reviewer_list is None
    readers: dict[str, list[tuple[CorrectionRateKey, MetricResult]]] = defaultdict(list)
    unmapped_readers: list[tuple[CorrectionRateKey, MetricResult]] = []
    for key in sorted(reviewed):
        agent = EXTRACTOR_AGENTS.get(key.extractor)
        (readers[agent] if agent else unmapped_readers).append((key, reviewed[key]))
    names = [
        *REPORT_AGENTS,
        *sorted({c.agent for c in people} - set(REPORT_AGENTS), key=_order),
    ]
    agents = []
    for name in names:
        mine = _decisions([c for c in people if c.agent == name and _counts(c)])
        shown = _decisions([c for c in people if c.agent == name and not _counts(c)])
        batch = sum(1 for parts in shown if _is_batch(parts[0]))
        off_list = len(shown) - batch
        count = counts.get(name, DecisionCount())
        corrections = _corrections(mine)
        judged = count.corrected + count.confirmed
        verdicts = Counter(outcome.verdict for outcome in gates if outcome.stop.agent == name)
        apart = [
            *([f"{off_list} by names not on the reviewer list"] if off_list else []),
            *([f"{batch} pages confirmed in a batch"] if batch else []),
        ]
        empty = (
            NO_LIST
            if no_list
            else "no reviewer on the list corrected or confirmed this agent's answer"
            + (f" ({' and '.join(apart)}: not counted)" if apart else "")
        )
        agents.append(
            AgentReport(
                agent=name,
                ai=ai.get(name, AiAnswers()),
                decisions=count,
                off_list=off_list,
                batch=batch,
                corrections=corrections,
                correction_rate=Rate(numerator=count.corrected, denominator=judged, empty=empty),
                misread_rate=_misread_rate(corrections, judged, empty),
                reviewed=tuple(readers.get(name, ())),
                gates={
                    verdict: verdicts[verdict] for verdict in GATE_VERDICTS if verdicts[verdict]
                },
                gate_usefulness=Rate(
                    numerator=verdicts[USEFUL],
                    denominator=verdicts[USEFUL] + verdicts[NEEDLESS],
                    empty=(
                        NO_LIST
                        if no_list
                        else "no stopped item of this agent was later corrected or confirmed by "
                        "a reviewer on the list"
                    ),
                ),
            )
        )
    return LearnerReport(
        database=export.database,
        window=window,
        export=export,
        agents=tuple(agents),
        unmapped_families=unmapped,
        unmapped_readers=tuple(unmapped_readers),
        gates=gates,
        proven_blank=proven_blank,
        unfinished_stops=unfinished_stops,
        budget_stops=budget_stops,
    )


def _order(name: str) -> tuple[int, str]:
    return (AGENT_ORDER.index(name) if name in AGENT_ORDER else len(AGENT_ORDER), name)


# --- reading the database -------------------------------------------------------------------------


def stopped_items(session: Session) -> tuple[list[StoppedItem], int, int]:
    """Every item whose newest event in a finished reading is a stop, newest reading per item.

    Returns the stops, the proven-blank pages left out, and the stops in readings that never
    finished (a failed reading is never shown as the result, so its stops asked nothing)."""
    finished = set(
        session.scalars(
            select(ReadingEvent.reading_id).where(ReadingEvent.kind == READING_FINISHED_KIND)
        )
    )
    events = session.execute(
        select(ReadingEvent)
        .where(ReadingEvent.kind != READING_FINISHED_KIND, ReadingEvent.item.is_not(None))
        .order_by(ReadingEvent.created_at, ReadingEvent.position, ReadingEvent.id)
    ).scalars()
    newest: dict[tuple[UUID | None, str], ReadingEvent] = {}
    last_stop: dict[tuple[UUID | None, str], ReadingEvent] = {}
    unfinished: set[tuple[UUID, UUID | None, str]] = set()
    for event in events:
        assert event.item is not None
        key = (event.document_version_id, event.item)
        if event.reading_id not in finished:
            if event.kind == "stopped":
                unfinished.add((event.reading_id, event.document_version_id, event.item))
            continue
        if event.kind == "answered":
            continue  # an answer is not a gate's word about the item
        newest[key] = event
        if event.kind == "stopped":
            last_stop[key] = event
    stops: list[StoppedItem] = []
    blank = 0
    pages: dict[tuple[UUID | None, int], str | None] = {}
    for key, event in newest.items():
        stop = last_stop.get(key)
        if stop is None or stop.reading_id != event.reading_id:
            continue
        if event.kind == "try_again_result" and event.code == _TRY_HELPED:
            continue  # a try helped: the item was passed on after all
        if stop.code == _PROVEN_BLANK:
            blank += 1
            continue
        assert stop.page_number is not None and stop.agent is not None
        place = (stop.document_version_id, stop.page_number)
        if place not in pages:
            pages[place] = (
                None
                if stop.document_version_id is None
                else session.scalar(
                    select(Page.content_hash).where(
                        Page.document_version_id == stop.document_version_id,
                        Page.index == stop.page_number - 1,
                    )
                )
            )
        stops.append(
            StoppedItem(
                reading_id=str(stop.reading_id),
                document_version_id=(
                    None if stop.document_version_id is None else str(stop.document_version_id)
                ),
                page_number=stop.page_number,
                page_sha256=pages[place],
                agent=stop.agent.lower(),
                item=key[1],
                code=stop.code or "",
                stopped_at=stop.created_at,
            )
        )
    return stops, blank, len(unfinished)


def candidate_versions(session: Session, candidates: Iterable[Candidate]) -> dict[str, str]:
    """The document version (file) each candidate's decision is about, for the tables whose
    decisions can answer an inspector's stop: a correction or a confirm of a reading (its
    observation's file), a decision on a page's result (its record's file), a typed width (its
    row's file) and a no-countertop answer (its page's file). Other candidates are left out, so
    they never match a stop."""
    wanted: dict[str, dict[UUID, list[str]]] = defaultdict(lambda: defaultdict(list))
    for candidate in candidates:
        try:
            decision = UUID(candidate.decision_id)
        except ValueError:
            continue
        wanted[candidate.table][decision].append(candidate.candidate_id)
    found: dict[UUID, UUID] = {}

    def add(rows: Iterable[Row[UUID, UUID]]) -> None:
        for decision, version in rows:
            if version is not None:
                found.setdefault(decision, version)

    if ids := list(wanted["correction_ledger"]):
        add(
            session.execute(
                select(CorrectionLedgerEntry.id, CanonicalObservation.document_version_id)
                .join(
                    CanonicalObservation,
                    CanonicalObservation.id == CorrectionLedgerEntry.canonical_observation_id,
                )
                .where(CorrectionLedgerEntry.id.in_(ids))
            ).all()
        )
    if ids := list(wanted["review_actions"]):
        add(
            session.execute(
                select(ReviewAction.id, CanonicalObservation.document_version_id)
                .join(
                    CanonicalObservation,
                    CanonicalObservation.id == ReviewAction.original_observation_id,
                )
                .where(ReviewAction.id.in_(ids))
            ).all()
        )
        add(
            session.execute(
                select(ReviewAction.id, ObservationCandidate.document_version_id)
                .join(Finding, Finding.id == ReviewAction.finding_id)
                .join(
                    ObservationCandidate, ObservationCandidate.id == Finding.scope_row_candidate_id
                )
                .where(ReviewAction.id.in_(ids), ReviewAction.original_observation_id.is_(None))
            ).all()
        )
    if ids := list(wanted["slot_row_review_decisions"]):
        add(
            session.execute(
                select(SlotRowReviewDecision.id, ObservationCandidate.document_version_id)
                .join(
                    ObservationCandidate,
                    ObservationCandidate.id == SlotRowReviewDecision.row_candidate_id,
                )
                .where(SlotRowReviewDecision.id.in_(ids))
            ).all()
        )
    if ids := list(wanted["no_countertop_answers"]):
        add(
            session.execute(
                select(NoCountertopAnswer.id, Page.document_version_id)
                .join(Page, Page.id == NoCountertopAnswer.page_id)
                .where(NoCountertopAnswer.id.in_(ids))
            ).all()
        )
    return {
        candidate_id: str(found[decision])
        for decisions in wanted.values()
        for decision, candidate_ids in decisions.items()
        if decision in found
        for candidate_id in candidate_ids
    }


def learner_report(
    session: Session,
    *,
    database: str,
    code_version: str = "unknown",
    reviewers: Reviewers | None = None,
    window: timedelta = timedelta(days=3650),
    now: datetime | None = None,
) -> LearnerReport:
    """The learner report of this database, read-only (the exporter's read-only transaction).

    `reviewers` is the local real-reviewer list (`None` reads `GV_SMRITI_REVIEWERS`); `window` is
    how far back `eval/correction_rate.py` looks."""
    # First: the exporter makes the transaction read-only before its first query.
    export = export_candidates(
        session, database=database, code_version=code_version, reviewers=reviewers
    )
    invocations = [
        (prompt_id, outcome, count)
        for prompt_id, outcome, count in session.execute(
            select(ModelInvocation.prompt_id, ModelInvocation.outcome, func.count()).group_by(
                ModelInvocation.prompt_id, ModelInvocation.outcome
            )
        )
    ]
    stops, blank, unfinished = stopped_items(session)
    reviewed = correction_rate(session, window=window, now=now)
    return build_report(
        export=export,
        invocations=invocations,
        stops=stops,
        reviewed=reviewed,
        window=f"the last {window.days} days",
        versions=candidate_versions(session, export.candidates),
        proven_blank=blank,
        unfinished_stops=unfinished,
    )


# --- the words --------------------------------------------------------------------------------------


def _agent_lines(agent: AgentReport) -> list[str]:
    families = ", ".join(f"{family} {count}" for family, count in agent.ai.families)
    count = agent.decisions
    fixes = agent.corrections
    lines = [
        f"{agent.agent} ({ROLES.get(agent.agent, 'not a named agent')})",
        (
            f"  AI answers on record: {agent.ai.calls} calls, {agent.ai.answered} answered"
            + (f" ({families})" if families else "")
        ),
        (
            f"  reviewer decisions (names on the real-reviewer list, counted): {count.decisions}: "
            f"{count.corrected} corrected, {count.confirmed} confirmed, {count.filled} typed a "
            f"held value, {count.decided} other decisions; {count.earlier} earlier records "
            "not counted"
        ),
        (
            f"  not counted: {agent.off_list} decisions by names not on the reviewer list; "
            f"{agent.batch} pages confirmed in a batch (each still needs its own look)"
        ),
        (
            f"  corrections {fixes.total}: AI misread {fixes.ai_misread}, vendor drew it "
            f"{fixes.vendor_drew_it}, unsure {fixes.unsure}, not answered yet {fixes.unanswered}, "
            f"not asked {fixes.not_asked}"
        ),
        f"  correction rate (corrected / corrected or confirmed): {agent.correction_rate.show()}",
        f"  AI misread rate (misread / corrected or confirmed): {agent.misread_rate.show()}",
        f"  overturned automatic results: {count.overturned}",
    ]
    if agent.reviewed:
        lines.append("  reviewed readings (eval/correction_rate.py):")
        lines.extend(
            f"    {key.check_type} · {key.extractor} {key.extractor_version}: {result}"
            for key, result in agent.reviewed
        )
    else:
        lines.append(
            f"  reviewed readings (eval/correction_rate.py): {NOT_MEASURED} (no reading of this "
            "agent's readers was confirmed or corrected in the window)"
        )
    stops = sum(agent.gates.values())
    shown = ", ".join(f"{verdict} {number}" for verdict, number in agent.gates.items())
    lines.append(
        f"  stops by its inspector: {stops}"
        + (f" ({shown})" if shown else "")
        + f"; usefulness (useful / useful or needless): {agent.gate_usefulness.show()}"
    )
    return lines


def render_report(report: LearnerReport) -> str:
    """The report for a person, in plain words."""
    export = report.export
    reviewers = (
        f"real reviewers from {export.reviewer_list}"
        if export.reviewer_list is not None
        else "no real-reviewer list given, so no decision is counted"
    )
    lines = [
        (
            f"Smriti learner report: {report.database} (code {export.code_version}); read-only, "
            f"no model calls; {reviewers}"
        ),
        (
            "Only decisions by names on the real-reviewer list count; other names and batch "
            "clicks are listed, never counted."
        ),
        (
            f"{NOT_MEASURED} means there is nothing to divide by (or an answer is still missing); "
            "it never reads as a zero rate."
        ),
        "",
    ]
    for agent in report.agents:
        lines.extend(_agent_lines(agent))
    if report.unmapped_families:
        lines.append("")
        lines.append("AI answers of prompts no agent is named for (listed, not guessed):")
        lines.extend(
            f"  {family}: {answers.calls} calls, {answers.answered} answered"
            for family, answers in report.unmapped_families.items()
        )
    lines.append("")
    if report.unmapped_readers:
        lines.append(
            f"Reviewed readings of readers that are no agent's (eval/correction_rate.py, "
            f"{report.window}):"
        )
        lines.extend(
            f"  {key.check_type} · {key.extractor} {key.extractor_version}: {result}"
            for key, result in report.unmapped_readers
        )
    if export.skipped:
        lines.append("Left out as the machine's own decisions (never a person's):")
        lines.extend(
            f"  {table}: "
            + ", ".join(f"{why} {count}" for why, count in sorted(export.skipped[table].items()))
            for table in sorted(export.skipped)
        )
    overturned = [c for c in overturned_listing(export) if not is_code_actor(c.decided_by)]
    counted = sum(1 for candidate in overturned if _counts(candidate))
    lines.append(
        f"Overturned automatic results (report only; nothing is switched off): {counted} by "
        f"reviewers on the list; {len(overturned) - counted} more listed, not counted"
    )
    for candidate in overturned:
        what = candidate.overturned or {}
        who = (
            "counted"
            if _counts(candidate)
            else "not counted: "
            + ("a batch click" if _is_batch(candidate) else "not on the reviewer list")
        )
        lines.append(
            f"  {candidate.decided_at[:10]} {candidate.agent}, page {candidate.page_number}: "
            f"{what.get('what')} by {what.get('by')} ({who}); {candidate.candidate_id}"
        )
    lines.append(
        f"Inspector stops in finished readings: {len(report.gates)}; left out, never judged: "
        f"{report.budget_stops} stops the AI budget made, {report.proven_blank} pages code proved "
        f"blank (waiting for nobody), {report.unfinished_stops} stops in readings that never "
        "finished"
    )
    for outcome in report.gates:
        stop = outcome.stop
        lines.append(
            f"  {stop.agent}, page {stop.page_number}, {stop.item} ({stop.code}): "
            f"{outcome.verdict}" + (f" ({outcome.decision})" if outcome.decision else "")
        )
    return "\n".join(lines)
