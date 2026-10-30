"""Each printed number of a vendor string gets its own spot (`string_spots`, 2026-10-31).

- The labels a finding names (code's consistency check: `p<page>.<id>`, the overall first) are used
  when their values are the printed ones.
- Else the parts are the one row of labels along the overall's dimension line, touching end to end,
  with the same values in either order; two such rows, or none, place no part.
- Else, inside the finding's box, a value is placed only when it is unambiguous there, among the
  labels running the row's way.
- Nothing is guessed: an unplaced part keeps its value and no box; a finding with nothing placed is
  left as it was. The read model accepts the result.

Made-up drawings and values only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.review.assistant.chat_contract import ChatFindingOut
from app.review.assistant.string_spots import PART_BOXES, TOTAL_BOX, blocks_with_spots, spots_of

FILE = "vendor.pdf"
PAGE = 4


def _label(
    label: str,
    text: str,
    inches: str,
    box: tuple[float, float, float, float],
    line: tuple[tuple[float, float], tuple[float, float]] | None,
    orientation: str = "v",
) -> dict[str, Any]:
    return {
        "id": label,
        "text": text,
        "inches": inches,
        "box": list(box),
        "orientation": orientation,
        "dim_line": None if line is None else [list(line[0]), list(line[1])],
    }


# A made-up height row drawn up the page: 3" + 50" + 3" against a 54" overall, with the overall's
# line just outside the parts' line (x 210 vs 200).
TOP = _label("D1", '3"', "3", (186.0, 100.0, 198.0, 112.0), ((200.0, 100.0), (200.0, 120.0)))
MIDDLE = _label("D2", '50"', "50", (186.0, 300.0, 198.0, 316.0), ((200.0, 120.0), (200.0, 500.0)))
BOTTOM = _label("D3", '3"', "3", (186.0, 504.0, 198.0, 516.0), ((200.0, 500.0), (200.0, 520.0)))
OVERALL = _label("D4", '54"', "54", (204.0, 300.0, 208.0, 312.0), ((210.0, 100.0), (210.0, 520.0)))
# Another 3" on the page, far from the row (a different view).
ELSEWHERE = _label("D5", '3"', "3", (600.0, 40.0, 612.0, 52.0), ((614.0, 30.0), (614.0, 50.0)))

LABELS = [TOP, MIDDLE, BOTTOM, OVERALL, ELSEWHERE]


def _labels_of(
    labels: Sequence[Mapping[str, Any]],
) -> Any:
    def labels_of(file: str | None, page: int) -> Sequence[Mapping[str, Any]] | None:
        return labels if (file, page) == (FILE, PAGE) else None

    return labels_of


def _finding(**change: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "page": PAGE,
        "item": "Sample unit A: height string",
        "category": "other",
        "architect": '56"',
        "vendor": '54"',
        "status": "different",
        "verified": False,
        "source": "vendor_arithmetic",
        "created_by": "page_reviewer",
        "parts": ['3"', '50"', '3"'],
        "printed_total": '54"',
        "vendor_box": {
            "page": PAGE,
            "x0": 180.0,
            "y0": 280.0,
            "x1": 215.0,
            "y1": 330.0,
            "file": FILE,
        },
        "dims": {
            "vendor": {
                "id": "D4",
                "page": PAGE,
                "file": FILE,
                "text": '54"',
                "inches": "54",
                "route": "drawn",
                "box": [204.0, 300.0, 208.0, 312.0],
            }
        },
    }
    finding.update(change)
    return finding


def _boxes(found: Mapping[str, Any]) -> list[tuple[float, float] | None]:
    return [
        None if part["box"] is None else (part["box"]["x0"], part["box"]["y0"])
        for part in found[PART_BOXES]
    ]


def test_the_row_along_the_overall_places_every_part_in_order() -> None:
    found = spots_of(_finding(), _labels_of(LABELS))
    assert found is not None
    assert found[TOTAL_BOX]["box"] == {
        "page": PAGE,
        "x0": 204.0,
        "y0": 300.0,
        "x1": 208.0,
        "y1": 312.0,
        "file": FILE,
    }
    assert found[TOTAL_BOX]["inches"] == "54"
    assert [part["text"] for part in found[PART_BOXES]] == ['3"', '50"', '3"']
    assert _boxes(found) == [(186.0, 100.0), (186.0, 300.0), (186.0, 504.0)]
    # Written up the page: the screen may turn its close-up.
    assert all(part["turned"] for part in found[PART_BOXES])


def test_the_row_is_found_whichever_way_the_parts_are_listed() -> None:
    found = spots_of(_finding(parts=['50"', '3"', '3"']), _labels_of(LABELS))
    # 50 + 3 + 3 is no row on the drawing (3, 50, 3): no chain; inside the box only the 50" is sure.
    assert found is not None and _boxes(found) == [(186.0, 300.0), None, None]
    reversed_row = [BOTTOM, MIDDLE, TOP]
    found = spots_of(_finding(parts=['3"', '50"', '3"']), _labels_of([*reversed_row, OVERALL]))
    assert found is not None and _boxes(found) == [(186.0, 100.0), (186.0, 300.0), (186.0, 504.0)]


def test_two_rows_that_both_fit_place_no_part_by_the_row() -> None:
    twin = [
        {**label, "id": f"T{label['id']}", "dim_line": [[195.0, p[1]] for p in label["dim_line"]]}
        for label in (TOP, MIDDLE, BOTTOM)
    ]
    found = spots_of(_finding(), _labels_of([*LABELS, *twin]))
    # Two rows: the box decides only where a value is unambiguous (the 50" has two labels too).
    assert found is not None
    assert _boxes(found) == [None, None, None]
    assert found[TOTAL_BOX]["box"] is not None


def test_code_named_labels_are_used_when_their_values_are_printed() -> None:
    consistency = _finding(
        created_by="consistency", ledger_ids=["p4.D4", "p4.D1", "p4.D2", "p4.D3"]
    )
    found = spots_of(consistency, _labels_of(LABELS))
    assert found is not None and _boxes(found) == [(186.0, 100.0), (186.0, 300.0), (186.0, 504.0)]
    # A named label whose value is not the printed one is not used for it.
    wrong = _finding(ledger_ids=["p4.D4", "p4.D5", "p4.D2", "p4.D2"])
    found = spots_of(wrong, _labels_of(LABELS))
    assert found is not None and _boxes(found) == [(186.0, 100.0), (186.0, 300.0), (186.0, 504.0)]


def test_inside_the_box_only_unambiguous_values_running_the_rows_way() -> None:
    # No overall label: the box (tall: a row up the page) decides.
    tall = {"page": PAGE, "x0": 180.0, "y0": 90.0, "x1": 202.0, "y1": 330.0, "file": FILE}
    found = spots_of(_finding(dims=None, vendor_box=tall), _labels_of([TOP, MIDDLE]))
    assert found is not None and found[TOTAL_BOX] is None
    # One 3" in the box for two 3" parts: neither is placed; the 50" is.
    assert _boxes(found) == [None, (186.0, 300.0), None]
    # A label whose dimension line runs across is never a part of a row up the page.
    across = {**MIDDLE, "dim_line": [[120.0, 318.0], [170.0, 318.0]], "orientation": "h"}
    assert spots_of(_finding(dims=None, vendor_box=tall), _labels_of([across])) is None


def test_nothing_placed_or_no_string_leaves_the_finding_as_it_was() -> None:
    assert spots_of(_finding(), _labels_of([ELSEWHERE])) is None
    assert spots_of(_finding(parts=None), _labels_of(LABELS)) is None
    assert spots_of(_finding(printed_total=None), _labels_of(LABELS)) is None
    assert spots_of(_finding(), _labels_of([])) is None
    blocks = [{"type": "text", "text": "Done."}, {"type": "finding", "finding": _finding()}]
    assert blocks_with_spots(blocks, None) == blocks
    out = blocks_with_spots(blocks, _labels_of(LABELS))
    assert out[0] == blocks[0]
    assert PART_BOXES in out[1]["finding"] and PART_BOXES not in blocks[1]["finding"]


def test_the_read_model_accepts_the_spots() -> None:
    found = spots_of(_finding(), _labels_of([MIDDLE, OVERALL]))
    assert found is not None
    shown = ChatFindingOut.model_validate({**_finding(), **found})
    assert shown.total_box is not None and shown.total_box.box is not None
    assert shown.part_boxes is not None
    assert [part.box is not None for part in shown.part_boxes] == [False, True, False]
    assert [part.text for part in shown.part_boxes] == ['3"', '50"', '3"']
    assert shown.part_boxes[1].turned is True and shown.part_boxes[0].inches == "3"
