"""A number drawn on the drawing is confirmed from a reading, never typed (#1251).

GV's rule, decided 2026-10-12 (#1181): people confirm readings; they do not type numbers drawn on the
vendor's or the architect's drawing. The waiting card already offers no box for them. This holds the
Measurements endpoint to the same rule, so a typed countertop depth can no longer reach the checks
and beat a reading.

What stays typable: the reviewer's own entries (`USER_INPUT`, e.g. the site width), the typed lists
(cabinet runs, #1205), and the settings, including the sink's spec-sheet size.

Values typed before this change stay in the database (append-only) and still count; nothing new of
that kind can be typed.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import yaml
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_artifact_store, get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import Package, PackageRevision, PackageState, Project
from app.models.parameters import ParameterSet as StoredParameterSet
from app.models.parameters import to_rows
from rules.parameters import ParameterLayer, ParameterSet, ParameterValue, Provenance
from rules.schema import Quantity
from storage.local import LocalStore
from tests.app.postgres_fixture import alembic_config
from units.measurement import Measurement, Unit
from workflow.measurements import operands_for

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
RULEBOOK = Path(__file__).resolve().parents[2] / "rules" / "rulebook"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


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


def _client(session: Session, *, store: LocalStore | None = None, project: UUID = PROJECT) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: Principal(
        id="reviewer-a", roles=frozenset({Role.ADMIN}), projects=frozenset({project})
    )
    if store is not None:
        app.dependency_overrides[get_artifact_store] = lambda: store
    return TestClient(app, raise_server_exceptions=False)


def _publish(session: Session, *files: str) -> None:
    """Publish rule files as the checks read them: the refusal follows the published rulebook."""
    from app.models.rules import RuleDefinition, RuleSnapshot
    from rules.schema import Rule
    from rules.snapshot import publish

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
    session.flush()


ALL_RULES = (
    "ct_depth_001.yaml",
    "ct_width_001.yaml",
    "cab_filler_001.yaml",
    "ct_sink_cutout_width_001.yaml",
    "ct_sink_offset_front_001.yaml",
)


def _package(session: Session) -> UUID:
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="confirm-only tests"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Example Stone Co")
    session.add(package)
    session.flush()
    session.add(
        PackageRevision(package_id=package.id, revision_number=1, state=PackageState.NEEDS_INPUT)
    )
    session.commit()
    return package.id


def _revision(session: Session, package: UUID) -> PackageRevision:
    return session.execute(
        select(PackageRevision).where(PackageRevision.package_id == package)
    ).scalar_one()


def _post(session: Session, package: UUID, body: dict[str, Any]) -> Any:
    return _client(session).post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements", json=body
    )


def _run_sets(session: Session) -> int:
    return session.execute(
        select(func.count())
        .select_from(StoredParameterSet)
        .where(StoredParameterSet.layer == ParameterLayer.RUN.value)
    ).scalar_one()


@pytest.mark.parametrize(
    ("rule_id", "name", "drawing"),
    [
        ("CT-DEPTH-001", "countertop_depth", "vendor's drawing"),
        ("CT-SINK-CUTOUT-WIDTH-001", "cutout_width", "vendor's drawing"),
        ("CT-SINK-OFFSET-FRONT-001", "front_offset", "vendor's drawing"),
        ("CT-WIDTH-001", "countertop_width", "vendor's drawing"),
        ("CAB-FILLER-001", "design_width", "architect's drawing"),
    ],
)
def test_a_single_number_drawn_on_a_drawing_is_refused_with_a_plain_reason(
    session: Session, rule_id: str, name: str, drawing: str
) -> None:
    """422, nothing stored, and a sentence a reviewer can act on: confirm a reading instead."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session, package, {"measurements": [{"rule_id": rule_id, "name": name, "value": '25"'}]}
    )

    assert response.status_code == 422, response.text
    message = response.json()["message"]
    assert f"{rule_id}.{name}" in message
    assert drawing in message
    assert "confirm" in message.lower()
    # It says where the number is drawn, and never claims a drawing was not supplied: nothing here
    # knows that (#1253 review).
    assert "not supplied" not in message
    assert _run_sets(session) == 0


def test_a_drawn_single_sent_as_a_list_is_refused_too(session: Session) -> None:
    """The rule follows what the input is, not which JSON field carried it."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session,
        package,
        {
            "measurements": [
                {"rule_id": "CT-DEPTH-001", "name": "countertop_depth", "values": ['25"']}
            ]
        },
    )

    assert response.status_code == 422, response.text
    assert _run_sets(session) == 0


def test_one_refused_entry_stores_nothing_from_the_whole_request(session: Session) -> None:
    """A setting and a site width sent beside a typed depth are not half-saved."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session,
        package,
        {
            "parameters": [{"name": "sink_interior_width", "value": '15"', "scope": "run"}],
            "measurements": [
                {"rule_id": "CAB-FILLER-001", "name": "field_width", "value": '120"'},
                {"rule_id": "CT-DEPTH-001", "name": "countertop_depth", "value": '25"'},
            ],
        },
    )

    assert response.status_code == 422, response.text
    assert _run_sets(session) == 0


def test_settings_the_sink_size_site_width_and_typed_lists_stay_allowed(session: Session) -> None:
    """What a reviewer supplies is still typed: settings, the sink spec size, USER_INPUT, lists."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session,
        package,
        {
            "parameters": [
                {"name": "sink_interior_width", "value": '15"', "scope": "run"},
                {"name": "countertop_overhang", "value": '1"', "source": "G.C / Client"},
            ],
            "measurements": [
                {"rule_id": "CAB-FILLER-001", "name": "field_width", "value": '120"'},
                {"rule_id": "CAB-FILLER-001", "name": "shop_cabinets", "values": ['30"', '36"']},
                {"rule_id": "CAB-FILLER-001", "name": "architectural_cabinets", "values": ['30"']},
                {"rule_id": "CT-WIDTH-001", "name": "cabinet_widths", "values": ['30"', '36"']},
            ],
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert [m["name"] for m in body["measurements"]] == ["CAB-FILLER-001:field_width"]
    assert {entry["name"] for entry in body["lists"]} == {
        "CAB-FILLER-001:architectural_cabinets",
        "CAB-FILLER-001:shop_cabinets",
        "CT-WIDTH-001:cabinet_widths",
    }


def test_the_form_marks_drawn_single_values_confirm_only(session: Session) -> None:
    """The screen is told which quantities take no box, from the same rule the save path uses."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _client(session).get(
        f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs"
    )

    assert response.status_code == 200, response.text
    confirm_only = {q["key"]: q["confirm_only"] for q in response.json()["quantities"]}
    assert confirm_only["SHOP:CT010"] is True
    assert confirm_only["SHOP:CT012"] is True
    assert confirm_only["SHOP:CT007"] is True
    assert confirm_only["SHOP:countertop_overall_width"] is True
    assert confirm_only["ARCH:CT001"] is True
    assert confirm_only["SHOP:cabinet_width"] is False
    assert confirm_only["ARCH:cabinet_width"] is False
    assert confirm_only["USER_INPUT:field_dimension"] is False
    assert confirm_only["USER_INPUT:cabinet_category"] is False


def _counted_depth(session: Session, package: UUID) -> Fraction:
    """The countertop depth the checks would use from the reviewer's values today."""
    value = operands_for(session, _revision(session, package).id)["CT-DEPTH-001"][
        "countertop_depth"
    ].value
    assert isinstance(value, Measurement)
    return value.exact


def _typed_before(session: Session, package: UUID) -> None:
    """A countertop depth typed before #1251, written the way the endpoint used to write it."""
    revision = _revision(session, package)
    earlier = ParameterSet(
        project_id=str(PROJECT),
        layer=ParameterLayer.RUN,
        version=1,
        parameters={
            "CT-DEPTH-001:countertop_depth": ParameterValue(
                value=Quantity(value=Fraction(51, 2), unit=Unit("in")),
                provenance=Provenance.MEASURED,
                set_by="reviewer-a",
                set_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
        },
    )
    stored, rows = to_rows(earlier, package_revision_id=revision.id)
    session.add(stored)
    for row in rows:
        session.add(row)
    session.commit()


def test_a_value_typed_before_the_rule_still_counts_and_survives_later_saves(
    session: Session,
) -> None:
    """Append-only: nothing typed earlier is removed, and a later save carries it forward."""
    _publish(session, *ALL_RULES)
    package = _package(session)
    _typed_before(session, package)

    later = _post(
        session,
        package,
        {"parameters": [{"name": "sink_interior_width", "value": '15"', "scope": "run"}]},
    )
    assert later.status_code == 201, later.text
    assert _run_sets(session) == 2  # a new version, carrying the earlier depth forward

    assert _counted_depth(session, package) == Fraction(51, 2)

    form = _client(session).get(f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs")
    saved = {q["key"]: q for q in form.json()["saved_measurements"]}
    assert len(saved["SHOP:CT010"]["values"]) == 1


def test_a_value_typed_before_cannot_be_replaced_by_typing(session: Session) -> None:
    """Nothing new is typable, including a correction of an earlier typed drawn value."""
    _publish(session, *ALL_RULES)
    package = _package(session)
    _typed_before(session, package)

    response = _post(
        session,
        package,
        {"measurements": [{"rule_id": "CT-DEPTH-001", "name": "countertop_depth", "value": '26"'}]},
    )

    assert response.status_code == 422, response.text
    assert _counted_depth(session, package) == Fraction(51, 2)


# **Only what a published check declares, in the shape it declares, can be typed** (#1253 review).
#
# The refusal above matches the exact (rule, input) pair, so a name the rulebook never declared
# walked past it: `countertop_depth#0` was stored as a one-item list for `countertop_depth`, which
# outranks any reading and then crashed the checks for the whole revision on the scalar's arity.
# Lists are sent as `values` under the plain input name; the server alone writes `#0`, `#1`, ... and
# the `rule:` prefix, so a name carrying either is never a legitimate entry.


@pytest.mark.parametrize(
    ("rule_id", "name"),
    [
        ("CT-DEPTH-001", "countertop_depth#0"),
        ("CT-DEPTH-001", "countertop_depth:0"),
        ("CAB-FILLER-001", "field_width#0"),
        ("CT-WIDTH-001", "cabinet_widths#3"),
        ("CT-DEPTH-001:countertop_depth", "x"),
        ("CAB-FILLER-001#0", "field_width"),
    ],
)
def test_a_name_carrying_the_stored_key_markers_is_refused(
    session: Session, rule_id: str, name: str
) -> None:
    """`#` and `:` are the server's own markers in a stored key; a sent name never carries them."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session, package, {"measurements": [{"rule_id": rule_id, "name": name, "value": '25"'}]}
    )

    assert response.status_code == 422, response.text
    assert _run_sets(session) == 0


@pytest.mark.parametrize(
    ("rule_id", "name"),
    [
        ("CT-DEPTH-001", "made_up_input"),
        ("NO-SUCH-RULE-001", "field_width"),
        # Declared by a rule file, but that rule is not published here.
        ("CT-SINK-CABINET-WIDTH-001", "sink_cabinet_width"),
    ],
)
def test_an_input_no_published_check_declares_is_refused(
    session: Session, rule_id: str, name: str
) -> None:
    """A value no published check reads would sit in the store unasked-for; it is refused instead."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(
        session, package, {"measurements": [{"rule_id": rule_id, "name": name, "value": '25"'}]}
    )

    assert response.status_code == 422, response.text
    assert f"{rule_id}.{name}" in response.json()["message"]
    assert _run_sets(session) == 0


@pytest.mark.parametrize(
    "entry",
    [
        # A list input sent one value: the check would compare a total against one cabinet.
        {"rule_id": "CT-WIDTH-001", "name": "cabinet_widths", "value": '30"'},
        {"rule_id": "CAB-FILLER-001", "name": "shop_cabinets", "value": '30"'},
        # A single input sent as a list: the check would refuse a tuple for a scalar and crash.
        {"rule_id": "CAB-FILLER-001", "name": "field_width", "values": ['120"']},
        # A category sent as a dimension: each cabinet's type travels as a classification.
        {"rule_id": "CAB-FILLER-001", "name": "cabinet_type", "values": ['30"']},
    ],
)
def test_a_value_in_the_wrong_shape_for_its_input_is_refused(
    session: Session, entry: dict[str, Any]
) -> None:
    """One value for a list, a list for one value, or a number for a category: 422, nothing kept."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _post(session, package, {"measurements": [entry]})

    assert response.status_code == 422, response.text
    assert f"{entry['rule_id']}.{entry['name']}" in response.json()["message"]
    assert _run_sets(session) == 0


def test_the_form_says_where_a_drawn_value_is_read(session: Session) -> None:
    """A confirm-only field names its view, never "the section drawing" for every one of them."""
    _publish(session, *ALL_RULES)
    package = _package(session)

    response = _client(session).get(
        f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs"
    )

    assert response.status_code == 200, response.text
    where = {q["key"]: q["where_to_find"] for q in response.json()["quantities"]}
    assert "plan view" in where["SHOP:CT012"]
    assert "vendor's" in where["SHOP:countertop_overall_width"].lower()
    assert "architect's" in where["ARCH:CT001"].lower()
    for key, text in where.items():
        assert "section drawing" not in (text or ""), key
    # Only a confirm-only field is told where to read it; a typed field keeps its own hint.
    assert where["USER_INPUT:field_dimension"] is None
    assert where["SHOP:cabinet_width"] is None


def test_the_form_names_every_page_a_reading_is_confirmed_on(session: Session) -> None:
    """Viewing one page, a reading confirmed on another is still known: the field is not empty."""
    from app.evidence.confirm import confirm_candidate_type
    from app.models import ObservationCandidate
    from tests.api.test_v1_loop import DRAWING
    from tests.extraction.test_reader import MISSING_SPACE
    from tests.workflow.test_pipeline_end_to_end import _revision as _read_revision
    from workflow.stages import DatabaseStages

    with tempfile.TemporaryDirectory() as directory:
        store = LocalStore(root=Path(directory), ticket_secret=b"a secret only this test knows")
        revision = _read_revision(session, store, data=DRAWING)
        _publish(session, *ALL_RULES)
        stages = DatabaseStages(store, missing_space=MISSING_SPACE)
        stages.extract_pages(session, revision.id)
        stages.validate_evidence(session, revision.id)
        session.commit()
        package = session.get_one(Package, revision.package_id)
        depth = session.scalars(
            select(ObservationCandidate).where(ObservationCandidate.raw_text == "648 [25 1/2]")
        ).one()
        confirm_candidate_type(
            session, candidate_id=depth.id, semantic_type="CT010", confirmed_by="reviewer-a"
        )
        session.commit()
        client = _client(session, store=store, project=package.project_id)

        base = f"/api/v1/projects/{package.project_id}/packages/{package.id}/required-inputs"
        on_page_two = client.get(f"{base}?page_number=2")
        on_page_one = client.get(f"{base}?page_number=1")

    assert on_page_two.status_code == 200, on_page_two.text
    assert on_page_two.json()["confirmed_readings"] == []
    pages = {q["key"]: q["confirmed_pages"] for q in on_page_two.json()["quantities"]}
    assert pages["SHOP:CT010"] == [1]
    assert pages["SHOP:CT012"] == []
    assert {q["key"]: q["confirmed_pages"] for q in on_page_one.json()["quantities"]}[
        "SHOP:CT010"
    ] == [1]


def _typed_depths(session: Session, package: UUID, depths: dict[str, Fraction]) -> None:
    """Countertop depths typed before #1251, one per rule input named, as the endpoint wrote them."""
    revision = _revision(session, package)
    earlier = ParameterSet(
        project_id=str(PROJECT),
        layer=ParameterLayer.RUN,
        version=1,
        parameters={
            f"{rule_id}:countertop_depth": ParameterValue(
                value=Quantity(value=depth, unit=Unit("in")),
                provenance=Provenance.MEASURED,
                set_by="reviewer-a",
                set_at=datetime(2026, 10, 1, tzinfo=UTC),
            )
            for rule_id, depth in depths.items()
        },
    )
    stored, rows = to_rows(earlier, package_revision_id=revision.id)
    session.add(stored)
    for row in rows:
        session.add(row)
    session.commit()


def _saved_depth(session: Session, package: UUID) -> dict[str, Any]:
    form = _client(session).get(f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs")
    assert form.status_code == 200, form.text
    return {q["key"]: q for q in form.json()["saved_measurements"]}["SHOP:CT010"]


def test_a_value_typed_for_only_some_checks_names_the_checks_that_hold_it(
    session: Session,
) -> None:
    """#1262: the countertop depth feeds two checks; typed for one, the form says which one."""
    _publish(session, "ct_depth_001.yaml", "ct_back_offset_min_001.yaml")
    package = _package(session)
    _typed_depths(session, package, {"CT-DEPTH-001": Fraction(51, 2)})

    saved = _saved_depth(session, package)

    assert saved["complete"] is False
    assert saved["held_by"] == ["CT-DEPTH-001"]
    assert saved["differs_for"] == []


def test_a_check_holding_a_different_typed_value_is_named_apart(session: Session) -> None:
    """#1262: a check holding another typed number does not use the confirmed reading either."""
    _publish(session, "ct_depth_001.yaml", "ct_back_offset_min_001.yaml")
    package = _package(session)
    _typed_depths(
        session,
        package,
        {"CT-DEPTH-001": Fraction(51, 2), "CT-BACK-OFFSET-MIN-001": Fraction(26)},
    )

    saved = _saved_depth(session, package)

    assert saved["complete"] is False
    assert len(saved["held_by"]) == 1
    assert len(saved["differs_for"]) == 1
    assert {*saved["held_by"], *saved["differs_for"]} == {
        "CT-DEPTH-001",
        "CT-BACK-OFFSET-MIN-001",
    }


def test_a_value_typed_for_every_check_is_held_by_all_of_them(session: Session) -> None:
    _publish(session, "ct_depth_001.yaml", "ct_back_offset_min_001.yaml")
    package = _package(session)
    _typed_depths(
        session,
        package,
        {"CT-DEPTH-001": Fraction(51, 2), "CT-BACK-OFFSET-MIN-001": Fraction(51, 2)},
    )

    saved = _saved_depth(session, package)

    assert saved["complete"] is True
    assert sorted(saved["held_by"]) == ["CT-BACK-OFFSET-MIN-001", "CT-DEPTH-001"]
    assert saved["differs_for"] == []


def test_the_waiting_card_and_the_form_say_where_a_drawn_value_is_read_the_same_way() -> None:
    """#1262: one sentence for both screens; a vendor's view is never named for the architect's."""
    from app.review.waiting_values import DRAWING_GUIDE, where_drawn

    assert where_drawn("SHOP", "CT010") == DRAWING_GUIDE["CT010"].where_to_find
    assert where_drawn("ARCH", "CT010") == "The architect's drawing."
    assert where_drawn("ARCH", "CT001") == "The architect's drawing."
    for text in (where_drawn("SHOP", "CT010"), where_drawn("ARCH", "CT001")):
        assert "not supplied" not in text
