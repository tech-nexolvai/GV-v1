"""Opening a PDF and reporting what is on its pages (B2.1, #123).

The seam `extraction/manifest.py` has been waiting for. Its docstring names this module's absence
outright — *"The reader is not this module. Opening the PDF, repairing it and rendering its pages is
B2.1 (#123)… The seam between the two is `RawPage`"* — and until now nothing in the repository opened
a PDF at all, so several thousand lines of page classification, geometry and assembly sat downstream
of an input that did not exist.

**Everything here is measured; nothing here is a conclusion.** A page's size comes from its page
dictionary, its characters from its content stream, its lines from its graphics operators. What any
of it *means* — which page is a plan, which text is a dimension, which line it annotates — belongs to
the modules that already exist for those questions. This one reports.

Four things it has to get right, and three of them are things pdfplumber does that would otherwise be
wrong quietly.

**Rotated text reads backwards by default.** A dimension written bottom-to-top comes back as `489`
where the drawing says `984`. That is the worst failure available to a reader: a real number,
correctly parsed, and wrong — no downstream check can catch it, because 489 is a perfectly plausible
dimension. `char_dir_rotated="btt"` is what makes it right, and `tests/extraction/test_reader.py`
holds a regression on exactly that pair of numbers.

**Rotation is read, never inferred.** `DimensionText` requires it reported rather than guessed from
the box, because a box around `984` is wider than it is tall and a box around `8` is not, so guessing
would read single-digit dimensions as rotated. It comes from the character's own transformation
matrix through `pdfplumber.ctm.CTM`.

**A page with no text objects is reported as unreadable, not as empty.** Some CAD plot
configurations convert text to vector outlines, and scanned sheets never had text objects at all. In
both cases pdfplumber returns nothing, and an empty result would travel downstream as "this page has
no dimensions" — a false pass by omission. `RawPage.unreadable_reason` exists for this and says so in
plain English.

**No float reaches a `Decimal`.** pdfplumber returns floats; `Decimal(str(value))` is the only
conversion used, because `Decimal(0.1)` carries binary rounding into a coordinate that a reviewer will
later be shown as evidence.

**A label the words came apart from is never read as a number (#738, #726).** CAD text sets
`24 3/4"` as a `24`, a smaller `3` above a `4`, and an inch mark, and `extract_words` joins them in
page order into `2434"` — a well-formed dimension, 2,434 inches, exact and wrong. Measured on the
client's first set (`AI_Set_1`), 69 labels came back that way, every one a stacked fraction by eye;
others came back as pieces (`7'` of a sideways `7' -11"`, 84 inches). So three kinds of run are set
aside, with their place on the page and no text (`SetAsideReason`): a stacked fraction, a label
written on two lines, and a piece of a longer label. The stacked fractions are what the admin's rule
sends to a reviewer (#726).

**A stacked fraction is put back together only from where its own characters sit** (#786, #880):
from its word, where `extract_words` kept the stack in one, and otherwise from the characters of the
stacks set aside, round the bar the page draws between numerator and denominator. Either way it is
marked as stacked, so it is a reviewer's suggestion and never evidence; anything that does not fit
the shape exactly stays set aside.

What this module deliberately does not do: rasterise a page, run OCR, merge fragmented dimensions, or
associate text with lines. The last two need thresholds, and thresholds need real drawings (#274) —
`AGENTS.md` §9, *"a fixture invented today encodes today's guess as ground truth"*.
"""

from __future__ import annotations

import io
import math
import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import combinations, pairwise
from typing import Any, Final
from uuid import UUID

import pdfplumber
from pdfplumber.ctm import CTM

from evidence.coordinates import (
    SUPPORTED_ROTATIONS,
    ImagePoint,
    PageBox,
    PageTransform,
    PdfPoint,
    StoredPoint,
)
from evidence.polygon import Polygon
from extraction.geometry.containment import DimensionExtent
from extraction.manifest import RawPage
from units.dual import DUAL_TOKEN_RE

#: A feet-and-inches dimension a space splits into two words: `2' -5"`, `6' -0"`, `5' -5 1/2"`.
#:
#: **Read whole or not at all.** `extract_words` splits `2' -5"` at the space into `2'` and `-5"`,
#: and `2'` alone is a well-formed dimension — 24 inches, recorded exactly, for a label that says 29.
#: Measured on the client's drawings (formats phase 1): their stamps hold labels written this way,
#: and every one split. The inch half alone parses to nothing, so it was the foot half that would
#: have been believed. Joined here by the mechanism that joins `984 [38 3/4]`, for the same reason.
#: Typographic marks (`’`, `”`, `−`) are matched too, so the label is joined even where the unit
#: parser cannot yet value it — a joined label it refuses is safe; a split one it values is not.
FEET_INCH_TOKEN_RE = re.compile(
    r"\d+(?:\s+\d+/\d+)?\s*['’′]\s*[-−–]?\s*\d+(?:\s+\d+/\d+|/\d+)?\s*[\"”″]"
)

#: A whole number of inches and its fraction, which a space splits: `24 3/4"`, `10 1/4"`.
#:
#: **Read whole or not at all.** In text set large enough that its space is wider than
#: `extract_words`' three-point gap, `24 3/4"` comes back as `24` and `3/4"`, and `3/4"` alone is
#: three quarters of an inch, exact and wrong. Found converting CAD drawings (formats phase 2), where
#: every label is the size its drawing sets.
MIXED_INCH_TOKEN_RE = re.compile(r"\d+\s+\d+/\d+\s*[\"”″]")

#: The tokens `extract_words` splits that must be read whole: the dual dimension, feet-and-inches,
#: and inches with a fraction.
_WHOLE_TOKENS: Final = (DUAL_TOKEN_RE, FEET_INCH_TOKEN_RE, MIXED_INCH_TOKEN_RE)

__all__ = [
    "FRAGMENT_REACH",
    "INCH_DENOMINATORS",
    "STACK_REACH",
    "PageContents",
    "SetAsideLabel",
    "SetAsideReason",
    "TextItem",
    "UnreadablePdf",
    "read_page_contents",
    "read_pages",
]

#: How far apart two digits set one over the other may be, in the text's own height, and still be
#: taken for a numerator and its denominator rather than two lines of text.
#:
#: **A ratio of the text's height, so it does not depend on the drawing's scale.** Measured on both
#: client sets (#738): a numerator and its denominator sit 0.8 to 1.1 text heights apart, centre to
#: centre, and lines of notes 1.4 and more. The two error directions are not equal — a note line taken
#: for a fraction costs a reviewer one look, a fraction taken for a note line lets a numerator be read
#: as a whole number (`1"` for the `1` of `2 1/2"`) — so the reach sits above every fraction measured.
STACK_REACH: Final = 1.3

#: Two digits closer than this, centre to centre, are on one line, not stacked: overprinted text.
_SAME_LINE: Final = 0.5

#: How far along its own line, in the text's height, the inches may follow a feet number that stands
#: alone before the feet are taken to be half of a longer label.
#:
#: **What it catches, measured on both client sets (#738):** sideways labels such as `7' -11"` and
#: `3' - 6"`, which `extract_words` returns as `7'` and `-11"` about 1.5 heights apart, and which the
#: feet-and-inches join cannot reach because it joins only along upright lines. `7'` alone is 84
#: inches. The inch half (`-11"`) parses to nothing, so only the feet half needs this. The error
#: directions are unequal again: a label set aside is read by another route or a reviewer, half a
#: label read as a whole one is a confident wrong number.
FRAGMENT_REACH: Final = 2.0

#: Closer than this, in the text's height, another digit on a run's own line is part of the same
#: label: narrower than any space between words. Measured on `AI_Set_1`: `9.7"` with the `1` of
#: `19.7"` 0.26 heights before it, `10"` and `1"` cut from their labels at 0.26 and 0.34. Whole
#: labels in a chain of dimensions stand a height and more apart, and are left alone.
_TOUCHING: Final = 0.5

#: A feet number standing alone, with no inches: `7'`, `3’`, `2 1/2'`.
_BARE_FEET_RE: Final = re.compile(r"\d+(?:\s+\d+/\d+)?\s*['’′]")

#: A run that could carry a value: it holds a unit mark, a bracket, or millimetres. Only those are
#: tested as pieces of longer labels; a run with no unit is never given a value (`app/evidence`).
_MAY_CARRY_A_VALUE: Final = re.compile(r"['’′\"”″\[\]]|mm")

#: A numerator or a denominator that `extract_words` split from its stack and that carries a unit
#: mark: `1"` for the `1` of `2 1/2"`. One or two digits, because a denominator is at most 64. Only
#: such a word is tested for a digit stacked beside it — measured on `AI_Set_2`, a longer word with a
#: digit over it is a line of a tightly set note (`29-7/8"W X` over `16-1/2"H)`), 18 of 18.
_STACK_PIECE_RE: Final = re.compile(r"\d{1,2}\s*['’′\"”″]+")

#: How much two sizes or two baselines may differ, in the text's height, and still be the same. Text
#: set in one run shares them exactly; this absorbs only the arithmetic of reading them back.
_SAME: Final = 0.01

#: Read bottom-to-top for rotated runs. Without it pdfplumber returns the characters of a rotated
#: dimension in reverse — `984` as `489` — which is a plausible number and therefore undetectable
#: downstream. Measured, not assumed; the regression is in the test module.
_ROTATED_CHAR_DIRECTION: Final = "btt"

#: Plain English, for a reviewer rather than a log. Named causes, because "no text" tells somebody
#: nothing and "plotted as outlines or scanned" tells them what to go and check.
_NO_TEXT_REASON: Final = (
    "no text objects on this page: it was plotted with text converted to outlines, or it is a "
    "scanned image. Its dimensions cannot be read without OCR."
)

_ONLY_SET_ASIDE_REASON: Final = (
    "the only text on this page was set aside unread: stacked fractions, labels written on two "
    "lines, or pieces of longer labels, none of which can be read as a number on its own."
)


class UnreadablePdf(ValueError):
    """Raised when the file cannot be opened as a PDF at all.

    Distinct from a page that cannot be read. A page with no text is a page whose dimensions need
    another route; a document that will not parse has no pages to report, and `RawPage` refuses to
    invent a page size to fill its required fields. The two must not arrive as the same thing.
    """


@dataclass(frozen=True, slots=True)
class TextItem:
    """One run of text as read, with where it sits and which way it reads.

    Carries the text itself, unlike `DimensionText` — this is the reader's observation, and the
    number becomes an observation with an identity later. The geometry is already in stored space so
    that whatever mints that identity does not have to know about page transforms.
    """

    text: str
    extent: Polygon
    """Where it sits, normalised against the visible page. What geometry and evidence work in."""

    image_extent: tuple[ImagePoint, ...]
    """The same box in integer image pixels, which is what `observation_candidates` stores.

    Carried alongside rather than derived on demand: recovering it from `extent` needs the `dpi`,
    `media_box` and `crop_box` the transform was built from, and none of those are persisted
    anywhere. A candidate written without it would have geometry nothing could later place.
    """

    rotation_degrees: int
    upright: bool
    stacked: bool = False
    """Composed from a label set as a stacked fraction (#738): exact, and a reviewer's suggestion
    only — recorded with `STACKED_FRACTION_FLAG`, so no number of readers agrees it into evidence."""


class SetAsideReason(StrEnum):
    """Why a run of text is not read as a number."""

    STACKED_FRACTION = "stacked_fraction"
    """Set as a stacked fraction: `2434"` for `24 3/4"`. Its place sends a reviewer to it and makes
    every model crop showing it abstain (#726)."""

    TWO_LINES = "two_lines"
    """Millimetres written over their bracketed inches, read as one run: `[52835]` for `585` over
    `[23]`. Not a fraction, so it is left for the readers that join that pair (`extraction/ocr.py`)."""

    FRAGMENT = "fragment"
    """A piece of a longer label: `7'` of a sideways `7' -11"`, `9.7"` of `19.7"`."""


@dataclass(frozen=True, slots=True)
class SetAsideLabel:
    """Where a run of text set aside unread sits, and why. **No text, on purpose.**

    What `extract_words` made of it is not what the drawing says, so it is not kept anywhere a later
    step could read it as a number.
    """

    extent: Polygon
    image_extent: tuple[ImagePoint, ...]
    reason: SetAsideReason


@dataclass(frozen=True, slots=True)
class PageContents:
    """What one page holds: its text runs and its straight line segments.

    `unreadable_reason` mirrors `RawPage`'s, and for the same reason: empty tuples with no explanation
    would read as a page with nothing on it.
    """

    page_index: int
    texts: tuple[TextItem, ...]
    segments: tuple[DimensionExtent, ...]
    unreadable_reason: str | None = None
    set_aside: tuple[SetAsideLabel, ...] = ()
    """Runs not read as numbers, with why: never in `texts`, never a reading."""

    @property
    def readable(self) -> bool:
        """Whether text was found. `False` sends the page to OCR rather than to the vector lane."""
        return self.unreadable_reason is None


def _decimal(value: object) -> Decimal:
    """A pdfplumber number as an exact Decimal.

    Through `str`, always. `Decimal(0.1)` is `0.1000000000000000055511151231257827`, and a coordinate
    carrying that is a coordinate that will not round-trip — which matters because these become the
    polygon a reviewer is shown as the evidence for a verdict.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    raise TypeError(f"cannot read {value!r} as a coordinate")


def _box(values: object) -> PageBox:
    """A pdfplumber media or crop box as the four Decimals `PageTransform` demands."""
    if not isinstance(values, (tuple, list)) or len(values) != 4:
        raise UnreadablePdf(f"page box {values!r} is not four coordinates")
    left, bottom, right, top = (_decimal(value) for value in values)
    return (left, bottom, right, top)


def page_boxes_in_pdf_space(page: object) -> tuple[PageBox, PageBox]:
    """One pdfplumber page's media and crop boxes, in PDF space rather than pdfplumber's.

    **The two libraries this project reads with do not agree about where the page is, and mixing
    them silently discarded a whole drawing.** `pdfplumber` runs both boxes through its own
    `_invert_box`, which flips `y` about the media box's height so that its coordinates run top-down.
    Everything else here is bottom-up PDF space: an annotation's `/Rect`, a stamp's appearance
    matrix, the path geometry pypdfium2 returns.

    For the ordinary page whose media box starts at `(0, 0)` the inversion is the identity, which is
    why this went unnoticed. For a page whose media box is offset — a sheet cropped out of a larger
    set, which is exactly how the client's drawings are produced — it is not. Measured on one:

        media box   PDF space [321.8, 477.6, 852.1, 837.6]
                    pdfplumber [321.8, -477.6, 852.1, -117.5]
        annotation  /Rect     [341.8, 497.6, 832.1, 817.6]

    The rectangle and the crop box then share no `y` at all, `_visible_annotation_rect` found an
    empty intersection, and a 67 KB appearance stream carrying 2,165 strokes was refused with
    "annotation rectangle does not intersect the visible crop box". The sister drawing survived only
    because its offset was small enough that the flipped ranges still overlapped — by luck, not by
    correctness, and every coordinate derived from it was wrong by twice the offset.

    So the raw `/MediaBox` and `/CropBox` are read from the page dictionary, which is the one place
    they are stated in the space everything else uses. A page that declares no `/CropBox` inherits
    the media box, as the specification says.
    """
    attrs = getattr(page, "page_obj", None)
    attrs = getattr(attrs, "attrs", None)
    if not isinstance(attrs, dict) or "MediaBox" not in attrs:
        # Nothing to correct against. The inverted boxes are what pdfplumber offers, and on a page
        # whose media box starts at the origin they are identical to PDF space anyway.
        return _box(page.mediabox), _box(page.cropbox)  # type: ignore[attr-defined]
    media = _box(_resolved(attrs["MediaBox"]))
    crop = _box(_resolved(attrs["CropBox"])) if "CropBox" in attrs else media
    return media, crop


def _resolved(value: object) -> object:
    """Follow a PDF indirect reference to the object it names, if it is one."""
    resolve = getattr(value, "resolve", None)
    return resolve() if callable(resolve) else value


def _rotation(value: object) -> int:
    """A page's `/Rotate`, normalised to the four values everything downstream accepts.

    PDF permits any multiple of 90, including negatives and values past 360; `RawPage` and
    `PageTransform` both accept only `0, 90, 180, 270`. Normalising here rather than refusing keeps a
    `/Rotate -90` page readable, which is a real thing plotters emit.
    """
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnreadablePdf(f"page rotation {value!r} is not an integer")
    normalised = value % 360
    if normalised not in SUPPORTED_ROTATIONS:
        raise UnreadablePdf(
            f"page rotation {value!r} is not a quarter turn. Every later stage assumes one of "
            "0, 90, 180 or 270, and a page rotated by anything else would have its geometry "
            "silently misplaced rather than refused."
        )
    return normalised


def _content_bytes(page: Any) -> bytes:
    """The page's content stream, which `build_manifest` hashes to identify the page.

    Best-effort by design: the hash exists so a re-read of the same page is recognisable, and a page
    whose stream cannot be reached still has a size, a rotation and a character count worth
    reporting. `b""` is explicitly allowed by `RawPage`.
    """
    try:
        contents = page.page_obj.get_data()
    except Exception:  # noqa: BLE001 - any failure to reach the stream is the same answer
        # Deliberately broad. The hash exists so a re-read of the same page is recognisable, and a
        # page whose stream cannot be reached still has a size, a rotation and a character count
        # worth reporting. Enumerating the ways a malformed stream can fail would be a list that a
        # new pdfminer release lengthens.
        return b""
    return contents if isinstance(contents, bytes) else b""


def read_pages(data: bytes) -> tuple[RawPage, ...]:
    """Every page of a PDF, as the observations `build_manifest` turns into a manifest.

    Reports rather than judges: `vector_character_count` is a count of characters found, and whether
    that is *enough* to call the page text-bearing is `build_manifest`'s decision, made against a
    `minimum_vector_characters` its caller has to supply.

    A page with no characters carries `unreadable_reason` and a count of zero — `RawPage` cross-checks
    that pair, and the pairing is the point: the page is not empty, it is unread.
    """
    if not isinstance(data, bytes):
        raise TypeError("data must be the PDF's bytes")
    if not data:
        raise UnreadablePdf("the file is empty")

    pages: list[RawPage] = []
    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            for index, page in enumerate(document.pages):
                characters = len(page.chars)
                media_box, crop_box = page_boxes_in_pdf_space(page)
                pages.append(
                    RawPage(
                        index=index,
                        content=_content_bytes(page),
                        width_pt=_decimal(page.width),
                        height_pt=_decimal(page.height),
                        rotation=_rotation(page.rotation),
                        vector_character_count=characters,
                        unreadable_reason=None if characters else _NO_TEXT_REASON,
                        # Read here because here is where the page dictionary is open. Rebuilding
                        # the transform later needs both boxes, and nothing downstream can recover
                        # them from the page's size alone (#530).
                        media_box=media_box,
                        crop_box=crop_box,
                    )
                )
    except UnreadablePdf:
        raise
    except Exception as error:
        # Any parse failure is one answer: this is not a document we can read. Deliberately not a
        # partial list of the pages reached before it failed — a truncated page list is a document
        # that looks shorter than it is, and every later stage would trust the count.
        raise UnreadablePdf(f"the file could not be read as a PDF: {error}") from error

    if not pages:
        raise UnreadablePdf("the file parsed as a PDF but contains no pages")
    return tuple(pages)


def read_page_contents(
    data: bytes,
    page_index: int,
    *,
    document_version_id: UUID,
    dpi: int,
    keep_char: Callable[[dict[str, Any]], bool] | None = None,
    keep_path: Callable[[dict[str, Any]], bool] | None = None,
) -> PageContents:
    """The text runs and straight segments on one page, in stored coordinates.

    `keep_char`, where given, is asked of every character before any word is formed, and a character
    it refuses is not read at all. `extraction/stamp_text.py` uses it to leave out coloured text.
    `keep_path`, where given, is asked the same of every line, rectangle and curve: a path it refuses
    is neither a segment nor a fraction's bar (#880). `extraction/stamp_text.py` uses it to leave out
    coloured paths.

    `dpi` has no default. Stored coordinates are normalised against the visible crop box and reached
    through integer image space, so the resolution decides how much precision survives the trip — a
    default here would silently pick that for every caller. `evidence/coordinates.py` documents the
    round trip as lossy by up to one pixel per axis.

    Both tuples come back in page order, and the segments are every straight line the page draws:
    line objects, rectangle edges, and the point pairs of each path. A dimension line may be any of
    the three depending on what plotted the sheet, so filtering them by what looks like a dimension
    is the association step's job, not this one's — a reader that dropped candidate geometry would
    make a missing dimension look like a drawing that never showed it.
    """
    if not isinstance(dpi, bool) and isinstance(dpi, int) and dpi > 0:
        pass
    else:
        raise ValueError("dpi must be a positive integer; stored coordinates depend on it")

    try:
        with pdfplumber.open(io.BytesIO(data)) as document:
            try:
                page = document.pages[page_index]
            except IndexError as error:
                raise UnreadablePdf(
                    f"page {page_index} is beyond the {len(document.pages)} pages in this document"
                ) from error

            media_box, crop_box = page_boxes_in_pdf_space(page)
            transform = PageTransform(
                dpi=dpi,
                rotation=_rotation(page.rotation),
                media_box=media_box,
                crop_box=crop_box,
            )
            height = _decimal(page.height)
            if keep_char is not None or keep_path is not None:
                page = page.filter(lambda obj: _kept(obj, keep_char, keep_path))
            # **Text printed twice in one place is read once.** Measured on `AI_Set_2`: a label drawn
            # twice over itself came back as `22''`, 22 inches, for a `2' - 6"`. pdfplumber's own
            # de-duplication removes a character only where an identical one, same font and size,
            # sits within a point of it — a second copy, never a second character.
            page = page.dedupe_chars()

            words = page.extract_words(return_chars=True, char_dir_rotated=_ROTATED_CHAR_DIRECTION)
            digits = _DigitGrid(page.chars)
            # **Stacked fractions come out before anything is joined or read** (#738). A `2434"` left
            # in would be a reading; a `24` beside it could be joined into a dual token.
            words, set_aside = _set_aside_stacked(words, digits)
            # **Then what can be read whole from its own characters is** (#738): a stacked fraction
            # whose whole, numerator and denominator are where a stack puts them becomes `24 3/4"`,
            # marked as stacked; millimetres written over their bracketed inches become `610 [24]`.
            # Anything that does not fit those shapes exactly stays set aside.
            words, set_aside = _read_what_composes(words, set_aside, digits)
            # **Then millimetres over inches that a space broke in two are read as one** (#904):
            # both halves of `3048` over `[120 1/4]`, with the space where the page has it.
            words, set_aside = _rejoin_two_lines(words, set_aside, digits, page.chars)
            # **Then the stacks the words came apart from are put back together** (#880), from where
            # their characters sit around the bar the page draws between numerator and denominator:
            # `152` and `1"` for a sideways `15 1/2"`. Held to the rules a reading put together from
            # its pieces is held to (#848), and still marked as stacked.
            words, set_aside = _compose_split_stacks(
                words, set_aside, digits, page.chars, (*page.lines, *page.rects, *page.curves)
            )
            # **Then labels side by side that `extract_words` ran together are read apart** (#904):
            # `4697` over `[2][4]` is `46 [2]` and `97 [4]`, each digit going with the bracket it
            # stands over.
            words = _read_side_by_side(words, digits, page.chars)
            # **Dual tokens are read whole, before the words they are made of.** `984 [38 3/4]` is
            # one reading of one dimension, and `extract_words` splits it at the spaces into `984`,
            # `[38` and `3/4]` — three fragments, none of which is a dimension. That splitting is the
            # same behaviour that once recorded `984 mm` as 984 inches. Recovering the token here
            # means the corroboration lane sees the drawing's own second reading (#528); leaving the
            # fragments in as well would record one dimension four times.
            dual = _dual_tokens(page, words, transform, height, document_version_id, page_index)
            runs = [merged for _, _, merged in dual] + [
                word for word in words if not _inside_any(word, dual)
            ]
            # **Then whatever is a piece of a longer label**, once the joins above have put back
            # together every label they can. `5' - 5"` is read whole; `7'` of a sideways `7' -11"`,
            # which no join reached, is set aside rather than read as 84 inches.
            kept_runs: list[dict[str, Any]] = []
            partners: set[int] = set()
            may_carry = [bool(_MAY_CARRY_A_VALUE.search(str(run.get("text", "")))) for run in runs]
            fragments = [
                carries and _is_fragment(run, digits, partners)
                for run, carries in zip(runs, may_carry, strict=True)
            ]
            for run, carries, fragment in zip(runs, may_carry, fragments, strict=True):
                # The other half of a feet number set aside goes with it: `0"` of a `2' - 0"` that
                # no join reached is no more a label than the `2'` is.
                other_half = carries and any(
                    id(char) in partners for char in run.get("chars") or ()
                )
                if fragment or other_half:
                    set_aside.append((run, SetAsideReason.FRAGMENT))
                else:
                    kept_runs.append(run)
            texts = tuple(
                item
                for run in kept_runs
                if (item := _text_item(run, transform, height, document_version_id, page_index))
                is not None
            )
            labels = tuple(
                SetAsideLabel(extent=item.extent, image_extent=item.image_extent, reason=reason)
                for run, reason in set_aside
                if (item := _text_item(run, transform, height, document_version_id, page_index))
                is not None
            )
            segments = _segments(page, transform, height, document_version_id, page_index)
    except UnreadablePdf:
        raise
    except Exception as error:
        raise UnreadablePdf(f"page {page_index} could not be read: {error}") from error

    return PageContents(
        page_index=page_index,
        texts=texts,
        segments=segments,
        unreadable_reason=(
            None if texts else (_ONLY_SET_ASIDE_REASON if labels else _NO_TEXT_REASON)
        ),
        set_aside=labels,
    )


#: A digit's place in its own text frame: `(along_low, along_high, across_low, across_high, up)`.
#: Along is the reading direction, across is up the line; `up` names the direction, rounded, so two
#: digits are compared only when they read the same way.
_Frame = tuple[float, float, float, float, tuple[float, float]]


def _frame(char: dict[str, Any]) -> _Frame | None:
    """Where one character sits, measured along and across its own line, whichever way it turns.

    The character's matrix says which way is up for it; its box is projected onto that and onto the
    reading direction. A sideways `7 3/8"` is then measured exactly as an upright one.
    """
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return None
    up_x, up_y = float(matrix[2]), float(matrix[3])
    length = math.hypot(up_x, up_y)
    if length == 0:
        return None
    up_x, up_y = up_x / length, up_y / length
    corners = [(float(char[x]), float(char[y])) for x in ("x0", "x1") for y in ("y0", "y1")]
    across = [x * up_x + y * up_y for x, y in corners]
    along = [x * up_y - y * up_x for x, y in corners]
    return min(along), max(along), min(across), max(across), (round(up_x, 3), round(up_y, 3))


def _digit_frames(chars: list[dict[str, Any]]) -> list[tuple[dict[str, Any], _Frame | None]]:
    return [(char, _frame(char)) for char in chars if str(char.get("text", "")).isdigit()]


class _DigitGrid:
    """Every digit on the page, in square cells three times the tallest digit wide, so a character's
    neighbours are found in the nine cells round it rather than by comparing it with every digit on
    the sheet. Three heights covers both reaches (`STACK_REACH`, `FRAGMENT_REACH`) from any digit.
    """

    def __init__(self, chars: list[dict[str, Any]]) -> None:
        measured = [(char, frame) for char, frame in _digit_frames(chars) if frame is not None]
        tallest = max((frame[3] - frame[2] for _, frame in measured), default=0.0)
        self._cell = 3 * tallest or 1.0
        self._cells: dict[tuple[int, int], list[tuple[int, _Frame]]] = defaultdict(list)
        self._boxes: dict[int, tuple[float, float, float, float]] = {}
        for char, frame in measured:
            self._cells[self._key(char)].append((id(char), frame))
            self._boxes[id(char)] = _page_box(char)

    def _key(self, char: dict[str, Any]) -> tuple[int, int]:
        centre_x = (float(char["x0"]) + float(char["x1"])) / 2
        centre_y = (float(char["y0"]) + float(char["y1"])) / 2
        return int(centre_x // self._cell), int(centre_y // self._cell)

    def near(self, char: dict[str, Any]) -> list[tuple[int, _Frame]]:
        """The digits in the nine cells round `char`, each with the identity of its character."""
        column, row = self._key(char)
        return [
            entry
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for entry in self._cells.get((column + dx, row + dy), ())
        ]

    def printed_over(self, word: dict[str, Any]) -> bool:
        """Whether a digit that is not the word's own overlaps it on the page, whichever way it reads:
        two labels printed one over the other, which no reading of either can be trusted from."""
        chars = list(word.get("chars") or ())
        if not chars:
            return False
        own = {id(char) for char in chars}
        boxes = [_page_box(char) for char in chars]
        left = min(box[0] for box in boxes)
        right = max(box[1] for box in boxes)
        bottom = min(box[2] for box in boxes)
        top = max(box[3] for box in boxes)
        for char in chars:
            for other_id, _ in self.near(char):
                if other_id in own:
                    continue
                x0, x1, y0, y1 = self._boxes[other_id]
                if min(right, x1) > max(left, x0) and min(top, y1) > max(bottom, y0):
                    return True
        return False


def _page_box(char: dict[str, Any]) -> tuple[float, float, float, float]:
    """A character's `(x0, x1, y0, y1)` on the page, y upward."""
    return float(char["x0"]), float(char["x1"]), float(char["y0"]), float(char["y1"])


def _mixed(frames: list[_Frame | None]) -> bool:
    """Whether a word's digits are not all one size, on one baseline, reading one way."""
    if any(frame is None for frame in frames):
        return True
    known = [frame for frame in frames if frame is not None]
    heights = [frame[3] - frame[2] for frame in known]
    bases = [frame[2] for frame in known]
    tolerance = _SAME * max(heights)
    return (
        max(heights) - min(heights) > tolerance
        or max(bases) - min(bases) > tolerance
        or len({frame[4] for frame in known}) > 1
    )


def _one_size(frames: list[_Frame | None]) -> bool:
    known = [frame for frame in frames if frame is not None]
    heights = [frame[3] - frame[2] for frame in known]
    return max(heights) - min(heights) <= _SAME * max(heights)


def _stacked_over(mine: _Frame, other: _Frame) -> bool:
    """Whether `other` is set directly over or under `mine`, as a numerator is over a denominator."""
    if mine[4] != other[4]:
        return False
    overlap = min(mine[1], other[1]) - max(mine[0], other[0])
    if overlap < 0.5 * min(mine[1] - mine[0], other[1] - other[0]):
        return False
    height = max(mine[3] - mine[2], other[3] - other[2])
    offset = abs((mine[2] + mine[3]) - (other[2] + other[3])) / 2 / height
    return _SAME_LINE <= offset <= STACK_REACH


def _set_aside_stacked(
    words: list[dict[str, Any]], digits: _DigitGrid
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], SetAsideReason]]]:
    """`(kept, set_aside)`: the words that may be read, and those set on more than one line.

    Two tests, both on digits only — an inch mark is routinely set smaller than its number.

    - **Inside the word**: its digits are not one size on one baseline. `2434"` for `24 3/4"`. A
      stacked fraction, unless its digits are all one size and it holds a bracket: then it is
      millimetres written over their bracketed inches (`[52835]` for `585` over `[23]`), measured on
      `AI_Set_1`.
    - **Beside it**: the word is a lone numerator or denominator with its mark (`_STACK_PIECE_RE`)
      and a digit of another word sits directly over or under one of its own, within
      `STACK_REACH`. `1"` for the numerator of `2 1/2"`, when `extract_words` split the stack.
    """
    kept: list[dict[str, Any]] = []
    set_aside: list[tuple[dict[str, Any], SetAsideReason]] = []
    for word in words:
        text = str(word.get("text", ""))
        own = _digit_frames(list(word.get("chars") or ()))
        if not own:
            kept.append(word)
            continue
        frames = [frame for _, frame in own]
        if _mixed(frames):
            two_lines = (
                not any(frame is None for frame in frames)
                and _one_size(frames)
                and ("[" in text or "]" in text)
            )
            reason = SetAsideReason.TWO_LINES if two_lines else SetAsideReason.STACKED_FRACTION
            set_aside.append((word, reason))
            continue
        if not _STACK_PIECE_RE.fullmatch(text):
            kept.append(word)
            continue
        own_ids = {id(char) for char, _ in own}
        beside = any(
            other_id not in own_ids and _stacked_over(frame, other)
            for char, frame in own
            if frame is not None
            for other_id, other in digits.near(char)
        )
        if beside:
            set_aside.append((word, SetAsideReason.STACKED_FRACTION))
        else:
            kept.append(word)
    return kept, set_aside


#: The denominators an inch fraction is written over. A stack whose bottom is anything else is not
#: read as a fraction of an inch. Public because the reader that puts a stacked fraction together
#: from its pieces holds it to the same rule (#848).
INCH_DENOMINATORS: Final = frozenset({2, 4, 8, 16, 32, 64})

#: The millimetres line of a dual dimension written one over the other.
_MILLIMETRES_RE: Final = re.compile(r"\d+(?:\.\d+)?")

#: Bracketed inches as a dual dimension put together from its pieces may hold them (#904, #909): a
#: whole number, a fraction, or a whole number and a fraction — `[32]`, `[3/4]`, `[120 1/4]`.
_PIECED_INCHES_RE: Final = re.compile(r"\[(?:(\d+)|(?:(\d+) )?(\d+)/(\d+))\]")

#: Two or more bracketed groups set back to back: the inches of labels side by side that
#: `extract_words` ran into one word, `[2][4]` (#904).
_BRACKET_GROUPS_RE: Final = re.compile(r"(?:\[[^\[\]]+\])(?:\[[^\[\]]+\])+")

#: A run of digits and nothing else: millimetres as a word holds them.
_DIGITS_RE: Final = re.compile(r"\d+")

#: An inch or foot mark, as fonts set them.
_UNIT_MARK_RE: Final = re.compile(r"['’′\"”″]")

#: A character with where it sits, as `_frame` measures it.
_Framed = tuple[dict[str, Any], _Frame]


def _two_rows(framed: list[_Framed]) -> tuple[list[_Framed], list[_Framed]] | None:
    """`(upper, lower)`: characters on exactly two lines, each in reading order, or `None`.

    Their centres across the line are sorted and split at the widest gap. That gap must be at
    least half a text height and every other gap less, so two lines are found only where there are
    two lines and not three, or one with a character a little out of place.
    """
    if len(framed) < 2:
        return None
    height = min(frame[3] - frame[2] for _, frame in framed)
    ordered = sorted(framed, key=lambda entry: (entry[1][2] + entry[1][3]) / 2)
    centres = [(frame[2] + frame[3]) / 2 for _, frame in ordered]
    gaps = [above - below for below, above in pairwise(centres)]
    widest = max(range(len(gaps)), key=lambda index: gaps[index])
    if gaps[widest] < 0.5 * height or any(
        gap >= 0.5 * height for index, gap in enumerate(gaps) if index != widest
    ):
        return None
    lower, upper = ordered[: widest + 1], ordered[widest + 1 :]
    return (
        sorted(upper, key=lambda entry: entry[1][0]),
        sorted(lower, key=lambda entry: entry[1][0]),
    )


def _framed(word: dict[str, Any]) -> list[_Framed] | None:
    """A word's printed characters with their places, or `None` if any cannot be placed or they do
    not all read the same way."""
    framed: list[_Framed] = []
    for char in word.get("chars") or ():
        if not str(char.get("text", "")).strip():
            continue
        frame = _frame(char)
        if frame is None:
            return None
        framed.append((char, frame))
    if not framed or len({frame[4] for _, frame in framed}) > 1:
        return None
    return framed


def _text_of(entries: list[_Framed]) -> str:
    return "".join(str(char["text"]) for char, _ in entries)


def _compose_fraction(word: dict[str, Any]) -> str | None:
    """A stacked fraction's label as one line — `24 3/4"`, `2'-10 1/2"`, `3/4"` — or `None`.

    Read from where the characters sit, never from their order in the word. The fraction's digits
    are those smaller than the label's tallest (or all of them, for a fraction standing alone), and
    they must lie on exactly two lines, one over the other along the label: the upper line is the
    numerator, the lower the denominator. The rest of the label — the whole number, a feet part, the
    inch mark — keeps its order, with the fraction put where it stands. Refused unless the
    numerator is less than the denominator and the denominator is a fraction of an inch.
    """
    framed = _framed(word)
    if framed is None:
        return None
    digits = [entry for entry in framed if str(entry[0]["text"]).isdigit()]
    if len(digits) < 2:
        return None
    tallest = max(frame[3] - frame[2] for _, frame in digits)
    small = [entry for entry in digits if entry[1][3] - entry[1][2] < tallest * (1 - _SAME)]
    if not small:
        small = digits  # a fraction standing alone: both its lines are the label's size
    rows = _two_rows(small)
    if rows is None:
        return None
    upper, lower = rows
    overlap = min(max(entry[1][1] for entry in upper), max(entry[1][1] for entry in lower)) - max(
        min(entry[1][0] for entry in upper), min(entry[1][0] for entry in lower)
    )
    if overlap <= 0:
        return None  # the two lines are side by side, not one over the other
    numerator, denominator = int(_text_of(upper)), int(_text_of(lower))
    if denominator not in INCH_DENOMINATORS or not 0 < numerator < denominator:
        return None

    stack = {id(char) for char, _ in small}
    rest = [entry for entry in framed if id(entry[0]) not in stack]
    starts = min(frame[0] for _, frame in small)
    ends = max(frame[1] for _, frame in small)
    before = [entry for entry in rest if (entry[1][0] + entry[1][1]) / 2 < starts]
    after = [entry for entry in rest if (entry[1][0] + entry[1][1]) / 2 > ends]
    if len(before) + len(after) != len(rest):
        return None  # something of the label's own stands inside the stack
    if any(str(char["text"]).isdigit() for char, _ in after):
        return None  # a whole number reads before its fraction, never after
    before.sort(key=lambda entry: entry[1][0])
    after.sort(key=lambda entry: entry[1][0])
    # **One label, touching end to end.** A `4"` beside a stacked `3/4"` is a label of its own;
    # glued into the same word, it would be read as `4 3/4"`. Every gap along the label — inside the
    # whole number, up to the stack, and from the stack to the mark — must be a touch, not a space.
    reach = _TOUCHING * tallest
    edges = [(frame[0], frame[1]) for _, frame in before] + [(starts, ends)]
    edges += [(frame[0], frame[1]) for _, frame in after]
    if any(following[0] - leading[1] > reach for leading, following in pairwise(edges)):
        return None
    text = f"{_text_of(before)} {numerator}/{denominator}{_text_of(after)}".strip()
    # A bare `3/8` inside a note says nothing of its unit; only a label with its mark is suggested.
    return text if _UNIT_MARK_RE.search(text) else None


def _compose_two_lines(word: dict[str, Any]) -> str | None:
    """Millimetres written over their bracketed inches, as the one dual token they are — `610 [24]`
    — or `None`. Either line may be the upper one; the token is written millimetres first.

    **Held to `_checked_dual`, as every dual dimension put together from pieces is** (#909). Each
    row is read as its characters in order, and a space is not a character the word holds
    (`extract_words` breaks a word at one). So a file that sets the space in `[120 1/4]` as a gap
    rather than a space character gives the row `[1201/4]`: 300 1/4 inches, exact and wrong. The
    guard refuses it, because 1201 is not below 4. The rows' text is otherwise read as it was;
    anything the guard refuses stays set aside as two lines.
    """
    framed = _framed(word)
    if framed is None:
        return None
    rows = _two_rows(framed)
    if rows is None:
        return None
    first, second = (_text_of(row) for row in rows)
    return _checked_dual(first, second) or _checked_dual(second, first)


def _checked_dual(millimetres: str, inches: str) -> str | None:
    """`millimetres` and `inches` as one dual token, `3048 [120 1/4]`, or `None`.

    **The guard on every dual dimension put together from pieces**: the joins #904 added, and the
    single-word two-line composer (`_compose_two_lines`, #909). Such a token is built from where its
    characters sit, and where a space or a split was misjudged the result can still parse:
    `[120 1/4]` read without its space is `[1201/4]`, 300 1/4 inches, exact and wrong. So the rules
    a stacked fraction put together from its pieces is held to (#848) are applied: the millimetres
    are one number; the inches are a whole number, a fraction or both; a fraction's denominator is
    in `INCH_DENOMINATORS` with the numerator below it; and no piece of more than one digit starts
    with a zero.
    """
    match = _PIECED_INCHES_RE.fullmatch(inches)
    if not _MILLIMETRES_RE.fullmatch(millimetres) or match is None:
        return None
    pieces = [millimetres, *(piece for piece in match.groups() if piece is not None)]
    if any(len(piece) > 1 and piece.startswith("0") for piece in pieces):
        return None
    if match.group(4) is not None:
        numerator, denominator = int(match.group(3)), int(match.group(4))
        if denominator not in INCH_DENOMINATORS or not 0 < numerator < denominator:
            return None
    token = f"{millimetres} {inches}"
    return token if DUAL_TOKEN_RE.fullmatch(token) else None


def _read_what_composes(
    words: list[dict[str, Any]],
    set_aside: list[tuple[dict[str, Any], SetAsideReason]],
    digits: _DigitGrid,
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], SetAsideReason]]]:
    """`(words, set_aside)` with every set-aside label that composes moved back among the words.

    Never a label another label is printed over (measured on `AI_Set_1`: a sideways `4 3/4"` drawn
    through an upright `ϕ 1 3/4"` composes perfectly and is not what anyone meant).
    """
    kept = list(words)
    still: list[tuple[dict[str, Any], SetAsideReason]] = []
    for word, reason in set_aside:
        composed: str | None = None
        if digits.printed_over(word):
            pass
        elif reason is SetAsideReason.STACKED_FRACTION:
            composed = _compose_fraction(word)
        elif reason is SetAsideReason.TWO_LINES:
            composed = _compose_two_lines(word)
        if composed is None:
            still.append((word, reason))
            continue
        kept.append(
            {**word, "text": composed, "stacked": reason is SetAsideReason.STACKED_FRACTION}
        )
    return kept, still


def _tallest_digit(entries: list[_Framed]) -> float:
    return max(
        (_height(frame) for char, frame in entries if str(char["text"]).isdigit()), default=0.0
    )


def _touching_run(row: list[_Framed], tallest: float) -> bool:
    """Whether a row's characters run along their line with no gap wider than `_TOUCHING` of
    `tallest`: one label's characters, not two labels' (a space between words of one label is
    narrower than that in the client's text)."""
    ordered = sorted(row, key=lambda entry: entry[1][0])
    reached = ordered[0][1][1]
    for _, frame in ordered[1:]:
        if frame[0] - reached > _TOUCHING * tallest:
            return False
        reached = max(reached, frame[1])
    return True


def _alone(label: list[_Framed], printed: list[_Framed]) -> bool:
    """Whether no character of the page outside `label` reads its way, is centred across within it,
    and touches it along the line or stands inside it: the refusal `_stack_on_bar` makes of a
    neighbour, which may be a piece of the label the pieces found are missing."""
    own = {id(char) for char, _ in label}
    up = label[0][1][4]
    foot, head = min(frame[2] for _, frame in label), max(frame[3] for _, frame in label)
    start, end = min(frame[0] for _, frame in label), max(frame[1] for _, frame in label)
    tallest = _tallest_digit(label)
    for char, frame in printed:
        if id(char) in own or frame[4] != up or not foot <= _across_middle(frame) <= head:
            continue
        reach = _TOUCHING * max(tallest, _height(frame) if str(char["text"]).isdigit() else 0)
        if frame[1] >= start - reach and frame[0] <= end + reach:
            return False
    return True


def _printed(chars: list[dict[str, Any]]) -> list[_Framed]:
    """Every character of the page that prints, with where it sits."""
    return [
        (char, frame)
        for char in chars
        if str(char.get("text", "")).strip() and (frame := _frame(char)) is not None
    ]


def _row_text(row: list[_Framed], other: list[_Framed], spaces: list[_Framed]) -> str | None:
    """One row's characters in reading order, with a space wherever the page sets a space
    character between two of them on this row; or `None` where a space between them sits where
    either row could own it.

    The space is the page's own character, never a gap judged by its width: in the client's text a
    space is narrower than `_TOUCHING`, so no width tells it from two characters set close.
    """
    low, high = min(frame[2] for _, frame in row), max(frame[3] for _, frame in row)
    other_low, other_high = min(frame[2] for _, frame in other), max(frame[3] for _, frame in other)
    text = str(row[0][0]["text"])
    for (_, before), (char, after) in pairwise(row):
        between = [
            frame
            for _, frame in spaces
            if frame[4] == before[4]
            and _along_middle(before) < _along_middle(frame) < _along_middle(after)
            and low <= _across_middle(frame) <= high
        ]
        if any(other_low <= _across_middle(frame) <= other_high for frame in between):
            return None
        text += (" " if between else "") + str(char["text"])
    return text


def _one_run_split(first: list[_Framed], second: list[_Framed]) -> bool:
    """Whether two words set aside as two lines are pieces of one run: reading the same way,
    touching along their line (`_TOUCHING`), and on the same two rows (`_two_rows`)."""
    if first[0][1][4] != second[0][1][4]:
        return False
    tallest = _tallest_digit([*first, *second])
    gap = max(min(frame[0] for _, frame in first), min(frame[0] for _, frame in second)) - min(
        max(frame[1] for _, frame in first), max(frame[1] for _, frame in second)
    )
    return gap <= _TOUCHING * tallest and _two_rows([*first, *second]) is not None


def _rejoin_two_lines(
    words: list[dict[str, Any]],
    set_aside: list[tuple[dict[str, Any], SetAsideReason]],
    digits: _DigitGrid,
    chars: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], SetAsideReason]]]:
    """`(words, set_aside)` with each label written as millimetres over bracketed inches that
    `extract_words` broke at a space put back together, as the one dual token it is (#904).

    **Why it was never read.** `extract_words` reads two rows set close as one word, their
    characters interleaved, and breaks a word at a space. Measured on `AI_Set_1` page 3, labels
    written as four digits over bracketed inches with a space in them, such as `3048` over
    `[120 1/4]`, came back as two words, each holding pieces of both rows: the space in the inches
    broke both rows in two. Neither half composes (`_compose_two_lines`), so both were set aside,
    and a label printed clearly reached a person blank.

    **So the halves are read as one.** Words still set aside as two lines that touch along their
    line and lie on the same two rows (`_one_run_split`) are pieces of one run. Each holds
    characters of both rows on its own side of the break, so the two rows stand one over the
    other. Each row is read
    from all their characters, with the space where the page sets its space character
    (`_row_text`), and the run is read as millimetres over bracketed inches only if it passes
    `_checked_dual`. Anything else leaves every piece set aside, as before: a space in the
    millimetres; a space either row could own; a row with a gap in it wider than `_TOUCHING`
    (`_touching_run`), which is two labels' text, or one with characters missing; a character of
    the page touching the run that is not part of it (`_alone`), which may be a piece of it the
    halves are missing; a run another label is printed over; inches that are not an inch value.
    Measured on `AI_Set_1`, the gap refuses two runs: millimetres with two characters' room missing
    from the middle of them, and the millimetres of two labels read as one six-digit number.
    The token is not marked stacked: it is not a fraction set as a stack.
    """
    pieces = [
        (index, framed)
        for index, (word, reason) in enumerate(set_aside)
        if reason is SetAsideReason.TWO_LINES and (framed := _framed(word)) is not None
    ]
    if len(pieces) < 2:
        return words, set_aside
    group = list(range(len(pieces)))

    def root(member: int) -> int:
        while group[member] != member:
            member = group[member]
        return member

    for first, second in combinations(range(len(pieces)), 2):
        if _one_run_split(pieces[first][1], pieces[second][1]):
            group[root(second)] = root(first)
    runs: dict[int, list[int]] = defaultdict(list)
    for member in range(len(pieces)):
        runs[root(member)].append(member)

    spaces = [
        (char, frame)
        for char in chars
        if str(char.get("text", "")).isspace() and (frame := _frame(char)) is not None
    ]
    printed = _printed(chars)
    kept = list(words)
    joined: set[int] = set()
    for members in runs.values():
        if len(members) < 2:
            continue
        framed = [entry for member in members for entry in pieces[member][1]]
        own = [char for char, _ in framed]
        rows = _two_rows(framed)
        if rows is None or digits.printed_over({"chars": own}):
            continue
        upper, lower = rows
        tallest = _tallest_digit(framed)
        if not (
            _touching_run(upper, tallest)
            and _touching_run(lower, tallest)
            and _alone(framed, printed)
        ):
            continue
        above, below = _row_text(upper, lower, spaces), _row_text(lower, upper, spaces)
        if above is None or below is None:
            continue
        token = _checked_dual(above, below) or _checked_dual(below, above)
        if token is None:
            continue
        kept.append({**_run_of(own), "text": token, "stacked": False})
        joined.update(pieces[member][0] for member in members)
    return kept, [entry for index, entry in enumerate(set_aside) if index not in joined]


#: An inch mark, as fonts set it: the one character a stack put back together from its characters
#: may end with (#880), as `extraction/fraction_parts.py` requires an inch mark drawn after the
#: fraction. A label in feet is left to a reviewer.
_INCH_MARK_RE: Final = re.compile(r"[\"”″]")


def _kept(
    obj: dict[str, Any],
    keep_char: Callable[[dict[str, Any]], bool] | None,
    keep_path: Callable[[dict[str, Any]], bool] | None,
) -> bool:
    """Whether a page object is read: a character `keep_char` keeps, a line, rectangle or curve
    `keep_path` keeps, and anything else. A test that was not given keeps everything."""
    kind = obj.get("object_type")
    if kind == "char":
        return keep_char is None or keep_char(obj)
    if kind in ("line", "rect", "curve"):
        return keep_path is None or keep_path(obj)
    return True


def _along_middle(frame: _Frame) -> float:
    return (frame[0] + frame[1]) / 2


def _across_middle(frame: _Frame) -> float:
    return (frame[2] + frame[3]) / 2


def _height(frame: _Frame) -> float:
    return frame[3] - frame[2]


def _path_span(path: dict[str, Any], up: tuple[float, float]) -> tuple[float, float, float, float]:
    """A path's box as `(along_low, along_high, across_low, across_high)` on a line read the way
    `up` names, its corners projected as `_frame` projects a character's."""
    up_x, up_y = up
    corners = [(float(path[x]), float(path[y])) for x in ("x0", "x1") for y in ("y0", "y1")]
    across = [x * up_x + y * up_y for x, y in corners]
    along = [x * up_y - y * up_x for x, y in corners]
    return min(along), max(along), min(across), max(across)


def _run_of(chars: list[dict[str, Any]]) -> dict[str, Any]:
    """Characters as one run, with the box round them, as `extract_words` gives a word."""
    return {
        "text": "".join(str(char["text"]) for char in chars),
        "chars": chars,
        "x0": min(char["x0"] for char in chars),
        "top": min(char["top"] for char in chars),
        "x1": max(char["x1"] for char in chars),
        "bottom": max(char["bottom"] for char in chars),
        "upright": bool(chars[0].get("upright", True)),
    }


def _stack_on_bar(
    path: dict[str, Any],
    up: tuple[float, float],
    nearby: list[_Framed],
    pool: list[_Framed],
    printed: list[_Framed],
    digits: _DigitGrid,
) -> dict[str, Any] | None:
    """The label a stacked fraction makes with `path` as its bar, read the way `up` names, as one
    run marked as stacked; or `None`.

    `nearby` is the set-aside digits round the path that read that way, `pool` every character of
    the stacks set aside, and `printed` every character on the page. The label is the layout of a
    stacked fraction (`extraction/glyph_bands.py`, #834), found among exact characters:

    - **the bar**: the path is longer along the line than it is thick across it, lies across it
      between the middles of the numerator and the denominator, and along it within their span;
    - **the numerator and the denominator**: the set-aside digits whose middles lie along the bar
      and within `STACK_REACH` of it, on exactly two lines (`_two_rows`), one either side of it,
      `_SAME_LINE` to `STACK_REACH` text heights apart;
    - **the whole number**: set-aside digits running back from the stack, centred across within
      it, each touching the label as far as it has been followed (`_TOUCHING` of the label's
      tallest digit, or of its own height where that is taller);
    - **the inch mark**: the one set-aside character touching the stack after it, and an inch mark.

    Refused, as `extraction/fraction_parts.py` refuses a reading put together from its pieces,
    unless the numerator is below the denominator, the denominator is in `INCH_DENOMINATORS`, and no
    piece of more than one digit starts with a zero. Refused too where any other character on the
    page reads the same way, is centred across within the stack, and touches the label or stands
    inside it (a neighbour, which may be a piece the count is missing), and where a digit that is
    not its own is printed over it (`_DigitGrid.printed_over`).
    """
    a0, a1, c0, c1 = _path_span(path, up)
    if a1 - a0 <= c1 - c0:
        return None  # thicker across the line than it is long along it: not a bar
    bar = (c0 + c1) / 2
    stack = [
        (char, frame)
        for char, frame in nearby
        if a0 <= _along_middle(frame) <= a1
        and abs(_across_middle(frame) - bar) <= STACK_REACH * _height(frame)
    ]
    rows = _two_rows(stack)
    if rows is None:
        return None
    upper, lower = rows
    if not max(_across_middle(frame) for _, frame in lower) < c0:
        return None  # the path is not above the denominator
    if not c1 < min(_across_middle(frame) for _, frame in upper):
        return None  # nor below the numerator
    start = min(frame[0] for _, frame in stack)
    end = max(frame[1] for _, frame in stack)
    if a0 < start or end < a1:
        return None  # it runs on past the stack: a line drawn through it, not its bar
    tallest = max(_height(frame) for _, frame in stack)
    offset = sum(_across_middle(frame) for _, frame in upper) / len(upper) - sum(
        _across_middle(frame) for _, frame in lower
    ) / len(lower)
    if not _SAME_LINE * tallest <= offset <= STACK_REACH * tallest:
        return None

    foot = min(frame[2] for _, frame in stack)
    head = max(frame[3] for _, frame in stack)
    stacked = {id(char) for char, _ in stack}
    line = [
        (char, frame)
        for char, frame in pool
        if id(char) not in stacked and frame[4] == up and foot <= _across_middle(frame) <= head
    ]
    # **The whole number, followed back from the stack** one touching digit at a time. Each one
    # taken can only bring more within reach, so the digits found do not depend on their order.
    whole: list[_Framed] = []
    taken: set[int] = set()
    grew = True
    while grew:
        grew = False
        for char, frame in line:
            if id(char) in taken or not str(char["text"]).isdigit():
                continue
            touching = start - frame[1] <= _TOUCHING * max(tallest, _height(frame))
            if _along_middle(frame) < start and touching:
                whole.append((char, frame))
                taken.add(id(char))
                start = min(start, frame[0])
                tallest = max(tallest, _height(frame))
                grew = True
    after = [
        (char, frame)
        for char, frame in line
        if _along_middle(frame) > end and frame[0] - end <= _TOUCHING * tallest
    ]
    if len(after) != 1 or not _INCH_MARK_RE.fullmatch(str(after[0][0]["text"])):
        return None  # no inch mark, or more than one character, after the fraction
    mark = after[0]
    end = max(end, mark[1][1])

    whole.sort(key=lambda entry: entry[1][0])
    label = [*whole, *upper, *lower, mark]
    own = {id(char) for char, _ in label}
    for char, frame in printed:
        if id(char) in own or frame[4] != up or not foot <= _across_middle(frame) <= head:
            continue
        # Heights are digits' heights, as everywhere in this module: a mark's box says nothing of
        # a label's size. Measured on `AI_Set_1`, an inch mark's box is 2.95 points across the
        # line beside stacked digits of 2.10; counted, it made the mark of a `1/8"` touch the
        # `3/4"` after it, 1.37 points away, and the `3/4"` was refused.
        reach = _TOUCHING * max(tallest, _height(frame) if str(char["text"]).isdigit() else 0)
        if frame[1] >= start - reach and frame[0] <= end + reach:
            return None  # a character touching the label, or inside it, that is no part of it
    chars = [char for char, _ in label]
    if digits.printed_over({"chars": chars}):
        return None

    pieces = (_text_of(whole), _text_of(upper), _text_of(lower))
    if any(len(piece) > 1 and piece.startswith("0") for piece in pieces):
        return None
    numerator, denominator = int(pieces[1]), int(pieces[2])
    if denominator not in INCH_DENOMINATORS or not 0 < numerator < denominator:
        return None
    text = f"{pieces[0]} {numerator}/{denominator}{mark[0]['text']}".strip()
    return {**_run_of(chars), "text": text, "stacked": True}


def _compose_split_stacks(
    words: list[dict[str, Any]],
    set_aside: list[tuple[dict[str, Any], SetAsideReason]],
    digits: _DigitGrid,
    chars: list[dict[str, Any]],
    paths: tuple[dict[str, Any], ...],
) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], SetAsideReason]]]:
    """`(words, set_aside)` with every stacked label that composes from the characters of the
    stacks still set aside added to the words, and what is left of those stacks still set aside.

    **Why words are not enough** (#880). `extract_words` groups characters by where they fall in
    reading order, and a stack's two lines can fall into different words or into a neighbour's:
    measured on `AI_Set_1`, a sideways `15 1/2"` came back as `152` and `1"`, each printed over by
    the other, and a chain of sideways labels as one word `"81"738"81"…` holding eight labels and the
    mark of a ninth. No word of those is a label, so each was set aside whole.

    **So the labels are found again from the characters**, anchored on what sets a stacked fraction
    apart from two lines of text set close together: the bar drawn between numerator and
    denominator, a path on the page (`_stack_on_bar`). Every character of a label comes from a stack
    set aside; where one it needs is in a word read or set aside for another reason, the label is not
    composed: its stack is incomplete, or that character touches the label or stands inside it
    without being part of it.
    A character two bars would both claim is given to the first, in page order, and the second label is
    not composed. What composes is marked as stacked, like every stacked fraction read whole, so it
    is a reviewer's suggestion and never evidence (#726). What is left of a word once its characters
    have gone into labels stays set aside, as its own run; a word none of whose characters composed
    stays as it was.

    Every number here is one the reader already holds (`STACK_REACH`, `_SAME_LINE`, `_TOUCHING`) or
    a comparison with no number in it; nothing is fitted to a drawing.
    """
    pool: list[_Framed] = []
    for word, reason in set_aside:
        if reason is not SetAsideReason.STACKED_FRACTION:
            continue
        for char in word.get("chars") or ():
            if str(char.get("text", "")).strip() and (frame := _frame(char)) is not None:
                pool.append((char, frame))
    if not pool:
        return words, set_aside
    by_id = {id(char): (char, frame) for char, frame in pool}
    stack_digits = _DigitGrid([char for char, _ in pool])
    printed = [
        (char, frame)
        for char in chars
        if str(char.get("text", "")).strip() and (frame := _frame(char)) is not None
    ]

    used: set[int] = set()
    composed: list[dict[str, Any]] = []
    for path in paths:
        nearby = [by_id[key] for key, _ in stack_digits.near(path)]
        for up in sorted({frame[4] for _, frame in nearby}):
            label = _stack_on_bar(
                path,
                up,
                [entry for entry in nearby if entry[1][4] == up],
                pool,
                printed,
                digits,
            )
            if label is None or any(id(char) in used for char in label["chars"]):
                continue
            used.update(id(char) for char in label["chars"])
            composed.append(label)
    if not composed:
        return words, set_aside

    still: list[tuple[dict[str, Any], SetAsideReason]] = []
    for word, reason in set_aside:
        own = list(word.get("chars") or ())
        left = [char for char in own if id(char) not in used]
        if len(left) == len(own):
            still.append((word, reason))
        elif any(str(char.get("text", "")).strip() for char in left):
            still.append((_run_of(left), reason))
    return [*words, *composed], still


def _bracket_groups(framed: list[_Framed]) -> list[list[_Framed]] | None:
    """A row of bracketed groups set back to back, `[2][4]`, as its groups in reading order; or
    `None` unless every character is in a group and no group reaches into the next along the line.
    """
    groups: list[list[_Framed]] = []
    for entry in sorted(framed, key=lambda entry: entry[1][0]):
        if str(entry[0]["text"]) == "[":
            groups.append([entry])
        elif groups and str(groups[-1][-1][0]["text"]) != "]":
            groups[-1].append(entry)
        else:
            return None
    if not all(str(group[-1][0]["text"]) == "]" for group in groups):
        return None
    spans = [(min(f[0] for _, f in group), max(f[1] for _, f in group)) for group in groups]
    if any(later[0] <= earlier[1] for earlier, later in pairwise(spans)):
        return None  # brackets that overlap along the line: no telling which a digit stands over
    return groups


def _number_over(number: list[_Framed], groups: list[list[_Framed]]) -> list[list[_Framed]] | None:
    """`number`'s digits shared out among `groups`, each to the group whose span along the line its
    middle lies in; or `None` unless every digit lies over exactly one group and every group has a
    digit over it."""
    spans = [(min(f[0] for _, f in group), max(f[1] for _, f in group)) for group in groups]
    parts: list[list[_Framed]] = [[] for _ in groups]
    for entry in sorted(number, key=lambda entry: entry[1][0]):
        over = [
            index
            for index, (low, high) in enumerate(spans)
            if low <= _along_middle(entry[1]) <= high
        ]
        if len(over) != 1:
            return None
        parts[over[0]].append(entry)
    return parts if all(parts) else None


def _rows_touch(number: list[_Framed], brackets: list[_Framed]) -> bool:
    """Whether `number` and `brackets` are two rows one directly over the other: each on a row of
    its own (`_two_rows`), and the gap between the rows, across the line, a touch (`_TOUCHING` of
    the tallest digit), as the two lines of one label are."""
    rows = _two_rows([*number, *brackets])
    if rows is None:
        return False
    number_ids = {id(char) for char, _ in number}
    if {id(char) for char, _ in rows[0]} == number_ids:
        upper, lower = number, brackets
    elif {id(char) for char, _ in rows[1]} == number_ids:
        upper, lower = brackets, number
    else:
        return False
    tallest = max(
        _height(frame) for char, frame in rows[0] + rows[1] if str(char["text"]).isdigit()
    )
    gap = min(frame[2] for _, frame in upper) - max(frame[3] for _, frame in lower)
    return gap <= _TOUCHING * tallest


def _read_side_by_side(
    words: list[dict[str, Any]], digits: _DigitGrid, chars: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """`words` with each pair of rows that holds labels side by side read as those labels (#904).

    **Why they were lost.** `extract_words` starts a new word only where the gap between two
    characters is wider than three points. Measured on `AI_Set_1` pages 4 and 5, two sideways labels
    of two digits over one-digit inches, set with digits 3 1/4 points tall, stand about a point
    apart, so each row came back as one word: as if `46` over `[2]` and `97` over `[4]` read
    `4697` and `[2][4]`. No join reads either, and both reached a person blank.

    **The brackets say where each label is.** A word of bracketed groups set back to back
    (`_BRACKET_GROUPS_RE`) holds the inches of as many labels, each group whole between its own
    brackets. A word of digits on the row directly over or under it (`_rows_touch`) holds their
    millimetres, and each digit goes with the group it stands over (`_number_over`), as a numerator
    is read with the bar it stands over (`_stack_on_bar`). Each label is then held to
    `_checked_dual`. The inches come only from their own brackets, and millimetres are never a
    verdict's operand, so a digit given to the wrong label could change no value.

    **Refused, and left as they were**, unless there is exactly one such number word, every digit of
    it stands over exactly one group and every group has a digit over it, the groups do not overlap
    along the line, each label's millimetres and inches run with no gap wider than `_TOUCHING`
    (`_touching_run`), no character of the page outside the pair touches it (`_alone`), no digit of
    another label is printed over it, and every label passes. The labels are not marked stacked:
    neither row is a fraction set as a stack.
    """
    printed: list[_Framed] | None = None
    owner = {
        id(char): index for index, word in enumerate(words) for char in word.get("chars") or ()
    }
    replaced: dict[int, list[dict[str, Any]]] = {}
    for index, word in enumerate(words):
        if not _BRACKET_GROUPS_RE.fullmatch(str(word.get("text", ""))):
            continue
        brackets = _framed(word)
        groups = None if brackets is None else _bracket_groups(brackets)
        if brackets is None or groups is None:
            continue
        found: list[tuple[int, list[_Framed], list[list[_Framed]]]] = []
        for other in sorted(
            {owner[key] for char, _ in brackets for key, _ in digits.near(char) if key in owner}
        ):
            if other == index or other in replaced:
                continue
            number = _framed(words[other])
            if (
                number is None
                or not _DIGITS_RE.fullmatch(str(words[other].get("text", "")))
                or number[0][1][4] != brackets[0][1][4]
                or not _rows_touch(number, brackets)
            ):
                continue
            parts = _number_over(number, groups)
            if parts is not None:
                found.append((other, number, parts))
        if len(found) != 1:
            continue
        other, number, parts = found[0]
        pair = [*number, *brackets]
        printed = _printed(chars) if printed is None else printed
        if digits.printed_over({"chars": [char for char, _ in pair]}) or not _alone(pair, printed):
            continue
        tallest = _tallest_digit(pair)
        labels = []
        for part, group in zip(parts, groups, strict=True):
            token = _checked_dual(_text_of(part), _text_of(group))
            if token is None or not (
                _touching_run(part, tallest) and _touching_run(group, tallest)
            ):
                break
            chars = [char for char, _ in (*part, *group)]
            labels.append({**_run_of(chars), "text": token, "stacked": False})
        else:
            replaced[other] = []
            replaced[index] = labels
    if not replaced:
        return words
    return [run for index, word in enumerate(words) for run in replaced.get(index, [word])]


def _is_fragment(run: dict[str, Any], digits: _DigitGrid, partners: set[int]) -> bool:
    """Whether a run is a piece of a longer label. Three shapes, each measured on the client's sets:

    - **Inside**: another digit sits within the run's own span: `101"` whose stacked `3/4` sits
      between the number and its mark.
    - **Touching**: another digit on its line closer than `_TOUCHING` to either end: `9.7"` cut from
      `19.7"`.
    - **Feet alone**: a bare feet number with another digit on its line within `FRAGMENT_REACH`:
      `7'` of a sideways `7' -11"`. Either side, because a drawing's mirrored text reads backwards.
      The digits it found are added to `partners`, so the run they belong to is set aside with it.
    """
    chars = list(run.get("chars") or ())
    own = [(char, frame) for char, frame in _digit_frames(chars) if frame is not None]
    if not own:
        return False
    up = own[0][1][4]
    frames = [frame for char in chars if (frame := _frame(char)) is not None and frame[4] == up]
    start = min(frame[0] for frame in frames)
    end = max(frame[1] for frame in frames)
    height = max(frame[3] - frame[2] for _, frame in own)
    centre = sum((frame[2] + frame[3]) / 2 for _, frame in own) / len(own)
    low, high = centre - height / 2, centre + height / 2
    bare_feet = _BARE_FEET_RE.fullmatch(str(run.get("text", "")).strip()) is not None
    reach = (FRAGMENT_REACH if bare_feet else _TOUCHING) * height
    found_partner = False
    own_ids = {id(char) for char in chars}
    for char, _ in own:
        for other_id, other in digits.near(char):
            if other_id in own_ids or other[4] != up:
                continue
            middle = (other[0] + other[1]) / 2
            if start <= middle <= end and other[2] < high and other[3] > low:
                return True
            on_line = abs((other[2] + other[3]) / 2 - centre) <= _SAME_LINE * height
            if on_line and (start - reach <= middle < start or end < middle <= end + reach):
                if not bare_feet:
                    return True
                partners.add(other_id)
                found_partner = True
    return found_partner


def _image(x: object, top: object, transform: PageTransform, height: Decimal) -> ImagePoint:
    """A pdfplumber `(x, top)` as an integer image point.

    pdfplumber measures `top` downward from the top of the page while PDF user space measures upward
    from the bottom, so the y is flipped against the page height before the transform sees it.

    **Kept rather than discarded, because it cannot be recovered later.** `observation_candidates`
    stores its polygon in image space, and going back from stored space needs the `dpi`, `media_box`
    and `crop_box` that this transform was built from — none of which the database holds. An earlier
    version computed this on the way to stored space and threw it away, which made a persisted
    candidate's geometry unreconstructable from anything the system had written down.
    """
    return transform.to_image(PdfPoint(x=_decimal(x), y=height - _decimal(top)))


def _stored(x: object, top: object, transform: PageTransform, height: Decimal) -> StoredPoint:
    """A pdfplumber `(x, top)` as a stored point.

    Reached through integer image space, because that is the only route `PageTransform` offers — and
    going through it rather than normalising directly keeps this agreeing with every other consumer
    of stored coordinates.
    """
    return transform.to_stored(_image(x, top, transform, height))


#: The most words a dual token can be split into.
#:
#: `984 [38 3/4]` is three; the regex forbids nested brackets, so a token cannot run long. A bound is
#: needed because the search tries consecutive runs, and without one a line of forty words would try
#: every span of it for a shape that is never more than a few words wide.
_MAXIMUM_TOKEN_WORDS = 6

#: The (left, top, right, bottom) a token occupies in PDF space, before any conversion.
_Box = tuple[Decimal, Decimal, Decimal, Decimal]


def _dual_tokens(
    page: Any,
    words: list[dict[str, Any]],
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> tuple[tuple[TextItem, _Box, dict[str, Any]], ...]:
    """Every `984 [38 3/4]` and every `2' -5"` on the page, as one text run each, with its box and
    the run itself (its characters are what `_is_fragment` measures).

    Both shapes are in `_WHOLE_TOKENS`: a dimension `extract_words` splits at a space, whose parts
    would be read as other dimensions or as none.

    **Rebuilt from the words, not from character offsets.** The first version matched the regex
    against a line's text and sliced its characters by the match offsets, which is wrong in a way
    that looks right: `extract_text_lines` puts a space between words that have a gap, and those
    spaces are not characters — a line reading `DEPTH 984 [38 3/4] TYP` has 22 text positions and 18
    characters, so every offset after the first space points at the wrong glyph.

    So the line is used only to say which words share it, by exact box containment, and the token is
    found by joining consecutive words back together. The box that comes out is the union of real
    word boxes, which is what the drawing actually says. A line's box can enclose a word that is not
    on it, so one word can be on two lines; `_join_lines` joins each character into one token at
    most, so the label it is part of is still read once (#894).

    **An upright line is read as the runs of it that can be one label** (#904, `_upright_runs`):
    broken where the gap along it is wider than `FRAGMENT_REACH`, and two rows read row by row.

    **Sideways labels are joined too** (#738). `extract_text_lines` groups only upright text, so a
    sideways `2' - 0"` stayed three words, and `2'` alone is 24 inches while `0"` alone is none.
    Sideways words are put on lines by their own geometry (`_sideways_lines`) and joined the same way,
    after the upright lines, from the words those did not use.
    """
    found: list[tuple[TextItem, _Box, dict[str, Any]]] = []
    upright_lines = []
    for line in page.extract_text_lines(return_chars=True):
        line_box = (line["x0"], line["top"], line["x1"], line["bottom"])
        upright_lines.extend(
            _upright_runs(
                sorted(
                    (word for word in words if _inside(word, line_box)),
                    key=lambda word: word["x0"],
                )
            )
        )
    _join_lines(upright_lines, found, transform, height, document_version_id, page_index)
    sideways = [
        word
        for word in words
        if not word.get("upright", True) and not _inside_any(word, tuple(found))
    ]
    _join_lines(
        _sideways_lines(sideways), found, transform, height, document_version_id, page_index
    )
    return tuple(found)


def _join_lines(
    lines: list[list[dict[str, Any]]],
    found: list[tuple[TextItem, _Box, dict[str, Any]]],
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> None:
    """Join consecutive words of each line that together make one `_WHOLE_TOKENS` dimension.

    **Each character is joined into one token at most** (#894). An upright line holds the words
    whose boxes lie inside its box (`_dual_tokens`), and the box `extract_text_lines` gives a line is
    the one round all of its characters, so it can enclose words that are not on it. Measured on
    `AI_Set_1`, a line of a note running across a drawing enclosed a stacked fraction none of whose
    characters it has, and the label was joined on its own line and again on the note's: one label
    read twice, from the same characters. So a run that holds a character a token already found
    holds — in this call or in `found` before it — is passed over as a run that does not match is,
    and the next smaller run is tried. The first token found keeps the characters, in the order the
    lines are joined; on both client sets every run passed over was a copy of that token.

    Which words a line holds is left as it was. Holding only the words with a character on the line
    was tried, and it changes real readings: measured on `AI_Set_2`, it lost a dual dimension whose
    pieces `extract_text_lines` puts on two lines, joined only because one line's box encloses them
    all, and changed the readings round it.
    """
    taken = {id(char) for _, _, merged in found for char in merged["chars"]}
    for members in lines:
        index = 0
        while index < len(members):
            for size in range(min(_MAXIMUM_TOKEN_WORDS, len(members) - index), 0, -1):
                run = members[index : index + size]
                joined = " ".join(str(word["text"]) for word in run)
                if not any(pattern.fullmatch(joined) for pattern in _WHOLE_TOKENS):
                    continue
                chars = [char for word in run for char in (word.get("chars") or ())]
                if any(id(char) in taken for char in chars):
                    continue
                box = (
                    min(word["x0"] for word in run),
                    min(word["top"] for word in run),
                    max(word["x1"] for word in run),
                    max(word["bottom"] for word in run),
                )
                merged = {
                    "text": " ".join(str(word["text"]) for word in run),
                    "chars": chars,
                    "x0": box[0],
                    "top": box[1],
                    "x1": box[2],
                    "bottom": box[3],
                    "upright": run[0].get("upright", True),
                    # A stacked fraction joined into a longer label keeps its mark: the label is
                    # still one a reviewer must confirm (#726).
                    "stacked": any(word.get("stacked", False) for word in run),
                }
                item = _text_item(merged, transform, height, document_version_id, page_index)
                if item is not None:
                    found.append((item, box, merged))
                    taken.update(id(char) for char in chars)
                index += size
                break
            else:
                index += 1


#: Which way is up for upright text, as `_frame` names it.
_UPRIGHT: Final = (0.0, 1.0)


def _upright_runs(members: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """The words of an upright line, in order of their left edges, as the runs a token may be
    joined from, each in reading order (#904).

    **Why the line alone is not enough.** `extract_text_lines` puts words on one line wherever their
    tops chain within three points, so a line runs on past any one label, and a line of a note set
    between two rows holds both rows. Measured on `AI_Set_2` page 7: one line held a chain of labels
    written as millimetres over bracketed inches, both rows of each. In order of left edges the words
    of two rows interleave, and each bracket starts a hair before its number: as if `[32]` came
    before `813`, and `813` was joined to `[1`, `44` and `3/4]` of the next label, 47 points along,
    into `813 [1 44 3/4]`. Set aside as a piece of a longer label, it left both labels unread. Had
    the next label's inches been one word, the join would have made a well-formed `813 [2]`, 2
    inches for a label that says 32, which only the test for pieces of longer labels
    (`_is_fragment`) set aside.

    **So the line is read as runs.** It is broken wherever the gap along it, from the furthest any
    word so far reaches, is wider than `FRAGMENT_REACH` of the text's height, as `_sideways_lines`
    breaks a sideways line: labels further apart than that are never joined into one. A run whose
    words lie on two rows (`_two_rows`), each word on one of them, is read row by row: as one label,
    the millimetres row then the inches row, where the two rows are one number over (or under) one
    bracketed inches that `_checked_dual` passes, and the two overlap along the line; and otherwise
    as each row on its own, so no token is joined across two rows that are not one label. Measured
    on `AI_Set_2` page 8, the overlap refuses a number joined to the bracketed inches of the next
    label, raised beside it on its leader. Every other run is read in order of left edges, as the
    whole line was before: a run on one row, on more than two rows, or with a word on both rows or
    not upright, and every run of a line that holds a sideways word, which is not broken at all.
    """
    if not members or not all(word.get("upright", True) for word in members):
        return [members]
    runs = [[members[0]]]
    furthest = members[0]
    for word in members[1:]:
        reach = FRAGMENT_REACH * max(
            word["bottom"] - word["top"], furthest["bottom"] - furthest["top"]
        )
        if word["x0"] - furthest["x1"] > reach:
            runs.append([word])
            furthest = word
            continue
        runs[-1].append(word)
        if word["x1"] > furthest["x1"]:
            furthest = word
    return [line for run in runs for line in _by_rows(run)]


def _by_rows(run: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """One run of an upright line, as `_upright_runs` reads it: one label over two rows, each row
    alone, or as it was."""
    if len(run) < 2:
        return [run]
    framed = [_framed(word) for word in run]
    if any(entries is None or entries[0][1][4] != _UPRIGHT for entries in framed):
        return [run]
    rows = _two_rows([entry for entries in framed if entries is not None for entry in entries])
    if rows is None:
        return [run]
    upper_ids = {id(char) for char, _ in rows[0]}
    upper: list[dict[str, Any]] = []
    lower: list[dict[str, Any]] = []
    for word, entries in zip(run, framed, strict=True):
        on_upper = {id(char) in upper_ids for char, _ in entries or ()}
        if on_upper == {True}:
            upper.append(word)
        elif on_upper == {False}:
            lower.append(word)
        else:
            return [run]  # a word on both rows
    for millimetres, inches in ((upper, lower), (lower, upper)):
        token = _checked_dual(
            " ".join(str(word["text"]) for word in millimetres),
            " ".join(str(word["text"]) for word in inches),
        )
        overlap = min(max(word["x1"] for word in millimetres), max(word["x1"] for word in inches))
        overlap -= max(min(word["x0"] for word in millimetres), min(word["x0"] for word in inches))
        if token is not None and overlap > 0:
            return [[*millimetres, *inches]]
    return [upper, lower]


#: A sideways word as `_sideways_lines` places it: `(up, across_centre, height, start, end, word)`.
_Placed = tuple[tuple[float, float], float, float, float, float, dict[str, Any]]


def _sideways_lines(words: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Sideways words grouped into lines by their own geometry, each line in reading order.

    Words share a line when they read the same way and their centres across the line are within
    half a text height; a line is broken wherever the gap along it exceeds `FRAGMENT_REACH`, so two
    labels on one dimension string are never joined into one.
    """
    placed: list[_Placed] = []
    for word in words:
        frames = [frame for char in word.get("chars") or () if (frame := _frame(char)) is not None]
        if not frames or len({frame[4] for frame in frames}) > 1:
            continue
        low = min(frame[2] for frame in frames)
        high = max(frame[3] for frame in frames)
        start = min(frame[0] for frame in frames)
        end = max(frame[1] for frame in frames)
        placed.append((frames[0][4], (low + high) / 2, high - low, start, end, word))
    placed.sort(key=lambda entry: (entry[0], entry[1]))

    groups: list[list[_Placed]] = []
    for entry in placed:
        first = groups[-1][0] if groups else None
        if (
            first is not None
            and entry[0] == first[0]
            and entry[1] - first[1] <= _SAME_LINE * max(entry[2], first[2])
        ):
            groups[-1].append(entry)
        else:
            groups.append([entry])

    lines: list[list[dict[str, Any]]] = []
    for group in groups:
        ordered = sorted(group, key=lambda entry: entry[3])
        line = [ordered[0][5]]
        for previous, entry in pairwise(ordered):
            if entry[3] - previous[4] > FRAGMENT_REACH * max(entry[2], previous[2]):
                lines.append(line)
                line = []
            line.append(entry[5])
        lines.append(line)
    return lines


def _inside(word: dict[str, Any], box: _Box) -> bool:
    """Whether a word's box lies within another, compared exactly rather than within a tolerance."""
    left, top, right, bottom = box
    return bool(
        word["x0"] >= left
        and word["x1"] <= right
        and word["top"] >= top
        and word["bottom"] <= bottom
    )


def _inside_any(
    word: dict[str, Any], dual: tuple[tuple[TextItem, _Box, dict[str, Any]], ...]
) -> bool:
    """Whether this word is a fragment of a dual token already read whole.

    Exact comparison, not a tolerance: the token's box is the union of the very characters the word
    is made of, so a fragment's edges cannot fall outside it.
    """
    return any(
        word["x0"] >= left
        and word["x1"] <= right
        and word["top"] >= top
        and word["bottom"] <= bottom
        for _, (left, top, right, bottom), _ in dual
    )


def _text_item(
    word: dict[str, Any],
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> TextItem | None:
    """One extracted word as a `TextItem`, or `None` when its box cannot be a polygon.

    A zero-area box is dropped rather than nudged into validity. `Polygon` refuses one, and rightly:
    a rectangle with no area names no region of the drawing, so widening it by a pixel would invent
    a location for evidence that has none.
    """
    text = str(word.get("text", ""))
    if not text:
        return None

    chars = word.get("chars") or ()
    rotation = _text_rotation(chars[0]) if chars else 0

    # One winding, converted twice. The corners are listed in the order
    # `tests/extraction/geometry/test_text_association.py` uses, so a polygon built here and one
    # built there wind the same way — an opposite winding is still a valid rectangle and would make
    # containment tests disagree for reasons nobody would look for.
    box = (
        (word["x0"], word["top"]),
        (word["x1"], word["top"]),
        (word["x1"], word["bottom"]),
        (word["x0"], word["bottom"]),
    )
    corners = tuple(_stored(x, top, transform, height) for x, top in box)
    image_corners = tuple(_image(x, top, transform, height) for x, top in box)
    try:
        extent = Polygon(
            points=corners,
            space="stored",
            document_version_id=document_version_id,
            page=page_index,
        )
    except (ValueError, TypeError):
        return None

    return TextItem(
        text=text,
        extent=extent,
        image_extent=image_corners,
        rotation_degrees=rotation,
        upright=bool(word.get("upright", True)),
        stacked=bool(word.get("stacked", False)),
    )


def _text_rotation(char: dict[str, Any]) -> int:
    """A character's rotation, from its own transformation matrix.

    Read, never inferred — `DimensionText` insists on that, because a box around `984` is wider than
    it is tall and a box around `8` is not, so inferring from the shape would report single-digit
    dimensions as rotated.

    Snapped to the nearest quarter turn. `CTM.skew_x` is a rotation in degrees and a plotter may emit
    `89.9999`; the four values are what everything downstream accepts, and refusing a sheet over a
    ten-thousandth of a degree would be pedantry rather than safety.
    """
    matrix = char.get("matrix")
    if not isinstance(matrix, (tuple, list)) or len(matrix) != 6:
        return 0
    degrees = float(CTM(*matrix).skew_x)
    return int(round(degrees / 90.0) * 90) % 360


def _segments(
    page: Any,
    transform: PageTransform,
    height: Decimal,
    document_version_id: UUID,
    page_index: int,
) -> tuple[DimensionExtent, ...]:
    """Every straight segment the page draws, from all three of pdfplumber's geometry lists.

    Line objects are the obvious source and not the only one: a plotter may emit a dimension line as
    a rectangle edge or as a path, and which one it chooses is a property of the software rather than
    of the drawing. Taking all three is what stops a dimension being invisible because of how the
    sheet was exported.

    Degenerate segments are dropped — `DimensionExtent` refuses identical endpoints, since a line
    spanning nothing annotates nothing.
    """
    found: list[DimensionExtent] = []

    def add(x0: object, top0: object, x1: object, top1: object) -> None:
        try:
            extent = DimensionExtent(
                start=_stored(x0, top0, transform, height),
                end=_stored(x1, top1, transform, height),
                document_version_id=document_version_id,
                page=page_index,
            )
        except (ValueError, TypeError):
            return
        found.append(extent)

    for line in page.lines:
        add(line["x0"], line["top"], line["x1"], line["bottom"])

    for rect in page.rects:
        x0, x1, top, bottom = rect["x0"], rect["x1"], rect["top"], rect["bottom"]
        add(x0, top, x1, top)
        add(x1, top, x1, bottom)
        add(x1, bottom, x0, bottom)
        add(x0, bottom, x0, top)

    for curve in page.curves:
        points = curve.get("pts") or ()
        # Consecutive pairs only. A path's points are in drawing order, so pairing them recovers the
        # straight runs; the curved parts come back as short chords, which is a faithful reading of
        # what was drawn rather than an interpolation of what was meant.
        for start, end in pairwise(points):
            add(start[0], height - _decimal(start[1]), end[0], height - _decimal(end[1]))

    return tuple(found)
