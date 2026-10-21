"""Dimension labels inside pasted pictures (#1830): read by two OCR engines, never a lone read.

Every picture and PDF here is made in the test: a made-up dimension label rendered into a small
grey picture, pasted into a made-up PDF either as a rectangle comment holding the picture (a
viewer's "paste image") or as the page's own image (a scanned page). Most tests use scripted OCR
engines, so they need no engine installed; the last ones read real rendered labels with the two
local engines (RapidOCR and Tesseract) and are skipped where either is missing. No model is ever
called. The values are invented.
"""

from __future__ import annotations

import zlib
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pikepdf
import pytest
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.review.assistant import dim_labels as dl
from app.review.assistant import picture_labels as pl
from app.review.assistant import verify_value as vv
from app.review.assistant.dim_findings import label_checks
from app.review.assistant.simple_check import verify as simple_verify

PAGE = (600.0, 400.0)
PICTURE_BOX = (60.0, 60.0, 510.0, 270.0)  # shown points: 450 x 210
PICTURE_PX = (900, 420)  # 2 pixels a point, as a pasted snapshot
HALVES = [{"box": [40, 40, 530, 290], "role": "architect"}]


# ---- made-up pictures and PDFs -------------------------------------------------------------------


def _label_picture(text: str, size: int = 24) -> Image.Image:
    font = ImageFont.load_default(size=size)
    left, top, right, bottom = font.getbbox(text)
    image = Image.new("L", (int(right - left) + 8, int(bottom - top) + 8), 255)
    ImageDraw.Draw(image).text((4 - left, 4 - top), text, font=font, fill=0)
    return image


def _picture(
    items: Sequence[tuple[str, tuple[int, int], int]], *, blur: float = 0.0
) -> Image.Image:
    """A white picture with each `(text, (x, y), turn)` rendered at its pixel spot over a dimension
    line (`turn` 90: turned to read up the page)."""
    image = Image.new("L", PICTURE_PX, 255)
    draw = ImageDraw.Draw(image)
    for text, (x, y), turn in items:
        label = _label_picture(text)
        if turn == 90:
            label = label.rotate(90, expand=True, fillcolor=255)
        image.paste(label, (x, y))
        if turn == 0:
            line_y = y + label.height + 6
            draw.line([(x - 40, line_y), (x + label.width + 40, line_y)], fill=0, width=2)
        else:
            line_x = x + label.width + 6
            draw.line([(line_x, y - 40), (line_x, y + label.height + 40)], fill=0, width=2)
    return image.filter(ImageFilter.GaussianBlur(blur)) if blur else image


def _marked(turn: int) -> Image.Image:
    """For the scripted engines: one 'label', a bar with a heavy left end (its start), at pixel
    (300, 150), turned `turn` (90: reads up the page)."""
    label = Image.new("L", (80, 24), 255)
    draw = ImageDraw.Draw(label)
    draw.rectangle((0, 0, 14, 23), fill=0)
    draw.rectangle((14, 8, 79, 15), fill=0)
    if turn == 90:
        label = label.rotate(90, expand=True, fillcolor=255)
    image = Image.new("L", PICTURE_PX, 255)
    image.paste(label, (300, 150))
    return image


def _image_stream(pdf: pikepdf.Pdf, image: Image.Image) -> pikepdf.Stream:
    stream = pikepdf.Stream(pdf, zlib.compress(image.tobytes()))
    stream.Type = pikepdf.Name.XObject
    stream.Subtype = pikepdf.Name.Image
    stream.Width, stream.Height = image.width, image.height
    stream.ColorSpace = pikepdf.Name.DeviceGray
    stream.BitsPerComponent = 8
    stream.Filter = pikepdf.Name.FlateDecode
    return stream


def _pdf(
    path: Path,
    image: Image.Image,
    *,
    pasted: bool = True,
    box: tuple[float, float, float, float] = PICTURE_BOX,
    text: str = "",
) -> Path:
    """A one-page PDF (600 x 400 points) showing `image` at `box` (shown points): as a rectangle
    comment holding it (`pasted`), or in the page's own content (a scanned page). `text`: page
    content drawn over it (Helvetica)."""
    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=PAGE)
    page = pdf.pages[0]
    picture = _image_stream(pdf, image)
    x0, top, x1, bottom = box
    width, height = x1 - x0, bottom - top
    y0 = PAGE[1] - bottom
    font = pikepdf.Dictionary(
        Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1, BaseFont=pikepdf.Name.Helvetica
    )
    content = text.encode()
    if pasted:
        appearance = pikepdf.Stream(pdf, f"q {width} 0 0 {height} 0 0 cm /Im0 Do Q".encode())
        appearance.Type = pikepdf.Name.XObject
        appearance.Subtype = pikepdf.Name.Form
        appearance.BBox = [0, 0, width, height]
        appearance.Resources = pikepdf.Dictionary(XObject=pikepdf.Dictionary(Im0=picture))
        comment = pikepdf.Dictionary(
            Type=pikepdf.Name.Annot,
            Subtype=pikepdf.Name.Square,
            Rect=[x0, y0, x1, y0 + height],
            AP=pikepdf.Dictionary(N=appearance),
        )
        page.obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(comment)]))
        page.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(F1=font))
    else:
        page.obj.Resources = pikepdf.Dictionary(
            XObject=pikepdf.Dictionary(Im0=picture), Font=pikepdf.Dictionary(F1=font)
        )
        content = f"q {width} 0 0 {height} {x0} {y0} cm /Im0 Do Q ".encode() + content
    page.obj.Contents = pikepdf.Stream(pdf, content)
    pdf.save(path)
    return path


# ---- scripted engines ----------------------------------------------------------------------------


def _ink(picture: np.ndarray) -> tuple[int, int, int, int] | None:
    ys, xs = np.nonzero(picture < 128)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


def _upright(picture: np.ndarray) -> bool:
    """The scripted label reads upright when its heavy end is on the left."""
    box = _ink(picture)
    if box is None:
        return False
    x0, _y0, x1, _y1 = box
    quarter = max(1, (x1 - x0) // 4)
    dark = picture < 128
    return int(dark[:, x0 : x0 + quarter].sum()) > int(dark[:, x1 - quarter : x1].sum())


class _Detector:
    """Finds the one bar lying flat in a turned picture; reads it as `found`."""

    def __init__(self, found: str) -> None:
        self.found = found

    def __call__(self, picture: np.ndarray) -> list[tuple[Any, str]]:
        box = _ink(picture)
        if box is None:
            return []
        x0, y0, x1, y1 = box
        if x1 - x0 < y1 - y0:
            return []  # standing: found in another turn
        text = self.found if _upright(picture) else "-"
        return [([[x0 - 2, y0 - 2], [x1 + 2, y0 - 2], [x1 + 2, y1 + 2], [x0 - 2, y1 + 2]], text)]


class _Reader:
    """Reads a line upright as `text`; upside down (or sideways) as `other`."""

    def __init__(self, name: str, text: str, other: str = "-") -> None:
        self.name = name
        self.text = text
        self.other = other
        self.calls = 0

    def __call__(self, picture: np.ndarray, *, single_line: bool) -> list[str]:
        self.calls += 1
        return [self.text if _upright(picture) else self.other]


def _engines(rapid: str, tesseract: str) -> list[_Reader]:
    return [_Reader("RapidOCR", rapid), _Reader("Tesseract", tesseract)]


def _grey(image: Image.Image) -> np.ndarray:
    return np.array(image.convert("L"))


# ---- parsing and the two-reads rule --------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "value"),
    [
        ("7'-3\"", "feet", Decimal(87)),
        ("7′ - 3″", "feet", Decimal(87)),
        ('41 1/8"', "dimension", Decimal("41.125")),
        ("1168 [46]", "mm 1168", Decimal(46)),
        ("[18]", "bracket", Decimal(18)),
        ("457", "bare", Decimal(457)),
    ],
)
def test_a_reading_is_one_dimension_by_the_parsers_every_route_uses(
    text: str, kind: str, value: Decimal
) -> None:
    assert pl._value(text) == (kind, value)


@pytest.mark.parametrize(
    "text", ["PTW-004", '+3"', '03"', '7.3"', "3'-0", '1/2" = 1\'-0"', 'TYP. 24"', ""]
)
def test_a_word_a_decimal_or_a_scale_is_never_a_reading(text: str) -> None:
    assert pl._value(text) is None


def test_a_reading_is_spelled_as_every_route_spells_it() -> None:
    assert pl._spelled("feet", Decimal(87)) == "7'-3\""
    assert pl._spelled("feet", Decimal(36)) == "3'-0\""
    assert pl._spelled("dimension", Decimal("41.125")) == '41 1/8"'
    assert pl._spelled("mm 1168", Decimal(46)) == "1168 [46]"


FOUND = ("found", 0)
SEVEN_THREE = ("feet", Decimal(87))


def test_two_engines_reading_one_value_the_found_way_confirm_it() -> None:
    values = {FOUND: set(), ("RapidOCR", 0): {SEVEN_THREE}, ("Tesseract", 0): {SEVEN_THREE}}
    assert pl._decide(values, ["RapidOCR", "Tesseract"]) == (0, SEVEN_THREE, "")


@pytest.mark.parametrize(
    ("values", "why"),
    [
        ({FOUND: set(), ("RapidOCR", 0): {SEVEN_THREE}}, "only one engine read it"),
        (
            {
                FOUND: set(),
                ("RapidOCR", 0): {SEVEN_THREE},
                ("Tesseract", 0): {SEVEN_THREE, ("feet", Decimal(81))},
            },
            "the readings disagree",
        ),
        (
            {
                FOUND: set(),
                ("RapidOCR", 0): {SEVEN_THREE},
                ("Tesseract", 0): {SEVEN_THREE},
                ("Tesseract", 180): {("feet", Decimal(78))},
            },
            "it reads as a dimension both ways round",
        ),
        (
            {FOUND: set(), ("RapidOCR", 180): {SEVEN_THREE}, ("Tesseract", 180): {SEVEN_THREE}},
            "it reads only turned the other way round",
        ),
        ({FOUND: set()}, "no engine read one dimension there"),
    ],
)
def test_anything_less_than_two_agreeing_reads_is_not_confirmed(
    values: dict[tuple[str, int], set[tuple[str, Decimal]]], why: str
) -> None:
    assert pl._decide(values, ["RapidOCR", "Tesseract"])[2] == why


# ---- one picture, scripted engines ---------------------------------------------------------------


def test_an_upright_label_is_confirmed_with_its_box_in_page_points() -> None:
    found, whole = pl.read_picture(
        _grey(_marked(0)), (60.0, 60.0), 2.0, _engines("7'-3\"", "7'-3\""), _Detector("7'-3\"")
    )

    assert whole
    (label,) = found
    assert (label.confirmed, label.text, label.inches, label.orientation) == (
        True,
        "7'-3\"",
        Decimal(87),
        "h",
    )
    # The bar is at pixels (300..380, 150..174) of a picture at 2 pixels a point, from (60, 60).
    assert label.box == pytest.approx((210.0, 135.0, 250.0, 147.0), abs=0.6)


def test_a_sideways_label_is_found_turned_and_read_up_the_page() -> None:
    found, _whole = pl.read_picture(
        _grey(_marked(90)), (60.0, 60.0), 2.0, _engines('41 1/8"', '41 1/8"'), _Detector('41 1/8"')
    )

    (label,) = found
    assert (label.confirmed, label.turn, label.orientation, label.along) == (
        True,
        90,
        "v",
        (0.0, -1.0),
    )
    assert label.box == pytest.approx((210.0, 135.0, 222.0, 175.0), abs=0.6)


def test_two_engines_reading_different_values_never_confirm() -> None:
    found, _whole = pl.read_picture(
        _grey(_marked(0)), (60.0, 60.0), 2.0, _engines("7'-3\"", "1'-3\""), _Detector("7'-3\"")
    )

    (label,) = found
    assert not label.confirmed
    assert label.why == "the readings disagree"


def test_one_engine_alone_never_confirms() -> None:
    found, _whole = pl.read_picture(
        _grey(_marked(0)), (60.0, 60.0), 2.0, _engines("7'-3\"", "-"), _Detector("7'-3")
    )

    (label,) = found
    assert (label.confirmed, label.why) == (False, "only one engine read it")


# ---- pages ---------------------------------------------------------------------------------------


def test_a_pasted_picture_gives_labels_after_the_page_text(tmp_path: Path) -> None:
    pdf = _pdf(
        tmp_path / "pasted.pdf",
        _marked(0),
        text='BT /F1 8 Tf 300 350 Td (24") Tj ET 280 345 m 340 345 l S',
    )
    readers = _engines("7'-3\"", "7'-3\"")

    found = dl.page_labels(
        pdf, 1, HALVES, picture_budget_s=60, readers=readers, detector=_Detector("7'-3\"")
    )

    pictures = [label for label in found.labels if label.get("origin") == "picture"]
    assert [(label["text"], label["inches"], label["route"]) for label in pictures] == [
        ("7'-3\"", "87", "drawn")
    ]
    label = pictures[0]
    assert (label["side"], label["orientation"], label["dim_line"]) == ("architect", "h", None)
    # Ids: the page's printed labels first, the picture's after them.
    assert [lab["id"] for lab in found.labels] == [f"D{n}" for n in range(1, len(found.labels) + 1)]
    assert found.labels[-1]["origin"] == "picture"
    assert found.pictures is not None
    assert found.pictures["status"] == "done"
    assert found.pictures["boxes"] == [list(PICTURE_BOX)]
    assert found.pictures["unconfirmed"] == []


def test_an_unconfirmed_number_is_listed_never_a_label(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))

    found = dl.page_labels(
        pdf,
        1,
        HALVES,
        picture_budget_s=60,
        readers=_engines("7'-3\"", "1'-3\""),
        detector=_Detector("7'-3\""),
    )

    assert not [label for label in found.labels if label.get("origin") == "picture"]
    assert found.pictures is not None
    (listed,) = found.pictures["unconfirmed"]
    assert listed["why"] == f"{pl.UNCONFIRMED} (the readings disagree)"
    assert listed["side"] == "architect"


def test_a_scanned_page_is_read_the_same_way(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "scan.pdf", _marked(90), pasted=False)

    found = dl.page_labels(
        pdf,
        1,
        HALVES,
        picture_budget_s=60,
        readers=_engines('41 1/8"', '41 1/8"'),
        detector=_Detector('41 1/8"'),
    )

    assert [(lab["text"], lab["orientation"]) for lab in found.labels] == [('41 1/8"', "v")]


def test_a_small_picture_is_not_read(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "logo.pdf", _marked(0), box=(60, 60, 120, 100))
    readers = _engines("7'-3\"", "7'-3\"")

    found = dl.page_labels(
        pdf, 1, HALVES, picture_budget_s=60, readers=readers, detector=_Detector("7'-3\"")
    )

    assert found.pictures is None
    assert readers[0].calls == 0


def test_one_engine_installed_reads_no_picture(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))
    one = [_Reader("RapidOCR", "7'-3\"")]

    found = dl.page_labels(
        pdf, 1, HALVES, picture_budget_s=60, readers=one, detector=_Detector("7'-3\"")
    )

    assert found.pictures is not None
    assert found.pictures["status"] == "no_engine"
    assert not found.labels


def test_the_picture_route_is_off_unless_asked(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))

    found = dl.page_labels(pdf, 1, HALVES)

    assert found.pictures is None
    assert found.labels == []


def test_the_pack_reads_pictures_between_the_text_and_the_drawn_ink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))
    monkeypatch.setattr(dl, "drawn_readers", lambda: _engines("7'-3\"", "7'-3\""))
    monkeypatch.setattr(pl, "default_detector", lambda: _Detector("7'-3\""))
    page_map = {"pages": [{"file": "pasted.pdf", "page": 1, "halves": HALVES}]}

    document = dl.build_dims({"pasted.pdf": pdf}, page_map, drawn_budget_s=30)

    (entry,) = document["pages"]
    assert [(lab["id"], lab["text"], lab.get("origin")) for lab in entry["labels"]] == [
        ("D1", "7'-3\"", "picture")
    ]
    assert entry["pictures"]["status"] == "done"
    # The drawn pass keeps the picture's labels.
    again = dl.drawn_labels(pdf, 1, HALVES, entry["labels"], budget_s=5, readers=[])
    assert [lab.get("origin") for lab in again.labels] == ["picture"]


# ---- what later steps see ------------------------------------------------------------------------


def test_verify_value_says_a_number_in_a_picture_is_not_confirmed(tmp_path: Path) -> None:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))
    box = {"x0": 205, "y0": 130, "x1": 255, "y1": 150}

    found = vv.verify_value(pdf, 1, box, "7'-3\"", drawn=False)

    assert (found.result, found.reason) == ("cannot_read", pl.UNCONFIRMED)


def test_a_picture_label_is_an_ocr_reading_never_printed_text() -> None:
    box = [210.0, 135.0, 250.0, 147.0]
    finding = {
        "architect": "7'-3\"",
        "arch_box": {"page": 1, "x0": 210.0, "y0": 135.0, "x1": 250.0, "y1": 147.0},
        "dims": {
            "architect": {
                "id": "D1",
                "page": 1,
                "text": "7'-3\"",
                "inches": "87",
                "route": "drawn",
                "origin": "picture",
                "side": "architect",
                "box": box,
            }
        },
    }

    checks = label_checks(finding, {})

    assert checks["verify_architect"]["route"] == "drawn"
    assert "pasted picture" in checks["verify_architect"]["reason"]
    assert "printed_architect" not in checks


def test_a_vendor_picture_label_has_its_own_ink_looked_at() -> None:
    label = {"route": "drawn", "origin": "picture", "box": [0, 0, 10, 10]}

    assert simple_verify._ink_check(label, None).outcome == "unknown"
    assert simple_verify._ink_check(label, lambda _label: (1, 0.0)).outcome == "pass"
    assert simple_verify._ink_check({"route": "drawn"}, None).outcome == "pass"


# ---- real engines on rendered labels (skipped where an engine is missing) ------------------------


def _real_engines() -> tuple[list[Any], Any]:
    readers = dl.drawn_readers()
    detector = pl.default_detector()
    if detector is None or {reader.name for reader in readers} != {"RapidOCR", "Tesseract"}:
        pytest.skip("RapidOCR and Tesseract are both needed")
    return readers, detector


def _read(tmp_path: Path, image: Image.Image) -> dl.PageLabels:
    readers, detector = _real_engines()
    pdf = _pdf(tmp_path / "real.pdf", image)
    return dl.page_labels(pdf, 1, HALVES, picture_budget_s=120, readers=readers, detector=detector)


def _confirmed(found: dl.PageLabels) -> list[tuple[str, str]]:
    return [(label["text"], label["orientation"]) for label in found.labels]


def test_real_engines_read_an_upright_feet_and_inches_label(tmp_path: Path) -> None:
    found = _read(tmp_path, _picture([("7'-3\"", (300, 150), 0)]))

    assert _confirmed(found) == [("7'-3\"", "h")]
    (label,) = found.labels
    # Its box is where the label was rendered: about (300, 150) pixels at 2 a point, from (60, 60).
    assert label["box"][0] == pytest.approx(210, abs=4)
    assert label["box"][1] == pytest.approx(135, abs=4)


def test_real_engines_read_a_sideways_fraction(tmp_path: Path) -> None:
    found = _read(tmp_path, _picture([('41 1/8"', (120, 120), 90)]))

    assert _confirmed(found) == [('41 1/8"', "v")]
    assert found.labels[0]["inches"] == "41.125"


def test_real_engines_read_millimetres_over_inches(tmp_path: Path) -> None:
    found = _read(tmp_path, _picture([("1168 [46]", (300, 150), 0)]))

    assert _confirmed(found) == [("1168 [46]", "h")]
    assert (found.labels[0]["mm"], found.labels[0]["units"]) == ("1168", "agrees")


def test_real_engines_never_confirm_a_blurry_label(tmp_path: Path) -> None:
    found = _read(tmp_path, _picture([("7'-3\"", (300, 150), 0)], blur=1.5))

    assert found.labels == []
    assert found.pictures is not None
    assert all(pl.UNCONFIRMED in item["why"] for item in found.pictures["unconfirmed"])
