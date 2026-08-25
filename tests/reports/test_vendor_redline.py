"""Only approved content leaves the building.

Source: ADR-0010; `AGENTS.md` §2.6 · Design: `docs/DESIGN_PRODUCT.md` §3.3 ·
Verification: ``reports/publication.py``.

The failure this file exists to prevent has no symptom. An unapproved finding in a vendor redline
looks exactly like an approved one; a *dropped* unapproved finding looks exactly like a check that
passed. Nobody downstream can tell, which is why the check has to be structural and why the wrong
answer is to filter.

Two tests carry the rest:

**A vendor render cannot be produced from an unapproved finding.** Asserted through the real render
path, ending in the stored artifact — not by unit-testing the predicate and trusting the wiring.

**The renderer itself refuses without clearance.** That is what makes `render_vendor_redline` the
only route rather than the polite one, so a caller reaching for `render_redline` directly cannot
issue a vendor document by accident.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from io import BytesIO
from pathlib import Path
from typing import Final
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.db.session import session_factory, unit_of_work
from app.models.package import Package, PackageRevision, PackageState, Project
from app.models.review import Approval, ApprovedFinding
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.verdicts import CheckRun
from app.models.verdicts import Finding as StoredFinding
from evidence.coordinates import PageTransform
from reports.publication import (
    IdentifiedFinding,
    SignedOff,
    UnapprovedContent,
    assert_vendor_safe,
    derived_expectations,
    render_vendor_redline,
    sign_off,
)
from reports.redline import (
    RedlinePackage,
    RedlinePage,
    ReportMode,
    VendorApprovalUnavailable,
    VendorClearance,
    render_redline,
)
from storage.local import LocalStore
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity
from verdict.trace import CalculationTrace, TracedOperand

pytest_plugins = ("tests.app.postgres_fixture",)

DOCUMENT: Final = UUID("11111111-1111-1111-1111-111111111111")
REVISION: Final = UUID("22222222-2222-2222-2222-222222222222")
APPROVAL: Final = UUID("33333333-3333-3333-3333-333333333333")
APPROVED_AT: Final = datetime(2026, 8, 25, 9, 30, tzinfo=UTC)

WIDTH: Final = Decimal(400)
HEIGHT: Final = Decimal(200)
BOX: Final = (Decimal(0), Decimal(0), WIDTH, HEIGHT)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sessions(postgres_engine: Engine) -> sessionmaker[Session]:
    Base.metadata.create_all(postgres_engine)
    return session_factory(postgres_engine)


def _source_pdf() -> bytes:
    buffer = BytesIO()
    canvas = Canvas(buffer, pagesize=(float(WIDTH), float(HEIGHT)), invariant=1)
    canvas.setFont("Helvetica", 10)
    canvas.drawString(20, 100, "SHEET A-101")
    canvas.showPage()
    canvas.save()
    buffer.seek(0)

    writer = PdfWriter()
    writer.add_page(PdfReader(buffer).pages[0])
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _package(revision: UUID = REVISION) -> RedlinePackage:
    return RedlinePackage(
        package_revision_id=revision,
        source_pdf=_source_pdf(),
        pages=(
            RedlinePage(
                document_version_id=DOCUMENT,
                page=0,
                source_index=0,
                transform=PageTransform(dpi=72, rotation=0, media_box=BOX, crop_box=BOX),
            ),
        ),
    )


def _trace(*, intermediates: tuple[tuple[str, object], ...] = ()) -> CalculationTrace:
    return CalculationTrace(
        operation="sum_within_tolerance",
        operands=(
            TracedOperand(name="cabinet_1", value=Fraction(2400), source="SHOP", evidence_ref=None),
            TracedOperand(name="cabinet_2", value=Fraction(3610), source="SHOP", evidence_ref=None),
        ),
        intermediates=intermediates,
        comparison="6010 vs 6012",
        tolerance=None,
        arithmetic_unit=None,
        outcome=Outcome.FAIL,
        engine_version="test",
        operation_version="1",
    )


def _finding(
    rule_id: str = "CT-WIDTH-001",
    *,
    outcome: Outcome = Outcome.FAIL,
    intermediates: tuple[tuple[str, object], ...] = (),
) -> Finding:
    return Finding(
        rule_id=rule_id,
        outcome=outcome,
        severity=Severity.CRITICAL,
        reason="the countertop is 2 mm wider than the cabinets beneath it",
        snapshot_id="snapshot-0000000000",
        engine_version="test",
        trace=_trace(intermediates=intermediates) if outcome is not Outcome.NOT_FOUND else None,
    )


def _identified(*findings: Finding) -> tuple[IdentifiedFinding, ...]:
    return tuple(IdentifiedFinding(finding_id=uuid4(), finding=finding) for finding in findings)


def _signed_off(
    *items: IdentifiedFinding,
    revision: UUID = REVISION,
    approved_by: str = "anant",
) -> SignedOff:
    return SignedOff(
        approval_id=APPROVAL,
        package_revision_id=revision,
        approved_by=approved_by,
        approved_at=APPROVED_AT,
        finding_ids=frozenset(item.finding_id for item in items),
    )


def _text(store_root: Path, key: str) -> str:
    """All the words in the finished redline, with the line breaks taken out."""
    with LocalStore(store_root).get(key) as handle:
        reader = PdfReader(handle)
        return " ".join(" ".join(page.extract_text().split()) for page in reader.pages)


# ---------------------------------------------------------------------------
# A vendor redline cannot be produced from unapproved findings
# ---------------------------------------------------------------------------


def test_an_unapproved_finding_cannot_appear_in_a_vendor_render(tmp_path: Path) -> None:
    """The acceptance criterion, asserted through the real render path.

    Not through the predicate alone: `assert_vendor_safe` returning False in a unit test proves
    nothing about whether the renderer consults it.
    """
    approved, unapproved = _identified(_finding("CT-WIDTH-001"), _finding("CT-DEPTH-001"))

    with pytest.raises(UnapprovedContent, match="CT-DEPTH-001"):
        render_vendor_redline(
            _package(),
            [approved, unapproved],
            LocalStore(tmp_path),
            signed_off=_signed_off(approved),
        )

    assert not list(tmp_path.rglob("*.pdf")), "a refused render must not leave an artifact behind"


def test_an_unapproved_finding_is_refused_rather_than_quietly_dropped(tmp_path: Path) -> None:
    """The tempting alternative, and the worse one.

    A vendor document missing a finding is indistinguishable from one where that check passed. The
    reviewer whose sign-off was being worked around is exactly the person who would have noticed,
    and they are not in the room when the file is sent.
    """
    approved, unapproved = _identified(_finding("CT-WIDTH-001"), _finding("CT-DEPTH-001"))

    with pytest.raises(UnapprovedContent) as raised:
        render_vendor_redline(
            _package(),
            [approved, unapproved],
            LocalStore(tmp_path),
            signed_off=_signed_off(approved),
        )

    message = str(raised.value)
    assert "Nothing has been rendered" in message
    assert "not dropped" in message
    assert str(unapproved.finding_id) in message, "the refusal must name what was missing"


def test_the_renderer_refuses_a_vendor_render_without_clearance(tmp_path: Path) -> None:
    """What makes `render_vendor_redline` the only route rather than the recommended one.

    A caller reaching straight for `render_redline` — the obvious thing to do, since it takes a
    `ReportMode` — cannot produce a vendor document.
    """
    with pytest.raises(VendorApprovalUnavailable, match="ADR-0010"):
        render_redline(_package(), [_finding()], ReportMode.VENDOR, LocalStore(tmp_path))


def test_clearance_on_an_internal_render_is_refused(tmp_path: Path) -> None:
    """Refused rather than ignored. An internal report is engine output and is issued under nobody's
    sign-off; printing an approval on it would say otherwise, and a caller who passed one believed
    it was doing something."""
    clearance = VendorClearance(approval_id=APPROVAL, approved_by="anant", approved_at=APPROVED_AT)

    with pytest.raises(ValueError, match="vendor render"):
        render_redline(
            _package(),
            [_finding()],
            ReportMode.INTERNAL,
            LocalStore(tmp_path),
            clearance=clearance,
        )


def test_an_approval_for_another_revision_cannot_clear_this_render(tmp_path: Path) -> None:
    """A document citing one revision's sign-off while showing another's findings misstates what
    was accepted — and the citation is the part a vendor would rely on."""
    covered = _identified(_finding())[0]
    other_revision = uuid4()

    with pytest.raises(UnapprovedContent, match="package revision"):
        render_vendor_redline(
            _package(),
            [covered],
            LocalStore(tmp_path),
            signed_off=_signed_off(covered, revision=other_revision),
        )


def test_a_fully_approved_render_is_produced(tmp_path: Path) -> None:
    """The happy path exists so the refusals above are not passing for the wrong reason.

    Without this, every test in this section would still pass if vendor rendering were broken
    outright.
    """
    covered = _identified(_finding(), _finding("CT-DEPTH-001"))

    stored = render_vendor_redline(
        _package(), list(covered), LocalStore(tmp_path), signed_off=_signed_off(*covered)
    )

    assert stored.key.endswith(".pdf")
    assert "vendor" in stored.key, "the mode belongs in the key; two modes are two artifacts"


# ---------------------------------------------------------------------------
# The report records who approved it and when
# ---------------------------------------------------------------------------


def test_the_vendor_report_names_its_approver_and_the_time(tmp_path: Path) -> None:
    """A vendor holding the document should be able to see whose sign-off it went out under
    without having to ask the person who sent it."""
    covered = _identified(_finding())

    stored = render_vendor_redline(
        _package(),
        list(covered),
        LocalStore(tmp_path),
        signed_off=_signed_off(*covered, approved_by="raj"),
    )
    text = _text(tmp_path, stored.key)

    assert "raj" in text
    assert "2026-08-25" in text
    assert str(APPROVAL) in text


def test_an_internal_report_names_no_approver(tmp_path: Path) -> None:
    """Engine output is not issued under anyone's sign-off, and an approval line on it would be a
    claim nobody made."""
    stored = render_redline(_package(), [_finding()], ReportMode.INTERNAL, LocalStore(tmp_path))

    assert "Approved by" not in _text(tmp_path, stored.key)


# ---------------------------------------------------------------------------
# Derived expectations are labelled and shown with their calculation
# ---------------------------------------------------------------------------


def test_a_derived_value_is_labelled_derived_and_shown_with_its_arithmetic(
    tmp_path: Path,
) -> None:
    """ADR-0010 permits showing a derived expectation and forbids issuing one, and the difference
    is entirely in the presentation. A bare number in a document a vendor builds from is an
    instruction whatever a heading two pages earlier said."""
    covered = _identified(_finding(intermediates=(("expected_width", "6010"),)))

    stored = render_vendor_redline(
        _package(), list(covered), LocalStore(tmp_path), signed_off=_signed_off(*covered)
    )
    text = _text(tmp_path, stored.key)

    assert "DERIVED" in text, "a calculated value must say it was calculated"
    assert "expected_width" in text
    assert "sum_within_tolerance" in text, "the operation that produced it"
    assert "cabinet_1 = 2400" in text, "the operands, so the arithmetic can be repeated by hand"
    assert "Do not build to them" in text


def test_the_derived_section_says_so_when_there_are_none(tmp_path: Path) -> None:
    """Emitted even when empty, like the unplaced section: a reader should be told the report
    contains no calculated numbers rather than infer it from a missing heading."""
    stored = render_redline(_package(), [_finding()], ReportMode.INTERNAL, LocalStore(tmp_path))
    text = _text(tmp_path, stored.key)

    assert "Derived expectations" in text
    assert "None." in text


def test_an_abstention_has_no_derived_values() -> None:
    """Nothing was calculated. Returning an empty calculation would suggest something was."""
    assert derived_expectations(_finding(outcome=Outcome.NOT_FOUND)) == ()


def test_a_derived_expectation_carries_the_rule_that_produced_it() -> None:
    """Several findings share one summary section; a value with no rule beside it cannot be traced
    back to the check that computed it."""
    (derived,) = derived_expectations(
        _finding("CT-FILLER-001", intermediates=(("left_filler", "47"),))
    )

    assert derived.rule_id == "CT-FILLER-001"
    assert derived.name == "left_filler"
    assert derived.value == "47"
    assert "sum_within_tolerance" in derived.calculation


# ---------------------------------------------------------------------------
# The approval is read from the database, never supplied by the caller
# ---------------------------------------------------------------------------


def _stored_revision(session: Session) -> PackageRevision:
    project = Project(name=f"P {uuid4().hex[:6]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Vicentia Millwork")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision


def _stored_finding(session: Session, revision: PackageRevision) -> StoredFinding:
    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:8]}")
    session.add(definition)
    session.flush()
    canonical = json.dumps({"id": definition.rule_id}, separators=(",", ":"))
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=canonical,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()
    finding = StoredFinding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.FAIL.value,
        severity=Severity.CRITICAL.value,
        trace={},
        parameter_set_versions={},
    )
    session.add(finding)
    session.flush()
    return finding


def _approve(
    session: Session, revision: PackageRevision, findings: Sequence[StoredFinding], by: str
) -> Approval:
    approval = Approval(package_revision_id=revision.id, approved_by=by)
    session.add(approval)
    session.flush()
    session.add_all(
        ApprovedFinding(
            approval_id=approval.id,
            finding_id=finding.id,
            package_revision_id=revision.id,
        )
        for finding in findings
    )
    session.flush()
    return approval


def test_the_sign_off_is_read_from_the_stored_approval(
    sessions: sessionmaker[Session],
) -> None:
    """The findings an approval covered come from `approved_findings`, not from an argument.

    A list of ids passed in by the caller is the client-supplied value that association table
    exists to avoid: nothing would check that any of them were ever signed.
    """
    with unit_of_work(sessions) as session:
        revision = _stored_revision(session)
        covered = [_stored_finding(session, revision) for _ in range(2)]
        _stored_finding(session, revision)  # signed off by nobody
        _approve(session, revision, covered, by="raj")
        revision_id = revision.id
        covered_ids = {finding.id for finding in covered}

    with unit_of_work(sessions) as session:
        signed = sign_off(session, revision_id)

    assert signed.approved_by == "raj"
    assert signed.package_revision_id == revision_id
    assert signed.finding_ids == covered_ids, "only the linked findings are covered"


def test_an_unapproved_revision_refuses_rather_than_returning_nothing(
    sessions: sessionmaker[Session],
) -> None:
    """An optional return invites the one line — `if approval:` — that turns a missing sign-off
    into a silent skip."""
    with unit_of_work(sessions) as session:
        revision = _stored_revision(session)
        _stored_finding(session, revision)
        revision_id = revision.id

    with unit_of_work(sessions) as session, pytest.raises(UnapprovedContent, match="ADR-0010"):
        sign_off(session, revision_id)


def test_a_re_approved_revision_uses_the_sign_off_in_force(
    sessions: sessionmaker[Session],
) -> None:
    """The latest approval governs. The earlier one stays in the table — `Approval` is immutable so
    the history of who accepted what survives a re-review."""
    with unit_of_work(sessions) as session:
        revision = _stored_revision(session)
        first = _stored_finding(session, revision)
        second = _stored_finding(session, revision)
        _approve(session, revision, [first], by="raj")
        _approve(session, revision, [first, second], by="anant")
        revision_id = revision.id
        both = {first.id, second.id}

    with unit_of_work(sessions) as session:
        signed = sign_off(session, revision_id)

    assert signed.approved_by == "anant"
    assert signed.finding_ids == both


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------


def test_assert_vendor_safe_passes_when_everything_is_covered() -> None:
    covered = _identified(_finding(), _finding("CT-DEPTH-001"))

    assert_vendor_safe(list(covered), _signed_off(*covered))


def test_assert_vendor_safe_names_every_uncovered_finding() -> None:
    """One name would be enough to fail; all of them is what makes the message actionable."""
    first, second, approved = _identified(
        _finding("CT-A"), _finding("CT-B"), _finding("CT-APPROVED")
    )

    with pytest.raises(UnapprovedContent) as raised:
        assert_vendor_safe([first, second, approved], _signed_off(approved))

    message = str(raised.value)
    assert "CT-A" in message and "CT-B" in message
    assert "2 finding(s)" in message


def test_an_empty_render_is_vendor_safe() -> None:
    """Nothing to approve, nothing unapproved. Refusing here would block a legitimate report on a
    revision where every check abstained and none were placed — a different situation, handled
    elsewhere, and not this gate's to decide."""
    assert_vendor_safe([], _signed_off())
