"""Scoring the architect reader on a synthetic key (#1052). No client value appears here.

Verification for: `eval/architect_reader.py`.
"""

from __future__ import annotations

from fractions import Fraction

import pytest

from eval.architect_reader import ReadSpan, key_pages, score_page, score_pages


def _usable(text: str, inches: int) -> ReadSpan:
    return ReadSpan(text, Fraction(inches), Fraction(inches), None)


def _held(text: str, inches: int, reason: str) -> ReadSpan:
    return ReadSpan(text, Fraction(inches), None, reason)


def test_usable_values_are_found_held_values_are_held_and_the_rest_is_missing() -> None:
    key = [Fraction(10), Fraction(20), Fraction(20), Fraction(30)]
    spans = [
        _usable("1'-8\"", 20),
        _held("2'-6\"", 30, "no scale witness"),
        _usable('10"', 10),
    ]

    score = score_page(4, key, spans)

    assert score.found == (Fraction(20), Fraction(10))
    assert score.held == ((Fraction(30), "no scale witness"),)
    assert score.missing == (Fraction(20),)
    assert score.extra_usable == ()
    assert score.key_count == 4


def test_a_usable_value_the_key_does_not_list_is_reported_for_a_person() -> None:
    score = score_page(1, [Fraction(10)], [_usable('10"', 10), _usable('11"', 11)])

    assert score.found == (Fraction(10),)
    assert score.extra_usable == ((Fraction(11), '11"'),)


def test_a_usable_span_wins_a_key_width_before_a_held_one() -> None:
    """Matched usable-first, so a width printed twice and read once usable counts as found."""
    spans = [_held("1'-0\"", 12, "two labels"), _usable("1'-0\"", 12)]

    score = score_page(2, [Fraction(12)], spans)

    assert score.found == (Fraction(12),)
    assert score.held == ()
    assert score.extra_held == ((Fraction(12), "1'-0\"", "two labels"),)


def test_matching_is_exact_never_near() -> None:
    score = score_page(3, [Fraction(42)], [ReadSpan("x", Fraction(83, 2), Fraction(83, 2), None)])

    assert score.found == ()
    assert score.missing == (Fraction(42),)
    assert score.extra_usable == ((Fraction(83, 2), "x"),)


def test_key_pages_reads_the_key_shape_and_refuses_a_float() -> None:
    key = {
        "pages": [
            {"page": 2, "arch_widths": [{"text": "3'-6\"", "in": 42}, {"text": "x", "in": "85/2"}]},
            {"page": 3, "arch_widths": []},
        ]
    }
    pages = key_pages(key)

    assert pages[0].widths == (Fraction(42), Fraction(85, 2))
    assert pages[1].widths == ()
    with pytest.raises(ValueError):
        key_pages({"pages": [{"page": 1, "arch_widths": [{"in": 42.5}]}]})


def test_score_pages_totals_every_page_in_the_key_or_read() -> None:
    key = key_pages(
        {
            "pages": [
                {"page": 1, "arch_widths": [{"in": 10}]},
                {"page": 2, "arch_widths": [{"in": 5}]},
            ]
        }
    )
    card = score_pages(key, {1: [_usable('10"', 10)], 7: [_usable('9"', 9)]})

    assert [page.page for page in card.pages] == [1, 2, 7]
    assert card.totals == {
        "key_widths": 2,
        "found": 1,
        "held": 0,
        "missing": 1,
        "extra_usable": 1,
        "extra_held": 0,
    }
