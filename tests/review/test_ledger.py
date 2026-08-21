"""The correction ledger: what it records, what refuses to change it, and who may read it (#233).

Four things have to hold, and each one is a section below.

* **Append-only in the database, not by convention.** The trigger from `0013_append_only.py` refuses
  `UPDATE` and `DELETE` on `correction_ledger`, through the ORM and through raw SQL. Asserted
  against a real PostgreSQL, because a mock would only prove the test author believed it.
* **The original is always kept beside the correction.** Without it there is no way to ask what we
  got wrong, and the reviewer correction rate in `AGENTS.md` §9 measures nothing.
* **Queryable by rule, check type and vendor.** That is how a pattern surfaces — the same rule
  corrected twenty times, or one vendor's drawings corrected far more than anyone else's.
* **Nothing in the rules path may read it.** An import guard, because a slogan does not survive a
  refactor (`docs/DESIGN_PRODUCT.md` §4.2).

The negative tests assert the *name* of the constraint PostgreSQL rejected on. A row usually
violates more than one thing, and `pytest.raises(IntegrityError)` accepts whichever fired first —
two tests in `tests/db/test_review_models.py` passed for the wrong reason until the name was
checked, one of them rejected by a unique index rather than the foreign key it claimed to exercise.

**The reading being corrected is built as one that genuinely passed the evidence gate**, with two
independent extractors agreeing. Not decoration: `check_canonical_observation_provenance()` refuses
a `CORROBORATED` observation that cannot show two supporting candidates, and the first version of
this file was rejected on CI for asserting the status without the evidence behind it. Every column
name in that fixture was correct — the *combination* was not, which is the part no amount of
reading `app/models/` would have caught. `test_the_reading_being_corrected_really_passed_the_
evidence_gate` pins it so a future failure cannot be made to go away by weakening it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from fractions import Fraction
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.db.base import Immutable, immutable_table_names, utc_now
from app.db.session import session_factory, unit_of_work
from app.models import (
    CanonicalObservation,
    CheckRun,
    CorrectionLedgerEntry,
    Document,
    DocumentKind,
    DocumentVersion,
    EvidenceCandidateRole,
    EvidenceSupportingCandidate,
    ExtractionRun,
    Finding,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageState,
    Page,
    Project,
    ReviewAction,
    ReviewActionKind,
    ReviewSession,
    RuleDefinition,
    RuleSnapshot,
    SourceArtifact,
    TaskRun,
    WorkflowRun,
)
from app.review import ledger
from evidence.canonical import Authority
from rules.semantic_types import DocumentRole, SemanticType
from tests.test_verdict_isolation import REPO_ROOT, _imports_in, _py_files, transitive_imports
from units.measurement import Unit
from verdict.operands import EvidenceStatus
from verdict.outcomes import Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

PAGE_HASH = "b" * 64


def _violated(error: IntegrityError) -> str | None:
    """The constraint PostgreSQL actually rejected on, from the driver's own diagnostics.

    The `ck_correction_ledger_` prefix is stripped before comparing. `app/models/review.py` names
    its checks bare and `Base` renders them through the project's naming convention, while
    `alembic/versions/0012_review_plane.py` writes the same names into `op.create_table`, which
    builds its own metadata and applies no convention. Which spelling is installed therefore depends
    on how the schema was built, and a test asserting one of them would be asserting the build
    route rather than the constraint. Everything that distinguishes one constraint from another
    survives the strip.
    """
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    name: str | None = getattr(diagnostic, "constraint_name", None)
    if name is None:
        return None
    return name.removeprefix("ck_correction_ledger_")


def _upgrade(engine: Engine) -> None:
    config = Config("alembic.ini")
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


# ---------------------------------------------------------------------------
# One complete scenario: a package, a rule, a finding, a reading and a reviewer
# ---------------------------------------------------------------------------


def _corroborated_observation(
    session: Session,
    unique: str,
    *,
    package_revision_id: UUID,
    document_version_id: UUID,
    page_id: UUID,
) -> CanonicalObservation:
    """A reading that genuinely passed the evidence gate, built the way one actually gets built.

    `check_canonical_observation_provenance()` in `0006_evidence_plane.py` is the evidence gate
    expressed in the database, and it refuses a `CORROBORATED` row that cannot show its work: it
    needs two supporting candidates and no conflicting one, or one supporting candidate plus a
    `DUAL_UNIT` lane. The first version of this fixture set `status=CORROBORATED` on an observation
    with no candidates at all and was rejected on CI — correctly. A status column is a claim; the
    linked candidates are the evidence for it, and the trigger is what keeps the two honest.

    So this builds two genuinely independent routes: two extractors, in two task runs, each
    reporting the same measurement. Reaching the same number twice by two different means is what
    "corroborated" means. Filling in a second row from the same extraction run would satisfy the
    count while being one route recorded twice, which is the thing the gate exists to refuse.

    The trigger is `DEFERRABLE INITIALLY DEFERRED`, so it runs at `COMMIT` rather than at `flush()`.
    That is why a broken fixture surfaced in the tests that commit and not in the ones that expect a
    flush to fail — worth knowing, because it means a fixture can look fine right up to the end of
    the transaction.
    """
    workflow = WorkflowRun(package_revision_id=package_revision_id, engine_run_id=f"run-{unique}")
    session.add(workflow)
    session.flush()

    value = Fraction(1, 3)
    candidate_ids: list[UUID] = []
    for extractor in ("pdfplumber", "ocr"):
        task = TaskRun(
            workflow_run_id=workflow.id,
            idempotency_key=f"extract-{extractor}-{unique}",
            task_type="extract_page",
            attempt=1,
            outcome="ok",
        )
        session.add(task)
        session.flush()

        run = ExtractionRun(
            task_run_id=task.id,
            extractor=extractor,
            extractor_version="1.0",
            config_hash="config-v1",
        )
        session.add(run)
        session.flush()

        candidate = ObservationCandidate(
            document_version_id=document_version_id,
            page_id=page_id,
            extraction_run_id=run.id,
            # Both extractors read the same dimension off the same page. That they agree is the
            # whole of the corroboration; if they disagreed the observation would be CONFLICTING
            # and a reviewer would be looking at it for a different reason.
            raw_text='1/3"',
            value_numerator=value.numerator,
            value_denominator=value.denominator,
            unit=Unit.INCH,
            unit_guess=Unit.INCH,
            semantic_guess=SemanticType.CT001,
            polygon=[[10, 10], [20, 10], [20, 20]],
            coordinate_space="image",
            confidence=None,
            ambiguity_flags=[],
        )
        session.add(candidate)
        session.flush()
        candidate_ids.append(candidate.id)

    observation = CanonicalObservation(
        document_version_id=document_version_id,
        page_id=page_id,
        document_role=DocumentRole.SHOP,
        polygon=[["0.1", "0.1"], ["0.2", "0.1"], ["0.2", "0.2"]],
        coordinate_space="stored",
        semantic_type=SemanticType.CT001,
        value_numerator=value.numerator,
        value_denominator=value.denominator,
        unit=Unit.INCH,
        status=EvidenceStatus.CORROBORATED,
        authority=Authority.AUTHORITATIVE,
        evidence_crop_uri=None,
    )
    session.add(observation)
    session.flush()

    primary, corroborating = candidate_ids
    session.add_all(
        (
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id,
                candidate_id=primary,
                role=EvidenceCandidateRole.PRIMARY,
            ),
            EvidenceSupportingCandidate(
                canonical_observation_id=observation.id,
                candidate_id=corroborating,
                role=EvidenceCandidateRole.CORROBORATING,
            ),
        )
    )
    session.flush()
    return observation


@dataclass(frozen=True, slots=True)
class Scenario:
    """Everything one correction needs to exist, so a query has something real to join through."""

    rule_id: str
    check_type: str
    vendor: str | None
    observation_id: UUID
    finding_id: UUID
    package_revision_id: UUID
    review_session_id: UUID


def _scenario(
    session: Session,
    *,
    rule_id: str | None = None,
    check_type: str = "internal",
    vendor: str | None = None,
) -> Scenario:
    """Build the whole chain, in dependency order: a package from a vendor, a rule, a check run, a
    finding, a reading that passed the evidence gate, and a reviewer sitting down with it.

    Every field is spelled as `app/models/` spells it, because inventing a plausible field name is
    how earlier stories in this plane lost CI rounds. That is necessary and it is not sufficient:
    the first version of this fixture used only real column names and still failed, because a
    `CORROBORATED` observation with no supporting candidates is a row whose columns are all valid
    and whose *combination* is not. Model introspection cannot tell you that — the rule lives in a
    trigger. `_corroborated_observation` is where it is satisfied.
    """
    unique = uuid4().hex[:8]
    project = Project(name=f"GV Ledger Test {unique}")
    session.add(project)
    session.flush()

    package = Package(project_id=project.id, vendor=vendor)
    session.add(package)
    session.flush()

    revision = PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    session.add(revision)
    session.flush()

    digest = hashlib.sha256(unique.encode()).hexdigest()
    artifact = SourceArtifact(
        storage_key=f"originals/{project.id}/drawing.pdf",
        sha256=digest,
        size=100,
        backend_version_id=None,
    )
    document = Document(package_id=revision.package_id, kind=DocumentKind.SHOP)
    session.add_all((artifact, document))
    session.flush()

    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256=digest, page_count=1
    )
    session.add(version)
    session.flush()

    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=PAGE_HASH,
        width_pt=612,
        height_pt=792,
        rotation=0,
        has_vector_text=True,
        render_failed=False,
    )
    session.add(page)
    session.flush()

    observation = _corroborated_observation(
        session,
        unique,
        package_revision_id=revision.id,
        document_version_id=version.id,
        page_id=page.id,
    )

    authored_rule_id = rule_id or f"CT-{unique}"
    definition = RuleDefinition(rule_id=authored_rule_id)
    session.add(definition)
    session.flush()

    body = f'{{"id":"{authored_rule_id}"}}'
    snapshot = RuleSnapshot(
        rule_definition_id=definition.id,
        snapshot_id=f"sha256:{hashlib.sha256(body.encode()).hexdigest()}",
        version="1.0.0",
        canonical_json=body,
        product_type="countertop",
        check_type=check_type,
        unconfirmed_tolerance_count=0,
    )
    session.add(snapshot)
    session.flush()

    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=snapshot.id,
        engine_version="verdict-1.2.3",
    )
    session.add(run)
    session.flush()

    finding = Finding(
        check_run_id=run.id,
        package_revision_id=revision.id,
        outcome=Outcome.FAIL.value,
        severity=Severity.CRITICAL.value,
        trace={},
        parameter_set_versions={},
    )
    session.add(finding)
    session.flush()

    review = ReviewSession(package_revision_id=revision.id, reviewer="anant")
    session.add(review)
    session.flush()

    return Scenario(
        rule_id=authored_rule_id,
        check_type=check_type,
        vendor=vendor,
        observation_id=observation.id,
        finding_id=finding.id,
        package_revision_id=revision.id,
        review_session_id=review.id,
    )


def _action(
    session: Session,
    scenario: Scenario,
    kind: ReviewActionKind = ReviewActionKind.CORRECT,
    actor: str = "anant",
) -> ReviewAction:
    action = ReviewAction(
        review_session_id=scenario.review_session_id,
        finding_id=scenario.finding_id,
        package_revision_id=scenario.package_revision_id,
        action=kind.value,
        actor=actor,
    )
    session.add(action)
    session.flush()
    return action


# ---------------------------------------------------------------------------
# Nothing in the rules path may read the ledger
# ---------------------------------------------------------------------------


def test_the_deciding_packages_cannot_reach_the_ledger() -> None:
    """`AGENTS.md` §2.6 — a correction is a reviewer fixing one drawing, not a rule change.

    The ledger lives in `app`, so the precise assertion is that `rules/` and `verdict/` cannot reach
    `app` by any chain of imports. Nothing stops a person reading the ledger and *authoring* a rule
    from what they see, and nothing should; what must not exist is code in the rules path that reads
    corrections directly, because then the rulebook starts changing without anybody publishing a
    version of it.
    """
    for package in ("rules", "verdict"):
        chains = transitive_imports(package)
        assert "app" not in chains, (
            f"{package}/ can reach the correction ledger via "
            f"{' -> '.join(chains.get('app', []))}. Corrections becoming rules by accumulation is "
            "how a system quietly starts deciding what it was told to check."
        )


def test_the_import_guard_is_capable_of_firing() -> None:
    """A guard that never fires looks identical to a clean codebase.

    `eval/` legitimately imports `app` — it reads stored results to compute release metrics — so the
    same walker finds the same edge when the edge really is there. Without this the test above would
    still pass if `transitive_imports` silently returned nothing.
    """
    assert "app" in transitive_imports("eval")


def test_the_rules_path_does_not_name_the_ledger_module_in_a_string() -> None:
    """An import guard sees nothing inside a raw SQL string or a dotted path built at runtime.

    `SELECT * FROM correction_ledger` in a `rules/` module reads the ledger just as surely as an
    import does, and imports nothing at all. `tests/test_verdict_isolation.py` checks the table
    name; this checks the module and its functions, which is the other way in now that they exist.
    """
    forbidden = ("app.review.ledger", "record_correction", "history_for_observation")
    for package in ("rules", "verdict"):
        offenders = [
            f"{path.name} names {name}"
            for path in _py_files(package)
            for name in forbidden
            if name in path.read_text(encoding="utf-8")
        ]
        assert not offenders, "the rules path names the ledger:\n  " + "\n  ".join(offenders)


def test_the_ledger_module_does_not_import_the_deciding_packages_either() -> None:
    """The boundary read the other way.

    `app/review/ledger.py` may import models and SQLAlchemy. It has no business importing `verdict/`
    or `rules/`: a ledger that could evaluate a rule would be a second place where a decision is
    made, and the golden rule allows exactly one.
    """
    imported = _imports_in(REPO_ROOT / "app" / "review" / "ledger.py")
    assert not {"verdict", "rules", "retrieval", "extraction"} & imported


# ---------------------------------------------------------------------------
# The fixture itself has to be honest
# ---------------------------------------------------------------------------


def test_the_reading_being_corrected_really_passed_the_evidence_gate(
    postgres_engine: Engine,
) -> None:
    """The fixture's own guard, and the reason it is a test rather than a comment.

    A correction is a reviewer overruling a reading that already qualified, entered a verdict and
    produced a finding. If `_corroborated_observation` were ever quietly reduced to one candidate to
    make some future failure go away, every test in this file would still pass while exercising a
    reading no evidence gate would have admitted — and the ledger would be measuring corrections to
    something the system never actually relied on.

    So the two independent routes are asserted here, in the same terms
    `check_canonical_observation_provenance()` uses: two supporting candidates, no conflicting one,
    and each from a different extraction run.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        observation_id = _scenario(session).observation_id

    with unit_of_work(factory) as session:
        observation = session.get(CanonicalObservation, observation_id)
        assert observation is not None
        assert observation.status == EvidenceStatus.CORROBORATED.value

        links = session.scalars(
            select(EvidenceSupportingCandidate).where(
                EvidenceSupportingCandidate.canonical_observation_id == observation_id
            )
        ).all()
        assert {link.role for link in links} == {
            EvidenceCandidateRole.PRIMARY.value,
            EvidenceCandidateRole.CORROBORATING.value,
        }

        runs = session.scalars(
            select(ObservationCandidate.extraction_run_id).where(
                ObservationCandidate.id.in_([link.candidate_id for link in links])
            )
        ).all()
        assert len(set(runs)) == 2, "two candidates from one extraction run is one route, twice"


# ---------------------------------------------------------------------------
# Append-only: what the database guarantees, asserted where it is guaranteed
# ---------------------------------------------------------------------------


def test_the_ledger_carries_the_immutable_marker_and_is_therefore_protected() -> None:
    """The marker is what puts the table in the migration's trigger list, so this is the link
    between "the model says immutable" and "the database refuses". `tests/db/test_append_only.py`
    asserts the migration's list and the marker's list are the same tuple."""
    assert issubclass(CorrectionLedgerEntry, Immutable)
    assert "correction_ledger" in immutable_table_names()


def test_an_update_to_a_correction_is_refused_by_the_database(postgres_engine: Engine) -> None:
    """The acceptance criterion, against a real database. Not a check in `app/review/ledger.py`:
    a Python guard in front of a database guard only tells you which of the two was circumvented."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original="1219 mm",
            corrected="1216 mm",
        )

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        stored = session.scalars(select(CorrectionLedgerEntry)).one()
        stored.corrected_value = "something more convenient"
        session.flush()


def test_a_delete_of_a_correction_is_refused_by_the_database(postgres_engine: Engine) -> None:
    """The record of what we got wrong is exactly what somebody would be tempted to remove."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original="1219 mm",
            corrected="1216 mm",
        )

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        session.delete(session.scalars(select(CorrectionLedgerEntry)).one())
        session.flush()


def test_raw_sql_cannot_edit_the_ledger_either(postgres_engine: Engine) -> None:
    """The ORM is not the only way in, and the guard is a trigger precisely so that it refuses
    whoever is connected."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original="1219 mm",
            corrected="1216 mm",
        )

    with pytest.raises(DBAPIError, match="append-only"), unit_of_work(factory) as session:
        session.execute(text("UPDATE correction_ledger SET corrected_value = '0 mm'"))


# ---------------------------------------------------------------------------
# The original is always kept beside the correction
# ---------------------------------------------------------------------------


def test_a_correction_keeps_the_original_beside_the_change(postgres_engine: Engine) -> None:
    """Storing only the corrected value would leave no way to ask what we got wrong."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        entry = ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original='48 3/4"',
            corrected='48 1/2"',
        )
        assert entry.id is not None

    with unit_of_work(factory) as session:
        stored = session.scalars(select(CorrectionLedgerEntry)).one()
        assert (stored.original_value, stored.corrected_value) == ('48 3/4"', '48 1/2"')
        assert stored.action == ReviewActionKind.CORRECT.value


def test_values_are_stored_verbatim_and_not_parsed(postgres_engine: Engine) -> None:
    """A correction is as likely to be to a unit, a label or an identifier as to a dimension.

    Round-tripping a non-numeric correction is what proves the ledger is not quietly a numeric
    table — and nothing here parses the text, so there is no rounding step for a float to enter.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original="B-24-L",
            corrected="B-24-R",
        )
    with unit_of_work(factory) as session:
        stored = session.scalars(select(CorrectionLedgerEntry)).one()
        assert (stored.original_value, stored.corrected_value) == ("B-24-L", "B-24-R")


def test_a_correction_that_changes_nothing_is_refused(postgres_engine: Engine) -> None:
    """That is a confirmation, and it belongs in `review_actions` as one. Storing it here would
    inflate the correction rate with non-events."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=scenario.observation_id,
            original="1219 mm",
            corrected="1219 mm",
        )
    assert _violated(raised.value) == "correction_actually_changes_something"


def test_a_correction_cannot_hang_off_a_confirmation(postgres_engine: Engine) -> None:
    """A ledger row attached to a `confirm` would count an event that was not a correction.

    Everything else about this row is valid — a real observation, two different values, no earlier
    correction for this action — so the action kind is the only thing left to reject it. The
    composite foreign key cannot be what fires either: `(id, 'confirm')` genuinely exists in
    `review_actions`, so the pair resolves and the `CHECK` pinning `action` to `correct` is the one
    constraint this row breaks. That is what makes the exact name below meaningful rather than an
    accident of which constraint PostgreSQL happened to evaluate first.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        scenario = _scenario(session)
        confirmation = _action(session, scenario, ReviewActionKind.CONFIRM)
        session.add(
            CorrectionLedgerEntry(
                review_action_id=confirmation.id,
                action=ReviewActionKind.CONFIRM.value,
                canonical_observation_id=scenario.observation_id,
                original_value="1219 mm",
                corrected_value="1216 mm",
            )
        )
        session.flush()
    assert _violated(raised.value) == "correction_action_is_a_correction"


def test_a_correction_must_name_a_reading_that_exists(postgres_engine: Engine) -> None:
    """ "Server-side rows, never a client-supplied value", in a schema. A correction against an
    observation id that names nothing would be a correction to something no verdict ever used."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario).id,
            canonical_observation_id=uuid4(),
            original="1219 mm",
            corrected="1216 mm",
        )
    assert _violated(raised.value) == "fk_correction_observation"


def test_one_correction_per_review_action(postgres_engine: Engine) -> None:
    """Two would leave "what did the reviewer change?" with two answers. A reviewer changing their
    mind writes a *new* action, which is what `history_for_observation` reads."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        action_id = _action(session, scenario).id
        ledger.record_correction(
            session,
            review_action_id=action_id,
            canonical_observation_id=scenario.observation_id,
            original="a",
            corrected="b",
        )
    with pytest.raises(IntegrityError) as raised, unit_of_work(factory) as session:
        entry = session.scalars(select(CorrectionLedgerEntry)).one()
        ledger.record_correction(
            session,
            review_action_id=entry.review_action_id,
            canonical_observation_id=entry.canonical_observation_id,
            original="a",
            corrected="c",
        )
    assert _violated(raised.value) == "ix_correction_ledger_review_action_id"


# ---------------------------------------------------------------------------
# Superseded, never edited
# ---------------------------------------------------------------------------


def test_a_second_correction_supersedes_without_erasing_the_first(postgres_engine: Engine) -> None:
    """What "append-only" means in practice.

    A second reviewer disagreeing with the first writes a new action and a new row. Both survive:
    the list answers "how did we get here", and its last entry answers "what do we think now". If
    the first row could be overwritten, the disagreement itself would disappear — and a
    disagreement between two reviewers about the same reading is a signal, not noise.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario, actor="anant").id,
            canonical_observation_id=scenario.observation_id,
            original="1219 mm",
            corrected="1216 mm",
        )
        ledger.record_correction(
            session,
            review_action_id=_action(session, scenario, actor="keyur").id,
            canonical_observation_id=scenario.observation_id,
            original="1216 mm",
            corrected="1213 mm",
        )
        observation_id = scenario.observation_id

    with unit_of_work(factory) as session:
        history = ledger.history_for_observation(session, observation_id)
        assert [(e.original_value, e.corrected_value) for e in history] == [
            ("1219 mm", "1216 mm"),
            ("1216 mm", "1213 mm"),
        ]


def test_the_history_of_an_uncorrected_reading_is_empty(postgres_engine: Engine) -> None:
    """An empty list, not an error. Most readings are never corrected, and that is the normal case
    rather than a missing record."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session)
        assert ledger.history_for_observation(session, scenario.observation_id) == []


# ---------------------------------------------------------------------------
# Queryable by rule, check type and vendor — that is how patterns surface
# ---------------------------------------------------------------------------


def _correct(session: Session, scenario: Scenario, corrected: str) -> CorrectionLedgerEntry:
    return ledger.record_correction(
        session,
        review_action_id=_action(session, scenario).id,
        canonical_observation_id=scenario.observation_id,
        original="1219 mm",
        corrected=corrected,
    )


def test_by_rule_returns_only_that_rule(postgres_engine: Engine) -> None:
    """A rule corrected over and over is the pattern this exists to surface, and it would be
    invisible if the query also returned every other rule's corrections."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        wanted = _scenario(session, rule_id="CT-WIDTH-001")
        other = _scenario(session, rule_id="CT-SINK-002")
        _correct(session, wanted, "1216 mm")
        _correct(session, other, "900 mm")

    with unit_of_work(factory) as session:
        found = ledger.by_rule(session, "CT-WIDTH-001", timedelta(days=30))
        assert [entry.corrected_value for entry in found] == ["1216 mm"]
        assert ledger.by_rule(session, "CT-NOBODY-999", timedelta(days=30)) == []


def test_by_check_type_returns_only_that_check_type(postgres_engine: Engine) -> None:
    """Check type is an attribute of the rule — which documents it reads — so a cluster here points
    at a class of drawing we read badly rather than at one rule being wrong."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        internal = _scenario(session, check_type="internal")
        cross = _scenario(session, check_type="cross_document")
        _correct(session, internal, "1216 mm")
        _correct(session, cross, "900 mm")

    with unit_of_work(factory) as session:
        found = ledger.by_check_type(session, "cross_document", timedelta(days=30))
        assert [entry.corrected_value for entry in found] == ["900 mm"]


def test_by_vendor_returns_only_that_vendor(postgres_engine: Engine) -> None:
    """ADR-0006: vendor identity is metadata, never a rule key. Spotting that one vendor's drawings
    keep needing the same correction is a conversation to have with them — it must never become an
    input to how carefully their drawings are checked."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        theirs = _scenario(session, vendor="Vendor A")
        someone_else = _scenario(session, vendor="Vendor B")
        _correct(session, theirs, "1216 mm")
        _correct(session, someone_else, "900 mm")

    with unit_of_work(factory) as session:
        found = ledger.by_vendor(session, "Vendor A", timedelta(days=30))
        assert [entry.corrected_value for entry in found] == ["1216 mm"]


def test_a_package_with_no_vendor_recorded_matches_no_vendor(postgres_engine: Engine) -> None:
    """`Package.vendor` is nullable, and a NULL matching nothing is the honest answer. Bucketing
    unattributed packages under some placeholder name would invent a vendor that does not exist."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        _correct(session, _scenario(session, vendor=None), "1216 mm")

    with unit_of_work(factory) as session:
        assert ledger.by_vendor(session, "", timedelta(days=30)) == []
        assert ledger.by_vendor(session, "unknown", timedelta(days=30)) == []
        assert session.scalars(select(CorrectionLedgerEntry)).all() != []


def test_a_correction_older_than_the_window_is_not_returned(postgres_engine: Engine) -> None:
    """The window is what makes "corrections this month" a different question from "ever".

    The old row is written with a backdated `created_at` rather than inserted and then aged, because
    it cannot be aged: the append-only trigger refuses the `UPDATE` that would do it. Writing the
    timestamp at insert is the only way to build this fixture, which is itself a demonstration that
    the table behaves as claimed.
    """
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session, rule_id="CT-WIDTH-001")
        session.add(
            CorrectionLedgerEntry(
                review_action_id=_action(session, scenario).id,
                action=ReviewActionKind.CORRECT.value,
                canonical_observation_id=scenario.observation_id,
                original_value="1219 mm",
                corrected_value="last year",
                created_at=utc_now() - timedelta(days=400),
            )
        )
        session.flush()
        _correct(session, scenario, "this week")

    with unit_of_work(factory) as session:
        recent = ledger.by_rule(session, "CT-WIDTH-001", timedelta(days=30))
        assert [entry.corrected_value for entry in recent] == ["this week"]

        everything = ledger.by_rule(session, "CT-WIDTH-001", timedelta(days=500))
        assert [entry.corrected_value for entry in everything] == ["last year", "this week"]


def test_results_are_ordered_oldest_first(postgres_engine: Engine) -> None:
    """Deterministic order, tie-broken on id. Two corrections written in the same transaction can
    share a timestamp, and an unordered result would shuffle between runs of the same report."""
    _upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as session:
        scenario = _scenario(session, rule_id="CT-WIDTH-001")
        for corrected in ("first", "second", "third"):
            _correct(session, scenario, corrected)

    with unit_of_work(factory) as session:
        found = ledger.by_rule(session, "CT-WIDTH-001", timedelta(days=30))
        assert [entry.created_at for entry in found] == sorted(entry.created_at for entry in found)
        assert len(found) == 3


# ---------------------------------------------------------------------------
# The window has to be a real window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", [timedelta(0), timedelta(days=-1)])
def test_a_window_that_cannot_contain_anything_is_refused(window: timedelta) -> None:
    """The failure mode worth catching: a zero or negative window returns an empty list, which reads
    as "no corrections" — indistinguishable from a clean month. A safety metric that reports good
    news when it was asked a nonsensical question is worse than one that fails.

    The session is never bound to an engine, which is the point: the refusal happens while the
    query is being built, before anything could reach a database.
    """
    with Session() as unbound:
        with pytest.raises(ValueError, match="positive duration"):
            ledger.by_rule(unbound, "CT-WIDTH-001", window)
        with pytest.raises(ValueError, match="positive duration"):
            ledger.by_check_type(unbound, "internal", window)
        with pytest.raises(ValueError, match="positive duration"):
            ledger.by_vendor(unbound, "Vendor A", window)
