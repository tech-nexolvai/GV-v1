"""What the product packs teach the engines, compiled once per registry (#1776).

Every product-shaped fact lives in its pack (`vocabulary/products/<id>/pack.yaml`, read by
`vocabulary.product_registry`); this module turns the packs into what code matches with, so a new
product needs no Python:

- `material_pattern(product)`: the words that name a product's materials (`material_words`), for
  the architect's "by others" notes (`architect_items`);
- `part_kinds()`: the parts an architect's note may ask for, with the architect's and the vendor's
  words (`part_kinds`), every active product's, in the registry's order (`architect_items`);
- `item_product(words)`: which product a chat-ledger reading's item name files under (`item_words`
  and `item_codes`, else the product's `words`; `ledger`).

**The products in scope** (#1776, GENERALITY §4.3). A check runs `with scoped(products):` (the simple
full check does, for its whole run); code it calls reads `in_scope()` (default: every active
product). Worker threads get the scope with `carry(fn)`. What the scope decides:

1. **Room context** (`room_pattern`): the core room words (ceiling, door, mirror, sconce, ...) minus
   the words of the products in scope (a mirror is not room context in a check of mirrors), plus
   their `room_words_extra`. With only the kitchen products in scope, it is the core list exactly.
2. **End words** (`end_vocabulary`): the core end kinds plus the in-scope packs' `end_words` (their
   names and aliases, `strict` and `centred`), compared in either order by `verify.ends_match`.
3. **The prompts** (`pack_prompt_parts(products)`): the in-scope packs' checklists, what counts as
   "ours", their extra end words and worked examples, as fixed text for the compare call's system
   prompt (one text per run, so the prompt cache still works) and for the blind look.
4. **What matters most** (`impact_touches`): the in-scope packs' `impact_words`.

Compiled patterns are cached per registry (`reset_registry()` reads the packs again, and a new
registry object gets fresh patterns). Pure code: no I/O beyond the registry, no model.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from dataclasses import dataclass
from typing import Any, Final

from vocabulary.product_packs import EndWord
from vocabulary.product_registry import (
    OTHER_CATEGORY,
    ChecklistItem,
    Product,
    ProductRegistry,
    registry,
)

__all__ = [
    "EndVocabulary",
    "PackPromptParts",
    "carry",
    "end_vocabulary",
    "impact_touches",
    "in_scope",
    "item_product",
    "material_pattern",
    "pack_prompt_parts",
    "part_kinds",
    "room_pattern",
    "scoped",
    "screen_words",
]

_NEVER: Final = re.compile(r"(?!)")
_LOCK: Final = threading.Lock()


def _per_registry[T](build: Callable[[ProductRegistry], T]) -> Callable[[ProductRegistry], T]:
    """`build(registry)`, made once per registry object (a registry read again gets a fresh one;
    registries hold dicts, so they cannot be cache keys themselves)."""
    made: dict[int, tuple[ProductRegistry, T]] = {}

    def get(known: ProductRegistry) -> T:
        with _LOCK:
            found = made.get(id(known))
            if found is not None and found[0] is known:
                return found[1]
        value = build(known)
        with _LOCK:
            if len(made) > 8:
                made.clear()
            made[id(known)] = (known, value)
        return value

    return get


def _words(words: Iterable[str]) -> str:
    return r"\b(?:" + "|".join(re.escape(w).replace("\\ ", " ") for w in words) + r")\b"


@_per_registry
def _materials(known: ProductRegistry) -> dict[str, re.Pattern[str]]:
    return {
        p.id: re.compile(_words(p.material_words), re.IGNORECASE)
        for p in known.products
        if p.material_words
    }


def material_pattern(product: Product) -> re.Pattern[str] | None:
    """The words that name this product's materials ("quartz", "laminate"), or None: none given."""
    return _materials(registry()).get(product.id)


@_per_registry
def _parts(known: ProductRegistry) -> tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]:
    out: list[tuple[str, re.Pattern[str], re.Pattern[str]]] = []
    seen: set[str] = set()
    for product in known.active():
        for part in product.part_kinds:
            if part.kind in seen:
                continue
            seen.add(part.kind)
            out.append(
                (
                    part.kind,
                    re.compile(part.architect, re.IGNORECASE),
                    re.compile(part.vendor, re.IGNORECASE),
                )
            )
    return tuple(out)


def part_kinds() -> tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...]:
    """(the part, the architect's words, the words that count as the vendor naming it) for every
    part kind of the active products, in the registry's order (a kind two packs list: the first)."""
    return _parts(registry())


@_per_registry
def _filers(known: ProductRegistry) -> tuple[tuple[str, frozenset[str], re.Pattern[str]], ...]:
    out = []
    for product in known.active():
        words = frozenset(product.item_words or (w for w in product.words if " " not in w))
        codes = (
            re.compile("|".join(f"(?:{c})" for c in product.item_codes))
            if product.item_codes
            else _NEVER
        )
        out.append((product.id, words, codes))
    return tuple(out)


def item_product(words: Iterable[str]) -> str:
    """The product a reading's item name files under, from its lower-case words: the first active
    product (in the registry's order) one of whose `item_words` (else single `words`) it uses, or
    one of whose `item_codes` a word fully is ("b36"); else `other`."""
    given = list(words)
    present = set(given)
    for pid, own, codes in _filers(registry()):
        if present & own or any(codes.fullmatch(w) for w in given):
            return pid
    return OTHER_CATEGORY


# ---- the products in scope -------------------------------------------------------------------------

_SCOPE: ContextVar[tuple[str, ...] | None] = ContextVar("gv_product_scope", default=None)


@contextmanager
def scoped(products: Sequence[str] | None) -> Iterator[tuple[str, ...]]:
    """Run the block with these products in scope (none: every active product)."""
    token = _SCOPE.set(tuple(products) if products else None)
    try:
        yield in_scope()
    finally:
        _SCOPE.reset(token)


def in_scope() -> tuple[str, ...]:
    """The products in scope: the check's (`scoped`), else every active product."""
    return _SCOPE.get() or registry().active_ids()


def carry[**P, R](fn: Callable[P, R]) -> Callable[P, R]:
    """`fn` run in a copy of the caller's context on each call: a worker thread (a pool's) keeps
    the check's product scope."""
    context = copy_context()

    def run(*args: P.args, **kwargs: P.kwargs) -> R:
        return context.copy().run(fn, *args, **kwargs)

    return run


def _products(known: ProductRegistry, products: Sequence[str] | None) -> tuple[Product, ...]:
    ids = tuple(products) if products is not None else in_scope()
    return tuple(p for pid in known.ordered(ids) if (p := known.get(pid)) is not None)


_MADE: Final[dict[tuple[Any, ...], tuple[ProductRegistry, Any]]] = {}


def _made[T](key: tuple[Any, ...], build: Callable[[], T]) -> T:
    """`build()`, once per registry and key."""
    known = registry()
    full = (id(known), *key)
    with _LOCK:
        found = _MADE.get(full)
        if found is not None and found[0] is known:
            return found[1]  # type: ignore[no-any-return]
    value = build()
    with _LOCK:
        if len(_MADE) > 256:
            _MADE.clear()
        _MADE[full] = (known, value)
    return value


# ---- 1. room context ------------------------------------------------------------------------------


def _alternatives(pattern: str) -> list[str]:
    """A regular expression's top-level alternatives (`a|b(?:c|d)|e` -> a, b(?:c|d), e)."""
    out, depth, start, escaped = [], 0, 0, False
    for at, char in enumerate(pattern):
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "|" and depth == 0:
            out.append(pattern[start:at])
            start = at + 1
    out.append(pattern[start:])
    return [term for term in out if term]


def _room(core: str, ids: tuple[str, ...]) -> str:
    products = _products(registry(), ids)
    ours = [w for p in products for w in p.words]
    kept = [
        term
        for term in _alternatives(core)
        if not any(re.search(term, word, re.IGNORECASE) for word in ours)
    ]
    extra = [w for p in products for w in p.room_words_extra]
    return "|".join([*kept, *(_words([w]) for w in extra)]) or r"(?!)"


def room_pattern(core: str, products: Sequence[str] | None = None) -> str:
    """The room-context words for the products in scope: each alternative of `core` that matches
    none of their `words` (a mirror is ours in a check of mirrors), then their `room_words_extra`.
    """
    ids = tuple(products) if products is not None else in_scope()
    return _made(("room", core, ids), lambda: _room(core, ids))


# ---- 2. end words ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EndVocabulary:
    """The end kinds code knows for the products in scope, in matching order (the first wins)."""

    kinds: tuple[tuple[str, re.Pattern[str]], ...]
    strict: frozenset[str]
    centred: frozenset[str]
    names: tuple[str, ...]
    """The packs' end words, for the prompts (after the core `END_WORDS`)."""


def _end_pattern(end: EndWord) -> re.Pattern[str]:
    return re.compile(_words(end.words), re.IGNORECASE)


def end_vocabulary(
    core: Sequence[tuple[str, re.Pattern[str]]],
    strict: Iterable[str],
    centred: Iterable[str],
    room: str,
    products: Sequence[str] | None = None,
) -> EndVocabulary:
    """`core` (the kitchen's end kinds, `room` first) for the products in scope: the room kind's
    words from `room_pattern(room)`, then the in-scope packs' end words (each its own kind; `strict`
    and `centred` as the pack says), then the rest of the core kinds."""
    ids = tuple(products) if products is not None else in_scope()
    core = tuple(core)
    strict, centred = frozenset(strict), frozenset(centred)

    def build() -> EndVocabulary:
        ends = [e for p in _products(registry(), ids) for e in p.end_words]
        room_now = re.compile(room_pattern(room, ids), re.IGNORECASE)
        head = [("room", room_now)] if core and core[0][0] == "room" else []
        rest = list(core[1:] if head else core)
        return EndVocabulary(
            kinds=(*head, *((e.name, _end_pattern(e)) for e in ends), *rest),
            strict=strict | {e.name for e in ends if e.strict},
            centred=centred | {e.name for e in ends if e.centred},
            names=tuple(e.name for e in ends),
        )

    return _made(("ends", id(core), room, ids), build)


def screen_words(core: Mapping[str, str]) -> dict[str, str]:
    """How the screens say each end word: `core`, then every active pack's `screen_words`."""
    return {
        **core,
        **{e.name: e.screen_words for p in registry().active() for e in p.end_words},
    }


# ---- 3. the prompts -------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackPromptParts:
    """What the in-scope packs add to a prompt (#1776), for the prompt owners to place: the compare
    call's system prompt (`compare_text`) and the blind look's (`blind_text`) use it as it is."""

    products: tuple[str, ...]
    checklists: tuple[tuple[str, tuple[ChecklistItem, ...]], ...]
    """(plural, checklist) per product in scope."""
    ours: tuple[tuple[str, str], ...]
    """(plural, what counts as ours) per product whose pack says it (`ours`)."""
    end_words: tuple[EndWord, ...]
    examples: tuple[tuple[str, Mapping[str, Any]], ...]
    """(plural, `example.json`) per product whose pack has one."""

    def compare_text(self) -> str:
        """The compare call's system-prompt part: the checklist of each product in scope, what
        counts as ours beyond the millwork, the extra end words and the products' worked examples.
        "" when nothing is in scope. The same bytes for the same products (the prompt cache)."""
        lines: list[str] = []
        if self.checklists:
            lines.append("What to compare for each product in scope:")
            for plural, items in self.checklists:
                lines.append(f"{plural}:")
                lines.extend(f"- {i.name.replace('_', ' ')}: {i.text}" for i in items)
        if self.ours:
            lines.append(
                "Ours (millwork: true) also means these products in scope, even where rule 5 "
                "calls such things room features:"
            )
            lines.extend(f"- {plural}: {what}." for plural, what in self.ours)
        if self.end_words:
            lines.append(
                "More end words for these products (rule 3; start an end with one, then what in "
                "brackets): "
                + "; ".join(
                    e.name + (f" ({', '.join(e.aliases)})" if e.aliases else "")
                    for e in self.end_words
                )
                + "."
            )
        for plural, example in self.examples:
            lines.append(f"Example for {plural.lower()} (made-up values):")
            lines.append(str(example["input"]))
            lines.append("Answer:")
            lines.append(json.dumps(example["reply"], separators=(",", ":")))
        return "\n".join(lines)

    def blind_text(self) -> str:
        """The blind look's system-prompt part: what counts as ours beyond the millwork and the
        extra end words. "" for products whose packs add neither (the kitchen's)."""
        if not self.ours and not self.end_words:
            return ""
        parts: list[str] = []
        if self.ours:
            parts.append(
                " In this review these products count as millwork too (answer millwork: yes for "
                "them): " + "; ".join(f"{plural}: {what}" for plural, what in self.ours) + "."
            )
        if self.end_words:
            parts.append(
                " More end words you may start an end with: "
                + ", ".join(e.name for e in self.end_words)
                + "."
            )
        return "".join(parts)


def pack_prompt_parts(products: Sequence[str] | None = None) -> PackPromptParts:
    """What the packs of these products (none given: those in scope) add to the prompts."""
    ids = tuple(products) if products is not None else in_scope()

    def build() -> PackPromptParts:
        chosen = _products(registry(), ids)
        return PackPromptParts(
            products=tuple(p.id for p in chosen),
            checklists=tuple((p.plural, p.checklist) for p in chosen if p.checklist),
            ours=tuple((p.plural, p.ours) for p in chosen if p.ours),
            end_words=tuple(e for p in chosen for e in p.end_words),
            examples=tuple((p.plural, p.example) for p in chosen if p.example is not None),
        )

    return _made(("prompt", ids), build)


# ---- 4. what matters most -------------------------------------------------------------------------


def impact_touches(
    products: Sequence[str] | None = None,
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """(product plural, its `impact_words`) for the products in scope whose packs give some."""
    ids = tuple(products) if products is not None else in_scope()

    def build() -> tuple[tuple[str, re.Pattern[str]], ...]:
        return tuple(
            (p.plural, re.compile(_words(p.impact_words), re.IGNORECASE))
            for p in _products(registry(), ids)
            if p.impact_words
        )

    return _made(("impact", ids), build)
