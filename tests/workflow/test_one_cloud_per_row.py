"""A failing countertop row is clouded once, around the row, never once per piece (Set 3 QA).

Synthetic values only. Each piece's reading is one evidence reference, and the redline drew one
cloud per reference, so one FAIL showed as six clouds on a six-piece row.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

from workflow.redline_outputs import one_cloud_per_row


def _ref(version: str, page: int, x0: str, y0: str, x1: str, y1: str) -> str:
    return json.dumps(
        {
            "document_version_id": version,
            "page": page,
            "polygon": [[x0, y0], [x1, y0], [x1, y1], [x0, y1]],
            "space": "stored",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _row(outcome: str, *, on_row: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid4(), outcome=outcome, scope_row_candidate_id=uuid4() if on_row else None
    )


def test_a_row_fail_becomes_one_cloud_around_its_pieces() -> None:
    version = str(uuid4())
    failing = _row("FAIL")
    refs = {
        failing.id: (
            _ref(version, 3, "0.10", "0.50", "0.20", "0.55"),
            _ref(version, 3, "0.20", "0.50", "0.35", "0.56"),
            _ref(version, 3, "0.05", "0.45", "0.40", "0.49"),
        )
    }
    (merged,) = one_cloud_per_row([failing], refs)[failing.id]
    assert json.loads(merged) == {
        "document_version_id": version,
        "page": 3,
        "polygon": [["0.05", "0.45"], ["0.40", "0.45"], ["0.40", "0.56"], ["0.05", "0.56"]],
        "space": "stored",
    }


def test_other_results_and_other_pages_keep_their_own_marks() -> None:
    version = str(uuid4())
    held = _row("REVIEW_REQUIRED")
    package_wide = _row("FAIL", on_row=False)
    two_pages = _row("FAIL")
    pieces = (
        _ref(version, 3, "0.10", "0.50", "0.20", "0.55"),
        _ref(version, 3, "0.20", "0.50", "0.35", "0.56"),
    )
    elsewhere = _ref(version, 4, "0.10", "0.10", "0.20", "0.20")
    refs = {held.id: pieces, package_wide.id: pieces, two_pages.id: (*pieces, elsewhere)}
    out = one_cloud_per_row([held, package_wide, two_pages], refs)
    assert out[held.id] == pieces
    assert out[package_wide.id] == pieces
    assert len(out[two_pages.id]) == 2 and out[two_pages.id][1] == elsewhere
