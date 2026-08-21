"""Supersede: the prior review survives untouched (#211, C3.3).

`docs/DESIGN_PLATFORM.md` §5: *"A new document revision never overwrites an old version; it supersedes
the prior package revision and starts a new workflow run."* A revision is not an edit, and this file is
about the "not an edit" half — what the prior revision looks like after being superseded, which is
**identical**.

Everything runs against a migrated PostgreSQL rather than `create_all`, because the properties are
properties of the database: the freeze trigger, the composite foreign keys, the one-version rule. #313
was the lesson — `create_all` builds from the models, so it compares the models with themselves.

Source: backend proposal §9.1 · Design: `docs/DESIGN_PLATFORM.md` §5, ADR-0018 · Verification: this file
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, func, select, update
from sqlalchemy.exc import DatabaseError
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.lifecycle.events import history
from app.lifecycle.supersede import (
    PACKAGE_WORKFLOW,
    NoNewVersions,
    NothingToSupersede,
    TwoVersionsOfOneDocument,
    VersionFromAnotherPackage,
    revision_chain,
    supersede,
    superseded_by,
)
from app.models import (
    Document,
    DocumentKind,
    DocumentVersion,
    OutboxEntry,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    PackageStateEvent,
    Project,
    SourceArtifact,
)

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]
ACTOR = "anant"


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    """A session factory against a database migrated to head."""
    config = Config("alembic.ini")
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


# ---------------------------------------------------------------------------
# A package with drawings in it
# ---------------------------------------------------------------------------


def _package(session: Session, name: str = "supersede") -> Package:
    project = Project(name=f"{name} {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    return package


def _version(session: Session, document: Document, digest: str) -> DocumentVersion:
    artifact = SourceArtifact(
        storage_key=f"originals/{uuid4().hex}.pdf",
        sha256=digest,
        size=4096,
        backend_version_id=None,
    )
    session.add(artifact)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=2
    )
    session.add(version)
    session.flush()
    return version


def _digest(seed: str) -> str:
    """A well-formed sha256 the constraint accepts, distinct per seed.

    Hashed rather than built by repeating the seed: `source_artifacts` carries
    `sha256 ~ '^[0-9a-f]{64}$'`, and repeating a letter outside that range produces a string that
    looks like a digest and is refused. `_digest("g")` did exactly that on the first run.
    """
    return hashlib.sha256(seed.encode()).hexdigest()


def _assembled_package(
    session: Session, *, drawings: int = 3, name: str = "supersede"
) -> tuple[Package, PackageRevision, dict[UUID, UUID]]:
    """A package whose revision 1 includes `drawings` documents, each at its first version.

    Returns the package, revision 1, and `{document_id: version_id}` for what it includes.
    """
    from app.lifecycle.states import begin

    package = _package(session, name)
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    begin(session, revision.id, actor=ACTOR)

    composition: dict[UUID, UUID] = {}
    for index in range(drawings):
        document = Document(package_id=package.id, kind=DocumentKind.SHOP)
        session.add(document)
        session.flush()
        version = _version(session, document, _digest(f"{chr(97 + index)}"))
        session.add(
            PackageRevisionDocument(
                package_revision_id=revision.id,
                package_id=package.id,
                document_id=document.id,
                document_version_id=version.id,
            )
        )
        composition[document.id] = version.id
    session.flush()
    return package, revision, composition


def _document(session: Session, document_id: UUID) -> Document:
    """The document, or fail the test loudly.

    `session.get` returns `Document | None`, and passing that straight into a helper typed `Document`
    is an error `mypy` would report if it checked tests — CI runs it over `app/` and `workflow/` only,
    so this went unnoticed until CodeRabbit read the diff. Resolved once here rather than asserted at
    eleven call sites.
    """
    document = session.get(Document, document_id)
    assert document is not None, f"no document {document_id}"
    return document


def _composition(session: Session, package_revision_id: UUID) -> dict[UUID, UUID]:
    rows = session.execute(
        select(
            PackageRevisionDocument.document_id, PackageRevisionDocument.document_version_id
        ).where(PackageRevisionDocument.package_revision_id == package_revision_id)
    ).all()
    return {document_id: version_id for document_id, version_id in rows}


# ---------------------------------------------------------------------------
# Every drawing is carried forward
# ---------------------------------------------------------------------------


def test_the_new_revision_is_a_complete_package_not_a_diff(factory: sessionmaker[Session]) -> None:
    """Input: one changed drawing of three. Outcome: all three. Why: a partial set fails silently.

    **The property this story exists for.** A countertop width check reads the cabinet elevation as
    well as the countertop sheet, so a revision holding only the changed drawing runs its checks
    against a partial set — and the absent drawings produce no failures, which reads as no problems
    (`AGENTS.md` §2.2).
    """
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=3)
        changed_document = next(iter(before))
        reissued = _version(session, _document(session, changed_document), _digest("f"))

        revision_two = supersede(
            session,
            package_id=package.id,
            new_document_versions=[reissued.id],
            actor=ACTOR,
        )
        session.flush()

        after = _composition(session, revision_two.id)
        assert set(after) == set(before), "every drawing carried forward, changed or not"
        assert after[changed_document] == reissued.id, "the re-issued drawing is at its new version"
        for document_id, version_id in before.items():
            if document_id != changed_document:
                assert after[document_id] == version_id, "an unchanged drawing kept its version"


def test_carrying_a_drawing_forward_stores_no_bytes_twice(factory: sessionmaker[Session]) -> None:
    """The mechanism ADR-0018 unlocked: one link row, the same version, the same artifact."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=2)
        changed = next(iter(before))
        reissued = _version(session, _document(session, changed), _digest("f"))
        artifacts_before = session.scalar(select(func.count()).select_from(SourceArtifact))
        versions_before = session.scalar(select(func.count()).select_from(DocumentVersion))

        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        assert session.scalar(select(func.count()).select_from(SourceArtifact)) == artifacts_before
        assert session.scalar(select(func.count()).select_from(DocumentVersion)) == versions_before


def test_a_drawing_new_to_the_package_is_included_too(factory: sessionmaker[Session]) -> None:
    """A re-issue may add a sheet that did not exist before, not only replace one."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=2)
        addition = Document(package_id=package.id, kind=DocumentKind.ARCHITECTURAL)
        session.add(addition)
        session.flush()
        added_version = _version(session, addition, _digest("g"))

        revision_two = supersede(
            session, package_id=package.id, new_document_versions=[added_version.id], actor=ACTOR
        )
        session.flush()

        after = _composition(session, revision_two.id)
        assert set(after) == set(before) | {addition.id}
        assert after[addition.id] == added_version.id


# ---------------------------------------------------------------------------
# The prior revision is untouched
# ---------------------------------------------------------------------------


def test_the_prior_revisions_document_set_is_unchanged(factory: sessionmaker[Session]) -> None:
    """**The acceptance criterion, and the reason a six-month-old review is defensible.**

    Revision 1 must still resolve to the versions it was checked against, whatever revision 2 includes.
    """
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=3)
        changed = next(iter(before))
        reissued = _version(session, _document(session, changed), _digest("f"))

        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        assert _composition(session, revision_one.id) == before, "March's answer changed"


def test_the_prior_revisions_history_gains_only_the_supersede(
    factory: sessionmaker[Session],
) -> None:
    """Its events are appended to, never rewritten — the move to `SUPERSEDED` and nothing else."""
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        events_before = [(e.sequence, e.to_state) for e in history(session, revision_one.id)]
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))

        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        events_after = [(e.sequence, e.to_state) for e in history(session, revision_one.id)]
        assert events_after[: len(events_before)] == events_before, "earlier events were rewritten"
        assert events_after[-1][1] == PackageState.SUPERSEDED.value


def test_the_prior_revision_lands_in_superseded(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))

        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        assert (
            session.scalar(
                select(PackageRevision.state).where(PackageRevision.id == revision_one.id)
            )
            == PackageState.SUPERSEDED.value
        )


def test_a_superseded_revisions_documents_can_no_longer_be_changed(
    factory: sessionmaker[Session],
) -> None:
    """**Enforced by the database, not by this module.** #366's freeze trigger refuses a change to any
    revision's document set once it has left assembly, and `SUPERSEDED` is well past that.

    This is asserted rather than restated in an AST guard: a constraint that holds is better evidence
    than a test walking the tree for callers who might not exist yet.
    """
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        membership = session.scalar(
            select(PackageRevisionDocument).where(
                PackageRevisionDocument.package_revision_id == revision_one.id
            )
        )
        assert membership is not None
        with pytest.raises(DatabaseError):
            # No trailing flush: `session.execute` raises here, so anything after it is unreachable.
            # And no `begin_nested` either — `unit_of_work` rolls back when its body raises rather than
            # committing, so the aborted transaction is discarded rather than committed. (CodeRabbit
            # suggested a savepoint on the assumption it commits; `app/db/session.py` shows it does not.)
            session.execute(
                update(PackageRevisionDocument)
                .where(PackageRevisionDocument.id == membership.id)
                .values(document_version_id=reissued.id)
            )


def test_a_superseded_revision_cannot_move_anywhere(factory: sessionmaker[Session]) -> None:
    """`SUPERSEDED` is terminal in the table (#209), so a superseded review cannot be reopened."""
    from app.lifecycle.states import TRANSITIONS, IllegalTransition, transition

    assert TRANSITIONS[PackageState.SUPERSEDED] == frozenset()
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()
        with pytest.raises(IllegalTransition):
            transition(session, revision_one.id, PackageState.APPROVED, actor=ACTOR)


# ---------------------------------------------------------------------------
# The chain, both ways
# ---------------------------------------------------------------------------


def test_the_new_revision_links_to_what_it_superseded(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        revision_two = supersede(
            session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR
        )
        session.flush()

        assert revision_two.supersedes_id == revision_one.id
        assert revision_two.revision_number == 2
        assert revision_two.state == PackageState.CREATED


def test_the_link_resolves_in_both_directions(factory: sessionmaker[Session]) -> None:
    """The acceptance criterion. `supersedes_id` answers new→old; `superseded_by` answers old→new, so a
    caller reading a superseded revision is not left inferring it from revision numbers — which say
    nothing about *which* revision superseded which."""
    with unit_of_work(factory) as session:
        package, revision_one, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        revision_two = supersede(
            session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR
        )
        session.flush()

        forward = superseded_by(session, revision_one.id)
        assert forward is not None and forward.id == revision_two.id
        assert (
            superseded_by(session, revision_two.id) is None
        ), "the current revision has no successor"


def test_the_chain_is_ordered_by_revision_number(factory: sessionmaker[Session]) -> None:
    """Two supersedes in a row. Ordered by number, which is the order §5 gives revisions — two rows
    written in the same microsecond have no order by timestamp."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=1)
        document = _document(session, next(iter(before)))
        assert document is not None

        second = supersede(
            session,
            package_id=package.id,
            new_document_versions=[_version(session, document, _digest("f")).id],
            actor=ACTOR,
        )
        session.flush()
        third = supersede(
            session,
            package_id=package.id,
            new_document_versions=[_version(session, document, _digest("e")).id],
            actor=ACTOR,
        )
        session.flush()

        chain = revision_chain(session, package.id)
        assert [r.revision_number for r in chain] == [1, 2, 3]
        assert chain[2].supersedes_id == second.id
        assert third.id == chain[2].id


# ---------------------------------------------------------------------------
# Exactly one workflow
# ---------------------------------------------------------------------------


def test_superseding_enqueues_exactly_one_workflow(factory: sessionmaker[Session]) -> None:
    """The test-plan item. Three drawings carried forward, one workflow — a revision is the unit that
    gets reviewed, so it is the unit that gets processed."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=3)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))

        revision_two = supersede(
            session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR
        )
        session.flush()

        rows = session.scalars(
            select(OutboxEntry).where(OutboxEntry.workflow == PACKAGE_WORKFLOW)
        ).all()
        assert len(rows) == 1, "one revision, one workflow"
        payload = rows[0].payload
        assert payload["package_revision_id"] == str(revision_two.id)
        assert payload["supersedes_id"] == str(revision_two.supersedes_id)
        assert payload["revision_number"] == 2


def test_the_payload_names_the_project_so_the_work_can_be_polled(
    factory: sessionmaker[Session],
) -> None:
    """`app/api/background.py` scopes its polling handle by `payload['project_id']`, so a workflow
    enqueued without one is work nobody can ask about afterwards."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        supersede(session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR)
        session.flush()

        entry = session.scalar(select(OutboxEntry).where(OutboxEntry.workflow == PACKAGE_WORKFLOW))
        assert entry is not None
        project_id = session.scalar(select(Package.project_id).where(Package.id == package.id))
        assert entry.payload["project_id"] == str(project_id)


def test_nothing_is_committed(factory: sessionmaker[Session]) -> None:
    """The prior revision's move, the new revision, its documents and the outbox row are one
    transaction or none. A superseded revision with no successor, or a successor nothing is working
    on, are both worse than a failure the caller sees."""
    package_id: UUID
    with factory() as session:
        package, _, _ = _assembled_package(session, drawings=1)
        package_id = package.id
        session.commit()

    with factory() as session:
        document_id = next(iter(_composition(session, revision_chain(session, package_id)[0].id)))
        document = _document(session, document_id)
        reissued = _version(session, document, _digest("f"))
        supersede(session, package_id=package_id, new_document_versions=[reissued.id], actor=ACTOR)
        session.rollback()

    with factory() as session:
        assert [r.revision_number for r in revision_chain(session, package_id)] == [1]
        assert session.scalar(select(func.count()).select_from(OutboxEntry)) == 0


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


def test_a_supersede_that_changes_nothing_is_refused(factory: sessionmaker[Session]) -> None:
    """It would close a review that is still valid and start another reaching the same conclusions,
    leaving a revision nobody can explain the existence of."""
    with unit_of_work(factory) as session:
        package, _, _ = _assembled_package(session, drawings=1)
        with pytest.raises(NoNewVersions, match="no new document versions"):
            supersede(session, package_id=package.id, new_document_versions=[], actor=ACTOR)


def test_a_version_that_does_not_exist_is_refused(factory: sessionmaker[Session]) -> None:
    """Named, so the caller knows which. An `IntegrityError` would name a constraint instead."""
    with unit_of_work(factory) as session:
        package, _, _ = _assembled_package(session, drawings=1)
        missing = uuid4()
        with pytest.raises(NoNewVersions, match=str(missing)):
            supersede(session, package_id=package.id, new_document_versions=[missing], actor=ACTOR)


def test_a_version_from_another_package_is_refused(factory: sessionmaker[Session]) -> None:
    """The foreign keys would refuse it too — `package_revision_documents` resolves `package_id`
    against both the revision and the document — but this says which document and why."""
    with unit_of_work(factory) as session:
        mine, _, _ = _assembled_package(session, drawings=1, name="mine")
        _, _, theirs_before = _assembled_package(session, drawings=1, name="theirs")
        stranger = next(iter(theirs_before.values()))

        with pytest.raises(VersionFromAnotherPackage, match="another package"):
            supersede(session, package_id=mine.id, new_document_versions=[stranger], actor=ACTOR)


def test_two_versions_of_one_drawing_is_refused(factory: sessionmaker[Session]) -> None:
    """Input: one drawing offered twice. Outcome: refused. Why: picking silently is the worst answer.

    **Found by CodeRabbit on #377, and it was a real bug.** `_documents_for` built a map keyed by
    version and then inverted it to key by document; two versions of one document collapsed into one
    entry, and which survived depended on the order the `IN` predicate returned rows — undefined. The
    revision would then be composed of a version nobody asked for, with nothing reporting it. A revision
    holds one version of any drawing, so there is no single right answer here and the caller has to say
    which they meant.
    """
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=1)
        document = _document(session, next(iter(before)))
        first = _version(session, document, _digest("f"))
        second = _version(session, document, _digest("e"))

        with pytest.raises(TwoVersionsOfOneDocument, match="more than one version"):
            supersede(
                session,
                package_id=package.id,
                new_document_versions=[first.id, second.id],
                actor=ACTOR,
            )


def test_the_refusal_survives_the_row_order(factory: sessionmaker[Session]) -> None:
    """Either order, same refusal. The bug's symptom was order-dependence, so the fix must not be."""
    for reverse in (False, True):
        with unit_of_work(factory) as session:
            package, _, before = _assembled_package(session, drawings=1, name=f"order {reverse}")
            document = _document(session, next(iter(before)))
            offered = [
                _version(session, document, _digest("f")).id,
                _version(session, document, _digest("e")).id,
            ]
            if reverse:
                offered.reverse()
            with pytest.raises(TwoVersionsOfOneDocument):
                supersede(
                    session,
                    package_id=package.id,
                    new_document_versions=offered,
                    actor=ACTOR,
                )


def test_a_package_with_no_revision_is_refused(factory: sessionmaker[Session]) -> None:
    """Reported as its own failure rather than as an illegal transition: there is no state to leave."""
    with unit_of_work(factory) as session, pytest.raises(NothingToSupersede):
        supersede(session, package_id=uuid4(), new_document_versions=[uuid4()], actor=ACTOR)


def test_the_new_revisions_history_is_opened(factory: sessionmaker[Session]) -> None:
    """A revision whose history was never opened has no genesis event, and its first transition would
    read as arriving from nowhere."""
    with unit_of_work(factory) as session:
        package, _, before = _assembled_package(session, drawings=1)
        reissued = _version(session, _document(session, next(iter(before))), _digest("f"))
        revision_two = supersede(
            session, package_id=package.id, new_document_versions=[reissued.id], actor=ACTOR
        )
        session.flush()

        events = history(session, revision_two.id)
        assert [e.sequence for e in events] == [1]
        assert events[0].from_state is None
        assert events[0].to_state == PackageState.CREATED.value
        assert events[0].actor == ACTOR


# ---------------------------------------------------------------------------
# Nothing deletes a revision
# ---------------------------------------------------------------------------


def _deletes_a_revision() -> list[str]:
    """Every place under `app/` or `workflow/` that deletes a `PackageRevision`.

    Narrowed deliberately. #211's plan asked for a guard that no module *deletes or updates* a
    superseded revision's children — and the children half now lives in the database: #366's
    `gv_reject_frozen_revision_documents` refuses any change to a revision's document set once it has
    left assembly, which `SUPERSEDED` is well past. Restating that as an AST walk would imply the walk
    is the reason it holds. What no constraint covers is deleting the revision row itself, so that is
    what this looks for.
    """
    offenders: list[str] = []
    for package in ("app", "workflow"):
        for path in sorted((REPO_ROOT / package).rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name == "delete" and any(
                    getattr(argument, "id", None) == "PackageRevision" for argument in node.args
                ):
                    offenders.append(f"{relative}:{node.lineno} deletes a PackageRevision")
                if name == "delete" and any(
                    getattr(argument, "attr", None) == "PackageRevision" for argument in node.args
                ):
                    offenders.append(f"{relative}:{node.lineno} deletes a PackageRevision")
    return offenders


def test_no_module_deletes_a_package_revision() -> None:
    """A deleted revision takes its findings' explanation with it.

    The database refuses it anyway — every child table references `package_revisions` with
    `ondelete=RESTRICT` — so this is the earlier warning rather than the guarantee, and it says so.
    """
    offenders = _deletes_a_revision()
    assert not offenders, (
        "these delete a package revision:\n  "
        + "\n  ".join(offenders)
        + "\n\nA revision is superseded, never removed. Its findings explain a decision somebody "
        "already acted on."
    )


def test_the_database_refuses_deleting_a_revision_with_history(
    factory: sessionmaker[Session],
) -> None:
    """The guarantee the AST walk only approximates. `ondelete=RESTRICT` on the children is what
    actually holds, and it holds against anything, including psql."""
    with unit_of_work(factory) as session:
        _, revision_one, _ = _assembled_package(session, drawings=1)
        assert session.scalar(
            select(func.count())
            .select_from(PackageStateEvent)
            .where(PackageStateEvent.package_revision_id == revision_one.id)
        )
        with pytest.raises(DatabaseError):
            # Unreachable flush removed, same reasoning as above.
            session.execute(
                PackageRevision.__table__.delete().where(PackageRevision.id == revision_one.id)
            )
