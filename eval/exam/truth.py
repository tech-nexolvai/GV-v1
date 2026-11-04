"""What a hand audit's verdict means for a score, and how the exam's data files are read (harness rebuild, Phase 0).

The exam is the $0 gate the rebuild runs before it spends money (Anant, 2026-11-04). It holds three
kinds of row, each in its own file beside this module:

- **`cases.tsv`** — one per audited answer: the run, the finding id, the vendor page, the group the
  run put it in, and the helper's verdict. `truth_of` turns the verdict into the two things a score
  needs: `truth_group` (where the answer belonged) and `listed` (whether a person should have seen
  it at all). An answer the run listed that should not have been listed is a false alarm; one it
  filed under Not compared that should have been listed is a **leak**, which is the failure the
  flag-mode rule exists to stop.
- **`pairing.tsv`** — one per vendor page the helpers checked: which architect page the run used and
  which one was right. Pairing is upstream of everything: a page sent to the wrong architect sheet
  invents differences that no later stage can tell from real ones.
- **`misses.tsv` / `miss_status.tsv`** — the real problems on the drawings that a run never raised,
  and each run's status against them (found / partly / missed). Precision alone cannot see these.
  Every finding in this product is written by an AI and code only does arithmetic on what the AI
  already wrote down, so a problem the AI never notices is invisible to every later stage and to
  every precision score ever taken. These rows are the only place the exam can see it.

**The miss list is a floor, not a census.** The helpers read the pages they were given, not all 52,
so the true recall is lower than any number computed here. `eval/exam/README.md` says what was
sampled.

Nothing here reads a drawing, a database or the network.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "CASES",
    "DIR",
    "LISTED_GROUPS",
    "MISSES",
    "MISS_STATUS",
    "PAIRING",
    "RUNS",
    "Case",
    "Miss",
    "MissStatus",
    "Pairing",
    "Truth",
    "load_cases",
    "load_miss_status",
    "load_misses",
    "load_pairing",
    "load_runs",
    "truth_of",
]

DIR: Final = Path(__file__).resolve().parent
CASES: Final = DIR / "cases.tsv"
PAIRING: Final = DIR / "pairing.tsv"
MISSES: Final = DIR / "misses.tsv"
MISS_STATUS: Final = DIR / "miss_status.tsv"
RUNS: Final = DIR / "runs.json"

#: The groups a person actually reads as a result. `right` is kept in the data and never listed,
#: and `not_compared` goes on its own separate list, not the problems list.
LISTED_GROUPS: Final = frozenset({"wrong", "likely_problem"})


@dataclass(frozen=True)
class Truth:
    """What the audit's verdict says should have happened to an answer."""

    #: Where the answer belonged: a group name, `not_listed` (raised but there is nothing to
    #: raise, and the audit did not say whether it is Right or nothing-to-compare), or `unknown`.
    group: str
    #: Whether a person should have seen it on the problems list. None when the audit could not say.
    listed: bool | None
    #: True when the audit says the run hid a real problem (a leak) rather than over-raising.
    leak: bool = False


def truth_of(verdict: str, stored_group: str) -> Truth:
    """`Truth` for one audited answer. The verdict vocabulary is `audit_parse.VERDICTS`.

    A flagged answer (red or please-check) judged TRUE or TRUE-minor belongs where it is, so the
    truth keeps the group the run used; judged FALSE it should not have been raised at all. A
    Not-compared answer carries its own vocabulary: (a) rightly nothing, (b) really a difference,
    (c) really an agreement — (b) and (c) are both leaks, one of a problem and one of a Right.
    Where a helper judged a Not-compared answer TRUE or FALSE instead of (a)/(b)/(c), TRUE means
    the point is real and so belongs on the problems list, and FALSE leaves it where it is.
    """
    if verdict in {"TRUE", "TRUE_MINOR"}:
        if stored_group in LISTED_GROUPS:
            return Truth(stored_group, True)
        return Truth("likely_problem", True, leak=True)
    if verdict == "FALSE":
        if stored_group in LISTED_GROUPS:
            return Truth("not_listed", False)
        return Truth(stored_group, False)
    if verdict == "NC_A":
        return Truth("not_compared", False)
    if verdict == "NC_B":
        return Truth("likely_problem", True, leak=stored_group not in LISTED_GROUPS)
    if verdict == "NC_C":
        return Truth("right", False, leak=stored_group not in LISTED_GROUPS)
    if verdict == "RIGHT_OK":
        return Truth("right", False)
    if verdict == "RIGHT_BAD":
        return Truth("likely_problem", True, leak=True)
    return Truth("unknown", None)


@dataclass(frozen=True)
class Case:
    """One audited answer, as `cases.tsv` holds it."""

    run: str
    finding_id: str
    page: int
    by: str
    category: str
    label: str
    stored_group: str
    verdict: str
    audit: str

    @property
    def truth(self) -> Truth:
        return truth_of(self.verdict, self.stored_group)


@dataclass(frozen=True)
class Pairing:
    """One vendor page's pairing truth, as `pairing.tsv` holds it."""

    run: str
    vendor_page: int
    run_architect_page: str
    right_architect_page: str
    right_view: str
    correct: str
    audit: str


@dataclass(frozen=True)
class Miss:
    """One real problem the drawings hold, whether or not any run raised it."""

    miss_id: str
    job: str
    pages: tuple[int, ...]
    kind: str
    severity: str
    description: str
    where: str
    cues: tuple[str, ...]
    source: str


@dataclass(frozen=True)
class MissStatus:
    """What one run did about one miss, as the helpers scored it by hand."""

    miss_id: str
    run: str
    status: str
    note: str


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        return [
            row
            for row in csv.DictReader(
                (line for line in handle if not line.startswith("#")), delimiter="\t"
            )
        ]


def load_cases(path: Path = CASES) -> list[Case]:
    """Every exam case, in file order."""
    return [
        Case(
            run=row["run"],
            finding_id=row["finding_id"],
            page=int(row["page"]),
            by=row["by"],
            category=row["category"],
            label=row["label"],
            stored_group=row["stored_group"],
            verdict=row["verdict"],
            audit=row["audit"],
        )
        for row in _rows(path)
    ]


def load_pairing(path: Path = PAIRING) -> list[Pairing]:
    """Every labelled vendor page, in file order."""
    return [
        Pairing(
            run=row["run"],
            vendor_page=int(row["vendor_page"]),
            run_architect_page=row["run_architect_page"],
            right_architect_page=row["right_architect_page"],
            right_view=row["right_view"],
            correct=row["correct"],
            audit=row["audit"],
        )
        for row in _rows(path)
    ]


def load_misses(path: Path = MISSES) -> list[Miss]:
    """Every known real problem, in file order."""
    return [
        Miss(
            miss_id=row["miss_id"],
            job=row["job"],
            pages=tuple(int(page) for page in row["pages"].split(",") if page.strip()),
            kind=row["kind"],
            severity=row["severity"],
            description=row["description"],
            where=row["where"],
            cues=tuple(cue.strip() for cue in row["cues"].split("|") if cue.strip()),
            source=row["source"],
        )
        for row in _rows(path)
    ]


def load_miss_status(path: Path = MISS_STATUS) -> list[MissStatus]:
    """Each run's hand-scored status against each miss, in file order."""
    return [
        MissStatus(miss_id=row["miss_id"], run=row["run"], status=row["status"], note=row["note"])
        for row in _rows(path)
    ]


def load_runs(path: Path = RUNS) -> dict[str, dict[str, object]]:
    """The run registry: neutral tag to what the exam knows about that run. It holds no file
    paths, because a saved run is the client's drawing data; the replay runner is told where they
    are (`scripts/exam_replay.py --runs`)."""
    import json

    return dict(json.loads(path.read_text(encoding="utf-8"))["runs"])
