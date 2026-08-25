"""From a pattern of corrections to a *suggestion* that a human might act on. No further.

*"Corrections silently become rules"* is a named risk in the system design, and it is a plausible
one: the correction ledger is exactly the data you would mine to tune a tolerance, and tuning a
tolerance from the drawings it failed on is how a check comes to agree with whatever the vendor
happened to send.

The control is three things — an append-only ledger, a **human proposal gate**, and a full gold-set
regression before anything ships. This module is the middle one.

**What it deliberately is not.** It does not produce a `Rule`. It does not produce
`rules.governance.proposal.RuleProposal`, which carries a validated rule and is what D6's `publish`
consumes. A suggestion here is prose plus the corrections that motivated it: *"these fourteen
corrections all moved the same dimension the same way — is CT-WIDTH-001's tolerance wrong?"* Turning
that into an actual rule change is a human writing one, and it then goes through D6 like any other
change: authored, validated, approved by someone who did not raise it, and regressed against the
gold set.

There is no state in which a suggestion is *pending application*, and no field that could carry one.
A queue of nearly-approved rule changes is the thing that eventually gets drained by someone in a
hurry.

**Nothing imports a publisher.** `tests/review/test_proposal_gate.py` walks the import graph and
fails if a path appears from the ledger to anything that can publish a rule. A comment saying "do
not automate this" is not a control; the test is.

Source: `AGENTS.md` §2.6; system design §16; issue #235.
Verification: ``tests/review/test_proposal_gate.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class RuleChangeSuggestion:
    """A human's suggestion that a rule may be wrong, with the corrections that prompted it.

    Frozen, and there is nothing here that applies it. The fields are the evidence and the argument;
    what happens next is a person deciding whether to write a rule change, which is a separate act
    with its own gate.

    Deliberately absent: any `status`, any `approved` flag, any `auto_apply`, and any `Rule`. Each
    would turn a suggestion into a change waiting for a rubber stamp, and the distance between those
    two is the control.
    """

    raised_by: str
    """The person who raised it. Never a system actor — see `suggest`."""

    motivating_corrections: tuple[UUID, ...]
    """The correction-ledger entries that prompted this, so an approver can judge the evidence
    rather than the claim. A suggestion without them is an opinion."""

    suggestion: str
    """Plain English: what looks wrong, and why these corrections suggest it."""

    raised_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.raised_by.strip():
            raise ValueError(
                "a suggestion must name the person who raised it. The first question about any "
                "proposed rule change is who wanted it and why, and an unattributed suggestion "
                "cannot answer either."
            )
        if not self.suggestion.strip():
            raise ValueError(
                "a suggestion must say what looks wrong. A list of corrections with no argument "
                "attached leaves the reader to infer the claim, and they will infer the one they "
                "already believed."
            )
        if not self.motivating_corrections:
            raise ValueError(
                "a suggestion must name the corrections that motivated it. Without them there is "
                "nothing for an approver to check, and 'the ledger suggests' becomes an assertion "
                "nobody can test — which is exactly how a correction pattern turns into a rule "
                "change without anyone having examined it."
            )
        if len(set(self.motivating_corrections)) != len(self.motivating_corrections):
            raise ValueError(
                "the same correction is listed twice, which overstates the evidence. Fourteen "
                "distinct corrections and one correction listed fourteen times read identically "
                "in a summary and mean very different things."
            )


def suggest(
    *, raised_by: str, motivating_corrections: tuple[UUID, ...], suggestion: str
) -> RuleChangeSuggestion:
    """Raise a suggestion. A person calls this; nothing calls it on their behalf.

    There is no `from_ledger(...)` beside it, and that omission is the point. A function that read
    the ledger and produced a suggestion would be the automated path this module exists to prevent —
    the human step would become "review the generated list", which is a different and much weaker
    control than "notice a pattern and argue for a change".

    Reading the ledger to *find* patterns is fine and already exists: `app/review/ledger.py` queries
    it, and `reports/vendor_patterns.py` aggregates it. What must stay manual is the step from
    "here is a pattern" to "here is a change I am proposing".
    """
    return RuleChangeSuggestion(
        raised_by=raised_by,
        motivating_corrections=motivating_corrections,
        suggestion=suggestion,
    )
