"""The architect's printed text, put back together from the characters the file holds (#1052).

Inside a pasted drawing pdfplumber gives single characters, and the words it builds from them come
apart where the architect's CAD set spaces: `3'` and `- 6"` are two words, two narrow bays' labels
touch (`1' -0"1' -0"`), and a sideways height reads backwards (`"5 - '2`). This module rebuilds
lines of text — by baseline for upright text, by column for sideways text, using each character's
own rotation — splits them into phrases where a real gap is, and finds in each phrase:

* every **dimension** (`DIMENSION_PATTERN`), with the words printed beside it, read by
  `labels.read_label`, so a qualifier is a flag and a note is held;
* every **scale note**: an architectural scale (`1/4" = 1'-0"`) or a ratio (`1:10`).

**What it never does.** It never reads coloured text (the caller passes black and grey only), never
reads a number out of a phrase it cannot split cleanly, and never fills in a character it did not
see. A character printed twice on top of itself — a CAD "bold" — is one character.

Everything is `Decimal`, in pdfplumber's frame (page points, `top` downward).

Source: issue #1052 · Verification: `tests/extraction/architect/test_text.py`
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final

from extraction.architect.labels import DIMENSION_PATTERN, LabelReading, read_label
from extraction.geometry.rows import Box
from units.normalise import plain_marks

__all__ = [
    "Orientation",
    "PrintedDimension",
    "PrintedText",
    "ScaleKind",
    "ScaleNote",
    "TextChar",
    "TextSettings",
    "find_printed",
]

_TWO: Final = Decimal(2)


class Orientation(StrEnum):
    """Which way a run of text reads on the page."""

    UPRIGHT = "upright"
    UP = "up"
    """Turned a quarter anticlockwise: reads from the bottom of the page to the top."""
    DOWN = "down"
    """Turned a quarter clockwise: reads from the top of the page to the bottom."""


@dataclass(frozen=True, slots=True)
class TextChar:
    """One black or grey character of real text, with its box and which way it reads."""

    text: str
    box: Box
    orientation: Orientation | None
    """`None` for a character set at an angle that is none of the three: never read."""


@dataclass(frozen=True, slots=True)
class TextSettings:
    """The few lengths the assembly uses, as fractions of the characters' own height (`em`)."""

    same_line_em: Decimal
    """Characters whose baselines (columns, for sideways text) are this close are one line."""
    space_em: Decimal
    """A gap wider than this between two characters is a space."""
    phrase_em: Decimal
    """A gap wider than this ends a phrase: two separate labels, not one."""
    duplicate_em: Decimal
    """The same character this close to another is the same character printed twice."""
    same_size_fraction: Decimal
    """A label's characters differ in height by at most this fraction, or it is held."""


class ScaleKind(StrEnum):
    ARCHITECTURAL = "architectural"
    """`1/4" = 1'-0"`: so much paper per foot. The architect's convention."""
    RATIO = "ratio"
    """`1:10`: one part paper to so many real. The vendor's convention on the client's sheets."""


@dataclass(frozen=True, slots=True)
class ScaleNote:
    """A scale printed in a drawing, and what it says exactly."""

    text: str
    kind: ScaleKind
    paper_per_real: Fraction
    """Paper length per real length: `1/48` for `1/4" = 1'-0"`, `1/10` for `1:10`."""
    box: Box


@dataclass(frozen=True, slots=True)
class PrintedDimension:
    """One dimension label found in the text, where it is printed, and what it reads as."""

    reading: LabelReading
    box: Box
    orientation: Orientation
    phrase: str
    """The whole phrase it was printed in, as printed."""
    chars: frozenset[TextChar] = field(default=frozenset(), compare=False)
    """The characters it is printed in."""

    @property
    def is_feet_and_inches(self) -> bool:
        return "'" in plain_marks(self.reading.dimension)


#: A centre-line mark, as a phrase on its own once its spaces are removed.
_CENTRE_LINE_MARKS: Final = frozenset({"CL", "C/L", "\u2104"})

#: Written into a whole line where a gap wider than a phrase break is, so a scale note may span
#: the wide gaps round its `=` while its paper length (`1 1/2"`) may not span one.
_WIDE_GAP: Final = "\u00a6"

#: `1/4" = 1'-0"`, `1 1/2" = 1'-0"`, `3" = 1'-0"`, in `plain_marks` spelling. The paper length
#: must not continue a number before it: in `ID 8.3 1/2" = 1'-0"` the paper length is `1/2"`.
_ARCHITECTURAL_SCALE: Final = re.compile(
    r"(?<![\d./])(?P<paper>\d+ +\d+/\d+|\d+/\d+|\d+) *\"[\s\u00a6]*=[\s\u00a6]*" r"1 *' *-? *0 *\""
)
#: `1:10`, `1 : 20`; not part of a longer run of digits, colons or slashes (a time, a ratio of
#: ratios).
_RATIO_SCALE: Final = re.compile(r"(?<![\d:/.])1\s*:\s*(?P<real>\d+)(?![\d:/.])")


def orientation_of(matrix: Sequence[object] | None, upright: bool | None) -> Orientation | None:
    """Which way a character reads, from its text matrix `(a, b, c, d, e, f)`.

    Exact quarter turns only; a character at any other angle is not read.
    """
    if matrix is None or len(matrix) < 4:
        return Orientation.UPRIGHT if upright else None
    try:
        a, b, c, d = (Decimal(str(value)) for value in list(matrix)[:4])
    except (ArithmeticError, TypeError, ValueError):
        return None
    if b == 0 and c == 0 and a > 0 and d > 0:
        return Orientation.UPRIGHT
    if a == 0 and d == 0 and b > 0 and c < 0:
        return Orientation.UP
    if a == 0 and d == 0 and b < 0 and c > 0:
        return Orientation.DOWN
    return None


def _height(char: TextChar) -> Decimal:
    box = char.box
    return box.height if char.orientation is Orientation.UPRIGHT else box.width


def _along(char: TextChar) -> Decimal:
    """Where a character sits along its line, in reading order."""
    if char.orientation is Orientation.UPRIGHT:
        return char.box.x0
    if char.orientation is Orientation.UP:
        return -char.box.bottom
    return char.box.top


def _across(char: TextChar) -> Decimal:
    """Which line a character is on: its baseline, or its column for sideways text."""
    if char.orientation is Orientation.UPRIGHT:
        return char.box.bottom
    if char.orientation is Orientation.UP:
        return char.box.x1
    return char.box.x0


def _start(char: TextChar) -> Decimal:
    return _along(char)


def _end(char: TextChar) -> Decimal:
    box = char.box
    if char.orientation is Orientation.UPRIGHT:
        return box.x1
    if char.orientation is Orientation.UP:
        return -box.top
    return box.bottom


def _deduplicated(chars: Sequence[TextChar], settings: TextSettings) -> list[TextChar]:
    kept: list[TextChar] = []
    for char in chars:
        reach = settings.duplicate_em * _height(char)
        if any(
            other.text == char.text
            and other.orientation is char.orientation
            and abs(other.box.x0 - char.box.x0) <= reach
            and abs(other.box.top - char.box.top) <= reach
            for other in kept
        ):
            continue
        kept.append(char)
    return kept


@dataclass(frozen=True, slots=True)
class _Phrase:
    chars: tuple[TextChar, ...]
    printed: str
    plain: str
    owner: tuple[int | None, ...]
    """For every character of `plain`, the index in `chars` it came from, or `None` for a space."""

    def box(self, start: int, end: int) -> Box:
        indices = {index for index in self.owner[start:end] if index is not None}
        boxes = [self.chars[index].box for index in sorted(indices)]
        result = boxes[0]
        for box in boxes[1:]:
            result = result.union(box)
        return result

    def chars_in(self, start: int, end: int) -> frozenset[TextChar]:
        return frozenset(self.chars[index] for index in self.owner[start:end] if index is not None)

    def printed_slice(self, start: int, end: int) -> str:
        indices = sorted({index for index in self.owner[start:end] if index is not None})
        if not indices:
            return ""
        pieces: list[str] = []
        for position, index in enumerate(indices):
            if position and self._spaced(indices[position - 1], index):
                pieces.append(" ")
            pieces.append(self.chars[index].text)
        return "".join(pieces)

    def _spaced(self, before: int, after: int) -> bool:
        # A space was written into `plain` between the two characters.
        first = self.owner.index(before)
        second = self.owner.index(after)
        return any(owner is None for owner in self.owner[first:second])


def _lines(chars: Sequence[TextChar], settings: TextSettings) -> list[list[TextChar]]:
    """Characters grouped into lines of one orientation, each in reading order."""
    lines: list[list[TextChar]] = []
    for orientation in Orientation:
        group = sorted(
            (char for char in chars if char.orientation is orientation),
            key=lambda char: (_across(char), _along(char)),
        )
        current: list[TextChar] = []
        for char in group:
            if current and abs(_across(char) - _across(current[-1])) > (
                settings.same_line_em * _height(char)
            ):
                lines.append(sorted(current, key=_along))
                current = []
            current.append(char)
        if current:
            lines.append(sorted(current, key=_along))
    return lines


def _vulgar(text: str) -> bool:
    return unicodedata.decomposition(text).startswith("<fraction>")


def _phrases(
    line: Sequence[TextChar], settings: TextSettings, *, split: bool = True
) -> list[_Phrase]:
    """A line cut into phrases where a gap wider than `phrase_em` is (or kept whole, `split=False`),
    each spelled twice: as printed, and in `plain_marks` spelling for the patterns."""
    phrases: list[_Phrase] = []
    chars: list[TextChar] = []
    printed: list[str] = []
    plain: list[str] = []
    owner: list[int | None] = []

    def close() -> None:
        if chars:
            phrases.append(_Phrase(tuple(chars), "".join(printed), "".join(plain), tuple(owner)))

    previous: TextChar | None = None
    for char in line:
        if previous is not None:
            gap = _start(char) - _end(previous)
            height = max(_height(char), _height(previous))
            if gap > settings.phrase_em * height:
                if split:
                    close()
                    chars, printed, plain, owner = [], [], [], []
                else:
                    printed.append(" ")
                    plain.append(_WIDE_GAP)
                    owner.append(None)
            elif gap > settings.space_em * height:
                printed.append(" ")
                plain.append(" ")
                owner.append(None)
        index = len(chars)
        chars.append(char)
        printed.append(char.text)
        piece = plain_marks(char.text)
        if _vulgar(char.text) and plain and plain[-1][-1:].isdigit():
            # `1½` is one and a half, never fifteen over two.
            plain.append(" ")
            owner.append(None)
        plain.append(piece)
        owner.extend([index] * len(piece))
        previous = char
    close()
    return phrases


def _scale_notes(phrase: _Phrase) -> list[tuple[ScaleNote, frozenset[TextChar]]]:
    """Every scale note in a whole line, with the characters it is printed in."""
    notes: list[tuple[ScaleNote, frozenset[TextChar]]] = []
    for match in _ARCHITECTURAL_SCALE.finditer(phrase.plain):
        paper = sum((Fraction(part) for part in match.group("paper").split()), Fraction(0))
        if paper <= 0:
            continue
        notes.append(
            (
                ScaleNote(
                    text=phrase.printed_slice(match.start(), match.end()),
                    kind=ScaleKind.ARCHITECTURAL,
                    paper_per_real=paper / 12,
                    box=phrase.box(match.start(), match.end()),
                ),
                phrase.chars_in(match.start(), match.end()),
            )
        )
    for match in _RATIO_SCALE.finditer(phrase.plain):
        real = int(match.group("real"))
        if real <= 0:
            continue
        notes.append(
            (
                ScaleNote(
                    text=phrase.printed_slice(match.start(), match.end()),
                    kind=ScaleKind.RATIO,
                    paper_per_real=Fraction(1, real),
                    box=phrase.box(match.start(), match.end()),
                ),
                phrase.chars_in(match.start(), match.end()),
            )
        )
    return notes


def _held(reading: LabelReading, reason: str) -> LabelReading:
    return LabelReading(
        text=reading.text,
        dimension=reading.dimension,
        qualifiers=reading.qualifiers,
        printed_inches=reading.printed_inches,
        held_reason=reason,
    )


def _dimensions(
    phrase: _Phrase, orientation: Orientation, settings: TextSettings
) -> list[PrintedDimension]:
    """Every dimension in the phrase outside the scale notes, each read with the words around it.

    A phrase with one dimension is read whole, so its qualifiers and any other words travel with
    it. A phrase with several is read dimension by dimension; any other word in it holds them all,
    because which dimension the word belongs to is not knowable from the text.
    """
    found = list(DIMENSION_PATTERN.finditer(phrase.plain))
    if not found:
        return []
    covered = [False] * len(phrase.plain)
    for match in found:
        for position in range(match.start(), match.end()):
            covered[position] = True
    rest = "".join(
        character if not covered[position] else " "
        for position, character in enumerate(phrase.plain)
    )
    phrase_text = phrase.printed_slice(0, len(phrase.plain))
    results: list[PrintedDimension] = []
    for match in found:
        if len(found) == 1:
            reading = read_label(phrase_text)
        else:
            reading = read_label(phrase.printed_slice(match.start(), match.end()))
            if rest.split():
                reading = _held(
                    reading,
                    "other words are printed in the same phrase as more than one dimension ("
                    + " ".join(rest.split())
                    + ")",
                )
        heights = {_height(char) for char in phrase.chars_in(match.start(), match.end())}
        if max(heights) - min(heights) > max(heights) * settings.same_size_fraction:
            # A stacked fraction's numerator and denominator are set smaller beside the whole
            # number, and joined along the line they read as one wrong number: `21` and `7/8`
            # as `218"`.
            reading = _held(
                reading, "its characters are not all one size: a stacked fraction or two labels"
            )
        results.append(
            PrintedDimension(
                reading=reading,
                box=phrase.box(match.start(), match.end()),
                orientation=orientation,
                phrase=phrase_text,
                chars=phrase.chars_in(match.start(), match.end()),
            )
        )
    return results


@dataclass(frozen=True, slots=True)
class PrintedText:
    """What one region's text holds: its dimension labels, its scale notes and every phrase."""

    dimensions: tuple[PrintedDimension, ...]
    scales: tuple[ScaleNote, ...]
    phrases: tuple[str, ...]
    """Every phrase as printed, for counting how the drawing writes its labels."""
    centre_marks: tuple[Box, ...]
    """Where a centre-line mark is printed: a phrase that is `CL`, `C L`, `C/L` or `℄` alone."""


def find_printed(chars: Sequence[TextChar], settings: TextSettings) -> PrintedText:
    """Every dimension label and every scale note in `chars`.

    `chars` must already be the drawing's black and grey text only, and only the part of the page
    the caller wants read (one drawing's region, say). Characters at an angle that is not a quarter
    turn are not read.
    """
    readable = _deduplicated([char for char in chars if char.orientation is not None], settings)
    dimensions: list[PrintedDimension] = []
    scales: list[ScaleNote] = []
    phrases: list[str] = []
    marks: list[Box] = []
    for line in _lines(readable, settings):
        orientation = line[0].orientation
        assert orientation is not None
        # Scale notes first, on the whole line: `1/2"  =  1'-0"` is set with wide gaps. Their
        # characters are then not read again as a `1'-0"` dimension.
        used: set[TextChar] = set()
        for whole in _phrases(line, settings, split=False):
            for note, printed_in in _scale_notes(whole):
                scales.append(note)
                used |= printed_in
        rest = [char for char in line if char not in used]
        for phrase in _phrases(rest, settings):
            phrases.append(phrase.printed)
            if "".join(phrase.plain.split()).upper() in _CENTRE_LINE_MARKS:
                marks.append(phrase.box(0, len(phrase.plain)))
            dimensions.extend(_dimensions(phrase, orientation, settings))
    return PrintedText(
        tuple(_beside_other_sizes(dimension, readable, settings) for dimension in dimensions),
        tuple(scales),
        tuple(phrases),
        tuple(marks),
    )


def _beside_other_sizes(
    dimension: PrintedDimension, chars: Sequence[TextChar], settings: TextSettings
) -> PrintedDimension:
    """Hold a dimension printed right against text of another size.

    A stacked fraction's whole number, numerator and denominator are set at different sizes and
    heights, so its pieces land on different lines: `21` on one, `7` above, `8"` below. Read alone,
    the denominator is a plausible wrong number (`8"`). Any character of another size touching the
    label along its line, and overlapping it across, holds it.
    """
    if not dimension.chars or dimension.reading.held_reason is not None:
        return dimension
    height = max(_height(char) for char in dimension.chars)
    reach = settings.phrase_em * height
    box = dimension.box
    for char in chars:
        if char in dimension.chars or char.orientation is not dimension.orientation:
            continue
        if abs(_height(char) - height) <= height * settings.same_size_fraction:
            continue
        if char.box.x0 > box.x1 + reach or char.box.x1 < box.x0 - reach:
            continue
        if char.box.top > box.bottom + reach or char.box.bottom < box.top - reach:
            continue
        across = (
            char.box.top < box.bottom and char.box.bottom > box.top
            if dimension.orientation is Orientation.UPRIGHT
            else char.box.x0 < box.x1 and char.box.x1 > box.x0
        )
        if across:
            return replace(
                dimension,
                reading=_held(
                    dimension.reading,
                    "it is printed against text of another size: part of a stacked fraction",
                ),
            )
    return dimension


def centre(box: Box) -> tuple[Decimal, Decimal]:
    return ((box.x0 + box.x1) / _TWO, (box.top + box.bottom) / _TWO)
