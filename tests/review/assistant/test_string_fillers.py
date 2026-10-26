"""Vendor strings with filler notes at their ends, and the architect guard (#1802).

A paid roster full check clouded two false reds from the vendor's own sum (R5): its readings and
code's labels left out the small filler notes printed at the two ends of a cabinet run (a note
such as `1/2" FILLER`, not a dimension), so a right overall was told it "should be" the sum of the
middle parts, and a wrong overall got a "should be" that was neither the vendor's nor the
architect's value. Code's veto never ran either: it grouped the parts' lines into 2 pt bands, and
a row drawn 1/2 pt lower in one place fell into two bands.

- `string_chain`: rows of parts grouped by distance, so the veto sees a part left out; end gaps a
  filler note fills exactly are counted in the sum (plain and stacked-fraction notes);
- the roster's string step: a string that adds up with its fillers is dropped; one that does not
  is red only when the architect prints that very sum over the same parts; else Not compared,
  "the string could not be proved complete"; a filler note by the run that the parts leave out
  (no lines code can follow) stops the blind reading;
- the global guard (`classification.architect_conflict`): a vendor value equal to the
  architect's is never red (when the architect prints the overall, the wrong value is a part),
  and a "should be" the architect's printed value contradicts is never proposed;
- a vendor-only sum on a page with no architect drawing paired with it gets no blind reading
  and is never red (Not compared, no correction proposed);
- the architect's own run has the same "gap" (its fillers are notes too): code takes the overall
  the architect prints, and a comparison against the architect's summed parts is never red.

Made-up values and geometry only (no client values): a run of 20" + 15" + 20" between two 1/2"
fillers, overall 56", drawn at 4 pt per inch. No network, no paid call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import classification, full_check_roster, proof
from app.review.assistant.full_check_advocate import (
    INCOMPLETE,
    architect_overalls,
    filler_notes_left_out,
    filler_size,
    same_chain,
    string_chain,
    string_gap,
)
from app.review.assistant.full_check_roster import PricePlan
from tests.review.assistant.test_chat_engine import DOLLARS
from tests.review.assistant.test_full_check_recall import _chains, _string, _Strings
from tests.review.assistant.test_full_check_roster import _roster

FILE = "Vendor set.pdf"
SCALE = 4.0  # points per inch
START = 280.0
BOX = {"page": 3, "x0": 270.0, "y0": 85.0, "x1": 520.0, "y1": 140.0, "file": FILE}


def _dim(
    label_id: str, side: str, text: str, inches: str, x0: float, x1: float, y: float
) -> dict[str, Any]:
    return {
        "id": label_id, "side": side, "text": text, "inches": inches, "orientation": "h",
        "route": "text", "box": [(x0 + x1) / 2 - 8, y - 9, (x0 + x1) / 2 + 8, y - 2],
        "dim_line": [[x0, y], [x1, y]], "scale_ok": True,
    }  # fmt: skip


def _note(label_id: str, side: str, text: str, inches: str, x: float) -> dict[str, Any]:
    """A filler note: printed with its size, no dimension line of its own (a leader at most)."""
    return {
        "id": label_id, "side": side, "text": text, "inches": inches, "orientation": "h",
        "route": "text", "box": [x - 12, 150, x + 12, 158], "dim_line": None, "scale_ok": None,
    }  # fmt: skip


def _run(
    side: str,
    overall: str,
    middle: str = "15",
    *,
    note: str = '1/2" FILLER',
    lines: bool = True,
    notes: bool = True,
) -> list[dict[str, Any]]:
    """The run as code's labels: the overall's line wall to wall (56" drawn), the three parts
    between the 1/2" fillers (the middle one printed `middle`, drawn 15"), and the two notes. The
    middle part's line is drawn 1/2 pt lower than the others (the paid run's shape)."""
    a, b = START + 0.5 * SCALE, START + 20.5 * SCALE
    c, d = b + 15 * SCALE, b + 35 * SCALE
    labels = [_dim("D1", side, f'{overall}"', overall, START, START + 56 * SCALE, 100)]
    if lines:
        labels += [
            _dim("D2", side, '20"', "20", a, b, 121.0),
            _dim("D3", side, f'{middle}"', middle, b, c, 121.5),
            _dim("D4", side, '20"', "20", c, d, 121.0),
        ]
    if notes:
        labels += [
            _note("D5", side, note, "0.5", START - 20),
            _note("D6", side, note, "0.5", START + 56 * SCALE + 20),
        ]
    return labels


def _parts(middle: str = "15") -> list[str]:
    return ['20"', f'{middle}"', '20"']


# ---- code's lines -----------------------------------------------------------------------------


def test_a_row_drawn_half_a_point_lower_is_still_one_row_so_a_left_out_part_is_seen() -> None:
    """Fail-first: the old 2 pt bands split this row, so the veto never ran (no notes here)."""
    labels = _run("vendor", "56", notes=False)
    gap = string_gap(_parts(), '56"', labels)
    assert gap is not None and "left a part out" in gap
    assert string_chain(_parts(), '56"', labels).outcome == "gap"


def test_filler_notes_that_fill_both_end_gaps_complete_the_string() -> None:
    chain = string_chain(_parts(), '56"', _run("vendor", "56"))
    assert chain.outcome == "fillers"
    assert chain.fillers == (Decimal("0.5"), Decimal("0.5"))
    # A stacked fraction read as two numbers (`1 2" FILLER` for 1/2") counts the same.
    stacked = string_chain(_parts(), '56"', _run("vendor", "56", note='1 2" FILLER'))
    assert stacked.outcome == "fillers" and stacked.fillers == (Decimal("0.5"), Decimal("0.5"))
    # A note of another size does not fill the gap: a part is missing.
    wide = _run("vendor", "56", note='2" FILLER')
    wide = [dict(lab, inches="2") if lab["id"] in ("D5", "D6") else lab for lab in wide]
    assert string_chain(_parts(), '56"', wide).outcome == "gap"


def test_filler_sizes_are_read_from_the_notes_words() -> None:
    assert filler_size({"text": '3/4" FILLER', "inches": "0.75"}) == Decimal("0.75")
    assert filler_size({"text": '3 4" FILLER', "inches": "34"}) == Decimal("0.75")
    assert filler_size({"text": '1 1/2" FILLER (TYP.)', "inches": "1.5"}) == Decimal("1.5")
    assert filler_size({"text": '3/4" CABINET SIDE', "inches": "0.75"}) is None
    assert filler_size({"text": '20"', "inches": "20"}) is None


def test_a_filler_note_the_parts_leave_out_is_named_when_code_finds_no_row() -> None:
    labels = _run("vendor", "56", lines=False)
    left = filler_notes_left_out(_parts(), labels[0], labels)
    assert left == ['1/2" FILLER', '1/2" FILLER']
    # A reading that counts the fillers at its ends leaves nothing out.
    assert filler_notes_left_out(['1/2"', *_parts(), '1/2"'], labels[0], labels) == []


def test_the_architects_run_of_the_same_parts_gives_its_printed_overall() -> None:
    architect = [_run("architect", "56")]
    assert architect_overalls(_parts(), architect) == {Decimal(56)}
    assert architect_overalls(_parts("16"), architect) == {Decimal(56)}  # one part differs
    assert architect_overalls(_parts("16"), architect, slips=0) == set()
    assert architect_overalls(['10"', '45"'], architect) == set()
    assert same_chain([Decimal(1), Decimal(2), Decimal(3)], [Decimal(3), Decimal(2), Decimal(1)])
    assert not same_chain([Decimal(1)], [Decimal(1)])


# ---- the roster's string step -----------------------------------------------------------------


def _pack(harness: Any, *, vendor: list[dict[str, Any]], architect: bool = True) -> None:
    folder = harness.engine.tools.workspace.set_pack
    dims = json.loads((folder / "dims.json").read_text())
    for entry in dims["pages"]:
        if entry["page"] == 3:
            entry["labels"] = vendor
        if entry["page"] == 2 and architect:
            entry["labels"] = _run("architect", "56")
    (folder / "dims.json").write_text(json.dumps(dims))


def _ledger_string(harness: Any, parts: list[str], total: str) -> str:
    """A string a reading grouped (the reading ledger's), parts without the filler notes."""
    from app.review.assistant.finding_checks import to_inches

    summed = sum((to_inches(p) or Decimal(0) for p in parts), Decimal(0))
    added = harness.engine.book.add_code_finding(
        {
            "page": 3, "item": "Dimension string (plan)", "category": "cabinet",
            "architect": f'{summed}"', "vendor": total, "status": "different",
            "source": "vendor_arithmetic", "parts": parts, "printed_total": total,
            "verified": False, "vendor_box": dict(BOX), "why": "parts do not add up",
        }
    )  # fmt: skip
    assert added is not None
    harness.engine._show_finding(added)
    return str(added["id"])


def _strings(harness: Any) -> full_check_roster._Run:
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(5 * DOLLARS, 2)
    run._strings()
    run._architect_guard()
    return run


def _live(harness: Any, finding_id: str) -> dict[str, Any] | None:
    return next((f for f in harness.engine.book.live() if f["id"] == finding_id), None)


def test_a_right_overall_with_filler_notes_at_its_ends_is_never_red(tmp_path: Path) -> None:
    """The paid run's first false red: 20 + 15 + 20 = 55 under a right 56" overall; both readings
    leave the 1/2" filler notes out. With them the string adds up: dropped, no reading paid."""
    script = _Strings(_chains((_parts(), '56"')))
    harness = _roster(tmp_path, script)
    _pack(harness, vendor=_run("vendor", "56"))
    finding_id = _ledger_string(harness, _parts(), '56"')

    _strings(harness)

    assert "string" not in script.calls
    assert _live(harness, finding_id) is None
    dropped = [b for b in harness.engine.blocks if b.get("type") == "finding_dropped"]
    assert any(b["finding_id"] == finding_id and "filler" in b["reason"] for b in dropped)


@pytest.mark.usefixtures("read_twice")
def test_a_wrong_overall_is_red_only_with_the_architects_own_value(tmp_path: Path) -> None:
    """The paid run's second: the overall printed 57" over a run whose parts and fillers make
    56", the architect printing 56" over the same parts: red, and "should be" 56" (never 55")."""
    script = _Strings(_chains((_parts(), '57"')))
    harness = _roster(tmp_path, script)
    _pack(harness, vendor=_run("vendor", "57"))
    finding_id = _ledger_string(harness, _parts(), '57"')

    _strings(harness)

    assert "string" not in script.calls
    found = _live(harness, finding_id)
    assert found is not None
    assert proof.proof_group(found) == "clouded"
    assert proof.should_be_value(found) == '56"'
    assert found["architect_printed"] == ["56"]
    assert '1/2"' in found["parts"][0] and '1/2"' in found["parts"][-1]

    # Without the architect's run nothing says which number is wrong: Not compared.
    script = _Strings(_chains((_parts(), '57"')))
    harness = _roster(tmp_path / "no-architect", script)
    _pack(harness, vendor=_run("vendor", "57"), architect=False)
    finding_id = _ledger_string(harness, _parts(), '57"')
    _strings(harness)
    found = _live(harness, finding_id)
    assert found is not None and proof.proof_group(found) == "not_compared"
    assert INCOMPLETE in found["string_reads"]["why"]
    assert INCOMPLETE in proof.classified(found).why


@pytest.mark.usefixtures("read_twice")
def test_code_only_still_runs_codes_own_string_steps(tmp_path: Path) -> None:
    """`code_only` turns off only the AI's second reading: code's own lines still drop a string
    that adds up with its filler notes, and still make the red the architect's printed sum
    proves (the setting's early return once skipped both)."""
    script = _Strings(_chains((_parts(), '56"')))
    harness = _roster(tmp_path / "right", script, mode="code_only")
    _pack(harness, vendor=_run("vendor", "56"))
    finding_id = _ledger_string(harness, _parts(), '56"')
    _strings(harness)
    assert "string" not in script.calls
    assert _live(harness, finding_id) is None

    script = _Strings(_chains((_parts(), '57"')))
    harness = _roster(tmp_path / "wrong", script, mode="code_only")
    _pack(harness, vendor=_run("vendor", "57"))
    finding_id = _ledger_string(harness, _parts(), '57"')
    _strings(harness)
    assert "string" not in script.calls
    found = _live(harness, finding_id)
    assert found is not None and proof.proof_group(found) == "clouded"
    assert proof.should_be_value(found) == '56"'


def test_code_only_leaves_a_string_codes_lines_cannot_settle_not_compared(
    tmp_path: Path,
) -> None:
    """No filler notes, no gap code can see: the string would get the AI's second reading; under
    `code_only` none is asked and it stays Not compared, saying why."""
    script = _Strings(_chains((_parts(), '56"')))
    harness = _roster(tmp_path, script, mode="code_only")
    _pack(harness, vendor=_run("vendor", "56", lines=False, notes=False))
    finding_id = _ledger_string(harness, _parts(), '56"')
    _strings(harness)
    assert "string" not in script.calls
    found = _live(harness, finding_id)
    assert found is not None and proof.proof_group(found) == "not_compared"
    assert found["string_reads"]["why"] == full_check_roster.CODE_ONLY_STRING
    assert found.get("string_geometry") is None


def test_a_wrong_part_never_makes_the_right_overall_red(tmp_path: Path) -> None:
    """The middle part printed 15 1/2" (drawn 15"), the overall right at 56" as the architect
    prints it: the sum is off, but the overall is never red, nor given another "should be"."""
    script = _Strings(_chains((_parts("15.5"), '56"')))
    harness = _roster(tmp_path, script)
    _pack(harness, vendor=_run("vendor", "56", middle="15.5"))
    finding_id = _ledger_string(harness, ['20"', '15 1/2"', '20"'], '56"')

    _strings(harness)

    found = _live(harness, finding_id)
    assert found is not None and proof.proof_group(found) == "not_compared"
    assert "string" not in script.calls


def test_a_filler_note_by_the_run_stops_the_blind_reading_when_code_finds_no_row(
    tmp_path: Path,
) -> None:
    script = _Strings(_chains((_parts(), '56"')))
    harness = _roster(tmp_path, script)
    _pack(harness, vendor=_run("vendor", "56", lines=False))
    finding_id = _ledger_string(harness, _parts(), '56"')

    _strings(harness)

    assert "string" not in script.calls  # two readings that leave notes out prove nothing
    found = _live(harness, finding_id)
    assert found is not None and found["string_reads"]["outcome"] == "gap"
    assert "FILLER" in found["string_reads"]["why"]
    assert proof.proof_group(found) == "not_compared"


@pytest.mark.usefixtures("read_twice")
def test_the_guard_stamps_a_code_string_whose_overall_the_architect_prints(
    tmp_path: Path,
) -> None:
    """A string code's own geometry verified (the consistency check) is still never red at an
    overall the architect prints the same (#1802: every red path)."""
    harness = _roster(tmp_path, _Strings(_chains()))
    _pack(harness, vendor=_run("vendor", "56", middle="15.5"))
    added = harness.engine.book.add_code_finding(
        {
            "page": 3, "item": "Dimension string (plan)", "category": "cabinet",
            "architect": '55 1/2"', "vendor": '56"', "status": "different",
            "source": "vendor_arithmetic", "parts": ['20"', '15 1/2"', '20"'],
            "printed_total": '56"', "verified": False, "vendor_box": dict(BOX),
            "string_geometry": "verified", "why": "parts do not add up",
        },
        created_by="consistency",
    )  # fmt: skip
    assert added is not None
    before = _live(harness, str(added["id"]))
    assert before is not None and proof.proof_group(before) == "clouded"  # the old red

    _strings(harness)

    found = _live(harness, str(added["id"]))
    assert found is not None and found["architect_printed"] == ["56"]
    assert proof.proof_group(found) == "not_compared"


# ---- the global guard -------------------------------------------------------------------------


def _architect_wrong(vendor: str, architect: str) -> dict[str, Any]:
    return {
        "page": 3, "item": "Cabinet width", "status": "different", "source": "architect",
        "vendor": f'{vendor}"', "architect": f'{architect}"', "trust_level": "confirmed",
        "witnesses": ["printed"], "paired_by": "code",
        "inches": {"vendor": vendor, "architect": architect},
        "vendor_box": {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
        "arch_box": {"page": 2, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
    }  # fmt: skip


@pytest.mark.usefixtures("read_twice")
def test_a_vendor_value_equal_to_the_architects_is_never_red() -> None:
    assert proof.proof_group(_architect_wrong("21", "18")) == "clouded"
    same = _architect_wrong("18", "18")
    assert classification.architect_conflict(same) is not None
    assert proof.proof_group(same) == "not_compared"
    assert "architect's own value" in proof.classified(same).why


def _string_wrong(printed: str, summed: str, architect: list[str] | None) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "page": 3, "item": "Dimension string (plan)", "status": "different",
        "source": "vendor_arithmetic", "created_by": "consistency", "string_geometry": "verified",
        "vendor": f'{printed}"', "printed_total": f'{printed}"', "architect": f'{summed}"',
        "parts_sum": summed, "trust_level": "confirmed", "witnesses": ["arithmetic"],
        "inches": {"vendor": printed, "architect": summed},
        "vendor_box": {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
    }  # fmt: skip
    if architect is not None:
        finding["architect_printed"] = architect
    return finding


@pytest.mark.usefixtures("read_twice")
def test_a_should_be_the_architects_printed_value_contradicts_is_never_proposed() -> None:
    assert proof.proof_group(_string_wrong("57", "56", None)) == "clouded"  # no architect run
    assert proof.proof_group(_string_wrong("57", "56", ["56"])) == "clouded"  # they agree
    # The vendor prints the architect's own overall: the wrong number is a part.
    assert proof.proof_group(_string_wrong("56", "55", ["56"])) == "not_compared"
    # The sum is neither: the "should be" would contradict the architect.
    assert proof.proof_group(_string_wrong("58", "55", ["56"])) == "not_compared"
    # Two architect runs of the same parts with other overalls: which one is not known.
    assert proof.proof_group(_string_wrong("57", "56", ["56", "60"])) == "not_compared"


# ---- pages with no architect pair, and the architect's own side ---------------------------------


def _unpair_page_3(harness: Any) -> None:
    """Page 3 as a vendor page with no architect drawing on it, in no typed area, not paired."""
    folder = harness.engine.tools.workspace.set_pack
    page_map = json.loads((folder / "page_map.json").read_text())
    for entry in page_map["pages"]:
        if entry["page"] == 3:
            entry.pop("area", None)
            entry.pop("section", None)
            entry.pop("pair_page", None)
            entry["halves"] = [h for h in entry.get("halves") or [] if h.get("role") == "vendor"]
    (folder / "page_map.json").write_text(json.dumps(page_map))


def test_a_vendor_only_sum_on_a_page_with_no_architect_pair_is_never_red(tmp_path: Path) -> None:
    """Two readings of a sum are not a second check on a page no architect drawing is paired
    with: no reading is paid, Not compared, and no "should be" is given."""
    five = ['3"', '11 1/8"', '3/4"', '20 3/4"', '3"']
    script = _Strings(_chains((five, '38 1/2"')))
    harness = _roster(tmp_path, script)
    _unpair_page_3(harness)
    finding_id = _string(harness, five, '38 1/2"')  # the overall's label on page 3, as before

    _strings(harness)

    assert "string" not in script.calls
    found = _live(harness, finding_id)
    assert found is not None and found.get("string_geometry") is None
    assert "no architect drawing is paired" in found["string_reads"]["why"]
    assert proof.proof_group(found) == "not_compared"
    assert "no correction is proposed" in proof.classified(found).why


def test_the_architects_own_run_with_filler_notes_is_safe_too() -> None:
    """The architect's run has the same "gap" (its fillers are notes too): code takes the
    overall the architect PRINTS, never the sum of its parts, and a comparison against the
    architect's summed parts is never red."""
    architect = _run("architect", "56")
    assert string_chain(_parts(), '56"', architect).outcome == "unknown"  # vendor labels only
    assert architect_overalls(_parts(), [architect]) == {Decimal(56)}  # printed, not 55"
    summed = {
        **_architect_wrong("56", "55"),
        "architect_parts": {"total": '55"', "vendor": '56"'},
    }
    assert proof.proof_group(summed) == "not_compared"
