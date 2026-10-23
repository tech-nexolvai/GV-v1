"""Code-paired dimensions as findings by themselves (`pair_findings`): Wrong or Right with proof.

Made-up pairs and labels only (no client data): page 2 holds an architect elevation (D1-D4) and a
vendor elevation (D10-D13) of one area.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import pair_findings as pf
from app.review.assistant import proof
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_trust import judged
from app.review.assistant.full_check_lenses import same_wrongs
from app.review.assistant.product_scope import CODE_MADE, ProductScope, refiled
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.vendor_text import internal

FILE = "Set.pdf"


def _label(ident: str, text: str, inches: str, side: str, x: float, route: str = "text") -> dict:
    return {
        "id": ident,
        "text": text,
        "inches": inches,
        "side": side,
        "route": route,
        "orientation": "h",
        "box": [
            x,
            100.0 if side == "architect" else 400.0,
            x + 20,
            110.0 if side == "architect" else 410.0,
        ],
    }


def _dims(*extra: dict[str, Any]) -> dict[str, Any]:
    labels = [
        _label("D1", "1' - 0\"", "12", "architect", 100),
        _label("D2", '8"', "8", "architect", 130),
        _label("D3", "1' - 6\"", "18", "architect", 160),
        _label("D4", '9"', "9", "architect", 190),
        _label("D10", "177 [7]", "7", "vendor", 100),
        _label("D11", "254 [10]", "10", "vendor", 130),
        _label("D12", "762 [30]", "30", "vendor", 160),
        _label("D13", "229 [9]", "9", "vendor", 190),
        *extra,
    ]
    return {"version": 5, "files": [FILE], "pages": [{"file": FILE, "page": 2, "labels": labels}]}


def _pair(arch: list[str], vendor: str, how: str, kind: str = "one", **more: Any) -> dict[str, Any]:
    return {
        "page": 2,
        "kind": kind,
        "architect": {"id": "+".join(arch), "ids": arch, "text": "", "page": 2},
        "vendor": [{"id": vendor, "text": ""}],
        "status": "different",
        "printed_both": True,
        "paired_by": "code",
        "method": "anchors",
        "how": f"paired by what both ends land on: {how}, on both drawings (no scale used)",
        **more,
    }


def _pairs(*pairs: dict[str, Any]) -> dict[str, Any]:
    return {"version": 1, "pairs": [], "pages": [{"file": FILE, "page": 2, "pairs": list(pairs)}]}


PAGE_MAP = {"version": 5, "files": [FILE], "pages": [{"file": FILE, "page": 2, "area": "3.Galley"}]}


def test_a_pair_whose_printed_values_differ_is_a_wrong_with_its_pairing() -> None:
    found = pf.findings_from(
        _pairs(_pair(["D1"], "D10", 'the left wall to outlet 1 of the row at 46" (centre)')),
        _dims(),
        PAGE_MAP,
    )
    assert len(found) == 1
    wrong = found[0]
    assert wrong["status"] == "different" and wrong["paired_by"] == "code"
    assert wrong["item"] == 'Galley: the left wall to outlet 1 of the row at 46" (centre)'
    assert (wrong["architect"], wrong["vendor"]) == ("1' - 0\"", "177 [7]")
    assert wrong["dims"]["vendor"]["id"] == "D10" and wrong["dims"]["architect"]["id"] == "D1"
    assert wrong["ledger_ids"] == ["p2.D10", "p2.D1"]
    assert wrong["created_by"] == pf.CREATED_BY and wrong["source"] == "architect"
    assert '5" less' in wrong["why"]


def test_values_within_a_sixteenth_are_right() -> None:
    near = _label("D14", "230 [9 1/16]", "9.0625", "vendor", 220)
    found = pf.findings_from(
        _pairs(
            _pair(["D4"], "D13", "outlet 1 to the right wall", status="same"),
            _pair(["D4"], "D14", "outlet 2 to the right wall"),
        ),
        _dims(near),
        PAGE_MAP,
    )
    assert [f["status"] for f in found] == ["same", "same"]
    assert "Both print the same" in found[0]["why"]


def test_a_chain_is_summed_and_compared_with_the_vendors_label() -> None:
    found = pf.findings_from(
        _pairs(_pair(["D1", "D3"], "D12", 'the left wall to outlet 1 of the row at 78"', "sum")),
        _dims(),
        PAGE_MAP,
    )
    assert len(found) == 1 and found[0]["architect"] == '30"' and found[0]["status"] == "same"
    assert "architect" not in found[0]["dims"]
    assert '1\' - 0" + 1\' - 6" = 30"' in found[0]["why"]


def test_a_chain_that_differs_is_never_wrong_it_is_not_compared() -> None:
    # The architect prints 1'-0" + 8" (20") but no overall; the vendor prints 762 [30] (D12).
    found = pf.findings_from(
        _pairs(_pair(["D1", "D2"], "D12", "the left wall to outlet 2", "sum")), _dims(), PAGE_MAP
    )
    item = found[0]
    assert item["status"] == "check" and item["paired_by"] == "code"
    assert item["architect_parts"] == {
        "parts": ["1' - 0\"", '8"'],
        "total": '20"',
        "vendor": '30"',
        "chain_proved": False,  # no dimension lines code could follow: code is unsure
    }
    assert "prints no overall here" in item["why"]
    checks = {"printed_architect": "not_found", "printed_vendor": "match", "units": "agrees"}
    stored = judged(
        {**item, "id": "F1", "checks": checks, "inches": {"architect": "20", "vendor": "30"}}
    )
    assert proof.proof_group(stored) == "not_compared"
    found_as = proof.classified(stored)
    assert (found_as.reason, found_as.label) == (
        "no_overall",
        "Not reviewed: architect gives no overall",
    )
    assert found_as.why == 'architect\'s parts add to 20"; vendor shows 30"'
    # Never red, even if a later step said the status were different.
    assert proof.proof_group({**stored, "status": "different"}) == "not_compared"


def test_an_answers_own_item_on_a_differing_chain_becomes_not_compared() -> None:
    code = pf.findings_from(
        _pairs(_pair(["D1", "D2"], "D12", "the left wall to outlet 2", "sum")), _dims(), PAGE_MAP
    )[0]
    existing = {"id": "F5", "status": "different", "why": "Wall to outlet differs."}
    changes = pf.proved_changes(code, existing)
    assert changes["status"] == "check" and changes["architect_parts"]["total"] == '20"'
    merged = {**existing, **changes}
    assert proof.proof_group(merged) == "not_compared"
    assert proof.classified(merged).reason == "no_overall"


def test_a_chain_with_a_label_read_from_drawn_ink_is_left_out() -> None:
    drawn = _label("D5", '6"', "6", "architect", 220, route="drawn")
    found = pf.findings_from(
        _pairs(_pair(["D1", "D5"], "D12", "the wall to the outlet", "sum")), _dims(drawn), PAGE_MAP
    )
    assert found == []


def test_the_same_pair_printed_twice_is_one_item() -> None:
    copy = _label("D20", "254 [10]", "10", "vendor", 130)
    copy["box"] = [130.0, 700.0, 150.0, 710.0]
    found = pf.findings_from(
        _pairs(
            _pair(["D2"], "D11", "outlet 1 to outlet 2"),
            _pair(["D2"], "D20", "outlet 1 to outlet 2"),
        ),
        _dims(copy),
        PAGE_MAP,
    )
    assert len(found) == 1 and found[0]["ledger_ids"] == ["p2.D11", "p2.D2", "p2.D20"]


def test_pairs_code_did_not_make_or_could_not_read_are_left_out() -> None:
    found = pf.findings_from(
        _pairs(
            _pair(["D1"], "D10", "a", paired_by=None),
            _pair(["D1"], "D10", "b", printed_both=False),
            _pair(["D1"], "D99", "c"),
            {**_pair(["D1"], "D10", "d"), "vendor": [{"id": "D10"}, {"id": "D11"}]},
        ),
        _dims(),
        PAGE_MAP,
    )
    assert found == []
    assert pf.findings_from(None, _dims()) == [] and pf.findings_from(_pairs(), None) == []


def test_wrongs_come_first() -> None:
    found = pf.findings_from(
        _pairs(
            _pair(["D4"], "D13", "x to y"),
            _pair(["D1", "D2"], "D12", "wall to outlet 2", "sum"),
            _pair(["D2"], "D11", "outlet 1 to outlet 2"),
        ),
        _dims(),
        PAGE_MAP,
    )
    assert [f["status"] for f in found] == ["different", "check", "same"]


def test_check_pack_reads_the_pack_files(tmp_path: Path) -> None:
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "pairs.json").write_text(json.dumps(_pairs(_pair(["D2"], "D11", "a to b"))))
    (folder / "dims.json").write_text(json.dumps(_dims()))
    (folder / "page_map.json").write_text(json.dumps(PAGE_MAP))
    assert [f["vendor"] for f in pf.check_pack(folder)] == ["254 [10]"]
    assert pf.check_pack(tmp_path / "none") == [] and pf.check_pack(None) == []


# ---- the same item ---------------------------------------------------------------------------------


def _code() -> dict[str, Any]:
    return pf.findings_from(
        _pairs(_pair(["D4"], "D11", "outlet 1 to outlet 2")), _dims(), PAGE_MAP
    )[0]


def test_the_pair_not_the_value_defines_the_item() -> None:
    code = _code()  # the vendor's 10" (D11) against the architect's 9" (D4)
    on_label = {
        "id": "F1",
        "page": 2,
        "source": "architect",
        "dims": {"vendor": {"id": "D11", "page": 2}},
    }
    other_label = {
        "id": "F2",
        "page": 2,
        "source": "architect",
        "vendor": "254 [10]",
        "architect": '9"',
        "dims": {"vendor": {"id": "D13", "page": 2}},
    }
    assert pf.same_item(code, [on_label]) is on_label
    # Another vendor label with the same two values is another item.
    assert pf.same_item(code, [other_label]) is None
    # A vendor-own item (the vendor's string) on the same label is not this comparison.
    string = {**on_label, "source": "vendor_arithmetic"}
    assert pf.same_item(code, [string]) is None


def test_without_labels_the_vendor_value_at_the_same_spot_is_the_same_item() -> None:
    code = _code()
    box = dict(code["vendor_box"])
    spot = {
        "id": "F3",
        "page": 2,
        "source": "architect",
        "vendor": "254 [10]",
        "architect": "a chain",
        "vendor_box": box,
    }
    elsewhere = {**spot, "vendor_box": {**box, "y0": 900.0, "y1": 910.0}}
    no_box = {**spot, "vendor_box": None}
    assert pf.same_item(code, [spot]) is spot
    assert pf.same_item(code, [elsewhere]) is None
    # No spot to tell them apart: both values must agree.
    assert pf.same_item(code, [no_box]) is None
    assert pf.same_item(code, [{**no_box, "architect": '9"'}]) is not None
    assert pf.same_item(code, [{**spot, "page": 3}]) is None


def test_code_wins_on_proof_over_the_answers_own_item() -> None:
    code = _code()
    existing = {
        "id": "F4",
        "status": "check",
        "why": "The AI could not pair them.",
        "arch_box": {"x0": 1},
    }
    changes = pf.proved_changes(code, existing)
    assert changes["status"] == "different" and changes["paired_by"] == "code"
    assert changes["architect"] == '9"' and changes["dims"]["architect"]["id"] == "D4"
    assert "arch_box" not in changes and changes["vendor_box"] == code["vendor_box"]
    assert changes["why"].startswith("The AI could not pair them. Code paired")


# ---- proof, scope and the hook ---------------------------------------------------------------------


def test_a_code_pair_read_by_code_is_clouded_and_without_the_pair_it_is_not() -> None:
    checks = {"printed_architect": "match", "printed_vendor": "match", "units": "agrees"}
    made = judged(
        {**_code(), "id": "F1", "checks": checks, "inches": {"architect": "9", "vendor": "10"}}
    )
    assert made["trust_level"] in ("confirmed", "likely")
    assert proof.proof_group(made) == "clouded"
    unpaired = judged({**made, "paired_by": None})
    assert proof.proof_group(unpaired) == "not_compared"


def test_code_pairs_are_code_made_for_the_product_scope() -> None:
    assert pf.CREATED_BY in CODE_MADE
    item = {**_code(), "item": "Galley: counter top to outlet", "category": "other"}
    assert refiled(item, ProductScope.of(["countertop"])) == "countertop"
    assert (
        refiled({**item, "item": "Galley: outlet to wall"}, ProductScope.of(["countertop"])) is None
    )


def test_the_book_proves_an_existing_finding_by_code(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    book = FindingBook(workspace=workspace, listing=listing, page_count=2)
    book.findings["F1"] = {"id": "F1", "page": 2, "status": "check", "item": "Galley: outlets"}
    changed = book.prove_by_code("F1", {"status": "different", "paired_by": "code"})
    assert (
        changed is not None
        and changed["paired_by"] == "code"
        and changed["item"] == "Galley: outlets"
    )
    assert book.live()[0]["status"] == "different"
    assert book.prove_by_code("F9", {"status": "same"}) is None


class _Book:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self._live = live
        self.added: list[tuple[dict[str, Any], str]] = []
        self.proved: list[tuple[str, dict[str, Any]]] = []

    def live(self) -> list[dict[str, Any]]:
        return list(self._live)

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        self.added.append((given, created_by))
        return {**given, "id": f"P{len(self.added)}"}

    def prove_by_code(self, finding_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        self.proved.append((finding_id, changes))
        return {"id": finding_id, **changes}


class _Engine:
    def __init__(self, mode: str, live: list[dict[str, Any]]) -> None:
        self.settings = type("S", (), {"mode": mode})()
        workspace = type("W", (), {"set_pack": Path("pack")})()
        self.tools = type("T", (), {"workspace": workspace})()
        self.book = _Book(live)
        self.finding_lock = threading.RLock()
        self.shown: list[dict[str, Any]] = []

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)


def test_the_hook_adds_new_items_and_proves_the_answers_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = pf.findings_from(
        _pairs(
            _pair(["D4"], "D11", "outlet 1 to outlet 2"), _pair(["D1"], "D10", "wall to outlet")
        ),
        _dims(),
        PAGE_MAP,
    )
    monkeypatch.setattr(pf, "check_pack", lambda folder: made)
    ai_item = {
        "id": "F7",
        "page": 2,
        "source": "architect",
        "status": "check",
        "dims": {"vendor": {"id": "D11", "page": 2}},
    }
    already = {
        "id": "F8",
        "page": 2,
        "source": "architect",
        "paired_by": "code",
        "dims": {"vendor": {"id": "D10", "page": 2}},
    }
    engine = _Engine("full_check", [ai_item])
    assert pf.add_to_answer(engine) == {"added": 1, "proved": 1}
    assert [g["vendor"] for g, by in engine.book.added] == ["177 [7]"]
    assert {by for _g, by in engine.book.added} == {pf.CREATED_BY}
    assert engine.book.proved[0][0] == "F7" and engine.book.proved[0][1]["status"] == "different"
    assert [f["id"] for f in engine.shown] == ["F7", "P1"]
    # An item code already paired is left as it is.
    engine = _Engine("full_check", [ai_item, already])
    assert pf.add_to_answer(engine) == {"added": 0, "proved": 1}


def test_the_hook_runs_only_in_a_full_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pf, "check_pack", lambda folder: [_code()])
    assert pf.add_to_answer(_Engine("chat", [])) is None
    monkeypatch.setattr(pf, "check_pack", lambda folder: [])
    assert pf.add_to_answer(_Engine("full_check", [])) is None


def test_a_code_pair_and_an_ai_item_on_the_same_label_fold_into_the_code_one() -> None:
    code = {**_code(), "id": "F9", "trust_level": "likely"}
    ai_item = {
        "id": "F2",
        "page": 2,
        "status": "different",
        "item": "Galley: outlet spacing",
        "architect": '9"',
        "vendor": "254 [10]",
        "trust_level": "likely",
        "dims": {"vendor": {"id": "D11", "page": 2}},
    }
    folds = same_wrongs([ai_item, code])
    assert [(f.keep, f.drop) for f in folds] == [("F9", "F2")]


def test_the_names_and_reasons_are_fit_for_the_vendor() -> None:
    found = pf.findings_from(
        _pairs(
            _pair(["D4"], "D11", 'outlet 1 of the row at 78" (centre) to outlet 2'),
            _pair(["D1", "D2"], "D12", "the left wall to the right wall", "sum"),
            _pair(["D4"], "D13", "outlet 1 to the right wall"),
        ),
        _dims(),
        PAGE_MAP,
    )
    assert len(found) == 3
    for item in found:
        assert not internal(item["item"]) and not internal(item["why"])


# ---- the architect's chain with no overall (client meeting, 2026-10-23) ----------------------------


def _drawn(
    ident: str, text: str, inches: str, start: float, end: float, y: float = 120.0
) -> dict[str, Any]:
    """An architect label on its dimension line from `start` to `end`, extension lines at both."""
    label = _label(ident, text, inches, "architect", start)
    label["dim_line"] = [[start, y], [end, y]]
    label["extension"] = [[[start, y - 8], [start, y]], [[end, y - 8], [end, y]]]
    return label


def _chain_dims(*architect: dict[str, Any]) -> dict[str, Any]:
    vendor = [_label("D12", "762 [30]", "30", "vendor", 160)]
    return {
        "version": 5,
        "files": [FILE],
        "pages": [{"file": FILE, "page": 2, "labels": [*architect, *vendor]}],
    }


def test_a_proved_chain_is_compared_like_a_printed_overall() -> None:
    # The architect prints 1'-0" + 8" (20") on one dimension line, end to end, no overall; the
    # vendor prints 30" between the same two anchors.
    dims = _chain_dims(_drawn("D1", "1' - 0\"", "12", 100, 136), _drawn("D2", '8"', "8", 136, 160))
    item = pf.findings_from(
        _pairs(_pair(["D1", "D2"], "D12", "the left wall to outlet 2", "sum")), dims, PAGE_MAP
    )[0]
    assert item["status"] == "different" and item["architect_parts"]["chain_proved"] is True
    assert item["architect"] == '20"' and "one chain end to end" in item["why"]
    derived = item["dims"]["architect"]
    assert derived["id"] == "D1+D2" and derived["derived"]["sum_of"] == ["D1", "D2"]
    box = item["arch_box"]
    assert derived["box"] == [box["x0"], box["y0"], box["x1"], box["y1"]]
    # Red only under the usual proof rules: code read both sides and code made the pair.
    checks = {"printed_architect": "match", "printed_vendor": "match", "units": "agrees"}
    stored = judged(
        {**item, "id": "F1", "checks": checks, "inches": {"architect": "20", "vendor": "30"}}
    )
    assert proof.proof_group(stored) == "clouded"
    assert proof.proof_group(judged({**stored, "paired_by": None})) != "clouded"


@pytest.mark.parametrize(
    "architect",
    [
        # A gap between the parts: not one chain.
        (_drawn("D1", "1' - 0\"", "12", 100, 130), _drawn("D2", '8"', "8", 136, 160)),
        # On two different dimension lines.
        (_drawn("D1", "1' - 0\"", "12", 100, 136), _drawn("D2", '8"', "8", 136, 160, y=140)),
        # An extension line missing at one end.
        (
            _drawn("D1", "1' - 0\"", "12", 100, 136),
            {**_drawn("D2", '8"', "8", 136, 160), "extension": [[[136, 112], [136, 120]], None]},
        ),
        # The architect prints the overall on a parallel line: the overall is compared itself.
        (
            _drawn("D1", "1' - 0\"", "12", 100, 136),
            _drawn("D2", '8"', "8", 136, 160),
            {**_drawn("D9", "1' - 8\"", "20", 100, 160, y=90), "id": "D9"},
        ),
    ],
)
def test_an_unproved_chain_stays_not_compared(architect: tuple[dict[str, Any], ...]) -> None:
    item = pf.findings_from(
        _pairs(_pair(["D1", "D2"], "D12", "the left wall to outlet 2", "sum")),
        _chain_dims(*architect),
        PAGE_MAP,
    )[0]
    assert item["status"] == "check" and item["architect_parts"]["chain_proved"] is False
    assert "architect" not in item["dims"]
    assert proof.classified(item).reason == "no_overall"
