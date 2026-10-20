"""The roster's helper instructions and the summary's words after test round 1 (#1805).

Synthetic values only: no network, no paid call, no Docker.

- The drawn-things helpers compare every tag and model by its code, and count them on their
  checklist line (a sink's model tag was compared by its bowl width instead).
- The tools' arguments and the set pack's JSON shapes are in every helper's instructions; `view`
  says it takes no box; the tag reader never prints whole files.
- A stopped check's summary says how many pages were not opened, never "Every item could be
  compared".
- The same vendor string read twice on one page (left and right copies) is one item.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import full_check_roster
from app.review.assistant.chat_tools import HELPER_TOOLS
from app.review.assistant.classification import not_compared_line
from app.review.assistant.full_check_brief import DRAWN_BRIEF, LENS_FIELDS, checklist_gaps
from app.review.assistant.full_check_specialists import KINDS, PACK_SHAPES, specialist_system
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import Harness
from tests.review.assistant.test_chat_engine_findings import _modules
from tests.review.assistant.test_chat_engine_ledger import _blocks, _checks
from tests.review.assistant.test_chat_findings import workspace_with_pdf


def test_the_drawn_helpers_compare_tags_and_models_by_their_code() -> None:
    assert "tags" in LENS_FIELDS["drawn"]
    assert "TAGS AND MODELS" in DRAWN_BRIEF and "a size never stands for a model" in DRAWN_BRIEF
    assert "tags a/b" in DRAWN_BRIEF
    done = "CHECKLIST p4: notes 1/1; tags 2/2; counts 3/3; hardware 0/0; not compared: none"
    assert checklist_gaps(done, [4], "drawn") == {}
    old = "CHECKLIST p4: notes 1/1; counts 3/3; hardware 0/0; not compared: none"
    assert checklist_gaps(old, [4], "drawn") == {4: "no count for tags"}
    short = "CHECKLIST p4: notes 1/1; tags 1/2; counts 3/3; hardware 0/0; not compared: none"
    assert "tags not all done" in checklist_gaps(short, [4], "drawn")[4]


def test_every_helper_knows_the_tools_arguments_and_the_packs_shapes() -> None:
    for kind in KINDS:
        assert PACK_SHAPES in specialist_system(kind, local=Path("/nowhere"))
    assert "view takes only path" in PACK_SHAPES and "it has no box" in PACK_SHAPES
    assert 'each "pages" is a LIST' in PACK_SHAPES and "annotations.json is itself a" in PACK_SHAPES
    view = next(t for t in HELPER_TOOLS if t["name"] == "view")
    assert "It takes no box" in view["description"]
    assert "box" not in view["input_schema"]["properties"]
    assert "never print the set pack's JSON" in full_check_roster.TAG_BRIEF


def test_a_check_with_pages_nobody_opened_never_says_every_item_was_compared() -> None:
    assert not_compared_line({}) == "Every item could be compared."
    assert not_compared_line({}, 13) == "13 pages not opened: their items were not compared."
    line = not_compared_line({"not_proved": 2}, 1)
    assert "1 page not opened." in line and "Every item" not in line


def test_the_same_string_read_twice_on_a_page_is_one_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _modules(monkeypatch, finding_checks=_checks())
    workspace_with_pdf(tmp_path)
    parts, total = ['1 1/2"', '6 5/8"', '4"'], '13"'

    def string(x0: float) -> dict[str, Any]:
        box = {"page": 2, "x0": x0, "y0": 100, "x1": x0 + 20, "y1": 300}
        return {"page": 2, "view": "elevation", "side": "vendor", "parts": parts,
                "printed_total": total, "box": box}  # fmt: skip

    provider = ScriptedProvider(
        lead=[
            reply(
                tool("s1", "record_string", string(60)),
                tool("s2", "record_string", string(660)),
                stop="tool_use",
            ),
            reply(text("The height string does not add up.")),
        ]
    )

    outcome = Harness(tmp_path, provider).run()

    final: dict[str, dict[str, Any]] = {}
    for found in _blocks(outcome, "finding"):
        final[found["id"]] = found
    dropped = {d["finding_id"] for d in _blocks(outcome, "finding_dropped")}
    sums = [
        f for f in final.values() if f["source"] == "vendor_arithmetic" and f["id"] not in dropped
    ]
    assert len(sums) == 1
    assert [a["vendor_box"]["x0"] for a in sums[0]["also_on"]] == [660]
