"""The reviewer's decision on each finding (#1521): ✓ Agree or ✗ Not a problem, kept append-only.

Step 5 of the reviewer's path. A click is one `AssistantFindingDecision` row; a changed mind is a
new row and the latest one per (answer, finding) counts (`latest_decisions`). The stored answer's
blocks are never rewritten: each finding's latest decision is added when the answer is read
(`with_decisions`), and the cloud drawing and check report are remade from them on request
(`make_decided_files`, by code, no AI call).

Since #1581 (owner, 2026-10-18: no human step) the decisions are optional overrides of the
zero-click drawing: it is complete without any, an agreed item is clouded and an item marked not a
problem is left out. The PDF is drawn by `proof_drawing.py`, loaded by name
(`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssistantChatMessage, AssistantFindingDecision
from app.review.assistant.chat_corrections import with_corrections
from app.review.assistant.chat_findings import (
    MarkupFile,
    live_findings,
    make_proof_file,
    make_report_file,
)
from app.review.assistant.chat_trust import cloud_kind
from app.review.assistant.impact import by_impact
from app.review.assistant.proof import proof_of
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.send_package import (
    PackageIdentity,
    make_package_file,
    package_context,
)

__all__ = [
    "DecidedFiles",
    "decided_findings",
    "decided_summary",
    "decision_out",
    "latest_decisions",
    "make_decided_files",
    "record_decision",
    "resolver_settled",
    "with_decisions",
]

type Decisions = Mapping[UUID, Mapping[str, Mapping[str, Any]]]


def record_decision(
    session: Session,
    *,
    chat_id: UUID,
    message_id: UUID,
    finding_id: str,
    decision: str,
    note: str | None,
    decided_by: str,
) -> AssistantFindingDecision:
    """One more row: the reviewer's decision now (earlier rows stay)."""
    text = (note or "").strip() or None
    row = AssistantFindingDecision(
        chat_id=chat_id,
        message_id=message_id,
        finding_id=finding_id,
        decision=decision,
        note=text,
        decided_by=decided_by[:200],
        decided_at=datetime.now(UTC),
    )
    session.add(row)
    session.flush()
    return row


def decision_out(row: AssistantFindingDecision) -> dict[str, Any]:
    return {
        "decision": row.decision,
        "note": row.note,
        "by": row.decided_by,
        "at": row.decided_at,
    }


def latest_decisions(
    session: Session, chat_id: UUID, message_id: UUID | None = None
) -> dict[UUID, dict[str, dict[str, Any]]]:
    """The latest decision per finding, per answer, of a chat (or of one answer)."""
    query = select(AssistantFindingDecision).where(AssistantFindingDecision.chat_id == chat_id)
    if message_id is not None:
        query = query.where(AssistantFindingDecision.message_id == message_id)
    latest: dict[UUID, dict[str, dict[str, Any]]] = {}
    for row in session.scalars(
        query.order_by(AssistantFindingDecision.decided_at, AssistantFindingDecision.created_at)
    ):
        latest.setdefault(row.message_id, {})[row.finding_id] = decision_out(row)
    return latest


def with_decisions(
    blocks: Sequence[Mapping[str, Any]] | None, decided: Mapping[str, Mapping[str, Any]] | None
) -> list[dict[str, Any]] | None:
    """An answer's blocks with each finding's latest decision added under `decision`, and where
    the zero-click cloud drawing puts it under `proof` (#1581: it follows the decision); the stored
    blocks are not changed."""
    if blocks is None:
        return None
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        if block.get("type") == "finding" and isinstance(finding, Mapping):
            decision = (decided or {}).get(str(finding.get("id")))
            if decision is not None:
                finding = {**finding, "decision": dict(decision)}
            block = {**block, "finding": {**finding, "proof": proof_of(finding)}}
        out.append(dict(block))
    return out


def decided_findings(
    message: AssistantChatMessage,
    decided: Mapping[str, Mapping[str, Any]] | None,
    corrections: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The answer's findings, in order, each at its newest version (a later message's correction,
    #1548) and with its latest decision."""
    return live_findings(with_decisions(with_corrections(message.blocks, corrections), decided))


def _trust(blocks: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any]:
    return next(
        (
            block["trust"]
            for block in reversed(list(blocks or []))
            if block.get("type") == "trust" and isinstance(block.get("trust"), Mapping)
        ),
        {},
    )


def _coverage(blocks: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    return next(
        (
            block["coverage"].get("counts")
            for block in reversed(list(blocks or []))
            if block.get("type") == "coverage" and isinstance(block.get("coverage"), Mapping)
        ),
        None,
    )


def decided_summary(
    message: AssistantChatMessage,
    question: str | None,
    findings: Sequence[Mapping[str, Any]],
    set_name: str,
) -> tuple[dict[str, str], list[str]]:
    """The report's cover lines and its "Not checked" lines, from the stored answer."""
    agreed = undecided = dismissed = right = 0
    for finding in findings:
        if cloud_kind(finding) is None:
            right += 1
            continue
        decided = (finding.get("decision") or {}).get("decision")
        if decided == "agree":
            agreed += 1
        elif decided == "not_a_problem":
            dismissed += 1
        else:
            undecided += 1
    trust = _trust(message.blocks)
    counts = trust.get("counts") or {}
    coverage = _coverage(message.blocks)
    summary = {
        "Drawing set": set_name,
        "Question": question or "",
        "Made": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
        "Model": f"{message.model or 'unknown'}, {message.effort or 'unknown'} effort",
        "Decisions": f"{agreed} agreed wrong, {undecided} not yet decided, {dismissed} not a "
        f"problem, {right} right",
        "Coverage": (
            "not recorded"
            if coverage is None
            else f"{coverage.get('total', 0) - coverage.get('not_opened', 0)} of "
            f"{coverage.get('total', 0)} pages accounted for "
            f"({coverage.get('not_comparable', 0)} not comparable)"
        ),
        "Trust": f"{counts.get('confirmed', 0)} confirmed, {counts.get('likely', 0)} likely, "
        f"{counts.get('to_check', 0)} AI reading only",
    }
    not_checked = [str(line) for line in trust.get("not_checked") or []]
    for item in (trust.get("gv_checks") or {}).get("left_out") or []:
        not_checked.append(
            f"GV Review's recorded difference {item.get('record_id')} (page {item.get('page')}: "
            f"{item.get('what')}) is not in this answer."
        )
    return summary, not_checked


@dataclass(frozen=True, slots=True)
class DecidedFiles:
    drawing: MarkupFile
    report: MarkupFile
    package: MarkupFile | None = None
    """The send-ready package made again (#1622), when asked for (`package_out`)."""


def resolver_settled(blocks: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]]:
    """What code settled by itself in the answer (#1554, its `resolver` block), for the files."""
    for block in reversed(list(blocks or [])):
        resolver = block.get("resolver") if block.get("type") == "resolver" else None
        if isinstance(resolver, Mapping):
            return [dict(e) for e in resolver.get("settled") or [] if isinstance(e, Mapping)]
    return []


def make_decided_files(
    workspace: ChatWorkspace,
    findings: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    not_checked: Sequence[str],
    drawing_out: Path,
    report_out: Path,
    *,
    settled: Sequence[Mapping[str, Any]] = (),
    package_out: Path | None = None,
    identity: PackageIdentity | None = None,
    numbers: Mapping[str, str] | None = None,
    incomplete: str | None = None,
    extra: Sequence[tuple[str, Path]] = (),
) -> DecidedFiles:
    """The cloud drawing and the check report made again with the reviewer's decisions as
    overrides (#1581, replacing #1521's only-agreed drawing): the same zero-click drawing as the
    full check's (`chat_findings.make_proof_file`), where an agreed item is clouded and an item
    marked not a problem is left out, stamped "… and reviewed by <name>". Raises `LookupError`
    when the chat has no copy of the set's PDFs. The biggest items first (#1569), so the drawing's
    and the report's numbers agree. `package_out` (a full check's answer): the send-ready package
    is made again too (#1622), from the same list and context (`identity`, the stable comment
    `numbers`, and whether the check finished), so the drawing's cover, the letter and the
    register say the same result and comment numbers. `extra`: the answer's other files the
    package holds as they were made (the Not-compared list, #1619), each name and path."""
    findings = by_impact(findings)
    context = None
    if package_out is not None:
        context = package_context(
            workspace,
            findings,
            identity or PackageIdentity(),
            numbers=numbers,
            incomplete=incomplete,
        )
    drawing = make_proof_file(workspace, findings, drawing_out, settled=settled, context=context)
    report = make_report_file(
        workspace, summary, findings, not_checked, report_out, settled=settled
    )
    package = None
    if package_out is not None and context is not None:
        package = make_package_file(
            workspace,
            findings,
            [(drawing.name, drawing_out), (report.name, report_out), *extra],
            package_out,
            context,
        )
    return DecidedFiles(drawing=drawing, report=report, package=package)


def package_block(blocks: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """A full check's `package` block (#1622): the review result's state and the comment numbers
    when its package was made."""
    for block in reversed(list(blocks or [])):
        found = block.get("package") if block.get("type") == "package" else None
        if isinstance(found, Mapping):
            return found
    return None
