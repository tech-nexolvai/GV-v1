"""Fewer decisions (#1554): code settles the "Your decision" items the drawings can answer.

Made-up findings (no client values), fake witnesses and fake reads: no network, no paid call.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_contract, chat_drawing, decision_resolver
from app.review.assistant.decision_resolver import (
    ResolverContext,
    SharperRead,
    keep_reason,
    question_for,
    resolve,
    settings_in_force,
)

RULES_MD = """## Settings in force

| Setting | Value | Where it comes from | Used by |
|---|---|---|---|
| Field cut per wall end (`field_cut`) | 1" | the rulebook's default | CT-WIDTH-001 |
| Filler width, maximum (`filler_max`) | 2" | the rulebook's default | CAB-FILLER-001 |
| Filler width, minimum (`filler_min`) | 1" | the rulebook's default | CAB-FILLER-001 |
| Countertop overhang (`countertop_overhang`) | not set | missing | CT-DEPTH-001 |
"""
SETTINGS = settings_in_force(RULES_MD)
AREAS = {2: "kitchen", 3: "kitchen", 4: "kitchen", 6: "lobby", 7: "lobby", 8: "lobby"}
BOX_A = {"page": 2, "x0": 10.0, "y0": 10.0, "x1": 40.0, "y1": 20.0, "file": "set.pdf"}
BOX_V = {"page": 2, "x0": 100.0, "y0": 100.0, "x1": 140.0, "y1": 110.0, "file": "set.pdf"}


def finding(fid: str, **changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": fid,
        "page": 2,
        "vs_page": None,
        "item": "Counter overall width",
        "category": "countertop",
        "architect": "no width printed",
        "vendor": '80"',
        "status": "check",
        "why": None,
        "source": "architect",
        "round": 1,
        "record_id": None,
        "parts": None,
        "printed_total": None,
        "arch_box": None,
        "vendor_box": None,
        "inches": {"architect": None, "vendor": None, "difference": None},
        "checks": {
            "printed_architect": "not_a_number",
            "printed_vendor": "not_a_number",
            "second_read": "not_run",
            "lead_checked": True,
        },
    }
    base.update(changes)
    return base


def by_id(result: decision_resolver.Resolution) -> dict[str, dict[str, Any]]:
    return {str(f["id"]): f for f in result.changed}


def entry_of(result: decision_resolver.Resolution, fid: str) -> dict[str, Any]:
    return next(e for e in result.block["settled"] if e["finding_id"] == fid)


# ---- the settings and the same item ---------------------------------------------------------------


def test_the_settings_in_force_are_read_from_rules_md() -> None:
    assert SETTINGS == {"field_cut": '1"', "filler_max": '2"', "filler_min": '1"'}
    assert settings_in_force(None) == {}


def test_the_same_item_on_two_rounds_becomes_one_item_listing_both_pages() -> None:
    first = finding("F1", page=2, round=1)
    second = finding("F2", page=3, round=2, vs_page=2)
    result = resolve([first, second], ResolverContext(areas=AREAS))
    assert set(result.absorbed) == {"F1"}
    assert result.absorbed["F1"].startswith("Settled by GV Review: the same item as F2")
    carried = by_id(result)["F2"]
    assert [a["finding_id"] for a in carried["also_on"]] == ["F1"]
    assert carried["status"] == "check" and carried["question"].endswith("?")
    assert entry_of(result, "F1")["outcome"] == "merged" and entry_of(result, "F1")["into"] == "F2"
    assert result.block["before"]["decision"] == 2 and result.block["after"]["decision"] == 1


def test_a_newer_round_that_matches_carries_the_item_as_fixed() -> None:
    old = finding("F1", item="Overall height", architect="7'-6\"", vendor='84"', round=1)
    new = finding(
        "F2", item="Overall height", architect="7'-6\"", vendor='90"', status="same", page=3
    )
    result = resolve([old, new], ResolverContext(areas=AREAS))
    assert "F1" in result.absorbed
    assert "round 2" not in result.absorbed["F1"] or "fixed" in result.absorbed["F1"]
    new_round = finding(
        "F2",
        item="Overall height",
        architect="7'-6\"",
        vendor='90"',
        status="same",
        page=3,
        round=2,
    )
    result = resolve([old, new_round], ResolverContext(areas=AREAS))
    assert "the vendor fixed it" in result.absorbed["F1"]
    assert by_id(result)["F2"]["also_on"][0]["finding_id"] == "F1"


def test_different_areas_and_recorded_differences_are_never_folded() -> None:
    here = finding("F1", page=2)
    there = finding("F2", page=7)
    recorded = finding("F3", page=3, record_id="c1")
    result = resolve([here, there, recorded], ResolverContext(areas=AREAS))
    assert "F2" not in result.absorbed and "F3" not in result.absorbed


def test_an_architect_item_never_joins_a_prototype_item_unless_the_architect_prints_nothing() -> (
    None
):
    architect = finding("F1", item="Overall height", architect="7'-11\" (95)", vendor='77 1/2"')
    prototype = finding(
        "F2",
        item="Overall height",
        architect="prototype 77 1/2",
        vendor='77 1/2"',
        status="same",
        source="brand_prototype",
        page=4,
    )
    result = resolve([architect, prototype], ResolverContext(areas=AREAS))
    assert "F1" not in result.absorbed
    nothing = finding("F3", item="Desk overall width", architect="no width printed", vendor='120"')
    compared = finding(
        "F4",
        item="Desk overall width",
        architect="prototype 10'-1\"",
        vendor='120"',
        status="different",
        source="brand_prototype",
        page=4,
    )
    result = resolve([nothing, compared], ResolverContext(areas=AREAS))
    assert "F3" in result.absorbed and entry_of(result, "F3")["into"] == "F4"


def test_the_same_value_of_the_same_kind_is_the_same_item() -> None:
    width = finding("F1", item="Vanity width", vendor="610 [24]", architect="no width printed")
    overall = finding(
        "F2",
        item="Vanity overall width",
        architect="4'-0\"",
        vendor="EQ in 610 [24] wall to wall",
        status="different",
    )
    height = finding("F3", item="Vanity height", vendor='24"', architect="no height printed")
    result = resolve([width, overall, height], ResolverContext(areas=AREAS))
    assert entry_of(result, "F1")["into"] == "F2"
    assert "F3" not in result.absorbed  # a height never joins a width


# ---- code's rules ---------------------------------------------------------------------------------


def test_fillers_outside_gvs_limits_are_wrong_and_inside_are_right() -> None:
    wide = finding("F1", item="End filler width", vendor='3"+2" filler each end (round 1: 2")')
    fine = finding("F2", item="End filler width", vendor='1 1/2" filler each end', page=7)
    result = resolve([wide, fine], ResolverContext(areas=AREAS, settings=SETTINGS))
    assert by_id(result)["F1"]["status"] == "different"
    assert by_id(result)["F1"]["settled"]["method"] == "gv_limits"
    assert '3"' in by_id(result)["F1"]["settled"]["how"] and '1" to 2"' in (
        by_id(result)["F1"]["settled"]["how"]
    )
    assert by_id(result)["F2"]["status"] == "same"


def test_a_run_width_that_mentions_its_fillers_is_not_a_filler_item() -> None:
    run = finding("F1", item="Buffet run width", vendor='150" = 3"+2" filler + 140" + 3"+2" filler')
    result = resolve([run], ResolverContext(areas=AREAS, settings=SETTINGS))
    assert by_id(result)["F1"]["status"] == "check"


def test_without_the_limits_a_filler_stays_a_decision() -> None:
    wide = finding("F1", item="End filler width", vendor='5" filler each end')
    result = resolve([wide], ResolverContext(areas=AREAS))
    assert by_id(result)["F1"]["status"] == "check"


def test_a_vendor_string_off_by_a_slip_is_wrong_and_a_big_gap_stays_a_question() -> None:
    slip = finding(
        "F1",
        source="vendor_arithmetic",
        item="Front height string",
        parts=["4", "10 1/8", "66"],
        printed_total="80",
        vendor="80",
    )
    gap = finding(
        "F2",
        source="vendor_arithmetic",
        item="Module widths",
        parts=['15 1/4"', '20 3/4"', '14 1/2"'],
        printed_total='54 1/4"',
        vendor="see parts",
        page=7,
    )
    result = resolve([slip, gap], ResolverContext(areas=AREAS))
    assert by_id(result)["F1"]["status"] == "different"
    assert '80 1/8"' in by_id(result)["F1"]["settled"]["how"]
    kept = by_id(result)["F2"]
    assert kept["status"] == "check" and kept["keep_reason"] == "vendor_disagrees"
    assert '50 1/2"' in kept["question"]


def _witness(printed: Mapping[int, set[str]]) -> Any:
    """A fake `verify_value`: `match` for the values printed on each page."""

    def verify(box: Mapping[str, Any], value: str) -> str:
        return "match" if value in printed.get(int(box["page"]), set()) else "cannot_read"

    return verify


def test_printed_against_printed_is_wrong_or_right_by_code() -> None:
    differs = finding(
        "F1",
        item="Outlet height",
        architect="5'-0\"",
        vendor='61 1/2" (tiny print)',
        arch_box=dict(BOX_A, page=2),
        vendor_box=dict(BOX_V, page=3),
    )
    same = finding(
        "F2",
        item="Counter height",
        architect="2'-4\"",
        vendor='28"',
        arch_box=dict(BOX_A, page=6),
        vendor_box=dict(BOX_V, page=7),
        page=7,
    )
    ctx = ResolverContext(
        areas=AREAS, verify=_witness({2: {"5'-0\""}, 3: {'61 1/2"'}, 6: {"2'-4\""}, 7: {'28"'}})
    )
    result = resolve([differs, same], ctx)
    assert by_id(result)["F1"]["status"] == "different"
    assert by_id(result)["F1"]["settled"]["how"].endswith('(1 1/2" apart).')
    assert by_id(result)["F2"]["status"] == "same"


def test_printed_values_that_measure_something_else_or_are_scaled_stay() -> None:
    printed = _witness({2: {"2'-8\"", '27 1/2"'}})
    other = finding(
        "F1",
        item="Cabinet height",
        architect="2'-8\"",
        vendor='27 1/2"',
        why="Architect 32 vs body 27 1/2 plus a 4 top; confirm what 2'-8\" measures",
        arch_box=BOX_A,
        vendor_box=BOX_V,
    )
    named = dict(other, id="F2", why=None, check_reason="different_extent", page=7)
    scaled = dict(other, id="F3", why=None, architect='about 32" (scaled)', page=8)
    unprinted = dict(
        other,
        id="F4",
        why=None,
        page=6,
        arch_box=dict(BOX_A, page=6),
        vendor_box=dict(BOX_V, page=6),
    )
    result = resolve([other, named, scaled, unprinted], ResolverContext(verify=printed))
    statuses = {fid: f["status"] for fid, f in by_id(result).items()}
    assert statuses == {"F1": "check", "F2": "check", "F3": "check", "F4": "check"}
    assert by_id(result)["F1"]["keep_reason"] == "measures_something_else"
    assert by_id(result)["F1"]["question"].startswith(
        "Can you confirm what 2'-8\" measures? (Architect"
    )
    assert by_id(result)["F2"]["keep_reason"] == "measures_something_else"


def test_the_newest_round_printing_the_architects_value_is_right() -> None:
    fixed = finding(
        "F1",
        item="Overall height",
        architect="7'-6\"",
        vendor='84" (cut off); round 2 (p5): 90"',
        checks={"printed_architect": "match", "printed_vendor": "not_a_number"},
    )
    result = resolve([fixed], ResolverContext(areas=AREAS))
    assert by_id(result)["F1"]["status"] == "same"
    assert by_id(result)["F1"]["settled"]["method"] == "newest_round"


def test_a_prototype_that_prints_the_vendors_value_is_right_and_a_conflict_stays() -> None:
    matches = finding(
        "F1",
        source="brand_prototype",
        item="Cubby width",
        architect='prototype rear 2\'-3" (27"); front 2\'-6" (30")',
        vendor='30"',
        why="Prototype front 2'-6\" matches",
    )
    conflict = finding(
        "F2",
        source="brand_prototype",
        item="Bay width",
        architect="prototype 1'-8 1/2\"",
        vendor='24"',
        page=7,
    )
    result = resolve([matches, conflict], ResolverContext(areas=AREAS))
    assert by_id(result)["F1"]["status"] == "same"
    kept = by_id(result)["F2"]
    assert kept["keep_reason"] == "which_governs" and kept["question"].startswith("Which governs")


def test_prototype_questions_on_one_page_pair_are_asked_once() -> None:
    one = finding("F1", source="brand_prototype", item="Bay A width", architect='prototype 21"')
    two = finding("F2", source="brand_prototype", item="Bay B depth", architect='prototype 30"')
    result = resolve([one, two], ResolverContext())
    assert set(result.absorbed) == {"F2"}
    assert entry_of(result, "F2")["method"] == "same_question"
    assert by_id(result)["F1"]["also_on"][0]["finding_id"] == "F2"


def test_other_trades_tags_are_not_a_difference_and_listed() -> None:
    tags = finding(
        "F1",
        category="other",
        item="Material tags / vanity finish",
        architect="TL-1 wall tile; no ST/WD tag",
        vendor="ST-9 stone top, WD-9 panel; sink SNK-X1",
    )
    result = resolve([tags], ResolverContext())
    assert "F1" in result.absorbed and "F1" not in by_id(result)
    listed = entry_of(result, "F1")
    assert listed["outcome"] == "not_a_difference" and "TL-1" in listed["how"]
    assert result.block["after"]["decision"] == 0


def test_millwork_tags_that_differ_need_a_legend_on_the_sheets() -> None:
    tags = finding(
        "F1",
        category="countertop",
        item="Counter top tag",
        architect="CT-7",
        vendor="ST-8 stone top",
    )
    kept = resolve([tags], ResolverContext(legend=lambda pages, codes: None))
    assert by_id(kept)["F1"]["status"] == "check"
    legend = "CT-7 = ST-8 QUARTZ"
    found = resolve([tags], ResolverContext(legend=lambda pages, codes: legend))
    assert by_id(found)["F1"]["status"] == "same"
    assert by_id(found)["F1"]["settled"]["method"] == "legend"


def test_light_fixtures_are_out_of_scope() -> None:
    lights = finding(
        "F1", category="other", item="Pendant light count", why="not countertop/cabinet scope"
    )
    result = resolve([lights], ResolverContext())
    assert entry_of(result, "F1")["method"] == "out_of_scope"


# ---- the sharper read ------------------------------------------------------------------------------


def test_a_sharper_read_counts_only_when_code_confirms_the_value() -> None:
    hidden = finding(
        "F1",
        item="Vanity top width",
        architect="2'-6 1/2\"",
        vendor="covered by GV's yellow box",
        arch_box=BOX_A,
        vendor_box=dict(BOX_V, page=3),
    )
    asked: list[str] = []

    def read(f: Mapping[str, Any]) -> SharperRead:
        asked.append(str(f["id"]))
        return SharperRead(architect=None, vendor='29"', sure=True)

    confirmed = ResolverContext(read=read, verify=_witness({2: {"2'-6 1/2\""}, 3: {'29"'}}))
    result = resolve([hidden], confirmed)
    assert by_id(result)["F1"]["status"] == "different"
    assert by_id(result)["F1"]["settled"]["method"] == "sharper_read"
    assert result.block["reads"] == 1 and asked == ["F1"]

    unconfirmed = ResolverContext(read=read, verify=_witness({2: {"2'-6 1/2\""}}))
    result = resolve([hidden], unconfirmed)
    kept = by_id(result)["F1"]
    assert kept["status"] == "check" and kept["keep_reason"] == "unreadable"
    assert 'sharper read sees 29"' in kept["question"]


def test_the_sharper_reads_stop_at_their_limit() -> None:
    hidden = [
        finding(
            f"F{n}",
            page=n,
            item=f"Top width {n}",
            architect="2'-0\"",
            vendor="unreadable",
            arch_box=dict(BOX_A, page=n),
            vendor_box=dict(BOX_V, page=n),
        )
        for n in range(1, 6)
    ]
    calls: list[str] = []

    def read(f: Mapping[str, Any]) -> None:
        calls.append(str(f["id"]))

    ctx = ResolverContext(read=read, verify=lambda box, value: "match", read_limit=3)
    resolve(hidden, ctx)
    assert len(calls) == 3


# ---- the questions ----------------------------------------------------------------------------------


def test_every_kept_item_has_one_question() -> None:
    items = [
        finding("F1", architect="no width printed; scales about 58", vendor='63 3/4"'),
        finding("F2", item="Door layout", architect="4 doors", vendor="3 doors", page=7),
        finding(
            "F3",
            item="Sink offsets",
            why="needs counter depth and backsplash settings",
            page=8,
            architect="no sink dimension",
        ),
    ]
    result = resolve(items, ResolverContext(areas=AREAS))
    questions = {f["id"]: f["question"] for f in result.changed}
    assert questions["F1"] == (
        "The architect prints no size for the counter overall width (the architect's drawing "
        "scales about 58): is the vendor's 63 3/4\" right?"
    )
    assert questions["F2"].startswith("Does the vendor's door layout match the architect's?")
    assert questions["F3"].startswith("Which job values apply")
    assert result.block["kept"] == {
        "architect_prints_nothing": 1,
        "layout": 1,
        "needs_job_value": 1,
    }


def test_the_decision_helps_own_question_is_used_when_it_has_one() -> None:
    item = finding(
        "F1",
        decision_help={"confirm": "Is the box above the unit by others?", "likely": "can't tell"},
    )
    assert question_for(item, keep_reason(item)) == "Is the box above the unit by others?"


# ---- the API shapes, the drawing and the report -------------------------------------------------------


def test_the_resolver_block_and_the_new_finding_fields_fit_the_contract() -> None:
    old = finding("F1", page=2)
    new = finding("F2", page=3, round=2)
    wide = finding("F3", item="End filler width", vendor='5" filler', page=6)
    result = resolve([old, new, wide], ResolverContext(areas=AREAS, settings=SETTINGS))
    chat_contract.ChatResolverBlock.model_validate({"type": "resolver", "resolver": result.block})
    for changed in result.changed:
        shaped = {
            **changed,
            "verified": False,
            "created_by": "lead",
            "checks": None,
            "inches": None,
        }
        chat_contract.ChatFindingOut.model_validate(shaped)


def test_the_notes_pages_and_the_report_list_what_code_settled(tmp_path: Path) -> None:
    settled = finding(
        "F1",
        status="different",
        settled={"outcome": "wrong", "method": "gv_limits", "how": 'the vendor draws 5".'},
        also_on=[{"finding_id": "F9", "page": 3, "round": 2, "vendor": '5"'}],
    )
    lines = chat_drawing.entry_lines(settled)
    assert 'Settled by GV Review: the vendor draws 5".' in lines
    assert 'Also on p.3 (round 2): vendor 5"' in lines
    asked = finding("F2", question='Is it 80"?')
    assert 'Question: Is it 80"?' in chat_drawing.entry_lines(asked)
    entries = [
        {
            "finding_id": "F3",
            "page": 6,
            "item": "Material tags",
            "outcome": "not_a_difference",
            "method": "other_trades_tags",
            "how": "the architect tags only tile here.",
            "vendor": "ST-9",
            "architect": "TL-1",
        }
    ]
    out = tmp_path / "report.pdf"
    chat_drawing.check_report_pdf(
        out, {"Drawing set": "set.pdf"}, [settled, asked], [], settled=entries
    )
    from pypdf import PdfReader

    text = "".join(page.extract_text() for page in PdfReader(str(out)).pages)
    assert "Settled automatically by GV Review (1)" in text
    assert "the architect tags only tile here." in text


# ---- the engine's hook ----------------------------------------------------------------------------------


class _Book:
    def __init__(self, findings: list[dict[str, Any]], pdf: Path) -> None:
        self.findings = {str(f["id"]): f for f in findings}
        self.pdf = pdf
        self.main_file = "set.pdf"

    def live(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.findings.values()]

    def absorb(self, finding_id: str) -> bool:
        return self.findings.pop(finding_id, None) is not None

    def replace(self, finding: dict[str, Any]) -> bool:
        if finding["id"] not in self.findings:
            return False
        self.findings[finding["id"]] = finding
        return True

    def pdf_paths(self) -> dict[str, Path]:
        return {"set.pdf": self.pdf}


class _Engine:
    def __init__(self, tmp_path: Path, findings: list[dict[str, Any]]) -> None:
        skills = tmp_path / "skills"
        skills.mkdir()
        (skills / "rules.md").write_text(RULES_MD, encoding="utf-8")
        self.tools = type(
            "Tools", (), {"workspace": type("W", (), {"skills": skills, "set_folder": tmp_path})()}
        )()
        self.book = _Book(findings, tmp_path / "set.pdf")
        self.finding_lock = threading.Lock()
        self.cancel = threading.Event()
        self.spent = 0
        self.blocks: list[dict[str, Any]] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.shown: list[str] = []
        self.dropped: list[tuple[str, str]] = []

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        self.dropped.append((finding_id, reason))

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(str(finding["id"]))

    def _add_block(self, block: dict[str, Any]) -> int:
        self.blocks.append(block)
        return len(self.blocks) - 1

    def emit(self, event: str, data: dict[str, Any]) -> None:
        self.events.append((event, data))


def test_the_engine_hook_folds_settles_shows_and_adds_the_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(decision_resolver, "_reader", lambda engine: None)
    monkeypatch.setattr(decision_resolver, "_legend_reader", lambda paths: None)
    findings = [
        finding("F1", page=2, vs_page=None),
        finding("F2", page=2, vs_page=None, round=2),
        finding("F3", item="End filler width", vendor='5" filler', page=6),
    ]
    engine = _Engine(tmp_path, findings)
    settled = decision_resolver.resolve_answer(engine)
    assert [d[0] for d in engine.dropped] == ["F1"]
    assert sorted(engine.shown) == ["F2", "F3"]
    assert engine.book.findings["F3"]["status"] == "different"
    assert engine.book.findings["F3"]["trust_level"]
    assert engine.blocks[0]["type"] == "resolver" and engine.events[0][0] == "resolver"
    assert engine.blocks[0]["resolver"]["cost_usd"] == "0.00"
    assert {e["finding_id"] for e in settled} == {"F1", "F3"}


def test_the_engine_hook_does_nothing_without_decision_items(tmp_path: Path) -> None:
    engine = _Engine(tmp_path, [finding("F1", status="different")])
    assert decision_resolver.resolve_answer(engine) == []
    assert engine.blocks == []


def test_a_full_check_folds_the_same_decision_item_and_lists_it_in_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from pypdf import PdfReader

    from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
    from tests.review.assistant.test_chat_engine import Harness
    from tests.review.assistant.test_chat_engine_findings import _full_check, _modules
    from tests.review.assistant.test_chat_findings import finding_input, workspace_with_pdf

    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    same_item = {
        "item": "Counter overall width",
        "status": "check",
        "architect": "no width printed",
        "why": "nothing to compare",
    }
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input(**same_item, vendor='80"', page=2)),
                tool("r2", "report_finding", finding_input(**same_item, vendor='80"', page=3)),
                stop="tool_use",
            ),
            reply(text("Two pages show the counter; the architect prints no width.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    _full_check(harness)

    outcome = harness.run()

    types_ = [block["type"] for block in outcome.blocks]
    assert types_.index("resolver") < types_.index("trust")
    resolver = next(b["resolver"] for b in outcome.blocks if b["type"] == "resolver")
    assert resolver["before"]["decision"] == 2 and resolver["after"]["decision"] == 1
    assert [e["outcome"] for e in resolver["settled"]] == ["merged"]
    dropped = [b for b in outcome.blocks if b["type"] == "finding_dropped"]
    assert len(dropped) == 1 and dropped[0]["reason"].startswith("Settled by GV Review:")
    kept = [
        b["finding"]
        for b in outcome.blocks
        if b["type"] == "finding" and b["finding"]["status"] == "check"
    ]
    assert len(kept) == 1 and kept[0]["question"].endswith("?")
    assert kept[0]["also_on"][0]["page"] in (2, 3)
    assert ("resolver", resolver) in [(e, d.get("resolver")) for e, d in harness.events]
    report = "".join(
        page.extract_text()
        for page in PdfReader(str(harness.workspace.files / harness.files[1]["id"])).pages
    )
    assert "Settled automatically by GV Review (1)" in report
    drawing = PdfReader(str(harness.workspace.files / harness.files[0]["id"]))
    assert "Settled automatically by GV Review" in drawing.pages[-1].extract_text()


def test_the_sharper_read_goes_through_the_engine_and_stops_at_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import read_adjudication
    from tests.review.assistant.test_chat_findings import workspace_with_pdf

    workspace = workspace_with_pdf(tmp_path)
    pdf = workspace.uploads / "Vendor set.pdf"
    monkeypatch.setattr(
        read_adjudication,
        "render_crops",
        lambda path, box, **_: read_adjudication.SideCrops(upright=b"png"),
    )

    class Reply:
        stop_reason = "end_turn"

        def text(self) -> str:
            return '{"architect": null, "vendor": "29\\"", "sure": true}'

    calls: list[dict[str, Any]] = []

    class Engine:
        cancel = threading.Event()
        check_prompt_id = "review-assistant-chat-check-v1"
        book = type("B", (), {"pdf_paths": staticmethod(lambda: {"Vendor set.pdf": pdf})})()

        def _price(self, model: Any, prompt: int, out: int) -> int:
            return 200_000

        def _fits(self, amount: int) -> bool:
            return True

        def _call(self, **kwargs: Any) -> Reply:
            calls.append(kwargs)
            return Reply()

    read = decision_resolver._reader(Engine())
    assert read is not None
    box = {"page": 2, "x0": 10.0, "y0": 10.0, "x1": 40.0, "y1": 20.0, "file": "Vendor set.pdf"}
    got = read({"id": "F1", "item": "Top width", "vendor_box": box, "arch_box": box})
    assert got == SharperRead(architect=None, vendor='29"', sure=True)
    assert calls[0]["effort"] == "low" and "resolve" in calls[0]["prompt_id"]
    assert read({"id": "F2", "item": "Top width", "vendor_box": box}) is not None
    # $0.20 each: a third would pass the $0.50 cap, so it is not made.
    assert read({"id": "F3", "item": "Top width", "vendor_box": box}) is None
    assert len(calls) == 2


# ---- the counts agree everywhere (#1554 review) -----------------------------------------------


def test_the_ai_s_counts_are_set_to_the_final_list() -> None:
    from app.review.assistant.decision_resolver import recount_words

    counts = {"wrong": 4, "decision": 1, "right": 12, "not_compared": 0}
    said = (
        "Six items are wrong; three need your decision and 9 match. On page 3 right of the sink, "
        "the 2 right-hand doors are fine. Round 2 wrong values: see p.2 wrong."
    )
    assert recount_words(said, counts) == (
        "Four items are wrong; one needs your decision and 12 match. On page 3 right of the sink, "
        "the 2 right-hand doors are fine. Round 2 wrong values: see p.2 wrong."
    )
    assert recount_words("43 wrong, 60 to check, 24 right.", counts) == (
        "4 wrong, 1 to check, 12 right."
    )


def test_every_count_shown_comes_from_the_final_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The AI's words, the resolver block, the trust report, the cloud drawing and the report all
    count the list left after the resolver: a count that differs anywhere fails."""
    import re

    from pypdf import PdfReader

    from app.review.assistant.chat_trust import shown_counts
    from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
    from tests.review.assistant.test_chat_engine import Harness
    from tests.review.assistant.test_chat_engine_findings import _full_check, _modules
    from tests.review.assistant.test_chat_findings import finding_input, workspace_with_pdf

    _modules(monkeypatch)
    workspace_with_pdf(tmp_path)
    same_item = {
        "item": "Counter overall width",
        "status": "check",
        "architect": "no width printed",
        "why": "nothing to compare",
    }
    filler = {"item": "End filler width", "status": "check", "architect": "GV rule 1-2"}
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input(**same_item, vendor='80"', page=2)),
                tool("r2", "report_finding", finding_input(**same_item, vendor='80"', page=3)),
                tool("r3", "report_finding", finding_input(**filler, vendor='5" filler', page=3)),
                stop="tool_use",
            ),
            reply(text("Two items are wrong and three need your decision; nothing is right.")),
        ]
    )
    harness = Harness(tmp_path, provider)
    harness.workspace.skills.mkdir(parents=True, exist_ok=True)
    (harness.workspace.skills / "rules.md").write_text(RULES_MD, encoding="utf-8")
    _full_check(harness)

    outcome = harness.run()

    live = [b["finding"] for b in outcome.blocks if b["type"] == "finding"]
    final = shown_counts(live)
    assert final["decision"] == 1 and final["wrong"] == 3  # 2 recorded + the filler
    # The AI's words.
    words = " ".join(b["text"] for b in outcome.blocks if b["type"] == "text")
    assert "Three items are wrong and one needs your decision" in words
    assert outcome.text.startswith("Three items are wrong and one needs your decision")
    # The resolver block.
    resolver = next(b["resolver"] for b in outcome.blocks if b["type"] == "resolver")
    assert resolver["after"] == {k: final[k] for k in ("wrong", "decision", "right")}
    assert resolver["before"]["decision"] == resolver["after"]["decision"] + len(
        resolver["settled"]
    )
    # The report's cover and the cloud drawing's counts line.
    report = "".join(
        page.extract_text()
        for page in PdfReader(str(harness.workspace.files / harness.files[1]["id"])).pages
    )
    assert (
        f"{final['wrong']} wrong, {final['decision']} need your decision, {final['right']} right"
        in report
    )
    assert f"What's wrong ({final['wrong']})" in report
    assert f"Your decision ({final['decision']})" in report
    # The zero-click cloud drawing's cover (#1581): its groups split the same final list.
    from app.review.assistant.proof import proof_counts

    groups = proof_counts(live)
    assert groups["clouded"] + groups["not_verified"] == final["wrong"]
    assert groups["could_not_compare"] == final["decision"]
    assert groups["right"] == final["right"]
    drawing = PdfReader(str(harness.workspace.files / harness.files[0]["id"]))
    cover = drawing.pages[0].extract_text()
    for count, words in (
        (groups["clouded"], "Wrong, clouded"),
        (groups["not_verified"], "Not verified by code"),
        (groups["could_not_compare"], "Could not compare"),
        (groups["right"], "Right"),
    ):
        assert re.search(rf"\b{count}\s+{words}", cover), (count, words)


# ---- trust follows how code settled it (#1554 review) ------------------------------------------


def _judged(result: decision_resolver.Resolution, fid: str) -> dict[str, Any]:
    from app.review.assistant.chat_trust import judged

    return judged(by_id(result)[fid])


def test_code_s_witness_is_recorded_so_trust_follows_the_trust_rule() -> None:
    printed = finding(
        "F1",
        item="Outlet height",
        architect="5'-0\"",
        vendor='61 1/2"',
        arch_box=dict(BOX_A, page=2),
        vendor_box=dict(BOX_V, page=3),
    )
    checked = dict(printed, id="F2", page=7, checks={**printed["checks"], "lead_checked": True})
    ctx = ResolverContext(verify=_witness({2: {"5'-0\""}, 3: {'61 1/2"'}}))
    result = resolve([printed, checked], ctx)
    one, two = _judged(result, "F1"), _judged(result, "F2")
    assert one["checks"]["printed_architect"] == one["checks"]["printed_vendor"] == "match"
    assert "printed" in one["witnesses"] and one["evidence_kind"] == "observed"
    lone = dict(one["checks"], lead_checked=False)
    from app.review.assistant.chat_trust import trust_level

    assert trust_level(lone, "different") == "likely"  # code alone: Likely
    assert two["trust_level"] == "confirmed"  # code's witness and the lead's own check

    drawn = {"result": "match", "route": "drawn", "value": '61 1/2"', "read": '61 1/2"'}
    ink = ResolverContext(
        verify=lambda box, value: (
            drawn if box["page"] == 3 else {"result": "match", "route": "text", "value": value}
        )
    )
    result = resolve([printed], ink)
    seen = _judged(result, "F1")
    assert seen["checks"]["verify_vendor"]["route"] == "drawn"
    assert "ocr_ink" in seen["witnesses"]


def test_arithmetic_and_rule_settlements_record_their_witness() -> None:
    slip = finding(
        "F1",
        source="vendor_arithmetic",
        item="Front height string",
        parts=["4", "10 1/8", "66"],
        printed_total="80",
        vendor="80",
    )
    wide = finding("F2", item="End filler width", vendor='5" filler', page=7)
    result = resolve([slip, wide], ResolverContext(settings=SETTINGS))
    sums, rule = _judged(result, "F1"), _judged(result, "F2")
    assert sums["checks"]["arithmetic"] == "does_not_add_up" and "arithmetic" in sums["witnesses"]
    assert rule["checks"]["rule"] == "agrees" and "rule" in rule["witnesses"]
    assert sums["trust_level"] == rule["trust_level"] == "confirmed"  # with the lead's check


def test_a_prototype_print_is_the_printed_witness_when_code_reads_both() -> None:
    matches = finding(
        "F1",
        source="brand_prototype",
        item="Cubby width",
        architect='prototype front 2\'-6" (30")',
        vendor='30"',
        why="Prototype front 2'-6\" matches",
        arch_box=BOX_A,
        vendor_box=dict(BOX_V, page=3),
    )
    read = ResolverContext(verify=_witness({2: {"2'-6\""}, 3: {'30"'}}))
    seen = _judged(resolve([matches], read), "F1")
    assert seen["status"] == "same" and "printed" in seen["witnesses"]
    assert "code reads both at their spots" in seen["settled"]["how"]
    unread = _judged(resolve([matches], ResolverContext()), "F1")
    assert unread["status"] == "same" and "printed" not in unread["witnesses"]


# ---- the vendor's own string reads as a sum, never "needs nothing" -----------------------------


def test_a_vendor_string_carries_code_s_sum_for_its_line(tmp_path: Path) -> None:
    from app.review.assistant.chat_trust import apply_checks, needs_text

    slip = finding(
        "F1",
        source="vendor_arithmetic",
        item="Front height string",
        parts=["4", "10 1/8", "66"],
        printed_total="80",
        vendor="80",
        architect="n/a",
        verified=True,
    )
    checked = apply_checks(slip, {}, None)
    assert checked["parts_sum"] == "80.125"
    assert needs_text(checked) == 'parts add up to 80 1/8"'


# ---- the same item is clouded on every page it is on ------------------------------------------------


def test_the_cloud_drawing_marks_every_page_an_item_is_also_on(tmp_path: Path) -> None:
    from pypdf import PdfReader

    from app.review.assistant import proof_drawing
    from tests.review.assistant.test_chat_findings import workspace_with_pdf

    workspace = workspace_with_pdf(tmp_path)
    pdf = workspace.uploads / "Vendor set.pdf"
    box = {"x0": 300.0, "y0": 400.0, "x1": 360.0, "y1": 416.0, "file": "Vendor set.pdf"}
    wrong = finding(
        "F1",
        status="different",
        page=2,
        item="Counter width",
        architect='37"',
        vendor='46"',
        vendor_box={**box, "page": 2},
        also_on=[
            {"finding_id": "F9", "page": 3, "vendor": '45"', "vendor_box": {**box, "page": 3}}
        ],
    )
    placed = [(1, wrong, 2, (300.0, 400.0, 360.0, 416.0), "Marked on page 2.")]
    out = tmp_path / "cloud.pdf"
    chat_drawing.markup_pdf(pdf, placed, out)
    pages = PdfReader(str(out)).pages
    assert '45"' in pages[2].extract_text()  # the round-2 page carries its own cloud and note
    notes = " ".join(page.extract_text() for page in pages[3:])
    assert "On the drawing (1)" in notes  # listed once on the notes pages

    agreed = dict(wrong, decision={"decision": "agree", "by": "R", "at": "2026-10-17T10:00:00Z"})
    out = tmp_path / "decided.pdf"
    proof_drawing.proof_markup_pdf(
        pdf, [(1, agreed, 2, placed[0][3], "")], out, set_name="Vendor set.pdf", stamp="Checked"
    )
    # The zero-click drawing starts with its cover: the vendor's page 3 is the PDF's fourth.
    assert '45"' in PdfReader(str(out)).pages[3].extract_text()
