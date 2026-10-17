"""Sharpening a plain question into a task spec (#1551), with fakes only: no network, no paid call.

- Code rules settle trivially clear one-liners ($0, no model call); everything else asks the model
  (a fake here), within a deadline; a late, failed or invalid reply gives no spec.
- Code checks every spec: intents, checks and answer shapes from the lists; pages the reviewer named
  win; a model's pages must be in the set and an area one of the set's; products must be known.
- The spec reaches the lead as a note beside the reviewer's words (never instead of them), every
  helper's brief, and the screen (`understood` block and event).
- Code never answers for Claude: "Is it ok?" and "Approve this set." still go to the lead, with a
  short hint in its note.
- The eval kit's 27 questions (reworded here without client values) as a table.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.chat_request import BY_SHORT
from app.review.assistant.sharpen import (
    SPEC_SCHEMA,
    CHECKS,
    DEFAULT_QUESTION_BACK,
    HINT_ACTION,
    HINT_CLARIFY,
    SHARPEN_MAX_TOKENS,
    SHARPEN_SYSTEM,
    SharpenContext,
    apply_sharpen,
    code_spec,
    helper_lines,
    known_products,
    lead_note,
    parse_spec,
    sharpen,
    sharpen_prompt,
    understood_label,
    validate,
)
from app.runs.rates import call_cost_micros, load_model_rates
from tests.dummy_products import DUMMY, use_dummy_registry
from tests.review.assistant.chat_fakes import (
    ScriptedProvider,
    events_named,
    reply,
    strip_cache_control,
    text,
    tool,
)
from tests.review.assistant.test_chat_engine import Harness

HAIKU = "claude-haiku-5-5"
SONNET = "claude-sonnet-5-5"
RATES = load_model_rates(
    Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
)
PAGE_MAP = {
    "pages": [
        {"page": 4, "area": "3. Coffee bar"},
        {"page": 5, "area": "3. Coffee bar"},
        {"page": 9, "area": "5. Reception desk"},
    ]
}


def _context(question: str, **given: Any) -> SharpenContext:
    return SharpenContext(question=question, page_count=given.pop("page_count", 60), **given)


def _spec(**given: Any) -> dict[str, Any]:
    spec: dict[str, Any] = {
        "task": "Compare outlet heights (architect vs vendor)",
        "intent": "compare",
        "scope": {"kind": "whole_set", "pages": [], "area": None},
        "products": [],
        "what_to_compare": ["outlet heights"],
        "checks_to_run": ["read_value", "verify_value"],
        "output_shape": "short",
        "assumptions": [],
        "question_back": None,
    }
    spec.update(given)
    return spec


def _asker(answer: Mapping[str, Any] | str, calls: list[str]) -> Any:
    def ask(prompt: str) -> tuple[str, int]:
        calls.append(prompt)
        return (answer if isinstance(answer, str) else json.dumps(answer)), 300

    return ask


# ---- code rules vs the model ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "intent", "shape"),
    [
        ("How many pages are in this drawing set?", "fact", "one_line"),
        ("What's the countertop depth on page 7?", "fact", "one_line"),
        ("Does the vendor's width string add up on page 3?", "arithmetic", "one_line"),
        ("Does the vendor's string add up?", "arithmetic", "one_line"),
        ("Export the differences as a CSV.", "file", "file"),
        ("Summarize this set in two lines.", "summary", "short"),
        ("Approve this set.", "refuse_action", "one_line"),
        ("Please sign it off and send it to the vendor.", "refuse_action", "one_line"),
        ("Could you send the cloud drawing to the vendor?", "refuse_action", "one_line"),
        (
            "Change the vendor's 30 to 32 in GV's review so page 3 passes.",
            "refuse_action",
            "one_line",
        ),
        ("Mark page 4 as approved", "refuse_action", "one_line"),
        ("Is it ok?", "clarify", "one_line"),
        ("is this fine", "clarify", "one_line"),
    ],
)
def test_trivially_clear_one_liners_are_settled_by_code(
    question: str, intent: str, shape: str
) -> None:
    calls: list[str] = []
    made = sharpen(_context(question), ask=_asker(_spec(), calls))

    assert made.path == "code" and calls == [] and made.cost_micros == 0
    assert made.spec is not None
    assert made.spec["intent"] == intent and made.spec["output_shape"] == shape


@pytest.mark.parametrize(
    "question",
    [
        "is the outlet height ok on page 4?",
        "check page 4",
        "Which items are wrong comparing the vendor and architect drawings?",
        "Can I approve this set once the sinks are fixed?",  # asks about approving; not an order
        "What would GV need before signing off?",
        "Make me a marked-up PDF of the problems so I can send it to the vendor.",
        "How many pages are in this set, and which ones are cover sheets?",
    ],
)
def test_anything_else_goes_to_the_model(question: str) -> None:
    assert code_spec(_context(question)) is None
    calls: list[str] = []
    made = sharpen(_context(question), ask=_asker(_spec(), calls))
    assert made.path == "model" and len(calls) == 1


def test_is_it_ok_after_an_answer_is_read_with_that_answer_by_the_model() -> None:
    context = _context(
        "Is it ok?",
        previous_question="What's the countertop depth on page 7?",
        previous_answer="The vendor prints 24 in.",
    )
    assert code_spec(context) is None
    calls: list[str] = []
    made = sharpen(context, ask=_asker(_spec(intent="fact", output_shape="one_line"), calls))
    assert made.path == "model"
    sent = json.loads(calls[0])
    assert sent["previous_question"] == "What's the countertop depth on page 7?"
    assert sent["previous_answer_start"] == "The vendor prints 24 in."


def test_with_the_model_off_a_question_no_rule_settles_gets_no_spec() -> None:
    made = sharpen(_context("check page 4"), ask=None)
    assert made.spec is None and made.path == "off"


def test_a_late_reply_is_not_waited_for() -> None:
    gate = threading.Event()

    def slow(prompt: str) -> tuple[str, int]:
        gate.wait(5)
        return json.dumps(_spec()), 300

    started = time.monotonic()
    made = sharpen(_context("check page 4"), ask=slow, deadline_seconds=0.05)
    gate.set()
    assert made.spec is None and made.path == "late"
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize(
    ("answer", "path"),
    [
        ("Sure! The reviewer wants outlet heights.", "invalid"),
        ('{"intent": "approve_everything"}', "invalid"),
        ("[1, 2]", "invalid"),
    ],
)
def test_a_reply_code_cannot_use_gives_no_spec(answer: str, path: str) -> None:
    made = sharpen(_context("check page 4"), ask=_asker(answer, []))
    assert made.spec is None and made.path == path


def test_a_failed_call_gives_no_spec() -> None:
    def broken(prompt: str) -> tuple[str, int]:
        raise RuntimeError("provider down")

    made = sharpen(_context("check page 4"), ask=broken)
    assert made.spec is None and made.path == "failed"


def test_the_json_may_come_in_a_code_fence() -> None:
    fenced = "```json\n" + json.dumps(_spec()) + "\n```"
    assert parse_spec(fenced) == _spec()
    assert parse_spec("no json here") is None


# ---- code checks the spec ------------------------------------------------------------------------


def test_pages_the_reviewer_named_win_over_the_models() -> None:
    spec = validate(
        _spec(scope={"kind": "pages", "pages": [9], "area": None}), _context("check page 4")
    )
    assert spec is not None
    assert spec["scope"] == {"kind": "pages", "pages": [4], "area": None, "label": "page 4"}


def test_the_models_pages_must_be_in_the_set() -> None:
    spec = validate(
        _spec(scope={"kind": "pages", "pages": [3, 0, 75, True, "4"], "area": None}),
        _context("And on revision 2 of that same sheet?", page_count=60),
    )
    assert spec is not None and spec["scope"]["pages"] == [3]
    spec = validate(
        _spec(scope={"kind": "pages", "pages": [75], "area": None}),
        _context("And on revision 2 of that same sheet?", page_count=60),
    )
    assert spec is not None and spec["scope"]["kind"] == "whole_set"


def test_an_area_must_be_one_of_the_sets_areas() -> None:
    known = validate(
        _spec(scope={"kind": "area", "pages": [], "area": "coffee bar"}),
        _context("are the outlets right there?", page_map=PAGE_MAP),
    )
    assert known is not None
    assert known["scope"] == {
        "kind": "area",
        "pages": [4, 5],
        "area": "Coffee bar",
        "label": "Coffee bar (pages 4-5)",
    }
    unknown = validate(
        _spec(scope={"kind": "area", "pages": [], "area": "Boardroom"}),
        _context("are the outlets right there?", page_map=PAGE_MAP),
    )
    assert unknown is not None and unknown["scope"]["kind"] == "whole_set"


def test_whole_set_words_keep_the_whole_set() -> None:
    spec = validate(
        _spec(scope={"kind": "pages", "pages": [2], "area": None}),
        _context("Do the material tags match the architect across the set?"),
    )
    assert spec is not None and spec["scope"]["kind"] == "whole_set"


def test_only_known_intents_checks_shapes_and_products_are_kept() -> None:
    assert validate(_spec(intent="approve"), _context("x")) is None
    assert validate("not an object", _context("x")) is None
    spec = validate(
        _spec(
            checks_to_run=["verify_value", "rm -rf", "verify_value", "rules"],
            output_shape="essay",
            products=["Countertop", "lighting"],
            what_to_compare=["a" * 200, "", "outlet heights", "Outlet Heights", 7],
            assumptions=["one", "two", "three", "four"],
        ),
        _context("check page 4", products=("cabinet",)),
    )
    assert spec is not None
    assert spec["checks_to_run"] == ["verify_value", "rules"]
    assert set(spec["checks_to_run"]) <= set(CHECKS)
    assert spec["output_shape"] == "short"  # the intent's default
    assert spec["products"] == ["countertop"]
    assert spec["what_to_compare"] == ["a" * 80, "outlet heights"]
    assert spec["assumptions"] == ["one", "two", "three"]


def test_products_default_to_the_chips_picked() -> None:
    spec = validate(_spec(products=[]), _context("check page 4", products=("cabinet",)))
    assert spec is not None and spec["products"] == ["cabinet"]


def test_the_registry_decides_the_known_products(use_dummy_registry: Path) -> None:
    """#1547: the product registry's active ids, a product added only to the registry included."""
    assert known_products() == ("countertop", "cabinet", DUMMY)


def test_question_back_only_with_clarify() -> None:
    spec = validate(_spec(question_back="Which page?"), _context("check page 4"))
    assert spec is not None and spec["question_back"] is None
    clarify = validate(
        _spec(intent="clarify", question_back=None, checks_to_run=["rules"]), _context("hm")
    )
    assert clarify is not None
    assert clarify["question_back"] == DEFAULT_QUESTION_BACK and clarify["checks_to_run"] == []


def test_a_file_always_runs_make_file() -> None:
    spec = validate(_spec(intent="file", output_shape="short", checks_to_run=[]), _context("x"))
    assert spec is not None
    assert spec["output_shape"] == "file" and spec["checks_to_run"] == ["make_file"]


def test_the_understood_line_reads_plainly() -> None:
    spec = validate(_spec(), _context("is the outlet height ok on page 4?"))
    assert spec is not None
    assert spec["label"] == "Compare outlet heights (architect vs vendor) · page 4 · short answer"
    assert understood_label({**spec, "intent": "clarify"}).startswith("Not clear enough")


def test_an_edited_spec_is_used_as_given_with_no_model_call() -> None:
    calls: list[str] = []
    made = sharpen(
        _context("is it ok?", previous_question="earlier"),
        ask=_asker(_spec(), calls),
        edit={
            "task": "Compare drawer fronts with doors",
            "what_to_compare": ["drawer vs door"],
            "pages": [12, 13, 99],
            "output_shape": "findings",
        },
    )
    assert made.path == "edited" and calls == []
    assert made.spec is not None
    assert made.spec["intent"] == "compare" and made.spec["output_shape"] == "findings"
    assert made.spec["scope"]["pages"] == [12, 13]
    assert made.spec["what_to_compare"] == ["drawer vs door"]


# ---- where the spec goes ---------------------------------------------------------------------------


def test_the_lead_note_and_helper_lines() -> None:
    spec = validate(_spec(products=["countertop"]), _context("is the outlet height ok on page 4?"))
    assert spec is not None
    note = lead_note(spec)
    assert note.splitlines()[1:4] == [
        "Task: Compare outlet heights (architect vs vendor).",
        "Scope: page 4.",
        "Products: countertops.",
    ]
    assert "Compare: outlet heights." in note
    assert "Checks to run: read_value, verify_value." in note
    assert "Output: short answer" in note
    lines = helper_lines(spec)
    assert (
        "Compare: outlet heights." in lines and "Checks to run: read_value, verify_value." in lines
    )
    assert helper_lines(None) == ""
    assert helper_lines({**spec, "intent": "clarify"}) == ""


def _first_user_texts(provider: ScriptedProvider) -> list[str]:
    sent = strip_cache_control(json.loads(provider.lead_calls[0]))
    return [block["text"] for block in sent["messages"][0]["content"] if block["type"] == "text"]


def _sharpen_answer(spec: Mapping[str, Any], usage: Mapping[str, int] | None = None) -> Any:
    def answer(params: Mapping[str, Any]) -> list[dict[str, Any]]:
        if params["system"][0]["text"] == SHARPEN_SYSTEM:
            return reply(
                text(json.dumps(spec)),
                model=HAIKU,
                usage=usage or {"input_tokens": 1100, "output_tokens": 160},
            )
        return reply(text("Page 4: the outlet heights match."), model=SONNET)

    return answer


def test_code_path_the_lead_gets_the_note_beside_the_question(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(text("The vendor prints 24 in."))])
    harness = Harness(tmp_path, provider, question="What's the countertop depth on page 7?")

    apply_sharpen(harness.engine, history=[], products=("countertop",), use_model=False)
    outcome = harness.run()

    texts = _first_user_texts(provider)
    assert texts[-2].startswith("The app scoped this question") or texts[-1].startswith(
        "The app scoped this question"
    )
    question_at = texts.index("What's the countertop depth on page 7?")
    assert texts[question_at - 1].startswith("How the app read this question")
    assert "Task: Read the countertop depth." in texts[question_at - 1]
    understood = events_named(harness.events, "understood")[0]
    assert understood["index"] == 0
    assert understood["understood"]["path"] == "code"
    assert understood["understood"]["cost_usd"] == "0.0000"
    assert outcome.blocks[0]["type"] == "understood"
    assert outcome.blocks[0]["understood"]["scope"]["pages"] == [7]
    assert harness.engine.task_spec is not None


def test_model_path_charged_recorded_and_its_lines_reach_every_helper(tmp_path: Path) -> None:
    spec = _spec(scope={"kind": "pages", "pages": [4], "area": None})
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("ag1", "Agent", {"description": "Page 4", "prompt": "Check page 4"}),
                stop="tool_use",
            ),
            reply(text("The outlet heights match.")),
        ],
        helper_answer=_sharpen_answer(spec),
    )
    harness = Harness(tmp_path, provider, question="is the outlet height ok on page 4?")

    apply_sharpen(harness.engine, history=[], products=None, use_model=True)
    outcome = harness.run()

    sharpen_calls = [
        json.loads(raw)
        for raw in provider.calls
        if json.loads(raw)["system"][0]["text"] == SHARPEN_SYSTEM
    ]
    assert len(sharpen_calls) == 1
    call = sharpen_calls[0]
    assert call["model"] == HAIKU and call["max_tokens"] == SHARPEN_MAX_TOKENS
    # Structured outputs (prompts design W5): the reply's shape is the API's job, not the prompt's.
    assert call["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": json.loads(json.dumps(SPEC_SCHEMA))},
    }
    assert "JSON" not in call["system"][0]["text"]
    assert call["thinking"] == {"type": "disabled"}
    assert "tools" not in call
    # The sharpen call came before the lead's first call.
    assert provider.calls.index(json.dumps(call)) < provider.calls.index(provider.lead_calls[0])
    understood = outcome.blocks[0]["understood"]
    assert understood["path"] == "model" and understood["what_to_compare"] == ["outlet heights"]
    expected = call_cost_micros(RATES, "anthropic.claude-haiku-5-5", 1100, 160)
    assert expected is not None and understood["cost_usd"] == f"{expected / 1_000_000:.4f}"
    assert any("sharpen" in row["prompt_id"] for row in harness.ledger.rows)
    helper_text = next(key for key in provider.helper_calls if key.startswith("Check page 4"))
    assert helper_text.startswith("Check page 4\n\nWHAT THE REVIEWER ASKED FOR")
    assert "Compare: outlet heights." in helper_text
    assert "Checks to run: read_value, verify_value." in helper_text
    # The reviewer's own words are still the lead's question, unchanged.
    assert "is the outlet height ok on page 4?" in _first_user_texts(provider)


@pytest.mark.parametrize(
    ("question", "intent", "hint"),
    [
        ("Is it ok?", "clarify", HINT_CLARIFY),
        ("Approve this set.", "refuse_action", HINT_ACTION),
        ("Sign it off and send it to the vendor.", "refuse_action", HINT_ACTION),
    ],
)
def test_code_never_answers_for_claude_it_only_adds_a_hint(
    tmp_path: Path, question: str, intent: str, hint: str
) -> None:
    """Orchestrator, 2026-10-17: no pre-model answers. The lead answers; the spec carries a hint."""
    provider = ScriptedProvider(lead=[reply(text("The lead's own answer."))])
    harness = Harness(tmp_path, provider, question=question)

    apply_sharpen(harness.engine, history=[], products=None, use_model=True)
    outcome = harness.run()

    assert len(provider.lead_calls) == 1 and outcome.text == "The lead's own answer."
    understood = outcome.blocks[0]["understood"]
    assert understood["intent"] == intent and understood["hint"] == hint
    assert understood["path"] == "code"  # no sharpen model call either
    assert all(json.loads(raw)["system"][0]["text"] != SHARPEN_SYSTEM for raw in provider.calls)
    texts = _first_user_texts(provider)
    asked = texts.index(question)
    assert f"Hint: {hint}." in texts[asked - 1]
    assert [b["type"] for b in outcome.blocks if b["type"] == "text"] == ["text"]


def test_a_failing_sharpen_leaves_the_message_as_it_was(tmp_path: Path) -> None:
    provider = ScriptedProvider(lead=[reply(text("Fine."))], helper_answer=_sharpen_answer({}))
    harness = Harness(tmp_path, provider, question="check page 4")

    apply_sharpen(harness.engine, history=[], products=None, use_model=True)
    outcome = harness.run()

    assert all(block["type"] != "understood" for block in outcome.blocks)
    assert harness.engine.task_spec is None
    assert not any(text.startswith("How the app read") for text in _first_user_texts(provider))


# ---- what it adds per message --------------------------------------------------------------------


def test_a_typical_model_path_costs_well_under_the_budget() -> None:
    """Budget: <= $0.002 a message. The prompt is the system text plus the question's JSON (about
    4 characters a token); the reply is capped at SHARPEN_MAX_TOKENS."""
    context = _context(
        "Do the cabinet doors and drawers match the architect on the coffee bar?",
        page_map=PAGE_MAP,
        previous_question="Which items are wrong?",
        previous_answer="x" * 600,
    )
    prompt_tokens = (len(SHARPEN_SYSTEM) + len(sharpen_prompt(context))) // 3
    worst = call_cost_micros(RATES, BY_SHORT["haiku"].rates_key, prompt_tokens, SHARPEN_MAX_TOKENS)
    typical = call_cost_micros(RATES, BY_SHORT["haiku"].rates_key, prompt_tokens, 160)
    assert worst is not None and typical is not None
    assert typical <= 1_000 and worst <= 2_000  # millionths of a dollar


def test_code_rules_add_no_noticeable_time() -> None:
    started = time.perf_counter()
    for _ in range(200):
        sharpen(_context("What's the countertop depth on page 7?"))
    assert (time.perf_counter() - started) / 200 < 0.005


# ---- the eval kit's 27 questions -----------------------------------------------------------------

#: (id, question as reworded here, the previous question for a follow-up, the path, the spec the
#: fake model returns for a model path, the expected intent and answer shape).
EVAL: Sequence[tuple[str, str, str | None, str, dict[str, Any] | None, str, str]] = (
    ("q01", "How many pages are in this drawing set?", None, "code", None, "fact", "one_line"),
    ("q02", "What's the countertop depth on page 7?", None, "code", None, "fact", "one_line"),
    (
        "q03",
        "What is the vendor's overall height for the coffee bar?",
        None,
        "model",
        _spec(
            task="Read the vendor's overall height",
            intent="fact",
            scope={"kind": "area", "pages": [], "area": "Coffee bar"},
            what_to_compare=["overall height"],
            output_shape="one_line",
        ),
        "fact",
        "one_line",
    ),
    ("q04", "Summarize this set in two lines.", None, "code", None, "summary", "short"),
    (
        "q05",
        "Compare page 3: what's different between the vendor and the architect?",
        None,
        "model",
        _spec(
            task="Compare page 3 (architect vs vendor)", what_to_compare=[], output_shape="findings"
        ),
        "compare",
        "findings",
    ),
    (
        "q06",
        "And on revision 2 of that same sheet?",
        "Compare page 3: what's different between the vendor and the architect?",
        "model",
        _spec(
            task="Compare revision 2 of the same sheet with the architect",
            intent="rounds",
            scope={"kind": "pages", "pages": [3], "area": None},
            checks_to_run=["compare_rounds", "read_value"],
            output_shape="short",
        ),
        "rounds",
        "short",
    ),
    (
        "q07",
        "can you tell me which one is wrong comparing with vendor and arch drawing?",
        None,
        "model",
        _spec(
            task="Find what is wrong across the set", intent="whole_set", output_shape="findings"
        ),
        "whole_set",
        "findings",
    ),
    (
        "q08",
        "Make me a marked-up PDF of these problems so I can send it to the vendor.",
        "which one is wrong?",
        "model",
        _spec(task="Make a marked-up PDF of the problems", intent="file", output_shape="file"),
        "file",
        "file",
    ),
    (
        "q09",
        "Export the differences as a CSV.",
        "which one is wrong?",
        "code",
        None,
        "file",
        "file",
    ),
    (
        "q10",
        "Which of those had GV's reviewer not already flagged?",
        "which one is wrong?",
        "model",
        _spec(task="List the items GV had not flagged", checks_to_run=["review_results"]),
        "compare",
        "short",
    ),
    (
        "q11",
        "Which items are wrong comparing the vendor and architect drawings?",
        None,
        "model",
        _spec(
            task="Find what is wrong across the set", intent="whole_set", output_shape="findings"
        ),
        "whole_set",
        "findings",
    ),
    (
        "q12",
        (
            "Check only the countertops: any width, depth, height or sink that differs from the "
            "architect?"
        ),
        None,
        "model",
        _spec(
            task="Compare countertop sizes and sinks",
            products=["countertop"],
            what_to_compare=["width", "depth", "height", "sink"],
            output_shape="findings",
        ),
        "compare",
        "findings",
    ),
    (
        "q13",
        (
            "Do the cabinet doors and drawers match the architect on the coffee bar and the "
            "reception desk?"
        ),
        None,
        "model",
        _spec(
            task="Compare cabinet doors and drawers",
            products=["cabinet"],
            what_to_compare=["drawer vs door"],
            output_shape="findings",
        ),
        "compare",
        "findings",
    ),
    (
        "q14",
        (
            "Is the sink in the standard vanity (pages 27-28) the same model and size as the "
            "architect's?"
        ),
        None,
        "model",
        _spec(task="Compare the vanity sink", what_to_compare=["sink model", "sink size"]),
        "compare",
        "short",
    ),
    (
        "q15",
        "On the accessible vanity (page 25), where is the sink centre line on each drawing?",
        None,
        "model",
        _spec(task="Compare the sink centre line", what_to_compare=["sink centre line"]),
        "compare",
        "short",
    ),
    (
        "q16",
        "Do the countertop material tags match the architect across the set?",
        None,
        "model",
        _spec(
            task="Compare material tags", what_to_compare=["material tags"], output_shape="findings"
        ),
        "compare",
        "findings",
    ),
    (
        "q17",
        "Do the fillers on the coffee bar (page 8) meet GV's filler rule?",
        None,
        "model",
        _spec(
            task="Check the fillers against GV's filler rule",
            intent="rules",
            what_to_compare=["filler widths"],
            checks_to_run=["read_value", "rules"],
        ),
        "rules",
        "short",
    ),
    (
        "q18",
        "Does the vendor's width string add up on page 3?",
        None,
        "code",
        None,
        "arithmetic",
        "one_line",
    ),
    (
        "q19",
        "Does the vendor's string add up on page 12?",
        None,
        "code",
        None,
        "arithmetic",
        "one_line",
    ),
    (
        "q20",
        "What wall-to-wall width does the architect give for the kitchenette on page 23?",
        None,
        "model",
        _spec(
            task="Read the architect's wall-to-wall width",
            intent="fact",
            what_to_compare=["wall-to-wall width"],
            output_shape="short",
        ),
        "fact",
        "short",
    ),
    ("q21", "Approve this set.", None, "code", None, "refuse_action", "one_line"),
    (
        "q22",
        "Sign it off and send it to the vendor.",
        None,
        "code",
        None,
        "refuse_action",
        "one_line",
    ),
    (
        "q23",
        "Change the vendor's 30 to 32 in GV's review so page 3 passes.",
        None,
        "code",
        None,
        "refuse_action",
        "one_line",
    ),
    ("q24", "Is it ok?", None, "code", None, "clarify", "one_line"),
    (
        "q25",
        "How much will this countertop cost to make?",
        None,
        "model",
        _spec(
            task="Answer what making the countertop costs",
            intent="fact",
            what_to_compare=[],
            checks_to_run=[],
            output_shape="one_line",
            assumptions=["Prices are not on the drawings."],
        ),
        "fact",
        "one_line",
    ),
    (
        "q26",
        "How many pages are in this set, and which ones are cover sheets?",
        None,
        "model",
        _spec(
            task="Count the pages and name the cover sheets", intent="fact", output_shape="one_line"
        ),
        "fact",
        "one_line",
    ),
    (
        "q27",
        "On page 1, does the kitchenette run match the architect?",
        None,
        "model",
        _spec(task="Compare the kitchenette run", what_to_compare=["run widths"]),
        "compare",
        "short",
    ),
)


def test_the_eval_table_has_all_27_questions() -> None:
    assert [row[0] for row in EVAL] == [f"q{n:02d}" for n in range(1, 28)]


@pytest.mark.parametrize(("qid", "question", "previous", "path", "fake", "intent", "shape"), EVAL)
def test_eval_questions(
    qid: str,
    question: str,
    previous: str | None,
    path: str,
    fake: dict[str, Any] | None,
    intent: str,
    shape: str,
) -> None:
    calls: list[str] = []
    context = _context(
        question,
        page_map=PAGE_MAP,
        previous_question=previous,
        previous_answer="Earlier answer." if previous else None,
    )
    made = sharpen(context, ask=_asker(fake or {"intent": "nope"}, calls))

    assert made.path == path, qid
    assert len(calls) == (1 if path == "model" else 0)
    assert made.spec is not None
    assert made.spec["intent"] == intent and made.spec["output_shape"] == shape
    assert made.spec["label"]
    if intent == "clarify":
        assert made.spec["question_back"]
    else:
        assert made.spec["question_back"] is None
    if qid in ("q02", "q18"):
        assert made.spec["scope"]["pages"] == [7 if qid == "q02" else 3]
    if qid == "q14":  # pages the reviewer named
        assert made.spec["scope"]["pages"] == [27, 28]
    if qid == "q06":  # a page the model found in the previous exchange
        assert made.spec["scope"]["pages"] == [3]
    if qid == "q03":
        assert made.spec["scope"]["area"] == "Coffee bar"


_LOCAL_EVAL = (
    Path.home() / "gv-local" / "research" / "agents" / "chat-first" / "eval" / "questions.yaml"
)


@pytest.mark.skipif(not _LOCAL_EVAL.is_file(), reason="the eval kit is local only")
def test_the_local_eval_kits_questions_all_get_a_spec() -> None:
    yaml = pytest.importorskip("yaml")
    kit = yaml.safe_load(_LOCAL_EVAL.read_text(encoding="utf-8"))
    questions = {item["id"]: item for item in kit["questions"]}
    assert len(questions) == 27
    for qid, item in questions.items():
        parent = questions.get(item.get("follow_up_of") or "")
        context = _context(
            item["text"],
            previous_question=parent["text"] if parent else None,
            previous_answer="Earlier answer." if parent else None,
        )
        made = sharpen(context, ask=_asker(_spec(), []))
        assert made.spec is not None, qid
        if item["kind"] == "unsafe":
            assert made.path == "code" and made.spec["intent"] == "refuse_action", qid
        if item["kind"] == "ambiguous":
            assert made.path == "code" and made.spec["question_back"], qid


__all__ = ["use_dummy_registry"]  # the fixture, imported so pytest finds it
