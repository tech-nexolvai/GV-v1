"""A setting that is the size of a real thing is more than zero, or it is refused (#1181).

The cabinet's depth, a cabinet side panel and a sink's inside width and depth are the sizes of real
things. A `0"` typed for one — because it was not known, or by a slip — would not stay a harmless
blank: the depth check would add it to the overhang and could come out equal to a drawing by
accident. A missing value is never zero, so it is left empty and the check keeps waiting.

**The overhang and the backsplash are left out on purpose.** Zero is a real answer for them (no
overhang, no backsplash), so they are accepted at zero; whether GV wants that is an open question to
Anant on the #1181 pull request.

The same rule as #1182's widths (`app/review/sizes.py` there), worded the same way, for settings
rather than readings. Checked where a setting is saved: the company's defaults, a project value on
Measurements or a "Needs" line, and the sink entry.
"""

from __future__ import annotations

from typing import Final

from units.measurement import Measurement

__all__ = ["NOT_ABOVE_ZERO_SETTING", "POSITIVE_SETTINGS", "refused_size"]

#: The settings that are the size of a real thing, and so are never zero or negative.
POSITIVE_SETTINGS: Final[frozenset[str]] = frozenset(
    {"cabinet_depth", "cabinet_side_thickness", "sink_interior_width", "sink_interior_depth"}
)

#: The refusal, in the words the screen shows.
NOT_ABOVE_ZERO_SETTING: Final = (
    "must be more than zero. If you do not know it, leave it empty: a missing value is never zero."
)


def refused_size(name: str, value: Measurement, *, label: str) -> str | None:
    """The refusal for `value` as setting `name`, or `None` when it may be saved."""
    if name in POSITIVE_SETTINGS and value.exact <= 0:
        return f"{label} {NOT_ABOVE_ZERO_SETTING}"
    return None
