"""Accuracy step 1: what code itself finds for the items the AI could not have proved, $0.

All synthetic: made-up labels and values, no PDF, no model call.

- An item the AI says the vendor's drawing does not show: code searches every vendor drawing of
  its area for the architect's printed value (`spot_locate.search_area`, the escalation's
  `vendor_search` step) and the Not-compared reason says what it found. The outcome never
  changes: a dimension the vendor does not print does not prove the item is left out, and the
  same value printed elsewhere does not prove it is the same dimension (never red, never Right).
- A difference the devil's advocate stopped at one of its code checks (the value inside GV's
  note, the reference on the vendor's own drawing, no label code read, ...) is Not compared with
  that check's words, never "different reference points".
"""

from __future__ import annotations

from typing import Any

from app.review.assistant import proof
from app.review.assistant.classification import classify
from app.review.assistant.escalation import FREE_STEPS, EscalationContext, escalate
from app.review.assistant.spot_locate import search_area, search_words

FILE = "set.pdf"
#: Page 4: the architect's drawing above, the vendor's round 1 below; page 5: round 2.
ARCHITECT_HALF = [0.0, 0.0, 600.0, 300.0]
VENDOR_HALF = [0.0, 320.0, 600.0, 780.0]
ROUND_2 = [0.0, 0.0, 600.0, 780.0]


def _label(
    id_: str, side: str, inches: str, at: tuple[float, float], way: str = "v", **more: Any
) -> dict[str, Any]:
    x, y = at
    return {
        "id": id_,
        "text": more.pop("text", f'{inches}"'),
        "inches": inches,
        "box": [x - 6, y - 10, x + 6, y + 10],
        "side": side,
        "orientation": way,
        "route": "text",
        **more,
    }


def _page_map() -> dict[str, Any]:
    return {
        "pages": [
            {
                "file": FILE,
                "page": 4,
                "area": "Coffee bar",
                "halves": [
                    {"role": "architect", "box": ARCHITECT_HALF, "round": None},
                    {"role": "vendor", "box": VENDOR_HALF, "round": 1},
                ],
            },
            {
                "file": FILE,
                "page": 5,
                "area": "Coffee bar",
                "halves": [{"role": "vendor", "box": ROUND_2, "round": 2}],
            },
            {
                "file": FILE,
                "page": 6,
                "area": "Lounge",
                "halves": [{"role": "vendor", "box": ROUND_2, "round": 1}],
            },
        ]
    }


def _missing(**changes: Any) -> dict[str, Any]:
    """An AI's "the vendor does not show it": the architect's outlet at 2'-3" (made up)."""
    finding: dict[str, Any] = {
        "id": "F7",
        "page": 4,
        "item": "Coffee bar: floor to outlet centre line",
        "category": "cabinet",
        "architect": "2' - 3\"",
        "vendor": "not shown on the vendor",
        "status": "check",
        "keep_reason": "other",
        "source": "architect",
        "created_by": "helper:3",
        "trust_level": "to_check",
        "witnesses": [],
        "checks": {"printed_architect": "match", "printed_vendor": "not_a_number"},
        "inches": {"architect": "27", "vendor": None},
        "arch_box": {"page": 4, "x0": 94, "y0": 90, "x1": 106, "y1": 110, "file": FILE},
        "vendor_box": None,
        "dims": {
            "architect": {
                "id": "D2",
                "page": 4,
                "file": FILE,
                "text": "2' - 3\"",
                "inches": "27",
                "side": "architect",
                "route": "text",
                "box": [94, 90, 106, 110],
            }
        },
    }
    finding.update(changes)
    return finding


def _ctx(vendor_labels: list[dict[str, Any]], round_2: list[dict[str, Any]] | None = None) -> Any:
    page_4 = [_label("D2", "architect", "27", (100, 100), text="2' - 3\""), *vendor_labels]
    return EscalationContext(
        main_file=FILE,
        labels={
            (FILE, 4): page_4,
            (FILE, 5): round_2 or [],
            (FILE, 6): [_label("D1", "vendor", "27", (50, 50))],
        },
        page_map=_page_map(),
    )


def _escalated(finding: dict[str, Any], ctx: EscalationContext) -> dict[str, Any]:
    return {str(f["id"]): f for f in escalate([finding], ctx).changed}[str(finding["id"])]


# ---- the search itself -------------------------------------------------------------------------


def test_search_counts_only_vendor_labels_inside_a_vendor_drawing_the_same_way_round() -> None:
    drawings = [
        (
            4,
            VENDOR_HALF,
            [
                _label("D2", "architect", "27", (100, 100)),  # the architect's own: not searched
                _label("D5", "vendor", "27", (100, 100)),  # outside the vendor drawing
                _label("D6", "vendor", "27", (200, 400), way="h"),  # a width, not a height
                _label("D7", "vendor", "27", (300, 500), derived={"rule": "equal_parts"}),
                _label("D8", "vendor", "30", (300, 600)),
            ],
        ),
        (5, ROUND_2, [_label("D3", "vendor", "18", (50, 50))]),
    ]
    found = search_area("2' - 3\"", "v", drawings)
    assert found.found == "none" and found.pages == (4, 5)
    assert found.searched == 3  # D6, D8 and D3 (D5 is outside, D7 a worked-out part)
    words = search_words(found.as_dict())
    assert words is not None and "pages 4 and 5" in words and "3 labels" in words
    # The same value the same way round is found, on any round's drawing.
    drawings[1][2].append(_label("D9", "vendor", "27", (60, 300)))
    assert search_area("2' - 3\"", "v", drawings).labels == ("p5.D9",)


def test_mm_and_inches_must_both_match_and_a_non_size_is_unknown() -> None:
    drawings = [
        (4, VENDOR_HALF, [_label("D4", "vendor", "27", (100, 400), text="686 [27]", mm="686")])
    ]
    assert search_area("686 [27]", "v", drawings).found == "one"
    assert search_area("690 [27]", "v", drawings).found == "none"
    assert search_area("faucet by others", "v", drawings).found == "unknown"
    assert search_area("2' - 3\"", "v", []).found == "unknown"


# ---- the escalation step and the reason ----------------------------------------------------------


def test_a_missing_item_with_no_vendor_label_printing_it_says_code_searched() -> None:
    assert FREE_STEPS[-1] == "vendor_search"
    finding = _escalated(_missing(), _ctx([_label("D9", "vendor", "33", (100, 500))]))
    assert finding["vendor_search"]["found"] == "none"
    assert finding["vendor_search"]["pages"] == [4, 5]  # the Lounge page is another area
    found = proof.proof_of(finding)
    assert proof.proof_group(finding) == "not_compared" and found["not_compared"] == "not_proved"
    assert "code searched the vendor's drawings of this area (pages 4 and 5)" in found["reason"]
    assert "prints 2' - 3\"" in found["reason"] and "by the AI's reading" not in found["reason"]
    assert finding["escalation"]["outcome"] == "undecided"


def test_the_same_value_on_the_vendor_drawing_is_named_but_never_made_right() -> None:
    finding = _escalated(_missing(), _ctx([], round_2=[_label("D4", "vendor", "27", (100, 500))]))
    assert finding["vendor_search"]["labels"] == ["p5.D4"]
    found = proof.proof_of(finding)
    # Code never pairs on a value alone: not Right, not red, the label named for the reviewer.
    assert proof.proof_group(finding) == "not_compared" and finding["status"] == "check"
    assert "the AI says the vendor's drawing does not show it, but code finds" in found["reason"]
    assert "p5.D4" in found["reason"] and "cannot prove it is the same dimension" in found["reason"]


def test_an_item_with_a_vendor_value_is_never_searched() -> None:
    finding = _escalated(
        _missing(vendor='30"', status="different", inches={"architect": "27", "vendor": "30"}),
        _ctx([]),
    )
    assert "vendor_search" not in finding
    assert all(step["step"] != "vendor_search" for step in finding["escalation"]["steps"])


# ---- the devil's advocate's stop -----------------------------------------------------------------


def _stopped(answers: dict[str, str], note: str, result: str = "objects") -> dict[str, Any]:
    return {
        "id": "F9",
        "page": 4,
        "item": "Coffee bar: counter height",
        "category": "cabinet",
        "architect": "3' - 0\"",
        "vendor": '37"',
        "status": "different",
        "source": "architect",
        "created_by": "helper:2",
        "witnesses": ["printed"],
        "checks": {"printed_architect": "match", "printed_vendor": "match"},
        "inches": {"architect": "36", "vendor": "37", "difference": "1"},
        "critic": {"result": result, "answers": answers, "note": note, "model": "devil's advocate"},
    }


def test_a_check_the_advocate_failed_is_the_reason_not_different_reference_points() -> None:
    for name, note in (
        ("roles", "Not proved: the reference is on the vendor's drawing."),
        ("annotation", "Not proved: inside GV's note '37\"+2\"'."),
        ("labels", "Not proved: the vendor's value has no label code read."),
        ("ink", "Not proved: the vendor value is in colour: GV's markup."),
    ):
        found = classify(_stopped({"ink": "pass", name: "fail"}, note), proved=False)
        assert found.outcome == "not_compared" and found.reason == "not_proved", name
        assert found.why is not None and found.why.startswith("code's check stopped it: "), name
        assert note.removeprefix("Not proved: ").rstrip(".") in found.why, name


def test_a_width_against_a_height_or_a_bulkhead_stays_different_points() -> None:
    for name in ("axis", "trade"):
        found = classify(_stopped({name: "fail", "roles": "fail"}, "Not proved: x."), proved=False)
        assert found.reason == "different_reference", name
    # Every check passed and still no proof (the pick or the geometry): as before.
    passed = classify(_stopped({"ink": "pass"}, "Not proved: a blind pick chose D3."), proved=False)
    assert passed.reason == "different_reference"


def test_the_first_failed_check_in_the_advocates_order_names_what_settles_it() -> None:
    # The advocate's note names its first failed check (ink before roles): so does the reason.
    found = classify(
        _stopped({"ink": "fail", "roles": "fail"}, "Not proved: the vendor value is in colour."),
        proved=False,
    )
    assert found.why == "code's check stopped it: the vendor value is in colour"
    assert found.settle is not None and "clear of GV's markup" in found.settle
