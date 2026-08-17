"""Typed gold-case manifests and their filesystem-safe loader.

The committed manifest is an empty template. Reviewed cases and their answers are client
material and belong under the git-ignored ``eval/gold_set/cases`` directory. This module
validates that local material; it never supplies or guesses missing ground truth.

Source: issue #68 and ``docs/V1_RESEARCH_AND_PLAN.md`` section 6.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from uuid import UUID

import yaml  # type: ignore[import-untyped]  # PyYAML does not publish inline type information.
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from rules.semantic_types import OperandSource, ProductType, SemanticType
from units.measurement import Measurement
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
    value: Measurement
    page: int = Field(ge=1)
    polygon: tuple[int, int, int, int]
    item_id: str = Field(min_length=1)

    @field_validator("value", mode="before")
    @classmethod
    def _measurement_is_authored_exactly(cls, value: object) -> object:
        """Reject lossy numeric input before Pydantic can coerce it to ``Fraction``."""

        if isinstance(value, Mapping):
            exact = value.get("exact")
            if isinstance(exact, (bool, float)):
                raise ValueError(  # noqa: TRY004 - Pydantic must attach the field path.
                    "measurement exact must be authored as exact text, never a float or boolean"
                )
        return value


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
    reason: str = Field(min_length=1)


#: Sources whose bytes exist and can therefore be hashed. `LITERAL` and `USER_INPUT` cannot: one is
#: written into a rule, the other is what somebody typed, and neither has a document to bind to.
HASHED_SOURCES: frozenset[OperandSource] = frozenset(
    {OperandSource.ARCH, OperandSource.SHOP, OperandSource.PRODUCT_SPEC}
)

#: `sha256:<64 lowercase hex>` — the form `storage/hashing.py` emits. One dialect across the system,
#: so a gold case's hash and a stored artifact's can be compared without anybody re-deriving either.
CONTENT_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReviewedDocument(BaseModel):
    """One document an annotator read, bound to the exact bytes they read.

    A gold case reads more than one. `GoldMatch` pairs an `arch_item` with a `shop_item`, so the
    architectural drawing is annotated as surely as the shop drawing is — and until `#187` this
    schema carried a single `content_hash` for the case. Swapping the architectural PDF would have
    invalidated every match while the integrity check reported the case intact, which is the one
    failure a gold set exists to prevent.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: OperandSource
    document_version_id: UUID
    content_hash: str

    @field_validator("source")
    @classmethod
    def _source_has_bytes(cls, value: OperandSource) -> OperandSource:
        if value not in HASHED_SOURCES:
            raise ValueError(
                f"{value.value} has no document to hash. A literal lives in a rule and a user input "
                "is what somebody typed; binding either to a content hash would claim a provenance "
                "that cannot be re-checked."
            )
        return value

    @field_validator("content_hash")
    @classmethod
    def _hash_is_canonical(cls, value: str) -> str:
        if CONTENT_HASH.fullmatch(value) is None:
            raise ValueError(
                f"content hash {value!r} must be 'sha256:<64 lowercase hex>'. The prefix names the "
                "algorithm, and it is the form storage/hashing.py already emits — two dialects would "
                "mean a stored artifact and its gold case could not be compared without translation."
            )
        return value


class Provenance(BaseModel):
    """Who authored a gold case and which immutable documents they reviewed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotator: str = Field(min_length=1)
    annotated_on: date
    documents: tuple[ReviewedDocument, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _one_document_per_source(self) -> Provenance:
        sources = [document.source for document in self.documents]
        duplicates = sorted({s.value for s in sources if sources.count(s) > 1})
        if duplicates:
            raise ValueError(
                f"two documents claim the same source: {duplicates}. An observation names its source, "
                "so a repeated one leaves no way to say which bytes it was read from."
            )
        return self

    def hash_for(self, source: OperandSource) -> str | None:
        """The recorded hash for one source, or `None` when that source was not reviewed."""
        for document in self.documents:
            if document.source is source:
                return document.content_hash
        return None


class Disagreement(BaseModel):
    """A second annotator's differing reading, preserved rather than resolved."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    annotator: str = Field(min_length=1)
    field: str = Field(min_length=1)
    their_value: str
    note: str = ""


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
    provenance: Provenance
    disagreements: tuple[Disagreement, ...] = ()

    @model_validator(mode="after")
    def _every_source_read_is_bound_to_bytes(self) -> GoldCase:
        """Refuse a case whose answer key relies on a document it never bound to a hash.

        Requiring "at least one document" is not enough, and the gap is the same one that prompted
        this schema: an annotation citing the architectural drawing, with only the shop drawing
        recorded, verifies clean while half of it is unbound. Whatever the answer key read has to be
        checkable, or the check reports on the part nobody was worried about.
        """
        recorded = {document.source for document in self.provenance.documents}
        missing = sorted(s.value for s in _hashable_sources_used(self.ground_truth) - recorded)
        if missing:
            raise ValueError(
                f"case {self.id!r} reads {missing} but records no content hash for "
                f"{'them' if len(missing) > 1 else 'it'}. An annotation against bytes nothing "
                "verifies cannot be trusted, and the integrity check would report the case intact."
            )
        return self


def _hashable_sources_used(ground_truth: GroundTruth) -> set[OperandSource]:
    """Every source with bytes that this case's answer key actually relied on.

    `GoldMatch` names no source explicitly, but pairing an `arch_item` with a `shop_item` means both
    drawings were read — the association is the annotation.
    """
    used = {o.source for o in ground_truth.observations if o.source in HASHED_SOURCES}
    if ground_truth.matches:
        used |= {OperandSource.ARCH, OperandSource.SHOP}
    return used


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
