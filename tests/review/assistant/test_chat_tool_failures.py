"""Tools that fail in a way nobody planned for (harness audit, 2026-10-19).

Before this fix:
- a tool that raised an unexpected error (a code bug, a damaged set-pack file) ended the WHOLE
  answer ("Something went wrong in the assistant") and left its step spinning on the screen for
  ever (`ok: null` in the stored answer);
- `view` of a named pipe the model made in its own folder (`mkfifo`) never returned: the answer
  stayed "running", Stop could not end it, and the chat took no new question until a restart;
- when Docker stopped answering in the middle of a chat, the Docker program's own error (with the
  server's socket path) was shown on the screen and given to the model as the command's output.

Now a tool's unexpected error is a plain tool error the model reads (the answer goes on), no step
is ever stored as running, `view` refuses anything that is not a regular file or a folder, and a
Docker failure is told in plain words.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import chat_tools
from app.review.assistant.chat_tools import TOOL_FAILED_WORDS, ChatTools
from app.review.assistant.sandbox import (
    DOCKER_FAILED_WORDS,
    ChatWorkspace,
    DockerSandbox,
    SandboxRefused,
    SandboxUnavailable,
    view_path,
)
from tests.review.assistant.chat_fakes import SONNET, ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_engine import Harness


def _steps(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b["step"] for b in blocks if b.get("type") == "step"]


def _boom(*_args: Any, **_kwargs: Any) -> Any:
    raise RuntimeError("/Users/someone/secret/path.py exploded")


def test_a_lead_tool_that_raises_is_a_tool_error_and_the_answer_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_tools, "view_path", _boom)
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "view", {"path": "notes.txt", "description": "Read"}), stop="tool_use"),
            reply(text("Done without it.")),
        ]
    )
    harness = Harness(tmp_path, provider, question="Read my notes file.")

    outcome = harness.run()

    assert outcome.stopped is None and outcome.error is None
    assert outcome.text == "Done without it."
    (step,) = _steps(outcome.blocks)
    assert step["ok"] is False and step["output"] == TOOL_FAILED_WORDS
    result = outcome.api_content[2]["content"][0]
    assert result["is_error"] is True and TOOL_FAILED_WORDS in result["content"][0]["text"]
    assert "/Users/" not in str(outcome.blocks) and "exploded" not in str(outcome.api_content)


def test_a_finding_tool_that_raises_is_a_tool_error_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = ScriptedProvider(
        lead=[
            reply(tool("a", "set_coverage", {"pages": [{"page": 1}]}), stop="tool_use"),
            reply(text("Done.")),
        ]
    )
    harness = Harness(tmp_path, provider, question="Read my notes file.")
    monkeypatch.setattr(harness.engine, "_finding_tool", _boom)

    outcome = harness.run()

    assert outcome.stopped is None and outcome.text == "Done."
    result = outcome.api_content[2]["content"][0]
    assert result["is_error"] is True and "exploded" not in str(result)


def test_a_helper_tool_that_raises_is_told_to_the_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chat_tools, "view_path", _boom)
    brief = "Look at page 2."
    provider = ScriptedProvider(
        lead=[
            reply(
                tool("h", "Agent", {"description": "Page 2", "prompt": brief, "model": "sonnet"}),
                stop="tool_use",
            ),
            reply(text("The helper could not look.")),
        ],
        helpers={
            brief: [
                reply(tool("v", "view", {"path": "x.png"}), stop="tool_use", model=SONNET),
                reply(text("Helper report: the view failed."), model=SONNET),
            ]
        },
    )
    harness = Harness(tmp_path, provider, question="Read my notes file.")

    outcome = harness.run()

    assert outcome.stopped is None and outcome.text == "The helper could not look."
    agents = next(s for s in _steps(outcome.blocks) if s.get("agents"))["agents"]
    assert agents[0]["ok"] is True
    assert [s["ok"] for s in agents[0]["steps"]] == [False]


def test_an_answer_that_fails_leaves_no_step_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = Harness(tmp_path, ScriptedProvider(lead=[]))
    engine = harness.engine
    engine._add_block(
        {"type": "step", "step": {"id": "a", "tool": "bash_tool", "label": "Reading", "ok": None}}
    )
    monkeypatch.setattr(engine, "_run", _boom)

    outcome = harness.run()

    assert outcome.stopped == "error"
    assert all(step["ok"] is not None for step in _steps(outcome.blocks))


def _with_timeout(work: Any, seconds: float = 5.0) -> tuple[bool, Any]:
    done: dict[str, Any] = {}

    def run() -> None:
        try:
            done["value"] = work()
        except BaseException as raised:  # noqa: BLE001 - handed back to the test
            done["error"] = raised

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        # Unblock the reader so the test process can end.
        return False, None
    return True, done


def test_view_refuses_a_named_pipe_at_once(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    pipe = workspace.home / "pipe.txt"
    os.mkfifo(pipe)
    try:
        finished, done = _with_timeout(lambda: view_path(workspace, "/home/claude/pipe.txt"))
    finally:
        if not pipe.exists():
            pass
    if not finished:
        # Let the blocked reader go before failing.
        with open(pipe, "w") as writer:
            writer.write("x")
    assert finished, "view of a named pipe never returned"
    assert isinstance(done.get("error"), SandboxRefused)


def test_the_view_tool_tells_the_model_plainly_about_a_pipe(tmp_path: Path) -> None:
    workspace = ChatWorkspace.open(tmp_path / "chat")
    os.mkfifo(workspace.home / "pipe.png")
    tools = ChatTools(workspace=workspace, sandbox=None)  # type: ignore[arg-type]

    finished, done = _with_timeout(
        lambda: tools.view({"path": "pipe.png"}, {"id": "a", "label": "x", "ok": None})
    )
    if not finished:
        with open(workspace.home / "pipe.png", "w") as writer:
            writer.write("x")
    assert finished
    result = done["value"]
    assert result.is_error and "not a regular file" in result.content[0]["text"]


def _fake_docker(tmp_path: Path, script: str) -> str:
    path = tmp_path / "docker"
    path.write_text("#!/bin/sh\n" + script)
    path.chmod(0o755)
    return str(path)


def test_a_docker_that_stopped_answering_is_told_plainly(tmp_path: Path) -> None:
    docker = _fake_docker(
        tmp_path,
        'echo "docker: Cannot connect to the Docker daemon at '
        'unix:///Users/someone/.docker/run/docker.sock. Is the docker daemon running?" >&2\n'
        "exit 125\n",
    )
    workspace = ChatWorkspace.open(tmp_path / "chat")
    sandbox = DockerSandbox(workspace, docker=docker)

    with pytest.raises(SandboxUnavailable):
        sandbox.run("ls")

    tools = ChatTools(workspace=workspace, sandbox=sandbox)
    result = tools.bash({"command": "ls"}, {"id": "a", "label": "x", "ok": None})
    assert result.is_error
    assert DOCKER_FAILED_WORDS in result.content[0]["text"]
    assert "/Users/" not in str(result.content) and "/Users/" not in str(result.step)


def test_a_command_that_itself_exits_125_is_still_its_own_result(tmp_path: Path) -> None:
    docker = _fake_docker(tmp_path, 'echo "my own words"\nexit 125\n')
    workspace = ChatWorkspace.open(tmp_path / "chat")
    sandbox = DockerSandbox(workspace, docker=docker)

    result = sandbox.run("exit 125")

    assert result.exit_code == 125 and "my own words" in result.output
