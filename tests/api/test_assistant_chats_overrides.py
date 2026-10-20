"""The reviewer's overrides on a full check's result (#1803), through the app on a real PostgreSQL.

On a Not-compared item the screen offers "It's a mistake: add a red mark" (an Agree with the value
it should be) and "It's fine: mark Right" (Not a problem); on a Wrong item "Not a mistake: remove
the red mark" (Not a problem). Pinned here:

- the reviewer's "should be" is stored with the decision and comes back on the finding;
- it makes a Not-compared item Wrong even where the architect prints no value, with the green
  "should be" marked "(reviewer)" and the proof saying it is the reviewer's decision, never the
  reviewer's login;
- the remade files (cloud drawing, Not-compared PDF and workbook, package) follow the decisions:
  a decided item leaves the Not-compared list, the new red is clouded with its proof entry.

No paid call is possible: the provider is scripted; the sandbox is a fake.
"""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest
from pypdf import PdfReader

from app.review.assistant import chat_engine, chat_trust
from tests.api.test_assistant_chats import _events, _named, _world
from tests.api.test_assistant_chats_findings import _red_near
from tests.review.assistant.chat_fakes import ScriptedProvider, reply, text, tool
from tests.review.assistant.test_chat_findings import VENDOR_BOX, finding_input, make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
PAGE3_BOX = {**VENDOR_BOX, "page": 3}
ASK = {"model": "claude-opus-5-5", "effort": "high", "thinking": True}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _provider() -> ScriptedProvider:
    """F1: a difference only the AI read (Not compared); F2 on page 3: the architect gives no
    size (Not compared)."""
    return ScriptedProvider(
        lead=[
            reply(
                tool("r1", "report_finding", finding_input()),
                tool(
                    "r2",
                    "report_finding",
                    finding_input(
                        item="Sink base width",
                        status="check",
                        architect="no size printed",
                        vendor='33"',
                        page=3,
                        vendor_box=dict(PAGE3_BOX),
                        why="The architect prints no width for this cabinet",
                    ),
                ),
                stop="tool_use",
            ),
            reply(text("Two items could not be compared.")),
        ]
    )


def _decide(world: Any, chat_id: str, message_id: str, finding_id: str, **body: Any) -> Any:
    return world.client.post(
        f"/api/v1/chats/{chat_id}/messages/{message_id}/findings/{finding_id}/decision", json=body
    )


def _pdf_text(pdf: bytes) -> str:
    return " ".join(
        " ".join((page.extract_text() or "").split()) for page in PdfReader(io.BytesIO(pdf)).pages
    )


def _newest(files: list[dict[str, Any]], ending: str) -> dict[str, Any]:
    return [file for file in files if file["name"].endswith(ending)][-1]


def test_a_not_compared_item_made_wrong_with_the_reviewers_value_and_one_marked_right(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, provider=_provider(), pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={**ASK, "mode": "full_check", "products": ["countertop"]},
        )
        assert response.status_code == 200, response.text
        answer = _named(_events(response.text), "message_end")[0]["message"]
        found = {b["finding"]["id"]: b["finding"] for b in answer["blocks"] if b["type"] == "finding"}
        assert {f["proof"]["group"] for f in found.values()} == {"not_compared"}
        first_list = _pdf_text(
            world.client.get(_newest(answer["files"], "Not compared.pdf")["url"]).content
        )
        assert "Sink base width" in first_list and "Vanity top length" in first_list

        # A "should be" goes with an Agree only; an empty one is no value.
        refused = _decide(world, chat_id, answer["id"], "F1", decision="not_a_problem", should_be='5"')
        assert refused.status_code == 422
        # It's a mistake: add a red mark, with the value it should be (the architect gives none).
        made = _decide(
            world, chat_id, answer["id"], "F2", decision="agree", should_be=' 34 1/2" ', note="Hinge side"
        )
        assert made.status_code == 201, made.text
        assert made.json()["should_be"] == '34 1/2"' and made.json()["note"] == "Hinge side"
        # It's fine: mark Right.
        right = _decide(world, chat_id, answer["id"], "F1", decision="not_a_problem", note="Field fit")
        assert right.status_code == 201 and right.json().get("should_be") is None

        stored = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]
        now = {b["finding"]["id"]: b["finding"] for b in stored["blocks"] if b["type"] == "finding"}
        assert now["F2"]["decision"]["should_be"] == '34 1/2"'
        assert now["F2"]["proof"]["group"] == "clouded"
        assert now["F2"]["proof"]["should_be"] == '34 1/2" (reviewer)'
        assert now["F1"]["proof"]["group"] == "dismissed"

        # The files are made again from the decisions.
        remade = world.client.post(
            f"/api/v1/chats/{chat_id}/messages/{answer['id']}/cloud-drawing"
        )
        assert remade.status_code == 200, remade.text
        files = world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"][-1]["files"]
        assert len(files) == 2 * len(answer["files"])
        drawing = world.client.get(remade.json()["url"]).content
        words = _pdf_text(drawing)
        assert 'Should be: 34 1/2" (reviewer)' in words
        assert "Why: Decided by the reviewer: Hinge side" in words
        assert "reviewer-one" in words  # the stamp names who reviewed it
        assert "Agreed by" not in words
        assert _red_near(drawing, 4, PAGE3_BOX) > 5  # page 1 is the cover

        # The Not-compared list is made again: both decided items leave it.
        listed = _newest(files, "Not compared.pdf")
        assert listed["id"] not in {f["id"] for f in answer["files"]}
        remade_list = _pdf_text(world.client.get(listed["url"]).content)
        assert "Sink base width" not in remade_list and "Vanity top length" not in remade_list
        sheet = _newest(files, "Not compared.xlsx")
        assert sheet["id"] not in {f["id"] for f in answer["files"]}

        # The package holds the new list and the new red, numbered.
        package = _newest(files, "review package.zip")
        with zipfile.ZipFile(io.BytesIO(world.client.get(package["url"]).content)) as archive:
            inside = archive.namelist()
            letter = _pdf_text(archive.read(next(n for n in inside if "Comment letter" in n)))
            listed_inside = _pdf_text(archive.read(next(n for n in inside if n.endswith("Not compared.pdf"))))
        assert "C-01" in letter and "Sink base width" in letter and '34 1/2"' in letter
        assert "reviewer-one" not in letter.split("reviewed by")[0]
        assert "Sink base width" not in listed_inside
