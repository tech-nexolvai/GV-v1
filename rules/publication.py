"""What may reach production, and what is still waiting on the client.

A rule carrying `TOLERANCE_UNCONFIRMED` is publishable for development: it can be authored,
reviewed and tested, and the engine will only ever return REVIEW REQUIRED from it. What it must
never do is reach production, where it would look like a working check.

**Why the report matters as much as the gate.** No tolerance value exists anywhere in the client
material. The `±1/8″` figure that circulated for weeks turned out to be our own placeholder from
a sample file, carrying the note *"PLACEHOLDER — please confirm your acceptable deviation"* — and
it reached `docs/RULE_ENGINE_SPEC.md` §4 and began reading as fact.

That is what an unconfirmed value does when nothing counts them: it stops looking provisional. A
rulebook can be authored, reviewed and appear finished while every number in it is a guess nobody
agreed to. So a sentinel alone is not enough; something has to be able to answer *"which rules
are still waiting on a real number, and how many?"*
"""

from __future__ import annotations

from dataclasses import dataclass

from rules.schema import Applicability, Rule, Tolerance
from rules.snapshot import RuleSnapshot, SnapshotStore


class NotProductionReadyError(Exception):
    """Raised when a rule that cannot decide anything was about to be released.

    Deliberately an error rather than a warning. A rule with an unconfirmed tolerance produces
    REVIEW REQUIRED for every drawing it sees, which in a report reads as *"the reviewer needs
    to look at this"* rather than *"nobody has told us what this check's limit is"*. Those are
    very different messages, and only one of them gets acted on.
    """


def tolerances_of(rule: Rule) -> tuple[Tolerance, ...]:
    """Every tolerance a rule carries, across its variants and its operation."""
    found: list[Tolerance] = []
    if isinstance(rule.applicability, Applicability):
        found.extend(v.tolerance for v in rule.applicability.variants if v.tolerance is not None)
    if rule.operation.tolerance is not None:
        found.append(rule.operation.tolerance)
    return tuple(found)


def unconfirmed_tolerance_count(rule: Rule) -> int:
    """How many of this rule's tolerances are still placeholders."""
    return sum(1 for t in tolerances_of(rule) if not t.is_confirmed)


def is_production_ready(rule: Rule) -> bool:
    """True when every tolerance this rule carries is a real client-supplied value.

    A rule with **no** tolerance at all is ready: `exists` and `equals` need none. Only a rule
    that declares one and has not had it confirmed is held back.
    """
    return unconfirmed_tolerance_count(rule) == 0


def assert_production_ready(rule: Rule) -> None:
    """Raise unless this rule can actually decide something.

    Called at the release boundary rather than at publish, because authoring and reviewing a
    rule with a placeholder is exactly what the sentinel is for. It is releasing one that is
    the mistake.
    """
    missing = unconfirmed_tolerance_count(rule)
    if missing:
        raise NotProductionReadyError(
            f"rule {rule.id!r} has {missing} unconfirmed tolerance"
            f"{'s' if missing > 1 else ''} and cannot decide anything. It would return REVIEW "
            "REQUIRED for every drawing, which reads as 'a reviewer should look at this' rather "
            "than 'nobody has told us the limit for this check'. Obtain the value before "
            "release, or hold the rule back."
        )


@dataclass(frozen=True, slots=True)
class AwaitingTolerance:
    """One rule that cannot be released, and how much is missing."""

    rule_id: str
    version: str
    snapshot_id: str
    unconfirmed: int

    def __str__(self) -> str:
        return (
            f"{self.rule_id} {self.version} — {self.unconfirmed} tolerance"
            f"{'s' if self.unconfirmed > 1 else ''} awaiting a client value"
        )


def awaiting_tolerance(store: SnapshotStore) -> tuple[AwaitingTolerance, ...]:
    """Every published rule still waiting on a real tolerance, newest version per rule.

    Reports the **effective** snapshot for each rule id rather than every historical one: the
    question being asked is *"what would block a release today?"*, and an old superseded
    snapshot does not.
    """
    found: list[AwaitingTolerance] = []
    for rule_id in store.rule_ids():
        snapshot: RuleSnapshot | None = store.latest(rule_id)
        if snapshot is None:
            continue
        missing = unconfirmed_tolerance_count(snapshot.rule)
        if missing:
            found.append(
                AwaitingTolerance(
                    rule_id=snapshot.rule_id,
                    version=snapshot.version,
                    snapshot_id=snapshot.snapshot_id,
                    unconfirmed=missing,
                )
            )
    return tuple(sorted(found, key=lambda a: a.rule_id))


def tolerance_report(store: SnapshotStore) -> str:
    """A plain-English summary for a status update or a client conversation.

    Written to be pasted into an email without editing: the point of the report is that somebody
    outside the codebase can see how much of the rulebook is still guesswork.
    """
    waiting = awaiting_tolerance(store)
    total = len(store.rule_ids())
    if not waiting:
        return f"All {total} published rule(s) have client-confirmed tolerances."

    headline = (
        f"{len(waiting)} of {total} published rule(s) cannot be released: "
        "the client has not supplied a tolerance for them."
    )
    lines = [headline, ""]
    lines.extend(f"  - {a}" for a in waiting)
    lines.append("")
    lines.append(
        "These rules can be authored and reviewed, and the engine returns REVIEW REQUIRED "
        "for each. None of them can produce a PASS or a FAIL until the value arrives."
    )
    return "\n".join(lines)
