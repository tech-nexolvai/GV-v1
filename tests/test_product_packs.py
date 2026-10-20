"""Product packs: a product is a folder of settings, not code (#1776). No database, no model, $0.

What is asserted:

- every product of the repository's registry is a pack folder (`vocabulary/products/<id>/`) whose
  `pack.yaml` id is its folder's name, with its `skill.md`; `products.yaml` lists them in order;
- `registry_data()` writes the same registry inline (what the tests copy and change);
- the `_template/` pack loads once copied, with every pack field filled in;
- the loader refuses two packs that claim the same word, end word, attribute or semantic type, a
  shared word no other product uses, a bad pattern, an unknown field and a malformed example;
- the data moved from code into the countertop and cabinet packs is unchanged: the architect
  note's part kinds and the material words (`architect_items`) and the chat ledger's filing words
  (`ledger`) give the same patterns and answers as the constants they replace.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.review.assistant import ledger
from app.review.assistant.product_packs import item_product, material_pattern, part_kinds
from vocabulary.product_registry import (
    DEFAULT_REGISTRY,
    RegistryError,
    load_registry,
    registry,
    registry_data,
)

PACKS = DEFAULT_REGISTRY.parent / "products"
TEMPLATE = PACKS / "_template"


# ---- the repository's packs --------------------------------------------------------------------


def test_every_product_is_a_pack_folder() -> None:
    listed = yaml.safe_load(DEFAULT_REGISTRY.read_text(encoding="utf-8"))["products"]
    folders = sorted(p.name for p in PACKS.iterdir() if p.is_dir() and not p.name.startswith("_"))
    assert all(isinstance(entry, str) for entry in listed)
    assert sorted(listed) == folders
    known = load_registry(DEFAULT_REGISTRY)
    assert known.ids() == tuple(listed)
    for product in known.products:
        assert product.folder == PACKS / product.id
        assert product.skill_file == PACKS / product.id / "skill.md" and product.has_skill
        data = yaml.safe_load((PACKS / product.id / "pack.yaml").read_text(encoding="utf-8"))
        assert data["id"] == product.id


def test_registry_data_writes_the_same_registry_inline(tmp_path: Path) -> None:
    data = registry_data(DEFAULT_REGISTRY)
    assert all(isinstance(entry, dict) and entry["folder"] for entry in data["products"])
    path = tmp_path / "products.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    copy, real = load_registry(path), load_registry(DEFAULT_REGISTRY)
    assert copy.products == real.products


def _with_pack(tmp_path: Path, pid: str, change: Callable[[dict[str, Any]], None]) -> Path:
    """The repository's registry plus the template copied as pack `pid`, changed by `change`."""
    data = registry_data(DEFAULT_REGISTRY)
    folder = tmp_path / pid
    shutil.copytree(TEMPLATE, folder)
    pack = yaml.safe_load((folder / "pack.yaml").read_text(encoding="utf-8"))
    pack["id"] = pid
    change(pack)
    (folder / "pack.yaml").write_text(yaml.safe_dump(pack, sort_keys=False), encoding="utf-8")
    packs = tmp_path / "packs"
    packs.mkdir(exist_ok=True)
    shutil.move(str(folder), packs / pid)
    data["packs_folder"] = "packs"
    data["products"].append(pid)
    path = tmp_path / "products.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_the_template_loads_as_a_pack(tmp_path: Path) -> None:
    widget = load_registry(_with_pack(tmp_path, "widget", lambda p: None)).get("widget")
    assert widget is not None and not widget.active and widget.draft
    assert widget.has_skill and widget.example is not None
    assert widget.ours.startswith("the widget itself")
    assert [e.name for e in widget.end_words] == ["widget_centre", "top_of_widget"]
    centre = widget.end_words[0]
    assert centre.centred and not centre.strict
    assert centre.words == ("widget centre", "centre of widget", "widget centre line")
    assert [(k.kind, k.countable) for k in widget.part_kinds] == [("knobs", True)]
    assert re.search(widget.part_kinds[0].vendor, "(2) KNOBS", re.IGNORECASE)
    finish = widget.attributes[0]
    assert finish.values == ("BRASS", "CHROME", "BLACK")
    assert finish.synonyms == {"CHROME": ("POLISHED CHROME",)}
    assert widget.size_formats == ("diameter",)
    assert [(t.code, t.unit) for t in widget.semantic_types] == [("WG_MOUNT_HT", "in")]
    assert widget.impact_words == ("mounting height",)


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda p: p.update(words=["cabinet"]), r"word\(s\) given to two products: cabinet"),
        (
            lambda p: p.update(end_words=[{"name": "top_of_widget", "aliases": ["fixture top"]}]),
            None,
        ),
        (lambda p: p.update(shared_words=["sprocket"]), "which no other product uses"),
        (lambda p: p.update(shared_words=["widget"]), "are its own words"),
        (lambda p: p.update(room_words_extra=["widget"]), "are its own words"),
        (lambda p: p.update(colour="red"), "unknown field"),
        (lambda p: p.update(folder="/elsewhere"), "unknown field"),
        (lambda p: p.update(item_codes=["(unclosed"]), "not a regular expression"),
        (
            lambda p: p.update(attributes=[{"name": "knob_finish", "pattern": "[x"}]),
            "not a regular expression",
        ),
        (lambda p: p.update(attributes=[{"name": "knob_finish"}]), "needs values or a pattern"),
        (lambda p: p.update(size_formats=["hexagon"]), "size_formats"),
        (
            lambda p: p.update(semantic_types=[{"code": "wg", "meaning": "x", "unit": "in"}]),
            "capital letters",
        ),
        (
            lambda p: p.update(semantic_types=[{"code": "WG_A", "meaning": "x", "unit": "kg"}]),
            "unit must be one of",
        ),
        (lambda p: p.update(part_kinds=[{"kind": "knobs"}]), "needs architect_words"),
        (
            lambda p: p.update(end_words=[{"name": "Widget Top"}]),
            "lowercase letters, digits and _",
        ),
    ],
    ids=[
        "word-of-another-pack",
        "own-end-words-are-fine",
        "shared-word-nobody-uses",
        "shared-own-word",
        "room-word-is-own-word",
        "unknown-field",
        "folder-in-a-pack",
        "bad-item-code",
        "bad-attribute-pattern",
        "attribute-without-values",
        "unknown-size-format",
        "bad-semantic-code",
        "bad-semantic-unit",
        "part-without-words",
        "bad-end-word-name",
    ],
)
def test_a_pack_that_does_not_hold_together_is_refused(
    tmp_path: Path, change: Callable[[dict[str, Any]], None], says: str | None
) -> None:
    path = _with_pack(tmp_path, "widget", change)
    if says is None:
        assert load_registry(path).get("widget") is not None
        return
    with pytest.raises(RegistryError, match=says):
        load_registry(path)


def test_two_packs_never_claim_the_same_end_word_attribute_or_type(tmp_path: Path) -> None:
    first = _with_pack(tmp_path / "a", "widget", lambda p: None)
    data = yaml.safe_load(first.read_text(encoding="utf-8"))
    twin = tmp_path / "a" / "packs" / "gadget"
    shutil.copytree(tmp_path / "a" / "packs" / "widget", twin)
    pack = yaml.safe_load((twin / "pack.yaml").read_text(encoding="utf-8"))
    pack.update(id="gadget", words=["gadget"])
    data["products"].append("gadget")

    def write(**changes: Any) -> Path:
        (twin / "pack.yaml").write_text(yaml.safe_dump({**pack, **changes}), encoding="utf-8")
        first.write_text(yaml.safe_dump(data), encoding="utf-8")
        return first

    with pytest.raises(RegistryError, match=r"end word\(s\) given to two products"):
        load_registry(write(attributes=[], semantic_types=[]))
    with pytest.raises(RegistryError, match=r"attribute\(s\) given to two products: knob_finish"):
        load_registry(write(end_words=[], semantic_types=[]))
    with pytest.raises(RegistryError, match=r"semantic type\(s\) given to two products"):
        load_registry(write(end_words=[], attributes=[]))
    assert load_registry(write(end_words=[], attributes=[], semantic_types=[])).get("gadget")


def test_a_malformed_example_is_refused(tmp_path: Path) -> None:
    path = _with_pack(tmp_path, "widget", lambda p: None)
    example = tmp_path / "packs" / "widget" / "example.json"
    example.write_text(json.dumps({"input": "x", "reply": {"pairs": []}}), encoding="utf-8")
    with pytest.raises(RegistryError, match="reply needs a `other_items` list"):
        load_registry(path)
    example.write_text("{not json", encoding="utf-8")
    with pytest.raises(RegistryError, match="could not be read"):
        load_registry(path)


def test_a_listed_pack_must_exist_and_carry_its_own_id(tmp_path: Path) -> None:
    path = _with_pack(tmp_path, "widget", lambda p: p.update(id="gizmo"))
    with pytest.raises(RegistryError, match="its id must be its folder's name"):
        load_registry(path)
    pack = tmp_path / "packs" / "widget" / "pack.yaml"
    pack.write_text(pack.read_text(encoding="utf-8").replace("id: gizmo", "id: widget"))
    assert load_registry(path).get("widget") is not None
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["products"].append("nowhere")
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(RegistryError, match="has no"):
        load_registry(path)


# ---- the data moved from code is unchanged --------------------------------------------------------

#: `architect_items._PARTS` before #1776, as it was written in code.
OLD_PARTS = (
    ("drawers", r"\bDRAWERS?\b|\bPULL[- ]?OUTS?\b", r"\bDRAWERS?\b|\bPULL[- ]?OUTS?\b|\bDWRS?\b"),
    ("shelves", r"\bSHEL(?:F|VES|VING)\b", r"\bSHEL(?:F|VES|VING)\b|\bSHLF\b"),
    ("pulls", r"\bPULLS?\b(?![- ]?OUT)", r"\bPULLS?\b(?![- ]?OUT)|\bHANDLES?\b|\bKNOBS?\b"),
    ("hinges", r"\bHINGES?\b", r"\bHINGES?\b|\bHINGED\b"),
    ("grommets", r"\bGROMMETS?\b", r"\bGROMMETS?\b"),
    ("locks", r"\bLOCKS?\b", r"\bLOCKS?\b"),
    (
        "bins",
        r"\b(?:TRASH|WASTE|RECYCL\w*)\b|\bBINS?\b",
        r"\b(?:TRASH|WASTE|RECYCL\w*)\b|\bBINS?\b",
    ),
    (
        "outlets",
        r"\bOUTLETS?\b|\bRECEPTACLES?\b|\bJ-?BOX(?:ES)?\b|\bGFC?I\b",
        r"\bOUTLETS?\b|\bRECEPTACLES?\b|\bJ-?BOX(?:ES)?\b|\bGFC?I\b|\bDUPLEX\b|\bPOWER\b",
    ),
)
#: `architect_items._MATERIAL_WORDS` before #1776.
OLD_MATERIALS = {
    "countertop": r"\b(?:stone|quartz|granite|marble|solid surface|countertop|counter top)\b",
    "cabinet": r"\b(?:laminate|plam|veneer|melamine|wood|lacquer|thermofoil)\b",
}


def _old_ledger_category(words: list[str]) -> str:
    """`ledger._category` before #1776."""
    countertop = ("countertop", "counter", "top", "backsplash", "splash", "overhang")
    cabinet = ("cabinet", "base", "wall", "tall", "vanity", "drawer", "door", "filler")
    present = set(words)
    if present & set(countertop) or "ct" in present:
        return "countertop"
    if present & set(cabinet) or any(re.fullmatch(r"(?:b|w|sb|t|v)\d+", w) for w in present):
        return "cabinet"
    return "other"


def test_the_architect_note_parts_and_material_words_are_unchanged() -> None:
    kinds = part_kinds()
    assert [(n, a.pattern, v.pattern) for n, a, v in kinds] == list(OLD_PARTS)
    assert all(a.flags & re.IGNORECASE and v.flags & re.IGNORECASE for _n, a, v in kinds)
    for pid, pattern in OLD_MATERIALS.items():
        product = registry().get(pid)
        assert product is not None
        found = material_pattern(product)
        assert found is not None and found.pattern == pattern and found.flags & re.IGNORECASE
    lighting = registry().get("lighting")
    assert lighting is not None and material_pattern(lighting) is None


@pytest.mark.parametrize(
    "item",
    [
        "Base B-36",
        "b36 base cabinet",
        "SB30",
        "vanity top",
        "Countertop overhang",
        "CT-1",
        "upper wall cabinet",
        "toe kick",
        "drawer bank",
        "filler 2",
        "sink centre line",
        "pendant light",
        "W2436",
        "tall pantry",
        "backsplash height",
        "",
    ],
)
def test_the_ledger_files_items_as_before(item: str) -> None:
    words = ledger._words(item)
    assert item_product(words) == _old_ledger_category(words)
    assert ledger._category(item) == _old_ledger_category(words)
