"""A typed width or length is more than zero, or it is refused (#1182).

**Why.** A countertop row passes when its pieces add up to the overall width. A person who typed
`0 in` for a piece they could not read, or a stray minus sign, made the sum come out right by
accident: the 2026-10-11 audit proved `0 | 21 | 18` against an overall of 39 passing. A piece of a
real countertop is never zero or negative wide, so such a value is a mistake, and a mistake must
not be able to reach a PASS.

**Where.** Every place a person types one: a slot-reader row's width (`app/api/slot_rows.py`), a
value saved in Measurements for an input of one of the types below (`app/api/measurements.py`), and
a reviewer's correction of a reading of one of them (`app/api/review.py`). Each answers 422 with
`NOT_ABOVE_ZERO`. The width check refuses such a piece on its own as well
(`verdict/operations/aggregate.py:row_total`), so a value from any other path cannot pass either.

**Which types.** The sizes of real things: the countertop, its pieces, cabinets, fillers, the sink
cut-out and the walls around them. Settings (a tolerance, a field cut, a clearance) are not here:
zero is a real answer for them ("exact", "no field cut").
"""

from __future__ import annotations

from typing import Final

from vocabulary.semantic_types import SemanticType

#: The semantic types whose value is the size of a real thing, and so is never zero or negative.
POSITIVE_SIZE_TYPES: Final[frozenset[str]] = frozenset(
    member.value
    for member in (
        SemanticType.COUNTERTOP_OVERALL_WIDTH,
        SemanticType.COUNTERTOP_PIECE_WIDTH,
        SemanticType.CABINET_WIDTH,
        SemanticType.FILLER_WIDTH,
        SemanticType.WALL_TO_WALL_DIMENSION,
        SemanticType.FIELD_DIMENSION,
        SemanticType.COUNTERTOP_DEPTH,
        SemanticType.SINK_CUTOUT_WIDTH,
        SemanticType.SINK_CUTOUT_DEPTH,
        SemanticType.CT004,  # the sink cabinet's width
    )
)

#: The refusal, in the words the screen shows.
NOT_ABOVE_ZERO: Final = (
    "A width or length must be more than zero. If you cannot read it, leave it empty: a missing "
    "piece is never zero."
)


def is_positive_size(semantic_type: str | None) -> bool:
    """Whether a value of this type must be more than zero."""
    return semantic_type in POSITIVE_SIZE_TYPES


def typed_negative(text: str) -> bool:
    """Whether a typed value starts with a minus sign.

    Checked before parsing, because the parser reads `-1 in` as a value with no unit and would
    answer with a message about units; the person should be told the real problem.
    """
    return text.strip().startswith(("-", "\u2212"))
