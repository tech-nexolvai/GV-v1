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
    DOWNLOAD_PURPOSE,
    UPLOAD_PURPOSE,
    ArtifactConflict,
    ArtifactStore,
    StoredArtifact,
    UploadTicket,
)
from storage.urls import (
    DEFAULT_EVIDENCE_URL_LIFETIME,
    EvidenceUrl,
    EvidenceUrlAuditEvent,
    EvidenceUrlIssuer,
)

__all__ = [
    "DEFAULT_EVIDENCE_URL_LIFETIME",
    "DOWNLOAD_PURPOSE",
    "UPLOAD_PURPOSE",
    "ArtifactConflict",
    "ArtifactCorrupt",
    "ArtifactStore",
    "Capability",
    "CapabilityInvalid",
    "EvidenceUrl",
    "EvidenceUrlAuditEvent",
    "EvidenceUrlIssuer",
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
