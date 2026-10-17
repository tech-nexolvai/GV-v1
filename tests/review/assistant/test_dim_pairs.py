"""Architect against vendor dimensions paired by drawn position (`dim_pairs.py`): pure code.

Labels are made by hand in `dims.json`'s shape: two drawings on one page at different scales, each
with a chain of dimensions on one line, every label with its two extension lines.
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_pairs as dp
from app.review.assistant import view_scales as vs
from app.review.assistant.chat_trust import cloud_kind
from app.review.assistant.full_check_advocate import code_pairs


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


def _page_three() -> list[dict[str, Any]]:
    """Architect D2-D4 (36", 24", 30") and the vendor's D1, D5, D6 (36 1/4", 23 3/4", 30")."""
    architect = _chain("architect", [Fraction(36), Fraction(24), Fraction(30)], 2.0, 100, 100, 2)
    vendor = _chain("vendor", [Fraction(145, 4), Fraction(95, 4), Fraction(30)], 3.0, 100, 400, 4)
    vendor[0]["id"] = "D1"
    return architect + vendor


def test_the_one_to_one_pairs_are_written_as_the_full_check_reads_them() -> None:
    dims = {"pages": [{"file": "set.pdf", "page": 3, "labels": _page_three()}]}
    document = dp.set_pairs(dims, vs.set_scales(dims))
    assert code_pairs(document) == {"p3.D1": "p3.D2", "p3.D5": "p3.D3", "p3.D6": "p3.D4"}
    first = document["pairs"][0]
    assert first["vendor"] == 'p3.D1 36 1/4"' and first["architect"] == 'p3.D2 36"'
    assert first["status"] == "different" and first["printed_both"] is True


def test_a_code_paired_wrong_item_goes_red_through_the_full_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant import test_full_check_roster as roster

    dims = {"pages": [{"file": "set.pdf", "page": 3, "labels": _page_three()}]}
    roster._clean_ink(monkeypatch)
    script = roster._Roster(advocate_pick="D9")  # an AI pick that would object
    harness = roster._roster(tmp_path, script)
    folder = harness.engine.tools.workspace.set_pack
    assert folder is not None
    (folder / dp.PAIRS_FILE).write_text(
        json.dumps(dp.set_pairs(dims, vs.set_scales(dims))), encoding="utf-8"
    )

    outcome = harness.run()

    top = next(
        f
        for f in roster._findings(outcome)
        if f["item"].startswith("Top on page") and f["page"] == 3
    )
    assert top["critic"]["result"] == "passed" and top["critic"]["answers"]["pairing"] == "code"
    assert cloud_kind(top) == "wrong"
