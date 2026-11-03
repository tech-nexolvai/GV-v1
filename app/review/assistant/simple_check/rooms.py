"""Room and unit numbers printed on a drawing, and the one architect page that is about a vendor
page's rooms. Code, $0, from the pages' printed text (`page_texts.json`, `dims.json`).

A vendor's sheet often says which rooms its piece is for ("Room Numbers: 101 (01), 201 (01)",
"ROOMS 101, 201", "UNITS 2A/3A", "RM 101-401"), and the architect's enlarged plan of that room
type prints the same rooms in its legend ("101, 201"). When the vendor's piece has no tag the
architect prints, those room numbers are the printed evidence of its page (a real set: a
kitchenette top for one suite type, its architect plan printing exactly the vendor's three rooms).

- **The vendor's rooms** (`vendor_rooms`): only the numbers printed right after a room or unit
  label (`ROOM`, `ROOMS`, `ROOM NO.`, `ROOM NUMBERS`, `RM`, `UNIT`, `UNITS`, `APT`), up
  to the first word that is not one: a quantity in brackets (`(01)`) is skipped; a range
  (`101-401`, `101 TO 401`, `101 THRU 401`) gives its two ends (its middle is not printed, so it
  is not guessed). A room number has three or four digits and maybe one letter (`105B`); after a
  unit or apartment label, also one or two digits and one or two letters (`2A`). `SUITE` is no
  label: it names a room type ("KING SUITE") and a hotel ("INN AND SUITES"), whose street number
  follows it in an address. Never an inch or millimetre value (a number with a unit mark, or a
  unit word after it), a sheet or tag number (`A-301`, `XX-305`: a letter, dash, dot or slash
  glued to it), a scale or a date.
- **A page's rooms** (`printed_rooms`): the same shapes printed on their own anywhere on a page.
- **The page about them** (`room_page`): the architect pages printing every one of the vendor's
  rooms (two or more), where they are a large part of what the page prints (`ROOM_SHARE`) and the
  page does not list a large part of the job's rooms (`ROOM_LIST_SHARE`: a room schedule, a
  finish list, a life-safety or floor plan prints many rooms, so rooms say nothing about which
  of them shows the piece). Exactly one page must pass; else none. A number printed on most of
  the architect's pages is its title block's (an address), never a room (`title_block`).

Pure: plain values in and out. The work list (`worklist._room_numbers_page`) uses it.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Hashable, Iterable, Mapping
from dataclasses import dataclass
from typing import Final

from vocabulary.dashes import dashes_to_hyphen

__all__ = [
    "MIN_ROOMS",
    "ROOM_LIST_SHARE",
    "ROOM_SHARE",
    "TITLE_BLOCK_MIN_PAGES",
    "TITLE_BLOCK_SHARE",
    "RoomPage",
    "has_short",
    "job_rooms",
    "printed_rooms",
    "room_page",
    "title_block",
    "vendor_rooms",
]

#: The vendor must print at least this many rooms: one room number may be printed anywhere.
MIN_ROOMS: Final = 2
#: The vendor's rooms are at least this share of the room numbers the page prints: the page is
#: about them (an enlarged plan of their room type), not a plan of many rooms that holds them too.
ROOM_SHARE: Final = 0.5
#: The page prints at most this share of the job's rooms (`job_rooms`): more is a list of the
#: job's rooms (a room schedule, a matrix), never the drawing of one room type.
ROOM_LIST_SHARE: Final = 0.5
#: A number printed on this share of the architect's pages or more is its title block's (the
#: office's address), not a room, when the file has at least `TITLE_BLOCK_MIN_PAGES` pages. A
#: room is printed on a few pages (its floor's plans, the schedule, its type's plan).
TITLE_BLOCK_SHARE: Final = 0.8
TITLE_BLOCK_MIN_PAGES: Final = 5
#: A job's room is printed on at least this many of its architect pages (a plan of the floor and a
#: schedule, an enlarged plan): a number printed once is a size, a part number or a note.
_JOB_MIN_PAGES: Final = 2

#: A room or unit label, maybe with its "numbers" word and a colon: `ROOM NUMBERS :`, `RM#`,
#: `ROOM NO.`, `UNITS`. Group `unit`: a unit or apartment label (short numbers allowed).
_LABEL: Final = re.compile(
    r"\b(?:ROOMS?|RMS?|(?P<unit>UNITS?|APTS?|APARTMENTS?))\b\.?"
    r"(?:\s*(?:NUMBERS?|NUMS?\b\.?|NOS?\b\.?|#))?\s*[:#]?"
)
#: A room number: three or four digits, maybe one letter (`101`, `105B`).
_ROOM: Final = r"\d{3,4}[A-Z]?"
#: A unit's short number after a unit label: one or two digits and one or two letters (`2A`).
_SHORT: Final = r"\d{1,2}[A-Z]{1,2}"
#: Nothing glued before a number: a letter or digit, a dash (a tag, a sheet number), a dot or a
#: slash (a sheet number, a date, a fraction), a unit mark.
_BEFORE: Final = r"(?<![\w.\-/'\"\u2032\u2033$])"
#: Nothing glued after it, and no unit after it (`610 MM`, `24 IN`).
_AFTER: Final = r"(?![\w.\-/'\"\u2032\u2033\u00b0%])(?!\s*(?:MM|CM|M|IN|INCH|INCHES|FT|SF|SQ)\b)"
_ROOM_TOKEN: Final = re.compile(rf"{_BEFORE}(?P<n>{_ROOM}){_AFTER}")
_SHORT_TOKEN: Final = re.compile(rf"{_BEFORE}(?P<n>{_SHORT}){_AFTER}")
#: Rooms printed with a slash between them (`101/201`): a list, never a date or a fraction (those
#: carry fewer digits on one side).
_SLASHED: Final = re.compile(rf"\b({_ROOM})/(?={_ROOM}\b)")
_SLASHED_SHORT: Final = re.compile(rf"\b({_SHORT})/(?={_SHORT}\b)")
#: What may stand between two numbers of one list: commas, `&`, `+`, `AND`, spaces, line ends.
_SEPARATOR: Final = re.compile(r"(?:[\s,;&+]|\bAND\b)*")
#: A quantity in brackets after a room (`(01)`, `(07))`): skipped.
_QUANTITY: Final = re.compile(r"\(\s*\d{1,3}\s*\)+")
#: A range of rooms (`101-401`, `101 TO 401`, `101 THRU 401`): its two ends.
_BETWEEN: Final = r"\s*(?:-|~|\bTO\b|\bTHRU\b|\bTHROUGH\b)\s*"
_RANGE_TOKEN: Final = re.compile(rf"{_BEFORE}(?P<n>{_ROOM}){_BETWEEN}(?P<m>{_ROOM}){_AFTER}")
_SHORT_RANGE: Final = re.compile(rf"{_BEFORE}(?P<n>{_SHORT}){_BETWEEN}(?P<m>{_SHORT}){_AFTER}")


def _normal(text: object) -> str:
    """Upper case, every look-alike dash the hyphen, slashed lists split (`101/201`)."""
    upper = dashes_to_hyphen(str(text or "")).upper()
    upper = _SLASHED.sub(r"\1, ", upper)
    return _SLASHED_SHORT.sub(r"\1, ", upper)


def _token(text: str, at: int, short: bool) -> re.Match[str] | None:
    """A range or one number at `at` (`short`: a unit's short numbers too)."""
    for pattern in (
        (_RANGE_TOKEN, _ROOM_TOKEN, _SHORT_RANGE, _SHORT_TOKEN)
        if short
        else (_RANGE_TOKEN, _ROOM_TOKEN)
    ):
        found = pattern.match(text, at)
        if found is not None:
            return found
    return None


def vendor_rooms(texts: Iterable[str]) -> frozenset[str]:
    """The room and unit numbers a vendor page prints after a room or unit label (see the
    module): every list on the page, together."""
    out: set[str] = set()
    for raw in texts:
        text = _normal(raw)
        for label in _LABEL.finditer(text):
            short = label.group("unit") is not None
            at = label.end()
            while True:
                at = _SEPARATOR.match(text, at).end()  # type: ignore[union-attr]
                quantity = _QUANTITY.match(text, at)
                if quantity is not None:
                    at = quantity.end()
                    continue
                found = _token(text, at, short)
                if found is None:
                    break
                # A range's two ends: its middle is not printed, so it is not guessed.
                out.update(n for n in found.groupdict().values() if n)
                at = found.end()
    return frozenset(out)


def has_short(numbers: Iterable[str]) -> bool:
    """Whether any of `numbers` is a unit's short number (`2A`), not a room number (`101`)."""
    return any(re.fullmatch(_ROOM, n) is None for n in numbers)


def printed_rooms(texts: Iterable[str], short: bool = False) -> frozenset[str]:
    """The room-shaped numbers a page prints on their own (see the module), anywhere on it;
    `short`: unit numbers of one or two digits and letters (`2A`) too."""
    out: set[str] = set()
    for raw in texts:
        text = _normal(raw)
        out.update(m.group("n") for m in _ROOM_TOKEN.finditer(text))
        if short:
            out.update(m.group("n") for m in _SHORT_TOKEN.finditer(text))
    return frozenset(out)


def title_block[K: Hashable](pages: Mapping[K, frozenset[str]]) -> frozenset[str]:
    """The numbers printed on `TITLE_BLOCK_SHARE` of the pages or more (`pages`: each page's
    room-shaped numbers), when there are `TITLE_BLOCK_MIN_PAGES` pages or more: the title block's
    (the office's street number), never a room."""
    if len(pages) < TITLE_BLOCK_MIN_PAGES:
        return frozenset()
    counted = Counter(n for found in pages.values() for n in found)
    return frozenset(n for n, c in counted.items() if c >= TITLE_BLOCK_SHARE * len(pages))


def job_rooms[K: Hashable](pages: Mapping[K, frozenset[str]]) -> frozenset[str]:
    """The job's rooms: the numbers printed on two or more of its architect pages (`pages`, the
    title block's numbers left out): a floor plan and the schedule, or an enlarged plan, print
    each room; a size or a part number is printed once."""
    counted = Counter(n for found in pages.values() for n in found)
    return frozenset(n for n, c in counted.items() if c >= _JOB_MIN_PAGES)


@dataclass(frozen=True, slots=True)
class RoomPage[K: Hashable]:
    """The architect page about the vendor's rooms (`room_page`)."""

    page: K
    rooms: frozenset[str]
    """The vendor's rooms, all printed on it."""
    printed: int
    """How many room numbers the page prints."""


def room_page[K: Hashable](
    vendor: frozenset[str], pages: Mapping[K, frozenset[str]], job: frozenset[str]
) -> tuple[RoomPage[K] | None, str | None]:
    """The one page of `pages` (each page's room numbers, the title block's left out) about the
    vendor's rooms (`vendor`, at least `MIN_ROOMS`): it prints every one of them, they are at least
    `ROOM_SHARE` of its room numbers, and its job rooms are at most `ROOM_LIST_SHARE` of `job`
    (`job_rooms`). `None` when no page or several pages pass, with why (`None`: the vendor prints
    too few rooms, or no page prints them all)."""
    if len(vendor) < MIN_ROOMS:
        return None, None
    holding = {key: found for key, found in pages.items() if vendor <= found}
    if not holding:
        return None, None
    passed: list[RoomPage[K]] = []
    crowded = False
    for key, found in holding.items():
        if len(found & job) > ROOM_LIST_SHARE * len(job):
            continue
        if len(vendor) >= ROOM_SHARE * len(found):
            passed.append(RoomPage(key, vendor, len(found)))
        else:
            crowded = True
    if len(passed) == 1:
        return passed[0], None
    if passed:
        return None, f"{len(passed)} architect pages are about its rooms: a tie is not broken"
    if crowded:
        return None, "the architect pages printing all its rooms print many other rooms too"
    return None, "the only architect pages printing all its rooms list most of the job's rooms"
