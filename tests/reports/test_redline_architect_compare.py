"""The cloud PDF shows where the vendor's dimension differs from the architect's (direct compare).

* Each "different" result is a cloud at the vendor's dimension with the words
  "✗ Architect: Y — vendor: X": a glyph and words, the inches as printed, never colour alone.
* A person who agreed ("vendor is wrong") keeps the cloud and gets the existing reviewer mark; one
  who said "Not a difference" removes the cloud, and it is listed on the summary.
* The summary says "Compared with the architect: N same · M different · K for a person · R by a
  rule" whenever the compare ran; a render without the compare is unchanged.
* A "different" result with no place on the drawing is listed with its words, never dropped.

The backend's `architect_compare_for_revision` is stubbed; all values are made up.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from reports.redline import (
    ArchitectAnswer,
    ArchitectCompare,
    ArchitectCompareCounts,
    ArchitectDifference,
    ReportMode,
    ReviewerMarkKind,
    render_redline,
)
from storage.local import LocalStore
from tests.reports.test_redline import DOCUMENT, _finding, _package, _read_back, _reference
from workflow.redline_outputs import architect_compare_input

WORDS = '✗ Architect: 24" — vendor: 23 1/2"'
COUNTS = SimpleNamespace(same=7, different=1, rule=3, unpaired=1, unread=1)
LINE = "Compared with the architect: 7 same · 1 different · 2 for a person · 3 by a rule"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _vendor_ref(page_number: int = 1) -> dict[str, Any]:
    """The backend's `RowLocation` shape: the same rectangle `_reference` encodes."""
    return {
        "page_id": str(uuid4()),
        "document_version_id": str(DOCUMENT),
        "page_number": page_number,
        "polygon": [["0.10", "0.20"], ["0.50", "0.20"], ["0.50", "0.40"], ["0.10", "0.40"]],
    }


def _item(result: str = "different", **changes: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "key": f"arch:{uuid4()}",
        "page": 1,
        "view": "Plan A",
        "architect_text": '24"',
        "vendor_text": '23 1/2"',
        "result": result,
        "why": "made up",
        "vendor_ref": _vendor_ref(),
        "architect_ref": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _pages(tmp_path: Path, items: list[object], *rotations: int) -> list[str]:
    architect, agreed = architect_compare_input(items, COUNTS)
    artifact = render_redline(
        _package(*rotations),
        [_finding(refs=(_reference(page=5),))],
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        reviewer_marks=list(agreed) if agreed else None,
        architect=architect,
    )
    return [_flat(text) for text, _ in _read_back(tmp_path, artifact.key)]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_different_dimension_is_clouded_with_the_architect_and_vendor_words(
    tmp_path: Path, rotation: int
) -> None:
    pages = _pages(tmp_path, [_item(), _item("same")], rotation)

    assert f"A1 · {WORDS}" in pages[0], "the drawing says both numbers, with the glyph"
    summary = " ".join(pages[1:])
    assert LINE in summary
    assert f"A1 {WORDS} — Page 1, Plan A" in summary
    assert "clouded on page 1" in summary


def test_only_different_results_are_clouded(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_item("same"), _item("rule"), _item("unread")])

    assert "Architect:" not in pages[0]
    summary = " ".join(pages[1:])
    assert LINE in summary
    assert "None. No architect dimension was found different" in summary


def test_a_different_dimension_with_no_place_is_listed_with_its_words(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_item(vendor_ref=None), _item(vendor_ref=_vendor_ref(8))])

    assert WORDS not in pages[0]
    summary = " ".join(pages[1:])
    assert f"A1 {WORDS}" in summary and f"A2 {WORDS}" in summary
    assert "not clouded on the drawing: the vendor's dimension has no recorded place" in summary
    assert "not clouded on the drawing: it is on page 8" in summary


def test_a_person_who_agreed_keeps_the_cloud_and_gets_the_reviewer_mark(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_item(answer="agree", answered_by="AB")])

    assert f"A1 · {WORDS}" in pages[0]
    assert "R1 ✓ Agreed by AB: vendor is wrong" in pages[0]
    summary = " ".join(pages[1:])
    assert "Agreed by AB: the vendor is wrong." in summary
    assert "What a person answered" in summary


def test_not_a_difference_removes_the_cloud_and_is_listed(tmp_path: Path) -> None:
    pages = _pages(tmp_path, [_item(answer="not_a_difference", answered_by="AB")])

    assert "Architect:" not in pages[0]
    summary = " ".join(pages[1:])
    assert f"A1 {WORDS}" in summary
    assert "AB said it is not a difference, so it is not clouded" in summary


def test_a_render_without_the_compare_has_no_architect_section(tmp_path: Path) -> None:
    artifact = render_redline(
        _package(), [_finding(refs=(_reference(),))], ReportMode.INTERNAL, LocalStore(tmp_path)
    )
    pages = [_flat(text) for text, _ in _read_back(tmp_path, artifact.key)]

    assert "Compared with the architect" not in " ".join(pages)


def test_a_compare_that_has_not_run_prints_nothing() -> None:
    assert architect_compare_input([], SimpleNamespace(same=0)) == (None, ())


def test_the_counts_and_marks_from_the_backend_shape() -> None:
    architect, agreed = architect_compare_input(
        [_item(answer="agree", answered_by="AB"), _item("same"), _item("unpaired")],
        {"same": 1, "different": 1, "rule": 0, "unpaired": 1, "unread": 0},
    )

    assert architect is not None
    assert architect.counts == ArchitectCompareCounts(
        same=1, different=1, for_a_person=1, by_a_rule=0
    )
    assert [item.answer for item in architect.different] == [ArchitectAnswer.AGREED]
    assert [mark.kind for mark in agreed] == [ReviewerMarkKind.CONFIRMED]
    assert agreed[0].evidence_ref == _reference()


def test_an_unknown_answer_is_refused_not_dropped() -> None:
    with pytest.raises(ValueError, match="maybe"):
        architect_compare_input([_item(answer="maybe", answered_by="AB")], COUNTS)


def test_values_are_checked() -> None:
    with pytest.raises(ValueError):
        ArchitectDifference(architect_text=" ", vendor_text='24"', subject="Page 1")
    with pytest.raises(ValueError):
        # no recorded place and no reason why: refused
        ArchitectDifference(architect_text='24"', vendor_text='24"', subject="Page 1")
    with pytest.raises(ValueError):
        ArchitectCompareCounts(same=-1, different=0, for_a_person=0, by_a_rule=0)
    with pytest.raises(TypeError):
        ArchitectCompare(counts=COUNTS)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# The draft reads the live compare; the backend function is stubbed
# ---------------------------------------------------------------------------


def test_the_draft_reads_nothing_before_the_compare_module_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow import draft_cloud_render

    monkeypatch.setattr(draft_cloud_render, "ARCHITECT_COMPARE_MODULE", "app.review.no_such_mod")
    monkeypatch.setattr(draft_cloud_render, "AI_REVIEW_MODULE", "app.review.no_such_mod")

    found = draft_cloud_render.architect_compare_for_draft(None, uuid4())  # type: ignore[arg-type]

    assert found == (None, ())


def test_the_draft_reads_the_live_compare(monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow import draft_cloud_render

    revision_id = uuid4()
    seen: list[object] = []

    def compare_for(_session: object, revision: object) -> tuple[list[object], object]:
        seen.append(revision)
        return [_item(), _item("same")], COUNTS

    stub = ModuleType("tests_stub_architect_compare")
    stub.architect_compare_for_revision = compare_for  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tests_stub_architect_compare", stub)
    monkeypatch.setattr(
        draft_cloud_render, "ARCHITECT_COMPARE_MODULE", "tests_stub_architect_compare"
    )
    # The old compare only, switched on: no AI page review on this revision.
    monkeypatch.setattr(draft_cloud_render, "AI_REVIEW_MODULE", "app.review.no_such_mod")
    monkeypatch.setattr(draft_cloud_render, "old_compare_switched_on", lambda: True)

    architect, agreed = draft_cloud_render.architect_compare_for_draft(
        None, revision_id  # type: ignore[arg-type]
    )

    assert seen == [revision_id]
    assert architect is not None and len(architect.different) == 1
    assert architect.counts.line == LINE
    assert agreed == ()
