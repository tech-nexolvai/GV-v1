"""Immutable local-filesystem implementation of the artifact-store contract.

Also the reference implementation of the upload ticket (#205). Read
``upload_ticket`` and ``verify_upload_ticket`` together with the honest statement of what
they do and do not enforce — the two docstrings are the guarantee, and the guarantee is
narrower here than it will be on S3 (#221).

Source: ``DESIGN_PLATFORM.md`` section 7 and issues #218 and #205.
Verification: ``tests/storage/test_local_store.py``.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final
from urllib.parse import quote

from storage.hashing import (
    SHA256_PATTERN,
    ArtifactCorrupt,
    IntegrityRecordMissing,
    sha256_stream,
)
from storage.signing import Capability, sign_capability, verify_capability
from storage.store import UPLOAD_PURPOSE, ArtifactConflict, StoredArtifact, UploadTicket

DEFAULT_ROOT: Final = Path.home() / ".gv-v1" / "artifacts"
READ_CHUNK: Final = 1024 * 1024
INTEGRITY_DIRECTORY: Final = ".gv-integrity"

#: The verb an upload ticket is signed for. One verb, because a ticket that covered several would be
#: broader than "write this object once" without anybody having decided that.
UPLOAD_METHOD: Final = "PUT"

#: Query parameter carrying the token in a ticket URL. Named after S3's convention closely enough that
#: the two backends produce recognisably the same shape.
TICKET_PARAMETER: Final = "ticket"


class TicketSigningNotConfigured(RuntimeError):
    """This store was built without a signing key, so it cannot issue or check tickets.

    Raised rather than falling back to a generated or hard-coded key. A per-process random key would
    make every ticket stop verifying at the next restart; a hard-coded one would make every deployment
    share a secret that is in the repository. Both fail quietly, which is why neither is offered.
    """


class LocalStore:
    """Store immutable artifacts beneath one configured filesystem root.

    Keys are portable, relative POSIX paths. Writes are staged beside their target and
    installed using an atomic hard link, so concurrent writers cannot overwrite a key.
    The default root is deliberately outside the repository and remains stable across
    process restarts.
    """

    def __init__(
        self, root: Path | str = DEFAULT_ROOT, *, ticket_secret: bytes | None = None
    ) -> None:
        self._root = Path(root).expanduser().resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        if ticket_secret is not None and not isinstance(ticket_secret, bytes | bytearray):
            raise TypeError("ticket_secret must be bytes")
        if ticket_secret is not None and not ticket_secret:
            raise ValueError("ticket_secret must not be empty")
        self._ticket_secret = bytes(ticket_secret) if ticket_secret is not None else None

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
        except FileNotFoundError as error:
            raise IntegrityRecordMissing(f"artifact key {key!r} has no integrity record") from error
        except UnicodeDecodeError as error:
            raise ArtifactCorrupt(
                f"artifact key {key!r} has a malformed integrity record"
            ) from error
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
        integrity = self._integrity_path(key)
        descriptor: int | None = None
        integrity_descriptor: int | None = None
        temporary: Path | None = None
        integrity_temporary: Path | None = None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(prefix=".gv-stage-", dir=target.parent)
            temporary = Path(temporary_name)
            integrity.parent.mkdir(parents=True, exist_ok=True)
            integrity_descriptor, integrity_name = tempfile.mkstemp(
                prefix=".gv-integrity-stage-", dir=integrity.parent
            )
            integrity_temporary = Path(integrity_name)

            staged_file = os.fdopen(descriptor, "wb")
            descriptor = None
            with staged_file as staged:
                while chunk := data.read(READ_CHUNK):
                    if not isinstance(chunk, bytes):
                        raise TypeError("data must yield bytes")
                    staged.write(chunk)
                staged.flush()
                os.fsync(staged.fileno())

            with temporary.open("rb") as staged:
                digest, size = sha256_stream(staged)
            record_file = os.fdopen(integrity_descriptor, "w", encoding="ascii", newline="")
            integrity_descriptor = None
            with record_file as record:
                record.write(digest)
                record.flush()
                os.fsync(record.fileno())

            if target.exists() and not self._same_bytes(target, temporary):
                raise ArtifactConflict(f"artifact key {key!r} already stores different bytes")

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

            if target.exists():
                if not self._same_bytes(target, temporary):
                    raise ArtifactConflict(f"artifact key {key!r} already stores different bytes")
            elif not self._publish(temporary, target) and not self._same_bytes(target, temporary):
                raise ArtifactConflict(
                    f"artifact key {key!r} was concurrently written with different bytes"
                ) from None

            return StoredArtifact(
                key=key,
                sha256=digest,
                size=size,
                backend_version_id=None,
            )
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if integrity_descriptor is not None:
                os.close(integrity_descriptor)
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            if integrity_temporary is not None:
                integrity_temporary.unlink(missing_ok=True)

    def get(self, key: str) -> BinaryIO:
        """Verify and open a stored artifact, raising if its bytes changed."""

        target = self._path(key)
        expected = self._read_digest(self._integrity_path(key), key)
        stored = target.open("rb")
        try:
            actual, _ = sha256_stream(stored)
            if actual != expected:
                raise ArtifactCorrupt(
                    f"artifact key {key!r} failed SHA-256 verification: "
                    f"expected {expected}, got {actual}"
                )
            stored.seek(0)
            return stored
        except BaseException:
            stored.close()
            raise

    def exists(self, key: str) -> bool:
        """Return whether ``key`` identifies a regular stored file."""

        return self._path(key).is_file() and self._integrity_path(key).is_file()

    def uri(self, key: str) -> str:
        """Return the stable absolute ``file:`` URI for ``key``."""

        return self._path(key).as_uri()

    def upload_ticket(self, key: str, *, content_type: str, expires_in: timedelta) -> UploadTicket:
        """Issue an HMAC-signed ticket permitting one write to ``key`` until it expires.

        Nothing is written and ``key`` need not exist. The token carries the key, the single purpose
        ``upload`` and the deadline, all signed, so it cannot be stretched to another object, turned
        into a download, or used after its lifetime.

        **What this enforces, and what it does not.** The token is a *verifiable statement* of what was
        authorised. A local filesystem has no gatekeeper to present it to, so a process that can
        already write under the storage root can write without a ticket, and this method cannot stop
        it. What it gives you is ``verify_upload_ticket``, so whatever does accept a write — a
        development upload shim, a test harness — can check correctly. On S3 (#221) the backend
        enforces its own signature and the same method returns a real presigned URL; the contract the
        control plane codes against does not change.

        The URL is a ``file:`` URI with the token in its query string. A filesystem ignores a query
        string, which is exactly why the enforcement note above matters and why the token is also
        returned in a form that can be checked directly.

        Raises:
            TicketSigningNotConfigured: this store was built without ``ticket_secret``.
            ValueError: the key is not a usable relative POSIX key, the content type is blank, or
                ``expires_in`` is not a positive interval.
        """

        secret = self._require_ticket_secret()
        # Validated through the same function `put` uses, so a key a ticket permits is always a key
        # the store would accept. Validating differently here is how a ticket comes to authorise a
        # write that then fails, or worse, one that escapes the root.
        target = self._path(key)
        if not isinstance(content_type, str) or not content_type.strip():
            raise ValueError("content_type must be a non-empty string")
        if not isinstance(expires_in, timedelta):
            raise TypeError("expires_in must be a timedelta")
        if expires_in <= timedelta(0):
            raise ValueError(
                "expires_in must be a positive interval — a ticket that is already expired fails as "
                "a rejected upload rather than as an error here"
            )

        expires_at = datetime.now(UTC) + expires_in
        token = sign_capability(
            secret=secret, purpose=UPLOAD_PURPOSE, key=key, expires_at=expires_at
        )
        return UploadTicket(
            key=key,
            url=f"{target.as_uri()}?{TICKET_PARAMETER}={quote(token, safe='')}",
            method=UPLOAD_METHOD,
            expires_at=expires_at,
            required_headers={"Content-Type": content_type},
        )

    def verify_upload_ticket(
        self, token: str, *, key: str, now: datetime | None = None
    ) -> Capability:
        """Check that ``token`` permits writing ``key`` right now, or raise.

        The caller names the key it is about to allow a write to; this agrees or refuses. It never
        reports what the token would prefer to be used for, because a caller acting on the token's own
        claims would be authorising whatever the token asked for.

        ``now`` defaults to the current instant and can be passed to judge expiry at a chosen moment —
        which is how the boundary is tested without moving the machine's clock.

        Raises:
            TicketSigningNotConfigured: this store was built without ``ticket_secret``.
            CapabilityInvalid: for every reason the token does not authorise this write.
        """

        secret = self._require_ticket_secret()
        return verify_capability(
            token,
            secret=secret,
            purpose=UPLOAD_PURPOSE,
            key=key,
            now=now if now is not None else datetime.now(UTC),
        )

    def _require_ticket_secret(self) -> bytes:
        if self._ticket_secret is None:
            raise TicketSigningNotConfigured(
                "this LocalStore was built without a ticket_secret, so it can neither issue nor "
                "check upload tickets. Pass one from configuration; there is deliberately no default, "
                "because a generated key stops verifying at the next restart and a hard-coded one is "
                "a shared secret committed to the repository."
            )
        return self._ticket_secret
