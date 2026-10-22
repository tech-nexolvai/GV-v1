"""Each drawing set's newest full check, for the Documents list and the sidebar.

The results now live in the chat's one-click full check (`assistant_chats` /
`assistant_chat_messages`, mode `full_check`), not in the old check runs and findings tables. The
package list (`GET .../packages`, the sidebar) and the Documents summary (`GET
.../packages-summary`) both add, per set on the page:

- `full_check`: the newest full check's review result, counts, when it ran and its chat
  (`chat_store.latest_full_checks`: a fixed number of queries for the whole page, never one per set);
- `checking`: a full check of the set runs now on this server (the chat registry, no query);
- `drawings`: where its set pack is (`chat_service.set_pack_state`: read-only file reads).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from sqlalchemy.orm import Session

from app.review.assistant.chat_service import set_pack_state
from app.review.assistant.chat_store import latest_full_checks
from app.schemas.packages import DrawingsState, LatestFullCheckOut

__all__ = ["SetCheckStatus", "set_check_statuses"]

_DRAWINGS: dict[str, DrawingsState] = {"building": "reading", "ready": "read", "failed": "failed"}


@dataclass(frozen=True, slots=True)
class SetCheckStatus:
    full_check: LatestFullCheckOut | None
    checking: bool
    drawings: DrawingsState | None


def _running_full_checks(app: Any) -> set[UUID]:
    """The sets with a full check running now (a message with a live card). The chat routes own
    the registry; they import this module's callers, so it is reached at call time."""
    try:
        chats = import_module("app.api.assistant_chats")
        registry = chats._registry_of(app)
    except Exception:  # noqa: BLE001 - no registry: nothing runs here
        return set()
    return {run.package_id for run in registry.runs() if run.live is not None}


def set_check_statuses(
    session: Session, app: Any, package_ids: Sequence[UUID]
) -> dict[UUID, SetCheckStatus]:
    """Every set's newest full check, whether one runs now and where its drawings are."""
    if not package_ids:
        return {}
    latest = latest_full_checks(session, package_ids)
    running = _running_full_checks(app)
    settings = getattr(getattr(app, "state", None), "settings", None)
    root = getattr(settings, "assistant_chat_root", None)
    chat_root = None if root is None else Path(cast(Path, root)).expanduser()
    out: dict[UUID, SetCheckStatus] = {}
    for package_id in package_ids:
        found = latest.get(package_id)
        state = None
        if chat_root is not None:
            try:
                state = set_pack_state(
                    chat_root, package_id, () if found is None else (found["chat_id"],)
                )
            except Exception:  # noqa: BLE001 - the list never fails over a set's files
                state = None
        out[package_id] = SetCheckStatus(
            full_check=None if found is None else LatestFullCheckOut.model_validate(found),
            checking=package_id in running,
            drawings=None if state is None else _DRAWINGS.get(state),
        )
    return out
