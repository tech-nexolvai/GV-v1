"""Model bake-off scoring over stubbed adapters, with no Bedrock credentials."""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import pytest

from eval.experiments.model_bakeoff import (
    BedrockBakeoffAdapter,
    Crop,
    KeyFrame,
    ModelBakeoffError,
    ModelRead,
    ModelSpec,
    ReadingParseError,
    _measured_coordinate_mode,
    key_frame,
    key_polygon_dpi,
    load_crops,
    load_model_specs,
    parse_dimension_reading,
    render_crop,
    render_csv,
    render_markdown,
    run_bakeoff,
)
from extraction.models.validation import CoordinateMode
from units.measurement import Measurement, Unit

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _decode_rgb_png(data: bytes) -> tuple[int, int, bytes]:
    offset = len(PNG_SIGNATURE)
    width = 0
    height = 0
    payloads: list[bytes] = []
    assert data.startswith(PNG_SIGNATURE)
    while offset < len(data):
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height, depth, colour, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            assert (depth, colour, compression, filtering, interlace) == (8, 2, 0, 0, 0)
        elif kind == b"IDAT":
            payloads.append(payload)
        elif kind == b"IEND":
            break
    inflated = zlib.decompress(b"".join(payloads))
    stride = width * 3
    rows = []
    for row in range(height):
        start = row * (stride + 1)
        assert inflated[start] == 0
        rows.append(inflated[start + 1 : start + 1 + stride])
    return width, height, b"".join(rows)


def _pdf_with_red_box() -> bytes:
    content = b"q 1 0 0 rg 20 40 40 40 re f Q\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] /Contents 4 0 R >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"endstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += str(number).encode() + b" 0 obj\n" + body + b"\nendobj\n"
    start = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(start).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


def _write_answer_key(
    case_dir: Path,
    *,
    annotator: str = "human reviewer",
    metadata: dict[str, object] | None = None,
) -> None:
    case_dir.mkdir(parents=True)
    (case_dir / "shop.pdf").write_bytes(_pdf_with_red_box())
    (case_dir / "arch.pdf").write_bytes(_pdf_with_red_box())
    (case_dir / "answer_key.json").write_text(
        json.dumps(
            {
                "id": "synthetic-bakeoff",
                "product_type": "countertop",
                "arch": "arch.pdf",
                "shop": "shop.pdf",
                "ground_truth": {
                    "observations": [
                        {
                            "semantic_type": "CT007",
                            "source": "SHOP",
                            "value": {"exact": "51/2", "unit": "in", "raw_text": '25 1/2"'},
                            "page": 1,
                            "polygon": [20, 20, 60, 60],
                            "item_id": "SYN-1",
                        }
                    ],
                    "matches": [],
                    "expected_findings": [],
                },
                "provenance": {
                    "annotator": annotator,
                    "annotated_on": "2026-09-16",
                    "documents": [
                        {
                            "source": "SHOP",
                            "document_version_id": "11111111-1111-4111-8111-111111111111",
                            "content_hash": "sha256:" + "a" * 64,
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    if metadata is not None:
        (case_dir / "model_bakeoff_metadata.json").write_text(
            json.dumps(metadata),
            encoding="utf-8",
        )


def _inches(value: str, raw: str | None = None) -> Measurement:
    return Measurement(exact=Fraction(value), unit=Unit.INCH, raw_text=raw)


def _spec(name: str) -> ModelSpec:
    return ModelSpec(
        name=name,
        model_id=f"bedrock:{name}",
        input_usd_per_million=Decimal("1.00"),
        output_usd_per_million=Decimal("5.00"),
    )


@dataclass(frozen=True, slots=True)
class StubAdapter:
    spec: ModelSpec
    readings: dict[str, tuple[str, Unit | None]]
    input_tokens: int = 100
    output_tokens: int = 10
    latency_ms: int = 250

    def read(self, crop: Crop) -> ModelRead:
        raw, unit = self.readings[crop.crop_id]
        return ModelRead(
            model_name=self.spec.name,
            model_id=self.spec.model_id,
            crop_id=crop.crop_id,
            raw_text=raw,
            unit_guess=unit,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            latency_ms=self.latency_ms,
        )


def test_exact_fraction_equivalence_is_scored_not_string_similarity() -> None:
    crop = Crop("c1", _inches("51/2", '25 1/2"'), tags=frozenset({"fraction"}))
    adapter = StubAdapter(_spec("nova-pro"), {"c1": ("25.5", Unit.INCH)})

    scorecard = run_bakeoff([adapter], [crop])

    assert scorecard.models[0].exact_rate == Fraction(1, 1)
    assert scorecard.models[0].tag_rate("fraction") == Fraction(1, 1)


def test_wrong_fraction_scores_zero_on_that_crop() -> None:
    crop = Crop("c1", _inches("51/2", '25 1/2"'), tags=frozenset({"fraction"}))
    adapter = StubAdapter(_spec("nova-pro"), {"c1": ('25 9/16"', Unit.INCH)})

    scorecard = run_bakeoff([adapter], [crop])

    assert scorecard.models[0].exact_rate == Fraction(0, 1)
    assert scorecard.models[0].reads[0].parsed == _inches("409/16", '25 9/16"')


def test_hard_cases_tokens_latency_cost_and_required_pairwise_rates_are_reported() -> None:
    crops = [
        Crop("fraction", _inches("51/2"), tags=frozenset({"fraction"})),
        Crop("rotated", _inches("10"), tags=frozenset({"rotated"})),
        Crop("small", _inches("4"), tags=frozenset({"small_glyph"})),
    ]
    nova = StubAdapter(
        _spec("nova-pro"),
        {"fraction": ("25.5", Unit.INCH), "rotated": ("10", Unit.INCH), "small": ("4", Unit.INCH)},
    )
    haiku = StubAdapter(
        _spec("claude-haiku-4.5"),
        {
            "fraction": ("25 1/2", Unit.INCH),
            "rotated": ("10", Unit.INCH),
            "small": ("3 7/8", Unit.INCH),
        },
    )
    qwen = StubAdapter(
        _spec("qwen3-vl-235b"),
        {
            "fraction": ("25 9/16", Unit.INCH),
            "rotated": ("10", Unit.INCH),
            "small": ("4", Unit.INCH),
        },
    )
    sonnet = StubAdapter(
        _spec("claude-sonnet-4.6"),
        {"fraction": ("25.5", Unit.INCH), "rotated": ("10", Unit.INCH), "small": ("4", Unit.INCH)},
    )

    scorecard = run_bakeoff([nova, haiku, qwen, sonnet], crops)

    nova_score = scorecard.models[0]
    assert nova_score.exact_rate == Fraction(3, 3)
    assert nova_score.tag_rate("fraction") == Fraction(1, 1)
    assert nova_score.tag_rate("rotated") == Fraction(1, 1)
    assert nova_score.tag_rate("small_glyph") == Fraction(1, 1)
    assert nova_score.input_tokens == 300
    assert nova_score.output_tokens == 30
    assert nova_score.average_latency_ms == Decimal(250)
    assert nova_score.cost_usd == Decimal("0.000450")

    pairs = {pair.names: pair for pair in scorecard.pairwise}
    assert pairs[frozenset(("nova-pro", "claude-haiku-4.5"))].disagree_rate == Fraction(1, 3)
    assert pairs[frozenset(("nova-pro", "qwen3-vl-235b"))].disagree_rate == Fraction(1, 3)
    assert pairs[frozenset(("claude-sonnet-4.6", "nova-pro"))].disagree_rate == Fraction(0, 3)
    assert "revise toward claude-sonnet-4.6 + nova-pro" in scorecard.recommendation

    markdown = render_markdown(scorecard)
    assert "Cost USD" in markdown
    assert "Nova Pro + Claude Haiku 4.5" in markdown
    assert "Recommendation:" in markdown
    assert "pair,,nova-pro+claude-haiku-4.5" in render_csv(scorecard)


def test_disagreement_counts_missing_or_unparseable_reads_as_review_work() -> None:
    crop = Crop("c1", _inches("10"))
    nova = StubAdapter(_spec("nova-pro"), {"c1": ("10", Unit.INCH)})
    bad = StubAdapter(_spec("claude-haiku-4.5"), {"c1": ("", Unit.INCH)})

    scorecard = run_bakeoff([nova, bad], [crop])

    assert scorecard.models[1].error_count == 1
    assert scorecard.pairwise[0].disagree_rate == Fraction(1, 1)


def test_unmarked_reading_without_unit_guess_is_refused() -> None:
    with pytest.raises(Exception, match="no unit_guess"):
        parse_dimension_reading("25.5", None)


def test_zero_crops_or_models_are_refused() -> None:
    with pytest.raises(ModelBakeoffError, match="zero crops"):
        run_bakeoff([StubAdapter(_spec("nova-pro"), {})], [])
    with pytest.raises(ModelBakeoffError, match="zero models"):
        run_bakeoff([], [Crop("c1", _inches("10"))])


def test_adapter_results_must_belong_to_the_requested_crop_and_model() -> None:
    @dataclass(frozen=True, slots=True)
    class MislabellingAdapter:
        spec: ModelSpec

        def read(self, crop: Crop) -> ModelRead:
            return ModelRead(
                model_name="someone-else",
                model_id=self.spec.model_id,
                crop_id="other-crop",
                raw_text="10",
                unit_guess=Unit.INCH,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0,
            )

    with pytest.raises(ModelBakeoffError, match="other-crop"):
        run_bakeoff([MislabellingAdapter(_spec("nova-pro"))], [Crop("c1", _inches("10"))])


def test_render_crop_uses_page_polygon_and_coordinate_dpi() -> None:
    crop = render_crop(
        _pdf_with_red_box(),
        page=1,
        polygon=(20, 20, 60, 60),
        polygon_dpi=72,
        output_dpi=72,
    )

    width, height, rgb = _decode_rgb_png(crop)

    assert (width, height) == (40, 40)
    assert rgb[0:3] == b"\xff\x00\x00"


def test_gold_case_directory_renders_human_key_and_refuses_self_verified(tmp_path: Path) -> None:
    case_dir = tmp_path / "human-case"
    _write_answer_key(
        case_dir,
        metadata={"provenance": "human-read", "tags": {"0": ["rotated", "small_glyph"]}},
    )

    crops = load_crops(case_dir, polygon_dpi=72)

    assert len(crops) == 1
    assert crops[0].expected == _inches("51/2", '25 1/2"')
    assert crops[0].tags == frozenset({"fraction", "rotated", "small_glyph"})
    # 40 pt square, shown to a model at production's reader resolution (#867): 40 / 72 * 300 is
    # 166.7, and the renderer drops the part pixel, as it dropped 333.3's at 600.
    assert _decode_rgb_png(crops[0].image)[0:2] == (166, 166)

    self_verified = tmp_path / "self-verified-case"
    _write_answer_key(
        self_verified,
        annotator="dual-notation self-verified (mm~=inch), NOT per-case human-read",
    )
    with pytest.raises(ModelBakeoffError, match="human-read answer key"):
        load_crops(self_verified, polygon_dpi=72)


# --- #835: a key says what frame its polygons are in, and a loader believes the key -------------


def test_a_key_that_records_no_frame_is_refused_unless_its_frame_is_named(
    tmp_path: Path,
) -> None:
    """**The trap the pilot key set.** Its polygons are at 300 dpi and nothing in it says so; read as
    600, every crop came from near the page's top-left corner. A frameless key now loads only when
    the caller names the frame, and so takes responsibility for it."""
    case_dir = tmp_path / "frameless"
    _write_answer_key(case_dir)

    with pytest.raises(ModelBakeoffError, match="does not record the pixel frame"):
        load_crops(case_dir)
    with pytest.raises(ModelBakeoffError, match="does not record the pixel frame"):
        key_polygon_dpi(case_dir, polygon_dpi=None)


def test_a_key_in_a_300_dpi_frame_loads_when_300_is_named(tmp_path: Path) -> None:
    """The pilot key's case: no frame recorded, polygons at 300 dpi. Named, it loads, and the
    polygon `(20, 20, 60, 60)` stands for 4.8..14.4 pt — not the 20..60 pt the default would read.
    """
    case_dir = tmp_path / "pilot-like"
    _write_answer_key(case_dir)

    (crop,) = load_crops(case_dir, polygon_dpi=300)

    # The page is 100 pt tall, so top-left 4.8 pt is PDF bottom-up 95.2.
    assert crop.pdf_box == (Decimal("4.8"), Decimal("85.6"), Decimal("14.4"), Decimal("95.2"))
    assert key_polygon_dpi(case_dir, polygon_dpi=300) == 300


def test_a_key_is_read_in_the_frame_it_records_and_a_different_one_is_refused(
    tmp_path: Path,
) -> None:
    """A recorded frame needs no caller to name it, and a caller who names another is wrong: read at
    a frame the key says it is not in, every crop comes from the wrong part of the page."""
    case_dir = tmp_path / "framed"
    _write_answer_key(
        case_dir, metadata={"frame": {"polygon_dpi": 72, "margin_pt": "9"}, "tags": {}}
    )

    (unnamed,) = load_crops(case_dir)
    (named,) = load_crops(case_dir, polygon_dpi=72)

    assert unnamed.pdf_box == named.pdf_box == (Decimal(20), Decimal(40), Decimal(60), Decimal(80))
    assert key_frame(case_dir) == KeyFrame(polygon_dpi=72, margin_pt=Decimal(9))
    with pytest.raises(ModelBakeoffError, match="records its polygons at 72 dpi, and 300"):
        load_crops(case_dir, polygon_dpi=300)
    with pytest.raises(ModelBakeoffError, match="records its polygons at 72 dpi"):
        key_polygon_dpi(case_dir, polygon_dpi=600)


@pytest.mark.parametrize(
    "frame",
    [
        {"polygon_dpi": 600.0, "margin_pt": "9"},
        {"polygon_dpi": 600, "margin_pt": 9.0},
        {"polygon_dpi": True, "margin_pt": "9"},
        {"polygon_dpi": 0, "margin_pt": "9"},
        {"polygon_dpi": 600, "margin_pt": "-1"},
        {"polygon_dpi": 600},
    ],
)
def test_a_frame_is_refused_unless_it_is_stated_exactly_and_whole(
    tmp_path: Path, frame: dict[str, object]
) -> None:
    """A frame is two exact numbers. A float, a zero resolution, a negative margin or a missing half
    is not a frame anything can be placed in, and is refused rather than read as one."""
    case_dir = tmp_path / "bad-frame"
    _write_answer_key(case_dir, metadata={"frame": frame})

    with pytest.raises(ModelBakeoffError, match="invalid bake-off metadata"):
        load_crops(case_dir, polygon_dpi=600)


def test_json_manifests_load_without_client_data_or_float_prices(tmp_path: Path) -> None:
    crops_path = tmp_path / "crops.json"
    crop_image = tmp_path / "crops/crop-001.png"
    crop_image.parent.mkdir()
    crop_image.write_bytes(b"fake-png-for-stubbed-adapter")
    crops_path.write_text(
        json.dumps(
            {
                "crops": [
                    {
                        "id": "crop-001",
                        "image": "crops/crop-001.png",
                        "expected": {"exact": "25 1/2", "unit": "in"},
                        "tags": ["fraction", "rotated"],
                        "nearby_text": ["CAB-7"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "nova-pro",
                        "model_id": "amazon.nova-pro-v1:0",
                        "input_usd_per_million": "0.80",
                        "output_usd_per_million": "3.20",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    crops = load_crops(crops_path, polygon_dpi=72)
    specs = load_model_specs(models_path)

    assert crops[0].expected == _inches("51/2")
    assert crops[0].image == b"fake-png-for-stubbed-adapter"
    assert crops[0].tags == frozenset({"fraction", "rotated"})
    assert specs[0].input_usd_per_million == Decimal("0.80")

    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "nova-pro",
                        "model_id": "amazon.nova-pro-v1:0",
                        "input_usd_per_million": 0.8,
                        "output_usd_per_million": "3.20",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ModelBakeoffError, match="model prices must be authored as exact text"):
        load_model_specs(models_path)


# --- #732: the scorer must read the drawing's notation, and each model runs in its measured space --


@pytest.mark.parametrize(
    ("returned", "inches"),
    [
        ('25-1/2"', "51/2"),  # hyphenated, as the trade writes it
        ('5-1/4"', "21/4"),
        ("381 [15]", "15"),  # dual unit — the bracketed inch is authoritative (Q12)
        ('381mm [15"]', "15"),  # the same token with its units spelled out, as a model returns it
        ('2" (VIF)', "2"),  # a site note is not part of the number
        ("6'-0\"", "72"),  # feet-inches keeps its hyphen
    ],
)
def test_a_model_reading_in_the_drawings_own_notation_is_parsed(returned: str, inches: str) -> None:
    """Before #732 every one of these was a parse failure, scored as wrong.

    Ten of the seventeen answers in the first human key are written this way, so a model that read
    the sheet exactly as printed would have been recorded as unable to read it.
    """
    parsed = parse_dimension_reading(returned, None)

    assert parsed.exact == Fraction(inches)
    assert parsed.unit is Unit.INCH


def test_a_compound_reading_is_refused_as_not_one_value() -> None:
    """`39 1/4"+6"` has no single value, so it can never be scored exact against one (#730)."""
    with pytest.raises(ReadingParseError, match="not one value"):
        parse_dimension_reading('39 1/4"+6"', Unit.INCH)


def test_each_production_reader_runs_in_the_space_production_measured() -> None:
    """Nova Pro answers on the 0-1000 grid (#668, #699); the bake-off must not call it in pixels.

    It is switched off in production (#751) and still resolves here: it was turned off for accuracy,
    not because its space is unknown, and the bake-off is where it keeps being measured.

    Called in the default space, a grid answer on a small crop is out of bounds, the call is rejected,
    and a correct reading is scored as a miss — a configuration production never runs.
    """
    nova = pytest.importorskip("extraction.models.nova")

    assert _measured_coordinate_mode(nova, "amazon.nova-pro-v1:0") is CoordinateMode.NOVA_GRID
    assert _measured_coordinate_mode(nova, "amazon.nova-2-lite-v1:0") is CoordinateMode.PIXELS
    assert _measured_coordinate_mode(nova, "us.amazon.nova-2-lite-v1:0") is CoordinateMode.PIXELS


def test_an_unmeasured_model_is_refused_rather_than_defaulted() -> None:
    """A model's name is not evidence of its answer space (#668); neither is a default.

    Claude Haiku is a disabled reader whose space has never been measured, and a model outside
    `VISION_READERS` has no recorded space at all. Either must state one in the manifest.
    """
    nova = pytest.importorskip("extraction.models.nova")
    assert _measured_coordinate_mode(nova, "anthropic.claude-haiku-4-5-20251001-v1:0") is None

    spec = ModelSpec(
        name="unmeasured",
        model_id="vendor.some-vision-model-v1:0",
        input_usd_per_million=Decimal(0),
        output_usd_per_million=Decimal(0),
    )
    with pytest.raises(ModelBakeoffError, match="no measured coordinate space"):
        BedrockBakeoffAdapter(spec)


def test_the_manifest_carries_a_stated_coordinate_space(tmp_path: Path) -> None:
    models_path = tmp_path / "models.json"
    models_path.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "name": "measured-elsewhere",
                        "model_id": "vendor.some-vision-model-v1:0",
                        "input_usd_per_million": "1",
                        "output_usd_per_million": "2",
                        "coordinate_mode": "nova_grid",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    (spec,) = load_model_specs(models_path)

    assert spec.coordinate_mode is CoordinateMode.NOVA_GRID


def test_a_bakeoff_crop_shows_the_vendor_drawing_and_not_the_reviewer_note() -> None:
    """**#742.** The bake-off must score readers on the pixels production sends them — the vendor's
    layer only. Before, the reviewer's notes were painted in, and a person authoring the key read
    those same crops."""
    from evidence.crop import decode_rgb_png
    from tests.extraction.test_rasterise import _reviewed_sheet

    # The square is at page (100..140, 100..140) pt on a 300 pt page: at 150 dpi, image
    # x 208..292, y 333..417. The crop is well inside it.
    png = render_crop(
        _reviewed_sheet(), page=1, polygon=(220, 345, 280, 405), polygon_dpi=150, output_dpi=150
    )
    width, height, rgb = decode_rgb_png(png)
    pixels = [rgb[offset : offset + 3] for offset in range(0, width * height * 3, 3)]

    assert not [p for p in pixels if p[0] > 200 and p[1] < 60 and p[2] < 60], "reviewer note shown"
    assert [p for p in pixels if max(p) < 60], "the vendor's drawing went with it"
