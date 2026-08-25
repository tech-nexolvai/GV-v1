"""Put the findings back on the drawing, and never lose one on the way.

With the reviewer UI deferred, `docs/DESIGN_PRODUCT.md` says plainly that **the redline is the
product**. It is the only place a reviewer sees a finding in context and the only thing a vendor
receives. Everything in this module follows from that one sentence.

**The overlay is a layer, and the drawing underneath is untouched.** The source page object is taken
from the original PDF and a transparent vector page is merged onto it, so the original content
stream — text, line work, the vendor's own annotations — survives byte-for-byte. Nothing is
rasterised. A redline that flattened the sheet to an image would destroy the text a reviewer
searches and the vector geometry the next revision is compared against, and it would do so
invisibly: the picture would still look right.

**A finding whose evidence cannot be placed is listed, never dropped.** This is the failure this
module exists to prevent. A finding that vanishes because it had no geometry is worse than a wrong
mark, because the reviewer never learns it existed and silence reads as approval — the exact failure
`NO_APPLICABLE_RULE` was invented to stop (`DESIGN_PRODUCT.md` §3.2). Abstentions are the *normal*
case here, not an edge case: a `NOT_FOUND` finding has no operands, so it has no evidence reference,
so there is nothing on the sheet to point at. Every such finding is written out on an appended
summary page, with the reason it could not be marked. The summary section is emitted even when it is
empty, so that "nothing was left off" is something the reader is told rather than something they
have to infer from an absence.

**Coordinates come from B8.1, and are never re-derived here.** An evidence reference carries the
polygon in the normalised stored space (`evidence/gate.py` writes it as exact decimal strings);
`PageTransform` is the only thing that turns that back into a place on the page. Re-deriving the
mapping in this module would mean two implementations of the same transform, and they would drift —
after which the highlight sits confidently on the wrong part of the sheet and nothing detects it.

The route runs stored → rendered image → PDF user space, because those are the conversions
`PageTransform` publishes. That passes through an integer pixel grid, so placement is quantised to
one rendered pixel: at the 72 dpi floor that is one point, and finer at any realistic render dpi. It
is a box around a region, not a measurement, so a quantisation of that size cannot mislead anyone —
but it is real and is stated here rather than glossed over.

**A transform that does not describe the page it is aimed at raises.** Rotation, media box and crop
box are checked against the actual PDF page before anything is drawn. A mismatch means the caller
has paired a page with another page's geometry, and the result would be a redline where every mark
is in the wrong place while looking entirely plausible. That is not something to report in a list;
it is something to refuse.

**Evidence belonging to a page this redline does not cover is listed, not raised.** That is an
ordinary situation — a shop-drawing redline whose finding cites the architectural set — and throwing
the whole report away over it would lose every other finding as well.

**Float appears in exactly one place, and it is not a comparison.** Every coordinate is carried and
compared as `Decimal`. `float` appears only at the ReportLab call boundary, because that is the type
its drawing API takes; by then the arithmetic is finished and the number is on its way into the file.
Nothing is decided after that conversion.

**A vendor render needs a `VendorClearance`, and this module cannot make one.** ADR-0010 is
unambiguous: *"no computed dimension reaches a vendor without reviewer sign-off."* Establishing
sign-off means reading the approval record and matching it against stored finding rows, and this
module has neither a database session nor row identities — it is handed finding *values*. So
`ReportMode.VENDOR` refuses unless something that can establish sign-off already has, which is
`reports.publication.render_vendor_redline`. Refusing here is what makes that the only route to a
vendor document rather than the recommended one.

**Every calculated value is labelled as calculated, in both modes.** ADR-0010 permits showing a
derived expectation and forbids issuing one, and the difference is entirely in how it is presented:
a bare number in a document somebody builds from is an instruction. Each is printed with its
operation and operands, under a heading that says not to build to it.

**What this deliberately does not do.** Nothing here decides which findings belong in a redline,
filters by severity, or lays out a cover sheet. The report is what it is given, in the order it is
given.

Source: `AGENTS.md` §4 and §8 Phase 7; ADR-0010 · Design: `docs/DESIGN_PRODUCT.md` §3.2 ·
Verification: `tests/reports/test_redline.py`
"""

from __future__ import annotations

import json
import textwrap
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from io import BytesIO
from typing import Final
from uuid import UUID

from pypdf import PageObject, PdfReader, PdfWriter
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from evidence.coordinates import PageTransform, PdfPoint, StoredPoint
from evidence.polygon import Polygon
from storage.hashing import content_key, sha256_stream
from storage.store import ArtifactStore, StoredArtifact
from verdict.finding import Finding
from verdict.outcomes import Outcome, is_abstention

PDF_CONTENT_TYPE: Final = "application/pdf"

#: Body text on the appended summary pages, in points.
LISTING_FONT_SIZE: Final = 9.0

#: Label text drawn beside a mark on the drawing, in points. Small on purpose: the drawing has to
#: stay readable underneath, and the full text of every finding is on the summary pages anyway.
MARK_FONT_SIZE: Final = 7.0


class ReportMode(StrEnum):
    """Who the render is for. `DESIGN_PRODUCT.md` §3.3."""

    INTERNAL = "internal"
    """Engine output, for the reviewer. Everything is shown, including abstentions."""

    VENDOR = "vendor"
    """Only reviewer-approved content. Reachable solely through `reports.publication`, which
    establishes the sign-off this module cannot."""


class PageMismatchError(ValueError):
    """Raised when a supplied transform does not describe the PDF page it is aimed at.

    Separate from `evidence.polygon.PolygonSpaceMismatchError` because the mistake is a different
    one: not two polygons from unrelated planes, but a page paired with another page's geometry.
    Both share the same rule — answer nothing rather than answer misleadingly.
    """


class VendorApprovalUnavailable(RuntimeError):
    """Raised on a vendor render that was not handed a `VendorClearance`.

    ADR-0010: no computed dimension reaches a vendor without sign-off. This module cannot establish
    sign-off itself — it holds no database session, and it is given finding *values*, which carry no
    row identity to match an approval against. So it refuses unless something that can establish it
    already has. That is `reports.publication.render_vendor_redline`, and refusing here is what
    makes it the only route rather than the recommended one.
    """


@dataclass(frozen=True, slots=True)
class VendorClearance:
    """Evidence that the content of a vendor render was signed off, and by whom.

    Deliberately not a boolean. A flag says a check happened somewhere; this says which approval it
    was, so the same fact that unlocks the render is the fact printed in it — a vendor holding the
    document can see whose sign-off it was issued under without asking.

    Built in `reports.publication` after every finding has been matched against the stored approval.
    Constructing one by hand to get past the gate is possible in the way that any Python object is,
    and pointless in the way that writing a false approval row would be: the document then names a
    person who did not sign it.
    """

    approval_id: UUID
    approved_by: str
    approved_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.approval_id, UUID):
            raise TypeError("approval_id must be a UUID")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise ValueError(
                "a clearance must name its approver. The report prints this as an attribution, so "
                "a blank one puts a document in front of a vendor claiming sign-off by nobody — "
                "which reads as approved and is not."
            )
        if not isinstance(self.approved_at, datetime):
            raise TypeError("approved_at must be a datetime")
        if self.approved_at.tzinfo is None:
            raise ValueError(
                "approved_at must be timezone-aware. A naive timestamp printed in a vendor document "
                "is a time in an unstated zone, and 'when was this approved?' is the question the "
                "line exists to answer."
            )


@dataclass(frozen=True, slots=True)
class RedlinePage:
    """One page of the PDF being redlined, and the transform that places evidence on it.

    `page` and `source_index` are separate because they answer different questions and are only
    incidentally equal. `page` is the page number evidence polygons are recorded against, inside
    their own document version. `source_index` is where that page sits in the PDF handed to this
    module — which differs the moment a redline covers a PDF assembled from more than one document.
    Collapsing them into one field would put marks on the wrong sheet of any assembled package.
    """

    document_version_id: UUID
    page: int
    source_index: int
    transform: PageTransform

    def __post_init__(self) -> None:
        if not isinstance(self.document_version_id, UUID):
            raise TypeError("document_version_id must be a UUID")
        for name, value in (("page", self.page), ("source_index", self.source_index)):
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} must be an integer")
            if value < 0:
                raise ValueError(f"{name} must be zero or greater")
        if not isinstance(self.transform, PageTransform):
            raise TypeError("transform must be a PageTransform")

    @property
    def key(self) -> tuple[UUID, int]:
        """What an evidence polygon is matched against: its document version and page."""
        return (self.document_version_id, self.page)


@dataclass(frozen=True, slots=True)
class RedlinePackage:
    """The revision being redlined: its identity, its pages, and the exact bytes to draw on.

    This is not `app.models.package.PackageRevision`. That row records a revision's lifecycle state
    and carries neither the source bytes nor the page transforms a redline needs, and taking it
    would tie rendering to a live database session for data it does not hold. `DESIGN_PRODUCT.md`
    §2 lists `reports/` as importing `verdict/`, `evidence/` and `storage/`; keeping the ORM out
    also keeps that table honest. The caller loads the revision, resolves its pages and passes what
    is needed.
    """

    package_revision_id: UUID
    source_pdf: bytes
    pages: tuple[RedlinePage, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.package_revision_id, UUID):
            raise TypeError("package_revision_id must be a UUID")
        if not isinstance(self.source_pdf, bytes):
            raise TypeError("source_pdf must be the exact bytes of the PDF being redlined")
        if not self.source_pdf:
            raise ValueError("source_pdf is empty; there is no drawing to mark up")
        if not isinstance(self.pages, tuple):
            raise TypeError("pages must be a tuple of RedlinePage values")
        if not self.pages:
            raise ValueError("a redline needs at least one page to place findings on")
        for entry in self.pages:
            if not isinstance(entry, RedlinePage):
                raise TypeError("pages must contain only RedlinePage values")
        if len({entry.key for entry in self.pages}) != len(self.pages):
            raise ValueError(
                "two pages claim the same document version and page number. One of them would "
                "silently take every mark belonging to the other."
            )
        if len({entry.source_index for entry in self.pages}) != len(self.pages):
            raise ValueError("two pages claim the same position in the source PDF")


@dataclass(frozen=True, slots=True)
class Unplaced:
    """A finding, or one of its evidence references, that could not be marked on a page.

    Carried out of the render and written onto the summary pages. It exists as a value rather than
    as a log line because "it is in the report" is the acceptance criterion, and a log nobody opens
    is indistinguishable from dropping it.
    """

    finding: Finding
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.finding, Finding):
            raise TypeError("finding must be a Finding")
        if not self.reason.strip():
            raise ValueError("an unplaced finding must say why it could not be marked")


@dataclass(frozen=True, slots=True)
class _OutcomeStyle:
    """How one outcome is drawn. §3.2: every outcome is visible, and none of them is 'nothing'."""

    stroke: tuple[float, float, float]
    fill: tuple[float, float, float] | None
    line_width: float
    dashed: bool


#: One entry per outcome, deliberately exhaustive. A missing entry would make that outcome render
#: as blank space, which §3.2 identifies as the failure mode the whole abstention design exists to
#: prevent. `tests/reports/test_redline.py` asserts the table covers `Outcome` completely.
OUTCOME_STYLES: Final[dict[Outcome, _OutcomeStyle]] = {
    Outcome.PASS: _OutcomeStyle((0.0, 0.45, 0.2), None, 0.75, False),
    Outcome.FAIL: _OutcomeStyle((0.8, 0.05, 0.05), (0.8, 0.05, 0.05), 2.0, False),
    Outcome.REVIEW_REQUIRED: _OutcomeStyle((0.85, 0.45, 0.0), (0.85, 0.45, 0.0), 2.0, False),
    Outcome.NOT_FOUND: _OutcomeStyle((0.15, 0.3, 0.6), None, 1.25, True),
    Outcome.NO_APPLICABLE_RULE: _OutcomeStyle((0.4, 0.2, 0.55), None, 1.25, True),
}


@dataclass(frozen=True, slots=True)
class _Located:
    """A decoded evidence reference: where on which page the operand was read."""

    document_version_id: UUID
    page: int
    points: tuple[StoredPoint, ...]


@dataclass(frozen=True, slots=True)
class _Undecodable:
    """An evidence reference that does not record a place on a page, and why not."""

    reason: str


@dataclass(frozen=True, slots=True)
class _Mark:
    """One finding, and the polygon it will be drawn at."""

    finding: Finding
    polygon: Polygon


def polygon_pdf_points(polygon: Polygon, transform: PageTransform) -> tuple[PdfPoint, ...]:
    """Return where a stored-space polygon sits in the page's PDF user space.

    The whole conversion is `PageTransform`'s, deliberately: `from_stored` to the rendered pixel
    grid, then `to_pdf` back to points from the page's bottom-left origin. Page rotation is handled
    inside those two calls, which is why nothing in this module branches on 90/180/270 to place a
    mark. See the module docstring for the one-pixel quantisation this route implies.
    """
    return tuple(transform.to_pdf(transform.from_stored(point)) for point in polygon.points)


def text_angle(rotation: int) -> int:
    """The angle label text must be drawn at so it reads upright to someone viewing the page.

    A PDF `/Rotate` turns the *rendered* page clockwise without touching the content stream, so
    text drawn horizontally in user space appears on its side on a rotated sheet. Turning the text
    the same way the viewer will turn the page cancels that out. Without this the marks land in the
    right place on a rotated drawing and are unreadable, which is a quieter kind of wrong.
    """
    if rotation not in (0, 90, 180, 270):
        raise ValueError("rotation must be one of 0, 90, 180 or 270")
    return rotation


def _turn(x: Decimal, y: Decimal, angle: int) -> tuple[Decimal, Decimal]:
    """Rotate a point about the origin by a quarter turn, exactly.

    Only multiples of 90 occur, so the rotation matrix has entries of 0, 1 and -1 and no
    trigonometry — and therefore no floating point — is involved.
    """
    if angle == 0:
        return (x, y)
    if angle == 90:
        return (-y, x)
    if angle == 180:
        return (-x, -y)
    return (y, -x)


def label_anchor(points: Sequence[PdfPoint], angle: int) -> PdfPoint:
    """The corner of a mark a label should start from, so it sits above-left as the page is read.

    "Above" and "left" are properties of the finished, rotated page, not of PDF user space, and
    they point at a different corner of the same box for each rotation. Working in the rotated
    frame and mapping back keeps that as one calculation rather than four hand-written cases,
    which is where the transposition mistakes live.
    """
    inverse = (360 - angle) % 360
    turned = [_turn(point.x, point.y, inverse) for point in points]
    anchor = _turn(min(x for x, _ in turned), max(y for _, y in turned), angle)
    return PdfPoint(anchor[0], anchor[1])


def render_redline(
    package: RedlinePackage,
    findings: Sequence[Finding],
    mode: ReportMode,
    store: ArtifactStore,
    *,
    clearance: VendorClearance | None = None,
) -> StoredArtifact:
    """Overlay the findings onto the source pages and store the result.

    The original page content is preserved: each source page is kept and a vector overlay merged
    onto it. Every finding either appears as a mark on a page or is written out on the appended
    summary pages — no finding is silently absent from both.

    Raises `PageMismatchError` if a page's transform does not describe that PDF page, and
    `VendorApprovalUnavailable` for `ReportMode.VENDOR` without a `VendorClearance`. Both are
    refusals rather than degraded output; see the module docstring.

    `clearance` is meaningful only in vendor mode, and supplying one for an internal render is
    refused rather than ignored — a caller who passed it believed it was doing something.
    """
    if not isinstance(package, RedlinePackage):
        raise TypeError("package must be a RedlinePackage")
    if isinstance(findings, str) or not isinstance(findings, Sequence):
        raise TypeError("findings must be a sequence of Finding values")
    for finding in findings:
        if not isinstance(finding, Finding):
            raise TypeError("findings must contain only Finding values")
    if not isinstance(mode, ReportMode):
        raise TypeError("mode must be a ReportMode")
    if not isinstance(store, ArtifactStore):
        raise TypeError("store must implement the ArtifactStore protocol")

    if clearance is not None and not isinstance(clearance, VendorClearance):
        raise TypeError("clearance must be a VendorClearance")
    if mode is ReportMode.VENDOR and clearance is None:
        raise VendorApprovalUnavailable(
            "a vendor redline cannot be produced from unapproved content. ADR-0010 requires "
            "reviewer sign-off before any computed dimension reaches a vendor, and nothing here "
            "can establish that these findings were signed off. Use "
            "`reports.publication.render_vendor_redline`, which reads the approval and checks "
            "every finding against it, or render the internal report for review."
        )
    if mode is not ReportMode.VENDOR and clearance is not None:
        raise ValueError(
            "clearance applies to a vendor render. An internal report is engine output and is not "
            "issued under anyone's sign-off; printing an approval on it would say otherwise."
        )

    reader = PdfReader(BytesIO(package.source_pdf))
    _check_pages_describe_the_pdf(package, reader)

    marks, unplaced, marked = _place(package, findings)
    document = _compose(package, reader, marks, findings, marked, unplaced, clearance)

    digest, _ = sha256_stream(BytesIO(document))
    key = content_key(f"redlines/{package.package_revision_id}/{mode.value}", digest, suffix=".pdf")
    return store.put(key, BytesIO(document), content_type=PDF_CONTENT_TYPE)


# ---------------------------------------------------------------------------
# Checking the caller handed us geometry that belongs to this PDF
# ---------------------------------------------------------------------------


def _exact(value: object) -> Decimal:
    """Read a PDF number as an exact Decimal.

    `str()` of a pypdf number gives the decimal text as written in the file, so this is a
    transcription rather than a conversion — no binary rounding enters the comparison.
    """
    return Decimal(str(value))


def _check_pages_describe_the_pdf(package: RedlinePackage, reader: PdfReader) -> None:
    """Refuse a transform that belongs to a different page than the one it is aimed at."""
    available = len(reader.pages)
    for entry in package.pages:
        if entry.source_index >= available:
            raise PageMismatchError(
                f"page {entry.page} is mapped to position {entry.source_index} of a PDF with "
                f"{available} page(s)"
            )
        source = reader.pages[entry.source_index]
        # Normalised because `/Rotate` may legally be negative or a multiple beyond one turn, and
        # `PageTransform` only accepts 0, 90, 180 and 270. A raw -90 would report a mismatch
        # against a transform that in fact describes the page correctly.
        rotation = source.rotation % 360
        if rotation != entry.transform.rotation:
            raise PageMismatchError(
                f"position {entry.source_index} of the PDF is rotated {rotation} degrees but its "
                f"transform says {entry.transform.rotation}. Every mark on this page would be "
                "placed against the wrong edge."
            )
        for name, box, expected in (
            ("media box", source.mediabox, entry.transform.media_box),
            ("crop box", source.cropbox, entry.transform.crop_box),
        ):
            actual = tuple(_exact(value) for value in box)
            if actual != expected:
                raise PageMismatchError(
                    f"the {name} of position {entry.source_index} is {actual}, but its transform "
                    f"was built for {expected}. The transform describes a different page."
                )


# ---------------------------------------------------------------------------
# Turning evidence references back into places on a page
# ---------------------------------------------------------------------------


def _decode_reference(reference: str) -> _Located | _Undecodable:
    """Recover the page and polygon an evidence reference records, or say why it cannot.

    `evidence/gate.py` seals provenance as deterministic JSON with exact decimal strings.
    `Finding.evidence_refs` is typed as plain strings, though, and other producers put opaque
    identifiers there, so anything that is not that JSON is an honest "no geometry recorded"
    rather than an error.
    """
    try:
        decoded = json.loads(reference)
    except (ValueError, TypeError):
        return _Undecodable(
            "its evidence reference is not a recorded page and polygon, so there is no place on "
            "the drawing to point at"
        )
    if not isinstance(decoded, dict):
        return _Undecodable("its evidence reference does not record a page and polygon")

    if decoded.get("space") != "stored":
        return _Undecodable(
            f"its evidence reference is in the {decoded.get('space')!r} coordinate space rather "
            "than the stored space a page transform can place"
        )
    try:
        document_version_id = UUID(str(decoded["document_version_id"]))
        page = decoded["page"]
        raw_points = decoded["polygon"]
    except (KeyError, ValueError) as error:
        return _Undecodable(f"its evidence reference is incomplete or malformed ({error})")

    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return _Undecodable("its evidence reference does not name a page number")
    if not isinstance(raw_points, list):
        return _Undecodable("its evidence reference does not carry a polygon")

    points: list[StoredPoint] = []
    for pair in raw_points:
        if not isinstance(pair, list) or len(pair) != 2:
            return _Undecodable("its recorded polygon has a malformed point")
        try:
            points.append(StoredPoint(Decimal(str(pair[0])), Decimal(str(pair[1]))))
        except (InvalidOperation, ValueError):
            return _Undecodable("its recorded polygon has a coordinate that is not a number")

    return _Located(document_version_id, page, tuple(points))


def _place(
    package: RedlinePackage, findings: Sequence[Finding]
) -> tuple[dict[int, list[_Mark]], list[Unplaced], int]:
    """Sort every finding into marks on pages and a list of what could not be marked.

    Every finding leaves this function accounted for. One with several evidence references is
    marked once per reference that places, and any reference that does not place is still listed —
    a finding shown at two of its three operands is not fully shown, and saying so costs one line.

    The third return value is how many findings got at least one mark, counted here rather than
    inferred later: two findings can be equal without being the same finding, and a count that
    collapsed them would understate what is on the drawing.
    """
    by_key = {entry.key: entry for entry in package.pages}
    marks: dict[int, list[_Mark]] = {entry.source_index: [] for entry in package.pages}
    unplaced: list[Unplaced] = []
    marked = 0

    for finding in findings:
        if not finding.evidence_refs:
            unplaced.append(
                Unplaced(
                    finding,
                    "it carries no evidence reference. A check that abstained before reading an "
                    "operand has no place on the drawing to point at.",
                )
            )
            continue

        placed_here = 0
        for reference in finding.evidence_refs:
            located = _decode_reference(reference)
            if isinstance(located, _Undecodable):
                unplaced.append(Unplaced(finding, located.reason))
                continue

            entry = by_key.get((located.document_version_id, located.page))
            if entry is None:
                unplaced.append(
                    Unplaced(
                        finding,
                        f"its evidence is on page {located.page} of document version "
                        f"{located.document_version_id}, which is not one of the pages in this "
                        "redline",
                    )
                )
                continue

            try:
                polygon = Polygon(
                    points=located.points,
                    space="stored",
                    document_version_id=located.document_version_id,
                    page=located.page,
                )
            except (TypeError, ValueError) as error:
                unplaced.append(
                    Unplaced(finding, f"its recorded polygon is not a usable page region ({error})")
                )
                continue

            marks[entry.source_index].append(_Mark(finding, polygon))
            placed_here += 1

        if placed_here:
            marked += 1

    return marks, unplaced, marked


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _compose(
    package: RedlinePackage,
    reader: PdfReader,
    marks: dict[int, list[_Mark]],
    findings: Sequence[Finding],
    marked: int,
    unplaced: Sequence[Unplaced],
    clearance: VendorClearance | None,
) -> bytes:
    """Merge the overlays onto the original pages and append the summary pages.

    Every source page is carried over, marked or not. Dropping unmarked pages would give a vendor
    a document that looks like the whole drawing set and is not.
    """
    # Cloned into the writer before anything is merged: pypdf only supports merging into a page it
    # owns, and modifying a reader's page in place is deprecated and documented as unreliable.
    writer = PdfWriter(clone_from=reader)
    by_index = {entry.source_index: entry for entry in package.pages}

    for index, page in enumerate(writer.pages):
        page_marks = marks.get(index) or ()
        if page_marks:
            page.merge_page(_overlay(by_index[index], page_marks))

    pages_with_marks = frozenset(index for index, page_marks in marks.items() if page_marks)
    summary = _listing(package, findings, marked, unplaced, pages_with_marks, clearance)
    for listing in PdfReader(BytesIO(summary)).pages:
        writer.add_page(listing)

    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _canvas(width: Decimal, height: Decimal) -> tuple[Canvas, BytesIO]:
    """A ReportLab canvas with timestamps suppressed, so two identical renders are identical.

    `AGENTS.md` §2.7 wants artifacts reproducible. Without `invariant` the creation date lands in
    the file and the same inputs produce different bytes, which turns a re-render into a new
    artifact for no reason a reader could see.
    """
    buffer = BytesIO()
    return Canvas(buffer, pagesize=(float(width), float(height)), invariant=1), buffer


def _overlay(entry: RedlinePage, marks: Sequence[_Mark]) -> PageObject:
    """Build the transparent page of marks that is merged onto one source page."""
    left, bottom, right, top = entry.transform.media_box
    canvas, buffer = _canvas(right - left, top - bottom)
    angle = text_angle(entry.transform.rotation)

    for mark in marks:
        _draw_mark(canvas, mark, entry.transform, angle)

    canvas.showPage()
    canvas.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _draw_mark(canvas: Canvas, mark: _Mark, transform: PageTransform, angle: int) -> None:
    """Outline the evidence region and label it with the rule and the outcome."""
    style = OUTCOME_STYLES[mark.finding.outcome]
    points = polygon_pdf_points(mark.polygon, transform)

    canvas.saveState()
    canvas.setStrokeColorRGB(*style.stroke)
    canvas.setLineWidth(style.line_width)
    if style.dashed:
        canvas.setDash(4, 3)

    path = canvas.beginPath()
    path.moveTo(float(points[0].x), float(points[0].y))
    for point in points[1:]:
        path.lineTo(float(point.x), float(point.y))
    path.close()

    if style.fill is not None:
        canvas.setFillColorRGB(*style.fill)
        canvas.setFillAlpha(0.12)
        canvas.drawPath(path, stroke=1, fill=1)
        canvas.setFillAlpha(1.0)
    else:
        canvas.drawPath(path, stroke=1, fill=0)
    canvas.restoreState()

    _draw_label(canvas, mark, points, style, angle)


def _draw_label(
    canvas: Canvas,
    mark: _Mark,
    points: Sequence[PdfPoint],
    style: _OutcomeStyle,
    angle: int,
) -> None:
    """Write the rule id and outcome beside the mark, turned to read upright on a rotated page.

    Only the rule and the outcome go on the drawing. The reason is on the summary pages instead:
    covering the line work with a paragraph would hide the very thing the reviewer is being asked
    to look at.
    """
    label = f"{mark.finding.rule_id} · {mark.finding.outcome.value}"
    anchor = label_anchor(points, angle)

    canvas.saveState()
    canvas.translate(float(anchor.x), float(anchor.y))
    canvas.rotate(angle)
    canvas.setFont("Helvetica-Bold", MARK_FONT_SIZE)

    width = canvas.stringWidth(label, "Helvetica-Bold", MARK_FONT_SIZE)
    canvas.setFillColorRGB(1.0, 1.0, 1.0)
    canvas.setFillAlpha(0.75)
    canvas.rect(-1.0, 1.0, width + 2.0, MARK_FONT_SIZE + 1.0, stroke=0, fill=1)
    canvas.setFillAlpha(1.0)

    canvas.setFillColorRGB(*style.stroke)
    canvas.drawString(0.0, MARK_FONT_SIZE * 0.35 + 1.0, label)
    canvas.restoreState()


# ---------------------------------------------------------------------------
# The summary pages — where a finding that could not be marked is still reported
# ---------------------------------------------------------------------------


def _listing_size(package: RedlinePackage) -> tuple[Decimal, Decimal]:
    """Match the summary pages to the visible size of the drawing they follow.

    Taken from the first drawing page rather than a fixed paper size, so a set of D-size sheets
    does not end with a letter-size afterthought. On a rotated page the visible sheet is the
    transposed one.
    """
    entry = min(package.pages, key=lambda page: page.source_index)
    left, bottom, right, top = entry.transform.crop_box
    width, height = right - left, top - bottom
    return (height, width) if entry.transform.rotation in (90, 270) else (width, height)


@dataclass(frozen=True, slots=True)
class DerivedExpectation:
    """A value the engine calculated, with the arithmetic that produced it.

    Not a specification, and the report says so on every line. A bare number in a document a vendor
    builds from is read as an instruction however clearly a heading two pages earlier said
    otherwise — which is precisely the reading ADR-0010 exists to prevent.
    """

    rule_id: str
    name: str
    value: str
    calculation: str
    """Plain English: which operation, over which operands and their values."""


def derived_expectations(finding: Finding) -> tuple[DerivedExpectation, ...]:
    """The values this finding's calculation produced, rather than read off a drawing.

    Read from `trace.intermediates`, which is where a derivation records what it computed. The
    operands are named with their values so the arithmetic can be repeated by hand: an expectation
    a reviewer cannot check is the thing ADR-0010 is trying to keep out of a vendor's hands, whether
    or not somebody signed it.

    An abstention has no trace and therefore no derived values — nothing was calculated, and
    returning an empty calculation would suggest something was.

    Lives here rather than in `reports.publication` because that module imports this one; the other
    direction would be a cycle. `reports.publication` re-exports it, so the documented entry point
    for vendor publication stays one module.
    """
    if not isinstance(finding, Finding):
        raise TypeError("finding must be a Finding")
    if finding.trace is None:
        return ()

    trace = finding.trace
    inputs = (
        ", ".join(f"{operand.name} = {operand.value}" for operand in trace.operands)
        or "no named operands"
    )
    return tuple(
        DerivedExpectation(
            rule_id=finding.rule_id,
            name=name,
            value=str(value),
            calculation=f"calculated by {trace.operation} from {inputs}",
        )
        for name, value in trace.intermediates
    )


def _derived_section(
    findings: Sequence[Finding],
    line: Callable[..., None],
    paragraph: Callable[..., None],
) -> None:
    """Write out every calculated value in the report, labelled as calculated.

    Emitted even when there are none, for the same reason the unplaced section is: a reader should
    be told that the report contains no derived numbers rather than having to infer it from an
    absent heading.
    """
    derived = [expectation for finding in findings for expectation in derived_expectations(finding)]

    line("Derived expectations", font="Helvetica-Bold", size=12.0)
    paragraph(
        "These values were CALCULATED by the checks below, not measured from the drawing and not "
        "issued as instructions. They are shown with their arithmetic so they can be checked. Do "
        "not build to them: a dimension to build to comes from the design, not from this report."
    )
    line("")
    if not derived:
        paragraph("None. No value in this report was calculated; every figure was read or given.")
    for expectation in derived:
        paragraph(f"{expectation.rule_id} — {expectation.name} (DERIVED): {expectation.value}")
        paragraph(expectation.calculation, "    ")
    line("")


def _listing(
    package: RedlinePackage,
    findings: Sequence[Finding],
    marked: int,
    unplaced: Sequence[Unplaced],
    pages_with_marks: frozenset[int],
    clearance: VendorClearance | None,
) -> bytes:
    """Render the appended pages that account for every finding not on the drawing.

    Emitted whether or not anything went unplaced. When nothing did, it says so — a reader should
    be told that nothing was left off rather than having to conclude it from an absent page.
    """
    width, height = _listing_size(package)
    canvas, buffer = _canvas(width, height)
    margin = 54.0
    cursor = float(height) - margin
    columns = max(40, int((float(width) - 2 * margin) / (LISTING_FONT_SIZE * 0.55)))

    def line(text: str, *, font: str = "Helvetica", size: float = LISTING_FONT_SIZE) -> None:
        nonlocal cursor
        if cursor < margin:
            canvas.showPage()
            cursor = float(height) - margin
        canvas.setFont(font, size)
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        canvas.drawString(margin, cursor, text)
        cursor -= size * 1.6

    def paragraph(text: str, indent: str = "") -> None:
        for wrapped in textwrap.wrap(text, columns) or [""]:
            line(f"{indent}{wrapped}")

    total_findings = len(findings)
    line("Redline summary", font="Helvetica-Bold", size=LISTING_FONT_SIZE * 1.8)
    paragraph(
        f"{total_findings} finding(s) in this report for package revision "
        f"{package.package_revision_id}. {marked} marked on a drawing page. "
        f"{total_findings - marked} not marked, and listed below."
    )
    line("")

    if clearance is not None:
        line("Approved by", font="Helvetica-Bold", size=12.0)
        paragraph(
            f"{clearance.approved_by} on {clearance.approved_at.isoformat()} "
            f"(approval {clearance.approval_id})."
        )
        paragraph(
            "Every finding in this document was covered by that sign-off. A person accepted "
            "responsibility for this content; it is not raw engine output."
        )
        line("")

    _derived_section(findings, line, paragraph)

    line("Page coverage", font="Helvetica-Bold", size=12.0)
    paragraph(
        "Every source page is listed. A page with no finding is not thereby approved; it means "
        "this report produced no page-local finding to draw there."
    )
    line("")
    for entry in sorted(package.pages, key=lambda item: item.source_index):
        if entry.source_index in pages_with_marks:
            paragraph(f"Page {entry.page + 1}: one or more findings are marked on this page.")
        else:
            paragraph(
                f"Page {entry.page + 1}: NO FINDINGS WERE PRODUCED OR PLACED ON THIS PAGE. "
                "This is not an approval."
            )
    line("")

    abstentions = [finding for finding in findings if is_abstention(finding.outcome)]
    line("What was not checked or could not be decided", font="Helvetica-Bold", size=12.0)
    paragraph(
        "These checks did not reach PASS or FAIL. They require a reviewer or rulebook action and "
        "must not be read as approval."
    )
    line("")
    if not abstentions:
        paragraph("None. Every finding in this report reached PASS or FAIL.")
    for finding in abstentions:
        paragraph(finding.summary())
        line("")

    not_checked = [item for item in unplaced if item.finding.outcome is Outcome.NO_APPLICABLE_RULE]
    others = [item for item in unplaced if item.finding.outcome is not Outcome.NO_APPLICABLE_RULE]

    line("Findings not marked on any drawing page", font="Helvetica-Bold", size=12.0)
    paragraph(
        "A finding is listed here because there was nowhere on the drawing to put it, not because "
        "it matters less. Nothing has been left out of this report."
    )
    line("")
    if not unplaced:
        paragraph("None. Every finding in this report is marked on a drawing page.")
    for item in others:
        paragraph(item.finding.summary())
        paragraph(f"why it is not on the drawing: {item.reason}", "    ")
        line("")

    if not_checked:
        line("")
        line("What was not checked", font="Helvetica-Bold", size=12.0)
        paragraph(
            "No published rule covers these. They were not examined and were not approved — "
            "the distinction matters, and this section exists so it is not mistaken for silence."
        )
        line("")
        for item in not_checked:
            paragraph(item.finding.summary())
            paragraph(f"why it is not on the drawing: {item.reason}", "    ")
            line("")

    canvas.showPage()
    canvas.save()
    return buffer.getvalue()
