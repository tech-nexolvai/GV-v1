"""Configuration boundaries for the local reader worker."""

from __future__ import annotations

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
