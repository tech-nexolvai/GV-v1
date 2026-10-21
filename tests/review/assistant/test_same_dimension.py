"""#1840: a dimension printed twice on one drawing is one blind pick, and the Not-compared list says
what really stopped an item. Made-up drawings and values only (no client data); no PDF, no model
call."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

import pytest

from app.review.assistant import classification, full_check_roster
from app.review.assistant.full_check_advocate import Check, Extent, decide, same_dimension
from app.review.assistant.simple_check import verify as v
from app.review.assistant.simple_check.verify import BlindAnswer, Item, Pack, Region, with_blind

FILE = "made-up.pdf"
ARCH = (0.0, 0.0, 600.0, 400.0)
VENDOR = (0.0, 420.0, 600.0, 800.0)


def _label(
    lid: str,
    inches: str,
    ends: tuple[float, float],
    y: float,
    *,
    side: str = "architect",
    page: int = 2,
    **more: Any,
) -> dict[str, Any]:
    x0, x1 = ends
    return {
        "id": lid,
        "file": FILE,
        "page": page,
        "side": side,
        "text": f'{inches}"',
        "inches": inches,
        "orientation": "h",
        "route": "text",
        "units": None,
        "box": [x0 + 5, y - 9, x0 + 25, y - 2],
        "dim_line": [[x0, y], [x1, y]],
        "scale_ok": True,
        **more,
    }


def _drawing(label: Mapping[str, Any]) -> object | None:
    """The made-up page map: an architect drawing above, the vendor's below."""
    box = label.get("box") or (0, 0, 0, 0)
    cy = (float(box[1]) + float(box[3])) / 2
    return ARCH if cy < 400 else VENDOR if cy < 800 else None


# A centre line to a wall, printed above the elevation (D4) and again below it (D9).
ABOVE = _label("D4", "30", (300.0, 360.0), 40)
BELOW = _label("D9", "30", (300.0, 360.6), 330)


def test_a_dimension_printed_twice_on_one_drawing_is_one_dimension() -> None:
    why = same_dimension(ABOVE, BELOW, _drawing)
    assert why is not None and "printed twice" in why
    assert same_dimension(BELOW, ABOVE, _drawing) is not None


@pytest.mark.parametrize(
    "other",
    [
        {**BELOW, "inches": "30.25", "text": '30 1/4"'},  # another value
        {**BELOW, "dim_line": [[300.0, 330.0], [363.0, 330.0]]},  # an end 3 pt away
        {**BELOW, "dim_line": [[240.0, 330.0], [300.0, 330.0]]},  # the next span along
        {**BELOW, "scale_ok": False},  # its line may not be the label's own
        {**BELOW, "scale_ok": None},
        {**BELOW, "origin": "picture"},  # a pasted picture's pixels have no line
        {**BELOW, "dim_line": None},
        {**BELOW, "orientation": "v"},
        {**BELOW, "side": "vendor"},
        {**BELOW, "page": 3},
        {**BELOW, "file": "other.pdf"},
        {**BELOW, "box": [305, 431, 325, 438]},  # inside the vendor's drawing
        {**ABOVE},  # the same label is no second print
    ],
)
def test_anything_else_is_not_the_same_dimension(other: dict[str, Any]) -> None:
    assert same_dimension(ABOVE, other, _drawing) is None


def test_a_label_on_no_drawing_is_never_one() -> None:
    assert same_dimension(ABOVE, BELOW, lambda label: None) is None


# ---- the roster's verdict -----------------------------------------------------------------------

SPANS = {"from": "centre_line", "to": "wall_face"}
REPLY = {"pick": "p2.D4", "vendor_spans": SPANS, "architect_spans": SPANS,
         "vendor_ink_black": "yes", "strong": True}  # fmt: skip
COMMON: dict[str, Any] = {
    "mode": "ai_verified",
    "checks": [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")],
    "printed": True,
    "helper_pick": "p2.D9",
    "code_paired": False,
    "item": "Vanity: centre line to the right wall",
}


def test_a_pick_naming_the_twin_agrees_and_everything_else_still_holds() -> None:
    cannot: tuple[Extent, str] = ("cannot_tell", "no place is tied on both drawings")
    before = decide(advocate=REPLY, extent=cannot, **COMMON)
    assert before.result == "objects" and "chose p2.D4, not p2.D9" in before.reason
    after = decide(advocate=REPLY, extent=cannot, twins=("p2.D4",), **COMMON)
    assert after.result == "passed" and after.answers["twin"] == "same_dimension"
    assert after.answers["pairing"] == "proved_by_two_picks"
    # The ends must still agree, a geometry that shows different points still blocks, a weak pick
    # still cannot settle what geometry cannot tell, a pick of another label still disagrees.
    differ = {**REPLY, "architect_spans": {"from": "wall_face", "to": "centre_line"}}
    assert decide(advocate=differ, extent=cannot, twins=("p2.D4",), **COMMON).result == "objects"
    apart: tuple[Extent, str] = ("different_extent", "the ends land apart")
    assert decide(advocate=REPLY, extent=apart, twins=("p2.D4",), **COMMON).result == "objects"
    weak = {**REPLY, "strong": False}
    assert decide(advocate=weak, extent=cannot, twins=("p2.D4",), **COMMON).result == "objects"
    other = {**REPLY, "pick": "p2.D5"}
    assert decide(advocate=other, extent=cannot, twins=("p2.D4",), **COMMON).result == "objects"
    # The room-end guard and the top-face guard stay.
    room = {**COMMON, "item": "Unit: floor to the bulkhead"}
    assert decide(advocate=REPLY, extent=cannot, twins=("p2.D4",), **room).result == "objects"
    face = {**COMMON, "item": "Vanity: floor to the underside of the top"}
    assert decide(advocate=REPLY, extent=cannot, twins=("p2.D4",), **face).result == "objects"


class _Labels:
    def __init__(self, labels: list[dict[str, Any]]) -> None:
        self.labels = labels

    def on_page(self, file: str, page: int, *, read_now: bool = True) -> list[dict[str, Any]]:
        return [label for label in self.labels if label["page"] == page]


def _run(labels: list[dict[str, Any]]) -> Any:
    engine = type("Engine", (), {})()
    engine.book = type("Book", (), {})()
    engine.book.dim_labels = _Labels(labels)
    engine.book.main_file = FILE
    return full_check_roster._Run(engine)


PAGE_MAP = {
    "pages": [
        {
            "file": FILE,
            "page": 2,
            "halves": [
                {"box": list(ARCH), "role": "architect", "round": None},
                {"box": list(VENDOR), "role": "vendor", "round": 1},
            ],
        }
    ]
}


def test_the_roster_names_the_twin_only_when_code_proves_it() -> None:
    other = _label("D5", "30", (100.0, 160.0), 40)
    run = _run([ABOVE, BELOW, other])
    assert run._twins(REPLY, BELOW, PAGE_MAP) == ("p2.D4",)
    assert run._twins({**REPLY, "pick": "p2.D5"}, BELOW, PAGE_MAP) == ()
    assert run._twins({**REPLY, "pick": "none"}, BELOW, PAGE_MAP) == ()
    assert run._twins(None, BELOW, PAGE_MAP) == ()
    assert run._twins(REPLY, BELOW, {"pages": []}) == ()  # no drawing to hold them


# ---- the simple check's blind look ----------------------------------------------------------------


def _pack() -> Pack:
    vendor = _label("D1", "24", (300.0, 348.0), 600, side="vendor")
    labels = [{k: val for k, val in label.items() if k not in ("file", "page")}
              for label in (ABOVE, BELOW, vendor)]  # fmt: skip
    return Pack.from_documents(
        main_file=FILE,
        dims={"files": [FILE], "pages": [{"file": FILE, "page": 2, "labels": labels}]},
        page_map={"pages": [{**PAGE_MAP["pages"][0], "area": "1. Vanity"}]},
        pairs={"pairs": []},
    )


ITEM = Item("p2.v1", Region(FILE, 2, VENDOR, 1), (Region(FILE, 2, ARCH),))


def test_the_blind_look_naming_the_twin_agrees() -> None:
    ends = ("centre line", "right wall", "centre line", "right wall")
    pair = {"vendor_label": "D1", "architect_label": "p2.D9", "measures": "centre line to wall",
            "vendor_from": ends[0], "vendor_to": ends[1], "architect_from": ends[2],
            "architect_to": ends[3], "verdict": "different", "note": ""}  # fmt: skip
    result = {"work_item": ITEM.id, "pairs": [pair], "other_items": [], "unaccounted_labels": []}
    found = v.verify_answer(result, ITEM, _pack(), ink=lambda label: (120, 0.0)).items[0]
    assert found.outcome == "candidate" and found.twins == ("p2.D4",)
    answer = BlindAnswer(pick="p2.D4", verdict="same_thing", vendor_from=ends[0],
                         vendor_to=ends[1], architect_from=ends[2], architect_to=ends[3],
                         vendor_ink_black="yes")  # fmt: skip
    assert with_blind(found, answer).outcome == "red"
    other = dataclasses.replace(answer, pick="p2.D7")
    grey = with_blind(found, other)
    assert grey.outcome == "not_compared" and "chose p2.D7" in grey.reason


# ---- what the Not-compared list says -------------------------------------------------------------


def _stopped(answers: dict[str, str], note: str) -> dict[str, Any]:
    return {
        "page": 7,
        "item": "Counter: overall width",
        "status": "different",
        "source": "architect",
        "vendor": '40"',
        "architect": "3' - 6\"",
        "vendor_box": {"page": 7, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
        "arch_box": {"page": 7, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
        "critic": {
            "result": "objects",
            "answers": answers,
            "note": note,
            "model": "devil's advocate",
        },
    }


PASS = {"ink": "pass", "annotation": "pass", "roles": "pass", "axis": "pass", "round": "pass"}


@pytest.mark.parametrize(
    ("answers", "note", "reason", "words"),
    [
        ({**PASS, "roles": "fail"}, "Not proved: the reference is on the vendor's drawing.",
         "not_proved", "vendor's own drawing (another round)"),
        ({**PASS, "roles": "fail", "axis": "fail"},
         "Not proved: the vendor value is on the brand's drawing.", "prototype", "brand prototype"),
        ({**PASS, "ink": "fail"}, "Not proved: the vendor value is in colour.", "not_proved",
         "in colour"),
        ({**PASS, "ink": "unknown"}, "Not proved: its ink could not be looked at.", "not_proved",
         "ink"),
        ({**PASS, "annotation": "fail"}, "Not proved: inside a note.", "not_proved",
         "under a reviewer's note"),
        ({"labels": "fail"}, "Not proved: no label.", "not_proved", "no printed label"),
        ({**PASS, "slip": "fail"}, "Not proved: check the reading.", "not_proved", "misreading"),
        ({**PASS, "advocate_pick": "p7.D3", "helper_pick": "p7.D1"},
         "Not proved: a blind second pick chose p7.D3, not p7.D1.", "not_proved",
         "another of the architect's dimensions"),
        # A doubt about the two ends stays "different reference points".
        ({**PASS, "extent": "different_extent"}, "Not proved: the ends land apart.",
         "different_reference", "same points"),
        ({**PASS, "spans": "differ"}, "Not proved: the ends land in different places.",
         "different_reference", "same points"),
        ({**PASS, "trade": "fail", "extent": "cannot_tell"},
         "Not proved: the architect's dimension may run to the bulkhead.",
         "different_reference", "same points"),
        ({**PASS, "axis": "fail"}, "Not proved: one is a width and the other a height.",
         "different_reference", "same points"),
    ],
)  # fmt: skip
def test_the_not_compared_reason_is_what_stopped_the_item(
    answers: dict[str, str], note: str, reason: str, words: str
) -> None:
    found = classification.classify(_stopped(answers, note), proved=False)
    assert found.outcome == "not_compared"
    assert found.reason == reason and words in str(found.why), (found.reason, found.why)


def test_an_item_the_advocate_never_judged_keeps_the_old_reason() -> None:
    finding = {**_stopped(PASS, ""), "critic": None}
    found = classification.classify(finding, proved=False)
    assert found.reason == "different_reference"
