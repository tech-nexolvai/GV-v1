"""The readers can tie the right number to the item (2026-10-29). $0: made-up labels, fake models.

On a real upload both readers read the vendor's upper outlet height and gave null for the
architect's 6' - 0": the architect's picture showed the number and its dimension's ends, but the
outlet it measures to was 6 pt outside the picture, and the elevation was turned on its side. The
quick first reader was also "unsure" on 56 readings, half of them items with no number to copy.

- A label's picture reaches what its dimension measures: at least `CONTEXT_PT` a side, its
  extension lines' far ends (at most `REACH_PT` away), and with no dimension line in the pack
  `NO_LINE_ALONG_PT` both ways; a close-up of the label keeps the number sharp.
- A label written up the page: the region as drawn, and its close-up turned upright.
- The question says how a value is tied to the item, that a value in two units is copied whole,
  and what "sure" means; it never carries a value written in the item's name.
- An item no red can come from whose every side is words is not read.
- Unchanged: a read must be sure, and code compares its numbers with the finding's exactly.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.review.assistant import read_adjudication, read_spots, second_read
from app.review.assistant.chat_engine import _nothing_to_copy, could_go_red
from app.review.assistant.read_adjudication import SideCrops
from app.review.assistant.read_spots import (
    CONTEXT_PT,
    MIN_SIDE_PT,
    NO_LINE_ALONG_PT,
    PAD_PT,
    REACH_PT,
    spots,
)
from app.review.assistant.second_read import (
    SURE_WORDS,
    TIE_WORDS,
    UNITS_WORDS,
    SecondRead,
    compare_read,
    neutral_item,
    picture_names,
)
from tests.review.assistant.test_second_reads_first import _engine, _finding, _queue

FILE = "set.pdf"


def _label(box: list[float], orientation: str, line: Any = None, extension: Any = None) -> dict:
    return {
        "id": "D2",
        "text": "6' - 0\"",
        "box": box,
        "side": "architect",
        "orientation": orientation,
        "dim_line": line,
        "extension": extension,
    }


def _spot(label: dict[str, Any]) -> read_spots.ReadSpot:
    box = label["box"]
    finding = {
        "id": "F1",
        "item": "Buffet: upper outlet height",
        "status": "different",
        "architect": "6' - 0\"",
        "vendor": '73 1/2"',
        "arch_box": {
            "page": 3,
            "x0": box[0],
            "y0": box[1],
            "x1": box[2],
            "y1": box[3],
            "file": FILE,
        },
        "dims": {"architect": {"id": "D2", "box": box, "page": 3, "file": FILE}},
    }
    return spots(finding, lambda f, p: [label])["architect"]


def _inside(point: tuple[float, float], region: tuple[float, float, float, float]) -> bool:
    return region[0] <= point[0] <= region[2] and region[1] <= point[1] <= region[3]


# ---- the picture reaches what the dimension measures --------------------------------------------------


def test_the_architects_outlet_height_shows_the_outlet() -> None:
    """The real upload's geometry: a 6' - 0" written up the page with no dimension line in the
    pack, the outlet it measures to 102 pt to its left, at the dimension's top end."""
    label = _label([753.52, 154.25, 761.63, 174.75], "v")
    outlet = (655.5, 110.7)
    region = _spot(label).region
    assert _inside(outlet, region)
    assert region[2] - region[0] == NO_LINE_ALONG_PT and region[3] - region[1] == NO_LINE_ALONG_PT
    # Twice the minimum across, as before, left it 6 pt outside.
    centre_x = (label["box"][0] + label["box"][2]) / 2
    assert outlet[0] < centre_x - MIN_SIDE_PT


def test_the_region_follows_the_extension_lines_but_not_too_far() -> None:
    line = [[300.0, 380.0], [300.0, 450.0]]
    near = _label([296.0, 405.0, 304.0, 425.0], "v", line, [[[180.0, 380.0], [300.0, 380.0]]])
    region = _spot(near).region
    assert _inside((180.0, 380.0), region)
    assert region[0] == pytest.approx(180.0 - PAD_PT)  # centred: as far the other way too
    far = _label([296.0, 405.0, 304.0, 425.0], "v", line, [[[-300.0, 450.0], [300.0, 450.0]]])
    region = _spot(far).region
    assert region[2] - region[0] == pytest.approx(2 * (REACH_PT + PAD_PT))
    assert region[0] == pytest.approx(300.0 - REACH_PT - PAD_PT)
    # No extension lines: the dimension line and the least context decide.
    bare = _label([296.0, 405.0, 304.0, 425.0], "v", line)
    region = _spot(bare).region
    assert (region[2] - region[0], region[3] - region[1]) == (CONTEXT_PT, CONTEXT_PT)


def test_every_label_region_adds_its_close_up() -> None:
    for label in (
        _label([753.52, 154.25, 761.63, 174.75], "v"),
        _label([296.0, 405.0, 304.0, 425.0], "v", [[300.0, 380.0], [300.0, 450.0]]),
        _label([296.0, 405.0, 316.0, 413.0], "h", [[280.0, 415.0], [330.0, 415.0]]),
    ):
        spot = _spot(label)
        assert spot.zoom is not None
        assert (spot.zoom[2] - spot.zoom[0], spot.zoom[3] - spot.zoom[1]) == (
            MIN_SIDE_PT,
            MIN_SIDE_PT,
        )


# ---- what each picture is ------------------------------------------------------------------------------


def test_a_label_up_the_page_shows_the_drawing_as_drawn_and_its_close_up_upright() -> None:
    crop = SideCrops(upright=b"region", zoom=b"close-turned", zoom_upright=True)
    assert crop.pictures() == [("plain", b"region"), ("zoom_upright", b"close-turned")]
    crop_kinds = [("architect", kind) for kind, _png in crop.pictures()]
    names = picture_names(crop_kinds)
    assert names == (
        "image 1 is the architect's drawing; image 2 is a close-up of the same spot on the "
        "architect's drawing, centred the same and turned a quarter turn, so its sideways text "
        "reads upright"
    )
    # The third reading names the pictures the same way, and records that text was turned.
    assert "turned a quarter turn, so its sideways text reads upright" in (
        read_adjudication.question("Top", crop_kinds)
    )

    class Model:
        def __call__(self, content: list[dict[str, Any]]) -> str:
            return '{"architect": "6\' - 0\\"", "vendor": null, "sure": true}'

    done = read_adjudication.adjudicate(
        {"item": "Top", "architect": "6' - 0\"", "vendor": None},
        {"architect": crop},
        Model(),
        model="sonnet",
        second_model="haiku",
    )
    assert done.reads[-1]["turned"] is True


# ---- what the readers are asked -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "asked"),
    [
        ('Buffet: heights stack (28 3/4")', "Buffet: heights stack"),
        ('Buffet: outlet spacing 33"', "Buffet: outlet spacing"),
        ("Breakfast 2: height 2'-0\"", "Breakfast 2: height"),
        ("Upper 6' - 0\" outlet", "Upper outlet"),
        ("Counter 610 [24] deep", "Counter deep"),
        ("Base B18L: width", "Base B18L: width"),
        ("Spacing between the two microwave outlets", "Spacing between the two microwave outlets"),
    ],
)
def test_the_question_never_carries_a_value_written_in_the_items_name(
    name: str, asked: str
) -> None:
    assert neutral_item(name) == asked
    for text in (
        second_read.question(name, ("architect", "vendor")),
        read_adjudication.question(name, (("architect", False), ("vendor", False))),
    ):
        assert f'<drawing_data source="item">\n{asked}\n</drawing_data>' in text


def test_a_name_that_is_only_a_value_is_asked_as_this_item() -> None:
    assert '<drawing_data source="item">\nthis item\n</drawing_data>' in second_read.question(
        '33"', ("vendor",)
    )


def test_both_readers_are_told_how_to_tie_a_value_and_what_sure_means() -> None:
    for text in (
        second_read.question("Top", ("architect", "vendor"), centred=True),
        read_adjudication.question("Top", (("architect", False), ("vendor", False)), centred=True),
    ):
        assert TIE_WORDS in text and UNITS_WORDS in text and SURE_WORDS in text
        assert "you are not sure" in text
    schema = second_read.READ_SCHEMA["properties"]["sure"]["description"]
    assert "the one for the item" in schema


# ---- the rule is unchanged ----------------------------------------------------------------------------


def test_a_read_still_counts_only_when_sure_and_exactly_the_same_number() -> None:
    finding = {"architect": '9"', "vendor": "254 [10]"}
    both = ("architect", "vendor")
    right = SecondRead(architect='9"', vendor="254 [10]", sure=True, asked=both)
    assert compare_read(right, finding).result == "agrees"
    assert compare_read(SecondRead('9"', "254 [10]", asked=both), finding).result == "unsure"
    # Only the millimetres copied: 254 inches, never the same number.
    assert compare_read(SecondRead('9"', "254", sure=True, asked=both), finding).result == (
        "disagrees"
    )
    assert compare_read(SecondRead('9"', None, sure=True, asked=both), finding).result == "unsure"


# ---- an item with no number is not read ---------------------------------------------------------------


def _look(id_: str, architect: str, vendor: str, **more: Any) -> dict[str, Any]:
    return _finding(
        id_, "check", item=f"this piece: {id_}", architect=architect, vendor=vendor, **more
    )


@pytest.mark.parametrize(
    ("architect", "vendor", "nothing"),
    [
        ("3 drawers left + 1 door right", "4 fronts left", True),
        ("no outlet in sink base", "1 outlet in sink base", True),
        ("WT-03.04 tag printed", "arrow with no tag text", True),
        ("2 outlets", "no outlets", True),
        ("about 4'-0\" scaled", "915 [36] wall to wall", True),
        ("3' - 0\"", "966 [38]", False),
        ('1’ − 0"', "177 [7]", False),
        ("see GV Review's record", "1828 [72]", False),  # the vendor's side is a number
        ('9"', "3 drawers", False),
        ("96", "no shelves", False),
    ],
)
def test_nothing_to_copy_is_words_on_every_side_read(
    architect: str, vendor: str, nothing: bool
) -> None:
    assert _nothing_to_copy(_look("F1", architect, vendor)) is nothing


def test_a_vendor_strings_architect_side_is_not_read_so_it_does_not_count() -> None:
    string = _look("F1", "the parts add up", '39 1/2"', source="vendor_arithmetic")
    assert not _nothing_to_copy(string)
    words = _look("F2", '39 5/8"', "a string", source="vendor_arithmetic")
    assert _nothing_to_copy(words)


def test_an_item_with_no_number_is_not_read_and_takes_no_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = _engine(monkeypatch)
    look = _look("F1", "3 drawers left + 1 door right", "4 fronts left")
    size = _finding("F2", "check")
    red = _finding("F3", "different", inches={"vendor": "31", "architect": "30"})
    assert not could_go_red(look) and could_go_red(red)
    assert _queue(engine, [look, size, red], final=True) == ["F2", "F3"]
    assert engine._others_read == {"F2"}
    assert engine.unread == []


def test_a_would_be_red_is_read_whatever_its_words(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only items no red can come from are skipped: a would-be red keeps its reading (and its
    hold when the reading cannot agree)."""
    engine = _engine(monkeypatch)
    red = _finding(
        "F1",
        "different",
        architect="a tag",
        vendor="another tag",
        inches={"vendor": "31", "architect": "30"},
    )
    assert could_go_red(red) and _nothing_to_copy(red)
    assert _queue(engine, [red]) == ["F1"]
