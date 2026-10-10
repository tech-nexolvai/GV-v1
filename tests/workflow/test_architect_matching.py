"""Matching each vendor row with one view of the architect's own file: code, both AIs, D1, D3 (#1166).

Verification for `workflow/architect_matching.py` (no database here; the records are in
`test_architect_match_records.py`). Invented geometry only: the vendor's view at 2 pt per inch, the
architect's at 3/2 pt per inch, pieces of 30, 24 and 18 inches. No client value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from evidence.crop import decode_rgb_png, encode_png
from extraction.architect.pairing import DrawnRow, DrawnSpan
from extraction.architect.view_matching import ArchitectViewFacts, CodeVerdict
from extraction.geometry.rows import Box
from extraction.slot_reader.bedrock import ARCH_MATCH_PROMPT_ID, ArchMatchAnswer, CropJob
from extraction.slot_reader.claude_output import picture_fits
from tests.workflow.test_architect_pairing import (
    ARCH_PT,
    OPUS,
    SONNET,
    VENDOR_PT,
    WIDTHS,
    _sealed_owners,
    _slot_page,
    arch_row,
)
from workflow.architect_match_contract import MatchedView, compared_with_text, restrict_to_view
from workflow.architect_match_records import RememberedMatch, view_carry_key
from workflow.architect_matching import (
    ALL_VIEWS_CAP,
    MATCH_PICTURE_MAX_SIDE,
    MEASURED_MATCH_SETTINGS,
    ArchitectMatcher,
    MatchingArchitectPairing,
    VendorPageFacts,
    match_picture,
)
from workflow.architect_pairing import MEASURED_PAIRING_SETTINGS, ArchitectPageInput
from workflow.architect_view_index import ArchitectViewCrop, IndexedView
from workflow.slot_reader import PageSlotResult

READERS = (OPUS, SONNET)
CONTENT = "c" * 64


def _ticks(widths: Sequence[int], scale: Fraction, start: int = 100) -> list[Decimal]:
    at = [Fraction(start)]
    for width in widths:
        at.append(at[-1] + width * scale)
    return [Decimal(value.numerator) / Decimal(value.denominator) for value in at]


def indexed(
    widths: Sequence[int] | None,
    *,
    number: int,
    separated: bool = True,
    bubble: str | None = None,
    sheet: str = "Z-101",
    page_id: UUID | None = None,
) -> IndexedView:
    view_id = uuid4()
    page = page_id or uuid4()
    rows: tuple[DrawnRow, ...] = ()
    if widths is not None:
        ticks = _ticks(widths, ARCH_PT, start=50)
        rows = (
            DrawnRow(
                key="arch-row:1",
                spans=tuple(DrawnSpan(ticks[k], ticks[k + 1], True) for k in range(len(widths))),
                overall=None,
                pt_per_inch=ARCH_PT,
            ),
        )
    view = MatchedView(
        view_id=view_id,
        document_version_id=uuid4(),
        page_id=page,
        page_number=number,
        view_number=1,
        view_tag="view-1",
        title="SYNTHETIC ELEVATION",
        bubble=bubble,
        sheet_number=sheet,
        scale_note='1/4" = 1\'-0"',
        file_name="the architect's drawings",
        separated=separated,
    )
    return IndexedView(
        view=view,
        facts=ArchitectViewFacts(str(view_id), sheet, bubble, rows, separated),
        points_per_inch=ARCH_PT,
        carry_key=view_carry_key("a" * 64, f"{number:064d}", "view-1", f"{number:064x}", None),
        view_number=1,
        extent=Box(Decimal(40), Decimal(20), Decimal(300), Decimal(200)),
    )


def crop(view: IndexedView, *, picture: bool = True) -> ArchitectViewCrop:
    png = encode_png(80, 40, bytes([200]) * (80 * 40 * 3)) if picture else None
    return ArchitectViewCrop(
        view_id=view.view.view_id,
        png=png,
        sha256=None if png is None else "d" * 64,
        storage_key=None,
        px_per_inch=ARCH_PT,
        indexed=view,
    )


def result(*, page_id: UUID | None = None) -> PageSlotResult:
    owners = _sealed_owners()
    ticks = _ticks(WIDTHS, VENDOR_PT)
    plan = SimpleNamespace(
        row=SimpleNamespace(y=Decimal(310), ticks=tuple(ticks)),
        slots=tuple(owner.owner for owner in owners),
        overall=SimpleNamespace(index=None, x0=owners[0].owner.x0, x1=owners[-1].owner.x1),
    )
    return PageSlotResult(
        page_index=0,
        page_id=page_id or uuid4(),
        document_version_id=uuid4(),
        plan=plan,  # type: ignore[arg-type]
        slots=tuple(owners),
        overall=None,
        mapping=None,  # type: ignore[arg-type]
        owner_candidate_ids={"slot:0": uuid4()},
    )


def facts(*, combined: bool = False, content: str = CONTENT) -> VendorPageFacts:
    return VendorPageFacts(
        page_index=0,
        content_hash=content,
        phrases=(),
        view_boxes=(),
        has_architect_view=combined,
    )


class Asker:
    """Both readers' answers by shown number (1-based), 0 for none, None for unsure."""

    def __init__(self, *picks: int | None) -> None:
        self.picks = picks
        self.jobs: list[CropJob] = []
        self.calls = 0

    def __call__(self, jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        self.calls += 1
        self.jobs.extend(jobs)
        answers: dict[tuple[str, str], object] = {}
        for job, pick in zip(jobs, self.picks, strict=False):
            if pick is False:  # type: ignore[comparison-overlap]
                continue
            count = job.architect_candidates or 0
            same = tuple(
                "yes" if pick is not None and n == pick else "no" for n in range(1, count + 1)
            )
            answers[(job.key, job.model_id)] = ArchMatchAnswer(job.model_id, pick, same, "drawn")
        return answers


def matcher(
    views: Sequence[IndexedView],
    *,
    vendor: VendorPageFacts | None = None,
    page_id: UUID,
    remembered: tuple[RememberedMatch, ...] = (),
    pictures: bool = True,
    architect_pages: Mapping[UUID, ArchitectPageInput] | None = None,
) -> ArchitectMatcher:
    return ArchitectMatcher(
        settings=MEASURED_MATCH_SETTINGS,
        views=tuple(views),
        crops={view.view.view_id: crop(view, picture=pictures) for view in views},
        remembered=remembered,
        vendor_pages={page_id: vendor or facts()},
        architect_pages=architect_pages or {},
    )


def run(
    views: Sequence[IndexedView],
    asker: Asker,
    *,
    ask_the_ais: bool = True,
    **options: Any,
) -> tuple[Any, PageSlotResult]:
    row = result()
    found = matcher(views, page_id=row.page_id, **options).match(
        [row],
        [_slot_page(row.page_id)],
        ask=asker,
        readers=READERS,
        ask_the_ais=ask_the_ais,
        store=None,
        effort="high",
    )
    return found.get(row.page_index), row


SAME = indexed(WIDTHS, number=1)
TWIN = indexed(WIDTHS, number=2)  # the same run and bays: a twin
FAR = indexed((40, 40), number=3)


def test_the_settings_are_the_measured_ones() -> None:
    assert MEASURED_MATCH_SETTINGS.run_length_tolerance_in == Fraction(1)
    assert MEASURED_MATCH_SETTINGS.clear_margin_in == Fraction(3)
    assert MEASURED_MATCH_SETTINGS.shown_to_ais == 3
    assert MEASURED_MATCH_SETTINGS.pairing == MEASURED_PAIRING_SETTINGS


def test_code_and_both_ais_on_the_same_view_is_automatic() -> None:
    asker = Asker(1, 1)
    match, _row = run([FAR, SAME], asker)

    assert match.status == "auto_matched" and match.source == "automatic"
    assert match.chosen == SAME.view
    assert match.code.verdict == CodeVerdict.GEOMETRY_CLEAR
    assert asker.calls == 1 and len(asker.jobs) == 2
    job = asker.jobs[0]
    assert job.arch_match_question and job.architect_candidates == 2
    assert job.question_packet is not None
    assert job.question_packet["prompt_id"] == ARCH_MATCH_PROMPT_ID
    assert job.question_packet["architect_view_ids"] == [
        str(SAME.view.view_id),
        str(FAR.view.view_id),
    ]
    assert job.question_packet["common_scale"] is True
    first = match.candidate_json[0]
    assert first["view_id"] == str(SAME.view.view_id) and first["shown_number"] == 1
    assert first["ai_picked_by"] == [OPUS, SONNET] and first["remembered"] is False
    assert [pick["answer"] for pick in match.ai_picks] == ["view", "view"]


def test_twins_go_to_the_reviewer_even_when_both_ais_agree() -> None:
    match, _row = run([SAME, TWIN], Asker(1, 1))

    assert match.status == "needs_reviewer" and match.chosen is None
    assert match.code.verdict == CodeVerdict.GEOMETRY_TIE
    assert any("evidence" in reason for reason in match.reasons)


@pytest.mark.parametrize("picks", [(1, 2), (1, None), (0, 0), (2, 2)])
def test_anything_but_both_ais_on_codes_view_goes_to_the_reviewer(
    picks: tuple[int | None, int | None],
) -> None:
    match, _row = run([SAME, FAR], Asker(*picks))

    assert match.status == "needs_reviewer" and match.chosen is None


def test_without_the_claude_pair_nothing_is_asked_and_the_reviewer_picks() -> None:
    asker = Asker(1, 1)
    match, _row = run([SAME, FAR], asker, ask_the_ais=False)

    assert asker.calls == 0 and match.ai_picks == () and match.question_packet is None
    assert match.status == "needs_reviewer"


def test_a_view_without_a_picture_is_never_asked_about() -> None:
    asker = Asker(1, 1)
    match, _row = run([SAME, FAR], asker, pictures=False)

    assert asker.calls == 0 and match.status == "needs_reviewer"


def test_three_judgments_on_a_view_not_clearly_apart_are_not_separated() -> None:
    crowded = indexed(WIDTHS, number=1, separated=False)
    match, _row = run([crowded, FAR], Asker(1, 1))

    assert match.status == "not_separated" and match.chosen == crowded.view


def test_a_row_on_a_combined_sheet_is_never_matched_and_nothing_is_asked() -> None:
    asker = Asker(1, 1)
    row = result()
    found = matcher([SAME], vendor=facts(combined=True), page_id=row.page_id).match(
        [row],
        [_slot_page(row.page_id)],
        ask=asker,
        readers=READERS,
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    assert found == {} and asker.calls == 0


def test_every_chosen_row_gets_one_match_with_its_reason_and_others_none() -> None:
    no_scale = result()
    no_scale = replace(
        no_scale,
        slots=tuple(
            SimpleNamespace(
                **{
                    **vars(owner),
                    "outcome": SimpleNamespace(state=None, value=None, label_index=None),
                }
            )
            for owner in no_scale.slots
        ),
    )
    no_anchor = replace(result(), page_index=1, owner_candidate_ids={})
    built = ArchitectMatcher(
        settings=MEASURED_MATCH_SETTINGS,
        views=(SAME, FAR),
        crops={view.view.view_id: crop(view) for view in (SAME, FAR)},
    )
    found = built.match(
        [no_scale, no_anchor],
        [],
        ask=Asker(),
        readers=READERS,
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    assert set(found) == {0}
    (match,) = found.values()
    assert match.status == "needs_reviewer" and match.code.verdict == CodeVerdict.NO_GEOMETRY
    assert any("scale" in reason for reason in match.reasons)


def test_no_view_in_the_architects_file_is_no_candidates() -> None:
    match, _row = run([], Asker())

    assert match.status == "no_candidates" and match.candidate_json == ()


def test_each_candidate_with_rows_carries_codes_position_pairing() -> None:
    page = ArchitectPageInput(
        page_id=SAME.view.page_id,
        architect_run_id=uuid4(),
        rows=(arch_row(1, WIDTHS), replace(arch_row(2, (40, 40)), view_annotation_index=2)),
        view_boxes=(),
    )
    match, _row = run([SAME, FAR], Asker(1, 1), architect_pages={SAME.view.page_id: page})

    pairing = match.candidate_json[0]["code_pairing"]
    assert pairing["source"] == "code" and pairing["status"] == "paired"
    assert len(pairing["pairs"]) == 3
    assert match.candidate_json[1]["code_pairing"] is None


def test_restrict_to_view_keeps_only_that_views_rows() -> None:
    page = ArchitectPageInput(
        page_id=uuid4(),
        architect_run_id=None,
        rows=(arch_row(1, WIDTHS), replace(arch_row(2, (40, 40)), view_annotation_index=2)),
        view_boxes=(Box(Decimal(0), Decimal(0), Decimal(9), Decimal(9)),),
    )
    extent = Box(Decimal(1), Decimal(2), Decimal(3), Decimal(4))

    restricted = restrict_to_view(page, 2, extent)

    assert [row.rank for row in restricted.rows] == [2] and restricted.view_boxes == (extent,)


def test_the_result_names_the_view_it_was_compared_with() -> None:
    assert compared_with_text(SAME.view) == (
        "compared with the architect's drawings, page 1, view 1 SYNTHETIC ELEVATION "
        "(sheet Z-101)"
    )


# --- remembered (D3) -------------------------------------------------------------------------------


def remembered(
    view: IndexedView | None,
    *,
    content: str = CONTENT,
    status: str = "reviewer_confirmed",
    files: tuple[str, ...] = ("a" * 64,),
) -> RememberedMatch:
    return RememberedMatch(
        record_id=uuid4(),
        package_revision_id=uuid4(),
        revision_number=1,
        status=status,
        vendor_item_key={
            "page_content_hash": content,
            "page_index": 0,
            "pieces": 3,
            "row_y_pt": "310.4",
        },
        view_key=None if view is None else view.carry_key,
        created_at=datetime.now(UTC),
        architect_files=files,
    )


def test_a_persons_pick_carries_to_the_identical_item_with_no_question() -> None:
    asker = Asker(2, 2)
    match, _row = run([SAME, TWIN], asker, remembered=(remembered(TWIN),))

    assert (match.status, match.source, match.chosen) == ("carried_over", "carried", TWIN.view)
    assert asker.calls == 0 and match.question_packet is None
    assert match.details is not None and match.details["carried_from_revision"] == 1


def test_a_persons_none_carries_to_the_identical_item_when_code_has_no_pick() -> None:
    match, _row = run([SAME, TWIN], Asker(), remembered=(remembered(None, status="none_matches"),))

    assert (match.status, match.source, match.chosen) == ("none_matches", "carried", None)


def test_code_contradicting_a_persons_pick_sends_it_back_with_the_pick_remembered() -> None:
    asker = Asker(1, 1)
    match, _row = run([SAME, FAR], asker, remembered=(remembered(FAR),))

    assert match.status == "needs_reviewer" and match.chosen is None
    assert asker.calls == 1, "not carried, so both AIs are asked"
    by_view = {item["view_id"]: item for item in match.candidate_json}
    assert by_view[str(FAR.view.view_id)]["remembered"] is True
    assert by_view[str(SAME.view.view_id)]["remembered"] is False
    assert any("reviewer decides again" in reason for reason in match.reasons)


def test_a_changed_vendor_page_is_never_carried_and_the_pick_is_only_remembered() -> None:
    match, _row = run(
        [SAME, TWIN], Asker(None, None), remembered=(remembered(TWIN, content="e" * 64),)
    )

    assert match.status == "needs_reviewer" and match.chosen is None and match.source == "automatic"
    flagged = [item for item in match.candidate_json if item["remembered"]]
    assert [item["view_id"] for item in flagged] == [str(TWIN.view.view_id)]
    assert any("remembered from revision 1" in text for text in flagged[0]["evidence"])


def test_an_automatic_match_against_a_persons_earlier_pick_goes_to_the_reviewer() -> None:
    match, _row = run([SAME, FAR], Asker(1, 1), remembered=(remembered(FAR, content="e" * 64),))

    assert match.status == "needs_reviewer"
    assert any("decided otherwise" in reason for reason in match.reasons)


def test_a_remembered_view_no_longer_in_the_file_is_the_reviewers() -> None:
    gone = indexed(WIDTHS, number=9)
    match, _row = run([SAME, FAR], Asker(1, 1), remembered=(remembered(gone),))

    assert match.status == "needs_reviewer"
    assert any("not in the architect's drawings any more" in reason for reason in match.reasons)


# --- the picture -----------------------------------------------------------------------------------


def test_the_picture_is_one_png_within_the_limit_with_the_vendor_row_outlined_in_red() -> None:
    page = _slot_page(uuid4())
    big = ArchitectViewCrop(
        view_id=uuid4(),
        png=encode_png(3000, 1000, bytes([255]) * (3000 * 1000 * 3)),
        sha256="d" * 64,
        storage_key=None,
        px_per_inch=Fraction(10),
        indexed=SAME,
    )

    png, common = match_picture(page, (0, 0, 600, 400), (100, 300, 244, 320), Fraction(2), [big])

    width, height, rgb = decode_rgb_png(png)
    assert common is True
    assert max(width, height) <= MATCH_PICTURE_MAX_SIDE and picture_fits(width, height, OPUS)
    assert bytes((220, 20, 60)) in rgb, "the vendor's row is outlined in red"
    assert bytes((0, 90, 220)) in rgb, "the architect views are tagged in blue"


def test_without_every_scale_the_panels_are_fitted_and_the_packet_says_so() -> None:
    page = _slot_page(uuid4())
    unknown = replace(crop(SAME), px_per_inch=None)

    _png, common = match_picture(
        page, (0, 0, 600, 400), (100, 300, 244, 320), Fraction(2), [unknown]
    )

    assert common is False


# --- inside the slot reader's architect step --------------------------------------------------------


def test_the_architect_step_matches_first_then_pairs_as_before() -> None:
    row = result()
    page = ArchitectPageInput(page_id=row.page_id, architect_run_id=None, rows=(), view_boxes=())
    step = MatchingArchitectPairing(
        settings=MEASURED_PAIRING_SETTINGS,
        pages={row.page_id: page},
        matcher=matcher([SAME, FAR], page_id=row.page_id),
    )

    (done,) = step.pair(
        [row],
        [_slot_page(row.page_id)],
        ask=Asker(1, 1),
        readers=READERS,
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    assert done.architect_match is not None and done.architect_match.status == "auto_matched"
    assert done.architect_pairing is not None, "the pairing still runs exactly as before"


def test_a_rerun_asks_the_identical_question_so_its_stored_answer_can_be_reused() -> None:
    """The index rows are new on every run; the question is the same while its pictures are."""
    from workflow.reader_reuse import question_identity

    row = result()
    page = _slot_page(row.page_id)
    packets = []
    for _run in range(2):
        views = [indexed(WIDTHS, number=1), indexed((40, 40), number=3)]
        asker = Asker(1, 1)
        matcher(views, page_id=row.page_id).match(
            [row], [page], ask=asker, readers=READERS, ask_the_ais=True, store=None, effort="high"
        )
        packets.append(asker.jobs[0].question_packet)

    first, second = packets
    assert first is not None and second is not None
    assert first["architect_view_ids"] != second["architect_view_ids"]
    assert first["packet_sha256"] != second["packet_sha256"]
    assert question_identity(first) == question_identity(second) is not None


# --- review fixes (PR #1170) ------------------------------------------------------------------------


def test_a_persons_none_is_not_carried_once_the_architects_file_changed() -> None:
    """Review finding 2: "none of these" was said of other architect drawings."""
    asker = Asker(None, None)
    match, _row = run(
        [SAME, TWIN],
        asker,
        remembered=(remembered(None, status="none_matches", files=("b" * 64,)),),
    )

    assert (match.status, match.source) == ("needs_reviewer", "automatic")
    assert any("have changed since" in reason for reason in match.reasons)
    assert asker.calls == 1, "not carried, so the AIs are asked"


def test_a_crowded_view_is_not_carried_once_the_architects_file_changed() -> None:
    crowded = indexed(WIDTHS, number=1, separated=False)
    match, _row = run(
        [crowded, TWIN], Asker(None, None), remembered=(remembered(crowded, files=("b" * 64,)),)
    )

    assert match.status == "needs_reviewer"


def test_every_record_names_the_architect_files_it_was_made_against() -> None:
    match, _row = run([SAME, FAR], Asker(1, 1))

    assert match.details is not None and match.details["architect_files"] == ["a" * 64]


def test_a_renumbered_view_is_never_carried_in_another_views_place() -> None:
    """Review finding 4: the same tag on the same page, but another drawing (another picture)."""
    renumbered = replace(
        TWIN,
        carry_key=view_carry_key("a" * 64, f"{2:064d}", "view-1", "f" * 64, None),
    )
    match, _row = run([SAME, renumbered], Asker(None, None), remembered=(remembered(TWIN),))

    assert match.status == "needs_reviewer"
    assert any("not in the architect's drawings any more" in reason for reason in match.reasons)


def test_an_identical_page_in_a_changed_vendor_file_is_never_carried() -> None:
    """Review finding 8: the vendor's file is part of the item's identity."""
    carried = remembered(TWIN)
    carried = replace(
        carried, vendor_item_key={**carried.vendor_item_key, "vendor_document_sha256": "1" * 64}
    )
    row = result()
    built = replace(
        matcher([SAME, TWIN], page_id=row.page_id, remembered=(carried,)),
        vendor_pages={row.page_id: replace(facts(), document_sha256="2" * 64)},
    )
    found = built.match(
        [row],
        [_slot_page(row.page_id)],
        ask=Asker(None, None),
        readers=READERS,
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    match = found[row.page_index]
    assert match.status == "needs_reviewer" and match.source == "automatic"
    assert [item["remembered"] for item in match.candidate_json].count(True) == 1


FRAME = Box(Decimal(80), Decimal(250), Decimal(300), Decimal(400))


@pytest.mark.parametrize(
    ("where", "verdict"),
    [
        (Box(Decimal(120), Decimal(380), Decimal(200), Decimal(390)), CodeVerdict.REFERENCE),
        (Box(Decimal(420), Decimal(380), Decimal(500), Decimal(390)), CodeVerdict.GEOMETRY_TIE),
    ],
    ids=["inside-the-vendors-drawing", "elsewhere-on-the-sheet"],
)
def test_only_a_reference_inside_the_vendors_drawing_counts(where: Box, verdict: str) -> None:
    """Review finding 3: a reference printed elsewhere on the sheet may name another drawing."""
    named = indexed(WIDTHS, number=1, bubble="4 QX 1.1")
    twin = indexed(WIDTHS, number=2, bubble="6 QX 1.1")
    row = result()
    page = replace(facts(), view_boxes=(FRAME,), phrases=(("REF 4/QX 1.1", where),))
    found = replace(
        matcher([named, twin], page_id=row.page_id), vendor_pages={row.page_id: page}
    ).match(
        [row],
        [_slot_page(row.page_id)],
        ask=Asker(1, 1),
        readers=READERS,
        ask_the_ais=True,
        store=None,
        effort="high",
    )

    assert found[row.page_index].code.verdict == verdict


class ViewAsker:
    """Each reader says yes to the views it is given (by view key), no to the rest."""

    def __init__(self, yes: Mapping[str, set[str]]) -> None:
        self.yes = yes
        self.jobs: list[CropJob] = []
        self.calls = 0

    def __call__(self, jobs: Sequence[CropJob]) -> Mapping[tuple[str, str], object]:
        self.calls += 1
        self.jobs.extend(jobs)
        answers: dict[tuple[str, str], object] = {}
        for job in jobs:
            assert job.question_packet is not None
            shown = list(job.question_packet["architect_view_ids"])  # type: ignore[call-overload]
            same = tuple(
                "yes" if key in self.yes.get(job.model_id, set()) else "no" for key in shown
            )
            pick = same.index("yes") + 1 if same.count("yes") == 1 else 0
            answers[(job.key, job.model_id)] = ArchMatchAnswer(job.model_id, pick, same, "drawn")
        return answers


def _many(count: int) -> list[IndexedView]:
    return [indexed(None, number=n) for n in range(1, count + 1)]


def test_without_a_code_pick_the_ais_see_every_view_and_only_order_the_list() -> None:
    views = _many(7)
    both, one = str(views[4].view.view_id), str(views[6].view.view_id)
    asker = ViewAsker({OPUS: {both, one}, SONNET: {both}})

    match, _row = run(views, asker)

    assert match.status == "needs_reviewer" and match.chosen is None
    assert match.code.pick is None
    assert asker.calls == 1, "one batch"
    keys = sorted({job.key for job in asker.jobs})
    assert keys == ["p0:arch-match:1", "p0:arch-match:2", "p0:arch-match:3"]
    assert len(asker.jobs) == 6
    assert [job.architect_candidates for job in asker.jobs if job.model_id == OPUS] == [3, 3, 1]
    order = [item["view_id"] for item in match.candidate_json]
    assert order[:2] == [both, one]
    assert order[2:] == [
        str(view.view.view_id) for view in views if str(view.view.view_id) not in (both, one)
    ]
    first, second = match.candidate_json[:2]
    assert "both AIs: yes" in first["evidence"] and first["rank"] == 1
    assert sorted(first["ai_picked_by"]) == sorted([OPUS, SONNET])
    assert second["ai_picked_by"] == [OPUS]
    assert {pick["group"] for pick in match.ai_picks} == {1, 2, 3}
    assert all(isinstance(pick["packet_sha256"], str) for pick in match.ai_picks)
    assert match.details is not None and len(match.details["questions"]) == 3


def test_every_view_is_asked_about_up_to_the_cap_and_the_reasons_say_so() -> None:
    views = _many(17)
    asker = ViewAsker({})

    match, _row = run(views, asker)

    shown = {key for job in asker.jobs for key in job.question_packet["architect_view_ids"]}  # type: ignore[index, union-attr]
    assert len(shown) == ALL_VIEWS_CAP == 15
    assert len({job.key for job in asker.jobs}) == 5
    assert any("shown the first 15" in reason for reason in match.reasons)
    unseen = [item for item in match.candidate_json if item["shown_number"] is None]
    assert len(unseen) == 2 and all("not shown to the AIs" in i["evidence"] for i in unseen)
    assert match.status == "needs_reviewer"


def test_with_a_code_pick_the_ais_get_one_question_about_its_top_views() -> None:
    views = [SAME, FAR, *_many(5)]
    asker = Asker(1, 1)

    match, _row = run(views, asker)

    assert {job.key for job in asker.jobs} == {"p0:arch-match"}
    assert all(job.architect_candidates == 3 for job in asker.jobs)
    assert match.status == "auto_matched"
