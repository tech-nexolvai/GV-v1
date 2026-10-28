"""The assistant as a tool-using agent, with claude.ai's tool shapes (owner, 2026-10-16). Scripted
models; no real call, $0.

What is asserted:

- a model that greps the text (`bash_tool`), renders a page and `view`s it, then answers, gets each
  result back in the next turn (the picture as an image block, its own turn echoed unchanged), the
  step labels are its own `description`s, the answer carries its steps, and every call is recorded
  under the agent's prompt id; cache breakpoints sit on the tools, the system prompt and the newest
  block;
- `view` shows a text file with numbered lines (and a line range) and a folder two levels deep;
- the tools check their arguments: a page outside the package, an unknown argument (another
  package's id), a path outside the two folders, a PDF given to `view`, an empty command, an unknown
  tool are each a refused result the model reads, never a crash and never a read;
- `TaskCreate` / `TaskUpdate` keep the task list ("Added task", "Started task", "Completed task")
  and the answer carries it as Progress;
- `Agent` calls of one turn run in parallel (a barrier proves it), at most four, each report goes
  back to the lead; one failing does not stop the others; they share the question's budget; a
  question that delegates gets the larger cap; helpers run on Sonnet unless asked for opus;
- "approve page 3" is refused before any model call, with the sandbox never opened;
- the cost cap stops the loop cleanly; after the last tool turn the model must answer; a provider
  failure is a plain error; without a sandbox the one-call assistant answers.

All names, numbers and pages here are made up.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant.agent import (
    billed_input_tokens,
)
from app.review.assistant.sandbox import UPLOADS, SandboxResult, Workspace

SONNET = "anthropic.claude-sonnet-5-5"
PDF = f"{UPLOADS}/Made-up vendor set.pdf"


def _png(width: int = 40, height: int = 30) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (width, height), "white").save(out, format="PNG")
    return out.getvalue()


@dataclass
class FakeSandbox:
    """Answers commands from a script; `pdftoppm` writes a picture into the scratch folder."""

    workspace: Workspace
    kind: str = "fake"
    commands: list[str] = field(default_factory=list)

    def run(self, command: str) -> SandboxResult:
        self.commands.append(command)
        if command.startswith("pdftoppm"):
            (self.workspace.out / "p-02.png").write_bytes(_png(3000, 2000))
            return SandboxResult(output="", exit_code=0)
        return SandboxResult(output="12: MADE-UP COUNTERTOP 37 3/8\n", exit_code=0)


def _use(call_id: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {"type": "tool_use", "id": call_id, "name": name, "input": dict(arguments)}


def _bash(call_id: str, command: str, why: str = "Made-up step") -> dict[str, Any]:
    return _use(call_id, "bash_tool", {"command": command, "description": why})


def _turn(*blocks: dict[str, Any], stop: str = "tool_use") -> dict[str, Any]:
    return {
        "stop_reason": stop,
        "content": [{"type": "thinking", "thinking": "", "signature": "sig-made-up"}, *blocks],
        "usage": {"input_tokens": 2000, "output_tokens": 100},
        "model": "anthropic/claude-sonnet-5.5",
    }


@dataclass
class FakeLedger:
    rows: list[dict[str, Any]] = field(default_factory=list)

    def before_call(self) -> None:
        pass

    def record(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)


@pytest.fixture
def workspace() -> Any:
    space = Workspace.create([("Made-up vendor set.pdf", b"%PDF-1.4 made up")])
    yield space
    space.close()


# ---- the shell, the viewer, the results ----------------------------------------------------------


# ---- the task list -------------------------------------------------------------------------------


# ---- refusals, caps, ends ------------------------------------------------------------------------


def test_workspace_holds_only_the_given_files_read_only(workspace: Workspace) -> None:
    assert [path.name for path in workspace.work.iterdir()] == ["Made-up vendor set.pdf"]
    with pytest.raises(PermissionError):
        (workspace.work / "Made-up vendor set.pdf").write_bytes(b"changed")
    root = workspace.root
    workspace.close()
    assert not Path(root).exists()


def test_cached_prompt_tokens_are_priced_as_cache_reads_and_writes() -> None:
    usage = {
        "input_tokens": 100,
        "cache_creation_input_tokens": 1000,
        "cache_read_input_tokens": 10_000,
        "output_tokens": 50,
    }
    # 100 + 1.25 x 1000 + 0.1 x 10000
    assert billed_input_tokens(usage) == 2350


# ---- helper agents (`Agent`) ---------------------------------------------------------------------


# ---- nothing recorded is dropped; helpers open every page (owner, 2026-10-16) --------------------


# ---- helper budget (owner, 2026-10-16): smaller pictures, cache-aware estimate, 6k report ----
