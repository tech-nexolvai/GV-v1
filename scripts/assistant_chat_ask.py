"""Ask the chat-first assistant one question from the command line, and watch it work (#1463).

It talks to a running API like the screen does: starts a chat about the drawing set (or uses
`--chat`), sends the question with the model, effort and thinking given, prints the streamed events
compactly as they arrive (thinking, steps, helpers, files, the answer as it is written), then the
stored answer and what it cost. Ctrl-C stops the message (it is still stored, as cancelled).

This makes real, paid model calls through the server's provider: run it only when that is intended.

Usage:
    python scripts/assistant_chat_ask.py --api http://127.0.0.1:8071 --package <package id> \\
        --question "Which items are wrong comparing the vendor and architect drawings?" \\
        [--model claude-opus-5-5] [--effort high] [--thinking on|off] [--chat <chat id>] \\
        [--header "Name: value"] [--quiet-thinking]

Exit code: 0 when the answer finished, 1 when it stopped early or failed, 2 for a refused request.
Standard library only; no key is read or printed here (the server holds the provider key).
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

_PREFIX = "/api/v1"


def _request(
    api: str, path: str, headers: dict[str, str], body: dict[str, Any] | None = None
) -> urllib.request.Request:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(f"{api.rstrip('/')}{_PREFIX}{path}", data=data, method="POST")
    request.add_header("content-type", "application/json")
    request.add_header("accept", "text/event-stream, application/json")
    for name, value in headers.items():
        request.add_header(name, value)
    return request


def _json(api: str, path: str, headers: dict[str, str]) -> Any:
    with urllib.request.urlopen(_request(api, path, headers, {}), timeout=60) as response:
        return json.loads(response.read().decode("utf-8") or "null")


def _events(lines: Iterator[bytes]) -> Iterator[tuple[str, Any]]:
    """Server-Sent Events as (event, data) pairs; keepalive comments are skipped."""
    event, data = "message", []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield event, json.loads("\n".join(data))
            event, data = "message", []
        elif line.startswith(":"):
            continue
        elif line.startswith("event:"):
            event = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data.append(line.removeprefix("data:").strip())


def _short(text: Any, limit: int = 100) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 3] + "..."


def _step_line(step: dict[str, Any], indent: str = "  ") -> str:
    mark = {True: "ok", False: "FAILED", None: "..."}[step.get("ok")]
    seconds = f" {step['seconds']}s" if step.get("seconds") is not None else ""
    detail = step.get("command") or step.get("path") or ""
    line = f"{indent}[{step.get('tool')}] {_short(step.get('label'), 70)} ({mark}{seconds})"
    if detail:
        line += f"\n{indent}    {_short(detail, 120)}"
    if step.get("output") and step.get("ok") is False:
        line += f"\n{indent}    -> {_short(step['output'], 160)}"
    return line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--api", required=True, help="the API's base URL, e.g. http://127.0.0.1:8071"
    )
    parser.add_argument("--package", required=True, help="the drawing set's package id")
    parser.add_argument("--question", required=True)
    parser.add_argument("--model", default="claude-opus-5-5")
    parser.add_argument("--effort", default="high")
    parser.add_argument("--thinking", choices=("on", "off"), default="on")
    parser.add_argument("--chat", help="an existing chat's id (default: start a new chat)")
    parser.add_argument(
        "--header", action="append", default=[], help='an extra request header, "Name: value"'
    )
    parser.add_argument("--quiet-thinking", action="store_true", help="do not print thinking")
    args = parser.parse_args(argv)
    headers = {
        name.strip(): value.strip()
        for name, value in (item.split(":", 1) for item in args.header if ":" in item)
    }

    chat = args.chat
    try:
        if chat is None:
            chat = _json(args.api, f"/packages/{args.package}/chats", headers)["id"]
            print(f"chat {chat} (new)")
        else:
            print(f"chat {chat}")
        body = {
            "text": args.question,
            "model": args.model,
            "effort": args.effort,
            "thinking": args.thinking == "on",
        }
        response = urllib.request.urlopen(
            _request(args.api, f"/chats/{chat}/messages", headers, body), timeout=3600
        )
    except urllib.error.HTTPError as error:
        print(f"refused: HTTP {error.code} {error.read().decode('utf-8', 'replace')[:500]}")
        return 2
    except urllib.error.URLError as error:
        print(f"could not reach {args.api}: {error.reason}")
        return 2

    final: dict[str, Any] | None = None
    errors: list[str] = []
    cost = "0.00"
    in_text = False
    try:
        with response:
            for event, data in _events(iter(response.readline, b"")):
                if event != "text_delta" and in_text:
                    print()
                    in_text = False
                if event == "message_start":
                    thinking = "thinking on" if data["thinking"] else "thinking off"
                    print(
                        f"message {data['message_id']}: {data['model']}, {data['effort']} "
                        f"effort, {thinking}"
                    )
                elif event == "thinking_start" and not args.quiet_thinking:
                    print("[thinking]")
                elif event == "thinking_delta" and not args.quiet_thinking:
                    sys.stdout.write(data["text"])
                    sys.stdout.flush()
                elif event == "thinking_end":
                    print(
                        f"\n[thought for {data['seconds']}s]"
                        if not args.quiet_thinking
                        else f"[thought for {data['seconds']}s]"
                    )
                elif event == "text_delta":
                    if not in_text:
                        print("[answer]")
                        in_text = True
                    sys.stdout.write(data["text"])
                    sys.stdout.flush()
                elif event == "step_start":
                    step = data["step"]
                    print(f"> {step['tool']}: {_short(step['label'], 90)}")
                elif event == "step_end":
                    print(_step_line(data["step"]))
                elif event == "tasks":
                    marks = {"pending": " ", "in_progress": ">", "completed": "x"}
                    print(
                        "  tasks: "
                        + "; ".join(
                            f"[{marks.get(t['status'], '?')}] {_short(t['subject'], 40)}"
                            for t in data["tasks"]
                        )
                    )
                elif event == "agent_start":
                    agent = data["agent"]
                    print(
                        f"  helper {agent['id']} started: {agent['description']} "
                        f"({agent['model']})"
                    )
                elif event == "agent_step":
                    print(_step_line(data["step"], indent="    helper: "))
                elif event == "agent_end":
                    agent = data["agent"]
                    state = "reported" if agent.get("ok") else "stopped"
                    stopped = f", {agent['stopped']}" if agent.get("stopped") else ""
                    print(
                        f"  helper {agent['id']} {state}{stopped}: "
                        f"{_short(agent.get('report'), 160)}"
                    )
                elif event == "file":
                    file = data["file"]
                    print(
                        f"  file: {file['name']} ({file['size']} bytes) "
                        f"{args.api.rstrip('/')}{file['url']}"
                    )
                elif event == "usage":
                    cost = data["cost_usd"]
                elif event == "error":
                    errors.append(data["message"])
                    print(f"error: {data['message']}")
                elif event == "message_end":
                    final = data["message"]
    except KeyboardInterrupt:
        print("\nstopping the message...")
        try:
            print(_json(args.api, f"/chats/{chat}/cancel", headers))
        except urllib.error.URLError as error:
            print(f"could not cancel: {error}")
        return 1

    print("\n" + "=" * 72)
    if final is None:
        print("no stored answer came back")
        return 1
    print(final["text"] or "(no answer text)")
    print("=" * 72)
    names = [f["name"] for f in final.get("files") or []]
    print(
        f"stopped: {final.get('stopped') or 'no (finished)'}"
        + (f"; error: {final['error']}" if final.get("error") else "")
    )
    print(f"files: {', '.join(names) if names else 'none'}")
    print(f"cost: ${final.get('cost_usd') or cost} · chat {chat} · message {final['id']}")
    return 0 if final.get("stopped") is None and not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
