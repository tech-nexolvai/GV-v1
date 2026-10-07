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
from enum import StrEnum
from pathlib import Path
from typing import Literal
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


class GroundingInk(StrEnum):
    """Which layer owns a printed label, as independently reviewed from the source drawing."""

    VENDOR = "vendor"
    GV = "gv"
    COVERED = "covered"
    UNKNOWN = "unknown"


class GroundingStatus(StrEnum):
    """Whether the person established a countertop target or left the question unresolved."""

    PRESENT = "present"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class GroundedBox(BaseModel):
    """A reviewed box in integer pixels of the exact, hash-bound page rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    x0: int = Field(ge=0)
    y0: int = Field(ge=0)
    x1: int = Field(gt=0)
    y1: int = Field(gt=0)

    @field_validator("x0", "y0", "x1", "y1", mode="before")
    @classmethod
    def _coordinate_is_exact_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(  # noqa: TRY004 - Pydantic attaches the exact field path.
                "ground truth pixel coordinates must be exact integers"
            )
        return value

    @model_validator(mode="after")
    def _positive_area(self) -> GroundedBox:
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            raise ValueError("a grounded box must have positive width and height")
        return self


class GroundedLabel(BaseModel):
    """A person-reviewed label-to-span link, kept separate from value correctness."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    span_id: str = Field(min_length=1)
    status: Literal["linked", "missing", "unreadable", "covered", "unknown"]
    label_id: str | None = None
    ink: GroundingInk
    text: str | None = None
    box: GroundedBox | None = None

    @model_validator(mode="after")
    def _linked_label_has_identity(self) -> GroundedLabel:
        if self.status == "linked" and (
            self.label_id is None or self.text is None or self.box is None
        ):
            raise ValueError("a linked label needs its identity, printed text and reviewed box")
        if self.ink is GroundingInk.COVERED and self.status != "covered":
            raise ValueError("a covered source label must use status='covered'")
        return self


class GroundedRow(BaseModel):
    """A human-reviewed dimension row and its labels for one physical countertop."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    row_id: str = Field(min_length=1)
    view_id: str = Field(min_length=1)
    role: Literal["pieces", "overall", "other"]
    box: GroundedBox
    endpoints: tuple[tuple[int, int], tuple[int, int]]
    labels: tuple[GroundedLabel, ...] = ()

    @field_validator("endpoints", mode="before")
    @classmethod
    def _endpoints_are_exact_integer_pairs(cls, value: object) -> object:
        if (
            not isinstance(value, (tuple, list))
            or len(value) != 2
            or any(
                not isinstance(point, (tuple, list))
                or len(point) != 2
                or any(isinstance(axis, bool) or not isinstance(axis, int) for axis in point)
                for point in value
            )
        ):
            raise ValueError("row endpoints must be two exact integer pixel points")
        return value

    @model_validator(mode="after")
    def _row_has_distinct_ends(self) -> GroundedRow:
        if self.endpoints[0] == self.endpoints[1]:
            raise ValueError("a row's two reviewed endpoints must be distinct")
        if self.role == "other" and self.labels:
            raise ValueError("an unrelated row cannot carry countertop label links")
        return self


class GroundedCountertop(BaseModel):
    """Independent object, view, endpoint and row truth for evaluation only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: str = Field(min_length=1)
    page: int = Field(ge=1)
    status: GroundingStatus
    object_id: str | None = None
    view_id: str | None = None
    source_ink: GroundingInk = GroundingInk.UNKNOWN
    stone_box: GroundedBox | None = None
    stone_ends: tuple[tuple[int, int], tuple[int, int]] | None = None
    acceptable_piece_rows: tuple[GroundedRow, ...] = ()
    acceptable_overall_rows: tuple[GroundedRow, ...] = ()
    unscored_reason: str | None = None

    @field_validator("page", mode="before")
    @classmethod
    def _page_is_exact_positive_integer(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("ground-truth page must be a positive integer")
        return value

    @field_validator("stone_ends", mode="before")
    @classmethod
    def _stone_ends_are_exact_integer_pairs(cls, value: object) -> object:
        if value is None:
            return value
        return GroundedRow._endpoints_are_exact_integer_pairs(value)

    @model_validator(mode="after")
    def _truth_is_explicit(self) -> GroundedCountertop:
        if self.status is GroundingStatus.PRESENT:
            if (
                self.object_id is None
                or self.view_id is None
                or self.stone_box is None
                or self.stone_ends is None
                or self.source_ink is GroundingInk.UNKNOWN
            ):
                raise ValueError(
                    "a present countertop needs reviewed identity, view, box, ends and ink"
                )
            if self.stone_ends[0] == self.stone_ends[1]:
                raise ValueError("the reviewed stone's two ends must be distinct")
            rows = (*self.acceptable_piece_rows, *self.acceptable_overall_rows)
            if any(row.view_id != self.view_id for row in rows):
                raise ValueError("accepted rows must belong to the reviewed countertop view")
            if any(row.role != "pieces" for row in self.acceptable_piece_rows):
                raise ValueError("acceptable_piece_rows may contain only piece rows")
            if any(row.role != "overall" for row in self.acceptable_overall_rows):
                raise ValueError("acceptable_overall_rows may contain only overall rows")
            row_ids = [row.row_id for row in rows]
            if len(row_ids) != len(set(row_ids)):
                raise ValueError("accepted row ids must be unique within a target")
            span_ids = [label.span_id for row in rows for label in row.labels]
            if len(span_ids) != len(set(span_ids)):
                raise ValueError("each reviewed span may appear in only one accepted row")
            if self.unscored_reason is not None:
                raise ValueError("present truth is scored and cannot carry an unscored reason")
        elif any(
            (
                self.object_id,
                self.view_id,
                self.stone_box,
                self.stone_ends,
                self.acceptable_piece_rows,
                self.acceptable_overall_rows,
            )
        ):
            raise ValueError("absent or unknown truth cannot claim present-object geometry")
        if self.status is GroundingStatus.ABSENT and self.unscored_reason is not None:
            raise ValueError("absent truth is a scored negative, not an unscored unknown")
        if self.status is GroundingStatus.UNKNOWN and not self.unscored_reason:
            raise ValueError("unknown countertop truth needs an unscored reason")
        return self


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
    grounded_countertops: tuple[GroundedCountertop, ...] = ()

    @model_validator(mode="after")
    def _targets_are_uniquely_named(self) -> GroundTruth:
        keys = [(target.page, target.target_id) for target in self.grounded_countertops]
        duplicates = sorted({key for key in keys if keys.count(key) > 1})
        if duplicates:
            raise ValueError(
                "ground-truth targets must have unique (page, target_id) identities: "
                f"{duplicates}"
            )
        return self


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
    if ground_truth.grounded_countertops:
        # Object, row and ink ownership all refer to the shop drawing, even if there are no
        # numeric observations yet (for example, an explicitly absent target).
        used.add(OperandSource.SHOP)
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
