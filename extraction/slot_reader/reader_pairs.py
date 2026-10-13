"""Which two AI models read the drawings: a setting with a closed list of pairs (#1211).

**Why a setting.** The pair used to be written into the slot reader (Opus 5.5 + Sonnet 5.5).
Opus is about 70% of the reading cost, and public evidence puts Gemini 3.8 Flash at or above Opus
on copying one printed value exactly, so the Gemini pair is offered beside the Claude pair. The
default stays the Claude pair: switching is the admin's call after our own measured bake-off.

**Only the listed pairs.** `GV_READER_PAIR` takes a pair's name from `READER_PAIRS`, nothing else,
so a deployment can never run an unpriced, unpinned or untested model.

**Two models from one maker.** The two-reader rule normally needs two makers (#775: two models of
one maker share mistakes). A listed pair is the one exception, as Opus + Sonnet already were: two
*different* named models, both answers still checked by code (drawn length, ink, geometry), and a
value sealed only on identical text. `is_named_pair` is where that exception is decided, so the
seal, the walls and the row evidence all ask the same question.

**Roles, by position.** The first model is the pair's main reader (the stronger exact reader on
the public evidence: Opus; Gemini 3.8 Flash); the second is the other. On the grounded route every
question goes to both, so the order only fixes how the pair is written in a run's identity.

Nothing here imports a model client or the network.
Source: issue #1211 · Verification: `tests/extraction/slot_reader/test_reader_pairs.py`
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final

__all__ = [
    "CLAUDE_PAIR",
    "DEFAULT_READER_PAIR",
    "GEMINI_FLASH",
    "GEMINI_PAIR",
    "GEMINI_PRO",
    "OPUS",
    "READER_PAIRS",
    "SONNET",
    "ReaderPair",
    "is_named_pair",
    "reader_pair",
]

OPUS: Final = "anthropic.claude-opus-5-5"
SONNET: Final = "anthropic.claude-sonnet-5-5"
GEMINI_FLASH: Final = "google.gemini-3-8-flash"
GEMINI_PRO: Final = "google.gemini-3-1-pro-preview"


@dataclass(frozen=True, slots=True)
class ReaderPair:
    """One allowed pair: its setting name, its two models (main reader first), the effort it is
    asked at when the pair fixes one, and the routes it may be called through."""

    name: str
    models: tuple[str, str]
    effort: str | None
    """`None`: the deployment's `GV_CLAUDE_READER_EFFORT` (the Claude pair). Otherwise the pair's
    own stated effort, the same for both models, sent on every call."""
    providers: tuple[str, ...]
    """The `GV_CLAUDE_READER_PROVIDER` values this pair may be called through."""


CLAUDE_PAIR: Final = ReaderPair(
    name="claude-opus-sonnet",
    models=(OPUS, SONNET),
    effort=None,
    providers=("openrouter", "anthropic"),
)
#: Gemini 3.8 Flash and Gemini 3.1 Pro, through OpenRouter only (Google Vertex, no data kept), both
#: at low effort: the lowest each model offers (Flash: low, medium, high; 3.1 Pro: low, medium,
#: high; Pro cannot turn thinking off), the effort the public single-value reading scores were
#: measured at, and the cheapest (thinking is billed as output). See `gemini.py`.
GEMINI_PAIR: Final = ReaderPair(
    name="gemini-flash-pro",
    models=(GEMINI_FLASH, GEMINI_PRO),
    effort="low",
    providers=("openrouter",),
)

READER_PAIRS: Final[Mapping[str, ReaderPair]] = MappingProxyType(
    {pair.name: pair for pair in (CLAUDE_PAIR, GEMINI_PAIR)}
)
#: Unchanged by #1211: the Claude pair until the bake-off passes and the admin switches.
DEFAULT_READER_PAIR: Final = CLAUDE_PAIR.name

_NAMED: Final = frozenset(frozenset(pair.models) for pair in READER_PAIRS.values())


def reader_pair(name: str) -> ReaderPair:
    """The listed pair of that name; refused, never guessed, for any other."""
    pair = READER_PAIRS.get(name)
    if pair is None:
        raise ValueError(f"GV_READER_PAIR must be one of {', '.join(READER_PAIRS)}: {name!r}")
    return pair


def is_named_pair(model_ids: Iterable[str]) -> bool:
    """Whether these readers are exactly one listed pair's two models (in either order)."""
    models = tuple(model_ids)
    return len(models) == 2 and frozenset(models) in _NAMED
