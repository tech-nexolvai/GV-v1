"""The depth reader decides each value from two AI judgments and code's (#1265). Fake readers only:
no model, no network. Made-up geometry and values.

The view is a plan, 4 pt to the inch: a wall band along its top, the stone's front edge 96 pt
below the wall (24"), a 340 pt overall above (85"), one sink outline 110 x 50 pt, its width and
front-to-back dimensions, and a 16 pt strip from the hole's front edge to the cabinet face.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
    read_depth,
)
from extraction.depth.spans import (
    MEASURED_SPAN_SETTINGS,
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
) -> PreparedView:
    page_ink = ink(
        plan_lines() if lines is None else lines,
        tuple(rectangle_path(*box) for box in outlines),
    )
    found = find_spans(page_ink, VIEW, MEASURED_SPAN_SETTINGS)
    phrases = [phrase("SINK", 205, 145, Orientation.UPRIGHT)]
    view = VendorView(
        number=1,
        heading=heading,
        heading_box=None,
        extent=VIEW,
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
            for sink in sink_outlines(page_ink, VIEW, phrases, MEASURED_SPAN_SETTINGS)
        ),
        panel_headings=(),
    )


def page_of(view: PreparedView, *, ink_class: InkClass = InkClass.VENDOR) -> PreparedPage:
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
        views=(view,),
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
        width = number_of(view, Axis.HORIZONTAL, 150, 260)
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
    return read_depth([page_of(view, **page)], ask=readers)  # type: ignore[arg-type]


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
        read_depth([page_of(view)], ask=FakeReaders(view), readers=("a.one", "b.two"))


def test_settings_fit_claude_pictures() -> None:
    # A whole-view picture at most 1800 px a side is 65 x 65 tiles: under the 4784 limit.
    side = -(-MEASURED_DEPTH_READER_SETTINGS.view_max_side // 28)
    assert side * side <= 4784
