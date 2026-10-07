"""Verification for issue #68: gold manifests fail loudly and keep cases local."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest
import yaml

from eval.gold_set.schema import (
    DEFAULT_MANIFEST_PATH,
    GoldManifest,
    GroundedCountertop,
    GroundingInk,
    GroundingStatus,
    GroundTruth,
    ManifestLoadError,
    load_manifest,
)
from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Measurement, Unit
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
                    "value": {"exact": "6012", "unit": "mm", "raw_text": "6012"},
                    "page": 3,
                    "polygon": [10, 20, 110, 40],
                    "item_id": "S-CT-1",
                }
            ],
            "matches": [{"arch_item": "A-CAB-1", "shop_item": "S-CAB-1"}],
            "expected_findings": [
                {
                    "check": "CT-WIDTH-001",
                    "outcome": "PASS",
                    "reason": "The reviewed dimensions close exactly.",
                }
            ],
        },
        "provenance": {
            "annotator": "reviewer@example.com",
            "annotated_on": "2026-08-15",
            "documents": [
                {
                    "source": "ARCH",
                    "document_version_id": "12345678-1234-5678-1234-567812345678",
                    "content_hash": "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                },
                {
                    "source": "SHOP",
                    "document_version_id": "12345678-1234-5678-1234-567812345678",
                    "content_hash": "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                },
            ],
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
    assert observation.value == Measurement(Fraction(6012), Unit.MM, "6012")
    assert observation.polygon == (10, 20, 110, 40)
    assert case.ground_truth.expected_findings[0].outcome is Outcome.PASS


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("page", 0),
        ("polygon", [10, 20, 110]),
        ("value", {"exact": 0.1, "unit": "mm", "raw_text": "0.1"}),
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


def _grounded_target(target_id: str, *, status: str = "present") -> dict[str, object]:
    if status != "present":
        return {
            "target_id": target_id,
            "page": 1,
            "status": status,
            **({"unscored_reason": "review key not complete"} if status == "unknown" else {}),
        }
    return {
        "target_id": target_id,
        "page": 1,
        "status": status,
        "object_id": f"object-{target_id}",
        "view_id": "front-elevation",
        "source_ink": "vendor",
        "stone_box": {"x0": 10, "y0": 10, "x1": 90, "y1": 20},
        "stone_ends": [[10, 15], [90, 15]],
        "acceptable_piece_rows": [
            {
                "row_id": "pieces-a",
                "view_id": "front-elevation",
                "role": "pieces",
                "box": {"x0": 10, "y0": 30, "x1": 90, "y1": 40},
                "endpoints": [[10, 35], [90, 35]],
                "labels": [
                    {
                        "span_id": "piece-0",
                        "status": "linked",
                        "label_id": "label-0",
                        "ink": "vendor",
                        "text": "8",
                        "box": {"x0": 20, "y0": 31, "x1": 25, "y1": 36},
                    }
                ],
            }
        ],
        "acceptable_overall_rows": [],
    }


def test_ground_truth_separates_target_rows_spans_and_ink() -> None:
    target = GroundedCountertop.model_validate(_grounded_target("counter-a"))

    assert target.status is GroundingStatus.PRESENT
    assert target.acceptable_piece_rows[0].labels[0].ink is GroundingInk.VENDOR
    assert target.acceptable_piece_rows[0].labels[0].span_id == "piece-0"


@pytest.mark.parametrize(
    "change",
    [
        {"stone_ends": [[10, 15], [10, 15]]},
        {
            "acceptable_piece_rows": [
                {
                    "row_id": "pieces-a",
                    "view_id": "other",
                    "role": "pieces",
                    "box": {"x0": 1, "y0": 1, "x1": 9, "y1": 9},
                    "endpoints": [[1, 2], [8, 2]],
                }
            ]
        },
        {"stone_box": {"x0": 1.5, "y0": 2, "x1": 9, "y1": 9}},
    ],
)
def test_ground_truth_rejects_incomplete_or_lossy_geometry(change: dict[str, object]) -> None:
    payload = _grounded_target("counter-a")
    payload.update(change)

    with pytest.raises(ValueError):
        GroundedCountertop.model_validate(payload)


def test_multiple_countertops_on_one_page_require_distinct_target_ids() -> None:
    first = _grounded_target("counter-a")
    second = _grounded_target("counter-b")
    second["object_id"] = "object-counter-b"
    truth = GroundTruth(
        observations=(),
        matches=(),
        expected_findings=(),
        grounded_countertops=(
            GroundedCountertop.model_validate(first),
            GroundedCountertop.model_validate(second),
        ),
    )

    assert [target.target_id for target in truth.grounded_countertops] == ["counter-a", "counter-b"]


def test_unknown_ground_truth_is_unscored_not_a_negative() -> None:
    unknown = GroundedCountertop.model_validate(_grounded_target("counter-a", status="unknown"))
    absent = GroundedCountertop.model_validate(_grounded_target("no-counter", status="absent"))

    assert unknown.status is GroundingStatus.UNKNOWN
    assert unknown.unscored_reason
    assert absent.status is GroundingStatus.ABSENT
