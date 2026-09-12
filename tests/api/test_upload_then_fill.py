"""Upload two drawings the way the UI does, and let a model fill the form (#594).

**Nothing in the autofill path is wired to a particular package.** That question was asked plainly —
*"what if a new user uploads documents and wants this to be filled with AI?"* — and the honest
answer needed a test rather than a sentence, because the demonstration that existed was a shell
script somebody had to trust.

So this walks the exact endpoint sequence `frontend/main/src/api/upload.ts:createPackage` makes,
in order, on a package created inside this test:

    POST /packages
    POST /packages/{id}/documents      → an upload ticket, per drawing
    PUT  the bytes to the ticket's key
    POST /documents/{id}/confirm       → per drawing
    POST /packages/{id}/extract        → what the modal calls once both are in
    (the worker runs the stage)
    GET  /candidates                   → what the reader found
    POST /measurements/propose         → the assignment, streamed

and then asserts a field comes back filled. No fixture confirmation, no seeded evidence, no
hand-placed association: the only thing given to the test is two PDFs and a published rulebook.

**The drawing carries real dimension geometry**, because that is the part the guard will not do
without: two dimensions drawn end to end with witness lines crossing their ends, which
`extraction/geometry/dimension_lines.py` recognises as a chain and `0044` records. A fixture of bare
strokes reaches `readings_attached: 0` and fills nothing — correctly, and uselessly as a test.

Source: issue #594 · Verified path: `app/api/documents.py` → `workflow/stages.py` →
`app/api/measurements.py`
"""

from __future__ import annotations

import hashlib
import io
import tempfile
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Project
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from tests.extraction.test_annotations import _appearance, _free_text, _pdf, _stamp
from tests.workflow.test_stages import _publish_rulebook
from workflow.association import AssociationSettings
from workflow.review import run_all
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()

#: The reader thresholds this fixture's geometry was drawn for. Stated rather than defaulted, for
#: the reason `text_association` gives: a default here would ship one drawing family's guess as
#: ground truth.
SETTINGS = AssociationSettings(
    line_minimum_pt=Decimal(50),
    glyph_maximum_pt=Decimal(10),
    glyph_gap_pt=Decimal(4),
    proximity_limit=Decimal("0.05"),
    ambiguity_margin=Decimal("0.005"),
    witness_tolerance=Decimal("0.01"),
    minimum_span=Decimal("0.02"),
    straightness=Decimal("0.0005"),
    crossing_margin=Decimal("0.001"),
)

#: Two dimensions drawn end to end at page y=100 — page x=100..200 and x=200..300 — with witness
#: lines crossing at each junction. A cabinet run, in other words: the shape `_chains` groups and
#: the only shape a many-valued field can be filled from.
SHOP_DRAWING = _pdf(
    annotations=[
        _free_text('15"', rect=b"[110 90 190 110]"),
        _free_text('36"', rect=b"[210 90 290 110]"),
        _stamp(appearance_object=8),
    ],
    extra_objects=[
        _appearance(
            b"1 w 150 550 m 250 550 l S\n"
            b"1 w 250 550 m 350 550 l S\n"
            b"1 w 150 520 m 150 580 l S\n"
            b"1 w 250 520 m 250 580 l S\n"
            b"1 w 350 520 m 350 580 l S\n"
        )
    ],
)

#: One dimension on the other sheet, so the package is the pair the modal requires.
ARCH_DRAWING = _pdf(
    annotations=[_free_text('51"', rect=b"[110 90 190 110]"), _stamp(appearance_object=7)],
    extra_objects=[
        _appearance(
            b"1 w 150 550 m 250 550 l S\n"
            b"1 w 150 520 m 150 580 l S\n"
            b"1 w 250 520 m 250 580 l S\n"
        )
    ],
)


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


@pytest.fixture
def store() -> Iterator[LocalStore]:
    with tempfile.TemporaryDirectory() as directory:
        yield LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, store: LocalStore) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_artifact_store] = lambda: store
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


class _StubModel:
    """A model that assigns the chained run to `cabinet_widths`, and records what it was given."""

    def __init__(self) -> None:
        self.contexts: list[Any] = []
        self.config = type("_C", (), {"model_id": "stub-model-v1"})()

    def propose(self, context: Any, *, refused: str | None = None) -> tuple[Any, ...]:
        from workflow.assignment import ProposedAssignment

        self.contexts.append(context)
        run = sorted(
            (r for r in context.readings if r.chain_key is not None),
            key=lambda r: r.order,
        )
        if not run:
            return ()
        field = next((f for f in context.fields if f.many and f.source == run[0].source), None)
        if field is None:
            return ()
        return (
            ProposedAssignment(
                field_key=field.key, candidate_ids=tuple(r.candidate_id for r in run)
            ),
        )


def _upload(client: Any, store: LocalStore, package_id: str, data: bytes, kind: str) -> None:
    """One drawing, through the four calls `uploadDocument` makes, in that order."""
    digest = hashlib.sha256(data).hexdigest()
    ticket = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package_id}/documents",
        json={"kind": kind, "sha256": digest},
    )
    assert ticket.status_code == 201, ticket.text
    issued = ticket.json()

    # The browser PUTs to `upload_url`; the local store's ticket points at a file path, and what the
    # dev route does with it is write the bytes under `storage_key`. Writing them here is the same
    # step, without standing a second HTTP server up inside a test.
    store.put(issued["storage_key"], io.BytesIO(data), content_type="application/pdf")

    confirmed = client.post(
        f"/api/v1/projects/{PROJECT}/documents/{issued['document_id']}/confirm",
        json={"sha256": digest, "page_count": 1},
    )
    assert confirmed.status_code == 201, confirmed.text


def test_a_package_uploaded_through_the_ui_can_be_filled_by_a_model(
    session: Session, store: LocalStore
) -> None:
    """**The whole question, end to end: upload two drawings, get the form filled.**

    Nothing here was arranged for this package. The fields come from the published rulebook, the
    readings from whatever the reader got off the PDFs, the attachment and the chain from the
    geometry, and the assignment from a model that is handed the context and nothing else. The only
    thing this test supplies is two files — which is all a new reviewer supplies.
    """
    session.add(Project(id=PROJECT, name="upload then fill"))
    session.flush()
    _publish_rulebook(session)
    session.commit()

    client = _client(session, store)

    created = client.post(f"/api/v1/projects/{PROJECT}/packages", json={"vendor": "Apex"})
    assert created.status_code == 201, created.text
    package_id = created.json()["id"]

    _upload(client, store, package_id, ARCH_DRAWING, "architectural")
    _upload(client, store, package_id, SHOP_DRAWING, "shop")

    queued = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/extract")
    assert queued.status_code == 202, queued.text
    session.commit()

    # **The request enqueues; it does not read anything, and it cannot.**
    # `tests/api/test_no_heavy_work.py` refuses `app/api/` any import path to the code that opens a
    # drawing, so `/extract` writes an outbox row and something outside the process does the work.
    # Asserted here rather than assumed, because "the button did nothing" and "the worker is not
    # running" are the same screen to a reviewer.
    revision_id = UUID(queued.json()["package_revision_id"])
    from app.models import OutboxEntry
    from app.models.runs import WorkflowRun

    enqueued = list(session.execute(select(OutboxEntry)).scalars())
    assert [entry.workflow for entry in enqueued] == ["extract_package"], enqueued

    # What `scripts/drain_outbox.py` does when it picks that row up.
    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.commit()
    run_all(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        package_revision_id=revision_id,
        workflow_run_id=workflow_run.id,
        stages=DatabaseStages(store=store, dpi=150, association=SETTINGS),
    )
    session.expire_all()

    listed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/candidates")
    assert listed.status_code == 200, listed.text
    values = [item["value"] for item in listed.json()["candidates"]]
    assert "15 in" in values and "36 in" in values, values

    import app.api.measurements as endpoint

    model = _StubModel()
    original = endpoint.configured_assignment_model
    endpoint.configured_assignment_model = lambda _settings: model  # type: ignore[assignment]
    try:
        streamed = client.post(
            f"/api/v1/projects/{PROJECT}/packages/{package_id}/measurements/propose"
        )
    finally:
        endpoint.configured_assignment_model = original  # type: ignore[assignment]

    assert streamed.status_code == 200, streamed.text
    import json as _json

    frames = [
        _json.loads(block.split("data:", 1)[1].strip())
        for block in streamed.text.split("\n\n")
        if block.strip().startswith("data:")
    ]
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["readings_attached"] >= 2, result
    assert result["fields_filled"] >= 1, result["unfilled_reason"]
    filled = result["assignments"][0]
    assert [item["value"] for item in filled["values"]] == ["15 in", "36 in"]
    # The run came back as a run: one chain, in the order the drawing draws it. That is the fact
    # `guard_assignment` checks and the reason a value can be trusted into a position.
    assert len({item["chain_key"] for item in filled["values"]}) == 1
    assert [item["chain_position"] for item in filled["values"]] == [0, 1]


def test_the_worker_fills_the_form_before_a_reviewer_opens_it(
    session: Session, store: LocalStore
) -> None:
    """**What "filled on upload" actually means: no button, no second request, already there.**

    `required-inputs` is what the form loads. A proposal that existed only behind a second call
    would meet a reviewer as an empty form and a thing to press — which is what #591 shipped and
    what this replaces. So the assertion is on the *form contract*: once the drawings have been
    read, the fields a model filled arrive with the form itself.
    """
    session.add(Project(id=PROJECT, name="filled on upload"))
    session.flush()
    _publish_rulebook(session)
    session.commit()

    client = _client(session, store)
    package_id = client.post(
        f"/api/v1/projects/{PROJECT}/packages", json={"vendor": "Apex"}
    ).json()["id"]
    _upload(client, store, package_id, ARCH_DRAWING, "architectural")
    _upload(client, store, package_id, SHOP_DRAWING, "shop")
    queued = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/extract")
    session.commit()

    revision_id = UUID(queued.json()["package_revision_id"])
    from app.models.runs import WorkflowRun

    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.commit()
    run_all(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        package_revision_id=revision_id,
        workflow_run_id=workflow_run.id,
        stages=DatabaseStages(store=store, dpi=150, association=SETTINGS),
    )
    session.expire_all()

    # What `scripts/drain_outbox.py` runs at the end of extraction, with the deployment's model.
    from workflow.propose import propose_for_revision

    summary = propose_for_revision(session, revision_id, _StubModel())  # type: ignore[arg-type]
    session.commit()
    assert summary["filled"], summary

    # And now the form, loaded exactly as the page loads it. No propose call anywhere.
    required = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/required-inputs")
    assert required.status_code == 200, required.text
    proposed = required.json()["proposed_readings"]

    assert proposed, "the form arrived empty after the drawings were read"
    run = next(field for field in proposed if field["many"])
    assert [item["value"] for item in run["values"]] == ["15 in", "36 in"]


def test_a_second_proposal_replaces_the_first_on_the_form(
    session: Session, store: LocalStore
) -> None:
    """Outcome: the newest filed proposal is the one the form shows.

    The table is append-only, so asking again writes a second set of rows beside the first rather
    than editing it. That is the record working as intended — and it only works if the reader picks
    the newest set rather than mixing the two, which would put two answers in one field.
    """
    from workflow.assignment import ProposedAssignment
    from workflow.propose import record_proposal, stored_proposal

    session.add(Project(id=PROJECT, name="second proposal"))
    session.flush()
    _publish_rulebook(session)
    session.commit()

    client = _client(session, store)
    package_id = client.post(
        f"/api/v1/projects/{PROJECT}/packages", json={"vendor": "Apex"}
    ).json()["id"]
    _upload(client, store, package_id, ARCH_DRAWING, "architectural")
    _upload(client, store, package_id, SHOP_DRAWING, "shop")
    queued = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/extract")
    session.commit()
    revision_id = UUID(queued.json()["package_revision_id"])

    from app.models.runs import WorkflowRun

    workflow_run = WorkflowRun(package_revision_id=revision_id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.commit()
    run_all(
        sessionmaker(bind=session.get_bind(), expire_on_commit=False),
        package_revision_id=revision_id,
        workflow_run_id=workflow_run.id,
        stages=DatabaseStages(store=store, dpi=150, association=SETTINGS),
    )
    session.expire_all()

    listed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package_id}/candidates").json()
    first = listed["candidates"][0]["candidate_id"]
    second = listed["candidates"][1]["candidate_id"]

    record_proposal(
        session,
        package_revision_id=revision_id,
        assignments=(ProposedAssignment("SHOP:CT004", (first,)),),
        model_id="first",
    )
    session.commit()
    record_proposal(
        session,
        package_revision_id=revision_id,
        assignments=(ProposedAssignment("SHOP:CT004", (second,)),),
        model_id="second",
    )
    session.commit()

    current = stored_proposal(session, revision_id)
    assert {row.model_id for row in current} == {"second"}
    assert [str(row.candidate_id) for row in current] == [second]
