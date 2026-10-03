"""Every script that builds the reader's settings passes every field they require (#902).

#869 made `FractionBarGeometry.turned_aspect_min` required. Every script that builds one was updated
except `scripts/fraction_parts_scorecard.py`, which then crashed before it scored anything, and no
test noticed because none runs that script's settings path. These settings have no defaults on
purpose, so a missing field is a crash, never a silent fallback. This test finds the crash before a
run does: it reads each script's source and checks that every call constructing one of the classes
below names every field the class requires.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

import pytest

from extraction.glyph_bands import FractionBarGeometry
from workflow.association import AssociationSettings, LocalizedOcrSettings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GUARDED = (FractionBarGeometry, AssociationSettings, LocalizedOcrSettings)


def _required(cls: type) -> frozenset[str]:
    return frozenset(
        field.name
        for field in dataclasses.fields(cls)
        if field.init
        and field.default is dataclasses.MISSING
        and field.default_factory is dataclasses.MISSING
    )


def _calls(source: str, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == name)
            or (isinstance(node.func, ast.Attribute) and node.func.attr == name)
        )
    ]


def _missing(source: str, cls: type) -> list[tuple[int, list[str]]]:
    """Each call to `cls` in `source` that leaves out a required field, by line.

    A call that spreads a mapping (`**settings`) or passes positional arguments is not checked
    field by field: what it passes cannot be read from the source.
    """
    required = _required(cls)
    found: list[tuple[int, list[str]]] = []
    for call in _calls(source, cls.__name__):
        if call.args or any(keyword.arg is None for keyword in call.keywords):
            continue
        named = {keyword.arg for keyword in call.keywords}
        if absent := sorted(required - named):
            found.append((call.lineno, absent))
    return found


@pytest.mark.parametrize("cls", GUARDED, ids=lambda cls: cls.__name__)
def test_every_script_passes_every_required_field(cls: type) -> None:
    gaps = {
        f"{path.name}:{line}": absent
        for path in sorted(SCRIPTS.glob("*.py"))
        for line, absent in _missing(path.read_text(), cls)
    }
    assert not gaps, f"{cls.__name__} built without required fields: {gaps}"


def test_the_guard_sees_a_missing_field() -> None:
    """The check itself: the call #902 found, with `turned_aspect_min` left out, is reported."""
    fields = sorted(_required(FractionBarGeometry) - {"turned_aspect_min"})
    source = "FractionBarGeometry(" + ", ".join(f"{name}=1" for name in fields) + ")\n"
    assert _missing(source, FractionBarGeometry) == [(1, ["turned_aspect_min"])]
    complete = (
        "FractionBarGeometry("
        + ", ".join(f"{name}=1" for name in sorted(_required(FractionBarGeometry)))
        + ")\n"
    )
    assert _missing(complete, FractionBarGeometry) == []


def test_the_guard_finds_calls_to_check() -> None:
    """Not vacuous: the scripts do build each guarded class somewhere."""
    for cls in GUARDED:
        assert any(
            _calls(path.read_text(), cls.__name__) for path in SCRIPTS.glob("*.py")
        ), cls.__name__


def test_the_fraction_scorecard_reads_every_setting_it_uses() -> None:
    """Passing the field is half of it: the setting must also be one the script reads (#902).

    `fraction_parts_scorecard.py` reads only the names in `READER_SETTINGS` from the settings
    file, so a `stated["…"]` lookup of any other name fails on the first run, as the first fix for
    #902 did with `GV_READER_FRACTION_TURNED_ASPECT_MIN`.
    """
    from scripts.fraction_parts_scorecard import READER_SETTINGS

    tree = ast.parse((SCRIPTS / "fraction_parts_scorecard.py").read_text())
    looked_up = {
        node.slice.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Name)
        and node.value.id == "stated"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, str)
    }
    assert looked_up, "the check found no settings lookups to compare"
    assert looked_up <= set(READER_SETTINGS), sorted(looked_up - set(READER_SETTINGS))
