"""The depth reader's measuring script (#1265): it refuses to pay without a cap, never writes into
a repository, reuses stored answers instead of paying twice, and writes Pariksha's answers so that a
value code held is a hold. No model, no network; made-up values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.depth_read as script
from eval.pariksha.depth import load_depth_answers
from extraction.depth.output import ViewKindAnswer
from extraction.depth.prompts import (
    VIEW_KIND_PROMPT,
    VIEW_KIND_PROMPT_ID,
    VIEW_KIND_SCHEMA,
    DepthQuestion,
)
from extraction.depth.read import CLAUDE_PAIR, CT007, CT010, Asked, ReaderSays, ValueReading
from extraction.form_reader.bedrock import AttemptUsage
from tests.extraction.slot_reader.test_bedrock import PNG

OPUS, SONNET = CLAUDE_PAIR
SHA = "b" * 64


def test_a_paid_run_needs_a_cap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    out = tmp_path / "out"
    code = script.main(["--pdf", "x.pdf", "--out", str(out), "--allow-paid"])
    assert code == script.REFUSED
    assert "cap" in capsys.readouterr().err


def test_a_cap_without_allow_paid_is_refused(tmp_path: Path) -> None:
    code = script.main(["--pdf", "x.pdf", "--out", str(tmp_path / "o"), "--cap-usd", "4"])
    assert code == script.REFUSED


def test_the_output_never_goes_into_the_repository() -> None:
    code = script.main(["--pdf", "x.pdf", "--out", str(script.REPO / "out-here")])
    assert code == script.REFUSED


def _value(usable: bool, texts: tuple[str, str], *, field: str = CT010) -> ValueReading:
    edge = "cabinet_face" if field == CT007 else None
    readers = tuple(
        ReaderSays(model, "value", text, start_edge=edge)
        for model, text in zip(CLAUDE_PAIR, texts, strict=True)
    )
    return ValueReading(
        field,
        3,
        readers,
        usable,
        None,
        texts[0],
        "made-up reason: code holds it" if not usable else "ok",
        start_edge=edge,
    )


def test_two_ais_agreeing_where_code_held_is_a_hold_in_the_final_answers() -> None:
    ai, final = script.value_entries(_value(False, ('24"', '24"')))
    assert [entry["value"] for entry in ai] == ['24"', '24"']
    assert [entry["held"] for entry in final] == ["made-up reason: code holds it"] * 2


def test_a_usable_value_and_a_disagreement_are_written_as_they_are() -> None:
    _ai, final = script.value_entries(_value(True, ('4"', '4"'), field=CT007))
    assert final[0] == {"model_id": OPUS, "value": '4"', "start_edge": "cabinet_face"}
    ai, final = script.value_entries(_value(False, ('24"', '25"')))
    assert ai == final


def test_answers_files_load_in_the_scorer() -> None:
    _ai, final = script.value_entries(_value(False, ('24"', '24"')))
    document = {
        "format": script.ANSWERS_FORMAT,
        "agent": script.DEPTH_AGENT,
        "run": "made up",
        "prompt_ids": [],
        "answers": [
            {"page_sha256": SHA, "question_id": "p1:plan:CT010:x", "field": CT010, "readers": final}
        ],
    }
    loaded = load_depth_answers(document)
    assert {reading.says for reading in loaded.answers[(SHA, "p1:plan:CT010:x", CT010)]} == {"held"}


def _asked(key: str) -> Asked:
    from extraction.depth.read import PreparedPage, question_packet

    question = DepthQuestion(VIEW_KIND_PROMPT_ID, (PNG,), VIEW_KIND_PROMPT, VIEW_KIND_SCHEMA)
    page = PreparedPage(0, SHA, "doc", None, lambda box: (0, 0, 0, 0), (), None)  # type: ignore[arg-type]
    return Asked(key, OPUS, 0, question, question_packet(page, key, question, "high"))


def test_no_paid_call_is_sent_without_allow_paid(tmp_path: Path) -> None:
    ledger = script.Ledger(tmp_path / "calls.jsonl", None)
    ask = script.make_asker(ledger, allow_paid=False, paid=None, max_tokens=100)
    answers = ask([_asked("v1:kind")])
    (answer,) = answers.values()
    assert "not asked" in answer.reason
    assert ask.not_sent == [1]


def test_a_stored_answer_is_reused_and_costs_nothing(tmp_path: Path) -> None:
    asked = _asked("v1:kind")
    ledger_path = tmp_path / "calls.jsonl"
    first = script.Ledger(ledger_path, None)
    first.record(
        AttemptUsage(
            OPUS,
            VIEW_KIND_PROMPT_ID,
            VIEW_KIND_PROMPT_ID,
            100,
            10,
            5,
            False,
            raw_response_text=json.dumps({"kind": "plan", "why": "made up"}),
            question_packet=asked.packet,
        )
    )
    ledger = script.Ledger(ledger_path, None)
    ask = script.make_asker(ledger, allow_paid=False, paid=None, max_tokens=100)
    answers = ask([asked])
    assert answers[("v1:kind", OPUS)] == ViewKindAnswer(OPUS, "plan", "made up")
    assert ledger.reused == 1 and ledger.calls == 0 and ask.not_sent == [0]


# --- review #1274 ---------------------------------------------------------------------------


def test_the_output_stays_under_gv_local(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = script.main(["--pdf", "x.pdf", "--out", str(tmp_path / "elsewhere")])
    assert code == script.REFUSED
    assert "gv-local" in capsys.readouterr().err


def test_a_paid_run_needs_the_switch_on(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GV_DEPTH_READER", raising=False)
    code = script.main(
        ["--pdf", "x.pdf", "--out", str(tmp_path / "o"), "--allow-paid", "--cap-usd", "1"]
    )
    assert code == script.REFUSED
    assert "GV_DEPTH_READER" in capsys.readouterr().err


def test_the_fraction_bar_lengths_are_read_and_never_defaulted(tmp_path: Path) -> None:
    geometry = script.fraction_bar_settings(script.DEFAULT_READER_SETTINGS)
    assert geometry.bar_thickness_max_pt > 0
    partial = tmp_path / "settings.sh"
    partial.write_text("GV_READER_FRACTION_BAR_THICKNESS_MAX_PT=0.3 \\\n")
    with pytest.raises(ValueError):
        script.fraction_bar_settings(partial)


def test_a_call_refused_by_the_cap_is_not_counted_as_paid(tmp_path: Path) -> None:
    ledger = script.Ledger(tmp_path / "calls.jsonl", None)
    ledger.record(
        AttemptUsage(
            OPUS, VIEW_KIND_PROMPT_ID, VIEW_KIND_PROMPT_ID, None, None, 0, False, "SpendCapExceeded"
        )
    )
    assert (ledger.calls, ledger.refused) == (0, 1)
    ledger.record(AttemptUsage(OPUS, VIEW_KIND_PROMPT_ID, VIEW_KIND_PROMPT_ID, 10, 5, 3, False))
    assert (ledger.calls, ledger.refused) == (1, 1)


def _key(agent: str, cases: list[dict[str, object]]) -> dict[str, object]:
    return {"format": "pariksha-key/1", "agent": agent, "cases": cases}


def test_a_keyed_value_with_no_view_found_is_written_as_a_hold() -> None:
    from extraction.depth.read import DepthReading

    other = "c" * 64
    cases: list[dict[str, object]] = [
        {
            "page_sha256": SHA,
            "question_id": "p0:plan:CT010:x",
            "field": CT010,
            "view": "plan",
            "statement_id": 1,
            "set": "s",
            "page": 1,
        },
        {
            "page_sha256": other,
            "question_id": "p0:plan:CT010:y",
            "field": CT010,
            "view": "plan",
            "statement_id": 2,
            "set": "s",
            "page": 1,
        },
    ]
    files, missed = script.answers_for_keys(
        DepthReading(views=(), questions_asked=0),
        [_key(script.DEPTH_AGENT, cases)],
        "made up",
        {SHA},
    )
    final = files[f"{script.DEPTH_AGENT}-final"]["answers"]
    # The page that was read but had no view is a hold; the page not in this run is left out.
    assert [entry["question_id"] for entry in final] == ["p0:plan:CT010:x"]
    assert all("no view found" in reader["held"] for reader in final[0]["readers"])
    assert len(missed) == 1
    loaded = load_depth_answers(files[f"{script.DEPTH_AGENT}-final"])
    assert {r.says for r in loaded.answers[(SHA, "p0:plan:CT010:x", CT010)]} == {"held"}


def _view_reading(number: int, heading: str, extent: tuple[int, int, int, int]) -> object:
    from decimal import Decimal

    from extraction.depth.read import ViewReading
    from extraction.depth.view_kind import code_view_kind, kind_from_heading
    from extraction.geometry.rows import Box

    return ViewReading(
        page_index=0,
        page_sha256=SHA,
        panel=1,
        view_number=number,
        extent=Box(*(Decimal(value) for value in extent)),
        heading=heading,
        code_kind=code_view_kind(kind_from_heading(heading), []),
        kinds=(),
        kind=None,
        kind_reason="",
        values=(),
        values_reason="",
    )


def test_a_key_place_matches_only_one_view_with_its_caption() -> None:
    front = _view_reading(1, "FRONT VIEW", (16, 213, 552, 592))
    plan = _view_reading(2, "TOP PLAN", (147, 518, 473, 527))
    views = [front, plan]
    # The key's plan lies inside the front view's (too large) extent: the captions differ, so it
    # is never scored against the front view.
    found, why = script.match_view(views, [355, 405, 550, 535], "TOP PLAN")  # type: ignore[arg-type]
    assert found is None and "captioned 'FRONT VIEW'" in why
    found, _why = script.match_view(views, [30, 230, 500, 500], "FRONT VIEW")  # type: ignore[arg-type]
    assert found is front
    found, why = script.match_view(views, [600, 600, 700, 700], "FRONT VIEW")  # type: ignore[arg-type]
    assert found is None and "0 reader views" in why
    overlapping = [front, _view_reading(3, "FRONT VIEW", (16, 213, 552, 592))]
    found, why = script.match_view(overlapping, [30, 230, 500, 500], None)  # type: ignore[arg-type]
    assert found is None and "2 reader views" in why
    # Same words, different punctuation and a scale note: the same caption.
    labelled = [_view_reading(1, "B - PLAN VIEW 1:15", (0, 0, 100, 100))]
    found, _why = script.match_view(labelled, [10, 10, 90, 90], "B PLAN VIEW")  # type: ignore[arg-type]
    assert found is labelled[0]


def test_the_key_is_found_in_an_env_file_whatever_its_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("# made up\nanthropic_api_key=made-up-key\n")
    assert script._env_key(env) == "made-up-key"
