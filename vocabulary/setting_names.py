"""Each setting's plain name, for a reviewer and for the vendor's copy of a report (#1181).

A setting is stored under a code name (`sink_interior_width`), and the reports used to print it
that way. A reader of a signed report, or the vendor reading the redline, should read what it is:
"Sink inside width". One table, so the screen, the report and the redline say the same thing.

Like the rest of `vocabulary/`, this imports nothing from the project.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

__all__ = ["SETTING_NAMES", "setting_name"]

#: Every setting the published rulebook uses, by its plain name.
SETTING_NAMES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "back_offset_minimum": "Sink back offset, minimum",
        "backsplash_thickness": "Backsplash thickness",
        "cabinet_depth": "Cabinet depth",
        "cabinet_side_thickness": "Cabinet side panel thickness",
        "countertop_overhang": "Countertop overhang",
        "double_door_cab_width_max": "Double-door cabinet width, maximum",
        "double_door_cab_width_min": "Double-door cabinet width, minimum",
        "drawer_cab_width_max": "Drawer cabinet width, maximum",
        "drawer_cab_width_min": "Drawer cabinet width, minimum",
        "field_cut": "Field cut per wall end",
        "filler_max": "Filler width, maximum",
        "filler_min": "Filler width, minimum",
        "front_offset_required": "Sink front offset",
        "single_door_cab_width_max": "Single-door cabinet width, maximum",
        "single_door_cab_width_min": "Single-door cabinet width, minimum",
        "sink_cutout_clearance": "Sink cut-out clearance",
        "sink_interior_depth": "Sink inside depth",
        "sink_interior_width": "Sink inside width",
    }
)


def setting_name(name: str) -> str:
    """The plain name, or the code name in words for a setting this table does not know yet."""
    if name in SETTING_NAMES:
        return SETTING_NAMES[name]
    words = name.replace("_", " ").strip()
    return words[:1].upper() + words[1:]
