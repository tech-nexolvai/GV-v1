"""The product packs' code checks: a specification value and a count, proved by code or nothing (#1776).

Until now code could only check a size: every spec, tag, note and count item of a compare answer was
Not compared ("code cannot check it"). For lighting, seating or artwork those are most of the
review. These two checks are generic: what they read comes from the packs of the products in scope
(`product_packs.in_scope()`), so a new product's specs and counts need only its pack.

**The attribute check** (`attributes` of a pack: a lamp base E26 / E12, a colour temperature 3000K,
a cord colour). For an item of the answer (a `tag` or a `note`, called the same or different), code
reads the printed text AT the answer's two boxes itself (the drawings' text layer: phrases whose
middle is inside the box), finds one attribute both sides state, one value each (a value counts
only beside one of the attribute's `label_words`, when it has some; synonyms are one value: MEDIUM
BASE is E26):

- the same value: **Right**, proved by code;
- different values: a **red candidate**; red only after the blind second look agrees the two
  callouts state the same property of the same item (`blind.PACK_KINDS`);
- anything else (no box, no printed value, two attributes, two values on one side, the box outside
  the item's drawings, a later round, room context): `None`, and the item keeps today's Not-compared
  reason. The model's own words for the values are never used.

**The count check** (`part_kinds` with `countable: true`: "(3) SHELVES", "ARMS: 4", "2 KNOBS"). For a
`layout_count` or `note` item, the same way: one countable part kind both boxes name with one count
each. Equal: Right; different: a red candidate after the blind look.

Kitchen packs give no attributes and no countable parts, so a countertop and cabinet check is
unchanged. Pure code: no model, no I/O beyond the set pack.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from app.review.assistant.product_packs import in_scope
from vocabulary.product_packs import Attribute
from vocabulary.product_registry import registry

if TYPE_CHECKING:
    from app.review.assistant.simple_check.verify import Item, Pack, Verified

__all__ = [
    "ATTRIBUTE_KINDS",
    "COUNT_KINDS",
    "attribute_values",
    "check",
    "counts",
]

#: The answer's item kinds each check looks at.
ATTRIBUTE_KINDS: Final = frozenset({"tag", "note"})
COUNT_KINDS: Final = frozenset({"layout_count", "note"})
#: How far outside a box a phrase's middle may sit and still be read as in it (points).
_PAD_PT: Final = 6.0
#: A count is a small whole number.
_COUNT: Final = r"\(?\s*(\d{1,2})\s*\)?"


@dataclass(frozen=True, slots=True)
class _Read:
    """What code read at one box: the phrases' text, joined."""

    text: str
    found: bool


def _box(raw: object) -> tuple[float, float, float, float] | None:
    from app.review.assistant.simple_check.verify import _box as box

    return box(raw)


def _inside(phrase_box: object, box: tuple[float, float, float, float]) -> bool:
    from app.review.assistant.simple_check.verify import _centre_in

    return _centre_in(_box(phrase_box), box, _PAD_PT)


def _read(pack: Pack, file: str, page: int, box: tuple[float, float, float, float]) -> _Read:
    words = [
        str(p.get("text") or "")
        for p in pack.phrases.get((file, page), ())
        if _inside(p.get("box"), box)
    ]
    text = " ".join(" ".join(words).split())
    return _Read(text, bool(text))


# ---- values ---------------------------------------------------------------------------------------


def _phrase(word: str) -> str:
    return r"(?<![A-Z0-9])" + re.escape(word).replace("\\ ", r"\s+") + r"(?![A-Z0-9])"


def attribute_values(attribute: Attribute, text: str) -> set[str]:
    """The values of `attribute` a text states, as the pack names them (synonyms folded): none when
    the attribute has label words and none of them is in the text."""
    upper = " ".join(text.upper().split())
    if attribute.label_words and not any(
        re.search(_phrase(w.upper()), upper) for w in attribute.label_words
    ):
        return set()
    found: set[str] = set()
    for value in attribute.values:
        names = (value, *attribute.synonyms.get(value, ()))
        if any(re.search(_phrase(name), upper) for name in names):
            found.add(value)
    if attribute.pattern is not None:
        for hit in re.finditer(attribute.pattern, upper, re.IGNORECASE):
            found.add(" ".join(hit.group(0).upper().split()))
    # A value whose words stand inside another value found ("BLACK" in "MATTE BLACK") is that one.
    return {v for v in found if not any(v != o and re.search(_phrase(v), o) for o in found)}


def counts(pattern: str, text: str) -> set[int]:
    """The counts a text prints for a part named by `pattern`: "(3) SHELVES", "3 SHELVES",
    "SHELVES (3)", "SHELVES: 3", "QTY 3 SHELVES"."""
    part = f"(?:{pattern})"
    out: set[int] = set()
    for form in (
        rf"{_COUNT}\s*(?:no\.?\s*)?{part}",
        rf"{part}\s*[:=\-]?\s*{_COUNT}(?!\s*[\"'/.]|\s*\d)",
        rf"\bQTY\.?\s*:?\s*{_COUNT}\s*{part}",
    ):
        for hit in re.finditer(form, text, re.IGNORECASE):
            out.add(int(hit.group(1)))
    return out


# ---- the check ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Found:
    kind: str
    """`attribute` or `count`."""
    name: str
    vendor: str
    architect: str


def _attribute(vendor: str, architect: str, products: Sequence[str]) -> _Found | None:
    known = registry()
    hits: list[_Found] = []
    for pid in products:
        product = known.get(pid)
        for attribute in product.attributes if product is not None else ():
            v, a = attribute_values(attribute, vendor), attribute_values(attribute, architect)
            if len(v) == 1 and len(a) == 1:
                hits.append(_Found("attribute", attribute.name, next(iter(v)), next(iter(a))))
            elif v or a:
                return None  # the attribute is there, but not one value on each side
    return hits[0] if len(hits) == 1 else None


def _count(vendor: str, architect: str, products: Sequence[str]) -> _Found | None:
    known = registry()
    hits: list[_Found] = []
    seen: set[str] = set()
    for pid in products:
        product = known.get(pid)
        for part in product.part_kinds if product is not None else ():
            if not part.countable or part.kind in seen:
                continue
            seen.add(part.kind)
            v, a = counts(part.vendor, vendor), counts(part.architect, architect)
            if len(v) == 1 and len(a) == 1:
                hits.append(_Found("count", part.kind, str(next(iter(v))), str(next(iter(a)))))
            elif v or a:
                return None
    return hits[0] if len(hits) == 1 else None


def check(
    entry: Mapping[str, Any],
    kind: str,
    verdict: str,
    key: str,
    base: Mapping[str, Any],
    item: Item,
    pack: Pack,
    a_file: str,
) -> Verified | None:
    """The attribute or count check of one other item of the answer (see the module): a Right, a
    red candidate, or None (not this check's: the item keeps its usual reason)."""
    from app.review.assistant.full_check_advocate import Check
    from app.review.assistant.simple_check.verify import (
        _ROOM_WORDS,
        Verified,
        _area_check,
        _round_check,
        _sentence,
    )

    if verdict not in ("same", "different") or kind not in ATTRIBUTE_KINDS | COUNT_KINDS:
        return None
    products = in_scope()
    if not _has_checks(products):
        return None
    a_page = entry.get("architect_page")
    v_box, a_box = _box(entry.get("vendor_box")), _box(entry.get("architect_box"))
    if not isinstance(a_page, int) or v_box is None or a_box is None:
        return None
    if _ROOM_WORDS.search(str(entry.get("what") or "")):
        return None
    if item.vendor.box is not None and not _inside(list(v_box), item.vendor.box):
        return None
    vendor = _read(pack, item.vendor.file, item.vendor.page, v_box)
    architect = _read(pack, a_file, a_page, a_box)
    if not vendor.found or not architect.found:
        return None
    found = (
        _attribute(vendor.text, architect.text, products) if kind in ATTRIBUTE_KINDS else None
    ) or (_count(vendor.text, architect.text, products) if kind in COUNT_KINDS else None)
    if found is None:
        return None
    area = _area_check(item, pack, a_file, a_page, a_box)
    if area.outcome != "pass":
        return None
    what = found.name.replace("_", " ")
    checks = (
        area,
        Check("read", "pass", f'code read "{vendor.text[:60]}" and "{architect.text[:60]}"'),
        Check(found.kind, "pass", f"{what}: vendor {found.vendor}, architect {found.architect}"),
    )
    finding = {
        **base,
        "vendor": found.vendor,
        "architect": found.architect,
        "paired_by": "code",
        "text_proof": {
            "kind": found.kind,
            "code": found.name,
            "architect": [{"page": a_page, "text": architect.text[:200]}],
            "vendor": [{"page": item.vendor.page, "text": vendor.text[:200]}],
            "pairing": "the compare answer's two boxes; code read the text at each",
        },
    }
    if found.vendor == found.architect:
        if verdict != "same":
            # The AI said the callouts differ: one attribute code reads equal does not prove the
            # rest of the callout is (#1776 review). The item keeps its usual reason.
            return None
        why = (
            f"The vendor's {what} is the architect's ({found.vendor}): code read both callouts "
            "at their boxes"
        )
        return Verified(
            key,
            kind,
            "right",
            checks=checks,
            finding={**finding, "status": "same", "why": _sentence(why)},
        )
    round_check = _round_check(pack, item)
    if round_check.outcome != "pass":
        return None
    why = (
        f"The vendor states {found.vendor} for the {what}; the architect states "
        f"{found.architect}. Code read both callouts at their boxes"
    )
    return Verified(
        key,
        kind,
        "candidate",
        checks=(*checks, round_check),
        finding={**finding, "status": "different", "why": _sentence(why)},
        blind=found.kind,
    )


def _has_checks(products: Iterable[str]) -> bool:
    known = registry()
    return any(
        (p := known.get(pid)) is not None
        and (p.attributes or any(k.countable for k in p.part_kinds))
        for pid in products
    )
