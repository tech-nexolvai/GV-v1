"""Read a hand audit's markdown tables into rows a machine can score (harness rebuild, Phase 0).

The exam's source material is the hand audits written while a run was checked by eye: one markdown
file per helper, each with a verdict table. The helpers wrote them for people, so the columns are
not the same twice: some put the finding id in its own cell, some write "#236 F86"; some say
"TRUE-real", some "TR"; some add "(under-graded)" or ", should be red" after the verdict. Rather
than ask the helpers to rewrite history, this reads any of them: for each table row it looks for a
finding id in any cell, a known verdict word in any cell, and a page number in any cell.

The verdict words, normalised (`VERDICTS`):

- a flagged answer (red or please-check) is **TRUE** (a real problem, first time raised),
  **TRUE_MINOR** (real but trivial, or a repeat of a point already raised), **FALSE** (not a
  problem: nothing to raise) or **CANT_TELL**;
- a Not-compared answer is **NC_A** (rightly nothing to compare; the helpers also write "FAIR"),
  **NC_B** (really a difference: it should have been raised; "SHOULD-FLAG") or **NC_C** (really an
  agreement: it should have been Right; "COULD-COMPARE");
- a Right answer is **RIGHT_OK** ("CORRECT") or **RIGHT_BAD** ("INCORRECT").

Nothing here reads a drawing, a database or the network, and nothing here decides anything: it
turns prose into rows. What the rows mean for a score is `eval.exam.truth`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["VERDICTS", "AuditRow", "parse_audit", "rows_of_text"]

#: The verdict words the helpers used, upper-cased, to the exam's own names.
VERDICTS: Final[dict[str, str]] = {
    "TRUE": "TRUE",
    "TRUE-REAL": "TRUE",
    "TR": "TRUE",
    "TRUE-MINOR": "TRUE_MINOR",
    "TM": "TRUE_MINOR",
    "FALSE": "FALSE",
    "CAN'T TELL": "CANT_TELL",
    "CANT TELL": "CANT_TELL",
    "FAIR": "NC_A",
    "(A) RIGHTLY NOTHING": "NC_A",
    "(A)": "NC_A",
    "SHOULD-FLAG": "NC_B",
    "SF": "NC_B",
    "(B) REAL DIFFERENCE": "NC_B",
    "(B) REAL, AGAINST GV RULE": "NC_B",
    "(B)": "NC_B",
    "COULD-COMPARE": "NC_C",
    "CC": "NC_C",
    "(C) REALLY AGREES": "NC_C",
    "(C)": "NC_C",
    "CORRECT": "RIGHT_OK",
    "INCORRECT": "RIGHT_BAD",
}

#: A cell that is only a finding id, with or without the audit's own row number before it.
_FINDING_CELL: Final = re.compile(r"^#?\d*\s*(F\d+)$")
#: "#236 F86": the audit's row number and the finding id in one cell.
_ROW_AND_FINDING: Final = re.compile(r"^#\d+\s+(F\d+)$")
#: A cell that is only a page number, written "p23" or "23".
_PAGE_CELL: Final = re.compile(r"^p?(\d{1,3})$")
#: What a helper added after the verdict: "(under-graded)", ", should be red", "(really red)".
_VERDICT_TAIL: Final = re.compile(
    r"\s*\((?:[^()]*)\)\s*$|\s*,\s*(?:should be|really)\b.*$", re.IGNORECASE
)


@dataclass(frozen=True)
class AuditRow:
    """One audited answer: which finding, on which vendor page, and the helper's verdict."""

    finding_id: str
    page: int | None
    verdict: str
    audit: str
    line: int


def _cells(line: str) -> list[str] | None:
    """A markdown table row's cells, or None for anything that is not one (a rule row included)."""
    text = line.strip()
    if not text.startswith("|"):
        return None
    cells = [cell.strip() for cell in text.strip("|").split("|")]
    if all(set(cell) <= set("-: ") for cell in cells):
        return None
    return cells


def _plain(cell: str) -> str:
    """A cell without markdown emphasis and with its spaces squeezed."""
    return re.sub(r"\s+", " ", re.sub(r"[*`]", "", cell)).strip()


def _verdict(cell: str) -> str | None:
    """The verdict a cell holds, or None. A trailing aside is dropped before the lookup, so
    "TRUE-real (under-graded)" and "SHOULD-FLAG (really red)" read as their verdict."""
    word = _plain(cell).upper().rstrip(".")
    for candidate in (word, _VERDICT_TAIL.sub("", word).strip()):
        if candidate in VERDICTS:
            return VERDICTS[candidate]
    opening = re.match(r"^\((A|B|C)\)", word)
    return VERDICTS[f"({opening.group(1)})"] if opening else None


def _finding(cells: list[str]) -> tuple[str, int] | None:
    """The finding id a row names, with the cell it sat in, from a cell that holds only one."""
    for index, cell in enumerate(cells):
        match = _FINDING_CELL.match(_plain(cell)) or _ROW_AND_FINDING.match(_plain(cell))
        if match:
            return match.group(1), index
    return None


def _page(cells: list[str], after: int) -> int | None:
    """The vendor page a row names. A cell written "p23" counts wherever it sits; a bare number
    counts only *after* the finding id, because several audits open each row with their own row
    number, which would otherwise be read as the page."""
    for index, cell in enumerate(cells):
        text = _plain(cell)
        match = _PAGE_CELL.match(text)
        if not match or not 1 <= int(match.group(1)) <= 200:
            continue
        if text.startswith("p") or index > after:
            return int(match.group(1))
    return None


def rows_of_text(text: str, audit: str) -> list[AuditRow]:
    """Every audited answer in one audit's markdown, in the order written. A finding named twice
    (a summary table repeating the verdict table) is kept once, at its first verdict."""
    seen: dict[str, AuditRow] = {}
    for number, line in enumerate(text.splitlines(), 1):
        cells = _cells(line)
        if not cells:
            continue
        found = _finding(cells)
        verdict = next((hit for cell in cells if (hit := _verdict(cell))), None)
        if found is None or verdict is None or found[0] in seen:
            continue
        finding_id, at = found
        seen[finding_id] = AuditRow(finding_id, _page(cells, at), verdict, audit, number)
    return list(seen.values())


def parse_audit(path: str | Path, audit: str | None = None) -> list[AuditRow]:
    """`rows_of_text` for a file, named by its stem unless a name is given."""
    path = Path(path)
    return rows_of_text(path.read_text(encoding="utf-8"), audit or path.stem)
