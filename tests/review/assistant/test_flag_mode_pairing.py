"""Flag mode's pairing (Anant, 2026-11-04): a page the pairing is unsure of is still compared.

In proof mode (the default) a vendor page whose architect page code and the AI page matcher could
not confirm is "pairing unsure": Not compared, and the check is "not for sending" until a person
confirms the page. On a real two-brand hotel that left almost every vendor page uncompared, though
the matcher's pick was right on most of them. In flag mode (`GV_ASSISTANT_FLAG_MODE=flag`):

- the page is paired with its best guess: the matcher's pick, sure or not (else code's own), and
  compared with that whole architect page; its reason says "best guess: architect page N";
- every finding compared on that page says so (`pairing_guess`), for the result builder;
- the helpers are told the pair is a best guess;
- only "no architect drawing of the same item", "drawing needed" (a tag on a plan, no sizes) and
  code ruling every page out stay Not compared, and "pairing unsure" never makes the check
  incomplete.

Proof mode is unchanged. Synthetic sets and made-up names only: $0, no model call.
"""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import preflight_pairs, reference_page
from app.review.assistant.full_check_plan import (
    BEST_GUESS_LINE,
    FLAG_MODE_ENV,
    CrossPairs,
    FanOutPlan,
    HelperJob,
    flag_mode,
    pair_lines,
    with_pairs,
)
from app.review.assistant.full_check_roster import remaining_plan, tag_pair_lines
from app.review.assistant.simple_check import pairing, worklist
from app.review.assistant.simple_check.schema import (
    AcrossCandidate,
    AcrossPair,
    Drawing,
    WorkItem,
    WorkList,
)
from app.review.assistant.simple_check.worklist import DRAWING_NEEDED
from tests.review.assistant import test_cross_file_projects as tags
from tests.review.assistant import test_pair_by_index_titles as titles
from tests.review.assistant.test_nothing_to_compare import UNSURE_WHY, _run

SHOP, ARCH = titles.SHOP, titles.ARCH


@pytest.fixture
def flagged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The synthetic sets' work lists in flag mode."""
    for module in (titles, tags):
        monkeypatch.setattr(module, "worklist_from", partial(worklist.worklist_from, flag=True))


def _item(work: Any, page: int) -> Any:
    return next(i for i in work.items if i.vendor.own_page == page)


# ---- the setting -----------------------------------------------------------------------------------


def test_flag_mode_is_off_unless_the_setting_says_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(FLAG_MODE_ENV, raising=False)
    assert not flag_mode()
    monkeypatch.setenv(FLAG_MODE_ENV, "proof")
    assert not flag_mode()
    monkeypatch.setenv(FLAG_MODE_ENV, "flag")
    assert flag_mode()


def test_the_kept_pairing_estimate_is_worked_out_again_when_the_mode_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FLAG_MODE_ENV, raising=False)
    proof = preflight_pairs.fingerprint(tmp_path)
    monkeypatch.setenv(FLAG_MODE_ENV, "flag")
    assert preflight_pairs.fingerprint(tmp_path) != proof


# ---- the work list ---------------------------------------------------------------------------------


def test_proof_mode_leaves_a_page_the_matcher_is_unsure_of_unpaired() -> None:
    unasked = titles._work(None)
    pair = titles._across(titles._work(titles._matched(unasked, {2: 3})), 2)
    assert pair.chosen == () and not pair.guess and pair.unsure


def test_a_page_the_pairing_is_unsure_of_is_compared_with_its_best_guess(flagged: None) -> None:
    unasked = titles._work(None)
    work = titles._work(titles._matched(unasked, {2: 3}))
    pair = titles._across(work, 2)
    ids = titles._ids(unasked, 2)
    # The matcher is sure of the kitchenette's page; code cannot confirm it: a best guess.
    assert pair.guess and pair.chosen == (ids[3],) and not pair.needed
    assert pair.why.startswith("best guess: architect page 3 (not confirmed; other candidate")
    assert pairing.PAIRING_UNSURE not in pair.why.split(": ", 1)[0]
    assert pair.unsure[0] == ids[3]  # the candidates stay, in order
    item = _item(work, 2)
    assert item.pairing_guess and [d.own_page for d in item.architect] == [3]
    # The run's pages: compared (paired), a best guess, and never Not compared.
    cross = CrossPairs.of(work)
    assert cross.pairs[2] == ((ARCH, 3),) and cross.guesses[2] == ((ARCH, 3),)
    assert 2 not in cross.unpaired
    # A page code and the matcher agree on is no guess.
    assert all(page not in cross.guesses for page in cross.pairs if page != 2)


def test_a_matcher_pick_code_rules_out_offers_codes_own_page_first(flagged: None) -> None:
    # Vendor TYPE 1, the matcher sure of TYPE 3: code's page of the same title is the guess.
    unasked = titles._work(None)
    pair = titles._across(titles._work(titles._matched(unasked, {3: 1})), 3)
    ids = titles._ids(unasked, 3)
    assert pair.guess and pair.chosen == (ids[2],)
    assert pair.why.startswith("best guess: architect page 2")
    assert "other candidate architect page 1" in pair.why


def test_a_tag_shown_only_on_a_plan_stays_not_compared(flagged: None) -> None:
    bench = tags._across(
        tags._work(tags._index(), tags._answers(["p1004"], ["p1004"], False, "p6.v0")), 6
    )
    assert bench.needed and bench.chosen == () and not bench.guess
    assert DRAWING_NEEDED in bench.why


def test_a_page_held_against_the_tags_plan_is_the_best_guess(flagged: None) -> None:
    # Proof mode: "pairing unsure: architect page 4 or architect page 5?" (the matcher is sure of
    # page 4; page 5 is the plan printing the vendor's tag, with no sizes).
    work = tags._work(tags._index(), tags._answers(["p1004"], ["p1004"], True, "p6.v0"))
    bench = tags._across(work, 6)
    assert bench.guess and bench.chosen == ("p1004",)
    assert bench.why.startswith("best guess: architect page 4 (not confirmed; other candidate")
    assert CrossPairs.of(work).guesses[6] == ((tags.ARCH, 4),)


def test_no_drawing_of_the_same_item_stays_not_compared() -> None:
    # The matcher found none and code has no pick: nothing to guess.
    vendor = pairing.Facts("v")
    pair = AcrossPair(
        vendor=_drawing(),
        candidates=(),
        answer=(),
        why=f"{pairing.UNPAIRED}: the AI page matcher found no architect drawing of the same item",
    )
    pairs = {"p1.v0": pair}
    worklist._best_guesses(pairs, lambda d: vendor, {})
    assert pairs["p1.v0"] is pair


def test_a_drawing_needed_or_paired_drawing_is_never_guessed() -> None:
    candidate = _candidate("p1003", 3)
    needed = AcrossPair(
        vendor=_drawing(), candidates=(candidate,), unsure=("p1003",), needed=True, why="x"
    )
    paired = AcrossPair(vendor=_drawing(), candidates=(candidate,), chosen=("p1003",), why="paired")
    pairs = {"a": needed, "b": paired}
    worklist._best_guesses(pairs, lambda d: pairing.Facts(d.ref), {"p1003": candidate})
    assert pairs == {"a": needed, "b": paired}


def test_codes_own_pick_is_the_guess_when_the_matcher_names_none() -> None:
    candidate = _candidate("p1003", 3)
    pair = AcrossPair(
        vendor=_drawing(),
        candidates=(candidate,),
        code_pick="p1003",
        answer=(),
        unsure=("p1003",),
        why=(
            f"{pairing.UNPAIRED}: {pairing.PAIRING_UNSURE}: architect page 3?; code chose "
            "architect page 3, but the AI page matcher found no architect drawing of the same item"
        ),
    )
    pairs = {"p1.v0": pair}
    worklist._best_guesses(pairs, lambda d: pairing.Facts(d.ref), {"p1003": candidate})
    guessed = pairs["p1.v0"]
    assert guessed.guess and guessed.chosen == ("p1003",)
    assert guessed.why == (
        "best guess: architect page 3 (not confirmed): code chose architect page 3, but the AI "
        "page matcher found no architect drawing of the same item"
    )


def test_another_vendor_files_guess_never_lands_on_the_main_files_page() -> None:
    work = WorkList(
        file=SHOP,
        items=(
            _work_item(SHOP, 2, (ARCH, 3), guess=False),
            _work_item("older round.pdf", 2, (ARCH, 9), guess=True),
            _work_item(SHOP, 4, (ARCH, 5), guess=True),
            # One drawing of page 6 is surely paired with page 7, another guessed on it: no guess.
            _work_item(SHOP, 6, (ARCH, 7), guess=False),
            _work_item(SHOP, 6, (ARCH, 7), guess=True, ref="p6.v1"),
        ),
        skipped=(),
        pages=(),
    )
    cross = CrossPairs.of(work)
    assert cross.pairs[2] == ((ARCH, 3),) and 2 not in cross.guesses
    assert cross.guesses == {4: ((ARCH, 5),)}
    assert cross.pairs[6] == ((ARCH, 7),)


# ---- the helpers' brief ----------------------------------------------------------------------------


def test_the_helpers_are_told_which_pairs_are_a_best_guess() -> None:
    lines = pair_lines(
        [(2, ((ARCH, 3),)), (4, ((ARCH, 5),))],
        [],
        "uploads",
        guesses={4: ((ARCH, 5),)},
    )
    assert len(lines) == 2
    assert lines[0].startswith("PAIRED ACROSS THE UPLOADS") and "p.4" not in lines[0]
    assert f"your p.2 with uploads/{ARCH} p.3" in lines[0]
    assert lines[1] == BEST_GUESS_LINE.format(pages=f"your p.4 with uploads/{ARCH} p.5")
    # Proof mode's lines are unchanged.
    assert pair_lines([(2, ((ARCH, 3),))], [], "uploads") == pair_lines(
        [(2, ((ARCH, 3),))], [], "uploads", guesses={}
    )
    said = tag_pair_lines(
        [2, 4],
        {2: ((ARCH, 3),), 4: ((ARCH, 5),)},
        vendor_file=SHOP,
        architect_file=ARCH,
        uploads="uploads",
        guesses={4: ((ARCH, 5),)},
    )
    assert f"your p.4 with uploads/{ARCH} p.5 (best guess)" in said
    assert f"your p.2 with uploads/{ARCH} p.3;" in said


def test_a_job_keeps_its_pages_guesses_when_planned_and_cut() -> None:
    cross = CrossPairs(pairs={2: ((ARCH, 3),), 4: ((ARCH, 5),)}, guesses={4: ((ARCH, 5),)})
    job = with_pairs(HelperJob(1, (2, 3, 4), SHOP), cross)
    assert job.guesses == ((4, ((ARCH, 5),)),)
    cut = remaining_plan(FanOutPlan((job,), ()), {2})
    assert cut.jobs[0].guesses == ((4, ((ARCH, 5),)),)
    assert remaining_plan(FanOutPlan((job,), ()), {4}).jobs[0].guesses == ()


# ---- the findings ----------------------------------------------------------------------------------


GUESSES = CrossPairs(pairs={3: ((ARCH, 7),), 4: ((ARCH, 2),)}, guesses={3: ((ARCH, 7),)})


def _stamped(finding: dict[str, Any], cross: CrossPairs = GUESSES) -> dict[str, Any]:
    stamped = reference_page.stamp(finding, cross.pairs, unpaired=cross.unpaired, vendor_file=SHOP)
    return reference_page.mark_guess(stamped, cross.guesses)


def test_a_finding_compared_on_a_best_guess_says_so() -> None:
    finding = _stamped({"id": "f1", "page": 3, "vs_page": 7, "architect": '30"', "vendor": '31"'})
    assert finding["pairing_guess"] == {
        "page": 7,
        "file": ARCH,
        "words": "best guess: architect page 7",
    }
    # No architect spot named: the pair's page, still the guess.
    assert _stamped({"id": "f2", "page": 3})["pairing_guess"]["page"] == 7


def test_only_findings_compared_with_the_guessed_page_say_so() -> None:
    # A confirmed pair, the vendor's own drawings, GV's rule: no best guess.
    assert "pairing_guess" not in _stamped({"id": "f1", "page": 4, "vs_page": 2})
    assert "pairing_guess" not in _stamped({"id": "f2", "page": 3, "source": "vendor_arithmetic"})
    assert "pairing_guess" not in _stamped({"id": "f3", "page": 3, "source": "rules"})
    # A spot off the pair is not compared on it.
    off = _stamped({"id": "f4", "page": 3, "vs_page": 8})
    assert off[reference_page.KEY]["outside"] and "pairing_guess" not in off
    # A page confirmed later loses the mark when stamped again.
    marked = _stamped({"id": "f5", "page": 3, "vs_page": 7})
    confirmed = CrossPairs(pairs=GUESSES.pairs)
    assert "pairing_guess" not in _stamped(marked, confirmed)
    # A page the cross-file pairing does not decide is left as it is.
    assert reference_page.mark_guess({"id": "f6", "page": 9}, GUESSES.guesses) == {
        "id": "f6",
        "page": 9,
    }


# ---- the check's completeness ---------------------------------------------------------------------


def test_pairing_unsure_never_makes_the_check_incomplete_in_flag_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(FLAG_MODE_ENV, raising=False)
    run = _run(tmp_path / "proof", {}, unpaired={3: UNSURE_WHY})
    assert "pairing unsure" in (run._not_for_sending() or "")
    monkeypatch.setenv(FLAG_MODE_ENV, "flag")
    run = _run(tmp_path / "flag", {}, unpaired={3: UNSURE_WHY})
    assert run._not_for_sending() is None
    assert 3 in run._nothing_to_compare()  # Not compared, with its reason


# ---- made-up drawings ------------------------------------------------------------------------------


def _drawing(file: str = SHOP, page: int = 1, ref: str | None = None) -> Drawing:
    """A made-up drawing on its file's `page` (an architect page is numbered after the vendor's)."""
    architect = file == ARCH
    return Drawing(
        ref=ref or f"p{page}.v0",
        file=file,
        page=1000 + page if architect else page,
        file_page=page if architect else None,
        half=0,
        role="architect" if architect else "vendor",
        box=(0.0, 0.0, 600.0, 800.0),
        crop_box=(0.0, 0.0, 600.0, 800.0),
    )


def _candidate(item: str, page: int) -> AcrossCandidate:
    return AcrossCandidate(
        id=item, file=ARCH, page=page, drawings=(_drawing(ARCH, page, f"{item}.a1"),)
    )


def _work_item(
    file: str, page: int, where: tuple[str, int], *, guess: bool, ref: str | None = None
) -> WorkItem:
    vendor = _drawing(file, page, ref)
    return WorkItem(
        id=vendor.ref,
        file=file,
        page=page,
        vendor=vendor,
        architect=(_drawing(where[0], where[1], f"p{1000 + where[1]}.a1"),),
        paired_by="best guess" if guess else "paired",
        pairing_guess=guess,
    )
