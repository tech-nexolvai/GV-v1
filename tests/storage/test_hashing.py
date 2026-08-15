"""Streaming hash, content key and read-time integrity tests for issue #219."""

from __future__ import annotations

import tracemalloc
from io import BytesIO
from pathlib import Path

import pytest
from sqlalchemy import Float

from app.models import SourceArtifact
from storage.hashing import CHUNK, ArtifactCorrupt, content_key, sha256_stream
from storage.local import INTEGRITY_DIRECTORY, LocalStore


class RepeatedByteStream:
    """Produce a large logical stream without allocating the whole value."""

    def __init__(self, size: int) -> None:
        self.remaining = size
        self.largest_request = 0

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            raise AssertionError("hashing attempted an unbounded read")
        self.largest_request = max(self.largest_request, size)
        amount = min(size, self.remaining)
        self.remaining -= amount
        return b"x" * amount


def test_large_stream_hashing_has_a_measured_memory_bound() -> None:
    """Input: logical 200 MB file. Outcome: <=1 MB reads. Why: originals may be very large."""

    size = 200 * 1024 * 1024
    stream = RepeatedByteStream(size)
    tracemalloc.start()
    digest, counted = sha256_stream(stream)  # type: ignore[arg-type]
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    assert digest == "514f0b9344ea04c03032092f3cc4449bbd384aa226b4f7355b39705aa99d3ad2"
    assert counted == size
    assert stream.largest_request == CHUNK
    assert peak < CHUNK * 3


def test_identical_content_produces_one_deterministic_key(tmp_path: Path) -> None:
    """Input: same bytes twice. Outcome: same key and one file. Why: reruns must deduplicate."""

    payload = b"same immutable drawing"
    digest, _ = sha256_stream(BytesIO(payload))
    key = content_key("originals", digest, suffix=".pdf")
    store = LocalStore(tmp_path / "artifacts")

    first = store.put(key, BytesIO(payload), content_type="application/pdf")
    second = store.put(key, BytesIO(payload), content_type="application/pdf")

    assert first == second
    assert key == f"originals/{digest[:2]}/{digest[2:4]}/{digest}.pdf"
    public_files = [
        path
        for path in store.root.rglob("*")
        if path.is_file() and INTEGRITY_DIRECTORY not in path.parts
    ]
    assert public_files == [store.root.joinpath(*key.split("/"))]


def test_corrupted_artifact_raises_on_read(tmp_path: Path) -> None:
    """Input: stored byte changed. Outcome: ArtifactCorrupt. Why: suspect evidence is never read."""

    store = LocalStore(tmp_path / "artifacts")
    key = "originals/drawing.pdf"
    store.put(key, BytesIO(b"approved bytes"), content_type="application/pdf")
    (store.root / "originals" / "drawing.pdf").write_bytes(b"tampered bytes")

    with pytest.raises(ArtifactCorrupt, match="failed SHA-256 verification"):
        store.get(key)


def test_missing_integrity_record_raises_on_read(tmp_path: Path) -> None:
    """Input: checksum record removed. Outcome: ArtifactCorrupt. Why: absence cannot mean trusted."""

    store = LocalStore(tmp_path / "artifacts")
    key = "crops/value.png"
    store.put(key, BytesIO(b"crop"), content_type="image/png")
    integrity = store.root / INTEGRITY_DIRECTORY / "crops" / "value.png.sha256"
    integrity.unlink()

    with pytest.raises(ArtifactCorrupt, match="no valid integrity record"):
        store.get(key)


@pytest.mark.parametrize(
    ("namespace", "digest", "suffix"),
    [
        ("../escape", "a" * 64, ".pdf"),
        ("originals", "A" * 64, ".pdf"),
        ("originals", "a" * 63, ".pdf"),
        ("originals", "a" * 64, "/file.pdf"),
    ],
)
def test_invalid_content_key_components_are_rejected(
    namespace: str, digest: str, suffix: str
) -> None:
    """Input: malformed key component. Outcome: rejection. Why: keys must remain portable."""

    with pytest.raises(ValueError):
        content_key(namespace, digest, suffix=suffix)


def test_postgres_artifact_record_keeps_hash_beside_key() -> None:
    """Input: SourceArtifact schema. Outcome: key and hash columns. Why: DB owns provenance."""

    table = SourceArtifact.__table__
    assert {"storage_key", "sha256"} <= set(table.columns.keys())
    assert not any(isinstance(column.type, Float) for column in table.columns)


def test_sha256_stream_rejects_text_streams() -> None:
    """Input: text-returning stream. Outcome: TypeError. Why: hashes describe bytes, not encoding."""

    class TextStream:
        def read(self, size: int = -1) -> str:
            del size
            return "not bytes"

    with pytest.raises(TypeError, match="must yield bytes"):
        sha256_stream(TextStream())  # type: ignore[arg-type]
