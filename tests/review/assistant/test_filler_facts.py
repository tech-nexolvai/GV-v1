"""GV's filler arithmetic on a vendor page (`filler_facts`): made-up runs and values only."""

from __future__ import annotations

from collections.abc import Sequence
from fractions import Fraction
from types import SimpleNamespace
from typing import Any

from app.review.assistant import filler_facts
from app.review.assistant.ledger import format_inches

#: Points per inch of the made-up elevations.
SCALE = 3.0


def _phrase(text: str, x0: float, y0: float, x1: float, y1: float) -> dict[str, Any]:
    return {"text": text, "box": [x0, y0, x1, y1], "n": len(text.split())}


def _run(
    parts: Sequence[Fraction | int], x: float = 100.0, y: float = 300.0
) -> list[dict[str, Any]]:
    """A dimension string: each part's value printed centred over its span."""
    out, at = [], x
    for part in parts:
        value = Fraction(part)
        centre = at + float(value) * SCALE / 2
        out.append(_phrase(format_inches(value), centre - 7, y, centre + 7, y + 6))
        at += float(value) * SCALE
    return out


def _note(text: str, x: float, y: float) -> dict[str, Any]:
    return _phrase(text, x, y, x + 60, y + 6)


def test_a_wall_end_with_no_box_filler_gets_gv_s_numbers() -> None:
    found = _run([30, 30, 30, 30]) + [
        _phrase('3"', 80, 315, 92, 321),  # the scribe strip, printed beyond the string
        _note("SCRIBE TO FIT", 100, 330),
        _note('(3" FILLER TO FIELD CUT)', 100, 336.5),
    ]
    (line,) = filler_facts.facts(found)
    assert line.startswith("Run at y 303 (x 138-")
    assert '30" + 30" + 30" + 30" = 120"' in line
    assert 'with the pieces beyond its ends 123"' in line
    assert 'left end: no narrow part in the string, 3" printed beyond the string' in line
    assert 'note "SCRIBE TO FIT (3" FILLER TO FIELD CUT)"' in line
    assert "Please check the left end: no box filler inside the run at this end." in line
    # 2" box filler fixed: the four cabinets give up 2" in all, 1/2" each.
    assert 'the cabinets take -2" in all' in line
    assert '-1/2" each (30" -> 29 1/2")' in line
    assert 'The cabinet next to that end: left 30" (alone: 28")' in line
    # The right end has nothing at it: described, never called out.
    assert "Please check the right end" not in line


def test_a_box_filler_in_gv_s_range_is_described_never_called_out() -> None:
    found = _run([3, 2, 30, 30, 1]) + [_note('SCRIBE TO FIT (5" FILLER)', 100, 330)]
    (line,) = filler_facts.facts(found)
    assert 'left end: 3" + 2" in the string (from the wall in)' in line
    assert 'right end: 1", first cabinet 30"' in line
    assert "Please check" not in line


def test_a_flat_strip_at_a_wall_is_a_flat_strip_not_a_box_filler() -> None:
    found = _run([Fraction(3, 4), 24, 24, 24, 24]) + [_note("FILLER", 80, 330)]
    (line,) = filler_facts.facts(found)
    assert 'Please check the left end: the filler is a flat 3/4" strip, not a box filler.' in line
    # 3/4" -> 2": the cabinets give up 1 1/4", over four cabinets 5/16" each.
    assert 'the cabinets take -1 1/4" in all' in line
    assert '-5/16" each (24" -> 23 11/16")' in line


def test_a_wide_filler_is_a_gv_note_and_a_share_that_does_not_divide_is_never_rounded() -> None:
    found = _run([4, 30, 30, 30]) + [_note("4in FILLER", 80, 330)]
    (line,) = filler_facts.facts(found)
    assert 'the filler 4" is outside GV\'s usual 1"-2" (a note only' in line
    # 4" -> 2": the cabinets take +2" over three: 2/3" each is no whole sixteenth.
    assert 'the cabinets take 2" in all' in line
    assert 'not a whole 1/16": no exact width, GV decides (never rounded)' in line
    assert 'left 30" (alone: 32")' in line


def test_an_end_with_no_wall_words_is_only_described() -> None:
    (line,) = filler_facts.facts(_run([Fraction(3, 4), 27, 27]))
    assert 'left end: 3/4", first cabinet 27"' in line
    assert "Please check" not in line and "GV's settled" not in line


def test_an_end_panel_at_a_scribed_end_names_gv_s_panel_rule() -> None:
    found = _run([2, 30, 30, 30]) + [
        _note("SCRIBE TO FIT", 80, 330),
        _note("E N D P ANEL", 80, 280),
    ]
    (line,) = filler_facts.facts(found)
    assert "no finished end panel against a wall" in line
    assert "Please check the left end" not in line  # the 2" filler itself is fine


def test_two_views_on_one_line_are_two_runs_and_a_two_part_row_is_none() -> None:
    elevation = _run([2, 30, 30, 30], x=100)
    section = _run([20, 4], x=900)  # a section's depth on the same line, far right
    found = elevation + section
    lines = filler_facts.facts(found)
    assert len(lines) == 1
    assert '2" + 30" + 30" + 30" = 92"' in lines[0]


def test_millimetre_pages_split_the_bracketed_values_in_order() -> None:
    found = [
        _phrase("762", 125, 290, 140, 296),
        _phrase("[30]", 125, 297, 140, 303),
        _phrase("762", 215, 290, 230, 296),
        _phrase("[30]", 215, 297, 230, 303),
        _phrase("[3/4][1/4]", 262, 297, 278, 303),
    ]
    (line,) = filler_facts.facts(found)
    assert '30" + 30" + 3/4" + 1/4" = 61"' in line
    assert 'right end: 1/4" + 3/4" in the string (from the wall in)' in line


def test_the_hook_logs_its_lines_and_gives_one_fenced_block() -> None:
    review = SimpleNamespace(found=_run([2, 30, 30]), labels=[], knowledge={})
    (block,) = filler_facts.blocks(review)
    assert block["type"] == "text" and "never red" in block["text"]
    assert '<drawing_data source="filler_facts">' in block["text"]
    assert review.knowledge["filler_facts"] and review.knowledge["filler_facts"][0].startswith(
        "Run"
    )
    empty = SimpleNamespace(found=[_phrase("SECTION A", 10, 10, 60, 16)], labels=[], knowledge={})
    assert filler_facts.blocks(empty) == []
    assert empty.knowledge == {"filler_facts": []}
