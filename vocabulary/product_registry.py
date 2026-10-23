"""The product registry: which products GV Review knows, read from `products.yaml` (#1547).

GV reviews more than countertops and cabinets: the client's category model has eight products
(cabinet, countertop, lighting, seating, case goods, mirrors, accessories, artwork), each with its
own checklist and two kinds of check (the vendor's dimensions against the architect's, and GV's own
standards that are not in the drawing). So a product is data, not code: one **product pack**,
`products/<id>/` beside this module (`pack.yaml`, `skill.md`, optionally `example.json`; #1776),
listed in `products.yaml`, plus its rules in the rulebook. Everything
that used to name "countertop" and "cabinet" (the chat's chips and checks, the skill, the full
check's checklist, the findings' grouping, the upload picker, the package's `product_type`) reads
the registry instead.

The file's path can be replaced with `GV_PRODUCT_REGISTRY` (tests use it to add a product without a
code change); `reset_registry()` makes the next read load it again.

**Checked on load, loudly.** A registry that does not hold together (a repeated id, an id the
database cannot store, an active product without a checklist or a skill file, a word or end word or
attribute two packs both claim) raises `RegistryError` naming the problem, rather than letting a
product appear that the chat could not review.

`products.yaml` lists the packs in order. An entry may also be the whole product written inline (a
mapping, with its skill file in `skills_folder` or its own `folder`): test registries use that, and
`registry_data()` gives every product of a registry in that form.

Like the rest of `vocabulary/`, this imports nothing from the project.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any, Final

from vocabulary.product_packs import (
    PACK_FIELDS,
    PACK_FILE,
    SKILL_FILE,
    Attribute,
    EndWord,
    LookInViews,
    PackError,
    PackSemanticType,
    PartKind,
    attributes,
    end_words,
    example,
    line,
    look_in_views,
    part_kinds,
    patterns,
    phrases,
    read_pack,
    semantic_types,
    size_formats,
)

__all__ = [
    "CHECK_KINDS",
    "DEFAULT_REGISTRY",
    "OTHER_CATEGORY",
    "REGISTRY_VARIABLE",
    "RUN_FIT_DISTRIBUTIONS",
    "RUN_FIT_LOOKS",
    "RUN_FIT_PLACES",
    "RUN_FIT_ROLES",
    "ChecklistItem",
    "Product",
    "ProductRegistry",
    "RegistryError",
    "RunFit",
    "RunPartKind",
    "load_registry",
    "registry",
    "registry_data",
    "reset_registry",
]

#: The registry kept in the repository.
DEFAULT_REGISTRY: Final = Path(__file__).resolve().parent / "products.yaml"
#: Replaces the registry's path (a test registry, or a deployment's own).
REGISTRY_VARIABLE: Final = "GV_PRODUCT_REGISTRY"
#: The two kinds of check a product can have (the client's category model, 2026-07-24).
CHECK_KINDS: Final = ("dimension_match", "global_rules")
#: The finding category for an item that belongs to no product.
OTHER_CATEGORY: Final = "other"

#: Fits `packages.product_type` (32 characters) and reads well in a URL and a file name.
_ID: Final = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_NAME: Final = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_PRODUCT_FIELDS: Final = frozenset(
    {
        "id",
        "label",
        "plural",
        "description",
        "active",
        "draft",
        "check_kinds",
        "checklist",
        "global_rule_hints",
        "category_label",
        "words",
        "hints",
        "tag_families",
        "run_fit",
        *PACK_FIELDS,
    }
)
#: An inline entry (a test registry) may name the folder its skill and example files are in.
_INLINE_FIELDS: Final = frozenset({*_PRODUCT_FIELDS, "folder"})
#: How a run's flex parts share the site difference (`run_fit.distribution`). Only `equally` so far:
#: a new way is a new word here and its arithmetic in `app/review/assistant/run_fit.py`.
RUN_FIT_DISTRIBUTIONS: Final = ("equally",)
#: A run part's role: `fixed` keeps its design size on site, `flex` absorbs the site difference.
RUN_FIT_ROLES: Final = ("fixed", "flex")
#: The drawn looks code can tell inside a part's column (`run_fit`): `plain` (no lines inside: a
#: strip), `stacked` (two or more full-width lines inside and no diagonals: stacked fronts) and
#: `crossed` (diagonals both ways: door swings or a cross).
RUN_FIT_LOOKS: Final = ("plain", "stacked", "crossed")
#: Where a part kind may be told by its look alone: at either `ends` of the run, or `any` place.
RUN_FIT_PLACES: Final = ("ends", "any")
_RUN_FIT_FIELDS: Final = frozenset({"source", "distribution", "parts", "height_stack"})
_RUN_PART_FIELDS: Final = frozenset({"kind", "role", "words", "drawn", "place"})
#: A tag family: the letters before a finish or material code's number (`QZ` in `QZ-12`).
_FAMILY: Final = re.compile(r"^[A-Z]{1,4}$")


class RegistryError(ValueError):
    """The product registry does not hold together; the message names the problem."""


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One thing to compare on every page for a product: a short name and what to compare."""

    name: str
    text: str


@dataclass(frozen=True, slots=True)
class RunPartKind:
    """One kind of part in a product's run (`run_fit`): a filler, a drawer bank, a door cabinet."""

    kind: str
    """Its name in GV's words, as the findings say it ("filler", "door cabinet")."""
    role: str
    """`fixed` (keeps its design size on site) or `flex` (absorbs the site difference)."""
    words: tuple[str, ...] = ()
    """Words that, printed with a part's own dimension, say it is this kind (lower case)."""
    drawn: str | None = None
    """The look code can tell it by inside its column (`RUN_FIT_LOOKS`), or `None`: words only."""
    place: str = "any"
    """`ends`: told by its look only at either end of the run; `any`: anywhere."""


@dataclass(frozen=True, slots=True)
class RunFit:
    """GV's rule for fitting a product's run to the site (#1763): which parts keep their design
    size and how the others share the difference. Data, so a new product needs no code."""

    source: str
    """Where the rule comes from (the rulebook cites it)."""
    distribution: str
    """How the flex parts share the site difference (`RUN_FIT_DISTRIBUTIONS`)."""
    parts: tuple[RunPartKind, ...]
    height_stack: bool = False
    """The run's height stack (its heights from the floor up) keeps its design sizes and adds up
    to its printed total."""

    def kind(self, name: str) -> RunPartKind | None:
        return next((part for part in self.parts if part.kind == name), None)


@dataclass(frozen=True, slots=True)
class Product:
    """One product GV Review knows."""

    id: str
    label: str
    plural: str
    description: str
    active: bool
    draft: bool
    check_kinds: tuple[str, ...]
    checklist: tuple[ChecklistItem, ...]
    global_rule_hints: tuple[str, ...]
    category_label: str
    skill_file: Path
    """`<skills_folder>/<id>.md`: how to review this product. May be missing for an inactive one."""
    words: tuple[str, ...] = ()
    """Words that, printed on a drawing or in an item's name, can only mean this product (lower
    case, as typed: "countertop", "toe kick"). Code reads them to tell which products a page holds
    and which product a code-made item belongs to (`product_scope`); none: never guessed."""
    hints: tuple[str, ...] = ()
    """Words that only suggest this product may be drawn ("sink", "stone"): a page printing one is
    never left out of a check of this product (`product_scope.outside_unit`); never a category."""
    tag_families: tuple[str, ...] = ()
    """The tag families that name this product's finishes and materials (`QZ` for `QZ-12`): code
    compares the architect's and the vendor's codes of these families (`architect_items`) and
    files what it finds under this product. None: code compares no tags for it."""
    run_fit: RunFit | None = None
    """GV's rule for fitting this product's runs to the site (`run_fit`), or `None`: no such rule
    until GV gives one."""
    # -- the product pack's fields (#1776; `product_packs` says what each one is for) --
    shared_words: tuple[str, ...] = ()
    ours: str = ""
    room_words_extra: tuple[str, ...] = ()
    end_words: tuple[EndWord, ...] = ()
    part_kinds: tuple[PartKind, ...] = ()
    material_words: tuple[str, ...] = ()
    attributes: tuple[Attribute, ...] = ()
    size_formats: tuple[str, ...] = ()
    semantic_types: tuple[PackSemanticType, ...] = ()
    impact_words: tuple[str, ...] = ()
    item_words: tuple[str, ...] = ()
    item_codes: tuple[str, ...] = ()
    look_in_views: tuple[LookInViews, ...] = ()
    example: Mapping[str, Any] | None = None
    """`example.json`: one made-up worked example (`input`, `reply`) for the compare prompt."""
    folder: Path | None = None
    """The pack's folder; None for a product written inline in a registry."""

    @property
    def has_skill(self) -> bool:
        return self.skill_file.is_file()


@dataclass(frozen=True, slots=True)
class ProductRegistry:
    """Every product, in the registry's order, and where it was read from."""

    products: tuple[Product, ...]
    source: Path
    skills_folder: Path

    def get(self, product_id: str) -> Product | None:
        return next((p for p in self.products if p.id == product_id), None)

    def ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.products)

    def active(self) -> tuple[Product, ...]:
        return tuple(p for p in self.products if p.active)

    def active_ids(self) -> tuple[str, ...]:
        return tuple(p.id for p in self.products if p.active)

    def categories(self) -> tuple[str, ...]:
        """A finding's possible categories: every ACTIVE product id (only those can be in focus),
        then `other`."""
        return (*self.active_ids(), OTHER_CATEGORY)

    def ordered(self, product_ids: Iterable[str]) -> list[str]:
        """These ids in the registry's order (unknown ones after, as given), without repeats."""
        wanted = list(dict.fromkeys(product_ids))
        known = [pid for pid in self.ids() if pid in wanted]
        return known + [pid for pid in wanted if pid not in known]

    def words(self, product_ids: Sequence[str], *, empty: str = "All products") -> str:
        """["countertop", "cabinet"] as "Countertops and Cabinets" (an unknown id as itself)."""
        names = [
            (product.plural if (product := self.get(pid)) is not None else pid)
            for pid in self.ordered(product_ids)
        ]
        if not names:
            return empty
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"

    def category_label(self, category: str | None) -> str:
        """How a finding category is shown: the product's group name, else "Other"."""
        product = self.get(category or "")
        return product.category_label if product is not None else "Other"


def _text(entry: Mapping[str, Any], name: str, where: str, *, default: str | None = None) -> str:
    value = entry.get(name, default)
    if not isinstance(value, str) or not value.strip():
        raise RegistryError(f"{where}: `{name}` must be non-empty text")
    return " ".join(value.split())


def _flag(entry: Mapping[str, Any], name: str, where: str, *, default: bool | None = None) -> bool:
    value = entry.get(name, default)
    if not isinstance(value, bool):
        raise RegistryError(f"{where}: `{name}` must be true or false")
    return value


def _run_fit(given: object, where: str) -> RunFit | None:
    """A product's `run_fit` entry, checked (see `products.yaml`)."""
    if given is None:
        return None
    where = f"{where} run_fit"
    if not isinstance(given, Mapping):
        raise RegistryError(f"{where}: must be a mapping")
    unknown = sorted(set(given) - _RUN_FIT_FIELDS)
    if unknown:
        raise RegistryError(f"{where}: unknown field(s) {', '.join(unknown)}")
    distribution = given.get("distribution", "equally")
    if distribution not in RUN_FIT_DISTRIBUTIONS:
        raise RegistryError(
            f"{where}: `distribution` must be one of {', '.join(RUN_FIT_DISTRIBUTIONS)}"
        )
    height = given.get("height_stack", False)
    if not isinstance(height, bool):
        raise RegistryError(f"{where}: `height_stack` must be true or false")
    raw_parts = given.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise RegistryError(f"{where}: `parts` must be a non-empty list")
    parts: list[RunPartKind] = []
    for raw in raw_parts:
        if not isinstance(raw, Mapping):
            raise RegistryError(f"{where}: each part must be a mapping")
        extra = sorted(set(raw) - _RUN_PART_FIELDS)
        if extra:
            raise RegistryError(f"{where}: unknown part field(s) {', '.join(extra)}")
        kind = _text(raw, "kind", where).lower()
        if any(part.kind == kind for part in parts):
            raise RegistryError(f"{where}: part kind {kind!r} is listed twice")
        role = raw.get("role")
        if role not in RUN_FIT_ROLES:
            raise RegistryError(f"{where}: part {kind!r} `role` must be fixed or flex")
        words = raw.get("words", [])
        if not isinstance(words, list) or not all(isinstance(w, str) and w.strip() for w in words):
            raise RegistryError(f"{where}: part {kind!r} `words` must be a list of text")
        drawn = raw.get("drawn")
        if drawn is not None and drawn not in RUN_FIT_LOOKS:
            raise RegistryError(
                f"{where}: part {kind!r} `drawn` must be one of {', '.join(RUN_FIT_LOOKS)}"
            )
        place = raw.get("place", "any")
        if place not in RUN_FIT_PLACES:
            raise RegistryError(f"{where}: part {kind!r} `place` must be ends or any")
        if drawn == "plain" and place != "ends":
            # A plain rectangle mid-run may be a door drawn without its swing: never told by look.
            raise RegistryError(f"{where}: part {kind!r}: a `plain` look is told only at the ends")
        if not words and drawn is None:
            raise RegistryError(f"{where}: part {kind!r} needs `words` or `drawn` to be told")
        parts.append(
            RunPartKind(
                kind=kind,
                role=str(role),
                words=tuple(dict.fromkeys(" ".join(w.lower().split()) for w in words)),
                drawn=drawn,
                place=str(place),
            )
        )
    if not any(part.role == "flex" for part in parts):
        raise RegistryError(f"{where}: at least one part kind must be flex")
    looks = [part.drawn for part in parts if part.drawn is not None]
    if len(looks) != len(set(looks)):
        raise RegistryError(f"{where}: two part kinds share one `drawn` look")
    return RunFit(
        source=_text(given, "source", where),
        distribution=str(distribution),
        parts=tuple(parts),
        height_stack=height,
    )


def _product(
    entry: object, skills_folder: Path | None, n: int, folder: Path | None = None
) -> Product:
    if not isinstance(entry, Mapping):
        raise RegistryError(f"product {n}: must be a mapping")
    try:
        return _checked(entry, skills_folder, n, folder)
    except PackError as error:
        raise RegistryError(str(error)) from None


def _checked(
    entry: Mapping[str, Any], skills_folder: Path | None, n: int, folder: Path | None
) -> Product:
    pid = entry.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise RegistryError(
            f"product {n}: id {pid!r} must be lowercase letters, digits and _ (2 to 32, "
            "starting with a letter)"
        )
    if pid == OTHER_CATEGORY:
        raise RegistryError(f"product {n}: `other` is the category of no product, not an id")
    where = f"product {pid!r}"
    allowed = _PRODUCT_FIELDS if folder is not None else _INLINE_FIELDS
    unknown = sorted(set(entry) - allowed)
    if unknown:
        raise RegistryError(f"{where}: unknown field(s) {', '.join(unknown)}")
    if folder is None and entry.get("folder") is not None:
        given = entry["folder"]
        if not isinstance(given, str) or not given.strip():
            raise RegistryError(f"{where}: `folder` must be a path")
        folder = Path(given).expanduser().resolve()
    if folder is not None:
        skill_file = folder / SKILL_FILE
    elif skills_folder is not None:
        skill_file = skills_folder / f"{pid}.md"
    else:
        raise RegistryError(f"{where}: written inline, it needs `skills_folder` or its `folder`")
    kinds = entry.get("check_kinds")
    if (
        not isinstance(kinds, list)
        or not kinds
        or any(kind not in CHECK_KINDS for kind in kinds)
        or len(set(kinds)) != len(kinds)
    ):
        raise RegistryError(f"{where}: `check_kinds` must list some of {', '.join(CHECK_KINDS)}")
    raw_items = entry.get("checklist")
    if not isinstance(raw_items, list):
        raise RegistryError(f"{where}: `checklist` must be a list")
    items: list[ChecklistItem] = []
    for item in raw_items:
        if not isinstance(item, Mapping) or set(item) != {"name", "text"}:
            raise RegistryError(f"{where}: each checklist item needs exactly `name` and `text`")
        name = item["name"]
        if not isinstance(name, str) or not _NAME.match(name):
            raise RegistryError(f"{where}: checklist name {name!r} must be lowercase and _")
        if any(existing.name == name for existing in items):
            raise RegistryError(f"{where}: checklist item {name!r} is listed twice")
        items.append(ChecklistItem(name, _text(item, "text", f"{where} checklist {name!r}")))
    hints = entry.get("global_rule_hints", [])
    if not isinstance(hints, list) or not all(isinstance(h, str) and h.strip() for h in hints):
        raise RegistryError(f"{where}: `global_rule_hints` must be a list of text")
    words, maybe = entry.get("words", []), entry.get("hints", [])
    for name, given in (("words", words), ("hints", maybe)):
        if not isinstance(given, list) or not all(isinstance(w, str) and w.strip() for w in given):
            raise RegistryError(f"{where}: `{name}` must be a list of text")
    families = entry.get("tag_families", [])
    if not isinstance(families, list) or not all(
        isinstance(f, str) and _FAMILY.match(f) for f in families
    ):
        raise RegistryError(f"{where}: `tag_families` must list 1 to 4 capital letters each")
    plural = _text(entry, "plural", where)
    product = Product(
        id=pid,
        label=_text(entry, "label", where),
        plural=plural,
        description=_text(entry, "description", where),
        active=_flag(entry, "active", where),
        draft=_flag(entry, "draft", where, default=False),
        check_kinds=tuple(kinds),
        checklist=tuple(items),
        global_rule_hints=tuple(" ".join(h.split()) for h in hints),
        category_label=_text(entry, "category_label", where, default=plural),
        skill_file=skill_file,
        words=tuple(dict.fromkeys(" ".join(w.lower().split()) for w in words)),
        hints=tuple(dict.fromkeys(" ".join(w.lower().split()) for w in maybe)),
        tag_families=tuple(dict.fromkeys(families)),
        run_fit=_run_fit(entry.get("run_fit"), where),
        shared_words=phrases(entry.get("shared_words"), f"{where} shared_words"),
        ours=line(entry.get("ours"), f"{where} ours", required=False),
        room_words_extra=phrases(entry.get("room_words_extra"), f"{where} room_words_extra"),
        end_words=end_words(entry.get("end_words"), where),
        part_kinds=part_kinds(entry.get("part_kinds"), where),
        material_words=phrases(entry.get("material_words"), f"{where} material_words"),
        attributes=attributes(entry.get("attributes"), where),
        size_formats=size_formats(entry.get("size_formats"), where),
        semantic_types=semantic_types(entry.get("semantic_types"), where),
        impact_words=phrases(entry.get("impact_words"), f"{where} impact_words"),
        item_words=phrases(entry.get("item_words"), f"{where} item_words"),
        item_codes=patterns(entry.get("item_codes"), f"{where} item_codes"),
        look_in_views=look_in_views(entry.get("look_in_views"), where),
        example=example(folder, where),
        folder=folder,
    )
    own = set(product.words)
    for name, given in (
        ("shared_words", product.shared_words),
        ("room_words_extra", product.room_words_extra),
    ):
        mine = sorted(own & set(given))
        if mine:
            raise RegistryError(f"{where}: {', '.join(mine)} in `{name}` are its own words")
    if product.active:
        if not product.checklist:
            raise RegistryError(f"{where}: an active product needs a checklist")
        if not product.has_skill or not product.skill_file.read_text(encoding="utf-8").strip():
            raise RegistryError(f"{where}: an active product needs its skill file {pid}.md")
    return product


def load_registry(path: Path) -> ProductRegistry:
    """Read and check one registry file. `RegistryError` when it does not hold together."""
    import yaml  # type: ignore[import-untyped]  # loaded here so the vocabulary stays light

    path = Path(path).expanduser().resolve()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise RegistryError(f"the product registry {path} could not be read: {error}") from None
    if not isinstance(data, Mapping) or data.get("version") != 1:
        raise RegistryError(f"{path}: needs `version: 1` and a `products` list")
    skills_folder = _folder(data, "skills_folder", path)
    packs_folder = _folder(data, "packs_folder", path)
    entries = data.get("products")
    if not isinstance(entries, list) or not entries:
        raise RegistryError(f"{path}: `products` must be a non-empty list")
    products = tuple(
        _entry(entry, skills_folder, packs_folder, n, path) for n, entry in enumerate(entries, 1)
    )
    ids = [p.id for p in products]
    repeated = sorted({pid for pid in ids if ids.count(pid) > 1})
    if repeated:
        raise RegistryError(f"{path}: product id(s) listed twice: {', '.join(repeated)}")
    _no_clashes(products, path)
    if not any(p.active for p in products):
        raise RegistryError(f"{path}: at least one product must be active")
    return ProductRegistry(products, path, skills_folder or packs_folder or path.parent)


def _folder(data: Mapping[str, Any], name: str, path: Path) -> Path | None:
    given = data.get(name)
    if given is None:
        return None
    if not isinstance(given, str) or not given.strip():
        raise RegistryError(f"{path}: `{name}` must be a folder")
    return (path.parent / given).resolve()


def _entry(
    entry: object,
    skills_folder: Path | None,
    packs_folder: Path | None,
    n: int,
    path: Path,
) -> Product:
    """One `products` entry: a pack id (its folder in `packs_folder`) or a product written inline."""
    if not isinstance(entry, str):
        return _product(entry, skills_folder, n)
    if packs_folder is None:
        raise RegistryError(f"{path}: pack {entry!r} is listed but there is no `packs_folder`")
    if entry.startswith("_") or not _ID.match(entry):
        raise RegistryError(f"{path}: {entry!r} is not a pack id")
    folder = packs_folder / entry
    if not (folder / PACK_FILE).is_file():
        raise RegistryError(f"{path}: the pack {entry!r} has no {folder / PACK_FILE}")
    try:
        data = read_pack(folder)
    except PackError as error:
        raise RegistryError(str(error)) from None
    if data.get("id") != entry:
        raise RegistryError(f"{folder / PACK_FILE}: its id must be its folder's name, {entry!r}")
    return _product(data, None, n, folder)


def _no_clashes(products: Sequence[Product], path: Path) -> None:
    """Refuse two packs that claim the same thing: a word, a tag family, an end word (or one of
    its aliases), an attribute or a semantic type. Each must mean one product, or the engines would
    file, match or check by the wrong one."""

    def twice(what: str, owned: list[tuple[str, str]]) -> None:
        owners: dict[str, set[str]] = {}
        for value, pid in owned:
            owners.setdefault(value, set()).add(pid)
        clash = sorted(
            f"{value} ({', '.join(sorted(pids))})"
            for value, pids in owners.items()
            if len(pids) > 1
        )
        if clash:
            raise RegistryError(f"{path}: {what} given to two products: {'; '.join(clash)}")

    twice("word(s)", [(w, p.id) for p in products for w in p.words])
    families = [f for p in products for f in p.tag_families]
    shared = sorted({f for f in families if families.count(f) > 1})
    if shared:
        raise RegistryError(f"{path}: tag famil(ies) given to two products: {', '.join(shared)}")
    twice(
        "end word(s)",
        [(word, p.id) for p in products for end in p.end_words for word in end.words],
    )
    twice("attribute(s)", [(a.name, p.id) for p in products for a in p.attributes])
    twice("semantic type(s)", [(t.code, p.id) for p in products for t in p.semantic_types])
    for product in products:
        for word in product.shared_words:
            if not any(word in other.words or word in other.hints for other in products):
                raise RegistryError(
                    f"{path}: product {product.id!r} shares {word!r}, which no other product uses"
                )


def registry_data(path: Path | None = None) -> dict[str, Any]:
    """A registry's file as data with every product written inline (each pack's fields plus its
    `folder`), so a test can change or add a product and write the result anywhere."""
    import yaml

    path = Path(path or _registry_path()).expanduser().resolve()
    data = dict(yaml.safe_load(path.read_text(encoding="utf-8")))
    skills = _folder(data, "skills_folder", path)
    packs = _folder(data, "packs_folder", path)
    entries: list[Any] = []
    for entry in data.get("products") or []:
        if isinstance(entry, str) and packs is not None:
            folder = packs / entry
            entries.append({**dict(read_pack(folder)), "folder": str(folder)})
        else:
            entries.append(entry)
    out: dict[str, Any] = {"version": data.get("version"), "products": entries}
    if skills is not None:
        out["skills_folder"] = str(skills)
    return out


def _registry_path() -> Path:
    return Path(os.environ.get(REGISTRY_VARIABLE) or DEFAULT_REGISTRY)


@cache
def _cached(path: Path) -> ProductRegistry:
    return load_registry(path)


def registry() -> ProductRegistry:
    """The registry in force: `GV_PRODUCT_REGISTRY`'s file, else `products.yaml`. Read once per
    path; `reset_registry()` reads it again."""
    return _cached(_registry_path())


def reset_registry() -> None:
    """Forget the registry read so far (a test that swaps the file, or after editing it)."""
    _cached.cache_clear()
    _reset_product_types()


def _reset_product_types() -> None:
    """Forget `ProductType` values made for registry ids that are not its own members, so a product
    a test registry added does not outlive that registry."""
    from vocabulary.semantic_types import ProductType

    members = {member.value for member in ProductType.__members__.values()}
    for value in [v for v in ProductType._value2member_map_ if v not in members]:
        del ProductType._value2member_map_[value]
