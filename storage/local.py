"""Immutable local-filesystem implementation of the artifact-store contract.

Source: ``DESIGN_PLATFORM.md`` section 7 and issue #218.
Verification: ``tests/storage/test_local_store.py``.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final

from storage.store import ArtifactConflict, StoredArtifact

DEFAULT_ROOT: Final = Path.home() / ".gv-v1" / "artifacts"
READ_CHUNK: Final = 1024 * 1024


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
        return self._root.joinpath(*parsed.parts)

    @staticmethod
    def _metadata(path: Path, key: str) -> StoredArtifact:
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as stored:
            while chunk := stored.read(READ_CHUNK):
                digest.update(chunk)
                size += len(chunk)
        return StoredArtifact(
            key=key,
            sha256=digest.hexdigest(),
            size=size,
            backend_version_id=None,
        )

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
        try:
            with os.fdopen(descriptor, "wb") as staged:
                while chunk := data.read(READ_CHUNK):
                    if not isinstance(chunk, bytes):
                        raise TypeError("data must yield bytes")
                    staged.write(chunk)
                staged.flush()
                os.fsync(staged.fileno())

            if target.exists():
                if not self._same_bytes(target, temporary):
                    raise ArtifactConflict(f"artifact key {key!r} already stores different bytes")
            else:
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    if not self._same_bytes(target, temporary):
                        raise ArtifactConflict(
                            f"artifact key {key!r} was concurrently written with different bytes"
                        ) from None
            return self._metadata(target, key)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, key: str) -> BinaryIO:
        """Open a stored artifact for binary reading."""

        return self._path(key).open("rb")

    def exists(self, key: str) -> bool:
        """Return whether ``key`` identifies a regular stored file."""

        return self._path(key).is_file()

    def uri(self, key: str) -> str:
        """Return the stable absolute ``file:`` URI for ``key``."""

        return self._path(key).as_uri()
