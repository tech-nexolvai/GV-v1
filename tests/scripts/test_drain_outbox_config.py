"""Configuration boundaries for the local reader worker."""

from __future__ import annotations

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
