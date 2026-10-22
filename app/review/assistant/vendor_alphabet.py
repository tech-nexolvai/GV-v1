"""The vendor's own digit shapes, learned from the set's confirmed labels: $0, no AI (#1860).

A vendor's drawings are drawn by one CAD program in one pen-stroke font, so every `4` it draws is
the same set of strokes. Where the drawn route has confirmed a number (two OCR engines agreeing,
`dim_labels`), its characters' strokes are samples of that font: the `2` of a confirmed `24"` is
a `2`. This module keeps those samples (the **alphabet**) and names the characters of a number no
reader confirmed by the nearest learned shape.

**A sample** is one character's centre-lines in its reading frame, each path resampled to
`POINTS` points and divided by the character's height, with its width-to-height ratio; two
samples are compared only when they have the same number of strokes, by the largest distance
between matching points (a stroke may be drawn either way round) plus the ratios' difference.

**Naming a character:** the nearest sample within `NEAR` names it, unless a sample of another
digit is within `NEAR + MARGIN` too (then it abstains). **Reading a number:** a one-row label (its
whole number and at most one stacked fraction at the end, its marks left out) is read only when
every character is named; the digits are laid out as the vectors draw them. Anything less is no
reading.

**A second witness only.** `read_number` counts the alphabet only beside its OCR pair: the two
agreeing is a reading, the two differing is not, the alphabet alone never is. Measured on the
large test set's 302 confirmed vendor digits (local, 2026-10-22): 14 shapes, none naming two
digits; each page named by the other pages only: 299 right, 0 wrong, 3 abstained.

Verification: `tests/review/assistant/test_vendor_alphabet.py` (made-up strokes).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final

import numpy as np

from app.review.assistant import ink_labels

__all__ = ["MARGIN", "NEAR", "POINTS", "Alphabet", "Sample", "describe", "samples_of"]

#: Points each stroke is resampled to.
POINTS: Final = 16
#: A character is named by a sample nearer than this (heights), and abstains when a sample of
#: another digit is within this plus `MARGIN`.
NEAR: Final = 0.12
MARGIN: Final = 0.05
#: Denominators a drawing uses.
_DENOMINATORS: Final = frozenset({2, 4, 8, 16, 32, 64})


@dataclass(frozen=True, slots=True)
class Sample:
    """One character's strokes (`describe`) and, when learned, its digit and page."""

    strokes: int
    points: tuple[tuple[float, float], ...]
    ratio: float
    digit: str = ""
    page: int = 0

    def as_list(self) -> list[Any]:
        return [self.strokes, [list(p) for p in self.points], self.ratio, self.digit, self.page]

    @classmethod
    def from_list(cls, raw: Sequence[Any]) -> Sample:
        strokes, points, ratio, digit, page = raw
        return cls(
            int(strokes),
            tuple((float(x), float(y)) for x, y in points),
            float(ratio),
            str(digit),
            int(page),
        )


def describe(glyph: ink_labels.Glyph, turn: int) -> Sample:
    """A character's strokes in its reading frame (`turn`), as a sample with no digit."""
    frame = ink_labels._frame(glyph.box, turn)  # type: ignore[arg-type]
    height = max(frame[3] - frame[1], 1e-6)
    parts: list[np.ndarray] = []
    paths = sorted(
        glyph.paths,
        key=lambda p: (
            ink_labels._frame(p.box, turn)[0],  # type: ignore[arg-type]
            ink_labels._frame(p.box, turn)[1],  # type: ignore[arg-type]
        ),
    )
    for path in paths:
        for line in path.polylines:
            pts = np.array([ink_labels._point(x, y, turn) for x, y in line], float)  # type: ignore[arg-type]
            if len(pts) == 0:
                continue
            if len(pts) < 2:
                pts = np.vstack([pts, pts])
            steps = np.linalg.norm(np.diff(pts, axis=0), axis=1)
            along = np.concatenate([[0.0], np.cumsum(steps)])
            if along[-1] <= 0:
                res = np.repeat(pts[:1], POINTS, axis=0)
            else:
                at = np.linspace(0.0, along[-1], POINTS)
                res = np.stack(
                    [np.interp(at, along, pts[:, 0]), np.interp(at, along, pts[:, 1])], 1
                )
            parts.append((res - np.array([frame[0], frame[1]])) / height)
    every = np.concatenate(parts) if parts else np.zeros((0, 2))
    return Sample(
        len(parts),
        tuple((round(float(x), 4), round(float(y), 4)) for x, y in every),
        round((frame[2] - frame[0]) / height, 3),
    )


def _distance(a: Sample, b: Sample) -> float:
    if a.strokes != b.strokes or len(a.points) != len(b.points) or not a.points:
        return 9.0
    pa, pb = np.array(a.points), np.array(b.points)
    near = float(np.abs(pa - pb).max())
    if a.strokes == 1:
        near = min(near, float(np.abs(pa[::-1] - pb).max()))
    return near + abs(a.ratio - b.ratio)


def _characters(
    label: ink_labels.Label, turn: int
) -> tuple[list[ink_labels.Glyph], list[ink_labels.Glyph], list[ink_labels.Glyph]] | None:
    """The one row's whole-number, numerator and denominator characters (marks left out), or
    `None` when the label is not one row with at most one stacked fraction at its end."""
    rows = ink_labels.rows(label, turn)  # type: ignore[arg-type]
    if len(rows) != 1:
        return None
    whole: list[ink_labels.Glyph] = []
    numerator: list[ink_labels.Glyph] = []
    denominator: list[ink_labels.Glyph] = []
    for token in rows[0]:
        if token.kind == "mark":
            continue
        if numerator:  # a character after the fraction: not one plain dimension
            return None
        if token.kind == "fraction":
            numerator = sorted(token.glyphs, key=lambda g: ink_labels._frame(g.box, turn)[0])  # type: ignore[arg-type]
            denominator = sorted(token.below, key=lambda g: ink_labels._frame(g.box, turn)[0])  # type: ignore[arg-type]
        else:
            whole.extend(token.glyphs)
    if not whole and not numerator:
        return None
    return whole, numerator, denominator


def _layout(value: Decimal) -> tuple[str, str, str] | None:
    """A value's digits as a drawing writes them: whole, numerator, denominator."""
    whole = int(value)
    rest = value - whole
    if rest == 0:
        return str(whole), "", ""
    for denominator in sorted(_DENOMINATORS):
        top = rest * denominator
        if top == top.to_integral_value():
            return (str(whole) if whole else ""), str(int(top)), str(denominator)
    return None


def samples_of(label: ink_labels.Label, turn: int, value: Decimal, page: int) -> list[Sample]:
    """A confirmed label's characters as learned samples: only when its vectors draw exactly as
    many characters as the value has digits in each place (whole, numerator, denominator)."""
    found = _characters(label, turn)
    digits = _layout(value)
    if found is None or digits is None:
        return []
    if tuple(len(part) for part in found) != tuple(len(part) for part in digits):
        return []
    out: list[Sample] = []
    for glyphs, part in zip(found, digits, strict=True):
        for glyph, digit in zip(glyphs, part, strict=True):
            shape = describe(glyph, turn)
            out.append(Sample(shape.strokes, shape.points, shape.ratio, digit, page))
    return out


@dataclass(slots=True)
class Alphabet:
    """The vendor's learned samples (`samples_of` over the set's confirmed labels)."""

    samples: list[Sample] = field(default_factory=list)

    def name(self, shape: Sample, *, skip_page: int = 0) -> str | None:
        """The digit the nearest learned sample names, or `None` (none near enough, or another
        digit nearly as near). `skip_page`: leave that page's samples out (a check)."""
        best: dict[str, float] = {}
        for sample in self.samples:
            if skip_page and sample.page == skip_page:
                continue
            gap = _distance(shape, sample)
            if gap < best.get(sample.digit, 9.0):
                best[sample.digit] = gap
        ranked = sorted(best.items(), key=lambda kv: kv[1])
        if not ranked or ranked[0][1] >= NEAR:
            return None
        if len(ranked) > 1 and ranked[1][1] < NEAR + MARGIN:
            return None
        return ranked[0][0]

    def read(self, label: ink_labels.Label, turn: int) -> Decimal | None:
        """The value of a one-row label when every character is named, laid out as the vectors
        draw it; else `None`."""
        if not self.samples:
            return None
        found = _characters(label, turn)
        if found is None:
            return None
        named: list[str] = []
        for glyphs in found:
            part = ""
            for glyph in glyphs:
                digit = self.name(describe(glyph, turn))
                if digit is None:
                    return None
                part += digit
            named.append(part)
        whole, top, bottom = named
        value = Decimal(int(whole)) if whole else Decimal(0)
        if top or bottom:
            if not top or not bottom or int(bottom) not in _DENOMINATORS:
                return None
            if not 0 < int(top) < int(bottom):
                return None
            value += Decimal(int(top)) / Decimal(int(bottom))
        return value if value > 0 else None

    def as_json(self) -> dict[str, Any]:
        return {"near": NEAR, "samples": [sample.as_list() for sample in self.samples]}

    @classmethod
    def from_json(cls, raw: Mapping[str, Any] | None) -> Alphabet:
        if not isinstance(raw, Mapping):
            return cls()
        out: list[Sample] = []
        for item in raw.get("samples") or []:
            try:
                out.append(Sample.from_list(item))
            except (TypeError, ValueError):
                continue
        return cls(out)
