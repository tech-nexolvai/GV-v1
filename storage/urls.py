"""Short-lived, single-artifact evidence download capabilities.

The artifact remains stored after a URL expires; only the temporary permission to download it ends.
Generated URLs are deliberately absent from audit records and object representations.

Source: ``docs/DESIGN_CONTROLS.md`` section 2.4 and issue #254.
Verification: ``tests/storage/test_urls.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol
from urllib.parse import quote, urlencode

from storage.signing import sign_capability
from storage.store import DOWNLOAD_PURPOSE

DEFAULT_EVIDENCE_URL_LIFETIME: Final = timedelta(minutes=5)
"""Operational default for an immediately consumed reviewer URL, not artifact retention."""

CAPABILITY_PARAMETER: Final = "capability"


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _aware(value: object, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value


def _lifetime(value: object) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError("lifetime must be a timedelta")
    if value <= timedelta(0):
        raise ValueError("lifetime must be positive")
    return value


@dataclass(frozen=True, slots=True)
class EvidenceUrlAuditEvent:
    """URL-free record of who received access to which artifact and why."""

    actor_id: str
    artifact_id: str
    storage_key: str
    purpose: str
    issued_at: datetime
    expires_at: datetime
    lifetime: timedelta

    def __post_init__(self) -> None:
        _text(self.actor_id, "actor_id")
        _text(self.artifact_id, "artifact_id")
        _text(self.storage_key, "storage_key")
        if self.purpose != DOWNLOAD_PURPOSE:
            raise ValueError(f"evidence URL purpose must be {DOWNLOAD_PURPOSE!r}")
        _aware(self.issued_at, "issued_at")
        _aware(self.expires_at, "expires_at")
        _lifetime(self.lifetime)
        if self.expires_at != self.issued_at + self.lifetime:
            raise ValueError("expires_at must equal issued_at plus lifetime")


class EvidenceUrlAuditRecorder(Protocol):
    """Persistence boundary; implementations own their transaction and audit storage."""

    def record(self, event: EvidenceUrlAuditEvent) -> None:
        """Persist one issuance event without receiving the URL or token."""


@dataclass(frozen=True, slots=True)
class EvidenceUrl:
    """One temporary download permission; its representation never reveals the URL."""

    artifact_id: str
    purpose: str
    issued_at: datetime
    expires_at: datetime
    url: str = field(repr=False)

    def __post_init__(self) -> None:
        _text(self.artifact_id, "artifact_id")
        if self.purpose != DOWNLOAD_PURPOSE:
            raise ValueError(f"evidence URL purpose must be {DOWNLOAD_PURPOSE!r}")
        _aware(self.issued_at, "issued_at")
        _aware(self.expires_at, "expires_at")
        _text(self.url, "url")
        if self.expires_at <= self.issued_at:
            raise ValueError("an evidence URL must expire after it is issued")


class EvidenceUrlIssuer:
    """Issue audited HMAC capabilities over backend-provided artifact URIs.

    The capability proves scope and expiry. The storage/API layer serving the URI must call
    ``verify_capability`` before returning bytes; this class cannot enforce a server it does not own.
    """

    def __init__(
        self,
        *,
        secret: bytes,
        artifact_uri: Callable[[str], str],
        recorder: EvidenceUrlAuditRecorder,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lifetime: timedelta = DEFAULT_EVIDENCE_URL_LIFETIME,
    ) -> None:
        if not isinstance(secret, bytes):
            raise TypeError("secret must be bytes")
        if not secret:
            raise ValueError("secret must not be empty")
        self._secret = secret
        self._artifact_uri = artifact_uri
        self._recorder = recorder
        self._clock = clock
        self._lifetime = _lifetime(lifetime)

    def issue(self, *, actor_id: str, artifact_id: str, storage_key: str) -> EvidenceUrl:
        """Issue one audited download URL or return nothing if audit persistence fails."""

        actor_id = _text(actor_id, "actor_id")
        artifact_id = _text(artifact_id, "artifact_id")
        storage_key = _text(storage_key, "storage_key")
        issued_at = _aware(self._clock(), "clock result")
        expires_at = issued_at + self._lifetime
        token = sign_capability(
            secret=self._secret,
            purpose=DOWNLOAD_PURPOSE,
            key=storage_key,
            expires_at=expires_at,
        )
        base_url = _text(self._artifact_uri(storage_key), "artifact URI")
        separator = "&" if "?" in base_url else "?"
        url = f"{base_url}{separator}{urlencode({CAPABILITY_PARAMETER: token}, quote_via=quote)}"
        event = EvidenceUrlAuditEvent(
            actor_id=actor_id,
            artifact_id=artifact_id,
            storage_key=storage_key,
            purpose=DOWNLOAD_PURPOSE,
            issued_at=issued_at,
            expires_at=expires_at,
            lifetime=self._lifetime,
        )
        self._recorder.record(event)
        return EvidenceUrl(artifact_id, DOWNLOAD_PURPOSE, issued_at, expires_at, url)
