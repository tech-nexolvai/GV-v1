"""Verification for issue #68: gold manifests fail loudly and keep cases local."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from eval.gold_set.schema import (
    DEFAULT_MANIFEST_PATH,
    GoldManifest,
    ManifestLoadError,
    load_manifest,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Unit
from verdict.outcomes import Outcome


def _valid_case() -> dict[str, object]:
    return {
        "id": "CT-PROJECT-001",
        "product_type": "countertop",
        "arch": "data/drawings/project/arch/approved.pdf",
        "shop": "data/drawings/project/shop/vendor.pdf",
        "ground_truth": {
            "observations": [
                {
                    "semantic_type": "countertop_overall_width",
                    "source": "SHOP",
                    "value": "6012",
                    "unit": "mm",
                    "page": 3,
                    "polygon": [10, 20, 110, 40],
                    "item_id": "S-CT-1",
                }
            ],
            "matches": [{"arch_item": "A-CAB-1", "shop_item": "S-CAB-1"}],
            "expected_findings": [{"check": "CT-WIDTH-001", "outcome": "PASS"}],
        },
    }


def _write_manifest(path: Path, cases: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"version": 0, "cases": cases}), encoding="utf-8")


def test_committed_template_loads_as_an_empty_manifest(tmp_path: Path) -> None:
    cases_directory = tmp_path / "private-cases"

    manifest = load_manifest(DEFAULT_MANIFEST_PATH, cases_directory=cases_directory)

    assert manifest == GoldManifest(version=0, cases=())
    assert cases_directory.is_dir()


def test_valid_case_preserves_exact_observation_and_controlled_types(tmp_path: Path) -> None:
    path = tmp_path / "private" / "manifest.yaml"
    _write_manifest(path, [_valid_case()])

    manifest = load_manifest(path, cases_directory=tmp_path / "case-files")
    case = manifest.cases[0]
    observation = case.ground_truth.observations[0]

    assert case.product_type is ProductType.COUNTERTOP
    assert observation.semantic_type is SemanticType.COUNTERTOP_OVERALL_WIDTH
    assert observation.source is OperandSource.SHOP
    assert observation.value == Fraction(6012)
    assert observation.unit is Unit.MM
    assert observation.polygon == (10, 20, 110, 40)
    assert case.ground_truth.expected_findings[0].outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("page", 0),
        ("polygon", [10, 20, 110]),
        ("value", 0.1),
        ("semantic_type", "made_up_width"),
        ("source", "UNKNOWN"),
    ],
)
def test_malformed_observation_is_a_loud_error(
    tmp_path: Path, field: str, bad_value: object
) -> None:
    case = _valid_case()
    observation = case["ground_truth"]["observations"][0]  # type: ignore[index]
    observation[field] = bad_value  # type: ignore[index]
    path = tmp_path / "manifest.yaml"
    _write_manifest(path, [case])

    with pytest.raises(ManifestLoadError) as error:
        load_manifest(path, cases_directory=tmp_path / "cases")

    assert str(path) in str(error.value)
    assert field in str(error.value)


def test_unknown_case_field_is_rejected_instead_of_ignored(tmp_path: Path) -> None:
    case = _valid_case()
    case["expected_finding"] = "PASS"
    path = tmp_path / "manifest.yaml"
    _write_manifest(path, [case])

    with pytest.raises(ManifestLoadError, match="expected_finding"):
        load_manifest(path, cases_directory=tmp_path / "cases")


def test_invalid_yaml_is_wrapped_with_the_manifest_path(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text("cases: [unterminated", encoding="utf-8")

    with pytest.raises(ManifestLoadError) as error:
        load_manifest(path, cases_directory=tmp_path / "cases")

    assert str(path) in str(error.value)


def test_duplicate_case_ids_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    _write_manifest(path, [_valid_case(), _valid_case()])

    with pytest.raises(ManifestLoadError, match="duplicate gold case id"):
        load_manifest(path, cases_directory=tmp_path / "cases")


def test_loader_creates_missing_directories_without_inventing_a_manifest(tmp_path: Path) -> None:
    path = tmp_path / "new" / "nested" / "manifest.yaml"
    cases_directory = tmp_path / "private" / "cases"

    with pytest.raises(FileNotFoundError):
        load_manifest(path, cases_directory=cases_directory)

    assert path.parent.is_dir()
    assert cases_directory.is_dir()
    assert not path.exists()


def test_proprietary_case_directory_is_ignored_by_git() -> None:
    gitignore = (DEFAULT_MANIFEST_PATH.parents[2] / ".gitignore").read_text(encoding="utf-8")
    assert "eval/gold_set/cases/" in gitignore
