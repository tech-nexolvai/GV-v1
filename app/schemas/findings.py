"""What a findings query returns, and the order it returns it in (#222, D1.1).

**The ordering is part of the contract, not an implementation detail.** A reviewer reads the top of
the list and stops when the day runs out, so what sorts first decides what gets looked at. It is
stated here, asserted by the tests, and repeated back in every response — `FindingPage.ordering` —
because an ordering a client has to infer is one they will infer wrongly.

**Abstentions are results.** `NOT_FOUND`, `REVIEW_REQUIRED` and `NO_APPLICABLE_RULE` are outcomes the
engine produced, not rows that are missing. They are in every unfiltered list, and they sort *above*
`PASS` within a severity: `docs/DESIGN_PRODUCT.md` §3.2 is explicit that silence reading as approval
is the failure the whole abstention design exists to prevent, and burying a `REVIEW REQUIRED` under
forty passes is that failure with extra steps.

**No calculation trace here.** A finding proving itself — snapshot, operands, trace, recompute — is
`docs/DESIGN_PRODUCT.md` §3.1's `FindingChain`, and it is a different (per-finding) request. A list
carrying every trace would be enormous and would still not be the recompute that §3.1 asks for, so
this returns the identity and enough version information to go and fetch one.

Source: backend proposal §10.2 Findings · Design: `docs/DESIGN_PRODUCT.md` §3.1 ·
Verification: `tests/api/test_findings_query.py`
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from verdict.outcomes import Outcome, Severity

#: Severities, worst first. The list a reviewer works down.
SEVERITY_ORDER: tuple[Severity, ...] = (
    Severity.CRITICAL,
    Severity.MAJOR,
    Severity.MINOR,
    Severity.ADVISORY,
)

#: Outcomes, most in need of attention first, applied *within* a severity.
#:
#: `FAIL` first is the acceptance criterion — "critical failures first". Severity alone does not
#: deliver it: a critical `PASS` and a critical `FAIL` tie on severity, and the tie-break would then
#: be whichever happened to be written first.
#:
#: The three abstentions come next and `PASS` comes last, which is the §3.2 rule applied to a list
#: rather than to a redline. A `REVIEW REQUIRED` sitting below the passes is a check nobody reads.
OUTCOME_ORDER: tuple[Outcome, ...] = (
    Outcome.FAIL,
    Outcome.REVIEW_REQUIRED,
    Outcome.NOT_FOUND,
    Outcome.NO_APPLICABLE_RULE,
    Outcome.PASS,
)

#: The ordering in plain English, returned with every page.
ORDERING_DESCRIPTION = (
    "Critical first, and within one severity the failures before the abstentions and the "
    "abstentions before the passes. Ties are broken by oldest first, then by id, so the order is "
    "total and a page boundary always falls in the same place. Full key: severity "
    "(CRITICAL, MAJOR, MINOR, ADVISORY), then outcome (FAIL, REVIEW_REQUIRED, NOT_FOUND, "
    "NO_APPLICABLE_RULE, PASS), then created_at ascending, then id ascending."
)


def validate_sort_orders(severities: tuple[Severity, ...], outcomes: tuple[Outcome, ...]) -> None:
    """Refuse a sort key that does not name every value it has to sort.

    A missing member does not raise anywhere — it sorts to a shared bucket at the end, and two
    findings that should have been separated by severity land in whatever order the database
    returned them. That is an unstable sort, which is precisely what breaks paging.

    A callable rather than inline code at import, so a test can watch it fail. A guard whose test
    cannot fail on a wrong answer proves nothing.
    """
    missing_severities = set(Severity) - set(severities)
    missing_outcomes = set(Outcome) - set(outcomes)
    if missing_severities or missing_outcomes:
        raise RuntimeError(
            "the findings sort key does not rank every value it has to sort: "
            f"severities {sorted(s.value for s in missing_severities)}, "
            f"outcomes {sorted(o.value for o in missing_outcomes)}. "
            "An unranked value shares a bucket with every other unranked value, so the sort is no "
            "longer total — and a paging scheme built on a non-total order silently skips and "
            "repeats rows."
        )
    if len(set(severities)) != len(severities) or len(set(outcomes)) != len(outcomes):
        raise RuntimeError("a value is ranked twice, so its position depends on which rank wins")


validate_sort_orders(SEVERITY_ORDER, OUTCOME_ORDER)


class FindingOut(BaseModel):
    """One finding, with enough version information to go and reconstruct it.

    Every version that could explain the result is here — the rule snapshot's content hash, the rule
    version, the engine build, the parameter sets in force. `AGENTS.md` §2.7: a finding that cannot
    be attributed to the versions that produced it is an assertion rather than a record.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    check_run_id: UUID
    package_revision_id: UUID
    revision_number: int
    """Which revision of the package this finding is about.

    Returned rather than assumed, because a package has several and a finding against a superseded
    revision may already have been fixed. A consumer that ignores this can report a stale failure.
    """

    outcome: Outcome
    severity: Severity

    rule_id: str
    """The authored identifier, e.g. `CT-WIDTH-001`."""

    rule_version: str
    rule_snapshot_id: UUID
    rule_snapshot_hash: str
    """The `sha256:…` content hash of the published rule. The rule *version* says which release;
    this says which exact bytes, and only the second survives a mistaken republish."""

    check_type: str
    product_type: str
    engine_version: str
    parameter_set_versions: dict[str, str]
    created_at: datetime


class FindingPage(BaseModel):
    """One page of findings, plus how to ask for the next one.

    `next_cursor` is `None` when this is the last page — and that is the only way to know. A page
    shorter than `limit` is *not* a reliable end-of-list signal in any cursor scheme, and treating it
    as one is how a consumer stops early and reports a package as clean.
    """

    items: list[FindingOut]
    next_cursor: str | None = None
    limit: int
    ordering: str = Field(default=ORDERING_DESCRIPTION)
