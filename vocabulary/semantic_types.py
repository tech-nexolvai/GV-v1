"""Single source of truth for semantic types and client vocabulary names.

Every rule and observation must reference these constants instead of hard-coded
strings, so a vocabulary change remains local to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

#: The vocabulary is **provisional** until Q20 (#16) confirms the codes are final.
#:
#: The client's workbook carries two naming schemes side by side — letters A–G on Sheet1 and
#: `CT0xx` on the variable sheet. ADR-0017 adopts `CT0xx`; Q20 confirms he has not renamed them.
#: A rename lands as an alias entry rather than a schema migration, which is why the structure can
#: ship ahead of the confirmation.
VOCABULARY_STATUS = "PROVISIONAL — pending Q20 (#16) confirmation"


class SemanticType(str, Enum):
    """Canonical meanings, named in the client's own codes (ADR-0017).

    The codes win over our descriptive names because they are **anchored to a diagram**.
    `CT_image10` defines each one positionally on an annotated drawing, and prose about geometry is
    where two readers diverge: "countertop width" is unambiguous until somebody asks whether it
    includes the end panel.

    Where one of our names maps to exactly one code it is declared here as a Python enum alias, so
    `SemanticType.SINK_CUTOUT_WIDTH is SemanticType.CT012` and existing rules keep resolving.
    Where the client's code carries a **position** that our generic name does not, no alias is
    declared — see `CABINET_WIDTH` below.
    """

    # -- the run, left to right (CT_image10, top row) ------------------------
    CT001 = "CT001"
    CT002 = "CT002"
    CT003 = "CT003"
    CT004 = "CT004"
    CT005 = "CT005"
    CT006 = "CT006"

    # -- the sink, front to back and left to right ---------------------------
    CT007 = "CT007"
    CT008 = "CT008"
    CT009 = "CT009"
    CT010 = "CT010"
    CT011 = "CT011"
    CT012 = "CT012"
    CT013 = "CT013"

    # -- named variables the workbook uses alongside the codes ---------------
    BACKSPLASH_THICKNESS = "B.S_THK"
    COUNTERTOP_OVERHANG = "C.T_OH"
    CABINET_SIDE_THICKNESS = "CAB_SIDE_THK"

    # -- our descriptive names, as true aliases where the mapping is 1:1 -----
    # The repeated value IS the mechanism: Python makes the second name an alias of the first, so
    # `SemanticType.SINK_CUTOUT_WIDTH is SemanticType.CT012` and a rule written with either name
    # matches evidence tagged with the other. Two parallel members would compare unequal, which is
    # the bug this avoids. PIE796 flags the duplication; here it is intended.
    WALL_TO_WALL_DIMENSION = "CT001"  # noqa: PIE796
    SINK_CUTOUT_WIDTH = "CT012"  # noqa: PIE796
    COUNTERTOP_DEPTH = "CT010"  # noqa: PIE796
    SINK_OFFSET_FRONT = "CT007"  # noqa: PIE796
    SINK_CUTOUT_DEPTH = "CT008"  # noqa: PIE796
    SINK_OFFSET_BACK = "CT009"  # noqa: PIE796

    # -- generic types the client expresses positionally ---------------------
    # `CABINET_WIDTH` is deliberately NOT an alias of CT003. The client has three positional codes
    # — CT003 left, CT004 sink cabinet, CT005 right — for a three-cabinet layout, while our type is
    # generic and carries the position in an index (`S_CAB_7`). Aliasing one to the other would
    # silently claim every cabinet is the left one.
    CABINET_WIDTH = "cabinet_width"
    FILLER_WIDTH = "filler_width"
    COUNTERTOP_OVERALL_WIDTH = "countertop_overall_width"

    # -- no client code exists for these -------------------------------------
    WALL_CONFIG = "wall_config"
    FIELD_DIMENSION = "field_dimension"
    MATERIAL = "material"

    def label(self) -> str:
        """The plain-English name, for reports and error messages.

        A rule file reading `CT007 - CT008` is unreadable without the diagram to hand, and the
        people debugging a failed check are usually us. This is what keeps the codes usable.
        """
        described = CLIENT_CODES.get(self.value)
        return described.description if described else self.value.replace("_", " ")


@dataclass(frozen=True, slots=True)
class ClientCode:
    """One client code, what it measures, and where that is defined.

    `anchor` matters as much as `description`. Every one of these is defined **positionally on a
    drawing**, not in prose, and the anchor is what a reviewer opens to check a rule against the
    thing it is supposed to measure.
    """

    code: str
    description: str
    descriptive_alias: str | None
    anchor: str


#: Every code the client's workbook uses, with the diagram that defines it.
#:
#: `CT011`–`CT013` are here on the strength of `CT_image10` alone — they appear in formula `F38`
#: but never as rows in the variable table. ADR-0017 originally excluded them for that reason and
#: was **wrong**: the diagram defines them as clearly as the rest. Reading the summary instead of
#: the image is the recurring failure with this client's material.
CLIENT_CODES: dict[str, ClientCode] = {
    "CT001": ClientCode("CT001", "wall to wall dimension", "wall_to_wall_dimension", "CT_image10"),
    "CT002": ClientCode("CT002", "cabinet filler, left", "filler_width (left)", "CT_image10"),
    "CT003": ClientCode("CT003", "cabinet 1 width, left cabinet underneath", None, "CT_image10"),
    "CT004": ClientCode("CT004", "cabinet 2 width, sink cabinet underneath", None, "CT_image10"),
    "CT005": ClientCode("CT005", "cabinet 3 width, right cabinet underneath", None, "CT_image10"),
    "CT006": ClientCode("CT006", "cabinet filler, right", "filler_width (right)", "CT_image10"),
    "CT007": ClientCode("CT007", "sink front offset", "sink_offset_front", "CT_image10"),
    "CT008": ClientCode("CT008", "sink hole depth", "sink_cutout_depth", "CT_image10"),
    "CT009": ClientCode("CT009", "sink back offset", "sink_offset_back", "CT_image10"),
    "CT010": ClientCode("CT010", "countertop depth", "countertop_depth", "CT_image10"),
    "CT011": ClientCode(
        "CT011",
        "clearance from the sink cabinet's left interior face to the cutout",
        None,
        "CT_image10",
    ),
    "CT012": ClientCode("CT012", "sink hole width", "sink_cutout_width", "CT_image10"),
    "CT013": ClientCode(
        "CT013",
        "clearance from the sink cabinet's right interior face to the cutout",
        None,
        "CT_image10",
    ),
    "B.S_THK": ClientCode("B.S_THK", "backsplash thickness", "backsplash_thickness", "workbook"),
    "C.T_OH": ClientCode("C.T_OH", "countertop overhang", "countertop_overhang", "workbook"),
    "CAB_SIDE_THK": ClientCode(
        "CAB_SIDE_THK", "cabinet side panel thickness", "cabinet_side_thickness", "workbook"
    ),
}


#: Lookup tables built once. `__members__` rather than iteration, because iterating an enum yields
#: only canonical members and would drop every descriptive alias — the half of this vocabulary that
#: exists so a rule written with either name resolves.
#:
#: Built as explicit maps rather than caught `ValueError`/`KeyError`: `.semgrep/gv-rules.yaml`
#: forbids a swallowed exception in the decision path, and it is right to. The behaviour here would
#: have been safe today, but a `try/except: pass` is one added return statement away from hiding a
#: failure, and this module feeds rule selection.
_BY_VALUE: dict[str, SemanticType] = {t.value: t for t in SemanticType}
_BY_NAME: dict[str, SemanticType] = dict(SemanticType.__members__)


def resolve_semantic(name: str) -> SemanticType:
    """Look a semantic type up by the client's code **or** by our descriptive name.

    Raises rather than guessing. A name we do not recognise is a rule referring to something that
    does not exist, and resolving it to the nearest match would let that rule publish and then
    check the wrong quantity.
    """
    text = name.strip()
    for candidate in (text, text.upper(), text.lower()):
        by_value = _BY_VALUE.get(candidate)
        if by_value is not None:
            return by_value
        by_name = _BY_NAME.get(candidate.upper().replace(".", "_").replace(" ", "_"))
        if by_name is not None:
            return by_name
    raise UnknownVocabularyError(
        f"unknown semantic type {name!r}. Known: the client's codes ({', '.join(sorted(CLIENT_CODES))}) "
        "or our descriptive aliases. A code the client has not defined must not be invented — "
        "see ADR-0017."
    )


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


#: The public surface. Explicit rather than a wildcard, because `rules/semantic_types.py`
#: re-exports from here and a name that silently stopped being exported would break importers
#: that never mentioned this module.
__all__ = [
    "CLIENT_CODES",
    "DOCUMENT_BACKED_SOURCES",
    "VOCABULARY_STATUS",
    "ClientCode",
    "DocumentName",
    "DocumentRole",
    "OperandSource",
    "ParameterName",
    "Position",
    "ProductType",
    "SemanticType",
    "UnknownVocabularyError",
    "WallConfig",
    "resolve",
    "resolve_semantic",
]
