"""The reviewer's clicks as candidate keyed cases for each check's track record (plan change #10).

A kind of check makes reds only with a clean record on enough keyed cases (`check_record`). Until
now the record grew only when someone keyed cases by hand. The reviewer's own clicks on the result
screen (✓ Agree / ✗ Not a problem, `assistant_finding_decisions`, #1521 / #1803) are the same kind
of evidence, so each click on an item **code proved Wrong by itself** (`proof.proved_wrong`: a red,
or a red held back as a Likely problem because its kind is still being proven) is a candidate:

- **Agree**: a confirmed case of that kind (the check was right). When the reviewer gave a "should
  be" other than the reference value code compared with, the candidate says so
  (`value_differs`): the check found a real problem, but a person must judge the value.
- **Not a problem**: a labelled **false red** of that kind (the check was wrong). This counts even
  when the item was held back as a Likely problem: a would-be red the reviewer rejects is the
  evidence the record exists to catch.
- **Any other click** (an item code did not prove: the reviewer is the witness, not a check) says
  nothing about a kind of check: no candidate.

**Candidates only.** Nothing here changes the record or any finding. The owner's local script reads
them (`candidates`, read only), writes a candidate file on the owner's machine (client values stay
there), and a person reviews each line and merges the accepted ones into the local answer keys; the
re-measured counts reach `check_record.json` through a PR. Each candidate names its job (the chat)
so the local split can keep a job whose correction taught a rule out of the cases that certify it
(the leakage guard).

$0: no AI, no network; reads the database only.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, Final, Literal, TypedDict
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AssistantChatMessage, AssistantFindingDecision
from app.review.assistant import check_record, proof
from app.review.assistant.chat_corrections import corrections_by_answer, with_corrections
from app.review.assistant.chat_decisions import latest_decisions
from app.review.assistant.chat_findings import live_findings
from app.review.assistant.chat_store import messages_of

__all__ = [
    "FALSE_RED",
    "RIGHT_RED",
    "Candidate",
    "Verdict",
    "candidates",
    "case_of",
]

type Verdict = Literal["right", "false"]
#: A reviewer's Agree on an item code proved Wrong: the check was right.
RIGHT_RED: Final[Verdict] = "right"
#: A reviewer's Not a problem on an item code proved Wrong: a false red of its kind.
FALSE_RED: Final[Verdict] = "false"


class Candidate(TypedDict):
    """One click as a candidate keyed case (values as the answer stored them: LOCAL use only)."""

    kind: str
    verdict: Verdict
    group: str
    held_back: bool
    value_differs: bool
    chat_id: str
    message_id: str
    finding_id: str
    page: Any
    item: str
    vendor: str
    architect: str
    decided_by: str
    decided_at: str
    note: str
    should_be: str


def _without_decision(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The item as code left it (what the reviewer was judging), without the click laid over."""
    return {key: value for key, value in finding.items() if key not in ("decision", "proof")}


def case_of(finding: Mapping[str, Any], decision: Mapping[str, Any]) -> dict[str, Any] | None:
    """The click as a candidate case: kind, verdict, where code had put the item and whether its
    kind held it back; `None` when the item was not proved Wrong by code (no check kind to judge)
    or the click is not Agree / Not a problem."""
    value = decision.get("decision")
    if value not in ("agree", "not_a_problem"):
        return None
    found = _without_decision(finding)
    if not proof.proved_wrong(found):
        return None
    kind = check_record.kind_of(found)
    should_be = " ".join(str(decision.get("should_be") or "").split())
    differs = bool(
        value == "agree"
        and should_be
        and proof.reviewer_value_refusal(found, should_be) is None
        and _plain(should_be) != _plain(str(found.get("architect") or ""))
    )
    return {
        "kind": kind,
        "verdict": RIGHT_RED if value == "agree" else FALSE_RED,
        "group": proof.proof_group(found),
        "held_back": not check_record.cleared(kind),
        "value_differs": differs,
        "should_be": should_be,
    }


def _plain(value: str) -> str:
    return "".join(value.lower().replace("″", '"').split())


def _chats_with_clicks(session: Session) -> list[UUID]:
    query = (
        select(AssistantFindingDecision.chat_id)
        .distinct()
        .order_by(AssistantFindingDecision.chat_id)
    )
    return list(session.scalars(query))


def _clicks(session: Session, chat_id: UUID) -> Iterator[Candidate]:
    corrections = corrections_by_answer(messages_of(session, chat_id))
    for message_id, decided in latest_decisions(session, chat_id).items():
        message = session.get(AssistantChatMessage, message_id)
        if message is None or not decided:
            continue
        findings = live_findings(with_corrections(message.blocks, corrections.get(str(message_id))))
        by_id = {str(f.get("id")): f for f in findings}
        for finding_id, decision in decided.items():
            finding = by_id.get(str(finding_id))
            case = case_of(finding, decision) if finding is not None else None
            if case is None or finding is None:
                continue
            yield Candidate(
                kind=case["kind"],
                verdict=case["verdict"],
                group=case["group"],
                held_back=case["held_back"],
                value_differs=case["value_differs"],
                chat_id=str(chat_id),
                message_id=str(message_id),
                finding_id=str(finding_id),
                page=finding.get("page"),
                item=str(finding.get("item") or ""),
                vendor=str(finding.get("vendor") or ""),
                architect=str(finding.get("architect") or ""),
                decided_by=str(decision.get("by") or ""),
                decided_at=str(decision.get("at") or ""),
                note=str(decision.get("note") or ""),
                should_be=case["should_be"],
            )


def candidates(session: Session) -> list[Candidate]:
    """Every chat's latest clicks that are evidence about a kind of check, oldest chat first.
    Reads only; the caller should open the session read only."""
    out: list[Candidate] = []
    for chat_id in _chats_with_clicks(session):
        out.extend(_clicks(session, chat_id))
    return out
