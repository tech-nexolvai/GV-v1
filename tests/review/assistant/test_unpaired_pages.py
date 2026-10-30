"""Pages no architect drawing could be paired with (#1885): said honestly, never re-sent.

A real two-file upload paired almost none of its vendor pages with a page of the architect's
file; the check then spent most of its money sending those pages to gap closers, the lead review
and two "look again" rounds, and offered a Continue that would do the same again. Now:

- a page the cross-file pairing leaves unpaired is "Not compared with the architect: no architect
  drawing found for it (why)" from the moment the pairs are final, in plain words;
- no gap closer, lead review, look-again or Continue is sent to compare it with an architect
  (a reviewer's hand pair, or a new matcher answer, opens it again on Continue);
- the helpers still do the checks that need no architect there (the vendor's own strings);
- when most vendor pages are unpaired, the summary says so first, in plain words;
- a vendor string's "should be" (the sum of its own parts) is never called the architect's.

Synthetic page maps, findings and scripted models only: $0, no model call.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import full_check_roster, not_compared
from app.review.assistant.chat_contract import ChatCoveragePageOut
from app.review.assistant.chat_summary import code_summary
from app.review.assistant.full_check_plan import (
    TAG_ONLY_NEEDS,
    TAG_ONLY_NOTE,
    UNPAIRED_NOTE,
    CrossPairs,
    mostly_unpaired,
    pair_lines,
    unpaired_words,
)
from app.review.assistant.full_check_roster import carried_done, reopened, unpaired_note
from app.review.assistant.simple_check.pairing import UNPAIRED
from app.review.assistant.vendor_text import internal
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

# ---- plain words and the threshold ---------------------------------------------------------------

REASONS = (
    f"{UNPAIRED}: the set has no architect drawing to pair it with",
    f"{UNPAIRED}: every architect item is ruled out by code (another section)",
    (
        f"{UNPAIRED}: the AI page matcher found no architect drawing of the same item (a desk), "
        "and code found no clear one"
    ),
    f"{UNPAIRED}: no architect drawing prints its dimensions (the closest, p1003.v0: 1 of 6)",
    (
        f"{UNPAIRED}: p1003.v0 and p1005.v1 match its dimensions equally (2 and 2 of 6), so "
        "code cannot tell which one it is"
    ),
    f"{UNPAIRED}: code found no clear architect drawing for it",
    (
        f"{UNPAIRED}: the AI page matcher is sure of A3 (same run), but code holds no strong "
        "evidence for it and does not put it clearly first (A3: finish tags)"
    ),
)


def test_every_pairing_reason_has_plain_words_with_no_working_words() -> None:
    said = [unpaired_words(reason) for reason in REASONS]
    assert said[0] == "the architect's file has no drawing to pair it with"
    assert said[2] == "no drawing of the same item was found in the architect's file"
    assert said[4] == "two architect drawings match it equally"
    for words in said:
        assert not internal(words), words
        assert "matcher" not in words and "p100" not in words and "A3" not in words


def test_most_pages_unpaired_means_more_than_half() -> None:
    assert mostly_unpaired(96, 99) and mostly_unpaired(50, 99)
    assert not mostly_unpaired(49, 99)
    assert not mostly_unpaired(2, 4) and mostly_unpaired(3, 4)
    assert not mostly_unpaired(0, 0)


def test_a_helper_on_an_unpaired_page_still_records_the_vendors_own_strings() -> None:
    lines = pair_lines([], [(5, f"{UNPAIRED}: code found no clear architect drawing")], "/up")
    assert "Do not pick an architect drawing" in lines[0]
    assert "record each vendor dimension string" in lines[0]


# ---- Continue: an unpaired page is not compared again unless its pairing changed ------------------


def test_continue_keeps_an_unpaired_page_unless_it_is_paired_now() -> None:
    note = UNPAIRED_NOTE.format(why="no drawing of the same item was found in the architect's file")
    assert unpaired_note(note)
    coverage = {
        "pages": [
            {"page": 3, "status": "not_comparable", "note": note, "by": "earlier"},
            {"page": 4, "status": "not_comparable", "note": note, "by": "earlier"},
            {
                "page": 5,
                "status": "not_opened",
                "note": "not compared: not reached",
                "by": "earlier",
            },
            {"page": 6, "status": "different", "note": None, "by": "earlier"},
        ]
    }
    assert carried_done(coverage) == {3, 4, 6}  # page 5 was not reached: compared again
    # Page 3 is still unpaired; page 4 is paired now (a hand pair or a new answer).
    across = CrossPairs(pairs={4: (("arch.pdf", 2),)}, unpaired={3: f"{UNPAIRED}: none"})
    assert reopened(coverage, across) == {4}
    # A page still waiting for the matcher is not known to be unpaired: opened again.
    assert reopened(coverage, across, waiting={3}) == {3, 4}


# ---- the roster: unpaired pages from the start, never re-sent -------------------------------------


class _Matched:
    """A page matcher that has decided every page at once (saved answers)."""

    total = 0
    to_answer: frozenset[int] = frozenset()

    def __init__(self, final: CrossPairs, unanswered: frozenset[int] = frozenset()) -> None:
        self.cross = final
        self.final = final
        self.unanswered = unanswered

    def start(self, first_pages: Any) -> None:
        pass

    def wait_pages(self, pages: Any, cancel: Any = None) -> CrossPairs:
        return self.final

    def waiting(self) -> frozenset[int]:
        return frozenset()

    def finish(self) -> CrossPairs:
        return self.final

    def unanswered_pages(self) -> frozenset[int]:
        return self.unanswered


def _run_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, matcher: _Matched
) -> tuple[Any, Any, _Roster, dict[str, list[Any]]]:
    _clean_ink(monkeypatch)
    script = _Roster()
    harness = _roster(tmp_path, script)
    engine = harness.engine

    def page_matching(page_map: Any) -> Any:
        engine._cross = matcher.cross
        return matcher

    monkeypatch.setattr(engine, "page_matching", page_matching)
    seen: dict[str, list[Any]] = {"close": [], "lead": []}
    close, lead_pages = full_check_roster._Run._close, full_check_roster._Run._lead_pages

    def spy_close(self: Any, pages: Any, *args: Any, **kwargs: Any) -> Any:
        seen["close"].append(sorted(pages))
        return close(self, pages, *args, **kwargs)

    def spy_lead(self: Any, *args: Any, **kwargs: Any) -> Any:
        found = lead_pages(self, *args, **kwargs)
        seen["lead"].append(list(found[0]))
        return found

    monkeypatch.setattr(full_check_roster._Run, "_close", spy_close)
    monkeypatch.setattr(full_check_roster._Run, "_lead_pages", spy_lead)
    outcome = harness.run()
    return outcome, engine, script, seen


def _numbers_pages(script: _Roster) -> set[int]:
    pages: set[int] = set()
    for params in script.params.get("numbers", []):
        brief = json.dumps(params["messages"][0]["content"])
        part = brief.split("on pages ", 1)[1].split(" of ", 1)[0]
        pages |= {int(n) for n in part.split(", ")}
    return pages


def _coverage(outcome: Any) -> dict[int, dict[str, Any]]:
    block = next(b for b in outcome.blocks if b["type"] == "coverage")
    return {int(e["page"]): e for e in block["coverage"]["pages"]}


WHY = f"{UNPAIRED}: the AI page matcher found no architect drawing of the same item"


def test_an_unpaired_page_is_not_compared_from_the_start_and_never_sent_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matcher = _Matched(CrossPairs(pairs={2: (("arch.pdf", 9),)}, unpaired={5: WHY}))

    outcome, engine, script, seen = _run_with(tmp_path, monkeypatch, matcher)

    # The helpers still opened page 5 (the vendor's own strings need no architect)...
    assert 5 in _numbers_pages(script)
    # ...but no gap closer, look-again or lead review was sent to compare it with an architect.
    assert all(5 not in pages for pages in seen["close"]), seen
    assert all(5 not in pages for pages in seen["lead"]), seen
    # Its coverage says so, in plain words, whatever its helper set there.
    entry = _coverage(outcome)[5]
    assert entry["status"] == "not_comparable"
    assert entry["note"] == UNPAIRED_NOTE.format(
        why="no drawing of the same item was found in the architect's file"
    )
    # It is in the Not-compared list with what would settle it.
    rows = [g for g in engine.extra_gaps if g["page"] == 5]
    assert len(rows) == 1 and "upload it" in rows[0]["needs"]
    assert not internal(rows[0]["what"])
    # Never sendable: the page was not compared with the architect, said apart.
    assert "1 vendor page has no architect drawing to compare with (p5)" in outcome.text
    # Continue is never offered to compare it again.
    resume = [b for b in outcome.blocks if b["type"] == "resume"]
    assert all("page 5" not in b["resume"]["redo"] for b in resume)
    assert "Not finished" not in outcome.text or "page 5 " not in outcome.text
    # A minority unpaired: no first-line warning.
    assert "vendor pages have an architect drawing in this upload" not in outcome.text
    # The first line says the one true reason it was not compared (never a list of maybes).
    first = next(line for line in outcome.text.splitlines() if line.startswith("Checked "))
    assert first.endswith(
        "3 pages were not compared (title, divider or skipped pages; no drawing to compare with)."
    ), first


TAG_WHY = (
    f"{UNPAIRED}: drawing needed: the architect shows this item only as a tag on a plan (XX-801 "
    "on its page 19; no elevation or section of the architect's prints it), with no sizes; the "
    "AI page matcher picked architect page 23 but is not sure"
)
#: The result screen's reason line "TAG ONLY" (`frontend/main/src/lib/chat-drawer.ts`).
TAG_ON_PLAN = re.compile(r"only as a tag on a plan \(([^()]+?) on its page (\d+)", re.IGNORECASE)


def test_an_item_shown_only_as_a_tag_names_the_plan_never_no_drawing_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The architect's only page printing the item's tag is a plan (`worklist._tag_page`): its
    coverage says so and names the plan's page, never "no architect drawing found" nor "pairing
    unsure"; the result screen's "Drawing needed" and its "TAG ONLY" line read the tag and page."""
    matcher = _Matched(CrossPairs(pairs={2: (("arch.pdf", 9),)}, unpaired={5: TAG_WHY}))

    outcome, engine, _script, _seen = _run_with(tmp_path, monkeypatch, matcher)

    note = _coverage(outcome)[5]["note"]
    assert note == TAG_ONLY_NOTE.format(why="XX-801 on its page 19")
    assert "no architect drawing found" not in note and "pairing unsure" not in note
    assert unpaired_note(note)
    page = ChatCoveragePageOut.model_validate({"page": 5, "status": "not_comparable", "note": note})
    assert page.drawing_needed and not page.pairing_unsure
    found = TAG_ON_PLAN.search(page.drawing_needed_why or "")
    assert found is not None and (found.group(1), found.group(2)) == ("XX-801", "19")
    rows = [g for g in engine.extra_gaps if g["page"] == 5]
    assert len(rows) == 1 and rows[0]["needs"] == TAG_ONLY_NEEDS
    assert "only as a tag on a plan (XX-801 on its page 19)" in rows[0]["what"]
    assert not internal(rows[0]["what"])


def test_most_pages_unpaired_are_said_first_and_the_check_still_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matcher = _Matched(CrossPairs(pairs={2: (("arch.pdf", 9),)}, unpaired={3: WHY, 4: WHY, 5: WHY}))

    outcome, _engine, script, seen = _run_with(tmp_path, monkeypatch, matcher)

    assert outcome.text.startswith(
        "Only 1 of 4 vendor pages have an architect drawing in this upload; add the "
        "architect's drawings of these items, or check that the architect's file is for the "
        "same project."
    )
    # No early stop: every page's helpers ran (the vendor's own checks on every page).
    assert _numbers_pages(script) == {2, 3, 4, 5}
    for pages in (*seen["close"], *seen["lead"]):
        assert not set(pages) & {3, 4, 5}, seen
    coverage = _coverage(outcome)
    assert all(coverage[p]["status"] == "not_comparable" for p in (3, 4, 5))
    assert "Check incomplete" in outcome.text


def test_a_page_the_matcher_could_not_answer_is_asked_again_on_continue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lost = f"{UNPAIRED}: code found no clear architect drawing for it"
    matcher = _Matched(CrossPairs(unpaired={5: lost}), unanswered=frozenset({5}))

    outcome, _engine, _script, seen = _run_with(tmp_path, monkeypatch, matcher)

    entry = _coverage(outcome)[5]
    assert entry["status"] == "not_comparable" and entry["note"].endswith("Continue asks again")
    assert all(5 not in pages for pages in (*seen["close"], *seen["lead"]))
    resume = [b for b in outcome.blocks if b["type"] == "resume"]
    assert resume and "5" in resume[0]["resume"]["redo"]


# ---- the words: a vendor string's sum is never the architect's --------------------------------------


def _string(page: int, printed: str = "18", summed: str = "20 3/8") -> dict[str, Any]:
    return {
        "id": f"F{page}",
        "page": page,
        "item": "dimension string (up and down)",
        "category": "cabinet",
        "status": "different",
        "source": "vendor_arithmetic",
        "created_by": "consistency",
        "string_geometry": "verified",
        "vendor": f'{printed}"',
        "printed_total": f'{printed}"',
        "architect": f'{summed}"',
        "parts": ['2 3/8"', '14"', '4"'],
        "parts_sum": "20.375",
        "trust_level": "confirmed",
        "witnesses": ["arithmetic"],
        "inches": {"vendor": printed, "architect": summed},
        "vendor_box": {"page": page, "x0": 1, "y0": 1, "x1": 2, "y1": 2},
    }


@pytest.mark.usefixtures("read_twice")
def test_the_biggest_issues_call_a_strings_sum_the_vendors_parts() -> None:
    text = code_summary(
        [_string(27), _string(44)],
        coverage={"total": 109, "same": 0, "different": 0},
        page_count=109,
        products=("cabinet",),
        stopped=None,
    )
    issues = next(line for line in text.splitlines() if line.startswith("Biggest issues"))
    assert 'vendor\'s parts add up to 20 3/8", vendor prints 18" (p27, p44)' in issues
    assert "architect" not in issues


@pytest.mark.usefixtures("read_twice")
def test_the_first_line_never_contradicts_the_wrong_count() -> None:
    """Was: "Checked 0 of 109 pages ... 8 wrong"."""
    text = code_summary(
        [_string(27), _string(44)],
        coverage={"total": 109, "same": 0, "different": 0},
        page_count=109,
        products=("cabinet",),
        stopped=None,
    )
    first = text.splitlines()[0]
    assert first.startswith(
        "Checked 2 of 109 pages (cabinets, 1 round): 0 compared with the architect, 2 only by "
        "code's checks of the vendor's own drawing"
    ), first
    assert "the other 107 pages were not compared" in first
    assert text.splitlines()[1].startswith("2 wrong")
    # With the compared pages known, a page compared and also string-checked counts once.
    mixed = code_summary(
        [_string(27), _string(44)],
        coverage={"total": 10, "same": 1, "different": 1},
        page_count=10,
        products=("cabinet",),
        stopped=None,
        compared_pages=[27, 30],
    )
    assert mixed.splitlines()[0].startswith(
        "Checked 3 of 10 pages (cabinets, 1 round): 2 compared with the architect, 1 only"
    )
    # Nothing outside the compared pages: the line is as before.
    plain = code_summary(
        [_string(27)],
        coverage={"total": 10, "same": 2, "different": 0},
        page_count=10,
        products=("cabinet",),
        stopped=None,
        compared_pages=[27, 30],
    )
    assert plain.splitlines()[0].startswith("Checked 2 of 10 pages (cabinets, 1 round); 8 pages")


def test_the_not_compared_list_never_calls_a_strings_sum_the_architects() -> None:
    finding = {**_string(27), "string_geometry": None, "created_by": "ledger"}
    rows = not_compared.rows([finding])
    assert rows, "an AI-grouped string not verified on the drawing is Not compared"
    assert rows[0].architect == "not used (the vendor's own parts add up to 20 3/8\")"
