"""The product registry: which products GV Review knows, read from `products.yaml` (#1547).

GV reviews more than countertops and cabinets: the client's category model has eight products
(cabinet, countertop, lighting, seating, case goods, mirrors, accessories, artwork), each with its
own checklist and two kinds of check (the vendor's dimensions against the architect's, and GV's own
standards that are not in the drawing). So a product is data, not code: one entry in
`products.yaml` beside this module, plus its skill file and its rules in the rulebook. Everything
that used to name "countertop" and "cabinet" (the chat's chips and checks, the skill, the full
check's checklist, the findings' grouping, the upload picker, the package's `product_type`) reads
the registry instead.

The file's path can be replaced with `GV_PRODUCT_REGISTRY` (tests use it to add a product without a
code change); `reset_registry()` makes the next read load it again.

**Checked on load, loudly.** A registry that does not hold together (a repeated id, an id the
database cannot store, an active product without a checklist or a skill file) raises
`RegistryError` naming the problem, rather than letting a product appear that the chat could not
review.

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

__all__ = [
    "CHECK_KINDS",
    "DEFAULT_REGISTRY",
    "OTHER_CATEGORY",
    "REGISTRY_VARIABLE",
    "ChecklistItem",
    "Product",
    "ProductRegistry",
    "RegistryError",
    "load_registry",
    "registry",
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
    }
)


class RegistryError(ValueError):
    """The product registry does not hold together; the message names the problem."""


@dataclass(frozen=True, slots=True)
class ChecklistItem:
    """One thing to compare on every page for a product: a short name and what to compare."""

    name: str
    text: str


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


def _product(entry: object, skills_folder: Path, n: int) -> Product:
    if not isinstance(entry, Mapping):
        raise RegistryError(f"product {n}: must be a mapping")
    pid = entry.get("id")
    if not isinstance(pid, str) or not _ID.match(pid):
        raise RegistryError(
            f"product {n}: id {pid!r} must be lowercase letters, digits and _ (2 to 32, "
            "starting with a letter)"
        )
    if pid == OTHER_CATEGORY:
        raise RegistryError(f"product {n}: `other` is the category of no product, not an id")
    where = f"product {pid!r}"
    unknown = sorted(set(entry) - _PRODUCT_FIELDS)
    if unknown:
        raise RegistryError(f"{where}: unknown field(s) {', '.join(unknown)}")
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
        skill_file=skills_folder / f"{pid}.md",
    )
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
    folder = data.get("skills_folder")
    if not isinstance(folder, str) or not folder.strip():
        raise RegistryError(f"{path}: needs `skills_folder`")
    skills_folder = (path.parent / folder).resolve()
    entries = data.get("products")
    if not isinstance(entries, list) or not entries:
        raise RegistryError(f"{path}: `products` must be a non-empty list")
    products = tuple(_product(entry, skills_folder, n) for n, entry in enumerate(entries, 1))
    ids = [p.id for p in products]
    repeated = sorted({pid for pid in ids if ids.count(pid) > 1})
    if repeated:
        raise RegistryError(f"{path}: product id(s) listed twice: {', '.join(repeated)}")
    if not any(p.active for p in products):
        raise RegistryError(f"{path}: at least one product must be active")
    return ProductRegistry(products, path, skills_folder)


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
