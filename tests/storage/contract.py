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

    # -----------------------------------------------------------------------
    # One key may not be a path prefix of another (#314)
    # -----------------------------------------------------------------------
    #
    # **These are in the shared suite deliberately, and that makes them a contract rather than a
    # local-store detail.** On a filesystem the two orders collide naturally — one stores a file where
    # the other needs a directory — and before #314 one of them leaked `FileExistsError` while the
    # other was reported in the words of a byte conflict. S3 has no directories, so neither order fails
    # there on its own: `C5.4` will have to refuse these deliberately to pass. That is the point. The
    # alternative — testing this only in `test_local_store.py` — would let the two backends disagree
    # about which keys are storable, on exactly the case this issue exists to stop diverging.

    @pytest.mark.parametrize(
        ("stored", "attempted"),
        [("a", "a/b"), ("prefix", "prefix/nested/object.bin")],
        ids=["one-level", "several-levels"],
    )
    def test_a_key_beneath_an_existing_key_is_refused(
        self, store_factory: StoreFactory, stored: str, attempted: str
    ) -> None:
        """Input: key nested under a stored key. Outcome: conflict. Why: neither can be stored."""

        store = store_factory()
        store.put(stored, BytesIO(b"first"), content_type="application/octet-stream")

        with pytest.raises(ArtifactConflict, match="prefix of another"):
            store.put(attempted, BytesIO(b"second"), content_type="application/octet-stream")

        # The refusal leaves the stored artifact exactly as it was. A half-applied collision that
        # damaged the existing key would be worse than the crash this replaced.
        assert store.exists(stored)
        with store.get(stored) as preserved:
            assert preserved.read() == b"first"
        assert not store.exists(attempted)

    @pytest.mark.parametrize(
        ("stored", "attempted"),
        [("a/b", "a"), ("prefix/nested/object.bin", "prefix/nested")],
        ids=["one-level", "several-levels"],
    )
    def test_a_key_above_an_existing_key_is_refused(
        self, store_factory: StoreFactory, stored: str, attempted: str
    ) -> None:
        """Input: key that is a prefix of a stored key. Outcome: conflict. Why: same collision, reversed.

        The order the original bug report called "correct". It did raise `ArtifactConflict`, but only
        because a directory's `st_size` differs from the staged file's — with a payload of exactly that
        size it reached `open("rb")` on a directory and raised `IsADirectoryError` instead.
        """

        store = store_factory()
        store.put(stored, BytesIO(b"first"), content_type="application/octet-stream")

        with pytest.raises(ArtifactConflict, match="prefix of another"):
            store.put(attempted, BytesIO(b"second"), content_type="application/octet-stream")

        assert store.exists(stored)
        with store.get(stored) as preserved:
            assert preserved.read() == b"first"
        assert not store.exists(attempted)

    def test_a_collision_is_reported_differently_from_a_byte_conflict(
        self, store_factory: StoreFactory
    ) -> None:
        """Input: both refusals. Outcome: distinct messages. Why: they call for different fixes.

        **The scope item that is easy to satisfy by accident and easy to lose.** A prefix collision
        means "choose a different key"; a byte conflict means "you are trying to change stored
        evidence". Before #314 the second order produced the byte-conflict wording for a collision,
        which sends whoever reads it looking for a bytes problem that never happened. Asserting each
        message does *not* match the other's pattern is what keeps them from converging again.
        """

        store = store_factory()
        store.put("shared", BytesIO(b"first"), content_type="application/octet-stream")
        store.put("other", BytesIO(b"A"), content_type="application/octet-stream")

        with pytest.raises(ArtifactConflict) as collision:
            store.put("shared/below", BytesIO(b"second"), content_type="application/octet-stream")
        with pytest.raises(ArtifactConflict) as byte_conflict:
            store.put("other", BytesIO(b"B"), content_type="application/octet-stream")

        collision_message = str(collision.value)
        byte_message = str(byte_conflict.value)

        assert "prefix of another" in collision_message
        assert "different bytes" in byte_message
        assert "different bytes" not in collision_message, "a collision is not a bytes problem"
        assert "prefix of another" not in byte_message

    def test_a_refused_collision_names_the_key_it_collides_with(
        self, store_factory: StoreFactory
    ) -> None:
        """Input: colliding put. Outcome: both keys named. Why: a refusal has to be actionable.

        Naming only the rejected key would leave the caller to work out which stored object is in the
        way, and on a store with thousands of keys that is a search rather than a fix.
        """

        store = store_factory()
        store.put("packages/p-1", BytesIO(b"first"), content_type="application/octet-stream")

        with pytest.raises(ArtifactConflict) as refusal:
            store.put("packages/p-1/source.pdf", BytesIO(b"second"), content_type="application/pdf")

        message = str(refusal.value)
        assert "packages/p-1/source.pdf" in message
        assert "packages/p-1'" in message, message

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
