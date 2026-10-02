"""Reading a label from its character shapes (#756 phase C).

Verification for: `extraction/glyph_reader.py`.

**How the fixtures stand in for a person's labels.** The templates are built the way phase B builds
them: labels are drawn, every character is described in its label by `described` — the same sizing
the inventory uses — and given the character it is. The labels read afterwards are *different*
labels made of those characters, so a pass means the reader composed something it was never shown.

The ones that matter most are the refusals: a stacked `3/4"` is never `3 3/4"` (#726), a turned `6`
is never read as a `9`, and a label with one unknown character, one piece of line-work, or a layout
that is not a dimension's abstains whole rather than returning part of itself.

Every fixture is authored geometry in a made-up font. No client drawing is read here.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from extraction.annotations import PathSegment, SegmentKind, VectorPath
from extraction.glyph_reader import (
    GlyphAbstention,
    GlyphReading,
    ReaderSettings,
    TemplateSet,
    described,
    gather_label,
    read_label,
)
from extraction.glyph_shapes import GlyphShape, ShapeSettings

SHAPE = ShapeSettings(size_px=24, bezier_steps=8, stroke_px=1, dilate_px=1)
SETTINGS = ReaderSettings(
    shape=SHAPE,
    maximum_distance=Decimal("0.5"),
    # The made-up font's `5` and `6` differ by one stroke, 0.16 px apart; a real set's margin is
    # measured in phase D. These tests are about composing and refusing, not about calibration.
    minimum_margin=Decimal("0.1"),
    maximum_size_ratio=Decimal("1.3"),
    label_gap_pt=Decimal(4),
    maximum_label_pt=Decimal(80),
    glyph_gap_pt=Decimal(4),
)

Points = list[tuple[float, float]]

#: A made-up font, ten units tall and six wide. Each character is a list of sub-paths.
FONT: dict[str, list[Points]] = {
    "0": [[(0, 0), (6, 0), (6, 10), (0, 10), (0, 0)]],
    "1": [[(3, 0), (3, 10)]],
    "2": [[(0, 10), (6, 10), (6, 5), (0, 0), (6, 0)]],
    "3": [[(0, 10), (6, 10), (6, 0), (0, 0)], [(2, 5), (6, 5)]],
    "4": [[(5, 0), (5, 10), (0, 3), (6, 3)]],
    "5": [[(6, 10), (0, 10), (0, 5), (6, 5), (6, 0), (0, 0)]],
    "6": [[(6, 10), (0, 10), (0, 0), (6, 0), (6, 5), (0, 5)]],
    "7": [[(0, 10), (6, 10), (2, 0)]],
    "8": [[(0, 0), (6, 0), (6, 10), (0, 10), (0, 0)], [(0, 5), (6, 5)]],
    "9": [[(0, 0), (6, 0), (6, 10), (0, 10), (0, 5), (6, 5)]],
    "[": [[(4, 11), (1, 11), (1, -1), (4, -1)]],
    "]": [[(2, 11), (5, 11), (5, -1), (2, -1)]],
    "'": [[(0, 7), (0, 10)]],
    "*": [[(0, 0), (6, 10)], [(0, 10), (6, 0)], [(3, 0), (3, 10)]],
    "arrow": [[(0, 5), (6, 5)], [(3, 8), (6, 5), (3, 2)]],
}

WIDTH = {"'": 1.5, "[": 5, "]": 5}

#: The space between one character's cell and the next. Tight, as a real label is: a `1` sits in
#: the middle of a six-wide cell, so its box is three units from the next character's plus this, and
#: the whole label has to stay one run at the 4 pt run gap the templates were sized with.
SPACING = 0.5


def _path(parts: list[Points], dx: float, dy: float, scale: float = 1) -> VectorPath:
    segments: list[PathSegment] = []
    for part in parts:
        for position, (x, y) in enumerate(part):
            segments.append(
                PathSegment(
                    kind=SegmentKind.MOVE if position == 0 else SegmentKind.LINE,
                    point=(Decimal(str(dx + x * scale)), Decimal(str(dy + y * scale))),
                    closes=False,
                )
            )
    return VectorPath(
        segments=tuple(segments),
        stroked=True,
        filled=False,
        stroke_colour=(0, 0, 0, 255),
        fill_colour=(0, 0, 0, 255),
    )


def _row(text: str, x: float, y: float, scale: float = 1) -> tuple[list[VectorPath], list[str]]:
    """A row of characters, left to right. An inch mark `"` is two tick paths."""
    paths: list[VectorPath] = []
    labels: list[str] = []
    for character in text.replace('"', "''"):
        if character == " ":
            x += 4 * scale
            continue
        paths.append(_path(FONT[character], x, y, scale))
        labels.append(character)
        x += (WIDTH.get(character, 6) + SPACING) * scale
    return paths, labels


def _bar(x: float, y: float, width: float = 6) -> VectorPath:
    return _path([[(0, 0), (width, 0)]], x, y)


def _fraction(
    whole: str, numerator: str, denominator: str, x: float = 0, y: float = 0
) -> tuple[list[VectorPath], list[str]]:
    """A stacked fraction: a whole number as tall as the stack, the fraction, and an inch mark."""
    paths: list[VectorPath] = []
    labels: list[str] = []
    if whole:
        whole_paths, whole_labels = _row(whole, x, y, scale=2.2)
        paths += whole_paths
        labels += whole_labels
        x += (6 + SPACING) * 2.2 * len(whole)
    top, top_labels = _row(numerator, x, y + 12)
    bottom, bottom_labels = _row(denominator, x, y)
    width = (6 + SPACING) * max(len(numerator), len(denominator)) - SPACING
    ticks, tick_labels = _row('"', x + width + SPACING, y + 12)
    return (
        paths + top + [_bar(x, y + 11, width)] + bottom + ticks,
        labels + top_labels + ["BAR"] + bottom_labels + tick_labels,
    )


def _templates(*training: tuple[list[VectorPath], list[str]]) -> TemplateSet:
    """Label every character of the training labels, as a person does in phase B."""
    labels: list[str] = []
    shapes: list[GlyphShape] = []
    for paths, names in training:
        _, bars, described_shapes = described(paths, settings=SETTINGS)
        for index, (shape, name) in enumerate(zip(described_shapes, names, strict=True)):
            if index in bars or shape is None:
                continue
            labels.append("not_a_character" if name in {"*", "arrow"} else name)
            shapes.append(shape)
    return TemplateSet(
        set_hash="0" * 64,
        shape_settings=SHAPE.config_hash,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        labels=tuple(labels),
        shapes=tuple(shapes),
    )


#: What a person labelled: every digit large, and every digit small beside a whole number; a row
#: of bracketed inches; and an arrow, which is line-work.
TEMPLATES = _templates(
    _row('0123456789"', 0, 0),
    *[_fraction("1", digit, digit) for digit in "0123456789"],
    _row("[0123456789]", 0, 0),
    ([_path(FONT["arrow"], 0, 0)], ["arrow"]),
)


def _read(
    paths: list[VectorPath], templates: TemplateSet = TEMPLATES
) -> GlyphReading | GlyphAbstention:
    return read_label(paths, paths, templates=templates, settings=SETTINGS)


def _turned(paths: list[VectorPath], degrees: int) -> list[VectorPath]:
    """The label turned on the page: 90 to read up the sheet, 270 to read down it."""

    def turn(point: tuple[Decimal, Decimal]) -> tuple[Decimal, Decimal]:
        x, y = point
        return (-y, x) if degrees == 90 else (y, -x)

    return [
        VectorPath(
            segments=tuple(
                PathSegment(kind=s.kind, point=turn(s.point), closes=s.closes)
                for s in path.segments
            ),
            stroked=path.stroked,
            filled=path.filled,
            stroke_colour=path.stroke_colour,
            fill_colour=path.fill_colour,
        )
        for path in paths
    ]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_plain_label_reads_exactly() -> None:
    """Outcome: `12"` read as 12 inches exactly, upright."""
    paths, _ = _row('12"', 0, 0)

    reading = _read(paths)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '12"'
    assert reading.value.exact == 12
    assert reading.rotation_degrees == 0


def test_a_stacked_fraction_is_composed_from_geometry_never_as_a_whole_number() -> None:
    """**#726's failure.** Outcome: a stacked `3/4"` reads `3/4"`, never `3 3/4"`.

    Two vision readers agreed on `3 3/4"` for a stacked `3/4"`. Here the bar is found by where it
    is, the numerator is what sits over it, and there is no whole number for a `3` to become.
    """
    paths, _ = _fraction("", "3", "4")

    reading = _read(paths)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '3/4"'
    assert reading.value.exact == Fraction(3, 4)
    # #756 D3 (2026-10-01): read and pre-filled, and marked so it is never confirmed by agreement.
    assert reading.stacked is True


def test_a_whole_number_and_a_fraction_read_together() -> None:
    """Outcome: `1 5/8"` — a whole number beside a stacked fraction the templates never held."""
    paths, _ = _fraction("1", "5", "8")

    reading = _read(paths)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '1 5/8"'
    assert reading.value.exact == Fraction(13, 8)


def test_a_millimetre_line_over_its_bracketed_inches_reads_as_the_inches() -> None:
    """Outcome: `254` over `[10]` reads as the dual label, and its value is the inch half (Q12)."""
    top, _ = _row("254", 0, 20)
    bottom, _ = _row("[10]", 0, 0)

    reading = _read(top + bottom)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == "254 [10]"
    assert reading.value.exact == 10


def test_a_label_reading_up_the_sheet_is_turned_upright_by_the_drafting_convention() -> None:
    """**Acceptance criterion.** Outcome: a label turned 90° reads the same, with its turn recorded."""
    paths, _ = _row('45"', 0, 0)

    reading = _read(_turned(paths, 90))

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '45"'
    assert reading.rotation_degrees == 90


def test_a_label_reading_down_the_sheet_is_read_the_other_way() -> None:
    """**Acceptance criterion.** Outcome: turned 270°, the conventional direction fails and the
    opposite one reads it."""
    paths, _ = _row('45"', 0, 0)

    reading = _read(_turned(paths, 270))

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '45"'
    assert reading.rotation_degrees == 270


def test_a_turned_six_is_never_read_as_a_nine() -> None:
    """**The font test's only miss** (#756). Outcome: read as `6"`, or refused — never `9"`."""
    for degrees in (90, 270):
        paths, _ = _row('6"', 0, 0)

        reading = _read(_turned(paths, degrees))

        assert not (isinstance(reading, GlyphReading) and "9" in reading.text), reading
        if isinstance(reading, GlyphReading):
            assert reading.text == '6"'


def test_a_label_that_reads_validly_both_ways_up_abstains() -> None:
    """**Acceptance criterion.** Outcome: `96` over `[6]`, turned, reads `96 [6]` one way up and
    `96 [9]` the other — both valid dimensions — and the reader will not choose between them."""
    top, _ = _row("96", 0, 20)
    bottom, _ = _row("[6]", 0, 0)

    reading = _read(_turned(top + bottom, 90))

    assert isinstance(reading, GlyphAbstention), reading
    assert "both ways up" in reading.reason


# ---------------------------------------------------------------------------
# Refusing
# ---------------------------------------------------------------------------


def test_one_unknown_character_makes_the_whole_label_abstain() -> None:
    """**Never part of a label.** Outcome: `12"` with an unlabelled shape in it is refused whole."""
    paths, _ = _row('12"', 0, 0)
    paths.append(_path(FONT["*"], 12, 0))

    reading = _read(paths)

    assert isinstance(reading, GlyphAbstention)
    assert "not been labelled" in reading.reason


def test_line_work_inside_a_label_makes_it_abstain() -> None:
    """Outcome: a shape a person called line-work, inside the label, refuses the label."""
    paths, _ = _row('12"', 0, 0)
    paths.append(_path(FONT["arrow"], 12, 0))

    reading = _read(paths)

    assert isinstance(reading, GlyphAbstention)
    assert "piece of the drawing" in reading.reason


def test_an_ambiguous_match_is_unmatched() -> None:
    """Outcome: two templates of one shape with different labels match nothing, by the margin."""
    one, _ = _row("1", 0, 0)
    (shape,) = [shape for shape in described(one, settings=SETTINGS)[2] if shape is not None]
    confused = TemplateSet(
        set_hash="1" * 64,
        shape_settings=SHAPE.config_hash,
        glyph_gap_pt=SETTINGS.glyph_gap_pt,
        labels=TEMPLATES.labels + ("7",),
        shapes=TEMPLATES.shapes + (shape,),
    )
    paths, _ = _row('12"', 0, 0)

    reading = _read(paths, confused)

    assert isinstance(reading, GlyphAbstention)
    assert "not been labelled" in reading.reason


def test_a_label_that_runs_into_other_text_abstains() -> None:
    """Outcome: characters that keep joining past `maximum_label_pt` are not one label."""
    paths, _ = _row("0123456789" * 2, 0, 0)

    reading = _read(paths)

    assert isinstance(reading, GlyphAbstention)
    assert "more text than one label holds" in reading.reason


def test_three_lines_is_not_a_dimension() -> None:
    top, _ = _row("12", 0, 40)
    middle, _ = _row("34", 0, 20)
    bottom, _ = _row('56"', 0, 0)

    reading = _read(top + middle + bottom)

    assert isinstance(reading, GlyphAbstention)
    assert "3 lines" in reading.reason


def test_a_bare_number_is_not_a_dimension() -> None:
    """Outcome: `12` with no unit reads as characters but values as nothing, so it abstains."""
    paths, _ = _row("12", 0, 0)

    reading = _read(paths)

    assert isinstance(reading, GlyphAbstention)


def test_templates_sized_another_way_are_refused() -> None:
    """Outcome: a template set sized with another run gap cannot be read against."""
    other = TemplateSet(
        set_hash=TEMPLATES.set_hash,
        shape_settings=TEMPLATES.shape_settings,
        glyph_gap_pt=Decimal("2.5"),
        labels=TEMPLATES.labels,
        shapes=TEMPLATES.shapes,
    )
    paths, _ = _row('12"', 0, 0)

    with pytest.raises(ValueError, match="run gap"):
        _read(paths, other)


# ---------------------------------------------------------------------------
# Gathering, and loading a set
# ---------------------------------------------------------------------------


def test_a_label_is_gathered_whole_from_the_page_rather_than_cut_to_its_seed() -> None:
    """**#641's cut-label failure.** Outcome: seeded with one character, the whole label is read."""
    paths, _ = _row('192"', 0, 0)
    elsewhere, _ = _row('7"', 200, 200)

    label, unclosed = gather_label(paths[2:3], paths + elsewhere, settings=SETTINGS)

    assert unclosed is None
    assert len(label) == len(paths)
    reading = read_label(paths[2:3], paths + elsewhere, templates=TEMPLATES, settings=SETTINGS)
    assert isinstance(reading, GlyphReading) and reading.text == '192"'


#: A label gap narrower than the run gap, as a real deployment's can be (phase D locked 3.0 pt against
#: the 4 pt run gap the templates were sized with). The band between them is where a label's end is
#: not settled.
NARROW = replace(SETTINGS, label_gap_pt=Decimal(3))


def test_a_character_just_past_a_labels_end_on_its_line_makes_it_abstain() -> None:
    """**The phase D wrong reading, reproduced** (#756, 2026-10-01). A `1` stands in the middle of its
    cell, so the space after it is wider than between other characters: in `10"` it is 3.5 units
    here, past a 3-unit label gap. Before this rule the label closed without the `1` and read `0"`.
    Outcome: the reader refuses — the `1` shares the label's run, so its end is not settled."""
    paths, _ = _row('10"', 0, 0)

    label, unclosed = gather_label(paths[1:], paths, settings=NARROW)
    reading = read_label(paths[1:], paths, templates=TEMPLATES, settings=NARROW)

    assert len(label) == len(paths) - 1, "the 1 is past the label gap"
    assert unclosed is not None and "just past" in unclosed
    assert isinstance(reading, GlyphAbstention), f"read {getattr(reading, 'text', None)!r}"
    assert "just past" in reading.reason


def test_a_character_near_a_label_but_off_its_line_does_not_stop_it() -> None:
    """Outcome: a character 3.5 units above the label — inside the run gap, but on another line — is
    not the label continuing, so `20"` still reads."""
    paths, _ = _row('20"', 0, 0)
    above, _ = _row("7", 0, 13.5)

    reading = read_label(paths, paths + above, templates=TEMPLATES, settings=NARROW)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '20"'


def test_a_character_on_the_line_but_past_the_run_gap_does_not_stop_it() -> None:
    """Outcome: the next label along the same line, further away than any run joins, is another
    label — `20"` still reads."""
    paths, _ = _row('20"', 0, 0)
    right = max(point[0] for path in paths for point in path.points)
    next_label, _ = _row('7"', float(right) + 5, 0)

    reading = read_label(paths, paths + next_label, templates=TEMPLATES, settings=NARROW)

    assert isinstance(reading, GlyphReading), reading
    assert reading.text == '20"'


def test_a_template_set_is_identified_by_its_hash(tmp_path: Path) -> None:
    """Outcome: a set whose folder name is not its hash is refused on loading."""
    digest = "ab" * 32
    folder = tmp_path / digest[:12]
    folder.mkdir()
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "shape_settings": SHAPE.config_hash,
                "reader_settings": {"GV_READER_GLYPH_GAP_PT": "4"},
            }
        ),
        encoding="utf-8",
    )
    np.savez_compressed(
        folder / "templates.npz",
        rasters=np.stack([shape.raster for shape in TEMPLATES.shapes]),
        labels=np.array(TEMPLATES.labels),
        relative_heights=np.array([str(shape.relative_height) for shape in TEMPLATES.shapes]),
        relative_widths=np.array([str(shape.relative_width) for shape in TEMPLATES.shapes]),
        dots=np.array([shape.dot for shape in TEMPLATES.shapes]),
    )

    loaded = TemplateSet.load(folder)
    assert loaded.labels == TEMPLATES.labels
    assert loaded.glyph_gap_pt == Decimal(4)

    renamed = tmp_path / "not-the-hash"
    folder.rename(renamed)
    with pytest.raises(ValueError, match="hash"):
        TemplateSet.load(renamed)


def test_a_label_that_runs_past_the_area_asked_about_abstains() -> None:
    """**Acceptance criterion** (#641's cut label). Outcome: asked about a crop that shows `92"` of
    `192"`, the reader refuses rather than read either the part or the whole."""
    paths, _ = _row('192"', 0, 0)
    crop = (Decimal(6), Decimal(-1), Decimal(40), Decimal(11))

    reading = read_label(paths[1:], paths, templates=TEMPLATES, settings=SETTINGS, within=crop)

    assert isinstance(reading, GlyphAbstention)
    assert "past the edge" in reading.reason
    whole = read_label(paths, paths, templates=TEMPLATES, settings=SETTINGS, within=None)
    assert isinstance(whole, GlyphReading) and whole.text == '192"'


def _match_one_pair_at_a_time(shape, templates, settings):  # type: ignore[no-untyped-def]
    """The matching rule written plainly, pair by pair — the oracle the vectorised one must equal."""
    from extraction.glyph_shapes import chamfer, size_ratio

    best: dict[str, Fraction] = {}
    for label, template in zip(templates.labels, templates.shapes, strict=True):
        if shape.dot or template.dot:
            if shape.dot and template.dot:
                best[label] = Fraction(0)
            continue
        if size_ratio(shape, template) > Fraction(settings.maximum_size_ratio):
            continue
        distance = chamfer(shape.raster, template.raster)
        if label not in best or distance < best[label]:
            best[label] = distance
    if not best:
        return None
    ranked = sorted(best.items(), key=lambda entry: (entry[1], entry[0]))
    label, distance = ranked[0]
    if distance > Fraction(settings.maximum_distance):
        return None
    if len(ranked) > 1 and ranked[1][1] - distance < Fraction(settings.minimum_margin):
        return None
    return label


def test_matching_every_template_at_once_decides_exactly_what_one_pair_at_a_time_does() -> None:
    """**Outcome: the same label, character for character, over every shape in these tests.**

    The vectorised matcher exists for speed on a whole drawing; it may not decide anything the
    plain rule would not.
    """
    from extraction.glyph_reader import _match

    labels = [
        _row('0123456789"', 0, 0),
        _row("[0123456789]", 0, 0),
        _fraction("1", "3", "4"),
        _fraction("", "7", "8"),
        _row('12"', 0, 0),
    ]
    checked = 0
    for paths, _ in labels:
        _, bars, shapes = described(paths, settings=SETTINGS)
        for index, shape in enumerate(shapes):
            if index in bars or shape is None:
                continue
            assert _match(shape, TEMPLATES, SETTINGS) == _match_one_pair_at_a_time(
                shape, TEMPLATES, SETTINGS
            )
            checked += 1
    assert checked > 30


def test_a_label_without_a_fraction_bar_is_not_marked_stacked() -> None:
    """The mark is earned by the bar alone: a plain label is confirmed like any other reading."""
    paths, _ = _row('12"', 0, 0)
    reading = _read(paths)

    assert isinstance(reading, GlyphReading), reading
    assert reading.stacked is False
