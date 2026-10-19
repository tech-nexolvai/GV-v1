"""Round 2 as a separate upload (#1728, separate PDFs step 4). Synthetic PDFs and labels only, $0.

The vendor's round-1 file comes beside its re-issued file (`PREVIOUS_ROUND_WORDS`): its drawings
take round 1, the re-issued ones round 2; both are compared with the same architect drawing (the
same ids, `set_pages`), the rounds paired with one architect drawing are one area for verify's
round check and for the round-2 check, which follows round 1's items into round 2 across the files.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.review.assistant import set_pack
from app.review.assistant.round_check import round_two
from app.review.assistant.simple_check.verify import Pack
from app.review.assistant.simple_check.worklist import build_worklist
from tests.review.assistant import test_round_check as rc
from tests.review.assistant.separate_files import Sheet, plain_pdf

ARCHITECT_KIND = "the architect's drawings"
SHOP_KIND = "the vendor's shop drawings"
EARLIER = "Round 1 - shop.pdf"
EARLIER_KIND = "the vendor's previous round of shop drawings (round 1)"


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


def test_the_rounds_come_from_the_uploads_kinds() -> None:
    listing = [
        {"name": "shop.pdf", "sha256": "a", "kind": SHOP_KIND},
        {"name": "arch.pdf", "sha256": "b", "kind": ARCHITECT_KIND},
        {"name": EARLIER, "sha256": "c", "kind": EARLIER_KIND},
    ]
    assert set_pack.upload_rounds(listing) == {EARLIER: 1, "shop.pdf": 2}
    assert set_pack.upload_roles(listing) == {
        "shop.pdf": "vendor",
        "arch.pdf": "architect",
        EARLIER: "vendor",
    }
    assert set_pack.upload_rounds(listing[:2]) == {}


def _pack(tmp_path: Path) -> Path:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    (uploads / "arch.pdf").write_bytes(
        plain_pdf([Sheet("ARCH ELEVATION", (24, 30, 18), "ELEV 2/A-501")])
    )
    # Round 1 printed 31 for 30; round 2 fixed it.
    (uploads / EARLIER).write_bytes(
        plain_pdf([Sheet("VENDOR ELEVATION", (24, 31, 18), "REF. ELEV 2/A-501")])
    )
    (uploads / "shop.pdf").write_bytes(
        plain_pdf([Sheet("VENDOR ELEVATION", (24, 30, 18), "REF. ELEV 2/A-501")])
    )
    listing = _listing(
        uploads,
        ("shop.pdf", SHOP_KIND),
        ("arch.pdf", ARCHITECT_KIND),
        (EARLIER, EARLIER_KIND),
    )
    folder = set_pack.pack_folder(tmp_path / "chats", listing)
    set_pack.ensure_pack(folder, uploads, listing, background=False)
    return folder


def test_both_rounds_are_compared_with_the_same_architect_drawing(tmp_path: Path) -> None:
    folder = _pack(tmp_path)
    work = build_worklist(folder, "shop.pdf")
    by_id = {item.id: item for item in work.items}
    # The architect's file keeps its ids (1001...); the earlier round is numbered after it.
    assert sorted(by_id) == ["p1.v2", "p2001.v1"]
    current, earlier = by_id["p1.v2"], by_id["p2001.v1"]
    assert [d.ref for d in current.architect] == ["p1001.a1"]
    assert [d.ref for d in earlier.architect] == ["p1001.a1"]
    assert current.newest_round and current.earlier_rounds == ("p2001.v1",)
    assert not earlier.newest_round and earlier.later_rounds == ("p1.v2",)
    assert earlier.file == EARLIER and earlier.vendor.own_page == 1
    assert sorted(work.round_groups) == [
        (EARLIER, 1, 2001, "p1001.a1"),
        ("shop.pdf", 1, 1, "p1001.a1"),
    ]
    # Only the vendor's re-issued file is in the page cover (the cloud drawing is on it).
    assert [cover.page for cover in work.pages] == [1]
    # Verify's round check: the rounds paired with one architect drawing are one area.
    pack = Pack.from_folder(folder, "shop.pdf").with_rounds(work.round_groups)
    assert pack.area_key(EARLIER, 2001) == pack.area_key("shop.pdf", 1)
    assert pack.newest_round(pack.area_key(EARLIER, 2001)) == 2
    plain = Pack.from_folder(folder, "shop.pdf")
    assert plain.area_key(EARLIER, 2001) is None


def test_the_round_two_check_follows_round_one_across_the_files() -> None:
    """The round-2 check's own synthetic sheet (round 1 on page 1, round 2 on page 2), with the
    two rounds in two files and no GV area: the work list's round groups line them up."""
    dims = rc._dims()
    first, second = dims["pages"]
    first["file"], second["file"], second["page"] = EARLIER, "sheet.pdf", 1
    page_map = {
        "files": ["sheet.pdf", EARLIER],
        "pages": [
            {
                "file": name,
                "page": 1,
                "area": None,
                "halves": [
                    {
                        "role": "vendor",
                        "round": number,
                        "box": [0, 0, rc.WIDTH, rc.HEIGHT],
                        "from_upload": True,
                    }
                ],
            }
            for name, number in ((EARLIER, 1), ("sheet.pdf", 2))
        ],
    }
    fixed = rc._finding("F1", '46"', '37"')
    still = rc._finding("F2", '12"', '14"')
    for finding in (fixed, still):
        finding["vendor_box"] = {**finding["vendor_box"], "file": EARLIER}
    groups = {(EARLIER, 1): "p1001.a1", ("sheet.pdf", 1): "p1001.a1"}
    report = round_two([fixed, still], dims, page_map, groups=groups)
    statuses = {item.finding_id: item.status for item in report.items}
    assert statuses == {"F1": "fixed", "F2": "still_wrong"}
    assert report.pairs[0]["round1"] == {"file": EARLIER, "page": 1}
    assert report.pairs[0]["round2"] == {"file": "sheet.pdf", "page": 1}
    # Without the groups the two files are not one area: nothing is followed (never a guess).
    assert round_two([fixed, still], dims, page_map).items == []
