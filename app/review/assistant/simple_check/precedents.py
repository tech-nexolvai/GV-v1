"""The "looks wrong but isn't" precedents the compare call and the blind look are given (#1775,
recipe step 6; Anthropic's security-review filter carries the same kind of list).

GV's eval kit holds 78 known false or risky reds. Their patterns, in generic words (no drawing
numbers, no client values), are written here as precedents: settled cases where two dimensions or
an item look like a difference but are not the same thing. Each is worded as "these are not the
same points" (or "this is not the vendor's"), never as an instruction to leave something out
("never report X", "ignore X", "is a GV note", "not a vendor error"), so a real difference between
the same two points is still found (a precedent too broad would hide a true red). The one thing
never to report is a sum the model made itself (P6): every printed number is still compared.

The list is pluggable: `precedents(products)` returns the general ones plus each in-scope
product's, and a product pack can add its own through `add_source` (Builder D's product packs:
start/end words and examples per product). Code renders them in a fixed order, so the system
prompt is the same bytes for a run (it stays cached).

Pure data: no model, no I/O.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["Precedent", "add_source", "precedents", "render"]


@dataclass(frozen=True, slots=True)
class Precedent:
    """One settled "looks wrong but isn't" case."""

    id: str
    rule: str
    """One line, in generic words: what is not the same as what."""
    trap: str
    """The kind of known false red it comes from (the eval kit's fixture group)."""
    products: tuple[str, ...] = ()
    """Registry product ids it applies to; empty: every product."""


_GENERAL: Final[tuple[Precedent, ...]] = (
    Precedent(
        "P1",
        "A run to another trade's work (a wall finish or covering by another trade, a bulkhead, "
        "box or soffit above the unit, the ceiling) is not the unit's own run, even when both "
        "start at the floor or the same wall. Millwork wall panels are millwork: compare them.",
        "different points: another trade's end",
    ),
    Precedent(
        "P2",
        "An overall is not one of its parts: wall to wall is not wall to a centre line, and a "
        "whole run is not one unit or one gap in it.",
        "different points: overall against a part",
    ),
    Precedent(
        "P3",
        "A dimension that starts or ends at a band (a splash, a base, a trim) is not the one that "
        "starts or ends at the surface on the other side of that band: they differ by the band.",
        "different points: a band between the ends",
    ),
    Precedent(
        "P4",
        "Room features are not millwork: ceilings, soffits, door heads, artwork, light switches, "
        "columns, light fixtures and wall outlets outside the unit. A room feature absent from "
        "the vendor's drawing is not missing millwork; anything that is millwork is still "
        "compared.",
        "room context absent on the vendor's drawing",
    ),
    Precedent(
        "P5",
        "GV's own notes and numbers (review markup) are never the design value. A brand "
        "prototype is reference only, not the architect's dimension, except where code's text "
        "says the architect gives no elevation of the area: there the brand prototype is the "
        "reference, and you compare the vendor with it.",
        "GV's markup or the prototype used as the design",
    ),
    Precedent(
        "P6",
        "A sum you make yourself from parts (a string that seems not to add up) is not a printed "
        "dimension: a part may be cut off the picture or belong to the next string. Code adds up "
        "strings: never report a sum you made as a value. Every printed number, with or without "
        "a tag, is still compared.",
        "AI-made strings",
    ),
    Precedent(
        "P7",
        "A depth on a plan is not a height on an elevation. A width on a plan and a width on an "
        "elevation are the same thing when both ends are the same edges, and are compared then.",
        "different views",
    ),
)

_BY_PRODUCT: Final[tuple[Precedent, ...]] = (
    Precedent(
        "C1",
        "To the underside of a top is not to its top: they differ by the top's thickness.",
        "different points: underside against top",
        ("countertop", "cabinet"),
    ),
    Precedent(
        "C2",
        "A height to the top of a splash or backsplash is not a height to the top of the counter.",
        "different points: splash against counter",
        ("countertop", "cabinet"),
    ),
    Precedent(
        "C3",
        "A filler width outside GV's usual range is not by itself a difference from the "
        "architect (code applies GV's filler rule); where the architect prints that filler's own "
        "size, compare the two.",
        "GV's filler rule as a red",
        ("cabinet",),
    ),
)

_SOURCES: list[Callable[[Sequence[str]], Iterable[Precedent]]] = []
_LOCK: Final = threading.Lock()


def add_source(source: Callable[[Sequence[str]], Iterable[Precedent]]) -> None:
    """Add a source of precedents (a product pack's): called with the in-scope product ids."""
    with _LOCK:
        if source not in _SOURCES:
            _SOURCES.append(source)


def precedents(products: Sequence[str] = ()) -> tuple[Precedent, ...]:
    """The general precedents, then each in-scope product's (every product's when `products` is
    empty: a whole-set check), then the added sources', in a fixed order, each id once."""
    wanted = set(products)
    found = list(_GENERAL)
    found += [p for p in _BY_PRODUCT if not wanted or wanted & set(p.products)]
    with _LOCK:
        sources = list(_SOURCES)
    for source in sources:
        found += list(source(tuple(products)))
    seen: set[str] = set()
    out: list[Precedent] = []
    for one in found:
        if one.id not in seen:
            seen.add(one.id)
            out.append(one)
    return tuple(out)


def render(products: Sequence[str] = (), *, leave_out: Iterable[str] = ()) -> str:
    """The precedents as the prompts show them: a heading and one numbered line each (those in
    `leave_out` left out: P6 for a blind look asked to pick a string of parts)."""
    skip = set(leave_out)
    lines = [f"{p.id}. {p.rule}" for p in precedents(products) if p.id not in skip]
    return (
        "Looks wrong but isn't (settled cases from GV's known false alarms; each says what is "
        "not the same thing, never that a real difference between the same two points should be "
        "left out):\n" + "\n".join(lines)
    )
