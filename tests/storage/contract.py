"""Shared contract suite for every artifact-store backend."""

from __future__ import annotations

from io import BytesIO
from typing import Protocol

import pytest

from storage.store import ArtifactConflict, ArtifactStore


class StoreFactory(Protocol):
    """Create a fresh store instance pointing at the same durable backend."""

    def __call__(self) -> ArtifactStore: ...


class ArtifactStoreContract:
    """Behaviour that local and future remote stores must satisfy unchanged."""

    @pytest.fixture
    def store_factory(self) -> StoreFactory:
        """Supply a backend-specific factory from the concrete test class."""

        raise NotImplementedError

    def test_put_get_exists_and_metadata(self, store_factory: StoreFactory) -> None:
        """Input: PDF bytes. Outcome: exact round-trip. Why: evidence pins exact content."""

        store = store_factory()
        payload = b"%PDF-1.7\nimmutable drawing bytes"
        artifact = store.put(
            "packages/p-1/source.pdf", BytesIO(payload), content_type="application/pdf"
        )

        assert store.exists(artifact.key)
        with store.get(artifact.key) as restored:
            assert restored.read() == payload
        assert artifact.key == "packages/p-1/source.pdf"
        assert artifact.sha256 == "ac5fa9eb5de7b700ac71b760dbcdff68579d5a86cd9b614dbaebfd7a19da0fa6"
        assert artifact.size == len(payload)
        assert artifact.backend_version_id is None

    def test_identical_repeated_put_is_a_no_op(self, store_factory: StoreFactory) -> None:
        """Input: same key and bytes twice. Outcome: same record. Why: retries are idempotent."""

        store = store_factory()
        first = store.put("renders/page-1.png", BytesIO(b"same"), content_type="image/png")
        second = store.put("renders/page-1.png", BytesIO(b"same"), content_type="image/png")

        assert second == first

    def test_existing_key_with_different_bytes_is_rejected(
        self, store_factory: StoreFactory
    ) -> None:
        """Input: reused key, changed bytes. Outcome: conflict. Why: overwrite breaks evidence."""

        store = store_factory()
        store.put("originals/drawing.pdf", BytesIO(b"revision A"), content_type="application/pdf")

        with pytest.raises(ArtifactConflict, match="different bytes"):
            store.put(
                "originals/drawing.pdf",
                BytesIO(b"revision B"),
                content_type="application/pdf",
            )
        with store.get("originals/drawing.pdf") as preserved:
            assert preserved.read() == b"revision A"

    def test_uri_is_stable_across_store_restart(self, store_factory: StoreFactory) -> None:
        """Input: same root after restart. Outcome: same URI and bytes. Why: findings stay resolvable."""

        first_store = store_factory()
        key = "crops/finding-7.png"
        first_store.put(key, BytesIO(b"crop"), content_type="image/png")
        original_uri = first_store.uri(key)

        restarted_store = store_factory()
        assert restarted_store.uri(key) == original_uri
        with restarted_store.get(key) as restored:
            assert restored.read() == b"crop"
