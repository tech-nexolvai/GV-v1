"""One stage factory for every runner (#1227).

The local outbox consumer (`scripts/drain_outbox.py`) and the Hatchet worker
(`workflow/entrypoints.py:run_worker`) used to build `DatabaseStages` each in their own way, and the
Hatchet one had fallen 11 arguments behind. Both now build it in one place, `workflow/sutradhar.py`.

What these tests pin:

- **The local runner is unchanged.** The arguments it gives `DatabaseStages` for a stated, made-up
  environment are pinned here, written against main before the move. (The same comparison on every
  local kit environment is a $0 proof kept outside the repository, because those values are local.)
- **The Hatchet worker builds the same arguments** as the local runner, except the store, the
  timings and the discriminators, which belong to each run rather than to the deployment.
- **Only the factory constructs `DatabaseStages`** among the runners.
- **A launcher that patches a reader's factory still reaches the stages**, because the factory
  imports its readers when it runs, not when it is imported.

No database, no engine and no model: `DatabaseStages` is replaced by a recorder.

Verification: this file
"""

from __future__ import annotations

import ast
import logging
import os
import runpy
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]

#: Made-up values for every stated setting, so each optional route is on and its settings visible.
SYNTHETIC_ENVIRONMENT: dict[str, str] = {
    "GV_DATABASE_URL": "postgresql+psycopg://gv:gv@localhost:5434/never-opened",
    "GV_LOCALIZED_OCR_ENABLED": "1",
    "GV_READER_LINE_MINIMUM_PT": "7",
    "GV_READER_GLYPH_MAXIMUM_PT": "8",
    "GV_READER_GLYPH_GAP_PT": "5",
    "GV_READER_PROXIMITY_LIMIT": "0.06",
    "GV_READER_AMBIGUITY_MARGIN": "0.007",
    "GV_READER_WITNESS_TOLERANCE": "0.003",
    "GV_READER_MINIMUM_SPAN": "0.02",
    "GV_READER_STRAIGHTNESS": "0.0004",
    "GV_READER_CROSSING_MARGIN": "0.0006",
    "GV_READER_LOCALIZED_MINIMUM_PATHS": "2",
    "GV_READER_LOCALIZED_MAXIMUM_SPAN": "0.4",
    "GV_READER_LOCALIZED_CROP_MARGIN_PT": "3",
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "0.25",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "2",
    "GV_READER_FRACTION_REACH_PT": "4",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "1.5",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "11",
    "GV_READER_FRACTION_PROPORTION_MAX": "2.25",
    "GV_READER_FRACTION_CHARACTER_GAP_PT": "3.5",
    "GV_READER_FRACTION_TURNED_ASPECT_MIN": "1.2",
    "GV_READER_MISSING_SPACE_HEIGHTS": "0.15",
    "GV_PART_PICTURE_MARGIN_PT": "40",
    "GV_PART_PICTURE_DPI": "120",
    "GV_AUTOMATIC_TYPES": "CT001, CT002",
    "GV_VISION_GATE_READER": "synthetic-gate",
    "GV_VISION_SHARPER_PICTURE_DPI": "600",
    "GV_ARCHITECT_READER_ENABLED": "true",
    "GV_AI_BUDGET_PER_PAGE_USD": "0.20",
    "GV_AI_BUDGET_MARGIN_USD": "2.00",
    "GV_AI_BUDGET_MINIMUM_USD": "4.00",
    "GV_AI_BUDGET_MAXIMUM_USD": "30.00",
    "GV_BEDROCK_CHAT_ENABLED": "false",
    "GV_FRACTION_PARTS": "0",
    "GV_READING_AGENT": "0",
}

#: The fewest settings a worker starts with: everything optional left off.
MINIMAL_ENVIRONMENT: dict[str, str] = {
    "GV_DATABASE_URL": "postgresql+psycopg://gv:gv@localhost:5434/never-opened",
    "GV_READER_MISSING_SPACE_HEIGHTS": "0.15",
    "GV_BEDROCK_CHAT_ENABLED": "false",
}


def _environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stated: dict[str, str]) -> Path:
    """Exactly `stated`, from an empty directory, so no `.env` and no leftover setting leaks in."""
    monkeypatch.chdir(tmp_path)
    for name in list(os.environ):
        if name.startswith("GV_") or name in {"OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"}:
            monkeypatch.delenv(name)
    for name, value in stated.items():
        monkeypatch.setenv(name, value)
    store = tmp_path / "store"
    monkeypatch.setenv("GV_DEV_STORAGE", str(store))
    return store


class _Recorder:
    """Stands in for `DatabaseStages` and keeps what each construction was given."""

    calls: list[tuple[tuple[object, ...], dict[str, object]]]

    def __init__(self) -> None:
        self.calls = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append((args, dict(kwargs)))
        return object()


def _record_stages(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    from workflow import stages

    recorder = _Recorder()
    monkeypatch.setattr(stages, "DatabaseStages", recorder)
    return recorder


def _expected_synthetic_arguments() -> dict[str, object]:
    """What main's local worker gave `DatabaseStages` for `SYNTHETIC_ENVIRONMENT` (written first)."""
    from app.evidence.automatic_typing import AutomaticTypingSettings
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
    from extraction.glyph_bands import FractionBarGeometry
    from extraction.reader import MissingSpace
    from vocabulary.semantic_types import SemanticType
    from workflow.ai_budget import AiBudget
    from workflow.association import AssociationSettings, LocalizedOcrSettings
    from workflow.part_pictures import PartPictureSettings
    from workflow.reader_pictures import PictureSettings

    return {
        "operands": None,
        "discriminators": {},
        "discriminator_sources": {},
        "association": AssociationSettings(
            line_minimum_pt=Decimal(7),
            glyph_maximum_pt=Decimal(8),
            glyph_gap_pt=Decimal(5),
            proximity_limit=Decimal("0.06"),
            ambiguity_margin=Decimal("0.007"),
            witness_tolerance=Decimal("0.003"),
            minimum_span=Decimal("0.02"),
            straightness=Decimal("0.0004"),
            crossing_margin=Decimal("0.0006"),
            fraction_bar=FractionBarGeometry(
                bar_thickness_max_pt=Decimal("0.25"),
                bar_length_min_pt=Decimal(2),
                reach_pt=Decimal(4),
                glyph_min_pt=Decimal("1.5"),
                glyph_max_pt=Decimal(11),
                proportion_max=Decimal("2.25"),
                character_gap_pt=Decimal("3.5"),
                turned_aspect_min=Decimal("1.2"),
            ),
        ),
        "localized_ocr": LocalizedOcrSettings(
            minimum_paths=2, maximum_span=Decimal("0.4"), crop_margin_pt=Decimal(3)
        ),
        "automatic_typing": AutomaticTypingSettings(
            frozenset({SemanticType.CT001, SemanticType.CT002})
        ),
        "findings_composer": None,
        "glyph_route": None,
        "reading_agent": None,
        "vision_gate": "synthetic-gate",
        "fraction_parts": None,
        "timings": None,
        "reader_pictures": PictureSettings(sharper_dpi=600),
        "missing_space": MissingSpace(gap_heights=Decimal("0.15")),
        "form_reader": None,
        "slot_reader": None,
        "part_pictures": PartPictureSettings(margin_pt=Decimal(40), dpi=120),
        "architect_reader": MEASURED_ARCHITECT_SETTINGS,
        "ai_budget": AiBudget(
            per_page_usd=Decimal("0.20"),
            margin_usd=Decimal("2.00"),
            minimum_usd=Decimal("4.00"),
            maximum_usd=Decimal("30.00"),
        ),
    }


def _expected_minimal_arguments() -> dict[str, object]:
    from extraction.reader import MissingSpace
    from workflow.ai_budget import AiBudget

    return {
        "operands": None,
        "discriminators": {},
        "discriminator_sources": {},
        "association": None,
        "localized_ocr": None,
        "automatic_typing": None,
        "findings_composer": None,
        "glyph_route": None,
        "reading_agent": None,
        "vision_gate": None,
        "fraction_parts": None,
        "timings": None,
        "reader_pictures": None,
        "missing_space": MissingSpace(gap_heights=Decimal("0.15")),
        "form_reader": None,
        "slot_reader": None,
        "part_pictures": None,
        "architect_reader": None,
        "ai_budget": AiBudget(),
    }


# ---------------------------------------------------------------------------
# The local runner is unchanged (golden, written against main first)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("stated", "expected"),
    [
        (SYNTHETIC_ENVIRONMENT, _expected_synthetic_arguments),
        (MINIMAL_ENVIRONMENT, _expected_minimal_arguments),
    ],
    ids=["every-route-stated", "minimal"],
)
def test_the_local_worker_builds_the_same_stage_arguments_as_before(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    stated: dict[str, str],
    expected: Any,
) -> None:
    import scripts.drain_outbox as worker
    from storage.local import LocalStore

    store_root = _environment(monkeypatch, tmp_path, stated)
    recorder = _record_stages(monkeypatch)

    worker._stages()

    ((args, kwargs),) = recorder.calls
    # The store is the one positional argument: the configured root, and a ticket secret that is
    # never used to issue a ticket.
    (store,) = args
    assert isinstance(store, LocalStore)
    assert store.root == store_root.resolve()
    assert store._ticket_secret == b"local-review-worker-never-issues-tickets"
    assert kwargs == expected()
    assert list(kwargs) == list(expected()), "the same arguments, in the same order"


def test_run_specific_arguments_reach_the_local_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discriminators, their sources and the timing recorder are passed through, copied."""
    import scripts.drain_outbox as worker

    _environment(monkeypatch, tmp_path, MINIMAL_ENVIRONMENT)
    recorder = _record_stages(monkeypatch)
    stated = {"wall_config": "back_only"}
    sources = {"wall_config": "readers_agreed"}
    timings = object()

    worker._stages(
        discriminators=stated,
        discriminator_sources=sources,
        timings=timings,  # type: ignore[arg-type]
    )

    ((_args, kwargs),) = recorder.calls
    assert kwargs["discriminators"] == stated and kwargs["discriminators"] is not stated
    assert kwargs["discriminator_sources"] == sources
    assert kwargs["discriminator_sources"] is not sources
    assert kwargs["timings"] is timings


@pytest.mark.parametrize(
    ("unset", "message"),
    [
        ("GV_READER_FRACTION_TURNED_ASPECT_MIN", "localized OCR is enabled but missing"),
        ("GV_PART_PICTURE_DPI", "how it is cut has no default; missing GV_PART_PICTURE_DPI"),
        ("GV_READER_MISSING_SPACE_HEIGHTS", "GV_READER_MISSING_SPACE_HEIGHTS is not stated"),
    ],
)
def test_a_partial_configuration_is_refused_with_the_same_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, unset: str, message: str
) -> None:
    import scripts.drain_outbox as worker

    _environment(monkeypatch, tmp_path, SYNTHETIC_ENVIRONMENT)
    monkeypatch.delenv(unset)
    _record_stages(monkeypatch)

    with pytest.raises(ValueError, match=message):
        worker._stages()


# ---------------------------------------------------------------------------
# The Hatchet worker builds the same stages
# ---------------------------------------------------------------------------


class _NeverStarted:
    def start(self) -> None:
        """The engine is not reached in these tests."""


def _run_worker(monkeypatch: pytest.MonkeyPatch) -> int:
    from app.config import Settings
    from workflow import hatchet_app
    from workflow.entrypoints import run_worker

    monkeypatch.setattr(hatchet_app, "build_worker", lambda *_args, **_kwargs: _NeverStarted())
    settings = Settings(hatchet_token="a-token")  # type: ignore[call-arg]
    return run_worker(settings, factory=object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stated", [SYNTHETIC_ENVIRONMENT, MINIMAL_ENVIRONMENT], ids=["every-route-stated", "minimal"]
)
def test_the_hatchet_worker_builds_the_local_worker_s_stage_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stated: dict[str, str]
) -> None:
    """Fails on main: `run_worker` passed 7 of the 19 arguments, so it read without the localized
    reader, the automatic types, the vision gate, the sharper pictures and the part pictures."""
    import scripts.drain_outbox as worker
    from storage.local import LocalStore
    from workflow.entrypoints import EXIT_OK

    store_root = _environment(monkeypatch, tmp_path, stated)
    recorder = _record_stages(monkeypatch)

    assert _run_worker(monkeypatch) == EXIT_OK
    worker._stages()

    hatchet, local = recorder.calls
    per_run = {"store", "timings", "discriminators", "discriminator_sources"}

    def deployment(call: tuple[tuple[object, ...], dict[str, object]]) -> dict[str, object]:
        return {name: value for name, value in call[1].items() if name not in per_run}

    assert deployment(hatchet) == deployment(local)
    assert set(deployment(local)) == set(local[1]) - per_run
    hatchet_store = hatchet[1].get("store") or hatchet[0][0]
    assert isinstance(hatchet_store, LocalStore)
    assert hatchet_store.root == store_root.resolve()


def test_the_hatchet_worker_refuses_a_partial_reader_configuration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Fails on main, which never read the localized reader's settings and so started without them.
    Now it refuses as the local worker does, with the same message, and a misconfiguration code."""
    from workflow.entrypoints import EXIT_MISCONFIGURED

    _environment(monkeypatch, tmp_path, SYNTHETIC_ENVIRONMENT)
    monkeypatch.delenv("GV_READER_FRACTION_TURNED_ASPECT_MIN")
    _record_stages(monkeypatch)

    with caplog.at_level(logging.ERROR, logger="gv.workflow.entrypoints"):
        assert _run_worker(monkeypatch) == EXIT_MISCONFIGURED
    assert "GV_READER_FRACTION_TURNED_ASPECT_MIN" in caplog.text


# ---------------------------------------------------------------------------
# One constructor
# ---------------------------------------------------------------------------

#: Offline tools that build stages for one purpose and never consume the outbox. Frozen: a new
#: entry needs a reason, and a runner never belongs here.
OFFLINE_TOOLS = frozenset(
    {
        "scripts/evaluate_goldset.py",
        "scripts/fraction_parts_scorecard.py",
        "scripts/run_checks.py",
        "scripts/seed_demo.py",
    }
)


def _constructs_database_stages(source: str) -> bool:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            called = node.func
            name = (
                called.id
                if isinstance(called, ast.Name)
                else called.attr if isinstance(called, ast.Attribute) else None
            )
            if name == "DatabaseStages":
                return True
    return False


def test_only_the_factory_constructs_database_stages() -> None:
    """Fails on main: the local worker and the Hatchet worker each built their own."""
    constructors = set()
    for path in REPOSITORY.rglob("*.py"):
        relative = path.relative_to(REPOSITORY).as_posix()
        if relative.startswith(("tests/", "frontend/", ".venv/", "alembic/")) or "/." in relative:
            continue
        if _constructs_database_stages(path.read_text(encoding="utf-8")):
            constructors.add(relative)

    assert constructors - OFFLINE_TOOLS == {"workflow/sutradhar.py"}


# ---------------------------------------------------------------------------
# A launcher's patch still takes effect
# ---------------------------------------------------------------------------


def test_a_launcher_that_patches_the_slot_reader_factory_reaches_the_stages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A local launcher replaces `workflow.slot_reader.configured_slot_reader` in its own process and
    then runs `scripts/drain_outbox.py` through `runpy`. The factory looks the reader up when it
    builds the stages, so the replacement is what the stages get, imported or run as a script."""
    import scripts.drain_outbox as worker
    from workflow import slot_reader

    _environment(monkeypatch, tmp_path, MINIMAL_ENVIRONMENT)
    recorder = _record_stages(monkeypatch)
    replaced = object()
    seen: list[object] = []

    def configured_slot_reader(settings: object, form: object, *_rest: object) -> object:
        seen.append(form)
        return replaced

    monkeypatch.setattr(slot_reader, "configured_slot_reader", configured_slot_reader)

    worker._stages()
    script = runpy.run_path(str(REPOSITORY / "scripts" / "drain_outbox.py"), run_name="launcher")
    script["_stages"]()

    assert [kwargs["slot_reader"] for _args, kwargs in recorder.calls] == [replaced, replaced]
    assert seen == [None, None], "the form reader built beside it is handed over"
