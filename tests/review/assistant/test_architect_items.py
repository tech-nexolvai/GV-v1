"""The finish check (#1623): missed, changed and added finishes against the architect, by code.

Every set here is made up: a page map with GV's typed headings and the printed phrases the set
pack's text pass would list (`dims.json`). Nothing reads a client drawing or calls a model.
"""

from __future__ import annotations

import json
import threading
import zlib
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import architect_items as ai
from app.review.assistant import proof, witness
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_trust import apply_checks
from app.review.assistant.chat_unknowns import unknown_records
from app.review.assistant.sandbox import ChatWorkspace

FILE = "set.pdf"
#: Page 1: the architect's drawing on top, the vendor's round 1 below. Page 2: the vendor's round 2.
ARCHITECT = {"box": [0, 0, 600, 180], "role": "architect", "round": None}
ROUND_1 = {"box": [0, 200, 600, 400], "role": "vendor", "round": 1}
ROUND_2 = {"box": [0, 0, 600, 400], "role": "vendor", "round": 2}
PROTOTYPE = {"box": [0, 0, 600, 400], "role": "brand_prototype", "round": None}
#: Words every vendor drawing prints, so its text is readable.
VENDOR_WORDS = ("FRONT VIEW", "PLAN VIEW", "ADJUSTABLE LEVELERS")


def _phrase(text: str, x: float, y: float, side: str = "vendor") -> dict[str, Any]:
    return {"text": text, "box": [x, y, x + 6 * len(text), y + 8], "side": side}


def _architect(*texts: str) -> list[dict[str, Any]]:
    return [_phrase(t, 40 + 120 * n, 40, "architect") for n, t in enumerate(texts)]


def _vendor(top: float, *texts: str) -> list[dict[str, Any]]:
    words = (*VENDOR_WORDS, *texts)
    return [_phrase(t, 40 + 130 * (n % 4), top + 20 * (n // 4)) for n, t in enumerate(words)]


def _set(
    architect: Sequence[str],
    round_1: Sequence[str] | None,
    round_2: Sequence[str] | None,
    *,
    prototype: Sequence[str] = (),
    labels: Sequence[dict[str, Any]] = (),
) -> tuple[dict[str, Any], dict[str, Any]]:
    """(dims, page_map) of a made-up area: page 1 architect + round 1, page 2 round 2, page 3 the
    brand prototype (when given)."""
    first: list[dict[str, Any]] = [ARCHITECT]
    phrases = _architect(*architect)
    if round_1 is not None:
        first.append(ROUND_1)
        phrases += _vendor(220, *round_1)
    pages = [
        {"file": FILE, "page": 1, "role": "architect+vendor", "halves": first, "area": "1. Galley"}
    ]
    dims_pages = [{"file": FILE, "page": 1, "phrases": phrases, "labels": list(labels)}]
    if round_2 is not None:
        pages.append(
            {
                "file": FILE,
                "page": 2,
                "role": "vendor",
                "halves": [ROUND_2],
                "area": "1. Galley",
                "area_from_page": 1,
            }
        )
        dims_pages.append({"file": FILE, "page": 2, "phrases": _vendor(20, *round_2)})
    if prototype:
        pages.append(
            {
                "file": FILE,
                "page": 3,
                "role": "brand_prototype",
                "halves": [PROTOTYPE],
                "area": "1. Galley",
                "area_from_page": 1,
            }
        )
        dims_pages.append(
            {"file": FILE, "page": 3, "phrases": [_phrase(t, 40, 40, "brand") for t in prototype]}
        )
    return {"files": [FILE], "pages": dims_pages}, {"files": [FILE], "pages": pages}


def _check(*args: Any, **kwargs: Any) -> ai.ItemsResult:
    texts = kwargs.pop("texts", None)
    products = kwargs.pop("products", None)
    dims, page_map = _set(*args, **kwargs)
    return ai.check_set(dims, page_map, texts=texts, products=products)


# ---- the codes -----------------------------------------------------------------------------------


def test_codes_are_one_code_however_they_are_spelled() -> None:
    found = ai.tag_codes("(LA41) DOOR, LA-041-CUST and LA - 41 by GV; QZ-7B")
    assert [key for _text, _family, key in found] == ["LA-41", "LA-41", "LA-41", "QZ-7B"]
    assert [text for text, _family, _key in found][1] == "LA-041-CUST"
    # A scale, a sheet number or a word is not a code.
    assert ai.tag_codes('1/4" = 1\'-0" ID 5.1 SCALE') == []


# ---- Wrong items -----------------------------------------------------------------------------------


def test_a_finish_the_vendor_never_prints_is_a_missed_wrong() -> None:
    result = _check(["LA-41", "LA-42"], ["LA-42"], ["LA-42-CUST"])
    assert len(result.findings) == 1
    missed = result.findings[0]
    assert missed["status"] == "different" and missed["category"] == "cabinet"
    assert missed["architect"] == "LA-41"
    assert missed["vendor"] == "not on the vendor's sheets"
    assert "no vendor sheet of this area prints it (p.1 round 1, p.2 round 2)" in missed["why"]
    assert missed["item"] == "Galley: cabinet finish LA-41 missed"
    assert missed["page"] == 2 and missed["vs_page"] == 1
    assert missed["arch_box"]["page"] == 1 and "vendor_box" not in missed
    assert missed["paired_by"] == "code" and missed["created_by"] == "architect_items"
    assert missed["text_proof"]["kind"] == "missed"
    assert "both sides print LA-42" in missed["why"]
    assert (result.paired, result.compared, result.matched) == (1, 2, 1)


def test_one_code_each_side_that_differs_is_a_changed_finish() -> None:
    # Round 1 printed the architect's QZ-7, so it is not missed; round 2's QZ-9 is added.
    (added,) = _check(["QZ-7"], ["QZ-7"], ["QZ-9", "LA-42"]).findings
    assert (
        added["vendor"] == "QZ-9" and "An earlier round printed QZ-7 (p.1 round 1)" in added["why"]
    )
    result = _check(["QZ-7", "LA-42"], ["LA-42"], ["QZ-9", "LA-42"])
    (changed,) = result.findings
    assert changed["item"] == "Galley: countertop finish QZ-7 changed"
    assert changed["category"] == "countertop"
    assert changed["vendor"] == "QZ-9 (no QZ-7 on any vendor sheet)"
    assert changed["vendor_box"]["page"] == 2
    assert changed["text_proof"]["kind"] == "changed"


def test_a_code_the_architect_never_prints_in_its_family_is_added() -> None:
    result = _check(["LA-41", "LA-42"], None, ["LA-41", "LA-42", "LA-47"])
    (added,) = result.findings
    assert added["item"] == "Galley: cabinet finish LA-47 not on the architect"
    assert added["vendor"] == "LA-47" and added["vendor_box"]["page"] == 2
    assert added["architect"] == "not on the architect's drawings (they tag LA-41, LA-42)"
    assert "arch_box" not in added and added["text_proof"]["kind"] == "added"


def test_only_the_products_in_scope_are_compared() -> None:
    result = _check(["LA-41", "QZ-7"], None, ["LA-42", "QZ-7"], products=["countertop"])
    assert result.findings == [] and result.compared == 1


# ---- not proved: never a Wrong -------------------------------------------------------------------


def test_an_area_code_cannot_pair_is_not_compared() -> None:
    result = _check(["LA-41"], None, ["WD-3"])
    assert result.findings == [] and result.paired == 0
    (open_item,) = result.unknowns
    assert open_item["why"] == "pairing_uncertain" and "LA-41" in open_item["what"]


def test_printed_dimensions_on_both_sides_prove_the_pairing() -> None:
    labels = [
        {"inches": "36", "box": [40, 100, 60, 108]},
        {"inches": "84", "box": [90, 100, 110, 108]},
        {"inches": "36", "box": [40, 300, 60, 308]},
        {"inches": "84", "box": [90, 300, 110, 308]},
    ]
    result = _check(["LA-41"], ["WD-3", "LA-42"], None, labels=labels)
    assert result.paired == 1
    assert result.findings[0]["text_proof"]["pairing"] == (
        "both sides print 2 of the same dimensions"
    )


def test_a_vendor_sheet_without_readable_text_proves_no_absence() -> None:
    dims, page_map = _set(["LA-41", "LA-42"], ["LA-42"], ["LA-42"])
    dims["pages"][1]["phrases"] = [_phrase("LA-42", 40, 20)]  # round 2: pen strokes but a tag
    result = ai.check_set(dims, page_map)
    assert result.findings == []
    (open_item,) = result.unknowns
    assert open_item["why"] == "unreadable" and "no readable text" in open_item["needs"]


def test_a_copy_in_the_page_text_code_did_not_place_proves_no_absence() -> None:
    texts = {(FILE, 1): "LA-41 LA-42 LA-42", (FILE, 2): "LA-42-CUST\r\nLA-41 (in red)"}
    result = _check(["LA-41", "LA-42"], ["LA-42"], ["LA-42-CUST"], texts=texts)
    assert result.findings == []
    assert result.unknowns[0]["why"] == "unreadable"
    assert "page 2 prints it outside" in result.unknowns[0]["needs"]
    # The page text holding only the copies code placed proves the absence.
    texts[(FILE, 2)] = "LA-42-CUST FRONT VIEW"
    assert len(_check(["LA-41", "LA-42"], ["LA-42"], ["LA-42-CUST"], texts=texts).findings) == 1


def test_codes_in_another_tag_system_are_not_compared() -> None:
    result = _check(["QZ-7", "LA-42"], None, ["SS-2", "LA-42"])
    assert result.findings == []
    (open_item,) = result.unknowns
    assert open_item["why"] == "not_comparable"
    assert open_item["what"] == "Galley: architect's QZ-7 against the vendor's SS-2"


def test_a_product_the_vendor_says_is_supplied_by_others_is_not_compared() -> None:
    result = _check(["QZ-7", "LA-42"], None, ["SLAB COUNTERTOP", "by GV", "LA-42"])
    assert result.findings == []
    assert "the vendor's sheet says" in result.unknowns[0]["what"]


def test_a_material_named_in_words_is_not_compared() -> None:
    result = _check(["QZ-7", "LA-42"], None, ["QUARTZ SLAB TOP", "LA-42"])
    assert result.findings == []
    assert "names the material in words" in result.unknowns[0]["what"]


def test_a_vendor_code_on_the_prototype_only_is_not_added() -> None:
    result = _check(["LA-41"], ["LA-41"], ["LA-41", "LA-49"], prototype=["LA-49 INSIDE FINISH"])
    assert result.findings == []
    assert result.unknowns[0]["what"] == "Galley: vendor's LA-49 is on the brand prototype only"


def test_a_finish_only_the_prototype_tags_is_not_compared() -> None:
    result = _check(["LA-41"], ["LA-41"], ["LA-41"], prototype=["QZ-5"])
    assert result.findings == []
    (open_item,) = result.unknowns
    assert open_item["what"] == "Galley: QZ-5 is on the brand prototype only (p.3)"


def test_other_trades_and_unknown_families_are_left_out() -> None:
    result = _check(["PTW-3", "TW-12-CUST", "XQ-4", "LA-42"], ["LA-42"], None)
    assert result.findings == [] and result.unknowns == []
    assert result.left_out == {"other_trades": 2, "unknown_family": 1}


# ---- notes -----------------------------------------------------------------------------------------


def test_a_note_naming_a_part_no_vendor_sheet_names_is_not_compared() -> None:
    dims, page_map = _set(["LA-42"], ["LA-42"], ["LA-42"])
    dims["pages"][0]["phrases"] += [
        _phrase("END CABINETS TO BE", 300, 100, "architect"),
        _phrase("SOFT-CLOSE DRAWERS", 300, 110, "architect"),
        _phrase("GALLEY ELEVATION", 300, 150, "architect"),
    ]
    result = ai.check_set(dims, page_map)
    (note,) = result.unknowns
    assert note["why"] == "not_comparable"
    assert note["what"] == 'Galley: architect\'s note "END CABINETS TO BE SOFT-CLOSE DRAWERS" (p.1)'
    assert "for the drawers" in note["needs"] and result.notes_open == 1
    # A vendor sheet naming drawers covers it.
    dims["pages"][1]["phrases"].append(_phrase("DRAWER GLIDES", 40, 300))
    assert ai.check_set(dims, page_map).unknowns == []


# ---- the trust rule and the cloud drawing ----------------------------------------------------------


def test_the_text_witness_makes_a_code_proved_wrong() -> None:
    checks = {"text": "agrees", "printed_architect": "not_a_number"}
    assert witness.witnesses(checks, "different") == ["text"]
    assert witness.evidence_kind(checks, "different") == "observed"
    assert witness.trust_level(checks, "different") == "likely"


def _pdf(pages: int) -> bytes:
    kids = " ".join(f"{3 + 2 * n} 0 R" for n in range(pages)).encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [%s] /Count %d >>"]
    objects[1] = objects[1] % (kids, pages)
    for n in range(pages):
        body = zlib.compress(b"0 0 0 RG 10 10 m 500 300 l S")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 400] /Contents %d 0 R >>"
            % (4 + 2 * n)
        )
        objects.append(
            b"<< /Filter /FlateDecode /Length %d >>\nstream\n" % len(body) + body + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        start,
    )
    return bytes(out)


def test_the_findings_go_through_the_real_book_and_checks_to_a_red_cloud(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / FILE).write_bytes(_pdf(2))
    listing = [{"name": FILE, "pages": 2, "kind": "the vendor's shop drawings"}]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    book = FindingBook(workspace=workspace, listing=listing, page_count=2)
    book.checker = lambda finding: apply_checks(finding, book.pdf_paths(), None)
    result = _check(["LA-41", "LA-42", "QZ-7"], ["LA-42", "QZ-9"], ["LA-42", "QZ-9"])
    assert len(result.findings) == 2
    for finding in result.findings:
        assert book.add_code_finding(finding, created_by=ai.CREATED_BY) is not None
    for stored in book.live():
        assert stored["paired_by"] == "code" and stored["text_proof"]["code"]
        assert stored["checks"]["text"] == "agrees"
        assert stored["witnesses"] == ["text"] and stored["trust_level"] == "likely"
        assert proof.proof_group(stored) == "clouded"
        assert proof.proof_line(stored).startswith("Code read the finish codes")
    changed = next(f for f in book.live() if f["text_proof"]["kind"] == "changed")
    assert proof.should_be(changed) == "QZ-7 (architect, page 1)"
    # The Not compared items reach the answer's list as set items.
    records = unknown_records([], None, set_gaps=[ai._unknown(1, "x", "not_comparable", "y")])
    assert records[0]["why"] == "not_comparable" and records[0]["scope"] == "set"


def test_a_lead_finding_of_its_own_is_not_given_the_text_witness() -> None:
    lead = {"created_by": "lead", "text_proof": {"code": "LA-41"}, "status": "different"}
    checked = apply_checks(lead, {}, None)
    assert checked["checks"]["text"] is None and "text" not in checked["witnesses"]


# ---- the set pack and the full check's hook ----------------------------------------------------------


def test_check_pack_reads_the_pack_files(tmp_path: Path) -> None:
    dims, page_map = _set(["LA-41", "LA-42"], None, ["LA-42"])
    folder = tmp_path / "pack"
    folder.mkdir()
    (folder / "dims.json").write_text(json.dumps(dims))
    (folder / "page_map.json").write_text(json.dumps(page_map))
    result = ai.check_pack(folder, read_texts=False)
    assert result is not None and len(result.findings) == 1
    assert ai.check_pack(tmp_path / "none") is None
    assert ai.check_pack(None) is None


class _Book:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.added: list[tuple[dict[str, Any], str]] = []
        self._live = live

    def live(self) -> list[dict[str, Any]]:
        return list(self._live)

    def add_code_finding(self, given: dict[str, Any], *, created_by: str) -> dict[str, Any]:
        self.added.append((given, created_by))
        return {**given, "id": f"F{len(self.added)}"}


class _Engine:
    def __init__(self, mode: str, live: list[dict[str, Any]]) -> None:
        self.settings = type("S", (), {"mode": mode, "products": ["cabinet"]})()
        workspace = type("W", (), {"set_pack": Path("pack"), "uploads": Path("up")})()
        self.tools = type("T", (), {"workspace": workspace})()
        self.book = _Book(live)
        self.finding_lock = threading.RLock()
        self.shown: list[dict[str, Any]] = []
        self.consistency_gaps: list[dict[str, Any]] = [{"page": 9, "why": "missing_round"}]
        self.items_line: str | None = None

    def _show_finding(self, finding: dict[str, Any]) -> None:
        self.shown.append(finding)


def test_the_hook_adds_new_findings_gaps_and_a_summary_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    made = _check(["LA-41", "LA-42", "LA-43"], None, ["LA-42"])
    made.unknowns = [{"page": 1, "why": "not_comparable"}]
    seen: dict[str, Any] = {}

    def fake(folder: Path | None, *, products: Sequence[str] | None = None) -> ai.ItemsResult:
        seen["products"] = products
        return made

    monkeypatch.setattr(ai, "check_pack", fake)
    # The lead already reported LA-43 on page 1: it is not added twice.
    engine = _Engine("full_check", [{"page": 1, "item": "Galley: finish LA-043 missing"}])
    ai.add_to_answer(engine)
    assert seen["products"] == ["cabinet"]
    assert [g["architect"] for g, by in engine.book.added] == ["LA-41"]
    assert {by for _g, by in engine.book.added} == {"architect_items"}
    assert [f["id"] for f in engine.shown] == ["F1"]
    assert engine.consistency_gaps == [
        {"page": 9, "why": "missing_round"},
        {"page": 1, "why": "not_comparable"},
    ]
    assert (engine.items_line or "").startswith("Architect's finishes (by code, $0): 3 architect")


def test_the_hook_runs_only_in_a_full_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "check_pack", lambda folder, products=None: _check(["LA-41"], None, []))
    engine = _Engine("chat", [])
    assert ai.add_to_answer(engine) is None
    assert engine.book.added == [] and engine.items_line is None


def test_the_hook_compares_only_the_messages_product_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.review.assistant.product_scope import ProductScope

    seen: dict[str, Any] = {}

    def fake(folder: Path | None, *, products: Sequence[str] | None = None) -> ai.ItemsResult:
        seen["products"] = products
        return ai.ItemsResult()

    monkeypatch.setattr(ai, "check_pack", fake)
    engine = _Engine("full_check", [])
    engine.product_scope = ProductScope.of(["countertop"])  # type: ignore[attr-defined]
    ai.add_to_answer(engine)
    assert seen["products"] == ["countertop"]


def test_the_summary_line_says_what_was_compared() -> None:
    assert ai.summary_line(ai.ItemsResult()) == (
        "Architect's finishes (by code, $0): no area has both drawings to compare."
    )
    line = ai.summary_line(_check(["LA-41", "LA-42"], None, ["LA-42"]))
    assert line == (
        "Architect's finishes (by code, $0): 2 architect finish codes compared in 1 area; "
        "1 missed or changed finish."
    )
