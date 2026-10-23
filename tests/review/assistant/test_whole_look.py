"""The whole-drawing look (`whole_look`) and its Likely problems (`likely_problems`).

Made-up drawings, labels and values only; a fake AI; no network and no paid call.

- The looks: one per area on GV's combined sheets (architect, every vendor round, the brand
  prototype), one per paired page with separate uploads; an area or a page with no architect
  drawing gets none; a drawing of unknown role is never shown; a long area is split.
- The words: the brand prototype is reference only, an earlier vendor round is the vendor's.
- The claims, by code: two labels that differ go the normal red / green path; a count, a missing
  piece or a one-label size is a Likely problem with its spot; a wrong label, a wrong drawing (the
  prototype, the vendor's round as the architect), GV's colour, equal values or a size with no label
  are dropped.
- The group: a Likely problem is never clouded unless the reviewer agrees; the letter says "Please
  check and confirm"; the review result is never Approved while one is open.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import likely_problems, proof, whole_look
from app.review.assistant.chat_findings import FindingBook
from app.review.assistant.review_action import review_action
from app.review.assistant.send_package import checks_of, register_status
from app.review.assistant.whole_look import Look, apply_look, claims_of, plan_looks
from tests.review.assistant.test_chat_findings import workspace_with_pdf

FILE = "Vendor set.pdf"
ARCH_FILE = "Architect set.pdf"
TOP = [20.0, 20.0, 590.0, 380.0]
BOTTOM = [20.0, 400.0, 590.0, 780.0]


def _half(role: str, box: list[float], round_: int | None = None, heading: str = "H") -> dict:
    return {"box": box, "role": role, "heading": heading, "round": round_}


def _entry(page: int, halves: list[dict[str, Any]], area: str | None, **more: Any) -> dict:
    return {
        "file": more.pop("file", FILE),
        "page": page,
        "halves": halves,
        "section": "1. Walls",
        "area": area,
        "area_from_page": more.pop("area_from_page", None),
        **more,
    }


def _combined_map() -> dict[str, Any]:
    """Area 1: p2 architect over vendor round 1, p3 vendor round 2, p4 architect over the brand
    prototype. Area 2: p5 vendor only (no architect drawing). p6: unknown role only."""
    return {
        "files": [FILE],
        "pages": [
            _entry(1, [], None),
            _entry(2, [_half("architect", TOP), _half("vendor", BOTTOM, 1)], "1. Kitchen"),
            _entry(3, [_half("vendor", TOP, 2)], None, area_from_page=2),
            _entry(
                4, [_half("architect", TOP), _half("brand_prototype", BOTTOM)], None,
                area_from_page=2,
            ),
            _entry(5, [_half("vendor", TOP, 1)], "2. Bar"),
            _entry(6, [_half("unknown", TOP)], "3. Desk"),
        ],
    }  # fmt: skip


def test_one_look_per_area_with_every_round_and_the_prototype() -> None:
    looks = plan_looks(_combined_map(), FILE, [2, 3, 4, 5, 6])
    assert len(looks) == 1  # the bar has no architect drawing; the desk's role is unknown
    look = looks[0]
    assert look.area == "1. Kitchen"
    roles = [(d.role, d.page, d.round) for d in look.drawings]
    # The architect's first, the vendor's by round, the brand prototype last.
    assert roles == [
        ("architect", 2, None),
        ("architect", 4, None),
        ("vendor", 2, 1),
        ("vendor", 3, 2),
        ("brand_prototype", 4, None),
    ]
    assert look.vendor_pages == (2, 3)
    assert look.newest_round == 2


def test_the_words_name_whose_drawing_each_picture_is() -> None:
    look = plan_looks(_combined_map(), FILE, [2, 3, 4])[0]
    words = whole_look.brief(look)
    assert "Picture 1: the ARCHITECT's drawing" in words
    assert "round 1" in words and "an EARLIER round of the vendor's own drawing" in words
    assert "round 2 is the vendor's current drawing" in words
    assert "BRAND PROTOTYPE" in words and "never this job's architect" in words
    system = whole_look.system_prompt()
    assert "Never decide it yourself" in system and "GV's review markup" in system


def test_a_long_area_is_split_with_its_architect_drawings_in_each_look() -> None:
    pages = [_entry(2, [_half("architect", TOP), _half("vendor", BOTTOM, 1)], "1. Long")]
    pages += [
        _entry(n, [_half("vendor", TOP, 1), _half("vendor", BOTTOM, 1)], None, area_from_page=2)
        for n in range(3, 8)
    ]
    looks = plan_looks({"pages": pages}, FILE, list(range(2, 8)))
    assert len(looks) == 2
    for look in looks:
        assert len(look.drawings) <= whole_look.MAX_PICTURES
        assert look.drawings[0].role == "architect"
    assert sum(len(look.vendor_pages) for look in looks) >= 6


def test_separate_uploads_look_at_each_page_with_its_paired_architect_pages() -> None:
    page_map = {
        "pages": [
            _entry(1, [_half("vendor", TOP, None)], "A", from_upload=True),
            _entry(2, [_half("vendor", TOP, None)], "B", from_upload=True),
            _entry(7, [_half("architect", TOP)], "A", file=ARCH_FILE, from_upload=True),
        ]
    }
    cross = type("Cross", (), {"pairs": {1: ((ARCH_FILE, 7),)}})()
    looks = plan_looks(page_map, FILE, [1, 2], cross)
    assert len(looks) == 1  # page 2 has no pair: nothing to compare
    assert [(d.file, d.page, d.role) for d in looks[0].drawings] == [
        (ARCH_FILE, 7, "architect"),
        (FILE, 1, "vendor"),
    ]
    assert not looks[0].drawings[1].pasted  # its own upload: its colours are its own ink


def test_claims_are_read_from_a_fenced_answer_and_an_unreadable_one_is_none() -> None:
    body = {"claims": [{"kind": "count", "sure": "likely"}, {"kind": "size", "sure": "sure"}]}
    claims = claims_of(f"Here it is.\n```json\n{json.dumps(body)}\n```")
    assert claims is not None and [c["kind"] for c in claims] == ["size", "count"]
    assert claims_of("I could not compare these.") is None
    assert claims_of('{"tags": []}') is None


# ---- the claims, by code -------------------------------------------------------------------------


def _label(label_id: str, side: str, text: str, inches: str, box: list[float], way: str) -> dict:
    return {
        "id": label_id, "text": text, "inches": inches, "box": box, "side": side,
        "orientation": way, "route": "text", "dim_line": None,
    }  # fmt: skip


def _setup(
    tmp_path: Path,
    more: list[dict[str, Any]] | None = None,
    phrases: list[dict[str, Any]] | None = None,
) -> tuple[FindingBook, Look]:
    """Page 2: the architect's upper height 3'-6" (D1) and width 4'-0" (D4) over the vendor's
    round 1 at 30" (D2); page 3: the vendor's round 2 prints 42" (D3); page 4: the brand
    prototype prints 2'-6" (D5). `more` and `phrases`: page 2's other labels and its words."""
    workspace = workspace_with_pdf(tmp_path, pages=4)
    folder = tmp_path / "pack"
    folder.mkdir()
    dims = {
        "version": 1,
        "files": [FILE],
        "pages": [
            {"file": FILE, "page": 2, "labels": [
                _label("D1", "architect", "3'-6\"", "42", [100, 100, 130, 110], "v"),
                _label("D2", "vendor", '30"', "30", [100, 500, 130, 510], "v"),
                _label("D4", "architect", "4'-0\"", "48", [200, 100, 230, 110], "h"),
                *(more or []),
            ], "phrases": phrases or []},
            {"file": FILE, "page": 3, "labels": [
                _label("D3", "vendor", '42"', "42", [100, 100, 130, 110], "v"),
            ]},
            {"file": FILE, "page": 4, "labels": [
                _label("D5", "brand", "2'-6\"", "30", [100, 500, 130, 510], "v"),
            ]},
        ],
    }  # fmt: skip
    (folder / "dims.json").write_text(json.dumps(dims), encoding="utf-8")
    workspace = dataclasses.replace(workspace, set_pack=folder)
    listing = json.loads((workspace.root / "uploads.json").read_text())
    book = FindingBook(workspace=workspace, listing=listing, page_count=4)
    look = plan_looks(_combined_map(), FILE, [2, 3, 4])[0]
    return book, look


def _side(picture: int, label: str | None, value: str) -> dict[str, Any]:
    return {"picture": picture, "label": label, "value": value}


def _claim(kind: str, vendor: dict, architect: dict, **more: Any) -> dict[str, Any]:
    return {
        "kind": kind,
        "item": more.pop("item", "Upper cabinets: height"),
        "vendor": vendor,
        "architect": architect,
        "vendor_spot": more.pop("vendor_spot", None),
        "why": more.pop("why", "The uppers are drawn shorter."),
        "sure": more.pop("sure", "sure"),
    }


def _code_pairs_both(vendor: Any, architect: Any) -> bool:
    """Stands for code's own pairs pairing the two labels (`whole_look.code_paired`)."""
    return True


def _apply(book: FindingBook, look: Look, claims: list[dict[str, Any]], **more: Any) -> Any:
    shown: list[dict[str, Any]] = []
    more.setdefault("same_part", _code_pairs_both)
    applied = apply_look(
        book=book,
        labels=book.dim_labels,
        look=look,
        claims=claims,
        category="other",
        show=shown.append,
        **more,
    )
    return applied, shown


def test_two_labels_that_differ_take_the_normal_red_green_path(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    # Pictures: 1 architect p2, 2 architect p4, 3 vendor r1 p2, 4 vendor r2 p3, 5 prototype p4.
    applied, shown = _apply(
        book, look, [_claim("size", _side(3, "D2", '30"'), _side(1, "D1", "3'-6\""))]
    )
    assert applied.recorded and not applied.dropped
    finding = next(f for f in book.live() if f["id"] == applied.recorded[0])
    assert finding["status"] == "different"
    assert finding["dims"]["vendor"]["id"] == "D2" and finding["dims"]["architect"]["id"] == "D1"
    assert finding["created_by"] == whole_look.CREATED_BY
    assert finding[likely_problems.FIELD]["proof"] == "labels"
    assert finding["item"].startswith("Kitchen: ")
    assert shown and shown[-1]["id"] == finding["id"]
    # Not proved by code (no witness, no pairing proof): a Likely problem, never Not compared.
    assert proof.proof_group(finding) == "likely_problem"
    found = proof.proof_of(finding)
    assert found["label"] == "Likely problem" and found["should_be"] is None
    assert found["likely_problem"]["words"].startswith("Please check and confirm: ")


def test_a_count_or_a_missing_piece_is_a_likely_problem_with_its_spot(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    claim = _claim(
        "count",
        _side(4, None, "1 unit"),
        _side(1, None, "2 units"),
        item="Buffet: units",
        vendor_spot={"x0": 10, "y0": 20, "x1": 30, "y1": 60},
    )
    applied, _shown = _apply(book, look, [claim])
    assert applied.likely and not applied.recorded
    finding = next(f for f in book.live() if f["id"] == applied.likely[0])
    assert finding["status"] == "check" and finding["page"] == 3
    box = finding["vendor_box"]  # 10-30% across and 20-60% down picture 4 (page 3's top half)
    assert box["page"] == 3 and box["x0"] == pytest.approx(20 + 570 * 0.1)
    assert box["y1"] == pytest.approx(20 + 360 * 0.6)
    assert finding["arch_box"]["page"] == 2
    assert likely_problems.likely_only(finding)
    assert proof.proof_group(finding) == "likely_problem"
    words = likely_problems.please_check(finding)
    assert "the architect's drawing shows 2 units" in words and "shows 1 unit" in words
    assert "(round 2)" in words


def test_a_likely_only_item_is_never_clouded_unless_the_reviewer_agrees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, look = _setup(tmp_path)
    applied, _ = _apply(
        book, look, [_claim("size", _side(3, "D2", '30"'), _side(1, None, "3'-6\""))]
    )
    finding = next(f for f in book.live() if f["id"] == applied.likely[0])
    finding["status"] = "different"
    monkeypatch.setattr(proof, "code_proved", lambda f: True)
    assert proof.proof_group(finding) == "likely_problem"
    agreed = {**finding, "decision": {"decision": "agree"}}
    monkeypatch.setattr(proof, "_decided", lambda f: "agree" if f.get("decision") else None)
    monkeypatch.setattr(proof, "stated_requirement", lambda f: '42"')
    assert proof.proof_group(agreed) == "clouded"


def test_a_two_label_claim_code_proves_is_clouded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book, look = _setup(tmp_path)
    applied, _ = _apply(book, look, [_claim("size", _side(3, "D2", '30"'), _side(1, "D1", "42"))])
    finding = next(f for f in book.live() if f["id"] == applied.recorded[0])
    monkeypatch.setattr(proof, "code_proved", lambda f: True)
    assert proof.proof_group(finding) == "clouded"


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        # The brand prototype taken as the architect.
        (_claim("size", _side(3, "D2", '30"'), _side(5, "D5", "2'-6\"")), "brand prototype"),
        # The vendor's own round taken as the architect.
        (_claim("size", _side(3, "D2", '30"'), _side(4, "D3", '42"')), "vendor's own drawing"),
        # The architect named as the vendor side.
        (_claim("size", _side(1, "D1", "3'-6\""), _side(1, "D1", "3'-6\"")), "not a vendor"),
        # A label not on the page.
        (_claim("size", _side(3, "D9", '30"'), _side(1, "D1", "3'-6\"")), "not found"),
        # A label of the other drawing on the same page.
        (_claim("size", _side(3, "D1", "3'-6\""), _side(1, "D1", "3'-6\"")), "not on the vendor"),
        # The AI's value is not what the label prints (a misread).
        (_claim("size", _side(3, "D2", '36"'), _side(1, "D1", "3'-6\"")), "prints"),
        # Equal values: no difference.
        (_claim("size", _side(4, "D3", '42"'), _side(1, "D1", "3'-6\"")), "same value"),
        # A width against a height.
        (_claim("size", _side(3, "D2", '30"'), _side(1, "D4", "4'-0\"")), "width"),
        # A size with no label on either side.
        (_claim("size", _side(3, None, '30"'), _side(1, None, "3'-6\"")), "no label"),
        # A picture that does not exist.
        (_claim("layout", _side(9, None, "x"), _side(1, None, "y")), "not a vendor"),
    ],
)
def test_a_wrong_label_or_drawing_is_dropped(
    tmp_path: Path, claim: dict[str, Any], why: str
) -> None:
    book, look = _setup(tmp_path)
    applied, shown = _apply(book, look, [claim])
    assert not applied.recorded and not applied.likely and not shown
    assert len(applied.dropped) == 1 and why in applied.dropped[0][1], applied.dropped
    assert book.live() == []


def test_a_value_in_gvs_colour_on_gvs_sheet_is_dropped(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    claim = _claim("size", _side(3, "D2", '30"'), _side(1, "D1", "3'-6\""))
    applied, _ = _apply(book, look, [claim], ink=lambda label: (40, 0.9))
    assert "GV's colour" in applied.dropped[0][1]
    applied, _ = _apply(book, look, [claim], ink=lambda label: (40, 0.0))
    assert applied.recorded


def test_a_claim_on_a_label_a_helper_listed_joins_that_item(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    helper, _ = book.report(
        {
            "page": 2, "item": "Uppers height", "category": "other", "status": "check",
            "verified": False, "source": "architect", "vendor_dim": "D2", "architect": "?",
            "check_reason": "pairing_not_certain",
        },
        "helper:1",
    )  # fmt: skip
    applied, _shown = _apply(
        book, look, [_claim("size", _side(3, "D2", '30"'), _side(1, "D1", "42"))]
    )
    assert applied.joined == [helper["id"]] and not applied.recorded
    joined = next(f for f in book.live() if f["id"] == helper["id"])
    assert joined[likely_problems.FIELD]["proof"] == "joined"
    assert proof.proof_group(joined) == "likely_problem"
    assert len(book.live()) == 1


def test_a_likely_claim_at_a_helpers_label_joins_that_item_by_code_facts(tmp_path: Path) -> None:
    """One item, one card: the look names the architect's label and a spot holding the helper's
    vendor label (no vendor label of its own), so it joins the helper's item; a claim that only
    shares the spot (no architect label) stays its own item."""
    book, look = _setup(tmp_path)
    helper, _ = book.report(
        {
            "page": 2, "item": "Uppers height", "category": "other", "status": "different",
            "verified": False, "source": "architect", "vendor_dim": "D2", "architect_dim": "D1",
        },
        "helper:1",
    )  # fmt: skip
    spot = {"x0": 10, "y0": 20, "x1": 25, "y1": 35}  # picture 3 (vendor p2), around D2
    claim = _claim("size", _side(3, None, '30"'), _side(1, "D1", "3'-6\""), vendor_spot=spot)
    applied, _shown = _apply(book, look, [claim])
    assert applied.joined == [helper["id"]] and not applied.likely
    joined = next(f for f in book.live() if f["id"] == helper["id"])
    assert joined[likely_problems.FIELD]["proof"] == "joined"
    assert proof.proof_group(joined) == "likely_problem"
    assert len(book.live()) == 1
    # Another vendor number in that spot (review of #1916): another vendor dimension, its own item.
    book, look = _setup(tmp_path / "other")
    book.report(
        {
            "page": 2, "item": "Uppers height", "category": "other", "status": "different",
            "verified": False, "source": "architect", "vendor_dim": "D2", "architect_dim": "D1",
        },
        "helper:1",
    )  # fmt: skip
    other = _claim("size", _side(3, None, '28"'), _side(1, "D1", "3'-6\""), vendor_spot=spot)
    applied, _shown = _apply(book, look, [other])
    assert applied.likely and not applied.joined and len(book.live()) == 2
    # The same spot without the architect's label: a big AI spot alone never joins.
    book, look = _setup(tmp_path / "again")
    book.report(
        {
            "page": 2, "item": "Uppers height", "category": "other", "status": "different",
            "verified": False, "source": "architect", "vendor_dim": "D2", "architect_dim": "D1",
        },
        "helper:1",
    )  # fmt: skip
    layout = _claim("layout", _side(3, None, "doors"), _side(1, None, "drawers"), vendor_spot=spot)
    applied, _shown = _apply(book, look, [layout])
    assert applied.likely and not applied.joined and len(book.live()) == 2


def test_the_same_claim_twice_and_too_many_claims_are_left_out(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    one = _claim("count", _side(4, None, "1 unit"), _side(1, None, "2 units"), item="Units")
    many = [one, one] + [
        _claim("layout", _side(4, None, f"v{n}"), _side(1, None, f"a{n}"), item=f"Layout {n}")
        for n in range(whole_look.MAX_CLAIMS + 2)
    ]
    applied, _ = _apply(book, look, many)
    assert len(applied.likely) == whole_look.MAX_CLAIMS - 1
    assert any(why == "the same claim twice" for _item, why in applied.dropped)
    assert sum(why == "more than the look's claims" for _i, why in applied.dropped) == 4


# ---- the letter, the register and the review result ------------------------------------------------


def _likely(**more: Any) -> dict[str, Any]:
    return {
        "id": "F7", "page": 3, "item": "Kitchen: units", "category": "cabinet",
        "architect": "2 units", "vendor": "1 unit", "status": "check", "source": "architect",
        "check_reason": "pairing_not_certain",
        likely_problems.FIELD: {
            "proof": "likely_only", "kind": "count", "sure": "sure", "why": "A unit is missing.",
            "words": "Please check and confirm: Kitchen: units: the architect's drawing shows 2 "
            "units; the vendor's drawing shows 1 unit.",
        },
        **more,
    }  # fmt: skip


def test_the_letter_and_the_register_say_please_check_and_confirm() -> None:
    finding = _likely()
    assert register_status(finding) == "likely_problem"
    checks = checks_of([finding, {**finding, "id": "F8", likely_problems.FIELD: None}])
    assert len(checks) == 1
    assert checks[0].item == "Kitchen: units" and checks[0].page == 3
    assert (
        checks[0].words
        == "the architect's drawing shows 2 units; the vendor's drawing shows 1 unit."
    )
    payload = likely_problems.payload(finding)
    assert payload == {
        "kind": "count",
        "words": finding[likely_problems.FIELD]["words"],
        "reason": "A unit is missing.",
        "sure": "sure",
        "by": "whole_drawing_look",
    }


def test_a_likely_problem_is_never_approved() -> None:
    right = {"id": "F1", "page": 2, "item": "Top", "status": "same", "architect": '30"',
             "vendor": '30"', "source": "architect", "category": "countertop"}  # fmt: skip
    rights = [{**right, "id": f"F{n}"} for n in range(1, 30)]
    found = review_action([*rights, _likely()])
    assert found.action != "approved"


def test_likely_problems_count_on_their_own() -> None:
    counts = proof.proof_counts([_likely(), {**_likely(), "id": "F9", likely_problems.FIELD: None}])
    assert counts["likely_problem"] == 1 and counts["not_compared"] == 1
    assert proof.GROUP_WORDS["likely_problem"] == likely_problems.GROUP_WORDS
    assert "likely_problem" in proof.UNDECIDED


# ---- in the full check ------------------------------------------------------------------------------


def test_a_roster_full_check_runs_one_look_per_area_beside_its_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.review.assistant.chat_fakes import reply, text
    from tests.review.assistant.test_full_check_roster import (
        SONNET,
        _clean_ink,
        _findings,
        _Roster,
        _roster,
    )

    class _WithLooks(_Roster):
        def __call__(self, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            if params["system"][0]["text"].startswith("You are an experienced millwork reviewer"):
                self._seen("whole_look", params)
                claim = _claim(
                    "missing_piece",
                    _side(3, None, "no upper cabinet"),
                    _side(1, None, "an upper cabinet"),
                    item="Upper cabinet",
                    vendor_spot={"x0": 0, "y0": 0, "x1": 50, "y1": 50},
                )
                return reply(text(json.dumps({"claims": [claim]})), model=SONNET)
            return super().__call__(params)

    _clean_ink(monkeypatch)
    script = _WithLooks()
    harness = _roster(tmp_path, script)
    engine = harness.engine
    engine.settings = dataclasses.replace(engine.settings, whole_look_model=SONNET)

    outcome = harness.run()

    assert script.calls["whole_look"] == 2  # areas 1 and 2: pages 2-3 and 4-5
    asked = script.params["whole_look"][0]
    assert asked["output_config"]["format"]["schema"]["required"] == ["claims"]
    assert {t["name"] for t in asked["tools"]} == {"zoom"}
    first = asked["messages"][0]["content"]
    assert any(block.get("type") == "image" for block in first)  # code drew the pictures
    assert "Picture 1: the ARCHITECT's drawing" in json.dumps(first)
    found = [f for f in _findings(outcome) if likely_problems.FIELD in f]
    assert sorted(f["page"] for f in found) == [3, 5]
    assert all(proof.proof_group(f) == "likely_problem" for f in found)
    assert all(f["created_by"] == whole_look.CREATED_BY for f in found)
    timings = engine.timings.as_dict(engine.spent)
    assert any(row["phase"] == whole_look.LENS for row in timings["phases"])


def test_no_look_runs_when_it_is_off(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    assert harness.engine.settings.whole_look_model is None
    harness.run()
    assert "whole_look" not in script.calls


def test_a_size_with_no_number_on_either_side_is_a_layout(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    claim = _claim("size", _side(4, None, "hinged doors"), _side(1, None, "pull-out drawers"))
    applied, _ = _apply(book, look, [claim])
    assert len(applied.likely) == 1 and not applied.dropped
    finding = next(f for f in book.live() if f["id"] == applied.likely[0])
    assert finding[likely_problems.FIELD]["kind"] == "layout"


def test_a_look_item_needs_codes_own_ends_not_two_agreeing_picks(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    applied, _ = _apply(book, look, [_claim("size", _side(3, "D2", '30"'), _side(1, "D1", "42"))])
    finding = next(f for f in book.live() if f["id"] == applied.recorded[0])
    picks = {"result": "passed", "model": "devil's advocate",
             "answers": {"pairing": "proved_by_two_picks"}}  # fmt: skip
    assert not proof.pairing_proved({**finding, "critic": picks})
    ends = {**picks, "answers": {"pairing": "proved"}}
    assert proof.pairing_proved({**finding, "critic": ends})
    # A helper's item keeps the rule it had: two agreeing picks prove its pairing.
    helper = {k: v for k, v in finding.items() if k != likely_problems.FIELD}
    assert proof.pairing_proved({**helper, "critic": picks})


def test_the_same_likely_problem_on_two_rounds_is_one_item(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    first = _claim("count", _side(3, None, "1 unit"), _side(1, None, "2 units"), item="Units")
    second = _claim("count", _side(4, None, "1 unit"), _side(1, None, "2 units"), item="Units")
    applied, _ = _apply(book, look, [first, second])
    assert len(applied.likely) == 1 and applied.folded == applied.likely
    kept = next(f for f in book.live() if f["id"] == applied.likely[0])
    assert kept[likely_problems.FIELD]["rounds"] == [1, 2]
    assert "(rounds 1 and 2)" in likely_problems.please_check(kept)


# ---- the client meeting of 2026-10-23: field cut, like for like, Reverse ---------------------------

#: Page 2's vendor top: a 49" overall over the 48" top, the 1" field cut at its wall end (D6), the
#: words FIELD CUT beside it; the architect prints a 1" overhang at the other end (D9).
_FIELD_CUT_LABELS = [
    _label("D6", "vendor", '1"', "1", [150, 450, 160, 460], "h"),
    _label("D7", "vendor", '49"', "49", [300, 430, 315, 440], "h"),
    _label("D8", "vendor", '48"', "48", [300, 455, 315, 465], "h"),
    _label("D9", "architect", "25 [1]", "1", [400, 150, 410, 160], "h"),
]
_FIELD_CUT_WORDS = [{"text": "FIELD CUT", "box": [90, 470, 135, 480], "side": "vendor"}]


def test_a_claim_on_the_vendors_field_cut_is_dropped(tmp_path: Path) -> None:
    book, look = _setup(tmp_path, _FIELD_CUT_LABELS, _FIELD_CUT_WORDS)
    claim = _claim(
        "layout",
        _side(3, "D6", '1" step at the left end'),
        _side(1, "D9", '1" at the right end'),
        item='Top: the 1" step is at the other end',
    )
    applied, shown = _apply(book, look, [claim])
    assert not applied.recorded and not applied.likely and not shown
    assert "field cut" in applied.dropped[0][1]
    # Without the FIELD CUT words the same 1" is an ordinary dimension (code confirms the pair).
    plain, _look = _setup(tmp_path / "plain", _FIELD_CUT_LABELS, [])
    again, _ = _apply(plain, look, [claim])
    assert again.likely and not again.dropped


def test_a_two_label_claim_code_cannot_confirm_is_dropped_not_shown(tmp_path: Path) -> None:
    book, look = _setup(tmp_path)
    size = _claim("size", _side(3, "D2", '30"'), _side(1, "D1", "3'-6\""))
    layout = _claim(
        "layout", _side(3, "D2", '30"'), _side(1, "D1", "3'-6\""), item="Uppers: layout"
    )
    applied, shown = _apply(book, look, [size, layout], same_part=lambda v, a: False)
    assert not applied.recorded and not applied.likely and not shown and not book.live()
    assert [why for _item, why in applied.dropped] == [
        "code could not confirm that both labels measure the same part from the same ends"
    ] * 2
    # A claim with one label (or none) is not a comparison of two labels: still a Likely problem.
    one = _claim("count", _side(3, "D2", "2 units"), _side(1, None, "3 units"), item="Units")
    kept, _ = _apply(book, look, [one], same_part=lambda v, a: False)
    assert kept.likely


def test_code_paired_reads_codes_own_pairs(tmp_path: Path) -> None:
    folder = tmp_path / "pack"
    folder.mkdir()
    pairs = {
        "pairs": [
            {"vendor": 'p2.D2 30"', "architect": "p2.D1 3'-6\"", "status": "different"},
            {"vendor": 'p3.D3 42"', "architect": "p2.D9 x", "status": "not_paired"},
        ]
    }
    (folder / "pairs.json").write_text(json.dumps(pairs), encoding="utf-8")
    same = whole_look.code_paired(folder)
    assert same({"page": 2, "id": "D2"}, {"page": 2, "id": "D1"})
    assert not same({"page": 2, "id": "D2"}, {"page": 2, "id": "D4"})
    assert not same({"page": 3, "id": "D3"}, {"page": 2, "id": "D9"})  # code did not pair them
    assert not whole_look.code_paired(None)({"page": 2, "id": "D2"}, {"page": 2, "id": "D1"})


def test_a_left_right_claim_the_references_reverse_variant_explains_is_dropped(
    tmp_path: Path,
) -> None:
    claim = _claim(
        "layout",
        _side(3, None, "sink 25 1/2 from the right end"),
        _side(1, None, "sink 25 1/2 from the left end"),
        item="Top: the sink sits at the other end",
        why="The sink centre looks mirrored on the reversed unit.",
    )
    reverse = [{"text": "ST-BV // ST-BV-Reverse", "box": [400, 300, 480, 310], "side": "architect"}]
    book, look = _setup(tmp_path, phrases=reverse)
    applied, shown = _apply(book, look, [claim])
    assert not applied.likely and not shown
    assert "Reverse variant" in applied.dropped[0][1]
    # A different size from the same end is a size difference: the Reverse variant explains only
    # which end, never another number (the review of 2026-10-23).
    sized = _claim(
        "layout",
        _side(3, None, "sink 25 1/2 from the left end"),
        _side(1, None, "sink 23 1/2 from the left end"),
        item="Top: the sink centre",
    )
    kept_size, _ = _apply(book, look, [sized])
    assert kept_size.likely and not kept_size.dropped
    one_number = _claim(
        "layout",
        _side(3, None, "sink 30 from the left end"),
        _side(1, None, "sink at the right end"),
        item="Top: the sink end",
    )
    kept_one, _ = _apply(book, look, [one_number])
    assert kept_one.likely and not kept_one.dropped
    # "MIRROR ABOVE" is a mirror on the wall, not a unit drawn the other way round.
    mirror = [{"text": "MIRROR ABOVE", "box": [400, 300, 480, 310], "side": "architect"}]
    on_wall, _look = _setup(tmp_path / "mirror", phrases=mirror)
    kept_mirror, _ = _apply(on_wall, look, [claim])
    assert kept_mirror.likely and not kept_mirror.dropped
    # Where the reference has no Reverse variant, the same claim is a Likely problem.
    plain, _look = _setup(tmp_path / "plain")
    kept, _ = _apply(plain, look, [claim])
    assert kept.likely
    # A count is not about which end: never explained by the Reverse variant.
    count = _claim("count", _side(3, None, "2 doors"), _side(1, None, "3 doors"), item="Doors")
    counted, _ = _apply(book, look, [count])
    assert counted.likely


def test_the_owners_switch_keeps_an_unconfirmed_claim_as_a_likely_problem_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(whole_look.UNCONFIRMED_ENV, "likely")
    book, look = _setup(tmp_path)
    size = _claim("size", _side(3, "D2", '30"'), _side(1, "D1", "3'-6\""))
    applied, _shown = _apply(book, look, [size], same_part=lambda v, a: False)
    assert applied.likely and not applied.recorded and not applied.dropped
    (finding,) = book.live()
    assert finding[likely_problems.FIELD]["proof"] == "likely_only"
    assert proof.proof_group(finding) == "likely_problem"  # never clouded
    # The field cut is still never compared, whatever the switch says.
    monkeypatch.setenv(whole_look.UNCONFIRMED_ENV, "nonsense")
    assert whole_look.unconfirmed_mode() == "drop"
