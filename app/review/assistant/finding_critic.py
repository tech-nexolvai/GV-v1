"""An adversarial critic of a whole-set answer's findings (chat accuracy, #1498).

Our scoring of past answers (the eval kit, Set 3) found the wrong items were not misread digits but
the wrong thing read: GV's own coloured note taken as the vendor's text, a neighbouring dimension
taken for the item's, and a brand prototype on one half of a sheet taken as the architect's
drawing. A second read cannot catch those: it is asked what is printed, not whether it is the
right thing. So for whole-set and full-check answers, one stronger model is shown each finding's
two evidence crops (as the reviewer sees them, GV's markup included), its item, its claimed values
and its source, and asked to find what is wrong with it, four questions per finding:

- `same_item`: is it the same item on both drawings?
- `right_dimension`: is the architect's value the dimension for that item, not a neighbouring one?
- `vendor_ink`: is the vendor's value the vendor's own black ink, not GV's coloured note?
- `architect_side`: is the reference drawing what the source says (the architect's drawing, not a
  brand prototype, for `architect`)?

Each answer is `yes`, `no` or `unsure`, with a short `note`. **Code reads the reply strictly**
(`parse_reply`): one JSON object, the asked-for fields only; anything else is `unsure` for the
whole batch, and never a crash. Then: any `no` is `objects` (the finding becomes To check and the
critic's note is shown); four `yes` is `passed`, a witness that is still AI, so it never makes a
finding Confirmed on its own; anything else is `unsure` (no effect).

Up to 8 findings per call and 4 calls per answer. The engine owns the model and the money
(`chat_read_review.py`): it passes `model_call`. Nothing here touches the network.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

__all__ = [
    "CRITIC_BATCH",
    "CRITIC_BATCHES",
    "CRITIC_EFFORT",
    "CRITIC_MAX_TOKENS",
    "CRITIC_MODEL",
    "CRITIC_PROMPT_TOKENS",
    "CRITIC_SCHEMA",
    "CRITIC_SOURCES",
    "QUESTIONS",
    "SYSTEM",
    "CriticVerdict",
    "batches",
    "candidates",
    "criticise",
    "parse_reply",
    "verdict_of",
]

type CriticResult = Literal["passed", "objects", "unsure"]
type Answer = Literal["yes", "no", "unsure"]

CRITIC_MODEL: Final = "sonnet"
CRITIC_EFFORT: Final = "low"
CRITIC_MAX_TOKENS: Final = 3000
#: Findings per call, and calls per answer.
CRITIC_BATCH: Final = 8
CRITIC_BATCHES: Final = 4
#: An upper bound of one call's prompt: two crops a finding (at most 1600 px a side, about 3,400
#: tokens each on Sonnet 5.5) and the words.
CRITIC_PROMPT_TOKENS: Final = CRITIC_BATCH * 2 * 3_400 + 4_000
#: The findings the critic can judge: those compared against a drawing (the architect's or a brand
#: prototype). A GV rule result, the vendor's own arithmetic and GV's recorded result are code's.
CRITIC_SOURCES: Final = ("architect", "brand_prototype")
QUESTIONS: Final = ("same_item", "right_dimension", "vendor_ink", "architect_side")
_ANSWERS: Final = frozenset({"yes", "no", "unsure"})
_LONGEST_NOTE: Final = 200
_LONGEST_TEXT: Final = 160
_FENCED: Final = re.compile(r"^```(?:json)?\s*(?P<body>.*?)\s*```$", re.DOTALL)

#: What code says when the critic answers "no" without a note.
_PLAIN_NO: Final = {
    "same_item": "the two drawings may not show the same item",
    "right_dimension": "the architect's value may be a neighbouring dimension",
    "vendor_ink": "the vendor's value may be GV's coloured note, not the vendor's ink",
    "architect_side": "the reference may not be the architect's drawing",
}

SYSTEM: Final = (
    "You check another reviewer's findings on millwork shop drawings, adversarially: look for the "
    "reason each finding is wrong. Answer only from what you can see in the pictures."
)

_INSTRUCTIONS: Final = (
    "Each finding above came with two crops: first the reference drawing (the architect's "
    "drawing, or a brand prototype when the source says so), then the vendor's shop drawing. GV's "
    "reviewers have drawn coloured markup (red, yellow, blue or green notes, clouds and numbers) on "
    "top of the drawings; the drawings' own ink is black or grey.\n"
    'For each finding, answer four questions, each "yes", "no" or "unsure":\n'
    "- same_item: is the item the same thing on both crops?\n"
    "- right_dimension: is the claimed reference value the dimension of that item, not a "
    "neighbouring dimension?\n"
    "- vendor_ink: is the claimed vendor value printed in the vendor's own black or grey ink, not "
    "a coloured GV note?\n"
    "- architect_side: is the reference crop what the source says (for source architect, the "
    "architect's drawing and not a brand prototype)?\n"
    'note: one short sentence saying what is wrong when any answer is no, else "".\n'
    "Answer for every finding, in the order shown, under its id."
)

_ANSWER: Final[Mapping[str, Any]] = {"type": "string", "enum": ["yes", "no", "unsure"]}
#: The reply's shape (structured outputs: the API returns exactly this JSON).
CRITIC_SCHEMA: Final[Mapping[str, Any]] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "same_item",
                    "right_dimension",
                    "vendor_ink",
                    "architect_side",
                    "note",
                ],
                "properties": {
                    "id": {"type": "string", "description": "The finding's id (F1)"},
                    "same_item": {**_ANSWER, "description": "The same item on both crops"},
                    "right_dimension": {
                        **_ANSWER,
                        "description": "The reference value is that item's own dimension",
                    },
                    "vendor_ink": {
                        **_ANSWER,
                        "description": "The vendor value is the vendor's black or grey ink",
                    },
                    "architect_side": {
                        **_ANSWER,
                        "description": "The reference crop is what its source says",
                    },
                    "note": {"type": "string", "description": "What is wrong, or empty"},
                },
            },
        }
    },
}

_SOURCE_WORDS: Final = {
    "architect": "the architect's drawing",
    "brand_prototype": "a brand prototype drawing",
}


@dataclass(frozen=True, slots=True)
class CriticVerdict:
    """The critic's answers on one finding, and code's reading of them."""

    result: CriticResult
    answers: Mapping[str, Answer] = field(default_factory=dict)
    note: str = ""

    def as_dict(self, model: str) -> dict[str, Any]:
        return {
            "result": self.result,
            "answers": dict(self.answers),
            "note": self.note,
            "model": model,
        }


UNSURE: Final = CriticVerdict(result="unsure")


def candidates(findings: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The findings the critic can judge (compared against a drawing, both crops made),
    differences first: they are what a reviewer acts on."""
    chosen = [
        dict(finding)
        for finding in findings
        if finding.get("source") in CRITIC_SOURCES
        and finding.get("arch_picture_id")
        and finding.get("vendor_picture_id")
    ]
    order = {"different": 0, "check": 1}
    chosen.sort(key=lambda finding: order.get(str(finding.get("status")), 2))
    return chosen


def batches(findings: Sequence[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """At most `CRITIC_BATCHES` batches of at most `CRITIC_BATCH` findings."""
    out = [
        list(findings[start : start + CRITIC_BATCH])
        for start in range(0, len(findings), CRITIC_BATCH)
    ]
    return out[:CRITIC_BATCHES]


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())[:_LONGEST_TEXT] or "(none)"


def _image(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def criticise(
    batch: Sequence[Mapping[str, Any]],
    crops: Mapping[str, tuple[bytes, bytes]],
    model_call: Callable[[list[dict[str, Any]]], str],
) -> dict[str, CriticVerdict]:
    """One call for one batch: each finding's words and its two crops (`crops`: finding id ->
    (reference png, vendor png)), then the questions. A finding without both crops is not asked.
    Raises what `model_call` raises (the caller records the critic as not run)."""
    asked = [finding for finding in batch if str(finding.get("id")) in crops]
    if not asked:
        return {}
    content: list[dict[str, Any]] = []
    for finding in asked:
        finding_id = str(finding.get("id"))
        content.append(
            {
                "type": "text",
                "text": (
                    f"Finding {finding_id}, page {_clean(finding.get('page'))}: "
                    f"item {_clean(finding.get('item'))}. "
                    f"Claimed reference value: {_clean(finding.get('architect'))}. "
                    f"Claimed vendor value: {_clean(finding.get('vendor'))}. "
                    f"Source: {_SOURCE_WORDS.get(str(finding.get('source')), 'a drawing')}. "
                    "Its two crops follow (reference, then vendor)."
                ),
            }
        )
        reference, vendor = crops[finding_id]
        content.append(_image(reference))
        content.append(_image(vendor))
    content.append({"type": "text", "text": _INSTRUCTIONS})
    return parse_reply(model_call(content), [str(finding.get("id")) for finding in asked])


def verdict_of(answers: Mapping[str, Answer], note: str) -> CriticVerdict:
    """Code's reading: any `no` objects (with the critic's note, or code's plain words), four
    `yes` pass, anything else is unsure."""
    if any(answers.get(question) == "no" for question in QUESTIONS):
        if not note:
            note = "; ".join(_PLAIN_NO[q] for q in QUESTIONS if answers.get(q) == "no")
        return CriticVerdict(result="objects", answers=answers, note=note[:_LONGEST_NOTE])
    if all(answers.get(question) == "yes" for question in QUESTIONS):
        return CriticVerdict(result="passed", answers=answers, note="")
    return CriticVerdict(result="unsure", answers=answers, note="")


def parse_reply(reply: object, ids: Sequence[str]) -> dict[str, CriticVerdict]:
    """The reply read strictly: `{"findings": [...]}` and nothing else, each entry with exactly
    `id`, the four questions (`yes` / `no` / `unsure`) and `note` (text). A reply that is not that
    object is `unsure` for every finding; an entry that is not right, a finding named twice, or a
    finding not answered is `unsure` for that finding. An id not asked about is ignored."""
    verdicts: dict[str, CriticVerdict] = {finding_id: UNSURE for finding_id in ids}
    if not isinstance(reply, str):
        return verdicts
    text = reply.strip()
    fenced = _FENCED.match(text)
    if fenced is not None:
        text = fenced.group("body")
    try:
        answer = json.loads(text)
    except ValueError:
        return verdicts
    if not isinstance(answer, dict) or set(answer) != {"findings"}:
        return verdicts
    entries = answer["findings"]
    if not isinstance(entries, list):
        return verdicts
    seen: dict[str, int] = {}
    parsed: dict[str, CriticVerdict] = {}
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        finding_id = entry["id"].strip()
        if finding_id not in verdicts:
            continue
        seen[finding_id] = seen.get(finding_id, 0) + 1
        if set(entry) != {"id", *QUESTIONS, "note"} or not isinstance(entry["note"], str):
            continue
        answers = {question: entry[question] for question in QUESTIONS}
        if not all(isinstance(value, str) and value in _ANSWERS for value in answers.values()):
            continue
        note = " ".join(entry["note"].split())
        parsed[finding_id] = verdict_of(answers, note)
    for finding_id, verdict in parsed.items():
        if seen.get(finding_id) == 1:
            verdicts[finding_id] = verdict
    return verdicts
