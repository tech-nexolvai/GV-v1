"""Do the work the API accepted but may not perform itself: loop over the outbox and hand each row
to `workflow.sutradhar.consume`, which runs it (#1227).

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

**The work itself lives in `workflow/sutradhar.py` (#1227)**, where the Hatchet worker builds its
stages too, so the two cannot drift. This file is the local runner's shell: it reads the settings,
loops over the outbox and hands each row to `sutradhar.consume`. A few private names are kept, as
thin delegates or aliases, because tests and local launchers call them. **Only `_stages` changes
what the flow does:** patching it here changes the stages every workflow gets. Every other step is
looked up in `workflow.sutradhar`, so patch it there.

**Never run two consumers on one outbox.** This consumer (`make worker`) and the Hatchet dispatcher
(`make dispatch`) both take rows with `FOR UPDATE SKIP LOCKED`; started together they compete for
the same rows. Run one.

Verification: `tests/scripts/test_drain_outbox.py`, `tests/workflow/test_sutradhar.py`
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import time
from collections.abc import Mapping
from contextlib import nullcontext
from datetime import UTC, datetime
from uuid import UUID

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from workflow import sutradhar
from workflow.timing import TimingRecorder, timing_recorder_from_environment

#: How long `--watch` sleeps between passes. Two seconds matches `GV_OUTBOX_POLL_SECONDS`'s default,
#: which `.env.example` describes as "the visible wait between a package being accepted and its
#: workflow starting, and somebody is watching".
WATCH_SECONDS = 2.0


# The settings each route reads, kept under their old names: tests and launchers use them.
PART_PICTURE_MARGIN_VARIABLE = sutradhar.PART_PICTURE_MARGIN_VARIABLE
PART_PICTURE_DPI_VARIABLE = sutradhar.PART_PICTURE_DPI_VARIABLE
PHRASE_GAP_VARIABLE = sutradhar.PHRASE_GAP_VARIABLE
_reader_configuration = sutradhar.reader_configuration
_part_picture_configuration = sutradhar.part_picture_configuration
_automatic_typing_configuration = sutradhar.automatic_typing_configuration
_phrase_grouping = sutradhar.phrase_grouping

# The steps tests call directly, kept under their old names. Patching one of these changes nothing
# the flow does: the flow looks its steps up in `workflow.sutradhar`, so patch them there.
_build_package_text = sutradhar.build_package_text
_resume_from_reviewer_input = sutradhar.resume_from_reviewer_input
_carry_forward = sutradhar.carry_forward


def _stages(
    *,
    discriminators: Mapping[str, str] | None = None,
    discriminator_sources: Mapping[str, str] | None = None,
    timings: TimingRecorder | None = None,
) -> object:
    """Build the local worker's real stages against the same storage root as the dev API."""
    return sutradhar.build_stages(
        discriminators=discriminators,
        discriminator_sources=discriminator_sources,
        timings=timings,
    )


def _stages_factory(**kwargs: object) -> object:
    """`_stages`, looked up when the stages are built, so a patch of it here takes effect."""
    return _stages(**kwargs)  # type: ignore[arg-type]


def _run_checks(
    session: object,
    package_revision_id: UUID,
    idempotency_key: str,
    discriminators: Mapping[str, str] | None = None,
    discriminator_sources: Mapping[str, str] | None = None,
) -> Mapping[str, object]:
    """Run the checks for one revision, with what the reviewer supplied (`sutradhar.run_checks`)."""
    return sutradhar.run_checks(
        session,
        package_revision_id,
        idempotency_key,
        discriminators,
        discriminator_sources,
        stages_factory=_stages_factory,
    )


def _extract_package(
    session: object,
    package_revision_id: UUID,
    idempotency_key: str,
    *,
    timings: TimingRecorder | None = None,
) -> Mapping[str, object]:
    """Run only the pre-verdict stages and leave OCR proposals waiting for human confirmation.

    `sutradhar.run_extraction` says how: a commit at each stage boundary, the hand-over with the last.
    """
    return sutradhar.run_extraction(
        session,
        package_revision_id,
        idempotency_key,
        timings=timings,
        stages_factory=_stages_factory,
    )


def _consume(
    session: object,
    *,
    workflow: str,
    payload: Mapping[str, object],
    idempotency_key: str,
    timings: TimingRecorder | None = None,
) -> Mapping[str, object]:
    """Do the work one outbox row names, in the caller's session (`sutradhar.consume`)."""
    return sutradhar.consume(
        session,
        workflow=workflow,
        payload=payload,
        idempotency_key=idempotency_key,
        timings=timings,
        stages_factory=_stages_factory,
    )


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
    timings = timing_recorder_from_environment()

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
        upload_started_at: datetime | None = None
        try:
            with factory() as session:
                if workflow == "extract_package":
                    from sqlalchemy import func, select

                    from app.models.document import Document, PackageRevisionDocument

                    upload_started_at = session.scalar(
                        select(func.min(Document.created_at))
                        .join(
                            PackageRevisionDocument,
                            PackageRevisionDocument.document_id == Document.id,
                        )
                        .where(PackageRevisionDocument.package_revision_id == revision_id)
                    )
                result = _consume(
                    session,
                    workflow=workflow,
                    payload=payload,
                    idempotency_key=idempotency_key,
                    timings=timings,
                )
                commit_measure = (
                    timings.measure("worker.final_commit", run_id=str(revision_id))
                    if timings is not None
                    else nullcontext()
                )
                with commit_measure:
                    session.commit()
            if (
                workflow == "extract_package"
                and upload_started_at is not None
                and timings is not None
            ):
                start = (
                    upload_started_at
                    if upload_started_at.tzinfo is not None
                    else upload_started_at.replace(tzinfo=UTC)
                )
                timings.record(
                    operation="worker.upload_to_final_commit",
                    run_id=str(revision_id),
                    page_index=None,
                    started_at=start.isoformat(),
                    elapsed_ms=max(0.0, (datetime.now(UTC) - start).total_seconds() * 1000),
                    status="ok",
                )
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
