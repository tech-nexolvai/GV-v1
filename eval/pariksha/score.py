"""Score Akshara, Rekha and Vastu from a stored run against a `pariksha-key/1` key (#1219).

**Each stored reply is read by the code that reads it live.** A reader's answer is its last accepted
(`ok`) reply to the question, replayed through `extraction.slot_reader.bedrock.replay_stored_answer`,
the path a re-run takes when it reuses a stored answer (#1112): the same JSON boundary, the same
strict answer shape, the same range checks. A reply today's code would not accept is *invalid*,
never guessed at. Texts are compared after `seal.normalise_text`, the fold the seal compares with.

**Akshara (label questions).** One reader's answer is:

* *unsure*: `belongs` is unsure, or it says it belongs but is not readable;
* *no*: a sure "no label here" (`belongs` no, or `no_dimension`);
* *yes* with its normalised text;
* *invalid*: no accepted answer.

Against the key it is right (the text is accepted, or "no" where the span has no label), wrong
(a confident answer that is not), unsure or invalid. The pair is **agreed-right** or
**agreed-wrong** when both readers say yes with the identical text (the agreement a label is sealed
on), and *held* otherwise: a disagreement, an unsure or a "no" goes to a person.

**Rekha (row choice).** A reader's answer is a box, `no_countertop`, `not_among_boxes`, unsure or
invalid (a v1/v2 answer of 0 named neither no-row answer and counts as unsure). The pair is
agreed-right or **agreed-wrong** when both give the same definite answer, and *split* otherwise.
Two unsure answers are not an agreement.

**Vastu (architect reader).** `eval/architect_reader.score_pages` on the stored spans, one case per
key width (`p<page>:<inches>#<n>`: found, held or missing) and one per usable value the key does not
list (`p<page>:extra:<inches>#<n>`), which a person checks: it is the only way the architect reader
can be confidently wrong, so it counts as a wrong answer when two scorecards are compared.

**Rates are over the questions asked.** A key case the run never asked (or asked about another span)
is listed as `not_asked` with the reason, and counted in `asked`, never as right or wrong. A rate
over nothing is `None` (not measured), never 0.

Source: issue #1219 · Verification: `tests/eval/test_pariksha_score.py`
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Final

from eval.architect_reader import score_pages
from eval.metrics import MetricResult
from eval.pariksha.keys import (
    AnswerKey,
    ArchitectAnswerKey,
    KeyCase,
    LabelTruth,
    QuestionIdentity,
    RowTruth,
)
from eval.pariksha.stored import (
    StoredArchitectRun,
    StoredAttempt,
    StoredQuestion,
    StoredRun,
    StoredRunError,
)
from eval.runs import MissingProvenanceError
from extraction.slot_reader.bedrock import CropJob, RowChoiceAnswer, replay_stored_answer
from extraction.slot_reader.seal import Belongs, ReaderAnswer, normalise_text

__all__ = [
    "AGREED_RIGHT",
    "AGREED_WRONG",
    "EXTRA_HELD",
    "EXTRA_USABLE",
    "FOUND",
    "HELD",
    "MISSING",
    "NOT_ASKED",
    "RIGHT_OUTCOMES",
    "SPLIT",
    "WRONG_OUTCOMES",
    "CaseScore",
    "ReaderScore",
    "Scorecard",
    "score_akshara",
    "score_rekha",
    "score_vastu",
]

AGREED_RIGHT: Final = "agreed_right"
AGREED_WRONG: Final = "agreed_wrong"
HELD: Final = "held"
SPLIT: Final = "split"
NOT_ASKED: Final = "not_asked"
FOUND: Final = "found"
MISSING: Final = "missing"
EXTRA_USABLE: Final = "extra_usable"
EXTRA_HELD: Final = "extra_held"
#: A case outcome that is a confident wrong answer: what "0 new wrong" counts.
WRONG_OUTCOMES: Final = frozenset({AGREED_WRONG, EXTRA_USABLE})
#: A case outcome that is a right automatic answer: what "better" counts.
RIGHT_OUTCOMES: Final = frozenset({AGREED_RIGHT, FOUND})

RIGHT: Final = "right"
WRONG: Final = "wrong"
UNSURE: Final = "unsure"
INVALID: Final = "invalid"
_VERDICTS: Final = (RIGHT, WRONG, UNSURE, INVALID)

#: A replayed request must be shown pictures; the stored question's own were already hashed into
#: its packet, and only the reply is read here, so a PNG signature stands in for each.
_PICTURE: Final = b"\x89PNG\r\n\x1a\n"
_REPLAY_MARK: Final = "pariksha-score"


@dataclass(frozen=True, slots=True)
class ReaderScore:
    model_id: str
    answer: str
    """What the reader said, for a person: the normalised text, `(no)`, `(unsure)`, `(invalid)`,
    or the row answer."""
    verdict: str
    """`right`, `wrong`, `unsure` or `invalid`."""


@dataclass(frozen=True, slots=True)
class CaseScore:
    case_id: str
    question: str
    expected: str
    outcome: str
    readers: tuple[ReaderScore, ...] = ()
    cost_micros: int = 0
    note: str = ""


@dataclass(frozen=True, slots=True)
class Scorecard:
    """One agent's score on one key from one stored run, with everything that could explain it."""

    agent: str
    key_name: str
    key_version: str
    key_sha256: str
    code_version: str
    """The code that produced the stored run (the version being measured)."""
    scorer_version: str
    """The code that scored it."""
    extractor_versions: Mapping[str, str]
    """`agent`, `prompt_ids`, `model_ids`, the run's extractor and config: the shape
    `evaluation_runs.extractor_versions` has."""
    source: Mapping[str, str]
    metrics: Mapping[str, MetricResult]
    cases: tuple[CaseScore, ...]
    created_at: str


def _rate(key: str, numerator: int, denominator: int, note: str = "") -> MetricResult:
    if denominator == 0:
        return MetricResult(key=key, value=None, numerator=0, denominator=0, note=note or "none")
    return MetricResult(
        key=key,
        value=Fraction(numerator, denominator),
        numerator=numerator,
        denominator=denominator,
        note=note,
    )


def _provenance(code_version: str) -> None:
    if not code_version.strip():
        raise MissingProvenanceError(
            "a scorecard must record the code version of the run it scores; without it a "
            "difference between two scorecards cannot be attributed"
        )


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _replay(question: StoredQuestion, attempt: StoredAttempt) -> object | None:
    """The answer the repository's reader code reads from a stored reply, or `None`."""
    if attempt.outcome != "ok" or attempt.raw_response is None:
        return None
    packet = question.packet
    page_index = packet.get("page_index")
    page = page_index if isinstance(page_index, int) and not isinstance(page_index, bool) else 0
    job: CropJob
    if question.identity.family == "slot-row-choice":
        count = packet.get("candidate_count")
        if not isinstance(count, int) or isinstance(count, bool):
            candidates = packet.get("candidate_ids")
            count = len(candidates) if isinstance(candidates, list) else None
        if count is None:
            return None
        job = CropJob(
            key=question.identity.question_id,
            model_id=attempt.model_id,
            page_index=page,
            png=_PICTURE,
            row_question=True,
            candidate_count=count,
            question_packet=packet,
        )
    else:
        images = packet.get("images")
        upright = isinstance(images, Mapping) and "upright_close_up" in images
        job = CropJob(
            key=question.identity.question_id,
            model_id=attempt.model_id,
            page_index=page,
            png=_PICTURE,
            view_png=_PICTURE,
            grounded_claude=True,
            question_packet=packet,
            upright_png=_PICTURE if upright else None,
        )
    replayed = replay_stored_answer(
        job, attempt.raw_response, reused_from=_REPLAY_MARK, max_tokens=1
    )
    return None if replayed is None else replayed[0]


def _answers(question: StoredQuestion, readers: Sequence[str]) -> dict[str, object | None]:
    """Each reader's last accepted answer to the question, read again; `None` when it has none."""
    found: dict[str, object | None] = dict.fromkeys(readers)
    for attempt in question.attempts:
        if attempt.outcome == "ok":
            found[attempt.model_id] = _replay(question, attempt)
    return found


def _readers(run: StoredRun) -> tuple[str, ...]:
    models = sorted({attempt.model_id for q in run.questions for attempt in q.attempts})
    if len(models) != 2:
        raise StoredRunError(
            f"the pair needs exactly two readers; run {run.extraction_run_id} has "
            f"{len(models)}: {', '.join(models) or 'none'}"
        )
    return tuple(models)


def _label_state(answer: object | None) -> tuple[str, str]:
    if not isinstance(answer, ReaderAnswer):
        return INVALID, ""
    if answer.belongs is Belongs.UNSURE:
        return UNSURE, ""
    if answer.belongs is Belongs.NO or answer.no_dimension:
        return "no", ""
    if not answer.readable:
        return UNSURE, ""
    return "yes", normalise_text(answer.text)


def _label_verdict(state: tuple[str, str], truth: LabelTruth) -> str:
    said, text = state
    if said in (INVALID, UNSURE):
        return said
    if truth.no_label:
        return RIGHT if said == "no" else WRONG
    return RIGHT if said == "yes" and text in truth.texts else WRONG


def _label_shown(state: tuple[str, str]) -> str:
    said, text = state
    return text if said == "yes" else f"({said})"


def _row_choice(answer: object | None) -> tuple[str, int | None]:
    if not isinstance(answer, RowChoiceAnswer):
        return INVALID, None
    kind = answer.kind
    if kind is None:
        # v1/v2: 0 meant either "no countertop" or "not among the boxes", so it names neither.
        kind = "row" if answer.row > 0 else UNSURE
    return (kind, answer.row) if kind == "row" else (kind, None)


def _row_verdict(choice: tuple[str, int | None], truth: RowTruth) -> str:
    kind, box = choice
    if kind in (INVALID, UNSURE):
        return kind
    return RIGHT if (kind, box) == (truth.kind, truth.box) else WRONG


def _row_shown(choice: tuple[str, int | None]) -> str:
    kind, box = choice
    return f"box {box}" if kind == "row" else f"({kind})" if kind in (INVALID, UNSURE) else kind


def _cost(question: StoredQuestion) -> int:
    return sum(attempt.cost_micros or 0 for attempt in question.attempts)


def _not_asked(case: KeyCase, note: str) -> CaseScore:
    return CaseScore(
        case_id=case.case_id,
        question=str(case.question),
        expected=str(case.truth),
        outcome=NOT_ASKED,
        note=f"not asked: {note}",
    )


def _match(case: KeyCase, index: Mapping[QuestionIdentity, StoredQuestion]) -> StoredQuestion | str:
    question = index.get(case.question)
    if question is None:
        return "no question with this identity in the run"
    if case.slot_box is not None and question.slot_box != case.slot_box:
        return (
            f"the run asked about a different span (slot box {question.slot_box or 'unknown'}; "
            f"the key's is {case.slot_box})"
        )
    if case.picture_sha256 is not None and case.picture_sha256 not in question.picture_sha256:
        return (
            f"the run showed a different picture (not {case.picture_sha256[:12]}), so its box "
            "numbers or span may not be the key's"
        )
    return question


def _score_pairs(
    agent: str,
    key: AnswerKey,
    run: StoredRun,
    *,
    code_version: str,
    scorer_version: str,
    created_at: str | None,
) -> Scorecard:
    _provenance(code_version)
    if key.agent != agent:
        raise ValueError(f"the key is {key.agent}'s, not {agent}'s")
    readers = _readers(run)
    index = {question.identity: question for question in run.questions}
    held_outcome = HELD if agent == "akshara" else SPLIT
    cases: list[CaseScore] = []
    pair: Counter[str] = Counter()
    per_reader: dict[str, Counter[str]] = defaultdict(Counter)
    cost = 0
    attempts = 0
    reused = 0
    keyed: set[QuestionIdentity] = set()
    for case in key.cases:
        matched = _match(case, index)
        if isinstance(matched, str):
            cases.append(_not_asked(case, matched))
            continue
        keyed.add(matched.identity)
        answers = _answers(matched, readers)
        verdicts: dict[str, str] = {}
        shown: dict[str, str] = {}
        definite: dict[str, object] = {}
        for model in readers:
            if isinstance(case.truth, LabelTruth):
                state = _label_state(answers[model])
                verdicts[model] = _label_verdict(state, case.truth)
                shown[model] = _label_shown(state)
                if state[0] == "yes":
                    definite[model] = state[1]
            else:
                choice = _row_choice(answers[model])
                verdicts[model] = _row_verdict(choice, case.truth)
                shown[model] = _row_shown(choice)
                if choice[0] not in (INVALID, UNSURE):
                    definite[model] = choice
        first, second = readers
        agreed = first in definite and second in definite and definite[first] == definite[second]
        if agreed:
            outcome = AGREED_RIGHT if verdicts[first] == RIGHT else AGREED_WRONG
        else:
            outcome = held_outcome
        pair[outcome] += 1
        for model in readers:
            per_reader[model][verdicts[model]] += 1
        question_cost = _cost(matched)
        cost += question_cost
        attempts += len(matched.attempts)
        reused += sum(1 for attempt in matched.attempts if attempt.reused)
        cases.append(
            CaseScore(
                case_id=case.case_id,
                question=str(case.question),
                expected=str(case.truth),
                outcome=outcome,
                readers=tuple(
                    ReaderScore(model_id=model, answer=shown[model], verdict=verdicts[model])
                    for model in readers
                ),
                cost_micros=question_cost,
            )
        )
    asked = sum(pair.values())
    metrics: dict[str, MetricResult] = {
        "asked": _rate("asked", asked, len(key.cases)),
        "pair_agreed_right": _rate("pair_agreed_right", pair[AGREED_RIGHT], asked),
        "pair_agreed_wrong": _rate("pair_agreed_wrong", pair[AGREED_WRONG], asked),
        f"pair_{held_outcome}": _rate(f"pair_{held_outcome}", pair[held_outcome], asked),
    }
    for model in readers:
        for verdict in _VERDICTS:
            name = f"reader_{verdict}:{model}"
            metrics[name] = _rate(name, per_reader[model][verdict], asked)
    metrics["cost_micros_per_question"] = _rate(
        "cost_micros_per_question",
        cost,
        asked,
        "every recorded attempt of both readers, failures included; a reused answer costs 0",
    )
    metrics["reused_answers"] = _rate("reused_answers", reused, attempts)
    unkeyed = sum(1 for question in run.questions if question.identity not in keyed)
    metrics["unkeyed_questions"] = _rate(
        "unkeyed_questions",
        unkeyed,
        len(run.questions),
        f"{run.unidentified} answer(s) had no page hash or question id" if run.unidentified else "",
    )
    prompts = sorted({question.prompt_id for question in run.questions})
    return Scorecard(
        agent=agent,
        key_name=key.name,
        key_version=key.version,
        key_sha256=key.sha256,
        code_version=code_version,
        scorer_version=scorer_version,
        extractor_versions={
            "agent": agent,
            "prompt_ids": ",".join(prompts),
            "model_ids": ",".join(readers),
            "extractor": f"{run.extractor}@{run.extractor_version}",
            "config_hash": run.config_hash,
        },
        source={"database": run.database, "extraction_run_id": run.extraction_run_id},
        metrics=metrics,
        cases=tuple(cases),
        created_at=created_at or _now(),
    )


def score_akshara(
    key: AnswerKey,
    run: StoredRun,
    *,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The number reader's scorecard: per reader and per pair, and cost per question."""
    return _score_pairs(
        "akshara",
        key,
        run,
        code_version=code_version,
        scorer_version=scorer_version,
        created_at=created_at,
    )


def score_rekha(
    key: AnswerKey,
    run: StoredRun,
    *,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The row choice's scorecard: agreed-right, agreed-wrong and split; per reader."""
    return _score_pairs(
        "rekha",
        key,
        run,
        code_version=code_version,
        scorer_version=scorer_version,
        created_at=created_at,
    )


def _inches(value: Fraction | None) -> str:
    return "?" if value is None else str(value)


def score_vastu(
    key: ArchitectAnswerKey,
    run: StoredArchitectRun,
    *,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The architect reader's scorecard: `eval/architect_reader.score_pages`, one case per width."""
    _provenance(code_version)
    if key.document_sha256 is not None and key.document_sha256 != run.document_sha256:
        raise StoredRunError("the stored spans are of another document than the key's")
    card = score_pages(key.pages, run.pages)
    cases: list[CaseScore] = []
    for page in card.pages:
        widths = next((entry.widths for entry in key.pages if entry.page == page.page), ())
        found = Counter(page.found)
        held = Counter(value for value, _reason in page.held)
        reasons: dict[Fraction, list[str]] = defaultdict(list)
        for value, reason in page.held:
            reasons[value].append(reason)
        seen: Counter[Fraction] = Counter()
        for width in widths:
            seen[width] += 1
            note = ""
            if found[width]:
                found[width] -= 1
                outcome = FOUND
            elif held[width]:
                held[width] -= 1
                outcome = HELD
                note = reasons[width].pop(0)
            else:
                outcome = MISSING
            cases.append(
                CaseScore(
                    case_id=f"p{page.page}:{width}#{seen[width]}",
                    question=f"page {page.page}",
                    expected=str(width),
                    outcome=outcome,
                    note=note,
                )
            )
        extra: Counter[str] = Counter()
        for value, text in page.extra_usable:
            extra[str(value)] += 1
            cases.append(
                CaseScore(
                    case_id=f"p{page.page}:extra:{value}#{extra[str(value)]}",
                    question=f"page {page.page}",
                    expected="(not in the key)",
                    outcome=EXTRA_USABLE,
                    readers=(
                        ReaderScore(model_id=run.extractor, answer=text, verdict="check by eye"),
                    ),
                    note="a usable value the key does not list: a person checks it on the page",
                )
            )
        extra_held: Counter[str] = Counter()
        for printed, text, reason in page.extra_held:
            label = _inches(printed)
            extra_held[label] += 1
            cases.append(
                CaseScore(
                    case_id=f"p{page.page}:extra-held:{label}#{extra_held[label]}",
                    question=f"page {page.page}",
                    expected="(not in the key)",
                    outcome=EXTRA_HELD,
                    readers=(ReaderScore(model_id=run.extractor, answer=text, verdict="held"),),
                    note=reason,
                )
            )
    totals = card.totals
    widths_total = totals["key_widths"]
    metrics = {
        name: _rate(name, totals[name], widths_total)
        for name in ("found", "held", "missing", "extra_usable", "extra_held")
    }
    return Scorecard(
        agent="vastu",
        key_name=key.name,
        key_version=key.version,
        key_sha256=key.sha256,
        code_version=code_version,
        scorer_version=scorer_version,
        extractor_versions={
            "agent": "vastu",
            "prompt_ids": "none (code only)",
            "model_ids": "none (code only)",
            "extractor": f"{run.extractor}@{run.extractor_version}",
            "config_hash": run.config_hash,
        },
        source={
            "database": run.database,
            "extraction_run_id": run.extraction_run_id,
            "document_sha256": run.document_sha256,
        },
        metrics=metrics,
        cases=tuple(cases),
        created_at=created_at or _now(),
    )
