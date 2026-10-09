"""The crop readers: one generic question per code-made crop, asked in parallel (#987).

**Generic on purpose.** The prompt names no client value and no client drawing: its examples are
invented. It asks only for the label's text as printed and four flags that can hold a reading back;
it does not ask for whole, numerator and denominator, because those are never used (E2 guard 2).

**Bounded like the form reader.** The same per-model start-rate pacer, the same thread-local Bedrock
clients, the same concurrency cap and throttle back-off, the same one re-ask for a malformed answer
and then an abstention — which sends that crop to the person, never the whole set. Kimi K3 is asked
at low effort, as the form reader asks it (#978).

**The wall question rides in the same batch (#992)**: a job with a second picture asks E3's narrow
yes/no question about the row's walls instead (`walls.WALL_PROMPT`), under the same pacer, so a
model's request rate never doubles. Every attempt's raw text is kept on its usage record, which the
worker stores privately (#985).

**The two Claude readers (#1051)** are asked the same questions with a fixed answer shape and a
stated effort (`claude_output.py`), carried under a key only the Claude adapters read, so a
Bedrock reader's request is exactly as before. A Claude reply is checked strictly against its
shape; a refusal, a token-limit stop or a shape miss is malformed, re-asked once, then abstains. A
picture the API would resize is refused before the call, and that question abstains with the
reason recorded. A span whose label is drawn sideways also gets an upright copy of its close-up as
a third picture, and the prompt says which picture that is.

Source: issues #987, #992, #1051 · Verification: `tests/extraction/slot_reader/test_bedrock.py`
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, ValidationError

from extraction.form_reader.bedrock import (
    KIMI_EFFORT,
    KIMI_K3_MODEL,
    AttemptUsage,
    ConverseClient,
    MalformedFormAnswer,
    _extract_json_object,
    _response_text,
    _response_usage,
)
from extraction.form_reader.pricing import RateLookup, require_priced_readers
from extraction.form_reader.runner import ClientProvider, ModelPacer, _is_throttle
from extraction.product_context import product_context_line, with_product
from extraction.slot_reader.claude_output import (
    ARCH_MEASURES,
    ARCH_PAIR_SCHEMA,
    COUNTER_BREAK_SCHEMA,
    CROP_SCHEMA,
    DEFAULT_CLAUDE_EFFORT,
    OUTPUT_CONFIG_KEY,
    ROW_CHOICE_SCHEMA,
    SPAN_SCHEMA,
    WALL_SCHEMA,
    ClaudeEffort,
    PictureWouldBeResized,
    RowKind,
    claude_answer,
    is_claude_model,
    output_config,
    require_picture_fits,
)
from extraction.slot_reader.seal import ReaderAnswer
from extraction.slot_reader.walls import WALL_PROMPT, WALL_PROMPT_ID, Side, WallAnswer
from vocabulary.semantic_types import ProductType

if TYPE_CHECKING:
    from extraction.slot_reader.anthropic import BatchSpendGuard

__all__ = [
    "ARCH_PAIR_PROMPT_ID",
    "ARCH_PAIR_PROMPT_IDS",
    "CLAUDE_SPAN_PROMPT",
    "CLAUDE_SPAN_PROMPT_ID",
    "CLAUDE_SPAN_PROMPT_IDS",
    "CLAUDE_UPRIGHT_NOTE",
    "COUNTER_BREAK_PROMPT",
    "COUNTER_BREAK_PROMPT_ID",
    "COUNTER_BREAK_PROMPT_IDS",
    "CROP_PROMPT",
    "CROP_PROMPT_ID",
    "ROW_BOX_COLOURS",
    "ROW_BOX_COLOUR_WORDS",
    "ROW_PROMPT",
    "ROW_PROMPT_ID",
    "ROW_PROMPT_IDS",
    "ArchPairAnswer",
    "CounterBreakAnswer",
    "CropJob",
    "RowChoiceAnswer",
    "arch_pair_prompt",
    "build_arch_pair_request",
    "build_counter_break_request",
    "build_crop_request",
    "build_row_request",
    "build_wall_request",
    "crop_prompt_id",
    "parse_stored_reader_answer",
    "read_arch_pair",
    "read_counter_break",
    "read_crop",
    "read_crops_parallel",
    "read_row_choice",
    "read_walls",
]

CROP_PROMPT_ID: Final = "slot-crop-v1"
ROW_PROMPT_ID: Final = "slot-row-choice-v3"
"""v3 (#1108): the answer says what kind of answer it is (`ROW_KINDS`). v2 gave one answer, 0, for
two different things: "the sheet has no stone countertop" and "a countertop is drawn but none of
the numbered boxes is its row"; since #1093 the first is only listed, so the second slipped past the
reviewer. v3 also asks for a second countertop's row (`also`), which V1 does not read but lists, and
names the box colours, none of which the reviewer's markup uses (box 1 used to be crimson while the
same question said red numbers are the reviewer's)."""
#: Earlier wordings of the row question, still recognised when a stored run is replayed.
ROW_PROMPT_IDS: Final = frozenset({ROW_PROMPT_ID, "slot-row-choice-v2", "slot-row-choice-v1"})
CLAUDE_SPAN_PROMPT_ID: Final = "claude-slot-span-v3"
"""v3 (#1104): every answer field is defined, as `CROP_PROMPT` defines them. v2 named `stacked`,
`combined`, `readable` and `no_dimension` without saying what they mean, so each reader guessed: on
the keyed set one reader called a plain piece label "combined" because the close-up also shows its
neighbours' labels, and that one answer held a label both readers had copied right.
v2 (#1051): a fixed answer shape and stated effort, and an upright third picture for a span whose
label is drawn sideways (`CLAUDE_UPRIGHT_NOTE`)."""
#: Earlier wordings, still recognised when a stored run is replayed.
CLAUDE_SPAN_PROMPT_IDS: Final = frozenset(
    {CLAUDE_SPAN_PROMPT_ID, "claude-slot-span-v2", "claude-slot-span-v1"}
)
COUNTER_BREAK_PROMPT_ID: Final = "claude-counter-break-v2"
ARCH_PAIR_PROMPT_ID: Final = "arch-pair-v2"
"""The architect-pairing question (#1053), asked of both Claude readers on every page with an
architect dimension code has not refused. v2 first asks what EVERY numbered architect dimension
measures (`ARCH_MEASURES`), then the pairing; code accepts a pair only when what it measures can be
the same thing (`workflow/architect_pairing.py`). v1 asked only for the pairing, and only when code
could not pair by drawn position."""
#: Earlier wordings, still recognised in stored records and invocations.
ARCH_PAIR_PROMPT_IDS: Final = frozenset({ARCH_PAIR_PROMPT_ID, "arch-pair-v1"})
#: Earlier wordings, still recognised when a stored run is replayed (v1 asked only about appliances).
COUNTER_BREAK_PROMPT_IDS: Final = frozenset({COUNTER_BREAK_PROMPT_ID, "claude-counter-break-v1"})


def parse_stored_reader_answer(text: str) -> Mapping[str, Any]:
    """Parse one append-only stored reply through the same JSON boundary as live calls."""
    return _extract_json_object(text)


#: The row picture's numbered box colours, box 1 first, with the words the row question uses for
#: them (#1108). None is a colour of the reviewer's markup (red or blue numbers, yellow boxes;
#: `extraction/ink.py`) or the vendor's black, and each is dark enough for the white number on it.
ROW_BOX_COLOURS: Final[tuple[tuple[str, bytes], ...]] = (
    ("green", bytes((0, 135, 70))),
    ("magenta", bytes((200, 0, 160))),
    ("brown", bytes((140, 85, 20))),
    ("purple", bytes((120, 50, 170))),
    ("teal", bytes((0, 125, 125))),
    ("lime", bytes((70, 150, 0))),
)
ROW_BOX_COLOUR_WORDS: Final = (
    ", ".join(name for name, _ in ROW_BOX_COLOURS[:-1]) + f" and {ROW_BOX_COLOURS[-1][0]}"
)


ROW_PROMPT: Final = (
    "This is a vendor's cabinet shop drawing sheet. The vendor drew it in black ink. A reviewer may "
    "have marked it up: numbers in red or blue, and yellow boxes, are the reviewer's markup, not "
    "the vendor's; ignore them. "
    "Numbered coloured boxes mark candidate dimension rows that were found on the drawing. They are "
    f"drawn in {ROW_BOX_COLOUR_WORDS}, colours the reviewer's markup never uses; each box's number "
    "is in the filled square of the same colour just left of it. "
    "Task: which numbered box marks the COUNTERTOP PIECE ROW — the horizontal chain of piece "
    "widths (fillers, cabinets, appliance spaces) on the vendor's FRONT VIEW / ELEVATION that "
    "runs along the stone countertop and measures the pieces under it from one end of the stone "
    "top to the other? It is NOT an upper-cabinet row, NOT a wall-to-wall or room dimension, NOT "
    "a row inside a plan (top) view or a section view, and NOT the architect's small drawing.\n"
    "Answer fields:\n"
    '- "kind": exactly one of these four words.\n'
    '  "row": one numbered box is the countertop piece row.\n'
    '  "no_countertop": no stone countertop is drawn on this sheet at all (for example only a '
    "wardrobe, closet or tall unit), even when a box marks a chain of widths.\n"
    '  "not_among_boxes": a stone countertop is drawn on this sheet, but none of the numbered '
    "boxes is its piece row (for example its row has no box, or the only boxed rows are upper "
    "cabinets or a plan view).\n"
    '  "unsure": you cannot tell which of the three above is true.\n'
    '- "row": the number of that box when "kind" is "row"; 0 for every other kind.\n'
    '- "also": the numbers of other boxes that are the piece row of a SECOND, separate stone '
    "countertop on the same sheet (for example an island drawn beside the main run); never the "
    'box in "row". Usually empty: [].\n'
    '- "why": one short sentence.\n'
    "Invented examples, not from this sheet: "
    '{"row": 2, "kind": "row", "also": [], "why": "Box 2 runs under the stone top in the front '
    'view from end to end."} '
    '{"row": 1, "kind": "row", "also": [4], "why": "Box 1 is the sink run; box 4 is a separate '
    'island top."} '
    '{"row": 0, "kind": "no_countertop", "also": [], "why": "Only a tall pantry unit is drawn; '
    'there is no stone top."} '
    '{"row": 0, "kind": "not_among_boxes", "also": [], "why": "A stone top is drawn, but its '
    'piece widths are inside no numbered box."} '
    '{"row": 0, "kind": "unsure", "also": [], "why": "Two boxes could each be the stone top\'s '
    'row."}\n'
    "Reply with ONLY one JSON object of that shape: "
    '{"row": <number>, "kind": "<kind>", "also": [<numbers>], "why": "<one short sentence>"}'
)


def arch_pair_prompt(*, vendor_pieces: int, architect_spans: int) -> str:
    """The architect-pairing question (`arch-pair-v2`) for one picture with `vendor_pieces` red and
    `architect_spans` blue numbered marks (#1053).

    It asks what physical thing every marked architect dimension measures, then which ones measure
    the same physical thing as the vendor's pieces and run. It never asks for, or lets the model
    use, a printed number: pairing by value is circular (it could only pair numbers that already
    agree), and the comparison is exact arithmetic done afterwards by code. Code, not the model,
    then decides which measures may pair with what.
    """
    if vendor_pieces < 1 or architect_spans < 1:
        raise ValueError("an architect-pairing question needs at least one mark of each kind")
    return (
        "This sheet shows an architect's elevation and a vendor's cabinet shop drawing of the same "
        "run of cabinets (black ink is the drawings'; ignore coloured reviewer marks). The vendor's "
        "countertop row is outlined in red; its pieces are numbered "
        f"V1 to V{vendor_pieces}, left to right, in the red tags. Some of the architect's "
        "dimensions are outlined in blue and numbered "
        f"A1 to A{architect_spans} in the blue tags. "
        "Judge everything only by where each dimension's two ends (its tick marks or arrows) are "
        "drawn and which drawn lines they touch. Do not compare, read or add up the printed "
        "numbers, and never pair two dimensions because their numbers are equal or close: the "
        "numbers are compared later. "
        f"Step one: for EVERY architect dimension, A1 to A{architect_spans}, say what physical "
        "thing it measures, with exactly one of these words: "
        '"countertop" (the stone or counter top, end to end); '
        '"cabinet_run" (a run of several cabinets, end to end); '
        '"single_cabinet" (one cabinet box); '
        '"filler_or_end_panel" (a filler, scribe or end panel); '
        '"wall_to_wall" (from one wall to the other); '
        '"clearance_or_gap" (a space between a wall and an object\'s edge, or between two '
        "objects); "
        '"blocking_or_backing" (blocking, backing or a hatched support strip, often in the wall); '
        '"fixture_or_appliance_centre" (to or from the centre line of an outlet, a sink, a faucet, '
        "an appliance, a light or an artwork); "
        '"appliance_opening" (an opening left for an appliance); '
        '"height_or_other" (a height, or anything else); '
        '"unsure" (you cannot tell). '
        "A dimension that runs to a centre line, to blocking or backing, or between a wall and an "
        "object's edge is never a cabinet or countertop width. "
        "Step two: for each vendor piece, and for the vendor's whole run (from the left end of V1 "
        f"to the right end of V{vendor_pieces}), give the A-number of the architect dimension that "
        "measures the SAME PHYSICAL THING: the same countertop, run, cabinet or filler, its two "
        "ends at the same drawn places. Several vendor pieces may together measure one architect "
        "dimension (the vendor split one cabinet or bay into pieces): give that A-number for each "
        "of those pieces. Use 0 where the architect prints no dimension of the same thing. "
        "Reply with ONLY a JSON object: "
        '{"architect": [{"a": <A-number>, "measures": "<one of the words above>"}, one entry for '
        f"each of A1 to A{architect_spans}], "
        '"overall": <A-number for the whole run, or 0>, '
        f'"pieces": [<A-number or 0 for V1>, ..., <for V{vendor_pieces}>], '
        '"why": "<one short sentence>"}'
    )


COUNTER_BREAK_PROMPT: Final = (
    "Picture 1 is the vendor's full drawing view. Picture 2 is the same view close around the "
    "marked countertop span. Look only inside the marked span. Is a tall appliance or tall unit "
    "drawn there, such as a refrigerator, oven or wall-oven tower, pantry, or tall cabinet? "
    "Answer yes only when the drawing lines show it physically occupies that span. Do not infer "
    "from a text label outside the span. Also say where the STONE TOP itself ends at the span's "
    'two ends: "to_walls" if the stone runs over the end pieces up to the walls; '
    '"short_of_ends" if the stone stops before an end, between full-height fillers, panels '
    'or tall units that rise past it; "into_walls" if the stone runs past the wall faces into '
    'the walls (a pocket or recess); "no_stone" if no stone top is drawn; otherwise '
    '"unsure". This is a hold-only safety question: an appliance or a stone that does not end '
    "at the walls sends the row to the reviewer; a no does not approve the row. Return only this JSON: "
    '{"contains_tall_appliance": true|false, "stone_ends": "to_walls|short_of_ends|into_walls|'
    'no_stone|unsure", "why": "short visual reason"}'
)

CROP_PROMPT: Final = (
    "This picture is cut from a cabinet shop drawing. It shows ONE dimension label with its "
    "dimension line and tick marks, or something that is not a dimension at all. Copy the label's "
    'characters EXACTLY as printed, including any inch mark ("). Do not convert, correct, complete '
    "or add anything. A label in millimetres with inches in brackets is copied with both, for "
    "example 250 [9 7/8].\n"
    "Return ONLY this JSON object:\n"
    '{"text": "the label exactly as printed, or an empty string if there is none",\n'
    ' "stacked": true if any fraction is drawn stacked (numerator above denominator), else false,\n'
    ' "combined": true if the label is a sum, an expression, a count or has words, such as '
    '4"+1" or (6EQ), else false,\n'
    ' "readable": false if any character is cut off at the picture\'s edge, overlapped, blurred, '
    "or you are not sure of it; else true,\n"
    ' "no_dimension": true if the picture shows no dimension label at all (only a symbol, an '
    "arrow, a letter or a line), else false}"
)

CLAUDE_SPAN_PROMPT: Final = (
    "Picture 1 is the full vendor shop drawing. A thin red box marks one span of the selected "
    "countertop dimension row. Picture 2 is a close-up of that same span. Is the printed "
    "dimension label in Picture 2 the label that belongs to the exact red-boxed span in Picture 1? "
    "Do not use a neighbour's label, an overall when a piece is marked, a height, or red/yellow "
    "reviewer markup. If it belongs, copy its characters exactly as printed, including inch marks, "
    "fractions, metric text in brackets, and words. Do not calculate, convert, correct, or complete "
    "the text. If the label does not belong to the marked span, is absent, or you are unsure, set "
    "belongs to false and return an empty text.\n"
    "The other fields describe only that one label, the one that belongs to the red-boxed span:\n"
    '- "stacked": true if a fraction in it is drawn with its numerator above its denominator.\n'
    '- "combined": true only if that label is itself a sum, an expression, a count or has words, '
    'such as 4"+1", 2"+1" Filler, 96"(4EQ) or INCLUDING FIELD CUT. A single dimension is not '
    "combined, even with a stacked fraction; a millimetre value with inches in brackets, such as "
    "610 [24], is not combined. Labels of neighbouring spans that also show in Picture 2 never make "
    "it combined.\n"
    '- "readable": false if any character of that label is cut off at the picture\'s edge, '
    "overlapped, blurred, or you are not sure of it.\n"
    '- "no_dimension": true if Picture 2 shows no dimension label for the span at all (only a '
    "symbol, an arrow, a letter or a line).\n"
    "Return only this JSON: "
    '{"belongs": true|false, "text": "exact printed label or empty string", '
    '"stacked": true|false, "combined": true|false, "readable": true|false, '
    '"no_dimension": true|false}'
)


#: Told to a reader shown a third picture (#1051). On both client sets every label drawn sideways
#: reads from bottom to top, so the copy is turned a quarter clockwise to stand upright.
CLAUDE_UPRIGHT_NOTE: Final = (
    "Picture 3 is Picture 2 turned a quarter turn clockwise, so that a label printed sideways "
    "stands upright. It shows the same span and the same label; use it to read characters that "
    "are sideways in Picture 2. Copy the label as printed, whichever picture you read it in."
)


def crop_prompt_id(product: ProductType | None = None) -> str:
    """The id a crop request is recorded under: `CROP_PROMPT_ID`, plus the product when the request
    carried the product line (#994)."""
    return with_product(CROP_PROMPT_ID, product)


class _CropAnswer(BaseModel):
    """The answer's shape, checked strictly. Extra keys are ignored: nothing reads them."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    text: StrictStr
    stacked: StrictBool
    combined: StrictBool
    readable: StrictBool
    no_dimension: StrictBool


class _GroundedCropAnswer(_CropAnswer):
    belongs: StrictBool


class _RowChoiceReply(BaseModel):
    """`kind` and `also` are new in `slot-row-choice-v3`; a reply of the v1/v2 shape still parses,
    with no kind and nothing else named."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    row: StrictInt
    why: StrictStr
    kind: RowKind | None = None
    also: tuple[StrictInt, ...] = ()


@dataclass(frozen=True, slots=True)
class RowChoiceAnswer:
    model_id: str
    row: int
    why: str
    kind: RowKind | None = None
    """What the answer says (`ROW_KINDS`, #1108). `None` for an answer of the v1/v2 shape, where
    0 meant either "no countertop" or "not among the boxes"."""
    also: tuple[int, ...] = ()
    """Other numbered boxes this reader named as a second countertop's piece row (#1108)."""


@dataclass(frozen=True, slots=True)
class ArchPairAnswer:
    """One reader's answer to the architect-pairing question: A-numbers, 0 for none (#1053)."""

    model_id: str
    overall: int
    pieces: tuple[int, ...]
    """One entry per vendor piece, V1 first."""
    why: str
    measures: tuple[str, ...] = ()
    """What each architect dimension measures (`ARCH_MEASURES`), A1 first; one entry per A-number
    (v2). Empty only for a v1 answer, which code never pairs on."""


class _ArchMeasure(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    a: StrictInt
    measures: StrictStr


class _ArchPairReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    architect: list[_ArchMeasure]
    overall: StrictInt
    pieces: list[StrictInt]
    why: StrictStr


@dataclass(frozen=True, slots=True)
class CounterBreakAnswer:
    model_id: str
    contains_tall_appliance: bool
    why: str
    stone_ends: str = "unsure"
    """Where the stone top ends at the span's ends (v2): `to_walls`, `short_of_ends`,
    `into_walls`, `no_stone` or `unsure`. Hold-only, like the appliance answer."""


def _base_model_id(model_id: str) -> str:
    return model_id.removeprefix("us.").removeprefix("global.")


def _with_claude_output(
    request: dict[str, Any], schema: Mapping[str, object], effort: ClaudeEffort
) -> dict[str, Any]:
    """A Claude request also states its answer schema and effort; any other reader's is unchanged."""
    if is_claude_model(request["modelId"]):
        request[OUTPUT_CONFIG_KEY] = output_config(schema, effort)
    return request


def _parsed_answer(
    model_id: str, response: Mapping[str, Any], raw: str, schema: Mapping[str, Any]
) -> Mapping[str, Any]:
    """A Claude reply is checked strictly against its schema; any other reader's as before."""
    if is_claude_model(model_id):
        return claude_answer(response, schema)
    return _extract_json_object(raw)


def build_crop_request(
    *,
    model_id: str,
    crop_png: bytes,
    max_tokens: int,
    product: ProductType | None = None,
    full_view_png: bytes | None = None,
    grounded_claude: bool = False,
    upright_png: bytes | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[str, Any]:
    """One slot question: whole marked vendor view, close-up, then the question.

    With a product (#994) the drawing set's product is one plain line of its own between the
    picture and the question. Without one the request is exactly as before.

    `upright_png` (grounded Claude only, #1051) is the close-up turned upright for a label drawn
    sideways: a third picture after the close-up, introduced by `CLAUDE_UPRIGHT_NOTE`.
    """
    if not model_id.strip():
        raise ValueError("a crop reader model id must be stated")
    if not crop_png.startswith(b"\x89PNG\r\n\x1a\n") or any(
        picture is not None and not picture.startswith(b"\x89PNG\r\n\x1a\n")
        for picture in (full_view_png, upright_png)
    ):
        raise ValueError("a slot reader is shown PNG pictures")
    if upright_png is not None and not (grounded_claude and full_view_png is not None):
        raise ValueError("an upright third picture belongs only to a grounded Claude span question")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    *(
                        []
                        if full_view_png is None
                        else [{"image": {"format": "png", "source": {"bytes": full_view_png}}}]
                    ),
                    {"image": {"format": "png", "source": {"bytes": crop_png}}},
                    *(
                        []
                        if upright_png is None
                        else [
                            {"image": {"format": "png", "source": {"bytes": upright_png}}},
                            {"text": CLAUDE_UPRIGHT_NOTE},
                        ]
                    ),
                    *([] if product is None else [{"text": product_context_line(product)}]),
                    {"text": CLAUDE_SPAN_PROMPT if grounded_claude else CROP_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return _with_claude_output(
        request, SPAN_SCHEMA if grounded_claude else CROP_SCHEMA, claude_effort
    )


def build_row_request(
    *,
    model_id: str,
    page_png: bytes,
    max_tokens: int,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[str, Any]:
    """Ask Opus to choose only among the code-ranked, numbered vendor-page candidates."""
    if not model_id.strip():
        raise ValueError("a row reader model id must be stated")
    if not page_png.startswith(_PNG_SIGNATURE):
        raise ValueError("a row reader requires a rendered PNG page")
    if isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": page_png}}},
                    {"text": ROW_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return _with_claude_output(request, ROW_CHOICE_SCHEMA, claude_effort)


def build_arch_pair_request(
    *,
    model_id: str,
    picture_png: bytes,
    vendor_pieces: int,
    architect_spans: int,
    max_tokens: int,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[str, Any]:
    """One numbered picture and the pairing question (#1053); Claude states its shape and effort."""
    if not model_id.strip():
        raise ValueError("an architect-pairing reader model id must be stated")
    if not picture_png.startswith(_PNG_SIGNATURE):
        raise ValueError("an architect-pairing question requires a rendered PNG picture")
    if isinstance(max_tokens, bool) or max_tokens < 1:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": picture_png}}},
                    {
                        "text": arch_pair_prompt(
                            vendor_pieces=vendor_pieces, architect_spans=architect_spans
                        )
                    },
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return _with_claude_output(request, ARCH_PAIR_SCHEMA, claude_effort)


def read_arch_pair(
    client: ConverseClient,
    *,
    model_id: str,
    picture_png: bytes,
    page_index: int,
    vendor_pieces: int,
    architect_spans: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> ArchPairAnswer:
    """Ask once, re-asking only a malformed answer: not one entry per vendor piece, an A-number
    outside the marks shown, or not exactly one known measure for every A-number shown. Malformed
    twice raises `MalformedFormAnswer` (the job abstains)."""
    for attempt in range(2):
        request = build_arch_pair_request(
            model_id=model_id,
            picture_png=picture_png,
            vendor_pieces=vendor_pieces,
            architect_spans=architect_spans,
            max_tokens=max_tokens,
            claude_effort=claude_effort,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    ARCH_PAIR_PROMPT_ID,
                    ARCH_PAIR_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            parsed = _ArchPairReply.model_validate(
                _parsed_answer(model_id, response, raw, ARCH_PAIR_SCHEMA)
            )
            numbers = (parsed.overall, *parsed.pieces)
            if len(parsed.pieces) != vendor_pieces:
                raise MalformedFormAnswer("the answer does not give one entry per vendor piece")
            if any(not 0 <= number <= architect_spans for number in numbers):
                raise MalformedFormAnswer("the answer names an A-number that is not marked")
            said = sorted(entry.a for entry in parsed.architect)
            if said != list(range(1, architect_spans + 1)):
                raise MalformedFormAnswer("the answer does not say once what every A measures")
            if any(entry.measures not in ARCH_MEASURES for entry in parsed.architect):
                raise MalformedFormAnswer("the answer gives a measure that is not offered")
            measures = {entry.a: entry.measures for entry in parsed.architect}
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    ARCH_PAIR_PROMPT_ID,
                    ARCH_PAIR_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "architect-pairing answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                ARCH_PAIR_PROMPT_ID,
                ARCH_PAIR_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return ArchPairAnswer(
            model_id=model_id,
            overall=parsed.overall,
            pieces=tuple(parsed.pieces),
            why=parsed.why[:300],
            measures=tuple(measures[a] for a in range(1, architect_spans + 1)),
        )
    raise AssertionError("unreachable")


class _CounterBreakReply(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    contains_tall_appliance: StrictBool
    stone_ends: Literal["to_walls", "short_of_ends", "into_walls", "no_stone", "unsure"] = "unsure"
    why: StrictStr


def build_counter_break_request(
    *,
    model_id: str,
    row_png: bytes,
    view_png: bytes,
    max_tokens: int,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[str, Any]:
    """Ask whether the selected row span contains a drawn tall-appliance bay; never read a value."""
    if not model_id.strip():
        raise ValueError("a counter-break reader model id must be stated")
    if not (row_png.startswith(_PNG_SIGNATURE) and view_png.startswith(_PNG_SIGNATURE)):
        raise ValueError("a counter-break reader is shown PNGs")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": view_png}}},
                    {"image": {"format": "png", "source": {"bytes": row_png}}},
                    {"text": COUNTER_BREAK_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return _with_claude_output(request, COUNTER_BREAK_SCHEMA, claude_effort)


def read_counter_break(
    client: ConverseClient,
    *,
    model_id: str,
    row_png: bytes,
    view_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> CounterBreakAnswer:
    """Ask once, re-asking only malformed JSON; a yes can only withhold the row."""
    for attempt in range(2):
        request = build_counter_break_request(
            model_id=model_id,
            row_png=row_png,
            view_png=view_png,
            max_tokens=max_tokens,
            claude_effort=claude_effort,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    COUNTER_BREAK_PROMPT_ID,
                    COUNTER_BREAK_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            parsed = _CounterBreakReply.model_validate(
                _parsed_answer(model_id, response, raw, COUNTER_BREAK_SCHEMA)
            )
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    COUNTER_BREAK_PROMPT_ID,
                    COUNTER_BREAK_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "counter-break answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                COUNTER_BREAK_PROMPT_ID,
                COUNTER_BREAK_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return CounterBreakAnswer(
            model_id=model_id,
            contains_tall_appliance=parsed.contains_tall_appliance,
            why=parsed.why[:300],
            stone_ends=parsed.stone_ends,
        )
    raise AssertionError("unreachable")


def read_row_choice(
    client: ConverseClient,
    *,
    model_id: str,
    page_png: bytes,
    page_index: int,
    candidate_count: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> RowChoiceAnswer:
    """Ask once, re-asking only malformed JSON; an out-of-range row is a reviewer decision."""
    if not 1 <= candidate_count <= 6:
        raise ValueError("row choice requires one to six code-ranked candidates")
    for attempt in range(2):
        request = build_row_request(
            model_id=model_id,
            page_png=page_png,
            max_tokens=max_tokens,
            claude_effort=claude_effort,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    ROW_PROMPT_ID,
                    ROW_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            parsed = _RowChoiceReply.model_validate(
                _parsed_answer(model_id, response, raw, ROW_CHOICE_SCHEMA)
            )
            if not 0 <= parsed.row <= candidate_count:
                raise MalformedFormAnswer("row choice is outside the numbered candidates")
            if parsed.kind is not None and (parsed.kind == "row") != (parsed.row > 0):
                raise MalformedFormAnswer("row choice's number and kind contradict each other")
            if any(not 1 <= other <= candidate_count for other in parsed.also):
                raise MalformedFormAnswer("a second row is outside the numbered candidates")
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    ROW_PROMPT_ID,
                    ROW_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "row answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                ROW_PROMPT_ID,
                ROW_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return RowChoiceAnswer(
            model_id=model_id,
            row=parsed.row,
            why=parsed.why[:300],
            kind=parsed.kind,
            also=tuple(sorted({other for other in parsed.also if other != parsed.row})),
        )
    raise AssertionError("unreachable")


def read_crop(
    client: ConverseClient,
    *,
    model_id: str,
    crop_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    product: ProductType | None = None,
    full_view_png: bytes | None = None,
    question_packet: Mapping[str, object] | None = None,
    grounded_claude: bool = False,
    upright_png: bytes | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> ReaderAnswer:
    """Ask one reader about one crop; re-ask once on a malformed answer, then raise."""
    prompt_id = CLAUDE_SPAN_PROMPT_ID if grounded_claude else crop_prompt_id(product)
    schema = SPAN_SCHEMA if grounded_claude else CROP_SCHEMA
    for attempt in range(2):
        request = build_crop_request(
            model_id=model_id,
            crop_png=crop_png,
            max_tokens=max_tokens,
            product=product,
            full_view_png=full_view_png,
            grounded_claude=grounded_claude,
            upright_png=upright_png,
            claude_effort=claude_effort,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            if grounded_claude:
                parsed_grounded = _GroundedCropAnswer.model_validate(
                    _parsed_answer(model_id, response, raw, schema)
                )
                parsed: _CropAnswer = parsed_grounded
                belongs = parsed_grounded.belongs
            else:
                parsed = _CropAnswer.model_validate(_parsed_answer(model_id, response, raw, schema))
                belongs = True
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    prompt_id,
                    prompt_id,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "crop answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                prompt_id,
                prompt_id,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return ReaderAnswer(
            model_id=model_id,
            text=parsed.text,
            readable=parsed.readable,
            no_dimension=parsed.no_dimension,
            stacked=parsed.stacked,
            combined=parsed.combined,
            belongs=belongs,
        )
    raise AssertionError("unreachable")


class _WallReply(BaseModel):
    """The wall answer's shape. A side outside yes/no/unsure makes the answer malformed."""

    model_config = ConfigDict(extra="ignore", frozen=True)

    left: StrictStr
    right: StrictStr
    behind: StrictStr
    view: StrictStr = ""
    left_evidence: StrictStr = ""
    right_evidence: StrictStr = ""
    behind_evidence: StrictStr = ""


def _side(value: str) -> Side:
    try:
        return Side(value.strip().lower())
    except ValueError as error:
        raise MalformedFormAnswer(f"a wall side must be yes, no or unsure: {value!r}") from error


def _wall_answer(model_id: str, reply: _WallReply) -> WallAnswer:
    view = reply.view.strip().lower()
    return WallAnswer(
        model_id=model_id,
        left=_side(reply.left),
        right=_side(reply.right),
        behind=_side(reply.behind),
        # Anything but a clear "plan" can never seal a back-only layout, so it is "other".
        view="plan" if view == "plan" else "elevation" if view == "elevation" else "other",
        evidence=(reply.left_evidence, reply.right_evidence, reply.behind_evidence),
    )


_PNG_SIGNATURE: Final = bytes((0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A))


def build_wall_request(
    *,
    model_id: str,
    row_png: bytes,
    view_png: bytes,
    max_tokens: int,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[str, Any]:
    """One wall question: the row picture, the whole view, then the question (E3's order)."""
    if not model_id.strip():
        raise ValueError("a wall reader model id must be stated")
    if not (row_png.startswith(_PNG_SIGNATURE) and view_png.startswith(_PNG_SIGNATURE)):
        raise ValueError("a wall reader is shown PNGs")
    if isinstance(max_tokens, bool) or max_tokens <= 0:
        raise ValueError("max_tokens must be a positive integer")
    request: dict[str, Any] = {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": row_png}}},
                    {"image": {"format": "png", "source": {"bytes": view_png}}},
                    {"text": WALL_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": max_tokens},
    }
    if _base_model_id(model_id) == KIMI_K3_MODEL:
        request["outputConfig"] = {"effort": KIMI_EFFORT}
    else:
        request["inferenceConfig"]["temperature"] = 0
    return _with_claude_output(request, WALL_SCHEMA, claude_effort)


def read_walls(
    client: ConverseClient,
    *,
    model_id: str,
    row_png: bytes,
    view_png: bytes,
    page_index: int,
    max_tokens: int,
    record_attempt: Callable[[AttemptUsage], None],
    question_packet: Mapping[str, object] | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> WallAnswer:
    """Ask one reader about one row's walls; re-ask once on a malformed answer, then raise.

    Every attempt's raw text is recorded with its usage (#985), privately, as the form reader's is.
    """
    for attempt in range(2):
        request = build_wall_request(
            model_id=model_id,
            row_png=row_png,
            view_png=view_png,
            max_tokens=max_tokens,
            claude_effort=claude_effort,
        )
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    WALL_PROMPT_ID,
                    WALL_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    page_index,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            raise
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw: str | None = None
        try:
            raw = _response_text(response)
            answer = _wall_answer(
                model_id,
                _WallReply.model_validate(_parsed_answer(model_id, response, raw, WALL_SCHEMA)),
            )
        except (MalformedFormAnswer, ValidationError) as error:
            record_attempt(
                AttemptUsage(
                    model_id,
                    WALL_PROMPT_ID,
                    WALL_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=page_index,
                    raw_response_text=raw,
                    attempt_number=attempt + 1,
                    question_packet=question_packet,
                )
            )
            if attempt:
                raise MalformedFormAnswer(
                    "wall answer remained malformed after one re-ask"
                ) from error
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                WALL_PROMPT_ID,
                WALL_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=page_index,
                raw_response_text=raw,
                attempt_number=attempt + 1,
                question_packet=question_packet,
            )
        )
        return answer
    raise AssertionError("unreachable")


@dataclass(frozen=True, slots=True)
class CropJob:
    """One reader, one crop. `key` is the caller's, to put the answer back where it belongs.

    For a label question, `png` is the close-up and `view_png` the marked full vendor view. A wall
    question sets `wall_question=True` and uses the same two-image shape with its own prompt.
    """

    key: str
    model_id: str
    page_index: int
    png: bytes
    view_png: bytes | None = None
    wall_question: bool = False
    row_question: bool = False
    counter_break_question: bool = False
    candidate_count: int | None = None
    grounded_claude: bool = False
    question_packet: Mapping[str, object] | None = None
    upright_png: bytes | None = None
    """A grounded Claude span whose label is drawn sideways: its close-up turned upright (#1051)."""
    arch_pair_question: bool = False
    """The architect-pairing question (#1053): `png` is the one numbered picture, and
    `vendor_pieces` / `architect_spans` say how many V and A marks it shows."""
    vendor_pieces: int | None = None
    architect_spans: int | None = None

    @property
    def pictures(self) -> tuple[bytes, ...]:
        """Every picture this job's request shows, in no particular order."""
        return tuple(
            picture
            for picture in (self.png, self.view_png, self.upright_png)
            if picture is not None
        )


def _job_prompt_id(job: CropJob, product: ProductType | None) -> str:
    if job.arch_pair_question:
        return ARCH_PAIR_PROMPT_ID
    if job.row_question:
        return ROW_PROMPT_ID
    if job.counter_break_question:
        return COUNTER_BREAK_PROMPT_ID
    if job.wall_question or (
        job.view_png is not None and job.question_packet is None and not job.grounded_claude
    ):
        return WALL_PROMPT_ID
    return CLAUDE_SPAN_PROMPT_ID if job.grounded_claude else crop_prompt_id(product)


def read_crops_parallel(
    jobs: Sequence[CropJob],
    *,
    clients: ClientProvider,
    rates: RateLookup | None,
    calls_per_minute: Mapping[str, int],
    max_concurrent_calls: int,
    max_tokens: int,
    max_throttle_retries: int,
    retry_backoff_seconds: float,
    record_attempt: Callable[[AttemptUsage], None],
    product: ProductType | None = None,
    spend_cap_usd: Decimal | None = None,
    spend_guard: BatchSpendGuard | None = None,
    pacer: ModelPacer | None = None,
    claude_effort: ClaudeEffort = DEFAULT_CLAUDE_EFFORT,
) -> dict[
    tuple[str, str],
    ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | ArchPairAnswer | None,
]:
    """Every job's answer by `(key, model_id)`; `None` where the reader abstained.

    A Claude job whose picture the API would resize is not sent: it abstains, and its attempt is
    recorded with that failure and no tokens (#1051).

    Label crops and wall questions share one pool, one pacer and one concurrency cap, so asking
    both never doubles a model's request rate.

    Refuses to start when any reader has no stated price or pacing limit, as the form reader does.
    """
    if isinstance(max_concurrent_calls, bool) or max_concurrent_calls <= 0:
        raise ValueError("max_concurrent_calls must be a positive integer")
    if isinstance(max_throttle_retries, bool) or max_throttle_retries < 0:
        raise ValueError("max_throttle_retries must be a non-negative integer")
    if retry_backoff_seconds <= 0:
        raise ValueError("retry_backoff_seconds must be positive")
    keys = [(job.key, job.model_id) for job in jobs]
    if len(set(keys)) != len(keys):
        raise ValueError("each crop is read once by each reader")
    readers = tuple(sorted({job.model_id for job in jobs}))
    if not readers:
        return {}
    missing = set(readers) - set(calls_per_minute)
    if missing:
        raise ValueError(f"missing per-model pacing limits for: {', '.join(sorted(missing))}")
    require_priced_readers(readers, rates)
    shared_pacer = pacer or ModelPacer(calls_per_minute)
    if spend_cap_usd is not None and spend_guard is not None:
        raise ValueError("pass either a Claude spend cap or its shared reservation guard, not both")
    if spend_cap_usd is not None:
        from extraction.slot_reader.anthropic import BatchSpendGuard

        spend_guard = BatchSpendGuard(spend_cap_usd, rates)

    def invoke(
        job: CropJob,
    ) -> tuple[
        tuple[str, str],
        ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | ArchPairAnswer | None,
    ]:
        if is_claude_model(job.model_id):
            try:
                require_picture_fits(job.pictures, job.model_id)
            except PictureWouldBeResized:
                record_attempt(
                    AttemptUsage(
                        job.model_id,
                        _job_prompt_id(job, product),
                        _job_prompt_id(job, product),
                        None,
                        None,
                        0,
                        False,
                        "PictureWouldBeResized",
                        job.page_index,
                        question_packet=job.question_packet,
                    )
                )
                return (job.key, job.model_id), None
        for throttle_attempt in range(max_throttle_retries + 1):
            shared_pacer.wait(job.model_id)
            try:
                client = clients.for_current_thread()
                if spend_guard is not None:
                    from extraction.slot_reader.anthropic import (
                        SpendCapExceeded,
                        SpendLimitedClient,
                    )

                    client = SpendLimitedClient(client, spend_guard)
                answer: (
                    ReaderAnswer
                    | WallAnswer
                    | RowChoiceAnswer
                    | CounterBreakAnswer
                    | ArchPairAnswer
                )
                if job.arch_pair_question:
                    pieces, spans = job.vendor_pieces, job.architect_spans
                    if (
                        isinstance(pieces, bool)
                        or isinstance(spans, bool)
                        or not isinstance(pieces, int)
                        or not isinstance(spans, int)
                    ):
                        raise ValueError("an architect-pairing question must state its numbering")
                    answer = read_arch_pair(
                        client,
                        model_id=job.model_id,
                        picture_png=job.png,
                        page_index=job.page_index,
                        vendor_pieces=pieces,
                        architect_spans=spans,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        question_packet=job.question_packet,
                        claude_effort=claude_effort,
                    )
                elif job.row_question:
                    count = job.candidate_count
                    if isinstance(count, bool) or not isinstance(count, int):
                        raise ValueError("a row question must record its candidate count")
                    answer = read_row_choice(
                        client,
                        model_id=job.model_id,
                        page_png=job.png,
                        page_index=job.page_index,
                        candidate_count=count,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        question_packet=job.question_packet,
                        claude_effort=claude_effort,
                    )
                elif job.counter_break_question:
                    if job.view_png is None:
                        raise ValueError(
                            "a counter-break question must include its full vendor view"
                        )
                    answer = read_counter_break(
                        client,
                        model_id=job.model_id,
                        row_png=job.png,
                        view_png=job.view_png,
                        page_index=job.page_index,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        question_packet=job.question_packet,
                        claude_effort=claude_effort,
                    )
                elif job.wall_question or (
                    job.view_png is not None
                    and job.question_packet is None
                    and not job.grounded_claude
                ):
                    if job.view_png is None:
                        raise ValueError("a wall question packet must include its full vendor view")
                    answer = read_walls(
                        client,
                        model_id=job.model_id,
                        row_png=job.png,
                        view_png=job.view_png,
                        page_index=job.page_index,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        question_packet=job.question_packet,
                        claude_effort=claude_effort,
                    )
                else:
                    answer = read_crop(
                        client,
                        model_id=job.model_id,
                        crop_png=job.png,
                        page_index=job.page_index,
                        max_tokens=max_tokens,
                        record_attempt=record_attempt,
                        product=product,
                        full_view_png=job.view_png,
                        question_packet=job.question_packet,
                        grounded_claude=job.grounded_claude,
                        upright_png=job.upright_png,
                        claude_effort=claude_effort,
                    )
                return (job.key, job.model_id), answer
            except (MalformedFormAnswer, PictureWouldBeResized):
                return (job.key, job.model_id), None
            except Exception as error:
                from extraction.slot_reader.anthropic import SpendCapExceeded

                if isinstance(error, SpendCapExceeded):
                    return (job.key, job.model_id), None
                if not _is_throttle(error) or throttle_attempt >= max_throttle_retries:
                    raise
                time.sleep(retry_backoff_seconds * (2**throttle_attempt))
        raise AssertionError("unreachable")

    answers: dict[
        tuple[str, str],
        ReaderAnswer | WallAnswer | RowChoiceAnswer | CounterBreakAnswer | ArchPairAnswer | None,
    ] = {}
    with ThreadPoolExecutor(max_workers=max_concurrent_calls) as executor:
        futures = [executor.submit(invoke, job) for job in jobs]
        for future in as_completed(futures):
            key, answer = future.result()
            answers[key] = answer
    return answers
