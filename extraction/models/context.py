"""Crop-neighbourhood context with an explicit, auditable geometric bound.

The crop itself remains an immutable evidence artifact. This module selects only nearby drawing
text and line geometry; it has no input capable of naming a page, document, or package, so full-
package context is not reachable through it.

Source: issue #252. Verification: tests/extraction/models/test_context.py.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal

Point = tuple[Decimal, Decimal]


def _finite(name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


def _distance(name: str, value: Decimal) -> None:
    _finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class Segment:
    """One exact line segment in PDF-point coordinates."""

    start: Point
    end: Point

    def __post_init__(self) -> None:
        for label, point in (("start", self.start), ("end", self.end)):
            if len(point) != 2:
                raise ValueError(f"segment {label} must contain exactly two coordinates")
            _finite(f"segment {label} x", point[0])
            _finite(f"segment {label} y", point[1])


@dataclass(frozen=True, slots=True)
class NearbyText:
    """Drawing text and its exact shortest distance from the crop, in PDF points."""

    text: str
    distance_pt: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise TypeError("nearby text must be a string")
        _distance("text distance_pt", self.distance_pt)


@dataclass(frozen=True, slots=True)
class NearbyGeometry:
    """Line geometry and its exact shortest distance from the crop, in PDF points."""

    segment: Segment
    distance_pt: Decimal

    def __post_init__(self) -> None:
        _distance("geometry distance_pt", self.distance_pt)


@dataclass(frozen=True, slots=True)
class AssemblyInput:
    """Synthetic-friendly inputs available around one crop, never a whole package."""

    nearby_text: tuple[NearbyText, ...]
    nearby_geometry: tuple[NearbyGeometry, ...]


@dataclass(frozen=True, slots=True)
class AssembledContext:
    """The exact bounded neighbourhood sent with one crop."""

    nearby_text: tuple[NearbyText, ...]
    nearby_geometry: tuple[NearbyGeometry, ...]

    def as_record(self) -> dict[str, object]:
        """Return JSON-safe data without converting any exact number to float."""

        return {
            "nearby_text": [
                {"text": item.text, "distance_pt": str(item.distance_pt)}
                for item in self.nearby_text
            ],
            "nearby_geometry": [
                {
                    "start": [str(value) for value in item.segment.start],
                    "end": [str(value) for value in item.segment.end],
                    "distance_pt": str(item.distance_pt),
                }
                for item in self.nearby_geometry
            ],
        }

    def as_data_text(self) -> str:
        """Render deterministic drawing data for Nova's user-content channel."""

        return json.dumps(
            self.as_record(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )


def assemble(source: AssemblyInput, *, bound_pt: Decimal) -> AssembledContext:
    """Select items at or inside ``bound_pt``; the inclusive edge is deliberate."""

    _distance("bound_pt", bound_pt)
    return AssembledContext(
        nearby_text=tuple(item for item in source.nearby_text if item.distance_pt <= bound_pt),
        nearby_geometry=tuple(
            item for item in source.nearby_geometry if item.distance_pt <= bound_pt
        ),
    )
