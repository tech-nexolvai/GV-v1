"""Backend-neutral contract for immutable binary artifacts.

Source: ``DESIGN_PLATFORM.md`` section 7 and issues #218 and #205.
Verification: ``tests/storage/contract.py`` and ``tests/storage/test_local_store.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO, Final, Protocol, runtime_checkable

#: The one thing an upload ticket permits. Named here, beside the contract, so the download side
#: (#254) adds a sibling constant rather than a second spelling of this one — the purpose is compared
#: exactly, so ``"upload"`` and ``"uploads"`` would be two incompatible vocabularies.
UPLOAD_PURPOSE: Final = "upload"
#: The only operation an evidence URL permits. Kept beside ``UPLOAD_PURPOSE`` so signing and
#: verification cannot drift onto two spellings for the same capability.
DOWNLOAD_PURPOSE: Final = "download"


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Identity and integrity metadata returned after storing exact bytes."""

    key: str
    sha256: str
    size: int
    backend_version_id: str | None


@dataclass(frozen=True, slots=True)
class UploadTicket:
    """Permission to write one artifact, for a bounded time, and nothing else.

    This is what lets the control plane stay out of the byte path (``DESIGN_PLATFORM.md`` §4.1 and
    §4.2, C2.6): the API hands back a ticket and the client writes to storage directly, so no request
    body ever carries a file.

    Three properties, and each is a deliberate narrowing rather than a convenience:

    * **Scoped** — ``key`` is one key. A ticket cannot be re-aimed at another object.
    * **Single-purpose** — it permits a write and nothing else. It is not a download URL and cannot be
      turned into one; reading is a separate issuance with its own audit trail (#254).
    * **Expiring** — ``expires_at`` is a timezone-aware instant after which it is refused.

    ``method`` and ``required_headers`` are part of the ticket because a signature normally covers
    them: sending the same bytes with a different verb or a different ``Content-Type`` is a different
    request, and on a backend that signs headers it will simply be rejected. Replay them exactly.

    **What the ticket does not promise.** It says what was authorised; whether that is *enforced*
    belongs to the backend. S3 checks its own signature. A local filesystem has nothing to check one,
    so on that backend the ticket is a verifiable statement and the enforcement lives with whatever
    accepts the write — see ``storage/local.py``.
    """

    key: str
    url: str
    method: str
    """The HTTP verb the ticket was signed for, e.g. ``PUT``."""

    expires_at: datetime
    """Timezone-aware. A naive instant would be compared against a different clock than it was
    written by, and the direction of that mistake is a ticket that outlives its lifetime."""

    required_headers: Mapping[str, str]
    """Headers the write must carry verbatim. Empty is legitimate; ``None`` is not, because a caller
    would then have to distinguish "no headers" from "we forgot to say"."""

    purpose: str = UPLOAD_PURPOSE
    """What this ticket permits. Present as a field rather than left implicit so the value travels
    with the ticket into the signature, and so ``__post_init__`` has something to refuse."""

    def __post_init__(self) -> None:
        """Enforce the three properties the docstring above claims.

        Added because the first version of this type claimed to be scoped, single-purpose and expiring
        and checked none of the three. A frozen dataclass whose invariants live only in prose is a
        promise to whoever reads the docstring and nothing at all to whoever constructs it — and this
        one is constructed by every storage backend, including ones not written yet.
        """
        for name, value in (("key", self.key), ("url", self.url), ("method", self.method)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"an upload ticket needs a non-empty {name}")
        if self.purpose != UPLOAD_PURPOSE:
            raise ValueError(
                f"an upload ticket's purpose is {UPLOAD_PURPOSE!r}, not {self.purpose!r}. Reading is a "
                "separate issuance with its own audit trail (#254); a ticket that could be turned "
                "into a download would route confidential drawings around it."
            )
        if not isinstance(self.expires_at, datetime):
            raise TypeError("expires_at must be a datetime")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError(
                "expires_at must be timezone-aware. A naive instant gets compared against a "
                "different clock than it was written by, and that mistake runs one way: a ticket "
                "that outlives its lifetime."
            )


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

    def delete(self, key: str) -> bool:
        """Remove the bytes at ``key``. Returns whether anything was there to remove.

        For retention (`app/retention/policy.py`), which is the only thing that should call it. The
        bytes expire; the **row** describing them does not — `SourceArtifact` and `EvidenceArtifact`
        carry `Immutable`, so the record that an artifact existed, and its hash, survive the
        deletion of its content. A retention policy that erased the record along with the bytes
        would destroy the audit trail it is supposed to be operating under.

        Returns `False` rather than raising when the key is already gone. Retention is re-run on a
        schedule and interrupted halfway more often than anyone plans for, so "already deleted" is
        the ordinary case on the second pass, not an error.
        """

        ...

    def upload_ticket(self, key: str, *, content_type: str, expires_in: timedelta) -> UploadTicket:
        """Issue permission to write ``key`` directly, valid for ``expires_in``.

        The control plane calls this instead of accepting a file, which is what keeps the byte path
        out of the API entirely (``DESIGN_PLATFORM.md`` §4.1). The returned ticket permits a write to
        that one key and nothing else, and it stops working when it expires.

        ``expires_in`` must be a positive interval. A zero or negative one would mint a ticket that is
        already dead, and the caller would find out as a failed upload rather than as an error here.

        The key does not have to exist, and issuing a ticket writes nothing: a ticket is an intention,
        and an intention that never turns into an upload leaves no trace to clean up.

        Raises:
            ValueError: the key or content type is unusable, or ``expires_in`` is not positive.
        """

        ...
