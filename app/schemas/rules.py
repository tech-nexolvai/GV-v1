"""What the rules and operations endpoints accept and return (#206, C2.4).

**Nothing here can carry a rule.** The only thing a client may send to this route group is the
content hash of a proposal somebody else raised, plus which boundary it is being published to. That
is not an omission: a request body that carried a rule would carry its `Tolerance` values too, and
the API would then be a way to put a number the client never confirmed into the thing that decides
PASS or FAIL. `docs/DESIGN_PLATFORM.md` §4.2 (C2.5) forbids a client-supplied verdict field, and the
cheapest way to honour it here is to have no field that could hold one.

**A tolerance is rendered as a string, never a number.** `1/8` is a `Fraction` in the rulebook, and
JSON has no fractions — serialising it would produce `0.125`, and ADR-0001 forbids a float anywhere
near this arithmetic. So the wire format carries the exact text (`"1/8 in"`, or `"UNCONFIRMED"`),
which round-trips without losing anything and cannot be mistaken for something you may do sums with.

**Readiness is reported, not re-decided.** `production_ready` and `unconfirmed_tolerances` come
straight from `rules/publication.py`. `release_note` renders that judgement as a sentence; it does
not form one of its own.

Source: backend proposal §10.2, §11 · Design: `docs/DESIGN_PLATFORM.md` §4.1 ·
Verification: `tests/api/test_rules_api.py`
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from rules.governance.publish import PublicationTarget

# ---------------------------------------------------------------------------
# The rulebook
# ---------------------------------------------------------------------------


class RuleSnapshotOut(BaseModel):
    """One published version of a rule, with everything needed to check what you were given.

    `snapshot_id` is the content hash and `canonical_json` is the exact bytes it was computed from,
    so a client can recompute `sha256(canonical_json)` and compare. What that proves is narrow and
    worth stating: the bytes in this response are the bytes this identifier names. It is not a
    signature — anyone able to change the response could change both halves — and it says nothing
    about whether this is the snapshot that judged any particular drawing. That question is answered
    by comparing this identifier with the one on the finding.
    """

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    version: str

    snapshot_id: str = Field(
        description=(
            "The content hash, algorithm included — `sha256:<hex>`. Recompute it from "
            "`canonical_json` to confirm you received what this identifier names."
        )
    )

    canonical_json: str = Field(
        description=(
            "The exact RFC 8785 bytes that were hashed. Returned in full rather than behind a "
            "second request because a hash you cannot check is a promise rather than a fact."
        )
    )

    production_ready: bool = Field(
        description=(
            "False when any tolerance in this rule is still a placeholder. Such a rule can be "
            "authored, reviewed and tested, and it can never produce a PASS or a FAIL."
        )
    )

    unconfirmed_tolerances: int
    release_note: str = Field(
        description="The same judgement as `production_ready`, in a sentence a person can act on."
    )

    effective: bool = Field(
        description=(
            "Whether this is the version the resolver would pick today — the highest version of "
            "this rule. Older snapshots stay listed because findings cite them."
        )
    )


class RuleOut(BaseModel):
    """One rule, as it stands today: its effective snapshot and whether it could be released."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    name: str
    version: str
    snapshot_id: str
    product_type: str
    check_type: str
    severity: str

    production_ready: bool
    unconfirmed_tolerances: int
    release_note: str

    published_versions: int = Field(
        description="How many snapshots exist for this rule, including superseded ones."
    )


class ApplicabilityVariantOut(BaseModel):
    """One branch of a rule's discriminator, e.g. `when: back_left_right`."""

    model_config = ConfigDict(extra="forbid")

    when: str
    tolerance: str | None = Field(
        description=(
            "The exact authored tolerance as text — `1/8 in`, `UNCONFIRMED` when the client has "
            "not supplied one, or null when this exact-equality operation uses no tolerance. A "
            "string rather than a number on purpose: `1/8` is a fraction, and rendering it as "
            "0.125 would put a float in the one place this project refuses to."
        )
    )
    tolerance_confirmed: bool | None = Field(
        description="Null when no tolerance applies; otherwise whether its value is confirmed."
    )


class ApplicabilityOut(BaseModel):
    """What a rule applies to: one discriminator and its branches, or everything of its type."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    version: str
    snapshot_id: str

    scope: str = Field(
        description=(
            "`global` when the rule applies to every item of its product type, otherwise "
            "`discriminated`. A rule with no discriminator has to say so — ADR-0007 refuses to read "
            "an absent applicability as 'applies to everything'."
        )
    )
    discriminator: str | None = Field(
        default=None, description="The field that selects a variant, e.g. `wall_config`."
    )
    variants: list[ApplicabilityVariantOut] = Field(default_factory=list)


class AwaitingToleranceOut(BaseModel):
    """One rule that cannot be released, and how much of it is still a guess."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str
    version: str
    snapshot_id: str
    unconfirmed: int


class ToleranceReportOut(BaseModel):
    """How much of the rulebook is still waiting on a number from the client.

    `report` is written to be pasted into an email unedited — that is the whole point of it, and
    `tests/api/test_rules_api.py` asserts on the wording rather than only on the counts.
    """

    model_config = ConfigDict(extra="forbid")

    report: str
    total_rules: int
    releasable: int
    blocked: int
    can_release_anything: bool = Field(
        description=(
            "False when no rule in the rulebook could produce a PASS or a FAIL. Kept separate from "
            "`blocked` because 'some rules are waiting' and 'nothing works yet' are different "
            "conversations."
        )
    )
    awaiting_tolerance: list[AwaitingToleranceOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------


class PublicationRequest(BaseModel):
    """Which proposal is being approved, and what for.

    It names a proposal rather than carrying one. Two reasons, and the second is the load-bearing
    one: no rule content crosses this boundary, so no tolerance a client typed can reach the
    rulebook; and naming the exact content hash means the approver approves the bytes they read,
    not whatever the proposal says by the time the request arrives.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str = Field(
        min_length=1,
        pattern=r"^sha256:[0-9a-f]{64}$",
        description=(
            "The content hash of the proposal being approved — the `sha256:<hex>` it would publish "
            "as. Not a proposal id: the hash changes the moment the proposed rule does, so an "
            "approval cannot silently follow an edit."
        ),
    )
    target: PublicationTarget = Field(
        description=(
            "`development` for authoring and testing, where a placeholder tolerance is expected. "
            "`production` for real drawings, where every tolerance must be a client-supplied "
            "number — a rule with a placeholder is refused here (ADR-0011)."
        )
    )


class PublicationOut(BaseModel):
    """What was published, by whom, and on what basis.

    Carries the approver, the author and the regression summary because "was this approved?" is a
    weaker question than "who approved it, who wrote it, and what was it checked against?", and only
    the second is worth anything when a finding is disputed months later.
    """

    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    rule_id: str
    version: str

    approved_by: str
    author: str
    rationale: str
    regression_summary: str
    target: PublicationTarget
    approved_at: datetime

    production_ready: bool
    unconfirmed_tolerances: int
    release_note: str


# ---------------------------------------------------------------------------
# The typed operation registry
# ---------------------------------------------------------------------------


class OperationOut(BaseModel):
    """One reviewed operation a rule may name, and the operands it takes.

    This is a signature, not an implementation. A rule selects an operation by name from the typed
    registry (`AGENTS.md` §2.2); there is no field here, and none anywhere in the API, that could
    carry executable text.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    kind: str = Field(
        description=(
            "`verdict` for an operation that decides an outcome, `derivation` for one that produces "
            "an intermediate value another operation consumes."
        )
    )
    operands: dict[str, str] = Field(
        description="Operand name to arity — `scalar` for one value, `list` for many."
    )
