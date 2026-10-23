"""The fields the results screen reads for "Drawing needed" and its "By page" view (2026-10-23).

- A vendor page the pairing left with no architect drawing for good (#1885: its coverage note is
  `UNPAIRED_NOTE`) is `drawing_needed` on read, with the plain reason; a page still waiting for an
  answer (Continue asks again) or with any other note is not.
- `vendor_page_titles`: what the set pack already knows of each vendor page (the title block's
  piece from the set index, else the page map's area; the project and sheet printed), no AI call.

Synthetic page maps and notes only: no client values.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.review.assistant.chat_contract import ChatCoverageOut, ChatCoveragePageOut, ChatFindingOut
from app.review.assistant.chat_service import PLAN_CHECK_FOLDER, vendor_page_titles
from app.review.assistant.full_check_plan import (
    UNPAIRED_NOTE,
    UNPAIRED_RETRY_NOTE,
    unpaired_note_why,
    unpaired_words,
)
from app.review.assistant.set_pack import pack_folder


def test_a_final_unpaired_note_is_drawing_needed_with_its_plain_reason() -> None:
    why = "no drawing of the same item was found in the architect's file"
    note = UNPAIRED_NOTE.format(why=why)
    assert unpaired_note_why(note) == why
    # A later note joined on keeps the reason.
    assert unpaired_note_why(f"{note}; vendor string checked") == why
    page = ChatCoveragePageOut.model_validate({"page": 7, "status": "not_comparable", "note": note})
    assert page.drawing_needed is True and page.drawing_needed_why == why


def test_other_notes_are_never_drawing_needed() -> None:
    retry = UNPAIRED_RETRY_NOTE.format(why="no answer yet")
    for note in (None, "", retry, "Not compared: the page was not reached", "compared"):
        assert unpaired_note_why(note) is None
        page = ChatCoveragePageOut.model_validate(
            {"page": 3, "status": "not_comparable", "note": note}
        )
        assert page.drawing_needed is False and page.drawing_needed_why is None
    # What the server says outright is kept.
    said = ChatCoveragePageOut.model_validate(
        {"page": 4, "status": "same", "note": None, "drawing_needed": False}
    )
    assert said.drawing_needed is False


def test_a_stored_coverage_block_says_it_on_read() -> None:
    note = UNPAIRED_NOTE.format(why=unpaired_words("pairing: ruled out every candidate"))
    coverage = ChatCoverageOut.model_validate(
        {
            "pages": [
                {"page": 1, "status": "same", "note": None, "by": "helper:1"},
                {"page": 2, "status": "not_comparable", "note": note, "by": "code"},
            ],
            "counts": {"total": 2, "same": 1, "different": 0, "not_comparable": 1, "not_opened": 0},
        }
    )
    assert [p.drawing_needed for p in coverage.pages] == [False, True]
    assert coverage.pages[1].drawing_needed_why == (
        "no drawing of the same item was found in the architect's file"
    )


def test_drawing_needed_reasons_said_up_front_have_plain_words() -> None:
    assert unpaired_words(
        "unpaired: drawing needed: its page is in Hotel X, and the architect's file has no "
        "drawings of that project"
    ) == ("the architect's file has no drawings of its project")
    assert unpaired_words(
        "unpaired: drawing needed: the architect's drawings of its project (4 pages) print no "
        "sizes of any piece (outlines, schedules or plans only)"
    ) == ("the architect's drawings of it print no sizes")
    assert unpaired_words(
        "unpaired: drawing needed: the architect shows this item only as a tag on a plan (A-1)"
    ) == ("the architect shows it only as a tag on a plan")


def _pack(root: Path, package: str, listing: list[dict[str, str]]) -> Path:
    plan = root / PLAN_CHECK_FOLDER / package
    plan.mkdir(parents=True)
    (plan / "uploads.json").write_text(json.dumps(listing), encoding="utf-8")
    folder = pack_folder(root, listing)
    folder.mkdir(parents=True)
    return folder


def test_vendor_page_titles_come_from_the_set_pack(tmp_path: Path) -> None:
    package = uuid4()
    listing = [
        {"name": "vendor.pdf", "sha256": "a" * 64, "kind": "the vendor's shop drawings"},
        {"name": "architect.pdf", "sha256": "b" * 64, "kind": "the architect's drawings"},
    ]
    folder = _pack(tmp_path, str(package), listing)
    pages = [
        {"file": "vendor.pdf", "page": 2, "area": "2. Coffee point", "sheet_numbers": ["SD-2"]},
        {"file": "vendor.pdf", "page": 1, "area": "1. Reception desk", "sheet_numbers": []},
        {"file": "vendor.pdf", "page": 3, "area": None, "sheet_numbers": []},
        {"file": "architect.pdf", "page": 1, "area": "Reception", "sheet_numbers": ["A-101"]},
    ]
    (folder / "page_map.json").write_text(json.dumps({"pages": pages}), encoding="utf-8")
    index = {
        "pages": {
            "vendor.pdf#1": {"projects": ["Made-up Hotel"], "item": "Front desk", "sheet": "SD-1"},
            "architect.pdf#1": {"projects": ["Other"], "item": "", "sheet": "A-101"},
        }
    }
    (folder / "set_index.json").write_text(json.dumps(index), encoding="utf-8")

    titles = vendor_page_titles(tmp_path, package)

    assert titles == [
        {"page": 1, "title": "Front desk", "project": "Made-up Hotel", "sheet": "SD-1"},
        {"page": 2, "title": "2. Coffee point", "project": None, "sheet": "SD-2"},
        {"page": 3, "title": None, "project": None, "sheet": None},
    ]


def test_no_pack_no_titles(tmp_path: Path) -> None:
    assert vendor_page_titles(tmp_path, uuid4()) == []
    package = uuid4()
    _pack(tmp_path, str(package), [{"name": "vendor.pdf", "sha256": "c" * 64}])
    # The pack folder exists but its page map is not made yet.
    assert vendor_page_titles(tmp_path, package) == []


def _string(**extra: object) -> dict[str, object]:
    return {
        "id": "F1", "page": 3, "item": "Sample unit: dimension string (up)", "category": "other",
        "architect": '11 5/8"', "vendor": '14"', "status": "different", "verified": False,
        "source": "vendor_arithmetic", "created_by": "consistency", "parts": ['2 3/8"', '14"', '4"'],
        "printed_total": '18"', **extra,
    }  # fmt: skip


def test_a_wrong_part_keeps_its_drawn_length_for_the_screen() -> None:
    kept = ChatFindingOut.model_validate(
        _string(
            wrong_part={
                "text": '14"',
                "should_be": "11.625",
                "label": "D3",
                "drawn": '11 5/8"',
                "overall": '18"',
            }
        )
    )
    assert kept.wrong_part is not None
    assert (
        kept.wrong_part.text,
        kept.wrong_part.should_be,
        kept.wrong_part.drawn,
        kept.wrong_part.overall,
    ) == ('14"', "11.625", '11 5/8"', '18"')


def test_an_older_wrong_part_reads_its_drawn_length_from_its_own_words() -> None:
    why = (
        "The drawing names the wrong number: at the drawing's scale (5 of its dimensions agree) "
        'the 14" dimension is drawn as 11 5/8", and the 18" overall is drawn to its value, so '
        'the 14" part should be 11 5/8" (18" - 2 3/8" - 4").'
    )
    older = ChatFindingOut.model_validate(
        _string(why=why, wrong_part={"text": '14"', "should_be": "11.625"})
    )
    assert older.wrong_part is not None and older.wrong_part.drawn == '11 5/8"'
    assert older.wrong_part.overall == '18"'
    # No wrong part (the overall is the wrong number): nothing is made up.
    assert ChatFindingOut.model_validate(_string(why=why)).wrong_part is None
