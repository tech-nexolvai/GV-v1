"""Short text values: sheet and revision numbers, callouts, tags, item numbers and dates (2026-11-04).

Some items compare an identifier, not a size: a sheet number, a revision number, a detail callout,
a tag, an item or room number, a date. On a real set two of them went wrong the same way. A
helper's "Key plan: revision number", the same one-digit number on both sides, went green: on each
sheet the digit was the last character of the street address in the title block, and code's
checks read it as that many inches printed at its spot. A sheet number such as "4.2 against 4.5"
was said as "the vendor's 4 3/16" ... they differ by 5/16"". So:

- **What an identifier is** (`kind`): the item names one (a revision, a sheet / drawing / page
  number, a callout or keynote, an item / room / unit number, a tag, a date) and no size (width,
  depth, offset, diameter, size...); or a side is a whole date, or starts with its own field word
  (`REV 2`, `SHEET 4`). Both sides must be values (a few characters, not "not shown" or "see the
  drawing": an absence keeps its own path). A tag, callout or item number the tag reader reads
  (a tag code, or words: `AB 9.9X`; `tag_reader.is_tag_item`, `words_only`) stays the tag
  reader's: revision and sheet numbers, dates, and values code would read as numbers (`6`, `101`,
  `9.9X`) are this module's.
- **Never inches** (`as_text`): its values are never read as a size anywhere. The trust checks hold
  no size reading of it (a lone `6` found in a box proves nothing about a revision) and its
  `inches` stay empty, so no step adds it up, pairs it by size or says "differs by 3/16"". Two
  values compare as text (`normal`: capitals, any dash a hyphen, the field word, spaces and hyphens
  dropped): the same, or they differ.
- **Proof of place** (`place`): each side's value must be printed whole at its spot, as code reads
  the page's text (`dims.json` phrases): the `6` of `48 MADE-UP ROAD 6` is part of other words,
  not a value of its own. A short value (one or two characters, `SHORT`) also needs its labelled
  field: its phrase carries its field word (`REV 2`), or it sits under, or right of, a heading of
  its kind (`REV`, `SHEET NO.`, `DATE`, ...) with nothing between. Anywhere else a lone `6` or `A`
  proves nothing: not proved, never Right and never Wrong.
- **Each drawing's own numbering** (PROPOSED 2026-11-04, waiting for Anant; `OWN_NUMBERING`): a
  vendor sheet's revision number and the architect's are each document's own numbering, so the
  same number or another one proves nothing about the drawings. Never Right or Wrong: Not compared,
  saying that whether the vendor's sheet follows the architect's latest revision is the date and
  revision-cloud check, not this pair.

The escalation reads both spots of each identifier item (`mark`) and keeps the record (`FIELD`):
the same text at both spots, each proved in its place, is Right; text that differs is a
please-check (printed words that differ, as `tag_reader.printed_difference`); never red. The
classification asks `held` why an item cannot be Right or Wrong. An item with no record (a chat
answer, one stored before) is never Right or Wrong on its identifier.

Pure code: plain values in, plain values out. Verification:
`tests/review/assistant/test_text_values.py` (made-up values and phrases).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from app.review.assistant import title_block, witness
from vocabulary.dashes import dashes_to_hyphen

__all__ = [
    "FIELD",
    "OWN_NUMBERING",
    "SHORT",
    "Kind",
    "Place",
    "as_text",
    "blocks_as_text",
    "held",
    "identifier",
    "kind",
    "mark",
    "normal",
    "outcome",
    "place",
    "please_check_words",
    "read_both",
    "same_text",
    "short",
]

type Kind = Literal["date", "revision", "sheet", "callout", "number", "tag"]

#: The finding's field for what code read at each side's spot (`mark`).
FIELD: Final = "text_value"
#: A value this long or shorter (letters and digits, as `normal` writes it) needs its labelled field.
SHORT: Final = 2
#: The rule for a pair of revision numbers (PROPOSED 2026-11-04, waiting for Anant).
OWN_NUMBERING: Final = (
    "a revision number is each drawing's own numbering: the vendor's and the architect's being "
    "the same or different says nothing about the drawings (PROPOSED 2026-11-04); whether the "
    "vendor's sheet follows the architect's latest revision is the date and revision-cloud check"
)
_OWN_SETTLE: Final = (
    "the architect's latest revision (its date or clouds) against the vendor's sheet, not the two "
    "revision numbers"
)

#: What an item names, tried in this order (a "sheet date vs design revision" is about dates).
_ITEM_KINDS: Final[tuple[tuple[Kind, re.Pattern[str]], ...]] = (
    ("date", re.compile(r"\bdate[sd]?\b", re.IGNORECASE)),
    (
        "revision",
        re.compile(r"\brev(?:ision)?s?\b\.?(?!\s*(?:cloud|delta|triangle|bubble))", re.IGNORECASE),
    ),
    (
        "sheet",
        re.compile(
            r"\b(?:sheet|sht|drawing|dwg|page)\.?\s*(?:no\b\.?|number|#|id\b|ref(?:erence)?\b)",
            re.IGNORECASE,
        ),
    ),
    (
        "callout",
        re.compile(
            r"\bcallouts?\b|\bkey\s*notes?\b"
            r"|\b(?:detail|section|elevation|view)\s*(?:no\b\.?|number|#|bubble|mark|ref\w*)",
            re.IGNORECASE,
        ),
    ),
    (
        "number",
        re.compile(
            r"\b(?:item|room|unit|cabinet|suite)\.?\s*(?:no\b\.?|number|#|code|id\b)",
            re.IGNORECASE,
        ),
    ),
    ("tag", re.compile(r"\btags?\b", re.IGNORECASE)),
)
#: Words that make an item a size, whatever else it names ("detail 3: width" is a width).
_SIZE_WORDS: Final = re.compile(
    r"\b(?:widths?|wide|depths?|deep|heights?|high|tall|lengths?|long|offsets?|diameters?|dia|"
    r"radius|radii|sizes?|thickness|thick|overhangs?|clearances?|distances?|spacing|gaps?|"
    r"reveals?|projections?|setbacks?|dimensions?|strings?|spans?)\b",
    re.IGNORECASE,
)
#: A side that is one whole date: `03/14/2026`, `2026/01/05`, `25.12.25`.
_DATE: Final = re.compile(r"^(\d{1,4})[/.-](\d{1,2})[/.-](\d{2,4})$")
#: The field words a title block prints with (or over) each kind of value. A short value is proved
#: in its place only beside one of its own kind.
_HEADINGS: Final[Mapping[str, str]] = {
    "revision": r"REV(?:ISION)?S?\.?(?:\s*(?:NO\.?|#|NUMBER|LETTER|MARK|LEVEL))?",
    "sheet": (
        r"(?:SHEET|SHT\.?)(?:\s*(?:NO\.?|#|NUMBER|ID))?|(?:DWG|DRAWING)\.?\s*(?:NO\.?|#|NUMBER)?"
        r"|PAGE(?:\s*(?:NO\.?|#|NUMBER))?"
    ),
    "date": r"(?:(?:ISSUE|ISSUED|REV(?:ISION)?\.?|PLOT|SHEET|DRAWING)\s*)?DATE(?:\s*ISSUED)?",
    "callout": r"(?:DETAIL|SECTION|ELEVATION|VIEW|KEY\s*NOTE|KEYNOTE)(?:\s*(?:NO\.?|#|NUMBER))?",
    "number": r"(?:ITEM|ROOM|UNIT|CABINET|SUITE)\.?\s*(?:NO\.?|#|NUMBER|CODE|ID)?",
    "tag": r"TAG|MARK|TYPE",
}
_HEADING: Final[Mapping[str, re.Pattern[str]]] = {
    name: re.compile(rf"^(?:{pattern})\s*[:#.]?$") for name, pattern in _HEADINGS.items()
}
#: A phrase that carries its own field word: `REV 2`, `REV. NO.: 2`, `SHEET 4`.
_WITH_FIELD: Final[Mapping[str, re.Pattern[str]]] = {
    name: re.compile(
        rf"^(?P<field>{pattern})\s*[:#.]?\s+(?P<value>\S.*)$|^(?P<field2>{pattern})"
        r"\s*[:#]\s*(?P<value2>\S.*)$"
    )
    for name, pattern in _HEADINGS.items()
}
#: A side's own field word, for the kind it names (`REV 2` is a revision, `SHEET 4` a sheet).
_VALUE_FIELD: Final[tuple[tuple[Kind, re.Pattern[str]], ...]] = (
    ("revision", re.compile(rf"^(?:{_HEADINGS['revision']})\s*[:#.]?\s+\S")),
    ("sheet", re.compile(rf"^(?:{_HEADINGS['sheet']})\s*[:#.]?\s+\S")),
)
#: The sources whose two sides are sizes by how code made them (a string's sum, GV's rule).
_SIZE_SOURCES: Final = frozenset({"vendor_arithmetic", "rules"})
#: How far round a spot a printed phrase may sit and still be the spot's (points), as the tag
#: reader reads a spot.
_SLACK_PT: Final = 2.0
#: A sheet number printed with its discipline's letters before it (`AB 4.2`, `A-101`) holds the
#: number a reading may give alone (`4.2`).
_DISCIPLINE: Final = re.compile(r"^[A-Z]{1,3}")
#: The tag reader's record (`tag_reader.TAG_READ`): a tag item it read stays its own.
_TAG_READ: Final = "tag_read"
#: A side that is a value: at most this many characters and words.
_VALUE_CHARS: Final = 24
_VALUE_WORDS: Final = 3
#: A side that says the thing is not there, or points elsewhere: no value (an absence keeps the
#: missing-item path, `classification.missing_on_vendor`).
_NO_VALUE: Final = re.compile(
    r"^(?:not\b|no\b(?!\.)|none\b|missing\b|absent\b|n/?a\b|see\b|tbd\b|unread\w*|illegible\b|-+$)",
    re.IGNORECASE,
)

type Box = tuple[float, float, float, float]


def _flat(value: object) -> str:
    return " ".join(str(value or "").split())


def _upper(text: object) -> str:
    return dashes_to_hyphen(_flat(text)).upper().strip(" \"'“”‘’")


def kind(finding: Mapping[str, Any]) -> Kind | None:
    """What identifier the item compares (see the module), else `None` (a size, words, a tag the
    tag reader compares, a string's sum, GV's rule)."""
    if finding.get("source") in _SIZE_SOURCES or finding.get(_TAG_READ) is not None:
        return None
    item = _flat(finding.get("item"))
    if _SIZE_WORDS.search(item):
        return None
    sides = [_upper(finding.get(side)) for side in ("vendor", "architect")]
    if not all(_value_like(side) for side in sides):
        return None
    found: Kind | None = None
    if any(_DATE.match(side) for side in sides):
        found = "date"
    else:
        found = next((name for name, pattern in _ITEM_KINDS if pattern.search(item)), None)
    if found is None:
        found = next(
            (name for name, pattern in _VALUE_FIELD for side in sides if pattern.match(side)), None
        )
    if found in ("tag", "callout", "number") and _tag_reader_reads(finding):
        return None  # the tag reader compares tags and printed words (`tag_reader`)
    return found


def _value_like(side: str) -> bool:
    """A side that is one short value (`6`, `AB 4.2`, `03/14/2026`), not a sentence or an
    absence."""
    return (
        bool(re.search(r"[A-Z0-9]", side))
        and len(side) <= _VALUE_CHARS
        and len(side.split()) <= _VALUE_WORDS
        and _NO_VALUE.match(side) is None
    )


def _tag_reader_reads(finding: Mapping[str, Any]) -> bool:
    """A tag comparison, or a vendor side in words (`AB 9.9X`): the tag reader reads both spots'
    printed words (`tag_reader.decide`, `decide_words`)."""
    from app.review.assistant.tag_reader import is_tag_item, words_only

    return is_tag_item(finding) or words_only(finding.get("vendor"))


def identifier(finding: Mapping[str, Any]) -> bool:
    """Whether the item compares an identifier, never a size (`kind`)."""
    return kind(finding) is not None


def _date(text: str) -> str | None:
    """A whole date as `YYYY-MM-DD` (month first unless it cannot be, as `title_block`)."""
    found = _DATE.match(text)
    if found is None:
        return None
    a, b, c = found.groups()
    try:
        if len(a) == 4:
            year, month, day = int(a), int(b), int(c)
        else:
            year, month, day = int(c) + (2000 if len(c) == 2 else 0), int(a), int(b)
            if month > 12:
                month, day = day, month
        if not (1 <= month <= 12 and 1 <= day <= 31):
            return None
    except ValueError:
        return None
    return f"{year:04d}-{month:02d}-{day:02d}"


def _value_part(text: str, name: str | None) -> tuple[str | None, str]:
    """(the field word printed with it, the value) for a side or a phrase: `REV 2` is (`REV`,
    `2`) for a revision; a phrase with no field word of `name` is (None, itself)."""
    upper = _upper(text)
    for kind_name in [name] if name else list(_WITH_FIELD):
        pattern = _WITH_FIELD.get(str(kind_name))
        found = pattern.match(upper) if pattern is not None else None
        if found is not None:
            field = found.group("field") or found.group("field2")
            value = found.group("value") or found.group("value2")
            return " ".join(field.split()), value
    return None, upper


def normal(text: object, name: str | None = None) -> str:
    """A value as code compares it: capitals, any look-alike dash a hyphen, its field word left out
    (`REV. 2` is `2`), spaces and hyphens dropped (`A-101` and `A 101` are `A101`); a whole date
    as `YYYY-MM-DD`."""
    _field, value = _value_part(str(text or ""), name)
    value = value.strip(" .,;:")
    date = _date(value.replace(" ", ""))
    if date is not None:
        return date
    return re.sub(r"[\s\-]+", "", value)


def short(text: object, name: str | None = None) -> bool:
    """A value of one or two letters or digits (`6`, `A`, `12`): found at a spot, it proves its place
    only in its labelled field."""
    value = normal(text, name)
    return 0 < len(value) <= SHORT and value.isalnum()


def same_text(a: object, b: object, name: str | None = None) -> bool:
    """Two values that are the same text as code compares them (`normal`), never as sizes."""
    left, right = normal(a, name), normal(b, name)
    return bool(left) and left == right


# ---- proof of place -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Place:
    """Where one side's value is printed, as code reads the page's text."""

    printed: bool
    """The value is printed whole at the spot (a phrase that is the value, with its field word or
    its sheet's discipline letters at most)."""
    placed: bool
    """And it is in its place: a value longer than `SHORT`, or a short one in its labelled
    field."""
    read: str | None = None
    """What code read for the value there, as `normal` writes it (`AB4.2` for `AB 4.2`)."""
    phrase: str | None = None
    """The printed phrase code read at the spot."""
    field: str | None = None
    """The field word or heading that labels it (`REV`, `SHEET NUMBER`)."""
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "printed": self.printed,
            "placed": self.placed,
            "read": self.read,
            "phrase": self.phrase,
            "field": self.field,
            "why": self.why,
        }


def _box(raw: object) -> Box | None:
    if isinstance(raw, Mapping):
        raw = [raw.get(k) for k in ("x0", "y0", "x1", "y1")]
    if not isinstance(raw, Sequence) or isinstance(raw, str) or len(raw) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in raw)
    except (TypeError, ValueError):
        return None
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _meets(a: Box, b: Box, slack: float = _SLACK_PT) -> bool:
    return (
        a[0] < b[2] + slack and b[0] < a[2] + slack and a[1] < b[3] + slack and b[1] < a[3] + slack
    )


def _holds(phrase: str, want: str, name: str | None) -> tuple[bool, str | None, str]:
    """(the phrase holds the value whole, its field word, what code reads as the value)."""
    field, value = _value_part(phrase, name)
    read = normal(value, name)
    if read == want:
        return True, field, read
    if name == "sheet" and want[:1].isdigit() and read.endswith(want):
        # `AB 4.2` holds the sheet number a reading gave as `4.2`: the letters are its discipline.
        prefix = read[: len(read) - len(want)]
        if _DISCIPLINE.fullmatch(prefix):
            return True, field, read
    return False, field, read


def _heading_over(
    value: Box, phrases: Sequence[tuple[str, Box]], name: str
) -> tuple[str, Box] | None:
    """The heading of kind `name` that labels the phrase at `value`: right above it in its column
    (its top at most `title_block.BELOW_PT` under the heading, the two overlapping across) with no
    phrase between, or left of it on its line (at most `title_block.RIGHT_PT`) with none between."""
    pattern = _HEADING[name]
    middle = (value[1] + value[3]) / 2
    above: list[tuple[float, str, Box]] = []
    left: list[tuple[float, str, Box]] = []
    for text, box in phrases:
        if box == value:
            continue
        across = box[0] < value[2] and value[0] < box[2]
        if across and 0 <= value[1] - box[3] <= title_block.BELOW_PT:
            above.append((value[1] - box[3], text, box))
        same_line = abs((box[1] + box[3]) / 2 - middle) <= title_block.SAME_LINE_PT
        if same_line and 0 <= value[0] - box[2] <= title_block.RIGHT_PT:
            left.append((value[0] - box[2], text, box))
    for candidates in (above, left):
        if not candidates:
            continue
        _gap, text, box = min(candidates, key=lambda c: c[0])
        if pattern.match(_upper(text)):
            return text, box
    return None


def place(
    phrases: Sequence[Mapping[str, Any]], box: object, value: object, name: str | None = None
) -> Place:
    """Where `value` is printed at the spot `box` among a page's `phrases` (`dims.json`: text and
    box), for an identifier of kind `name` (see the module)."""
    spot = _box(box)
    want = normal(value, name)
    if spot is None:
        return Place(False, False, why="the value has no spot on the drawing")
    if not want:
        return Place(False, False, why="the value names nothing code can look for")
    page = [
        (_flat(p.get("text")), b) for p in phrases if (b := _box(p.get("box"))) and p.get("text")
    ]
    here = [(text, b) for text, b in page if text and _meets(b, spot)]
    if not here:
        return Place(
            False, False, why="code finds no printed text at the spot (it may be drawn as strokes)"
        )
    fragment: str | None = None
    for text, b in here:
        whole, field, read = _holds(text, want, name)
        if not whole:
            tokens = re.split(r"[^A-Z0-9.]+", _upper(text))
            if fragment is None and want in {t.strip(".") for t in tokens}:
                fragment = text
            continue
        if not short(value, name):
            return Place(True, True, read, text, field, f'code reads "{text}" at the spot')
        if field is not None:
            return Place(True, True, read, text, field, f'code reads "{text}" at the spot')
        heading = _heading_over(b, page, name) if name is not None and name in _HEADING else None
        if heading is not None:
            return Place(
                True,
                True,
                read,
                text,
                _flat(heading[0]),
                f'code reads "{text}" at the spot, in its field under or beside "{heading[0]}"',
            )
        return Place(
            True,
            False,
            read,
            text,
            None,
            f'code reads "{text}" at the spot, but a value of one or two characters proves its '
            "place only in its labelled field (a heading of its kind right above or beside it); "
            "none labels this one",
        )
    if fragment is not None:
        return Place(
            False,
            False,
            None,
            fragment,
            None,
            f'the "{want}" at the spot is part of the printed words "{fragment}", not a value of '
            "its own",
        )
    shown = "; ".join(text for text, _b in here)[:120]
    return Place(False, False, None, shown, None, f'code reads "{shown}" at the spot, not "{want}"')


# ---- the record ---------------------------------------------------------------------------------


_SIDES: Final = (("vendor", "vendor_box"), ("architect", "arch_box"))


def _spot_of(finding: Mapping[str, Any], box_field: str) -> tuple[str | None, int, Box] | None:
    raw = finding.get(box_field)
    box = _box(raw)
    if box is None or not isinstance(raw, Mapping):
        return None
    page = raw.get("page")
    if isinstance(page, bool) or not isinstance(page, int):
        return None
    file = raw.get("file")
    return (str(file) if file else None, page, box)


def mark(
    finding: Mapping[str, Any],
    phrases: Callable[[str | None, int], Sequence[Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """The record (`FIELD`) for an identifier item, else `None`: what code reads at each side's
    spot (`place`), for the values and spots the item holds now. `phrases(file, page)`: the page's
    printed phrases (`None` file: the main upload)."""
    name = kind(finding)
    if name is None:
        return None
    record: dict[str, Any] = {"kind": name}
    for side, box_field in _SIDES:
        at = _spot_of(finding, box_field)
        value = _flat(finding.get(side))
        found = (
            place(phrases(at[0], at[1]), at[2], value, name)
            if at is not None
            else Place(False, False, why="the value has no spot on the drawing")
        )
        record[side] = {
            **found.as_dict(),
            "value": value,
            "box": list(at[2]) if at is not None else None,
            "page": at[1] if at is not None else None,
        }
    return record


def _current(finding: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The record, when it was read for the values and spots the item holds now."""
    record = finding.get(FIELD)
    if not isinstance(record, Mapping) or record.get("kind") != kind(finding):
        return None
    for side, box_field in _SIDES:
        held = record.get(side)
        if not isinstance(held, Mapping) or held.get("value") != _flat(finding.get(side)):
            return None
        at = _spot_of(finding, box_field)
        if (list(at[2]) if at is not None else None) != held.get("box"):
            return None
    return record


def _side_words(side: str) -> str:
    return "the vendor's" if side == "vendor" else "the architect's"


def _kind_words(name: str) -> str:
    return {
        "date": "date",
        "revision": "revision number",
        "sheet": "sheet number",
        "callout": "callout",
        "number": "number",
        "tag": "tag",
    }.get(name, "value")


def outcome(finding: Mapping[str, Any]) -> Literal["right", "please_check"] | None:
    """What code's record proves: `right` (both sides proved in their place and the same text, a
    kind that may be Right), `please_check` (both proved and the text differs), else `None`."""
    record = _current(finding)
    if record is None or record.get("kind") == "revision":
        return None
    vendor, architect = record.get("vendor") or {}, record.get("architect") or {}
    if not (vendor.get("placed") and architect.get("placed")):
        return None
    return "right" if vendor.get("read") == architect.get("read") else "please_check"


def read_both(finding: Mapping[str, Any]) -> bool:
    """Code read both of an identifier item's values at their spots, each in its place, and they
    are the same text (`chat_trust.code_read_both`, for identifiers)."""
    return outcome(finding) == "right"


def held(finding: Mapping[str, Any]) -> tuple[str, str] | None:
    """Why an identifier item (status same or different) can be neither Right nor Wrong, as (why,
    what would settle it); `None` when code's record lets it be Right, or for any other item. A
    difference code read at both spots is a please-check (`likely_problems`), never red: its why
    is given here too, for the Not-compared reason under it."""
    name = kind(finding)
    if name is None or finding.get("status") not in ("same", "different"):
        return None
    what = _kind_words(name)
    record = _current(finding)
    settle = f"the {what} read by code, as printed text in its field, at both spots"
    for side in ("vendor", "architect"):
        found = (record or {}).get(side) or {}
        if record is not None and not found.get("placed"):
            # What code read at the spot comes first: a digit that is part of an address is no
            # revision number at all, whatever the rule for revision numbers.
            return f"{_side_words(side)} {what}: {found.get('why') or 'not read'}", settle
    if name == "revision":
        return OWN_NUMBERING, _OWN_SETTLE
    if record is None:
        unread = (
            f"a {what} is compared as printed text, never as a size; code has not read it at "
            "both spots"
        )
        return unread, settle
    vendor, architect = record["vendor"], record["architect"]
    if vendor.get("read") == architect.get("read"):
        if finding.get("status") == "same":
            return None
        return (
            f'code reads the same {what} at both spots ("{vendor.get("phrase")}"): no difference',
            settle,
        )
    differ = (
        f'code read {_side_words("vendor")} {what} "{vendor.get("phrase")}" and '
        f'{_side_words("architect")} "{architect.get("phrase")}" at their spots: they differ '
        "(compared as text, never as sizes)"
    )
    return differ, f'which {what} is right: "{vendor.get("phrase")}" or "{architect.get("phrase")}"'


def please_check_words(finding: Mapping[str, Any]) -> tuple[str, str] | None:
    """(why, the please-check's words) for an identifier whose text differs at both spots (`outcome`
    `please_check`), else `None`."""
    if outcome(finding) != "please_check":
        return None
    record = _current(finding) or {}
    vendor, architect = record.get("vendor") or {}, record.get("architect") or {}
    item = _flat(finding.get("item")) or "this item"
    what = _kind_words(str(record.get("kind")))
    why = (
        f'code read {_side_words("vendor")} {what} "{vendor.get("phrase")}" (page '
        f'{vendor.get("page")}) and {_side_words("architect")} "{architect.get("phrase")}" (page '
        f"{architect.get('page')}), both from the drawings' printed text: they differ"
    )
    from app.review.assistant.likely_problems import LETTER_LEAD

    words = (
        f'{LETTER_LEAD}: {item}: the vendor\'s drawing prints "{vendor.get("phrase")}"; the '
        f'architect\'s prints "{architect.get("phrase")}"; they differ.'
    )
    return why, words


# ---- never a size -------------------------------------------------------------------------------


#: The trust checks that read a side as a size: an identifier holds none of them.
_SIZE_CHECKS: Final[Mapping[str, Any]] = {
    "printed_architect": "not_a_number",
    "printed_vendor": "not_a_number",
    "verify_architect": None,
    "verify_vendor": None,
    "units": None,
}


def as_text(finding: Mapping[str, Any]) -> dict[str, Any]:
    """The finding with no size reading of an identifier (see the module): its `inches` empty, its
    checks holding no printed-size match, and its trust worked out again from them. Any other
    finding is returned as it is (a copy)."""
    out = dict(finding)
    if not identifier(finding):
        return out
    out["inches"] = {"architect": None, "vendor": None, "difference": None}
    checks = finding.get("checks")
    if isinstance(checks, Mapping):
        fixed = {**checks, **_SIZE_CHECKS}
        out["checks"] = fixed
        status = finding.get("status") if isinstance(finding.get("status"), str) else None
        out["trust_level"] = witness.trust_level(fixed, status)
        out["evidence_kind"] = witness.evidence_kind(fixed, status)
        out["witnesses"] = witness.witnesses(fixed, status)
    return out


def blocks_as_text(blocks: Sequence[Mapping[str, Any]] | None) -> list[dict[str, Any]] | None:
    """An answer's blocks with each identifier finding as text (`as_text`), on read: an answer
    stored before still never speaks of a sheet number in inches."""
    if blocks is None:
        return None
    out: list[dict[str, Any]] = []
    for block in blocks:
        finding = block.get("finding") if isinstance(block, Mapping) else None
        if isinstance(finding, Mapping) and identifier(finding):
            block = {**block, "finding": as_text(finding)}
        out.append(dict(block))
    return out
