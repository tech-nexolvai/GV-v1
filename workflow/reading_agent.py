"""The reading agent in the worker: its settings, and the crops it is shown (#757, part 2).

`extraction/agent/policy.py` decides each step; `extraction/agent/graph.py` bounds it. This module is
what the steps *do* on a real page: the three crop refinements, each system-owned — a planner names
one and this code performs it — and the settings a deployment must state before any of it runs.

**Off by default.** `GV_READING_AGENT` unset, no agent is built and nothing changes. Set, every
number below is required and none has a default, for the reason every threshold here has none: the
step budget, whether an escalation reader is asked, the higher resolution a *sharper* look renders
at, which reader is primary and which escalates, and how a label's whole run is gathered.

**Which reader escalates is the admin's decision (#757 D-A2).** Both readers are named by their
extractor — the name `evidence/corroborate.py` counts independence by — and must be different, so
the escalation is a second reader and not the first one asked again.

**The refinements.**
- *Whole run* — the crop widened to the label's whole glyph run, from the file's own paths
  (`extraction/agent/geometry.py`). Where the file does not settle where the label ends, the
  refinement fails and says so, and the decision table hands the region to a reviewer.
- *Upright* — the crop turned by the label's own direction (`extraction.vector_first.upright_png`).
- *Sharper* — the page rendered again at the stated higher resolution, the same code and the same
  coordinates as the first render. A page too large to render whole at it fails the refinement with
  the reason; the table then carries on without it.

Each keeps the others: a sharper look at a widened, turned label is still widened and turned.

Source: issue #757 · Verification: `tests/workflow/test_reading_agent.py`
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from io import BytesIO
from typing import Final

from evidence.crop import CropSpec, CropStatus, RenderedPage, crop_pixel_box, generate_crop
from evidence.polygon import Polygon
from extraction.agent.geometry import LabelReach
from extraction.agent.graph import GraphLimits, RefinedCrop, RetryableToolFailure
from extraction.agent.tools import RefineCropArguments, Refinement
from extraction.glyph_bands import FractionLayout
from extraction.vector_first import upright_png
from storage.hashing import content_key, sha256_stream
from storage.store import ArtifactStore

__all__ = [
    "READING_AGENT_ENV",
    "ReadingAgentSettings",
    "RegionCrops",
    "reading_agent_from_environment",
]

READING_AGENT_ENV: Final = "GV_READING_AGENT"

#: The settings a deployment must state when it turns the agent on.
_REQUIRED: Final = (
    "GV_AGENT_MAX_STEPS",
    "GV_AGENT_MAX_ESCALATIONS",
    "GV_AGENT_SHARPER_DPI",
    "GV_AGENT_PRIMARY_READER",
    "GV_AGENT_LABEL_GAP_PT",
    "GV_AGENT_MAX_LABEL_PT",
)
ESCALATION_READER_ENV: Final = "GV_AGENT_ESCALATION_READER"


@dataclass(frozen=True, slots=True)
class ReadingAgentSettings:
    """Everything the agent is run with. Every field is stated; `config_hash` holds them all."""

    max_steps: int
    max_vlm_escalations: int
    sharper_dpi: int
    """The resolution a *sharper* look renders at. Stated, never defaulted (#757)."""

    primary_reader: str
    escalation_reader: str | None
    """`None` exactly when no escalation is permitted."""

    label_gap_pt: Decimal
    maximum_label_pt: Decimal

    def __post_init__(self) -> None:
        for name in ("max_steps", "max_vlm_escalations", "sharper_dpi"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
        if self.sharper_dpi <= 0:
            raise ValueError("sharper_dpi must be greater than zero")
        if not isinstance(self.primary_reader, str) or not self.primary_reader.strip():
            raise ValueError("primary_reader must name a vision reader")
        if self.max_vlm_escalations and not self.escalation_reader:
            raise ValueError("an escalation is permitted, so the escalation reader must be named")
        if not self.max_vlm_escalations and self.escalation_reader is not None:
            raise ValueError("no escalation is permitted, so no escalation reader may be named")
        if self.escalation_reader == self.primary_reader:
            raise ValueError(
                "the escalation reader must be a different reader from the primary; the same one "
                "asked twice is not a second witness"
            )
        # The graph's own bounds and the gathering lengths, checked now rather than at the first
        # region a worker reaches.
        _ = self.limits
        LabelReach(self.label_gap_pt, self.maximum_label_pt, Decimal(1))

    @property
    def limits(self) -> GraphLimits:
        """The graph's bounds. No OCR verification route is wired, so none is permitted."""
        return GraphLimits(
            max_steps=self.max_steps,
            max_ocr_retries=0,
            max_primary_vlm_calls=1,
            max_vlm_escalations=self.max_vlm_escalations,
            max_nearby_text_items=0,
            max_nearby_geometry_items=0,
        )

    def reach(self, glyph_gap_pt: Decimal) -> LabelReach:
        """How a label is gathered here, with the run gap the page's regions were formed by."""
        return LabelReach(self.label_gap_pt, self.maximum_label_pt, glyph_gap_pt)

    @property
    def config_text(self) -> str:
        """Every setting, written out — what `config_hash` is a fingerprint of."""
        return (
            f"agent_steps={self.max_steps};agent_escalations={self.max_vlm_escalations};"
            f"sharper_dpi={self.sharper_dpi};primary={self.primary_reader};"
            f"escalation={self.escalation_reader or '-'};label_gap_pt={self.label_gap_pt};"
            f"maximum_label_pt={self.maximum_label_pt}"
        )

    @property
    def config_hash(self) -> str:
        """Part of every agent run's identity: a region read under other settings is another run.

        A fingerprint of `config_text`, because a run's identity column holds 200 characters and the
        region's own id takes a fifth of them. The sharper resolution is kept readable beside it.
        """
        digest = hashlib.sha256(self.config_text.encode()).hexdigest()[:16]
        return f"sharper_dpi={self.sharper_dpi};agent={digest}"


def _integer(values: Mapping[str, str], name: str) -> int:
    try:
        return int(values[name].strip())
    except ValueError as error:
        raise ValueError(f"{name} must be a whole number: {error}") from error


def _length(values: Mapping[str, str], name: str) -> Decimal:
    try:
        return Decimal(values[name].strip())
    except InvalidOperation as error:
        raise ValueError(f"{name} must be a number") from error


def reading_agent_from_environment(
    environ: Mapping[str, str] | None = None,
) -> ReadingAgentSettings | None:
    """The agent a deployment configured, or `None` where it did not turn it on."""
    values = os.environ if environ is None else environ
    if values.get(READING_AGENT_ENV, "").strip().lower() not in {"1", "true", "yes"}:
        return None
    missing = [name for name in _REQUIRED if not values.get(name, "").strip()]
    if missing:
        raise ValueError(
            f"{READING_AGENT_ENV} is on but " + ", ".join(missing) + " are not set, and none of "
            "them has a default"
        )
    escalation = values.get(ESCALATION_READER_ENV, "").strip() or None
    return ReadingAgentSettings(
        max_steps=_integer(values, "GV_AGENT_MAX_STEPS"),
        max_vlm_escalations=_integer(values, "GV_AGENT_MAX_ESCALATIONS"),
        sharper_dpi=_integer(values, "GV_AGENT_SHARPER_DPI"),
        primary_reader=values["GV_AGENT_PRIMARY_READER"].strip(),
        escalation_reader=escalation,
        label_gap_pt=_length(values, "GV_AGENT_LABEL_GAP_PT"),
        maximum_label_pt=_length(values, "GV_AGENT_MAX_LABEL_PT"),
    )


@dataclass
class RegionCrops:
    """The crops one region's run is shown, cut by code. A planner names a refinement; this does it.

    `render(dpi)` renders the page, vendor's drawing only, or returns why it could not. `stacked`
    says whether a crop of a polygon shows a stacked fraction, and `layouts` where the parts of each
    one it shows were drawn, so every request says so of the crop it actually sends (#735, #834) — a
    widened crop can show one the first did not.
    """

    store: ArtifactStore
    render: Callable[[int], RenderedPage | str]
    base: RenderedPage
    polygon: Polygon
    margin_pt: Decimal
    sharper_dpi: int
    whole_run: Polygon | None
    """The region widened to its label's whole run, where the file's paths settle it."""

    rotation_degrees: int
    stacked: Callable[[Polygon], bool]
    layouts: Callable[[Polygon], tuple[FractionLayout, ...]]
    _dpi: int = field(init=False)
    _polygon: Polygon = field(init=False)
    _turned: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        self._dpi = self.base.dpi
        self._polygon = self.polygon

    @property
    def dpi(self) -> int:
        """The resolution of the crop now being shown."""
        return self._dpi

    @property
    def shows_stacked_fraction(self) -> bool:
        """Whether the crop now being shown shows a stacked fraction."""
        return self.stacked(self._polygon)

    @property
    def stacked_layouts(self) -> tuple[FractionLayout, ...]:
        """Where the parts of each stacked fraction the crop now being shown shows were drawn."""
        return self.layouts(self._polygon)

    def first(self) -> str | None:
        """Cut the first crop, round the region as it was read. `None` where it cannot be cut."""
        cut = self._cut(self.base)
        return None if isinstance(cut, RetryableToolFailure) else cut

    def png(self, key: str) -> bytes:
        """The bytes of a crop this run cut."""
        with self.store.get(key) as stored:
            return stored.read()

    def refine(self, arguments: RefineCropArguments) -> RefinedCrop | RetryableToolFailure:
        """Apply one named refinement to the current crop, keeping those already applied."""
        refinement = arguments.refinement
        if refinement is Refinement.WHOLE_RUN:
            if self.whole_run is None:
                return RetryableToolFailure(
                    "where the label ends is not settled by the drawing's own lines, so the crop "
                    "could not be widened to it"
                )
            if self._polygon is self.whole_run:
                return RetryableToolFailure("the crop already shows the label's whole run")
            previous = self._polygon
            self._polygon = self.whole_run
            return self._recut(undo=lambda: setattr(self, "_polygon", previous))
        if refinement is Refinement.UPRIGHT:
            if self.rotation_degrees == 0:
                return RetryableToolFailure("the label is not turned, so there is nothing to undo")
            if self._turned:
                return RetryableToolFailure("the label is already turned upright")
            self._turned = True
            return self._recut(undo=lambda: setattr(self, "_turned", False))
        if self._dpi >= self.sharper_dpi:
            return RetryableToolFailure("the crop is already at the sharper resolution")
        previous_dpi = self._dpi
        self._dpi = self.sharper_dpi
        return self._recut(undo=lambda: setattr(self, "_dpi", previous_dpi))

    def _recut(self, *, undo: Callable[[], None]) -> RefinedCrop | RetryableToolFailure:
        dpi = self._dpi
        rendered = self.base if dpi == self.base.dpi else self.render(dpi)
        if isinstance(rendered, str):
            undo()
            return RetryableToolFailure(f"the page could not be rendered at {dpi} dpi: {rendered}")
        cut = self._cut(rendered)
        if isinstance(cut, RetryableToolFailure):
            undo()
            return cut
        return RefinedCrop(cut)

    def _cut(self, rendered: RenderedPage) -> str | RetryableToolFailure:
        spec = CropSpec(polygon=self._polygon, context_margin_pt=self.margin_pt, dpi=rendered.dpi)
        result = generate_crop(rendered, spec, self.store)
        if result.status is not CropStatus.AVAILABLE or result.artifact is None:
            return RetryableToolFailure(f"the crop could not be cut: {result.reason}")
        key = result.artifact.key
        if self._turned:
            key = self._store_png(
                rendered,
                upright_png(self.png(key), label_rotation_degrees=self.rotation_degrees),
            )
        return key

    def _store_png(self, rendered: RenderedPage, png: bytes) -> str:
        stream = BytesIO(png)
        digest, _ = sha256_stream(stream)
        key = content_key(
            f"evidence-crops/{rendered.document_version_id}/pages/{rendered.page_index}",
            digest,
            suffix=".png",
        )
        stream.seek(0)
        self.store.put(key, stream, content_type="image/png")
        return key


def crop_box_px(
    rendered: RenderedPage, polygon: Polygon, margin_pt: Decimal
) -> tuple[int, int, int, int]:
    """The page pixels a crop of `polygon` is cut from, at `rendered`'s resolution."""
    return crop_pixel_box(
        rendered, CropSpec(polygon=polygon, context_margin_pt=margin_pt, dpi=rendered.dpi)
    )
