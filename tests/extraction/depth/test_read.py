"""The depth reader decides each value from two AI judgments and code's (#1265). Fake readers only:
no model, no network. Made-up geometry and values.

The view is a plan, 4 pt to the inch: a wall band along its top, the stone's front edge 96 pt
below the wall (24"), a 340 pt overall above (85"), one sink outline 110 x 50 pt, its width and
front-to-back dimensions, and a 16 pt strip from the hole's front edge to the cabinet face.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from fractions import Fraction
from uuid import UUID

import pytest

from evidence.crop import RenderedPage
from extraction.architect.text import Orientation
from extraction.depth.output import SpanRoleAnswer, ViewKindAnswer
from extraction.depth.prompts import (
    DEPTH_SPAN_PROMPT_ID,
    SPAN_ROLE_PROMPT_ID,
    VIEW_KIND_PROMPT_ID,
)
from extraction.depth.read import (
    CLAUDE_PAIR,
    CT007,
    CT008,
    CT010,
    CT012,
    MEASURED_DEPTH_READER_SETTINGS,
    Asked,
    DepthReading,
    NoAnswer,
    PreparedPage,
    PreparedView,
    ValueReading,
    decide_view_kind,
    depth_reader_enabled,
    label_areas,
    label_boxes,
    read_depth,
)
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
    TextLane,
    cutout_judgment,
    find_spans,
    sink_outlines,
    text_lanes,
)
from extraction.depth.view_kind import (
    CodeViewKind,
    VendorView,
    ViewKind,
    code_view_kind,
    kind_from_heading,
)
from extraction.geometry.dimension_lines import Axis
from extraction.geometry.rows import Box, InkLine
from extraction.ink import InkAt, InkClass
from extraction.slot_reader.seal import ReaderAnswer
from tests.extraction.depth.test_spans import (
    VIEW,
    d,
    horizontal_dimension,
    ink,
    line,
    phrase,
    rectangle_path,
    vertical_dimension,
    wall_hatch,
)
from tests.extraction.test_glyph_bands import FRACTION, GEOMETRY

OPUS, SONNET = CLAUDE_PAIR
SHA = "a" * 64


def plan_lines(*, strip: tuple[float, float] = (170, 186), front_to: float = 400) -> list[InkLine]:
    return (
        wall_hatch(90, 100, 60, 400)
        + [line(60, 100, 400, 100), line(60, 90, 400, 90)]  # the wall's two faces
        + [line(60, 196, front_to, 196)]  # the stone's front edge
        + vertical_dimension(50, 100, 196)  # the depth, 96 pt
        + horizontal_dimension(30, 60, 400)  # the overall, 340 pt
        + horizontal_dimension(250, 150, 260)  # the cut-out's width, 110 pt
        + vertical_dimension(300, 120, 170)  # the cut-out's front to back, 50 pt
        + vertical_dimension(360, *strip)  # the strip in front of the hole
    )


def prepared(
    *,
    heading: str = "PLAN VIEW",
    lines: list[InkLine] | None = None,
    outlines: Sequence[tuple[float, float, float, float]] = ((150, 120, 260, 170),),
    extent: Box = VIEW,
    number: int = 1,
) -> PreparedView:
    page_ink = ink(
        plan_lines() if lines is None else lines,
        tuple(rectangle_path(*box) for box in outlines),
    )
    found = find_spans(page_ink, extent, MEASURED_SPAN_SETTINGS)
    phrases = [phrase("SINK", 205, 145, Orientation.UPRIGHT)]
    view = VendorView(
        number=number,
        heading=heading,
        heading_box=None,
        extent=extent,
        separated=True,
        reason="made up",
        found_by="heading words",
    )
    return PreparedView(
        page_index=0,
        panel=1,
        view=view,
        found=found,
        code_kind=code_view_kind(kind_from_heading(heading), found.bands),
        lanes=text_lanes(found.spans, [], MEASURED_SPAN_SETTINGS, chars=[]),
        sinks=tuple(
            (sink, cutout_judgment(sink, []))
            for sink in sink_outlines(page_ink, extent, phrases, MEASURED_SPAN_SETTINGS)
        ),
        panel_headings=(),
        label_boxes=label_boxes(page_ink, found.spans, MEASURED_DEPTH_READER_SETTINGS),
        label_areas=label_areas(
            page_ink,
            found.spans,
            text_lanes(found.spans, [], MEASURED_SPAN_SETTINGS, chars=[]),
            MEASURED_DEPTH_READER_SETTINGS,
        ),
        page_height=page_ink.height,
    )


def page_of(
    view: PreparedView, *, ink_class: InkClass = InkClass.VENDOR, also: Sequence[PreparedView] = ()
) -> PreparedPage:
    rendered = RenderedPage(
        document_version_id=UUID(int=1),
        page_index=0,
        page_content_hash=SHA,
        rotation=0,
        render_failed=False,
        width_px=600,
        height_px=400,
        dpi=72,
        rgb_bytes=b"\xff" * (600 * 400 * 3),
    )

    def pixels(box: Box) -> tuple[int, int, int, int]:
        return int(box.x0), int(box.top), int(box.x1), int(box.bottom)

    return PreparedPage(
        page_index=0,
        page_sha256=SHA,
        document_version_id="doc",
        rendered=rendered,
        pixels=pixels,
        views=(view, *also),
        ink_at=lambda _box: InkAt(ink_class, ""),
    )


def number_of(view: PreparedView, axis: Axis, start: float, end: float) -> int:
    (found,) = [
        span
        for span in view.found.spans
        if span.axis is axis and (span.start_pt, span.end_pt) == (d(start), d(end))
    ]
    return found.number


def number(text: str, model: str, **flags: object) -> ReaderAnswer:
    shape: dict[str, object] = {
        "text": text,
        "readable": True,
        "no_dimension": False,
        "stacked": False,
        "combined": False,
        "belongs": "yes",
    }
    shape.update(flags)
    return ReaderAnswer(model_id=model, **shape)  # type: ignore[arg-type]


type Reply = ViewKindAnswer | SpanRoleAnswer | ReaderAnswer | NoAnswer


class FakeReaders:
    """Answers each question from tables; records every question asked."""

    def __init__(
        self,
        view: PreparedView,
        *,
        kinds: Mapping[str, str | None] | None = None,
        roles: Mapping[str, Mapping[str, object]] | None = None,
        numbers: Mapping[str, Mapping[int, Reply | str]] | None = None,
    ) -> None:
        self.view = view
        self.kinds = kinds or {OPUS: "plan", SONNET: "plan"}
        depth = number_of(view, Axis.VERTICAL, 100, 196)
        width = next(
            span.number
            for span in view.found.spans
            if span.axis is Axis.HORIZONTAL and span.offset_pt == d(250)
        )
        front_back = number_of(view, Axis.VERTICAL, 120, 170)
        strip = next(
            span.number
            for span in view.found.spans
            if span.axis is Axis.VERTICAL and span.offset_pt == d(360)
        )
        self.spans = {"depth": depth, "width": width, "front_back": front_back, "strip": strip}
        agreed: dict[str, object] = {
            "depth": depth,
            "cutout_width": width,
            "cutout_depth": front_back,
            "front_offset": strip,
            "front_offset_start": "cabinet_face",
            "hole_outline": "A",
        }
        self.roles = roles or {OPUS: agreed, SONNET: agreed}
        overall = number_of(view, Axis.HORIZONTAL, 60, 400)
        plain: dict[int, Reply | str] = {
            overall: '85"',
            depth: '24"',
            width: '27 1/2"',
            front_back: '12 1/2"',
            strip: '4"',
        }
        self.numbers = numbers or {OPUS: plain, SONNET: plain}
        self.asked: list[Asked] = []

    def __call__(self, questions: Sequence[Asked]) -> dict[tuple[str, str], Reply]:
        self.asked.extend(questions)
        out: dict[tuple[str, str], Reply] = {}
        for asked in questions:
            model = asked.model_id
            prompt = asked.question.prompt_id
            if prompt == VIEW_KIND_PROMPT_ID:
                kind = self.kinds.get(model, "missing")
                out[(asked.key, model)] = (
                    NoAnswer("abstained")
                    if kind == "missing"
                    else ViewKindAnswer(model, kind, "made up")  # type: ignore[arg-type]
                )
            elif prompt == SPAN_ROLE_PROMPT_ID:
                picks = self.roles.get(model)
                out[(asked.key, model)] = (
                    NoAnswer("abstained")
                    if picks is None
                    else SpanRoleAnswer(model_id=model, why="made up", **picks)  # type: ignore[arg-type]
                )
            else:
                assert prompt == DEPTH_SPAN_PROMPT_ID
                span = int(asked.key.rsplit(":n", 1)[1])
                said = self.numbers.get(model, {}).get(span, NoAnswer("abstained"))
                out[(asked.key, model)] = number(said, model) if isinstance(said, str) else said
        return out

    def prompts(self) -> list[str]:
        return [asked.question.prompt_id for asked in self.asked]


def read(view: PreparedView, readers: FakeReaders, **page: object) -> DepthReading:
    return read_depth(
        [page_of(view, **page)], ask=readers, fraction_bar=GEOMETRY  # type: ignore[arg-type]
    )


def values(reading: DepthReading) -> dict[str, ValueReading]:
    (view,) = reading.views
    return {value.field: value for value in view.values}


# --- the switch -----------------------------------------------------------------------------


def test_the_switch_is_off_by_default_and_refuses_an_unknown_word() -> None:
    assert depth_reader_enabled({}) is False
    assert depth_reader_enabled({"GV_DEPTH_READER": ""}) is False
    assert depth_reader_enabled({"GV_DEPTH_READER": "false"}) is False
    assert depth_reader_enabled({"GV_DEPTH_READER": "true"}) is True
    with pytest.raises(ValueError):
        depth_reader_enabled({"GV_DEPTH_READER": "maybe"})


# --- every value usable when both judgments agree ---------------------------------------------


def test_both_judgments_agreeing_gives_each_value_in_exact_inches() -> None:
    view = prepared()
    reading = read(view, FakeReaders(view))
    (only,) = reading.views
    assert only.kind is ViewKind.PLAN
    found = values(reading)
    assert {name: value.usable for name, value in found.items()} == dict.fromkeys(
        (CT010, CT012, CT008, CT007), True
    ), {name: value.reason for name, value in found.items()}
    assert found[CT010].inches == 24
    assert found[CT012].inches == Fraction(55, 2)
    assert found[CT008].inches == Fraction(25, 2)
    assert found[CT007].inches == 4 and found[CT007].start_edge == "cabinet_face"
    assert found[CT012].outline == "A"
    assert all(value.checks for value in found.values())
    # One view: two kind questions, two role questions, five numbers (four values + the overall).
    assert reading.questions_asked == 2 + 2 + 2 * 5


# --- fail-first: every doubt holds --------------------------------------------------------


def test_readers_disagreeing_on_the_view_kind_means_nothing_is_read() -> None:
    view = prepared()
    readers = FakeReaders(view, kinds={OPUS: "plan", SONNET: "front"})
    reading = read(view, readers)
    (only,) = reading.views
    assert only.kind is None and only.values == ()
    assert "disagree" in only.kind_reason
    assert readers.prompts() == [VIEW_KIND_PROMPT_ID] * 2  # nothing more was asked


def test_a_wrong_heading_holds_even_when_both_readers_agree() -> None:
    # Code reads "FRONT VIEW" (and sees a wall band, so it cannot say); both readers see a plan.
    view = prepared(heading="FRONT VIEW")
    reading = read(view, FakeReaders(view))
    (only,) = reading.views
    assert only.kind is None and only.values == ()


def test_decide_view_kind_needs_both_readers_and_code() -> None:
    code = CodeViewKind(ViewKind.PLAN, kind_from_heading("PLAN"), True, "made up")
    plan = ViewKindAnswer(OPUS, "plan", "")
    assert decide_view_kind(code, [plan, ViewKindAnswer(SONNET, "plan", "")])[0] is ViewKind.PLAN
    assert decide_view_kind(code, [plan, None])[0] is None
    assert decide_view_kind(code, [plan, ViewKindAnswer(SONNET, None, "")])[0] is None
    unknown = CodeViewKind(None, kind_from_heading(None), False, "no heading")
    assert decide_view_kind(unknown, [plan, ViewKindAnswer(SONNET, "plan", "")])[0] is None


def test_a_strip_the_readers_say_starts_at_the_stone_edge_is_never_ct007() -> None:
    view = prepared()
    readers = FakeReaders(view)
    stone = {**readers.roles[OPUS], "front_offset_start": "stone_front_edge"}
    readers.roles = {OPUS: stone, SONNET: stone}
    found = values(read(view, readers))
    assert not found[CT007].usable and found[CT007].inches is None
    assert "stone's front edge" in found[CT007].reason
    assert all(says.says == "held" for says in found[CT007].readers)
    # No number was paid for a strip that can never be the front offset.
    assert f":n{readers.spans['strip']}" not in " ".join(a.key for a in readers.asked)


def test_a_strip_code_sees_reaching_the_stone_edge_holds_even_when_readers_say_cabinet() -> None:
    # The strip's dimension runs on to the stone's front edge.
    view = prepared(lines=plan_lines(strip=(170, 196), front_to=340))
    readers = FakeReaders(view)
    found = values(read(view, readers))
    assert found[CT007].usable is False
    assert "code holds it" in found[CT007].reason
    assert "stone's edge" in found[CT007].reason


def test_two_outlines_and_no_word_hold_the_cut_out() -> None:
    view = prepared(outlines=((150, 120, 260, 170), (156, 126, 254, 164)))
    found = values(read(view, FakeReaders(view)))
    for field in (CT012, CT008, CT007):
        assert not found[field].usable, field
    assert "one click" in found[CT012].reason
    assert found[CT010].usable  # the depth is not the sink's


def test_millimetres_only_hold() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    for model in CLAUDE_PAIR:
        readers.numbers = {model: {**readers.numbers[model], depth: "610"} for model in CLAUDE_PAIR}
    found = values(read(view, readers))
    assert not found[CT010].usable and found[CT010].inches is None
    assert "millimetres" in found[CT010].reason


def test_bracketed_inches_count_and_millimetres_only_corroborate() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    readers.numbers = {
        model: {**readers.numbers[model], depth: "610 [24]"} for model in CLAUDE_PAIR
    }
    assert values(read(view, readers))[CT010].inches == 24
    readers.numbers = {
        model: {**readers.numbers[model], depth: "700 [24]"} for model in CLAUDE_PAIR
    }
    assert not values(read(view, readers))[CT010].usable


def test_a_stacked_fraction_holds() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    stacked = number('24"', OPUS, stacked=True)
    readers.numbers = {
        OPUS: {**readers.numbers[OPUS], depth: stacked},
        SONNET: readers.numbers[SONNET],
    }
    found = values(read(view, readers))
    assert not found[CT010].usable
    assert "stacked" in found[CT010].reason


def test_one_reader_missing_gives_no_value() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    readers.numbers = {
        OPUS: readers.numbers[OPUS],
        SONNET: {**readers.numbers[SONNET], depth: NoAnswer("abstained")},
    }
    found = values(read(view, readers))
    assert not found[CT010].usable and found[CT010].inches is None
    assert [says.says for says in found[CT010].readers] == ["value", "invalid"]


def test_one_reader_missing_from_the_roles_gives_no_value() -> None:
    view = prepared()
    readers = FakeReaders(view)
    readers.roles = {OPUS: readers.roles[OPUS]}
    found = values(read(view, readers))
    assert not any(value.usable for value in found.values())


def test_code_disallowing_holds_even_when_both_readers_agree() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    # Both read 34" for a dimension drawn 24" long at the view's scale.
    readers.numbers = {model: {**readers.numbers[model], depth: '34"'} for model in CLAUDE_PAIR}
    found = values(read(view, readers))
    assert not found[CT010].usable
    assert "code holds it" in found[CT010].reason


def test_code_disallowing_the_dimension_as_the_depth_holds() -> None:
    view = prepared()
    readers = FakeReaders(view)
    # Both readers call the cut-out's front-to-back dimension the stone's depth.
    wrong = {
        **readers.roles[OPUS],
        "depth": readers.spans["front_back"],
        "cutout_depth": "none",
    }
    readers.roles = {OPUS: wrong, SONNET: wrong}
    found = values(read(view, readers))
    assert not found[CT010].usable
    assert "wall band" in found[CT010].reason


def test_readers_picking_different_dimensions_hold_and_ask_no_number() -> None:
    view = prepared()
    readers = FakeReaders(view)
    other = {**readers.roles[OPUS], "depth": "unsure"}
    readers.roles = {OPUS: readers.roles[OPUS], SONNET: other}
    found = values(read(view, readers))
    assert not found[CT010].usable
    assert [says.says for says in found[CT010].readers] == ["held", "unsure"]


def test_the_reviewers_ink_on_the_close_up_holds_the_number() -> None:
    view = prepared()
    found = values(read(view, FakeReaders(view), ink_class=InkClass.GV))
    assert not any(value.usable for value in found.values())


def test_only_the_claude_pair_reads() -> None:
    view = prepared()
    with pytest.raises(ValueError):
        read_depth(
            [page_of(view)],
            ask=FakeReaders(view),
            readers=("a.one", "b.two"),
            fraction_bar=GEOMETRY,
        )


def test_settings_fit_claude_pictures() -> None:
    # A whole-view picture at most 1800 px a side is 65 x 65 tiles: under the 4784 limit.
    side = -(-MEASURED_DEPTH_READER_SETTINGS.view_max_side // 28)
    assert side * side <= 4784


# --- review #1274 ---------------------------------------------------------------------------


def with_depth(readers: FakeReaders, said: Reply | str) -> FakeReaders:
    depth = readers.spans["depth"]
    readers.numbers = {model: {**readers.numbers[model], depth: said} for model in CLAUDE_PAIR}
    return readers


@pytest.mark.parametrize(
    "read_as", ['22"', '23 3/4"', '23 7/8"', '24 1/8"', '24 1/4"', '24 3/4"', '25 1/2"']
)
def test_a_reading_a_quarter_inch_off_the_drawn_length_holds(read_as: str) -> None:
    # The depth is drawn exactly 24" long: within 1/10 these all agreed. Drawn lengths measure to
    # about a fiftieth, so an eighth off (24 1/8", 23 7/8") is another value, and holds.
    view = prepared()
    found = values(read(view, with_depth(FakeReaders(view), read_as)))[CT010]
    assert not found.usable and "outside" in found.reason


def test_a_value_confirmed_only_by_drawn_length_says_so() -> None:
    view = prepared()
    found = values(read(view, with_depth(FakeReaders(view), '24 1/16"')))[CT010]
    assert found.usable
    assert found.confirmed_by == "drawn-length-only"
    assert found.scale_source == "readers"
    assert any("weaker" in check for check in found.checks)


def fraction_strokes(x: float, y: float) -> list[InkLine]:
    """A stacked fraction as the plotter draws it (`test_glyph_bands.FRACTION`), at (x, y)."""
    return [
        line(x + float(x0), y - float(y0), x + float(x1), y - float(y1))
        for x0, y0, x1, y1 in FRACTION
    ]


@pytest.mark.parametrize("read_as", ['24"', '24 1/8"'])
def test_a_stacked_fraction_the_bar_detector_finds_holds(read_as: str) -> None:
    # The depth's own label, drawn on its own side (left of the vertical line), is stacked.
    view = prepared(lines=plan_lines() + fraction_strokes(38, 150))
    found = values(read(view, with_depth(FakeReaders(view), read_as)))[CT010]
    assert not found.usable and found.inches is None
    assert "stacked" in found.reason


def test_code_naming_another_kind_than_both_readers_holds_the_view() -> None:
    # A plan headed "FRONT VIEW" with no wall band drawn: code says front view, both readers plan.
    lines = [stroke for stroke in plan_lines() if stroke not in wall_hatch(90, 100, 60, 400)]
    view = prepared(heading="FRONT VIEW", lines=lines)
    assert view.code_kind.kind is ViewKind.ELEVATION
    (only,) = read(view, FakeReaders(view)).views
    assert only.kind is None and only.values == ()
    assert "label says front view" in only.kind_reason
    code = CodeViewKind(ViewKind.ELEVATION, kind_from_heading("FRONT VIEW"), False, "made up")
    both = [ViewKindAnswer(OPUS, "plan", ""), ViewKindAnswer(SONNET, "plan", "")]
    assert decide_view_kind(code, both)[0] is None


def test_no_code_check_confirming_the_number_holds() -> None:
    # The overall's number is not read, so there is no scale and no printed text: no check.
    view = prepared()
    readers = FakeReaders(view)
    overall = number_of(view, Axis.HORIZONTAL, 60, 400)
    readers.numbers = {
        model: {**readers.numbers[model], overall: NoAnswer("abstained")} for model in CLAUDE_PAIR
    }
    found = values(read(view, readers))[CT010]
    assert not found.usable and "no code check could confirm" in found.reason


def test_vector_text_disagreeing_with_both_readers_holds() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    lanes = dict(view.lanes)
    lanes[depth] = TextLane(depth, ('25"',), Fraction(25), "reads exactly")
    found = values(read(replace(view, lanes=lanes), readers))[CT010]
    assert not found.usable and "printed text at this dimension says" in found.reason


def test_vector_text_agreeing_is_the_strongest_confirmation() -> None:
    view = prepared()
    readers = FakeReaders(view)
    depth = readers.spans["depth"]
    lanes = dict(view.lanes)
    lanes[depth] = TextLane(depth, ('24"',), Fraction(24), "reads exactly")
    found = values(read(replace(view, lanes=lanes), readers))[CT010]
    assert found.usable and found.confirmed_by == "vector-text"


SECTION_EXTENT = Box(d(445), d(305), d(600), d(400))


def section(
    *, extent: Box = VIEW, y: float = 320, left: float = 460, right: float = 560
) -> PreparedView:
    return prepared(
        heading="SECTION A",
        lines=horizontal_dimension(y, left, right),
        outlines=(),
        extent=extent,
        number=2 if extent != VIEW else 1,
    )


class SectionReaders:
    """Both readers: a section, its one dimension is the depth, read as `read_as`."""

    def __init__(self, view: PreparedView, read_as: str) -> None:
        (self.span,) = [span.number for span in view.found.spans]
        self.read_as = read_as
        self.key = view.key

    def owns(self, asked: Asked) -> bool:
        return asked.key.startswith(self.key + ":")

    def __call__(self, questions: Sequence[Asked]) -> dict[tuple[str, str], Reply]:
        out: dict[tuple[str, str], Reply] = {}
        for asked in questions:
            prompt, model = asked.question.prompt_id, asked.model_id
            if prompt == VIEW_KIND_PROMPT_ID:
                out[(asked.key, model)] = ViewKindAnswer(model, "section_or_side", "")
            elif prompt == SPAN_ROLE_PROMPT_ID:
                out[(asked.key, model)] = SpanRoleAnswer(
                    model_id=model,
                    depth=self.span,
                    cutout_width="none",
                    cutout_depth="none",
                    front_offset="none",
                    front_offset_start="none",
                    hole_outline="none",
                    why="",
                )
            else:
                out[(asked.key, model)] = number(self.read_as, model)
        return out


def test_a_scale_taken_from_the_very_dimension_checked_confirms_nothing() -> None:
    # A section whose only horizontal dimension is the depth: its own reading would set the scale.
    view = section()
    assert view.code_kind.kind is ViewKind.SECTION
    reading = read_depth([page_of(view)], ask=SectionReaders(view, '25"'), fraction_bar=GEOMETRY)
    found = values(reading)[CT010]
    assert not found.usable
    assert "comes from this very dimension" in found.reason


def test_a_strip_that_does_not_end_on_the_hole_holds() -> None:
    view = prepared(lines=plan_lines(strip=(174, 186)))
    readers = FakeReaders(view)
    strip = readers.spans["strip"]
    readers.numbers = {model: {**readers.numbers[model], strip: '3"'} for model in CLAUDE_PAIR}
    found = values(read(view, readers))[CT007]
    assert not found.usable and "does not end on the sink hole's front edge" in found.reason


def test_a_cut_out_dimension_off_the_outline_edges_holds() -> None:
    lines = [
        stroke for stroke in plan_lines() if stroke not in horizontal_dimension(250, 150, 260)
    ] + horizontal_dimension(250, 150, 250)
    view = prepared(lines=lines)
    readers = FakeReaders(view)
    width = number_of(view, Axis.HORIZONTAL, 150, 250)
    readers.numbers = {model: {**readers.numbers[model], width: '25"'} for model in CLAUDE_PAIR}
    found = values(read(view, readers))[CT012]
    assert not found.usable and "do not lie on outline A" in found.reason


def _two_views(section_lane: Fraction) -> tuple[ValueReading, ValueReading]:
    plan_extent = Box(d(0), d(0), d(440), d(300))
    plan = prepared(extent=plan_extent)
    side = section(extent=SECTION_EXTENT)
    (span,) = side.found.spans
    lanes = {span.number: TextLane(span.number, (f'{section_lane}"',), section_lane, "reads")}
    side = replace(side, lanes=lanes)
    plan_readers, side_readers = FakeReaders(plan), SectionReaders(side, '25"')

    def ask(questions: Sequence[Asked]) -> dict[tuple[str, str], Reply]:
        return {
            **side_readers([q for q in questions if side_readers.owns(q)]),
            **plan_readers([q for q in questions if not side_readers.owns(q)]),
        }

    reading = read_depth([page_of(plan, also=(side,))], ask=ask, fraction_bar=GEOMETRY)
    first, second = reading.views
    return values_of(first), values_of(second)


def values_of(view: object) -> ValueReading:
    return next(value for value in view.values if value.field == CT010)  # type: ignore[attr-defined]


def test_two_views_whose_depths_disagree_hold_on_both() -> None:
    plan, side = _two_views(Fraction(25))
    assert plan.agreed_inches == 24 and side.agreed_inches == 25
    assert not plan.usable and not side.usable
    assert "disagree" in plan.reason and "disagree" in side.reason


def test_a_code_held_reading_that_disagrees_still_holds_the_other_view() -> None:
    # M2: the section's agreed 25" is held by code (its printed text says 26"), yet it still
    # disagrees with the plan's 24", so the plan's depth holds too.
    plan, side = _two_views(Fraction(26))
    assert side.agreed_inches == 25 and not side.usable
    assert not plan.usable and "disagree" in plan.reason


def test_a_zero_reading_holds_and_never_raises() -> None:
    view = prepared()
    found = values(read(view, with_depth(FakeReaders(view), '0"')))[CT010]
    assert not found.usable and "zero" in found.reason
    readers = FakeReaders(view)
    overall = number_of(view, Axis.HORIZONTAL, 60, 400)
    readers.numbers = {model: {**readers.numbers[model], overall: '0"'} for model in CLAUDE_PAIR}
    assert not values(read(view, readers))[CT010].usable


def test_a_view_not_cut_apart_from_its_neighbours_is_never_read() -> None:
    view = prepared()
    unseparated = replace(view, view=replace(view.view, cut_apart=False, reason="made up"))
    from extraction.depth.read import prepare_view

    again = prepare_view(
        ink=ink(plan_lines(), (rectangle_path(150, 120, 260, 170),)),
        chars=[],
        panel_phrase_chars=[],
        view=unseparated.view,
        page_index=0,
        panel=1,
        panel_headings=[],
        settings=MEASURED_DEPTH_READER_SETTINGS,
    )
    assert again.code_kind.kind is None and "not cut apart" in again.code_kind.reason
    (only,) = read(again, FakeReaders(view)).views
    assert only.kind is None and only.values == ()


def _projection_pair() -> tuple[PreparedView, PreparedView]:
    """A plan headed plan with its wall not drawn, and a front view below drawing the same 340 pt
    overall at the same x."""
    plan_extent = Box(d(0), d(0), d(440), d(300))
    lines = [stroke for stroke in plan_lines() if stroke not in wall_hatch(90, 100, 60, 400)]
    plan = prepared(lines=lines, extent=plan_extent)
    front = prepared(
        heading="FRONT VIEW",
        lines=horizontal_dimension(330, 60, 400),
        outlines=(),
        extent=Box(d(0), d(305), d(440), d(400)),
        number=2,
    )
    return plan, front


def test_a_projected_plan_is_reported_but_nothing_on_it_is_read() -> None:
    from extraction.depth.read import with_projected_plans

    plan, front = _projection_pair()
    assert plan.code_kind.kind is None
    assert front.code_kind.kind is ViewKind.ELEVATION
    judged, _front = with_projected_plans([plan, front], MEASURED_DEPTH_READER_SETTINGS)
    assert judged.code_kind.kind is ViewKind.PLAN and judged.code_kind.by_projection
    readers = FakeReaders(plan)
    reading = read_depth(
        [page_of(judged)], ask=readers, fraction_bar=GEOMETRY  # type: ignore[arg-type]
    )
    (only,) = reading.views
    assert only.kind is ViewKind.PLAN and only.values == ()
    assert "only by its heading and a projection" in only.values_reason
    assert readers.prompts() == [VIEW_KIND_PROMPT_ID] * 2  # no role or number paid for
    # Without the front view, the heading alone never makes it a plan.
    (alone,) = with_projected_plans([plan], MEASURED_DEPTH_READER_SETTINGS)
    assert alone.code_kind.kind is None


def test_swapped_headings_without_a_band_read_nothing() -> None:
    from extraction.depth.read import with_projected_plans

    # The real front view is headed "PLAN VIEW"; the real plan (no wall band) "FRONT VIEW".
    real_front = prepared(
        heading="PLAN VIEW",
        lines=horizontal_dimension(330, 60, 400),
        outlines=(),
        extent=Box(d(0), d(305), d(440), d(400)),
        number=2,
    )
    lines = [stroke for stroke in plan_lines() if stroke not in wall_hatch(90, 100, 60, 400)]
    real_plan = prepared(heading="FRONT VIEW", lines=lines, extent=Box(d(0), d(0), d(440), d(300)))
    plan_view, front_view = with_projected_plans(
        [real_plan, real_front], MEASURED_DEPTH_READER_SETTINGS
    )
    # Code's projection cue follows the headings here (it cannot tell), so it is never enough to
    # read anything: even if both readers agreed with it, nothing is read.
    assert front_view.code_kind.kind is ViewKind.PLAN and front_view.code_kind.by_projection
    readers_any = lambda questions: {
        (q.key, q.model_id): ViewKindAnswer(q.model_id, "plan", "") for q in questions
    }
    reading = read_depth(
        [page_of(front_view, also=(plan_view,))], ask=readers_any, fraction_bar=GEOMETRY
    )
    assert all(view.values == () for view in reading.views)


def test_projection_needs_a_front_view_cut_apart_on_the_same_panel() -> None:
    from extraction.depth.read import with_projected_plans

    plan, front = _projection_pair()
    S = MEASURED_DEPTH_READER_SETTINGS
    not_apart = replace(front, view=replace(front.view, cut_apart=False))
    assert with_projected_plans([plan, not_apart], S)[0].code_kind.kind is None
    other_panel = replace(front, panel=2)
    assert with_projected_plans([plan, other_panel], S)[0].code_kind.kind is None
    section_like = replace(front, code_kind=replace(front.code_kind, kind=ViewKind.SECTION))
    assert with_projected_plans([plan, section_like], S)[0].code_kind.kind is None
    # A plan-headed view that is itself not cut apart is never made a plan by projection.
    swallowing = replace(plan, view=replace(plan.view, cut_apart=False))
    assert with_projected_plans([swallowing, front], S)[0].code_kind.kind is None


def test_the_number_close_up_hides_every_heading_on_the_panel() -> None:
    view = replace(prepared(), panel_headings=(Box(d(30), d(140), d(45), d(150)),))
    page = page_of(view)
    rgb = bytearray(page.rendered.rgb_bytes)
    for y in range(140, 150):
        for x in range(30, 45):
            rgb[(y * 600 + x) * 3 : (y * 600 + x) * 3 + 3] = b"\x00\x00\x00"
    page = replace(page, rendered=replace(page.rendered, rgb_bytes=bytes(rgb)))
    readers = FakeReaders(view)
    read_depth([page], ask=readers, fraction_bar=GEOMETRY)  # type: ignore[arg-type]
    depth = readers.spans["depth"]
    (asked,) = [a for a in readers.asked if a.key.endswith(f":n{depth}") and a.model_id == OPUS]
    from evidence.crop import decode_rgb_png

    width, _height, pixels = decode_rgb_png(asked.question.pictures[1])
    # The close-up starts 24 pt left of the line (x 26) and 12 pt above the span (y 88).
    x, y = 37 - 26, 145 - 88
    assert pixels[(y * width + x) * 3 : (y * width + x) * 3 + 3] == b"\xff\xff\xff"


def test_no_question_is_paid_for_a_view_not_cut_apart() -> None:
    view = prepared()
    held = replace(view, view=replace(view.view, cut_apart=False, reason="made up"))
    readers = FakeReaders(view)
    (only,) = read(held, readers).views
    assert readers.asked == []
    assert only.kind is None and [says.says for says in only.kinds] == ["held", "held"]


def test_a_neighbours_stacked_label_across_the_line_does_not_hold_the_depth() -> None:
    # The depth's own label (a plain mark left of the line) and a neighbour's stacked fraction
    # just right of it: the detector looks only at the label's own side.
    own_label = [line(42, 140, 46, 156)]
    view = prepared(lines=plan_lines() + own_label + fraction_strokes(53, 150))
    found = values(read(view, with_depth(FakeReaders(view), '24"')))[CT010]
    assert found.usable, found.reason
    # With nothing drawn on its own side, both sides are looked at, and the stacked label holds.
    lonely = prepared(lines=plan_lines() + fraction_strokes(53, 150))
    assert not values(read(lonely, with_depth(FakeReaders(lonely), '24"')))[CT010].usable


def test_two_stones_in_one_plan_hold_every_depth_first() -> None:
    from extraction.depth.read import two_stones

    # A second depth from the same wall to a second front edge, drawn deeper (two stones).
    second = (
        [line(400, 100, 560, 100), line(400, 90, 560, 90)]
        + wall_hatch(90, 100, 400, 560)
        + [line(460, 212, 560, 212)]
        + vertical_dimension(440, 100, 212)
    )
    view = prepared(lines=plan_lines() + second)
    assert two_stones(view, MEASURED_SPAN_SETTINGS) is not None
    found = values(read(view, FakeReaders(view)))
    assert not found[CT010].usable and "different lengths from the wall" in found[CT010].reason
    assert not found[CT007].usable  # the strip needs the stone's front edge: not known
    assert two_stones(prepared(), MEASURED_SPAN_SETTINGS) is None


def test_reviewers_ink_away_from_the_label_does_not_hold_it() -> None:
    view = prepared()
    depth_area = view.label_areas[number_of(view, Axis.VERTICAL, 100, 196)]
    page = page_of(view)

    def ink_at(box: tuple[int, int, int, int]) -> InkAt:
        # GV's ink only far below the depth's label (still inside its close-up).
        return InkAt(InkClass.GV if box[3] > 200 else InkClass.VENDOR, "")

    assert int(depth_area.bottom) <= 200
    found = values(
        read_depth([replace(page, ink_at=ink_at)], ask=FakeReaders(view), fraction_bar=GEOMETRY)  # type: ignore[arg-type]
    )
    assert found[CT010].usable, found[CT010].reason


def test_a_depth_drawn_as_a_chains_overall_asks_its_links_and_the_sum_confirms_it() -> None:
    # A section: the depth runs left to right (96 pt) with its chain 40 + 56 pt below it. The
    # depth is the view's only overall, so drawn length cannot check it: the chain's exact sum can.
    lines = (
        horizontal_dimension(320, 100, 196)
        + horizontal_dimension(350, 100, 140)
        + horizontal_dimension(350, 140, 196)
    )
    view = prepared(heading="SECTION A", lines=lines, outlines=())
    depth = number_of(view, Axis.HORIZONTAL, 100, 196)
    links = [number_of(view, Axis.HORIZONTAL, 100, 140), number_of(view, Axis.HORIZONTAL, 140, 196)]
    assert any(c.overall.number == depth for c in view.found.closures)

    def ask(read_as: dict[int, str]) -> tuple[DepthReading, list[Asked]]:
        asked: list[Asked] = []

        def answer(questions: Sequence[Asked]) -> dict[tuple[str, str], Reply]:
            asked.extend(questions)
            out: dict[tuple[str, str], Reply] = {}
            for q in questions:
                if q.question.prompt_id == VIEW_KIND_PROMPT_ID:
                    out[(q.key, q.model_id)] = ViewKindAnswer(q.model_id, "section_or_side", "")
                elif q.question.prompt_id == SPAN_ROLE_PROMPT_ID:
                    out[(q.key, q.model_id)] = SpanRoleAnswer(
                        model_id=q.model_id,
                        depth=depth,
                        cutout_width="none",
                        cutout_depth="none",
                        front_offset="none",
                        front_offset_start="none",
                        hole_outline="none",
                        why="",
                    )
                else:
                    span = int(q.key.rsplit(":n", 1)[1])
                    out[(q.key, q.model_id)] = number(read_as[span], q.model_id)
            return out

        reading = read_depth([page_of(view)], ask=answer, fraction_bar=GEOMETRY)
        return reading, asked

    reading, asked = ask({depth: '24"', links[0]: '10"', links[1]: '14"'})
    found = values(reading)[CT010]
    numbered = {q.key.rsplit(":n", 1)[1] for q in asked if ":n" in q.key}
    assert {str(n) for n in links} <= numbered
    assert found.usable and found.confirmed_by == "chain", found.reason
    reading, _asked = ask({depth: '24"', links[0]: '10"', links[1]: '13"'})
    assert not values(reading)[CT010].usable


def test_a_short_run_from_the_wall_beside_the_depth_holds_too() -> None:
    from extraction.depth.read import two_stones

    # Fail closed: a 30 pt run from the wall (a cubby, a part) beside the 96 pt depth holds it.
    part = [line(420, 130, 560, 130)] + vertical_dimension(440, 100, 130)
    lines = [s for s in plan_lines() if s not in wall_hatch(90, 100, 60, 400)]
    lines += wall_hatch(90, 100, 60, 560) + [line(400, 100, 560, 100), line(400, 90, 560, 90)]
    view = prepared(lines=lines + part)
    assert two_stones(view, MEASURED_SPAN_SETTINGS) is not None
    assert not values(read(view, FakeReaders(view)))[CT010].usable


def test_two_stones_dimensioned_as_one_chain_never_confirm_a_depth() -> None:
    # Stone A 96 pt deep beside stone B 112 pt deep, dimensioned as a chain 96 + 16 = 112 from the
    # wall: the links run across two stones; the depth must not be confirmed by their sum.
    chain = vertical_dimension(20, 100, 196) + vertical_dimension(20, 196, 212)
    overall = vertical_dimension(35, 100, 212)
    base = [s for s in plan_lines() if s not in vertical_dimension(50, 100, 196)]
    view = prepared(lines=base + chain + overall + [line(93, 212, 400, 212)])  # stone B's front
    readers = FakeReaders(view)
    deepest = next(
        s.number
        for s in view.found.spans
        if s.axis is Axis.VERTICAL and (s.start_pt, s.end_pt) == (d(100), d(212))
    )
    roles = {**readers.roles[OPUS], "depth": deepest}
    readers.roles = {OPUS: roles, SONNET: roles}
    links = [
        next(
            s.number
            for s in view.found.spans
            if s.axis is Axis.VERTICAL and (s.start_pt, s.end_pt) == (d(196), d(212))
        )
    ]
    for model in CLAUDE_PAIR:
        readers.numbers[model] = {**readers.numbers[model], deepest: '28"', links[0]: '4"'}
    found = values(read(view, readers))[CT010]
    assert not found.usable and "different lengths from the wall" in found.reason
    # Nothing there can be confirmed, so the chain's links are not paid for.
    assert not any(a.key.endswith(f":n{links[0]}") for a in readers.asked)
