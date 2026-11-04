"""GV Review's fixed question list: the questions the review asks about each kind of item.

Every finding in this product is made by an AI; code only does arithmetic on numbers the AI already
wrote down. So a problem the AI never notices is invisible to the whole system, and no later stage
can recover it. Two runs of the same job that are asked "what looks wrong on this page?" share only
a third of their answers, and the hand audits list dozens of real problems nobody ever asked about.

A fixed question does not drift. "What is this top's finished depth, and what does the architect give
for it?" is asked the same way on every top, in every run, and the question itself carries the
settled rule that decides the answer and the look-alikes that are not problems. This module reads
that list from `vocabulary/review_questions.yaml` and hands it to the rest of the code.

What is in the file:

- **The item taxonomy.** The kinds of thing GV reviews, drawn from three audited jobs: countertop
  piece, sink or appliance cut-out sheet, base cabinet run, upper cabinet run, vanity, filler,
  splash, accessory detail, appliance opening, key plan, and cover or checklist sheet. Each kind
  says which of its views carries which facts.
- **The questions.** Each one has a stable key, the question in plain English, the item kinds it is
  asked about, the facts it needs and the side each comes from, the settled rule that governs the
  answer, one line of provenance saying exactly where that rule comes from, what makes an answer a
  problem, what explicitly does not (the anti-patterns), and whether code can prove it (so it can go
  red) or only the AI can judge it.

Some questions are governed by a value the reviewer enters per job rather than by a GV standard: the
front overhang, the end overhang at a free end, the cabinet depth, the splash thickness, the sink's
inside size and the cabinet width limits. Those carry `missing_fact_when`, and when the job has not
set the value the honest answer is a missing fact, never a defect and never a silent pass.

A question whose `item_kinds` is `["*"]` is asked about every item, whatever its kind. That matters
for the coverage ledger: a sheet whose item was never identified is still charged those questions,
so an unidentified item shows up as a hole rather than disappearing from the grid.

Nothing here is invented. Every rule is either GV's own company standard or something settled with
the client and recorded as settled. The file holds no client, project or drawing names: that is what
keeps it generic millwork knowledge, and what makes it vendor-agnostic.

This module reads and checks; it never writes. Loading is cached, and `reset()` makes the next read
load the file again (tests use it with `GV_REVIEW_QUESTIONS` to point at a made-up list).

Verification: tests/review/assistant/test_question_list.py (made-up questions, no client data).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

__all__ = [
    "COMPARES",
    "DECIDED_BY",
    "EVERY_KIND",
    "FILE",
    "FROM",
    "SIDES",
    "ItemKind",
    "Need",
    "Question",
    "QuestionList",
    "QuestionsError",
    "code_questions",
    "item_kind",
    "item_kinds",
    "question",
    "question_list",
    "questions",
    "questions_for",
    "reset",
    "vendor_only_questions",
]

#: The question list's file, beside the vocabulary the rest of the review reads.
FILE: Final = "review_questions.yaml"

#: The schema the file must declare, so an older file is refused rather than half-read.
SCHEMA: Final = "gv-review-questions/1"

#: Environment variable that replaces the file's path (tests point it at a made-up list).
PATH_ENV: Final = "GV_REVIEW_QUESTIONS"

#: An `item_kinds` entry that means "asked about every item, whatever its kind".
EVERY_KIND: Final = "*"

#: Where a fact comes from: the vendor's drawing, the architect's drawing, or a GV rule or setting.
SIDES: Final[tuple[str, ...]] = ("vendor", "architect", "gv_rule")

#: Who can settle the answer: code can prove it from numbers (so it can go red), or only the AI can
#: judge it (so it is always a please-check).
DECIDED_BY: Final[tuple[str, ...]] = ("code", "ai")

#: What the answer is weighed against. Measured over 683 audited answers: a question the vendor's
#: own sheets answer was right 98.6% of the time, while one weighed against the architect was right
#: 67% of the time and carried almost all the noise.
COMPARES: Final[tuple[str, ...]] = ("vendor_only", "gv_rule", "architect")

#: Where the question came from, so the list's provenance can be reported.
FROM: Final[tuple[str, ...]] = ("true_answers", "misses", "both", "rulebook")


class QuestionsError(RuntimeError):
    """The question list does not hold together, named so it can be fixed."""


@dataclass(frozen=True, slots=True)
class Need:
    """One fact a question needs: which side it comes from, and the view that carries it."""

    side: str
    fact: str
    view: str = ""


@dataclass(frozen=True, slots=True)
class ItemKind:
    """One kind of item GV reviews, and the views that carry its facts."""

    key: str
    label: str
    what: str
    views: Mapping[str, str]

    def view_names(self) -> tuple[str, ...]:
        """The kind's view names, in the order the file gives them."""
        return tuple(self.views)


@dataclass(frozen=True, slots=True)
class Question:
    """One fixed question, with the rule that decides it and the look-alikes that are not problems."""

    key: str
    asks: str
    item_kinds: tuple[str, ...]
    needs: tuple[Need, ...]
    rule: str
    source: str
    problem_when: str
    missing_fact_when: str
    not_a_problem_when: tuple[str, ...]
    decided_by: str
    compares: str
    came_from: str
    seen_as: Mapping[str, Any]

    @property
    def every_kind(self) -> bool:
        """True when this question is asked about every item, whatever its kind."""
        return EVERY_KIND in self.item_kinds

    def applies_to(self, kind: str | None) -> bool:
        """Is this question asked about an item of `kind`? An unknown kind gets the `*` questions."""
        if self.every_kind:
            return True
        return kind is not None and kind in self.item_kinds

    @property
    def governed_by_a_job_value(self) -> bool:
        """Does a job setting govern the answer, so its absence is a missing fact, not a defect?"""
        return bool(self.missing_fact_when)

    @property
    def needs_the_architect(self) -> bool:
        """Is this question weighed against the architect's drawing?"""
        return self.compares == "architect"

    @property
    def can_go_red(self) -> bool:
        """Can code prove this question's answer wrong, so the finding can go red?"""
        return self.decided_by == "code"

    def sides(self) -> tuple[str, ...]:
        """The sides this question's facts come from, in the order the file gives them."""
        seen: list[str] = []
        for need in self.needs:
            if need.side not in seen:
                seen.append(need.side)
        return tuple(seen)


@dataclass(frozen=True, slots=True)
class QuestionList:
    """The whole list: the item taxonomy and the questions, checked and ready to use."""

    kinds: tuple[ItemKind, ...]
    asked: tuple[Question, ...]

    def kind(self, key: str) -> ItemKind | None:
        """The item kind with this key, or None."""
        return next((kind for kind in self.kinds if kind.key == key), None)

    def question(self, key: str) -> Question | None:
        """The question with this key, or None."""
        return next((one for one in self.asked if one.key == key), None)

    def for_kind(self, kind: str | None) -> tuple[Question, ...]:
        """Every question asked about an item of this kind, in the file's order.

        An unknown kind (None, or a kind the file does not name) gets only the questions asked about
        every item: we still know those apply, even when we do not know what the item is.
        """
        return tuple(one for one in self.asked if one.applies_to(kind))

    def kind_keys(self) -> tuple[str, ...]:
        """Every item-kind key the file names."""
        return tuple(kind.key for kind in self.kinds)


def _text(value: object, where: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuestionsError(f"{where}: {field} must be text that says something")
    return value.strip()


def _lines(value: object, where: str, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise QuestionsError(f"{where}: {field} must be a list")
    return tuple(_text(line, where, field) for line in value)


def _one_of(value: object, allowed: Sequence[str], where: str, field: str) -> str:
    text = _text(value, where, field)
    if text not in allowed:
        raise QuestionsError(f"{where}: {field} is {text!r}, not one of {', '.join(allowed)}")
    return text


def _need(raw: object, where: str) -> Need:
    if not isinstance(raw, Mapping):
        raise QuestionsError(f"{where}: each entry under needs must be a mapping")
    return Need(
        side=_one_of(raw.get("side"), SIDES, where, "needs.side"),
        fact=_text(raw.get("fact"), where, "needs.fact"),
        view=str(raw.get("view") or "").strip(),
    )


def _item_kind(raw: object) -> ItemKind:
    if not isinstance(raw, Mapping):
        raise QuestionsError("item_kinds: each entry must be a mapping")
    key = _text(raw.get("key"), "item_kinds", "key")
    views = raw.get("views")
    if not isinstance(views, Mapping) or not views:
        raise QuestionsError(f"item kind {key}: views must be a mapping with at least one view")
    return ItemKind(
        key=key,
        label=_text(raw.get("label"), key, "label"),
        what=_text(raw.get("what"), key, "what"),
        views={str(name): _text(says, key, f"views.{name}") for name, says in views.items()},
    )


def _question(raw: object, kind_keys: Sequence[str]) -> Question:
    if not isinstance(raw, Mapping):
        raise QuestionsError("questions: each entry must be a mapping")
    key = _text(raw.get("key"), "questions", "key")
    kinds = _lines(raw.get("item_kinds"), key, "item_kinds")
    if not kinds:
        raise QuestionsError(f"question {key}: item_kinds must name at least one kind")
    for named in kinds:
        if named != EVERY_KIND and named not in kind_keys:
            raise QuestionsError(f"question {key}: item kind {named!r} is not in the taxonomy")
    needs = raw.get("needs")
    if not isinstance(needs, Sequence) or isinstance(needs, (str, bytes)) or not needs:
        raise QuestionsError(f"question {key}: needs must list at least one fact")
    seen_as = raw.get("seen_as") or {}
    if not isinstance(seen_as, Mapping):
        raise QuestionsError(f"question {key}: seen_as must be a mapping")
    return Question(
        key=key,
        asks=_text(raw.get("asks"), key, "asks"),
        item_kinds=kinds,
        needs=tuple(_need(one, key) for one in needs),
        rule=_text(raw.get("rule"), key, "rule"),
        source=_text(raw.get("source"), key, "source"),
        problem_when=_text(raw.get("problem_when"), key, "problem_when"),
        missing_fact_when=str(raw.get("missing_fact_when") or "").strip(),
        not_a_problem_when=_lines(raw.get("not_a_problem_when"), key, "not_a_problem_when"),
        decided_by=_one_of(raw.get("decided_by"), DECIDED_BY, key, "decided_by"),
        compares=_compares(raw, key),
        came_from=_one_of(raw.get("from"), FROM, key, "from"),
        seen_as=dict(seen_as),
    )


def _compares(raw: Mapping[str, Any], key: str) -> str:
    """What the question is weighed against: stated in the file, or worked out from its facts."""
    given = raw.get("compares")
    if given is not None:
        return _one_of(given, COMPARES, key, "compares")
    sides = {one.get("side") for one in raw["needs"] if isinstance(one, Mapping)}
    if "architect" in sides:
        return "architect"
    if "gv_rule" in sides:
        return "gv_rule"
    return "vendor_only"


def _path() -> Path:
    named = os.environ.get(PATH_ENV)
    if named:
        return Path(named)
    return Path(__file__).resolve().parents[3] / "vocabulary" / FILE


def _read(path: Path) -> Mapping[str, Any]:
    import yaml  # type: ignore[import-untyped]  # loaded here so the module stays light

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:  # pragma: no cover - a missing file is a deployment fault
        raise QuestionsError(f"cannot read the question list at {path}: {error}") from error
    except yaml.YAMLError as error:
        raise QuestionsError(f"the question list at {path} is not valid YAML: {error}") from error
    if not isinstance(data, Mapping):
        raise QuestionsError(f"the question list at {path} must be a mapping")
    return data


def _built(data: Mapping[str, Any]) -> QuestionList:
    if data.get("schema") != SCHEMA:
        raise QuestionsError(f"the question list declares {data.get('schema')!r}, not {SCHEMA!r}")
    raw_kinds = data.get("item_kinds")
    if not isinstance(raw_kinds, Sequence) or isinstance(raw_kinds, (str, bytes)) or not raw_kinds:
        raise QuestionsError("item_kinds must list at least one kind")
    kinds = tuple(_item_kind(one) for one in raw_kinds)
    repeated = _repeated(kind.key for kind in kinds)
    if repeated:
        raise QuestionsError(f"item kind {repeated!r} is listed twice")
    raw_asked = data.get("questions")
    if not isinstance(raw_asked, Sequence) or isinstance(raw_asked, (str, bytes)) or not raw_asked:
        raise QuestionsError("questions must list at least one question")
    kind_keys = tuple(kind.key for kind in kinds)
    asked = tuple(_question(one, kind_keys) for one in raw_asked)
    repeated = _repeated(one.key for one in asked)
    if repeated:
        raise QuestionsError(f"question {repeated!r} is listed twice")
    return QuestionList(kinds=kinds, asked=asked)


def _repeated(keys: Iterable[str]) -> str | None:
    seen: set[str] = set()
    for key in keys:
        if key in seen:
            return key
        seen.add(key)
    return None


@cache
def question_list() -> QuestionList:
    """The whole question list, read once and checked."""
    return _built(_read(_path()))


def reset() -> None:
    """Forget the loaded list, so the next read loads the file again."""
    question_list.cache_clear()


def item_kinds() -> tuple[ItemKind, ...]:
    """Every kind of item GV reviews."""
    return question_list().kinds


def item_kind(key: str) -> ItemKind | None:
    """One item kind, or None when the taxonomy does not name it."""
    return question_list().kind(key)


def questions() -> tuple[Question, ...]:
    """Every question, in the file's order."""
    return question_list().asked


def question(key: str) -> Question | None:
    """One question, or None when the list does not hold it."""
    return question_list().question(key)


def questions_for(kind: str | None) -> tuple[Question, ...]:
    """Every question asked about an item of this kind; an unknown kind gets the `*` questions."""
    return question_list().for_kind(kind)


def code_questions() -> tuple[Question, ...]:
    """The questions code can prove, so their answers can go red."""
    return tuple(one for one in questions() if one.can_go_red)


def vendor_only_questions() -> tuple[Question, ...]:
    """The questions the vendor's own sheets answer, which need no architect drawing at all."""
    return tuple(one for one in questions() if one.compares == "vendor_only")
