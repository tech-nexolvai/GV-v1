"""The result drawer's page pictures (2026-10-23, after the client meeting): the vendor's page as
before, and now another upload's page by its name, or the architect's file's page, so the drawer's
architect side has its own "Whole page" and an item that names an architect page shows that page.
Made-up file names only; no AI, no database."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid4

from app.review.assistant import set_pack
from app.review.assistant.chat_service import PLAN_CHECK_FOLDER, plan_page_picture

VENDOR = "Made-up vendor drawings.pdf"
ARCHITECT = "Made-up architect set.pdf"
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24


def _set(root: Path, *, architect: bool = True) -> UUID:
    """A drawing set's listing and its pack's page pictures: vendor p1-p2, architect p1-p3."""
    package_id = uuid4()
    listing = [
        {"name": VENDOR, "sha256": "a" * 64, "pages": 2, "kind": "the vendor's shop drawings"}
    ]
    if architect:
        listing.append(
            {"name": ARCHITECT, "sha256": "b" * 64, "pages": 3, "kind": "the architect's drawings"}
        )
    plan = root / PLAN_CHECK_FOLDER / str(package_id)
    plan.mkdir(parents=True)
    (plan / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    folder = set_pack.pack_folder(root, listing)
    for item in listing:
        pages = folder / "pages" / set_pack.stem(str(item["name"]))
        pages.mkdir(parents=True)
        for page in range(1, int(str(item["pages"])) + 1):
            (pages / f"p-{page:03d}.png").write_bytes(PNG)
    return package_id


def test_the_vendors_page_by_default_and_another_uploads_page_by_name(tmp_path: Path) -> None:
    package_id = _set(tmp_path)
    vendor = plan_page_picture(tmp_path, package_id, 2)
    assert vendor is not None and vendor.parent.name == set_pack.stem(VENDOR)
    named = plan_page_picture(tmp_path, package_id, 3, file=ARCHITECT)
    assert named is not None and named.parent.name == set_pack.stem(ARCHITECT)
    assert named.name == "p-003.png"
    # The vendor's file has no page 3: none, never another file's page in its place.
    assert plan_page_picture(tmp_path, package_id, 3) is None


def test_the_architects_file_by_side(tmp_path: Path) -> None:
    package_id = _set(tmp_path)
    page = plan_page_picture(tmp_path, package_id, 1, side="architect")
    assert page is not None and page.parent.name == set_pack.stem(ARCHITECT)


def test_an_unknown_file_or_no_architects_file_gives_no_picture(tmp_path: Path) -> None:
    package_id = _set(tmp_path, architect=False)
    assert plan_page_picture(tmp_path, package_id, 1, file="other.pdf") is None
    assert plan_page_picture(tmp_path, package_id, 1, file="../" + VENDOR) is None
    # No architect's file: none (never the vendor's page said to be the architect's).
    assert plan_page_picture(tmp_path, package_id, 1, side="architect") is None
    assert plan_page_picture(tmp_path, package_id, 1) is not None


def test_a_joined_vendor_file_is_found_in_the_sets_chat_copy(tmp_path: Path) -> None:
    """2026-11-04: the plan check's copy of the uploads was made before two shop-drawing files
    were joined into one, so it lists them apart and has no pack; the chat's copy lists the joined
    file the findings name, and its pack has the pictures. Whole pages and page titles come from
    the chat's copy; a file neither copy lists still gives none."""
    from app.review.assistant.chat_service import JOINED_NAME, vendor_page_titles

    package_id = uuid4()
    apart = [
        {
            "name": "Made-up cabinets.pdf",
            "sha256": "c" * 64,
            "pages": 2,
            "kind": "the vendor's shop drawings",
        },
        {
            "name": "Made-up stone tops.pdf",
            "sha256": "d" * 64,
            "pages": 1,
            "kind": "the vendor's shop drawings",
        },
        {"name": ARCHITECT, "sha256": "b" * 64, "pages": 3, "kind": "the architect's drawings"},
    ]
    plan = tmp_path / PLAN_CHECK_FOLDER / str(package_id)
    plan.mkdir(parents=True)
    (plan / "uploads.json").write_text(json.dumps(apart), encoding="utf-8")
    joined_name = JOINED_NAME.format(count=2)
    joined = [
        {"name": joined_name, "sha256": "e" * 64, "pages": 3, "kind": "the vendor's shop drawings"},
        apart[2],
    ]
    chat_id = uuid4()
    chat = tmp_path / str(chat_id)
    chat.mkdir()
    (chat / "uploads.json").write_text(json.dumps(joined), encoding="utf-8")
    folder = set_pack.pack_folder(tmp_path, joined)
    for item in joined:
        pages = folder / "pages" / set_pack.stem(str(item["name"]))
        pages.mkdir(parents=True)
        for page in range(1, int(str(item["pages"])) + 1):
            (pages / f"p-{page:03d}.png").write_bytes(PNG)
    (folder / "page_map.json").write_text(
        json.dumps({"pages": [{"file": joined_name, "page": 3, "sheet_numbers": ["9"]}]}),
        encoding="utf-8",
    )

    # Before: only the plan check's copy is read, and it has no picture.
    assert plan_page_picture(tmp_path, package_id, 3, file=joined_name) is None
    assert vendor_page_titles(tmp_path, package_id) == []
    # With the set's chats: the joined file's page, the architect's by side, the vendor's by default.
    found = plan_page_picture(tmp_path, package_id, 3, file=joined_name, chat_ids=[chat_id])
    assert found is not None and found.parent.name == set_pack.stem(joined_name)
    architect = plan_page_picture(tmp_path, package_id, 2, side="architect", chat_ids=[chat_id])
    assert architect is not None and architect.parent.name == set_pack.stem(ARCHITECT)
    vendor = plan_page_picture(tmp_path, package_id, 1, chat_ids=[chat_id])
    assert vendor is not None and vendor.parent.name == set_pack.stem(joined_name)
    assert plan_page_picture(tmp_path, package_id, 1, file="other.pdf", chat_ids=[chat_id]) is None
    titles = vendor_page_titles(tmp_path, package_id, [chat_id])
    assert [t["page"] for t in titles] == [3] and titles[0]["sheet"] == "9"
