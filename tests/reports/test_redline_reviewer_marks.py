"""A person's work is visible on the cloud PDF, with a glyph and words, and nothing is dropped (#1363).

* A confirmation reads "✓ Confirmed by AB". It is a person's answer, never a check result, so a
  confirmed reading never gets a PASS mark.
* A fix reads "✎ Fixed by AB: AI read X → Y".
* An answer (walls, a page, a result checked by hand) is drawn where code recorded a place for it,
  and listed with its reason where it has none.
* A check that could not run is printed "❍ NOT CHECKED", never with a judged outcome.
* Every mark is listed on the summary pages, placed or not.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reports.redline import (
    NOT_CHECKED,
    RedlineFinding,
    ReportMode,
    ReviewerMark,
    ReviewerMarkKind,
    render_redline,
)
from storage.local import LocalStore
from tests.reports.test_redline import _finding, _package, _read_back, _reference
from verdict.outcomes import Outcome, Severity


def _flat(text: str) -> str:
    return " ".join(text.split())


def _pages(
    tmp_path: Path, findings: list[object], marks: list[ReviewerMark] | None, *rotations: int
) -> list[str]:
    artifact = render_redline(
        _package(*rotations),
        findings,  # type: ignore[arg-type]
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        reviewer_marks=marks,
    )
    return [_flat(text) for text, _ in _read_back(tmp_path, artifact.key)]


def _confirmed(**changes: object) -> ReviewerMark:
    values: dict[str, object] = {
        "kind": ReviewerMarkKind.CONFIRMED,
        "words": "Confirmed by AB",
        "subject": "Page 1: countertop piece width 24",
        "evidence_ref": _reference(),
    }
    values.update(changes)
    return ReviewerMark(**values)  # type: ignore[arg-type]


def test_a_confirmation_is_drawn_with_its_glyph_and_words_and_never_as_pass(
    tmp_path: Path,
) -> None:
    pages = _pages(tmp_path, [_finding("CT-2", Outcome.REVIEW_REQUIRED)], [_confirmed()])

    assert "✓ Confirmed by AB" in pages[0]
    assert "PASS" not in pages[0], "a person's confirmation is not a check that passed"


def test_a_fix_says_what_the_ai_read_and_what_the_person_fixed_it_to(tmp_path: Path) -> None:
    fixed = ReviewerMark(
        kind=ReviewerMarkKind.FIXED,
        words='Fixed by AB: AI read 35 1/2" → 36"',
        subject="Page 1: countertop piece width",
        evidence_ref=_reference(),
    )
    pages = _pages(tmp_path, [_finding("CT-2", Outcome.REVIEW_REQUIRED)], [fixed])

    assert '✎ Fixed by AB: AI read 35 1/2" → 36"' in pages[0]


def test_an_answer_with_no_recorded_place_is_listed_with_its_reason(tmp_path: Path) -> None:
    answer = ReviewerMark(
        kind=ReviewerMarkKind.ANSWERED,
        words="Checked by reviewer AB: OK",
        subject="Countertop A — CT-DEPTH-001",
        unplaced_reason="this result is about a whole countertop, which has no one place",
    )
    pages = _pages(tmp_path, [_finding("CT-2", Outcome.REVIEW_REQUIRED)], [answer])

    assert "✍ Checked by reviewer AB: OK" not in pages[0]
    summary = " ".join(pages[1:])
    assert "What a person answered" in summary
    assert "✍ Checked by reviewer AB: OK" in summary
    assert "which has no one place" in summary


def test_a_mark_on_a_page_outside_this_drawing_is_listed_not_dropped(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_finding()], [_confirmed(evidence_ref=_reference(page=7))])

    assert "✓ Confirmed by AB" not in pages[0]
    summary = " ".join(pages[1:])
    assert "✓ Confirmed by AB" in summary
    assert "not on the drawing" in summary


def test_every_mark_is_listed_on_the_summary_even_when_drawn(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_finding()], [_confirmed()])

    summary = " ".join(pages[1:])
    assert "R1 ✓ Confirmed by AB" in summary
    assert "marked on page 1" in summary


def test_a_new_snapshot_with_no_answers_says_so(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_finding()], [])

    summary = " ".join(pages[1:])
    assert "What a person answered" in summary
    assert "None. No person confirmed, fixed or answered anything" in summary


def test_an_old_snapshot_has_no_answer_section(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_finding()], None)

    assert "What a person answered" not in " ".join(pages)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_marks_read_on_a_rotated_sheet(tmp_path: Path, rotation: int) -> None:
    pages = _pages(tmp_path, [_finding()], [_confirmed()], rotation)

    assert "✓ Confirmed by AB" in pages[0]


def test_a_check_that_could_not_run_is_printed_not_checked_with_a_glyph(tmp_path: Path) -> None:
    waiting = RedlineFinding(
        rule_id="CT-DEPTH-001",
        outcome=Outcome.REVIEW_REQUIRED,
        severity=Severity.CRITICAL,
        reason=f"{NOT_CHECKED}: Waiting for the depth reading.",
        snapshot_id="snapshot-0000000000",
        engine_version="test",
        evidence_refs=(_reference(),),
        not_checked=True,
    )
    pages = _pages(tmp_path, [waiting], [])

    assert f"❍ {NOT_CHECKED}" in pages[0]
    assert "REVIEW_REQUIRED" not in pages[0], "a check that never ran is not shown as judged"


def test_a_person_marked_wrong_is_drawn_with_a_cross(tmp_path: Path) -> None:
    wrong = ReviewerMark(
        kind=ReviewerMarkKind.CHECKED_WRONG,
        words="Checked by reviewer AB: WRONG",
        subject="Page 1: Countertop row — CT-WIDTH-001",
        evidence_ref=_reference(),
    )
    pages = _pages(tmp_path, [_finding()], [wrong])

    assert "✗ Checked by reviewer AB: WRONG" in pages[0]


def test_a_mark_needs_words() -> None:
    with pytest.raises(ValueError):
        _confirmed(words=" ")


def test_several_answers_about_one_place_stack_rather_than_print_over_each_other(
    tmp_path: Path,
) -> None:
    marks = [_confirmed(words=f"Checked by reviewer {who}: OK") for who in ("AB", "CD", "EF")]
    pages = _pages(tmp_path, [_finding()], marks)

    for number, who in enumerate(("AB", "CD", "EF"), start=1):
        assert f"R{number} ✓ Checked by reviewer {who}: OK" in pages[0]
