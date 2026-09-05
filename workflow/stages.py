"""The first real pipeline stage: running the rules and writing what they decided.

Until now the only implementation of `Stages` was `NoStages`, which answers `{"implemented": False}`
for every stage and is deliberately loud about it — a default returning `{}` would let a package walk
the whole pipeline and arrive at review looking processed. This replaces one of those six answers with
work, and leaves the other five saying exactly what they said before.

**Why `run_checks` first, when extraction is not built.** The engine has been finished and tested for
some time and has never had a caller: no code in the repository writes a `CheckRun` or a `Finding`, so
every finding anyone has seen was inserted by hand. Wiring the caller proves the spine — applicable
rules selected, parameters resolved, `execute()` run, rows written, findings visible to a reviewer —
and makes extraction a matter of supplying operands to something that already works, rather than
another layer with nothing downstream of it.

**Every finding will be `NOT_FOUND`, and that is the honest result.** There are no observations, so
there are no operands, so no check can decide anything. The alternative — waiting until extraction
exists — leaves the engine uncalled and the `CHECKS_HAVE_RUN` entry condition unsatisfiable, so no
package can legally reach `AWAITING_REVIEW` at all.

**Why here and not in `app/`.** The `Stages` protocol is handed a SQLAlchemy `Session`, and
`sqlalchemy` is in the banned set for `verdict/` and `rules/` — those packages could not implement
this protocol if they wanted to. `workflow/` is the sanctioned bridge: it already imports `app` models
and the domain layers side by side (`workflow/retry.py`), which is exactly what a stage has to do.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from app.models.document import DocumentVersion, PackageRevisionDocument, Page
from app.models.package import Package, PackageRevision
from app.models.parameters import declared_defaults, load_parameter_sets
from app.models.runs import ExtractionRun, TaskRun
from app.telemetry.tracing import traced
from app.verdicts.record import record_finding, supersede_runs
from app.verdicts.rulebook import snapshot_store
from extraction.manifest import build_manifest
from extraction.ocr import OcrEngine, RapidOcrEngine, read_page
from extraction.rasterise import render_page
from extraction.reader import UnreadablePdf, read_page_contents, read_pages
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
        del session, package_revision_id
        return self._not_built("ingest")

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
        del session, package_revision_id
        return self._not_built("match")

    def validate_evidence(
        self, session: Session, package_revision_id: UUID
    ) -> Mapping[str, object]:
        del session, package_revision_id
        return self._not_built("validate_evidence")

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
