"""Words and schema pieces every chat tool shares, so the tools describe one thing one way.

A box or a point on a drawing is always in the same units (`BOX_UNITS`): the units of
`pdftotext -bbox`, the set pack's `page_map.json`, `zoom`, `verify_value`, `measure_on_drawing`, the
finding tools and the reading ledger. Before this module each tool said it in its own words, and a
real answer left `page` out of a finding's box because the box looked optional.

Pure data, no I/O. The tests (`tests/review/assistant/test_tool_contracts.py`) hold every tool to
the same contract: a description that says what it does, when to use it, what it returns and one
short example (which must validate against the tool's own schema); every object closed
(`additionalProperties: false`) with its `required` list; every field described.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

__all__ = [
    "BOX_UNITS",
    "SIDE_WORDS",
    "box_schema",
    "coordinate",
    "described",
    "point_schema",
]

#: The one way every tool says where something is on a page (in each box's description).
BOX_UNITS: Final = (
    "PDF points (1/72 inch) of the page as displayed (rotation applied), origin top-left, y down, "
    "as pdftotext -bbox and page_map.json give them; on a page picture, points = pixels x 72 / dpi"
)

#: Which side a box or a value belongs to, the same words in every tool.
SIDE_WORDS: Final = (
    "architect = the architect's (ID set) design drawing; vendor = the vendor's shop drawing"
)


def coordinate(axis: str) -> dict[str, Any]:
    """One number of a box or a point, described in a word (the box carries the units)."""
    words = {"x0": "left", "y0": "top", "x1": "right", "y1": "bottom", "x": "across", "y": "down"}
    return {"type": "number", "description": words[axis]}


def box_schema(
    description: str,
    *,
    page: str | None = None,
    page_required: bool = False,
    file: str | None = None,
) -> dict[str, Any]:
    """A box `{x0, y0, x1, y1}` (and `page`, `file` when given) with the shared units in its
    description. `page`/`file` are the descriptions of those fields; `None` leaves them out."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    if page is not None:
        properties["page"] = {"type": "integer", "minimum": 1, "description": page}
        if page_required:
            required.append("page")
    for axis in ("x0", "y0", "x1", "y1"):
        properties[axis] = coordinate(axis)
        required.append(axis)
    if file is not None:
        properties["file"] = {"type": "string", "description": file}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        "description": f"{description} Units: {BOX_UNITS}.",
    }


def point_schema(description: str) -> dict[str, Any]:
    """A point `{x, y}` on a page, in the box units (said once per tool, in its boxes or here)."""
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["x", "y"],
        "properties": {"x": coordinate("x"), "y": coordinate("y")},
        "description": f"{description} (PDF points, origin top-left, y down)",
    }


def described(schema: Mapping[str, Any], description: str) -> dict[str, Any]:
    """A copy of `schema` with its own description."""
    return {**schema, "description": description}
