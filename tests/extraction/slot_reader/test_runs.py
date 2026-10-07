"""Each slot's whole label and the crop a reader is shown of it (#987).

Verification for: `extraction/slot_reader/runs.py`. Sheets are hand-built (`sheets.py`); no client
drawing is read.
"""

from __future__ import annotations

import ast
from dataclasses import fields, is_dataclass, replace
from decimal import Decimal
from enum import StrEnum
from pathlib import Path

import pytest

from extraction.geometry.rows import MEASURED_SETTINGS, Box, PageRows
from extraction.rows import page_rows_and_ink
from extraction.slot_reader.runs import (
    E2_CROP_SETTINGS,
    Lane,
    SlotPlan,
    choose_row,
    plan_slots,
)
from tests.extraction.slot_reader import sheets

DPI = 150


def _plan(data: bytes) -> SlotPlan:
    rows = page_rows_and_ink(data, 0, dpi=DPI, settings=MEASURED_SETTINGS)
    return plan_slots(
        rows.candidates.rows, rows.ink, settings=E2_CROP_SETTINGS, row_settings=MEASURED_SETTINGS
    )


def test_text_labels_are_one_label_per_slot_with_the_files_own_text() -> None:
    plan = _plan(sheets.sheet(sheets.text_labels()))

    assert plan.row is not None and plan.ambiguity is None
    assert [[label.text for label in slot.labels] for slot in plan.slots] == [
        ['12"'],
        ['24"'],
        ['36"'],
    ]
    assert all(label.lane is Lane.TEXT for slot in plan.slots for label in slot.labels)
    assert plan.overall is not None
    assert [label.text for label in plan.overall.labels] == ['72"']


def test_a_glyph_label_keeps_its_straight_strokes() -> None:
    """The row builder locates a glyph label by its curves; a `1` drawn as one straight stroke is
    left out of that box. The run a reader is shown must hold it, or `13` is cropped as `3`."""
    data = sheets.sheet(sheets.glyph_labels())
    rows = page_rows_and_ink(data, 0, dpi=DPI, settings=MEASURED_SETTINGS)
    row = rows.candidates.rows.candidates[0]
    plan = _plan(data)

    for slot, planned in zip(row.slots, plan.slots, strict=True):
        assert slot.label is not None
        (label,) = planned.labels
        assert label.lane is Lane.GLYPHS and label.text is None
        assert label.box.x0 < slot.label.box.x0, "the straight stroke was left out of the run"
        assert label.crop.x0 < label.box.x0 and label.crop.x1 > label.box.x1


def test_a_crop_never_shows_a_neighbouring_label() -> None:
    """Narrow slots are cropped with their ticks, and each crop stops short of every other run."""
    plan = _plan(sheets.sheet(sheets.text_labels()))
    labels = [label for owner in (*plan.slots, plan.overall) if owner for label in owner.labels]

    for label in labels:
        for other in labels:
            if other is not label:
                assert not label.crop.meets(other.box) or _inside(other.box, label.box)


def _inside(inner: Box, outer: Box) -> bool:
    return outer.x0 <= inner.x0 and inner.x1 <= outer.x1 and outer.top <= inner.top <= outer.bottom


def test_a_narrow_slot_is_cropped_with_its_ticks() -> None:
    plan = _plan(sheets.sheet(sheets.text_labels()))

    first = plan.slots[0]
    (label,) = first.labels
    assert label.ticks_in_crop
    assert label.crop.x0 <= first.x0 and label.crop.x1 >= first.x1


def test_a_label_at_the_drawings_edge_is_flagged() -> None:
    """The row is drawn just under the stamp's visible top (appearance y 700) and the first label
    runs off that edge: the file holds only part of it, so it is unreadable by geometry."""
    chain_y, overall_y = 690.0, 675.0
    drawing = sheets.text(168, 697, '12"')
    drawing += sheets.text(243, chain_y + 4, '24"') + sheets.text(318, chain_y + 4, '36"')
    drawing += sheets.text(243, overall_y + 4, '72"')
    plan = _plan(sheets.sheet(drawing, chain_y=chain_y, overall_y=overall_y))

    flags = [[label.touches_edge for label in slot.labels] for slot in plan.slots]
    assert flags == [[True], [False], [False]]


def test_a_page_with_no_candidate_has_no_row() -> None:
    plan = plan_slots(
        PageRows(candidates=(), rejected=()),
        page_rows_and_ink(
            sheets.sheet(sheets.text_labels()), 0, dpi=DPI, settings=MEASURED_SETTINGS
        ).ink,
        settings=E2_CROP_SETTINGS,
        row_settings=MEASURED_SETTINGS,
    )
    assert plan.row is None and plan.slots == () and plan.overall is None


def test_a_row_without_an_overall_is_never_trusted_silently() -> None:
    rows = page_rows_and_ink(
        sheets.sheet(sheets.text_labels()), 0, dpi=DPI, settings=MEASURED_SETTINGS
    ).candidates.rows
    first = replace(rows.candidates[0], overall=None)

    row, why = choose_row(PageRows(candidates=(first,), rejected=()))
    assert row is first and why is not None and "no overall" in why


def test_a_tie_with_the_second_row_is_never_picked_silently() -> None:
    rows = page_rows_and_ink(
        sheets.sheet(sheets.text_labels()), 0, dpi=DPI, settings=MEASURED_SETTINGS
    ).candidates.rows
    first = rows.candidates[0]
    twin = replace(first, y=first.y + 100, rank=2)

    _, why = choose_row(PageRows(candidates=(first, twin), rejected=()))
    assert why == "another row on the page fits as well"
    fewer = replace(twin, labelled=first.labelled - 1)
    _, why = choose_row(PageRows(candidates=(first, fewer), rejected=()))
    assert why is None


def _leaves(value: object) -> list[object]:
    if is_dataclass(value) and not isinstance(value, type):
        return [leaf for field in fields(value) for leaf in _leaves(getattr(value, field.name))]
    if isinstance(value, tuple | list):
        return [leaf for item in value for leaf in _leaves(item)]
    return [value]


def test_the_plan_holds_no_float() -> None:
    leaves = _leaves(_plan(sheets.sheet(sheets.glyph_labels())))
    assert leaves
    assert not any(isinstance(leaf, float) for leaf in leaves)
    assert all(
        isinstance(leaf, str | bool | int | Decimal | StrEnum) or leaf is None for leaf in leaves
    )


def test_the_same_page_gives_the_same_plan_twice() -> None:
    data = sheets.sheet(sheets.glyph_labels())
    assert _plan(data) == _plan(data)


def test_crop_settings_refuse_a_float() -> None:
    with pytest.raises(TypeError):
        replace(E2_CROP_SETTINGS, padding_pt=4.0)  # type: ignore[arg-type]


PACKAGE = Path(__file__).resolve().parents[3] / "extraction" / "slot_reader"


def test_the_slot_reader_never_uses_a_float_and_never_imports_the_verdict() -> None:
    """No float in anything that decides (exact match makes a float's rounding the verdict), and
    `verdict/` is not reachable from here. `tests/test_verdict_isolation.py` guards the reverse."""
    # `bedrock.py` is the network edge: its only float is a back-off delay in seconds.
    for path in sorted(PACKAGE.glob("*.py")):
        if path.name == "bedrock.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                assert node.id != "float", f"{path.name} uses float"
            if isinstance(node, ast.Constant):
                assert not isinstance(node.value, float), f"{path.name} has a float literal"
            modules = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            )
            for module in modules:
                assert not module.startswith(("verdict", "rules")), f"{path.name} imports {module}"
