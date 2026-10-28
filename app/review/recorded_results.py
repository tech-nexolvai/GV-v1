"""GV Review's recorded results for one package: what the upload pipeline found, as the full check reads them.

Moved here unchanged on 2026-10-28 from the old review screens' modules: the stored-findings facts
(from `app/api/reviewer_chat.py`) and the records loader (`load_snapshot`, `ai_review_and_job_values`,
from `app/api/review_assistant.py`). The chat-first full check reads them through
`app/api/assistant_chats.py` to cross-check its own findings against GV Review's recorded ones.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.visual_countertops import _countertop_results_for_revision
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    RuleDefinition,
    RuleSnapshot,
)
from app.review.approval import readiness_and_decisions
from app.review.assistant.full_check_brief import LEAD_RESULTS
from app.review.assistant.records import ReviewSnapshot, build_snapshot
from app.review.review_items import review_items_for_revision
from workflow.findings_composer import ComposerFinding, ComposerOperand, reviewer_reason


@dataclass(frozen=True, slots=True)
class _LiveRunFacts:
    revision_id: UUID
    findings: tuple[ComposerFinding, ...]
    checks_have_run: bool


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _evidence_page(reference: object) -> str | None:
    """Use the recorded page label only when the stored reference is structurally complete."""
    if not isinstance(reference, str) or not reference:
        return None
    try:
        content = json.loads(reference)
        page = content["page"]
        document = content["document_version_id"]
    except (KeyError, TypeError, ValueError):
        return None
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return None
    if not isinstance(document, str) or not document.strip():
        return None
    # `pages.index` is zero-based, the way the reader addresses a document; a reviewer counts sheets
    # from one, and so does every other surface in the product (for example the drawing list in
    # `frontend/main/src/components/measure/DrawingRolesList.tsx`). This endpoint was the exception,
    # which put "Sheet 0 has the failure" into an AI answer — a sheet that exists on no drawing.
    #
    # The comment here previously called the raw value intentional because this endpoint "does not
    # calculate a new page number or infer a sheet title". Not inferring a *title* is right and
    # unchanged. Converting an index to the page number it denotes is not an inference; it is the
    # same page, written the way the person reading it counts.
    return str(page + 1)


def _operands(trace: Mapping[str, object]) -> tuple[ComposerOperand, ...]:
    raw = trace.get("operands")
    if not isinstance(raw, list):
        return ()
    return tuple(
        ComposerOperand(
            name=_text(item.get("name")),
            value=_text(item.get("value")),
            source=_text(item.get("source")),
            evidence_page=_evidence_page(item.get("evidence_ref")),
        )
        for item in raw
        if isinstance(item, Mapping)
    )


def _facts(
    finding: Finding,
    definition: RuleDefinition,
    check_name: str,
    rule_description: str | None = None,
) -> ComposerFinding:
    """Project exactly the stored deterministic record into the language-only schema."""
    trace = finding.trace
    operands = _operands(trace)
    pages = tuple(dict.fromkeys(item.evidence_page for item in operands if item.evidence_page))
    return ComposerFinding(
        key=str(finding.id),
        check=definition.rule_id,
        check_name=check_name,
        outcome=finding.outcome,
        severity=finding.severity,
        reason=reviewer_reason(
            finding.reason or _text(trace.get("reason")) or "No reason was recorded.",
            finding.outcome,
        ),
        comparison=_text(trace.get("comparison")) or None,
        # The stored comparison and operands already carry exact rendered values.  This endpoint does
        # not rebuild a delta from rational columns or do arithmetic just to make chat more verbose.
        difference=None,
        tolerance=_text(trace.get("tolerance")) or None,
        arithmetic_unit=_text(trace.get("arithmetic_unit")) or None,
        operands=operands,
        evidence_pages=pages,
        notes=() if finding.notes is None else tuple(finding.notes),
        # The published rule's own account of what it checks. Context, so an answer can say what a
        # check is *for* rather than only what it concluded — the snapshot was already being parsed
        # here for its name, and the description was sitting unused beside it.
        rule_description=rule_description,
    )


def _checks_have_run(session: Session, revision_id: UUID) -> bool:
    """Whether any check has actually been run on this revision.

    **Separate from "are there findings", because an empty list means two different things.** A
    package uploaded and never checked and a package checked with nothing wrong both produce no
    findings, and answering them the same way told a reviewer "No FAIL findings in this run" about a
    drawing nothing had looked at — which reads as a clean bill of health. The presence of an
    unsuperseded `CheckRun` is what separates them, and it is a row rather than an inference.
    """
    return (
        session.execute(
            select(CheckRun.id)
            .where(CheckRun.package_revision_id == revision_id, CheckRun.superseded_at.is_(None))
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def _live_run_facts(session: Session, project_id: UUID, package_id: UUID) -> _LiveRunFacts | None:
    """One revision's unsuperseded findings and whether anything was checked, or no such package.

    Both, because the findings alone cannot tell the caller which of the two empty cases it is in.
    """
    revision = session.execute(
        select(PackageRevision.id)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    if revision is None:
        return None

    checked = _checks_have_run(session, revision)
    rows = session.execute(
        select(Finding, RuleDefinition, RuleSnapshot.canonical_json)
        .join(CheckRun, CheckRun.id == Finding.check_run_id)
        .join(RuleSnapshot, RuleSnapshot.id == CheckRun.rule_snapshot_id)
        .join(RuleDefinition, RuleDefinition.id == RuleSnapshot.rule_definition_id)
        .where(
            Finding.package_revision_id == revision,
            CheckRun.superseded_at.is_(None),
        )
        .order_by(Finding.created_at, Finding.id)
    ).all()
    result: list[ComposerFinding] = []
    for finding, definition, canonical_json in rows:
        name: object = None
        description: object = None
        try:
            payload = json.loads(canonical_json)
            if isinstance(payload, Mapping):
                name = payload.get("name")
                description = payload.get("description")
        except (TypeError, ValueError):
            # A snapshot that will not parse still has a finding worth showing. The rule id is a
            # usable heading and the description is simply absent, which is what `None` means.
            pass
        check_name = name if isinstance(name, str) and name.strip() else definition.rule_id
        result.append(
            _facts(
                finding,
                definition,
                check_name,
                (
                    description.strip()
                    if isinstance(description, str) and description.strip()
                    else None
                ),
            )
        )
    return _LiveRunFacts(revision_id=revision, findings=tuple(result), checks_have_run=checked)


def load_snapshot(
    session: Session, project_id: UUID, package_id: UUID, revision: PackageRevision
) -> ReviewSnapshot:
    """This review's records, from the functions the countertop screen, sign-off and the simple
    review list (#1352) use."""
    countertops = _countertop_results_for_revision(session, package_id, revision)
    readiness, records = readiness_and_decisions(session, revision.id)
    live = _live_run_facts(session, project_id, package_id)
    return build_snapshot(
        countertops,
        readiness,
        () if live is None else live.findings,
        records.decisions,
        checks_have_run=live is not None and live.checks_have_run,
        review_items=review_items_for_revision(
            session, project_id=project_id, package_id=package_id, revision=revision
        ),
    )


def ai_review_row_for_model(row: dict[str, Any]) -> dict[str, Any]:
    """An AI page review row as the model reads it: `clouded` is spelled out as what it means,
    that THIS APP's cloud PDF will cloud the row, so it is never read as GV's own markup
    (owner, 2026-10-16)."""
    out = dict(row)
    if "clouded" in out:
        out["will_be_clouded_on_our_cloud_pdf"] = out.pop("clouded")
    return out


def ai_review_and_job_values(
    session: Session, project_id: UUID, revision_id: UUID, page: int | None
) -> dict[str, Any]:
    """The AI page review rows (one page) or its per-page summary (whole package), and the job's
    values: the ones typed for the project and what the job values reader found."""
    from app.api.company_settings import _project_set, _text
    from app.review.ai_review import ai_review_for_revision
    from app.review.review_items import _job_value_suggestions

    review = ai_review_for_revision(session, revision_id)
    fields = {
        "key",
        "measures",
        "architect_text",
        "vendor_text",
        "result",
        "why",
        "status",
        "clouded",
        "verified_by",
        "evaluator_reason",
    }
    pages: list[dict[str, Any]] = []
    for sheet in review.pages:
        if page is not None and sheet.page != page:
            continue
        rows = [
            ai_review_row_for_model(row.model_dump(mode="json", include=fields))
            for row in sheet.rows
        ]
        if page is None:
            counts: dict[str, int] = {}
            for row in sheet.rows:
                counts[row.result] = counts.get(row.result, 0) + 1
            summary: dict[str, Any] = {"page": sheet.page, "status": sheet.status, **counts}
            summary["different"] = [row for row in rows if row["result"] == "different"]
            # The rows the page review could not settle or saw on one side only: leads a full
            # check settles on the drawing (#1515), never recorded results.
            leads = [row for row in rows if row["result"] in LEAD_RESULTS]
            if leads:
                summary["leads"] = leads
            pages.append(summary)
        else:
            pages.append({"page": sheet.page, "status": sheet.status, "rows": rows})
    _version, values, _rows = _project_set(session, project_id)
    typed = {name: _text(value) for name, value in sorted(values.items())}
    found = [
        item.model_dump(mode="json", exclude={"location"})
        for item in _job_value_suggestions(session, revision_id)
        if page is None or item.page == page
    ]
    return {
        "ai_page_review": pages,
        "job_values_typed_for_this_project": typed,
        "job_values_found_on_the_drawings": found,
    }
