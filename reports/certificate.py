"""Generate the immutable, reproducible record of a package sign-off.

The certificate is canonical JSON rather than a presentation PDF.  Its purpose is to answer, years
later, who approved which immutable findings, what was dismissed or corrected, and which rule and
engine versions made each decision.  Canonical key ordering, stable row ordering and stored
timestamps make the bytes reproducible; no clock, random value or current rule state enters here.

The returned dataclasses are frozen and the issued bytes are written through the immutable artifact
store under their own SHA-256.  Those are separate guarantees: Python prevents accidental mutation
in memory, while content-addressed storage prevents later bytes replacing the issued record.

Source: issue #232 · Design: ``docs/DESIGN_PRODUCT.md`` §4.3 ·
Verification: ``tests/reports/test_certificate.py``
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Final, Literal
from uuid import UUID

from storage.hashing import content_key
from storage.store import ArtifactStore, StoredArtifact

CERTIFICATE_CONTENT_TYPE: Final = "application/json"
Resolution = Literal["accepted", "dismissed"]

__all__ = [
    "CERTIFICATE_CONTENT_TYPE",
    "CertificateCorrection",
    "CertificateFinding",
    "CertificateInput",
    "GeneratedCertificate",
    "generate_certificate",
    "issue_certificate",
]


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _instant(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class CertificateFinding:
    """One immutable finding revision and the reviewer's disposition of it."""

    finding_id: UUID
    outcome: str
    severity: str
    rule_snapshot_id: str
    engine_version: str
    resolution: Resolution
    resolved_by: str
    resolved_at: datetime
    note: str | None = None

    def __post_init__(self) -> None:
        _text(self.outcome, "outcome")
        _text(self.severity, "severity")
        _text(self.rule_snapshot_id, "rule_snapshot_id")
        _text(self.engine_version, "engine_version")
        if self.resolution not in ("accepted", "dismissed"):
            raise ValueError("resolution must be 'accepted' or 'dismissed'")
        _text(self.resolved_by, "resolved_by")
        _instant(self.resolved_at, "resolved_at")
        if self.note is not None and not self.note.strip():
            raise ValueError("note must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class CertificateCorrection:
    """One append-only correction included verbatim in the sign-off record."""

    action_id: UUID
    finding_id: UUID
    corrected_by: str
    corrected_at: datetime
    original_value: str
    corrected_value: str

    def __post_init__(self) -> None:
        _text(self.corrected_by, "corrected_by")
        _instant(self.corrected_at, "corrected_at")
        _text(self.original_value, "original_value")
        _text(self.corrected_value, "corrected_value")
        if self.original_value == self.corrected_value:
            raise ValueError("a correction must change the value")


@dataclass(frozen=True, slots=True)
class CertificateInput:
    """Stored facts needed to reproduce one approved package certificate."""

    approval_id: UUID
    package_revision_id: UUID
    reviewed_by: str
    review_started_at: datetime
    review_completed_at: datetime
    approved_at: datetime
    code_version: str
    findings: tuple[CertificateFinding, ...]
    corrections: tuple[CertificateCorrection, ...] = ()

    def __post_init__(self) -> None:
        _text(self.reviewed_by, "reviewed_by")
        _text(self.code_version, "code_version")
        started = _instant(self.review_started_at, "review_started_at")
        completed = _instant(self.review_completed_at, "review_completed_at")
        approved = _instant(self.approved_at, "approved_at")
        if completed < started:
            raise ValueError("review_completed_at cannot precede review_started_at")
        if approved < started:
            raise ValueError("approved_at cannot precede review_started_at")
        if not isinstance(self.findings, tuple) or not all(
            isinstance(item, CertificateFinding) for item in self.findings
        ):
            raise TypeError("findings must be a tuple of CertificateFinding values")
        if not isinstance(self.corrections, tuple) or not all(
            isinstance(item, CertificateCorrection) for item in self.corrections
        ):
            raise TypeError("corrections must be a tuple of CertificateCorrection values")
        if not self.findings:
            raise ValueError("a review certificate must contain at least one finding")
        finding_ids = [finding.finding_id for finding in self.findings]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("a finding may appear only once in a certificate")
        allowed = set(finding_ids)
        outside = [item.finding_id for item in self.corrections if item.finding_id not in allowed]
        if outside:
            raise ValueError("every correction must refer to a finding in the certificate")


@dataclass(frozen=True, slots=True)
class GeneratedCertificate:
    """Exact certificate bytes and their independently checkable content identity."""

    document: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.document, bytes):
            raise TypeError("document must be bytes")
        if hashlib.sha256(self.document).hexdigest() != self.sha256:
            raise ValueError("certificate bytes do not match their SHA-256")

    def content_matches(self) -> bool:
        """Return whether the bytes still match the identity assigned at generation."""
        return hashlib.sha256(self.document).hexdigest() == self.sha256


def _finding_record(finding: CertificateFinding) -> dict[str, object]:
    return {
        "engine_version": finding.engine_version,
        "finding_id": str(finding.finding_id),
        "note": finding.note,
        "outcome": finding.outcome,
        "resolved_at": _timestamp(finding.resolved_at),
        "resolved_by": finding.resolved_by,
        "rule_snapshot_id": finding.rule_snapshot_id,
        "severity": finding.severity,
    }


def generate_certificate(source: CertificateInput) -> GeneratedCertificate:
    """Render canonical bytes using only stored facts supplied by the caller."""
    if not isinstance(source, CertificateInput):
        raise TypeError("source must be a CertificateInput")
    accepted = sorted(
        (item for item in source.findings if item.resolution == "accepted"),
        key=lambda x: str(x.finding_id),
    )
    dismissed = sorted(
        (item for item in source.findings if item.resolution == "dismissed"),
        key=lambda x: str(x.finding_id),
    )
    corrections = sorted(source.corrections, key=lambda x: (str(x.finding_id), str(x.action_id)))
    payload = {
        "accepted_findings": [_finding_record(item) for item in accepted],
        "approval": {
            "approval_id": str(source.approval_id),
            "approved_at": _timestamp(source.approved_at),
            "code_version": source.code_version,
            "package_revision_id": str(source.package_revision_id),
            "review_completed_at": _timestamp(source.review_completed_at),
            "review_started_at": _timestamp(source.review_started_at),
            "reviewed_by": source.reviewed_by,
        },
        "certificate_format": "gv-review-certificate-v1",
        "corrections": [
            {
                "action_id": str(item.action_id),
                "corrected_at": _timestamp(item.corrected_at),
                "corrected_by": item.corrected_by,
                "corrected_value": item.corrected_value,
                "finding_id": str(item.finding_id),
                "original_value": item.original_value,
            }
            for item in corrections
        ],
        # A separate top-level collection, not a flag buried among accepted rows. This makes a
        # dismissal as visible to a reader and a parser as an accepted finding.
        "dismissed_findings": [_finding_record(item) for item in dismissed],
    }
    document = (
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    return GeneratedCertificate(document, hashlib.sha256(document).hexdigest())


def issue_certificate(source: CertificateInput, store: ArtifactStore) -> StoredArtifact:
    """Generate and immutably store one certificate under its content hash."""
    if not isinstance(store, ArtifactStore):
        raise TypeError("store must implement the ArtifactStore protocol")
    generated = generate_certificate(source)
    key = content_key(
        f"certificates/{source.package_revision_id}/{source.approval_id}",
        generated.sha256,
        suffix=".json",
    )
    return store.put(key, BytesIO(generated.document), content_type=CERTIFICATE_CONTENT_TYPE)
