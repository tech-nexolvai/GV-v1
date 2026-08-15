"""How much of the rulebook is still guesswork, in a form somebody can act on.

`rules/publication.py` can already answer this per rule. What it could not do is answer it for the
rulebook as a whole, which is the question that actually gets asked — by a client wondering when
the system will start deciding things, and by us deciding whether a release is worth cutting.

**Why this exists as its own module rather than a line in a report.** The `±1/8″` tolerance that
circulated for weeks was our own placeholder from `Countertop_Checks_SAMPLE_Nexolv.xlsx`, carrying
the note *"PLACEHOLDER — please confirm your acceptable deviation"*. It reached
`docs/RULE_ENGINE_SPEC.md` §4 and began reading as fact, because nothing counted placeholders and
so nothing contradicted the impression that the rulebook was finished.

A sentinel alone does not prevent that. Something has to be able to say *"four of six rules are
still waiting on a number"* without being asked.

`C2.4` (#206) will expose this over HTTP. The shape is domain-level here for the same reason roles
are: how ready the rulebook is does not depend on the transport that asked.
"""

from __future__ import annotations

from dataclasses import dataclass

from rules.publication import awaiting_tolerance, tolerance_report
from rules.snapshot import SnapshotStore


@dataclass(frozen=True, slots=True)
class Readiness:
    """What fraction of the rulebook could actually decide something today."""

    total_rules: int
    releasable: int
    awaiting_tolerance: tuple[str, ...]

    @property
    def blocked(self) -> int:
        return len(self.awaiting_tolerance)

    @property
    def can_release_anything(self) -> bool:
        """False when no rule could produce a PASS or a FAIL.

        Distinguished from "some rules are blocked" because the two are different conversations:
        one is a gap, the other is that the product does not work yet.
        """
        return self.releasable > 0

    def __str__(self) -> str:
        if self.total_rules == 0:
            return "The rulebook is empty. Nothing can be checked."
        if not self.awaiting_tolerance:
            return f"All {self.total_rules} rule(s) have client-confirmed tolerances."
        return (
            f"{self.releasable} of {self.total_rules} rule(s) are releasable. "
            f"{self.blocked} still await a client tolerance: "
            f"{', '.join(self.awaiting_tolerance)}."
        )


def assess(store: SnapshotStore) -> Readiness:
    """Measure the rulebook, counting the effective version of each rule.

    The effective version rather than every historical snapshot: the question is *"what would block
    a release today?"*, and a superseded snapshot does not.
    """
    waiting = awaiting_tolerance(store)
    total = len(store.rule_ids())
    return Readiness(
        total_rules=total,
        releasable=total - len(waiting),
        awaiting_tolerance=tuple(a.rule_id for a in waiting),
    )


def report(store: SnapshotStore) -> str:
    """A plain-English summary, written to be pasted into a client email unedited.

    Delegates the per-rule detail to `rules/publication.py` rather than re-rendering it, so the two
    cannot drift into saying different things about the same rulebook.
    """
    readiness = assess(store)
    lines = [str(readiness), ""]
    lines.append(tolerance_report(store))
    if not readiness.can_release_anything and readiness.total_rules:
        lines.extend(
            [
                "",
                (
                    "No rule in the rulebook can currently produce a PASS or a FAIL. Every check "
                    "would return REVIEW REQUIRED, which reads to a reviewer as 'look at this' "
                    "rather than 'nobody has told us the limit'."
                ),
            ]
        )
    return "\n".join(lines)
