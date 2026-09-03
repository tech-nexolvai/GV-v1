"""Writing down what the reader saw.

The first code to put a row in `observation_candidates`. Until now the reader could read a drawing and
the rasteriser could render one, and every reading evaporated when the function returned — the one
`session.add` for this table sits in a function with no callers, and takes the row as a parameter
rather than building one.

**Candidates, not observations, and that is the whole scope.** `evidence/normalize.py` refuses a
candidate with no semantic type and *will not infer one from position*; nothing in the system assigns
one; and `docs/DESIGN.md` names text-to-item association as one of four things it declines to specify
until real drawings exist. `observation_candidates` exists for exactly this state — a reading that
happened, recorded before anything knows what it is a reading *of*. Its `semantic_guess` is nullable
for the same reason.

Three things the database enforces that a writer has to meet rather than discover.

**The value is all-or-nothing.** `(value_numerator, value_denominator, unit)` must be entirely present
or entirely absent, so a token that would not parse is stored with no value at all rather than a value
of zero — and it is still stored, because a dimension nobody could read and a dimension that was not
there must not look alike.

**The fraction must be reduced.** A `before_insert` listener rejects `gcd(n, d) != 1`, so that one
number has one representation and two readings of the same dimension compare equal. `Fraction` already
reduces; this is a guard against something reaching the row another way.

**The geometry is image space, in integer pixels.** `coordinate_space` is checked against the literal
`'image'`, and the reader carries those pixels alongside its stored-space polygon precisely because
they cannot be recovered afterwards: `dpi`, `media_box` and `crop_box` are not persisted anywhere.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionRun
from extraction.manifest import PageManifest
from extraction.reader import TextItem
from units.imperial import ImperialParseError, parse_imperial
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches

__all__ = [
    "UNKNOWN_UNIT_FLAG",
    "UNPARSED_FLAG",
    "open_extraction_run",
    "persist_manifest",
    "record_candidates",
]

#: Flagged rather than dropped. A token the parser could not read is still a reading that happened,
#: and the flag is what lets a reviewer be shown it as unread rather than as absent.
UNPARSED_FLAG = "unparsed_token"

#: A bare number, recorded with no value because its unit is unknown.
#:
#: **This replaced an `unmarked_unit` option, and the reason is worth keeping.** The idea was that a
#: caller who knew the sheet was drawn in inches could say so, and a bare `38 3/4` would parse. Run
#: against a real page it recorded `984 mm` as **984 inches** — 82 feet — because `extract_words`
#: splits at the space, so the number and its unit marker arrive as two separate tokens and the bare
#: one looks exactly like a number that never had a unit.
#:
#: A caller can know what a *sheet* is drawn in. It cannot know whether *this* token's unit was
#: tokenised away, and the two are indistinguishable by the time the parser sees them. So a bare
#: number is recorded with no value: the reading is kept, and nothing is claimed about it.
UNKNOWN_UNIT_FLAG = "no_unit_on_token"


def open_extraction_run(
    session: Session,
    *,
    task_run_id: UUID,
    extractor: str,
    extractor_version: str,
    config_hash: str,
) -> ExtractionRun:
    """The run a set of candidates came from, created once per stage execution.

    Nothing has ever created one of these, though the table has existed since the run records landed
    and `app/budget/attribution.py` already reads it to attribute cost per extractor. A candidate
    carries no extractor of its own — it points here — so without a run there is no way to say what
    read a number, and a re-read by a newer version would be indistinguishable from the first.

    Reused when one already exists for this task run and extractor, because a stage that is
    redelivered has already claimed its task run and should not accumulate a run per attempt.

    **`config_hash` is part of that identity, and leaving it out was a provenance bug** (found in
    review on #484, fixed in #487). Without it, a second read of the same task run at a different DPI
    got the *first* run back: the new candidates carried geometry rendered at the new DPI while the
    row still recorded the old one, so the stored evidence described a configuration that did not
    produce it. Nothing downstream could detect that, because both the geometry and the hash are
    individually well-formed. A different configuration is a different run.
    """
    existing = session.execute(
        select(ExtractionRun).where(
            ExtractionRun.task_run_id == task_run_id,
            ExtractionRun.extractor == extractor,
            ExtractionRun.extractor_version == extractor_version,
            ExtractionRun.config_hash == config_hash,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    run = ExtractionRun(
        task_run_id=task_run_id,
        extractor=extractor,
        extractor_version=extractor_version,
        config_hash=config_hash,
    )
    session.add(run)
    session.flush()
    return run


def record_candidates(
    session: Session,
    texts: Sequence[TextItem],
    *,
    document_version_id: UUID,
    page_id: UUID,
    extraction_run_id: UUID,
) -> list[ObservationCandidate]:
    """Persist every text run the reader found on one page.

    **Every one, including the ones that are not dimensions.** A page's title block, its revision
    note and its sheet number all come back from the reader as text, and none of them parses as a
    measurement. They are recorded anyway, with no value and a flag: which text on a drawing is a
    dimension is decided by association, which is not built, and a reader that pre-filtered would be
    making that decision by guess — and making it invisibly, since a dropped token leaves no trace.

    **Only a token that carries its own unit gets a value.** `38 3/4"` and `984 mm` parse; a bare
    `38` does not, and is stored with the reading intact and no number. That is stricter than it
    first looks necessary, and it is a correction: an earlier version let a caller declare the
    sheet's unit, and recorded `984 mm` as 984 *inches* — 82 feet — because word splitting had
    already separated the `mm`.

    **Writes nothing when this run has already recorded this page.** The table is append-only and a
    re-read is a new reading, but a *redelivery* is not a re-read — it is the same work arriving
    twice, and duplicating it makes one dimension look like two.
    """
    # **A redelivery must not double the rows.** A killed worker is redelivered, reclaims the same
    # task run, and reuses the same extraction run — and this used to write the page's candidates a
    # second time. The rows are individually correct, which is what makes it bad: downstream
    # association cannot tell two identical readings of one dimension from one dimension read twice,
    # and `38` appears on a drawing more than once for real.
    #
    # Keyed on the run and the page rather than on text, because two genuinely distinct text runs with
    # the same characters are ordinary and must both survive. A re-read under a different
    # configuration is a *different* `ExtractionRun` now that `config_hash` is part of its identity,
    # so this suppresses only the repeat of work already recorded. Found in review on #484 (#487).
    already = list(
        session.execute(
            select(ObservationCandidate).where(
                ObservationCandidate.extraction_run_id == extraction_run_id,
                ObservationCandidate.page_id == page_id,
            )
        ).scalars()
    )
    if already:
        return already

    written: list[ObservationCandidate] = []
    for item in texts:
        measurement, flags = _parse(item.text)
        row = ObservationCandidate(
            document_version_id=document_version_id,
            page_id=page_id,
            extraction_run_id=extraction_run_id,
            raw_text=item.text,
            # All three or none of the three: the check constraint refuses a partly-filled value, and
            # a value of zero for something unreadable would be a number nobody wrote.
            value_numerator=None if measurement is None else measurement.exact.numerator,
            value_denominator=None if measurement is None else measurement.exact.denominator,
            unit=None if measurement is None else measurement.unit.value,
            unit_guess=None if measurement is None else measurement.unit.value,
            # No semantic type, and none inferred. Nothing in the system assigns one and normalisation
            # refuses to guess from position, which is the behaviour rather than a shortfall.
            semantic_guess=None,
            polygon=[[point.x, point.y] for point in item.image_extent],
            coordinate_space="image",
            # A deterministic read of a text object is not a probabilistic one. `None` says there is
            # no confidence to report, where `1.0` would claim a certainty that means nothing here.
            confidence=None,
            ambiguity_flags=list(flags),
        )
        session.add(row)
        written.append(row)

    session.flush()
    return written


def _parse(text: str) -> tuple[Measurement | None, tuple[str, ...]]:
    """A text run as an exact measurement, or nothing and a reason.

    `normalise_to_inches` reads a token that names its own unit — `984 mm`, `3'-6 1/2"`, `38 3/4"` —
    and converts it exactly. Anything else comes back without a value.

    The two failures are told apart because they mean different things to whoever reads the row. A
    bare number is a dimension whose unit is unknown, quite possibly because tokenisation split it
    off; a title block is simply not a dimension. Recording both as "unparsed" would lose the
    distinction that decides whether anybody should look.

    A failure is not an error either way. Most text on a drawing is not a dimension.
    """
    try:
        return normalise_to_inches(text), ()
    except UnitNormalisationError:
        # No value, and the flag says which kind of nothing this is. Both branches abstain; neither
        # swallows, because a reading that produced no measurement still has to say why.
        return None, (_unvalued_reason(text),)


def _unvalued_reason(text: str) -> str:
    """Why a text run carries no measurement — a bare number, or not a number at all.

    Split out from `_parse` so that no `except` branch ends in a bare `pass`. That is a rule the
    repo enforces (`.semgrep/gv-rules.yaml`, `gv-no-silently-swallowed-errors`) and it is the right
    rule here: an exception on this path decides what a reviewer is shown, so each one has to name
    an outcome rather than fall through to whatever comes next.
    """
    try:
        parse_imperial(text)
    except ImperialParseError:
        return UNPARSED_FLAG
    # It parsed as a number but carried no unit. The value is deliberately not kept: see
    # `UNKNOWN_UNIT_FLAG`.
    return UNKNOWN_UNIT_FLAG


def persist_manifest(session: Session, manifest: PageManifest) -> list[Page]:
    """The document's pages, as rows every later stage can point at.

    Nothing has ever written one. `app/api/documents.py` records a `SourceArtifact`, a
    `DocumentVersion` and a page count, then stops — so `observation_candidates.page_id` had nothing
    to reference, and page classification, the fan-out and evidence all read a table that was always
    empty.

    **Idempotent, because a workflow redelivery is ordinary.** `pages` is append-only and unique on
    `(document_version_id, index)`, so a second attempt must recognise what is already there rather
    than raise — and it cannot rewrite it either. Existing rows are returned as they stand: if a
    re-read disagrees with what was stored, that is a fact about the document worth surfacing, not
    something for this function to paper over by overwriting a row the constraint forbids it to
    touch.
    """
    stored = {
        page.index: page
        for page in session.execute(
            select(Page).where(Page.document_version_id == manifest.document_version_id)
        ).scalars()
    }

    pages: list[Page] = []
    for record in manifest.pages:
        existing = stored.get(record.index)
        if existing is not None:
            pages.append(existing)
            continue
        page = Page(
            document_version_id=manifest.document_version_id,
            index=record.index,
            content_hash=record.content_hash,
            width_pt=record.width_pt,
            height_pt=record.height_pt,
            rotation=record.rotation,
            has_vector_text=record.has_vector_text,
            render_failed=record.render_failed,
            sheet_number=record.sheet_number,
            # `None` is a real answer — nobody could classify it — and the column is nullable for
            # that reason rather than awaiting a default.
            page_type=None if record.page_type is None else record.page_type.value,
        )
        session.add(page)
        pages.append(page)

    session.flush()
    return pages
