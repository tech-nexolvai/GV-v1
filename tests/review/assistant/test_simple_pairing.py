"""Automatic pairing across separate files (#1723, separate PDFs step 2). Synthetic PDFs and made-up
pack facts only, $0, no model call.

Code pairs each vendor drawing of a separate file with one architect drawing, by printed references,
then by layout; a tie or no match is Not compared with code's reason (zero human clicks); the
compare call's "no" rejects a pair code made.
"""

from __future__ import annotations

import hashlib
import json
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import set_pack
from app.review.assistant.simple_check import blind as blind_module
from app.review.assistant.simple_check import compare as compare_module
from app.review.assistant.simple_check.pairing import UNPAIRED, Facts, decide, references_in
from app.review.assistant.simple_check.schema import (
    REPLY_SCHEMA,
    CompareOutcome,
    CompareResult,
    Pair,
    Reply,
)
from app.review.assistant.simple_check.worklist import build_worklist
from tests.review.assistant import test_simple_run_stop as harness
from tests.review.assistant.separate_files import Sheet, cover, plain_pdf

F = Fraction
ARCHITECT_KIND = "the architect's drawings"
SHOP_KIND = "the vendor's shop drawings"

# ---- the decision ----------------------------------------------------------------------------------


def test_references_are_read_and_normalised() -> None:
    found = references_in(["REF. ELEV: 02 / A-501", "SHEET A13.02", 'SCALE 3/4" = 1\'-0"'])
    assert found == {"2/A501", "A501", "A13.02"}
    assert references_in(['24" CABINET', "3/4 FILLER"]) == frozenset()


def _facts(ref: str, values: list[float], *refs: str) -> Facts:
    return Facts(ref, tuple(F(v) for v in values), references_in(refs))


RUN = [24, 30, 18, 72]


def test_a_shared_printed_reference_pairs() -> None:
    vendor = _facts("p1.v0", [24, 31, 18, 73], "REF. ELEV 2/A-501")
    found = decide(
        vendor,
        [
            _facts("p4.a1", RUN, "2/A-501"),
            _facts("p5.a1", [24, 30, 18, 72], "3/A-501"),
        ],
    )
    assert found.chosen == "p4.a1"
    assert "printed reference 2/A501" in found.why


def test_without_references_a_clear_layout_pairs_and_twins_do_not() -> None:
    vendor = _facts("p1.v0", [24, 30, 18, 73])
    clear = decide(vendor, [_facts("p4.a1", RUN), _facts("p5.a1", [36, 36, 12, 84])])
    assert clear.chosen == "p4.a1" and clear.why.startswith("layout: 3 of its 4")
    twins = decide(vendor, [_facts("p4.a1", RUN), _facts("p5.a1", RUN)])
    assert twins.chosen is None
    assert twins.why.startswith(UNPAIRED) and "match its dimensions equally" in twins.why


def test_no_match_too_few_values_and_a_contradicted_reference_are_not_paired() -> None:
    nothing = decide(_facts("p1.v0", [11, 13, 17]), [_facts("p4.a1", RUN)])
    assert nothing.chosen is None and "no architect drawing prints its dimensions" in nothing.why
    few = decide(_facts("p1.v0", [24, 30]), [_facts("p4.a1", RUN), _facts("p5.a1", RUN)])
    assert few.chosen is None and "too few dimensions" in few.why
    contradicted = decide(
        _facts("p1.v0", [24, 30, 18, 72], "2/A-501"),
        [_facts("p4.a1", [36, 36, 12, 84], "2/A-501"), _facts("p5.a1", RUN)],
    )
    assert contradicted.chosen is None and "matches its dimensions better" in contradicted.why
    alone = decide(_facts("p1.v0", [24, 30, 18], "2/A-501"), [_facts("p4.a1", [5, 6, 7], "A-501")])
    assert alone.chosen is None and "prints only 0 of" in alone.why
    assert decide(_facts("p1.v0", RUN), []).chosen is None


def test_references_tied_across_views_go_to_the_layout() -> None:
    vendor = _facts("p1.v0", [24, 30, 18, 73], "SHEET A-501")
    found = decide(
        vendor,
        [_facts("p4.a1", RUN, "A-501"), _facts("p4.a2", [36, 36, 12, 84], "A-501")],
    )
    assert found.chosen == "p4.a1"
    assert found.why.startswith("printed reference shared by several views; layout")


# ---- a real set pack of two separate files -----------------------------------------------------


def _listing(uploads: Path, *files: tuple[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "sha256": hashlib.sha256((uploads / name).read_bytes()).hexdigest(),
            "pages": 1,
            "kind": kind,
        }
        for name, kind in files
    ]


def _build(tmp_path: Path, files: dict[str, bytes], *kinds: tuple[str, str]) -> Path:
    uploads = tmp_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (uploads / name).write_bytes(data)
    listing = _listing(uploads, *kinds)
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, uploads, listing, background=False)
    return folder


ARCH_SHEETS = [
    cover(),
    Sheet("ARCH ELEVATION A", (24, 30, 18), "ELEV 2/A-501"),
    Sheet("ARCH ELEVATION B", (36, 36, 12), "ELEV 3/A-501"),
]
VENDOR_SHEETS = [
    # A printed reference, and a planted difference (31 for 30).
    Sheet("VENDOR ELEVATION", (24, 31, 18), "REF. ELEV 2/A-501"),
    # No reference: the layout decides.
    Sheet("VENDOR ELEVATION", (36, 36, 12)),
    # Nothing the architect prints.
    Sheet("VENDOR ELEVATION", (11, 13, 17, 19)),
]


def test_a_vendor_file_pairs_with_the_architect_file_by_code(tmp_path: Path) -> None:
    folder = _build(
        tmp_path,
        {"shop.pdf": plain_pdf(VENDOR_SHEETS), "arch.pdf": plain_pdf(ARCH_SHEETS)},
        ("shop.pdf", SHOP_KIND),
        ("arch.pdf", ARCHITECT_KIND),
    )
    work = build_worklist(folder, "shop.pdf")
    paired = {item.id: [d.ref for d in item.architect] for item in work.items}
    # The architect file's pages are set pages 4-6 (cover, A, B) after the vendor's 3.
    assert paired == {"p1.v0": ["p5.a1"], "p2.v0": ["p6.a1"]}
    first = work.items[0]
    assert first.architect[0].file == "arch.pdf" and first.architect[0].own_page == 2
    assert first.paired_by is not None and "printed reference" in first.paired_by
    assert work.items[1].paired_by is not None and "layout" in work.items[1].paired_by
    unpaired = next(s for s in work.skipped if s.drawing == "p3.v0")
    assert unpaired.reason.startswith(UNPAIRED) and unpaired.reason.endswith("(code)")
    assert work.uncovered == ()


def test_the_architects_own_file_comes_before_a_page_of_the_vendors_file(tmp_path: Path) -> None:
    """The same layout printed as the vendor file's first page ("ARCH LAYOUT") and uploaded as
    the architect's file: the architect's own file is compared."""
    vendor = [Sheet("ARCH LAYOUT", (24, 30, 18), "REF 2/A-501"), *VENDOR_SHEETS[:1]]
    folder = _build(
        tmp_path,
        {"shop.pdf": plain_pdf(vendor), "arch.pdf": plain_pdf([ARCH_SHEETS[1]])},
        ("shop.pdf", SHOP_KIND),
        ("arch.pdf", ARCHITECT_KIND),
    )
    work = build_worklist(folder, "shop.pdf")
    assert [(i.id, [d.ref for d in i.architect]) for i in work.items] == [("p2.v0", ["p3.a1"])]
    inside = next(s for s in work.skipped if s.drawing == "p1.a1")
    assert "the architect's own file is compared instead" in inside.reason
    # Without the architect's file, the vendor file's own architect page is the candidate.
    alone = _build(tmp_path / "alone", {"shop.pdf": plain_pdf(vendor)}, ("shop.pdf", SHOP_KIND))
    work = build_worklist(alone, "shop.pdf")
    assert [(i.id, [d.ref for d in i.architect]) for i in work.items] == [("p2.v0", ["p1.a1"])]


# ---- the compare call's word on the pair -----------------------------------------------------------


def test_the_reply_form_asks_for_same_drawing() -> None:
    assert "same_drawing" in REPLY_SCHEMA["required"]
    reply = Reply.model_validate(
        {"work_item": "p1.v0", "pairs": [], "other_items": [], "unaccounted_labels": []}
    )
    assert reply.same_drawing == "unsure"  # an answer saved before #1723
    assert Reply.model_validate({**reply.model_dump(), "same_drawing": "No"}).same_drawing == "no"


VENDOR, ARCH = "vendor.pdf", "arch.pdf"


def _label(lid: str, inches: str, box: list[float], side: str) -> dict[str, Any]:
    return {
        "id": lid,
        "text": f'{inches}"',
        "inches": inches,
        "box": box,
        "side": side,
        "orientation": "h",
        "route": "text",
        "mm": None,
        "units": None,
        "dim_line": None,
        "extension": None,
    }


def _separate_pack(folder: Path) -> Path:
    whole = [0.0, 0.0, 600.0, 850.0]

    def page(file: str, number: int, role: str) -> dict[str, Any]:
        return {
            "file": file,
            "page": number,
            "role": role,
            "halves": [{"box": whole, "role": role, "round": None, "from_upload": True}],
            "area": None,
            "area_from_page": None,
            "section": None,
            "sheet_numbers": [],
        }

    ref = [{"text": "REF. ELEV 2/A-501", "box": [50, 820, 200, 830], "side": "vendor"}]
    documents = {
        "page_map.json": {
            "version": set_pack.PACK_VERSION,
            "files": [VENDOR, ARCH],
            "upload_roles": {VENDOR: "vendor", ARCH: "architect"},
            "pages": [page(VENDOR, 1, "vendor"), page(ARCH, 1, "architect")],
        },
        "dims.json": {
            "files": [VENDOR, ARCH],
            "pages": [
                {
                    "file": VENDOR,
                    "page": 1,
                    "labels": [
                        _label("D1", "36", [100, 100, 130, 110], "vendor"),
                        _label("D2", "24", [200, 100, 230, 110], "vendor"),
                        _label("D3", "30", [300, 100, 330, 110], "vendor"),
                    ],
                    "phrases": ref,
                },
                {
                    "file": ARCH,
                    "page": 1,
                    "labels": [
                        _label("D1", "35.25", [100, 100, 130, 110], "architect"),
                        _label("D2", "24", [200, 100, 230, 110], "architect"),
                        _label("D3", "30", [300, 100, 330, 110], "architect"),
                    ],
                    "phrases": [{**ref[0], "text": "ELEV 2/A-501", "side": "architect"}],
                },
            ],
        },
        "pairs.json": {"version": 1, "pairs": [], "pages": []},
        "page_texts.json": {"pages": []},
        "status.json": {"state": "ready", "version": set_pack.PACK_VERSION},
    }
    folder.mkdir(parents=True)
    for name, value in documents.items():
        (folder / name).write_text(json.dumps(value), encoding="utf-8")
    return folder


class _SeparateRun(harness._Run):
    """The stop tests' real chat engine, on a vendor file and a separate architect file."""

    def __init__(self, tmp_path: Path) -> None:
        original = harness._pack
        harness_pdf = harness._pdf
        try:
            harness._pack = _separate_pack  # type: ignore[assignment]
            super().__init__(tmp_path)
        finally:
            harness._pack = original  # type: ignore[assignment]
        harness_pdf(self.workspace.uploads / ARCH, pages=1)
        uploads = self.workspace.uploads
        (uploads / harness.FILE).rename(uploads / VENDOR)
        harness_pdf(uploads / VENDOR, pages=1)
        listing = [
            {"name": VENDOR, "pages": 1, "kind": "the vendor's shop drawings"},
            {"name": ARCH, "pages": 1, "kind": ARCHITECT_KIND},
        ]
        (self.workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
        self.engine.book.listing = listing


def _compare(monkeypatch: pytest.MonkeyPatch, same_drawing: str, seen: list[Any]) -> None:
    def compare_all(items: Any, *, source: Any, on_outcome: Any, **_: Any) -> list[Any]:
        done = []
        for item in items:
            seen.append(item)
            source(item)
            outcome = CompareOutcome(
                work_item=item.id,
                status="ok",
                result=CompareResult(
                    work_item=item.id,
                    pairs=(
                        Pair(
                            vendor_label="p1.D1",
                            architect_label="p2.D1",
                            measures="overall width of the run",
                            vendor_from="wall_face (left)",
                            vendor_to="wall_face (right)",
                            architect_from="wall_face (left)",
                            architect_to="wall_face (right)",
                            verdict="different",
                            note="",
                        ),
                    ),
                    other_items=(),
                    unaccounted_labels=("p1.D2", "p1.D3"),
                    same_drawing=same_drawing,  # type: ignore[arg-type]
                ),
                asked_model="claude-sonnet-5-5",
                model="claude-sonnet-5-5",
                effort="high",
                attempts=1,
            )
            on_outcome(outcome)
            done.append(outcome)
        return done

    monkeypatch.setattr(compare_module, "compare_all", compare_all)
    monkeypatch.setattr(compare_module, "PictureSource", harness._Pictures)


def test_a_red_item_across_files_names_the_architect_file_and_its_own_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _SeparateRun(tmp_path)
    seen: list[Any] = []
    _compare(monkeypatch, "yes", seen)
    monkeypatch.setattr(blind_module, "look", harness._agreeing)
    outcome = check.engine.run()
    assert [item.id for item in seen] == ["p1.v0"]
    assert seen[0].architect[0].ref == "p2.a1" and seen[0].paired_by
    red = [f for f in check.findings(outcome) if f.get("status") == "different"]
    assert len(red) == 1, check.findings(outcome)
    finding = red[0]
    assert finding["vendor"] == '36"' and finding["architect"] == '35.25"'
    assert finding["arch_box"]["file"] == ARCH and finding["arch_box"]["page"] == 1
    assert finding["vendor_box"]["file"] == VENDOR and finding["vendor_box"]["page"] == 1
    assert "Every drawing in the work list was compared (1 drawings)." in outcome.text


def test_the_compare_calls_no_makes_the_drawing_not_compared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check = _SeparateRun(tmp_path)
    _compare(monkeypatch, "no", [])
    monkeypatch.setattr(blind_module, "look", harness._agreeing)
    outcome = check.engine.run()
    assert not [f for f in check.findings(outcome) if f.get("status") == "different"]
    assert "could not be paired with an architect drawing by code" in outcome.text
    assert "Not for sending" in outcome.text
    rows = check.engine.extra_gaps
    assert any(
        "the compare call says the architect drawing code paired it with (p2.a1)" in r["what"]
        for r in rows
    )
