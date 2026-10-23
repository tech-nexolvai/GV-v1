"""One reference page per finding (stream V, 2026-10-23): every picture, caption and code check of a
finding's architect side uses the architect page its vendor page is paired with, never the vendor's
page number and never "the first architect file".

Made-up file names and values only; no AI, no database; tiny PDFs made here with reportlab.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from app.review.assistant import set_pack
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.chat_service import PLAN_CHECK_FOLDER, plan_page_picture
from app.review.assistant.chat_tools import ChatTools, new_step
from app.review.assistant.full_check_plan import CrossPairs
from app.review.assistant.reference_page import KEY, stamp
from app.review.assistant.sandbox import ChatWorkspace, SandboxRefused, page_folder
from app.review.assistant.spot_guard import unbacked

VENDOR = "Made up vendor set.pdf"
ARCH_A = "Made up architect hotel A.pdf"
ARCH_B = "Made up architect hotel B.pdf"
#: Vendor p3 is paired with page 7 of the SECOND architect file; vendor p4 with page 2 of the first.
PAIRS = CrossPairs(
    pairs={3: ((ARCH_B, 7),), 4: ((ARCH_A, 2), (ARCH_A, 3))},
    unpaired={5: "not paired with an architect drawing: no clear match"},
)
VENDOR_BOX = {"page": 3, "x0": 300, "y0": 400, "x1": 360, "y1": 416}


def _pdf(pages: int) -> bytes:
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=(612.0, 792.0), invariant=1)
    for n in range(1, pages + 1):
        c.drawString(40, 40, f"sheet {n}")
        c.showPage()
    c.save()
    return out.getvalue()


def _listing() -> list[dict[str, Any]]:
    return [
        {"name": VENDOR, "pages": 6, "kind": "the vendor's shop drawings"},
        {"name": ARCH_A, "pages": 4, "kind": "the architect's drawings"},
        {"name": ARCH_B, "pages": 9, "kind": "the architect's drawings"},
    ]


def _workspace(tmp_path: Path, listing: list[dict[str, Any]]) -> ChatWorkspace:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    for item in listing:
        (workspace.uploads / item["name"]).write_bytes(_pdf(int(item["pages"])))
    (workspace.root / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    return workspace


def _book(tmp_path: Path) -> FindingBook:
    listing = _listing()
    book = FindingBook(workspace=_workspace(tmp_path, listing), listing=listing, page_count=6)
    book.pairs_now = lambda: PAIRS
    return book


def _finding(**changes: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "page": 3,
        "item": "Vanity top length",
        "category": "countertop",
        "architect": '37"',
        "vendor": '38"',
        "status": "different",
        "why": "Top 1 in longer than the design",
        "verified": False,
        "vendor_box": dict(VENDOR_BOX),
        "source": "architect",
    }
    base.update(changes)
    return base


# ---- report and update: the architect spot stays on its pair -------------------------------------


def test_an_architect_spot_off_its_pair_is_refused_with_the_pair_named(tmp_path: Path) -> None:
    book = _book(tmp_path)
    spot = {"page": 3, "x0": 10, "y0": 10, "x1": 40, "y1": 20, "file": ARCH_B}
    with pytest.raises(SandboxRefused) as refused:
        book.report(_finding(vs_page=3, arch_box=spot), "helper:1")
    words = str(refused.value)
    assert "page 7 of " + ARCH_B in words and "page 3" in words
    assert not book.live()


def test_a_spot_with_no_file_takes_its_pairs_file_never_the_first_architect_file(
    tmp_path: Path,
) -> None:
    book = _book(tmp_path)
    spot = {"page": 7, "x0": 10, "y0": 10, "x1": 40, "y1": 20}
    finding, _notes = book.report(_finding(vs_page=7, arch_box=spot), "helper:1")
    assert finding["arch_box"]["file"] == ARCH_B and finding["arch_box"]["page"] == 7


def test_a_vs_page_off_the_pair_is_refused(tmp_path: Path) -> None:
    book = _book(tmp_path)
    with pytest.raises(SandboxRefused, match="vs_page"):
        book.report(_finding(page=4, vendor_box={**VENDOR_BOX, "page": 4}, vs_page=9), "lead")
    kept, _notes = book.report(
        _finding(page=4, vendor_box={**VENDOR_BOX, "page": 4}, vs_page=3), "lead"
    )
    assert kept["vs_page"] == 3


def test_a_spot_on_the_pair_with_a_vs_page_off_it_is_refused(tmp_path: Path) -> None:
    book = _book(tmp_path)
    spot = {"page": 7, "x0": 10, "y0": 10, "x1": 40, "y1": 20, "file": ARCH_B}
    with pytest.raises(SandboxRefused, match="vs_page"):
        book.report(_finding(vs_page=2, arch_box=spot), "lead")


def test_the_vendors_own_check_may_box_the_vendors_own_drawing(tmp_path: Path) -> None:
    book = _book(tmp_path)
    own = {**VENDOR_BOX, "file": VENDOR}
    finding, _notes = book.report(
        _finding(source="vendor_arithmetic", arch_box=own, parts=['10"', '27"']), "lead"
    )
    assert finding["arch_box"]["file"] == VENDOR


def test_an_update_moving_the_spot_off_the_pair_is_refused(tmp_path: Path) -> None:
    book = _book(tmp_path)
    spot = {"page": 7, "x0": 10, "y0": 10, "x1": 40, "y1": 20, "file": ARCH_B}
    finding, _notes = book.report(_finding(vs_page=7, arch_box=spot), "lead")
    with pytest.raises(SandboxRefused, match="arch_box"):
        book.update({"finding_id": finding["id"], "arch_box": {**spot, "page": 2}})


def test_no_known_pairs_refuse_nothing(tmp_path: Path) -> None:
    book = _book(tmp_path)
    book.pairs_now = None
    spot = {"page": 2, "x0": 10, "y0": 10, "x1": 40, "y1": 20, "file": ARCH_A}
    finding, _notes = book.report(_finding(vs_page=2, arch_box=spot), "lead")
    assert finding["arch_box"]["page"] == 2


# ---- the stamp: one reference page, set from the pair ---------------------------------------------


def _stamped(finding: dict[str, Any]) -> dict[str, Any]:
    out = stamp(finding, PAIRS.pairs, references=PAIRS.references, unpaired=PAIRS.unpaired)
    return dict(out[KEY])


def test_two_architect_files_the_reference_is_the_pairs_file_and_page() -> None:
    # No spot, no vs_page: the pair's page of the SECOND architect file, not page 3 of the first.
    record = _stamped(_finding())
    assert (record["file"], record["page"], record["why"], record["outside"]) == (
        ARCH_B,
        7,
        None,
        False,
    )
    assert record["pair"] == [{"file": ARCH_B, "page": 7}]


def test_the_spot_on_the_pair_is_the_reference() -> None:
    box = {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": ARCH_A}
    record = _stamped(_finding(page=4, vendor_box={**VENDOR_BOX, "page": 4}, arch_box=box))
    assert (record["file"], record["page"], record["outside"]) == (ARCH_A, 3, False)


def test_a_spot_off_the_pair_is_marked_outside_and_shows_the_pairs_page() -> None:
    box = {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": VENDOR}
    record = _stamped(_finding(arch_box=box))
    assert record["outside"] is True and (record["file"], record["page"]) == (ARCH_B, 7)


def test_an_unpaired_page_has_no_architect_page_and_says_why() -> None:
    record = _stamped(_finding(page=5, vendor_box={**VENDOR_BOX, "page": 5}, vs_page=5))
    assert record["file"] is None and record["page"] is None
    assert "No architect page is paired with vendor page 5" in str(record["why"])


def test_a_vendor_only_card_has_no_architect_page() -> None:
    record = _stamped(_finding(source="vendor_arithmetic", parts=['10"', '28"'], vs_page=7))
    assert record["page"] is None and "vendor's own drawings" in str(record["why"])


def test_the_vendors_own_check_with_boxes_on_its_own_page_is_never_outside() -> None:
    # A span printed twice: both boxes on the vendor's own page of a paired vendor page.
    own = {**VENDOR_BOX, "file": VENDOR}
    for change in (
        {"source": "cross_view", "reference_side": "vendor"},
        {"created_by": "consistency"},
    ):
        record = _stamped(_finding(arch_box=own, vs_page=3, **change))
        assert record["outside"] is False and record["page"] is None


def test_on_an_unpaired_page_only_codes_own_pairing_gives_the_spots_page() -> None:
    # Code's run-by-run pairing gives an unpaired page's item its own architect spot.
    box = {"page": 2, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": ARCH_A}
    unpaired = {"page": 5, "vendor_box": {**VENDOR_BOX, "page": 5}, "arch_box": box}
    record = _stamped(_finding(created_by="run_fit", **unpaired))
    assert (record["file"], record["page"], record["outside"]) == (ARCH_A, 2, False)
    # An AI's own choice of architect page there was never paired: outside, no page shown.
    guessed = _stamped(_finding(created_by="helper:3", **unpaired))
    assert guessed["outside"] is True and guessed["page"] is None


def test_one_page_number_in_two_architect_files_of_the_pair_is_on_the_pair() -> None:
    pairs = {8: (("plan.pdf", 3), ("elevations.pdf", 3))}
    finding = _finding(page=8, vendor_box={**VENDOR_BOX, "page": 8}, vs_page=3)
    record = stamp(finding, pairs)[KEY]
    assert record["outside"] is False and (record["file"], record["page"]) == ("plan.pdf", 3)


def test_a_page_the_cross_pairing_did_not_decide_gets_no_stamp() -> None:
    finding = _finding(page=1, vendor_box={**VENDOR_BOX, "page": 1})
    assert KEY not in stamp(finding, PAIRS.pairs, unpaired=PAIRS.unpaired)


# ---- spot_guard: the architect value is looked for on the reference page --------------------------


def test_spot_guard_looks_on_the_reference_page_never_the_vendors_page_number() -> None:
    asked: list[tuple[str | None, int]] = []

    def labels(file: str | None, page: int) -> list[dict[str, Any]]:
        asked.append((file, page))
        printed = '37"' if (file, page) == (ARCH_B, 7) else '99"'
        inches = "37" if printed == '37"' else "99"
        return [{"side": "unknown", "inches": inches, "text": printed}]

    finding = stamp(_finding(), PAIRS.pairs, unpaired=PAIRS.unpaired)
    assert unbacked(finding, labels) is None
    # Only the pair's page of the second architect file: never page 3 (the vendor's number).
    assert asked == [(ARCH_B, 7)]


def test_spot_guard_looks_on_every_page_of_a_two_page_pair() -> None:
    def labels(file: str | None, page: int) -> list[dict[str, Any]]:
        return [{"side": "unknown", "inches": "37" if (file, page) == (ARCH_A, 3) else "99"}]

    finding = stamp(_finding(page=4, vendor_box={**VENDOR_BOX, "page": 4}), PAIRS.pairs)
    assert unbacked(finding, labels) is None  # printed on the pair's SECOND page


def test_spot_guard_keeps_the_vendors_own_check_as_before() -> None:
    finding = stamp(_finding(source="cross_view", reference_side="vendor", vs_page=4), PAIRS.pairs)
    asked: list[tuple[str | None, int]] = []

    def labels(file: str | None, page: int) -> list[dict[str, Any]]:
        asked.append((file, page))
        return [{"side": "unknown", "inches": "37"}]

    assert unbacked(finding, labels) is None and asked[0][1] == 4


def test_spot_guard_keeps_an_unpaired_architect_value_from_the_vendor() -> None:
    def labels(file: str | None, page: int) -> list[dict[str, Any]]:
        return [{"side": "unknown", "inches": "37"}]  # the value IS on the vendor's page

    finding = stamp(
        _finding(page=5, vendor_box={**VENDOR_BOX, "page": 5}), PAIRS.pairs, unpaired=PAIRS.unpaired
    )
    why = unbacked(finding, labels)
    assert why is not None and "No architect page is paired with vendor page 5" in why


def test_spot_guard_drops_a_right_whose_spot_is_off_its_pair() -> None:
    box = {"page": 3, "x0": 1, "y0": 1, "x1": 2, "y1": 2, "file": VENDOR}
    finding = stamp(_finding(status="same", arch_box=box), PAIRS.pairs)
    why = unbacked(finding, lambda _f, _p: [])
    assert why is not None and "not on the page paired" in why
    # A Not compared one is kept from the vendor too: a later step could settle it on that page.
    listed = stamp(_finding(status="check", arch_box=box), PAIRS.pairs)
    assert unbacked(listed, lambda _f, _p: []) is not None


# ---- zoom and the read tools: no silent vendor file ------------------------------------------------


def _tools(tmp_path: Path, listing: list[dict[str, Any]]) -> ChatTools:
    workspace = _workspace(tmp_path, listing)
    return ChatTools(workspace, None, uploads=listing)  # type: ignore[arg-type]


def test_zoom_without_a_file_in_a_two_file_set_is_refused_naming_each_file(tmp_path: Path) -> None:
    tools = _tools(tmp_path, _listing())
    raw = {"crops": [{"page": 2, "box": {"x0": 0, "y0": 0, "x1": 50, "y1": 50}}]}
    result = tools.zoom(raw, new_step("s1", "zoom", raw))
    text = result.content[0]["text"]
    assert result.is_error and "file is needed" in text
    assert all(name in text for name in (VENDOR, ARCH_A, ARCH_B)) and "9 pages" in text
    assert not tools.seen


def test_a_side_picks_its_only_file_and_refuses_when_that_side_has_two(tmp_path: Path) -> None:
    box = {"x0": 0, "y0": 0, "x1": 1, "y1": 1}
    two = _tools(tmp_path / "two", _listing()[:2])
    assert two._crop_request({"page": 2, "box": box}, 0, "architect")[0] == ARCH_A
    assert two._crop_request({"page": 2, "box": box}, 0, "vendor")[0] == VENDOR
    with pytest.raises(SandboxRefused, match="file is needed"):
        two._crop_request({"page": 2, "box": box}, 0)
    three = _tools(tmp_path / "three", _listing())
    with pytest.raises(SandboxRefused, match="file is needed"):
        three._crop_request({"page": 2, "box": box}, 0, "architect")
    one = _tools(tmp_path / "one", _listing()[:1])
    assert one._crop_request({"page": 2, "box": box}, 0)[0] == VENDOR


# ---- the pack README and the drawer's page pictures ------------------------------------------------


def test_the_readme_names_each_uploads_real_page_folder() -> None:
    text = set_pack.readme([VENDOR, ARCH_A])
    assert "pages/Made_up_vendor_set/" in text and "pages/Made_up_architect_hotel_A/" in text
    assert page_folder(VENDOR) == set_pack.stem(VENDOR)
    assert 'upload\'s name without ".pdf" (A' not in text


def _set(root: Path) -> UUID:
    package_id = uuid4()
    listing = _listing()
    plan = root / PLAN_CHECK_FOLDER / str(package_id)
    plan.mkdir(parents=True)
    (plan / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    folder = set_pack.pack_folder(root, listing)
    for item in listing:
        pages = folder / "pages" / set_pack.stem(str(item["name"]))
        pages.mkdir(parents=True)
        for page in range(1, int(item["pages"]) + 1):
            (pages / f"p-{page:03d}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    return package_id


def test_two_architect_files_side_alone_gives_no_page_the_file_gives_the_right_one(
    tmp_path: Path,
) -> None:
    package_id = _set(tmp_path)
    assert plan_page_picture(tmp_path, package_id, 3, side="architect") is None
    named = plan_page_picture(tmp_path, package_id, 7, file=ARCH_B)
    assert named is not None and named.parent.name == set_pack.stem(ARCH_B)
