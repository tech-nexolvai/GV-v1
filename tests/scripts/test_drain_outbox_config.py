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
