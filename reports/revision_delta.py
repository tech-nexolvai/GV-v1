"""What changed between two revisions of a package (#186, B11.4).

A re-submission is normally a small change to a large package. Reviewing it from scratch wastes the
reviewer's time and hides what actually moved, so this answers three questions: what got fixed, what
broke, and what is the same as last time.

**The interesting question is not "what changed" but "why".** A check that passed last month and fails
now has two very different explanations: the drawing changed, or the rule did. Those call for opposite
responses — one is a vendor conversation, the other is our own regression — and a delta that reported only
the flip would leave the reviewer to guess. So every change carries the rule snapshot **and** the
governing revision from both sides, and names which of them moved.

That gives a fourth case the issue does not ask for and which is worth surfacing anyway: an outcome that
flipped while *neither* the drawing nor the rule changed. That should be impossible — the engine is
deterministic — so it is reported as `unexplained` rather than folded in with the rest. A delta that
quietly listed it as "newly failing" would hide the only evidence that determinism had broken.

**A delta is refused, not approximated, when the two sides are not comparable.** Comparing package
revisions with different sheet sets produces a list of "new" findings that are new only because the sheet
is new, which reads as a package that got worse. `NotComparable` says what differs instead.

**Keyed by item and check, never by position.** `Finding` carries `rule_id` but no item — the drawing
model that will supply one is B7 (#164, #166) and is not built. So the caller supplies the key explicitly.
That is deliberate rather than a gap: a module that derived keys from list position would compare the
third finding of one revision with the third of another, which is the specific mistake the criterion
names, and it would do it silently.

Source: backend proposal §11 · Design: `docs/DESIGN_EXTRACTION.md` §7 ·
Verification: `tests/reports/test_revision_delta.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from verdict.finding import Finding
from verdict.outcomes import Outcome

__all__ = [
    "Change",
    "ChangeCause",
    "CheckKey",
    "KeyedFinding",
    "NotComparable",
    "RevisionDelta",
    "RevisionSide",
    "compare_revisions",
]

#: Why the two sides could not be compared. Constants rather than prose, so a report template can branch.
DIFFERENT_SHEET_SETS: Final = "different_sheet_sets"
SAME_REVISION: Final = "same_revision"
DUPLICATE_KEY: Final = "duplicate_key"


class ChangeCause(StrEnum):
    """What moved underneath a changed outcome.

    The distinction the whole module exists for: `DRAWING` is a conversation with the vendor, `RULE` is a
    change we made, `BOTH` needs untangling, and `UNEXPLAINED` should not be possible.
    """

    DRAWING = "DRAWING"
    RULE = "RULE"
    BOTH = "BOTH"
    UNEXPLAINED = "UNEXPLAINED"


@dataclass(frozen=True, slots=True)
class CheckKey:
    """What makes two findings the same check across revisions.

    `item` is the thing checked — a countertop, a cabinet run — and comes from the caller because
    `Finding` does not carry it yet (B7). `variant` is part of the identity, not a detail: the same rule
    applied under `back_left_right` and under `island` are different checks and pairing them would compare
    two unrelated numbers.
    """

    item: str
    rule_id: str
    variant: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("item", self.item), ("rule_id", self.rule_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"{name} must identify something; a blank key pairs unrelated checks"
                )


@dataclass(frozen=True, slots=True)
class KeyedFinding:
    """One finding with the key that identifies it across revisions."""

    key: CheckKey
    finding: Finding


@dataclass(frozen=True, slots=True)
class RevisionSide:
    """One package revision: which revision it is, which sheets it holds, and what was found.

    `governing_revisions` maps sheet number to the revision that governed it — the output of B11.2
    (#184). It is what lets a change be attributed to the drawing rather than to the rule.
    """

    revision_number: int
    governing_revisions: dict[str, str]
    findings: tuple[KeyedFinding, ...]

    @property
    def sheet_numbers(self) -> frozenset[str]:
        return frozenset(self.governing_revisions)


@dataclass(frozen=True, slots=True)
class Change:
    """One check whose outcome moved, with both sides and what moved underneath it."""

    key: CheckKey
    before: Outcome
    after: Outcome

    snapshot_before: str
    snapshot_after: str
    revision_before: str | None
    revision_after: str | None
    cause: ChangeCause

    @property
    def snapshot_changed(self) -> bool:
        return self.snapshot_before != self.snapshot_after

    @property
    def revision_changed(self) -> bool:
        return self.revision_before != self.revision_after


@dataclass(frozen=True, slots=True)
class RevisionDelta:
    """What moved between two revisions of one package.

    `appeared` and `disappeared` exist because a check present on only one side is not "changed" and is
    certainly not "unchanged" — and silently dropping it would lose a finding from the report. A check
    that disappeared may be a resolved problem or a sheet that stopped being read, and those are
    different.
    """

    resolved: tuple[Change, ...] = ()
    newly_failing: tuple[Change, ...] = ()
    other_changes: tuple[Change, ...] = ()
    unchanged: tuple[CheckKey, ...] = ()
    appeared: tuple[KeyedFinding, ...] = ()
    disappeared: tuple[KeyedFinding, ...] = ()

    @property
    def is_comparable(self) -> bool:
        return True

    @property
    def unexplained(self) -> tuple[Change, ...]:
        """Outcomes that moved with neither the drawing nor the rule changing.

        Surfaced as its own view rather than a bucket, because a reader scanning for regressions should
        not have to notice it — and because it is the only signal that the engine stopped being
        deterministic.
        """
        return tuple(
            change
            for change in (*self.resolved, *self.newly_failing, *self.other_changes)
            if change.cause is ChangeCause.UNEXPLAINED
        )


@dataclass(frozen=True, slots=True)
class NotComparable:
    """The two sides are not revisions of one sheet set, so no delta is presented.

    Refusing rather than approximating: a delta across different sheet sets lists findings as "new" when
    the sheet is new, which reads as a package that got worse.
    """

    cause: str
    detail: str

    @property
    def is_comparable(self) -> bool:
        return False


def _cause(snapshot_changed: bool, revision_changed: bool) -> ChangeCause:
    """Which explanation fits: the drawing, the rule, both, or neither."""
    if snapshot_changed and revision_changed:
        return ChangeCause.BOTH
    if snapshot_changed:
        return ChangeCause.RULE
    if revision_changed:
        return ChangeCause.DRAWING
    return ChangeCause.UNEXPLAINED


def _by_key(side: RevisionSide) -> dict[CheckKey, KeyedFinding] | str:
    """Index one side by key, or name the duplicate that makes it un-indexable."""
    indexed: dict[CheckKey, KeyedFinding] = {}
    for entry in side.findings:
        if entry.key in indexed:
            return (
                f"revision {side.revision_number} has two findings for item "
                f"{entry.key.item!r}, rule {entry.key.rule_id!r}, variant {entry.key.variant!r}. "
                "One of them would silently win the comparison."
            )
        indexed[entry.key] = entry
    return indexed


def compare_revisions(before: RevisionSide, after: RevisionSide) -> RevisionDelta | NotComparable:
    """What changed from `before` to `after`, or why the two cannot be compared.

    Refuses when:

    * **The sheet sets differ.** Then the two are not revisions of one sheet set, and every finding on an
      added sheet would be reported as new.
    * **The revision numbers are equal.** Comparing a revision with itself produces an all-unchanged
      delta that looks like a reviewed re-submission. Nothing was submitted.
    * **Either side has two findings under one key.** One would silently win.

    Everything else is bucketed. `resolved` is a FAIL that stopped being one; `newly_failing` is a FAIL
    that was not one before. Both are defined against `FAIL` specifically rather than against "not PASS",
    because an abstention becoming a FAIL is a new problem while a FAIL becoming an abstention is not a
    fix — it is the system declining to say, and it lands in `other_changes` where a reviewer will see it
    rather than in `resolved` where they might not.
    """
    if before.sheet_numbers != after.sheet_numbers:
        only_before = sorted(before.sheet_numbers - after.sheet_numbers)
        only_after = sorted(after.sheet_numbers - before.sheet_numbers)
        return NotComparable(
            DIFFERENT_SHEET_SETS,
            f"revision {before.revision_number} and revision {after.revision_number} do not hold the "
            f"same sheets. Only in {before.revision_number}: {only_before or 'none'}. Only in "
            f"{after.revision_number}: {only_after or 'none'}. A delta across different sheet sets "
            "reports findings as new when the sheet is new.",
        )

    if before.revision_number == after.revision_number:
        return NotComparable(
            SAME_REVISION,
            f"both sides are revision {before.revision_number}. Comparing a revision with itself "
            "produces an all-unchanged delta that reads like a reviewed re-submission.",
        )

    for side in (before, after):
        indexed = _by_key(side)
        if isinstance(indexed, str):
            return NotComparable(DUPLICATE_KEY, indexed)

    old = _by_key(before)
    new = _by_key(after)
    assert isinstance(old, dict) and isinstance(new, dict)  # established above

    resolved: list[Change] = []
    newly_failing: list[Change] = []
    other: list[Change] = []
    unchanged: list[CheckKey] = []

    for key in sorted(set(old) & set(new), key=lambda k: (k.item, k.rule_id, k.variant or "")):
        was, is_now = old[key].finding, new[key].finding
        if was.outcome is is_now.outcome:
            unchanged.append(key)
            continue

        sheet_before = _sheet_revision(before, was)
        sheet_after = _sheet_revision(after, is_now)
        change = Change(
            key=key,
            before=was.outcome,
            after=is_now.outcome,
            snapshot_before=was.snapshot_id,
            snapshot_after=is_now.snapshot_id,
            revision_before=sheet_before,
            revision_after=sheet_after,
            cause=_cause(was.snapshot_id != is_now.snapshot_id, sheet_before != sheet_after),
        )

        # **`resolved` requires a PASS, not merely the absence of a FAIL.** The docstring above said so
        # and the first version of this branch did not: it put every FAIL that stopped being a FAIL into
        # `resolved`, including one that became REVIEW_REQUIRED. A reviewer scanning the fixed list would
        # have read "we stopped being able to judge it" as "it went away". Caught by the test, which is
        # the only reason the sentence and the code now agree.
        if was.outcome is Outcome.FAIL and is_now.outcome is Outcome.PASS:
            resolved.append(change)
        elif is_now.outcome is Outcome.FAIL:
            newly_failing.append(change)
        else:
            other.append(change)

    return RevisionDelta(
        resolved=tuple(resolved),
        newly_failing=tuple(newly_failing),
        other_changes=tuple(other),
        unchanged=tuple(unchanged),
        appeared=tuple(new[key] for key in sorted(set(new) - set(old), key=_ordering)),
        disappeared=tuple(old[key] for key in sorted(set(old) - set(new), key=_ordering)),
    )


def _ordering(key: CheckKey) -> tuple[str, str, str]:
    """Deterministic order, so two runs over the same data produce the same report."""
    return (key.item, key.rule_id, key.variant or "")


def _sheet_revision(side: RevisionSide, finding: Finding) -> str | None:
    """The governing revision behind a finding, or `None` when this side names exactly one sheet set.

    **A deliberate simplification, stated rather than hidden.** `Finding` does not record which sheet it
    was drawn from — `evidence_refs` hold document and page references, and mapping those to a sheet
    number needs B6's manifest, which this module does not take. So when a side governs exactly one
    sheet, that revision is used; otherwise the answer is `None` for both sides and a changed outcome
    attributes to the rule or to nothing.

    The consequence is honest but limited: on a multi-sheet package this cannot yet say *the drawing
    changed*. It never says the wrong thing — `None == None` means "no revision change observed", so the
    cause falls to `RULE` or `UNEXPLAINED`, both of which send a reviewer looking rather than reassuring
    them. Wiring per-finding sheet attribution needs the finding to carry its sheet, which is B7's.
    """
    del finding
    if len(side.governing_revisions) == 1:
        return next(iter(side.governing_revisions.values()))
    return None
