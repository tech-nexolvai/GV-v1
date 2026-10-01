"""The shape reader in the model bake-off, scored by the same code as every vision reader (#756 phase D).

`eval/experiments/model_bakeoff.py` scores a reader through one small surface: given a crop, return
what you read. A vision reader answers from the crop's pixels. This one answers from the drawing's
paths under the crop — `Crop.pdf_box` says where on the page the crop is — through
`extraction/glyph_reader.py` and a template set a person labelled. So the scorecard, the exact
comparison, the per-kind rates and the pairwise agreement with each vision reader are the same
code, and a glyph reading is judged exactly as a model's is.

**Leakage is kept out upstream, not here.** The template set must come from an inventory that
excluded the answer key's crops (`scripts/glyph_inventory.py inventory --exclude-crops`), which
the admin chose on #756 so the scored readings are never among the shapes a person labelled. This
adapter refuses a set whose manifest does not record that exclusion.

A refusal is returned as no reading, with the reader's own sentence as the error — the scorecard
counts it as abstained, which is what it is: a label handed to a reviewer.

Source: issue #756 · Verification: `tests/eval/test_glyph_bakeoff.py`
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from eval.experiments.model_bakeoff import Crop, ModelBakeoffError, ModelRead, ModelSpec
from extraction.annotations import VectorPath, read_annotation_layers
from extraction.glyph_bands import FractionBarGeometry
from extraction.glyph_reader import GlyphReading, ReaderSettings, TemplateSet, read_label
from extraction.glyph_shapes import ShapeSettings
from units.measurement import Unit

__all__ = ["GlyphBakeoffAdapter", "shape_settings_from"]


def shape_settings_from(config_hash: str) -> ShapeSettings:
    """The `ShapeSettings` a template set records, as a bake-off error where it cannot be read."""
    try:
        return ShapeSettings.from_config_hash(config_hash)
    except ValueError as error:
        raise ModelBakeoffError(
            f"the template set's shape settings are unreadable: {error}"
        ) from error


def _inside(box: tuple[Decimal, ...], within: tuple[Decimal, ...]) -> bool:
    return (
        within[0] <= box[0] and within[1] <= box[1] and box[2] <= within[2] and box[3] <= within[3]
    )


def _box(path: VectorPath) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    xs = [x for x, _ in path.points]
    ys = [y for _, y in path.points]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class GlyphBakeoffAdapter:
    """Read a crop from the drawing's paths, with a person's templates, for the bake-off."""

    spec: ModelSpec
    pdf: bytes
    templates: TemplateSet
    settings: ReaderSettings
    reader_settings: dict[str, str]
    """The GV_READER_* values the inventory planned with; the page's paths are read with the same."""

    _pages: dict[int, tuple[VectorPath, ...]] = field(default_factory=dict, init=False, repr=False)

    @classmethod
    def from_template_set(
        cls,
        directory: Path,
        *,
        pdf: bytes,
        maximum_distance: Decimal,
        minimum_margin: Decimal,
        maximum_size_ratio: Decimal,
        label_gap_pt: Decimal,
        maximum_label_pt: Decimal,
    ) -> GlyphBakeoffAdapter:
        """Build one from a template set on disk, refusing a set that could have seen the key."""
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if not manifest.get("excluded_crops"):
            raise ModelBakeoffError(
                "this template set's inventory did not exclude an answer key's crops, so the "
                "readings it would be scored on could be among the shapes it was labelled from"
            )
        templates = TemplateSet.load(directory)
        reader = {str(key): str(value) for key, value in manifest["reader_settings"].items()}
        settings = ReaderSettings(
            shape=shape_settings_from(templates.shape_settings),
            maximum_distance=maximum_distance,
            minimum_margin=minimum_margin,
            maximum_size_ratio=maximum_size_ratio,
            label_gap_pt=label_gap_pt,
            maximum_label_pt=maximum_label_pt,
            glyph_gap_pt=templates.glyph_gap_pt,
        )
        spec = ModelSpec(
            name="glyph-reader",
            model_id=f"glyph:{templates.set_hash[:12]}",
            input_usd_per_million=Decimal(0),
            output_usd_per_million=Decimal(0),
        )
        return cls(
            spec=spec, pdf=pdf, templates=templates, settings=settings, reader_settings=reader
        )

    def _page_glyphs(self, page_index: int) -> tuple[VectorPath, ...]:
        if page_index not in self._pages:
            reader = self.reader_settings
            layers = read_annotation_layers(
                self.pdf,
                page_index,
                document_version_id=UUID(int=0),
                dpi=600,
                line_minimum_pt=Decimal(reader["GV_READER_LINE_MINIMUM_PT"]),
                glyph_maximum_pt=Decimal(reader["GV_READER_GLYPH_MAXIMUM_PT"]),
                glyph_gap_pt=Decimal(reader["GV_READER_GLYPH_GAP_PT"]),
                fraction_bar=FractionBarGeometry(
                    bar_thickness_max_pt=Decimal(reader["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
                    bar_length_min_pt=Decimal(reader["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
                    reach_pt=Decimal(reader["GV_READER_FRACTION_REACH_PT"]),
                    glyph_min_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MIN_PT"]),
                    glyph_max_pt=Decimal(reader["GV_READER_FRACTION_GLYPH_MAX_PT"]),
                    proportion_max=Decimal(reader["GV_READER_FRACTION_PROPORTION_MAX"]),
                ),
            )
            self._pages[page_index] = layers.glyph_paths
        return self._pages[page_index]

    def read(self, crop: Crop) -> ModelRead:
        started = time.monotonic_ns()

        def answer(text: str | None, error: str | None) -> ModelRead:
            return ModelRead(
                model_name=self.spec.name,
                model_id=self.spec.model_id,
                crop_id=crop.crop_id,
                raw_text=text,
                unit_guess=Unit.INCH if text is not None else None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=(time.monotonic_ns() - started) // 1_000_000,
                error=error,
            )

        if crop.pdf_box is None:
            return answer(None, "the crop has no place on a page, so there are no paths to read")
        glyphs = self._page_glyphs(crop.page)
        seeds = [path for path in glyphs if _inside(_box(path), crop.pdf_box)]
        result = read_label(
            seeds, glyphs, templates=self.templates, settings=self.settings, within=crop.pdf_box
        )
        if isinstance(result, GlyphReading):
            return answer(result.text, None)
        return answer(None, result.reason)
