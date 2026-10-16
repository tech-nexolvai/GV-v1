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
from typing import Any, Final
from uuid import UUID

from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    RectangleObject,
)
from reportlab.pdfgen.canvas import Canvas  # type: ignore[import-untyped]

from evidence.coordinates import PageTransform, PdfPoint, StoredPoint
from evidence.polygon import Polygon
from reports.signed_review import ReviewCover, SignedReview, with_review_pdf
from storage.hashing import content_key, sha256_stream
from storage.store import ArtifactStore, StoredArtifact
from verdict.finding import Finding
from verdict.outcomes import Outcome, Severity, is_abstention
from verdict.trace import CalculationTrace
from workflow.changed_values import ChangedValues

PDF_CONTENT_TYPE: Final = "application/pdf"

#: Body text on the appended summary pages, in points.
LISTING_FONT_SIZE: Final = 9.0

#: Label text drawn beside a mark on the drawing, in points. Small on purpose: the drawing has to
#: stay readable underneath, and the full text of every finding is on the summary pages anyway.
MARK_FONT_SIZE: Final = 7.0


class LabelLayout(StrEnum):
    """How a mark is named on the drawing (QA 2026-10-16).

    * `AS_BEFORE`: the words beside the cloud, as every cloud PDF before the final QA was drawn
      (a sign-off snapshot of format 1 or 2 is drawn this way, byte for byte).
    * `TAGS`: only the short tag (01, A1, R1) beside the cloud, in a free spot off the page's own
      text, the other clouds and the tags already placed; the full words are in the margin note
      and the summary. The draft and every snapshot of format 3 or later.
    """

    AS_BEFORE = "as_before"
    TAGS = "tags"


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
class RedlineFinding:
    """The stored facts needed to place a finding without recomputing its verdict.

    ``Finding`` is the engine's in-memory value and normally carries a complete calculation trace.
    The output stage, however, reads an immutable persisted finding after the decision has already
    happened.  Rebuilding measurements by parsing its display trace would violate ADR-0001.  This
    narrow presentation value therefore carries the stored verdict facts unchanged.  It is not an
    alternate verdict representation: it cannot execute a check, and its only job is to label an
    already-recorded mark and summary entry.
    """

    rule_id: str
    outcome: Outcome
    severity: Severity
    reason: str
    snapshot_id: str
    engine_version: str
    trace: CalculationTrace | None = None
    calculation_available: bool = False
    """The stored finding had deterministic calculation detail, kept in the findings report.

    The redline's drawing labels deliberately do not reconstruct those display values.  This flag
    prevents its summary from claiming no calculation happened when a persisted presentation value
    intentionally omits the calculation trace.
    """
    evidence_refs: tuple[str, ...] = ()
    scope_label: str | None = None
    comparison: str | None = None
    notes: tuple[str, ...] = ()
    not_checked: bool = False
    """The check could not run (decision 1, 2026-10-15): drawn `❍ NOT CHECKED`, never with its
    stored outcome as if it had been judged (#1363). The stored verdict is unchanged."""

    def __post_init__(self) -> None:
        if not self.rule_id.strip():
            raise ValueError("a redline finding must name the rule that produced it")
        if not self.snapshot_id.strip():
            raise ValueError("a redline finding must record its rule snapshot")

    def summary(self) -> str:
        head = f"{self.rule_id}: {self.outcome.value} [{self.severity.value}] — {self.reason}"
        return f"{head} ({self.snapshot_id[:15]}...)"


type RenderableFinding = Finding | RedlineFinding


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

    finding: RenderableFinding
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.finding, (Finding, RedlineFinding)):
            raise TypeError("finding must be a Finding or RedlineFinding")
        if not self.reason.strip():
            raise ValueError("an unplaced finding must say why it could not be marked")


#: How a check that could not run is printed (#1340, #1363; Anant, 2026-10-15).
NOT_CHECKED: Final = "NOT CHECKED"

#: The glyph beside `NOT_CHECKED`. Never the colour alone.
NOT_CHECKED_GLYPH: Final = "❍"

#: The glyph beside a dimension the vendor drew differently from the architect. Never colour alone.
ARCHITECT_DIFFERENT_GLYPH: Final = "\u2717"


class ReviewerMarkKind(StrEnum):
    """What a person did, as drawn on the cloud PDF (#1363). Each has its own glyph."""

    CONFIRMED = "confirmed"
    """A person agreed with the AI's reading: "✓ Confirmed by AB". Not a check result."""

    FIXED = "fixed"
    """A person fixed the AI's reading: "✎ Fixed by AB: AI read X → Y"."""

    ANSWERED = "answered"
    """A person's answer: walls chosen, a page answered, a result checked by hand."""

    CHECKED_WRONG = "checked_wrong"
    """A person checked a result by hand and found it wrong on the drawing."""

    NOT_CONFIRMED = "not_confirmed"
    """A question nobody answered at sign-off (owner decision 2026-10-16: every question is
    optional): "Not confirmed by a person — <what> (AI: <best guess>)", drawn grey."""


#: One glyph per kind, deliberately exhaustive: every mark is a glyph plus words, never colour alone.
REVIEWER_MARK_GLYPHS: Final[dict[ReviewerMarkKind, str]] = {
    ReviewerMarkKind.CONFIRMED: "✓",
    ReviewerMarkKind.FIXED: "✎",
    ReviewerMarkKind.ANSWERED: "✍",
    ReviewerMarkKind.CHECKED_WRONG: "✗",
    ReviewerMarkKind.NOT_CONFIRMED: "❖",
}

#: The colour of each kind: only the second signal, after the glyph and the words.
_REVIEWER_STROKES: Final[dict[ReviewerMarkKind, tuple[float, float, float]]] = {
    ReviewerMarkKind.CONFIRMED: (0.05, 0.3, 0.55),
    ReviewerMarkKind.FIXED: (0.45, 0.15, 0.55),
    ReviewerMarkKind.ANSWERED: (0.25, 0.3, 0.4),
    ReviewerMarkKind.CHECKED_WRONG: (0.8, 0.05, 0.05),
    ReviewerMarkKind.NOT_CONFIRMED: (0.5, 0.5, 0.5),
}

#: Glyphs drawn in a standard font that has them, so text extraction reads them back as written.
_GLYPH_FONTS: Final[dict[str, str]] = {
    **{glyph: "ZapfDingbats" for glyph in REVIEWER_MARK_GLYPHS.values()},
    NOT_CHECKED_GLYPH: "ZapfDingbats",
    ARCHITECT_DIFFERENT_GLYPH: "ZapfDingbats",
    "→": "Symbol",
}


@dataclass(frozen=True, slots=True)
class ReviewerMark:
    """One person's confirm, fix or answer, as the cloud PDF shows it (#1363).

    A presentation value, like `RedlineFinding`: the words were written from stored records by the
    caller (the frozen sign-off snapshot, or the live records for a draft), and nothing here reads
    or decides anything. `evidence_ref` is a code-recorded place in the same JSON shape a finding's
    evidence carries; with none, `unplaced_reason` says why, and the mark is listed instead.
    """

    kind: ReviewerMarkKind
    words: str
    subject: str
    details: tuple[str, ...] = ()
    evidence_ref: str | None = None
    unplaced_reason: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ReviewerMarkKind):
            raise TypeError("kind must be a ReviewerMarkKind")
        if not self.words.strip():
            raise ValueError("a reviewer mark must say what the person did")
        if not self.subject.strip():
            raise ValueError("a reviewer mark must say what it is about")

    @property
    def text(self) -> str:
        """The glyph and the words, as drawn."""
        return f"{REVIEWER_MARK_GLYPHS[self.kind]} {self.words}"


class ArchitectAnswer(StrEnum):
    """A person's answer on a "different" architect compare result."""

    AGREED = "agreed"
    """"Agree: vendor is wrong": still clouded; the person's own mark is a reviewer mark."""

    NOT_A_DIFFERENCE = "not_a_difference"
    """"Not a difference": no cloud, listed on the summary with who said so."""


@dataclass(frozen=True, slots=True)
class ArchitectCompareCounts:
    """How every architect dimension of the revision came out, for the summary page's one line."""

    same: int
    different: int
    for_a_person: int
    by_a_rule: int

    def __post_init__(self) -> None:
        for value in (self.same, self.different, self.for_a_person, self.by_a_rule):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("architect compare counts are whole numbers, never negative")

    @property
    def line(self) -> str:
        return (
            f"Compared with the architect: {self.same} same \u00b7 {self.different} different "
            f"\u00b7 {self.for_a_person} for a person \u00b7 {self.by_a_rule} by a rule"
        )


@dataclass(frozen=True, slots=True)
class AiReviewCounts:
    """How the AI page review's rows came out (`app/review/ai_review.py`), for the summary's line.

    The AI read both drawings and code decided each row; `unsure` counts the rows the AI could not
    settle, a number hidden under markup ("unreadable") included, so no row leaves the line."""

    same: int
    different: int
    not_shown: int
    unsure: int

    def __post_init__(self) -> None:
        for value in (self.same, self.different, self.not_shown, self.unsure):
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError("AI page review counts are whole numbers, never negative")

    @property
    def line(self) -> str:
        return (
            f"Compared with the architect (AI read): {self.same} same \u00b7 {self.different} "
            f"different \u00b7 {self.not_shown} not shown \u00b7 {self.unsure} unsure"
        )


#: Printed under the AI page review's counts line: the differences come from an AI reading, and
#: no person answered them one by one (decision 2026-10-16), so the reader checks the drawings.
AI_REVIEW_NOTE: Final = "Differences found by the AI reading; check against the drawings."


@dataclass(frozen=True, slots=True)
class ArchitectDifference:
    """One architect dimension the vendor drew differently (direct compare), as the PDF shows it.

    A presentation value, like `ReviewerMark`: code decided "different" and stored it
    (`app/review/architect_compare.py`); nothing here reads or decides anything. Both numbers are
    as printed. `evidence_ref` is the vendor dimension's stored place, in the JSON shape a
    finding's evidence carries; with none, `unplaced_reason` says why, and it is listed instead.
    """

    architect_text: str
    vendor_text: str
    subject: str
    evidence_ref: str | None = None
    unplaced_reason: str | None = None
    answer: ArchitectAnswer | None = None
    answered_by: str | None = None
    measures: str | None = None
    """What the dimension measures, as the AI page review says it ("overall width"); `None` for a
    direct-compare result, whose words are then exactly as before."""
    ai_read: bool = False
    """An AI page review row (decision 2026-10-16): worded "Needs: X (per architect: what) —
    Vendor drew: Y". False for a direct-compare result, whose words are exactly as before."""

    def __post_init__(self) -> None:
        if not self.architect_text.strip() or not self.vendor_text.strip():
            raise ValueError("an architect difference must say both printed numbers")
        if not self.subject.strip():
            raise ValueError("an architect difference must say where it is")
        if self.evidence_ref is None and not (self.unplaced_reason or "").strip():
            raise ValueError("an architect difference has a recorded place or says why not")
        if self.answer is not None and not (self.answered_by or "").strip():
            raise ValueError("a person's answer on an architect difference names the person")

    @property
    def words(self) -> str:
        """ "✗ Architect: Y — vendor: X", as drawn and listed; an AI page review row says
        "✗ Needs: Y (per architect: what it measures) — Vendor drew: X"."""
        measures = (self.measures or "").strip()
        if self.ai_read:
            what = f" (per architect: {measures})" if measures else ""
            return (
                f"{ARCHITECT_DIFFERENT_GLYPH} Needs: {self.architect_text}{what} \u2014 "
                f"Vendor drew: {self.vendor_text}"
            )
        words = (
            f"{ARCHITECT_DIFFERENT_GLYPH} Architect: {self.architect_text} \u2014 "
            f"vendor: {self.vendor_text}"
        )
        return f"{words} ({measures})" if measures else words


@dataclass(frozen=True, slots=True)
class ArchitectCompare:
    """The direct compare, or the AI page review, for one render: the counts, and every
    "different" result. The two are never drawn together (`workflow/redline_outputs.py`)."""

    counts: ArchitectCompareCounts | AiReviewCounts
    different: tuple[ArchitectDifference, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.counts, ArchitectCompareCounts | AiReviewCounts):
            raise TypeError("counts must be ArchitectCompareCounts or AiReviewCounts")
        if any(not isinstance(item, ArchitectDifference) for item in self.different):
            raise TypeError("different must hold ArchitectDifference values")


def review_cover_for(
    architect: ArchitectCompare | None,
    *,
    left_out_rules: frozenset[str] = frozenset(),
    left_out_note: str | None = None,
) -> ReviewCover:
    """The signed cover's additions (format 3): the compare section as the draft's front page
    words it, every difference listed, and the rules left out. The cover is plain Helvetica,
    which has no ✗: the heading says "different" instead."""
    if architect is None:
        return ReviewCover(None, (), left_out_rules, left_out_note)
    ai = isinstance(architect.counts, AiReviewCounts)
    title = (
        f"Compared with the architect{' (AI read)' if ai else ''} "
        f"({len(architect.different)} different)"
    )
    lines = [architect.counts.line, *([AI_REVIEW_NOTE] if ai else [])]
    if not architect.different:
        lines.append("None. No architect dimension was found different from the vendor's.")
    lines.extend(
        f"A{number} {item.words.removeprefix(f'{ARCHITECT_DIFFERENT_GLYPH} ')} \u2014 {item.subject}"
        for number, item in enumerate(architect.different, start=1)
    )
    return ReviewCover(title, tuple(lines), left_out_rules, left_out_note)


@dataclass(frozen=True, slots=True)
class _ArchitectPlaced:
    """A "different" result, its number (A1, A2, ...) and the polygon its cloud is drawn at."""

    item: ArchitectDifference
    number: int
    polygon: Polygon


@dataclass(frozen=True, slots=True)
class _ReviewerPlaced:
    """A reviewer mark, its number on the summary, and the polygon it is drawn at."""

    mark: ReviewerMark
    number: int
    polygon: Polygon


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
    Outcome.FAIL: _OutcomeStyle((0.8, 0.05, 0.05), None, 1.25, False),
    Outcome.REVIEW_REQUIRED: _OutcomeStyle((0.85, 0.45, 0.0), None, 1.25, False),
    Outcome.NOT_FOUND: _OutcomeStyle((0.15, 0.3, 0.6), None, 1.25, True),
    Outcome.NO_APPLICABLE_RULE: _OutcomeStyle((0.4, 0.2, 0.55), None, 1.25, True),
}

#: A check that could not run: grey and dashed, with `❍ NOT CHECKED` beside it (#1363).
NOT_CHECKED_STYLE: Final = _OutcomeStyle((0.4, 0.4, 0.4), None, 1.25, True)


def _not_checked(finding: RenderableFinding) -> bool:
    return isinstance(finding, RedlineFinding) and finding.not_checked


def _style(finding: RenderableFinding) -> _OutcomeStyle:
    return NOT_CHECKED_STYLE if _not_checked(finding) else OUTCOME_STYLES[finding.outcome]


def _result_words(finding: RenderableFinding) -> str:
    """The stored outcome, or `❍ NOT CHECKED` for a check that could not run."""
    if _not_checked(finding):
        return f"{NOT_CHECKED_GLYPH} {NOT_CHECKED}"
    return finding.outcome.value


def _runs(text: str, font: str) -> list[tuple[str, str]]:
    """Split text into (font, segment) runs: each glyph in a font that has it, the rest in `font`."""
    runs: list[tuple[str, str]] = []
    for character in text:
        wanted = _GLYPH_FONTS.get(character, font)
        if runs and runs[-1][0] == wanted:
            runs[-1] = (wanted, runs[-1][1] + character)
        else:
            runs.append((wanted, character))
    return runs


def _has_glyph(text: str) -> bool:
    return any(character in _GLYPH_FONTS for character in text)


def _draw_text(canvas: Canvas, x: float, y: float, text: str, font: str, size: float) -> None:
    """Draw text whose glyphs (✓ ✎ ✍ ✗ ❍ →) need another standard font, one run at a time."""
    cursor = x
    for run_font, segment in _runs(text, font):
        canvas.setFont(run_font, size)
        canvas.drawString(cursor, y, segment)
        cursor += canvas.stringWidth(segment, run_font, size)


def _text_width(text: str, font: str, size: float) -> float:
    """How wide `text` is drawn by `_draw_text`, its glyphs in their own fonts."""
    from reportlab.pdfbase.pdfmetrics import stringWidth  # type: ignore[import-untyped]

    return float(
        sum(stringWidth(segment, run_font, size) for run_font, segment in _runs(text, font))
    )


def _fitted(text: str, font: str, size: float, limit: float) -> tuple[str, ...]:
    """`text` as one line when it fits `limit` points; else wrapped at spaces onto lines that do,
    each after the first indented (QA 2026-10-16: a long heading ran off the page). A word longer
    than a whole line is broken where it must be."""
    if _text_width(text, font, size) <= limit:
        return (text,)
    indent = "    "
    lines: list[str] = []
    current = ""
    for word in text.split():
        prefix = f"{current} " if current else (indent if lines else "")
        if _text_width(prefix + word, font, size) <= limit:
            current = prefix + word
            continue
        if current:
            lines.append(current)
            prefix = indent
        piece = prefix
        for character in word:
            if piece.strip() and _text_width(piece + character, font, size) > limit:
                lines.append(piece)
                piece = indent
            piece += character
        current = piece
    if current:
        lines.append(current)
    return tuple(lines)


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

    finding: RenderableFinding
    polygon: Polygon


CALLOUT_COLUMN_WIDTH: Final = Decimal(252)
CALLOUT_MARGIN: Final = Decimal(16)
CALLOUT_LINE_HEIGHT: Final = Decimal(10)
CALLOUT_FONT_SIZE: Final = 7.5
#: How wide a margin note's line may run: its column, less the gap before it and one after it.
CALLOUT_TEXT_WIDTH: Final = float(CALLOUT_COLUMN_WIDTH - 2 * CALLOUT_MARGIN)


def _fact_note(finding: RenderableFinding) -> str:
    """A fixed presentation template, never a new reading or an inferred role."""
    outcome = finding.outcome
    parts = [
        (
            finding.rule_id  # its reason already starts `NOT CHECKED: `
            if _not_checked(finding)
            else f"{finding.rule_id} · {outcome.value.replace('_', ' ')}"
        )
    ]
    scope = finding.scope_label if isinstance(finding, RedlineFinding) else None
    if scope:
        parts.append(scope)
    if finding.reason:
        parts.append(finding.reason)
    comparison = (
        finding.comparison
        if isinstance(finding, RedlineFinding)
        else finding.trace.comparison
        if finding.trace is not None
        else None
    )
    if outcome in (Outcome.FAIL, Outcome.REVIEW_REQUIRED) and comparison:
        parts.append(f"Comparison: {comparison}")
    if outcome is not Outcome.NO_APPLICABLE_RULE:
        parts.extend(note for note in finding.notes if note.startswith("Wall layout:"))
    return ". ".join(part.rstrip(". ") for part in parts if part) + "."


def _note_lines(finding: RenderableFinding) -> tuple[str, ...]:
    return tuple(textwrap.wrap(_fact_note(finding), width=47, break_long_words=True))


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
    findings: Sequence[RenderableFinding],
    mode: ReportMode,
    store: ArtifactStore,
    *,
    clearance: VendorClearance | None = None,
    changed_values: ChangedValues | None = None,
    signed_review: SignedReview | None = None,
    countertop_labels: Sequence[str] = (),
    reviewer_marks: Sequence[ReviewerMark] | None = None,
    architect: ArchitectCompare | None = None,
    label_layout: LabelLayout = LabelLayout.AS_BEFORE,
    review_cover: ReviewCover | None = None,
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

    `reviewer_marks` are a person's confirms, fixes and answers (#1363): each is drawn where code
    recorded a place for it, and every one is listed on the summary pages, placed or not. `None`
    (a sign-off snapshot frozen before #1363) draws exactly what was drawn before; an empty
    sequence says on the summary that no person answered anything.

    `architect` (the direct compare) clouds each "different" architect dimension at the vendor's
    dimension, "✗ Architect: Y — vendor: X", unless a person said it is not a difference, and lists
    every one, with the counts, on the summary. `None` (no compare, or a snapshot frozen before
    it) draws exactly what was drawn before.
    """
    if not isinstance(package, RedlinePackage):
        raise TypeError("package must be a RedlinePackage")
    if isinstance(findings, str) or not isinstance(findings, Sequence):
        raise TypeError("findings must be a sequence of Finding values")
    for finding in findings:
        if not isinstance(finding, (Finding, RedlineFinding)):
            raise TypeError("findings must contain only Finding or RedlineFinding values")
    if not isinstance(mode, ReportMode):
        raise TypeError("mode must be a ReportMode")
    if not isinstance(store, ArtifactStore):
        raise TypeError("store must implement the ArtifactStore protocol")
    if isinstance(countertop_labels, str) or any(
        not isinstance(label, str) or not label.strip() for label in countertop_labels
    ):
        raise TypeError("countertop_labels must be a sequence of non-empty strings")
    if reviewer_marks is not None and (
        isinstance(reviewer_marks, str)
        or not isinstance(reviewer_marks, Sequence)
        or any(not isinstance(mark, ReviewerMark) for mark in reviewer_marks)
    ):
        raise TypeError("reviewer_marks must be a sequence of ReviewerMark values")
    if architect is not None and not isinstance(architect, ArchitectCompare):
        raise TypeError("architect must be an ArchitectCompare")
    if not isinstance(label_layout, LabelLayout):
        raise TypeError("label_layout must be a LabelLayout")

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
    reviewer_placed, reviewer_unplaced = _place_reviewer(package, reviewer_marks or ())
    architect_placed, architect_unplaced = _place_architect(package, architect)
    document = _compose(
        package,
        reader,
        marks,
        findings,
        marked,
        unplaced,
        clearance,
        changed_values,
        countertop_labels,
        reviewer_placed,
        (
            None
            if reviewer_marks is None
            else _ReviewerListing(tuple(reviewer_marks), reviewer_unplaced)
        ),
        architect_placed,
        None if architect is None else _ArchitectListing(architect, architect_unplaced),
        label_layout,
    )
    if signed_review is not None:
        if clearance is None or signed_review.approval_id != clearance.approval_id:
            raise VendorApprovalUnavailable("signed redline review does not match its approval")
        document = with_review_pdf(document, signed_review, review_cover)

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
    package: RedlinePackage, findings: Sequence[RenderableFinding]
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


@dataclass(frozen=True, slots=True)
class _ReviewerListing:
    """Every reviewer mark, in order, and why each one not drawn is not drawn (by its number)."""

    marks: tuple[ReviewerMark, ...]
    unplaced: dict[int, str]


def _place_reviewer(
    package: RedlinePackage, reviewer_marks: Sequence[ReviewerMark]
) -> tuple[dict[int, list[_ReviewerPlaced]], dict[int, str]]:
    """Sort a person's marks into places on pages and reasons; every one leaves accounted for.

    Placement follows the findings' own rule: only a recorded page and polygon in the stored space,
    on a page of this redline. Numbered from 1 in the order given, so the drawing and the summary
    name each mark the same way.
    """
    by_key = {entry.key: entry for entry in package.pages}
    placed: dict[int, list[_ReviewerPlaced]] = {}
    unplaced: dict[int, str] = {}
    for number, mark in enumerate(reviewer_marks, start=1):
        if mark.evidence_ref is None:
            unplaced[number] = (
                mark.unplaced_reason or "no place on the drawing was recorded for this answer"
            )
            continue
        located = _decode_reference(mark.evidence_ref)
        if isinstance(located, _Undecodable):
            unplaced[number] = located.reason.replace("its evidence reference", "its place", 1)
            continue
        entry = by_key.get((located.document_version_id, located.page))
        if entry is None:
            unplaced[number] = (
                f"it is on page {located.page + 1} of a document version that is not one of the "
                "pages in this redline"
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
            unplaced[number] = f"its recorded place is not a usable page region ({error})"
            continue
        placed.setdefault(entry.source_index, []).append(_ReviewerPlaced(mark, number, polygon))
    return placed, unplaced


@dataclass(frozen=True, slots=True)
class _ArchitectListing:
    """The direct compare, and why each "different" result not clouded is not (by its number)."""

    compare: ArchitectCompare
    unplaced: dict[int, str]


def _place_architect(
    package: RedlinePackage, architect: ArchitectCompare | None
) -> tuple[dict[int, list[_ArchitectPlaced]], dict[int, str]]:
    """Sort the "different" results into clouds on pages and reasons; every one leaves accounted
    for. The same placement rule as a finding's: a recorded page and stored polygon on a page of
    this redline. Numbered from 1 in the order given (A1, A2, ...)."""
    placed: dict[int, list[_ArchitectPlaced]] = {}
    unplaced: dict[int, str] = {}
    if architect is None:
        return placed, unplaced
    by_key = {entry.key: entry for entry in package.pages}
    for number, item in enumerate(architect.different, start=1):
        if item.answer is ArchitectAnswer.NOT_A_DIFFERENCE:
            unplaced[number] = (
                f"{item.answered_by} said it is not a difference, so it is not clouded"
            )
            continue
        if item.evidence_ref is None:
            unplaced[number] = item.unplaced_reason or "no place on the drawing was recorded"
            continue
        located = _decode_reference(item.evidence_ref)
        if isinstance(located, _Undecodable):
            unplaced[number] = located.reason.replace("its evidence reference", "its place", 1)
            continue
        entry = by_key.get((located.document_version_id, located.page))
        if entry is None:
            unplaced[number] = (
                f"it is on page {located.page + 1} of a document version that is not one of the "
                "pages in this redline"
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
            unplaced[number] = f"its recorded place is not a usable page region ({error})"
            continue
        placed.setdefault(entry.source_index, []).append(_ArchitectPlaced(item, number, polygon))
    return placed, unplaced


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _compose(
    package: RedlinePackage,
    reader: PdfReader,
    marks: dict[int, list[_Mark]],
    findings: Sequence[RenderableFinding],
    marked: int,
    unplaced: Sequence[Unplaced],
    clearance: VendorClearance | None,
    changed_values: ChangedValues | None,
    countertop_labels: Sequence[str],
    reviewer_placed: dict[int, list[_ReviewerPlaced]] | None = None,
    reviewer_listing: _ReviewerListing | None = None,
    architect_placed: dict[int, list[_ArchitectPlaced]] | None = None,
    architect_listing: _ArchitectListing | None = None,
    label_layout: LabelLayout = LabelLayout.AS_BEFORE,
) -> bytes:
    """Merge the overlays onto the original pages and append the summary pages.

    Every source page is carried over, marked or not. Dropping unmarked pages would give a vendor
    a document that looks like the whole drawing set and is not.
    """
    # Cloned into the writer before anything is merged: pypdf only supports merging into a page it
    # owns, and modifying a reader's page in place is deprecated and documented as unreliable.
    writer = PdfWriter(clone_from=reader)
    by_index = {entry.source_index: entry for entry in package.pages}

    reviewer_placed = reviewer_placed or {}
    architect_placed = architect_placed or {}
    for index, page in enumerate(writer.pages):
        page_marks = marks.get(index) or ()
        page_reviewer = reviewer_placed.get(index) or ()
        page_architect = architect_placed.get(index) or ()
        if page_marks or page_reviewer or page_architect:
            entry = by_index[index]
            ordered = _ordered_marks(entry, page_marks)
            reviewer_ordered = tuple(sorted(page_reviewer, key=lambda item: item.number))
            architect_ordered = tuple(sorted(page_architect, key=lambda item: item.number))
            # A heading too long for its column wraps (QA 2026-10-16): its later lines are laid out
            # with the note, so the column still holds all of it.
            placements, columns = _callout_layout(
                entry,
                [
                    (
                        *_heading_lines(_finding_heading(index, mark.finding), "Courier-Bold")[1:],
                        *_note_lines(mark.finding),
                    )
                    for index, mark in enumerate(ordered, start=1)
                ]
                + [
                    (
                        *_heading_lines(f"R{item.number}  {item.mark.text}", "Helvetica-Bold")[1:],
                        *_reviewer_lines(item.mark),
                    )
                    for item in reviewer_ordered
                ]
                + [
                    (
                        *_heading_lines(f"A{item.number}  {item.item.words}", "Courier-Bold")[1:],
                        *_architect_lines(item.item),
                    )
                    for item in architect_ordered
                ],
            )
            expanded = _expanded_boxes(entry, columns)
            after_reviewer = len(ordered) + len(reviewer_ordered)
            overlay = _overlay(
                entry,
                ordered,
                placements[: len(ordered)],
                expanded,
                reviewer_ordered,
                placements[len(ordered) : after_reviewer],
                architect_ordered,
                placements[after_reviewer:],
                (
                    _Ink(
                        page_ink(
                            package.source_pdf,
                            index,
                            _sheet(entry, text_angle(entry.transform.rotation)),
                        )
                    )
                    if label_layout is LabelLayout.TAGS
                    else None
                ),
            )
            _append_overlay(writer, page, overlay, expanded)

    pages_with_marks = frozenset(index for index, page_marks in marks.items() if page_marks)
    summary = _listing(
        package,
        findings,
        marked,
        unplaced,
        pages_with_marks,
        clearance,
        changed_values,
        countertop_labels,
        reviewer_listing,
        {
            item.number: entry.page
            for entry in package.pages
            for item in reviewer_placed.get(entry.source_index, ())
        },
        architect_listing,
        {
            item.number: entry.page
            for entry in package.pages
            for item in architect_placed.get(entry.source_index, ())
        },
    )
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


def _ordered_marks(entry: RedlinePage, marks: Sequence[_Mark]) -> tuple[_Mark, ...]:
    """Stable top-to-bottom order in the visible, rotated sheet."""
    angle = text_angle(entry.transform.rotation)

    def key(mark: _Mark) -> tuple[Decimal, Decimal, str]:
        anchor = label_anchor(polygon_pdf_points(mark.polygon, entry.transform), angle)
        x, y = _turn(anchor.x, anchor.y, (360 - angle) % 360)
        return (-y, x, mark.finding.rule_id)

    return tuple(sorted(marks, key=key))


def _visible_size(entry: RedlinePage) -> tuple[Decimal, Decimal]:
    left, bottom, right, top = entry.transform.crop_box
    width, height = right - left, top - bottom
    return (height, width) if entry.transform.rotation in (90, 270) else (width, height)


def _finding_heading(index: int, finding: RenderableFinding) -> str:
    """A finding's margin-note heading: its number, rule and result."""
    return f"{index:02d}  {finding.rule_id} \u00b7 {_result_words(finding)}"


def _heading_lines(heading: str, font: str) -> tuple[str, ...]:
    """A margin-note heading on the lines it takes inside its column (one, unless it is long)."""
    return _fitted(heading, font, CALLOUT_FONT_SIZE, CALLOUT_TEXT_WIDTH)


def _reviewer_lines(mark: ReviewerMark) -> tuple[str, ...]:
    """The callout under a reviewer mark's heading: what it is about, then its details."""
    return tuple(
        wrapped
        for text in (mark.subject, *mark.details)
        for wrapped in textwrap.wrap(text, width=47, break_long_words=True)
    )


def _architect_lines(item: ArchitectDifference) -> tuple[str, ...]:
    """The callout under an architect difference's heading: where it is."""
    return tuple(textwrap.wrap(item.subject, width=47, break_long_words=True))


def _callout_layout(
    entry: RedlinePage, notes: Sequence[tuple[str, ...]]
) -> tuple[tuple[tuple[int, Decimal, tuple[str, ...]], ...], int]:
    """Allocate full notes in external columns; never truncate or use the drawing crop."""
    _, height = _visible_size(entry)
    column = 0
    cursor = height - CALLOUT_MARGIN
    result: list[tuple[int, Decimal, tuple[str, ...]]] = []
    for lines in notes:
        needed = CALLOUT_LINE_HEIGHT * (len(lines) + 2)
        if result and cursor - needed < CALLOUT_MARGIN:
            column += 1
            cursor = height - CALLOUT_MARGIN
        result.append((column, cursor, lines))
        cursor -= needed
    return tuple(result), column + 1


def _expanded_boxes(
    entry: RedlinePage, columns: int
) -> tuple[tuple[Decimal, Decimal, Decimal, Decimal], tuple[Decimal, Decimal, Decimal, Decimal]]:
    """Extend the visible right edge without scaling or moving the original crop."""
    amount = CALLOUT_COLUMN_WIDTH * columns
    side = entry.transform.rotation

    def extend(
        box: tuple[Decimal, Decimal, Decimal, Decimal],
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        left, bottom, right, top = box
        if side == 0:
            return (left, bottom, right + amount, top)
        if side == 90:
            return (left, bottom, right, top + amount)
        if side == 180:
            return (left - amount, bottom, right, top)
        return (left, bottom - amount, right, top)

    return extend(entry.transform.media_box), extend(entry.transform.crop_box)


def _visual_point(entry: RedlinePage, u: Decimal, v: Decimal) -> tuple[Decimal, Decimal]:
    """External-strip position in the same visible frame the reader sees."""
    left, bottom, right, top = entry.transform.crop_box
    angle = entry.transform.rotation
    if angle == 0:
        return left + u, bottom + v
    if angle == 90:
        return right - v, bottom + u
    if angle == 180:
        return right - u, top - v
    return left + v, top - u


def _overlay(
    entry: RedlinePage,
    marks: Sequence[_Mark],
    placements: Sequence[tuple[int, Decimal, tuple[str, ...]]],
    expanded: tuple[
        tuple[Decimal, Decimal, Decimal, Decimal],
        tuple[Decimal, Decimal, Decimal, Decimal],
    ],
    reviewer: Sequence[_ReviewerPlaced] = (),
    reviewer_placements: Sequence[tuple[int, Decimal, tuple[str, ...]]] = (),
    architect: Sequence[_ArchitectPlaced] = (),
    architect_placements: Sequence[tuple[int, Decimal, tuple[str, ...]]] = (),
    ink: _Ink | None = None,
) -> PageObject:
    """Build the transparent vector mark and callout page.

    `ink` (the page's own drawing, `page_ink`) switches the drawing to tags only
    (`LabelLayout.TAGS`); `None` draws the labels exactly as before."""
    left, bottom, right, top = expanded[0]
    canvas, buffer = _canvas(right - left, top - bottom)
    canvas.translate(float(-left), float(-bottom))
    angle = text_angle(entry.transform.rotation)
    visible_width, _ = _visible_size(entry)

    sheet = _sheet(entry, angle)
    taken: list[tuple[float, float, float, float]] = []
    tags: _Tags | None = None
    if ink is not None:
        tags = _Tags(
            sheet,
            angle,
            ink.page,
            tuple(
                _turned_box(polygon_pdf_points(polygon, entry.transform), angle, pad)
                for polygon, pad in (
                    *((mark.polygon, _CLOUD_REACH) for mark in marks),
                    *((placed.polygon, _CLOUD_REACH) for placed in architect),
                    *((item.polygon, _FRAME_REACH) for item in reviewer),
                )
            ),
        )
    for index, (mark, (column, cursor, lines)) in enumerate(
        zip(marks, placements, strict=True), start=1
    ):
        if tags is None:
            _draw_mark(canvas, mark, entry.transform, angle, index, sheet, taken)
        else:
            points = polygon_pdf_points(mark.polygon, entry.transform)
            _draw_cloud(canvas, points, _style(mark.finding))
            tags.draw(canvas, points, _CLOUD_REACH, f"{index:02d}", _style(mark.finding).stroke)
        u = visible_width + column * CALLOUT_COLUMN_WIDTH + CALLOUT_MARGIN
        x, y = _visual_point(entry, u, cursor)
        style = _style(mark.finding)
        heading = _heading_lines(_finding_heading(index, mark.finding), "Courier-Bold")
        extra = len(heading) - 1
        canvas.saveState()
        canvas.translate(float(x), float(y))
        canvas.rotate(angle)
        canvas.setFillColorRGB(*style.stroke)
        if _not_checked(mark.finding) or extra:
            _draw_text(canvas, 0, 0, heading[0], "Courier-Bold", CALLOUT_FONT_SIZE)
        else:
            canvas.setFont("Courier-Bold", CALLOUT_FONT_SIZE)
            canvas.drawString(0, 0, heading[0])
        _heading_rest(canvas, heading, "Courier-Bold")
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        canvas.setFont("Courier", CALLOUT_FONT_SIZE)
        for line_number, line in enumerate(lines[extra:], start=extra + 1):
            canvas.drawString(0, -line_number * float(CALLOUT_LINE_HEIGHT), line)
        canvas.restoreState()

    for placed, (column, cursor, lines) in zip(architect, architect_placements, strict=True):
        if tags is None:
            _draw_architect(canvas, placed, entry.transform, angle, sheet, taken)
        else:
            points = polygon_pdf_points(placed.polygon, entry.transform)
            _draw_cloud(canvas, points, ARCHITECT_STYLE)
            tags.draw(canvas, points, _CLOUD_REACH, f"A{placed.number}", ARCHITECT_STYLE.stroke)
        u = visible_width + column * CALLOUT_COLUMN_WIDTH + CALLOUT_MARGIN
        x, y = _visual_point(entry, u, cursor)
        heading = _heading_lines(f"A{placed.number}  {placed.item.words}", "Courier-Bold")
        extra = len(heading) - 1
        canvas.saveState()
        canvas.translate(float(x), float(y))
        canvas.rotate(angle)
        canvas.setFillColorRGB(*ARCHITECT_STYLE.stroke)
        _draw_text(canvas, 0, 0, heading[0], "Courier-Bold", CALLOUT_FONT_SIZE)
        _heading_rest(canvas, heading, "Courier-Bold")
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        canvas.setFont("Courier", CALLOUT_FONT_SIZE)
        for line_number, line in enumerate(lines[extra:], start=extra + 1):
            canvas.drawString(0, -line_number * float(CALLOUT_LINE_HEIGHT), line)
        canvas.restoreState()

    reviewer_taken: list[tuple[Decimal, Decimal, Decimal, Decimal]] = []
    for item, (column, cursor, lines) in zip(reviewer, reviewer_placements, strict=True):
        if tags is None:
            _draw_reviewer_mark(canvas, item, entry, angle, reviewer_taken)
        else:
            points = polygon_pdf_points(item.polygon, entry.transform)
            _draw_reviewer_frame(canvas, points, _REVIEWER_STROKES[item.mark.kind])
            tags.draw(
                canvas, points, _FRAME_REACH, f"R{item.number}", _REVIEWER_STROKES[item.mark.kind]
            )
        u = visible_width + column * CALLOUT_COLUMN_WIDTH + CALLOUT_MARGIN
        x, y = _visual_point(entry, u, cursor)
        heading = _heading_lines(f"R{item.number}  {item.mark.text}", "Helvetica-Bold")
        extra = len(heading) - 1
        canvas.saveState()
        canvas.translate(float(x), float(y))
        canvas.rotate(angle)
        canvas.setFillColorRGB(*_REVIEWER_STROKES[item.mark.kind])
        _draw_text(canvas, 0, 0, heading[0], "Helvetica-Bold", CALLOUT_FONT_SIZE)
        _heading_rest(canvas, heading, "Helvetica-Bold")
        canvas.setFillColorRGB(0.1, 0.1, 0.1)
        for line_number, line in enumerate(lines[extra:], start=extra + 1):
            _draw_text(
                canvas,
                0,
                -line_number * float(CALLOUT_LINE_HEIGHT),
                line,
                "Courier",
                CALLOUT_FONT_SIZE,
            )
        canvas.restoreState()

    canvas.showPage()
    canvas.save()
    buffer.seek(0)
    return PdfReader(buffer).pages[0]


def _heading_rest(canvas: Canvas, heading: Sequence[str], font: str) -> None:
    """The later lines of a wrapped margin-note heading, under its first, in the colour set."""
    for line_number, line in enumerate(heading[1:], start=1):
        _draw_text(
            canvas, 0, -line_number * float(CALLOUT_LINE_HEIGHT), line, font, CALLOUT_FONT_SIZE
        )


def _append_overlay(
    writer: PdfWriter,
    page: PageObject,
    overlay: PageObject,
    expanded: tuple[
        tuple[Decimal, Decimal, Decimal, Decimal],
        tuple[Decimal, Decimal, Decimal, Decimal],
    ],
) -> None:
    """Add a Form XObject stream while keeping every source stream byte-identical."""
    media, crop = expanded
    content = overlay.get_contents()
    if content is None:
        raise ValueError("the redline overlay has no vector content")
    form = DecodedStreamObject()
    form.set_data(content.get_data())
    form.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/FormType"): NumberObject(1),
            NameObject("/BBox"): RectangleObject((0, 0, media[2] - media[0], media[3] - media[1])),
            NameObject("/Matrix"): ArrayObject(
                [
                    NumberObject(1),
                    NumberObject(0),
                    NumberObject(0),
                    NumberObject(1),
                    FloatObject(media[0]),
                    FloatObject(media[1]),
                ]
            ),
            NameObject("/Resources"): overlay["/Resources"].clone(writer),
        }
    )
    form_ref = writer._add_object(form)
    source_resources = page.get("/Resources")
    resources = DictionaryObject(
        source_resources.get_object() if source_resources is not None else {}
    )
    xobjects = DictionaryObject(resources.get("/XObject", DictionaryObject()).get_object())
    name = NameObject("/GVRedline939")
    if name in xobjects:
        raise ValueError("source drawing already uses the reserved redline resource name")
    xobjects[name] = form_ref
    resources[NameObject("/XObject")] = xobjects
    page[NameObject("/Resources")] = resources
    command = DecodedStreamObject()
    command.set_data(b"q /GVRedline939 Do Q\n")
    command_ref = writer._add_object(command)
    original = page.get("/Contents")
    page[NameObject("/Contents")] = ArrayObject(
        ([*original] if isinstance(original, ArrayObject) else [original] if original else [])
        + [command_ref]
    )
    page.mediabox = RectangleObject(tuple(FloatObject(value) for value in media))
    page.cropbox = RectangleObject(tuple(FloatObject(value) for value in crop))


def _draw_mark(
    canvas: Canvas,
    mark: _Mark,
    transform: PageTransform,
    angle: int,
    index: int,
    sheet: _Box,
    taken: list[_Box],
) -> None:
    """Outline the evidence region and label it with the rule and the outcome."""
    style = _style(mark.finding)
    points = polygon_pdf_points(mark.polygon, transform)

    canvas.saveState()
    canvas.setStrokeColorRGB(*style.stroke)
    canvas.setLineWidth(style.line_width)
    if style.dashed:
        canvas.setDash(4, 3)

    canvas.restoreState()

    # The stored polygon may tightly bound the vendor's glyphs. Stroking that polygon
    # crosses the number itself; the padded cloud alone is the visible region mark.
    _draw_cloud(canvas, points, style)
    _draw_label(canvas, mark, points, style, angle, index, sheet, taken)


def _draw_cloud(canvas: Canvas, points: Sequence[PdfPoint], style: _OutcomeStyle) -> None:
    """Fine vector scallops around the real evidence polygon, never a raster or opaque mask."""
    left = min(float(point.x) for point in points) - 8
    right = max(float(point.x) for point in points) + 8
    bottom = min(float(point.y) for point in points) - 8
    top = max(float(point.y) for point in points) + 8
    radius = 5.0
    canvas.saveState()
    canvas.setStrokeColorRGB(*style.stroke)
    canvas.setLineWidth(style.line_width)
    if style.dashed:
        canvas.setDash(4, 3)
    for x in range(int(left), int(right) + 1, 10):
        canvas.arc(x - radius, top - radius, x + radius, top + radius, 0, 180)
        canvas.arc(x - radius, bottom - radius, x + radius, bottom + radius, 180, 180)
    for y in range(int(bottom), int(top) + 1, 10):
        canvas.arc(left - radius, y - radius, left + radius, y + radius, 90, 180)
        canvas.arc(right - radius, y - radius, right + radius, y + radius, 270, 180)
    canvas.restoreState()


#: A box in the page's visible (turned) frame: left, bottom, right, top, in points.
_Box = tuple[float, float, float, float]

#: How far inside the sheet's edge a label on the drawing is kept.
LABEL_INSET: Final = 2.0

#: The smallest size the full words of a label on the drawing are shrunk to before the label says
#: only its number.
LABEL_MIN_FONT_SIZE: Final = 5.5


def _sheet(entry: RedlinePage, angle: int) -> _Box:
    """The visible sheet (its crop box) in the turned frame labels are placed in."""
    inverse = (360 - angle) % 360
    left, bottom, right, top = entry.transform.crop_box
    corners = [_turn(x, y, inverse) for x in (left, right) for y in (bottom, top)]
    return (
        float(min(x for x, _ in corners)),
        float(min(y for _, y in corners)),
        float(max(x for x, _ in corners)),
        float(max(y for _, y in corners)),
    )


def _label_box(x: float, base: float, width: float, size: float) -> _Box:
    return (x, base - size * 0.25, x + width, base + size)


def _clashes(box: _Box, taken: Sequence[_Box]) -> bool:
    return any(
        box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]
        for other in taken
    )


@dataclass(frozen=True, slots=True)
class _LabelSpot:
    """Where a label on the drawing goes, in the turned frame, and in which words and size."""

    text: str
    size: float
    x: float
    base: float
    as_before: bool
    """Drawn exactly as before (QA 2026-10-16): the full words, in place, touching nothing."""


def _label_spot(
    points: Sequence[PdfPoint],
    angle: int,
    sheet: _Box,
    taken: list[_Box],
    full: str,
    short: str | None,
    font: str,
) -> _LabelSpot:
    """Place a label above its cloud, inside the sheet and off every label already placed.

    The full words, where they always went, when they fit inside the sheet and touch no other
    label: drawn exactly as before. Else moved inside the sheet (below the cloud when there is no
    room above it), shrunk down to `LABEL_MIN_FONT_SIZE` if that is what it takes; and when even
    that does not fit or would print over another label, only `short` (the cloud's number; the
    full words stay in the margin note and on the summary), stacked clear of the others. With no
    `short` the full words are kept, moved inside the sheet.
    """
    inverse = (360 - angle) % 360
    turned = [_turn(point.x, point.y, inverse) for point in points]
    anchor_x = float(min(x for x, _ in turned))
    cloud_top = float(max(y for _, y in turned))
    cloud_bottom = float(min(y for _, y in turned))
    left, bottom, right, top = (
        sheet[0] + LABEL_INSET,
        sheet[1] + LABEL_INSET,
        sheet[2] - LABEL_INSET,
        sheet[3] - LABEL_INSET,
    )

    def spot(text: str, size: float) -> tuple[float, float, float]:
        width = _text_width(text, font, size)
        base = cloud_top + 12 + size * 0.35 + 1.0
        if base + size > top:
            # No room above the cloud: just below it (its scallops reach 13 points out).
            base = cloud_bottom - 13 - size
        base = max(bottom + size * 0.25, min(base, top - size))
        x = max(left, min(anchor_x, right - width))
        return x, base, width

    default_base = cloud_top + 12 + MARK_FONT_SIZE * 0.35 + 1.0
    sizes = [MARK_FONT_SIZE]
    while sizes[-1] - 0.5 >= LABEL_MIN_FONT_SIZE:
        sizes.append(sizes[-1] - 0.5)
    for size in sizes:
        x, base, width = spot(full, size)
        box = _label_box(x, base, width, size)
        fits = box[0] >= left - 1e-6 and box[2] <= right + 1e-6
        if fits and not _clashes(box, taken):
            taken.append(box)
            as_before = size == MARK_FONT_SIZE and x == anchor_x and base == default_base
            return _LabelSpot(full, size, x, base, as_before)
    text = full if short is None else short
    size = MARK_FONT_SIZE if short is not None else LABEL_MIN_FONT_SIZE
    x, base, width = spot(text, size)
    step = size + 2
    for direction in (1, -1):
        candidate = base
        while bottom + size * 0.25 <= candidate <= top - size:
            if not _clashes(_label_box(x, candidate, width, size), taken):
                taken.append(_label_box(x, candidate, width, size))
                return _LabelSpot(text, size, x, candidate, False)
            candidate += step * direction
    # Nowhere clear inside the sheet: at its own place, still inside the sheet.
    taken.append(_label_box(x, base, width, size))
    return _LabelSpot(text, size, x, base, False)


def _draw_moved_label(
    canvas: Canvas, spot: _LabelSpot, angle: int, stroke: tuple[float, float, float], font: str
) -> None:
    """A label that was moved, shrunk or shortened, on a pale backing so it reads over the lines."""
    x, y = _turn(Decimal(str(spot.x)), Decimal(str(spot.base)), angle)
    width = _text_width(spot.text, font, spot.size)
    canvas.saveState()
    canvas.translate(float(x), float(y))
    canvas.rotate(angle)
    canvas.setFillColorRGB(1, 1, 1)
    canvas.setFillAlpha(0.8)
    canvas.rect(-1, -spot.size * 0.25 - 1, width + 2, spot.size * 1.25 + 2, stroke=0, fill=1)
    canvas.setFillAlpha(1)
    canvas.setFillColorRGB(*stroke)
    _draw_text(canvas, 0.0, 0.0, spot.text, font, spot.size)
    canvas.restoreState()


def _draw_label(
    canvas: Canvas,
    mark: _Mark,
    points: Sequence[PdfPoint],
    style: _OutcomeStyle,
    angle: int,
    index: int,
    sheet: _Box,
    taken: list[_Box],
) -> None:
    """Write the rule id and outcome beside the mark, turned to read upright on a rotated page.

    Only the rule and the outcome go on the drawing. The reason is on the summary pages instead:
    covering the line work with a paragraph would hide the very thing the reviewer is being asked
    to look at.
    """
    label = f"{index:02d} · {mark.finding.rule_id} · {_result_words(mark.finding)}"
    spot = _label_spot(points, angle, sheet, taken, label, None, "Helvetica-Bold")
    if not spot.as_before:
        _draw_moved_label(canvas, spot, angle, style.stroke, "Helvetica-Bold")
        return
    anchor = label_anchor(points, angle)

    canvas.saveState()
    canvas.translate(float(anchor.x), float(anchor.y))
    canvas.rotate(angle)
    canvas.translate(0, 12)
    if _not_checked(mark.finding):
        canvas.setFillColorRGB(*style.stroke)
        _draw_text(
            canvas, 0.0, MARK_FONT_SIZE * 0.35 + 1.0, label, "Helvetica-Bold", MARK_FONT_SIZE
        )
    else:
        canvas.setFont("Helvetica-Bold", MARK_FONT_SIZE)
        canvas.setFillColorRGB(*style.stroke)
        canvas.drawString(0.0, MARK_FONT_SIZE * 0.35 + 1.0, label)
    canvas.restoreState()


#: An architect difference is clouded as a wrong result is: the glyph and words say what it is.
ARCHITECT_STYLE: Final = OUTCOME_STYLES[Outcome.FAIL]


def _draw_architect(
    canvas: Canvas,
    placed: _ArchitectPlaced,
    transform: PageTransform,
    angle: int,
    sheet: _Box,
    taken: list[_Box],
) -> None:
    """A cloud at the vendor's dimension, "A1 · ✗ Architect: Y — vendor: X" above it; only "A1"
    when the words would leave the sheet or print over another label (`_label_spot`)."""
    points = polygon_pdf_points(placed.polygon, transform)
    _draw_cloud(canvas, points, ARCHITECT_STYLE)
    spot = _label_spot(
        points,
        angle,
        sheet,
        taken,
        f"A{placed.number} · {placed.item.words}",
        f"A{placed.number}",
        "Helvetica-Bold",
    )
    if not spot.as_before:
        _draw_moved_label(canvas, spot, angle, ARCHITECT_STYLE.stroke, "Helvetica-Bold")
        return
    anchor = label_anchor(points, angle)
    canvas.saveState()
    canvas.translate(float(anchor.x), float(anchor.y))
    canvas.rotate(angle)
    canvas.translate(0, 12)
    canvas.setFillColorRGB(*ARCHITECT_STYLE.stroke)
    _draw_text(
        canvas,
        0.0,
        MARK_FONT_SIZE * 0.35 + 1.0,
        f"A{placed.number} · {placed.item.words}",
        "Helvetica-Bold",
        MARK_FONT_SIZE,
    )
    canvas.restoreState()


def _draw_reviewer_frame(
    canvas: Canvas, points: Sequence[PdfPoint], stroke: tuple[float, float, float]
) -> None:
    """A person's mark: a dotted frame around the region (never a cloud, which is a check's)."""
    left = min(float(point.x) for point in points) - 3
    right = max(float(point.x) for point in points) + 3
    bottom = min(float(point.y) for point in points) - 3
    top = max(float(point.y) for point in points) + 3
    canvas.saveState()
    canvas.setStrokeColorRGB(*stroke)
    canvas.setLineWidth(0.9)
    canvas.setDash(1, 2)
    canvas.rect(left, bottom, right - left, top - bottom, stroke=1, fill=0)
    canvas.restoreState()


# ---------------------------------------------------------------------------
# Tags only (`LabelLayout.TAGS`, QA 2026-10-16)
# ---------------------------------------------------------------------------

#: How far a cloud's scallops reach past its evidence polygon (8 of padding and a 5 radius).
_CLOUD_REACH: Final = 13.0

#: How far a person's dotted frame reaches past its region.
_FRAME_REACH: Final = 3.0

#: The tag's size on the drawing, and the gap kept between a tag and its cloud.
TAG_FONT_SIZE: Final = 7.0
_TAG_GAP: Final = 1.5


def _turned_box(points: Sequence[PdfPoint], angle: int, pad: float) -> _Box:
    """The box around `points`, `pad` points out, in the page's visible (turned) frame."""
    inverse = (360 - angle) % 360
    turned = [_turn(point.x, point.y, inverse) for point in points]
    return (
        float(min(x for x, _ in turned)) - pad,
        float(min(y for _, y in turned)) - pad,
        float(max(x for x, _ in turned)) + pad,
        float(max(y for _, y in turned)) + pad,
    )


#: The scale the page is drawn at to find its ink (pixels per point), and how dark a pixel must
#: be to count as ink.
_INK_SCALE: Final = 2.0
_INK_DARK: Final = 170


@dataclass(frozen=True, slots=True)
class PageInk:
    """The page's own drawing, as dark pixels, to keep a tag off its words and lines.

    Drawn from the source page (pypdfium2), so it sees every mark the page makes: real text, text
    drawn as pen strokes, dimension lines and hatching. Only used to choose where a tag goes; it
    decides nothing."""

    image: Any
    sheet: _Box

    def cost(self, box: _Box) -> int:
        """How many dark pixels of the page are under `box` (in the turned frame)."""
        height = self.sheet[3] - self.sheet[1]
        left = int((box[0] - self.sheet[0]) * _INK_SCALE)
        right = int((box[2] - self.sheet[0]) * _INK_SCALE) + 1
        top = int((height - (box[3] - self.sheet[1])) * _INK_SCALE)
        bottom = int((height - (box[1] - self.sheet[1])) * _INK_SCALE) + 1
        width, tall = self.image.size
        left, right = max(0, left), min(width, right)
        top, bottom = max(0, top), min(tall, bottom)
        if right <= left or bottom <= top:
            return 0
        region = self.image.crop((left, top, right, bottom))
        return int(sum(region.histogram()[:_INK_DARK]))


def page_ink(source_pdf: bytes, index: int, sheet: _Box) -> PageInk | None:
    """Page `index` drawn in grey, its visible sheet (`_sheet`) top-left at the image's. `None` when
    the page cannot be drawn: tags are then kept off the clouds and each other only."""
    import pypdfium2 as pdfium  # type: ignore[import-untyped]

    try:
        document = pdfium.PdfDocument(source_pdf)
        try:
            image = document[index].render(scale=_INK_SCALE, grayscale=True).to_pil().convert("L")
        finally:
            document.close()
    except Exception:  # noqa: BLE001 - placement help only; a page that cannot be drawn has no ink
        return None
    return PageInk(image, sheet)


def _overlap(box: _Box, other: _Box) -> float:
    width = min(box[2], other[2]) - max(box[0], other[0])
    height = min(box[3], other[3]) - max(box[1], other[1])
    return width * height if width > 0 and height > 0 else 0.0


@dataclass(frozen=True, slots=True)
class _Ink:
    """Tags only on this page, with the page's ink when it could be drawn."""

    page: PageInk | None


@dataclass(slots=True)
class _Tags:
    """The tags on one page: each put beside its cloud in the first free spot (QA 2026-10-16).

    Free means inside the sheet, off the page's own words, off every cloud and frame, and off
    every tag already placed. Spots are tried nearest first, round the cloud (above, right, left,
    below), then a little further out each ring. When none is free, the spot that covers the
    least of the drawing's words and clouds is used, never one on another tag."""

    sheet: _Box
    angle: int
    ink: PageInk | None
    clouds: tuple[_Box, ...]
    placed: list[_Box] | None = None

    def spot(self, cloud: _Box, width: float, size: float) -> _Box:
        placed = self.placed if self.placed is not None else []
        left, bottom, right, top = (
            self.sheet[0] + LABEL_INSET,
            self.sheet[1] + LABEL_INSET,
            self.sheet[2] - LABEL_INSET,
            self.sheet[3] - LABEL_INSET,
        )
        height = size * 1.25
        best: tuple[float, _Box] | None = None
        for ring in range(6):
            gap = _TAG_GAP + ring * height
            x0, y0, x1, y1 = cloud
            middle = (y0 + y1) / 2 - height / 2
            candidates = (
                (x0, y1 + gap),  # above, from its left
                (x1 - width, y1 + gap),  # above, to its right
                (x1 + gap, y1 - height),  # right, high
                (x1 + gap, middle),  # right, middle
                (x0 - gap - width, y1 - height),  # left, high
                (x0 - gap - width, middle),  # left, middle
                (x0, y0 - gap - height),  # below, from its left
                (x1 - width, y0 - gap - height),  # below, to its right
                (x1 + gap, y0),  # right, low
                (x0 - gap - width, y0),  # left, low
            )
            for x, y in candidates:
                x = max(left, min(x, right - width))
                y = max(bottom, min(y, top - height))
                box = (x, y, x + width, y + height)
                if any(_overlap(box, other) > 0 for other in placed):
                    continue
                # Ink in pixels; a cloud's area counts as fully inked, twice over; a further ring
                # costs a little, so a near spot wins a tie.
                cost = (
                    (0 if self.ink is None else self.ink.cost(box))
                    + 2 * _INK_SCALE**2 * sum(_overlap(box, other) for other in self.clouds)
                    + (ring * 2 if ring else 0)
                )
                if cost == 0:
                    return box
                if best is None or cost < best[0]:
                    best = (cost, box)
        if best is not None:
            return best[1]
        # Every spot round the cloud is taken by another tag: stack above them, inside the sheet.
        x = max(left, min(cloud[0], right - width))
        y = cloud[3] + _TAG_GAP
        while any(_overlap((x, y, x + width, y + height), other) > 0 for other in placed):
            y += height
        y = min(y, top - height)
        return (x, y, x + width, y + height)

    def draw(
        self,
        canvas: Canvas,
        points: Sequence[PdfPoint],
        reach: float,
        tag: str,
        stroke: tuple[float, float, float],
    ) -> None:
        if self.placed is None:
            self.placed = []
        size = TAG_FONT_SIZE
        width = _text_width(tag, "Helvetica-Bold", size) + 2
        box = self.spot(_turned_box(points, self.angle, reach), width, size)
        self.placed.append(box)
        x, y = _turn(Decimal(str(box[0] + 1)), Decimal(str(box[1] + size * 0.3)), self.angle)
        canvas.saveState()
        canvas.translate(float(x), float(y))
        canvas.rotate(self.angle)
        # A pale backing so the tag reads over the line work; the drawing still shows through.
        canvas.setFillColorRGB(1, 1, 1)
        canvas.setFillAlpha(0.85)
        canvas.rect(-1, -size * 0.3, width, size * 1.25, stroke=0, fill=1)
        canvas.setFillAlpha(1)
        canvas.setFillColorRGB(*stroke)
        _draw_text(canvas, 0.0, 0.0, tag, "Helvetica-Bold", size)
        canvas.restoreState()


def _draw_reviewer_mark(
    canvas: Canvas,
    item: _ReviewerPlaced,
    entry: RedlinePage,
    angle: int,
    taken: list[tuple[Decimal, Decimal, Decimal, Decimal]],
) -> None:
    """A person's mark: a dotted frame (never a cloud, which is a check's) and its glyph and words.

    The words go just below the region as the page is read, so they never sit on a finding's
    label above it; when that would leave the visible sheet (a whole-page answer), just inside.
    Several answers about one place (a row's results checked by hand) stack one line apart rather
    than print over each other: `taken` holds the labels already on this page, in the page's
    visible frame.
    """
    points = polygon_pdf_points(item.polygon, entry.transform)
    stroke = _REVIEWER_STROKES[item.mark.kind]
    _draw_reviewer_frame(canvas, points, stroke)

    inverse = (360 - angle) % 360
    turned = [_turn(point.x, point.y, inverse) for point in points]
    low_x = min(x for x, _ in turned)
    low_y = min(y for _, y in turned)
    left_c, bottom_c, right_c, top_c = entry.transform.crop_box
    sheet_bottom = min(
        y
        for _, y in (
            _turn(corner_x, corner_y, inverse)
            for corner_x in (left_c, right_c)
            for corner_y in (bottom_c, top_c)
        )
    )
    size = Decimal(str(MARK_FONT_SIZE))
    label = f"R{item.number} {item.mark.text}"
    width = Decimal(
        str(
            sum(
                canvas.stringWidth(segment, font, MARK_FONT_SIZE)
                for font, segment in _runs(label, "Helvetica-Bold")
            )
        )
    )
    below = low_y - 4 - size
    inside = below < sheet_bottom + 2
    offset = low_y + 4 if inside else below
    step = (size + 2) * (1 if inside else -1)

    def clashes(y: Decimal) -> bool:
        return any(
            low_x < right_t and low_x + width > left_t and y < top_t and y + size > bottom_t
            for left_t, bottom_t, right_t, top_t in taken
        )

    while clashes(offset):
        offset += step
    taken.append((low_x, offset, low_x + width, offset + size))
    anchor = _turn(low_x, offset, angle)
    canvas.saveState()
    canvas.translate(float(anchor[0]), float(anchor[1]))
    canvas.rotate(angle)
    # A pale backing so the words read over the line work; the drawing still shows through.
    canvas.setFillColorRGB(1, 1, 1)
    canvas.setFillAlpha(0.8)
    canvas.rect(-1, -2, float(width) + 2, float(size) + 2, stroke=0, fill=1)
    canvas.setFillAlpha(1)
    canvas.setFillColorRGB(*stroke)
    _draw_text(canvas, 0.0, 0.0, label, "Helvetica-Bold", MARK_FONT_SIZE)
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


def derived_expectations(finding: RenderableFinding) -> tuple[DerivedExpectation, ...]:
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
    if not isinstance(finding, (Finding, RedlineFinding)):
        raise TypeError("finding must be a Finding or RedlineFinding")
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
    findings: Sequence[RenderableFinding],
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
        if any(
            isinstance(finding, RedlineFinding) and finding.calculation_available
            for finding in findings
        ):
            paragraph(
                "No calculated value is reproduced on this redline. Consult the immutable findings "
                "report for the exact deterministic calculation."
            )
        else:
            paragraph(
                "None. No value in this report was calculated; every figure was read or given."
            )
    for expectation in derived:
        paragraph(f"{expectation.rule_id} — {expectation.name} (DERIVED): {expectation.value}")
        paragraph(expectation.calculation, "    ")
    line("")


def _reviewer_section(
    listing: _ReviewerListing,
    pages: dict[int, int],
    line: Callable[..., None],
    paragraph: Callable[..., None],
) -> None:
    """Every confirm, fix and answer a person gave, drawn or not: listed, never dropped (#1363)."""
    line("What a person answered", font="Helvetica-Bold", size=12.0)
    paragraph(
        'A person\'s answer is not a check result. "Confirmed by" means a reviewer looked at the '
        "AI's reading and agreed with it; the check's own result is listed separately, and a "
        "confirmation never turns it into PASS. Every answer is listed here, on the drawing or not."
    )
    line("")
    if not listing.marks:
        paragraph("None. No person confirmed, fixed or answered anything on this revision.")
    for number, mark in enumerate(listing.marks, start=1):
        paragraph(f"R{number} {mark.text} — {mark.subject}")
        for detail in mark.details:
            paragraph(detail, "    ")
        if number in listing.unplaced:
            paragraph(f"not on the drawing: {listing.unplaced[number]}", "    ")
        else:
            paragraph(f"marked on page {pages[number] + 1}", "    ")
        line("")


def _architect_section(
    listing: _ArchitectListing,
    pages: dict[int, int],
    line: Callable[..., None],
    paragraph: Callable[..., None],
) -> None:
    """The counts, then every "different" result, clouded or not: listed, never dropped."""
    line("Compared with the architect", font="Helvetica-Bold", size=12.0)
    paragraph(listing.compare.counts.line)
    if isinstance(listing.compare.counts, AiReviewCounts):
        paragraph(AI_REVIEW_NOTE)
    line("")
    if not listing.compare.different:
        paragraph("None. No architect dimension was found different from the vendor's.")
    for number, item in enumerate(listing.compare.different, start=1):
        paragraph(f"A{number} {item.words} \u2014 {item.subject}")
        if item.answer is ArchitectAnswer.AGREED:
            paragraph(f"Agreed by {item.answered_by}: the vendor is wrong.", "    ")
        if number in listing.unplaced:
            paragraph(f"not clouded on the drawing: {listing.unplaced[number]}", "    ")
        else:
            paragraph(f"clouded on page {pages[number] + 1}", "    ")
        line("")


def _listing(
    package: RedlinePackage,
    findings: Sequence[RenderableFinding],
    marked: int,
    unplaced: Sequence[Unplaced],
    pages_with_marks: frozenset[int],
    clearance: VendorClearance | None,
    changed_values: ChangedValues | None,
    countertop_labels: Sequence[str],
    reviewer_listing: _ReviewerListing | None = None,
    reviewer_pages: dict[int, int] | None = None,
    architect_listing: _ArchitectListing | None = None,
    architect_pages: dict[int, int] | None = None,
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
        if _has_glyph(text):
            canvas.setFillColorRGB(0.1, 0.1, 0.1)
            _draw_text(canvas, margin, cursor, text, font, size)
        else:
            canvas.setFont(font, size)
            canvas.setFillColorRGB(0.1, 0.1, 0.1)
            canvas.drawString(margin, cursor, text)
        cursor -= size * 1.6

    def paragraph(text: str, indent: str = "") -> None:
        for wrapped in textwrap.wrap(text, columns) or [""]:
            line(f"{indent}{wrapped}")

    total_findings = len(findings)
    line("Redline summary", font="Helvetica-Bold", size=LISTING_FONT_SIZE * 1.8)
    if clearance is None:
        line("BEFORE REVIEW — NOT A FINAL REPORT", font="Helvetica-Bold", size=12.0)
    paragraph(
        f"{total_findings} finding(s) in this report for package revision "
        f"{package.package_revision_id}. {marked} marked on a drawing page. "
        f"{total_findings - marked} not marked, and listed below."
    )
    line("")

    if countertop_labels:
        line("Countertops in this report", font="Helvetica-Bold", size=12.0)
        for label in countertop_labels:
            paragraph(label)
        line("")

    if architect_listing is not None:
        _architect_section(architect_listing, architect_pages or {}, line, paragraph)

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

    if changed_values is not None:
        line("Project values that differ from GV standards", font="Helvetica-Bold", size=12.0)
        if changed_values.message is not None:
            paragraph(changed_values.message)
        else:
            for value in changed_values.company_standards_displaced or ("None",):
                paragraph(value)
            line("Required values not set", font="Helvetica-Bold", size=12.0)
            for value in changed_values.outstanding or ("None",):
                paragraph(value)
            # The vendor's copy: each setting's plain name and value, never who typed it or a note.
            line("Values entered for this job", font="Helvetica-Bold", size=12.0)
            for text in [value.plain() for value in changed_values.entered] or ["None"]:
                paragraph(text)
            line("GV defaults used", font="Helvetica-Bold", size=12.0)
            for text in [value.plain() for value in changed_values.defaults_used] or ["None"]:
                paragraph(text)
        line("")

    if reviewer_listing is not None:
        _reviewer_section(reviewer_listing, reviewer_pages or {}, line, paragraph)

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
