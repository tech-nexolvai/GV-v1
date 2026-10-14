"""Pariksha's depth and view-kind scorecards (phase 5, #1257): `rekha-view` and `akshara-depth`.

**Why.** Phase 5 reads values the width reader never reads: the kind of each view (plan, section or
side, elevation), the countertop's depth (CT010), the sink cut-out's width and front-to-back size
(CT012, CT008) and the strip in front of it (CT007). Before any reader of these ships it must be
measured, and a change to it accepted only when it is **better with 0 new agreed-wrong**, like every
Pariksha scorecard (`eval/pariksha/__init__.py`). The reader does not exist yet (part 3), so this
scores reader answers written in a small file (`pariksha-depth-answers/1`, below); reading them from
a stored run waits for the reader.

**The key (`pariksha-key/1`, agents `rekha-view` and `akshara-depth`).** Local only, like every key:
it holds what client drawings print. The agents and their families are registered in
`eval/pariksha/keys.py` (`DEPTH_FAMILIES`), whose loader refuses these keys and points here.

    {"format": "pariksha-key/1", "agent": "akshara-depth", "name": "...", "version": "...",
     "status": "...", "note": "...", "documents": {"<label>": {"pdf": "...", "sha256": "<64 hex>"}},
     "cases": [{"family": "depth-span", "page_sha256": "<64 hex>",
                "question_id": "p6:plan:CT010:<label>", "set": "...", "page": 7, "view": "plan",
                "field": "CT010" | "CT012+CT008" | "CT007", "statement_id": 4,
                "confidence": "high" | "medium" | "low" | "n/a",
                "status": "claude-double-read" | "claude-single-read" | "open",
                "open_reason": "<only, and always, on an open case>",
                "answer": ..., "source": "..."}]}

* **`answer`**, by field: CT010 `{"texts": ['25 1/2"', ...]}` (the printed texts the value may be);
  CT012+CT008 `{"CT012": {"texts": [...]}, "CT008": {"texts": [...]}, "outline": "..."}` (one case,
  two values); CT007 `{"texts": [...], "start_edge": "..."}`. Any field may instead expect a
  **hold**: `{"held": "<reason>"}` or `{"hold": "<reason>", "start_edge": "..."}` (two sink
  outlines and no word naming the hole; a front offset printed from the stone's edge). An open
  case's answer is kept as written and never read.
* **Values are exact inches.** Each text is read by the width reader's own rule
  (`extraction.slot_reader.labels.plain_dimension`, after `seal.normalise_text`): `25 1/2"` is 51/2,
  and `648 [25 1/2]` is 51/2 (the bracketed inch wins). A text with no inch value (`648 mm`, a bare
  `25`) is refused: millimetres are never a value (Q12).
* **`rekha-view`** cases: `{"family": "view-kind", "question_id": "p3:view@x0,y0,x1,y1",
  "region_pt": [x0, y0, x1, y1], "heading_printed": "...", "heading_agrees": true,
  "answer": {"kind": "plan" | "section" | "side" | "elevation" | "detail" | "other"}, ...}`. The
  reader's question has one answer for a section and a side view (`view-kind-v1`: SECTION/SIDE),
  so both are scored as `section_or_side`.
* **`status`** says who read the key, and how it is used:
  * `claude-double-read`: Claude read it twice (the second time blind) and no check objected.
    **Only these are scored** into the outcomes the compare judges.
  * `claude-single-read`: read once. Scored apart (`key_single_read`, with the pair's outcome in
    the case's note and the `single_read_*` counts): never used to accept or reject.
  * `open`: not settled (with `open_reason`). Listed with the reason, never scored.

Every scorecard carries the key's provenance, **"key read by Claude twice, not by a person"**
(`KEY_PROVENANCE`): in its source and in what the script prints.

**The answers file (`pariksha-depth-answers/1`).** What the reader (part 3) will write, one entry
per value asked, naming it by the key's own identity (page hash, question id, field):

    {"format": "pariksha-depth-answers/1", "agent": "akshara-depth", "run": "<which run>",
     "prompt_ids": ["..."],
     "answers": [{"page_sha256": "<64 hex>", "question_id": "p6:plan:CT010:<label>",
                  "field": "CT010" | "CT012" | "CT008" | "CT007" | "view",
                  "readers": [{"model_id": "...", "value": '25 1/2"'},
                              {"model_id": "...", "unsure": true}]}]}

Each reader says exactly one of: `value` (a printed text, `akshara-depth`), `kind` (`rekha-view`:
plan, section_or_side, section, side, elevation, detail, other), `unsure: true`, `held` (the reason
it would not give a value) or `invalid` (it has no accepted answer, and why). `start_edge` and
`outline` may go with a value; they are shown, never scored. A value that is not an exact inch value
(`648 mm`) is no usable answer: it scores `invalid`. The pair is exactly two readers, the same two in
every entry; one value answered twice is refused.

**What is scored, per value.** The pair: **agreed-right** (both give the same exact value, or the
same kind, and the key accepts it), **agreed-wrong** (both give the same one and the key does not,
including a value where the key expects a hold: the dangerous direction), **disagreed** (two
different values), **held-as-key** (both hold where the key expects a hold), **held** (a reader holds
with a reason), **one-unsure**, **both-unsure** and **no-answer** (a reader has no usable answer).
Each reader: right (a hold where the key holds is right), wrong, held, unsure or invalid. Metrics
per field (`<metric>:CT010`, ...) and in total, over the double-read values asked; a key value the
file does not answer is `not_asked`, and an answer the key does not name is `unkeyed_answers`.

**Compare.** `compare_depth`: the Pariksha compare, with agreed-right and held-as-key as its right
outcomes and agreed-wrong as its wrong one, so a seeded agreed-wrong rejects. Single-read and open
cases keep one outcome on every card, so they never move a verdict. It compares two cards of the
same one of these agents only; the other compares refuse these cards.

Source: issue #1257 · Verification: `tests/eval/test_pariksha_depth.py`,
`tests/scripts/test_pariksha_depth_scripts.py`
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Final

from eval.metrics import MetricResult
from eval.pariksha.compare import Comparison
from eval.pariksha.keys import DEPTH_FAMILIES, KEY_FORMAT, KeyFormatError, QuestionIdentity

# The helpers every Pariksha scorecard shares, so these cards rate, date and refuse an
# unattributable run exactly as the others do.
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
from eval.pariksha.stored import StoredRunError
from eval.pariksha.walls import compare_translated
from eval.regression import IncomparableRuns
from eval.release_metrics import Direction
from extraction.slot_reader.labels import plain_dimension
from extraction.slot_reader.seal import normalise_text
from units.measurement import Unit
from vocabulary.semantic_types import SemanticType

__all__ = [
    "AGREED_RIGHT",
    "AGREED_WRONG",
    "ANSWERS_FORMAT",
    "BOTH_UNSURE",
    "DEPTH_AGENT",
    "DEPTH_AGENTS",
    "DISAGREED",
    "DOUBLE_READ",
    "FIELDS",
    "HELD",
    "HELD_AS_KEY",
    "KEY_OPEN",
    "KEY_PROVENANCE",
    "KEY_SINGLE_READ",
    "NOT_ASKED",
    "NO_ANSWER",
    "ONE_UNSURE",
    "OPEN",
    "SINGLE_READ",
    "STATUSES",
    "VIEW_AGENT",
    "DepthAnswers",
    "DepthAnswersError",
    "DepthKey",
    "DepthValue",
    "HoldTruth",
    "KindTruth",
    "Reading",
    "ValueTruth",
    "compare_depth",
    "depth_direction",
    "describe_key",
    "inches",
    "key_counts",
    "load_depth_answers",
    "load_depth_key",
    "read_depth_answers",
    "read_depth_key",
    "score_depth",
]

VIEW_AGENT: Final = "rekha-view"
DEPTH_AGENT: Final = "akshara-depth"
DEPTH_AGENTS: Final = tuple(DEPTH_FAMILIES)
ANSWERS_FORMAT: Final = "pariksha-depth-answers/1"
#: Every scorecard on these keys says who read the key (Anant's decision, 2026-10-14 night).
KEY_PROVENANCE: Final = "key read by Claude twice, not by a person"

DOUBLE_READ: Final = "claude-double-read"
SINGLE_READ: Final = "claude-single-read"
OPEN: Final = "open"
STATUSES: Final = (DOUBLE_READ, SINGLE_READ, OPEN)

#: The field codes, from the shared vocabulary (never written out as text here).
CT010: Final = SemanticType.CT010.value
CT012: Final = SemanticType.CT012.value
CT008: Final = SemanticType.CT008.value
CT007: Final = SemanticType.CT007.value
#: The key's one case for the cut-out's two values.
CUTOUT: Final = f"{CT012}+{CT008}"
#: The values each agent's answers name; a CT012+CT008 key case is two of them.
FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    VIEW_AGENT: ("view",),
    DEPTH_AGENT: (CT010, CT012, CT008, CT007),
}
_KEY_FIELDS: Final[Mapping[str, tuple[str, ...]]] = {
    CT010: (CT010,),
    CUTOUT: (CT012, CT008),
    CT007: (CT007,),
}
#: A key's view kinds and the class the reader's question answers with (`view-kind-v1` has one
#: answer, SECTION/SIDE, for both).
_KIND_CLASS: Final[Mapping[str, str]] = {
    "plan": "plan",
    "section": "section_or_side",
    "side": "section_or_side",
    "section_or_side": "section_or_side",
    "elevation": "elevation",
    "detail": "detail",
    "other": "other",
}
_VIEWS: Final = ("plan", "section", "side", "elevation", "detail", "other")
_CONFIDENCE: Final = ("high", "medium", "low", "n/a")

DISAGREED: Final = "disagreed"
ONE_UNSURE: Final = "one_unsure"
BOTH_UNSURE: Final = "both_unsure"
HELD: Final = "held"
HELD_AS_KEY: Final = "held_as_key"
NO_ANSWER: Final = "no_answer"
KEY_SINGLE_READ: Final = "key_single_read"
KEY_OPEN: Final = "key_open"
_PAIR_OUTCOMES: Final = (
    AGREED_RIGHT,
    AGREED_WRONG,
    DISAGREED,
    HELD_AS_KEY,
    HELD,
    ONE_UNSURE,
    BOTH_UNSURE,
    NO_ANSWER,
)

RIGHT: Final = "right"
WRONG: Final = "wrong"
UNSURE: Final = "unsure"
INVALID: Final = "invalid"
NOT_SCORED: Final = "not_scored"
_READER_VERDICTS: Final = (RIGHT, WRONG, HELD, UNSURE, INVALID)

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_QUESTION: Final = re.compile(r"^p\d+:\S+$")
_VIEW_QUESTION: Final = re.compile(r"^p\d+:view@\d+,\d+,\d+,\d+$")
_HEADER: Final = frozenset(
    {"format", "agent", "name", "version", "status", "note", "documents", "cases"}
)
_COMMON: Final = frozenset(
    {
        "family",
        "page_sha256",
        "question_id",
        "set",
        "page",
        "statement_id",
        "status",
        "open_reason",
        "source",
        "answer",
    }
)
_DEPTH_CASE: Final = _COMMON | {"view", "field", "confidence"}
_VIEW_CASE: Final = _COMMON | {"region_pt", "heading_printed", "heading_agrees"}
_ANSWERS_HEADER: Final = frozenset({"format", "agent", "run", "prompt_ids", "answers"})
_ENTRY: Final = frozenset({"page_sha256", "question_id", "field", "readers"})
_SAYS: Final = ("value", "kind", "unsure", "held", "invalid")
_READER: Final = frozenset({"model_id", *_SAYS, "start_edge", "outline"})
_LISTED: Final = 5


class DepthAnswersError(StoredRunError):
    """The answers file is not a `pariksha-depth-answers/1` file for this key; the message says why."""


# --- the key --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValueTruth:
    """The exact inch values a value may be (from the printed texts the key accepts)."""

    values: tuple[Fraction, ...]
    texts: tuple[str, ...]
    note: str = ""

    def __str__(self) -> str:
        shown = " | ".join(self.texts)
        return f"{shown} ({self.note})" if self.note else shown


@dataclass(frozen=True, slots=True)
class HoldTruth:
    """The key expects a hold: no value may be given, for this reason."""

    reason: str
    start_edge: str | None = None

    def __str__(self) -> str:
        return f"hold: {self.reason}"


@dataclass(frozen=True, slots=True)
class KindTruth:
    kind: str
    """The kind class (`plan`, `section_or_side`, `elevation`, `detail`, `other`)."""
    printed: str = ""
    """The heading the vendor printed under the view (shown; a heading can be wrong)."""

    def __str__(self) -> str:
        return f"{self.kind} (heading {self.printed!r})" if self.printed else self.kind


Truth = ValueTruth | HoldTruth | KindTruth


@dataclass(frozen=True, slots=True)
class DepthValue:
    """One scored value: a key case's field."""

    question: QuestionIdentity
    field: str
    status: str
    truth: Truth | None
    """`None` on an open case: it is never read."""
    open_reason: str = ""
    where: str = ""
    """The set, page and view, for a person."""

    @property
    def case_id(self) -> str:
        return f"{self.question.case_id}#{self.field}"


@dataclass(frozen=True, slots=True)
class DepthKey:
    agent: str
    name: str
    version: str
    values: tuple[DepthValue, ...]
    sha256: str


def canonical_hash(document: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def inches(text: str) -> Fraction | None:
    """The exact inches a printed text says, or `None` (no inch value: mm, a bare number, words)."""
    measured = plain_dimension(normalise_text(text), allow_explicit_mm=True)
    if measured is None or measured.unit is not Unit.INCH:
        return None
    return measured.exact


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KeyFormatError(f"{field} must be a non-empty string, not {value!r}")
    return value


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - known)
    if extra:
        raise KeyFormatError(f"unknown field(s) in {where}: {', '.join(extra)}")


def _value_truth(answer: object, where: str, note: str = "") -> ValueTruth:
    if not isinstance(answer, Mapping) or set(answer) != {"texts"}:
        raise KeyFormatError(f"{where}: a value is {{'texts': [...]}}, not {answer!r}")
    texts = answer["texts"]
    if not isinstance(texts, list) or not texts:
        raise KeyFormatError(f"{where}: texts must be a non-empty list of printed texts")
    values: list[Fraction] = []
    shown: list[str] = []
    for text in texts:
        value = inches(_text(text, f"{where} texts"))
        if value is None:
            raise KeyFormatError(
                f"{where}: {text!r} is not an exact inch value (inches are authoritative; "
                "millimetres are never a value)"
            )
        if value not in values:
            values.append(value)
            shown.append(normalise_text(text))
    return ValueTruth(values=tuple(values), texts=tuple(shown), note=note)


def _hold(answer: Mapping[str, object], where: str) -> HoldTruth | None:
    """`{"held": reason}` or `{"hold": reason, "start_edge": ...}`; `None` when it is no hold."""
    word = "held" if "held" in answer else "hold" if "hold" in answer else None
    if word is None:
        return None
    _unknown(answer, frozenset({word, "start_edge"}), f"{where} answer")
    edge = answer.get("start_edge")
    return HoldTruth(
        reason=_text(answer[word], f"{where} {word}"),
        start_edge=None if edge is None else _text(edge, f"{where} start_edge"),
    )


def _depth_truths(field: str, answer: Mapping[str, object], where: str) -> dict[str, Truth]:
    hold = _hold(answer, where)
    if hold is not None:
        return dict.fromkeys(_KEY_FIELDS[field], hold)
    if field == CUTOUT:
        if set(answer) - {"outline"} != {CT012, CT008}:
            raise KeyFormatError(
                f"{where}: a CT012+CT008 answer gives both CT012 and CT008 ({{'texts': [...]}} "
                f"each, and the outline) or a hold, not {sorted(answer)}"
            )
        outline = answer.get("outline")
        note = "" if outline is None else f"outline: {_text(outline, f'{where} outline')}"
        return {
            name: _value_truth(answer[name], f"{where} {name}", note) for name in _KEY_FIELDS[field]
        }
    if field == CT007:
        _unknown(answer, frozenset({"texts", "start_edge"}), f"{where} answer")
        edge = answer.get("start_edge")
        note = "" if edge is None else f"from: {_text(edge, f'{where} start_edge')}"
        texts = {"texts": answer.get("texts")}
        return {CT007: _value_truth(texts, f"{where} {CT007}", note)}
    if set(answer) != {"texts"}:
        raise KeyFormatError(f"{where}: a CT010 answer is {{'texts': [...]}} or a hold")
    return {CT010: _value_truth(answer, f"{where} {CT010}")}


def _int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise KeyFormatError(f"{field} must be a whole number, not {value!r}")
    return value


def _case(agent: str, data: object, index: int) -> list[DepthValue]:
    where = f"case {index + 1}"
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where} is not an object")
    _unknown(data, _VIEW_CASE if agent == VIEW_AGENT else _DEPTH_CASE, where)
    family = _text(data.get("family"), f"{where} family")
    if family != DEPTH_FAMILIES[agent]:
        raise KeyFormatError(
            f"{where}: family {family!r} is not {agent}'s ({DEPTH_FAMILIES[agent]!r})"
        )
    page_sha = data.get("page_sha256")
    if not isinstance(page_sha, str) or not _SHA256.match(page_sha):
        raise KeyFormatError(f"{where}: page_sha256 must be 64 lower-case hex characters")
    question_id = _text(data.get("question_id"), f"{where} question_id")
    pattern = _VIEW_QUESTION if agent == VIEW_AGENT else _QUESTION
    if not pattern.match(question_id):
        raise KeyFormatError(f"{where}: question_id {question_id!r} is not this agent's shape")
    page = _int(data.get("page"), f"{where} page")
    _int(data.get("statement_id"), f"{where} statement_id")
    _text(data.get("set"), f"{where} set")
    _text(data.get("source"), f"{where} source")
    status = data.get("status")
    if status not in STATUSES:
        raise KeyFormatError(f"{where}: status must be one of {STATUSES}, not {status!r}")
    reason = data.get("open_reason")
    if (status == OPEN) != (reason is not None):
        raise KeyFormatError(f"{where}: an open case, and only an open case, has an open_reason")
    answer = data.get("answer")
    if not isinstance(answer, Mapping):
        raise KeyFormatError(f"{where}: answer must be an object")
    identity = QuestionIdentity(family=family, page_sha256=page_sha, question_id=question_id)
    truths: dict[str, Truth | None]
    if agent == VIEW_AGENT:
        region = data.get("region_pt")
        if region is not None and (
            not isinstance(region, list)
            or len(region) != 4
            or not all(isinstance(n, int | float) and not isinstance(n, bool) for n in region)
        ):
            raise KeyFormatError(f"{where}: region_pt is four numbers, not {region!r}")
        printed = data.get("heading_printed", "")
        if not isinstance(printed, str):
            raise KeyFormatError(f"{where}: heading_printed must be text")
        if not isinstance(data.get("heading_agrees", True), bool):
            raise KeyFormatError(f"{where}: heading_agrees must be true or false")
        shown = f"{data['set']} p{page}"
        if status == OPEN:
            truths = {"view": None}
        else:
            if set(answer) != {"kind"} or answer["kind"] not in _VIEWS:
                raise KeyFormatError(
                    f"{where}: a view answer is {{'kind': one of {_VIEWS}}}, not {answer!r}"
                )
            truths = {"view": KindTruth(kind=_KIND_CLASS[str(answer["kind"])], printed=printed)}
    else:
        field = data.get("field")
        if field not in _KEY_FIELDS:
            raise KeyFormatError(
                f"{where}: field must be one of {tuple(_KEY_FIELDS)}, not {field!r}"
            )
        view = data.get("view")
        if view not in _VIEWS:
            raise KeyFormatError(f"{where}: view must be one of {_VIEWS}, not {view!r}")
        if data.get("confidence") not in _CONFIDENCE:
            raise KeyFormatError(f"{where}: confidence must be one of {_CONFIDENCE}")
        shown = f"{data['set']} p{page} {view}"
        if status == OPEN:
            truths = dict.fromkeys(_KEY_FIELDS[str(field)])
        else:
            truths = dict(_depth_truths(str(field), answer, where))
    return [
        DepthValue(
            question=identity,
            field=name,
            status=str(status),
            truth=truth,
            open_reason="" if reason is None else _text(reason, f"{where} open_reason"),
            where=shown,
        )
        for name, truth in truths.items()
    ]


def load_depth_key(data: Mapping[str, object]) -> DepthKey:
    """Read a `rekha-view` or `akshara-depth` key, refusing anything it does not know."""
    if not isinstance(data, Mapping):
        raise KeyFormatError("a key is one JSON object")
    if data.get("format") != KEY_FORMAT:
        raise KeyFormatError(f"format must be {KEY_FORMAT!r}, not {data.get('format')!r}")
    agent = data.get("agent")
    if agent not in DEPTH_FAMILIES:
        raise KeyFormatError(f"agent must be one of {DEPTH_AGENTS}, not {agent!r}")
    _unknown(data, _HEADER, "the key")
    name = _text(data.get("name"), "name")
    version = _text(data.get("version"), "version")
    for field in ("status", "note"):
        if field in data and not isinstance(data[field], str):
            raise KeyFormatError(f"{field} must be text")
    documents = data.get("documents", {})
    if not isinstance(documents, Mapping) or not all(
        isinstance(entry, Mapping)
        and isinstance(entry.get("sha256"), str)
        and _SHA256.match(str(entry["sha256"]))
        for entry in documents.values()
    ):
        raise KeyFormatError("documents maps a label to {'pdf': ..., 'sha256': '<64 hex>'}")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise KeyFormatError("cases must be a non-empty list")
    values: list[DepthValue] = []
    seen: set[QuestionIdentity] = set()
    for index, case in enumerate(raw_cases):
        read = _case(str(agent), case, index)
        if read[0].question in seen:
            raise KeyFormatError(f"the key names question {read[0].question} twice")
        seen.add(read[0].question)
        values += read
    return DepthKey(
        agent=str(agent),
        name=name,
        version=version,
        values=tuple(values),
        # The key as written: a scorecard's key hash names exactly the answers it was scored on.
        sha256=canonical_hash(data),
    )


def read_depth_key(path: Path) -> DepthKey:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise KeyFormatError(f"{path.name} is not JSON: {error}") from error
    return load_depth_key(data)


def key_counts(key: DepthKey) -> Counter[tuple[str, str]]:
    """How many values the key has per (status, field)."""
    return Counter((value.status, value.field) for value in key.values)


def describe_key(key: DepthKey) -> str:
    """The key's provenance and its values per status and field, for a person."""
    counts = key_counts(key)
    lines = [f"  key: {KEY_PROVENANCE}", f"  key values ({len(key.values)}):"]
    for status in STATUSES:
        per_field = [
            f"{field} {counts[(status, field)]}"
            for field in FIELDS[key.agent]
            if counts[(status, field)]
        ]
        total = sum(counts[(status, field)] for field in FIELDS[key.agent])
        lines.append(f"    {status}: {total}" + (f" ({', '.join(per_field)})" if per_field else ""))
    holds = sum(isinstance(value.truth, HoldTruth) for value in key.values)
    if holds:
        lines.append(
            f"    of which the key expects a hold (a value there is agreed-wrong): {holds}"
        )
    opened = [value for value in key.values if value.status == OPEN]
    for value in opened:
        lines.append(f"    open {value.where} {value.field}: {value.open_reason}")
    return "\n".join(lines)


# --- the answers file -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """One reader's answer about one value."""

    model_id: str
    says: str
    """`value`, `kind`, `unsure`, `held` or `invalid`."""
    text: str = ""
    """The value's printed text, the kind, or the hold's or the invalid answer's reason."""
    value: Fraction | None = None
    """A `value`'s exact inches (`None`: no inch value, so `says` is `invalid`)."""
    extra: str = ""
    """`start_edge` and `outline`, shown only."""

    @property
    def usable(self) -> bool:
        return self.says in ("value", "kind")

    @property
    def same(self) -> Fraction | str | None:
        """What two usable answers agree on: exact inches, or the kind class."""
        return self.value if self.says == "value" else self.text

    def __str__(self) -> str:
        if self.says in ("value", "kind"):
            shown = self.text
        elif self.says == "unsure":
            shown = "(unsure)"
        else:
            shown = f"({self.says}: {self.text})"
        return f"{shown} [{self.extra}]" if self.extra else shown


@dataclass(frozen=True, slots=True)
class DepthAnswers:
    agent: str
    run: str
    prompt_ids: tuple[str, ...]
    readers: tuple[str, str]
    answers: Mapping[tuple[str, str, str], tuple[Reading, ...]]
    """(page hash, question id, field) -> the two readers' answers, in `readers` order."""
    sha256: str


def _answers_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DepthAnswersError(f"{field} must be a non-empty string, not {value!r}")
    return value


def _reading(agent: str, data: object, where: str) -> Reading:
    if not isinstance(data, Mapping):
        raise DepthAnswersError(f"{where}: a reader's answer is an object")
    extra = sorted(set(data) - _READER)
    if extra:
        raise DepthAnswersError(f"{where}: unknown field(s): {', '.join(extra)}")
    model = _answers_text(data.get("model_id"), f"{where} model_id")
    said = [word for word in _SAYS if word in data]
    if len(said) != 1:
        raise DepthAnswersError(
            f"{where}: a reader says exactly one of {_SAYS}, not {said or 'none'}"
        )
    says = said[0]
    shown = [
        f"{name}: {_answers_text(data[name], f'{where} {name}')}"
        for name in ("start_edge", "outline")
        if name in data
    ]
    if shown and says != "value":
        raise DepthAnswersError(f"{where}: start_edge and outline go with a value only")
    if says == "unsure":
        if data["unsure"] is not True:
            raise DepthAnswersError(f"{where}: unsure is true or absent")
        return Reading(model_id=model, says="unsure")
    if says in ("held", "invalid"):
        return Reading(model_id=model, says=says, text=_answers_text(data[says], f"{where} {says}"))
    if says == "kind":
        if agent != VIEW_AGENT:
            raise DepthAnswersError(f"{where}: a kind answers a {VIEW_AGENT} question only")
        kind = data["kind"]
        if kind not in _KIND_CLASS:
            raise DepthAnswersError(f"{where}: kind must be one of {tuple(_KIND_CLASS)}")
        return Reading(model_id=model, says="kind", text=_KIND_CLASS[str(kind)])
    if agent != DEPTH_AGENT:
        raise DepthAnswersError(f"{where}: a value answers a {DEPTH_AGENT} question only")
    text = _answers_text(data["value"], f"{where} value")
    value = inches(text)
    if value is None:
        # Not an inch value (`648 mm`): no usable answer, never a value. Kept, and shown.
        return Reading(
            model_id=model,
            says="invalid",
            text=f"{text!r} is not an exact inch value",
            extra="; ".join(shown),
        )
    return Reading(
        model_id=model, says="value", text=normalise_text(text), value=value, extra="; ".join(shown)
    )


def load_depth_answers(data: Mapping[str, object]) -> DepthAnswers:
    """Read a `pariksha-depth-answers/1` file, refusing anything it does not know."""
    if not isinstance(data, Mapping):
        raise DepthAnswersError("an answers file is one JSON object")
    if data.get("format") != ANSWERS_FORMAT:
        raise DepthAnswersError(f"format must be {ANSWERS_FORMAT!r}, not {data.get('format')!r}")
    agent = data.get("agent")
    if agent not in DEPTH_AGENTS:
        raise DepthAnswersError(f"agent must be one of {DEPTH_AGENTS}, not {agent!r}")
    extra = sorted(set(data) - _ANSWERS_HEADER)
    if extra:
        raise DepthAnswersError(f"unknown field(s) in the answers file: {', '.join(extra)}")
    run = _answers_text(data.get("run"), "run")
    prompts = data.get("prompt_ids", [])
    if not isinstance(prompts, list) or not all(isinstance(p, str) and p for p in prompts):
        raise DepthAnswersError("prompt_ids must be a list of prompt ids")
    entries = data.get("answers")
    if not isinstance(entries, list) or not entries:
        raise DepthAnswersError("answers must be a non-empty list")
    readings: dict[tuple[str, str, str], dict[str, Reading]] = {}
    for index, item in enumerate(entries):
        where = f"answer {index + 1}"
        if not isinstance(item, Mapping):
            raise DepthAnswersError(f"{where} is not an object")
        unknown = sorted(set(item) - _ENTRY)
        if unknown:
            raise DepthAnswersError(f"{where}: unknown field(s): {', '.join(unknown)}")
        page = item.get("page_sha256")
        if not isinstance(page, str) or not _SHA256.match(page):
            raise DepthAnswersError(f"{where}: page_sha256 must be 64 lower-case hex characters")
        question = _answers_text(item.get("question_id"), f"{where} question_id")
        field = item.get("field")
        if field not in FIELDS[str(agent)]:
            raise DepthAnswersError(
                f"{where}: field must be one of {FIELDS[str(agent)]}, not {field!r}"
            )
        identity = (page, question, str(field))
        if identity in readings:
            raise DepthAnswersError(f"{where}: {question} {field} is answered twice")
        readers = item.get("readers")
        if not isinstance(readers, list) or not readers:
            raise DepthAnswersError(f"{where}: readers must be a non-empty list")
        said: dict[str, Reading] = {}
        for number, reader in enumerate(readers):
            reading = _reading(str(agent), reader, f"{where} reader {number + 1}")
            if reading.model_id in said:
                raise DepthAnswersError(f"{where}: {reading.model_id} answers twice")
            said[reading.model_id] = reading
        readings[identity] = said
    models = sorted({model for said in readings.values() for model in said})
    if len(models) != 2:
        raise DepthAnswersError(
            f"the pair needs exactly two readers; the file has {len(models)}: {', '.join(models)}"
        )
    first, second = models
    return DepthAnswers(
        agent=str(agent),
        run=run,
        prompt_ids=tuple(prompts),
        readers=(first, second),
        answers={
            identity: tuple(
                said.get(model)
                or Reading(model_id=model, says="invalid", text="no answer in the file")
                for model in (first, second)
            )
            for identity, said in readings.items()
        },
        sha256=canonical_hash(data),
    )


def read_depth_answers(path: Path) -> DepthAnswers:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise DepthAnswersError(f"{path.name} is not JSON: {error}") from error
    return load_depth_answers(data)


# --- scoring --------------------------------------------------------------------------------


def _accepts(truth: Truth, reading: Reading) -> bool:
    if isinstance(truth, ValueTruth):
        return reading.says == "value" and reading.value in truth.values
    if isinstance(truth, KindTruth):
        return reading.says == "kind" and reading.text == truth.kind
    return False  # the key expects a hold: no value is right


def _pair(first: Reading, second: Reading, truth: Truth) -> str:
    if first.says == "invalid" or second.says == "invalid":
        return NO_ANSWER
    if first.usable and second.usable:
        if first.same != second.same:
            return DISAGREED
        return AGREED_RIGHT if _accepts(truth, first) else AGREED_WRONG
    if first.says == "held" and second.says == "held":
        return HELD_AS_KEY if isinstance(truth, HoldTruth) else HELD
    if first.says == "held" or second.says == "held":
        return HELD
    if first.usable or second.usable:
        return ONE_UNSURE
    return BOTH_UNSURE


def _verdict(reading: Reading, truth: Truth) -> str:
    if reading.says == "invalid":
        return INVALID
    if reading.says == "unsure":
        return UNSURE
    if reading.says == "held":
        return RIGHT if isinstance(truth, HoldTruth) else HELD
    return RIGHT if _accepts(truth, reading) else WRONG


def score_depth(
    key: DepthKey,
    answers: DepthAnswers,
    *,
    code_version: str,
    scorer_version: str = "unknown",
    created_at: str | None = None,
) -> Scorecard:
    """The scorecard: per key value, the pair and each reader; per field and in total."""
    _provenance(code_version)
    if answers.agent != key.agent:
        raise DepthAnswersError(
            f"the answers are a {answers.agent} file and the key is a {key.agent} key"
        )
    cases: list[CaseScore] = []
    pair: dict[str, Counter[str]] = defaultdict(Counter)
    single: Counter[str] = Counter()
    per_reader: dict[str, Counter[str]] = defaultdict(Counter)
    asked: Counter[str] = Counter()
    keyed: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    keyed_identities: set[tuple[str, str, str]] = set()
    for value in key.values:
        statuses[value.status] += 1
        identity = (value.question.page_sha256, value.question.question_id, value.field)
        keyed_identities.add(identity)
        readings = answers.answers.get(identity)
        question = f"{value.question} {value.field} ({value.where})"
        if value.status == OPEN or value.truth is None:
            cases.append(
                CaseScore(
                    case_id=value.case_id,
                    question=question,
                    expected="(open)",
                    outcome=KEY_OPEN,
                    readers=tuple(
                        ReaderScore(model_id=r.model_id, answer=str(r), verdict=NOT_SCORED)
                        for r in readings or ()
                    ),
                    note=f"key open, never scored: {value.open_reason}",
                )
            )
            continue
        expected = str(value.truth)
        if value.status == DOUBLE_READ:
            keyed[value.field] += 1
        if readings is None:
            cases.append(
                CaseScore(
                    case_id=value.case_id,
                    question=question,
                    expected=expected,
                    outcome=KEY_SINGLE_READ if value.status == SINGLE_READ else NOT_ASKED,
                    note="not asked: the answers file has no answer to this value",
                )
            )
            continue
        outcome = _pair(readings[0], readings[1], value.truth)
        scored = tuple(
            ReaderScore(model_id=r.model_id, answer=str(r), verdict=_verdict(r, value.truth))
            for r in readings
        )
        if value.status == SINGLE_READ:
            single[outcome] += 1
            cases.append(
                CaseScore(
                    case_id=value.case_id,
                    question=question,
                    expected=expected,
                    outcome=KEY_SINGLE_READ,
                    readers=scored,
                    note=f"key not double-read, never judged: the pair would be {outcome}",
                )
            )
            continue
        asked[value.field] += 1
        pair[value.field][outcome] += 1
        for reader in scored:
            per_reader[reader.model_id][reader.verdict] += 1
        cases.append(
            CaseScore(
                case_id=value.case_id,
                question=question,
                expected=expected,
                outcome=outcome,
                readers=scored,
            )
        )
    total_keyed, total_asked = sum(keyed.values()), sum(asked.values())
    metrics: dict[str, MetricResult] = {
        "asked": _rate("asked", total_asked, total_keyed, "double-read key values answered"),
    }
    fields = FIELDS[key.agent]
    for outcome in _PAIR_OUTCOMES:
        name = f"pair_{outcome}"
        metrics[name] = _rate(
            name, sum(pair[field][outcome] for field in fields), total_asked, "double-read values"
        )
    if len(fields) > 1:
        for field in fields:
            metrics[f"asked:{field}"] = _rate(f"asked:{field}", asked[field], keyed[field])
            for outcome in _PAIR_OUTCOMES:
                name = f"pair_{outcome}:{field}"
                metrics[name] = _rate(name, pair[field][outcome], asked[field])
    for model in answers.readers:
        for verdict in _READER_VERDICTS:
            name = f"reader_{verdict}:{model}"
            metrics[name] = _rate(name, per_reader[model][verdict], total_asked)
    singles = statuses[SINGLE_READ]
    metrics["key_single_read"] = _rate(
        "key_single_read", singles, len(key.values), "key not double-read: reported, never judged"
    )
    single_asked = sum(single.values())
    for outcome in _PAIR_OUTCOMES:
        name = f"single_read_{outcome}"
        metrics[name] = _rate(name, single[outcome], single_asked, "key not double-read")
    metrics["key_open"] = _rate(
        "key_open", statuses[OPEN], len(key.values), "key open: listed with its reason"
    )
    unkeyed = sorted(
        f"{question} {field}"
        for (page, question, field) in answers.answers
        if (page, question, field) not in keyed_identities
    )
    metrics["unkeyed_answers"] = _rate(
        "unkeyed_answers",
        len(unkeyed),
        len(answers.answers),
        ", ".join(unkeyed[:_LISTED]) + (", ..." if len(unkeyed) > _LISTED else ""),
    )
    return Scorecard(
        agent=key.agent,
        key_name=key.name,
        key_version=key.version,
        key_sha256=key.sha256,
        code_version=code_version,
        scorer_version=scorer_version,
        extractor_versions={
            "agent": key.agent,
            "prompt_ids": ",".join(answers.prompt_ids) or "(not given)",
            "model_ids": ",".join(answers.readers),
            "answers": ANSWERS_FORMAT,
            **_code_note(code_version),
        },
        source={
            "database": "answers file",
            "extraction_run_id": answers.run,
            "answers_sha256": answers.sha256,
            "key_provenance": KEY_PROVENANCE,
        },
        metrics=metrics,
        cases=tuple(cases),
        created_at=created_at or _now(),
    )


# --- compare --------------------------------------------------------------------------------

_HIGHER: Final = ("asked", "pair_agreed_right", "pair_held_as_key", "reader_right:")
_NEITHER: Final = ("key_", "single_read_", "unkeyed_answers")


def depth_direction(name: str) -> Direction | None:
    """More is better for rights, less for the rest; counts about the key or file have none."""
    if name.startswith(_NEITHER):
        return None
    return Direction.MINIMUM if name.startswith(_HIGHER) else Direction.MAXIMUM


def compare_depth(before: Scorecard, after: Scorecard) -> Comparison:
    """Better (more right values or expected holds), with 0 new agreed-wrong.

    Refuses any card that is not a `rekha-view` or `akshara-depth` card, and two cards of
    different agents."""
    for card in (before, after):
        if card.agent not in DEPTH_AGENTS:
            raise IncomparableRuns(
                f"this compare judges {' or '.join(sorted(DEPTH_AGENTS))} scorecards only; this "
                f"is a {card.agent} scorecard (before: {before.agent}, after: {after.agent})"
            )
    if before.agent != after.agent:
        raise IncomparableRuns(
            f"a {before.agent} scorecard and a {after.agent} scorecard measure different agents"
        )
    return compare_translated(
        before, after, outcomes={HELD_AS_KEY: AGREED_RIGHT}, direction=depth_direction
    )
