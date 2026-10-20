"""The product-pack fields: what one product teaches the generic engines, as data (#1776).

A product is a folder, `vocabulary/products/<id>/`: `pack.yaml` (its registry entry and the fields
below), `skill.md` (how to review it) and, optionally, `example.json` (one made-up worked example
for the compare prompt). Adding a product never touches Python: the engines read these fields for
the products in a check's scope (`app/review/assistant/product_packs.py`).

The fields this module reads and checks (the registry's own fields, id, label, checklist, words,
hints, tag_families, run_fit, are in `product_registry`):

- `shared_words`: words this product shares with another product (a dresser's drawer is not a
  cabinet's): while this product is active they name no product alone, so an item named with one is
  filed by its drawing's own product.
- `ours`: one line, what counts as "ours" for this product (the compare prompt's millwork rule and
  the blind look's question are built from it).
- `room_words_extra`: room-context words while this product is in scope (another trade's work near
  it, never compared).
- `end_words`: where a dimension of this product starts or ends (`name`, `aliases`, `strict`,
  `centred`, `screen_words`), added to the core end words while the product is in scope.
- `part_kinds`: the parts an architect's note may ask for and a count may name (`kind`,
  `architect_words`/`architect_pattern`, `vendor_words`/`vendor_pattern`, `countable`).
- `material_words`: words that name this product's materials (the architect's "by others" notes).
- `attributes`: specification values code can read and compare (`name`, `label_words`, `values`
  and/or `pattern`, `synonyms`): the generic attribute check.
- `size_formats`: size callouts this product prints that the reader should read (`diameter`:
  `Ø24"`; `w_x_h`: `24" W x 36" H`).
- `semantic_types`: the codes this product's GV rules may name (`code`, `meaning`, `unit`).
- `impact_words`: what makes one of its differences matter more (the "biggest first" order).
- `item_words` / `item_codes`: broad words and code shapes that file a reading of the chat ledger
  under this product (default: its `words`).

**Checked on load, loudly**, like the rest of the registry: an unknown field, a pattern that does
not compile or a word in the wrong place raises `RegistryError`. Clashes BETWEEN packs (a word two
products claim, an end word or attribute named twice) are refused by `product_registry`.

Like the rest of `vocabulary/`, this imports nothing from the project.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "ATTRIBUTE_FIELDS",
    "END_WORD_FIELDS",
    "EXAMPLE_FILE",
    "PACK_FIELDS",
    "PACK_FILE",
    "PART_KIND_FIELDS",
    "SEMANTIC_UNITS",
    "SIZE_FORMATS",
    "SKILL_FILE",
    "Attribute",
    "EndWord",
    "PackError",
    "PackSemanticType",
    "PartKind",
    "attributes",
    "end_words",
    "example",
    "line",
    "part_kinds",
    "patterns",
    "phrases",
    "read_pack",
    "semantic_types",
    "size_formats",
]

#: A pack's settings file inside its folder.
PACK_FILE: Final = "pack.yaml"
#: Its skill text and its worked example.
SKILL_FILE: Final = "skill.md"
EXAMPLE_FILE: Final = "example.json"
#: The size callouts the reader can be taught (`size_formats`).
SIZE_FORMATS: Final = ("diameter", "w_x_h")
#: A pack semantic type's unit: a size in inches, or a text value (a lamp base, a colour).
SEMANTIC_UNITS: Final = ("in", "text")
#: The fields a pack adds to a registry entry.
PACK_FIELDS: Final = frozenset(
    {
        "shared_words",
        "ours",
        "room_words_extra",
        "end_words",
        "part_kinds",
        "material_words",
        "attributes",
        "size_formats",
        "semantic_types",
        "impact_words",
        "item_words",
        "item_codes",
    }
)
END_WORD_FIELDS: Final = frozenset({"name", "aliases", "strict", "centred", "screen_words"})
PART_KIND_FIELDS: Final = frozenset(
    {
        "kind",
        "architect_words",
        "architect_pattern",
        "vendor_words",
        "vendor_pattern",
        "countable",
    }
)
ATTRIBUTE_FIELDS: Final = frozenset({"name", "label_words", "values", "pattern", "synonyms"})
_SEMANTIC_FIELDS: Final = frozenset({"code", "meaning", "unit"})
_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{1,39}$")
_CODE: Final = re.compile(r"^[A-Z][A-Z0-9_]{1,31}$")


class PackError(ValueError):
    """One pack field does not hold together; `product_registry` raises it as `RegistryError`."""


@dataclass(frozen=True, slots=True)
class EndWord:
    """Where a dimension of this product starts or ends ("fixture_centre")."""

    name: str
    """The word the compare answer starts an end with (lower case, `_` between words)."""
    aliases: tuple[str, ...] = ()
    """Other words that name the same point ("centre of fixture", "cl fixture"), lower case."""
    strict: bool = False
    """A careful reader never confuses it with another strict end (two that differ are different
    points, never "unclear")."""
    centred: bool = False
    """A centre line named alone ("centre_line") may be the centre of this point."""
    screen_words: str = ""
    """How the screens say it ("the centre of the fixture"); default: the name in words."""

    @property
    def words(self) -> tuple[str, ...]:
        """Every way it is written, as plain words: its name ("fixture centre") and its aliases."""
        return tuple(dict.fromkeys((self.name.replace("_", " "), *self.aliases)))


@dataclass(frozen=True, slots=True)
class PartKind:
    """One kind of part an architect's note may ask for, and a count may name ("shelves")."""

    kind: str
    architect: str
    """The regular expression (case-insensitive) of the architect's words for it."""
    vendor: str
    """The regular expression of the words that count as the vendor naming it."""
    countable: bool = False
    """The generic count check compares how many the two drawings print ("(3) SHELVES")."""


@dataclass(frozen=True, slots=True)
class Attribute:
    """A specification value code can read and compare ("lamp_base": E26, E12, GU10)."""

    name: str
    label_words: tuple[str, ...] = ()
    """Words that must stand beside a value for it to be this attribute ("cord" for a colour);
    none: the value names the attribute by itself (E26 is a lamp base)."""
    values: tuple[str, ...] = ()
    """The values it may take, as printed (upper case)."""
    pattern: str | None = None
    """A regular expression a value may match instead ("\\d{4}K" for a colour temperature)."""
    synonyms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Other ways each value is printed ("MEDIUM BASE" is E26), upper case."""


@dataclass(frozen=True, slots=True)
class PackSemanticType:
    """A code a pack's GV rules may name (`SemanticType` = core codes + these)."""

    code: str
    meaning: str
    unit: str


# ---- small readers -------------------------------------------------------------------------------


def line(value: object, where: str, *, required: bool = True) -> str:
    """One line of text, its spaces tidied; "" when optional and not given."""
    if value is None and not required:
        return ""
    if not isinstance(value, str) or not value.strip():
        raise PackError(f"{where} must be non-empty text")
    return " ".join(value.split())


def phrases(value: object, where: str, *, upper: bool = False) -> tuple[str, ...]:
    """A list of words or phrases, each tidied to one space and lower (or upper) case, no repeats."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(w, str) and w.strip() for w in value):
        raise PackError(f"{where} must be a list of text")
    tidy = (" ".join(w.split()) for w in value)
    return tuple(dict.fromkeys(w.upper() if upper else w.lower() for w in tidy))


def patterns(value: object, where: str) -> tuple[str, ...]:
    """A list of regular expressions, each checked to compile."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(p, str) and p for p in value):
        raise PackError(f"{where} must be a list of regular expressions")
    for pattern in value:
        _compiles(pattern, where)
    return tuple(value)


def _compiles(pattern: str, where: str) -> None:
    try:
        re.compile(pattern)
    except re.error as error:
        raise PackError(f"{where}: {pattern!r} is not a regular expression ({error})") from None


def _flag(raw: Mapping[str, Any], name: str, where: str) -> bool:
    value = raw.get(name, False)
    if not isinstance(value, bool):
        raise PackError(f"{where}: `{name}` must be true or false")
    return value


def _mappings(value: object, where: str, fields: frozenset[str]) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, Mapping) for v in value):
        raise PackError(f"{where} must be a list of mappings")
    for raw in value:
        unknown = sorted(set(raw) - fields)
        if unknown:
            raise PackError(f"{where}: unknown field(s) {', '.join(unknown)}")
    return list(value)


def _words_pattern(words: tuple[str, ...]) -> str:
    """Whole words or phrases, any of them (case is the caller's flag)."""
    return r"\b(?:" + "|".join(re.escape(w).replace("\\ ", " ") for w in words) + r")\b"


# ---- the fields ----------------------------------------------------------------------------------


def end_words(value: object, where: str) -> tuple[EndWord, ...]:
    where = f"{where} end_words"
    out: list[EndWord] = []
    for raw in _mappings(value, where, END_WORD_FIELDS):
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise PackError(f"{where}: name {name!r} must be lowercase letters, digits and _")
        if any(e.name == name for e in out):
            raise PackError(f"{where}: {name!r} is listed twice")
        screen = line(raw.get("screen_words"), f"{where} {name!r} screen_words", required=False)
        out.append(
            EndWord(
                name=name,
                aliases=phrases(raw.get("aliases"), f"{where} {name!r} aliases"),
                strict=_flag(raw, "strict", f"{where} {name!r}"),
                centred=_flag(raw, "centred", f"{where} {name!r}"),
                screen_words=screen or f"the {name.replace('_', ' ')}",
            )
        )
    return tuple(out)


def _part_side(raw: Mapping[str, Any], name: str, where: str, fallback: str | None) -> str | None:
    """One side's pattern of a part kind: its `<name>_pattern`, or its `<name>_words` as whole
    words, or `fallback`."""
    words = phrases(raw.get(f"{name}_words"), f"{where} {name}_words")
    pattern = raw.get(f"{name}_pattern")
    if pattern is not None and words:
        raise PackError(f"{where}: give {name}_words or {name}_pattern, not both")
    if pattern is not None:
        if not isinstance(pattern, str) or not pattern:
            raise PackError(f"{where}: {name}_pattern must be a pattern")
        _compiles(pattern, f"{where} {name}_pattern")
        return pattern
    return _words_pattern(words) if words else fallback


def part_kinds(value: object, where: str) -> tuple[PartKind, ...]:
    where = f"{where} part_kinds"
    out: list[PartKind] = []
    for raw in _mappings(value, where, PART_KIND_FIELDS):
        kind = line(raw.get("kind"), f"{where} kind").lower()
        if any(p.kind == kind for p in out):
            raise PackError(f"{where}: {kind!r} is listed twice")

        architect = _part_side(raw, "architect", f"{where} {kind!r}", None)
        if architect is None:
            raise PackError(f"{where} {kind!r}: needs architect_words or architect_pattern")
        vendor = _part_side(raw, "vendor", f"{where} {kind!r}", architect)
        assert vendor is not None
        out.append(
            PartKind(
                kind=kind,
                architect=architect,
                vendor=vendor,
                countable=_flag(raw, "countable", f"{where} {kind!r}"),
            )
        )
    return tuple(out)


def attributes(value: object, where: str) -> tuple[Attribute, ...]:
    where = f"{where} attributes"
    out: list[Attribute] = []
    for raw in _mappings(value, where, ATTRIBUTE_FIELDS):
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise PackError(f"{where}: name {name!r} must be lowercase letters, digits and _")
        if any(a.name == name for a in out):
            raise PackError(f"{where}: {name!r} is listed twice")
        values = phrases(raw.get("values"), f"{where} {name!r} values", upper=True)
        pattern = raw.get("pattern")
        if pattern is not None:
            if not isinstance(pattern, str) or not pattern:
                raise PackError(f"{where} {name!r}: pattern must be a regular expression")
            _compiles(pattern, f"{where} {name!r} pattern")
        if not values and pattern is None:
            raise PackError(f"{where} {name!r}: needs values or a pattern")
        synonyms_raw = raw.get("synonyms") or {}
        if not isinstance(synonyms_raw, Mapping):
            raise PackError(f"{where} {name!r}: synonyms must map a value to its other words")
        synonyms: dict[str, tuple[str, ...]] = {}
        for value_name, others in synonyms_raw.items():
            canonical = " ".join(str(value_name).split()).upper()
            if values and canonical not in values:
                raise PackError(
                    f"{where} {name!r}: synonym of {canonical!r}, not one of its values"
                )
            synonyms[canonical] = phrases(others, f"{where} {name!r} synonyms", upper=True)
        out.append(
            Attribute(
                name=name,
                label_words=phrases(raw.get("label_words"), f"{where} {name!r} label_words"),
                values=values,
                pattern=pattern,
                synonyms=synonyms,
            )
        )
    return tuple(out)


def size_formats(value: object, where: str) -> tuple[str, ...]:
    given = phrases(value, f"{where} size_formats")
    wrong = [f for f in given if f not in SIZE_FORMATS]
    if wrong:
        raise PackError(
            f"{where} size_formats: {', '.join(wrong)} (known: {', '.join(SIZE_FORMATS)})"
        )
    return given


def semantic_types(value: object, where: str) -> tuple[PackSemanticType, ...]:
    where = f"{where} semantic_types"
    out: list[PackSemanticType] = []
    for raw in _mappings(value, where, _SEMANTIC_FIELDS):
        code = raw.get("code")
        if not isinstance(code, str) or not _CODE.match(code):
            raise PackError(f"{where}: code {code!r} must be capital letters, digits and _")
        if any(t.code == code for t in out):
            raise PackError(f"{where}: {code!r} is listed twice")
        unit = raw.get("unit")
        if unit not in SEMANTIC_UNITS:
            raise PackError(f"{where} {code!r}: unit must be one of {', '.join(SEMANTIC_UNITS)}")
        out.append(
            PackSemanticType(code, line(raw.get("meaning"), f"{where} {code!r} meaning"), unit)
        )
    return tuple(out)


def example(folder: Path | None, where: str) -> Mapping[str, Any] | None:
    """The pack's worked example (`example.json`: `input`, the item's made-up text, and `reply`, the
    answer in the compare form), or None when it has none."""
    if folder is None:
        return None
    path = folder / EXAMPLE_FILE
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PackError(f"{where}: {EXAMPLE_FILE} could not be read: {error}") from None
    if (
        not isinstance(data, Mapping)
        or set(data) != {"input", "reply"}
        or not isinstance(data["input"], str)
        or not data["input"].strip()
        or not isinstance(data["reply"], Mapping)
    ):
        raise PackError(
            f"{where}: {EXAMPLE_FILE} needs exactly `input` (text) and `reply` (the form)"
        )
    reply = data["reply"]
    for key in ("pairs", "other_items", "unaccounted_labels"):
        if not isinstance(reply.get(key), list):
            raise PackError(f"{where}: {EXAMPLE_FILE} reply needs a `{key}` list")
    return data


def read_pack(folder: Path) -> Mapping[str, Any]:
    """A pack folder's `pack.yaml`, read (not yet checked: `product_registry` checks it)."""
    import yaml  # type: ignore[import-untyped]  # loaded here so the vocabulary stays light

    path = folder / PACK_FILE
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PackError(f"the product pack {path} could not be read: {error}") from None
    if not isinstance(data, Mapping):
        raise PackError(f"{path}: must be a mapping (one product's registry entry)")
    return data
