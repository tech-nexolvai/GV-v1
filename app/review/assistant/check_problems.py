"""What went wrong in a full check, said in plain words at its end ("How this was checked").

The owner should never have to read logs to learn that a check met a problem. At the end of every
roster full check, code lists the problems the run met, each in one everyday sentence with no
internal names (no step names, no drawing ids, no model words):

- what the pre-flight check found that touches many pages, a stop the reviewer ran past, and the
  supervisor's warning (`plan_supervisor`);
- pages that could not be read;
- vendor pages with no architect drawing, grouped by why (`full_check_plan.unpaired_words`);
- pages the matcher did not answer in time (Continue asks again);
- checks by code that did not finish;
- pages not compared and possible mistakes not proved, and why (the money ran out, the check was
  stopped, or not reached);
- the money running out.

It only reports: it never changes a finding, a page or the verdict. The list is kept in the trust
block (`problems`) and shown first under "How this was checked"; empty when nothing went wrong.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine

__all__ = ["attach", "page_words", "problems"]

_log = logging.getLogger(__name__)

#: Pre-flight lines worth repeating at the end (the rest are plain facts).
_PREFLIGHT_KEEP: Final = frozenset(
    {"unreadable", "pages_broken", "duplicates", "project_missing", "only_plans", "no_sizes"}
)
#: The code steps' names in everyday words (`chat_engine.CODE_CHECK_NAMES` and its other steps).
_STEP_WORDS: Final[Mapping[str, str]] = {
    "the reading ledger's check": "adding up the vendor's numbers",
    "the vendor consistency check": "comparing the vendor's own pages with each other",
    "the finish check": "checking the finish tags",
    "code's own pairs": "matching sizes by their place on the drawings",
    "GV's site-fit rule": "GV's site-fit rule",
    "GV's filler range": "GV's filler range",
    "GV's sink rules": "GV's sink rules (offsets, cut-out, countertop depth)",
    "the measuring by scale where the architect prints no size": (
        "measuring by scale where the architect prints no size"
    ),
    "GV Review's recorded results": "GV Review's own earlier checks",
    "the drawings' printed numbers (the labels step did not finish)": (
        "reading the printed numbers"
    ),
}


def _count(number: int, one: str, many: str | None = None) -> str:
    return f"{number} {one if number == 1 else (many or one + 's')}"


def page_words(pages: Sequence[int], most: int = 8) -> str:
    """ "page 4", "pages 3, 5 and 9", "pages 3, 5, 6 … and 12 more"."""
    ordered = sorted(set(pages))
    if len(ordered) == 1:
        return f"page {ordered[0]}"
    shown = ordered[:most]
    more = len(ordered) - len(shown)
    if more:
        return f"pages {', '.join(str(p) for p in shown)} and {more} more"
    return f"pages {', '.join(str(p) for p in shown[:-1])} and {shown[-1]}"


def _preflight_lines(gate: Any) -> list[str]:
    if gate is None:
        return []
    out: list[str] = []
    report = getattr(gate, "report", None)
    for line in getattr(report, "lines", ()):
        if line.level == "stop" and getattr(gate, "run_anyway", False):
            out.append(f"Run anyway was chosen although: {line.words}")
        elif line.level == "warn" or line.id in _PREFLIGHT_KEEP:
            out.append(line.words)
    look = getattr(gate, "supervisor", None)
    if look is not None and look.verdict in ("warn", "stop") and look.why:
        out.append(f"A second look before the check said: {look.why}")
    return out


def _unpaired_lines(unpaired: Mapping[int, str], retry: Mapping[int, str]) -> list[str]:
    from app.review.assistant.full_check_plan import unpaired_words

    groups: dict[str, list[int]] = {}
    for page, reason in unpaired.items():
        groups.setdefault(unpaired_words(reason), []).append(page)
    out = [
        f"{_count(len(pages), 'vendor page')} not compared with the architect ({why}): "
        f"{page_words(pages)}."
        for why, pages in sorted(groups.items(), key=lambda item: -len(item[1]))
    ]
    if retry:
        out.append(
            f"{_count(len(retry), 'vendor page')} could not be matched to the architect's "
            f"drawings in time: {page_words(list(retry))}. Continue tries again."
        )
    return out


def problems(
    *,
    gate: Any = None,
    unpaired: Mapping[int, str] | None = None,
    retry: Mapping[int, str] | None = None,
    failed_steps: Sequence[str] = (),
    pages_left: Sequence[int] = (),
    unproved: int = 0,
    why_left: str = "",
    out_of_money: bool = False,
    price: str | None = None,
) -> list[str]:
    """The run's problems, one plain sentence each, the most important first."""
    out: list[str] = []
    if out_of_money:
        out.append(
            f"The fixed price{f' (${price})' if price else ''} ran out before the check finished."
        )
    if pages_left:
        because = {
            "out of money": "the money ran out",
            "the check was stopped before the end": "the check was stopped",
        }.get(why_left, "they could not be compared in this run")
        out.append(
            f"{_count(len(pages_left), 'page')} not compared ({because}): "
            f"{page_words(pages_left)}. Continue compares them."
        )
    if unproved:
        out.append(
            f"{_count(unproved, 'possible mistake')} not proved yet, so not marked red. "
            "Continue proves them."
        )
    for step in dict.fromkeys(failed_steps):
        out.append(
            f"One check by code did not finish ({_STEP_WORDS.get(step, 'a check by code')}): "
            "its results may be missing, so this check is not for sending."
        )
    out.extend(_unpaired_lines(unpaired or {}, retry or {}))
    out.extend(_preflight_lines(gate))
    return list(dict.fromkeys(out))


def attach(engine: ChatEngine, found: Sequence[str]) -> None:
    """Puts the problems in the answer's trust block (shown under "How this was checked")."""
    index = engine._trust_index
    if index is None:
        return
    with engine._lock:
        block = engine.blocks[index]
        if block.get("type") != "trust":
            return
        trust = {**block["trust"], "problems": list(found)}
        engine.blocks[index] = {"type": "trust", "trust": trust}
    engine.emit("trust", {"index": index, "trust": json.loads(json.dumps(trust))})


def for_roster(run: Any, stopped: str | None) -> list[str]:
    """The problems of a roster full check (`full_check_roster._Run`) as it ends."""
    engine = run.engine
    try:
        pages, items, why = run._unfinished(stopped)
    except Exception:  # noqa: BLE001 - listed without the pages left
        _log.exception("check problems: pages left not worked out")
        pages, items, why = [], [], ""
    plan = getattr(run, "plan", None)
    price = None
    if plan is not None:
        price = f"{plan.price / 1_000_000:.2f}"
    return problems(
        gate=getattr(run, "preflight", None),
        unpaired=dict(getattr(run, "unpaired", {}) or {}),
        retry=dict(getattr(run, "retry", {}) or {}),
        failed_steps=list(getattr(engine, "code_checks_failed", []) or []),
        pages_left=pages,
        unproved=len(items),
        why_left=why,
        out_of_money=stopped == "cost_cap" or bool(getattr(run, "stopped_for_money", False)),
        price=price,
    )
