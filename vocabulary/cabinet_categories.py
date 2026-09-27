"""What a cabinet *is*, as Raj's deck names it.

Four categories, and the deck names all four. Three regular types carry their own width bound on
slides 3 and 7 — `SINGLE_DOOR_CAB_WIDTH_MIN`, `DOUBLE_DOOR_CAB_WIDTH_MIN`,
`DRAWER_CAB_WIDTH_MIN` — and `CAB_EQUIP` carries none, because the distribution never moves it.

**Here rather than in `verdict/`** for the reason the package docstring gives: naming a concept
must not mean importing the engine that decides with it. Four packages need these strings and none
of the uses tells the others anything about how a verdict is reached —

* `verdict/operations/distribution.py` reads a cabinet's category to know whether it may move,
* `rules/required_inputs.py` puts the choices on the form a reviewer fills in,
* `app/` stores what they chose and hands it back,
* the frontend renders the list.

The value strings are recorded in a finding's trace and stored against a package revision, so they
are part of the data contract: renaming one invalidates stored rows and every finding that cites
them. Treat them as fixed.

Source: `cab_Checks_Sep_21.pptx` (2026-09-21), slides 2, 3, 7 and 11.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["CABINET_CATEGORY_LABELS", "CabinetCategory"]


class CabinetCategory(StrEnum):
    """The reviewer's classification of one cabinet."""

    SINGLE_DOOR = "single_door"
    DOUBLE_DOOR = "double_door"
    DRAWER = "drawer"
    EQUIPMENT = "equipment"

    @property
    def is_equipment(self) -> bool:
        """Whether this cabinet's width is fixed.

        Slide 3: *"the equipment cabinet dimensions should not be reduced otherwise equipment will
        not fit."* It binds in both directions — scenario 2 grows the run, and an opening too wide
        for the appliance is as wrong as one too narrow.
        """
        return self is CabinetCategory.EQUIPMENT

    @property
    def bound_prefix(self) -> str:
        """The stem of this category's two width-bound parameters.

        `single_door` gives `single_door_cab_width_min` and `..._max`, which is how the rulebook
        names them and how Raj's glossary names them. Equipment has no bound and raises, rather than
        returning a name nothing supplies.
        """
        if self.is_equipment:
            raise ValueError(
                "an equipment cabinet has no width bound: its width is fixed by the equipment, and "
                "the distribution never moves it"
            )
        return f"{self.value}_cab_width"


#: What to show a person, in the order a form should list them.
CABINET_CATEGORY_LABELS: tuple[tuple[CabinetCategory, str], ...] = (
    (CabinetCategory.SINGLE_DOOR, "Single door"),
    (CabinetCategory.DOUBLE_DOOR, "Double door"),
    (CabinetCategory.DRAWER, "Drawer"),
    (CabinetCategory.EQUIPMENT, "Equipment (width cannot change)"),
)
