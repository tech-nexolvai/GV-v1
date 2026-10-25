"""Reading coverage (#1862): how many printed numbers code read, per set and per page, and where
the rest are. Invented pages only: synthetic CAD stroke lettering (`stroke_text`) read by the tiny
`StrokeReader`, and hand-made `dims.json` documents. Nothing here needs an OCR engine or a model.
It only counts: no label, finding or verdict changes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import not_compared
from app.review.assistant import reading_coverage as rc
from app.review.assistant import reading_ink as ri
from tests.review.assistant.stroke_text import StrokeReader, stroke_ops
from tests.review.assistant.test_dim_labels import HALVES, _dimension, _line, _pdf

# ---- the pen-stroke count on an invented page ---------------------------------------------------


def _engines() -> list[Any]:
    return [StrokeReader("RapidOCR"), StrokeReader("Tesseract")]


def _page(tmp_path: Path, body: bytes) -> tuple[Path, dl.PageLabels]:
    path = tmp_path / "set.pdf"
    path.write_bytes(_pdf([body]))
    found = dl.drawn_labels(path, 1, HALVES, [], budget_s=60, readers=_engines(), count_ink=True)
    return path, found


def _spots(found: dl.PageLabels) -> list[dict[str, Any]]:
    spots = ri.unread_spots({("set.pdf", 1): found.ink})[("set.pdf", 1)]
    assert spots is not None
    return sorted(spots, key=lambda s: (s["box"][1], s["box"][0]))


READ = _dimension(300, 400, 300) + stroke_ops('36"', 340, 302)


def test_a_read_number_is_no_spot_and_teaches_its_digit_shapes(tmp_path: Path) -> None:
    _, found = _page(tmp_path, READ)
    assert [label["text"] for label in found.labels] == ['36"']
    assert isinstance(found.ink, ri.PageInk)
    assert sorted(digit for _shape, digit in found.ink.digits) == ["3", "6"]
    assert _spots(found) == []


def test_only_the_vendors_read_numbers_teach_digit_shapes(tmp_path: Path) -> None:
    path, found = _page(tmp_path, READ)
    parsed = dl._parse(path, 0)
    labels = [{**label, "side": "architect"} for label in found.labels]
    assert ri.page_ink(parsed, HALVES, labels).digits == []
    assert len(ri.page_ink(parsed, HALVES, found.labels).digits) == 2


def test_a_marked_number_with_no_dimension_line_is_counted_unread(tmp_path: Path) -> None:
    _, found = _page(tmp_path, READ + stroke_ops('48"', 450, 150))
    (spot,) = _spots(found)
    assert spot["side"] == "vendor"
    assert spot["reasons"] == ["other"]
    x0, y0, x1, y1 = spot["box"]
    assert 449 <= x0 and x1 <= 470 and 240 <= y0 and y1 <= 252  # shown points (y down)


def test_an_unmarked_number_counts_only_by_the_sets_own_digit_shapes(tmp_path: Path) -> None:
    # `63` has no inch mark: counted because the vendor drew a 6 and a 3 in a number code read.
    _, found = _page(tmp_path, READ + stroke_ops("63", 300, 80))
    (spot,) = _spots(found)
    assert spot["reasons"][0] == "no_inch_mark"
    # With no number read on the set, the same ink is not counted (no alphabet to match).
    _, alone = _page(tmp_path, stroke_ops("63", 300, 80))
    assert _spots(alone) == []


def test_ink_that_is_no_digit_is_not_counted(tmp_path: Path) -> None:
    # A little cross and a chevron (a symbol, a word's letters): no inch mark, no digit shape.
    symbol = (
        _line(480, 100, 486, 108)
        + _line(480, 108, 486, 100)
        + _line(488, 108, 491, 100)
        + _line(491, 100, 494, 108)
    )
    _, found = _page(tmp_path, READ + symbol)
    assert _spots(found) == []


def test_a_sideways_number_says_so(tmp_path: Path) -> None:
    _, found = _page(tmp_path, READ + stroke_ops('25"', 560, 100, turn=True))
    (spot,) = _spots(found)
    assert spot["reasons"][0] == "sideways"


def test_a_stacked_fraction_says_so(tmp_path: Path) -> None:
    _, found = _page(tmp_path, READ + stroke_ops('3{1/2}"', 450, 150))
    (spot,) = _spots(found)
    assert "stacked_fraction" in spot["reasons"]


def test_counting_never_changes_the_labels(tmp_path: Path) -> None:
    body = READ + stroke_ops('48"', 450, 150) + stroke_ops("63", 300, 80)
    path = tmp_path / "same.pdf"
    path.write_bytes(_pdf([body]))
    plain = dl.drawn_labels(path, 1, HALVES, [], budget_s=60, readers=_engines())
    counted = dl.drawn_labels(path, 1, HALVES, [], budget_s=60, readers=_engines(), count_ink=True)
    assert plain.labels == counted.labels
    assert plain.ink is None and counted.ink is not None


def test_a_failed_count_keeps_the_pages_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("counting broke")

    monkeypatch.setattr(ri, "page_ink", broken)
    _, found = _page(tmp_path, READ)
    assert [label["text"] for label in found.labels] == ['36"'] and found.ink is None


# ---- the set's coverage from an invented dims.json ----------------------------------------------


def _label(text: str, side: str, box: list[float], **more: Any) -> dict[str, Any]:
    return {"text": text, "side": side, "box": box, "route": "text", **more}


DOCUMENT: dict[str, Any] = {
    "files": ["set.pdf"],
    "pages": [
        {
            "file": "set.pdf",
            "page": 1,
            "drawn": "done",
            "labels": [
                _label('12"', "vendor", [300, 10, 310, 16]),
                _label('14"', "vendor", [320, 10, 330, 16], route="drawn"),
                _label('9"', "vendor", [340, 10, 346, 16], route="drawn", origin="picture"),
                _label('18"', "vendor", [350, 10, 360, 16], derived="equal_parts"),
                _label('30"', "architect", [100, 10, 110, 16]),
            ],
            "phrases": [
                {"text": '24 1/4"W x 18"D', "side": "vendor", "box": [400, 50, 480, 58]},
                {"text": 'SCALE: 1/2" = 1\'-0"', "side": "vendor", "box": [400, 380, 480, 388]},
                {"text": '12"', "side": "vendor", "box": [300, 10, 310, 16]},
                {"text": "TYP.", "side": "vendor", "box": [400, 70, 420, 78]},
            ],
            "pictures": {
                "status": "done",
                "boxes": [[500, 100, 590, 190]],
                "unconfirmed": [{"text": '22"', "side": "vendor", "box": [510, 110, 520, 116]}],
            },
        },
        {"file": "set.pdf", "page": 2, "drawn": "not_run", "labels": [], "phrases": []},
    ],
}

STROKES: dict[tuple[str, int], Any] = {
    ("set.pdf", 1): [
        {"box": [450, 150, 470, 158], "side": "vendor", "reasons": ["mm_row", "sideways"]},
        {"box": [452, 200, 470, 208], "side": "vendor", "reasons": ["other"]},
        # Read after all: a label covers it now.
        {"box": [320, 10, 330, 16], "side": "vendor", "reasons": ["other"]},
        {"box": [120, 200, 130, 208], "side": "architect", "reasons": ["too_small"]},
    ],
    ("set.pdf", 2): None,
}

ANNOTATIONS = [
    # GV's small sticker over the second spot; the stamp holding the whole drawing never counts.
    {"file": "set.pdf", "page": 1, "kind": "FreeText", "box": [448, 196, 480, 212]},
    {"file": "set.pdf", "page": 1, "kind": "Stamp", "box": [0, 0, 600, 400]},
]


def _coverage() -> dict[str, Any]:
    return rc.build(DOCUMENT, STROKES, ANNOTATIONS, seconds=1.234)


def test_the_counts_per_side_and_kind() -> None:
    vendor = _coverage()["sides"]["vendor"]
    # Read: 12" typed, 14" strokes, 9" picture (the EQ part is worked out, never printed).
    # Not read: 2 numbers in the note, 2 stroke spots, 1 unconfirmed picture number.
    assert (vendor["read"], vendor["printed"]) == (3, 8)
    assert vendor["typed"] == {"read": 1, "printed": 3}
    assert vendor["strokes"] == {"read": 1, "printed": 3}
    assert vendor["pictures"] == {"read": 1, "printed": 2}
    architect = _coverage()["sides"]["architect"]
    assert (architect["read"], architect["printed"]) == (1, 2)


def test_every_unread_number_keeps_its_box_and_why() -> None:
    page = _coverage()["pages"][0]
    reasons = sorted((s["side"], s["reason"], s["count"]) for s in page["not_read"])
    assert reasons == [
        ("architect", "too_small", 1),
        ("vendor", "gv_sticker", 1),  # GV's sticker over it comes first
        ("vendor", "in_note", 2),
        ("vendor", "mm_row", 1),
        ("vendor", "picture", 1),
    ]
    assert all(len(s["box"]) == 4 for s in page["not_read"])
    assert _coverage()["not_read_by_reason"]["vendor"] == {
        "gv_sticker": 1,
        "picture": 1,
        "mm_row": 1,
        "in_note": 2,
    }


def test_a_page_the_drawn_reader_did_not_reach_is_said() -> None:
    coverage = _coverage()
    assert coverage["pages_not_counted"] == [{"file": "set.pdf", "page": 2, "drawn": "not_run"}]
    assert coverage["pages"][1]["strokes_counted"] is False
    assert any("not counted on 1 page" in line for line in rc.lines(coverage))


def test_the_plain_lines_and_the_headline() -> None:
    coverage = _coverage()
    assert rc.summary_line(coverage, "vendor") == (
        "Numbers read: 3 of about 8 printed on the vendor's drawings (38%)"
    )
    assert rc.lines(coverage)[1] == (
        "Numbers read: 1 of about 2 printed on the architect's drawings (50%)"
    )
    head = rc.headline(coverage)
    assert head is not None
    assert (head["vendor_read"], head["vendor_printed"], head["vendor_percent"]) == (3, 8, 38)
    assert (head["vendor_strokes_read"], head["vendor_strokes_printed"]) == (1, 3)
    assert head["pages_not_counted"] == 1
    assert rc.headline(None) is None and rc.lines(None) == []


def test_the_screen_report_groups_each_pages_spots_by_reason() -> None:
    report = rc.report(_coverage())
    assert report is not None
    assert report["vendor"] == {"read": 3, "printed": 8, "percent": 38}
    (row,) = report["pages"]  # page 2 prints nothing
    assert row["viewable"] is True and row["vendor_printed"] == 8
    groups = {g["reason"]: g for g in row["not_read"]}
    assert groups["in_note"]["count"] == 2 and groups["in_note"]["words"] == "inside a note"
    assert groups["gv_sticker"]["spots"][0]["box"] == [452, 200, 470, 208]
    # The report fits the contract the screen reads.
    from app.review.assistant.chat_contract import ChatReadingOut

    ChatReadingOut.model_validate(report)


def test_the_file_round_trip(tmp_path: Path) -> None:
    rc.write(tmp_path, _coverage())
    assert rc.load(tmp_path) == json.loads(json.dumps(_coverage()))
    (tmp_path / rc.FILE).write_text('{"schema": "something else"}')
    assert rc.load(tmp_path) is None and rc.load(None) is None


def test_the_not_read_sheet() -> None:
    rows = rc.sheet_rows(_coverage())
    assert rows[0][2].startswith("Numbers read: 3 of about 8")
    spot_rows = [row for row in rows if row[0] == "1"]
    assert [
        "1",
        "Vendor",
        "GV's sticker or markup over it",
        "1",
        "452, 200, 470, 208",
        "pen strokes",
    ] in spot_rows


def test_the_workbook_has_a_not_read_sheet_only_with_coverage() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    import io

    with_sheet = openpyxl.load_workbook(io.BytesIO(not_compared.xlsx([], _coverage())))
    assert with_sheet.sheetnames[-1] == rc.SHEET
    without = openpyxl.load_workbook(io.BytesIO(not_compared.xlsx([])))
    assert rc.SHEET not in without.sheetnames


# ---- the set pack writes it ----------------------------------------------------------------------


def test_the_set_pack_writes_the_coverage_beside_its_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import set_pack

    uploads, folder = tmp_path / "uploads", tmp_path / "pack"
    uploads.mkdir()
    folder.mkdir()
    # 36" on its line (the drawn route), 48" with no line (the OCR route reads it, #1848), and an
    # unmarked 63 no reader reads (counted by the set's own digit shapes).
    body = READ + stroke_ops('48"', 450, 150) + stroke_ops("63", 300, 80)
    (uploads / "set.pdf").write_bytes(_pdf([body]))
    monkeypatch.setattr(set_pack, "_drawn_budget", lambda: 60.0)
    monkeypatch.setattr(dl, "drawn_readers", _engines)
    pages = [{"file": "set.pdf", "page": 1, "halves": HALVES}]
    set_pack.write_dims(folder, uploads, ["set.pdf"], pages)
    labels = json.loads((folder / "dims.json").read_text())["pages"][0]["labels"]
    assert sorted(label["text"] for label in labels) == ['36"', '48"']  # the labels as ever
    coverage = rc.load(folder)
    assert coverage is not None
    assert coverage["sides"]["vendor"]["strokes"] == {"read": 2, "printed": 3}
    assert rc.summary_line(coverage, "vendor") == (
        "Numbers read: 2 of about 3 printed on the vendor's drawings (67%)"
    )
    assert coverage["seconds"] >= 0


def test_a_broken_count_leaves_the_pack_without_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import set_pack

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("counting broke")

    monkeypatch.setattr(ri, "unread_spots", broken)
    set_pack._write_coverage(tmp_path, DOCUMENT, {})
    assert rc.load(tmp_path) is None


# ---- shown on the check ---------------------------------------------------------------------------


def test_a_full_checks_trust_report_carries_it_other_answers_do_not(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from app.review.assistant.chat_engine import ChatEngine

    rc.write(tmp_path, _coverage())

    def engine(mode: str, pack: Path | None) -> Any:
        return SimpleNamespace(
            settings=SimpleNamespace(mode=mode),
            tools=SimpleNamespace(workspace=SimpleNamespace(set_pack=pack)),
        )

    shown = ChatEngine._reading_report(engine("full_check", tmp_path))  # type: ignore[arg-type]
    assert shown is not None and shown["lines"][0].startswith("Numbers read: 3 of about 8")
    assert ChatEngine._reading_report(engine("chat", tmp_path)) is None  # type: ignore[arg-type]
    assert ChatEngine._reading_report(engine("full_check", None)) is None  # type: ignore[arg-type]


def test_the_report_cover_made_again_keeps_the_numbers_read_line() -> None:
    from types import SimpleNamespace

    from app.review.assistant.chat_decisions import decided_summary

    trust = {"counts": {}, "not_checked": [], "reading": rc.report(_coverage())}
    message = SimpleNamespace(blocks=[{"type": "trust", "trust": trust}], model="m", effort="low")
    summary, _ = decided_summary(message, None, [], "set.pdf")  # type: ignore[arg-type]
    assert summary["Numbers read"].startswith("Numbers read: 3 of about 8 printed on the vendor's")
