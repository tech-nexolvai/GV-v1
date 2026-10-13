"""Write one `model_invocations` row per model call, without ever changing the call's answer.

The seed of Sutra's recorder (`docs/decisions/SUTRA_HARNESS.md`). Two callers recorded nothing
before #1221: the layout classifier (`extraction/layout.py`) and the reading assignment
(`workflow/assignment_bedrock.py`). Both now hand each Bedrock call (its raw response or its error)
to this recorder, which turns it into a row through `BedrockConverseInvocationRecorder`, the same
place the findings narration and the reviewer chat are recorded. So outcome, tokens, cost and the
failure reason (#1215) are worked out in one place for all four.

**Two steps: `record` while the model is asked, `write_pending` once the step has its answer.**
`record` only keeps the call in memory and cannot fail, so recording can never turn into a
different answer inside the AI step (a raised error there is read as a failed call: the layout
reader abstains, the assignment fails closed). `write_pending` runs afterwards, in the caller's own
code, as `_BufferedVisionRecorder` does for the vision readers.

**`write_pending` hides only its own failures.** It first flushes the session outside any guard,
so an error that belongs to other pending work raises exactly as it would have without the
recorder. Each row is then written in its own savepoint: if the database refuses *that* row, the
savepoint is rolled back, the session stays usable, and the refusal is logged at warning level. A
lost row is a logged gap in the cost record, never a different result.

**Which transaction the row belongs to is the caller's decision, and it is a real one.**

- In a worker (`commit=False`), the row joins the worker's own transaction, like the rows of the
  vision readers and the findings narration: it commits or rolls back with the work it explains. A
  worker that fails after a call loses that row with the rest of its work. Closing that gap is the
  call ledger's job (`workflow/reading_watch.py`), which Sutra will widen to every caller.
- In an API request (`commit=True`), the row is committed at once, as the reviewer chat's is
  (#1134): `get_session` never commits, so an uncommitted row would vanish with the request though
  the call was paid for. If anything else is pending in the session (`new`, `dirty`, `deleted`),
  it does not commit, and logs that it did not: committing someone else's half-done work is worse
  than a row that is lost with the request. Work already flushed but not committed cannot be seen
  from here; the API caller must not have any.

This module must stay importable from `app/api/` (`tests/api/test_no_heavy_work.py`), so it names
no extraction code; the layout classifier's call is unpacked into keywords by its caller.

Source: issue #1221 · Verification: `tests/workflow/test_model_call_recorder.py`
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from time import monotonic_ns
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.runs.invocations import BedrockConverseInvocationRecorder

__all__ = ["ModelCallRecorder"]

logger = logging.getLogger("gv.workflow.model_call_recorder")


@dataclass(frozen=True, slots=True)
class _Call:
    model_id: str
    prompt_id: str
    template_id: str
    latency_ns: int
    """Measured when the call ended, so writing the row later does not lengthen it."""
    response: Mapping[str, Any] | None
    error: BaseException | None


@dataclass(slots=True)
class ModelCallRecorder:
    """Records Bedrock calls against exactly one scope: a package revision or an extraction run.

    The scope follows ADR-0019: a call made while a page is read belongs to that page's extraction
    run; a review-time call (the assignment, like the chat and the findings narration) belongs to
    the package revision.
    """

    session: Session
    package_revision_id: UUID | None = None
    extraction_run_id: UUID | None = None
    commit: bool = False
    _pending: list[_Call] = field(init=False, default_factory=list)

    def __post_init__(self) -> None:
        if (self.package_revision_id is None) == (self.extraction_run_id is None):
            raise ValueError(
                "a model call recorder needs exactly one scope: a package revision or an "
                "extraction run (ADR-0019)"
            )

    def record(
        self,
        *,
        model_id: str,
        prompt_id: str,
        template_id: str,
        started_ns: int,
        response: Mapping[str, Any] | None,
        error: BaseException | None,
    ) -> None:
        """Keep one call until `write_pending`. Touches no database and cannot fail."""
        latency_ns = max(0, monotonic_ns() - started_ns)
        self._pending.append(_Call(model_id, prompt_id, template_id, latency_ns, response, error))

    def write_pending(self) -> int:
        """Write every kept call as one row each; return how many rows were written.

        Raises only what the session's other pending work raises when flushed.
        """
        calls, self._pending = self._pending, []
        if not calls:
            return 0
        # Looked at only where it matters: before a commit of the recorder's own.
        others_pending = self.commit and bool(
            self.session.new or self.session.dirty or self.session.deleted
        )
        # Outside any guard: a refusal that belongs to other pending work raises as itself.
        self.session.flush()
        written = 0
        for call in calls:
            if call.response is None and call.error is None:
                # Never an `ok` row with zero tokens for a call that did not end normally.
                logger.warning(
                    "model call not recorded: prompt %s ended with neither an answer nor an error",
                    call.prompt_id,
                )
                continue
            try:
                with self.session.begin_nested():
                    BedrockConverseInvocationRecorder(
                        self.session,
                        self.package_revision_id,
                        extraction_run_id=self.extraction_run_id,
                    ).record(
                        model_id=call.model_id,
                        prompt_id=call.prompt_id,
                        template_id=call.template_id,
                        # The recorder measures up to "now"; start it the call's own length ago.
                        started_ns=monotonic_ns() - call.latency_ns,
                        response=call.response,
                        error=call.error,
                    )
                written += 1
            except Exception as record_error:  # noqa: BLE001 - its own row only, after the flush
                # Class name only: a database error can repeat the row's values.
                logger.warning(
                    "model call not recorded: prompt %s, %s",
                    call.prompt_id,
                    type(record_error).__name__,
                )
        if self.commit and written:
            if others_pending:
                logger.warning(
                    "model call rows not committed: other work was pending in the session"
                )
            else:
                self.session.commit()
        return written
