"""The cloud PDF marks the AI page review's "different" rows (the AI reads, code decides).

* Each "different" row is a cloud at its `vendor_box`, "✗ Architect: X — vendor: Y (what it
  measures)"; a person who agreed keeps the cloud and gets the reviewer mark; "Not a difference"
  removes the cloud and is listed; a row with no `vendor_box` is listed with its page and words.
* The summary says "Compared with the architect (AI read): N same · M different · K not shown ·
  U unsure".
* One compare per drawing: with AI rows the old direct compare is not drawn, and the old one is
  drawn only while `GV_ARCH_COMPARE_RECORDS` is on. The draft reads live; the signed PDF reads the
  sign-off snapshot (`ai_review`); a snapshot without it is drawn exactly as before.

The backend's `ai_review_for_revision` is stubbed; all values are made up.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from reports.redline import AiReviewCounts, ReportMode, ReviewerMarkKind, render_redline
from storage.local import LocalStore
from tests.reports.test_redline import DOCUMENT, _finding, _package, _read_back, _reference
from workflow.redline_outputs import ai_review_input

WORDS = '✗ Architect: 36" — vendor: 35 1/2" (overall width)'
LINE = "Compared with the architect (AI read): 1 same · 1 different · 1 not shown · 2 unsure"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _box(page_number: int = 1) -> dict[str, Any]:
    """The contract's `vendor_box`: the same rectangle `_reference` encodes."""
    return {
        "document_version_id": str(DOCUMENT),
        "page_number": page_number,
        "polygon": [["0.10", "0.20"], ["0.50", "0.20"], ["0.50", "0.40"], ["0.10", "0.40"]],
    }


def _row(result: str = "different", **changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "key": f"air:{uuid4()}",
        "architect_text": '36"',
        "architect_inches": "36",
        "measures": "overall width",
        "vendor_text": '35 1/2"',
        "vendor_inches": "71/2",
        "result": result,
        "why": "made up",
        "vendor_box": _box(),
        "architect_ref": None,
        "actions": ["agree", "not_this"],
        "status": "open",
        "answer": None,
        "answered_by": None,
    }
    values.update(changes)
    return values


def _review(*rows: dict[str, object], page: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        pages=[SimpleNamespace(page=page, status="done", cost_usd="0.10", rows=list(rows))],
        counts={},
    )


def _pages(tmp_path: Path, review: object, *rotations: int) -> list[str]:
    architect, agreed = ai_review_input(review)
    artifact = render_redline(
        _package(*rotations),
        [_finding(refs=(_reference(page=5),))],
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        reviewer_marks=list(agreed) if agreed else None,
        architect=architect,
    )
    return [_flat(text) for text, _ in _read_back(tmp_path, artifact.key)]


EVERY_RESULT = (
    _row(),
    _row("same"),
    _row("not_shown", vendor_box=None),
    _row("unsure"),
    _row("unreadable"),
)


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_a_different_row_is_clouded_at_the_vendor_box_with_its_words(
    tmp_path: Path, rotation: int
) -> None:
    pages = _pages(tmp_path, _review(*EVERY_RESULT), rotation)

    assert f"A1 · {WORDS}" in pages[0], "the drawing says both numbers and what they measure"
    assert pages[0].count("Architect:") == 2, "one cloud label and one callout, only the different"
    summary = " ".join(pages[1:])
    assert LINE in summary
    assert f"A1 {WORDS} — Page 1" in summary
    assert "clouded on page 1" in summary


def test_a_different_row_with_no_vendor_box_is_listed_with_page_and_words(tmp_path: Path) -> None:
    pages = _pages(tmp_path, _review(_row(vendor_box=None), page=3))

    assert "Architect:" not in pages[0]
    summary = " ".join(pages[1:])
    assert f"A1 {WORDS} — Page 3" in summary
    assert "not clouded on the drawing: the vendor's dimension has no recorded place" in summary


def test_a_person_who_agreed_keeps_the_cloud_and_gets_the_mark(tmp_path: Path) -> None:
    pages = _pages(tmp_path, _review(_row(answer="agree", answered_by="AB")))

    assert f"A1 · {WORDS}" in pages[0]
    assert "R1 ✓ Agreed by AB: vendor is wrong" in pages[0]
    assert "Agreed by AB: the vendor is wrong." in " ".join(pages[1:])


def test_not_a_difference_removes_the_cloud_and_is_listed(tmp_path: Path) -> None:
    pages = _pages(tmp_path, _review(_row(answer="not_this", answered_by="AB")))

    assert "Architect:" not in pages[0]
    summary = " ".join(pages[1:])
    assert f"A1 {WORDS}" in summary
    assert "AB said it is not a difference, so it is not clouded" in summary


def test_the_counts_come_from_the_rows_and_the_marks_from_the_answers() -> None:
    architect, agreed = ai_review_input(
        {"pages": [{"page": 2, "rows": [*EVERY_RESULT, _row(answer="agree", answered_by="AB")]}]}
    )

    assert architect is not None
    assert architect.counts == AiReviewCounts(same=1, different=2, not_shown=1, unsure=2)
    assert [mark.kind for mark in agreed] == [ReviewerMarkKind.CONFIRMED]
    assert agreed[0].evidence_ref == _reference()


def test_a_review_with_no_rows_prints_nothing() -> None:
    assert ai_review_input(None) == (None, ())
    assert ai_review_input({"pages": [{"page": 1, "status": "not_run", "rows": []}]}) == (None, ())


def test_an_unknown_answer_or_result_is_refused_not_dropped() -> None:
    with pytest.raises(ValueError, match="maybe"):
        ai_review_input(_review(_row(answer="maybe", answered_by="AB")))
    with pytest.raises(ValueError, match="sideways"):
        ai_review_input(_review(_row("sideways")))


# ---------------------------------------------------------------------------
# The draft reads live; never the AI review and the old compare together
# ---------------------------------------------------------------------------


def _stub(monkeypatch: pytest.MonkeyPatch, name: str, function: str, value: object) -> list[object]:
    seen: list[object] = []

    def read(_session: object, revision: object) -> object:
        seen.append(revision)
        return value

    module = ModuleType(name)
    setattr(module, function, read)
    monkeypatch.setitem(sys.modules, name, module)
    return seen


def _draft_stubs(
    monkeypatch: pytest.MonkeyPatch, review: object, *, old_on: bool
) -> tuple[list[object], list[object]]:
    from workflow import draft_cloud_render

    ai = _stub(monkeypatch, "tests_stub_ai_review", "ai_review_for_revision", review)
    old = _stub(
        monkeypatch,
        "tests_stub_old_compare",
        "architect_compare_for_revision",
        ([SimpleNamespace(result="same", page=1, view=None)], {"same": 1}),
    )
    monkeypatch.setattr(draft_cloud_render, "AI_REVIEW_MODULE", "tests_stub_ai_review")
    monkeypatch.setattr(draft_cloud_render, "ARCHITECT_COMPARE_MODULE", "tests_stub_old_compare")
    monkeypatch.setattr(draft_cloud_render, "old_compare_switched_on", lambda: old_on)
    return ai, old


@pytest.mark.parametrize("old_on", [True, False])
def test_the_draft_draws_the_live_ai_review_and_not_the_old_compare(
    monkeypatch: pytest.MonkeyPatch, old_on: bool
) -> None:
    from workflow import draft_cloud_render

    revision = uuid4()
    ai, old = _draft_stubs(monkeypatch, _review(*EVERY_RESULT), old_on=old_on)

    architect, _agreed = draft_cloud_render.architect_compare_for_draft(
        None, revision  # type: ignore[arg-type]
    )

    assert ai == [revision] and old == []
    assert architect is not None and architect.counts.line == LINE


def test_the_old_compare_is_not_drawn_when_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow import draft_cloud_render

    _ai, old = _draft_stubs(monkeypatch, _review(), old_on=False)

    found = draft_cloud_render.architect_compare_for_draft(None, uuid4())  # type: ignore[arg-type]

    assert found == (None, ()) and old == []


def test_the_old_compare_is_drawn_while_switched_on_without_ai_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from workflow import draft_cloud_render

    _ai, old = _draft_stubs(monkeypatch, _review(), old_on=True)

    architect, _agreed = draft_cloud_render.architect_compare_for_draft(
        None, uuid4()  # type: ignore[arg-type]
    )

    assert len(old) == 1
    assert architect is not None and architect.counts.line.startswith(
        "Compared with the architect: 1 same"
    )


def test_the_switch_follows_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    import app.config
    from workflow import draft_cloud_render

    class Fake(BaseSettings):
        model_config = SettingsConfigDict(env_prefix="GV_")
        arch_compare_records: bool = False

    monkeypatch.setattr(app.config, "Settings", Fake)
    monkeypatch.delenv("GV_ARCH_COMPARE_RECORDS", raising=False)
    assert draft_cloud_render.old_compare_switched_on() is False
    monkeypatch.setenv("GV_ARCH_COMPARE_RECORDS", "true")
    assert draft_cloud_render.old_compare_switched_on() is True


# ---------------------------------------------------------------------------
# The signed PDF reads the sign-off snapshot only
# ---------------------------------------------------------------------------


def _old_frozen() -> SimpleNamespace:
    item = {
        "result": "different",
        "page": 1,
        "view": None,
        "architect_text": '24"',
        "vendor_text": '23"',
        "vendor_ref": None,
    }
    return SimpleNamespace(items=(item,), counts={"same": 0, "different": 1})


def test_the_signed_pdf_draws_the_frozen_ai_review_and_not_the_old_compare() -> None:
    from workflow.signed_outputs import frozen_compare_input

    frozen = {"pages": [{"page": 1, "rows": list(EVERY_RESULT)}], "counts": {}}
    architect, _agreed = frozen_compare_input(
        SimpleNamespace(ai_review=frozen, architect_compare=_old_frozen())
    )

    assert architect is not None and architect.counts.line == LINE
    assert [item.measures for item in architect.different] == ["overall width"]


def test_a_snapshot_without_the_ai_review_is_drawn_as_before() -> None:
    from workflow.signed_outputs import frozen_compare_input

    old, _ = frozen_compare_input(SimpleNamespace(architect_compare=_old_frozen()))
    assert old is not None
    assert old.counts.line.startswith("Compared with the architect: 0 same · 1 different")
    assert old.different[0].words == '✗ Architect: 24" — vendor: 23"'
    assert frozen_compare_input(SimpleNamespace()) == (None, ())
