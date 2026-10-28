"""Harness pre-checks (final plan, change 11). Made-up values only; no network, no paid call.

- **Credit before a full check.** With OpenRouter, the account's credit is read for free before
  any paid step (`provider_usage.credit_left`); below the fixed price plus a spare dollar, the check
  pauses with plain words ("The AI account has $X left; this check needs up to $Y … Top up, then
  Run."), Run anyway or not. When the credit cannot be read the check starts as before and the
  pre-flight says so. Anthropic's own API has no balance: no line.
- **An honest stop on a 402.** When the AI account runs out part way, no new helper starts, what
  is done is kept, and the answer says "Stopped: the AI account ran out of credit. Top up, then
  Continue." (summary, trust problems, Continue), never that the fixed price ran out.
- **Lazy Docker.** With Docker not running the answer still runs; only the shell tool answers
  that it is unavailable, and the full check's pre-flight says so in one line.
"""

from __future__ import annotations

import dataclasses
import json
import threading
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from app.models import AssistantChatMessage
from app.provider_usage import CREDITS_URL, KEY_URL, credit_left
from app.review.assistant.chat_engine import MessageOutcome
from app.review.assistant.chat_provider import (
    CREDIT_NOT_STARTED,
    CREDIT_STOP_WORDS,
    AnthropicSdkProvider,
    Credit,
    ProviderChoice,
    ProviderFailure,
    ServiceBreaker,
)
from app.review.assistant.chat_service import _say_service_stop, continuable
from app.review.assistant.chat_tools import ChatTools
from app.review.assistant.plan_supervisor import SANDBOX_DOWN_WORDS, harness_lines
from app.review.assistant.sandbox import (
    NO_SANDBOX_WORDS,
    ChatWorkspace,
    NoSandbox,
    SandboxUnavailable,
)
from tests.review.assistant.chat_fakes import ScriptedProvider
from tests.review.assistant.test_full_check_roster import _clean_ink, _Roster, _roster

DOLLARS = 1_000_000

# ---- reading the credit ------------------------------------------------------------------------


def _fetch(answers: Mapping[str, tuple[int, object]]) -> Any:
    asked: list[tuple[str, str]] = []

    def fetch(url: str, headers: Mapping[str, str], timeout: float) -> tuple[int, bytes]:
        asked.append((url, headers["Authorization"]))
        status, body = answers.get(url, (404, b""))
        return status, body if isinstance(body, bytes) else json.dumps(body).encode()

    fetch.asked = asked  # type: ignore[attr-defined]
    return fetch


def test_the_credit_left_is_the_account_credits_less_its_usage() -> None:
    fetch = _fetch(
        {
            CREDITS_URL: (200, {"data": {"total_credits": 58.07, "total_usage": 50.0}}),
            KEY_URL: (200, {"data": {"usage": 3, "limit_remaining": None}}),
        }
    )

    assert credit_left("made-up-key", fetch=fetch) == Decimal("8.07")
    # The key goes only to OpenRouter, as a bearer token.
    assert {url for url, _ in fetch.asked} == {CREDITS_URL, KEY_URL}
    assert all(auth == "Bearer made-up-key" for _, auth in fetch.asked)


def test_a_key_limit_below_the_account_credit_is_what_is_left() -> None:
    fetch = _fetch(
        {
            CREDITS_URL: (200, {"data": {"total_credits": 100, "total_usage": 10}}),
            KEY_URL: (200, {"data": {"limit_remaining": 2.5}}),
        }
    )

    assert credit_left("made-up-key", fetch=fetch) == Decimal("2.5")


@pytest.mark.parametrize(
    "answer",
    [
        (500, b""),
        (200, b"not json"),
        (200, {"data": {"total_credits": "lots"}}),
        (200, {"error": "no"}),
    ],
)
def test_a_credit_that_cannot_be_read_is_none(answer: tuple[int, object]) -> None:
    assert credit_left("made-up-key", fetch=_fetch({CREDITS_URL: answer})) is None


def test_anthropics_own_api_offers_no_credit_to_read() -> None:
    provider = AnthropicSdkProvider(ProviderChoice("anthropic", "made-up-key"), timeout_seconds=60)

    assert provider.credit() == Credit("not_offered", None)


def test_openrouter_reads_its_credit(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def fake(key: str) -> Decimal:
        seen.append(key)
        return Decimal("12.5")

    monkeypatch.setattr("app.provider_usage.credit_left", fake)
    provider = AnthropicSdkProvider(ProviderChoice("openrouter", "made-up-key"), timeout_seconds=60)

    assert provider.credit() == Credit("ok", Decimal("12.5"))
    assert seen == ["made-up-key"]
    assert "made-up-key" not in repr(provider)


# ---- the pre-flight lines ----------------------------------------------------------------------


@dataclasses.dataclass
class _Engine:
    provider: Any
    tools: Any


def _with_credit(found: Credit | Exception | None) -> Any:
    class Provider:
        name = "openrouter"

        def credit(self) -> Credit:
            if isinstance(found, Exception):
                raise found
            assert found is not None
            return found

    return Provider()


def test_too_little_credit_is_a_stop_with_plain_words() -> None:
    engine = _Engine(_with_credit(Credit("ok", Decimal("4.20"))), tools=None)

    lines = harness_lines(engine, 9 * DOLLARS)  # type: ignore[arg-type]

    assert [line.id for line in lines] == ["credit_low"]
    line = lines[0]
    assert line.level == "stop" and not line.goes_on and line.who == "you"
    assert line.words == (
        "The AI account has $4.20 left; this check needs up to $9.00 (and $1.00 kept spare). "
        "Top up, then Run."
    )
    assert "fixed price" not in line.words


def test_the_spare_dollar_counts() -> None:
    near = harness_lines(_Engine(_with_credit(Credit("ok", Decimal("9.50"))), None), 9 * DOLLARS)  # type: ignore[arg-type]
    enough = harness_lines(_Engine(_with_credit(Credit("ok", Decimal(10))), None), 9 * DOLLARS)  # type: ignore[arg-type]

    assert [line.id for line in near] == ["credit_low"]
    assert [(line.id, line.level) for line in enough] == [("credit_ok", "ok")]
    assert enough[0].words == "The AI account has $10.00 left; this check needs up to $9.00."


@pytest.mark.parametrize("found", [Credit("unknown", None), RuntimeError("no network")])
def test_a_credit_that_cannot_be_read_starts_as_usual_and_says_so(
    found: Credit | Exception,
) -> None:
    lines = harness_lines(_Engine(_with_credit(found), None), 9 * DOLLARS)  # type: ignore[arg-type]

    assert [(line.id, line.level, line.goes_on) for line in lines] == [
        ("credit_unknown", "note", True)
    ]
    assert lines[0].words == (
        "The AI account's credit could not be read, so the check starts as usual."
    )


def test_no_credit_line_without_a_balance_to_read() -> None:
    assert harness_lines(_Engine(_with_credit(Credit("not_offered", None)), None), DOLLARS) == []  # type: ignore[arg-type]
    assert harness_lines(_Engine(object(), None), DOLLARS) == []  # type: ignore[arg-type]


def test_docker_down_is_one_line_and_the_check_goes_on(tmp_path: Path) -> None:
    tools = ChatTools(
        workspace=ChatWorkspace.open(tmp_path / "chat"),
        sandbox=NoSandbox(ChatWorkspace.open(tmp_path / "chat")),
        page_count=1,
        results=lambda page: {},
    )

    lines = harness_lines(_Engine(object(), tools), DOLLARS)  # type: ignore[arg-type]

    assert [(line.id, line.level, line.goes_on) for line in lines] == [
        ("sandbox_down", "warn", True)
    ]
    assert lines[0].words == SANDBOX_DOWN_WORDS


# ---- lazy Docker ---------------------------------------------------------------------------------


def test_with_no_sandbox_only_the_shell_tool_says_it_is_unavailable(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    sandbox = NoSandbox(workspace)
    with pytest.raises(SandboxUnavailable, match="unavailable: the sandbox is not running"):
        sandbox.run("ls")
    sandbox.kill_all()  # nothing runs: harmless
    tools = ChatTools(workspace=workspace, sandbox=sandbox, page_count=1, results=lambda p: {})

    result = tools.bash({"command": "ls", "description": "List"}, {"tool": "bash_tool"})

    assert result.is_error
    assert result.content == [{"type": "text", "text": f"Refused: {NO_SANDBOX_WORDS}"}]


# ---- the full check: credit too low, unreadable, or gone part way --------------------------------


@dataclasses.dataclass
class _Account(ScriptedProvider):
    """A scripted provider with the real breaker and a credit check, like the SDK one."""

    credit_found: Credit | None = None

    def __post_init__(self) -> None:
        super().__post_init__()
        self.breaker = ServiceBreaker()

    def credit(self) -> Credit:
        return self.credit_found or Credit("not_offered", None)

    def open_stream(self, params: Mapping[str, Any]) -> Any:
        self.breaker.check()
        try:
            return super().open_stream(params)
        except ProviderFailure as failure:
            self.breaker.failed(failure)
            raise


def _payment() -> ProviderFailure:
    return ProviderFailure(
        "The AI service account needs attention (payment), so the AI did not answer. Tell your "
        "admin.",
        status=402,
        retryable=False,
        kind="payment",
    )


def _account(
    script: _Roster, *, credit: Credit | None = None, fail_first: bool = False
) -> _Account:
    """The roster's script behind the account; with `fail_first` the very first call (the cache
    warm-up, before any helper) is refused for payment, as on 25 Oct."""
    first = threading.Event()

    def answer(params: Mapping[str, Any]) -> Any:
        if fail_first and not first.is_set():
            first.set()
            return _payment()
        return script(params)

    return _Account(lead=[], helper_answer=answer, credit_found=credit)


def _run(tmp_path: Path, provider: _Account, *, run_anyway: bool = False) -> tuple[Any, Any]:
    harness = _roster(tmp_path, _Roster())
    provider.cancel = harness.cancel
    harness.provider = provider
    engine = harness.engine
    engine.provider = provider
    engine.settings = dataclasses.replace(engine.settings, run_anyway=run_anyway)
    return harness, harness.run()


def _block(outcome: MessageOutcome, kind: str) -> Any:
    return next((b[kind] for b in outcome.blocks if b.get("type") == kind), None)


@pytest.mark.parametrize("run_anyway", [False, True])
def test_a_full_check_with_too_little_credit_pauses_before_any_paid_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, run_anyway: bool
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    provider = _account(script, credit=Credit("ok", Decimal("1.50")))
    provider.name = "openrouter"  # the account whose credit is read (no warm-up call there)

    _harness, outcome = _run(tmp_path, provider, run_anyway=run_anyway)

    assert outcome.stopped == "cancelled"  # stored as "paused before spending"
    preflight = _block(outcome, "preflight")
    assert preflight["paused"] is True and preflight["stops"] == ["credit_low"]
    words = preflight["lines"][0]["words"]
    assert words.startswith("The AI account has $1.50 left; this check needs up to $")
    assert words.endswith("Top up, then Run.")
    assert outcome.text == f"Paused before comparing anything: {words} Nothing was compared."
    assert script.calls == {}  # no helper, no look, no pick
    assert provider.calls == [] and outcome.cost_micros == 0


def test_a_full_check_whose_credit_cannot_be_read_runs_and_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    provider = _account(_Roster(), credit=Credit("unknown", None))

    _harness, outcome = _run(tmp_path, provider)

    preflight = _block(outcome, "preflight")
    assert preflight["paused"] is False
    assert [line["id"] for line in preflight["lines"] if line["id"].startswith("credit")] == [
        "credit_unknown"
    ]
    assert outcome.stopped is None
    assert "Fixed price $5: this check used $" in outcome.text


def test_a_full_check_with_enough_credit_runs_with_an_ok_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    provider = _account(_Roster(), credit=Credit("ok", Decimal(40)))

    _harness, outcome = _run(tmp_path, provider)

    lines = _block(outcome, "preflight")["lines"]
    assert [line["id"] for line in lines if line["id"].startswith("credit")] == ["credit_ok"]
    assert outcome.stopped is None and CREDIT_STOP_WORDS not in outcome.text


def test_a_402_part_way_stops_honestly_and_continue_finishes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clean_ink(monkeypatch)
    script = _Roster()
    provider = _account(script, credit=Credit("ok", Decimal(40)), fail_first=True)

    harness, outcome = _run(tmp_path, provider)

    # No helper started once the account asked for payment; nothing was paid after it.
    assert script.calls == {}
    steps = [b["step"] for b in outcome.blocks if b.get("type") == "step"]
    agents = [a for step in steps for a in step.get("agents") or []]
    assert agents and all(a["report"] == CREDIT_NOT_STARTED for a in agents)
    # The answer: kept, continuable, and honest about why.
    assert outcome.stopped is None
    assert outcome.text.startswith(CREDIT_STOP_WORDS)
    assert "Check incomplete: the AI account ran out of credit" in outcome.text
    assert "Not finished (the AI account ran out of credit)" in outcome.text
    assert "within the fixed price" not in outcome.text
    assert "fixed price ran out" not in outcome.text
    assert "step did not finish" not in outcome.text
    problems = _block(outcome, "trust")["problems"]
    assert problems[0] == CREDIT_STOP_WORDS
    assert not any("fixed price" in line for line in problems)
    resume = _block(outcome, "resume")
    assert resume["mode"] == "full_check" and resume["redo"].startswith(CREDIT_STOP_WORDS)
    stored = AssistantChatMessage(
        chat_id=uuid4(),
        seq=2,
        role="assistant",
        created_by="reviewer",
        text=outcome.text,
        blocks=outcome.blocks,
        api_content=[],
        mode="full_check",
        stopped=outcome.stopped,
    )
    assert continuable(stored)
    # The service's own "tell your admin" words are not added on top of the honest stop.
    before = list(outcome.blocks)
    _say_service_stop(provider, outcome)
    assert outcome.blocks == before
    assert harness.engine.out_of_credit()


def test_a_402_after_all_the_work_is_done_is_not_a_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account runs out on the summary's rewording (the last call): every page is compared,
    so the check is not "stopped"; only the service's own line says what happened."""
    from app.review.assistant.full_check_roster import SUMMARY_SYSTEM

    _clean_ink(monkeypatch)
    script = _Roster()

    def answer(params: Mapping[str, Any]) -> Any:
        if params["system"][0]["text"] == SUMMARY_SYSTEM:
            return _payment()
        return script(params)

    provider = _Account(lead=[], helper_answer=answer, credit_found=Credit("ok", Decimal(40)))

    _harness, outcome = _run(tmp_path, provider)

    assert provider.breaker.kind == "payment"
    assert outcome.stopped is None
    assert CREDIT_STOP_WORDS not in outcome.text
    assert _block(outcome, "resume") is None
    _say_service_stop(provider, outcome)
    assert outcome.text.endswith(
        "**AI calls stopped:** The AI service account needs attention (payment), so the AI did "
        "not answer. Tell your admin."
    )


def _unreachable() -> ProviderFailure:
    return ProviderFailure(
        "The AI service could not be reached.", status=None, retryable=False, kind="unreachable"
    )


def test_an_outage_part_way_is_stored_as_an_outage_never_as_out_of_money(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2026-10-28: an outage cut a real check during its devil's advocate; it was stored as
    "out of money" (`cost_cap`) with most of its price left. It is stored as what it was: not a
    money stop, the reason said, and Continue finishes it."""
    from app.review.assistant.full_check_roster import ADVOCATE_SYSTEM

    _clean_ink(monkeypatch)
    script = _Roster()

    def answer(params: Mapping[str, Any]) -> Any:
        if params["system"][0]["text"] == ADVOCATE_SYSTEM:
            return _unreachable()
        return script(params)

    provider = _Account(lead=[], helper_answer=answer, credit_found=Credit("ok", Decimal(40)))

    harness, outcome = _run(tmp_path, provider)

    assert provider.breaker.kind == "unreachable"
    assert not harness.engine.out_of_credit()
    assert outcome.stopped is None and outcome.cap_hit is None  # never "cost_cap"
    assert "Not finished (the AI service could not be reached)" in outcome.text
    assert "Check incomplete: the AI service could not be reached" in outcome.text
    assert "out of money" not in outcome.text
    assert "within the fixed price" not in outcome.text
    assert "step did not finish" not in outcome.text
    problems = _block(outcome, "trust")["problems"]
    assert problems[0].startswith("Stopped part way: the AI service could not be reached.")
    assert not any("fixed price" in line for line in problems)
    resume = _block(outcome, "resume")
    assert resume["redo"].startswith("Stopped: the AI service could not be reached.")
