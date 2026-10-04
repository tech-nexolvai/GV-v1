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
# Stacked fractions read piece by piece, on in the demo (#875)
# ---------------------------------------------------------------------------


def test_the_demo_worker_reads_stacked_fractions_piece_by_piece(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**On in the demo, at the sizes #848 measured** (the admin's decision of 2026-10-03, on #756).
    Built the way the worker builds it, from what the demo's worker block states — and with the gate
    reader stated too, because the route's second reader is that reader and it refuses to start
    without one (#865)."""
    from workflow.stages import FRACTION_PARTS_ENV, fraction_parts_from_environment

    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    stated = dict(
        re.findall(
            r"^(GV_FRACTION_PARTS[A-Z0-9_]*|GV_VISION_GATE_READER)=(\S+) \\$",
            worker_block,
            flags=re.MULTILINE,
        )
    )
    for name in list(os.environ):
        if name.startswith(FRACTION_PARTS_ENV):
            monkeypatch.delenv(name)
    for name, value in stated.items():
        monkeypatch.setenv(name, value)

    drawing = fraction_parts_from_environment()

    assert drawing is not None, "the demo does not switch the fraction-parts route on"
    assert (drawing.height_px, drawing.stroke_px, drawing.margin_px, drawing.bezier_steps) == (
        40,
        4,
        32,
        8,
    )
    assert stated.get("GV_VISION_GATE_READER"), "the route's second reader is not stated"


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
# The reader pair, Qwen3-VL + Nova 2 Lite, on in the demo (#907)
# ---------------------------------------------------------------------------


def _demo_worker_settings(*names: str) -> dict[str, str]:
    demo = (Path(__file__).resolve().parents[2] / "scripts" / "demo.sh").read_text(encoding="utf-8")
    worker_block = demo[: demo.index("scripts/drain_outbox.py --watch")]
    pattern = "|".join(re.escape(name) for name in names)
    return dict(re.findall(rf"^({pattern})=(\S+) \\$", worker_block, flags=re.MULTILINE))


def test_the_demo_worker_runs_the_new_pair_with_qwen_first(monkeypatch: pytest.MonkeyPatch) -> None:
    """**On in the demo** (the admin's decision of 2026-10-04, after the production measurement):
    the worker names Qwen3-VL and Nova 2 Lite as the trial measured it, Qwen reads first (#787),
    and Nova's sharper picture is rendered at the stated 900 dpi. Built the way the worker builds
    its stages, from what the demo's worker block states — they start, so nothing is missing."""
    from extraction.models.nova import (
        NOVA_2_LITE_TAUGHT_EXTRACTOR,
        QWEN3_VL_235B_EXTRACTOR,
        AnswerFormat,
        ReaderPicture,
    )
    from workflow.reader_pictures import PictureSettings, picture_settings_from_environment
    from workflow.stages import (
        DatabaseStages,
        configured_vision_readers_from_environment,
        fraction_parts_from_environment,
    )

    stated = _demo_worker_settings(
        "GV_BEDROCK_VISION_ENABLED",
        "GV_BEDROCK_VISION_READERS",
        "GV_VISION_GATE_READER",
        "GV_VISION_SHARPER_PICTURE_DPI",
        "GV_FRACTION_PARTS",
        "GV_FRACTION_PARTS_HEIGHT_PX",
        "GV_FRACTION_PARTS_STROKE_PX",
        "GV_FRACTION_PARTS_MARGIN_PX",
        "GV_FRACTION_PARTS_BEZIER_STEPS",
    )
    for name, value in stated.items():
        monkeypatch.setenv(name, value)

    assert stated["GV_BEDROCK_VISION_READERS"] == "qwen3-vl-235b,nova-2-lite-taught"
    assert stated["GV_VISION_GATE_READER"] == QWEN3_VL_235B_EXTRACTOR
    assert picture_settings_from_environment() == PictureSettings(sharper_dpi=900)
    readers = configured_vision_readers_from_environment()
    by_name = {reader.config.extractor: reader.config for reader in readers}
    assert set(by_name) == {QWEN3_VL_235B_EXTRACTOR, NOVA_2_LITE_TAUGHT_EXTRACTOR}
    assert by_name[QWEN3_VL_235B_EXTRACTOR].answer_format is AnswerFormat.JSON_SCHEMA
    assert by_name[NOVA_2_LITE_TAUGHT_EXTRACTOR].picture is ReaderPicture.UPRIGHT_SHARPER
    DatabaseStages(
        None,
        vision_readers=readers,
        vision_gate=stated["GV_VISION_GATE_READER"],
        reader_pictures=picture_settings_from_environment(),
    )
    assert (
        fraction_parts_from_environment() is not None
    ), "the stacked route's second reader is Qwen"


def test_the_demo_worker_without_the_sharper_dpi_would_not_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control: the same readers with the dpi left out are refused, never given a default."""
    from workflow.reader_pictures import SHARPER_PICTURE_DPI_ENV, picture_settings_from_environment
    from workflow.stages import DatabaseStages, configured_vision_readers_from_environment

    stated = _demo_worker_settings("GV_BEDROCK_VISION_ENABLED", "GV_BEDROCK_VISION_READERS")
    for name, value in stated.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(SHARPER_PICTURE_DPI_ENV, raising=False)

    with pytest.raises(ValueError, match=SHARPER_PICTURE_DPI_ENV):
        DatabaseStages(
            None,
            vision_readers=configured_vision_readers_from_environment(),
            reader_pictures=picture_settings_from_environment(),
        )


def test_every_vision_run_identity_fits_its_column_on_the_demo_s_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**A run's identity is 200 characters at most** (`extraction_runs.config_hash`). Written
    out, the demo's settings, a gate and an upright, sharper picture ran past it — 262 characters,
    which the database would have refused on the first page the demo read. Every defined reader,
    gated or not, on the settings the demo's worker block states, fits; so do the runs that read
    the file's own text, which carry the reader's missing-space setting too (#912)."""
    import scripts.drain_outbox as worker
    from app.models import ExtractionRun
    from extraction.models.nova import (
        VISION_READERS,
        ReaderPicture,
        vision_config_for_extractor,
    )
    from workflow.reader_pictures import picture_settings_from_environment
    from workflow.reading_agent import reading_agent_from_environment
    from workflow.stages import (
        RUN_IDENTITY_CHARACTERS,
        BedrockVisionReader,
        DatabaseStages,
        missing_space_from_environment,
    )

    _demo_reader_environment(monkeypatch)
    for name, value in _demo_worker_settings(
        "GV_VISION_SHARPER_PICTURE_DPI",
        "GV_READING_AGENT",
        "GV_AGENT_MAX_STEPS",
        "GV_AGENT_MAX_ESCALATIONS",
        "GV_AGENT_SHARPER_DPI",
        "GV_AGENT_PRIMARY_READER",
        "GV_AGENT_ESCALATION_READER",
        "GV_AGENT_LABEL_GAP_PT",
        "GV_AGENT_MAX_LABEL_PT",
    ).items():
        monkeypatch.setenv(name, value)
    association, localized = worker._reader_configuration()
    assert association is not None
    readers = []
    for definition in VISION_READERS:
        config = vision_config_for_extractor(definition.extractor)
        if config is not None:
            readers.append(BedrockVisionReader(config))
    assert any(reader.config.picture is ReaderPicture.UPRIGHT_SHARPER for reader in readers)
    stages = DatabaseStages(
        None,
        association=association,  # type: ignore[arg-type]
        localized_ocr=localized,  # type: ignore[arg-type]
        vision_readers=readers,
        reading_agent=reading_agent_from_environment(),
        reader_pictures=picture_settings_from_environment(),
        missing_space=missing_space_from_environment(),
    )
    limit = ExtractionRun.__table__.c.config_hash.type.length
    assert stages._text_run_config() == "dpi=300;missing_space>=0.1"
    assert limit == RUN_IDENTITY_CHARACTERS
    longest_gate = max((reader.config.extractor for reader in readers), key=len)

    for reader in readers:
        for gate in (None, longest_gate):
            identity = stages._vision_run_config(reader, gate=gate)
            assert len(identity) <= limit, (reader.config.extractor, gate, len(identity))
            assert identity.startswith("dpi=300;route=vision;")
            if reader.config.picture is ReaderPicture.UPRIGHT_SHARPER:
                assert "picture=upright_sharper;sharper_picture_dpi=900;run=" in identity
            else:
                assert "picture=" not in identity
        if reader.config.picture is ReaderPicture.AS_CUT:
            # Unchanged from before #907 where it fits: written out, so old runs are found again.
            assert "fraction_bar=" in stages._vision_run_config(reader, gate=None)
    sharper = next(r for r in readers if r.config.picture is ReaderPicture.UPRIGHT_SHARPER)
    assert stages._vision_run_config(sharper, gate=None) != stages._vision_run_config(
        sharper, gate=longest_gate
    ), "a gated reader's run is another run, whatever its picture"


def test_every_run_identity_fits_its_column_whatever_the_missing_space_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**#907's identities and #912's setting together.** The setting is part of every run that
    reads the file's own text, and of the fingerprint of a reader shown an upright, sharper picture,
    whose turn reads that text. Stated with digits enough to run past the column, a text run's
    identity is a fingerprint instead, never cut short; another value is always another run; and a
    reader shown the crop as cut keeps the identity it had, which the setting does not touch."""
    from decimal import Decimal

    import scripts.drain_outbox as worker
    from app.models import ExtractionRun
    from extraction.models.nova import VISION_READERS, ReaderPicture, vision_config_for_extractor
    from extraction.reader import MissingSpace
    from workflow.reader_pictures import picture_settings_from_environment
    from workflow.stages import BedrockVisionReader, DatabaseStages

    _demo_reader_environment(monkeypatch)
    monkeypatch.setenv(
        "GV_VISION_SHARPER_PICTURE_DPI",
        _demo_worker_settings("GV_VISION_SHARPER_PICTURE_DPI")["GV_VISION_SHARPER_PICTURE_DPI"],
    )
    association, localized = worker._reader_configuration()
    readers = [
        BedrockVisionReader(config)
        for definition in VISION_READERS
        if (config := vision_config_for_extractor(definition.extractor)) is not None
    ]
    limit = ExtractionRun.__table__.c.config_hash.type.length

    def stages(gap_heights: str) -> DatabaseStages:
        return DatabaseStages(
            None,
            association=association,  # type: ignore[arg-type]
            localized_ocr=localized,  # type: ignore[arg-type]
            vision_readers=readers,
            reader_pictures=picture_settings_from_environment(),
            missing_space=MissingSpace(gap_heights=Decimal(gap_heights)),
        )

    demo, other, long = stages("0.1"), stages("0.12"), stages("0." + "1" * 300)
    identities = {
        name: (
            built._text_run_config(),
            {r: built._vision_run_config(r, gate=None) for r in readers},
        )
        for name, built in (("demo", demo), ("other", other), ("long", long))
    }
    for text, vision in identities.values():
        assert len(text) <= limit
        assert all(len(identity) <= limit for identity in vision.values())
    assert identities["long"][0].startswith("dpi=300;run=")
    assert len({text for text, _ in identities.values()}) == 3, "another value is another run"
    for reader in readers:
        seen = {vision[reader] for _, vision in identities.values()}
        if reader.config.picture is ReaderPicture.UPRIGHT_SHARPER:
            assert len(seen) == 3, "the turn reads the text the setting decides"
        else:
            assert len(seen) == 1, "a reader shown the crop as cut keeps its identity"
