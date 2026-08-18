"""What a reviewer may do, and the four things they may not (#229, D4.1).

Four claims are worth the file, and each one is a refusal:

* a session over a superseded revision cannot be opened, so nobody signs off replaced work;
* an action takes its revision from the finding on the server, never from the caller (`C2.5`);
* an action cannot be edited — a changed mind is a second row, and the first one stays;
* nothing can be recorded by nobody, and there is no fifth verb.

The database tests skip without `DATABASE_URL` and run on CI. They use the real column names from
`app/models/review.py`; the schema is built by `alembic upgrade head` rather than `create_all`,
because what matters is that the code works against the migration that will actually be deployed.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory, unit_of_work
from app.models import (
    CheckRun,
    Finding,
    Package,
    PackageRevision,
    PackageState,
    Project,
    ReviewAction,
    ReviewActionKind,
    RuleDefinition,
    RuleSnapshot,
)
from app.review import session as review_session_module
from app.review.session import (
    ActionOutsideTheSession,
    ActorNotNamed,
    NoSuchFinding,
    NoSuchPackageRevision,
    NoSuchReviewSession,
    ReviewRefused,
    RevisionSuperseded,
    SessionAlreadyComplete,
    UnknownReviewAction,
    action_history,
    complete_session,
    open_session,
    record_action,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def _revision(
    db: Session, *, state: PackageState = PackageState.AWAITING_REVIEW
) -> PackageRevision:
    """One project, one package, one revision — a fresh tree per call.

    A fresh project every time because `(package_id, revision_number)` is unique, and two tests
    sharing a package would fail on the second insert for a reason that has nothing to do with what
    they assert.
    """
    project = Project(name=f"GV review session test {uuid4()}")
    db.add(project)
    db.flush()
    package = Package(project_id=project.id, vendor=None)
    db.add(package)
    db.flush()
    revision = PackageRevision(package_id=package.id, revision_number=1, state=state.value)
    db.add(revision)
    db.flush()
    return revision


def _successor(db: Session, revision: PackageRevision) -> PackageRevision:
    """A newer revision of the same package that names `revision` as the one it replaces."""
    newer = PackageRevision(
        package_id=revision.package_id,
        revision_number=revision.revision_number + 1,
        state=PackageState.AWAITING_REVIEW.value,
        supersedes_id=revision.id,
    )
    db.add(newer)
    db.flush()
    return newer


def _finding(db: Session, revision: PackageRevision) -> Finding:
    """A real finding under `revision`, with the rule snapshot and check run it needs.

    Built the long way rather than with a stub, because `findings` carries a composite foreign key
    back to its check run's revision — a shortcut here would be rejected by the schema, not by the
    thing the test is about.
    """
    import hashlib

    definition = RuleDefinition(rule_id=f"CT-{uuid4().hex[:6]}")
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
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    db.add(run)
    db.flush()
    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome="FAIL",
        severity="CRITICAL",
        trace={},
        parameter_set_versions={},
    )
    db.add(finding)
    db.flush()
    return finding


# ---------------------------------------------------------------------------
# Shape and boundaries — no database needed
# ---------------------------------------------------------------------------


def test_the_four_verbs_are_the_models_four_verbs() -> None:
    """Re-exported, not redefined. A second copy of the enum would be a second answer to "what may a
    reviewer do?", and the database CHECK is generated from the model's copy."""
    from app.models.review import ReviewActionKind as model_kind

    assert review_session_module.ReviewActionKind is model_kind
    assert {kind.value for kind in ReviewActionKind} == {
        "confirm",
        "correct",
        "except",
        "dismiss",
    }


def test_there_is_no_way_to_edit_an_action() -> None:
    """Append-only starts with the API surface. A function called `update_action` would be used, and
    the database refusal underneath it would surface as a 500 rather than as a design."""
    forbidden = {"edit", "update", "amend", "revise", "delete", "modify"}
    offenders = [
        name
        for name in dir(review_session_module)
        if not name.startswith("_") and any(word in name.lower() for word in forbidden)
    ]
    assert offenders == [], f"append-only, so nothing here may edit an action: {offenders}"


def test_an_action_cannot_be_told_which_revision_it_is_about() -> None:
    """`C2.5` in one assertion. There is no `package_revision_id` parameter, so the value stored can
    only have come from the server-side finding row."""
    parameters = inspect.signature(record_action).parameters
    assert "package_revision_id" not in parameters
    assert set(parameters) == {"db", "review_session_id", "finding_id", "action", "actor", "note"}


def test_every_refusal_is_one_family() -> None:
    """So the HTTP boundary can answer all of them with a single 404.

    Separate unrelated exception types would eventually get separate status codes, and the first one
    mapped to 403 would confirm to a caller outside the project that what they named exists — which
    is the thing project isolation exists to keep quiet.
    """
    for refusal in (
        NoSuchPackageRevision,
        RevisionSuperseded,
        NoSuchReviewSession,
        SessionAlreadyComplete,
        NoSuchFinding,
        ActionOutsideTheSession,
        ActorNotNamed,
        UnknownReviewAction,
    ):
        assert issubclass(refusal, ReviewRefused)


def test_the_module_reaches_nothing_it_may_not() -> None:
    """`docs/DESIGN_PRODUCT.md` §2: `app/review/` may import `app/` and `evidence/`, and nothing
    else. A correction reaching the rulebook by import is how "a correction is not a rule" stops
    being true, and an import guard is the only form of that claim which survives a refactor."""
    source = (REPO_ROOT / "app" / "review" / "session.py").read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    forbidden = {"verdict", "rules", "extraction", "retrieval", "reports", "boto3", "requests"}
    assert not (
        imported & forbidden
    ), f"app/review/session.py must not import {imported & forbidden}"


# ---------------------------------------------------------------------------
# Opening a session — against a real database
# ---------------------------------------------------------------------------


def test_a_session_opens_over_a_live_revision(postgres_engine: Engine) -> None:
    """The check has to be able to say yes."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        assert opened.package_revision_id == revision.id
        assert opened.reviewer == "anant"
        assert opened.completed_at is None
        assert opened.created_at is not None


def test_a_session_over_a_superseded_revision_cannot_be_opened(postgres_engine: Engine) -> None:
    """The acceptance criterion. Signing off a replaced drawing puts a named person's approval on
    work nobody will build."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db, state=PackageState.SUPERSEDED)
        with pytest.raises(RevisionSuperseded):
            open_session(db, package_revision_id=revision.id, reviewer="anant")


def test_a_revision_with_a_successor_counts_as_superseded(postgres_engine: Engine) -> None:
    """The state column still reads `AWAITING_REVIEW` here, and the revision is superseded anyway.

    `state` is written by whatever moved the lifecycle on; `supersedes_id` is written by whatever
    created the newer revision. Nothing keeps them in step, so checking only the column would open a
    session over a replaced revision whenever the lifecycle write was the one that got missed.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db, state=PackageState.AWAITING_REVIEW)
        _successor(db, revision)
        with pytest.raises(RevisionSuperseded):
            open_session(db, package_revision_id=revision.id, reviewer="anant")


def test_a_session_over_a_revision_that_does_not_exist_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db, pytest.raises(NoSuchPackageRevision):
        open_session(db, package_revision_id=uuid4(), reviewer="anant")


def test_a_refusal_repeats_only_what_the_caller_supplied(postgres_engine: Engine) -> None:
    """The isolation boundary, expressed in prose rather than in a status code.

    A refusal that named the project or the package would tell a caller outside the project that the
    thing they guessed at exists — which is the whole reason cross-project access answers 404 rather
    than 403. The message is checked against a *real* refusal, and the wording that makes a missing
    revision and an unreachable one read the same is checked with it.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db, state=PackageState.SUPERSEDED)
        with pytest.raises(RevisionSuperseded) as superseded:
            open_session(db, package_revision_id=revision.id, reviewer="anant")
        assert str(revision.package_id) not in str(superseded.value)

        with pytest.raises(NoSuchPackageRevision) as missing:
            open_session(db, package_revision_id=uuid4(), reviewer="anant")
        assert "outside your projects" in str(missing.value)


def test_a_session_needs_a_named_reviewer(postgres_engine: Engine) -> None:
    """There is no anonymous review. Whitespace counts as nothing — it satisfies the database's
    `reviewer <> ''` check while naming nobody."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        for nobody in ("", "   "):
            with pytest.raises(ActorNotNamed):
                open_session(db, package_revision_id=revision.id, reviewer=nobody)


def test_an_unnamed_reviewer_is_refused_before_anything_is_looked_up(
    postgres_engine: Engine,
) -> None:
    """The order matters: an anonymous request must not be able to probe which revisions exist."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db, pytest.raises(ActorNotNamed):
        open_session(db, package_revision_id=uuid4(), reviewer="")


# ---------------------------------------------------------------------------
# Completing a session
# ---------------------------------------------------------------------------


def test_completing_a_session_records_when_the_reviewer_stopped(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        completed = complete_session(db, review_session_id=opened.id)
        assert completed.completed_at is not None
        assert completed.completed_at >= completed.created_at


def test_a_session_cannot_be_completed_twice(postgres_engine: Engine) -> None:
    """Not idempotent on purpose: a `completed_at` that can be moved cannot answer "when did this
    reviewer stop?"."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        complete_session(db, review_session_id=opened.id)
        with pytest.raises(SessionAlreadyComplete):
            complete_session(db, review_session_id=opened.id)


def test_completing_a_session_that_does_not_exist_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db, pytest.raises(NoSuchReviewSession):
        complete_session(db, review_session_id=uuid4())


# ---------------------------------------------------------------------------
# Recording an action
# ---------------------------------------------------------------------------


def test_an_action_records_actor_timestamp_and_session(postgres_engine: Engine) -> None:
    """The three things the acceptance asks every action to carry."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        recorded = record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="keyur",
            note="measured on site",
        )
        assert recorded.actor == "keyur"
        assert recorded.created_at is not None
        assert recorded.review_session_id == opened.id
        assert recorded.action == ReviewActionKind.CONFIRM.value


def test_an_action_takes_its_revision_from_the_finding(postgres_engine: Engine) -> None:
    """`C2.5`. The caller supplied two ids and no revision; the stored revision came off the finding
    row the server loaded."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        recorded = record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.DISMISS,
            actor="anant",
        )
        assert recorded.package_revision_id == finding.package_revision_id


def test_an_action_on_a_finding_from_another_package_is_refused(postgres_engine: Engine) -> None:
    """A session reviewing package A carrying an action on a finding from package B would misstate
    what was reviewed, and an approval built from it would misstate what was signed off."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        mine = _revision(db)
        other = _revision(db)
        stranger = _finding(db, other)
        opened = open_session(db, package_revision_id=mine.id, reviewer="anant")
        with pytest.raises(ActionOutsideTheSession):
            record_action(
                db,
                review_session_id=opened.id,
                finding_id=stranger.id,
                action=ReviewActionKind.CONFIRM,
                actor="anant",
            )


def test_an_action_on_a_finding_that_does_not_exist_is_refused(postgres_engine: Engine) -> None:
    """The finding is resolved server-side, so an id naming nothing is a refusal rather than a row
    referencing thin air."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        with pytest.raises(NoSuchFinding):
            record_action(
                db,
                review_session_id=opened.id,
                finding_id=uuid4(),
                action=ReviewActionKind.CONFIRM,
                actor="anant",
            )


def test_an_action_in_a_session_that_does_not_exist_is_refused(postgres_engine: Engine) -> None:
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        with pytest.raises(NoSuchReviewSession):
            record_action(
                db,
                review_session_id=uuid4(),
                finding_id=finding.id,
                action=ReviewActionKind.CONFIRM,
                actor="anant",
            )


def test_nobody_can_record_an_action(postgres_engine: Engine) -> None:
    """There is no anonymous confirmation. A confirmation is a direct write into the trusted set, and
    one with no human attached is a door in the back of the evidence gate."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        for nobody in ("", "  "):
            with pytest.raises(ActorNotNamed):
                record_action(
                    db,
                    review_session_id=opened.id,
                    finding_id=finding.id,
                    action=ReviewActionKind.CONFIRM,
                    actor=nobody,
                )


def test_there_is_no_fifth_verb(postgres_engine: Engine) -> None:
    """`edit` in particular. It would collapse confirm, correct, except and dismiss into one, and the
    ledger exists to keep them apart."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        with pytest.raises(UnknownReviewAction):
            record_action(
                db,
                review_session_id=opened.id,
                finding_id=finding.id,
                action="edit",  # type: ignore[arg-type]  # what a JSON body can deliver at runtime
                actor="anant",
            )


def test_a_completed_session_takes_no_more_actions(postgres_engine: Engine) -> None:
    """An action appended to a closed sitting would misdate what happened: the record would say the
    reviewer stopped at one time and acted after it."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        complete_session(db, review_session_id=opened.id)
        with pytest.raises(SessionAlreadyComplete):
            record_action(
                db,
                review_session_id=opened.id,
                finding_id=finding.id,
                action=ReviewActionKind.CONFIRM,
                actor="anant",
            )


# ---------------------------------------------------------------------------
# Append-only
# ---------------------------------------------------------------------------


def test_a_changed_mind_is_a_new_action(postgres_engine: Engine) -> None:
    """Both answers survive, in order. The first one is still true — it says what somebody thought at
    the time, and that is the part an audit asks about."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="anant",
        )
        record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.DISMISS,
            actor="anant",
            note="the arch drawing was the stale one",
        )

    with unit_of_work(factory) as db:
        history = action_history(db, finding_id=finding.id)
        assert [entry.action for entry in history] == [
            ReviewActionKind.CONFIRM.value,
            ReviewActionKind.DISMISS.value,
        ]


def test_editing_an_action_is_refused_by_the_database(postgres_engine: Engine) -> None:
    """Not by convention. `review_actions` carries the append-only trigger from `#202`, so the
    refusal holds for anything connected to the database, not only for code that goes through this
    module."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.CONFIRM,
            actor="anant",
        )

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as db:
        stored = db.scalars(select(ReviewAction)).one()
        stored.note = "on second thoughts"
        db.flush()


def test_deleting_an_action_is_refused_too(postgres_engine: Engine) -> None:
    """Raw SQL, because the ORM is not the only way in."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=opened.id,
            finding_id=finding.id,
            action=ReviewActionKind.CORRECT,
            actor="anant",
        )

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as db:
        db.execute(text("DELETE FROM review_actions"))


def test_the_history_of_a_finding_nobody_touched_is_empty(postgres_engine: Engine) -> None:
    """An empty history is a real answer, not a missing one — and asserting it stops the two tests
    above from passing against a query that returns everything."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        finding = _finding(db, revision)
        assert action_history(db, finding_id=finding.id) == ()


def test_the_history_covers_one_finding_only(postgres_engine: Engine) -> None:
    """Two findings under the same session, one acted on. A history that returned both would make
    every assertion above pass for the wrong reason."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        revision = _revision(db)
        acted_on = _finding(db, revision)
        untouched = _finding(db, revision)
        opened = open_session(db, package_revision_id=revision.id, reviewer="anant")
        record_action(
            db,
            review_session_id=opened.id,
            finding_id=acted_on.id,
            action=ReviewActionKind.EXCEPT,
            actor="anant",
        )
        assert len(action_history(db, finding_id=acted_on.id)) == 1
        assert action_history(db, finding_id=untouched.id) == ()
