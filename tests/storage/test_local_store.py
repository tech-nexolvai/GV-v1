"""Local filesystem implementation of the shared artifact-store contract."""

from __future__ import annotations

import inspect
from io import BytesIO
from pathlib import Path

import pytest

from storage.local import DEFAULT_ROOT, LocalStore
from storage.store import ArtifactStore
from tests.storage.contract import ArtifactStoreContract, StoreFactory


class TestLocalStoreContract(ArtifactStoreContract):
    """Run the backend-neutral contract against a persistent temporary root."""

    @pytest.fixture
    def store_factory(self, tmp_path: Path) -> StoreFactory:
        """Input: one durable root. Outcome: restartable factory. Why: simulate process restart."""

        return lambda: LocalStore(tmp_path / "artifacts")


def test_local_store_satisfies_backend_neutral_protocol(tmp_path: Path) -> None:
    """Input: LocalStore. Outcome: protocol match. Why: callers must not know the backend."""

    assert isinstance(LocalStore(tmp_path), ArtifactStore)
    signatures = " ".join(
        str(inspect.signature(getattr(ArtifactStore, method)))
        for method in ("put", "get", "exists", "uri")
    ).lower()
    assert "s3" not in signatures
    assert "bucket" not in signatures


@pytest.mark.parametrize("key", ["", "/absolute/file", "../escape", "safe/../../escape", "a\\b"])
def test_unsafe_keys_are_rejected(tmp_path: Path, key: str) -> None:
    """Input: unsafe path key. Outcome: rejection. Why: artifacts must stay under the root."""

    store = LocalStore(tmp_path / "artifacts")
    with pytest.raises(ValueError):
        store.put(key, BytesIO(b"bytes"), content_type="application/octet-stream")


def test_default_root_is_not_inside_the_repository() -> None:
    """Input: default root. Outcome: outside checkout. Why: client files cannot be committed."""

    repository = Path(__file__).resolve().parents[2]
    assert not DEFAULT_ROOT.resolve().is_relative_to(repository)
