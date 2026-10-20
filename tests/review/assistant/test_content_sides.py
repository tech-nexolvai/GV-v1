"""#1825: a combined sheet that prints no GV heading is compared within itself.

The set pack decides its drawings' roles from the content of both drawings (`content_sides`, the
rule of #1052), keeps drawings pasted as pictures in the drawings-only copy, and gives no side to
a label lying inside both an architect's and a vendor's drawing. GV's headed sheets are read as
before. The sheet is `tests/extraction/architect/combined_sheet.py`'s, invented: every value here
is made up.

Verification for: `app/review/assistant/content_sides.py`, `set_pack.build_pack`,
`dim_labels._side`, `plan_check.doubts`.
"""

from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pikepdf
import pytest
from pypdf import PdfReader

from app.review.assistant import content_sides, plan_check, set_pack
from app.review.assistant.dim_labels import _side
from tests.extraction.architect.combined_sheet import combined_sheet

SHOP_KIND = (
    "the vendor's shop drawings (a sheet may carry the architect's drawing beside the vendor's, "
    "with GV's markup on top)"
)


def _pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pdf: bytes, *, in_process: bool = True
) -> Path:
    if in_process:
        monkeypatch.setenv("GV_DIM_LABELS_IN_PROCESS", "1")
    else:
        monkeypatch.delenv("GV_DIM_LABELS_IN_PROCESS", raising=False)
    monkeypatch.setenv("GV_DIM_LABELS_DRAWN_BUDGET_S", "0")
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "shop.pdf").write_bytes(pdf)
    listing = [
        {
            "name": "shop.pdf",
            "sha256": hashlib.sha256(pdf).hexdigest(),
            "pages": 1,
            "kind": SHOP_KIND,
        }
    ]
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, uploads, listing, background=False)
    assert set_pack.read_status(folder)["state"] == "ready"
    return folder


def _read(folder: Path, name: str) -> Any:
    return json.loads((folder / name).read_text())


def _halves(folder: Path) -> list[dict[str, Any]]:
    (entry,) = _read(folder, "page_map.json")["pages"]
    return sorted(entry["halves"], key=lambda half: half["box"][1])


def test_a_sheet_with_no_heading_gets_its_roles_from_both_drawings_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _pack(tmp_path, monkeypatch, combined_sheet(headings=False))
    architect, vendor = _halves(folder)
    assert (architect["role"], vendor["role"]) == ("architect", "vendor")
    assert architect["role_by"] == vendor["role_by"] == content_sides.ROLE_BY
    assert "no heading is printed on this page" in architect["role_reason"]
    (entry,) = _read(folder, "page_map.json")["pages"]
    assert entry["role"] == "architect+vendor"
    # Every label now says whose drawing it is on, so code can pair the two drawings.
    (page,) = _read(folder, "dims.json")["pages"]
    sides = {label["side"] for label in page["labels"]}
    assert {"architect", "vendor"} <= sides
    feet = [label for label in page["labels"] if "'" in label["text"]]
    assert feet and {label["side"] for label in feet} == {"architect"}


def test_the_separate_process_decides_the_same(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _pack(tmp_path, monkeypatch, combined_sheet(headings=False), in_process=False)
    assert [half["role"] for half in _halves(folder)] == ["architect", "vendor"]


def _gv_headings(pdf: bytes) -> bytes:
    """The sheet's notes typed at GV's heading size (24 pt), as GV types them."""
    with pikepdf.open(io.BytesIO(pdf)) as document:
        for annotation in document.pages[0].obj["/Annots"]:
            if annotation.get("/Subtype") == pikepdf.Name.FreeText:
                annotation["/DA"] = pikepdf.String("/Helv 24 Tf 0 g")
        out = io.BytesIO()
        document.save(out)
        return out.getvalue()


def test_a_headed_sheet_is_read_from_its_headings_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _pack(tmp_path, monkeypatch, _gv_headings(combined_sheet()))
    halves = _halves(folder)
    assert [half["role"] for half in halves] == ["architect", "vendor"]
    assert not any("role_by" in half for half in halves)


def test_a_sheet_whose_vendor_drawing_says_nothing_keeps_no_role(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rule needs both sides clear: one silent drawing and nothing is decided, as before."""
    folder = _pack(tmp_path, monkeypatch, combined_sheet(headings=False, vendor_text=False))
    assert [half["role"] for half in _halves(folder)] == ["general", "general"]


def test_a_pasted_picture_is_kept_for_the_agents_and_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _pack(tmp_path, monkeypatch, combined_sheet(headings=False, picture=True))
    (entry,) = _read(folder, "page_map.json")["pages"]
    assert len(entry["pictures"]) == 1
    copy = PdfReader(str(folder / "shop_drawings_only.pdf"))
    kinds = sorted(str(a.get_object()["/Subtype"]) for a in copy.pages[0]["/Annots"])
    assert kinds == ["/Square", "/Stamp", "/Stamp"]  # the picture, beside the two drawings
    # A picture is never a drawing code reads: the roles are the two stamps' alone.
    assert [half["role"] for half in _halves(folder)] == ["architect", "vendor"]


def test_roles_are_set_only_when_both_sides_match_a_drawing() -> None:
    entries = [
        {
            "page": 1,
            "role": "general",
            "headings": [],
            "halves": [
                {"box": [10, 10, 100, 100], "role": "general"},
                {"box": [10, 200, 100, 300], "role": "general"},
            ],
        }
    ]
    assert content_sides.unheaded_pages(entries) == [1]
    one_side = {1: [{"box": [10, 10, 100, 100], "role": "architect", "reason": "x"}]}
    assert content_sides.apply_sides(entries, one_side) == []
    assert entries[0]["role"] == "general"
    both = {
        1: [
            {"box": [11, 10, 100, 101], "role": "architect", "reason": "x"},
            {"box": [10, 200, 100, 300], "role": "vendor", "reason": "y"},
        ]
    }
    assert content_sides.apply_sides(entries, both) == [1]
    assert [h["role"] for h in entries[0]["halves"]] == ["architect", "vendor"]
    assert entries[0]["role"] == "architect+vendor"


def test_headed_or_separate_file_pages_are_never_decided_here() -> None:
    general = {"box": [0, 0, 1, 1], "role": "general"}
    entries = [
        {"page": 1, "headings": ["ID SET ELEVATION"], "halves": [general, general]},
        {"page": 2, "headings": [], "halves": [general, {**general, "role": "vendor"}]},
        {"page": 3, "headings": [], "halves": [general, {**general, "from_upload": True}]},
        {"page": 4, "headings": [], "halves": [general]},
    ]
    assert content_sides.unheaded_pages(entries) == []


def test_a_label_inside_both_an_architect_and_a_vendor_drawing_has_no_side() -> None:
    halves = [
        {"box": [0, 0, 100, 100], "role": "architect"},
        {"box": [80, 0, 200, 100], "role": "vendor"},
    ]
    assert _side((85, 40, 95, 45), halves) == "unknown"
    assert _side((20, 40, 30, 45), halves) == "architect"
    assert _side((150, 40, 160, 45), halves) == "vendor"
    # Overlapping a drawing with no role (GV's pasted symbol) changes nothing.
    assert _side((85, 40, 95, 45), [halves[0], {**halves[1], "role": "general"}]) == "architect"


def test_a_drawing_code_left_undecided_on_such_a_sheet_is_assumed_general() -> None:
    page_map = {
        "files": ["shop.pdf"],
        "pages": [
            {
                "file": "shop.pdf",
                "page": 1,
                "role": "architect+vendor",
                "headings": [],
                "halves": [
                    {"box": [0, 0, 100, 100], "role": "general"},
                    {"box": [0, 200, 100, 300], "role": "architect", "role_by": "content"},
                    {"box": [0, 400, 100, 500], "role": "vendor", "role_by": "content"},
                ],
            }
        ],
    }
    (doubt,) = [d for d in plan_check.doubts(page_map, "shop.pdf", limit=None) if d.kind == "role"]
    assert doubt.options[0][0] == "general"
    # On a headed sheet the first answer is still the vendor's, as before.
    for half in page_map["pages"][0]["halves"]:
        half.pop("role_by", None)
    (doubt,) = [d for d in plan_check.doubts(page_map, "shop.pdf", limit=None) if d.kind == "role"]
    assert doubt.options[0][0] == "vendor"


def test_a_big_sets_answer_may_hold_more_findings(tmp_path: Path) -> None:
    """#1825: the cap grows with the vendor's pages, so the passes after the lead of a big set can
    still record what they compare (a 60-page check hit the fixed 300 and recorded nothing)."""
    from app.review.assistant.chat_findings import FINDINGS_PER_PAGE, MAX_FINDINGS, FindingBook
    from app.review.assistant.sandbox import ChatWorkspace

    workspace = ChatWorkspace.open(tmp_path / "chat")
    assert FindingBook(workspace=workspace, page_count=5).finding_cap == MAX_FINDINGS
    assert FindingBook(workspace=workspace, page_count=60).finding_cap == 60 * FINDINGS_PER_PAGE
    assert FindingBook(workspace=workspace).finding_cap == MAX_FINDINGS
