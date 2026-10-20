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

Compiled patterns are cached per registry (`reset_registry()` reads the packs again, and a new
registry object gets fresh patterns). Pure code: no I/O beyond the registry, no model.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable, Iterable
from typing import Final

from vocabulary.product_registry import OTHER_CATEGORY, Product, ProductRegistry, registry

__all__ = [
    "item_product",
    "material_pattern",
    "part_kinds",
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
