"""What a piece is — filler, cabinet, appliance space, equal cabinets — or that nobody knows (#987).

**The source of truth for a kind is what the drawing prints, or where the piece stands** (decided
2026-10-07): a word in or at the piece's label (`Filler`, `FILLER PANEL`, a cabinet tag, `DW`,
`REF`, `RANGE`, `EQ`), or the first or last piece of a row whose end meets a wall (Raj: a filler is
the strip between the wall and the cabinet). Anything else is **unknown**, and unknown is an honest
answer: the person names it on the screen (Phase 5).

**Never from the width, never from the reviewer's ink.** A 2" piece is not a filler because it is
2" — a cabinet can be that narrow and a filler wider — and the reviewer's red "filler" is the
reviewer's opinion, not the vendor's drawing. The words this is given are black or grey text
from the file or text two readers sealed; the caller never passes the reviewer's words.

**Kind never changes a width.** It only decides which form field a sealed width is offered to, and
an unknown piece is offered to none (`mapping.py`).

**A wall end needs evidence of a wall.** Neither client set draws a black or grey wall hatch at a
row's end, so no detector has anything to be measured on yet; the caller passes the wall ends it
knows of, which today is none (#987).

Source: issue #987 · Verification: `tests/extraction/slot_reader/test_kinds.py`
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

from vocabulary.cabinet_codes import is_cabinet_code

__all__ = ["KindProposal", "PieceKind", "WallEnd", "propose_kind"]


class PieceKind(StrEnum):
    FILLER = "filler"
    CABINET = "cabinet"
    APPLIANCE_SPACE = "appliance_space"
    EQUAL_CABINETS = "equal_cabinets"
    UNKNOWN = "unknown"


type WallEnd = Literal["left", "right"]

_TOKEN: Final = re.compile(r"[A-Za-z0-9]+(?:[-−][A-Za-z0-9]+)*")
_APPLIANCES: Final = frozenset({"DW", "REF", "RANGE"})
_EQUAL: Final = re.compile(r"[0-9]*EQ")


@dataclass(frozen=True, slots=True)
class KindProposal:
    kind: PieceKind
    evidence: str
    """In plain words: the word that named it, the wall it stands against, or why it is unknown."""


def _kinds_in(text: str) -> list[tuple[PieceKind, str]]:
    found: list[tuple[PieceKind, str]] = []
    for token in _TOKEN.findall(text):
        upper = token.upper()
        if upper == "FILLER":
            found.append((PieceKind.FILLER, f'printed word "{token}"'))
        elif upper in _APPLIANCES:
            found.append((PieceKind.APPLIANCE_SPACE, f'printed word "{token}"'))
        elif _EQUAL.fullmatch(upper):
            found.append((PieceKind.EQUAL_CABINETS, f'printed "{token}"'))
        elif is_cabinet_code(token):
            found.append((PieceKind.CABINET, f'cabinet tag "{token}"'))
    return found


def propose_kind(
    words: Iterable[str],
    *,
    index: int,
    count: int,
    wall_ends: frozenset[WallEnd],
) -> KindProposal:
    """The kind of piece `index` of `count` (left to right), from its printed words and wall ends.

    Two different kinds named for one piece → unknown, with both named: the drawing contradicts
    itself and the person decides.
    """
    if count <= 0 or not 0 <= index < count:
        raise ValueError("the piece must be one of the row's pieces")
    found: list[tuple[PieceKind, str]] = []
    for text in words:
        found.extend(_kinds_in(text))
    if index == 0 and "left" in wall_ends:
        found.append((PieceKind.FILLER, "first piece, against a wall"))
    if index == count - 1 and "right" in wall_ends:
        found.append((PieceKind.FILLER, "last piece, against a wall"))
    kinds = {kind for kind, _ in found}
    if not kinds:
        return KindProposal(PieceKind.UNKNOWN, "no printed word or wall names it")
    if len(kinds) > 1:
        named = "; ".join(sorted({evidence for _, evidence in found}))
        return KindProposal(PieceKind.UNKNOWN, f"the drawing names it two ways: {named}")
    kind = next(iter(kinds))
    evidence = "; ".join(dict.fromkeys(evidence for _, evidence in found))
    return KindProposal(kind, evidence)
