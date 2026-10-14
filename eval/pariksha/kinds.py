"""Pariksha's piece-kind scorecard (Bala, #1250): the kind code names for each keyed piece.

**Why.** A countertop row's pieces are fillers, cabinets, panels or appliance spaces. Today the
product names a kind only from a word in the piece's own dimension label (`propose_kind`,
`extraction/slot_reader/kinds.py`), so almost every piece is "unknown". Any later way of naming
kinds (the cabinet tag printed on the face, the wall end, two AIs) must be measured first and
accepted only when it is **better with 0 new wrong** (`eval/pariksha/__init__.py`). This scores, at
$0, the kinds a stored run already holds.

**The key (`pariksha-key/1`, agent `bala-kinds`).** Local only (it holds client drawings' pieces).
One case per keyed piece:

    {"format": "pariksha-key/1", "agent": "bala-kinds", "name": "...", "version": "...",
     "cases": [{"family": "piece-kind", "page_sha256": "<64 hex>", "question_id": "p6:piece2",
                "pieces": 5, "row_widths": ["2", "91/8", null, "47/2", "2"],
                "answer": {"kinds": ["filler"]} | {"not_scored": "section"},
                "source": "..."}]}

* **`question_id`**: `p<page index>:piece<n>`, the piece's position from the left (the product's
  `slot:<n>`). **`pieces`**: how many pieces the keyed row has. A run that read another number of
  pieces on the page read another row, or split it another way: the case is "not asked", never
  scored against the wrong piece. (The product reads one countertop row per page.)
* **`row_widths`** (optional): the keyed row's piece widths in inches, exact (`"91/8"`), `null`
  where the key's text gives none; the same list on every case of the row. The same piece count is
  not enough to know the run read the keyed row: the run's piece spans must also be **in
  proportion** to these widths (each span within `ROW_FIT_PX` pixels or `ROW_FIT_SHARE` of the
  width times the row's pixels per inch, whichever is more). A row that does not fit, or whose key
  gives fewer than two widths, is **row unverified**: listed, never scored right or wrong.
* **`answer.kinds`**: the kinds a reader may name for it and be right: `filler`, `panel`,
  `cabinet`, `equal_cabinets`, `appliance_space`. The product has no word for a panel, so naming a
  panel a filler is **wrong** (the bar set by the cabinets design). `truth_kinds` turns the keys'
  own words into these. **`not_scored`**: the key's word names no kind (a section, a countertop
  segment, unknown): listed, never scored.

**The sources.** One scorecard scores one source; two sources of the same run, on the same key, are
compared case by case like two versions:

* **`product`**: the kind the run stored on each piece (`kind:` flag); a piece with none recorded
  is unknown.
* **`face-tag`**: a code rule measured, not shipped: a cabinet tag (`vocabulary/cabinet_codes`,
  finish codes refused), the word FILLER or an appliance word (DW, REF, RANGE) on the vendor's text
  layers (`extraction.stamp_text`, `rapidocr`, `pdfplumber`) whose whole box lies inside the piece's
  left-right span, its middle at most `FACE_TAG_REACH_PX` above the piece's top. Exactly one kind
  found names it; none or two kinds leave it unknown, naming both. GV's markup
  (`extraction.annotations`) is the answer key and never read.

**Outcomes and metrics.** Per keyed piece: **right** (a kind the key accepts), **wrong** (any other
kind), **unknown**, `not_scored`, `row_unverified` or `not_asked`. Over the run: `named_pieces`,
how many of all the pieces the run read the source names at all; `skipped_malformed`, the run's
piece candidates whose position or span flag could not be read (counted, never dropped silently;
the row's overall, `slot:overall`, is not a piece).

**Compare.** `compare_kinds`: the Pariksha compare, with right and wrong as its right and wrong. A
case that becomes `row_unverified` counts as no longer asked, which rejects. It compares two kinds
cards only: a card of another agent (or two different agents) is refused.

Source: issue #1250 · Verification: `tests/eval/test_pariksha_kinds.py`
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import DocumentVersion, Page
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from eval.metrics import MetricResult
from eval.pariksha.compare import Comparison
from eval.pariksha.keys import KEY_FORMAT, KeyFormatError
from eval.pariksha.score import (
    AGREED_RIGHT,
    AGREED_WRONG,
    NOT_ASKED,
    CaseScore,
    ReaderScore,
    Scorecard,
    _code_note,
    _now,
    _provenance,
    _rate,
)
from eval.pariksha.stored import StoredRunError, _choose_run, _read_only, _run_row
from eval.pariksha.walls import canonical_hash, compare_translated, require_agent
from eval.release_metrics import Direction
from vocabulary.cabinet_codes import is_cabinet_code, is_finish_code

__all__ = [
    "FACE_TAG",
    "FACE_TAG_REACH_PX",
    "KINDS",
    "KINDS_AGENT",
    "KINDS_FAMILY",
    "NOT_SCORED",
    "PRODUCT",
    "RIGHT",
    "ROW_FIT_PX",
    "ROW_FIT_SHARE",
    "ROW_UNVERIFIED",
    "SOURCES",
    "TEXT_LAYERS",
    "UNKNOWN",
    "WRONG",
    "KindCase",
    "KindTruth",
    "KindsKey",
    "StoredPiece",
    "StoredPiecesRun",
    "StoredWord",
    "compare_kinds",
    "dump_kinds_key",
    "face_tag_kind",
    "kinds_direction",
    "load_kinds_key",
    "load_stored_pieces",
    "read_kinds_key",
    "score_kinds",
    "truth_kinds",
]

KINDS_AGENT: Final = "bala-kinds"
KINDS_FAMILY: Final = "piece-kind"
#: The kinds a key may accept: the product's own (`PieceKind`, less unknown), and the panel, which
#: the product cannot name.
KINDS: Final = ("filler", "panel", "cabinet", "equal_cabinets", "appliance_space")
PRODUCT: Final = "product"
FACE_TAG: Final = "face-tag"
SOURCES: Final = (PRODUCT, FACE_TAG)
RIGHT: Final = "right"
WRONG: Final = "wrong"
UNKNOWN: Final = "unknown"
NOT_SCORED: Final = "not_scored"
#: The run read as many pieces as the key's row, but nothing shows it is the same row.
ROW_UNVERIFIED: Final = "row_unverified"
#: How far a piece's span may lie from the key's width times the row's pixels per inch, and still be
#: that piece: the larger of these pixels and this share of the expected span. The keyed rows of the
#: stored runs fit within 3 pixels; a row split another way is off by a whole piece.
ROW_FIT_PX: Final = 6
ROW_FIT_SHARE: Final = Fraction(1, 20)
#: The vendor's text layers the face-tag rule reads; GV's markup (`extraction.annotations`) never.
TEXT_LAYERS: Final = ("extraction.stamp_text", "rapidocr", "pdfplumber")
#: How far above a piece's top a tag's middle may stand: 2 inches of paper at the reader's 300 dpi.
FACE_TAG_REACH_PX: Final = 600
FACE_TAG_RULE: Final = (
    "face-tag-v1: a cabinet tag, FILLER, DW, REF or RANGE on the vendor's text layers, its whole "
    f"box inside the piece's span, its middle at most {FACE_TAG_REACH_PX} px (2 in at 300 dpi) "
    "above the piece's top; exactly one kind names it"
)

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_QUESTION: Final = re.compile(r"^p\d+:piece(\d+)$")
_HEADER: Final = frozenset({"format", "agent", "name", "version", "cases"})
_CASE: Final = frozenset(
    {"family", "page_sha256", "question_id", "pieces", "row_widths", "answer", "source"}
)
_WIDTH: Final = re.compile(r"^[1-9]\d*(?:/[1-9]\d*)?$")
_WORD: Final = re.compile(r"[a-z]+")
_CABINET_WORDS: Final = frozenset({"cabinet", "cabinets", "vanity"})
#: The kinds one key word may name together: a cabinet (of equal ones), or a filler or panel.
_TOGETHER: Final = (frozenset({"cabinet", "equal_cabinets"}), frozenset({"filler", "panel"}))
_TOKEN: Final = re.compile(r"[A-Za-z0-9]+(?:[-−][A-Za-z0-9]+)*")
_APPLIANCE_WORDS: Final = frozenset({"DW", "REF", "RANGE"})
_FORM_READER: Final = "extraction.form_reader"
_OVERALL: Final = "overall"

Box = tuple[int, int, int, int]


# --- the key ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class KindTruth:
    kinds: tuple[str, ...]
    """The kinds a reader may name and be right; empty when the piece is not scored."""
    not_scored: str | None = None
    """The key's own word, when it names no kind that is scored."""

    def __str__(self) -> str:
        return " or ".join(self.kinds) if self.kinds else f"not scored ({self.not_scored})"


def truth_kinds(word: str) -> KindTruth:
    """The kinds a key's own word for a piece accepts (the form keys' words, normalised).

    The words are read as a set, so their order never matters ("filler/panel" and "panel/filler"
    accept the same kinds). A word naming kinds the key cannot both mean ("cabinet / filler") is
    kept, not scored: it is never guessed."""
    lowered = word.strip().lower()
    if lowered in KINDS:
        return KindTruth(kinds=(lowered,))
    words = set(_WORD.findall(lowered))
    named: set[str] = set()
    if words & _CABINET_WORDS:
        named.add("cabinet")
        if "equal" in words:
            named.add("equal_cabinets")
    if "filler" in words or "fillers" in words:
        named.add("filler")
    if words & {"panel", "panels"} or {"end", "piece"} <= words:
        named.add("panel")
    if {"into", "wall"} <= words:
        named |= {"filler", "panel"}
    if "space" in words:
        named.add("appliance_space")
    if len(named) == 1 or any(named <= together for together in _TOGETHER if named):
        return KindTruth(kinds=tuple(kind for kind in KINDS if kind in named))
    return KindTruth(kinds=(), not_scored=word.strip() or "no kind given")


@dataclass(frozen=True, slots=True)
class KindCase:
    page_sha256: str
    question_id: str
    pieces: int
    truth: KindTruth
    source: str = ""
    row_widths: tuple[Fraction | None, ...] | None = None
    """The keyed row's piece widths in inches (`None` where the key gives none); `None` when the
    key gives no widths, so the run's row can never be verified."""

    @property
    def slot(self) -> int:
        found = _QUESTION.match(self.question_id)
        assert found is not None
        return int(found.group(1))

    @property
    def case_id(self) -> str:
        return f"{self.page_sha256[:16]}/{self.question_id}"


@dataclass(frozen=True, slots=True)
class KindsKey:
    name: str
    version: str
    cases: tuple[KindCase, ...]
    sha256: str


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KeyFormatError(f"{field} must be a non-empty string, not {value!r}")
    return value


def _answer(data: object, where: str) -> KindTruth:
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where}: answer must be an object")
    if set(data) == {"not_scored"}:
        return KindTruth(kinds=(), not_scored=_text(data["not_scored"], f"{where} not_scored"))
    if set(data) != {"kinds"}:
        raise KeyFormatError(
            f"{where}: an answer is {{'kinds': [...]}} or {{'not_scored': '<the key's word>'}}"
        )
    kinds = data["kinds"]
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(kind not in KINDS for kind in kinds)
        or len(set(kinds)) != len(kinds)
    ):
        raise KeyFormatError(f"{where}: kinds must be a non-empty list of {KINDS}, not {kinds!r}")
    return KindTruth(kinds=tuple(str(kind) for kind in kinds))


def _row_widths(value: object, pieces: int, where: str) -> tuple[Fraction | None, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or len(value) != pieces
        or not all(
            width is None or (isinstance(width, str) and _WIDTH.match(width)) for width in value
        )
    ):
        raise KeyFormatError(
            f"{where}: row_widths is a list of the row's {pieces} piece widths in inches, each "
            f"exact ('91/8') or null, not {value!r}"
        )
    return tuple(None if width is None else Fraction(width) for width in value)


def _case(data: object, index: int) -> KindCase:
    where = f"case {index + 1}"
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where} is not an object")
    extra = sorted(set(data) - _CASE)
    if extra:
        raise KeyFormatError(f"unknown field(s) in {where}: {', '.join(extra)}")
    if data.get("family") != KINDS_FAMILY:
        raise KeyFormatError(
            f"{where}: family must be {KINDS_FAMILY!r}, not {data.get('family')!r}"
        )
    page = data.get("page_sha256")
    if not isinstance(page, str) or not _SHA256.match(page):
        raise KeyFormatError(f"{where}: page_sha256 must be 64 lower-case hex characters")
    question_id = _text(data.get("question_id"), f"{where} question_id")
    found = _QUESTION.match(question_id)
    if found is None:
        raise KeyFormatError(f"{where}: question_id is p<page index>:piece<n>, not {question_id!r}")
    pieces = data.get("pieces")
    if isinstance(pieces, bool) or not isinstance(pieces, int) or pieces < 1:
        raise KeyFormatError(f"{where}: pieces must be a whole number from 1, not {pieces!r}")
    if int(found.group(1)) >= pieces:
        raise KeyFormatError(f"{where}: piece {found.group(1)} is not one of the row's {pieces}")
    source = data.get("source", "")
    if not isinstance(source, str):
        raise KeyFormatError(f"{where}: source must be text")
    return KindCase(
        page_sha256=page,
        question_id=question_id,
        pieces=pieces,
        truth=_answer(data.get("answer"), where),
        source=source,
        row_widths=_row_widths(data.get("row_widths"), pieces, where),
    )


def load_kinds_key(data: Mapping[str, object]) -> KindsKey:
    """Read a piece-kind key from its parsed JSON, refusing anything it does not know."""
    if not isinstance(data, Mapping):
        raise KeyFormatError("a key is one JSON object")
    if data.get("format") != KEY_FORMAT:
        raise KeyFormatError(f"format must be {KEY_FORMAT!r}, not {data.get('format')!r}")
    if data.get("agent") != KINDS_AGENT:
        raise KeyFormatError(f"agent must be {KINDS_AGENT!r}, not {data.get('agent')!r}")
    extra = sorted(set(data) - _HEADER)
    if extra:
        raise KeyFormatError(f"unknown field(s) in the key: {', '.join(extra)}")
    name = _text(data.get("name"), "name")
    version = _text(data.get("version"), "version")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise KeyFormatError("cases must be a non-empty list")
    cases = tuple(_case(entry, index) for index, entry in enumerate(raw_cases))
    seen: set[tuple[str, int]] = set()
    rows: dict[str, tuple[int, tuple[Fraction | None, ...] | None]] = {}
    for case in cases:
        if (case.page_sha256, case.slot) in seen:
            raise KeyFormatError(f"the key names piece {case.case_id} twice")
        seen.add((case.page_sha256, case.slot))
        row = rows.setdefault(case.page_sha256, (case.pieces, case.row_widths))
        if row != (case.pieces, case.row_widths):
            raise KeyFormatError(
                f"the cases of page {case.page_sha256[:12]} give its row different pieces or "
                "row_widths: one row per page has one list"
            )
    key = KindsKey(name=name, version=version, cases=cases, sha256="")
    return replace(key, sha256=canonical_hash(dump_kinds_key(key)))


def read_kinds_key(path: Path) -> KindsKey:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise KeyFormatError(f"{path.name} is not JSON: {error}") from error
    return load_kinds_key(data)


def dump_kinds_key(key: KindsKey) -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for case in key.cases:
        document: dict[str, object] = {
            "family": KINDS_FAMILY,
            "page_sha256": case.page_sha256,
            "question_id": case.question_id,
            "pieces": case.pieces,
            **(
                {}
                if case.row_widths is None
                else {
                    "row_widths": [
                        None if width is None else str(width) for width in case.row_widths
                    ]
                }
            ),
            "answer": (
                {"kinds": list(case.truth.kinds)}
                if case.truth.kinds
                else {"not_scored": case.truth.not_scored}
            ),
        }
        if case.source:
            document["source"] = case.source
        cases.append(document)
    return {
        "format": KEY_FORMAT,
        "agent": KINDS_AGENT,
        "name": key.name,
        "version": key.version,
        "cases": cases,
    }


# --- the stored run -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StoredWord:
    """Text on one of the vendor's text layers, with its box in the page picture's pixels."""

    text: str
    box: tuple[float, float, float, float]


@dataclass(frozen=True, slots=True)
class StoredPiece:
    page_sha256: str
    page_index: int
    slot: int
    box: Box
    """The piece's span (`slot-box:`), in the page picture's pixels."""
    kind: str | None
    """The kind the run stored (`kind:`), `None` when it recorded none."""


@dataclass(frozen=True, slots=True)
class StoredPiecesRun:
    database: str
    extraction_run_id: str
    extractor: str
    extractor_version: str
    config_hash: str
    document_sha256: str | None
    pieces: tuple[StoredPiece, ...]
    words: Mapping[str, tuple[StoredWord, ...]]
    """Page hash → the vendor's text-layer words on that page."""
    malformed: int = 0
    """The run's piece candidates whose position or span flag could not be read."""


def _flag(flags: Sequence[str], prefix: str) -> str | None:
    return next((flag.removeprefix(prefix) for flag in flags if flag.startswith(prefix)), None)


def _box(polygon: object) -> tuple[float, float, float, float] | None:
    if not isinstance(polygon, list) or not polygon:
        return None
    try:
        xs = [float(point[0]) for point in polygon]
        ys = [float(point[1]) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def load_stored_pieces(
    session: Session, *, database: str, extraction_run_id: UUID | None = None
) -> StoredPiecesRun:
    """Every piece one run read (its span and stored kind), and the vendor's words on its pages."""
    _read_only(session)
    found = session.execute(
        select(
            ObservationCandidate.extraction_run_id,
            ObservationCandidate.page_id,
            ObservationCandidate.ambiguity_flags,
            Page.index.label("page_index"),
            Page.content_hash,
            DocumentVersion.sha256.label("document_sha256"),
        )
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(DocumentVersion, DocumentVersion.id == ObservationCandidate.document_version_id)
        .where(
            ExtractionRun.extractor == _FORM_READER,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader"]),
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()
    pieces_found = []
    malformed: Counter[UUID] = Counter()
    for row in found:
        flags = [flag for flag in row.ambiguity_flags or () if isinstance(flag, str)]
        slot, box = _flag(flags, "slot:"), _flag(flags, "slot-box:")
        if slot == _OVERALL:
            continue  # the row's overall: not a piece
        if slot is None or not slot.isdigit() or box is None:
            malformed[row.extraction_run_id] += 1
            continue
        parts = box.split(",")
        if len(parts) != 4 or not all(part.lstrip("-").isdigit() for part in parts):
            malformed[row.extraction_run_id] += 1
            continue
        x0, y0, x1, y1 = (int(part) for part in parts)
        pieces_found.append((row, int(slot), (x0, y0, x1, y1), _flag(flags, "kind:")))
    counts: dict[UUID, int] = defaultdict(int)
    for row, *_rest in pieces_found:
        counts[row.extraction_run_id] += 1
    run_id = _choose_run(counts, extraction_run_id, "slot-reader pieces")
    run = _run_row(session, run_id)
    chosen = [entry for entry in pieces_found if entry[0].extraction_run_id == run_id]
    page_of: dict[UUID, str] = {}
    for row, *_rest in chosen:
        page_of[row.page_id] = row.content_hash
    if len(set(page_of.values())) != len(page_of):
        raise StoredRunError(
            f"run {run_id} read the same page (by content) twice; which reading the key means is "
            "not knowable"
        )
    words: dict[str, list[StoredWord]] = defaultdict(list)
    if page_of:
        for page_id, raw_text, polygon in session.execute(
            select(
                ObservationCandidate.page_id,
                ObservationCandidate.raw_text,
                ObservationCandidate.polygon,
            )
            .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
            .where(
                ExtractionRun.extractor.in_(TEXT_LAYERS),
                ObservationCandidate.page_id.in_(page_of),
            )
            .order_by(ObservationCandidate.created_at, ObservationCandidate.id)
        ):
            word_box = _box(polygon)
            if word_box is not None and raw_text:
                words[page_of[page_id]].append(StoredWord(text=raw_text, box=word_box))
    documents = sorted({row.document_sha256 for row, *_rest in chosen})
    return StoredPiecesRun(
        database=database,
        extraction_run_id=str(run_id),
        extractor=run.extractor,
        extractor_version=run.extractor_version,
        config_hash=run.config_hash,
        document_sha256=documents[0] if len(documents) == 1 else None,
        pieces=tuple(
            StoredPiece(
                page_sha256=row.content_hash,
                page_index=row.page_index,
                slot=slot,
                box=box,
                kind=kind,
            )
            for row, slot, box, kind in chosen
        ),
        words={page: tuple(found_words) for page, found_words in words.items()},
        malformed=malformed[run_id],
    )


# --- the kinds a source names -------------------------------------------------------------------


def _tag_kind(token: str) -> str | None:
    upper = token.upper()
    if token == upper and is_cabinet_code(token) and not is_finish_code(token):
        return "cabinet"
    if upper == "FILLER":
        return "filler"
    if upper in _APPLIANCE_WORDS:
        return "appliance_space"
    return None


def face_tag_kind(box: Box, words: Sequence[StoredWord]) -> tuple[str, str]:
    """The kind the printed face tags name for a piece, and why (`FACE_TAG_RULE`)."""
    x0, y0, x1, _y1 = box
    found: list[tuple[str, str]] = []
    for word in words:
        wx0, wy0, wx1, wy1 = word.box
        middle = (wy0 + wy1) / 2
        if not (wx0 >= x0 and wx1 <= x1 and y0 - FACE_TAG_REACH_PX <= middle <= y0):
            continue
        for token in _TOKEN.findall(word.text):
            kind = _tag_kind(token)
            if kind is not None:
                found.append((kind, token))
    kinds = {kind for kind, _token in found}
    if not kinds:
        return UNKNOWN, "no tag printed wholly inside the piece's span just above it"
    if len(kinds) > 1:
        named = "; ".join(sorted({f'{kind} "{token}"' for kind, token in found}))
        return UNKNOWN, f"the drawing names it two ways: {named}"
    printed = ", ".join(dict.fromkeys(f'"{token}"' for _kind, token in found))
    return next(iter(kinds)), f"printed {printed}"


def _named(piece: StoredPiece, run: StoredPiecesRun, source: str) -> tuple[str, str]:
    if source == PRODUCT:
        if piece.kind is None:
            return UNKNOWN, "no kind recorded on the piece"
        return piece.kind, f"stored kind:{piece.kind}"
    return face_tag_kind(piece.box, run.words.get(piece.page_sha256, ()))


# --- scoring ------------------------------------------------------------------------------------


def _not_asked(case: KindCase, note: str) -> CaseScore:
    return CaseScore(
        case_id=case.case_id,
        question=f"{KINDS_FAMILY}@{case.page_sha256[:12]}/{case.question_id}",
        expected=str(case.truth),
        outcome=NOT_ASKED,
        note=f"not asked: {note}",
    )


def _row_unfit(case: KindCase, on_page: Mapping[int, StoredPiece]) -> str | None:
    """Why the run's row cannot be shown to be the keyed row, or `None` when its spans fit.

    Exact arithmetic: the row's pixels per inch is its known spans over its known widths."""
    if case.row_widths is None:
        return "the key gives no widths for this row, so the same row cannot be verified"
    known = [
        (slot, on_page[slot], width)
        for slot, width in enumerate(case.row_widths)
        if width is not None and slot in on_page
    ]
    if len(known) < 2:
        return "the key gives fewer than two widths the run has pieces for: the row is unverified"
    spans: list[tuple[int, Fraction, Fraction]] = [
        (slot, Fraction(piece.box[2] - piece.box[0]), width) for slot, piece, width in known
    ]
    total_px = sum((span for _slot, span, _width in spans), Fraction(0))
    total_in = sum((width for _slot, _span, width in spans), Fraction(0))
    scale = total_px / total_in
    off = [
        slot
        for slot, span, width in spans
        if abs(span - width * scale) > max(Fraction(ROW_FIT_PX), width * scale * ROW_FIT_SHARE)
    ]
    if off:
        shown = ", ".join(f"piece {slot}" for slot in off)
        return (
            f"the run's piece spans are not in proportion to the key's widths ({shown}): the "
            "same piece count, but another row or another split"
        )
    return None


def score_kinds(
    key: KindsKey,
    run: StoredPiecesRun,
    *,
    source: str,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The kind one source names for each keyed piece: right, wrong or unknown."""
    if source not in SOURCES:
        raise ValueError(f"the kind source must be one of {SOURCES}, not {source!r}")
    _provenance(code_version)
    by_page: dict[str, dict[int, StoredPiece]] = defaultdict(dict)
    for piece in run.pieces:
        if piece.slot in by_page[piece.page_sha256]:
            raise StoredRunError(
                f"piece {piece.slot} of page {piece.page_sha256[:12]} is read twice in run "
                f"{run.extraction_run_id}; which one the key means is not knowable"
            )
        by_page[piece.page_sha256][piece.slot] = piece
    cases: list[CaseScore] = []
    outcomes: Counter[str] = Counter()
    keyed: set[tuple[str, int]] = set()
    unverified = 0
    for case in key.cases:
        on_page = by_page.get(case.page_sha256)
        if not on_page:
            cases.append(_not_asked(case, "no row read on this page in the run"))
            continue
        if len(on_page) != case.pieces:
            cases.append(
                _not_asked(
                    case,
                    f"the run read {len(on_page)} pieces on this page; the key has "
                    f"{case.pieces} (another row, or the row split another way)",
                )
            )
            continue
        unfit = _row_unfit(case, on_page)
        if unfit is not None:
            unverified += 1
            cases.append(
                CaseScore(
                    case_id=case.case_id,
                    question=f"{KINDS_FAMILY}@{case.page_sha256[:12]}/{case.question_id}",
                    expected=str(case.truth),
                    outcome=ROW_UNVERIFIED,
                    note=f"row unverified: {unfit}",
                )
            )
            continue
        found = on_page.get(case.slot)
        if found is None:
            cases.append(_not_asked(case, f"the run has no piece {case.slot} on this page"))
            continue
        piece = found
        keyed.add((piece.page_sha256, piece.slot))
        kind, evidence = _named(piece, run, source)
        if case.truth.not_scored is not None:
            outcome = NOT_SCORED
        elif kind == UNKNOWN:
            outcome = UNKNOWN
        else:
            outcome = RIGHT if kind in case.truth.kinds else WRONG
        outcomes[outcome] += 1
        cases.append(
            CaseScore(
                case_id=case.case_id,
                question=f"{KINDS_FAMILY}@{case.page_sha256[:12]}/{case.question_id}",
                expected=str(case.truth),
                outcome=outcome,
                readers=(ReaderScore(model_id=source, answer=kind, verdict=outcome),),
                note=evidence,
            )
        )
    asked = sum(outcomes.values())
    scored = asked - outcomes[NOT_SCORED]
    pieces = len(run.pieces)
    named = sum(1 for piece in run.pieces if _named(piece, run, source)[0] != UNKNOWN)
    metrics: dict[str, MetricResult] = {
        "asked": _rate("asked", asked, len(key.cases), "keyed pieces the run read"),
        "scored": _rate("scored", scored, asked, "the key names a kind for them"),
        RIGHT: _rate(RIGHT, outcomes[RIGHT], scored),
        WRONG: _rate(WRONG, outcomes[WRONG], scored),
        UNKNOWN: _rate(UNKNOWN, outcomes[UNKNOWN], scored),
        "named_pieces": _rate(
            "named_pieces", named, pieces, "every piece the run read, keyed or not"
        ),
        "unkeyed_pieces": _rate(
            "unkeyed_pieces",
            sum(1 for piece in run.pieces if (piece.page_sha256, piece.slot) not in keyed),
            pieces,
        ),
        "row_unverified": _rate(
            "row_unverified",
            unverified,
            len(key.cases),
            "the run read as many pieces, but their spans do not show the keyed row",
        ),
        "skipped_malformed": _rate(
            "skipped_malformed",
            run.malformed,
            pieces + run.malformed,
            "piece candidates whose position or span flag could not be read",
        ),
    }
    source_document = {"database": run.database, "extraction_run_id": run.extraction_run_id}
    if run.document_sha256 is not None:
        source_document["document_sha256"] = run.document_sha256
    return Scorecard(
        agent=KINDS_AGENT,
        key_name=key.name,
        key_version=key.version,
        key_sha256=key.sha256,
        code_version=code_version,
        scorer_version=scorer_version,
        extractor_versions={
            "agent": KINDS_AGENT,
            "kind_source": source,
            "rule": "the run's stored kind: flags" if source == PRODUCT else FACE_TAG_RULE,
            "prompt_ids": "none (code only)",
            "model_ids": "none (code only)",
            "extractor": f"{run.extractor}@{run.extractor_version}",
            "config_hash": run.config_hash,
            **_code_note(code_version),
        },
        source=source_document,
        metrics=metrics,
        cases=tuple(cases),
        created_at=created_at or _now(),
    )


# --- compare ------------------------------------------------------------------------------------

_KINDS_HIGHER: Final = ("asked", RIGHT)
_KINDS_NEITHER: Final = ("scored", "named_pieces", "unkeyed_pieces", "skipped_malformed")


def kinds_direction(name: str) -> Direction | None:
    if name in _KINDS_NEITHER:
        return None
    return Direction.MINIMUM if name in _KINDS_HIGHER else Direction.MAXIMUM


def compare_kinds(before: Scorecard, after: Scorecard) -> Comparison:
    """Better (more right, no more wrong), with 0 new wrong kinds.

    A case that becomes row-unverified is no longer asked: never a way to stop being wrong."""
    require_agent(before, after, KINDS_AGENT)
    return compare_translated(
        before,
        after,
        outcomes={RIGHT: AGREED_RIGHT, WRONG: AGREED_WRONG, ROW_UNVERIFIED: NOT_ASKED},
        direction=kinds_direction,
    )
