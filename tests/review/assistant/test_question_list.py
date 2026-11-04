"""The fixed question list loads, holds together, and refuses a list that does not.

Mirrors `app/review/assistant/question_list.py`:

- every question has a stable key, the question in plain English, the item kinds it is asked about,
  the facts it needs, the rule, what makes an answer a problem and what does not;
- `*` means the question is asked about every item, so an item whose kind is unknown still gets
  those questions and shows as a hole in the coverage ledger;
- `compares` says what the answer is weighed against, stated in the file or worked out from the
  facts the question needs;
- a list that repeats a key, names an item kind the taxonomy does not hold, or declares the wrong
  schema is refused by name rather than half-read.

Also checks the real shipped list: it must be loadable, carry the backbone questions the audits
found on all three jobs, and hold no client, project or drawing names.

Made-up questions and values only.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.review.assistant import question_list as ql

MADE_UP = """
schema: gv-review-questions/1
item_kinds:
  - key: widget
    label: Widget
    what: A made-up thing, for the tests only.
    views:
      plan: the widget seen from above
  - key: gizmo
    label: Gizmo
    what: Another made-up thing.
    views:
      detail: the gizmo drawn large
questions:
  - key: widget_length
    asks: How long is the widget?
    item_kinds: [widget]
    needs:
      - side: vendor
        fact: the printed length
        view: plan
      - side: architect
        fact: the designed length
        view: plan
    rule: The lengths are compared in inches, exactly.
    problem_when: the two lengths differ.
    not_a_problem_when:
      - The two are measured from different ends.
    decided_by: code
    from: true_answers
    seen_as:
      words: ["widget length"]
  - key: anything_labelled
    asks: Is everything on the sheet labelled?
    item_kinds: ["*"]
    needs:
      - side: vendor
        fact: every label
        view: plan
    rule: Every piece carries a label.
    problem_when: a piece has no label.
    decided_by: ai
    from: misses
"""


def _load(tmp_path: Path, text: str, monkeypatch: pytest.MonkeyPatch) -> ql.QuestionList:
    path = tmp_path / "made_up.yaml"
    path.write_text(text, encoding="utf-8")
    monkeypatch.setenv(ql.PATH_ENV, str(path))
    ql.reset()
    return ql.question_list()


@pytest.fixture(autouse=True)
def _forget() -> None:
    ql.reset()
    yield
    ql.reset()


def test_a_made_up_list_loads_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loaded = _load(tmp_path, MADE_UP, monkeypatch)
    assert loaded.kind_keys() == ("widget", "gizmo")
    assert [one.key for one in loaded.asked] == ["widget_length", "anything_labelled"]
    length = loaded.question("widget_length")
    assert length is not None
    assert length.asks == "How long is the widget?"
    assert length.can_go_red
    assert length.sides() == ("vendor", "architect")
    assert length.not_a_problem_when == ("The two are measured from different ends.",)


def test_a_star_question_is_asked_about_every_kind_and_about_an_unknown_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _load(tmp_path, MADE_UP, monkeypatch)
    assert [one.key for one in loaded.for_kind("widget")] == ["widget_length", "anything_labelled"]
    assert [one.key for one in loaded.for_kind("gizmo")] == ["anything_labelled"]
    # An item nobody identified still gets the questions asked about every item.
    assert [one.key for one in loaded.for_kind(None)] == ["anything_labelled"]
    assert [one.key for one in loaded.for_kind("not_a_kind")] == ["anything_labelled"]


def test_compares_is_worked_out_from_the_facts_unless_the_file_says(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loaded = _load(tmp_path, MADE_UP, monkeypatch)
    length = loaded.question("widget_length")
    labelled = loaded.question("anything_labelled")
    assert length is not None and labelled is not None
    assert length.compares == "architect" and length.needs_the_architect
    assert labelled.compares == "vendor_only" and not labelled.needs_the_architect

    said = MADE_UP.replace(
        "    decided_by: code\n", "    decided_by: code\n    compares: gv_rule\n"
    )
    stated = _load(tmp_path, said, monkeypatch)
    stated_length = stated.question("widget_length")
    assert stated_length is not None
    assert stated_length.compares == "gv_rule"


@pytest.mark.parametrize(
    ("broken", "says"),
    [
        (MADE_UP.replace("gv-review-questions/1", "gv-review-questions/0"), "declares"),
        (MADE_UP.replace("  - key: anything_labelled", "  - key: widget_length"), "listed twice"),
        (MADE_UP.replace("item_kinds: [widget]", "item_kinds: [sprocket]"), "not in the taxonomy"),
        (MADE_UP.replace("    decided_by: code", "    decided_by: maybe"), "decided_by"),
        (MADE_UP.replace("      - side: vendor", "      - side: nobody"), "needs.side"),
        (MADE_UP.replace("    from: true_answers", "    from: a hunch"), "from"),
    ],
)
def test_a_list_that_does_not_hold_together_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, broken: str, says: str
) -> None:
    with pytest.raises(ql.QuestionsError) as raised:
        _load(tmp_path, broken, monkeypatch)
    assert says in str(raised.value)


def test_the_shipped_list_loads_and_every_question_is_whole() -> None:
    loaded = ql.question_list()
    assert len(loaded.kinds) >= 10
    assert len(loaded.asked) >= 50
    for question in loaded.asked:
        assert question.asks.endswith("?"), question.key
        assert question.needs, question.key
        assert question.rule and question.problem_when, question.key
        assert question.decided_by in ql.DECIDED_BY
        assert question.compares in ql.COMPARES
        assert question.came_from in ql.FROM
        for kind in question.item_kinds:
            assert kind == ql.EVERY_KIND or loaded.kind(kind) is not None, question.key


def test_the_shipped_list_carries_the_backbone_questions() -> None:
    """The three questions that found real problems on all three audited jobs."""
    for key in ("string_adds_up", "sink_cutout_size", "ct_overall_length"):
        backbone = ql.question(key)
        assert backbone is not None, key
        assert backbone.can_go_red, key
        # None of the three needs an architect drawing to answer.
        assert backbone.compares in ("vendor_only", "gv_rule"), key
    assert ql.question("string_adds_up").every_kind  # type: ignore[union-attr]


def test_every_item_kind_is_asked_at_least_the_shared_questions() -> None:
    loaded = ql.question_list()
    shared = {one.key for one in loaded.for_kind(None)}
    assert len(shared) >= 5
    for kind in loaded.kind_keys():
        asked = {one.key for one in loaded.for_kind(kind)}
        assert shared <= asked, kind


def test_the_shipped_list_names_no_client_project_or_drawing() -> None:
    """The list is generic millwork knowledge: that is what makes it vendor-agnostic."""
    whole = (Path(ql.__file__).resolve().parents[3] / "vocabulary" / ql.FILE).read_text("utf-8")
    # GV's own published rulebook ids are the one place a code is allowed.
    text = "\n".join(line for line in whole.splitlines() if "rule_ids:" not in line)
    # A drawing number looks like two or more capitals, a dash and digits.
    assert not re.search(r"\b[A-Z]{2,}-\d{2,}\b", text)
    # A capitalised word in the middle of a sentence would be a project, a brand or a person.
    allowed = {"GV", "ADA", "AI", "A", "English"}
    found = {one for one in re.findall(r"[a-z,;)]\s+([A-Z][A-Za-z]+)", text)} - allowed
    assert not found, sorted(found)


def test_code_and_vendor_only_helpers_pick_the_right_questions() -> None:
    assert all(one.can_go_red for one in ql.code_questions())
    assert all(one.compares == "vendor_only" for one in ql.vendor_only_questions())
    assert ql.question("fronts_and_bays") not in ql.code_questions()
