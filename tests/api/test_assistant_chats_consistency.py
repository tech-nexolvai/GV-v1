"""The stand-alone vendor consistency check (#1584) through the app on a real PostgreSQL: code only,
no model call ($0), stored as a question and its answer that reload the same.

The check itself is scripted here (its geometry is tested with synthetic sheets in
`tests/review/assistant/test_vendor_consistency.py`); the provider is scripted and never called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.api import assistant_chats
from app.review.assistant import chat_engine, chat_trust
from app.review.assistant import consistency_answer as answer
from app.review.assistant.vendor_consistency import ConsistencyResult
from tests.api.test_assistant_chats import _world
from tests.review.assistant.chat_fakes import ScriptedProvider
from tests.review.assistant.test_chat_findings import make_pdf

pytest_plugins = ("tests.app.postgres_fixture",)

PDF3 = make_pdf(3)
STRING = {
    "page": 2,
    "item": "Buffet: dimension string (across, round 2)",
    "category": "other",
    "architect": '61"',
    "vendor": '60"',
    "status": "different",
    "why": 'The vendor\'s parts don\'t add up: 20" + 41" = 61" ≠ 60" printed (off 1").',
    "verified": False,
    "source": "vendor_arithmetic",
    "parts": ['20"', '41"'],
    "printed_total": '60"',
    "vendor_box": {"page": 2, "x0": 40, "y0": 40, "x1": 200, "y1": 90},
    "sum_text": '20" + 41" = 61" ≠ 60" printed (off 1")',
    "created_by": "consistency",
    "ledger_ids": ["p2.D3", "p2.D5", "p2.D6"],
}
GAP = {
    "page": 3,
    "what": "Vanity: round 2 does not include this area (round 1 did, p.3)",
    "why": "missing_round",
    "needs": "the vendor's drawing of this area for that round",
    "scope": "set",
}


@pytest.fixture(autouse=True)
def _no_checks_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_trust, "load_module", lambda name: None)
    monkeypatch.setattr(chat_engine, "load_module", lambda name: None)


def _scripted(monkeypatch: pytest.MonkeyPatch, result: ConsistencyResult | None) -> None:
    def run(workspace: Any, listing: Any) -> answer.ConsistencyAnswer:
        return answer.run_check(workspace, listing, check=lambda folder, uploads: result)

    monkeypatch.setattr(assistant_chats, "run_check", run)


def test_the_check_is_stored_as_an_answer_by_code_with_no_model_call(postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    found = ConsistencyResult(
        findings=[STRING],
        unknowns=[GAP],
        strings={"found": 3, "adds_up": 2, "does_not_add_up": 1, "doubt": 0},
    )
    _scripted(monkeypatch, found)
    provider = ScriptedProvider()
    with _world(postgres_engine, tmp_path, provider=provider, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(f"/api/v1/chats/{chat_id}/consistency")
        assert response.status_code == 200, response.text
        stored = response.json()
        assert stored["model"] == "code-check" and stored["cost_usd"] == "0.00"
        kinds = [block["type"] for block in stored["blocks"]]
        assert kinds == ["text", "finding", "unknowns", "trust", "accuracy"]
        assert stored["blocks"][0]["text"].startswith("Vendor consistency (by code, $0)")
        finding = stored["blocks"][1]["finding"]
        assert finding["id"] == "F1" and finding["created_by"] == "consistency"
        assert finding["checks"]["arithmetic"] in (None, "does_not_add_up")
        assert finding["vendor_picture_id"]
        assert stored["blocks"][2]["unknowns"] == [{"id": "U1", **GAP}]
        # No model was asked.
        assert provider.lead_calls == []
        chat = world.client.get(f"/api/v1/chats/{chat_id}").json()
        question, reloaded = chat["messages"][-2:]
        assert question["role"] == "user" and question["text"] == "Vendor consistency check"
        assert reloaded == stored
        # Its findings download like any answer's.
        csv = world.client.get(f"/api/v1/chats/{chat_id}/messages/{stored['id']}/findings.csv")
        assert csv.status_code == 200 and '60"' in csv.text


def test_a_set_still_being_read_is_told_plainly(postgres_engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    _scripted(monkeypatch, None)
    with _world(postgres_engine, tmp_path, pdf=PDF3, pages=3) as world:
        chat_id = world.chat()
        response = world.client.post(f"/api/v1/chats/{chat_id}/consistency")
        assert response.status_code == 409
        assert response.json()["error"] == "pack_building"
        assert world.client.get(f"/api/v1/chats/{chat_id}").json()["messages"] == []


def test_another_chats_check_is_not_found(postgres_engine, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    with _world(postgres_engine, tmp_path, pdf=PDF3, pages=3) as world:
        missing = world.client.post(
            "/api/v1/chats/00000000-0000-0000-0000-000000000000/consistency"
        )
        assert missing.status_code == 404
