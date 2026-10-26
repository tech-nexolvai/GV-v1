"""The reviewer's clicks as candidate keyed cases (`click_cases`, plan change #10).

An Agree on an item code proved Wrong is a confirmed case of its kind of check; a Not a problem on
one is a labelled false red; a click on anything code did not prove is no evidence about a kind.
Made-up values only; the database part runs the real app on a test PostgreSQL."""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import check_record, click_cases, proof
from tests.review.assistant.reading_fixture import two_routes


def _proved(**more: Any) -> dict[str, Any]:
    """A size the vendor prints against the architect's, proved by code (made-up values)."""
    return two_routes(
        {
            "id": "F1",
            "page": 3,
            "item": "Vanity: top width",
            "status": "different",
            "vendor": '50"',
            "architect": '48"',
            "source": "architect",
            "checks": {"printed_architect": "match", "printed_vendor": "match"},
            "trust_level": "confirmed",
            "witnesses": ["printed"],
            "paired_by": "code",
            "inches": {"architect": "48", "vendor": "50", "difference": "2"},
            **more,
        }
    )


def _ai_only(**more: Any) -> dict[str, Any]:
    """The same item read by the AI only: code proved nothing."""
    return _proved(checks={}, trust_level="to_check", witnesses=[], paired_by=None, **more)


def test_the_made_up_items_are_what_they_say() -> None:
    assert proof.proved_wrong(_proved())
    assert not proof.proved_wrong(_ai_only())
    assert not proof.proved_wrong(_proved(status="same"))


def test_an_agree_on_a_code_proved_item_is_a_confirmed_case() -> None:
    case = click_cases.case_of(_proved(), {"decision": "agree"})
    assert case is not None
    assert case["kind"] == "size_vs_architect" and case["verdict"] == click_cases.RIGHT_RED
    assert case["value_differs"] is False


def test_not_a_problem_on_a_code_proved_item_is_a_false_red() -> None:
    case = click_cases.case_of(_proved(), {"decision": "not_a_problem"})
    assert case is not None
    assert case["kind"] == "size_vs_architect" and case["verdict"] == click_cases.FALSE_RED


def test_a_click_on_an_item_code_did_not_prove_is_no_case() -> None:
    for decision in ("agree", "not_a_problem"):
        assert click_cases.case_of(_ai_only(), {"decision": decision}) is None
    assert click_cases.case_of(_proved(status="check"), {"decision": "agree"}) is None
    assert click_cases.case_of(_proved(), {"decision": "undo"}) is None


def test_the_item_is_judged_as_code_left_it_not_as_the_click_made_it() -> None:
    # The stored item carries the click (`decision`) and where it put it (`proof`): neither counts.
    laid = _proved(decision={"decision": "not_a_problem"}, proof={"group": "dismissed"})
    case = click_cases.case_of(laid, {"decision": "not_a_problem"})
    assert case is not None and case["group"] == "clouded"


def test_a_held_back_red_counts_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kind is still being proven: code's Wrong waits as a Likely problem. The reviewer's click
    # is evidence about the kind all the same (a rejected would-be red is a false red).
    monkeypatch.setenv(check_record.SWITCH_ENV, "on")
    monkeypatch.setattr(check_record, "records", lambda path=None: {})
    found = _proved(created_by="sink_offset")
    assert proof.proof_group(found) == "likely_problem"
    agreed = click_cases.case_of(found, {"decision": "agree"})
    rejected = click_cases.case_of(found, {"decision": "not_a_problem"})
    assert agreed is not None and rejected is not None
    assert agreed["kind"] == rejected["kind"] == "sink_offset"
    assert agreed["held_back"] and agreed["group"] == "likely_problem"
    assert (agreed["verdict"], rejected["verdict"]) == ("right", "false")


def test_an_agree_with_another_should_be_is_flagged_for_a_person() -> None:
    same = click_cases.case_of(_proved(), {"decision": "agree", "should_be": ' 48" '})
    other = click_cases.case_of(_proved(), {"decision": "agree", "should_be": '47 1/2"'})
    assert same is not None and other is not None
    assert same["value_differs"] is False
    assert other["value_differs"] is True and other["should_be"] == '47 1/2"'
    assert other["verdict"] == "right"  # a real problem; the person judges the value


# ---- the database: what the owner's local export reads --------------------------------------------

pytest_plugins = ("tests.app.postgres_fixture",)


def test_candidates_read_the_latest_clicks_from_the_database(  # type: ignore[no-untyped-def]
    postgres_engine, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.review.assistant import chat_engine, chat_trust
    from tests.api.test_assistant_chats import _named, _world
    from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
    from tests.review.assistant.test_chat_findings import finding_input

    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool("r2", "report_finding", finding_input(item="Sink base width")),
                stop="tool_use",
            ),
            reply(text("Two differences.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id = world.chat()
        status, events = world.ask(chat_id, "Which items are wrong?")
        assert status == 200
        answer = _named(events, "message_end")[0]["message"]
        path = f"/api/v1/chats/{chat_id}/messages/{answer['id']}/findings"
        assert (
            world.client.post(f"{path}/F1/decision", json={"decision": "agree"}).status_code == 201
        )
        assert (
            world.client.post(f"{path}/F2/decision", json={"decision": "agree"}).status_code == 201
        )
        assert (
            world.client.post(f"{path}/F2/decision", json={"decision": "not_a_problem"}).status_code
            == 201
        )

        # The scripted items are AI readings only: code proved nothing, so no candidate at all.
        assert click_cases.candidates(world.session) == []

        # Had code proved them, each latest click is one candidate (F2's changed mind counts once).
        monkeypatch.setattr(proof, "proved_wrong", lambda finding: True)
        found = click_cases.candidates(world.session)
        assert [(c["finding_id"], c["verdict"]) for c in found] == [
            ("F1", "right"),
            ("F2", "false"),
        ]
        assert {c["chat_id"] for c in found} == {chat_id}
        assert all(c["message_id"] == answer["id"] and c["decided_by"] for c in found)
