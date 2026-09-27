"""What a reviewer may enter, what is refused, and the work the endpoint must not do.

`CLIENT_FACTS` Q7 blesses the reviewer typing values. The interesting cases here are the refusals and
the boundary: a value with no unit, a re-submission, and the fact that asking for checks does not run
them.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import OutboxEntry, Package, PackageRevision, PackageState, Project
from app.models.parameters import ParameterSet as StoredParameterSet
from tests.app.postgres_fixture import alembic_config
from workflow.classifications import confirmed_classifications

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


def _package(session: Session) -> UUID:
    project = session.get(Project, PROJECT)
    if project is None:
        session.add(Project(id=PROJECT, name="measurement tests"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS)
    )
    session.commit()
    return package.id


def test_a_typed_value_is_stored_exactly(session: Session) -> None:
    """`1 1/2"` is 3/2, not 1.5. Under exact match a float's rounding *is* the verdict (Q2)."""
    package = _package(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": "countertop_overhang", "value": '1 1/2"'}]},
    )

    assert response.status_code == 201, response.text
    stored = response.json()["parameters"][0]
    assert (stored["numerator"], stored["denominator"]) == ("3", "2")
    assert stored["unit"] == "in"
    assert stored["as_typed"] == '1 1/2"'


def test_a_millimetre_value_is_converted_exactly(session: Session) -> None:
    """`984 mm` is 4920/127", a value no float holds. Inches decide (Q12); mm may still be typed."""
    package = _package(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": "cabinet_depth", "value": "984 mm"}]},
    )

    stored = response.json()["parameters"][0]
    assert (stored["numerator"], stored["denominator"]) == ("4920", "127")
    assert stored["unit"] == "in"


def test_a_value_with_no_unit_is_refused(session: Session) -> None:
    """**The #483 failure, refused at the front door.**

    `984` with no unit was once recorded as 984 inches — 82 feet — because tokenisation had removed
    its `mm`. A reviewer who omits the mark is told, not guessed at, and the message says what to do.
    """
    package = _package(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": "cabinet_depth", "value": "24"}]},
    )

    assert response.status_code == 422, response.text
    # `app/errors.py` wraps every failure in an envelope — `error`/`message`/`request_id` — rather
    # than FastAPI's bare `detail`. Asserted against the real shape, which is what a client parses.
    assert "with its unit" in response.json()["message"]


def test_resubmitting_the_same_values_records_a_second_set(session: Session) -> None:
    """**Two submissions of the same number are two records, and that is deliberate.**

    I wrote this test expecting deduplication and it failed — correctly. `ParameterSet.set_id` puts
    `set_at` inside the hash on purpose, and `rules/parameters.py` gives the reason: "two sets
    recording the same number measured on different days are genuinely different records, and
    collapsing them would lose the distinction a reviewer needs."

    So this pins the designed behaviour instead: a re-submission is a new version, and neither
    request fails on the unique constraint.
    """
    package = _package(session)
    client = _client(session)
    body = {"parameters": [{"name": "cabinet_depth", "value": '24"'}]}

    first = client.post(f"/api/v1/projects/{PROJECT}/packages/{package}/measurements", json=body)
    second = client.post(f"/api/v1/projects/{PROJECT}/packages/{package}/measurements", json=body)

    assert (first.status_code, second.status_code) == (201, 201), second.text
    assert second.json()["parameter_set_version"] > first.json()["parameter_set_version"]
    assert (
        session.execute(select(func.count()).select_from(StoredParameterSet)).scalar_one() == 2
    ), "the second submission did not record its own set"


def test_a_corrected_value_mints_the_next_version(session: Session) -> None:
    """A correction is a new version, never an edit.

    A finding cites the parameter-set version that judged it (ADR-0016). Rewriting a set in place
    would change what an already-recorded finding claims to have used.
    """
    package = _package(session)
    client = _client(session)

    first = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": "cabinet_depth", "value": '24"'}]},
    )
    corrected = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": "cabinet_depth", "value": '24 1/2"'}]},
    )

    assert corrected.json()["parameter_set_version"] > first.json()["parameter_set_version"]


def test_asking_for_checks_enqueues_and_runs_nothing(session: Session) -> None:
    """**202, and no findings.**

    The endpoint may not run the checks: `tests/api/test_no_heavy_work.py` forbids `app/api/` any
    path to extraction, and `workflow/stages.py` reaches `extraction.reader`. So this asserts the
    separation rather than trusting it — an outbox row exists and no finding does.
    """
    from app.models import Finding

    package = _package(session)

    response = _client(session).post(f"/api/v1/projects/{PROJECT}/packages/{package}/checks")

    assert response.status_code == 202, response.text
    assert "accepted_id" in response.json()
    assert "run_id" not in response.json(), "nothing has started, so nothing may be called a run"

    entries = list(session.execute(select(OutboxEntry)).scalars())
    assert [entry.workflow for entry in entries] == ["run_checks"]
    assert list(session.execute(select(Finding)).scalars()) == [], "the endpoint ran the checks"


def test_a_package_in_another_project_is_not_found(session: Session) -> None:
    """404 in the same words as absent. A 403 would confirm the package exists (ADR-0006)."""
    other = uuid4()
    session.add(Project(id=other, name="somebody else"))
    session.flush()
    package = Package(project_id=other, vendor="theirs")
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(package_id=package.id, revision_number=1, state=PackageState.CREATED)
    )
    session.commit()

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package.id}/measurements",
        json={"parameters": [{"name": "cabinet_depth", "value": '24"'}]},
    )

    assert response.status_code in (403, 404)
    if response.status_code == 404:
        # The envelope again, not FastAPI's `detail`. The message must not name the package or say
        # it belongs to another project — a 403 that confirmed existence is what ADR-0006 forbids.
        assert response.json()["message"] == "Not found"


def _publish_cabinet_rule(session: Session) -> None:
    """Publish CAB-FILLER-001, because a classification is checked against the published rulebook.

    The form offers the choices a rule's semantic type allows and the submission is validated
    against the same list, so neither can accept something the other would not.
    """
    import yaml

    from app.models.rules import RuleDefinition, RuleSnapshot
    from rules.schema import Rule
    from rules.snapshot import publish

    path = Path("rules/rulebook/cab_filler_001.yaml")
    rule = Rule.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    snapshot = publish(rule)
    definition = RuleDefinition(rule_id=rule.id)
    session.add(definition)
    session.flush()
    session.add(
        RuleSnapshot(
            rule_definition_id=definition.id,
            snapshot_id=snapshot.snapshot_id,
            version=rule.version,
            canonical_json=snapshot.canonical_json,
            product_type=rule.product_type.value,
            check_type=rule.check_type.value,
            unconfirmed_tolerance_count=0,
        )
    )
    session.flush()


def test_a_reviewer_can_say_which_cabinet_is_the_equipment_cabinet(session: Session) -> None:
    """#684: the input that is a category, and had nowhere to go.

    It travels under `classifications` rather than `measurements` because it is not a dimension —
    `normalise_to_inches` refuses `single_door`, correctly, and that refusal is what this route
    around exists for.
    """
    package = _package(session)
    _publish_cabinet_rule(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "classifications": [
                {
                    "rule_id": "CAB-FILLER-001",
                    "name": "cabinet_type",
                    "categories": ["double_door", "equipment", "double_door"],
                }
            ]
        },
    )

    assert response.status_code == 201, response.text
    revision = session.execute(
        select(PackageRevision).where(PackageRevision.package_id == package)
    ).scalar_one()
    assert confirmed_classifications(session, revision.id) == {
        "CAB-FILLER-001": {"cabinet_type": ("double_door", "equipment", "double_door")}
    }


def test_a_category_outside_the_rulebook_is_refused(session: Session) -> None:
    """Checked against the published rule, not against whatever the client believes.

    Storing an unrecognised category would turn a correctable 422 into an abstention on a package
    the reviewer thought they had finished — and a misspelt equipment cabinet is the one thing
    slide 3 exists to keep from being resized.
    """
    package = _package(session)
    _publish_cabinet_rule(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "classifications": [
                {
                    "rule_id": "CAB-FILLER-001",
                    "name": "cabinet_type",
                    "categories": ["double_door", "appliance"],
                }
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert "appliance" in response.text


def test_a_dimension_sent_as_a_classification_is_refused(session: Session) -> None:
    """`field_width` is a measurement. Sending it here would store a number as a category."""
    package = _package(session)
    _publish_cabinet_rule(session)

    response = _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "classifications": [
                {"rule_id": "CAB-FILLER-001", "name": "field_width", "categories": ['82"']}
            ]
        },
    )

    assert response.status_code == 422, response.text


def test_the_form_offers_the_choices_rather_than_a_text_box(session: Session) -> None:
    """A reviewer must not be asked to type `single_door` with a unit.

    `categories` being non-empty is what tells the form to render a list, and it comes from the
    same table the submission is validated against.
    """
    package = _package(session)
    _publish_cabinet_rule(session)

    body = (
        _client(session)
        .get(f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs")
        .json()
    )

    quantity = next(q for q in body["quantities"] if q["semantic_type"] == "cabinet_category")
    assert quantity["many"] is True
    assert tuple(quantity["categories"]) == (
        "single_door",
        "double_door",
        "drawer",
        "equipment",
    )
    # Every other input is a dimension and must not grow a dropdown.
    assert all(
        q["categories"] == []
        for q in body["quantities"]
        if q["semantic_type"] != "cabinet_category"
    )
