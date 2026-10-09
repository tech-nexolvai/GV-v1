"""Score the architect reader against a hand key: found, held, missing and extra, page by page (#1052).

The key is a person's reading of each page's architect drawing: the widths it prints, as text and
exact inches (`data/goldset/arch-key-*/arch_key.json`, local and never committed). The reader's
result for a page is reduced here to plain spans — printed text, printed inches, usable inches,
held reason — so the scoring has no PDF in it and can be tested on a synthetic key.

**How a page is scored, on exact inches only.** The key's widths are a multiset. Usable spans
(value not held) are matched to key widths first, equal value to equal value; then held spans are
matched to the key widths still open. So for each key width the answer is one of:

* **found** — a usable span has its value;
* **held** — a span printing its value was read but held, with the reason;
* **missing** — no span printing its value was read.

What is left over is reported too: **extra usable** values (read, usable, not in the key — either
the key leaves a printed width out, or the reader is wrong: a person looks at each one) and
**extra held** values. A wrong value cannot be named by the scorer, because the key says *what* is
printed, not *where*; every extra usable value is listed so a person checks it against the page.

Nothing here rounds. Source: issue #1052 · Verification: `tests/eval/test_architect_reader.py`
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction

__all__ = [
    "KeyPage",
    "PageScore",
    "ReadSpan",
    "Scorecard",
    "key_pages",
    "score_page",
    "score_pages",
]


@dataclass(frozen=True, slots=True)
class KeyPage:
    """One page of the hand key: the architect widths it prints, exact."""

    page: int
    """1-based, as a person counts pages."""
    widths: tuple[Fraction, ...]
    texts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReadSpan:
    """One labelled span the reader returned, as the scorer needs it."""

    text: str
    printed_inches: Fraction | None
    inches: Fraction | None
    """Usable value; `None` when held."""
    held_reason: str | None
    on_outline: bool | None = None


@dataclass(frozen=True, slots=True)
class PageScore:
    page: int
    found: tuple[Fraction, ...]
    held: tuple[tuple[Fraction, str], ...]
    missing: tuple[Fraction, ...]
    extra_usable: tuple[tuple[Fraction, str], ...]
    """Usable values the key does not list: `(inches, printed text)`."""
    extra_held: tuple[tuple[Fraction | None, str, str], ...]
    """Held spans the key does not list: `(printed inches, text, reason)`."""

    @property
    def key_count(self) -> int:
        return len(self.found) + len(self.held) + len(self.missing)


@dataclass(frozen=True, slots=True)
class Scorecard:
    pages: tuple[PageScore, ...]
    totals: Mapping[str, int] = field(default_factory=dict)


def _exact(value: object) -> Fraction:
    if isinstance(value, bool):
        raise TypeError("a width cannot be a bool")
    if isinstance(value, (int, str, Fraction)):
        return Fraction(value)
    raise ValueError(f"a key width must be an int or exact text, not {value!r}")


def key_pages(key: Mapping[str, object]) -> tuple[KeyPage, ...]:
    """The key's pages, from the `arch_key.json` shape: `pages[].page`, `pages[].arch_widths[]`
    with `in` (an int or exact text such as `"85/2"`) and `text`."""
    pages = key.get("pages")
    if not isinstance(pages, list):
        raise TypeError("the key has no pages list")
    result: list[KeyPage] = []
    for entry in pages:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
            raise TypeError(f"a key page has no page number: {entry!r}")
        widths = entry.get("arch_widths") or []
        if not isinstance(widths, list):
            raise TypeError(f"page {entry['page']}'s arch_widths is not a list")
        result.append(
            KeyPage(
                page=int(entry["page"]),
                widths=tuple(_exact(width["in"]) for width in widths),
                texts=tuple(str(width.get("text", "")) for width in widths),
            )
        )
    return tuple(result)


def _take(pool: list[Fraction], value: Fraction | None) -> bool:
    if value is not None and value in pool:
        pool.remove(value)
        return True
    return False


def score_page(page: int, key_widths: Sequence[Fraction], spans: Iterable[ReadSpan]) -> PageScore:
    """One page's score: usable spans matched first, then held ones, on exact inches."""
    open_widths = list(key_widths)
    found: list[Fraction] = []
    extra_usable: list[tuple[Fraction, str]] = []
    labelled = [span for span in spans if span.printed_inches is not None or span.text]
    held_spans: list[ReadSpan] = []
    for span in labelled:
        if span.inches is None:
            held_spans.append(span)
            continue
        if _take(open_widths, span.inches):
            found.append(span.inches)
        else:
            extra_usable.append((span.inches, span.text))
    held: list[tuple[Fraction, str]] = []
    extra_held: list[tuple[Fraction | None, str, str]] = []
    for span in held_spans:
        reason = span.held_reason or "held"
        if span.printed_inches is not None and _take(open_widths, span.printed_inches):
            held.append((span.printed_inches, reason))
        else:
            extra_held.append((span.printed_inches, span.text, reason))
    return PageScore(
        page=page,
        found=tuple(found),
        held=tuple(held),
        missing=tuple(open_widths),
        extra_usable=tuple(extra_usable),
        extra_held=tuple(extra_held),
    )


def score_pages(key: Sequence[KeyPage], read: Mapping[int, Sequence[ReadSpan]]) -> Scorecard:
    """Every page in the key or read, scored; totals across them."""
    widths = {entry.page: entry.widths for entry in key}
    pages = tuple(
        score_page(page, widths.get(page, ()), read.get(page, ()))
        for page in sorted(set(widths) | set(read))
    )
    totals = {
        "key_widths": sum(page.key_count for page in pages),
        "found": sum(len(page.found) for page in pages),
        "held": sum(len(page.held) for page in pages),
        "missing": sum(len(page.missing) for page in pages),
        "extra_usable": sum(len(page.extra_usable) for page in pages),
        "extra_held": sum(len(page.extra_held) for page in pages),
    }
    return Scorecard(pages=pages, totals=totals)
