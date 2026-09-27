"""Whether a cluster of glyphs was drawn on one line or stacked (#541).

**The failure this exists to catch.** On a real crop from `AI_Set 2` page 3 a label reading
`28 3/4"` came back from the vision reader as `284`, and the seam accepted it — correctly, because
`284` passes `units.normalise.normalise_to_inches`. It is a well-formed dimension token. No
validator reading the string has grounds to refuse it, and a wrong dimension that parses is the
critical false-PASS shape: it enters exact arithmetic and passes.

The opposite direction is already caught. `28 1/2"` read as `1/2` is refused by the bare-fraction
guard in `extraction/models/validation.py`, because a fraction with no whole number is suspect on
its face. `284` is not suspect on its face, so the only thing left that knows better is the drawing.

**So this asks the sheet, not the model.** A stacked fraction is *drawn* as glyphs at two distinct
heights with a bar between them. If a region's glyph boxes occupy two bands and the reading contains
no `/`, something was absorbed. That is a deterministic cross-check of what came back against what
was drawn: it asks the model nothing, it never decides what the number is, and a mismatch abstains
rather than correcting.

**No default separation, and that is the point.** What vertical gap counts as "stacked" without
false-flagging a legitimate single-line reading cannot be settled from the one sheet we hold — it is
the same gate `#179` sits behind, and the reason `line_minimum_pt`, `glyph_gap_pt` and
`proximity_limit` are all argument-only. A number fitted to `AI_Set 2` would look like a working
detector and would be a guess with a test around it. Validate it against the real set (#274).

Source: issue #541. Verification: tests/extraction/test_glyph_bands.py.
"""

from __future__ import annotations

from decimal import Decimal

__all__ = [
    "BandSeparationError",
    "GlyphBox",
    "reading_must_contain_a_fraction",
    "stacked_pairs",
]

#: One glyph's bounding box as `annotations.py` holds it while clustering: `(x0, y0, x1, y1)` in the
#: page's own units, y increasing upward as PDF space does.
type GlyphBox = tuple[Decimal, Decimal, Decimal, Decimal]


class BandSeparationError(ValueError):
    """The separation threshold was not supplied as a usable positive distance."""


def _checked(separation_pt: Decimal) -> Decimal:
    if not isinstance(separation_pt, Decimal) or not separation_pt.is_finite():
        raise BandSeparationError(
            "separation_pt must be a finite Decimal. It has no default: what gap counts as stacked "
            "cannot be set from one sheet (#541, #274)."
        )
    if separation_pt <= 0:
        raise BandSeparationError(
            f"separation_pt must be positive, got {separation_pt}. A zero or negative gap makes "
            "any two glyphs on one line look stacked."
        )
    return separation_pt


def stacked_pairs(
    boxes: list[GlyphBox] | tuple[GlyphBox, ...],
    *,
    separation_pt: Decimal,
    rotation_degrees: int = 0,
) -> int:
    """How many pairs of glyphs sit one above the other rather than side by side.

    **Not a count of horizontal bands, and the difference is the whole mechanism.** The first
    version of this projected every glyph onto the across-baseline axis and counted the groups. It
    finds one band on `28 3/4`, because the full-height `2` and `8` span the numerator's row *and*
    the denominator's — the whole number bridges the gap the fraction makes. A test with a realistic
    label caught it; a test with only the fraction in it would not have.

    What actually distinguishes a stacked fraction is a **pair**: two glyphs that overlap *along*
    the baseline — they occupy the same run of the line — while being clear of each other *across*
    it. The `3` and the `4` of `28 3/4` do. No two glyphs of `2' - 10"` do, however wide the label.

    Along-baseline overlap is required rather than merely allowed, because that is what says the two
    glyphs are competing for one position in the reading order. Two characters side by side never
    do, and a `,` dipping below its neighbour overlaps them along the line but not clear of it.
    """
    gap = _checked(separation_pt)
    if len(boxes) < 2:
        return 0

    turned = rotation_degrees % 180 != 0
    # `(along_low, along_high, across_low, across_high)` — the baseline's own axes, so a label that
    # has turned a quarter turn is measured the way it is read rather than the way the page runs.
    spans = [
        (box[1], box[3], box[0], box[2]) if turned else (box[0], box[2], box[1], box[3])
        for box in boxes
    ]

    found = 0
    for index, (along_low, along_high, across_low, across_high) in enumerate(spans):
        for other_low, other_high, other_across_low, other_across_high in spans[index + 1 :]:
            overlaps_along = along_low < other_high and other_low < along_high
            clear_across = (
                other_across_low - across_high >= gap or across_low - other_across_high >= gap
            )
            if overlaps_along and clear_across:
                found += 1
    return found


def reading_must_contain_a_fraction(
    boxes: list[GlyphBox] | tuple[GlyphBox, ...],
    *,
    separation_pt: Decimal,
    rotation_degrees: int = 0,
) -> bool:
    """Whether what was drawn obliges the reading to carry a `/`.

    One stacked pair is enough. A simple fraction contributes exactly one — its numerator and its
    denominator — and asking for more would miss `28 3/4`, which is the case this exists for.
    """
    return stacked_pairs(boxes, separation_pt=separation_pt, rotation_degrees=rotation_degrees) > 0
