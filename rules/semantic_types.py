"""Single source of truth for semantic types.

PROVISIONAL — confirm the exact names with Raj before Phase 3 (rules). Every rule and
observation MUST reference these constants, never a hard-coded string, so a rename is a
one-file change.

See docs/RULE_ENGINE_SPEC.md and memory.md ("OPEN — semantic type vocabulary").
"""

from enum import Enum


class SemanticType(str, Enum):
    # --- dimensions (mm canonical) ---
    COUNTERTOP_OVERALL_WIDTH = "countertop_overall_width"
    CABINET_WIDTH = "cabinet_width"
    FILLER_WIDTH = "filler_width"
    SINK_CUTOUT_WIDTH = "sink_cutout_width"

    # --- context / discriminators / inputs ---
    WALL_CONFIG = "wall_config"          # back_left_right | back_left | back_only | island
    FIELD_DIMENSION = "field_dimension"  # on-site measured wall-to-wall; USER_INPUT, on no drawing

    # --- materials / finish ---
    MATERIAL = "material"


class OperandSource(str, Enum):
    """Where a rule operand comes from (see RULE_ENGINE_SPEC.md §3e)."""
    ARCH = "ARCH"                # approved architectural set
    SHOP = "SHOP"                # vendor shop drawing
    LITERAL = "LITERAL"          # fixed standard value (global rules)
    USER_INPUT = "USER_INPUT"    # human-provided (e.g. on-site field dimension)


class WallConfig(str, Enum):
    """PROVISIONAL — confirm with Raj. Drives tolerance + field-cut count per rule variant."""
    BACK_LEFT_RIGHT = "back_left_right"   # 3 walls (Raj's starting case, tol ~1/8")
    BACK_LEFT = "back_left"               # 2 walls (L)
    BACK_ONLY = "back_only"              # 1 wall
    ISLAND = "island"                    # no walls
