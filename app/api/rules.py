"""Reading the rulebook, and the one route that changes it (#206, C2.4).

Four things this module is built to get right.

**Publication goes through D6, not around it.** There is no publication logic here. The endpoint
resolves the caller to an approver, finds the proposal they named, and hands both to
`rules.governance.publish.publish()`. Every refusal — not authorised, self-approval, not approvable,
unconfirmed tolerance at the production boundary, regression failed — is D6's, and each one is
forwarded with its own reason and its own status code. There is deliberately no "already approved"
shortcut and no override: a gate with a bypass is not a gate, and the bypass is always added for a
reason that sounds good at the time.

**The API never receives rule content.** The request body is a content hash and a target, nothing
else. A body that carried the proposed rule would carry its tolerances, and the API would become a
way to put a number the client never confirmed into the thing that decides PASS or FAIL. It also
means the approver approves the exact bytes they read: the hash changes the moment the proposal
does, so an approval cannot silently follow an edit.

**Whether a rule can be released is reported, never re-decided.** `is_production_ready` and
`unconfirmed_tolerance_count` in `rules/publication.py` are the judgement (ADR-0011). This module
renders it — as a boolean, a count and a sentence — and that is all.

**The rulebook is a seam, and it fails closed.** `SnapshotStore` and `PublicationLog` are in-memory
and process-level; nothing in this repository persists them yet, and the `rule_snapshots` table has
no writer. The regression check that D6 requires (#238) has no default *on purpose* — publication
that silently works without a regression run is the exact failure `AGENTS.md` §9 names. So the
deployment wires all four parts onto `app.state.rulebook` and this module refuses when it has not,
in the same way `get_artifact_store` refuses rather than defaulting to a local directory. A default
here would be worse than a missing setting: it would be a rulebook that publishes into a dictionary
nobody reads, while every response says the publication succeeded.

**Scope.** These routes carry no `{project_id}` because rules are not project data. ADR-0006 settles
that rules are GV's own standards — `app/models/rules.py` has no project column on either table, and
the resolver never filters by project — so there is no project-scoped read to enforce. They are
guarded by role instead: any of the three defined roles may read the rulebook, and publishing needs
`Action.PUBLISH_RULE` from the permission table.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.1, §4.3 ·
Verification: `tests/api/test_rules_api.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import Action, Principal, Role, require_action, require_role
from app.schemas.rules import (
    ApplicabilityOut,
    ApplicabilityVariantOut,
    AwaitingToleranceOut,
    PublicationOut,
    PublicationRequest,
    RuleOut,
    RuleSnapshotOut,
    ToleranceReportOut,
)
from rules.governance import readiness
from rules.governance.proposal import RuleProposal
from rules.governance.publish import (
    Approver,
    NotApprovable,
    NotAuthorised,
    PublicationLog,
    RegressionCheck,
    RegressionFailed,
    SelfApproval,
)
from rules.governance.publish import Role as GovernanceRole
from rules.governance.publish import publish as publish_snapshot
from rules.publication import (
    NotProductionReadyError,
    awaiting_tolerance,
    is_production_ready,
    tolerance_report,
    unconfirmed_tolerance_count,
)
from rules.schema import Applicability, Rule, Tolerance
from rules.snapshot import (
    RuleSnapshot,
    SnapshotConflictError,
    SnapshotStore,
    VersionConflictError,
)

router = APIRouter(tags=["rules"])

#: What a "no such rule" refusal says. Short, because there is nothing to hide here — rules are GV's
#: own standards, not another project's data — but consistent with the rest of the API.
NOT_FOUND_DETAIL: Final = "Not found"

#: Every role defined at the API. Reading the rulebook is not one of the five actions in
#: `app/auth/roles.py`, and inventing a sixth would be deciding a permissions policy this story was
#: not asked to decide — so the route names the roles instead, which is what `require_role` is for.
#: A reviewer needs to read the rule that judged a drawing they are signing off; a rule admin needs
#: to read what they are about to change. Nobody else reaches an authenticated route at all.
READERS: Final = (Role.REVIEWER, Role.RULE_ADMIN, Role.ADMIN)


# ---------------------------------------------------------------------------
# The rulebook seam
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Rulebook:
    """The four things D6 needs, supplied by the deployment as one object.

    Bundled rather than passed as four separate dependencies so that a deployment cannot wire three
    of them. A rulebook with no regression check is not a rulebook that is nearly configured; it is
    one that would publish without the gate `AGENTS.md` §9 requires.
    """

    store: SnapshotStore
    """Published snapshots. Append-only; the identifier is the content hash."""

    log: PublicationLog
    """Who approved what. Append-only, and never overwritten."""

    proposals: Mapping[str, RuleProposal]
    """Proposals awaiting approval, keyed by the content hash each would publish as.

    Keyed by content hash rather than by a proposal id so that the thing an approver names is the
    thing they read. There is no route here that raises a proposal: authoring is done in
    `rules/rulebook/*.yaml` and validated by `rules.governance.proposal.propose`, and giving that a
    persisted home is a database table this story is not allowed to add.
    """

    regression: RegressionCheck
    """The gold-set regression D6 runs before publishing. Required, with no default (#238)."""


class RulebookNotConfigured(RuntimeError):
    """No rulebook is wired, so nothing can be read from it or published to it.

    Raised rather than defaulting to an empty store. An empty default would answer every read with
    "no rules" — which reads as a fact about the rulebook rather than as a missing setting — and
    would accept a publication into a dictionary that is discarded when the process restarts.
    """


#: Where a deployment puts the rulebook: `app.state.rulebook = Rulebook(...)`. Read off the app
#: rather than the environment, for the same reason settings are.
RULEBOOK_STATE: Final = "rulebook"


def get_rulebook(request: Request) -> Rulebook:
    """The configured rulebook, or a refusal.

    Local to this module rather than in `app/api/dependencies.py`: that module holds what more than
    one route group needs, and this is needed by one.
    """
    rulebook: Rulebook | None = getattr(request.app.state, RULEBOOK_STATE, None)
    if rulebook is None:
        raise RulebookNotConfigured(
            "no rulebook is configured. Set `app.state.rulebook` in the application factory or "
            "override `get_rulebook`; this refuses rather than defaulting to an empty store, "
            "because an empty default answers 'which rules exist?' with a wrong fact instead of a "
            "missing setting."
        )
    return rulebook


# ---------------------------------------------------------------------------
# Rendering — reporting the domain's judgements, never forming one
# ---------------------------------------------------------------------------


def release_note(rule: Rule) -> str:
    """Say in one sentence whether this rule could be released, and why not.

    The judgement is `rules/publication.py`'s; this only renders it. Spelled out because
    `production_ready: false` in a JSON body is easy to skim past, and what it means — the check
    cannot decide anything, for a reason that has nothing to do with the drawing — is not something
    a reader can infer from the flag.
    """
    missing = unconfirmed_tolerance_count(rule)
    if not missing:
        return "Releasable: every tolerance this rule uses is a client-confirmed value."
    subject = (
        "1 tolerance in this rule is still a placeholder"
        if missing == 1
        else f"{missing} tolerances in this rule are still placeholders"
    )
    return (
        f"Not releasable: {subject}. The client has not supplied the number, so this rule returns "
        "REVIEW REQUIRED for every drawing and can never produce a PASS or a FAIL."
    )


def render_tolerance(tolerance: Tolerance) -> str:
    """A tolerance as exact text: `1/8 in`, or `UNCONFIRMED`.

    Text rather than a number because `1/8` is a `Fraction`. JSON has no fractions, so serialising
    it would produce `0.125` — a float, in the one place this project refuses to have one
    (ADR-0001). The string round-trips exactly and cannot be mistaken for something to do sums with.
    """
    if not tolerance.is_confirmed:
        return "UNCONFIRMED"
    unit = tolerance.unit.value if tolerance.unit is not None else ""
    return f"{tolerance.value} {unit}".strip()


def _snapshot_out(snapshot: RuleSnapshot, *, effective: bool) -> RuleSnapshotOut:
    """One snapshot, with the bytes its identifier was computed from."""
    return RuleSnapshotOut(
        rule_id=snapshot.rule_id,
        version=snapshot.version,
        snapshot_id=snapshot.snapshot_id,
        canonical_json=snapshot.canonical_json,
        production_ready=is_production_ready(snapshot.rule),
        unconfirmed_tolerances=unconfirmed_tolerance_count(snapshot.rule),
        release_note=release_note(snapshot.rule),
        effective=effective,
    )


def _rule_out(snapshot: RuleSnapshot, *, published_versions: int) -> RuleOut:
    """One rule as it stands today, from its effective snapshot."""
    rule = snapshot.rule
    return RuleOut(
        rule_id=snapshot.rule_id,
        name=rule.name,
        version=snapshot.version,
        snapshot_id=snapshot.snapshot_id,
        product_type=rule.product_type.value,
        check_type=rule.check_type.value,
        severity=rule.severity.value,
        production_ready=is_production_ready(rule),
        unconfirmed_tolerances=unconfirmed_tolerance_count(rule),
        release_note=release_note(rule),
        published_versions=published_versions,
    )


def _effective(store: SnapshotStore, rule_id: str) -> RuleSnapshot:
    """The snapshot the resolver would pick for this rule today, or a 404.

    A rule nobody has published is reported as absent rather than as an empty rule. "This rule
    exists but has no versions" is not a state the store can be in, and inventing a response for it
    would describe something that cannot happen.
    """
    snapshot = store.latest(rule_id)
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)
    return snapshot


# ---------------------------------------------------------------------------
# Reading the rulebook
# ---------------------------------------------------------------------------


@router.get(
    "/rules",
    response_model=list[RuleOut],
    summary="Every published rule, with whether it could be released",
)
def list_rules(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    rulebook: Annotated[Rulebook, Depends(get_rulebook)],
) -> list[RuleOut]:
    """Every rule in the rulebook, at its effective version, rule id order.

    A rule whose tolerance is still a placeholder appears here like any other, and says plainly that
    it cannot be released. Leaving it out would make the rulebook look finished, which is exactly
    how a placeholder starts reading as a fact (ADR-0011).

    Not paged. The collection is the rules GV has authored — tens of them, not thousands — and no
    API client can add to it, so a cursor would be machinery guarding against growth that this
    endpoint cannot experience.
    """
    del principal  # the dependency is the check; the endpoint needs nothing from the caller

    return [
        _rule_out(
            _effective(rulebook.store, rule_id),
            published_versions=len(rulebook.store.versions_of(rule_id)),
        )
        for rule_id in rulebook.store.rule_ids()
    ]


@router.get(
    "/rules/tolerance-report",
    response_model=ToleranceReportOut,
    summary="How much of the rulebook is still waiting on a client value",
)
def read_tolerance_report(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    rulebook: Annotated[Rulebook, Depends(get_rulebook)],
) -> ToleranceReportOut:
    """Which rules are still guesswork, and how many.

    Declared before `/rules/{rule_id}/...` so the literal path cannot be shadowed by a rule id.

    The prose comes from `rules/publication.py` unchanged, because it is written to be pasted into a
    client email without editing — a report only this codebase can read is not a report. The counts
    beside it come from `rules/governance/readiness.py`, so the numbers and the sentence cannot
    drift into describing different rulebooks.

    Why this is an endpoint at all: the `±1/8″` figure that circulated for weeks was our own
    placeholder from a sample file, and it reached `docs/RULE_ENGINE_SPEC.md` §4 reading as fact.
    Nothing counted placeholders, so nothing contradicted the impression that the rulebook was
    finished.
    """
    del principal

    assessment = readiness.assess(rulebook.store)
    return ToleranceReportOut(
        report=tolerance_report(rulebook.store),
        total_rules=assessment.total_rules,
        releasable=assessment.releasable,
        blocked=assessment.blocked,
        can_release_anything=assessment.can_release_anything,
        awaiting_tolerance=[
            AwaitingToleranceOut(
                rule_id=waiting.rule_id,
                version=waiting.version,
                snapshot_id=waiting.snapshot_id,
                unconfirmed=waiting.unconfirmed,
            )
            for waiting in awaiting_tolerance(rulebook.store)
        ],
    )


@router.get(
    "/rules/{rule_id}/snapshots",
    response_model=list[RuleSnapshotOut],
    summary="Every published version of one rule, newest first",
)
def list_snapshots(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    rulebook: Annotated[Rulebook, Depends(get_rulebook)],
    rule_id: str,
) -> list[RuleSnapshotOut]:
    """Every snapshot of this rule, each with its content hash and the bytes behind it.

    Superseded versions stay listed. A finding cites the snapshot that produced it, and answering
    "what did you tell us in March?" means being able to read the rule as it was in March.

    Each entry carries `canonical_json` in full so the hash can actually be checked. That costs a
    few kilobytes per version and buys the client the ability to confirm they received what the
    identifier names; a hash returned without the bytes it covers is decoration.
    """
    del principal

    effective = _effective(rulebook.store, rule_id)
    snapshots = sorted(
        rulebook.store.versions_of(rule_id),
        key=lambda item: tuple(int(part) for part in item.version.split(".")),
        reverse=True,
    )
    return [
        _snapshot_out(snapshot, effective=snapshot.snapshot_id == effective.snapshot_id)
        for snapshot in snapshots
    ]


@router.get(
    "/rules/{rule_id}/applicability",
    response_model=ApplicabilityOut,
    summary="What this rule applies to",
)
def read_applicability(
    principal: Annotated[Principal, Depends(require_role(*READERS))],
    rulebook: Annotated[Rulebook, Depends(get_rulebook)],
    rule_id: str,
) -> ApplicabilityOut:
    """The discriminator that selects a variant, and the tolerance each branch carries.

    Reported for the effective version, because the question this answers is "what would this rule
    do to a drawing today?".

    A rule that applies to everything of its product type says so explicitly (`scope: global`) — it
    does not simply have no variants. ADR-0007 refuses to read an absent applicability as "applies
    to everything", and this response keeps that distinction visible rather than flattening both
    cases into an empty list.
    """
    del principal

    snapshot = _effective(rulebook.store, rule_id)
    applicability = snapshot.rule.applicability
    if not isinstance(applicability, Applicability):
        return ApplicabilityOut(
            rule_id=snapshot.rule_id,
            version=snapshot.version,
            snapshot_id=snapshot.snapshot_id,
            scope="global",
            discriminator=None,
            variants=[],
        )
    return ApplicabilityOut(
        rule_id=snapshot.rule_id,
        version=snapshot.version,
        snapshot_id=snapshot.snapshot_id,
        scope="discriminated",
        discriminator=applicability.discriminator,
        variants=[
            ApplicabilityVariantOut(
                when=variant.when,
                tolerance=render_tolerance(variant.tolerance),
                tolerance_confirmed=variant.tolerance.is_confirmed,
            )
            for variant in applicability.variants
        ],
    )


# ---------------------------------------------------------------------------
# Publishing — every decision belongs to D6
# ---------------------------------------------------------------------------

#: How an API role maps onto the rulebook's own roles. Written out, and deliberately incomplete.
#:
#: `app/auth/roles.py` has three roles; `rules/governance/publish.py` has two. `admin` has no
#: rulebook equivalent, and it is not given one here. Adding `admin` to the governance enum, or
#: mapping it to `rule_admin`, would let the transport grant a publishing right the rulebook never
#: defined — and the approval record would then name a role the domain has no concept of, which is
#: precisely the record somebody relies on when a finding is disputed. So an `admin` who does not
#: also hold `rule_admin` is refused, with a message that says what to ask for.
#:
#: Whether `admin` *should* be able to publish is a decision for whoever owns the permission table,
#: not for this module. It is raised in the pull request rather than settled here.
GOVERNANCE_ROLES: Final[Mapping[Role, GovernanceRole]] = {
    Role.REVIEWER: GovernanceRole.REVIEWER,
    Role.RULE_ADMIN: GovernanceRole.RULE_ADMIN,
}


def approver_for(principal: Principal) -> Approver:
    """Turn the authenticated caller into the rulebook's idea of an approver, or refuse.

    Refuses rather than choosing a nearby role. The two `Role` enums are not the same enum and are
    not going to be converged by this endpoint: one describes what the API permits, the other
    describes a distinction the rulebook makes about who may change what the system decides.

    `reviewer` is mapped even though a reviewer may not publish. That is on purpose: D6 refuses it
    with a message explaining why publishing and reviewing are different rights, and pre-empting
    that here would replace D6's reason with our own.
    """
    if not principal.id.strip():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "An approval must name a person, and the authenticated caller has no name. "
                "'Approved by the system' answers a different question from 'approved by whom', "
                "and only the second is defensible later."
            ),
        )

    mapped = {GOVERNANCE_ROLES[role] for role in principal.roles if role in GOVERNANCE_ROLES}
    if not mapped:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Your role has no equivalent in the rulebook's approval model, so this publication "
                "could not be recorded as approved by anyone the rulebook recognises. Publishing "
                "needs the rule-administrator role. This is refused rather than approximated: "
                "recording an approval under a role the rulebook does not define would make the "
                "record unusable at the moment it matters."
            ),
        )

    # `rule_admin` wins when both are held: they are separate rights rather than a ranking, and the
    # caller is exercising the publishing one. A caller holding only `reviewer` is passed through as
    # a reviewer so that D6 refuses them, and says why.
    role = (
        GovernanceRole.RULE_ADMIN
        if GovernanceRole.RULE_ADMIN in mapped
        else GovernanceRole.REVIEWER
    )
    return Approver(name=principal.id, role=role)


@router.post(
    "/rules/{rule_id}/publish",
    response_model=PublicationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Publish an approved proposal through the D6 gate",
)
def publish_rule(
    principal: Annotated[Principal, Depends(require_action(Action.PUBLISH_RULE))],
    rulebook: Annotated[Rulebook, Depends(get_rulebook)],
    rule_id: str,
    body: PublicationRequest,
) -> PublicationOut:
    """Approve and publish one proposal. Every decision is D6's.

    This endpoint does four things and decides nothing: it maps the caller onto a rulebook approver,
    finds the proposal they named, calls `rules.governance.publish.publish()`, and turns whatever
    comes back — a snapshot or a refusal — into a response. There is no "already approved"
    shortcut, no override for a failed regression, and no path that writes a snapshot without going
    through that call.

    **Who may.** `Action.PUBLISH_RULE` from the permission table, and then a rulebook role. The two
    are different questions and both are asked. A caller the API permits but the rulebook has no
    role for is refused with `403` rather than approximated — see `GOVERNANCE_ROLES`.

    **Which proposal.** Named by the content hash it would publish as. The hash changes the moment
    the proposed rule does, so approving one is approving the exact bytes that were read.

    **What each refusal means.**

    | Status | Reason |
    |---|---|
    | `403` | The caller may not publish, or may not approve this particular proposal (they raised it). |
    | `404` | No such proposal for this rule. |
    | `409` | The proposal did not validate, its tolerance is unconfirmed and the target is production, the regression failed, or this content is already published. |

    Every one of those carries D6's own explanation. Replacing them with a generic message would
    send an author looking for a fault in their rule when the real problem is that they are not
    allowed to publish at all.
    """
    approver = approver_for(principal)

    proposal = rulebook.proposals.get(body.snapshot_id)
    if proposal is None or proposal.rule_id != rule_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND_DETAIL)

    try:
        snapshot = publish_snapshot(
            proposal,
            approver=approver,
            store=rulebook.store,
            log=rulebook.log,
            regression=rulebook.regression,
            target=body.target,
        )
    except (NotAuthorised, SelfApproval) as refusal:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(refusal)) from refusal
    except (
        NotApprovable,
        NotProductionReadyError,
        RegressionFailed,
        SnapshotConflictError,
        VersionConflictError,
        ValueError,
    ) as refusal:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(refusal)) from refusal

    # The approval is read back from the log rather than rebuilt here, so the response cannot say
    # something different from the record. An approval time invented at this layer would be a second
    # answer to "when was this approved?", and the two would disagree the first time a clock did.
    approval = rulebook.log.for_snapshot(snapshot.snapshot_id)
    if approval is None:  # pragma: no cover - publish() records before it stores
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="the snapshot was stored without an approval record",
        )

    return PublicationOut(
        snapshot_id=snapshot.snapshot_id,
        rule_id=snapshot.rule_id,
        version=snapshot.version,
        approved_by=approval.approver,
        author=approval.author,
        rationale=approval.rationale,
        regression_summary=approval.regression_summary,
        target=approval.target,
        approved_at=approval.approved_at,
        production_ready=is_production_ready(snapshot.rule),
        unconfirmed_tolerances=unconfirmed_tolerance_count(snapshot.rule),
        release_note=release_note(snapshot.rule),
    )


__all__ = [
    "GOVERNANCE_ROLES",
    "READERS",
    "RULEBOOK_STATE",
    "Rulebook",
    "RulebookNotConfigured",
    "approver_for",
    "get_rulebook",
    "release_note",
    "render_tolerance",
    "router",
]
