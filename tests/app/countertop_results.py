"""The countertop results of a package's newest revision, read the way the removed route read them.

`GET …/countertop-results` was the old review screens' route, removed on 2026-10-28. What it served is
still built by live code (`app/review/countertop_results.py`, which the full check's recorded results
read), so the tests of that builder read it through this helper instead: the same revision choice and
the same JSON the route returned. Test-only.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Package, PackageRevision
from app.review.countertop_results import _countertop_results_for_revision


class ResultsResponse:
    """What a test read off the route's response: its status, its JSON and its text."""

    def __init__(self, body: dict[str, Any] | None) -> None:
        self.status_code = 200 if body is not None else 404
        self._body = body if body is not None else {"detail": "Not found"}
        self.text = json.dumps(self._body)

    def json(self) -> dict[str, Any]:
        return self._body


def countertop_results(
    session: Session, project_id: UUID, package_id: UUID, revision_number: int | None = None
) -> ResultsResponse:
    """The newest revision's results (or `revision_number`'s), as the route answered them."""
    # The package is loaded with its revision, as the route did, so its product costs no statement.
    chosen = (
        select(PackageRevision, Package)
        .join(Package, Package.id == PackageRevision.package_id)
        .where(Package.id == package_id, Package.project_id == project_id)
    )
    if revision_number is not None:
        chosen = chosen.where(PackageRevision.revision_number == revision_number)
    found = session.execute(
        chosen.order_by(PackageRevision.revision_number.desc()).limit(1)
    ).first()
    if found is None:
        return ResultsResponse(None)
    out = _countertop_results_for_revision(session, package_id, found[0])
    return ResultsResponse(out.model_dump(mode="json", by_alias=True))
