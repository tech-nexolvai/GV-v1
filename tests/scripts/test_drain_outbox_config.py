"""Configuration boundaries for the local reader worker."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

from vocabulary.semantic_types import SemanticType


def test_automatic_typing_is_disabled_without_an_explicit_allowlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker never infers a deployment's allowed semantic vocabulary."""
    import scripts.drain_outbox as worker

    monkeypatch.delenv("GV_AUTOMATIC_TYPES", raising=False)
    assert worker._automatic_typing_configuration() is None

    monkeypatch.setenv("GV_AUTOMATIC_TYPES", SemanticType.CT007.value)
    configured = worker._automatic_typing_configuration()
    assert configured is not None
    assert configured.permitted_types == frozenset({SemanticType.CT007})


def test_automatic_typing_refuses_a_non_vocabulary_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.drain_outbox as worker

    monkeypatch.setenv("GV_AUTOMATIC_TYPES", "cabinet width")
    with pytest.raises(ValueError, match="exact semantic-type tags"):
        worker._automatic_typing_configuration()


# ---------------------------------------------------------------------------
# The phrase index's gap (#836)
# ---------------------------------------------------------------------------


def test_the_phrase_gap_has_no_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset means no phrases are built, and the log says why, rather than a guessed gap."""
    import scripts.drain_outbox as worker

    monkeypatch.delenv(worker.PHRASE_GAP_VARIABLE, raising=False)

    assert worker._phrase_grouping() is None
    assert worker._build_package_text(object(), uuid4()) == {
        "built": False,
        "reason": f"{worker.PHRASE_GAP_VARIABLE} is not set",
    }


def test_the_demo_worker_states_the_measured_phrase_gap(monkeypatch: pytest.MonkeyPatch) -> None:
    """**The index is on in the demo (#849)**, at the admin's 0.34 of 2026-10-03, measured on both
    client drawings in #840. Read from the worker's block the way the worker reads its environment.
    """
    import scripts.drain_outbox as worker
    from retrieval.package_text import PhraseGrouping

    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    stated = re.findall(
        rf"^{worker.PHRASE_GAP_VARIABLE}=(\S+) \\$", worker_block, flags=re.MULTILINE
    )
    assert stated == ["0.34"]

    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, stated[0])
    assert worker._phrase_grouping() == PhraseGrouping(gap_line_heights=Decimal("0.34"))


def test_a_stated_phrase_gap_is_read_exactly(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.drain_outbox as worker
    from retrieval.package_text import PhraseGrouping

    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, "0.3")

    assert worker._phrase_grouping() == PhraseGrouping(gap_line_heights=Decimal("0.3"))


@pytest.mark.parametrize("stated", ["wide", "-0.1", "NaN"])
def test_a_malformed_phrase_gap_is_reported_not_ignored(
    monkeypatch: pytest.MonkeyPatch, stated: str
) -> None:
    """A typo must not look like a deployment that chose not to build the index."""
    import scripts.drain_outbox as worker

    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, stated)

    with pytest.raises(ValueError):
        worker._phrase_grouping()
    result = worker._build_package_text(object(), uuid4())
    assert result["built"] is False
    assert result["reason"]


def test_a_failed_phrase_build_is_reported_never_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """**It cannot fail the extraction.** The failure is named by its type only: a database error
    repeats the row it refused, and that row is the drawing's text."""
    import scripts.drain_outbox as worker
    from retrieval import package_text

    class _Savepoint:
        def __enter__(self) -> None:
            return None

        def __exit__(self, *_: object) -> None:
            return None

    class _Session:
        def begin_nested(self) -> _Savepoint:
            return _Savepoint()

    def _broken(*_: object) -> object:
        raise RuntimeError("a row quoting SINK BASE 30 1/2")

    monkeypatch.setenv(worker.PHRASE_GAP_VARIABLE, "0.3")
    monkeypatch.setattr(package_text, "build_package_phrases", _broken)

    result = worker._build_package_text(_Session(), uuid4())

    assert result == {"built": False, "reason": "the phrase build failed: RuntimeError"}


# ---------------------------------------------------------------------------
# The stacked-fraction detector's turned labels (#869)
# ---------------------------------------------------------------------------


def _demo_reader_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reader settings the demo's worker block states, set as the worker's environment."""
    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    stated = re.findall(
        r"^(GV_READER_[A-Z0-9_]+|GV_LOCALIZED_OCR_ENABLED)=(\S+) \\$",
        worker_block,
        flags=re.MULTILINE,
    )
    assert stated, "the demo's worker block states no reader settings"
    for name, value in stated:
        monkeypatch.setenv(name, value)


def test_the_demo_worker_looks_for_turned_labels_at_the_measured_aspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Sideways stacked fractions are looked for in the demo (#869)**, at the 1.1 measured on
    `AI_Set 2`. Built the way the worker builds it, from what the demo's worker block states."""
    import scripts.drain_outbox as worker
    from workflow.association import AssociationSettings

    _demo_reader_environment(monkeypatch)

    association, _ = worker._reader_configuration()

    assert isinstance(association, AssociationSettings)
    assert association.fraction_bar.turned_aspect_min == Decimal("1.1")


def test_the_worker_refuses_to_guess_the_turned_aspect(monkeypatch: pytest.MonkeyPatch) -> None:
    """**No default.** A worker not told which bars across a stamp's baseline count refuses to start
    rather than choosing a number for every deployment."""
    import scripts.drain_outbox as worker

    _demo_reader_environment(monkeypatch)
    monkeypatch.delenv("GV_READER_FRACTION_TURNED_ASPECT_MIN")

    with pytest.raises(ValueError, match="GV_READER_FRACTION_TURNED_ASPECT_MIN"):
        worker._reader_configuration()


# ---------------------------------------------------------------------------
# Every suggested part's picture (#897)
# ---------------------------------------------------------------------------


def _demo_worker_block() -> str:
    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    return demo[: demo.index("scripts/drain_outbox.py --watch")]


def _without_picture_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.drain_outbox as worker

    for name in (worker.PART_PICTURE_MARGIN_VARIABLE, worker.PART_PICTURE_DPI_VARIABLE):
        monkeypatch.delenv(name, raising=False)


def test_the_demo_worker_states_how_each_parts_picture_is_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**On in the demo** (the admin's decision of 2026-10-04): an inch round each part, at 150 dpi.
    Built the way the worker builds it, from what the demo's worker block states."""
    import scripts.drain_outbox as worker
    from workflow.part_pictures import PartPictureSettings

    stated = dict(
        re.findall(
            r"^(GV_PART_PICTURE_[A-Z_]+)=(\S+) \\$", _demo_worker_block(), flags=re.MULTILINE
        )
    )
    assert stated == {
        worker.PART_PICTURE_MARGIN_VARIABLE: "72",
        worker.PART_PICTURE_DPI_VARIABLE: "150",
    }
    _without_picture_settings(monkeypatch)
    for name, value in stated.items():
        monkeypatch.setenv(name, value)

    assert worker._part_picture_configuration(required=True) == PartPictureSettings(
        margin_pt=Decimal(72), dpi=150
    )


@pytest.mark.parametrize("unset", ["GV_PART_PICTURE_MARGIN_PT", "GV_PART_PICTURE_DPI"])
def test_a_worker_that_suggests_parts_refuses_to_guess_how_to_picture_them(
    monkeypatch: pytest.MonkeyPatch, unset: str
) -> None:
    """**No default.** Wherever parts are suggested, a worker not told how to cut their pictures
    refuses to start, naming what is missing, rather than leaving the Measure page without them."""
    import scripts.drain_outbox as worker

    monkeypatch.setenv(worker.PART_PICTURE_MARGIN_VARIABLE, "72")
    monkeypatch.setenv(worker.PART_PICTURE_DPI_VARIABLE, "150")
    monkeypatch.delenv(unset)

    with pytest.raises(ValueError, match=unset):
        worker._part_picture_configuration(required=True)


def test_the_worker_requires_picture_settings_wherever_it_suggests_parts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker's stages are built with the reader's settings, which is where parts are
    suggested, so its picture settings are required there; without them it does not start."""
    import scripts.drain_outbox as worker

    _demo_reader_environment(monkeypatch)
    _without_picture_settings(monkeypatch)
    monkeypatch.setenv("GV_DATABASE_URL", "postgresql+psycopg://gv:gv@localhost:5433/never-opened")
    for name in list(os.environ):
        if name.startswith(("GV_READING_AGENT", "GV_FRACTION_PARTS", "GV_GLYPH")):
            monkeypatch.delenv(name)

    with pytest.raises(ValueError, match="GV_PART_PICTURE_MARGIN_PT"):
        worker._stages()


def test_without_suggestions_nothing_asks_for_a_picture_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where no part is suggested and nothing is stated, no picture is cut: off, not defaulted."""
    import scripts.drain_outbox as worker

    _without_picture_settings(monkeypatch)

    assert worker._part_picture_configuration(required=False) is None


@pytest.mark.parametrize(
    ("margin", "dpi"),
    [
        ("wide", "150"),
        ("72", "high"),
        ("72", "150.5"),
        ("-1", "150"),
        ("NaN", "150"),
        ("72", "0"),
        ("72", ""),
    ],
    ids=[
        "margin-words",
        "dpi-words",
        "dpi-fraction",
        "margin-negative",
        "margin-nan",
        "dpi-zero",
        "half-stated",
    ],
)
def test_a_malformed_picture_setting_is_reported_not_ignored(
    monkeypatch: pytest.MonkeyPatch, margin: str, dpi: str
) -> None:
    """A typo, or one of the two left out, must not look like a deployment that chose no pictures."""
    import scripts.drain_outbox as worker

    monkeypatch.setenv(worker.PART_PICTURE_MARGIN_VARIABLE, margin)
    monkeypatch.setenv(worker.PART_PICTURE_DPI_VARIABLE, dpi)

    with pytest.raises(ValueError):
        worker._part_picture_configuration(required=False)


# ---------------------------------------------------------------------------
# A space the file left out inside the inches (#912)
# ---------------------------------------------------------------------------


def test_the_demo_worker_reads_with_the_measured_missing_space_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**Stated in the demo's worker block, at the 0.1 measured on both client drawings** (#912).
    Built the way the worker builds it, from what the block states."""
    from extraction.reader import MissingSpace
    from workflow.stages import missing_space_from_environment

    _demo_reader_environment(monkeypatch)

    assert missing_space_from_environment() == MissingSpace(gap_heights=Decimal("0.1"))


@pytest.mark.parametrize("stated", [None, "", "a tenth", "0", "1", "-0.1"])
def test_the_worker_refuses_to_guess_the_missing_space_setting(
    monkeypatch: pytest.MonkeyPatch, stated: str | None
) -> None:
    """**No default.** Unstated, or not a share of the text's height above 0 and below 1, the
    setting is refused with the variable named: the stages are never built without it."""
    from workflow.stages import MISSING_SPACE_ENV, missing_space_from_environment

    if stated is None:
        monkeypatch.delenv(MISSING_SPACE_ENV, raising=False)
    else:
        monkeypatch.setenv(MISSING_SPACE_ENV, stated)

    with pytest.raises(ValueError, match=MISSING_SPACE_ENV):
        missing_space_from_environment()


# ---------------------------------------------------------------------------
# The label lengths the cut-label guard is stated with, in the demo (#919)
# ---------------------------------------------------------------------------


def _demo_worker_settings(*names: str) -> dict[str, str]:
    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    pattern = "|".join(re.escape(name) for name in names)
    return dict(re.findall(rf"^({pattern})=(\S+) \\$", worker_block, flags=re.MULTILINE))


def test_the_demo_worker_states_the_label_lengths_the_reading_agent_had(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**The same 4 and 40 pt as before.** The demo's reading agent stated them; it was retired with
    the label-first route (2026-10-28), and the cut-label guard now takes them on their own. Built the
    way the worker builds them, from what the demo's worker block states."""
    from decimal import Decimal

    from extraction.geometry.labels import LabelLengths
    from workflow.stages import LABEL_LENGTH_SETTINGS, label_lengths_from_environment

    stated = _demo_worker_settings(*LABEL_LENGTH_SETTINGS)
    assert stated == {"GV_LABEL_GAP_PT": "4", "GV_MAX_LABEL_PT": "40"}
    for name, value in stated.items():
        monkeypatch.setenv(name, value)

    assert label_lengths_from_environment() == LabelLengths(Decimal(4), Decimal(40))


def test_the_demo_worker_turns_on_no_retired_reader() -> None:
    """The label-first route's switches are gone from the demo's worker block (2026-10-28)."""
    retired = (
        "GV_BEDROCK_VISION_ENABLED",
        "GV_BEDROCK_VISION_READERS",
        "GV_VISION_GATE_READER",
        "GV_VISION_SHARPER_PICTURE_DPI",
        "GV_READING_AGENT",
        "GV_FRACTION_PARTS",
    )

    assert _demo_worker_settings(*retired) == {}


def test_every_run_identity_fits_its_column_whatever_the_missing_space_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**#912's setting in every run that reads the file's own text.** Stated with digits enough to
    run past the column, a text run's identity is a fingerprint instead, never cut short; another
    value is always another run. (The vision readers' identities were checked here too until they
    were retired with the label-first route, 2026-10-28.)"""
    from decimal import Decimal

    import scripts.drain_outbox as worker
    from app.models import ExtractionRun
    from extraction.reader import MissingSpace
    from workflow.stages import DatabaseStages

    _demo_reader_environment(monkeypatch)
    association, localized = worker._reader_configuration()
    limit = ExtractionRun.__table__.c.config_hash.type.length

    def stages(gap_heights: str) -> DatabaseStages:
        return DatabaseStages(
            None,
            association=association,  # type: ignore[arg-type]
            localized_ocr=localized,  # type: ignore[arg-type]
            missing_space=MissingSpace(gap_heights=Decimal(gap_heights)),
        )

    demo, other, long = stages("0.1"), stages("0.12"), stages("0." + "1" * 300)
    identities = {
        name: built._text_run_config()
        for name, built in (("demo", demo), ("other", other), ("long", long))
    }
    for text in identities.values():
        assert len(text) <= limit
    assert identities["long"].startswith("dpi=300;run=")
    assert len(set(identities.values())) == 3, "another value is another run"


# ---------------------------------------------------------------------------
# The architect reader (#1052): the local worker builds it exactly as the deployed worker does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enabled", [None, "false", "true"])
def test_the_local_worker_passes_the_architect_reader_switch_through(
    monkeypatch: pytest.MonkeyPatch, enabled: str | None
) -> None:
    """`GV_ARCHITECT_READER_ENABLED` reaches the stages the local worker builds: on gives the measured
    settings, off or unset gives none. A worker that dropped it would read nothing, silently."""
    import scripts.drain_outbox as worker
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS
    from workflow import stages
    from workflow.stages import MISSING_SPACE_ENV

    built: dict[str, object] = {}

    class _Capture:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            built.update(kwargs)

    monkeypatch.setattr(stages, "DatabaseStages", _Capture)
    monkeypatch.setenv("GV_DATABASE_URL", "postgresql+psycopg://gv:gv@localhost:5433/never-opened")
    for name in list(os.environ):
        if name.startswith(
            (
                "GV_READER_",
                "GV_LOCALIZED_OCR",
                "GV_READING_AGENT",
                "GV_FRACTION_PARTS",
                "GV_GLYPH",
                "GV_FORM_READER",
                "GV_SLOT_READER",
                "GV_CLAUDE_READER",
            )
        ):
            monkeypatch.delenv(name)
    monkeypatch.setenv(MISSING_SPACE_ENV, "0.1")
    if enabled is None:
        monkeypatch.delenv("GV_ARCHITECT_READER_ENABLED", raising=False)
    else:
        monkeypatch.setenv("GV_ARCHITECT_READER_ENABLED", enabled)

    worker._stages()

    expected = MEASURED_ARCHITECT_SETTINGS if enabled == "true" else None
    assert built["architect_reader"] is expected
