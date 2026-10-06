"""Made-up adversarial inputs exercise the existing reader/evidence guards, never client data."""

from fractions import Fraction

import pytest

from eval.form_first_safety import WidthInputs, published_ct_width_snapshot, run_width_check
from extraction.form_reader.agreement import ComparedReading, compare_page_answers
from extraction.form_reader.mapping import map_page_to_fields
from extraction.form_reader.schema import CountertopForm, FormDimension, PageFormAnswer
from extraction.ink import InkClass, InkLabel, PageInk
from verdict.outcomes import Outcome
from workflow.form_reader import apply_ink, guard_located_comparison

READERS = ("us.moonshotai.kimi-k3", "qwen.qwen3-vl-235b-a22b")


def _dimension(
    text: str | None,
    *,
    kind: str = "cabinet",
    stacked: bool = False,
    combined: bool = False,
    readable: bool = True,
    position: int = 0,
) -> FormDimension:
    return FormDimension(
        text=text,
        whole=None,
        numerator=None,
        denominator=None,
        stacked=stacked,
        kind=kind,
        combined=combined,
        readable=readable,
        box=None,
        position=position,
    )


def _answer(
    *,
    overall: FormDimension | None = None,
    scope: str | None = "run",
    chain: tuple[FormDimension, ...] = (),
    page: int = 1,
) -> PageFormAnswer:
    return PageFormAnswer(
        page_index=page,
        countertops=[CountertopForm(overall=overall, overall_scope=scope, chain=list(chain))],
    )


def _compare(left: PageFormAnswer, right: PageFormAnswer) -> tuple[ComparedReading, ...]:
    return compare_page_answers(left, right, first_maker=READERS[0], second_maker=READERS[1])


def test_stacked_fraction_misread_is_review_only() -> None:
    comparison = _compare(
        _answer(overall=_dimension('8"')),
        _answer(overall=_dimension('8"', stacked=True)),
    )[0]

    assert comparison.state == "review_required"
    assert comparison.reason == "stacked"
    assert comparison.value is None


def test_wall_scope_overall_is_review_only() -> None:
    comparison = _compare(
        _answer(overall=_dimension('8"'), scope="wall"),
        _answer(overall=_dimension('8"'), scope="wall"),
    )[0]

    assert comparison.state == "review_required"
    assert comparison.reason == "scope-not-run"
    assert comparison.value is None


def test_two_countertops_on_a_page_produce_no_field_proposals() -> None:
    first = PageFormAnswer(
        page_index=1,
        countertops=[
            CountertopForm(overall=_dimension('8"', position=0), overall_scope="run", chain=[]),
            CountertopForm(overall=_dimension('9"', position=0), overall_scope="run", chain=[]),
        ],
    )
    second = PageFormAnswer.model_validate(first.model_dump())

    readings = _compare(first, second)
    mapping = map_page_to_fields(readings, first_countertop_count=2, second_countertop_count=2)

    assert mapping.proposals == ()
    assert len(mapping.questions) == 2
    assert all("multiple countertops" in question.review_reason for question in mapping.questions)


def test_garbled_reader_output_is_review_only() -> None:
    comparison = _compare(
        _answer(overall=_dimension('8"')),
        _answer(overall=_dimension(None, readable=False)),
    )[0]

    assert comparison.state == "review_required"
    assert comparison.reason == "unreadable"
    assert comparison.value is None


@pytest.mark.parametrize("ink_class", [InkClass.GV, InkClass.COVERED])
def test_red_or_covered_gv_correction_never_seals_even_when_pieces_add_up(
    ink_class: InkClass,
) -> None:
    readings = _compare(
        _answer(
            overall=_dimension('8"'),
            chain=(_dimension('4"', position=1), _dimension('4"', kind="filler", position=2)),
        ),
        _answer(
            overall=_dimension('8"'),
            chain=(_dimension('4"', position=1), _dimension('4"', kind="filler", position=2)),
        ),
    )
    image_polygon = ((10, 10), (20, 10), (20, 20), (10, 20))
    page_ink = PageInk(
        dpi=72,
        labels=(InkLabel("synthetic mark", (10, 10, 20, 20), ink_class),),
        paths=(),
        stamps=(),
    )

    held, detected_ink, _reviewer_text = apply_ink(readings[0], image_polygon, page_ink)
    mapping = map_page_to_fields(
        (held, *readings[1:]), first_countertop_count=1, second_countertop_count=1
    )
    # The synthetic component chain exactly totals the overall, but the overall is still held.
    finding = run_width_check(
        WidthInputs(
            overall=None,
            cabinets=(Fraction(4),),
            fillers=(Fraction(4),),
            wall_layout="back_only",
            field_cut=Fraction(1),
        ),
        published_ct_width_snapshot(),
    )

    assert detected_ink is ink_class
    assert held.state == "review_required"
    assert held.value is None
    assert all(
        proposal.field_key != "SHOP:countertop_overall_width" for proposal in mapping.proposals
    )
    assert finding.outcome is Outcome.NOT_FOUND
    assert finding.trace is None


def test_missing_or_edge_cut_location_cannot_clear_an_agreement() -> None:
    comparison = _compare(
        _answer(overall=_dimension('8"')),
        _answer(overall=_dimension('8"')),
    )[0]

    guarded = guard_located_comparison(comparison, None, lambda _polygon: False)
    mapping = map_page_to_fields((guarded,), first_countertop_count=1, second_countertop_count=1)

    assert guarded.state == "review_required"
    assert guarded.reason == "label-location-unknown"
    assert guarded.value is None
    assert mapping.proposals == ()


def test_missing_filler_is_not_found_by_the_published_rule() -> None:
    finding = run_width_check(
        WidthInputs(
            overall=Fraction(8),
            cabinets=(Fraction(4),),
            fillers=None,
            wall_layout="back_only",
            field_cut=Fraction(1),
        ),
        published_ct_width_snapshot(),
    )

    assert finding.outcome is Outcome.NOT_FOUND
    assert finding.trace is None
