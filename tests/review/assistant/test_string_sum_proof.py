"""The vendor's own dimension strings: when code proves one (red), when it only asks (please-check),
when it shows the list is not the string (Not compared), and how the sum is named on screen and in
the files (Set A, 2026-11-04).

Settled with GV: inches count, millimetres are ignored, exact match (1/8" off is red), flag
everything. Every sheet and value here is made up: small synthetic PDFs read by the real label and
line-work readers, or `dims.json`-shaped labels. Nothing calls a model or reads a client drawing.
"""

from __future__ import annotations

import csv
import io
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import chat_drawing, ledger, proof
from app.review.assistant import vendor_consistency as vc
from app.review.assistant.chat_trust import findings_csv
from app.review.assistant.send_package import REGISTER_COLUMNS, register_rows
from app.review.assistant.vendor_text import PARTS_ADD_UP, reference_value
from tests.review.assistant.test_proof_sentence import _context
from tests.review.assistant.test_vendor_consistency import (
    FILE,
    _check,
    _dim,
    _ledger_string,
    _line,
    _LiveBook,
    _raw,
    _row_result,
    _text,
)
from tests.review.assistant.test_vendor_consistency import _Engine as _BaseEngine

# ---- a string ticked only at its joints (code's own check) --------------------------------------


def _part(x0: float, x1: float, y: float, text: str, *, start: bool, end: bool) -> bytes:
    """A horizontal part of a row (PDF space) with a tick only at the ends asked for: a drafter
    who ticks the joints draws none at the row's two outer ends."""
    body = _line(x0, y, x1, y) + _line(x0, y - 90, x0, y + 3) + _line(x1, y - 90, x1, y + 3)
    if start:
        body += _line(x0 - 1.5, y - 1.5, x0 + 1.5, y + 1.5)
    if end:
        body += _line(x1 - 1.5, y - 1.5, x1 + 1.5, y + 1.5)
    return body + _text((x0 + x1) / 2 - 5, y + 2, text)


def _joints_only(*, row_y: float = 280, runs_on: bool = False, joint: bool = True) -> bytes:
    """20" + 41" (the 41" drawn 40", 2 pt per inch) under a 60" overall ticked at both ends; the
    parts' row ticked only at its joint. `runs_on`: the row's line goes on past its right end (a
    line cut where another crosses it). `joint`: the joint's tick is drawn."""
    body = (
        _dim(300, 420, 300, '60"')
        + _part(300, 340, row_y, '20"', start=False, end=joint)
        + _part(340, 420, row_y, '41"', start=joint, end=False)
        + _dim(450, 500, 150, '25"')
    )
    if runs_on:
        body += _line(420, row_y, 445, row_y)
    return body


def test_a_row_ticked_only_at_its_joints_is_proved_like_a_fully_ticked_one(tmp_path: Path) -> None:
    """The overall's ends are marked, the row is the next one under it, and nothing carries the
    row's line on past its outer ends: the bare outer ends are the string's real ends, so code
    proves the string exactly as when every end is ticked."""
    (tmp_path / "bare").mkdir()
    bare = _check(tmp_path / "bare", _joints_only())
    assert bare.strings["does_not_add_up"] == 1 and bare.strings["doubt"] == 0
    (finding,) = bare.findings
    assert finding["created_by"] == vc.CREATED_BY and finding["status"] == "different"
    assert finding["parts"] == ['20"', '41"'] and finding["printed_total"] == '60"'


def test_a_bare_outer_end_whose_line_runs_on_is_never_taken_for_an_end(tmp_path: Path) -> None:
    """The row's line goes on past its right end: it is cut there by a crossing line, so the end
    without its own mark proves nothing (no finding)."""
    result = _check(tmp_path, _joints_only(runs_on=True))
    assert result.strings["does_not_add_up"] == 0 and result.findings == []
    assert any("end mark" in check.reason for check in result.checks if check.verdict == "doubt")


def test_every_joint_still_needs_its_own_mark(tmp_path: Path) -> None:
    result = _check(tmp_path, _joints_only(joint=False))
    assert result.strings["does_not_add_up"] == 0 and result.findings == []


def test_a_row_far_from_its_overall_needs_its_outer_marks(tmp_path: Path) -> None:
    """A bare-ended row 100 pt away (another part of the view lies between): code cannot show it
    is the overall's own row, so it is a doubt; with its ends ticked it is proved as before."""
    (tmp_path / "far").mkdir()
    (tmp_path / "ticked").mkdir()
    far = _check(tmp_path / "far", _joints_only(row_y=200))
    assert far.strings["does_not_add_up"] == 0 and far.findings == []
    ticked = _check(
        tmp_path / "ticked",
        _dim(300, 420, 300, '60"')
        + _dim(300, 340, 200, '20"')
        + _dim(340, 420, 200, '41"')
        + _dim(450, 500, 150, '25"'),
    )
    assert ticked.strings["does_not_add_up"] == 1


def _two_views() -> bytes:
    """Two views of one width on one sheet, one above the other, each ticked at the joints only:
    the top prints 60 1/8" over 20" + 40", the bottom 60 1/8" over 15" + 45" (every line drawn
    to its value but the overalls, drawn 60")."""
    return (
        _dim(300, 420, 330, '60 1/8"')
        + _part(300, 340, 310, '20"', start=False, end=True)
        + _part(340, 420, 310, '40"', start=True, end=False)
        + _dim(300, 420, 120, '60 1/8"')
        + _part(300, 330, 100, '15"', start=False, end=True)
        + _part(330, 420, 100, '45"', start=True, end=False)
        + _dim(450, 500, 200, '25"')
    )


def test_two_views_printing_the_same_overall_are_each_judged_on_their_own_row(
    tmp_path: Path,
) -> None:
    """Each overall label is judged with each row: the lower view's overall is not skipped because
    the upper view prints the same value, and a row across the sheet is never its string."""
    result = _check(tmp_path, _two_views())
    wrong = {
        (check.overall.id, tuple(p.text for p in check.parts))
        for check in result.checks
        if check.verdict == "does_not_add_up"
    }
    overalls = {check.overall.id for check in result.checks if check.overall.text == '60 1/8"'}
    assert len(overalls) == 2
    assert {parts for _id, parts in wrong} == {('20"', '40"'), ('15"', '45"')}
    assert len({overall for overall, _parts in wrong}) == 2  # each row with its own overall
    # A reading of the lower string, at its overall's spot, is verified by code's own row (main
    # judged the lower overall nowhere and dropped the reading for the upper row's parts).
    lower = next(c for c in result.checks if c.verdict != "doubt" and c.parts[0].text == '15"')
    x0, y0, x1, y1 = lower.overall.box
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1, "file": FILE}
    reading = _ledger_string(['15"', '45"'], '60 1/8"', vendor_box=spot)
    judged = vc.judge_strings([reading], result.checks)
    assert judged.verified == ("F7",) and judged.dropped == {}


# ---- a reading's list that is not the overall's string (labels only) ----------------------------

#: A 30" overall at 2 pt per inch (line 100 to 160) drawn to its value, and code's own row under it
#: printing 1 1/2" + 20" from its left end; the rest of the line (17 pt, 8 1/2") has no label code
#: reads (its text could not be read).
_OVERALL = {**_raw("D1", '30"', "30", (100.0, 160.0), 300.0), "scale_ok": True}
_PARTIAL = (
    _raw("D2", '1 1/2"', "1.5", (100.0, 103.0), 280.0),
    _raw("D3", '20"', "20", (103.0, 143.0), 280.0),
)


def _reading(parts: list[str], **more: Any) -> dict[str, Any]:
    x0, y0, x1, y1 = _OVERALL["box"]
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1, "file": FILE}
    return _ledger_string(parts, '30"', vendor_box=spot, **more)


def test_a_list_holding_the_overall_itself_is_not_compared_never_a_please_check() -> None:
    judged = vc.judge_rows([_reading(['1 1/2"', '20"', '30"'])], _row_result(_OVERALL, *_PARTIAL))
    assert judged.unproved == {} and judged.dropped == {}
    why = judged.not_this["F7"]
    assert why.startswith("Not compared: the reading's parts include the overall itself (30\")")
    # What code's own row shows once the overall is taken out of the list.
    assert '1 1/2" + 20" and leave about 8 1/2" with no label code reads' in why


def test_a_part_longer_than_its_overall_is_not_compared() -> None:
    # The overall alone (no row of code's along it): code cannot see the row, but a part longer
    # than the overall shows these numbers are not one string.
    judged = vc.judge_rows([_reading(['2"', '35"'])], _row_result(_OVERALL))
    assert judged.unproved == {} and judged.dropped == {}
    assert 'part 35" is longer than the 30" overall' in judged.not_this["F7"]


def test_a_list_codes_row_shows_is_short_of_a_part_is_not_compared() -> None:
    """1 1/2" + 20" + 1 1/2" against 30": code's own row prints the first two and leaves 17 pt;
    the reading's other part (3 pt drawn) cannot fill it, so a part was left out."""
    judged = vc.judge_rows(
        [_reading(['1 1/2"', '20"', '1 1/2"'])], _row_result(_OVERALL, *_PARTIAL)
    )
    assert judged.unproved == {}
    why = judged.not_this["F7"]
    assert "the reading's other parts (1 1/2\") cannot fill it" in why
    assert "so the reading left a part out" in why


def test_a_list_whose_other_parts_fill_codes_row_stays_a_please_check() -> None:
    """1 1/2" + 20" + 8 3/8" against 30" (1/8" short): the part code cannot read is drawn about as
    long as the rest of the line, so code cannot tell: still flagged as a please-check."""
    judged = vc.judge_rows(
        [_reading(['1 1/2"', '20"', '8 3/8"'])], _row_result(_OVERALL, *_PARTIAL)
    )
    assert judged.not_this == {}
    assert "please check" in judged.unproved["F7"]


class _Engine(_BaseEngine):
    def __init__(self, live: list[dict[str, Any]]) -> None:
        super().__init__("full_check", [])
        self.book = _LiveBook(live)
        self.dropped: list[tuple[str, str]] = []

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))


def test_the_hook_sends_a_list_that_is_not_the_string_to_not_compared(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Kept, never dropped: Not compared with code's plain reason, never a please-check (its
    please-check mark is taken off), never red; a sibling reading of the same overall that code
    only cannot see stays a please-check beside it."""
    from app.review.assistant import likely_problems

    result = _row_result(_OVERALL, *_PARTIAL)
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    bad = _reading(
        ['1 1/2"', '20"', '30"'],
        created_by="page_reviewer",
        **{likely_problems.FIELD: {"by": "page_reviewer", "kind": "size", "words": "Check it."}},
    )
    plausible = {**_reading(['1 1/2"', '20"', '8 3/8"']), "id": "F8"}
    engine = _Engine([bad, plausible])
    vc.add_to_answer(engine)
    live = {f["id"]: f for f in engine.book.live()}
    assert set(live) == {"F7", "F8"} and engine.dropped == []
    moved = live["F7"]
    assert moved["status"] == "different" and likely_problems.FIELD not in moved
    assert moved["string_reads"]["outcome"] == "gap"
    assert proof.proof_group(moved) == "not_compared"
    assert "the overall itself" in (proof.classified(moved).why or "")
    assert proof.proof_group(live["F8"]) == "likely_problem" and live["F8"]["status"] == "check"


# ---- the words: rounded inch figures are no misread ---------------------------------------------


def test_a_metric_string_whose_millimetres_add_up_is_not_called_a_misread() -> None:
    entry = SimpleNamespace(
        parts=("30 [1 1/8]", "940 [37]", "30 [1 1/8]"), printed_total="1000 [39 3/8]"
    )
    words = ledger._misread_words(entry, [])  # type: ignore[arg-type]
    assert words is not None and "misread" not in words
    assert words.startswith("The millimetres add up (30 + 940 + 30 = 1000 mm)")
    assert "the inch figures, each rounded from its millimetres, do not" in words
    # An inch-only string keeps the misread hint.
    inch = SimpleNamespace(parts=('20"', '13"'), printed_total='38"')
    assert "misread" in (ledger._misread_words(inch, []) or "")  # type: ignore[arg-type]


# ---- the sum is never shown as an architect value ------------------------------------------------


def _string_card(**more: Any) -> dict[str, Any]:
    return {
        **_ledger_string(['20"', '12 5/8"'], '32 1/2"'),
        "inches": {"architect": "32.625", "vendor": "32.5", "difference": "-0.125"},
        **more,
    }


def test_reference_value_names_a_strings_sum_and_leaves_others_alone() -> None:
    assert reference_value(_string_card()) == f'{PARTS_ADD_UP} 32 5/8"'
    assert reference_value({"source": "architect", "architect": '36"'}) == '36"'
    assert reference_value({"source": "vendor_arithmetic", "architect": None}) == ""


def test_the_csv_says_parts_add_up_and_leaves_the_architects_inches_empty() -> None:
    rows = list(csv.reader(io.StringIO(findings_csv([_string_card()]).lstrip("﻿"))))
    head, row = rows[0], rows[1]
    assert row[head.index("Architect (as printed)")] == 'parts add up to 32 5/8"'
    assert row[head.index("Architect (in)")] == ""
    other = {**_string_card(), "source": "architect", "architect": '32 5/8"'}
    rows = list(csv.reader(io.StringIO(findings_csv([other]).lstrip("﻿"))))
    assert rows[1][rows[0].index("Architect (in)")] == "32.625"


def test_the_cloud_notes_and_the_register_say_parts_add_up() -> None:
    open_string = _string_card(status="check")
    assert "architect" not in chat_drawing.note_text(1, open_string)
    assert 'parts add up to 32 5/8"' in chat_drawing.note_text(1, open_string)
    assert chat_drawing.entry_lines(open_string)[0] == 'Vendor: 32 1/2"   Parts add up to: 32 5/8"'
    please = vc.unproved_check(_string_card(), "Code could not confirm these parts; please check.")
    assert proof.proof_group(please) == "likely_problem"
    (row,) = [row for row in register_rows([please], _context()) if row[1]]
    assert row[REGISTER_COLUMNS.index("Reference")] == 'parts add up to 32 5/8"'


def test_a_reading_is_judged_by_codes_string_with_its_own_parts() -> None:
    """One overall that two rows tile (its own row, and another view's row of the same span that
    adds up): the reading of its own row's parts is verified by that row, never dropped as
    "another chain" because the other row comes first (a real sheet, made-up values)."""
    overall = vc.read_label(_raw("D1", '30"', "30", (100.0, 160.0), 300.0), FILE, 1)
    far = [
        vc.read_label(_raw("D2", '20"', "20", (100.0, 140.0), 150.0), FILE, 1),
        vc.read_label(_raw("D3", '10"', "10", (140.0, 160.0), 150.0), FILE, 1),
    ]
    own = [
        vc.read_label(_raw("D4", '20 7/8"', "20.875", (100.0, 141.75), 280.0), FILE, 1),
        vc.read_label(_raw("D5", '9"', "9", (141.75, 160.0), 280.0), FILE, 1),
    ]
    assert overall is not None and None not in far and None not in own
    checks = [
        vc.StringCheck(overall, tuple(far), "adds_up", Fraction(30), ""),  # type: ignore[arg-type]
        vc.StringCheck(overall, tuple(own), "does_not_add_up", Fraction(239, 8), ""),  # type: ignore[arg-type]
    ]
    judged = vc.judge_strings([_reading(['20 7/8"', '9"'])], checks)
    assert judged.verified == ("F7",) and judged.dropped == {}
    # A reading of other parts is judged by the overall's nearest row (its own), not the far one.
    other = vc.judge_strings([_reading(['21"', '8 7/8"'])], checks)
    assert 'as 20 7/8" + 9" (printed 30")' in other.dropped["F7"]


# ---- strings code now proves (Set A, 2026-11-04, second pass) -----------------------------------


def _long_dim(x0: float, x1: float, y: float, text: str, *, tick: float = 3.5, off: float = 0.0):
    """A horizontal dimension (PDF space) whose ticks are long slashes (`tick` each way, about
    10 pt long), moved `off` along the slash from the line's end (0: centred on it)."""
    body = _line(x0, y, x1, y) + _line(x0, y - 90, x0, y + 3) + _line(x1, y - 90, x1, y + 3)
    for x in (x0, x1):
        body += _line(x - tick + off, y - tick + off, x + tick + off, y + tick + off)
    return body + _text((x0 + x1) / 2 - 5, y + 2, text)


def test_a_long_tick_centred_on_the_end_is_an_end_mark(tmp_path: Path) -> None:
    """A sheet drawn at a large scale ticks its ends with slashes about 10 pt long: centred on the
    line's end, each is an end mark, so the string is proved; the same slash moved off the end (a
    piece of the drawing passing near it) proves nothing."""
    (tmp_path / "centred").mkdir()
    (tmp_path / "off").mkdir()
    sheet = (
        _long_dim(300, 420, 300, '60"')
        + _long_dim(300, 340, 280, '20"')
        + _long_dim(340, 420, 280, '41"')
        + _dim(450, 500, 150, '25"')
    )
    centred = _check(tmp_path / "centred", sheet)
    assert centred.strings["does_not_add_up"] == 1 and len(centred.findings) == 1
    off = (
        _long_dim(300, 420, 300, '60"', off=2.5)
        + _long_dim(300, 340, 280, '20"', off=2.5)
        + _long_dim(340, 420, 280, '41"', off=2.5)
        + _dim(450, 500, 150, '25"')
    )
    moved = _check(tmp_path / "off", off)
    assert moved.strings["does_not_add_up"] == 0 and moved.findings == []


def _inside_view(*, toward: bool = True) -> bytes:
    """A 60" overall drawn inside a view (PDF y 300), its extension lines the view's sides running
    down past it; its parts' row 100 pt below (y 200), ticked only at its joint, its extension
    lines drawn up from it toward the overall (`toward`), else down away from it."""

    def part(x0: float, x1: float, text: str, *, start: bool, end: bool) -> bytes:
        y = 200
        up = (y - 3, y + 12) if toward else (y - 90, y + 3)
        body = _line(x0, y, x1, y) + _line(x0, up[0], x0, up[1]) + _line(x1, up[0], x1, up[1])
        if start:
            body += _line(x0 - 1.5, y - 1.5, x0 + 1.5, y + 1.5)
        if end:
            body += _line(x1 - 1.5, y - 1.5, x1 + 1.5, y + 1.5)
        return body + _text((x0 + x1) / 2 - 5, y + 2, text)

    overall = (
        _line(300, 300, 420, 300)
        + _line(300, 230, 300, 360)  # the view's sides, through the overall's line and down
        + _line(420, 230, 420, 360)
        + _line(298.5, 298.5, 301.5, 301.5)
        + _line(418.5, 298.5, 421.5, 301.5)
        + _text(355, 302, '60"')
    )
    return (
        overall
        + part(300, 340, '20"', start=False, end=True)
        + part(340, 420, '41"', start=True, end=False)
        + _dim(450, 500, 150, '25"')
    )


def test_a_row_far_from_its_overall_on_its_own_extension_lines_is_proved(tmp_path: Path) -> None:
    """The overall sits inside the view between the sides it measures, its parts' row below the
    view: the row's outer extension lines continue the overall's (one straight line at each end,
    drawn toward each other), so the bare outer ends are the string's ends. Extension lines that
    run away from the overall prove nothing (a doubt, as before)."""
    (tmp_path / "shared").mkdir()
    (tmp_path / "away").mkdir()
    shared = _check(tmp_path / "shared", _inside_view())
    assert shared.strings["does_not_add_up"] == 1 and len(shared.findings) == 1
    away = _check(tmp_path / "away", _inside_view(toward=False))
    assert away.strings["does_not_add_up"] == 0 and away.findings == []


def test_millimetres_over_a_fraction_of_an_inch_read_cleanly() -> None:
    """`13 [1/2]` (13 mm, half an inch) is one clean value, like `915 [36]`; a lost fraction is not."""
    for text, clean in (
        ("13 [1/2]", True),
        ("915 [36]", True),
        ("18 [3/4]", True),
        ("13 [1/]", False),
    ):
        raw = {**_raw("D1", text, "0.5", (100.0, 101.3), 300.0), "text": text}
        label = vc.read_label(raw, FILE, 1)
        assert label is not None and label.clean is clean, text


def test_other_parts_missing_the_same_overall_are_another_string(tmp_path: Path) -> None:
    """Two views print 60 1/8" over two different rows (20" + 40" and 15" + 45"), both 1/8" short:
    two slips, two findings (one per overall and parts, no longer one per overall)."""
    result = _check(tmp_path, _two_views())
    strings = sorted(tuple(f["parts"]) for f in result.findings)
    assert strings == [('15"', '45"'), ('20"', '40"')]


def test_a_reading_with_no_spot_is_judged_by_codes_string_with_its_very_parts(
    tmp_path: Path,
) -> None:
    """Several labels print the reading's overall and it names none of them: code's string with
    the reading's very parts is its string (verified, with that string's spot and overall label).
    A list matching none of code's strings is left to the row check."""
    result = _check(tmp_path, _two_views())
    reading = _ledger_string(['15"', '45"'], '60 1/8"')
    judged = vc.judge_strings([reading], result.checks)
    assert judged.verified == ("F7",) and judged.dropped == {}
    lower = next(c for c in result.checks if c.verdict != "doubt" and c.parts[0].text == '15"')
    spot = judged.spots["F7"]
    assert spot["label"]["id"] == lower.overall.id and spot["vendor_box"]["page"] == 1
    assert spot["vendor_box"]["y0"] <= lower.overall.box[1]
    other = vc.judge_strings([_ledger_string(['10"', '50"'], '60 1/8"')], result.checks)
    assert other.verified == () and other.dropped == {}


def test_codes_own_string_with_other_parts_is_not_hidden_by_a_reading_of_another(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A reading's string of one row does not hide code's own finding of the other row printing
    the same overall: both are added to the answer."""
    from tests.review.assistant.test_vendor_consistency import _hook

    result = _check(tmp_path, _two_views())
    upper = next(c for c in result.checks if c.verdict != "doubt" and c.parts[0].text == '20"')
    x0, y0, x1, y1 = upper.overall.box
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1, "file": FILE}
    engine, _dropped = _hook(
        monkeypatch, result, _ledger_string(['20"', '40"'], '60 1/8"', vendor_box=spot)
    )
    strings = sorted(
        tuple(f["parts"]) for f in engine.book.live() if f.get("source") == "vendor_arithmetic"
    )
    assert strings == [('15"', '45"'), ('20"', '40"')]
