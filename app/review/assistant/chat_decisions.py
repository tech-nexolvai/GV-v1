"""The reviewer's decision on each finding (#1521): ✓ Agree or ✗ Not a problem, kept append-only.

Step 5 of the reviewer's path. A click is one `AssistantFindingDecision` row; a changed mind is a
new row and the latest one per (answer, finding) counts (`latest_decisions`). The stored answer's
blocks are never rewritten: each finding's latest decision is added when the answer is read
(`with_decisions`), and the cloud drawing and check report are remade from them on request
(`make_decided_files`, by code, no AI call).

The PDF drawing is in `decided_drawing.py`, loaded by name (`tests/api/test_no_heavy_work.py`).
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssistantChatMessage, AssistantFindingDecision
from app.review.assistant.chat_corrections import with_corrections
from app.review.assistant.chat_findings import (
    MarkupFile,
    live_findings,
    main_upload,
    markup_name,
    place_findings,
    read_listing,
)
from app.review.assistant.chat_trust import cloud_kind
from app.review.assistant.sandbox import ChatWorkspace

__all__ = [
    "DRAWING_MODULE",
    "DecidedFiles",
    "decided_findings",
    "decided_summary",
    "decision_out",
    "latest_decisions",
    "make_decided_files",
    "record_decision",
    "with_decisions",
]

DRAWING_MODULE: Final = "app.review.assistant.decided_drawing"

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
    """An answer's blocks with each finding's latest decision added under `decision` (the stored
    blocks are not changed)."""
    if blocks is None or not decided:
        return None if blocks is None else [dict(block) for block in blocks]
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        if block.get("type") == "finding" and isinstance(finding, Mapping):
            decision = decided.get(str(finding.get("id")))
            if decision is not None:
                block = {**block, "finding": {**finding, "decision": dict(decision)}}
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


def make_decided_files(
    workspace: ChatWorkspace,
    findings: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    not_checked: Sequence[str],
    drawing_out: Path,
    report_out: Path,
) -> DecidedFiles:
    """The cloud drawing and the check report from the findings' decisions, written to the two
    paths. Raises `LookupError` when the chat has no copy of the set's PDFs."""
    name, placed = place_findings(workspace, findings)
    module = import_module(DRAWING_MODULE)
    module.decided_markup_pdf(workspace.uploads / name, placed, drawing_out)
    main = main_upload(read_listing(workspace))
    set_name = name if main is None else str(main["name"])
    module.decided_report_pdf(report_out, summary, findings, not_checked)

    def made(path: Path, kind: str) -> MarkupFile:
        data = path.read_bytes()
        return MarkupFile(markup_name(set_name, kind), len(data), hashlib.sha256(data).hexdigest())

    return DecidedFiles(
        drawing=made(drawing_out, "AI cloud drawing"), report=made(report_out, "AI check report")
    )
