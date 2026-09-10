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
        "GV_READER_LOCALIZED_MINIMUM_PATHS": os.environ.get("GV_READER_LOCALIZED_MINIMUM_PATHS"),
        "GV_READER_LOCALIZED_MAXIMUM_SPAN": os.environ.get("GV_READER_LOCALIZED_MAXIMUM_SPAN"),
        "GV_READER_LOCALIZED_CROP_MARGIN_PT": os.environ.get("GV_READER_LOCALIZED_CROP_MARGIN_PT"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise ValueError("localized OCR is enabled but missing " + ", ".join(missing))
    from workflow.association import AssociationSettings, LocalizedOcrSettings

    return (
        AssociationSettings(
            line_minimum_pt=Decimal(required["GV_READER_LINE_MINIMUM_PT"] or ""),
            glyph_maximum_pt=Decimal(required["GV_READER_GLYPH_MAXIMUM_PT"] or ""),
            glyph_gap_pt=Decimal(required["GV_READER_GLYPH_GAP_PT"] or ""),
            proximity_limit=Decimal(required["GV_READER_PROXIMITY_LIMIT"] or ""),
            ambiguity_margin=Decimal(required["GV_READER_AMBIGUITY_MARGIN"] or ""),
        ),
        LocalizedOcrSettings(
            minimum_paths=int(required["GV_READER_LOCALIZED_MINIMUM_PATHS"] or ""),
            maximum_span=Decimal(required["GV_READER_LOCALIZED_MAXIMUM_SPAN"] or ""),
            crop_margin_pt=Decimal(required["GV_READER_LOCALIZED_CROP_MARGIN_PT"] or ""),
        ),
    )


def _stages(*, discriminators: Mapping[str, str] | None = None) -> object:
    """Build the local worker's real stages against the same storage root as the dev API."""
    from storage.local import LocalStore
    from workflow.findings_bedrock import configured_findings_composer
    from workflow.stages import DatabaseStages

    # A LocalStore needs a signing key to satisfy its interface, but this worker never issues upload
    # tickets.  It only reads already-confirmed objects from the same explicitly configured dev root.
    store = LocalStore(
        root=pathlib.Path(os.environ.get("GV_DEV_STORAGE", ".dev-storage")).resolve(),
        ticket_secret=b"local-review-worker-never-issues-tickets",
    )
    association, localized = _reader_configuration()
    from app.config import Settings

    return DatabaseStages(
        store,
        operands=None,
        discriminators=dict(discriminators or {}),
        association=association,
        localized_ocr=localized,
        findings_composer=configured_findings_composer(Settings()),  # type: ignore[call-arg]
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
    outcome = run_stage(
        session,
        stage="run_checks",
        state=PackageState.RUNNING_CHECKS,
        package_revision_id=package_revision_id,
        workflow_run_id=workflow_run_id,
        stages=stages,  # type: ignore[arg-type]
    )
    output = run_stage(
        session,
        stage="generate_outputs",
        state=PackageState.GENERATING_OUTPUTS,
        package_revision_id=package_revision_id,
        workflow_run_id=workflow_run_id,
        stages=stages,  # type: ignore[arg-type]
    )
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
        with factory() as session:
            if workflow == "extract_package":
                result = _extract_package(session, revision_id, idempotency_key)
            elif workflow == "run_checks":
                raw = payload.get("discriminators")
                # Narrowed rather than cast: the payload is JSON from a database row, so its shape is
                # a claim this process checks rather than assumes. A malformed declaration lets a
                # rule abstain rather than selecting a layout by guess.
                stated = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
                result = _run_checks(session, revision_id, idempotency_key, stated)
            else:
                print(f"  no local consumer for {workflow!r} — leaving it for its worker")
                raise NotImplementedError(f"no local consumer for {workflow!r}")
            session.commit()
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
