"""Single source of truth for semantic types and client vocabulary names.

Every rule and observation must reference these constants instead of hard-coded
strings, so a vocabulary change remains local to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum


class SemanticType(str, Enum):
    """Canonical meanings used by observations and rule selectors."""

    COUNTERTOP_OVERALL_WIDTH = "countertop_overall_width"
    CABINET_WIDTH = "cabinet_width"
    FILLER_WIDTH = "filler_width"
    SINK_CUTOUT_WIDTH = "sink_cutout_width"
    WALL_TO_WALL_DIMENSION = "wall_to_wall_dimension"

    WALL_CONFIG = "wall_config"
    FIELD_DIMENSION = "field_dimension"

    MATERIAL = "material"


class ProductType(str, Enum):
    """What kind of thing a rule checks — the client's checklist, in one word.

    A controlled vocabulary rather than a free string (ADR-0007). The applicability resolver
    matches this exactly and case-sensitively, so a free string would let ``"Countertop"`` or a
    typo publish cleanly and then match nothing: a rule that exists, looks authored, and never
    fires. Validation at publish turns that into a loud authoring error instead.

    Adding a product type is a one-line change here, which is the point of keeping the
    vocabulary in one module.
    """

    COUNTERTOP = "countertop"
    CABINET = "cabinet"


class OperandSource(str, Enum):
    """Where a rule operand comes from (see RULE_ENGINE_SPEC.md section 3e).

    The distinction that matters here is **what can be re-checked later**, not how
    authoritative the origin felt at the time. A value read from a hashed document can be
    read again in six months against the exact bytes it came from; a value someone typed
    cannot, however senior the person was.
    """

    ARCH = "ARCH"
    SHOP = "SHOP"
    LITERAL = "LITERAL"
    USER_INPUT = "USER_INPUT"

    PRODUCT_SPEC = "PRODUCT_SPEC"
    """Read from a manufacturer's specification document, ingested as a versioned, hashed
    artifact exactly like a drawing (ADR-0015).

    Sink interior dimensions come from here — the client's checklist points at the
    "production specification", and the worked example was a Kohler cut sheet.

    **Not** for a value supplied by a person. A dimension sent by email or read out on a
    call is `USER_INPUT`, even when the sender is the manufacturer. Labelling it
    `PRODUCT_SPEC` would let a remembered number carry a document's provenance, and a
    reviewer reading the finding would have no signal that the expected value could not be
    verified against anything.
    """


class DocumentRole(str, Enum):
    """Document-backed sources carried by canonical observations.

    Values derive from :class:`OperandSource` so both vocabularies remain an identity
    mapping rather than acquiring a translation layer.
    """

    ARCH = OperandSource.ARCH.value
    SHOP = OperandSource.SHOP.value
    PRODUCT_SPEC = OperandSource.PRODUCT_SPEC.value


class WallConfig(str, Enum):
    """Provisional wall layouts that drive rule applicability variants."""

    BACK_LEFT_RIGHT = "back_left_right"
    BACK_LEFT = "back_left"
    BACK_ONLY = "back_only"
    ISLAND = "island"


class Position(str, Enum):
    """Position carried by a client name without changing its semantic type."""

    LEFT = "left"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class DocumentName:
    """Meaning and provenance encoded by a prefixed client document name."""

    semantic_type: SemanticType
    source: OperandSource
    index: int | None = None
    position: Position | None = None


@dataclass(frozen=True, slots=True)
class ParameterName:
    """A client parameter name resolved later through parameter layers."""

    parameter: str


type VocabularyBinding = DocumentName | ParameterName


class UnknownVocabularyError(ValueError):
    """Raised when a client vocabulary name has no registered meaning."""


_PREFIX_SOURCES = {
    "A": OperandSource.ARCH,
    "S": OperandSource.SHOP,
    "F": OperandSource.USER_INPUT,
    "P": OperandSource.PRODUCT_SPEC,
}

#: Sources whose values come off a document we have hashed and pinned, so a finding drawn
#: from one can be re-checked against the exact bytes later. `LITERAL` and `USER_INPUT` are
#: deliberately absent: neither has a document behind it (ADR-0015).
DOCUMENT_BACKED_SOURCES: frozenset[OperandSource] = frozenset(
    {OperandSource.ARCH, OperandSource.SHOP, OperandSource.PRODUCT_SPEC}
)
_PARAMETERS = {
    "Filler_Width_Min",
    "Filler_Width_Max",
}
_DOCUMENT_NAMES = {
    "Wall_2_Wall_Dim": (SemanticType.WALL_TO_WALL_DIMENSION, None),
    "Filler_Left": (SemanticType.FILLER_WIDTH, Position.LEFT),
    "Filler_Right": (SemanticType.FILLER_WIDTH, Position.RIGHT),
}
_CABINET_RE = re.compile(r"CAB_(?P<index>\d+)")


def resolve(name: str) -> VocabularyBinding:
    """Resolve one exact client name without guessing or caller-side string matching."""

    if not isinstance(name, str):
        raise UnknownVocabularyError(f"vocabulary name must be text, got {name!r}")

    if name in _PARAMETERS:
        return ParameterName(parameter=name)

    prefix, separator, base_name = name.partition("_")
    source = _PREFIX_SOURCES.get(prefix)
    if not separator or source is None or not base_name:
        raise UnknownVocabularyError(f"unknown client vocabulary name: {name!r}")

    cabinet = _CABINET_RE.fullmatch(base_name)
    if cabinet is not None and source in {OperandSource.ARCH, OperandSource.SHOP}:
        return DocumentName(
            semantic_type=SemanticType.CABINET_WIDTH,
            source=source,
            index=int(cabinet.group("index")),
        )

    document = _DOCUMENT_NAMES.get(base_name)
    if document is not None:
        semantic_type, position = document
        if position is not None and source is OperandSource.USER_INPUT:
            raise UnknownVocabularyError(f"unknown client vocabulary name: {name!r}")
        return DocumentName(
            semantic_type=semantic_type,
            source=source,
            position=position,
        )

    raise UnknownVocabularyError(f"unknown client vocabulary name: {name!r}")
