"""Backend-neutral contract for immutable binary artifacts.

Source: ``DESIGN_PLATFORM.md`` section 7 and issue #218.
Verification: ``tests/storage/contract.py`` and ``tests/storage/test_local_store.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import BinaryIO, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Identity and integrity metadata returned after storing exact bytes."""

    key: str
    sha256: str
    size: int
    backend_version_id: str | None


class ArtifactConflict(Exception):
    """Raised when an existing key names different bytes; overwriting is forbidden."""


@runtime_checkable
class ArtifactStore(Protocol):
    """Storage operations available without exposing a particular backend."""

    def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredArtifact:
        """Store bytes at ``key``, or return the existing identical artifact."""

        ...

    def get(self, key: str) -> BinaryIO:
        """Open the bytes stored at ``key`` for binary reading."""

        ...

    def exists(self, key: str) -> bool:
        """Return whether ``key`` identifies a stored artifact."""

        ...

    def uri(self, key: str) -> str:
        """Return a stable URI for ``key`` without requiring it to exist."""

        ...
