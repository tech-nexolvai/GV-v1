"""Hints that help find a region on a busy drawing, and never help decide anything.

`CALL_2026_08_25_INPUTS` N1. Raj suggested vendors outline the sink cut-out in a distinct colour —
blue — so the system can find it on a crowded countertop plan. That is genuinely useful: the cut-out
is the hardest region to locate on the sheet, and a colour convention turns a search problem into a
mask.

Three things follow, and all three are the point of this module.

**A locator is not evidence.** Colour says *look here*; it never says *this is 32 1/2 inches*. A
value must still be read from a dimension and qualified the ordinary way — `EvidenceStatus` decides
what may enter a verdict, and nothing here produces one. A blue outline being present is not a reason
to trust the number inside it, and a blue outline being absent is not a reason to doubt one.

**The extractor must work without it.** It is *"a convention we can request, not something the current
drawings guarantee"*. Every vendor who has not adopted it draws the cut-out in black like everything
else, so a pipeline that needs the hint is a pipeline that fails on most packages. `LocatorHint` is
always optional and its absence is never an error.

**A hint that is wrong must cost nothing.** A vendor who uses blue for something else — a revision
cloud, a hatch — produces a hint pointing at the wrong region. Because the value still has to be read
and qualified from that region, the failure mode is a search that finds nothing, which is a
`NOT_FOUND` a reviewer sees. That is the whole reason for keeping colour on this side of the line.

Nothing here reads pixels. Detecting blue in a rendered page needs real drawings to calibrate
against, and there are none yet (#274); `AGENTS.md` §9 — *"a fixture invented today encodes today's
guess as ground truth"* — rules out inventing a threshold. This is the shape a detector reports
through, and the guarantee that whatever it reports cannot reach a verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "LocatorHint",
    "LocatorSource",
    "prefer",
]


class LocatorSource(StrEnum):
    """How a region was suggested.

    A closed list because each member is a claim about reliability, and an open one would let a
    future detector describe itself in terms nobody weighed.
    """

    COLOUR = "colour"
    """A colour convention, e.g. the blue sink cut-out outline (N1).

    The weakest of the three and the only one that depends on a vendor following a request. It is
    listed first because it is the one somebody will be tempted to trust.
    """

    LABEL = "label"
    """Text on the drawing naming the region — a tag, a callout, a note."""

    GEOMETRY = "geometry"
    """The shape itself: a closed contour of about the right size in about the right place."""


@dataclass(frozen=True, slots=True)
class LocatorHint:
    """A suggestion about where to look, with no authority over what is found there.

    Deliberately carries no value, no unit and no evidence status. There is nowhere in this object to
    put a dimension, which is the simplest way to guarantee a locator cannot become one — a reviewer
    reading the type sees immediately that colour cannot have decided anything, and a future author
    who wants it to has to change the shape rather than pass a field through.
    """

    source: LocatorSource
    page: int
    region: tuple[int, int, int, int]
    """`(x0, y0, x1, y1)` in page coordinates. Where to look, not what is there."""

    note: str = ""
    """Free text for a reviewer — "blue outline, 4 sides" — never parsed by anything."""

    def __post_init__(self) -> None:
        if not isinstance(self.source, LocatorSource):
            raise TypeError("source must be a LocatorSource")
        x0, y0, x1, y1 = self.region
        if x1 <= x0 or y1 <= y0:
            raise ValueError(
                f"region {self.region!r} is empty or inverted. A hint pointing at nothing is a bug "
                "in the detector, not a region containing nothing."
            )


def prefer(hints: tuple[LocatorHint, ...]) -> tuple[LocatorHint, ...]:
    """Order hints by how much a search should lean on them: geometry, then label, then colour.

    Colour is ordered **last**, which is the opposite of how tempting it is. It is the only source
    that depends on a vendor having adopted a convention nobody has agreed to yet, so a search that
    tried it first would work beautifully on the drawings that follow it and quietly worse on the
    rest — the failure that looks like success during a demo.

    Ordering, not filtering. Every hint is returned: a colour hint is still worth trying when nothing
    else suggested a region, and dropping it would lose the case N1 was raised for.
    """
    rank = {LocatorSource.GEOMETRY: 0, LocatorSource.LABEL: 1, LocatorSource.COLOUR: 2}
    return tuple(sorted(hints, key=lambda hint: (rank[hint.source], hint.page, hint.region)))
