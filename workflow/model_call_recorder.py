"""Write one `model_invocations` row per model call, without ever changing the call's answer.

The seed of Sutra's recorder (`docs/decisions/SUTRA_HARNESS.md`). Two callers recorded nothing
before #1221: the layout classifier (`extraction/layout.py`) and the reading assignment
(`workflow/assignment_bedrock.py`). Both now hand each Bedrock call (its raw response or its error)
to this recorder, which turns it into a row through `BedrockConverseInvocationRecorder`, the same
place the findings narration and the reviewer chat are recorded. So outcome, tokens, cost and the
failure reason (#1215) are worked out in one place for all four.

**Recording never changes the answer.** The row is written in a savepoint. If the database refuses
it, the savepoint is rolled back, the session stays usable, and the refusal is logged at warning
level rather than raised: an answer, an abstention or a fail-closed result is the same with or
without a working record. A lost row is a logged gap in the cost record, never a different result.

**Which transaction the row belongs to is the caller's decision, and it is a real one.**

- In a worker (`commit=False`), the row joins the worker's own transaction, like the rows of the
  vision readers and the findings narration: it commits or rolls back with the work it explains. A
  worker that fails after a call loses that row with the rest of its work. Closing that gap is the
  call ledger's job (`workflow/reading_watch.py`), which Sutra will widen to every caller.
- In an API request (`commit=True`), the row is committed at once, as the reviewer chat's is
  (#1134): `get_session` never commits, so an uncommitted row would vanish with the request though
  the call was paid for. Only use it where nothing else is pending in the session.

This module must stay importable from `app/api/` (`tests/api/test_no_heavy_work.py`), so it names
no extraction code; the layout classifier's call is unpacked into keywords by its caller.

Source: issue #1221 · Verification: `tests/workflow/test_model_call_recorder.py`
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.runs.invocations import BedrockConverseInvocationRecorder

__all__ = ["ModelCallRecorder"]

logger = logging.getLogger("gv.workflow.model_call_recorder")


@dataclass(frozen=True, slots=True)
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
        """Write one row for one call. Never raises: a refused row is logged and the answer kept."""
        try:
            with self.session.begin_nested():
                BedrockConverseInvocationRecorder(
                    self.session,
                    self.package_revision_id,
                    extraction_run_id=self.extraction_run_id,
                ).record(
                    model_id=model_id,
                    prompt_id=prompt_id,
                    template_id=template_id,
                    started_ns=started_ns,
                    response=response,
                    error=error,
                )
            if self.commit:
                self.session.commit()
        except Exception as record_error:  # noqa: BLE001 - recording must not change the answer
            # Class name only: a database error can repeat the row's values.
            logger.warning(
                "model call not recorded: prompt %s, %s",
                prompt_id,
                type(record_error).__name__,
            )
            if self.commit:
                try:
                    self.session.rollback()
                except Exception:  # noqa: BLE001 - the session is already being given up on
                    logger.warning("model call recorder could not roll back its session")
