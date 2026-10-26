"""Pieces of a row of sizes paired by where each starts along the row: made-up rows, no client data.

A side counter 80" long is dimensioned on both drawings as a row of sizes on one upright line
(2 points to the inch on the vendor's, 3 on the reference's). The vendor's row is 20 + 14 + 24 +
22; its 24" (a range gap, say) starts 34" along. The reference prints two 24" pieces, one starting
10" along and one 34" along. An item the AI called the same pairs the vendor's 24" with one of them.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

import pytest

from app.review.assistant import dim_ends, proof, row_places, scale_fit
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"


def _row(
    prefix: str, sizes: list[float], *, row: float, scale: float, start: float = 100.0
) -> list[dim_ends.Span]:
    """Sizes end to end on one upright line at `row`, from `start` (points)."""
    out: list[dim_ends.Span] = []
    at = start
    for n, size in enumerate(sizes, 1):
        length = size * scale
        value = Fraction(size).limit_denominator(16)
        out.append(dim_ends.Span(f"{prefix}{n}", "v", row, at, at + length, value, f'{size:g}"'))
        at += length
    return out


def _page(file: str, spans: list[dim_ends.Span]) -> dim_ends.Page:
    return dim_ends.Page(file, 1, spans={s.id: s for s in spans})


def _labels(spans: list[dim_ends.Span], side: str) -> list[dict[str, Any]]:
    return [
        {
            "id": s.id,
            "text": s.text,
            "inches": str(float(s.inches)),
            "box": [s.row + 2, (s.a + s.b) / 2 - 3, s.row + 14, (s.a + s.b) / 2 + 3],
            "side": side,
            "orientation": "h",
            "route": "text",
        }
        for s in spans
    ]


def vendor_spans(sizes: tuple[float, ...] = (20, 14, 24, 22)) -> list[dim_ends.Span]:
    return _row("V", list(sizes), row=300.0, scale=2.0)


def reference_spans(
    sizes: tuple[float, ...] = (10, 24, 24, 22), *, beside: tuple[float, ...] | None = None
) -> list[dim_ends.Span]:
    spans = _row("R", list(sizes), row=500.0, scale=3.0)
    if beside is not None:
        spans += _row("S", list(beside), row=520.0, scale=3.0)
    return spans


def _finding(vendor_id: str, reference_id: str, value: str = "24") -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "Kitchen: side counter piece",
        "vendor": f'{value}"',
        "architect": f'{value}"',
        "status": "same",
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": value, "architect": value, "difference": "0"},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "witnesses": ["printed"],
        "dims": {
            "vendor": {"id": vendor_id, "page": 1, "file": VENDOR, "inches": value},
            "architect": {"id": reference_id, "page": 1, "file": REFERENCE, "inches": value},
        },
    }


def _pages(ven: list[dim_ends.Span], ref: list[dim_ends.Span]) -> row_places.Pages:
    found = {VENDOR: _page(VENDOR, ven), REFERENCE: _page(REFERENCE, ref)}
    return lambda file, page: found.get(file)


# ---- the record ----------------------------------------------------------------------------------


def test_the_same_piece_is_recorded_and_nothing_else_changes() -> None:
    record = row_places.judge(_finding("V3", "R3"), _pages(vendor_spans(), reference_spans()))
    assert record is not None
    assert record["outcome"] == "same_place"
    assert record["vendor_start"] == record["reference_start"] == '34"'


def test_another_piece_of_the_same_value_is_re_pointed_to_the_one_that_starts_there() -> None:
    record = row_places.judge(_finding("V3", "R2"), _pages(vendor_spans(), reference_spans()))
    assert record is not None
    assert record["outcome"] == "repointed"
    assert record["architect"] == f"{REFERENCE}|1|R3"
    assert record["was"] == f"{REFERENCE}|1|R2"
    assert '34"' in record["why"] and '10"' in record["why"]


def test_with_no_piece_starting_there_the_item_is_elsewhere() -> None:
    ref = reference_spans((10, 24, 46))
    record = row_places.judge(_finding("V3", "R2"), _pages(vendor_spans(), ref))
    assert record is not None
    assert record["outcome"] == "elsewhere"
    assert record["why"] == (
        'the vendor\'s 24" starts at 34" along its row of sizes; the reference\'s 24" starts at 10"'
    )


def test_a_row_beside_over_the_same_stretch_is_searched_too() -> None:
    # The AI's row has no 24" at 34"; the row beside it (same ends, same total) has one.
    ref = reference_spans((10, 24, 46), beside=(6, 28, 24, 22))
    record = row_places.judge(_finding("V3", "R2"), _pages(vendor_spans(), ref))
    assert record is not None
    assert record["outcome"] == "repointed"
    assert record["architect"] == f"{REFERENCE}|1|S3"


def test_a_mirrored_row_is_read_the_other_way_round() -> None:
    # A reversed unit: the vendor's row runs the other way (22 + 24 + 14 + 20).
    ven = vendor_spans((22, 24, 14, 20))
    assert row_places.judge(_finding("V2", "R3"), _pages(ven, reference_spans()))["outcome"] == (
        "same_place"
    )
    assert row_places.judge(_finding("V2", "R2"), _pages(ven, reference_spans()))["outcome"] == (
        "repointed"
    )


# ---- never a guess -------------------------------------------------------------------------------


def test_rows_with_different_totals_are_not_measured() -> None:
    ref = reference_spans((10, 24, 24, 23))
    assert row_places.judge(_finding("V3", "R2"), _pages(vendor_spans(), ref)) is None


def test_a_way_round_not_known_decides_nothing() -> None:
    # 20 + 24 + 20 against 20 + 24 + 20 read either way: every joint falls on a joint both ways.
    ven = vendor_spans((20, 24, 20))
    ref = reference_spans((20, 24, 20))
    assert row_places.judge(_finding("V2", "R2"), _pages(ven, ref)) is None


def test_a_label_without_a_line_is_not_measured() -> None:
    ven = [s for s in vendor_spans() if s.id != "V3"]
    assert row_places.judge(_finding("V3", "R2"), _pages(ven, reference_spans())) is None


def test_a_size_alone_on_its_line_is_not_a_piece() -> None:
    ven = _row("V", [80], row=300.0, scale=2.0)
    ref = reference_spans()
    assert row_places.judge(_finding("V1", "R1", "80"), _pages(ven, ref)) is None


def test_two_sizes_at_one_joint_are_not_one_row() -> None:
    ven = vendor_spans()
    ven.append(dim_ends.Span("V9", "v", 300.0, ven[1].b, ven[1].b + 30.0, Fraction(15), '15"'))
    assert row_places.judge(_finding("V3", "R2"), _pages(ven, reference_spans())) is None


def test_the_row_under_a_printed_overall_is_measured() -> None:
    # The vendor's line runs on with a 10" splash piece before the counter; its 80" overall
    # (printed beside the row) bounds the counter's pieces.
    ven = _row("V", [10, 20, 14, 24, 22], row=300.0, scale=2.0, start=80.0)
    ven.append(dim_ends.Span("V9", "v", 330.0, ven[1].a, ven[-1].b, Fraction(80), '80"'))
    record = row_places.judge(_finding("V4", "R2"), _pages(ven, reference_spans()))
    assert record is not None
    assert record["outcome"] == "repointed"
    assert record["architect"] == f"{REFERENCE}|1|R3"


def test_a_vendor_field_cut_at_the_row_s_end_is_left_out() -> None:
    ven = _row("V", [1, 20, 14, 24, 22], row=300.0, scale=2.0)
    page = _page(VENDOR, ven)
    page.field_cuts = type("Cuts", (), {"labels": frozenset({"V1"})})()
    found = {VENDOR: page, REFERENCE: _page(REFERENCE, reference_spans())}
    record = row_places.judge(_finding("V4", "R2"), lambda f, p: found.get(f))
    assert record is not None
    assert record["outcome"] == "repointed"


# ---- a line split by its text --------------------------------------------------------------------


def _fit(spans: list[dim_ends.Span], gap: scale_fit.Drawn, views: dict[str, int]) -> Any:
    fit = scale_fit.PageFit()
    for s in spans:
        fit.spans[s.id] = scale_fit.Drawn(s.id, s.axis, s.row, s.a, s.b, s.inches, "marks")
        fit.sides[s.id] = "vendor"
        fit.checks[s.id] = scale_fit.Check(s.id, s.axis, s.inches, float(s.inches), True)
    fit.spans[gap.id] = gap
    fit.sides[gap.id] = "vendor"
    fit.views.update(views)
    box = (0.0, 0.0, 1000.0, 1000.0)
    fit.fits = [
        scale_fit.AxisFit("vendor", view, "v", 0.5, (), 0.0, box) for view in set(views.values())
    ]
    return fit


def test_a_line_split_by_its_text_fills_its_gap_at_the_neighbours_scale() -> None:
    spans = vendor_spans()
    gap = spans.pop(2)
    page = _page(VENDOR, spans)
    drawn = scale_fit.Drawn(gap.id, "v", gap.row, gap.a, gap.b, gap.inches, "marks")
    page.fit = _fit(spans, drawn, {"V1": 1, "V2": 1, "V4": 2})
    assert "V3" in row_places.lines_of(page)
    found = {VENDOR: page, REFERENCE: _page(REFERENCE, reference_spans())}
    record = row_places.judge(_finding("V3", "R2"), lambda f, p: found.get(f))
    assert record is not None and record["outcome"] == "repointed"


def test_a_gap_line_not_drawn_to_its_value_is_never_used() -> None:
    spans = vendor_spans()
    gap = spans.pop(2)
    page = _page(VENDOR, spans)
    drawn = scale_fit.Drawn(gap.id, "v", gap.row, gap.a, gap.b, Fraction(30), "marks")
    page.fit = _fit(spans, drawn, {"V1": 1, "V2": 1, "V4": 1})
    assert "V3" not in row_places.lines_of(page)


# ---- the escalation and the outcome --------------------------------------------------------------


def _escalated(finding: dict[str, Any], ref: list[dim_ends.Span]) -> dict[str, Any]:
    ven = vendor_spans()
    ctx = EscalationContext(
        main_file=VENDOR,
        labels={(VENDOR, 1): _labels(ven, "vendor"), (REFERENCE, 1): _labels(ref, "architect")},
        pages=_pages(ven, ref),
    )
    result = escalate([finding], ctx)
    changed = {str(f["id"]): f for f in result.changed}
    return judged(changed.get("F1", finding))


def test_the_escalation_points_the_item_at_code_s_pick_and_keeps_it_right() -> None:
    found = _escalated(_finding("V3", "R2"), reference_spans())
    assert found["dims"]["architect"]["id"] == "R3"
    assert found["arch_box"]["y0"] == pytest.approx(_labels(reference_spans(), "x")[2]["box"][1])
    assert found["checks"]["verify_architect"]["label"] == "R3"
    assert found[row_places.FIELD]["outcome"] == "repointed"
    assert proof.proof_group(found) == "right"
    # Run again: code's own pick stands, its record kept.
    again = _escalated(found, reference_spans())
    assert again[row_places.FIELD]["outcome"] == "repointed"
    assert again["dims"]["architect"]["id"] == "R3"


def test_with_no_matching_piece_the_item_is_not_compared_never_red() -> None:
    found = _escalated(_finding("V3", "R2"), reference_spans((10, 24, 46)))
    assert found["dims"]["architect"]["id"] == "R2"
    assert proof.proof_group(found) == "not_compared"
    classified = proof.classified(found)
    assert classified.outcome == "not_compared"
    assert 'starts at 34"' in classified.why


def test_a_stale_record_is_ignored_once_the_item_pairs_other_labels() -> None:
    found = _escalated(_finding("V3", "R2"), reference_spans((10, 24, 46)))
    found["dims"]["architect"]["id"] = "R9"
    assert row_places.elsewhere_reason(found) is None
