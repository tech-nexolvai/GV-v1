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


class OperandSource(str, Enum):
    """Where a rule operand comes from (see RULE_ENGINE_SPEC.md section 3e)."""

    ARCH = "ARCH"
    SHOP = "SHOP"
    LITERAL = "LITERAL"
    USER_INPUT = "USER_INPUT"


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
}
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
