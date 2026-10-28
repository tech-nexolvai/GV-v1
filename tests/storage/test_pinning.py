"""A fact names the exact bytes it came from, and the pin is refused rather than guessed (#220, C5.3).

**Most of what this file asserts is already true, and asserting it is the point.** All four of the story's
acceptance criteria are enforced by the schema — non-nullable columns, foreign keys, a unique constraint on
`(document_id, sha256)` and an immutable table. A property held only by a column somebody could later make
nullable is a property with no alarm on it, so each one is a test that fails if the column changes.

The rest covers `require_pin`, which exists so that a caller cannot proceed unpinned by accident: it either
returns a pin or raises, and it has no "latest version" mode to reach for.

Source: backend proposal §11; `AGENTS.md` §2.7 · Design: `docs/DESIGN_PLATFORM.md` §7 · Verification: this
file
"""

from __future__ import annotations

import ast
import hashlib
import io
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.models.document import Document, DocumentKind, DocumentVersion, SourceArtifact
from app.models.evidence import CanonicalObservation, EvidenceArtifact, ObservationCandidate
from app.models.package import Package, PackageRevision, PackageState, Project
from storage.hashing import ArtifactCorrupt, IntegrityRecordMissing
from storage.pinning import Pin, require_pin
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REPO_ROOT = Path(__file__).resolve().parents[2]

BYTES = b"%PDF-1.7 pretend drawing"
DIGEST = hashlib.sha256(BYTES).hexdigest()


@pytest.fixture
def factory(postgres_engine: Engine) -> sessionmaker[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    return session_factory(postgres_engine)


def _version(
    session: Session,
    *,
    digest: str = DIGEST,
    artifact_digest: str | None = None,
    key: str = "originals/probe.pdf",
) -> DocumentVersion:
    """One document version and the source artifact it pins.

    `artifact_digest` defaults to matching, and is separable so the disagreement case can be built — that
    is a state the write path will not produce, which is exactly why it has to be constructed here.
    """
    project = Project(name=f"pinning {uuid4().hex[:8]}")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()
    document = Document(package_id=package.id, kind=DocumentKind.SHOP)
    artifact = SourceArtifact(
        storage_key=key,
        sha256=artifact_digest if artifact_digest is not None else digest,
        size=len(BYTES),
        backend_version_id=None,
    )
    session.add_all((document, artifact))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=digest,
        page_count=1,
    )
    session.add(version)
    session.flush()
    return version


class _Store:
    """Just enough of `ArtifactStore` to hand back bytes, so the store path can be exercised.

    Every method written out rather than answered by `__getattr__`: a stub that replies to any name is how
    a wrong return shape got counted as real data elsewhere in this repository.
    """

    def __init__(self, content: bytes) -> None:
        self.content = content

    def get(self, key: str) -> io.BytesIO:
        del key
        return io.BytesIO(self.content)


# ---------------------------------------------------------------------------
# require_pin
# ---------------------------------------------------------------------------


def test_a_version_resolves_to_the_bytes_it_recorded(factory: sessionmaker[Session]) -> None:
    with unit_of_work(factory) as session:
        version = _version(session)
        pin = require_pin(session, version.id)

    assert pin == Pin(document_version_id=version.id, sha256=DIGEST, bytes_verified=False)
    assert (
        pin.bytes_verified is False
    ), "no store was given, so the records were checked against each other and the bytes were not read"


def test_a_version_that_does_not_exist_is_refused(factory: sessionmaker[Session]) -> None:
    """Not "returns None". A caller that got a falsy pin and carried on would be extracting facts about
    nothing in particular, which is the failure §7 describes."""
    with (
        unit_of_work(factory) as session,
        pytest.raises(IntegrityRecordMissing) as caught,
    ):
        require_pin(session, uuid4())

    assert "nothing is pinned to it" in str(caught.value)


def test_a_version_cannot_be_pointed_at_a_missing_artifact(factory: sessionmaker[Session]) -> None:
    """The second absence is unreachable too, and for the same reason as the first.

    I wrote this to detach a version from its artifact and see the message. PostgreSQL refuses:
    `RestrictViolation`, from the composite foreign key's `ondelete="RESTRICT"`. So "the artifact row is
    gone" is defence against a state the schema does not permit — worth having its own message for when it
    is reached some other way, and worth not claiming as a live path.

    That makes three of `require_pin`'s refusals unreachable while the schema holds. The schema is doing
    nearly all of this story's work; this function's value is that a caller cannot proceed *unpinned*, not
    that it is the thing keeping the pins honest.
    """
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError) as caught, unit_of_work(factory) as session:
        version = _version(session)
        session.execute(
            DocumentVersion.__table__.update()
            .where(DocumentVersion.id == version.id)
            .values(source_artifact_id=uuid4())
        )

    assert isinstance(
        caught.value.orig, psycopg.errors.ForeignKeyViolation | psycopg.errors.RestrictViolation
    ), f"refused, but not by the artifact key: {type(caught.value.orig).__name__}"


def test_the_database_will_not_let_a_version_disagree_with_its_artifact(
    factory: sessionmaker[Session],
) -> None:
    """**I wrote this to construct a disagreement, and could not.** The schema forbids it outright.

    `document_versions` has a composite foreign key `(source_artifact_id, sha256) → (source_artifacts.id,
    source_artifacts.sha256)`, so a version recording a digest its artifact does not have is a row
    PostgreSQL refuses. That is the same shape as ADR-0018's fix for cross-package leakage: make the
    invariant a key rather than a convention.

    So `require_pin`'s equality check is defence against a row that arrived some other way, not a live
    path — and this test asserts the thing that actually holds, rather than pretending to exercise a state
    the database will not produce.
    """
    from sqlalchemy.exc import IntegrityError

    composite = [
        constraint
        for constraint in DocumentVersion.__table__.constraints
        if type(constraint).__name__ == "ForeignKeyConstraint"
        and {column.name for column in constraint.columns} == {"source_artifact_id", "sha256"}
    ]
    assert composite, (
        "the composite foreign key is gone, so a version can now record a digest its artifact does not "
        "have — and require_pin's check becomes the only thing standing between that and a wrong pin"
    )

    other = hashlib.sha256(b"different bytes entirely").hexdigest()
    with pytest.raises(IntegrityError) as caught, unit_of_work(factory) as session:
        version = _version(session, digest=DIGEST, artifact_digest=other)
        del version

    # **Which constraint refused it.** `_version` inserts a project, package, revision, document and
    # artifact before the version, so any IntegrityError from any of those would satisfy a bare
    # `pytest.raises` — the same defect I fixed in #402's node-key test and then repeated here.
    assert isinstance(
        caught.value.orig, psycopg.errors.ForeignKeyViolation
    ), f"refused, but not by a foreign key: {type(caught.value.orig).__name__}"
    assert "sha256" in str(
        caught.value.orig
    ), "and it must be the composite key on (source_artifact_id, sha256), not another relation"


def test_a_prefixed_digest_cannot_even_be_stored(factory: sessionmaker[Session]) -> None:
    """The other branch I could not reach, for a second reason.

    `sha256` is `VARCHAR(64)` with a `^[0-9a-f]{64}$` check, so the `sha256:`-prefixed form used by
    `model_invocations.node_invocation_key` is both too long and the wrong shape. Two formats exist in this
    codebase; the database keeps them apart without help.
    """
    from sqlalchemy.exc import DataError

    assert DocumentVersion.__table__.columns["sha256"].type.length == 64

    with pytest.raises(DataError) as caught, unit_of_work(factory) as session:
        version = _version(session)
        session.execute(
            DocumentVersion.__table__.update()
            .where(DocumentVersion.id == version.id)
            .values(sha256=f"sha256:{DIGEST}")
        )

    # **Which DataError.** PostgreSQL checks the column width before the append-only trigger fires, so this
    # must be the length violation and not something else the setup happened to trip — otherwise the test
    # passes while proving nothing about the column being 64 characters wide.
    assert "value too long for type character varying" in str(
        caught.value
    ), f"refused, but not for the length: {str(caught.value)[:160]}"


def test_bytes_that_no_longer_match_are_refused(factory: sessionmaker[Session]) -> None:
    """The store path. What it adds over `ArtifactStore.get` — which already verifies against the store's
    own record — is catching the store and the database disagreeing with *each other*."""
    with unit_of_work(factory) as session:
        version = _version(session)
        # Not `is not None`: `require_pin` either raises or returns a `Pin`, so that could never fail —
        # review's point. Assert what the call is for.
        verified = require_pin(session, version.id, store=_Store(BYTES))  # type: ignore[arg-type]
        assert verified.sha256 == DIGEST
        assert verified.bytes_verified is True, "a store was given, so the bytes were re-hashed"
        with pytest.raises(ArtifactCorrupt, match="no longer there"):
            require_pin(session, version.id, store=_Store(b"someone replaced the drawing"))  # type: ignore[arg-type]


def test_there_is_no_way_to_ask_for_the_latest_version() -> None:
    """**The absence is the design, so it is asserted.**

    A resolver that could answer "the current document" turns a fact about specific bytes into a fact about
    whatever is there now. `storage/pinning.py` must offer no such entry point, and neither its function
    names nor its parameters may suggest one.
    """
    source = (REPO_ROOT / "storage" / "pinning.py").read_text()
    tree = ast.parse(source)
    names = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    for name in names:
        assert not any(
            word in name.lower() for word in ("latest", "current", "newest")
        ), f"{name} reads as a resolver for whatever is there now"

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        # posonlyargs and async definitions included: the first scan missed both, so a
        # `def get(version, /, *, latest=True)` or an async resolver would have passed.
        arguments = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        for argument in arguments:
            assert not any(
                word in argument.arg.lower() for word in ("latest", "current", "newest")
            ), f"{node.name}({argument.arg}) offers a way to ask for whatever is there now"


# ---------------------------------------------------------------------------
# The schema facts, turned into tests that can fail
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model",
    [ObservationCandidate, CanonicalObservation, EvidenceArtifact],
    ids=["candidate", "canonical", "evidence-artifact"],
)
def test_evidence_cannot_exist_without_a_document_version(model: type) -> None:
    """Acceptance criterion one, already true — and now with an alarm on it.

    `nullable=False` plus a foreign key is what makes "an observation cannot be created without a document
    version" a fact rather than a habit. Somebody making this column nullable would satisfy every other
    test in the suite.
    """
    column = model.__table__.columns["document_version_id"]
    assert column.nullable is False, f"{model.__name__}.document_version_id became optional"
    assert [str(fk.target_fullname) for fk in column.foreign_keys] == ["document_versions.id"]


def test_a_finding_reaches_its_bytes_through_its_evidence() -> None:
    """Findings are pinned transitively, not directly, and that is the right shape.

    `Finding → FindingEvidence → CanonicalObservation → document_version_id`, every hop a foreign key. A
    `document_version_id` column on `findings` would be a second copy of a fact already recorded — and a
    second copy is a chance for the two to disagree.
    """
    from app.models.verdicts import Finding, FindingEvidence

    assert "document_version_id" not in Finding.__table__.columns
    link = FindingEvidence.__table__.columns["canonical_observation_id"]
    assert [str(fk.target_fullname) for fk in link.foreign_keys] == ["canonical_observations.id"]
    assert CanonicalObservation.__table__.columns["document_version_id"].nullable is False


def test_new_bytes_are_a_new_version_and_identical_bytes_are_refused(
    factory: sessionmaker[Session],
) -> None:
    """Acceptance criterion three, and the one I expected to have to build.

    `uq_document_versions_document_id_sha256` already does it from both directions: the same bytes cannot
    become a second version of one document, and different bytes get their own row rather than overwriting.
    With `DocumentVersion` immutable on top, there is no overwrite path at all.
    """
    from sqlalchemy.exc import IntegrityError

    with unit_of_work(factory) as session:
        first = _version(session)
        document_id = first.document_id

        # Different bytes: a new version, not an overwrite.
        other = hashlib.sha256(b"a revised drawing").hexdigest()
        second_artifact = SourceArtifact(
            storage_key="originals/probe-v2.pdf", sha256=other, size=99, backend_version_id=None
        )
        session.add(second_artifact)
        session.flush()
        second = DocumentVersion(
            document_id=document_id,
            source_artifact_id=second_artifact.id,
            sha256=other,
            page_count=1,
        )
        session.add(second)
        session.flush()
        assert second.id != first.id
        assert require_pin(session, second.id).sha256 == other
        assert require_pin(session, first.id).sha256 == DIGEST, "the first version is untouched"

    # The same bytes again for the same document: refused.
    with pytest.raises(IntegrityError) as caught, unit_of_work(factory) as session:
        duplicate_artifact = SourceArtifact(
            storage_key="originals/probe-again.pdf",
            sha256=DIGEST,
            size=len(BYTES),
            backend_version_id=None,
        )
        session.add(duplicate_artifact)
        session.flush()
        session.add(
            DocumentVersion(
                document_id=document_id,
                source_artifact_id=duplicate_artifact.id,
                sha256=DIGEST,
                page_count=1,
            )
        )
        session.flush()

    assert isinstance(
        caught.value.orig, psycopg.errors.UniqueViolation
    ), f"refused, but not by a unique constraint: {type(caught.value.orig).__name__}"
    message = str(caught.value.orig)
    assert "document_id" in message and "sha256" in message, (
        "and it must be uq_document_versions_document_id_sha256 — the constraint that makes identical "
        "bytes a duplicate rather than a second version"
    )


def test_nothing_in_extraction_resolves_a_document_without_a_version() -> None:
    """Acceptance criterion two, asserted against the source.

    Already true — nothing under `extraction/` names a document except through a version. It is asserted
    because the way this criterion gets broken is not a wrong answer but a convenience: a helper that takes
    a `document_id` "just for now", which then quietly becomes the path everything uses.
    """
    extraction = REPO_ROOT / "extraction"
    # Without this the whole test passes when the directory is renamed or removed — `rglob` over a missing
    # path yields nothing, so "no offenders" and "nothing looked at" are indistinguishable.
    assert extraction.is_dir(), "extraction/ is not where this guard expects it"
    modules = sorted(extraction.rglob("*.py"))
    assert modules, "extraction/ has no modules, so this guard is checking nothing"

    offenders: list[str] = []
    for path in modules:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for argument in node.args.args + node.args.kwonlyargs:
                name = argument.arg
                if name == "document_id" or (name.endswith("document") and "version" not in name):
                    offenders.append(
                        f"{path.relative_to(REPO_ROOT)}:{node.lineno} {node.name}({name})"
                    )
    assert not offenders, (
        "these take a document without a version, so a fact extracted through them would name 'the "
        "drawing' rather than the bytes:\n  " + "\n  ".join(offenders)
    )


def test_pinning_and_supersession_answer_different_questions(
    factory: sessionmaker[Session],
) -> None:
    """They compose; neither substitutes for the other, which is what the story asks to verify.

    Pinning fixes the **bytes** a fact came from. Supersession decides which **sheet governs** now. A
    superseded revision's facts stay pinned to the bytes they were read from — that is the whole point of
    being able to answer "what did you tell us in March?" — and the pin says nothing about whether that
    sheet still governs.
    """
    from app.lifecycle.states import begin, transition

    with unit_of_work(factory) as session:
        version = _version(session)
        revision = session.get(PackageRevision, _revision_of(session, version))
        assert revision is not None
        begin(session, revision.id, actor="anant")
        transition(session, revision.id, PackageState.SUPERSEDED, actor="anant")

        pin = require_pin(session, version.id)
        assert pin.sha256 == DIGEST, "the bytes are still named, superseded or not"
        assert (
            PackageState(revision.state) == PackageState.SUPERSEDED
        ), "and the revision is still identifiable as superseded, which the pin does not encode"


def _revision_of(session: Session, version: DocumentVersion) -> UUID:
    """The revision whose package owns this version's document."""
    document = session.get(Document, version.document_id)
    assert document is not None
    revision = (
        session.query(PackageRevision)
        .filter(PackageRevision.package_id == document.package_id)
        .one()
    )
    return revision.id
