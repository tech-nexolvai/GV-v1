"""Pages with nothing to compare are done (2026-11-03, Anant: "count 'nothing to compare' as done").

A full check of a real job ended "Check incomplete ... Not for sending until it is" because of
pages that could never be compared: vendor pages with no architect drawing of their item in the
upload, and key plans and an appliance checklist that the AI and every pass after the lead judged
not comparable (no sizes on them). More money or a Continue could never finish them. Now:

- such a page is done: it goes to the Not-compared list with its plain reason, and the summary's
  first line counts it apart, by kind ("6 had nothing to compare (no architect drawing: 2; no
  sizes to compare, e.g. key plans or checklists: 4), listed under Not compared");
- only unfinished work keeps the check incomplete and offers Continue: pages left for money, not
  reached or only partly compared, a page the matcher did not answer in time, and "pairing
  unsure" (its architect drawing is in the upload: a person confirms which page it is);
- a page with no architect drawing is never approved unseen (Anant, 2026-10-22): the review
  result is "Review needed" where it would otherwise be an approval;
- the "Only 1 of 4 vendor pages have an architect drawing ..." advice stays, as advice.

Made-up pages, notes and items; scripted models only: $0, no model call.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.review.assistant import full_check_roster, not_compared
from app.review.assistant.chat_summary import (
    NOTHING_NO_DRAWING,
    NOTHING_NO_SIZES,
    NOTHING_TITLE,
    code_summary,
)
from app.review.assistant.full_check_roster import (
    CARRIED_BY,
    NOTHING_NEEDS,
    NOTHING_WHY,
    ai_reason,
    judged_nothing,
    line_without,
)
from app.review.assistant.review_action import CheckState, review_action
from app.review.assistant.simple_check.pairing import UNPAIRED
from app.review.assistant.vendor_text import internal
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster
from tests.review.assistant.test_proof_drawing import _proved, _right
from tests.review.assistant.test_send_package import _minor
from tests.review.assistant.test_unpaired_pages import TAG_WHY, WHY

#: A key plan's note, as a page's helper and its closers write it.
KEY_PLAN = "Key plan: room tags only, no millwork sizes on either drawing"
LOOKED = "not compared: still not compared after 2 more looks"
UNSURE_WHY = f"{UNPAIRED}: pairing unsure: architect page 9 or architect page 2?"

# ---- the pure parts --------------------------------------------------------------------------------


def test_the_ais_own_reason_is_kept_and_codes_words_left_out() -> None:
    assert ai_reason(f"{KEY_PLAN}; {LOOKED}") == KEY_PLAN
    assert ai_reason("A; B; nothing to compare: confirmed by 3 more looks") == "A; B"
    assert ai_reason(LOOKED) is None
    assert ai_reason(None) is None


def test_a_line_of_what_was_not_done_loses_only_the_pages_with_nothing_to_compare() -> None:
    line = "pages 3, 7, 8 and 9: not compared, still not compared after 2 more looks"
    assert line_without(line, {3, 7, 8, 9}) is None
    assert line_without(line, {3, 8}) == (
        "pages 7 and 9: not compared, still not compared after 2 more looks"
    )
    assert line_without("pages 4-6: not compared, out of money", {5}) == (
        "pages 4 and 6: not compared, out of money"
    )
    assert line_without("page 2: not compared, x", {3}) == "page 2: not compared, x"
    # A line that names no pages stays as it is.
    assert line_without("Numbers p2-3: used its share of the price", {2, 3}) == (
        "Numbers p2-3: used its share of the price"
    )


@pytest.mark.parametrize(
    ("entry", "looked", "carried", "nothing"),
    [
        ({"status": "not_comparable", "note": KEY_PLAN, "by": "helper:7"}, True, False, True),
        ({"status": "not_comparable", "note": KEY_PLAN, "by": "lead"}, True, False, True),
        # The helper alone, with no pass after the lead agreeing: not yet.
        ({"status": "not_comparable", "note": KEY_PLAN, "by": "helper:7"}, False, False, False),
        # Kept from the earlier answer a Continue goes on from.
        ({"status": "not_comparable", "note": KEY_PLAN, "by": CARRIED_BY}, False, True, True),
        # Code's notes, a note that says it was not reached or partly compared, or no reason.
        ({"status": "not_comparable", "note": KEY_PLAN, "by": "code"}, True, False, False),
        (
            {"status": "not_comparable", "note": "partly compared", "by": "helper:7"},
            True,
            False,
            False,
        ),
        (
            {"status": "not_comparable", "note": f"{KEY_PLAN}; not compared: out of money"},
            True,
            False,
            False,
        ),
        ({"status": "not_comparable", "note": LOOKED, "by": "helper:7"}, True, False, False),
        ({"status": "not_comparable", "note": None, "by": "helper:7"}, True, False, False),
        ({"status": "different", "note": KEY_PLAN, "by": "helper:7"}, True, False, False),
    ],
)
def test_what_counts_as_the_ais_judgement_of_nothing_to_compare(
    entry: dict[str, Any], looked: bool, carried: bool, nothing: bool
) -> None:
    assert judged_nothing(entry, looked=looked, carried=carried) is nothing


# ---- each page class: complete or not ---------------------------------------------------------------


def _run(
    tmp_path: Path,
    pages: dict[int, tuple[str, str | None, str]] | None = None,
    *,
    skipped: tuple[tuple[int, str], ...] = (),
    **fields: Any,
) -> Any:
    """A roster run at its finish over six vendor pages, every page "same" unless `pages` says
    otherwise (`{page: (status, note, by)}`); `fields` set the run's own state."""
    harness = _roster(tmp_path, _Roster())
    engine = harness.engine
    book = engine.book
    book.set_coverage({"pages": [{"page": p, "status": "same"} for p in range(1, 7)]}, "helper:1")
    for page, (status, note, by) in (pages or {}).items():
        book.set_coverage({"pages": [{"page": page, "status": status, "note": note}]}, by)
    engine._plan = SimpleNamespace(
        skipped=skipped, jobs=(SimpleNamespace(pages=(1, 2, 3, 4, 5, 6)),)
    )
    run = full_check_roster._Run(engine=engine)
    for name, value in fields.items():
        setattr(run, name, value)
    run.vendor_pages = frozenset(range(1, 7)) - {page for page, _why in skipped}
    if run.unpaired or run.retry:
        run._class_unpaired()
    return run


#: (what the page is, its coverage, the run's state, whether the check is complete, the words).
CASES: list[tuple[str, dict[int, Any], dict[str, Any], bool, str]] = [
    (
        "a divider",
        {1: ("not_comparable", "divider", "code")},
        {"skipped": ((1, "divider"),)},
        True,
        "",
    ),
    ("no architect drawing", {}, {"unpaired": {3: WHY}}, True, ""),
    ("only a tag on the architect's plan", {}, {"unpaired": {3: TAG_WHY}}, True, ""),
    (
        "a key plan the passes after the lead agreed on",
        {3: ("not_comparable", f"{KEY_PLAN}; {LOOKED}", "helper:7")},
        {"looks": {3: 2}},
        True,
        "",
    ),
    (
        "a page the earlier answer settled (Continue)",
        {3: ("not_comparable", f"{KEY_PLAN}; {LOOKED}", CARRIED_BY)},
        {},
        True,
        "",
    ),
    (
        "a key plan no pass after the lead took yet",
        {3: ("not_comparable", KEY_PLAN, "helper:7")},
        {},
        False,
        "1 vendor page was not compared (p3)",
    ),
    (
        "a page not reached",
        {3: ("not_comparable", "not reached within the fixed price", "helper:7")},
        {"looks": {3: 1}},
        False,
        "1 vendor page was not compared (p3)",
    ),
    (
        "a page left for money",
        {3: ("not_comparable", f"{KEY_PLAN}; not compared: out of money", "helper:7")},
        {"looks": {3: 1}, "money_pages": {3}},
        False,
        "1 vendor page was not compared (p3)",
    ),
    (
        "a page marked not comparable with no reason",
        {3: ("not_comparable", None, "helper:7")},
        {"looks": {3: 2}},
        False,
        "1 vendor page was not compared (p3)",
    ),
    (
        "a page never opened",
        {
            3: (
                "not_opened",
                "not compared: not reached by the passes after the lead review",
                "code",
            )
        },
        {},
        False,
        "1 vendor page was not compared (p3)",
    ),
    (
        "pairing unsure",
        {},
        {"unpaired": {3: UNSURE_WHY}},
        False,
        (
            "1 vendor page was not compared, pairing unsure: a person confirms which architect "
            "page shows it (p3)"
        ),
    ),
    (
        "a page the matcher did not answer in time",
        {},
        {"retry": {3: WHY}},
        False,
        (
            "1 vendor page was not matched to the architect's drawings in time (p3): Continue "
            "asks again"
        ),
    ),
]


@pytest.mark.parametrize(
    ("pages", "fields", "complete", "words"),
    [case[1:] for case in CASES],
    ids=[case[0] for case in CASES],
)
def test_each_kind_of_page_is_done_or_left_as_the_owner_decided(
    tmp_path: Path, pages: dict[int, Any], fields: dict[str, Any], complete: bool, words: str
) -> None:
    run = _run(tmp_path, pages, **fields)
    why = run._not_for_sending()
    left, _items, _why = run._unfinished(None)
    if complete:
        assert why is None
        assert left == []  # no Continue for it
        assert 3 not in run._pages_unfinished() and 1 not in run._pages_unfinished()
    else:
        assert why == words
        unsure = "pairing unsure" in words
        # Continue compares what it can finish; a pairing a person confirms is not one of them.
        assert (3 in left) is not unsure


def test_a_broken_step_keeps_the_check_incomplete_whatever_its_pages(tmp_path: Path) -> None:
    run = _run(tmp_path, {}, unpaired={3: WHY})
    run.failed_steps = ["Numbers p2-3 ended with an error"]
    assert run._not_for_sending() == "a step did not finish: Numbers p2-3 ended with an error"


# ---- at the finish: said, listed, and never a page left ---------------------------------------------


def test_the_finish_lists_the_pages_with_nothing_to_compare_and_keeps_the_rest(
    tmp_path: Path,
) -> None:
    partly = "partly compared: the second elevation was not read"
    run = _run(
        tmp_path,
        {
            3: ("not_comparable", f"{KEY_PLAN}; {LOOKED}", "helper:7"),
            4: ("not_comparable", f"{partly}; {LOOKED}", "helper:8"),
            5: (
                "not_comparable",
                "Checklist of appliances, no sizes; LABELS p5.D1 are numbers",
                "helper:9",
            ),
        },
        looks={3: 2, 4: 2, 5: 1},
        unpaired={6: WHY},
    )
    run.not_done = [
        "pages 3-5: not compared, still not compared after 2 more looks",
        "Numbers p2-3: used its share of the price",
    ]
    engine = run.engine

    run._settle_nothing_to_compare()

    # What was not done keeps only the page really left (and the lines that name no page).
    assert run.not_done == [
        "page 4: not compared, still not compared after 2 more looks",
        "Numbers p2-3: used its share of the price",
    ]
    assert engine.no_drawing_pages == 1
    notes = {e["page"]: e["note"] for e in engine.book.coverage_map()["pages"]}
    assert notes[3] == f"{KEY_PLAN}; nothing to compare: confirmed by 2 more looks"
    assert notes[5].endswith("; nothing to compare: confirmed by 1 more look")
    assert notes[4] == f"{partly}; {LOOKED}"  # unfinished: as it was
    # Each is a row of the Not-compared list with its plain reason (an internal one is replaced).
    rows = {g["page"]: g for g in engine.extra_gaps if g["why"] == "nothing_to_compare"}
    assert set(rows) == {3, 5}
    assert rows[3]["what"] == f"Vendor page 3: nothing to compare ({KEY_PLAN})"
    assert rows[5]["what"] == f"Vendor page 5: nothing to compare ({NOTHING_WHY})"
    assert all(row["needs"] == NOTHING_NEEDS and not internal(row["what"]) for row in rows.values())
    listed = not_compared.gap_rows(list(rows.values()))
    assert [row.reason for row in listed] == ["nothing_to_compare", "nothing_to_compare"]
    # Only page 4 is left: incomplete for it alone, and Continue compares it alone.
    assert run._not_for_sending() == "1 vendor page was not compared (p4)"
    assert run._unfinished(None) == ([4], [], "still not compared after 2 more looks")
    kinds = run._nothing_kinds()
    assert kinds == {3: NOTHING_NO_SIZES, 5: NOTHING_NO_SIZES, 6: NOTHING_NO_DRAWING}
    assert run._left_kinds(None) == {4: "not finished: still not compared after 2 more looks"}


# ---- the whole check: complete, honest, never approved unseen ---------------------------------------


def test_a_key_plan_the_closers_agree_on_leaves_the_check_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Page 3's helper and every pass after it mark it not comparable, with its reason: it had
    nothing to compare. The check is complete, the page is counted apart and listed."""
    _clean_ink(monkeypatch)
    harness = _roster(tmp_path, _Roster(unpaired=3))
    outcome = harness.run()

    assert "Check incomplete" not in outcome.text
    assert full_check_roster.NOT_SENDABLE_NEXT not in outcome.text
    assert not [b for b in outcome.blocks if b["type"] == "resume"]
    first = outcome.text.splitlines()[0]
    assert (
        "1 had nothing to compare (no sizes to compare, e.g. key plans or checklists: 1)" in first
    )
    assert "listed under Not compared" in first
    note = next(e for e in harness.engine.book.coverage_map()["pages"] if e["page"] == 3)["note"]
    assert note.startswith("no architect drawing in the set; nothing to compare: confirmed by")
    assert "still not compared" not in note
    rows = [g for g in harness.engine.extra_gaps if g["page"] == 3]
    assert [row["why"] for row in rows] == ["nothing_to_compare"]
    package = next(b for b in outcome.blocks if b["type"] == "package")["package"]
    assert package["state"]["incomplete"] is None


def test_a_page_with_no_architect_drawing_is_never_approved_unseen() -> None:
    clean = [_right(id=f"R{n}") for n in range(5)]
    assert review_action(clean).action == "approved"
    found = review_action(clean, CheckState(no_drawing=2))
    assert (found.action, found.rule, found.vendor_words) == ("review_needed", "RN", "Under review")
    assert found.reason == (
        "2 vendor pages had no architect drawing to compare with: a person looks at them before "
        "this can be approved."
    )
    assert found.stamped  # complete: the package is made, marked Under review
    one = review_action([_minor(), *clean], CheckState(no_drawing=1))
    assert one.action == "review_needed"
    assert one.reason == (
        "1 vendor page had no architect drawing to compare with: a person looks at it before "
        "this can be approved. 1 minor correction noted as well."
    )
    # A correction that changes fabrication is still Revise and resubmit, and a check that did not
    # finish still has no stamp.
    assert review_action([_proved(), *clean], CheckState(no_drawing=3)).action == "revise_resubmit"
    unfinished = CheckState(incomplete="it was stopped", no_drawing=3)
    assert review_action(clean, unfinished).action == "not_for_sending"


def test_the_no_drawing_count_is_kept_with_the_package_state() -> None:
    state = CheckState(no_drawing=4)
    assert CheckState.from_dict(state.as_dict()) == state
    assert CheckState.from_dict({"incomplete": None}).no_drawing == 0  # an older answer


# ---- the summary's first line --------------------------------------------------------------------


_LIST = ("Set - Not compared.pdf", "Set - Not compared.xlsx")


def test_the_first_line_counts_what_had_nothing_to_compare_apart_from_what_is_left() -> None:
    counts = {"total": 12, "same": 2, "different": 1, "not_comparable": 9, "not_opened": 0}
    nothing = {
        1: NOTHING_TITLE,
        2: NOTHING_TITLE,
        4: NOTHING_NO_SIZES,
        5: NOTHING_NO_DRAWING,
        6: NOTHING_NO_DRAWING,
        7: NOTHING_NO_DRAWING,
    }
    summary = code_summary(
        [],
        coverage=counts,
        page_count=12,
        products=("countertop", "cabinet"),
        stopped=None,
        files=_LIST,
        compared_pages=[3, 8, 9],
        left_kinds={10: "pairing unsure", 11: "pairing unsure", 12: "not finished: out of money"},
        nothing_pages=nothing,
    )
    assert summary.split("\n")[0] == (
        "Checked 3 of 12 pages (countertops and cabinets, 1 round); 4 had nothing to compare (no "
        "architect drawing: 3; no sizes to compare, e.g. key plans or checklists: 1), listed under "
        "Not compared; 2 were title, divider or skipped pages; the other 3 pages were not compared "
        "(pairing unsure; not finished: out of money)."
    )


def test_a_check_with_nothing_left_says_no_page_was_left() -> None:
    counts = {"total": 6, "same": 3, "different": 0, "not_comparable": 3, "not_opened": 0}
    summary = code_summary(
        [],
        coverage=counts,
        page_count=6,
        products=("countertop",),
        stopped=None,
        files=(),  # no Not-compared list made: it is not said to be listed
        compared_pages=[2, 3, 4],
        left_kinds={},
        nothing_pages={1: NOTHING_TITLE, 5: NOTHING_NO_SIZES, 6: NOTHING_NO_SIZES},
    )
    assert summary.split("\n")[0] == (
        "Checked 3 of 6 pages (countertops, 1 round); 2 had nothing to compare (no sizes to "
        "compare, e.g. key plans or checklists: 2); 1 was a title, divider or skipped page."
    )


def test_a_page_said_as_checked_by_codes_own_checks_is_not_counted_twice() -> None:
    counts = {"total": 4, "same": 1, "different": 0, "not_comparable": 3, "not_opened": 0}
    own = {"page": 3, "status": "same", "source": "vendor_arithmetic", "verdict": "right"}
    summary = code_summary(
        [own],
        coverage=counts,
        page_count=4,
        products=("countertop",),
        stopped=None,
        files=_LIST,
        compared_pages=[1],
        left_kinds={},
        nothing_pages={2: NOTHING_NO_DRAWING, 3: NOTHING_NO_DRAWING, 4: NOTHING_NO_DRAWING},
    )
    assert summary.split("\n")[0] == (
        "Checked 2 of 4 pages (countertops, 1 round): 1 compared with the architect, 1 only by "
        "code's checks of the vendor's own drawing (its dimension strings, GV's rules); 2 had "
        "nothing to compare (no architect drawing: 2), listed under Not compared."
    )


def test_the_next_step_names_the_pages_with_no_architect_drawing() -> None:
    counts = {"total": 4, "same": 2, "different": 0, "not_comparable": 2, "not_opened": 0}
    rights = [_right(id=f"R{n}", page=1) for n in range(3)]
    summary = code_summary(
        rights,
        coverage=counts,
        page_count=4,
        products=("countertop",),
        stopped=None,
        compared_pages=[1, 2],
        left_kinds={},
        nothing_pages={3: NOTHING_NO_DRAWING, 4: NOTHING_NO_DRAWING},
        no_drawing=2,
    )
    assert (
        "Next: a person decides the 2 pages with no architect drawing before this can be "
        "approved." in summary.splitlines()
    )
