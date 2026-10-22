"""The tag reader (#1859): tags read and compared by code; words never "Illegible".

Made-up tags only (`AB-012-CUST`, `XY-3`).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import classification, escalation, proof, tag_reader
from app.review.assistant.tag_reader import SpotRead, decide, decide_words, tag_keys


def test_tag_keys_spell_each_tag_one_way() -> None:
    assert tag_keys("(AB-012-CUST) TOP") == ["AB-12-CUST"]
    assert tag_keys("XY-03, XY 3, xy-3") == ["XY-3"]
    assert tag_keys("XY03") == ["XY-3"]
    assert tag_keys('27-3/8"') == []
    assert tag_keys("TOEKICK- XY-04") == ["XY-4"]


def test_a_tag_item_is_two_tags_not_sizes() -> None:
    assert tag_reader.is_tag_item({"vendor": "AB-12-CUST", "architect": "AB-12"})
    assert not tag_reader.is_tag_item({"vendor": '30"', "architect": "AB-12"})
    assert not tag_reader.is_tag_item({"vendor": "Felt pads", "architect": "AB-12"})


def _read(
    tags: tuple[str, ...], role: str, *, route: str | None = "text", page: int = 3, **more: Any
) -> SpotRead:
    return SpotRead(page, (0.0, 0.0, 10.0, 10.0), tags, ", ".join(tags), route, role, **more)


def test_the_same_tag_at_both_spots_is_right() -> None:
    found = decide(
        "XY-03", "XY-3", _read(("XY-3",), "vendor"), _read(("XY-3",), "architect", page=9)
    )
    assert found.outcome == "right"


def test_a_suffix_is_part_of_the_tag() -> None:
    found = decide(
        "AB-12-CUST", "AB-12", _read(("AB-12-CUST",), "vendor"), _read(("AB-12",), "architect")
    )
    assert (found.outcome, found.reason) == ("not_compared", "tags_differ")
    assert "confirm the vendor's code maps to the architect's" in found.why


def test_two_tag_systems_stay_a_question() -> None:
    found = decide("PQ-7", "AB-12", _read(("PQ-7",), "vendor"), _read(("AB-12",), "architect"))
    assert found.reason == "tags_differ"
    assert "different tag systems" in found.why


def test_the_reference_must_be_the_architects_drawing() -> None:
    found = decide("XY-3", "XY-3", _read(("XY-3",), "vendor"), _read(("XY-3",), "vendor", page=2))
    assert (found.outcome, found.reason) == ("not_compared", "tag_unconfirmed")
    assert "vendor's own drawing" in found.why
    prototype = _read(("XY-3",), "brand_prototype")
    assert decide("XY-3", "XY-3", _read(("XY-3",), "vendor"), prototype).outcome == "not_compared"
    assert (
        decide(
            "XY-3", "XY-3", _read(("XY-3",), "vendor"), prototype, prototype_reference=True
        ).outcome
        == "right"
    )


def test_code_must_read_the_claimed_tag_at_the_spot() -> None:
    found = decide("AB-12", "AB-12", _read(("XY-1",), "vendor"), _read(("AB-12",), "architect"))
    assert found.reason == "tag_unconfirmed"
    assert "not the AI's AB-12" in found.why


def test_strokes_code_cannot_read_are_words_drawn_as_strokes() -> None:
    unread = _read((), "vendor", route=None, ink=True, why="the two OCR engines did not agree")
    found = decide("AB-12", "AB-12", unread, _read(("AB-12",), "architect"))
    assert found.reason == "stroke_words"
    assert found.why.startswith("words drawn as strokes")


def _change(architect: str, vendor: str, page: int = 3) -> dict[str, Any]:
    return {
        "page": page,
        "text_proof": {"kind": "changed", "code": architect, "vendor_codes": [vendor]},
    }


def test_a_different_tag_is_wrong_only_with_the_finish_checks_proof() -> None:
    vendor, architect = _read(("XY-4",), "vendor"), _read(("XY-3",), "architect")
    assert decide("XY-4", "XY-3", vendor, architect).outcome == "not_compared"
    proved = decide("XY-4", "XY-3", vendor, architect, changes=[_change("XY-3", "XY-04")])
    assert proved.outcome == "wrong"
    assert proved.proof is not None and proved.proof["kind"] == "changed"
    # Another area's (another page's) change, or other codes, prove nothing here.
    assert (
        decide("XY-4", "XY-3", vendor, architect, changes=[_change("XY-3", "XY-04", 40)]).outcome
        == "not_compared"
    )
    assert (
        decide("XY-4", "XY-3", vendor, architect, changes=[_change("XY-2", "XY-04")]).outcome
        == "not_compared"
    )


def test_words_are_a_note_never_illegible() -> None:
    found = decide_words("Soft-close runners", "Felt pads", _read((), "vendor", route="text"))
    assert found.reason == "words"
    unread = decide_words(
        "Soft-close runners", "Felt pads", _read((), "vendor", route=None, ink=True)
    )
    assert unread.reason == "stroke_words"


def test_read_spot_reads_printed_phrases_first() -> None:
    phrases = [{"text": "(AB-012-CUST) TOP", "box": [1, 1, 9, 9], "side": "vendor"}]
    found = tag_reader.read_spot("A.pdf", 3, (0, 0, 10, 10), phrases)
    assert (found.tags, found.route, found.role) == (("AB-12-CUST",), "text", "vendor")
    calls: list[Any] = []

    def strokes(file: str, page: int, box: Any) -> tuple[tuple[str, ...], str, bool, str]:
        calls.append(box)
        return ("XY-3",), "XY-3", True, ""

    by_ink = tag_reader.read_spot("A.pdf", 3, (50, 50, 60, 60), phrases, strokes=strokes)
    assert (by_ink.tags, by_ink.route) == (("XY-3",), "strokes")
    assert len(calls) == 1


# ---- through the escalation and the classification ------------------------------------------------


_PAGE_MAP = {
    "pages": [
        {
            "file": "A.pdf",
            "page": 3,
            "halves": [
                {"role": "vendor", "box": [0, 0, 500, 800]},
                {"role": "architect", "box": [500, 0, 1000, 800]},
            ],
        }
    ]
}


def _tag_finding(vendor: str, architect: str, **more: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "item": "Area: top tag",
        "vendor": vendor,
        "architect": architect,
        "status": "different",
        "source": "architect",
        "category": "countertop",
        "created_by": "tag_reader",
        "vendor_box": {"page": 3, "x0": 100, "y0": 100, "x1": 140, "y1": 110, "file": "A.pdf"},
        "arch_box": {"page": 3, "x0": 600, "y0": 100, "x1": 640, "y1": 110, "file": "A.pdf"},
        "checks": {"printed_vendor": "not_a_number", "printed_architect": "not_a_number"},
    }
    base.update(more)
    return base


def _ctx(vendor_text: str, architect_text: str, **more: Any) -> escalation.EscalationContext:
    phrases = [
        {"text": vendor_text, "box": [101, 101, 139, 109], "side": "vendor"},
        {"text": architect_text, "box": [601, 101, 639, 109], "side": "architect"},
    ]
    return escalation.EscalationContext(
        main_file="A.pdf", page_map=_PAGE_MAP, phrases={("A.pdf", 3): phrases}, **more
    )


def test_escalation_makes_the_same_tag_right() -> None:
    found = escalation.escalate([_tag_finding("XY-03", "XY-3")], _ctx("XY-03", "XY-3")).changed[0]
    assert found["status"] == "same"
    assert proof.proof_group(found) == "right"


def test_escalation_keeps_a_different_tag_not_compared_never_red() -> None:
    found = escalation.escalate(
        [_tag_finding("AB-12-CUST", "AB-12")], _ctx("AB-12-CUST", "AB-12")
    ).changed[0]
    assert proof.proof_group(found) == "not_compared"
    assert proof.classified(found).reason == "tags_differ"
    assert not proof.code_proved(found)


def test_escalation_reds_a_tag_change_the_finish_check_proved() -> None:
    ctx = _ctx("XY-4", "XY-3", changes=[_change("XY-3", "XY-4")])
    found = escalation.escalate([_tag_finding("XY-4", "XY-3")], ctx).changed[0]
    assert found["paired_by"] == "code"
    assert proof.proof_group(found) == "clouded"


def test_an_ai_right_that_code_contradicts_is_not_right() -> None:
    same = _tag_finding("XY-3", "XY-3", status="same")
    found = escalation.escalate([same], _ctx("XY-1", "XY-3")).changed[0]
    assert proof.proof_group(found) == "not_compared"
    assert proof.classified(found).reason == "tag_unconfirmed"


@pytest.mark.parametrize(
    ("vendor", "reason"),
    [("Heavy duty adjustable levelers", "words"), ("illegible", "unreadable")],
)
def test_words_never_fall_to_illegible(vendor: str, reason: str) -> None:
    finding = _tag_finding(vendor, "Felt pads", created_by="helper:1")
    finding.pop("arch_box")
    assert classification.classify(finding, proved=False).reason == reason
