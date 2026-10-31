"""The page reviewer's separate verifier (`selfcheck_verifier`, plan M7b), with a scripted AI.

Made-up drawings and values only; no network and no paid call. The reviewer's loop runs on the
stand-in engine of `test_page_reviewer` (GV's real zoom, text layer and finding book); after its
review, the verifier's call answers from the same script.

- It is off by default; on, the reviewer's conversation gets no self-check and a new, short
  conversation (the reviewer's system prompt and tools, no tool calls) asks about the red and
  low-confidence drafts, reds first, each with code's close-up and the text printed in its box.
- confirmed keeps a draft as reported, cannot_tell keeps it as a please-check (score at most 7),
  not_shown drops it with the verifier's reason.
- Zero false reds: the verifier never makes or upgrades a red; a red it does not confirm (or that
  it never answered: no money, an unreadable reply) is held back to a please-check.
- Money: its calls stay within the reviewer's cap and the message cap; the log says what it did.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.review.assistant import likely_problems, page_reviewer, proof, selfcheck_verifier
from app.review.assistant.chat_engine import _Reply
from app.review.assistant.chat_request import MODELS
from app.review.assistant.page_reviewer import Draft, Options, PageReview, Reviews
from app.review.assistant.selfcheck_verifier import (
    CANNOT_TELL_MOST,
    FIELD,
    answers_of,
    order,
    score_of,
    settled_score,
)
from tests.review.assistant.test_page_reviewer import (
    _FIXED,
    _PER_TOKEN,
    FILE,
    _Engine,
    _notes,
    _Script,
    _string_issue,
    _support_issue,
    _text,
    _tool,
)


class _Recording(_Engine):
    """The stand-in engine, also keeping whether each call allowed tool calls (`final`)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.finals: list[bool] = []

    def _call(self, **given: Any) -> _Reply:
        self.finals.append(bool(given["final"]))
        return super()._call(**given)


def _run(
    tmp_path: Any,
    turns: Sequence[Any],
    *,
    cap: int = 5_000_000,
    cost: int = 100_000,
    separate: bool = True,
    message_cap: int = 50_000_000,
) -> tuple[_Recording, PageReview, Reviews, Any]:
    engine = _Recording(tmp_path, _Script(turns, cost), cap=message_cap)
    review = PageReview(page=2, file=FILE, options=Options(separate_verifier=separate))
    reviews = Reviews()
    agent = SimpleNamespace(model=MODELS["claude-opus-5-5"], cap_micros=cap, spent=0, last_call=0)
    page_reviewer._Run(engine, agent, review, reviews, {"steps": []}, 0).run()  # type: ignore[arg-type]
    reviews.pages["p2"] = review
    return engine, review, reviews, agent


def _answer(*rows: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [_text(json.dumps({"answers": list(rows)}))]


def _low_support() -> dict[str, Any]:
    return _support_issue(confidence="low")


def _low_scope() -> dict[str, Any]:
    return _support_issue(
        kind="E",
        item="Trash bin: supplier",
        wrong="A trash bin is drawn; it does not say who supplies it.",
        box={"x0": 250, "y0": 460, "x1": 400, "y1": 540},
        confidence="low",
    )


def _medium_label() -> dict[str, Any]:
    return _support_issue(
        kind="F",
        item="Title block: item number",
        wrong="The item number does not match the plan.",
        box={"x0": 1080, "y0": 740, "x1": 1180, "y1": 780},
        confidence="medium",
    )


def _ours(engine: _Engine) -> list[dict[str, Any]]:
    return [f for f in engine.book.live() if f["created_by"] == "page_reviewer"]


# ---- pure code -----------------------------------------------------------------------------------


def test_it_is_off_by_default() -> None:
    assert Options().separate_verifier is False
    assert Settings.model_fields["assistant_separate_verifier"].default is False


def test_a_cannot_tell_score_is_at_most_seven_and_scores_are_whole_0_to_10() -> None:
    assert settled_score("cannot_tell", 9) == CANNOT_TELL_MOST == 7
    assert settled_score("confirmed", 9) == 9
    assert settled_score("confirmed", 12) == 10 and settled_score("not_shown", -3) == 0
    assert settled_score("confirmed", 7.6) == 8
    assert settled_score("confirmed", True) is None and settled_score("confirmed", "8") is None


def test_only_usable_answers_about_the_drafts_asked_are_read() -> None:
    text = json.dumps(
        {
            "answers": [
                {"n": 1, "verdict": "confirmed", "score": 8, "why": "  shown  "},
                {"n": 1, "verdict": "not_shown", "score": 0, "why": "a second answer"},
                {"n": 2, "verdict": "probably", "score": 5, "why": "not a verdict"},
                {"n": 9, "verdict": "confirmed", "score": 9, "why": "never asked"},
                {"n": 3, "verdict": "cannot_tell", "why": "no score"},
            ]
        }
    )
    got = answers_of("Here it is:\n```json\n" + text + "\n```", [1, 2, 3])
    assert sorted(got) == [1, 3]
    assert got[1].verdict == "confirmed" and got[1].score == 8 and got[1].why == "shown"
    assert got[3].score is None
    assert answers_of("I think they are all fine.", [1]) == {}
    assert answers_of(json.dumps({"answers": "all fine"}), [1]) == {}


def test_reds_come_first_then_low_confidence() -> None:
    def draft(n: int, claim: str, confidence: str) -> Draft:
        box = (0.0, 0.0, 10.0, 10.0)
        return Draft(n, "A", claim, box, "item", "wrong", None, None, confidence, None)

    drafts = [draft(1, "please_check", "low"), draft(2, "wrong", "high"), draft(3, "wrong", "low")]
    assert [d.n for d in order(drafts)] == [3, 2, 1]


def test_a_score_is_read_back_only_as_a_whole_number() -> None:
    assert score_of({FIELD: {"verdict": "confirmed", "score": 8}}) == 8
    assert score_of({FIELD: {"verdict": "confirmed", "score": None}}) is None
    assert score_of({}) is None


# ---- in the reviewer's run ---------------------------------------------------------------------------


def test_the_verifier_is_a_separate_short_conversation_that_settles_each_draft(
    tmp_path: Any,
) -> None:
    def verifier(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return _answer(
            {"n": 1, "verdict": "confirmed", "score": 9, "why": "No leg under the overhang."},
            {"n": 2, "verdict": "not_shown", "score": 1, "why": "The note says BIN BY OWNER."},
        )

    engine, review, reviews, agent = _run(
        tmp_path,
        [[_low_support(), _low_scope(), _medium_label()], [_text("Done.")], verifier],
    )
    calls = engine.script.calls
    assert len(calls) == 3
    # The reviewer's own conversation never got a self-check.
    assert not any("Self-check" in n for c in calls for n in _notes(c.messages))
    assert review.counts.get("resolve_finding") is None
    # The verifier's call: a new conversation of one message, on the reviewer's cached prefix
    # (its system prompt and tools), with no tool calls allowed.
    check = calls[2]
    assert len(check.messages) == 1 and check.messages[0]["role"] == "user"
    assert check.system == calls[0].system and check.tools == calls[0].tools
    assert engine.finals == [False, False, True]
    words = "\n".join(b["text"] for b in check.messages[0]["content"] if b.get("type") == "text")
    assert "does the drawing show this problem?" in words
    assert "#1 (construction) Island top: overhang support" in words
    assert '"ST-01"' in words  # the text printed inside the first draft's box
    assert "#3" not in words  # a medium please-check is kept as reported, never asked
    assert "low" not in words.split("Issues:")[1]  # its reviewer's doubt is not shown
    labels = [b["text"] for b in check.messages[0]["content"] if b.get("type") == "text"]
    assert sum(1 for t in labels if t.startswith("Crop") and "issue #" in t) == 2
    assert sum(1 for b in check.messages[0]["content"] if b.get("type") == "image") == 2

    assert [d.status for d in review.drafts] == ["kept", "dropped", "kept_unreflected"]
    kept = {f["item"]: f for f in _ours(engine)}
    assert sorted(kept) == ["Island top: overhang support", "Title block: item number"]
    support = kept["Island top: overhang support"]
    assert support[FIELD] == {
        "verdict": "confirmed",
        "score": 9,
        "why": "No leg under the overhang.",
    }
    assert score_of(support) == 9 and FIELD not in kept["Title block: item number"]
    # A please-check the verifier confirmed is still a please-check, never red.
    assert support["status"] == "check"
    assert support[likely_problems.FIELD]["proof"] == "likely_only"
    assert proof.proof_group(support) == "likely_problem"
    assert len(engine.dropped) == 1

    (row,) = reviews.block()["pages"]
    assert row["verifier_calls"] == 1 and row["verifier_spent_usd"] == 0.1
    assert (row["verifier_confirmed"], row["verifier_not_shown"], row["verifier_cannot_tell"]) == (
        1,
        1,
        0,
    )
    assert row["verifier_dropped"] == [
        {"n": 2, "item": "Trash bin: supplier", "why": "The note says BIN BY OWNER."}
    ]
    assert row["kept"] == 2 and row["dropped"] == 1
    assert agent.spent <= 5_000_000


def test_a_cannot_tell_keeps_a_please_check_with_its_score_at_most_seven(tmp_path: Any) -> None:
    engine, review, *_ = _run(
        tmp_path,
        [
            [_low_support()],
            [_text("Done.")],
            _answer(
                {"n": 1, "verdict": "cannot_tell", "score": 9, "why": "Leg may be in section."}
            ),
        ],
    )
    assert [d.status for d in review.drafts] == ["kept"]
    (found,) = _ours(engine)
    assert found[FIELD]["verdict"] == "cannot_tell" and score_of(found) == 7
    assert found["status"] == "check" and proof.proof_group(found) == "likely_problem"


def test_a_confirmed_red_stays_a_red_candidate_exactly_as_code_recorded_it(tmp_path: Any) -> None:
    """The verifier adds its answer and nothing else: the string still needs code's proof."""
    engine, review, *_ = _run(
        tmp_path,
        [
            [_string_issue()],
            [_text("Done.")],
            _answer({"n": 1, "verdict": "confirmed", "score": 10, "why": "Parts add to 35 3/4."}),
        ],
    )
    (found,) = _ours(engine)
    first = next(f for f in engine.shown if f["id"] == found["id"])
    assert {k: v for k, v in found.items() if k != FIELD} == {
        k: v for k, v in first.items() if k != FIELD
    }
    assert found["source"] == "vendor_arithmetic" and found["status"] == "different"
    assert found[FIELD]["verdict"] == "confirmed"
    assert not proof.code_proved(found)  # never more than before
    assert [d.status for d in review.drafts] == ["kept"] and review.held_back == 0


@pytest.mark.parametrize("verdict", ["cannot_tell", None])
def test_a_red_the_verifier_does_not_confirm_is_held_back_to_a_please_check(
    tmp_path: Any, verdict: str | None
) -> None:
    """cannot_tell, or a draft the reply left out: the red candidate is taken away and a
    please-check (never red) put in its place, its words saying so."""
    rows = [{"n": 1, "verdict": verdict, "score": 6, "why": "The total is half hidden."}]
    engine, review, reviews, _agent = _run(
        tmp_path,
        [[_string_issue()], [_text("Done.")], _answer(*(rows if verdict else []))],
    )
    (found,) = _ours(engine)
    assert found["status"] == "check" and found.get("source") != "vendor_arithmetic"
    assert found[likely_problems.FIELD]["proof"] == "likely_only"
    assert proof.proof_group(found) == "likely_problem"
    assert selfcheck_verifier.HELD_NOTE in found[likely_problems.FIELD]["words"]
    assert len(engine.dropped) == 1  # the red candidate
    (draft,) = review.drafts
    assert draft.status == "held_back" and draft.claim == "please_check"
    (row,) = reviews.block()["pages"]
    assert row["verifier_held_back"] == 1 and row["kept"] == 1
    if verdict:
        assert found[FIELD] == {"verdict": "cannot_tell", "score": 6, "why": rows[0]["why"]}
    else:
        assert FIELD not in found and row["verifier_unanswered"] == 1
        assert row["verifier_skipped"] == "unreadable"


def test_a_not_shown_red_is_dropped_with_its_reason(tmp_path: Any) -> None:
    engine, review, reviews, _agent = _run(
        tmp_path,
        [
            [_string_issue()],
            [_text("Done.")],
            _answer({"n": 1, "verdict": "not_shown", "score": 0, "why": "The overall is 35 3/4."}),
        ],
    )
    assert _ours(engine) == [] and [d.status for d in review.drafts] == ["dropped"]
    (row,) = reviews.block()["pages"]
    assert row["verifier_dropped"][0]["why"] == "The overall is 35 3/4."


def test_no_money_for_the_verifier_holds_reds_back_and_keeps_please_checks(tmp_path: Any) -> None:
    """Cap $1, each reviewer call $0.46: after two calls $0.07 is left, under the verifier's least
    ($0.01 + 4,000 tokens at 20 micros = $0.09). No call; the red is held back."""
    engine, review, reviews, agent = _run(
        tmp_path,
        [[_string_issue(), _low_support()], [_text("Done.")], [_text("never asked")]],
        cap=1_000_000,
        cost=460_000,
    )
    assert len(engine.script.calls) == 2 and agent.spent == 920_000
    assert [d.status for d in review.drafts] == ["held_back", "kept_unreflected"]
    assert all(f["status"] == "check" for f in _ours(engine))
    assert not any(f.get("source") == "vendor_arithmetic" for f in _ours(engine))
    (row,) = reviews.block()["pages"]
    assert row["verifier_calls"] == 0 and row["verifier_skipped"] == "money"
    assert row["verifier_unanswered"] == 2


def test_the_verifier_call_stays_within_the_reviewers_cap_and_the_message_cap(
    tmp_path: Any,
) -> None:
    """Cap $1, reviewer calls $0.40: $0.20 is left, room for ($0.20 - $0.01) / 20 = 9,500 tokens,
    under the verifier's 16,000 most; its worst case fits the cap. With the message's own cap
    lower, that is the bound."""
    verified = _answer({"n": 1, "verdict": "confirmed", "score": 8, "why": "Shown."})
    turns = [[_low_support()], [_text("Done.")], verified]
    engine, *_ = _run(tmp_path / "page", turns, cap=1_000_000, cost=400_000)
    last = engine.script.calls[-1]
    assert engine.finals[-1] is True
    assert last.max_tokens == (1_000_000 - 800_000 - _FIXED) // _PER_TOKEN == 9_500
    assert 800_000 + _FIXED + last.max_tokens * _PER_TOKEN <= 1_000_000

    message_cap = 800_000 + _FIXED + 5_000 * _PER_TOKEN
    engine, *_ = _run(
        tmp_path / "message", turns, cap=1_000_000, cost=400_000, message_cap=message_cap
    )
    assert engine.finals[-1] is True and engine.script.calls[-1].max_tokens == 5_000


def test_with_the_verifier_off_the_self_check_runs_in_the_conversation_as_before(
    tmp_path: Any,
) -> None:
    def settle(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        assert any("Self-check" in n for n in _notes(messages))
        return [_tool("resolve_finding", n=1, decision="keep", why="seen")]

    engine, review, reviews, _agent = _run(
        tmp_path,
        [[_low_support()], [_text("Done.")], settle, [_text("Settled.")]],
        separate=False,
    )
    assert engine.finals == [False, False, False, False]
    assert [d.status for d in review.drafts] == ["kept"] and review.verifier_calls == 0
    (row,) = reviews.block()["pages"]
    assert row["verifier_calls"] == 0 and row["verifier_confirmed"] == 0
