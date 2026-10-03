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
import re
from decimal import Decimal
from pathlib import Path

import pytest

from extraction.glyph_bands import FractionBarGeometry
from extraction.reader import MissingSpace
from workflow.association import AssociationSettings, LocalizedOcrSettings

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
GUARDED = (FractionBarGeometry, AssociationSettings, LocalizedOcrSettings, MissingSpace)

#: The reader's functions that read a page's text, each of which requires the reader's setting
#: (#912): a script that calls one without it crashes on its first page, as #902's did.
READERS = ("read_page_contents", "read_stamp_text", "coloured_text")

#: The scripts whose stages read a page's text, and so must be built with the reader's setting
#: (#912). A script that builds stages and runs extraction must be named here.
STAGES_THAT_READ = (
    "drain_outbox.py",
    "evaluate_goldset.py",
    "fraction_parts_scorecard.py",
    "seed_demo.py",
)


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


def _names(call: ast.Call, keyword: str) -> bool:
    """Whether `call` passes `keyword`, or spreads a mapping that may hold it."""
    return any(item.arg in (keyword, None) for item in call.keywords)


@pytest.mark.parametrize("reader", READERS)
def test_every_script_that_reads_a_page_passes_the_missing_space_setting(reader: str) -> None:
    """**No default** (#912). Each call a script makes to a reader function states the setting."""
    gaps = [
        f"{path.name}:{call.lineno}"
        for path in sorted(SCRIPTS.glob("*.py"))
        for call in _calls(path.read_text(), reader)
        if not _names(call, "missing_space")
    ]
    assert not gaps, f"{reader} called without missing_space: {gaps}"


def test_every_script_whose_stages_read_a_page_builds_them_with_the_setting() -> None:
    """`DatabaseStages` reads no page without the reader's setting (#912), and a script that builds
    it and runs extraction must say so: one forgotten fails on its first drawing, after any setup.
    """
    reading = {
        path.name: path.read_text()
        for path in sorted(SCRIPTS.glob("*.py"))
        if _calls(path.read_text(), "DatabaseStages")
        and re.search(r"\b(extract_pages|run_all|run_stage)\(", path.read_text())
    }
    assert reading, "the check found no script whose stages read a page"
    assert sorted(reading) == sorted(STAGES_THAT_READ), "name each script whose stages read here"
    for name, source in reading.items():
        assert any(
            _names(call, "missing_space") for call in _calls(source, "DatabaseStages")
        ), f"{name} builds its stages without missing_space"


def test_the_demo_states_the_reader_setting_once_for_everything_it_starts() -> None:
    """The worker block states it, as every `GV_READER_*` setting is stated there, and the seed,
    which reads its synthetic drawing with the same reader, is given the same value (#912)."""
    demo = (SCRIPTS / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    stated = re.findall(r"^GV_READER_MISSING_SPACE_HEIGHTS=(\S+) \\$", worker_block, re.MULTILINE)
    everywhere = re.findall(r"GV_READER_MISSING_SPACE_HEIGHTS=(\S+)", demo)

    assert len(stated) == 1, "the worker block does not state GV_READER_MISSING_SPACE_HEIGHTS"
    assert len(everywhere) == 2, "stated for the worker and for the seed, and nowhere else"
    assert set(everywhere) == set(stated)
    assert MissingSpace(gap_heights=Decimal(stated[0])).gap_heights == Decimal("0.1")


def test_every_script_that_reads_the_setting_from_the_demo_finds_it() -> None:
    """Passing the setting is half of it: each script that reads it from a `scripts/demo.sh`-style
    file must read it from there, or its first page fails (#902's lesson, for #912's setting)."""
    from scripts.author_reading_answer_key import _frame_settings, _stated_missing_space
    from scripts.fraction_parts_scorecard import READER_SETTINGS, read_stated
    from scripts.gate_replay import read_settings

    demo = SCRIPTS / "demo.sh"
    stated = MissingSpace(gap_heights=Decimal("0.1"))

    assert _stated_missing_space(demo) == stated
    assert _frame_settings(demo)["GV_READER_MISSING_SPACE_HEIGHTS"] == "0.1"
    assert read_settings(demo)["GV_READER_MISSING_SPACE_HEIGHTS"] == "0.1"
    assert read_stated(demo, READER_SETTINGS)["GV_READER_MISSING_SPACE_HEIGHTS"] == "0.1"
