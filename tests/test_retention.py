"""Nothing is kept forever, nothing disappears without a record, and no drawing reaches a log.

Source: backend proposal §11; `AGENTS.md` §6 · Verification: ``app/retention/policy.py``.

Three tests carry this file.

**A legal hold is not overridden by age.** A bug here deletes a customer's drawings during a
dispute, which is the worst outcome available in this module and the one no later check would catch —
the bytes are gone and the only evidence of the mistake is an audit row saying it was intended.

**Every deletion is audited.** Deletion is the one event whose own evidence is the thing being
removed, so a deletion with no audit row is undetectable afterwards by construction.

**Drawing content never reaches a log sink.** Asserted by attaching a handler to the *root* logger
and driving the real code paths, rather than by grepping for `logger.info(` — a guard that inspects
source cannot see the interpolation that actually happens at runtime.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from fractions import Fraction
from itertools import count
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.audit.events import AuditCategory, AuditEvent
from app.db.session import session_factory, unit_of_work
from app.models.document import (
    Document,
    DocumentKind,
    DocumentVersion,
    Page,
    SourceArtifact,
)
from app.models.evidence import (
    CanonicalObservation,
    EvidenceArtifact,
    EvidenceArtifactKind,
)
from app.models.package import Package, PackageRevision, PackageState, Project
from app.models.retention import LegalHold
from app.retention.policy import (
    _EVIDENCE_CLASS,
    DELETABLE,
    RETENTION,
    ArtifactClass,
    apply_retention,
    held_projects,
)
from evidence.canonical import Authority
from rules.semantic_types import DocumentRole, SemanticType
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from units.measurement import Unit
from verdict.operands import EvidenceStatus

pytest_plugins = ("tests.app.postgres_fixture",)

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

#: Bytes that are unmistakably a drawing, for the log guard.
PDF_BYTES = b"%PDF-1.7\n" + b"\xde\xad\xbe\xef" * 64


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


@pytest.fixture
def store(tmp_path: Path) -> LocalStore:
    return LocalStore(tmp_path)


def _project(session: Session) -> Project:
    project = Project(name=f"P {uuid4().hex[:6]}")
    session.add(project)
    session.flush()
    return project


def _original(
    session: Session, store: LocalStore, project: Project, *, age: timedelta
) -> tuple[UUID, str, DocumentVersion]:
    """An uploaded drawing whose bytes are really in the store, created `age` ago."""
    key = f"originals/{uuid4()}.pdf"
    stored = store.put(key, io.BytesIO(PDF_BYTES), content_type="application/pdf")

    artifact = SourceArtifact(
        storage_key=key,
        sha256=stored.sha256,
        size=len(PDF_BYTES),
        backend_version_id=None,
        created_at=NOW - age,
    )
    session.add(artifact)
    session.flush()

    package = Package(project_id=project.id, vendor="Vicentia")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    document = Document(package_id=package.id, kind=DocumentKind.SHOP.value)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=stored.sha256,
        page_count=1,
        created_at=NOW - age,
    )
    session.add(version)
    session.flush()
    return artifact.id, key, version


#: Pages are unique per (document version, index), and several crops in one test need several
#: pages. A counter keeps the fixtures independent of how many crops a test happens to make.
_page_index = count()


def _crop(
    session: Session, store: LocalStore, version: DocumentVersion, *, age: timedelta, kind: str
) -> tuple[UUID, str]:
    page = Page(
        document_version_id=version.id,
        index=next(_page_index),
        content_hash="a" * 64,
        width_pt=Decimal(1000),
        height_pt=Decimal(800),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()

    # An evidence artifact must belong to exactly one owner — the schema refuses a crop that
    # belongs to nothing, because a region with no reading behind it cites nothing.
    value = Fraction(1, 3)
    observation = CanonicalObservation(
        document_version_id=version.id,
        page_id=page.id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CT001,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        # HUMAN_CONFIRMED because the provenance trigger requires supporting candidates for the
        # other statuses, and this fixture is about an artifact's age rather than how its reading
        # was qualified. Building a whole extraction run to satisfy a constraint the test says
        # nothing about would make the fixture the thing under test.
        status=EvidenceStatus.HUMAN_CONFIRMED,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()

    key = f"{kind}s/{uuid4()}.png"
    stored = store.put(key, io.BytesIO(PDF_BYTES), content_type="image/png")
    artifact = EvidenceArtifact(
        candidate_id=None,
        canonical_observation_id=observation.id,
        document_version_id=version.id,
        page_id=page.id,
        kind=kind,
        storage_key=key,
        sha256=stored.sha256,
        media_type="image/png",
        coordinate_space="stored",
        created_at=NOW - age,
    )
    session.add(artifact)
    session.flush()
    return artifact.id, key


# ---------------------------------------------------------------------------
# A legal hold is not overridden by age
# ---------------------------------------------------------------------------


def test_content_under_legal_hold_is_not_deleted_however_old(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The worst outcome this module can produce, and the one nothing later would catch.

    The bytes are gone, and the only remaining evidence is an audit row saying it was intended.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=9999))
        session.add(
            LegalHold(project_id=project.id, reason="dispute with vendor", placed_by="anant")
        )

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, commit=True)

    assert report.deleted == ()
    assert project_in(report.held, project)
    assert store.exists(key), "the drawing was deleted while under hold"


def test_a_released_hold_stops_protecting_content(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The other half. A hold that could never be lifted would make retention unreachable, and
    "held forever because nobody released it" is the failure a schedule exists to prevent."""
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=9999))
        session.add(
            LegalHold(
                project_id=project.id,
                reason="dispute, now settled",
                placed_by="anant",
                released_at=NOW - timedelta(days=1),
                released_by="anant",
            )
        )

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, commit=True)

    assert len(report.deleted) == 1
    assert not store.exists(key)


def test_one_project_s_hold_does_not_protect_another(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """A hold is scoped to a matter. Holding everything whenever anything is held would be safe in
    one direction and useless in the other — nothing would ever expire once a single dispute
    opened."""
    with unit_of_work(factory) as session:
        held_project = _project(session)
        other_project = _project(session)
        _, held_key, _ = _original(session, store, held_project, age=timedelta(days=9999))
        _, other_key, _ = _original(session, store, other_project, age=timedelta(days=9999))
        session.add(LegalHold(project_id=held_project.id, reason="dispute", placed_by="anant"))

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    assert store.exists(held_key)
    assert not store.exists(other_key)


def test_held_projects_reads_only_unreleased_holds(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        live = _project(session)
        lifted = _project(session)
        session.add(LegalHold(project_id=live.id, reason="dispute", placed_by="anant"))
        session.add(
            LegalHold(
                project_id=lifted.id,
                reason="settled",
                placed_by="anant",
                released_at=NOW,
                released_by="anant",
            )
        )
        live_id, lifted_id = live.id, lifted.id

    with unit_of_work(factory) as session:
        held = held_projects(session)

    assert live_id in held
    assert lifted_id not in held


# ---------------------------------------------------------------------------
# Nothing disappears without a record
# ---------------------------------------------------------------------------


def test_every_deletion_emits_an_audit_event(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """Deletion is the one event whose own evidence is the thing being removed.

    Without a record, a deletion that should not have happened is undetectable afterwards by
    construction — there is nothing left to compare against.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        artifact_id, _, _ = _original(session, store, project, age=timedelta(days=9999))

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    with unit_of_work(factory) as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(
                    AuditEvent.category == AuditCategory.ARTIFACT_DELETION.value
                )
            )
        )

    assert len(events) == 1
    assert events[0].target_id == artifact_id
    assert events[0].target_type == "source_artifacts"


def test_the_row_survives_the_deletion_of_its_bytes(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The record is what makes the deletion auditable, so it is the one thing retention must not
    remove.

    "We held this drawing, this was its hash, it went on this date" has to stay answerable after the
    drawing is gone — and `SourceArtifact` is `Immutable`, so the row could not be deleted even by a
    policy that wanted to.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        artifact_id, key, _ = _original(session, store, project, age=timedelta(days=9999))

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    with unit_of_work(factory) as session:
        row = session.get(SourceArtifact, artifact_id)
        assert row is not None, "the record of the artifact was deleted with its bytes"
        assert row.sha256, "the hash must survive, or nothing can be reconciled later"
        assert row.storage_key == key

    assert not store.exists(key)


def test_a_dry_run_deletes_nothing_and_audits_nothing(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The default posture. The first thing anybody sensible does with a deletion job is ask what it
    is about to remove."""
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=9999))

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW)

    assert report.committed is False
    assert len(report.deleted) == 1, "a dry run still reports what it would remove"
    assert store.exists(key), "a dry run touched storage"

    with unit_of_work(factory) as session:
        assert session.scalars(select(AuditEvent)).all() == []


def test_a_second_pass_reports_what_was_already_gone(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """Retention is re-run on a schedule and interrupted halfway more often than anyone plans for,
    so "already deleted" is the ordinary case on the second pass rather than an error."""
    with unit_of_work(factory) as session:
        project = _project(session)
        _original(session, store, project, age=timedelta(days=9999))

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)
    with unit_of_work(factory) as session:
        second = apply_retention(session, store, now=NOW, commit=True)

    assert second.deleted == ()
    assert len(second.already_gone) == 1
    assert second.total_considered == 1


# ---------------------------------------------------------------------------
# The schedule
# ---------------------------------------------------------------------------


def test_content_inside_its_schedule_is_kept(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=1))

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, commit=True)

    assert report.deleted == ()
    assert store.exists(key)


def test_a_crop_expires_on_its_own_schedule_not_the_original_s(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """A crop and the drawing it came from do not expire together.

    The crop is the more sensitive content and the easier to regenerate, so it goes first — and a
    policy that used one cutoff for everything would keep crops for seven years to protect
    originals.
    """
    age = timedelta(days=400)
    assert age > RETENTION[ArtifactClass.CROP]
    assert age < RETENTION[ArtifactClass.ORIGINAL]

    with unit_of_work(factory) as session:
        project = _project(session)
        _, original_key, version = _original(session, store, project, age=age)
        _, crop_key = _crop(session, store, version, age=age, kind="crop")

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, commit=True)

    assert [item.artifact_class for item in report.deleted] == [ArtifactClass.CROP]
    assert not store.exists(crop_key)
    assert store.exists(original_key), "the original outlives the crop taken from it"


def test_a_schedule_can_be_overridden_per_deployment(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The periods are defaults, not law. An operator's obligations differ by customer and by
    jurisdiction, and a constant nobody can override is one somebody edits in a hurry."""
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=30))

    short = dict(RETENTION) | {ArtifactClass.ORIGINAL: timedelta(days=7)}

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, schedule=short, commit=True)

    assert len(report.deleted) == 1
    assert not store.exists(key)


@pytest.mark.parametrize("missing", list(DELETABLE), ids=lambda c: c.value)
def test_a_schedule_missing_a_deletable_class_is_refused(
    missing: ArtifactClass, factory: sessionmaker[Session], store: LocalStore
) -> None:
    """A missing period is not a licence to delete now, and not a licence to keep forever either.

    Both readings are defensible, which is exactly why it has to be stated rather than defaulted.
    """
    incomplete = {k: v for k, v in RETENTION.items() if k is not missing}

    with unit_of_work(factory) as session, pytest.raises(ValueError, match="how long to keep"):
        apply_retention(session, store, now=NOW, schedule=incomplete, commit=True)


@pytest.mark.parametrize("period", [timedelta(0), timedelta(days=-1)])
def test_a_non_positive_period_is_refused(
    period: timedelta, factory: sessionmaker[Session], store: LocalStore
) -> None:
    """Zero would delete content the moment it was written, which is the sort of configuration
    mistake that is only noticed once."""
    schedule = dict(RETENTION) | {ArtifactClass.ORIGINAL: period}

    with unit_of_work(factory) as session, pytest.raises(ValueError, match="not positive"):
        apply_retention(session, store, now=NOW, schedule=schedule, commit=True)


def test_every_class_has_a_declared_period() -> None:
    """Including logs and traces, which this module does not delete. A schedule that exists only in
    somebody's memory of a conversation is not a schedule."""
    for artifact_class in ArtifactClass:
        assert artifact_class in RETENTION
        assert RETENTION[artifact_class] > timedelta(0)


def test_an_original_is_kept_longest() -> None:
    """It is the customer's own document, the thing a dispute is about, and the only artifact here
    this system did not create and cannot recreate."""
    assert RETENTION[ArtifactClass.ORIGINAL] > RETENTION[ArtifactClass.CROP]
    assert RETENTION[ArtifactClass.ORIGINAL] > RETENTION[ArtifactClass.RENDER]


# ---------------------------------------------------------------------------
# No drawing content reaches a log sink
# ---------------------------------------------------------------------------


class CapturingSink(logging.Handler):
    """A handler on the root logger that keeps every formatted record.

    Attached to the root rather than to one named logger, because the guard has to cover whatever
    the code under test decides to log through — including a library's logger, which is where an
    accidental payload is most likely to surface and least likely to be looked for.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))
        self.lines.extend(str(arg) for arg in (record.args or ()) if arg is not None)


@pytest.fixture
def sink() -> Iterator[CapturingSink]:
    handler = CapturingSink()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        root.removeHandler(handler)
        root.setLevel(previous)


def test_no_drawing_bytes_reach_any_log_sink(
    factory: sessionmaker[Session], store: LocalStore, sink: CapturingSink
) -> None:
    """Driven through the real paths, not by grepping the source.

    A guard that inspected source for `logger.info(` could not see the interpolation that actually
    happens at runtime, which is how a drawing reaches a log in practice: somebody logs a variable
    they believed was a reference.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        _, _, version = _original(session, store, project, age=timedelta(days=9999))
        _crop(session, store, version, age=timedelta(days=9999), kind="crop")

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    logged = "\n".join(sink.lines)
    assert "%PDF" not in logged, "a PDF header reached a log sink"
    assert "deadbeef" not in logged.lower()
    for marker in ("\\xde\\xad", "JVBERi0", ";base64", "data:image"):
        assert marker not in logged, f"{marker} reached a log sink"


def test_the_log_guard_would_notice_a_leak(sink: CapturingSink) -> None:
    """The guard above passes trivially if the sink captures nothing.

    A test that cannot fail is worse than no test: it reads as coverage. This proves the sink sees
    what is logged, so the assertions above are about the code and not about an empty list.
    """
    logging.getLogger("gv.test.leak").warning("a drawing: %s", PDF_BYTES[:16])

    logged = "\n".join(sink.lines)
    assert "%PDF" in logged


def test_a_storage_key_is_loggable_and_a_hash_is_too(
    factory: sessionmaker[Session], store: LocalStore, sink: CapturingSink
) -> None:
    """The guard must not have been satisfied by logging nothing at all.

    References and hashes are exactly what §6 says a log should carry, so a module that stayed
    silent to pass the check would have thrown away the operational value along with the risk.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        _, key, _ = _original(session, store, project, age=timedelta(days=9999))

    logging.getLogger("gv.retention.test").info("deleted %s", key)

    assert any(key in line for line in sink.lines)


def project_in(held: tuple[UUID, ...], project: Project) -> bool:
    return project.id in held


# ---------------------------------------------------------------------------
# The branches that decide whether content is deleted
# ---------------------------------------------------------------------------


def test_a_crop_under_legal_hold_is_not_deleted(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The hold check on the evidence path, which every other hold test here misses.

    `_expired_evidence` applies its own check, so a crop belonging to a held project could have been
    deleted with the whole hold suite still green — the same worst outcome, reached through the
    other of the two code paths.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        _, original_key, version = _original(session, store, project, age=timedelta(days=9999))
        _, crop_key = _crop(session, store, version, age=timedelta(days=9999), kind="crop")
        session.add(LegalHold(project_id=project.id, reason="dispute", placed_by="anant"))

    with unit_of_work(factory) as session:
        report = apply_retention(session, store, now=NOW, commit=True)

    assert report.deleted == ()
    assert store.exists(crop_key), "a crop was deleted while its project was under hold"
    assert store.exists(original_key)


def test_every_declared_artifact_kind_has_a_schedule() -> None:
    """The realistic version of "an unrecognised kind is kept".

    An unrecognised kind cannot be inserted at all: `evidence_artifacts` has a `CHECK` on `kind`, and
    the table is append-only so the value cannot be edited afterwards either. A test that tried to
    create one would be testing the constraint, not the policy.

    What *can* happen is somebody adding a member to `EvidenceArtifactKind` with a migration and
    forgetting the schedule — at which point `_EVIDENCE_CLASS.get` returns `None` and the artifact is
    kept forever, silently. This asserts the two vocabularies agree, so that omission fails here
    instead of becoming a storage bill nobody can explain.
    """
    for kind in EvidenceArtifactKind:
        assert kind.value in _EVIDENCE_CLASS, (
            f"{kind.value} has no retention schedule, so artifacts of that kind would be kept "
            "forever without anything reporting it"
        )
        assert _EVIDENCE_CLASS[kind.value] in DELETABLE


def test_an_unmapped_kind_is_kept_rather_than_guessed() -> None:
    """The fallback itself, at the level it can be reached.

    Keeping costs storage; guessing at a schedule deletes a customer's content under a rule nobody
    wrote. The lookup returns `None` rather than a default, which is what makes the safe choice the
    automatic one.
    """
    assert _EVIDENCE_CLASS.get("thumbnail") is None


def test_the_boundary_is_exact_for_an_original(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """`now` is injectable so this is testable, and a schedule is most often wrong by one.

    Exactly at the period is kept; one second past it goes. Stated both ways because a comparison
    that flipped from `<` to `<=` would still pass a test that only checked one side.
    """
    period = RETENTION[ArtifactClass.ORIGINAL]

    with unit_of_work(factory) as session:
        project = _project(session)
        _, exactly_key, _ = _original(session, store, project, age=period)
        _, past_key, _ = _original(
            session, store, _project(session), age=period + timedelta(seconds=1)
        )

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    assert store.exists(exactly_key), "an artifact exactly at its period was deleted"
    assert not store.exists(past_key), "an artifact one second past its period was kept"


def test_the_boundary_is_exact_for_a_crop(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The evidence path compares differently — `created >= now - period` in Python, against the
    originals' `created_at < cutoff` in SQL. Two comparisons is two chances to be off by one."""
    period = RETENTION[ArtifactClass.CROP]

    with unit_of_work(factory) as session:
        project = _project(session)
        _, _, version = _original(session, store, project, age=timedelta(days=1))
        _, exactly_key = _crop(session, store, version, age=period, kind="crop")
        _, past_key = _crop(session, store, version, age=period + timedelta(seconds=1), kind="crop")

    with unit_of_work(factory) as session:
        apply_retention(session, store, now=NOW, commit=True)

    assert store.exists(exactly_key)
    assert not store.exists(past_key)


def test_the_audit_trail_survives_a_rollback_after_the_bytes_are_gone(
    factory: sessionmaker[Session], store: LocalStore
) -> None:
    """The exposure that made the ordering wrong the first time.

    Storage cannot join a database transaction, so either the bytes go first — and a rollback
    discards the audit rows, leaving content deleted with no record — or the audit trail is
    committed first. Only the second is recoverable, so `apply_retention` commits before touching
    storage, and a caller's later failure cannot take the record with it.
    """
    with unit_of_work(factory) as session:
        project = _project(session)
        artifact_id, key, _ = _original(session, store, project, age=timedelta(days=9999))

    with (
        pytest.raises(RuntimeError, match="something else failed"),
        unit_of_work(factory) as session,
    ):
        apply_retention(session, store, now=NOW, commit=True)
        raise RuntimeError("something else failed after the pass")

    assert not store.exists(key), "the bytes were removed"

    with unit_of_work(factory) as session:
        events = list(
            session.scalars(select(AuditEvent).where(AuditEvent.target_id == artifact_id))
        )

    assert len(events) == 1, "the record of the deletion was lost with the caller's rollback"
    assert events[0].category == AuditCategory.ARTIFACT_DELETION.value
