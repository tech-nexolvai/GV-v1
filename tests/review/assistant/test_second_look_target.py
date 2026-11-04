"""The blind second look is asked about the exact number its finding names (2026-11-04). $0: made-up
sheets read by the real label and line-work readers, and made-up findings; nothing calls a model.

The contract, for every kind of finding a second or third reading reads (`read_spots.spots`):

- the label a side's picture is centred on prints that side's value (`finding[side]`, the value
  `second_read.compare_read` compares the reading with), and the picture is centred on it;
- the string's overall is asked only when the finding names the overall; a finding that names one
  of its parts (`wrong_part`: the drawing's scale names the wrong number) is asked that part;
- the question never carries the value (the readers stay blind).

A real upload broke it: the finding's vendor value was the wrong part, the readers were asked for
the string's overall, read the overall printed beside the part, and code's comparison of that
overall with the part held a red code had proved. Made-up values only.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from app.review.assistant import second_read
from app.review.assistant import vendor_consistency as vc
from app.review.assistant.finding_checks import to_inches
from app.review.assistant.read_adjudication import SideCrops, adjudicate, third_read
from app.review.assistant.read_spots import ReadPictures, spots
from app.review.assistant.second_read import SecondRead, compare_read
from tests.review.assistant.test_vendor_consistency import (
    _pack,
    _sheet_twice,
    _sheet_unsized,
    _sheet_wrong,
    _sheet_wrong_overall,
)

type LabelsOf = Callable[[str, int], Sequence[Mapping[str, Any]] | None]


def _centre(box: Sequence[float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def _question(finding: Mapping[str, Any], side: str, labels_of: LabelsOf) -> str:
    spot = spots(finding, labels_of)[side]
    return second_read.question(
        str(finding.get("item") or ""),
        [(side, "plain")],
        centred=spot.centred,
        overall=spot.overall,
        part=spot.part,
    )


def assert_reads_the_named_number(finding: Mapping[str, Any], labels_of: LabelsOf) -> None:
    """The contract (see the module) for one finding, every side a reading looks at."""
    found = spots(finding, labels_of)
    assert found, finding.get("id")
    for side, spot in found.items():
        claimed = finding.get(side)
        assert isinstance(claimed, str) and to_inches(claimed) is not None
        if spot.label is not None:
            page = labels_of(str(spot.file), spot.page) or []
            (label,) = [one for one in page if str(one.get("id")) == spot.label]
            # The picture is centred on a label that prints the value the reading is compared with.
            assert to_inches(label["text"]) == to_inches(claimed), (side, label["text"], claimed)
            (cx, cy), (lx, ly) = _centre(spot.region), _centre(label["box"])
            assert abs(cx - lx) < 1e-6 and abs(cy - ly) < 1e-6
            assert spot.centred
        if spot.overall:
            # The overall is asked only when the finding names it.
            assert to_inches(claimed) == to_inches(finding.get("printed_total") or claimed)
            assert not spot.part
        if spot.part:
            assert finding.get("source") == "vendor_arithmetic" and side == "vendor"
            assert to_inches(claimed) != to_inches(finding.get("printed_total"))
        # Blind: the question never names the value (nor the should-be).
        words = _question(finding, side, labels_of)
        for value in (finding.get("vendor"), finding.get("architect")):
            if isinstance(value, str) and value.strip():
                assert value not in words, value


def _code_findings(tmp_path: Path, sheet: bytes) -> tuple[list[dict[str, Any]], LabelsOf]:
    """Code's own vendor consistency findings on a made-up sheet, with the sheet's labels."""
    dims, page_map, uploads = _pack(tmp_path, [sheet])
    result = vc.check_set(dims, page_map, segments=vc.line_work(uploads))
    pages = {(p["file"], p["page"]): p["labels"] for p in dims["pages"]}

    def labels_of(file: str, page: int) -> Sequence[Mapping[str, Any]] | None:
        return pages.get((file, page))

    return [{**f, "id": f"F{n}"} for n, f in enumerate(result.findings, 1)], labels_of


# ---- code's own findings, as made ------------------------------------------------------------------


def test_a_string_whose_drawing_names_a_part_is_read_at_that_part(tmp_path: Path) -> None:
    (finding,), labels_of = _code_findings(tmp_path, _sheet_wrong())
    assert finding["wrong_part"]["text"] == finding["vendor"] == '41"'  # the producer's claim
    assert finding["printed_total"] == '60"'
    assert_reads_the_named_number(finding, labels_of)
    spot = spots(finding, labels_of)["vendor"]
    assert spot.part and not spot.overall and spot.label == finding["dims"]["vendor"]["id"]
    words = _question(finding, "vendor", labels_of)
    assert second_read.PART_WORDS in words and second_read.CENTRE_WORDS in words
    assert second_read.OVERALL_WORDS not in words and "60" not in words


def test_a_string_whose_overall_is_wrong_is_read_at_its_overall(tmp_path: Path) -> None:
    (finding,), labels_of = _code_findings(tmp_path, _sheet_wrong_overall())
    assert finding["vendor"] == finding["printed_total"]
    assert_reads_the_named_number(finding, labels_of)
    spot = spots(finding, labels_of)["vendor"]
    assert spot.overall and not spot.part and spot.label is not None


def test_an_overall_past_unsized_ends_is_read_at_its_overall(tmp_path: Path) -> None:
    (finding,), labels_of = _code_findings(tmp_path, _sheet_unsized())
    assert_reads_the_named_number(finding, labels_of)
    assert spots(finding, labels_of)["vendor"].overall


def test_a_span_printed_twice_is_read_at_each_print(tmp_path: Path) -> None:
    findings, labels_of = _code_findings(tmp_path, _sheet_twice())
    assert findings
    for finding in findings:
        assert_reads_the_named_number(finding, labels_of)


# ---- findings other makers write (made-up labels) ---------------------------------------------------


def _label(id_: str, text: str, box: list[float], line: list[list[float]]) -> dict[str, Any]:
    return {"id": id_, "text": text, "box": box, "dim_line": line, "orientation": "h"}


#: A made-up string on page 2: 2" + 30 1/2" + 2" printed 33" overall (the middle part is wrong),
#: and a 33" overall's line under it; an architect's 24" on page 3.
OVERALL = _label("D4", '33"', [240.0, 150.0, 256.0, 160.0], [[100.0, 162.0], [400.0, 162.0]])
PARTS = [
    _label("D1", '2"', [102.0, 130.0, 114.0, 140.0], [[100.0, 142.0], [118.0, 142.0]]),
    _label("D2", '30 1/2"', [240.0, 130.0, 262.0, 140.0], [[118.0, 142.0], [382.0, 142.0]]),
    _label("D3", '2"', [386.0, 130.0, 398.0, 140.0], [[382.0, 142.0], [400.0, 142.0]]),
]
ARCH = _label("A1", '24"', [300.0, 400.0, 316.0, 410.0], [[250.0, 412.0], [366.0, 412.0]])
VENDOR = _label("V1", '25"', [300.0, 400.0, 316.0, 410.0], [[250.0, 412.0], [366.0, 412.0]])


def _labels_of(file: str, page: int) -> list[dict[str, Any]]:
    if (file, page) == ("vendor.pdf", 2):
        return [OVERALL, *PARTS, VENDOR]
    return [ARCH] if (file, page) == ("arch.pdf", 3) else []


def _box(label: Mapping[str, Any], file: str, page: int) -> dict[str, Any]:
    x0, y0, x1, y1 = label["box"]
    return {"page": page, "x0": x0, "y0": y0, "x1": x1, "y1": y1, "file": file}


def _dim(label: Mapping[str, Any], file: str, page: int) -> dict[str, Any]:
    return {
        "id": label["id"],
        "text": label["text"],
        "box": label["box"],
        "page": page,
        "file": file,
    }


def _comparison(**changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "item": "Base cabinet width",
        "source": "architect",
        "status": "different",
        "architect": '24"',
        "vendor": '25"',
        "arch_box": _box(ARCH, "arch.pdf", 3),
        "vendor_box": _box(VENDOR, "vendor.pdf", 2),
        "dims": {"architect": _dim(ARCH, "arch.pdf", 3), "vendor": _dim(VENDOR, "vendor.pdf", 2)},
    }
    finding.update(changes)
    return finding


def _reviewer_string(**changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F2",
        "item": "Elevation: width string",
        "source": "vendor_arithmetic",
        "created_by": "page_reviewer",
        "status": "different",
        "architect": '34 1/2"',
        "vendor": '33"',
        "printed_total": '33"',
        "parts": ['2"', '30 1/2"', '2"'],
        "vendor_box": {"page": 2, "x0": 98.0, "y0": 126.0, "x1": 402.0, "y1": 164.0}
        | {"file": "vendor.pdf"},
        "dims": {"vendor": _dim(OVERALL, "vendor.pdf", 2)},
    }
    finding.update(changes)
    return finding


def test_every_kind_of_finding_a_reading_reads_holds_the_contract() -> None:
    part = _reviewer_string(
        vendor='30 1/2"',
        architect='29"',
        vendor_box=_box(PARTS[1], "vendor.pdf", 2),
        dims={"vendor": _dim(PARTS[1], "vendor.pdf", 2)},
        wrong_part={"text": '30 1/2"', "should_be": "29"},
    )
    kinds = [
        _comparison(),
        _reviewer_string(),
        _reviewer_string(dims=None),  # no label named: the whole string's box, its overall asked
        part,
        {**part, "dims": None},  # no label named: the part's own box, the part asked
    ]
    for finding in kinds:
        assert_reads_the_named_number(finding, _labels_of)
    assert spots(part, _labels_of)["vendor"].label == "D2"
    unnamed = spots({**part, "dims": None}, _labels_of)["vendor"]
    assert unnamed.part and not unnamed.overall and unnamed.label is None and unnamed.centred


def test_a_label_that_prints_another_value_is_never_the_centre() -> None:
    # A finding whose named label prints another number than the one it claims: the label is not
    # used, so no reading is told that number is the item's.
    stale = _comparison(vendor='26"')
    spot = spots(stale, _labels_of)["vendor"]
    assert spot.label is None
    assert_reads_the_named_number(stale, _labels_of)
    # A string whose part label is named for its overall: neither the overall nor the part.
    crossed = _reviewer_string(dims={"vendor": _dim(PARTS[1], "vendor.pdf", 2)})
    spot = spots(crossed, _labels_of)["vendor"]
    assert spot.label is None and spot.overall and not spot.part


# ---- the readings' words and code's comparison ------------------------------------------------------


class _Model:
    def __init__(self, vendor: str) -> None:
        self.vendor = vendor
        self.sent: list[str] = []

    def __call__(self, content: list[dict[str, Any]]) -> str:
        self.sent.append(content[-1]["text"])
        return json.dumps({"architect": None, "vendor": self.vendor, "sure": True})


def test_both_readings_ask_for_the_part_and_only_its_reading_agrees() -> None:
    part = _reviewer_string(
        vendor='30 1/2"',
        architect='29"',
        vendor_box=_box(PARTS[1], "vendor.pdf", 2),
        dims={"vendor": _dim(PARTS[1], "vendor.pdf", 2)},
    )
    drawn = ReadPictures(
        crops={"vendor": SideCrops(upright=b"\x89PNG")}, spots=spots(part, _labels_of)
    )
    assert drawn.part and not drawn.overall and drawn.centred
    model = _Model('30 1/2"')
    read = second_read.second_read(
        {"vendor": b"\x89PNG"},
        part["item"],
        model,
        centred=drawn.centred,
        overall=drawn.overall,
        part=drawn.part,
    )
    assert compare_read(read, part).result == "agrees"
    third_read(drawn.crops, part["item"], model, centred=True, part=True)
    for words in model.sent:
        assert second_read.PART_WORDS in words and second_read.OVERALL_WORDS not in words
        assert "30 1/2" not in words and "29" not in words
    # A reading of the overall printed beside the part never agrees: it is held, never red.
    overall = SecondRead(architect=None, vendor='33"', sure=True, asked=("vendor",))
    assert compare_read(overall, part).result == "disagrees"
    held = adjudicate(
        {**part, "checks": {"second_read": "disagrees"}, "second_read_values": {"vendor": '33"'}},
        drawn.crops,
        _Model('33"'),
        model="sonnet",
        second_model="haiku",
        part=True,
    )
    assert held.outcome == "disagrees"
