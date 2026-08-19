"""Writing and reading the correction ledger — every value a reviewer changed, kept forever.

`AGENTS.md` §9 counts the reviewer correction rate as a release metric, so this is the table that
says where the automation is unreliable. That is exactly the record somebody would be tempted to
tidy, which is why nothing here can rewrite one: the only write is an insert.

**Which layer guarantees what.** Being precise about this matters, because "append-only" is easy to
claim and easy to over-claim.

* The **database** refuses `UPDATE` and `DELETE` on `correction_ledger`. `alembic/versions/
  0013_append_only.py` installs a `BEFORE UPDATE OR DELETE` trigger calling `gv_reject_mutation()`
  over every table carrying the `Immutable` marker, this one included. It refuses whoever is
  connected, through the ORM or through raw SQL. This module adds nothing to that and does not
  re-check it — a Python guard in front of a database guard only tells you which of the two somebody
  went around.
* The **database** also refuses a ledger row that is not a correction (a composite foreign key plus
  a `CHECK` pin it to a `correct` review action), a row whose two values are equal, an empty value,
  a second row for the same action, and an observation id that names nothing.
* This **module** decides only *when* those refusals surface. `record_correction` flushes, so a bad
  row raises at the call that wrote it rather than at some later commit in another part of the
  request.
* Nothing here — and nothing in the database — stops a correction being *superseded*. A later
  reviewer correcting the same observation writes a **new** review action and a **new** ledger row;
  both rows survive, and `history_for_observation` returns them in the order they happened. The
  newest is the current answer. That is the whole of what "never edited, only superseded" means.
* Not guaranteed at any layer: tamper-proofing. A role that owns the table can disable the trigger,
  and a superuser can bypass every user trigger. `tests/db/test_append_only.py` demonstrates that
  boundary rather than pretending it away.

**Why the queries join out to the rule, the check type and the vendor rather than storing them.**
A ledger row that carried its own copy of the rule id would be a copy that can disagree with the
finding it came from, and the disagreement would surface as a correction attributed to the wrong
rule. Every read below walks real foreign keys — action → finding → check run → rule snapshot, and
check run → revision → package — so the answer is derived from the same rows the verdict was.

**Vendor is read here, and only here.** ADR-0006 makes vendor identity metadata and never a rule
key: a vendor that repeatedly gets filler distribution wrong is a conversation, not a different
rulebook. Spotting that pattern is the one legitimate use, and it lives on the reviewer's side of
the line. `tests/test_vendor_neutrality.py` asserts `verdict/` and `rules/` cannot reach vendor data
at all.

**Nothing in the rules path may read this module.** A correction is a reviewer fixing one drawing;
a rule is a published decision with an author and a regression run behind it. Corrections becoming
rules by accumulation is how a system quietly starts deciding what it was told to check
(`AGENTS.md` §2.6, `docs/DESIGN_PRODUCT.md` §4.2). Enforced by import guard, not by this paragraph:
`tests/review/test_ledger.py` and `tests/test_verdict_isolation.py`.

**No arithmetic happens here.** Both values are stored verbatim as text, because a correction is as
likely to be to a unit, a label or an identifier as to a dimension. Nothing in this module parses,
converts or compares them numerically, so there is nothing for a float to round.

Source: `AGENTS.md` §2.6, §9; system design §16 · Design: `docs/DESIGN_PRODUCT.md` §4 ·
Verification: `tests/review/test_ledger.py`
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, Select, select
from sqlalchemy.orm import Session

from app.db.base import utc_now
from app.models.package import Package, PackageRevision
from app.models.review import CorrectionLedgerEntry, ReviewAction, ReviewActionKind
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun, Finding

__all__ = [
    "by_check_type",
    "by_rule",
    "by_vendor",
    "history_for_observation",
    "record_correction",
]


def record_correction(
    session: Session,
    *,
    review_action_id: UUID,
    canonical_observation_id: UUID,
    original: str,
    corrected: str,
) -> CorrectionLedgerEntry:
    """Write one correction and return the stored row.

    The original is written beside the correction, always. Keeping only the corrected value would
    leave no way to ask what we got wrong, which is the entire point of the table.

    `canonical_observation_id`, not `observation_id`: there are two observation tables, and only a
    `CanonicalObservation` is a fact the system acted on. An `ObservationCandidate` is one
    extractor's unverified reading, and a correction filed against one would be a correction to
    something no verdict ever used. The column is named `canonical_observation_id` and the argument
    matches it, so the two cannot be confused at a call site.

    The row is flushed before returning. Without that, a row the database refuses — a correction
    hung off a `confirm`, a correction that changes nothing, an observation id naming no row — would
    raise at commit time, somewhere the caller cannot tell which write caused it.

    Nothing is checked here that the database checks. Re-checking would let this function and the
    schema drift apart, and the version that drifted would be the one nobody was watching.

    Args:
        session: the caller's session. This function never commits; the caller's transaction
            decides whether the correction and the review action it belongs to stand together.
        review_action_id: the `correct` action this records. The database resolves it against
            `review_actions` and refuses any other kind of action.
        canonical_observation_id: the reading that was wrong.
        original: what we read, verbatim.
        corrected: what the reviewer says it is, verbatim.

    Returns:
        The persisted entry, with its id and `created_at` populated.
    """
    entry = CorrectionLedgerEntry(
        review_action_id=review_action_id,
        action=ReviewActionKind.CORRECT.value,
        canonical_observation_id=canonical_observation_id,
        original_value=original,
        corrected_value=corrected,
    )
    session.add(entry)
    session.flush()
    return entry


def _since(window: timedelta) -> datetime:
    """The start of the window, refusing one that cannot contain anything.

    A zero or negative window returns an empty list from every query below, which reads as "no
    corrections" — an answer indistinguishable from a clean month. A metric that reports good news
    when it was asked a nonsensical question is worse than one that fails.
    """
    if window <= timedelta(0):
        raise ValueError(
            "window must be a positive duration; a zero or negative window would report "
            "'no corrections' rather than 'you asked for nothing'"
        )
    return utc_now() - window


#: The one row type every query below returns. Named so the join helper's signature stays readable.
LedgerQuery = Select[tuple[CorrectionLedgerEntry]]


def _entries_in_window(window: timedelta, *where: ColumnElement[bool]) -> LedgerQuery:
    """Ledger entries in the window, joined out to the rule and the package that produced them.

    One join path shared by all three pattern queries, so they cannot answer the same question
    differently. Every hop is a foreign key that already exists:

        correction_ledger -> review_actions -> findings -> check_runs
                                                        -> rule_snapshots -> rule_definitions
                                            check_runs  -> package_revisions -> packages

    Ordered oldest first and tie-broken on id: two corrections written in the same transaction can
    share a timestamp, and an unordered result would shuffle between runs of the same report.
    """
    return (
        select(CorrectionLedgerEntry)
        .join(ReviewAction, ReviewAction.id == CorrectionLedgerEntry.review_action_id)
        .join(Finding, Finding.id == ReviewAction.finding_id)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .join(PackageRevision, PackageRevision.id == CheckRun.package_revision_id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(CorrectionLedgerEntry.created_at >= _since(window), *where)
        .order_by(CorrectionLedgerEntry.created_at, CorrectionLedgerEntry.id)
    )


def by_rule(session: Session, rule_id: str, window: timedelta) -> list[CorrectionLedgerEntry]:
    """Corrections against one rule, within the last `window`.

    `rule_id` is the authored identifier — `CT-WIDTH-001` — not a snapshot hash. A rule that keeps
    being corrected is the same rule across its published versions, and asking per snapshot would
    split the very pattern this exists to surface.
    """
    return list(session.scalars(_entries_in_window(window, RuleDefinition.rule_id == rule_id)))


def by_check_type(
    session: Session, check_type: str, window: timedelta
) -> list[CorrectionLedgerEntry]:
    """Corrections against every rule of one check type.

    Check type is an attribute of the rule — which documents it reads — so a cluster here points at
    a class of drawing we read badly rather than at one rule being wrong.
    """
    return list(session.scalars(_entries_in_window(window, RuleSnapshot.check_type == check_type)))


def by_vendor(session: Session, vendor: str, window: timedelta) -> list[CorrectionLedgerEntry]:
    """Corrections on packages from one vendor.

    Metadata, never a rule key (ADR-0006). This answers "whose drawings do we keep having to
    correct", which is a conversation to have with that vendor — it is not, and must never become,
    an input to how carefully their drawings are checked.

    Packages with no vendor recorded are simply absent from the result; `Package.vendor` is
    nullable and a NULL matches nothing, which is the honest answer rather than a bucket.
    """
    return list(session.scalars(_entries_in_window(window, Package.vendor == vendor)))


def history_for_observation(
    session: Session, canonical_observation_id: UUID
) -> list[CorrectionLedgerEntry]:
    """Every correction ever filed against one reading, oldest first.

    This is what supersession looks like when nothing can be edited. A second reviewer disagreeing
    with the first writes a new review action and a new row; the first row stays exactly as it was,
    and the last row in this list is the current answer. Reading only the newest would answer "what
    do we think now"; the whole list answers "how did we get here", which is the question an audit
    asks.

    No window: the point of keeping corrections forever is being able to read all of them.
    """
    return list(
        session.scalars(
            select(CorrectionLedgerEntry)
            .where(CorrectionLedgerEntry.canonical_observation_id == canonical_observation_id)
            .order_by(CorrectionLedgerEntry.created_at, CorrectionLedgerEntry.id)
        )
    )
