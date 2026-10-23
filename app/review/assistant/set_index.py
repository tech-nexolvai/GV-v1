"""The set's index, read by AI and checked by code: which project, set and views each page shows.

Real two-file uploads hold several projects in each file (four hotels in one vendor PDF and one
architect PDF), and the architect's file may hold a brand prototype besides the projects' own
drawings. How a file says which page belongs to which project differs from one office to the next
(a title block, a cover, a divider page, a sheet index), so no fixed word or page layout is relied
on. The architect's set is the source of truth:

1. **One cheap read per page** (Sonnet, thinking off, low effort): the page's picture and its
   printed text (GV's typed boxes too) in, a closed JSON form out: the project names and the
   set's kind the page prints (`architectural`, `interior design`, `prototype`, `brand standard`,
   `reference`, `shop drawing`), the words that say the kind, whether the page introduces the
   pages after it (a cover or a divider), its sheet number, and each titled view (title as
   printed, view number, kind, item tags, whether it prints sizes, whether it shows a whole floor
   or building system, its box). A vendor page's read also names its item and item tags.
2. **Code checks every string** against what the page prints (`verify`): a name, title, tag or
   sheet number not printed on the page is dropped (and listed under `dropped`), a view whose
   title is not printed is dropped, a kind of set is kept only when the words that say it are
   printed. The model judges meaning; code only keeps what the page really shows.
3. **Saved in the set pack** (`set_index.json`, with the model, usage and cost), so a work list is
   built again at $0; a page whose question changed (`PROMPT_VERSION`) is read again.

`simple_check.projects` groups the projects from the checked names (code); the work list pairs a
vendor drawing only with views of its own project, never the brand prototype (R1).
"""

from __future__ import annotations

import base64
import io
import json
import logging
import math
import os
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.reply_json import json_of_reply

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine

__all__ = [
    "INDEX_FILE",
    "PROMPT_VERSION",
    "SET_KINDS",
    "IndexReport",
    "load",
    "printed_text",
    "read_set",
    "reply_schema",
    "run_for_engine",
    "verify",
]

_log = logging.getLogger(__name__)

#: The index in the set pack (the work list reads it: `worklist.INDEX_FILE`).
INDEX_FILE: Final = "set_index.json"
#: Changes when the question changes: a page read with an older question is read again.
PROMPT_VERSION: Final = 1
PROMPT_ID: Final = "review-assistant-chat-set-index-{provider}-{harness}"
#: The kinds of set a page may say it belongs to.
SET_KINDS: Final = (
    "architectural",
    "interior design",
    "prototype",
    "brand standard",
    "reference",
    "shop drawing",
    "unknown",
)
VIEW_KINDS: Final = ("plan", "elevation", "section", "detail", "schedule", "other")
#: The page's picture (visual tokens, `ceil(w/28) x ceil(h/28)`), its text, the reply.
PAGE_TOKENS: Final = 1_600
MAX_TEXT_CHARS: Final = 6_000
MAX_TOKENS: Final = 2_000
PARALLEL: Final = 8
_MAX_VIEWS: Final = 24
_MAX_STRING: Final = 120
_WORD: Final = re.compile(r"[A-Z0-9]+")

SYSTEM: Final = """You index construction drawing sets for GV Review, a shop-drawing checker.

You are shown ONE page of an uploaded PDF (its picture and the text printed on it) and say what \
the page itself shows, in a JSON form:

- projects: the project or building names the page prints (title block, cover, divider, sheet \
index), each exactly as printed. Empty when it prints none.
- set_kind: what kind of set the page belongs to by what it prints: architectural, interior \
design, prototype (a brand's prototype design), brand standard, reference, shop drawing, or \
unknown. set_words: the printed words that say so, exactly as printed.
- introduces_following_pages: true only for a cover, divider or section title page whose \
project or set applies to the pages after it.
- sheet: the page's own sheet number as printed in its title block, or empty.
- item, item_tags: for a shop drawing, the piece the page draws (as printed in its title block) \
and its item codes as printed (such as XX-001.C). Empty otherwise.
- variant_words: printed words saying the piece is an accessible variant (for example ADA, \
accessible, HC when it means that), exactly as printed. hand_words: printed words saying it is \
drawn opposite-hand or mirrored (for example MIRROR, OPP. HAND, REV when it means reversed). \
Empty when none.
- views: each titled view on the page: title as printed (whole, including words after "@"), \
number (its bubble), kind (plan, elevation, section, detail, schedule, other), item_tags printed \
in it, prints_sizes (true when the view prints dimensions of the piece itself), whole_floor \
(true for a key, floor, ceiling, furniture, finish or electrical plan of a whole floor or \
building system), variant_words and hand_words printed in it (as above), and its box as \
fractions of the page width and height [x0, y0, x1, y1].

Copy text exactly as printed; never invent or complete a name, tag or number. Answer with the \
JSON form only."""


def reply_schema() -> dict[str, Any]:
    """The closed answer form."""
    strings = {"type": "array", "items": {"type": "string"}}
    view = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "number": {"type": "string"},
            "kind": {"type": "string", "enum": list(VIEW_KINDS)},
            "item_tags": strings,
            "prints_sizes": {"type": "boolean"},
            "whole_floor": {"type": "boolean"},
            "variant_words": strings,
            "hand_words": strings,
            "box": {"type": "array", "items": {"type": "number"}},
        },
        "required": [
            "title",
            "number",
            "kind",
            "item_tags",
            "prints_sizes",
            "whole_floor",
            "variant_words",
            "hand_words",
            "box",
        ],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "projects": strings,
            "set_kind": {"type": "string", "enum": list(SET_KINDS)},
            "set_words": strings,
            "introduces_following_pages": {"type": "boolean"},
            "sheet": {"type": "string"},
            "item": {"type": "string"},
            "item_tags": strings,
            "variant_words": strings,
            "hand_words": strings,
            "views": {"type": "array", "items": view},
        },
        "required": [
            "projects",
            "set_kind",
            "set_words",
            "introduces_following_pages",
            "sheet",
            "item",
            "item_tags",
            "variant_words",
            "hand_words",
            "views",
        ],
        "additionalProperties": False,
    }


# ---- what a page prints ------------------------------------------------------------------------


def _compact(text: object) -> str:
    return "".join(_WORD.findall(str(text or "").upper()))


def _tokens(text: object) -> list[str]:
    return _WORD.findall(str(text or "").upper())


@dataclass(frozen=True, slots=True)
class Printed:
    """What one page prints, for checking the model's strings: its words and its text with every
    sign and space dropped."""

    tokens: frozenset[str]
    compact: str
    phrases: tuple[tuple[str, tuple[float, float, float, float]], ...] = ()
    """Its printed phrases with their boxes (points), to check a view's box."""


def printed_text(
    file: str,
    page: int,
    *,
    page_texts: Any = None,
    dims: Any = None,
    notes: Any = None,
    annotations: Any = None,
) -> tuple[str, Printed]:
    """A page's printed text as the model is shown it (the PDF's text, then GV's typed boxes) and
    what code checks the model's strings against (that text, the phrases and tags code read)."""
    parts: list[str] = []
    for entry in (page_texts or {}).get("pages") or [] if isinstance(page_texts, Mapping) else []:
        if isinstance(entry, Mapping) and entry.get("file") == file and entry.get("page") == page:
            parts.append(" ".join(str(entry.get("text") or "").split()))
    typed = [
        " ".join(str(one.get("text") or "").split())
        for one in (annotations if isinstance(annotations, list) else [])
        if isinstance(one, Mapping)
        and one.get("file") == file
        and one.get("page") == page
        and one.get("kind") == "FreeText"
        and str(one.get("text") or "").strip()
    ]
    phrases: list[tuple[str, tuple[float, float, float, float]]] = []
    for entry in (dims or {}).get("pages") or [] if isinstance(dims, Mapping) else []:
        if not (isinstance(entry, Mapping) and entry.get("file") == file):
            continue
        if entry.get("page") != page:
            continue
        for phrase in entry.get("phrases") or []:
            box = phrase.get("box") if isinstance(phrase, Mapping) else None
            if isinstance(box, list) and len(box) == 4:
                x0, y0, x1, y1 = (float(v) for v in box)
                phrases.append((str(phrase.get("text") or ""), (x0, y0, x1, y1)))
    tags: list[str] = []
    for entry in (notes or {}).get("pages") or [] if isinstance(notes, Mapping) else []:
        if isinstance(entry, Mapping) and entry.get("file") == file and entry.get("page") == page:
            for drawing in entry.get("drawings") or []:
                for tag in drawing.get("tags") or [] if isinstance(drawing, Mapping) else []:
                    tags.append(str(tag.get("text") if isinstance(tag, Mapping) else tag))
    shown = " ".join([*parts, *(f"[GV typed: {t}]" for t in typed)])[:MAX_TEXT_CHARS]
    every = " ".join([*parts, *typed, *(p for p, _b in phrases), *tags])
    return shown, Printed(frozenset(_tokens(every)), _compact(every), tuple(phrases))


def is_printed(text: object, printed: Printed) -> bool:
    """Whether a string the model returned is printed on the page: every word of it is a printed
    word, or the whole of it, signs and spaces dropped, is in the printed text (a tag printed
    `XX-001.C` returned as `XX 001.C`)."""
    words = _tokens(text)
    if not words:
        return False
    if all(w in printed.tokens for w in words):
        return True
    return _compact(text) in printed.compact


def _strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [" ".join(str(v).split())[:_MAX_STRING] for v in value if str(v or "").strip()]


def _box(value: object, size: tuple[float, float] | None) -> list[float] | None:
    """A view's box as fractions of the page, in points (origin top-left)."""
    if size is None or not isinstance(value, list) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    if not all(0.0 <= v <= 1.0 and math.isfinite(v) for v in (x0, y0, x1, y1)):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    width, height = size
    return [
        round(x0 * width, 1),
        round(y0 * height, 1),
        round(x1 * width, 1),
        round(y1 * height, 1),
    ]


def _title_inside(title: str, box: list[float], printed: Printed) -> bool:
    """Whether some printed phrase of the title lies in (or within a line of) the view's box."""
    words = set(_tokens(title))
    margin = 40.0
    for text, at in printed.phrases:
        if not (set(_tokens(text)) & words):
            continue
        cx, cy = (at[0] + at[2]) / 2, (at[1] + at[3]) / 2
        if box[0] - margin <= cx <= box[2] + margin and box[1] - margin <= cy <= box[3] + margin:
            return True
    return not printed.phrases


def verify(
    answer: Mapping[str, Any], printed: Printed, size: tuple[float, float] | None = None
) -> dict[str, Any]:
    """The model's read with only what the page prints (see the module); what was dropped is
    listed under `dropped`. `size`: the page's size in points (a view's box is kept in points)."""
    dropped: list[str] = []

    def keep(values: Sequence[str], what: str) -> list[str]:
        out = []
        for value in values:
            if is_printed(value, printed):
                out.append(value)
            else:
                dropped.append(f"{what} {value!r} is not printed on the page")
        return out

    projects = keep(_strings(answer.get("projects")), "project")
    set_words = keep(_strings(answer.get("set_words")), "set words")
    kind = answer.get("set_kind") if answer.get("set_kind") in SET_KINDS else "unknown"
    if kind != "unknown" and not set_words:
        dropped.append(f"set kind {kind!r}: the words that say it are not printed")
        kind = "unknown"
    sheet = " ".join(str(answer.get("sheet") or "").split())[:_MAX_STRING]
    if sheet and not is_printed(sheet, printed):
        dropped.append(f"sheet {sheet!r} is not printed on the page")
        sheet = ""
    item = " ".join(str(answer.get("item") or "").split())[:_MAX_STRING]
    if item and not is_printed(item, printed):
        dropped.append(f"item {item!r} is not printed on the page")
        item = ""
    views: list[dict[str, Any]] = []
    for raw in answer.get("views") or [] if isinstance(answer.get("views"), list) else []:
        if not isinstance(raw, Mapping) or len(views) >= _MAX_VIEWS:
            continue
        title = " ".join(str(raw.get("title") or "").split())[:_MAX_STRING]
        if not title or not is_printed(title, printed):
            dropped.append(f"view {title!r} is not printed on the page")
            continue
        box = _box(raw.get("box"), size)
        if box is not None and not _title_inside(title, box, printed):
            dropped.append(f"view {title!r}: its title is not inside the box given")
            box = None
        number = " ".join(str(raw.get("number") or "").split())[:8]
        views.append(
            {
                "title": title,
                "number": number if number and is_printed(number, printed) else "",
                "kind": raw.get("kind") if raw.get("kind") in VIEW_KINDS else "other",
                "item_tags": keep(_strings(raw.get("item_tags")), "view tag"),
                "prints_sizes": bool(raw.get("prints_sizes")),
                "whole_floor": bool(raw.get("whole_floor")),
                "variant_words": keep(_strings(raw.get("variant_words")), "view variant word"),
                "hand_words": keep(_strings(raw.get("hand_words")), "view hand word"),
                "box": box,
            }
        )
    return {
        "projects": projects,
        "set_kind": kind,
        "set_words": set_words,
        "introduces": bool(answer.get("introduces_following_pages")),
        "sheet": sheet,
        "item": item,
        "item_tags": keep(_strings(answer.get("item_tags")), "item tag"),
        "variant_words": keep(_strings(answer.get("variant_words")), "variant word"),
        "hand_words": keep(_strings(answer.get("hand_words")), "hand word"),
        "views": views,
        "dropped": dropped,
    }


# ---- asking ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AskReply:
    """One call's reply: its text, why it stopped, its usage, the model that answered, its cost."""

    text: str
    stop_reason: str | None
    usage: Mapping[str, Any]
    model: str
    cost_usd: float = 0.0


#: One call: (system, messages, the answer form) -> the reply. Raises on a failed call.
type Asker = Callable[[list[dict[str, Any]], Mapping[str, Any]], AskReply]
#: A page's picture: (file, page) -> PNG bytes, width, height; `None` when there is none.
type PagePicture = Callable[[str, int], tuple[bytes, int, int] | None]


@dataclass(slots=True)
class IndexReport:
    """What one index read did."""

    asked: int = 0
    answered: int = 0
    reused: int = 0
    failed: list[tuple[str, int, str]] = field(default_factory=list)
    cost_usd: float = 0.0
    skipped_for_money: int = 0


def _key(file: str, page: int) -> str:
    return f"{file}#{page}"


def load(folder: Path) -> dict[str, dict[str, Any]]:
    """The saved index, per page (`file#page`), empty when there is none."""
    try:
        document = json.loads((folder / INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    pages = document.get("pages") if isinstance(document, Mapping) else None
    return {str(k): dict(v) for k, v in (pages or {}).items() if isinstance(v, Mapping)}


_FILE_LOCK = threading.Lock()


def _save(folder: Path, key: str, entry: Mapping[str, Any]) -> None:
    with _FILE_LOCK:
        pages = load(folder)
        pages[key] = dict(entry)
        path = folder / INDEX_FILE
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}")
        temporary.write_text(
            json.dumps({"version": 1, "pages": pages}, indent=1, sort_keys=True),
            encoding="utf-8",
        )
        temporary.replace(path)


def _image(png: bytes) -> dict[str, Any]:
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/png",
            "data": base64.b64encode(png).decode("ascii"),
        },
    }


def _parse(text: str) -> Mapping[str, Any] | None:
    found = json_of_reply(text)
    return found if isinstance(found, Mapping) else None


def read_page(
    file: str,
    page: int,
    role: str,
    shown: str,
    printed: Printed,
    size: tuple[float, float] | None,
    ask: Asker,
    picture: PagePicture | None,
) -> tuple[dict[str, Any] | None, str, float]:
    """One page's read, checked (`verify`): the entry to save, or `None` and why; its cost."""
    content: list[dict[str, Any]] = []
    made = picture(file, page) if picture is not None else None
    if made is not None:
        content.append(_image(made[0]))
    whose = "the architect's file" if role == "architect" else "the vendor's file"
    content.append(
        {
            "type": "text",
            "text": (
                f"Page {page} of {whose}. Its printed text:\n{shown or '(none)'}\n\n"
                "Fill the JSON form for this page."
            ),
        }
    )
    reply = ask([{"role": "user", "content": content}], reply_schema())
    if reply.stop_reason in ("refusal", "max_tokens"):
        return None, f"the read stopped ({reply.stop_reason})", reply.cost_usd
    answer = _parse(reply.text)
    if answer is None:
        return None, "the read was not the form", reply.cost_usd
    entry = verify(answer, printed, size)
    entry.update(
        {
            "file": file,
            "page": page,
            "role": role,
            "model": reply.model,
            "usage": dict(reply.usage),
            "cost_usd": round(reply.cost_usd, 6),
            "prompt_version": PROMPT_VERSION,
            "asked_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    return entry, "", reply.cost_usd


@dataclass(frozen=True, slots=True)
class PageToRead:
    file: str
    page: int
    role: str
    shown: str
    printed: Printed
    size: tuple[float, float] | None


def read_set(
    pages: Sequence[PageToRead],
    folder: Path,
    ask: Asker,
    picture: PagePicture | None,
    *,
    fits: Callable[[], bool] = lambda: True,
    stop: threading.Event | None = None,
) -> IndexReport:
    """Every page's read, saved in the pack; a page read with the same question is reused at no
    cost. `fits`: whether one more read fits the run's money (else it is not asked)."""
    report = IndexReport()
    saved = load(folder)
    todo = []
    for one in pages:
        entry = saved.get(_key(one.file, one.page))
        if entry is not None and entry.get("prompt_version") == PROMPT_VERSION:
            report.reused += 1
        else:
            todo.append(one)
    lock = threading.Lock()

    def task(one: PageToRead) -> None:
        if stop is not None and stop.is_set():
            return
        if not fits():
            with lock:
                report.skipped_for_money += 1
            return
        with lock:
            report.asked += 1
        try:
            entry, why, cost = read_page(
                one.file, one.page, one.role, one.shown, one.printed, one.size, ask, picture
            )
        except Exception as failed:  # noqa: BLE001 - the page stays out of the index
            entry, why, cost = None, type(failed).__name__, 0.0
        with lock:
            report.cost_usd += cost
            if entry is None:
                report.failed.append((one.file, one.page, why))
                return
            report.answered += 1
        _save(folder, _key(one.file, one.page), entry)

    with ThreadPoolExecutor(max_workers=PARALLEL, thread_name_prefix="gv-set-index") as pool:
        list(pool.map(task, todo))
    return report


def pages_of(folder: Path) -> list[PageToRead]:
    """Every page of a two-file set's uploads, with what it prints (from the set pack)."""

    def read(name: str) -> Any:
        try:
            return json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    page_map = read("page_map.json") or {}
    roles = page_map.get("upload_roles") or {}
    page_texts, dims, notes, annotations = (
        read("page_texts.json"),
        read("dims.json"),
        read("notes.json"),
        read("annotations.json"),
    )
    out: list[PageToRead] = []
    for entry in page_map.get("pages") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            continue
        file = str(entry.get("file"))
        role = str(roles.get(file) or "vendor")
        shown, printed = printed_text(
            file,
            int(entry["page"]),
            page_texts=page_texts,
            dims=dims,
            notes=notes,
            annotations=annotations,
        )
        size = None
        for half in entry.get("halves") or []:
            box = half.get("box") if isinstance(half, Mapping) else None
            if (
                isinstance(box, list)
                and len(box) == 4
                and float(box[0]) == 0
                and float(box[1]) == 0
            ):
                size = (float(box[2]), float(box[3]))
        out.append(PageToRead(file, int(entry["page"]), role, shown, printed, size))
    return out


def pack_picture(folder: Path) -> PagePicture:
    """A page's picture from the pack's page pictures (`pages/<name>/p-NNN.png`), shrunk to
    `PAGE_TOKENS`."""
    pack = import_module("app.review.assistant.set_pack")
    sizes = import_module("app.review.assistant.simple_check.compare")

    def picture(file: str, page: int) -> tuple[bytes, int, int] | None:
        path = folder / "pages" / pack.stem(file) / f"p-{page:03d}.png"
        try:
            from PIL import Image

            with Image.open(path) as opened:
                target = sizes.resized_size(opened.width, opened.height, PAGE_TOKENS)
                shrunk = opened.convert("RGB").resize(target, Image.Resampling.LANCZOS)
        except (OSError, ValueError):
            return None
        buffer = io.BytesIO()
        shrunk.save(buffer, format="PNG", compress_level=3)
        return buffer.getvalue(), target[0], target[1]

    return picture


def engine_asker(engine: ChatEngine, model_name: str, effort: str = "low") -> Asker:
    """The read's call through the engine's own call path (streamed, recorded on the Usage page,
    charged to the answer), as the page matcher's."""
    from app.review.assistant.chat_engine import HARNESS_ID
    from app.review.assistant.chat_request import MODELS, thinking_config

    model = MODELS[model_name]
    prompt_id = PROMPT_ID.format(provider=engine.provider.name, harness=HARNESS_ID)
    estimate = import_module("app.review.assistant.simple_check.compare").estimate_cost_usd

    def ask(messages: list[dict[str, Any]], schema: Mapping[str, Any]) -> AskReply:
        reply = engine._call(
            model=model,
            effort=effort,
            thinking=thinking_config(model, False),
            system=SYSTEM,
            tools=(),
            messages=messages,
            final=False,
            max_tokens=MAX_TOKENS,
            prompt_id=prompt_id,
            display=None,
            output_format=schema,
        )
        usage = dict(reply.usage)
        answered = reply.model or model.id
        return AskReply(reply.text(), reply.stop_reason, usage, answered, estimate(answered, usage))

    return ask


def run_for_engine(engine: ChatEngine, folder: Path) -> IndexReport | None:
    """A full check's index read (two-file sets): every page not yet read, within the answer's
    runaway guard. `None` when it is off."""
    model_name = getattr(engine.settings, "set_index_model", None)
    if not model_name or folder is None:
        return None
    from app.review.assistant.chat_request import MODELS

    model = MODELS[model_name]
    tokens = PAGE_TOKENS + MAX_TEXT_CHARS // 3 + 800

    def fits() -> bool:
        return bool(engine._fits(engine._price(model, tokens, MAX_TOKENS) or 0))

    report = read_set(
        pages_of(folder),
        folder,
        engine_asker(engine, model_name),
        pack_picture(folder),
        fits=fits,
        stop=engine.cancel,
    )
    _log.info(
        "set index: %d asked, %d answered, %d reused, %d failed, $%.3f",
        report.asked,
        report.answered,
        report.reused,
        len(report.failed),
        report.cost_usd,
    )
    return report
