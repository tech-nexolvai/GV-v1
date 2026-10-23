"""Metric and mixed label forms (#1778): read, with the 25.4 cross-check and the notes kept. $0.

Inches count; millimetres only cross-check (they are never a verdict operand):

- inches first with the millimetres in brackets (`31 1/4" [794]`, `25 [635]`) is read as the
  inches when the bracket is them times 25.4, and GV's usual millimetres first (`794 [31 1/4]`)
  reads as before; a marked pair whose numbers disagree is read as its inches (settled with GV:
  "inches count; ignore millimetres"), and its label keeps the millimetres, marked ignored;
- a reference dimension (`(24")`), verify in field (`VIF 24"`) and plus or minus (`24"+/-`) are
  read, each with its flag kept on the label; a value marked VIF or plus or minus is never red;
- a millimetre-only value (`794mm`) is listed on its page, never a label with inches.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels
from app.review.assistant import pair_findings as pf
from app.review.assistant.finding_checks import (
    inch_first_mm,
    printed_mm,
    to_inches,
    units_check,
    value_flags,
)
from tests.review.assistant import test_pair_findings as tpf
from tests.review.assistant import test_simple_verify as tsv
from tests.review.assistant.test_dim_labels import _dimension, _pdf, _text

# ---- to_inches ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        # inches first, millimetres in brackets: the 25.4 cross-check settles the order
        ('31 1/4" [794]', "31.25"),
        ("31 1/4 [794]", "31.25"),
        ("2'-0\" [610]", "24"),
        ('31 1/4" [800]', "31.25"),  # the millimetres disagree: the inches count (settled)
        ('660 [27"]', "27"),  # millimetres first that disagree: the inches count (settled)
        ('914 [36"]', "36"),
        ("25 [635]", "25"),  # was read as 635 inches
        ("24 [610]", "24"),  # was read as 610 inches
        ("610 [2'-0\"]", "24"),  # millimetres first, feet and inches in brackets
        # a reference dimension
        ('(24")', "24"),
        ("(2'-6\")", "30"),
        # verify in field, plus or minus, approximate, reference words
        ('VIF 24"', "24"),
        ('V.I.F. 24"', "24"),
        ('24"+/-', "24"),
        ('24" +/-', "24"),
        ('24"±', "24"),
        ('+/- 24"', "24"),
        ('±24"', "24"),
        ('APPROX. 24"', "24"),
        ('24" +/- VIF', "24"),
        ('REF 24"', "24"),
    ],
)
def test_metric_and_mixed_forms_read_as_exact_inches(text: str, inches: str) -> None:
    assert to_inches(text) == Decimal(inches)


@pytest.mark.parametrize(
    ("text", "inches"),
    [
        # GV's own order and every form read before: unchanged
        ("794 [31 1/4]", "31.25"),
        ('794 [31 1/4"]', "31.25"),
        ('794mm [31 1/4"]', "31.25"),
        ("940 [37]", "37"),
        ("940 (37)", "37"),
        ("457 [18]", "18"),
        ("25 [1]", "1"),
        ("36 [1 1/2]", "1.5"),
        ("305 [10]", "10"),  # the halves disagree: read as before, the units check says so
        ('24" (VIF)', "24"),
        ('24" VIF', "24"),
        ("3'-1\"", "37"),
        ('37"', "37"),
    ],
)
def test_forms_read_before_are_unchanged(text: str, inches: str) -> None:
    assert to_inches(text) == Decimal(inches)


@pytest.mark.parametrize(
    "text",
    [
        "794mm",  # millimetres alone are never inches (and 794 mm is no exact number of inches)
        "794 mm",
        "(24)",  # a bare number in brackets is a count or a tag
        "+/- 24",  # a noted value with no mark is not one sure value
        "VIF 24",
        'REFRIGERATOR 24"',
        "36 x 24",
    ],
)
def test_what_is_still_not_one_value(text: str) -> None:
    assert to_inches(text) is None


@pytest.mark.parametrize(
    ("text", "flags"),
    [
        ('(24")', ("reference",)),
        ('REF 24"', ("reference",)),
        ('VIF 24"', ("vif",)),
        ('24" (VIF)', ("vif",)),
        ('24"+/-', ("approx",)),
        ('±24"', ("approx",)),
        ('24" +/- VIF', ("vif", "approx")),
        ('24"', ()),
        ('31 1/4" [794]', ()),
    ],
)
def test_value_flags(text: str, flags: tuple[str, ...]) -> None:
    assert value_flags(text) == flags


@pytest.mark.parametrize(
    ("text", "units", "mm"),
    [
        ('31 1/4" [794]', "agrees", "794"),
        ("25 [635]", "agrees", "635"),
        ('31 1/4" [800]', "disagrees", "800"),
        ("794 [31 1/4]", "agrees", None),  # GV's order: its own check, no inch-first mm
        ("305 [10]", "disagrees", None),
    ],
)
def test_the_25_4_cross_check_both_ways(text: str, units: str, mm: str | None) -> None:
    assert units_check(text) == units
    assert inch_first_mm(text) == mm


@pytest.mark.parametrize(
    ("text", "mm"),
    [
        ('660 [27"]', "660"),
        ('914 [36"]', "914"),
        ("794 [31 1/4]", "794"),
        ("940 (37)", "940"),
        ('31 1/4" [794]', "794"),
        ('31 1/4" [800]', "800"),
        ('27"', None),
        ("794mm", None),
    ],
)
def test_printed_millimetres_either_way_round(text: str, mm: str | None) -> None:
    assert printed_mm(text) == mm


# ---- the labels ---------------------------------------------------------------------------------


def test_the_labels_read_the_forms_and_keep_their_notes(tmp_path: Path) -> None:
    body = b""
    for n, value in enumerate(['31 1/4" [794]', '(24")', '18"+/-', 'VIF 30"', '12"']):
        x = 60 + 100 * n
        body += _dimension(x, x + 90, 300) + _text(x + 10, 303, value)
    body += _text(100, 200, "WIDTH 794mm") + _text(300, 200, "3mm REVEAL")
    pdf = tmp_path / "forms.pdf"
    pdf.write_bytes(_pdf([body]))
    found = dim_labels.page_labels(pdf, 1, [{"box": [0, 0, 600, 400], "role": "vendor"}])
    labels = {label["text"]: label for label in found.labels}
    assert set(labels) == {'31 1/4" [794]', '(24")', '18"+/-', '30"', '12"'}
    first = labels['31 1/4" [794]']
    assert (first["inches"], first["mm"], first["units"]) == ("31.25", "794", "agrees")
    assert "flags" not in first and "flags" not in labels['12"']
    assert labels['(24")']["flags"] == ["reference"] and labels['(24")']["inches"] == "24"
    assert labels['18"+/-']["flags"] == ["approx"]
    assert labels['30"']["flags"] == ["vif"]  # the VIF printed just before it, in its row
    # Millimetres alone: listed, never a label (a 3 mm reveal is a note, not a dimension).
    assert [(m["text"], m["mm"], m["side"]) for m in found.metric] == [("794mm", "794", "vendor")]


def test_millimetres_first_rows_are_read_as_their_inches(tmp_path: Path) -> None:
    """`914 [36"]` and `660 [27"]` (660 mm is 26"): the label is the inches either way; the
    millimetres are kept as printed, and where they disagree they are ignored (settled with GV:
    "inches count; ignore millimetres"), never a doubt on the label."""
    body = b""
    for n, value in enumerate(['914 [36"]', '660 [27"]']):
        x = 60 + 140 * n
        body += _dimension(x, x + 120, 300) + _text(x + 20, 303, value)
    pdf = tmp_path / "mm-first.pdf"
    pdf.write_bytes(_pdf([body]))
    found = dim_labels.page_labels(pdf, 1, [{"box": [0, 0, 600, 400], "role": "vendor"}])
    labels = {label["text"]: label for label in found.labels}
    agreeing, ignored = labels['914 [36"]'], labels['660 [27"]']
    assert (agreeing["inches"], agreeing["mm"], agreeing["units"]) == ("36", "914", "agrees")
    assert "mm_ignored" not in agreeing
    assert (ignored["inches"], ignored["mm"], ignored["units"]) == ("27", "660", None)
    assert ignored["mm_ignored"] is True


def test_a_page_with_no_metric_value_keeps_its_dims_entry_as_before(tmp_path: Path) -> None:
    pdf = tmp_path / "plain.pdf"
    pdf.write_bytes(_pdf([_dimension(100, 200, 300) + _text(140, 303, '30"')]))
    document = dim_labels.build_dims({"plain.pdf": pdf}, None)
    assert set(document["pages"][0]) == {
        "file",
        "page",
        "drawn",
        "labels",
        "scale_notes",
        "phrases",
    }


# ---- never red -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flag", "words"), [("vif", "verify in field"), ("approx", "plus or minus")]
)
def test_a_value_marked_vif_or_plus_or_minus_is_never_red(flag: str, words: str) -> None:
    docs = tsv._documents()
    for entry in docs["dims"]["pages"]:
        if entry["page"] == 2:
            entry["labels"][0] = {**entry["labels"][0], "flags": [flag]}
    pack = tsv.Pack.from_documents(main_file=tsv.FILE, **docs)
    found = tsv._one(tsv._pair("p2.D1", "p1.D1"), pack=pack)
    assert found.outcome == "not_compared"
    assert words in found.reason and found.reason_code == "not_proved"


def test_a_reference_dimension_can_still_be_red() -> None:
    docs = tsv._documents()
    for entry in docs["dims"]["pages"]:
        if entry["page"] == 2:
            entry["labels"][0] = {**entry["labels"][0], "flags": ["reference"]}
    pack = tsv.Pack.from_documents(main_file=tsv.FILE, **docs)
    assert tsv._one(tsv._pair("p2.D1", "p1.D1"), pack=pack).outcome == "candidate"


def test_a_code_pair_on_a_vif_value_is_a_check_never_wrong() -> None:
    marked: dict[str, Any] = {**tpf._label("D10", '7" VIF', "7", "vendor", 100), "flags": ["vif"]}
    dims = tpf._dims()
    dims["pages"][0]["labels"] = [
        marked if label["id"] == "D10" else label for label in dims["pages"][0]["labels"]
    ]
    found = pf.findings_from(
        tpf._pairs(tpf._pair(["D1"], "D10", "the left wall to outlet 1")), dims, tpf.PAGE_MAP
    )
    assert [f["status"] for f in found] == ["check"]
    assert "verify in field or plus or minus" in found[0]["why"]
