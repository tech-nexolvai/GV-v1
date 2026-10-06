"""What may leave the building: only content a reviewer signed off.

ADR-0010 draws a line the rest of the product depends on. The engine **may** compute a derived
expectation — a tolerance comparison is arithmetic on derived sums, and a reviewer cannot judge a
FAIL without seeing it. What the engine may not do is **issue** one. *"The countertop is 6012 mm;
cabinets and fillers sum to 6010 mm; the difference is 2 mm"* is a calculation. *"Make the left
filler 47 mm"* is an instruction, and if a vendor builds to it and it is wrong, the question of who
decided has a different answer.

So: **no computed dimension reaches a vendor without reviewer sign-off.**

**The gate is a capability, not a check somebody remembers to call.** `render_redline` refuses
`ReportMode.VENDOR` unless it is handed a `VendorClearance`, and the only place one is built is
`render_vendor_redline` below — after every finding in the render has been matched against the
stored approval. A filter would have been the tempting alternative and is the worse one: silently
dropping an unapproved finding produces a document that looks complete, and the reviewer never
learns that something was removed on their behalf. This raises instead, and names what was not
covered.

**Approval is read from the database, never accepted from the caller.** A list of approved ids
passed in as an argument is the client-supplied value `ApprovedFinding` exists to avoid — nothing
would check that any of them were ever signed. Here the approval row and its links are the source,
so the report can only contain findings a named person actually accepted.

**Derived expectations are labelled where they are shown.** A number that appears in a vendor
document without saying it was calculated reads as a specification, which is exactly the reading
ADR-0010 forbids. Each one is printed with the operation and the operands it came from, so the
vendor sees an expectation with its arithmetic rather than a figure to build to.

Source: ADR-0010; `AGENTS.md` §2.6 · Design: `docs/DESIGN_PRODUCT.md` §3.3 ·
Verification: ``tests/reports/test_vendor_redline.py``
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from reports.redline import (
    DerivedExpectation,
    RedlinePackage,
    ReportMode,
    VendorClearance,
    derived_expectations,
    render_redline,
)
from storage.store import ArtifactStore, StoredArtifact
from verdict.finding import Finding

__all__ = [
    "DerivedExpectation",
    "IdentifiedFinding",
    "SignedOff",
    "UnapprovedContent",
    "assert_vendor_safe",
    "derived_expectations",
    "render_vendor_redline",
    "sign_off",
]


from app.review.publication import SignedOff, UnapprovedContent, sign_off


@dataclass(frozen=True, slots=True)
class IdentifiedFinding:
    """A finding value together with the stored row it was loaded from.

    The renderer works in `verdict.finding.Finding`, which is the engine's own value type and
    deliberately carries no database identity — it is the same object whether it came from a run, a
    replay or a test. Approval, though, is recorded against `findings.id`. This pairs the two for
    the length of one render, so coverage is decided by primary key rather than by matching on rule
    id and hoping a revision produced only one finding per rule.
    """

    finding_id: UUID
    finding: Finding


def assert_vendor_safe(findings: Sequence[IdentifiedFinding], signed_off: SignedOff) -> None:
    """Raise unless every finding in the render was covered by the sign-off.

    Coverage only. Whether the approval is even *for* this package revision is checked by
    `render_vendor_redline`, which is the caller that holds the package — this function is given a
    `SignedOff` and a list of findings and cannot tell what package they belong to.
    """
    if isinstance(findings, str) or not isinstance(findings, Sequence):
        raise TypeError("findings must be a sequence of IdentifiedFinding values")
    for item in findings:
        if not isinstance(item, IdentifiedFinding):
            raise TypeError("findings must contain only IdentifiedFinding values")
    if not isinstance(signed_off, SignedOff):
        raise TypeError("signed_off must be a SignedOff")

    uncovered = [item for item in findings if item.finding_id not in signed_off.finding_ids]
    if uncovered:
        listed = ", ".join(
            f"{item.finding.rule_id} ({item.finding_id})"
            for item in sorted(
                uncovered, key=lambda item: (item.finding.rule_id, str(item.finding_id))
            )
        )
        raise UnapprovedContent(
            f"{len(uncovered)} finding(s) in this render were not covered by approval "
            f"{signed_off.approval_id}, signed by {signed_off.approved_by}: {listed}. Nothing has "
            "been rendered. These are not dropped from the report, because a vendor document "
            "missing a finding looks exactly like one where that check passed."
        )


def render_vendor_redline(
    package: RedlinePackage,
    findings: Sequence[IdentifiedFinding],
    store: ArtifactStore,
    *,
    signed_off: SignedOff,
) -> StoredArtifact:
    """Render the vendor deliverable, once every finding in it has been signed off.

    The only route to a vendor PDF. `render_redline` will not produce one without the
    `VendorClearance` built here, and that is built only after `assert_vendor_safe` has returned —
    so "approved content only" is a property of the call graph rather than a convention.

    The clearance carries who approved and when into the document itself, because a vendor holding
    a redline should be able to see whose sign-off it was issued under without asking.
    """
    if not isinstance(signed_off, SignedOff):
        raise TypeError("signed_off must be a SignedOff")
    if not isinstance(package, RedlinePackage):
        raise TypeError("package must be a RedlinePackage")
    if package.package_revision_id != signed_off.package_revision_id:
        raise UnapprovedContent(
            f"the approval is for package revision {signed_off.package_revision_id} and this "
            f"render is of {package.package_revision_id}. A document citing one revision's "
            "sign-off while showing another's findings misstates what was accepted."
        )

    assert_vendor_safe(findings, signed_off)

    return render_redline(
        package,
        [item.finding for item in findings],
        ReportMode.VENDOR,
        store,
        clearance=VendorClearance(
            approval_id=signed_off.approval_id,
            approved_by=signed_off.approved_by,
            approved_at=signed_off.approved_at,
        ),
    )
