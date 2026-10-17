"""The plan check before a paid full check, through the routes (#1569).

`GET /packages/{id}/full-check-plan` reads the set's page map (the set pack is made from the real
tiny PDF on first ask) and asks only where it is unsure; the answers go with the full check and are
kept, append-only, beside the page map. Scripted provider: no paid call.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar

from app.review.assistant import finding_critic
from app.review.assistant.chat_tools import HELPER_SYSTEM_PROMPT, LEAD_SYSTEM_PROMPT
from app.review.assistant.plan_check import ANSWERS_FILE
from tests.api.test_assistant_chats import _events, _named, _world
from tests.api.test_assistant_chats_findings import ASK, PDF3
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import finding_input


def _plan(world: Any, wanted: str = "ready", seconds: float = 30) -> dict[str, Any]:
    url = f"/api/v1/packages/{world.package_id}/full-check-plan"
    ends = time.monotonic() + seconds
    while True:
        response = world.client.get(url)
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        if body["state"] == wanted or time.monotonic() > ends:
            return body
        time.sleep(0.2)


def _pack_folder(root: Path) -> Path:
    folders = [p for p in (root / "set-packs").iterdir() if p.is_dir()]
    assert len(folders) == 1
    return folders[0]


def _pictures_made(pack: Path, seconds: float = 60) -> None:
    """Waits for the pack's page pictures (made after the page map; slower on a busy machine)."""
    ends = time.monotonic() + seconds
    while time.monotonic() < ends:
        status = json.loads((pack / "status.json").read_text(encoding="utf-8"))
        if "pages" in (status.get("done") or []) or status.get("state") == "ready":
            return
        time.sleep(0.2)
    raise AssertionError("the page pictures were not made")


def _half(role: str, y: float, round_: int | None = None) -> dict[str, Any]:
    return {"box": [36, y, 576, y + 300], "role": role, "heading": None, "round": round_}


def test_the_plan_check_asks_where_unsure_and_keeps_the_answers(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    provider = ScriptedProvider(lead=[reply(text("Done."))])
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        # The first ask copies the PDFs aside and starts the set pack: code's plan, nothing to ask
        # on a set with no pasted drawings.
        first = _plan(world)
        assert first["state"] == "ready", first
        assert first["doubts"] == [] and first["summary"] == "3 pages · 0 pairs · 3 general pages"

        # A page map with one unsure drawing: p2's lower drawing is headed as a brand prototype
        # and no architect's drawing of its area was found.
        pack = _pack_folder(world.root)
        _pictures_made(pack)
        page_map = json.loads((pack / "page_map.json").read_text(encoding="utf-8"))
        entry = page_map["pages"][1]
        entry.update(
            {
                "area": "1. Coffee point",
                "section": "1. Kitchens",
                "halves": [_half("vendor", 0, 1), _half("brand_prototype", 320)],
                "role": "brand_prototype+vendor",
            }
        )
        (pack / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")

        plan = _plan(world)

        assert plan["more"] == 0 and len(plan["doubts"]) == 1
        doubt = plan["doubts"][0]
        assert doubt["id"] == "prototype:f0:p2:h1" and doubt["kind"] == "prototype"
        assert doubt["question"].startswith("Page 2: is the lower drawing the brand prototype")
        assert [o["value"] for o in doubt["options"]] == ["brand_prototype", "architect"]
        assert doubt["picture_url"] == f"/api/v1/packages/{world.package_id}/plan-pages/2"
        assert doubt["box"] is not None and all(0 <= v <= 1 for v in doubt["box"])
        picture = world.client.get(doubt["picture_url"])
        assert picture.status_code == 200 and picture.content[:4] == b"\x89PNG"
        assert (
            world.client.get(f"/api/v1/packages/{world.package_id}/plan-pages/99").status_code
            == 404
        )

        # The answers go with the full check, and the question is not asked again.
        chat_id = world.chat()
        bad = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={
                **ASK,
                "mode": "full_check",
                "plan_answers": [{"id": doubt["id"], "answer": "pair:3"}],
            },
        )
        assert bad.status_code == 422
        not_full = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={
                **ASK,
                "text": "hi",
                "plan_answers": [{"id": doubt["id"], "answer": "architect"}],
            },
        )
        assert not_full.status_code == 422
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={
                **ASK,
                "mode": "full_check",
                "products": ["countertop"],
                "plan_answers": [{"id": doubt["id"], "answer": "architect"}],
            },
        )
        assert response.status_code == 200, response.text
        assert _named(_events(response.text), "message_end")
        rows = [json.loads(line) for line in (pack / ANSWERS_FILE).read_text().splitlines()]
        assert [(row["id"], row["answer"], row["by"]) for row in rows] == [
            (doubt["id"], "architect", "reviewer-one")
        ]
        assert _plan(world)["doubts"] == []
        # ... and as the set's notes, for later chats (#1548).
        from app.models import AssistantSetNote

        notes = world.session.query(AssistantSetNote).filter_by(package_id=world.package_id).all()
        assert [(n.note, n.page, n.source) for n in notes] == [
            ("Plan check: on page 2, drawing 2 is: architect's drawing.", 2, "decision")
        ]
        # The page map on disk is not changed: the answers are laid over it when it is read.
        assert json.loads((pack / "page_map.json").read_text(encoding="utf-8")) == page_map

    with _world(postgres_engine, tmp_path / "b", member=False) as outsider:
        url = f"/api/v1/packages/{world.package_id}/full-check-plan"
        assert outsider.client.get(url).status_code == 404
        assert (
            outsider.client.get(url.replace("full-check-plan", "plan-pages/2")).status_code == 404
        )


# ---- a two-lens full check, end to end against a fake Anthropic server ---------------------------


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def _words(content: Any) -> str:
    if isinstance(content, str):
        return content
    return " ".join(b.get("text", "") for b in content if isinstance(b, dict))


class _FakeLenses(BaseHTTPRequestHandler):
    """A full check with two helpers per page group, answered by role: the warm-up, the numbers
    and drawn-things helpers (each reports the same top, the drawn one a drawer note too), the
    second and third reads (agree), the critic (passes) and the lead (a short note)."""

    requests: ClassVar[list[dict[str, Any]]] = []
    problems: ClassVar[list[str]] = []

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def helper(self, body: dict[str, Any], brief: str) -> list[dict[str, Any]]:
        model = body["model"]
        drawn = brief.startswith("OBJECTIVE: the DRAWN THINGS")
        pages = [int(n) for n in brief.split("on pages ", 1)[1].split(" of ", 1)[0].split(", ")]
        step = sum(1 for m in body["messages"] if m["role"] == "assistant")
        if step == 0 and body.get("tool_choice") != {"type": "none"}:
            top = {**finding_input(item="Coffee point: top width"), "page": 1}
            top["vendor_box"] = {**top["vendor_box"], "page": 1}
            top["arch_box"] = {**top["arch_box"], "page": 1}
            calls = [tool(f"t{drawn}", "report_finding", top)]
            if drawn:
                note = {
                    **top,
                    "item": "Coffee point: bin drawers note",
                    "architect": "pull-out bin drawers (note)",
                    "vendor": "2 doors drawn",
                }
                calls.append(tool("n1", "report_finding", note))
            else:
                covered = {"pages": [{"page": p, "status": "different"} for p in pages]}
                calls.append(tool("c1", "set_coverage", covered))
            return reply(*calls, stop="tool_use", model=model)
        lines = "\n".join(
            f"CHECKLIST p{p}: architect 1/1; strings 0/0; notes 1/1; counts 1/1; hardware 0/0; "
            "not compared: none"
            for p in pages
        )
        return reply(text(f"Checked.\n{lines}"), model=model)

    def do_POST(self) -> None:
        body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)))
        self.requests.append(body)
        system = body["system"][0]["text"]
        first = _words(body["messages"][0]["content"])
        model = body["model"]
        if system == HELPER_SYSTEM_PROMPT and first == "Reply with OK.":
            events = reply(text("OK"), model=model, stop="max_tokens")
        elif system == HELPER_SYSTEM_PROMPT:
            events = self.helper(body, first)
        elif system == LEAD_SYSTEM_PROMPT:
            if model != "claude-opus-5-5":
                self.problems.append(f"lead on {model}")
            events = reply(
                text("The top width differs; the drawer note is not followed."), model=model
            )
        elif system == finding_critic.SYSTEM:
            ids = re.findall(r"Finding (F\d+)", json.dumps(body["messages"]))
            verdict = {
                "findings": [
                    {
                        "id": i,
                        "same_item": "yes",
                        "right_dimension": "yes",
                        "vendor_ink": "yes",
                        "architect_side": "yes",
                        "note": "",
                    }
                    for i in dict.fromkeys(ids)
                ]
            }
            events = reply(text(json.dumps(verdict)), model=model)
        else:
            answer = {"architect": "3'-1\"", "vendor": "3'-10\"", "sure": True}
            events = reply(text(json.dumps(answer)), model=model)
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("connection", "close")
        self.end_headers()
        self.wfile.write(_sse(events))
        self.wfile.flush()


def test_a_two_lens_full_check_end_to_end_against_a_fake_anthropic_server(postgres_engine, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import ChatRuntime, chat_runtime
    from app.review.assistant.chat_provider import AnthropicSdkProvider, ProviderChoice

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLenses)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}")
    _FakeLenses.requests, _FakeLenses.problems = [], []
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
            # The set pack, then a page map with two areas and one unsure drawing on p2.
            assert _plan(world)["state"] == "ready"
            pack = _pack_folder(world.root)
            page_map = json.loads((pack / "page_map.json").read_text(encoding="utf-8"))
            for entry, area, halves in (
                (
                    page_map["pages"][0],
                    "1. Coffee point",
                    [_half("architect", 0), _half("vendor", 320, 1)],
                ),
                (
                    page_map["pages"][1],
                    "2. Bar",
                    [_half("vendor", 0, 1), _half("brand_prototype", 320)],
                ),
            ):
                roles = sorted({h["role"] for h in halves})
                entry.update(
                    {
                        "area": area,
                        "section": "1. Kitchens",
                        "halves": halves,
                        "role": "+".join(roles),
                    }
                )
            (pack / "page_map.json").write_text(json.dumps(page_map), encoding="utf-8")
            doubt = _plan(world)["doubts"][0]
            assert doubt["id"] == "prototype:f0:p2:h1"

            chat_id = world.chat()
            response = world.client.post(
                f"/api/v1/chats/{chat_id}/messages",
                json={
                    **ASK,
                    "mode": "full_check",
                    "products": ["countertop"],
                    "plan_answers": [{"id": doubt["id"], "answer": "architect"}],
                },
            )

            assert response.status_code == 200, response.text
            message = _named(_events(response.text), "message_end")[0]["message"]
            assert message["stopped"] is None, message
            assert _FakeLenses.problems == []
            # Two helpers on pages 1-2 (p3 is a divider): the numbers and the drawn things.
            briefs = [
                r
                for r in _FakeLenses.requests
                if r["system"][0]["text"] == HELPER_SYSTEM_PROMPT
                and not any(m["role"] == "assistant" for m in r["messages"])
                and _words(r["messages"][0]["content"]) != "Reply with OK."
            ]
            assert len(briefs) == 2
            drawn = next(
                r
                for r in briefs
                if _words(r["messages"][0]["content"]).startswith("OBJECTIVE: the DRAWN THINGS")
            )
            assert drawn["model"] == "claude-sonnet-5-5" and drawn["output_config"] == {
                "effort": "low"
            }
            # The reviewer's answer reached the helpers' briefs.
            assert "(the reviewer said so in the plan check)" in _words(
                drawn["messages"][0]["content"]
            )
            # The two helpers' same top was folded; the drawn-things helper's note stays.
            findings = [b["finding"] for b in message["blocks"] if b["type"] == "finding"]
            items = sorted(f["item"] for f in findings)
            assert items.count("Coffee point: top width") == 1
            assert "Coffee point: bin drawers note" in items
            dropped = [b["reason"] for b in message["blocks"] if b["type"] == "finding_dropped"]
            assert any("both helpers of this page reported it" in reason for reason in dropped)
            # Biggest first: every Wrong item carries its impact, ranked.
            wrong = [f for f in findings if f["status"] == "different"]
            assert sorted(f["impact"]["rank"] for f in wrong) == list(range(1, len(wrong) + 1))
            assert [f["name"] for f in message["files"]] == [
                "Vendor set - GV cloud drawing.pdf",
                "Vendor set - AI check report.pdf",
            ]
            rows = (pack / ANSWERS_FILE).read_text(encoding="utf-8").splitlines()
            assert [json.loads(row)["answer"] for row in rows] == ["architect"]
    finally:
        server.shutdown()
        server.server_close()
