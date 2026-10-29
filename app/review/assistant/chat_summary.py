"""The full check's summary written by code, when the AI did not write its own (#1518).

A full check always ends with a short plain-English summary for the reviewer. The lead is told to
write it (`FULL_CHECK_INSTRUCTION`); when the message stops before it does (a cap, the turn limit,
a cancel, an error) or it writes nothing, code writes one from the findings, the coverage and the
trust report, with no AI call. Pure functions: no I/O, no money.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Final

from app.review.assistant.chat_findings import products_words
from app.review.assistant.chat_trust import cloud_kind
from app.review.assistant.classification import not_compared_line
from app.review.assistant.impact import by_impact
from app.review.assistant.likely_problems import COUNT_WORDS as LIKELY_COUNT
from app.review.assistant.proof import classified, proof_counts, proof_group, vendor_own
from app.review.assistant.review_action import review_action
from app.review.assistant.rule_notes import COUNT_WORDS as RULE_NOTE_COUNT
from app.review.assistant.vendor_text import vendor_text

#: The small note under a summary code wrote (the screen shows it as written).
CODE_SUMMARY_NOTE: Final = "*Summary written by GV Review: the AI did not finish its own.*"

#: How many issues the summary names.
TOP_ISSUES: Final = 5


def likely_line(count: int) -> str:
    """The summary's line on the whole-drawing look's Likely problems (2026-10-23)."""
    found = "1 likely problem" if count == 1 else f"{count} likely problems"
    return (
        f"{found} in the design (a missing or extra piece, another count, size or layout) "
        f"{'was' if count == 1 else 'were'} found by looking at whole drawings but not proved by "
        "code: please check and confirm (they are in the comment letter and the register as "
        '"Please check and confirm").'
    )


def rule_note_line(count: int) -> str:
    """The summary's line on the sizes the architect specifies that break a GV rule (#1839)."""
    sizes = "1 size" if count == 1 else f"{count} sizes"
    return (
        f"{sizes} the architect specifies {'breaks' if count == 1 else 'break'} a GV rule and the "
        "vendor prints the same: nothing is asked of the vendor; ask the architect (the questions "
        "are in the comment letter and the register)."
    )


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
    groups: dict[tuple[str, str, str, bool], list[int]] = {}
    for finding in top_issues(findings, limit=len(findings)):
        # The guard on internal words (#1622, `vendor_text`): no record id or "see GV Review's
        # record" in the summary either.
        key = (
            vendor_text(finding.get("item"), f"Item on page {finding.get('page')}") or "Item",
            vendor_text(finding.get("vendor"), "?") or "?",
            vendor_text(finding.get("architect"), "no stated value") or "no stated value",
            # A vendor string's "should be" is the sum of its own parts, never the architect's
            # (#1885): said as such.
            vendor_own(finding) == "sum",
        )
        groups.setdefault(key, []).append(int(finding.get("page") or 0))
        if len(groups) > TOP_ISSUES:
            break
    return [
        (
            f"{item}: vendor's parts add up to {other}, vendor prints {vendor} ({_pages(pages)})"
            if own
            else f"{item}: vendor {vendor}, architect {other} ({_pages(pages)})"
        )
        for (item, vendor, other, own), pages in list(groups.items())[:TOP_ISSUES]
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


def _own_only_pages(
    findings: Sequence[Mapping[str, Any]],
    compared: Collection[int] | None,
    checked: int,
    also: Collection[int] = (),
) -> int:
    """How many pages have a result code decided (Wrong or Right) but were not compared with
    the architect: the vendor's own checks only (#1885), and the `also` pages (checked within the
    vendor's own drawings, no architect drawing in the set). Without the compared pages, only when
    no page was compared (else they cannot be told apart)."""
    if compared is None and checked:
        return 0
    seen = set(compared or ())
    return len(
        {
            int(f["page"])
            for f in findings
            if isinstance(f.get("page"), int)
            and int(f["page"]) not in seen
            and proof_group(f) in ("clouded", "right")
        }
        | (set(also) - seen)
    )


#: The first line's reasons for the pages not compared, when the caller does not say them.
LEFT_DEFAULT: Final = "title or divider sheets, pages with no drawing to compare, or not reached"


def _left_words(
    findings: Sequence[Mapping[str, Any]],
    compared: Collection[int] | None,
    kinds: Mapping[int, str] | None,
    also: Collection[int] = (),
) -> str:
    """Why the pages not compared were not (`code_summary`'s `left_kinds`), each reason once in
    page order, leaving out the pages said apart as checked by code's own checks; the general
    words (`LEFT_DEFAULT`) when the caller gives none."""
    if not kinds:
        return LEFT_DEFAULT
    seen = set(compared or ())
    own = (
        set()
        if compared is None
        else {
            int(f["page"])
            for f in findings
            if isinstance(f.get("page"), int)
            and int(f["page"]) not in seen
            and proof_group(f) in ("clouded", "right")
        }
        | set(also)
    )
    words = list(dict.fromkeys(kinds[page] for page in sorted(kinds) if page not in own))
    return "; ".join(words) if words else LEFT_DEFAULT


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
    compared_pages: Collection[int] | None = None,
    left_kinds: Mapping[int, str] | None = None,
    own_pages: Collection[int] = (),
) -> str:
    """The 5-6 line summary: what was checked, the verdict, the biggest issues, the one line on
    what could not be compared (#1619), why it stopped (if it did) and the next step; then the
    small note. `also`: code's own lines to add before the next step (the vendor consistency
    check's, #1584). `reasons`: the Not-compared list's rows' reasons (its product scope), else
    worked out from `findings`. `compared_pages`: the pages the coverage counts as compared,
    so a page whose only results are code's checks of the vendor's own drawing (its strings,
    GV's rules) is said apart (#1885); without it, such pages are said only when no page was
    compared. `left_kinds`: each page not compared and why, in a few words ("no drawing to
    compare with", "not finished: out of money"): the first line then names only the reasons
    that are true (2026-10-25: a check stopped by its price said "title or divider sheets, pages
    with no drawing to compare, or not reached" though every page had its drawing). `own_pages`:
    the pages checked within the vendor's own drawings with no architect drawing in the set
    (2026-10-29): said apart, never "compared with the architect" whatever their coverage says."""
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
        # A page counts as checked only when it was compared: same or different (#1811). A page
        # with nothing to compare it with, one never reached, or one whose differences are still
        # open is said apart, never counted as checked.
        total = int(coverage.get("total") or 0)
        checked = int(coverage.get("same") or 0) + int(coverage.get("different") or 0)
        if own_pages and compared_pages is not None:
            compared_pages = [page for page in compared_pages if page not in set(own_pages)]
            checked = len(compared_pages)
        rest = max(0, total - checked)
        own = _own_only_pages(findings, compared_pages, checked, own_pages)
        why_left = _left_words(findings, compared_pages, left_kinds, own_pages)
        if own:
            # #1885: "Checked 0 of 109 pages ... 8 wrong" said two things at once; the pages with
            # only the vendor's own checks are counted apart from those compared.
            rest = max(0, rest - own)
            first = (
                f"Checked {checked + own} of {total} pages ({scope}): {checked} compared with "
                f"the architect, {own} only within the vendor's own drawings (no architect "
                "drawing for them: dimension strings, views, notes, GV's rules)"
                + (
                    f"; the other {rest} {'page was' if rest == 1 else 'pages were'} not "
                    f"compared ({why_left})."
                    if rest
                    else "."
                )
            )
        else:
            first = f"Checked {checked} of {total} pages ({scope})" + (
                f"; {rest} {'page was' if rest == 1 else 'pages were'} not compared "
                f"({why_left})."
                if rest
                else "."
            )
    else:
        pages = len({f.get("page") for f in findings})
        of = f" of {page_count}" if page_count else ""
        first = f"Checked items on {pages}{of} pages ({scope}); pages were not all recorded."
    noted = counts["rule_note"]
    likely = counts["likely_problem"]
    lines = [
        first,
        # #1717: the screen's words (one meaning each): Wrong / Right / Not compared; #1839: what
        # breaks a GV rule at the architect's size is decided, never "not compared".
        f"{wrong} wrong (proved by code), {right} right, "
        + (f"{noted} {RULE_NOTE_COUNT}, " if noted else "")
        + (f"{likely} {LIKELY_COUNT}, " if likely else "")
        + f"{nothing} not compared.",
    ]
    issues = _issue_words([f for f in findings if proof_group(f) == "clouded"])
    lines.append(
        f"Biggest issues: {'; '.join(issues)}."
        if issues
        else "No wrong value was proved by code so far."
    )
    if noted:
        lines.append(rule_note_line(noted))
    if likely:
        lines.append(likely_line(likely))
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
    action = review_action(findings)
    if action.action == "review_needed":
        # Anant 2026-10-22: never approved because the system could not look; a person decides.
        things = _plural(action.not_compared, "Not compared item", "Not compared items")
        steps.append(f"a person decides the {things} before this can be approved")
    elif wrong and drawing:
        steps.append("send the cloud drawing to the vendor")
    elif wrong:
        steps.append("send the wrong items to the vendor")
    if not steps:
        steps.append("look over the report before signing off")
    lines.append(f"Next: {'; '.join(steps)}.")
    return "\n".join(lines) + "\n\n" + CODE_SUMMARY_NOTE
