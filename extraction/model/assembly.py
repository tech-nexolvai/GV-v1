"""Which cabinets sit beneath this countertop — and when nobody can tell.

CT-1, the headline check, is "the countertop is as wide as the cabinets and fillers beneath it".
Before any arithmetic can happen, something has to decide **which** cabinets those are. That is this
module. `docs/RULE_ENGINE_SPEC.md` §3a calls it `scope: same_assembly`.

**This is the most consequential resolver in the pipeline, and its failure is invisible.** A cabinet
wrongly included or wrongly dropped changes the expected width by a full cabinet. The verdict engine
will then compute that wrong number exactly, trace it faithfully, and report it with complete
confidence. Nothing downstream can catch it: the dual-unit lane corroborates that `984` was *read*
correctly and says nothing about *what it belongs to*. So the whole design here is built around
refusing rather than guessing.

**A partial run is never returned.** The run must reach both ends of the countertop, or the answer is
`CannotResolve`. This is the acceptance criterion that matters: a run one cabinet short is not a
smaller assembly, it is the same assembly with a cabinet missing, and summing it produces a PASS-shaped
number that is wrong by 600 mm.

**No run at all is a refusal too.** A countertop with nothing of cabinet or filler type beneath it
returns `CannotResolve`, not an empty `Assembly`. The first version of this module made it an empty
`Assembly` on the reasoning that "we found nothing" and "we found something that does not add up" send
a reviewer to different places — which is true, and is now carried by the wording of two different
refusals instead. What made it wrong is that the empty case took an early return past the end-reach
check, so it was the one `Assembly` that did not cover its countertop, and a caller trusting the type
would have summed it to zero believing the run complete. A countertop with every cabinet missing is
the largest version of a missing member, not an exemption from it.

**Ordered left to right, because the order is load-bearing.** Filler distribution (A6.4) is positional
— which filler is the left one and which the right one changes what a rule expects. A set would lose
that, so the run is a tuple ordered along the page and every member carries its index.

**Every member records why it was included.** `signals` maps each member to a plain-English sentence a
reviewer can check against the drawing. §2.4: "the geometry matched" is not a reason anybody can
verify.

## What the three signals actually are here

*View membership* is exact. Two elevations can sit on one sheet, so identity is `(page, tag)` and not
the tag alone (§4.1). An item in a different view is not in this assembly, and if such an item also
lands inside the countertop's extent on the page then view membership and geometry **disagree** — which
is the case §4.2 leaves open, and which is answered by refusing. Which signal should win is empirical
and `data/drawings/` is empty.

*Geometric containment* is the projection onto the run axis, not a polygon predicate. This is a
deliberate departure from the obvious `Polygon.contains`: in an elevation a countertop and the cabinets
beneath it share **no** area at all, while in a plan they share **all** of it. Neither answer
distinguishes membership, so neither can be the test. The extent along the run direction means the same
thing in both kinds of view, which is why it is the one used.

*Identifier grouping* can contradict membership here but cannot establish it. The shipped `DrawingItem`
carries a vendor code, a mark or a catalogue number — none of which names an assembly. Deriving an
assembly grouping from identifier text needs real drawings to know what the vendor prints, and
inventing a scheme now would encode today's guess as ground truth. What identifiers *can* do is catch
the same physical cabinet being read twice: two members sharing a unique identifier or a mark is a
contradiction, and it refuses rather than counting that cabinet twice.

## The tolerance is the caller's, and there is no default

What gap between two cabinets is a joint and what gap is a missing cabinet is empirical. So is how far
a countertop may fall short of its run before something is wrong. `edge_tolerance` is required and
keyword-only so that no call site acquires one by accident, and it is in **stored units** — normalised
`0..1` against the page — which is not a distance: the same number is a different physical size on an
A4 sheet and on a 24x36 one. Converting a physical tolerance per page belongs with `PageTransform`.

One number currently covers three different questions — the gap between neighbours, the overlap between
neighbours, and how far the run may fall short of the countertop's ends. Those may well want different
numbers once real drawings exist. Stated here rather than silently assumed.

## What this deliberately does not do

*It does not decide cross-view identity.* The cabinet in elevation D and the cabinet in plan E being one
physical cabinet is B7.3's problem. Items from other views are excluded, never merged.

*It only orders a run that runs across the page.* "Left to right" means the reviewer's left to right, and
the only axis on which that means anything is the page's x. A countertop drawn taller than it is wide —
a plan of a wall running up the sheet — is refused rather than ordered bottom-to-top and called
left-to-right, because A6.4 would then distribute the fillers the wrong way round. Whether that case
needs its own handling is a question for the first real plan view.

*It does not check that a member is vertically below the countertop.* Stored space is normalised and
rotation-applied, and which way its y axis points is not this module's to assume; more to the point, a
plan view has no "below" at all. A second row of cabinets drawn under the same span is caught anyway,
because two rows overlap along the run axis and overlapping members refuse.

*It does not know a countertop from a cabinet by type.* It refuses only the plainly wrong case — being
asked for the assembly beneath a cabinet. The vocabulary describes measurements rather than item kinds,
so a stricter check would be asserting something the vocabulary does not yet say.

Source: `docs/RULE_ENGINE_SPEC.md` §3a; backend proposal Appendix B stage I, §10.1 ·
Design: `docs/DESIGN_EXTRACTION.md` §4.2 · Verification: `tests/extraction/model/test_assembly.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from uuid import UUID

from evidence.polygon import PolygonSpaceMismatchError
from extraction.geometry.containment import Axis, CannotResolve, Interval
from extraction.model.items import DrawingItem, IdentifierKind
from vocabulary.semantic_types import SemanticType

__all__ = [
    "ASSEMBLY_MEMBER_TYPES",
    "Assembly",
    "AssemblyMember",
    "AssemblyResolution",
    "CannotResolve",
    "DrawingContext",
    "resolve_assembly",
]

type AssemblyResolution = Assembly | CannotResolve

#: The run direction. Fixed to the page's x because "left to right" has no other meaning — see the
#: module docstring.
RUN_AXIS: Axis = "horizontal"

#: What may be a member of a countertop's assembly.
#:
#: Taken from `CLIENT_CODES`, which anchors each code to the annotated diagram `CT_image10` rather
#: than to prose: CT002 and CT006 are the left and right fillers, CT003–CT005 the three cabinets
#: underneath, and the two generic names cover a run that is not the client's three-cabinet layout.
#: An item of any other type sitting inside the countertop's extent is not silently skipped — it
#: leaves a gap in the run, and a gap refuses.
ASSEMBLY_MEMBER_TYPES: frozenset[SemanticType] = frozenset(
    {
        SemanticType.CABINET_WIDTH,
        SemanticType.FILLER_WIDTH,
        SemanticType.CT002,
        SemanticType.CT003,
        SemanticType.CT004,
        SemanticType.CT005,
        SemanticType.CT006,
    }
)

#: Identifier kinds that name one physical unit, so two members carrying the same value is a
#: contradiction rather than a coincidence. `CATALOGUE` is deliberately absent: every unit of a model
#: shares its catalogue number, and three identical cabinets in a row is the normal case.
_UNIQUE_IDENTIFIER_KINDS: frozenset[IdentifierKind] = frozenset(
    {IdentifierKind.VENDOR_UNIQUE, IdentifierKind.MARK}
)


@dataclass(frozen=True, slots=True)
class DrawingContext:
    """The items read off one document version — what the resolver is allowed to look at.

    Scoped to a single document version on purpose. Mixing two versions would let a cabinet from a
    superseded sheet join a run drawn on the current one, and every geometric comparison between them
    would be answered confidently and meaninglessly.
    """

    document_version_id: UUID
    items: tuple[DrawingItem, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if not isinstance(self.items, tuple) or any(
            not isinstance(entry, DrawingItem) for entry in self.items
        ):
            raise TypeError("items must be a tuple of DrawingItem values")

        seen: set[UUID] = set()
        for item in self.items:
            if item.view.document_version_id != self.document_version_id:
                raise PolygonSpaceMismatchError(
                    f"item {item.id} belongs to document version "
                    f"{item.view.document_version_id}, not {self.document_version_id}. A context "
                    "spanning two versions would let a superseded cabinet join a current run."
                )
            if item.id in seen:
                raise ValueError(
                    f"item {item.id} appears twice in the context. One item listed twice is one "
                    "cabinet summed twice, and the arithmetic would be exactly wrong."
                )
            seen.add(item.id)


@dataclass(frozen=True, slots=True)
class AssemblyMember:
    """One cabinet or filler in the run, and where it sits in it.

    `position` is left-to-right along the page, counting from zero. It is carried rather than left
    implicit in the tuple order because A6.4 distributes fillers positionally, and a caller that
    re-sorts the run for display would otherwise silently change what the rule expects.
    """

    item: DrawingItem
    position: int

    def __post_init__(self) -> None:
        if not isinstance(self.item, DrawingItem):
            raise TypeError("item must be a DrawingItem")
        if isinstance(self.position, bool) or not isinstance(self.position, int):
            raise TypeError("position must be an integer")
        if self.position < 0:
            raise ValueError("position counts from zero, left to right")


@dataclass(frozen=True, slots=True)
class Assembly:
    """A countertop and the complete run of cabinets and fillers beneath it.

    **Complete** is the whole point: `resolve_assembly` returns this type only when the run reaches
    both ends of the countertop with no gap and no overlap. Everything short of that — including a
    countertop with nothing read beneath it at all — is a `CannotResolve`.

    **That guarantee belongs to the resolver, not to this type, and the difference matters.**
    Coverage is a question about tolerance — how far the run may fall short before something is
    missing — and this type holds no tolerance and has no business holding one. So `__post_init__`
    checks only what it can check without one: the ordering, that no item appears twice, that no
    member came from another view, and that every member has a recorded signal. An `Assembly` built
    by hand is therefore only as good as whoever built it. A rule may sum what `resolve_assembly`
    returns without checking coverage first; it may not assume that of any `Assembly` that turns up
    from somewhere else.

    The earlier version of this docstring claimed the type itself enforced coverage, which was
    false in exactly one case — an empty run took an early return that skipped the end-reach check —
    and that case is now a refusal. The claim is written down this carefully because a type that
    over-promises is worse than one that promises nothing: a caller stops checking.
    """

    countertop: DrawingItem
    run: tuple[AssemblyMember, ...]
    signals: Mapping[UUID, str]
    """Why each member was included, keyed by item id, in language a reviewer can check against the
    drawing. Every member has one and nothing else appears — enforced below, because a signals map
    that quietly went out of step with the run would make the trace worse than useless."""

    def __post_init__(self) -> None:
        if not isinstance(self.countertop, DrawingItem):
            raise TypeError("countertop must be a DrawingItem")
        if not isinstance(self.run, tuple) or any(
            not isinstance(member, AssemblyMember) for member in self.run
        ):
            raise TypeError("run must be a tuple of AssemblyMember values")

        positions = [member.position for member in self.run]
        if positions != list(range(len(self.run))):
            raise ValueError(
                "the run must be ordered left to right with positions 0, 1, 2, … A gap or a "
                "repeat in the positions means a member was dropped or duplicated, and A6.4 "
                "distributes fillers by position."
            )

        ids = [member.item.id for member in self.run]
        if len(set(ids)) != len(ids):
            raise ValueError("the same item appears twice in the run, which would sum it twice")
        if self.countertop.id in ids:
            raise ValueError("the countertop cannot be a member of the run beneath itself")

        foreign = [
            member.item.id for member in self.run if member.item.view != self.countertop.view
        ]
        if foreign:
            raise ValueError(
                f"members {foreign} were found in a different view from the countertop. Recognising "
                "an item across views is B7.3's problem, and merging them here would assemble a run "
                "out of two different drawings."
            )

        if set(self.signals) != set(ids):
            raise ValueError(
                "every member must have a recorded signal and nothing else may. A reviewer cannot "
                "check an inclusion that gives no reason for itself."
            )
        for value in self.signals.values():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("a signal must say, in plain English, why the member was included")

        object.__setattr__(self, "signals", MappingProxyType(dict(self.signals)))


def resolve_assembly(
    countertop: DrawingItem,
    ctx: DrawingContext,
    *,
    edge_tolerance: Decimal,
) -> AssemblyResolution:
    """Find the cabinets and fillers beneath `countertop`, or refuse.

    Returns a complete, ordered `Assembly`, or a `CannotResolve` carrying the reason and the
    candidates it was choosing between — which is the mark for human confirmation. It never returns
    a run that is missing a member.

    `edge_tolerance` is in stored units (the normalised `0..1` page space) and is required. See the
    module docstring for why it has no default and why one number currently covers three questions.

    Raises `PolygonSpaceMismatchError` if the countertop belongs to a different document version from
    the context, and `ValueError` if it is itself a cabinet or filler. Neither is ambiguity to mark
    for review; both are caller mistakes, and answering them would mean assembling a run for
    something that has none.
    """
    _check_tolerance(edge_tolerance)

    if countertop.view.document_version_id != ctx.document_version_id:
        raise PolygonSpaceMismatchError(
            f"the countertop belongs to document version {countertop.view.document_version_id} and "
            f"the context to {ctx.document_version_id}. The run would be assembled out of two "
            "different documents."
        )
    if countertop.item_type in ASSEMBLY_MEMBER_TYPES:
        raise ValueError(
            f"asked for the assembly beneath a {countertop.item_type.value}, which is itself a "
            "cabinet or filler. Nothing sits beneath it, and answering would produce a run for "
            "something that is not a countertop."
        )

    span = _project(countertop, RUN_AXIS)
    if not _runs_across_the_page(countertop):
        return CannotResolve(
            "the countertop is not wider than it is tall on the page, so there is no left-to-right "
            "run to order. Ordering it up the page instead and calling that left-to-right would "
            "hand A6.4 the fillers the wrong way round.",
            (countertop,),
        )

    beneath = (
        (item, _project(item, RUN_AXIS))
        for item in ctx.items
        if item.id != countertop.id
        and item.view.page == countertop.view.page
        and item.item_type in ASSEMBLY_MEMBER_TYPES
    )
    candidates = [
        (item, interval) for item, interval in beneath if _within(interval, span, edge_tolerance)
    ]

    other_views = [item for item, _ in candidates if item.view != countertop.view]
    if other_views:
        return CannotResolve(
            f"{len(other_views)} cabinet or filler on this page lies inside the countertop's span "
            f"but belongs to view {_views_named(other_views)} rather than view "
            f"{countertop.view.tag}. Two elevations share a sheet, so view membership and geometry "
            "disagree here, and which one wins is a question only real drawings can answer.",
            tuple(item for item, _ in candidates),
        )

    if not candidates:
        # A refusal, not an empty `Assembly`. This was the one path that reached `Assembly` without
        # passing the end-reach check below, which made the type's central claim false: a caller
        # told a run may be summed without checking coverage would have summed nothing to zero
        # believing it was complete. A countertop whose cabinets were all missed is the largest
        # possible version of a missing member, not an exemption from it (§4.2).
        #
        # The distinction worth keeping is kept in the wording and in the empty candidate list:
        # "nothing was found" sends a reviewer to the extraction, "what was found does not add up"
        # sends them to the drawing. That is a difference between two refusals, which is safe,
        # rather than a difference between a refusal and a summable answer, which was not.
        return CannotResolve(
            "nothing of cabinet or filler type was read beneath this countertop, so there is no "
            "run to check — most likely the cabinets have not been extracted from this view yet. "
            "This is not an empty assembly a rule may sum: a countertop with every cabinet missing "
            "is the largest possible version of a missing member, not a special case exempt from "
            "it.",
            (),
        )

    ordered = sorted(candidates, key=lambda pair: (pair[1][0], pair[1][1]))
    members = [item for item, _ in ordered]
    spans = [interval for _, interval in ordered]

    duplicate = _duplicate_identifier(members)
    if duplicate is not None:
        kind, value = duplicate
        return CannotResolve(
            f"two members are both printed {kind.value} {value!r}, which names one physical unit. "
            "Either the same cabinet was read twice or one of the identifiers was misread, and "
            "summing both would count that cabinet twice.",
            tuple(members),
        )

    # Signed, not absolute: `abs(...)` would report an overlap as a gap and send a reviewer looking
    # for a cabinet that was never missing. Two rows of cabinets drawn under one countertop land
    # here, which is how the missing "is it vertically below?" test is covered.
    for index in range(len(ordered) - 1):
        if spans[index][1] - spans[index + 1][0] > edge_tolerance:
            return CannotResolve(
                f"{_describe(members[index], spans[index])} and "
                f"{_describe(members[index + 1], spans[index + 1])} overlap along the run rather "
                "than sitting side by side, so they are not one row of cabinets and summing them "
                "would count the shared part twice",
                tuple(members),
            )

    for index in range(len(ordered) - 1):
        if spans[index + 1][0] - spans[index][1] > edge_tolerance:
            return CannotResolve(
                f"there is a gap between {_describe(members[index], spans[index])} and "
                f"{_describe(members[index + 1], spans[index + 1])} wider than the stated "
                "tolerance, so something between them is missing or was not read. A run with a "
                "hole in it summed as if it were whole is short by whatever fills the hole.",
                tuple(members),
            )

    reached: Interval = (spans[0][0], spans[-1][1])
    if abs(reached[0] - span[0]) > edge_tolerance or abs(reached[1] - span[1]) > edge_tolerance:
        return CannotResolve(
            f"the run found beneath the countertop covers {reached[0]} to {reached[1]}, but the "
            f"countertop spans {span[0]} to {span[1]}. It does not reach both ends, so a member is "
            "missing — and a run one cabinet short is not a shorter assembly, it is this assembly "
            "with a cabinet dropped.",
            tuple(members),
        )

    return Assembly(
        countertop=countertop,
        run=tuple(AssemblyMember(item=item, position=index) for index, item in enumerate(members)),
        signals={
            item.id: _signal(item, interval, countertop, span)
            for item, interval in zip(members, spans, strict=True)
        },
    )


def _check_tolerance(tolerance: Decimal) -> None:
    if not isinstance(tolerance, Decimal):
        raise TypeError(
            "edge_tolerance must be a Decimal. A float tolerance would let binary rounding decide "
            "whether a joint is a joint or a missing cabinet."
        )
    if not tolerance.is_finite():
        # NaN would pass every check below it — `Decimal("NaN") < 0` is False, and so is every
        # `... > NaN` afterwards — so a run with a cabinet missing would sail through the gap, the
        # overlap and the reach checks and be returned as complete. That is the exact false PASS
        # this module exists to prevent, arriving in the costume of a clean answer. Infinity is the
        # mirror image: every neighbouring pair looks adjacent, however far apart.
        raise ValueError(
            "edge_tolerance must be a finite number. A NaN or infinite tolerance does not widen or "
            "narrow the checks, it removes them — a gap, an overlap and a run that falls short all "
            "answer the same way, and the resolver returns a partial run as if it were complete."
        )
    if tolerance < 0:
        raise ValueError("edge_tolerance cannot be negative")


def _project(item: DrawingItem, axis: Axis) -> Interval:
    """The item's extent along `axis`, low value first.

    The same projection `extraction/geometry/containment.py` uses, for the same reason: a polygon
    predicate answers whether two shapes share area, and membership of a run is a question about
    extent along one direction.
    """
    values = [point.x if axis == "horizontal" else point.y for point in item.extent.points]
    return (min(values), max(values))


def _runs_across_the_page(countertop: DrawingItem) -> bool:
    """Whether the countertop is wider than it is tall, so a left-to-right order means something."""
    horizontal = _project(countertop, "horizontal")
    vertical = _project(countertop, "vertical")
    return horizontal[1] - horizontal[0] > vertical[1] - vertical[0]


def _within(span: Interval, outer: Interval, tolerance: Decimal) -> bool:
    return span[0] >= outer[0] - tolerance and span[1] <= outer[1] + tolerance


def _views_named(items: list[DrawingItem]) -> str:
    return ", ".join(sorted({item.view.tag for item in items}))


def _describe(item: DrawingItem, span: Interval) -> str:
    """An item a reviewer can pick out of the drawing.

    The type alone will not do it: a run of three base cabinets is three `CT003`s, and "the gap
    between the CT003 and the CT003" names no particular pair. Where it sits along the run does.
    """
    return f"the {item.item_type.value} from {span[0]} to {span[1]}"


def _duplicate_identifier(members: list[DrawingItem]) -> tuple[IdentifierKind, str] | None:
    """The first identifier that names one physical unit yet appears on two members, if any."""
    seen: set[tuple[IdentifierKind, str]] = set()
    for item in members:
        for identifier in item.identifiers:
            if identifier.kind not in _UNIQUE_IDENTIFIER_KINDS:
                continue
            key = (identifier.kind, identifier.value_as_printed)
            if key in seen:
                return key
            seen.add(key)
    return None


def _signal(
    item: DrawingItem, span: Interval, countertop: DrawingItem, countertop_span: Interval
) -> str:
    """Why this member was included, written for the person checking it against the sheet."""
    printed = ", ".join(
        f"{identifier.kind.value} {identifier.value_as_printed!r}"
        for identifier in item.identifiers
    )
    marked = f", printed {printed}" if printed else ", with nothing printed on it"
    return (
        f"the {item.item_type.value}{marked}, is in view {countertop.view.tag} on page "
        f"{item.view.page + 1} with the countertop, and its extent across the page "
        f"({span[0]} to {span[1]}) lies inside the countertop's ({countertop_span[0]} to "
        f"{countertop_span[1]}). It is one of an unbroken row that reaches both ends of the "
        "countertop"
    )
