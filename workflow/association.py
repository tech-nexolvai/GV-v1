"""The settings the association step needs, and the readings it is given.

`extraction/geometry/text_association.py` decides which dimension line a number annotates. It takes
two lengths and gives them no defaults, and its docstring says why: they are empirical, and *"a
default here would ship today's guess as ground truth"*. Nothing has ever called it, because nothing
had numbers to pass.

This module is the join between that decision and the pipeline: the settings a deployment states, and
the conversion from what a reader produced into what `associate` takes.

**Five numbers, none with a default, all recorded on the run that used them.** Three describe the
vendor's path geometry — which runs of a path are line-work rather than glyph outlines — and two
describe association itself. A pipeline must not be the place a threshold acquires a value, so they
arrive as configuration and travel into the extraction run's `config_hash`, which is what makes a
re-association under different numbers a different run rather than the same one quietly meaning
something else (#487).

**Nothing here decides which segments are dimension lines.** That is #179 and it is still gated on
real drawings. Every straight line on the page is a candidate, and `associate` refuses when it cannot
tell two of them apart — which on the first real sheet it did five times out of twenty.

Source: issue #545. Verification: `tests/workflow/test_association.py`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from evidence.polygon import Polygon
from extraction.geometry.text_association import DimensionText

__all__ = ["AssociationSettings", "LocalizedOcrSettings", "ReadItem", "dimension_texts"]


class ReadItem(Protocol):
    """What both readers already produce: where a reading sits and which way it reads.

    A protocol rather than a base class, because `extraction.reader.TextItem` and
    `extraction.annotations.MarkupNote` are produced by different modules for different reasons and
    neither should have to know about this one. What they have in common is exactly what an
    association needs.
    """

    @property
    def extent(self) -> Polygon:
        """Where it sits, in stored page coordinates."""

    @property
    def rotation_degrees(self) -> int:
        """Which way it reads, as the file states it — never inferred from the box."""


@dataclass(frozen=True, slots=True)
class AssociationSettings:
    """The five lengths the association step runs under. Stated by a deployment, never defaulted.

    **All five are required and none is a guess this code makes.** `dpi` is already handled this way
    by `DatabaseStages`, and `text_association` requires its two for the reason it gives at length.
    The three geometry lengths come from `extraction/annotations.py`, which requires them because
    deciding which vector primitives are dimension lines is a detector (#179) and one sheet cannot
    fix a number that decides what gets read.

    The first three are in **PDF points** — a physical length, 72 to the inch, the same on every
    sheet size. The last two are in **stored units**, normalised `0..1` against the visible page, so
    one number means a different physical distance on an A4 sheet than on a 24×36 one. That
    difference is not a wart to smooth over: `associate` measures in stored space, so a caller
    holding a physical distance converts it per page, and the conversion belongs with the transform
    that owns the PDF→stored step.

    Values used against `AI_Set 2` are recorded in the issue, not here. A value in this docstring
    would become the value everyone uses, which is the default this class exists to refuse.
    """

    line_minimum_pt: Decimal
    """A run between consecutive path points at least this long is line-work."""

    glyph_maximum_pt: Decimal
    """A path whose bounding box is smaller than this in both axes may be part of a glyph."""

    glyph_gap_pt: Decimal
    """Small paths this close together are one text run."""

    proximity_limit: Decimal
    """How far from a line a reading may sit and still be considered its annotation."""

    ambiguity_margin: Decimal
    """How much nearer the best candidate must be than the next before the choice counts as made.
    Within it the answer is no association — which is a result, not a failure to produce one.

    **It has to be wider than a pixel to mean anything.** Stored space is reached through integer
    image pixels, so at 150 dpi nothing on a 300-point page is finer than 1/625 of it. Two lines
    placed symmetrically either side of a reading do not come out equidistant — they cannot — so a
    margin below that resolution never fires, and the choice goes to whichever line happened to
    round nearer. Found by writing a test for the symmetric case and watching it decide."""

    def __post_init__(self) -> None:
        for name in (
            "line_minimum_pt",
            "glyph_maximum_pt",
            "glyph_gap_pt",
            "proximity_limit",
            "ambiguity_margin",
        ):
            value = getattr(self, name)
            if isinstance(value, float):
                raise TypeError(
                    f"{name} must be a Decimal, never a float. A float would make which line a "
                    "number belongs to depend on binary rounding, and the wrong answer would look "
                    "exactly like the right one."
                )
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @property
    def config_hash(self) -> str:
        """These settings as the text that becomes part of the association run's identity.

        Every number appears, because every one of them can change which line a reading is attached
        to. A hash that omitted one would let a re-association under a different value reuse the
        first run, and the rows would then record numbers that did not produce them — the provenance
        bug #487 fixed for dpi.
        """
        return (
            f"line>={self.line_minimum_pt};glyph<={self.glyph_maximum_pt};"
            f"gap<={self.glyph_gap_pt};near<={self.proximity_limit};"
            f"margin={self.ambiguity_margin}"
        )


@dataclass(frozen=True, slots=True)
class LocalizedOcrSettings:
    """Explicit geometry and crop bounds for vendor-region OCR.

    These do not type or rank a reading. They only state which outlined path clusters are worth an
    OCR crop; every other cluster remains a visible geometry refusal in ``plan_reads``. They have no
    defaults because crop selection is drawing-specific in exactly the way association is.
    """

    minimum_paths: int
    maximum_span: Decimal
    crop_margin_pt: Decimal

    def __post_init__(self) -> None:
        if isinstance(self.minimum_paths, bool) or not isinstance(self.minimum_paths, int):
            raise TypeError("minimum_paths must be an integer")
        if self.minimum_paths < 1:
            raise ValueError("minimum_paths must be greater than zero")
        for name in ("maximum_span", "crop_margin_pt"):
            value = getattr(self, name)
            if isinstance(value, float):
                raise TypeError(f"{name} must be a Decimal, never a float")
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValueError(f"{name} must be a finite Decimal")
            if value <= 0:
                raise ValueError(f"{name} must be greater than zero")

    @property
    def config_hash(self) -> str:
        return (
            f"minimum_paths={self.minimum_paths};maximum_span={self.maximum_span};"
            f"crop_margin_pt={self.crop_margin_pt}"
        )


def dimension_texts(
    items: Sequence[ReadItem], candidate_ids: Sequence[UUID]
) -> tuple[DimensionText, ...]:
    """Pair each reading with the candidate row that recorded it, in the shape `associate` takes.

    **Paired by position, and the position is load-bearing.** Both candidate writers iterate their
    input in order and return what they wrote in that order, so the *n*th row is the *n*th reading.
    That is a real coupling and it is checked rather than trusted: a length mismatch raises, because
    a silent misalignment would attach every reading to the line belonging to a different one — and
    every row would look perfectly well-formed.

    `leader_endpoint` is `None` throughout. A leader is the drafter saying in the drawing itself what
    a note labels, and it is the strongest signal `associate` has — but nothing in this repository
    detects one yet, and `DimensionText` documents `None` as "no leader was drawn" rather than "one
    was drawn and not found". Passing a guessed endpoint would be worse than passing none.
    """
    if len(items) != len(candidate_ids):
        raise ValueError(
            f"{len(items)} readings and {len(candidate_ids)} candidate rows cannot be paired. "
            "Pairing is by position, so a mismatch would attach readings to each other's lines."
        )
    return tuple(
        DimensionText(
            observation_id=candidate_id,
            extent=item.extent,
            rotation_degrees=item.rotation_degrees,
            leader_endpoint=None,
        )
        for item, candidate_id in zip(items, candidate_ids, strict=True)
    )
