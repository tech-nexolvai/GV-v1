"""An item on a drawing (#165, B7.2).

The tests that matter are the refusals. An item is what a rule is *about*, so a wrong one produces a
finding that is internally consistent, fully traced and completely wrong — the failure mode the
evidence gate exists to prevent, arriving through the drawing model instead.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon
from extraction.model.items import (
    DrawingItem,
    ItemCorroborationError,
    ViewIdentity,
    contains,
)
from vocabulary.semantic_types import SemanticType

DOCUMENT = uuid4()


def _polygon(
    *,
    document_version_id=DOCUMENT,
    page: int = 1,
    box: tuple[str, str, str, str] = ("0.1", "0.1", "0.4", "0.4"),
) -> Polygon:
    x0, y0, x1, y1 = (Decimal(v) for v in box)
    return Polygon(
        points=(
            StoredPoint(x=x0, y=y0),
            StoredPoint(x=x1, y=y0),
            StoredPoint(x=x1, y=y1),
            StoredPoint(x=x0, y=y1),
        ),
        space="stored",
        document_version_id=document_version_id,
        page=page,
    )


def _view(*, document_version_id=DOCUMENT, page: int = 1, tag: str = "D") -> ViewIdentity:
    return ViewIdentity(document_version_id=document_version_id, page=page, tag=tag)


def _item(**overrides) -> DrawingItem:
    kwargs = {
        "view": _view(),
        "item_type": SemanticType.CT001,
        "extent": _polygon(),
    }
    kwargs.update(overrides)
    return DrawingItem(**kwargs)


# ---------------------------------------------------------------------------
# An item is a candidate, never a fact
# ---------------------------------------------------------------------------


def test_an_item_is_uncorroborated_when_built() -> None:
    assert _item().corroborated is False


def test_an_item_cannot_be_constructed_already_corroborated() -> None:
    """The control. Items are read off a drawing, and one created as a fact would be a second route
    into the verdict that bypasses the evidence gate — refused outright rather than defaulted."""
    with pytest.raises(ItemCorroborationError, match="bypasses"):
        _item(corroborated=True)


def test_an_item_cannot_be_mutated_into_a_fact() -> None:
    """Frozen. Refusing it at construction and permitting it afterwards would be no control at all."""
    item = _item()
    # `frozen=True` with `slots=True` raises FrozenInstanceError, an AttributeError subclass.
    with pytest.raises((AttributeError, TypeError)):
        item.corroborated = True  # type: ignore[misc]


# ---------------------------------------------------------------------------
# The type comes from the vocabulary
# ---------------------------------------------------------------------------


def test_the_item_type_must_come_from_the_vocabulary() -> None:
    """A free string can be extracted, stored, matched against nothing, and never noticed. The
    vocabulary is anchored to an annotated diagram (ADR-0017)."""
    with pytest.raises(TypeError, match="never a bare string"):
        _item(item_type="countertop_overall_width")


def test_a_misspelt_type_is_refused_rather_than_stored() -> None:
    with pytest.raises(TypeError):
        _item(item_type="CT00l")  # letter l, not digit 1


# ---------------------------------------------------------------------------
# One item, one view
# ---------------------------------------------------------------------------


def test_view_identity_is_the_pair_not_the_tag() -> None:
    """Sheets reuse D, E, F page after page. Two views sharing a tag on different pages are
    different views, and a type treating the tag as identity would merge them."""
    assert _view(page=1, tag="D") != _view(page=2, tag="D")
    assert _view(page=1, tag="D") == _view(page=1, tag="D")


def test_a_view_needs_the_tag_that_is_printed() -> None:
    for empty in ("", "   "):
        with pytest.raises(ValueError, match="non-empty text printed"):
            _view(tag=empty)


def test_cross_view_identity_is_not_representable() -> None:
    """B7.3's job. A "same as" field would invite somebody to guess that the item in elevation D and
    the item in plan E are the same cabinet, and guessing which cabinet a dimension belongs to is
    how a finding becomes confidently wrong."""
    assert not {"same_as", "assembly", "physical_item"} & set(DrawingItem.__slots__)


def test_an_extent_from_another_document_is_refused() -> None:
    """Geometry from a different document is not this item's geometry, and containment against it
    would be answered confidently and wrongly."""
    with pytest.raises(ValueError, match="different document"):
        _item(extent=_polygon(document_version_id=uuid4()))


def test_an_extent_on_another_page_is_refused() -> None:
    with pytest.raises(ValueError, match="page"):
        _item(extent=_polygon(page=7))


# ---------------------------------------------------------------------------
# Extent is a polygon so containment is geometric
# ---------------------------------------------------------------------------


def test_containment_answers_which_item_a_dimension_belongs_to() -> None:
    item = _item()
    inside = _polygon(box=("0.15", "0.15", "0.2", "0.2"))
    assert contains(item, inside)


def test_a_neighbouring_region_is_not_contained() -> None:
    """The reason the extent is a polygon rather than a bounding box: neighbouring cabinets have
    overlapping boxes while their outlines do not."""
    item = _item()
    elsewhere = _polygon(box=("0.6", "0.6", "0.8", "0.8"))
    assert not contains(item, elsewhere)


def test_the_extent_must_be_a_validated_polygon() -> None:
    """Not a raw tuple. `evidence.polygon.Polygon` already rejects zero-area and self-intersecting
    geometry exactly, and a second polygon vocabulary here would have to repeat that or skip it."""
    with pytest.raises(TypeError, match="evidence.polygon.Polygon"):
        _item(extent=(0, 0, 1, 1))


# ---------------------------------------------------------------------------
# The import boundary this story needed opening
# ---------------------------------------------------------------------------


def test_the_vocabulary_comes_from_the_neutral_package() -> None:
    """`docs/DESIGN_EXTRACTION.md` §2 forbids `extraction/` importing `rules/` — an extractor that
    knows which rule is coming can be tuned to satisfy it. The vocabulary moved to `vocabulary/` so
    naming a concept no longer means importing the rule engine."""
    from extraction.model import items

    assert items.SemanticType.__module__.startswith("vocabulary")
