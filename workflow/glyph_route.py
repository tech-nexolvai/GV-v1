"""The shape reader as a pipeline route, off unless a deployment points it at a template set (#756 E).

`extraction/glyph_reader.py` reads one label from its confirmed character shapes. This is the route
that runs it over a page: every region the stamp-only route plans to read is a seed, each label is
gathered whole and read once, and what it read — or why it would not — is handed back to the stage
to record.

**Off by default.** `GV_GLYPH_TEMPLATES` names a template set written by
`scripts/glyph_inventory.py build`; unset, the route does not exist and nothing changes. Set, the
five match settings are required and none has a default, for the reason every threshold on this
route has none (#756 §Rules).

**Its readings never seal, until the admin decides what they may seal (#756 D2).** The stage records
them as candidates for a reviewer to confirm — they reach the form pre-filled (#712) — and keeps
them out of cross-route corroboration entirely, so a shape reading and a vision reading that agree
do not become a sealed value. A mislabelled template is a systematic error that repeats on every
sheet; what may count as its second witness is the admin's decision, not this module's.

Source: issue #756 · Verification: `tests/workflow/test_glyph_route.py`
"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Final

from extraction.annotations import OutlinedTextRegion, VectorPath
from extraction.glyph_reader import (
    GlyphAbstention,
    GlyphReading,
    ReaderSettings,
    TemplateSet,
    gather_label,
    read_label,
)
from extraction.glyph_shapes import ShapeSettings

__all__ = [
    "GLYPH_EXTRACTOR",
    "GLYPH_TEMPLATES_ENV",
    "GlyphRoute",
    "glyph_route_from_environment",
    "read_page_labels",
]

#: The extractor name glyph readings are recorded under — its own, so every consumer can tell a
#: reading made from shapes from one a model made.
GLYPH_EXTRACTOR: Final = "extraction.glyph_reader"

GLYPH_TEMPLATES_ENV: Final = "GV_GLYPH_TEMPLATES"

#: The settings a deployment must state when it turns the route on.
_REQUIRED: Final = {
    "GV_GLYPH_MAX_DISTANCE": "maximum_distance",
    "GV_GLYPH_MARGIN": "minimum_margin",
    "GV_GLYPH_SIZE_RATIO": "maximum_size_ratio",
    "GV_GLYPH_LABEL_GAP_PT": "label_gap_pt",
    "GV_GLYPH_MAX_LABEL_PT": "maximum_label_pt",
}


@dataclass(frozen=True, slots=True)
class GlyphRoute:
    """One template set and the settings it is read with."""

    templates: TemplateSet
    settings: ReaderSettings

    @property
    def config_hash(self) -> str:
        """Part of the extraction run's identity: rows read under another set are another run."""
        return f"{self.settings.config_hash};templates={self.templates.set_hash}"


def glyph_route_from_environment(environ: Mapping[str, str] | None = None) -> GlyphRoute | None:
    """The route a deployment configured, or `None` where it configured none."""
    values = os.environ if environ is None else environ
    path = values.get(GLYPH_TEMPLATES_ENV, "").strip()
    if not path:
        return None
    missing = [name for name in _REQUIRED if not values.get(name, "").strip()]
    if missing:
        raise ValueError(
            f"{GLYPH_TEMPLATES_ENV} is set but "
            + ", ".join(missing)
            + " are not, and none of them "
            "has a default"
        )
    try:
        stated = {field: Decimal(values[name].strip()) for name, field in _REQUIRED.items()}
    except InvalidOperation as error:
        raise ValueError(f"a {GLYPH_TEMPLATES_ENV} setting is not a number: {error}") from error
    templates = TemplateSet.load(Path(path))
    return GlyphRoute(
        templates=templates,
        settings=ReaderSettings(
            shape=ShapeSettings.from_config_hash(templates.shape_settings),
            glyph_gap_pt=templates.glyph_gap_pt,
            **stated,
        ),
    )


def read_page_labels(
    regions: Sequence[OutlinedTextRegion],
    page_glyphs: Sequence[VectorPath],
    route: GlyphRoute,
) -> tuple[list[GlyphReading], Counter[str]]:
    """Read every label the planned regions belong to, once each; count why the rest abstained.

    Two regions are often one label — a whole number and the inch mark beyond a gap — so a label
    gathered from one seed is not read again from another. Every region is read or counted.
    """
    consumed: set[int] = set()
    readings: list[GlyphReading] = []
    abstentions: Counter[str] = Counter()
    for region in regions:
        if not region.glyph_paths or any(id(path) in consumed for path in region.glyph_paths):
            continue
        members, _unclosed = gather_label(region.glyph_paths, page_glyphs, settings=route.settings)
        consumed.update(id(path) for path in members)
        result = read_label(
            members, page_glyphs, templates=route.templates, settings=route.settings
        )
        if isinstance(result, GlyphAbstention):
            abstentions[result.reason] += 1
        else:
            readings.append(result)
    return readings, abstentions
