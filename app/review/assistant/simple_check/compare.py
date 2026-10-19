"""The simple full check's compare call: one model call per work item (CONTRACT-SIMPLE, 2026-10-19).

For each vendor drawing of the work list (`worklist.py`), one call sees:

- **sharp pictures** of the vendor drawing and of each architect drawing of its area (the governing
  one first), cut from the set pack's drawings-only copy through the hi-res page cache
  (`set_pack.crop_picture`, `page_cache`), each as sharp as Claude reads unresized (`fitting_dpi`:
  at most 2576 px a side and 4784 visual tokens, `ceil(w/28) x ceil(h/28)`). A drawing too big to
  stay legible in one picture (below `MIN_LEGIBLE_DPI`) is cut into overlapping tiles, each at least
  that sharp, plus one small overview of the whole drawing;
- **code's numbered labels drawn on them** (`dim_labels.draw_marks`, the zoom tool's marks, #1571:
  Set-of-Mark), so the model names a label instead of typing a value or guessing a place;
- **a text list of every label with the value code read**, the area, the products named on it, the
  run's product scope and GV's rule summary (`prompts.py`).

It answers in a fixed form (`schema.REPLY_SCHEMA`, Anthropic's structured outputs in
`output_config.format`; adaptive thinking runs first, the form is filled after, per the docs'
"grammar state resets between sections"). Code maps the pixel boxes to PDF points, puts label ids
in code's form, and checks the answer (`schema.check_result`). An answer that fails a check gets
ONE follow-up turn in the same conversation (its thinking passed back unchanged, so the model only
fixes what code named); still wrong, the item is `failed` with the reason, never dropped.

**The call** goes through the chat's provider (`chat_provider`, the official SDK, the key from the
settings) with the chat's own request builder (`chat_request.call_params`: the model's thinking
setting, `output_config.effort`, the refusal fallback and thinking-binding betas on Anthropic's own
API, OpenRouter's keep-no-data routing elsewhere). The default model is Sonnet 5.5 at high effort;
an item the work list marks hard goes to Opus 5.5 (`CompareSettings`).

**Prompt caching.** The system prompt (with GV's rules and the scope) is the same bytes for every
call of a run and carries the cache breakpoint; the structured-output schema never changes either.
A cache entry is readable only once its first response starts streaming, so the first call starts
alone and the others wait (up to `WARM_WAIT_S`) for its first event. The pictures carry no
breakpoint: a call is asked once, so a cache write on them would only cost more.

**Retries.** The SDK retries a refused connection, 429 and 5xx before a stream starts (twice); a
stream that fails midway, an overloaded or rate-limited service, or a 5xx is asked again here, up to
`retries` times with growing waits. A refusal is not retried.

**Pictures exactly as sent.** Each picture is pre-sized by code with Anthropic's own resize rule and
marked `transformations.oversized_image: "error"` (Anthropic's API), so the server never shrinks
it silently and a pixel box maps back 1:1.

Loads `set_pack` and `dim_labels` by name (they draw with pypdfium2, Pillow and OpenCV, which
`app/api/` must not reach by import).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from importlib import import_module
from pathlib import Path
from typing import Any, Final

from pydantic import ValidationError

from app.review.assistant.chat_provider import ChatProvider, ProviderFailure, failure_from
from app.review.assistant.chat_request import MODELS, ChatModel, call_params, thinking_config
from app.review.assistant.page_pictures import fitting_dpi
from app.review.assistant.simple_check.prompts import item_text, repair_text
from app.review.assistant.simple_check.schema import (
    REPLY_SCHEMA,
    Box,
    CompareOutcome,
    CompareResult,
    Drawing,
    OtherItem,
    Pair,
    Picture,
    Reply,
    WorkItem,
    canonical_ref,
    check_result,
)

__all__ = [
    "DOWN_AFTER",
    "DOWN_REASON",
    "MAX_IMAGES",
    "MIN_LEGIBLE_DPI",
    "PRICES",
    "CompareSettings",
    "PictureSource",
    "SentPicture",
    "compare_all",
    "compare_item",
    "estimate_cost_usd",
    "image_tokens",
    "plan_parts",
    "request_content",
    "resized_size",
    "to_result",
]

_log = logging.getLogger(__name__)

#: Claude's high-resolution picture limits (Anthropic's vision docs, 2026-10).
MAX_EDGE_PX: Final = 2576
MAX_IMAGE_TOKENS: Final = 4784
_PATCH_PX: Final = 28
#: A drawing is sent whole only when that is at least this sharp; else it is cut into tiles. At 200
#: dpi a 6 pt character is 17 px tall (below about 150 dpi small drawing text stops being legible).
MIN_LEGIBLE_DPI: Final = 200
#: At most this many tiles per drawing (a 16 x 10 inch drawing needs 2 at 200 dpi).
MAX_TILES: Final = 6
#: Tiles overlap by this share of their side (and at least `_OVERLAP_MIN_PT`), so a dimension cut
#: by one tile's edge is whole on its neighbour.
_OVERLAP_SHARE: Final = 0.08
_OVERLAP_MIN_PT: Final = 18.0
#: A tiled drawing's overview: its whole picture within this many visual tokens.
OVERVIEW_TOKENS: Final = 1600
#: Anthropic: more than 20 images in one request brings a stricter per-image size limit.
MAX_IMAGES: Final = 20
#: Anthropic's request size limit is 32 MB; the pictures (base64) stay under this.
MAX_REQUEST_BYTES: Final = 28_000_000
#: How long the other calls wait for the first call's first event (the prompt cache's write).
WARM_WAIT_S: Final = 90.0
_RETRY_BASE_S: Final = 4.0
_RATE_LIMIT_BASE_S: Final = 20.0
_RETRY_STATUSES: Final = frozenset({408, 409, 429, 500, 502, 503, 504, 520, 522, 524, 529})
#: Drawings in a row whose model call still failed after every try: the AI service is taken as
#: down and the drawings not started yet fail at once with the reason (the app test, 2026-10-19:
#: with the service down, 39 drawings each tried again and again took 6 minutes).
DOWN_AFTER: Final = 3
DOWN_REASON: Final = (
    f"not started: the AI service did not answer for {DOWN_AFTER} drawings in a row; check again "
    "when it answers"
)

#: Anthropic's list prices per million tokens (input, output, cache read, 5-minute cache write),
#: from the bundled model catalogue (2026-10); an estimate only, the Usage page uses the rates file.
PRICES: Final[Mapping[str, tuple[Decimal, Decimal, Decimal, Decimal]]] = {
    "claude-sonnet-5-5": (Decimal(2), Decimal(10), Decimal("0.20"), Decimal("2.50")),
    "claude-opus-5-5": (Decimal(4), Decimal(20), Decimal("0.20"), Decimal(5)),
}


# ---- settings ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompareSettings:
    """The compare call's settings (`app/config.py`: `assistant_simple_*`)."""

    model: str = "claude-sonnet-5-5"
    effort: str = "high"
    hard_model: str = "claude-opus-5-5"
    hard_effort: str = "high"
    parallel: int = 8
    max_tokens: int = 64000
    retries: int = 3

    @classmethod
    def from_settings(cls, settings: Any) -> CompareSettings:
        return cls(
            model=settings.assistant_simple_compare_model,
            effort=settings.assistant_simple_compare_effort,
            hard_model=settings.assistant_simple_hard_model,
            hard_effort=settings.assistant_simple_hard_effort,
            parallel=settings.assistant_simple_parallel,
            max_tokens=settings.assistant_simple_max_tokens,
            retries=settings.assistant_simple_retries,
        )

    def for_item(self, item: WorkItem) -> tuple[ChatModel, str]:
        """The model and effort for this item: the hard model for an item marked hard."""
        name, effort = (
            (self.hard_model, self.hard_effort) if item.hard else (self.model, self.effort)
        )
        return MODELS[name], effort


# ---- picture sizes -----------------------------------------------------------------------------


def image_tokens(width: int, height: int) -> int:
    """Visual tokens of a picture: one per 28 x 28 pixel patch."""
    return math.ceil(width / _PATCH_PX) * math.ceil(height / _PATCH_PX)


def _fits(width: int, height: int, max_tokens: int = MAX_IMAGE_TOKENS) -> bool:
    return (
        math.ceil(width / _PATCH_PX) * _PATCH_PX <= MAX_EDGE_PX
        and math.ceil(height / _PATCH_PX) * _PATCH_PX <= MAX_EDGE_PX
        and image_tokens(width, height) <= max_tokens
    )


def resized_size(width: int, height: int, max_tokens: int = MAX_IMAGE_TOKENS) -> tuple[int, int]:
    """The size Claude would shrink a picture to (Anthropic's reference rule): code shrinks it
    itself, so the server never does and pixel boxes map back 1:1."""
    if _fits(width, height, max_tokens):
        return width, height
    if height > width:
        h, w = resized_size(height, width, max_tokens)
        return w, h
    aspect = width / height
    lo, hi = 1, width
    while lo + 1 < hi:
        mid = (lo + hi) // 2
        if _fits(mid, max(round(mid / aspect), 1), max_tokens):
            lo = mid
        else:
            hi = mid
    return lo, max(round(lo / aspect), 1)


def _dpi_within(width_pt: float, height_pt: float, max_tokens: int) -> int:
    """The highest dpi (50 to 300) at which a box fits `max_tokens` visual tokens."""
    for dpi in range(300, 49, -1):
        if _fits(int(width_pt * dpi / 72) + 2, int(height_pt * dpi / 72) + 2, max_tokens):
            return dpi
    return 50


@dataclass(frozen=True, slots=True)
class _Part:
    part: str
    box: Box
    dpi: int


def _sharpest(box: Box) -> int:
    return fitting_dpi(Decimal(str(box[2] - box[0])), Decimal(str(box[3] - box[1])))


def _tiling_score(sharp: int, count: int, min_dpi: int) -> tuple[bool, int, int]:
    if sharp >= min_dpi:
        return True, -count, sharp
    return False, sharp, -count


def plan_parts(box: Box, min_dpi: int = MIN_LEGIBLE_DPI, overview: bool = True) -> list[_Part]:
    """How one drawing's box is sent: whole when that is at least `min_dpi` sharp; else the fewest
    overlapping tiles that each are (at most `MAX_TILES`), plus a small overview of the whole."""
    whole = _sharpest(box)
    if whole >= min_dpi:
        return [_Part("whole", box, whole)]
    x0, y0, x1, y1 = box
    width, height = x1 - x0, y1 - y0
    best: tuple[int, int, int, list[Box]] | None = None
    for count in range(2, MAX_TILES + 1):
        for columns in range(1, count + 1):
            if count % columns:
                continue
            rows = count // columns
            overlap_x = max(_OVERLAP_MIN_PT, width / columns * _OVERLAP_SHARE) if columns > 1 else 0
            overlap_y = max(_OVERLAP_MIN_PT, height / rows * _OVERLAP_SHARE) if rows > 1 else 0
            tile_w = (width + overlap_x * (columns - 1)) / columns
            tile_h = (height + overlap_y * (rows - 1)) / rows
            tiles = [
                (
                    x0 + c * (tile_w - overlap_x),
                    y0 + r * (tile_h - overlap_y),
                    min(x1, x0 + c * (tile_w - overlap_x) + tile_w),
                    min(y1, y0 + r * (tile_h - overlap_y) + tile_h),
                )
                for r in range(rows)
                for c in range(columns)
            ]
            sharp = min(_sharpest(tile) for tile in tiles)
            # The fewest tiles that reach `min_dpi` (the sharpest of those); while none does,
            # the sharpest so far.
            if best is None or _tiling_score(sharp, count, min_dpi) > _tiling_score(
                best[0], best[1], min_dpi
            ):
                best = (sharp, count, columns, tiles)
        if best is not None and best[0] >= min_dpi:
            break
    assert best is not None
    _sharp, count, _columns, tiles = best
    parts = [
        _Part(f"tile {n} of {count}", tile, _sharpest(tile)) for n, tile in enumerate(tiles, 1)
    ]
    if overview:
        parts.insert(0, _Part("overview", box, _dpi_within(width, height, OVERVIEW_TOKENS)))
    return parts


def _plans(item: WorkItem) -> list[tuple[Drawing, list[_Part]]]:
    """Every drawing's parts, within `MAX_IMAGES`: overviews go first, then the tiles get coarser."""
    drawings = [item.vendor, *item.architect]
    for min_dpi, overview in ((MIN_LEGIBLE_DPI, True), (MIN_LEGIBLE_DPI, False), (150, False)):
        plans = [(d, plan_parts(d.crop_box, min_dpi, overview)) for d in drawings]
        if sum(len(parts) for _d, parts in plans) <= MAX_IMAGES:
            return plans
    return [(d, plan_parts(d.crop_box, 0, False)) for d in drawings][:MAX_IMAGES]


# ---- drawing the pictures ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SentPicture:
    picture: Picture
    png: bytes


def _inside(box: Box, part: Box) -> bool:
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    return part[0] <= cx <= part[2] and part[1] <= cy <= part[3]


class PictureSource:
    """Draws a work item's pictures from a set pack: the drawings-only copy (GV's markup hidden),
    through the pack's hi-res page cache, with code's label marks."""

    def __init__(self, folder: Path, uploads: Path) -> None:
        self.folder = folder
        self.uploads = uploads
        self._pack = import_module("app.review.assistant.set_pack")
        self._marks = import_module("app.review.assistant.dim_labels")

    def prefetch(self, items: Iterable[WorkItem]) -> None:
        """Start drawing every page the items need, in the page cache's own pool."""
        cache = import_module("app.review.assistant.page_cache")
        pages: dict[str, set[int]] = {}
        for item in items:
            for drawing in (item.vendor, *item.architect):
                pages.setdefault(drawing.file, set()).add(drawing.page)
        for name, numbers in pages.items():
            try:
                pdf = self._pack.drawings_only(self.folder, self.uploads, name)
                cache.prefetch(self.folder / self._pack.HIRES, pdf, sorted(numbers))
            except Exception as failed:  # noqa: BLE001 - each crop draws its page itself
                _log.info("simple check: pages not prefetched: %s", type(failed).__name__)

    def pictures(self, item: WorkItem) -> list[SentPicture]:
        """The item's pictures in the order they are sent: the vendor drawing, then each
        architect drawing (the governing one first). Raises `ValueError` when a drawing cannot be
        drawn (the item then fails with that reason)."""
        out: list[SentPicture] = []
        for drawing, parts in _plans(item):
            pdf = self._pack.drawings_only(self.folder, self.uploads, drawing.file)
            for part in parts:
                out.append(self._picture(len(out) + 1, drawing, part, pdf))
        return out

    def _picture(self, number: int, drawing: Drawing, part: _Part, pdf: Path) -> SentPicture:
        from PIL import Image

        crop = self._pack.crop_picture(
            pdf, drawing.page, part.box, part.dpi, cache=self.folder / self._pack.HIRES
        )
        png, width, height = crop.png, crop.width, crop.height
        budget = OVERVIEW_TOKENS if part.part == "overview" else MAX_IMAGE_TOKENS
        target = resized_size(width, height, budget)
        if target != (width, height):
            with Image.open(io.BytesIO(png)) as opened:
                shrunk = opened.convert("RGB").resize(target, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            shrunk.save(buffer, format="PNG", compress_level=3)
            png, (width, height) = buffer.getvalue(), target
        shown = [label for label in drawing.labels if _inside(label.box, crop.box)]
        if shown:
            png = self._marks.draw_marks(
                png, crop.box, [{"id": label.id, "box": list(label.box)} for label in shown]
            )
        picture = Picture(
            number=number,
            drawing=drawing.ref,
            role=drawing.role,
            file=drawing.file,
            page=drawing.page,
            box=crop.box,
            width=width,
            height=height,
            dpi=round(crop.dpi * width / crop.width) if crop.width else crop.dpi,
            tokens=image_tokens(width, height),
            part=part.part,
            labels=tuple(label.ref for label in shown),
        )
        return SentPicture(picture, png)


# ---- the request -------------------------------------------------------------------------------


def _caption(picture: Picture, item: WorkItem) -> str:
    whose = "the vendor drawing" if picture.role == "vendor" else "the architect drawing"
    if picture.role == "architect" and item.architect and picture.drawing == item.architect[0].ref:
        whose = "the governing architect drawing"
    part = "" if picture.part == "whole" else f", {picture.part}"
    tags = (
        f"; its tags are page {picture.page}'s (D5 = p{picture.page}.D5)" if picture.labels else ""
    )
    return (
        f"Image {picture.number}: {whose} {picture.drawing}, page {picture.page}{part}, "
        f"{picture.width} x {picture.height} px{tags}."
    )


def request_content(
    item: WorkItem, sent: Sequence[SentPicture], *, provider: str = "anthropic"
) -> list[dict[str, Any]]:
    """The user message: each picture after its one-line caption, then the item's text."""
    content: list[dict[str, Any]] = []
    for one in sent:
        content.append({"type": "text", "text": _caption(one.picture, item)})
        image: dict[str, Any] = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": base64.b64encode(one.png).decode("ascii"),
            },
        }
        if provider == "anthropic":
            # A picture the server would shrink is refused instead (its boxes would not map back).
            image["transformations"] = {"oversized_image": "error"}
        content.append(image)
    content.append({"type": "text", "text": item_text(item, [s.picture for s in sent])})
    return content


def _params(
    *,
    provider: str,
    model: ChatModel,
    effort: str,
    system: str,
    messages: list[dict[str, Any]],
    max_tokens: int,
) -> dict[str, Any]:
    params = call_params(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        effort=effort,
        thinking=thinking_config(model, True),
        system=system,
        tools=(),
        messages=messages,
        final=False,
        max_tokens=max_tokens,
        output_format=REPLY_SCHEMA,
    )
    # No cache breakpoint on the pictures: each drawing is asked once (only the system prompt is
    # shared between calls).
    for message in params["messages"]:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [
                (
                    {k: v for k, v in block.items() if k != "cache_control"}
                    if isinstance(block, Mapping)
                    else block
                )
                for block in content
            ]
    return params


# ---- one streamed call -------------------------------------------------------------------------


@dataclass(slots=True)
class _Answer:
    content: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    stop_reason: str | None = None
    model: str | None = None
    fell_back: bool = False

    def text(self) -> str:
        return "".join(str(b.get("text") or "") for b in self.content if b.get("type") == "text")

    def history(self) -> list[dict[str, Any]]:
        """The answer as the next turn sends it back: thinking blocks unchanged, then its text."""
        out: list[dict[str, Any]] = []
        for block in self.content:
            kind = block.get("type")
            if kind == "thinking":
                out.append(
                    {
                        "type": "thinking",
                        "thinking": str(block.get("thinking") or ""),
                        "signature": str(block.get("signature") or ""),
                    }
                )
            elif kind == "redacted_thinking":
                out.append({"type": "redacted_thinking", "data": block.get("data")})
            elif kind == "text":
                out.append({"type": "text", "text": str(block.get("text") or "")})
        return out


def _merge(usage: dict[str, int], update: object) -> None:
    if isinstance(update, Mapping):
        for key, value in update.items():
            if isinstance(value, int) and not isinstance(value, bool):
                usage[key] = value


def _stream(
    provider: ChatProvider, params: Mapping[str, Any], first_event: Callable[[], None] | None
) -> _Answer:
    """One streamed call, its events gathered. Raises `ProviderFailure`."""
    answer = _Answer()
    stream = provider.open_stream(params)
    try:
        for event in stream:
            if first_event is not None:
                first_event()
                first_event = None
            kind = event.get("type")
            if kind == "message_start":
                message = event.get("message") or {}
                answer.model = (
                    message.get("model") if isinstance(message.get("model"), str) else None
                )
                _merge(answer.usage, message.get("usage"))
            elif kind == "content_block_start":
                index = int(event.get("index", len(answer.content)))
                block = dict(event.get("content_block") or {})
                while len(answer.content) <= index:
                    answer.content.append({})
                answer.content[index] = block
                if block.get("type") == "fallback":
                    answer.fell_back = True
                    to = block.get("to")
                    if isinstance(to, Mapping) and isinstance(to.get("model"), str):
                        answer.model = to["model"]
            elif kind == "content_block_delta":
                index = int(event.get("index", 0))
                if index >= len(answer.content):
                    continue
                block, delta = answer.content[index], event.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta":
                    block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
                elif dtype == "thinking_delta":
                    block["thinking"] = str(block.get("thinking") or "") + str(
                        delta.get("thinking") or ""
                    )
                elif dtype == "signature_delta":
                    block["signature"] = delta.get("signature")
            elif kind == "message_delta":
                delta = event.get("delta") or {}
                answer.stop_reason = delta.get("stop_reason") or answer.stop_reason
                _merge(answer.usage, event.get("usage"))
    except ProviderFailure:
        raise
    except Exception as error:  # noqa: BLE001 - every failure is told plainly
        raise failure_from(error) from None
    finally:
        stream.close()
    answer.content = [block for block in answer.content if block]
    return answer


def _retryable(failure: ProviderFailure) -> bool:
    return failure.retryable or failure.status in _RETRY_STATUSES


def _wait_for(failure: ProviderFailure, attempt: int) -> float:
    base = _RATE_LIMIT_BASE_S if failure.status in (429, 529) else _RETRY_BASE_S
    jitter: float = random.uniform(0, base)
    return base * (1 << attempt) + jitter


# ---- reading an answer -------------------------------------------------------------------------

_FENCE: Final = re.compile(r"^```(?:json)?\s*|\s*```$")


def _json_of(text: str) -> object:
    """The answer's JSON (structured outputs return it bare; a route without them may fence it)."""
    plain = _FENCE.sub("", text.strip())
    try:
        return json.loads(plain)
    except ValueError:
        start, end = plain.find("{"), plain.rfind("}")
        if 0 <= start < end:
            return json.loads(plain[start : end + 1])
        raise


def _px_box(
    image: int | None,
    box: Sequence[float] | None,
    pictures: Mapping[int, Picture],
    role: str,
    where: str,
    problems: list[str],
) -> tuple[Box | None, int | None]:
    """A pixel box on a picture, in PDF points on its page (and the page)."""
    if image is None and box is None:
        return None, None
    picture = pictures.get(image) if image is not None else None
    if picture is None:
        problems.append(f"{where}: image {image} is not one of this call's pictures")
        return None, None
    if picture.role != role:
        problems.append(f"{where}: image {image} is the {picture.role}'s drawing, not the {role}'s")
        return None, None
    if box is None or len(box) != 4:
        problems.append(f"{where}: a box is [x0, y0, x1, y1] in pixels on image {image}")
        return None, None
    x0, x1 = sorted((float(box[0]), float(box[2])))
    y0, y1 = sorted((float(box[1]), float(box[3])))
    slack = 4.0
    if x0 < -slack or y0 < -slack or x1 > picture.width + slack or y1 > picture.height + slack:
        problems.append(
            f"{where}: box {list(box)} is outside image {image} "
            f"({picture.width} x {picture.height} px)"
        )
        return None, None
    x0, y0 = max(0.0, x0), max(0.0, y0)
    x1, y1 = min(float(picture.width), x1), min(float(picture.height), y1)
    bx0, by0, bx1, by1 = picture.box
    sx, sy = (bx1 - bx0) / picture.width, (by1 - by0) / picture.height
    points = (
        round(bx0 + x0 * sx, 2),
        round(by0 + y0 * sy, 2),
        round(bx0 + x1 * sx, 2),
        round(by0 + y1 * sy, 2),
    )
    return points, picture.page


def to_result(
    reply: Reply, item: WorkItem, pictures: Sequence[Picture]
) -> tuple[CompareResult | None, list[str]]:
    """The contract's `CompareResult` from the model's reply: label ids in code's form, pixel boxes
    in PDF points on their pages; with the problems found on the way and by `check_result`."""
    problems: list[str] = []
    by_number = {picture.number: picture for picture in pictures}
    pages = item.architect_pages
    only_page = pages[0] if len(pages) == 1 else None
    pairs: list[Pair] = []
    for n, raw in enumerate(reply.pairs):
        vendor = canonical_ref(raw.vendor_label, item.page)
        architect = (
            None
            if raw.architect_label in (None, "", "null")
            else canonical_ref(raw.architect_label, only_page)
        )
        if vendor is None:
            problems.append(f"pairs[{n}].vendor_label {raw.vendor_label!r} is not a label id")
            continue
        if raw.architect_label not in (None, "", "null") and architect is None:
            problems.append(
                f"pairs[{n}].architect_label {raw.architect_label!r} is not a label id like "
                "p23.D17"
            )
            continue
        pairs.append(
            Pair.model_validate(
                {**raw.model_dump(), "vendor_label": vendor, "architect_label": architect}
            )
        )
    others: list[OtherItem] = []
    for n, other in enumerate(reply.other_items):
        where = f"other_items[{n}] ({other.kind}: {other.what[:60]})"
        vendor_box, vendor_page = _px_box(
            other.vendor_image, other.vendor_box_px, by_number, "vendor", where, problems
        )
        architect_box, architect_page = _px_box(
            other.architect_image, other.architect_box_px, by_number, "architect", where, problems
        )
        if vendor_page is not None and vendor_page != item.page:
            problems.append(f"{where}: the vendor box is not on page {item.page}")
            vendor_box = None
        others.append(
            OtherItem(
                kind=other.kind,
                what=other.what,
                vendor=other.vendor,
                architect=other.architect,
                vendor_box=vendor_box,
                architect_box=architect_box,
                architect_page=architect_page,
                verdict=other.verdict,
                millwork=other.millwork,
            )
        )
    unaccounted: list[str] = []
    for text in reply.unaccounted_labels:
        ref = canonical_ref(text, item.page)
        if ref is None:
            problems.append(f"unaccounted_labels holds {text!r}, which is not a label id")
        else:
            unaccounted.append(ref)
    result = CompareResult(
        work_item=reply.work_item.strip(),
        pairs=tuple(pairs),
        other_items=tuple(others),
        unaccounted_labels=tuple(dict.fromkeys(unaccounted)),
    )
    problems += check_result(result, item)
    return result, problems


def _read(
    answer: _Answer, item: WorkItem, pictures: Sequence[Picture]
) -> tuple[CompareResult | None, list[str]]:
    if answer.stop_reason == "max_tokens":
        return None, ["the answer was cut off at the token limit: keep notes and measures short"]
    try:
        document = _json_of(answer.text())
        reply = Reply.model_validate(document)
    except (ValueError, ValidationError) as wrong:
        detail = str(wrong).splitlines()[0][:300] if str(wrong) else type(wrong).__name__
        return None, [f"the answer is not the JSON form ({detail})"]
    return to_result(reply, item, pictures)


# ---- one work item -----------------------------------------------------------------------------


def estimate_cost_usd(model_id: str, usage: Mapping[str, int]) -> float:
    """What a call cost at Anthropic's list prices (an estimate: the Usage page uses the rates)."""
    price = PRICES.get(model_id.removeprefix("anthropic/").split("-2026")[0])
    if price is None:
        price = PRICES["claude-opus-5-5"]
    per = Decimal(1_000_000)
    cost = (
        Decimal(usage.get("input_tokens", 0)) * price[0]
        + Decimal(usage.get("output_tokens", 0)) * price[1]
        + Decimal(usage.get("cache_read_input_tokens", 0)) * price[2]
        + Decimal(usage.get("cache_creation_input_tokens", 0)) * price[3]
    ) / per
    return float(round(cost, 6))


def _add(total: dict[str, int], usage: Mapping[str, int]) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
    ):
        total[key] = total.get(key, 0) + int(usage.get(key, 0) or 0)


def compare_item(
    item: WorkItem,
    *,
    provider: ChatProvider,
    source: Callable[[WorkItem], list[SentPicture]],
    system: str,
    settings: CompareSettings,
    first_event: Callable[[], None] | None = None,
    before_call: Callable[[], None] | None = None,
    stop: threading.Event | None = None,
    sleep: Callable[[float], None] = time.sleep,
    down: threading.Event | None = None,
) -> CompareOutcome:
    """One work item's compare call, its retries and its one repair turn. `down`: set when the AI
    service is taken as down (`compare_all`): no more tries."""
    started = time.monotonic()
    model, effort = settings.for_item(item)
    usage: dict[str, int] = {}
    cost = 0.0
    attempts = 0
    answered_by: str | None = None
    pictures: tuple[Picture, ...] = ()

    def outcome(**fields: Any) -> CompareOutcome:
        return CompareOutcome(
            work_item=item.id,
            asked_model=model.id,
            model=answered_by,
            effort=effort,
            attempts=attempts,
            usage=dict(usage),
            cost_usd=round(cost, 6),
            seconds=round(time.monotonic() - started, 1),
            pictures=pictures,
            **fields,
        )

    try:
        sent = source(item)
    except Exception as failed:  # noqa: BLE001 - told with the item, never dropped
        _log.warning("simple check: %s not drawn: %s", item.id, type(failed).__name__)
        if first_event is not None:
            first_event()
        return outcome(status="failed", reason=f"the drawings could not be drawn ({failed})")
    pictures = tuple(one.picture for one in sent)
    request_bytes = sum(len(one.png) * 4 // 3 for one in sent)
    if request_bytes > MAX_REQUEST_BYTES:
        if first_event is not None:
            first_event()
        return outcome(
            status="failed",
            reason=f"the pictures are {request_bytes // 1_000_000} MB, over the request limit",
        )
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": request_content(item, sent, provider=provider.name)}
    ]
    if before_call is not None:
        before_call()
    repaired = False
    last: CompareResult | None = None
    problems: list[str] = []
    while True:
        if stop is not None and stop.is_set():
            return outcome(
                status="failed",
                reason="the run was stopped before this drawing's answer came",
                problems=tuple(problems),
                partial=last,
                repaired=repaired,
            )
        params = _params(
            provider=provider.name,
            model=model,
            effort=effort,
            system=system,
            messages=messages,
            max_tokens=settings.max_tokens,
        )
        answer: _Answer | None = None
        for attempt in range(settings.retries + 1):
            attempts += 1
            try:
                answer = _stream(provider, params, first_event)
                break
            except ProviderFailure as failure:
                if first_event is not None:
                    first_event()  # the other calls need not wait for this one any more
                gone = down is not None and down.is_set()
                if not _retryable(failure) or attempt >= settings.retries or gone:
                    return outcome(
                        status="failed",
                        reason=f"the model call failed: {failure}",
                        problems=tuple(problems),
                        partial=last,
                        repaired=repaired,
                    )
                wait = _wait_for(failure, attempt)
                _log.info(
                    "simple check: %s call failed (%s), again in %.0f s",
                    item.id,
                    failure.status,
                    wait,
                )
                sleep(wait)
            finally:
                first_event = None
        assert answer is not None
        _add(usage, answer.usage)
        answered_by = answer.model or model.id
        cost += estimate_cost_usd(answered_by, answer.usage)
        if answer.stop_reason == "refusal":
            return outcome(
                status="failed",
                reason="Claude declined to compare this drawing",
                partial=last,
                repaired=repaired,
            )
        result, problems = _read(answer, item, pictures)
        last = result or last
        if not problems and result is not None:
            return outcome(status="ok", result=result, repaired=repaired)
        if repaired:
            return outcome(
                status="failed",
                reason="the answer still failed code's checks after one repair turn",
                problems=tuple(problems),
                partial=last,
                repaired=True,
            )
        repaired = True
        follow_up = {
            "role": "user",
            "content": [{"type": "text", "text": repair_text(item, problems)}],
        }
        if answer.fell_back:
            # Another model answered: its thinking cannot be passed back; ask again from the start
            # with the problems and the earlier answer in the question.
            earlier = answer.text()[:20_000]
            messages = [
                {
                    "role": "user",
                    "content": [
                        *messages[0]["content"],
                        {
                            "type": "text",
                            "text": f"An earlier answer was:\n{earlier}\n\n{repair_text(item, problems)}",
                        },
                    ],
                }
            ]
        else:
            messages = [
                messages[0],
                {"role": "assistant", "content": answer.history()},
                follow_up,
            ]


# ---- every work item ---------------------------------------------------------------------------


def compare_all(
    items: Sequence[WorkItem],
    *,
    provider: ChatProvider,
    source: Callable[[WorkItem], list[SentPicture]],
    system: str,
    settings: CompareSettings,
    stop: threading.Event | None = None,
    on_outcome: Callable[[CompareOutcome], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    warm_wait_s: float = WARM_WAIT_S,
) -> list[CompareOutcome]:
    """Every item's outcome, in the items' order, `settings.parallel` calls at a time. The first
    call starts alone; the others start once its first event has come (its prompt cache entry is
    then readable) or `warm_wait_s` has passed. An item left when `stop` is set is `failed`
    ("stopped"), never dropped."""
    warmed = threading.Event()
    results: list[CompareOutcome | None] = [None] * len(items)
    lock = threading.Lock()
    down = threading.Event()
    failed_in_row = [0]

    def wait_for_warm() -> None:
        warmed.wait(warm_wait_s)

    def run(index: int) -> None:
        item = items[index]
        model, effort = settings.for_item(item)
        if stop is not None and stop.is_set():
            done = CompareOutcome(
                work_item=item.id,
                status="failed",
                reason="the run was stopped before this drawing was compared",
                asked_model=model.id,
                effort=effort,
            )
        elif down.is_set():
            done = CompareOutcome(
                work_item=item.id,
                status="failed",
                reason=DOWN_REASON,
                asked_model=model.id,
                effort=effort,
            )
        else:
            first = index == 0
            done = compare_item(
                item,
                provider=provider,
                source=source,
                system=system,
                settings=settings,
                first_event=warmed.set if first else None,
                before_call=None if first else wait_for_warm,
                stop=stop,
                sleep=sleep,
                down=down,
            )
            if first:
                warmed.set()
        with lock:
            results[index] = done
            # A call that failed after being tried again (not a refused request) counts towards
            # the service being down; any answer resets the count.
            if done.status == "ok":
                failed_in_row[0] = 0
            elif (done.reason or "").startswith("the model call failed") and done.attempts > 1:
                failed_in_row[0] += 1
                if failed_in_row[0] >= DOWN_AFTER:
                    down.set()
        if on_outcome is not None:
            try:
                on_outcome(done)
            except Exception:  # noqa: BLE001 - a listener never stops the run
                _log.exception("simple check: outcome listener failed")

    if not items:
        return []
    with ThreadPoolExecutor(
        max_workers=max(1, settings.parallel), thread_name_prefix="gv-simple-compare"
    ) as pool:
        futures = [pool.submit(run, n) for n in range(len(items))]
        for future in futures:
            future.result()
    return [done for done in results if done is not None]
