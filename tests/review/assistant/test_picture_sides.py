"""A pasted picture takes the side of the GV heading over it (#1835, decided 2026-10-21).

A picture pasted on a sheet (a rectangle comment holding a raster snapshot, #1825) becomes a
drawing of the page map only when a GV heading lies over it, by the heading rule pasted drawings
have; its labels (`origin: "picture"`, #1830) take that side. A picture with no heading stays
nobody's, a label inside both an architect's and a vendor's region gets no side, and every rule for
pasted drawings is unchanged. A picture label never passes a geometry check on a line. Every sheet,
heading and value here is made up in the test; no model is called.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.review.assistant import dim_labels as dl
from app.review.assistant import dim_pairs as dp
from app.review.assistant import picture_labels as pl
from app.review.assistant import plan_check
from app.review.assistant import set_pack as sp
from app.review.assistant.full_check_advocate import same_extent
from app.review.assistant.simple_check.worklist import worklist_from
from tests.review.assistant.test_dim_anchors import _kitchen, da
from tests.review.assistant.test_full_check_roster import _label as _line_label
from tests.review.assistant.test_picture_labels import (
    PICTURE_BOX,
    _Detector,
    _engines,
    _marked,
    _pdf,
)

NAME = "shop.pdf"
STAMP = [40.0, 300.0, 560.0, 780.0]  # the vendor's pasted drawing, lower half
PICTURE = [40.0, 60.0, 560.0, 260.0]  # a pasted picture, upper half


def _note(text: str, box: list[float]) -> dict[str, Any]:
    return {
        "file": NAME,
        "page": 1,
        "kind": "FreeText",
        "author": "GV",
        "text": text,
        "font_pt": 24.0,
        "box": box,
    }


def _stamp(box: list[float]) -> dict[str, Any]:
    return {
        "file": NAME,
        "page": 1,
        "kind": "Stamp",
        "author": None,
        "text": None,
        "font_pt": None,
        "box": box,
    }


def _picture(box: list[float]) -> dict[str, Any]:
    return {**_stamp(box), "kind": "Square", "picture": True}


def _entry(annotations: list[dict[str, Any]], **more: Any) -> dict[str, Any]:
    (entry,) = sp._page_entries(NAME, annotations, [""], 1, **more)
    return entry


# ---- the page map --------------------------------------------------------------------------------


def test_a_picture_under_an_architect_heading_is_the_architects_drawing() -> None:
    headings = [
        _note("ARCHITECT ELEVATION", [40, 30, 300, 55]),
        _note("SHOP DRAWING", [40, 270, 300, 295]),
    ]
    entry = _entry([*headings, _stamp(STAMP), _picture(PICTURE)])

    stamp, picture = entry["halves"]
    assert (stamp["role"], stamp.get("picture")) == ("vendor", None)
    assert picture == {
        "box": PICTURE,
        "role": "architect",
        "heading": "ARCHITECT ELEVATION",
        "round": None,
        "picture": True,
    }
    assert entry["role"] == "architect+vendor"
    assert entry["pictures"] == [PICTURE]


def test_a_picture_under_a_vendor_heading_is_the_vendors_with_its_round() -> None:
    entry = _entry(
        [
            _note("SHOP DRAWING", [40, 30, 300, 55]),
            _note("SECOND SET OF COMMENTS", [40, 262, 300, 286]),
            _picture(PICTURE),
        ]
    )

    (picture,) = entry["halves"]
    assert (picture["role"], picture["round"], picture["picture"]) == ("vendor", 2, True)


def test_a_picture_with_no_heading_over_it_is_no_drawing() -> None:
    # The only heading is over the pasted drawing; the picture sits beside it, far below.
    stamp = [40.0, 60.0, 300.0, 400.0]
    beside = [320.0, 300.0, 560.0, 500.0]
    annotations = [_note("SHOP DRAWING", [40, 30, 300, 55]), _stamp(stamp)]
    before = _entry(annotations)

    entry = _entry([*annotations, _picture(beside)])

    assert entry["halves"] == before["halves"]
    assert [half["role"] for half in entry["halves"]] == ["vendor"]
    assert entry["pictures"] == [beside]


def test_a_picture_never_changes_the_pasted_drawings() -> None:
    annotations = [
        _note("ARCHITECT ELEVATION", [40, 30, 300, 55]),
        _note("SHOP DRAWING", [40, 270, 300, 295]),
        _stamp(STAMP),
        _stamp([320, 60, 560, 250]),
    ]
    before = _entry(annotations)

    entry = _entry([*annotations, _picture(PICTURE)])

    assert entry["halves"][: len(before["halves"])] == before["halves"]


def test_no_picture_is_a_drawing_on_a_whole_page_drawing() -> None:
    """#1798: a page that prints dimensions by itself is one drawing; GV's pastes on it are not."""
    entry = _entry(
        [_note("ARCHITECT ELEVATION", [40, 30, 300, 55]), _picture(PICTURE)],
        upload_role="vendor",
        sizes=[(612.0, 792.0)],
        own_texts=['24" 36" 12"'],
    )

    assert [(half["role"], half.get("picture")) for half in entry["halves"]] == [("vendor", None)]


def test_a_settled_whole_page_drawing_drops_the_picture_and_counts_as_before() -> None:
    page_map: dict[str, Any] = {
        "pages": [
            {
                "file": NAME,
                "page": 1,
                "role": "architect",
                "halves": [{"box": PICTURE, "role": "architect", "picture": True}],
                "pictures": [PICTURE],
                "candidate": {
                    "box": [0, 0, 612, 792],
                    "role": "vendor",
                    "heading": None,
                    "round": None,
                    "from_upload": True,
                    "upload": "vendor",
                },
            }
        ]
    }
    # Three labels inside the picture: they count as outside the pasted drawings, as before.
    labels = [{"box": [100 + 40 * n, 100, 130 + 40 * n, 110]} for n in range(3)]
    dims = {"pages": [{"file": NAME, "page": 1, "labels": labels}]}

    _found, promoted = sp.settle_candidates(page_map, dims)

    assert promoted == [(NAME, 1)]
    assert [half["role"] for half in page_map["pages"][0]["halves"]] == ["vendor"]


# ---- the labels' sides ---------------------------------------------------------------------------


IN_PICTURE = (100.0, 100.0, 130.0, 110.0)
ARCH_PICTURE = {"box": PICTURE, "role": "architect", "picture": True}
VENDOR_PICTURE = {"box": PICTURE, "role": "vendor", "picture": True}


def test_a_picture_label_takes_its_pictures_heading() -> None:
    vendor_stamp = {"box": STAMP, "role": "vendor"}

    assert dl._side(IN_PICTURE, [vendor_stamp, ARCH_PICTURE], [PICTURE]) == "architect"
    assert dl._side(IN_PICTURE, [VENDOR_PICTURE], [PICTURE]) == "vendor"


def test_a_label_in_a_picture_with_no_heading_has_no_side() -> None:
    # The picture lies on the architect's pasted drawing, but nobody headed the picture.
    around = {"box": [0, 0, 600, 800], "role": "architect"}

    assert dl._side(IN_PICTURE, [around], [PICTURE]) == "unknown"
    # Without the pasted pictures (a label printed on the page), the drawing's rule as before.
    assert dl._side(IN_PICTURE, [around]) == "architect"


def test_a_label_in_both_an_architects_and_a_vendors_region_has_no_side() -> None:
    vendor_around = {"box": [0, 0, 600, 800], "role": "vendor"}
    same_around = {"box": [0, 0, 600, 800], "role": "architect"}

    assert dl._side(IN_PICTURE, [vendor_around, ARCH_PICTURE], [PICTURE]) == "unknown"
    assert dl._side(IN_PICTURE, [same_around, ARCH_PICTURE], [PICTURE]) == "architect"
    # Two pictures over each other headed apart: no side.
    other = [50.0, 70.0, 300.0, 200.0]
    vendor_other = {"box": other, "role": "vendor", "picture": True}
    assert dl._side(IN_PICTURE, [ARCH_PICTURE, vendor_other], [PICTURE, other]) == "unknown"


def test_a_printed_label_never_takes_a_pictures_side() -> None:
    vendor_stamp = {"box": STAMP, "role": "vendor"}
    near_both = (100.0, 270.0, 130.0, 280.0)  # 10 pt under the picture, 20 pt over the stamp

    assert dl._side(near_both, [vendor_stamp, ARCH_PICTURE]) == "vendor"
    assert dl._side(IN_PICTURE, [ARCH_PICTURE]) == "unknown"


def _read_pack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, halves: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    pdf = _pdf(tmp_path / "pasted.pdf", _marked(0))
    monkeypatch.setattr(dl, "drawn_readers", lambda: _engines("7'-3\"", "7'-3\""))
    monkeypatch.setattr(pl, "default_detector", lambda: _Detector("7'-3\""))
    page_map = {
        "pages": [
            {"file": "pasted.pdf", "page": 1, "halves": halves, "pictures": [list(PICTURE_BOX)]}
        ]
    }
    (entry,) = dl.build_dims({"pasted.pdf": pdf}, page_map)["pages"]
    return [label for label in entry["labels"] if label.get("origin") == "picture"]


def test_the_pack_gives_a_headed_pictures_labels_its_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headed = {"box": list(PICTURE_BOX), "role": "architect", "picture": True}

    (label,) = _read_pack(tmp_path, monkeypatch, [headed])

    assert (label["text"], label["side"], label["dim_line"]) == ("7'-3\"", "architect", None)


def test_the_pack_leaves_a_headless_pictures_labels_with_no_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A vendor's pasted drawing round the picture does not make the picture the vendor's.
    around = {"box": [40, 40, 530, 290], "role": "vendor"}

    (label,) = _read_pack(tmp_path, monkeypatch, [around])

    assert label["side"] == "unknown"


def test_the_pack_gives_no_side_where_a_picture_and_a_drawing_disagree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    around = {"box": [40, 40, 530, 290], "role": "vendor"}
    headed = {"box": list(PICTURE_BOX), "role": "architect", "picture": True}

    (label,) = _read_pack(tmp_path, monkeypatch, [around, headed])

    assert label["side"] == "unknown"


# ---- never a geometry pass on a line -------------------------------------------------------------


def test_a_picture_label_never_passes_the_drawn_extent_check() -> None:
    vendor = _line_label("D1", "vendor", "46", (300, 392))
    architect = _line_label("D2", "architect", "37", (100, 174))
    anchors_v = [
        _line_label("D3", "vendor", "24", (380, 428)),
        _line_label("D4", "vendor", "12", (440, 464)),
    ]
    anchors_a = [
        _line_label("D5", "architect", "24", (180, 228)),
        _line_label("D6", "architect", "12", (240, 264)),
    ]
    assert same_extent(vendor, architect, [vendor, *anchors_v], [architect, *anchors_a])[0] == (
        "same_extent"
    )

    # Even were a line ever given to a picture label, it proves nothing.
    pictured = {**architect, "origin": "picture"}
    state, _why = same_extent(vendor, pictured, [vendor, *anchors_v], [pictured, *anchors_a])
    assert state == "cannot_tell"


def test_a_picture_label_never_pairs_on_anchors_or_position() -> None:
    arch, _vendor = _kitchen()
    pictured = [{**label, "origin": "picture"} for label in arch.labels]

    assert da.drawing("architect", pictured, arch.segments, tuple(arch.box)) is None  # type: ignore[arg-type]
    assert dp._along({**arch.labels[0], "dim_line": [[0, 0], [10, 0]], "origin": "picture"}) is None


def test_anchor_pairing_never_takes_a_picture_drawing() -> None:
    arch, vendor = _kitchen()
    dims = {
        "pages": [
            {"file": NAME, "page": 1, "labels": arch.labels},
            {"file": NAME, "page": 2, "labels": vendor.labels},
        ]
    }

    def page_map(picture: bool) -> dict[str, Any]:
        architect = {"box": arch.box, "role": "architect", **({"picture": True} if picture else {})}
        return {
            "pages": [
                {"file": NAME, "page": 1, "area": "A", "halves": [architect]},
                {"file": NAME, "page": 2, "area": "A", "halves": [{"box": vendor.box, "role": "vendor"}]},
            ]
        }  # fmt: skip

    segments = {1: arch.segments, 2: vendor.segments}
    paired = da.set_anchor_pairs(dims, page_map(False), lambda _f, page: segments[page])
    assert paired[(NAME, 2)]["pairs"]
    assert da.set_anchor_pairs(dims, page_map(True), lambda _f, page: segments[page]) == {}


# ---- the work list and the plan check ------------------------------------------------------------


def _labels() -> list[dict[str, Any]]:
    return [
        # read in the architect's headed picture
        {"id": "D1", "text": "7'-3\"", "inches": "87", "box": [100, 100, 130, 110],
         "side": "architect", "orientation": "h", "route": "drawn", "origin": "picture"},
        # printed on the vendor's pasted drawing, close to the picture
        {"id": "D2", "text": '46"', "inches": "46", "box": [100, 302, 130, 312],
         "side": "vendor", "orientation": "h", "route": "text"},
        # printed just under the picture, given to the nearest pasted drawing (none in reach)
        {"id": "D3", "text": '12"', "inches": "12", "box": [100, 262, 130, 272],
         "side": "architect", "orientation": "h", "route": "text"},
    ]  # fmt: skip


def test_a_picture_drawing_holds_only_the_labels_read_inside_it() -> None:
    page_map = {
        "files": [NAME],
        "pages": [
            {
                "file": NAME,
                "page": 1,
                "area": "1. Area",
                "role": "architect+vendor",
                "halves": [
                    {"box": STAMP, "role": "vendor", "heading": "SHOP DRAWING", "round": 1},
                    {**ARCH_PICTURE, "heading": "ARCHITECT ELEVATION", "round": None},
                ],
                "pictures": [PICTURE],
            }
        ],
    }
    dims = {"pages": [{"file": NAME, "page": 1, "labels": _labels()}]}

    work = worklist_from(page_map, dims=dims, main_file=NAME)

    (item,) = work.items
    drawings = [item.vendor, *item.architect]
    held = {d.ref: [label.ref for label in d.labels] for d in drawings}
    assert held == {"p1.v1": ["p1.D2"], "p1.a1": ["p1.D1"]}


def test_a_headed_picture_never_makes_code_assume_a_drawings_role() -> None:
    page_map = {
        "files": [NAME],
        "pages": [
            {
                "file": NAME,
                "page": 1,
                "area": "1. Area",
                "role": "architect",
                "halves": [{"box": STAMP, "role": "general", "heading": None, "round": None}],
            }
        ],
    }
    before = [d.id for d in plan_check.doubts(page_map, NAME, limit=None)]
    page_map["pages"][0]["halves"].append({**ARCH_PICTURE, "heading": "ARCHITECT ELEVATION"})

    after = [d.id for d in plan_check.doubts(page_map, NAME, limit=None)]

    assert not any(d.startswith("role:") for d in after)
    assert [d for d in after if d.startswith("role:")] == [
        d for d in before if d.startswith("role:")
    ]
