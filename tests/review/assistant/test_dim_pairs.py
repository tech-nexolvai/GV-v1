"""Architect against vendor dimensions paired by drawn position (`dim_pairs.py`): pure code.

Labels are made by hand in `dims.json`'s shape: two drawings on one page at different scales, each
with a chain of dimensions on one line, every label with its two extension lines.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant import dim_pairs as dp
from app.review.assistant import view_scales as vs


def _chain(
    side: str, values: list[Fraction], pt_per_in: float, x: float, y: float, first: int
) -> list[dict[str, Any]]:
    labels: list[dict[str, Any]] = []
    at = x
    for number, value in enumerate(values, start=first):
        end = at + float(value) * pt_per_in
        whole, rest = divmod(value, 1)
        text = f'{whole} {rest}"' if rest else f'{whole}"'
        labels.append(
            {
                "id": f"D{number}",
                "text": text,
                "inches": str(float(value)),
                "box": [(at + end) / 2 - 6, y - 8, (at + end) / 2 + 6, y - 2],
                "side": side,
                "orientation": "h",
                "route": "text",
                "dim_line": [[at, y], [end, y]],
                "extension": [[[at, y + 40], [at, y]], [[end, y + 40], [end, y]]],
                "span_pt": end - at,
            }
        )
        at = end
    return labels


def _pairs(vendor: list[Fraction]) -> dict[str, Any]:
    architect = [Fraction(36), Fraction(24), Fraction(30)]
    labels = _chain("architect", architect, 2.0, 100, 100, 1) + _chain(
        "vendor", vendor, 3.0, 100, 400, 4
    )
    scales, checks = vs.page_scales(7, labels)
    return dp.page_pairs(7, labels, scales, checks)


def test_spans_whose_ends_land_on_each_other_pair_and_are_compared_exactly() -> None:
    found = _pairs([Fraction(145, 4), Fraction(95, 4), Fraction(30)])
    by_arch = {p["architect"]["text"]: p for p in found["pairs"]}
    assert by_arch['36"']["status"] == "different"
    assert by_arch['36"']["vendor_in"] == '36 1/4"' and by_arch['36"']["printed_both"]
    assert by_arch['24"']["status"] == "different" and by_arch['24"']["vendor_in"] == '23 3/4"'
    assert by_arch['30"']["status"] == "same"
    assert "paired by drawn position" in by_arch['30"']["how"]


def test_rows_that_do_not_line_up_are_not_paired_by_value() -> None:
    # The vendor's pieces are drawn elsewhere along the row: no alignment, nothing paired, even
    # where a printed value happens to equal the architect's.
    found = _pairs([Fraction(12), Fraction(30), Fraction(48)])
    assert found["pairs"] == []
    assert any("no_fit" in note or "ambiguous" in note for note in found["notes"])


def test_a_label_not_drawn_to_its_value_keeps_its_row_out() -> None:
    architect = _chain("architect", [Fraction(36), Fraction(24), Fraction(30)], 2.0, 100, 100, 1)
    vendor = _chain("vendor", [Fraction(36), Fraction(24), Fraction(30)], 3.0, 100, 400, 4)
    vendor += _chain("vendor", [Fraction(36), Fraction(24)], 3.0, 100, 600, 7)
    vendor[-1]["inches"], vendor[-1]["text"] = "40", '40"'  # typed over: drawn 24", printed 40"
    labels = architect + vendor
    scales, checks = vs.page_scales(7, labels)
    found = dp.page_pairs(7, labels, scales, checks)
    assert any("D8 not drawn to its value" in note for note in found["notes"])
    assert all(p["vendor"][0]["id"] != "D8" for p in found["pairs"])
