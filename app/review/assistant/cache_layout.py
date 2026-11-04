"""The `shared` prompt layout: one canonical prompt prefix per model for the full check's
specialists (rebuild plan Step 5, `GV_ASSISTANT_CACHE_LAYOUT=shared`; `legacy` is the default).

Why: in the last big paid run, writing the prompt cache was 51% of the bill. The API caches a
prompt by its exact prefix, in the order tools, then system prompt, then messages (Anthropic's
prompt-caching docs), and a change anywhere in that prefix makes everything after it a new write.
Every kind of roster specialist had its own system prompt (its job and its own knowledge files)
and its own tool list, so each kind wrote its own prefix, and on OpenRouter each helper that started
at the same time wrote it again (the warm-up ran only on Anthropic's own API).

Under `shared`, the roster's specialists that share the helpers' tools (the page helpers for
numbers and for drawn things, the tag readers, the gap closers, the verifiers and the lead review)
get, on each model:

- **one tool list** (`SPECIALIST_TOOLS`: every helper tool and `update_finding`), the same for every
  kind and never changed during a conversation; a kind that must not use a tool is told so in its
  first message (`job_text`) and code refuses the call (`refused`), so asking is not the only
  guard;
- **one system prompt** (`specialist_system`): the helpers' shared core, the set pack's shapes and
  the union of the kinds' knowledge files and GV's local examples and lessons, with no kind's job
  in it;
- **each kind's job, tools and brief last**, in its first message (`job_text`), after the
  pictures that kinds looking at the same pages share (the page helpers' overview pictures,
  `ChatEngine.page_group`), marked with a cache breakpoint (`shared_opening`);
- **one effort per model** (`specialist_effort`), so a kind's effort never invalidates another's
  cache: the highest effort any of these kinds ran at before, so no kind thinks less than it did
  (the lead review, alone on its model, keeps its own).

The breakpoints of such a call (`SPECIALIST_CACHE`): the system prompt (which caches the tools with
it) and the newest block, plus the one after the shared pictures when there are any: at most 3 of
the API's 4.

Pure code and file reads; no AI call.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from app.review.assistant.chat_findings import UPDATE_FINDING
from app.review.assistant.chat_request import (
    CACHE_LAYOUTS,
    EFFORTS,
    SINGLE_SHOT,
    CachePlan,
    cache_mark,
)
from app.review.assistant.chat_tools import HELPER_TOOLS
from app.review.assistant.full_check_specialists import (
    HELPER_CORE,
    KINDS,
    PACK_SHAPES,
    knowledge_text,
    local_extras,
)

__all__ = [
    "SPECIALIST_CACHE",
    "SPECIALIST_KNOWLEDGE",
    "SPECIALIST_TOOLS",
    "TOOL_NAMES",
    "cache_layout",
    "is_shared",
    "job_text",
    "kind_job",
    "one_off",
    "refused",
    "shared_opening",
    "specialist_effort",
    "specialist_system",
]

#: Every tool a roster specialist of the helpers' family may be given, in one fixed order.
SPECIALIST_TOOLS: Final[tuple[Mapping[str, Any], ...]] = (*HELPER_TOOLS, UPDATE_FINDING)
TOOL_NAMES: Final = tuple(str(tool["name"]) for tool in SPECIALIST_TOOLS)
#: The union of the knowledge the kinds carried in their own system prompts (each file whole, in
#: the pack's order), so no kind loses a section it had.
SPECIALIST_KNOWLEDGE: Final[tuple[tuple[str, str | None], ...]] = (
    ("glossary.md", None),
    ("reading-drawings.md", None),
    ("what-dimensions-measure.md", None),
    ("vendor-mistakes.md", None),
    ("field-cut-and-ends.md", None),
    ("typical-sizes.md", None),
)
#: The kinds whose GV local examples the shared system carries (each example names its kinds).
_EXAMPLE_KINDS: Final = ("numbers", "drawn", "prover", "tags", "gap")
#: A specialist's breakpoints: the system prompt (tools cached with it) and the newest block; a
#: shared picture group marks its own (`shared_opening`).
SPECIALIST_CACHE: Final = CachePlan(tools=None, system="5m", newest="5m")


def cache_layout(engine: Any) -> str:
    """The engine's layout (`EngineSettings.cache_layout`): `shared`, else `legacy`."""
    layout = getattr(getattr(engine, "settings", None), "cache_layout", "legacy")
    return layout if layout in CACHE_LAYOUTS else "legacy"


def is_shared(engine: Any) -> bool:
    return cache_layout(engine) == "shared"


def specialist_system(local: Path | None = None) -> str:
    """The one system prompt of the roster's specialists under `shared`: the helpers' core, the
    set pack's shapes and the union of the kinds' knowledge, with GV's local examples and
    lessons. No kind's job: that comes in each specialist's first message (`job_text`)."""
    parts = [
        HELPER_CORE,
        PACK_SHAPES,
        (
            "YOUR JOB, YOUR PAGES AND YOUR TOOLS are in your first message: several kinds of "
            "helper share this prompt, so do only your own job, with only the tools your message "
            "names."
        ),
        (
            "What a senior millwork reviewer knows (background: a value is Wrong only against "
            "the architect's printed value, the vendor's own string, or a rule in rules.md):"
        ),
        knowledge_text(SPECIALIST_KNOWLEDGE),
    ]
    extras = local_extras(_EXAMPLE_KINDS, local)
    if extras:
        parts.append(extras)
    return "\n\n".join(parts)


def job_text(job: str, allowed: Collection[str] | None) -> str:
    """A specialist's job and its tools, first in its first message (`None`: every tool)."""
    if allowed is None:
        tools = "every tool listed"
    else:
        tools = ", ".join(name for name in TOOL_NAMES if name in allowed)
    return (
        f"YOUR JOB: {job}\n\nYOUR TOOLS: {tools}. Any other tool is for the other kinds of "
        "helper: code refuses it."
    )


def refused(allowed: Collection[str] | None, name: str) -> bool:
    """A tool call code refuses: one not among the specialist's own tools."""
    return allowed is not None and name not in allowed


def shared_opening(blocks: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A specialist's opening blocks (the pictures several kinds share) with a cache breakpoint
    on the last one; none when there are no blocks."""
    out = [dict(block) for block in blocks]
    if out:
        out[-1] = cache_mark(out[-1])
    return out


def specialist_effort(efforts: Collection[str]) -> str:
    """One effort for every specialist of a model: the highest of `efforts` (the ones its kinds
    ran at before), so none thinks less than it did."""
    return max(efforts, key=EFFORTS.index)


def one_off(engine: Any) -> CachePlan | None:
    """A one-off call's cache plan: under `shared`, no breakpoint on its newest block
    (`chat_request.SINGLE_SHOT`: it is never sent again, so a write there is never read);
    `None` (the legacy three) otherwise."""
    return SINGLE_SHOT if is_shared(engine) else None


def kind_job(kind: str) -> str:
    """A helper kind's job line (`full_check_specialists.KINDS`)."""
    return KINDS[kind].job
