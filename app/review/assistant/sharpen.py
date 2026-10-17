"""Sharpen a plain question into a task spec before any tool or helper runs (#1551).

Reviewers often type very plain questions ("is the height ok?", "check page 4"). The owner's ask:
"if the user sends the question in very plain words, sharpen it before delegating to subagents and
tool calling". So every typed chat message (not a full check, not Continue) gets a short **task
spec** first:

    {task, intent, scope: {kind, pages, area, label}, products, what_to_compare, checks_to_run,
     output_shape, assumptions, question_back}

How it is made, cheapest first:

1. **Edited** (`path: edited`): the reviewer corrected an earlier "Understood as" line and resent;
   their spec is used as given (checked by code), no model call.
2. **Code** (`path: code`): a trivially clear one-liner ("How many pages are in this set?",
   "Does the vendor's string add up on page 3?", "Approve this set.") is decided by the rules
   below, $0 and instant.
3. **Model** (`path: model`): everything else asks Claude Haiku 5.5 at low effort, no thinking,
   for ONE JSON object, within a hard time limit (`deadline_seconds`). Late, refused, failed or
   invalid output means no spec: the chat then runs exactly as it did before this step.

Code always checks the spec (`validate`): known intents, checks and answer shapes only; pages the
reviewer named win over the model's; a model's pages must be in the set, an area must be one of
the set's areas (the set pack's page map); products must be known (the product registry's active
products, #1547). Nothing in the spec is a verdict: it only guides the work.

Where the spec goes:

- the lead gets it as a short note **beside** the reviewer's own words, never replacing them
  (`lead_note`, put just before the question);
- every helper brief, code-planned or written by the lead, gets the spec's "compare" and
  "checks" lines (`helper_lines`, added by the engine before the app's footer);
- the screen gets an `understood` block and SSE event: "Understood as: Compare outlet heights
  (architect vs vendor) · page 4 · short answer · Edit";
- code never answers for Claude (orchestrator, 2026-10-17): every message still goes to the lead.
  An unclear question ("Is it ok?" with nothing before it, intent `clarify`) or an order only a
  person carries out (approve, sign off, send, change GV's review: `refuse_action`) only adds a
  short `hint` to the lead's note (`HINT_CLARIFY`, `HINT_ACTION`).

Verification: `tests/review/assistant/test_sharpen.py`, `tests/api/test_assistant_chats_sharpen.py`.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.chat_findings import page_ranges, products_words
from app.review.assistant.chat_scope import area_pages, load_page_map, question_scope
from vocabulary.product_registry import registry

if TYPE_CHECKING:
    from app.review.assistant.chat_engine import ChatEngine

__all__ = [
    "CHECKS",
    "HINT_ACTION",
    "HINT_CLARIFY",
    "INTENTS",
    "OUTPUT_SHAPES",
    "SHARPEN_MAX_TOKENS",
    "SHARPEN_MODEL",
    "SHARPEN_PROMPT_ID",
    "SHARPEN_SYSTEM",
    "SharpenContext",
    "Sharpened",
    "apply_sharpen",
    "code_spec",
    "edited_spec",
    "helper_lines",
    "known_products",
    "lead_note",
    "parse_spec",
    "sharpen",
    "sharpen_prompt",
    "understood_label",
    "validate",
]

_log = logging.getLogger(__name__)

INTENTS: Final = (
    "fact",
    "compare",
    "whole_set",
    "rules",
    "arithmetic",
    "rounds",
    "file",
    "summary",
    "clarify",
    "refuse_action",
)
OUTPUT_SHAPES: Final = ("one_line", "short", "findings", "file")
#: What the work may run, in the chat's own tool words (`ledger_strings`: record the vendor's
#: dimension strings and let code add them up; `rules`: GV's rules in the skill's rules.md).
CHECKS: Final = (
    "read_value",
    "measure_on_drawing",
    "verify_value",
    "ledger_strings",
    "compare_rounds",
    "rules",
    "review_results",
    "make_file",
)
#: The answer's shape when the spec names none, by intent.
_DEFAULT_SHAPE: Final[Mapping[str, str]] = {
    "fact": "one_line",
    "compare": "short",
    "whole_set": "findings",
    "rules": "short",
    "arithmetic": "one_line",
    "rounds": "findings",
    "file": "file",
    "summary": "short",
    "clarify": "one_line",
    "refuse_action": "one_line",
}
_SHAPE_WORDS: Final[Mapping[str, str]] = {
    "one_line": "one-line answer",
    "short": "short answer",
    "findings": "list of findings",
    "file": "a file",
}
_SHAPE_GUIDE: Final[Mapping[str, str]] = {
    "one_line": "one sentence: the value or yes/no, and the fact behind it",
    "short": "a few plain lines, no table, no headings",
    "findings": "the findings (report_finding), with at most two plain lines around them",
    "file": "the file, handed over with present_files, and one or two plain lines",
}

#: The cheap model, its effort and output allowance (a spec is about 100-200 tokens).
SHARPEN_MODEL: Final = "haiku"
SHARPEN_EFFORT: Final = "low"
SHARPEN_MAX_TOKENS: Final = 400
#: An upper bound of one sharpen call's prompt, for the cap check before it is made.
_SHARPEN_PROMPT_TOKENS: Final = 2_500
#: On the Usage page (counted in the chat's spend).
SHARPEN_PROMPT_ID: Final = "review-assistant-chat-sharpen-{provider}-sharpen-v1"

_MAX_TASK: Final = 120
_MAX_ITEMS: Final = 6
_MAX_ITEM: Final = 80
_MAX_ASSUMPTIONS: Final = 3
_MAX_ASSUMPTION: Final = 140
_MAX_QUESTION_BACK: Final = 200
_MAX_PAGES: Final = 60
_PREVIOUS_CHARS: Final = 600
_MICROS: Final = Decimal(1_000_000)


SHARPEN_SYSTEM: Final = """You turn a reviewer's question about a millwork shop-drawing set (the vendor's drawings checked against the architect's) into a task spec for the AI that will answer it. You never answer the question.

Reply with ONE JSON object and nothing else:
{"task": "...", "intent": "...", "scope": {"kind": "whole_set", "pages": [], "area": null}, "products": [], "what_to_compare": [], "checks_to_run": [], "output_shape": "...", "assumptions": [], "question_back": null}

- task: one short imperative line (at most 12 words) in the reviewer's own terms, e.g. "Compare outlet heights (architect vs vendor)".
- intent: fact (one value, count or yes/no), compare (vendor vs architect on some items), whole_set (everything wrong across the set), rules (GV's rules), arithmetic (does a dimension string add up), rounds (what changed between rounds or revisions), file (make a PDF, CSV or other file), summary, clarify, refuse_action (asks to approve, sign off, send, or change GV's review).
- scope: kind "pages" with the pages the question names or clearly means (also a page from the previous exchange, e.g. "that same sheet"); kind "area" only with an area from the list given; else kind "whole_set" with no pages.
- products: only from the products given.
- what_to_compare: 0 to 6 short items the answer must read or compare, e.g. "outlet heights", "drawer vs door", "overall width".
- checks_to_run: only from read_value, measure_on_drawing, verify_value, ledger_strings, compare_rounds, rules, review_results, make_file.
- output_shape: one_line, short, findings or file.
- assumptions: 0 to 3 short notes on what you assumed.
- question_back: only with intent clarify, one short question to the reviewer. Use clarify only when no reasonable reading exists (e.g. "Is it ok?" with nothing before it); otherwise null.
Never add a number or value that is not in the question."""


@dataclass(frozen=True, slots=True)
class SharpenContext:
    """What the sharpen step knows about the message (no I/O beyond the page map)."""

    question: str
    page_count: int = 0
    page_map: Mapping[str, Any] | None = None
    main_file: str | None = None
    products: tuple[str, ...] = ()
    """The products picked (the composer's chips), or the set's own; empty when not stated."""
    known: tuple[str, ...] = ("countertop", "cabinet")
    """Every product the app knows (the registry's active ids: `known_products`)."""
    previous_question: str | None = None
    previous_answer: str | None = None

    @property
    def code_scope(self) -> dict[str, Any]:
        return question_scope(
            self.question,
            page_map=self.page_map,
            page_count=self.page_count,
            main_file=self.main_file,
        )

    @property
    def areas(self) -> dict[str, tuple[str, list[int]]]:
        return area_pages(self.page_map, self.main_file)


@dataclass(slots=True)
class Sharpened:
    """The spec (None: none was made) and how it was made, with its time and cost."""

    spec: dict[str, Any] | None
    path: str
    """`code`, `model`, `edited`, or why there is none: `late`, `failed`, `invalid`, `no_room`,
    `off`."""
    seconds: float = 0.0
    cost_micros: int = 0
    details: dict[str, Any] = field(default_factory=dict)


# ---- products ------------------------------------------------------------------------------------


def known_products() -> tuple[str, ...]:
    """The products the app knows: the product registry's active ids (#1547)."""
    return registry().active_ids()


# ---- the code rules ------------------------------------------------------------------------------

_LEAD_IN: Final = r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
#: Explicit requests for an action a person takes on the review screen.
_REFUSE: Final = (
    re.compile(_LEAD_IN + r"(?:approve|reject)\b", re.IGNORECASE),
    re.compile(
        _LEAD_IN + r"sign(?:\s+(?:it|this|them|the\s+\w+(?:\s+\w+)?))?\s*-?\s*off\b",
        re.IGNORECASE,
    ),
    re.compile(_LEAD_IN + r"send\b.*\bto\s+the\s+vendor\b", re.IGNORECASE),
    re.compile(
        _LEAD_IN + r"(?:change|edit|set|mark|update|fix|overwrite)\b.*"
        r"(?:\bgv'?s\s+review\b|\bin\s+the\s+review\b|\bpass(?:es)?\b|"
        r"\bas\s+(?:passed|approved|fixed|done|right|correct)\b)",
        re.IGNORECASE,
    ),
)
#: A question with nothing to act on (only with nothing before it in the chat).
_VAGUE: Final = re.compile(
    r"^\s*(?:(?:is|are|does|do|was)\s+(?:it|this|that|they|everything|all\s+of\s+it|all)\s+"
    r"(?:ok|okay|fine|good|right|correct|alright|all\s+right|work|look\s+(?:ok|okay|good|right))"
    r"|(?:ok|okay|good|fine|right|correct)|any\s+thoughts|thoughts)\s*\??\s*$",
    re.IGNORECASE,
)
_PAGE_COUNT: Final = re.compile(
    r"^\s*how\s+many\s+(?:pages|sheets)\s+(?:are\s+)?(?:there\s+)?(?:in|does)\s+(?:this|the)\s+"
    r"(?:drawing\s+)?(?:set|package|pdf|submittal)(?:\s+have)?\s*\??\s*$",
    re.IGNORECASE,
)
_STRING: Final = re.compile(
    r"^\s*does\s+the\s+(?:vendor'?s\s+)?(?:([a-z]+)\s+)?(?:dimension\s+)?string\s+add\s+up"
    r"(?:\s+on\s+(?:page|p\.?|sheet)\s*\d{1,3})?\s*\??\s*$",
    re.IGNORECASE,
)
_VALUE_ON_PAGE: Final = re.compile(
    r"^\s*what(?:'s|\s+is|\s+are)\s+the\s+([a-z][a-z' -]{2,60}?)\s+on\s+(?:page|p\.?|sheet)\s*"
    r"\d{1,3}\s*\??\s*$",
    re.IGNORECASE,
)
_CSV: Final = re.compile(
    r"^\s*(?:please\s+)?(?:export|download|give\s+me|make(?:\s+me)?)\b.{0,60}\bcsv\b.{0,20}$",
    re.IGNORECASE,
)
_SUMMARY: Final = re.compile(
    r"^\s*(?:please\s+)?(?:summari[sz]e|give\s+me\s+a\s+summary\s+of)\b(?:\s+\S+){0,10}\s*[.?!]?\s*$",
    re.IGNORECASE,
)

#: The question back the lead may use (or improve) when a question cannot be acted on.
DEFAULT_QUESTION_BACK: Final = (
    'What should I check, and where? For example: "Do the countertop depths on page 7 match the '
    'architect?" or "Run a full check of the whole set."'
)


#: The lead's hint for a question that cannot be acted on as asked (no answer is written by code).
HINT_CLARIFY: Final = "ambiguous: ask one question back about what to check"
#: The lead's hint for an order only a person carries out.
HINT_ACTION: Final = "a GV reviewer approves on the review screen; say so and offer what you can do"


def _spec(
    task: str,
    intent: str,
    *,
    what: Sequence[str] = (),
    checks: Sequence[str] = (),
    shape: str | None = None,
    assumptions: Sequence[str] = (),
    question_back: str | None = None,
    scope: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "task": task,
        "intent": intent,
        "scope": dict(scope or {"kind": "whole_set", "pages": [], "area": None}),
        "products": [],
        "what_to_compare": list(what),
        "checks_to_run": list(checks),
        "output_shape": shape or _DEFAULT_SHAPE[intent],
        "assumptions": list(assumptions),
        "question_back": question_back,
    }


def code_spec(context: SharpenContext) -> dict[str, Any] | None:
    """The spec of a trivially clear one-liner, by code ($0, no model call); None otherwise. Not
    validated yet (`validate` does that for every path)."""
    question = " ".join(context.question.split())
    if not question:
        return None
    if any(rule.search(question) for rule in _REFUSE):
        return _spec("Take an action on GV's review", "refuse_action")
    if _VAGUE.match(question) and not context.previous_question:
        return _spec(
            "Not clear enough to check yet",
            "clarify",
            question_back=DEFAULT_QUESTION_BACK,
        )
    if _PAGE_COUNT.match(question):
        return _spec("Count the pages in the set", "fact")
    found = _STRING.match(question)
    if found:
        kind = (found.group(1) or "").lower()
        what = f"the vendor's {kind} string vs its overall" if kind else "the vendor's string"
        return _spec(
            "Check the vendor's dimension string adds up",
            "arithmetic",
            what=[what.replace("  ", " ")],
            checks=["read_value", "ledger_strings"],
        )
    found = _VALUE_ON_PAGE.match(question)
    if found:
        item = " ".join(found.group(1).split())
        return _spec(f"Read the {item}", "fact", what=[item], checks=["read_value", "verify_value"])
    if _CSV.match(question):
        assumptions = ["Uses the items from the last answer."] if context.previous_question else []
        return _spec(
            "Make a CSV file of the items",
            "file",
            checks=["make_file"],
            assumptions=assumptions,
        )
    if _SUMMARY.match(question):
        return _spec("Summarise the drawing set", "summary")
    return None


def edited_spec(edit: Mapping[str, Any]) -> dict[str, Any]:
    """The spec the reviewer corrected on the screen ("Understood as … Edit"): their task, what to
    compare, pages and answer shape, used as given (then checked by `validate`)."""
    pages = [page for page in edit.get("pages") or [] if isinstance(page, int)]
    intent = str(edit.get("intent") or "")
    what = [str(item) for item in edit.get("what_to_compare") or [] if isinstance(item, str)]
    if intent not in INTENTS or intent in ("clarify", "refuse_action"):
        intent = "compare" if what else "fact"
    return _spec(
        str(edit.get("task") or ""),
        intent,
        what=what,
        checks=[str(item) for item in edit.get("checks_to_run") or [] if isinstance(item, str)],
        shape=str(edit.get("output_shape") or "") or None,
        scope=(
            {"kind": "pages", "pages": pages, "area": None}
            if pages
            else {"kind": "whole_set", "pages": [], "area": None}
        ),
    )


# ---- checking a spec -----------------------------------------------------------------------------


def _clean(value: object, limit: int) -> str:
    return " ".join(str(value).split())[:limit].strip() if isinstance(value, str) else ""


def _strings(value: object, items: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        cleaned = _clean(item, limit)
        if cleaned and cleaned.lower() not in (known.lower() for known in out):
            out.append(cleaned)
    return out[:items]


def _key(text: str) -> str:
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text.lower()).split())


def _scope_label(kind: str, pages: Sequence[int], area: str | None) -> str:
    if kind == "whole_set":
        return "the whole set"
    where = f"page {pages[0]}" if len(pages) == 1 else f"pages {page_ranges(list(pages))}"
    if area and kind == "area":
        return f"{area} ({where})" if pages else area
    return f"{where} ({area})" if area else where


def _checked_scope(given: object, context: SharpenContext, *, edited: bool) -> dict[str, Any]:
    """The spec's scope: pages or an area the reviewer named win; else the spec's own pages (in
    the set) or area (one of the set's areas); else the whole set."""
    code = context.code_scope
    if code["kind"] != "whole_set" and not edited:
        kind, pages, area = str(code["kind"]), list(code["pages"]), code.get("area")
    elif code.get("why") == "whole_set_words" and not edited:
        kind, pages, area = "whole_set", [], None
    else:
        kind, pages, area = "whole_set", [], None
        raw = given if isinstance(given, Mapping) else {}
        wanted = [
            page
            for page in raw.get("pages") or []
            if isinstance(page, int)
            and not isinstance(page, bool)
            and page >= 1
            and (not context.page_count or page <= context.page_count)
        ]
        named_area = raw.get("area")
        match = (
            context.areas.get(_key(named_area))
            if isinstance(named_area, str) and named_area.strip()
            else None
        )
        if raw.get("kind") == "area" and match is not None and match[1]:
            kind, pages, area = "area", list(match[1]), match[0]
        elif raw.get("kind") in ("pages", "area") and wanted:
            kind, pages = "pages", sorted(dict.fromkeys(wanted))[:_MAX_PAGES]
            area = match[0] if match is not None else None
    pages = sorted(set(pages)) if kind != "whole_set" else []
    return {"kind": kind, "pages": pages, "area": area, "label": _scope_label(kind, pages, area)}


def validate(
    raw: object, context: SharpenContext, *, edited: bool = False
) -> dict[str, Any] | None:
    """The spec as code accepts it, or None when it cannot be used (not an object, an unknown
    intent, an edit with no task). Everything else is trimmed to what is allowed."""
    if not isinstance(raw, Mapping):
        return None
    intent = raw.get("intent")
    if intent not in INTENTS:
        return None
    what = _strings(raw.get("what_to_compare"), _MAX_ITEMS, _MAX_ITEM)
    checks = [
        check
        for check in dict.fromkeys(_strings(raw.get("checks_to_run"), len(CHECKS), 40))
        if check in CHECKS
    ]
    shape = raw.get("output_shape")
    if shape not in OUTPUT_SHAPES:
        shape = _DEFAULT_SHAPE[str(intent)]
    known = {product.lower(): product for product in context.known}
    products = [
        known[name.lower()]
        for name in _strings(raw.get("products"), len(known), 40)
        if name.lower() in known
    ] or [product for product in context.products if product in known.values()]
    task = _clean(raw.get("task"), _MAX_TASK)
    if edited and not task:
        return None
    question_back = _clean(raw.get("question_back"), _MAX_QUESTION_BACK) or None
    if intent == "clarify":
        question_back = question_back or DEFAULT_QUESTION_BACK
        what, checks, shape = [], [], "one_line"
    else:
        question_back = None
    if intent == "refuse_action":
        what, checks, shape = [], [], "one_line"
    if intent == "file" or shape == "file":
        shape = "file"
        if "make_file" not in checks:
            checks.append("make_file")
    if not task:
        task = (
            f"{_INTENT_VERB[str(intent)]} {', '.join(what)}" if what else _INTENT_TASK[str(intent)]
        )
    spec = {
        "task": task.rstrip("."),
        "intent": intent,
        "scope": _checked_scope(raw.get("scope"), context, edited=edited),
        "products": products,
        "what_to_compare": what,
        "checks_to_run": checks,
        "output_shape": shape,
        "assumptions": _strings(raw.get("assumptions"), _MAX_ASSUMPTIONS, _MAX_ASSUMPTION),
        "question_back": question_back,
        "hint": (
            HINT_CLARIFY
            if intent == "clarify"
            else HINT_ACTION if intent == "refuse_action" else None
        ),
    }
    spec["label"] = understood_label(spec)
    return spec


_INTENT_VERB: Final[Mapping[str, str]] = {
    "fact": "Read",
    "compare": "Compare",
    "whole_set": "Find what is wrong:",
    "rules": "Check GV's rules on",
    "arithmetic": "Add up",
    "rounds": "Compare rounds:",
    "file": "Make a file of",
    "summary": "Summarise",
    "clarify": "Ask what to check",
    "refuse_action": "Take an action",
}
_INTENT_TASK: Final[Mapping[str, str]] = {
    "fact": "Answer the question",
    "compare": "Compare the vendor with the architect",
    "whole_set": "Find what is wrong across the set",
    "rules": "Check GV's rules",
    "arithmetic": "Check the dimension strings add up",
    "rounds": "Compare the rounds",
    "file": "Make the file",
    "summary": "Summarise the drawing set",
    "clarify": "Not clear enough to check yet",
    "refuse_action": "Take an action on GV's review",
}


def understood_label(spec: Mapping[str, Any]) -> str:
    """The quiet line under the question: "Compare outlet heights (architect vs vendor) · page 4
    · short answer"."""
    if spec["intent"] == "clarify":
        return "Not clear enough to check yet: one question back to you"
    if spec["intent"] == "refuse_action":
        return "An action a GV reviewer takes on the review screen"
    return " · ".join(
        (str(spec["task"]), str(spec["scope"]["label"]), _SHAPE_WORDS[str(spec["output_shape"])])
    )


def parse_spec(text: str) -> object:
    """The JSON object in a model's reply (strict: one object; a code fence around it is
    allowed); None when there is none."""
    body = text.strip()
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", body, re.DOTALL)
    if fenced:
        body = fenced.group(1)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        return json.loads(body[start : end + 1])
    except ValueError:
        return None


# ---- where the spec goes -------------------------------------------------------------------------


def lead_note(spec: Mapping[str, Any]) -> str:
    """The lead's short note, put just before the reviewer's own words (never instead of them)."""
    lines = [
        (
            "How the app read this question (a guide only: the reviewer's own words come next "
            "and win where they differ):"
        ),
        f"Task: {spec['task']}.",
        f"Scope: {spec['scope']['label']}.",
    ]
    if spec.get("products"):
        lines.append(f"Products: {products_words(spec['products']).lower()}.")
    if spec.get("what_to_compare"):
        lines.append(f"Compare: {'; '.join(spec['what_to_compare'])}.")
    if spec.get("checks_to_run"):
        lines.append(f"Checks to run: {', '.join(spec['checks_to_run'])}.")
    shape = str(spec["output_shape"])
    lines.append(f"Output: {_SHAPE_WORDS[shape]} ({_SHAPE_GUIDE[shape]}).")
    if spec.get("assumptions"):
        lines.append(f"Assumed: {' '.join(spec['assumptions'])}")
    if spec.get("hint"):
        lines.append(f"Hint: {spec['hint']}.")
    if spec.get("question_back"):
        lines.append(f"A question back you could ask: {spec['question_back']}")
    return "\n".join(lines)


def helper_lines(spec: Mapping[str, Any] | None) -> str:
    """What every helper brief gets from the spec ("" when there is nothing to add): the items to
    compare and the checks to run, so a helper does the work the reviewer asked for."""
    if not spec or spec.get("intent") in ("clarify", "refuse_action"):
        return ""
    what = spec.get("what_to_compare") or []
    checks = [check for check in spec.get("checks_to_run") or [] if check != "make_file"]
    if not what and not checks:
        return ""
    lines = [
        f"\n\nWHAT THE REVIEWER ASKED FOR (the app's reading of the question): {spec['task']}."
    ]
    if what:
        lines.append(f"Compare: {'; '.join(what)}.")
    if checks:
        lines.append(f"Checks to run: {', '.join(checks)}.")
    return "\n".join(lines)


def sharpen_prompt(context: SharpenContext) -> str:
    """The user message of the model path: the question and what the app knows, as JSON."""
    return json.dumps(
        {
            "question": context.question,
            "previous_question": (context.previous_question or "")[:_PREVIOUS_CHARS] or None,
            "previous_answer_start": (context.previous_answer or "")[:_PREVIOUS_CHARS] or None,
            "pages_in_set": context.page_count or None,
            "areas": [title for title, _pages in context.areas.values()][:40],
            "products": list(context.known),
            "products_picked": list(context.products),
            "app_scope": context.code_scope["label"],
        },
        ensure_ascii=False,
    )


# ---- making the spec -----------------------------------------------------------------------------


class _NoRoomError(Exception):
    """The message's money caps leave no room for the sharpen call."""


#: The model path: the prompt's JSON text in, the reply's text and its cost (millionths) out.
type Ask = Callable[[str], tuple[str, int]]


def sharpen(
    context: SharpenContext,
    *,
    ask: Ask | None = None,
    edit: Mapping[str, Any] | None = None,
    deadline_seconds: float = 1.5,
    clock: Callable[[], float] = monotonic,
) -> Sharpened:
    """The spec for one message: edited, code, else the model within `deadline_seconds` (a late,
    failed or invalid reply gives no spec). Never raises."""
    started = clock()
    if edit is not None:
        spec = validate(edited_spec(edit), context, edited=True)
        if spec is not None:
            return Sharpened(spec, "edited", clock() - started)
    found = code_spec(context)
    if found is not None:
        spec = validate(found, context)
        if spec is not None:
            return Sharpened(spec, "code", clock() - started)
    if ask is None:
        return Sharpened(None, "off", clock() - started)
    reply: dict[str, Any] = {}
    done = threading.Event()

    def work() -> None:
        try:
            reply["text"], reply["cost"] = ask(sharpen_prompt(context))
        except _NoRoomError:
            reply["why"] = "no_room"
        except Exception as failed:  # noqa: BLE001 - no spec; the chat goes on as before
            reply["why"] = "failed"
            reply["error"] = type(failed).__name__
        finally:
            done.set()

    # A thread, so a slow reply is left behind at the deadline (it is still recorded and charged
    # by the engine when it ends).
    threading.Thread(target=work, name="chat-sharpen", daemon=True).start()
    if not done.wait(max(0.0, deadline_seconds)):
        return Sharpened(None, "late", clock() - started)
    seconds = clock() - started
    cost = int(reply.get("cost") or 0)
    if "text" not in reply:
        return Sharpened(None, str(reply.get("why") or "failed"), seconds, cost)
    spec = validate(parse_spec(str(reply["text"])), context)
    if spec is None:
        return Sharpened(None, "invalid", seconds, cost)
    return Sharpened(spec, "model", seconds, cost)


def _cost_text(micros: int) -> str:
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))


def _previous_exchange(history: Sequence[Mapping[str, Any]]) -> tuple[str | None, str | None]:
    """The chat's last question (the user turn's last text that is not a tool result) and the
    start of the answer to it (its last assistant words)."""
    question = answer = None
    for message in reversed(history):
        content = message.get("content")
        blocks = content if isinstance(content, list) else []
        texts = [
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, Mapping) and block.get("type") == "text"
        ]
        if message.get("role") == "assistant" and answer is None and texts:
            answer = "\n".join(text for text in texts if text.strip()) or None
        if message.get("role") == "user" and texts and answer is not None:
            question = texts[-1]
            break
    return question, answer


def _engine_ask(engine: ChatEngine) -> Ask:
    """The model path through the engine's own call: recorded for the Usage page and charged to
    the message, like a second read."""
    from app.review.assistant.chat_provider import ProviderFailure
    from app.review.assistant.chat_request import BY_SHORT, thinking_config

    model = BY_SHORT[SHARPEN_MODEL]

    def ask(prompt: str) -> tuple[str, int]:
        estimate = engine._price(model, _SHARPEN_PROMPT_TOKENS, SHARPEN_MAX_TOKENS)
        if not engine._fits(estimate):
            raise _NoRoomError
        before = engine.spent
        reply = engine._call(
            model=model,
            effort=SHARPEN_EFFORT,
            thinking=thinking_config(model, False),
            system=SHARPEN_SYSTEM,
            tools=(),
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
            final=False,
            max_tokens=SHARPEN_MAX_TOKENS,
            prompt_id=SHARPEN_PROMPT_ID.format(provider=engine.provider.name),
            display=None,
        )
        if reply.stop_reason == "refusal":
            raise ProviderFailure("declined", status=None, retryable=False)
        return reply.text(), max(0, engine.spent - before)

    return ask


def apply_sharpen(
    engine: ChatEngine,
    *,
    history: Sequence[Mapping[str, Any]],
    products: Sequence[str] | None,
    use_model: bool,
    edit: Mapping[str, Any] | None = None,
    deadline_seconds: float = 1.5,
) -> None:
    """The sharpen step on a running message, before `engine.run()`: the spec's block and event,
    the lead's note and the helpers' lines. The message always goes on to the lead. Never raises:
    a failure leaves the message as it was."""
    try:
        _apply(engine, history, products, use_model, edit, deadline_seconds)
    except Exception:  # noqa: BLE001 - the chat goes on as before this step
        _log.exception("assistant chat sharpen step failed")


def _apply(
    engine: ChatEngine,
    history: Sequence[Mapping[str, Any]],
    products: Sequence[str] | None,
    use_model: bool,
    edit: Mapping[str, Any] | None,
    deadline_seconds: float,
) -> None:
    from app.review.assistant.chat_scope import question_of

    question = question_of(engine.user_content)
    previous_question, previous_answer = _previous_exchange(history)
    context = SharpenContext(
        question=question,
        page_count=engine.book.page_count,
        page_map=load_page_map(engine.tools.workspace.set_folder),
        main_file=engine.book.main_file,
        products=tuple(products or ()),
        known=known_products(),
        previous_question=previous_question,
        previous_answer=previous_answer,
    )
    made = sharpen(
        context,
        ask=_engine_ask(engine) if use_model else None,
        edit=edit,
        deadline_seconds=deadline_seconds,
    )
    _log.info(
        "assistant chat sharpen: %s in %.2f s, %d micros", made.path, made.seconds, made.cost_micros
    )
    spec = made.spec
    if spec is None:
        return
    understood = {
        **spec,
        "path": made.path,
        "seconds": round(made.seconds, 3),
        "cost_usd": _cost_text(made.cost_micros),
    }
    index = engine._add_block({"type": "understood", "understood": understood})
    engine.emit("understood", {"index": index, "understood": json.loads(json.dumps(understood))})
    engine.task_spec = spec
    # The note goes just before the reviewer's words: the question stays the last text block (the
    # scope and the guards read it there).
    content = list(engine.user_content)
    at = max(
        (n for n, block in enumerate(content) if block.get("type") == "text"),
        default=len(content),
    )
    content.insert(at, {"type": "text", "text": lead_note(spec)})
    engine.user_content = content
