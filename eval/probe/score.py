"""Scoring a probe: did the finding stage raise the problems that are there? (Phase 1)

What the probe can measure, in the order it matters:

1. **Recall on the covered problems.** Of the known problems the page set is a fair test of, how
   many did the run raise.
2. **The control-page noise rate.** Flags raised per page on pages with no known problem. Recall
   alone is maximised by flagging everything; this is the number that catches it.
3. **The per-problem hit rate over repeats.** The finding stage is not repeatable — on the two
   audited runs of one job, thirteen known problems swung and eleven of those were losses — so a
   single pass/fail is not a measurement. A problem found 3 of 3 times is fixed; 1 of 3 is luck.

**How a problem is judged raised.** By the exam's own matcher (`replay.auto_status`): an answer
must sit on one of the miss's pages and carry at least three of its cue words. That matcher is a
screen, not a verdict, and its error is known, because it was run against the 121 hand-scored
run/miss pairs:

| threshold | problems the helpers found that it finds | problems they did not that it leaves alone |
|---|---|---|
| 2 cues | 12 of 13 | 47 of 95 |
| **3 cues (default)** | **10 of 13** | **76 of 95** |
| 4 cues | 6 of 13 | 88 of 95 |

So a probe's recall reads about a fifth high on the misses and about a quarter low on the hits.
That is good enough to rank two arms, and not good enough to quote as the product's recall. Every
score carries the finding id the matcher leant on (`ProblemScore.evidence`) so a person can settle
a doubtful one in a minute rather than re-auditing a run.

**What a false alarm is here.** Nothing in this package can know that a flag is wrong — that needs
a hand audit. A flag on a *control* page is the closest honest proxy: the page has no known
problem, so a run that flags it is either noticing something the helpers missed or making it up,
and a change that raises that rate is making more noise. The miss list is a floor, so the rate is
an upper bound on correctness, not a false-alarm count.

Nothing here spends anything.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from eval.exam.replay import auto_status
from eval.exam.truth import LISTED_GROUPS, Miss
from eval.probe.pages import Selection

__all__ = [
    "ArmScore",
    "ProblemScore",
    "RepeatFindings",
    "compare",
    "score_arm",
]


@dataclass(frozen=True)
class RepeatFindings:
    """One run of one arm: the answers it produced, and what it cost."""

    repeat: int
    findings: list[dict[str, Any]]
    cost_usd: float = 0.0
    seconds: float = 0.0


def _page_number(finding: dict[str, Any]) -> int | None:
    stuck_report = import_module("app.review.assistant.stuck_report")
    page = stuck_report._page_number(finding.get("page"))
    return int(page) if isinstance(page, int) else None


def _listed(finding: dict[str, Any]) -> bool:
    """Whether this answer reached the problems list a person reads."""
    stuck_report = import_module("app.review.assistant.stuck_report")
    return stuck_report.group_of(finding) in LISTED_GROUPS


@dataclass
class ProblemScore:
    """One known problem, across every repeat of one arm."""

    miss_id: str
    kind: str
    severity: str
    pages: tuple[int, ...]
    found: int = 0
    repeats: int = 0
    #: The finding the matcher leant on, per repeat it was found in: for settling a doubt by hand.
    evidence: list[str] = field(default_factory=list)

    @property
    def hit_rate(self) -> float | None:
        return self.found / self.repeats if self.repeats else None

    @property
    def verdict(self) -> str:
        """`found` every time, `flaky` some of the time, `missed` never."""
        if not self.repeats:
            return "not run"
        if self.found == self.repeats:
            return "found"
        return "flaky" if self.found else "missed"


@dataclass
class ArmScore:
    """One arm of an experiment, over all its repeats."""

    arm: str
    job: str
    repeats: int = 0
    pages: int = 0
    control_pages: int = 0
    problems: list[ProblemScore] = field(default_factory=list)
    #: Problems whose sheets are only partly in the page set: reported, never scored.
    partly: tuple[str, ...] = ()
    #: Problems asked for that this page set cannot see at all.
    uncovered: tuple[str, ...] = ()
    flags_on_problem_pages: list[int] = field(default_factory=list)
    flags_on_control_pages: list[int] = field(default_factory=list)
    answers: list[int] = field(default_factory=list)
    cost_usd: float = 0.0
    seconds: float = 0.0

    @property
    def scoreable(self) -> int:
        return len(self.problems)

    @property
    def found_every_time(self) -> int:
        return sum(1 for problem in self.problems if problem.verdict == "found")

    @property
    def flaky(self) -> int:
        return sum(1 for problem in self.problems if problem.verdict == "flaky")

    @property
    def missed(self) -> int:
        return sum(1 for problem in self.problems if problem.verdict == "missed")

    @property
    def mean_recall(self) -> float | None:
        """The average share of covered problems a single run raises."""
        if not self.problems or not self.repeats:
            return None
        return sum(problem.found for problem in self.problems) / (len(self.problems) * self.repeats)

    @property
    def best_recall(self) -> float | None:
        """The share raised by at least one repeat: the ceiling this arm can reach."""
        if not self.problems:
            return None
        return sum(1 for problem in self.problems if problem.found) / len(self.problems)

    @property
    def control_noise(self) -> float | None:
        """Flags per control page per run. Lower is quieter; it is not a false-alarm count."""
        if not self.control_pages or not self.flags_on_control_pages:
            return None
        runs = len(self.flags_on_control_pages)
        return sum(self.flags_on_control_pages) / (self.control_pages * runs)

    @property
    def problem_flags_per_page(self) -> float | None:
        if not self.pages or not self.flags_on_problem_pages:
            return None
        runs = len(self.flags_on_problem_pages)
        pages = self.pages - self.control_pages
        return sum(self.flags_on_problem_pages) / (pages * runs) if pages else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "arm": self.arm,
            "job": self.job,
            "repeats": self.repeats,
            "pages": self.pages,
            "control_pages": self.control_pages,
            "scoreable_problems": self.scoreable,
            "found_every_time": self.found_every_time,
            "flaky": self.flaky,
            "missed": self.missed,
            "mean_recall": self.mean_recall,
            "best_recall": self.best_recall,
            "control_noise_per_page_per_run": self.control_noise,
            "problem_flags_per_page_per_run": self.problem_flags_per_page,
            "answers_per_run": self.answers,
            "partly_covered": list(self.partly),
            "not_covered": list(self.uncovered),
            "cost_usd": round(self.cost_usd, 2),
            "seconds": round(self.seconds),
            "problems": [
                {
                    "miss_id": problem.miss_id,
                    "kind": problem.kind,
                    "severity": problem.severity,
                    "pages": list(problem.pages),
                    "found": problem.found,
                    "of": problem.repeats,
                    "verdict": problem.verdict,
                    "evidence": problem.evidence,
                }
                for problem in self.problems
            ],
        }


def score_arm(
    arm: str,
    selection: Selection,
    repeats: list[RepeatFindings],
    misses: list[Miss],
) -> ArmScore:
    """Score every repeat of one arm against the problems its page set is a fair test of."""
    by_id = {miss.miss_id: miss for miss in misses}
    covered = [by_id[miss_id] for miss_id in selection.covered if miss_id in by_id]
    controls = set(selection.control_pages)
    problem_pages = set(selection.problem_pages)

    score = ArmScore(
        arm=arm,
        job=selection.job,
        repeats=len(repeats),
        pages=len(selection.pages),
        control_pages=len(controls),
        partly=selection.partly,
        uncovered=selection.uncovered,
    )
    tally = {
        miss.miss_id: ProblemScore(
            miss_id=miss.miss_id,
            kind=miss.kind,
            severity=miss.severity,
            pages=miss.pages,
            repeats=len(repeats),
        )
        for miss in covered
    }
    for run in repeats:
        score.cost_usd += run.cost_usd
        score.seconds += run.seconds
        score.answers.append(len(run.findings))
        flags: Counter[str] = Counter()
        for finding in run.findings:
            if not _listed(finding):
                continue
            page = _page_number(finding)
            if page in controls:
                flags["control"] += 1
            elif page in problem_pages:
                flags["problem"] += 1
        score.flags_on_control_pages.append(flags["control"])
        score.flags_on_problem_pages.append(flags["problem"])
        for miss in covered:
            status, finding_id = auto_status(miss, run.findings)
            if status == "found":
                tally[miss.miss_id].found += 1
                tally[miss.miss_id].evidence.append(f"r{run.repeat}:{finding_id}")
    score.problems = [tally[miss.miss_id] for miss in covered]
    return score


def compare(before: ArmScore, after: ArmScore) -> dict[str, Any]:
    """Two arms on the same pages, side by side, and which problems moved between them.

    Both numbers are reported together on purpose. Recall that went up while the control-page
    noise went up with it is not an improvement, it is a louder run.
    """
    moved_to: list[str] = []
    moved_from: list[str] = []
    old = {problem.miss_id: problem for problem in before.problems}
    for problem in after.problems:
        was = old.get(problem.miss_id)
        if was is None:
            continue
        if problem.found > was.found:
            moved_to.append(f"{problem.miss_id} ({was.found}->{problem.found} of {was.repeats})")
        elif problem.found < was.found:
            moved_from.append(f"{problem.miss_id} ({was.found}->{problem.found} of {was.repeats})")

    def _delta(left: float | None, right: float | None) -> float | None:
        return None if left is None or right is None else right - left

    return {
        "before": before.as_dict(),
        "after": after.as_dict(),
        "gained": moved_to,
        "lost": moved_from,
        "mean_recall_delta": _delta(before.mean_recall, after.mean_recall),
        "control_noise_delta": _delta(before.control_noise, after.control_noise),
        "cost_usd": round(before.cost_usd + after.cost_usd, 2),
        "verdict": _verdict(before, after, moved_to, moved_from),
    }


def _problems(count: int) -> str:
    return f"{count} problem" if count == 1 else f"{count} problems"


def _verdict(before: ArmScore, after: ArmScore, gained: list[str], lost: list[str]) -> str:
    """One plain line, written so nobody has to read the table to get the headline."""
    moved = f"{_problems(len(gained))} gained, {len(lost)} lost"
    head = f"{after.arm} against {before.arm}"
    if before.mean_recall is None or after.mean_recall is None:
        return f"{head}: not enough runs to say"
    recall = after.mean_recall - before.mean_recall
    noise = (after.control_noise or 0.0) - (before.control_noise or 0.0)
    if recall > 0 and noise <= 0:
        return f"{head}: better, {moved}, control pages no louder"
    if recall > 0:
        return (
            f"{head}: found more ({moved}) but the control pages got louder by "
            f"{noise:+.2f} flags a page, so check whether it is just shouting"
        )
    if recall < 0:
        return f"{head}: worse, {moved}"
    return f"{head}: no change in what was found"
