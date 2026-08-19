"""Backend-neutral immutable artifact storage."""

from storage.hashing import (
    ArtifactCorrupt,
    IntegrityRecordMissing,
    content_key,
    sha256_stream,
)
from storage.local import LocalStore, TicketSigningNotConfigured
from storage.signing import Capability, CapabilityInvalid, sign_capability, verify_capability
from storage.store import (
    UPLOAD_PURPOSE,
    ArtifactConflict,
    ArtifactStore,
    StoredArtifact,
    UploadTicket,
)

__all__ = [
    "UPLOAD_PURPOSE",
    "ArtifactConflict",
    "ArtifactCorrupt",
    "ArtifactStore",
    "Capability",
    "CapabilityInvalid",
    "IntegrityRecordMissing",
    "LocalStore",
    "StoredArtifact",
    "TicketSigningNotConfigured",
    "UploadTicket",
    "content_key",
    "sha256_stream",
    "sign_capability",
    "verify_capability",
]
