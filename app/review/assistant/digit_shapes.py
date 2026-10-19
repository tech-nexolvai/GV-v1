"""The shape matcher: a drawn number's digits matched to known fonts' digits, no machine learning.

One of `read_number`'s independent routes (the September glyph research, #756: vendor numbers
are CAD single-stroke lettering, one PDF path per character). Each character of a drawn label is
drawn from its own vectors on a small picture, thinned to its centre-line and compared with the
centre-lines of the ten digits of a few known fonts (`digit_shapes.json`, made once by
`build_templates` from the Hershey single-stroke fonts, a CAD stroke `1` and the free Bitstream
Vera font that ships with reportlab):

- **digits only** (letters caused most of the earlier prototype's misreads);
- first the **number of closed loops** must match (a 6 has one, an 8 two): the 6/8 confusion;
- then the **chamfer distance** between the two centre-lines, plus how far their width-to-height
  ratios differ;
- a character split across strokes (a `4` stored as an angle and an upright) is one character
  when its pieces overlap along the row;
- **any unsure character means no reading** (too far from every digit, or too close to two):
  this route abstains far more often than it guesses.

A stacked fraction's numerator and denominator are matched the same way; the value is valid only
with a denominator a drawing uses (2 to 64) and a numerator below it. Pure: numpy and OpenCV, $0.

Source: `READ-HANDDRAWN-RESEARCH.md` §3 (local, 2026-10-19) · Verification:
`tests/review/assistant/test_read_number.py`
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

import cv2
import numpy as np

from app.review.assistant import ink_labels

__all__ = [
    "MAX_DISTANCE",
    "MIN_MARGIN",
    "TEMPLATES_FILE",
    "Template",
    "build_templates",
    "shape_read",
]

#: The templates, made by `build_templates` (`python -m app.review.assistant.digit_shapes`).
TEMPLATES_FILE: Final = Path(__file__).with_name("digit_shapes.json")
#: The picture side a character and a template are compared at (pixels).
_SIDE: Final = 32
DIGITS: Final = "0123456789"
#: A character further than this from its best digit is unsure.
MAX_DISTANCE: Final = 3.0
#: A character whose best digit beats the next by less than this is unsure.
MIN_MARGIN: Final = 0.25
#: How much a different width-to-height ratio counts against a match.
_ASPECT_WEIGHT: Final = 2.0
_DENOMINATORS: Final = frozenset({2, 4, 8, 16, 32, 64})


@dataclass(frozen=True, slots=True)
class Template:
    """One font's digit: its centre-line at `_SIDE` pixels, its width-to-height ratio and its
    closed loops."""

    digit: str
    font: str
    skeleton: np.ndarray
    aspect: float
    loops: int


# ---- pictures --------------------------------------------------------------------------------------


def thin(picture: np.ndarray) -> np.ndarray:
    """Zhang-Suen thinning of a 0/1 picture: its centre-line, one pixel wide."""
    img = (picture > 0).astype(np.uint8)
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            pad = np.pad(img, 1)
            p2, p3, p4, p5 = pad[:-2, 1:-1], pad[:-2, 2:], pad[1:-1, 2:], pad[2:, 2:]
            p6, p7, p8, p9 = pad[2:, 1:-1], pad[2:, :-2], pad[1:-1, :-2], pad[:-2, :-2]
            around = [p2, p3, p4, p5, p6, p7, p8, p9]
            count = sum(x.astype(int) for x in around)
            ring = [*around, p2]
            turns = sum(((ring[i] == 0) & (ring[i + 1] == 1)).astype(int) for i in range(8))
            if step == 0:
                edge = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                edge = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            gone = (img == 1) & (count >= 2) & (count <= 6) & (turns == 1) & edge
            if gone.any():
                img[gone] = 0
                changed = True
    return img


def _fit(ink: np.ndarray, side: int = _SIDE, pad: int = 2) -> tuple[np.ndarray, float]:
    """The ink cropped and scaled (keeping its shape) into a `side` square; its width/height."""
    ys, xs = np.nonzero(ink)
    if len(xs) == 0:
        return np.zeros((side, side), np.uint8), 1.0
    crop = ink[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1].astype(np.uint8)
    tall, wide = crop.shape
    scale = (side - 2 * pad) / max(tall, wide)
    new_tall, new_wide = max(1, round(tall * scale)), max(1, round(wide * scale))
    small = cv2.resize(crop * 255, (new_wide, new_tall), interpolation=cv2.INTER_AREA) > 60
    out = np.zeros((side, side), np.uint8)
    top, left = (side - new_tall) // 2, (side - new_wide) // 2
    out[top : top + new_tall, left : left + new_wide] = small
    return out, wide / tall


def loops(ink: np.ndarray) -> int:
    """Closed loops in a glyph picture (0, 4, 6, 9 one; 8 two)."""
    picture = cv2.dilate((ink > 0).astype(np.uint8) * 255, np.ones((3, 3), np.uint8))
    contours, tree = cv2.findContours(picture, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
    if tree is None:
        return 0
    return int(
        sum(
            1
            for i, node in enumerate(tree[0])
            if node[3] >= 0 and cv2.contourArea(contours[i]) >= 20
        )
    )


def chamfer(a: np.ndarray, b: np.ndarray) -> float:
    """The mean distance from each centre-line pixel of one picture to the other's, both ways."""
    pa, pb = np.argwhere(a), np.argwhere(b)
    if len(pa) == 0 or len(pb) == 0:
        return 99.0
    to_b = cv2.distanceTransform((1 - b).astype(np.uint8), cv2.DIST_L2, 3)
    to_a = cv2.distanceTransform((1 - a).astype(np.uint8), cv2.DIST_L2, 3)
    return float(0.5 * (to_b[pa[:, 0], pa[:, 1]].mean() + to_a[pb[:, 0], pb[:, 1]].mean()))


# ---- templates -------------------------------------------------------------------------------------


def _template(digit: str, font: str, ink: np.ndarray) -> Template:
    big = cv2.resize(ink.astype(np.uint8) * 255, (100, 100), interpolation=cv2.INTER_AREA) > 60
    picture, aspect = _fit(ink)
    return Template(digit, font, thin(picture), aspect, loops(big.astype(np.uint8)))


def _hershey(digit: str, face: int) -> np.ndarray:
    img = np.zeros((200, 200), np.uint8)
    cv2.putText(img, digit, (40, 150), face, 4.0, 1, 6, cv2.LINE_8)
    return img


def _outline(digit: str, path: str) -> np.ndarray:
    from PIL import Image, ImageDraw, ImageFont

    font = ImageFont.truetype(path, 120)
    picture = Image.new("L", (220, 220), 0)
    ImageDraw.Draw(picture).text((40, 20), digit, font=font, fill=255)
    return (np.array(picture) > 127).astype(np.uint8)


#: The Hershey single-stroke fonts OpenCV draws (a CAD font's centre-lines).
_HERSHEY: Final = (
    ("hershey_simplex", cv2.FONT_HERSHEY_SIMPLEX),
    ("hershey_plain", cv2.FONT_HERSHEY_PLAIN),
    ("hershey_duplex", cv2.FONT_HERSHEY_DUPLEX),
)


#: CAD single-stroke shapes the fonts above lack, drawn from their strokes (x, y as shares of
#: the height, y down): the `1` of a CAD sans font, a flag and a stem with no foot (Hershey's `1`
#: has a foot, a free outline font's a foot or a thick flag).
_STROKES: Final[dict[str, tuple[tuple[tuple[float, float], ...], ...]]] = {
    "1": (((0.0, 0.3), (0.32, 0.0), (0.32, 1.0)),),
}


def _stroked(digit: str) -> np.ndarray | None:
    lines = _STROKES.get(digit)
    if lines is None:
        return None
    picture = np.zeros((200, 200), np.uint8)
    for line in lines:
        drawn = np.array([(40 + 150 * x, 25 + 150 * y) for x, y in line]).round().astype(np.int32)
        cv2.polylines(picture, [drawn.reshape(-1, 1, 2)], False, 1, 6)
    return picture


def build_templates(outline_fonts: Iterable[tuple[str, str]] = ()) -> list[Template]:
    """The digit templates: the Hershey fonts, the CAD strokes (`_STROKES`), plus each
    `(name, ttf path)` outline font."""
    fonts = list(outline_fonts)
    out: list[Template] = []
    for digit in DIGITS:
        for name, face in _HERSHEY:
            out.append(_template(digit, name, _hershey(digit, face)))
        stroked = _stroked(digit)
        if stroked is not None:
            out.append(_template(digit, "cad_stroke", stroked))
        for name, path in fonts:
            out.append(_template(digit, name, _outline(digit, path)))
    return out


def _packed(template: Template) -> dict[str, Any]:
    return {
        "digit": template.digit,
        "font": template.font,
        "aspect": round(template.aspect, 6),
        "loops": template.loops,
        "skeleton": np.packbits(template.skeleton.astype(np.uint8)).tobytes().hex(),
    }


def _unpacked(raw: dict[str, Any]) -> Template:
    bits = np.unpackbits(np.frombuffer(bytes.fromhex(str(raw["skeleton"])), np.uint8))
    skeleton = bits[: _SIDE * _SIDE].reshape(_SIDE, _SIDE).astype(np.uint8)
    return Template(
        str(raw["digit"]), str(raw["font"]), skeleton, float(raw["aspect"]), int(raw["loops"])
    )


_LOADED: list[Template] | None = None
_LOCK: Final = threading.Lock()


def templates() -> list[Template]:
    """The committed templates (`digit_shapes.json`), loaded once; the Hershey fonts alone when
    the file is missing."""
    global _LOADED
    with _LOCK:
        if _LOADED is None:
            try:
                document = json.loads(TEMPLATES_FILE.read_text(encoding="utf-8"))
                _LOADED = [_unpacked(raw) for raw in document["templates"]]
            except (OSError, ValueError, KeyError):
                _LOADED = build_templates()
        return _LOADED


# ---- reading one label -----------------------------------------------------------------------------


def _ink_of(glyphs: Sequence[ink_labels.Glyph], turn: ink_labels.Turn) -> np.ndarray:
    """The glyphs' polylines turned into the reading frame, drawn on a 100-pixel canvas."""
    lines = [
        [ink_labels._point(x, y, turn) for x, y in line]
        for glyph in glyphs
        for path in glyph.paths
        for line in path.polylines
        if line
    ]
    points = np.array([point for line in lines for point in line], float)
    low = points.min(0)
    span = float(max(points.max(0) - low))
    scale = 90.0 / max(span, 1e-6)
    picture = np.zeros((100, 100), np.uint8)
    for line in lines:
        drawn = ((np.array(line) - low) * scale + 5).round().astype(np.int32)
        if len(drawn) == 1:
            cv2.circle(picture, (int(drawn[0][0]), int(drawn[0][1])), 1, 1, -1)
        else:
            cv2.polylines(picture, [drawn.reshape(-1, 1, 2)], False, 1, 3)
    return picture


@dataclass(frozen=True, slots=True)
class CharacterMatch:
    """One character's best digit, how far it is, and the margin to the next digit."""

    digit: str
    distance: float
    margin: float
    runner_up: str

    @property
    def sure(self) -> bool:
        return self.distance <= MAX_DISTANCE and self.margin >= MIN_MARGIN


def match_character(
    glyphs: Sequence[ink_labels.Glyph],
    turn: ink_labels.Turn,
    known: Sequence[Template] | None = None,
) -> CharacterMatch:
    """The digit one character's glyphs are closest to (same loop count first)."""
    ink = _ink_of(glyphs, turn)
    count = loops(ink)
    picture, aspect = _fit(ink)
    skeleton = thin(picture)
    best: dict[str, float] = {}
    for template in templates() if known is None else known:
        if template.loops != count:
            continue
        distance = chamfer(skeleton, template.skeleton) + _ASPECT_WEIGHT * abs(
            float(np.log(max(aspect, 0.05) / max(template.aspect, 0.05)))
        )
        best[template.digit] = min(best.get(template.digit, 99.0), distance)
    ranked = [*sorted(best.items(), key=lambda kv: kv[1]), ("?", 99.0), ("?", 99.0)]
    (first, d1), (second, d2) = ranked[0], ranked[1]
    return CharacterMatch(first, d1, d2 - d1, second)


def _characters(
    glyphs: Sequence[ink_labels.Glyph], turn: ink_labels.Turn
) -> list[list[ink_labels.Glyph]]:
    """Glyphs whose extents along the row overlap by half the narrower are one character (a `4`
    stored as an upright and an angle)."""
    placed = sorted(((g, ink_labels._frame(g.box, turn)) for g in glyphs), key=lambda p: p[1][0])
    groups: list[list[ink_labels.Glyph]] = []
    extents: list[tuple[float, float]] = []
    for glyph, frame in placed:
        if groups:
            low, high = extents[-1]
            narrow = min(high - low, frame[2] - frame[0])
            size = max(high - low, frame[2] - frame[0], 0.3)
            if min(high, frame[2]) - max(low, frame[0]) >= 0.5 * narrow - 0.05 * size:
                groups[-1].append(glyph)
                extents[-1] = (min(low, frame[0]), max(high, frame[2]))
                continue
        groups.append([glyph])
        extents.append((frame[0], frame[2]))
    return groups


@dataclass(frozen=True, slots=True)
class ShapeReading:
    """The value the shapes spell, or `None` with why."""

    value: Decimal | None
    why: str
    characters: tuple[CharacterMatch, ...] = ()


def _spell(
    glyphs: Sequence[ink_labels.Glyph], turn: ink_labels.Turn, found: list[CharacterMatch]
) -> str | None:
    out = ""
    for character in _characters(glyphs, turn):
        match = match_character(character, turn)
        found.append(match)
        if not match.sure:
            return None
        out += match.digit
    return out


def shape_read(label: ink_labels.Label, turn: ink_labels.Turn) -> ShapeReading:
    """The value the label's shapes spell read as `turn`, or none: one row with a whole number
    and at most one stacked fraction, every character sure."""
    found: list[CharacterMatch] = []
    spelled: list[tuple[str, tuple[str, str] | None]] = []
    for row in ink_labels.rows(label, turn):
        whole_glyphs = [g for token in row if token.kind == "glyph" for g in token.glyphs]
        fraction: tuple[str, str] | None = None
        for token in row:
            if token.kind != "fraction":
                continue
            top = _spell(token.glyphs, turn, found)
            bottom = _spell(token.below, turn, found)
            if top is None or bottom is None:
                return ShapeReading(None, "a character of the fraction is unsure", tuple(found))
            fraction = (top, bottom)
        whole = _spell(whole_glyphs, turn, found) if whole_glyphs else ""
        if whole is None:
            last = found[-1]
            return ShapeReading(
                None,
                f"a character is unsure ({last.digit} or {last.runner_up})",
                tuple(found),
            )
        if whole or fraction:
            spelled.append((whole, fraction))
    if len(spelled) != 1:
        return ShapeReading(None, "not one row of digits", tuple(found))
    whole, fraction = spelled[0]
    if whole.startswith("0") and whole != "0":
        return ShapeReading(None, "a leading zero, which no dimension has", tuple(found))
    value = Fraction(int(whole)) if whole else Fraction(0)
    if fraction is not None:
        top, bottom = int(fraction[0] or 0), int(fraction[1] or 0)
        if bottom not in _DENOMINATORS or not 0 < top < bottom:
            return ShapeReading(None, f"a fraction {top}/{bottom} no drawing prints", tuple(found))
        value += Fraction(top, bottom)
    if value <= 0:
        return ShapeReading(None, "zero", tuple(found))
    return ShapeReading(
        Decimal(value.numerator) / Decimal(value.denominator), "every character sure", tuple(found)
    )


def main() -> int:
    """Write `digit_shapes.json` from the Hershey fonts and the free outline fonts found."""
    import reportlab  # the Bitstream Vera font ships with it (a free licence)

    vera = Path(reportlab.__file__).parent / "fonts" / "Vera.ttf"
    made = build_templates([("vera", str(vera))])
    TEMPLATES_FILE.write_text(
        json.dumps({"version": 1, "templates": [_packed(t) for t in made]}, indent=0) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {len(made)} templates to {TEMPLATES_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
