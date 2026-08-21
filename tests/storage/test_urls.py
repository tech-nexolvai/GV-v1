"""Short-lived evidence URL tests for issue #254."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from storage.signing import CapabilityInvalid, verify_capability
from storage.store import DOWNLOAD_PURPOSE, UPLOAD_PURPOSE
from storage.urls import (
    CAPABILITY_PARAMETER,
    DEFAULT_EVIDENCE_URL_LIFETIME,
    EvidenceUrlAuditEvent,
    EvidenceUrlIssuer,
)

NOW = datetime(2026, 8, 21, 10, 30, tzinfo=UTC)
SECRET = b"test-only-evidence-url-secret"


class RecordingAudit:
    def __init__(self, *, fail: bool = False) -> None:
        self.events: list[EvidenceUrlAuditEvent] = []
        self.fail = fail

    def record(self, event: EvidenceUrlAuditEvent) -> None:
        if self.fail:
            raise RuntimeError("audit unavailable")
        self.events.append(event)


def _issuer(
    recorder: RecordingAudit, *, lifetime: timedelta = DEFAULT_EVIDENCE_URL_LIFETIME
) -> EvidenceUrlIssuer:
    return EvidenceUrlIssuer(
        secret=SECRET,
        artifact_uri=lambda key: f"https://evidence.invalid/{key}",
        recorder=recorder,
        clock=lambda: NOW,
        lifetime=lifetime,
    )


def _token(url: str) -> str:
    return parse_qs(urlsplit(url).query)[CAPABILITY_PARAMETER][0]


def test_default_url_is_scoped_and_expires_after_five_minutes() -> None:
    """Input: one crop. Output: 5-minute download. Why: leaked access is short-lived."""

    issued = _issuer(RecordingAudit()).issue(
        actor_id="reviewer-7", artifact_id="artifact-4", storage_key="crop/a.png"
    )

    assert issued.expires_at == NOW + timedelta(minutes=5)
    verify_capability(
        _token(issued.url),
        secret=SECRET,
        purpose=DOWNLOAD_PURPOSE,
        key="crop/a.png",
        now=issued.expires_at - timedelta(microseconds=1),
    )
    with pytest.raises(CapabilityInvalid):
        verify_capability(
            _token(issued.url),
            secret=SECRET,
            purpose=DOWNLOAD_PURPOSE,
            key="crop/a.png",
            now=issued.expires_at,
        )


def test_capability_cannot_be_retargeted_or_used_for_upload() -> None:
    """Input: altered key/purpose. Output: refusal. Why: one URL grants one operation."""

    issued = _issuer(RecordingAudit()).issue(
        actor_id="reviewer-7", artifact_id="artifact-4", storage_key="crop/a.png"
    )
    token = _token(issued.url)

    with pytest.raises(CapabilityInvalid):
        verify_capability(
            token,
            secret=SECRET,
            purpose=DOWNLOAD_PURPOSE,
            key="crop/b.png",
            now=NOW,
        )
    with pytest.raises(CapabilityInvalid):
        verify_capability(token, secret=SECRET, purpose=UPLOAD_PURPOSE, key="crop/a.png", now=NOW)


def test_configured_lifetime_and_url_free_audit_record_are_exact() -> None:
    """Input: 90-second policy. Output: matching audit. Why: configuration changes are visible."""

    audit = RecordingAudit()
    issued = _issuer(audit, lifetime=timedelta(seconds=90)).issue(
        actor_id="reviewer-7", artifact_id="artifact-4", storage_key="crop/a.png"
    )

    assert audit.events == [
        EvidenceUrlAuditEvent(
            actor_id="reviewer-7",
            artifact_id="artifact-4",
            storage_key="crop/a.png",
            purpose=DOWNLOAD_PURPOSE,
            issued_at=NOW,
            expires_at=NOW + timedelta(seconds=90),
            lifetime=timedelta(seconds=90),
        )
    ]
    assert "url" not in asdict(audit.events[0])
    assert "capability" not in repr(audit.events[0]).casefold()
    assert issued.url not in repr(issued)


def test_audit_failure_prevents_a_url_from_being_returned() -> None:
    """Input: unavailable audit sink. Output: exception. Why: unaudited access is never handed out."""

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _issuer(RecordingAudit(fail=True)).issue(
            actor_id="reviewer-7", artifact_id="artifact-4", storage_key="crop/a.png"
        )


@pytest.mark.parametrize("lifetime", [timedelta(0), timedelta(seconds=-1)])
def test_non_positive_lifetimes_are_refused(lifetime: timedelta) -> None:
    """Input: dead/negative lifetime. Output: refusal. Why: expiry must be meaningful."""

    with pytest.raises(ValueError, match="positive"):
        _issuer(RecordingAudit(), lifetime=lifetime)


def test_naive_clock_is_refused() -> None:
    """Input: timezone-free clock. Output: refusal. Why: expiry must use one known timeline."""

    issuer = EvidenceUrlIssuer(
        secret=SECRET,
        artifact_uri=lambda key: f"https://evidence.invalid/{key}",
        recorder=RecordingAudit(),
        clock=lambda: datetime(2026, 8, 21, 10, 30),  # noqa: DTZ001 - refusal fixture
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        issuer.issue(actor_id="reviewer-7", artifact_id="artifact-4", storage_key="crop/a.png")
