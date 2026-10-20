"""The job tracker's routes (#1578): every item found across a project's sets, rounds and checks.

- `GET /projects/{project_id}/tracker`: totals, each reviewed drawing set (its rounds with their
  open counts, its checks), every item with its history, and the vendor scorecard.
- `GET /projects/{project_id}/tracker.xlsx`: the same as a workbook (Items, Vendors, Checks; filters
  on, every cell text so `1/8` stays `1/8`), filtered like the screen (`state`, `package_id`,
  `vendor`). Where the server has no openpyxl, the items come back as CSV instead.

Read-only and $0: built by code (`app/review/assistant/job_tracker.py`) from the stored chat answers,
each finding at its newest correction (#1548) and with its latest decision (#1521). Nothing is
written. Project scope like every project route: a caller outside the project gets a 404.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy import case, func, literal_column, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnClause

from app.api.dependencies import get_session
from app.auth import Action, Principal, require_action, require_project_access
from app.models import AssistantChat, AssistantChatMessage, AssistantFindingDecision, Package
from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
from app.review.assistant.chat_decisions import decision_out, with_decisions
from app.review.assistant.chat_findings import live_findings
from app.review.assistant.chat_store import package_name
from app.review.assistant.job_tracker import TrackerAnswer, TrackerSet, build_tracker
from app.review.assistant.workbook import write_xlsx
from app.schemas.job_tracker import TrackerOut

router = APIRouter(tags=["job tracker"])

#: Only the blocks the tracker reads: findings and later corrections (an answer's steps and
#: thinking can be large and are never needed here).
_FINDING_BLOCKS: Final[ColumnClause[Any]] = literal_column(
    '\'$[*] ? (@.type == "finding" || @.type == "finding_update")\'::jsonpath'
)
#: A full check's question as the screen writes it (older rows have no `mode`).
_FULL_CHECK: Final = re.compile(r"^(?:Full|Continue the full) check(?: · [^\n]*)?$")
_TITLE_CHARS: Final = 80

type StateFilter = Literal["open", "fixed", "not_a_problem", "right_later", "all"]


@dataclass(frozen=True, slots=True)
class _Row:
    """An answer's id, role and finding blocks, for `corrections_by_answer`."""

    id: UUID
    role: str
    blocks: list[dict[str, Any]] | None


def _title(question: str | None, full_check: bool) -> str:
    words = " ".join((question or "").split())
    if not words:
        return "Full check" if full_check else "Question"
    return words if len(words) <= _TITLE_CHARS else words[: _TITLE_CHARS - 1].rstrip() + "…"


def load_tracker(session: Session, project_id: UUID) -> dict[str, Any]:
    """The project's tracker, from its stored answers and decisions."""
    packages = list(
        session.scalars(
            select(Package).where(Package.project_id == project_id).order_by(Package.created_at)
        )
    )
    sets = [
        TrackerSet(package_id=p.id, name=package_name(p), vendor=p.vendor, product=p.product_type)
        for p in packages
    ]
    if not packages:
        return build_tracker(sets, [])
    chats = {
        chat.id: chat
        for chat in session.scalars(
            select(AssistantChat).where(AssistantChat.package_id.in_([p.id for p in packages]))
        )
    }
    if not chats:
        return build_tracker(sets, [])
    blocks = AssistantChatMessage.blocks
    rows = session.execute(
        select(
            AssistantChatMessage.id,
            AssistantChatMessage.chat_id,
            AssistantChatMessage.seq,
            AssistantChatMessage.created_at,
            AssistantChatMessage.mode,
            AssistantChatMessage.stopped,
            AssistantChatMessage.role,
            # Only a question's words are needed (an answer's text can be long).
            case((AssistantChatMessage.role == "user", AssistantChatMessage.text)).label("text"),
            func.jsonb_path_query_array(blocks, _FINDING_BLOCKS).label("found"),
        )
        .where(AssistantChatMessage.chat_id.in_(list(chats)))
        .order_by(AssistantChatMessage.chat_id, AssistantChatMessage.seq)
    ).all()
    questions: dict[tuple[UUID, int], tuple[str, str | None]] = {}
    by_chat: dict[UUID, list[_Row]] = {}
    for row in rows:
        if row.role == "user":
            questions[(row.chat_id, row.seq)] = (row.text, row.mode)
        elif row.found:
            by_chat.setdefault(row.chat_id, []).append(_Row(row.id, row.role, list(row.found)))
    decided = _decisions(session, list(by_chat))
    answers: list[TrackerAnswer] = []
    meta = {row.id: row for row in rows if row.role == "assistant"}
    for chat_id, answer_rows in by_chat.items():
        corrections = corrections_by_answer(answer_rows)
        for answer in answer_rows:
            findings = live_findings(
                with_decisions(
                    with_corrections(answer.blocks, corrections.get(str(answer.id))),
                    decided.get(answer.id),
                )
            )
            if not findings:
                continue
            row = meta[answer.id]
            question, asked_mode = questions.get((chat_id, row.seq - 1), (None, None))
            full_check = (
                row.mode == "full_check"
                or asked_mode == "full_check"
                or (asked_mode is None and _FULL_CHECK.match((question or "").strip()) is not None)
            )
            answers.append(
                TrackerAnswer(
                    package_id=chats[chat_id].package_id,
                    chat_id=chat_id,
                    message_id=answer.id,
                    at=row.created_at,
                    kind="full_check" if full_check else "question",
                    title=_title(question, full_check),
                    stopped=row.stopped,
                    findings=findings,
                )
            )
    return build_tracker(sets, answers)


def _decisions(session: Session, chat_ids: Sequence[UUID]) -> dict[UUID, dict[str, dict[str, Any]]]:
    """The latest decision per finding, per answer, of these chats."""
    latest: dict[UUID, dict[str, dict[str, Any]]] = {}
    if not chat_ids:
        return latest
    for row in session.scalars(
        select(AssistantFindingDecision)
        .where(AssistantFindingDecision.chat_id.in_(list(chat_ids)))
        .order_by(AssistantFindingDecision.decided_at, AssistantFindingDecision.created_at)
    ):
        latest.setdefault(row.message_id, {})[row.finding_id] = decision_out(row)
    return latest


@router.get(
    "/projects/{project_id}/tracker",
    response_model=TrackerOut,
    summary="Every item found across the project's drawing sets, rounds and checks",
)
def read_tracker(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
) -> TrackerOut:
    del principal
    built = load_tracker(session, project_id)
    return TrackerOut.model_validate(
        {**built, "project_id": str(project_id), "made_at": datetime.now(UTC).isoformat()}
    )


# ---- export --------------------------------------------------------------------------------------

STATE_WORDS: Final[Mapping[str, str]] = {
    "open": "Open",
    "fixed": "Closed",
    "not_a_problem": "Not a problem",
    "right_later": "Right on a later check",
}

_ITEM_COLUMNS: Final = (
    "State",
    "Item",
    "Drawing set",
    "Vendor",
    "Product",
    "What it touches",
    "Check",
    "Page",
    "Vendor drew",
    "Should be",
    "First seen round",
    "Round now",
    "Closed in round",
    "Tags",
    "Decision",
    "Decided by",
    "Checks that saw it",
    "First seen",
    "Last seen",
)


def state_words(item: Mapping[str, Any]) -> str:
    """'Open · Wrong', 'Closed · round 2', 'Not a problem · <name>'."""
    state = str(item["state"])
    if state == "open":
        return "Open · Wrong" if item.get("open_kind") == "wrong" else "Open · Not compared"
    if state == "fixed" and item.get("fixed_round"):
        return f"Closed · round {item['fixed_round']}"
    if state == "not_a_problem" and item.get("decided_by"):
        return f"Not a problem · {item['decided_by']}"
    return STATE_WORDS.get(state, state)


def _day(value: object) -> str:
    return str(value or "")[:10]


def _decision_words(value: object) -> str:
    return {"agree": "Agreed", "not_a_problem": "Not a problem"}.get(str(value or ""), "")


def item_row(item: Mapping[str, Any]) -> list[str]:
    return [
        state_words(item),
        str(item["item"]),
        str(item["set_name"]),
        str(item["vendor_name"]),
        str(item["category_label"]),
        str(item["kind"]),
        str(item["check_label"]),
        "" if item.get("page") is None else str(item["page"]),
        str(item["vendor_value"]),
        str(item["architect_value"]),
        str(item["first_round"]),
        str(item["round_now"]),
        "" if item.get("fixed_round") is None else str(item["fixed_round"]),
        ", ".join(item.get("tags") or []),
        _decision_words(item.get("decision")),
        str(item.get("decided_by") or ""),
        str(item["checks_seen"]),
        _day(item["first_seen_at"]),
        _day(item["last_seen_at"]),
    ]


def filtered_items(
    built: Mapping[str, Any], state: str, package_id: UUID | None, vendor: str | None
) -> list[Mapping[str, Any]]:
    wanted_vendor = None if vendor is None else " ".join(vendor.split()).lower()
    return [
        item
        for item in built["items"]
        if (state == "all" or item["state"] == state)
        and (package_id is None or item["package_id"] == str(package_id))
        and (wanted_vendor is None or item["vendor_key"] == wanted_vendor)
    ]


def _vendor_rows(built: Mapping[str, Any]) -> list[list[str]]:
    def top(entries: Sequence[Mapping[str, Any]]) -> str:
        return "; ".join(f"{e['label']} ({e['count']})" for e in entries[:3])

    return [
        [
            str(v["vendor_name"]),
            str(v["sets"]),
            str(v["rounds"]),
            str(v["mistakes"]),
            str(v["open"]),
            (
                f"{v['fixed_next_round']} of {v['fixed_next_of']} ({v['fixed_next_share']}%)"
                if v["fixed_next_share"] is not None
                else "No next round yet"
            ),
            str(v["average_open_per_round"] or ""),
            top(v["by_product"]),
            top(v["by_kind"]),
            top(v["by_check"]),
        ]
        for v in built["vendors"]
    ]


def _check_rows(built: Mapping[str, Any]) -> list[list[str]]:
    rows: list[list[str]] = []
    for the_set in built["sets"]:
        for check in the_set["checks"]:
            rows.append(
                [
                    str(the_set["name"]),
                    "Full check" if check["kind"] == "full_check" else "Question",
                    str(check["title"]),
                    _day(check["at"]),
                    {"cancelled": "Stopped by the reviewer"}.get(
                        str(check["stopped"] or ""), str(check["stopped"] or "")
                    ),
                    ", ".join(str(r) for r in check["rounds"]),
                    str(check["wrong"]),
                    str(check["decision"]),
                    str(check["right"]),
                ]
            )
    return rows


def items_csv(items: Sequence[Mapping[str, Any]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(_ITEM_COLUMNS)
    for item in items:
        writer.writerow(item_row(item))
    return out.getvalue()


def tracker_xlsx(built: Mapping[str, Any], items: Sequence[Mapping[str, Any]]) -> bytes:
    """The workbook: Items (the filtered ones), Vendors, Checks (`workbook.write_xlsx`: every
    cell text, a filter and a frozen header on each sheet). Raises `ModuleNotFoundError` when
    openpyxl is not installed."""
    return write_xlsx(
        [
            ("Items", _ITEM_COLUMNS, [item_row(item) for item in items]),
            (
                "Vendors",
                (
                    "Vendor",
                    "Drawing sets",
                    "Rounds",
                    "Mistakes",
                    "Open now",
                    "Fixed on the next round",
                    "Average open per round",
                    "Most by product",
                    "Most by what it touches",
                    "Most by check",
                ),
                _vendor_rows(built),
            ),
            (
                "Checks",
                (
                    "Drawing set",
                    "Kind",
                    "Question",
                    "Date",
                    "Stopped",
                    "Rounds",
                    "Wrong",
                    "Not compared",
                    "Right",
                ),
                _check_rows(built),
            ),
        ]
    )


@router.get(
    "/projects/{project_id}/tracker.xlsx",
    response_class=Response,
    summary="The tracker as a workbook (Items, Vendors, Checks); CSV of the items without openpyxl",
)
def export_tracker(
    principal: Annotated[Principal, Depends(require_project_access)],
    _: Annotated[Principal, Depends(require_action(Action.READ_PACKAGE))],
    session: Annotated[Session, Depends(get_session)],
    project_id: UUID,
    state: Annotated[
        StateFilter,
        Query(description="Which items: all (default), open, fixed, not_a_problem, right_later."),
    ] = "all",
    package_id: Annotated[UUID | None, Query(description="One drawing set only.")] = None,
    vendor: Annotated[
        str | None, Query(max_length=200, description="One vendor only (its name, any case).")
    ] = None,
) -> Response:
    del principal
    built = load_tracker(session, project_id)
    items = filtered_items(built, state, package_id, vendor)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    try:
        body = tracker_xlsx(built, items)
    except ModuleNotFoundError:
        return Response(
            content=items_csv(items).encode("utf-8"),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="GV tracker {stamp}.csv"'},
        )
    return Response(
        content=body,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="GV tracker {stamp}.xlsx"'},
    )
