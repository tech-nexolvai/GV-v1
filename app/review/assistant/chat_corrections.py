"""Follow-up corrections (#1548): a later message corrects a finding of an earlier answer.

claude.ai, asked a follow-up that showed more, wrote "correction to my earlier table: ...". Here the
lead does it with the same tools: `update_finding` / `drop_finding` with an earlier answer's finding
(`A1-F5` = the chat's first answer, finding F5).

**Append-only.** The earlier answer's row is never changed. The correction is stored in the LATER
answer's blocks as `{type: "finding_update", update: {ref, message_id, finding_id, answer, version,
finding, dropped}}`: `message_id` / `finding_id` link it to the original, `version` counts the
corrections (the original is version 1), `finding` is the whole new version (or the last one, when
`dropped` gives the reason it was dropped).

**Read.** Whoever reads an answer (the chat screen, the marked-up PDF, the cloud drawing, the check
report, the CSV, the next message's list of earlier findings) applies the newest correction of each
of its findings (`with_corrections`): an updated finding shows its newest version with `updated_in`
(the later answer's id, its number, the version); a dropped one becomes a `finding_dropped` block
with `dropped_in`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final, Protocol

__all__ = [
    "REF",
    "Earlier",
    "corrections_by_answer",
    "earlier_findings",
    "earlier_findings_note",
    "parse_ref",
    "with_corrections",
]

#: An earlier answer's finding: `A<answer>-F<n>` (answers numbered from 1 in the chat).
REF: Final = re.compile(r"^A([1-9]\d{0,3})-(F[1-9]\d{0,4})$")
#: Earlier findings listed to the lead in a follow-up, at most (differences first).
_LISTED: Final = 80


class _Message(Protocol):
    @property
    def id(self) -> Any: ...
    @property
    def role(self) -> str: ...
    @property
    def blocks(self) -> list[dict[str, Any]] | None: ...


def parse_ref(text: object) -> tuple[int, str] | None:
    """(answer number, finding id) of `A1-F5`, else `None`."""
    if not isinstance(text, str):
        return None
    found = REF.match(text.strip())
    return None if found is None else (int(found.group(1)), found.group(2))


def corrections_by_answer(
    messages: Iterable[_Message],
) -> dict[str, dict[str, dict[str, Any]]]:
    """Each corrected answer's newest correction per finding: original message id -> finding id ->
    the update (later answers win)."""
    newest: dict[str, dict[str, dict[str, Any]]] = {}
    for message in messages:
        if message.role != "assistant":
            continue
        for block in message.blocks or []:
            update = block.get("update") if isinstance(block, Mapping) else None
            if block.get("type") != "finding_update" or not isinstance(update, Mapping):
                continue
            target = str(update.get("message_id") or "")
            finding_id = str(update.get("finding_id") or "")
            if target and finding_id:
                newest.setdefault(target, {})[finding_id] = {
                    **dict(update),
                    "corrected_by": str(message.id),
                }
    return newest


def with_corrections(
    blocks: Sequence[Mapping[str, Any]] | None,
    updates: Mapping[str, Mapping[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    """An answer's blocks with each finding at its newest correction (the stored blocks are not
    changed)."""
    if blocks is None:
        return None
    if not updates:
        return [dict(block) for block in blocks]
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding")
        update = (
            updates.get(str(finding.get("id")))
            if block.get("type") == "finding" and isinstance(finding, Mapping)
            else None
        )
        if update is None:
            out.append(dict(block))
            continue
        assert isinstance(finding, Mapping)
        later = {
            "message_id": str(update.get("corrected_by") or ""),
            "answer": update.get("by_answer"),
            "version": int(update.get("version") or 2),
        }
        if update.get("dropped"):
            out.append(
                {
                    "type": "finding_dropped",
                    "finding_id": str(finding.get("id")),
                    "reason": str(update["dropped"]),
                    "dropped_in": later,
                }
            )
            continue
        newest = update.get("finding")
        shown = dict(newest) if isinstance(newest, Mapping) else dict(finding)
        shown["id"] = finding.get("id")
        shown["updated_in"] = later
        out.append({**dict(block), "finding": shown})
    return out


#: One earlier finding at its newest version: `{ref, message_id, answer, version, finding}`.
type Earlier = dict[str, Any]


def earlier_findings(messages: Sequence[_Message]) -> dict[str, Earlier]:
    """Every finding of the chat's stored answers at its newest version, by ref (`A1-F5`)."""
    answers = [message for message in messages if message.role == "assistant"]
    updates = corrections_by_answer(answers)
    found: dict[str, Earlier] = {}
    for number, message in enumerate(answers, start=1):
        corrected = updates.get(str(message.id), {})
        for block in with_corrections(message.blocks or [], corrected) or []:
            finding = block.get("finding")
            if block.get("type") != "finding" or not isinstance(finding, Mapping):
                continue
            finding_id = str(finding.get("id") or "")
            if not finding_id:
                continue
            ref = f"A{number}-{finding_id}"
            version = int(corrected.get(finding_id, {}).get("version") or 1)
            found[ref] = {
                "ref": ref,
                "message_id": str(message.id),
                "answer": number,
                "version": version,
                "finding": {key: value for key, value in finding.items() if key != "updated_in"},
            }
    return found


def _words(finding: Mapping[str, Any]) -> str:
    status = {"different": "wrong", "check": "not compared", "same": "right"}.get(
        str(finding.get("status")), str(finding.get("status") or "")
    )
    return (
        f"p.{finding.get('page')} {' '.join(str(finding.get('item') or '').split())[:80]}: "
        f"architect {finding.get('architect')}, vendor {finding.get('vendor')} ({status})"
    )


def earlier_findings_note(found: Mapping[str, Earlier]) -> str | None:
    """The follow-up's short list of earlier findings the lead may correct (differences first)."""
    if not found:
        return None
    ordered = sorted(
        found.values(),
        key=lambda item: (
            {"different": 0, "check": 1}.get(str(item["finding"].get("status")), 2),
            item["answer"],
            int(str(item["finding"].get("id", "F0"))[1:] or 0),
        ),
    )
    lines = [
        "<earlier_findings>",
        (
            "Findings of earlier answers in this chat. If this message shows one was wrong or "
            "incomplete, correct it with update_finding or drop_finding and its id (the earlier "
            "answer then shows your newest version):"
        ),
        *(f"- {item['ref']} {_words(item['finding'])}" for item in ordered[:_LISTED]),
    ]
    if len(ordered) > _LISTED:
        lines.append(f"({len(ordered) - _LISTED} more, mostly right items, not listed.)")
    lines.append("</earlier_findings>")
    return "\n".join(lines)
