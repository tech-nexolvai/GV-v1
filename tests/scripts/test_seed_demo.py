"""The seeded reviewer-input demo must arrive ready for review, not look perpetually busy."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.models import PackageState


def _seed_demo_module() -> Any:
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
    specification = importlib.util.spec_from_file_location("seed_demo_for_test", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def test_seeded_demo_runs_checks_generates_outputs_then_enters_review(
    monkeypatch: Any,
) -> None:
    """A completed seed has to show the reviewer the truthful final lifecycle state."""
    module = _seed_demo_module()
    calls: list[tuple[PackageState, str]] = []

    def moved(
        session: object,
        revision_id: object,
        state: PackageState,
        *,
        actor: str,
        reason: str,
    ) -> None:
        del session, revision_id, actor
        calls.append((state, reason))

    import app.lifecycle.states

    monkeypatch.setattr(app.lifecycle.states, "transition", moved)

    class Stages:
        def run_checks(self, session: object, revision_id: object) -> Mapping[str, object]:
            del session, revision_id
            return {"findings": 9}

        def generate_outputs(self, session: object, revision_id: object) -> Mapping[str, object]:
            del session, revision_id
            return {"outputs": 2}

    result, outputs = module._finish_seeded_review(object(), uuid4(), Stages())

    assert result == {"findings": 9}
    assert outputs == {"outputs": 2}
    assert calls == [
        (PackageState.GENERATING_OUTPUTS, "the seeded reviewer inputs were checked"),
        (
            PackageState.AWAITING_REVIEW,
            "the seeded findings and handoff outputs are ready for reviewer sign-off",
        ),
    ]
