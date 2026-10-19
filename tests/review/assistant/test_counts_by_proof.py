"""What is unknown is what code did not settle (the pipeline audit, 2026-10-19): a value drawn as
pen strokes is "unreadable" only when the item's outcome rests on it, never for an item code
proved Wrong anyway (the vendor's own string measured on the drawing). Made-up values only."""

from __future__ import annotations

from typing import Any

from app.review.assistant.chat_unknowns import unknown_records
from app.review.assistant.proof import proof_group


def _drawn(fid: str, **more: Any) -> dict[str, Any]:
    return {
        "id": fid,
        "page": 3,
        "item": "Kitchen: dimension string (across, round 1)",
        "status": "different",
        "vendor": '30 1/4"',
        "architect": '30 3/8"',
        "source": "architect",
        "checks": {"printed_architect": "match", "printed_vendor": "no_text", "second_read": None},
        **more,
    }


def test_a_proved_item_drawn_in_pen_strokes_is_not_unknown() -> None:
    proved = _drawn(
        "F12",
        trust_level="confirmed",
        witnesses=["printed"],
        paired_by="code",
        inches={"architect": "30.375", "vendor": "30.25", "difference": "-0.125"},
    )
    unproved = _drawn("F13")
    assert proof_group(proved) == "clouded" and proof_group(unproved) == "not_compared"
    found = unknown_records([proved, unproved], None)
    assert [u["what"].split()[0] for u in found if u["why"] == "unreadable"] == ["F13"]
