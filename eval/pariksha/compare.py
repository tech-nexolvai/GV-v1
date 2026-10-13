"""Compare two Pariksha scorecards: better, and 0 new wrong answers, or not (#1219).

A change to a prompt, a model, a picture or the code ships only when its scorecard is **better and
has no new wrong answer**:

* **New wrong**: a case whose outcome after the change is a confident wrong one
  (`score.WRONG_OUTCOMES`: agreed-wrong, or Vastu's unlisted usable value) and was not before. A
  case the older run never asked counts too: its first answer can be the new wrong one.
  `eval/regression.compare_cases` skips a case present in only one run, which is right for a gold
  set whose cases are added over time and wrong here, so the per-case walk is its own.
* **Better**: more right automatic answers (`score.RIGHT_OUTCOMES`) and no more wrong ones, or as
  many right and fewer wrong.

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
from eval.pariksha.score import NOT_ASKED, RIGHT_OUTCOMES, WRONG_OUTCOMES, CaseScore, Scorecard
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
            ("fixed", self.fixed),
            ("broke", self.broke),
            ("moved", self.moved),
        ):
            for change in changes:
                if label == "broke" and change in self.new_wrong:
                    continue
                lines.append(f"  {change.case_id}: {change.before} -> {change.after} ({label})")
        lines.append(f"  unchanged: {self.unchanged}")
        lines += ["", self.verdict]
        return "\n".join(lines)


def _provenance(card: Scorecard) -> str:
    return (
        f"code {card.code_version}, prompts {card.extractor_versions.get('prompt_ids')}, "
        f"run {card.source.get('database')}/{card.source.get('extraction_run_id')}"
    )


def _shown(metric: MetricResult | None) -> str:
    if metric is None or metric.value is None:
        return "not measured"
    return f"{metric.numerator}/{metric.denominator}"


def _outcomes(card: Scorecard) -> dict[str, CaseScore]:
    return {case.case_id: case for case in card.cases}


def compare_scorecards(before: Scorecard, after: Scorecard) -> Comparison:
    """Before → after per metric and per case, and the verdict."""
    if before.agent != after.agent:
        raise IncomparableRuns(
            f"a {before.agent} scorecard and a {after.agent} scorecard measure different agents"
        )
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
    if new_wrong:
        listed = ", ".join(change.case_id for change in new_wrong[:_LISTED])
        more = f", and {len(new_wrong) - _LISTED} more" if len(new_wrong) > _LISTED else ""
        verdict = f"REJECT ({len(new_wrong)} new wrong: {listed}{more})"
    elif not better:
        verdict = (
            f"REJECT (not better: right {right_before} -> {right_after}, "
            f"wrong {wrong_before} -> {wrong_after})"
        )
    else:
        verdict = "ACCEPT (better, 0 new wrong)"
    return Comparison(
        agent=before.agent,
        before=before,
        after=after,
        metrics=deltas,
        fixed=tuple(fixed),
        broke=tuple(broke),
        new_wrong=tuple(new_wrong),
        moved=tuple(moved),
        unchanged=unchanged,
        accepted=verdict.startswith("ACCEPT"),
        verdict=verdict,
    )
