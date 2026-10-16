"""Code's half of the job values reader (#1407): exact inches, refusals, and the outcome per value.

The four values (cabinet depth, countertop overhang, backsplash thickness, cabinet side thickness)
are "Specified · G.C / Client · Project specific" (the client lead's checklist), so they may come
**only from the architect's side** (Q10, `rules.parameter_sources.CITABLE_SIDES`). The caller hands
this module architect panels only (`role` `arch`, or `reference` for a brand prototype); a vendor
panel never reaches it. GV's coloured markup is never a source.

**What the readers give, and what code does with it.** Each of the two readers says, per value and
per panel: printed or not, the text as printed, its unit, the label it saw, and a box (0-1000 of the
picture). Code then, for each reading:

* normalises the text to exact inches (`units.normalise.normalise_to_inches`, a `Fraction`), and
  **refuses** millimetres only, a bare number, or text that is not one dimension;
* **refuses** a box that is not a box on the panel, and a box on GV's coloured ink (`InkClass.GV`
  or `COVERED`, the W1 vendor-ink rule): the readers may have copied GV's number.

Then, per value on one panel:

* **auto** only when both readers give the same exact inches at overlapping boxes, the number is
  printed as real black text inside that box (not a stacked fraction, which a person confirms:
  decision D3, 2026-10-01), a label word for that value is printed in black next to it, the ink there
  is known to be the drawing's own, and the panel is this job's architect drawing (`arch`, never a
  brand prototype);
* **suggestion** when the readers disagree, only one read it, or any of those code checks short of
  a refusal fails: every reading is listed, a person picks;
* **refused** when every reading was refused (millimetres, GV's ink, off the panel), with why;
* **nothing** when neither reader found it printed.

Across panels (`combine`): a value two architect panels state differently is never auto; both are
shown as suggestions. **No value is ever worked out from others** (no countertop depth minus
overhang): only a number printed for that value is read, and code finds it at the spot.

**A $0 code finder** (`code_finds`) reads a note that states a value in words, with no model: a
number bound to `THICK`, `DEEP` or `OVERHANG` in a note whose words name the value (for example a
stacked `5/8"` then `THICK, ... SPLASH`). One judgment only, so it is only ever a suggestion.

Source: issue #1407. Verification: `tests/extraction/job_values/test_decide.py`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from fractions import Fraction
from typing import Final, Literal

from extraction.architect.text import Orientation, TextChar
from extraction.geometry.rows import Box
from extraction.ink import InkClass
from units.imperial import format_inches
from units.measurement import Unit
from units.normalise import normalise_to_inches

__all__ = [
    "SETTINGS",
    "CodeFind",
    "JobValueResult",
    "NumberToken",
    "Outcome",
    "Panel",
    "PanelDecision",
    "ReaderReading",
    "code_finds",
    "combine",
    "decide_panel",
    "form_suggestion",
    "numbers_in",
    "text_lines",
]

#: The four per-job values, by the settings' own names (`rules/parameter_sources.py`).
SETTINGS: Final = (
    "cabinet_depth",
    "countertop_overhang",
    "backsplash_thickness",
    "cabinet_side_thickness",
)

#: The words that name each value on a drawing. A label must be printed next to an auto value.
LABEL_WORDS: Final[Mapping[str, re.Pattern[str]]] = {
    "cabinet_depth": re.compile(r"\bCAB(?:INET)?\.?\s*DEPTH\b|\bCAB(?:INET)?\.?\s+DEEP\b"),
    "countertop_overhang": re.compile(r"OVERHANG|\bO\.\s?H\.?(?![A-Z])"),
    "backsplash_thickness": re.compile(r"SPLASH"),
    "cabinet_side_thickness": re.compile(r"\b(?:SIDE|END)\s*PANEL|\bFINISHED\s+(?:SIDE|END)\b"),
}

#: The word a code-only find binds its number to, per value (`code_finds`).
_BINDING: Final[Mapping[str, re.Pattern[str]]] = {
    "cabinet_depth": re.compile(r"\s*(?:DEEP|DEPTH)\b"),
    "countertop_overhang": re.compile(r"\s*(?:OVERHANG|O\.\s?H\.?)"),
    "backsplash_thickness": re.compile(r"\s*(?:THICK|THK)\b"),
    "cabinet_side_thickness": re.compile(r"\s*(?:THICK|THK)\b"),
}

#: One dimension as a drawing writes it: `5/8"`, `1 1/2"`, `1'-11 1/4"`, `3'`, or `19 mm`.
_DIMENSION: Final = re.compile(
    r"(?<![\w/.])(?:\d+'\s*-?\s*)?(?:\d+\s+)?\d+(?:/\d+)?\""
    r"|(?<![\w/.])\d+'(?:\s*-\s*\d+(?:\s+\d+/\d+)?\")?"
    r"|(?<![\w/.])\d+(?:\.\d+)?\s*MM\b",
    re.IGNORECASE,
)

#: Millimetres in a reading's text. `normalise_to_inches` converts them, and inches count (Q12).
_MILLIMETRES: Final = re.compile(r"\d\s*MM\b", re.IGNORECASE)

#: How far round a reading's box code looks for the number (points), and for its label.
NUMBER_PAD_PT: Final = Decimal(4)
LABEL_PAD_PT: Final = Decimal(36)


class Outcome(StrEnum):
    AUTO = "auto"
    SUGGESTION = "suggestion"
    REFUSED = "refused"
    NOTHING = "nothing"


Role = Literal["arch", "reference"]
Status = Literal["printed", "not_printed", "unsure", "no_answer"]


@dataclass(frozen=True, slots=True)
class Panel:
    """One architect drawing on a page, as code prepared it at no cost."""

    document_version_id: str
    page_index: int
    panel: int
    role: Role
    """`arch`: this job's architect drawing. `reference`: a brand prototype, suggestions only."""
    box: Box
    chars: tuple[TextChar, ...]
    """The panel's black and grey text (`open_page`): GV's coloured text is never here."""
    ink_of: Callable[[Box], InkClass | None]
    """Whose ink a box on the page holds; `None` when it could not be read."""


@dataclass(frozen=True, slots=True)
class ReaderReading:
    """One reader's answer for one value on one panel, as it said it."""

    reader: str
    status: Status
    printed: str = ""
    unit: str = ""
    label: str = ""
    box: tuple[int, int, int, int] | None = None
    """0-1000 across and down the panel's picture."""
    why: str = ""


@dataclass(frozen=True, slots=True)
class _Valid:
    reading: ReaderReading
    value: Fraction
    box: Box


@dataclass(frozen=True, slots=True)
class PanelDecision:
    setting: str
    panel: Panel
    outcome: Outcome
    value: Fraction | None
    """Set for auto and for a suggestion both readers agree on; `None` otherwise."""
    box: Box | None
    readings: tuple[ReaderReading, ...]
    values: tuple[tuple[str, Fraction], ...] = ()
    """Every reading code accepted as one inch value: (reader, value)."""
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CodeFind:
    setting: str
    panel: Panel
    value: Fraction
    text: str
    box: Box
    stacked: bool


@dataclass(frozen=True, slots=True)
class JobValueResult:
    """The reader's answer for one value of this job, over every panel it read."""

    setting: str
    outcome: Outcome
    value: Fraction | None = None
    page_index: int | None = None
    document_version_id: str | None = None
    box: Box | None = None
    readings: tuple[ReaderReading, ...] = ()
    choices: tuple[tuple[Fraction, int], ...] = ()
    """For a suggestion: each distinct value read, with the 0-based page it is on."""
    reasons: tuple[str, ...] = ()
    source: str = ""
    """`ai-pair` (both readers), `ai-reader` (one or disagreeing readers) or `code`."""
    held: tuple[str, ...] = field(default=())
    """Every refused or not-confirmed reading elsewhere, with why: nothing vanishes silently."""


# ---------------------------------------------------------------------------
# Text: lines and numbers, by code
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Item:
    text: str
    box: Box
    stacked: bool


@dataclass(frozen=True, slots=True)
class Line:
    text: str
    box: Box
    spans: tuple[tuple[int, int, Box, bool], ...]
    """Each item's place in `text`, its box, and whether it is a stacked fraction."""


@dataclass(frozen=True, slots=True)
class NumberToken:
    text: str
    value: Fraction | None
    unit: Unit | None
    box: Box
    stacked: bool
    line: int
    end: int
    """Where the number ends in its line's text (for the binding word after it)."""


def _h(box: Box) -> Decimal:
    return box.bottom - box.top


def _union(boxes: Sequence[Box]) -> Box:
    return Box(
        min(b.x0 for b in boxes),
        min(b.top for b in boxes),
        max(b.x1 for b in boxes),
        max(b.bottom for b in boxes),
    )


def _meets(a: Box, b: Box) -> bool:
    return a.x0 <= b.x1 and b.x0 <= a.x1 and a.top <= b.bottom and b.top <= a.bottom


def _grown(box: Box, by: Decimal) -> Box:
    return Box(box.x0 - by, box.top - by, box.x1 + by, box.bottom + by)


def _digit_runs(chars: Sequence[TextChar]) -> list[list[TextChar]]:
    digits = sorted((c for c in chars if c.text.isdigit()), key=lambda c: (c.box.top, c.box.x0))
    runs: list[list[TextChar]] = []
    for char in digits:
        for run in runs:
            last = run[-1]
            height = _h(last.box)
            if abs(char.box.top - last.box.top) <= height / 10 and Decimal(
                0
            ) <= char.box.x0 - last.box.x1 <= height * Decimal("0.3"):
                run.append(char)
                break
        else:
            runs.append([char])
    return runs


def _stacked(chars: Sequence[TextChar]) -> tuple[list[_Item], set[int]]:
    """Fractions printed one number over the other (`5` over `8`): one item `5/8` each, and the
    ids of the characters they used."""
    runs = _digit_runs(chars)
    items: list[_Item] = []
    used: set[int] = set()
    for top in runs:
        top_box = _union([c.box for c in top])
        height = _h(top_box)
        for bottom in runs:
            if bottom is top or any(id(c) in used for c in (*top, *bottom)):
                continue
            bottom_box = _union([c.box for c in bottom])
            across = min(top_box.x1, bottom_box.x1) - max(top_box.x0, bottom_box.x0)
            gap = bottom_box.top - top_box.bottom
            if (
                bottom_box.top >= top_box.top + height / 2
                and -height * Decimal("0.2") <= gap <= height * Decimal("0.4")
                and across >= min(top_box.x1 - top_box.x0, bottom_box.x1 - bottom_box.x0) / 2
                and abs(_h(bottom_box) - height) <= height / 5
            ):
                items.append(
                    _Item(
                        "".join(c.text for c in top) + "/" + "".join(c.text for c in bottom),
                        _union([top_box, bottom_box]),
                        True,
                    )
                )
                used.update(id(c) for c in (*top, *bottom))
                break
    return items, used


def text_lines(chars: Sequence[TextChar]) -> list[Line]:
    """Upright black text as lines, stacked fractions joined into the line beside them.

    Sideways text is left out: a number printed sideways is never confirmed by code here."""
    upright = [c for c in chars if c.orientation is Orientation.UPRIGHT and c.text.strip()]
    stacked, used = _stacked(upright)
    items = [_Item(c.text, c.box, False) for c in upright if id(c) not in used]
    rows: list[list[_Item]] = []
    for item in sorted(items, key=lambda i: (i.box.top + i.box.bottom)):
        centre = (item.box.top + item.box.bottom) / 2
        for row in rows:
            first = row[0].box
            if abs(centre - (first.top + first.bottom) / 2) <= _h(first) * Decimal("0.3") and abs(
                _h(item.box) - _h(first)
            ) <= _h(first) * Decimal("0.3"):
                row.append(item)
                break
        else:
            rows.append([item])
    for fraction in stacked:
        centre = (fraction.box.top + fraction.box.bottom) / 2
        home = min(
            (
                row
                for row in rows
                if row[0].box.top <= centre <= row[0].box.bottom
                and any(
                    Decimal(0) <= i.box.x0 - fraction.box.x1 <= _h(row[0].box)
                    or Decimal(0) <= fraction.box.x0 - i.box.x1 <= _h(row[0].box)
                    for i in row
                )
            ),
            key=lambda row: abs(centre - (row[0].box.top + row[0].box.bottom) / 2),
            default=None,
        )
        if home is None:
            rows.append([fraction])
        else:
            home.append(fraction)
    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda i: i.box.x0)
        text = ""
        spans: list[tuple[int, int, Box, bool]] = []
        height = max(_h(i.box) for i in row)
        previous: _Item | None = None
        for item in row:
            if previous is not None and item.box.x0 - previous.box.x1 > height / 5:
                text += " "
            start = len(text)
            text += item.text
            spans.append((start, len(text), item.box, item.stacked))
            previous = item
        lines.append(Line(text.upper(), _union([i.box for i in row]), tuple(spans)))
    lines.sort(key=lambda line: (line.box.top, line.box.x0))
    return lines


def numbers_in(lines: Sequence[Line]) -> list[NumberToken]:
    """Every dimension printed in `lines`, read exactly; one code cannot read has `value` None."""
    found: list[NumberToken] = []
    for index, line in enumerate(lines):
        for match in _DIMENSION.finditer(line.text):
            start, end = match.span()
            parts = [(box, stacked) for s, e, box, stacked in line.spans if s < end and start < e]
            if not parts:
                continue
            value: Fraction | None = None
            unit: Unit | None = None
            try:
                measured = normalise_to_inches(match.group(0))
                value = measured.exact
                unit = Unit.MM if _MILLIMETRES.search(match.group(0)) else measured.unit
            except Exception:  # noqa: BLE001 - unreadable: no value, never a guess
                value, unit = None, None
            found.append(
                NumberToken(
                    match.group(0),
                    value,
                    unit,
                    _union([box for box, _ in parts]),
                    any(stacked for _, stacked in parts),
                    index,
                    end,
                )
            )
    return found


# ---------------------------------------------------------------------------
# One panel, one value
# ---------------------------------------------------------------------------


def _box_on_panel(panel: Panel, box: tuple[int, int, int, int] | None) -> Box | None:
    if box is None or len(box) != 4:
        return None
    x0, y0, x1, y1 = box
    if not (0 <= x0 < x1 <= 1000 and 0 <= y0 < y1 <= 1000):
        return None
    width, height = panel.box.x1 - panel.box.x0, panel.box.bottom - panel.box.top
    return Box(
        panel.box.x0 + width * x0 / 1000,
        panel.box.top + height * y0 / 1000,
        panel.box.x0 + width * x1 / 1000,
        panel.box.top + height * y1 / 1000,
    )


def _check_reading(panel: Panel, reading: ReaderReading) -> _Valid | str | None:
    """The reading as exact inches at a place on the panel, why it is refused, or `None` when the
    reader found nothing printed."""
    if reading.status != "printed":
        return None
    if reading.unit.strip().lower() == "mm" or _MILLIMETRES.search(reading.printed):
        return f"{reading.reader} read millimetres only; inches count, so it is refused"
    try:
        measured = normalise_to_inches(reading.printed.strip())
    except Exception:  # noqa: BLE001 - refused with why, never guessed
        return f"{reading.reader}'s text is not one dimension in inches, so it is refused"
    if measured.unit is not Unit.INCH:
        return f"{reading.reader} read millimetres only; inches count, so it is refused"
    if measured.exact <= 0:
        return f"{reading.reader}'s value is not a size, so it is refused"
    box = _box_on_panel(panel, reading.box)
    if box is None:
        return f"{reading.reader}'s place is not a box on this drawing, so it is refused"
    ink = panel.ink_of(box)
    if ink is InkClass.GV or ink is InkClass.COVERED:
        return (
            f"{reading.reader} read it where GV's coloured markup is; GV's markup is never a "
            "source, so it is refused"
        )
    return _Valid(reading, measured.exact, box)


def decide_panel(
    panel: Panel, setting: str, first: ReaderReading, second: ReaderReading
) -> PanelDecision:
    """Both readers' answers for one value on one panel, decided by code (see the module)."""
    readings = (first, second)
    valid: list[_Valid] = []
    refusals: list[str] = []
    for reading in readings:
        checked = _check_reading(panel, reading)
        if isinstance(checked, _Valid):
            valid.append(checked)
        elif isinstance(checked, str):
            refusals.append(checked)
    values = tuple((v.reading.reader, v.value) for v in valid)
    if not valid:
        if refusals:
            return PanelDecision(
                setting, panel, Outcome.REFUSED, None, None, readings, (), tuple(refusals)
            )
        return PanelDecision(
            setting,
            panel,
            Outcome.NOTHING,
            None,
            None,
            readings,
            (),
            ("neither reader found it printed on this drawing",),
        )
    if len(valid) == 1:
        return PanelDecision(
            setting,
            panel,
            Outcome.SUGGESTION,
            None,
            valid[0].box,
            readings,
            values,
            (*refusals, "only one reader read it, so a person picks"),
        )
    a, b = valid
    if a.value != b.value:
        return PanelDecision(
            setting,
            panel,
            Outcome.SUGGESTION,
            None,
            _union([a.box, b.box]),
            readings,
            values,
            ("the two readers read different values, so a person picks",),
        )
    reasons: list[str] = []
    if not _meets(a.box, b.box):
        reasons.append("the two readers point at different places")
    spot = _union([a.box, b.box])
    lines = text_lines([c for c in panel.chars if _meets(c.box, _grown(spot, NUMBER_PAD_PT))])
    here = [n for n in numbers_in(lines) if n.value == a.value and n.unit is Unit.INCH]
    if not here:
        reasons.append("code did not find that number printed as text at that place")
    elif all(n.stacked for n in here):
        reasons.append("it is printed as a stacked fraction, which a person confirms")
    near = text_lines([c for c in panel.chars if _meets(c.box, _grown(spot, LABEL_PAD_PT))])
    if not any(LABEL_WORDS[setting].search(line.text) for line in near):
        reasons.append("no label naming this value is printed next to it")
    if panel.ink_of(spot) is None:
        reasons.append("whose ink is at that place could not be read")
    if panel.role != "arch":
        reasons.append("it is on a brand prototype, not this job's own architect drawing")
    if reasons:
        return PanelDecision(
            setting, panel, Outcome.SUGGESTION, a.value, spot, readings, values, tuple(reasons)
        )
    return PanelDecision(
        setting,
        panel,
        Outcome.AUTO,
        a.value,
        spot,
        readings,
        values,
        ("both readers read it, and code found it printed there with its label",),
    )


# ---------------------------------------------------------------------------
# The $0 code finder
# ---------------------------------------------------------------------------


def code_finds(panel: Panel) -> list[CodeFind]:
    """Each value a note on this panel states in words, read by code alone (one per value at most).

    A note's lines are its line and the next line just below it (a note wraps). The number is the
    one directly before the binding word (`THICK`, `DEEP`, `OVERHANG`): `4" HIGH` is never a
    thickness. Two different numbers for one value on the panel: none is found."""
    lines = text_lines(panel.chars)
    numbers = numbers_in(lines)
    found: dict[str, set[tuple[Fraction, str, bool]]] = {}
    boxes: dict[tuple[str, Fraction], Box] = {}
    for number in numbers:
        if number.value is None or number.unit is not Unit.INCH:
            continue
        line = lines[number.line]
        window = [line.text]
        for other in lines:
            gap = other.box.top - line.box.bottom
            if (
                other is not line
                and Decimal(0) <= gap <= _h(line.box)
                and other.box.x0 <= line.box.x1
                and line.box.x0 <= other.box.x1
            ):
                window.append(other.text)
        words = " ".join(window)
        after = line.text[number.end :]
        named = [s for s in SETTINGS if LABEL_WORDS[s].search(words) and _BINDING[s].match(after)]
        if len(named) != 1:
            continue
        (setting,) = named
        found.setdefault(setting, set()).add((number.value, number.text, number.stacked))
        boxes[(setting, number.value)] = number.box
    out: list[CodeFind] = []
    for setting, values in found.items():
        if len({value for value, _, _ in values}) != 1:
            continue
        value, text, stacked = min(values, key=lambda v: v[1])
        out.append(CodeFind(setting, panel, value, text, boxes[(setting, value)], stacked))
    return out


# ---------------------------------------------------------------------------
# Every panel, one value
# ---------------------------------------------------------------------------


def _where(decision: PanelDecision) -> str:
    return f"p.{decision.panel.page_index + 1}"


def combine(
    setting: str, decisions: Sequence[PanelDecision], finds: Sequence[CodeFind]
) -> JobValueResult:
    """One value's answer for the job, from every panel read and every code find."""
    autos = [d for d in decisions if d.outcome is Outcome.AUTO]
    suggestions = [d for d in decisions if d.outcome is Outcome.SUGGESTION]
    held = tuple(
        f"{_where(d)}: {'; '.join(d.reasons)}"
        for d in decisions
        if d.outcome in (Outcome.REFUSED, Outcome.SUGGESTION)
    )
    stated: dict[Fraction, int] = {}
    for d in [*autos, *suggestions]:
        for _reader, value in d.values:
            stated.setdefault(value, d.panel.page_index)
    for find in finds:
        stated.setdefault(find.value, find.panel.page_index)
    choices = tuple(sorted(stated.items(), key=lambda item: (item[1], item[0])))
    if autos and len(stated) == 1:
        best = autos[0]
        return JobValueResult(
            setting,
            Outcome.AUTO,
            best.value,
            best.panel.page_index,
            best.panel.document_version_id,
            best.box,
            best.readings,
            choices,
            best.reasons,
            "ai-pair",
            held,
        )
    if stated:
        if autos:
            reasons: tuple[str, ...] = (
                "architect drawings state different values for it, so a person picks",
            )
            first = autos[0]
        elif suggestions:
            first = suggestions[0]
            reasons = first.reasons
        else:
            find = finds[0]
            return JobValueResult(
                setting,
                Outcome.SUGGESTION,
                find.value,
                find.panel.page_index,
                find.panel.document_version_id,
                find.box,
                (ReaderReading("code", "printed", find.text),),
                choices,
                (
                    "code found it in a note's words, with no AI; one judgment only, so a "
                    "person confirms it" + ("; it is a stacked fraction" if find.stacked else ""),
                ),
                "code",
                held,
            )
        agreed = first.value if len(stated) == 1 else None
        return JobValueResult(
            setting,
            Outcome.SUGGESTION,
            agreed,
            first.panel.page_index,
            first.panel.document_version_id,
            first.box,
            tuple(r for d in [*autos, *suggestions] for r in d.readings),
            choices,
            reasons,
            "ai-pair" if agreed is not None and first.value is not None else "ai-reader",
            held,
        )
    refused = [d for d in decisions if d.outcome is Outcome.REFUSED]
    if refused:
        return JobValueResult(
            setting,
            Outcome.REFUSED,
            readings=tuple(r for d in refused for r in d.readings),
            reasons=tuple(reason for d in refused for reason in d.reasons),
            held=held,
        )
    return JobValueResult(
        setting,
        Outcome.NOTHING,
        reasons=(
            (
                "not printed on any architect drawing read"
                if decisions
                else "no architect drawing was read for it"
            ),
        ),
        held=held,
    )


def form_suggestion(result: JobValueResult) -> dict[str, object] | None:
    """What the #1403 "Enter this job's values" form shows beside the value's field, or `None`.

    `value` is in the form's own format (inches with the inch mark, `ProjectValueIn.value`), so the
    person's click saves exactly what was shown. `filled` is True only for auto: the form shows
    "Filled from p.N · Change", and the value is saved as AI-filled with both readers and the page.
    A suggestion with several choices names each; nothing is saved until a person clicks."""
    if result.outcome not in (Outcome.AUTO, Outcome.SUGGESTION) or result.page_index is None:
        return None
    return {
        "name": result.setting,
        "value": None if result.value is None else f'{format_inches(result.value)}"',
        "filled": result.outcome is Outcome.AUTO,
        "page": result.page_index + 1,
        "document_version_id": result.document_version_id,
        "box": (
            None
            if result.box is None
            else [
                str(result.box.x0),
                str(result.box.top),
                str(result.box.x1),
                str(result.box.bottom),
            ]
        ),
        "source": result.source,
        "choices": [
            {"value": f'{format_inches(value)}"', "page": page + 1}
            for value, page in result.choices
        ],
        "readers": [
            {"reader": r.reader, "printed": r.printed, "status": r.status} for r in result.readings
        ],
        "note": "; ".join(result.reasons),
    }
