"""Set pack roles from the upload kind (#1719, separate PDFs step 1). Synthetic PDFs only, $0.

A plain CAD-like architect file and vendor file (no pasted drawings, no GV headings) must give the
page map architect and vendor drawings; GV's combined sheets must be read exactly as before.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import set_pack
from tests.review.assistant.separate_files import Sheet, cover, plain_pdf
from tests.review.assistant.test_set_pack import combined_set
from vocabulary.panel_headings import page_title_role

ARCHITECT_KIND = "the architect's drawings"
SHOP_KIND = (
    "the vendor's shop drawings (a sheet may carry the architect's drawing beside the vendor's, "
    "with GV's markup on top)"
)


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


def _page_map(tmp_path: Path, uploads: Path, listing: list[dict[str, Any]]) -> dict[str, Any]:
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, uploads, listing, background=False)
    assert set_pack.read_status(folder)["state"] == "ready"
    return json.loads((folder / "page_map.json").read_text())


def _roles(page_map: dict[str, Any]) -> list[tuple[str, int, list[str]]]:
    return [
        (entry["file"], entry["page"], [half["role"] for half in entry["halves"]])
        for entry in page_map["pages"]
    ]


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    folder = tmp_path / "uploads"
    folder.mkdir()
    (folder / "arch.pdf").write_bytes(
        plain_pdf([cover(), Sheet("ARCH ELEVATION", (24, 30, 18), "REF. ELEV 2/A-501")])
    )
    (folder / "shop.pdf").write_bytes(
        plain_pdf(
            [
                Sheet("VENDOR ELEVATION", (24, 30, 18), "REF. ELEV 2/A-501"),
                Sheet("CABINET RUN 2", (36, 36)),
            ]
        )
    )
    (folder / "combined.pdf").write_bytes(combined_set())
    return folder


def test_plain_architect_and_vendor_files_get_whole_page_drawings(
    tmp_path: Path, uploads: Path
) -> None:
    listing = _listing(uploads, ("shop.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    page_map = _page_map(tmp_path, uploads, listing)
    assert _roles(page_map) == [
        ("shop.pdf", 1, ["vendor"]),
        # No title words: a page of the shop file is the vendor's.
        ("shop.pdf", 2, ["vendor"]),
        # The cover prints no dimension: a general page, no drawing.
        ("arch.pdf", 1, []),
        ("arch.pdf", 2, ["architect"]),
    ]
    half = page_map["pages"][3]["halves"][0]
    assert half["from_upload"] is True
    assert half["box"] == [0.0, 0.0, 1224.0, 792.0]
    assert page_map["upload_roles"] == {"shop.pdf": "vendor", "arch.pdf": "architect"}
    # Every label code reads takes its drawing's side.
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    dims = json.loads((folder / "dims.json").read_text())
    sides = {
        (entry["file"], entry["page"]): {label["side"] for label in entry["labels"]}
        for entry in dims["pages"]
        if entry["labels"]
    }
    assert sides == {
        ("shop.pdf", 1): {"vendor"},
        ("shop.pdf", 2): {"vendor"},
        ("arch.pdf", 2): {"architect"},
    }


def test_a_shop_file_page_titled_as_the_architects_is_the_architects(
    tmp_path: Path, uploads: Path
) -> None:
    (uploads / "both.pdf").write_bytes(
        plain_pdf([Sheet("ARCH LAYOUT", (24, 30)), Sheet("VENDOR LAYOUT", (24, 30))])
    )
    page_map = _page_map(tmp_path, uploads, _listing(uploads, ("both.pdf", SHOP_KIND)))
    assert _roles(page_map) == [("both.pdf", 1, ["architect"]), ("both.pdf", 2, ["vendor"])]


def test_every_page_of_the_architects_file_is_the_architects_whatever_it_prints(
    tmp_path: Path, uploads: Path
) -> None:
    (uploads / "arch2.pdf").write_bytes(plain_pdf([Sheet("VENDOR TO VERIFY", (24, 30))]))
    page_map = _page_map(tmp_path, uploads, _listing(uploads, ("arch2.pdf", ARCHITECT_KIND)))
    assert _roles(page_map) == [("arch2.pdf", 1, ["architect"])]


def test_a_vendor_only_plain_file_still_gives_vendor_drawings(
    tmp_path: Path, uploads: Path
) -> None:
    page_map = _page_map(tmp_path, uploads, _listing(uploads, ("shop.pdf", SHOP_KIND)))
    assert _roles(page_map) == [("shop.pdf", 1, ["vendor"]), ("shop.pdf", 2, ["vendor"])]


def test_combined_sheets_are_read_exactly_as_before(tmp_path: Path, uploads: Path) -> None:
    alone = _listing(uploads, ("combined.pdf", SHOP_KIND))
    with_architect = _listing(uploads, ("combined.pdf", SHOP_KIND), ("arch.pdf", ARCHITECT_KIND))
    before = _page_map(tmp_path, uploads, alone)
    after = _page_map(tmp_path / "two", uploads, with_architect)
    combined = [entry for entry in after["pages"] if entry["file"] == "combined.pdf"]
    assert combined == before["pages"]
    assert not any(h.get("from_upload") for e in combined for h in e["halves"])
    assert _roles(before) == [
        ("combined.pdf", 1, []),
        ("combined.pdf", 2, ["architect", "vendor"]),
        ("combined.pdf", 3, ["brand_prototype", "vendor"]),
    ]
    assert before["upload_roles"] == {}
    # A set with only a combined shop file keeps its pack's name (no rebuild).
    text = "\n".join(f"{item['name']}:{item['sha256']}" for item in alone)
    assert (
        set_pack.pack_key(alone)
        == hashlib.sha256(f"v{set_pack.PACK_VERSION}\n{text}".encode()).hexdigest()[:32]
    )
    assert set_pack.pack_key(with_architect) != set_pack.pack_key(
        [dict(item, kind=SHOP_KIND) for item in with_architect]
    )


def test_the_same_file_in_both_places_is_read_once_by_its_headings(
    tmp_path: Path, uploads: Path
) -> None:
    (uploads / "copy.pdf").write_bytes((uploads / "combined.pdf").read_bytes())
    listing = _listing(uploads, ("combined.pdf", SHOP_KIND), ("copy.pdf", ARCHITECT_KIND))
    assert set_pack.upload_roles(listing) == {"combined.pdf": "vendor"}
    page_map = _page_map(tmp_path, uploads, listing)
    assert page_map["upload_roles"] == {}
    copy = [entry for entry in page_map["pages"] if entry["file"] == "copy.pdf"]
    assert [h.get("from_upload") for e in copy for h in e["halves"]] == [None] * 4


@pytest.mark.parametrize(
    ("text", "role"),
    [
        ('ARCH LAYOUT SCALE: 3/4" = 1\'-0"', "architect"),
        ("ID SET ELEVATION", "architect"),
        ("I.D. SET", "architect"),
        ("Architectural elevation", "architect"),
        ("VENDOR LAYOUT", "vendor"),
        ("Vendor's shop drawing", "vendor"),
        ("SUBMITTAL 3", "vendor"),
        ("FABRICATOR ELEVATION", "vendor"),
        ("VENDOR LAYOUT - VERIFY WITH ARCH.", None),
        ("SEARCH ARCHIVE CABINETS", None),
        ('CABINETS 24"', None),
    ],
)
def test_page_title_words(text: str, role: str | None) -> None:
    assert page_title_role(text) == role
