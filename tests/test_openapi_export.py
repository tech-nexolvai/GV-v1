"""The schema the frontend generates its types from.

`frontend/main/src/api/schema.d.ts` is generated from `scripts/export_openapi.py` and never
hand-edited, so a field renamed on the server breaks the frontend build rather than surfacing as a
blank panel during a review. That only holds while the export keeps working, which is what these
assert.

Verification for: `scripts/export_openapi.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from export_openapi import schema


def test_the_schema_builds_without_a_database() -> None:
    """CI has no server and the export must not need one. `create_app` builds the schema from the
    route table alone, which is also what stops it going stale against whatever is deployed."""
    document = schema()

    assert document["openapi"].startswith("3.")
    assert document["paths"], "no paths — the frontend would generate an empty client"


def test_every_route_the_review_ui_needs_is_published() -> None:
    """Named explicitly rather than counted. A count passes while the wrong routes are present, and
    these are what the review screens are built on (the old findings list and finding chain went with
    the old review screens on 2026-10-28; the chat's own routes are checked with the chat)."""
    paths = schema()["paths"]

    for required in (
        "/api/v1/projects/{project_id}/packages",
        "/api/v1/projects/{project_id}/packages/{package_id}",
        "/api/v1/packages/{package_id}/chats",
        "/api/v1/chats/{chat_id}",
    ):
        assert required in paths, f"{required} is missing from the published schema"
