"""Whose ink a label is: the vendor's, the reviewer's, or the vendor's under the reviewer's (#979).

**The reviewer's marks are baked into the vendor's drawing.** On every client set we have, GV's
reviewer corrected the vendor's sheet before it was snapped into the review file: red or blue
numbers written beside or over the vendor's, and yellow boxes painted over the vendor's black
numbers. Nothing in the file's structure separates the two — no layer, no tag, no annotation of its
own — and a vision reader shown the page copies the red number as readily as the black one. Two
readers agreeing does not help: both copy the same red number (Measurements 2026-10-07, E2).

**Colour is the one thing that tells them apart,** and this module turns it into a plain answer for
every word on the page and for any box a reading was located in:

- `VENDOR` — set in black or grey, with nothing coloured painted over it: the drawing's own ink;
- `GV` — set in colour: the reviewer's ink (the drawing is plotted in black, so anything coloured
  is treated as somebody's markup, including a vendor's logo colour — the safe side);
- `COVERED` — set in black or grey, but a coloured fill covers it: the vendor's number under the
  reviewer's yellow box. The file still holds the vendor's number; a picture does not show it.

**What it decides and what it never does.** It only ever withholds: a reading on `GV` or `COVERED`
ink goes to a person, and the reviewer's own text is kept as context ("reviewer wrote …"), never
as a value. It reads the same copy of the page as the text readers — the page's pasted drawings
only (`stamp_text.stamps_only`) — by the same rule for what is the drawing's ink (`drawing_ink`),
so it cannot disagree with them about colour. Geometry is in whole pixels, and the fill test is the
exact integer winding rule the GV-mark guard already uses; no float decides anything.

**Its limits, stated.** A reviewer who writes in black is not seen — in production there is no
reviewer's markup, because vendors send drawings nobody has reviewed yet. A drawing that is page
content rather than a pasted stamp shows no labels here, and a box on such a page answers `VENDOR`
because nothing coloured was found; the vendor-only render the readers see is built from the same
stamps, so the two agree about what is there. Red text drawn as glyph paths rather than font text
is found as coloured paths: it marks the box it touches as `GV`, or `COVERED` where a glyph's fill
happens to cover the box's centre — either way, never a value.

Source: Plan "Code finds the slots, AI reads them", Phase 2 · Verification: `tests/extraction/test_ink.py`
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pdfplumber

from extraction.reader import UnreadablePdf, pixel_placement
from extraction.stamp_text import (
    ColouredPath,
    PixelBox,
    coloured_paths,
    drawing_ink,
    pasted_stamps,
    stamps_only,
)

__all__ = ["InkAt", "InkClass", "InkLabel", "PageInk", "read_page_ink"]


class InkClass(StrEnum):
    """Whose ink a label, or the place a reading was located in, is."""

    VENDOR = "vendor"
    """Black or grey, with nothing coloured over it: the drawing's own ink."""

    GV = "gv"
    """Set in colour, or touched by a coloured stroke, coloured text or a pasted stamp: the
    reviewer's ink."""

    COVERED = "covered"
    """The vendor's black or grey ink with a coloured fill painted over it: present in the file,
    hidden in the picture."""


@dataclass(frozen=True, slots=True)
class InkLabel:
    """One word inside the page's pasted drawings, where it is, and whose ink it is."""

    text: str
    box: PixelBox
    """`(left, top, right, bottom)` in the page's pixels at the dpi the page was read at."""
    ink: InkClass


@dataclass(frozen=True, slots=True)
class InkAt:
    """What ink a located box holds, and what the reviewer wrote there, if anything."""

    ink: InkClass
    reviewer_text: str
    """The coloured words touching the box, in reading order, joined by spaces: context for the
    person ("reviewer wrote …"), never a value. Empty where none touches it."""


def _meet(first: PixelBox, second: PixelBox) -> bool:
    """Whether two boxes share any point, edges included."""
    return (
        first[0] <= second[2]
        and second[0] <= first[2]
        and first[1] <= second[3]
        and second[1] <= first[3]
    )


def _centre(box: PixelBox) -> tuple[int, int]:
    return ((box[0] + box[2]) // 2, (box[1] + box[3]) // 2)


@dataclass(frozen=True, slots=True)
class PageInk:
    """Every word on one page's pasted drawings with whose ink it is, and the coloured geometry
    that decides it, all in the page's pixels at `dpi`."""

    dpi: int
    labels: tuple[InkLabel, ...]
    paths: tuple[ColouredPath, ...]
    """Every line, rectangle, curve and fill the drawings draw in colour (`coloured_paths`)."""
    stamps: tuple[PixelBox, ...]
    """Every stamp pasted onto one of the drawings, whatever its colour (`pasted_stamps`)."""

    def at(self, box: PixelBox) -> InkAt:
        """Whose ink a located box holds, edges included.

        `COVERED` where a coloured fill covers the box's centre, or the box touches a word already
        found covered; otherwise `GV` where coloured text, a coloured path or a pasted stamp touches
        the box at all; otherwise `VENDOR`. The order matters only for the word used: a yellow box
        with a red number over it is "covered", and the red number is the reviewer's text.
        """
        if box[0] > box[2] or box[1] > box[3]:
            raise ValueError(
                f"box {box!r} has its right before its left or its bottom above its top"
            )
        reviewer = " ".join(
            label.text
            for label in self.labels
            if label.ink is InkClass.GV and _meet(label.box, box)
        )
        centre = _centre(box)
        if any(path.covers(centre) for path in self.paths) or any(
            label.ink is InkClass.COVERED and _meet(label.box, box) for label in self.labels
        ):
            return InkAt(InkClass.COVERED, reviewer)
        if (
            reviewer
            or any(_meet(stamp, box) for stamp in self.stamps)
            or any(path.meets(box) for path in self.paths)
        ):
            return InkAt(InkClass.GV, reviewer)
        return InkAt(InkClass.VENDOR, "")


def _word_box(word: dict[str, Any], place: Any) -> PixelBox:
    corners = [place(word["x0"], word["top"]), place(word["x1"], word["bottom"])]
    return (
        min(point.x for point in corners),
        min(point.y for point in corners),
        max(point.x for point in corners),
        max(point.y for point in corners),
    )


def read_page_ink(data: bytes, page_index: int, *, dpi: int) -> PageInk:
    """Whose ink every word on the page's pasted drawings is, in the page's pixels at `dpi`.

    Words are pdfplumber's, split where their colour changes, so a red correction set against a
    black number is its own word. Each is placed on the page's pixels as every text run is
    (`reader.pixel_placement`). Raises `UnreadablePdf` where the page's pasted drawings cannot be
    read; a caller then treats the page's ink as unknown, which can only hold readings back.
    """
    paths = coloured_paths(data, page_index, dpi=dpi)
    stamps = pasted_stamps(data, page_index, dpi=dpi)
    flattened = stamps_only(data, page_index)
    try:
        with pdfplumber.open(io.BytesIO(flattened)) as document:
            page = document.pages[page_index]
            place = pixel_placement(page, dpi)
            words = page.extract_words(extra_attrs=["non_stroking_color"])
            labels: list[InkLabel] = []
            for word in words:
                text = str(word.get("text", "")).strip()
                if not text:
                    continue
                box = _word_box(word, place)
                if not drawing_ink(word):
                    ink = InkClass.GV
                elif any(path.covers(_centre(box)) for path in paths):
                    ink = InkClass.COVERED
                else:
                    ink = InkClass.VENDOR
                labels.append(InkLabel(text=text, box=box, ink=ink))
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(
            f"page {page_index}'s pasted drawings could not be read for whose ink they hold: {error}"
        ) from error
    return PageInk(dpi=dpi, labels=tuple(labels), paths=paths, stamps=stamps)
