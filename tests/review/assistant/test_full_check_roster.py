"""A full check as a fixed-price roster of specialists, with no lead (prompts design, Balanced).

Scripted models, a synthetic page map and a real tiny PDF: no network, no paid call, no Docker.

- The price: each phase's share of the fixed price and each specialist's cap within it; the caps
  can never add up to more than the price.
- The devil's advocate's code checks (ink, GV's notes, roles, axis, newest round, mm vs inches,
  unit slips), the drawn-extent check, and decision D1 in both modes.
- The run: no lead call; pickers, readers and the tag reader at once; the verifier and the tag
  reader reply in their schema; the critic is off; the advocate passes a red candidate whose blind
  pick and spans agree and keeps grey one whose pick differs; the summary names the fixed price;
  a price too small for every phase lists what was not done instead of dropping it.
"""

from __future__ import annotations

import dataclasses
import io
import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from app.review.assistant import full_check_roster
from app.review.assistant.chat_engine import FULL_CHECK_INSTRUCTION, ROSTER_HELPER_MAX_TOKENS
from app.review.assistant.full_check_advocate import (
    ADVOCATE_SYSTEM,
    CODE_PAIRS_FILE,
    Check,
    code_checks,
    code_pairs,
    decide,
    ink_colour,
    is_slip,
    needs_pairing,
    same_extent,
)
from app.review.assistant.full_check_roster import (
    SUMMARY_SYSTEM,
    PricePlan,
    apply_proofs,
    compare_tags,
    keeps_numbers,
)
from app.review.assistant.full_check_specialists import HELPER_CORE
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import DOLLARS, Harness
from tests.review.assistant.test_chat_findings import workspace_with_pdf
from tests.review.assistant.test_full_check_fanout import (
    FILE,
    HAIKU,
    RECORDS,
    _box,
    _first_text,
    _pack,
    _set_map,
    _steps,
)

SONNET = "claude-sonnet-5-5"


@pytest.fixture(autouse=True)
def _reader(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GV_READER_MISSING_SPACE_HEIGHTS", "0.1")


# ---- the price -----------------------------------------------------------------------------------------


def test_the_caps_never_add_up_to_more_than_the_price() -> None:
    for groups in (1, 4, 13, 40):
        plan = PricePlan.balanced(5 * DOLLARS, groups)
        assert plan.most() <= 5 * DOLLARS
        # Every specialist of a phase together stays inside the phase's budget.
        assert plan.caps["numbers"] * groups <= plan.budgets["numbers"]
        assert plan.caps["drawn"] * groups <= plan.budgets["drawn"]
        assert plan.caps["gaps"] * full_check_roster.MAX_GAP_CLOSERS <= plan.budgets["gaps"]
        assert plan.caps["provers"] * groups <= plan.budgets["provers"]
    # A page-group helper's cap follows its pages (#1634): a five-page group of a 45-page set gets
    # five forty-fifths of its phase, with no small fixed cap; the groups together never more.
    plan = PricePlan.balanced(5 * DOLLARS, 10, 45)
    assert (
        plan.cap_for("numbers", 5)
        == int(5 * DOLLARS * full_check_roster.SHARES["numbers"]) * 5 // 45
    )
    assert plan.cap_for("numbers", 5) > 150_000  # more than the old fixed cap that starved them
    assert (
        plan.cap_for("drawn", 3) == int(5 * DOLLARS * full_check_roster.SHARES["drawn"]) * 3 // 45
    )
    assert sum(plan.cap_for("numbers", n) for n in [5] * 9) <= plan.budgets["numbers"]
    assert plan.cap_for("tags", 45) == plan.caps["tags"] <= full_check_roster.JOB_CAPS["tags"]


def test_every_share_and_cap_follows_the_price() -> None:
    """#1805: caps made for a $5 price stopped "look again" at $1.69 of a $15 price."""
    five, fifteen = PricePlan.balanced(5 * DOLLARS, 2), PricePlan.balanced(15 * DOLLARS, 2)
    assert abs(sum(full_check_roster.SHARES.values()) - 1) < 1e-9
    for name in full_check_roster.SHARES:
        assert abs(fifteen.budgets[name] - 3 * five.budgets[name]) <= 3
    for name in ("tags", "provers", "advocate", "lead", "summary"):
        assert abs(fifteen.caps[name] - 3 * five.caps[name]) <= 3
    assert (
        full_check_roster.scaled_cap("lead", 15 * DOLLARS) == 3 * full_check_roster.JOB_CAPS["lead"]
    )
    # The steps after the verifier keep their shares; the second reads' share is kept to the end.
    after = fifteen.after("provers")
    assert after == sum(
        fifteen.budgets[n] for n in ("lead", "advocate", "escalation", "summary", "reads")
    )
    assert fifteen.after("summary") == fifteen.budgets["reads"]


def test_a_phase_pool_lets_a_busy_specialist_take_what_the_others_left() -> None:
    from types import SimpleNamespace

    def agent(cap: int) -> Any:
        return SimpleNamespace(cap_micros=cap, spent=0, done=False, top_up=None)

    pool = full_check_roster._Pool(1_000_000)
    cheap, busy = pool.add(agent(500_000)), pool.add(agent(500_000))
    assert busy.top_up == pool.top_up
    busy.spent = 450_000
    assert pool.top_up(busy) == 0  # the other one may still spend its cap
    cheap.spent, cheap.done = 100_000, True
    assert pool.top_up(busy) == 400_000  # what the finished one left, never more
    busy.cap_micros += 400_000
    assert pool.top_up(busy) == 0


def test_stopped_at_its_cap_only_when_the_price_is_used_up(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    run = _run(harness)
    run._capped("Look again, pages 3-7")
    assert not run.stopped_for_money
    assert "used its share of the price" in run.not_done[-1]
    harness.engine._spent = 5 * DOLLARS - 10_000
    run._capped("Look again, pages 8-9")
    assert run.stopped_for_money and run.not_done[-1].endswith("stopped at its cap")


def test_the_timings_line_shows_each_phase_against_its_share() -> None:
    from app.review.assistant.chat_timings import Timings

    timings = Timings()
    timings.set_budget("helpers", 2_500_000)
    with timings.phase("helpers"):
        timings.charge(1_250_000)
    with timings.phase("lead_review"):
        timings.charge(10_000)
    line = Timings.log_line(timings.as_dict(1_260_000))
    assert "helpers" in line and "$1.25 of $2.50 (1)" in line
    assert "lead_review" in line and "$0.01 (1)" in line


# ---- the advocate's code checks -------------------------------------------------------------------------


def _label(
    label_id: str,
    side: str,
    inches: str,
    ends: tuple[float, float],
    *,
    y: float = 200,
    orientation: str = "h",
    **extra: Any,
) -> dict[str, Any]:
    x0, x1 = ends
    return {
        "id": label_id,
        "page": 5,
        "file": FILE,
        "side": side,
        "text": f'{inches}"',
        "inches": inches,
        "orientation": orientation,
        "route": "text",
        "units": None,
        "box": [x0 + 10, y - 10, x0 + 30, y - 2],
        "dim_line": [[x0, y], [x1, y]],
        **extra,
    }


def _png(colour: tuple[int, int, int]) -> bytes:
    picture = Image.new("RGB", (20, 10), (255, 255, 255))
    for x in range(5, 15):
        picture.putpixel((x, 5), colour)
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    return buffer.getvalue()


def test_ink_tells_the_drawings_black_from_gvs_colour() -> None:
    black = ink_colour(_png((20, 20, 20)))
    red = ink_colour(_png((220, 30, 40)))
    assert black is not None and black[1] == 0
    assert red is not None and red[1] == 1
    assert ink_colour(_png((255, 255, 255))) is None


def test_unit_slips_and_drawn_misreads_are_not_differences() -> None:
    assert is_slip(Decimal(36), Decimal(3), drawn=False)
    assert is_slip(Decimal(18), Decimal(180), drawn=False)
    assert is_slip(Decimal(17), Decimal(11), drawn=True)  # a drawn 7 read as 1
    assert is_slip(Decimal(17), Decimal(11), drawn=False) is None
    assert is_slip(Decimal(10), Decimal(8), drawn=True) is None


def test_the_code_checks_catch_gvs_notes_the_wrong_roles_axis_round_and_units() -> None:
    vendor = _label("D1", "vendor", "46", (300, 392))
    architect = _label("D2", "architect", "37", (100, 174))
    clean = code_checks(vendor, architect, vendor_ink=(40, 0.0))
    assert all(check.outcome == "pass" for check in clean), clean
    note = {"page": 5, "file": FILE, "kind": "FreeText", "text": "STONE TOP by GV",
            "box": [300, 180, 360, 210]}  # fmt: skip
    failed = {c.name: c for c in code_checks(vendor, architect, annotations=[note],
                                             vendor_ink=(40, 0.5))}  # fmt: skip
    assert failed["annotation"].outcome == "fail" and "by GV" in failed["annotation"].why
    assert failed["ink"].outcome == "fail"
    brand = {**architect, "side": "brand"}
    assert {c.name: c.outcome for c in code_checks(vendor, brand, vendor_ink=(4, 0))}["roles"] == (
        "fail"
    )
    upright = {**architect, "orientation": "v"}
    assert {c.name: c.outcome for c in code_checks(vendor, upright, vendor_ink=(4, 0))}["axis"] == (
        "fail"
    )
    mixed = {**vendor, "units": "disagrees"}
    assert {c.name: c.outcome for c in code_checks(mixed, architect, vendor_ink=(4, 0))}[
        "units"
    ] == "fail"
    page_map = {
        "pages": [
            {"page": 5, "file": FILE, "area": "A", "halves": [
                {"role": "vendor", "round": 1, "box": [0, 0, 900, 900]}]},
            {"page": 6, "file": FILE, "area": "A", "halves": [
                {"role": "vendor", "round": 2, "box": [0, 0, 900, 900]}]},
        ]
    }  # fmt: skip
    old = {c.name: c for c in code_checks(vendor, architect, page_map=page_map, vendor_ink=(4, 0))}
    assert old["round"].outcome == "fail" and "round 2" in old["round"].why
    unknown = {c.name: c.outcome for c in code_checks(vendor, architect)}
    assert unknown["ink"] == "unknown"


def test_the_drawn_extents_must_land_together_after_lining_the_drawings_up() -> None:
    # The architect's drawing at x 100-300, the vendor's at x 300-500, both 2 pt an inch.
    vendor = _label("D1", "vendor", "46", (300, 392))
    architect = _label("D2", "architect", "37", (100, 174))
    anchors_v = [
        _label("D3", "vendor", "24", (380, 428)),
        _label("D4", "vendor", "12", (440, 464)),
    ]
    anchors_a = [
        _label("D5", "architect", "24", (180, 228)),
        _label("D6", "architect", "12", (240, 264)),
    ]
    state, why = same_extent(vendor, architect, [vendor, *anchors_v], [architect, *anchors_a])
    assert state == "same_extent", why
    # A neighbouring segment of the architect's chain: it starts 30 pt along, so its ends land
    # apart by more than the claimed difference allows.
    neighbour = _label("D2", "architect", "37", (130, 204))
    state, _why = same_extent(vendor, neighbour, [vendor, *anchors_v], [neighbour, *anchors_a])
    assert state == "different_extent"
    # One anchor only: code cannot line the drawings up, so it cannot tell.
    state, _why = same_extent(vendor, architect, [vendor, anchors_v[0]], [architect, anchors_a[0]])
    assert state == "cannot_tell"
    no_line = {**architect, "dim_line": None}
    assert same_extent(vendor, no_line, anchors_v, anchors_a)[0] == "cannot_tell"


def test_a_line_off_its_drawings_scale_is_no_proof_of_different_points() -> None:
    """A label printed beside its own short dimension line, across from it (a horizontal value
    next to a vertical offset), may be given a nearby drawing edge as its line: the label reader
    then marks it off its drawing's scale (`scale_ok: False`). Two such lines landing apart prove
    nothing about the points the two values measure: code cannot tell, never 'different'."""
    vendor = _label("D1", "vendor", "46", (300, 392))
    anchors_v = [
        _label("D3", "vendor", "24", (380, 428)),
        _label("D4", "vendor", "12", (440, 464)),
    ]
    anchors_a = [
        _label("D5", "architect", "24", (180, 228)),
        _label("D6", "architect", "12", (240, 264)),
    ]
    neighbour = _label("D2", "architect", "37", (130, 204))
    labels_v, labels_a = [vendor, *anchors_v], [neighbour, *anchors_a]
    assert same_extent(vendor, neighbour, labels_v, labels_a)[0] == "different_extent"
    for v, a in (
        ({**vendor, "scale_ok": False}, neighbour),
        (vendor, {**neighbour, "scale_ok": False}),
    ):
        state, why = same_extent(v, a, labels_v, labels_a)
        assert state == "cannot_tell" and "off its drawing's scale" in why, why
    # On the drawing's scale (or not measured): the lines still decide.
    for flag in (True, None):
        v = {**vendor, "scale_ok": flag}
        assert same_extent(v, neighbour, labels_v, labels_a)[0] == "different_extent"


class _Labels:
    """The run's labels of one page (`DimLabels.on_page`), with an uploads folder."""

    def __init__(self, labels: list[dict[str, Any]]) -> None:
        self.labels = labels
        self.uploads = Path("/nowhere")

    def on_page(self, file: str, page: int, *, read_now: bool = True) -> list[dict[str, Any]]:
        return self.labels


def _bare_run(labels: list[dict[str, Any]]) -> Any:
    engine = type("Engine", (), {})()
    engine.book = type("Book", (), {})()
    engine.book.dim_labels = _Labels(labels)
    return full_check_roster._Run(engine)


def test_where_the_anchor_fit_cannot_tell_the_drawn_run_decides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_extent`: the anchor fit first; only its `cannot_tell` asks the drawn run (`same_ends`),
    whose answer is said as the drawn run's; a failed drawn-run proof leaves the fit's answer."""
    vendor = _label("D30", "vendor", "29.25", (100, 205.3))
    architect = _label("D2", "architect", "29", (400, 458))
    run = _bare_run([vendor, architect])
    asked: list[str] = []

    def proved(v: Any, a: Any, **_kw: Any) -> tuple[str, str]:
        asked.append(str(v["id"]))
        return "same_extent", "one end is tied on both drawings (the floor)"

    monkeypatch.setattr(full_check_roster.same_ends, "prove_labels", proved)
    state, why = run._extent(vendor, architect, {"pages": []})
    assert state == "same_extent" and why.startswith("the drawn run: ") and asked == ["D30"]

    def broken(*_a: Any, **_kw: Any) -> tuple[str, str]:
        raise RuntimeError("no line work")

    monkeypatch.setattr(full_check_roster.same_ends, "prove_labels", broken)
    state, _why = run._extent(vendor, architect, {"pages": []})
    assert state == "cannot_tell"

    # a width against a height: the anchor fit's own answer, the drawn run never asked
    asked.clear()
    monkeypatch.setattr(full_check_roster.same_ends, "prove_labels", proved)
    height = _label("D3", "architect", "29", (400, 458), orientation="v")
    assert run._extent(vendor, height, None)[0] == "different_extent" and asked == []


def _passing() -> list[Check]:
    return [Check(name, "pass") for name in ("ink", "annotation", "roles", "axis", "round")]


def test_decision_d1_needs_every_condition_and_code_only_needs_code() -> None:
    spans = {"from": "floor", "to": "top_of_counter"}
    agree = {"pick": "p5.D2", "vendor_spans": spans, "architect_spans": spans,
             "vendor_ink_black": "yes"}  # fmt: skip
    same = ("same_extent", "ok")
    common = {"checks": _passing(), "printed": True, "helper_pick": "p5.D2", "code_paired": False}
    passed = decide(mode="ai_pair_plus_code", advocate=agree, extent=same, **common)  # type: ignore[arg-type]
    assert passed.result == "passed" and passed.answers["pairing"] == "proved"
    for advocate, extent, words in (
        ({**agree, "pick": "p5.D9"}, same, "a blind second pick chose p5.D9"),
        ({**agree, "pick": "none"}, same, "chose no architect dimension"),
        ({**agree, "architect_spans": {"from": "floor", "to": "box_above_unit"}}, same,
         "land in different places"),
        (agree, ("different_extent", "the ends land apart"), "the ends land apart"),
        (agree, ("cannot_tell", "too few dimensions"), "too few dimensions"),
        (None, same, "Not verified within the fixed price"),
        (None, ("cannot_tell", "no line"), "no line"),  # geometry first: no AI call is needed
        ({**agree, "vendor_ink_black": "no"}, same, "in colour"),
    ):  # fmt: skip
        verdict = decide(mode="ai_pair_plus_code", advocate=advocate, extent=extent, **common)  # type: ignore[arg-type]
        assert verdict.result == "objects" and words in verdict.reason, (words, verdict.reason)
    not_printed = decide(
        mode="ai_pair_plus_code",
        advocate=agree,
        extent=same,
        **{**common, "printed": False},  # type: ignore[arg-type]
    )
    assert not_printed.result == "objects"
    code_only = decide(mode="code_only", advocate=agree, extent=same, **common)  # type: ignore[arg-type]
    assert code_only.result == "objects" and "code_only" in code_only.reason
    vendor_sum = decide(
        mode="code_only",
        advocate=None,
        extent=None,
        **{**common, "code_paired": True},  # type: ignore[arg-type]
    )
    assert vendor_sum.result == "passed" and vendor_sum.answers["pairing"] == "code"
    failed = decide(
        mode="ai_pair_plus_code",
        advocate=agree,
        extent=same,
        **{**common, "checks": [*_passing(), Check("slip", "fail", "a unit slip")]},  # type: ignore[arg-type]
    )
    assert failed.result == "objects" and "a unit slip" in failed.reason
    critic = passed.critic()
    assert critic["result"] == "passed" and critic["model"] == "devil's advocate"


def test_which_items_need_their_pairing_proved() -> None:
    assert needs_pairing({"source": "architect"})
    assert needs_pairing({"source": "gv_review"})
    assert not needs_pairing({"source": "gv_review", "rule_check_id": "CT-WIDTH-001"})
    assert not needs_pairing({"source": "vendor_arithmetic"})
    assert not needs_pairing({"source": "architect", "paired_by": "code"})


def test_an_areas_tags_of_different_kinds_are_not_compared() -> None:
    """Tags filed under an area's name alone: a fixture tag against a finish tag labels two
    different things (no kind in common), so it is never a Wrong; tags of one kind still are, and
    a named piece is compared as before."""
    table = {
        "tags": [
            {"page": 3, "side": "architect", "item": "2. Lobby", "tag": "PL-191A", "box": None},
            {"page": 3, "side": "vendor", "item": "2. Lobby", "tag": "LA-097", "box": None},
            {"page": 4, "side": "architect", "item": "5. Bar", "tag": "LA-091", "box": None},
            {"page": 4, "side": "architect", "item": "5. Bar", "tag": "CT-099", "box": None},
            {"page": 4, "side": "vendor", "item": "5. Bar", "tag": "LA-092", "box": None},
            {"page": 6, "side": "architect", "item": "6. Desk: top", "tag": "CT-099", "box": None},
            {"page": 6, "side": "vendor", "item": "6. Desk: top", "tag": "ST-094", "box": None},
        ]
    }  # fmt: skip
    found = {f["item"]: f["status"] for f in compare_tags(json.dumps(table))}
    assert found == {"5. Bar: tag": "different", "6. Desk: top: tag": "different"}


def test_code_compares_the_tag_table_by_piece() -> None:
    table = {
        "tags": [
            {"page": 5, "side": "architect", "item": "Unit B: top", "tag": "ST-02", "box": None},
            {"page": 5, "side": "vendor", "item": "unit b top", "tag": "ST-3",
             "box": {"page": 5, "x0": 1, "y0": 1, "x1": 9, "y1": 9}},
            {"page": 5, "side": "architect", "item": "Unit B: splash", "tag": "st 2", "box": None},
            {"page": 5, "side": "vendor", "item": "Unit B: splash", "tag": "ST-02", "box": None},
            {"page": 6, "side": "vendor", "item": "Unit C: top", "tag": "QZ-1", "box": None},
        ]
    }  # fmt: skip
    found = {f["item"]: f for f in compare_tags(json.dumps(table))}
    assert (
        found["unit b top: tag"]["status"] == "different"
        or found.get("Unit B: top: tag", {}).get("status") == "different"
    )
    statuses = sorted(f["status"] for f in found.values())
    assert statuses == ["different", "same"]  # the vendor-only tag is not compared
    assert compare_tags("not json") == []
    # #1811: the same table fenced, or after a paragraph (as a route without structured outputs
    # returns it), gives the same rows; two different tables in one reply give none.
    bare = compare_tags(json.dumps(table))
    assert compare_tags(f"```json\n{json.dumps(table)}\n```") == bare
    assert compare_tags(f"I read every tag.\n\n```json\n{json.dumps(table)}\n```") == bare
    other = {"tags": table["tags"][:2]}
    assert (
        compare_tags(f"```json\n{json.dumps(table)}\n```\n```json\n{json.dumps(other)}\n```") == []
    )


def test_the_reworded_summary_must_keep_every_number() -> None:
    code = "Checked 58 of 60 pages. 12 Wrong, 3 need your decision, 40 Right."
    assert keeps_numbers(code, "We checked 58 of 60 pages: 12 Wrong, 3 to decide, 40 Right.")
    assert not keeps_numbers(code, "We checked 58 of 60 pages: 13 Wrong, 3 to decide, 40 Right.")
    assert not keeps_numbers(code, "We checked most pages: 12 Wrong, 3 to decide, 40 Right.")


# ---- the run, end to end, with scripted models ------------------------------------------------------------


def _dims(pages: list[int]) -> dict[str, Any]:
    out = []
    for page in pages:
        out.append(
            {
                "file": FILE,
                "page": page,
                "drawn": "not_run",
                "labels": [
                    {**_label("D1", "vendor", "46", (300, 392)), "page": page,
                     "box": [300, 100, 360, 116]},
                    {**_label("D2", "architect", "37", (100, 174)), "page": page,
                     "box": [100, 100, 160, 116]},
                ],
            }
        )  # fmt: skip
    return {"version": 1, "files": [FILE], "pages": out}


class _Roster:
    """Every call of a roster full check, answered from its parameters, with what each saw."""

    def __init__(
        self, advocate_pick: str = "D2", *, fenced: bool = False, unpaired: int | None = None
    ) -> None:
        self.lock = threading.Lock()
        self.calls: dict[str, int] = {}
        self.params: dict[str, list[Mapping[str, Any]]] = {}
        self.advocate_pick = advocate_pick
        #: Every JSON answer wrapped as a route without structured outputs sends it (#1811).
        self.fenced = fenced
        #: A page its numbers helper records as having no architect drawing to compare.
        self.unpaired = unpaired

    def _json(self, value: object) -> str:
        body = json.dumps(value)
        return f"Here is my answer.\n\n```json\n{body}\n```" if self.fenced else body

    def _seen(self, kind: str, params: Mapping[str, Any]) -> None:
        with self.lock:
            self.calls[kind] = self.calls.get(kind, 0) + 1
            self.params.setdefault(kind, []).append(params)

    def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        system = params["system"][0]["text"]
        asked = _first_text(params)
        if asked == "Reply with OK.":
            self._seen("warm", params)
            return reply(text("OK"), model=SONNET, stop="max_tokens")
        if system == ADVOCATE_SYSTEM:
            self._seen("advocate", params)
            spans = {"from": "floor", "to": "top_of_counter"}
            answer = {"architect_pick": self.advocate_pick, "vendor_spans": spans,
                      "architect_spans": spans, "vendor_ink_black": "yes", "note": ""}  # fmt: skip
            return reply(text(self._json(answer)), model=SONNET)
        if system == SUMMARY_SYSTEM:
            self._seen("summary", params)
            return reply(text(json.dumps({"lines": ["Too short."]})), model=HAIKU)
        if system.startswith(HELPER_CORE):
            return self.helper(params, asked)
        self._seen("read", params)
        answer = {"architect": '37"', "vendor": '46"', "sure": True}
        return reply(text(json.dumps(answer)), model=HAIKU if params["model"] == HAIKU else SONNET)

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        step = _steps(params)
        if brief.startswith("LEAD REVIEW"):
            self._seen("lead", params)
            return reply(text("Nothing left to place."), model=params["model"])
        if brief.startswith("TAGS:"):
            self._seen("tags", params)
            # Each tag with its box, as the brief asks: a tag with no spot never reaches the
            # vendor (#1650).
            spot = {"page": 2, "x0": 10, "y0": 10, "x1": 30, "y1": 16}
            table = {"tags": [
                {"page": 2, "side": "architect", "item": "Area 1: top", "tag": "ST-1", "box": spot},
                {"page": 2, "side": "vendor", "item": "Area 1: top", "tag": "ST-2", "box": spot},
            ]}  # fmt: skip
            return reply(text(self._json(table)), model=SONNET)
        if brief.startswith("PROVE:"):
            self._seen("prove", params)
            ids = [line.split()[1] for line in brief.splitlines() if line.startswith("- F")]
            proofs = [
                {"finding_id": i, "vendor_dim": None, "architect_dim": None, "vendor_box": None,
                 "arch_box": None, "outcome": "cannot_prove", "why": "no label"}
                for i in ids
            ]  # fmt: skip
            return reply(text(self._json({"proofs": proofs})), model=SONNET)
        drawn = brief.startswith("OBJECTIVE: the drawn things")
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        self._seen("drawn" if drawn else "numbers", params)
        page = pages[0]
        if step == 0 and not drawn:
            # One top on each of its pages: one pass per kind (#1805), so no gap closer re-reads
            # a page this helper compared.
            found = [
                {
                    "page": p,
                    "item": f"Top on page {p}",
                    "category": "countertop",
                    "status": "different",
                    "verified": False,
                    "source": "architect",
                    "vendor_dim": "D1",
                    "architect_dim": "D2",
                }
                for p in pages
                if p != self.unpaired
            ]
            covered = {"pages": [
                {"page": p, "status": "not_comparable", "note": "no architect drawing in the set"}
                if p == self.unpaired else {"page": p, "status": "different"}
                for p in pages
            ]}  # fmt: skip
            return reply(
                *(tool(f"f{f['page']}", "report_finding", f) for f in found),
                tool(f"c{page}", "set_coverage", covered),
                stop="tool_use",
                model=SONNET,
            )
        if step == 0 and drawn:
            item = {"page": page, "item": f"Unit p{page}: fronts", "category": "cabinet",
                    "architect": "pull-out drawers (note)", "vendor": "2 doors drawn",
                    "status": "different", "verified": False, "source": "architect",
                    "arch_box": _box(page, 500), "vendor_box": _box(page, 500)}  # fmt: skip
            return reply(tool(f"d{page}", "report_finding", item), stop="tool_use", model=SONNET)
        fields = ("notes 1/1; counts 1/1; hardware 0/0" if drawn
                  else "architect 1/1; notes 0/0; strings 0/0")  # fmt: skip
        lines = "\n".join(f"CHECKLIST p{p}: {fields}; not compared: none" for p in pages)
        return reply(text(f"Done.\n{lines}"), model=SONNET)


def _roster(
    tmp_path: Path, script: _Roster, *, price: int = 5 * DOLLARS, mode: str = "ai_pair_plus_code"
) -> Harness:
    areas = 2
    pages = 2 + areas * 2
    workspace_with_pdf(tmp_path, pages=pages)
    provider = ScriptedProvider(lead=[], helper_answer=script)
    harness = Harness(tmp_path, provider, question=FULL_CHECK_INSTRUCTION, message_cap=price)
    engine = harness.engine
    folder = _pack(tmp_path, _set_map(areas))
    (folder / "dims.json").write_text(json.dumps(_dims(list(range(1, pages + 1)))))
    engine.tools.workspace = dataclasses.replace(harness.workspace, set_pack=folder)
    engine.book.workspace = engine.tools.workspace  # the labels are read from the pack
    engine.tools.results = lambda page: RECORDS
    engine.tools.uploads = [{"name": FILE, "pages": pages, "kind": "the vendor's shop drawings"}]
    engine.book.page_count = pages
    engine.settings = dataclasses.replace(
        engine.settings,
        mode="full_check",
        products=("countertop",),
        drawn_helpers=True,
        roster=True,
        pairing_proof=mode,
    )
    return harness


def _clean_ink(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tiny test PDF prints no ink at the labels' spots: the ink check is its own test."""
    monkeypatch.setattr(full_check_roster._Run, "_ink", lambda self, label: (40, 0.0))
    monkeypatch.setattr(
        full_check_roster._Run,
        "_extent",
        lambda self, vendor, architect, page_map=None: ("same_extent", "the ends land together"),
    )


def _findings(outcome: Any) -> list[dict[str, Any]]:
    return [block["finding"] for block in outcome.blocks if block["type"] == "finding"]


def test_a_roster_full_check_runs_its_specialists_with_no_lead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)

    outcome = harness.run()

    provider = harness.provider
    assert provider.lead_calls == []  # no lead: code plans, specialists work, code finishes
    assert script.calls["numbers"] >= 2 and script.calls["drawn"] >= 2
    assert script.calls["tags"] == 1
    # The tag reader replies in its shape; the critic never runs (the advocate replaces it).
    tags = script.params["tags"][0]
    assert tags["output_config"]["format"]["schema"]["required"] == ["tags"]
    assert {t["name"] for t in tags["tools"]} == {"bash_tool", "view", "zoom"}
    assert "critic" not in script.calls
    findings = _findings(outcome)
    tops = [f for f in findings if f["item"].startswith("Top on page")]
    assert tops and all(f["vendor"] == '46"' and f["architect"] == '37"' for f in tops)
    # Both values are code's labels. On a round-2 page (3, 5) the advocate's blind pick agrees,
    # so the pairing is proved; on a round-1 page code's newest-round check keeps it grey first
    # (no AI call is spent on it).
    newest = [f for f in tops if f["page"] in (3, 5)]
    older = [f for f in tops if f["page"] in (2, 4)]
    assert newest and older
    assert script.calls["advocate"] == len(newest)
    for finding in newest:
        assert finding["critic"]["result"] == "passed", finding["critic"]
        assert finding["checks"]["critic"] == "passed"
        assert finding["trust_level"] == "confirmed"
        assert finding["pairing"]["architect"] == f"p{finding['page']}.D2"
    for finding in older:
        assert finding["critic"]["result"] == "objects"
        assert "round 2 is the vendor's current drawing" in finding["critic"]["note"]
        assert finding["trust_level"] == "to_check"
    advocate = script.params["advocate"][0]
    blocks = advocate["messages"][0]["content"]
    question = blocks[-1]["text"]
    # Blind: the question names the vendor's label only; the architect's crop numbers every label.
    assert "D1" in question and "D2" not in question and '37"' not in question
    assert sum(1 for b in blocks if b["type"] == "image") == 2
    listed = [b["text"] for b in blocks if b["type"] == "text" and "numbered:" in b["text"]]
    assert any("D2" in words for words in listed)  # every label of the architect's drawing
    assert advocate["output_config"]["format"]["schema"]["required"][0] == "architect_pick"
    # A judgement: adaptive thinking at a lower effort, never thinking off (#1775, C4).
    assert advocate["thinking"]["type"] == "adaptive"
    assert advocate["output_config"]["effort"] == "medium"
    # The tag table compared by code: ST-1 vs ST-2.
    assert any(f["item"] == "Area 1: top: tag" and f["status"] == "different" for f in findings)
    # The summary: code's words (the reworded one dropped a number) and the fixed price.
    assert "Fixed price $5: this check used $" in outcome.text
    assert "Too short." not in outcome.text
    assert outcome.stopped is None
    assert outcome.api_content[-1]["role"] == "assistant"


def test_the_divider_pages_code_skips_are_shown_on_the_coverage_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Code records the pages it leaves out (the two dividers) and shows the map at once, before
    any helper records (the coverage map is read under the engine's lock, #1612)."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster())

    with caplog.at_level("WARNING", logger="app.review.assistant.full_check_roster"):
        outcome = harness.run()

    assert "could not record the divider pages" not in caplog.text

    shown = [b["coverage"] for b in outcome.blocks if b["type"] == "coverage"]
    assert len(shown) == 1
    by_code = {e["page"]: e for e in shown[0]["pages"] if e["by"] == "code"}
    assert set(by_code) == {1, 6}
    assert all(entry["status"] == "not_comparable" for entry in by_code.values())
    sent = [data["coverage"] for event, data in harness.events if event == "coverage"]
    # The first map sent is code's alone: the dividers, before any helper has recorded.
    assert {(e["page"], e["by"]) for e in sent[0]["pages"] if e["by"]} == {(1, "code"), (6, "code")}


def test_a_blind_pick_that_differs_keeps_the_item_grey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster(advocate_pick="D7")
    outcome = _roster(tmp_path, script).run()

    tops = [f for f in _findings(outcome) if f["item"].startswith("Top on page")]
    newest = [f for f in tops if f["page"] in (3, 5)]
    assert newest
    for finding in newest:
        assert finding["critic"]["result"] == "objects"
        assert "a blind second pick chose p" in finding["critic"]["note"]
        assert finding["trust_level"] == "to_check"  # so the red/green drawing lists it in grey


def test_code_only_mode_never_clouds_an_ai_pairing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    outcome = _roster(tmp_path, script, mode="code_only").run()

    assert "advocate" not in script.calls  # no AI pick is asked for: it could not prove anything
    tops = [f for f in _findings(outcome) if f["item"].startswith("Top on page")]
    assert tops and all(f["critic"]["result"] == "objects" for f in tops)
    newest = [f for f in tops if f["page"] in (3, 5)]
    assert newest and all("code_only" in f["critic"]["note"] for f in newest)


def test_a_price_too_small_lists_what_was_not_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script, price=DOLLARS // 50)  # 2 cents

    outcome = harness.run()

    assert harness.provider.lead_calls == []
    assert outcome.cost_micros <= DOLLARS // 50
    assert "Fixed price $0.02" in outcome.text
    assert outcome.stopped == "cost_cap"
    assert "Not done within the fixed price (listed, not dropped)" in outcome.text


def test_the_verifiers_proofs_are_applied_by_code(tmp_path: Path) -> None:
    script = _Roster()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    finding, _notes = engine.book.report(
        {
            "page": 2,
            "item": "Top on page 2",
            "category": "countertop",
            "architect": '37"',
            "vendor": '37 1/2"',
            "status": "different",
            "verified": False,
            "source": "architect",
            "arch_box": _box(2, 100),
            "vendor_box": _box(2, 300),
        },
        "helper:1",
    )
    other, _notes = engine.book.report(
        {**{k: finding[k] for k in ("page", "category", "architect", "status", "source")},
         "item": "Top two", "vendor": '40"', "verified": False}, "helper:1",
    )  # fmt: skip
    proofs = {"proofs": [
        {"finding_id": finding["id"], "vendor_dim": "D2", "architect_dim": "D2",
         "vendor_box": None, "arch_box": None, "outcome": "inputs_found", "why": ""},
        {"finding_id": other["id"], "vendor_dim": None, "architect_dim": None,
         "vendor_box": None, "arch_box": None, "outcome": "cannot_prove", "why": "no value"},
    ]}  # fmt: skip
    done = apply_proofs(engine, json.dumps(proofs), [finding["id"], other["id"], "F99"])
    assert done[finding["id"]].startswith("Right")
    updated = next(f for f in engine.book.live() if f["id"] == finding["id"])
    assert updated["status"] == "same" and updated["vendor"] == '37"'
    assert done[other["id"]] == "cannot prove: no value"
    assert apply_proofs(engine, "not json", ["F1"]) == {
        "F1": "no proof: the verifier's reply was not readable"
    }
    # #1811: a fenced proof is read (the item already proved stays proved, the other is read).
    fenced = apply_proofs(engine, f"Proofs:\n```json\n{json.dumps(proofs)}\n```", [other["id"]])
    assert fenced == {other["id"]: "cannot prove: no value"}


def test_codes_own_pairing_is_proof_and_a_different_one_keeps_it_grey(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert code_pairs(
        [
            {"vendor": 'p3.D1 46"', "arch": 'p3.D2 37"', "status": "different"},
            {"vendor": "p5.D1", "architect": "p5.D7", "status": "same"},
            {"vendor": "p4.D1", "architect": "p4.D2", "status": "cannot_pair"},
        ]
    ) == {"p3.D1": "p3.D2", "p5.D1": "p5.D7"}
    _clean_ink(monkeypatch)
    script = _Roster(advocate_pick="D9")  # the AI pick would object: code's pairing decides
    harness = _roster(tmp_path, script)
    folder = harness.engine.tools.workspace.set_pack
    assert folder is not None
    (folder / CODE_PAIRS_FILE).write_text(
        json.dumps(
            [
                {"vendor": "p3.D1", "architect": "p3.D2", "status": "different"},
                {"vendor": "p5.D1", "architect": "p5.D7", "status": "different"},
            ]
        )
    )

    outcome = harness.run()

    tops = {f["page"]: f for f in _findings(outcome) if f["item"].startswith("Top on page")}
    assert tops[3]["critic"]["result"] == "passed"
    assert tops[3]["critic"]["answers"]["pairing"] == "code"
    assert tops[5]["critic"]["result"] == "objects"
    assert "code pairs p5.D1 with p5.D7" in tops[5]["critic"]["note"]
    assert "advocate" not in script.calls  # neither needed an AI pick


def test_a_typed_value_at_a_labelled_spot_stands_for_its_label(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    run = full_check_roster._Run(harness.engine)
    finding = {
        "page": 3,
        "vendor": '46"',
        "architect": "3'-1\"",
        "vendor_box": {"page": 3, "x0": 290, "y0": 90, "x1": 370, "y1": 120},
        "arch_box": {"page": 3, "x0": 90, "y0": 90, "x1": 170, "y1": 120},
    }
    assert run._label_at(finding, "vendor_box", "vendor")["id"] == "D1"  # type: ignore[index]
    assert run._label_at(finding, "arch_box", "architect")["id"] == "D2"  # type: ignore[index]
    # Another value at the spot, or no box: no label stands for it.
    assert run._label_at({**finding, "vendor": '45"'}, "vendor_box", "vendor") is None
    assert run._label_at({**finding, "vendor_box": None}, "vendor_box", "vendor") is None


# ---- the escalation's one deeper look (#1619) ------------------------------------------------------


def _typed_wrong(harness: Harness) -> str:
    """A Wrong a helper typed with its boxes, at code's labelled spots, that no code check has
    read yet (so the devil's advocate passed it by in P4): a question to the vendor today."""
    engine = harness.engine
    finding, _notes = engine.book.report(
        {
            "page": 3,
            "item": "Top on page 3",
            "category": "countertop",
            "architect": '37"',
            "vendor": '46"',
            "status": "different",
            "verified": False,
            "source": "architect",
            "arch_box": _box(3, 100),
            "vendor_box": _box(3, 300),
        },
        "helper:1",
    )
    typed = {
        **finding,
        "checks": {"printed_architect": "not_found", "printed_vendor": "not_found"},
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
        "trust_level": "to_check",
        "witnesses": [],
    }
    assert engine.book.replace(typed)
    return str(finding["id"])


def _run(harness: Harness) -> Any:
    run = full_check_roster._Run(harness.engine)
    run.plan = PricePlan.balanced(harness.engine.settings.message_cap_micros, 2)
    return run


def test_the_proving_tools_read_both_values_of_every_red_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1805: before the devil's advocate, code calls verify_value (and read_number where it
    cannot read) on both values of every red candidate; the answers are witnesses."""
    from app.review.assistant import witness

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    finding_id = _typed_wrong(harness)
    asked: list[tuple[str, int, str, str]] = []

    def verify(
        self: Any, name: str, page: int, box: Any, expected: str, side: str = "vendor"
    ) -> Any:
        asked.append((name, page, expected, side))
        if side == "vendor":  # pen strokes: verify_value could not, read_number read it
            return {"result": "match", "route": "drawn", "read": expected, "reads": [],
                    "reason": "", "value": expected, "pending": False, "words": "read",
                    "read_number": "ocr_shape", "read_again": True}  # fmt: skip
        return {"result": "match", "route": "drawn", "read": expected, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": "read"}  # fmt: skip

    from app.review.assistant.chat_tools import ChatTools

    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    heard: list[Mapping[str, Any]] = []
    engine.tools.on_proof = heard.append
    run = _run(harness)
    with engine.timings.phase("provers"):
        counts = run._prove_values("verifier")

    assert sorted(side for *_rest, side in asked) == ["architect", "vendor"]
    assert counts == {"verify_value": 2, "read_number": 1}
    assert run.tool_calls["verifier"] == counts
    found = next(f for f in engine.book.live() if f["id"] == finding_id)
    assert found["checks"]["verify_vendor"]["result"] == "match"
    assert "read_again" not in found["checks"]["verify_vendor"]
    assert "ocr_ink" in witness.witnesses(found["checks"], found["status"])
    assert [e["side"] for e in found["proof_tools"]] == ["architect", "vendor"]
    assert found["proof_tools"][1]["tool"] == "read_number"
    assert len(heard) == 2
    row = next(r for r in engine.timings.as_dict(0)["phases"] if r["phase"] == "provers")
    assert row["tools"] == {"read_number": 1, "verify_value": 2}


def test_a_proving_tool_never_reads_over_what_code_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant.chat_tools import ChatTools

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    finding_id = _typed_wrong(harness)
    current = next(f for f in engine.book.live() if f["id"] == finding_id)
    printed_elsewhere = {"result": "mismatch", "route": "text", "read": '45"', "value": '46"',
                         "reads": [], "reason": "", "pending": False, "words": "45 here"}  # fmt: skip
    assert engine.book.replace(
        {**current, "checks": {**current["checks"], "verify_vendor": printed_elsewhere}}
    )
    monkeypatch.setattr(ChatTools, "verify_answer", lambda *a, **k: {
        "result": "match", "route": "drawn", "read": '46"', "value": '46"', "reads": [],
        "reason": "", "pending": False, "words": "read",
    })  # fmt: skip
    _run(harness)._prove_values("verifier")
    found = next(f for f in engine.book.live() if f["id"] == finding_id)
    assert found["checks"]["verify_vendor"]["result"] == "mismatch"  # kept: never red
    assert found["checks"]["verify_architect"]["result"] == "match"
    assert "read_number" in full_check_roster._PROVER_TOOLS


def _five_wrongs(harness: Harness) -> None:
    """Five Wrongs a helper typed at their spots, no code check read yet, on pages 3 to 5."""
    engine = harness.engine
    for n in range(5):
        finding, _notes = engine.book.report(
            {"page": 3 + n % 3, "item": f"Top {n}", "category": "countertop",
             "architect": f"{30 + n}\"", "vendor": f"{40 + 2 * n}\"", "status": "different",
             "verified": False, "source": "architect", "arch_box": _box(3 + n % 3, 100 + n),
             "vendor_box": _box(3 + n % 3, 300 + n)},
            "helper:1",
        )  # fmt: skip
        assert engine.book.replace(
            {**finding, "checks": {"printed_architect": "not_found",
             "printed_vendor": "not_found"}, "trust_level": "to_check", "witnesses": []}
        )  # fmt: skip


def _proved_on_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, workers: str
) -> tuple[list[tuple[Any, ...]], list[dict[str, Any]], dict[str, int]]:
    """Five red candidates proved with `workers` threads; the slow reads are the first ones and
    one side's tool raises: what the book and the proof listener hold afterwards."""
    import time

    from app.review.assistant.chat_tools import ChatTools

    monkeypatch.setenv("GV_ASSISTANT_CODE_WORKERS", workers)
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _five_wrongs(harness)

    def verify(
        self: Any, name: str, page: int, box: Any, expected: str, side: str = "vendor"
    ) -> Any:
        inches = int(expected.rstrip('"'))
        time.sleep(0.01 * (50 - inches) / 4)  # the first items asked end last
        if expected == '44"':
            raise RuntimeError("this spot could not be rendered")
        result = "match" if side == "architect" else "cannot_read"
        return {"result": result, "route": "drawn", "read": expected, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": f"read {page}",
                "read_again": side == "vendor"}  # fmt: skip

    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    heard: list[tuple[Any, ...]] = []
    engine.tools.on_proof = lambda proof: heard.append(
        (proof["page"], proof["claim"], proof["side"], proof["answer"]["result"])
    )
    run = _run(harness)
    with engine.timings.phase("provers"):
        counts = run._prove_values("verifier")
    counts = {**counts, "failed_steps": len(run.failed_steps)}
    found = [
        {k: f.get(k) for k in ("id", "checks", "proof_tools", "reader_error")}
        for f in sorted(engine.book.live(), key=lambda f: str(f["id"]))
    ]
    return heard, found, counts


def test_the_proving_tools_on_the_pool_give_what_one_at_a_time_gave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1850: the reads run on a few threads, finishing in any order; every finding gets the
    same answers (run after run), the listener hears them in the same order, and an item whose
    reader raised is Not compared with the reason, never a stop for the others."""
    one = _proved_on_pool(tmp_path / "one", monkeypatch, "1")
    pool = _proved_on_pool(tmp_path / "pool", monkeypatch, "3")
    again = _proved_on_pool(tmp_path / "again", monkeypatch, "3")
    assert pool == one == again
    heard, found, counts = pool
    # One read that raised is not a broken step: the check stays complete.
    assert counts == {"verify_value": 10, "read_number": 4, "failed_steps": 0}
    assert len(heard) == 9  # the side whose tool raised is not heard
    assert sum(len(f["proof_tools"] or []) for f in found) == 10
    # The item whose read raised is Not compared, with the reason; the others are not touched.
    failed = [e for f in found for e in f["proof_tools"] or [] if e.get("failed")]
    assert [(e["side"], e["value"], e["result"]) for e in failed] == [
        ("vendor", '44"', "cannot_read")
    ]
    marked = [f for f in found if f["reader_error"]]
    assert len(marked) == 1 and marked[0]["proof_tools"][-1].get("failed")


def test_stop_ends_the_proving_reads_on_the_pool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stop during the first read: the item being read ends its read and is kept, as before;
    no item after it starts."""
    from app.review.assistant.chat_tools import ChatTools

    monkeypatch.setenv("GV_ASSISTANT_CODE_WORKERS", "2")
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _five_wrongs(harness)
    asked: list[str] = []

    def verify(
        self: Any, name: str, page: int, box: Any, expected: str, side: str = "vendor"
    ) -> Any:
        asked.append(expected)
        engine.cancel.set()  # the reviewer pressed Stop
        return {"result": "match", "route": "drawn", "read": expected, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": "read"}  # fmt: skip

    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    counts = _run(harness)._prove_values("verifier")
    assert len(asked) <= 4  # at most the two items already running, both sides each
    assert counts["verify_value"] <= len(asked)
    kept = [f for f in engine.book.live() if f.get("proof_tools")]
    assert 1 <= len(kept) <= 2


def test_the_escalation_reads_the_labels_then_gives_one_deeper_look_within_the_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.review.assistant import proof

    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    finding_id = _typed_wrong(harness)
    run = _run(harness)

    run._escalate(SimpleNamespace(jobs=[]), None)

    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    # The free step read both values at code's labels; then the advocate's one blind pick agreed.
    assert found["checks"]["printed_vendor"] == "match" and found["dims"]["vendor"]["id"] == "D1"
    assert script.calls.get("advocate") == 1
    assert found["critic"]["result"] == "passed" and found["pairing"]["architect"] == "p3.D2"
    assert proof.proof_group(found) == "clouded"
    assert run.spent.get("escalation", 0) > 0 and "advocate" not in run.spent
    # One look only: a second escalation asks nothing again.
    run._escalate(SimpleNamespace(jobs=[]), None)
    assert script.calls.get("advocate") == 1


def test_the_escalation_spends_only_what_is_left_under_the_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from app.review.assistant import proof

    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    finding_id = _typed_wrong(harness)
    run = _run(harness)
    monkeypatch.setattr(full_check_roster._Run, "_left", lambda self: 0)

    run._escalate(SimpleNamespace(jobs=[]), None)

    found = next(f for f in harness.engine.book.live() if f["id"] == finding_id)
    assert "advocate" not in script.calls  # no money left: no AI call
    assert found["checks"]["printed_vendor"] == "match"  # the free step still ran
    assert proof.proof_group(found) == "not_compared"  # still not compared, never red
    # Said on the item (#1641): the check it needed was not reached within the fixed price.
    assert found["critic"]["note"].startswith("Not verified within the fixed price")
    assert str(proof.classified(found).why).startswith("not verified within the fixed price")
    assert any("not verified" in line for line in run.not_done)


# ---- every page accounted for within the fixed price (#1634) ------------------------------------


def test_pages_a_helper_left_are_found_from_its_coverage() -> None:
    coverage = {
        "pages": [
            {"page": 2, "status": "different", "by": "helper:1"},
            {"page": 3, "status": "not_opened", "by": "helper:1", "note": "not reached"},
            {"page": 4, "status": "not_comparable", "by": "helper:2", "note": "Not reached"},
            {"page": 5, "status": "not_comparable", "by": "helper:2", "note": "brand prototype"},
            {"page": 6, "status": "not_comparable", "by": "code", "note": "divider page"},
        ]
    }
    left = full_check_roster.unfinished_pages([2, 3, 4, 5, 7], coverage)
    assert left == {3: "not reached", 4: "Not reached", 7: "no helper set its coverage"}


def test_the_gap_closers_take_what_the_price_reaches_records_first() -> None:
    pages = {page: ["x"] for page in (5, 9, 12, 20)}
    room = 3 * full_check_roster.MIN_GAP_PAGE_MICROS
    assert full_check_roster.gap_pages(pages, room, has_record={20}) == ([5, 9, 20], [12])
    assert full_check_roster.page_runs([3, 4, 5, 9, 12], 2) == [[3, 4, 5], [9, 12]]
    assert full_check_roster.page_runs([3], 10) == [[3]]
    assert full_check_roster.page_ranges_words([4, 5, 6]) == "pages 4-6"
    assert full_check_roster.page_ranges_words([4, 9]) == "pages 4 and 9"


def test_the_gap_closers_get_what_p1_left_less_the_later_phases(tmp_path: Path) -> None:
    harness = _roster(tmp_path, _Roster())
    run = _run(harness)
    assert run.plan is not None
    later = sum(run.plan.budgets[n] for n in full_check_roster.PROTECTED)
    assert later == run.plan.protected()
    # Nothing spent yet: all but the later phases' money, far more than the gap share alone.
    assert run._gap_budget() == 5 * DOLLARS - later > run.plan.budgets["gaps"]
    # P1 used it all: nothing for the gap closers, the protected money stays (#1641).
    harness.engine._spent = 5 * DOLLARS - later
    assert run._gap_budget() == 0


def test_a_gap_closer_never_re_does_a_page_its_kind_compared(tmp_path: Path) -> None:
    """#1805: one pass per kind. A numbers gap closer never takes a page a numbers helper compared,
    whatever its checklist (or a drawn helper's) left open: that waits for the lead review."""
    from types import SimpleNamespace

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    run = _run(harness)
    run.agents = [
        SimpleNamespace(number=1, lens="numbers"),
        SimpleNamespace(number=2, lens="drawn"),
    ]
    engine._checklist_gaps = {
        1: {2: "no CHECKLIST line", 4: "1 of 3 vendor dimension labels not handled: D5"},
        2: {3: "notes not all done, and 'not compared' says none", 5: "no CHECKLIST line"},
    }
    asked: list[Any] = []

    def launch(agents: Any, on_done: Any = None) -> list[dict[str, Any]]:
        asked.extend(agents)
        return [{"report": "", "stopped": None} for _ in agents]

    engine._launch = launch  # type: ignore[method-assign]
    engine._plan = None
    run._gap_closers(None, "", False)

    assert asked == []
    assert run.repeats_avoided == [2, 3, 4, 5]
    # A page the helper never reached is taken, with its open lines named.
    engine.book.set_coverage(
        {"pages": [{"page": p, "status": "same"} for p in (2, 3, 5)]}, "helper:1"
    )
    engine._plan = SimpleNamespace(jobs=[SimpleNamespace(pages=(2, 3, 4, 5))])
    run._gap_closers(None, "", False)
    assert [agent.pages for agent in asked] == [(4,)]
    assert "left open by the numbers helper" in asked[0].prompt and "D5" in asked[0].prompt
    assert run.repeats_avoided == [2, 3, 5]


def test_a_note_about_the_drawing_is_not_a_page_left_unread() -> None:
    coverage = {
        "pages": [
            {"page": 2, "status": "different", "by": "helper:1",
             "note": "vendor did not dimension the toe kick"},
            {"page": 3, "status": "different", "by": "helper:1", "note": "partly compared"},
            {"page": 4, "status": "different", "by": "helper:1", "note": "did not reach the end"},
        ]
    }  # fmt: skip
    assert sorted(full_check_roster.unfinished_pages([2, 3, 4], coverage)) == [3, 4]
    # The helper's own work said partly done, in other words, still counts (review of #1812).
    for note in ("did not check the vendor strings", "partly; right half pending",
                 "only the vendor elevation compared", "Compared partially",
                 "the architect's plan does not show it, and I did not look at the section",
                 "the sections did not get compared", "it did not get compared",
                 "which did not fit in my turns", "drawing did not load, not compared",
                 "the vendor's elevation did not load", "the plan has not been read"):  # fmt: skip
        entry = {"page": 2, "status": "same", "by": "helper:1", "note": note}
        assert full_check_roster.unfinished_pages([2], {"pages": [entry]}) == {2: note}, note


def test_open_lines_the_lead_review_did_not_take_are_said_so(tmp_path: Path) -> None:
    """Review of #1812: a compared page's open checklist lines no gap closer re-reads wait for the
    lead review; the ones it did not take are named on the coverage and in what was not done."""
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    run = _run(harness)
    engine._checklist_gaps = {1: {2: "1 of 3 vendor dimension labels not handled: D5", 4: "x"}}
    engine.book.set_coverage({"pages": [{"page": p, "status": "same"} for p in (2, 4)]}, "helper:1")
    run.repeats_avoided = [2, 4]
    run.lead_taken = {4}
    run._note_left_open()
    assert any("p2" in line or "page 2" in line for line in run.not_done), run.not_done
    assert not any("4" in line for line in run.not_done)
    notes = {e["page"]: e.get("note") for e in engine.book.coverage_map()["pages"]}
    assert "D5" in str(notes.get(2)) and "not compared again" in str(notes.get(2))


def test_a_step_stopped_at_its_share_counts_as_a_money_stop_once_the_price_is_used_up(
    tmp_path: Path,
) -> None:
    run = _run(_roster(tmp_path, _Roster()))
    run.plan = full_check_roster.PricePlan.balanced(5 * DOLLARS, 2, 6)
    run.started_at = run.engine.spent
    run._capped("Helper 1")
    assert run.capped_early and not run.stopped_for_money
    assert not run._stopped_for_money()  # the later steps' money is still there
    # The later steps used all they could: only the summary's and second reads' shares are left.
    run.engine._spent += run.plan.price - run.plan.after("escalation")
    assert run._stopped_for_money()


class _Lazy(_Roster):
    """A numbers helper that says its last page was not reached, with money left, twice."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            self._seen("gap", params)
            return reply(text("Closed."), model=SONNET)
        drawn = brief.startswith("OBJECTIVE: the drawn things")
        if drawn or brief.startswith(("TAGS:", "PROVE:")):
            return super().helper(params, brief)
        self._seen("numbers", params)
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        lines = [f"CHECKLIST p{pages[0]}: architect 1/1; strings 0/0; not compared: none"]
        lines += [f"CHECKLIST p{p}: architect 0/1; strings 0/0; not compared: not reached"
                  for p in pages[1:]]  # fmt: skip
        return reply(text("Stopped.\n" + "\n".join(lines)), model=SONNET)


def test_a_helper_that_stops_short_is_sent_on_then_a_gap_closer_finishes_its_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Lazy()
    harness = _roster(tmp_path, script)

    harness.run()

    numbers = script.params["numbers"]
    # The roster's per-call allowance (its money is held per call at that bound), with room for
    # thinking and a dozen tool calls (#1817).
    assert {p["max_tokens"] for p in numbers} == {ROSTER_HELPER_MAX_TOKENS}
    # Each numbers helper stopped short with money left: sent on once to its unread pages.
    sent_on = [p for p in numbers if "You still have money and tool calls left" in json.dumps(p)]
    assert len(sent_on) == 2  # one call each for the two numbers helpers, then never again
    # Still not compared: a gap closer takes those pages with the money P1 left.
    assert script.calls.get("gap", 0) >= 1
    gap = script.params["gap"][0]
    assert "page not compared yet" in _first_text(gap)


class _Cut(_Roster):
    """A numbers helper whose first reply is cut at its length limit."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if brief.startswith(("OBJECTIVE: the drawn things", "TAGS:", "PROVE:")):
            return super().helper(params, brief)
        if brief.startswith(full_check_roster.GAP_SCHEMA_NOTE):
            return reply(text("Closed."), model=SONNET)
        self._seen("numbers", params)
        if _steps(params) == 0:
            return reply(text("Page 2: the vendor's top is"), stop="max_tokens", model=SONNET)
        return super().helper(params, brief)


def test_a_reply_cut_at_the_smaller_allowance_is_sent_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Cut()
    _roster(tmp_path, script).run()
    later = [p for p in script.params["numbers"] if _steps(p) > 0]
    assert later and all("cut at its length limit" in json.dumps(p) for p in later)


def test_blind_picks_started_together_hold_their_price_within_the_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The advocate's six workers: each blind pick holds its price while its call runs, so picks
    started together never pass the phase's budget between them (#1775, C4)."""
    import time
    from types import SimpleNamespace

    made: list[str] = []
    lock = threading.Lock()

    def held_pick(self: Any, finding: Any, *_rest: Any) -> dict[str, Any]:
        with lock:
            made.append(str(finding["id"]))
        time.sleep(0.05)  # the other workers ask while this one runs
        with lock:
            self._charge(self.pick_phase, 50_000)
            self.engine.spent += 50_000  # a call is charged only when it ends
        return {"pick": "none"}

    monkeypatch.setattr(full_check_roster._Run, "_held_pick", held_pick)
    engine = SimpleNamespace(
        _price=lambda *a: 50_000,
        _fits=lambda estimate: True,
        settings=SimpleNamespace(message_cap_micros=10_000_000),
        spent=0,
    )
    run = full_check_roster._Run(engine=engine)  # type: ignore[arg-type]
    # The price is the phase's budget, nothing kept for later steps (#1805: what a phase leaves
    # rolls forward, so the price, not only the share, is what the picks must not pass).
    run.plan = SimpleNamespace(  # type: ignore[assignment]
        budgets={"advocate": 120_000}, price=120_000, after=lambda phase: 0
    )
    with ThreadPoolExecutor(max_workers=6) as pool:
        replies = list(pool.map(lambda n: run._blind_pick({"id": f"F{n}"}, {}, {}, None), range(6)))
    assert len(made) == 2 and run.spent["advocate"] <= 120_000 and run.held == 0
    assert replies.count(None) == 4


# ---- answers wrapped in a code block; the not-for-sending rule (#1811) --------------------------


def _package_state(outcome: Any) -> dict[str, Any]:
    block = next((b for b in outcome.blocks if b.get("type") == "package"), None)
    return dict((block or {}).get("package", {}).get("state") or {})


def test_answers_in_a_code_block_give_the_same_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through a route without structured outputs every JSON answer comes after a sentence in a
    ```json block: the tag table, the advocate's picks and the proofs are read, and the check
    comes out exactly as with bare answers (no item more or less red)."""
    _clean_ink(monkeypatch)
    results = []
    for n, fenced in enumerate((False, True)):
        folder = tmp_path / str(n)
        folder.mkdir()
        harness = _roster(folder, _Roster(fenced=fenced))
        outcome = harness.run()
        results.append(
            sorted(
                (f["item"], f["status"], (f.get("critic") or {}).get("result"))
                for f in _findings(outcome)
            )
        )
    bare, fenced_run = results
    assert fenced_run == bare
    assert ("Area 1: top: tag", "different", None) in [r[:2] + (None,) for r in fenced_run]
    assert any(r[2] == "passed" for r in fenced_run)  # the advocate's fenced picks were read


def test_a_vendor_page_not_compared_is_never_send_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    outcome = _roster(tmp_path, _Roster(unpaired=3)).run()
    assert "send the cloud drawing" not in outcome.text
    assert "send the wrong items" not in outcome.text
    assert full_check_roster.NOT_SENDABLE_NEXT in outcome.text
    assert "Check incomplete: 1 vendor page was not compared (p3). Not for sending" in outcome.text
    state = _package_state(outcome)
    assert not state or state.get("incomplete") == "1 vendor page was not compared (p3)"


def test_a_whole_roster_check_with_wrongs_still_says_send_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    outcome = _roster(tmp_path, _Roster()).run()
    assert "Next: send the cloud drawing to the vendor." in outcome.text.splitlines()
    assert "Check incomplete" not in outcome.text
    assert full_check_roster.NOT_SENDABLE_NEXT not in outcome.text


def test_a_helper_that_errors_makes_the_check_not_for_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    _clean_ink(monkeypatch)
    run = full_check_roster._Run(engine=None)  # type: ignore[arg-type]
    agents = [SimpleNamespace(description="Tags"), SimpleNamespace(description="Numbers p2-3")]
    run.engine = SimpleNamespace(  # type: ignore[assignment]
        _launch=lambda given, **_k: [{"stopped": None}, {"stopped": "error"}]
    )
    run._launched(agents)
    assert run._not_for_sending() == "a step did not finish: Numbers p2-3 ended with an error"
    run._apply_tags('```json\n{"tags": [\n')  # cut short: its rows were never compared
    assert "the tag table could not be read" in str(run._not_for_sending())


class _SilentHelper(_Roster):
    """The numbers helper of pages 2-3 records its findings, then never writes its report."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if (
            _steps(params) > 0
            and brief.startswith("OBJECTIVE:")
            and not brief.startswith("OBJECTIVE: the drawn things")
            and "on pages 2" in brief
        ):
            self._seen("silent", params)
            return reply(text(""), model=SONNET)
        return super().helper(params, brief)


def test_a_helper_with_no_report_is_logged_and_its_pages_noted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#1811: a helper that ends with no written report says why in the log, and its pages are
    noted as not compared with that reason (a gap closer then takes them)."""
    _clean_ink(monkeypatch)
    script = _SilentHelper()
    harness = _roster(tmp_path, script)
    noted: list[tuple[int, str]] = []
    original = harness.engine._note_coverage

    def note(page: int, words: str) -> None:
        noted.append((page, words))
        original(page, words)

    monkeypatch.setattr(harness.engine, "_note_coverage", note)
    with caplog.at_level("WARNING"):
        outcome = harness.run()
    assert script.calls.get("silent")
    said = [r.getMessage() for r in caplog.records if "no written report" in r.getMessage()]
    assert said and "stop=end_turn" in said[0] and "turns=" in said[0]
    assert {page for page, words in noted if "did not write its report" in words} == {2, 3}
    coverage = next(b for b in outcome.blocks if b.get("type") == "coverage")["coverage"]
    page = next(p for p in coverage["pages"] if p["page"] == 2)
    assert page["by"] != "helper:1"  # a later look compared the page again


def test_a_wrong_shaped_tag_table_is_a_failed_step() -> None:
    from types import SimpleNamespace

    for report in ('[{"page": 2, "side": "vendor"}]', '{"rows": []}', '{"tags": "none"}'):
        run = full_check_roster._Run(engine=SimpleNamespace())  # type: ignore[arg-type]
        run._apply_tags(report)
        assert run.failed_steps == ["the tag table could not be read"], report
    run = full_check_roster._Run(engine=SimpleNamespace())  # type: ignore[arg-type]
    run._apply_tags('{"tags": []}')  # read, nothing tagged on both sides: not a failure
    assert run.failed_steps == []


def test_a_page_noted_not_reached_is_not_compared_even_when_marked_different(
    tmp_path: Path,
) -> None:
    """#1654's pattern: a page still "different" whose note says it was not reached within the
    price, or that its helper wrote no report, is not compared: never "send it"."""
    from types import SimpleNamespace

    harness = _roster(tmp_path, _Roster())
    book = harness.engine.book
    book.set_coverage(
        {
            "pages": [
                {"page": 1, "status": "same"},
                {"page": 2, "status": "different",
                 "note": "partly compared; not reached within the fixed price"},
                {"page": 3, "status": "same"},
                {"page": 4, "status": "same"},
                {"page": 5, "status": "same"},
                {"page": 6, "status": "same"},
            ]
        },
        "helper:1",
    )  # fmt: skip
    book.note_coverage(3, "not compared in detail: its helper did not write its report")
    harness.engine._plan = SimpleNamespace(skipped=())
    run = full_check_roster._Run(engine=harness.engine)
    assert run._not_for_sending() == "2 vendor pages were not compared (p2, p3)"
    harness.engine._plan = SimpleNamespace(skipped=((2, "divider"), (3, "divider")))
    assert run._not_for_sending() is None


def test_nothing_recorded_is_not_for_sending(tmp_path: Path) -> None:
    from types import SimpleNamespace

    harness = _roster(tmp_path, _Roster())
    harness.engine._plan = SimpleNamespace(skipped=())
    run = full_check_roster._Run(engine=harness.engine)
    assert not harness.engine.book.coverage_used
    assert run._not_for_sending() == "no page's comparison was recorded"


class _SilentTags(_Roster):
    """The tag reader never writes its report."""

    def helper(self, params: Mapping[str, Any], brief: str) -> list[dict[str, Any]]:
        if brief.startswith("TAGS:"):
            self._seen("tags", params)
            return reply(text(""), model=SONNET)
        return super().helper(params, brief)


def test_a_silent_tag_reader_never_notes_pages_it_does_not_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _SilentTags())
    noted: list[int] = []
    original = harness.engine._note_coverage

    def note(page: int, words: str) -> None:
        if "did not write its report" in words:
            noted.append(page)
        original(page, words)

    monkeypatch.setattr(harness.engine, "_note_coverage", note)
    outcome = harness.run()
    assert noted == []
    assert "send the cloud drawing" not in outcome.text  # the tag step broke: not for sending


class _Counted:
    """The live line's item counts as a check sends them (`FullCheckProgress.set_items`)."""

    def __init__(self) -> None:
        self.items: list[tuple[int, int]] = []
        self.ended = 0
        self.phases: list[str] = []

    def set_items(self, done: int, total: int) -> None:
        self.items.append((done, total))

    def end_items(self) -> None:
        self.ended += 1

    def phase(self, name: str) -> None:
        self.phases.append(name)

    def set_step(self, *_a: Any, **_k: Any) -> None:
        return None

    def changed(self, *_a: Any, **_k: Any) -> None:
        return None


def test_the_final_checks_by_code_count_their_items_on_the_live_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-23: the proving reads say "checking n of m items" as they go, and end the count."""
    from app.review.assistant.chat_tools import ChatTools

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _five_wrongs(harness)
    counted = _Counted()
    engine._progress = counted  # type: ignore[assignment]

    def verify(self: Any, name: str, page: int, box: Any, expected: str, side: str = "") -> Any:
        return {"result": "cannot_read", "route": "none", "read": None, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": "not read"}  # fmt: skip

    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    _run(harness)._prove_values("verifier")
    assert counted.items[0] == (0, 5) and counted.items[-1] == (5, 5)
    assert sorted(counted.items) == counted.items and counted.ended == 1


def test_a_side_code_already_read_is_never_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-23: a side whose value code already read (typed text, or ink) keeps that answer and
    a new one is never written over it, so the proving step does not read it again."""
    from app.review.assistant.chat_tools import ChatTools

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _five_wrongs(harness)
    first = engine.book.live()[0]
    read = {"result": "match", "route": "text", "read": first["vendor"], "reads": [],
            "reason": "", "value": first["vendor"], "pending": False}  # fmt: skip
    engine.book.replace({**first, "checks": {**first["checks"], "verify_vendor": read}})
    asked: list[tuple[str, str]] = []

    def verify(self: Any, name: str, page: int, box: Any, expected: str, side: str = "") -> Any:
        asked.append((expected, side))
        return {"result": "cannot_read", "route": "none", "read": None, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": "not read"}  # fmt: skip

    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    _run(harness)._prove_values("verifier")
    assert (first["vendor"], "vendor") not in asked and len(asked) == 9
    kept = next(f for f in engine.book.live() if f["id"] == first["id"])
    assert kept["checks"]["verify_vendor"]["result"] == "match"


def test_stop_ends_the_final_checks_by_code_within_seconds_and_keeps_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-23: Stop pressed while the final checks by code run (each read slow): the reads
    already running end, nothing else is read, the advocate judges nothing more and no further
    code check starts; the stopped answer keeps every finding and says how to continue."""
    import time
    from types import SimpleNamespace

    from app.review.assistant.chat_tools import ChatTools

    monkeypatch.setenv("GV_ASSISTANT_CODE_WORKERS", "2")
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    _five_wrongs(harness)
    asked: list[str] = []

    def verify(self: Any, name: str, page: int, box: Any, expected: str, side: str = "") -> Any:
        asked.append(expected)
        if len(asked) == 2:
            engine.cancel.set()  # the reviewer pressed Stop while code reads
        time.sleep(0.3)
        return {"result": "cannot_read", "route": "none", "read": None, "reads": [],
                "reason": "", "value": expected, "pending": False, "words": "not read"}  # fmt: skip

    judged: list[str] = []
    monkeypatch.setattr(ChatTools, "verify_answer", verify)
    monkeypatch.setattr(
        full_check_roster._Run, "_judge", lambda self, f, *_a: judged.append(f["id"])
    )
    checks: list[str] = []
    monkeypatch.setattr(engine, "_ledger_findings", lambda: checks.append("ledger"))
    run = _run(harness)
    before = {f["id"] for f in engine.book.live()}
    started = time.monotonic()
    run._proof_steps(SimpleNamespace(jobs=[]), None)
    outcome = run._finish("cancelled")
    assert time.monotonic() - started < 10
    # Code's own checks ran once, before the stop; the reads stopped with the two running.
    assert len(asked) <= 4 and judged == [] and checks == ["ledger"]
    assert outcome.stopped == "cancelled"
    assert before <= {f["id"] for f in engine.book.live()}
    assert any(b.get("type") == "resume" for b in outcome.blocks)
    assert harness.files == []  # the files are made when the check is continued


def test_after_stop_only_the_ledgers_sums_are_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-23: after Stop the reading ledger's sums (what the helpers recorded) are added,
    and no check that reads the whole set again starts (a Continue runs them)."""
    from app.review.assistant import chat_engine

    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    checks: list[str] = []
    monkeypatch.setattr(engine, "_ledger_findings", lambda: checks.append("ledger"))
    monkeypatch.setattr(chat_engine, "add_consistency", lambda _e: checks.append("strings"))
    monkeypatch.setattr(chat_engine, "add_run_fit", lambda _e: checks.append("run fit"))
    engine.cancel.set()
    engine.add_code_findings()
    assert checks == ["ledger"] and engine.code_checks_failed == []
