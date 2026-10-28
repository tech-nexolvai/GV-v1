"""Reviewer-facing text never says "Claude" (#1756).

The product is GV Review; a model is named only by its short name ("Opus 5.5"). Errors, stage
labels, reasons and column names the reviewer can see say "the AI" instead. Prompts sent to the
model, logs, model ids, API docs and comments may still name it.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import anthropic
import httpx

from app.review.assistant.chat_provider import RETENTION_WORDS, failure_from

ROOT = Path(__file__).resolve().parents[3]
WORD = re.compile(r"\bClaude\b")

#: Text sent to the model, never shown to the reviewer: (file, constant).
PROMPTS = {
    ("app/review/assistant/chat_tools.py", "LEAD_SYSTEM_PROMPT"),
    ("app/review/assistant/chat_tools.py", "HELPER_SYSTEM_PROMPT"),
    ("app/review/assistant/chat_tools.py", "FABLE_NOTE"),
}
#: Calls whose strings never reach the screen: logs.
LOG_NAMES = {"_log", "log", "logger", "_logger", "logging"}


def _not_shown(tree: ast.Module) -> set[int]:
    """The ids of string nodes the reviewer never sees: docstrings, logs, API docs, prompts."""
    hidden: set[int] = set()

    def strings(node: ast.AST) -> None:
        hidden.update(id(sub) for sub in ast.walk(node) if isinstance(sub, ast.Constant))

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list):
            for statement in body:
                if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Constant):
                    hidden.add(id(statement.value))  # docstrings and attribute docstrings
        if isinstance(node, ast.Call):
            target = node.func
            owner = target.value if isinstance(target, ast.Attribute) else None
            if isinstance(owner, ast.Name) and owner.id in LOG_NAMES:
                strings(node)
            for keyword in node.keywords:
                if keyword.arg == "description":  # OpenAPI docs, not the screen
                    strings(keyword.value)
    return hidden


def _reviewer_strings_saying_claude() -> list[str]:
    found: list[str] = []
    files = [
        *ROOT.glob("app/review/assistant/**/*.py"),
        *ROOT.glob("app/api/**/*.py"),
    ]
    for path in sorted(files):
        relative = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hidden = _not_shown(tree)
        for node in tree.body:
            if isinstance(node, ast.Assign | ast.AnnAssign):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                names = {target.id for target in targets if isinstance(target, ast.Name)}
                if any((relative, name) in PROMPTS for name in names):
                    hidden.update(id(sub) for sub in ast.walk(node))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in hidden
                and WORD.search(node.value)
            ):
                found.append(f"{relative}:{node.lineno}: {node.value[:70]!r}")
    return found


def test_no_reviewer_facing_string_says_claude() -> None:
    assert _reviewer_strings_saying_claude() == []


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status, request=request)
    return cls("refused", response=response, body=None)


def test_a_refused_key_says_the_ai_did_not_answer() -> None:
    failure = failure_from(_status_error(anthropic.AuthenticationError, 401))
    assert str(failure) == (
        "The AI service refused this server's key, so the AI did not answer. Tell your admin."
    )


def test_provider_failures_never_say_claude() -> None:
    errors: list[BaseException] = [
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PermissionDeniedError, 403),
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.APIStatusError, 402),
        _status_error(anthropic.APIStatusError, 400),
        _status_error(anthropic.APIStatusError, 500),
        ValueError("unreadable"),
    ]
    for error in errors:
        assert not WORD.search(str(failure_from(error))), error
    assert not WORD.search(RETENTION_WORDS)
