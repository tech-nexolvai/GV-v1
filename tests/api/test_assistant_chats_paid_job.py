"""One paid full check at a time across chats, and the set's one meter with the worker's spend
(#1770), through the app on a real PostgreSQL. No paid call is possible: the provider is a fake."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID, uuid4

from tests.api.test_assistant_chats import DEFAULT, _world

pytest_plugins = ("tests.app.postgres_fixture",)


def test_a_full_check_while_another_chat_runs_one_is_a_409(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.api.assistant_chats import chat_registry

    with _world(postgres_engine, tmp_path) as world:
        running_chat, chat_id = world.chat(), world.chat()

        class _Request:
            app = world.app

        registry = chat_registry(_Request())  # type: ignore[arg-type]
        other = registry.claim(UUID(running_chat), world.package_id, uuid4(), paid=True)
        assert other is not None
        other.started = True
        response = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "Full check", **DEFAULT, "mode": "full_check"},
        )
        assert response.status_code == 409
        assert response.json()["error"] == "paid_job_running"
        assert "Only one full check runs at a time" in response.json()["message"]
        assert world.provider.calls == []
        registry.release(other)


def test_the_set_meter_adds_the_workers_recorded_spend(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    from app.models import ModelInvocation

    with _world(postgres_engine, tmp_path) as world:
        for prompt, cost in (
            ("reading-made-up-v1", 1_500_000),
            ("review-assistant-chat-x", 250_000),
        ):
            world.session.add(
                ModelInvocation(
                    package_revision_id=world.revision_id,
                    model_id="synthetic.model",
                    prompt_id=prompt,
                    template_id="t",
                    input_tokens=10,
                    output_tokens=10,
                    cost_micros=cost,
                    latency_ms=1,
                    outcome="ok",
                )
            )
        world.session.commit()
        models = world.client.get(f"/api/v1/assistant/models?package_id={world.package_id}").json()
    assert models["worker_spent_usd"] == "1.50"
    chats = Decimal(models["package_spent_usd"])
    assert chats == Decimal("0.25")  # the chat row: the chats' own meter, as before
    assert Decimal(models["set_spent_usd"]) == chats + Decimal("1.50")


def test_a_saved_run_replayed_at_no_cost_is_not_a_paid_job(postgres_engine, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A replay of a saved run makes no model call ($0), so another chat's paid full check does not
    refuse it; "Compare them again" (`compare_missing`) does pay, so it is refused."""
    from app.api.assistant_chats import chat_registry

    with _world(
        postgres_engine, tmp_path, settings={"assistant_full_check_mode": "simple"}
    ) as world:
        running_chat, chat_id = world.chat(), world.chat()

        class _Request:
            app = world.app

        registry = chat_registry(_Request())  # type: ignore[arg-type]
        other = registry.claim(UUID(running_chat), world.package_id, uuid4(), paid=True)
        assert other is not None
        other.started = True
        again = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={
                "text": "",
                **DEFAULT,
                "mode": "full_check",
                "replay": "run-1",
                "compare_missing": True,
            },
        )
        assert again.status_code == 409 and again.json()["error"] == "paid_job_running"
        replayed = world.client.post(
            f"/api/v1/chats/{chat_id}/messages",
            json={"text": "", **DEFAULT, "mode": "full_check", "replay": "run-1"},
        )
        assert replayed.status_code == 200, replayed.text
        assert world.provider.calls == []
        registry.release(other)
