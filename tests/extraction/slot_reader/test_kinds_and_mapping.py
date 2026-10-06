"""What a piece is, and which sealed readings reach the form (#987).

Verification for `extraction/slot_reader/kinds.py` and `extraction/slot_reader/mapping.py`.
Every value is invented.
"""

from __future__ import annotations

import inspect

import pytest

from extraction.slot_reader.kinds import KindProposal, PieceKind, propose_kind
from extraction.slot_reader.mapping import (
    CABINET_FIELD,
    FILLER_FIELD,
    OVERALL_FIELD,
    WHAT_IS_IT,
    PieceReading,
    map_row,
)
from extraction.slot_reader.seal import LabelState, OwnerOutcome, plain_dimension


def kind(*words: str, index: int = 1, count: int = 3, walls: frozenset = frozenset()) -> PieceKind:  # type: ignore[type-arg]
    return propose_kind(words, index=index, count=count, wall_ends=walls).kind


def test_a_printed_word_names_the_piece() -> None:
    assert kind('4"+1" Filler') is PieceKind.FILLER
    assert kind("FILLER PANEL") is PieceKind.FILLER
    assert kind("B24") is PieceKind.CABINET
    assert kind("DW") is PieceKind.APPLIANCE_SPACE
    assert kind("REF") is PieceKind.APPLIANCE_SPACE
    assert kind("RANGE") is PieceKind.APPLIANCE_SPACE
    assert kind('96"(6EQ)') is PieceKind.EQUAL_CABINETS
    assert kind("EQ") is PieceKind.EQUAL_CABINETS


def test_no_word_is_unknown_and_two_kinds_are_unknown() -> None:
    assert kind('2"') is PieceKind.UNKNOWN
    assert kind("") is PieceKind.UNKNOWN
    conflict = propose_kind(["Filler", "B24"], index=0, count=2, wall_ends=frozenset())
    assert conflict.kind is PieceKind.UNKNOWN and "two ways" in conflict.evidence
    # A finish code is not a cabinet tag.
    assert kind("WD-01") is PieceKind.UNKNOWN


def test_a_wall_end_names_only_the_end_piece() -> None:
    walls = frozenset({"left"})
    assert kind(index=0, walls=walls) is PieceKind.FILLER
    assert kind(index=1, walls=walls) is PieceKind.UNKNOWN
    assert kind(index=2, walls=frozenset({"right"})) is PieceKind.FILLER


def test_width_is_never_an_input_to_kind() -> None:
    assert set(inspect.signature(propose_kind).parameters) == {
        "words",
        "index",
        "count",
        "wall_ends",
    }


def sealed(text: str) -> OwnerOutcome:
    return OwnerOutcome(LabelState.SEALED, plain_dimension(text), 0, None, None)


REVIEW = OwnerOutcome(LabelState.REVIEW, None, None, "readers-differ", "readers differ")
FILLER = KindProposal(PieceKind.FILLER, 'printed word "Filler"')
CABINET = KindProposal(PieceKind.CABINET, 'cabinet tag "B24"')
UNKNOWN = KindProposal(PieceKind.UNKNOWN, "no printed word or wall names it")


def pieces(*items: tuple[OwnerOutcome, KindProposal]) -> list[PieceReading]:
    return [PieceReading(index, outcome, kind) for index, (outcome, kind) in enumerate(items)]


def test_a_whole_sealed_named_chain_is_offered_left_to_right() -> None:
    mapping = map_row(
        sealed('36"'),
        pieces(
            (sealed('1"'), FILLER),
            (sealed('15"'), CABINET),
            (sealed('18"'), CABINET),
            (sealed('2"'), FILLER),
        ),
        row_ambiguity=None,
    )
    assert [(p.field_key, p.position, p.slot_index) for p in mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (FILLER_FIELD, 0, 0),
        (CABINET_FIELD, 0, 1),
        (CABINET_FIELD, 1, 2),
        (FILLER_FIELD, 1, 3),
    ]
    assert mapping.held == ()


def test_an_unknown_piece_holds_every_piece_and_asks_what_it_is() -> None:
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('1"'), FILLER), (sealed('3/4"'), UNKNOWN), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    assert [p.field_key for p in mapping.proposals] == [OVERALL_FIELD]
    held = dict(mapping.held)
    assert held[1].startswith(WHAT_IS_IT)
    assert "piece 2" in held[0] and "piece 2" in held[2]


def test_a_missing_slot_reading_is_never_a_shorter_sum() -> None:
    """One slot unsealed: no piece is offered, so no saved form can add up a shorter chain."""
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('1"'), FILLER), (REVIEW, CABINET), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    assert [p.field_key for p in mapping.proposals] == [OVERALL_FIELD]
    assert {index for index, _ in mapping.held} == {0, 2}


def test_pieces_adding_up_never_changes_what_is_offered() -> None:
    """The same chain, once adding up to the overall and once not: identical proposals. Nothing
    here sums pieces, to pick a reading or to check one."""
    chain = pieces((sealed('1"'), FILLER), (sealed('18"'), CABINET), (sealed('1"'), FILLER))
    adds_up = map_row(sealed('20"'), chain, row_ambiguity=None)
    does_not = map_row(sealed('99"'), chain, row_ambiguity=None)
    assert adds_up.proposals == does_not.proposals and adds_up.held == does_not.held

    chain_unknown = pieces((sealed('1"'), FILLER), (sealed('18"'), UNKNOWN), (sealed('1"'), FILLER))
    assert (
        map_row(sealed('20"'), chain_unknown, row_ambiguity=None).proposals
        == map_row(sealed('99"'), chain_unknown, row_ambiguity=None).proposals
    )


def test_an_uncertain_row_offers_nothing() -> None:
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('18"'), CABINET), (sealed('18"'), CABINET)),
        row_ambiguity="another row on the page fits as well",
    )
    assert mapping.proposals == ()


def test_the_pieces_must_be_every_slot_in_order() -> None:
    with pytest.raises(ValueError):
        map_row(
            None,
            [PieceReading(1, sealed('2"'), CABINET)],
            row_ambiguity=None,
        )
