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
    PIECE_FIELD,
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


def test_a_whole_sealed_named_chain_is_offered_as_piece_widths_only() -> None:
    """CT-WIDTH-001 (#993) sends piece widths beside a cabinet or filler list to review, so a row
    offered as pieces is never offered to the kind fields too."""
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
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
        (PIECE_FIELD, 3, 3),
    ]
    assert mapping.held == ()


def test_a_named_chain_without_a_sealed_overall_still_fills_the_kind_fields() -> None:
    mapping = map_row(
        None,
        pieces((sealed('1"'), FILLER), (sealed('15"'), CABINET), (sealed('2"'), FILLER)),
        row_ambiguity=None,
    )
    assert [(p.field_key, p.position, p.slot_index) for p in mapping.proposals] == [
        (FILLER_FIELD, 0, 0),
        (CABINET_FIELD, 0, 1),
        (FILLER_FIELD, 1, 2),
    ]


def test_a_fully_sealed_row_offers_every_piece_whatever_its_kind() -> None:
    """#992: the countertop's width sums every piece of its row, so kinds no longer gate it. An
    unknown piece still keeps the kind fields back: those go only as a whole named chain."""
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('1"'), FILLER), (sealed('3/4"'), UNKNOWN), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    assert [(p.field_key, p.position, p.slot_index) for p in mapping.proposals] == [
        (OVERALL_FIELD, 0, None),
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 1, 1),
        (PIECE_FIELD, 2, 2),
    ]
    assert mapping.held == ()


def test_non_claude_reader_keeps_the_legacy_hold_for_an_unknown_piece() -> None:
    mapping = map_row(
        None,
        pieces((sealed('1"'), FILLER), (sealed('3/4"'), UNKNOWN), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    assert mapping.proposals == ()
    assert 1 in {index for index, _ in mapping.held}


def test_non_claude_reader_keeps_the_legacy_all_or_nothing_chain() -> None:
    """Partial proposals are reserved for the explicitly enabled Claude reader."""
    mapping = map_row(
        sealed('120"'),
        pieces((sealed('24"'), CABINET), (REVIEW, UNKNOWN), (sealed('24"'), CABINET)),
        row_ambiguity=None,
    )
    assert mapping.proposals == ()
    assert dict(mapping.held)[None].startswith("held back: piece 2 under this overall")
    assert 1 not in dict(mapping.held), "unsealed readings already carry their own review reason"


def test_no_piece_widths_without_a_sealed_overall() -> None:
    mapping = map_row(
        REVIEW,
        pieces((sealed('1"'), FILLER), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    keys = {p.field_key for p in mapping.proposals}
    assert PIECE_FIELD not in keys and OVERALL_FIELD not in keys


def test_a_held_row_offers_nothing_and_says_why() -> None:
    reason = "width already includes the field cut"
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('18"'), CABINET), (sealed('18"'), CABINET)),
        row_ambiguity=None,
        row_hold=reason,
    )
    assert mapping.proposals == ()
    assert dict(mapping.held) == {None: reason, 0: reason, 1: reason}


def test_claude_reader_proposes_sealed_pieces_in_their_original_positions() -> None:
    """A useful proposal is shown, but the missing neighbour cannot shift the later width left."""
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('1"'), FILLER), (REVIEW, CABINET), (sealed('18"'), CABINET)),
        row_ambiguity=None,
        allow_partial_proposals=True,
    )
    assert [
        (proposal.field_key, proposal.position, proposal.slot_index)
        for proposal in mapping.proposals
    ] == [
        (PIECE_FIELD, 0, 0),
        (PIECE_FIELD, 2, 2),
    ]
    assert {index for index, _ in mapping.held} == {None, 1}


def test_partial_proposals_are_not_enabled_by_default() -> None:
    mapping = map_row(
        sealed('36"'),
        pieces((sealed('1"'), FILLER), (REVIEW, CABINET), (sealed('18"'), CABINET)),
        row_ambiguity=None,
    )
    assert mapping.proposals == ()


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
