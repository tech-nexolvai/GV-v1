"""The draft cloud PDF before sign-off (#1340): queued, drawn by the worker, never the signed file.

Every value here is synthetic.
"""

from __future__ import annotations

import contextlib
import io
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.models import CheckRun, DocumentVersion, Finding, OutboxEntry, OutputArtifact, Page
from app.models.package import PackageRevision
from app.models.rules import RuleDefinition, RuleSnapshot
from app.models.signed_exports import ApprovalExportBundle
from app.review.signed_exports import request_signed_exports
from storage.local import LocalStore
from tests.api.test_signed_exports import _legacy
from tests.api.test_v1_loop import PROJECT, _uploaded
from tests.api.test_v1_loop import client as client_fixture
from tests.api.test_v1_loop import session as session_fixture
from tests.api.test_v1_loop import store as store_fixture
from workflow.draft_cloud import DRAFT_CLOUD_WORKFLOW, DraftCloudRefused
from workflow.outbox import OutboxDispatchError, dispatch_committed

session = session_fixture
store = store_fixture
client = client_fixture

BANNER = "DRAFT – not signed off"
WAITING = (
    "Waiting for the countertop depth of this countertop. This check runs for it from the depth "
    "and sink reader's readings only once this countertop is confirmed and its run decided on "
    "Measurements."
)


def _result(db: Session, revision: PackageRevision, outcome: str, reason: str) -> Finding:
    """One live result with its own rule, snapshot and check run."""
    import hashlib

    definition = RuleDefinition(rule_id=f"CT-TEST-{uuid4().hex[:6]}")
    db.add(definition)
    db.flush()
    body = f'{{"id":"{definition.rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type="internal",
        unconfirmed_tolerance_count=0,
    )
    db.add(snapshot)
    db.flush()
    run = CheckRun(
        package_revision_id=revision.id, rule_snapshot_id=snapshot.id, engine_version="verdict-1"
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=outcome,
        severity="FLAG",
        trace={},
        parameter_set_versions={},
        reason=reason,
    )
    db.add(finding)
    db.flush()
    return finding


def _page(db: Session) -> None:
    version = db.scalars(select(DocumentVersion)).one()
    db.add(
        Page(
            document_version_id=version.id,
            index=0,
            content_hash="a" * 64,
            width_pt=200,
            height_pt=100,
            rotation=0,
            has_vector_text=True,
            media_box=["0", "0", "200", "100"],
            crop_box=["0", "0", "200", "100"],
        )
    )


def _checked(db: Session, storage: LocalStore) -> tuple[UUID, PackageRevision]:
    """A checked package: one wrong result, one waiting, one for a person, one passed."""
    package_id, revision, _ = _uploaded(db, storage)
    revision.state = "AWAITING_REVIEW"
    _page(db)
    _result(db, revision, "FAIL", "Vendor wrote 72 in; it should be 66 in.")
    _result(db, revision, "REVIEW_REQUIRED", WAITING)
    _result(db, revision, "REVIEW_REQUIRED", "The two AIs did not agree on this line.")
    _result(db, revision, "PASS", "55 in == 55 in")
    db.commit()
    return package_id, revision


def _drain(db: Session, storage: LocalStore, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the local worker does with the queued request: `sutradhar.consume`, its own session."""
    from workflow import sutradhar

    monkeypatch.setenv("GV_DEV_STORAGE", str(storage.root))
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)

    def start(*, workflow: str, payload: Any, idempotency_key: str) -> None:
        with factory() as own:
            sutradhar.consume(
                own, workflow=workflow, payload=payload, idempotency_key=idempotency_key
            )
            own.commit()

    # A refusal is parked with its reason and re-raised once; the tests read the parked reason.
    with contextlib.suppress(OutboxDispatchError):
        dispatch_committed(factory, start)
    db.expire_all()


def _pages_text(content: bytes) -> list[str]:
    return [
        " ".join((page.extract_text() or "").split())
        for page in PdfReader(io.BytesIO(content)).pages
    ]


def test_asking_for_a_draft_with_no_results_is_refused(
    session: Session, store: LocalStore, client: Any
) -> None:
    package_id, _, _ = _uploaded(session, store)
    answer = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/draft-cloud")
    assert answer.status_code == 409, answer.text
    assert "Run the checks first" in answer.json()["message"]


def test_a_draft_is_queued_once_drawn_by_the_worker_and_bannered_on_every_page(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, _ = _checked(session, store)
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    assert client.get(base + "/draft-cloud").json()["status"] == "not_requested"
    assert client.get(base + "/draft-cloud.pdf").status_code == 409

    first = client.post(base + "/draft-cloud")
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "preparing"
    second = client.post(base + "/draft-cloud")
    assert second.json()["request_id"] == first.json()["request_id"]
    assert (
        session.scalar(
            select(func.count(OutboxEntry.id)).where(OutboxEntry.workflow == DRAFT_CLOUD_WORKFLOW)
        )
        == 1
    )

    _drain(session, store, monkeypatch)
    status = client.get(base + "/draft-cloud").json()
    assert status["status"] == "ready", status
    assert status["findings"] == 4 and status["bytes"] > 0

    pdf = client.get(base + "/draft-cloud.pdf")
    assert pdf.status_code == 200 and pdf.headers["content-type"] == "application/pdf"
    pages = _pages_text(pdf.content)
    assert all(BANNER in text for text in pages), pages
    front = pages[0]
    assert "Will be clouded as wrong (1)" in front
    assert "Vendor wrote 72 in; it should be 66 in." in front
    assert "Needs a person (1)" in front and "The two AIs did not agree on this line." in front
    assert "NOT CHECKED (1)" in front and "NOT CHECKED: 1 check(s): Waiting for" in front
    # The waiting check is printed NOT CHECKED on the drawing's own summary too, never as judged.
    rest = " ".join(text.replace(BANNER, "") for text in pages[1:]).split()
    assert "NOT CHECKED: Waiting for the countertop depth" in " ".join(rest)
    kinds = set(session.scalars(select(OutputArtifact.kind)))
    assert kinds == {"draft_redline"}


def test_the_same_request_delivered_twice_records_one_draft(
    session: Session, store: LocalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workflow.draft_cloud import request_draft_cloud
    from workflow.draft_cloud_render import render_draft_cloud

    _, revision = _checked(session, store)
    request = request_draft_cloud(session, revision.id, requested_by="ana@example.com")
    session.commit()
    first = render_draft_cloud(session, store, revision.id, request, requested_by="ana@example.com")
    again = render_draft_cloud(session, store, revision.id, request, requested_by="ana@example.com")
    assert again == {"draft_id": first["draft_id"], "already_recorded": True}
    assert first["failures"] == 1 and first["needs_a_person"] == 1 and first["not_checked"] == 1


def test_a_draft_that_cannot_be_drawn_is_failed_with_its_reason_never_silent(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, revision, _ = _uploaded(session, store)  # no page transforms recorded
    _result(session, revision, "FAIL", "Wrong.")
    session.commit()
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    assert client.post(base + "/draft-cloud").status_code == 202
    _drain(session, store, monkeypatch)
    status = client.get(base + "/draft-cloud").json()
    assert status["status"] == "failed"
    assert status["reason"].startswith("The draft could not be drawn")
    refused = client.get(base + "/draft-cloud.pdf")
    assert refused.status_code == 409 and refused.json()["message"] == status["reason"]
    # Asking again queues a new request rather than hiding behind the parked one.
    again = client.post(base + "/draft-cloud").json()
    assert again["status"] == "preparing" and again["request_id"] != status["request_id"]


def test_the_worker_turns_any_failure_into_a_sentence() -> None:
    from workflow.draft_cloud_render import consume_draft_request

    with pytest.raises(DraftCloudRefused) as refused:
        consume_draft_request(None, None, {"package_revision_id": "not-a-uuid"}, str(uuid4()))  # type: ignore[arg-type]
    assert refused.value.plain_reason.startswith("The draft was not made: ValueError")


def test_a_draft_is_never_served_as_the_signed_cloud_pdf(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a bundle naming a draft as its redline is refused: signed downloads serve `redline` only."""
    from workflow.draft_cloud_render import render_draft_cloud

    package_id, approval, findings = _legacy(session, store)
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    request = uuid4()
    drawn = render_draft_cloud(
        session, store, approval.package_revision_id, request, requested_by="ana@example.com"
    )
    draft = session.get(OutputArtifact, UUID(str(drawn["draft_id"])))
    assert draft is not None and draft.kind == "draft_redline"
    snapshot = request_signed_exports(session, approval.id)

    def artifact(kind: str) -> OutputArtifact:
        saved = store.put(
            f"signed-test/{kind}",
            io.BytesIO(b"%PDF " + kind.encode()),
            content_type="application/pdf",
        )
        row = OutputArtifact(
            package_revision_id=approval.package_revision_id,
            kind=kind,
            storage_key=saved.key,
            sha256=saved.sha256,
            media_type="application/pdf",
            findings=len(findings),
        )
        session.add(row)
        session.flush()
        return row

    # A draft that happens to cover the same number of results, wrongly named as the redline.
    named = OutputArtifact(
        package_revision_id=approval.package_revision_id,
        kind="draft_redline",
        storage_key=draft.storage_key + ".copy",
        sha256=draft.sha256,
        media_type="application/pdf",
        findings=len(findings),
    )
    session.add(named)
    session.flush()
    session.add(
        ApprovalExportBundle(
            snapshot_id=snapshot.id,
            package_revision_id=approval.package_revision_id,
            pdf_id=artifact("findings_pdf").id,
            workbook_id=artifact("findings_workbook").id,
            redline_id=named.id,
        )
    )
    session.commit()
    for endpoint in ("redline.pdf", "report.pdf", "report"):
        refused = client.get(base + "/" + endpoint)
        assert refused.status_code == 409, (endpoint, refused.text)
        assert "incomplete or belongs to a different revision" in refused.json()["message"]


def test_before_sign_off_the_signed_download_refuses_even_with_a_draft_ready(
    session: Session, store: LocalStore, client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    package_id, _ = _checked(session, store)
    base = f"/api/v1/projects/{PROJECT}/packages/{package_id}"
    client.post(base + "/draft-cloud")
    _drain(session, store, monkeypatch)
    assert client.get(base + "/draft-cloud").json()["status"] == "ready"
    assert client.get(base + "/redline.pdf").status_code == 409


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_the_banner_sits_inside_the_shown_page_at_any_rotation(rotation: int) -> None:
    from workflow.draft_cloud_render import _user_point

    box = (Decimal(10), Decimal(20), Decimal(210), Decimal(120))
    shown = (Decimal(200), Decimal(100)) if rotation in (0, 180) else (Decimal(100), Decimal(200))
    for u, v in ((0, 0), (shown[0], shown[1]), (shown[0] / 2, shown[1])):
        x, y = _user_point(rotation, box, Decimal(u), Decimal(v))
        assert box[0] <= x <= box[2] and box[1] <= y <= box[3]


def test_a_rotated_drawing_page_is_bannered() -> None:
    from workflow.draft_cloud_render import with_banner

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=200)
    writer.pages[0].rotate(90)
    drawing = io.BytesIO()
    writer.write(drawing)
    result = with_banner(_blank_pdf(), drawing.getvalue())
    pages = PdfReader(io.BytesIO(result)).pages
    assert pages[1].rotation == 90
    assert all(BANNER in (page.extract_text() or "") for page in pages)


def _blank_pdf() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def test_a_waiting_check_a_person_settled_is_still_printed_not_checked(
    session: Session, store: LocalStore
) -> None:
    """Settling a check that never ran (decision 1) does not make it checked."""
    from app.models.review import ReviewActionKind
    from app.review.session import open_session, record_action
    from workflow.draft_cloud_render import _live_rows, open_items

    _, revision = _checked(session, store)
    waiting = session.scalars(select(Finding).where(Finding.reason == WAITING)).one()
    review = open_session(session, package_revision_id=revision.id, reviewer="ana@example.com")
    record_action(
        session,
        review_session_id=review.id,
        finding_id=waiting.id,
        action=ReviewActionKind.DISMISS,
        actor="ana@example.com",
        note="Not checked: the depth reader is off",
    )
    session.commit()
    items = open_items(session, revision.id, _live_rows(session, revision.id))
    assert items.not_checked_ids == {waiting.id}
    assert items.not_checked[0].reason.endswith("(A person settled it as not checked.)")
    assert len(items.failures) == 1 and len(items.needs_a_person) == 1


def test_the_not_checked_prefix_is_the_one_several_countertops_writes() -> None:
    """NOT CHECKED rests on the reason's first words; it cannot quietly move away from them."""
    from workflow import draft_cloud_render
    from workflow.several_countertops import WAITING, WAITS_FOR, waiting_reason

    assert WAITING.startswith(draft_cloud_render._WAITING)
    for rule_id in WAITS_FOR:
        for reader in (True, False):
            assert waiting_reason(rule_id, depth_reader=reader).startswith(
                draft_cloud_render._WAITING
            )


def test_identical_not_checked_reasons_are_said_once_with_every_result() -> None:
    from workflow.draft_cloud_render import OpenItem, grouped_by_reason

    items = [
        OpenItem("Countertop row on page 2", WAITING),
        OpenItem("Countertop row on page 3", "No rule covers this."),
        OpenItem("Countertop row on page 4", WAITING),
    ]
    assert grouped_by_reason(items) == [
        (WAITING, ("Countertop row on page 2", "Countertop row on page 4")),
        ("No rule covers this.", ("Countertop row on page 3",)),
    ]


def test_a_draft_is_refused_once_the_review_is_signed_off(
    session: Session, store: LocalStore, client: Any
) -> None:
    """A draft is for before sign-off; the signed cloud PDF stands for a signed review."""
    package_id, _, _ = _legacy(session, store)
    answer = client.post(f"/api/v1/projects/{PROJECT}/packages/{package_id}/draft-cloud")
    assert answer.status_code == 409, answer.text
    assert "signed off" in answer.json()["message"]
    assert (
        session.scalar(
            select(func.count(OutboxEntry.id)).where(OutboxEntry.workflow == DRAFT_CLOUD_WORKFLOW)
        )
        == 0
    )
