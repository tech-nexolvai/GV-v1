"""Chat v2/v3 through the app on a real PostgreSQL: findings with code-made evidence, coverage, the
trust report, the marked-up PDF and the CSV, and a whole-set answer end to end against a local fake
Anthropic server over a real socket (the official SDK, $0).

No paid call is possible: the provider is scripted or a local fake; the sandbox is a fake.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import threading
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, ClassVar
from uuid import UUID, uuid4

import pypdfium2 as pdfium  # type: ignore[import-untyped]
import pytest
from pypdf import PdfReader

from app.review.assistant import chat_engine, chat_trust
from app.review.assistant.chat_findings import COVERAGE_CHECK_PREFIX
from tests.api.test_assistant_chats import _events, _held, _named, _running, _until_saved, _world
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import VENDOR_BOX, finding_input, make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
ASK = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """The checks builder's modules absent: their checks are recorded as not run."""
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _whole_set_script() -> list[list[dict[str, Any]] | BaseException]:
    return [
        reply(
            tool("r1", "report_finding", finding_input()),
            tool("c1", "set_coverage", {"pages": [{"page": 2, "status": "different"}]}),
            stop="tool_use",
        ),
        reply(text("One difference.")),
        reply(
            tool(
                "c2",
                "set_coverage",
                {
                    "pages": [
                        {"page": 1, "status": "same"},
                        {"page": 3, "status": "not_comparable", "note": "notes sheet"},
                    ]
                },
            ),
            stop="tool_use",
        ),
        reply(text("One difference, on page 2.")),
    ]


def _fast_script() -> list[list[dict[str, Any]] | BaseException]:
    """#fast (#1497): a narrow question answered the way Claude does: one reply asks for the set
    pack's status, two zoom crops and the recorded results at once; the next reports one finding
    and answers. Two model calls in all."""
    crops = [
        {"page": 1, "box": {"x0": 40, "y0": 40, "x1": 200, "y1": 90}, "label": "overall"},
        {"page": 2, "box": {"x0": 40, "y0": 40, "x1": 200, "y1": 90}, "label": "depth"},
    ]
    return [
        reply(
            tool(
                "b1",
                "bash_tool",
                {"command": "cat /mnt/user-data/set/status.json", "description": "Pack"},
            ),
            tool("z1", "zoom", {"crops": crops, "description": "Read the two values"}),
            tool("r0", "review_results", {"page": 2}),
            stop="tool_use",
        ),
        reply(text("One difference, on page 2.")),
    ]


def test_findings_stream_are_stored_and_reload_the_same_with_their_pictures(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=_whole_set_script())
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        status, events = world.ask(chat_id, "Which items are wrong?")
        assert status == 200
        message = _named(events, "message_end")[0]["message"]
        kinds = [block["type"] for block in message["blocks"]]
        assert kinds == ["finding", "coverage", "text", "text", "trust", "accuracy"]
        finding = message["blocks"][0]["finding"]
        assert finding["id"] == "F1" and finding["arch_box"]["file"] == "Vendor set.pdf"
        assert finding["checks"]["printed_vendor"] == "not_run"
        coverage = message["blocks"][1]["coverage"]
        assert coverage["counts"]["not_opened"] == 0 and coverage["counts"]["total"] == 3
        # The whole-set guard sent the lead back once with the missing pages.
        guard = json.loads(world.provider.lead_calls[2])["messages"][-1]["content"][-1]["text"]
        assert guard.startswith(COVERAGE_CHECK_PREFIX) and "pages 1, 3" in guard
        # Live events and the stored blocks agree; a reload equals message_end.
        live = {data["index"]: data["finding"] for data in _named(events, "finding")}
        assert live[0] == finding
        assert _named(events, "coverage")[-1]["coverage"] == coverage
        assert _named(events, "trust")[-1]["trust"] == message["blocks"][4]["trust"]
        reloaded = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]
        assert reloaded == message
        for key in ("arch_picture_id", "vendor_picture_id"):
            picture = world.client.get(f"/api/v1/chats/{chat_id}/pictures/{finding[key]}")
            assert picture.status_code == 200 and picture.content[:4] == b"\x89PNG"
        followed = world.client.get(f"/api/v1/chats/{chat_id}/messages/{message['id']}/events")
        assert _events(followed.text) == [("message_end", {"message": message})]


def _red_near(pdf: bytes, page: int, box: Mapping[str, float]) -> int:
    document = pdfium.PdfDocument(pdf)
    try:
        picture = document[page - 1].render(scale=1).to_pil().convert("RGB")
    finally:
        document.close()
    area = picture.crop(
        (int(box["x0"]) - 14, int(box["y0"]) - 14, int(box["x1"]) + 14, int(box["y1"]) + 14)
    )
    raw = area.tobytes()
    return sum(
        1
        for r, g, b in zip(raw[0::3], raw[1::3], raw[2::3], strict=True)
        if r > 180 and g < 140 and b < 90
    )


def test_the_marked_up_pdf_is_drawn_by_code_once_per_answer(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
            reply(text("Nothing to report.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        _status, events = world.ask(chat_id, "Which items are wrong?")
        answer = _named(events, "message_end")[0]["message"]
        url = f"/api/v1/chats/{chat_id}/messages/{answer['id']}/markup-pdf"

        made = world.client.post(url)
        assert made.status_code == 200, made.text
        file = made.json()
        assert file["name"] == "Vendor set - AI markup.pdf" and file["mime"] == "application/pdf"
        pdf = world.client.get(file["url"])
        assert pdf.status_code == 200 and "attachment" in pdf.headers["content-disposition"]
        reader = PdfReader(io.BytesIO(pdf.content))
        assert len(reader.pages) == 4
        assert "AI markup: draft, not GV's signed review" in reader.pages[0].extract_text()
        # The checks modules are off here, so the finding is "to check": an amber cloud.
        assert "AI 1 ? Your decision: Vanity top length" in reader.pages[1].extract_text()
        assert _red_near(pdf.content, 2, VENDOR_BOX) > 5
        assert _red_near(pdf.content, 3, VENDOR_BOX) == 0
        # The same file again, and it is the answer's file on reload.
        again = world.client.post(url)
        assert again.status_code == 200 and again.json() == file
        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]
        assert stored["files"] == [file]

        # An answer without findings, a question, an unknown answer.
        _status, events = world.ask(chat_id, "Anything else?")
        plain = _named(events, "message_end")[0]["message"]
        refused = world.client.post(f"/api/v1/chats/{chat_id}/messages/{plain['id']}/markup-pdf")
        assert refused.status_code == 422 and refused.json()["error"] == "no_findings"
        question = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][0]["id"]
        for message_id in (question, str(uuid4())):
            missing = world.client.post(f"/api/v1/chats/{chat_id}/messages/{message_id}/markup-pdf")
            assert missing.status_code == 404
        csv_missing = world.client.get(f"/api/v1/chats/{chat_id}/messages/{uuid4()}/findings.csv")
        assert csv_missing.status_code == 404

    # Outside the caller's projects: a 404 that says nothing (never a 403, the repo's rule).
    with _world(postgres_engine, tmp_path / "b", member=False) as outsider:
        assert outsider.client.post(url).status_code == 404
        assert outsider.client.get(url.replace("markup-pdf", "findings.csv")).status_code == 404


def test_the_findings_download_as_a_csv(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One difference.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        _status, events = world.ask(chat_id, "Which items are wrong?")
        answer = _named(events, "message_end")[0]["message"]
        response = world.client.get(f"/api/v1/chats/{chat_id}/messages/{answer['id']}/findings.csv")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")
        assert "attachment" in response.headers["content-disposition"]
        rows = list(csv.DictReader(io.StringIO(response.text.lstrip("﻿"))))
        assert len(rows) == 1 and rows[0]["Item"] == "Vanity top length"
        assert rows[0]["Second read"] == "not_run" and rows[0]["Round status"] == "single_round"


def test_following_a_running_message_replays_its_findings(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import _Followed, follow_message
    from app.review.assistant.chat_service import run_message

    gate = threading.Event()
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            _held("One difference on page 2.", gate),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider) as world:
        chat_id, registry, message = _running(world, provider)
        run = message.run
        threading.Thread(target=run_message, args=(message.job, run), daemon=True).start()
        for _ in range(200):
            if any(name == "text_delta" for name, _ in run.feed.kept()):
                break
            threading.Event().wait(0.02)

        async def follow() -> list[tuple[str, Any]]:
            seen: list[tuple[str, Any]] = []
            async for event in follow_message(_Followed(feed=run.feed, stored=None)):
                seen.append((event.event or "", json.loads(event.raw_data or "null")))
                if len(seen) == 1:
                    gate.set()
            return seen

        seen = asyncio.run(follow())
        replayed = [data for name, data in seen if name == "finding"]
        stored = seen[-1][1]["message"]
        # The finding as reported, then as code finished it (its round status): the last one is
        # the stored block.
        assert [data["index"] for data in replayed] == [0, 0]
        assert replayed[-1]["finding"] == stored["blocks"][0]["finding"]
        assert [name for name, _ in seen].index("finding") < [n for n, _ in seen].index("trust")
        _until_saved(registry, chat_id)


# ---- end to end against a local fake Anthropic server (the official SDK over a socket) ----------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class _FakeAnthropic(BaseHTTPRequestHandler):
    """Plays a whole-set script by the tag in the reviewer's question (#findings), like the
    orchestrator's fake_anthropic.py, and keeps every request for the test to check."""

    requests: ClassVar[list[dict[str, Any]]] = []
    problems: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        self.requests.append(body)
        names = [t["name"] for t in body.get("tools") or []]
        messages = body["messages"]
        if self.headers.get("x-api-key") != "sk-ant-fake-qa":
            self.problems.append("not the fake key")
        if body["model"] != "claude-opus-5-5" or "temperature" in body:
            self.problems.append(f"request {body['model']}")
        if "report_finding" not in names or "set_coverage" not in names:
            self.problems.append(f"tools {names}")

        def question(m: dict[str, Any]) -> bool:
            content = m["content"]
            if m["role"] != "user" or isinstance(content, str):
                return m["role"] == "user"
            words = " ".join(b.get("text", "") for b in content if b.get("type") == "text")
            no_results = not any(b.get("type") == "tool_result" for b in content)
            return bool(no_results and not words.startswith(COVERAGE_CHECK_PREFIX))

        start = max(i for i, m in enumerate(messages) if question(m))
        step = sum(1 for m in messages[start:] if m["role"] == "assistant")
        tag = re.search(r"#(\w+)", json.dumps(messages[start]["content"]))
        scripts = {"findings": _whole_set_script, "fast": _fast_script}
        chosen = scripts.get(tag.group(1)) if tag else None
        script = chosen() if chosen is not None else [reply(text("?"))]
        events = script[min(step, len(script) - 1)]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(_sse(events))
        self.wfile.flush()


def test_a_whole_set_answer_end_to_end_against_a_fake_anthropic_server(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import ChatRuntime, chat_runtime
    from app.review.assistant.chat_provider import AnthropicSdkProvider, ProviderChoice

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    _FakeAnthropic.requests, _FakeAnthropic.problems = [], []
    try:
        with _world(postgres_engine, tmp_path, pdf=PDF3, pages=3) as world:
            scripted = world.app.dependency_overrides[chat_runtime]()
            choice = ProviderChoice("anthropic", "sk-ant-fake-qa")
            world.app.dependency_overrides[chat_runtime] = lambda: ChatRuntime(
                choice=choice,
                provider=lambda: AnthropicSdkProvider(choice, timeout_seconds=30),
                open_sandbox=scripted.open_sandbox,
                skill_builder=scripted.skill_builder,
                rates=scripted.rates,
            )
            chat_id = world.chat()
            status, events = world.ask(chat_id, "Which items are wrong? #findings", **ASK)
            assert status == 200
            message = _named(events, "message_end")[0]["message"]
            assert message["stopped"] is None, message
            assert message["text"] == "One difference, on page 2."
            kinds = [block["type"] for block in message["blocks"]]
            assert kinds == ["finding", "coverage", "text", "text", "trust", "accuracy"]
            assert message["blocks"][1]["coverage"]["counts"]["not_opened"] == 0
            trust = message["blocks"][4]["trust"]
            assert trust["counts"]["total"] == 1 and trust["coverage"]["pages_not_opened"] == []
            assert len(_FakeAnthropic.requests) == 4 and _FakeAnthropic.problems == []
            made = world.client.post(f"/api/v1/chats/{chat_id}/messages/{message['id']}/markup-pdf")
            assert made.status_code == 200
            assert UUID(made.json()["id"])
    finally:
        server.shutdown()
        server.server_close()


def test_a_narrow_answer_asks_for_its_tools_at_once_end_to_end(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """#fast through the real SDK and route: three tools in one reply run together and go back in
    one message, in order; the zoom's two crops reach the model as pictures; two calls in all."""
    from app.api.assistant_chats import ChatRuntime, chat_runtime
    from app.review.assistant.chat_provider import AnthropicSdkProvider, ProviderChoice

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeAnthropic)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    _FakeAnthropic.requests, _FakeAnthropic.problems = [], []
    try:
        with _world(postgres_engine, tmp_path, pdf=PDF3, pages=3) as world:
            scripted = world.app.dependency_overrides[chat_runtime]()
            choice = ProviderChoice("anthropic", "sk-ant-fake-qa")
            world.app.dependency_overrides[chat_runtime] = lambda: ChatRuntime(
                choice=choice,
                provider=lambda: AnthropicSdkProvider(choice, timeout_seconds=30),
                open_sandbox=scripted.open_sandbox,
                skill_builder=scripted.skill_builder,
                rates=scripted.rates,
            )
            chat_id = world.chat()
            status, events = world.ask(
                chat_id, "Is the depth on page 2 the architect's? #fast", **ASK
            )
            assert status == 200
            message = _named(events, "message_end")[0]["message"]
            assert message["stopped"] is None, message
            assert len(_FakeAnthropic.requests) == 2 and _FakeAnthropic.problems == []
            second = _FakeAnthropic.requests[1]["messages"]
            results = second[-1]["content"]
            assert [r["tool_use_id"] for r in results] == ["b1", "z1", "r0"]
            zoomed = results[1]["content"]
            assert [b["type"] for b in zoomed] == ["text", "image", "text", "image"]
            assert all(b["source"]["type"] == "base64" for b in zoomed if b["type"] == "image")
            assert "drawings only" in zoomed[0]["text"]
            steps = [b["step"] for b in message["blocks"] if b["type"] == "step"]
            assert [step["tool"] for step in steps] == ["bash_tool", "view", "review_results"]
            assert steps[1]["label"] == "Read the two values" and steps[1]["picture_id"]
            assert (
                world.client.get(
                    f"/api/v1/chats/{chat_id}/pictures/{steps[1]['picture_id']}"
                ).content[:4]
                == b"\x89PNG"
            )
            tools = [t["name"] for t in _FakeAnthropic.requests[0]["tools"]]
            assert tools[:3] == ["bash_tool", "view", "zoom"]
    finally:
        server.shutdown()
        server.server_close()


def test_a_full_check_through_the_route_shows_its_label_and_ends_with_its_two_files(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One item is wrong.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "mode": "full_check", "products": ["countertop"]},
        )
        assert response.status_code == 200, response.text
        events = _events(response.text)
        message = _named(events, "message_end")[0]["message"]
        names = [f["name"] for f in message["files"]]
        assert names == ["Vendor set - AI cloud drawing.pdf", "Vendor set - AI check report.pdf"]
        assert [d["file"]["name"] for d in _named(events, "file")] == names
        assert message["accuracy"] is not None and message["accuracy"]["claims"] == 1
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        assert chat["messages"][0]["text"] == "Full check · Countertops · High effort"
        # Both the question and its answer say what they were (#1513).
        for stored in (*chat["messages"], message):
            assert stored["mode"] == "full_check" and stored["products"] == ["countertop"]
        assert chat["messages"][1]["accuracy"]["rescored"] is False
        sent = json.loads(world.provider.lead_calls[0])["messages"][0]["content"]
        words = " ".join(block.get("text", "") for block in sent)
        assert "Focus for this chat: countertops." in words
        assert "Full check of this drawing set" in words
        for file in message["files"]:
            assert world.client.get(file["url"]).content[:4] == b"%PDF"

        # A chat message still needs its words; a product at most once.
        empty = world.client.post(f"/api/v1/chats/{chat_id}/messages", json={**ASK, "text": " "})
        twice = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "text": "hi", "products": ["cabinet", "cabinet"]},
        )
        assert empty.status_code == 422 and twice.status_code == 422


def test_a_typed_question_keeps_its_mode_and_an_old_badge_is_scored_again(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """#1513: a typed question and its answer come back as `chat` with the products picked; an
    answer stored before the recalibration (here: the same answer with an old 78 and no mode) is
    scored again on read and by the follow route, and nothing in the database changes."""
    from sqlalchemy import select

    from app.models import AssistantChatMessage
    from app.review.assistant.chat_store import add_assistant_message

    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("One item looks wrong.")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "text": "Is the vanity top right?", "products": ["cabinet"]},
        )
        assert response.status_code == 200, response.text
        today = _named(_events(response.text), "message_end")[0]["message"]
        assert today["mode"] == "chat" and today["products"] == ["cabinet"]
        assert today["accuracy"]["rescored"] is False

        row = world.session.get(AssistantChatMessage, UUID(today["id"]))
        old_blocks = [
            (
                {
                    "type": "accuracy",
                    "accuracy": {**block["accuracy"], "score": 78, "level": "orange"},
                }
                if block.get("type") == "accuracy"
                else block
            )
            for block in row.blocks
        ]
        old = add_assistant_message(
            world.session,
            message_id=uuid4(),
            chat_id=row.chat_id,
            created_by="someone",
            model=row.model,
            effort=row.effort,
            thinking=row.thinking,
            text=row.text,
            blocks=old_blocks,
            api_content=[],
            cost_micros=0,
            stopped=None,
            error=None,
        )
        world.session.commit()

        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        question, answer, older = chat["messages"]
        assert question["mode"] == "chat" and question["products"] == ["cabinet"]
        assert answer["accuracy"]["score"] == today["accuracy"]["score"]
        assert older["mode"] is None and older["products"] is None
        assert older["accuracy"]["score"] == today["accuracy"]["score"] != 78
        assert older["accuracy"]["rescored"] is True
        follow = world.client.get(f"/api/v1/chats/{chat_id}/messages/{old.id}/events")
        followed = _named(_events(follow.text), "message_end")[0]["message"]
        assert followed["accuracy"] == older["accuracy"]

        # Read only: the stored block still says 78.
        world.session.expire_all()
        stored = world.session.scalars(
            select(AssistantChatMessage).where(AssistantChatMessage.id == old.id)
        ).one()
        assert next(b for b in stored.blocks if b["type"] == "accuracy")["accuracy"]["score"] == 78


def test_a_full_check_with_no_words_is_stored_with_codes_summary_and_its_progress(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(
        lead=[
            reply(tool("r1", "report_finding", finding_input()), stop="tool_use"),
            reply(text("")),
        ]
    )
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "mode": "full_check", "products": ["countertop"]},
        )
        assert response.status_code == 200, response.text
        events = _events(response.text)
        assert _named(events, "progress")[-1]["progress"]["phase"] == "writing"
        ended = _named(events, "message_end")[0]["message"]
        assert ended["text"].startswith("Checked ")
        assert "Summary written by GV Review" in ended["text"]
        # A reload shows the same: the summary block marked as code's, and the last progress.
        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][1]
        assert stored["text"] == ended["text"]
        assert stored["blocks"] == ended["blocks"]
        written = [b for b in stored["blocks"] if b["type"] == "text" and b.get("by") == "code"]
        assert len(written) == 1 and written[0]["text"] == ended["text"]
        progress = [b for b in stored["blocks"] if b["type"] == "progress"]
        assert len(progress) == 1 and progress[0]["progress"]["phase"] == "writing"
        assert [f["name"] for f in stored["files"]] == [
            "Vendor set - AI cloud drawing.pdf",
            "Vendor set - AI check report.pdf",
        ]
