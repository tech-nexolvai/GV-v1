"""The depth reader's answers, read strictly: anything malformed is invalid, never a value (#1265).

A Claude reply is first checked against its question's schema (`claude_output.claude_answer`: the
reply finished its turn, is exactly one JSON object, has exactly the schema's keys and types, and
each word is from its closed list). Then this module reads what the schema cannot say:

* **view kind**: one of `prompts.AI_VIEW_KINDS`; `unsure` is its own answer (`kind=None`).
* **span roles**: each pick is a numbered dimension the picture shows, `none` or `unsure`; a
  letter is one the picture prints. One number given for two different roles is a contradiction,
  and `front_offset_start` must go with its strip: an edge only when a strip is picked, and `none`
  only when none is. Either is malformed.
* **the number**: v5's span answer (`seal.ReaderAnswer`), read exactly as the width reader reads it.

A malformed answer raises `MalformedFormAnswer`: the shared route re-asks once, then the reader
abstains (`None`), which the reader records as "no accepted answer". Nothing here turns an answer
into a value; `read.py` decides that with code's judgment.

Source: issue #1265 · Verification: `tests/extraction/depth/test_output.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

from extraction.depth.prompts import (
    AI_VIEW_KINDS,
    OFFSET_STARTS,
    PICK_NONE,
    PICK_UNSURE,
    outline_letters,
)
from extraction.form_reader.bedrock import MalformedFormAnswer
from extraction.slot_reader.seal import Belongs, ReaderAnswer, parse_belongs

__all__ = [
    "ROLES",
    "DepthReply",
    "Pick",
    "SpanRoleAnswer",
    "ViewKindAnswer",
    "depth_number_answer",
    "span_role_answer",
    "view_kind_answer",
]

type Pick = int | Literal["none", "unsure"]
"""A numbered dimension (or, for the hole, a letter's position from 1), `none` or `unsure`."""
type OffsetStart = Literal["stone_front_edge", "cabinet_face", "unsure", "none"]
type AiKind = Literal["front", "plan", "section_or_side", "detail", "other"]

#: The roles a span-role answer names, in its schema's order.
ROLES: Final = ("depth", "cutout_width", "cutout_depth", "front_offset")


@dataclass(frozen=True, slots=True)
class ViewKindAnswer:
    model_id: str
    kind: AiKind | None
    """`None` when the reader said "unsure"."""
    why: str


@dataclass(frozen=True, slots=True)
class SpanRoleAnswer:
    model_id: str
    depth: Pick
    cutout_width: Pick
    cutout_depth: Pick
    front_offset: Pick
    front_offset_start: OffsetStart
    hole_outline: str | Literal["none", "unsure"]
    """A letter the outlines picture prints, `none` or `unsure`."""
    why: str

    def pick(self, role: str) -> Pick:
        if role not in ROLES:
            raise ValueError(f"{role!r} is not a span role")
        value: Pick = getattr(self, role)
        return value


type DepthReply = ViewKindAnswer | SpanRoleAnswer | ReaderAnswer
"""Every answer the depth reader's three questions give."""


def _text(data: Mapping[str, object], name: str) -> str:
    value = data.get(name)
    if not isinstance(value, str):
        raise MalformedFormAnswer(f"the answer's {name!r} is not text")
    return value


def _exact_keys(data: Mapping[str, object], keys: frozenset[str]) -> None:
    if set(data) != keys:
        raise MalformedFormAnswer("the answer's fields are not exactly the question's")


def view_kind_answer(data: Mapping[str, object], *, model_id: str) -> ViewKindAnswer:
    """A `view-kind-v1` answer; `unsure` is `kind=None`."""
    _exact_keys(data, frozenset({"kind", "why"}))
    kind = _text(data, "kind")
    if kind not in AI_VIEW_KINDS:
        raise MalformedFormAnswer(f"{kind!r} is not a kind of view the question offers")
    why = _text(data, "why")
    if kind == PICK_UNSURE:
        return ViewKindAnswer(model_id, None, why)
    chosen: AiKind
    if kind == "front":
        chosen = "front"
    elif kind == "plan":
        chosen = "plan"
    elif kind == "section_or_side":
        chosen = "section_or_side"
    elif kind == "detail":
        chosen = "detail"
    else:
        chosen = "other"
    return ViewKindAnswer(model_id, chosen, why)


def _pick(word: str, spans: int) -> Pick:
    if word == PICK_NONE:
        return "none"
    if word == PICK_UNSURE:
        return "unsure"
    if not word.isdigit() or word != str(int(word)) or not 1 <= int(word) <= spans:
        raise MalformedFormAnswer(f"{word!r} is not a numbered dimension the picture shows")
    return int(word)


def span_role_answer(
    data: Mapping[str, object], *, model_id: str, spans: int, outlines: int
) -> SpanRoleAnswer:
    """A `depth-span-role-v1` answer, checked against the picture's own counts."""
    _exact_keys(data, frozenset({*ROLES, "front_offset_start", "hole_outline", "why"}))
    picks = {role: _pick(_text(data, role), spans) for role in ROLES}
    numbers = [pick for pick in picks.values() if isinstance(pick, int)]
    if len(numbers) != len(set(numbers)):
        raise MalformedFormAnswer("one numbered dimension was given for two different things")
    start = _text(data, "front_offset_start")
    if start not in OFFSET_STARTS:
        raise MalformedFormAnswer(f"{start!r} is not a start the question offers")
    strip = picks["front_offset"]
    if isinstance(strip, int) == (start == PICK_NONE):
        raise MalformedFormAnswer(
            "front_offset_start names an edge only when a strip is picked, and none otherwise"
        )
    hole = _text(data, "hole_outline")
    if hole not in (*outline_letters(outlines), PICK_NONE, PICK_UNSURE):
        raise MalformedFormAnswer(f"{hole!r} is not an outline the picture letters")
    offset_start: OffsetStart
    if start == "stone_front_edge":
        offset_start = "stone_front_edge"
    elif start == "cabinet_face":
        offset_start = "cabinet_face"
    elif start == PICK_UNSURE:
        offset_start = "unsure"
    else:
        offset_start = "none"
    return SpanRoleAnswer(
        model_id=model_id,
        depth=picks["depth"],
        cutout_width=picks["cutout_width"],
        cutout_depth=picks["cutout_depth"],
        front_offset=strip,
        front_offset_start=offset_start,
        hole_outline=hole,
        why=_text(data, "why"),
    )


def depth_number_answer(data: Mapping[str, object], *, model_id: str) -> ReaderAnswer:
    """A `claude-depth-span-v1` answer: v5's shape, read as the width reader reads it. A text given
    with anything but "yes" is a contradiction (the question says to return an empty text)."""
    _exact_keys(
        data, frozenset({"belongs", "text", "stacked", "combined", "readable", "no_dimension"})
    )
    flags = {name: data.get(name) for name in ("stacked", "combined", "readable", "no_dimension")}
    if not all(isinstance(flag, bool) for flag in flags.values()):
        raise MalformedFormAnswer("the answer's flags are not true or false")
    belongs_word = _text(data, "belongs")
    if belongs_word not in ("yes", "no", "unsure"):
        raise MalformedFormAnswer(f"{belongs_word!r} is not yes, no or unsure")
    belongs = parse_belongs(belongs_word)
    text = _text(data, "text")
    if belongs is not Belongs.YES and text.strip():
        raise MalformedFormAnswer("a label was copied for a dimension the reader says it lacks")
    return ReaderAnswer(
        model_id=model_id,
        text=text,
        readable=flags["readable"] is True,
        no_dimension=flags["no_dimension"] is True,
        stacked=flags["stacked"] is True,
        combined=flags["combined"] is True,
        belongs=belongs,
    )
