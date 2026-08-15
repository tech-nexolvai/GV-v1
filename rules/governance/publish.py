"""Publishing a rule: who may, who may not, and what is recorded when they do.

This is the last gate before a rule can decide anything about a real drawing. `AGENTS.md` §9
requires *"rule change → human approval + full gold-set regression"*, and until this module existed
neither was enforced: a rule could be authored and used with nobody approving it and nothing
checking it had not broken something.

**On the missing auth layer.** `C2.2` (#204) will supply an HTTP principal with roles, and it is
not built. Rather than wait, the roles are modelled here as a domain concept — who may approve a
rule is a property of the rulebook, not of the transport that carried the request. When #204 lands
it maps its principal onto `Approver`; nothing here changes.

**On the regression gate.** `D6.2` (#238) implements it and is not built either. `publish` takes it
as a **required argument with no default**. A default would be a hole: publication would silently
work without regression the moment someone forgot to pass one, and that is exactly the failure
`AGENTS.md` §9 names. Today a caller must pass something explicit and say what it means.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from rules.governance.proposal import RuleProposal
from rules.snapshot import RuleSnapshot, SnapshotStore
from rules.snapshot import publish as build_snapshot


class Role(StrEnum):
    """Who someone is, for the purposes of the rulebook.

    Deliberately small. This is not an authorisation system — it is the one distinction the
    rulebook cares about: publishing a rule changes what the system decides for every future
    drawing, and reviewing one does not.
    """

    REVIEWER = "reviewer"
    """May confirm evidence and approve a package. May **not** publish a rule."""

    RULE_ADMIN = "rule_admin"
    """May publish a rule snapshot."""


@dataclass(frozen=True, slots=True)
class Approver:
    """A named human with a role. Never a service, never a role alone.

    `AGENTS.md` §2.6 requires a human approval; an approval attributed to "the system" or to a role
    with no person behind it is the thing that requirement exists to prevent.
    """

    name: str
    role: Role

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError(
                "an approver must be a named person. 'Approved by rule_admin' answers a different "
                "question from 'approved by whom', and only the second is defensible later."
            )


class NotAuthorised(Exception):
    """The approver does not hold the role publication requires."""


class SelfApproval(Exception):
    """The approver raised the proposal.

    Separate from `NotAuthorised` because the fix is different: one needs a different person, the
    other needs different permissions, and conflating them sends someone to the wrong place.
    """


class NotApprovable(Exception):
    """The proposal did not validate. Authority cannot make an incoherent rule coherent."""


class RegressionFailed(Exception):
    """The gold-set regression did not pass. There is deliberately no override."""


@dataclass(frozen=True, slots=True)
class RegressionOutcome:
    """What a regression run concluded, and why.

    `passed=False` blocks publication outright. `D6.2` (#238) produces this from a real gold-set
    comparison; until then a caller must construct one explicitly and say what it is based on.
    """

    passed: bool
    summary: str

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError(
                "a regression outcome must say what it was based on. A bare pass with no basis is "
                "indistinguishable from no regression run at all."
            )


#: Supplied by the caller. `D6.2` will provide the real one.
RegressionCheck = Callable[[RuleProposal], RegressionOutcome]


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """The immutable record that a named person approved a specific snapshot.

    Carries the snapshot id rather than the rule id: the question asked later is *"who approved
    the thing that produced this finding?"*, and a finding records a snapshot.
    """

    snapshot_id: str
    rule_id: str
    version: str
    approver: str
    author: str
    rationale: str
    regression_summary: str
    approved_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __str__(self) -> str:
        return (
            f"{self.rule_id} {self.version} ({self.snapshot_id[:12]}) — proposed by {self.author}, "
            f"approved by {self.approver}"
        )


class PublicationLog:
    """Append-only record of every publication.

    Append-only for the same reason the correction ledger is: the record of who approved what is
    exactly what someone would be tempted to tidy after a rule turns out to have been wrong.
    """

    def __init__(self) -> None:
        self._records: dict[str, ApprovalRecord] = {}

    def record(self, approval: ApprovalRecord) -> None:
        if approval.snapshot_id in self._records:
            raise ValueError(
                f"snapshot {approval.snapshot_id[:12]} is already recorded as approved by "
                f"{self._records[approval.snapshot_id].approver}. An approval is never overwritten."
            )
        self._records[approval.snapshot_id] = approval

    def for_snapshot(self, snapshot_id: str) -> ApprovalRecord | None:
        return self._records.get(snapshot_id)

    def __len__(self) -> int:
        return len(self._records)

    def all(self) -> tuple[ApprovalRecord, ...]:
        return tuple(sorted(self._records.values(), key=lambda r: r.approved_at))


def publish(
    proposal: RuleProposal,
    *,
    approver: Approver,
    store: SnapshotStore,
    log: PublicationLog,
    regression: RegressionCheck,
) -> RuleSnapshot:
    """Publish a proposal, or refuse and say which gate stopped it.

    The order matters. Cheap refusals come first so an author is not told "regression failed" when
    the real problem is that they are not allowed to publish at all.

    `regression` has no default on purpose — see the module docstring.
    """
    if not proposal.approvable:
        raise NotApprovable(
            f"proposal for {proposal.rule_id} did not validate: {proposal.validation}. "
            "Authority decides whether a coherent change ships, not whether an incoherent one is "
            "coherent."
        )

    if approver.role is not Role.RULE_ADMIN:
        raise NotAuthorised(
            f"{approver.name} holds {approver.role.value} and cannot publish a rule. Publishing "
            "changes what the system decides for every future drawing; reviewing a package does not."
        )

    if approver.name.strip().casefold() == proposal.author.strip().casefold():
        raise SelfApproval(
            f"{approver.name} raised this proposal and cannot also approve it. The value of the "
            "approval is that a second person looked."
        )

    outcome = regression(proposal)
    if not outcome.passed:
        raise RegressionFailed(
            f"gold-set regression blocked publication of {proposal.rule_id}: {outcome.summary}. "
            "There is no override — a rule that regresses critical false-PASS does not ship."
        )

    snapshot = build_snapshot(proposal.proposed)
    approval = ApprovalRecord(
        snapshot_id=snapshot.snapshot_id,
        rule_id=snapshot.rule_id,
        version=snapshot.version,
        approver=approver.name,
        author=proposal.author,
        rationale=proposal.rationale,
        regression_summary=outcome.summary,
    )

    # Atomicity, without a transaction. `SnapshotStore` is append-only by design (§2.7) and has no
    # remove, so a failed second write cannot be rolled back. Two things follow.
    #
    # First, check both preconditions before either write, so neither is expected to fail.
    if log.for_snapshot(snapshot.snapshot_id) is not None:
        raise ValueError(
            f"snapshot {snapshot.short_id} is already recorded as approved. Publishing identical "
            "rule content twice would produce a second approval for the same bytes."
        )

    # Second, if one does fail anyway, the ordering decides which orphan we are left with. The
    # approval goes first: an approval naming a snapshot that was never stored is inert and
    # detectable, whereas a stored snapshot with no approval is returned by `latest()` and used by
    # the engine while looking legitimately published.
    log.record(approval)
    store.add(snapshot)

    return snapshot
