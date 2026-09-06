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
from typing import Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.document import Page
from app.models.evidence import ObservationCandidate
from app.models.runs import ExtractionFailure, ExtractionRun
from evidence.candidate import ObservationCandidate as DomainCandidate
from evidence.coordinates import ImagePoint
from evidence.corroborate import corroborate
from extraction.manifest import PageManifest
from extraction.ocr import OcrItem
from extraction.reader import TextItem
from units.dual import DualDimension, DualDimensionParseError, parse_dual
from units.imperial import ImperialParseError, parse_imperial
from units.measurement import Measurement, Unit
from units.normalise import UnitNormalisationError, normalise_to_inches

__all__ = [
    "UNKNOWN_UNIT_FLAG",
    "UNPARSED_FLAG",
    "open_extraction_run",
    "persist_manifest",
    "record_candidates",
    "record_ocr_candidates",
    "record_unreadable_document",
    "record_unreadable_page",
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
    page_index: int,
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

    run = session.get(ExtractionRun, extraction_run_id)
    if run is None:
        raise ValueError(
            f"no extraction run {extraction_run_id}: a candidate must name what read it"
        )

    written: list[ObservationCandidate] = []
    for item in texts:
        measurement, flags, dual = _parse(item.text)
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
        # **Set before the insert, never after.** `observation_candidates` is append-only and 0013
        # enforces it with a trigger, so assigning these once the row exists would be an UPDATE the
        # database refuses. The lane's answer is available here because it compares two readings
        # inside one token: no other row has to exist first.
        row.corroboration_status, row.corroboration_lane = _corroboration(
            row, dual=dual, run=run, page_index=page_index
        )
        session.add(row)
        written.append(row)

    session.flush()
    return written


def record_ocr_candidates(
    session: Session,
    items: Sequence[OcrItem],
    *,
    document_version_id: UUID,
    page_id: UUID,
    extraction_run_id: UUID,
    page_index: int,
) -> list[ObservationCandidate]:
    """The same rows, from the other reading route.

    Two differences from the vector route, and both are about honesty rather than convenience.

    **Confidence is stored.** `record_candidates` writes `None`, because a deterministic read of a
    text object is not a probabilistic one and `1.0` would claim a certainty that means nothing. An
    OCR engine really does report how sure it is, so the number is kept — and kept only. Nothing
    filters, ranks or gates on it; a low-confidence reading that were quietly dropped would be
    indistinguishable from a page with nothing on it, which is the failure this whole route exists to
    fix.

    **The parsing rule is unchanged.** A token carrying its own unit gets a value and a bare number
    does not, exactly as for vector text. This route happens to suffer the `984 mm` splitting problem
    less, because the engine returns a line whole where `extract_words` splits at the space — but that
    is a property of the engine, not a guarantee, and the rule does not soften for it.

    Idempotent per run and page, for the reason `record_candidates` gives: a redelivery is the same
    work arriving twice, not a second reading.
    """
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

    run = session.get(ExtractionRun, extraction_run_id)
    if run is None:
        raise ValueError(
            f"no extraction run {extraction_run_id}: a candidate must name what read it"
        )

    written: list[ObservationCandidate] = []
    for item in items:
        measurement, flags, dual = _parse(item.text)
        row = ObservationCandidate(
            document_version_id=document_version_id,
            page_id=page_id,
            extraction_run_id=extraction_run_id,
            raw_text=item.text,
            value_numerator=None if measurement is None else measurement.exact.numerator,
            value_denominator=None if measurement is None else measurement.exact.denominator,
            unit=None if measurement is None else measurement.unit.value,
            unit_guess=None if measurement is None else measurement.unit.value,
            semantic_guess=None,
            polygon=[[point.x, point.y] for point in item.image_extent],
            coordinate_space="image",
            confidence=item.confidence,
            ambiguity_flags=list(flags),
        )
        # **Set before the insert, never after.** `observation_candidates` is append-only and 0013
        # enforces it with a trigger, so assigning these once the row exists would be an UPDATE the
        # database refuses. The lane's answer is available here because it compares two readings
        # inside one token: no other row has to exist first.
        row.corroboration_status, row.corroboration_lane = _corroboration(
            row, dual=dual, run=run, page_index=page_index
        )
        session.add(row)
        written.append(row)

    session.flush()
    return written


def _corroboration(
    row: ObservationCandidate,
    *,
    dual: DualDimension | None,
    run: ExtractionRun,
    page_index: int,
) -> tuple[str | None, str | None]:
    """What the dual-unit lane made of this reading, as (status, lane) for the row.

    **The decision is not made here.** `evidence/corroborate.py` owns what agreement means, and
    `units/policy.py:check_dual` owns the rounding band beneath it — an extraction module that
    decided agreement for itself would be a second opinion about evidence, which is the thing
    `DESIGN_PLATFORM.md` forbids. This assembles the reading into the shape that module takes and
    records its answer.

    **Agreement cannot promote past `RAW_CANDIDATE` today, and that is correct.** `corroborate`
    returns `CORROBORATED` only when a semantic type is known, and candidates are deliberately
    untyped until the real drawings (#274) and the vocabulary Q20 defers. So a consistent pair is
    recorded as `RAW_CANDIDATE` with the lane named — the lane ran, and the promotion is waiting on
    meaning rather than on evidence. **Disagreement is `CONFLICTING` now**, which is the half that is
    useful today: a drawing whose own two readings contradict each other is something a reviewer
    needs to see, and nothing about that needs to know what the dimension is.
    """
    if dual is None:
        return None, None

    result = corroborate(
        [
            DomainCandidate(
                candidate_id=str(row.id),
                extractor=run.extractor,
                extractor_version=run.extractor_version,
                raw_text=row.raw_text,
                # **The primary — the millimetre half — is the reading being corroborated.**
                # `corroborate` refuses a candidate whose measurement is not the dual's primary, and
                # it is right to: the alternate is the second opinion, not the subject. The row
                # itself keeps the inch reading as its value, because Q12 makes inches the only unit
                # a verdict may ever operate on. So the two differ on purpose — one is what the
                # lane examined, the other is what a rule could one day use.
                parsed_value=dual.primary,
                unit_guess=None if row.unit_guess is None else Unit(row.unit_guess),
                # Never inferred. The whole point of this lane is that it qualifies a *reading*
                # without knowing what the reading is of.
                semantic_guess=None,
                page=page_index,
                polygon=tuple(ImagePoint(x=int(x), y=int(y)) for x, y in row.polygon),
                confidence=row.confidence,
                ambiguity_flags=tuple(row.ambiguity_flags),
            )
        ],
        dual_dimension=dual,
    )
    if result.lane is None:
        # `NOT_CORROBORATED`: the token had no alternate after all, so no lane applied. `_dual`
        # already refuses those, and this is the belt to that braces.
        return None, None
    return result.status.value, result.lane.value


def _dual(text: str) -> DualDimension | None:
    """A token that states one dimension twice, in two units — or `None`.

    **Only a bracketed pair counts**, which is why `alternate` is tested rather than trusting that
    `parse_dual` succeeded. `parse_dual` also accepts a lone millimetre number, and using that here
    would make a bare `984` parse as 984 mm — reintroducing precisely the bug `UNKNOWN_UNIT_FLAG`
    documents, where a number separated from its unit marker by tokenisation was recorded in the
    wrong system. A bare number keeps meaning "unit unknown".
    """
    try:
        dual = parse_dual(text)
    except DualDimensionParseError:
        return None
    return dual if dual.alternate is not None else None


def _parse(text: str) -> tuple[Measurement | None, tuple[str, ...], DualDimension | None]:
    """A text run as an exact measurement, or nothing and a reason.

    `normalise_to_inches` reads a token that names its own unit — `984 mm`, `3'-6 1/2"`, `38 3/4"` —
    and converts it exactly. Anything else comes back without a value.

    The two failures are told apart because they mean different things to whoever reads the row. A
    bare number is a dimension whose unit is unknown, quite possibly because tokenisation split it
    off; a title block is simply not a dimension. Recording both as "unparsed" would lose the
    distinction that decides whether anybody should look.

    A failure is not an error either way. Most text on a drawing is not a dimension.

    **A dual token is read first, and its inch half is the value.** `984 [38 3/4]` states the same
    dimension in both units, and Q12 makes the inch reading the governing one: millimetres on a GV
    drawing are the vendor's machine reference and never a verdict operand. Until now
    `normalise_to_inches` could not read the token at all, so the whole reading — both halves of it —
    was stored with no value and an `unparsed` flag. The millimetre half is not discarded either; it
    is handed back for the corroboration lane, which is the one thing it is good for.
    """
    dual = _dual(text)
    if dual is not None and dual.alternate is not None:
        return dual.alternate, (), dual
    try:
        return normalise_to_inches(text), (), None
    except UnitNormalisationError:
        # No value, and the flag says which kind of nothing this is. Both branches abstain; neither
        # swallows, because a reading that produced no measurement still has to say why.
        return None, (_unvalued_reason(text),), None


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


def record_unreadable_document(
    session: Session,
    *,
    extraction_run_id: UUID,
    document_version_id: UUID,
    error: Exception,
) -> ExtractionFailure:
    """A drawing that would not parse, written down rather than skipped.

    Until this existed the stage caught `UnreadablePdf` and moved on, so the document contributed no
    pages, no candidates and no result — and the package still reported extraction as complete. A
    drawing nobody could read looked exactly like a drawing with nothing on it.

    Only the exception's *class* is kept. A `pdfminer` message can quote the bytes it choked on, and
    `AGENTS.md` §6 forbids drawing content in a trace for the reason it should not sit in a table
    either.
    """
    return _record(
        session,
        extraction_run_id=extraction_run_id,
        document_version_id=document_version_id,
        page_index=None,
        reason="document_unreadable",
        error=error,
    )


#: What `error_type` says when nothing was raised — the check that refused, named.
#:
#: The column exists to answer "what went wrong with this document"; for a digest mismatch the answer
#: is the comparison itself, and there is no exception class to name.
DIGEST_CHECK: Final = "DigestMismatch"


def record_digest_mismatch(
    session: Session,
    *,
    extraction_run_id: UUID,
    document_version_id: UUID,
) -> ExtractionFailure:
    """A document whose stored bytes are not the bytes that were uploaded.

    Different in kind from the other two failures: this file parses perfectly well. It is simply not
    the drawing anybody submitted, so reading it would produce dimensions that look like readings of
    the package under review and are readings of something else. That is worse than an unreadable
    file, because nothing about the result looks wrong.

    `ingest` reports the same mismatch, and reporting was all it could do — acting on it is an entry
    condition on this stage, which is where the bytes are in hand (#523). Checking here rather than
    trusting `ingest` also closes the window between the two: the artifact could change in between,
    and a stage that reads a document is the right place to establish that it is the right document.

    No exception to record, so the `error_type` names the check that refused rather than a class that
    was never raised. A blank would leave the row unable to say what happened.
    """
    return _record(
        session,
        extraction_run_id=extraction_run_id,
        document_version_id=document_version_id,
        page_index=None,
        reason="document_digest_mismatch",
        error=None,
    )


def record_unreadable_page(
    session: Session,
    *,
    extraction_run_id: UUID,
    document_version_id: UUID,
    page_index: int,
    error: Exception,
) -> ExtractionFailure:
    """One page that would not parse, in a document whose other pages might.

    Distinct from a page that was read and had nothing on it, which is an ordinary result and stays a
    candidate count of zero. Same distinction as the document-level case, one level down: "read
    nothing" and "could not be read" must not arrive at a reviewer as the same thing.
    """
    return _record(
        session,
        extraction_run_id=extraction_run_id,
        document_version_id=document_version_id,
        page_index=page_index,
        reason="page_unreadable",
        error=error,
    )


def _record(
    session: Session,
    *,
    extraction_run_id: UUID,
    document_version_id: UUID,
    page_index: int | None,
    reason: str,
    error: Exception | None,
) -> ExtractionFailure:
    """The row every recorder writes, so they cannot drift in what they store.

    `error` is optional because one failure has no exception behind it: a digest mismatch is a
    comparison that came out unequal, not something that was raised. `type(None).__name__` would put
    `NoneType` in the column — a word that describes our Python and not the drawing — so that case
    names the check instead.
    """
    failure = ExtractionFailure(
        extraction_run_id=extraction_run_id,
        document_version_id=document_version_id,
        page_index=page_index,
        reason=reason,
        # The class name, never `str(error)` — see `record_unreadable_document`.
        error_type=DIGEST_CHECK if error is None else type(error).__name__,
    )
    session.add(failure)
    session.flush()
    return failure


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
