"""Findings, coverage, evidence crops, the marked-up PDF and the trust report (chat v2 / v3).

All $0: real tiny PDFs made here with reportlab, drawn and read with pypdfium2 and pypdf.
"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from PIL import Image
from pypdf import PdfReader
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant.chat_findings import (
    DROP_FINDING,
    REPORT_FINDING,
    SET_COVERAGE,
    UPDATE_FINDING,
    FindingBook,
    coverage_note,
    live_findings,
    make_markup_file,
    page_ranges,
)
from app.review.assistant.chat_trust import (
    findings_csv,
    recorded_from,
    round_statuses,
    trust_level,
    trust_report,
)
from app.review.assistant.sandbox import ChatWorkspace, SandboxRefused
from vocabulary.product_registry import registry

WIDTH, HEIGHT = 612.0, 792.0
#: Where the test PDF prints its numbers (points, origin top-left): page 1 architect, page 2 vendor.
ARCH_BOX = {"page": 1, "x0": 100, "y0": 80, "x1": 160, "y1": 96}
VENDOR_BOX = {"page": 2, "x0": 300, "y0": 400, "x1": 360, "y1": 416}


def make_pdf(pages: int = 3) -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(WIDTH, HEIGHT), invariant=1)
    for n in range(1, pages + 1):
        c.setFont("Helvetica", 12)
        if n == 1:
            c.drawString(102, HEIGHT - 93, '37"')
        if n == 2:
            c.drawString(302, HEIGHT - 413, "3'-10\"")
        c.drawString(40, 40, f"sheet {n}")
        c.showPage()
    c.save()
    return out.getvalue()


def workspace_with_pdf(tmp_path: Path, pages: int = 3) -> ChatWorkspace:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    (workspace.uploads / "Vendor set.pdf").write_bytes(make_pdf(pages))
    listing = [
        {"name": "Vendor set.pdf", "pages": pages, "kind": "the vendor's shop drawings"},
    ]
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    return workspace


def book(tmp_path: Path, pages: int = 3) -> FindingBook:
    workspace = workspace_with_pdf(tmp_path, pages)
    listing = json.loads((workspace.root / "uploads.json").read_text())
    return FindingBook(workspace=workspace, listing=listing, page_count=pages)


def finding_input(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "page": 2,
        "vs_page": 1,
        "item": "Vanity top length",
        "category": "countertop",
        "architect": '37"',
        "vendor": "3'-10\"",
        "status": "different",
        "why": "Top 9 in longer than the design",
        "verified": True,
        "arch_box": dict(ARCH_BOX),
        "vendor_box": dict(VENDOR_BOX),
        "source": "architect",
    }
    base.update(changes)
    return base


# ---- the tools' schemas --------------------------------------------------------------------------


def test_the_tool_schemas_are_strict() -> None:
    for tool in (REPORT_FINDING, UPDATE_FINDING, DROP_FINDING, SET_COVERAGE):
        assert tool["input_schema"]["additionalProperties"] is False
    report = REPORT_FINDING["input_schema"]
    # The two values may come from numbered labels (architect_dim / vendor_dim, #1558); code
    # still refuses a finding with neither a value nor a label.
    assert set(report["required"]) == {
        "page",
        "item",
        "category",
        "status",
        "verified",
        "source",
    }
    assert {"architect_dim", "vendor_dim"} <= set(report["properties"])
    props = report["properties"]
    # #1547: every active product of the registry, then "other".
    assert props["category"]["enum"][:2] == ["countertop", "cabinet"]
    assert props["category"]["enum"] == [*registry().active_ids(), "other"]
    assert props["status"]["enum"] == ["different", "check", "same"]
    assert props["source"]["enum"] == [
        "architect",
        "brand_prototype",
        "rules",
        "vendor_arithmetic",
        "gv_review",
    ]
    assert props["arch_box"]["additionalProperties"] is False
    assert {"parts", "printed_total", "rule_check_id", "round"} <= set(props)
    assert UPDATE_FINDING["input_schema"]["required"] == ["finding_id"]
    assert DROP_FINDING["input_schema"]["required"] == ["finding_id", "reason"]
    page = SET_COVERAGE["input_schema"]["properties"]["pages"]["items"]
    assert page["properties"]["status"]["enum"] == [
        "same",
        "different",
        "not_comparable",
        "not_opened",
    ]


@pytest.mark.parametrize(
    ("changes", "words"),
    [
        ({"category": "sink"}, "category must be one of"),
        ({"status": "wrong"}, "status must be one of"),
        ({"source": "guess"}, "source must be one of"),
        ({"page": 0}, "page must be a whole number"),
        ({"page": 9}, "not in the vendor's file"),
        ({"page": True}, "page must be a whole number"),
        ({"item": "  "}, "item is empty"),
        ({"verified": "yes"}, "verified must be true or false"),
        ({"vendor_box": {"page": 1, "x0": 1, "y0": 1, "x1": "a", "y1": 2}}, "must be a number"),
        ({"vendor_box": {"page": 1, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "z": 1}}, "unknown field"),
        ({"arch_box": {**ARCH_BOX, "file": "other.pdf"}}, "there is no upload"),
        ({"colour": "red"}, "unknown argument"),
        ({"round": 0}, "round must be a whole number"),
    ],
)
def test_a_bad_finding_is_refused_plainly(
    tmp_path: Path, changes: dict[str, Any], words: str
) -> None:
    with pytest.raises(SandboxRefused, match=words):
        book(tmp_path).report(finding_input(**changes), "lead")


def test_a_missing_field_is_refused(tmp_path: Path) -> None:
    given = finding_input()
    del given["vendor"]
    with pytest.raises(SandboxRefused, match="missing argument 'vendor'"):
        book(tmp_path).report(given, "lead")


# ---- crops ---------------------------------------------------------------------------------------


def test_a_finding_gets_evidence_crops_from_the_original_page(tmp_path: Path) -> None:
    findings = book(tmp_path)
    finding, notes = findings.report(finding_input(), "lead")

    assert finding["id"] == "F1" and notes == []
    assert finding["arch_box"]["file"] == "Vendor set.pdf"
    for key in ("arch_picture_id", "vendor_picture_id"):
        path = findings.workspace.pictures / f"{finding[key]}.png"
        picture = Image.open(path)
        # The box (60 x 16 points) padded by 24 points a side, at 170 dpi.
        width, height = picture.size
        assert abs(width - 108 * 170 / 72) <= 2 and abs(height - 64 * 170 / 72) <= 2
        # The printed number is in the crop: dark pixels.
        assert min(picture.convert("L").tobytes()) < 100


def test_a_box_off_the_page_or_missing_has_no_picture_and_no_error(tmp_path: Path) -> None:
    findings = book(tmp_path)
    off = {"page": 1, "x0": 900, "y0": 900, "x1": 950, "y1": 950}
    finding, notes = findings.report(
        finding_input(arch_box=off, vendor_box={**VENDOR_BOX, "page": 3}), "lead"
    )
    assert finding["arch_picture_id"] is None
    assert finding["vendor_picture_id"] is not None
    assert notes == ["architect box: no picture (the box is not on page 1 of Vendor set.pdf)"]

    given = finding_input()
    del given["arch_box"], given["vendor_box"]
    bare, notes = findings.report(given, "lead")
    assert bare["arch_picture_id"] is None and bare["vendor_picture_id"] is None and notes == []

    beyond = {"page": 7, "x0": 1, "y0": 1, "x1": 20, "y1": 20}
    far, notes = findings.report(finding_input(vendor_box=beyond), "lead")
    assert far["vendor_picture_id"] is None and "not on page 7" in notes[0]


def test_a_big_box_is_drawn_at_a_lower_dpi_not_cut(tmp_path: Path) -> None:
    findings = book(tmp_path)
    whole = {"page": 1, "x0": 0, "y0": 0, "x1": WIDTH, "y1": HEIGHT}
    finding, _ = findings.report(finding_input(arch_box=whole), "lead")
    picture = Image.open(findings.workspace.pictures / f"{finding['arch_picture_id']}.png")
    assert max(picture.size) <= 1600


# ---- helpers, updates, drops ---------------------------------------------------------------------


def test_a_helpers_finding_is_never_verified(tmp_path: Path) -> None:
    findings = book(tmp_path)
    finding, _ = findings.report(finding_input(verified=True), "helper:2")
    assert finding["verified"] is False and finding["created_by"] == "helper:2"
    assert [f["id"] for f in findings.by("helper:2")] == ["F1"]


def test_update_changes_fields_and_redraws_a_moved_box(tmp_path: Path) -> None:
    findings = book(tmp_path)
    first, _ = findings.report(finding_input(), "helper:1")
    moved = {**VENDOR_BOX, "x0": 290}
    updated, _ = findings.update({"finding_id": "F1", "verified": True, "vendor_box": moved})

    assert updated["verified"] is True and updated["vendor_box"]["x0"] == 290
    assert updated["vendor_picture_id"] != first["vendor_picture_id"]
    assert updated["arch_picture_id"] == first["arch_picture_id"]
    assert updated["created_by"] == "helper:1"
    cleared, _ = findings.update({"finding_id": "F1", "why": None})
    assert cleared["why"] is None
    with pytest.raises(SandboxRefused, match="no finding F9"):
        findings.update({"finding_id": "F9", "verified": True})
    with pytest.raises(SandboxRefused, match="item must be text"):
        findings.update({"finding_id": "F1", "item": None})
    with pytest.raises(SandboxRefused, match="at least one field"):
        findings.update({"finding_id": "F1"})


def test_drop_removes_a_finding_and_it_cannot_come_back(tmp_path: Path) -> None:
    findings = book(tmp_path)
    findings.report(finding_input(), "lead")
    findings.report(finding_input(item="Sink centre"), "lead")
    assert findings.drop({"finding_id": "F1", "reason": "misread the 3"}) == ("F1", "misread the 3")
    assert [f["id"] for f in findings.live()] == ["F2"]
    with pytest.raises(SandboxRefused, match="was dropped"):
        findings.update({"finding_id": "F1", "verified": True})
    with pytest.raises(SandboxRefused, match="reason is empty"):
        findings.drop({"finding_id": "F2", "reason": " "})


# ---- coverage ------------------------------------------------------------------------------------


def test_coverage_merges_and_counts_every_page(tmp_path: Path) -> None:
    findings = book(tmp_path, pages=5)
    findings.set_coverage(
        {"pages": [{"page": 1, "status": "same"}, {"page": 2, "status": "different"}]}, "lead"
    )
    coverage = findings.set_coverage(
        {
            "pages": [
                {"page": 2, "pair_page": 1, "status": "same"},
                {"page": 3, "status": "not_comparable", "note": "cover sheet"},
                {"page": 4, "status": "not_opened"},
            ]
        },
        "helper:1",
    )
    assert coverage["counts"] == {
        "total": 5,
        "same": 2,
        "different": 0,
        "not_comparable": 1,
        "not_opened": 2,
    }
    assert [entry["page"] for entry in coverage["pages"]] == [1, 2, 3, 4, 5]
    assert coverage["pages"][1] == {
        "page": 2,
        "pair_page": 1,
        "status": "same",
        "note": None,
        "by": "helper:1",
    }
    assert coverage["pages"][4]["by"] is None
    # Not opened with no note, or never recorded: not accounted for.
    assert findings.coverage_gaps() == [4, 5]
    findings.set_coverage({"pages": [{"page": 4, "status": "not_opened", "note": "blank"}]}, "lead")
    assert findings.coverage_gaps() == [5]
    with pytest.raises(SandboxRefused, match="not in the vendor's file"):
        findings.set_coverage({"pages": [{"page": 6, "status": "same"}]}, "lead")
    with pytest.raises(SandboxRefused, match="status must be one of"):
        findings.set_coverage({"pages": [{"page": 1, "status": "done"}]}, "lead")


def test_page_ranges_and_the_guard_note() -> None:
    assert page_ranges([9, 3, 4, 5, 11, 12]) == "3-5, 9, 11-12"
    note = coverage_note([3, 4, 9], "Vendor set.pdf")
    assert note.startswith("Coverage check: pages 3-4, 9 of Vendor set.pdf")


# ---- the marked-up PDF ---------------------------------------------------------------------------


def _red_pixels(pdf: Path, page: int, box: tuple[float, float, float, float]) -> int:
    """Strongly red pixels in an area (points, origin top-left) of a page rendered at 72 dpi."""
    document = pdfium.PdfDocument(str(pdf))
    try:
        picture = document[page - 1].render(scale=1).to_pil().convert("RGB")
    finally:
        document.close()
    x0, y0, x1, y1 = (int(v) for v in box)
    area = picture.crop((max(0, x0), max(0, y0), x1, y1))
    raw = area.tobytes()
    return sum(
        1
        for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True)
        if r > 180 and g < 90 and b < 90
    )


def test_the_marked_up_pdf_has_clouds_at_the_boxes_notes_and_the_stamp(tmp_path: Path) -> None:
    findings = book(tmp_path)
    findings.report(finding_input(), "lead")
    no_box = finding_input(item="Backsplash height")
    del no_box["arch_box"], no_box["vendor_box"]
    findings.report(no_box, "lead")
    arch_only = finding_input(item="Sink model")
    del arch_only["vendor_box"]
    findings.report(arch_only, "lead")
    out = tmp_path / "out.pdf"

    made = make_markup_file(findings.workspace, findings.live(), out)

    assert made.name == "Vendor set - AI markup.pdf" and made.size == out.stat().st_size
    reader = PdfReader(str(out))
    assert len(reader.pages) == 4  # the three pages and a notes page
    # A cloud at the vendor box (page 2) and at the architect box of the third finding (page 1),
    # and none where nothing was reported.
    x0, y0, _x1, y1 = (VENDOR_BOX[k] for k in ("x0", "y0", "x1", "y1"))
    assert _red_pixels(out, 2, (x0 - 14, y0 - 14, x0 - 4, y1 + 14)) > 5
    a0, b0, _a1, b1 = (ARCH_BOX[k] for k in ("x0", "y0", "x1", "y1"))
    assert _red_pixels(out, 1, (a0 - 14, b0 - 4, a0 - 4, b1 + 4)) > 5
    assert _red_pixels(out, 3, (200, 200, 500, 600)) == 0
    page2 = reader.pages[1].extract_text()
    # Numbered biggest first (#1569): the sink (a cut-out) and the height come before a length.
    assert "\u00d7 3" in page2 and 'Vanity top length: vendor 3\'-10", make it 37"' in page2
    assert "AI markup: draft, not GV's signed review" in reader.pages[0].extract_text()
    notes = reader.pages[3].extract_text()
    for words in (
        "\u00d7 1",
        "\u00d7 2",
        "\u00d7 3",
        "Backsplash height",
        "Not marked on the drawing: no box",
        "On the drawing (2)",
        "Not on the drawing (1)",
    ):
        assert words in notes
    # The upload itself is untouched.
    assert (findings.workspace.uploads / "Vendor set.pdf").read_bytes() == make_pdf(3)


def test_live_findings_leave_out_dropped_ones() -> None:
    blocks = [
        {"type": "text", "text": "x"},
        {"type": "finding", "finding": {"id": "F1"}},
        {"type": "finding_dropped", "finding_id": "F2", "reason": "wrong"},
        {"type": "finding", "finding": {"id": "F3"}},
    ]
    assert [f["id"] for f in live_findings(blocks)] == ["F1", "F3"]


# ---- trust ---------------------------------------------------------------------------------------


def _checks(**changes: Any) -> dict[str, Any]:
    base = {
        "printed_architect": "not_run",
        "printed_vendor": "not_run",
        "second_read": "not_run",
        "rule": None,
        "arithmetic": None,
        "lead_checked": False,
    }
    return {**base, **changes}


@pytest.fixture
def no_checks_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks builder's module absent (this engine's own trust rule decides)."""
    from app.review.assistant import chat_trust

    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)


def test_trust_levels_follow_contract3(no_checks_module: None) -> None:
    printed = {"printed_architect": "match", "printed_vendor": "match"}
    assert trust_level(_checks(**printed, second_read="agrees")) == "confirmed"
    # A non-AI witness and the lead's own check on the drawing.
    assert trust_level(_checks(**printed, lead_checked=True)) == "confirmed"
    assert trust_level(_checks(**printed)) == "likely"
    # Two AI readings agreeing, no non-AI witness: Likely at best.
    assert trust_level(_checks(second_read="agrees", lead_checked=True)) == "likely"
    assert trust_level(_checks(lead_checked=True)) == "to_check"
    assert trust_level(_checks(**printed, second_read="disagrees", lead_checked=True)) == "to_check"
    assert trust_level(_checks(rule="agrees", second_read="agrees")) == "confirmed"
    assert trust_level(_checks(rule="disagrees", second_read="agrees")) == "to_check"
    sums = _checks(arithmetic="does_not_add_up", second_read="agrees")
    assert trust_level(sums, "different") == "confirmed"
    assert trust_level(_checks(arithmetic="adds_up"), "different") == "to_check"


def test_round_statuses_link_the_same_item_across_rounds() -> None:
    findings = [
        {"id": "F1", "item": "Vanity top length", "status": "different", "round": 1},
        {"id": "F2", "item": "vanity  top length", "status": "same", "round": 2},
        {"id": "F3", "item": "Sink centre", "status": "different", "round": 1},
        {"id": "F4", "item": "Sink centre", "status": "different", "round": 2},
        {"id": "F5", "item": "Backsplash", "status": "different", "round": 1},
        {"id": "F6", "item": "Filler", "status": "different", "round": 2},
    ]
    assert round_statuses(findings) == {
        "F1": "fixed_in_later_round",
        "F2": "fixed_in_later_round",
        "F3": "still_wrong",
        "F4": "still_wrong",
        "F5": "not_in_later_round",
        "F6": "new_in_later_round",
    }
    assert set(round_statuses(findings[:1]).values()) == {"single_round"}


def test_recorded_differences_and_the_trust_report(no_checks_module: None) -> None:
    document = {
        "records": {
            "countertops": [
                {"id": "c1", "page": 4, "label": "Vanity", "outcome": "Needs correction"},
                {"id": "c2", "page": 5, "label": "Desk", "outcome": "Looks right"},
            ],
            "other_checks": [
                {"id": "k1", "check": "Sink clearance", "outcome": "Needs correction", "pages": [9]}
            ],
        },
        "ai_page_review": [{"page": 12, "different": [{"key": "overall width"}]}],
    }
    recorded = recorded_from(document)
    assert [(d["page"], d["what"]) for d in recorded.differences] == [
        (4, "Vanity"),
        (9, "Sink clearance"),
        (12, "overall width"),
    ]
    findings = [
        {
            "id": "F1",
            "page": 4,
            "record_id": "c1",
            "category": "countertop",
            "status": "different",
            "trust_level": "confirmed",
            "checks": _checks(
                printed_architect="match", printed_vendor="match", second_read="agrees"
            ),
        },
        {
            "id": "F2",
            "page": 20,
            "category": "cabinet",
            "status": "different",
            "source": "rules",
            "rule_check_id": "k1",
            "trust_level": "to_check",
            "checks": _checks(printed_architect="no_text", rule="agrees", lead_checked=True),
        },
    ]
    coverage = {
        "counts": {"total": 3, "same": 1, "different": 1, "not_comparable": 0, "not_opened": 1},
        "pages": [
            {"page": 1, "status": "same"},
            {"page": 2, "status": "different"},
            {"page": 3, "status": "not_opened"},
        ],
    }
    report = trust_report(findings, coverage, recorded, second_reads_skipped=1)

    assert report["counts"] == {"confirmed": 1, "likely": 0, "to_check": 1, "total": 2}
    assert report["by_category"]["cabinet"]["to_check"] == 1
    assert report["checks"]["printed_at_spot"]["match"] == 1
    assert report["checks"]["second_read"] == {
        "agrees": 1,
        "disagrees": 0,
        "unsure": 0,
        "not_run": 1,
    }
    assert report["checks"]["rules"] == [
        {"finding_id": "F2", "rule_check_id": "k1", "result": "agrees"}
    ]
    assert report["gv_checks"]["recorded"] == 3 and report["gv_checks"]["included"] == 2
    assert report["gv_checks"]["left_out"][0]["page"] == 12
    assert report["gv_checks"]["left_out"][0]["record_id"] == "ai-review:p12:overall width"
    # A recorded difference the lead dismissed with a reason is accounted for, and listed.
    dismissed = trust_report(
        findings, None, recorded, dismissed={"ai-review:p12:overall width": "the arch shows 42"}
    )["gv_checks"]
    assert dismissed["left_out"] == [] and dismissed["included"] == 2
    assert dismissed["dismissed"] == [
        {"record_id": "ai-review:p12:overall width", "reason": "the arch shows 42"}
    ]
    # Without the record's id, a code FAIL on the same page is not taken as reported.
    no_id = [{**findings[0], "record_id": None}, findings[1]]
    assert [
        i["record_id"] for i in trust_report(no_id, None, recorded)["gv_checks"]["left_out"]
    ] == [
        "c1",
        "ai-review:p12:overall width",
    ]
    assert report["coverage"]["pages_not_opened"] == [3]
    assert any("drawn as strokes" in line and "20" in line for line in report["not_checked"])
    assert "Pages not opened: 3." in report["not_checked"]
    unread = trust_report(findings, None, recorded_from(None))
    assert unread["gv_checks"] is None
    assert any("could not be read" in line for line in unread["not_checked"])


def test_the_csv_has_trust_columns_and_never_runs_a_formula() -> None:
    finding = {
        "id": "F1",
        "page": 2,
        "category": "countertop",
        "item": "=HYPERLINK(1)",
        "architect": '37"',
        "vendor": "3'-10\"",
        "status": "different",
        "source": "architect",
        "trust_level": "likely",
        "inches": {"architect": "37", "vendor": "46", "difference": "9"},
        "checks": _checks(printed_architect="match", lead_checked=True),
        "created_by": "lead",
    }
    text = findings_csv([finding])
    assert text.startswith("﻿")
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    header, row = rows
    assert header[0] == "AI #" and "Trust" in header and "Second read" in header
    values = dict(zip(header, row, strict=True))
    assert values["Item"] == "'=HYPERLINK(1)"
    assert values["Difference (in)"] == "9" and values["Trust"] == "Likely"
    assert values["Checked on drawing by the AI"] == "yes"


def test_code_numbers_come_from_the_checks_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the checks module present, `inches` are its numbers and its checks are kept."""
    import types

    from app.review.assistant import chat_trust

    fake = types.SimpleNamespace(
        to_inches=lambda text: {'37"': Decimal(37), "3'-10\"": Decimal(46)}.get(text),
        check_finding=lambda finding, paths: types.SimpleNamespace(
            as_dict=lambda: {
                "printed_architect": "match",
                "printed_vendor": "match",
                "arithmetic": None,
            }
        ),
    )
    monkeypatch.setattr(chat_trust, "load_module", lambda name: fake)
    checked = chat_trust.apply_checks(finding_input(), {}, None)
    assert checked["inches"] == {"architect": "37", "vendor": "46", "difference": "9"}
    assert checked["checks"]["printed_vendor"] == "match"
    assert checked["checks"]["lead_checked"] is True
    # Printed at both spots (a non-AI witness) and checked by the lead.
    assert checked["trust_level"] == "confirmed"
    assert checked["evidence_kind"] == "observed" and checked["witnesses"] == ["printed"]


def test_accuracy_points_levels_and_the_coverage_penalty() -> None:
    from app.review.assistant.chat_trust import accuracy

    findings = [
        {"id": "F1", "trust_level": "confirmed", "status": "different", "checks": _checks()},
        {"id": "F2", "trust_level": "likely", "status": "different", "checks": _checks()},
        {
            "id": "F3",
            "trust_level": "likely",
            "status": "different",
            "checks": _checks(second_read="disagrees"),
        },
        {
            "id": "F4",
            "trust_level": "to_check",
            "status": "different",
            "created_by": "gv_review",
            "checks": _checks(),
        },
    ]
    score = accuracy(findings, [{"id": "C1", "value": "3", "printed": "no_text"}], None)
    assert [item["points"] for item in score["breakdown"]] == [1.0, 0.95, 0.0, 1.0, 0.9]
    assert score["score"] == 77 and score["level"] == "orange" and score["claims"] == 5
    assert "test sets" in score["measured"] and "43 of 45" in score["calibration"]
    coverage = {
        "counts": {"total": 4},
        "pages": [
            {"page": 1, "status": "same"},
            {"page": 2, "status": "not_opened", "note": None},
            {"page": 3, "status": "not_opened", "note": "blank"},
            {"page": 4, "status": "same"},
        ],
    }
    penalised = accuracy(findings[:1], [], coverage)
    assert penalised["score"] == 75 and penalised["coverage_factor"] == 0.75
    assert accuracy([], [], None) == {
        "score": None,
        "level": "n/a",
        "claims": 0,
        "breakdown": [],
        "coverage_factor": None,
        "measured": score["measured"],
        "calibration": score["calibration"],
        "not_compared": 0,
        "unknowns": 0,
    }
    assert accuracy(findings[:1], [], None)["level"] == "green"
