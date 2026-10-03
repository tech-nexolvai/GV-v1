"""What one part of a drawing is: a cabinet, a filler or a countertop (#852).

A *part* is one physical thing a person can point at on an elevation. The computer may suggest one
(`part_proposals`); only a person's confirmation turns it into a drawing item (`part_confirmations`).
Both record a kind, and this is the closed list they record it from.

**Here rather than in `app/models/`** for the reason the package docstring gives: naming a concept
must not mean importing the code that stores it. The code that will suggest parts reads the drawing,
and `extraction/` does not import `app/`.

**A part's kind is not its item type.** `drawing_items.item_type` holds a `SemanticType`, and the
code that already reasons about runs types a cabinet and a filler by the generic width each one
carries: `extraction/model/assembly.py` counts `CABINET_WIDTH` and `FILLER_WIDTH` items as members
of a run and does not count `COUNTERTOP_OVERALL_WIDTH`. `PartKind.item_type` gives that member, so an
item a person confirmed is one that resolver can read without a translation table of its own.

The value strings are stored on suggestions and confirmations, so they are part of the data contract:
renaming one invalidates stored rows. Treat them as fixed.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import Final

from vocabulary.semantic_types import SemanticType

__all__ = ["PartKind"]


class PartKind(StrEnum):
    """The three kinds of part a countertop check is about."""

    CABINET = "cabinet"
    FILLER = "filler"
    COUNTERTOP = "countertop"

    @property
    def item_type(self) -> SemanticType:
        """The `drawing_items.item_type` a confirmed part of this kind is stored under.

        The generic type, never a positional `CT0xx` code. `CT003` is the *left* cabinet of the
        client's three-cabinet layout, and a confirmation says what a part is, not where it sits in a
        layout — choosing `CT003` here would claim every cabinet is the left one.
        """
        return _ITEM_TYPES[self]


_ITEM_TYPES: Final[MappingProxyType[PartKind, SemanticType]] = MappingProxyType(
    {
        PartKind.CABINET: SemanticType.CABINET_WIDTH,
        PartKind.FILLER: SemanticType.FILLER_WIDTH,
        PartKind.COUNTERTOP: SemanticType.COUNTERTOP_OVERALL_WIDTH,
    }
)
