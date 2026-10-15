"""A cloud PDF drawn for an old sign-off snapshot is byte-for-byte what it was before #1363.

#1363 added a person's marks (Confirmed / Fixed / answers) and NOT CHECKED to the cloud PDF, read
from a new version of the frozen sign-off snapshot. A snapshot frozen before that version carries
none of them, and its cloud PDF must not change by a single byte: a signed document re-made from
the same record has to be the same document.

The digests below were taken from `render_redline` on main *before* #1363 (4ae080b2), with the
inputs built here. If one changes, the drawing of an old signed review changed: that is a bug, not a
golden file to refresh.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Final

from reports.redline import ReportMode, render_redline
from storage.local import LocalStore
from tests.reports.test_redline import _finding, _package, _reference
from verdict.outcomes import Outcome

#: sha256 of the old-format renders on main before #1363.
BEFORE_1363: Final = {
    "upright": "4a00569868fe575c345c7324b805ce72e31a5828e1d1ade7d74dc503156f88d8",
    "rotated": "66415c41a4caf5d93446c421c9ec31f13c893955391cf39015eab355a9bcc68c",
}


def _findings() -> list[object]:
    return [
        _finding("CT-1", Outcome.FAIL, refs=(_reference(),)),
        _finding("CT-2", Outcome.REVIEW_REQUIRED),
        _finding("CT-3", Outcome.NOT_FOUND, refs=(_reference(page=1),)),
        _finding("CT-4", Outcome.NO_APPLICABLE_RULE),
    ]


def _digest(tmp_path: Path, *rotations: int) -> str:
    artifact = render_redline(
        _package(*rotations),
        _findings(),  # type: ignore[arg-type]
        ReportMode.INTERNAL,
        LocalStore(tmp_path),
        countertop_labels=("Page 1 — Countertop row A",),
    )
    return hashlib.sha256(LocalStore(tmp_path).get(artifact.key).read()).hexdigest()


def test_an_old_snapshot_draws_the_same_bytes_as_before_1363_upright(tmp_path: Path) -> None:
    assert _digest(tmp_path, 0, 0) == BEFORE_1363["upright"]


def test_an_old_snapshot_draws_the_same_bytes_as_before_1363_rotated(tmp_path: Path) -> None:
    assert _digest(tmp_path, 90, 270) == BEFORE_1363["rotated"]
