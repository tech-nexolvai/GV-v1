"""Reading a stored exception and deciding whether it still applies.

An exception is a reviewer saying *this particular thing is acceptable, until this date*. It is the
one place in the product where a human deliberately accepts something the checks failed, so it is
also the easiest place to switch a check off by accident and never notice.

Three refusals carry the whole story, and each one exists because of a specific way this goes wrong.

**An exception with no end date is a deleted check.** `expires_at` is required here exactly as it is
`NOT NULL` in `app/models/review.py`: no default, no `None`, nothing to omit. The date is what forces
somebody to look again, and the person who looks again is usually not the person who granted it.

**An exception with a vague scope is a rule change with no author.** The scope names one finding, one
item or one package revision, by id, and matching is exact — same kind *and* same id. Nothing here
matches "similar" things, walks up to a parent, or falls back to a broader scope when the narrow one
misses. A rule that should stop firing everywhere is a rule change and goes through the rulebook,
where somebody reviews it.

**An expiry only checked when the exception is written has already failed.** The clock moves after
the row is stored, so expiry is enforced *here*, at the moment the exception is read and asked
whether it applies — `ExceptionGrant.from_stored` will happily build an exception that expired last
year, and `decide` will then refuse to apply it and say so. An expired exception that quietly kept
suppressing findings would be indistinguishable from having deleted the check.

Nothing in this module removes a finding. `apply_exceptions` returns exactly one decision per finding
in the order it was given them, so a suppressed finding is still in the report, still readable, and
now carries the reason, the approver and the expiry beside it. A finding that disappears because
somebody excepted it is a finding nobody can audit.

All times are timezone-aware and compared exactly. A naive datetime is rejected rather than assumed
to be UTC: guessing the zone is how an exception ends up living hours longer than it was granted for,
and in the worst case forever. There is no floating-point arithmetic anywhere in this module — time
is compared as `datetime` and measured as `timedelta`, both exact.

Source: backend proposal §10.1; `AGENTS.md` §2.6 · Design: `docs/DESIGN_PRODUCT.md` §4.1 ·
Verification: `tests/review/test_exceptions.py`
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from app.models.review import ExceptionScope

__all__ = [
    "ExceptionDecision",
    "ExceptionGrant",
    "ExceptionScope",
    "FindingRef",
    "StoredException",
    "apply_exceptions",
    "decide",
]


#: Narrowest first. Used only to report *which* exception is the one closest to the finding when
#: more than one covers it; it never widens anything, because a scope that does not match exactly
#: was never a candidate in the first place.
_NARROWNESS: dict[ExceptionScope, int] = {
    ExceptionScope.FINDING: 0,
    ExceptionScope.ITEM: 1,
    ExceptionScope.PACKAGE: 2,
}

#: A fixed aware upper bound, so "latest expiry first" can be expressed as an ascending sort key
#: (`_LATEST_POSSIBLE - expires_at`, a `timedelta`). Negating a timestamp would mean converting it
#: to a number, and the only obvious conversion is `float`.
_LATEST_POSSIBLE = datetime.max.replace(tzinfo=UTC)


def _aware(value: datetime, what: str) -> datetime:
    """Return `value` if it carries a real timezone, and refuse it otherwise.

    A naive datetime is not a time — it is a time and a missing assumption. Silently treating it as
    UTC would move an expiry by the reader's offset, which shows up as an exception that outlives
    its own end date rather than as an error anybody notices.
    """
    if not isinstance(value, datetime):
        raise TypeError(f"{what} must be a datetime, not {type(value).__name__}")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{what} must be timezone-aware; a naive datetime has no defined instant, and "
            "assuming one would silently change when this exception expires"
        )
    return value


class StoredException(Protocol):
    """The shape of a stored exception row, by its real column names.

    Structural rather than an import of the ORM class, so the read path can be exercised without a
    database — but the names are exactly `ReviewException`'s, so a column rename breaks this instead
    of quietly producing an exception with a missing field. See `app/models/review.py`.
    """

    @property
    def scope(self) -> str: ...

    @property
    def scope_id(self) -> UUID: ...

    @property
    def reason(self) -> str: ...

    @property
    def approved_by(self) -> str: ...

    @property
    def expires_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class ExceptionGrant:
    """One reviewer's decision to accept one specific thing, until one specific moment.

    Frozen, because an exception that can be widened or extended in place is a different exception
    with the same author's name on it.

    Every field is required. There is deliberately no default for `expires_at` — not `None`, not
    "a year", not "never" — so an unbounded exception cannot be constructed at all. That is the
    control this story exists for.

    An *already expired* grant is constructible on purpose: exceptions are read back from the
    database long after they were written, and refusing to build them would mean the report could
    not say "this expired on the 3rd" — it would just show nothing, which is the silent suppression
    the whole design is avoiding. Whether a grant still applies is answered by `applies_at`.
    """

    scope: ExceptionScope
    """Which kind of thing this covers: one finding, one item, or one package revision."""

    scope_id: UUID
    """*Which* finding, item or package revision. Matching on this is exact — see `covers`."""

    reason: str
    """Why this deviation is acceptable, in plain English. The sentence a future reader needs most:
    an exception nobody explained is one nobody can review, so an empty reason is refused."""

    approved_by: str
    """Who accepted it, by name. `AGENTS.md` §2.6 — an anonymous exception is nobody's decision."""

    expires_at: datetime
    """When it stops applying. Required, timezone-aware, and enforced every time it is read."""

    def __post_init__(self) -> None:
        """Refuse anything that would make this exception broader or longer than it was granted."""
        scope = self.scope
        if not isinstance(scope, ExceptionScope):
            try:
                scope = ExceptionScope(scope)
            except ValueError as unknown:
                permitted = ", ".join(member.value for member in ExceptionScope)
                raise ValueError(
                    f"{self.scope!r} is not an exception scope. An exception covers one named "
                    f"thing ({permitted}); switching a rule off everywhere is a rule change and "
                    "goes through the rulebook, not through here"
                ) from unknown
            object.__setattr__(self, "scope", scope)

        if not isinstance(self.scope_id, UUID):
            raise TypeError(
                "scope_id must be a UUID naming the finding, item or package revision this "
                f"covers, not {type(self.scope_id).__name__} — matching is exact, and a string "
                "would silently match nothing"
            )

        if not self.reason.strip():
            raise ValueError(
                "an exception needs a stated reason; one nobody explained is one nobody can review"
            )

        if not self.approved_by.strip():
            raise ValueError(
                "an exception needs a named approver; accepting a failed check is a person's "
                "decision, not the system's"
            )

        object.__setattr__(self, "expires_at", _aware(self.expires_at, "expires_at"))

    @classmethod
    def from_stored(cls, row: StoredException) -> ExceptionGrant:
        """Build a grant from a stored row, without deciding anything yet.

        The expiry is *not* checked here. Reading an expired exception has to succeed so the report
        can say it expired; refusing to load it would make it vanish instead.
        """
        return cls(
            scope=ExceptionScope(row.scope),
            scope_id=row.scope_id,
            reason=row.reason,
            approved_by=row.approved_by,
            expires_at=row.expires_at,
        )

    def applies_at(self, when: datetime) -> bool:
        """Is this exception still in force at `when`?

        Exclusive at the boundary: at exactly `expires_at` the exception has expired. An expiry
        names the moment cover ends, and where the two readings differ the safe one is the one that
        turns the check back on — a wrong FAIL costs review time, a wrong PASS can be manufactured.
        """
        return _aware(when, "when") < self.expires_at

    def covers(self, scope: ExceptionScope, scope_id: UUID) -> bool:
        """Does this exception name exactly this thing?

        Exact on both halves, and nothing else. Same-kind-and-same-id or no cover: there is no
        prefix match, no fuzzy match and no widening, because an exception that silently grew to
        cover a second thing is a rule change with nobody's name on it.

        A scope that is not one of the three raises rather than returning `False`. "This does not
        match" and "you asked a question that makes no sense" are different answers, and quietly
        merging them would let a typo read as a clean no-match.
        """
        return ExceptionScope(scope) is self.scope and scope_id == self.scope_id

    def status_at(self, when: datetime) -> str:
        """One plain-English sentence saying whether this still applies, and until or since when."""
        if self.applies_at(when):
            return f"in force until {self.expires_at.isoformat()}, approved by {self.approved_by}"
        return f"expired on {self.expires_at.isoformat()}, approved by {self.approved_by}"


@dataclass(frozen=True, slots=True)
class FindingRef:
    """A finding, and the two larger things an exception may instead have been granted against.

    An exception scoped to an item or a package covers this finding only if the finding really
    belongs to that item or that package revision — which is why the ids are carried here rather
    than looked up later. `item_id` is optional because not every finding is about a drawing item;
    when it is absent, an item-scoped exception simply does not cover this finding. It does not fall
    back to something broader.
    """

    finding_id: UUID
    package_revision_id: UUID
    item_id: UUID | None = None

    def id_for(self, scope: ExceptionScope) -> UUID | None:
        """The id an exception of this scope would have to name to cover this finding."""
        match scope:
            case ExceptionScope.FINDING:
                return self.finding_id
            case ExceptionScope.ITEM:
                return self.item_id
            case ExceptionScope.PACKAGE:
                return self.package_revision_id


@dataclass(frozen=True, slots=True)
class ExceptionDecision:
    """What the exceptions say about one finding at one moment, lapsed ones included.

    Deliberately not a boolean. A report that only knew "suppressed: yes" could not tell a reviewer
    who accepted it, why, or when it comes back; and a report that only knew "suppressed: no" could
    not tell them that an exception used to cover this and has since run out, which is the moment the
    finding needs looking at again.
    """

    finding: FindingRef
    at: datetime
    """The instant this decision was made against. Recorded because the answer depends on it."""

    in_force: tuple[ExceptionGrant, ...]
    """Every exception covering this finding and still live at `at`, narrowest first."""

    expired: tuple[ExceptionGrant, ...]
    """Every exception that covers this finding but has run out. Kept so the report can say so."""

    @property
    def applied(self) -> ExceptionGrant | None:
        """The exception the report leads with: the narrowest one covering this finding.

        Narrowest, because that is the decision somebody made about *this thing* rather than about
        the package it happens to sit in. Ties break towards the one that expires last, since that
        is when cover at this scope actually ends, and then on id so the choice is deterministic.
        `in_force` still lists every one of them — nothing is hidden by the choice.
        """
        return self.in_force[0] if self.in_force else None

    @property
    def is_excepted(self) -> bool:
        """Is this finding covered by a live exception right now?"""
        return bool(self.in_force)

    def explain(self) -> str:
        """One plain-English sentence for the report. Never silent, in either direction."""
        applied = self.applied
        if applied is not None:
            sentence = (
                f"Excepted at {applied.scope.value} scope until {applied.expires_at.isoformat()} "
                f"by {applied.approved_by}: {applied.reason.strip()}"
            )
            others = len(self.in_force) - 1
            if others:
                sentence += f" ({others} further exception(s) also cover this finding)"
            return sentence + ". The finding itself stands and is reported unchanged."
        if self.expired:
            lapsed = ", ".join(
                f"{grant.approved_by} at {grant.scope.value} scope, "
                f"expired {grant.expires_at.isoformat()}"
                for grant in self.expired
            )
            return (
                f"Not excepted: {len(self.expired)} exception(s) covering this finding have "
                f"expired ({lapsed}). An expired exception stops applying by itself, so this "
                "finding counts again and needs a decision."
            )
        return "Not excepted: no exception names this finding, its item or its package revision."


def _sort_in_force(grants: Iterable[ExceptionGrant]) -> tuple[ExceptionGrant, ...]:
    """Narrowest scope first, then latest expiry, then id — a total order, so the report is stable."""
    return tuple(
        sorted(
            grants,
            key=lambda grant: (
                _NARROWNESS[grant.scope],
                _LATEST_POSSIBLE - grant.expires_at,
                str(grant.scope_id),
            ),
        )
    )


def _sort_expired(grants: Iterable[ExceptionGrant]) -> tuple[ExceptionGrant, ...]:
    """Most recently expired first, then narrowest scope, then id."""
    return tuple(
        sorted(
            grants,
            key=lambda grant: (
                _LATEST_POSSIBLE - grant.expires_at,
                _NARROWNESS[grant.scope],
                str(grant.scope_id),
            ),
        )
    )


def _covers(grant: ExceptionGrant, finding: FindingRef) -> bool:
    """Does this exception name this finding, or the item or package revision it belongs to?

    Every candidate is offered to `covers` as a *pair* — the scope kind and the id at that scope —
    so both halves are compared. Comparing the id alone would let an item-scoped exception cover a
    finding whose id happened to equal the item's; ids are opaque and could collide across tables,
    and an exception that reached one table further than it was written for is a rule change nobody
    approved. A scope the finding has no id for (an item-scoped exception against a finding that is
    not about an item) is simply not a candidate: nothing widens to fill the gap.
    """
    for scope in ExceptionScope:
        target = finding.id_for(scope)
        if target is not None and grant.covers(scope, target):
            return True
    return False


def decide(
    finding: FindingRef, grants: Iterable[ExceptionGrant], *, when: datetime
) -> ExceptionDecision:
    """Work out what the exceptions say about one finding at one moment.

    Two separate questions, in this order, and a grant has to pass both:

    1. does it name this exact thing (exact scope kind and exact id)?
    2. is it still in force at `when`?

    Grants that fail the first are not this finding's business and are not mentioned. Grants that
    pass the first and fail the second are reported as expired, because "an exception used to cover
    this and has run out" is exactly what a reviewer needs to be told.

    `when` must be timezone-aware. Passing a naive one raises rather than being assumed to be UTC.
    """
    moment = _aware(when, "when")
    in_force: list[ExceptionGrant] = []
    expired: list[ExceptionGrant] = []
    for grant in grants:
        if not _covers(grant, finding):
            continue
        (in_force if grant.applies_at(moment) else expired).append(grant)
    return ExceptionDecision(
        finding=finding,
        at=moment,
        in_force=_sort_in_force(in_force),
        expired=_sort_expired(expired),
    )


def apply_exceptions(
    findings: Sequence[FindingRef], grants: Iterable[ExceptionGrant], *, when: datetime
) -> tuple[ExceptionDecision, ...]:
    """Decide every finding against every exception, dropping none of them.

    One decision per finding, in the order the findings were given. Nothing is filtered out, and
    that is the point: an excepted finding still appears in the report, now carrying who accepted
    it, why, and when the acceptance runs out. A finding that vanished because somebody excepted it
    would be indistinguishable from a check that was never run.
    """
    held = tuple(grants)
    return tuple(decide(finding, held, when=when) for finding in findings)
