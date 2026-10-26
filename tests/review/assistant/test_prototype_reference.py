"""R1, the brand prototype as the reference (Anant, 2026-10-20, issue #1799): when the architect
gives NO elevation of an item, the brand prototype elevation is the reference (as GV's reviewer
does); the architect still governs wherever it gives information; with no prototype nothing
changes. The words say "reference: brand prototype". Made-up drawings and values only; no PDF,
no model call ($0)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import pytest

from app.review.assistant import classification, full_check_roster, proof
from app.review.assistant.chat_trust import apply_checks
from app.review.assistant.full_check_advocate import code_checks
from app.review.assistant.plan_check import architect_in_area
from app.review.assistant.simple_check import verify as v
from app.review.assistant.simple_check.verify import BlindAnswer, Item, Pack, Region
from app.review.assistant.simple_check.worklist import worklist_from

FILE = "set.pdf"
TOP = [0.0, 0.0, 500.0, 400.0]
BOTTOM = [0.0, 420.0, 500.0, 800.0]
WHOLE = [0.0, 0.0, 500.0, 800.0]
WORDS = classification.PROTOTYPE_REFERENCE_WORDS
ADVOCATE_PASSED = {
    "model": "devil's advocate",
    "result": "passed",
    "note": "a blind second pick chose the same label; code checked both ends",
    "answers": {"pairing": "advocate"},
}


# ---- the rule: classification and the proof's words ---------------------------------------------


def _finding(**changes: Any) -> dict[str, Any]:
    """A Wrong item code proved: both values printed at their spots, the pairing passed."""
    finding: dict[str, Any] = {
        "id": "F1",
        "page": 2,
        "item": "Bar: counter height",
        "category": "cabinet",
        "architect": "3'-0\"",
        "vendor": '34"',
        "status": "different",
        "source": "brand_prototype",
        "why": "the vendor prints 34 in, the other drawing 36 in",
        "trust_level": "confirmed",
        "witnesses": ["printed"],
        "checks": {"verify_vendor": "match", "verify_architect": "match"},
        "inches": {"architect": "36", "vendor": "34", "difference": "2"},
        "vendor_box": {"file": FILE, "page": 2, "x0": 10, "y0": 10, "x1": 20, "y1": 20},
        "arch_box": {"file": FILE, "page": 3, "x0": 10, "y0": 10, "x1": 20, "y1": 20},
        "vs_page": 3,
        "critic": ADVOCATE_PASSED,
    }
    finding.update(changes)
    return finding


@pytest.mark.usefixtures("read_twice")
def test_no_architect_elevation_makes_the_prototype_the_reference() -> None:
    marked = _finding(reference=classification.PROTOTYPE_REFERENCE)
    assert classification.prototype_governs(marked)
    assert proof.code_proved(marked)
    assert proof.proof_group(marked) == "clouded"
    found = proof.proof_of(marked)
    assert found["group"] == "clouded" and found["rule"] == "R1"
    assert WORDS in found["should_be"] and found["should_be"].startswith('36"')
    assert found["proof"].startswith("Reference: brand prototype")
    assert WORDS in proof.governs(marked)
    # Not proved: Not compared for its own reason, which still names the reference.
    unproved = _finding(reference=classification.PROTOTYPE_REFERENCE, critic=None)
    listed = proof.proof_of(unproved)
    assert listed["group"] == "not_compared" and listed["not_compared"] != "prototype"
    assert WORDS in listed["reason"]
    # Called the same: Right, compared with the prototype.
    same = _finding(reference=classification.PROTOTYPE_REFERENCE, status="same", vendor='36"')
    assert proof.proof_group(same) == "right"


@pytest.mark.usefixtures("read_twice")
def test_architect_and_prototype_disagree_the_architect_wins() -> None:
    # Without code's mark the architect gives the item: the prototype is reference only, never
    # red, however well it is read.
    plain = _finding()
    assert not classification.prototype_governs(plain)
    assert not proof.code_proved(plain)
    assert proof.proof_group(plain) == "not_compared"
    assert proof.proof_of(plain)["not_compared"] == "prototype"
    assert WORDS not in proof.governs(plain)
    # The architect's own comparison of the same item is the one that is red.
    architect = _finding(source="architect")
    assert proof.proof_group(architect) == "clouded"
    assert WORDS not in proof.proof_of(architect)["should_be"]
    # A mark on an architect comparison means nothing.
    assert not classification.prototype_reference(_finding(source="architect", reference="x"))


def test_no_prototype_is_as_before() -> None:
    architect = _finding(source="architect")
    assert classification.classify(architect, proved=True).rule == "R4"
    assert not proof.proof_line(architect).startswith("Reference")
    assert proof.proof_line(_finding(source="architect", witnesses=[], checks={}, critic=None)) == (
        "AI reading only: no code check backs it"
    )
    # "Per brand standard" in the architect's words still makes the prototype govern.
    pointed = _finding(why="the architect's elevation says per brand standard", critic=None)
    assert classification.classify(pointed, proved=False).reason != "prototype"


# ---- the page map decides: no architect drawing of the area -------------------------------------


def _page(page: int, halves: list[dict[str, Any]], area: str | None, **more: Any) -> dict[str, Any]:
    return {
        "file": FILE,
        "page": page,
        "halves": halves,
        "section": "1. WALL",
        "area": area,
        "area_from_page": None,
        **more,
    }


def _half(role: str, box: list[float], round_: int | None = None) -> dict[str, Any]:
    return {"box": box, "role": role, "heading": None, "round": round_}


def _map() -> dict[str, Any]:
    """Area 1 (Kitchen): the architect over the vendor, the prototype on the next page. Area 2
    (Bar): the vendor over the prototype, no architect drawing anywhere. Area 3 (Bench): the
    vendor alone."""
    return {
        "version": 5,
        "files": [FILE],
        "pages": [
            _page(1, [_half("architect", TOP), _half("vendor", BOTTOM, 1)], "1. Kitchen"),
            _page(2, [_half("brand_prototype", TOP)], "1. Kitchen"),
            _page(3, [_half("brand_prototype", TOP), _half("vendor", BOTTOM, 1)], "2. Bar"),
            _page(4, [_half("vendor", WHOLE, 1)], "3. Bench"),
        ],
    }


def _label(n: int, box: list[float], side: str, text: str, inches: str) -> dict[str, Any]:
    return {
        "id": f"D{n}",
        "text": text,
        "inches": inches,
        "box": box,
        "side": side,
        "route": "text",
    }


def _dims() -> dict[str, Any]:
    return {
        "version": 1,
        "files": [FILE],
        "pages": [
            {
                "file": FILE,
                "page": 1,
                "labels": [
                    _label(1, [100, 100, 130, 110], "architect", "3' - 0\"", "36"),
                    _label(2, [100, 500, 130, 510], "vendor", '34"', "34"),
                ],
                "phrases": [],
            },
            {
                "file": FILE,
                "page": 2,
                "labels": [_label(1, [100, 100, 130, 110], "brand", "3' - 2\"", "38")],
                "phrases": [],
            },
            {
                "file": FILE,
                "page": 3,
                "labels": [
                    _label(1, [100, 100, 130, 110], "brand", "3' - 0\"", "36"),
                    _label(2, [100, 500, 130, 510], "vendor", '34"', "34"),
                ],
                "phrases": [],
            },
            {
                "file": FILE,
                "page": 4,
                "labels": [_label(1, [100, 500, 130, 510], "vendor", '34"', "34")],
                "phrases": [],
            },
        ],
    }


def test_the_page_map_says_where_the_architect_gives_no_elevation() -> None:
    page_map = _map()
    assert architect_in_area(page_map, FILE, 1)  # the architect's own page
    assert not architect_in_area(page_map, FILE, 3)  # vendor and prototype only
    assert not architect_in_area(page_map, FILE, 4)
    # Code cannot tell: the architect governs, as before.
    assert architect_in_area(None, FILE, 3)
    assert architect_in_area(page_map, FILE, 99)
    # A page paired with an architect page in the plan check is compared with it.
    page_map["pages"][2]["pair_page"] = 1
    assert architect_in_area(page_map, FILE, 3)


def test_the_simple_work_list_compares_with_the_prototype_only_without_an_architect() -> None:
    work = worklist_from(_map(), dims=_dims(), main_file=FILE)
    by_id = {item.id: item for item in work.items}
    # No architect elevation of the Bar: the prototype is the reference.
    bar = by_id["p3.v1"]
    assert bar.prototype_reference
    assert [d.ref for d in bar.architect] == ["p3.b1"] and bar.reference == ()
    # The Kitchen has the architect's drawing: it governs; the prototype stays reference only.
    kitchen = by_id["p1.v1"]
    assert not kitchen.prototype_reference
    assert [d.ref for d in kitchen.architect] == ["p1.a1"]
    assert [d.ref for d in kitchen.reference] == ["p2.b1"]
    # No prototype and no architect: skipped, as before.
    assert "p4.v1" not in by_id
    assert any(s.page == 4 and "nothing to compare it with" in s.reason for s in work.skipped)
    how = {cover.page: cover.how for cover in work.pages}
    assert how[2] == "reference" and how[3] == "vendor"
    assert work.uncovered == ()


def _pack() -> Pack:
    return Pack.from_documents(
        main_file=FILE,
        dims=_dims(),
        page_map=_map(),
        pairs={"pairs": []},
        page_texts={"pages": []},
    )


def _pair(vendor: str, architect: str) -> dict[str, Any]:
    return {
        "vendor_label": vendor,
        "architect_label": architect,
        "measures": "counter height",
        "vendor_from": "floor",
        "vendor_to": "top of counter",
        "architect_from": "floor",
        "architect_to": "top of counter",
        "verdict": "different",
        "note": "",
    }


def _verify(item: Item, pair: dict[str, Any]) -> v.Verified:
    result = {"work_item": item.id, "pairs": [pair], "other_items": [], "unaccounted_labels": []}
    found = v.verify_answer(result, item, _pack(), ink=lambda label: (120, 0.0))
    assert len(found.items) == 1
    return found.items[0]


def _stored(finding: Mapping[str, Any]) -> dict[str, Any]:
    return apply_checks({"id": "F1", "created_by": v.CREATED_BY, **finding}, {}, None)


@pytest.mark.usefixtures("read_twice")
def test_simple_verify_proves_a_difference_from_the_reference_prototype() -> None:
    bar = Item(
        "p3.v1",
        Region(FILE, 3, tuple(BOTTOM), 1),  # type: ignore[arg-type]
        (Region(FILE, 3, tuple(TOP)),),  # type: ignore[arg-type]
        prototype_reference=True,
    )
    found = _verify(bar, _pair("p3.D2", "p3.D1"))
    assert found.outcome == "candidate", found
    roles = next(c for c in found.checks if c.name == "roles")
    assert roles.outcome == "pass"
    ends = found.ends
    blind = BlindAnswer(
        pick="p3.D1",
        verdict="same_thing",
        vendor_from=ends["vendor_from"],
        vendor_to=ends["vendor_to"],
        architect_from=ends["architect_from"],
        architect_to=ends["architect_to"],
        vendor_ink_black="yes",
    )
    red = v.with_blind(found, blind)
    assert red.outcome == "red"
    finding = red.finding
    assert finding["source"] == "brand_prototype"
    assert finding["reference"] == classification.PROTOTYPE_REFERENCE
    assert "brand prototype prints" in finding["why"]
    stored = _stored(finding)
    assert proof.proof_group(stored) == "clouded"
    assert proof.proof_line(stored).startswith("Reference: brand prototype")
    assert WORDS in proof.should_be(stored)


def test_simple_verify_keeps_the_prototype_reference_only_beside_an_architect() -> None:
    kitchen = Item(
        "p1.v1",
        Region(FILE, 1, tuple(BOTTOM), 1),  # type: ignore[arg-type]
        (Region(FILE, 1, tuple(TOP)), Region(FILE, 2, tuple(TOP))),  # type: ignore[arg-type]
    )
    grey = _verify(kitchen, _pair("p1.D2", "p2.D1"))
    assert grey.outcome == "not_compared" and grey.reason_code == "prototype"
    assert "reference" not in grey.finding
    assert proof.classified(_stored(grey.finding)).reason == "prototype"


# ---- the full check: code marks the findings, the advocate accepts the prototype's side ---------


class _Book:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.findings = {f["id"]: f for f in findings}

    def live(self) -> list[dict[str, Any]]:
        return [dict(f) for f in self.findings.values()]


class _Engine:
    def __init__(self, findings: list[dict[str, Any]]) -> None:
        self.book = _Book(findings)

    def _update_finding(self, finding_id: str, change: Callable[[dict[str, Any]], Any]) -> None:
        self.book.findings[finding_id] = change(dict(self.book.findings[finding_id]))


@pytest.mark.usefixtures("read_twice")
def test_the_full_check_marks_prototype_findings_from_the_page_map() -> None:
    no_architect = _finding(id="F1", page=3, vendor_box={"file": FILE, "page": 3})
    beside_architect = _finding(
        id="F2", page=1, vendor_box={"file": FILE, "page": 1}, reference="brand_prototype"
    )
    architect = _finding(id="F3", page=4, source="architect", vendor_box={"file": FILE, "page": 4})
    engine = _Engine([no_architect, beside_architect, architect])
    run = full_check_roster._Run(engine)  # type: ignore[arg-type]
    run._prototype_references(_map())
    found = engine.book.findings
    assert found["F1"]["reference"] == classification.PROTOTYPE_REFERENCE
    # A mark the page map does not back is cleared: the architect gives the Kitchen.
    assert "reference" not in found["F2"]
    assert "reference" not in found["F3"]
    assert proof.proof_group(found["F1"]) == "clouded"
    assert proof.proof_group(found["F2"]) == "not_compared"


def test_the_advocate_accepts_the_prototype_side_only_as_the_reference() -> None:
    vendor = {"id": "D2", "page": 3, "file": FILE, "box": [100, 500, 130, 510], "side": "vendor"}
    brand = {"id": "D1", "page": 3, "file": FILE, "box": [100, 100, 130, 110], "side": "brand"}
    for label in (vendor, brand):
        label["orientation"] = "v"

    def roles(**given: Any) -> str:
        checks = code_checks(vendor, brand, vendor_ink=(120, 0.0), **given)
        return next(c.outcome for c in checks if c.name == "roles")

    assert roles() == "fail"
    assert roles(prototype_reference=True) == "pass"
