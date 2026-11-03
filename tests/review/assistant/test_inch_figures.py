"""A vendor string whose millimetres add up but whose inch figures do not stays red, and its words
point at the inch figures (option A, 2026-11-03).

Settled with GV: inches count, millimetres are ignored, exact match, flag everything; GV's own
reviewer corrected exactly such a string on a keyed sheet. So nothing here adds or removes a red:
only the words change, on the card, the letter, the register, the cloud drawing and the summary.
Three made-up string shapes from real metric drawings:

- brackets rounded to 1/4" (36 [1 1/2] + 334 [13 1/4] + 36 [1 1/2] = 406 mm, inches 16 1/4 vs
  [16]): no figure to change can be derived from the millimetres, so none is named;
- brackets rounded to 1/8", one figure derivable (175 + 300 + 102 = 577 mm, inches 22 5/8 vs
  [22 3/4]): only 11 3/4 to 11 7/8 makes the inches add up and stays a rounding of its 300 mm,
  named as a suggestion ("e.g."), never as proof;
- brackets rounded to 1/8" with two end parts alike (300 + 1500 + 300 = 2100 mm, inches 82 1/2 vs
  [82 5/8]): several figures would do, so none is named.

An inch-only string, inches-first labels, a real 1/2" slip and millimetres off by 25 mm keep
today's words. Pure code, $0.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest
from pypdf import PdfReader

from app.review.assistant import chat_summary, inch_figures, proof, proof_words
from app.review.assistant import vendor_consistency as vc
from app.review.assistant.chat_contract import ChatFindingOut
from app.review.assistant.full_check_advocate import string_chain
from app.review.assistant.inch_figures import PRINTED_STRING, figures
from app.review.assistant.send_package import letter_of, register_rows, requires_words
from tests.review.assistant.test_proof_drawing import VENDOR_30, _box, _draw, _proved, _words
from tests.review.assistant.test_proof_sentence import _context
from tests.review.assistant.test_vendor_consistency import (
    FILE,
    _Engine,
    _FullBook,
    _ledger_string,
    _raw,
)

#: Brackets rounded to 1/4" (made-up values).
QUARTER = (["36 [1 1/2]", "334 [13 1/4]", "36 [1 1/2]"], "406 [16]")
#: Brackets rounded to 1/8", one figure the millimetres name.
EIGHTH = (["175 [6 7/8]", "300 [11 3/4]", "102 [4]"], "577 [22 3/4]")
#: Brackets rounded to 1/8", end parts alike: no one figure.
ALIKE = (["300 [11 3/4]", "1500 [59]", "300 [11 3/4]"], "2100 [82 5/8]")


# ---- the string -----------------------------------------------------------------------------------


def test_quarter_inch_brackets_say_the_millimetres_add_up_and_name_no_figure() -> None:
    found = figures(*QUARTER)
    assert found is not None and found.exact and found.suggestion is None
    assert found.sentence() == (
        "The millimetres add up (36 + 334 + 36 = 406 mm), but the inch figures do not "
        "(1 1/2 + 13 1/4 + 1 1/2 = 16 1/4 vs [16]). Correct the inch figures so they add up to "
        "the printed [16]."
    )
    assert found.should_words() == "inch figures adding up to [16]"


def test_a_figure_the_millimetres_alone_derive_is_named_as_a_suggestion() -> None:
    found = figures(*EIGHTH)
    assert found is not None
    assert found.suggestion == (Fraction(47, 4), Fraction(95, 8))
    assert found.sentence() == (
        "The millimetres add up (175 + 300 + 102 = 577 mm), but the inch figures do not "
        "(6 7/8 + 11 3/4 + 4 = 22 5/8 vs [22 3/4]). Correct the inch figures so they add up to "
        "the printed [22 3/4] (e.g. 11 3/4 to 11 7/8)."
    )
    card = proof.should_be(_metric(parts=EIGHTH[0], printed_total=EIGHTH[1], vendor=EIGHTH[1]))
    assert card == (
        "inch figures adding up to [22 3/4] (the millimetres add up: 175 + 300 + 102 = 577 mm; "
        "e.g. 11 3/4 to 11 7/8)"
    )


def test_several_figures_that_would_do_name_none() -> None:
    """GV's reviewer chose one such figure on a keyed sheet; the millimetres alone cannot say
    which, so code names none."""
    found = figures(*ALIKE)
    assert found is not None and found.suggestion is None
    assert found.inch_line() == "11 3/4 + 59 + 11 3/4 = 82 1/2 vs [82 5/8]"
    assert "e.g." not in found.sentence()


def test_millimetres_within_their_own_rounding_on_a_metric_drawing_say_so() -> None:
    """1 mm off over three parts: each printed millimetre may be half a millimetre off its true
    size (and the drawing works in millimetres: its brackets are not the millimetres' source)."""
    found = figures(["36 [1 1/2]", "333 [13 1/4]", "36 [1 1/2]"], "406 [16]")
    assert found is not None and not found.exact
    assert found.mm_words() == (
        "The millimetres add up within their own rounding (36 + 333 + 36 = 405 mm, printed 406 mm)"
    )
    assert found.mm_note().endswith("(adds up within rounding)")


@pytest.mark.parametrize(
    ("parts", "total"),
    [
        (['1 1/2"', '13 1/4"', '1 1/2"'], '16"'),  # inch only
        (['1 1/2" [36]', '13 1/4" [337]', '1 1/2" [38]'], '16" [406]'),  # inches first
        (["36 [1 1/2]", "347 [13 3/4]", "36 [1 1/2]"], "406 [16]"),  # a real 1/2" slip
        (["36 [1 1/2]", "334 [13 1/4]", "36 [1 1/2]"], "431 [17]"),  # millimetres 25 mm off
        (["36 [1 1/2]", "334 [13]", "36 [1 1/2]"], "406 [16]"),  # the inch figures add up
        # 3 mm off and the millimetres only the inches converted: not the vendor's own numbers
        (["38 [1 1/2]", "337 [13 1/4]", "38 [1 1/2]"], "416 [16 3/8]"),
    ],
)
def test_other_strings_keep_todays_words(parts: list[str], total: str) -> None:
    assert figures(parts, total) is None


# ---- a red finding's words --------------------------------------------------------------------------


def _metric(**changes: Any) -> dict[str, Any]:
    """A red vendor string code read (made-up values), printed millimetres first."""
    parts, total = QUARTER
    given: dict[str, Any] = {
        "id": "F5",
        "item": "Upper cabinet height string",
        "source": "vendor_arithmetic",
        "created_by": "consistency",
        "architect": '16 1/4"',
        "vendor": total,
        "parts": parts,
        "printed_total": total,
        "parts_sum": "16.25",
        "witnesses": ["arithmetic"],
        "inches": {"architect": None, "vendor": "16"},
        "arch_box": None,
        "critic": None,
        "vendor_box": _box(VENDOR_30),
    }
    return _proved(**{**given, **changes})


def _typed(**changes: Any) -> dict[str, Any]:
    """The same string as a page reviewer types it (the inches only), with code's own labels of
    the row kept on it (`PRINTED_STRING`)."""
    return _metric(
        created_by="page_reviewer",
        vendor='16"',
        parts=['1 1/2"', '13 1/4"', '1 1/2"'],
        printed_total='16"',
        **{PRINTED_STRING: {"parts": QUARTER[0], "total": QUARTER[1]}},
        **changes,
    )


def _inch_only() -> dict[str, Any]:
    return _metric(vendor='16"', parts=['1 1/2"', '13 1/4"', '1 1/2"'], printed_total='16"')


SENTENCE = (
    "The millimetres add up (36 + 334 + 36 = 406 mm), but the inch figures do not "
    "(1 1/2 + 13 1/4 + 1 1/2 = 16 1/4 vs [16]). Correct the inch figures so they add up to the "
    "printed [16]."
)


@pytest.mark.parametrize("make", [_metric, _typed])
def test_every_file_points_at_the_inch_figures(make: Any) -> None:
    finding = make()
    assert proof_words.sentence(finding) == SENTENCE
    roles = {role: words for words, role in proof_words.sentence_runs(finding) if role != "plain"}
    assert roles == {"vendor": "16 1/4", "should": "[16]"}
    assert proof_words.should_text(finding) == "inch figures adding up to [16]"
    assert proof_words.sum_line(finding) == (
        "36 + 334 + 36 = 406 mm (adds up); 1 1/2 + 13 1/4 + 1 1/2 = 16 1/4, not the [16] printed"
    )
    assert proof_words.reference_words(finding) == "The vendor's own millimetres (they add up)"
    assert proof.should_be(finding) == (
        "inch figures adding up to [16] (the millimetres add up: 36 + 334 + 36 = 406 mm)"
    )
    assert "The millimetres add up (36 + 334 + 36 = 406 mm) but the inch figures add up to " in (
        proof.proof_line(finding)
    )
    assert requires_words(finding) == (
        "The vendor's millimetres add up (36 + 334 + 36 = 406 mm); its inch figures must add up "
        "to the printed [16]"
    )


def test_an_inch_only_string_keeps_todays_words() -> None:
    finding = _inch_only()
    assert inch_figures.of_finding(finding) is None
    assert proof_words.sentence(finding) == (
        'Vendor prints 16" for the upper cabinet height string, but its own parts add up to '
        '16 1/4" (1/4" more than the total). Correct the total to 16 1/4", or the parts so they '
        'add up to 16".'
    )
    assert proof.should_be(finding) == "16 1/4\" (sum of the vendor's own parts)"


def test_no_red_is_added_or_removed() -> None:
    """Each string is in the same group with the new words as without them: only words change.
    Code's own string is red either way."""
    without_mm = _inch_only()
    assert proof.proof_group(_metric()) == proof.proof_group(without_mm) == "clouded"
    typed = _typed()
    plain = {k: v for k, v in typed.items() if k != PRINTED_STRING}
    assert proof.proof_group(typed) == proof.proof_group(plain)
    assert proof.proof_of(typed)["group"] == proof.proof_of(plain)["group"]


def test_a_part_the_drawing_names_keeps_its_own_words() -> None:
    named = _metric(wrong_part={"text": "334 [13 1/4]", "should_be": "13"})
    assert inch_figures.of_finding(named) is None


def test_the_letter_and_the_register_say_the_same() -> None:
    finding = _metric()
    letter = letter_of([finding], _context(), drawing_set="sheet.pdf")
    (item,) = letter.corrections
    assert item.words == SENTENCE
    assert item.requires.startswith("The vendor's millimetres add up")
    assert item.sum_line == proof_words.sum_line(finding)
    (row,) = [row for row in register_rows([finding], _context()) if row[0]]
    assert row[7] == f"{SENTENCE} Sum: {proof_words.sum_line(finding)}."
    assert row[9] == "inch figures adding up to [16]"


def test_the_cloud_drawing_points_at_the_inch_figures(tmp_path: Path) -> None:
    out = _draw(tmp_path, [_metric(comment_no="C-05")])
    reader = PdfReader(str(out))
    sheet = _words(reader.pages[1].extract_text())
    assert "inch figures 16 1/4 should add up to [16]" in sheet
    card = _words(reader.pages[2].extract_text())
    for words in (
        SENTENCE,
        "Millimetres: 36 + 334 + 36 = 406 mm (adds up)",
        "Inch figures must add up to [16]",
    ):
        assert words in card, words


def test_the_summary_says_the_millimetres_add_up() -> None:
    (line,) = chat_summary._issue_words([_metric()])
    assert "the millimetres add up, the inch figures do not" in line
    assert "16 1/4 vs [16]" in line


def test_the_saved_answer_keeps_codes_printed_row() -> None:
    kept = ChatFindingOut.model_validate({**_typed(), "verified": False})
    assert kept.printed_string is not None
    assert kept.printed_string.total == "406 [16]"


# ---- code keeps its own labels of the row on a reading's string -----------------------------------

#: The QUARTER string drawn at 2 pt per inch from its millimetres: 406 mm over 36 + 334 + 36 mm.
_PT_PER_MM = 2 / 25.4
_ENDS = (100.0, 100.0 + 36 * _PT_PER_MM, 100.0 + 370 * _PT_PER_MM, 100.0 + 406 * _PT_PER_MM)
_OVERALL = _raw("D1", "406 [16]", "16", (_ENDS[0], _ENDS[3]), 300.0)
_ROW = (
    _raw("D2", "36 [1 1/2]", "1.5", (_ENDS[0], _ENDS[1]), 280.0),
    _raw("D3", "334 [13 1/4]", "13.25", (_ENDS[1], _ENDS[2]), 280.0),
    _raw("D4", "36 [1 1/2]", "1.5", (_ENDS[2], _ENDS[3]), 280.0),
)


def _reading() -> dict[str, Any]:
    x0, y0, x1, y1 = _OVERALL["box"]
    spot = {"x0": x0, "y0": y0, "x1": x1, "y1": y1, "page": 1, "file": FILE}
    return _ledger_string(['1 1/2"', '13 1/4"', '1 1/2"'], '16"', vendor_box=spot)


def test_the_row_code_matches_gives_its_labels_as_printed() -> None:
    chain = string_chain(['1 1/2"', '13 1/4"', '1 1/2"'], '16"', [_OVERALL, *_ROW])
    assert chain.outcome == "tiles"
    assert chain.printed == tuple(QUARTER[0]) and chain.overall_printed == QUARTER[1]
    judged = vc.judge_rows(
        [_reading()], vc.ConsistencyResult(page_labels={(FILE, 1): [_OVERALL, *_ROW]})
    )
    assert judged.complete == ("F7",)
    assert judged.printed["F7"] == {"parts": QUARTER[0], "total": QUARTER[1]}


def _hooked(monkeypatch: pytest.MonkeyPatch, typed: dict[str, Any]) -> dict[str, Any]:
    result = vc.ConsistencyResult(page_labels={(FILE, 1): [_OVERALL, *_ROW]})
    monkeypatch.setattr(vc, "check_pack", lambda folder, uploads, **_: result)
    engine = _Engine("full_check", [])
    engine.book = _FullBook([typed])
    engine._show_dropped = lambda finding_id, reason: None  # type: ignore[attr-defined]
    vc.add_to_answer(engine)
    (kept,) = engine.book.live()
    return kept


def test_the_hook_keeps_codes_row_on_the_readings_string(monkeypatch: pytest.MonkeyPatch) -> None:
    typed = _reading()
    typed["checks"] = {"arithmetic": "does_not_add_up", "parts_read": "ai", "critic": "passed"}
    kept = _hooked(monkeypatch, typed)
    assert kept["parts_read"] == "code"
    assert kept[PRINTED_STRING] == {"parts": QUARTER[0], "total": QUARTER[1]}
    assert proof_words.should_text(kept) == "inch figures adding up to [16]"


def test_a_string_codes_own_string_verifies_keeps_codes_printed_row() -> None:
    """A reading's string that is code's own string of that overall (same parts): verified, with
    code's labels of the row as printed."""
    overall = vc.read_label(_OVERALL, FILE, 1)
    parts = tuple(vc.read_label(raw, FILE, 1) for raw in _ROW)
    assert overall is not None and None not in parts
    check = vc.StringCheck(overall, parts, "does_not_add_up", Fraction(65, 4), "")  # type: ignore[arg-type]
    judged = vc.judge_strings([_reading()], [check])
    assert judged.verified == ("F7",)
    assert judged.printed["F7"] == {"parts": QUARTER[0], "total": QUARTER[1]}
