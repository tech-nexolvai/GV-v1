"""The products a check covers, as a strict scope (Anant, 2026-10-18).

The owner: "When someone selects Cabinet or Countertop from the toggle, only that is compared, and
the other is not. If they select all, it checks all; any two, any three, as new products come." So
the products picked in the chat's chips are not a hint for the AI; they decide what is checked,
ruled, counted and drawn, for a full check and for a typed question alike:

- **One scope.** `ProductScope.of(picked)`: the picked product ids, checked against the product
  registry's ACTIVE products (`vocabulary/products.yaml`); nothing picked means every active
  product (All is the default, 2026-10-18: a set's stated product is not a scope). `every` is true when every active
  product is in scope ("All"). It is stored on the message (its `scope` block's `products`) and
  shown on screen ("Checked: Countertops").
- **Before any AI call**, only the in-scope products' checklists, skill files and GV rules are
  given (`skills_export.rules_markdown`, `full_check_brief.product_checklist`), and GV Review's
  recorded results are cut to the in-scope products (`scoped_document`).
- **Code steps** only keep in-scope items: GV's rule checks by each rule's own product
  (`record_products`, from the rulebook), the vendor consistency check, the tag reader and the
  drawn-things helpers by each item's category; a code-made item named "other" gets a product's
  category when its name uses only that product's words (`category_for`).
- **Re-filed before dropped.** Under a pick that is not All, an "other" or unknown item (whoever
  made it) is re-filed to a product by the registry's `words` and `hints` of every product, on its
  name, then its other words, then its page's printed words and area title (`refiled`): a pull
  size is a cabinet item, a pendant light is lighting (never picked while not active), a tag of
  "counter / cabinets" stays in either pick.
- **A hard filter** (`FindingBook` refuses an out-of-scope finding; `final_blocks` drops any that
  still reached the answer's blocks): a finding whose category is not in scope never reaches the
  answer, its counts, the cloud drawing, the report, the CSV, the tracker or the grey and amber
  lists, even when a helper ignores its brief. `other` and an unknown category are in scope only
  when every product is. The count dropped is kept in the trust block (`product_scope`), not shown.
  The Not-compared rows that are not findings (the sheet gaps, the finish check's notes, the simple
  full check's labels with no architect size) are cut the same way (`scoped_gaps`).
- **The longest phrase wins** when a text is read for its products: "vanity top" is a countertop's
  phrase, so the "vanity" inside it does not also name a cabinet (`named_products`).
- **Cost.** An area whose every page names only out-of-scope products (by the registry's `words`,
  read from the pages' printed text) is not sent to a helper (`outside_unit`); a page that names
  no product, or even hints at one in scope (its `hints`: "sink" for countertops), still goes. The
  full check's fixed price shrinks with the pages left to compare (`scaled_price`).

Pure code: no AI call, no database. Verification: `tests/review/assistant/test_product_scope.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from vocabulary.product_registry import OTHER_CATEGORY, registry

__all__ = [
    "CODE_MADE",
    "ProductScope",
    "category_for",
    "filed_under",
    "final_blocks",
    "main_product",
    "named_products",
    "no_rules_note",
    "outside_unit",
    "refiled",
    "scaled_price",
    "scoped_document",
    "scoped_gaps",
    "scoped_loader",
]

#: Who makes a finding by code (not an AI that was told the products): their "other" items may get a
#: product's category from their words (`category_for`).
CODE_MADE: Final = frozenset(
    {
        "ledger",
        "consistency",
        "tag_reader",
        "gv_review",
        "code",
        "architect_items",
        "pairs",
        "simple_check",
        "filler_range",
        "run_fit",
    }
)
_NOT_WORD: Final = re.compile(r"[^0-9a-z]+")


def _plain(text: object) -> str:
    """Lower case, letters and digits only, one space between words, a space at each end."""
    return f" {' '.join(_NOT_WORD.sub(' ', str(text or '').lower()).split())} "


@dataclass(frozen=True, slots=True)
class ProductScope:
    """The products one message checks (registry ids, in the registry's order)."""

    products: tuple[str, ...]
    every: bool
    """Every active product is in scope ("All"): `other` and unknown categories are kept too."""

    @classmethod
    def of(cls, picked: Sequence[str] | None) -> ProductScope:
        """The picked ACTIVE products (an unknown or inactive id is left out); none picked (or none
        left): every active product. All is the default (2026-10-18): a drawing set's stated
        product is not a scope (a set stated "countertop" often holds cabinets too); the reviewer
        narrows by picking chips."""
        known = registry()
        active = known.active_ids()
        chosen = [pid for pid in known.ordered(picked or ()) if pid in active] or list(active)
        return cls(tuple(chosen), set(chosen) == set(active))

    def allows(self, category: object) -> bool:
        """Whether a finding of this category is in scope: a product in scope; `other` or an
        unknown category only when every product is."""
        if self.every:
            return True
        return isinstance(category, str) and category in self.products

    @property
    def label(self) -> str:
        """In plain words: "Countertops", "Countertops and Cabinets"."""
        return registry().words(self.products)

    def as_dict(self) -> dict[str, Any]:
        """What the message's `scope` block says of its products."""
        return {
            "products": list(self.products),
            "all_products": self.every,
            "products_label": self.label,
        }

    def out_words(self, category: object) -> str:
        """An out-of-scope category in plain words ("cabinets", "items of no product")."""
        product = registry().get(str(category or ""))
        return product.plural.lower() if product is not None else "items of no product"

    def refusal(self, category: object) -> str:
        """What a helper is told when it reports an out-of-scope item (nothing is recorded)."""
        return (
            f"not recorded: {self.out_words(category)} are outside this check, which covers "
            f"{self.label.lower()} only. Report only {self.label.lower()} items."
        )


# ---- which product a text names ----------------------------------------------------------------


def _spans(plain: str, word: str) -> list[tuple[int, int]]:
    """Where a registry word or phrase stands in the plain text, as (start, end) of its spaced
    form (" vanity top ")."""
    spaced = _plain(word)
    if spaced.strip() == "":
        return []
    out: list[tuple[int, int]] = []
    at = plain.find(spaced)
    while at >= 0:
        out.append((at, at + len(spaced)))
        at = plain.find(spaced, at + 1)
    return out


def _uses(plain: str, words: Iterable[str], shadows: Sequence[tuple[int, int]] = ()) -> bool:
    """Whether the text uses one of the words. A word standing only inside a LONGER phrase of
    another product (`shadows`) does not count: "vanity top" is a countertop's, so the "vanity"
    in it is not a cabinet's (the longest phrase wins)."""
    joined = plain.replace(" ", "")
    for word in words:
        spaced = _plain(word)
        for start, end in _spans(plain, word):
            if not any(s <= start and end <= e and e - s > end - start for s, e in shadows):
                return True
        if " " in spaced.strip() and spaced.replace(" ", "") in joined:
            return True
    return False


def named_products(text: object, *, hints: bool = False, every: bool = False) -> set[str]:
    """The active products whose registry `words` the text uses ("toe kick", "toekick" and "Toe-
    kick" all count); with `hints`, their `hints` too ("sink": maybe a countertop); with `every`,
    the products not active yet too (a pendant light is lighting, never a cabinet). A product
    with no words is never named. A word an ACTIVE product lists in its pack's `shared_words` (a
    dresser's "drawer", #1776) names no product: the item is filed by its drawing's own product.
    The longest phrase wins: a word that stands only inside a
    longer phrase of another product names that product alone ("vanity top" is a countertop's,
    not also a cabinet's "vanity")."""
    plain = _plain(text)
    known = registry()
    products = list(known.products if every else known.active())
    # Words an active product shares with another (a dresser's drawer, #1776) name no product.
    shared = {w for p in known.active() for w in p.shared_words}

    def own(words: Iterable[str]) -> list[str]:
        return [w for w in words if w not in shared] if shared else list(words)

    def phrases(product: Any) -> list[str]:
        return own([*product.words, *(product.hints if hints else ())])

    found: dict[str, list[tuple[int, int]]] = {
        product.id: [span for word in phrases(product) for span in _spans(plain, word)]
        for product in products
    }
    named: set[str] = set()
    for product in products:
        shadows = [span for other, spans in found.items() if other != product.id for span in spans]
        if _uses(plain, own(product.words), shadows) or (
            hints and _uses(plain, own(product.hints), shadows)
        ):
            named.add(product.id)
    return named


def main_product(text: object) -> str | None:
    """The one active product a drawing's or a page's printed words are mostly about: the product
    whose registry `words` stand there most often (each phrase counted where it stands, the longest
    phrase winning), when one has strictly the most; else `None`. A run of base cabinets with its
    fillers and drawers is a cabinet run even when its top is named once; hints never count."""
    plain = _plain(text)
    known = registry()
    products = list(known.active())
    found = {p.id: [span for word in p.words for span in _spans(plain, word)] for p in products}
    counts: dict[str, int] = {}
    for product in products:
        shadows = [span for other, spans in found.items() if other != product.id for span in spans]
        counts[product.id] = sum(
            1
            for start, end in found[product.id]
            if not any(s <= start and end <= e and e - s > end - start for s, e in shadows)
        )
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    if not ranked or ranked[0][1] == 0 or (len(ranked) > 1 and ranked[1][1] == ranked[0][1]):
        return None
    return ranked[0][0]


def _own_name(item: object) -> str:
    """An item's own words: its name after the area's "Area: " (the area is the page's words)."""
    name = " ".join(str(item or "").split())
    _head, sep, rest = name.partition(": ")
    return rest if sep and rest else name


def _words_of(page_text: str | Callable[[], str]) -> str:
    return page_text() if callable(page_text) else page_text


def _filed(finding: Mapping[str, Any], page_text: str | Callable[[], str]) -> tuple[set[str], bool]:
    """`filed_under`'s products, and whether they came from the page (step 4) rather than the
    item's own words."""
    own = _own_name(finding.get("item"))
    for text, hints in (
        (own, True),
        (str(finding.get("item") or ""), False),
        (_own_text(finding), True),
    ):
        named = named_products(text, hints=hints, every=True)
        if named:
            return named, False
    return named_products(_words_of(page_text), hints=True, every=True), True


def filed_under(finding: Mapping[str, Any], page_text: str | Callable[[], str] = "") -> set[str]:
    """The products an item is filed under, the first step that names any:

    1. its own name, after the area ("Vanity top width" is a countertop's, whatever the area is
       called): the registry's words and hints ("upper outlet", "top of counter"; "counter /
       cabinets tags" names both);
    2. its whole name's words ("Countertop: overall length");
    3. its other words (why, both values, the question and help it gives);
    4. every product its page's printed words and area title show, words and hints (orchestrator,
       2026-10-19, the inclusive rule: an outlet or a string on a drawing of several products
       counts for EACH of them, so a one-product pick never loses an item that may be its own).

    An item named by its own words stays strict (steps 1-3). Products not active yet count too (a
    pendant light is lighting). Empty: no product."""
    return _filed(finding, page_text)[0]


def category_for(finding: Mapping[str, Any], page_text: str | Callable[[], str] = "") -> str:
    """A finding's category when every product is in scope (All: each item once): its own when it
    is an active product; for a code-made `other` item (`CODE_MADE`), the one product it is filed
    under (`filed_under`). Filed under several by its page (no product words of its own): the one
    its page shows most (`main_product`), else the first in the registry's order. Several named by
    its own words, or none: `other`."""
    category = str(finding.get("category") or OTHER_CATEGORY)
    if category in registry().active_ids():
        return category
    if str(finding.get("created_by") or "") not in CODE_MADE:
        return category
    named, from_page = _filed(finding, page_text)
    if len(named) == 1:
        return next(iter(named))
    if not named or not from_page:
        return category
    main = main_product(_words_of(page_text))
    if main is not None and main in named:
        return main
    ordered = registry().ordered(sorted(named))
    return ordered[0] if ordered else category


def no_rules_note(products: Sequence[str]) -> str | None:
    """What an answer says when a product in scope has no published GV rule yet, so it is never
    silently checked against nothing: "No GV rules for Lighting yet: compared against the
    architect only." `None` when every product has rules."""
    if not products:
        return None
    return f"No GV rules for {registry().words(products)} yet: compared against the architect only."


# ---- re-filing an "other" item before it is dropped ----------------------------------------------

#: The finding's other words read for its product: why, both values and the help it gives.
_OWN_TEXT: Final = ("why", "architect", "vendor", "question", "check_reason")


def _own_text(finding: Mapping[str, Any]) -> str:
    parts = [str(finding.get(name) or "") for name in _OWN_TEXT]
    helped = finding.get("decision_help")
    if isinstance(helped, Mapping):
        parts += [str(value) for value in helped.values() if isinstance(value, str)]
    return " ".join(parts)


def refiled(
    finding: Mapping[str, Any], scope: ProductScope, page_text: str | Callable[[], str] = ""
) -> str | None:
    """The category a finding is kept under, or `None` when it is out of scope (dropped).

    A finding of an active product is kept when that product is in scope. With every product in
    scope nothing is dropped (a code-made "other" item still gets its one product: `category_for`).
    Otherwise an "other" or unknown item is re-filed before it is dropped (orchestrator,
    2026-10-18: a pull size or a finish tag the AI filed as "other" is a cabinet item; 2026-10-19:
    an outlet or a string on a cabinet run is a cabinet item), by `filed_under`: its own name after
    the area, its whole name, its other words, its page's main product, its page's words.

    One product matched: filed there, kept only when it is in scope (a product not active yet never
    is). Several matched: kept, under the first one in scope, when one is ("counter / cabinets
    tags" stays in either pick). None: dropped (an item of no product is checked only under All)."""
    category = str(finding.get("category") or OTHER_CATEGORY)
    if category in registry().active_ids():
        return category if scope.allows(category) else None
    if scope.every:
        return category_for(finding, page_text)
    matched = filed_under(finding, page_text)
    picked = [product for product in scope.products if product in matched]
    return picked[0] if picked else None


# ---- the final filter -----------------------------------------------------------------------------


def final_blocks(
    blocks: Sequence[Mapping[str, Any]], scope: ProductScope, refused: int = 0
) -> tuple[list[dict[str, Any]], int]:
    """The answer's blocks with every out-of-scope finding left out (the backstop after the
    finding book's own refusal), and the count dropped in all (`refused` by the book, plus these).
    The trust block keeps the count (`product_scope`); the screen does not show it."""
    kept: list[dict[str, Any]] = []
    dropped = 0
    for block in blocks:
        if block.get("type") == "finding":
            finding = block.get("finding")
            if isinstance(finding, Mapping) and refiled(finding, scope) is None:
                dropped += 1
                continue
        kept.append(dict(block))
    total = refused + dropped
    note = {**scope.as_dict(), "dropped_out_of_scope": total}
    for n, block in enumerate(kept):
        trust = block.get("trust")
        # The engine wrote the note with the book's count; it is replaced only when this filter
        # dropped more (or there is none), so the stored trust block matches the streamed one.
        if (
            block.get("type") == "trust"
            and isinstance(trust, Mapping)
            and (dropped or not isinstance(trust.get("product_scope"), Mapping))
        ):
            kept[n] = {**block, "trust": {**trust, "product_scope": note}}
    return kept, total


#: A finish or material code in a row's words ("LA-002", "QZ 1"): its family is the capital letters.
_TAG_CODE: Final = re.compile(r"\b([A-Z]{2,5})\s?-?\s?\d{1,4}[A-Z]?\b")


def _family_product(text: str) -> str | None:
    """The one product the finish codes in the words belong to (the registry's `tag_families`:
    LA-002 and WD-01 are a cabinet's), or `None` when they name none or several."""
    families = {found.group(1) for found in _TAG_CODE.finditer(text)}
    owners = {
        product.id
        for product in registry().products
        for family in product.tag_families
        if family in families
    }
    return next(iter(owners)) if len(owners) == 1 else None


def scoped_gaps(
    gaps: Iterable[Mapping[str, Any]],
    scope: ProductScope,
    page_words: Callable[[int], str] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """The set's Not-compared rows that are not findings (the vendor consistency check's sheet
    gaps, the finish check's notes and codes, the simple full check's labels the architect gives
    no size for), cut to the products in scope exactly as a finding is (`refiled`): a row's own
    product when it has one (`category`), else the one its finish codes belong to, else re-filed
    from its item, then its words, then its page's words. All: every row. Returns the rows kept
    and how many were left out."""
    rows = [dict(gap) for gap in gaps if isinstance(gap, Mapping)]
    if scope.every:
        return rows, 0
    kept: list[dict[str, Any]] = []
    dropped = 0
    for gap in rows:
        what = str(gap.get("what") or "")
        item = str(gap.get("item") or what)
        category = str(gap.get("category") or "") or _family_product(what) or OTHER_CATEGORY
        page = gap.get("page")
        words: str | Callable[[], str] = ""
        if page_words is not None and isinstance(page, int):
            words = lambda page=page: page_words(page)
        finding = {"category": category, "item": item, "why": what, "question": gap.get("needs")}
        if refiled(finding, scope, words) is None:
            dropped += 1
            continue
        kept.append(gap)
    return kept, dropped


# ---- GV Review's recorded results -----------------------------------------------------------------


def _record_product(item: Mapping[str, Any], products: Mapping[str, Any]) -> str:
    product = products.get(str(item.get("id")))
    return str(product) if isinstance(product, str) and product else OTHER_CATEGORY


def scoped_document(document: object, scope: ProductScope) -> Any:
    """`review_results`' document cut to the products in scope: the countertop records only when
    countertops are in scope, each other GV check only when its rule's product is
    (`record_products`, written by the API from the rulebook), and an AI page review difference
    only when its words name an in-scope product, or every product is in scope. The page review's
    leads (rows it could not settle) are hints, not results: they stay. Unchanged for "All"."""
    if scope.every or not isinstance(document, Mapping):
        return document
    out: dict[str, Any] = json.loads(json.dumps(document))
    named = out.get("record_products")
    products: Mapping[str, Any] = named if isinstance(named, Mapping) else {}
    records = out.get("records")
    if isinstance(records, dict):
        if "countertop" not in scope.products and isinstance(records.get("countertops"), list):
            records["countertops"] = []
        if isinstance(records.get("other_checks"), list):
            records["other_checks"] = [
                item
                for item in records["other_checks"]
                if isinstance(item, Mapping) and scope.allows(_record_product(item, products))
            ]
    for sheet in out.get("ai_page_review") or []:
        if isinstance(sheet, dict) and isinstance(sheet.get("different"), list):
            sheet["different"] = [
                row
                for row in sheet["different"]
                if isinstance(row, Mapping)
                and scope.allows(_one(named_products(f"{row.get('key')} {row.get('measures')}")))
            ]
    return out


def scoped_loader(
    loader: Callable[[int | None], Mapping[str, Any]] | None, scope: ProductScope
) -> Callable[[int | None], Mapping[str, Any]] | None:
    """`review_results`' reader, its documents cut to the products in scope (`scoped_document`):
    what the lead reads, the helpers' briefs and the full check's seeded findings."""
    if loader is None or scope.every:
        return loader
    read = loader

    def load(page: int | None) -> Mapping[str, Any]:
        document = scoped_document(read(page), scope)
        return document if isinstance(document, Mapping) else {}

    return load


def _one(named: set[str]) -> str:
    return next(iter(named)) if len(named) == 1 else OTHER_CATEGORY


# ---- what a full check sends to helpers ----------------------------------------------------------


def outside_unit(pages: Sequence[int], texts: Mapping[int, str], scope: ProductScope) -> str | None:
    """Why one area (its pages) is left out of the check, or `None` to check it: left out only when
    EVERY page's printed text names an out-of-scope product (its `words`) and no page even hints at
    a product in scope (its `words` or `hints`: "sink" keeps a page for countertops). A page with
    no text, or naming no product, keeps the area (never guessed)."""
    if scope.every or not pages:
        return None
    named: set[str] = set()
    wanted = set(scope.products)
    for page in pages:
        text = texts.get(page, "")
        on_page = named_products(text) - wanted
        if not on_page or named_products(text, hints=True) & wanted:
            return None
        named |= on_page
    words = registry().words(sorted(named)).lower()
    return (
        f"only {words} drawn here (code read the page's words), and this check covers "
        f"{scope.label.lower()} only"
    )


def scaled_price(price: int, pages: int, pages_all: int) -> int:
    """The full check's fixed price for the pages left to compare (millionths of $): the whole
    price over the set's pages, times the pages in scope; never more than the price."""
    if pages_all <= 0 or pages >= pages_all:
        return price
    return max(0, price * pages // pages_all)
