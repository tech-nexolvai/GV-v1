"""An item on a drawing: the thing a rule is about.

Every rule that sums cabinets needs items to sum. A countertop, a base cabinet, a filler — each is
one item, found in one view, with an extent on the page.

**An item is a candidate, never a fact.** Items come from reading a drawing, which is the AI's job,
and `AGENTS.md` §2.1 keeps reading separate from deciding. If an item could be constructed already
corroborated, the drawing model would become a second route into the verdict that bypasses the
evidence gate — so the constructor refuses it outright rather than defaulting and hoping. Promotion
is `evidence/`'s job and happens by building a new item, not by mutating this one.

**The type comes from the vocabulary, not from a string.** `SemanticType` is a controlled list
anchored to an annotated diagram (ADR-0017). A free string would let `"countertop_widht"` be
extracted, stored, matched against nothing, and never noticed.

**One item belongs to one view.** Recognising that the item in elevation D and the item in plan E
are the same physical cabinet is cross-view identity — `B7.3`'s problem, deliberately not
representable here. A nullable "same as" field would invite it to be guessed, and guessing which
cabinet a dimension belongs to is exactly how a finding becomes internally consistent and completely
wrong.

**Why `vocabulary` and not `rules`.** `docs/DESIGN_EXTRACTION.md` §2 forbids `extraction/` from
importing `rules/`: an extractor that knows which rule is coming can be tuned to satisfy it. The
vocabulary moved to `vocabulary/` so naming a concept no longer means importing the rule engine.

Source: backend proposal §10.1 · Design: `docs/DESIGN_EXTRACTION.md` §4.1 ·
Verification: `tests/extraction/model/test_items.py`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID, uuid4

from evidence.polygon import Polygon
from vocabulary.semantic_types import SemanticType


class ItemCorroborationError(ValueError):
    """Something tried to construct an item that was already corroborated.

    Refused rather than ignored. An item is AI output until two independent routes agree, and a type
    that accepted `corroborated=True` from its caller would let any code path mint a fact.
    """


@dataclass(frozen=True, slots=True)
class ViewIdentity:
    """Which view an item was found in — the page, and the tag printed on it.

    Identity is the pair. Sheets reuse `D`, `E`, `F` page after page, so a tag alone identifies
    nothing, and treating it as identity would merge two elevations from different sheets — after
    which every item beneath them belongs to the wrong drawing.
    """

    document_version_id: UUID
    page: int
    tag: str

    def __post_init__(self) -> None:
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if isinstance(self.page, bool) or not isinstance(self.page, int):
            raise TypeError("page must be an integer")
        if self.page < 0:
            raise ValueError("page must be zero or greater")
        if not isinstance(self.tag, str) or not self.tag.strip():
            raise ValueError("a view tag must be the non-empty text printed on the sheet")


@dataclass(frozen=True, slots=True)
class DrawingItem:
    """One item found on a drawing, uncorroborated by construction."""

    view: ViewIdentity
    item_type: SemanticType
    extent: Polygon
    id: UUID = field(default_factory=uuid4)
    corroborated: bool = False
    """Always `False` here. Present so the field exists on the type that `evidence/` promotes into
    and `app/models/drawing.py` persists, not so that a caller may set it — see `__post_init__`."""

    def __post_init__(self) -> None:
        if not isinstance(self.view, ViewIdentity):
            raise TypeError("view must be a ViewIdentity")
        if not isinstance(self.item_type, SemanticType):
            raise TypeError(
                "item_type must come from the SemanticType vocabulary, never a bare string. A free "
                "string can be extracted, stored, matched against nothing, and never noticed."
            )
        if not isinstance(self.extent, Polygon):
            raise TypeError("extent must be an evidence.polygon.Polygon")
        if self.corroborated:
            raise ItemCorroborationError(
                "an item cannot be constructed already corroborated. Items are read off a drawing, "
                "and one created as a fact would be a second route into the verdict that bypasses "
                "the evidence gate."
            )
        if self.extent.document_version_id != self.view.document_version_id:
            raise ValueError(
                f"the extent belongs to document version {self.extent.document_version_id} but the "
                f"view belongs to {self.view.document_version_id}. An item whose geometry came from "
                "a different document is not an item, and containment against it would be answered "
                "confidently and wrongly."
            )
        if self.extent.page != self.view.page:
            raise ValueError(
                f"the extent is on page {self.extent.page} but the view is on page {self.view.page}"
            )


def contains(item: DrawingItem, other: Polygon) -> bool:
    """Whether a polygon lies inside an item's extent.

    The reason the extent is a polygon rather than a bounding box: `B10.4` asks which item a
    dimension belongs to, and neighbouring cabinets have overlapping boxes while their outlines do
    not. Comparison across coordinate planes raises inside `Polygon` rather than returning a
    misleading answer.
    """
    return item.extent.contains(other)
