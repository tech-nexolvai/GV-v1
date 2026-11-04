"""Re-group a saved run's findings with today's code and score them against the exam (harness rebuild, Phase 0).

This is the $0 gate. It never calls an AI, never opens a drawing and never touches a database: it
reads the findings a paid run already produced, asks the **current** `proof.proof_group` where each
one would go now, and compares both that and the group the run actually stored against the hand
audits. A change that improves the score here has earned a paid run; one that does not, has not.

**Two groups, both reported.** A saved finding carries the `proof.group` the live server worked
out while the run was going (`stored`). Re-grouping the same finding with the code on this branch
can disagree, because grouping code merged since changes where an answer lands — on the flag run it
moves 143 answers out of the problems list. The exam scores `stored` as *what the user saw*, which
is the thing the audits judged, and reports `today` beside it as *what this branch would do*, so a
later phase can see both without either being mistaken for the other. Closing that gap is Phase 2's
job, not this one's.

**Precision is not the measure that matters most.** Every finding is written by an AI, and code
only does arithmetic on what the AI already wrote down, so a problem the AI never noticed is
invisible to every later stage — and to every precision score ever taken on this product. The miss
tables (`misses.tsv`, `miss_status.tsv`) hold the real problems the helpers found on the drawings,
with each run's hand-scored status, and `score_misses` reports recall from them. Its automatic
matcher is deliberately mean: a page must match and at least two of the miss's cues must appear in
some answer's text. It never overwrites the helper's own status; where the two disagree it says so,
for a person to settle.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from eval.exam.truth import (
    LISTED_GROUPS,
    Case,
    Miss,
    MissStatus,
    Pairing,
    truth_of,
)

__all__ = [
    "FLAG_MODE_ENV",
    "GROUPS",
    "PAIRING_CLASSES",
    "RIGHT_PAIRINGS",
    "Scores",
    "coverage_of",
    "diff_stage",
    "findings_of",
    "groups_of",
    "guessed_pages",
    "pairing_class",
    "pairing_stage",
    "right_paired_pages",
    "score_cases",
    "score_misses",
    "score_pairing",
    "table",
]

#: The environment switch the server reads for flag mode (`classification.flag_mode`).
FLAG_MODE_ENV: Final = "GV_ASSISTANT_FLAG_MODE"

#: The groups the result screen shows, in the order the table prints them.
GROUPS: Final = ("wrong", "likely_problem", "not_compared", "right")

#: How many of a miss's cues must appear before the matcher will call it found: three, or all of
#: them when the miss has fewer. Two was tried first and matched eighteen misses on a run whose
#: helpers found two, because single domain words ("sink", "width") are in half the answers on a
#: drawing set.
CUES_NEEDED: Final = 3

#: The finding fields the miss matcher searches.
_SEARCHED: Final = (
    "item",
    "why",
    "vendor",
    "architect",
    "printed_string",
    "printed_total",
    "sum_text",
    "not_compared_why",
)


@contextmanager
def flag_mode(on: bool) -> Iterator[None]:
    """Run the body with flag mode on or off, putting the environment back afterwards. The
    grouping code reads the switch at call time, so this is how a run is re-grouped as it ran."""
    before = os.environ.get(FLAG_MODE_ENV)
    os.environ[FLAG_MODE_ENV] = "flag" if on else ""
    try:
        yield
    finally:
        if before is None:
            os.environ.pop(FLAG_MODE_ENV, None)
        else:
            os.environ[FLAG_MODE_ENV] = before


def findings_of(path: Path) -> list[dict[str, Any]]:
    """Every answer of a saved run's newest full check, in the order it was shown. This is the
    stuck report's own loader, so the exam sees exactly the answers the person saw."""
    import json

    stuck_report = import_module("app.review.assistant.stuck_report")
    blocks, _ = stuck_report._blocks_of(json.loads(path.read_text(encoding="utf-8")))
    rows, _ = stuck_report.answers(blocks)
    return rows


def coverage_of(path: Path) -> dict[str, Any] | None:
    """The coverage block of a saved run's newest full check: one entry per vendor page with the
    architect page it was compared with (`pair_page`) and whether code was unsure of that pairing
    (`pairing_unsure`). `None` when the run has none."""
    import json

    stuck_report = import_module("app.review.assistant.stuck_report")
    blocks, _ = stuck_report._blocks_of(json.loads(path.read_text(encoding="utf-8")))
    found = stuck_report._coverage(blocks)
    return dict(found) if found else None


def groups_of(findings: list[dict[str, Any]], flag: bool) -> dict[str, tuple[str, str]]:
    """Each finding's (stored, today) group. `stored` is the group the live server wrote into the
    saved finding; `today` is what `proof.proof_group` gives for the same finding on this branch,
    under the same flag-mode setting the run used."""
    stuck_report = import_module("app.review.assistant.stuck_report")
    proof = import_module("app.review.assistant.proof")
    screen = stuck_report._SCREEN
    out: dict[str, tuple[str, str]] = {}
    with flag_mode(flag):
        for finding in findings:
            today = proof.proof_group(finding)
            out[str(finding.get("id"))] = (
                stuck_report.group_of(finding),
                screen.get(today, today),
            )
    return out


@dataclass
class Scores:
    """One run's score on one side (the stored groups, or today's)."""

    run: str
    side: str
    cases: int = 0
    listed: int = 0
    listed_right: int = 0
    should_list: int = 0
    leaks_problem: int = 0
    leaks_agreement: int = 0
    reds: int = 0
    reds_true: int = 0
    checks: int = 0
    checks_true: int = 0
    per_group: dict[str, Counter[str]] = field(default_factory=dict)
    unknown: int = 0

    @property
    def precision(self) -> float | None:
        """Of what the run put on the problems list, the share that belonged there."""
        return self.listed_right / self.listed if self.listed else None

    @property
    def recall(self) -> float | None:
        """Of the audited answers that belonged on the problems list, the share it listed. This is
        recall *within the answers the run produced*: a problem no answer names at all cannot
        appear here at any score. `score_misses` is the one that sees those."""
        return self.listed_right / self.should_list if self.should_list else None

    @property
    def red_precision(self) -> float | None:
        """Of the reds, the share that belonged on the problems list."""
        return self.reds_true / self.reds if self.reds else None

    @property
    def check_precision(self) -> float | None:
        """Of the please-checks, the share that belonged on the problems list."""
        return self.checks_true / self.checks if self.checks else None


def score_cases(
    cases: list[Case],
    groups: dict[str, dict[str, tuple[str, str]]],
    pages: Mapping[str, set[int]] | None = None,
) -> list[Scores]:
    """Score every run on both sides. `groups` maps a run tag to its findings' (stored, today)
    groups, as `groups_of` returns them. With `pages` (run tag to vendor pages), only the cases on
    those pages are scored, and a run missing from it scores no case."""
    out: list[Scores] = []
    for run in sorted({case.run for case in cases}):
        found = groups.get(run)
        if found is None:
            continue
        kept = None if pages is None else pages.get(run, set())
        for side, index in (("stored", 0), ("today", 1)):
            score = Scores(run=run, side=side)
            score.per_group = {group: Counter() for group in GROUPS}
            for case in cases:
                if case.run != run or case.finding_id not in found:
                    continue
                if kept is not None and case.page not in kept:
                    continue
                group = found[case.finding_id][index]
                truth = truth_of(case.verdict, group)
                score.cases += 1
                if truth.listed is None:
                    score.unknown += 1
                    continue
                if group in LISTED_GROUPS:
                    score.listed += 1
                    score.listed_right += int(truth.listed)
                if group == "wrong":
                    score.reds += 1
                    score.reds_true += int(truth.listed)
                elif group == "likely_problem":
                    score.checks += 1
                    score.checks_true += int(truth.listed)
                if truth.listed:
                    score.should_list += 1
                    if group not in LISTED_GROUPS:
                        score.leaks_problem += 1
                elif truth.group == "right" and group == "not_compared":
                    score.leaks_agreement += 1
                if group in score.per_group:
                    score.per_group[group][truth.group] += 1
            out.append(score)
    return out


def score_pairing(pairing: list[Pairing]) -> dict[str, Counter[str]]:
    """Per run, how many vendor pages went to the right architect page. `view` means the page was
    right and the view on it was not, which invents differences just as a wrong page does."""
    out: dict[str, Counter[str]] = {}
    for row in pairing:
        out.setdefault(row.run, Counter())[row.correct] += 1
    return out


# ---- the pairing stage (rebuild plan Step 1A) ----------------------------------------------------

#: How one audited vendor page was paired, in the order the stage table prints them:
#:
#: - `exact`: the right architect page, and the run did not mark the pairing as a guess;
#: - `best_guess`: the right architect page, but the run marked it a best guess or code was
#:   unsure of it (flag mode compares such a page with its guess; proof mode leaves it unsure);
#: - `nothing_to_compare`: the audit says no architect page shows this one, and the run did not
#:   compare it with a wrong one;
#: - `wrong_view`: the right sheet, the wrong view on it;
#: - `wrong_page`: another architect page;
#: - `unpaired`: no architect page, where the audit names one.
PAIRING_CLASSES: Final = (
    "exact",
    "best_guess",
    "nothing_to_compare",
    "wrong_view",
    "wrong_page",
    "unpaired",
)

#: The classes that compare the vendor's page with what the audit says it shows.
RIGHT_PAIRINGS: Final = frozenset({"exact", "best_guess", "nothing_to_compare"})

#: The words flag mode writes into a finding compared on a best-guess page
#: (`reference_page.guess_words`), which runs saved before `pairing_guess` existed carry only in
#: their stored group's reason.
_GUESS_WORDS: Final = re.compile(r"best guess: architect page (\d+)")


def guessed_pages(
    findings: list[dict[str, Any]], coverage: Mapping[str, Any] | None
) -> dict[int, set[int]]:
    """The vendor pages a saved run paired only as a guess, each with the architect pages it
    guessed (an empty set when the run did not say which).

    Three signs, any one is enough: the coverage block says code was unsure of the page's pairing
    (`pairing_unsure`, with its candidate pages); a finding on the page carries `pairing_guess`
    (flag mode, `reference_page.mark_guess`); or a finding's stored reason carries flag mode's
    words "best guess: architect page N"."""
    out: dict[int, set[int]] = {}
    for entry in (coverage or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or not entry.get("pairing_unsure"):
            continue
        page = entry.get("page")
        if not isinstance(page, int):
            continue
        listed = out.setdefault(page, set())
        for candidate in entry.get("pairing_unsure_pages") or []:
            number = candidate.get("page") if isinstance(candidate, Mapping) else None
            if isinstance(number, int):
                listed.add(number)
    for finding in findings:
        page = finding.get("page")
        if not isinstance(page, int):
            continue
        guess = finding.get("pairing_guess")
        if isinstance(guess, Mapping):
            number = guess.get("page")
            listed = out.setdefault(page, set())
            if isinstance(number, int):
                listed.add(number)
        proof = finding.get("proof")
        reason = proof.get("reason") if isinstance(proof, Mapping) else None
        for match in _GUESS_WORDS.finditer(str(reason or "")):
            out.setdefault(page, set()).add(int(match.group(1)))
    return out


def pairing_class(row: Pairing, guessed: bool) -> str:
    """Which of `PAIRING_CLASSES` one audited vendor page is. `guessed`: the run paired the page
    only as a guess (`guessed_pages`). `unknown` for an audit value the exam does not know."""
    if row.correct == "view":
        return "wrong_view"
    if row.correct == "no":
        return "unpaired" if row.run_architect_page == "none" else "wrong_page"
    if row.correct != "yes":
        return "unknown"
    if row.right_architect_page == "none":
        return "nothing_to_compare"
    return "best_guess" if guessed else "exact"


def pairing_stage(
    pairing: list[Pairing], guesses: Mapping[str, Mapping[int, set[int]]]
) -> dict[str, dict[str, Any]]:
    """The pairing exam, per run: how many audited vendor pages fell in each of
    `PAIRING_CLASSES`, and how many of the wrong ones (wrong view, wrong page) the run had at least
    marked a guess. `guesses` maps a run tag to `guessed_pages`; a run missing from it is scored
    as if it marked no guess."""
    out: dict[str, dict[str, Any]] = {}
    for row in pairing:
        guessed = row.vendor_page in guesses.get(row.run, {})
        kind = pairing_class(row, guessed)
        entry = out.setdefault(
            row.run,
            {"pages": 0, "wrong_marked_guess": 0, **{name: 0 for name in PAIRING_CLASSES}},
        )
        entry["pages"] += 1
        entry[kind] = entry.get(kind, 0) + 1
        if guessed and kind in {"wrong_view", "wrong_page"}:
            entry["wrong_marked_guess"] += 1
    return dict(sorted(out.items()))


def right_paired_pages(
    pairing: list[Pairing], guesses: Mapping[str, Mapping[int, set[int]]]
) -> dict[str, set[int]]:
    """Per run, the audited vendor pages compared with what the audit says they show
    (`RIGHT_PAIRINGS`). The diff exam scores these on their own, so a pairing failure is never
    counted as a finding failure."""
    out: dict[str, set[int]] = {}
    for row in pairing:
        guessed = row.vendor_page in guesses.get(row.run, {})
        if pairing_class(row, guessed) in RIGHT_PAIRINGS:
            out.setdefault(row.run, set()).add(row.vendor_page)
    return out


# ---- the diff stage (rebuild plan Step 1A) -------------------------------------------------------


def diff_stage(
    scores: list[Scores],
    right_pages: list[Scores],
    recall: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """The diff exam, per run and side: red precision, please-check precision, and recall against
    the miss list (found, partly, missed, unscored), on every audited answer and again on the
    pages paired right only (`right_pages`, `score_cases` with `right_paired_pages`). The miss
    list's recall is the run's whatever the side: the stored and today groups share one set of
    answers."""
    on_right = {(score.run, score.side): score for score in right_pages}
    out: dict[str, dict[str, Any]] = {}
    for score in scores:
        clean = on_right.get((score.run, score.side))
        misses = recall.get(score.run, {})
        hand = misses.get("hand", {})
        out.setdefault(score.run, {})[score.side] = {
            "reds": score.reds,
            "reds_true": score.reds_true,
            "red_precision": score.red_precision,
            "checks": score.checks,
            "checks_true": score.checks_true,
            "check_precision": score.check_precision,
            "right_pages": (
                None
                if clean is None
                else {
                    "reds": clean.reds,
                    "reds_true": clean.reds_true,
                    "red_precision": clean.red_precision,
                    "checks": clean.checks,
                    "checks_true": clean.checks_true,
                    "check_precision": clean.check_precision,
                }
            ),
            "misses": {
                "known": misses.get("known_problems", 0),
                "found": hand.get("found", 0),
                "partly": hand.get("partly", 0),
                "missed": hand.get("missed", 0),
                "unscored": hand.get("unscored", 0),
                "recall": misses.get("recall"),
                "recall_with_partial": misses.get("recall_with_partial"),
            },
        }
    return out


def _searchable(finding: dict[str, Any]) -> str:
    """One lower-cased string of everything in a finding the miss matcher may look at."""
    parts = [str(finding.get(key) or "") for key in _SEARCHED]
    card = finding.get("card")
    if isinstance(card, dict):
        question = card.get("question")
        if isinstance(question, dict):
            parts.append(str(question.get("key") or ""))
    return " ".join(parts).lower()


def auto_status(miss: Miss, findings: list[dict[str, Any]]) -> tuple[str, str]:
    """Whether a run's answers name this miss, and which answer the matcher leant on.

    Deliberately mean: an answer must sit on one of the miss's pages and must carry at least two of
    the miss's cues (one, when the miss has only one cue). It is a hint for a person, never a
    verdict: a run can name a problem in words this does not hold, and can carry the words while
    naming something else. `score_misses` reports it beside the helper's own status, and counts
    the disagreements rather than resolving them.
    """
    stuck_report = import_module("app.review.assistant.stuck_report")
    needed = min(CUES_NEEDED, len(miss.cues)) or 1
    for finding in findings:
        if stuck_report._page_number(finding.get("page")) not in miss.pages:
            continue
        text = _searchable(finding)
        hits = sum(1 for cue in miss.cues if cue.lower() in text)
        if hits >= needed:
            return "found", str(finding.get("id"))
    return "missed", ""


def score_misses(
    misses: list[Miss],
    status: list[MissStatus],
    findings: dict[str, list[dict[str, Any]]],
    jobs: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Recall per run, from the hand-scored statuses, with the automatic matcher beside it.

    `jobs` maps a run tag to its job, so a run is only scored against its own job's misses.
    """
    hand: dict[str, dict[str, str]] = {}
    for row in status:
        hand.setdefault(row.run, {})[row.miss_id] = row.status
    out: dict[str, dict[str, Any]] = {}
    for run, job in sorted(jobs.items()):
        mine = [miss for miss in misses if miss.job == job]
        scored = hand.get(run, {})
        counts = Counter(scored.get(miss.miss_id, "unscored") for miss in mine)
        disagree: list[str] = []
        auto = Counter()
        for miss in mine:
            guess, _ = auto_status(miss, findings.get(run, []))
            auto[guess] += 1
            seen = scored.get(miss.miss_id)
            if seen in {"found", "missed"} and seen != guess:
                disagree.append(f"{miss.miss_id}: hand says {seen}, matcher says {guess}")
        known = sum(counts[key] for key in ("found", "partly", "missed"))
        out[run] = {
            "job": job,
            "known_problems": len(mine),
            "scored": known,
            "unscored": counts["unscored"],
            "hand": dict(counts),
            "recall": (counts["found"] / known) if known else None,
            "recall_with_partial": (
                (counts["found"] + 0.5 * counts["partly"]) / known if known else None
            ),
            "auto": dict(auto),
            "disagreements": disagree,
        }
    return out


def stability(misses: list[Miss], status: list[MissStatus], jobs: dict[str, str]) -> dict[str, Any]:
    """For a job checked by more than one run, how many known problems one run found and another
    missed. A problem that appears and disappears between two runs of the same drawings is the
    clearest sign that the finding stage is not repeatable."""
    by_run: dict[str, dict[str, str]] = {}
    for row in status:
        by_run.setdefault(row.run, {})[row.miss_id] = row.status
    out: dict[str, Any] = {}
    by_job: dict[str, list[str]] = {}
    for run, job in jobs.items():
        by_job.setdefault(job, []).append(run)
    for job, runs in sorted(by_job.items()):
        if len(runs) < 2:
            continue
        runs = sorted(runs)
        swung = []
        for miss in (miss for miss in misses if miss.job == job):
            seen = {run: by_run.get(run, {}).get(miss.miss_id) for run in runs}
            values = {value for value in seen.values() if value}
            if "found" in values and values & {"missed"}:
                swung.append({"miss_id": miss.miss_id, **seen})
        out[job] = {"runs": runs, "swing": len(swung), "cases": swung}
    return out


def never_found_kinds(misses: list[Miss], status: list[MissStatus]) -> dict[str, int]:
    """The kinds of problem no audited run has ever raised, most common first. This is the list
    the next workstream has to answer for, because a kind the finding stage has never noticed once
    is not a tuning problem."""
    counts = Counter(miss.kind for miss in never_found(misses, status))
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def never_found(misses: list[Miss], status: list[MissStatus]) -> list[Miss]:
    """The known problems no audited run ever raised. These are what the next workstream has to
    answer for: a kind of problem the finding stage has never once noticed."""
    seen: dict[str, set[str]] = {}
    for row in status:
        seen.setdefault(row.miss_id, set()).add(row.status)
    return [miss for miss in misses if not (seen.get(miss.miss_id, set()) & {"found", "partly"})]


def table(scores: list[Scores]) -> list[str]:
    """The score as a person reads it: one line per run and side."""

    def pct(value: float | None) -> str:
        return "   -  " if value is None else f"{value * 100:5.1f}%"

    header = (
        f"{'run':<11} {'side':<7} {'cases':>5} {'listed':>6} {'precis':>6} "
        f"{'recall':>6} {'reds':>5} {'red ok':>6} {'leak!':>5} {'leak=':>5}"
    )
    lines = [header, "-" * 72]
    for score in scores:
        lines.append(
            f"{score.run:<11} {score.side:<7} {score.cases:>5} {score.listed:>6} "
            f"{pct(score.precision)} {pct(score.recall)} {score.reds:>5} "
            f"{pct(score.red_precision)} {score.leaks_problem:>5} {score.leaks_agreement:>5}"
        )
    return lines
