"""The chat agent's skill folder: GV's review know-how and the rules for one drawing set (#1465).

claude.ai gives Claude "skills": a read-only folder with a `SKILL.md` that Claude reads before a kind
of task. The chat agent gets one, mounted read-only at `/mnt/skills/gv-review` in a chat's workspace,
and `build_skill_folder` writes it:

- `SKILL.md`, copied byte for byte from `skills/gv-review/SKILL.md` beside this module: how GV
  reviews a vendor's shop drawing against the architect's. Generic and the same for every product:
  no client value.
- `products/<id>.md` for each product in focus (#1547): the product's own skill file from the
  product registry (`vocabulary/products.yaml`), followed by its checklist and the kinds of GV
  standard it has, written from the registry. The products in focus are the reviewer's pick, else
  the drawing set's own product, else every active product.
- `rules.md`, exported for this drawing set: each published check that runs on it, in plain words,
  and every setting those checks read, in inches, with where its value comes from (the rulebook's
  default, GV's company standard, the project's value or sink card, or a value typed for this
  drawing set). The values are `settings_in_force`'s, the ones a check run reads. A setting nobody
  gave is written as missing, never as zero or a guess.
- GV's own files kept on this machine (`GV_ASSISTANT_SKILLS_LOCAL`, by default
  `~/gv-local/skills/gv-review`: GV's checklists), copied in when that folder exists. Client
  material stays on this machine; the repository holds none.

**Read-only.** Plain reads through the same helpers the settings screens use; nothing is written to
the database, and nothing here can change a check, a result or the review.

**The same records give the same bytes:** checks and settings in a fixed order, no time of writing.
The folder is built beside `dest` and then moved into place, so a chat never sees half a folder; an
existing `dest` is replaced only when it is empty or a skill folder this function wrote.

Source: issue #1465. Verification: `tests/review/assistant/test_skills_export.py`.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.package import Package, PackageRevision, package_product
from app.models.project_sinks import ProjectSink
from app.review.assistant.internal_refs import without_internal_refs
from app.review.settings_in_force import SINK_SETTINGS, InForce, settings_in_force
from app.verdicts.rulebook import snapshot_store
from rules.derivations import Derivation
from rules.schema import (
    TOLERANCE_UNCONFIRMED,
    Applicability,
    Cardinality,
    InputSelector,
    OperationRef,
    Quantity,
    Rule,
    Tolerance,
)
from units.imperial import format_inches
from units.measurement import Unit
from vocabulary.product_registry import Product, registry
from vocabulary.semantic_types import ProductType, SemanticType
from vocabulary.setting_names import setting_name
from vocabulary.wall_layouts import WALL_LAYOUT_WORDS

__all__ = [
    "DEFAULT_LOCAL_FOLDER",
    "LOCAL_FOLDER_VARIABLE",
    "SKILL_NAME",
    "SKILL_SOURCE",
    "build_skill_folder",
    "focus_products",
    "local_skills_folder",
    "product_markdown",
    "rules_markdown",
]

SKILL_NAME: Final = "gv-review"
#: The skill's instructions, kept in the repository and copied unchanged into every chat.
SKILL_SOURCE: Final = Path(__file__).resolve().parent / "skills" / SKILL_NAME / "SKILL.md"
#: Where GV's own files for the skill are kept on this machine (checklists; never committed).
LOCAL_FOLDER_VARIABLE: Final = "GV_ASSISTANT_SKILLS_LOCAL"
DEFAULT_LOCAL_FOLDER: Final = "~/gv-local/skills/gv-review"

#: The files and folder this function writes; a local file of the same name never replaces them.
_OWN_FILES: Final = frozenset({"SKILL.md", "rules.md", "products"})
#: The folder of the products' skill files inside the skill folder.
PRODUCTS_FOLDER: Final = "products"

_SOURCES: Final[Mapping[str, str]] = {
    "ARCH": "from the architect's drawing",
    "SHOP": "from the vendor's drawing",
    "USER_INPUT": "entered by a person",
    "LITERAL": "a fixed value",
    "PRODUCT_SPEC": "from the product's spec sheet",
}

_CHECK_TYPES: Final[Mapping[str, str]] = {
    "internal": "the vendor's drawing against itself (its own numbers must add up)",
    "arch_vs_shop": "the vendor's drawing against the architect's drawing",
    "global": "the vendor's drawing against a fixed GV standard",
}

_ORIGINS: Final[Mapping[str, str]] = {
    "rulebook": "the rulebook's default",
    "gv_default": "GV's company standard",
    "sink_card": "this project's sink card",
    "project": "this project's value",
    "package": "typed for this drawing set",
}

_MISSING: Final = "missing: nobody has entered it, so a check that needs it waits (NOT FOUND)"

_HEADER: Final = """\
# GV's rules for this drawing set

Exported from GV Review for this drawing set: the checks GV's code runs on it and every setting
they read, with the value in force and where it comes from. Read-only: a value is changed in GV
Review (Company settings), never here.

How a setting's value is chosen, lowest to highest: the rulebook's default, then GV's company
standard, then this project's value (or its sink card), then a value typed for this drawing set.
The highest one present is in force. A setting with none is missing: the check that needs it says
NOT FOUND until a person enters it. Never assume a missing value.

GV compares exactly: a number matches or it does not, unless a check below names a tolerance.
All numbers are in inches.
"""


def local_skills_folder(environ: Mapping[str, str] | None = None) -> Path:
    """The folder of GV's own skill files on this machine: `GV_ASSISTANT_SKILLS_LOCAL`, else the
    default. It may not exist; `build_skill_folder` then copies nothing from it."""
    source = os.environ if environ is None else environ
    return Path(source.get(LOCAL_FOLDER_VARIABLE) or DEFAULT_LOCAL_FOLDER).expanduser()


def build_skill_folder(
    session: Session,
    package_revision_id: UUID,
    dest: Path,
    products: Sequence[str] | None = None,
) -> None:
    """Write the skill folder for one drawing set at `dest` (mounted at `/mnt/skills/gv-review`).

    `SKILL.md`, `rules.md` and `products/<id>.md` for each product in focus (`focus_products`),
    plus GV's local files when their folder exists. Replaces an earlier
    folder this function wrote at `dest`; refuses (`FileExistsError`) to replace anything else, so
    a wrong path can never wipe a chat's files. `LookupError` when the revision does not exist.
    `products`: the products the reviewer picked for the chat (`rules_markdown`).
    """
    dest = Path(dest)
    rules_text = rules_markdown(session, package_revision_id, products)
    focus = focus_products(session, package_revision_id, products)
    if dest.exists() and not _replaceable(dest):
        raise FileExistsError(f"{dest} is not an empty folder or a skill folder; not replaced")
    dest.parent.mkdir(parents=True, exist_ok=True)
    building = Path(tempfile.mkdtemp(prefix=f".{dest.name}.building-", dir=dest.parent))
    try:
        local = local_skills_folder()
        if local.is_dir():
            _copy_local(local, building)
        (building / "SKILL.md").write_bytes(SKILL_SOURCE.read_bytes())
        (building / "rules.md").write_bytes(rules_text.encode("utf-8"))
        folder = building / PRODUCTS_FOLDER
        folder.mkdir(exist_ok=True)
        for product in focus:
            (folder / f"{product.id}.md").write_bytes(product_markdown(product).encode("utf-8"))
        _make_readable(building)
        if dest.exists():
            old = dest.with_name(f".{dest.name}.old-{uuid.uuid4().hex[:12]}")
            dest.rename(old)
            building.rename(dest)
            shutil.rmtree(old, ignore_errors=True)
        else:
            building.rename(dest)
    except BaseException:
        shutil.rmtree(building, ignore_errors=True)
        raise


def _replaceable(dest: Path) -> bool:
    """Whether `dest` may be replaced: an empty folder, or one holding this skill's `SKILL.md`."""
    if not dest.is_dir() or dest.is_symlink():
        return False
    skill = dest / "SKILL.md"
    if skill.is_file():
        return f"name: {SKILL_NAME}" in skill.read_text(encoding="utf-8", errors="replace")[:400]
    return not any(dest.iterdir())


def _copy_local(source: Path, target: Path) -> None:
    """Copy GV's local skill files: plain files only, in name order, without hidden files or links
    (a link could reach a file outside the folder), and never over `SKILL.md` or `rules.md`."""
    for folder, folders, files in os.walk(source, followlinks=False):
        here = Path(folder)
        folders[:] = sorted(
            name for name in folders if not name.startswith(".") and not (here / name).is_symlink()
        )
        relative = here.relative_to(source)
        for name in sorted(files):
            path = here / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            if relative == Path(".") and name in _OWN_FILES:
                continue
            if relative.parts[:1] == (PRODUCTS_FOLDER,):
                continue
            out = target / relative / name
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(path.read_bytes())


def _make_readable(root: Path) -> None:
    """Readable by the sandbox's unprivileged user (the mount itself is read-only)."""
    root.chmod(0o755)
    for folder, folders, files in os.walk(root):
        for name in folders:
            (Path(folder) / name).chmod(0o755)
        for name in files:
            (Path(folder) / name).chmod(0o644)


# --------------------------------------------------------------------------------------------------
# products/<id>.md
# --------------------------------------------------------------------------------------------------


def focus_products(
    session: Session, package_revision_id: UUID, products: Sequence[str] | None = None
) -> tuple[Product, ...]:
    """The products in focus, in the registry's order: the reviewer's pick; else the drawing set's
    own product; else (a set from before the product was asked) every active product. An id the
    registry does not know is left out (an old answer's product that was since retired)."""
    known = registry()
    if products is None:
        revision = session.get(PackageRevision, package_revision_id)
        package = None if revision is None else session.get(Package, revision.package_id)
        own = None if package is None else package_product(package)
        if own is None:
            return known.active()
        products = [own.value]
    return tuple(
        product for pid in known.ordered(products) if (product := known.get(pid)) is not None
    )


def product_markdown(product: Product) -> str:
    """One product's file in the skill folder: its own skill text, then its checklist and the kinds
    of GV standard it has, both from the registry, so the brief and the skill never disagree."""
    text = product.skill_file.read_text(encoding="utf-8").rstrip() if product.has_skill else ""
    lines = [text or f"# {product.plural}\n\n{product.description}", ""]
    if product.draft:
        lines.append(
            "**Draft:** this product's checklist is a generic starter, not yet checked with GV.\n"
        )
    lines.append(f"## Checklist for {product.plural.lower()} (every page)\n")
    lines.extend(f"- **{item.name.replace('_', ' ')}**: {item.text}" for item in product.checklist)
    lines.append("")
    if "global_rules" in product.check_kinds:
        lines.append("## GV standards that are not on the drawing\n")
        if product.global_rule_hints:
            lines.extend(f"- {hint}" for hint in product.global_rule_hints)
        else:
            lines.append("- None listed yet.")
        lines.append(
            "\nThe values in force are in `rules.md`; a standard with no published rule is "
            "reported as a check for the reviewer, never assumed.\n"
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------------
# rules.md
# --------------------------------------------------------------------------------------------------


def rules_markdown(
    session: Session, package_revision_id: UUID, products: Sequence[str] | None = None
) -> str:
    """`rules.md` for one drawing set: the checks that run on it and the settings in force.

    With `products` (the reviewer's pick in the chat, chat v4), the checks of those products come
    first and every other product's checks are listed after them, under "Other products"."""
    revision = session.get(PackageRevision, package_revision_id)
    if revision is None:
        raise LookupError(f"no drawing set revision {package_revision_id}")
    package = session.get(Package, revision.package_id)
    if package is None:
        raise LookupError(f"no drawing set for revision {package_revision_id}")
    product = package_product(package)
    store = snapshot_store(session)
    picked = None if products is None else {ProductType(name) for name in products}
    rules = sorted(
        (
            snapshot.rule
            for snapshot in (store.latest(rule_id) for rule_id in store.rule_ids())
            if snapshot is not None
            and (picked is not None or product is None or snapshot.rule.product_type is product)
        ),
        key=lambda rule: (picked is not None and rule.product_type not in picked, rule.id),
    )
    in_force = settings_in_force(session, package.project_id, revision.id, rules)
    sink = session.execute(
        select(ProjectSink)
        .where(ProjectSink.project_id == package.project_id)
        .order_by(ProjectSink.created_at.desc(), ProjectSink.id.desc())
        .limit(1)
    ).scalar_one_or_none()

    lines = [_HEADER]
    if picked is not None:
        names = " and ".join(registry().ordered(item.value for item in picked))
        lines.append(
            f"Products picked for this chat: {names}. Their checks come first; the other "
            'products\' checks follow under "Other products".\n'
        )
        covered = {rule.product_type for rule in rules}
        bare = registry().ordered(item.value for item in picked if item not in covered)
        if bare:
            lines.append(
                f"No GV check is published yet for: {', '.join(bare)}. Compare the vendor with the "
                "architect, and report a GV standard the product file names as a check for the "
                "reviewer (status check), never as a rule.\n"
            )
    elif product is None:
        covered_words = registry().words(sorted({rule.product_type.value for rule in rules}))
        lines.append(
            "Product: not stated for this drawing set, so every published check runs "
            f"({covered_words.lower()}).\n"
        )
    else:
        lines.append(
            f"Product: {product.value}. Only the {product.value} checks run on this set.\n"
        )
    if not rules:
        lines.append(
            "No check is published for this drawing set yet, so GV's code checks nothing here. "
            "Compare the vendor's drawing with the architect's (SKILL.md).\n"
        )
        return without_internal_refs("\n".join(lines))
    lines.extend(_settings_section(rules, in_force, sink))
    lines.append("## The checks\n")
    other = False
    for rule in rules:
        if picked is not None and rule.product_type not in picked and not other:
            other = True
            lines.append("## Other products (not picked for this chat)\n")
        lines.extend(_rule_section(rule, in_force))
    # The rulebook's notes carry the team's ticket numbers; the agent must never quote one (#1520).
    return without_internal_refs("\n".join(lines))


def _settings_section(
    rules: Sequence[Rule], in_force: Mapping[str, InForce], sink: ProjectSink | None
) -> list[str]:
    users: dict[str, list[str]] = {}
    notes: dict[str, str] = {}
    for rule in rules:
        for name, parameter in rule.parameters.items():
            users.setdefault(name, []).append(rule.id)
            if parameter.note is not None:
                notes.setdefault(name, " ".join(parameter.note.split()))
    lines = [
        "## Settings in force\n",
        "| Setting | In force | Where it comes from | Used by |",
        "|---|---|---|---|",
    ]
    for name in sorted(users):
        value = in_force.get(name)
        shown, origin = (
            ("not set", _MISSING) if value is None else (_inches(value.text), _origin(value))
        )
        lines.append(
            f"| {setting_name(name)} (`{name}`) | {shown} | {origin} | {', '.join(users[name])} |"
        )
    lines.append("")
    for name in sorted(users):
        value = in_force.get(name)
        # A rulebook note is about its default, so it is printed only while the default is in
        # force, as the checks print it (`rules.schema.Parameter.note`).
        if name in notes and value is not None and value.origin == "rulebook":
            lines.append(f"- Note on {setting_name(name)}: {notes[name]}")
    # The card's sink is named only while its sizes are the ones in force, as the sink screen does:
    # a size typed for this drawing set may be another sink's.
    from_card = any(
        value.origin == "sink_card"
        for name, value in in_force.items()
        if name in SINK_SETTINGS and name in users
    )
    if sink is not None and from_card:
        where = "" if sink.source_note is None else f" (size from: {sink.source_note})"
        lines.append(f"- Sink on this project's sink card: {sink.model_name}{where}.")
    lines.append("")
    return lines


def _origin(value: InForce) -> str:
    words = _ORIGINS.get(value.origin, value.origin)
    details = [
        part
        for part in (
            None if value.set_by is None else f"set by {value.set_by}",
            None if value.set_at is None else value.set_at.date().isoformat(),
            value.source,
        )
        if part
    ]
    return words if not details else f"{words} ({'; '.join(details)})"


def _rule_section(rule: Rule, in_force: Mapping[str, InForce]) -> list[str]:
    title = rule.name or rule.id
    lines = [f"### {rule.id}: {title}\n"]
    lines.append(f"- Product: {rule.product_type.value}. Version {rule.version}.")
    lines.append(f"- Compares: {_CHECK_TYPES.get(rule.check_type.value, rule.check_type.value)}.")
    if rule.description:
        lines.append(f"- What it checks: {' '.join(rule.description.split())}")
    for name, selector in rule.inputs.items():
        lines.append(f"- Reads `{name}`: {_input_words(selector)}.")
    for name in rule.parameters:
        value = in_force.get(name)
        shown = "not set (missing)" if value is None else _inches(value.text)
        lines.append(f"- Setting `{name}` ({setting_name(name)}): {shown}.")
    for derivation in rule.derivations:
        lines.append(f"- Works out `{derivation.name}` = {_derivation_words(derivation)}.")
    lines.append(f"- Decides: {_decision_words(rule.operation)}")
    if isinstance(rule.applicability, Applicability):
        lines.extend(_variant_lines(rule.applicability))
    lines.append(
        f"- A value it needs is missing: {rule.on_missing.value.replace('_', ' ')}. "
        f"Readings that disagree: {rule.on_ambiguous.value.replace('_', ' ')}."
    )
    lines.append("")
    return lines


def _input_words(selector: InputSelector) -> str:
    meaning = SemanticType(selector.semantic_type).label()
    source = _SOURCES.get(selector.source.value, selector.source.value)
    count = "every one in the run" if selector.cardinality is Cardinality.MANY else "one value"
    return f"{meaning}, {source} ({count})"


def _derivation_words(derivation: Derivation) -> str:
    operands = derivation.operands
    if derivation.operation == "sum" and isinstance(operands.get("values"), tuple | list):
        return " + ".join(f"`{name}`" for name in operands["values"])
    if derivation.operation == "difference_between" and {"a", "b"} <= set(operands):
        return f"`{operands['a']}` - `{operands['b']}`"
    if derivation.operation == "scale" and {"value", "multiplier"} <= set(operands):
        return f"`{operands['value']}` x `{operands['multiplier']}`"
    if derivation.operation == "row_total":
        named = ", ".join(f"`{_binding(binding)}`" for binding in operands.values())
        return (
            f"the total width of the row under the countertop ({named}): every piece in it, "
            "else its cabinets plus its fillers"
        )
    bound = ", ".join(f"{key} `{_binding(binding)}`" for key, binding in operands.items())
    return f"{derivation.operation.replace('_', ' ')} of {bound}"


def _binding(binding: str | tuple[str, ...]) -> str:
    return binding if isinstance(binding, str) else " + ".join(binding)


def _decision_words(operation: OperationRef) -> str:
    operands = operation.operands
    if operation.type == "equals" and {"actual", "expected"} <= set(operands):
        return f"`{operands['actual']}` must equal `{operands['expected']}` exactly."
    if operation.type == "minimum" and {"x", "bound"} <= set(operands):
        return f"`{operands['x']}` must be at least `{operands['bound']}`."
    if operation.type == "maximum" and {"x", "bound"} <= set(operands):
        return f"`{operands['x']}` must be at most `{operands['bound']}`."
    if operation.type == "pairwise_within_tolerance" and {"left", "right"} <= set(operands):
        return (
            f"each value in `{operands['left']}` must match its paired value in "
            f"`{operands['right']}` {_tolerance_words(operation.tolerance)} (paired by place on "
            "the drawing, never by value)."
        )
    if operation.type == "cabinet_run_distribution":
        return (
            "fits the cabinet run to the wall-to-wall measured on site: the difference from the "
            "architect's wall-to-wall goes to the fillers first (within the filler limits), then "
            "is shared over the regular cabinets (within their width limits); equipment cabinets "
            "(sink, appliances) never change size. If it cannot fit, the answer is an RFI to the "
            'architect. A share that is not a whole 1/16" goes to the reviewer, never rounded.'
        )
    bound = ", ".join(f"{key} `{value}`" for key, value in operands.items())
    tolerance = "" if operation.tolerance is None else f", {_tolerance_words(operation.tolerance)}"
    return f"{operation.type.replace('_', ' ')} of {bound}{tolerance}."


def _tolerance_words(tolerance: Tolerance | None) -> str:
    if tolerance is None:
        return "exactly"
    if tolerance.value == TOLERANCE_UNCONFIRMED:
        return "within a tolerance not confirmed yet (so the check can only say REVIEW REQUIRED)"
    if not isinstance(tolerance.value, Fraction):
        return f"within {tolerance.value}"
    if tolerance.value == 0:
        return "exactly"
    unit = tolerance.unit or Unit.INCH
    return f"within {_quantity_words(Quantity(value=tolerance.value, unit=unit))}"


def _variant_lines(applicability: Applicability) -> list[str]:
    lines = [
        (
            f"- Depends on `{applicability.discriminator}`, confirmed for each countertop (by code "
            "or a person, never guessed):"
        )
    ]
    for variant in applicability.variants:
        words = WALL_LAYOUT_WORDS.get(variant.when, variant.when.replace("_", " "))
        details = [f"`{name}` = {count}" for name, count in sorted(variant.extras.items())]
        if variant.tolerance is not None:
            details.append(f"tolerance: {_tolerance_words(variant.tolerance)}")
        tail = f": {', '.join(details)}" if details else ": applies"
        lines.append(f"  - `{variant.when}` ({words}){tail}.")
    lines.append(
        "  - Any other value, or one nobody has set: the check does not run for it (it never "
        "guesses the layout)."
    )
    return lines


def _quantity_words(quantity: Quantity) -> str:
    if quantity.unit is Unit.INCH:
        return f'{format_inches(quantity.exact_value)}"'
    return _inches(f"{quantity.exact_value} {quantity.unit.value}")


def _inches(text: str) -> str:
    """A value as `settings_in_force` writes it, in inches: `2 1/2"` stays; a value entered in
    millimetres is shown in inches (to a thousandth, exact conversion) with the millimetres beside.
    """
    if not text.endswith(" mm"):
        return text
    millimetres = Fraction(text[: -len(" mm")])
    inches = millimetres * Fraction(5, 127)
    shown = (Decimal(inches.numerator) / Decimal(inches.denominator)).quantize(Decimal("0.001"))
    return f'about {shown}" (entered as {text})'
