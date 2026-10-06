"""Do the work the API accepted but may not perform itself.

`POST .../checks` writes an outbox row and commits. It cannot run the checks: every import under
`app/api/` is walked by `tests/api/test_no_heavy_work.py`, and `workflow/stages.py` reaches
`extraction.reader` since the OCR route landed — so the control plane may not import the thing that
does the work. That separation is `DESIGN_PLATFORM.md` §4.2, not an inconvenience.

**This is the stand-in for a registered worker, and it says so.** Phase 6 puts a Hatchet workflow
behind the outbox; until then the row would sit there looking accepted while nothing ran, which is
worse than not offering the button. This drains it in one pass, in a process that is allowed to
import extraction.

It reuses `workflow/outbox.py:dispatch_committed` rather than polling itself, which is the whole
point: `FOR UPDATE SKIP LOCKED`, attempt counting, the increment-then-start-then-stamp ordering and
the at-least-once guarantee are already written and tested there. A second poll loop here would be a
second set of those decisions, and the two would drift.

Usage:

    python scripts/drain_outbox.py            # one pass
    python scripts/drain_outbox.py --watch    # keep draining, for a demo

Verification: `tests/test_drain_outbox.py`
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import time
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.evidence.automatic_typing import AutomaticTypingSettings

#: How long `--watch` sleeps between passes. Two seconds matches `GV_OUTBOX_POLL_SECONDS`'s default,
#: which `.env.example` describes as "the visible wait between a package being accepted and its
#: workflow starting, and somebody is watching".
WATCH_SECONDS = 2.0


def _reader_configuration() -> tuple[object | None, object | None]:
    """Read explicitly supplied localized-reader settings, or leave that optional route off.

    The thresholds select pixels to OCR, so they are deployment configuration rather than product
    defaults.  A partial configuration is refused; silently filling one value would change which
    drawing region gets read.  The demo launcher supplies its documented synthetic-fixture values.
    """
    if os.environ.get("GV_LOCALIZED_OCR_ENABLED", "").lower() not in {"1", "true", "yes"}:
        return None, None
    required = {
        "GV_READER_LINE_MINIMUM_PT": os.environ.get("GV_READER_LINE_MINIMUM_PT"),
        "GV_READER_GLYPH_MAXIMUM_PT": os.environ.get("GV_READER_GLYPH_MAXIMUM_PT"),
        "GV_READER_GLYPH_GAP_PT": os.environ.get("GV_READER_GLYPH_GAP_PT"),
        "GV_READER_PROXIMITY_LIMIT": os.environ.get("GV_READER_PROXIMITY_LIMIT"),
        "GV_READER_AMBIGUITY_MARGIN": os.environ.get("GV_READER_AMBIGUITY_MARGIN"),
        # The dimension-line detector (#179). Required for the same reason as the rest: each
        # decides which strokes a reading may attach to, and a default would be this machine's
        # guess shipped as every deployment's.
        "GV_READER_WITNESS_TOLERANCE": os.environ.get("GV_READER_WITNESS_TOLERANCE"),
        "GV_READER_MINIMUM_SPAN": os.environ.get("GV_READER_MINIMUM_SPAN"),
        "GV_READER_STRAIGHTNESS": os.environ.get("GV_READER_STRAIGHTNESS"),
        "GV_READER_CROSSING_MARGIN": os.environ.get("GV_READER_CROSSING_MARGIN"),
        "GV_READER_LOCALIZED_MINIMUM_PATHS": os.environ.get("GV_READER_LOCALIZED_MINIMUM_PATHS"),
        "GV_READER_LOCALIZED_MAXIMUM_SPAN": os.environ.get("GV_READER_LOCALIZED_MAXIMUM_SPAN"),
        "GV_READER_LOCALIZED_CROP_MARGIN_PT": os.environ.get("GV_READER_LOCALIZED_CROP_MARGIN_PT"),
        # The stacked-fraction detector (#735). Required with the rest because the rule it enforces —
        # a stacked fraction always goes to a reviewer (#726) — cannot run without it, and the guard
        # it replaced was optional and was never once supplied.
        "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT": os.environ.get(
            "GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"
        ),
        "GV_READER_FRACTION_BAR_LENGTH_MIN_PT": os.environ.get(
            "GV_READER_FRACTION_BAR_LENGTH_MIN_PT"
        ),
        "GV_READER_FRACTION_REACH_PT": os.environ.get("GV_READER_FRACTION_REACH_PT"),
        "GV_READER_FRACTION_GLYPH_MIN_PT": os.environ.get("GV_READER_FRACTION_GLYPH_MIN_PT"),
        "GV_READER_FRACTION_GLYPH_MAX_PT": os.environ.get("GV_READER_FRACTION_GLYPH_MAX_PT"),
        "GV_READER_FRACTION_PROPORTION_MAX": os.environ.get("GV_READER_FRACTION_PROPORTION_MAX"),
        # Where each part of a stacked label was drawn (#834), which a reading of it must match.
        "GV_READER_FRACTION_CHARACTER_GAP_PT": os.environ.get(
            "GV_READER_FRACTION_CHARACTER_GAP_PT"
        ),
        # Which bars across a stamp's baseline are a turned label's fraction (#869).
        "GV_READER_FRACTION_TURNED_ASPECT_MIN": os.environ.get(
            "GV_READER_FRACTION_TURNED_ASPECT_MIN"
        ),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("localized OCR is enabled but missing " + ", ".join(missing))
    from extraction.glyph_bands import FractionBarGeometry
    from workflow.association import AssociationSettings, LocalizedOcrSettings

    return (
        AssociationSettings(
            line_minimum_pt=Decimal(required["GV_READER_LINE_MINIMUM_PT"] or ""),
            glyph_maximum_pt=Decimal(required["GV_READER_GLYPH_MAXIMUM_PT"] or ""),
            glyph_gap_pt=Decimal(required["GV_READER_GLYPH_GAP_PT"] or ""),
            proximity_limit=Decimal(required["GV_READER_PROXIMITY_LIMIT"] or ""),
            ambiguity_margin=Decimal(required["GV_READER_AMBIGUITY_MARGIN"] or ""),
            witness_tolerance=Decimal(required["GV_READER_WITNESS_TOLERANCE"] or ""),
            minimum_span=Decimal(required["GV_READER_MINIMUM_SPAN"] or ""),
            straightness=Decimal(required["GV_READER_STRAIGHTNESS"] or ""),
            crossing_margin=Decimal(required["GV_READER_CROSSING_MARGIN"] or ""),
            fraction_bar=FractionBarGeometry(
                bar_thickness_max_pt=Decimal(
                    required["GV_READER_FRACTION_BAR_THICKNESS_MAX_PT"] or ""
                ),
                bar_length_min_pt=Decimal(required["GV_READER_FRACTION_BAR_LENGTH_MIN_PT"] or ""),
                reach_pt=Decimal(required["GV_READER_FRACTION_REACH_PT"] or ""),
                glyph_min_pt=Decimal(required["GV_READER_FRACTION_GLYPH_MIN_PT"] or ""),
                glyph_max_pt=Decimal(required["GV_READER_FRACTION_GLYPH_MAX_PT"] or ""),
                proportion_max=Decimal(required["GV_READER_FRACTION_PROPORTION_MAX"] or ""),
                character_gap_pt=Decimal(required["GV_READER_FRACTION_CHARACTER_GAP_PT"] or ""),
                turned_aspect_min=Decimal(required["GV_READER_FRACTION_TURNED_ASPECT_MIN"] or ""),
            ),
        ),
        LocalizedOcrSettings(
            minimum_paths=int(required["GV_READER_LOCALIZED_MINIMUM_PATHS"] or ""),
            maximum_span=Decimal(required["GV_READER_LOCALIZED_MAXIMUM_SPAN"] or ""),
            crop_margin_pt=Decimal(required["GV_READER_LOCALIZED_CROP_MARGIN_PT"] or ""),
        ),
    )


#: How a suggested part's picture is cut (#897): how far past the part's outline it reaches, in PDF
#: points, and the resolution the vendor's page is rendered at. No defaults.
PART_PICTURE_MARGIN_VARIABLE = "GV_PART_PICTURE_MARGIN_PT"
PART_PICTURE_DPI_VARIABLE = "GV_PART_PICTURE_DPI"


def _part_picture_configuration(*, required: bool) -> object | None:
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


def _automatic_typing_configuration() -> AutomaticTypingSettings | None:
    """Enable only explicitly approved exact-tag types; all other readings stay for review."""
    raw = os.environ.get("GV_AUTOMATIC_TYPES", "").strip()
    if not raw:
        return None
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


def _phrase_grouping() -> object | None:
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


def _build_package_text(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Build the revision's phrase index, and report what happened. Never raises.

    **It cannot fail the extraction.** The drawings were read and the readings are recorded; a
    search index is built from them and can be built again. So every failure is caught and reported
    in the worker's log line, and the build runs inside a savepoint, so a failure part-way through
    takes back its own rows and nothing that extraction wrote.

    A failure is named by its type alone. A database error repeats the row it refused, and a row here
    is the drawing's own text, which has no place in a log line (`AGENTS.md` §6).
    """
    try:
        grouping = _phrase_grouping()
    except ValueError as error:
        return {"built": False, "reason": str(error)}
    if grouping is None:
        return {"built": False, "reason": f"{PHRASE_GAP_VARIABLE} is not set"}
    from retrieval.package_text import build_package_phrases

    try:
        with session.begin_nested():  # type: ignore[attr-defined]
            built = build_package_phrases(
                session, package_revision_id, grouping  # type: ignore[arg-type]
            )
    except Exception as error:  # noqa: BLE001 - reported, never fatal to a read drawing
        return {"built": False, "reason": f"the phrase build failed: {type(error).__name__}"}
    return built.summary()


def _hunt_values(
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


def _stages(*, discriminators: Mapping[str, str] | None = None) -> object:
    """Build the local worker's real stages against the same storage root as the dev API."""
    from storage.local import LocalStore
    from workflow.findings_bedrock import configured_findings_composer
    from workflow.stages import (
        DatabaseStages,
        fraction_parts_from_environment,
        missing_space_from_environment,
    )

    # A LocalStore needs a signing key to satisfy its interface, but this worker never issues upload
    # tickets.  It only reads already-confirmed objects from the same explicitly configured dev root.
    store = LocalStore(
        root=pathlib.Path(os.environ.get("GV_DEV_STORAGE", ".dev-storage")).resolve(),
        ticket_secret=b"local-review-worker-never-issues-tickets",
    )
    association, localized = _reader_configuration()
    automatic_typing = _automatic_typing_configuration()
    from app.config import Settings
    from workflow.glyph_route import glyph_route_from_environment
    from workflow.reader_pictures import picture_settings_from_environment
    from workflow.reading_agent import reading_agent_from_environment

    return DatabaseStages(
        store,
        operands=None,
        discriminators=dict(discriminators or {}),
        association=association,
        localized_ocr=localized,
        automatic_typing=automatic_typing,
        findings_composer=configured_findings_composer(Settings()),  # type: ignore[call-arg]
        # Off unless GV_GLYPH_TEMPLATES names a template set a person labelled (#756).
        glyph_route=glyph_route_from_environment(),
        # Off unless GV_READING_AGENT is on, and then every one of its settings is required (#757).
        reading_agent=reading_agent_from_environment(),
        # Off unless named: the vision reader that goes first, the others reading only where it
        # read a value (#787). An unknown name is refused when the stages are built.
        vision_gate=os.environ.get("GV_VISION_GATE_READER", "").strip() or None,
        # Off unless GV_FRACTION_PARTS is on, and then every drawing setting is required (#848),
        # and so is the gate reader above: it is the route's second reader (#865).
        fraction_parts=fraction_parts_from_environment(),
        # The dpi an upright, sharper picture is rendered at (#907). No default: a stage with a
        # reader shown one refuses to start without it.
        reader_pictures=picture_settings_from_environment(),
        # Always required: how wide a gap inside the inches is a space the file left out (#912).
        # Unstated, building the stages fails with an error naming the variable, so no page is
        # read without it.
        missing_space=missing_space_from_environment(),
        # Required wherever parts are suggested, which is wherever the reader's settings are (#897).
        part_pictures=_part_picture_configuration(  # type: ignore[arg-type]
            required=association is not None
        ),
    )


def _run_checks(
    session: object,
    package_revision_id: UUID,
    idempotency_key: str,
    discriminators: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Run the checks for one revision, with what the reviewer supplied.

    **Discriminators come from the request rather than the database**, because that is what they are:
    a statement about how to read this package on this run. Without them `CT-WIDTH-001` and
    `CAB-FILLER-001` abstain with REVIEW_REQUIRED however complete the measurements are — the
    resolver cannot choose a variant nobody stated, and refuses to guess one.
    """
    # Imported here so this control-plane-safe script still has no heavy imports until it is asked
    # to consume work.  The operands are loaded by `DatabaseStages.run_checks` only after a human has
    # confirmed a candidate; no OCR proposal becomes a verdict operand by this path.
    from app.lifecycle.states import transition
    from app.models import PackageRevision, PackageState, WorkflowRun
    from workflow.review import run_stage

    revision = session.get(PackageRevision, package_revision_id)  # type: ignore[arg-type]
    if revision is None:
        return {"implemented": True, "ran": False, "reason": "no such package revision"}
    _resume_from_reviewer_input(session, revision)
    stages = _stages(discriminators=discriminators)
    workflow_run_id = UUID(idempotency_key)
    _ensure_workflow_run(session, package_revision_id, workflow_run_id, WorkflowRun)
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


def _resume_from_reviewer_input(session: object, revision: object) -> None:
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


def _ensure_workflow_run(
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


def _cut_part_pictures(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Cut the pictures still missing on one revision's drawings (#897).

    What a person adding a part asks for: the part is recorded by the API, which may not render a
    page, and its picture is cut here. It writes pictures and nothing else, and asking twice cuts
    nothing twice.
    """
    stages = _stages()
    cut: Mapping[str, object] = stages.cut_part_pictures(  # type: ignore[attr-defined]
        session, package_revision_id
    )
    return cut


def _render_vendor_page_pictures(
    session: object, package_revision_id: UUID
) -> Mapping[str, object]:
    """Prepare stored vendor-only pages for click-to-place, without invoking any reader."""
    stages = _stages()
    return stages.render_vendor_page_pictures(  # type: ignore[attr-defined]
        session, package_revision_id
    )


def _propose_measurements(session: object, package_revision_id: UUID) -> Mapping[str, object]:
    """Ask a model which reading fills which field, check it, and file what survived.

    **Every failure leaves the reviewer exactly where they are today**, typing the values in
    themselves — which is what a deployment with no model configured does, and what happened before
    this step existed. So nothing here raises: a drawing that was read successfully must not be
    lost because a provider was unreachable.

    Imported inside the function for the reason the other consumers are: this script stays
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


def _extract_package(
    session: object, package_revision_id: UUID, idempotency_key: str
) -> Mapping[str, object]:
    """Run only the pre-verdict stages and leave OCR proposals waiting for human confirmation."""
    from app.lifecycle.side_states import enter_needs_input
    from app.models import PackageState, WorkflowRun
    from workflow.review import run_stage

    stages = _stages()
    workflow_run_id = UUID(idempotency_key)
    _ensure_workflow_run(session, package_revision_id, workflow_run_id, WorkflowRun)
    results: dict[str, object] = {}
    for stage, state in (
        ("ingest", PackageState.INGESTING),
        ("extract_pages", PackageState.EXTRACTING),
        ("match", PackageState.MATCHING),
        ("validate_evidence", PackageState.VALIDATING_EVIDENCE),
    ):
        outcome = run_stage(
            session,
            stage=stage,
            state=state,
            package_revision_id=package_revision_id,
            workflow_run_id=workflow_run_id,
            stages=stages,  # type: ignore[arg-type]
        )
        results[stage] = dict(outcome.payload)
    # **Index the package's own words, once they are all recorded** (#836). Built from the stored
    # rows, so it needs nothing extraction did not already write, and it cannot fail the extraction:
    # `_build_package_text` reports a failure rather than raising one.
    package_text = _build_package_text(session, package_revision_id)
    results["package_text"] = package_text
    # **Then look in it for the settings the rules still need** (#881), off unless switched on. It
    # files pointers a person confirms by typing the number blind, never a setting, and it cannot
    # fail the extraction either.
    results["value_hunter"] = _hunt_values(session, package_revision_id, package_text)

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
    results["propose_measurements"] = _propose_measurements(session, package_revision_id)

    # Extraction is deliberately pre-verdict work. Whether it found many readings or none,
    # the next actor is the reviewer: confirm the untyped proposals and supply the values the
    # reader abstained on. Leaving a zero-result revision in VALIDATING_EVIDENCE makes that
    # honest abstention indistinguishable from a worker that is still running.
    enter_needs_input(
        session,
        package_revision_id,
        actor="local review worker",
        needed=(
            "confirm any AI reading proposals, then provide the dimensions the reader abstained on "
            "before running deterministic checks"
        ),
    )
    return results


def _consume(
    session: object, *, workflow: str, payload: Mapping[str, object], idempotency_key: str
) -> Mapping[str, object]:
    """Do the work one outbox row names, in the caller's session. Commits nothing.

    A workflow with no local consumer is refused, never dropped: the row stays undispatched for
    whichever worker does consume it.
    """
    from workflow.part_pictures import CUT_PART_PICTURES_WORKFLOW
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
    if workflow == "extract_package":
        return _extract_package(session, revision_id, idempotency_key)
    if workflow == "run_checks":
        raw = payload.get("discriminators")
        # Narrowed rather than cast: the payload is JSON from a database row, so its shape is a
        # claim this process checks rather than assumes. A malformed declaration lets a rule abstain
        # rather than selecting a layout by guess.
        stated = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        return _run_checks(session, revision_id, idempotency_key, stated)
    if workflow == CUT_PART_PICTURES_WORKFLOW:
        # A person added a part (#897): cut its picture, and any other still missing.
        return _cut_part_pictures(session, revision_id)
    if workflow == RENDER_VENDOR_PAGE_PICTURES_WORKFLOW:
        return _render_vendor_page_pictures(session, revision_id)
    print(f"  no local consumer for {workflow!r} — leaving it for its worker")
    raise NotImplementedError(f"no local consumer for {workflow!r}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="keep draining rather than one pass")
    args = parser.parse_args(argv)

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session, sessionmaker

    from app.config import Settings
    from app.db.session import session_factory
    from workflow.outbox import dispatch_committed

    settings = Settings()  # type: ignore[call-arg]
    factory: sessionmaker[Session] = session_factory(create_engine(settings.database_url))

    def _start(*, workflow: str, payload: Mapping[str, object], idempotency_key: str) -> None:
        """The starter `dispatch_committed` calls, doing the work in its own session.

        **Its own session on purpose.** `dispatch_committed` holds the outbox row's transaction, and
        the ordering it documents — increment attempts, start, then stamp `dispatched_at` — only
        means anything if starting is separable from that bookkeeping. Writing findings into the
        dispatcher's transaction would tie a rolled-back dispatch to discarded findings, which is a
        different failure mode than the one that module reasoned about.

        A repeat of the same key must be a no-op, and `run_checks` already is: it supersedes prior
        runs for the revision and writes a fresh set, so running it twice leaves one live set rather
        than two.
        """
        revision_id = UUID(str(payload["package_revision_id"]))
        try:
            with factory() as session:
                result = _consume(
                    session, workflow=workflow, payload=payload, idempotency_key=idempotency_key
                )
                session.commit()
        except Exception as error:
            if workflow == "generate_signed_exports":
                from workflow.signed_outputs import record_publication_failure

                record_publication_failure(factory, UUID(str(payload["approval_id"])), error)
            raise
        print(f"  {workflow} {revision_id}: {dict(result)}")

    passes = 0
    while True:
        try:
            started = dispatch_committed(factory, _start)
        except Exception as failed:  # noqa: BLE001 - reported, and the loop continues
            print(f"dispatch reported failures: {failed}", file=sys.stderr)
            started = 0
        passes += 1
        if started:
            print(f"dispatched {started} row(s)")
        if not args.watch:
            if not started:
                print("nothing to dispatch")
            return 0
        time.sleep(WATCH_SECONDS)


if __name__ == "__main__":  # pragma: no cover - exercised through main() in tests
    raise SystemExit(main())
