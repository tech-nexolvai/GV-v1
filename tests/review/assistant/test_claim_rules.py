"""Claim rules (`claim_rules`) and the two new one-card folds (2026-10-25, the client-meeting job).

- A notch is cut on site (GV, 2026-10-23): an item about a notch is withdrawn, never red, never a
  Likely problem; an item that also names another subject (a sink) stays.
- Unit counts: the vendor's regular and reversed sheets are added up before comparing with the
  reference's total; the same is withdrawn, a missing other sheet is Not compared (never a Likely
  problem), a real difference stays.
- A wall outline is not the countertop: an L the reference draws at the wall's weight (or that
  code cannot find drawn as a top) is no claim; one drawn as a top stays.
- A side splash is not a mitred piece: the vendor's legend decides; no legend, no claim.
- One card per page for "pairing unsure"; one Likely problem per set for a material difference;
  a red is never folded.

Made-up drawings and values only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import claim_rules, one_card
from app.review.assistant.escalation import EscalationContext, escalate
from app.review.assistant.proof import classified, proof_group

VENDOR = "vendor.pdf"
REF = "reference.pdf"


def _box(file: str, page: int, box: tuple[float, float, float, float]) -> dict[str, Any]:
    x0, y0, x1, y1 = box
    return {"file": file, "page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1}


def _claim(fid: str, item: str, *, kind: str = "layout", **more: Any) -> dict[str, Any]:
    """A whole-drawing look's claim code could not prove: a Likely problem."""
    finding = {
        "id": fid,
        "page": 4,
        "vs_page": 9,
        "item": item,
        "vendor": more.pop("vendor", "see the drawing"),
        "architect": more.pop("architect", "see the drawing"),
        "status": "check",
        "source": "architect",
        "category": "other",
        "created_by": "whole_look",
        "vendor_box": _box(VENDOR, 4, (100, 100, 140, 120)),
        "arch_box": _box(REF, 9, (100, 100, 200, 200)),
        "whole_look": {
            "by": "whole_drawing_look",
            "kind": kind,
            "proof": "likely_only",
            "sure": "likely",
            "words": f"Please check and confirm: {item}.",
        },
    }
    finding.update(more)
    return finding


def _changes(
    findings: list[dict[str, Any]],
    texts: dict[tuple[str, int], str] | None = None,
    outlines: Any = None,
) -> dict[str, claim_rules.Change]:
    return dict(
        claim_rules.changes(findings, page_texts=texts or {}, main_file=VENDOR, outlines=outlines)
    )


# ---- 1. notches ---------------------------------------------------------------------------------


def test_a_notch_claim_is_withdrawn_by_gvs_rule() -> None:
    claim = _claim("F1", "Top: notched right end, notch depth unknown")
    assert proof_group(claim) == "likely_problem"
    found = _changes([claim])["F1"]
    assert found.outcome == "withdrawn" and found.rule == "notch"
    assert "cut on site" in found.why


def test_a_notch_item_that_also_names_a_sink_or_was_decided_stays() -> None:
    sink = _claim("F1", "Sink model and notch at the left end")
    decided = _claim("F2", "Top: notch at the left end", decision="agree")
    right = _claim("F3", "Top: notch at the left end", status="same")
    right.pop("whole_look")
    assert _changes([sink, decided, right]) == {}


# ---- 2. unit counts -----------------------------------------------------------------------------

_REFERENCE_LIST = (
    "QTY.:\nUnit Type Quantity\nAB-CD // AB-CD-Reverse (ADA), 4 units;\n"
    "EF-CD-Reverse (ADA), 1 units;\nGH2-PH (ADA), 1 units;\n"
)
_REGULAR = "DRAWING # 4\nVANITY\nREGULAR UNITS\nAB-CD - 2 UNITS\nGH2-PH - 1 UNIT\n"
_REVERSED = "DRAWING # 5\nVANITY\nREVERSED UNITS\nAB-CD-R - 2 UNITS\nEF-CD-R - 1 UNIT\n"
#: The page before: another item's reversed sheet, whose units are not on the reference's list.
_OTHER = "DRAWING # 3\nKITCHEN\nREVERSED UNITS\nXY-CD-R - 9 UNITS\n"


def _count_claim() -> dict[str, Any]:
    return _claim(
        "F1",
        "Unit quantities on this drawing",
        kind="count",
        vendor="AB-CD 2 units, GH2-PH 1 (3 units)",
        architect="AB-CD // AB-CD-Reverse 4 units, EF-CD-Reverse 1, GH2-PH 1 (6 units)",
    )


def test_counts_over_the_regular_and_reversed_sheets_that_add_up_withdraw_the_claim() -> None:
    texts = {(REF, 9): _REFERENCE_LIST, (VENDOR, 3): _OTHER, (VENDOR, 4): _REGULAR}
    texts[(VENDOR, 5)] = _REVERSED
    found = _changes([_count_claim()], texts)["F1"]
    assert found.outcome == "withdrawn" and found.rule == "unit_counts"
    assert "page 4 + page 5" in found.why and "6 units" in found.why


def test_counts_that_still_differ_after_adding_up_stay_a_likely_problem() -> None:
    reversed_short = _REVERSED.replace("AB-CD-R - 2 UNITS", "AB-CD-R - 1 UNIT")
    texts = {(REF, 9): _REFERENCE_LIST, (VENDOR, 4): _REGULAR, (VENDOR, 5): reversed_short}
    assert _changes([_count_claim()], texts) == {}
    # One sheet with no regular / reversed heading is compared on its own, as before.
    single = "DRAWING # 4\nAB-CD - 3 UNITS\nEF-CD-R - 1 UNIT\nGH2-PH - 1 UNIT\n"
    assert _changes([_count_claim()], {(REF, 9): _REFERENCE_LIST, (VENDOR, 4): single}) == {}


def test_a_count_code_cannot_add_up_is_not_compared_never_a_likely_problem() -> None:
    claim = _count_claim()
    texts = {(REF, 9): _REFERENCE_LIST, (VENDOR, 4): _REGULAR}  # the reversed sheet is missing
    found = _changes([claim], texts)["F1"]
    assert found.outcome == "not_compared"
    held = {**claim, claim_rules.FIELD: found.record()}
    assert proof_group(held) == "not_compared"
    assert "regular units on page 4" in str(classified(held).why)
    assert claim_rules.held(held) == found.why


def test_the_escalation_withdraws_or_holds_back_through_the_rules() -> None:
    texts = {(REF, 9): _REFERENCE_LIST, (VENDOR, 4): _REGULAR, (VENDOR, 5): _REVERSED}
    notch = _claim("F2", "Top: notch at the open end")
    result = escalate(
        [_count_claim(), notch], EscalationContext(main_file=VENDOR, page_texts=texts)
    )
    assert set(result.absorbed) == {"F1", "F2"}
    assert result.settled["claim_rules"] == 2
    held = escalate(
        [_count_claim()],
        EscalationContext(
            main_file=VENDOR, page_texts={(REF, 9): _REFERENCE_LIST, (VENDOR, 4): _REGULAR}
        ),
    )
    assert not held.absorbed
    assert proof_group(held.changed[0]) == "not_compared"


# ---- 3. a wall outline is not the countertop ----------------------------------------------------


def _l_claim() -> dict[str, Any]:
    return _claim(
        "F1",
        "Vanity: top shape / side return",
        vendor="plain rectangle top",
        architect="L-shaped top with a return at the right end (plan)",
    )


@pytest.mark.parametrize(
    ("seen", "outcome"), [("wall", "withdrawn"), (None, "withdrawn"), ("top", None)]
)
def test_an_l_shaped_top_stands_only_when_code_sees_it_drawn_as_a_top(
    seen: str | None, outcome: str | None
) -> None:
    asked: list[Any] = []

    def outlines(file: str, page: int, box: Any) -> str | None:
        asked.append((file, page, box))
        return seen

    found = _changes([_l_claim()], outlines=outlines).get("F1")
    assert asked == [(REF, 9, (100.0, 100.0, 200.0, 200.0))]
    assert (found.outcome if found else None) == outcome
    if found is not None:
        assert found.rule == "wall_outline"
        assert ("wall's weight" in found.why) == (seen == "wall")


def _pdf(content: str) -> bytes:
    """A one-page 300 x 300 PDF drawing `content` (a content stream)."""
    stream = content.encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for at in offsets:
        out += b"%010d 00000 n \n" % at
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


#: Thin drawing lines (the page's usual weight), each its own path.
_THIN = "".join(f"0.12 w {10 + i} 20 m {10 + i} 60 l S\n" for i in range(30))
#: An L outline (PDF y up: on the page it spans x 100-200, y 100-200 from the top).
_L = "{w} w 100 100 m 200 100 l 200 150 l 150 150 l 150 200 l 100 200 l h S\n"


@pytest.mark.parametrize(
    ("drawing", "seen"),
    [
        (_THIN + _L.format(w="1.08"), "wall"),  # drawn at the page's heaviest weight
        (_THIN + _L.format(w="0.36") + "1.08 w 10 250 m 60 250 l S\n", "top"),  # lighter
        (_THIN + "1.08 w 100 100 m 200 100 l 200 200 l 100 200 l h S\n", None),  # a rectangle
    ],
)
def test_the_outline_reader_tells_a_wall_from_a_top_by_line_weight(
    tmp_path: Path, drawing: str, seen: str | None
) -> None:
    (tmp_path / REF).write_bytes(_pdf(drawing))
    reader = claim_rules.OutlineReader(lambda file: tmp_path / file)
    assert reader(REF, 1, (100.0, 100.0, 200.0, 200.0)) == seen
    assert reader(REF, 1, (0.0, 0.0, 40.0, 40.0)) is None  # nothing L-shaped there
    assert claim_rules.OutlineReader(lambda file: None)(REF, 1, (0, 0, 1, 1)) is None


# ---- 4. a side splash is not a mitred piece -------------------------------------------------------

_LEGEND = 'LEGEND\nV = EASED & POLISHED\nM1.5 = MITERED EDGE\n1 1/2" [40mm]\n'


def test_a_mitred_piece_claim_that_is_not_the_legends_mitre_is_withdrawn() -> None:
    claim = _claim(
        "F1",
        'Countertop: 4" x 25" mitered pieces at both ends',
        kind="extra_piece",
        vendor='2 end pieces, 4" x 25"',
    )
    found = _changes([claim], {(VENDOR, 4): _LEGEND})["F1"]
    assert found.outcome == "withdrawn" and found.rule == "side_splash"
    assert 'M1.5 (built up to 1 1/2")' in found.why
    # No mitre in the legend: code cannot tell them apart, so no claim.
    assert "no claim" in _changes([claim], {(VENDOR, 4): "LEGEND\n"})["F1"].why


def test_a_piece_of_the_legends_mitre_size_or_a_helpers_item_stays() -> None:
    edge = _claim(
        "F1",
        'Countertop: 1 1/2" mitered apron pieces at both ends',
        kind="extra_piece",
        vendor='2 aprons, 1 1/2" x 25"',
    )
    helper = _claim("F2", "Mitred ends at both ends", created_by="helper:3")
    helper.pop("whole_look")
    assert _changes([edge, helper], {(VENDOR, 4): _LEGEND}) == {}
    assert claim_rules.mitre_build_up(_LEGEND) == ("M1.5", 1.5)


# ---- one card per page, one material difference per set -------------------------------------------


def _unpaired(fid: str, box: tuple[float, float, float, float], **more: Any) -> dict[str, Any]:
    finding = {
        "id": fid,
        "page": 7,
        "item": more.pop("item", "Kitchen: sink, splash, finish"),
        "vendor": more.pop("vendor", "sink K-1, full splash"),
        "architect": "no agreed pairing (reference p3 or p5)",
        "status": "check",
        "source": "architect",
        "category": "countertop",
        "vendor_box": _box(VENDOR, 7, box),
        "reference_page": {
            "why": "No architect page is paired with vendor page 7.",
            "file": None,
            "page": None,
            "pair": [],
        },
    }
    finding.update(more)
    return finding


def test_a_whole_page_placeholder_folds_into_the_item_it_holds_on_an_unpaired_page() -> None:
    page = _unpaired("F1", (0, 0, 842, 595), item="Kitchen: drawn items", vendor="not read")
    item = _unpaired("F5", (300, 60, 500, 250))
    elsewhere = _unpaired("F6", (300, 60, 500, 250), page=8)
    assert proof_group(page) == "not_compared"
    folded = one_card.fold([page, item, elsewhere])
    assert [(f.keep, f.drop) for f in folded.folded] == [("F5", "F1")]
    kept = next(k for k in folded.kept if k["id"] == "F5")
    assert [c["finding_id"] for c in kept["same_item"]["copies"]] == ["F1"]
    assert proof_group(kept) == "not_compared"
    # Another record of the pairing (another page's), or a spot side by side: both stay.
    other = _unpaired("F7", (300, 60, 500, 250))
    other["reference_page"] = {**other["reference_page"], "why": "No architect page is paired."}
    beside = _unpaired("F8", (500, 300, 700, 500))
    assert one_card.fold([page, other]).folded == []
    assert one_card.fold([beside, item]).folded == []


def _material(fid: str, page: int, *, likely: bool = True, **more: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": fid,
        "page": page,
        "vs_page": page + 10,
        "item": "Countertop material",
        "vendor": "2cm Example Bianco stone",
        "architect": "Solid surface countertop SS 01 (section)",
        "status": "check",
        "source": "architect",
        "category": "countertop",
        "created_by": "whole_look" if likely else f"helper:{page}",
        "vendor_box": _box(VENDOR, page, (100, 100, 300, 200)),
    }
    if likely:
        finding["whole_look"] = {
            "kind": "layout",
            "proof": "likely_only",
            "words": "Please check and confirm: Countertop material.",
        }
    finding.update(more)
    return finding


def test_one_material_difference_is_one_likely_problem_for_the_whole_set() -> None:
    copies = [
        _material("F2", 5, likely=False, architect="solid surface countertop (section note)"),
        _material("F7", 7),
        _material("F9", 9),
        _material("F12", 12, vendor="Example Bianco, no SS tag", item="Makeup counter: finish tag"),
    ]
    assert [proof_group(f) for f in copies] == [
        "not_compared",
        "likely_problem",
        "likely_problem",
        "likely_problem",
    ]
    folded = one_card.fold(copies)
    assert sorted(f.drop for f in folded.folded) == ["F12", "F2", "F9"]
    (card,) = folded.kept
    assert card["id"] == "F7" and proof_group(card) == "likely_problem"
    assert "whole set, vendor pages 5, 7, 9 and 12" in card["item"]
    assert "pages 5, 7, 9 and 12" in card["whole_look"]["words"]
    assert sorted(c["finding_id"] for c in card["same_item"]["copies"]) == ["F12", "F2", "F9"]


def test_material_fold_never_hides_a_red_and_needs_a_likely_problem_to_carry_it() -> None:
    # Only Not-compared copies: nothing carries them, each stays.
    helpers = [_material("F2", 5, likely=False), _material("F3", 6, likely=False)]
    assert one_card.fold(helpers).folded == []
    # The vendor names the reference's material: no difference claimed, never folded.
    same = _material("F4", 8, vendor="Solid surface SS 01")
    assert one_card.fold([_material("F7", 7), same]).folded == []
    # One page only: no set-wide card (the same page pair's copies fold as before, by tags).
    (only,) = one_card.fold([_material("F7", 7), _material("F8", 7)]).folded
    assert "finish tags" in only.reason


def test_a_red_copy_stays_its_own_card(monkeypatch: pytest.MonkeyPatch) -> None:
    red = _material("F1", 4)
    real = proof_group

    def group(finding: Any) -> Any:
        return "clouded" if finding.get("id") == "F1" else real(finding)

    monkeypatch.setattr(one_card.proof, "proof_group", group)
    folded = one_card.fold([red, _material("F7", 7), _material("F9", 9)])
    assert [(f.keep, f.drop) for f in folded.folded] == [("F7", "F9")]


# ---- from the independent review ------------------------------------------------------------------


def test_a_size_beside_a_notch_or_a_count_of_other_things_is_never_taken_away() -> None:
    size = _claim("F1", "Countertop depth at the column notch", status="different")
    drawers = _claim("F2", "Drawer count on this run", kind="count")
    texts = {(REF, 9): _REFERENCE_LIST, (VENDOR, 4): _REGULAR, (VENDOR, 5): _REVERSED}
    assert _changes([size, drawers], texts) == {}


@pytest.mark.parametrize("written", ['1-1/2"', '1.5"', '1 1/2"'])
def test_the_legends_mitre_size_is_read_however_it_is_written(written: str) -> None:
    edge = _claim(
        "F1",
        f"Countertop: {written} mitered apron pieces at both ends",
        kind="extra_piece",
        vendor=f"2 aprons, {written} x 25 in",
    )
    assert _changes([edge], {(VENDOR, 4): _LEGEND}) == {}


def test_two_materials_with_different_tags_stay_two_cards_and_a_refold_keeps_its_words() -> None:
    laminate = [
        _material(
            "F1", 4, item="Door finish", architect="Plastic laminate PL-1", vendor="Walnut stain"
        ),
        _material(
            "F2", 5, item="Door finish", architect="Plastic laminate PL-1", vendor="Walnut stain"
        ),
        _material(
            "F3", 6, item="Countertop material", architect="Laminate PL-2", vendor="Walnut stain"
        ),
        _material(
            "F4", 7, item="Countertop material", architect="Laminate PL-2", vendor="Walnut stain"
        ),
    ]
    folded = one_card.fold(laminate)
    assert sorted((f.keep, f.drop) for f in folded.folded) == [("F1", "F2"), ("F3", "F4")]
    # A Continue folds a new page's copy into the card: one "whole set" ending, every page.
    card = next(k for k in folded.kept if k["id"] == "F1")
    again = one_card.fold([card, _material("F9", 9, item="Door finish", architect="PL-1 laminate")])
    (kept,) = again.kept
    assert kept["item"] == "Door finish (the whole set, vendor pages 4, 5 and 9)"
    assert kept["whole_look"]["words"].count("The same on") == 1


def test_nested_unpaired_spots_fold_into_the_one_that_stays() -> None:
    page = _unpaired("F1", (0, 0, 842, 595), item="Kitchen: drawn items", vendor="not read")
    middle = _unpaired("F2", (100, 50, 600, 400))
    inner = _unpaired("F3", (300, 60, 500, 250))
    folded = one_card.fold([page, middle, inner])
    assert sorted((f.keep, f.drop) for f in folded.folded) == [("F3", "F1"), ("F3", "F2")]
