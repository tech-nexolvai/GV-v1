"""The strict product scope in the simple full check's outputs (Anant, 2026-10-18: "only the
selected products are checked, ruled, counted and drawn"). With one product picked, an item of the
other product is never paid a blind look, never listed as Not compared ("the answer's item limit
was reached" was its old, wrong reason), and the set's rows that are not findings (sheet gaps,
finish notes, labels the architect gives no size for) are cut to the scope too. A word inside a
longer phrase of another product does not name it ("vanity top" is a countertop's). A real chat
engine on a made-up set; the compare calls and the blind look replaced; no model call, no money."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import not_compared
from app.review.assistant.chat_engine import ChatEngine, EngineSettings
from app.review.assistant.chat_request import MODELS
from app.review.assistant.chat_tools import ChatTools
from app.review.assistant.product_scope import (
    ProductScope,
    category_for,
    main_product,
    named_products,
    refiled,
    scoped_gaps,
)
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check import verify as verify_module
from app.review.assistant.simple_check.schema import CompareOutcome, CompareResult, Pair
from tests.review.assistant.chat_fakes import FakeLedger, FakeSandbox
from tests.review.assistant.test_simple_run_stop import (
    DOLLARS,
    OVERALL,
    RATES,
    _agreeing,
    _pack,
    _pdf,
    _Pictures,
)
from tests.review.assistant.test_simple_verify import FILE, ROUND_1

# ---- reading a text for its products ------------------------------------------------------------


def test_the_longest_phrase_names_the_product() -> None:
    assert named_products("Vanity top width (plan)", hints=True) == {"countertop"}
    assert named_products("Floor to vanity top", hints=True) == {"countertop"}
    assert named_products("Standard Vanity", hints=True) == {"cabinet"}
    # Both stand on their own here: the area's vanity, and the vanity top.
    assert named_products("Standard Vanity: Vanity top width", hints=True) == {
        "countertop",
        "cabinet",
    }
    assert named_products("vanity base cabinet and its vanity top") == {"cabinet"}


def test_an_answers_item_is_filed_by_its_own_words_first() -> None:
    both = verify_module.Item("p1.v1", ROUND_1.vendor, products=("cabinet", "countertop"))
    one = verify_module.Item("p1.v1", ROUND_1.vendor, products=("cabinet",))
    assert verify_module._category(both, "Vanity top width (elevation)") == "countertop"
    assert verify_module._category(both, "toe kick height") == "cabinet"
    assert verify_module._category(both, "floor to the outlet") == "other"
    # A drawing of one product files what its words do not name...
    assert verify_module._category(one, "floor to the outlet") == "cabinet"
    # ...and a product's own word wins over the drawing's.
    assert verify_module._category(one, "countertop overall length") == "countertop"


def test_an_item_with_no_product_words_is_filed_under_its_runs_product() -> None:
    """Orchestrator, 2026-10-19: an outlet or a string on a cabinet run is a cabinet item (under
    All, 25 of 29 items were "other"). The run's product is the one its printed words name most
    (hints never count); its own words still come first."""
    run = "2. Kitchen  BASE CABINET  FILLER  3 DRAWERS  FILLER  STONE TOP"
    assert main_product(run) == "cabinet"
    assert main_product("QUARTZ COUNTERTOP  BACKSPLASH  FILLER") == "countertop"
    assert main_product("CABINET  QUARTZ") is None  # a tie names none
    assert main_product("SINK  COUNTER") is None  # hints never count
    outlet = {"category": "other", "item": "Kitchen with cooktop: outlet 1 to the left wall"}
    string = {"category": "other", "item": "Kitchen: dimension string (across, round 1)"}
    for item in (outlet, string):
        code = {**item, "created_by": "pairs"}
        assert category_for(code, run) == "cabinet"
        assert refiled(code, ProductScope.of(("cabinet",)), run) == "cabinet"
        assert refiled(code, ProductScope.of(("countertop",)), run) is None
    # Its own words win over the run's: a top's size on a cabinet run is a countertop item.
    top = {"category": "other", "item": "Kitchen: floor to top of counter", "created_by": "pairs"}
    assert category_for(top, run) == "countertop"
    # The simple check files by the drawing's main product too.
    both = verify_module.Item(
        "p1.v1", ROUND_1.vendor, products=("cabinet", "countertop"), main_product="cabinet"
    )
    assert verify_module._category(both, "the left wall to outlet 1") == "cabinet"
    assert verify_module._category(both, "vanity top width") == "countertop"


# ---- the set's rows that are not findings ------------------------------------------------------


def test_the_sets_rows_are_cut_to_the_scope() -> None:
    gaps = [
        {"page": 13, "what": 'Gym: architect\'s note "ALL CABINETS TO HAVE DRAWERS"'},
        {"page": 19, "what": "Pantry: architect's LA-9 against the vendor's WD-7"},
        {
            "page": 34,
            "what": "102 [4] (page 34, label D8): architect gives no size",
            "item": "Bar: splash height",
            "category": "countertop",
        },
        {"page": 36, "what": "Lobby: round 2 does not include this area (round 1 did, p.6)"},
    ]
    words = {13: "", 19: "", 34: "", 36: "BASE CABINET  STONE TOP"}.get

    def kept(products: tuple[str, ...]) -> list[int]:
        rows, _dropped = scoped_gaps(gaps, ProductScope.of(products), lambda p: words(p) or "")
        return [row["page"] for row in rows]

    # The lobby's page is a cabinet run (its words name a cabinet; "stone" only hints at a top).
    assert kept(("countertop",)) == [34]
    assert kept(("cabinet",)) == [13, 19, 36]
    assert kept(()) == [13, 19, 34, 36]  # All: every row
    _rows, dropped = scoped_gaps(gaps, ProductScope.of(("countertop",)), lambda p: words(p) or "")
    assert dropped == 3
    # A row that names no product, on a page whose words name none either, is no product's.
    _rows, dropped = scoped_gaps(gaps, ProductScope.of(("countertop",)), None)
    assert dropped == 3


# ---- a whole simple full check under one product ---------------------------------------------------

SPLASH = Pair(
    vendor_label="p2.D5",
    architect_label=None,
    measures="splash height",
    vendor_from="top_of_counter",
    vendor_to="top_of_splash",
    architect_from="",
    architect_to="",
    verdict="no_architect_value",
    note="",
)
TOE_KICK = SPLASH.model_copy(
    update={"vendor_label": "p2.D4", "measures": "toe kick height", "vendor_to": "floor"}
)


def _answer(item: Any) -> CompareOutcome:
    labels = [label.ref for label in item.vendor.labels]
    pairs = (OVERALL, SPLASH, TOE_KICK) if "p2.D1" in labels else ()
    named = {p.vendor_label for p in pairs}
    return CompareOutcome(
        work_item=item.id,
        status="ok",
        result=CompareResult(
            work_item=item.id,
            pairs=pairs,
            other_items=(),
            unaccounted_labels=tuple(ref for ref in labels if ref not in named),
        ),
        asked_model="claude-sonnet-5-5",
        model="claude-sonnet-5-5",
        effort="high",
        attempts=1,
        usage={"input_tokens": 9000, "output_tokens": 3000},
        cost_usd=0.05,
        seconds=1.0,
    )


def _check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, products: tuple[str, ...]
) -> tuple[Any, list[str], list[Any]]:
    """One simple full check of the made-up set with these products picked: the outcome, the
    blind looks made, and the Not-compared list's rows."""
    workspace = ChatWorkspace.open(tmp_path / "chat", set_pack=_pack(tmp_path / "pack"))
    _pdf(workspace.uploads / FILE)
    (workspace.root / "uploads.json").write_text(
        json.dumps([{"name": FILE, "pages": 4, "kind": "the vendor's shop drawings"}]),
        encoding="utf-8",
    )

    def compare_all(items: Any, *, source: Any, on_outcome: Any, **_: Any) -> list[Any]:
        done = []
        for item in items:
            source(item)
            outcome = _answer(item)
            on_outcome(outcome)
            done.append(outcome)
        return done

    monkeypatch.setattr(compare_module, "compare_all", compare_all)
    monkeypatch.setattr(compare_module, "PictureSource", _Pictures)
    looked: list[str] = []

    def look(*args: Any) -> Any:
        looked.append(args[1].key)
        return _agreeing(*args)

    monkeypatch.setattr(blind_module, "look", look)
    listed: list[Any] = []
    make_files = not_compared.make_files

    def recording(workspace: Any, rows: Any, *paths: Any) -> Any:
        listed.extend(rows)
        return make_files(workspace, rows, *paths)

    monkeypatch.setattr(not_compared, "make_files", recording)
    engine = ChatEngine(
        settings=EngineSettings(
            model=MODELS["claude-opus-5-5"],
            effort="high",
            thinking=True,
            lead_max_turns=40,
            helper_max_turns=20,
            message_cap_micros=15 * DOLLARS,
            package_cap_micros=70 * DOLLARS,
            rates=lambda _key: RATES,
            mode="full_check",
            full_check_mode="simple",
            simple={},
            products=products,
        ),
        provider=SimpleNamespace(name="anthropic"),  # type: ignore[arg-type]
        tools=ChatTools(
            workspace=workspace,
            sandbox=FakeSandbox(workspace, lambda _c, _w: ("ok", 0)),
            page_count=4,
            results=None,
            register_file=lambda fid, name, size, mime, sha: {"id": fid, "name": name},
        ),
        ledger=FakeLedger(),
        emit=lambda _event, _data: None,
        history=(),
        user_content=[{"type": "text", "text": "Full check"}],
        package_spent=lambda: 0,
        read_picture=lambda pid: (workspace.pictures / f"{pid}.png").read_bytes(),
        cancel=threading.Event(),
        sleep=lambda _seconds: False,
    )
    return engine.run(), looked, listed


def _simple(outcome: Any) -> list[dict[str, Any]]:
    return [
        b["finding"]
        for b in outcome.blocks
        if b.get("type") == "finding" and b["finding"].get("created_by") == "simple_check"
    ]


def test_a_cabinet_check_shows_and_pays_for_no_countertop_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, looked, rows = _check(tmp_path, monkeypatch, ("cabinet",))
    assert outcome.stopped is None
    # The countertop's red candidate (the overall counter width) got no paid blind look...
    assert looked == []
    # ...and is nowhere: not a finding, not a Not-compared row (no "item limit" reason).
    assert all(f["vendor"] != '45 1/4"' for f in _simple(outcome))
    words = [f"{row.item} {row.why}" for row in rows]
    assert not any("item limit" in line for line in words)
    assert not any("label D1)" in line and "page 2" in line for line in words)
    # The countertop's splash (no architect size) is not listed; the cabinet's toe kick is, in
    # one row for its drawing (not one row per label).
    assert not any("(D5)" in line for line in words)
    assert any(
        "1 dimension the architect gives no size for" in line and "(D4)" in line for line in words
    )
    trust = next(b["trust"] for b in outcome.blocks if b.get("type") == "trust")
    assert trust["product_scope"]["dropped_out_of_scope"] >= 1


def test_all_products_still_look_at_and_list_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome, looked, rows = _check(tmp_path, monkeypatch, ())
    assert len(looked) == 1
    red = next(f for f in _simple(outcome) if f["vendor"] == '45 1/4"')
    assert red["status"] == "different" and red["category"] == "countertop"
    words = [row.item for row in rows]
    assert any("(D5)" in line for line in words)
    assert any("(D4)" in line for line in words)


def test_labels_with_no_architect_size_are_one_row_per_drawing() -> None:
    """The app test (2026-10-19): the screen said 46 Not compared and the workbook had 1,232 rows,
    1,175 of them one per label the architect gives no size for. They are one row per drawing."""
    from app.review.assistant.simple_check import run as run_module
    from app.review.assistant.simple_check.verify import Verified

    labels = [
        Verified(
            f"p23.v1:p23.D{n}",
            "pair",
            "listed",
            "architect gives no size",
            "nothing_to_compare",
            vendor={"id": f"D{n}", "page": 23, "text": f'{n}"'},
        )
        for n in range(1, 10)
    ]
    row = run_module._no_size_row("p23.v1", "cabinet", labels)
    assert row["what"].startswith(
        'Vendor drawing on page 23: 9 dimensions the architect gives no size for: 1" (D1), 2" (D2)'
    )
    assert row["what"].endswith("and 3 more") and row["category"] == "cabinet"
    listed = not_compared.gap_rows([row])
    assert len(listed) == 1 and listed[0].reason == "nothing_to_compare"
