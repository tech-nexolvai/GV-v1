"""Sutradhar: the one place that builds the stages and runs the work an outbox row names (#1227).

Two runners used to build the pipeline each in their own way. The local outbox consumer
(`scripts/drain_outbox.py`, the one every kit and demo starts) built `DatabaseStages` with every
route and setting the deployment states, ran extraction stage by stage with a commit at each boundary,
carried a person's answers forward, filled the form and handed the revision to the reviewer. The
Hatchet worker (`workflow/entrypoints.py:run_worker`, set up but not run) had fallen 12 arguments
behind: it passed the store and 6 of those 19 (the one other it left out, `operands=None`, is the
default). A setting added to one and forgotten in the other is exactly how they drifted.

So both now build the stages here, with `build_stages`, and the local consumer runs its work here,
with `run_extraction` and `consume`. **Everything below was moved word for word** from
`scripts/drain_outbox.py`: the same arguments in the same order, the same commit points and
transactions, the same post-extraction steps, the same states, the same error messages and the same
failure actor ("local review worker"). `scripts/drain_outbox.py` is now a thin shell that keeps its
old private names, so the tests and launchers that call or patch them keep working.

**Every heavy import is inside the function that needs it.** That keeps this module free of
extraction until it is asked to build or run something, and it means a launcher that replaces a
reader's factory in its own process (for example `workflow.slot_reader.configured_slot_reader`)
before the stages are built is what the stages get.

**Only one consumer may drain an outbox.** The local consumer (`make worker`) and the Hatchet
dispatcher (`make dispatch`) both take rows with `FOR UPDATE SKIP LOCKED`; started together on one
database they compete for the same rows, and a row the Hatchet side takes is one the local consumer
never runs. Hatchet's six-stage `register` (`workflow/review.py`) is left as it is (frozen): it cannot
hand a revision to a person and take it back, which the local flow does.

Verification: `tests/workflow/test_sutradhar.py`, `tests/scripts/test_drain_outbox*.py`,
`tests/workflow/test_reading_progress.py`, `tests/workflow/test_entrypoints.py`
"""

from __future__ import annotations

import os
import pathlib
from collections.abc import Callable, Mapping
from contextlib import nullcontext
from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover - annotations only, keeps this module light to import
    from app.config import Settings
    from app.evidence.automatic_typing import AutomaticTypingSettings
    from storage.store import ArtifactStore
    from workflow.timing import TimingRecorder

__all__ = [
    "ASSOCIATION_VARIABLES",
    "PART_FINDER_VARIABLE",
    "PART_PICTURE_DPI_VARIABLE",
    "PART_PICTURE_MARGIN_VARIABLE",
    "PHRASE_GAP_VARIABLE",
    "StagesFactory",
    "automatic_typing_configuration",
    "bala_auto_items_configuration",
    "bala_cabinet_prefill_configuration",
    "build_package_text",
    "build_stages",
    "cabinet_ai_reading_configuration",
    "carry_forward",
    "consume",
    "cut_part_pictures",
    "depth_reader_configuration",
    "drop_closed_revision",
    "ensure_workflow_run",
    "hunt_package_values",
    "part_finder_configuration",
    "part_picture_configuration",
    "phrase_grouping",
    "propose_measurements",
    "reader_configuration",
    "render_vendor_page_pictures",
    "resume_from_reviewer_input",
    "run_checks",
    "run_extraction",
]

#: Builds the stages for one piece of work. `build_stages` itself, or a caller's stand-in for it;
#: called with the keyword arguments `build_stages` takes.
StagesFactory = Callable[..., object]


#: Code's geometry-only part finder (#1236, decision 3 of 2026-10-14): on, it runs in memory in the
#: match stage as Bala's second judgment for each piece, with the reader's line and detector settings
#: (`ASSOCIATION_VARIABLES`) and without turning on the localized reader.
PART_FINDER_VARIABLE = "GV_PART_FINDER_ENABLED"

#: Code's countertop items on two judgments (#1244, the ADR-0021 amendment of 2026-10-14): on, the
#: match stage confirms a countertop suggestion with no click where both AI row readers and code's
#: placement agree. Off by default; a person can always undo, and a person always wins.
BALA_AUTO_ITEMS_VARIABLE = "GV_BALA_AUTO_ITEMS"
#: Code pre-fills "cabinet" on a piece with one printed cabinet tag (#1266). Off by default.
BALA_CABINET_PREFILL_VARIABLE = "GV_BALA_CABINET_PREFILL"
#: The five depth and sink checks per countertop row on two judgments (#1390). Off by default.
DEPTH_PER_ROW_VARIABLE = "GV_DEPTH_PER_ROW"
#: A cabinet package read by the same AI readers as a countertop package (cabinets e2e), so the
#: cabinet run can be proposed from its widths. Off by default: reading costs money (#1205).
CABINET_AI_READING_VARIABLE = "GV_CABINET_AI_READING"

#: The settings the line-work reader and the dimension-line detector take (`AssociationSettings`).
#: Required for the same reason as the rest: each decides which strokes a reading may attach to,
#: and a default would be this machine's guess shipped as every deployment's.
ASSOCIATION_VARIABLES: tuple[str, ...] = (
    "GV_READER_LINE_MINIMUM_PT",
    "GV_READER_GLYPH_MAXIMUM_PT",
    "GV_READER_GLYPH_GAP_PT",
    "GV_READER_PROXIMITY_LIMIT",
    "GV_READER_AMBIGUITY_MARGIN",
    # The dimension-line detector (#179).
    "GV_READER_WITNESS_TOLERANCE",
    "GV_READER_MINIMUM_SPAN",
    "GV_READER_STRAIGHTNESS",
    "GV_READER_CROSSING_MARGIN",
    # The stacked-fraction detector (#735). Required with the rest because the rule it enforces —
    # a stacked fraction always goes to a reviewer (#726) — cannot run without it, and the guard
    # it replaced was optional and was never once supplied.
    "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT",
    "GV_READER_FRACTION_BAR_LENGTH_MIN_PT",
    "GV_READER_FRACTION_REACH_PT",
    "GV_READER_FRACTION_GLYPH_MIN_PT",
    "GV_READER_FRACTION_GLYPH_MAX_PT",
    "GV_READER_FRACTION_PROPORTION_MAX",
    # Where each part of a stacked label was drawn (#834), which a reading of it must match.
    "GV_READER_FRACTION_CHARACTER_GAP_PT",
    # Which bars across a stamp's baseline are a turned label's fraction (#869).
    "GV_READER_FRACTION_TURNED_ASPECT_MIN",
)

#: The localized reader's own three, on top of `ASSOCIATION_VARIABLES`.
LOCALIZED_VARIABLES: tuple[str, ...] = (
    "GV_READER_LOCALIZED_MINIMUM_PATHS",
    "GV_READER_LOCALIZED_MAXIMUM_SPAN",
    "GV_READER_LOCALIZED_CROP_MARGIN_PT",
)


def _switched_on(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def _stated(names: tuple[str, ...], route: str) -> dict[str, str]:
    """Every named setting, or an error naming the missing ones: a partial configuration is refused,
    because silently filling one value would change which drawing region gets read."""
    required = {name: os.environ.get(name) or "" for name in names}
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError(f"{route} is enabled but missing " + ", ".join(missing))
    return required


def _association(required: Mapping[str, str]) -> object:
    from extraction.glyph_bands import FractionBarGeometry
    from workflow.association import AssociationSettings

    return AssociationSettings(
        line_minimum_pt=Decimal(required["GV_READER_LINE_MINIMUM_PT"]),
        glyph_maximum_pt=Decimal(required["GV_READER_GLYPH_MAXIMUM_PT"]),
        glyph_gap_pt=Decimal(required["GV_READER_GLYPH_GAP_PT"]),
        proximity_limit=Decimal(required["GV_READER_PROXIMITY_LIMIT"]),
        ambiguity_margin=Decimal(required["GV_READER_AMBIGUITY_MARGIN"]),
        witness_tolerance=Decimal(required["GV_READER_WITNESS_TOLERANCE"]),
        minimum_span=Decimal(required["GV_READER_MINIMUM_SPAN"]),
        straightness=Decimal(required["GV_READER_STRAIGHTNESS"]),
        crossing_margin=Decimal(required["GV_READER_CROSSING_MARGIN"]),
        fraction_bar=FractionBarGeometry(
            bar_thickness_max_pt=Decimal(required["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"]),
            bar_length_min_pt=Decimal(required["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"]),
            reach_pt=Decimal(required["GV_READER_FRACTION_REACH_PT"]),
            glyph_min_pt=Decimal(required["GV_READER_FRACTION_GLYPH_MIN_PT"]),
            glyph_max_pt=Decimal(required["GV_READER_FRACTION_GLYPH_MAX_PT"]),
            proportion_max=Decimal(required["GV_READER_FRACTION_PROPORTION_MAX"]),
            character_gap_pt=Decimal(required["GV_READER_FRACTION_CHARACTER_GAP_PT"]),
            turned_aspect_min=Decimal(required["GV_READER_FRACTION_TURNED_ASPECT_MIN"]),
        ),
    )


def reader_configuration() -> tuple[object | None, object | None]:
    """Read explicitly supplied localized-reader settings, or leave that optional route off.

    The thresholds select pixels to OCR, so they are deployment configuration rather than product
    defaults.  A partial configuration is refused; silently filling one value would change which
    drawing region gets read.  The demo launcher supplies its documented synthetic-fixture values.
    """
    if not _switched_on("GV_LOCALIZED_OCR_ENABLED"):
        return None, None
    # In the order the error has always named them: the detector's four, the localized three, then
    # the fraction detector's.
    required = _stated(
        ASSOCIATION_VARIABLES[:9] + LOCALIZED_VARIABLES + ASSOCIATION_VARIABLES[9:], "localized OCR"
    )
    from workflow.association import LocalizedOcrSettings

    return (
        _association(required),
        LocalizedOcrSettings(
            minimum_paths=int(required["GV_READER_LOCALIZED_MINIMUM_PATHS"]),
            maximum_span=Decimal(required["GV_READER_LOCALIZED_MAXIMUM_SPAN"]),
            crop_margin_pt=Decimal(required["GV_READER_LOCALIZED_CROP_MARGIN_PT"]),
        ),
    )


def part_finder_configuration() -> object | None:
    """Code's part finder's settings when `GV_PART_FINDER_ENABLED` is on, else `None` (#1236).

    The same line-work and detector settings as the localized reader's (`ASSOCIATION_VARIABLES`),
    all required, and nothing else: turning the finder on reads no page's pixels and changes no
    reading. Where the localized reader is on, the stages use its settings instead.
    """
    if not _switched_on(PART_FINDER_VARIABLE):
        return None
    return _association(_stated(ASSOCIATION_VARIABLES, "code's part finder"))


def bala_auto_items_configuration() -> bool:
    """Whether code may confirm a countertop on two judgments (`GV_BALA_AUTO_ITEMS`, #1244).

    Off unless switched on. It needs no settings of its own: both judgments read what the slot
    reader and the drawings' confirmations already recorded, and nothing here asks a model.
    """
    return _switched_on(BALA_AUTO_ITEMS_VARIABLE)


def depth_reader_configuration() -> bool:
    """Whether the depth and sink reader runs after Bala (`GV_DEPTH_READER`, #1290).

    Off unless switched on; an unrecognised word is refused, never read as on. On, it reads with the
    slot reader's Claude pair, so without `GV_CLAUDE_READER_ENABLED` the match stage reads nothing
    and says why.
    """
    from extraction.depth.read import depth_reader_enabled

    return depth_reader_enabled(os.environ)


def job_values_reader_configuration() -> bool:
    """Whether the job values reader runs after the depth reader (`GV_JOB_VALUES_READER`, #1409).

    Off unless switched on; an unrecognised word is refused, never read as on. On, it reads with
    the slot reader's Claude pair, so without `GV_CLAUDE_READER_ENABLED` it reads nothing and says
    why.
    """
    from extraction.job_values.read import job_values_reader_enabled

    return job_values_reader_enabled(os.environ)


def depth_per_row_configuration() -> bool:
    """Whether the five depth and sink checks run per countertop row on two judgments
    (`GV_DEPTH_PER_ROW`, #1390).

    Off unless switched on. It needs no settings of its own and asks no model: both judgments read
    what the slot reader, Bala and the depth reader already recorded. It acts only with the depth
    and sink reader on (`GV_DEPTH_READER`).
    """
    return _switched_on(DEPTH_PER_ROW_VARIABLE)


def cabinet_ai_reading_configuration() -> bool:
    """Whether a cabinet package is read by the AI readers like a countertop package
    (`GV_CABINET_AI_READING`, cabinets e2e).

    Off unless switched on. Off, a cabinet package is not read (#1205, `AI_READING_SKIPPED`). On,
    the same form and slot readers read it, with the same budget and seals, so its rows' widths can
    be proposed as the cabinet run for a person to confirm (`app/review/cabinet_run_proposal.py`).
    It changes no question the readers ask; it only stops skipping the package. It is a paid
    reading (about what a countertop package of the same pages costs), so switching it on is
    Anant's call.
    """
    return _switched_on(CABINET_AI_READING_VARIABLE)


def bala_cabinet_prefill_configuration() -> bool:
    """Whether code pre-fills "cabinet" on a piece with one printed cabinet tag
    (`GV_BALA_CABINET_PREFILL`, #1266).

    Off unless switched on, and to stay off until the piece-kind scorecard shows 0 wrong (Anant's
    decision 5 of 2026-10-14). A pre-fill is never a decision: a person still clicks. It reads the
    tags with code's part finder, so without `GV_PART_FINDER_ENABLED` (or the reader's settings) it
    pre-fills nothing, and each piece says why.
    """
    return _switched_on(BALA_CABINET_PREFILL_VARIABLE)


#: How a suggested part's picture is cut (#897): how far past the part's outline it reaches, in PDF
#: points, and the resolution the vendor's page is rendered at. No defaults.
PART_PICTURE_MARGIN_VARIABLE = "GV_PART_PICTURE_MARGIN_PT"
PART_PICTURE_DPI_VARIABLE = "GV_PART_PICTURE_DPI"


def part_picture_configuration(*, required: bool) -> object | None:
    """The stated way to cut each part's picture, or `None` where nothing asks for one (#897).

    **Required whenever the worker suggests parts** (`required`, which is whenever the localized
    reader's settings are stated): the admin decided on 2026-10-04 that every suggested part gets
    its own picture, so a worker that suggests parts and cannot picture them refuses to start
    rather than leaving the Measure page without them. Otherwise optional, for a part a person adds,
    but never half-stated. Neither value has a default; a value that is not a number is refused
    rather than ignored.
    """
    stated = {
        name: os.environ.get(name, "").strip()
        for name in (PART_PICTURE_MARGIN_VARIABLE, PART_PICTURE_DPI_VARIABLE)
    }
    missing = [name for name, value in stated.items() if not value]
    if not required and len(missing) == len(stated):
        return None
    if missing:
        raise ValueError(
            "every suggested part gets a picture (#897), and how it is cut has no default; "
            "missing " + ", ".join(missing)
        )
    from decimal import InvalidOperation

    from workflow.part_pictures import PartPictureSettings

    try:
        margin = Decimal(stated[PART_PICTURE_MARGIN_VARIABLE])
    except InvalidOperation as error:
        raise ValueError(
            f"{PART_PICTURE_MARGIN_VARIABLE} must be a number of PDF points, such as 36"
        ) from error
    raw_dpi = stated[PART_PICTURE_DPI_VARIABLE]
    if not raw_dpi.isdigit():
        raise ValueError(f"{PART_PICTURE_DPI_VARIABLE} must be a whole number of dots per inch")
    return PartPictureSettings(margin_pt=margin, dpi=int(raw_dpi))


def automatic_typing_configuration() -> AutomaticTypingSettings | None:
    """Enable only explicitly approved exact-tag types; all other readings stay for review."""
    raw = os.environ.get("GV_AUTOMATIC_TYPES", "").strip()
    if not raw:
        return None
    from app.evidence.automatic_typing import AutomaticTypingSettings
    from vocabulary.semantic_types import SemanticType

    names = [item.strip() for item in raw.split(",") if item.strip()]
    try:
        permitted = frozenset(SemanticType(name) for name in names)
    except ValueError as error:
        raise ValueError(
            "GV_AUTOMATIC_TYPES must contain exact semantic-type tags, comma-separated"
        ) from error
    return AutomaticTypingSettings(permitted)


#: The phrase index's one setting (#836): how wide a space between two runs on one line may be, in line
#: heights, and still join them. No default — it is measured on the client's drawings and stated in
#: the worker's environment by the deployment, and without it no phrases are built.
PHRASE_GAP_VARIABLE = "GV_PHRASE_GAP_LINE_HEIGHTS"


def phrase_grouping() -> object | None:
    """The stated phrase gap, or `None` when the deployment has not stated one.

    A value that is not a finite number, or is negative, is refused rather than ignored: a typo that
    quietly left the index unbuilt would look exactly like a deployment that chose not to build it.
    """
    raw = os.environ.get(PHRASE_GAP_VARIABLE, "").strip()
    if not raw:
        return None
    from decimal import InvalidOperation

    from retrieval.package_text import PhraseGrouping

    try:
        gap = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(
            f"{PHRASE_GAP_VARIABLE} must be a number of line heights, such as 0.25"
        ) from error
    return PhraseGrouping(gap_line_heights=gap)


def build_package_text(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Build the revision's phrase index, and report what happened. Never raises.

    **It cannot fail the extraction.** The drawings were read and the readings are recorded; a
    search index is built from them and can be built again. So every failure is caught and reported
    in the worker's log line, and the build runs inside a savepoint, so a failure part-way through
    takes back its own rows and nothing that extraction wrote.

    A failure is named by its type alone. A database error repeats the row it refused, and a row here
    is the drawing's own text, which has no place in a log line (`AGENTS.md` §6).
    """
    try:
        grouping = phrase_grouping()
    except ValueError as error:
        return {"built": False, "reason": str(error)}
    if grouping is None:
        return {"built": False, "reason": f"{PHRASE_GAP_VARIABLE} is not set"}
    from retrieval.package_text import build_package_phrases

    try:
        with session.begin_nested():  # type: ignore[attr-defined]
            built = build_package_phrases(
                session,
                package_revision_id,
                grouping,  # type: ignore[arg-type]
            )
    except Exception as error:  # noqa: BLE001 - reported, never fatal to a read drawing
        return {"built": False, "reason": f"the phrase build failed: {type(error).__name__}"}
    return built.summary()


def hunt_package_values(
    session: object, package_revision_id: UUID, package_text: Mapping[str, object]
) -> Mapping[str, object]:
    """Point at the passages that state the settings the rules still need, if switched on (#881).

    Off unless `GV_VALUE_HUNTER` is on, and run only once the phrase index was built in this pass,
    because the index is what it searches. **It cannot fail the extraction**, for the reason the
    phrase build cannot: every failure is caught and reported, and the hunt runs inside a savepoint,
    so a failure part-way through takes back the pointers it filed and nothing that extraction wrote.
    A failure is named by its type alone, as the phrase build's is.
    """
    from workflow.value_hunter import VALUE_HUNTER_ENV, hunt_values, value_hunter_enabled

    if not value_hunter_enabled():
        return {"ran": False, "reason": f"{VALUE_HUNTER_ENV} is off"}
    if package_text.get("built") is not True:
        return {
            "ran": False,
            "reason": "the phrase index was not built, so there is nothing to search",
        }
    try:
        with session.begin_nested():  # type: ignore[attr-defined]
            hunt = hunt_values(session, package_revision_id)  # type: ignore[arg-type]
    except Exception as error:  # noqa: BLE001 - reported, never fatal to a read drawing
        return {"ran": False, "reason": f"the value hunter failed: {type(error).__name__}"}
    return hunt.summary()


def build_stages(
    *,
    store: ArtifactStore | None = None,
    settings: Settings | None = None,
    discriminators: Mapping[str, str] | None = None,
    discriminator_sources: Mapping[str, str] | None = None,
    timings: TimingRecorder | None = None,
) -> object:
    """Build the real stages, for every runner, against the same storage root as the dev API.

    Left out, `store` and `settings` are built exactly as the local worker always built them, and in
    the same order, so a configuration error is reported by the same check as before. A runner that
    already holds its own (the Hatchet worker, `workflow/entrypoints.py`) passes them in.
    """
    from storage.local import LocalStore
    from workflow.ai_budget import ai_budget_from_settings
    from workflow.findings_bedrock import configured_findings_composer
    from workflow.stages import (
        DatabaseStages,
        configured_architect_reader,
        label_lengths_from_environment,
        missing_space_from_environment,
    )

    # A LocalStore needs a signing key to satisfy its interface, but this worker never issues upload
    # tickets.  It only reads already-confirmed objects from the same explicitly configured dev root.
    if store is None:
        store = LocalStore(
            root=pathlib.Path(os.environ.get("GV_DEV_STORAGE", ".dev-storage")).resolve(),
            ticket_secret=b"local-review-worker-never-issues-tickets",
        )
    association, localized = reader_configuration()
    part_finder = part_finder_configuration()
    automatic_typing = automatic_typing_configuration()
    from app.config import Settings
    from workflow.form_reader import configured_form_reader
    from workflow.glyph_route import glyph_route_from_environment
    from workflow.slot_reader import configured_slot_reader

    if settings is None:
        settings = Settings()  # type: ignore[call-arg]
    form_reader = configured_form_reader(settings)
    return DatabaseStages(
        store,
        operands=None,
        discriminators=dict(discriminators or {}),
        discriminator_sources=dict(discriminator_sources or {}),
        association=association,
        localized_ocr=localized,
        automatic_typing=automatic_typing,
        findings_composer=configured_findings_composer(settings),
        # Off unless GV_GLYPH_TEMPLATES names a template set a person labelled (#756).
        glyph_route=glyph_route_from_environment(),
        # Unstated unless both are set: the lengths the agreement gate's cut-label guard gathers a
        # label by (#919). With them unstated the guard holds every agreement for a person.
        label_lengths=label_lengths_from_environment(),
        timings=timings,
        # Always required: how wide a gap inside the inches is a space the file left out (#912).
        # Unstated, building the stages fails with an error naming the variable, so no page is
        # read without it.
        missing_space=missing_space_from_environment(),
        form_reader=form_reader,
        # Off unless GV_SLOT_READER_ENABLED, and then only beside the form reader (#987).
        slot_reader=(slot_reader := configured_slot_reader(settings, form_reader)),
        # Required wherever parts are suggested (#897): wherever the reader's settings are, and
        # wherever the slot reader runs, because Bala suggests a part for every row it reads (#1236).
        part_pictures=part_picture_configuration(  # type: ignore[arg-type]
            required=association is not None or slot_reader is not None
        ),
        # Code's geometry-only part finder, in memory, as Bala's second judgment (#1236).
        part_finder=part_finder,  # type: ignore[arg-type]
        # Off unless GV_BALA_AUTO_ITEMS: code's countertop items on two judgments (#1244).
        bala_auto_items=bala_auto_items_configuration(),
        # Off unless GV_BALA_CABINET_PREFILL: "cabinet" pre-filled from one printed tag (#1266).
        bala_cabinet_prefill=bala_cabinet_prefill_configuration(),
        # Off unless GV_CABINET_AI_READING: a cabinet package read like a countertop package.
        cabinet_ai_reading=cabinet_ai_reading_configuration(),
        # Off unless GV_DEPTH_READER: each countertop's plan and section views read (#1290).
        depth_reader=depth_reader_configuration(),
        # Off unless GV_DEPTH_PER_ROW: the five depth checks per countertop row (#1390).
        depth_per_row=depth_per_row_configuration(),
        # Off unless GV_JOB_VALUES_READER: this job's four values from the architect (#1409).
        job_values_reader=job_values_reader_configuration(),
        # Off unless GV_ARCHITECT_READER_ENABLED: the architect's numbers read by code (#1052).
        architect_reader=configured_architect_reader(settings),
        # What the AI readers may spend on a drawing set, grown by its pages (#1206).
        ai_budget=ai_budget_from_settings(settings),
    )


def run_checks(
    session: object,
    package_revision_id: UUID,
    idempotency_key: str,
    discriminators: Mapping[str, str] | None = None,
    discriminator_sources: Mapping[str, str] | None = None,
    *,
    stages_factory: StagesFactory | None = None,
) -> Mapping[str, object]:
    """Run the checks for one revision, with what the reviewer supplied.

    **Discriminators come from the request rather than the database**, because that is what they are:
    a statement about how to read this package on this run. Without them `CT-WIDTH-001` and
    `CAB-FILLER-001` abstain with REVIEW_REQUIRED however complete the measurements are — the
    resolver cannot choose a variant nobody stated, and refuses to guess one.
    """
    # Imported here so this control-plane-safe module still has no heavy imports until it is asked
    # to consume work.  The operands are loaded by `DatabaseStages.run_checks` only after a human has
    # confirmed a candidate; no OCR proposal becomes a verdict operand by this path.
    from app.lifecycle.states import transition
    from app.models import PackageRevision, PackageState, WorkflowRun
    from workflow.review import run_stage

    revision = session.get(PackageRevision, package_revision_id)  # type: ignore[arg-type]
    if revision is None:
        return {"implemented": True, "ran": False, "reason": "no such package revision"}
    workflow_run_id = UUID(idempotency_key)
    dropped = drop_closed_revision(session, revision, workflow_run_id, WorkflowRun)
    if dropped is not None:
        return dropped
    resume_from_reviewer_input(session, revision)
    stages = (stages_factory or build_stages)(
        discriminators=discriminators, discriminator_sources=discriminator_sources
    )
    ensure_workflow_run(session, package_revision_id, workflow_run_id, WorkflowRun)
    # **Named by the row that asked, so asking twice runs twice.** Without it the key is the stage
    # and the revision, so a reviewer who supplied a missing value and pressed Run checks again was
    # indistinguishable from a redelivery of the first request: nothing ran, nothing moved, and the
    # findings stayed as they were. Redelivering *this* row keeps this key and stays idempotent.
    outcome = run_stage(
        session,
        stage="run_checks",
        state=PackageState.RUNNING_CHECKS,
        package_revision_id=package_revision_id,
        workflow_run_id=workflow_run_id,
        stages=stages,  # type: ignore[arg-type]
        request=idempotency_key,
    )
    output = run_stage(
        session,
        stage="generate_outputs",
        state=PackageState.GENERATING_OUTPUTS,
        package_revision_id=package_revision_id,
        workflow_run_id=workflow_run_id,
        stages=stages,  # type: ignore[arg-type]
        request=idempotency_key,
    )
    # **Only hand over if this run actually got there.** A stage that recognised itself as already
    # done moved nothing, so the revision is still wherever it was — and declaring a hand-over from
    # there asked the machine for `AWAITING_REVIEW -> AWAITING_REVIEW`, which it refuses, so a
    # redelivery failed for ever instead of returning quietly.
    if not (outcome.already_done and output.already_done):
        transition(
            session,
            package_revision_id,
            PackageState.AWAITING_REVIEW,
            actor="local review worker",
            reason="reviewer-confirmed readings were checked and handoff artifacts generated",
        )
    return {"checks": dict(outcome.payload), "outputs": dict(output.payload)}


def drop_closed_revision(
    session: object, revision: object, workflow_run_id: UUID, workflow_run_type: object
) -> Mapping[str, object] | None:
    """Drop a check run queued for a closed revision, with the reason recorded (#1182).

    A signed-off, sent-back, replaced or cancelled revision never runs its checks again: the
    lifecycle table has no move from any of them to `RUNNING_CHECKS`. Before #1182 such a run was
    tried, refused and retried on every poll for ever. Now it is dropped once: a `task_runs` row with
    the outcome `dropped: <state>` records it under the run's own workflow id, and the outbox row is
    marked delivered because the request was answered. The API refuses to queue one since #1182, so
    this is for a request queued before the sign-off (or before the fix).
    """
    from sqlalchemy import select

    from app.lifecycle.closed import closed_reason
    from app.models import PackageRevision, TaskRun

    if not isinstance(revision, PackageRevision):
        raise TypeError("revision must be a PackageRevision")
    reason = closed_reason(revision.state)
    if reason is None:
        return None
    ensure_workflow_run(session, revision.id, workflow_run_id, workflow_run_type)
    key = f"run_checks:{workflow_run_id}:dropped"
    if (
        session.scalar(  # type: ignore[attr-defined]
            select(TaskRun.id).where(TaskRun.idempotency_key == key)
        )
        is None
    ):
        session.add(  # type: ignore[attr-defined]
            TaskRun(
                workflow_run_id=workflow_run_id,
                idempotency_key=key,
                task_type="run_checks",
                attempt=1,
                outcome=f"dropped: {revision.state}",
            )
        )
        session.flush()  # type: ignore[attr-defined]
    return {"implemented": True, "ran": False, "reason": f"dropped: {reason}"}


def resume_from_reviewer_input(session: object, revision: object) -> None:
    """Resume the exact stage that deliberately handed control to the reviewer.

    Extraction ends in ``NEEDS_INPUT`` from ``VALIDATING_EVIDENCE`` so an empty proposal set has a
    visible, actionable handoff.  Once the reviewer submits values, the lifecycle guard correctly
    requires resumption at that same stage before checks may run; jumping straight to
    ``RUNNING_CHECKS`` would bypass the invariant.  Validation's task has already been recorded, so
    the subsequent idempotent ``run_stage`` does not repeat rendering — this transition records the
    legal return from the human handoff.
    """
    from app.lifecycle.states import transition
    from app.models import PackageRevision, PackageState

    if not isinstance(revision, PackageRevision):
        raise TypeError("revision must be a PackageRevision")
    if PackageState(revision.state) is not PackageState.NEEDS_INPUT:
        return
    transition(
        session,  # type: ignore[arg-type]
        revision.id,
        PackageState.VALIDATING_EVIDENCE,
        actor="local review worker",
        reason="reviewer supplied inputs; resuming from the evidence-validation handoff",
    )


def ensure_workflow_run(
    session: object,
    package_revision_id: UUID,
    workflow_run_id: UUID,
    workflow_run_type: object,
) -> None:
    """Create the durable parent before a stage claims its task.

    ``task_runs.workflow_run_id`` is a foreign key, not a label.  The outbox id is the stable
    idempotency key, so the local worker deliberately reuses it as the workflow id; a repeat finds
    the same parent and lets ``run_stage`` make its normal idempotent decision.
    """
    if session.get(workflow_run_type, workflow_run_id) is None:  # type: ignore[union-attr]
        session.add(  # type: ignore[union-attr]
            workflow_run_type(
                id=workflow_run_id,
                package_revision_id=package_revision_id,
                engine_run_id=str(workflow_run_id),
            )
        )
        session.flush()  # type: ignore[union-attr]


def cut_part_pictures(
    session: object, package_revision_id: UUID, *, stages_factory: StagesFactory | None = None
) -> Mapping[str, object]:
    """Cut the pictures still missing on one revision's drawings (#897).

    What a person adding a part asks for: the part is recorded by the API, which may not render a
    page, and its picture is cut here. It writes pictures and nothing else, and asking twice cuts
    nothing twice.
    """
    stages = (stages_factory or build_stages)()
    cut: Mapping[str, object] = stages.cut_part_pictures(  # type: ignore[attr-defined]
        session, package_revision_id
    )
    return cut


def render_vendor_page_pictures(
    session: object, package_revision_id: UUID, *, stages_factory: StagesFactory | None = None
) -> Mapping[str, object]:
    """Prepare stored vendor-only pages for click-to-place, without invoking any reader."""
    stages = (stages_factory or build_stages)()
    return stages.render_vendor_page_pictures(  # type: ignore[attr-defined]
        session, package_revision_id
    )


def propose_measurements(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Ask a model which reading fills which field, check it, and file what survived.

    **Every failure leaves the reviewer exactly where they are today**, typing the values in
    themselves — which is what a deployment with no model configured does, and what happened before
    this step existed. So nothing here raises: a drawing that was read successfully must not be
    lost because a provider was unreachable.

    Imported inside the function for the reason the other consumers are: this module stays
    control-plane-safe until it is actually asked to consume work.
    """
    from app.config import Settings
    from workflow.assignment_bedrock import configured_assignment_model
    from workflow.propose import propose_for_revision

    try:
        model = configured_assignment_model(Settings())  # type: ignore[call-arg]
        return propose_for_revision(session, package_revision_id, model)  # type: ignore[arg-type]
    except Exception as error:  # noqa: BLE001 - reported, never fatal to a read drawing
        return {"ran": False, "reason": f"the proposal step failed: {error}"}


def carry_forward(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Carry a person's answers from the revision this one superseded (#1180), in a savepoint.

    Reported, never raised: a carry that fails leaves the reviewer to answer again, which is the
    safe direction, and must not lose the drawings that were just read.
    """
    from app.lifecycle.carry_forward import carry_after_reading

    try:
        with session.begin_nested():  # type: ignore[attr-defined]
            return carry_after_reading(session, package_revision_id).as_payload()  # type: ignore[arg-type]
    except Exception as error:  # noqa: BLE001 - reported, never fatal to a read drawing
        return {"ran": False, "reason": f"carrying answers forward failed: {type(error).__name__}"}


def run_extraction(
    session: object,
    package_revision_id: UUID,
    idempotency_key: str,
    *,
    timings: TimingRecorder | None = None,
    stages_factory: StagesFactory | None = None,
) -> Mapping[str, object]:
    """Run only the pre-verdict stages and leave OCR proposals waiting for human confirmation.

    **Each stage commits as it finishes (#1178).** The reading used to run in one transaction, so
    for the eight minutes it took the package said `UPLOADED` and nothing it had done was visible.
    Now `ingest`, `extract_pages` and `match` each commit when they end, and the last stage commits
    together with the hand-over to the reviewer (the caller's commit). Inside a stage nothing changes:
    its claim, its state move and its rows still land together or not at all, so a stage either
    completes or is retried from its start. What is visible while a stage runs is written beside it,
    in short transactions of its own: the progress line and each paid model call (the call ledger,
    `workflow/reading_watch.py`).

    **A failure says so, and a retry resumes where it failed.** A stage that raises is rolled back,
    and the failure is recorded in a transaction of its own (`workflow/review.py:_record_failure`),
    naming the stage that failed. Redelivering the outbox row then finds every finished stage
    already claimed (`already_done`, nothing redone, nothing paid twice) and starts again at the
    stage that failed — where the readers reuse every answer the ledger already holds (#1112). A
    killed process records nothing; the package stays in the last committed stage's state, and the
    redelivery resumes from there the same way.
    """
    from sqlalchemy.orm import sessionmaker

    from app.lifecycle.side_states import enter_needs_input
    from app.models import PackageState, WorkflowRun
    from app.runs.reading_progress import record_progress, side_engine
    from workflow.after_reading import queue_after_reading
    from workflow.review import _record_failure, run_stage

    stages = (stages_factory or build_stages)(timings=timings)
    workflow_run_id = UUID(idempotency_key)
    ensure_workflow_run(session, package_revision_id, workflow_run_id, WorkflowRun)
    # Committed before any stage, so the call ledger can hang its runs off it (#1178).
    session.commit()  # type: ignore[attr-defined]
    failures = sessionmaker(bind=session.get_bind())  # type: ignore[attr-defined]
    results: dict[str, object] = {}
    final = "validate_evidence"
    for stage, state in (
        ("ingest", PackageState.INGESTING),
        ("extract_pages", PackageState.EXTRACTING),
        ("match", PackageState.MATCHING),
        ("validate_evidence", PackageState.VALIDATING_EVIDENCE),
    ):
        measure = (
            timings.measure(
                f"worker.stage.{stage}", run_id=str(package_revision_id), page_index=None
            )
            if timings is not None
            else nullcontext()
        )
        try:
            with measure:
                outcome = run_stage(
                    session,
                    stage=stage,
                    state=state,
                    package_revision_id=package_revision_id,
                    workflow_run_id=workflow_run_id,
                    stages=stages,  # type: ignore[arg-type]
                )
            if stage == "extract_pages":
                # **A later revision takes the reviewer's answers on what did not change** (#1180):
                # wall answers on identical rows and drawing roles on identical pages, before
                # matching reads the roles. Inside the stage's transaction, so the reading and its
                # carried answers commit together; idempotent, so a redelivery that finds the stage
                # done carries nothing twice. It cannot fail the reading: a failure means asking again.
                results["carry_forward"] = carry_forward(session, package_revision_id)
            if stage != final:
                # The stage boundary. The last stage stays open: the hand-over below lands with it.
                session.commit()  # type: ignore[attr-defined]
        except Exception as error:
            session.rollback()  # type: ignore[attr-defined]
            _record_failure(
                failures,
                package_revision_id,
                actor="local review worker",
                error=error,
                stage_state=state,
            )
            raise
        results[stage] = dict(outcome.payload)
    # **The hand-over is part of the last stage (#1178 review).** Phrase index, value hunter, the form
    # proposal and `NEEDS_INPUT` share `validate_evidence`'s transaction, so a failure anywhere here
    # rolls that stage back too, is recorded against it, and the retry starts again at it. Outside the
    # handler, a failure here left the package in `MATCHING` with nothing said, redelivered every poll.
    try:
        record_progress(side_engine(session), package_revision_id, "form")  # type: ignore[arg-type]
        # **Index the package's own words, once they are all recorded** (#836). Built from the stored
        # rows, so it needs nothing extraction did not already write, and it cannot fail the extraction:
        # `build_package_text` reports a failure rather than raising one.
        measure = (
            timings.measure("worker.package_text", run_id=str(package_revision_id))
            if timings is not None
            else nullcontext()
        )
        with measure:
            package_text = build_package_text(session, package_revision_id)
        results["package_text"] = package_text
        # **Then look in it for the settings the rules still need** (#881), off unless switched on. It
        # files pointers a person confirms by typing the number blind, never a setting, and it cannot
        # fail the extraction either.
        measure = (
            timings.measure("worker.value_hunter", run_id=str(package_revision_id))
            if timings is not None
            else nullcontext()
        )
        with measure:
            results["value_hunter"] = hunt_package_values(
                session, package_revision_id, package_text
            )

        # **Fill the reviewer's form, now, while the facts are in hand.**
        #
        # This is the whole point of doing it here rather than behind a button on the form: a reviewer
        # opening Measure finds it already filled and marked, instead of an empty form and a request to
        # make. It also costs one model call per upload rather than one per page load.
        #
        # It cannot fail the extraction. The drawings were read and the readings are recorded; a
        # provider that will not answer leaves the form to be filled by hand, which is exactly what a
        # deployment with no model configured does anyway. `propose_for_revision` catches its own
        # failures and reports them; this only decides what to print.
        measure = (
            timings.measure("worker.fill_with_ai_proposal", run_id=str(package_revision_id))
            if timings is not None
            else nullcontext()
        )
        with measure:
            results["propose_measurements"] = propose_measurements(session, package_revision_id)

        # Extraction is deliberately pre-verdict work. Whether it found many readings or none,
        # the next actor is the reviewer: confirm the untyped proposals and supply the values the
        # reader abstained on. Leaving a zero-result revision in VALIDATING_EVIDENCE makes that
        # honest abstention indistinguishable from a worker that is still running.
        # **Then ask for what comes next, on its own** (Set 3 QA): the page pictures "Look" needs,
        # and the check run when nothing it needs is missing. Queued in this transaction, so they
        # land with the hand-over; what still stops the checks is said in `needed`.
        after = queue_after_reading(session, package_revision_id)  # type: ignore[arg-type]
        results["after_reading"] = {
            "pictures_queued": after.pictures_queued,
            "checks_queued": after.checks_queued,
        }
        enter_needs_input(
            session,
            package_revision_id,
            actor="local review worker",
            needed=after.needed,
        )
    except Exception as error:
        session.rollback()  # type: ignore[attr-defined]
        _record_failure(
            failures,
            package_revision_id,
            actor="local review worker",
            error=error,
            stage_state=PackageState.VALIDATING_EVIDENCE,
        )
        raise
    return results


def consume(
    session: object,
    *,
    workflow: str,
    payload: Mapping[str, object],
    idempotency_key: str,
    timings: TimingRecorder | None = None,
    stages_factory: StagesFactory | None = None,
) -> Mapping[str, object]:
    """Do the work one outbox row names, in the caller's session.

    Commits nothing, except `extract_package`, which commits at each stage boundary so the reading
    is visible while it runs (#1178); its last stage and the hand-over are left for the caller's
    commit, like every other workflow's work.

    A workflow with no local consumer is refused, never dropped: the row stays undispatched for
    whichever worker does consume it.

    `stages_factory` builds the stages each piece of work needs; left out, it is `build_stages`.
    `process_package_revision` has no consumer here and is refused like any other unknown name.
    """
    from workflow.depth_again import READ_DEPTHS_AGAIN_WORKFLOW
    from workflow.draft_cloud import DRAFT_CLOUD_WORKFLOW
    from workflow.job_values import READ_JOB_VALUES_WORKFLOW
    from workflow.part_pictures import CUT_PART_PICTURES_WORKFLOW
    from workflow.slot_row_scope import READ_UNREAD_PAGES_WORKFLOW
    from workflow.vendor_page_pictures import RENDER_VENDOR_PAGE_PICTURES_WORKFLOW

    revision_id = UUID(str(payload["package_revision_id"]))
    if workflow == "generate_signed_exports":
        from sqlalchemy import select

        from app.models.review import Approval
        from storage.local import LocalStore
        from workflow.signed_outputs import generate_signed_outputs

        approval_id = UUID(str(payload["approval_id"]))
        approval = session.scalar(select(Approval).where(Approval.id == approval_id))
        if approval is None or approval.package_revision_id != revision_id:
            raise ValueError("signed export request belongs to a different revision")

        store = LocalStore(
            root=pathlib.Path(os.environ["GV_DEV_STORAGE"]).resolve(),
            ticket_secret=b"local-review-worker-never-issues-tickets",
        )
        bundle = generate_signed_outputs(session, store, approval_id)
        return {"bundle_id": str(bundle.id)}
    if workflow == DRAFT_CLOUD_WORKFLOW:
        # A reviewer asked for the draft cloud PDF before sign-off (#1340). It decides nothing;
        # a failure is raised with its sentence, and the outbox parks the request with it.
        from storage.local import LocalStore
        from workflow.draft_cloud_render import consume_draft_request

        draft_store = LocalStore(
            root=pathlib.Path(os.environ["GV_DEV_STORAGE"]).resolve(),
            ticket_secret=b"local-review-worker-never-issues-tickets",
        )
        return consume_draft_request(session, draft_store, payload, idempotency_key)  # type: ignore[arg-type]
    if workflow == "extract_package":
        return run_extraction(
            session,
            revision_id,
            idempotency_key,
            timings=timings,
            stages_factory=stages_factory,
        )
    if workflow == "run_checks":
        raw = payload.get("discriminators")
        # Narrowed rather than cast: the payload is JSON from a database row, so its shape is a
        # claim this process checks rather than assumes. A malformed declaration lets a rule abstain
        # rather than selecting a layout by guess.
        stated = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        # Where each came from (#992): a value the readers agreed on is named so on its finding.
        raw_sources = payload.get("discriminator_sources")
        sources = (
            {str(k): str(v) for k, v in raw_sources.items()}
            if isinstance(raw_sources, dict)
            else {}
        )
        return run_checks(
            session, revision_id, idempotency_key, stated, sources, stages_factory=stages_factory
        )
    if workflow == READ_UNREAD_PAGES_WORKFLOW:
        # A person asked to read the pages the AI budget stopped (#1206), on the same revision.
        stages = (stages_factory or build_stages)(timings=timings)
        return stages.read_unread_pages(session, revision_id)  # type: ignore[attr-defined]
    if workflow == READ_DEPTHS_AGAIN_WORKFLOW:
        # A person asked to read the depths again (#1308): Bala and the depth and sink reader only,
        # on the same revision, as a run of this request's own. A refusal is raised with its
        # sentence, and the outbox parks the request with it.
        from workflow.depth_again import DepthsAgainRefused

        try:
            stages = (stages_factory or build_stages)(timings=timings)
            read: Mapping[str, object] = stages.read_depths_again(  # type: ignore[attr-defined]
                session, revision_id, idempotency_key
            )
        except DepthsAgainRefused:
            raise
        except Exception as error:
            # Never retried for ever behind "reading now" (#1308 review): parked at once with
            # why. Asking again costs nothing for the answers already paid for (stored answers).
            raise DepthsAgainRefused(
                f"Depths were not read again: {type(error).__name__}: {error}"[:900]
            ) from error
        return read
    if workflow == READ_JOB_VALUES_WORKFLOW:
        # Someone asked for a set's job values (#1409): the job values reader only, on the same
        # revision, as a run of this request's own. A refusal is raised with its sentence.
        stages = (stages_factory or build_stages)(timings=timings)
        return stages.read_job_values_again(  # type: ignore[attr-defined,no-any-return]
            session, revision_id, idempotency_key
        )
    if workflow == CUT_PART_PICTURES_WORKFLOW:
        # A person added a part (#897): cut its picture, and any other still missing.
        return cut_part_pictures(session, revision_id, stages_factory=stages_factory)
    if workflow == RENDER_VENDOR_PAGE_PICTURES_WORKFLOW:
        return render_vendor_page_pictures(session, revision_id, stages_factory=stages_factory)
    print(f"  no local consumer for {workflow!r} — leaving it for its worker")
    raise NotImplementedError(f"no local consumer for {workflow!r}")
