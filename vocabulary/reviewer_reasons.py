"""Display words for stored reading/row refusal codes; never changes eligibility."""

import re
from collections.abc import Iterable

from vocabulary.check_holds import CHECK_HOLD_REASONS

REVIEWER_REASONS = {
    "counter-break": "This row includes a tall appliance or range bay; it may be the wall-to-wall line, not the countertop.",
    "field-cut-included": "This width already includes the field cut. The reviewer must check it.",
    "vif": "This dimension is provisional (VIF). Verify it on site before using it.",
    **CHECK_HOLD_REASONS,
    "reviewer-markup": "Covered by reviewer markup. Check the vendor's own number.",
    "row-ambiguous": "The readers could not establish which row measures the countertop.",
    "row-choice-disagreement": "The readers did not agree on which row measures the countertop.",
    "row-partial": "This row is incomplete. The reviewer must check the missing widths.",
    "incomplete-row": "This row is incomplete. The reviewer must check the missing widths.",
    "wall-layout-needed": "Choose the wall layout for this countertop row.",
    "only-one-reader": "Only one reader supplied this label. Check its value on the drawing.",
    "stacked": "Check the stacked fraction on the drawing.",
    "no-label": "No dimension label was found for this span. Check the drawing.",
    "unsure": "A reader was not sure this label belongs to the marked span; check it on the drawing.",
    "many-labels": "More than one label could belong to this span. Check which one applies.",
}


def reviewer_reason(flags: Iterable[str], fallback: str | None) -> str | None:
    """Specific whole-row holds outrank incidental missing-reader messages."""
    codes = {flag.removeprefix("row-hold:").removeprefix("check-hold:") for flag in flags}
    if fallback and "reviewer markup" in fallback.lower():
        codes.add("reviewer-markup")
    for code, words in REVIEWER_REASONS.items():
        if code in codes:
            return words
    return fallback


_MISSING_INPUT_WORDS = {
    "shop_cabinets": "the vendor's cabinet widths",
    "arch_cabinets": "the architectural cabinet widths",
    "countertop_overhang": "the countertop overhang",
    "cabinet_depth": "the cabinet depth",
    "cabinet_side_thickness": "the cabinet side thickness",
    "sink_interior_depth": "the sink's inside depth",
    "sink_interior_width": "the sink's inside width",
    "front_offset": "the sink's distance from the front edge",
    "countertop_width": "the countertop width",
    "piece_widths": "the widths of the pieces under the countertop",
    "field_cut": "the field cut",
    "wall_config": "the wall layout",
}


def finding_reviewer_reason(outcome: str, reason: str | None) -> str | None:
    """Display a missing input in reviewer words; preserve the raw reason and trace separately."""
    if outcome != "NOT_FOUND" or not reason:
        return reason
    missing = re.search(r"required value '([^']+)'", reason)
    if missing and missing[1] in _MISSING_INPUT_WORDS:
        return f"This check needs {_MISSING_INPUT_WORDS[missing[1]]}. Check the drawing or request the missing detail."
    if re.search(r"\b[a-zA-Z]+_[a-zA-Z_]+\b", reason):
        return "This check is missing required information. Check the drawing or request the missing detail."
    return reason
