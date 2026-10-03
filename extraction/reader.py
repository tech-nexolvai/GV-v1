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
from itertools import pairwise
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
) -> PageContents:
    """The text runs and straight segments on one page, in stored coordinates.

    `keep_char`, where given, is asked of every character before any word is formed, and a character
    it refuses is not read at all. `extraction/stamp_text.py` uses it to leave out coloured text.

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
            if keep_char is not None:
                test = keep_char
                page = page.filter(lambda obj: obj.get("object_type") != "char" or test(obj))
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

#: The two lines of a dual dimension written one over the other: millimetres, and bracketed inches.
_MILLIMETRES_RE: Final = re.compile(r"\d+(?:\.\d+)?")
_BRACKETED_RE: Final = re.compile(r"\[[^\[\]]+\]")

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
    — or `None`. Either line may be the upper one; the token is written millimetres first."""
    framed = _framed(word)
    if framed is None:
        return None
    rows = _two_rows(framed)
    if rows is None:
        return None
    first, second = (_text_of(row) for row in rows)
    for millimetres, inches in ((first, second), (second, first)):
        if _MILLIMETRES_RE.fullmatch(millimetres) and _BRACKETED_RE.fullmatch(inches):
            token = f"{millimetres} {inches}"
            return token if DUAL_TOKEN_RE.fullmatch(token) else None
    return None


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
    word boxes, which is what the drawing actually says.

    **Sideways labels are joined too** (#738). `extract_text_lines` groups only upright text, so a
    sideways `2' - 0"` stayed three words, and `2'` alone is 24 inches while `0"` alone is none.
    Sideways words are put on lines by their own geometry (`_sideways_lines`) and joined the same way,
    after the upright lines, from the words those did not use.
    """
    found: list[tuple[TextItem, _Box, dict[str, Any]]] = []
    upright_lines = []
    for line in page.extract_text_lines(return_chars=True):
        line_box = (line["x0"], line["top"], line["x1"], line["bottom"])
        upright_lines.append(
            sorted((word for word in words if _inside(word, line_box)), key=lambda word: word["x0"])
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
    """Join consecutive words of each line that together make one `_WHOLE_TOKENS` dimension."""
    for members in lines:
        index = 0
        while index < len(members):
            for size in range(min(_MAXIMUM_TOKEN_WORDS, len(members) - index), 0, -1):
                run = members[index : index + size]
                joined = " ".join(str(word["text"]) for word in run)
                if not any(pattern.fullmatch(joined) for pattern in _WHOLE_TOKENS):
                    continue
                box = (
                    min(word["x0"] for word in run),
                    min(word["top"] for word in run),
                    max(word["x1"] for word in run),
                    max(word["bottom"] for word in run),
                )
                merged = {
                    "text": " ".join(str(word["text"]) for word in run),
                    "chars": [char for word in run for char in (word.get("chars") or ())],
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
                index += size
                break
            else:
                index += 1


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
