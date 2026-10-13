"""Compare two Pariksha scorecards: better, and 0 new wrong answers, or not (#1219).

A change to a prompt, a model, a picture or the code ships only when its scorecard is **better and
has no new wrong answer**:

* **New wrong**: a case whose outcome after the change is a confident wrong one
  (`score.WRONG_OUTCOMES`: agreed-wrong, or Vastu's unlisted usable value) and was not before. A
  case the older run never asked counts too: its first answer can be the new wrong one.
  `eval/regression.compare_cases` skips a case present in only one run, which is right for a gold
  set whose cases are added over time and wrong here, so the per-case walk is its own.
* **No longer asked**: a case the older run asked and the newer did not. Not asking a question is
  never a way to stop answering it wrong, so each one rejects the change, and so does any fall in
  `asked`.
* **Better**: more right automatic answers (`score.RIGHT_OUTCOMES`) and no more wrong ones, or as
  many right and fewer wrong.

Two scorecards made with different keys (or, for Vastu, of different documents) are not compared
at all: a bigger or corrected key moves the score without the agent changing.

Per metric, each before → after is an `eval.regression.MetricDelta`, so "better" and "worse" are
judged by the same code the release gate uses, with this module's directions. Per case, every
change is listed: fixed (now agreed-right or found), broke (was, and is not), new wrong, or another
move (for example agreed-wrong to held, which is safer but not yet right).

Source: issue #1219 · Verification: `tests/eval/test_pariksha_scorecard.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from eval.metrics import MetricResult
from eval.pariksha.score import (
    NOT_ASKED,
    RIGHT_OUTCOMES,
    UNRECORDED,
    WRONG_OUTCOMES,
    CaseScore,
    Scorecard,
)
from eval.regression import IncomparableRuns, MetricDelta
from eval.release_metrics import Direction

__all__ = ["CaseChange", "Comparison", "compare_scorecards", "metric_direction"]

#: Metrics where more is better; every other scored metric is better lower. `unkeyed_questions`
#: and `reused_answers` describe the run and the key, not the agent, so they have no direction.
_HIGHER: Final = ("asked", "pair_agreed_right", "reader_right:", "found")
_NEITHER: Final = ("unkeyed_questions", "reused_answers")
_LISTED: Final = 10


def metric_direction(name: str) -> Direction | None:
    if name.startswith(_NEITHER):
        return None
    return Direction.MINIMUM if name.startswith(_HIGHER) else Direction.MAXIMUM


@dataclass(frozen=True, slots=True)
class CaseChange:
    case_id: str
    before: str
    after: str
    expected_before: str
    expected_after: str


@dataclass(frozen=True, slots=True)
class Comparison:
    agent: str
    before: Scorecard
    after: Scorecard
    metrics: tuple[MetricDelta, ...]
    fixed: tuple[CaseChange, ...]
    broke: tuple[CaseChange, ...]
    new_wrong: tuple[CaseChange, ...]
    moved: tuple[CaseChange, ...]
    """Changes that are none of the above (for example agreed-wrong to held)."""
    dropped: tuple[CaseChange, ...]
    """Cases asked before and not asked after: each one rejects the change."""
    unchanged: int
    accepted: bool
    verdict: str

    @property
    def key_changed(self) -> bool:
        return self.before.key_sha256 != self.after.key_sha256

    def render(self) -> str:
        before, after = self.before, self.after
        lines = [
            f"Pariksha compare: {self.agent}",
            f"  before: {_provenance(before)}",
            f"  after:  {_provenance(after)}",
        ]
        if self.key_changed:
            lines.append(
                f"  NOTE: the answer keys differ ({before.key_name} {before.key_version} -> "
                f"{after.key_name} {after.key_version}); cases are compared by their identity"
            )
        lines += ["", "Metrics (before -> after):"]
        for delta in self.metrics:
            if delta.improved:
                judged = "better"
            elif delta.regressed:
                judged = "worse"
            elif delta.measurement_gained:
                judged = "newly measured"
            elif delta.before != delta.after:
                judged = "changed; describes the run and key, not the agent"
            else:
                judged = "same"
            shown_before = _shown(before.metrics.get(delta.metric))
            shown_after = _shown(after.metrics.get(delta.metric))
            lines.append(f"  {delta.metric}: {shown_before} -> {shown_after} ({judged})")
        lines += ["", "Cases:"]
        for label, changes in (
            ("NEW WRONG", self.new_wrong),
            ("NO LONGER ASKED", self.dropped),
            ("fixed", self.fixed),
            ("broke", self.broke),
            ("moved", self.moved),
        ):
            for change in changes:
                if label == "broke" and (change in self.new_wrong or change in self.dropped):
                    continue
                if label == "moved" and change in self.dropped:
                    continue
                lines.append(f"  {change.case_id}: {change.before} -> {change.after} ({label})")
        lines.append(f"  unchanged: {self.unchanged}")
        lines += ["", self.verdict]
        return "\n".join(lines)


def _provenance(card: Scorecard) -> str:
    flag = " (NOT ATTRIBUTABLE)" if card.code_version == UNRECORDED else ""
    return (
        f"code {card.code_version}{flag}, prompts {card.extractor_versions.get('prompt_ids')}, "
        f"run {card.source.get('database')}/{card.source.get('extraction_run_id')}"
    )


def _listed(changes: list[CaseChange]) -> str:
    listed = ", ".join(change.case_id for change in changes[:_LISTED])
    return listed + (f", and {len(changes) - _LISTED} more" if len(changes) > _LISTED else "")


def _asked(card: Scorecard) -> int | None:
    metric = card.metrics.get("asked")
    return None if metric is None else metric.numerator


def _comparable(before: Scorecard, after: Scorecard) -> None:
    """Refuse a comparison whose difference would not be the agent's."""
    if before.agent != after.agent:
        raise IncomparableRuns(
            f"a {before.agent} scorecard and a {after.agent} scorecard measure different agents"
        )
    if before.key_sha256 != after.key_sha256:
        raise IncomparableRuns(
            f"the scorecards were made with different answer keys ({before.key_name} "
            f"{before.key_version}, {before.key_sha256[:12]}; {after.key_name} "
            f"{after.key_version}, {after.key_sha256[:12]}): a bigger or corrected key moves the "
            "score without the agent changing. Score both runs with the same key"
        )
    documents = (before.source.get("document_sha256"), after.source.get("document_sha256"))
    if documents[0] != documents[1]:
        raise IncomparableRuns(
            f"the scorecards read different documents ({str(documents[0])[:12]}, "
            f"{str(documents[1])[:12]})"
        )
    for card in (before, after):
        unreadable = card.metrics.get("unreadable_stored_answers")
        if unreadable is not None and unreadable.numerator:
            raise IncomparableRuns(
                f"the scorecard of run {card.source.get('extraction_run_id')} has "
                f"{unreadable.numerator} stored answer(s) it could not read"
            )


def _shown(metric: MetricResult | None) -> str:
    if metric is None or metric.value is None:
        return "not measured"
    return f"{metric.numerator}/{metric.denominator}"


def _outcomes(card: Scorecard) -> dict[str, CaseScore]:
    return {case.case_id: case for case in card.cases}


def compare_scorecards(before: Scorecard, after: Scorecard) -> Comparison:
    """Before → after per metric and per case, and the verdict."""
    _comparable(before, after)
    deltas = tuple(
        MetricDelta(
            metric=name,
            check_type=before.agent,
            before=None if name not in before.metrics else before.metrics[name].value,
            after=None if name not in after.metrics else after.metrics[name].value,
            direction=metric_direction(name),
        )
        for name in sorted(set(before.metrics) | set(after.metrics))
    )
    old, new = _outcomes(before), _outcomes(after)
    fixed: list[CaseChange] = []
    broke: list[CaseChange] = []
    new_wrong: list[CaseChange] = []
    moved: list[CaseChange] = []
    dropped: list[CaseChange] = []
    unchanged = 0
    for case_id in sorted(set(old) | set(new)):
        was, now = old.get(case_id), new.get(case_id)
        change = CaseChange(
            case_id=case_id,
            before=NOT_ASKED if was is None else was.outcome,
            after=NOT_ASKED if now is None else now.outcome,
            expected_before="" if was is None else was.expected,
            expected_after="" if now is None else now.expected,
        )
        if change.before == change.after and change.expected_before == change.expected_after:
            unchanged += 1
            continue
        sorted_into = False
        if change.before != NOT_ASKED and change.after == NOT_ASKED:
            # Not asking a question is never a way to stop answering it wrong.
            dropped.append(change)
        if change.after in WRONG_OUTCOMES and change.before not in WRONG_OUTCOMES:
            new_wrong.append(change)
            sorted_into = True
        if change.after in RIGHT_OUTCOMES and change.before not in RIGHT_OUTCOMES:
            fixed.append(change)
            sorted_into = True
        if change.before in RIGHT_OUTCOMES and change.after not in RIGHT_OUTCOMES:
            broke.append(change)
            sorted_into = True
        if not sorted_into:
            moved.append(change)

    def count(card: Scorecard, outcomes: frozenset[str]) -> int:
        return sum(1 for case in card.cases if case.outcome in outcomes)

    right_before, right_after = count(before, RIGHT_OUTCOMES), count(after, RIGHT_OUTCOMES)
    wrong_before, wrong_after = count(before, WRONG_OUTCOMES), count(after, WRONG_OUTCOMES)
    better = (right_after > right_before and wrong_after <= wrong_before) or (
        right_after >= right_before and wrong_after < wrong_before
    )
    reasons: list[str] = []
    if new_wrong:
        reasons.append(f"{len(new_wrong)} new wrong: {_listed(new_wrong)}")
    if dropped:
        reasons.append(f"{len(dropped)} no longer asked: {_listed(dropped)}")
    asked_before, asked_after = _asked(before), _asked(after)
    if asked_before is not None and asked_after is not None and asked_after < asked_before:
        reasons.append(f"asked {asked_before} -> {asked_after}")
    if not reasons and not better:
        reasons.append(
            f"not better: right {right_before} -> {right_after}, "
            f"wrong {wrong_before} -> {wrong_after}"
        )
    verdict = f"REJECT ({'; '.join(reasons)})" if reasons else "ACCEPT (better, 0 new wrong)"
    return Comparison(
        agent=before.agent,
        before=before,
        after=after,
        metrics=deltas,
        fixed=tuple(fixed),
        broke=tuple(broke),
        new_wrong=tuple(new_wrong),
        moved=tuple(moved),
        dropped=tuple(dropped),
        unchanged=unchanged,
        accepted=verdict.startswith("ACCEPT"),
        verdict=verdict,
    )
