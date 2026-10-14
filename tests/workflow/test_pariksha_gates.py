"""Pariksha's gates (#1228): Rekha's new row gate, the gate record, and the gates already enforced.

Synthetic sheets and values only. The Rekha tests read a hand-built drawing with the real slot
reader and fake readers, so "no later agent is asked" is the readers' own request log.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from typing import Any

import pytest
from sqlalchemy import select

import workflow.pariksha_run_check as run_check_module
from eval import release_gates
from extraction.geometry.rows import Box, LabelKind, OverallRow, SlotLabel, StoredBox
from tests.extraction.slot_reader import sheets
from tests.workflow.test_slot_reader import (
    OPUS,
    SONNET,
    FakeReaders,
    _scaffold,
    runtime,
    slot_page,
)
from workflow.pariksha_gates import (
    GATE_FLAG,
    REKHA_NO_NUMBERS,
    REKHA_NO_NUMBERS_REASON,
    REKHA_OVERALL_KEPT,
    REKHA_READ_THROUGH_KEPT,
    REKHA_ROW_GATE,
    Agent,
    GateDecision,
    GateResult,
    GateScope,
    gate_results_in,
    stopped_by_rekha,
)
from workflow.rekha_gate import CENTRED_SHARE, rekha_row_gate
from workflow.slot_reader import PageSlotResult, SlotPage, read_slot_pages

pytest_plugins = ("tests.app.postgres_fixture",)


# --- sheets ----------------------------------------------------------------------------------------


def _number_sheet() -> bytes:
    """A chain whose three pieces each print a dimension at their middle (invented numbers)."""
    return sheets.sheet(sheets.text_labels(('12"', '24"', '12"'), '48"'))


def _tag_sheet() -> bytes:
    """The same chain with only material tags on it: letters and digits, no inch mark."""
    body = b"".join(sheets.text(x - 9, sheets.CHAIN_Y + 4, "XQ-07") for x in sheets.SLOT_CENTRES)
    return sheets.sheet(body + sheets.text(241, sheets.OVERALL_Y + 4, "ZZ-01"))


def _off_centre_glyph_sheet() -> bytes:
    """Glyph runs that stand well to one side of every piece, as a neighbour's numbers do."""
    body = b"".join(sheets.glyphs(x - 4, sheets.CHAIN_Y + 3) for x in (155.0, 215.0, 335.0))
    return sheets.sheet(body + sheets.glyphs(156, sheets.OVERALL_Y + 3))


def _claude(readers: FakeReaders) -> Any:
    return replace(runtime(readers, claude_row_reader=True))


def _read(page: SlotPage, readers: FakeReaders) -> PageSlotResult:
    (result,) = read_slot_pages(
        [page], runtime=_claude(readers), record_attempt=lambda _attempt: None
    )
    return result


def _agreeing_readers() -> FakeReaders:
    return FakeReaders(
        lambda _model, _png: '12"',
        row=lambda _model: {"row": 1, "kind": "row", "also": [], "why": "synthetic: line 1"},
    )


# --- the gate record ---------------------------------------------------------------------------


def test_a_gate_result_round_trips_through_its_flag() -> None:
    result = GateResult(
        Agent.REKHA,
        REKHA_ROW_GATE,
        GateScope.ROW,
        GateDecision.STOP,
        REKHA_NO_NUMBERS,
        REKHA_NO_NUMBERS_REASON,
        "3",
    )

    assert result.flag == f"{GATE_FLAG}rekha:{REKHA_ROW_GATE}:stop:{REKHA_NO_NUMBERS}"
    (read,) = gate_results_in(["slot:0", result.flag], scope=GateScope.ROW, scope_id="3")
    assert read == result
    assert stopped_by_rekha([result.flag])
    assert result.note.startswith("Pariksha gate (Rekha, row-has-printed-numbers): stop")


def test_a_malformed_gate_flag_is_skipped_never_a_crash() -> None:
    flags = [
        f"{GATE_FLAG}rekha:only-two",
        f"{GATE_FLAG}nobody:g:stop:x",
        f"{GATE_FLAG}rekha:g:maybe:x",
    ]

    assert gate_results_in(flags, scope=GateScope.ROW) == ()
    assert not stopped_by_rekha(flags)


def test_a_gate_result_needs_its_reason() -> None:
    with pytest.raises(ValueError, match="reason"):
        GateResult(Agent.NINA, "safety-rules", GateScope.FINDING, GateDecision.STOP, "x", " ")


def test_the_run_checks_contract_gates_keep_the_release_gates_names() -> None:
    """Production may not import `eval/`; the names are copied and must stay the same."""
    assert run_check_module.OCR_GATE == release_gates.OCR_GATE
    assert run_check_module.UNKNOWN_UNIT_GATE == release_gates.UNKNOWN_UNIT_GATE
    assert run_check_module.MISSING_SOURCE_GATE == release_gates.MISSING_SOURCE_GATE
    assert run_check_module.ADVISORY_GATE == release_gates.ADVISORY_GATE


# --- Rekha's rule on its own ---------------------------------------------------------------------


def _row(page: SlotPage) -> Any:
    return page.rows.candidates.rows.candidates[0]


def test_a_row_with_printed_numbers_at_its_pieces_passes() -> None:
    gate = rekha_row_gate(_row(slot_page(_number_sheet())), page_number=1)

    assert gate.decision is GateDecision.PASS
    assert (gate.agent, gate.gate, gate.scope_id) == (Agent.REKHA, REKHA_ROW_GATE, "1")


def test_a_row_of_material_tags_is_stopped_with_the_plain_reason() -> None:
    row = _row(slot_page(_tag_sheet()))
    assert row.labelled == len(row.slots), "the row builder sees the tags as labels"

    gate = rekha_row_gate(row)

    assert gate.decision is GateDecision.STOP
    assert gate.code == REKHA_NO_NUMBERS
    assert gate.reason == "The chosen line has no printed numbers; the reviewer picks the row."


def test_numbers_that_stand_off_to_one_side_of_every_piece_are_not_the_rows() -> None:
    row = _row(slot_page(_off_centre_glyph_sheet()))
    assert all(label.kind is LabelKind.GLYPHS for slot in row.slots for label in slot.labels)

    assert rekha_row_gate(row).decision is GateDecision.STOP


def _label(middle: Decimal, *, text: str | None = None, half: Decimal = Decimal(1)) -> SlotLabel:
    """A label whose box's middle is `middle` (page points)."""
    box = Box(middle - half, Decimal(590), middle + half, Decimal(596))
    stored = StoredBox(left=Decimal(0), top=Decimal(0), right=Decimal(1), bottom=Decimal(1))
    if text is None:
        return SlotLabel(LabelKind.GLYPHS, box, stored, (), 3, False, False, Decimal(2))
    return SlotLabel(LabelKind.TEXT, box, stored, (text,), 0, False, False, Decimal(2))


def _only_first_piece(label: SlotLabel) -> Any:
    row = _row(slot_page(_number_sheet()))
    first = row.slots[0]
    return replace(row, overall=None, slots=(replace(first, labels=(label,)),))


def test_the_centre_rule_is_exact_at_its_edge() -> None:
    first = _row(slot_page(_number_sheet())).slots[0]
    middle = (first.x0 + first.x1) / 2
    edge = Decimal(11) / Decimal(100) * (first.x1 - first.x0)
    assert Fraction(edge) == CENTRED_SHARE * Fraction(first.x1 - first.x0)

    on_edge = rekha_row_gate(_only_first_piece(_label(middle + edge)))
    past_edge = rekha_row_gate(_only_first_piece(_label(middle + edge + Decimal("0.01"))))

    assert on_edge.decision is GateDecision.PASS
    assert past_edge.decision is GateDecision.STOP


@pytest.mark.parametrize(
    ("text", "number"),
    [
        ('24"', True),
        ("610", True),
        ("[24 1/2]", True),
        ('2" BLACK', True),
        ("[30]VIF", True),
        ("XQ-07", False),
        ("W3030", False),
        ("ELEVATION", False),
    ],
)
def test_text_with_letters_is_a_code_unless_it_carries_a_dimension_mark(
    text: str, number: bool
) -> None:
    first = _row(slot_page(_number_sheet())).slots[0]
    label = _label((first.x0 + first.x1) / 2, text=text)

    expected = GateDecision.PASS if number else GateDecision.STOP
    assert rekha_row_gate(_only_first_piece(label)).decision is expected


def _sliver_band() -> Any:
    """A material-tag band with one sliver piece, as both readers chose on an acceptance set
    (#1230 review): the tags stand over the wide pieces; over the 2.4 pt sliver stands a stroke
    3 pt wide, its middle 0.2 pt from the sliver's (well inside 11% of 2.4 pt)."""
    row = _row(slot_page(_number_sheet()))
    first, second, third = row.slots
    sliver = replace(
        second,
        x0=second.x0,
        x1=second.x0 + Decimal("2.4"),
        labels=(_label(second.x0 + Decimal("1.4"), half=Decimal("1.5")),),
    )
    tags = [
        replace(slot, labels=(_label((slot.x0 + slot.x1) / 2, text="XQ-07"),))
        for slot in (first, third)
    ]
    return replace(row, overall=None, slots=(tags[0], sliver, tags[1]))


def test_a_tag_band_is_not_passed_by_a_stroke_over_a_sliver_piece() -> None:
    gate = rekha_row_gate(_sliver_band())

    assert gate.decision is GateDecision.STOP
    assert gate.code == REKHA_NO_NUMBERS


def test_a_label_wider_than_its_sliver_piece_never_counts_but_the_others_still_do() -> None:
    band = _sliver_band()
    first, sliver, third = band.slots
    numbered = replace(third, labels=(_label((third.x0 + third.x1) / 2, text='12"'),))

    gate = rekha_row_gate(replace(band, slots=(first, sliver, numbered)))

    assert gate.decision is GateDecision.PASS
    narrow = replace(sliver, labels=(_label(sliver.x0 + Decimal("1.2"), half=Decimal("1.0")),))
    assert rekha_row_gate(replace(band, slots=(first, narrow, third))).decision is GateDecision.PASS


def test_an_overall_with_a_printed_number_is_recorded_not_stopped() -> None:
    """No information is lost: the overall's reading is offered today."""
    row = _row(slot_page(_number_sheet()))
    assert row.overall is not None
    tags = tuple(
        replace(slot, labels=(_label((slot.x0 + slot.x1) / 2, text="XQ-07"),)) for slot in row.slots
    )
    overall = replace(row.overall, labels=(_label((row.overall.x0 + row.overall.x1) / 2),))

    gate = rekha_row_gate(replace(row, slots=tags, overall=overall))

    assert gate.decision is GateDecision.RECORDED
    assert gate.code == REKHA_OVERALL_KEPT
    assert not gate.stopped


def test_a_row_the_equal_shares_line_could_read_is_recorded_not_stopped() -> None:
    gate = rekha_row_gate(_row(slot_page(_tag_sheet())), read_through_possible=True)

    assert gate.decision is GateDecision.RECORDED
    assert gate.code == REKHA_READ_THROUGH_KEPT


def test_the_overall_type_has_the_labels_the_gate_reads() -> None:
    assert "labels" in OverallRow.__dataclass_fields__


# --- Rekha's gate in the reader ------------------------------------------------------------------


def test_a_good_row_is_handed_on_and_read() -> None:
    readers = _agreeing_readers()

    result = _read(slot_page(_number_sheet()), readers)

    assert result.row_gate is not None and result.row_gate.decision is GateDecision.PASS
    assert result.plan.row is not None and result.row_choice_number == 1
    assert readers.requests, "the labels are asked"
    assert readers.wall_requests and readers.counter_break_requests


def test_a_stopped_row_asks_no_label_walls_or_counter_break_question() -> None:
    readers = _agreeing_readers()

    result = _read(slot_page(_tag_sheet()), readers)

    assert result.row_gate is not None and result.row_gate.stopped
    assert result.plan.row is None
    assert result.slots == () and result.overall is None
    assert result.row_choice_number == 0
    assert result.row_choice is not None
    assert result.row_choice.why.startswith(REKHA_NO_NUMBERS_REASON)
    assert sorted(model for model, _ in readers.row_requests) == sorted([OPUS, SONNET])
    assert readers.requests == []
    assert readers.wall_requests == []
    assert readers.counter_break_requests == []
    assert result.row_choice_picks == ((OPUS, 1), (SONNET, 1)), "the readers' picks are kept"


def test_without_the_row_readers_there_is_no_row_gate() -> None:
    readers = FakeReaders(lambda _model, _png: '12"')
    (result,) = read_slot_pages(
        [slot_page(_tag_sheet())], runtime=runtime(readers), record_attempt=lambda _a: None
    )

    assert result.row_gate is None


# --- recorded, and reaching the reviewer's "pick the row" flow ------------------------------------


@pytest.fixture
def session(postgres_engine: Any) -> Iterator[Any]:
    from alembic import command
    from app.db.session import session_factory
    from tests.app.postgres_fixture import alembic_config

    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _read_and_persist(session: Any, data: bytes) -> tuple[Any, Any, PageSlotResult]:
    from workflow.slot_reader import persist_slot_readings

    revision, version, page_row, run = _scaffold(session)
    page = replace(slot_page(data), page_id=page_row.id, document_version_id=version.id)
    result = _read(page, _agreeing_readers())
    persist_slot_readings(
        session,
        package_revision_id=revision.id,
        extraction_run_id=run.id,
        reader_ids=(OPUS, SONNET),
        results=[result],
    )
    return revision, run, result


def test_a_stopped_row_is_kept_as_a_page_the_reviewer_picks_the_row_on(session: Any) -> None:
    from app.models.evidence import ObservationCandidate
    from workflow.slot_row_scope import slot_rows_and_unchosen_pages

    revision, run, result = _read_and_persist(session, _tag_sheet())

    record = session.scalar(
        select(ObservationCandidate).where(
            ObservationCandidate.extraction_run_id == run.id,
            ObservationCandidate.ambiguity_flags.contains(["slot-reader-row-choice"]),
        )
    )
    assert record is not None
    assert result.row_gate is not None and result.row_gate.flag in record.ambiguity_flags
    assert "row-choice:0" in record.ambiguity_flags
    assert {"row-pick:opus-5-5:1", "row-pick:sonnet-5-5:1"} <= set(record.ambiguity_flags)
    assert record.review_reason.startswith(REKHA_NO_NUMBERS_REASON)
    rows, (page,) = slot_rows_and_unchosen_pages(session, revision.id)
    assert rows == ()
    assert page.split, "it blocks sign-off like a page the readers split on"
    assert page.reason.startswith(REKHA_NO_NUMBERS_REASON)
    assert "Both AIs chose line 1" in page.reason
    assert "on the Measurements screen" in page.reason, "the reason says where to act"
    assert "Pariksha" not in page.reason and "code sees" not in page.reason


def test_a_passed_row_keeps_its_gate_result_on_its_anchor(session: Any) -> None:
    from workflow.slot_row_scope import slot_rows_and_unchosen_pages

    revision, _run, result = _read_and_persist(session, _number_sheet())

    (row,), unchosen = slot_rows_and_unchosen_pages(session, revision.id)
    assert unchosen == ()
    assert result.row_gate is not None
    assert result.row_gate.flag in row.anchor.ambiguity_flags
    others = [candidate for candidate in row.candidates if candidate.id != row.anchor.id]
    assert all(
        not any(flag.startswith(GATE_FLAG) for flag in candidate.ambiguity_flags or ())
        for candidate in others
    ), "kept once per row"


def test_the_run_check_counts_each_agents_gate_results(session: Any) -> None:
    from workflow.pariksha_run_check import gate_tally

    revision, _run, _result = _read_and_persist(session, _tag_sheet())

    counts: Mapping[tuple[str, str], tuple[int, int, int]] = {
        (count.agent.value, count.gate): (count.passed, count.stopped, count.recorded)
        for count in gate_tally(session, revision.id)
    }
    assert counts[("Bala", "every-page-accounted")] == (1, 0, 0)
    assert counts[("Rekha", "two-readers-agree")] == (1, 0, 0)
    assert counts[("Rekha", REKHA_ROW_GATE)] == (0, 1, 0)
    assert ("Akshara", "seal") not in counts, "nothing was read on the stopped row"
