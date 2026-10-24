"""The tag reader (#1859, reading plan step 5): finish, material and product tags read and compared
by code, $0, no AI.

Tags follow fixed patterns: one to four letters, a dash, digits, an optional letter and suffix
(made-up `AB-012-CUST`). Code reads them at each side's spot:

- **text**: the drawing's own printed phrases at the spot (the set pack's `dims.json` phrases: black
  or grey text only, GV's coloured markup is never read);
- **strokes**: a tag drawn as pen strokes, re-typeset on one line (`ink_labels`, as the drawn
  numbers are) and read by two OCR engines told only the characters a tag is written with; both
  must read the same tags at every size that gives one. No number logic.

Code then compares the architect's tag with the vendor's (`decide`):

- **Right**: both read by code at their spots, the reference on the architect's drawing (or the
  brand prototype where it is the reference, R1), and exactly the same tags (`AB-02`, `AB-2` and
  `AB2` are one tag; a suffix is part of it: `AB-2-CUST` is not `AB-2`).
- **Wrong** only when code's finish check (`architect_items`) proved the same change on that area
  (the architect's tag, read from the drawing's text, and the vendor's only tag of that family):
  the item carries that proof.
- Otherwise **Not compared**, with the true reason: the tags differ ("confirm the vendor's code
  maps to the architect's"; two different tag systems stay a question), code reads another tag at
  the AI's spot, the reference is not the architect's drawing, or the vendor's words are drawn as
  strokes code cannot read yet. A note or model in words is never "Illegible": it is a note in
  words, compared by no code.

`decide` is pure; `read_spot` reads the text; `StrokeReader` reads the strokes (pdfium, OCR: loaded
by name, never imported by `app/api/`). Verification: `tests/review/assistant/test_tag_reader.py`.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import import_module
from pathlib import Path
from typing import Any, Final, Literal

__all__ = [
    "TAG_READ",
    "Decision",
    "SpotRead",
    "StrokeReader",
    "decide",
    "family",
    "is_tag_item",
    "read_spot",
    "tag_keys",
    "words_only",
]

_log = logging.getLogger(__name__)

#: The finding's key for what the tag reader read and decided.
TAG_READ: Final = "tag_read"
#: A tag as drawings print it: letters, a dash, digits, an optional letter, an optional suffix.
_TAG: Final = re.compile(
    r"(?<![A-Za-z0-9])([A-Z]{1,4}) ?- ?(\d{1,4})([A-Z]?)(?: ?- ?([A-Z]{2,8}))?(?![A-Za-z0-9])"
)
#: A tag written with no dash (`WD01`): only with two or more letters.
_TAG_NO_DASH: Final = re.compile(r"(?<![A-Za-z0-9])([A-Z]{2,4})(\d{1,4})([A-Z]?)(?![A-Za-z0-9])")
#: How far round a spot a printed phrase may sit and still be the spot's (points).
_SLACK_PT: Final = 2.0
#: Words that say the value could not be read (never a note in words).
_UNREAD: Final = re.compile(
    r"\b(?:unread\w*|illegible|unclear|blurr\w*|covered|cut\s+off)\b"
    r"|\bnot\s+(?:read|legible|readable|clear)\b|\bcan(?:no|')?t\s+(?:be\s+)?read\b",
    re.IGNORECASE,
)

type Box = tuple[float, float, float, float]
type Outcome = Literal["right", "wrong", "not_compared"]


def tag_keys(text: object) -> list[str]:
    """Every tag in `text`, spelled one way (`AB-012-CUST` is `AB-12-CUST`; `XY-01` and `XY01`
    are `XY-1`), in order, each once."""
    words = " ".join(str(text or "").upper().split())
    out: list[str] = []
    for found in _TAG.finditer(words):
        letters, number, letter, suffix = found.groups()
        key = f"{letters}-{int(number)}{letter}" + (f"-{suffix}" if suffix else "")
        if key not in out:
            out.append(key)
    if not out:
        for found in _TAG_NO_DASH.finditer(words):
            letters, number, letter = found.groups()
            key = f"{letters}-{int(number)}{letter}"
            if key not in out:
                out.append(key)
    return out


def family(key: str) -> str:
    """A tag's kind: its letters (`AB-2-CUST` is an `AB`)."""
    return key.split("-", 1)[0]


def _base(key: str) -> str:
    """The tag without its suffix (`AB-2-CUST` is `AB-2`), as the finish check keys them."""
    return "-".join(key.split("-")[:2])


def _size(text: object) -> bool:
    checks = import_module("app.review.assistant.finding_checks")
    claim = checks.claim_value(" ".join(str(text or "").split()))
    return claim is not None and checks.to_inches(claim) is not None


def words_only(text: object) -> bool:
    """A side that is words, not a size: letters, no size code reads in it, and no words that say
    it could not be read."""
    words = " ".join(str(text or "").split())
    return (
        bool(re.search(r"[A-Za-z]{2,}", words)) and not _UNREAD.search(words) and not _size(words)
    )


def is_tag_item(finding: Mapping[str, Any]) -> bool:
    """Both sides name tags and neither is a size: a tag comparison."""
    return all(
        words_only(finding.get(side)) and bool(tag_keys(finding.get(side)))
        for side in ("vendor", "architect")
    )


# ---- reading a spot ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpotRead:
    """What code read at one side's spot."""

    page: int
    box: Box
    tags: tuple[str, ...] = ()
    """The tags code read there (`tag_keys`)."""
    words: str = ""
    """The printed words there (text), or the strokes' reading."""
    route: str | None = None
    """`text`, `strokes`, or None (nothing read)."""
    role: str | None = None
    """Whose drawing the spot is on: architect, vendor, brand_prototype (the page map)."""
    ink: bool = False
    """Pen strokes are drawn there (words code could not read are strokes)."""
    why: str = ""
    """Why the strokes were not read (when not)."""

    def as_dict(self) -> dict[str, Any]:
        return {
            "page": self.page,
            "box": [round(v, 1) for v in self.box],
            "tags": list(self.tags),
            "words": self.words,
            "route": self.route,
            "role": self.role,
            "ink": self.ink,
            "why": self.why,
        }


def _meets(a: Sequence[float], b: Sequence[float], slack: float) -> bool:
    return (
        a[0] < b[2] + slack and b[0] < a[2] + slack and a[1] < b[3] + slack and b[1] < a[3] + slack
    )


def _role_at(
    page_map: Mapping[str, Any] | None, file: str | None, page: int, box: Box
) -> str | None:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    for entry in (page_map or {}).get("pages") or []:
        if not isinstance(entry, Mapping) or entry.get("page") != page:
            continue
        if file is not None and entry.get("file") not in (None, file):
            continue
        for half in entry.get("halves") or []:
            hb = half.get("box") if isinstance(half, Mapping) else None
            if (
                isinstance(hb, Sequence)
                and len(hb) == 4
                and hb[0] <= cx <= hb[2]
                and hb[1] <= cy <= hb[3]
            ):
                return str(half.get("role") or "") or None
    return None


#: (file, page, box) -> what the strokes there read: (tags, words, ink drawn, why not).
type StrokeRead = Callable[[str, int, Box], tuple[tuple[str, ...], str, bool, str]]


def read_spot(
    file: str,
    page: int,
    box: Box,
    phrases: Sequence[Mapping[str, Any]],
    *,
    page_map: Mapping[str, Any] | None = None,
    strokes: StrokeRead | None = None,
) -> SpotRead:
    """What code reads at one spot: the printed phrases there, else the strokes there."""
    role = _role_at(page_map, file, page, box)
    here = [
        p
        for p in phrases
        if isinstance(p.get("box"), Sequence)
        and len(p["box"]) == 4
        and _meets([float(v) for v in p["box"]], box, _SLACK_PT)
    ]
    if here:
        words = " ".join(" ".join(str(p.get("text") or "") for p in here).split())
        side = {str(p.get("side")) for p in here if p.get("side")}
        if role is None and len(side) == 1:
            role = next(iter(side))
        return SpotRead(page, box, tuple(tag_keys(words)), words, "text", role)
    if strokes is None:
        return SpotRead(page, box, role=role, why="the strokes were not read here")
    try:
        tags, words, ink, why = strokes(file, page, box)
    except Exception as failed:  # noqa: BLE001 - a reader that cannot run reads nothing
        return SpotRead(page, box, role=role, why=f"the strokes could not be read ({failed})")
    return SpotRead(
        page, box, tuple(tags), words, "strokes" if tags else None, role, ink=ink, why=why
    )


# ---- the strokes ---------------------------------------------------------------------------------


#: The characters a tag is written with: what Tesseract is told to look for.
_TAG_CHARS: Final = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-"


class _TagTesseract:
    """Tesseract told only a tag's characters, one line."""

    name = "Tesseract"

    def __init__(self) -> None:
        vv = import_module("app.review.assistant.verify_value")
        self._tesseract = vv.lenient_tesseract(import_module("pytesseract"))
        self._tesseract.get_tesseract_version()

    def __call__(self, picture: Any) -> str:
        config = "--psm 7 -c " + shlex.quote(f"tessedit_char_whitelist={_TAG_CHARS}")
        return " ".join(
            str(self._tesseract.image_to_string(picture, config=config, timeout=60)).split()
        )


@dataclass(slots=True)
class StrokeReader:
    """Tags drawn as pen strokes, read by two OCR engines that must agree (see the module)."""

    pdf_of: Callable[[str], Path | None]
    """The PDF a file's pages are read from (the set pack's markup-free copy)."""
    _engines: list[Any] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def engines(self) -> list[Any]:
        with self._lock:
            if self._engines is None:
                found: list[Any] = []
                try:
                    rapid = import_module("app.review.assistant.dim_labels").LineReader()
                    found.append(lambda picture: " ".join(rapid(picture, single_line=True)))
                except Exception as missing:  # noqa: BLE001 - an engine not installed
                    _log.warning("tag reader: RapidOCR is not available: %s", missing)
                try:
                    found.append(_TagTesseract())
                except Exception as missing:  # noqa: BLE001 - an engine not installed
                    _log.warning("tag reader: Tesseract is not available: %s", missing)
                self._engines = found
            return list(self._engines)

    def __call__(self, file: str, page: int, box: Box) -> tuple[tuple[str, ...], str, bool, str]:
        pdf = self.pdf_of(file)
        if pdf is None:
            return (), "", False, "the page's file is not here"
        numbers = import_module("app.review.assistant.read_number")
        ink_labels = import_module("app.review.assistant.ink_labels")
        parsed = numbers._PAGES.get(pdf, page)
        label = numbers.ink_label(parsed.page, box)
        if label is None:
            return (), "", False, "no pen strokes at the spot"
        engines = self.engines()
        if len(engines) < 2:
            return (), "", True, "the second OCR engine is not installed"
        agreed: set[tuple[str, ...]] = set()
        lines: list[str] = []
        for way in ink_labels.turns(label):
            for row in ink_labels.rows(label, way):
                for height in ink_labels.HEIGHTS_PX:
                    picture = ink_labels.row_picture(row, way, height, inch_mark=False, slash=True)
                    if picture is None:
                        continue
                    reads = [engine(picture).upper() for engine in engines]
                    lines += reads
                    keys = _agreed_keys(reads)
                    if keys:
                        agreed.add(keys)
        if len(agreed) == 1:
            tags = next(iter(agreed))
            return tags, ", ".join(tags), True, ""
        seen = ", ".join(sorted({line for line in lines if line})[:4]) or "nothing"
        why = (
            "the two OCR engines did not agree on one tag"
            if len(agreed) <= 1
            else "the strokes read as different tags at different sizes"
        )
        return (), "", True, f"{why} (read {seen})"


def _agreed_keys(reads: Sequence[str]) -> tuple[str, ...]:
    """The tags every engine's reading gives, or () when they differ. Two readings of the same
    characters with a word break in one only agree (`KOLHER K-2214` and `KOLHERK-2214`: the
    engine that ran the words together reads no tag in them), and the tags are the ones the
    reading with the break gives; any other difference is no agreement."""
    keys = [tuple(tag_keys(read)) for read in reads]
    if keys and keys[0] and all(k == keys[0] for k in keys):
        return keys[0]
    squash = import_module("app.review.assistant.pasted_views").squash
    found = {k for k in keys if k}
    if len(found) == 1 and len({squash(read) for read in reads}) == 1:
        return found.pop()
    return ()


def stroke_reader_for_pack(pack: Path | None, uploads: Path | None = None) -> StrokeReader:
    """A `StrokeReader` reading the set pack's markup-free copy (GV's markup and stickers hidden),
    else the upload."""
    numbers = import_module("app.review.assistant.read_number")
    return StrokeReader(numbers.reader_for_pack(pack, uploads).pdf_of)


# ---- deciding ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """The tag reader's outcome for one item."""

    outcome: Outcome
    reason: str | None = None
    """Not compared: the reason (`classification.REASONS`)."""
    why: str = ""
    settle: str = ""
    proof: Mapping[str, Any] | None = None
    """Wrong: the finish check's proof the item carries (`text_proof`)."""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"outcome": self.outcome, "why": self.why}
        if self.reason:
            out["reason"] = self.reason
        if self.settle:
            out["settle"] = self.settle
        return out


def _words(tags: Sequence[str]) -> str:
    return ", ".join(tags) if tags else "no tag"


def _short(text: object, limit: int = 60) -> str:
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def _unread(side: str, read: SpotRead, claim: str) -> Decision:
    """A side whose words code did not read at its spot."""
    if read.ink:
        return Decision(
            "not_compared",
            "stroke_words",
            f"words drawn as strokes: code could not read the {side}'s {_short(claim)} at its spot "
            f"on page {read.page}" + (f" ({read.why})" if read.why else ""),
            f"the {side}'s words read by code at page {read.page} (or a drawing with text)",
        )
    return Decision(
        "not_compared",
        "tag_unconfirmed",
        f"code finds no printed words at the {side}'s spot on page {read.page} (the AI read "
        f"{_short(claim)} there)",
        f"the {side}'s tag read by code at its spot",
    )


def _reference_ok(read: SpotRead, prototype_reference: bool) -> bool:
    return read.role == "architect" or (prototype_reference and read.role == "brand_prototype")


def decide(
    vendor_claim: object,
    architect_claim: object,
    vendor: SpotRead,
    architect: SpotRead | None,
    *,
    prototype_reference: bool = False,
    changes: Sequence[Mapping[str, Any]] = (),
) -> Decision:
    """Code's outcome for an item whose two sides name tags (see the module). `changes`: the finish
    check's proved "changed" findings (`architect_items`, `text_proof.kind == "changed"`)."""
    claimed_v, claimed_a = tag_keys(vendor_claim), tag_keys(architect_claim)
    if vendor.route is None:
        return _unread("vendor", vendor, str(vendor_claim))
    if not set(claimed_v) <= set(vendor.tags):
        return Decision(
            "not_compared",
            "tag_unconfirmed",
            f"the vendor's spot on page {vendor.page} prints {_short(vendor.words)} (read by code), "
            f"not the AI's {_words(claimed_v)}",
            "the vendor's tag read by code at the spot the item names",
        )
    if architect is None:
        return Decision(
            "not_compared",
            "tag_unconfirmed",
            f"the architect's {_words(claimed_a)} has no spot on the drawing for code to read",
            "the architect's tag read by code at its spot",
        )
    if not _reference_ok(architect, prototype_reference):
        whose = {"vendor": "the vendor's own", "brand_prototype": "the brand prototype's"}.get(
            str(architect.role), "no architect"
        )
        return Decision(
            "not_compared",
            "tag_unconfirmed",
            f"the reference {_words(claimed_a)} is on {whose} drawing (page {architect.page}), "
            "not the architect's: tags are compared with the architect only",
            "the architect's tag for the same piece",
        )
    if architect.route is None:
        return _unread("architect", architect, str(architect_claim))
    if not set(claimed_a) <= set(architect.tags):
        return Decision(
            "not_compared",
            "tag_unconfirmed",
            f"the architect's spot on page {architect.page} prints {_short(architect.words)} (read "
            f"by code), not the AI's {_words(claimed_a)}",
            "the architect's tag read by code at the spot the item names",
        )
    if set(claimed_v) == set(claimed_a):
        return Decision(
            "right",
            why=f"code read {_words(claimed_v)} at the vendor's spot (page {vendor.page}) and at "
            f"the architect's (page {architect.page}): the same tag",
        )
    families_v = {family(k) for k in claimed_v}
    families_a = {family(k) for k in claimed_a}
    if not families_v & families_a:
        return Decision(
            "not_compared",
            "tags_differ",
            f"tags differ: the architect's {_words(claimed_a)}, the vendor's {_words(claimed_v)} "
            "(the two use different tag systems)",
            f"a printed legend, or the vendor's confirmation, that {_words(claimed_v)} is the "
            f"architect's {_words(claimed_a)}",
        )
    pages = {vendor.page, architect.page}
    for change in changes:
        proof = change.get("text_proof") if isinstance(change, Mapping) else None
        if not isinstance(proof, Mapping) or proof.get("kind") != "changed":
            continue
        theirs = {_base(k) for code in proof.get("vendor_codes") or [] for k in tag_keys(code)}
        on = {change.get("page"), change.get("vs_page")} - {None}
        if (
            {_base(k) for k in claimed_a} == {str(proof.get("code"))}
            and {_base(k) for k in claimed_v} == theirs
            and set(claimed_v)
            == {k for code in proof.get("vendor_codes") or [] for k in tag_keys(code)}
            and on & pages
        ):
            return Decision(
                "wrong",
                why=f"code read the architect's {_words(claimed_a)} and the vendor's "
                f"{_words(claimed_v)} at their spots; code's finish check proved the vendor tags "
                "this family with another code on every sheet of the area",
                proof=dict(proof),
            )
    return Decision(
        "not_compared",
        "tags_differ",
        f"tags differ: the architect's {_words(claimed_a)}, the vendor's {_words(claimed_v)} "
        "(both read by code): confirm the vendor's code maps to the architect's",
        f"the vendor's confirmation (or a printed legend) that {_words(claimed_v)} is the "
        f"architect's {_words(claimed_a)}",
    )


def decide_words(vendor_claim: object, architect_claim: object, vendor: SpotRead) -> Decision:
    """An item whose vendor side is a note or model in words, not a tag comparison: never
    "Illegible". Code says what it read at the vendor's spot; words are compared by no code."""
    if vendor.route is None and vendor.ink:
        return _unread("vendor", vendor, str(vendor_claim))
    printed = (
        f'the vendor\'s spot on page {vendor.page} prints "{_short(vendor.words)}"'
        if vendor.route == "text"
        else f"code finds no printed words at the vendor's spot on page {vendor.page}"
    )
    return Decision(
        "not_compared",
        "words",
        f'a note in words, not a size: vendor "{_short(vendor_claim)}", architect '
        f'"{_short(architect_claim)}"; {printed}; code compares sizes and tags, not notes',
        "a person reads the two notes (or a tag code can compare)",
    )
