"""`read_number` (#1746): a number nobody could read, read by independent routes, $0.

Synthetic CAD stroke lettering (`stroke_text`) on tiny PDFs, the tiny `StrokeReader` as the two
OCR engines, made-up values only (no client data). No model call.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import read_number as rn
from app.review.assistant.classification import classify
from app.review.assistant.read_number import Reading, Request, Route, decide
from app.review.assistant.simple_check import verify as v
from app.review.assistant.view_scales import ViewScale
from tests.review.assistant.stroke_text import StrokeReader, stroke_ops
from tests.review.assistant.test_dim_labels import _dimension, _line, _pdf
from tests.review.assistant.test_simple_verify import (
    ROUND_2,
    _one,
    _pack,
    _pair,
    _reader,
    _stored,
    _unlabelled,
)

FILE = "set.pdf"
#: The page's vendor drawing (shown points; the synthetic page is 600 x 400).
HALVES: list[dict[str, Any]] = [
    {"box": [0, 0, 250, 400], "role": "architect", "round": None},
    {"box": [250, 0, 600, 400], "role": "vendor", "round": 1},
]
#: Half an inch per drawn point on the vendor's drawing.
SCALES = [ViewScale(1, "vendor", 0.5, (), 0.0, (250.0, 0.0, 600.0, 400.0), None, None)]
#: Where the pen-stroke 46" sits (shown points).
SPOT = (326.0, 334.0, 350.0, 350.0)


def _engines() -> list[Any]:
    return [StrokeReader("RapidOCR"), StrokeReader("Tesseract")]


def _dashes(x0: float, x1: float, y: float, dash: float = 1.6, gap: float = 1.4) -> bytes:
    """A dashed line (a door swing's dashes): each dash its own straight path."""
    out = b""
    x = x0
    while x < x1:
        out += _line(x, y, x + dash, y)
        x += dash + gap
    return out


def _page(tmp_path: Path, body: bytes) -> Path:
    path = tmp_path / FILE
    path.write_bytes(_pdf([body]))
    return path


def _forty_six(tmp_path: Path, crossed: bool = True) -> Path:
    # A 92-point dimension (46" at half an inch a point), its value drawn as pen strokes, and a
    # dashed line running straight through the 6.
    body = _dimension(300, 392, 50) + stroke_ops('46"', 330, 53, height=9)
    if crossed:
        body += _dashes(305, 385, 57.5)
    return _page(tmp_path, body)


def _read(pdf: Path, **given: Any) -> Reading:
    labels = given.pop("labels", [])
    request = Request(FILE, 1, given.pop("side", "vendor"), **given)
    return rn.read_number(
        request, pdf, labels=labels, halves=HALVES, scales=SCALES, engines=_engines()
    )


# ---- the agreement rule ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "value", "code"),
    [
        ({"text": Decimal(16)}, Decimal(16), "text"),
        ({"ocr": Decimal(46), "shape": Decimal(46)}, Decimal(46), "ocr_shape"),
        # two readers that agree are not blocked by a ruler on another dimension's line
        ({"ocr": Decimal(2), "shape": Decimal(2), "ruler": 16.7}, Decimal(2), "ocr_shape"),
        ({"ocr": Decimal(46), "ruler": 45.99}, Decimal(46), "ocr_ruler"),
        ({"shape": Decimal(46), "ruler": 46.1}, Decimal(46), "shape_ruler"),
        ({"ocr": Decimal(46), "shape": Decimal(64)}, None, "readers_disagree"),
        # millimetres read as inches: the ruler blocks the lone reader
        ({"ocr": Decimal(52), "ruler": 1.99}, None, "ruler_blocks"),
        ({"ocr": Decimal(46)}, None, "one_route"),
        ({"ocr": Decimal(46), "ruler": 47.0}, None, "one_route"),  # between seconding and blocking
        ({"ruler": 46.0}, None, "no_reader"),  # the ruler never supplies a value
        ({}, None, "no_reader"),
    ],
)
def test_the_agreement_rule(given: dict[str, Any], value: Decimal | None, code: str) -> None:
    found = decide(**given)
    assert found.value == value and found.code == code


def test_two_readers_and_the_ruler_all_name_the_ruler() -> None:
    assert decide(ocr=Decimal(46), shape=Decimal(46), ruler=45.99).agreed == (
        "ocr",
        "shape",
        "ruler",
    )


# ---- the ink at a spot ----------------------------------------------------------------------------


def test_a_pen_stroke_number_crossed_by_a_dashed_line_is_read_by_two_routes(
    tmp_path: Path,
) -> None:
    found = _read(_forty_six(tmp_path), box=SPOT)
    assert found.outcome == "read" and found.inches == Decimal(46), found
    assert len(found.agreed) >= 2 and "ocr" in found.agreed
    ruler = next(r for r in found.routes if r.name == "ruler")
    assert ruler.value is not None and abs(float(ruler.value) - 46) <= 0.25
    # the same with the claimed value checked
    claimed = _read(_forty_six(tmp_path), box=SPOT, claim='46"')
    assert claimed.matches('46"') and claimed.as_verify('46"')["result"] == "match"
    assert not claimed.matches('64"') and claimed.as_verify('64"')["result"] == "mismatch"


def test_the_dashed_line_is_stripped_before_the_number_is_grouped(tmp_path: Path) -> None:
    pdf = _forty_six(tmp_path)
    page = rn._PAGES.get(pdf, 1).page
    near = rn._near(page, SPOT, 26)
    stripped = rn.strip_line_work(near)
    assert len(near) - len(stripped) >= 20  # every dash, none of the number's strokes
    label = rn.ink_label(page, SPOT)
    assert label is not None and label.box[2] - label.box[0] < 20


def test_a_number_cut_off_at_its_drawings_edge_stays_unread(tmp_path: Path) -> None:
    # A number written up the page whose strokes stop on the drawing's clipped left edge.
    pdf = _page(tmp_path, stroke_ops("96", 259, 120, height=9, turn=True))
    found = _read(pdf, box=(248.0, 270.0, 262.0, 284.0))
    assert found.outcome == "unread" and found.code == "cut_off"
    assert "cut off in the file" in found.reason


def test_no_number_at_the_spot_says_so(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), box=(500.0, 100.0, 510.0, 110.0))
    assert found.outcome == "unread" and found.code == "no_ink"


# ---- finding the spot -------------------------------------------------------------------------------


def _labels() -> list[dict[str, Any]]:
    def label(lid: str, text: str, inches: str, box: list[float], **more: Any) -> dict[str, Any]:
        return {
            "id": lid,
            "text": text,
            "inches": inches,
            "box": box,
            "side": "vendor",
            "route": "text",
            "mm": None,
            "units": None,
            "dim_line": None,
            "span_pt": None,
            "page": 1,
            "file": FILE,
            **more,
        }

    return [
        label("D1", "406 [16]", "16", [400, 100, 420, 110], mm="406", units="agrees"),
        label("D2", '20"', "20", [400, 150, 410, 160]),
        label("D3", '20"', "20", [450, 150, 460, 160]),
        label("D4", '22"', "22", [400, 200, 410, 210], units="disagrees"),
        label(
            "D5",
            '46"',
            "46",
            list(SPOT),
            route="drawn",
            dim_line=[[300, 350], [392, 350]],
            span_pt=92.0,
        ),
    ]


def test_a_typed_label_is_read_exactly_by_its_text(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), claim="406 [16]", labels=_labels())
    assert found.outcome == "read" and found.agreed == ("text",) and found.label == "D1"
    assert _read(_forty_six(tmp_path), label="D4", labels=_labels()).code == "units_disagree"


def test_several_spots_are_listed_for_the_ai_to_pick_never_guessed(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), claim='20"', labels=_labels())
    assert found.outcome == "choose" and [c["id"] for c in found.candidates] == ["D2", "D3"]
    picked = _read(_forty_six(tmp_path), label="D3", labels=_labels())
    assert picked.outcome == "read" and picked.label == "D3"


def test_a_value_with_no_spot_says_exactly_that(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), claim='52"', labels=_labels())
    assert found.outcome == "unread" and found.reason == "the value has no spot on the drawing"


def test_a_drawn_label_the_pack_read_is_checked_by_its_shapes_and_ruler(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), label="D5", labels=_labels())
    assert found.outcome == "read" and found.inches == Decimal(46) and found.label == "D5"
    assert "ocr" in found.agreed and len(found.agreed) >= 2


# ---- the set pack's cache ----------------------------------------------------------------------------


def test_readings_are_stored_in_the_set_pack_and_never_paid_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pack = tmp_path / "pack"
    pack.mkdir()
    pdf = _forty_six(tmp_path)
    (pack / "dims.json").write_text(
        json.dumps({"pages": [{"file": FILE, "page": 1, "labels": []}]})
    )
    (pack / "page_map.json").write_text(
        json.dumps({"pages": [{"file": FILE, "page": 1, "halves": HALVES}]})
    )
    calls: list[Request] = []
    real = rn.read_number

    def counted(request: Request, *args: Any, **kwargs: Any) -> Reading:
        calls.append(request)
        return real(request, *args, **kwargs)

    monkeypatch.setattr(rn, "read_number", counted)
    reader = rn.NumberReader(pack, lambda file: pdf, _engines())
    first = reader(Request(FILE, 1, box=SPOT))
    again = rn.NumberReader(pack, lambda file: pdf, _engines())(Request(FILE, 1, box=SPOT))
    assert len(calls) == 1 and again == first
    stored = json.loads((pack / rn.READINGS_FILE).read_text())
    assert stored["version"] == rn.READER_VERSION and len(stored["readings"]) == 1


# ---- the simple full check calls it by itself -----------------------------------------------------


def _number(found: Mapping[tuple[str, str | None], Reading]) -> Any:
    asked: list[Request] = []

    def read(request: Request) -> Reading:
        asked.append(request)
        return found.get((request.side, request.claim), Reading("unread", reason="nothing"))

    read.asked = asked  # type: ignore[attr-defined]
    return read


def _read_as(value: str, inches: str) -> Reading:
    return Reading(
        "read",
        value=inches,
        text=value,
        agreed=("ocr", "shape"),
        routes=(Route("ocr", inches, value), Route("shape", inches, value)),
        reason="read by ocr and shape",
        code="ocr_shape",
    )


def _verified(entry: dict[str, Any], read: Any, number: Any) -> v.Verified:
    result = {
        "work_item": ROUND_2.id,
        "pairs": [],
        "other_items": [entry],
        "unaccounted_labels": [],
    }
    return v.verify_answer(result, ROUND_2, _pack(), read=read, number=number).items[0]


def test_a_value_verify_value_could_not_read_is_read_again() -> None:
    number = _number({("vendor", '46"'): _read_as('46"', "46")})
    found = _verified(_unlabelled(), _reader({(1, "3' - 1\""): "match"}), number)
    assert found.outcome == "candidate" and found.reads["verify_vendor"]["result"] == "match"
    assert found.reads["verify_vendor"]["read_number"] == "ocr_shape"


def test_a_value_the_ai_could_not_read_is_read_by_code() -> None:
    number = _number(
        {("vendor", None): _read_as('46"', "46"), ("vendor", '46"'): _read_as('46"', "46")}
    )
    found = _verified(_unlabelled(vendor=None), _reader({(1, "3' - 1\""): "match"}), number)
    assert found.outcome == "candidate", found.reason
    assert found.finding["vendor"] == '46"'


def test_an_unread_value_keeps_its_exact_reason() -> None:
    cut = Reading(
        "unread",
        reason="the number's strokes stop on the edge of its drawing: it is cut off in the file",
        code="cut_off",
    )
    number = _number({("vendor", '46"'): cut})
    found = _verified(_unlabelled(), _reader({(1, "3' - 1\""): "match"}), number)
    assert found.outcome == "not_compared" and "cut off in the file" in found.reason
    stored = _stored(found.finding)
    stored["checks"] = {**stored["checks"], **found.reads}
    classified = classify(stored, proved=False)
    assert classified.reason == "unreadable" and "cut off in the file" in (classified.why or "")


def test_a_value_code_read_as_another_is_never_read_over() -> None:
    number = _number({("vendor", '46"'): _read_as('46"', "46")})
    read = _reader({(1, "3' - 1\""): "match", (2, '46"'): "mismatch"})
    found = _verified(_unlabelled(), read, number)
    assert found.outcome == "not_compared"
    assert number.asked == []


# ---- the risk guard: reading never proves a pairing ---------------------------------------------------


@pytest.mark.parametrize(
    "ends",
    [
        # the disputed reference-point forms: to the top of a splash against the underside of the
        # uppers; to the underside of a top against its top; floor to a header against a bulkhead
        (
            "top of splash",
            "underside of upper cabinets",
            "countertop",
            "underside of upper cabinets",
        ),
        ("floor", "underside of countertop", "floor", "top of counter"),
        ("floor", "top of header", "floor", "top of bulkhead"),
    ],
)
def test_disputed_reference_points_are_still_stopped_with_every_value_read(
    ends: tuple[str, str, str, str],
) -> None:
    # Every value read (typed labels, and a reader that reads anything as claimed): the item is
    # still stopped by the reference-point checks, never a candidate, never red.
    found = _one(_pair("p2.D1", "p1.D1", ends=ends))
    assert found.outcome == "not_compared", found
    stored = _stored(found.finding)
    assert classify(stored, proved=False).outcome == "not_compared"
    points = _one(_pair("p2.D1", "p1.D1", "different_points", ends=ends))
    assert points.outcome == "not_compared" and points.reason_code == "different_reference"


# ---- separate files: a set page is read at its file's own page (#1722) ------------------------------


def test_a_separate_files_set_page_is_read_at_its_own_page(tmp_path: Path) -> None:
    from app.review.assistant.simple_check.verify import Item, Pack, Region
    from tests.review.assistant.test_simple_pairing import ARCH, VENDOR, _separate_pack

    pack = Pack.from_folder(_separate_pack(tmp_path / "pack"), VENDOR)
    whole = (0.0, 0.0, 600.0, 850.0)
    item = Item("p1.v0", Region(VENDOR, 1, whole), (Region(ARCH, 1001, whole),))
    number = _number({})
    entry = _unlabelled(architect=None, architect_page=1001)
    result = {"work_item": item.id, "pairs": [], "other_items": [entry], "unaccounted_labels": []}
    v.verify_answer(result, item, pack, number=number)
    asked = [(r.file, r.page, r.side) for r in number.asked]
    assert (ARCH, 1, "architect") in asked and all(page < 1000 for _f, page, _s in asked)


def test_a_page_qualified_label_id_names_the_pages_label(tmp_path: Path) -> None:
    found = _read(_forty_six(tmp_path), label="p1001.D1", labels=_labels())
    assert found.outcome == "read" and found.label == "D1"
