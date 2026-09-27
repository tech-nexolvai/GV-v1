"""A reviewer says which cabinet is the sink cabinet, and it reaches the check (#684).

The storage guarantees this file exists to pin: order survives, a correction supersedes without
deleting, an unanswered position is left absent rather than filled, and what comes out is a sealed
operand attributed to the person who supplied it.
"""

from __future__ import annotations

from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.db.session import session_factory
from app.models import Package, PackageRevision, PackageState, Project
from app.models.evidence import ItemClassification
from tests.app.postgres_fixture import alembic_config
from verdict.operands import EvidenceStatus
from workflow.classifications import (
    classification_operands,
    confirmed_classifications,
    record_classifications,
)

pytest_plugins = ("tests.app.postgres_fixture",)


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


@pytest.fixture
def revision_id(session: Session) -> UUID:
    project = Project(name="classification")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    return revision.id


RULE = "CAB-FILLER-001"
INPUT = "cabinet_type"


def _record(session: Session, revision_id: UUID, *categories: str, by: str = "reviewer") -> None:
    record_classifications(
        session,
        package_revision_id=revision_id,
        rule_id=RULE,
        input_name=INPUT,
        categories=categories,
        confirmed_by=by,
    )
    session.flush()


def test_the_run_comes_back_in_the_order_it_was_entered(
    session: Session, revision_id: UUID
) -> None:
    """Order is the run left to right, and the distribution adjusts positionally.

    A reversal would move the wrong cabinets and hold the wrong one fixed, and the totals would
    still add up — so this is exactly the failure that would not announce itself.
    """
    _record(session, revision_id, "single_door", "equipment", "drawer")

    assert confirmed_classifications(session, revision_id) == {
        RULE: {INPUT: ("single_door", "equipment", "drawer")}
    }


def test_a_correction_supersedes_without_deleting_what_it_replaced(
    session: Session, revision_id: UUID
) -> None:
    """Append-only: a finding cites the version that judged it (ADR-0016)."""
    _record(session, revision_id, "single_door", "equipment", "drawer")
    _record(session, revision_id, "double_door", "equipment", "drawer", by="second reviewer")

    assert confirmed_classifications(session, revision_id)[RULE][INPUT] == (
        "double_door",
        "equipment",
        "drawer",
    )
    assert session.query(ItemClassification).count() == 6, "the earlier answer is still on record"


def test_correcting_one_cabinet_does_not_drop_the_others(
    session: Session, revision_id: UUID
) -> None:
    """Latest *per position*, not per run.

    A reviewer who fixes one cabinet writes one row. Resolving the newest row for the whole run
    would leave a one-cabinet run, which the operation then abstains on — a correction that quietly
    unanswered two other questions.
    """
    _record(session, revision_id, "single_door", "equipment", "drawer")
    record_classifications(
        session,
        package_revision_id=revision_id,
        rule_id=RULE,
        input_name=INPUT,
        categories=("double_door",),
        confirmed_by="reviewer",
    )
    session.flush()

    assert confirmed_classifications(session, revision_id)[RULE][INPUT] == (
        "double_door",
        "equipment",
        "drawer",
    )


def test_two_rules_asking_the_same_question_are_kept_apart(
    session: Session, revision_id: UUID
) -> None:
    """Keyed by rule and input, as measurements are: two rules may each declare `cabinet_type`."""
    _record(session, revision_id, "single_door")
    record_classifications(
        session,
        package_revision_id=revision_id,
        rule_id="SOME-OTHER-RULE",
        input_name=INPUT,
        categories=("equipment",),
        confirmed_by="reviewer",
    )
    session.flush()

    confirmed = confirmed_classifications(session, revision_id)
    assert confirmed[RULE][INPUT] == ("single_door",)
    assert confirmed["SOME-OTHER-RULE"][INPUT] == ("equipment",)


def test_what_reaches_a_check_is_sealed_and_attributed(session: Session, revision_id: UUID) -> None:
    """A reviewer's answer is `HUMAN_CONFIRMED` and names where it came from.

    Not because a category could be misread — nobody read it — but because a finding has to be able
    to say who decided this cabinet could not move.
    """
    _record(session, revision_id, "single_door", "equipment", by="a reviewer")

    operand = classification_operands(session, revision_id)[RULE][INPUT]

    assert operand.value == ("single_door", "equipment")
    assert operand.status is EvidenceStatus.HUMAN_CONFIRMED
    assert operand.source == "USER_INPUT"
    assert operand.evidence_ref is not None and RULE in operand.evidence_ref


def test_a_package_nobody_classified_yields_nothing_rather_than_a_default(
    session: Session, revision_id: UUID
) -> None:
    """The absence has to stay an absence.

    An empty run, or a guessed "regular", would let the distribution resize a cabinet nobody looked
    at — and if that cabinet were the sink cabinet, it is the one failure slide 3 exists to prevent.
    """
    assert confirmed_classifications(session, revision_id) == {}
    assert classification_operands(session, revision_id) == {}


def test_the_database_refuses_an_edit(session: Session, revision_id: UUID) -> None:
    """Append-only is the schema's rule, not only this module's.

    `gv_reject_mutation` is on the table, so a correction has to be another row even for code that
    never read this file.
    """
    _record(session, revision_id, "single_door")
    stored = session.query(ItemClassification).one()

    stored.category = "equipment"
    with pytest.raises(Exception, match="append-only|gv_reject_mutation|immutable"):
        session.flush()
    session.rollback()
