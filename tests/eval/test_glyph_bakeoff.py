"""The shape reader in the bake-off (#756 phase D).

Verification for: `eval/experiments/glyph_bakeoff.py`, and the two bake-off changes it needs —
`Crop.pdf_box` and the scorecard's wrong / abstained counts.

The one that matters most is the refusal: a template set whose inventory did not exclude the
answer key's crops is not scored at all, because its readings could be among the shapes a person
labelled — the scorecard would measure the labeller's memory of the key.

The page's paths are supplied directly, in the made-up font of `tests/extraction/test_glyph_reader.py`,
so no client drawing is read and no model is called.
"""

from __future__ import annotations

import json
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

import numpy as np
import pytest

from eval.experiments.glyph_bakeoff import GlyphBakeoffAdapter, shape_settings_from
from eval.experiments.model_bakeoff import (
    Crop,
    ModelBakeoffError,
    pdf_box,
    render_markdown,
    run_bakeoff,
)
from tests.extraction.test_annotations import BOTH_LAYERS
from tests.extraction.test_glyph_reader import SETTINGS, SHAPE, TEMPLATES, _fraction, _row
from units.measurement import Measurement, Unit

READER = {
    "GV_READER_LINE_MINIMUM_PT": "6",
    "GV_READER_GLYPH_MAXIMUM_PT": "6",
    "GV_READER_GLYPH_GAP_PT": "4",
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": "0.3",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": "1",
    "GV_READER_FRACTION_REACH_PT": "3",
    "GV_READER_FRACTION_GLYPH_MIN_PT": "1",
    "GV_READER_FRACTION_GLYPH_MAX_PT": "12",
    "GV_READER_FRACTION_PROPORTION_MAX": "2.5",
}


def _inches(value: Fraction | int) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f"{value} in")


def _adapter() -> GlyphBakeoffAdapter:
    """An adapter whose page is a `12"` label, a stacked `3/4"` and a bare `99`, far apart."""
    from eval.experiments.model_bakeoff import ModelSpec

    twelve, _ = _row('12"', 0, 0)
    fraction, _ = _fraction("", "3", "4", x=200, y=0)
    bare, _ = _row("99", 400, 0)
    adapter = GlyphBakeoffAdapter(
        spec=ModelSpec(
            name="glyph-reader",
            model_id="glyph:test",
            input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
        ),
        pdf=b"",
        templates=TEMPLATES,
        settings=SETTINGS,
        reader_settings=READER,
    )
    adapter._pages[0] = tuple(twelve + fraction + bare)
    return adapter


def _crop(crop_id: str, expected: Measurement, box: tuple[int, int, int, int]) -> Crop:
    return Crop(
        crop_id=crop_id,
        expected=expected,
        page=0,
        pdf_box=tuple(Decimal(value) for value in box),  # type: ignore[arg-type]
    )


def test_the_shape_reader_is_scored_by_the_bake_offs_own_scorecard() -> None:
    """**Outcome: right, wrong and abstained counted by the same code as every vision reader.**

    `12"` reads right; the fraction reads right; the bare `99` has no unit and is refused; and a
    crop whose key says 13" where the label says 12" is counted wrong — the count a reader must
    hold at zero.
    """
    crops = [
        _crop("plain", _inches(12), (-5, -5, 30, 15)),
        _crop("fraction", _inches(Fraction(3, 4)), (195, -5, 220, 30)),
        _crop("bare", _inches(99), (395, -5, 420, 15)),
        _crop("keyed-wrong", _inches(13), (-5, -5, 30, 15)),
    ]

    scorecard = run_bakeoff([_adapter()], crops, required_pairs=())

    (score,) = scorecard.models
    assert score.exact_count == 2
    assert score.wrong_count == 1
    assert score.abstained_count == 1
    table = render_markdown(scorecard)
    assert "| Wrong | Abstained |" in table
    assert "| glyph-reader | 1/2 (50.0%) | 1 | 1 |" in table


def test_a_crop_that_cuts_its_label_is_refused_not_read_in_part() -> None:
    """Outcome: a crop showing `2"` of `12"` abstains — the reader reads the whole label or nothing."""
    crop = _crop("cut", _inches(2), (5, -5, 30, 15))

    read = _adapter().read(crop)

    assert read.raw_text is None
    assert read.error is not None and "past the edge" in read.error


def test_a_crop_with_no_page_box_is_refused() -> None:
    crop = Crop(crop_id="image-only", expected=_inches(1))

    read = _adapter().read(crop)

    assert read.raw_text is None and read.error is not None


def _write_set(folder_root: Path, *, excluded: str | None) -> Path:
    digest = "cd" * 32
    folder = folder_root / digest[:12]
    folder.mkdir(parents=True)
    (folder / "manifest.json").write_text(
        json.dumps(
            {
                "sha256": digest,
                "shape_settings": SHAPE.config_hash,
                "reader_settings": READER,
                "excluded_crops": excluded,
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
    return folder


MATCH = {
    "maximum_distance": Decimal("0.5"),
    "minimum_margin": Decimal("0.1"),
    "maximum_size_ratio": Decimal("1.3"),
    "label_gap_pt": Decimal(4),
    "maximum_label_pt": Decimal(80),
}


def test_a_template_set_that_could_have_seen_the_key_is_refused(tmp_path: Path) -> None:
    """**The admin's leakage rule** (#756, 2026-09-30). Outcome: no exclusion recorded, no scoring."""
    folder = _write_set(tmp_path, excluded=None)

    with pytest.raises(ModelBakeoffError, match="did not exclude"):
        GlyphBakeoffAdapter.from_template_set(folder, pdf=b"", **MATCH)


def test_a_set_that_excluded_the_key_is_loaded_with_its_own_sizing(tmp_path: Path) -> None:
    folder = _write_set(tmp_path, excluded="crops.csv")

    adapter = GlyphBakeoffAdapter.from_template_set(folder, pdf=b"", **MATCH)

    assert adapter.spec.model_id == f"glyph:{'cd' * 6}"
    assert adapter.settings.shape == SHAPE
    assert adapter.settings.glyph_gap_pt == Decimal(4)
    assert shape_settings_from(SHAPE.config_hash) == SHAPE


def test_the_crop_box_is_the_one_the_vision_image_is_cut_by() -> None:
    """Outcome: `pdf_box` turns a key's pixel polygon into PDF points, top-left to bottom-left origin.

    One computation for both routes, so a vision reader and the shape reader score the same area.
    """
    box = pdf_box(BOTH_LAYERS, page=1, polygon=(600, 600, 1200, 900), polygon_dpi=600)

    assert box[0] == Decimal(72) and box[2] == Decimal(144)
    assert box[3] - box[1] == Decimal(36)
