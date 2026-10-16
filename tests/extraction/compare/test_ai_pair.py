"""The AI pair check: two AIs choose the vendor line for an unpaired architect span; code decides.

Verification for: `extraction/compare/ai_pair.py` and `workflow/compare_ai_pair.py` (no database).
The models are stand-ins that answer what each test says; no model is called and every number is
made up. The architect's elevation runs 0..300 pt across, the vendor's 0..600 pt; both are heights
and widths drawn by hand from plain values.
"""

from __future__ import annotations

import io
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from extraction.compare.ai_pair import (
    AI_PAIR_ENV,
    AI_PAIR_PROMPT_ID,
    CLAUDE_PAIR,
    MEASURED_AI_PAIR_SETTINGS,
    PdfPictures,
    ai_pair_enabled,
    ai_pair_schema,
    pair_with_ais,
)
from extraction.compare.dimensions import ArchitectDimension, Axis, DrawnDimension
from extraction.compare.page import ArchitectViewDimensions, ComparePage, VendorViewDimensions
from extraction.compare.pair import Pairing, PairStatus
from extraction.form_reader.bedrock import AttemptUsage
from extraction.geometry.rows import Box, TickSource
from extraction.ink import InkClass
from workflow.compare_ai_pair import ai_pair_unpaired

OPUS, SONNET = CLAUDE_PAIR
ON = {AI_PAIR_ENV: "true"}


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _width(start: float, end: float, *, y: float, label: bool = True) -> DrawnDimension:
    middle = (start + end) / 2
    return DrawnDimension(
        axis=Axis.HORIZONTAL,
        offset_pt=_d(y),
        start_pt=_d(start),
        end_pt=_d(end),
        row=0,
        index=0,
        label_boxes=(Box(_d(middle - 5), _d(y - 8), _d(middle + 5), _d(y - 1)),) if label else (),
        label_text=None,
        tick_source=TickSource.SLASH,
    )


def _height(start: float, end: float, *, x: float) -> DrawnDimension:
    middle = (start + end) / 2
    return DrawnDimension(
        axis=Axis.VERTICAL,
        offset_pt=_d(x),
        start_pt=_d(start),
        end_pt=_d(end),
        row=0,
        index=0,
        label_boxes=(Box(_d(x - 8), _d(middle - 5), _d(x - 1), _d(middle + 5)),),
        label_text=None,
        tick_source=TickSource.SLASH,
    )


def _arch_dim(number: int, span: DrawnDimension) -> ArchitectDimension:
    return ArchitectDimension(
        number=number,
        text='30"',
        label_box=Box(Decimal(0), Decimal(0), Decimal(1), Decimal(1)),
        printed_inches=Fraction(30),
        inches=Fraction(30),
        held_reason=None,
        span=span,
        placement_reason="on a line",
        drawn_inches=Fraction(30),
    )


ARCH_BOX = Box(Decimal(0), Decimal(0), Decimal(300), Decimal(200))
VENDOR_BOX = Box(Decimal(400), Decimal(0), Decimal(1000), Decimal(400))


def _sheet(
    *arch_spans: DrawnDimension,
    vendor_lines: Sequence[DrawnDimension],
    vendor_kind: str = "elevation",
) -> tuple[ComparePage, tuple[Pairing, ...]]:
    dims = tuple(_arch_dim(n, span) for n, span in enumerate(arch_spans, start=1))
    arch = ArchitectViewDimensions(
        annotation_index=0,
        box=ARCH_BOX,
        title=None,
        scale_note=None,
        points_per_inch=Decimal(2),
        scale_reason="witnessed",
        dimensions=dims,
        # The run: the architect's dimensioned widths reach from 0 to 300.
        lines=(_width(0, 300, y=190), *arch_spans),
        kind="elevation",
    )
    vendor = VendorViewDimensions(
        number=1,
        panel_box=VENDOR_BOX,
        heading="ELEVATION",
        extent=VENDOR_BOX,
        points_per_inch=Fraction(4),
        scale_reason="printed",
        lines=tuple(vendor_lines),
        kind=vendor_kind,
    )
    page = ComparePage(0, (arch,), (vendor,), ())
    pairings = tuple(
        Pairing(0, 0, dim, PairStatus.UNPAIRED, None, None, None, None, "geometry found none")
        for dim in dims
    )
    return page, pairings


def _png() -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (56, 56), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


class _Pictures:
    """Stand-in pictures: a blank PNG, and the ink each label box is said to be."""

    def __init__(self, gv_labels: Sequence[Box] = ()) -> None:
        self.gv = set(gv_labels)
        self.drawn: list[int] = []

    def picture(self, page_index, architect_view, architect, vendor_view, candidates):  # type: ignore[no-untyped-def]
        self.drawn.append(len(candidates))
        return _png()

    def ink(self, page_index: int, box: Box) -> InkClass | None:
        return InkClass.GV if box in self.gv else InkClass.VENDOR

    def page_sha256(self, page_index: int) -> str:
        return "0" * 64


class _Model:
    """A stand-in model: answers each question with the pick (and inches) the test sets."""

    def __init__(self, picks: Sequence[str], inches: str = '30"') -> None:
        self.picks = list(picks)
        self.inches = inches
        self.calls = 0

    def converse(self, **request: Any) -> Mapping[str, Any]:
        pick = self.picks[min(self.calls, len(self.picks) - 1)]
        self.calls += 1
        body = {
            "pick": pick,
            "vendor_label": "" if not pick.isdigit() else "813 [32]",
            "vendor_inches": "" if not pick.isdigit() else self.inches,
            "why": "made-up answer",
        }
        return {
            "stopReason": "end_turn",
            "output": {"message": {"content": [{"text": json.dumps(body)}]}},
            "usage": {"inputTokens": 1000, "outputTokens": 100},
        }


def _run(
    page: ComparePage,
    pairings: Sequence[Pairing],
    opus: _Model,
    sonnet: _Model,
    *,
    pictures: _Pictures | None = None,
    spend_guard: Any = None,
) -> tuple[tuple[Pairing, ...], list[AttemptUsage]]:
    attempts: list[AttemptUsage] = []
    out = ai_pair_unpaired(
        None,
        [page],
        pairings,
        pdf=b"",
        document_version_id="synthetic-version",
        clients={OPUS: opus, SONNET: sonnet},
        record_attempt=attempts.append,
        spend_guard=spend_guard,
        environ=ON,
        pictures=pictures or _Pictures(),
    )
    return out, attempts


# The architect's span runs 100..160 of 0..300 (a third of the way along); the vendor's matching
# line runs 600..720 of 400..1000 (the same third), and a decoy at the far end runs 880..1000.
def _match() -> DrawnDimension:
    return _width(600, 720, y=300)


def _decoy() -> DrawnDimension:
    return _width(880, 1000, y=300)


def _run_line() -> DrawnDimension:
    return _width(400, 1000, y=380)


def test_both_ais_choose_the_same_line_and_code_agrees_so_it_is_paired() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match(), _decoy()])

    out, attempts = _run(page, pairings, _Model(["2"]), _Model(["2"]))

    (paired,) = out
    assert paired.status is PairStatus.PAIRED
    assert paired.paired_by == "two AIs"
    assert paired.vendor == _match()
    assert paired.vendor_label == _match().label_boxes[0]
    assert paired.vendor_inches == Fraction(30)
    assert paired.vendor_inches_reason is not None and "both AIs" in paired.vendor_inches_reason
    assert paired.vendor_inches_drawn == Fraction(30)  # 120 pt at 4 pt per inch: geometry only
    assert {a.model_id for a in attempts} == {OPUS, SONNET}
    assert all(a.prompt_id == AI_PAIR_PROMPT_ID for a in attempts)


def test_the_ais_disagree_so_it_stays_unpaired_with_the_reason() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match(), _decoy()])

    (out,), _ = _run(page, pairings, _Model(["2"]), _Model(["3"]))

    assert out.status is PairStatus.UNPAIRED
    assert out.vendor is None
    assert "the two AIs disagree" in out.reason
    assert out.reason.startswith("geometry found none"), "the geometry reason is kept"


def test_both_say_none_so_it_stays_unpaired() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])

    (out,), _ = _run(page, pairings, _Model(["none"]), _Model(["none"]))

    assert out.status is PairStatus.UNPAIRED
    assert "both AIs said no vendor line measures this span" in out.reason


def test_one_says_none_or_unsure_so_it_stays_unpaired() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])

    (out,), _ = _run(page, pairings, _Model(["2"]), _Model(["unsure"]))

    assert out.status is PairStatus.UNPAIRED
    assert "did not both choose a line" in out.reason


def test_a_line_of_the_other_axis_is_refused_even_when_both_choose_it() -> None:
    page, pairings = _sheet(
        _width(100, 160, y=150), vendor_lines=[_run_line(), _height(100, 300, x=650)]
    )

    (out,), _ = _run(page, pairings, _Model(["2"]), _Model(["2"]))

    assert out.status is PairStatus.UNPAIRED
    assert "vertical line" in out.reason and "refused" in out.reason


def test_a_line_whose_number_is_in_gv_ink_is_refused() -> None:
    match = _match()
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), match])

    (out,), _ = _run(
        page,
        pairings,
        _Model(["2"]),
        _Model(["2"]),
        pictures=_Pictures(gv_labels=[match.label_boxes[0]]),
    )

    assert out.status is PairStatus.UNPAIRED
    assert "GV's coloured markup" in out.reason


def test_a_line_at_the_other_end_of_the_run_is_refused_by_geometry() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match(), _decoy()])

    (out,), _ = _run(page, pairings, _Model(["3"]), _Model(["3"]))

    assert out.status is PairStatus.UNPAIRED
    assert "another part of the run" in out.reason


def test_the_budget_stops_the_asking_and_every_dimension_says_so() -> None:
    from app.runs.rates import load_model_rates
    from extraction.slot_reader.anthropic import BatchSpendGuard

    rates = load_model_rates(
        Path(__file__).resolve().parents[3] / "deploy" / "model_rates.us-east-1.json"
    )
    page, pairings = _sheet(
        _width(100, 160, y=150),
        _width(20, 60, y=150),
        vendor_lines=[_run_line(), _match(), _width(430, 520, y=300)],
    )
    opus, sonnet = _Model(["2"]), _Model(["2"])

    out, attempts = _run(
        page, pairings, opus, sonnet, spend_guard=BatchSpendGuard(Decimal("0.000001"), rates)
    )

    assert opus.calls == sonnet.calls == 0
    assert attempts == []
    assert all(p.status is PairStatus.UNPAIRED for p in out)
    assert all("budget" in p.reason for p in out)


def test_one_line_chosen_for_two_architect_dimensions_pairs_neither() -> None:
    page, pairings = _sheet(
        _width(100, 160, y=150), _width(110, 170, y=120), vendor_lines=[_run_line(), _match()]
    )

    out, _ = _run(page, pairings, _Model(["2"]), _Model(["2"]))

    assert [p.status for p in out] == [PairStatus.UNPAIRED, PairStatus.UNPAIRED]
    assert all("the same vendor line for 2" in p.reason for p in out)


def test_a_geometry_pair_is_never_changed_or_asked_about() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])
    geometry = Pairing(
        0, 0, pairings[0].architect, PairStatus.PAIRED, 1, _match(), Fraction(30), "floor", "geo"
    )
    pictures = _Pictures()

    out, attempts = _run(page, [geometry], _Model(["1"]), _Model(["1"]), pictures=pictures)

    assert out == (geometry,)
    assert attempts == [] and pictures.drawn == []


def test_the_vendor_number_needs_the_same_inches_from_both_and_never_millimetres() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])

    (differ,), _ = _run(page, pairings, _Model(["2"], '30"'), _Model(["2"], '31"'))
    (mm,), _ = _run(page, pairings, _Model(["2"], "813 mm"), _Model(["2"], "813 mm"))

    assert differ.status is PairStatus.PAIRED and differ.vendor_inches is None
    assert differ.vendor_inches_reason is not None and differ.vendor_inches_reason.startswith(
        "unread"
    )
    assert mm.status is PairStatus.PAIRED and mm.vendor_inches is None
    assert mm.vendor_inches_reason is not None and "millimetres" in mm.vendor_inches_reason


def test_a_stored_reading_of_the_label_is_used_first() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])
    seen: list[Pairing] = []

    def stored(pairing: Pairing) -> Fraction | None:
        seen.append(pairing)
        return Fraction(61, 2)

    out = ai_pair_unpaired(
        None,
        [page],
        pairings,
        pdf=b"",
        document_version_id="synthetic-version",
        clients={OPUS: _Model(["2"]), SONNET: _Model(["2"])},
        record_attempt=lambda _a: None,
        stored_vendor_inches=stored,
        environ=ON,
        pictures=_Pictures(),
    )

    assert out[0].vendor_inches == Fraction(61, 2)
    assert seen and seen[0].vendor_label == _match().label_boxes[0]


def test_a_stored_answer_is_replayed_not_paid_again() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])
    answer = json.dumps({"pick": "2", "vendor_label": "", "vendor_inches": '30"', "why": "x"})
    opus, sonnet = _Model(["1"]), _Model(["1"])

    out = ai_pair_unpaired(
        None,
        [page],
        pairings,
        pdf=b"",
        document_version_id="synthetic-version",
        clients={OPUS: opus, SONNET: sonnet},
        record_attempt=lambda _a: None,
        stored=lambda _model, _packet: answer,
        environ=ON,
        pictures=_Pictures(),
    )

    assert opus.calls == sonnet.calls == 0
    assert out[0].status is PairStatus.PAIRED and out[0].vendor == _match()


def test_switched_off_nothing_is_asked_and_nothing_changes() -> None:
    page, pairings = _sheet(_width(100, 160, y=150), vendor_lines=[_run_line(), _match()])
    opus, sonnet = _Model(["2"]), _Model(["2"])

    out = ai_pair_unpaired(
        None,
        [page],
        pairings,
        pdf=b"",
        document_version_id="synthetic-version",
        clients={OPUS: opus, SONNET: sonnet},
        record_attempt=lambda _a: None,
        environ={},
        pictures=_Pictures(),
    )

    assert out == pairings
    assert opus.calls == sonnet.calls == 0


def test_the_switch_must_be_a_yes_or_no() -> None:
    assert ai_pair_enabled({}) is False
    assert ai_pair_enabled({AI_PAIR_ENV: "1"}) is True
    with pytest.raises(ValueError):
        ai_pair_enabled({AI_PAIR_ENV: "maybe"})


def test_a_vendor_view_of_another_kind_is_never_asked_about() -> None:
    page, pairings = _sheet(
        _width(100, 160, y=150), vendor_lines=[_run_line(), _match()], vendor_kind="plan"
    )
    pictures = _Pictures()

    run = pair_with_ais(
        [page],
        pairings,
        pictures=pictures,
        clients={OPUS: _Model(["2"]), SONNET: _Model(["2"])},
        document_version_id="synthetic-version",
        record_attempt=lambda _a: None,
    )

    assert run.asked == 0 and pictures.drawn == []
    assert "no vendor view on the sheet is a elevation" in run.pairings[0].reason


def test_only_real_dimension_lines_are_numbered() -> None:
    unlabelled = _width(600, 720, y=250, label=False)
    page, pairings = _sheet(
        _width(100, 160, y=150), vendor_lines=[_run_line(), _match(), unlabelled]
    )
    pictures = _Pictures()

    pair_with_ais(
        [page],
        pairings,
        pictures=pictures,
        clients=None,
        document_version_id="synthetic-version",
        record_attempt=lambda _a: None,
    )

    assert pictures.drawn == [2]


def test_the_answer_shape_is_a_closed_list() -> None:
    schema = ai_pair_schema(3)
    assert schema["properties"]["pick"]["enum"] == ["1", "2", "3", "none", "unsure"]  # type: ignore[index]
    with pytest.raises(ValueError):
        ai_pair_schema(0)


def test_the_pdf_picture_fits_the_pair_unresized() -> None:
    """A smoke test on the synthetic combined sheet: the picture is a PNG the pair reads as is."""
    from extraction.compare.page import MEASURED_COMPARE_SETTINGS, read_compare_page
    from extraction.slot_reader.claude_output import picture_fits, png_size
    from tests.extraction.architect.combined_sheet import combined_sheet

    data = combined_sheet()
    page = read_compare_page(data, 0, settings=MEASURED_COMPARE_SETTINGS, dpi=150)
    (arch,) = page.architect
    dim = next(d for d in arch.dimensions if d.span is not None and d.span.label_boxes)
    assert dim.span is not None
    (vendor,) = page.vendor
    pictures = PdfPictures(data, MEASURED_AI_PAIR_SETTINGS)

    png = pictures.picture(0, arch, dim, vendor, [(dim.span, dim.span.label_boxes[0])])

    width, height = png_size(png)
    assert all(picture_fits(width, height, model) for model in CLAUDE_PAIR)
    assert len(pictures.page_sha256(0)) == 64
