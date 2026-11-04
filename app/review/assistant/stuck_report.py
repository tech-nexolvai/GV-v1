"""Where a full check got stuck (Anant, 2026-11-04): for every answer that is not red or green, the
step that stopped it, worked out by code from the answer's own fields, $0.

Why: code falls back to a please-check or Not compared whenever it cannot prove something, so a
new drawing style shows up as misses, never as false reds. Finding which step stopped each miss
took hours of hand auditing. Now every full check names that step for each open answer, counts
them, and adds how much of the drawings the set pack read (`reading_coverage.json`), so a new
drawing shows its own gaps.

**The stages** (`STAGES`; the first rule that fits wins, in this order):

- `ai_only`: an AI saw it and code has no check that reads it (the page reviewer's and the
  whole-drawing look's please-checks on labels, finishes, scope, layout; a helper's reading never
  recorded at its spot, `internal`; a note in words);
- `rule`: a settled rule keeps it from red (a rule note, a kind of check still being proven
  (`held`), a job value or GV setting not entered, an overall past its sized pieces, a field-verify
  note, a code check that only ever makes please-checks, a typed-over overall);
- `string_not_seen`: a vendor string whose parts only an AI listed: code could not see the row on
  the drawing (`checks.parts_read` "ai", or `string_not_verified`);
- `no_architect_value`: the architect prints nothing to compare (or only parts, only a scaled
  size, or the reference is not the architect's);
- `pairing`: not sure which architect page or value the vendor's pairs with;
- `same_span`: code read both numbers but could not show they measure the same span (different
  reference points, a centre line, ends not confirmed);
- `reading`: a number the item rests on was not read, or not read the same way, by code;
- `second_look`: code proved it, but the blind AI second look (second or third reading, the
  devil's advocate, a blind pick) disagreed or was unsure;
- `not_proved`: code tried and could not prove a difference an AI reported;
- `other`: none of these (a new kind of answer: worth a look).

**The groups** are the result screen's (`proof.group`, worked out by `proof.py`): `wrong` (red),
`right` (green), `likely_problem` (please check), `not_compared`, `rule_note` (ask the architect)
and `dismissed` (the reviewer's "Not a problem"). Only red, green and dismissed have no stage. A
finding with no `proof` is grouped by its card's question as `card_fold.question_key` makes it.

**Pages.** An answer can only be stuck where the check compared something. A vendor page whose
architect page code was not sure of (`pairing_unsure` in the coverage block), or that has no
architect drawing, produces no architect answer at all, so the report lists those pages with their
own stage (`PAGE_STAGES`) beside the answers.

**One row per finding id.** Only the chosen answer's findings (a chat JSON holds every full check
of the chat, each numbering from F1: only the latest is read). A later block of the same id
replaces the earlier one (a stream re-sends a finding each time it changes) and keeps its place;
a finding the check dropped (`finding_dropped`) is not an answer. The copies of one card stay rows
of their own (each is an answer on its own page); the totals also count cards.

It only reports: it never changes a finding, a page or a verdict. A full check stores it in its
trust block (`FIELD`; not part of the chat's read model, so the chat's payload stays the same);
`GET /chats/{chat_id}/stuck-report` serves it, and `python -m app.review.assistant.stuck_report
<run.json> [--pack <set pack folder>] [--json]` prints it from a saved run.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant import classification, reading_coverage
from app.review.assistant.witness import read_result

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine

__all__ = [
    "FIELD",
    "GROUPS",
    "PAGE_STAGES",
    "SCHEMA",
    "STAGES",
    "answers",
    "attach",
    "build",
    "checked",
    "from_blocks",
    "group_of",
    "latest_check",
    "lines",
    "main",
    "prepared",
    "stage_of",
    "stored",
]

_log = logging.getLogger(__name__)

#: The trust block's key the report is stored under.
FIELD: Final = "stuck"
SCHEMA: Final = "gv-stuck-report/1"

#: Each stage in plain words, in the order the report shows them.
STAGES: Final[Mapping[str, str]] = {
    "reading": "a number was not read (or not read the same way) by code",
    "string_not_seen": "code could not see the vendor's string on the drawing",
    "pairing": "not sure which architect page or value it pairs with",
    "same_span": "code could not show both numbers measure the same span",
    "no_architect_value": "the architect gives nothing to compare",
    "second_look": "code proved it, but the blind second look disagreed or was unsure",
    "not_proved": "code could not prove a difference an AI reported",
    "rule": "a settled rule keeps it from red",
    "ai_only": "found by an AI only; code has no check that reads it",
    "other": "none of the above",
}
#: A page's own stage, when the check never compared it with an architect page.
PAGE_STAGES: Final[Mapping[str, str]] = {
    "pairing": "pairing unsure: no architect comparison ran on the page",
    "no_architect_drawing": "the upload has no architect drawing of it",
    "nothing_to_compare": "nothing on the page to compare with the architect",
    "not_opened": "the check did not open the page",
}
#: The result screen's groups (`proof.GROUPS`, with `clouded` called `wrong`).
GROUPS: Final = ("wrong", "right", "likely_problem", "not_compared", "rule_note", "dismissed")
_SCREEN: Final[Mapping[str, str]] = {
    "clouded": "wrong",
    "right": "right",
    "likely_problem": "likely_problem",
    "not_compared": "not_compared",
    "rule_note": "rule_note",
    "dismissed": "dismissed",
}
#: Groups with nothing stuck: red, green, and what the reviewer marked fine.
_DONE: Final = frozenset({"wrong", "right", "dismissed"})
#: A Likely problem's card questions (`card_fold.question_key`).
_LIKELY_KEYS: Final = frozenset({"unsized_ends", "ends", "pick", "likely"})

#: Each Not-compared reason's stage (`classification.REASONS`); `not_proved` is worked out.
_REASON_STAGES: Final[Mapping[str, str]] = {
    "nothing_to_compare": "no_architect_value",
    "no_overall": "no_architect_value",
    "ai_unsure": "no_architect_value",
    "architect_disagrees": "no_architect_value",
    "round_only": "no_architect_value",
    "prototype": "no_architect_value",
    "prototype_only": "no_architect_value",
    "no_sheet": "pairing",
    "different_reference": "same_span",
    "unreadable": "reading",
    "no_spot": "reading",
    "stroke_words": "reading",
    "gv_sticker": "reading",
    "field_verify": "rule",
    "architect_outside_gv_rule": "rule",
    "outside_gv_rule": "rule",
    "missing_setting": "rule",
    "words": "ai_only",
    "internal": "ai_only",
    "string_not_verified": "string_not_seen",
    "tags_differ": "not_proved",
    "tag_unconfirmed": "not_proved",
    "finish_not_comparable": "not_proved",
    "not_seen": "not_proved",
}

#: The AI authors of a finding (`chat_contract.ChatFindingOut.created_by`); the rest are code's.
_AI_AUTHORS: Final = frozenset({"lead", "page_reviewer", "whole_look"})
#: AI authors that use `pairing_not_certain` only to say "no architect side" (vendor-only looks).
_VENDOR_ONLY_LOOKS: Final = frozenset({"page_reviewer", "whole_look"})
#: Who made a please-check, in plain words (`likely_problems.BY` and `created_by`).
_WHO: Final[Mapping[str, str]] = {
    "page_reviewer": "the page reviewer (AI)",
    "whole_look": "the whole-drawing look (AI)",
    "whole_drawing_look": "the whole-drawing look (AI)",
    "lead": "the lead (AI)",
}
#: Second and third readings, in words.
_READ_WORDS: Final[Mapping[str, str]] = {
    "unsure": "was unsure",
    "disagrees": "disagrees",
    "agrees": "agrees",
    "agrees_after_adjudication": "agrees",
    "not_run": "did not run",
}
#: Why code could not see a string, inside the please-check's reason (`vendor_consistency`).
_STRING_WHY: Final = re.compile(r"one string on the drawing \((.+?)\); please check", re.DOTALL)
#: A typed-over overall's please-check (`proof.typed_over`).
_TYPED_OVER: Final = re.compile(r"looks typed over")
#: A helper's reading never recorded at its spot (`classification.internal`).
_INTERNAL_WORDS: Final = (
    "a helper (AI) read it but never recorded it at its spot, so code never read it (kept for "
    "GV's reviewer only)"
)
#: The longest reason a row keeps.
_MAX_REASON: Final = 200


# ---- reading a finding ---------------------------------------------------------------------------


def _map(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _short(text: object, limit: int = _MAX_REASON) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def _question(finding: Mapping[str, Any]) -> str | None:
    key = _map(_map(finding.get("card")).get("question")).get("key")
    return str(key) if key else None


def _ai_author(finding: Mapping[str, Any]) -> bool:
    by = str(finding.get("created_by") or "")
    return by in _AI_AUTHORS or by.startswith("helper")


def _who(finding: Mapping[str, Any]) -> str:
    by = str(finding.get("created_by") or "")
    if by.startswith("helper"):
        return "a helper (AI)"
    look = str(_map(_map(finding.get("proof")).get("likely_problem")).get("by") or "")
    return _WHO.get(by) or _WHO.get(look) or "an AI"


def _witnesses(finding: Mapping[str, Any]) -> list[str]:
    found = finding.get("witnesses")
    return [str(w) for w in found] if isinstance(found, list) else []


def _verify(checks: Mapping[str, Any], side: str) -> Mapping[str, Any]:
    return _map(checks.get(f"verify_{side}"))


def _read_doubt(finding: Mapping[str, Any], checks: Mapping[str, Any]) -> tuple[str, str] | None:
    """(side, plain words) when code's own reading of a number's spot failed, disagrees with the
    number, or never finished; `None` when every side it read agrees."""
    sides = ("vendor",) if finding.get("source") == "vendor_arithmetic" else ("vendor", "architect")
    for side in sides:
        verified = _verify(checks, side)
        if verified.get("pending"):
            return side, f"code's reading of the {side}'s number at its spot never finished"
        result = verified.get("result")
        if result == "mismatch":
            read = _short(verified.get("read"), 40)
            return side, f"code reads {read or 'another value'} at the {side}'s spot"
        if result == "cannot_read":
            return side, f"code could not read the {side}'s number at its spot"
    return None


def group_of(finding: Mapping[str, Any]) -> str:
    """The result screen's group (`GROUPS`): the server's `proof.group`; for a finding without one,
    its card's question (`card_fold.question_key`: a Not-compared reason or a job value missing is
    Not compared, a please-check's question a Likely problem), else its status."""
    group = _map(finding.get("proof")).get("group")
    if isinstance(group, str) and group in _SCREEN:
        return _SCREEN[group]
    key = _question(finding)
    status = finding.get("status")
    if key is None:
        if status == "different":
            return "wrong"
        if status == "same" and finding.get("compared") is not False:
            return "right"
        return "not_compared"
    if key in _SCREEN:
        return _SCREEN[key]
    if key in classification.REASONS or key.startswith("missing_setting"):
        return "not_compared"
    if key in _LIKELY_KEYS or key.startswith(("look:", "held:")):
        return "likely_problem"
    return "not_compared"


def _string_words(finding: Mapping[str, Any], checks: Mapping[str, Any]) -> str:
    said = str(_map(finding.get("proof")).get("reason") or "")
    found = _STRING_WHY.search(said)
    if found:
        words = found.group(1)
    else:
        words = str(_map(finding.get("string_reads")).get("why") or "") or (
            "code could not see the parts as one string on the drawing; it has only the AI's list "
            "of parts"
        )
    if _verify(checks, "vendor").get("result") == "cannot_read":
        words += "; code's own reading of the spot could not read the number either"
    return _short(words)


def _second_words(finding: Mapping[str, Any], checks: Mapping[str, Any], key: str | None) -> str:
    if key == "pick":
        return "code paired the two dimensions, but a blind AI pick chose another dimension"
    said = []
    second = checks.get("second_read")
    if second:
        said.append(f"the blind second reading {_READ_WORDS.get(str(second), str(second))}")
    third = checks.get("adjudication")
    if third:
        said.append(f"the third reading {_READ_WORDS.get(str(third), str(third))}")
    if checks.get("critic") == "objects":
        said.append("the devil's advocate objects")
    words = "code proved it, but " + (" and ".join(said) or "a second look did not agree")
    # What the second look read, when it is not the number the finding names (it may have been
    # asked about, or have read, another number near it).
    named = _short(finding.get("vendor"), 40)
    seen = _short(_map(finding.get("second_read_values")).get("vendor"), 40)
    if seen and named and _bare(seen) != _bare(named):
        words += f"; it read {seen} where the finding names {named}"
    return words


def _bare(value: str) -> str:
    """A value with no spaces, quotes or inch marks, to tell two readings of one number apart."""
    return re.sub(r"[\s\"”″']", "", value).lower()


def stage_of(finding: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]] | None:
    """The stage that stopped the finding (`STAGES`), the reason in plain words, and the fields
    that decided it; `None` for a red, a green or a reviewer's "Not a problem". Reads only the
    finding's fields as the API serves them (`proof`, `card`, `checks`, `source`, `created_by`,
    `check_reason`, `witnesses`, `string_reads`): the same answer from a saved run as from a
    stored check."""
    group = group_of(finding)
    if group in _DONE:
        return None
    proof = _map(finding.get("proof"))
    checks = _map(finding.get("checks"))
    key = _question(finding) or ""
    reason = str(proof.get("not_compared") or "")
    status = finding.get("status")
    source = finding.get("source")
    parts_read = checks.get("parts_read") or finding.get("parts_read")
    said = _short(proof.get("reason") or finding.get("why"))
    by = finding.get("created_by")
    code_read = bool(_witnesses(finding)) or parts_read == "code"

    if reason == "internal" or key == "internal":
        return "ai_only", _INTERNAL_WORDS, {"proof.not_compared": "internal", "created_by": by}
    if group == "rule_note":
        return "rule", said or "the architect's size breaks a GV rule", {"proof.group": group}
    if proof.get("held") or key.startswith("held:"):
        return (
            "rule",
            said or "code proved it, but this kind of check is still being proven",
            {"proof.held": True, "card.question": key or None},
        )
    if key == "unsized_ends":
        return "rule", said, {"card.question": key}
    if reason == "missing_setting" or key.startswith("missing_setting"):
        missing = key.split(":", 1)[1].replace("|", " and ") if ":" in key else None
        words = f"a job value is not entered: {missing}" if missing else said
        return "rule", words, {"proof.not_compared": reason or None, "card.question": key or None}
    gap = _map(finding.get("string_reads")).get("outcome") == "gap"
    if source == "vendor_arithmetic" and (
        parts_read == "ai" or gap or reason == "string_not_verified"
    ):
        used: dict[str, Any] = {"source": source, "checks.parts_read": parts_read}
        if gap:
            used["string_reads"] = "gap"
        if reason:
            used["proof.not_compared"] = reason
        if _verify(checks, "vendor").get("result"):
            used["checks.verify_vendor"] = _verify(checks, "vendor").get("result")
        return "string_not_seen", _string_words(finding, checks), _kept(used)
    if reason in _REASON_STAGES:
        words = said or classification.REASON_WORDS.get(reason, reason)
        return _REASON_STAGES[reason], words, {"proof.not_compared": reason}
    if key == "pick":
        return "second_look", _second_words(finding, checks, key), {"card.question": key}
    if key == "ends":
        return "same_span", said, {"card.question": key}
    if status == "different" and code_read:
        used = {"witnesses": _witnesses(finding) or None, "checks.parts_read": parts_read}
        doubt = _read_doubt(finding, checks)
        if doubt is not None:
            side, words = doubt
            used[f"checks.verify_{side}"] = _verify(checks, side).get("result") or "pending"
            return "reading", f"code's own check backs it, but {words}", used
        result = read_result(checks)
        if result in ("unsure", "disagrees") or checks.get("critic") == "objects":
            used.update(
                {
                    "checks.second_read": checks.get("second_read"),
                    "checks.adjudication": checks.get("adjudication"),
                    "checks.critic": checks.get("critic"),
                    "vendor": finding.get("vendor"),
                    "second_read.vendor": _map(finding.get("second_read_values")).get("vendor"),
                }
            )
            return "second_look", _second_words(finding, checks, None), _kept(used)
    if source == "vendor_arithmetic" and status == "different" and parts_read is None:
        return (
            "reading",
            "code did not read the string's parts itself",
            {"source": source, "checks.parts_read": None},
        )
    inches = _map(finding.get("inches"))
    if status == "different" and _ai_author(finding) and inches.get("vendor") is not None:
        doubt = _read_doubt(finding, checks)
        if doubt is not None and doubt[0] == "vendor" and checks.get("printed_vendor") != "match":
            return (
                "reading",
                f"an AI read the vendor's {_short(finding.get('vendor'), 40)}, but {doubt[1]}",
                {"created_by": by, "checks.verify_vendor": _verify(checks, "vendor").get("result")},
            )
    if (
        finding.get("check_reason") == "pairing_not_certain"
        and _ai_author(finding)
        and by not in _VENDOR_ONLY_LOOKS
    ):
        return "pairing", said, {"check_reason": "pairing_not_certain", "created_by": by}
    if group == "likely_problem":
        if _ai_author(finding) and not code_read:
            kind = _map(proof.get("likely_problem")).get("kind")
            what = f" ({kind})" if kind else ""
            words = (
                f"found by {_who(finding)} looking at the drawing{what}; no code check reads it, "
                "so it stays a please-check"
            )
            return (
                "ai_only",
                words,
                _kept(
                    {
                        "created_by": by,
                        "evidence_kind": finding.get("evidence_kind"),
                        "card.question": key or None,
                    }
                ),
            )
        if _TYPED_OVER.search(said):
            return "rule", said, {"proof.reason": "typed over"}
        if not _ai_author(finding) and status == "check":
            words = (
                f"a please-check by code's own check ({by or 'code'}), whose rules never make this "
                f"case red: {said}"
            )
            return "rule", words, {"created_by": by, "status": status}
    if group == "not_compared" or reason == "not_proved":
        return "not_proved", said, _kept({"proof.not_compared": reason or None, "created_by": by})
    return "other", said, _kept({"created_by": by, "card.question": key or None, "status": status})


def _kept(used: Mapping[str, Any]) -> dict[str, Any]:
    """The evidence fields that were set (a field that is absent says nothing)."""
    return {name: value for name, value in used.items() if value not in (None, [], "")}


# ---- the answer's findings -----------------------------------------------------------------------


def answers(blocks: Iterable[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """An answer's findings, one per id, in the order they first appeared: a later block of the
    same id replaces the earlier one; a finding dropped after it was shown (`finding_dropped`) is
    left out. Returns them with how many repeated blocks were merged."""
    kept: dict[str, dict[str, Any]] = {}
    merged = 0
    for block in blocks:
        if not isinstance(block, Mapping):
            continue
        if block.get("type") == "finding_dropped":
            kept.pop(str(block.get("finding_id")), None)
            continue
        finding = block.get("finding")
        if block.get("type") != "finding" or not isinstance(finding, Mapping):
            continue
        finding_id = str(finding.get("id") or "")
        if finding_id in kept:
            merged += 1
            kept[finding_id] = dict(finding)  # the newest version, at its first place
        else:
            kept[finding_id] = dict(finding)
    return list(kept.values()), merged


def prepared(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An answer's blocks with each finding's `proof` and `card` as the API serves them (worked out
    on read, with no reviewer decision: `proof.blocks_with_proof`, `card_fold.blocks_with_cards`).
    A stored answer's findings carry neither; a saved API answer's already have both."""
    findings = [
        block.get("finding")
        for block in blocks
        if block.get("type") == "finding" and isinstance(block.get("finding"), Mapping)
    ]
    if all(isinstance(_map(f).get("proof"), Mapping) for f in findings):
        return [dict(block) for block in blocks]
    from app.review.assistant.card_fold import blocks_with_cards
    from app.review.assistant.proof import blocks_with_proof

    return blocks_with_cards(blocks_with_proof(blocks))


def _coverage(blocks: Iterable[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    found = None
    for block in blocks:
        if isinstance(block, Mapping) and block.get("type") == "coverage":
            found = _map(block.get("coverage")) or None
    return found


def _page_rows(
    coverage: Mapping[str, Any] | None, findings: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """The vendor pages with no architect comparison, each with its own stage (`PAGE_STAGES`) and
    how many answers are on it (they come from checks of the vendor's page alone). A page with
    nothing to compare and no answer (a divider, a key plan) is left out: nothing is stuck there."""
    on_page = Counter(f.get("page") for f in findings)
    out: list[dict[str, Any]] = []
    for entry in (coverage or {}).get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        if entry.get("pairing_unsure"):
            why = entry.get("pairing_unsure_why")
            stage, reason = "pairing", f"pairing unsure ({why})" if why else "pairing unsure"
        elif entry.get("status") == "not_opened":
            stage, reason = "not_opened", str(entry.get("note") or "not opened")
        elif entry.get("drawing_needed"):
            stage = "no_architect_drawing"
            reason = str(entry.get("drawing_needed_why") or entry.get("note") or "")
        elif entry.get("status") == "not_comparable":
            stage, reason = "nothing_to_compare", str(entry.get("note") or "")
        else:
            continue
        page = entry.get("page")
        if stage == "nothing_to_compare" and not on_page.get(page):
            continue  # a divider, a key plan or a schedule with no answer: nothing is stuck there
        out.append(
            {
                "page": page,
                "stage": stage,
                "reason": _short(reason, 160),
                "answers": on_page.get(page, 0),
            }
        )
    return out


def _reading(
    coverage: Mapping[str, Any] | None, spots: Iterable[tuple[str | None, int]]
) -> dict[str, Any] | None:
    """The set pack's reading coverage (`reading_coverage.json`): numbers read of those printed per
    side, the unread ones by reason, and the vendor's count on each page that has a stuck answer
    (`spots`: its file, when known, and page)."""
    if not coverage:
        return None
    sides = _map(coverage.get("sides"))
    by_reason = _map(coverage.get("not_read_by_reason"))
    out: dict[str, Any] = {}
    for side in ("vendor", "architect"):
        counts = _map(sides.get(side))
        printed = int(counts.get("printed") or 0)
        if not printed:
            out[side] = None
            continue
        read = int(counts.get("read") or 0)
        reasons = _map(by_reason.get(side))
        out[side] = {
            "read": read,
            "printed": printed,
            "percent": round(100 * read / printed),
            "not_read": {
                str(name): int(count)
                for name, count in sorted(reasons.items(), key=lambda kv: (-int(kv[1]), kv[0]))
            },
        }
    wanted = set(spots)
    pages: list[dict[str, Any]] = []
    for entry in coverage.get("pages") or []:
        if not isinstance(entry, Mapping):
            continue
        counts = _map(_map(entry.get("sides")).get("vendor"))
        if not counts.get("printed"):
            continue
        page, file = entry.get("page"), entry.get("file")
        if (file, page) not in wanted and (None, page) not in wanted:
            continue
        unread: Counter[str] = Counter()
        for spot in entry.get("not_read") or []:
            if isinstance(spot, Mapping) and spot.get("side") == "vendor":
                unread[str(spot.get("reason"))] += int(spot.get("count") or 1)
        pages.append(
            {
                "page": page,
                "file": file,
                "read": int(counts.get("read") or 0),
                "printed": int(counts.get("printed") or 0),
                "not_read": dict(unread.most_common()),
            }
        )
    out["pages"] = pages
    return out


def build(
    findings: Sequence[Mapping[str, Any]],
    *,
    coverage: Mapping[str, Any] | None = None,
    reading: Mapping[str, Any] | None = None,
    merged: int = 0,
) -> dict[str, Any]:
    """The report (see the module): `findings` already one per id (`answers`), with their `proof`
    and `card` (`prepared`); `coverage` the answer's coverage block; `reading` the set pack's
    reading coverage. Small, plain JSON."""
    groups: Counter[str] = Counter()
    rows: list[dict[str, Any]] = []
    spots: set[tuple[str | None, int]] = set()
    for finding in findings:
        group = group_of(finding)
        groups[group] += 1
        found = stage_of(finding)
        if found is None:
            continue
        stage, reason, used = found
        page = finding.get("page")
        if isinstance(page, int):
            file = _map(finding.get("vendor_box")).get("file")
            spots.add((str(file) if file else None, page))
        card = _map(finding.get("card")).get("id")
        row: dict[str, Any] = {
            "id": str(finding.get("id") or ""),
            "page": finding.get("page"),
            "group": group,
            "stage": stage,
            "reason": _short(reason),
            "evidence": used,
        }
        if card and str(card) != row["id"]:
            row["card"] = str(card)  # a copy: the card it is shown on
        rows.append(row)
    stages: list[dict[str, Any]] = []
    for stage, words in STAGES.items():
        mine = [row for row in rows if row["stage"] == stage]
        if not mine:
            continue
        stages.append(
            {
                "stage": stage,
                "words": words,
                "answers": len(mine),
                "cards": len({row.get("card") or row["id"] for row in mine}),
                "by_group": dict(Counter(row["group"] for row in mine)),
            }
        )
    return {
        "schema": SCHEMA,
        "answers": len(findings),
        "merged": merged,
        "groups": {group: groups.get(group, 0) for group in GROUPS},
        "stuck": len(rows),
        "stages": stages,
        "findings": rows,
        "pages": _page_rows(coverage, findings),
        "reading": _reading(reading, spots),
    }


def from_blocks(
    blocks: Sequence[Mapping[str, Any]], *, reading: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The report of one answer from its blocks (stored, or as the API serves them)."""
    findings, merged = answers(prepared(blocks))
    return build(findings, coverage=_coverage(blocks), reading=reading, merged=merged)


def checked(blocks: Iterable[Mapping[str, Any]] | None) -> bool:
    """Whether a full check's answer checked anything: it holds findings or a trust report (one
    that stopped before any check holds neither)."""
    return any(_map(b).get("type") in ("finding", "trust") for b in blocks or [])


def latest_check(chat: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """A chat's newest full check (`GET /chats/{chat_id}`): its last answer in full-check mode that
    checked anything (`checked`)."""
    for message in reversed(list(chat.get("messages") or [])):
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        if message.get("mode") == "full_check" and checked(message.get("blocks")):
            return message
    return None


def attach(engine: ChatEngine) -> None:
    """Stores the report in a full check's trust block (`FIELD`), as its last step: after the files,
    the score and the timings, so it sees every finding as stored. Not sent as a live event (the
    screen does not show it) and never fails the answer."""
    if getattr(engine.settings, "mode", None) != "full_check":
        return
    index = engine._trust_index
    if index is None:
        return
    try:
        reading = reading_coverage.load(engine.tools.workspace.set_pack)
        report = from_blocks(engine.stored_blocks(), reading=reading)
    except Exception:  # noqa: BLE001 - stored without its report; the endpoint works it out
        _log.exception("where-it-got-stuck report failed")
        return
    with engine._lock:
        block = engine.blocks[index]
        if block.get("type") != "trust":
            return
        engine.blocks[index] = {"type": "trust", "trust": {**block["trust"], FIELD: report}}


def stored(blocks: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """The report a full check stored in its trust block, else `None` (an older check)."""
    for block in blocks or []:
        if isinstance(block, Mapping) and block.get("type") == "trust":
            found = _map(block.get("trust")).get(FIELD)
            if isinstance(found, Mapping) and found.get("schema") == SCHEMA:
                return found
    return None


# ---- the plain-English table ---------------------------------------------------------------------

_GROUP_WORDS: Final[Mapping[str, str]] = {
    "wrong": "wrong",
    "right": "right",
    "likely_problem": "likely problem",
    "not_compared": "not compared",
    "rule_note": "ask the architect",
    "dismissed": "reviewer: not a problem",
}
_SHORT_GROUP: Final[Mapping[str, str]] = {
    "likely_problem": "likely",
    "not_compared": "not cmp",
    "rule_note": "rule note",
}


def lines(report: Mapping[str, Any], title: str | None = None) -> list[str]:
    """The report as a plain-English table for a terminal."""
    out: list[str] = []
    if title:
        out += [title, ""]
    groups = _map(report.get("groups"))
    shown = " · ".join(
        f"{_GROUP_WORDS[g]} {groups[g]}" for g in GROUPS if groups.get(g) or g in ("wrong", "right")
    )
    out.append(f"Answers: {report.get('answers', 0)} ({shown}).")
    out.append(
        f"Stuck (not red or green): {report.get('stuck', 0)}. One row per finding id; "
        f"{report.get('merged', 0)} repeated rows merged."
    )
    out += ["", "Where they got stuck:"]
    columns = [g for g in ("likely_problem", "not_compared", "rule_note") if groups.get(g)]
    head = "  {:<19} {:>7} {:>6}".format("stage", "answers", "cards")
    head += "".join(f" {_SHORT_GROUP.get(g, g):>9}" for g in columns)
    out += [head + "  what it means"]
    for row in report.get("stages") or []:
        by = _map(row.get("by_group"))
        line = f"  {row['stage']:<19} {row['answers']:>7} {row['cards']:>6}"
        line += "".join(f" {by.get(g, 0):>9}" for g in columns)
        out.append(f"{line}  {row['words']}")
    pages = report.get("pages") or []
    if pages:
        out += ["", "Pages with no architect comparison (the page's own blocker):"]
        for page in pages:
            out.append(
                f"  p{page['page']:<4} {page['stage']:<20} {page['answers']:>3} answers  "
                f"{page['reason']}"
            )
    reading = _map(report.get("reading"))
    if reading:
        out += ["", "Numbers the set pack read:"]
        for side in ("vendor", "architect"):
            counts = _map(reading.get(side))
            if not counts:
                continue
            reasons = ", ".join(
                f"{reading_coverage.REASONS.get(name, name)} {count}"
                for name, count in list(_map(counts.get("not_read")).items())[:5]
            )
            out.append(
                f"  {side}: {counts['read']} of {counts['printed']} ({counts['percent']}%); "
                f"not read: {reasons or 'none'}"
            )
        for page in reading.get("pages") or []:
            reasons = ", ".join(
                f"{reading_coverage.REASONS.get(name, name)} {count}"
                for name, count in _map(page.get("not_read")).items()
            )
            out.append(
                f"    p{page['page']}: vendor {page['read']} of {page['printed']}"
                + (f"; not read: {reasons}" if reasons else "")
            )
    rows = report.get("findings") or []
    if rows:
        out += ["", "Each stuck answer:"]
        for row in sorted(rows, key=lambda r: (_page_number(r.get("page")), _id_number(r["id"]))):
            copy = f" (copy of {row['card']})" if row.get("card") else ""
            group = _SHORT_GROUP.get(row["group"], row["group"])
            out.append(
                f"  p{row.get('page')!s:<4} {row['id']:<6} {group:<9} {row['stage']:<17} "
                f"{row['reason']}{copy}"
            )
    return out


def _page_number(page: object) -> int:
    return page if isinstance(page, int) and not isinstance(page, bool) else 10**6


def _id_number(finding_id: str) -> tuple[int, str]:
    digits = re.sub(r"\D", "", finding_id)
    return (int(digits) if digits else 10**6, finding_id)


# ---- the command line ----------------------------------------------------------------------------


def _blocks_of(loaded: Any) -> tuple[list[Mapping[str, Any]], str | None]:
    """The blocks to report from a saved file and a title: a chat (`GET /chats/{chat_id}`: its
    newest full check), one message (its `blocks`), or a bare list of findings or blocks."""
    if isinstance(loaded, Mapping) and isinstance(loaded.get("messages"), list):
        message = latest_check(loaded)
        if message is None:
            raise SystemExit("no full check with findings in this chat")
        checks = sum(
            1
            for m in loaded["messages"]
            if isinstance(m, Mapping) and m.get("role") == "assistant"
            if m.get("mode") == "full_check" and checked(m.get("blocks"))
        )
        title = (
            f"Where it got stuck: {loaded.get('package_name') or 'drawing set'} · full check "
            f"{message.get('created_at')} (answer {str(message.get('id'))[:8]}; the newest of "
            f"{checks} full checks in this chat, each numbering its findings from F1, so only "
            "this one is read)"
        )
        return list(message.get("blocks") or []), title
    if isinstance(loaded, Mapping) and isinstance(loaded.get("blocks"), list):
        return list(loaded["blocks"]), None
    if isinstance(loaded, Mapping) and isinstance(loaded.get("findings"), list):
        loaded = loaded["findings"]
    if isinstance(loaded, list):
        return [
            item if _map(item).get("type") else {"type": "finding", "finding": item}
            for item in loaded
            if isinstance(item, Mapping)
        ], None
    raise SystemExit("not a saved chat, message or list of findings")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.review.assistant.stuck_report",
        description="Where a full check got stuck, from a saved run ($0, no AI call).",
    )
    parser.add_argument("run", type=Path, help="a saved chat JSON (GET /chats/{chat_id})")
    parser.add_argument("--pack", type=Path, help="the set pack folder (its reading coverage)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    given = parser.parse_args(argv)
    blocks, title = _blocks_of(json.loads(given.run.read_text(encoding="utf-8")))
    reading = reading_coverage.load(given.pack) if given.pack else None
    if given.pack and reading is None:
        print(f"(no reading coverage in {given.pack})", file=sys.stderr)
    report = stored(blocks) if not given.pack else None
    if report is None:
        report = from_blocks(blocks, reading=reading)
    if given.json:
        print(json.dumps(report, indent=1))
    else:
        print("\n".join(lines(report, title)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
