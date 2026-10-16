"""The job values reader (#1407): which architect drawings are read, the question, and the run.

**Off by default** (`GV_JOB_VALUES_READER`). Nothing in the worker calls it yet: it is measured on
all three sets first (`scripts/job_values_measure.py`, paid only with the owner's OK), then wired.

**Which drawings (code, $0).** Only the architect's side (Q10, `rules.parameter_sources.
CITABLE_SIDES`): on a combined sheet, the pasted drawings GV's printed heading names the architect's
(`arch`) or a brand prototype (`reference`, read for suggestions only), by
`extraction.panels.propose_panel_roles`; in a separate architect file, every drawing on its pages.
**A vendor drawing is never prepared, so it can never be sent.** A drawing whose own black text
names no SECTION, DETAIL, PLAN or ELEVATION view is not read. Sections and details are read first,
then plans, then elevations (included for the side thickness, owner 2026-10-16), at most
`max_panels` per job; each one left out is reported with why. No such drawing: nothing is sent.

**The question.** One picture per drawing (the vendor-only render: GV's markup annotations are not
painted), asked of both readers of the decided Claude pair, with a strict answer shape: per value,
printed / not printed / unsure, the text exactly as printed, its unit, the label beside it, and a
box. The prompt forbids working a value out from others. Each attempt is handed to `record_attempt`
(the shared `CallLedger` in the worker, a local ledger in the script, so `model_invocations` gets
its cost); a malformed answer is asked once more, then the reader abstains.

**The budget.** One spend guard for the job (`BatchSpendGuard`, the slot reader's): each call's worst
case is reserved before it is sent, and a call the cap refuses stops the reading. Every drawing not
asked says so, and its values are nothing with the reason.

**Code decides** (`extraction.job_values.decide`): exact inches, refusals, auto / suggestion /
refused / nothing.

Source: issue #1407. Verification: `tests/extraction/job_values/test_decide.py`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any, Final
from uuid import UUID

from extraction.form_reader.bedrock import (
    AttemptUsage,
    MalformedFormAnswer,
    _response_text,
    _response_usage,
)
from extraction.geometry.rows import Box
from extraction.ink import InkClass
from extraction.job_values.decide import (
    SETTINGS,
    CodeFind,
    JobValueResult,
    Panel,
    PanelDecision,
    ReaderReading,
    code_finds,
    combine,
    decide_panel,
    text_lines,
)
from extraction.slot_reader.claude_output import (
    DEFAULT_CLAUDE_EFFORT,
    OUTPUT_CONFIG_KEY,
    ClaudeEffort,
    _conforms,
    claude_answer,
    output_config,
    picture_fits,
    require_picture_fits,
    visual_tokens,
)

__all__ = [
    "CLAUDE_PAIR",
    "JOB_VALUES_PROMPT",
    "JOB_VALUES_PROMPT_ID",
    "JOB_VALUES_READER_ENV",
    "JOB_VALUES_SCHEMA",
    "MEASURED_JOB_VALUES_SETTINGS",
    "JobValuesReading",
    "JobValuesSettings",
    "PreparedPanel",
    "choose_panels",
    "estimated_input_tokens",
    "job_values_reader_enabled",
    "prepare_pdf_panels",
    "read_job_values",
]

#: The switch. Off unless set to a yes word; anything unrecognised is refused, never read as on.
JOB_VALUES_READER_ENV: Final = "GV_JOB_VALUES_READER"
#: The decided Claude pair, the same two readers as the width and depth readers'.
CLAUDE_PAIR: Final = ("anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5")
JOB_VALUES_PROMPT_ID: Final = "job-values-v1"


def job_values_reader_enabled(environ: Mapping[str, str]) -> bool:
    """Whether `GV_JOB_VALUES_READER` switches the reader on. Unset or empty is off (the default)."""
    raw = environ.get(JOB_VALUES_READER_ENV, "").strip().lower()
    if raw in ("", "0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    raise ValueError(f"{JOB_VALUES_READER_ENV} must be true or false, not {raw!r}")


@dataclass(frozen=True, slots=True)
class JobValuesSettings:
    dpi: int
    max_panels: int
    """At most this many drawings are asked about per job; the rest are reported as not read."""
    max_tokens: int
    effort: ClaudeEffort
    prompt_tokens: int
    """The question's words, in tokens, for the budget's estimate of each call (pictures apart)."""
    concurrency: int


MEASURED_JOB_VALUES_SETTINGS: Final = JobValuesSettings(
    dpi=300,
    max_panels=40,
    max_tokens=3000,
    effort=DEFAULT_CLAUDE_EFFORT,
    prompt_tokens=1200,
    concurrency=4,
)

#: The views read, in the order they are read: a section or detail first, then a plan, then an
#: elevation (side thickness, owner 2026-10-16). Searched in the drawing's own black text.
_VIEW_ORDER: Final = (
    re.compile(r"SECTION|DETAIL"),
    re.compile(r"PLAN"),
    re.compile(r"ELEVATION"),
)

_VALUE_WORDS: Final = {
    "cabinet_depth": "cabinet depth (the base cabinet's depth, front to back)",
    "countertop_overhang": "countertop overhang (how far the top projects past the cabinet face)",
    "backsplash_thickness": "backsplash thickness",
    "cabinet_side_thickness": "cabinet side (end/finished side panel) thickness",
}

JOB_VALUES_PROMPT: Final = (
    "This picture is one architect's drawing for a millwork job. Find whether it PRINTS each of "
    "these four values:\n"
    + "\n".join(f"- {name}: {words}" for name, words in _VALUE_WORDS.items())
    + "\n\nRules:\n"
    "- Only a number printed on the drawing for that value, as a dimension or in a note that names "
    "it. Never work a value out from other numbers (no subtracting, adding or measuring), and never "
    "use a typical or standard value. If you would have to infer it, answer not_printed.\n"
    "- printed: copy the number exactly as printed, with its inch or foot marks (for example "
    '5/8" or 1\'-11 1/4"). Write a stacked fraction as 5/8.\n'
    "- unit: inch for inches or feet-and-inches, mm for millimetres only, none if no unit is "
    "printed.\n"
    "- label: the words printed beside the number that say what it is, exactly; empty if none.\n"
    "- box: [left, top, right, bottom] of the number on this picture, each 0 to 1000 across and "
    "down the picture. [0, 0, 0, 0] when not printed.\n"
    "- status: printed, not_printed, or unsure. Use unsure if you cannot read it clearly or cannot "
    "tell which value it is.\n"
    "Return the one JSON object only."
)

_VALUE_SCHEMA: Final = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["printed", "not_printed", "unsure"]},
        "printed": {"type": "string"},
        "unit": {"type": "string", "enum": ["inch", "mm", "none"]},
        "label": {"type": "string"},
        "box": {"type": "array", "items": {"type": "integer"}},
    },
    "required": ["status", "printed", "unit", "label", "box"],
    "additionalProperties": False,
}
JOB_VALUES_SCHEMA: Final = {
    "type": "object",
    "properties": {name: dict(_VALUE_SCHEMA) for name in SETTINGS},
    "required": list(SETTINGS),
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Drawings, prepared by code at no cost
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedPanel:
    panel: Panel
    picture: bytes
    view_rank: int | None
    """0 section/detail, 1 plan, 2 elevation; `None`: names no view, never read."""
    page_sha256: str


def _view_rank(panel: Panel) -> int | None:
    words = " ".join(line.text for line in text_lines(panel.chars))
    for rank, pattern in enumerate(_VIEW_ORDER):
        if pattern.search(words):
            return rank
    return None


def _picture(rendered: Any, pixels: tuple[int, int, int, int]) -> bytes:
    """The drawing's pixels, shrunk by the smallest whole factor that the pair reads unresized."""
    from PIL import Image

    image = Image.frombytes("RGB", (rendered.width_px, rendered.height_px), rendered.rgb_bytes)
    x0, y0, x1, y1 = pixels
    crop = image.crop(
        (max(0, x0), max(0, y0), min(rendered.width_px, x1), min(rendered.height_px, y1))
    )
    factor = 1
    while not all(
        picture_fits(-(-crop.width // factor), -(-crop.height // factor), model)
        for model in CLAUDE_PAIR
    ):
        factor += 1
    if factor > 1:
        crop = crop.reduce(factor)
    out = io.BytesIO()
    crop.save(out, format="PNG", optimize=True)
    return out.getvalue()


def prepare_pdf_panels(
    data: bytes,
    page_index: int,
    *,
    document: str,
    architect_file: bool,
    settings: JobValuesSettings,
) -> list[PreparedPanel]:
    """The architect drawings on one page, prepared by code. Read-only: the PDF is never changed.

    `architect_file`: the page is from the architect's own file (a two-file package), so each
    pasted drawing, or the whole page when it has none, is the architect's. Otherwise only the
    drawings GV's heading names `arch` or `reference` are; a `shop` or unheaded drawing never is."""
    from extraction.annotations import read_markup_layer
    from extraction.architect.reader import MEASURED_ARCHITECT_SETTINGS, open_page, page_stamps
    from extraction.ink import read_page_ink
    from extraction.panels import propose_panel_roles
    from extraction.rasterise import render_page
    from extraction.reader import UnreadablePdf, read_pages
    from extraction.stamp_text import stamps_only

    stamps, _pictures = page_stamps(data, page_index)
    roles: list[tuple[Box, str]] = []
    if architect_file:
        if stamps:
            roles = [(box, "arch") for box, _factor in stamps.values()]
        else:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(data)) as pdf:
                page = pdf.pages[page_index]
                whole = Box(
                    Decimal(0), Decimal(0), Decimal(str(page.width)), Decimal(str(page.height))
                )
            roles = [(whole, "arch")]
    else:
        layers = read_markup_layer(
            data, page_index, document_version_id=UUID(int=0), dpi=settings.dpi
        )
        roles = [
            (stamps[proposal.annotation_index][0], proposal.role)
            for proposal in propose_panel_roles(layers.vendor_stamps, layers.markup)
            if proposal.role in ("arch", "reference") and proposal.annotation_index in stamps
        ]
    if not roles:
        return []
    page_sha = hashlib.sha256(read_pages(data)[page_index].content).hexdigest()
    source = stamps_only(data, page_index) if stamps else data
    opened = open_page(
        source,
        page_index,
        drawing_boxes=tuple(box for box, _factor in stamps.values()),
        rows=MEASURED_ARCHITECT_SETTINGS.rows,
        settings=MEASURED_ARCHITECT_SETTINGS,
        dpi=settings.dpi,
    )
    try:
        ink_at = read_page_ink(data, page_index, dpi=settings.dpi).at
    except UnreadablePdf:
        ink_at = None

    def ink_of(box: Box) -> InkClass | None:
        if ink_at is None:
            return None
        return ink_at(opened.pixels(box)).ink

    rendered = render_page(
        data,
        page_index,
        document_version_id=UUID(int=0),
        page_content_hash=page_sha,
        dpi=settings.dpi,
        maximum_pixels=100_000_000,
        vendor_only=True,
    )
    prepared: list[PreparedPanel] = []
    for number, (box, role) in enumerate(roles, start=1):
        chars = tuple(
            c
            for c in opened.chars
            if box.x0 <= c.box.x0
            and c.box.x1 <= box.x1
            and box.top <= c.box.top
            and c.box.bottom <= box.bottom
        )
        panel = Panel(document, page_index, number, role, box, chars, ink_of)  # type: ignore[arg-type]
        prepared.append(
            PreparedPanel(
                panel, _picture(rendered, opened.pixels(box)), _view_rank(panel), page_sha
            )
        )
    return prepared


def choose_panels(
    panels: Sequence[PreparedPanel], settings: JobValuesSettings
) -> tuple[list[PreparedPanel], list[tuple[PreparedPanel, str]]]:
    """The drawings asked about, best first, and every one left out with why."""
    left: list[tuple[PreparedPanel, str]] = [
        (p, "its text names no section, detail, plan or elevation view")
        for p in panels
        if p.view_rank is None
    ]
    ranked = sorted(
        (p for p in panels if p.view_rank is not None),
        key=lambda p: (p.view_rank, p.panel.role != "arch", p.panel.page_index, p.panel.panel),
    )
    chosen = ranked[: settings.max_panels]
    left.extend(
        (p, f"over this job's cap of {settings.max_panels} drawings")
        for p in ranked[settings.max_panels :]
    )
    return chosen, left


def estimated_input_tokens(panel: PreparedPanel, settings: JobValuesSettings) -> int:
    """The question's size in tokens: the picture's documented tiles plus the words."""
    from extraction.slot_reader.claude_output import png_size

    width, height = png_size(panel.picture)
    return visual_tokens(width, height) + settings.prompt_tokens


# ---------------------------------------------------------------------------
# Asking
# ---------------------------------------------------------------------------


def question_packet(panel: PreparedPanel, effort: ClaudeEffort) -> dict[str, object]:
    """What the question shows and asks, signed: the identity a stored answer is reused by."""
    body: dict[str, object] = {
        "reader": "job-values-reader",
        "document_version_id": panel.panel.document,
        "page_index": panel.panel.page_index,
        "source_page_sha256": panel.page_sha256,
        "question_id": f"p{panel.panel.page_index}-d{panel.panel.panel}",
        "prompt_id": JOB_VALUES_PROMPT_ID,
        "prompt_sha256": hashlib.sha256(JOB_VALUES_PROMPT.encode()).hexdigest(),
        "schema_sha256": hashlib.sha256(
            json.dumps(JOB_VALUES_SCHEMA, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "effort": effort,
        "images": {"picture_1": {"sha256": hashlib.sha256(panel.picture).hexdigest()}},
    }
    body["packet_sha256"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return body


def _request(panel: PreparedPanel, model_id: str, settings: JobValuesSettings) -> dict[str, Any]:
    return {
        "modelId": model_id,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"image": {"format": "png", "source": {"bytes": panel.picture}}},
                    {"text": JOB_VALUES_PROMPT},
                ],
            }
        ],
        "inferenceConfig": {"maxTokens": settings.max_tokens, "temperature": 0},
        OUTPUT_CONFIG_KEY: output_config(JOB_VALUES_SCHEMA, settings.effort),
    }


def _readings(model_id: str, data: Mapping[str, Any]) -> dict[str, ReaderReading]:
    out: dict[str, ReaderReading] = {}
    for name in SETTINGS:
        value = data[name]
        box = value["box"]
        out[name] = ReaderReading(
            model_id,
            value["status"],
            value["printed"],
            value["unit"],
            value["label"],
            tuple(box) if len(box) == 4 and any(box) else None,
        )
    return out


def _no_answer(model_id: str, why: str) -> dict[str, ReaderReading]:
    return {name: ReaderReading(model_id, "no_answer", why=why) for name in SETTINGS}


class _Stop:
    def __init__(self) -> None:
        self.cause: str | None = None


def _ask(
    panel: PreparedPanel,
    model_id: str,
    *,
    client: Any,
    settings: JobValuesSettings,
    record_attempt: Callable[[AttemptUsage], None],
    spend_guard: Any,
    stored: Callable[[str, Mapping[str, object]], str | None] | None,
    stop: _Stop,
) -> dict[str, ReaderReading]:
    packet = question_packet(panel, settings.effort)
    index = panel.panel.page_index
    if stored is not None:
        raw = stored(model_id, packet)
        if raw is not None:
            try:
                parsed = json.loads(raw)
                if not _conforms(parsed, JOB_VALUES_SCHEMA):
                    raise MalformedFormAnswer("the stored answer does not match its shape")
                record_attempt(
                    AttemptUsage(
                        model_id,
                        JOB_VALUES_PROMPT_ID,
                        JOB_VALUES_PROMPT_ID,
                        0,
                        0,
                        0,
                        False,
                        page_index=index,
                        raw_response_text=raw,
                        question_packet=packet,
                        reused_from="stored",
                    )
                )
                return _readings(model_id, parsed)
            except (MalformedFormAnswer, json.JSONDecodeError):
                pass
    if stop.cause is not None:
        return _no_answer(model_id, f"not asked: the reading stopped ({stop.cause})")
    require_picture_fits([panel.picture], model_id)
    for attempt in range(2):
        reservation = None
        if spend_guard is not None:
            from extraction.slot_reader.anthropic import SpendCapExceeded

            try:
                reservation = spend_guard.reserve(
                    model_id, estimated_input_tokens(panel, settings), settings.max_tokens
                )
            except SpendCapExceeded:
                stop.cause = "budget"
                return _no_answer(model_id, "not asked: this job's AI budget was reached")
        request = _request(panel, model_id, settings)
        if attempt:
            request["messages"][0]["content"].append(
                {"text": "Your previous reply was malformed. Return the one JSON object only."}
            )
        started = monotonic()
        try:
            response = client.converse(**request)
        except Exception as error:  # noqa: BLE001 - recorded; the reader abstains
            record_attempt(
                AttemptUsage(
                    model_id,
                    JOB_VALUES_PROMPT_ID,
                    JOB_VALUES_PROMPT_ID,
                    None,
                    None,
                    int((monotonic() - started) * 1000),
                    False,
                    type(error).__name__,
                    index,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            return _no_answer(model_id, f"the call failed: {type(error).__name__}")
        if reservation is not None:
            spend_guard.settle(reservation, response)
        input_tokens, output_tokens = _response_usage(response)
        elapsed = int((monotonic() - started) * 1000)
        raw_text: str | None = None
        try:
            raw_text = _response_text(response)
            parsed = claude_answer(response, JOB_VALUES_SCHEMA)
        except MalformedFormAnswer:
            record_attempt(
                AttemptUsage(
                    model_id,
                    JOB_VALUES_PROMPT_ID,
                    JOB_VALUES_PROMPT_ID,
                    input_tokens,
                    output_tokens,
                    elapsed,
                    True,
                    page_index=index,
                    raw_response_text=raw_text,
                    attempt_number=attempt + 1,
                    question_packet=packet,
                )
            )
            continue
        record_attempt(
            AttemptUsage(
                model_id,
                JOB_VALUES_PROMPT_ID,
                JOB_VALUES_PROMPT_ID,
                input_tokens,
                output_tokens,
                elapsed,
                False,
                page_index=index,
                raw_response_text=raw_text,
                attempt_number=attempt + 1,
                question_packet=packet,
            )
        )
        return _readings(model_id, parsed)
    return _no_answer(model_id, "the reader abstained: its answer was malformed twice")


@dataclass
class JobValuesReading:
    results: dict[str, JobValueResult]
    asked: int = 0
    """Drawings asked about (each by both readers)."""
    not_read: list[str] = field(default_factory=list)
    """Every drawing prepared and not asked, with why."""
    stopped: str | None = None
    finds: list[CodeFind] = field(default_factory=list)
    decisions: list[PanelDecision] = field(default_factory=list)


def read_job_values(
    panels: Sequence[PreparedPanel],
    *,
    clients: Mapping[str, Any] | None,
    settings: JobValuesSettings,
    record_attempt: Callable[[AttemptUsage], None],
    spend_guard: Any = None,
    stored: Callable[[str, Mapping[str, object]], str | None] | None = None,
    models: Sequence[str] = CLAUDE_PAIR,
) -> JobValuesReading:
    """Read this job's four values from its architect drawings: code first, then the pair.

    `clients` maps each model id to a client with `converse(**request)`; `None` asks no model (the
    $0 run: code finds only, every drawing reported as not asked)."""
    finds = [find for prepared in panels for find in code_finds(prepared.panel)]
    chosen, left = choose_panels(panels, settings)
    reading = JobValuesReading(results={}, finds=finds)
    reading.not_read = [
        f"p.{p.panel.page_index + 1} drawing {p.panel.panel}: {why}" for p, why in left
    ]
    stop = _Stop()
    answers: dict[tuple[int, str], dict[str, ReaderReading]] = {}
    if clients is None:
        reading.not_read += [
            f"p.{p.panel.page_index + 1} drawing {p.panel.panel}: no model may be asked in this run"
            for p in chosen
        ]
        chosen = []
    else:
        first, second = models
        jobs = [(n, p, m) for n, p in enumerate(chosen) for m in (first, second)]

        def run(job: tuple[int, PreparedPanel, str]) -> None:
            number, prepared, model = job
            answers[(number, model)] = _ask(
                prepared,
                model,
                client=clients[model],
                settings=settings,
                record_attempt=record_attempt,
                spend_guard=spend_guard,
                stored=stored,
                stop=stop,
            )

        with ThreadPoolExecutor(max_workers=max(1, settings.concurrency)) as pool:
            list(pool.map(run, jobs))
        reading.asked = len(chosen)
    for number, prepared in enumerate(chosen):
        a, b = answers[(number, models[0])], answers[(number, models[1])]
        for name in SETTINGS:
            reading.decisions.append(decide_panel(prepared.panel, name, a[name], b[name]))
    reading.stopped = stop.cause
    for name in SETTINGS:
        result = combine(
            name,
            [d for d in reading.decisions if d.setting == name],
            [f for f in finds if f.setting == name],
        )
        if stop.cause is not None and result.outcome.value == "nothing":
            result = JobValueResult(
                name,
                result.outcome,
                reasons=(*result.reasons, "the reading stopped: this job's AI budget was reached"),
                held=result.held,
            )
        reading.results[name] = result
    return reading
