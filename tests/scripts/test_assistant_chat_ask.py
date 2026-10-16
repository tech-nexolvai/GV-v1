"""The headless chat script (#1463) against a canned local server: no network, no paid call.

Verification for: `scripts/assistant_chat_ask.py`. It starts a chat, sends the question with the
model, effort and thinking given, prints the streamed events and the stored answer, and exits 0 only
when the answer finished.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from scripts.assistant_chat_ask import main

CHAT = "11111111-1111-1111-1111-111111111111"
PACKAGE = "22222222-2222-2222-2222-222222222222"


def _sse(events: list[tuple[str, dict[str, Any]]]) -> bytes:
    body = ": keepalive\n\n"
    for name, data in events:
        body += f"event: {name}\ndata: {json.dumps(data)}\n\n"
    return body.encode()


STREAM = [
    (
        "message_start",
        {"message_id": "m1", "model": "claude-opus-5-5", "effort": "high", "thinking": True},
    ),
    ("thinking_start", {"index": 0}),
    ("thinking_delta", {"index": 0, "text": "Reading the skill."}),
    ("thinking_end", {"index": 0, "seconds": 2}),
    ("step_start", {"index": 1, "step": {"tool": "bash_tool", "label": "Count pages", "ok": None}}),
    (
        "step_end",
        {
            "index": 1,
            "step": {
                "tool": "bash_tool",
                "label": "Count pages",
                "ok": True,
                "command": "pdfinfo x.pdf",
                "seconds": 0.4,
            },
        },
    ),
    ("tasks", {"tasks": [{"id": "1", "subject": "Read", "status": "completed"}]}),
    (
        "agent_start",
        {
            "step_index": 2,
            "agent": {"id": "a1", "description": "Pages 1-9", "model": "claude-opus-5-5"},
        },
    ),
    (
        "agent_end",
        {
            "step_index": 2,
            "agent": {
                "id": "a1",
                "description": "Pages 1-9",
                "model": "claude-opus-5-5",
                "ok": True,
                "report": "Page 4 sink",
                "stopped": None,
            },
        },
    ),
    (
        "file",
        {
            "file": {
                "id": "f1",
                "name": "issues.csv",
                "size": 12,
                "mime": "text/csv",
                "url": f"/api/v1/chats/{CHAT}/files/f1",
            }
        },
    ),
    ("text_delta", {"index": 3, "text": "Page 4: "}),
    ("text_delta", {"index": 3, "text": "the sink differs."}),
    ("usage", {"cost_usd": "0.42"}),
    (
        "message_end",
        {
            "message": {
                "id": "m1",
                "text": "Page 4: the sink differs.",
                "stopped": None,
                "error": None,
                "cost_usd": "0.42",
                "files": [{"name": "issues.csv"}],
            }
        },
    ),
]


@pytest.fixture
def server() -> Iterator[tuple[str, list[tuple[str, Any]]]]:
    seen: list[tuple[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            seen.append((self.path, body))
            if self.path.endswith("/chats"):
                payload = json.dumps({"id": CHAT}).encode()
                self.send_response(201)
                self.send_header("content-type", "application/json")
            else:
                payload = _sse(STREAM)
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", seen
    finally:
        httpd.shutdown()


def test_it_starts_a_chat_streams_the_answer_and_prints_the_cost(
    server: tuple[str, list[tuple[str, Any]]], capsys: pytest.CaptureFixture[str]
) -> None:
    api, seen = server

    code = main(
        [
            "--api",
            api,
            "--package",
            PACKAGE,
            "--question",
            "Which items are wrong?",
            "--model",
            "claude-sonnet-5-5",
            "--effort",
            "medium",
            "--thinking",
            "off",
        ]
    )

    assert code == 0
    assert seen[0] == (f"/api/v1/packages/{PACKAGE}/chats", {})
    assert seen[1] == (
        f"/api/v1/chats/{CHAT}/messages",
        {
            "text": "Which items are wrong?",
            "model": "claude-sonnet-5-5",
            "effort": "medium",
            "thinking": False,
        },
    )
    out = capsys.readouterr().out
    for words in (
        "[thought for 2s]",
        "[bash_tool] Count pages (ok 0.4s)",
        "helper a1 reported: Page 4 sink",
        "file: issues.csv",
        "Page 4: the sink differs.",
        "cost: $0.42",
    ):
        assert words in out


def test_an_existing_chat_is_used_as_it_is(server: tuple[str, list[tuple[str, Any]]]) -> None:
    api, seen = server

    assert main(["--api", api, "--package", PACKAGE, "--question", "Again?", "--chat", CHAT]) == 0
    assert [path for path, _ in seen] == [f"/api/v1/chats/{CHAT}/messages"]
