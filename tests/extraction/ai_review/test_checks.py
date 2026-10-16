"""Code decides the AI page review's rows: exact inches, never the AI's say alone (decision 2026-10-16).

Verification for `extraction/ai_review/checks.py`. Synthetic numbers only.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from extraction.ai_review.checks import ArchitectListed, check_rows, parse_inches, plausible_box


@pytest.mark.parametrize(
    ("printed", "inches"),
    [
        ("610 [24]", Fraction(24)),
        ("1067 [42 1/2]", Fraction(85, 2)),
        ('25 1/2"', Fraction(51, 2)),
        ('31"', Fraction(31)),
        ("2'-7\"", Fraction(31)),
        ("2’-10 1/2”", Fraction(69, 2)),
        ("5'-0\"", Fraction(60)),
        ("787", None),
        ("", None),
        ("EQ", None),
        ("610 [24] [25]", None),
        (None, None),
    ],
)
def test_parse_inches_reads_only_exact_inches(printed: str | None, inches: Fraction | None) -> None:
    assert parse_inches(printed) == inches


LISTED = [
    ArchitectListed("A1", "2'-7\"", Fraction(31)),
    ArchitectListed("A2", "1'-3\"", Fraction(15)),
    ArchitectListed("A3", '4"', Fraction(4)),
    ArchitectListed("A4", "6'-0\"", Fraction(72)),
]


def _row(ref: str, text: str, vendor: str, result: str, box: list[int] | None = None) -> dict:
    return {
        "architect_id": ref,
        "architect_text": text,
        "measures": "floor to counter top",
        "vendor_text": vendor,
        "result": result,
        "why": "synthetic",
        "vendor_box": box or [],
    }


def test_agree_and_disagree_are_decided_by_exact_arithmetic() -> None:
    rows, dropped = check_rows(
        LISTED,
        [
            _row("A1", "2'-7\"", "787 [31]", "SAME", [100, 100, 140, 120]),
            _row("A2", "1'-3\"", "432 [17]", "DIFFERENT", [500, 500, 540, 520]),
            _row("A3", '4"', "102 [4]", "DIFFERENT"),  # the AI says different, numbers agree
            _row("A4", "6'-0\"", "1829 [73]", "SAME"),  # the AI says same, numbers differ
        ],
    )
    assert not dropped
    assert [r.result for r in rows] == ["same", "different", "unsure", "unsure"]
    assert rows[0].vendor_inches == 31 and rows[0].vendor_box == (100, 100, 140, 120)
    assert rows[1].vendor_inches == 17


def test_unparseable_vendor_number_is_unsure_never_different() -> None:
    rows, _ = check_rows(LISTED[:1], [_row("A1", "2'-7\"", "787", "DIFFERENT")])
    assert rows[0].result == "unsure"


def test_markup_hidden_number_stays_unreadable_and_not_shown_is_kept() -> None:
    rows, _ = check_rows(
        LISTED[:2],
        [
            _row("A1", "2'-7\"", "", "UNREADABLE"),
            _row("A2", "1'-3\"", "", "NOT_SHOWN"),
        ],
    )
    assert [r.result for r in rows] == ["unreadable", "not_shown"]
    assert all(r.vendor_inches is None and r.vendor_box is None for r in rows)


def test_rows_code_did_not_read_are_dropped_and_missing_ones_listed() -> None:
    rows, dropped = check_rows(
        LISTED[:2],
        [
            _row("A9", "3'-4\"", "1016 [40]", "SAME"),  # not listed
            _row("A1", "2'-8\"", "813 [32]", "SAME"),  # not the number code read
            _row("A2", "1'-3\"", "381 [15]", "SAME"),
            _row("A2", "1'-3\"", "381 [15]", "SAME"),  # a second answer
        ],
    )
    assert [d.architect_id for d in dropped] == ["A9", "A1", "A2"]
    assert [r.ref for r in rows] == ["A1", "A2"]
    assert rows[0].result == "unsure" and rows[0].ai_result is None
    assert rows[1].result == "same"


@pytest.mark.parametrize(
    ("box", "kept"),
    [
        ([10, 10, 60, 30], True),
        ([10, 10, 900, 30], False),
        ([60, 10, 10, 30], False),
        ([0, 0, 1001, 10], False),
        ([1, 2, 3], False),
        ("10,10,20,20", False),
    ],
)
def test_vendor_box_must_be_plausible(box: object, kept: bool) -> None:
    assert (plausible_box(box) is not None) is kept
