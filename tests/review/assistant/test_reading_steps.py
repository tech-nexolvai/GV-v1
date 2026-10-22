"""More pen-stroke numbers read (#1860): mm/inch cross-check, read_number at pack time, the alphabet.

Everything here is made up in the test: numbers drawn as pen strokes (the made-up stroke font of
`test_ocr_labels`), scripted OCR engines, and invented values. No model is called, no OCR engine
is needed.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import ink_labels
from app.review.assistant import read_number as rn
from app.review.assistant import vendor_alphabet as va
from tests.review.assistant.test_ocr_labels import _ADVANCE, _STROKES

# ---- made-up strokes as ink ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Path:
    box: tuple[float, float, float, float]
    polylines: tuple[tuple[tuple[float, float], ...], ...]
    filled: bool = False
    width: float = 0.5


def _label(text: str, x: float = 100.0, y: float = 100.0, size: float = 8.0) -> ink_labels.Label:
    """`text` drawn as strokes reading left to right, top of the letters at `y` (shown points)."""
    unit = size / 9.0
    paths: list[_Path] = []
    pen = 0.0
    for char in text:
        for stroke in _STROKES[char]:
            points = tuple((x + (pen + gx) * unit, y + (9 - gy) * unit) for gx, gy in stroke)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            paths.append(_Path((min(xs), min(ys), max(xs), max(ys)), (points,)))
        pen += _ADVANCE.get(char, 7.5)
    xs = [v for p in paths for v in (p.box[0], p.box[2])]
    ys = [v for p in paths for v in (p.box[1], p.box[3])]
    found = ink_labels.labels(paths, (min(xs), min(ys), max(xs), max(ys)))
    assert len(found) == 1, found
    return found[0]


def _taught(*values: str) -> va.Alphabet:
    """An alphabet learned from made-up confirmed labels, one a page."""
    samples: list[va.Sample] = []
    for page, text in enumerate(values, start=1):
        value = Decimal(text.rstrip('"'))
        samples += va.samples_of(_label(text), 0, value, page)
    return va.Alphabet(samples)


# ---- the vendor's alphabet -----------------------------------------------------------------------


def test_a_confirmed_label_teaches_one_sample_a_character() -> None:
    samples = va.samples_of(_label('18"'), 0, Decimal(18), 3)

    assert [(s.digit, s.page) for s in samples] == [("1", 3), ("8", 3)]


def test_a_label_whose_characters_do_not_match_its_value_teaches_nothing() -> None:
    assert va.samples_of(_label('18"'), 0, Decimal(128), 1) == []


def test_the_alphabet_reads_a_number_of_learned_shapes() -> None:
    alphabet = _taught('18"', '32"', '64"', '70"')

    assert alphabet.read(_label('81"'), 0) == Decimal(81)
    assert alphabet.read(_label('2"'), 0) == Decimal(2)


def test_the_alphabet_abstains_on_a_shape_it_never_saw() -> None:
    alphabet = _taught('18"', '32"')

    assert alphabet.name(va.describe(_label('6"').glyphs[0], 0)) is None
    assert alphabet.read(_label('16"'), 0) is None


def test_a_page_named_by_the_other_pages_only() -> None:
    alphabet = _taught('18"', '81"', '32"')
    sample = alphabet.samples[0]

    assert alphabet.name(sample, skip_page=1) == sample.digit


def test_the_alphabet_round_trips_through_the_pack() -> None:
    alphabet = _taught('18"', '32"')

    again = va.Alphabet.from_json(alphabet.as_json())

    assert again.read(_label('23"'), 0) == Decimal(23)
    assert va.Alphabet.from_json(None).read(_label('23"'), 0) is None


# ---- read_number: the alphabet is a second witness only ------------------------------------------


@pytest.mark.parametrize(
    ("given", "value", "code"),
    [
        ({"ocr": Decimal(23), "alphabet": Decimal(23)}, Decimal(23), "ocr_alphabet"),
        ({"ocr": Decimal(23), "alphabet": Decimal(28)}, None, "readers_disagree"),
        ({"alphabet": Decimal(23)}, None, "no_reader"),
        ({"alphabet": Decimal(23), "ruler": 23.0}, None, "no_reader"),
        ({"shape": Decimal(23), "alphabet": Decimal(23)}, None, "one_route"),
        (
            {"ocr": Decimal(23), "shape": Decimal(28), "alphabet": Decimal(23)},
            None,
            "readers_disagree",
        ),
        (
            {"ocr": Decimal(23), "shape": Decimal(23), "alphabet": Decimal(23)},
            Decimal(23),
            "ocr_shape",
        ),
    ],
)
def test_the_alphabet_counts_only_beside_the_ocr_pair(
    given: dict[str, Any], value: Decimal | None, code: str
) -> None:
    found = rn.decide(**given)

    assert (found.value, found.code) == (value, code)


def test_a_pack_reader_loads_the_alphabet_the_pack_keeps(tmp_path: Any) -> None:
    alphabet = _taught('18"', '32"')
    (tmp_path / "dims.json").write_text(
        __import__("json").dumps({"pages": [], "alphabet": alphabet.as_json()})
    )

    reader = rn.reader_for_pack(tmp_path)

    assert len(reader.alphabet().samples) == len(alphabet.samples)
    assert rn.reader_for_pack(None).alphabet().samples == []


# ---- millimetres over inches ---------------------------------------------------------------------


def _mm_label(upper: str, lower: str) -> ink_labels.Label:
    """Millimetres over bracketed inches: the digits of `upper`, and `[` `lower` `"` `]` drawn
    under them (the brackets as tall thin strokes)."""
    top = _label(upper, 100.0, 100.0)
    under = _label(lower + '"', 104.0, 111.0)
    x0, y0, x1, y1 = under.box
    left = _Path(
        (x0 - 2.0, y0 - 1.0, x0 - 2.0, y1 + 1.0), (((x0 - 2.0, y0 - 1.0), (x0 - 2.0, y1 + 1.0)),)
    )
    right = _Path(
        (x1 + 2.0, y0 - 1.0, x1 + 2.0, y1 + 1.0), (((x1 + 2.0, y0 - 1.0), (x1 + 2.0, y1 + 1.0)),)
    )
    paths = [p for g in (*top.glyphs, *under.glyphs) for p in g.paths] + [left, right]
    box = (
        min(p.box[0] for p in paths),
        min(p.box[1] for p in paths),
        max(p.box[2] for p in paths),
        max(p.box[3] for p in paths),
    )
    (found,) = [lab for lab in ink_labels.labels(paths, box) if dl._overlap(lab.box, box) > 0.5]
    return found


class _Rows:
    """Reads the millimetre row (narrow, few strokes) as `mm` and an inch row as `inch`; with its
    brackets (`bracket`) the inch row reads `[inch]`."""

    def __init__(self, name: str, mm: str, inch: str, bracket: bool = True) -> None:
        self.name = name
        self.mm, self.inch, self.bracket = mm, inch, bracket
        self.seen: list[int] = []

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        dark = picture < 128
        columns = int(np.count_nonzero(dark.any(axis=0)))
        self.seen.append(columns)
        return [self._line(picture)]

    def _line(self, picture: np.ndarray) -> str:
        # The rows are told apart by their ink's height: the millimetres are drawn first.
        if len(self.seen) % 3 == 1:
            return self.mm
        if len(self.seen) % 3 == 2:
            return f'[{self.inch}"]' if self.bracket else ""
        return f'{self.inch}"'


@pytest.mark.parametrize(
    ("rapid", "tesseract", "want"),
    [
        (("203", "8"), ("203", "8"), (Decimal(8), "203")),
        # One engine reads nothing with the brackets: the bracket-free row still reads.
        (("203", "8"), ("203", "8", False), (Decimal(8), "203")),
        # A misread inch row is caught by the millimetres, and the other way round.
        (("203", "16"), ("203", "16"), None),
        (("406", "8"), ("406", "8"), None),
    ],
)
def test_millimetres_and_inches_confirm_each_other(
    rapid: Sequence[Any], tesseract: Sequence[Any], want: Any
) -> None:
    label = _mm_label("203", "8")
    assert dl._mm_rows(label) == 0
    engines = [_Rows("RapidOCR", *rapid), _Rows("Tesseract", *tesseract)]

    assert dl._mm_cross(label, 0, engines) == want


def test_a_character_drawn_in_two_pieces_counts_once() -> None:
    label = _label('73"')
    (row,) = ink_labels.rows(label, 0)
    seven = row[0].glyphs[0]
    # Split the 7's top bar off as a small mark of its own.
    bar = _Path(seven.paths[0].box, (seven.paths[0].polylines[0][:2],))
    rest = _Path(seven.paths[0].box, (seven.paths[0].polylines[0][1:],))
    split = [
        ink_labels.Token("glyph", (ink_labels.Glyph((rest,), seven.box),), (), 0.0, 0.0),
        ink_labels.Token(
            "mark",
            (
                ink_labels.Glyph(
                    (bar,), (seven.box[0], seven.box[1], seven.box[2] - 1, seven.box[1] + 0.5)
                ),
            ),
            (),
            0.0,
            0.0,
        ),
        *row[1:],
    ]

    merged = dl._merged(split, 0)

    assert [t.kind for t in merged][:2] == ["glyph", "glyph"]
    assert len(merged) == len(row)


# ---- read_number at pack time --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "glyphs", "number"),
    [
        ('23"', 4, True),
        ("1 3/8", 4, True),
        ("203", 3, True),
        ("WD-02", 5, False),
        ("11", 2, False),
        ('1"', 3, False),
        ("TYP 23", 6, False),
        ("", 3, False),
    ],
)
def test_the_quick_look_keeps_what_looks_like_a_number(
    text: str, glyphs: int, number: bool
) -> None:
    assert dl._looks_like_number(text, glyphs) is number


def test_an_ocr_only_reading_is_seconded_only_by_a_matching_alphabet() -> None:
    alphabet = _taught('18"', '32"', '64"')
    ink = _label('23"')
    held = dl.SweepHeld("set.pdf", 1, ink.box, Decimal(23), None, None, ink, (0,))
    wrong = dl.SweepHeld("set.pdf", 1, ink.box, Decimal(28), None, None, ink, (0,))

    assert [(item.ocr, value) for item, value in dl.seconded([held, wrong], alphabet)] == [
        (Decimal(23), Decimal(23))
    ]
