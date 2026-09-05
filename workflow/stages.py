"""The pipeline stages: what each one actually does, and where the work stops.

Five of the six are built. `ingest` checks a document is still the one that was uploaded;
`extract_pages` reads its pages and records what was on them; `validate_evidence` cuts a picture of
every reading; `match` proposes which architectural item is which shop item; `run_checks` runs the
rules. `generate_outputs` still answers `{"implemented": False}`, the way `NoStages` answers
everything — deliberately loud, because a default returning `{}` would let a package walk the whole
pipeline and arrive at review looking processed.

**The stages were wired in the opposite order to the one the data flows in**, and that was
deliberate. `run_checks` came first, when extraction did not exist, because the engine had been
finished and tested for a long time and had never had a caller — every finding anyone had seen was
inserted by hand. Wiring the last stage first proved the spine and made extraction a matter of
supplying operands to something that already worked. The reading half (#517) followed the same
principle: `evidence/crop.py` and `retrieval/matching.py` were finished, tested and unreachable from
production, so what was missing was the connection rather than the algorithm.

**The pipeline stops at untyped candidates, and that is the state rather than a shortfall.** A
candidate is a reading with a picture of where it came from. Nothing gives it a meaning, so nothing
mints a canonical observation, so nothing becomes eligible as a verdict operand — `evidence/gate.py`
takes a canonical observation and there are none. Which value means "countertop depth" needs the real
drawings (#274) and a vocabulary Q20 explicitly defers, and a heuristic here would look like progress
and be a fabricated fact in a review. `docs/decisions/PIPELINE_SPINE.md` records the whole boundary.

**A check therefore still abstains unless a reviewer supplies the reading**, which `CLIENT_FACTS` Q7
blesses for exactly this. That is the honest result, not a broken one: no observations means no
operands means nothing to decide from.

**Why here and not in `app/`.** The `Stages` protocol is handed a SQLAlchemy `Session`, and
`sqlalchemy` is in the banned set for `verdict/` and `rules/` — those packages could not implement
this protocol if they wanted to. `workflow/` is the sanctioned bridge: it already imports `app` models
and the domain layers side by side (`workflow/retry.py`), which is exactly what a stage has to do.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from decimal import Decimal
from uuid import UUID

from opentelemetry.trace import Status, StatusCode
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.documents import storage_key
from app.db.base import utc_now
from app.evidence.record import (
    open_extraction_run,
    persist_manifest,
    record_candidates,
    record_ocr_candidates,
    record_unreadable_document,
    record_unreadable_page,
)
from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    PackageRevisionDocument,
    Page,
)
from app.models.drawing import DrawingItem, DrawingView, ItemIdentifier
from app.models.evidence import EvidenceArtifact, EvidenceArtifactKind, ObservationCandidate
from app.models.matching import MatchCandidate as MatchCandidateRow
from app.models.package import Package, PackageRevision
from app.models.parameters import declared_defaults, load_parameter_sets
from app.models.runs import ExtractionRun, TaskRun
from app.telemetry.tracing import traced
from app.verdicts.record import record_finding, supersede_runs
from app.verdicts.rulebook import snapshot_store
from evidence.coordinates import StoredPoint
from evidence.crop import CropSpec, CropStatus, RenderedPage, generate_crop
from evidence.polygon import Polygon
from extraction.manifest import build_manifest
from extraction.ocr import OcrEngine, RapidOcrEngine, read_page
from extraction.rasterise import PageTooLarge, render_page
from extraction.reader import UnreadablePdf, read_page_contents, read_pages
from retrieval.identifiers import NormalizedIdentifier, normalize_identifier
from retrieval.matching import MatchableItem, MatchDocumentRole, exact_match
from rules.applicability import Abstention, CheckContext, resolve
from rules.parameters import ParameterSet, resolve_all
from rules.project import ProjectScope
from rules.semantic_types import ProductType
from rules.snapshot import RuleSnapshot
from storage.store import ArtifactStore
from verdict.engine import execute
from verdict.finding import Finding
from verdict.operands import VerdictOperand
from verdict.operations import register_all
from workflow.idempotency import stage_idempotency_key
from workflow.measurements import run_parameters_for
from workflow.review import ENGINE_VERSION, PageResult

#: What produced these readings, recorded on the extraction run so a candidate can say what read it.
EXTRACTOR = "pdfplumber"
EXTRACTOR_VERSION = "extraction.reader/1"

#: One character is enough to call a page text-bearing. `build_manifest` requires the threshold from
#: its caller and gives it no default, because "enough text to be worth reading" is a judgement about
#: real drawings. One is the only value that is not a guess: it separates a page with text from a page
#: with none, which is the distinction the reader already reports.
MINIMUM_VECTOR_CHARACTERS = 1

#: The pixel ceiling for one rendered page, used only by the OCR route.
#:
#: 40 megapixels is roughly an ANSI E sheet at 150 dpi with room to spare, and 120 MB of RGB in one
#: allocation. Above it `render_page` raises `PageTooLarge` rather than shrinking, because a page
#: quietly rendered smaller is a page read at a resolution nobody chose.
MAXIMUM_RENDER_PIXELS = 40_000_000

#: How much page to keep around an evidence crop, in PDF points (72 to the inch).
#:
#: Nine points is an eighth of an inch. A crop of exactly the text box is unreadable as evidence — a
#: reviewer looking at `38 3/4"` needs to see what it is dimensioning — and a crop of the whole page
#: is not evidence of anything in particular. `CropSpec` gives this no default and requires it from
#: the caller, which is the right call: it is a judgement about drawings, and this value is the
#: smallest one that shows a dimension line either side of its text. **Expect to tune it against the
#: real GV drawings when #274 lands**; it is a starting point chosen deliberately, not a measured one.
CROP_CONTEXT_MARGIN_PT = Decimal(9)

#: How many individual refusals a stage payload carries, before it reports only the count.
#:
#: The payload is stored as JSON on the task run. A document whose pages will not render produces one
#: refusal per candidate — thousands of near-identical sentences — and a reviewer reads the first few
#: or none at all. The exact number is always reported alongside.
REPORTED_REFUSALS = 20

#: The identifier kinds that can say two drawn items are the same item.
#:
#: `catalogue` is deliberately absent. `app/models/drawing.py` states the reason on the column
#: itself: a catalogue number is shared by every unit of that model, while a mark is unique to a
#: drawing. Matching on a catalogue number would propose that every cabinet of one model is the same
#: cabinet — mass ambiguity presented as evidence, which is worse than no match at all.
MATCHABLE_IDENTIFIER_KINDS: tuple[str, ...] = ("vendor_unique", "mark")

#: `Document.kind` to the two roles arch-to-shop matching accepts.
#:
#: Schedules and product specs are absent because they are not drawings of the item: a schedule
#: tabulates, and matching a tabulated row to a drawn cabinet is a different problem with a different
#: answer. They are ingested and read like any other document; they just do not take part in this.
MATCH_ROLES: Mapping[str, MatchDocumentRole] = {
    DocumentKind.ARCHITECTURAL.value: MatchDocumentRole.ARCH,
    DocumentKind.SHOP.value: MatchDocumentRole.SHOP,
}

__all__ = ["DatabaseStages"]


class DatabaseStages:
    """The pipeline as far as it is built: checks run, everything else still says it did not.

    Deliberately not a subclass of `NoStages` and deliberately without a `__getattr__`. A catch-all
    once made `join_pages` count a phantom page, because `extract_pages` returned a mapping that a
    fall-through produced — so every unimplemented stage is written out, and adding a seventh stage to
    the protocol will fail loudly here instead of being silently answered.
    """

    def __init__(
        self,
        store: ArtifactStore | None = None,
        *,
        dpi: int = 150,
        ocr_engine: OcrEngine | None = None,
        operands: Mapping[str, Mapping[str, VerdictOperand]] | None = None,
        discriminators: Mapping[str, str] | None = None,
    ) -> None:
        """`store` is optional because nothing builds one for a worker yet.

        The artifact store lives on `app.state` in the API and is constructed by
        `scripts/dev_server.py`; no settings-driven factory exists. Rather than invent one here,
        `extract_pages` reports that it has no store and does nothing — which is a fact a caller can
        act on, where a crash on a missing dependency would look like a broken document.

        """
        self._store = store
        self._dpi = dpi
        # Injected so a test can pass a stub: building the real one loads ONNX models, and a suite
        # that loaded them to test row-writing would be paying for a model it is not testing.
        self._ocr_engine = ocr_engine
        # **Supplied operands and discriminators, keyed by rule id.**
        #
        # Nothing in the pipeline produces a verdict operand yet: a candidate has no semantic type,
        # `evidence/gate.py:seal` needs a canonical observation, and nothing mints one. So `run_checks`
        # executed every rule against `{}` and every rule abstained — correct, and not a demonstration
        # of anything.
        #
        # These let a caller supply what a reviewer would supply. `CLIENT_FACTS` Q7 blesses exactly
        # that for sink specs: *"the reviewer types the values into input fields for that drawing set"*.
        # A reviewer's own reading is HUMAN_CONFIRMED, which the evidence gate already treats as
        # qualified — so this is the sanctioned route with a human at the reading end, not a bypass.
        #
        # Empty by default, so the production path is unchanged and still abstains until evidence
        # exists. Nothing here invents a value: a caller that supplies none gets the old behaviour.
        self._operands = dict(operands or {})
        self._discriminators = dict(discriminators or {})

    def _not_built(self, stage: str) -> Mapping[str, object]:
        """The same answer `NoStages` gives, for the stages that are still not built.

        Repeated rather than delegated so the two cannot drift: a reader comparing this class with the
        protocol sees six methods and can tell at a glance which one does work.
        """
        return {"implemented": False, "stage": stage}

    def ingest(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Confirm every document this revision names is still the bytes that were uploaded.

        The API hashed each file on the way in and recorded the digest and the page count. Nothing
        has ever checked them again. Until now a document that was truncated, replaced or corrupted
        in storage would be read by `extract_pages` without a word, and the readings would look like
        readings of the drawing somebody submitted.

        **Three facts are checked, and all three are already recorded** — no new authority is
        invented here. The bytes hash to `DocumentVersion.sha256`; the file still parses as a PDF;
        it still has `DocumentVersion.page_count` pages.

        **It reports; it does not gate.** A mismatch is returned in the payload rather than raised,
        for the reason #491 gave: a corrupt artifact is not transient, so raising would roll the
        claim back and retry the same broken file for ever. What *should* happen — a revision with a
        failed digest never reaching `extract_pages` — is an entry condition on the next stage, and
        no such condition exists yet. That is a real gap and it is stated here rather than papered
        over: today this makes the failure visible, and a human acts on it.
        """
        if self._store is None:
            # The same answer `extract_pages` gives, and for the same reason: no store is a fact
            # about this worker's configuration, not about the drawings.
            return {
                "implemented": True,
                "ran": False,
                "reason": "no artifact store is configured",
                "documents": 0,
            }

        records = _document_records_for(session, package_revision_id)
        verified = 0
        mismatched: list[str] = []
        unreadable: list[str] = []
        miscounted: list[str] = []
        for version_id, key, sha256, page_count in records:
            data = _fetch(self._store, key)
            if hashlib.sha256(data).hexdigest() != sha256:
                # Recorded and not read further. Counting its pages would be describing a file that
                # is not the one under review.
                mismatched.append(str(version_id))
                continue
            try:
                pages = read_pages(data)
            except UnreadablePdf as error:
                unreadable.append(f"{version_id}: {error}")
                continue
            if len(pages) != page_count:
                miscounted.append(f"{version_id}: {len(pages)} pages, {page_count} recorded")
                continue
            verified += 1

        return {
            "implemented": True,
            "ran": True,
            "documents": len(records),
            "verified": verified,
            "digest_mismatched": mismatched,
            "unreadable": unreadable,
            "page_count_changed": miscounted,
        }

    def extract_pages(self, session: Session, package_revision_id: UUID) -> Sequence[PageResult]:
        """Read every document attached to this revision, and write down what was on its pages.

        Persists three things nothing has ever written: the page manifest, an extraction run, and the
        observation candidates themselves. All three go through the session inside the stage, because
        `run_stage` discards what a stage returns — the payload is for the record, not the work.

        **The candidates carry no semantic type**, and that is the state rather than a shortfall:
        nothing in the system assigns one, and normalisation refuses to infer one from position.
        """
        if self._store is None:
            return ()

        documents = _documents_for(session, package_revision_id)
        if not documents:
            return ()

        task_run = _task_run_for(session, package_revision_id, "extract_pages")
        if task_run is None:
            # The stage is always called through `run_stage`, which claims a task run first. Reached
            # only when something called this directly, and a candidate with no run could not say
            # what read it.
            return ()

        run = open_extraction_run(
            session,
            task_run_id=task_run.id,
            extractor=EXTRACTOR,
            extractor_version=EXTRACTOR_VERSION,
            config_hash=f"dpi={self._dpi}",
        )

        results: list[PageResult] = []
        for version, key in documents:
            # No `try` around the fetch. An artifact this stage cannot read must fail the stage, not
            # be skipped — see `_fetch`.
            data = _fetch(self._store, key)
            with traced(
                "extraction.document",
                document_version_id=str(version),
                task_run_id=str(task_run.id),
                extractor_version=EXTRACTOR_VERSION,
            ):
                results.extend(self._read_document(session, version_id=version, data=data, run=run))
        return tuple(results)

    def _read_document(
        self, session: Session, *, version_id: UUID, data: bytes, run: ExtractionRun
    ) -> list[PageResult]:
        """One document: its manifest, then its text, page by page."""
        try:
            raw_pages = read_pages(data)
        except UnreadablePdf as error:
            # A document that will not parse is not a document with no dimensions — and until #491 the
            # difference was invisible, because this returned an empty list and the package still
            # reported extraction as complete. Recorded rather than raised: a corrupt file is not
            # transient, so raising would roll back the claim and retry it for ever (#491).
            record_unreadable_document(
                session,
                extraction_run_id=run.id,
                document_version_id=version_id,
                error=error,
            )
            return []

        manifest = build_manifest(
            raw_pages, version_id, minimum_vector_characters=MINIMUM_VECTOR_CHARACTERS
        )
        pages = persist_manifest(session, manifest)

        results: list[PageResult] = []
        for page in pages:
            written = 0
            route = "vector"
            if not page.has_vector_text:
                # **A scanned page, which the vector reader cannot see at all.** Until this, such a
                # page produced no candidates and was indistinguishable from a page with nothing on
                # it. Scanned sheets are one of the six things #274 asks the client for, so this is
                # not a hypothetical (#499).
                route = "ocr"
                written = self._read_page_by_ocr(
                    session,
                    version_id=version_id,
                    data=data,
                    page=page,
                    task_run_id=run.task_run_id,
                )
            elif page.has_vector_text:
                with traced(
                    "extraction.page",
                    document_version_id=str(version_id),
                    page_index=page.index,
                    extractor_version=EXTRACTOR_VERSION,
                ) as span:
                    try:
                        contents = read_page_contents(
                            data, page.index, document_version_id=version_id, dpi=self._dpi
                        )
                    except UnreadablePdf as error:
                        # One page that will not parse, in a document whose other pages might. The
                        # count below stays 0, so without a row this would be indistinguishable from a
                        # page that was read and had nothing on it. The span says so too, but a span
                        # is ephemeral and unexported — the row is the durable half (#491).
                        contents = None
                        record_unreadable_page(
                            session,
                            extraction_run_id=run.id,
                            document_version_id=version_id,
                            page_index=page.index,
                            error=error,
                        )
                        span.set_status(Status(StatusCode.ERROR, "page did not parse"))
                    if contents is not None:
                        written = len(
                            record_candidates(
                                session,
                                contents.texts,
                                document_version_id=version_id,
                                page_id=page.id,
                                extraction_run_id=run.id,
                            )
                        )
            results.append(
                PageResult(
                    index=page.index,
                    payload={
                        "candidates": written,
                        "has_vector_text": page.has_vector_text,
                        # Which route read this page. Two readings of the same page by different
                        # routes are the basis of corroboration, so the route has to be visible.
                        "route": route,
                    },
                )
            )
        return results

    def _read_page_by_ocr(
        self,
        session: Session,
        *,
        version_id: UUID,
        data: bytes,
        page: Page,
        task_run_id: UUID,
    ) -> int:
        """Render one page and read it with the OCR engine, recording what it found.

        **A separate extraction run, not the vector one.** A candidate points at a run to say what
        read it, and `open_extraction_run` keys a run on extractor, version and config — so OCR
        readings land under their own run and a reviewer can tell a scanned reading from a vector one
        without inspecting the text.

        **The dpi is part of that run's identity**, because it is part of the reading: the same page
        at 150 and at 300 gives the engine different pixels and can give different text.

        **Refuses rather than skips when the engine is unavailable.** A missing optional dependency is
        a configuration fact, not a fact about the drawing — the same distinction `_fetch` draws for a
        storage failure. Skipping would put the pipeline back where it started, reporting a scanned
        page as read and empty.
        """
        engine = self._ocr()
        rendered = render_page(
            data,
            page.index,
            document_version_id=version_id,
            page_content_hash=page.content_hash,
            dpi=self._dpi,
            # The rasteriser's own ceiling, passed explicitly because it has no default: a page that
            # would not fit in memory must raise `PageTooLarge` rather than be silently shrunk.
            maximum_pixels=MAXIMUM_RENDER_PIXELS,
        )
        with traced(
            "extraction.page.ocr",
            document_version_id=str(version_id),
            page_index=page.index,
            extractor_version=engine.version,
        ):
            read = read_page(rendered, engine=engine)
            ocr_run = open_extraction_run(
                session,
                task_run_id=task_run_id,
                extractor=engine.name,
                extractor_version=engine.version,
                config_hash=f"dpi={self._dpi}",
            )
            return len(
                record_ocr_candidates(
                    session,
                    read.items,
                    document_version_id=version_id,
                    page_id=page.id,
                    extraction_run_id=ocr_run.id,
                )
            )

    def _ocr(self) -> OcrEngine:
        """The OCR engine, built once and only when a page actually needs it.

        Constructing `RapidOcrEngine` loads ONNX models, which is slow and pointless for a package of
        vector drawings. Injected rather than imported at the call site so a test can pass a stub and
        the suite never loads a model it is not testing.
        """
        if self._ocr_engine is None:
            self._ocr_engine = RapidOcrEngine()
        return self._ocr_engine

    def match(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Propose which architectural item is which shop item, and write the proposals down.

        `retrieval/matching.py` is finished and tested and has never had a caller: no
        `match_candidates` row has ever been written by anything but a test. This runs the real exact
        lane over the revision's own drawing items and persists what it proposes.

        **Per identifier kind, not per item.** An item can carry a vendor code and a mark at once,
        and the model keeps both deliberately because they disagree often enough that keeping one
        would lose the disagreement. Collapsing them here would re-make that choice by guess, so each
        kind is matched against its own kind and the lane is recorded on every row.

        **A proposal, and nothing more.** `match_candidates` has no approval column by design, and
        this writes nothing else — no approved match, no verdict operand. Approval is a separate
        insert that names who decided, and nothing here decides.

        **Today this finds nothing, and says so rather than appearing to work.** Writing a candidate
        needs two `drawing_items` rows, an item needs a view and a type from the `CT0xx` vocabulary,
        and nothing in the system detects a view or an item on a page — `extraction/model/` reasons
        about items it is given and does not find them. Both missing pieces are semantic: they need
        the real drawings (#274) and the vocabulary Q20 defers. So this stage is wired to the real
        matcher and returns an honest zero with the reason, which is what it should say until item
        detection exists. The moment it does, this runs unchanged.
        """
        items = _matchable_items(session, package_revision_id)
        if not items:
            return {
                "implemented": True,
                "ran": True,
                "items": 0,
                "candidates": 0,
                "reason": (
                    "no drawing items exist for this revision: nothing detects views or items on a "
                    "page yet, which needs the real drawings (#274) and the vocabulary Q20 defers"
                ),
            }

        written = 0
        proposed = 0
        for kind in MATCHABLE_IDENTIFIER_KINDS:
            architectural = [
                item
                for item, item_kind in items
                if item_kind == kind and item.document_role is MatchDocumentRole.ARCH
            ]
            shop = [
                item
                for item, item_kind in items
                if item_kind == kind and item.document_role is MatchDocumentRole.SHOP
            ]
            if not architectural or not shop:
                continue
            for result in exact_match(architectural, shop):
                for candidate in result.candidates:
                    proposed += 1
                    # The pair is unique on (left, right, lane), and a re-run proposes the same pair
                    # again. Checked rather than caught: an IntegrityError would abort the whole
                    # transaction, taking the rows that were fine with it.
                    exists = session.execute(
                        select(MatchCandidateRow.id).where(
                            MatchCandidateRow.left_item_id == candidate.left_item_id,
                            MatchCandidateRow.right_item_id == candidate.right_item_id,
                            MatchCandidateRow.lane == candidate.lane.value,
                        )
                    ).first()
                    if exists is not None:
                        continue
                    session.add(
                        MatchCandidateRow(
                            left_item_id=candidate.left_item_id,
                            right_item_id=candidate.right_item_id,
                            lane=candidate.lane.value,
                            score=candidate.score,
                        )
                    )
                    written += 1

        session.flush()
        return {
            "implemented": True,
            "ran": True,
            "items": len({item.item_id for item, _ in items}),
            "proposed": proposed,
            "candidates": written,
        }

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        """Cut the picture of the region every candidate was read from.

        `evidence/crop.py` has been finished and tested for months with **no production caller**, so
        no reviewer has ever been shown the pixels behind a reading. A number in a table that a
        person cannot check against the sheet is a number they have to take on trust, which is the
        one thing this system is not supposed to ask for.

        **What this does not do.** It does not qualify evidence, promote a candidate, or assign it a
        meaning. A crop is a picture of a region; the candidate it belongs to stays exactly as
        untyped after this stage as before it. The stage is named for the step it will eventually
        also perform — corroboration and the evidence gate — and it performs the part that is built.

        **The coordinate round trip is the delicate part, so it is exact rather than trusted.** A
        candidate's polygon is integer image pixels at the dpi the reader used. `CropSpec` wants
        stored space: the same points normalised to 0..1. Dividing by the rendered page's own pixel
        dimensions is the exact inverse of the multiplication `_crop_box` performs, so the pixels
        that come back are the pixels the reader was looking at — provided the render matches the
        read. Rendering at `self._dpi`, the dpi the candidates were read at, is what makes that true,
        and `_stored_polygon` refuses rather than guesses when a point falls outside the page.
        """
        if self._store is None:
            return {
                "implemented": True,
                "ran": False,
                "reason": "no artifact store is configured",
                "crops": 0,
            }

        keys = dict(_documents_for(session, package_revision_id))
        if not keys:
            return {"implemented": True, "ran": True, "candidates": 0, "crops": 0}

        rows = session.execute(
            select(ObservationCandidate, Page)
            .join(Page, Page.id == ObservationCandidate.page_id)
            .where(ObservationCandidate.document_version_id.in_(list(keys)))
            .order_by(Page.index, ObservationCandidate.created_at)
        ).all()
        if not rows:
            return {"implemented": True, "ran": True, "candidates": 0, "crops": 0}

        # **Which candidates already have a crop.** `evidence_artifacts` is append-only and unique on
        # (storage_key, sha256), and a crop's key is content-addressed — so re-running this stage on
        # the same page would regenerate byte-identical crops and collide. A redelivery is not a
        # second reading, exactly as `record_candidates` says of its own rows.
        existing = session.execute(
            select(
                EvidenceArtifact.candidate_id,
                EvidenceArtifact.storage_key,
                EvidenceArtifact.sha256,
            ).where(EvidenceArtifact.document_version_id.in_(list(keys)))
        ).all()
        already = {candidate_id for candidate_id, _, _ in existing}

        by_page: dict[UUID, list[ObservationCandidate]] = {}
        pages: dict[UUID, Page] = {}
        for candidate, page in rows:
            by_page.setdefault(page.id, []).append(candidate)
            pages[page.id] = page

        written = 0
        abstained: list[str] = []
        skipped = 0
        # **Seeded from the database, not empty.** Two candidates whose crops are byte-identical
        # share one content-addressed key, so only the first gets a row and the second is skipped
        # without one. On the next pass that second candidate is not in `already` — it has no
        # artifact — and regenerating its crop collides with the row its twin wrote. An in-memory set
        # cannot see that, because the collision is with work from a previous call.
        seen: set[tuple[str, str]] = {(key, digest) for _, key, digest in existing}
        documents: dict[UUID, bytes] = {}
        for page_id, candidates in by_page.items():
            page = pages[page_id]
            key = keys.get(page.document_version_id)
            if key is None:
                continue
            if page.render_failed:
                # The manifest already recorded that this page would not render. Asking the
                # rasteriser again would produce the same failure more slowly.
                abstained.append(f"page {page.index}: the manifest recorded a failed render")
                skipped += len(candidates)
                continue
            if page.document_version_id not in documents:
                documents[page.document_version_id] = _fetch(self._store, key)
            try:
                rendered = render_page(
                    documents[page.document_version_id],
                    page.index,
                    document_version_id=page.document_version_id,
                    page_content_hash=page.content_hash,
                    dpi=self._dpi,
                    maximum_pixels=MAXIMUM_RENDER_PIXELS,
                )
            except (PageTooLarge, UnreadablePdf, ValueError) as error:
                # One page that will not render, in a document whose others might. Every candidate on
                # it keeps its reading and goes without a picture, which is the honest pair.
                abstained.append(f"page {page.index}: {error}")
                skipped += len(candidates)
                continue

            for candidate in candidates:
                if candidate.id in already:
                    skipped += 1
                    continue
                polygon = _stored_polygon(candidate, rendered)
                if polygon is None:
                    abstained.append(
                        f"page {page.index}: a candidate's polygon does not describe a region of "
                        "this rendering"
                    )
                    continue
                result = generate_crop(
                    rendered,
                    CropSpec(
                        polygon=polygon, context_margin_pt=CROP_CONTEXT_MARGIN_PT, dpi=self._dpi
                    ),
                    self._store,
                )
                if result.status is not CropStatus.AVAILABLE or result.artifact is None:
                    # `generate_crop` abstains rather than raising, and the reason is a sentence. It
                    # is carried through rather than counted, because "17 crops failed" tells a
                    # reviewer nothing they can act on.
                    abstained.append(f"page {page.index}: {result.reason}")
                    continue
                artifact = result.artifact
                identity = (artifact.key, artifact.sha256)
                if identity in seen:
                    # Two candidates whose crops are byte-identical — the same region read twice, or
                    # two identical labels in the same place. The image is stored once and belongs to
                    # whichever candidate reached it first; this one gets no artifact row, because
                    # the unique constraint on (storage_key, sha256) permits only one. That is a
                    # real limit rather than a tidy outcome: a reviewer following the second
                    # candidate finds no picture. Fixing it means letting two rows share one stored
                    # object, which is a schema decision and not this change's to make.
                    skipped += 1
                    continue
                seen.add(identity)
                session.add(
                    EvidenceArtifact(
                        candidate_id=candidate.id,
                        canonical_observation_id=None,
                        document_version_id=page.document_version_id,
                        page_id=page.id,
                        kind=EvidenceArtifactKind.CROP.value,
                        storage_key=artifact.key,
                        sha256=artifact.sha256,
                        media_type="image/png",
                        # The crop is pixels of a rendered page, so the space it is expressed in is
                        # the image's, not the normalised one the polygon was converted to.
                        coordinate_space="image",
                    )
                )
                written += 1

        session.flush()
        return {
            "implemented": True,
            "ran": True,
            "candidates": len(rows),
            "crops": written,
            "already_had_one": skipped,
            "refused": len(abstained),
            # Capped, because this payload is persisted as JSON and a document that fails to render
            # would otherwise put one sentence per candidate into it. The count above is exact; these
            # are the examples a person reads first.
            "refusals": abstained[:REPORTED_REFUSALS],
        }

    def generate_outputs(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("generate_outputs")

    def run_checks(self, session: Session, package_revision_id: UUID) -> Mapping[str, object]:
        """Run every applicable rule against this revision and record what each decided.

        **`register_all()` first, every time.** The operation registry is global and empty until
        somebody fills it; today the only thing that does is importing `app/api/operations.py`, which
        a worker process never touches. Without this call every operation lookup fails and the engine
        converts the failure into `REVIEW_REQUIRED` — so the whole package would abstain, plausibly,
        for a reason that appears nowhere. It is idempotent.

        **Previous runs are superseded before new ones are written**, inside this transaction, so no
        reader ever sees two sets of findings for one revision.
        """
        register_all()

        revision = session.get(PackageRevision, package_revision_id)
        if revision is None:
            return {"implemented": True, "ran": False, "reason": "no such package revision"}

        package = session.get(Package, revision.package_id)
        if package is None:
            return {"implemented": True, "ran": False, "reason": "no such package"}

        store = snapshot_store(session)
        if not store.rule_ids():
            # Not a failure. Nothing is published, so there is nothing to check — and saying so is
            # different from running zero rules and reporting success.
            return {
                "implemented": True,
                "ran": False,
                "reason": "no rules are published; nothing to check",
            }

        # **The RUN layer, which nothing else loads.** `load_parameter_sets` covers GLOBAL and
        # PROJECT and says why — "RUN sets are supplied per review and are not loaded here" — so a
        # run-scope parameter reached no resolver at all. `sink_interior_depth` and
        # `sink_interior_width` come off the sink cut sheet for one review, and without this both
        # sink-cutout checks abstained however carefully a reviewer typed them, for a reason nothing
        # on the findings list could explain.
        #
        # Loaded here rather than injected, because unlike operands these values *are* in the
        # database: reading them is the same act as reading the project's own settings.
        stored_layers = [
            *load_parameter_sets(session, package.project_id),
            *(
                run
                for run in (run_parameters_for(session, package_revision_id),)
                if run is not None
            ),
        ]
        rules = [store.latest(rule_id) for rule_id in store.rule_ids()]
        defaults = declared_defaults(
            [snapshot.rule for snapshot in rules if snapshot is not None], when=utc_now()
        )
        layers = _layered(defaults, stored_layers)
        resolved = resolve_all(*layers)

        # `ProjectScope` wants the pinned project layer. Where a project has set nothing, the
        # rulebook's own defaults stand in — they are a real published answer, not a fabricated one.
        project_layer = next(
            (layer for layer in layers if layer.project_id == str(package.project_id)),
            None,
        )
        scope = ProjectScope(
            project_id=str(package.project_id),
            parameter_set=project_layer if project_layer is not None else _empty_project(package),
        )

        # **Every product type, not one.** The resolver keys candidates on an exact product-type
        # match, so asking about countertops alone would leave the cabinet rules unrun — and unrun is
        # indistinguishable from passing once the reviewer is looking at the list. A package carries
        # no product type today (there is no column for it), and guessing one from the vendor or the
        # filename would decide which checks apply by inference. Running the whole rulebook is the
        # honest reading until a package can say what is in it: a cabinet rule against a countertop
        # package abstains, which is visible, where omitting it is not.
        #
        # No discriminator can be established without extraction, so a rule that declares one
        # abstains rather than being resolved to a variant nobody read off a drawing.
        superseded = supersede_runs(session, package_revision_id)

        written = 0
        skipped = 0
        for product_type in ProductType:
            resolution = resolve(
                store,
                CheckContext(
                    product_type=product_type,
                    project=scope,
                    # A reviewer stating the wall layout is the same manual input as a reviewer
                    # typing a dimension. Empty unless supplied, so a rule with a discriminator
                    # still abstains rather than being resolved to a variant nobody established.
                    discriminators=self._discriminators,
                ),
            )
            # **A rule that could not even be attempted becomes a finding too.**
            # The resolver abstains when it cannot establish which variant applies — today that is
            # every rule with a discriminator, because nothing reads `wall_config` off a drawing. If
            # those were only counted, the reviewer would see the checks that ran and have no way to
            # learn that two more never started. Unrun and passed are indistinguishable on a list,
            # which is the failure this whole system is built to prevent, so they are recorded with
            # the resolver's own reason.
            for abstention in resolution.abstentions:
                if abstention.rule_id is None:
                    # Nothing to attribute it to, so nothing to write. Counted instead, and returned,
                    # rather than attached to an arbitrary rule.
                    skipped += 1
                    continue
                snapshot = store.latest(abstention.rule_id)
                if snapshot is None:
                    skipped += 1
                    continue
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=_unresolved(snapshot, abstention),
                    operands={},
                    parameter_set_ids={layer.layer.value: layer.set_id for layer in layers},
                )
                written += 1

            for applicable in resolution.applicable:
                supplied = self._operands.get(applicable.snapshot.rule.id, {})
                finding = execute(
                    applicable.snapshot,
                    supplied,
                    resolved,
                    discriminators=self._discriminators,
                )
                record_finding(
                    session,
                    package_revision_id=package_revision_id,
                    finding=finding,
                    operands=supplied,
                    parameter_set_ids={layer.layer.value: layer.set_id for layer in layers},
                    missing=_declared_inputs(applicable.snapshot.rule),
                )
                written += 1

        return {
            "implemented": True,
            "ran": True,
            "findings": written,
            "rules_published": len(store.rule_ids()),
            "superseded_runs": superseded,
            "not_applicable": skipped,
        }


def _unresolved(snapshot: RuleSnapshot, abstention: Abstention) -> Finding:
    """A rule the resolver could not even attempt, as the finding a reviewer will read.

    Built here rather than by the engine because the engine was never reached: applicability is
    decided before any arithmetic, and a rule whose variant is unknown has no operands to trace. The
    severity comes from the rule itself, so an unattempted critical check is still critical.
    """
    return Finding(
        rule_id=snapshot.rule.id,
        outcome=abstention.outcome,
        severity=snapshot.rule.severity,
        reason=abstention.reason,
        snapshot_id=snapshot.snapshot_id,
        engine_version=ENGINE_VERSION,
    )


def _layered(defaults: ParameterSet, stored: Sequence[ParameterSet]) -> tuple[ParameterSet, ...]:
    """The rulebook defaults beneath whatever the database supplies.

    `rules.parameters.resolve` refuses two sets in one layer, and the defaults are GLOBAL — so a
    stored global set replaces them wholesale rather than merging. That is the correct reading: a
    company standard that has been recorded is the standard, and a rule author's default is what
    applies until somebody records one.
    """
    stored_layers = {layer.layer for layer in stored}
    if defaults.layer in stored_layers:
        return tuple(stored)
    return (defaults, *stored)


def _empty_project(package: Package) -> ParameterSet:
    """A project layer with nothing in it, for a project that has configured nothing.

    `ProjectScope` requires a pinned set and cannot take `None`. An empty one is truthful — this
    project has set no overrides — and resolution then falls through to the layers beneath it.
    """
    from rules.parameters import ParameterLayer

    return ParameterSet(
        project_id=str(package.project_id),
        layer=ParameterLayer.PROJECT,
        version=1,
        parameters={},
    )


def _declared_inputs(rule: object) -> dict[str, str]:
    """The operands a rule needs, so an abstention can name what was not read.

    Reported from the rule rather than from the empty operand mapping, because "nothing was supplied"
    is not useful and "no dimension was read for cutout_width (SHOP)" sends somebody to the drawing.
    """
    inputs = getattr(rule, "inputs", {})
    return {name: getattr(selector, "source", "?") for name, selector in inputs.items()}


def _documents_for(session: Session, package_revision_id: UUID) -> list[tuple[UUID, str]]:
    """Every document version attached to this revision, with the key its bytes live under.

    Read through `package_revision_documents` rather than from `documents`, because a document
    belongs to a *package* while a revision is composed of specific *versions* — going the short way
    would read whatever version is newest rather than the one this revision was built from, and a
    review would then be of drawings nobody submitted.

    The storage key is derived rather than stored: `app/api/documents.py` builds it from the document
    id and the content hash, and it is recomputed the same way here. Deriving it in two places is
    worth naming as a smell — change that scheme and this breaks — but the alternative is a column
    that does not exist, and adding one is a migration this change should not carry.
    """
    rows = session.execute(
        select(
            PackageRevisionDocument.document_version_id,
            PackageRevisionDocument.document_id,
            DocumentVersion.sha256,
        )
        .join(
            DocumentVersion,
            DocumentVersion.id == PackageRevisionDocument.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(DocumentVersion.created_at)
    ).all()
    return [(version_id, storage_key(document_id, sha)) for version_id, document_id, sha in rows]


def _document_records_for(
    session: Session, package_revision_id: UUID
) -> list[tuple[UUID, str, str, int]]:
    """Every document version in this revision, with its key, recorded digest and page count.

    Separate from `_documents_for` rather than widening it: that function's callers want somewhere to
    read bytes from, and this one wants the facts to check those bytes against. One function
    returning a four-tuple to callers that use two of it is how a helper starts drifting.
    """
    rows = session.execute(
        select(
            PackageRevisionDocument.document_version_id,
            PackageRevisionDocument.document_id,
            DocumentVersion.sha256,
            DocumentVersion.page_count,
        )
        .join(
            DocumentVersion,
            DocumentVersion.id == PackageRevisionDocument.document_version_id,
        )
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(DocumentVersion.created_at)
    ).all()
    return [
        (version_id, storage_key(document_id, sha), sha, page_count)
        for version_id, document_id, sha, page_count in rows
    ]


def _stored_polygon(candidate: ObservationCandidate, rendered: RenderedPage) -> Polygon | None:
    """A candidate's image-pixel polygon as the normalised one a `CropSpec` takes.

    Returns `None` rather than raising, and rather than clamping. A point outside the rendering means
    the pixels in front of us are not the pixels the reader measured against — a different dpi, a
    different page, a re-rendered document. Clamping would produce a crop of a real region that is
    not the region the reading came from, which is worse than no crop: it is a picture that argues
    for the wrong number. `validate_evidence` counts the refusal and says which page it was on.
    """
    width = Decimal(rendered.width_px)
    height = Decimal(rendered.height_px)
    try:
        points = tuple(
            StoredPoint(x=Decimal(int(x)) / width, y=Decimal(int(y)) / height)
            for x, y in candidate.polygon
        )
        return Polygon(
            points=points,
            space="stored",
            document_version_id=candidate.document_version_id,
            page=rendered.page_index,
        )
    except (ArithmeticError, TypeError, ValueError):
        # `Polygon` refuses a degenerate, self-intersecting or out-of-page shape, and a text run with
        # zero width is degenerate. Every one of those is a reason not to cut a crop.
        return None


def _projection(
    item: DrawingItem,
    role: MatchDocumentRole,
    identifier: NormalizedIdentifier | None,
    project_id: UUID,
    package_revision_id: UUID,
) -> MatchableItem:
    """One drawing item as the matcher's own projection of it."""
    return MatchableItem(
        item_id=item.id,
        identifier=identifier,
        project_id=project_id,
        package_revision_id=package_revision_id,
        # The item's own type, which is the scope the matcher compares within: a countertop is not a
        # candidate match for a cabinet however alike their marks.
        category=item.item_type,
        document_role=role,
    )


def _matchable_items(
    session: Session, package_revision_id: UUID
) -> list[tuple[MatchableItem, str]]:
    """This revision's drawing items as the matcher's own projection, paired with identifier kind.

    One entry per (item, identifier): an item carrying both a vendor code and a mark takes part in
    both lanes, and each is matched only against its own kind. An item with no identifier at all is
    included once with `identifier=None`, because the matcher's answer for it — unmatched, for a
    stated reason — is a result a reviewer needs, not an absence to hide.

    The role comes from `Document.kind`, which is the only place a drawing says whether it is the
    architect's or the shop's. Schedules and product specs are filtered out by `MATCH_ROLES`.
    """
    project_id = session.execute(
        select(Package.project_id)
        .join(PackageRevision, PackageRevision.package_id == Package.id)
        .where(PackageRevision.id == package_revision_id)
    ).scalar_one_or_none()
    if project_id is None:
        return []

    rows = session.execute(
        select(DrawingItem, Document.kind, ItemIdentifier)
        .join(DrawingView, DrawingView.id == DrawingItem.drawing_view_id)
        .join(Page, Page.id == DrawingView.page_id)
        .join(DocumentVersion, DocumentVersion.id == Page.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(
            PackageRevisionDocument,
            PackageRevisionDocument.document_version_id == DocumentVersion.id,
        )
        .outerjoin(ItemIdentifier, ItemIdentifier.drawing_item_id == DrawingItem.id)
        .where(PackageRevisionDocument.package_revision_id == package_revision_id)
        .order_by(DrawingItem.created_at)
    ).all()

    # Grouped by item first, because the decision below is per item and not per row. An item with
    # two identifiers arrives as two rows, and an item whose only identifier is a catalogue number
    # must still appear — as unmatchable, which is a result — rather than vanish because its one row
    # was filtered out. Filtering row by row made exactly that mistake.
    grouped: dict[UUID, tuple[DrawingItem, str, list[ItemIdentifier]]] = {}
    for item, kind, identifier in rows:
        entry = grouped.setdefault(item.id, (item, kind, []))
        if identifier is not None:
            entry[2].append(identifier)

    items: list[tuple[MatchableItem, str]] = []
    for item, kind, identifiers in grouped.values():
        role = MATCH_ROLES.get(kind)
        if role is None:
            continue
        usable = [
            identifier
            for identifier in identifiers
            if identifier.kind in MATCHABLE_IDENTIFIER_KINDS
        ]

        if not usable:
            # No identifier this lane can use — either none at all, or only a catalogue number,
            # which names a model rather than a unit. The matcher's answer is unmatched with a
            # reason, and a reviewer needs to see that far more than they need the row hidden.
            items.append((_projection(item, role, None, project_id, package_revision_id), ""))
            continue
        for identifier in usable:
            items.append(
                (
                    _projection(
                        item,
                        role,
                        normalize_identifier(identifier.value_as_printed),
                        project_id,
                        package_revision_id,
                    ),
                    identifier.kind,
                )
            )
    return items


def _task_run_for(session: Session, package_revision_id: UUID, stage: str) -> TaskRun | None:
    """The task run `run_stage` claimed for this stage.

    Looked up by recomputing the key rather than taken as an argument, because `run_stage` holds the
    `Claim` and does not hand it to the stage body. Widening the `Stages` protocol for one caller
    would change a seam five other implementations already satisfy; recomputing a deterministic key
    does not.
    """
    key = stage_idempotency_key(
        package_revision_id=package_revision_id, stage=stage, engine_version=ENGINE_VERSION
    )
    return session.execute(
        select(TaskRun).where(TaskRun.idempotency_key == key)
    ).scalar_one_or_none()


def _fetch(store: ArtifactStore, key: str) -> bytes:
    """The stored bytes for one document. Raises when they cannot be read.

    **This used to catch `Exception` and return `None`, and that was the bug** (found in review on
    #484, fixed in #487). The caller skipped a document that returned nothing, so a missing artifact,
    an object whose digest no longer matches, and a document that simply is not attached to the
    revision all became the same silent outcome: a revision that reported extraction as done, with
    one of its drawings never read. A package that looks checked while a drawing in it was never
    opened is the package-level shape of a false PASS.

    The old docstring even said the store "verifies its own digest on the way out, so a corrupt
    object arrives here as an exception rather than as wrong bytes" — and then discarded that
    exception. The sentence was true and the code threw the value away.

    So it raises, and the raise is the right mechanism rather than a nuisance: a storage failure is
    infrastructure, not a fact about the drawing, and `run_stage` rolls the stage back for
    re-delivery, which is exactly what should happen to a fetch that may well succeed next time.
    """
    with store.get(key) as stored:
        return stored.read()
