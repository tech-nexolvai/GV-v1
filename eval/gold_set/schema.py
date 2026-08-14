"""Typed gold-case manifests and their filesystem-safe loader.

The committed manifest is an empty template. Reviewed cases and their answers are client
material and belong under the git-ignored ``eval/gold_set/cases`` directory. This module
validates that local material; it never supplies or guesses missing ground truth.

Source: issue #68 and ``docs/V1_RESEARCH_AND_PLAN.md`` section 6.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import yaml  # type: ignore[import-untyped]  # PyYAML does not publish inline type information.
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Unit, to_exact_fraction
from verdict.outcomes import Outcome

DEFAULT_MANIFEST_PATH = Path(__file__).with_name("manifest.yaml")
DEFAULT_CASES_DIRECTORY = Path(__file__).with_name("cases")


class ManifestLoadError(ValueError):
    """The gold manifest could not be parsed or did not conform to the case schema."""


class GoldObservation(BaseModel):
    """One reviewed value and the exact place where it appears on a drawing."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    semantic_type: SemanticType
    source: OperandSource
    value: Fraction
    unit: Unit
    page: int = Field(ge=1)
    polygon: tuple[int, int, int, int]
    item_id: str = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def _value_is_exact(cls, value: object) -> Fraction:
        return to_exact_fraction(value)


class GoldMatch(BaseModel):
    """A reviewed association between architectural and shop-drawing items."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    arch_item: str = Field(min_length=1)
    shop_item: str = Field(min_length=1)


class ExpectedFinding(BaseModel):
    """The outcome a reviewed case expects from one published check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check: str = Field(min_length=1)
    outcome: Outcome


class GroundTruth(BaseModel):
    """The human-reviewed observations, matches, and findings for one case."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    observations: tuple[GoldObservation, ...]
    matches: tuple[GoldMatch, ...]
    expected_findings: tuple[ExpectedFinding, ...]


class GoldCase(BaseModel):
    """One architectural/shop package and its reviewed answer key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    product_type: ProductType
    arch: Path
    shop: Path
    ground_truth: GroundTruth


class GoldManifest(BaseModel):
    """Versioned index of local proprietary gold cases."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(ge=0)
    cases: tuple[GoldCase, ...]

    @model_validator(mode="after")
    def _case_ids_are_unique(self) -> GoldManifest:
        ids = [case.id for case in self.cases]
        duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate gold case id(s): {duplicates}")
        return self


def load_manifest(
    path: str | Path = DEFAULT_MANIFEST_PATH,
    *,
    cases_directory: str | Path = DEFAULT_CASES_DIRECTORY,
) -> GoldManifest:
    """Create required directories, then parse and validate a gold manifest.

    Directory creation is idempotent and never creates example case data. A missing manifest
    remains a loud ``FileNotFoundError``; malformed YAML or invalid case data raises
    ``ManifestLoadError`` with the source path and the original exception as its cause.

    Pass a manifest path under the ignored cases directory for real client cases. The default
    path loads only the committed empty template.
    """
    manifest_path = Path(path)
    case_path = Path(cases_directory)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    case_path.mkdir(parents=True, exist_ok=True)

    try:
        raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        return GoldManifest.model_validate(raw)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, yaml.YAMLError, ValidationError) as error:
        raise ManifestLoadError(f"invalid gold manifest {manifest_path}: {error}") from error
