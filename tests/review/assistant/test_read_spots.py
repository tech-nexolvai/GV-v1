"""Where a second or third reading looks, and what it is asked (the reading fix, 2026-10-28). $0:
made-up findings and labels; one test draws a real tiny PDF.

- A vendor string is read at its printed overall's own label, and asked for the overall.
- The picture is centred on the item's label, wide enough for both ends of its dimension line,
  never smaller than `MIN_SIDE_PT` a side; "the value at the centre" is said only when the box
  read holds one value.
- The picture is drawn without GV's markup (the evidence crop the reviewer sees keeps it).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pikepdf
from PIL import Image
from reportlab.pdfgen import canvas

from app.review.assistant import read_spots, second_read
from app.review.assistant.chat_findings import _default_crop
from app.review.assistant.read_adjudication import SideCrops, adjudicate, question, third_read
from app.review.assistant.read_spots import MIN_SIDE_PT, ReadPictures, spots

FILE = "set.pdf"


def _label(id_: str, text: str, box: list[float], line: list[list[float]] | None = None) -> dict:
    label: dict[str, Any] = {"id": id_, "text": text, "box": box, "side": "vendor"}
    if line is not None:
        label["dim_line"] = line
    return label


#: A made-up vendor string: four parts on one row, the overall on the row above.
OVERALL = _label("D6", '48"', [200.0, 100.0, 216.0, 110.0], [[100.0, 112.0], [400.0, 112.0]])
PARTS = [
    _label("D7", '12"', [140.0, 120.0, 154.0, 130.0]),
    _label("D8", '12"', [210.0, 120.0, 224.0, 130.0]),
    _label("D9", '12"', [280.0, 120.0, 294.0, 130.0]),
    _label("D10", '12 1/8"', [350.0, 120.0, 370.0, 130.0]),
]
LABELS = [OVERALL, *PARTS]


def _string(**changes: Any) -> dict[str, Any]:
    finding: dict[str, Any] = {
        "id": "F1",
        "item": "Dimension string (elevation)",
        "source": "vendor_arithmetic",
        "status": "different",
        "architect": '48 1/8"',
        "vendor": '48"',
        "printed_total": '48"',
        "parts": ['12"', '12"', '12"', '12 1/8"'],
        "vendor_box": {"page": 2, "x0": 98.0, "y0": 98.0, "x1": 402.0, "y1": 132.0, "file": FILE},
        "arch_box": {"page": 2, "x0": 98.0, "y0": 98.0, "x1": 402.0, "y1": 132.0, "file": FILE},
        "dims": {"vendor": {"id": "D6", "box": OVERALL["box"], "page": 2, "file": FILE}},
    }
    finding.update(changes)
    return finding


def _labels_of(file: str, page: int) -> list[dict[str, Any]]:
    return LABELS if (file, page) == (FILE, 2) else []


def _centre(box: tuple[float, float, float, float]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2, (box[1] + box[3]) / 2


def test_a_vendor_string_is_read_at_its_overalls_own_label_with_both_ends() -> None:
    found = spots(_string(), _labels_of)
    assert set(found) == {"vendor"}  # its "architect" is code's sum: printed nowhere
    spot = found["vendor"]
    assert spot.overall and spot.values == 1 and spot.centred
    x0, _y0, x1, _y1 = spot.region
    # Centred on the overall's label, reaching both ends of its dimension line.
    assert _centre(spot.region) == _centre(tuple(OVERALL["box"]))  # type: ignore[arg-type]
    assert x0 < 100.0 and x1 > 400.0


def test_a_whole_string_box_holds_several_values_so_no_centre_is_said() -> None:
    whole = spots(_string(dims={}), _labels_of)["vendor"]
    assert whole.overall and whole.values == 5 and not whole.centred
    # A label that is not the printed overall is never taken for it.
    part = {"vendor": {"id": "D8", "box": PARTS[1]["box"], "page": 2, "file": FILE}}
    other = spots(_string(dims=part), _labels_of)["vendor"]
    assert other.values == 5 and not other.centred


def test_a_tiny_label_gets_room_for_what_it_measures() -> None:
    small = _label("D3", '10"', [300.0, 400.0, 306.0, 408.0], [[301.0, 404.0], [320.0, 404.0]])
    finding = {
        "id": "F2",
        "item": "Spacing between the two outlets",
        "source": "architect",
        "status": "different",
        "vendor": '10"',
        "architect": '9"',
        "vendor_box": {"page": 3, "x0": 300.0, "y0": 400.0, "x1": 306.0, "y1": 408.0, "file": FILE},
        "dims": {"vendor": {"id": "D3", "box": small["box"], "page": 3, "file": FILE}},
    }
    spot = spots(finding, lambda file, page: [small])["vendor"]
    x0, y0, x1, y1 = spot.region
    assert x1 - x0 >= MIN_SIDE_PT and y1 - y0 >= MIN_SIDE_PT
    assert x1 > 320.0 and _centre(spot.region) == (303.0, 404.0)
    assert spot.centred and not spot.overall


def test_the_second_reads_question_asks_for_the_overall_and_says_the_centre_only_when_true() -> (
    None
):
    plain = second_read.question("Top depth", ("architect", "vendor"))
    assert second_read.CENTRE_WORDS not in plain and second_read.OVERALL_WORDS not in plain
    string = second_read.question("Dimension string", ("vendor",), centred=True, overall=True)
    assert second_read.OVERALL_WORDS in string and second_read.CENTRE_WORDS in string
    assert "48" not in string  # never the finding's values


def test_the_third_reads_question_says_the_centre_only_for_one_value() -> None:
    shown = [("vendor", False)]
    assert second_read.CENTRE_WORDS not in question("Dimension string", shown, centred=False)
    words = question("Dimension string", shown, centred=True, overall=True)
    assert second_read.CENTRE_WORDS in words and second_read.OVERALL_WORDS in words


class _Model:
    def __init__(self) -> None:
        self.sent: list[list[dict[str, Any]]] = []

    def __call__(self, content: list[dict[str, Any]]) -> str:
        self.sent.append(content)
        return json.dumps({"architect": None, "vendor": '48"', "sure": True})


def test_both_readings_send_the_words_their_pictures_allow() -> None:
    model = _Model()
    crops = {"vendor": SideCrops(upright=b"\x89PNG")}
    second_read.second_read({"vendor": b"\x89PNG"}, "Dimension string", model, overall=True)
    third_read(crops, "Dimension string", model, centred=False, overall=True)
    adjudicate(_string(), crops, model, model="sonnet", second_model="haiku", centred=False)
    for content in model.sent:
        words = content[-1]["text"]
        assert second_read.CENTRE_WORDS not in words
    assert second_read.OVERALL_WORDS in model.sent[0][-1]["text"]
    assert second_read.OVERALL_WORDS in model.sent[1][-1]["text"]


def test_identical_pictures_are_one_reading_key() -> None:
    a = ReadPictures(crops={"vendor": SideCrops(upright=b"one")}, spots={})
    b = ReadPictures(crops={"vendor": SideCrops(upright=b"one")}, spots={})
    c = ReadPictures(crops={"vendor": SideCrops(upright=b"two")}, spots={})
    assert a.key("ask") == b.key("ask") != c.key("ask") and a.key("ask") != a.key("other")


def _marked_upload(folder: Path) -> Path:
    """A one-page upload: the vendor's 36" and a GV comment (a red note) over it."""
    out = io.BytesIO()
    page = canvas.Canvas(out, pagesize=(612, 792))
    page.setFont("Helvetica", 10)
    page.drawString(110, 792 - 90, '36"')
    page.showPage()
    page.save()
    pdf = pikepdf.open(io.BytesIO(out.getvalue()))
    look = pikepdf.Stream(pdf, b"1 0 0 rg 0 0 60 20 re f")
    look.Type, look.Subtype, look.BBox = pikepdf.Name.XObject, pikepdf.Name.Form, [0, 0, 60, 20]
    note = pikepdf.Dictionary(
        Type=pikepdf.Name.Annot,
        Subtype=pikepdf.Name.FreeText,
        Rect=[100, 692, 160, 712],
        Contents="GV note",
        AP=pikepdf.Dictionary(N=look),
    )
    pdf.pages[0].obj.Annots = pdf.make_indirect(pikepdf.Array([pdf.make_indirect(note)]))
    folder.mkdir()
    pdf.save(str(folder / FILE))
    return folder / FILE


def _red(png: bytes) -> int:
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return sum(1 for r, g, b in image.get_flattened_data() if r > 200 and g < 80 and b < 80)


def test_the_readings_pictures_are_drawn_without_gvs_markup(tmp_path: Path) -> None:
    upload = _marked_upload(tmp_path / "uploads")
    pack = tmp_path / "pack"
    pack.mkdir()
    finding = {
        "id": "F3",
        "item": "Top width",
        "source": "architect",
        "status": "different",
        "vendor": '36"',
        "architect": '37"',
        "vendor_box": {"page": 1, "x0": 100.0, "y0": 80.0, "x1": 160.0, "y1": 100.0, "file": FILE},
    }
    evidence = _default_crop(upload, 1, (100.0, 80.0, 160.0, 100.0))
    assert evidence is not None and _red(evidence) > 1000  # the reviewer's crop keeps GV's note
    source = read_spots.source_pdf(pack, upload.parent, FILE)
    assert source.parent == pack and source.name.endswith("_drawings_only.pdf")
    drawn = read_spots.draw(finding, labels_of=None, pdf_for=lambda _file: source)
    picture = drawn.crops["vendor"].upright
    assert _red(picture) == 0
    width, height = Image.open(io.BytesIO(picture)).size
    assert min(width, height) >= MIN_SIDE_PT * 300 / 72 - 2


def test_the_engine_reads_from_the_set_packs_drawings_only_copy(tmp_path: Path) -> None:
    """The second and third readings' pictures (`ChatEngine.reading_pictures`) come from the
    set pack's drawings-only copy, GV's note gone, drawn once per finding."""
    import dataclasses

    from tests.review.assistant.chat_fakes import ScriptedProvider
    from tests.review.assistant.test_chat_engine import Harness

    harness = Harness(tmp_path, ScriptedProvider(lead=[]))
    engine = harness.engine
    upload = _marked_upload(tmp_path / "marked")
    (engine.tools.workspace.uploads / FILE).write_bytes(upload.read_bytes())
    pack = tmp_path / "pack"
    pack.mkdir()
    engine.tools.workspace = dataclasses.replace(engine.tools.workspace, set_pack=pack)
    engine.book.workspace = engine.tools.workspace
    finding = {
        "id": "F3",
        "item": "Top width",
        "source": "architect",
        "status": "different",
        "vendor": '36"',
        "architect": '37"',
        "vendor_box": {"page": 1, "x0": 100.0, "y0": 80.0, "x1": 160.0, "y1": 100.0, "file": FILE},
    }

    drawn = engine.reading_pictures(finding)

    assert set(drawn.crops) == {"vendor"} and _red(drawn.crops["vendor"].upright) == 0
    assert (pack / "set_drawings_only.pdf").is_file()
    assert engine.reading_pictures(finding) is drawn


# ---- the label's own direction decides the turned copy (2026-10-28) ------------------------------


def _one_label(label: dict[str, Any], page: int = 3) -> dict[str, Any]:
    box = label["box"]
    return {
        "id": "F10",
        "item": "Spacing between the two microwave outlets",
        "source": "architect",
        "status": "different",
        "vendor": label["text"],
        "architect": '9"',
        "vendor_box": {"page": page, "x0": box[0], "y0": box[1], "x1": box[2], "y1": box[3]}
        | {"file": FILE},
        "dims": {"vendor": {"id": label["id"], "box": box, "page": page, "file": FILE}},
    }


def test_a_horizontal_label_gets_no_turned_copy_whatever_its_box() -> None:
    """A stacked `254 [10]` (two lines) is taller than wide; code read it along the page, so its
    text is horizontal and a turned copy only shows it on its side."""
    stacked = {**_label("D37", "254 [10]", [300.0, 400.0, 312.0, 420.0]), "orientation": "h"}
    spot = spots(_one_label(stacked), lambda file, page: [stacked])["vendor"]
    assert not spot.tall
    upright = {**stacked, "box": [300.0, 400.0, 320.0, 406.0], "orientation": "v"}
    assert spots(_one_label(upright), lambda file, page: [upright])["vendor"].tall


def test_a_label_without_a_known_direction_is_judged_by_its_box() -> None:
    for orientation in (None, "turned"):
        tall = {**_label("D5", '46"', [300.0, 400.0, 306.0, 430.0]), "orientation": orientation}
        assert spots(_one_label(tall), lambda file, page, one=tall: [one])["vendor"].tall
        wide = {**tall, "box": [300.0, 400.0, 330.0, 406.0]}
        assert not spots(_one_label(wide), lambda file, page, one=wide: [one])["vendor"].tall


def test_a_horizontal_label_is_drawn_without_a_turned_copy(tmp_path: Path) -> None:
    pdf = _marked_upload(tmp_path / "uploads")
    stacked = {**_label("D37", '36"', [100.0, 80.0, 112.0, 100.0]), "orientation": "h"}
    finding = _one_label(stacked, page=1)
    pictures = read_spots.draw(
        finding, labels_of=lambda file, page: [stacked], pdf_for=lambda _file: pdf
    )
    assert pictures.crops["vendor"].turned is None
    upright = {**stacked, "orientation": "v"}
    pictures = read_spots.draw(
        _one_label(upright, page=1), labels_of=lambda file, page: [upright], pdf_for=lambda _f: pdf
    )
    # A vertical label gets its text turned upright: since 2026-10-29 in its close-up, the region
    # as drawn beside it (was: the region turned).
    kinds = [kind for kind, _png in pictures.crops["vendor"].pictures()]
    assert kinds == ["plain", "zoom_upright"]
