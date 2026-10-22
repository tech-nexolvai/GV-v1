"""A millwork item the architect dimensions that the vendor's drawings leave out: red (#1847).

Anant, 2026-10-22: "a missing millwork item is a red mark." When the architect dimensions an item
GV fabricates or must coordinate on its drawings (an outlet, receptacle, junction box, data or
power box on the millwork, blocking, a cut-out, a grommet, a filler, a panel) and code confirms
the vendor prints that value on no vendor drawing of the area, in any round, the item is Wrong:
"Missing on the vendor's drawing: architect shows 1' - 2" (outlet, architect page 8)". There is no
"should be" beyond the architect's own value. Room features (ceilings, soffits, bulkheads,
artwork, a TV, mirrors, light switches and fixtures, shower and accessibility fittings, doors)
are never red here: they stay "vendor doesn't show it" (Not compared).

The escalation's `vendor_search` step (#1842) already searched every vendor drawing of the area
for the architect's printed value. `judge` decides, by code alone, whether that search proves a
missing millwork item. Zero false reds first, so every one of these must hold, or the item stays
Not compared with the reason:

1. **The architect's value is code's own reading at its spot**: a numbered label from the page's
   text layer (not pen strokes read by OCR), on the architect's drawing (not the brand prototype,
   not the vendor's other round), not inside one of GV's notes and not in GV's colour.
2. **The item is millwork, by its words**: the item's name, the architect's words for it and the
   architect's own printed words beside the dimension name a millwork word (`MILLWORK_WORDS`), and
   none of them names a room feature (`ROOM_WORDS`). An outlet is millwork only when it sits on
   the millwork: its height from the floor (its own value when it is a floor height, else the
   floor height the architect prints for it, both code-read labels) is no taller than the
   vendor's millwork (the tallest height the vendor prints for the area). Unsure: Not compared.
3. **Every vendor drawing of the area was read**: each has labels and words code read, none sits
   on a page with a pasted picture, and every vendor page of the area was opened in the check.
4. **The vendor prints the value nowhere**: no vendor label of the area (either way round, any
   round), no word on the vendor's drawings and no text of a vendor-only page prints it.
5. **The vendor does not show the item dimensioned another way**: the vendor's words do not
   carry a number, no other finding pairs the same architect label with a vendor value, the
   vendor's drawings never name the item ("Electrical outlets by GV", "grommet cover"), and no
   vendor label of the same way round prints a value within `NEAR_IN` of it (a different value
   at the same feature is a comparison, not a missing item).

The red goes through the normal proof (`proof.code_proved`): the finding carries `text_proof`
(kind `missing_item`: what code read, the pages and labels it searched), which the trust rule
counts as the `text` witness (`chat_trust`), and `paired_by: code` (the page map's area pairs the
drawings). Pure code: no PDF, no AI call (GV's colour is looked up through the reader passed in).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib import import_module
from typing import Any, Final

from app.review.assistant.finding_checks import dimension_tokens, to_inches

__all__ = [
    "ELECTRICAL_WORDS",
    "KIND",
    "MILLWORK_WORDS",
    "NEAR_IN",
    "ROOM_WORDS",
    "Judged",
    "VendorDrawing",
    "judge",
    "kind_of",
    "proof_words",
]

#: The `text_proof` kind of a missing millwork item.
KIND: Final = "missing_item"
#: A vendor value the same way round this close to the architect's may be the same item
#: dimensioned another way (inches): never "missing".
NEAR_IN: Final = Decimal(3)
#: Two printed values this close are the same value (inches).
_SAME_IN: Final = Decimal(1) / 32
#: How far round the architect's label its printed words count as "beside it" (points).
_BESIDE_PT: Final = 60.0
#: A vendor drawing proves an absence only with at least this many labels and worded phrases.
_MIN_LABELS: Final = 3
_MIN_WORDS: Final = 3
#: A page whose pictures cover more than this share may hold a pasted drawing code cannot search.
_MAX_PICTURE_SHARE: Final = 0.05
#: A height's dimension line ends on the floor line when within this (points).
_FLOOR_PT: Final = 3.0
#: How near (points) a phrase must be to join a note naming the item.
_NOTE_PT: Final = (40.0, 16.0)

#: The millwork words (generic trade words), each with the words a vendor note naming the item uses.
_KINDS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "outlet",
        (
            r"outlets?|receptacles?|duplex(?:es)?|quad(?:plex)?|g\.?f\.?c?\.?i\.?s?"
            r"|j-?box(?:es)?|junction\s+box(?:es)?|data\s+(?:box|outlet|port|jack)s?"
            r"|power\s+(?:box|outlet|strip|module)s?|electrical\s+box(?:es)?"
        ),
        r"outlet|receptacle|duplex|g\.?f\.?c?\.?i|j-?box|junction|electric|\belec\b|power|data",
    ),
    ("blocking", r"blocking|backing", r"blocking|backing"),
    ("grommet", r"grommets?", r"grommet"),
    ("cut-out", r"cut-?outs?|cut\s+outs?", r"cut-?out|cut\s+out"),
    ("filler", r"fillers?", r"filler"),
    ("panel", r"(?:end|side|fixed|back|finished|toe|access)\s+panels?|panels?", r"panel"),
)
#: Any millwork word, as one pattern.
MILLWORK_WORDS: Final = re.compile(
    r"\b(?:" + "|".join(f"(?:{words})" for _k, words, _n in _KINDS) + r")\b", re.IGNORECASE
)
#: The millwork words that need proof the item sits on the millwork (an outlet on a wall is not).
ELECTRICAL_WORDS: Final = re.compile(rf"\b(?:{_KINDS[0][1]})\b", re.IGNORECASE)
#: Room features and other trades' work (generic words): never a missing millwork item.
ROOM_WORDS: Final = re.compile(
    r"\b(?:ceilings?|soffits?|bulkheads?|art\s?work|art|pictures?|tvs?|t\.v\.|televisions?"
    r"|monitors?|mirrors?|lights?|lighting|lamps?|switch(?:es)?|thermostats?|fixtures?"
    r"|sconces?|chandeliers?|pendants?|showers?|tubs?|bath\s?tubs?|grab\s*bars?|towels?|hooks?"
    r"|toilets?|urinals?|accessor(?:y|ies)|faucets?|fireplaces?|doors?|door\s?heads?|doorways?"
    r"|windows?|columns?|sprinklers?|diffusers?|grilles?|vents?|wall\s?coverings?|paint"
    r"|tiles?|crowns?|electrical\s+panels?|panel\s?boards?|breakers?)\b",
    re.IGNORECASE,
)
#: Words saying a value is a height from the floor.
_FLOOR: Final = re.compile(
    r"\bfloor\b|\ba\.?f\.?f\.?(?=\W|$)|above\s+(?:the\s+)?(?:finished\s+)?floor", re.IGNORECASE
)

type Box = tuple[float, float, float, float]
#: The architect label -> whether it is set in GV's colour (None: could not be looked at).
type ColourReader = Callable[[Mapping[str, Any]], bool | None]


@dataclass(frozen=True, slots=True)
class VendorDrawing:
    """One vendor drawing of the item's area, as the set pack read it."""

    page: int
    round: int | None
    box: Box
    labels: Sequence[Mapping[str, Any]] = ()
    """Every label of the page (only those inside `box` count)."""
    phrases: Sequence[Mapping[str, Any]] = ()
    """Every phrase of the page's text layer (only those inside `box` count)."""
    text: str = ""
    """The page's whole text when the page holds only vendor drawings, else empty."""
    picture_share: float | None = None
    """The share of the page covered by pictures (`health.json`), None when not known."""
    opened: bool = True
    """The check opened the page (its coverage is not "not opened")."""


@dataclass(frozen=True, slots=True)
class Judged:
    """Whether a missing item is a proved missing millwork item, and why (not)."""

    red: bool
    why: str
    """Why it is not red (plain words); for a red, the red's sentence."""
    kind: str | None = None
    values: tuple[Mapping[str, Any], ...] = ()
    """The architect labels the red stands on (the item's value, and its floor height)."""
    pages: tuple[int, ...] = ()
    searched: int = 0
    rounds: tuple[tuple[int, int | None], ...] = field(default_factory=tuple)


def _flat(text: object) -> str:
    return " ".join(str(text or "").split())


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _centre_in(raw: object, box: Box, pad: float = 0.0) -> bool:
    found = _box(raw)
    if found is None:
        return False
    cx, cy = (found[0] + found[2]) / 2, (found[1] + found[3]) / 2
    return box[0] - pad <= cx <= box[2] + pad and box[1] - pad <= cy <= box[3] + pad


def _meets(raw: object, box: Box, pad_x: float, pad_y: float) -> bool:
    found = _box(raw)
    if found is None:
        return False
    return not (
        found[2] < box[0] - pad_x
        or found[0] > box[2] + pad_x
        or found[3] < box[1] - pad_y
        or found[1] > box[3] + pad_y
    )


def _inches_word(value: Decimal) -> str:
    whole = int(value)
    rest = value - whole
    if rest == 0:
        return f'{whole}"'
    sixteenths = int((rest * 16).to_integral_value())
    top, bottom = sixteenths, 16
    while top % 2 == 0 and bottom > 1:
        top, bottom = top // 2, bottom // 2
    return f'{whole} {top}/{bottom}"' if whole else f'{top}/{bottom}"'


def kind_of(texts: Sequence[object]) -> tuple[str | None, str | None]:
    """(the millwork kind its words name, the room feature they name), from the item's words:
    the first millwork word wins; any room word is reported."""
    kind: str | None = None
    room: str | None = None
    for text in texts:
        words = _flat(text)
        if not words:
            continue
        if room is None:
            found = ROOM_WORDS.search(words)
            room = found.group(0).lower() if found else None
        if kind is None:
            for name, pattern, _note in _KINDS:
                if re.search(rf"\b(?:{pattern})\b", words, re.IGNORECASE):
                    kind = name
                    break
    return kind, room


def _note_pattern(kind: str) -> re.Pattern[str]:
    words = next(note for name, _w, note in _KINDS if name == kind)
    return re.compile(words, re.IGNORECASE)


def _values_in(text: str) -> set[Decimal]:
    """Every size a printed text holds, as inches (millimetres as inches too)."""
    verify = import_module("app.review.assistant.simple_check.verify")
    return {Decimal(v.numerator) / Decimal(v.denominator) for v in verify.values_in(text)}


def _close(a: Decimal, b: Decimal, within: Decimal) -> bool:
    return abs(a - b) <= within


def _inside(drawing: VendorDrawing, items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [item for item in items if _centre_in(item.get("box"), drawing.box)]


def _vendor_labels(drawing: VendorDrawing) -> list[Mapping[str, Any]]:
    return [
        label
        for label in _inside(drawing, drawing.labels)
        if label.get("side") == "vendor"
        and not label.get("derived")
        and _decimal(label.get("inches")) is not None
    ]


def _where(drawing: VendorDrawing) -> str:
    return f"page {drawing.page}" + (f" round {drawing.round}" if drawing.round else "")


def _unread(drawings: Sequence[VendorDrawing]) -> str | None:
    """Why the vendor's drawings cannot prove an absence (one unread drawing is enough)."""
    if not drawings:
        return "no vendor drawing of the area to search"
    for drawing in drawings:
        where = _where(drawing)
        if not drawing.opened:
            return f"the vendor's {where} was not opened in this check"
        if drawing.picture_share is None:
            return f"code does not know whether the vendor's {where} holds a pasted picture"
        if drawing.picture_share > _MAX_PICTURE_SHARE:
            return f"the vendor's {where} holds a pasted picture code cannot search"
        words = [
            p
            for p in _inside(drawing, drawing.phrases)
            if re.search(r"[A-Za-z]{2,}", str(p.get("text") or ""))
        ]
        if len(_vendor_labels(drawing)) < _MIN_LABELS or len(words) < _MIN_WORDS:
            return f"the vendor's {where} has no label and text layer code read to search"
    return None


def _printed(drawings: Sequence[VendorDrawing], value: Decimal) -> str | None:
    """Where the vendor prints `value` (a label either way round, a word, a vendor-only page's
    text), or None when nowhere."""
    for drawing in drawings:
        for label in _vendor_labels(drawing):
            inches = _decimal(label.get("inches"))
            if inches is not None and _close(inches, value, _SAME_IN):
                return f"the vendor prints it at p{drawing.page}.{label.get('id')}"
        for phrase in _inside(drawing, drawing.phrases):
            text = str(phrase.get("text") or "")
            if any(_close(v, value, _SAME_IN) for v in _values_in(text)):
                return f"the vendor's {_where(drawing)} prints it ({_flat(text)[:30]})"
        if drawing.text and any(_close(v, value, _SAME_IN) for v in _values_in(drawing.text)):
            return f"the text of the vendor's {_where(drawing)} prints it"
    return None


def _lower_end(label: Mapping[str, Any]) -> float | None:
    """The lower end (page y, points) of a height's dimension line, or None."""
    line = label.get("dim_line")
    try:
        return max(float(point[1]) for point in line)  # type: ignore[union-attr]
    except (TypeError, ValueError, IndexError):
        return None


def _floor_line(drawing: VendorDrawing) -> float | None:
    """Where the vendor's drawing stands on the floor: the lower end of its tallest height."""
    heights = [
        (inches, label)
        for label in _vendor_labels(drawing)
        if label.get("orientation") == "v" and (inches := _decimal(label.get("inches"))) is not None
    ]
    if not heights:
        return None
    return _lower_end(max(heights, key=lambda h: h[0])[1])


def _near(
    drawings: Sequence[VendorDrawing], value: Decimal, way: object, *, floor: bool = False
) -> tuple[Decimal, str] | None:
    """The closest vendor value the same way round within `NEAR_IN` (another value at the same
    feature, maybe), or None. For a height from the floor (`floor`), only the vendor's heights
    from the floor count (their dimension line ends where the drawing's tallest height ends, or
    code does not know where it ends): a plan's depth is not a height."""
    best: tuple[Decimal, str] | None = None
    for drawing in drawings:
        ground = _floor_line(drawing) if floor else None
        for label in _vendor_labels(drawing):
            other = label.get("orientation")
            # A way round is known only for a drawn dimension (a note's value has no line).
            if (
                way in ("h", "v")
                and other in ("h", "v")
                and other != way
                and _lower_end(label) is not None
            ):
                continue
            if floor and ground is not None:
                end = _lower_end(label)
                if end is not None and abs(end - ground) > _FLOOR_PT:
                    continue
            inches = _decimal(label.get("inches"))
            if inches is None:
                continue
            gap = abs(inches - value)
            if gap <= NEAR_IN and (best is None or gap < best[0]):
                best = (gap, f"{_flat(label.get('text'))} (p{drawing.page}.{label.get('id')})")
    return best


def _named(drawings: Sequence[VendorDrawing], kind: str) -> str | None:
    """A vendor note naming the item ("Electrical outlets by GV"), joined with the words next to
    it, or None."""
    pattern = _note_pattern(kind)
    for drawing in drawings:
        phrases = _inside(drawing, drawing.phrases)
        for phrase in phrases:
            text = str(phrase.get("text") or "")
            if not pattern.search(text):
                continue
            box = _box(phrase.get("box"))
            near = [
                str(p.get("text") or "")
                for p in phrases
                if box is not None and _meets(p.get("box"), box, *_NOTE_PT)
            ]
            return f"'{_flat(' '.join(near or [text]))[:60]}' ({_where(drawing)})"
    return None


def _tallest(drawings: Sequence[VendorDrawing]) -> Decimal | None:
    heights = [
        inches
        for drawing in drawings
        for label in _vendor_labels(drawing)
        if label.get("orientation") == "v" and (inches := _decimal(label.get("inches"))) is not None
    ]
    return max(heights) if heights else None


def _parts(finding: Mapping[str, Any]) -> list[str]:
    """The architect's words for the item, a part per value ("1' - 2\" (CL); 1' - 6\" (AFF)"),
    with the folded copies' item and words."""
    parts = [p for p in re.split(r"[;\n]", _flat(finding.get("architect"))) if p.strip()]
    for entry in finding.get("also_on") or []:
        if isinstance(entry, Mapping):
            parts.append(f"{_flat(entry.get('item'))}: {_flat(entry.get('architect'))}")
    return parts


def _floor_label(
    finding: Mapping[str, Any],
    label: Mapping[str, Any],
    architect: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """The code-read architect label that is the item's height from the floor: its own label when
    it is a height and the words say the floor, else a height label the architect's words give
    for it with the floor ("1' - 6\" (height AFF)"), printed on the same architect drawing."""
    words = f"{_flat(finding.get('item'))} {_flat(finding.get('architect'))}"
    if label.get("orientation") == "v" and _FLOOR.search(words):
        return label
    for part in _parts(finding):
        if not _FLOOR.search(part):
            continue
        for token in dimension_tokens(part):
            want = to_inches(token)
            for other in architect:
                if (
                    other is not label
                    and other.get("orientation") == "v"
                    and other.get("route") == "text"
                    and not other.get("derived")
                    and want is not None
                    and _decimal(other.get("inches")) == want
                ):
                    return other
    return None


def _ref(label: Mapping[str, Any]) -> str:
    return f"p{label.get('page')}.{label.get('id')}"


def _paired_elsewhere(
    finding: Mapping[str, Any], refs: set[str], others: Sequence[Mapping[str, Any]]
) -> str | None:
    """Another finding that compares one of the item's architect labels with a vendor value."""
    for other in others:
        if other.get("id") == finding.get("id"):
            continue
        dims = other.get("dims") if isinstance(other.get("dims"), Mapping) else {}
        architect = (dims or {}).get("architect")
        if not isinstance(architect, Mapping) or _ref(architect) not in refs:
            continue
        vendor = (dims or {}).get("vendor")
        if isinstance(vendor, Mapping) or re.search(r"\d", _flat(other.get("vendor"))):
            return (
                f"{other.get('id')} compares it with the vendor's {_flat(other.get('vendor'))[:30]}"
            )
    return None


def judge(
    finding: Mapping[str, Any],
    *,
    label: Mapping[str, Any] | None,
    label_role: str | None,
    architect_labels: Sequence[Mapping[str, Any]],
    architect_phrases: Sequence[Mapping[str, Any]],
    notes: Sequence[Mapping[str, Any]],
    drawings: Sequence[VendorDrawing],
    others: Sequence[Mapping[str, Any]],
    colour: ColourReader | None,
) -> Judged:
    """Whether code proves `finding` (an item the AI says the vendor's drawing does not show) is a
    missing millwork item (see the module's rules, in their order). `label`: the architect label
    its `dims` names (its page and file set); `label_role`: the role of the page-map drawing that
    holds it; `architect_labels` / `architect_phrases`: the labels and words of that drawing;
    `notes`: GV's typed notes on its page; `drawings`: the vendor's drawings of the area (every
    round); `others`: the answer's other live findings; `colour`: GV's colour at a label."""
    if label is None:
        return Judged(False, "the architect's value has no label code read")
    if label.get("route") != "text" or label.get("derived"):
        return Judged(False, "the architect's value is not printed text (read from pen strokes)")
    if label.get("side") != "architect" or label_role != "architect":
        whose = label_role or label.get("side") or "unknown"
        return Judged(False, f"the value is on the {whose}'s drawing, not the architect's")
    value = _decimal(label.get("inches"))
    if value is None or value <= 0:
        return Judged(False, "the architect's value is not one size")
    if any(_centre_in(label.get("box"), b, 0.0) for b in (_box(n.get("box")) for n in notes) if b):
        return Judged(False, "the architect's value is inside one of GV's notes")
    box = _box(label.get("box"))
    beside = [
        str(p.get("text") or "")
        for p in architect_phrases
        if box is not None and _meets(p.get("box"), box, _BESIDE_PT, _BESIDE_PT)
    ]
    texts: list[object] = [finding.get("item"), finding.get("architect"), " ".join(beside)]
    for entry in finding.get("also_on") or []:
        if isinstance(entry, Mapping):
            texts += [entry.get("item"), entry.get("architect")]
    kind, room = kind_of(texts)
    if room is not None:
        return Judged(False, f"a room feature ({room}), not a millwork item")
    if kind is None:
        return Judged(False, "code finds no millwork word for it (outlet, blocking, cut-out, ...)")
    vendor_words = [_flat(finding.get("vendor"))] + [
        _flat(e.get("vendor")) for e in finding.get("also_on") or [] if isinstance(e, Mapping)
    ]
    if any(re.search(r"\d", words) for words in vendor_words):
        return Judged(False, "the vendor's side names a value: it may show the item", kind)
    unread = _unread(drawings)
    if unread is not None:
        return Judged(False, unread, kind)
    values: list[Mapping[str, Any]] = [label]
    if kind == "outlet":
        floor = _floor_label(finding, label, architect_labels)
        tallest = _tallest(drawings)
        height = _decimal(floor.get("inches")) if floor is not None else None
        if height is None or tallest is None:
            return Judged(
                False, "code cannot tell whether it sits on the millwork (no height from the floor)",
                kind,
            )  # fmt: skip
        if height > tallest:
            return Judged(
                False,
                f"it sits {_inches_word(height)} above the floor, above the vendor's millwork "
                f"(its tallest height {_inches_word(tallest)}): a wall item, not on the millwork",
                kind,
            )
        if floor is not None and floor is not label:
            values.append(floor)
    floor_value = values[-1] if kind == "outlet" else None
    for one in values:
        want = _decimal(one.get("inches"))
        assert want is not None
        printed = _printed(drawings, want)
        if printed is not None:
            return Judged(False, f"{_flat(one.get('text'))}: {printed}", kind)
        way = one.get("orientation") if _lower_end(one) is not None else None
        near = _near(drawings, want, way, floor=one is floor_value)
        if near is not None:
            return Judged(
                False,
                f"the vendor prints {near[1]} the same way round, within {_inches_word(NEAR_IN)} "
                f"of {_flat(one.get('text'))}: it may show the item dimensioned another way",
                kind,
            )
    elsewhere = _paired_elsewhere(finding, {_ref(v) for v in values}, others)
    if elsewhere is not None:
        return Judged(False, f"the vendor dimensions it: {elsewhere}", kind)
    named = _named(drawings, kind)
    if named is not None:
        return Judged(False, f"the vendor's drawing names it: {named}", kind)
    for one in values:
        coloured = colour(one) if colour is not None else None
        if coloured is None:
            return Judged(False, "code could not look at the architect value's colour", kind)
        if coloured:
            return Judged(False, "the architect's value is in GV's colour: GV's markup", kind)
    shown = " and ".join(_flat(v.get("text")) for v in values)
    sentence = (
        f"Missing on the vendor's drawing: architect shows {shown} ({kind}, architect page "
        f"{label.get('page')})"
    )
    searched = sum(len(_vendor_labels(d)) for d in drawings)
    pages = tuple(sorted({d.page for d in drawings}))
    rounds = tuple(sorted({(d.page, d.round) for d in drawings}, key=lambda r: (r[0], r[1] or 0)))
    return Judged(True, sentence, kind, tuple(values), pages, searched, rounds)


def _pages_words(pages: Sequence[object]) -> str:
    named = [str(p) for p in pages]
    if len(named) == 1:
        return f"page {named[0]}"
    return f"pages {', '.join(named[:-1])} and {named[-1]}"


def proof_words(proof: Mapping[str, Any]) -> str:
    """The proof card's line for a missing millwork item: "code searched 20 vendor labels on
    pages 8 and 9: none prints 1' - 2\"" (`text_proof` of kind `missing_item`)."""
    values = [str(v.get("text")) for v in proof.get("architect") or [] if isinstance(v, Mapping)]
    shown = " or ".join(values) or "the architect's value"
    count = int(proof.get("searched") or 0)
    labels = "1 vendor label" if count == 1 else f"{count} vendor labels"
    pages = [v.get("page") for v in proof.get("vendor") or [] if isinstance(v, Mapping)]
    unique = list(dict.fromkeys(p for p in pages if p is not None))
    where = f" on {_pages_words(unique)}" if unique else ""
    return (
        f"code searched {labels}{where} (every vendor drawing and round of the area): none prints "
        f"{shown}"
    )
