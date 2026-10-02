"""Search a package's own words, and get back where they are — never what they say (#836).

The readers store text one run at a time, and a run is often a single word: `ACCESS`, `PANEL`, `BY`.
Nothing could find "the passage that states the overhang", because no passage existed. This module
builds them, keeps them in `text_phrases`, and searches them.

**The search answers with ids, never with text and never with a number.** A result is a phrase id
and the ids of the runs it was built from (`PhraseHit`), and nothing else: no excerpt, no score.
"Web search may fetch a document. A document may yield a number. Search results may never yield a
number directly" (`docs/AI_FILLS_THE_FORM_PLAN.md` §4). A caller that wants the value a passage
states reads it from the run's own exact numerator and denominator, through the same gates as every
other reading, and a reviewer still confirms it.

**Only text that is the file's own characters is indexed.** The vector text (`pdfplumber`), the
reviewer's `/FreeText` markup, the vendor's CAD notes and a pasted drawing's font text — the routes
`workflow.stages.EXACT_TEXT_EXTRACTORS` names (#792). An allowlist, so a new reader is left out until
someone adds it on purpose. A model's reading and an OCR engine's are readings of pixels, and two of
them have already agreed on a wrong value (`3 3/4"` for a stacked `3/4"`); a passage built from one
would be a guess presented as the drawing's words.

**Layout anchors are left out by what they are, not by what they say.** The layout step stores a
sentence about its answer (`layout wall_config: abstained: ...`) as a candidate in the vector run,
because a crop needs a candidate to belong to. It is the system talking, not the drawing. Such a row
is recognised by owning a crop that a `layout_proposals` row cites. Crops are content-addressed, so a
layout proposal could in principle cite a reading's identical crop; that reading would then be left
out too, which loses a passage rather than adding a wrong one.

**Phrases are built from runs on one line, by one route, on one page.**

- *One line* means the same top and the same bottom, to the pixel. The vector and pasted-drawing
  readers take their boxes from the font, not the ink, so the words of one line in one font share
  both exactly; a word in another size, or on the next line, has a different box and starts its own
  phrase. A CAD note's box is the rectangle the CAD program wrote, and two join only where they
  share both edges.
- *The gap* is the horizontal space from one run's right edge to the next run's left edge, measured
  in line heights so it means the same at every font size and resolution. Runs join when that space
  is zero or more and at most `PhraseGrouping.gap_line_heights`. Overlapping runs never join: two
  strings printed in one place are not one after the other.
- *The gap has no default.* It is measured on the client's drawings and stated by the deployment;
  the value used is recorded on each phrase (`text_phrases.grouping`) and on the pull request, not
  here, because a value written in this docstring would become the one everybody uses.
- *A reviewer's note is one phrase on its own.* An annotation is already a whole passage, written as
  one, and joining two that happen to share a line would make a passage nobody wrote.
- *Sideways text is not recognised as sideways.* The stored box carries no rotation, so a sideways
  run's "line height" is its length, and two sideways runs with the same top and bottom side by side
  are measured like any other pair. The words of one sideways line sit above and below each other,
  so they are never joined. Telling sideways text apart needs the rotation the reader knew and did
  not store.

**Tagged by origin.** `markup` for the reviewer's notes; `dimension` when at least one run is
attached to a dimension line by its latest association row; `note` for everything else.

**Scoped to one package revision, always.** Runs are gathered through the revision's own documents,
every phrase row names its revision, the search filters on it, and every row the database returns is
checked against it before it is handed back.

Source: issue #836, plan step 3.1 on #798. Verification: `tests/retrieval/test_package_text.py`.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import RowMapping, select, text
from sqlalchemy.orm import Session

from app.evidence.sides import MARKUP_ROUTE
from app.models.document import PackageRevisionDocument, Page
from app.models.evidence import (
    EvidenceArtifact,
    LayoutProposal,
    ObservationAssociation,
    ObservationCandidate,
)
from app.models.package import PackageRevision
from app.models.package_text import PhraseTag, TextPhrase, TextPhraseMember
from app.models.runs import ExtractionRun

__all__ = [
    "EXACT_TEXT_ROUTES",
    "PackageTextError",
    "Phrase",
    "PhraseBuild",
    "PhraseGrouping",
    "PhraseHit",
    "TextRun",
    "build_package_phrases",
    "group_runs",
    "package_runs",
    "search_package_text",
]

#: The routes whose text is the file's own characters, spelled as `workflow.stages` spells them.
#:
#: Restated rather than imported, because that module reads PDFs and renders pages and an index over
#: stored rows has no business loading either. `tests/retrieval/test_package_text.py` fails if the two
#: part, in either direction.
EXACT_TEXT_ROUTES: Final = frozenset(
    {"pdfplumber", MARKUP_ROUTE, "extraction.cad_text", "extraction.stamp_text"}
)

SEARCH_SQL: Final = """
WITH query AS (
    SELECT websearch_to_tsquery('simple', CAST(:query AS text)) AS words
)
SELECT
    phrase.id AS phrase_id,
    phrase.package_revision_id,
    array_agg(member.candidate_id ORDER BY member.position) AS candidate_ids
FROM text_phrases AS phrase
JOIN text_phrase_members AS member ON member.phrase_id = phrase.id
CROSS JOIN query
WHERE phrase.package_revision_id = :package_revision_id
  AND phrase.build_id = :build_id
  AND phrase.tag = ANY(CAST(:tags AS text[]))
  AND (
      to_tsvector('simple'::regconfig, phrase.content) @@ query.words
      OR phrase.content ILIKE :fragment
  )
GROUP BY phrase.id, phrase.package_revision_id, phrase.position, phrase.content, query.words
ORDER BY
    ts_rank_cd(to_tsvector('simple'::regconfig, phrase.content), query.words) DESC,
    phrase.position,
    phrase.id
""".strip()
"""The search, as the database runs it.

`to_tsvector('simple'::regconfig, content)` is spelled exactly as `ix_text_phrases_content_words` is,
because PostgreSQL uses an expression index only for the expression it was built on. The rank orders
the rows and never leaves this statement: it is a binary float, and nothing outside needs it."""


class PackageTextError(ValueError):
    """The database returned a row outside the search's contract.

    A phrase of another revision, a phrase with no runs, or an id that is not a UUID.
    """


@dataclass(frozen=True, slots=True)
class PhraseGrouping:
    """How far apart two runs on one line may be and still be one phrase. Stated, never defaulted.

    **No default, and that is the point.** The right gap is a fact about how the client's drawings
    are typeset, so it is measured on them and stated by the deployment. A default here would ship
    one measurement as every drawing's truth. The worker reads it from `GV_PHRASE_GAP_LINE_HEIGHTS`
    and builds nothing when it is not set.
    """

    gap_line_heights: Decimal
    """The widest space between two runs that still joins them, as a multiple of the line's height.
    Zero is allowed, and joins only runs that touch."""

    def __post_init__(self) -> None:
        value = self.gap_line_heights
        if isinstance(value, float):
            raise TypeError(
                "gap_line_heights must be a Decimal, never a float: which runs join would then "
                "depend on binary rounding at exactly the boundary the setting draws"
            )
        if not isinstance(value, Decimal) or not value.is_finite():
            raise ValueError("gap_line_heights must be a finite Decimal")
        if value < 0:
            raise ValueError("gap_line_heights cannot be negative; overlapping runs never join")

    @property
    def rule(self) -> str:
        """The grouping as text, for `text_phrases.grouping` and the build's digest."""
        return f"same top and bottom; gap from 0 to {self.gap_line_heights} line heights"


@dataclass(frozen=True, slots=True)
class TextRun:
    """One stored run of exact text, with the facts grouping and tagging need."""

    candidate_id: UUID
    page_id: UUID
    page_index: int
    extraction_run_id: UUID
    route: str
    text: str
    box: tuple[int, int, int, int] | None
    """`(left, top, right, bottom)` in the image pixels the candidate's polygon is stored in, or
    `None` where the stored polygon cannot be read as one. A run with no box is still a phrase and can
    still be found; it is never joined to a neighbour it cannot be shown to sit beside."""

    attached: bool
    """Whether this run's latest association row attaches it to a dimension line."""

    @property
    def markup(self) -> bool:
        """Whether the reviewer wrote it, rather than the drawing."""
        return self.route == MARKUP_ROUTE


@dataclass(frozen=True, slots=True)
class Phrase:
    """Runs that read as one passage, left to right, and what kind of passage they are."""

    runs: tuple[TextRun, ...]
    tag: PhraseTag

    @property
    def content(self) -> str:
        """The runs' text joined with single spaces — what the index is built on."""
        return " ".join(run.text for run in self.runs)


@dataclass(frozen=True, slots=True)
class PhraseHit:
    """Where a passage is: the phrase, and the runs it was built from. Nothing it says.

    **Two ids, and no field that could hold a number.** No excerpt, because an excerpt of a drawing's
    text carries the drawing's numbers; no score, because a score is a number and a caller holding one
    has something to compare. The order of the results is the ranking.

    `candidate_ids` are the runs — `observation_candidates` rows — in reading order along the line.
    """

    phrase_id: UUID
    candidate_ids: tuple[UUID, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.phrase_id, UUID):
            raise TypeError("phrase_id must be a UUID")
        if not isinstance(self.candidate_ids, tuple) or not self.candidate_ids:
            raise TypeError("candidate_ids must be a non-empty tuple")
        if not all(isinstance(candidate_id, UUID) for candidate_id in self.candidate_ids):
            raise TypeError("candidate_ids must contain only UUIDs")


@dataclass(frozen=True, slots=True)
class PhraseBuild:
    """What one build decided, and whether it had to write anything to say so."""

    build_id: UUID | None
    """The build that is current once this returns, or `None` when the revision has no build because
    there has never been anything to index."""

    written: bool
    """`False` when nothing was added: the current build already said exactly this, or there was
    nothing to index."""

    phrases: tuple[Phrase, ...]

    def summary(self) -> dict[str, object]:
        """Counts only, for the worker's log. A drawing's text has no place in a log line."""
        tags = Counter(phrase.tag for phrase in self.phrases)
        return {
            "built": True,
            "written": self.written,
            "phrases": len(self.phrases),
            "runs": sum(len(phrase.runs) for phrase in self.phrases),
            **{tag.value: tags[tag] for tag in PhraseTag},
        }


def _box(polygon: object) -> tuple[int, int, int, int] | None:
    """The polygon's bounding box, or `None` if it is not a list of integer pixel pairs.

    Taken from the minimum and maximum rather than from the points' order: the markup route stores
    its corners starting at the bottom left, the vector route at the top left.
    """
    if not isinstance(polygon, list) or not polygon:
        return None
    xs: list[int] = []
    ys: list[int] = []
    for point in polygon:
        if not isinstance(point, list) or len(point) != 2:
            return None
        x, y = point
        if not all(isinstance(value, int) and not isinstance(value, bool) for value in (x, y)):
            return None
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def package_runs(session: Session, package_revision_id: UUID) -> tuple[TextRun, ...]:
    """Every run of exact text in this revision's documents that a phrase may be built from.

    **Only the newest run of each route on each page.** Extraction run again under a new
    configuration reads the same text into a new run, and indexing both would make every passage
    appear twice. Newest by the run's creation, then by its id, so two runs recorded in the same
    instant still choose the same one every time.

    Blank text is left out: it has no words to find, and the table refuses a blank phrase.
    """
    is_layout_anchor = (
        select(LayoutProposal.id)
        .join(EvidenceArtifact, EvidenceArtifact.id == LayoutProposal.crop_artifact_id)
        .where(EvidenceArtifact.candidate_id == ObservationCandidate.id)
        .exists()
    )
    rows = session.execute(
        select(ObservationCandidate, ExtractionRun.extractor, ExtractionRun.created_at, Page.index)
        .join(ExtractionRun, ExtractionRun.id == ObservationCandidate.extraction_run_id)
        .join(Page, Page.id == ObservationCandidate.page_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == ObservationCandidate.document_version_id,
        )
        .where(
            PackageRevisionDocument.package_revision_id == package_revision_id,
            ExtractionRun.extractor.in_(sorted(EXACT_TEXT_ROUTES)),
            ~is_layout_anchor,
        )
        .order_by(Page.index, ObservationCandidate.created_at, ObservationCandidate.id)
    ).all()

    newest: dict[tuple[UUID, str], tuple[datetime, UUID]] = {}
    for candidate, route, run_created_at, _ in rows:
        key = (candidate.page_id, route)
        stamp = (run_created_at, candidate.extraction_run_id)
        if key not in newest or stamp > newest[key]:
            newest[key] = stamp
    kept = [
        (candidate, route, page_index)
        for candidate, route, run_created_at, page_index in rows
        if newest[(candidate.page_id, route)] == (run_created_at, candidate.extraction_run_id)
        and candidate.raw_text.strip()
    ]

    attached = _attached(session, [candidate.id for candidate, _, _ in kept])
    return tuple(
        TextRun(
            candidate_id=candidate.id,
            page_id=candidate.page_id,
            page_index=page_index,
            extraction_run_id=candidate.extraction_run_id,
            route=route,
            text=candidate.raw_text,
            box=_box(candidate.polygon),
            attached=candidate.id in attached,
        )
        for candidate, route, page_index in kept
    )


def _attached(session: Session, candidate_ids: Sequence[UUID]) -> frozenset[UUID]:
    """The runs whose latest association row attaches them to a line.

    The latest, as `workflow/propose.py` reads it: `open_extraction_run` keys a run on its
    configuration, so a second row is a re-association under the deployment's current thresholds,
    not a competing opinion.
    """
    if not candidate_ids:
        return frozenset()
    latest: dict[UUID, bool] = {}
    for candidate_id, refusal_reason in session.execute(
        select(ObservationAssociation.candidate_id, ObservationAssociation.refusal_reason)
        .where(ObservationAssociation.candidate_id.in_(candidate_ids))
        .order_by(ObservationAssociation.created_at, ObservationAssociation.id)
    ).all():
        latest[candidate_id] = refusal_reason is None
    return frozenset(candidate_id for candidate_id, is_attached in latest.items() if is_attached)


def _joins(before: TextRun, after: TextRun, limit: Fraction) -> bool:
    """Whether `after` continues the phrase `before` ends, on a line both share.

    Exact: the pixels are integers and the limit is a `Fraction`, so the boundary the setting draws
    is the boundary that is applied.
    """
    if before.box is None or after.box is None:
        return False
    _, top, right, bottom = before.box
    height = bottom - top
    if height <= 0:
        return False
    gap = after.box[0] - right
    return gap >= 0 and Fraction(gap) <= limit * height


def _tag(runs: tuple[TextRun, ...]) -> PhraseTag:
    if runs[0].markup:
        return PhraseTag.MARKUP
    if any(run.attached for run in runs):
        return PhraseTag.DIMENSION
    return PhraseTag.NOTE


def group_runs(runs: Iterable[TextRun], grouping: PhraseGrouping) -> tuple[Phrase, ...]:
    """Join runs into phrases by line and gap, and put the phrases in reading order.

    Reading order is page, route, then top to bottom and left to right, so a build's positions are
    the same every time it is given the same runs.
    """
    if not isinstance(grouping, PhraseGrouping):
        raise TypeError("grouping must be a PhraseGrouping")
    limit = Fraction(grouping.gap_line_heights)
    alone: list[tuple[TextRun, ...]] = []
    lines: dict[tuple[UUID, UUID, int, int], list[TextRun]] = {}
    for run in runs:
        if run.markup or run.box is None:
            alone.append((run,))
            continue
        _, top, _, bottom = run.box
        lines.setdefault((run.page_id, run.extraction_run_id, top, bottom), []).append(run)

    groups = list(alone)
    for line in lines.values():
        line.sort(key=lambda run: (run.box, run.candidate_id.int))
        current = [line[0]]
        for run in line[1:]:
            if _joins(current[-1], run, limit):
                current.append(run)
            else:
                groups.append(tuple(current))
                current = [run]
        groups.append(tuple(current))

    def reading_order(group: tuple[TextRun, ...]) -> tuple[object, ...]:
        first = group[0]
        left, top = (first.box[0], first.box[1]) if first.box is not None else (-1, -1)
        return (first.page_index, first.page_id.int, first.route, top, left, first.candidate_id.int)

    return tuple(Phrase(runs=group, tag=_tag(group)) for group in sorted(groups, key=reading_order))


def _digest(grouping: PhraseGrouping, phrases: Sequence[Phrase]) -> str:
    """Everything a build decides, as one hash: the rule, and each phrase's tag and runs in order."""
    lines = [grouping.rule] + [
        f"{phrase.tag.value}:{','.join(str(run.candidate_id) for run in phrase.runs)}"
        for phrase in phrases
    ]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


def _current_build(session: Session, package_revision_id: UUID) -> tuple[UUID, str] | None:
    """The newest build of this revision and its digest, or `None` if it has never been built.

    Newest by `created_at`, then by `build_id` so two builds recorded in the same instant still order
    the same way every time — the rule `workflow/propose.py` uses for the newest proposal.
    """
    row = session.execute(
        select(TextPhrase.build_id, TextPhrase.build_digest)
        .where(TextPhrase.package_revision_id == package_revision_id)
        .order_by(TextPhrase.created_at.desc(), TextPhrase.build_id.desc())
        .limit(1)
    ).first()
    return None if row is None else (row[0], row[1])


def build_package_phrases(
    session: Session, package_revision_id: UUID, grouping: PhraseGrouping
) -> PhraseBuild:
    """Build this revision's phrases and file them as a new build, unless nothing has changed.

    Appends, because both tables are append-only: a rebuild is a second set of rows, and the newest
    set is the current one. A build that would decide exactly what the current one decided writes
    nothing and returns the current build, so a redelivered extraction does not double every phrase.
    A revision with no exact text writes nothing either; an empty build would be a row saying nothing.
    """
    if not isinstance(package_revision_id, UUID):
        raise TypeError("package_revision_id must be a UUID")
    if session.get(PackageRevision, package_revision_id) is None:
        raise ValueError(f"no package revision {package_revision_id}")
    phrases = group_runs(package_runs(session, package_revision_id), grouping)
    digest = _digest(grouping, phrases)
    current = _current_build(session, package_revision_id)
    if current is not None and current[1] == digest:
        return PhraseBuild(build_id=current[0], written=False, phrases=phrases)
    if not phrases:
        return PhraseBuild(
            build_id=None if current is None else current[0], written=False, phrases=()
        )

    build_id = uuid4()
    rows: list[tuple[TextPhrase, Phrase]] = []
    for position, phrase in enumerate(phrases):
        row = TextPhrase(
            package_revision_id=package_revision_id,
            build_id=build_id,
            build_digest=digest,
            grouping=grouping.rule,
            page_id=phrase.runs[0].page_id,
            extraction_run_id=phrase.runs[0].extraction_run_id,
            position=position,
            tag=phrase.tag.value,
            content=phrase.content,
        )
        session.add(row)
        rows.append((row, phrase))
    # Phrases first, members after: there is no relationship between the two models for the unit of
    # work to order the inserts by, and a member written first would name a phrase that is not there.
    session.flush()
    for row, phrase in rows:
        for member_position, run in enumerate(phrase.runs):
            session.add(
                TextPhraseMember(
                    phrase_id=row.id, candidate_id=run.candidate_id, position=member_position
                )
            )
    session.flush()
    return PhraseBuild(build_id=build_id, written=True, phrases=phrases)


def _fragment(query: str) -> str:
    """`query` as an `ILIKE` pattern that matches it anywhere, with its own `%`, `_` and `\\` literal."""
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _hit(row: RowMapping, package_revision_id: UUID) -> PhraseHit:
    """One returned row as a hit, once it is shown to be this revision's and to name its runs."""
    if row.get("package_revision_id") != package_revision_id:
        raise PackageTextError("the database returned a phrase outside the requested revision")
    phrase_id = row.get("phrase_id")
    candidate_ids = row.get("candidate_ids")
    if not isinstance(phrase_id, UUID):
        raise PackageTextError("the database returned a phrase id that is not a UUID")
    if not isinstance(candidate_ids, list | tuple) or not candidate_ids:
        raise PackageTextError("the database returned a phrase with no runs")
    try:
        return PhraseHit(phrase_id=phrase_id, candidate_ids=tuple(candidate_ids))
    except TypeError as error:
        raise PackageTextError(str(error)) from error


def search_package_text(
    session: Session,
    package_revision_id: UUID,
    query: str,
    *,
    tags: Iterable[PhraseTag] | None = None,
) -> tuple[PhraseHit, ...]:
    """The phrases of this revision's current build that match `query`, best first. Ids only.

    A phrase matches when its words match `query` as a web-style search — words, `"quoted phrases"`,
    `-excluded` — or when `query` appears anywhere in its text, which finds a word glued to its
    neighbour. Results are ordered by PostgreSQL's cover-density rank of the word match, which is
    zero for a phrase found only by a fragment, and ties keep reading order.

    `tags` narrows the search to those kinds of passage; `None` searches every kind. A revision that
    has never been built has no phrases, and the answer is an empty tuple rather than an error.
    """
    if not isinstance(package_revision_id, UUID):
        raise TypeError("package_revision_id must be a UUID")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    wanted = tuple(PhraseTag) if tags is None else tuple(tags)
    if not wanted or not all(isinstance(tag, PhraseTag) for tag in wanted):
        raise ValueError("tags must name at least one PhraseTag")

    current = _current_build(session, package_revision_id)
    if current is None:
        return ()
    rows = session.execute(
        text(SEARCH_SQL),
        {
            "package_revision_id": package_revision_id,
            "build_id": current[0],
            "query": query,
            "fragment": _fragment(query.strip()),
            "tags": sorted(tag.value for tag in wanted),
        },
    ).mappings()
    return tuple(_hit(row, package_revision_id) for row in rows)
