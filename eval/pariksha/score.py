"""Score Akshara, Rekha and Vastu from a stored run against a `pariksha-key/1` key (#1219).

**Each stored reply is read by the code that reads it live** (a reply of an older prompt version,
by the parser for that version; `eval/pariksha/__init__.py`, "Reading stored answers"). A reader's answer is its last accepted
(`ok`) reply to the question, replayed through `extraction.slot_reader.bedrock.replay_stored_answer`,
the path a re-run takes when it reuses a stored answer (#1112): the same JSON boundary, the same
strict answer shape, the same range checks. A reply today's code would not accept is *invalid*,
never guessed at. Texts are compared after `seal.normalise_text`, the fold the seal compares with.

**Akshara (label questions).** One reader's answer is:

* *unsure*: `belongs` is unsure, or it says it belongs but is not readable;
* *no*: a sure "no label here" (`belongs` no, or `no_dimension`);
* *yes* with its normalised text (a usable answer, as the seal needs: an empty text is unsure);
* *invalid*: no accepted answer.

Against the key it is right (the text is accepted, or "no" where the span has no label), wrong
(a confident answer that is not), unsure or invalid. The pair is **agreed-right** or
**agreed-wrong** when both readers say yes with the identical text (the agreement a label is sealed
on), and *held* otherwise: a disagreement, an unsure or a "no" goes to a person.

**Rekha (row choice).** A reader's answer is a box, `no_countertop`, `not_among_boxes`, unsure or
invalid (a v1/v2 answer of 0 named neither no-row answer and counts as unsure). The pair is
agreed-right or **agreed-wrong** only when both give the same answer the product takes without a
person: the same box, or both "no countertop". Anything else is *split*, including two "not among
the boxes" or two unsure answers, which the product sends to the reviewer.

**Rows by geometry (#1321).** A row case may name its line by geometry (`RowTruth.at`, with the
key's `row_tolerance_pt`) instead of a box number. Its question is still found by its identity, but
on the picture the question showed, the keyed line is turned into that picture's own answer by the
box map (`eval/pariksha/row_geometry.py`, given as `row_boxes`): the one box within the tolerance,
or "not among the boxes" when none is. Then the readers are scored exactly as against a box-number
key. With no proven boxes for the picture, a box count other than the question's, or two boxes
within the tolerance, the case is *not asked*, with the reason: a box is never guessed.

**Vastu (architect reader).** `eval/architect_reader.score_pages` on the stored spans, one case per
key width (`p<page>:<inches>#<n>`: found, held or missing) and one per usable value the key does not
list (`p<page>:extra:<inches>#<n>`), which a person checks: it is the only way the architect reader
can be confidently wrong, so it counts as a wrong answer when two scorecards are compared.

**Rates are over the questions asked.** A key case the run never asked (or asked about another span)
is listed as `not_asked` with the reason, and counted in `asked`, never as right or wrong. A label
case is found by its span (page, slot box, label position), so a renumbered slot is followed. A rate
over nothing is `None` (not measured), never 0.

Source: issue #1219 · Verification: `tests/eval/test_pariksha_score.py`
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Final

from pydantic import ValidationError

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
from eval.pariksha.row_geometry import RowBoxes, on_picture
from eval.pariksha.stored import (
    StoredArchitectRun,
    StoredAttempt,
    StoredQuestion,
    StoredRun,
    StoredRunError,
)
from eval.runs import MissingProvenanceError
from extraction.form_reader.bedrock import MalformedFormAnswer
from extraction.product_context import PRODUCT_SUFFIX

# The reader's own answer models, which read the v1-v3 shapes (boolean `belongs`, rows with no
# `kind`): the product's parser for a stored answer of that version, used for nothing else here.
from extraction.slot_reader.bedrock import (
    CropJob,
    RowChoiceAnswer,
    _GroundedCropAnswer,
    _RowChoiceReply,
    parse_stored_reader_answer,
    replay_stored_answer,
)
from extraction.slot_reader.seal import Belongs, ReaderAnswer, normalise_text, parse_belongs

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
    "UNRECORDED",
    "WRONG_OUTCOMES",
    "CaseScore",
    "ReaderScore",
    "Scorecard",
    "UnreadableStoredAnswers",
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
_PROMPT_VERSION: Final = re.compile(r"-v(\d+)$")
#: The first prompt version of each family whose stored replies today's strict reader reads
#: (`claude-slot-span-v4` made `belongs` a word; `slot-row-choice-v3` added `kind`). Older
#: replies are read by `_older_shape`.
_STRICT_FROM: Final[Mapping[str, int]] = {"claude-slot-span": 4, "slot-row-choice": 3}
#: A code version is a commit (7 to 40 hex characters), or this word, which the scorecard flags.
UNRECORDED: Final = "unrecorded"
_COMMIT: Final = re.compile(r"^[0-9a-f]{7,40}$")
#: Row answers both readers may give that the product takes without a person: the same box, or
#: both "no countertop" (listed, not blocking). Two "not among the boxes" or "unsure" still go to
#: the reviewer (`workflow/slot_row_scope.py`), so they are split, never an agreement.
_AGREEABLE_ROW_KINDS: Final = frozenset({"row", "no_countertop"})
_REPLAY_MARK: Final = "pariksha-score"


class UnreadableStoredAnswers(StoredRunError):
    """Answers accepted when they were stored cannot be read again: the scorecard is refused."""


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
    if code_version != UNRECORDED and not _COMMIT.match(code_version):
        raise MissingProvenanceError(
            f"the code version of the scored run must be its commit (7 to 40 hex characters) or "
            f"{UNRECORDED!r}, not {code_version!r}; without it a difference between two "
            "scorecards cannot be attributed"
        )


def _code_note(code_version: str) -> dict[str, str]:
    if code_version != UNRECORDED:
        return {}
    return {"code_version_note": "the run's code version is unrecorded: not attributable"}


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _candidate_count(question: StoredQuestion) -> int | None:
    count = question.packet.get("candidate_count")
    if isinstance(count, int) and not isinstance(count, bool):
        return count
    candidates = question.packet.get("candidate_ids")
    return len(candidates) if isinstance(candidates, list) else None


def _replay(question: StoredQuestion, attempt: StoredAttempt) -> object | None:
    """The answer the repository's reader code reads from a stored reply, or `None`."""
    if attempt.outcome != "ok" or attempt.raw_response is None:
        return None
    packet = question.packet
    page_index = packet.get("page_index")
    page = page_index if isinstance(page_index, int) and not isinstance(page_index, bool) else 0
    job: CropJob
    if question.identity.family == "slot-row-choice":
        count = _candidate_count(question)
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


def _version(prompt_id: str) -> int | None:
    found = _PROMPT_VERSION.search(prompt_id.split(PRODUCT_SUFFIX, 1)[0])
    return None if found is None else int(found.group(1))


def _older_shape(question: StoredQuestion, attempt: StoredAttempt) -> object | None:
    """A reply of a prompt version older than today's strict answer shape, read the way the
    product reads that version's stored answers: `parse_stored_reader_answer` for the JSON, the
    reader's own answer model for the fields (v1-v3 `belongs` was a boolean, read by
    `seal.parse_belongs`; v1-v2 rows had no `kind`), and the live reader's range checks."""
    if attempt.raw_response is None:
        return None
    try:
        data = parse_stored_reader_answer(attempt.raw_response)
        if question.identity.family == "slot-row-choice":
            row = _RowChoiceReply.model_validate(data)
            count = _candidate_count(question)
            if count is None or not 0 <= row.row <= count:
                return None
            if row.kind is not None and (row.kind == "row") != (row.row > 0):
                return None
            if any(not 1 <= other <= count for other in row.also):
                return None
            return RowChoiceAnswer(
                model_id=attempt.model_id,
                row=row.row,
                why=row.why[:300],
                kind=row.kind,
                also=tuple(sorted({other for other in row.also if other != row.row})),
            )
        span = _GroundedCropAnswer.model_validate(data)
        return ReaderAnswer(
            model_id=attempt.model_id,
            text=span.text,
            readable=span.readable,
            no_dimension=span.no_dimension,
            stacked=span.stacked,
            combined=span.combined,
            belongs=parse_belongs(span.belongs),
        )
    except (MalformedFormAnswer, ValidationError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class _Read:
    answers: dict[str, object | None]
    older_shape: int
    unreadable: int


def _answers(question: StoredQuestion, readers: Sequence[str]) -> _Read:
    """Each reader's accepted answer to the question, read again; `None` when it has none.

    A reply recorded `ok` that cannot be read again is counted as unreadable, never as the
    reader's invalid answer: it was accepted when it was stored."""
    found: dict[str, object | None] = dict.fromkeys(readers)
    older = unreadable = 0
    version = _version(question.prompt_id)
    strict_from = _STRICT_FROM[question.identity.family]
    for attempt in question.attempts:
        if attempt.outcome != "ok":
            continue
        if version is not None and version < strict_from:
            answer = _older_shape(question, attempt)
            older += answer is not None
        else:
            answer = _replay(question, attempt)
        unreadable += answer is None
        found[attempt.model_id] = answer
    return _Read(answers=found, older_shape=older, unreadable=unreadable)


def _check_run(run: StoredRun) -> None:
    """One question, one packet, at most one accepted answer per reader: anything else is not a
    run that can be scored question by question."""
    seen: dict[QuestionIdentity, StoredQuestion] = {}
    for question in run.questions:
        other = seen.setdefault(question.identity, question)
        if other is not question:
            if other.packet.get("packet_sha256") != question.packet.get("packet_sha256"):
                raise StoredRunError(
                    f"{question.identity} was asked with two different question packets in "
                    f"run {run.extraction_run_id}"
                )
            raise StoredRunError(
                f"{question.identity} is listed twice in run {run.extraction_run_id}"
            )
        accepted = Counter(a.model_id for a in question.attempts if a.outcome == "ok")
        doubled = sorted(model for model, count in accepted.items() if count > 1)
        if doubled:
            raise StoredRunError(
                f"{question.identity} has more than one accepted answer from "
                f"{', '.join(doubled)} in run {run.extraction_run_id}: which one counts is not "
                "knowable"
            )


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
    if not answer.usable:
        # Says it belongs and is readable but gives no text: no reading, as the seal treats it.
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


def _position(question_id: str) -> str:
    """The label's position within its slot: the question id's last part (`p2:slot0:1` → `1`)."""
    return question_id.rsplit(":", 1)[-1]


@dataclass(frozen=True, slots=True)
class _Index:
    by_identity: Mapping[QuestionIdentity, StoredQuestion]
    by_span: Mapping[tuple[str, str, str], tuple[StoredQuestion, ...]]
    """(page hash, slot box, label position) → the label questions about that span."""

    @classmethod
    def of(cls, run: StoredRun) -> _Index:
        spans: dict[tuple[str, str, str], list[StoredQuestion]] = defaultdict(list)
        for question in run.questions:
            if question.slot_box is not None:
                spans[
                    (
                        question.identity.page_sha256,
                        question.slot_box,
                        _position(question.identity.question_id),
                    )
                ].append(question)
        return cls(
            by_identity={question.identity: question for question in run.questions},
            by_span={span: tuple(found) for span, found in spans.items()},
        )


def _match(case: KeyCase, index: _Index) -> StoredQuestion | str:
    question: StoredQuestion | None
    if case.slot_box is not None:
        # By the span, not the slot number: a renumbered slot is followed, never dropped.
        found = index.by_span.get(
            (case.question.page_sha256, case.slot_box, _position(case.question.question_id)), ()
        )
        if len(found) > 1:
            raise StoredRunError(
                f"{len(found)} questions of the run are about the span {case.slot_box} on page "
                f"{case.question.page_sha256[:12]} ({', '.join(str(q.identity) for q in found)}); "
                "which one the key means is not knowable"
            )
        question = found[0] if found else None
        if question is None:
            same_id = index.by_identity.get(case.question)
            if same_id is not None:
                return (
                    f"the run asked {case.question.question_id} about a different span (slot box "
                    f"{same_id.slot_box or 'unknown'}; the key's is {case.slot_box}) and nothing "
                    "about the key's span"
                )
            return f"no question about the span {case.slot_box} on this page in the run"
    else:
        question = index.by_identity.get(case.question)
        if question is None:
            return "no question with this identity in the run"
    if case.picture_sha256 is not None and case.picture_sha256 not in question.picture_sha256:
        return (
            f"the run showed a different picture (not {case.picture_sha256[:12]}), so its box "
            "numbers or span may not be the key's"
        )
    return question


def _on_this_picture(
    case: KeyCase, question: StoredQuestion, key: AnswerKey, row_boxes: RowBoxes | None
) -> tuple[RowTruth, str] | str:
    """A geometry case's truth on the picture this question showed, as that picture's own answer
    (with what to show as expected), or why it cannot be scored on it."""
    truth = case.truth
    assert isinstance(truth, RowTruth) and truth.at is not None
    assert key.row_tolerance_pt is not None
    if row_boxes is None:
        return (
            "no box geometry was given, so the readers' box numbers cannot be matched to the "
            "keyed line (give the run's box map)"
        )
    page = question.identity.page_sha256
    shown = [sha for sha in question.picture_sha256 if row_boxes.get(sha, page) is not None]
    if not shown:
        pictures = ", ".join(sha[:12] for sha in question.picture_sha256) or "none recorded"
        return f"no box geometry for the picture this question showed on its page ({pictures})"
    if len(shown) > 1:
        return (
            f"the box map knows {len(shown)} of the pictures this question showed "
            f"({', '.join(sha[:12] for sha in shown)}); which one numbers its boxes is not knowable"
        )
    picture = row_boxes.get(shown[0], page)
    assert picture is not None
    count = _candidate_count(question)
    if count is not None and count != len(picture.boxes):
        return (
            f"the box map gives picture {picture.picture_sha256[:12]} {len(picture.boxes)} boxes, "
            f"but the question showed {count}"
        )
    found = on_picture(truth.at, picture, key.row_tolerance_pt)
    if isinstance(found, str):
        return found
    return found, f"{found} on picture {picture.picture_sha256[:12]} ({truth.at})"


def _score_pairs(
    agent: str,
    key: AnswerKey,
    run: StoredRun,
    *,
    code_version: str,
    scorer_version: str,
    created_at: str | None,
    row_boxes: RowBoxes | None = None,
) -> Scorecard:
    _provenance(code_version)
    if key.agent != agent:
        raise ValueError(f"the key is {key.agent}'s, not {agent}'s")
    _check_run(run)
    readers = _readers(run)
    index = _Index.of(run)
    held_outcome = HELD if agent == "akshara" else SPLIT
    cases: list[CaseScore] = []
    pair: Counter[str] = Counter()
    per_reader: dict[str, Counter[str]] = defaultdict(Counter)
    cost = 0
    attempts = 0
    reused = 0
    older_shape = 0
    unreadable: list[str] = []
    keyed: set[QuestionIdentity] = set()
    for case in key.cases:
        matched = _match(case, index)
        if isinstance(matched, str):
            cases.append(_not_asked(case, matched))
            continue
        truth = case.truth
        expected = str(case.truth)
        if isinstance(truth, RowTruth) and truth.at is not None:
            here = _on_this_picture(case, matched, key, row_boxes)
            if isinstance(here, str):
                cases.append(_not_asked(case, here))
                continue
            truth, expected = here
        keyed.add(matched.identity)
        read = _answers(matched, readers)
        answers = read.answers
        older_shape += read.older_shape
        unreadable += [str(matched.identity)] * read.unreadable
        verdicts: dict[str, str] = {}
        shown: dict[str, str] = {}
        definite: dict[str, object] = {}
        for model in readers:
            if isinstance(truth, LabelTruth):
                state = _label_state(answers[model])
                verdicts[model] = _label_verdict(state, truth)
                shown[model] = _label_shown(state)
                if state[0] == "yes":
                    definite[model] = state[1]
            else:
                choice = _row_choice(answers[model])
                verdicts[model] = _row_verdict(choice, truth)
                shown[model] = _row_shown(choice)
                if choice[0] in _AGREEABLE_ROW_KINDS:
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
                question=str(matched.identity),
                expected=expected,
                outcome=outcome,
                readers=tuple(
                    ReaderScore(model_id=model, answer=shown[model], verdict=verdicts[model])
                    for model in readers
                ),
                cost_micros=question_cost,
            )
        )
    if unreadable:
        listed = ", ".join(unreadable[:5]) + (", ..." if len(unreadable) > 5 else "")
        raise UnreadableStoredAnswers(
            f"{len(unreadable)} accepted answer(s) of run {run.extraction_run_id} cannot be read "
            f"by today's reader code or by the parser for their prompt version ({listed}). "
            "Scoring them as the readers' invalid answers would hide that the run was not "
            "measured; no scorecard is written"
        )
    asked = sum(pair.values())
    readable = sum(
        1
        for question in run.questions
        if question.identity in keyed
        for attempt in question.attempts
        if attempt.outcome == "ok"
    )
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
    metrics["older_shape_answers"] = _rate(
        "older_shape_answers",
        older_shape,
        readable,
        "accepted answers of an older prompt version, read by that version's parser",
    )
    metrics["unreadable_stored_answers"] = _rate("unreadable_stored_answers", 0, readable)
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
            "reader_settings": run.reader_settings or "not found",
            **_code_note(code_version),
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
    row_boxes: RowBoxes | None = None,
) -> Scorecard:
    """The row choice's scorecard: agreed-right, agreed-wrong and split; per reader. A geometry
    key's cases are matched to the readers' boxes through `row_boxes` (#1321)."""
    return _score_pairs(
        "rekha",
        key,
        run,
        code_version=code_version,
        scorer_version=scorer_version,
        created_at=created_at,
        row_boxes=row_boxes,
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
            **_code_note(code_version),
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
