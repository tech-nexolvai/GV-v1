"""The full check's summary written by code, when the AI did not write its own (#1518).

A full check always ends with a short plain-English summary for the reviewer. The lead is told to
write it (`FULL_CHECK_INSTRUCTION`); when the message stops before it does (a cap, the turn limit,
a cancel, an error) or it writes nothing, code writes one from the findings, the coverage and the
trust report, with no AI call. Pure functions: no I/O, no money.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.review.assistant.chat_findings import products_words
from app.review.assistant.chat_trust import cloud_kind
from app.review.assistant.classification import not_compared_line
from app.review.assistant.impact import by_impact
from app.review.assistant.proof import classified, proof_counts, proof_group
from app.review.assistant.vendor_text import vendor_text

#: The small note under a summary code wrote (the screen shows it as written).
CODE_SUMMARY_NOTE: Final = "*Summary written by GV Review: the AI did not finish its own.*"

#: How many issues the summary names.
TOP_ISSUES: Final = 5


def needs_code_summary(text: str, stopped: str | None) -> bool:
    """A full check that stopped early, or ended with no words, gets code's summary."""
    return stopped is not None or not text.strip()


def top_issues(
    findings: Sequence[Mapping[str, Any]], limit: int = TOP_ISSUES
) -> list[Mapping[str, Any]]:
    """The issues by impact (`impact.by_impact`, #1569): wrong before your decision; among the
    wrong ones, those a check confirmed (Confirmed, Likely) before an AI reading only; then the
    biggest (the size of the difference, whether it changes fabrication, how sure); right items
    never."""
    return [f for f in by_impact(findings) if cloud_kind(f) is not None][:limit]


def _pages(pages: Sequence[int]) -> str:
    """[39, 40, 41, 51] as "p39-41, p51"."""
    ordered = sorted(set(pages))
    runs: list[list[int]] = []
    for page in ordered:
        if runs and page == runs[-1][-1] + 1:
            runs[-1].append(page)
        else:
            runs.append([page])
    return ", ".join(f"p{run[0]}" if len(run) == 1 else f"p{run[0]}-{run[-1]}" for run in runs)


def _issue_words(findings: Sequence[Mapping[str, Any]]) -> list[str]:
    """The top issues, the same item and values on several pages said once."""
    groups: dict[tuple[str, str, str], list[int]] = {}
    for finding in top_issues(findings, limit=len(findings)):
        # The guard on internal words (#1622, `vendor_text`): no record id or "see GV Review's
        # record" in the summary either.
        key = (
            vendor_text(finding.get("item"), f"Item on page {finding.get('page')}") or "Item",
            vendor_text(finding.get("vendor"), "?") or "?",
            vendor_text(finding.get("architect"), "no stated value") or "no stated value",
        )
        groups.setdefault(key, []).append(int(finding.get("page") or 0))
        if len(groups) > TOP_ISSUES:
            break
    return [
        f"{item}: vendor {vendor}, architect {architect} ({_pages(pages)})"
        for (item, vendor, architect), pages in list(groups.items())[:TOP_ISSUES]
    ]


def _stop_words(
    stopped: str | None, cap_hit: str | None, cap_usd: str, budget_usd: str, turns: int
) -> str | None:
    if stopped == "cost_cap" and cap_hit == "package":
        return (
            f"Stopped early: this drawing set used its ${budget_usd} AI budget, so the AI's final "
            "pass was not done; your admin can raise the budget."
        )
    if stopped == "cost_cap":
        return (
            f"Stopped early at the ${cap_usd} limit, so the AI's final pass was not done: ask it "
            "to go on, or run the full check again."
        )
    if stopped == "turn_limit":
        return (
            f"Stopped early after all {turns} tool turns, so the AI's final pass was not done: "
            "ask it to go on."
        )
    if stopped == "cancelled":
        return (
            "Stopped before the AI finished, so its final pass and the trust checks were not "
            "done: run the full check again to finish it."
        )
    if stopped == "error":
        return (
            "Stopped by an error before the AI finished, so its final pass and the trust checks "
            "were not done: run the full check again."
        )
    return None


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def code_summary(
    findings: Sequence[Mapping[str, Any]],
    *,
    coverage: Mapping[str, Any] | None,
    page_count: int,
    products: Sequence[str],
    stopped: str | None,
    cap_hit: str | None = None,
    cap_usd: str = "",
    budget_usd: str = "",
    turns: int = 0,
    files: Sequence[str] = (),
    also: Sequence[str] = (),
    reasons: Sequence[str] | None = None,
) -> str:
    """The 5-6 line summary: what was checked, the verdict, the biggest issues, the one line on
    what could not be compared (#1619), why it stopped (if it did) and the next step; then the
    small note. `also`: code's own lines to add before the next step (the vendor consistency
    check's, #1584). `reasons`: the Not-compared list's rows' reasons (its product scope), else
    worked out from `findings`."""
    # The cloud drawing's own counts (#1581, #1619): Wrong = proved by code (clouded), Right, and
    # what could not be compared; the screen and the files count the same final list.
    counts = proof_counts(findings)
    wrong, right = counts["clouded"], counts["right"]
    if reasons is None:
        reasons = [
            str(classified(f).reason or "not_proved")
            for f in findings
            if proof_group(f) == "not_compared"
        ]
    nothing = len(reasons)
    rounds = len({int(f.get("round") or 1) for f in findings}) or 1
    scope = f"{products_words(products).lower()}, {_plural(rounds, 'round', 'rounds')}"
    if coverage is not None:
        total = int(coverage.get("total") or 0)
        checked = total - int(coverage.get("not_opened") or 0)
        first = f"Checked {checked} of {total} pages ({scope})."
    else:
        pages = len({f.get("page") for f in findings})
        of = f" of {page_count}" if page_count else ""
        first = f"Checked items on {pages}{of} pages ({scope}); pages were not all recorded."
    lines = [
        first,
        # #1717: the screen's words (one meaning each): Wrong / Right / Not compared.
        f"{wrong} wrong (proved by code), {right} right, {nothing} not compared.",
    ]
    issues = _issue_words([f for f in findings if proof_group(f) == "clouded"])
    lines.append(
        f"Biggest issues: {'; '.join(issues)}."
        if issues
        else "No wrong value was proved by code so far."
    )
    unopened = int(coverage.get("not_opened") or 0) if coverage is not None else 0
    line = not_compared_line(Counter(reasons), unopened)
    lines.append(
        line.replace("See the Not-compared list.", "See the Not-compared list (PDF / Excel).")
    )
    stop = _stop_words(stopped, cap_hit, cap_usd, budget_usd, turns)
    if stop is not None:
        lines.append(stop)
    lines += [line for line in also if line]
    steps: list[str] = []
    drawing = any("cloud" in name.lower() for name in files)
    if wrong and drawing:
        steps.append("send the cloud drawing to the vendor")
    elif wrong:
        steps.append("send the wrong items to the vendor")
    if not steps:
        steps.append("look over the report before signing off")
    lines.append(f"Next: {'; '.join(steps)}.")
    return "\n".join(lines) + "\n\n" + CODE_SUMMARY_NOTE
