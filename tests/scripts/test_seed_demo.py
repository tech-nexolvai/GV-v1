"""The seeded reviewer-input demo must arrive ready for review, not look perpetually busy."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

import pytest
from pytest import MonkeyPatch
from sqlalchemy.orm import Session

from app.models import PackageState
from extraction.reader import read_page_contents


class _DemoStages(Protocol):
    """The small stage surface the seed hand-off owns."""

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Record findings for the requested revision."""

        ...

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Build reviewer outputs for the requested revision."""

        ...


class _SeedDemoModule(Protocol):
    """The typed surface loaded dynamically from the executable seed script."""

    def _finish_seeded_review(
        self, session: Session, revision_id: UUID, stages: _DemoStages
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        """Run the seeded review through checking, output generation, and hand-off."""

        ...


def _seed_demo_module() -> _SeedDemoModule:
    """Load the executable script without treating ``scripts`` as an import package."""
    path = Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py"
    specification = importlib.util.spec_from_file_location("seed_demo_for_test", path)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return cast(_SeedDemoModule, module)


def test_seeded_demo_runs_checks_generates_outputs_then_enters_review(
    monkeypatch: MonkeyPatch,
) -> None:
    """The final state transition is after outputs, not merely after a successful check run."""
    module = _seed_demo_module()
    events: list[tuple[str, str]] = []

    def moved(
        session: object,
        revision_id: object,
        state: PackageState,
        *,
        actor: str,
        reason: str,
    ) -> None:
        """Record a lifecycle transition without requiring a database."""
        del session, revision_id, actor, reason
        events.append(("transition", state.value))

    import app.lifecycle.states

    monkeypatch.setattr(app.lifecycle.states, "transition", moved)

    class Stages:
        """A stage double that records the order in which the seed invokes it."""

        def run_checks(self, session: object, revision_id: object) -> Mapping[str, object]:
            """Record the deterministic check stage."""
            del session, revision_id
            events.append(("stage", "run_checks"))
            return {"findings": 9}

        def generate_outputs(self, session: object, revision_id: object) -> Mapping[str, object]:
            """Record the handoff-output stage."""
            del session, revision_id
            events.append(("stage", "generate_outputs"))
            return {"outputs": 2}

    result, outputs = module._finish_seeded_review(cast(Session, object()), uuid4(), Stages())

    assert result == {"findings": 9}
    assert outputs == {"outputs": 2}
    assert events == [
        ("stage", "run_checks"),
        ("transition", PackageState.GENERATING_OUTPUTS.value),
        ("stage", "generate_outputs"),
        ("transition", PackageState.AWAITING_REVIEW.value),
    ]


def test_seeded_demo_does_not_handoff_when_checks_fail(monkeypatch: MonkeyPatch) -> None:
    """A failed deterministic check cannot be presented to the reviewer as ready."""
    module = _seed_demo_module()
    events: list[str] = []

    def moved(*args: object, **kwargs: object) -> None:
        """Record an unexpected lifecycle transition."""
        del args, kwargs
        events.append("transition")

    import app.lifecycle.states

    monkeypatch.setattr(app.lifecycle.states, "transition", moved)

    class BrokenStages:
        """A failing check stage with an output stage that must remain unused."""

        def run_checks(self, session: object, revision_id: object) -> Mapping[str, object]:
            """Fail before any lifecycle hand-off can occur."""
            del session, revision_id
            events.append("run_checks")
            raise RuntimeError("check failure")

        def generate_outputs(self, session: object, revision_id: object) -> Mapping[str, object]:
            """Fail the test if output generation is attempted after a check failure."""
            del session, revision_id
            events.append("generate_outputs")
            raise AssertionError("generate_outputs must not run")

    with pytest.raises(RuntimeError, match="check failure"):
        module._finish_seeded_review(cast(Session, object()), uuid4(), BrokenStages())

    assert events == ["run_checks"]


def test_evidence_demo_fixture_is_a_synthetic_pdf_with_the_declared_reading() -> None:
    """The runnable demo's crop source is safe fixture data, not a copied client drawing."""
    module = cast(Any, _seed_demo_module())

    contents = read_page_contents(
        module.EVIDENCE_DRAWING,
        0,
        document_version_id=uuid4(),
        dpi=300,
    )

    assert module.EVIDENCE_DRAWING.startswith(b"%PDF-")
    assert module.EVIDENCE_LABEL == "641 [25 1/4]"
    assert [item.text for item in contents.texts] == [
        "641 [25 1/4]",
        "SYNTHETIC",
        "SHOP",
        "DEPTH",
    ]
