"""What each dimension measures from and to (`dim_ends`), and the sink centre on the sink base
(`sink_centre`): made-up drawings, no client data.

The vendor's plan (4 points to the inch) draws a 46" top whose left end carries GV's 1" field cut
(a 46" overall over the 45" top, FIELD CUT printed), a 16" x 10" sink cut-out with its model
inside, and the cut-out's centre printed as two dimensions in a row from the top's ends. The
reference (3 points to the inch) prints three cabinets, 12" + 20" sink base + 12", with their
tags, the 45" top 1" past the last cabinet (the overhang), and the sink drawn centred on the sink
base in plan.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from app.review.assistant import dim_ends, escalation, field_cut, proof, sink_centre
from app.review.assistant.chat_contract import ChatFindingSettledOut
from app.review.assistant.chat_trust import judged
from app.review.assistant.escalation import EscalationContext, escalate

type Point = tuple[float, float]
type Segment = tuple[Point, Point]

VENDOR = "vendor.pdf"
REFERENCE = "reference.pdf"


@dataclass
class Drawing:
    """One page: inches to points through its own origin and scale."""

    side: str
    scale: float
    x0: float = 100.0
    segments: list[Segment] = field(default_factory=list)
    labels: list[dict[str, Any]] = field(default_factory=list)
    phrases: list[dict[str, Any]] = field(default_factory=list)

    def x(self, inches: float) -> float:
        return self.x0 + inches * self.scale

    def line(self, a: Point, b: Point) -> None:
        self.segments.append((a, b))

    def rect(self, x0: float, y0: float, x1: float, y1: float) -> None:
        self.line((x0, y0), (x1, y0))
        self.line((x1, y0), (x1, y1))
        self.line((x1, y1), (x0, y1))
        self.line((x0, y1), (x0, y0))

    def phrase(self, text: str, x: float, y: float) -> None:
        self.phrases.append(
            {
                "text": text,
                "box": [x - 3 * len(text) / 2, y - 4, x + 3 * len(text) / 2, y + 4],
                "side": self.side,
            }
        )

    def dim(
        self,
        a_in: float,
        b_in: float,
        row: float,
        value: float,
        text: str | None = None,
        reach: float | None = None,
    ) -> str:
        """A ticked dimension line from a_in to b_in at `row`, its text centred above the line,
        extension lines up to `reach` (a y on the page)."""
        a, b = self.x(a_in), self.x(b_in)
        self.line((a, row), (b, row))
        for at in (a, b):
            self.line((at - 1.2, row + 1.2), (at + 1.2, row - 1.2))
            if reach is not None:
                self.line((at, min(reach, row)), (at, max(reach, row) + 2))
        key = f"D{len(self.labels) + 1}"
        centre = (a + b) / 2
        words = text or _inches(value)
        self.labels.append(
            {
                "id": key,
                "text": words,
                "inches": str(value),
                "box": [centre - 8, row - 11, centre + 8, row - 2],
                "side": self.side,
                "orientation": "h",
                "route": "text",
            }
        )
        return key


def _inches(value: float) -> str:
    whole = int(value)
    part = Fraction(value - whole).limit_denominator(16)
    return f'{whole} {part}"' if part else f'{whole}"'


def vendor_plan(
    left_in: float, right_in: float, *, cut_at: str = "left"
) -> tuple[Drawing, field_cut.PageFieldCuts]:
    """The vendor's plan: the 46" top (1" field cut at `cut_at`), the cut-out centred `left_in`
    from the top's left end, printed as `left_in` + `right_in`."""
    d = Drawing("vendor", 4.0)
    front, back = 300.0, 200.0
    d.rect(d.x(0), back, d.x(46), front)
    centre = left_in
    d.rect(d.x(centre - 8), 230.0, d.x(centre + 8), 270.0)
    d.phrase("KOHLER K-1234", d.x(centre), 250.0)
    overall = d.dim(0, 46, 120.0, 46)
    part = d.dim(1, 46, 150.0, 45) if cut_at == "left" else d.dim(0, 45, 150.0, 45)
    d.phrase("FIELD CUT", d.x(0) - 40, 140.0)
    first = d.dim(0, left_in, 330.0, left_in, reach=front)
    second = d.dim(left_in, 46, 330.0, right_in, reach=front)
    del part, first, second
    cuts = field_cut.PageFieldCuts(Fraction(1), frozenset(), {overall: 1})
    return d, cuts


def reference_plan(*, centred: bool = True) -> Drawing:
    """The reference: 12" + 20" sink base + 12" with their tags, the 45" top 1" past the last
    cabinet, and the sink drawn (centred on the base, or 3" off)."""
    d = Drawing("architect", 3.0)
    d.dim(0, 45, 310.0, 45)
    d.dim(0, 12, 350.0, 12)
    d.dim(12, 32, 350.0, 20)
    d.dim(32, 44, 350.0, 12)
    for text, middle in (("VDB12-ADA", 6.0), ("VSB20-ADA", 22.0), ("VDB12-ADA", 38.0)):
        d.phrase(text, d.x(middle), 334.0)
    d.rect(d.x(0), 370.0, d.x(45), 440.0)  # the top in plan
    sink = 22.0 if centred else 25.0
    d.rect(d.x(sink - 7), 385.0, d.x(sink + 7), 415.0)
    d.phrase("SINK", d.x(sink), 400.0)
    return d


def _page(d: Drawing, file: str, page: int, cuts: Any = None) -> dim_ends.Page:
    return dim_ends.page_of(file, page, d.labels, d.phrases, d.segments, cuts)


# ---- what each end lands on ------------------------------------------------------------------------


def test_each_end_is_named_by_what_it_lands_on() -> None:
    vendor, cuts = vendor_plan(22, 24)
    page = _page(vendor, VENDOR, 1, cuts)
    first, second = page.ends("D3"), page.ends("D4")
    assert first is not None and second is not None
    assert first.low.kinds == {"field_cut_end"} and first.high.kinds == {"sink_centre"}
    assert second.low.kinds == {"sink_centre"} and "top_edge" in second.high.kinds
    top = page.ends("D2")
    assert top is not None and "wall" in top.low.kinds  # the 39" top's wall end

    reference = _page(reference_plan(), REFERENCE, 1)
    base = reference.ends("D3")
    assert base is not None and base.low.kinds == {"cabinet_face"} == base.high.kinds
    assert reference.cabinets["D3"].sink_base and not reference.cabinets["D2"].sink_base
    ref_top = reference.ends("D1")
    assert ref_top is not None and ref_top.low.kinds == {"cabinet_face"}
    assert {"overhang_end", "top_edge"} <= ref_top.high.kinds


def test_an_end_code_cannot_name_proves_nothing() -> None:
    d = Drawing("vendor", 4.0)
    d.dim(0, 30, 100.0, 30)
    d.dim(0, 30, 160.0, 30)
    page = _page(d, VENDOR, 1)
    one, two = page.ends("D1"), page.ends("D2")
    assert one is not None and two is not None
    assert not one.low.kinds and not one.high.kinds
    assert dim_ends.alike(one, two)[0] == "unknown"


def test_a_sink_centre_is_never_compared_with_a_cabinet_width() -> None:
    vendor = _page(vendor_plan(22, 24)[0], VENDOR, 1, vendor_plan(22, 24)[1])
    reference = _page(reference_plan(), REFERENCE, 1)
    verdict, why = dim_ends.alike(vendor.ends("D3"), reference.ends("D3"))  # type: ignore[arg-type]
    assert verdict == "unlike"
    assert why == (
        "the vendor's 22\" measures from the field-cut end of the top to the sink's centre line; "
        "the architect's 20\" from a cabinet's side to a cabinet's side"
    )


def test_the_field_cut_relates_the_overall_to_the_reference_top() -> None:
    vendor, cuts = vendor_plan(23, 23)
    page = _page(vendor, VENDOR, 1, cuts)
    reference = _page(reference_plan(), REFERENCE, 1)
    overall, top = page.ends("D1"), reference.ends("D1")
    assert overall is not None and top is not None
    assert dim_ends.alike(overall, top)[0] == "unlike"  # a field-cut end against a cabinet side
    assert dim_ends.field_cut_relief(overall, top) == 1  # 46" less one field cut = 45"
    part = page.ends("D2")
    assert part is not None and dim_ends.alike(part, top)[0] != "unlike"


def test_a_mirrored_pair_is_read_the_other_way_round() -> None:
    reference = _page(reference_plan(), REFERENCE, 1)
    base = reference.ends("D3")
    assert base is not None
    sink = dim_ends.Ends(
        dim_ends.Span("V1", "h", 0, 0, 10, Fraction(20)),
        dim_ends.End(0, frozenset({"cabinet_face"})),
        dim_ends.End(10, frozenset({"sink_centre"})),
    )
    mirrored = dim_ends.Ends(sink.span, sink.high, sink.low)
    assert dim_ends.alike(sink, base)[0] == "unlike"
    assert dim_ends.alike(mirrored, base)[0] == "unlike"
    same = dim_ends.Ends(sink.span, base.high, base.low)
    assert dim_ends.alike(same, base)[0] == "alike"


def test_a_notch_step_and_a_wall_band_are_seen_in_the_line_work() -> None:
    d = Drawing("vendor", 4.0)
    # A top whose left end steps 1" in at the back (a notch), drawn as an outline.
    d.line((100, 100), (300, 100))
    d.line((300, 100), (300, 200))
    d.line((300, 200), (96, 200))
    d.line((96, 200), (96, 150))
    d.line((96, 150), (100, 150))  # the step: 1"
    d.line((100, 150), (100, 100))
    d.dim(0, 50, 80.0, 50)
    page = _page(d, VENDOR, 1)
    steps = page.notches_in((90, 90, 120, 210))
    assert [(s.axis, s.at) for s in steps] == [("h", 150)]
    assert page.notches_in((200, 90, 310, 210)) == []

    ref = Drawing("architect", 3.0)
    ref.dim(0, 30, 300.0, 30)
    right = ref.x(30)
    ref.line((right, 200), (right, 290))  # the wall's near face
    ref.line((right + 15, 190), (right + 15, 295))  # its far face, 5" beyond
    walled = _page(ref, REFERENCE, 1)
    span = walled.spans["D1"]
    assert dim_ends.wall_band(walled, span, span.b, 1)
    assert not dim_ends.wall_band(walled, span, span.a, -1)


# ---- the sink centre --------------------------------------------------------------------------------


def _compare(
    left: float, right: float, *, cut_at: str = "left", centred: bool = True
) -> sink_centre.Compared:
    vendor, cuts = vendor_plan(left, right, cut_at=cut_at)
    sinks = sink_centre.vendor_sinks(_page(vendor, VENDOR, 1, cuts))
    bases = sink_centre.reference_bases(_page(reference_plan(centred=centred), REFERENCE, 1))
    assert len(sinks) == 1
    if not bases:
        raise LookupError("no base")
    return sink_centre.compare(sinks[0], bases[0])


def test_a_sink_off_the_base_centre_is_a_difference() -> None:
    found = _compare(22, 24)
    assert found.outcome == "different"
    assert found.why == (
        "The vendor's sink centre is 1\" off the sink base's centre: from the top's right end, the "
        "vendor's cut-out centre is 24\", the reference's sink base centre 23\"."
    )
    assert '12" + 20"/2' in found.architect


def test_a_sink_on_the_base_centre_is_right() -> None:
    found = _compare(23, 23)
    assert found.outcome == "same"


def test_a_reversed_unit_is_compared_from_the_matching_ends() -> None:
    # The vendor draws the unit reversed: its field cut at the right, the sink 24" from the left.
    assert _compare(24, 22, cut_at="right").outcome == "different"
    assert _compare(23, 23, cut_at="right").outcome == "same"


def test_no_comparison_when_the_reference_does_not_draw_the_sink_centred() -> None:
    try:
        _compare(22, 24, centred=False)
    except LookupError:
        return
    raise AssertionError("an off-centre sink must give no base to compare with")


# ---- the escalation's like-for-like step -----------------------------------------------------------


def _finding(
    vendor_id: str,
    architect_id: str,
    vendor_value: str,
    architect_value: str,
    status: str = "different",
    **more: Any,
) -> dict[str, Any]:
    return {
        "id": "F1",
        "page": 1,
        "vs_page": 1,
        "item": "sink centre line from left",
        "vendor": vendor_value,
        "architect": architect_value,
        "status": status,
        "source": "architect",
        "category": "countertop",
        "created_by": "helper:1",
        "inches": {"vendor": vendor_value.strip('"'), "architect": architect_value.strip('"')},
        "checks": {"printed_vendor": "match", "printed_architect": "match"},
        "dims": {
            "vendor": {
                "id": vendor_id,
                "page": 1,
                "file": VENDOR,
                "inches": vendor_value.strip('"'),
            },
            "architect": {
                "id": architect_id,
                "page": 1,
                "file": REFERENCE,
                "inches": architect_value.strip('"'),
            },
        },
        **more,
    }


def _ctx(left: float, right: float, **drawings: Drawing) -> EscalationContext:
    vendor, cuts = vendor_plan(left, right)
    reference = reference_plan()
    for name, d in drawings.items():
        if name == "vendor":
            vendor = d
        else:
            reference = d
    labels = {(VENDOR, 1): vendor.labels, (REFERENCE, 1): reference.labels}
    phrases = {(VENDOR, 1): vendor.phrases, (REFERENCE, 1): reference.phrases}
    lines = {(VENDOR, 1): vendor.segments, (REFERENCE, 1): reference.segments}
    return EscalationContext(
        main_file=VENDOR,
        labels=labels,
        phrases=phrases,
        pages=dim_ends.pages_reader(
            labels, phrases, lambda f, p: lines.get((f, p)), lambda f, p: cuts, VENDOR
        ),
    )


def test_a_sink_offset_set_against_the_base_width_goes_to_the_sink_centre_check() -> None:
    result = escalate([_finding("D3", "D3", '22"', '20"')], _ctx(22, 24))
    found = judged(result.changed[0])
    assert found["created_by"] == sink_centre.CREATED_BY
    assert proof.proof_group(found) == "likely_problem"  # never red: no track record yet
    assert "1\" off the sink base's centre" in found["why"]
    assert found[sink_centre.FIELD]["outcome"] == "different"


def test_a_sink_offset_set_against_another_cabinet_still_goes_to_the_sink_centre_check() -> None:
    result = escalate([_finding("D3", "D2", '22"', '12"', status="same")], _ctx(22, 24))
    found = judged(result.changed[0])
    assert found["created_by"] == sink_centre.CREATED_BY
    assert proof.proof_group(found) == "likely_problem"


def test_a_right_item_rechecked_on_the_base_centre_still_reads_as_a_chat_finding() -> None:
    # An item already Right, set against another cabinet, re-judged Right on the base centre.
    result = escalate([_finding("D3", "D2", '23"', '12"', status="same")], _ctx(23, 23))
    found = judged(result.changed[0])
    assert found["created_by"] == sink_centre.CREATED_BY and found["status"] == "same"
    assert found["settled"]["was"] == "check"
    ChatFindingSettledOut.model_validate(found["settled"])
    # An answer saved before the fix still opens.
    saved = ChatFindingSettledOut.model_validate({**found["settled"], "was": "same"})
    assert saved.was == "check"


def test_an_unlike_record_says_why_and_blocks_right_and_red() -> None:
    reference = reference_plan()
    reference.phrases = [p for p in reference.phrases if p["text"] != "SINK"]  # no sink drawn
    finding = _finding("D3", "D3", '22"', '22"', status="same")
    result = escalate([finding], _ctx(22, 24, reference=reference))
    found = judged(result.changed[0])
    assert proof.proof_group(found) == "not_compared"
    reason = proof.classified(found)
    assert reason.reason == "different_reference"
    assert "the sink's centre line" in str(reason.why) and "a cabinet's side" in str(reason.why)
    assert not proof.code_proved({**found, "status": "different"})
    # Paired with other labels later: the record no longer speaks.
    moved = {
        **found,
        "dims": {**found["dims"], "architect": {"id": "D4", "page": 1, "file": REFERENCE}},
    }
    assert dim_ends.unlike_reason(moved) is None


def test_a_field_cut_overall_equal_to_the_reference_top_stays_as_it_was() -> None:
    finding = _finding("D1", "D1", '46"', '45"', status="same")
    result = escalate([finding], _ctx(23, 23))
    assert all(f["id"] != "F1" or dim_ends.FIELD not in f for f in result.changed)


def test_a_notch_claim_with_its_step_drawn_is_dropped() -> None:
    vendor, _cuts = vendor_plan(23, 23)
    vendor.line((96, 230), (100, 230))  # a 1" step on the top's left end
    vendor.line((96, 230), (96, 300))
    vendor.line((100, 230), (100, 160))
    claim = {
        "id": "F2",
        "page": 1,
        "vs_page": 1,
        "status": "check",
        "source": "architect",
        "item": "Stepped (notched) left end of the top",
        "vendor": '1" step at the back',
        "architect": "straight end",
        "created_by": "whole_look",
        "vendor_box": {"page": 1, "x0": 90, "y0": 200, "x1": 130, "y1": 300, "file": VENDOR},
    }
    result = escalate([claim], _ctx(23, 23, vendor=vendor))
    assert "F2" in result.absorbed and "notch" in result.absorbed["F2"]
    plain = {**claim, "id": "F3", "item": "Countertop edge profile", "vendor": "eased edge"}
    assert "F3" not in escalate([plain], _ctx(23, 23, vendor=vendor)).absorbed


def test_an_l_shaped_top_that_is_the_wall_is_dropped_but_not_a_no_return_claim() -> None:
    reference = Drawing("architect", 3.0)
    reference.dim(0, 30, 300.0, 30)
    right = reference.x(30)
    reference.line((right, 200), (right, 290))
    reference.line((right + 15, 190), (right + 15, 295))
    claim = {
        "id": "F4",
        "page": 1,
        "vs_page": 1,
        "status": "check",
        "source": "architect",
        "item": "top shape / side return",
        "vendor": "plain rectangle top",
        "architect": "L-shaped top with return at right end (plan)",
        "created_by": "helper:1",
        "arch_box": {"page": 1, "x0": 80, "y0": 180, "x1": 260, "y1": 320, "file": REFERENCE},
    }
    result = escalate([claim], _ctx(23, 23, reference=reference))
    assert "F4" in result.absorbed and "wall" in result.absorbed["F4"]
    negated = {**claim, "id": "F5", "architect": "no end apron or return; walls at both ends"}
    assert "F5" not in escalate([negated], _ctx(23, 23, reference=reference)).absorbed


def test_no_pages_reader_leaves_every_item_as_it_was() -> None:
    finding = _finding("D3", "D3", '22"', '20"')
    result = escalate([finding], EscalationContext(main_file=VENDOR))
    assert all(
        dim_ends.FIELD not in f and f.get("created_by") != "sink_centre" for f in result.changed
    )
    assert escalation.undecided({**finding, "created_by": sink_centre.CREATED_BY}) is False


# ---- guards ------------------------------------------------------------------------------------------


def test_a_chain_never_loops_on_a_dimension_shorter_than_a_point() -> None:
    page = dim_ends.Page("x.pdf", 1, ppi=0.9)
    page.spans = {
        "A": dim_ends.Span("A", "h", 100.0, 0.0, 0.5, Fraction(1)),
        "B": dim_ends.Span("B", "h", 100.0, 0.5, 30.0, Fraction(33)),
    }
    assert sink_centre._chain(page, "h", 0.0, 30.0, [100.0]) is None
    assert sink_centre._chain(page, "h", 0.0, 60.0, [100.0]) is None


def _end(*kinds: str) -> dim_ends.End:
    return dim_ends.End(0.0, frozenset(kinds))


def test_both_ways_round_with_different_answers_claims_nothing() -> None:
    span = dim_ends.Span("V", "h", 0, 0, 10, Fraction(20))
    vendor = sink_centre.VendorSink(
        span,
        dim_ends.Span("W", "h", 0, 10, 20, Fraction(26)),
        _end("wall"),
        _end("wall"),
        0,
        0,
        Fraction(1),
    )
    base = sink_centre.Base(
        dim_ends.Span("B", "h", 0, 0, 1, Fraction(20)),
        "SB20",
        dim_ends.Span("T", "h", 0, 0, 1, Fraction(46)),
        _end("wall"),
        _end("wall"),
        Fraction(22),
        (Fraction(12),),
    )
    # Read directly the sink is 2" off; read mirrored it is 4" off: both differ, so it may say so.
    assert sink_centre.compare(vendor, base).outcome == "different"
    centred_one_way = sink_centre.Base(
        base.base, base.tag, base.top, _end("wall"), _end("wall"), Fraction(20), (Fraction(10),)
    )
    # Read directly the sink is on the centre, read mirrored it is 6" off: nothing is claimed.
    assert sink_centre.compare(vendor, centred_one_way).outcome == "unsure"


def test_a_long_size_both_ways_keeps_an_l_shaped_claim() -> None:
    reference = Drawing("architect", 3.0)
    reference.dim(0, 40, 300.0, 40)
    right = reference.x(40)
    reference.line((right, 200), (right, 290))
    reference.line((right + 15, 190), (right + 15, 295))
    reference.labels.append(
        {
            "id": "D9",
            "text": '48"',
            "inches": "48",
            "box": [110, 240, 120, 260],
            "side": "architect",
            "orientation": "v",
            "route": "text",
            "scale_ok": True,
            "dim_line": [[105, 160], [105, 304]],
        }
    )
    claim = {
        "id": "F6",
        "page": 1,
        "vs_page": 1,
        "status": "check",
        "source": "architect",
        "item": "top shape",
        "vendor": "plain rectangle top",
        "architect": "L-shaped top with return at right end",
        "created_by": "helper:1",
        "arch_box": {"page": 1, "x0": 80, "y0": 150, "x1": 260, "y1": 320, "file": REFERENCE},
    }
    assert "F6" not in escalate([claim], _ctx(23, 23, reference=reference)).absorbed


def test_a_difference_that_is_only_the_field_cut_is_never_red() -> None:
    finding = _finding("D1", "D1", '46"', '45"', status="different")
    result = escalate([finding], _ctx(23, 23))
    found = judged(next(f for f in result.changed if f["id"] == "F1"))
    assert proof.proof_group(found) == "not_compared"
    assert "field cut" in str(proof.classified(found).why)


def test_a_failing_ends_step_leaves_the_item_as_it_was(monkeypatch: Any) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("made up")

    monkeypatch.setattr(dim_ends, "alike", boom)
    finding = _finding("D3", "D3", '22"', '20"')
    result = escalate([finding], _ctx(22, 24))
    assert all(f.get("created_by") != sink_centre.CREATED_BY for f in result.changed)
