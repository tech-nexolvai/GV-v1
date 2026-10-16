"""The cloud PDF marks the AI page review's "different" rows (the AI reads, code decides).

* Each "different" row is a cloud at its `vendor_box`, by itself (decision 2026-10-16: no person
  answers it), "✗ Needs: X (per architect: what it measures) — Vendor drew: Y"; an old stored
  answer changes nothing and adds no mark; a row with no `vendor_box` is listed with its page and
  words. Same, not-shown and unsure rows are never clouded.
* The summary says "Compared with the architect (AI read): N same · M different · K not shown ·
  U unsure", then "Differences found by the AI reading; check against the drawings."
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

from reports.redline import AiReviewCounts, ReportMode, render_redline
from storage.local import LocalStore
from tests.reports.test_redline import DOCUMENT, _finding, _package, _read_back, _reference
from workflow.redline_outputs import ai_review_input

WORDS = '✗ Needs: 36" (per architect: overall width) — Vendor drew: 35 1/2"'
LINE = "Compared with the architect (AI read): 1 same · 1 different · 1 not shown · 2 unsure"
NOTE = "Differences found by the AI reading; check against the drawings."


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
        "actions": [],
        "status": "done",
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
    assert pages[0].count("Needs:") == 2, "one cloud label and one callout, only the different"
    assert "Architect:" not in pages[0]
    summary = " ".join(pages[1:])
    assert f"{LINE} {NOTE}" in summary
    assert f"A1 {WORDS} — Page 1" in summary
    assert "clouded on page 1" in summary


def test_a_different_row_with_no_vendor_box_is_listed_with_page_and_words(tmp_path: Path) -> None:
    pages = _pages(tmp_path, _review(_row(vendor_box=None), page=3))

    assert "Needs:" not in pages[0]
    summary = " ".join(pages[1:])
    assert f"A1 {WORDS} — Page 3" in summary
    assert "not clouded on the drawing: the vendor's dimension has no recorded place" in summary


@pytest.mark.parametrize("answer", ["agree", "not_this", "maybe"])
def test_an_old_answer_changes_nothing_the_row_is_clouded_with_no_mark(
    tmp_path: Path, answer: str
) -> None:
    pages = _pages(tmp_path, _review(_row(answer=answer, answered_by="AB", status="done")))

    assert f"A1 · {WORDS}" in pages[0]
    assert "Agreed by" not in " ".join(pages)
    assert "not a difference" not in " ".join(pages)
    assert "clouded on page 1" in " ".join(pages[1:])


def test_the_architect_value_in_feet_also_says_its_inches(tmp_path: Path) -> None:
    pages = _pages(
        tmp_path,
        _review(
            _row(
                architect_text="2'-9\"",
                architect_inches="33",
                measures="floor to top of counter",
                vendor_text='34"',
            ),
            _row(
                architect_text="2'-9 1/2\"", architect_inches="67/2", measures="a", vendor_text="b"
            ),
        ),
    )

    assert (
        'A1 · ✗ Needs: 2\'-9" (33") (per architect: floor to top of counter) — Vendor drew: 34"'
        in pages[0]
    )
    assert 'Needs: 2\'-9 1/2" (33 1/2") (per architect: a) — Vendor drew: b' in pages[0]


def test_the_counts_come_from_the_rows_and_no_answer_makes_a_mark() -> None:
    architect, agreed = ai_review_input(
        {"pages": [{"page": 2, "rows": [*EVERY_RESULT, _row(answer="agree", answered_by="AB")]}]}
    )

    assert architect is not None
    assert architect.counts == AiReviewCounts(same=1, different=2, not_shown=1, unsure=2)
    assert agreed == ()
    assert [item.answer for item in architect.different] == [None, None]
    assert [item.evidence_ref for item in architect.different] == [_reference(), _reference()]


def test_a_review_with_no_rows_prints_nothing() -> None:
    assert ai_review_input(None) == (None, ())
    assert ai_review_input({"pages": [{"page": 1, "status": "not_run", "rows": []}]}) == (None, ())


def test_an_unknown_result_is_refused_not_dropped() -> None:
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
