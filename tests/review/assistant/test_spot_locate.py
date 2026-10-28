"""A recorded value with no spot is found again at its printed label, never called unreadable (#1738).

All $0 and synthetic: made-up labels, a tiny reportlab PDF, no model call.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_trust import apply_checks
from app.review.assistant.classification import REASON_WORDS, TRADE_WORDS, classify
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.simple_check.run import _seed_for
from app.review.assistant.spot_locate import code_spotted, locate, relocate

FILE = "Vendor set.pdf"
HEIGHT = 792.0


def _label(lid: str, text: str, inches: str, box: list[float], **more: Any) -> dict[str, Any]:
    return {
        "id": lid,
        "text": text,
        "inches": inches,
        "box": box,
        "side": more.pop("side", "vendor"),
        "orientation": "h",
        "route": more.pop("route", "text"),
        "mm": more.pop("mm", None),
        **more,
    }


#: Page 2: an architect half above, two vendor rounds below (made-up values).
HALVES: list[dict[str, Any]] = [
    {"box": [0, 0, 612, 300], "role": "architect", "round": None},
    {"box": [0, 300, 306, 792], "role": "vendor", "round": 1},
    {"box": [306, 300, 612, 792], "role": "vendor", "round": 2},
]
LABELS = [
    _label("D1", "3' - 4\"", "40", [100, 80, 140, 92], side="architect"),
    _label("D2", "1016 [40 1/4]", "40.25", [100, 400, 150, 410], mm="1016"),
    _label("D3", "1022 [40 1/4]", "40.25", [100, 430, 150, 440], mm="1022"),
    _label("D4", '27 1/2"', "27.5", [120, 500, 150, 512], route="drawn"),
    _label("D5", '19"', "19", [120, 600, 140, 612]),
    _label("D6", '19"', "19", [400, 600, 420, 612]),
    _label("D7", '11"', "11", [120, 650, 140, 662]),
    _label("D8", '11"', "11", [160, 650, 180, 662]),
    _label("D9", "EQ", "9", [200, 650, 220, 662], derived={"from": "D7"}),
]


def test_a_typed_mm_and_inch_value_is_found_by_its_whole_text() -> None:
    # The same inches are printed twice; only one label prints these millimetres.
    found = locate(LABELS, "vendor", "1022 [40 1/4]", HALVES)
    assert found.found == "one" and found.label is not None and found.label["id"] == "D3"


def test_a_pen_stroke_value_code_read_is_found() -> None:
    found = locate(LABELS, "vendor", '27 1/2"', HALVES)
    assert found.found == "one" and found.label is not None and found.label["id"] == "D4"


def test_the_same_value_on_each_rounds_copy_is_the_newest_rounds() -> None:
    found = locate(LABELS, "vendor", '19"', HALVES)
    assert found.found == "one" and found.label is not None and found.label["id"] == "D6"
    assert "newest round (2)" in found.why


def test_several_spots_are_never_guessed() -> None:
    found = locate(LABELS, "vendor", '11"', HALVES)  # two labels of the same round
    assert found.found == "several" and found.label is None
    assert [c["id"] for c in found.candidates] == ["D7", "D8"]  # the equal part is not printed


def test_no_label_and_no_labels_and_no_number_are_none() -> None:
    assert locate(LABELS, "vendor", '52"', HALVES).found == "none"
    assert locate(None, "vendor", '27 1/2"', HALVES).found == "none"
    assert locate(LABELS, "vendor", "see GV Review's record", HALVES).found == "none"
    # A side's labels only: the architect's 40" is not the vendor's.
    assert locate(LABELS, "vendor", "3' - 4\"", HALVES).found == "none"
    assert locate(LABELS, "architect", '40"', HALVES).label["id"] == "D1"  # type: ignore[index]


def _seed(**changes: Any) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 2,
        "item": "Desk: overall width",
        "architect": "3' - 4\"",
        "vendor": '27 1/2"',
        "status": "different",
        "source": "gv_review",
        "created_by": "gv_review",
        "arch_box": None,
        "vendor_box": None,
        "record_id": "ai-review:p2:r1",
        **changes,
    }


def _on_page(page: int = 2) -> list[dict[str, Any]]:
    return [{**label, "file": FILE, "page": page} for label in LABELS]


def test_relocate_gives_both_sides_their_spot_and_says_what_it_searched() -> None:
    found = relocate(_seed(), _on_page(), file=FILE, halves=HALVES)
    assert found["vendor_box"] == {
        "page": 2,
        "x0": 120.0,
        "y0": 500.0,
        "x1": 150.0,
        "y1": 512.0,
        "file": FILE,
    }
    assert found["dims"]["vendor"]["id"] == "D4" and found["dims"]["vendor"]["route"] == "drawn"
    assert found["dims"]["architect"]["id"] == "D1" and found["arch_box"]["page"] == 2
    assert found["spot_search"]["vendor"]["found"] == "one"
    assert code_spotted(found)  # its spots are code's: a reading may still merge onto it
    given = {**found, "vendor_box": {**found["vendor_box"], "x0": 1.0}, "dims": {}}
    assert not code_spotted(given)


def test_a_value_with_several_spots_keeps_none() -> None:
    found = relocate(_seed(vendor='11"'), _on_page(), file=FILE, halves=HALVES)
    assert found["vendor_box"] is None and "vendor" not in (found.get("dims") or {})
    assert found["spot_search"]["vendor"]["candidates"] == ["D7", "D8"]


# ---- the classification: never "Illegible" for a value with no spot --------------------------------


def test_a_value_with_no_spot_says_exactly_that_never_illegible() -> None:
    unspotted = _seed(vendor='52"', checks={"printed_vendor": "no_box"})
    found = classify(relocate(unspotted, _on_page(), file=FILE, halves=HALVES), proved=False)
    assert found.reason == "no_spot" and found.label != "Illegible"
    assert "the value has no spot on the drawing" in (found.why or "")
    assert REASON_WORDS["no_spot"] == "the value has no spot on the drawing"
    assert TRADE_WORDS["no_spot"] == "Not reviewed: no spot on the drawing"
    several = relocate(_seed(vendor='11"'), _on_page(), file=FILE, halves=HALVES)
    found = classify({**several, "checks": {"printed_vendor": "no_box"}}, proved=False)
    assert found.reason == "no_spot" and "several spots (D7, D8)" in (found.why or "")
    # A value code looked at, at its spot, and could not read stays unreadable.
    looked = _seed(vendor_box={"page": 2, "x0": 1, "y0": 1, "x1": 9, "y1": 9})
    looked["checks"] = {"printed_vendor": "no_text", "verify_vendor": {"result": "cannot_read"}}
    assert classify(looked, proved=False).reason == "unreadable"


# ---- the seed: re-located and read by code's checks ------------------------------------------------


def _pdf() -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(612, HEIGHT), invariant=1)
    c.showPage()
    c.setFont("Helvetica", 8)
    c.drawString(101, HEIGHT - 89, "3' - 4\"")
    c.drawString(101, HEIGHT - 438, "1022 [40 1/4]")
    c.showPage()
    c.save()
    return out.getvalue()


def _book(tmp_path: Path) -> FindingBook:
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "dims.json").write_text(
        json.dumps({"version": 1, "pages": [{"file": FILE, "page": 2, "labels": LABELS}]})
    )
    (pack / "page_map.json").write_text(
        json.dumps({"pages": [{"file": FILE, "page": 2, "halves": HALVES}]})
    )
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=pack)
    (workspace.uploads / FILE).write_bytes(_pdf())
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    paths = {FILE: workspace.uploads / FILE}
    return FindingBook(
        workspace=workspace,
        listing=listing,
        page_count=2,
        checker=lambda finding: apply_checks(finding, paths, None),
    )


def _record(vendor: str) -> dict[str, Any]:
    return {
        "record_id": f"ai-review:p2:{vendor}",
        "page": 2,
        "what": "Desk: overall width",
        "category": "other",
        "architect": "3' - 4\"",
        "vendor": vendor,
        "source": "ai_page_review",
    }


def test_recorded_typed_and_drawn_values_are_read_at_their_spot(tmp_path: Path) -> None:
    book = _book(tmp_path)
    typed = book.seed(_record("1022 [40 1/4]"))
    drawn = book.seed(_record('27 1/2"'))
    assert typed is not None and drawn is not None
    assert typed["dims"]["vendor"]["id"] == "D3" and typed["checks"]["printed_vendor"] == "match"
    assert typed["checks"]["verify_vendor"]["result"] == "match"
    assert drawn["dims"]["vendor"]["id"] == "D4"
    assert drawn["checks"]["verify_vendor"]["route"] == "drawn"
    for seeded in (typed, drawn):
        found = classify(seeded, proved=False)
        # Read by code; the AI page review's pairing is still not proved: never red, never
        # "Illegible".
        assert found.outcome == "not_compared" and found.reason not in ("unreadable", "no_spot")


def test_a_recorded_value_with_no_label_says_no_spot(tmp_path: Path) -> None:
    seeded = _book(tmp_path).seed(_record('52"'))
    assert seeded is not None and seeded["vendor_box"] is None
    assert classify(seeded, proved=False).reason == "no_spot"


def test_a_compare_answer_still_merges_onto_a_relocated_row(tmp_path: Path) -> None:
    book = _book(tmp_path)
    seeded = book.seed(_record('27 1/2"'))
    assert seeded is not None and seeded["vendor_box"] is not None
    answer = {"status": "different", "page": 2, "vendor": '27 1/2"', "architect": "3' - 4\""}
    assert _seed_for(answer, book.live()) is not None


def test_a_relocated_row_gets_a_picture_of_each_spot_code_found(tmp_path: Path) -> None:
    """2026-10-28: 8 of 10 placed rows of a real upload came with no picture, so no reading of
    them was ever made. A spot code finds gets its picture as any other finding's spot does."""
    book = _book(tmp_path)
    seeded = book.seed(_record("1022 [40 1/4]"))
    assert seeded is not None and seeded["vendor_box"] is not None
    assert seeded["vendor_picture_id"]
    assert (book.workspace.pictures / f"{seeded['vendor_picture_id']}.png").is_file()
    unplaced = book.seed(_record('52"'))
    assert unplaced is not None and unplaced["vendor_picture_id"] is None
