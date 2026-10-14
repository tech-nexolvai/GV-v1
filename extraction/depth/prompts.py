"""The depth and sink reader's three questions, pinned by id (#1265, design §5).

Each question is asked of both Claude readers, with a fixed answer shape (strict JSON, sent as the
request's structured output) and **"unsure" always allowed**: a reader that cannot tell says so,
and that is never a wrong answer. Every question tells the reader to ignore GV's yellow, red and
green marks: on second-round sheets they are baked into the vendor's pasted drawing and do reach
the picture (`pictures.py`).

* **`view-kind-v1`**: what kind of drawing one view is, from a picture with its printed heading
  painted out, so this judgment stays independent of code's, which reads the heading.
* **`depth-span-role-v1`**: which numbered dimension is the stone's full depth, the sink hole's
  width and front-to-back size, and the strip in front of the hole (and where that strip starts);
  and which lettered outline is the hole. A number, a letter, "none" or "unsure".
* **`claude-depth-span-v1`**: the width reader's span question (`claude-slot-span-v5`) for a
  dimension that runs top to bottom on a plan, or across a cut through the counter: copy the label
  exactly as printed. Its answer shape is v5's, so the shared two-reader agreement (`seal`) reads it.

A question's words depend only on what its pictures show (how many numbered dimensions, how many
lettered outlines, which way the dimension runs), and those counts are in the question packet, so
a stored answer is reused only for the identical question (`workflow/reader_reuse.py`).

Every example value in these questions is invented.

Source: issue #1265 · Verification: `tests/extraction/depth/test_prompts.py`
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final, Literal

from extraction.slot_reader.claude_output import SPAN_SCHEMA

__all__ = [
    "AI_VIEW_KINDS",
    "DEPTH_PROMPT_IDS",
    "DEPTH_SPAN_PROMPT_ID",
    "DEPTH_SPAN_SCHEMA",
    "OFFSET_STARTS",
    "PICK_NONE",
    "PICK_UNSURE",
    "SPAN_ROLE_PROMPT_ID",
    "VIEW_KIND_PROMPT",
    "VIEW_KIND_PROMPT_ID",
    "VIEW_KIND_SCHEMA",
    "DepthQuestion",
    "SpanDirection",
    "depth_span_prompt",
    "outline_letters",
    "span_role_prompt",
    "span_role_schema",
]

VIEW_KIND_PROMPT_ID: Final = "view-kind-v1"
SPAN_ROLE_PROMPT_ID: Final = "depth-span-role-v1"
DEPTH_SPAN_PROMPT_ID: Final = "claude-depth-span-v1"
#: The three pinned ids, registered with the shared route (`slot_reader.bedrock.job_prompt_id`).
DEPTH_PROMPT_IDS: Final = frozenset(
    {VIEW_KIND_PROMPT_ID, SPAN_ROLE_PROMPT_ID, DEPTH_SPAN_PROMPT_ID}
)

#: The kinds a reader may name. One answer for a section and a side view, as the scorer reads it
#: (`eval/pariksha/depth.py`, `section_or_side`).
AI_VIEW_KINDS: Final[tuple[str, ...]] = (
    "front",
    "plan",
    "section_or_side",
    "detail",
    "other",
    "unsure",
)
PICK_NONE: Final = "none"
PICK_UNSURE: Final = "unsure"
#: Where the strip in front of the sink hole is measured from, or that there is no such strip.
OFFSET_STARTS: Final[tuple[str, ...]] = ("stone_front_edge", "cabinet_face", "unsure", "none")

_MARKS: Final = (
    "Ignore GV's yellow, red and green marks, and any blue or purple ones: yellow boxes, red or "
    "blue writing, clouds and ticks are the reviewer's, never the vendor's drawing."
)


def _object(properties: Mapping[str, Mapping[str, object]]) -> dict[str, object]:
    return {
        "type": "object",
        "properties": {name: dict(schema) for name, schema in properties.items()},
        "required": list(properties),
        "additionalProperties": False,
    }


_WHY: Final = {"type": "string"}

VIEW_KIND_PROMPT: Final = (
    "This picture is one drawing cut from a vendor's shop drawing of cabinets and a stone "
    "countertop. Its printed title has been painted over on purpose: judge only what is drawn.\n"
    "What kind of drawing is it?\n"
    '- "front": a front view. You see cabinet doors and drawers standing up, and heights run up '
    "the page.\n"
    '- "plan": a plan or top view. You look down on the stone: a sink is a rectangle seen from '
    "above, often with a round drain, and the wall behind the stone is often a hatched or solid "
    "band along one long edge.\n"
    '- "section_or_side": a section or a side view: a cut through the counter, or the counter seen '
    "from its end. The stone is a thin slab lying on a cabinet's side profile.\n"
    '- "detail": a close-up of one small part, such as an edge profile or a sink rim.\n'
    '- "other": anything else, such as notes, a table, a title block or a 3D picture.\n'
    '- "unsure": you cannot tell. Being unsure is always allowed and is never a wrong answer.\n'
    f"{_MARKS}\n"
    'Return only this JSON: {"kind": "front|plan|section_or_side|detail|other|unsure", '
    '"why": "a few words"}'
)
VIEW_KIND_SCHEMA: Final = _object(
    {"kind": {"type": "string", "enum": list(AI_VIEW_KINDS)}, "why": _WHY}
)


def outline_letters(outlines: int) -> tuple[str, ...]:
    """The letters the lettered-outlines picture prints, A first."""
    if isinstance(outlines, bool) or not 0 <= outlines <= 26:
        raise ValueError("a picture letters between 0 and 26 outlines")
    return tuple(chr(ord("A") + index) for index in range(outlines))


def _check_counts(spans: int, outlines: int) -> None:
    if isinstance(spans, bool) or not isinstance(spans, int) or spans < 1:
        raise ValueError("a span-role question shows at least one numbered dimension")
    outline_letters(outlines)


def span_role_schema(spans: int, outlines: int) -> dict[str, object]:
    """The answer shape of `depth-span-role-v1` for a picture with `spans` numbered dimensions and
    `outlines` lettered outlines: each role is one word from a closed list per picture (`1`..`n`,
    `none`, `unsure`), so a number that is not drawn cannot be given."""
    _check_counts(spans, outlines)
    pick = {
        "type": "string",
        "enum": [*(str(number) for number in range(1, spans + 1)), PICK_NONE, PICK_UNSURE],
    }
    return _object(
        {
            "depth": pick,
            "cutout_width": pick,
            "cutout_depth": pick,
            "front_offset": pick,
            "front_offset_start": {"type": "string", "enum": list(OFFSET_STARTS)},
            "hole_outline": {
                "type": "string",
                "enum": [*outline_letters(outlines), PICK_NONE, PICK_UNSURE],
            },
            "why": _WHY,
        }
    )


def span_role_prompt(*, spans: int, outlines: int, plan: bool) -> str:
    """The words of `depth-span-role-v1`: Picture 1 the numbered dimensions, Picture 2 (when
    `outlines` > 0) the lettered sink outlines."""
    _check_counts(spans, outlines)
    letters = outline_letters(outlines)
    view = (
        "a plan (top) view: you look down on the stone"
        if plan
        else "a section or side view: a cut through the counter, or the counter seen from its end"
    )
    lettered = (
        ""
        if not outlines
        else (
            " Picture 2 is the same drawing with each closed outline drawn round the sink framed "
            f"in colour and lettered {letters[0]}"
            + ("" if len(letters) == 1 else f" to {letters[-1]}")
            + "."
        )
    )
    hole = (
        '- "hole_outline": which lettered outline in Picture 2 is the hole cut in the stone for '
        "the sink (not the sink's bowl, rim or basin drawn inside or around it)."
        if outlines
        else '- "hole_outline": no outlines are lettered for this drawing, so answer "none".'
    )
    return (
        f"Picture 1 is one drawing cut from a vendor's shop drawing of a stone countertop: {view}. "
        "Its printed title has been painted over. Code has drawn every dimension line it found in "
        "its own colour, with a number on a small square beside the line, numbered 1 to "
        f"{spans}.{lettered}\n"
        "Which numbered dimension measures each of these? Judge from what is drawn.\n"
        '- "depth": the stone\'s full depth, from its front edge to the wall behind it.\n'
        '- "cutout_width": the hole cut in the stone for the sink, measured side to side.\n'
        '- "cutout_depth": that hole measured front to back.\n'
        '- "front_offset": the strip of stone in front of the sink hole, from the front up to the '
        "hole's front edge.\n"
        '- "front_offset_start": where that strip\'s dimension starts. "stone_front_edge" if it '
        'starts at the stone\'s own front edge (so the overhang is included), "cabinet_face" if it '
        'starts at the face of the cabinet below, set back from the stone\'s edge, "unsure" if '
        'you cannot tell, and "none" when front_offset is "none" or "unsure".\n'
        f"{hole}\n"
        'Answer each with a number from Picture 1 (a letter for hole_outline), "none" if nothing '
        'drawn measures it, or "unsure" if you cannot tell. Being unsure is always allowed and is '
        "never a wrong answer. Never give one number for two different things. Judge only the "
        f"vendor's black drawing. {_MARKS}\n"
        'Return only this JSON: {"depth": "...", "cutout_width": "...", "cutout_depth": "...", '
        '"front_offset": "...", "front_offset_start": "...", "hole_outline": "...", '
        '"why": "a few words"}'
    )


type SpanDirection = Literal["down_the_plan", "across_the_plan", "across_the_section"]


def depth_span_prompt(*, direction: SpanDirection, turned: bool) -> str:
    """The words of `claude-depth-span-v1`: `claude-slot-span-v5`'s reading rules for one
    dimension of a plan or a section. `turned` says Pictures 3 and 4 are Picture 2 turned a
    quarter turn each way (a dimension whose label is printed sideways)."""
    runs = {
        "down_the_plan": (
            "On this plan the dimension runs top to bottom, front to back across the stone, so "
            "its label is usually printed sideways."
        ),
        "across_the_plan": "On this plan the dimension runs left to right.",
        "across_the_section": (
            "This drawing is a cut through the counter; the dimension runs left to right across it."
        ),
    }[direction]
    pictures = (
        " Picture 3 is Picture 2 turned a quarter turn clockwise and Picture 4 a quarter turn the "
        "other way, so a label printed sideways stands upright in one of them. They show the same "
        "dimension and the same label: read it in whichever picture it stands upright."
        if turned
        else ""
    )
    return (
        "Picture 1 shows one drawing from a vendor's shop drawing of a stone countertop. One "
        "dimension line is drawn over in magenta (pink-purple) with a number beside it; magenta is "
        "never the reviewer's colour. Picture 2 is a sharper close-up of that dimension: two short "
        "magenta bars across its line show where it starts and ends, and the dimension is the "
        f"stretch between them. {runs}{pictures}\n"
        "Does the printed dimension label of that dimension appear in Picture 2? The label is the "
        "one printed for that stretch, usually beside the middle of its line. Never use a "
        "neighbouring dimension's label. "
        f"{_MARKS} A number in the reviewer's marks is never the vendor's label.\n"
        "If the label belongs, copy its characters exactly as printed, including inch marks, "
        "fractions and words. Do not calculate, convert, correct or complete the text. Write it on "
        "one line in plain keyboard characters. Write a fraction as numerator/denominator with one "
        'space after the whole number, such as 18 5/16" - never 185/16"; a stacked fraction is '
        "written the same way. Put an inch mark only where one is printed. If two units are "
        "printed, such as millimetres with the inches in brackets, copy both exactly as printed, "
        "for example 330 [13]: the inch value in brackets is the one that counts.\n"
        '- "belongs": "yes" if you are sure the label you copy belongs to the marked dimension. '
        '"no" only if you are sure it has no printed label of its own. "unsure" if you cannot '
        "tell, for example two labels could belong to it, or the label may be cut off or hidden "
        'under the reviewer\'s marks. Being unsure is never a no. When the answer is not "yes", '
        "return an empty text.\n"
        '- "stacked": true if a fraction in the label is drawn with its numerator above its '
        "denominator.\n"
        '- "combined": true only if the label is itself a sum, an expression, a count or has '
        "words. A single dimension is not combined, even with a stacked fraction; millimetres with "
        "inches in brackets are not combined.\n"
        '- "readable": false if any character of the label is cut off, overlapped, blurred, or you '
        "are not sure of it.\n"
        '- "no_dimension": true if Picture 2 shows no dimension label for it at all.\n'
        'Return only this JSON: {"belongs": "yes|no|unsure", "text": "exact printed label or '
        'empty string", "stacked": true|false, "combined": true|false, "readable": true|false, '
        '"no_dimension": true|false}'
    )


#: v5's answer shape, field for field: the shared agreement (`seal.seal_label`) reads it.
DEPTH_SPAN_SCHEMA: Final = SPAN_SCHEMA


@dataclass(frozen=True, slots=True)
class DepthQuestion:
    """One depth-reader question, as the shared route asks it (`slot_reader.bedrock.CropJob.depth`).

    `pictures` are shown in order, then `prompt`; `schema` is the strict answer shape. `spans` and
    `outlines` are the counts a span-role answer is checked against (0 for the other questions).
    """

    prompt_id: str
    pictures: tuple[bytes, ...]
    prompt: str
    schema: Mapping[str, object] = field(repr=False)
    spans: int = 0
    outlines: int = 0

    def __post_init__(self) -> None:
        if self.prompt_id not in DEPTH_PROMPT_IDS:
            raise ValueError(f"{self.prompt_id!r} is not one of the depth reader's pinned prompts")
        if not self.pictures:
            raise ValueError("a depth-reader question shows at least one picture")
        if self.prompt_id == SPAN_ROLE_PROMPT_ID:
            _check_counts(self.spans, self.outlines)
            if len(self.pictures) != (2 if self.outlines else 1):
                raise ValueError(
                    "a span-role question shows its outlines picture only with letters"
                )
