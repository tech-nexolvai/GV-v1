"""What an answer did NOT check, as records (chat accuracy, P6): built by code, $0.

Each answer gets a list of unknowns, one record each:
`{id: "U1", page, what, why, needs, scope}`, where `why` is one of

- `not_opened`: a page the answer never accounted for, or a "What's right" item whose two sides were
  not both looked at;
- `unreadable`: a value drawn as pen strokes (no text layer) whose second read was unsure or not run;
- `missing_job_value`: a GV check that could not run because a job value is missing;
- `pairing_uncertain`: a "What's right" item whose page pair is not compared;
- `refused_alignment`: two rounds of a sheet that code could not line up (the change was not
  measured).

`needs` says what would settle it. `scope` is `page` (a whole page) or `item` (one value): the
accuracy score counts page gaps through the pages accounted for and item gaps against the claims.
The trust card counts them; a follow-up "check the rest" message gets them as its work list.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

__all__ = [
    "UNKNOWN_WHYS",
    "asks_for_the_rest",
    "last_unknowns",
    "refused_from_diff",
    "unknown_records",
    "work_list_note",
]

UNKNOWN_WHYS: Final = (
    "unreadable",
    "not_opened",
    "missing_job_value",
    "pairing_uncertain",
    "refused_alignment",
)
#: The most records one answer lists (a 300-page set with nothing opened stays readable).
_MAX_UNKNOWNS: Final = 200
#: The most records a "check the rest" note hands the lead.
_MAX_IN_NOTE: Final = 40

_NEEDS: Final = {
    "not_opened": "open the page and compare it with its architect page",
    "not_viewed": "look at both sides at this spot and compare them",
    "unreadable": "a person reads the value on the drawing (it is drawn as pen strokes)",
    "missing_job_value": "the job value from GV (add it to the job, then check again)",
    "pairing_uncertain": "find the architect page this page matches, then compare",
    "refused_alignment": "compare the two rounds by eye (code could not line them up)",
}


def _sides_unreadable(finding: Mapping[str, Any]) -> bool:
    checks = finding.get("checks") or {}
    pen = "no_text" in (checks.get("printed_architect"), checks.get("printed_vendor"))
    return pen and checks.get("second_read") in ("unsure", "not_run", None)


def _page(value: object) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def unknown_records(
    findings: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any] | None,
    *,
    waiting: Sequence[Mapping[str, Any]] = (),
    refused_diffs: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The answer's unknowns, by code, in page order.

    `findings`: the live findings (with checks and `not_compared_why`). `coverage`: the coverage map
    when the answer was whole-set work (its unaccounted pages become `not_opened`). `waiting`: GV
    checks waiting on a job value (`Recorded.waiting`). `refused_diffs`: round pairs code would not
    line up, `{page, round2_page?, reason}` (`refused_from_diff`)."""
    records: list[dict[str, Any]] = []
    if coverage:
        for entry in coverage.get("pages") or []:
            if entry.get("status") == "not_opened" and not entry.get("note"):
                page = _page(entry.get("page"))
                records.append(
                    {
                        "page": page,
                        "what": f"Page {page}",
                        "why": "not_opened",
                        "needs": _NEEDS["not_opened"],
                        "scope": "page",
                    }
                )
    for finding in findings:
        label = f"{finding.get('id')} {finding.get('item') or 'item'}".strip()
        why = finding.get("not_compared_why")
        if finding.get("status") == "same" and why in ("not_viewed", "pair_not_covered"):
            records.append(
                {
                    "page": _page(finding.get("page")),
                    "what": label,
                    "why": "not_opened" if why == "not_viewed" else "pairing_uncertain",
                    "needs": _NEEDS["not_viewed" if why == "not_viewed" else "pairing_uncertain"],
                    "scope": "item",
                }
            )
        elif _sides_unreadable(finding):
            records.append(
                {
                    "page": _page(finding.get("page")),
                    "what": label,
                    "why": "unreadable",
                    "needs": _NEEDS["unreadable"],
                    "scope": "item",
                }
            )
    for item in waiting:
        records.append(
            {
                "page": _page(item.get("page")),
                "what": str(item.get("what") or "GV check"),
                "why": "missing_job_value",
                "needs": _NEEDS["missing_job_value"],
                "scope": "item",
            }
        )
    for diff in refused_diffs:
        later = _page(diff.get("round2_page"))
        what = f"Round change on page {diff.get('page')}" + (f" vs page {later}" if later else "")
        reason = str(diff.get("reason") or "").strip()
        records.append(
            {
                "page": _page(diff.get("page")),
                "what": what + (f" ({reason})" if reason else ""),
                "why": "refused_alignment",
                "needs": _NEEDS["refused_alignment"],
                "scope": "item",
            }
        )
    records.sort(key=lambda r: (r["page"] is None, r["page"] or 0, UNKNOWN_WHYS.index(r["why"])))
    return [
        {"id": f"U{number}", **record}
        for number, record in enumerate(records[:_MAX_UNKNOWNS], start=1)
    ]


_ROUND_PAGES: Final = re.compile(r"Round 1 p\.(\d+) vs round 2 p\.(\d+)")


def refused_from_diff(
    result: Mapping[str, Any] | None, tool_input: Mapping[str, Any] | None = None
) -> dict[str, Any] | None:
    """A refused round diff (`revision_diff.compare_rounds` returned `aligned: false`) as an input
    to `unknown_records`; `None` when the rounds lined up (or it was a list of pairs). The pages
    come from the tool's input, else from the result's text ("Round 1 p.4 vs round 2 p.9")."""
    if not isinstance(result, Mapping):
        return None
    diff = result.get("diff")
    if not isinstance(diff, Mapping) or diff.get("aligned") is not False:
        return None
    given = tool_input if isinstance(tool_input, Mapping) else {}
    first, later = given.get("round1_page"), given.get("round2_page")
    named = _ROUND_PAGES.search(str(result.get("text") or ""))
    if named is not None:
        first = first if isinstance(first, int) else int(named.group(1))
        later = later if isinstance(later, int) else int(named.group(2))
    return {"page": first, "round2_page": later, "reason": "the drawings do not line up"}


# ---- "check the rest" ----------------------------------------------------------------------------

_THE_REST: Final = re.compile(
    r"\b(?:check|do|finish|cover|look at|go through|work through|open)\b.{0,30}?"
    r"\b(?:the )?(?:rest|remaining|missing|unknowns?|not checked|left over|what you (?:missed|skipped))\b",
    re.IGNORECASE,
)


def asks_for_the_rest(question: str) -> bool:
    """Whether a follow-up asks to work off what the last answer did not check."""
    return bool(_THE_REST.search(question or ""))


def last_unknowns(
    blocks_by_message: Iterable[Sequence[Mapping[str, Any]] | None],
) -> list[dict[str, Any]]:
    """The unknowns of the latest answer that has an `unknowns` block (answers oldest first)."""
    found: list[dict[str, Any]] = []
    for blocks in blocks_by_message:
        for block in blocks or []:
            if isinstance(block, Mapping) and block.get("type") == "unknowns":
                found = [dict(u) for u in block.get("unknowns") or [] if isinstance(u, Mapping)]
    return found


def work_list_note(unknowns: Sequence[Mapping[str, Any]]) -> str | None:
    """One short note to the lead: the last answer's unknowns as this answer's work list."""
    if not unknowns:
        return None
    lines = [
        f"- {u.get('id')} page {u.get('page') or '?'}: {u.get('what')} ({u.get('why')}); "
        f"needs: {u.get('needs')}"
        for u in unknowns[:_MAX_IN_NOTE]
    ]
    more = len(unknowns) - _MAX_IN_NOTE
    if more > 0:
        lines.append(f"- and {more} more of the same kind")
    return (
        "GV Review note (by code): the last answer left these unchecked. Work them off as your "
        "list; for each, report a finding, record coverage, or say why it still cannot be "
        "checked.\n" + "\n".join(lines)
    )
