"""Code pre-fills "cabinet" where exactly one cabinet tag is printed on a piece (#1266, P5-K1).

Verification for `workflow/bala_items.py` (`_printed_tag`, `_kind`, `cabinet_prefill`) and the
plumbing that feeds it (`workflow/stages.py:_printed_texts`). Anant's decision 5 of 2026-10-14:
code pre-fills, a person still clicks once, and the switch (`GV_BALA_CABINET_PREFILL`) stays off
until the piece-kind scorecard shows 0 wrong.

**Every tag here is made up** (`B27`, `SB33`, `DW21`, ...): shaped like a cabinet code
(`vocabulary/cabinet_codes.py`) and naming nothing real. Code's part finder is replaced by a
`CodeFinding` built by hand, so each test states exactly what the finder read; the rows are the
synthetic slot-reader rows of `tests/workflow/test_bala_items.py` (pieces 200 px wide from x = 200,
their labels at y = 800-860 px, on a 1275 x 1650 px page).

**The outcome that matters most:** a tag above the run that is a wall cabinet's (one printed higher
than the row's own tags) never pre-fills anything, and nothing here ever writes an item.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.db.session import session_factory
from app.models import DrawingItem, ObservationCandidate, Page, PartProposal, PartProposalRow
from app.models.runs import ExtractionRun, TaskRun
from tests.workflow.test_bala_items import HEIGHT_PX, WIDTH_PX, _Revision
from tests.workflow.test_part_proposals_route import _upgrade
from vocabulary.part_kinds import PartKind
from workflow.bala_items import (
    PRINTED_ON_IT,
    TAG_NAMES_IT,
    CodeFinding,
    CodePart,
    PlannedPart,
    PrintedCode,
    plan_items,
    propose_items,
)
from workflow.stages import _printed_texts

pytest_plugins = ("tests.app.postgres_fixture",)

TOLERANCE = Decimal("0.004")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


# -- what code's part finder read, built by hand ----------------------------------------------------


def _x(px: int) -> Decimal:
    return Decimal(px) / WIDTH_PX


def _y(px: int) -> Decimal:
    return Decimal(px) / HEIGHT_PX


def _tag(text: str, piece: int, top_px: int, *, height_px: int = 20) -> PrintedCode:
    """A tag printed over piece `piece`, wholly inside its span, `top_px` down the page."""
    left = 200 + 200 * piece + 60
    return PrintedCode(
        text=text, left=_x(left), top=_y(top_px), right=_x(left + 80), bottom=_y(top_px + height_px)
    )


def _part(view: UUID, piece: int, code: PrintedCode | None, *, shift_px: int = 0) -> CodePart:
    """Code's cabinet with the piece's two ends (or `shift_px` off them), and its attached code."""
    return CodePart(
        view_id=view,
        kind=PartKind.CABINET,
        left=_x(200 + 200 * piece + shift_px),
        right=_x(400 + 200 * piece + shift_px),
        code=code,
    )


def _finding(*parts: CodePart, also: tuple[PrintedCode, ...] = ()) -> CodeFinding:
    """The finding: its parts, and every code printed on the page (the attached ones and `also`)."""
    attached = tuple(part.code for part in parts if part.code is not None)
    return CodeFinding(parts=parts, tolerance=TOLERANCE, codes=attached + also)


def _pieces(
    session: Session, built: _Revision, finding: CodeFinding | str, *, prefill: bool = True
) -> dict[int, PlannedPart]:
    plan = plan_items(
        session, built.revision.id, code_parts=lambda _page: finding, cabinet_prefill=prefill
    )
    (row,) = plan.rows
    return {part.slot_index: part for part in row.parts if part.slot_index is not None}


# -- the pre-fill -----------------------------------------------------------------------------------


def _beside(piece: int, *, top_px: int = 700) -> PrintedCode:
    """Another base cabinet's tag over the row, in the same band: the row's own tags are printed
    at one height, and a lone tag is refused (`test_a_lone_tag_over_the_row_names_nothing`)."""
    return _tag("B21", piece, top_px)


def test_one_tag_on_a_piece_with_the_same_ends_pre_fills_a_named_cabinet(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(
        _part(view, 0, None), _part(view, 1, _tag("B27", 1, 700)), also=(_beside(2),)
    )

    pieces = _pieces(session, built, finding)

    assert pieces[1].kind is PartKind.CABINET
    assert pieces[1].kind_named is True
    assert f"{TAG_NAMES_IT} 'B27' {PRINTED_ON_IT}" in pieces[1].reason
    # The others: nothing printed names them, and each says why no tag counted.
    assert pieces[0].kind_named is False
    assert "no one cabinet tag inside its span alone" in pieces[0].reason
    assert pieces[2].kind_named is False
    assert "found no cabinet with its ends" in pieces[2].reason


@pytest.mark.parametrize("text", ["B27", "BC27L", "BD19-4", "DB19-4", "SB33", "SB27P5R"])
def test_every_base_cabinet_letter_code_pre_fills(session: Session, text: str) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag(text, 0, 700)), also=(_beside(1),))

    assert _pieces(session, built, finding)[0].kind_named is True


@pytest.mark.parametrize(
    "text",
    [
        "DW21",  # a dishwasher
        "MW27",  # a microwave
        "HD33",  # a hood
        "BF331",  # a base filler
        "WF343",  # a wall filler
        "F331",  # a filler
        "EP23",  # an end panel
        "FP37",  # a finished panel
        "RF37",  # a refrigerator panel
        "RP83",  # a refrigerator end panel
        "W3315",  # a wall cabinet
        "OW3315",  # an open wall cabinet
        "TC84",  # a tall cabinet
        "K24",  # a letter code nobody listed
    ],
)
def test_a_code_that_is_not_a_base_cabinet_s_names_nothing(session: Session, text: str) -> None:
    """The reviewer's case: a code's shape alone fits appliances, fillers and panels too, so only
    the listed base-cabinet letter codes pre-fill; every other stays unknown, saying why."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag(text, 0, 700)), also=(_beside(1),))

    piece = _pieces(session, built, finding)[0]

    assert piece.kind_named is False
    assert f"{text!r}" in piece.reason and "base-cabinet letter code" in piece.reason


def test_the_reviewer_s_row_pre_fills_only_the_cabinet(session: Session) -> None:
    """A base cabinet, a dishwasher and a base filler tagged at one height: one cabinet."""
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(
        _part(view, 0, _tag("B27", 0, 700)),
        _part(view, 1, _tag("DW21", 1, 700)),
        _part(view, 2, _tag("BF331", 2, 700)),
    )

    pieces = _pieces(session, built, finding)

    assert [pieces[slot].kind_named for slot in range(3)] == [True, False, False]


def test_switched_off_the_same_finding_names_nothing(session: Session) -> None:
    """Off is exactly today's: the same kinds and the same words, with or without the finder."""
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(_part(view, 1, _tag("B27", 1, 700)), also=(_beside(2),))

    off = _pieces(session, built, finding, prefill=False)
    without = _pieces(session, built, "code's part finder was not asked", prefill=False)

    assert off == without
    assert not any(part.kind_named for part in off.values())
    assert "nothing printed names its kind; say what it is" in off[1].reason


def test_a_wall_cabinet_tag_above_the_run_stays_unknown(session: Session) -> None:
    """The AI Set 1 shape, with made-up tags: base tags over pieces 0 and 2, and a tag far higher
    over piece 1 (an appliance space with no tag of its own). The finder attaches the high tag to
    piece 1, because it reads only across the page; Bala refuses it, even with a base letter code.
    """
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(
        _part(view, 0, _tag("B27", 0, 700)),
        _part(view, 1, _tag("B45", 1, 300)),
        _part(view, 2, _tag("SB33", 2, 700)),
    )

    pieces = _pieces(session, built, finding)

    assert pieces[1].kind_named is False
    assert "'B45'" in pieces[1].reason and "wall cabinet's" in pieces[1].reason
    assert "'B27'" in pieces[1].reason and "'SB33'" in pieces[1].reason
    # The base tags still pre-fill their own pieces.
    assert pieces[0].kind_named is True and pieces[2].kind_named is True


def test_a_lower_tag_anywhere_over_the_row_makes_a_higher_one_a_wall_cabinet_s(
    session: Session,
) -> None:
    """The lower tag need not belong to a part the finder found: any cabinet code printed over the
    row is evidence of where the row's own tags stand."""
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(_part(view, 1, _tag("B45", 1, 300)), also=(_tag("B27", 2, 700),))

    pieces = _pieces(session, built, finding)

    assert pieces[1].kind_named is False
    assert "wall cabinet's" in pieces[1].reason


def test_base_tags_printed_below_the_dimension_still_make_a_high_tag_a_wall_cabinet_s(
    session: Session,
) -> None:
    """The reviewer's M1 case: a high tag over an appliance space, the base tags printed under the
    dimension line (y 900, the dimension at 800). Those still stand lower, so it is refused."""
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(
        _part(view, 1, _tag("B45", 1, 300)),
        also=(_tag("B27", 0, 900), _tag("SB33", 2, 900)),
    )

    pieces = _pieces(session, built, finding)

    assert pieces[1].kind_named is False
    assert "wall cabinet's" in pieces[1].reason


def test_a_lone_tag_over_the_row_names_nothing(session: Session) -> None:
    """With no other cabinet code printed over the row, nothing shows a tag is the row's own and
    not a wall cabinet's above a piece with none of its own: unknown."""
    built = _Revision(session)
    built.row(built.run(), pieces=3)
    view = built.views[0].id
    finding = _finding(_part(view, 1, _tag("B45", 1, 300)))

    piece = _pieces(session, built, finding)[1]

    assert piece.kind_named is False
    assert "the only cabinet code printed over the row" in piece.reason


def test_tags_in_one_band_a_little_apart_all_count(session: Session) -> None:
    """Base tags of one run stand at about one height; one a little lower than another (less than
    its own height) is the same band, not a wall."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), _part(view, 1, _tag("SB33", 1, 715)))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is True and pieces[1].kind_named is True


def test_a_turned_tag_is_measured_across_its_text(session: Session) -> None:
    """A tag printed turned is tall and narrow: its own height is the smaller side of its box, so
    a base tag 30 px lower still marks it as higher up the wall."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    turned = PrintedCode(text="B45", left=_x(260), top=_y(600), right=_x(280), bottom=_y(680))
    finding = _finding(_part(view, 0, turned), also=(_tag("SB33", 1, 711),))

    piece = _pieces(session, built, finding)[0]

    assert piece.kind_named is False
    assert "wall cabinet's" in piece.reason


def test_a_tag_outside_the_row_does_not_count_against_one_over_it(session: Session) -> None:
    """A lower tag beyond the row's ends belongs to another run: it says nothing about this one."""
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    beyond = PrintedCode(text="B18", left=_x(1000), top=_y(780), right=_x(1060), bottom=_y(790))
    finding = _finding(_part(view, 0, _tag("SB33", 0, 600)), also=(beyond, _beside(1, top_px=600)))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is True


def test_a_tag_not_printed_above_the_dimension_does_not_count(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 900)), also=(_beside(1),))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is False
    assert "not printed above its dimension" in pieces[0].reason


def test_a_part_with_other_ends_names_nothing(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700), shift_px=40), also=(_beside(1),))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is False
    assert "found no cabinet with its ends" in pieces[0].reason


def test_a_part_on_another_drawing_names_nothing(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    finding = _finding(_part(uuid4(), 0, _tag("B27", 0, 700)), also=(_beside(1),))

    assert _pieces(session, built, finding)[0].kind_named is False


def test_two_different_tags_read_on_one_piece_name_nothing_naming_both(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), _part(view, 0, _tag("SB33", 0, 705)))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is False
    assert "'B27'" in pieces[0].reason and "'SB33'" in pieces[0].reason


def test_a_label_naming_another_kind_and_a_tag_name_it_two_ways(session: Session) -> None:
    """Two printed kinds: unknown, naming both. The label's FILLER is not pre-filled over a tag."""
    built = _Revision(session)
    built.row(built.run(), pieces=2, kinds={0: ("filler", 'printed word "FILLER"')})
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), also=(_beside(1),))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind_named is False
    assert "two ways" in pieces[0].reason
    assert "filler" in pieces[0].reason and "'B27'" in pieces[0].reason


def test_a_label_naming_a_cabinet_and_a_tag_agree(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2, kinds={0: ("cabinet", 'printed tag "B27"')})
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), also=(_beside(1),))

    pieces = _pieces(session, built, finding)

    assert pieces[0].kind is PartKind.CABINET and pieces[0].kind_named is True


def test_without_the_finder_nothing_is_pre_filled_and_the_piece_says_why(session: Session) -> None:
    built = _Revision(session)
    built.row(built.run(), pieces=2)

    pieces = _pieces(session, built, "code's part finder is off: its settings are not stated")

    assert not any(part.kind_named for part in pieces.values())
    assert "no printed tag was read" in pieces[0].reason


def _link(session: Session, anchor: UUID, slot: int) -> PartProposalRow:
    return session.scalars(
        select(PartProposalRow).where(
            PartProposalRow.row_candidate_id == anchor, PartProposalRow.slot_index == slot
        )
    ).one()


def test_a_pre_filled_piece_is_filed_named_and_is_still_a_person_s_click(session: Session) -> None:
    """Filed with `kind_named`, which the Measure page pre-selects; never confirmed, never an item,
    and never automatic."""
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), also=(_beside(1),))

    outcome = propose_items(
        session, built.revision.id, code_parts=lambda _page: finding, cabinet_prefill=True
    )

    link = _link(session, anchor, 0)
    proposal = session.get_one(PartProposal, link.part_proposal_id)
    assert link.kind_named is True
    assert proposal.kind == PartKind.CABINET.value
    assert "'B27'" in proposal.reason
    assert int(session.scalar(select(func.count()).select_from(DrawingItem)) or 0) == 0
    (row,) = outcome.plan.rows
    assert row.automatic.pieces == (False, False)


@pytest.mark.parametrize(("first", "then"), [(True, False), (False, True)])
def test_the_switch_governs_new_filings_only(session: Session, first: bool, then: bool) -> None:
    """Decided with the coordinator (option B, no migration): a suggestion already filed keeps
    the `kind_named` it was filed with when match runs again after the switch flips. Off does not
    take back pre-fills already filed (they stay one-click pre-selections a person confirms); on
    applies from the next reading."""
    built = _Revision(session)
    anchor = built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), also=(_beside(1),))

    propose_items(
        session, built.revision.id, code_parts=lambda _page: finding, cabinet_prefill=first
    )
    filed = _link(session, anchor, 0)
    again = propose_items(
        session, built.revision.id, code_parts=lambda _page: finding, cabinet_prefill=then
    )

    assert again.written == 0 and again.found == 3
    assert _link(session, anchor, 0).id == filed.id
    assert _link(session, anchor, 0).kind_named is first
    # The plan itself follows the switch: a new reading would be filed with it.
    (row,) = again.plan.rows
    assert next(part for part in row.parts if part.slot_index == 0).kind_named is then


# -- the printed codes code's part finder is given (`workflow/stages.py`) ------------------------------


def _text_run(session: Session, built: _Revision, extractor: str) -> ExtractionRun:
    task = TaskRun(
        workflow_run_id=built.workflow.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    run = ExtractionRun(
        task_run_id=task.id, extractor=extractor, extractor_version="1", config_hash="t", dpi=150
    )
    session.add(run)
    session.flush()
    return run


def _read(
    session: Session, built: _Revision, run: ExtractionRun, text: str, box: tuple[int, ...]
) -> None:
    x0, y0, x1, y1 = box
    session.add(
        ObservationCandidate(
            document_version_id=built.version.id,
            page_id=built.pages[0].id,
            extraction_run_id=run.id,
            raw_text=text,
            polygon=[[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            coordinate_space="image",
            ambiguity_flags=[],
        )
    )
    session.flush()


def test_the_finder_is_given_the_vendor_s_cabinet_codes_once_each(session: Session) -> None:
    """One print read by two layers is one code; two prints are two; GV's markup and an AI
    reader's text are never given; words that are not cabinet codes are not either."""
    built = _Revision(session)
    stamp = _text_run(session, built, "extraction.stamp_text")
    ocr = _text_run(session, built, "rapidocr")
    markup = _text_run(session, built, "extraction.annotations")
    vision = _text_run(session, built, "vision.some-model")
    _read(session, built, stamp, "K24", (260, 700, 340, 720))
    _read(session, built, ocr, "K24", (262, 701, 338, 719))  # the same print, read again
    _read(session, built, stamp, "K24", (860, 700, 940, 720))  # the same code printed again
    _read(session, built, stamp, "Q30", (460, 700, 540, 720))
    _read(session, built, stamp, "SINK", (460, 600, 540, 620))  # a word, not a code
    _read(session, built, markup, "Z3015", (660, 700, 740, 720))  # GV's markup: the answer key
    _read(session, built, vision, "K18", (660, 750, 740, 770))  # an AI reading

    page = session.get_one(Page, built.pages[0].id)
    given = _printed_texts(session, page)

    assert sorted((text.text, min(p.x for p in text.extent.points)) for text in given) == sorted(
        [("K24", _x(260)), ("K24", _x(860)), ("Q30", _x(460))]
    )
    assert all(text.extent.space == "stored" for text in given)


# -- the piece-kind scorecard reads what Bala filed (`eval/pariksha/kinds.py`, source `bala`) -------


def test_the_kind_scorecard_scores_what_bala_filed(session: Session) -> None:
    """How the switch is measured before it goes on: the same run scored on what Bala filed."""
    from eval.pariksha.kinds import (
        BALA,
        KINDS_AGENT,
        KINDS_FAMILY,
        load_kinds_key,
        load_stored_pieces,
        score_kinds,
    )

    built = _Revision(session)
    built.row(built.run(), pieces=2)
    view = built.views[0].id
    finding = _finding(_part(view, 0, _tag("B27", 0, 700)), also=(_beside(1),))
    propose_items(
        session, built.revision.id, code_parts=lambda _page: finding, cabinet_prefill=True
    )
    session.commit()
    key = load_kinds_key(
        {
            "format": "pariksha-key/1",
            "agent": KINDS_AGENT,
            "name": "synthetic",
            "version": "1",
            "cases": [
                {
                    "family": KINDS_FAMILY,
                    "page_sha256": built.pages[0].content_hash,
                    "question_id": f"p0:piece{slot}",
                    "pieces": 2,
                    "row_widths": ["20", "20"],
                    "answer": {"kinds": [kind]},
                }
                for slot, kind in ((0, "cabinet"), (1, "filler"))
            ],
        }
    )

    stored = load_stored_pieces(session, database="synthetic")
    session.rollback()
    card = score_kinds(key, stored, source=BALA, code_version="unrecorded")

    assert [piece.bala for piece in stored.pieces] == [("cabinet", True), ("cabinet", False)]
    assert {case.question.rsplit("/", 1)[-1]: case.outcome for case in card.cases} == {
        "p0:piece0": "right",
        "p0:piece1": "unknown",  # suggested as a cabinet, but nothing names it: never "wrong"
    }
    assert card.extractor_versions["kind_source"] == BALA
