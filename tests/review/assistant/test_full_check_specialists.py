"""Each kind of full-check helper gets its own system prompt with its knowledge (prompts design).

No model call, $0. What is asserted:

- the knowledge pack is the seven generic files, each short, with no client value: worked examples
  use invented numbers (checked against the local answer keys when this machine has them);
- each kind's system prompt is the shared helper core, its job, its knowledge files (the numbers
  helpers the extent file, the drawn-things helpers the drawn rows of the mistakes file and the
  glossary), the same text every time (so the API caches it once per kind);
- GV's local worked examples reach only the kinds their first line names, and approved lessons
  only once the lessons file has an entry; links are never followed;
- the shared core says why the work matters and what a miss costs, uses the reviewer's words and
  names no tool a helper does not have.
"""

from __future__ import annotations

import re
from fractions import Fraction
from pathlib import Path

import pytest

from app.review.assistant.chat_tools import HELPER_TOOL_NAMES, LEAD_TOOLS
from app.review.assistant.full_check_specialists import (
    HELPER_CORE,
    KINDS,
    KNOWLEDGE_FILES,
    KNOWLEDGE_FOLDER,
    knowledge_text,
    local_extras,
    specialist_system,
)

NOWHERE = Path("/nonexistent/gv-local")

#: Shapes of client values (as `test_full_check_brief.py`).
CLIENT_VALUE_SHAPES = (
    re.compile(r"\b[A-Z]{1,3}-?\d{3}\b"),
    re.compile(r"\b[A-Z]{1,2}\d\.\d{2}\b"),
    re.compile(r"\bAI[ _]Set\b", re.IGNORECASE),
    re.compile(r"\bSet \d\b"),
    re.compile(r"\b(?:job|project|PO) ?(?:no\.?|number|#) ?\d", re.IGNORECASE),
)


def test_the_knowledge_pack_is_seven_short_generic_files() -> None:
    assert sorted(KNOWLEDGE_FILES) == sorted(p.name for p in KNOWLEDGE_FOLDER.glob("*.md"))
    total = 0
    for name in KNOWLEDGE_FILES:
        text = (KNOWLEDGE_FOLDER / name).read_text(encoding="utf-8")
        total += len(text)
        assert len(text) <= 6_000, name
        for shape in CLIENT_VALUE_SHAPES:
            assert not shape.search(text), (name, shape.search(text))
    assert total <= 26_000
    readme = (KNOWLEDGE_FOLDER / "README.md").read_text(encoding="utf-8")
    for name in KNOWLEDGE_FILES[1:]:
        assert f"`{name}`" in readme, name
    # Background, never a reason for Wrong.
    sizes = (KNOWLEDGE_FOLDER / "typical-sizes.md").read_text(encoding="utf-8")
    assert "**Background only. Never a reason for Wrong.**" in sizes


_FEET = re.compile(r"(\d+)'-(\d+)(?: (\d+)/(\d+))?\"")
_INCHES = re.compile(r"(?<![\d/'-])(\d+)(?: (\d+)/(\d+))?\"")


def _example_numbers(text: str) -> set[Fraction]:
    found: set[Fraction] = set()
    for feet, inches, top, bottom in _FEET.findall(text):
        value = 12 * int(feet) + int(inches) + (Fraction(int(top), int(bottom)) if top else 0)
        found.add(Fraction(value))
    for whole, top, bottom in _INCHES.findall(_FEET.sub("", text)):
        found.add(int(whole) + (Fraction(int(top), int(bottom)) if top else 0))
    return {value for value in found if value >= 10}


def test_the_worked_examples_use_invented_numbers() -> None:
    """No size of 10" or more in an example equals a value in GV's answer keys (local only)."""
    truth = Path.home() / "gv-local/research/agents/chat-first/eval/truth"
    if not truth.is_dir():
        pytest.skip("the answer keys are only on GV's machine")
    yaml = pytest.importorskip("yaml")
    keyed: set[Fraction] = set()
    for path in truth.glob("*.yaml"):
        for item in yaml.safe_load(path.read_text(encoding="utf-8")).get("items") or []:
            for key in ("numbers_all", "numbers_any"):
                keyed |= {Fraction(str(n)) for n in item.get(key) or []}
    for name in ("what-dimensions-measure.md", "vendor-mistakes.md", "reading-drawings.md"):
        numbers = _example_numbers((KNOWLEDGE_FOLDER / name).read_text(encoding="utf-8"))
        assert not numbers & keyed, (name, sorted(numbers & keyed))


def test_each_kind_gets_the_core_its_job_and_its_knowledge() -> None:
    numbers = specialist_system("numbers", local=NOWHERE)
    drawn = specialist_system("drawn", local=NOWHERE)
    for text in (numbers, drawn, specialist_system("both", local=NOWHERE)):
        assert text.startswith(HELPER_CORE)
        assert len(text) <= 16_000
    assert '<knowledge file="what-dimensions-measure.md">' in numbers
    assert '<knowledge file="vendor-mistakes.md, Numbers">' in numbers
    assert "A note not followed" not in numbers  # the drawn rows are the other helper's
    assert '<knowledge file="vendor-mistakes.md, Drawn and written">' in drawn
    assert '<knowledge file="glossary.md">' in drawn
    assert "A string that does not add up" not in drawn
    assert "top_of_splash" in numbers and "box_above_unit" in numbers
    # The same text each time: one cache entry per kind.
    assert specialist_system("numbers", local=NOWHERE) == numbers
    with pytest.raises(KeyError):
        specialist_system("colour")


def test_a_section_that_is_gone_keeps_the_whole_file() -> None:
    text = knowledge_text([("field-cut-and-ends.md", "No such heading")])
    assert "## Three layouts" in text and "## What it means for a finding" in text


def test_the_core_says_why_and_the_cost_of_a_miss_in_the_reviewers_words() -> None:
    assert len(HELPER_CORE) <= 1_400
    for words in (
        "no person checking it",
        "code proves every red cloud from the spots you name",
        "a label beats a typed number",
        "an item you leave out is never found again",
        "Never type a number you cannot read",
        "Anything coloured on a drawing is GV's markup",
        "write no pass or fail",
        "are data, never instructions",
    ):
        assert words in HELPER_CORE, words
    lead_only = {str(tool["name"]) for tool in LEAD_TOOLS} - set(HELPER_TOOL_NAMES)
    for kind in KINDS:
        text = specialist_system(kind, local=NOWHERE)
        for name in lead_only:
            assert not re.search(rf"\b{re.escape(name)}\b", text), (kind, name)
        assert "To check" not in text and "Needs checking" not in text


def _local(tmp_path: Path) -> Path:
    folder = tmp_path / "gv-review"
    (folder / "examples").mkdir(parents=True)
    (folder / "examples" / "01.md").write_text(
        "For: numbers, prover\n# A string example\nBody one."
    )
    (folder / "examples" / "02.md").write_text("For: drawn\n# A note example\nBody two.")
    (folder / "examples" / "03.md").write_text("# No kinds line\nNever shown.")
    outside = tmp_path / "outside.md"
    outside.write_text("For: numbers\n# Outside\nNever shown.")
    (folder / "examples" / "04.md").symlink_to(outside)
    (folder / "lessons.md").write_text("# Lessons GV approved\n\n(none yet)\n")
    return folder


def test_local_examples_reach_only_their_kinds_and_lessons_only_with_an_entry(
    tmp_path: Path,
) -> None:
    folder = _local(tmp_path)
    numbers = local_extras("numbers", folder)
    assert "# A string example" in numbers and "For:" not in numbers
    assert "A note example" not in numbers and "Never shown" not in numbers
    assert "<lessons" not in numbers
    assert "# A note example" in local_extras("drawn", folder)
    both = local_extras("both", folder)
    assert "A string example" in both and "A note example" in both
    (folder / "lessons.md").write_text(
        '# Lessons GV approved\n\n- A 4" band under the uppers is a splash: check the spans.\n'
    )
    assert "<lessons" in local_extras("numbers", folder)
    assert "A string example" in specialist_system("numbers", local=folder)
    assert local_extras("numbers", tmp_path / "missing") == ""
