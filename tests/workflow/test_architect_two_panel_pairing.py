"""Pairing a vendor row against the architect view matched with it, and saying where each row stands
(#1167). No database here; the end-to-end package is `test_separate_architect_matched_pairing.py`.

Verification for `restrict_to_view`, `ArchitectPageInput.has_architect_view`, `ArchitectPairing`
with a matcher (a fake returning #1166's contract `RowMatch` values; both AIs a fake `ask` keyed
`(key, model)`), `pair_picture_two_panel` / `pair_question_two_panel`, and the row texts of
`plan_architect_row` for every match state. Invented geometry only: the vendor's view at 2 pt per
inch, the architect's at 3/2 pt per inch, pieces of 30, 24 and 18 inches. No client value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from evidence.crop import decode_rgb_png, encode_png
from extraction.geometry.rows import Box
from extraction.slot_reader.bedrock import (
    ARCH_PAIR_2PANEL_PROMPT_ID,
    ARCH_PAIR_PROMPT_ID,
    CropJob,
    job_prompt_id,
)
from tests.workflow.test_architect_pairing import (
    OPUS,
    SONNET,
    WIDTHS,
    _result,
    _sealed_owners,
    _slot_page,
    answer,
    arch_row,
)
from workflow.architect_match_contract import (
    EffectiveMatch,
    MatchedView,
    RowMatch,
    compared_with_text,
    restrict_to_view,
)
from workflow.architect_pairing import (
    MEASURED_PAIRING_SETTINGS,
    TWO_PANEL_GUTTER_PX,
    ArchitectPageInput,
    ArchitectPairing,
    ArchitectRowInput,
    pair_picture_two_panel,
    vendor_row_input,
)
from workflow.architect_pairing_contract import EffectivePairing
from workflow.architect_row_plan import (
    CHOOSE_ARCHITECT_VIEW,
    NO_ARCHITECT_VIEW_MATCHES,
    NOT_MATCHED_YET_ROW,
    SEPARATE_ARCHITECT_FILE_ROW,
    Disposition,
    matched_words,
    not_separated_reason,
    plan_architect_row,
)

RED = bytes((220, 20, 60))
BLUE = bytes((0, 90, 220))


def _view(number: int, page_id: UUID) -> MatchedView:
    return MatchedView(
        view_id=uuid4(),
        document_version_id=uuid4(),
        page_id=page_id,
        page_number=2,
        view_number=number,
        view_tag=f"view-{number}",
        title="SYNTHETIC ELEVATION",
        bubble=None,
        sheet_number="X-101",
        scale_note='1/4" = 1\'-0"',
        file_name="synthetic-architect.pdf",
        separated=True,
    )


def _in_view(row: ArchitectRowInput, number: int) -> ArchitectRowInput:
    return replace(row, view_annotation_index=number)


def _architect_sheet(*rows: ArchitectRowInput) -> ArchitectPageInput:
    """A page of the architect's own file: view 1 at the left, view 2 at the right."""
    return ArchitectPageInput(
        page_id=uuid4(),
        architect_run_id=uuid4(),
        rows=rows,
        view_boxes=(),
        confirmed_views=frozenset({1, 2}),
        view_extents={
            1: Box(Decimal(380), Decimal(20), Decimal(560), Decimal(200)),
            2: Box(Decimal(600), Decimal(20), Decimal(800), Decimal(200)),
        },
    )


@dataclass(frozen=True)
class Picture:
    png: bytes
    box_px: tuple[int, int, int, int]
    dpi: int


def _picture(width: int = 500, height: int = 200) -> Picture:
    return Picture(
        encode_png(width, height, bytes([255]) * (width * height * 3)),
        (350, 0, 350 + width, height),
        72,
    )


@dataclass
class FakeMatcher:
    status: str
    chosen: MatchedView | None
    seen: list[UUID] = field(default_factory=list)

    def match(
        self, results: Sequence[Any], pages: Sequence[Any], **_: object
    ) -> dict[int, RowMatch]:
        self.seen.extend(result.page_id for result in results)
        return {
            result.page_index: RowMatch(
                status=self.status,  # type: ignore[arg-type]
                source="carried" if self.status == "carried_over" else "automatic",
                chosen=self.chosen,
                code=SimpleNamespace(verdict="geometry_clear", pick=None, ranked=(), reasons=()),
                ai_picks=(),
                candidate_json=(),
                reasons=(),
                question_packet=None,
            )
            for result in results
        }


def _run(
    pages: Mapping[UUID, ArchitectPageInput],
    matcher: FakeMatcher | None,
    *,
    vendor_page: UUID | None = None,
    crops: Mapping[UUID, Picture] | None = None,
    pick: tuple[int, tuple[int, ...]] = (0, (1, 2, 3)),
) -> tuple[Any, list[CropJob]]:
    page_id = vendor_page or uuid4()
    result = replace(_result(_sealed_owners()), page_id=page_id)
    asked: list[CropJob] = []

    def ask(jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        asked.extend(jobs)
        return {
            (job.key, job.model_id): answer(job.model_id, pick, job.architect_spans or 0)
            for job in jobs
        }

    (paired,) = ArchitectPairing(
        MEASURED_PAIRING_SETTINGS, pages, matcher=matcher, crops=crops or {}
    ).pair(
        [result],
        [_slot_page(page_id)],
        ask=ask,
        readers=(OPUS, SONNET),
        ask_the_ais=True,
        store=None,
        effort="high",
    )
    return paired, asked


# --- restricting a page to one view ------------------------------------------------------------------


def test_restricting_to_a_view_keeps_only_its_rows_framed_by_its_extent() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1), _in_view(arch_row(2, WIDTHS), 2))
    extent = sheet.view_extents[2]

    only = restrict_to_view(sheet, 2, extent)

    assert [row.rank for row in only.rows] == [2]
    assert only.view_boxes == (extent,)
    assert only.page_id == sheet.page_id and only.architect_run_id == sheet.architect_run_id
    assert restrict_to_view(sheet, 3, extent).rows == ()


def test_a_page_has_its_own_architect_view_when_a_drawing_is_confirmed_or_rows_were_read() -> None:
    empty = ArchitectPageInput(page_id=uuid4(), architect_run_id=None, rows=(), view_boxes=())

    assert not empty.has_architect_view
    assert replace(empty, confirmed_views=frozenset({1})).has_architect_view
    assert replace(empty, rows=(arch_row(1, WIDTHS),)).has_architect_view


# --- the pairing step with a matcher ------------------------------------------------------------------


def test_a_row_on_a_page_with_its_own_architect_view_is_never_matched_and_asked_v3() -> None:
    own = ArchitectPageInput(
        page_id=uuid4(),
        architect_run_id=uuid4(),
        rows=(arch_row(1, WIDTHS),),
        view_boxes=(Box(Decimal(380), Decimal(20), Decimal(560), Decimal(200)),),
    )
    matcher = FakeMatcher("auto_matched", _view(1, own.page_id))
    result = replace(_result(_sealed_owners()), page_id=own.page_id)
    page = _slot_page(own.page_id)
    runs = []
    for with_matcher in (None, matcher):
        asked: list[CropJob] = []

        def ask(
            jobs: Sequence[CropJob], asked: list[CropJob] = asked
        ) -> Mapping[tuple[str, str], object]:
            asked.extend(jobs)
            return {
                (job.key, job.model_id): answer(job.model_id, (0, (1, 2, 3)), 3) for job in jobs
            }

        (paired,) = ArchitectPairing(
            MEASURED_PAIRING_SETTINGS, {own.page_id: own}, matcher=with_matcher
        ).pair(
            [result],
            [page],
            ask=ask,
            readers=(OPUS, SONNET),
            ask_the_ais=True,
            store=None,
            effort="high",
        )
        runs.append((paired, list(asked)))

    (before, asked_before), (after, asked_after) = runs
    assert matcher.seen == []
    assert after.architect_match is None
    assert after == before, "the matcher changes nothing on a page with its own architect view"
    assert [job.question_packet for job in asked_after] == [
        job.question_packet for job in asked_before
    ]
    assert {job_prompt_id(job, None) for job in asked_after} == {ARCH_PAIR_PROMPT_ID}
    assert not any(job.arch_pair_two_panel for job in asked_after)


def test_an_automatic_match_pairs_against_that_view_only_with_the_two_panel_question() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, (72,)), 1), _in_view(arch_row(2, WIDTHS), 2))
    chosen = _view(2, sheet.page_id)
    matcher = FakeMatcher("auto_matched", chosen)

    paired, asked = _run({sheet.page_id: sheet}, matcher, crops={chosen.view_id: _picture()})

    assert paired.architect_match is not None and paired.architect_match.chosen == chosen
    outcome = paired.architect_pairing
    assert outcome is not None and (outcome.source, outcome.status) == ("code+ais", "paired")
    view_two = {span.candidate_id for span in sheet.rows[1].spans}
    assert {pair.architect_candidate_id for pair in outcome.pairs} == view_two
    assert outcome.details["architect_view_id"] == str(chosen.view_id)
    assert outcome.details["architect_view_number"] == 2
    assert outcome.details["architect_page_index"] == 1
    assert [job_prompt_id(job, None) for job in asked] == [ARCH_PAIR_2PANEL_PROMPT_ID] * 2
    packet = asked[0].question_packet
    assert packet is not None
    assert packet["architect_candidate_ids"] == [
        str(span.candidate_id) for span in sheet.rows[1].spans
    ]
    assert packet["architect_view_number"] == 2 and packet["common_scale"] is True
    assert all(job.arch_pair_common_scale for job in asked)
    ai: Any = outcome.details["ai"]
    assert ai["prompt_id"] == ARCH_PAIR_2PANEL_PROMPT_ID


def test_a_carried_match_is_paired_too() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1))
    chosen = _view(1, sheet.page_id)

    paired, _asked = _run(
        {sheet.page_id: sheet},
        FakeMatcher("carried_over", chosen),
        crops={chosen.view_id: _picture()},
    )

    assert paired.architect_pairing is not None
    assert paired.architect_pairing.source == "code+ais"


@pytest.mark.parametrize(
    "status", ["needs_reviewer", "none_matches", "not_separated", "no_candidates"]
)
def test_no_other_match_state_is_paired_and_no_pairing_question_is_asked(status: str) -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1))
    chosen = _view(1, sheet.page_id) if status == "not_separated" else None

    paired, asked = _run({sheet.page_id: sheet}, FakeMatcher(status, chosen))

    assert paired.architect_match is not None and paired.architect_match.status == status
    assert paired.architect_pairing is None
    assert asked == []


def test_without_the_views_picture_code_pairs_alone_and_no_ai_is_asked() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1))

    paired, asked = _run(
        {sheet.page_id: sheet}, FakeMatcher("auto_matched", _view(1, sheet.page_id))
    )

    assert asked == []
    assert paired.architect_pairing is not None
    assert paired.architect_pairing.source == "code", "one judgment: the reviewer confirms it"


def test_a_matched_view_the_architect_reader_did_not_read_is_not_paired() -> None:
    paired, asked = _run({}, FakeMatcher("auto_matched", _view(1, uuid4())))

    assert paired.architect_match is not None
    assert paired.architect_pairing is None and asked == []


# --- the two-panel picture ----------------------------------------------------------------------------


def _pixels(png: bytes) -> tuple[int, int, bytes]:
    return decode_rgb_png(png)


def _column_white(width: int, height: int, rgb: bytes, x: int) -> bool:
    return all(
        rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] == b"\xff\xff\xff" for y in range(height)
    )


def test_the_two_panel_picture_puts_the_vendor_left_the_architect_right_at_one_scale() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1))
    vendor = vendor_row_input(_result(_sealed_owners()))
    assert vendor is not None
    page = _slot_page(uuid4())

    picture = pair_picture_two_panel(page.rendered, vendor, sheet, _picture())

    width, height, rgb = _pixels(picture.png)
    assert picture.common_scale is True
    assert width <= 1800 and height <= 1800
    colours = {rgb[k : k + 3] for k in range(0, len(rgb), 3)}
    assert RED in colours and BLUE in colours
    # The vendor panel is 2 pt per inch at 72 dpi; the architect's 1.5 pt per inch at 72 dpi is
    # brought to the same pixels per inch: 500 px wide becomes 667.
    left = (vendor.pieces[-1].box_px[2] + 20) - max(0, vendor.pieces[0].box_px[0] - 20)
    assert width == left + TWO_PANEL_GUTTER_PX + round(500 * 2 / 1.5)
    assert all(_column_white(width, height, rgb, left + k) for k in range(TWO_PANEL_GUTTER_PX))


def test_without_both_scales_the_panels_are_the_same_height_and_the_question_says_so() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS, scale=None), 1))
    vendor = vendor_row_input(_result(_sealed_owners()))
    assert vendor is not None

    picture = pair_picture_two_panel(
        _slot_page(uuid4()).rendered, vendor, sheet, _picture(300, 900)
    )

    width, height, _rgb = _pixels(picture.png)
    assert picture.common_scale is False
    assert height <= 1800 and width <= 1800


def test_a_huge_view_picture_is_shrunk_to_at_most_1800_pixels_a_side() -> None:
    sheet = _architect_sheet(_in_view(arch_row(1, WIDTHS), 1))
    vendor = vendor_row_input(_result(_sealed_owners()))
    assert vendor is not None
    big = Picture(encode_png(3000, 2500, bytes([255]) * (3000 * 2500 * 3)), (0, 0, 3000, 2500), 72)

    picture = pair_picture_two_panel(_slot_page(uuid4()).rendered, vendor, sheet, big)

    width, height, rgb = _pixels(picture.png)
    assert max(width, height) <= 1800
    colours = {rgb[k : k + 3] for k in range(0, len(rgb), 3)}
    assert RED in colours, "the vendor's marks survive the shrink"


# --- every row says where it stands -------------------------------------------------------------------


def _match(status: str, view: MatchedView | None) -> EffectiveMatch:
    return EffectiveMatch(
        record_id=uuid4(),
        status=status,  # type: ignore[arg-type]
        source="automatic",
        matched=view,
        needs_reviewer=status == "needs_reviewer",
        reasons=(),
        decided_by=None,
    )


ROW: Any = SimpleNamespace(anchor=SimpleNamespace(page_id=uuid4(), document_version_id=uuid4()))


@pytest.mark.parametrize(
    ("status", "disposition", "reason"),
    [
        ("needs_reviewer", Disposition.UNRESOLVED, CHOOSE_ARCHITECT_VIEW),
        ("none_matches", Disposition.NOT_COMPARED, NO_ARCHITECT_VIEW_MATCHES),
        ("no_candidates", Disposition.NOT_COMPARED, SEPARATE_ARCHITECT_FILE_ROW),
    ],
)
def test_each_match_state_says_where_the_row_stands(
    status: str, disposition: Disposition, reason: str
) -> None:
    plan = plan_architect_row(None, ROW, None, separate_architect_file=True, match=_match(status, None), architect_file_indexed=True)  # type: ignore[arg-type]

    assert (plan.disposition, plan.reason, plan.pairs, plan.matched) == (
        disposition,
        reason,
        (),
        None,
    )


def test_a_view_not_clearly_apart_is_compared_by_hand() -> None:
    view = _view(3, uuid4())

    plan = plan_architect_row(None, ROW, None, match=_match("not_separated", view))  # type: ignore[arg-type]

    assert plan.disposition is Disposition.UNRESOLVED
    assert plan.reason == not_separated_reason(view)
    assert plan.reason == (
        "The architect's view 3 on page 2 is not clearly apart from its neighbour, so its "
        "dimensions were not read. Compare this countertop by hand, then mark it checked."
    )


def test_a_matched_view_with_no_pairing_for_it_waits_for_a_re_run() -> None:
    view = _view(2, uuid4())

    plan = plan_architect_row(None, ROW, None, match=_match("reviewer_confirmed", view))  # type: ignore[arg-type]

    assert plan.disposition is Disposition.UNRESOLVED
    assert (plan.reason or "").startswith(f"{matched_words(view)}: ")
    assert "Run the checks again" in (plan.reason or "")
    assert plan.matched is None, "nothing compared, so no architect value may be used"


def test_a_matched_row_not_compared_names_the_view_before_todays_reason() -> None:
    view = _view(2, uuid4())
    pairing = EffectivePairing(
        record_id=uuid4(), source="none", status="nothing_comparable", pairs=(), reasons=()
    )

    sides: Any = SimpleNamespace(same_file_as_both_sides=lambda _version: False)

    plan = plan_architect_row(
        None, ROW, pairing, sides=sides, match=_match("auto_matched", view)  # type: ignore[arg-type]
    )

    assert plan.disposition is Disposition.NOT_COMPARED
    assert plan.reason == (
        "Matched with synthetic-architect.pdf, page 2, view 2: "
        "The architect prints nothing comparable for this row."
    )
    assert plan.matched == view
    assert compared_with_text(view).startswith(
        "compared with synthetic-architect.pdf, page 2, view 2"
    )


def test_with_no_match_the_separate_file_says_whether_it_was_matched_or_not_read() -> None:
    def reason(indexed: bool) -> str | None:
        return plan_architect_row(
            None,  # type: ignore[arg-type]
            ROW,
            None,
            separate_architect_file=True,
            architect_file_indexed=indexed,
        ).reason

    assert reason(False) == SEPARATE_ARCHITECT_FILE_ROW, "#1161's sentence, unchanged"
    assert reason(True) == NOT_MATCHED_YET_ROW
    assert NOT_MATCHED_YET_ROW == (
        "The architect's drawings were uploaded as a separate file and this countertop has not "
        "been matched with a view in it yet. Run the checks again."
    )
    no_file = plan_architect_row(None, ROW, None)  # type: ignore[arg-type]
    assert no_file.reason == "No architect dimension is paired with this row."
