"""A helper's box recorded in a shrunk page picture's pixels, put back in points (2026-11-04).

Made-up values only.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import classification, picture_frame, proof, sandbox

_SHEET = (2592.0, 1728.0)  # 36 x 24 in
_LETTER = (792.0, 612.0)


def _size(file: str, page: int) -> tuple[float, float] | None:
    return _LETTER if page == 6 else _SHEET


def _finding(fid: str, architect: str, box: tuple[float, ...], **more: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": fid,
        "page": 6,
        "item": "Matrix: a count",
        "vendor": "12",
        "architect": architect,
        "status": "check",
        "created_by": "helper:7",
        "arch_box": dict(zip(("x0", "y0", "x1", "y1"), box), page=3, file="A.pdf"),
    }
    base.update(more)
    return base


def test_a_whole_sheet_seen_by_a_helper_is_shrunk_to_about_six_tenths() -> None:
    factors = picture_frame.frames(_SHEET)
    assert round(factors[0], 3) == round(3600 / 1568 * 0.72, 3)
    assert picture_frame.frames(_LETTER) == []  # shown at its own size


def test_a_box_in_pixels_moves_onto_its_printed_value() -> None:
    k = picture_frame.frames(_SHEET)[0]
    phrases = {("A.pdf", 3): [{"text": "47", "box": [1650.0, 540.0, 1672.0, 562.0]}]}
    finding = _finding("F1", "K 47 (rows)", (1650 / k, 540 / k, 1672 / k, 562 / k))
    moved = picture_frame.fix_boxes([finding], phrases, _size, "V.pdf")
    assert [m.finding_id for m in moved] == ["F1"]
    assert (finding["arch_box"]["x0"], finding["arch_box"]["y1"]) == (1650.0, 562.0)
    assert "architect" in finding[picture_frame.FIELD]


def test_a_box_on_its_value_or_placed_on_a_label_stays() -> None:
    phrases = {("A.pdf", 3): [{"text": "47", "box": [100.0, 100.0, 120.0, 110.0]}]}
    right = _finding("F1", "47", (99, 99, 121, 111))
    placed = _finding("F2", "47", (10, 10, 20, 20), dims={"architect": {"id": "D3"}})
    assert picture_frame.fix_boxes([right, placed], phrases, _size, "V.pdf") == []
    assert right["arch_box"]["x0"] == 99


def test_a_lone_digit_or_words_never_prove_a_frame() -> None:
    k = picture_frame.frames(_SHEET)[0]
    phrases = {("A.pdf", 3): [{"text": "5", "box": [1650.0, 540.0, 1660.0, 562.0]}]}
    finding = _finding("F1", "total 5", (1650 / k, 540 / k, 1660 / k, 562 / k))
    assert picture_frame.fix_boxes([finding], phrases, _size, "V.pdf") == []


def test_the_same_helpers_frame_moves_its_other_box_on_the_page() -> None:
    k = picture_frame.frames(_SHEET)[0]
    phrases = {
        ("A.pdf", 3): [
            {"text": "47", "box": [1650.0, 540.0, 1672.0, 562.0]},
            {"text": "1", "box": [1650.0, 1200.0, 1660.0, 1220.0]},
            {"text": "0", "box": [985.0, 730.0, 990.0, 740.0]},
        ]
    }
    proving = _finding("F1", "K 47", (1650 / k, 540 / k, 1672 / k, 562 / k))
    other = _finding("F2", "total 5 (rooms 31, 32)", (1640 / k, 1190 / k, 1700 / k, 1230 / k))
    other_helper = _finding(
        "F3",
        "total 5 (rooms 31, 32)",
        (1640 / k, 1190 / k, 1700 / k, 1230 / k),
        created_by="helper:8",
    )
    moved = picture_frame.fix_boxes([proving, other, other_helper], phrases, _size, "V.pdf")
    assert sorted(m.finding_id for m in moved) == ["F1", "F2"]
    assert round(other["arch_box"]["y0"]) == 1190


def test_the_view_tool_says_how_a_page_pictures_pixels_map_to_points() -> None:
    viewed = sandbox.Viewed("Picture", b"", 1568, 1045, (3600, 2400))
    note = sandbox._points_note(f"{sandbox.SET_PACK}/pages/A_B/p-003.png", viewed)
    assert "points = pixels x 1.653" in note and "2592 x 1728 pt" in note
    assert sandbox._points_note(f"{sandbox.SET_PACK}/contact/c-1.png", viewed) == ""


@pytest.mark.parametrize(
    ("architect", "group"),
    [
        ("K 47 (rows of one type); not a size", "likely_problem"),
        ("no size printed", "not_compared"),
        ("not shown", "not_compared"),
    ],
)
def test_an_unsure_pairing_with_an_architect_value_is_a_please_check(
    architect: str, group: str
) -> None:
    finding = _finding(
        "F1",
        architect,
        (0, 0, 1, 1),
        vendor="52",
        check_reason="pairing_not_certain",
        keep_reason="architect_prints_nothing",
    )
    assert proof.proof_group(finding) == group
    why = classification.classify(finding, proved=False).why or ""
    if group == "likely_problem":
        assert "architect prints" in why and "no size or information" not in why


def test_a_box_on_the_claims_words_stays_whatever_its_numbers() -> None:
    k = picture_frame.frames(_SHEET)[0]
    phrases = {
        ("A.pdf", 3): [
            {"text": "WATER FILLER BY OTHERS", "box": [100.0, 100.0, 200.0, 110.0]},
            {"text": "AB-007", "box": [100 * k, 100 * k, 120 * k, 110 * k]},
        ]
    }
    finding = _finding("F1", "water filler by others (AB-007 cut out)", (100, 100, 200, 110))
    assert picture_frame.fix_boxes([finding], phrases, _size, "V.pdf") == []


@pytest.mark.parametrize(
    ("vendor", "architect", "group", "words"),
    [
        ("52", "52 units", "not_compared", "the same as"),
        ("52", "53 units (or 52)", "not_compared", "the same as"),
        ("12", "page 4 shows the bar, no XY-007", "not_compared", "no size or information"),
    ],
)
def test_an_unsure_pairing_with_the_same_numbers_is_no_please_check(
    vendor: str, architect: str, group: str, words: str
) -> None:
    finding = _finding(
        "F1", architect, (0, 0, 1, 1), vendor=vendor, check_reason="pairing_not_certain"
    )
    assert proof.proof_group(finding) == group
    assert words in (classification.classify(finding, proved=False).why or "")


def test_an_unsure_pairing_stopped_by_gvs_markup_stays_not_compared() -> None:
    finding = _finding(
        "F1",
        "1' - 6\"",
        (0, 0, 1, 1),
        vendor='22 3/4"',
        status="different",
        check_reason="pairing_not_certain",
        critic={
            "model": "devil's advocate",
            "result": "failed",
            "answers": {"ink": "fail"},
            "note": "Not proved: the vendor value is in colour: GV's markup.",
        },
    )
    assert not classification.pairing_unsure(finding)
    assert proof.proof_group(finding) == "not_compared"
