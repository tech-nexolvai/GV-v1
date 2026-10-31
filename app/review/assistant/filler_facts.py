"""GV's filler arithmetic on a vendor page, for the page reviewer (2026-11-01), $0, no AI.

GV's reviewer rewrites a run's end when the vendor drew no box filler at a wall: "provide box
filler", the filler's size, and the cabinet next to it made smaller so the run still fits. The rule
was in the reviewer's rules file in words only; the numbers were never worked out, so on a
vendor-only set (no architect run for `run_fit` to compare with) the reviewer looked at the end and
said nothing. This module works the numbers out from the page's text layer, one line per run:

- **The run.** The page's dimension values lined up on one horizontal line (as `dimension_strings`
  reads them: inches with a mark or in brackets after millimetres, and code's own labels), cut where
  the drawn spacing jumps (two views side by side on one line: the gap between two values' centres
  is far more than their half-widths at the run's own scale). A run needs at least three parts, two
  of them cabinets (wider than `END_PART_MAX`), and something at an end: a narrow part, or a
  filler / scribe / end-panel / field-cut note beside the end.
- **Each end.** Its narrow parts from the wall inward (the innermost one, next to the first
  cabinet, is the filler; anything outside it is the scribe or field-cut allowance), the narrow
  values printed just beyond the string, the notes beside it, and the first cabinet.
- **GV's settled rules** (the client lead's email and worked sheets; decided 2026-10-20/21): a box
  filler of 1"-2" at a wall end (GV writes 2"), never a flat strip; the range is a GV **note**,
  never red (GV has accepted wider fillers); when a run is refitted, the fillers keep their size
  and the door cabinets share the difference equally (a drawer bank and equipment keep theirs); a
  share that is not a whole 1/16" is never rounded. An end is called out only with a filler, scribe
  or field-cut note beside it (evidence of a wall: "none where there is no wall"), and "no box
  filler" only when the strip itself is printed beyond the string too.
- **The line.** The run's parts and their sum, the overall printed with them, each end, and, for an
  end that breaks GV's rule, the widths GV's rule gives with a 2" box filler kept fixed: the
  difference shared equally by the cabinets (with the share, or why it gives no exact width), and
  the cabinet next to that end (what it would be if it took the whole difference, as GV's reviewer
  often writes it).

Every line is a please-check lead for the reviewer, never a finding: code cannot see the wall, the
door or the drawer bank, so the reviewer confirms in a zoom before reporting. Plain code on printed
text: no model call. Verification: `tests/review/assistant/test_filler_facts.py` (made-up rows).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from itertools import pairwise
from statistics import median
from typing import Any, Final

from app.review.assistant.ledger import format_inches

__all__ = [
    "END_PART_MAX",
    "FILLER_MAX",
    "FILLER_MIN",
    "GV_FILLER",
    "End",
    "Run",
    "blocks",
    "facts",
    "runs",
]

#: GV's filler range (the client lead's written rule: at least 1", 2" to use), a GV note
#: only (never red), and the box filler GV writes at a wall end.
FILLER_MIN: Final = Fraction(1)
FILLER_MAX: Final = Fraction(2)
GV_FILLER: Final = Fraction(2)
#: A part at most this wide at a run's end is an end piece (filler, gap, scribe, end panel); wider
#: is a cabinet (stock fillers are cut from 3" and 6" strips; over 6" is added to the cabinet).
END_PART_MAX: Final = Fraction(6)
#: A part at most this wide is a flat strip (a panel's thickness), not a box filler.
FLAT_MAX: Final = Fraction(3, 4)
#: Two values are on one line when their centres are this close (points; as `dimension_strings`).
ROW_PT: Final = 4.0
#: A run is cut where two values' centres are farther apart than this many times their half-widths
#: at the run's scale, plus `SPLIT_SLACK_PT` (a narrow part's label is often printed beside it).
SPLIT_FACTOR: Final = 1.5
SPLIT_SLACK_PT: Final = 20.0
#: A narrow value printed beyond a run's end: this close to the end (along) and the line (across).
BEYOND_ALONG_PT: Final = 60.0
BEYOND_ACROSS_PT: Final = 25.0
#: A note belongs to the nearest run end within this (along, from the note's sides; across).
NOTE_ALONG_PT: Final = 80.0
NOTE_ACROSS_PT: Final = 150.0
#: Lines of a note printed under each other: this close (points) and left edges this close.
NOTE_LINE_GAP_PT: Final = 3.0
NOTE_LEFT_PT: Final = 20.0
MAX_NOTE_WORDS: Final = 14
MAX_NOTE_CHARS: Final = 90
#: Runs listed in the first message, at most.
MAX_RUNS: Final = 12

#: Words of a note about a run's end, read with the spaces squashed out (a text layer often prints
#: "E N D P ANEL"): a filler, a scribe, a field cut, an end panel, a gap.
_FILLER: Final = re.compile(r"FILLER")
_SCRIBE: Final = re.compile(r"SCRIBE")
_FIELD_CUT: Final = re.compile(r"FIELDCUT|CUTONSITE|CUTASREQUIRED")
_PANEL: Final = re.compile(r"ENDPAN|SIDEPAN|FINISHEDEND|FIN\.END")
_NOTE: Final = re.compile(r"FILLER|SCRIBE|FIELDCUT|CUTONSITE|ENDPAN|SIDEPAN|FINISHEDEND|FIN\.END")
#: A note's later line has words (a dimension printed under a note is no part of it).
_WORDY: Final = re.compile(r"[A-Za-z]{2,}")
#: Words that print a run's wall-to-wall with its value.
_WALL_TO_WALL: Final = re.compile(r"W/W|WALL\s*TO\s*WALL|WALL-TO-WALL|\bWTW\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class _Value:
    text: str
    inches: Fraction
    box: tuple[float, float, float, float]

    @property
    def centre(self) -> float:
        return (self.box[0] + self.box[2]) / 2

    @property
    def middle(self) -> float:
        return (self.box[1] + self.box[3]) / 2


@dataclass
class End:
    """One end of a run, read from the wall inward."""

    side: str
    """left or right."""
    inside: list[Fraction] = field(default_factory=list)
    """The narrow parts of the string at this end, from the wall inward."""
    beyond: list[Fraction] = field(default_factory=list)
    """Narrow values printed just beyond the string at this end (outside its overall)."""
    notes: list[str] = field(default_factory=list)
    first_cabinet: Fraction | None = None

    @property
    def filler(self) -> Fraction | None:
        """The innermost narrow part (next to the first cabinet): the filler, when there is one."""
        return self.inside[-1] if self.inside else None

    def words(self) -> str:
        return " ".join(re.sub(r"\s+", "", note).upper() for note in self.notes)

    def at_a_wall(self) -> bool:
        """Evidence the end meets a wall: a filler, scribe or field-cut note beside it."""
        words = self.words()
        return bool(_FILLER.search(words) or _SCRIBE.search(words) or _FIELD_CUT.search(words))

    def panel(self) -> bool:
        return bool(_PANEL.search(self.words()))

    def breach(self) -> str | None:
        """Why this end breaks GV's filler rule, or `None`."""
        if not self.at_a_wall():
            return None
        filler = self.filler
        if filler is None:
            # Only with the strip itself printed beyond the string: a note alone may be another
            # view's (code places notes by distance, not by leader lines).
            return "no box filler inside the run at this end" if self.beyond else None
        if self.panel() and len(self.inside) == 1 and filler <= FLAT_MAX:
            return None  # an end panel, not a filler (B3 is said separately)
        if filler <= FLAT_MAX:
            return f"the filler is a flat {_in(filler)} strip, not a box filler"
        if not FILLER_MIN <= filler <= FILLER_MAX:
            return (
                f'the filler {_in(filler)} is outside GV\'s usual 1"-2" (a note only: GV has '
                "accepted wider fillers)"
            )
        return None


@dataclass
class Run:
    """One run: its parts left to right and what is printed with it."""

    parts: list[_Value]
    overall: Fraction | None = None
    wall_to_wall: Fraction | None = None
    ends: tuple[End, End] = (End("left"), End("right"))

    @property
    def total(self) -> Fraction:
        return sum((p.inches for p in self.parts), Fraction(0))

    @property
    def box(self) -> tuple[float, float, float, float]:
        return (
            min(p.box[0] for p in self.parts),
            min(p.box[1] for p in self.parts),
            max(p.box[2] for p in self.parts),
            max(p.box[3] for p in self.parts),
        )

    @property
    def middle(self) -> float:
        return median(p.middle for p in self.parts)

    def cabinets(self) -> list[Fraction]:
        return [p.inches for p in self.parts if p.inches > END_PART_MAX]


def _in(value: Fraction) -> str:
    return format_inches(value)


def _phrase_box(phrase: Mapping[str, Any]) -> tuple[float, float, float, float] | None:
    raw = phrase.get("box")
    if isinstance(raw, Mapping):
        try:
            raw = [raw["x0"], raw["y0"], raw["x1"], raw["y1"]]
        except KeyError:
            return None
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (x0, y0, x1, y1)


def _values(
    found: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]] = ()
) -> list[_Value]:
    """The page's dimension values lying flat (as `page_reviewer.dimension_strings` reads them)."""
    from app.review.assistant.finding_checks import to_inches
    from app.review.assistant.page_reviewer import bracketed_sizes

    out: list[_Value] = []
    for phrase in found:
        text, box = str(phrase.get("text") or ""), _phrase_box(phrase)
        if box is None or (box[3] - box[1]) > 1.5 * (box[2] - box[0]):
            continue  # a turned value: a height, not a run's width
        bracketed = bracketed_sizes(text)
        if bracketed:
            # Several values in one phrase (`[3/4][1/4]`): each takes its share of the box.
            step = (box[2] - box[0]) / len(bracketed)
            for n, value in enumerate(bracketed):
                part = (box[0] + n * step, box[1], box[0] + (n + 1) * step, box[3])
                out.append(_Value(f"[{_in(Fraction(value))}]", Fraction(value), part))
        elif re.search(r"[\"'″]", text):
            inches = to_inches(text)
            if inches is not None and inches > 0:
                out.append(_Value(text.strip(), Fraction(inches), box))
    for label in labels:
        if label.get("orientation") == "v":
            continue
        box = _phrase_box(label)
        try:
            size = Fraction(Decimal(str(label.get("inches"))))
        except (ArithmeticError, ValueError):
            continue
        if box is not None and size > 0:
            out.append(_Value(str(label.get("text") or _in(size)).strip(), size, box))
    return out


def _rows(values: Sequence[_Value]) -> list[list[_Value]]:
    used: set[int] = set()
    rows: list[list[_Value]] = []
    for i, value in enumerate(values):
        if i in used:
            continue
        members = [
            j
            for j, other in enumerate(values)
            if j not in used and abs(other.middle - value.middle) <= ROW_PT
        ]
        used.update(members)
        rows.append(sorted((values[j] for j in members), key=lambda v: v.box[0]))
    return rows


def _split(row: list[_Value]) -> list[list[_Value]]:
    """The row cut where the drawn spacing jumps (another view on the same line)."""
    ratios = [
        (b.centre - a.centre) / float((a.inches + b.inches) / 2)
        for a, b in pairwise(row)
        if a.inches > END_PART_MAX and b.inches > END_PART_MAX and b.centre > a.centre
    ]
    if not ratios:
        return [row]
    scale = median(ratios)
    out: list[list[_Value]] = [[row[0]]]
    for a, b in pairwise(row):
        expected = scale * float((a.inches + b.inches) / 2)
        if b.centre - a.centre > SPLIT_FACTOR * expected + SPLIT_SLACK_PT:
            out.append([b])
        else:
            out[-1].append(b)
    return out


def _note_blocks(found: Sequence[Mapping[str, Any]]) -> list[tuple[str, tuple[float, ...]]]:
    """The page's notes about a run's end, each note's lines joined (a note printed on 3 lines)."""
    lines: list[tuple[str, tuple[float, float, float, float]]] = []
    for phrase in found:
        box = _phrase_box(phrase)
        text = " ".join(str(phrase.get("text") or "").split())
        if box is not None and text and len(text.split()) <= MAX_NOTE_WORDS:
            lines.append((text, box))
    lines.sort(key=lambda item: (item[1][1], item[1][0]))
    taken: set[int] = set()
    out: list[tuple[str, tuple[float, ...]]] = []
    for i, (text, box) in enumerate(lines):
        if i in taken:
            continue
        block, (x0, y0, x1, y1) = [text], box
        taken.add(i)
        for j in range(i + 1, len(lines)):
            other, b = lines[j]
            if j in taken or b[1] - y1 > NOTE_LINE_GAP_PT:
                continue
            if b[1] >= y1 - 1 and abs(b[0] - x0) <= NOTE_LEFT_PT and _WORDY.search(other):
                block.append(other)
                taken.add(j)
                x0, x1, y1 = min(x0, b[0]), max(x1, b[2]), b[3]
        words = " ".join(block)
        if _NOTE.search(re.sub(r"\s+", "", words).upper()):
            out.append((words[:MAX_NOTE_CHARS], (x0, y0, x1, y1)))
    return out


def _beyond(run: Run, values: Sequence[_Value], side: str) -> list[_Value]:
    x0, _y0, x1, _y1 = run.box
    out = [
        v
        for v in values
        if v not in run.parts
        and v.inches <= END_PART_MAX
        and abs(v.middle - run.middle) <= BEYOND_ACROSS_PT
        and (
            (side == "left" and v.box[2] <= x0 and x0 - v.box[2] <= BEYOND_ALONG_PT)
            or (side == "right" and v.box[0] >= x1 and v.box[0] - x1 <= BEYOND_ALONG_PT)
        )
    ]
    return sorted(out, key=lambda v: v.box[0], reverse=side == "left")


def _overall(run: Run, values: Sequence[_Value], found: Sequence[Mapping[str, Any]]) -> None:
    """The overall printed with the run (a value equal to its parts' sum, on another line over the
    run), and a wall-to-wall printed with its words."""
    x0, _y0, x1, _y1 = run.box
    near = [
        v
        for v in values
        if v not in run.parts and x0 <= v.centre <= x1 and abs(v.middle - run.middle) <= 60
    ]
    if any(v.inches == run.total for v in near):
        run.overall = run.total
    for phrase in found:
        box = _phrase_box(phrase)
        text = str(phrase.get("text") or "")
        if box is None or not _WALL_TO_WALL.search(text):
            continue
        if abs((box[1] + box[3]) / 2 - run.middle) > 60 or not (x0 - 40 <= box[0] <= x1 + 40):
            continue
        sizes = [v for v in values if box[0] <= v.centre <= box[2] and box[1] <= v.middle <= box[3]]
        if sizes:
            run.wall_to_wall = max(v.inches for v in sizes)


def runs(found: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]] = ()) -> list[Run]:
    """Every run on the page with something at an end (see the module's notes)."""
    values = _values(found, labels)
    out: list[Run] = []
    for row in _rows(values):
        for piece in _split(row):
            if len(piece) < 3:
                continue  # a run is at least two cabinets and an end piece
            run = Run(piece, ends=(End("left"), End("right")))
            if len(run.cabinets()) < 2:
                continue
            left, right = run.ends
            for end, ordered in ((left, piece), (right, piece[::-1])):
                for part in ordered:
                    if part.inches > END_PART_MAX:
                        end.first_cabinet = part.inches
                        break
                    end.inside.append(part.inches)
                end.beyond = [v.inches for v in _beyond(run, values, end.side)]
            _overall(run, values, found)
            out.append(run)
    for note, box in _note_blocks(found):
        best: tuple[float, float, End] | None = None
        for run in out:
            x0, _y0, x1, _y1 = run.box
            across = abs((box[1] + box[3]) / 2 - run.middle)
            if across > NOTE_ACROSS_PT:
                continue
            for end, at in ((run.ends[0], x0), (run.ends[1], x1)):
                if box[0] - NOTE_ALONG_PT <= at <= box[2] + NOTE_ALONG_PT:
                    along = min(abs(at - box[0]), abs(at - box[2]))
                    if best is None or (across, along) < best[:2]:
                        best = (across, along, end)
        if best is not None:
            best[2].notes.append(note)
    return [run for run in out if any(end.inside or end.notes for end in run.ends)]


def _end_words(end: End) -> str:
    bits: list[str] = []
    if end.inside:
        parts = " + ".join(_in(v) for v in end.inside)
        bits.append(
            f"{parts} in the string (from the wall in)" if len(end.inside) > 1 else f"{parts}"
        )
    else:
        bits.append("no narrow part in the string")
    if end.beyond:
        bits.append(f"{' + '.join(_in(v) for v in end.beyond)} printed beyond the string")
    for note in end.notes[:3]:
        bits.append(f'note "{note}"')
    if end.first_cabinet is not None:
        bits.append(f"first cabinet {_in(end.first_cabinet)}")
    return f"{end.side} end: " + ", ".join(bits)


def _rule_widths(run: Run, broken: Sequence[End]) -> str:
    """The widths GV's settled site-fit rule gives with a 2" box filler fixed at each broken end."""
    change = sum(((end.filler or Fraction(0)) - GV_FILLER for end in broken), Fraction(0))
    cabinets = run.cabinets()
    sides = " and ".join(end.side for end in broken)
    lead = f"With GV's {_in(GV_FILLER)} box filler at the {sides} end"
    if change == 0:
        return f'{lead} the cabinets keep their widths (the difference is 0").'
    share = change / len(cabinets)
    words = (
        f"{lead}, the cabinets take {_in(change)} in all; GV's settled site-fit rule shares it "
        f"equally over the door cabinets ({len(cabinets)} here if all have doors; a drawer bank "
        "and equipment keep their size): "
    )
    if (share * 16).denominator == 1:
        new = sorted({c: c + share for c in cabinets}.items(), key=lambda item: -item[0])
        words += f"{_in(share)} each (" + ", ".join(f"{_in(a)} -> {_in(b)}" for a, b in new) + ")"
    else:
        words += (
            f'{_in(share)} each is not a whole 1/16": no exact width, GV decides (never rounded)'
        )
    nexts = [
        f"{end.side} {_in(end.first_cabinet)} (alone: {_in(end.first_cabinet + change)})"
        for end in broken
        if end.first_cabinet is not None and len(broken) == 1
    ]
    if nexts:
        words += f". The cabinet next to that end: {nexts[0]}"
    return words + "."


def line(run: Run) -> str:
    """One run as one fact line."""
    x0, _y0, x1, _y1 = run.box
    parts = " + ".join(_in(p.inches) for p in run.parts)
    head = f"Run at y {run.middle:.0f} (x {x0:.0f}-{x1:.0f}): {parts} = {_in(run.total)}"
    if run.overall is not None:
        head += " (the overall printed with it)"
    beyond = sum((sum(end.beyond, Fraction(0)) for end in run.ends), Fraction(0))
    if beyond:
        head += f"; with the pieces beyond its ends {_in(run.total + beyond)}"
    if run.wall_to_wall is not None:
        head += f"; wall-to-wall printed {_in(run.wall_to_wall)}"
    text = head + ". " + "; ".join(_end_words(end) for end in run.ends) + "."
    broken = [end for end in run.ends if end.breach()]
    for end in broken:
        text += f" Please check the {end.side} end: {end.breach()}."
    if broken and len(run.cabinets()) > 0:
        text += " " + _rule_widths(run, broken)
    for end in run.ends:
        if end.panel() and end.at_a_wall():
            text += (
                f" The {end.side} end has an end panel and a scribe/filler note: GV's rule is no "
                "finished end panel against a wall (check whether this end meets one)."
            )
    return text


def facts(
    found: Sequence[Mapping[str, Any]], labels: Sequence[Mapping[str, Any]] = ()
) -> list[str]:
    """The page's filler fact lines, at most `MAX_RUNS`."""
    return [line(run) for run in runs(found, labels)][:MAX_RUNS]


_LEAD: Final = (
    "Filler facts code worked out from this page's dimension rows (each run's parts, its ends from "
    'the wall inward, the first cabinet each side). GV\'s rules: a 1"-2" box filler at a wall end '
    '(GV writes 2"), never a flat scribe strip; the range is a GV note, never red; when a run is '
    "refitted the fillers keep their size and the door cabinets share the difference equally. "
    "Code cannot see the wall, doors or drawers: zoom on the end before you report, as a "
    "please-check (kind B for the filler type, kind A for the sizes), quoting the printed numbers:"
)


def blocks(review: Any) -> list[dict[str, Any]]:
    """The page reviewer's hook (`_Run.opening`): the filler facts as one text block (none when the
    page has no run with something at an end), logged as `filler_facts`."""
    from app.review.assistant.untrusted import fence

    lines = facts(getattr(review, "found", []) or [], getattr(review, "labels", []) or [])
    knowledge = getattr(review, "knowledge", None)
    if isinstance(knowledge, dict):
        knowledge["filler_facts"] = lines
    if not lines:
        return []
    return [{"type": "text", "text": _LEAD + "\n" + fence("\n".join(lines), "filler_facts")}]
