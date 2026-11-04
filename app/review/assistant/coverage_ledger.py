"""The coverage ledger: every item crossed with every question it should be asked, and what came back.

Finding in this product is done by an AI, so what is never noticed is never found. Today the AI is
pointed at a page and asked, in effect, what looks wrong. The hand audits of four runs show what that
costs: three quarters of everything missed is a question nobody asked, and a sixth of it is a
question the same run demonstrably asked on another sheet and never asked again here. Two runs of one
job shared only a third of their answers.

A ledger turns that from "whatever the AI happened to notice" into a grid you can see holes in. For
one job it lays out every item against every question that item's kind is asked (`question_list`),
and records for each cell whether an answer came back, what produced it, and how firm it is. After a
run it can say: how many cells were filled, how many were not and why, and which items were never
identified at all.

**What a cell holds.** A cell is one item and one question. It is answered when at least one finding
on that item's pages matches the question. The answer's firmness is taken from the finding: `proved`
when code settled it, `flagged` when it is a please-check, `noted` for a rule note, and
`not_compared` when the question was reached but nothing could be weighed. An unanswered cell carries
why: the item was never identified, nothing at all came back on its pages, or nobody asked.

**Measuring saved runs.** `items_from_run` reads a saved run and makes one item per vendor page the
run covered, guessing each page's kind from the findings on it. A page with no findings has no kind,
which is exactly the honest answer: that item was never identified. Those items are still charged the
questions asked about every item, so an unidentified page shows as a hole rather than vanishing from
the grid. Matching a free-text finding to a question is done with the question's own `seen_as` hints,
and it is deliberately generous: a finding that might answer a question counts as answering it, so
the coverage this module reports is the best case for today's approach, not the worst.

Reading one page as one item is an approximation, used because today's runs carry no item assembly.
The module itself does not care: hand it real multi-sheet items and it builds the same grid.

This module reads and reports; it writes nothing, calls no AI and costs nothing. It is not wired into
the live roster: that is a separate step for the owner to approve.

Run it over saved runs with:

    python -m app.review.assistant.coverage_ledger <run.json> [...] [--per-question] [--never-asked]

Verification: tests/review/assistant/test_coverage_ledger.py (made-up findings, no client data).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.review.assistant.question_list import Question, questions_for
from app.review.assistant.question_list import questions as all_questions

__all__ = [
    "CONFIDENCE",
    "GAPS",
    "SLOT_STATUSES",
    "Cell",
    "Item",
    "Ledger",
    "SlotRow",
    "answers_to",
    "grid",
    "items_from_run",
    "kind_of_page",
    "ledger_for",
    "lines",
    "main",
    "never_asked",
    "slot_summary",
    "with_slot_answers",
]

#: How firm an answer is, strongest first. A cell takes the strongest answer it has.
CONFIDENCE: Final[tuple[str, ...]] = ("proved", "flagged", "noted", "not_compared", "none")

#: Why a cell has no answer.
GAPS: Final[Mapping[str, str]] = {
    "unidentified": "the item was never identified, so nothing could be asked about it",
    "silent": "nothing at all came back on this item's pages",
    "not_asked": "the item was identified and answered, but nobody asked this question",
    "not_shown": "the question was asked as a slot and the drawings do not show it",
    "": "",
}

#: What a slot answer reports for one item and one question (`SlotRow`): answered (marks named
#: and decided by code), not shown (asked; the drawings do not show it, with a fixed reason), or not
#: asked (the slot was due and no answer came back for it).
SLOT_STATUSES: Final[tuple[str, ...]] = ("answered", "not_shown", "not_asked")
#: A slot answer's outcome (`slot_maths.SlotOutcome`) mapped to how firm the cell's answer is.
_SLOT_CONFIDENCE: Final[Mapping[str, str]] = {
    "differs": "proved",
    "same": "proved",
    "not_compared": "not_compared",
}

#: A finding's group, as `stuck_report.group_of` names it, mapped to how firm its answer is.
_GROUP_CONFIDENCE: Final[Mapping[str, str]] = {
    "wrong": "proved",
    "right": "proved",
    "likely_problem": "flagged",
    "rule_note": "noted",
    "dismissed": "noted",
    "not_compared": "not_compared",
}

#: Words that suggest a page carries an item of each kind, used only to measure saved runs.
_KIND_WORDS: Final[Mapping[str, tuple[str, ...]]] = {
    "countertop_piece": ("countertop", "counter top", "quartz", "stone top", "top plan", "slab"),
    "sink_cutout_sheet": ("cut-out", "cutout", "cut out", "sink cut", "faucet hole", "bowl"),
    "base_cabinet_run": ("base cabinet", "base run", "base plan", "toe kick", "drawer base"),
    "upper_cabinet_run": ("upper", "wall cabinet", "open shelf", "microwave bay", "wall plan"),
    "vanity": ("vanity",),
    "filler": ("filler", "scribe"),
    "splash": ("backsplash", "back splash", "side splash", "splash"),
    "accessory_detail": ("hook rail", "floating shelf", "shelf detail", "rod", "bracket", "leg"),
    "appliance_opening": ("refrigerator", "fridge", "range", "dishwasher", "appliance"),
    "key_plan": ("key plan", "room type", "matrix", "room schedule", "room list"),
    "cover_sheet": ("cover", "checklist", "material board", "general notes", "spec sheet"),
}

#: A finding's product category, where it names an item kind outright.
_CATEGORY_KIND: Final[Mapping[str, str]] = {
    "countertop": "countertop_piece",
    "cabinet": "base_cabinet_run",
}

#: Fields of a finding whose words say what the finding is about.
_TEXT_FIELDS: Final[tuple[str, ...]] = ("item", "why", "question", "vendor", "architect")


@dataclass(frozen=True, slots=True)
class Item:
    """One thing GV reviews: its kind, the pages it is drawn on, and a name for a person."""

    key: str
    kind: str | None
    label: str
    pages: tuple[int, ...] = ()

    @property
    def identified(self) -> bool:
        """Was this item's kind ever settled?"""
        return self.kind is not None


@dataclass(frozen=True, slots=True)
class Cell:
    """One item crossed with one question, and what came back."""

    item: str
    question: str
    findings: tuple[str, ...] = ()
    answered_by: tuple[str, ...] = ()
    confidence: str = "none"
    gap: str = ""

    @property
    def answered(self) -> bool:
        """Did anything answer this question about this item?"""
        return bool(self.findings)


@dataclass(frozen=True, slots=True)
class Ledger:
    """One job's grid: its items, its cells, and the questions nobody asked."""

    job: str
    items: tuple[Item, ...]
    cells: tuple[Cell, ...]
    findings: int = 0
    matched: int = 0
    unmatched: tuple[str, ...] = field(default=())

    @property
    def answered(self) -> int:
        """How many cells came back with an answer."""
        return sum(1 for cell in self.cells if cell.answered)

    @property
    def coverage(self) -> float:
        """The share of the grid that today's run actually filled, 0 to 1."""
        if not self.cells:
            return 0.0
        return self.answered / len(self.cells)

    @property
    def unidentified(self) -> tuple[Item, ...]:
        """The items whose kind was never settled: nothing could be asked about them."""
        return tuple(item for item in self.items if not item.identified)

    def gaps(self) -> dict[str, int]:
        """How many unanswered cells there are of each reason, worst first."""
        counted: dict[str, int] = {}
        for cell in self.cells:
            if not cell.answered:
                counted[cell.gap] = counted.get(cell.gap, 0) + 1
        return dict(sorted(counted.items(), key=lambda pair: -pair[1]))

    def by_confidence(self) -> dict[str, int]:
        """How many cells hold an answer of each firmness."""
        counted = {name: 0 for name in CONFIDENCE}
        for cell in self.cells:
            counted[cell.confidence] += 1
        return counted

    def for_question(self, key: str) -> tuple[Cell, ...]:
        """Every cell of one question, across the job's items."""
        return tuple(cell for cell in self.cells if cell.question == key)

    def summary(self) -> dict[str, Any]:
        """The whole ledger as plain numbers, for a report or a test."""
        return {
            "job": self.job,
            "items": len(self.items),
            "identified": len(self.items) - len(self.unidentified),
            "unidentified": len(self.unidentified),
            "cells": len(self.cells),
            "answered": self.answered,
            "coverage": round(self.coverage, 4),
            "findings": self.findings,
            "matched": self.matched,
            "gaps": self.gaps(),
            "confidence": self.by_confidence(),
            "never_asked": list(never_asked(self)),
        }


def _text_of(finding: Mapping[str, Any]) -> str:
    """Every word of a finding that says what it is about, lowercased."""
    parts: list[str] = []
    for name in _TEXT_FIELDS:
        value = finding.get(name)
        if isinstance(value, str):
            parts.append(value)
    card = finding.get("card")
    if isinstance(card, Mapping):
        asked = card.get("question")
        if isinstance(asked, Mapping):
            for name in ("key", "words"):
                value = asked.get(name)
                if isinstance(value, str):
                    parts.append(value)
    helps = finding.get("decision_help")
    if isinstance(helps, Mapping):
        value = helps.get("measures")
        if isinstance(value, str):
            parts.append(value)
    proof = finding.get("proof")
    if isinstance(proof, Mapping):
        value = proof.get("reason")
        if isinstance(value, str):
            parts.append(value)
    return " ".join(parts).lower()


def _rule_ids(finding: Mapping[str, Any]) -> set[str]:
    """The rulebook ids a finding names, upper-cased."""
    found: set[str] = set()
    value = finding.get("rule_check_id")
    if isinstance(value, str):
        found.add(value.upper())
    for name in ("gv_standard", "formula", "claim_rule"):
        block = finding.get(name)
        if isinstance(block, Mapping):
            for key in ("id", "rule", "rule_id"):
                inner = block.get(key)
                if isinstance(inner, str):
                    found.add(inner.upper())
    return found


def _made_by(finding: Mapping[str, Any]) -> str:
    """Who made the finding, with any numbering stripped (`helper:63` becomes `helper`)."""
    value = finding.get("created_by")
    if not isinstance(value, str):
        return ""
    return value.split(":", 1)[0].strip().lower()


def answers_to(
    finding: Mapping[str, Any], asked: Sequence[Question] | None = None
) -> tuple[str, ...]:
    """The keys of the questions this finding plausibly answers.

    Generous on purpose. A finding that names a question's rulebook id answers it outright;
    otherwise one of the question's own words must appear in the finding's text, and, where the
    question says which code or lens produces its answers, the finding must come from one of them.
    Being generous means the coverage reported is the best case for today's open-ended approach.
    """
    questions = tuple(asked) if asked is not None else all_questions()
    text = _text_of(finding)
    ids = _rule_ids(finding)
    maker = _made_by(finding)
    hit: list[str] = []
    for question in questions:
        seen = question.seen_as
        rules = {str(one).upper() for one in seen.get("rule_ids", ()) or ()}
        if rules & ids:
            hit.append(question.key)
            continue
        words = tuple(str(one).lower() for one in seen.get("words", ()) or ())
        if not words or not any(word in text for word in words):
            continue
        makers = tuple(str(one).lower() for one in seen.get("created_by", ()) or ())
        if makers and maker and maker not in makers:
            continue
        hit.append(question.key)
    return tuple(hit)


def kind_of_page(findings: Sequence[Mapping[str, Any]]) -> str | None:
    """The kind of item a page carries, guessed from the findings on it, or None when it has none.

    Used only to measure saved runs, which carry no item assembly. A page with no findings has no
    kind: that page's item was never identified, which is the honest answer and the one the ledger
    reports.
    """
    if not findings:
        return None
    text = " ".join(_text_of(one) for one in findings)
    scored: dict[str, int] = {}
    for finding in findings:
        category = finding.get("category")
        if isinstance(category, str):
            named = _CATEGORY_KIND.get(category.strip().lower())
            if named:
                scored[named] = scored.get(named, 0) + 1
    for kind, words in _KIND_WORDS.items():
        score = sum(text.count(word) for word in words)
        if score:
            scored[kind] = scored.get(kind, 0) + score
    if not scored:
        return None
    best = max(scored.values())
    for kind in _KIND_WORDS:
        if scored.get(kind, 0) == best:
            return kind
    return None


def items_from_run(findings: Sequence[Mapping[str, Any]], pages: Sequence[int]) -> tuple[Item, ...]:
    """One item per vendor page the run covered, with its kind guessed from the page's findings."""
    per_page: dict[int, list[Mapping[str, Any]]] = {page: [] for page in pages}
    for finding in findings:
        page = finding.get("page")
        if isinstance(page, int):
            per_page.setdefault(page, []).append(finding)
    items: list[Item] = []
    for page in sorted(per_page):
        on_page = per_page[page]
        kind = kind_of_page(on_page)
        label = f"page {page}" if kind is None else f"page {page} ({kind})"
        items.append(Item(key=f"p{page}", kind=kind, label=label, pages=(page,)))
    return tuple(items)


def grid(items: Sequence[Item]) -> tuple[Cell, ...]:
    """Every cell of the grid, empty: each item crossed with the questions its kind is asked."""
    cells: list[Cell] = []
    for item in items:
        for question in questions_for(item.kind):
            cells.append(Cell(item=item.key, question=question.key))
    return tuple(cells)


def _best(first: str, second: str) -> str:
    """The firmer of two confidences."""
    return first if CONFIDENCE.index(first) <= CONFIDENCE.index(second) else second


def ledger_for(
    job: str,
    items: Sequence[Item],
    findings: Sequence[Mapping[str, Any]],
    *,
    group_of: Any = None,
) -> Ledger:
    """One job's filled ledger: the grid, with every finding matched onto the cells it answers.

    `group_of` names a finding's group (`stuck_report.group_of` by default); it decides how firm each
    answer is. Pass your own to test without importing the report.
    """
    if group_of is None:
        from app.review.assistant.stuck_report import group_of as grouped

        group_of = grouped
    asked = all_questions()
    by_page: dict[int, list[Mapping[str, Any]]] = {}
    for finding in findings:
        page = finding.get("page")
        if isinstance(page, int):
            by_page.setdefault(page, []).append(finding)

    filled: dict[tuple[str, str], Cell] = {}
    for cell in grid(items):
        filled[(cell.item, cell.question)] = cell

    matched: set[str] = set()
    for item in items:
        on_item = [one for page in item.pages for one in by_page.get(page, [])]
        for finding in on_item:
            confidence = _GROUP_CONFIDENCE.get(str(group_of(finding)), "noted")
            maker = str(finding.get("created_by") or "ai")
            for key in answers_to(finding, asked):
                empty = filled.get((item.key, key))
                if empty is None:
                    continue
                matched.add(str(finding.get("id")))
                filled[(item.key, key)] = Cell(
                    item=empty.item,
                    question=empty.question,
                    findings=empty.findings + (str(finding.get("id")),),
                    answered_by=empty.answered_by
                    + ((maker,) if maker not in empty.answered_by else ()),
                    confidence=_best(empty.confidence, confidence),
                )

    silent = {item.key for item in items if not any(by_page.get(page) for page in item.pages)}
    unknown = {item.key for item in items if not item.identified}
    done: list[Cell] = []
    for cell in filled.values():
        if cell.answered:
            done.append(cell)
            continue
        if cell.item in unknown:
            gap = "unidentified"
        elif cell.item in silent:
            gap = "silent"
        else:
            gap = "not_asked"
        done.append(Cell(item=cell.item, question=cell.question, confidence="none", gap=gap))
    unmatched = tuple(
        sorted(str(one.get("id")) for one in findings if str(one.get("id")) not in matched)
    )
    return Ledger(
        job=job,
        items=tuple(items),
        cells=tuple(done),
        findings=len(findings),
        matched=len(matched),
        unmatched=unmatched,
    )


@dataclass(frozen=True, slots=True)
class SlotRow:
    """One slot asked about one item (Step 4): what came back, from the slot path's own record."""

    item: str
    question: str
    status: str
    reason: str = ""
    outcome: str = ""
    """`same`, `differs` or `not_compared` when answered."""
    red_barred: bool = False
    finding: str = ""
    """The finding's id when the answer was recorded."""


def with_slot_answers(ledger: Ledger, rows: Sequence[SlotRow]) -> Ledger:
    """The ledger with the slot path's answers laid on its cells.

    An answered slot fills its cell (made by `slots`): a decided answer is `proved`, one whose red
    code barred is `flagged`, one code could not compare is `not_compared`. A slot asked and not
    shown leaves the cell unanswered with the gap `not_shown`. A row for an item or a question the
    grid does not hold is left out: the grid is the question list's, never the slot path's."""
    cells = {(cell.item, cell.question): cell for cell in ledger.cells}
    for row in rows:
        if row.status not in SLOT_STATUSES:
            raise ValueError(f"slot status {row.status!r} is not one of {SLOT_STATUSES}")
        cell = cells.get((row.item, row.question))
        if cell is None:
            continue
        if row.status == "answered":
            firm = _SLOT_CONFIDENCE.get(row.outcome, "not_compared")
            if firm == "proved" and row.red_barred:
                firm = "flagged"
            cells[(row.item, row.question)] = Cell(
                item=cell.item,
                question=cell.question,
                findings=cell.findings + (row.finding or f"slot:{row.question}",),
                answered_by=cell.answered_by
                + (("slots",) if "slots" not in cell.answered_by else ()),
                confidence=_best(cell.confidence, firm),
            )
        elif row.status == "not_shown" and not cell.answered:
            cells[(row.item, row.question)] = Cell(
                item=cell.item, question=cell.question, confidence="none", gap="not_shown"
            )
    return Ledger(
        job=ledger.job,
        items=ledger.items,
        cells=tuple(cells.values()),
        findings=ledger.findings,
        matched=ledger.matched,
        unmatched=ledger.unmatched,
    )


def slot_summary(rows: Sequence[SlotRow]) -> dict[str, int]:
    """How many slot rows are answered, not shown and not asked."""
    counted = {status: 0 for status in SLOT_STATUSES}
    for row in rows:
        counted[row.status] = counted.get(row.status, 0) + 1
    return counted


def never_asked(ledger: Ledger) -> tuple[str, ...]:
    """The questions that no item in this job got an answer to, in the list's own order."""
    asked: set[str] = {cell.question for cell in ledger.cells if cell.answered}
    applicable: set[str] = {cell.question for cell in ledger.cells}
    return tuple(
        one.key for one in all_questions() if one.key in applicable and one.key not in asked
    )


def lines(ledger: Ledger, *, per_question: bool = False, show_never: bool = False) -> list[str]:
    """The ledger written out for a person."""
    summary = ledger.summary()
    out = [
        f"{ledger.job}",
        (
            f"  items {summary['items']} "
            f"({summary['identified']} identified, {summary['unidentified']} never identified)"
        ),
        (
            f"  cells {summary['cells']}, answered {summary['answered']} "
            f"-> coverage {summary['coverage'] * 100:.1f}%"
        ),
        (
            f"  findings {summary['findings']}, of which {summary['matched']} answer a question "
            f"on the list"
        ),
    ]
    firm = ", ".join(
        f"{name} {count}"
        for name, count in summary["confidence"].items()
        if count and name != "none"
    )
    if firm:
        out.append(f"  answers by firmness: {firm}")
    for gap, count in summary["gaps"].items():
        out.append(f"  {count} cells unanswered: {GAPS.get(gap, gap)}")
    if per_question:
        out.append("  per question (answered / asked about):")
        for question in all_questions():
            cells = ledger.for_question(question.key)
            if not cells:
                continue
            done = sum(1 for cell in cells if cell.answered)
            out.append(f"    {question.key:38s} {done:4d} / {len(cells):4d}  [{question.compares}]")
    if show_never:
        missing = never_asked(ledger)
        out.append(f"  never asked at all ({len(missing)}):")
        out.extend(f"    {key}" for key in missing)
    return out


def _findings_of(path: str) -> tuple[list[dict[str, Any]], list[int]]:
    """A saved run's findings and the vendor pages it covered."""
    from app.review.assistant import stuck_report

    with open(path, encoding="utf-8") as handle:
        loaded = json.load(handle)
    blocks, _ = stuck_report._blocks_of(loaded)
    found, _ = stuck_report.answers(blocks)
    pages: list[int] = []
    for block in blocks:
        if block.get("type") != "coverage":
            continue
        coverage = block.get("coverage")
        if not isinstance(coverage, Mapping):
            continue
        for entry in coverage.get("pages") or ():
            page = entry.get("page") if isinstance(entry, Mapping) else None
            if isinstance(page, int) and page not in pages:
                pages.append(page)
    for one in found:
        page = one.get("page")
        if isinstance(page, int) and page not in pages:
            pages.append(page)
    return found, sorted(pages)


def main(argv: Sequence[str] | None = None) -> int:
    """Show the coverage of one or more saved runs."""
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("runs", nargs="+", help="saved run JSON files")
    parser.add_argument("--per-question", action="store_true", help="a line per question")
    parser.add_argument(
        "--never-asked", action="store_true", help="list the questions nobody asked"
    )
    parser.add_argument("--json", action="store_true", help="print the summary as JSON")
    args = parser.parse_args(list(argv) if argv is not None else None)
    summaries: list[dict[str, Any]] = []
    for path in args.runs:
        found, pages = _findings_of(path)
        made = ledger_for(path, items_from_run(found, pages), found)
        if args.json:
            summaries.append(made.summary())
        else:
            print(
                "\n".join(lines(made, per_question=args.per_question, show_never=args.never_asked))
            )
            print()
    if args.json:
        print(json.dumps(summaries, indent=1))
    return 0


if __name__ == "__main__":  # pragma: no cover - a thin command line
    raise SystemExit(main(sys.argv[1:]))
