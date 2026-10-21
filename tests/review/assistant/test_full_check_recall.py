"""The full check's proof step and recall (#1641): what the paid Set 3 run of 2026-10-19 lost.

Scripted models, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- The price: the page helpers and gap closers never spend the protected money of the checks after
  them (the verifier, the devil's advocate, the lead review); a helper's last call fits its share.
- A roster helper gets one turn to record what it read before it wraps up; what it still could not
  record (`UNRECORDED` lines) is kept as an AI-only item, never lost.
- The devil's advocate: its verdict survives code's later re-checks while its labels stay; code's
  geometry is checked before its paid pick; an item the price did not reach says so; a newer round
  that prints the same value is still the vendor's current drawing.
- The vendor's own strings under D1: a blind second reading (another model, never shown the first
  reading) that lists the same parts and overall proves the string; one listing other parts that
  add up drops it; code's own lines veto a reading that left a part out.
- The lead review places items the helpers read but code has no spot for.
- The brief names each drawing with no labels (pen strokes, a picture): it is still compared.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_engine, full_check_roster, proof
from app.review.assistant.chat_engine import RECORD_NOTE, ROSTER_WRAP_UP_NOTE
from app.review.assistant.chat_trust import apply_checks, kept_advocate
from app.review.assistant.full_check_advocate import (
    STRING_SYSTEM,
    Check,
    code_checks,
    compare_strings,
    string_gap,
)
from app.review.assistant.full_check_brief import label_summary
from app.review.assistant.full_check_roster import PROTECTED, SHARES, PricePlan, unrecorded_lines
from tests.review.assistant.chat_fakes import reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS
from tests.review.assistant.test_full_check_fanout import _box, _first_text, _steps
from tests.review.assistant.test_full_check_roster import (
    _clean_ink,
    _findings,
    _label,
    _Roster,
    _roster,
)

OPUS = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"

# Made-up values (no client values): a run of five parts that adds to 38 5/8" printed 38 1/2"; and
# a reading that mixed two runs (a 41 1/2" from the next run in place of 68" and 3/4").
FIVE = ['3"', '11 1/8"', '3/4"', '20 3/4"', '3"']
MIXED = ['8 1/2"', '41 1/2"', '4 1/4"']


# ---- the price ------------------------------------------------------------------------------------


def test_the_protected_phases_keep_their_share_of_the_price() -> None:
    assert sum(SHARES.values()) == pytest.approx(1.0)
    plan = PricePlan.balanced(5 * DOLLARS, 10, 45)
    assert plan.protected() == sum(plan.budgets[name] for name in PROTECTED)
    # About a third of the price is kept for the checks that prove (and find) what the helpers
    # read: the verifier, the devil's advocate with the blind string readings, the lead review.
    assert 0.3 * 5 * DOLLARS <= plan.protected() <= 0.4 * 5 * DOLLARS
    assert plan.caps["lead"] == min(full_check_roster.JOB_CAPS["lead"], plan.budgets["lead"])


class _Spender(_Roster):
    """Page helpers that each spend a lot per call and never stop by themselves."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith("OBJECTIVE"):
            return super().helper(params, brief)
        self._seen("spender", params)
        if params.get("tool_choice", {}).get("type") == "none":
            return reply(text("Stopped."), model=SONNET, usage={"input_tokens": 20_000})
        zoom = {"crops": [{"page": 2, "box": {"x0": 0, "y0": 0, "x1": 50, "y1": 50}}]}
        step = _steps(params)
        return reply(
            tool(f"z{step}", "zoom", zoom),
            stop="tool_use",
            model=SONNET,
            usage={"input_tokens": 60_000},
        )


def test_the_helpers_never_spend_the_protected_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Spender()
    harness = _roster(tmp_path, script)
    seen: list[int] = []
    original = full_check_roster._Run._provers

    def provers(self: Any, plan: Any) -> None:
        seen.append(self.engine.spent)  # what the helpers and gap closers spent, all of it
        original(self, plan)

    monkeypatch.setattr(full_check_roster._Run, "_provers", provers)

    harness.run()

    plan = PricePlan.balanced(5 * DOLLARS, 2, 4)
    assert seen and seen[0] <= 5 * DOLLARS - plan.protected()
    assert script.calls["spender"] > 2  # they did spend: the ceiling stopped them


# ---- a helper records before it wraps up ---------------------------------------------------------


class _Reader(_Roster):
    """A numbers helper that zooms at a high price, then records in its record turn, and writes
    one UNRECORDED line in its report."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith("OBJECTIVE: a full GV review"):
            return super().helper(params, brief)
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        page = pages[0]
        self._seen("reader", params)
        last = params["messages"][-1]["content"]
        said = " ".join(b.get("text", "") for b in last if isinstance(b, Mapping))
        if params.get("tool_choice", {}).get("type") == "none":
            self._seen("final", params)
            return reply(
                text(
                    f"Read more than I recorded.\nUNRECORDED p{page} | Area 1: outlet height | "
                    f'architect 3\'-3" | vendor 48" | different | both from the floor\n'
                    f"CHECKLIST p{page}: architect 1/2; notes 0/0; strings 0/0; not compared: "
                    "ran out of budget"
                ),
                model=SONNET,
            )
        if "Record now" in said:
            self._seen("record", params)
            found = {"page": page, "item": f"Top on page {page}", "category": "countertop",
                     "status": "different", "verified": False, "source": "architect",
                     "vendor_dim": "D1", "architect_dim": "D2"}  # fmt: skip
            return reply(tool(f"r{page}", "report_finding", found), stop="tool_use", model=SONNET)
        zoom = {"crops": [{"page": page, "box": {"x0": 0, "y0": 0, "x1": 50, "y1": 50}}]}
        return reply(
            tool(f"z{_steps(params)}", "zoom", zoom),
            stop="tool_use",
            model=SONNET,
            usage={"input_tokens": 120_000},
        )


def test_a_helper_records_what_it_read_before_it_wraps_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Reader()
    outcome = _roster(tmp_path, script).run()

    # At its cap the helper got one turn with its tools to record (not a tool-less wrap-up) ...
    assert script.calls.get("record")
    record_call = script.params["record"][0]
    assert record_call["tool_choice"]["type"] == "auto"
    # ... then its report, asked for the lines it could still not record.
    final = script.params["final"][0]["messages"][-1]["content"]
    assert any("UNRECORDED p<page>" in b.get("text", "") for b in final)
    findings = _findings(outcome)
    assert any(f["item"].startswith("Top on page") for f in findings)  # recorded in its turn
    kept = [f for f in findings if f["item"] == "Area 1: outlet height"]
    notes = [
        b["reason"]
        for b in outcome.blocks
        if b["type"] == "finding_dropped" and "Area 1: outlet height" in str(b.get("reason"))
    ]
    # The UNRECORDED line is kept: here it has no spot and its values are not printed on its page
    # (code's labels of the sheet), so it is an internal note naming the item and both values,
    # never sent to the vendor (#1650); with a spot or a printed value it stays an item (an AI
    # reading: never red).
    assert not kept and notes, "the UNRECORDED lines are kept as internal notes"
    assert all(note.startswith("Internal note, not for the vendor") for note in notes)
    assert '48"' in notes[0] and "3'-3\"" in notes[0]


def test_unrecorded_lines_are_read_from_a_report() -> None:
    report = (
        'Notes.\nUNRECORDED p4 | Area 2: outlet height | architect 3\'-1" | vendor 46" | '
        "different | floor to outlet on both\n"
        'UNRECORDED p9 | Other page | architect 1" | vendor 2" | check | not mine\n'
        'UNRECORDED p4 | No values | architect  | vendor 3" | check | x\n'
    )
    lines = unrecorded_lines(report, (3, 4, 5))
    assert lines == [
        {
            "page": 4,
            "item": "Area 2: outlet height",
            "architect": "3'-1\"",
            "vendor": '46"',
            "status": "different",
            "why": "floor to outlet on both",
        }
    ]
    assert "UNRECORDED" in ROSTER_WRAP_UP_NOTE and "{why}" in RECORD_NOTE


# ---- the devil's advocate -------------------------------------------------------------------------


def test_the_advocates_verdict_survives_code_rechecks_while_its_labels_stay() -> None:
    dims = {
        "vendor": {"id": "D1", "page": 3, "inches": "46"},
        "architect": {"id": "D2", "page": 3, "inches": "37"},
    }
    critic = {"result": "passed", "answers": {}, "note": "", "model": "devil's advocate"}
    finding = {
        "id": "F1",
        "page": 3,
        "item": "x",
        "architect": '37"',
        "vendor": '46"',
        "status": "different",
        "source": "architect",
        "dims": dims,
        "critic": critic,
        "checks": {"critic": "passed"},
        "pairing": {"vendor": "p3.D1", "architect": "p3.D2"},
    }
    assert kept_advocate(finding) == critic
    checked = apply_checks(finding, {}, None)
    assert checked["critic"] == critic and checked["checks"]["critic"] == "passed"
    # Other labels now: judged again. A verdict the price stopped: tried again.
    moved = {**finding, "dims": {**dims, "architect": {"id": "D7", "page": 3}}}
    assert apply_checks(moved, {}, None)["critic"] is None
    stopped = {**finding, "critic": {**critic, "note": "Not verified within the fixed price."}}
    assert kept_advocate(stopped) is None
    other = {**finding, "critic": {**critic, "model": "critic"}}
    assert kept_advocate(other) is None


def test_a_newer_round_printing_the_same_value_is_still_the_current_drawing() -> None:
    page_map = {
        "pages": [
            {"page": 4, "area": "2. Area 2",
             "halves": [{"role": "vendor", "round": 1, "box": [0, 400, 1000, 800]}]},
            {"page": 5, "area": "2. Area 2",
             "halves": [{"role": "vendor", "round": 2, "box": [0, 0, 1000, 800]}]},
        ]
    }  # fmt: skip
    vendor = {**_label("D1", "vendor", "46", (300, 392), y=500), "page": 4}
    architect = {**_label("D2", "architect", "37", (100, 174)), "page": 4}

    def round_check(newer: str | None) -> Check:
        found = code_checks(vendor, architect, page_map=page_map, vendor_ink=(40, 0.0),
                            newer_same=newer)  # fmt: skip
        return next(c for c in found if c.name == "round")

    assert round_check(None).outcome == "fail"
    passed = round_check("p5.D7")
    assert passed.outcome == "pass" and "p5.D7" in passed.why


def test_the_advocate_names_what_the_price_did_not_reach_on_the_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script, price=5 * DOLLARS)
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(5 * DOLLARS, 2)
    monkeypatch.setattr(full_check_roster._Run, "_phase_room", lambda self, phase, est: False)
    finding, _notes = harness.engine.book.report(
        {"page": 3, "item": "Top on page 3", "category": "countertop", "status": "different",
         "verified": False, "source": "architect", "vendor_dim": "D1", "architect_dim": "D2"},
        "helper:1",
    )  # fmt: skip

    run._advocate(None)

    found = next(f for f in harness.engine.book.live() if f["id"] == finding["id"])
    assert "advocate" not in script.calls
    assert found["critic"]["note"].startswith("Not verified within the fixed price")
    assert str(proof.classified(found).why).startswith("not verified within the fixed price")
    # Said once the escalation, which tries it again with what is left, is done (#1829).
    assert not run.stopped_for_money
    run._note_unverified()
    assert run.stopped_for_money


# ---- the vendor's own strings: two blind readings ---------------------------------------------------


def _chains(*chains: tuple[list[str], str]) -> dict[str, Any]:
    return {
        "chains": [{"parts": p, "overall": o, "direction": "across"} for p, o in chains],
        "note": "",
    }


def test_two_blind_readings_of_the_same_string_prove_it_and_other_parts_drop_it() -> None:
    same = _chains((FIVE, '38 1/2"'), (['10"', '22"'], '32"'))
    outcome, why = compare_strings(FIVE, '38 1/2"', same)
    assert outcome == "proved" and '38 5/8"' in why
    reversed_read = _chains((FIVE[::-1], '38 1/2"'))
    assert compare_strings(FIVE, '38 1/2"', reversed_read)[0] == "proved"
    # The mixed reading: the second reading lists the run's own parts, and they add up.
    mixed = _chains((['8 1/2"', '68"', '3/4"', '4 1/4"'], '81 1/2"'))
    outcome, why = compare_strings(MIXED, '81 1/2"', mixed)
    assert outcome == "adds_up" and "add up" in why
    # Other parts that do not add up either, no string with that overall, a slip of 1/32".
    other = _chains((['8 1/2"', '41"', '4 1/4"'], '81 1/2"'))
    assert compare_strings(MIXED, '81 1/2"', other)[0] == "disagrees"
    assert compare_strings(MIXED, '81 1/2"', _chains())[0] == "not_found"
    near = ['3"', '11 1/8"', '3/4"', '20 21/32"', '3"']
    assert compare_strings(near, '38 1/2"', _chains((near, '38 1/2"')))[0] == "within_rounding"
    two = _chains((FIVE, '38 1/2"'), (['20"', '18"'], '38 1/2"'))
    assert compare_strings(FIVE, '38 1/2"', two)[0] == "disagrees"


def test_codes_own_lines_veto_a_reading_that_left_a_part_out() -> None:
    def part(label_id: str, inches: str, ends: tuple[float, float], y: float) -> dict[str, Any]:
        return {**_label(label_id, "vendor", inches, ends, y=y)}

    overall = part("D9", "62.25", (300.0, 500.0), 733.5)
    row = [
        part("D1", "16.875", (302.5, 356.7), 719.5),
        part("D2", "26.75", (357.0, 443.0), 719.5),
        part("D3", "16.875", (443.3, 497.5), 719.5),
    ]
    parts = ['16 7/8"', '26 3/4"', '16 7/8"']
    gap = string_gap(parts, '62 1/4"', [overall, *row])
    assert gap is not None and "left a part out" in gap
    # Parts that tile the overall end to end: no veto (the vendor's numbers are what is wrong).
    tiled = [
        part("D1", "16.875", (300.0, 356.7), 719.5),
        part("D2", "26.75", (357.0, 443.0), 719.5),
        part("D3", "16.875", (443.3, 500.0), 719.5),
    ]
    assert string_gap(parts, '62 1/4"', [overall, *tiled]) is None
    # No lines code could find (pen strokes): no veto, the readings decide.
    assert string_gap(parts, '62 1/4"', []) is None


class _Strings(_Roster):
    """The blind string reader's one answer."""

    def __init__(self, answer: dict[str, Any]) -> None:
        super().__init__()
        self.answer = answer

    def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if params["system"][0]["text"] == STRING_SYSTEM:
            self._seen("string", params)
            return reply(text(json.dumps(self.answer)), model=params["model"])
        return super().__call__(params)


def _overall(harness: Any, total: str, *, whole: bool = True, extra: str | None = None) -> None:
    """The string's printed overall as code's label on page 3 (its whole line found, or cut
    short), and another vendor dimension along its run when `extra` is given."""
    from app.review.assistant.finding_checks import to_inches

    folder = harness.engine.tools.workspace.set_pack
    dims = json.loads((folder / "dims.json").read_text())
    page = next(e for e in dims["pages"] if e["page"] == 3)
    page["labels"].append(
        {"id": "D9", "text": total, "inches": str(to_inches(total)), "side": "vendor",
         "orientation": "h", "route": "text", "box": [312, 120, 330, 128],
         "dim_line": [[300, 170], [360, 170]], "scale_ok": whole}
    )  # fmt: skip
    if extra is not None:
        page["labels"].append(
            {"id": "D10", "text": extra, "inches": str(to_inches(extra)), "side": "vendor",
             "orientation": "h", "route": "drawn", "box": [352, 150, 358, 157],
             "dim_line": [[350, 158], [360, 158]]}
        )  # fmt: skip
    (folder / "dims.json").write_text(json.dumps(dims))


def _string(harness: Any, parts: list[str], total: str, *, overall: bool = True) -> str:
    from app.review.assistant.finding_checks import to_inches

    if overall:
        _overall(harness, total)
    summed = sum((to_inches(p) or Decimal(0) for p in parts), Decimal(0))
    added = harness.engine.book.add_code_finding(
        {
            "page": 3,
            "item": "Dimension string (elevation)",
            "category": "countertop",
            "architect": f'{summed}"',
            "vendor": total,
            "status": "different",
            "source": "vendor_arithmetic",
            "parts": parts,
            "printed_total": total,
            "verified": False,
            "vendor_box": _box(3, 300),
            "why": "parts do not add up",
        }
    )
    assert added is not None
    harness.engine._show_finding(added)
    return str(added["id"])


def _strings_run(harness: Any) -> Any:
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(5 * DOLLARS, 2)
    return run


def test_a_string_two_blind_readings_agree_on_is_red(tmp_path: Path) -> None:
    script = _Strings(_chains((FIVE, '38 1/2"')))
    harness = _roster(tmp_path, script)
    finding_id = _string(harness, FIVE, '38 1/2"')
    before = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert proof.proof_group(before) == "not_compared"  # an AI-grouped string, no geometry

    _strings_run(harness)._strings()

    asked = script.params["string"][0]
    assert asked["model"] == OPUS  # another model than the helpers'
    seen = json.dumps(asked["messages"])
    assert "11 1/8" not in seen and "38 5/8" not in seen  # blind to the first reading
    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert found["string_geometry"] == "two_reads"
    assert proof.proof_group(found) == "clouded"
    assert "a second reading, blind to the first" in proof.proof_line(found)


def test_a_string_the_blind_reading_reads_as_another_run_is_dropped(tmp_path: Path) -> None:
    script = _Strings(_chains((['8 1/2"', '68"', '3/4"', '4 1/4"'], '81 1/2"')))
    harness = _roster(tmp_path, script)
    finding_id = _string(harness, MIXED, '81 1/2"')

    _strings_run(harness)._strings()

    assert finding_id not in {f["id"] for f in harness.engine.book.live()}
    dropped = [b for b in harness.engine.blocks if b.get("type") == "finding_dropped"]
    assert any(b["finding_id"] == finding_id and "add up" in b["reason"] for b in dropped)


def test_a_string_reading_that_disagrees_stays_not_compared_and_code_only_asks_nothing(
    tmp_path: Path,
) -> None:
    script = _Strings(_chains((['3"', '11"', '3/4"', '20 1/2"', '3"'], '38 1/2"')))
    harness = _roster(tmp_path, script)
    finding_id = _string(harness, FIVE, '38 1/2"')
    _strings_run(harness)._strings()
    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert found.get("string_geometry") is None
    assert found["string_reads"]["outcome"] == "disagrees"
    assert proof.proof_group(found) == "not_compared"

    script = _Strings(_chains((FIVE, '38 1/2"')))
    harness = _roster(tmp_path / "code-only", script, mode="code_only")
    finding_id = _string(harness, FIVE, '38 1/2"')
    _strings_run(harness)._strings()
    assert "string" not in script.calls  # D1's AI path is off under code_only
    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert proof.proof_group(found) == "not_compared"


# ---- the lead review -------------------------------------------------------------------------------


class _Lead(_Roster):
    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if not brief.startswith("LEAD REVIEW"):
            return super().helper(params, brief)
        self._seen("lead", params)
        if _steps(params) == 0:
            item = brief.split("ITEMS TO PLACE:", 1)[1]
            finding_id = item.split("- ", 1)[1].split(" ", 1)[0]
            place = {"finding_id": finding_id, "vendor_dim": "D1", "architect_dim": "D2"}
            return reply(tool("u1", "update_finding", place), stop="tool_use", model=OPUS)
        return reply(text("Placed."), model=OPUS)


def test_the_lead_review_places_what_the_helpers_read_but_did_not_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Lead()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    finding, _notes = engine.book.report(
        {"page": 3, "item": "Area 1: outlet height", "category": "countertop",
         "architect": "3'-1\"", "vendor": '46"', "status": "different", "verified": False,
         "source": "architect"},
        "helper:1",
    )  # fmt: skip
    run = _strings_run(harness)

    run._lead_review(None)

    lead = script.params["lead"][0]
    assert lead["model"] == OPUS
    assert finding["id"] in _first_text(lead)
    placed = next(f for f in engine.book.live() if f["id"] == finding["id"])
    assert placed["dims"]["vendor"]["id"] == "D1" and placed["dims"]["architect"]["id"] == "D2"
    assert placed["checks"]["printed_vendor"] == "match"


# ---- the brief: drawings with no labels are still compared -------------------------------------


def test_the_brief_names_each_drawing_code_found_no_labels_on() -> None:
    page_map = {
        "pages": [
            {"page": 28, "halves": [
                {"role": "architect", "heading": "ARCH SET ELEVATION", "box": [200, 40, 990, 390]},
                {"role": "brand_prototype", "heading": "BRAND", "box": [400, 410, 800, 820]},
            ]},
            {"page": 4, "halves": [
                {"role": "architect", "box": [240, 50, 890, 330]},
                {"role": "vendor", "round": 1, "box": [210, 410, 1030, 790]},
            ]},
        ]
    }  # fmt: skip
    labels = {
        28: [{"side": "brand", "box": [700, 446, 730, 454]}],
        4: [{"side": "architect", "box": [740, 180, 750, 200]}],
    }
    lines = label_summary([4, 28], labels, page_map)
    assert "NO labels on the vendor's drawing (round 1)" in lines[0]
    assert "NO labels on the architect's drawing 'ARCH SET ELEVATION'" in lines[1]
    assert "still to compare" in lines[1]
    # A drawing with labels gets no note.
    assert "architect's drawing" not in lines[0]


# ---- #1652: what the paid run of 2026-10-19 showed ----------------------------------------------


def test_a_helpers_note_is_never_red_and_never_sent_even_with_printed_values(
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from app.review.assistant.classification import internal

    harness = _roster(tmp_path, _Roster())
    run = _strings_run(harness)
    # Both values are printed on page 3 (the fixture's labels: 46" vendor, 37" architect).
    report = (
        'UNRECORDED p3 | Area 1: filler widths | architect 37" | vendor 46" | different | '
        "both ends on the walls\n"
        "UNRECORDED p3 | Area 1: strings | architect none printed | vendor as listed | check | x"
    )
    run._salvage(SimpleNamespace(pages=(2, 3), number=7), {"report": report})

    notes = [f for f in harness.engine.book.live() if f.get("salvaged")]
    assert len(notes) == 1  # "as listed" is no value read: no note
    note = notes[0]
    assert note["status"] == "check" and internal(note)
    assert proof.proof_group(note) == "not_compared"
    assert proof.classified(note).label == "Internal: read but not recorded at its spot"
    assert not proof.vendor_facing(note)
    # Whatever later gives it a rule witness or a click, it is never clouded.
    ruled = {**note, "status": "different", "witnesses": ["rule"], "trust_level": "likely",
             "settled": {"method": "gv_limits", "how": "GV's filler limits are 1\" to 2\" (x"},
             "decision": {"decision": "agree"}}  # fmt: skip
    assert proof.proof_group(ruled) == "not_compared" and not proof.code_proved(ruled)


def test_an_unsearched_one_sided_ledger_item_is_for_gvs_reviewer_only() -> None:
    from app.review.assistant.classification import internal

    one_sided = {"id": "F9", "page": 27, "status": "check", "created_by": "ledger",
                 "source": "completeness", "architect": "shown", "vendor": "not found",
                 "item": "Area 4: floor to outlet"}  # fmt: skip
    assert internal(one_sided) and not proof.vendor_facing(one_sided)
    tags = {**one_sided, "created_by": "architect_items", "source": "architect"}
    assert not internal(tags)  # code searched every vendor sheet for a tag: it may be sent


def test_the_protected_share_scales_with_the_price() -> None:
    for price in (5 * DOLLARS, 6 * DOLLARS):
        plan = PricePlan.balanced(price, 10, 45)
        assert plan.protected() == pytest.approx(0.30 * price, rel=0.01)


def test_each_kind_of_specialist_is_timed_in_its_own_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Lead()
    harness = _roster(tmp_path, script)
    harness.engine.book.report(
        {"page": 3, "item": "Area 1: outlet height", "category": "countertop",
         "architect": "3'-1\"", "vendor": '46"', "status": "different", "verified": False,
         "source": "architect"},
        "helper:1",
    )  # fmt: skip
    run = _strings_run(harness)

    run._lead_review(None)

    phases = harness.engine.timings.as_dict(harness.engine.spent)["phases"]
    names = {p["phase"]: p for p in phases}
    assert "lead_review" in names and names["lead_review"]["calls"] >= 1
    assert "helpers" not in names  # the lead review is never counted as a page helper
    lead = script.params["lead"][0]
    assert lead["output_config"].get("effort", "low") == "low"


class _Thinker(_Lead):
    """A lead that first answers with nothing but thinking, then places the item. The empty reply
    is never sent back (harness audit 2026-10-19: the API refuses an empty message in a history):
    the note that sends the lead on joins its brief's message."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        sent_on = chat_engine._CUT_NOTE in json.dumps(params["messages"])
        if brief.startswith("LEAD REVIEW") and not sent_on:
            self._seen("empty", params)
            return reply(text(""), model=OPUS)
        if brief.startswith("LEAD REVIEW"):
            assert all(m["content"] for m in params["messages"]), "an empty message was sent back"
        return super().helper(params, brief)


def test_a_reply_with_no_words_and_no_tools_is_sent_on_not_taken_as_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Thinker()
    harness = _roster(tmp_path, script)
    finding, _notes = harness.engine.book.report(
        {"page": 3, "item": "Area 1: outlet height", "category": "countertop",
         "architect": "3'-1\"", "vendor": '46"', "status": "different", "verified": False,
         "source": "architect"},
        "helper:1",
    )  # fmt: skip

    _strings_run(harness)._lead_review(None)

    assert script.calls.get("empty") == 1 and script.calls.get("lead", 0) >= 1
    placed = next(f for f in harness.engine.book.live() if f["id"] == finding["id"])
    assert placed["dims"]["vendor"]["id"] == "D1"


def test_each_gap_closer_gets_enough_money_to_make_its_calls() -> None:
    assert full_check_roster.MIN_GAP_RUN_MICROS >= 200_000
    budget = 790_000
    closers = max(1, budget // full_check_roster.MIN_GAP_RUN_MICROS)
    chosen, _left = full_check_roster.gap_pages({p: ["x"] for p in range(3, 30)}, budget)
    runs = full_check_roster.page_runs(chosen, closers)
    assert len(runs) == 3 and all(budget * len(r) // len(chosen) >= 200_000 for r in runs)


# ---- #1654: what the paid run of 2026-10-19 (test 6) showed ---------------------------------


def test_a_page_marked_with_a_not_reached_note_is_unfinished_whatever_its_status() -> None:
    coverage = {
        "pages": [
            {"page": 25, "status": "different", "by": "helper:4",
             "note": "not reached within the fixed price"},
            {"page": 31, "status": "not_opened", "by": "helper:5", "note": "partly compared"},
            {"page": 36, "status": "different", "by": "helper:6",
             "note": "only overview read; leads not individually recorded"},
            {"page": 23, "status": "different", "by": "helper:27", "note": None},
            {"page": 26, "status": "not_comparable", "by": "code", "note": "divider"},
        ]
    }  # fmt: skip
    left = full_check_roster.unfinished_pages([23, 25, 31, 36, 40], coverage)
    assert set(left) == {25, 31, 36, 40}


def test_the_lead_review_takes_what_is_left_and_the_open_pages_biggest_first(
    tmp_path: Path,
) -> None:
    harness = _roster(tmp_path, _Roster(), price=6 * DOLLARS)
    run = _strings_run(harness)
    run.plan = PricePlan.balanced(6 * DOLLARS, 2, 4)
    # Nothing spent yet: the lead takes all but what the steps after it need, never less than its
    # own share, and as many pages as that pays for.
    money = run._lead_money()
    assert money > run.plan.caps["lead"]
    assert money <= 6 * DOLLARS - run.plan.budgets["advocate"] - run.plan.budgets["summary"]
    engine = harness.engine
    engine.book.report(
        {"page": 5, "item": "Area 2: outlet height", "category": "countertop",
         "architect": "6'-0\"", "vendor": '73 1/2"', "status": "check", "verified": False,
         "source": "architect",
         "decision_help": {"measures": "x", "estimate": None, "likely": "can't tell",
                           "confirm": "y"}},
        "helper:1",
    )  # fmt: skip
    pages, unplaced = run._lead_pages(money)
    assert 5 in pages and unplaced and unplaced[0]["page"] == 5
    assert len(run._lead_pages(100_000)[0]) == 1  # one page's money: the biggest item's page


def test_the_record_turn_is_never_refused_for_the_helpers_share(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_engine import ChatEngine

    _clean_ink(monkeypatch)
    script = _Reader()
    harness = _roster(tmp_path, script)
    # The helpers' shared ceiling is reached: no call is held for them any more.
    monkeypatch.setattr(ChatEngine, "_hold", lambda self, share, estimate: False)

    harness.run()

    assert script.calls.get("record"), "the helper still got its turn to record"
    record_call = script.params["record"][0]
    assert (
        record_call["tool_choice"]["type"] == "auto"
        and record_call["max_tokens"] <= chat_engine.RECORD_MAX_TOKENS
    )


def test_dpi_and_marks_given_on_a_crop_are_the_calls(tmp_path: Path) -> None:
    from app.review.assistant.chat_tools import new_step

    harness = _roster(tmp_path, _Roster())
    raw = {"crops": [{"page": 2, "box": {"x0": 0, "y0": 0, "x1": 80, "y1": 80}, "dpi": 150,
                      "marks": True}], "description": "x"}  # fmt: skip
    result = harness.engine.tools.zoom(raw, new_step("z1", "zoom", raw), "helper:1")
    assert result.step["ok"], result.step.get("output")


def test_an_absence_claim_with_no_spot_on_the_architect_is_internal() -> None:
    from app.review.assistant.classification import internal

    claim = {"id": "F26", "page": 53, "status": "check", "created_by": "helper:9",
             "source": "architect", "architect": "5'-4\", 13'-11\"",
             "vendor": "not shown on the vendor", "arch_box": None, "dims": None,
             "item": "Area 9: locating dimensions"}  # fmt: skip
    assert internal(claim) and not proof.vendor_facing(claim)
    spotted = {**claim, "arch_box": _box(53, 100)}
    assert not internal(spotted)  # the architect's side is at its spot: it may be listed


# ---- #1656: what the paid run of 2026-10-19 (test 7) showed -----------------------------------


def test_a_string_whose_overall_line_is_cut_short_gets_no_reading_and_no_red(
    tmp_path: Path,
) -> None:
    """The p40 shape: the overall's line was cut where a leader crosses it, so the top two parts
    "tiled" it, and both readings saw only that cut view (9 1/2 + 74 of 89 1/2)."""
    script = _Strings(_chains((['9 1/2"', '74"'], '89 1/2"')))
    harness = _roster(tmp_path, script)
    _overall(harness, '89 1/2"', whole=False)
    finding_id = _string(harness, ['9 1/2"', '74"'], '89 1/2"', overall=False)

    _strings_run(harness)._strings()

    assert "string" not in script.calls  # no reading of a cut view
    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert found["string_reads"]["outcome"] == "gap"
    assert "whole dimension line" in found["string_reads"]["why"]
    assert proof.proof_group(found) == "not_compared"


def test_another_dimension_along_the_overalls_run_vetoes_the_reading(tmp_path: Path) -> None:
    script = _Strings(_chains((['9 1/2"', '74"'], '89 1/2"')))
    harness = _roster(tmp_path, script)
    _overall(harness, '89 1/2"', extra='5 1/4"')
    finding_id = _string(harness, ['9 1/2"', '74"'], '89 1/2"', overall=False)

    _strings_run(harness)._strings()

    assert "string" not in script.calls
    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert found["string_reads"]["outcome"] == "gap" and '5 1/4"' in found["string_reads"]["why"]
    assert proof.proof_group(found) == "not_compared"


def test_the_blind_reading_sees_the_overalls_whole_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_tools import ChatTools

    script = _Strings(_chains((FIVE, '38 1/2"')))
    harness = _roster(tmp_path, script)
    finding = _string(harness, FIVE, '38 1/2"')
    harness.engine.book.replace(
        {**next(f for f in harness.engine.book.live() if f["id"] == finding),
         "vendor_box": {"page": 3, "x0": 300, "y0": 100, "x1": 320, "y1": 116}}
    )  # fmt: skip

    asked: list[Any] = []
    zoom = ChatTools.zoom

    def seen(self: Any, raw: Any, step: Any, by: str = "lead") -> Any:
        asked.append(raw)
        return zoom(self, raw, step, by)

    monkeypatch.setattr(ChatTools, "zoom", seen)
    _strings_run(harness)._strings()

    assert script.calls.get("string") == 1
    box = asked[-1]["crops"][0]["box"]
    # The string's own box ends at x 320 and y 116; the overall's whole line runs to x 360, y 170.
    assert box["x1"] >= 360 and box["y1"] >= 170


def test_a_right_that_codes_own_check_contradicts_is_never_right() -> None:
    same = {"id": "F29", "page": 34, "status": "same", "source": "vendor_arithmetic",
            "created_by": "helper:5", "architect": '72"', "vendor": "30+18+18",
            "parts": ["30", "18", "18"], "printed_total": "72", "item": "Area 8: overall",
            "checks": {"arithmetic": "does_not_add_up"}}  # fmt: skip
    assert proof.proof_group(same) == "not_compared"
    assert "does not add up" in str(proof.classified(same).why)
    agreeing = {**same, "checks": {"arithmetic": "adds_up"}, "compared": True}
    assert proof.proof_group(agreeing) == "right"
