"""Scoring the pipeline at every place GV's reviewer flagged the vendor (#850, step 5 of #728).

On the first real client set GV's reviewer wrote a number in 54 `/FreeText` notes over the vendor's
drawing: single values, sums, and "scribe to fit" notes naming a filler. Nothing used them to test
the pipeline. Each one marks a place a person looked at closely, which makes it a place to ask what
the pipeline read there.

**What a box means, and what it does not.** A box marks a place where the vendor was wrong. Its
number is GV's — on the first real set, mostly the architect's, written over the vendor's — so:

- **it is never the vendor's value.** The vendor's value comes only from a person's key. A key entry
  holding GV's number in GV's own unit is refused, because it cannot be told from a person reading
  the box itself: on the first real set the 2026-09-29 pilot key, cut from renders with the
  reviewer's notes painted in, holds GV's own text at seven boxes. A key holding GV's number *in
  another unit* — millimetres beside the inches, or feet — cannot have come from a box that does
  not say it that way, and that box only rewrites the unit;
- **no box does not mean correct.** A reviewer boxes what they noticed, not everything that is
  wrong. So this scores only under boxes and never between them, and it does not measure false
  FAILs: a FAIL where nobody drew a box cannot be called false;
- **a site no reading reached is "not found", never "correct".** It says nothing about the vendor.

**What it counts.** Under each box: whether any vendor reading holds a value (found); where a key
gives the vendor's value, whether the readings match it; and two numbers that must be zero —
GV's number taken as the vendor's, and a reading that was agreed on or sealed although the key says
it is wrong. A PASS whose evidence lies under a box is listed for a person.

**Pure.** No database, no file, no model. `scripts/markup_yardstick.py` reads the drawing, the run
and the keys, and places every box in one frame before handing them here. Values are exact
`Fraction`s, in inches wherever a unit is stated: the only unit a value is compared in (Q12).

Source: issue #850 and the #728 plan. Verification: `tests/eval/test_markup_yardstick.py`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from units.imperial import ImperialParseError, parse_imperial
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from verdict.operands import QUALIFIED_STATUSES, EvidenceStatus

__all__ = [
    "Box",
    "GvNote",
    "KeyEntry",
    "KeyStatus",
    "PlacedFinding",
    "ReadState",
    "Reading",
    "Seal",
    "Site",
    "SiteKind",
    "SiteResult",
    "Yardstick",
    "measure",
    "render",
    "site_of",
]

#: `(left, top, right, bottom)`, in one frame the caller chose for every box it hands over. The
#: script uses stored space — the visible page, top-left origin, normalised to one — which is where
#: `app/evidence/sides.py` places readings too.
type Box = tuple[Decimal, Decimal, Decimal, Decimal]

#: A number followed by a unit mark: inches (`"`, `″`, `”`, `''`), feet (`'`, `′`, `’`) or `mm`.
#: What makes a number in a sentence a dimension rather than a count or a tag code.
_MARKED_NUMBER: Final = re.compile(r"\d\s*(?:\"|″|”|''|'|′|’|mm\b)", re.IGNORECASE)

#: A foot mark after a number, and not the first half of `''`, which is how inches are typed.
_FOOT: Final = re.compile(r"\d\s*['′’](?!['′’])")

#: Millimetres written out after a number.
_MM: Final = re.compile(r"\d\s*mm\b", re.IGNORECASE)

#: The corroboration statuses that mean a lane ran and found no disagreement.
#: `evidence/corroborate.py` records agreement as `RAW_CANDIDATE` with the lane named unless the
#: readings share a semantic type, and none has one at extraction, so the lane, not the status, is
#: what says it was agreed.
_AGREED: Final = frozenset({EvidenceStatus.RAW_CANDIDATE.value, EvidenceStatus.CORROBORATED.value})

_QUALIFIED: Final = frozenset(status.value for status in QUALIFIED_STATUSES)

_PASS: Final = "PASS"


class SiteKind(StrEnum):
    """What a box holds."""

    SINGLE = "single"
    """One value — a whole number, a fraction or a decimal, with or without a unit, and with or
    without a site instruction after it. The only kind that corrects one label, so the only kind
    scored against a key and the only one where a vendor reading of GV's number is counted as
    taken."""

    SUM = "sum"
    """Two values and an operator. GV's terms can be the vendor's own numbers in another order
    (`extraction/annotations.py`), so a reading that equals one proves nothing: a sum is never
    scored against a key, and a vendor reading of a term is not counted as GV's number taken."""

    NOTE = "note"
    """A dimension inside words: on the first real set, eight "scribe to fit" notes naming the
    filler to leave. Not one value either, and counted the way a sum is."""


@dataclass(frozen=True, slots=True)
class GvNote:
    """One reviewer note, with its text exactly as the file holds it."""

    page_index: int
    text: str
    box: Box


@dataclass(frozen=True, slots=True)
class Site:
    """A note that holds a dimension, and the number GV wrote when it is one value."""

    note: GvNote
    kind: SiteKind
    gv_value: Fraction | None
    """For a `SINGLE` site, GV's number in inches — or the bare number, where GV wrote no unit,
    because a must-be-zero count errs towards a person looking. `None` for the other kinds."""


class KeyStatus(StrEnum):
    """Where the vendor's value at a one-value site came from, or why there is none."""

    KEYED = "keyed"
    """A person's key gives the vendor's value, and it is not GV's number."""

    UNIT_ONLY = "unit_only"
    """The key gives GV's number, read off the vendor's label in another unit. The box only
    rewrites the unit, so GV's number is the vendor's value here."""

    REPEATS_BOX = "repeats_box"
    """The key gives GV's number in GV's own unit. Refused: it cannot be told apart from a person
    reading the box, and a box never stands in for the vendor's value."""

    DISAGREES = "disagrees"
    """Key entries under the box hold different values. No value is picked."""

    NONE = "none"
    """No usable key entry lies under the box."""

    NOT_SCORED = "not_scored"
    """A sum or a note: not one value, so no single vendor value answers it."""


class ReadState(StrEnum):
    """What the readings under a box did, against the vendor's value."""

    RIGHT = "right"
    """Every reading with a value holds the vendor's value."""

    MIXED = "mixed"
    """Some readings hold the vendor's value and some do not."""

    WRONG = "wrong"
    """No reading holds the vendor's value."""

    NOT_FOUND = "not_found"
    """No reading with a value lies under the box. Never counted as right."""

    UNKEYED = "unkeyed"
    """There is no vendor's value to score against."""


@dataclass(frozen=True, slots=True)
class KeyEntry:
    """One value a person read off the vendor's drawing, and where."""

    key: str
    item_id: str
    page_index: int
    box: Box
    value: Fraction
    """In inches."""
    raw_text: str | None
    """What the person typed. Its unit is what tells a unit rewrite from the box read again."""


@dataclass(frozen=True, slots=True)
class Seal:
    """A canonical observation that holds a reading, as primary or corroborating support."""

    observation_id: str
    document_role: str
    status: str
    value: Fraction
    """The observation's own value, in inches: the number a verdict would use. Compared as well as
    the reading's, so no count rests on the two being equal."""

    @property
    def qualified(self) -> bool:
        """Whether the observation may enter a verdict."""
        return self.status in _QUALIFIED


@dataclass(frozen=True, slots=True)
class Reading:
    """One row the pipeline recorded, placed in the caller's frame."""

    reading_id: str
    page_index: int
    box: Box
    route: str
    from_markup: bool
    """Whether the reviewer's markup route recorded it: GV's own text, never a vendor reading."""
    value: Fraction | None
    """In inches; `None` for a reading that holds no number with a unit."""
    lane: str | None
    status: str | None
    seals: tuple[Seal, ...] = ()

    @property
    def agreed(self) -> bool:
        """Whether a corroboration lane ran on it and found no disagreement: two readers from
        different vendors gave the same number, or a dual-unit label's two halves agree."""
        return self.lane is not None and self.status in _AGREED

    @property
    def sealed(self) -> bool:
        """Whether an observation that may enter a verdict holds it."""
        return any(seal.qualified for seal in self.seals)

    def holds(self, value: Fraction | None) -> bool:
        """Whether the reading, or an observation that seals it, says `value`."""
        return value is not None and (
            self.value == value or any(seal.value == value for seal in self.seals)
        )

    def contradicts(self, value: Fraction) -> bool:
        """Whether an agreed reading, or an observation that seals it, says other than `value`.

        Only what was agreed or sealed counts: a lone raw reading that differs is a misreading
        nobody trusted yet, scored by `ReadState`, not by this.
        """
        agreed_wrong = self.agreed and self.value is not None and self.value != value
        return agreed_wrong or any(seal.qualified and seal.value != value for seal in self.seals)


@dataclass(frozen=True, slots=True)
class PlacedFinding:
    """A finding, at the place one of its observations sits. One per observation it used."""

    finding_id: str
    outcome: str
    page_index: int
    box: Box


@dataclass(frozen=True, slots=True)
class SiteResult:
    """Everything the pipeline did under one box."""

    site: Site
    readings: tuple[Reading, ...]
    """Every vendor reading under the box, with a value or not."""
    markup_readings: tuple[Reading, ...]
    key_status: KeyStatus
    vendor_value: Fraction | None
    read: ReadState
    gv_taken: tuple[Reading, ...]
    """Readings that took GV's number as a drawing's: a vendor reading that says GV's number at a
    one-value box that does not only rewrite the unit, or the box itself held by an observation."""
    agreed_or_sealed_wrong: tuple[Reading, ...]
    """Vendor readings the key says are wrong: agreed on by a corroboration lane (`Reading.agreed`),
    or held by an observation able to enter a verdict with a value other than the key's."""
    agreed_or_sealed_unchecked: tuple[Reading, ...]
    """Agreed or sealed vendor readings at a box with no vendor's value to check them against."""
    findings: tuple[PlacedFinding, ...]
    """Every finding with an observation under the box, whatever its outcome."""

    @property
    def found(self) -> bool:
        """Whether a vendor reading with a value lies under the box. Not whether it is right."""
        return any(reading.value is not None for reading in self.readings)

    @property
    def passes(self) -> tuple[PlacedFinding, ...]:
        """The PASS findings under the box: checks passed on evidence from a place GV flagged."""
        return tuple(finding for finding in self.findings if finding.outcome == _PASS)


@dataclass(frozen=True, slots=True)
class Yardstick:
    """The result over every box on the drawing."""

    notes: int
    """Every reviewer note handed in, dimension or not."""
    sites: tuple[SiteResult, ...]
    shared_key_entries: int
    """Key entries lying under two or more boxes, which no box uses: which label each was cut
    round cannot be told from where it lies."""

    def counts(self) -> dict[str, int]:
        """Every count the report prints, under stable names."""
        kinds = Counter(result.site.kind for result in self.sites)
        statuses = Counter(result.key_status for result in self.sites)
        reads = Counter(result.read for result in self.sites)
        return {
            "notes": self.notes,
            "sites": len(self.sites),
            "sites_single": kinds[SiteKind.SINGLE],
            "sites_sum": kinds[SiteKind.SUM],
            "sites_note": kinds[SiteKind.NOTE],
            "found": sum(1 for result in self.sites if result.found),
            "not_found": sum(1 for result in self.sites if not result.found),
            "keyed": statuses[KeyStatus.KEYED] + statuses[KeyStatus.UNIT_ONLY],
            "read_right": reads[ReadState.RIGHT],
            "read_mixed": reads[ReadState.MIXED],
            "read_wrong": reads[ReadState.WRONG],
            "keyed_not_found": reads[ReadState.NOT_FOUND],
            "unit_only": statuses[KeyStatus.UNIT_ONLY],
            "key_repeats_box": statuses[KeyStatus.REPEATS_BOX],
            "key_disagrees": statuses[KeyStatus.DISAGREES],
            "key_shared": self.shared_key_entries,
            "no_key": statuses[KeyStatus.NONE],
            "gv_taken_readings": len(_distinct(r for s in self.sites for r in s.gv_taken)),
            "gv_taken_sites": sum(1 for result in self.sites if result.gv_taken),
            "agreed_or_sealed_wrong_readings": len(
                _distinct(r for s in self.sites for r in s.agreed_or_sealed_wrong)
            ),
            "agreed_or_sealed_wrong_sites": sum(
                1 for result in self.sites if result.agreed_or_sealed_wrong
            ),
            "agreed_or_sealed_unchecked_readings": len(
                _distinct(r for s in self.sites for r in s.agreed_or_sealed_unchecked)
            ),
            "pass_at_site": len({f.finding_id for s in self.sites for f in s.passes}),
        }


def site_of(note: GvNote) -> Site | None:
    """The site a note makes, or `None` when it holds no dimension.

    Read with the parsers the pipeline reads a dimension with (`units.notation`,
    `units.normalise`), and one step further: a number GV wrote with no unit is still one value
    here, where a reading of it would hold none, because the number is what a vendor reading must
    not repeat. A title, a material code or `(VIF)` is no number and no site.
    """
    text = note.text
    if is_compound(text):
        return Site(note, SiteKind.SUM, None)
    token = canonical_notation(text)[0]
    try:
        return Site(note, SiteKind.SINGLE, normalise_to_inches(token).exact)
    except UnitNormalisationError:
        value = _bare_number(token)
    if value is not None:
        return Site(note, SiteKind.SINGLE, value)
    if _MARKED_NUMBER.search(text):
        return Site(note, SiteKind.NOTE, None)
    return None


def _bare_number(token: str) -> Fraction | None:
    """A number GV wrote with no unit, or `None` when the text is not a number at all."""
    try:
        return parse_imperial(token)
    except ImperialParseError:
        return None


def _unit_system(text: str | None) -> str:
    """Which unit a text states its number in: `mm` (alone or beside inches), `ft`, or `in`.

    **No unit at all counts as inches**, because a person keying a unitless box would add the inch
    mark the key requires; reading that as a different unit would accept the box as a rewrite.
    """
    if text is None:
        return "in"
    if canonical_notation(text)[1] is not None or _MM.search(text):
        return "mm"
    if _FOOT.search(text):
        return "ft"
    return "in"


def _overlaps(first: Box, second: Box) -> bool:
    """Whether two boxes share some area. Boxes that only touch along an edge do not."""
    return (
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
    )


def _under(box: Box, page_index: int, other: Box, other_page: int) -> bool:
    return page_index == other_page and _overlaps(box, other)


def _distinct(readings: Iterable[Reading]) -> set[str]:
    return {reading.reading_id for reading in readings}


def _key_status(site: Site, entries: Sequence[KeyEntry]) -> tuple[KeyStatus, Fraction | None]:
    """The vendor's value at one site, from the key entries under its box, or why there is none.

    **The box is never consulted for the value**: GV's number decides only whether a key entry is
    refused. Every path that returns a value returns one a person keyed off the drawing.
    """
    if site.kind is not SiteKind.SINGLE:
        return KeyStatus.NOT_SCORED, None
    if not entries:
        return KeyStatus.NONE, None
    values = {entry.value for entry in entries}
    if len(values) != 1:
        return KeyStatus.DISAGREES, None
    (value,) = values
    if value != site.gv_value:
        return KeyStatus.KEYED, value
    gv_unit = _unit_system(site.note.text)
    if any(_unit_system(entry.raw_text) != gv_unit for entry in entries):
        return KeyStatus.UNIT_ONLY, value
    return KeyStatus.REPEATS_BOX, None


def _read_state(vendor_value: Fraction | None, readings: Sequence[Reading]) -> ReadState:
    if vendor_value is None:
        return ReadState.UNKEYED
    values = [reading.value for reading in readings if reading.value is not None]
    if not values:
        return ReadState.NOT_FOUND
    right = sum(1 for value in values if value == vendor_value)
    if right == len(values):
        return ReadState.RIGHT
    return ReadState.WRONG if right == 0 else ReadState.MIXED


def _gv_taken(
    site: Site,
    status: KeyStatus,
    readings: Sequence[Reading],
    markup_readings: Sequence[Reading],
) -> tuple[Reading, ...]:
    """Readings that took GV's number as a drawing's.

    Two ways. **The box itself held by an observation**, at any kind of box: whatever its role, the
    reviewer's text has become one side's reading (#802). And **a vendor reading that says GV's
    number at a one-value box**, itself or through an observation sealing it — unless a key shows
    the box only rewrites the unit, where GV's number is the vendor's. With no key, or a refused
    one, the match is counted: the box marks the vendor as wrong here, so the vendor's label is not
    expected to say GV's number.
    """
    taken = [reading for reading in markup_readings if reading.seals]
    if site.kind is SiteKind.SINGLE and status is not KeyStatus.UNIT_ONLY:
        taken += [reading for reading in readings if reading.holds(site.gv_value)]
    return tuple(taken)


def measure(
    notes: Sequence[GvNote],
    readings: Sequence[Reading],
    key: Sequence[KeyEntry] = (),
    findings: Sequence[PlacedFinding] = (),
) -> Yardstick:
    """Score the pipeline under every box that holds a dimension.

    Everything must be in one frame. A reading or a key entry is under a box when it shares some
    area with it on the same page; a key entry under two boxes is used for neither.
    """
    sites = [site for note in notes if (site := site_of(note)) is not None]
    for site in sites:
        left, top, right, bottom = site.note.box
        if right <= left or bottom <= top:
            raise ValueError(
                f"a note on page {site.note.page_index + 1} has a box with no area, so no reading "
                "could ever lie under it and every one would read as not found"
            )

    under: dict[int, list[KeyEntry]] = {index: [] for index in range(len(sites))}
    shared = 0
    for entry in key:
        holding = [
            index
            for index, site in enumerate(sites)
            if _under(entry.box, entry.page_index, site.note.box, site.note.page_index)
        ]
        if len(holding) > 1:
            shared += 1
            continue
        for index in holding:
            under[index].append(entry)

    results: list[SiteResult] = []
    for index, site in enumerate(sites):
        here = [
            reading
            for reading in readings
            if _under(reading.box, reading.page_index, site.note.box, site.note.page_index)
        ]
        vendor = tuple(reading for reading in here if not reading.from_markup)
        markup = tuple(reading for reading in here if reading.from_markup)
        status, vendor_value = _key_status(site, under[index])
        checked = [reading for reading in vendor if reading.agreed or reading.sealed]
        results.append(
            SiteResult(
                site=site,
                readings=vendor,
                markup_readings=markup,
                key_status=status,
                vendor_value=vendor_value,
                read=_read_state(vendor_value, vendor),
                gv_taken=_gv_taken(site, status, vendor, markup),
                agreed_or_sealed_wrong=(
                    ()
                    if vendor_value is None
                    else tuple(reading for reading in checked if reading.contradicts(vendor_value))
                ),
                agreed_or_sealed_unchecked=tuple(checked) if vendor_value is None else (),
                findings=tuple(
                    finding
                    for finding in findings
                    if _under(finding.box, finding.page_index, site.note.box, site.note.page_index)
                ),
            )
        )
    return Yardstick(notes=len(notes), sites=tuple(results), shared_key_entries=shared)


def render(yardstick: Yardstick) -> str:
    """The result as counts a person can paste, with the PASS findings listed by id and page.

    **No text and no value from the drawing appears**, neither GV's nor the vendor's: the counts can
    go on a public issue while the drawing stays under `data/`.
    """
    counts = yardstick.counts()

    def row(label: str, name: str) -> str:
        return f"  {label:<56}{counts[name]:>6}"

    lines = [
        "WHERE GV'S REVIEWER FLAGGED THE VENDOR",
        "=" * 64,
        row("reviewer notes", "notes"),
        row("...that hold a dimension: a site", "sites"),
        row("    one value", "sites_single"),
        row("    a sum", "sites_sum"),
        row("    a dimension in a note", "sites_note"),
        "",
        row("found: a vendor reading with a value under the box", "found"),
        row("not found", "not_found"),
        "",
        "THE VENDOR'S VALUE, FROM A PERSON'S KEY (one-value sites)",
        "-" * 64,
        row("keyed", "keyed"),
        row("    read right", "read_right"),
        row("    read right by one reading and wrong by another", "read_mixed"),
        row("    read wrong", "read_wrong"),
        row("    not found", "keyed_not_found"),
        row("    of the keyed: boxes that only rewrite the unit", "unit_only"),
        row("key refused: it repeats the box", "key_repeats_box"),
        row("key entries under one box disagree", "key_disagrees"),
        row("key entries under two boxes, used for neither", "key_shared"),
        row("no key", "no_key"),
        "",
        "MUST BE ZERO",
        "-" * 64,
        row("GV's number taken as the vendor's: readings", "gv_taken_readings"),
        row("    at sites", "gv_taken_sites"),
        row("agreed or sealed, and wrong by the key: readings", "agreed_or_sealed_wrong_readings"),
        row("    at sites", "agreed_or_sealed_wrong_sites"),
        "",
        "FOR A PERSON TO LOOK AT",
        "-" * 64,
        row("PASS findings with evidence under a box", "pass_at_site"),
    ]
    listed: set[str] = set()
    for result in yardstick.sites:
        for finding in result.passes:
            if finding.finding_id not in listed:
                listed.add(finding.finding_id)
                lines.append(
                    f"      finding {finding.finding_id}: page {result.site.note.page_index + 1}"
                )
    lines += [
        row(
            "agreed or sealed, with no key to check them: readings",
            "agreed_or_sealed_unchecked_readings",
        ),
        "",
        "WHAT THIS DOES NOT MEASURE",
        "-" * 64,
        "  False FAILs. A place with no box is not known to be right, so a FAIL",
        "  there cannot be called false.",
        "  Correctness where nothing was found. A site no reading reached is",
        "  not found, which says nothing about the vendor's drawing.",
        "  The vendor's value from the box. A box marks where the vendor was",
        "  wrong; the vendor's value comes only from a person's key.",
    ]
    return "\n".join(lines)
