"""Verification for issue #45: cabinet and field client vocabulary bindings.

Source: ``Cabinet_Checks.xlsx`` and the administrator decisions on issue #45.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from rules.semantic_types import (
    DocumentName,
    OperandSource,
    ParameterName,
    Position,
    SemanticType,
    UnknownVocabularyError,
    resolve,
)


@pytest.mark.parametrize(
    ("name", "source"),
    [
        ("A_Wall_2_Wall_Dim", OperandSource.ARCH),
        ("S_Wall_2_Wall_Dim", OperandSource.SHOP),
        ("F_Wall_2_Wall_Dim", OperandSource.USER_INPUT),
    ],
)
def test_wall_to_wall_prefix_maps_to_document_role(name: str, source: OperandSource) -> None:
    """The vocabulary module alone maps A, S and F prefixes to provenance."""

    assert resolve(name) == DocumentName(
        semantic_type=SemanticType.WALL_TO_WALL_DIMENSION,
        source=source,
    )


@pytest.mark.parametrize(
    ("name", "source", "index"),
    [
        ("A_CAB_1", OperandSource.ARCH, 1),
        ("S_CAB_7", OperandSource.SHOP, 7),
        ("A_CAB_8", OperandSource.ARCH, 8),
        ("S_CAB_999", OperandSource.SHOP, 999),
    ],
)
def test_cabinet_index_is_unbounded_instance_metadata(
    name: str, source: OperandSource, index: int
) -> None:
    """Every cabinet shares one semantic type while preserving its integer suffix."""

    assert resolve(name) == DocumentName(
        semantic_type=SemanticType.CABINET_WIDTH,
        source=source,
        index=index,
    )


@pytest.mark.parametrize(
    ("name", "source", "position"),
    [
        ("A_Filler_Left", OperandSource.ARCH, Position.LEFT),
        ("A_Filler_Right", OperandSource.ARCH, Position.RIGHT),
        ("S_Filler_Left", OperandSource.SHOP, Position.LEFT),
        ("S_Filler_Right", OperandSource.SHOP, Position.RIGHT),
    ],
)
def test_filler_side_is_position_not_a_distinct_semantic_type(
    name: str, source: OperandSource, position: Position
) -> None:
    """Aggregate rules can select every filler through FILLER_WIDTH."""

    assert resolve(name) == DocumentName(
        semantic_type=SemanticType.FILLER_WIDTH,
        source=source,
        position=position,
    )


@pytest.mark.parametrize("name", ["Filler_Width_Min", "Filler_Width_Max"])
def test_filler_limits_are_layered_parameters_without_operand_source(name: str) -> None:
    """Parameter provenance comes from its layer, never a document-source fiction."""

    binding = resolve(name)

    assert binding == ParameterName(parameter=name)
    assert not hasattr(binding, "source")


@pytest.mark.parametrize(
    "name",
    [
        "",
        "X_Wall_2_Wall_Dim",
        "A_Unknown",
        "A_CAB_",
        "A_CAB_three",
        "A_CAB_-1",
        "A_CAB_3_extra",
        "F_CAB_3",
        "F_Filler_Left",
        "Filler_Width_Average",
    ],
)
def test_unknown_or_malformed_names_raise_without_guessing(name: str) -> None:
    """Partial resemblance never becomes a vocabulary binding."""

    with pytest.raises(UnknownVocabularyError):
        resolve(name)


@pytest.mark.parametrize("value", [None, 3, object()])
def test_non_text_names_raise_typed_vocabulary_error(value: object) -> None:
    """All unresolved boundary inputs use the module's typed abstention error."""

    with pytest.raises(UnknownVocabularyError):
        resolve(value)  # type: ignore[arg-type]


def test_bindings_are_immutable() -> None:
    """Resolved meaning and provenance cannot change after construction."""

    document = resolve("A_CAB_3")
    parameter = resolve("Filler_Width_Min")

    assert isinstance(document, DocumentName)
    assert isinstance(parameter, ParameterName)
    with pytest.raises(FrozenInstanceError):
        document.index = 4  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        parameter.parameter = "Filler_Width_Max"  # type: ignore[misc]
