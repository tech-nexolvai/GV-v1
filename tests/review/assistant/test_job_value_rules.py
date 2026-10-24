"""Not-compared reasons that say what would settle an item, and GV's field cut in the escalation.

Made-up values, $0. Covers:

- `job_value_rules`: an item GV's own rule sets (the countertop's depth, the sink cut-out's size)
  whose job values are not set is "a job value is missing", with the rule, never "different
  reference points"; it stays Not compared (never red, never Right);
- `classification`: an item on a vendor page no architect page is paired with says so ("no sheet"),
  and an item the reading says the architect does not show is "architect gives no information";
- `escalation`: a difference code reads at both spots that is the vendor's overall with GV's field
  cut in it is Right, as when the item is recorded; anything else is left as it was.
"""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from app.review.assistant import classification, job_value_rules, proof
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.field_cut import PageFieldCuts

FILE = "set.pdf"

RULES_MISSING = """
| Setting | In force | Where it comes from | Used by |
|---|---|---|---|
| Cabinet depth (`cabinet_depth`) | not set | missing | CT-DEPTH-001 |
| Countertop overhang (`countertop_overhang`) | not set | missing | CT-DEPTH-001 |
| Sink cut-out clearance (`sink_cutout_clearance`) | 1/4" | the rulebook's default | x |
| Sink inside depth (`sink_interior_depth`) | not set | missing | CT-SINK-CUTOUT-DEPTH-001 |
| Sink inside width (`sink_interior_width`) | not set | missing | CT-SINK-CUTOUT-WIDTH-001 |
"""
RULES_SET = """
| Cabinet depth (`cabinet_depth`) | 23" | this project's value | CT-DEPTH-001 |
| Countertop overhang (`countertop_overhang`) | 1" | this project's value | CT-DEPTH-001 |
"""


def _label(id_: str, side: str, inches: str, at: tuple[float, float]) -> dict[str, Any]:
    x, y = at
    return {
        "id": id_,
        "text": f'{inches}"',
        "inches": inches,
        "box": [x - 8, y - 4, x + 8, y + 4],
        "side": side,
        "orientation": "h",
        "route": "text",
    }


def _box(at: tuple[float, float], page: int = 3) -> dict[str, Any]:
    x, y = at
    return {"page": page, "x0": x - 20, "y0": y - 10, "x1": x + 20, "y1": y + 10, "file": FILE}


def _item(**changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 3,
        "round": 1,
        "item": "Vanity: top depth",
        "category": "countertop",
        "architect": "24 1/4",
        "vendor": '26"',
        "status": "different",
        "source": "architect",
        "trust_level": "to_check",
        "witnesses": [],
        "checks": {"printed_architect": "no_text", "printed_vendor": "no_text"},
        "inches": {"architect": "24.25", "vendor": "26", "difference": "1.75"},
        "vendor_box": _box((300, 300)),
        "arch_box": _box((100, 100)),
    }
    finding.update(changes)
    return finding


def _ctx(vendor: str = "26", architect: str = "24.25", **more: Any) -> EscalationContext:
    labels = [
        _label("D1", "vendor", vendor, (300, 300)),
        _label("D2", "architect", architect, (100, 100)),
    ]
    return EscalationContext(main_file=FILE, labels={(FILE, 3): labels}, **more)


# ---- which rule sets an item ------------------------------------------------------------------


def test_the_rule_is_named_by_the_items_own_words_only() -> None:
    assert job_value_rules.rule_of("Bath vanity: top depth").id == "CT-DEPTH-001"  # type: ignore[union-attr]
    assert job_value_rules.rule_of("Kitchen: countertop depth (right end)").id == "CT-DEPTH-001"  # type: ignore[union-attr]
    assert job_value_rules.rule_of("Vanity: sink cutout width").id == "CT-SINK-CUTOUT-WIDTH-001"  # type: ignore[union-attr]
    assert job_value_rules.rule_of("Vanity: sink cut-out depth").id == "CT-SINK-CUTOUT-DEPTH-001"  # type: ignore[union-attr]
    # Another part's size, or a position, is never the rule's.
    for words in (
        "Vanity: side splash depth",
        "Vanity: top depth incl. backsplash",
        "Vanity: sink cutout width from left",
        "Vanity: sink cut-out centre line",
        "Vanity: cabinet depth",
        "Vanity: top width",
        "Vanity: top thickness",
        "Vanity: sink cutout corner radius",
        "Base cabinet B3: top depth",
        "Kitchen sink run: top depth",
    ):
        assert job_value_rules.rule_of(words) is None, words
    # A top depth is a countertop's; a cut-out may be filed under the sink cabinet.
    assert job_value_rules.rule_of("Desk: top depth", "other") is None
    assert job_value_rules.rule_of("Vanity: sink cutout width", "cabinet") is not None


def test_only_values_rules_md_lists_as_not_set_are_missing() -> None:
    depth = job_value_rules.rule_of("Vanity: top depth")
    width = job_value_rules.rule_of("Vanity: sink cutout width")
    assert depth is not None and width is not None
    assert job_value_rules.missing_values(RULES_MISSING, depth) == [
        "cabinet depth",
        "countertop overhang",
    ]
    assert job_value_rules.missing_values(RULES_MISSING, width) == ["sink inside width"]
    assert job_value_rules.missing_values(RULES_SET, depth) == []
    # Not listed at all (an older rules.md): nothing is known, so nothing is said.
    assert job_value_rules.missing_values("", depth) is None
    assert job_value_rules.mark(_item(), RULES_SET) is None
    assert job_value_rules.mark(_item(), None) is None


# ---- the escalation names the rule; the item stays Not compared -------------------------------


def test_a_top_depth_whose_job_values_are_missing_says_so_never_red_or_right() -> None:
    result = escalate([_item()], _ctx(rules_md=RULES_MISSING))
    finding = {str(f["id"]): f for f in result.changed}["F1"]
    assert finding[job_value_rules.FIELD]["rule"] == "CT-DEPTH-001"
    assert proof.proof_group(finding) == "not_compared"
    found = proof.classified(finding)
    assert found.reason == "missing_setting"
    assert "cabinet depth + overhang" in str(found.why) and "CT-DEPTH-001" in str(found.why)
    assert "countertop overhang" in str(found.settle)
    # With the job values entered, the rule is GV's sink plan check's: nothing changes here.
    kept = escalate([_item()], _ctx(rules_md=RULES_SET))
    finding = {str(f["id"]): f for f in kept.changed}["F1"]
    assert job_value_rules.FIELD not in finding
    assert proof.classified(finding).reason == "different_reference"


def test_a_cut_out_item_names_the_sinks_inside_size() -> None:
    item = _item(item="Vanity: sink cutout depth", vendor='12"', architect="1 1/2")
    item["inches"] = {"architect": "1.5", "vendor": "12", "difference": "10.5"}
    result = escalate([item], _ctx("12", "1.5", rules_md=RULES_MISSING))
    finding = {str(f["id"]): f for f in result.changed}["F1"]
    found = proof.classified(finding)
    assert found.reason == "missing_setting" and "sink inside depth" in str(found.why)
    assert proof.proof_group(finding) == "not_compared"


def test_a_proved_or_right_item_keeps_its_outcome() -> None:
    same = _item(status="same", inches={"architect": "26", "vendor": "26"}, architect='26"')
    result = escalate([same], _ctx("26", "26", rules_md=RULES_MISSING))
    assert all(job_value_rules.FIELD not in f for f in result.changed)
    # A record on an item code proved Wrong never changes it (the reason is only a fallback).
    record = job_value_rules.mark(_item(), RULES_MISSING)
    proved = _item(paired_by="code", witnesses=["printed"], **{job_value_rules.FIELD: record})
    assert classification.classify(proved, proved=True).outcome == "wrong"


# ---- the other honest reasons ------------------------------------------------------------------


def test_an_item_on_a_page_with_no_paired_architect_page_says_so() -> None:
    finding = _item(
        status="check",
        keep_reason="other",
        item="Kitchen: notes and tags",
        vendor="sink model, full splash",
        architect="no agreed architect drawing (p9 or p2?)",
        inches={},
        reference_page={
            "file": None,
            "page": None,
            "pair": [],
            "outside": False,
            "why": "No architect page is paired with vendor page 3.",
        },
    )
    found = classification.classify(finding, proved=False)
    assert found.outcome == "not_compared" and found.reason == "no_sheet"
    assert "no architect page is paired with vendor page 3" in str(found.why)


def test_an_item_the_architect_does_not_show_gives_no_information() -> None:
    finding = _item(
        status="check",
        keep_reason="other",
        item="Counter: stone thickness",
        vendor='3/4"',
        architect="not shown on the architect",
        inches={},
    )
    found = classification.classify(finding, proved=False)
    assert found.reason == "nothing_to_compare"
    # A reading with something on both sides keeps its own reason.
    other = classification.classify(dict(finding, architect="solid surface"), proved=False)
    assert other.reason == "not_proved"


# ---- GV's field cut on a difference code reads -------------------------------------------------


def _top(status: str) -> dict[str, Any]:
    finding = _item(
        item="Vanity: top overall length with field cut",
        vendor='61"',
        architect="60",
        status=status,
        inches={"architect": "60", "vendor": "61", "difference": "1"},
    )
    if status == "check":
        finding["keep_reason"] = "other"
    return finding


def _cuts(overalls: dict[str, int], labels: frozenset[str] = frozenset()) -> Any:
    found = PageFieldCuts(Fraction(1), labels, overalls)
    return lambda file, page: found


def test_an_overall_with_the_field_cut_in_it_is_right_when_code_reads_both() -> None:
    for status in ("check", "different"):
        result = escalate([_top(status)], _ctx("61", "60", field_cuts=_cuts({"D1": 1})))
        finding = {str(f["id"]): f for f in result.changed}["F1"]
        assert finding["status"] == "same", status
        assert finding["field_cut"]["outcome"] == "allowed"
        assert proof.proof_group(finding) == "right"
        assert "field cut" in finding["settled"]["how"]


def test_no_field_cut_on_the_drawing_leaves_the_difference_as_it_was() -> None:
    for cuts in (None, _cuts({}), _cuts({"D9": 1})):
        result = escalate([_top("check")], _ctx("61", "60", field_cuts=cuts))
        finding = {str(f["id"]): f for f in result.changed}["F1"]
        assert finding["status"] == "different" and "field_cut" not in finding
        assert proof.proof_group(finding) == "not_compared"
    # Two field cuts' worth is not one wall end's.
    two = _top("check")
    two["inches"] = {"architect": "59", "vendor": "61", "difference": "2"}
    result = escalate([two], _ctx("61", "59", field_cuts=_cuts({"D1": 1})))
    assert {str(f["id"]): f for f in result.changed}["F1"]["status"] == "different"


def test_the_field_cut_label_itself_is_never_compared_with_the_architect() -> None:
    item = _top("check")
    item["inches"] = {"architect": "2", "vendor": "1", "difference": "1"}
    result = escalate([item], _ctx("1", "2", field_cuts=_cuts({}, frozenset({"D1"}))))
    finding = {str(f["id"]): f for f in result.changed}["F1"]
    assert finding["status"] == "check" and proof.proof_group(finding) != "right"
    assert proof.proof_group(finding) != "clouded"


def test_the_record_goes_when_the_job_values_are_entered_and_never_hides_an_unread_value() -> None:
    record = job_value_rules.mark(_item(), RULES_MISSING)
    stale = _item(**{job_value_rules.FIELD: record})
    result = escalate([stale], _ctx(rules_md=RULES_SET))
    finding = {str(f["id"]): f for f in result.changed}["F1"]
    assert job_value_rules.FIELD not in finding
    # A vendor value nobody could read keeps its own reason: job values would not settle it.
    unread = _item(
        status="check",
        keep_reason="unreadable",
        vendor="partly cut off",
        inches={"architect": "24.25", "vendor": None},
        **{job_value_rules.FIELD: record},
    )
    assert classification.classify(unread, proved=False).reason != "missing_setting"


def test_the_field_cut_reads_only_the_labels_that_print_the_compared_values() -> None:
    # The vendor's label kept on the item prints another value than the one compared.
    item = _top("different")
    item["dims"] = {
        "vendor": {"id": "D1", "page": 3, "file": FILE, "inches": "62", "text": '62"'},
        "architect": {"id": "D2", "page": 3, "file": FILE, "inches": "60", "text": '60"'},
    }
    item["checks"] = {"printed_vendor": "match", "printed_architect": "match"}
    result = escalate([item], _ctx("61", "60", field_cuts=_cuts({"D1": 1})))
    finding = {str(f["id"]): f for f in result.changed}["F1"]
    assert finding["status"] == "different" and "field_cut" not in finding
    # An item the splash step looked at keeps the splash step's outcome.
    splash = _top("different")
    splash["splash_views"] = {"outcome": "not_compared", "why": "no splash size"}
    result = escalate([splash], _ctx("61", "60", field_cuts=_cuts({"D1": 1})))
    assert {str(f["id"]): f for f in result.changed}["F1"]["status"] == "different"


def test_an_unread_architect_value_is_never_called_absent_and_no_agreed_page_is_unpaired() -> None:
    base = _item(
        status="check", keep_reason="other", item="Desk: top tag", vendor="ST-01", inches={}
    )
    for unread in ("no tag legible", "no notes or tags readable", "not legible on the plan"):
        found = classification.classify(dict(base, architect=unread), proved=False)
        assert found.reason == "not_proved", unread
    older = classification.classify(
        dict(base, architect="no agreed architect drawing"), proved=False
    )
    assert older.reason == "no_sheet"
