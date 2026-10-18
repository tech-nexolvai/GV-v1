"""The stand-alone vendor consistency check (#1584): the "+" menu's $0 action, stored as an answer.

The full check runs `vendor_consistency` at its finish. A reviewer can also run it alone, at any time
and at no cost: this builds that answer by code only (no model, no AI read). Its findings go through
the same finding book and checks as any answer's (ids, evidence crops, code's checks, the trust
rule), so the screen, the CSV and the cloud drawing use them as usual; its grey items are the
answer's "not checked" list; its words are code's.

Pure of the web layer: the API copies the set's PDFs to the chat, calls `run_check`, and stores the
question and this answer (`chat_store`).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from app.review.assistant.chat_findings import FindingBook, main_upload
from app.review.assistant.chat_trust import accuracy, apply_checks, trust_report
from app.review.assistant.chat_unknowns import unknown_records
from app.review.assistant.sandbox import ChatWorkspace
from app.review.assistant.vendor_consistency import (
    CREATED_BY,
    ConsistencyResult,
    check_pack,
    summary_line,
)

__all__ = [
    "CHECK_MODEL",
    "NO_MODEL_CALL_MICROS",
    "QUESTION",
    "ConsistencyAnswer",
    "PackNotReady",
    "answer_words",
    "run_check",
]

#: The question the chat shows for the action.
QUESTION: Final = "Vendor consistency check"
#: The answer's "model": code, not an AI (the footer reads "Code Check").
CHECK_MODEL: Final = "code-check"
#: What a code-only answer costs: it makes no model call at all (not a cost left unrecorded).
NO_MODEL_CALL_MICROS: Final = 0
#: The finding fields the engine's checker always sets (`ChatEngine._checked`).
_ALWAYS: Final = (
    "ledger_ids",
    "reference_side",
    "sum_text",
    "settled",
    "question",
    "keep_reason",
    "also_on",
    "parts_sum",
    "escalation",
    "round_status",
    "second_read_values",
)


class PackNotReady(RuntimeError):
    """The set's dimension labels are not made yet (the set pack is still being built)."""


@dataclass(frozen=True, slots=True)
class ConsistencyAnswer:
    text: str
    blocks: list[dict[str, Any]]
    api_content: list[dict[str, Any]]
    """The question and answer as the next message's history: the AI then knows what code found."""
    result: ConsistencyResult


def answer_words(result: ConsistencyResult, wrong: int) -> str:
    """The answer's words: what was checked and what came out, plainly."""
    lines = [summary_line(result)]
    if wrong:
        lines.append(
            f"{wrong} {'item is' if wrong == 1 else 'items are'} wrong in the vendor's own "
            "numbers: each is proved by code from the drawing (listed below, with the proof)."
        )
    else:
        lines.append(
            "Nothing in the vendor's own numbers contradicts itself where code could check."
        )
    if result.strings.get("doubt"):
        doubt = result.strings["doubt"]
        lines.append(
            f"{doubt} {'string was' if doubt == 1 else 'strings were'} left out: code could not "
            "prove from the drawing that the numbers belong together."
        )
    lines.append(
        "Code only, no AI: it adds up every dimension string whose parts and overall share their "
        "extension lines (an overhang or inset is never added up), compares two values printed "
        "for the same span, and lists what the set lacks."
    )
    return "\n\n".join(lines)


def run_check(
    workspace: ChatWorkspace,
    listing: Sequence[Mapping[str, Any]],
    *,
    check: Callable[..., ConsistencyResult | None] = check_pack,
) -> ConsistencyAnswer:
    """The check on the chat's set pack, as a stored answer's text and blocks. Raises
    `PackNotReady` when the labels are not made yet."""
    result = check(workspace.set_pack, workspace.uploads)
    if result is None:
        raise PackNotReady("the drawing set is still being read")
    main = main_upload(listing)
    book = FindingBook(
        workspace=workspace,
        listing=listing,
        page_count=int(main.get("pages") or 0) if main is not None else 0,
    )

    def checked(finding: dict[str, Any]) -> dict[str, Any]:
        out = apply_checks(finding, book.pdf_paths(), None)
        for key in _ALWAYS:
            out.setdefault(key, None)
        return out

    book.checker = checked
    for finding in result.findings:
        book.add_code_finding(finding, created_by=CREATED_BY)
    findings = book.live()
    unknowns = unknown_records(findings, None, set_gaps=result.unknowns)
    text = answer_words(result, sum(1 for f in findings if f.get("status") == "different"))
    blocks: list[dict[str, Any]] = [{"type": "text", "text": text, "by": "code"}]
    blocks += [{"type": "finding", "finding": finding} for finding in findings]
    if unknowns:
        blocks.append({"type": "unknowns", "unknowns": unknowns})
    if findings or unknowns:
        blocks.append(
            {"type": "trust", "trust": trust_report(findings, None, None, unknowns=unknowns)}
        )
    blocks.append({"type": "accuracy", "accuracy": accuracy(findings, [], None, unknowns)})
    listed = [
        f"- {f['id']} p.{f['page']} {f['item']}: vendor {f['vendor']}, should be {f['architect']}"
        f" ({f.get('why') or ''})"
        for f in findings
    ] + [f"- not checked: p.{u.get('page')} {u['what']}" for u in unknowns]
    history = "\n".join([text, *listed])
    api_content = [
        {"role": "user", "content": [{"type": "text", "text": QUESTION}]},
        {"role": "assistant", "content": [{"type": "text", "text": history}]},
    ]
    return ConsistencyAnswer(text=text, blocks=blocks, api_content=api_content, result=result)
