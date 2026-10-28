"""Two independent readings before any red: every number a red rests on is read by two different
kinds of route that agree, and none of them names a doubt (final plan change #2, 2026-10-26).

Code reads a printed number in several ways that do not share their mistakes. They are the
**routes** (each a different kind of evidence):

- `text`: the PDF's own text layer prints it at its spot (exact, when the file has text);
- `ocr`: two OCR engines read the drawn ink and agree (`dim_labels`' drawn route, a pasted
  picture's or a vector page's OCR label, the architect's hand lettering, `verify_value`'s ink
  route, `read_number`'s OCR pair), or, for a typed number, read its own spot drawn as a picture
  and agree with the text (`typed_ocr`, option B of 2026-10-26). The two engines are one kind:
  they read the same pixels;
- `drawn_length`: the value agrees with its own dimension line's length at its view's fitted
  scale (`scale_fit`, final plan #3; `read_number`'s ruler);
- `units`: the millimetres printed with the inches agree with them (`units_check`);
- `strokes`: the pen-stroke reader names every character from known stroke shapes
  (`read_number`'s shape matcher, the vendor's own alphabet).

**The rule.** A number may feed a red only when at least two different kinds of route agree on
it, and no route and no reader names a doubt:

- a fraction that lost its slash (`7 14"` for `7 1/4"`);
- a pen-stroke guess (the stroke reader alone, or reading another value);
- millimetres and inches that disagree;
- a value that does not agree with its drawn length (`the vendor's 23" is drawn 24 3/16" long`,
  the owner's rule of 2026-10-26: such a number never makes a red);
- the drawing printing another value at the spot (`verify_value` mismatch, or both OCR engines
  reading another whole number of the same shape at a typed number's spot, `typed_ocr`);
- a second or third AI reading that is unsure (the AI is never a route, but its doubt counts).

A would-be red that fails it is a Likely problem with the reason in plain words (`held`): "the
vendor's 23" is read from the PDF's text, but its drawn length at its view's own scale is
24 3/16"". It never makes anything red: it only takes reds away.

**Which numbers a red rests on:** both values of a comparison (the vendor's and the reference's);
the vendor's value for a GV rule or GV's limits; every part and the printed overall of a vendor
string that does not add up, and both prints of a span the vendor prints twice (`ledger_ids`). A
red on words code read in the drawings' text (a finish code, a missing item: the `text` witness)
rests on no printed number and is not judged here.

**Recorded on the finding** (`FIELD`): each number's label, the routes that agree and any doubt,
worked out where the drawings' labels and line work are at hand (`mark`: the full check's free
steps, `escalation`, and the vendor consistency check). What only the finding's own checks know
(the text layer and the ink read at the spot, `verify_value`, `read_number`, the AI's reads) is
added when the item is grouped (`verdict`). A number with no record is judged on its label alone
(no drawn length). `agreed` gives each value's agreeing routes for a red's proof (data only).

Pure code: plain values in, plain values out. Verification:
`tests/review/assistant/test_reading_routes.py` (made-up findings and labels).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from app.review.assistant import witness
from app.review.assistant.finding_checks import units_check
from app.review.assistant.label_origin import ocr_read

__all__ = [
    "DRAWN",
    "DRAWN_DOUBT",
    "FIELD",
    "KINDS",
    "NOT_DOUBLE_READ",
    "OCR",
    "ROUTE_WORDS",
    "STROKES",
    "TEXT",
    "UNITS",
    "Value",
    "Verdict",
    "agreed",
    "drawn_length",
    "held",
    "lost_slash",
    "mark",
    "sides_needed",
    "verdict",
]

#: The finding's record of how each number it rests on was read.
FIELD: Final = "reading_routes"
#: Why a would-be red's second AI reading was never made (Anant, 2026-10-28: the reading's cap,
#: a failed call, an outage, a credit stop, the check stopped or ended first), set by the check
#: that reads twice: a doubt, so the item is a please-check "not double-read", never red, until a
#: reading is made (a Continue makes it). The simple full check reads once by design: never set.
NOT_DOUBLE_READ: Final = "not_double_read"

TEXT: Final = "text"
OCR: Final = "ocr"
DRAWN: Final = "drawn_length"
UNITS: Final = "units"
STROKES: Final = "strokes"
#: Every kind of route, in the order a record lists them.
KINDS: Final = (TEXT, OCR, DRAWN, UNITS, STROKES)
#: Each route as a sentence says it ("read 23" from the PDF's text and ...").
ROUTE_WORDS: Final[Mapping[str, str]] = {
    TEXT: "the PDF's text",
    OCR: "two OCR engines on the drawn ink",
    DRAWN: "its drawn length at its view's scale",
    UNITS: "the millimetres printed with it",
    STROKES: "the pen-stroke reader",
}
#: The doubt a value that does not agree with its drawn length names, before that length.
DRAWN_DOUBT: Final = "its drawn length at its view's own scale is "
#: How many different kinds of route must agree on a number before it may feed a red.
NEEDED: Final = 2

#: `read_number`'s routes (its `verify_<side>` words: "Read by code by ocr and shape: 24"").
_READ_NUMBER: Final[Mapping[str, str]] = {
    "text": TEXT,
    "ocr": OCR,
    "shape": STROKES,
    "alphabet": STROKES,
    "ruler": DRAWN,
}
_READ_BY: Final = re.compile(r"^Read by code by (.+?):")
#: A whole number, a space and a run of digits a slash would make a fraction of: `7 14"`, `15 12"`.
_LOST_SLASH: Final = re.compile(r"^\s*\d+\s+(\d)(\d{1,2})\s*(?:\"|''|in\b)?\s*$")
_DENOMINATORS: Final = frozenset({2, 4, 8, 16, 32, 64})
_WHOSE: Final[Mapping[str, str]] = {"vendor": "the vendor's", "architect": "the reference's"}

type FitOf = Callable[[str, int], Any]
"""A page's scale fit (`scale_fit.PageFit`) by (file, page), `None` when code has none."""
type LabelsOf = Callable[[str, int], Sequence[Mapping[str, Any]] | None]
"""A page's labels as `dims.json` holds them, by (file, page)."""
type NumberOf = Callable[[Mapping[str, Any]], tuple[tuple[str, ...], str | None] | None]
"""The number reader on a label (`read_number.label_routes`): `(the routes that agree, None)`,
`((), the other value it reads)` or `None`."""
type TypedOf = Callable[[Mapping[str, Any]], tuple[str, str | None] | None]
"""Both OCR engines on a typed label's own spot (`typed_ocr.TypedReader`): `("agrees", None)`,
`("disagrees", the value they read)` or `None`."""


@dataclass(frozen=True, slots=True)
class Value:
    """One number a red rests on, with the routes that agree on it and its doubts."""

    side: str
    label: str
    text: str
    routes: tuple[str, ...] = ()
    doubts: tuple[str, ...] = ()
    file: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """The label's file and `p<page>.<id>`: one label of the set."""
        return (self.file, self.label)

    @property
    def whose(self) -> str:
        return _WHOSE.get(self.side, "the")

    @property
    def enough(self) -> bool:
        return len(set(self.routes)) >= NEEDED and not self.doubts

    def as_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "label": self.label,
            "text": self.text,
            "routes": list(self.routes),
            "doubts": list(self.doubts),
            "file": self.file,
        }

    @classmethod
    def of(cls, raw: Mapping[str, Any]) -> Value:
        return cls(
            side=str(raw.get("side") or ""),
            label=str(raw.get("label") or ""),
            text=str(raw.get("text") or ""),
            routes=tuple(str(r) for r in raw.get("routes") or () if r in KINDS),
            doubts=tuple(str(d) for d in raw.get("doubts") or () if d),
            file=str(raw.get("file") or ""),
        )


@dataclass(frozen=True, slots=True)
class Verdict:
    """Whether a red's numbers were each read by two different routes with no doubt."""

    values: tuple[Value, ...] = ()
    doubts: tuple[str, ...] = field(default=())
    """Doubts about the whole item (an unsure AI reading)."""
    judged: bool = True
    """False for a red that rests on no printed number (words read from the text)."""
    unrecorded: bool = False
    """A vendor string with no record of how its parts were read (`mark` never saw it)."""

    @property
    def ok(self) -> bool:
        return self.kind == "ok"

    @property
    def kind(self) -> str:
        """`ok`; `doubt` (a route or reader names one); `missing` (a number has one route only)."""
        if not self.judged:
            return "ok"
        if self.doubts or any(v.doubts for v in self.values):
            return "doubt"
        if self.unrecorded or not self.values or not all(v.enough for v in self.values):
            return "missing"
        return "ok"

    def reason(self) -> str | None:
        """Why the item is held back, in one plain line; `None` when it may be red."""
        if self.ok:
            return None
        for value in self.values:
            if value.doubts:
                return _doubt_words(value)
        if self.doubts:
            return self.doubts[0]
        for value in self.values:
            if not value.enough:
                return _missing_words(value)
        if self.unrecorded:
            return "code has no record of how the string's other numbers were read"
        return "code could not tell how the numbers were read"


def lost_slash(text: object) -> bool:
    """A value that reads like a fraction whose slash was lost (`7 14"` for `7 1/4"`): a whole
    number, a space, and digits that split into a fraction smaller than one."""
    found = _LOST_SLASH.match(str(text or ""))
    if found is None:
        return False
    numerator, denominator = int(found.group(1)), int(found.group(2))
    return denominator in _DENOMINATORS and 0 < numerator < denominator


def _inches(value: float) -> str:
    """Inches to the nearest 1/16 as a drawing writes them: `24 3/16"`."""
    sixteenths = round(abs(value) * 16)
    whole, rest = divmod(sixteenths, 16)
    if not rest:
        return f'{whole}"'
    numerator, denominator = rest, 16
    while numerator % 2 == 0:
        numerator //= 2
        denominator //= 2
    return f'{whole} {numerator}/{denominator}"' if whole else f'{numerator}/{denominator}"'


def _ref(label: Mapping[str, Any]) -> str:
    return f"p{label.get('page')}.{label.get('id')}"


def _key(label: Mapping[str, Any]) -> tuple[str, str]:
    return (str(label.get("file") or ""), _ref(label))


def _label_value(
    label: Mapping[str, Any],
    side: str,
    fit: Any,
    *,
    full: Mapping[str, Any] | None = None,
    typed: TypedOf | None = None,
    numbers: NumberOf | None = None,
) -> Value:
    """A label's routes and doubts from what code read for it: its reading route, its millimetres,
    its drawn length at its view's fit (`fit`, a `scale_fit.PageFit` or `None`) and, for a typed
    number, both OCR engines reading its own spot (`typed`, `typed_ocr`)."""
    data = {**dict(label), **dict(full or {})}
    text = str(data.get("text") or "")
    routes: list[str] = []
    doubts: list[str] = []
    if ocr_read(data) or data.get("route") == "drawn":
        routes.append(OCR)
    elif data.get("route") in (None, "text") and text:
        routes.append(TEXT)
    units = data.get("units") if data.get("units") in ("agrees", "disagrees") else None
    units = units or units_check(text)
    if units == "agrees":
        routes.append(UNITS)
    elif units == "disagrees":
        doubts.append("its millimetres and inches disagree")
    check = None
    if fit is not None:
        try:
            check = fit.check(data.get("id"))
        except Exception:  # noqa: BLE001 - no check: the drawn length says nothing
            check = None
    if check is not None:
        if check.agrees:
            routes.append(DRAWN)
        else:
            doubts.append(f"{DRAWN_DOUBT}{_inches(check.drawn_in)}")
    if lost_slash(text):
        doubts.append("it reads like a fraction that lost its slash")
    if typed is not None and TEXT in routes:
        try:
            answer = typed(data)
        except Exception:  # noqa: BLE001 - an unread spot is no route and no doubt
            answer = None
        if answer is not None and answer[0] == "agrees":
            routes.append(OCR)
        elif answer is not None and answer[0] == "disagrees":
            doubts.append(f"two OCR engines read {answer[1]} at its spot")
    if numbers is not None and not doubts and len(set(routes)) < NEEDED:
        # Code's number reader (`read_number`): its stroke shapes and its ruler (the line's length
        # at the drawing's scale) are other kinds of route than the label's own.
        try:
            read = numbers(data)
        except Exception:  # noqa: BLE001 - not read: no route and no doubt
            read = None
        if read is not None and read[1] is not None:
            doubts.append(f"code's number reader reads {read[1]} at its spot")
        elif read is not None:
            routes += [_READ_NUMBER[name] for name in read[0] if name in _READ_NUMBER]
    return Value(
        side,
        _ref(data),
        text,
        tuple(dict.fromkeys(routes)),
        tuple(doubts),
        str(data.get("file") or ""),
    )


def _dims(finding: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    dims = finding.get("dims")
    if not isinstance(dims, Mapping):
        return {}
    return {
        side: label
        for side in ("vendor", "architect")
        if isinstance(label := dims.get(side), Mapping) and label.get("id")
    }


def _file_of(finding: Mapping[str, Any]) -> str | None:
    for key in ("vendor_box", "arch_box"):
        box = finding.get(key)
        if isinstance(box, Mapping) and box.get("file"):
            return str(box["file"])
    for label in _dims(finding).values():
        if label.get("file"):
            return str(label["file"])
    return None


_LEDGER_REF: Final = re.compile(r"^p(\d+)\.(\S+)$")


def _ledger_labels(finding: Mapping[str, Any]) -> list[tuple[str, int, str, str]]:
    """A vendor string's or a twice-printed span's labels (`ledger_ids`: `p13.D7`) as (file, page,
    id, side): every one is the vendor's, but the reference print of a span is the "reference"."""
    refs = finding.get("ledger_ids")
    file = _file_of(finding)
    if not isinstance(refs, list) or not refs or file is None:
        return []
    reference = None
    if finding.get("reference_side") == "vendor":
        architect = _dims(finding).get("architect")
        reference = _ref(architect) if architect is not None else None
    out: list[tuple[str, int, str, str]] = []
    for ref in refs:
        found = _LEDGER_REF.match(str(ref))
        if found is None:
            continue
        side = "architect" if reference is not None and str(ref) == reference else "vendor"
        out.append((file, int(found.group(1)), found.group(2), side))
    return out


def _uses_numbers(finding: Mapping[str, Any]) -> bool:
    """Whether the item rests on printed numbers: not words read from the drawings' text (a finish
    code, a missing item), which have no number to read twice."""
    stored = finding.get("witnesses")
    found = (
        [str(w) for w in stored]
        if isinstance(stored, list)
        else witness.witnesses(finding.get("checks") or {}, finding.get("status"))
    )
    return "text" not in found


def _sides_needed(finding: Mapping[str, Any]) -> tuple[str, ...]:
    """The sides whose numbers a red compares: the vendor's alone against GV's rule or limits."""
    settled = finding.get("settled")
    if finding.get("source") in ("rules", "vendor_arithmetic") or (
        isinstance(settled, Mapping) and settled.get("method") == "gv_limits"
    ):
        return ("vendor",)
    return ("vendor", "architect")


def sides_needed(finding: Mapping[str, Any]) -> tuple[str, ...]:
    """The sides whose numbers a red of this item compares (`_sides_needed`): the vendor's alone
    against GV's rule, its limits or the vendor's own string; else both."""
    return _sides_needed(finding)


def mark(
    finding: dict[str, Any],
    labels_of: LabelsOf | None,
    fit_of: FitOf | None,
    typed: TypedOf | None = None,
    numbers: NumberOf | None = None,
) -> bool:
    """Record on the finding (`FIELD`) how each number it rests on was read: its label's route, its
    millimetres and its drawn length at its view's fit. True when the record changed. Only an item
    code says is Wrong is recorded (a Right or an open item needs no proof of its reading)."""
    if finding.get("status") != "different" or not _uses_numbers(finding):
        return False
    wanted: list[tuple[str, int, str, str, Mapping[str, Any] | None]] = []
    ledger = _ledger_labels(finding)
    if ledger and finding.get("source") in ("vendor_arithmetic", "cross_view"):
        wanted = [(file, page, key, side, None) for file, page, key, side in ledger]
    else:
        dims = _dims(finding)
        for side in _sides_needed(finding):
            label = dims.get(side)
            if label is None or not label.get("file"):
                continue
            wanted.append(
                (str(label["file"]), int(label.get("page") or 0), str(label["id"]), side, label)
            )
    values: list[dict[str, Any]] = []
    fits: dict[tuple[str, int], Any] = {}
    for file, page, key, side, own in wanted:
        full = None
        if labels_of is not None:
            try:
                full = next(
                    (
                        la
                        for la in labels_of(file, page) or ()
                        if isinstance(la, Mapping) and str(la.get("id")) == key
                    ),
                    None,
                )
            except Exception:  # noqa: BLE001 - the finding's own label then
                full = None
        if full is None and own is None:
            continue
        if (file, page) not in fits:
            try:
                fits[(file, page)] = fit_of(file, page) if fit_of is not None else None
            except Exception:  # noqa: BLE001 - no fit: no drawn length
                fits[(file, page)] = None
        base = dict(own or {})
        base.update({"id": key, "page": page, "file": file})
        values.append(
            _label_value(
                base, side, fits[(file, page)], full=full, typed=typed, numbers=numbers
            ).as_dict()
        )
    before = finding.get(FIELD)
    finding[FIELD] = {"values": values}
    # With what the finding's own checks read at its spots, so the record is the whole proof.
    found = verdict(finding)
    record = {
        "values": [v.as_dict() for v in found.values],
        "doubts": list(found.doubts),
        "enough": found.ok,
    }
    finding[FIELD] = record
    return before != record


def _stored(finding: Mapping[str, Any]) -> list[Value] | None:
    record = finding.get(FIELD)
    if not isinstance(record, Mapping) or not isinstance(record.get("values"), list):
        return None
    return [Value.of(raw) for raw in record["values"] if isinstance(raw, Mapping)]


def _read_number_routes(words: object) -> list[str]:
    found = _READ_BY.match(str(words or ""))
    if found is None:
        return []
    names = [n.strip() for n in re.split(r",|\band\b", found.group(1)) if n.strip()]
    return [_READ_NUMBER[n] for n in names if n in _READ_NUMBER]


def _spot_routes(checks: Mapping[str, Any], side: str) -> tuple[list[str], list[str]]:
    """What the finding's own checks read at a side's spot: routes and doubts."""
    routes: list[str] = []
    doubts: list[str] = []
    if checks.get(f"printed_{side}") == "match":
        routes.append(TEXT)
    verified = checks.get(f"verify_{side}")
    if isinstance(verified, Mapping):
        result = verified.get("result")
        if result == "match":
            by_number = _read_number_routes(verified.get("words"))
            if by_number:
                routes += by_number
                if STROKES in by_number and not ({TEXT, OCR} & set(by_number)):
                    doubts.append("it is a pen-stroke reading that no OCR or text reading confirms")
            else:
                routes.append(OCR if verified.get("route") == "drawn" else TEXT)
        elif result == "mismatch":
            doubts.append(f"the drawing prints {verified.get('read')} at its spot")
    return routes, doubts


def _ai_doubts(checks: Mapping[str, Any], finding: Mapping[str, Any]) -> list[str]:
    """An AI reading of the spot that is unsure or disagrees (never a route, but a doubt); or no
    AI reading at all where the check reads twice (`NOT_DOUBLE_READ`)."""
    result = witness.read_result(checks)
    why = finding.get(NOT_DOUBLE_READ)
    if result in (None, "not_run") and isinstance(why, str) and why.strip():
        return [f"not double-read: {why.strip()}"]
    if result == "unsure":
        return ["a second reading of the drawing was unsure"]
    if result == "disagrees":
        return ["a second reading of the drawing disagrees"]
    return []


def verdict(finding: Mapping[str, Any]) -> Verdict:
    """The item's numbers, each with its agreeing routes and doubts (see the module): the record
    `mark` stored where it still names the item's labels, else each label alone, with what the
    finding's own checks read at its spots added."""
    if not _uses_numbers(finding):
        return Verdict(judged=False)
    checks = finding.get("checks") if isinstance(finding.get("checks"), Mapping) else {}
    checks = checks or {}
    dims = _dims(finding)
    stored = _stored(finding)
    values: list[Value] = []
    if stored is not None and finding.get("source") in ("vendor_arithmetic", "cross_view"):
        values = list(stored)
    else:
        by_key = {v.key: v for v in stored or ()}
        for side in _sides_needed(finding):
            label = dims.get(side)
            if label is None:
                inches = finding.get("inches") if isinstance(finding.get("inches"), Mapping) else {}
                if side == "vendor" or (inches or {}).get(side) is not None:
                    # A number with no label: its record, else only the checks at its spot.
                    kept = next((v for v in stored or () if v.side == side and not v.label), None)
                    values.append(kept or Value(side, "", str(finding.get(side) or "")))
                continue
            kept = by_key.get(_key(label))
            values.append(kept if kept is not None else _label_value(label, side, None))
    out: list[Value] = []
    marked = {side: _key(label) for side, label in dims.items()}
    seen: set[str] = set()
    for value in values:
        if value.side in seen or (value.label and marked.get(value.side) != value.key):
            out.append(value)  # a string's other part: only its own record speaks for it
            continue
        seen.add(value.side)
        routes, doubts = _spot_routes(checks, value.side)
        out.append(
            Value(
                value.side,
                value.label,
                value.text or str(finding.get(value.side) or ""),
                tuple(dict.fromkeys((*value.routes, *routes))),
                tuple(dict.fromkeys((*value.doubts, *doubts))),
                value.file,
            )
        )
    return Verdict(
        values=tuple(out),
        doubts=tuple(_ai_doubts(checks, finding)),
        unrecorded=stored is None and finding.get("source") == "vendor_arithmetic",
    )


def drawn_length(finding: Mapping[str, Any], side: str = "vendor") -> str | None:
    """The drawn length (`24 3/16"`) code measured for the item's own `side` label when that
    length does not agree with its printed value (the `DRAWN_DOUBT`), else `None`."""
    label = _dims(finding).get(side)
    if label is None:
        return None
    key = _key(label)
    for value in verdict(finding).values:
        if value.side != side or (value.label and value.key != key):
            continue
        for doubt in value.doubts:
            if doubt.startswith(DRAWN_DOUBT):
                return doubt[len(DRAWN_DOUBT) :].strip() or None
    return None


def held(finding: Mapping[str, Any]) -> str | None:
    """Why a would-be red is held back for its reading, in one plain line, else `None`."""
    return verdict(finding).reason()


def agreed(finding: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Each number of the item with the routes that agree on it (a red's proof, data only)."""
    return [
        {"side": v.side, "label": v.label, "text": v.text, "routes": list(v.routes)}
        for v in verdict(finding).values
    ]


def _read_words(value: Value) -> str:
    named = [ROUTE_WORDS[r] for r in value.routes if r in ROUTE_WORDS]
    if not named:
        return f"{value.whose} {value.text} is read by no code route"
    return f"{value.whose} {value.text} is read from {' and '.join(named)}"


def _doubt_words(value: Value) -> str:
    """`the vendor's 23" is read from the PDF's text, but its drawn length at its view's own scale
    is 24 3/16"`."""
    return f"{_read_words(value)}, but {value.doubts[0]}"


def _missing_words(value: Value) -> str:
    return (
        f"{_read_words(value)} only: no second, different reading agrees yet (its drawn length, "
        "its millimetres or an OCR reading)"
    )
