"""The answer-key file Pariksha scores against: `pariksha-key/1` (#1219).

The format is described in `eval/pariksha/__init__.py`. This module reads it strictly, refusing
anything it does not know, and writes it back in the same shape (the converters that turn local
keys into this format use `dump_key`).

Source: issue #1219 · Verification: `tests/eval/test_pariksha_keys.py`
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from eval.architect_reader import KeyPage, key_pages
from extraction.slot_reader.seal import normalise_text

__all__ = [
    "AGENTS",
    "DEPTH_FAMILIES",
    "FAMILIES",
    "KEY_FORMAT",
    "NO_ROW_KINDS",
    "AnswerKey",
    "ArchitectAnswerKey",
    "KeyCase",
    "KeyFormatError",
    "LabelTruth",
    "QuestionIdentity",
    "RowTruth",
    "dump_key",
    "load_key",
    "read_key",
]

KEY_FORMAT: Final = "pariksha-key/1"
#: The agents a key can be for, and the prompt family each one's questions are asked under.
FAMILIES: Final[Mapping[str, str]] = {
    "akshara": "claude-slot-span",
    "rekha": "slot-row-choice",
}
AGENTS: Final = ("akshara", "rekha", "vastu")
#: The phase-5 agents (#1257): the view-kind question and the depth and sink values, with the family
#: each one's key cases name. Their keys are `pariksha-key/1` too, with their own case shape, read by
#: `eval/pariksha/depth.py`; `load_key` refuses them and says so, so this loader never scores one.
DEPTH_FAMILIES: Final[Mapping[str, str]] = {
    "rekha-view": "view-kind",
    "akshara-depth": "depth-span",
}
#: A row question's two answers that name no box (`slot-row-choice-v3` and later).
NO_ROW_KINDS: Final = ("no_countertop", "not_among_boxes")

_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")
_BOX: Final = re.compile(r"^\d+,\d+,\d+,\d+$")
_HEADER: Final = frozenset({"format", "agent", "name", "version", "cases"})
_ARCH_HEADER: Final = frozenset({"format", "agent", "name", "version", "document_sha256", "pages"})
_CASE: Final = frozenset(
    {"family", "page_sha256", "question_id", "slot_box", "picture_sha256", "answer", "source"}
)


class KeyFormatError(ValueError):
    """The key file is not a `pariksha-key/1` key; the message says which part."""


@dataclass(frozen=True, slots=True)
class QuestionIdentity:
    """What a question is, the same in every run and every database that asked it."""

    family: str
    """The prompt id without its version: `claude-slot-span`, `slot-row-choice`."""
    page_sha256: str
    """The page's content hash (`source_page_sha256` in the question packet)."""
    question_id: str
    """The id the code gives the question on its page (`p2:slot0:0`, `p2:row-choice`)."""

    @property
    def case_id(self) -> str:
        return f"{self.page_sha256[:16]}/{self.question_id}"

    def __str__(self) -> str:
        return f"{self.family}@{self.page_sha256[:12]}/{self.question_id}"


@dataclass(frozen=True, slots=True)
class LabelTruth:
    """The printed texts a label question's answer may be, normalised; none = the span has no label."""

    texts: tuple[str, ...]

    @property
    def no_label(self) -> bool:
        return not self.texts

    def __str__(self) -> str:
        return "no label" if self.no_label else " | ".join(self.texts)


@dataclass(frozen=True, slots=True)
class RowTruth:
    """The box that is the countertop row, or one of the two answers that name no box."""

    box: int | None
    kind: str
    """`row`, `no_countertop` or `not_among_boxes`."""

    def __str__(self) -> str:
        return f"box {self.box}" if self.kind == "row" else self.kind


@dataclass(frozen=True, slots=True)
class KeyCase:
    question: QuestionIdentity
    truth: LabelTruth | RowTruth
    slot_box: str | None = None
    """Labels (required): the span's box as recorded (`slot-box:` flag). The question is found by
    page, box and label position, so a renumbered slot is followed and another span never matches."""
    picture_sha256: str | None = None
    """Rows (required): the numbered picture whose box numbers the answer uses; a question that
    showed another picture is not this case's. Optional on a label."""
    source: str = ""
    """Who keyed it and how (free text, for a person)."""

    @property
    def case_id(self) -> str:
        return self.question.case_id


@dataclass(frozen=True, slots=True)
class AnswerKey:
    """A label or row key: one case per question."""

    agent: str
    name: str
    version: str
    cases: tuple[KeyCase, ...]
    sha256: str
    """Hash of the key's canonical JSON, recorded in every scorecard scored against it."""


@dataclass(frozen=True, slots=True)
class ArchitectAnswerKey:
    """Vastu's key: the architect widths printed on each page (`eval/architect_reader.py`)."""

    name: str
    version: str
    document_sha256: str | None
    pages: tuple[KeyPage, ...]
    raw_pages: tuple[Mapping[str, object], ...]
    sha256: str


def _canonical_hash(data: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KeyFormatError(f"{field} must be a non-empty string, not {value!r}")
    return value


def _unknown(data: Mapping[str, object], known: frozenset[str], where: str) -> None:
    extra = sorted(set(data) - known)
    if extra:
        raise KeyFormatError(f"unknown field(s) in {where}: {', '.join(extra)}")


def _label_truth(answer: Mapping[str, object], where: str) -> LabelTruth:
    if set(answer) == {"no_label"} and answer["no_label"] is True:
        return LabelTruth(texts=())
    if set(answer) != {"texts"}:
        raise KeyFormatError(
            f"{where}: a label answer is {{'texts': [...]}} or {{'no_label': true}}, not {answer!r}"
        )
    texts = answer["texts"]
    if not isinstance(texts, list) or not texts:
        raise KeyFormatError(f"{where}: texts must be a non-empty list of printed texts")
    normalised: list[str] = []
    for text in texts:
        folded = normalise_text(_text(text, f"{where} texts"))
        if folded not in normalised:
            normalised.append(folded)
    return LabelTruth(texts=tuple(normalised))


def _row_truth(answer: Mapping[str, object], where: str) -> RowTruth:
    if set(answer) == {"box"}:
        box = answer["box"]
        if isinstance(box, bool) or not isinstance(box, int) or box < 1:
            raise KeyFormatError(f"{where}: box must be a whole number from 1, not {box!r}")
        return RowTruth(box=box, kind="row")
    if set(answer) == {"kind"} and answer["kind"] in NO_ROW_KINDS:
        return RowTruth(box=None, kind=str(answer["kind"]))
    raise KeyFormatError(
        f"{where}: a row answer is {{'box': n}} or {{'kind': one of {NO_ROW_KINDS}}}, "
        f"not {answer!r}"
    )


def _case(agent: str, data: object, index: int) -> KeyCase:
    where = f"case {index + 1}"
    if not isinstance(data, Mapping):
        raise KeyFormatError(f"{where} is not an object")
    _unknown(data, _CASE, where)
    family = _text(data.get("family"), f"{where} family")
    if family != FAMILIES[agent]:
        raise KeyFormatError(f"{where}: family {family!r} is not {agent}'s ({FAMILIES[agent]!r})")
    page = data.get("page_sha256")
    if not isinstance(page, str) or not _SHA256.match(page):
        raise KeyFormatError(f"{where}: page_sha256 must be 64 lower-case hex characters")
    question_id = _text(data.get("question_id"), f"{where} question_id")
    slot_box = data.get("slot_box")
    if agent == "akshara" and slot_box is None:
        raise KeyFormatError(
            f"{where}: a label case needs its slot_box: slot numbers follow the chosen row, so "
            "without the box the case could be scored against another span"
        )
    if slot_box is not None and (
        agent != "akshara" or not isinstance(slot_box, str) or not _BOX.match(slot_box)
    ):
        raise KeyFormatError(f"{where}: slot_box is a label's 'x0,y0,x1,y1', not {slot_box!r}")
    picture = data.get("picture_sha256")
    if agent == "rekha" and picture is None:
        raise KeyFormatError(
            f"{where}: a row case needs picture_sha256, the numbered picture its box number "
            "belongs to: on a re-numbered picture the same number is another line"
        )
    if picture is not None and (not isinstance(picture, str) or not _SHA256.match(picture)):
        raise KeyFormatError(f"{where}: picture_sha256 must be 64 lower-case hex characters")
    answer = data.get("answer")
    if not isinstance(answer, Mapping):
        raise KeyFormatError(f"{where}: answer must be an object")
    truth: LabelTruth | RowTruth = (
        _label_truth(answer, where) if agent == "akshara" else _row_truth(answer, where)
    )
    source = data.get("source", "")
    if not isinstance(source, str):
        raise KeyFormatError(f"{where}: source must be text")
    return KeyCase(
        question=QuestionIdentity(family=family, page_sha256=page, question_id=question_id),
        truth=truth,
        slot_box=slot_box,
        picture_sha256=picture,
        source=source,
    )


def load_key(data: Mapping[str, object]) -> AnswerKey | ArchitectAnswerKey:
    """Read a key from its parsed JSON, refusing anything that is not `pariksha-key/1`."""
    if not isinstance(data, Mapping):
        raise KeyFormatError("a key is one JSON object")
    if data.get("format") != KEY_FORMAT:
        raise KeyFormatError(f"format must be {KEY_FORMAT!r}, not {data.get('format')!r}")
    agent = data.get("agent")
    if agent in DEPTH_FAMILIES:
        raise KeyFormatError(
            f"a {agent} key is read by eval/pariksha/depth.py (read_depth_key), not by this loader"
        )
    if agent not in AGENTS:
        raise KeyFormatError(f"agent must be one of {AGENTS}, not {agent!r}")
    name = _text(data.get("name"), "name")
    version = _text(data.get("version"), "version")
    if agent == "vastu":
        _unknown(data, _ARCH_HEADER, "the key")
        document = data.get("document_sha256")
        if document is not None and (not isinstance(document, str) or not _SHA256.match(document)):
            raise KeyFormatError("document_sha256 must be 64 lower-case hex characters")
        pages = data.get("pages")
        try:
            parsed = key_pages({"pages": pages})
        except (TypeError, ValueError, KeyError) as error:
            raise KeyFormatError(f"pages: {error}") from error
        assert isinstance(pages, list)
        architect = ArchitectAnswerKey(
            name=name,
            version=version,
            document_sha256=document,
            pages=parsed,
            raw_pages=tuple(pages),
            sha256="",
        )
        return replace(architect, sha256=_canonical_hash(dump_key(architect)))
    _unknown(data, _HEADER, "the key")
    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise KeyFormatError("cases must be a non-empty list")
    cases = tuple(_case(str(agent), entry, index) for index, entry in enumerate(raw_cases))
    seen: set[QuestionIdentity] = set()
    for case in cases:
        if case.question in seen:
            raise KeyFormatError(f"the key names question {case.question} twice")
        seen.add(case.question)
    key = AnswerKey(agent=str(agent), name=name, version=version, cases=cases, sha256="")
    # Hashed in its canonical form, so two spellings of one key (key order, a `″` for a `"`) are
    # one key, and a scorecard's key hash says exactly which answers it was scored against.
    return replace(key, sha256=_canonical_hash(dump_key(key)))


def read_key(path: Path) -> AnswerKey | ArchitectAnswerKey:
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as error:
        raise KeyFormatError(f"{path.name} is not JSON: {error}") from error
    return load_key(data)


def _case_document(case: KeyCase) -> dict[str, object]:
    answer: dict[str, object]
    if isinstance(case.truth, LabelTruth):
        answer = {"no_label": True} if case.truth.no_label else {"texts": list(case.truth.texts)}
    elif case.truth.kind == "row":
        answer = {"box": case.truth.box}
    else:
        answer = {"kind": case.truth.kind}
    document: dict[str, object] = {
        "family": case.question.family,
        "page_sha256": case.question.page_sha256,
        "question_id": case.question.question_id,
        "answer": answer,
    }
    if case.slot_box is not None:
        document["slot_box"] = case.slot_box
    if case.picture_sha256 is not None:
        document["picture_sha256"] = case.picture_sha256
    if case.source:
        document["source"] = case.source
    return document


def dump_key(key: AnswerKey | ArchitectAnswerKey) -> dict[str, object]:
    """The key as a `pariksha-key/1` JSON object (texts as normalised when read)."""
    if isinstance(key, ArchitectAnswerKey):
        document: dict[str, object] = {
            "format": KEY_FORMAT,
            "agent": "vastu",
            "name": key.name,
            "version": key.version,
            "pages": [dict(page) for page in key.raw_pages],
        }
        if key.document_sha256 is not None:
            document["document_sha256"] = key.document_sha256
        return document
    return {
        "format": KEY_FORMAT,
        "agent": key.agent,
        "name": key.name,
        "version": key.version,
        "cases": [_case_document(case) for case in key.cases],
    }
