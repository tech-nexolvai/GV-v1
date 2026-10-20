"""A test product registry: the repository's registry plus one made-up product (#1547).

`use_dummy_registry` writes a copy of `vocabulary/products.yaml` with a made-up active product,
`dummy_widget`, and its skill file into a temporary folder, points `GV_PRODUCT_REGISTRY` at it and
forgets the registry read so far. It is how the tests show that a product added to the registry
reaches the API, the chat's checks, the skill folder and the full-check brief with no code change.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from vocabulary.product_registry import REGISTRY_VARIABLE, registry_data, reset_registry

DUMMY = "dummy_widget"

DUMMY_ENTRY = {
    "id": DUMMY,
    "label": "Dummy widget",
    "plural": "Dummy widgets",
    "description": "A made-up product for tests.",
    "active": True,
    "draft": True,
    "check_kinds": ["dimension_match", "global_rules"],
    "checklist": [
        {"name": "widget_size", "text": "Widget width and height against the architect's."},
        {"name": "widget_knob", "text": "Knob count and finish code."},
    ],
    "global_rule_hints": ["Knob finish matches GV's made-up standard."],
}

DUMMY_SKILL = "# Dummy widgets\n\nMade up for tests: compare each widget's size and knobs.\n"


def write_dummy_registry(folder: Path) -> Path:
    """The repository's registry plus `dummy_widget`, written into `folder`; returns its path."""
    data = registry_data()
    skills = folder / "products"
    skills.mkdir(parents=True, exist_ok=True)
    (skills / f"{DUMMY}.md").write_text(DUMMY_SKILL, encoding="utf-8")
    data["skills_folder"] = "products"
    data["products"].append(DUMMY_ENTRY)
    path = folder / "products.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@contextmanager
def dummy_registry(folder: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    path = write_dummy_registry(folder)
    monkeypatch.setenv(REGISTRY_VARIABLE, str(path))
    reset_registry()
    try:
        yield path
    finally:
        monkeypatch.delenv(REGISTRY_VARIABLE, raising=False)
        reset_registry()


@pytest.fixture
def use_dummy_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    with dummy_registry(tmp_path / "registry", monkeypatch) as path:
        yield path
