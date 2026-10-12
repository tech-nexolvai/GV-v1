"""Which revisions are closed to change, and the plain sentence for each (#1182).

A revision whose review is finished (`APPROVED`, `CHANGES_REQUESTED`) or that has left the line
(`SUPERSEDED`, `CANCELLED`) never takes another input, decision or check run: the lifecycle table has
no way back from any of them to `RUNNING_CHECKS`, and a signed review is the record GV handed over.
The API refuses such writes with these sentences (`app/api/signed_off.py`) and the local worker drops
a check run queued for such a revision with the same reason (`scripts/drain_outbox.py`).

Here rather than in the API so the worker can read it without importing the web layer.
"""

from __future__ import annotations

from typing import Final

from app.models.package import PackageState

#: The answer for each closed state, in the words the screen shows.
CLOSED_DETAIL: Final[dict[str, str]] = {
    PackageState.APPROVED.value: "This review is signed off. Start a new revision to change it.",
    PackageState.CHANGES_REQUESTED.value: (
        "Changes were requested on this review, so it is closed. Start a new revision to change it."
    ),
    PackageState.SUPERSEDED.value: (
        "A newer revision replaced this one. Make the change on the newest revision."
    ),
    PackageState.CANCELLED.value: "This review was cancelled, so nothing on it can change.",
}

CLOSED_STATES: Final[frozenset[str]] = frozenset(CLOSED_DETAIL)


def closed_reason(state: str) -> str | None:
    """The plain sentence for a closed revision, or `None` while it is open."""
    return CLOSED_DETAIL.get(state)
