"""Two stops that hid real vendor mistakes as "Not compared" (a real upload, 2026-10-27).

1. **GV's note on top of the vendor's own value.** Code reads every label from the vendor's own
   layer, with GV's notes left out, so a note of GV's lying on top of a label is not what code read.
   The advocate's note check stopped every value under a note, and GV writes its notes over the very
   sizes it questions. It now fails only when the note itself prints the vendor's value, or when the
   value has no code label.
2. **Code's geometry pairs them, a blind AI pick does not.** A Wrong whose two dimensions code shows
   ending at the same places and that passes every code check, held back only because the blind
   second pick named another reference dimension (or none), is a Likely problem ("please check"):
   never red, never a silent Not compared. Where code cannot tell, or code shows them apart, nothing
   changes.

Made-up values only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import proof
from app.review.assistant.chat_trust import judged
from app.review.assistant.full_check_advocate import (
    Check,
    Extent,
    Verdict,
    code_checks,
    decide,
    note_values,
)

FILE = "shop.pdf"
_SPANS = {"from": "wall_face", "to": "centre_line"}
_SAME: tuple[Extent, str] = ("same_extent", "both ends are tied on both drawings")
_CANNOT: tuple[Extent, str] = ("cannot_tell", "a dimension line's ends were not found")


def _label(label_id: str, side: str, inches: str, box: tuple[float, float], route: str = "drawn"):
    x, y = box
    return {
        "id": label_id,
        "side": side,
        "inches": inches,
        "text": f'{inches}"',
        "route": route,
        "orientation": "h",
        "page": 5,
        "file": FILE,
        "box": [x, y, x + 20, y + 8],
    }


def _note(text: str, box: tuple[float, float, float, float]) -> dict[str, Any]:
    return {"page": 5, "file": FILE, "kind": "FreeText", "text": text, "box": list(box)}


# ---- 1. GV's note on top of the vendor's value --------------------------------------------------


def test_a_note_printing_another_value_does_not_stop_the_vendors_own_ink() -> None:
    vendor = _label("D6", "vendor", "41.5", (300, 400))
    architect = _label("D1", "architect", "44", (100, 100))
    under = _note('41 1/4"+7"', (290, 395, 340, 412))
    checks = {c.name: c for c in code_checks(vendor, architect, annotations=[under],
                                             vendor_ink=(40, 0.0))}  # fmt: skip
    assert checks["annotation"].outcome == "pass"
    assert "under GV's note" in checks["annotation"].why
    assert all(c.outcome == "pass" for c in checks.values())


def test_a_note_printing_the_vendors_value_still_stops_it() -> None:
    vendor = _label("D6", "vendor", "41.5", (300, 400))
    same = _note('41 1/2"', (290, 395, 340, 412))
    checks = {c.name: c for c in code_checks(vendor, None, annotations=[same],
                                             vendor_ink=(40, 0.0))}  # fmt: skip
    assert checks["annotation"].outcome == "fail"
    assert "prints the same value" in checks["annotation"].why


def test_a_value_with_no_code_reading_under_a_note_is_still_stopped() -> None:
    vendor = {**_label("D6", "vendor", "41.5", (300, 400)), "route": None}
    under = _note("STONE TOP", (290, 395, 340, 412))
    checks = {c.name: c for c in code_checks(vendor, None, annotations=[under],
                                             vendor_ink=(40, 0.0))}  # fmt: skip
    assert checks["annotation"].outcome == "fail"


def test_a_note_off_the_value_never_counts() -> None:
    vendor = _label("D6", "vendor", "41.5", (300, 400))
    away = _note('41 1/2"', (10, 10, 60, 30))
    checks = {c.name: c.outcome for c in code_checks(vendor, None, annotations=[away],
                                                     vendor_ink=(40, 0.0))}  # fmt: skip
    assert checks["annotation"] == "pass"


def test_note_values_reads_each_size_a_note_prints() -> None:
    assert {str(v) for v in note_values('41 1/4"+7"')} == {"41.25", "7"}
    assert {str(v) for v in note_values('50-1/2"')} == {"50.5"}
    assert note_values("STONE TOP by GV") == set()


# ---- 2. code's geometry pairs them, a blind pick does not ---------------------------------------


def _decide(extent: tuple[Extent, str], pick: str = "none", failed: str | None = None) -> Verdict:
    names = ("ink", "annotation", "roles", "axis", "round", "units", "slip")
    return decide(
        mode="code_ends",
        checks=[Check(n, "fail" if n == failed else "pass", "made up") for n in names],
        printed=True,
        helper_pick="p4.D8",
        advocate={
            "pick": pick,
            "vendor_spans": _SPANS,
            "architect_spans": _SPANS,
            "vendor_ink_black": "yes",
            "strong": True,
        },
        extent=extent,
        code_paired=False,
        item="Left wall to outlet centre",
    )


def _finding(verdict: Verdict, extent: tuple[Extent, str], **more: Any) -> dict[str, Any]:
    """A Wrong a helper paired (made-up 9" against the reference's 1'-2"), code reading both values
    at their spots, with the advocate's verdict and the pairing record as the roster writes them."""
    critic = verdict.critic()
    return judged(
        {
            "id": "F6",
            "page": 7,
            "vs_page": 7,
            "item": "Left wall to outlet centre",
            "category": "countertop",
            "status": "different",
            "vendor": '9"',
            "architect": "1' - 2\"",
            "source": "gv_review",
            "vendor_box": {"page": 7, "x0": 100, "y0": 100, "x1": 120, "y1": 110},
            "arch_box": {"page": 7, "x0": 300, "y0": 100, "x1": 320, "y1": 110},
            "checks": {
                "printed_architect": "match",
                "printed_vendor": "match",
                "critic": critic["result"],
            },
            "critic": critic,
            "pairing": {"mode": "code_ends", "extent": list(extent)},
            "inches": {"architect": "14", "vendor": "9", "difference": "-5"},
            **more,
        }
    )


@pytest.mark.usefixtures("read_twice")
def test_code_tied_ends_with_a_disagreeing_pick_is_a_likely_problem_never_red() -> None:
    verdict = _decide(_SAME)
    assert verdict.result == "objects" and "blind second pick chose no" in verdict.reason
    finding = _finding(verdict, _SAME)
    assert not proof.code_proved(finding)
    assert proof.pick_disagrees(finding)
    assert proof.proof_group(finding) == "likely_problem"
    words = proof.likely_words(finding)
    assert "the vendor's 9\" against the reference's 1' - 2\"" in words
    assert 'they differ by 5"' in words
    assert "a second AI look did not pair them (it chose no reference dimension)" in words
    shown = proof.proof_of(finding)
    assert shown["should_be"] is None and shown["reason"] == proof.pick_words(finding)


@pytest.mark.usefixtures("read_twice")
def test_a_pick_naming_another_label_reads_so() -> None:
    finding = _finding(_decide(_SAME, pick="p4.D9"), _SAME)
    assert proof.proof_group(finding) == "likely_problem"
    assert "it chose the reference's p4.D9" in proof.likely_words(finding)


@pytest.mark.usefixtures("read_twice")
def test_where_code_cannot_tell_a_disagreeing_pick_stays_not_compared() -> None:
    finding = _finding(_decide(_CANNOT), _CANNOT)
    assert not proof.pick_disagrees(finding)
    assert proof.proof_group(finding) == "not_compared"


@pytest.mark.usefixtures("read_twice")
def test_a_failed_code_check_stays_not_compared() -> None:
    finding = _finding(_decide(_SAME, failed="round"), _SAME)
    assert not proof.pick_disagrees(finding)
    assert proof.proof_group(finding) == "not_compared"


@pytest.mark.usefixtures("read_twice")
def test_an_item_code_never_read_at_its_spot_stays_not_compared() -> None:
    finding = _finding(_decide(_SAME), _SAME, checks={"critic": "objects"})
    assert not proof.pick_disagrees(finding)
    assert proof.proof_group(finding) == "not_compared"
