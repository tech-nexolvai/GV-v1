"""Which drawing view a check may read its evidence from.

`CALL_2026_08_25_INPUTS` N2. Checks are not uniform across a package — they live in specific views,
and reading a dimension from the wrong one is the failure `docs/DESIGN_EXTRACTION.md` §3.2 names:
*"A countertop width found on a cabinet elevation is a plausible number attached to the wrong
drawing, and no tolerance check catches it."*

Overhang is the case that makes the point. It is only dimensioned in a section — absent from plan and
elevation entirely — so a plan-only pipeline never checks it and reports nothing wrong, which is a
false pass by omission rather than by arithmetic.

**An unmapped check is refused, not waved through.** Raj owes the full check-to-view list, and until
it lands `views_for` answers `None` for anything not in the working set below, and `may_read_from`
answers `False`. That is the deliberately awkward direction: a permissive default would let every
future check read any page, silently, and nothing downstream could tell that routing had never been
decided for it. Refusing turns the missing list into an abstention a reviewer sees.

**Nothing consumes this yet.** There is no pipeline joining a rule to the page its operands came from
— `Rule` carries no view field and extraction does not route by check. This is the declaration that
pipeline will read, built now because the mapping is a rules fact and belongs beside the rules, not
because a caller is waiting. Wiring it is a separate piece of work, and until then no check's
behaviour changes.

**It imports `vocabulary` only.** `PageType` lives there precisely so `rules/`, `extraction/` and
`app/` can all name a page without importing each other (`docs/DESIGN.md` §2).
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from vocabulary.page_types import PageType

__all__ = [
    "CHECK_VIEWS",
    "UnroutedCheckError",
    "may_read_from",
    "unrouted",
    "views_for",
]


class UnroutedCheckError(LookupError):
    """Raised when a caller demands the views for a check nobody has routed yet."""


#: The working set from the 2026-08-25 call, keyed by rule id.
#:
#: Raj gave three groupings and offered the complete mapping later:
#:
#: * **Plan** — cut-out position, front and back offsets, the wall-to-wall dimension.
#: * **Elevation** — cabinet widths, fillers, tags.
#: * **Section** — overhang, which is not visible in plan or elevation at all.
#:
#: `CT-DEPTH-001` is **deliberately absent**. Countertop depth is not in any of the three groupings
#: he gave, and placing it by inference — depth sounds sectional, so try section — would be exactly
#: the guess this module exists to prevent, on a routing decision nobody would ever see was made. It
#: stays unrouted until the full list arrives.
#:
#: No rule checks overhang yet, so `SECTION` names no check here. The grouping is recorded in the
#: docstring above rather than dropped, because the next person to author an overhang rule needs to
#: know it can only be read from a section before they wonder why the plan never has the number.
_ROUTES: dict[str, frozenset[PageType]] = {
    # Plan — the countertop laid out flat, where cut-outs and offsets are dimensioned.
    "CT-SINK-CUTOUT-WIDTH-001": frozenset({PageType.PLAN}),
    "CT-SINK-CUTOUT-DEPTH-001": frozenset({PageType.PLAN}),
    "CT-SINK-OFFSET-FRONT-001": frozenset({PageType.PLAN}),
    "CT-BACK-OFFSET-MIN-001": frozenset({PageType.PLAN}),
    "CT-WIDTH-001": frozenset({PageType.PLAN}),
    # Elevation — the run seen face-on, where cabinet widths and fillers are called out.
    "CAB-FILLER-001": frozenset({PageType.ELEVATION}),
    "CAB-ARCH-VS-SHOP-001": frozenset({PageType.ELEVATION}),
}

#: Read-only at runtime, and that is load-bearing rather than tidiness.
#:
#: A `Mapping` annotation is a promise to a type checker and nothing to the interpreter: the first
#: version exported the `dict` itself, so any importer could write `CHECK_VIEWS["CT-DEPTH-001"] =
#: ...` and route a check the client has never routed. That defeats the entire guarantee of this
#: module — `may_read_from` would then permit evidence for it, and nothing would record that the
#: route was invented at runtime rather than decided.
CHECK_VIEWS: Mapping[str, frozenset[PageType]] = MappingProxyType(_ROUTES)


def views_for(rule_id: str) -> frozenset[PageType] | None:
    """The views this check may read from, or `None` when nobody has routed it.

    `None` is a real answer and not an empty set. An empty set would say "this check may read from
    no view", which is a routing decision somebody made; `None` says the decision has not been made,
    and the two want different handling — the first is a bug to fix, the second is a question for
    the client.
    """
    return CHECK_VIEWS.get(rule_id)


def may_read_from(rule_id: str, page_type: PageType | None) -> bool:
    """Whether this check's evidence may come off a page of this type.

    False for an unrouted check, and false for an unclassified page. Both are absences rather than
    permissions: a page `extraction/page_type.py` could not classify must not become a page every
    check is willing to read, which would make the classifier's honesty the thing that widens the
    blast radius.
    """
    allowed = views_for(rule_id)
    if allowed is None or page_type is None:
        return False
    return page_type in allowed


def unrouted(rule_ids: frozenset[str] | set[str] | tuple[str, ...]) -> tuple[str, ...]:
    """Which of these checks have no view mapping, sorted.

    For a startup check or a report: the answer is the outstanding half of Raj's list, and it should
    be visible as a list of names rather than discovered one abstention at a time.
    """
    return tuple(sorted(rule_id for rule_id in rule_ids if rule_id not in CHECK_VIEWS))


def require_views(rule_id: str) -> frozenset[PageType]:
    """The views for a check, raising when it is unrouted.

    For callers that cannot proceed without an answer and want the failure at the point of the
    missing decision rather than three frames later. Callers that should abstain instead use
    `views_for` and handle `None`.
    """
    allowed = views_for(rule_id)
    if allowed is None:
        raise UnroutedCheckError(
            f"no drawing view is mapped for {rule_id!r}. The full check-to-view list is owed by the "
            "client (CALL_2026_08_25_INPUTS N2); until it lands this check must abstain rather than "
            "read from whichever page happens to be to hand."
        )
    return allowed
