"""What the job values reader found, read back for the review list (#1409). Read-only and light.

Kept apart from `workflow/job_values.py` (which runs the reader and reaches the worker's stages) so
the API can read the results without reaching any heavy or model code
(`tests/api/test_no_heavy_work.py`).

Source: issue #1409. Verification: `tests/workflow/test_job_values_wiring.py`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.job_values import JOB_VALUE_SETTINGS, JobValueReading

__all__ = [
    "JOB_VALUES_EXTRACTOR",
    "READ_JOB_VALUES_WORKFLOW",
    "JobValueSuggestion",
    "latest_job_values",
]

JOB_VALUES_EXTRACTOR: Final = "job-values-reader"
#: The outbox work that reads one revision's job values on request (`read_job_values_again`).
READ_JOB_VALUES_WORKFLOW: Final = "read_job_values"

#: What the form says about where a value came from: code's own find on the drawing's text, or the
#: pair of AI readers (with code's checks when it is auto).
_SOURCE_WORDS: Final = {"code": "drawing", "ai-pair": "two AIs", "ai-reader": "two AIs"}
_SHOWN: Final = ("auto", "suggestion")


@dataclass(frozen=True, slots=True)
class JobValueSuggestion:
    """What the form shows beside one value's field: from the newest reading of the revision."""

    name: str
    value: str
    page: int
    page_id: str | None
    document_version_id: str | None
    polygon: list[list[str]]
    source: str
    auto: bool
    choices: list[dict[str, object]]
    note: str


def latest_job_values(session: Session, package_revision_id: UUID) -> dict[str, JobValueSuggestion]:
    """Each value the newest job values reading found (auto or a suggestion with a value), by name.

    Read-only. A value the newest reading found nothing for, or refused, is left out: the form then
    shows no suggestion for it."""
    newest = session.scalar(
        select(JobValueReading.extraction_run_id)
        .where(JobValueReading.package_revision_id == package_revision_id)
        .order_by(JobValueReading.created_at.desc(), JobValueReading.id.desc())
        .limit(1)
    )
    if newest is None:
        return {}
    out: dict[str, JobValueSuggestion] = {}
    for row in session.scalars(
        select(JobValueReading).where(
            JobValueReading.extraction_run_id == newest,
            JobValueReading.package_revision_id == package_revision_id,
        )
    ):
        shown = row.suggestion
        if row.outcome not in _SHOWN or not shown:
            continue
        value = shown.get("value")
        page = shown.get("page")
        if not isinstance(value, str) or not isinstance(page, int):
            continue
        polygon: Any = shown.get("polygon")
        choices: Any = shown.get("choices")
        out[row.setting] = JobValueSuggestion(
            name=row.setting,
            value=value,
            page=page,
            page_id=_text(shown.get("page_id")),
            document_version_id=_text(shown.get("document_version_id")),
            polygon=polygon if isinstance(polygon, list) else [],
            source=_SOURCE_WORDS.get(str(shown.get("source")), "two AIs"),
            auto=row.outcome == "auto" and shown.get("filled") is True,
            choices=choices if isinstance(choices, list) else [],
            note=str(shown.get("note") or ""),
        )
    order = {name: n for n, name in enumerate(JOB_VALUE_SETTINGS)}
    return dict(sorted(out.items(), key=lambda pair: order.get(pair[0], len(order))))


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
