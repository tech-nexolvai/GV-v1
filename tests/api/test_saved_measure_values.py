"""The Measurements form gets back what a reviewer already saved (#1074).

Verification for `saved_measurements` and `saved_parameters` on `GET .../required-inputs`
(`app/api/measurements.py:_saved_entries`). Before #1074 nothing returned a typed value, so a reload
showed the form empty although the values were stored and the checks were reading them.

The tests follow the issue: a saved value comes back after a reload, the latest entry wins, another
revision's or project's values never appear, settings keep their source, and the read is a fixed
number of queries however much was saved. Every value is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, event, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, PackageRevision, PackageState, Project
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.rules import RuleDefinition, RuleSnapshot
from rules.schema import Rule
from rules.snapshot import publish
from tests.app.postgres_fixture import alembic_config
from units.normalise import normalise_to_inches

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
OTHER_PROJECT = uuid4()
RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"

#: Two rules sharing the sink cutout width (`SHOP:CT012`), one with the sink cabinet width alone
#: (`SHOP:CT004`), plus a run-scope and a project-scope setting.
SINK_RULES = ("ct_sink_cabinet_width_001.yaml", "ct_sink_cutout_width_001.yaml")
CUTOUT = "SHOP:CT012"
SINK_CABINET = "SHOP:CT004"
CABINETS = "SHOP:cabinet_width"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


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


def _client(session: Session, *, actor: str = "anant") -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    principal = Principal(
        id=actor, roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT, OTHER_PROJECT})
    )
    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: principal
    return TestClient(app, raise_server_exceptions=False)


def _package(session: Session, project: UUID = PROJECT) -> UUID:
    if session.get(Project, project) is None:
        session.add(Project(id=project, name="saved values tests"))
        session.flush()
    package = Package(project_id=project, vendor="Synthetic Vendor")
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(
            package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
        )
    )
    session.commit()
    return package.id


def _publish(session: Session, *files: str) -> None:
    for name in files:
        rule = Rule.model_validate(yaml.safe_load((RULEBOOK / name).read_text(encoding="utf-8")))
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
    session.commit()


def _save(client: Any, package: UUID, *, project: UUID = PROJECT, **body: Any) -> dict[str, Any]:
    response = client.post(f"/api/v1/projects/{project}/packages/{package}/measurements", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


def _form(client: Any, package: UUID, *, project: UUID = PROJECT) -> dict[str, Any]:
    response = client.get(f"/api/v1/projects/{project}/packages/{package}/required-inputs")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _measured(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {saved["key"]: saved for saved in form["saved_measurements"]}


def _settings_saved(form: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {saved["name"]: saved for saved in form["saved_parameters"]}


def _cutout(value: str) -> list[dict[str, str]]:
    """The cutout width as the form saves it: once per rule input it feeds."""
    return [
        {"rule_id": "CT-SINK-CABINET-WIDTH-001", "name": "cutout_width", "value": value},
        {"rule_id": "CT-SINK-CUTOUT-WIDTH-001", "name": "cutout_width", "value": value},
    ]


def test_a_saved_measurement_comes_back_after_a_reload(session: Session) -> None:
    """Outcome: the cutout width typed as `30 1/2"` comes back on a fresh GET as exactly 61/2 in,
    written so the form can send it again unchanged, with who saved it and when, and marked complete
    because both checks that read it hold it."""
    _publish(session, *SINK_RULES)
    package = _package(session)
    client = _client(session)

    _save(client, package, measurements=_cutout('30 1/2"'))
    saved = _measured(_form(_client(session), package))

    assert set(saved) == {CUTOUT}
    (value,) = saved[CUTOUT]["values"]
    assert (value["numerator"], value["denominator"], value["unit"]) == ("61", "2", "in")
    assert value["text"] == '30 1/2"'
    assert normalise_to_inches(value["text"]).exact == normalise_to_inches('30 1/2"').exact
    assert (saved[CUTOUT]["set_by"], value["set_by"]) == ("anant", "anant")
    assert saved[CUTOUT]["set_at"] == value["set_at"]
    assert saved[CUTOUT]["complete"] is True


def test_a_millimetre_value_comes_back_as_the_exact_inches_it_became(session: Session) -> None:
    """The typed characters are not stored, only the number. Outcome: `984 mm` comes back as
    4920/127 in and as text that reads back to exactly that, never as a rounded decimal."""
    _publish(session, *SINK_RULES)
    package = _package(session)

    _save(_client(session), package, measurements=_cutout("984 mm"))
    (value,) = _measured(_form(_client(session), package))[CUTOUT]["values"]

    assert (value["numerator"], value["denominator"]) == ("4920", "127")
    assert normalise_to_inches(value["text"]).exact == normalise_to_inches("984 mm").exact
    assert "." not in value["text"]


def test_the_latest_entry_wins_and_an_untouched_field_stays(session: Session) -> None:
    """Monday: cutout 30" and sink cabinet 36". Tuesday, by someone else: cutout 31" alone.
    Outcome: the cutout is Tuesday's, by Tuesday's reviewer; the sink cabinet is still Monday's."""
    _publish(session, *SINK_RULES)
    package = _package(session)

    _save(
        _client(session, actor="monday"),
        package,
        measurements=[
            *_cutout('30"'),
            {"rule_id": "CT-SINK-CABINET-WIDTH-001", "name": "sink_cabinet_width", "value": '36"'},
        ],
    )
    _save(_client(session, actor="tuesday"), package, measurements=_cutout('31"'))
    saved = _measured(_form(_client(session), package))

    assert [v["text"] for v in saved[CUTOUT]["values"]] == ['31"']
    assert saved[CUTOUT]["set_by"] == "tuesday"
    assert [v["text"] for v in saved[SINK_CABINET]["values"]] == ['36"']
    assert saved[SINK_CABINET]["set_by"] == "monday"
    assert saved[SINK_CABINET]["set_at"] < saved[CUTOUT]["set_at"]


def test_a_quantity_saved_for_only_one_of_its_checks_is_not_complete(session: Session) -> None:
    """The cutout feeds two checks. Outcome: saved for one, it comes back but is not complete; saved
    for both with different numbers, the newer comes back and is still not complete."""
    _publish(session, *SINK_RULES)
    package = _package(session)
    client = _client(session)
    first, second = _cutout('30"')

    _save(client, package, measurements=[first])
    one = _measured(_form(client, package))[CUTOUT]
    _save(client, package, measurements=[{**second, "value": '32"'}])
    differing = _measured(_form(client, package))[CUTOUT]

    assert ([v["text"] for v in one["values"]], one["complete"]) == (['30"'], False)
    assert ([v["text"] for v in differing["values"]], differing["complete"]) == (['32"'], False)


def test_a_run_of_values_comes_back_in_layout_order(session: Session) -> None:
    """Outcome: three cabinets typed left to right come back left to right, each exact."""
    _publish(session, "ct_width_001.yaml")
    package = _package(session)
    widths = ['24"', '18 1/4"', '30"']

    _save(
        _client(session),
        package,
        measurements=[{"rule_id": "CT-WIDTH-001", "name": "cabinet_widths", "values": widths}],
    )
    saved = _measured(_form(_client(session), package))[CABINETS]

    assert [v["text"] for v in saved["values"]] == widths
    assert [(v["numerator"], v["denominator"]) for v in saved["values"]] == [
        ("24", "1"),
        ("73", "4"),
        ("30", "1"),
    ]
    assert saved["complete"] is True


def test_settings_keep_their_layer_source_reference_and_who(session: Session) -> None:
    """Outcome: the clearance comes back as the fabricator's, the sink width as this review's own,
    each with who typed it; a setting nobody saved is absent rather than empty."""
    _publish(session, *SINK_RULES)
    package = _package(session)

    _save(
        _client(session),
        package,
        parameters=[
            {
                "name": "sink_cutout_clearance",
                "value": '1/8"',
                "source": "Fabricator",
                "reference": "Cut sheet, note 2",
            },
            {"name": "sink_interior_width", "value": '28"', "scope": "run"},
        ],
    )
    saved = _settings_saved(_form(_client(session), package))

    clearance, width = saved["sink_cutout_clearance"], saved["sink_interior_width"]
    assert (clearance["layer"], clearance["source"], clearance["reference"]) == (
        "project",
        "Fabricator",
        "Cut sheet, note 2",
    )
    assert clearance["citation"] is None
    assert (clearance["value"]["numerator"], clearance["value"]["denominator"]) == ("1", "8")
    assert clearance["value"]["text"] == '1/8"'
    assert clearance["value"]["set_by"] == "anant"
    assert (width["layer"], width["value"]["text"]) == ("run", '28"')
    assert "cabinet_side_thickness" not in saved


def test_another_packages_or_projects_values_never_appear(session: Session) -> None:
    """Package A saves a cutout and a run setting; package B in the same project and package C in
    another project save nothing. Outcome: B and C get nothing of A's run values. A project setting
    is the project's by design, so B sees A's project setting and C does not."""
    _publish(session, *SINK_RULES)
    first, second = _package(session), _package(session)
    elsewhere = _package(session, OTHER_PROJECT)

    _save(
        _client(session),
        first,
        measurements=_cutout('30"'),
        parameters=[
            {"name": "sink_interior_width", "value": '28"', "scope": "run"},
            {"name": "sink_cutout_clearance", "value": '1/8"', "source": "Fabricator"},
        ],
    )
    sibling = _form(_client(session), second)
    foreign = _form(_client(session), elsewhere, project=OTHER_PROJECT)

    assert sibling["saved_measurements"] == []
    assert set(_settings_saved(sibling)) == {"sink_cutout_clearance"}
    assert foreign["saved_measurements"] == []
    assert foreign["saved_parameters"] == []


def test_an_earlier_revisions_values_never_appear(session: Session) -> None:
    """The vendor resubmits: revision 2 replaces revision 1. Outcome: the form for revision 2 shows
    none of the measurements or run settings saved for revision 1."""
    _publish(session, *SINK_RULES)
    package = _package(session)
    _save(
        _client(session),
        package,
        measurements=_cutout('30"'),
        parameters=[{"name": "sink_interior_width", "value": '28"', "scope": "run"}],
    )

    session.add(
        PackageRevision(package_id=package, revision_number=2, state=PackageState.AWAITING_REVIEW)
    )
    session.commit()
    form = _form(_client(session), package)

    assert form["saved_measurements"] == []
    assert form["saved_parameters"] == []


def test_a_package_in_a_project_the_caller_names_wrongly_is_not_found(session: Session) -> None:
    """Asking for project B's package under project A is the same 404 as a package that does not
    exist, and leaks none of its saved values."""
    _publish(session, *SINK_RULES)
    elsewhere = _package(session, OTHER_PROJECT)
    _save(_client(session), elsewhere, project=OTHER_PROJECT, measurements=_cutout('30"'))

    response = _client(session).get(
        f"/api/v1/projects/{PROJECT}/packages/{elsewhere}/required-inputs"
    )

    assert response.status_code == 404
    assert "saved_measurements" not in response.text


def test_reading_the_form_writes_nothing(session: Session) -> None:
    """Read-only: no parameter set is minted by looking at the form."""
    _publish(session, *SINK_RULES)
    package = _package(session)
    _save(_client(session), package, measurements=_cutout('30"'))

    def sets() -> int:
        return session.execute(select(func.count()).select_from(StoredParameterSet)).scalar_one()

    before = sets()
    _form(_client(session), package)
    _form(_client(session), package)

    assert sets() == before


def _statements(session: Session, client: Any, package: UUID) -> int:
    seen: list[str] = []

    def count(*args: Any) -> None:
        seen.append(str(args[2]))

    bind = session.get_bind()
    event.listen(bind, "before_cursor_execute", count)
    try:
        _form(client, package)
    finally:
        event.remove(bind, "before_cursor_execute", count)
    return len(seen)


def test_the_read_is_a_fixed_number_of_queries(session: Session) -> None:
    """No query per saved value: one saved field and a dozen cost the same."""
    _publish(session, *SINK_RULES, "ct_width_001.yaml")
    small, large = _package(session), _package(session)
    client = _client(session)

    _save(client, small, measurements=_cutout('30"'))
    _save(
        client,
        large,
        measurements=[
            *_cutout('30"'),
            {"rule_id": "CT-SINK-CABINET-WIDTH-001", "name": "sink_cabinet_width", "value": '36"'},
            {
                "rule_id": "CT-WIDTH-001",
                "name": "cabinet_widths",
                "values": [f'{n}"' for n in range(12, 24)],
            },
        ],
        parameters=[
            {"name": "sink_interior_width", "value": '28"', "scope": "run"},
            {"name": "sink_cutout_clearance", "value": '1/8"', "source": "Fabricator"},
        ],
    )

    assert _statements(session, client, small) == _statements(session, client, large)
