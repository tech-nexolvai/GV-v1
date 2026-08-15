"""Streamed SHA-256 and deterministic content-addressed artifact keys.

Source: ``DESIGN_PLATFORM.md`` section 7 and issue #219.
Verification: ``tests/storage/test_hashing.py``.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import PurePosixPath
from typing import BinaryIO, Final

CHUNK: Final = 1024 * 1024
SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


class ArtifactCorrupt(Exception):
    """Raised when stored bytes do not match their recorded SHA-256."""


def sha256_stream(data: BinaryIO) -> tuple[str, int]:
    """Return the hex SHA-256 and byte count without loading the object into memory.

    Hashing starts at the stream's current position and consumes it through EOF. The
    caller owns rewinding or closing the stream.
    """

    if not hasattr(data, "read"):
        raise TypeError("data must be a binary file-like object")
    digest = hashlib.sha256()
    size = 0
    while chunk := data.read(CHUNK):
        if not isinstance(chunk, bytes):
            raise TypeError("data must yield bytes")
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def content_key(namespace: str, sha256: str, *, suffix: str) -> str:
    """Return a portable content-addressed key partitioned by hash prefix."""

    if not isinstance(namespace, str):
        raise TypeError("namespace must be a string")
    if not namespace or "\\" in namespace:
        raise ValueError("namespace must be a non-empty POSIX relative path")
    parsed = PurePosixPath(namespace)
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parsed.parts):
        raise ValueError("namespace must not be absolute or contain '.' or '..'")
    if not isinstance(sha256, str) or SHA256_PATTERN.fullmatch(sha256) is None:
        raise ValueError("sha256 must be exactly 64 lowercase hexadecimal characters")
    if not isinstance(suffix, str):
        raise TypeError("suffix must be a string")
    if suffix and (
        not suffix.startswith(".") or suffix in {".", ".."} or "/" in suffix or "\\" in suffix
    ):
        raise ValueError("suffix must be empty or a dot-prefixed extension without separators")

    prefix = "/".join(parsed.parts)
    return f"{prefix}/{sha256[:2]}/{sha256[2:4]}/{sha256}{suffix}"
