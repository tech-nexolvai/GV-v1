"""What a package's pages are — the list every later stage fans out over.

A review that quietly skipped a page and a review that read every page produce findings that look
identical. Nothing downstream can tell them apart, because every later stage only ever sees what the
manifest handed it. So this is the one place that has to know a page existed and was not read, and
it is built once per document version and never mutated afterwards.

Three rules follow from that, and they are the whole module:

**A page that could not be read is recorded, not dropped.** `render_failed=True` keeps the page in
the list with its number intact. Omitting it would renumber every page after it and produce a
package that looks complete.

**A gap in the page numbers is refused.** Records must arrive as page 0, 1, 2 … with none missing
and none repeated. A reader that lost a page between the file and here cannot make that loss
invisible by handing over a shorter list.

**It records; it does not classify.** `page_type` stays `None` until B6.2 (#161) decides it, and
`None` is a real answer rather than a missing one (`docs/DESIGN_EXTRACTION.md` §3.2). A countertop
width found on a cabinet elevation is a plausible number attached to the wrong drawing and no
tolerance check catches it, so an unclassifiable page must never be rounded to the nearest plausible
type. It stays in the manifest and is still extracted; it is only excluded from `scope: same_view`.

**Where a text layer ends and a scan begins is a number the caller states.** A scanned drawing is
rarely free of vector characters — a stamp, a footer, a plot date, the residue of somebody else's
OCR pass. Treating those few characters as a text layer routes the page down the vector lane (B2.2)
instead of OCR (B2.4), after which every dimension on it is simply absent, and the manifest still
says the page was read. How many characters separate the two cases is empirical, `data/drawings/` is
empty, and a default here would ship today's guess as ground truth — so `minimum_vector_characters`
is a required keyword-only argument with no default, exactly like `endpoint_tolerance` in
`extraction/geometry/containment.py`. It is a count of characters rather than a `Decimal`, which is
why it has no non-finite case to guard: an integer count is already exact, and a float is refused
outright.

**The reader is not this module.** Opening the PDF, repairing it and rendering its pages is B2.1
(#123), and it needs real drawings before it can honestly be written. The seam between the two is
`RawPage`: what a reader observed about one page, before any judgement is applied to it. That keeps
every rule above testable today, and it keeps this module free of a PDF library — which matters,
because CI installs only the `dev`, `rules` and `platform` extras, so a `pdfplumber` or `pypdfium2`
path here would ship with nothing running against it.

**What this deliberately does not carry.** No reason string for a failed page: the shipped `pages`
table (`app/models/document.py`) has a boolean and nothing else, and a reviewer asking *why* page 7
could not be read needs a column before it needs a field. No `revision` — `RevisionLabel` is B11.1's
type (#183) and inventing it here would give the real one something to collide with. No count of the
characters that decided `has_vector_text`, for the same reason: there is nowhere to put it yet.

Later stages that learn something about a page — its sheet number, its type — build a **new** record
with `dataclasses.replace` rather than mutating this one. Everything here is frozen, and a rerun
produces a new manifest rather than editing the old one (`AGENTS.md` §2.7).

Source: backend proposal Appendix B stage E, §10.1 `pages` ·
Design: `docs/DESIGN_EXTRACTION.md` §3.1 · Verification: `tests/extraction/test_manifest.py`
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final
from uuid import UUID

from evidence.coordinates import SUPPORTED_ROTATIONS
from vocabulary.page_types import PageType

SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")

#: The exact keys `PageRecord.to_dict` writes and `PageRecord.from_dict` demands. Serialisation is
#: strict in both directions: a missing key is a truncated record and an unknown one is a field this
#: version does not understand, and guessing at either is how a manifest crosses a workflow boundary
#: meaning something different from what it meant when it left.
_RECORD_KEYS: Final = frozenset(
    {
        "index",
        "content_hash",
        "width_pt",
        "height_pt",
        "rotation",
        "has_vector_text",
        "render_failed",
        "sheet_number",
        "sheet_title",
        "page_type",
    }
)

_MANIFEST_KEYS: Final = frozenset({"document_version_id", "pages"})


def _require_int(value: object, name: str) -> int:
    """A real integer — not a bool, and not a float that happens to be whole."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def _require_index(value: object, name: str) -> int:
    """A page number. 0-based, always, everywhere inside the system."""
    index = _require_int(value, name)
    if index < 0:
        raise ValueError(f"{name} must be zero or greater — page indexes are 0-based internally")
    return index


def _require_dimension(value: object, name: str) -> Decimal:
    """A page side length in PDF points, exact and real.

    `Decimal("Infinity") > 0` is `True`, so an infinite width would pass a plain positivity check
    and then make every later comparison against it meaningless. `Decimal("NaN") > 0` is `False`, so
    a NaN width would be reported as "not positive" — a refusal naming a cause that is not the real
    one. Both are checked here rather than left to whatever asks the first question.
    """
    if isinstance(value, float):
        raise TypeError(
            f"{name} must be a Decimal, never a float. A page size that arrived through binary "
            "floating point has already lost exactness, and everything measured against it inherits "
            "the loss (ADR-0001)."
        )
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be a Decimal")
    if not value.is_finite():
        raise ValueError(
            f"{name} must be a finite number. A NaN or infinite page size does not describe a page: "
            "every comparison against it answers the same way, and the answer is not about the page."
        )
    if value <= 0:
        raise ValueError(f"{name} must be greater than zero — a page with no size is not a page")
    return value


def _require_optional_text(value: object, name: str) -> str | None:
    """Text printed on the sheet, or nothing. Blank is not a value — it is a value nobody read."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{name} must be the text printed on the sheet, or None")
    if not value.strip():
        raise ValueError(
            f"{name} must be non-empty text or None. Blank is indistinguishable from unread, and "
            "the difference between them is the difference between a fact and a gap."
        )
    return value


def _require_flag(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be True or False")
    return value


@dataclass(frozen=True, slots=True)
class RawPage:
    """What a reader observed about one page, before any judgement is applied to it.

    The seam between B2.1 (#123), which opens the PDF, and this module, which decides what the
    observation means. Everything here is measured or read; nothing here is a conclusion.

    `unreadable_reason` is plain English for a reviewer — "the page object could not be decoded",
    not an exception class name. Setting it is what makes the page's record `render_failed`.

    **Dimensions are required even for an unreadable page**, because a page's size comes from the
    page dictionary while reading its content is a separate act that can fail on its own, and the
    shipped `pages` table requires them. A reader that cannot determine even a page's size has not
    found a page it cannot read — it has found a document it cannot parse, and it must say so rather
    than invent a page size to fill this field.
    """

    index: int
    content: bytes
    width_pt: Decimal
    height_pt: Decimal
    rotation: int
    vector_character_count: int
    unreadable_reason: str | None = None

    def __post_init__(self) -> None:
        _require_index(self.index, "index")
        if not isinstance(self.content, bytes):
            raise TypeError("content must be the page's bytes as read, even if that is b''")
        _require_dimension(self.width_pt, "width_pt")
        _require_dimension(self.height_pt, "height_pt")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int):
            raise TypeError("rotation must be an integer")
        if self.rotation not in SUPPORTED_ROTATIONS:
            raise ValueError(
                "rotation must be one of 0, 90, 180 or 270, exactly as /Rotate gives it"
            )
        if isinstance(self.vector_character_count, bool) or not isinstance(
            self.vector_character_count, int
        ):
            raise TypeError("vector_character_count must be an integer count of characters")
        if self.vector_character_count < 0:
            raise ValueError("vector_character_count cannot be negative")
        _require_optional_text(self.unreadable_reason, "unreadable_reason")
        if self.unreadable_reason is not None and self.vector_character_count > 0:
            raise ValueError(
                "a page recorded as unreadable cannot also report characters read from it. One of "
                "the two observations is wrong, and a manifest that accepted both would route the "
                "page down the vector lane while telling every later stage it was never read."
            )


@dataclass(frozen=True, slots=True)
class PageRecord:
    """One page of one document version, as the manifest knows it.

    `index` is 0-based, always, everywhere inside the system. `display_index` is the same page
    counted from one, which is the only number a reviewer should ever be shown. Both are named so
    that neither has to be inferred — an off-by-one here puts a highlight box on the wrong sheet and
    reads as a real disagreement about the drawing.
    """

    index: int
    content_hash: str
    width_pt: Decimal
    height_pt: Decimal
    rotation: int
    has_vector_text: bool
    render_failed: bool
    sheet_number: str | None = None
    sheet_title: str | None = None
    page_type: PageType | None = None
    """`None` means *nobody could classify this page*, which is a real outcome and not a gap to be
    filled with the likeliest member (`docs/DESIGN_EXTRACTION.md` §3.2)."""

    def __post_init__(self) -> None:
        _require_index(self.index, "index")
        if not isinstance(self.content_hash, str) or not SHA256_PATTERN.fullmatch(
            self.content_hash
        ):
            raise ValueError(
                "content_hash must be a SHA-256 digest: 64 lowercase hexadecimal characters. It is "
                "what lets a re-run prove it read the same page rather than assume it."
            )
        _require_dimension(self.width_pt, "width_pt")
        _require_dimension(self.height_pt, "height_pt")
        if isinstance(self.rotation, bool) or not isinstance(self.rotation, int):
            raise TypeError("rotation must be an integer")
        if self.rotation not in SUPPORTED_ROTATIONS:
            raise ValueError(
                "rotation must be one of 0, 90, 180 or 270. It is recorded as printed and never "
                "normalised away: a crop taken without it lands on the wrong part of the sheet."
            )
        _require_flag(self.has_vector_text, "has_vector_text")
        _require_flag(self.render_failed, "render_failed")
        _require_optional_text(self.sheet_number, "sheet_number")
        _require_optional_text(self.sheet_title, "sheet_title")
        if self.page_type is not None and not isinstance(self.page_type, PageType):
            raise TypeError(
                "page_type must come from the PageType vocabulary or be None. A free string can be "
                "written, stored, matched against nothing and never noticed."
            )

    @property
    def display_index(self) -> int:
        """The same page counted from one — the only page number a reviewer is ever shown."""
        return self.index + 1

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe form of this record. `Decimal`s become their exact string spelling.

        `Decimal("612.00")` is written as `"612.00"` and comes back as `Decimal("612.00")`, trailing
        zeros and all. Writing it as a JSON number would hand it to a float parser on the way back
        in, which is the one thing a manifest crossing a workflow boundary must not do.
        """
        return {
            "index": self.index,
            "content_hash": self.content_hash,
            "width_pt": str(self.width_pt),
            "height_pt": str(self.height_pt),
            "rotation": self.rotation,
            "has_vector_text": self.has_vector_text,
            "render_failed": self.render_failed,
            "sheet_number": self.sheet_number,
            "sheet_title": self.sheet_title,
            "page_type": None if self.page_type is None else self.page_type.value,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PageRecord:
        """Rebuild a record, refusing anything this version does not fully understand."""
        if not isinstance(data, Mapping):
            raise TypeError("a page record must be built from a mapping")
        _require_exact_keys(data, _RECORD_KEYS, "page record")

        page_type = data["page_type"]
        if page_type is not None and not isinstance(page_type, str):
            raise TypeError("page_type must be a string from the PageType vocabulary, or null")

        return cls(
            index=_require_index(data["index"], "index"),
            content_hash=_require_serialised_str(data["content_hash"], "content_hash"),
            width_pt=_decimal_from_serialised(data["width_pt"], "width_pt"),
            height_pt=_decimal_from_serialised(data["height_pt"], "height_pt"),
            rotation=_require_int(data["rotation"], "rotation"),
            has_vector_text=_require_flag(data["has_vector_text"], "has_vector_text"),
            render_failed=_require_flag(data["render_failed"], "render_failed"),
            sheet_number=_require_optional_text(data["sheet_number"], "sheet_number"),
            sheet_title=_require_optional_text(data["sheet_title"], "sheet_title"),
            page_type=None if page_type is None else PageType(page_type),
        )


@dataclass(frozen=True, slots=True)
class PageManifest:
    """Every page of one document version, in order, with none missing.

    Built once and frozen. A manifest with a gap in its page numbers, a repeated page, or no pages
    at all is refused outright rather than carried: every later stage fans out over this, and
    fanning out over an incomplete list produces a review that read less than it claims to have.
    """

    document_version_id: UUID
    pages: tuple[PageRecord, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        if not isinstance(self.pages, tuple):
            raise TypeError(
                "pages must be a tuple. A list could be appended to after the manifest was built, "
                "and a manifest that can grow is not a record of what was read."
            )
        if not self.pages:
            raise ValueError(
                "a manifest with no pages is refused. Every later stage fans out over it, and "
                "fanning out over nothing produces a review that looks complete and read nothing."
            )
        for position, record in enumerate(self.pages):
            if not isinstance(record, PageRecord):
                raise TypeError("pages must contain PageRecord values")
            if record.index != position:
                raise ValueError(
                    f"page records must run 0, 1, 2 … in order with none missing: expected index "
                    f"{position} at position {position}, found {record.index}. A gap or a reorder "
                    "means a page was dropped or renumbered, and this is the only place that can "
                    "notice."
                )

    def display_index(self, index: int) -> int:
        """The 1-based page number a reviewer sees, for the 0-based `index` used internally.

        Raises rather than answering for a page this manifest does not have. Returning `index + 1`
        for a page that does not exist would put a real-looking page number on a citation that
        points nowhere.
        """
        _require_index(index, "index")
        if index >= len(self.pages):
            raise IndexError(
                f"this manifest has {len(self.pages)} page(s); there is no page at index {index}"
            )
        return self.pages[index].display_index

    def to_dict(self) -> dict[str, object]:
        """A JSON-safe form of the whole manifest, for the workflow boundary in B6.4."""
        return {
            "document_version_id": str(self.document_version_id),
            "pages": [record.to_dict() for record in self.pages],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> PageManifest:
        """Rebuild a manifest, refusing anything this version does not fully understand."""
        if not isinstance(data, Mapping):
            raise TypeError("a manifest must be built from a mapping")
        _require_exact_keys(data, _MANIFEST_KEYS, "manifest")

        document_version_id = data["document_version_id"]
        if not isinstance(document_version_id, str):
            raise TypeError("document_version_id must be a UUID string")

        pages = data["pages"]
        if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
            raise TypeError("pages must be a sequence of page records")

        return cls(
            document_version_id=UUID(document_version_id),
            pages=tuple(PageRecord.from_dict(_require_mapping(page)) for page in pages),
        )


def build_manifest(
    pages: Sequence[RawPage],
    document_version_id: UUID,
    *,
    minimum_vector_characters: int,
) -> PageManifest:
    """Turn one reader's observations into the manifest for one document version.

    `minimum_vector_characters` is how many extractable characters a page must carry before it
    counts as having a vector text layer. It is required, and it decides which extraction lane the
    page takes — see the module docstring for why there is no default and why a scanned page with a
    handful of stray characters is the case that matters.

    Every page keeps its number. A page the reader could not read arrives with an
    `unreadable_reason` and leaves as a record with `render_failed=True`, still in place; its
    content hash is the hash of whatever was read, which for such a page may be nothing at all. That
    is why `render_failed` and not the hash is the field a later stage must check.

    No page is classified here, and no sheet number is read here. Both stay `None` until the stories
    that can honestly decide them (#161, #162) fill them in on a new record.
    """
    _check_minimum_vector_characters(minimum_vector_characters)
    if not isinstance(pages, Sequence) or isinstance(pages, (str, bytes)):
        raise TypeError("pages must be a sequence of RawPage observations")
    for page in pages:
        if not isinstance(page, RawPage):
            raise TypeError("pages must contain RawPage observations")

    records = tuple(
        PageRecord(
            index=page.index,
            content_hash=hashlib.sha256(page.content).hexdigest(),
            width_pt=page.width_pt,
            height_pt=page.height_pt,
            rotation=page.rotation,
            has_vector_text=page.vector_character_count >= minimum_vector_characters,
            render_failed=page.unreadable_reason is not None,
        )
        for page in pages
    )
    return PageManifest(document_version_id=document_version_id, pages=records)


def _check_minimum_vector_characters(minimum: int) -> None:
    if isinstance(minimum, bool) or not isinstance(minimum, int):
        raise TypeError(
            "minimum_vector_characters must be an int — a whole number of characters. A float or a "
            "Decimal would make the boundary between 'this page has a text layer' and 'this page "
            "needs OCR' depend on rounding, and that boundary decides whether a page is read at all."
        )
    if minimum < 1:
        raise ValueError(
            "minimum_vector_characters must be at least 1. Zero is not a lower threshold, it is no "
            "threshold: it declares that a page with no extractable text has a text layer, which "
            "sends every scanned sheet down the vector lane to find nothing."
        )


def _require_exact_keys(data: Mapping[str, object], expected: frozenset[str], what: str) -> None:
    present = set(data)
    if missing := sorted(expected - present):
        raise ValueError(f"the serialised {what} is missing: {', '.join(missing)}")
    if unknown := sorted(present - expected):
        raise ValueError(
            f"the serialised {what} carries fields this version does not understand: "
            f"{', '.join(unknown)}. It was written by a different version, and reading it as if it "
            "were this one would silently drop whatever those fields meant."
        )


def _require_mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("each serialised page must be a mapping")
    return value


def _require_serialised_str(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _decimal_from_serialised(value: object, name: str) -> Decimal:
    """Read a dimension back from its exact string spelling, and refuse anything else.

    A JSON number here means the value passed through a float on its way to the wire, so the exact
    page size is already gone and only an approximation of it arrived. Accepting it would launder
    that approximation into a manifest whose whole purpose is to say precisely what was read.
    """
    if isinstance(value, (bool, float, int)):
        raise TypeError(
            f"{name} must be a string holding the exact decimal, not a JSON number. A number here "
            "has already been through a float parser and is no longer the value that was read."
        )
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string holding the exact decimal")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError(f"{name} is not a decimal number: {value!r}") from error
    return _require_dimension(parsed, name)
