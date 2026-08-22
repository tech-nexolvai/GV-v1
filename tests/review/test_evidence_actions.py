"""Issue #230: named human decisions create new trusted facts, never edits."""

from __future__ import annotations

from fractions import Fraction
from uuid import UUID, uuid4

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.auth import Principal, Role
from app.db.session import session_factory, unit_of_work
from app.models import (
    CanonicalObservation,
    CorrectionLedgerEntry,
    EvidenceSupportingCandidate,
    FindingEvidence,
    ReviewActionKind,
)
from app.review.evidence_actions import (
    EvidenceConfirmationNotAuthorised,
    confirm_evidence,
    confirmed_for_same_evidence,
    correct_evidence,
)
from tests.app.postgres_fixture import alembic_config
from tests.review.test_ledger import _scenario
from verdict.operands import EvidenceStatus

pytest_plugins = ("tests.app.postgres_fixture",)


def upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


def reviewer(name: str = "anant") -> Principal:
    return Principal(name, frozenset({Role.REVIEWER}))


def link_finding_to_observation(db: Session, finding_id: UUID, observation_id: UUID) -> None:
    # Kept as a tiny fixture helper; production code verifies this server-side relationship.
    db.add(
        FindingEvidence(
            finding_id=finding_id,
            canonical_observation_id=observation_id,
            role="operand",
        )
    )
    db.flush()


def test_confirmation_names_the_human_and_creates_a_new_trusted_fact(
    postgres_engine: Engine,
) -> None:
    """Input: authorised reviewer confirms evidence. Outcome: named action plus new trusted row."""

    upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        scenario = _scenario(db)
        link_finding_to_observation(db, scenario.finding_id, scenario.observation_id)
        decision = confirm_evidence(
            db,
            principal=reviewer(),
            review_session_id=scenario.review_session_id,
            finding_id=scenario.finding_id,
            observation_id=scenario.observation_id,
        )
        assert decision.action.actor == "anant"
        assert decision.action.action == ReviewActionKind.CONFIRM.value
        assert decision.action.original_observation_id == scenario.observation_id
        assert decision.action.resulting_observation_id == decision.resulting.id
        assert decision.resulting.id != decision.original.id
        assert decision.resulting.status == EvidenceStatus.HUMAN_CONFIRMED.value


def test_correction_preserves_original_and_writes_ledger_in_the_transaction(
    postgres_engine: Engine,
) -> None:
    """Input: 1/3 inch corrected to 1/2. Outcome: both immutable facts and one ledger row."""

    upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        scenario = _scenario(db)
        link_finding_to_observation(db, scenario.finding_id, scenario.observation_id)
        decision = correct_evidence(
            db,
            principal=reviewer("keyur"),
            review_session_id=scenario.review_session_id,
            finding_id=scenario.finding_id,
            observation_id=scenario.observation_id,
            corrected_value=Fraction(1, 2),
        )
        ledger = db.scalars(
            select(CorrectionLedgerEntry).where(
                CorrectionLedgerEntry.review_action_id == decision.action.id
            )
        ).one()
        assert decision.original.value_numerator == 1
        assert decision.original.value_denominator == 3
        assert decision.resulting.value_numerator == 1
        assert decision.resulting.value_denominator == 2
        assert '"value":"1/3"' in ledger.original_value
        assert '"value":"1/2"' in ledger.corrected_value
        assert decision.action.actor == "keyur"


def test_confirmation_carries_to_a_rerun_of_identical_evidence(postgres_engine: Engine) -> None:
    """Input: duplicate immutable reading after confirmation. Outcome: prior human result reused."""

    upgrade(postgres_engine)
    factory = session_factory(postgres_engine)
    with unit_of_work(factory) as db:
        scenario = _scenario(db)
        link_finding_to_observation(db, scenario.finding_id, scenario.observation_id)
        confirmed = confirm_evidence(
            db,
            principal=reviewer(),
            review_session_id=scenario.review_session_id,
            finding_id=scenario.finding_id,
            observation_id=scenario.observation_id,
        ).resulting
        original = db.get(CanonicalObservation, scenario.observation_id)
        assert original is not None
        rerun = CanonicalObservation(
            document_version_id=original.document_version_id,
            page_id=original.page_id,
            document_role=original.document_role,
            polygon=original.polygon,
            coordinate_space=original.coordinate_space,
            semantic_type=original.semantic_type,
            value_numerator=original.value_numerator,
            value_denominator=original.value_denominator,
            unit=original.unit,
            status=original.status,
            authority=original.authority,
            evidence_crop_uri=original.evidence_crop_uri,
        )
        db.add(rerun)
        db.flush()
        for link in db.scalars(
            select(EvidenceSupportingCandidate).where(
                EvidenceSupportingCandidate.canonical_observation_id == original.id
            )
        ):
            db.add(
                EvidenceSupportingCandidate(
                    canonical_observation_id=rerun.id,
                    candidate_id=link.candidate_id,
                    role=link.role,
                )
            )
        db.flush()

        carried = confirmed_for_same_evidence(db, observation_id=rerun.id)
        assert carried is not None
        assert carried.id == confirmed.id


def test_unauthorised_principal_is_refused_before_any_database_lookup() -> None:
    """Input: rule admin. Outcome: refusal before ids are inspected or anything is written."""

    class DatabaseMustNotBeTouched:
        def get(self, *args: object, **kwargs: object) -> object:
            raise AssertionError("authorization must happen before database access")

    try:
        confirm_evidence(
            DatabaseMustNotBeTouched(),  # type: ignore[arg-type]
            principal=Principal("rule-author", frozenset({Role.RULE_ADMIN})),
            review_session_id=uuid4(),
            finding_id=uuid4(),
            observation_id=uuid4(),
        )
    except EvidenceConfirmationNotAuthorised:
        pass
    else:
        raise AssertionError("a rule admin must not gain evidence-confirmation authority")
