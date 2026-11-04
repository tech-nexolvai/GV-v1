"""Score a reader's sealed readings against the reading exam's confirmed key (rebuild plan Step 1A and the Step 2 gate).

The reading exam's key (`reading.py`) says what is printed in each sampled label, confirmed by a
person. A reader writes `stroke_reads.json` (`SCHEMA`): for each label it looked at, its box, the
turns it tried, every answer, and a state: `sealed_witnessed` (two readers of different makers
printed the same text and the drawing's scale agrees), `sealed_unwitnessed` (they agreed, with no
scale to check), `held` (a person must decide) or `unread`.

**The gate is agreed-wrong, and it must be 0.** A sealed reading whose value is not the confirmed
one is the failure Step 2 exists to prevent: two readers agreeing on a wrong number ("68" turned
over reads "89", and both would say so) becomes a value nobody questions. Holding or not reading a
label is safe; it only lowers the read rate.

**How a key row finds its label** (`match`): the same file and page, and boxes overlapping by an
intersection over union of at least `IOU_MIN`, each label used once, best overlap first. A key row
with no such label is `unmatched` and reported, never guessed.

**How a reading is judged** (`judge`): in exact inches where both the confirmed text and the
reading give a value (`inches_of`: `extraction/slot_reader/labels.py` over `units/`, the same
parse the seal uses; the reading's own `inches` is an exact fraction string such as `69/2`), and
by the text itself, after the seal's quote and space folding, where either does not (a bare `2`,
`22.0'`). A sealed value on a spot the key says prints no number (`reading.NOT_A_NUMBER`) is
agreed-wrong (a sealed text that gives no value there is `unverifiable`). A sealed reading of a label the person found unreadable cannot be checked: it is
counted as `unverifiable`, beside the gate, never inside it. Rows the person has not confirmed are
left out and counted.

Nothing here reads a drawing, a model or the network.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, Final

from eval.exam.reading import NOT_A_NUMBER, KeyRow
from extraction.slot_reader.labels import expand_label, plain_dimension
from extraction.slot_reader.seal import normalise_text
from units.measurement import Unit

__all__ = [
    "IOU_MIN",
    "SCHEMA",
    "SEALED",
    "STATES",
    "Judged",
    "Read",
    "Tally",
    "Truth",
    "file_key",
    "inches_of",
    "iou",
    "judge",
    "load_reads",
    "match",
    "score",
    "summary",
    "truth_of",
    "with_answers",
]

SCHEMA: Final = "gv-stroke-reads/1"
#: The states that are a reading: two readers of different makers printed the same text.
SEALED: Final = frozenset({"sealed_witnessed", "sealed_unwitnessed"})
STATES: Final = frozenset({*SEALED, "held", "unread"})
#: The least intersection over union for a key row and a label to be the same label.
IOU_MIN: Final = 0.5
#: A box with no width or no height (one straight stroke) is given this much (points) around its
#: middle on that side, so it can overlap anything at all.
_THIN_PT: Final = 0.5

type Box = tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class Truth:
    """What the key says a label prints. `kind`: `value` (a text, with its inches when it parses),
    `not_a_number`, `unreadable`, or `unconfirmed` (no person's answer yet)."""

    kind: str
    text: str = ""
    inches: Fraction | None = None


@dataclass(frozen=True, slots=True)
class Read:
    """One label of `stroke_reads.json`."""

    id: str
    file: str
    page: int
    box: Box
    state: str
    text: str | None
    inches: Fraction | None
    turn: int | None
    reasons: tuple[str, ...] = ()
    why: str | None = None


@dataclass(frozen=True, slots=True)
class Judged:
    """One key row's outcome: `right`, `agreed_wrong`, `unverifiable` (sealed, but the key cannot
    check it), `held`, `unread`, `unmatched` or `unconfirmed`."""

    row: KeyRow
    outcome: str
    read: Read | None = None
    truth: Truth | None = None


@dataclass
class Tally:
    """One reason's (or the whole exam's) counts."""

    rows: int = 0
    confirmed: int = 0
    matched: int = 0
    sealed: int = 0
    witnessed: int = 0
    right: int = 0
    agreed_wrong: int = 0
    unverifiable: int = 0
    held: int = 0
    unread: int = 0
    unmatched: int = 0
    unconfirmed: int = 0
    wrong_ids: list[str] = field(default_factory=list)

    @property
    def read_rate(self) -> float | None:
        """Of the confirmed rows matched to a label, the share sealed."""
        return self.sealed / self.matched if self.matched else None

    def add(self, judged: Judged) -> None:
        self.rows += 1
        if judged.outcome == "unconfirmed":
            self.unconfirmed += 1
            return
        self.confirmed += 1
        if judged.outcome == "unmatched":
            self.unmatched += 1
            return
        self.matched += 1
        read = judged.read
        if read is not None and read.state in SEALED:
            self.sealed += 1
            self.witnessed += int(read.state == "sealed_witnessed")
        if judged.outcome == "right":
            self.right += 1
        elif judged.outcome == "agreed_wrong":
            self.agreed_wrong += 1
            self.wrong_ids.append(judged.row.id)
        elif judged.outcome == "unverifiable":
            self.unverifiable += 1
        elif judged.outcome == "held":
            self.held += 1
        elif judged.outcome == "unread":
            self.unread += 1


def inches_of(text: str) -> Fraction | None:
    """The exact inches a printed text means, or `None` when it gives none (a bare number, words):
    a plain dimension, a corroborated mm [in] pair, a sum `a"+b"` or equal shares `N"(K EQ)`, by
    the seal's own parse."""
    folded = normalise_text(text)
    plain = plain_dimension(folded)
    if plain is not None and plain.unit is Unit.INCH:
        return plain.exact
    expanded = expand_label(folded)
    return expanded.value.exact if expanded is not None else None


def _fraction(value: object) -> Fraction | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Fraction(str(value).strip())
    except (ValueError, ZeroDivisionError):
        return None


def truth_of(row: KeyRow) -> Truth:
    """What a key row's `confirmed` column says is printed: `yes` takes the proposal (an empty or
    unreadable proposal then means unreadable); `unreadable` is unreadable; any other text is the
    corrected text; empty, or a bare `no`, is unconfirmed."""
    answer = row.confirmed.strip()
    lowered = answer.lower()
    if not answer or lowered == "no":
        return Truth("unconfirmed")
    if lowered == "unreadable":
        return Truth("unreadable")
    if lowered == "yes":
        if not row.proposed_text.strip() or row.confidence == "unreadable":
            return Truth("unreadable")
        text = row.proposed_text.strip()
    else:
        text = answer
    if text == NOT_A_NUMBER:
        return Truth("not_a_number", text)
    return Truth("value", text, inches_of(text))


def _value(read: Read) -> Fraction | None:
    if read.inches is not None:
        return read.inches
    return inches_of(read.text) if read.text else None


def judge(row: KeyRow, truth: Truth, read: Read | None) -> Judged:
    """One key row's outcome against the label matched to it (`None`: no label matched)."""
    if truth.kind == "unconfirmed":
        return Judged(row, "unconfirmed", read, truth)
    if read is None:
        return Judged(row, "unmatched", None, truth)
    if read.state not in SEALED:
        return Judged(row, "held" if read.state == "held" else "unread", read, truth)
    if truth.kind == "unreadable":
        return Judged(row, "unverifiable", read, truth)
    value = _value(read)
    if truth.kind == "not_a_number":
        return Judged(row, "agreed_wrong" if value is not None else "unverifiable", read, truth)
    if truth.inches is not None and value is not None:
        return Judged(row, "right" if value == truth.inches else "agreed_wrong", read, truth)
    same = read.text is not None and normalise_text(read.text) == normalise_text(truth.text)
    return Judged(row, "right" if same else "agreed_wrong", read, truth)


def iou(a: Sequence[float], b: Sequence[float]) -> float:
    """Intersection over union of two boxes (a side with no extent is given `_THIN_PT`)."""
    a, b = _solid(a), _solid(b)
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    if width <= 0 or height <= 0:
        return 0.0
    inter = width * height
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _solid(box: Sequence[float]) -> Box:
    x0, y0, x1, y1 = (float(v) for v in box)
    if x1 - x0 <= 0:
        x0, x1 = (x0 + x1) / 2 - _THIN_PT, (x0 + x1) / 2 + _THIN_PT
    if y1 - y0 <= 0:
        y0, y1 = (y0 + y1) / 2 - _THIN_PT, (y0 + y1) / 2 + _THIN_PT
    return x0, y0, x1, y1


def file_key(name: str) -> str:
    """A file's name as the matcher compares it: no folder, no `.pdf`, no `_drawings_only`, only
    letters and digits, lower case. The pack's copy and the upload's name compare equal."""
    stem = Path(name).name
    stem = re.sub(r"\.pdf$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"_drawings_only$", "", stem)
    return re.sub(r"[^0-9a-z]", "", stem.lower())


def load_reads(path: Path) -> list[Read]:
    """The labels of a `stroke_reads.json`. Raises `ValueError` for another schema or a state the
    schema does not name: a file this cannot read must never score as all-unread."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or data.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not {SCHEMA}")
    out: list[Read] = []
    for label in data.get("labels") or []:
        state = str(label.get("state"))
        if state not in STATES:
            raise ValueError(f"{path}: label {label.get('id')!r} has state {state!r}")
        box = label.get("box") or []
        if len(box) != 4:
            raise ValueError(f"{path}: label {label.get('id')!r} has no box")
        turn = label.get("turn")
        out.append(
            Read(
                id=str(label.get("id")),
                file=str(label.get("file")),
                page=int(label.get("page")),
                box=(float(box[0]), float(box[1]), float(box[2]), float(box[3])),
                state=state,
                text=None if label.get("text") is None else str(label.get("text")),
                inches=_fraction(label.get("inches")),
                turn=turn if isinstance(turn, int) and not isinstance(turn, bool) else None,
                reasons=tuple(str(r) for r in label.get("reasons") or []),
                why=label.get("why"),
            )
        )
    return out


def match(
    rows: Sequence[KeyRow],
    reads: Sequence[Read],
    *,
    files: Mapping[tuple[str, int], str] | None = None,
    minimum: float = IOU_MIN,
) -> dict[str, Read]:
    """Each key row's label (by row id): the same file (`file_key`) and page, intersection over
    union at least `minimum`, each label given to one row, the best overlaps first. A row's file
    is its `file` column, or `files[(job, file_index)]` when that is empty."""
    by_page: dict[tuple[str, int], list[int]] = {}
    for index, read in enumerate(reads):
        by_page.setdefault((file_key(read.file), read.page), []).append(index)
    pairs: list[tuple[float, int, int]] = []
    for position, row in enumerate(rows):
        name = row.file or (files or {}).get((row.job, row.file_index), "")
        if not name:
            continue
        for index in by_page.get((file_key(name), row.page), []):
            overlap = iou(row.box, reads[index].box)
            if overlap >= minimum:
                pairs.append((overlap, position, index))
    pairs.sort(key=lambda pair: (-pair[0], pair[1], pair[2]))
    taken_rows: set[int] = set()
    taken_reads: set[int] = set()
    out: dict[str, Read] = {}
    for _, position, index in pairs:
        if position in taken_rows or index in taken_reads:
            continue
        taken_rows.add(position)
        taken_reads.add(index)
        out[rows[position].id] = reads[index]
    return out


def with_answers(rows: Iterable[KeyRow], answers: Mapping[str, str]) -> list[KeyRow]:
    """The key with `confirmed` taken from `answers` (id to answer, as `index.html` copies them)
    wherever an answer is given."""
    return [replace(row, confirmed=answers[row.id]) if row.id in answers else row for row in rows]


def score(
    rows: Sequence[KeyRow],
    reads: Sequence[Read],
    *,
    files: Mapping[tuple[str, int], str] | None = None,
) -> tuple[dict[str, Tally], Tally, list[Judged], int]:
    """Every key row judged: a tally per reason (the key's `reason` column), one overall, every
    judgement, and how many labels of the reads no key row matched (labels outside the exam)."""
    found = match(rows, reads, files=files)
    per: dict[str, Tally] = {}
    overall = Tally()
    judged: list[Judged] = []
    for row in rows:
        outcome = judge(row, truth_of(row), found.get(row.id))
        judged.append(outcome)
        per.setdefault(row.reason, Tally()).add(outcome)
        overall.add(outcome)
    used = {id(read) for read in found.values()}
    return per, overall, judged, sum(1 for read in reads if id(read) not in used)


def summary(per: Mapping[str, Tally], overall: Tally) -> dict[str, Any]:
    """The tallies as plain data, for `--json`."""

    def plain(tally: Tally) -> dict[str, Any]:
        return {
            "rows": tally.rows,
            "confirmed": tally.confirmed,
            "matched": tally.matched,
            "sealed": tally.sealed,
            "witnessed": tally.witnessed,
            "read_rate": tally.read_rate,
            "right": tally.right,
            "agreed_wrong": tally.agreed_wrong,
            "unverifiable": tally.unverifiable,
            "held": tally.held,
            "unread": tally.unread,
            "unmatched": tally.unmatched,
            "unconfirmed": tally.unconfirmed,
        }

    return {"overall": plain(overall), "reasons": {k: plain(v) for k, v in sorted(per.items())}}
