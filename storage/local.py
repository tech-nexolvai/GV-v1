"""Immutable local-filesystem implementation of the artifact-store contract.

Source: ``DESIGN_PLATFORM.md`` section 7 and issue #218.
Verification: ``tests/storage/test_local_store.py``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from storage.hashing import SHA256_PATTERN, ArtifactCorrupt, sha256_stream
from storage.store import ArtifactConflict, StoredArtifact

DEFAULT_ROOT: Final = Path.home() / ".gv-v1" / "artifacts"
READ_CHUNK: Final = 1024 * 1024
INTEGRITY_DIRECTORY: Final = ".gv-integrity"


class LocalStore:
    """Store immutable artifacts beneath one configured filesystem root.

    Keys are portable, relative POSIX paths. Writes are staged beside their target and
    installed using an atomic hard link, so concurrent writers cannot overwrite a key.
    The default root is deliberately outside the repository and remains stable across
    process restarts.
    """

    def __init__(self, root: Path | str = DEFAULT_ROOT) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        """Return the resolved storage root for configuration and diagnostics."""

        return self._root

    def _path(self, key: str) -> Path:
        if not isinstance(key, str):
            raise TypeError("artifact key must be a string")
        if not key or "\\" in key:
            raise ValueError("artifact key must be a non-empty POSIX relative path")
        parsed = PurePosixPath(key)
        if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
            raise ValueError("artifact key must not be absolute or contain '.' or '..'")
        if parsed.parts[0] == INTEGRITY_DIRECTORY:
            raise ValueError(f"artifact key namespace {INTEGRITY_DIRECTORY!r} is reserved")
        return self._root.joinpath(*parsed.parts)

    def _integrity_path(self, key: str) -> Path:
        parsed = PurePosixPath(key)
        relative = Path(*parsed.parts)
        return self._root / INTEGRITY_DIRECTORY / relative.parent / f"{relative.name}.sha256"

    @staticmethod
    def _read_digest(path: Path, key: str) -> str:
        try:
            digest = path.read_text(encoding="ascii")
        except (FileNotFoundError, UnicodeDecodeError) as error:
            raise ArtifactCorrupt(f"artifact key {key!r} has no valid integrity record") from error
        if SHA256_PATTERN.fullmatch(digest) is None:
            raise ArtifactCorrupt(f"artifact key {key!r} has a malformed integrity record")
        return digest

    @staticmethod
    def _publish(temporary: Path, target: Path) -> bool:
        """Atomically link a staged file, returning whether this call installed it."""

        try:
            os.link(temporary, target)
        except FileExistsError:
            return False
        return True

    @staticmethod
    def _same_bytes(left: Path, right: Path) -> bool:
        if left.stat().st_size != right.stat().st_size:
            return False
        with left.open("rb") as existing, right.open("rb") as staged:
            while True:
                existing_chunk = existing.read(READ_CHUNK)
                staged_chunk = staged.read(READ_CHUNK)
                if existing_chunk != staged_chunk:
                    return False
                if not existing_chunk:
                    return True

    def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredArtifact:
        """Atomically store ``data`` without allowing an existing key to change."""

        if not isinstance(content_type, str) or not content_type.strip():
            raise ValueError("content_type must be a non-empty string")
        if not hasattr(data, "read"):
            raise TypeError("data must be a binary file-like object")

        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".gv-stage-", dir=target.parent)
        temporary = Path(temporary_name)
        integrity = self._integrity_path(key)
        integrity.parent.mkdir(parents=True, exist_ok=True)
        integrity_descriptor, integrity_name = tempfile.mkstemp(
            prefix=".gv-integrity-stage-", dir=integrity.parent
        )
        integrity_temporary = Path(integrity_name)
        try:
            with os.fdopen(descriptor, "wb") as staged:
                while chunk := data.read(READ_CHUNK):
                    if not isinstance(chunk, bytes):
                        raise TypeError("data must yield bytes")
                    staged.write(chunk)
                staged.flush()
                os.fsync(staged.fileno())

            with temporary.open("rb") as staged:
                digest, size = sha256_stream(staged)
            with os.fdopen(integrity_descriptor, "w", encoding="ascii", newline="") as record:
                record.write(digest)
                record.flush()
                os.fsync(record.fileno())

            if target.exists():
                if not self._same_bytes(target, temporary):
                    raise ArtifactConflict(f"artifact key {key!r} already stores different bytes")
            else:
                if not self._publish(temporary, target) and not self._same_bytes(target, temporary):
                    raise ArtifactConflict(
                        f"artifact key {key!r} was concurrently written with different bytes"
                    ) from None

            if integrity.exists():
                recorded = self._read_digest(integrity, key)
                if recorded != digest:
                    raise ArtifactCorrupt(
                        f"artifact key {key!r} conflicts with its integrity record"
                    )
            elif not self._publish(integrity_temporary, integrity):
                recorded = self._read_digest(integrity, key)
                if recorded != digest:
                    raise ArtifactCorrupt(
                        f"artifact key {key!r} was concurrently given a different integrity record"
                    )

            return StoredArtifact(
                key=key,
                sha256=digest,
                size=size,
                backend_version_id=None,
            )
        finally:
            temporary.unlink(missing_ok=True)
            integrity_temporary.unlink(missing_ok=True)

    def get(self, key: str) -> BinaryIO:
        """Verify and open a stored artifact, raising if its bytes changed."""

        target = self._path(key)
        expected = self._read_digest(self._integrity_path(key), key)
        with target.open("rb") as stored:
            actual, _ = sha256_stream(stored)
        if actual != expected:
            raise ArtifactCorrupt(
                f"artifact key {key!r} failed SHA-256 verification: "
                f"expected {expected}, got {actual}"
            )
        return target.open("rb")

    def exists(self, key: str) -> bool:
        """Return whether ``key`` identifies a regular stored file."""

        return self._path(key).is_file() and self._integrity_path(key).is_file()

    def uri(self, key: str) -> str:
        """Return the stable absolute ``file:`` URI for ``key``."""

        return self._path(key).as_uri()
