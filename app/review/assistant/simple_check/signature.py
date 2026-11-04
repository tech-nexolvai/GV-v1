"""The page signature of one drawing: what code reads to tell look-alike drawings apart. $0.

Rebuild plan Step 3 (2026-11-05). On a real hotel set the AI page matcher was shown 42 whole
architect sheets as small pictures with no text, and picked the same sheet for 27 of 39 vendor
pages: a sheet held a standard and an accessible kitchen, or a studio's kitchen and a one-bedroom's,
so the right sheet was often the wrong drawing. Pairing now ranks the architect's drawings (each
titled view of a sheet) one by one, and this signature, read from what the pack already holds
(no new pack file), blocks and re-ranks them before the agreement rule and before the matcher is
asked:

- **room words** (`room_words`): the room or piece a title names, from a fixed list of plain room
  words ("KITCHENETTE" is a kitchen), so a title block's brand words never count;
- **the unit type** (`unit_type`): studio, one-bedroom, two-bedroom ("1 BDRM", "ONE BEDROOM",
  "K1BR"). Two different unit types rule a pair out; a unit type on one side only lowers the
  rank a little. A vendor's own type code that the architect never prints ("TYPE 2") takes the
  unit type its other sheets print beside it (`unit_aliases`);
- **the accessible marker**: code's own words (`pairing.markers_in`: ADA, ACCESSIBLE, ACC) and the
  set index's checked variant words (`HC` where the set's model read it so);
- **sheet and callout tags**: the sheet numbers and view references the drawing prints
  (`pairing.references_in`) and its item tags;
- **the fixtures from left to right** (`fixture_order`: sink, cooktop, dishwasher, refrigerator,
  microwave), compared both ways (`order_match`): read backwards, the unit is drawn opposite-hand;
- **the top three overall widths** (`top_widths`): a soft signal only. Vendor and architect sizes
  differ by design on true pairs (that is what the check finds), so widths add less than the
  agreement rule's weak lead (`pairing.WEAK_LEAD`): sizes alone never decide a pair.

Its weight is kept apart in the score (`pairing.Scored.signed`), so it re-ranks and never counts
toward code's own pick. Pure: plain values in and out.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from typing import Final

from app.review.assistant.simple_check import pairing

__all__ = [
    "MAX_ROOM",
    "MAX_WIDTHS",
    "ROOM_FAMILIES",
    "ROOM_WORDS",
    "W_KIND",
    "W_ORDER",
    "W_ROOM",
    "W_ROOM_OTHER",
    "W_SHEET",
    "W_UNIT",
    "W_UNIT_ONE_SIDE",
    "W_WIDTH",
    "Signature",
    "SignatureMatch",
    "fixture_order",
    "match",
    "mirrored",
    "order_match",
    "rerank",
    "room_words",
    "signature_of",
    "top_widths",
    "unit_aliases",
    "unit_type",
]

#: Plain room and piece words a title may name, each to the word it counts as. Only these count:
#: a title block's brand or project words never tell two drawings of one set apart.
ROOM_WORDS: Final[Mapping[str, str]] = {
    "KITCHEN": "KITCHEN",
    "KITCHENETTE": "KITCHEN",
    "KITCHENET": "KITCHEN",
    "VANITY": "VANITY",
    "VANITIES": "VANITY",
    "BATH": "BATH",
    "BATHROOM": "BATH",
    "CLOSET": "CLOSET",
    "WARDROBE": "WARDROBE",
    "PANTRY": "PANTRY",
    "LAUNDRY": "LAUNDRY",
    "LOBBY": "LOBBY",
    "RECEPTION": "RECEPTION",
    "REGISTRATION": "RECEPTION",
    "DESK": "DESK",
    "BAR": "BAR",
    "BREAKFAST": "BREAKFAST",
    "BUFFET": "BUFFET",
    "BENCH": "BENCH",
    "SHELF": "SHELF",
    "SHELVING": "SHELF",
    "DRAPERY": "DRAPERY",
    "MEDIA": "MEDIA",
    "FITNESS": "FITNESS",
    "BOARDROOM": "BOARDROOM",
    "CONFERENCE": "CONFERENCE",
    "LOUNGE": "LOUNGE",
    "OFFICE": "OFFICE",
    "MARKET": "MARKET",
    "COFFEE": "COFFEE",
    "SUITE": "SUITE",
    "GUESTROOM": "GUESTROOM",
    "KING": "KING",
    "QUEEN": "QUEEN",
    "ELEVATOR": "ELEVATOR",
    "STAIR": "STAIR",
    "CORRIDOR": "CORRIDOR",
    "PATIO": "PATIO",
}
#: Room words that name pieces of one room (a vanity's drawing is a bathroom's elevation; a
#: kitchen's shelf, a breakfast area's coffee bar): never "another room" to each other (`match`).
ROOM_FAMILIES: Final = (
    frozenset({"VANITY", "BATH"}),
    frozenset({"KITCHEN", "PANTRY", "SHELF"}),
    frozenset({"BREAKFAST", "BUFFET", "COFFEE", "MARKET", "PANTRY", "BAR"}),
    frozenset({"LOBBY", "RECEPTION", "DESK"}),
    frozenset({"GUESTROOM", "KING", "QUEEN", "SUITE", "CLOSET", "WARDROBE", "DRAPERY", "MEDIA"}),
)
_WORD: Final = re.compile(r"[A-Z]+")
#: A unit type: studio, or a number of bedrooms ("1 BDRM", "ONE BEDROOM", "2BR", "K1BR").
_STUDIO: Final = re.compile(r"\bSTUDIO\b")
_BEDROOMS: Final = re.compile(
    r"(?:\b|(?<=K))(ONE|TWO|THREE|[1-3])\s*-?\s*(?:BDRMS?|BEDROOMS?|BED|BR)\b"
)
_NUMBER: Final = {"ONE": "1", "TWO": "2", "THREE": "3"}
#: A vendor's own type code ("TYPE-2", "Type 4 (Left)", "type-1Left").
_TYPE_CODE: Final = re.compile(r"\bTYPE\s*-?\s*(\d{1,2}(?!\d)|[A-Z](?![A-Z]))")
#: A fixture's words, each to the fixture it names (`fixture_order`).
_FIXTURES: Final = (
    ("SINK", re.compile(r"\bSINK\b|\bSNK\b")),
    ("RANGE", re.compile(r"\bCOOK\s*TOP\b|\bRANGE\b|\bSTOVE\b|\bOVEN\b")),
    ("DW", re.compile(r"\bDISH\s*WASHER\b|\bD\.?W\b")),
    ("REF", re.compile(r"\bREFRIG(?:ERATOR)?\b|\bFRIDGE\b|\bREF\b\.?(?!\s*(?:ELEV|SHEET|DWG)\b)")),
    ("MW", re.compile(r"\bMICRO(?:WAVE)?\b|\bM\.?W\b")),
)
#: A fixture is a short label: a phrase of at most this many words (a note naming a sink is no
#: sink).
_FIXTURE_WORDS: Final = 4
#: A phrase naming a drawing or a note, never a fixture standing where it is drawn.
_NOT_FIXTURE: Final = re.compile(r"\b(?:SEE|ELEV|ELEVATION|PLAN|SECTION|DETAIL|NOTE|CUTOUT|TYP)\b")

#: Weights (`match`): each room word both name, at most `MAX_ROOM`; room words on both sides,
#: none shared nor of one room (`ROOM_FAMILIES`; a penalty: a vanity's drawing for a kitchen's); the same unit type; a unit type on
#: one side only (a penalty); the vendor printing the drawing's sheet number; a kind of view both
#: draw (an elevation for the vendor's sheet of elevations); the fixtures in order (either way);
#: each overall width close on both, at most `MAX_WIDTHS`.
W_ROOM: Final = 1.0
MAX_ROOM: Final = 1.5
W_ROOM_OTHER: Final = -1.5
W_UNIT: Final = 1.0
W_UNIT_ONE_SIDE: Final = -0.5
W_SHEET: Final = 0.5
W_KIND: Final = 0.5
W_ORDER: Final = 0.5
W_WIDTH: Final = 0.125
#: Below `pairing.WEAK_LEAD`: widths alone never give the lead the agreement rule needs.
MAX_WIDTHS: Final = 0.25
#: How many of a drawing's overall widths count (`top_widths`).
TOP_WIDTHS: Final = 3
#: The fixtures must agree on at least this many in order (`order_match`).
MIN_ORDER: Final = 2


@dataclass(frozen=True)
class Signature:
    """One drawing's signature (see the module)."""

    ref: str
    rooms: frozenset[str] = frozenset()
    unit: str | None = None
    """`STUDIO`, `1-BEDROOM`, `2-BEDROOM`..., or `None` when its titles name none (or several)."""
    accessible: bool = False
    sheets: frozenset[str] = frozenset()
    """The sheet numbers and view references it prints, normalised (`pairing.references_in`)."""
    sheet: str | None = None
    """Its own sheet's number, normalised (an architect's drawing: its title block's)."""
    tags: frozenset[str] = frozenset()
    """Its item tags, normalised (`pairing.item_tags_in`)."""
    order: tuple[str, ...] = ()
    widths: tuple[Fraction, ...] = ()
    kinds: frozenset[str] = frozenset()
    """The kinds of view its titles name (`pairing.views_in`: plan, elevation, section)."""


def _upper(texts: Iterable[str]) -> list[str]:
    return [" ".join(str(t or "").upper().split()) for t in texts]


def room_words(texts: Iterable[str]) -> frozenset[str]:
    """The room and piece words `texts` name (`ROOM_WORDS`, a plural's S dropped)."""
    found: set[str] = set()
    for text in _upper(texts):
        for word in _WORD.findall(text):
            if word not in ROOM_WORDS and len(word) > 4 and word.endswith("S"):
                word = word[:-1]
            if word in ROOM_WORDS:
                found.add(ROOM_WORDS[word])
    return frozenset(found)


def unit_type(texts: Iterable[str]) -> str | None:
    """The one unit type `texts` name (`STUDIO`, `1-BEDROOM`), or `None` (none, or several)."""
    found: set[str] = set()
    for text in _upper(texts):
        if _STUDIO.search(text):
            found.add("STUDIO")
        for number in _BEDROOMS.findall(text):
            found.add(f"{_NUMBER.get(number, number)}-BEDROOM")
    return next(iter(found)) if len(found) == 1 else None


def _type_code(text: str) -> str | None:
    found = {m.group(1) for m in _TYPE_CODE.finditer(" ".join(text.upper().split()))}
    return f"TYPE {next(iter(found))}" if len(found) == 1 else None


def unit_aliases(titles: Mapping[str, str]) -> dict[str, str]:
    """The unit type of the vendor's drawings whose title gives only the vendor's own type code
    (`titles`: drawing ref -> its item title). A code ("TYPE 2") printed beside one unit type on
    the vendor's other titles of the same room ("TYPE 2 ... KITCHENETTE K 1BR"), and never beside
    another, is that unit type: the architect never prints the vendor's codes. A title naming no
    room (a model number and the code) takes the unit type the code is printed beside on any of
    the vendor's titles, when it is only one. Drawing ref -> unit type, for the titles naming the
    code and no unit type."""
    seen: dict[tuple[frozenset[str], str], set[str]] = {}
    by_code: dict[str, set[str]] = {}
    for title in titles.values():
        code = _type_code(title)
        unit = unit_type([title])
        if code is not None and unit is not None:
            seen.setdefault((room_words([title]), code), set()).add(unit)
            by_code.setdefault(code, set()).add(unit)
    out: dict[str, str] = {}
    for ref, title in titles.items():
        code = _type_code(title)
        if code is None or unit_type([title]) is not None:
            continue
        rooms = room_words([title])
        # The same room's titles first; a title naming no room (a model number and the code)
        # takes the code's one unit type across the vendor's titles.
        units = seen.get((rooms, code)) or (by_code.get(code, set()) if not rooms else set())
        if len(units) == 1:
            out[ref] = next(iter(units))
    return out


def fixture_order(phrases: Iterable[tuple[str, Sequence[float]]]) -> tuple[str, ...]:
    """The fixtures a drawing labels, from left to right (each label's middle), each once in a row:
    a short label (at most `_FIXTURE_WORDS` words) naming a sink, a cooktop or range, a
    dishwasher, a refrigerator or a microwave; a note or a drawing's title is no fixture."""
    found: list[tuple[float, str]] = []
    for text, box in phrases:
        upper = " ".join(str(text or "").upper().split())
        if not upper or len(upper.split()) > _FIXTURE_WORDS or _NOT_FIXTURE.search(upper):
            continue
        for name, pattern in _FIXTURES:
            if pattern.search(upper):
                found.append(((float(box[0]) + float(box[2])) / 2, name))
                break
    out: list[str] = []
    for _x, name in sorted(found):
        if not out or out[-1] != name:
            out.append(name)
    return tuple(out)


def _common(one: Sequence[str], other: Sequence[str]) -> int:
    """The longest run of `one`'s fixtures `other` holds in the same order (gaps allowed)."""
    rows = [[0] * (len(other) + 1) for _ in range(len(one) + 1)]
    for i, a in enumerate(one):
        for j, b in enumerate(other):
            rows[i + 1][j + 1] = rows[i][j] + 1 if a == b else max(rows[i][j + 1], rows[i + 1][j])
    return rows[-1][-1]


def order_match(mine: Sequence[str], theirs: Sequence[str]) -> tuple[int, int]:
    """How many fixtures two drawings show in the same order, read forwards and read backwards
    (the other drawing mirrored): fuzzy, a fixture one drawing leaves out is skipped."""
    return _common(mine, theirs), _common(mine, tuple(reversed(theirs)))


def top_widths(oriented: Iterable[tuple[str, Fraction]]) -> tuple[Fraction, ...]:
    """A drawing's top three overall widths (across values of 24" or more), largest first."""
    found = sorted(
        {v for axis, v in oriented if axis == "h" and v >= pairing.MIN_TELLING_INCHES},
        reverse=True,
    )
    return tuple(found[:TOP_WIDTHS])


def signature_of(
    facts: pairing.Facts,
    titles: Iterable[str],
    phrases: Iterable[tuple[str, Sequence[float]]] = (),
    *,
    unit: str | None = None,
    sheets: Iterable[str] = (),
    views: Iterable[str] = (),
) -> Signature:
    """A drawing's signature from its pairing facts (markers, references, tags, sizes), its
    titles (the room words and unit type are read from these only: a whole page's printed words
    hold its title block's), its printed phrases with their boxes (the fixtures), `unit` when
    the titles name none (`unit_aliases`), its sheet's own number (`sheets`, its first) and the
    titles of its views (`views`: the kinds of view it draws, with its own titles')."""
    titled = list(titles)
    return Signature(
        ref=facts.ref,
        rooms=room_words(titled),
        unit=unit_type(titled) or unit,
        accessible=bool(facts.markers),
        sheets=facts.references,
        sheet=next(iter(sorted(pairing.references_in(sheets))), None),
        tags=facts.item_tags if facts.item_tags is not None else facts.tags,
        order=fixture_order(phrases),
        widths=top_widths(facts.oriented),
        kinds=pairing.views_in([*titled, *views]),
    )


@dataclass(frozen=True)
class SignatureMatch:
    """How a target's signature matches the vendor drawing's (`match`)."""

    bonus: float = 0.0
    evidence: tuple[str, ...] = ()
    contradicts: str | None = None
    mirrored: bool = False
    """The fixtures read in reverse order only: the unit is drawn opposite-hand."""


def _close(one: Fraction, other: Fraction) -> bool:
    """Two widths close enough to count as one (`pairing.SIZE_TOLERANCE`, or 1")."""
    big = max(one, other)
    return abs(one - other) <= max(
        Fraction(1), big * Fraction(pairing.SIZE_TOLERANCE).limit_denominator(100)
    )


def _related(one: frozenset[str], other: frozenset[str]) -> bool:
    """Whether two sets of room words share a word, or a room family (`ROOM_FAMILIES`)."""
    return bool(one & other) or any(one & family and other & family for family in ROOM_FAMILIES)


def _words(unit: str) -> str:
    return "studio" if unit == "STUDIO" else unit.lower().replace("-", " ") + " unit"


def match(
    vendor: Signature, target: Signature, room_counts: Mapping[str, int], total: int
) -> SignatureMatch:
    """The vendor drawing's signature against one target's: its weight, the evidence in words,
    a rule-out (another unit type; an accessible marker on one side only), whether the fixtures
    read backwards. `room_counts`: how many targets name each room word, of `total` (a word every
    target names tells nothing)."""
    bonus = 0.0
    evidence: list[str] = []
    contradicts: str | None = None
    if vendor.accessible != target.accessible:
        contradicts = (
            "the vendor's drawing is marked accessible and this drawing is not"
            if vendor.accessible
            else "this drawing is marked accessible and the vendor's is not"
        )
    if vendor.unit and target.unit and vendor.unit != target.unit:
        contradicts = contradicts or (
            f"another unit type: the vendor's drawing is a {_words(vendor.unit)}, this drawing "
            f"a {_words(target.unit)}"
        )
    elif vendor.unit and vendor.unit == target.unit:
        bonus += W_UNIT
        evidence.append(f"both draw a {_words(vendor.unit)}")
    elif vendor.unit or target.unit:
        bonus += W_UNIT_ONE_SIDE
        named = vendor.unit or target.unit or ""
        evidence.append(
            f"only {'the vendor' if vendor.unit else 'this drawing'} names a unit type "
            f"({_words(named)})"
        )
    shared = sorted(
        w for w in vendor.rooms & target.rooms if total <= 1 or room_counts.get(w, 0) < total
    )
    if shared:
        bonus += min(MAX_ROOM, W_ROOM * len(shared))
        evidence.append("both name " + ", ".join(w.lower() for w in shared))
    elif vendor.rooms and target.rooms and not _related(vendor.rooms, target.rooms):
        bonus += W_ROOM_OTHER
        evidence.append(
            "it names another room or piece ("
            + ", ".join(w.lower() for w in sorted(target.rooms))
            + " against the vendor's "
            + ", ".join(w.lower() for w in sorted(vendor.rooms))
            + ")"
        )
    if target.sheet is not None and target.sheet in vendor.sheets:
        bonus += W_SHEET
        evidence.append(f"the vendor prints its sheet number {target.sheet}")
    kinds = sorted(vendor.kinds & target.kinds)
    if kinds and len(vendor.kinds) == 1:
        # Only when the vendor's sheet draws one kind of view: a sheet of a plan and an elevation
        # says nothing of which of the architect's views it is.
        bonus += W_KIND
        evidence.append(f"both draw a {kinds[0]}")
    mirrored = False
    if len(vendor.order) >= MIN_ORDER and len(target.order) >= MIN_ORDER:
        forward, backward = order_match(vendor.order, target.order)
        if forward >= MIN_ORDER and forward >= backward:
            bonus += W_ORDER
            evidence.append(
                "the fixtures in the same order (" + ", ".join(vendor.order).lower() + ")"
            )
        elif backward >= MIN_ORDER:
            bonus += W_ORDER
            mirrored = True
            evidence.append(
                "the fixtures in reverse order ("
                + ", ".join(vendor.order).lower()
                + "): drawn opposite-hand"
            )
    close = [w for w in vendor.widths if any(_close(w, theirs) for theirs in target.widths)]
    if close:
        bonus += min(MAX_WIDTHS, W_WIDTH * len(close))
        evidence.append(
            "overall widths close on both (" + ", ".join(pairing.size_words(w) for w in close) + ")"
        )
    return SignatureMatch(round(bonus, 3), tuple(evidence), contradicts, mirrored)


def rerank(
    ranked: Sequence[pairing.Scored], vendor: Signature, targets: Mapping[str, Signature]
) -> tuple[pairing.Scored, ...]:
    """`ranked` (`pairing.rank` over the targets) with the page signature laid over it: each
    target's weight added (kept apart in `Scored.signed`), its evidence after code's own, and its
    rule-out when code had none; then sorted as `rank` sorts (not ruled out first, best first,
    ties in the given order). A target with no signature is left as it was."""
    counts: Counter[str] = Counter(w for s in targets.values() for w in s.rooms)
    out: list[pairing.Scored] = []
    for scored in ranked:
        signature = targets.get(scored.ref)
        if signature is None:
            out.append(scored)
            continue
        found = match(vendor, signature, counts, len(targets))
        if not found.bonus and not found.evidence and found.contradicts is None:
            out.append(scored)
            continue
        out.append(
            replace(
                scored,
                score=round(scored.score + found.bonus, 3),
                signed=round(scored.signed + found.bonus, 3),
                evidence=(*scored.evidence, *found.evidence),
                contradicts=scored.contradicts or found.contradicts,
            )
        )
    order = {s.ref: n for n, s in enumerate(out)}
    return tuple(
        sorted(out, key=lambda s: (s.contradicts is None, s.score, -order[s.ref]), reverse=True)
    )


def mirrored(vendor: Signature, target: Signature) -> bool:
    """Whether the target shows the vendor's fixtures only in reverse order (`order_match`)."""
    return match(vendor, target, {}, 0).mirrored
