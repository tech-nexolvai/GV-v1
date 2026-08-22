"""The permanent, reproducible package sign-off record (#232, D4.4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from typing import BinaryIO
from uuid import UUID

import pytest

from reports.certificate import (
    CertificateCorrection,
    CertificateFinding,
    CertificateInput,
    generate_certificate,
    issue_certificate,
)
from storage.store import StoredArtifact, UploadTicket

APPROVAL_ID = UUID("10000000-0000-0000-0000-000000000001")
REVISION_ID = UUID("20000000-0000-0000-0000-000000000002")
ACCEPTED_ID = UUID("30000000-0000-0000-0000-000000000003")
DISMISSED_ID = UUID("40000000-0000-0000-0000-000000000004")
ACTION_ID = UUID("50000000-0000-0000-0000-000000000005")
STARTED = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _finding(
    finding_id: UUID, resolution: str, *, engine: str = "verdict-abc123"
) -> CertificateFinding:
    return CertificateFinding(
        finding_id=finding_id,
        outcome="FAIL",
        severity="CRITICAL",
        rule_snapshot_id=f"sha256:{str(finding_id).replace('-', ''):0<64}",
        engine_version=engine,
        resolution=resolution,  # type: ignore[arg-type]
        resolved_by="anant",
        resolved_at=STARTED + timedelta(minutes=5),
        note="reviewed against the approved architectural set",
    )


def _source(*, findings: tuple[CertificateFinding, ...] | None = None) -> CertificateInput:
    selected = findings or (_finding(ACCEPTED_ID, "accepted"), _finding(DISMISSED_ID, "dismissed"))
    return CertificateInput(
        approval_id=APPROVAL_ID,
        package_revision_id=REVISION_ID,
        reviewed_by="anant",
        review_started_at=STARTED,
        review_completed_at=STARTED + timedelta(minutes=20),
        approved_at=STARTED + timedelta(minutes=20),
        code_version="gv-1.8.0+abc123",
        findings=selected,
        corrections=(
            CertificateCorrection(
                action_id=ACTION_ID,
                finding_id=ACCEPTED_ID,
                corrected_by="anant",
                corrected_at=STARTED + timedelta(minutes=10),
                original_value='{"num":3,"den":8}',
                corrected_value='{"num":1,"den":2}',
            ),
        ),
    )


def _payload(source: CertificateInput) -> dict[str, object]:
    return json.loads(generate_certificate(source).document)


def test_certificate_names_rules_engine_and_code_versions() -> None:
    """Input: approved findings. Output: the exact historic rule and executable versions."""
    payload = _payload(_source())
    accepted = payload["accepted_findings"]
    assert isinstance(accepted, list)
    assert accepted[0]["rule_snapshot_id"].startswith("sha256:")
    assert accepted[0]["engine_version"] == "verdict-abc123"
    assert payload["approval"]["code_version"] == "gv-1.8.0+abc123"


def test_dismissed_findings_are_a_peer_of_accepted_findings() -> None:
    """Input: one accepted and one dismissed. Output: equal top-level collections, neither hidden."""
    payload = _payload(_source())
    assert len(payload["accepted_findings"]) == 1
    assert len(payload["dismissed_findings"]) == 1
    assert payload["dismissed_findings"][0]["finding_id"] == str(DISMISSED_ID)


def test_corrections_preserve_original_and_corrected_values() -> None:
    """Input: one ledger correction. Output: both exact authored strings and the named human."""
    correction = _payload(_source())["corrections"][0]
    assert correction["original_value"] == '{"num":3,"den":8}'
    assert correction["corrected_value"] == '{"num":1,"den":2}'
    assert correction["corrected_by"] == "anant"


def test_regeneration_is_byte_for_byte_identical_despite_input_order() -> None:
    """Input: identical stored rows in opposite order. Output: identical document and SHA-256."""
    first = _source()
    second = _source(findings=tuple(reversed(first.findings)))
    generated_first = generate_certificate(first)
    generated_second = generate_certificate(second)
    assert generated_first.document == generated_second.document
    assert generated_first.sha256 == generated_second.sha256
    assert generated_first.content_matches()


class MemoryStore:
    """Minimal immutable store double; filesystem atomicity is storage/local.py's own contract."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, data: BinaryIO, *, content_type: str) -> StoredArtifact:
        assert content_type == "application/json"
        document = data.read()
        existing = self.objects.setdefault(key, document)
        assert existing == document
        return StoredArtifact(key, sha256(document).hexdigest(), len(document), None)

    def get(self, key: str) -> BinaryIO:
        return BytesIO(self.objects[key])

    def exists(self, key: str) -> bool:
        return key in self.objects

    def uri(self, key: str) -> str:
        return f"memory://{key}"

    def upload_ticket(self, key: str, *, content_type: str, expires_in: timedelta) -> UploadTicket:
        raise NotImplementedError((key, content_type, expires_in))


def test_issued_certificate_is_content_addressed_and_idempotent() -> None:
    """Input: same approval issued twice. Output: one immutable key carrying the certificate hash."""
    store = MemoryStore()
    first = issue_certificate(_source(), store)
    second = issue_certificate(_source(), store)
    assert first == second
    assert first.key.endswith(f"{first.sha256}.json")


def test_naive_timestamps_are_refused() -> None:
    """Input: timezone-less approval time. Output: refusal instead of a machine-dependent document."""
    source = _source()
    with pytest.raises(ValueError, match="timezone-aware"):
        CertificateInput(
            approval_id=source.approval_id,
            package_revision_id=source.package_revision_id,
            reviewed_by=source.reviewed_by,
            review_started_at=source.review_started_at,
            review_completed_at=source.review_completed_at,
            approved_at=STARTED.replace(tzinfo=None),
            code_version=source.code_version,
            findings=source.findings,
            corrections=source.corrections,
        )


def test_a_correction_cannot_name_a_finding_outside_the_certificate() -> None:
    """Input: correction for an unapproved finding. Output: refusal, never an orphan ledger entry."""
    source = _source()
    correction = CertificateCorrection(
        action_id=ACTION_ID,
        finding_id=UUID("60000000-0000-0000-0000-000000000006"),
        corrected_by="anant",
        corrected_at=STARTED,
        original_value="wrong",
        corrected_value="right",
    )
    with pytest.raises(ValueError, match="finding in the certificate"):
        CertificateInput(
            approval_id=source.approval_id,
            package_revision_id=source.package_revision_id,
            reviewed_by=source.reviewed_by,
            review_started_at=source.review_started_at,
            review_completed_at=source.review_completed_at,
            approved_at=source.approved_at,
            code_version=source.code_version,
            findings=source.findings,
            corrections=(correction,),
        )
