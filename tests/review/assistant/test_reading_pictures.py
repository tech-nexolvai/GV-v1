"""What the readings are shown (2026-10-28, after a real upload's outlet items stayed unread). $0:
made-up labels, fake models; two tests draw a real tiny PDF.

- A label code reads up the page is shown turned, its text upright, alone, to both readings (the
  second reading used to get only the sideways text).
- A label with no dimension line in the pack gets a wider region along its direction, so what it
  measures shows.
- A long thin region adds a close-up of the label.
- At most two pictures a side; both readings see the same ones.
"""

from __future__ import annotations

import io
import json
import threading
import types
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image
from reportlab.pdfgen import canvas

from app.review.assistant import read_spots
from app.review.assistant.chat_engine import ChatEngine
from app.review.assistant.read_adjudication import SideCrops, third_read
from app.review.assistant.read_spots import (
    CONTEXT_PT,
    MIN_SIDE_PT,
    NO_LINE_ACROSS_PT,
    NO_LINE_ALONG_PT,
    PAD_PT,
    ReadPictures,
    ReadSpot,
    spots,
)
from app.review.assistant.second_read import SecondRead, question, second_read

FILE = "set.pdf"
REPLY = json.dumps({"architect": "6' - 0\"", "vendor": '73 1/2"', "sure": True})


class FakeModel:
    def __init__(self, reply: str = REPLY) -> None:
        self.reply = reply
        self.sent: list[list[dict[str, Any]]] = []

    def __call__(self, content: list[dict[str, Any]]) -> str:
        self.sent.append(content)
        return self.reply


def _label(
    id_: str, text: str, box: list[float], orientation: str | None, line: Any = None
) -> dict:
    return {
        "id": id_,
        "text": text,
        "box": box,
        "side": "architect",
        "orientation": orientation,
        "dim_line": line,
    }


def _finding(label: dict[str, Any], side: str = "architect", page: int = 3) -> dict[str, Any]:
    box = label["box"]
    key = "arch_box" if side == "architect" else "vendor_box"
    return {
        "id": "F56",
        "item": "Buffet: upper outlet height",
        "source": "architect",
        "status": "different",
        "architect": "6' - 0\"",
        "vendor": '73 1/2"',
        key: {"page": page, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3], "file": FILE},
        "dims": {side: {"id": label["id"], "box": box, "page": page, "file": FILE}},
    }


def _size(region: tuple[float, float, float, float]) -> tuple[float, float]:
    return region[2] - region[0], region[3] - region[1]


# ---- what a side shows -----------------------------------------------------------------------------


def test_a_side_shows_at_most_two_pictures_and_upright_text_first() -> None:
    sideways = SideCrops(upright=b"region", turned=b"turned", sideways=True)
    assert sideways.pictures() == [("upright", b"turned")]
    unknown = SideCrops(upright=b"region", turned=b"turned")
    assert unknown.pictures() == [("plain", b"region"), ("turned", b"turned")]
    flat = SideCrops(upright=b"region", zoom=b"close")
    assert flat.pictures() == [("plain", b"region"), ("zoom", b"close")]
    up_close = SideCrops(upright=b"region", turned=b"turned", sideways=True, zoom=b"close")
    assert up_close.pictures() == [("upright", b"turned"), ("zoom", b"close")]
    crowded = SideCrops(upright=b"region", turned=b"turned", zoom=b"close")
    assert len(crowded.pictures()) == 2 and ("zoom", b"close") not in crowded.pictures()


def test_a_vertical_label_is_shown_upright_alone_and_a_horizontal_one_as_drawn() -> None:
    up = _label("D2", "6' - 0\"", [300.0, 400.0, 308.0, 430.0], "v", [[304, 330], [304, 500]])
    spot = spots(_finding(up), lambda f, p: [up])["architect"]
    assert spot.tall and spot.upright_only
    flat = _label("D12", '9"', [300.0, 400.0, 306.0, 410.0], "h", [[280, 412], [330, 412]])
    spot = spots(_finding(flat), lambda f, p: [flat])["architect"]
    assert not spot.tall and not spot.upright_only
    # A direction code does not know: the region as drawn beside its turned copy, as before.
    odd = _label("D9", '46"', [300.0, 400.0, 306.0, 430.0], "turned", [[303, 380], [303, 450]])
    spot = spots(_finding(odd), lambda f, p: [odd])["architect"]
    assert spot.tall and not spot.upright_only and spot.zoom is None


# ---- what the region shows ---------------------------------------------------------------------------


def test_a_label_with_no_dimension_line_shows_more_along_its_direction() -> None:
    up = _label("D2", "6' - 0\"", [300.0, 400.0, 308.0, 430.0], "v")
    region = spots(_finding(up), lambda f, p: [up])["architect"].region
    assert _size(region) == (NO_LINE_ACROSS_PT, NO_LINE_ALONG_PT)
    assert ((region[0] + region[2]) / 2, (region[1] + region[3]) / 2) == (304.0, 415.0)
    flat = _label("D3", "3' - 1\"", [300.0, 400.0, 330.0, 408.0], "h")
    region = spots(_finding(flat), lambda f, p: [flat])["architect"].region
    assert _size(region) == (NO_LINE_ALONG_PT, NO_LINE_ACROSS_PT)
    # With a dimension line: at least `CONTEXT_PT` a side since 2026-10-29 (was the 96 pt
    # minimum: a short line's picture showed the number but not what it measures) ...
    lined = {**flat, "dim_line": [[290.0, 410.0], [340.0, 410.0]]}
    region = spots(_finding(lined), lambda f, p: [lined])["architect"].region
    assert _size(region) == (CONTEXT_PT, CONTEXT_PT)
    # ... and a long line still decides: both ends (155 pt from the centre) and the margin.
    long = {**flat, "dim_line": [[160.0, 410.0], [470.0, 410.0]]}
    region = spots(_finding(long), lambda f, p: [long])["architect"].region
    assert _size(region) == (2 * (155.0 + PAD_PT), CONTEXT_PT)


def test_a_long_thin_region_adds_a_close_up_of_the_label() -> None:
    """F56's vendor side: a small stacked 73 1/2" on a 253 pt dimension line (96 x 301 pt)."""
    small = _label("D21", '73 1/2"', [400.0, 300.0, 410.0, 318.0], "v", [[405, 182], [405, 435]])
    small["side"] = "vendor"
    spot = spots(_finding(small, "vendor"), lambda f, p: [small])["vendor"]
    width, height = _size(spot.region)
    # Since 2026-10-29 the region is at least `CONTEXT_PT` wide (192 x 302, no longer long and
    # thin): it is still far bigger than the close-up, which it adds.
    assert (width, height) == (CONTEXT_PT, 302.0)
    assert height >= read_spots.CLOSE_UP_RATIO * MIN_SIDE_PT
    assert spot.zoom is not None and _size(spot.zoom) == (MIN_SIDE_PT, MIN_SIDE_PT)
    assert spot.zoom[1] > spot.region[1] and spot.zoom[3] < spot.region[3]
    # A region the close-up's own size adds none (since 2026-10-29 a label's region is at least
    # `CONTEXT_PT`, twice the close-up, so a label's region always adds it: see below).
    own = (400.0, 300.0, 410.0, 318.0)
    assert read_spots._zoom(read_spots.around(own), own) is None
    square = {**small, "dim_line": [[405, 280], [405, 340]]}
    zoom = spots(_finding(square, "vendor"), lambda f, p: [square])["vendor"].zoom
    assert zoom is not None and _size(zoom) == (MIN_SIDE_PT, MIN_SIDE_PT)


# ---- both readings see the same pictures --------------------------------------------------------------


def test_the_second_reading_names_each_picture() -> None:
    model = FakeModel()
    crops = {
        "architect": [("upright", b"\x89PNG a-turned")],
        "vendor": [("upright", b"\x89PNG v-turned"), ("zoom", b"\x89PNG v-close")],
    }
    read = second_read(crops, "Buffet: upper outlet height", model, centred=True)
    assert read.vendor == '73 1/2"' and read.asked == ("architect", "vendor")
    content = model.sent[0]
    assert [block["type"] for block in content] == ["image", "image", "image", "text"]
    words = content[-1]["text"]
    assert "image 1 is the architect's drawing turned a quarter turn, so its sideways text" in words
    assert "image 2 is the vendor's shop drawing turned a quarter turn" in words
    assert "image 3 is a close-up of the same spot on the vendor's shop drawing" in words
    # Plain pictures are asked about in the same words as before.
    assert question("Top", ("architect", "vendor")) == question(
        "Top", [("architect", "plain"), ("vendor", "plain")]
    )


def test_the_third_reading_sees_one_upright_picture_of_a_sideways_label() -> None:
    model = FakeModel()
    crops = {
        "architect": SideCrops(upright=b"\x89PNG a", turned=b"\x89PNG a-t", sideways=True),
        "vendor": SideCrops(upright=b"\x89PNG v", turned=b"\x89PNG v-t", sideways=True),
    }
    third_read(crops, "Buffet: upper outlet height", model, centred=True)
    content = model.sent[0]
    assert [block["type"] for block in content] == ["image", "image", "text"]
    assert "turned a quarter turn, so its sideways text reads upright" in content[-1]["text"]


def test_the_engine_sends_the_second_reading_the_upright_copy() -> None:
    seen: list[Any] = []

    class Reader:
        @staticmethod
        def second_read(crops: Any, item: str, call: Any, **words: Any) -> SecondRead:
            seen.append(crops)
            return SecondRead(architect=None, vendor='73 1/2"', sure=True, asked=("vendor",))

        @staticmethod
        def compare_read(read: SecondRead, finding: Any) -> SecondRead:
            from app.review.assistant.second_read import compare_read

            return compare_read(read, finding)

    finding = {
        "id": "F56",
        "item": "Buffet: upper outlet height",
        "status": "different",
        "architect": "6' - 0\"",
        "vendor": '73 1/2"',
        "checks": {"second_read": "not_run"},
    }
    store = {"F56": dict(finding)}
    engine: Any = types.SimpleNamespace(
        _lock=threading.Lock(),
        finding_lock=threading.RLock(),
        cancel=threading.Event(),
        book=types.SimpleNamespace(
            live=lambda: [dict(f) for f in store.values()],
            replace=lambda f: bool(store.update({f["id"]: dict(f)}) or True),
        ),
        _reads={},
        _read_slots={},
        _read_waiting={},
        _read_share=None,
        _show_finding=lambda f: None,
        _queue_third_read=lambda f: None,
        _second_read_call=lambda content, share: "{}",
        reading_pictures=lambda f: ReadPictures(
            crops={"vendor": SideCrops(upright=b"region", turned=b"turned", sideways=True)},
            spots={"vendor": ReadSpot("vendor", None, 1, (0.0, 0.0, 96.0, 300.0), values=1)},
        ),
    )
    for name in ("_not_double_read", "_apply_second_read", "_second_read_one"):
        setattr(engine, name, getattr(ChatEngine, name).__get__(engine))
    engine._second_read_one(Reader, finding)
    assert seen == [{"vendor": [("upright", b"turned")]}]
    assert store["F56"]["checks"]["second_read"] == "agrees"


# ---- drawn ----------------------------------------------------------------------------------------------


def _page(folder: Path) -> Path:
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=(612, 792))
    page.setFont("Helvetica", 10)
    page.drawString(110, 792 - 90, '36"')
    page.showPage()
    page.save()
    folder.mkdir()
    pdf = pikepdf.open(io.BytesIO(out.getvalue()))
    pdf.save(str(folder / FILE))
    return folder / FILE


def _pixels(png: bytes) -> tuple[int, int]:
    size: tuple[int, int] = Image.open(io.BytesIO(png)).size
    return size


def test_a_sideways_long_label_is_drawn_as_drawn_with_an_upright_close_up(tmp_path: Path) -> None:
    """Since 2026-10-29 the region is shown as drawn (the drawing its own way up: a turned
    elevation lay on its side and hid what a height measures) and the close-up alone is turned,
    its text upright (was: the region turned, then the close-up)."""
    pdf = _page(tmp_path / "uploads")
    small = _label("D21", '73 1/2"', [100.0, 300.0, 110.0, 318.0], "v", [[105, 182], [105, 435]])
    finding = _finding(small, "architect", page=1)
    drawn = read_spots.draw(finding, labels_of=lambda f, p: [small], pdf_for=lambda _f: pdf)
    crop = drawn.crops["architect"]
    assert not crop.sideways and crop.turned is None and crop.zoom is not None
    assert crop.zoom_upright
    shown = crop.pictures()
    assert [kind for kind, _png in shown] == ["plain", "zoom_upright"]
    region_w, region_h = _pixels(crop.upright)
    assert region_h > region_w and shown[0][1] == crop.upright
    assert _pixels(crop.zoom) == (400, 400)
    # Both readings key the same pictures: the close-up is part of the reading's key.
    plain = ReadPictures(crops={"architect": SideCrops(upright=crop.upright)}, spots=drawn.spots)
    assert plain.key("ask") != drawn.key("ask")
