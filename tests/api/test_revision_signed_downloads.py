"""Revision 1's signed files stay downloadable after revision 2 starts (#1180).

The signed files are the record a dispute asks for: "what did you sign off in March?". Starting a
revision closes revision 1 (`SUPERSEDED`), so the routes read "was it signed off" from its approval,
not from its state, and take `?revision=N`. Synthetic data only.
"""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.orm import Session

from storage.local import LocalStore
from tests.api.test_signed_exports import _legacy
from tests.api.test_v1_loop import PROJECT
from tests.api.test_v1_loop import client as client_fixture
from tests.api.test_v1_loop import session as session_fixture
from tests.api.test_v1_loop import store as store_fixture
from workflow.signed_outputs import generate_signed_outputs

session = session_fixture
store = store_fixture
client = client_fixture


def test_revision_ones_signed_files_survive_revision_two(
    session: Session, store: LocalStore, client: Any
) -> None:
    package_id, approval, _findings = _legacy(session, store)
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    assert client.post(base + "/signed-exports").status_code == 202
    generate_signed_outputs(session, store, approval.id)
    session.commit()
    signed = {name: client.get(f"{base}/{name}").content for name in ("report", "report.pdf")}

    started = client.post(
        base + "/revisions",
        json={
            "expected_revision": 1,
            "shop": {"sha256": hashlib.sha256(b"%PDF-1.7 synthetic re-issue\n").hexdigest()},
        },
    )
    assert started.status_code == 201, started.text

    # Revision 2 is not signed off: the newest revision's files are refused, plainly.
    assert client.get(base + "/report.pdf").status_code == 409
    assert client.get(base + "/signed-exports").status_code == 409
    # Revision 1's are exactly the files that were signed.
    assert client.get(base + "/signed-exports?revision=1").json()["status"] == "ready"
    for name in ("report", "report.pdf"):
        response = client.get(f"{base}/{name}?revision=1")
        assert response.status_code == 200, response.text
        assert response.content == signed[name]
        assert "-r1." in response.headers["content-disposition"]
    assert client.get(base + "/redline.pdf?revision=1").status_code == 200
    assert client.get(base + "/report.pdf?revision=9").status_code == 404
