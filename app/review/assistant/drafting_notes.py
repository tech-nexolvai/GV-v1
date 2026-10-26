"""Drafting notes next to a reference size: HOLD, V.I.F., +/- and the half-inch convention (final
plan #9). Code reads them, $0, no AI.

Drafting practice marks which numbers are hard and which are soft (AIA best practice; casework
specs ask for field measurement before fabrication):

- **HOLD** next to a size: a controlling size that must be kept. It is a hard requirement: nothing
  below softens a difference against it, and its please-check line says so.
- **V.I.F.** (verify in field), **field verify** or **+/-** next to a size: the number is to be
  measured on site. A difference against it is never red and never a Likely problem: it is Not
  compared, "the drawing says verify in field", with a plain field-verify note.
- **The half-inch convention**: an architect dimensions to the wall's face (or a stud's), rounded
  to the nearest 1/2", while the cabinet maker builds to finished faces with far finer precision.
  So a difference of 1/2" or less, where code proved the architect's dimension ends at a wall's
  face (`dim_ends`: a wall drawn beyond it), is "convention difference, field verify", not a
  Likely problem.

**Only where code reads the note next to the dimension**: the label reader's own flags for the
words written with the value (`dim_labels`: `VIF 24"`, `24" +/-`), the label's own text, or a
printed phrase on the reference's page that holds the label's text and the note with at most two
other words (made-up: "12'-6" (3.81 m) +/- OVERALL"), or a phrase of the note alone touching the
label on its line. A general note elsewhere on the sheet ("all sizes to be verified in field") is
never read as one size's note. No guessing: the note is read on the reference's own label only.

The finding's record (`FIELD`) keeps the reference label it was read for, so a later step that
pairs the item with another label is never judged by a stale note (`softened`, `hold`).

Pure code: plain values in, plain values out. Verification:
`tests/review/assistant/test_drafting_notes.py` (made-up labels and phrases).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any, Final, Literal

__all__ = [
    "CONVENTION_MAX_IN",
    "FIELD",
    "Kind",
    "hold",
    "mark",
    "note_words",
    "read_note",
    "softened",
]

#: The finding's record of the drafting note on its reference size.
FIELD: Final = "drafting_note"
#: The largest difference the half-inch convention explains (inches).
CONVENTION_MAX_IN: Final = Fraction(1, 2)

type Kind = Literal["hold", "field_verify", "approx", "convention"]
type Box = tuple[float, float, float, float]

_HOLD: Final = re.compile(r"(?<![A-Z])HOLD(?![A-Z])", re.IGNORECASE)
_VIF: Final = re.compile(
    r"(?<![A-Z])V\.?\s?I\.?\s?F\.?(?![A-Z])|\bverify\s+in\s+field\b|\bfield\s+verify\b",
    re.IGNORECASE,
)
_APPROX: Final = re.compile(r"\+\s?/\s?-|±|\+-")
#: Bracketed numbers and units beside a value ("(3.81 m)", "[610mm]"): the value's own.
_BRACKETED_NUMBER: Final = re.compile(r"[\(\[][^\)\]]*\d[^\)\]]*[\)\]]")
_WORD: Final = re.compile(r"[A-Za-z]{2,}")
#: Words a size's own phrase may carry beside the note ("+/- OVERALL", "VIF CLR").
_MAX_OTHER_WORDS: Final = 2

_KIND_WORDS: Final[Mapping[str, str]] = {
    "hold": "HOLD (a size that must be kept)",
    "field_verify": "V.I.F. (verify in field)",
    "approx": "+/- (approximate, to be measured on site)",
}


def _flat(value: object) -> str:
    return " ".join(str(value or "").split())


def _box(raw: object) -> Box | None:
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _kinds_in(text: str) -> list[Kind]:
    found: list[Kind] = []
    if _HOLD.search(text):
        found.append("hold")
    if _VIF.search(text):
        found.append("field_verify")
    if _APPROX.search(text):
        found.append("approx")
    return found


def _note_only(text: str) -> bool:
    """A phrase that is a note and nothing else ("HOLD", "(V.I.F.)", "+/-", "FIELD VERIFY")."""
    rest = _HOLD.sub(" ", text)
    rest = _VIF.sub(" ", rest)
    rest = _APPROX.sub(" ", rest)
    return bool(_kinds_in(text)) and not re.search(r"[A-Za-z0-9]", rest)


def _beside(label: Box, phrase: Box, upright: bool) -> bool:
    """The phrase touches the label on its line (a character's height apart at most), or sits
    right under or over it."""
    if upright:
        label = (label[1], label[0], label[3], label[2])
        phrase = (phrase[1], phrase[0], phrase[3], phrase[2])
    height = max(label[3] - label[1], 1.0)
    across = min(label[3], phrase[3]) - max(label[1], phrase[1])
    along_gap = max(phrase[0] - label[2], label[0] - phrase[2], 0.0)
    if across >= 0.5 * min(height, max(phrase[3] - phrase[1], 1.0)) and along_gap <= height:
        return True
    along = min(label[2], phrase[2]) - max(label[0], phrase[0])
    across_gap = max(phrase[1] - label[3], label[1] - phrase[3], 0.0)
    narrow = max(min(label[2] - label[0], phrase[2] - phrase[0]), 1.0)
    return along >= 0.5 * narrow and across_gap <= 0.6 * height


def _overlaps(one: Box, two: Box) -> bool:
    return min(one[2], two[2]) > max(one[0], two[0]) and min(one[3], two[3]) > max(one[1], two[1])


def _own_phrase(label_text: str, phrase_text: str) -> list[Kind]:
    """The notes in a phrase that holds the label's own text, when the phrase is that size's own
    line: the note and at most `_MAX_OTHER_WORDS` other words."""
    if not label_text or label_text not in phrase_text:
        return []
    rest = phrase_text.replace(label_text, " ", 1)
    kinds = _kinds_in(rest)
    if not kinds:
        return []
    rest = _BRACKETED_NUMBER.sub(" ", rest)
    rest = _HOLD.sub(" ", rest)
    rest = _VIF.sub(" ", rest)
    rest = _APPROX.sub(" ", rest)
    if len(_WORD.findall(rest)) > _MAX_OTHER_WORDS:
        return []  # a sentence, not a size's note
    return kinds


def read_note(label: Mapping[str, Any], phrases: Sequence[Mapping[str, Any]]) -> list[Kind]:
    """The drafting notes code reads next to one printed size (see the module), most telling first
    (HOLD, then V.I.F., then +/-); empty when it reads none."""
    found: list[Kind] = []
    for flag in label.get("flags") or ():
        kind: Kind | None = (
            "field_verify" if flag == "vif" else "approx" if flag == "approx" else None
        )
        if kind is not None:
            found.append(kind)
    text = _flat(label.get("text"))
    found.extend(_kinds_in(text))
    box = _box(label.get("box"))
    if box is not None:
        upright = str(label.get("orientation") or "h") == "v"
        for phrase in phrases:
            spot = _box(phrase.get("box"))
            words = _flat(phrase.get("text"))
            if spot is None or not words:
                continue
            if _overlaps(box, spot) or _beside(box, spot, upright):
                if _note_only(words) and _beside(box, spot, upright):
                    found.extend(_kinds_in(words))
                else:
                    found.extend(_own_phrase(text, words))
    order: tuple[Kind, ...] = ("hold", "field_verify", "approx")
    return [kind for kind in order if kind in found]


def note_words(kinds: Sequence[str]) -> str:
    """The notes in plain words: "V.I.F. (verify in field)"."""
    return " and ".join(_KIND_WORDS[k] for k in kinds if k in _KIND_WORDS)


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _key(side: object) -> str | None:
    if not isinstance(side, Mapping) or not side.get("id"):
        return None
    return f"{side.get('file') or ''}|{side.get('page')}|{side.get('id')}"


def _current(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The record, while the item still pairs the reference label it was read for."""
    held = finding.get(FIELD)
    if not isinstance(held, Mapping):
        return None
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else {}
    if _key((dims or {}).get("architect")) != held.get("label"):
        return None
    return held


def hold(finding: Mapping[str, Any]) -> bool:
    """The reference marks the size HOLD: a hard requirement, never softened."""
    held = _current(finding)
    return held is not None and "hold" in (held.get("kinds") or ())


def softened(finding: Mapping[str, Any]) -> str | None:
    """Why a difference against the reference's size is never red and never a Likely problem (the
    reference marks it V.I.F. or +/-, or it is within the half-inch convention to a wall's face),
    in plain words; `None` otherwise, and always for a HOLD size."""
    held = _current(finding)
    if held is None or "hold" in (held.get("kinds") or ()):
        return None
    if finding.get("status") not in ("different", "check"):
        return None
    why = str(held.get("why") or "")
    return why or None


def _why(kinds: Sequence[str], finding: Mapping[str, Any], difference: Decimal | None) -> str:
    architect = _flat(finding.get("architect"))
    if "field_verify" in kinds or "approx" in kinds:
        return (
            f"field verify: the reference marks its {architect} {note_words(kinds)}, so the size "
            "is measured on site; the vendor's size is not judged against it"
        )
    from app.review.assistant.ledger import format_inches

    by = f" by {format_inches(difference)}" if difference is not None else ""
    return (
        f"convention difference, field verify: the reference dimensions {architect} to the "
        f"wall's face, rounded to the nearest 1/2\" by drafting convention; the vendor's size "
        f"differs{by}"
    )


def mark(
    finding: Mapping[str, Any],
    labels: Callable[[str, int], Sequence[Mapping[str, Any]]],
    phrases: Callable[[str, int], Sequence[Mapping[str, Any]]],
    ends: Callable[[str, int, str], Any] | None = None,
) -> dict[str, Any] | None:
    """The record (`FIELD`) for one finding, else `None`: the notes code reads next to its
    reference label, or the half-inch convention when code proved the reference's dimension ends
    at a wall's face (`ends(file, page, label id)` gives its `dim_ends.Ends`) and the two values
    differ by 1/2" or less. A HOLD is kept so nothing softens it."""
    dims = finding.get("dims") if isinstance(finding.get("dims"), Mapping) else None
    side = (dims or {}).get("architect")
    key = _key(side)
    if key is None or not isinstance(side, Mapping) or "+" in str(side.get("id")):
        return None  # no single reference label (a chain's sum is its parts')
    file, page, label_id = str(side.get("file") or ""), int(side.get("page") or 0), str(side["id"])
    label = next((dict(l) for l in labels(file, page) if str(l.get("id")) == label_id), None)
    if label is None:
        return None
    inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
    vendor, architect = _decimal((inches or {}).get("vendor")), _decimal(
        (inches or {}).get("architect")
    )
    if architect is not None and _decimal(label.get("inches")) not in (None, architect):
        return None  # the label is not the size compared
    kinds: list[str] = list(read_note(label, phrases(file, page)))
    difference = abs(vendor - architect) if vendor is not None and architect is not None else None
    if (
        not kinds
        and ends is not None
        and difference is not None
        and 0 < difference <= Decimal(CONVENTION_MAX_IN.numerator) / CONVENTION_MAX_IN.denominator
    ):
        found = ends(file, page, label_id)
        if found is not None and "wall" in (found.low.kinds | found.high.kinds):
            kinds = ["convention"]
    if not kinds:
        return None
    record: dict[str, Any] = {"kinds": kinds, "label": key}
    if "hold" not in kinds:
        record["why"] = _why(kinds, finding, difference)
    else:
        record["why"] = f"the reference marks its {_flat(finding.get('architect'))} HOLD"
    return record
