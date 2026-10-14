"""The six waiting checks ask for exactly what they miss, and run once it is supplied (#1181).

Verification for `app/review/waiting_values.py` (`missing_inputs` on the findings list),
`app/api/project_sink.py` (the project's sink entry and the sink a package uses),
`app/review/settings_in_force.py` (what Measurements shows in force), the company defaults and the
zero refusals (`app/api/company_settings.py`, `app/api/measurements.py`), and `entered` /
`defaults_used` in the changed-values report (`workflow/changed_values.py`).

A drawn number is never asked for, and never typed (GV's rule: people confirm readings, they do not
fill in measurements; Anant, 2026-10-12; the Measurements save refuses one since #1251). Where a test
needs one to reach a PASS or FAIL it arrives the way the app now gets it: a reading on the vendor's
drawing that a reviewer confirms (`confirm_candidate_type`, "Confirm a reading"). Every check runs
through the real engine (`DatabaseStages.run_checks`). Every number and name is synthetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import session_factory
from app.evidence.confirm import confirm_candidate_type
from app.main import create_app
from app.models import (
    CanonicalObservation,
    Document,
    DocumentKind,
    DocumentVersion,
    ExtractionRun,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Page,
    Project,
    SourceArtifact,
    TaskRun,
    WorkflowRun,
)
from app.models.project_sinks import ProjectSink
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook
from units.normalise import normalise_to_inches
from workflow.measurements import operands_for
from workflow.stages import DatabaseStages

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()

#: Every layout-free rule runs without a wall answer; the sink cabinet check is for three-sided
#: layouts only, so the runs here state one, as a reviewer would.
WALLS = {"wall_config": "back_left_right"}

#: The six checks and exactly what each lacks on a set where nothing was read or typed: the settings
#: (and the sink) first, by name, then the drawn numbers, by the client's code (listed, never asked).
SIX: dict[str, list[tuple[str, str]]] = {
    "CT-DEPTH-001": [
        ("cabinet_depth", "project"),
        ("countertop_overhang", "project"),
        ("SHOP:CT010", "drawing"),
    ],
    "CT-SINK-CUTOUT-WIDTH-001": [("sink_interior_width", "sink"), ("SHOP:CT012", "drawing")],
    "CT-SINK-CUTOUT-DEPTH-001": [("sink_interior_depth", "sink"), ("SHOP:CT008", "drawing")],
    "CT-SINK-OFFSET-FRONT-001": [("SHOP:CT007", "drawing")],
    "CT-BACK-OFFSET-MIN-001": [
        ("backsplash_thickness", "project"),
        ("countertop_overhang", "project"),
        ("SHOP:CT007", "drawing"),
        ("SHOP:CT008", "drawing"),
        ("SHOP:CT010", "drawing"),
    ],
    "CT-SINK-CABINET-WIDTH-001": [
        ("cabinet_side_thickness", "project"),
        ("SHOP:CT004", "drawing"),
        ("SHOP:CT011", "drawing"),
        ("SHOP:CT012", "drawing"),
        ("SHOP:CT013", "drawing"),
    ],
}


#: The two cabinet checks on a cabinet package where nothing was entered (#1205): the six cabinet
#: width limits are project settings, typed here; the cabinet lists, each cabinet's type and the site
#: width are the reviewer's entries on Measurements, listed with where to enter them; the architect's
#: wall-to-wall width is drawn, listed and never typed here.
CABINET: dict[str, list[tuple[str, str]]] = {
    "CAB-FILLER-001": [
        ("double_door_cab_width_max", "project"),
        ("double_door_cab_width_min", "project"),
        ("drawer_cab_width_max", "project"),
        ("drawer_cab_width_min", "project"),
        ("single_door_cab_width_max", "project"),
        ("single_door_cab_width_min", "project"),
        ("ARCH:CT001", "drawing"),
        ("ARCH:cabinet_width", "measurements"),
        ("ARCH:filler_width", "measurements"),
        ("SHOP:cabinet_width", "measurements"),
        ("SHOP:filler_width", "measurements"),
        ("USER_INPUT:cabinet_category", "measurements"),
        ("USER_INPUT:field_dimension", "measurements"),
    ],
    "CAB-ARCH-VS-SHOP-001": [
        ("ARCH:cabinet_width", "measurements"),
        ("SHOP:cabinet_width", "measurements"),
    ],
}


def _upgrade(engine: Engine) -> None:
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    _upgrade(postgres_engine)
    opened = session_factory(postgres_engine)()
    _publish_rulebook(opened)
    opened.commit()
    try:
        yield opened
    finally:
        opened.close()


def _client(session: Session, *, role: str = "reviewer", actor: str = "a reviewer") -> Any:
    from fastapi.testclient import TestClient

    from app.auth import Principal, Role, authenticate

    app = create_app(
        Settings(  # type: ignore[call-arg]
            database_url="postgresql+psycopg://unused@localhost/unused", environment="test"
        )
    )
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = lambda: Principal(
        id=actor, roles=frozenset({Role(role)}), projects=frozenset({PROJECT})
    )
    return TestClient(app, raise_server_exceptions=False)


def _package(session: Session, product: str | None = None) -> tuple[UUID, UUID]:
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="waiting values tests"))
        session.flush()
    package = Package(project_id=PROJECT, vendor="Synthetic Vendor", product_type=product)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.AWAITING_REVIEW
    )
    session.add(revision)
    session.commit()
    return package.id, revision.id


def _run(session: Session, revision: UUID) -> None:
    DatabaseStages(discriminators=WALLS).run_checks(session, revision)
    session.commit()


def _findings(client: Any, package: UUID) -> dict[str, dict[str, Any]]:
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/findings?limit=200")
    assert response.status_code == 200, response.text
    return {item["rule_id"]: item for item in response.json()["items"]}


def _settings(client: Any, package: UUID, **values: str) -> None:
    body = {
        "parameters": [
            {"name": name, "value": value, "scope": "project", "reference": "Synthetic spec"}
            for name, value in values.items()
        ]
    }
    response = client.post(f"/api/v1/projects/{PROJECT}/packages/{package}/measurements", json=body)
    assert response.status_code == 201, response.text


#: Which rule inputs each drawn number fills. A confirmed reading reaches each of them through the
#: rulebook's own join (source and type), so one confirmation fills both checks that read it.
READS: dict[str, tuple[tuple[str, str], ...]] = {
    "SHOP:CT004": (("CT-SINK-CABINET-WIDTH-001", "sink_cabinet_width"),),
    "SHOP:CT007": (
        ("CT-SINK-OFFSET-FRONT-001", "front_offset"),
        ("CT-BACK-OFFSET-MIN-001", "front_offset"),
    ),
    "SHOP:CT008": (
        ("CT-SINK-CUTOUT-DEPTH-001", "cutout_depth"),
        ("CT-BACK-OFFSET-MIN-001", "sink_depth"),
    ),
    "SHOP:CT010": (
        ("CT-DEPTH-001", "countertop_depth"),
        ("CT-BACK-OFFSET-MIN-001", "countertop_depth"),
    ),
    "SHOP:CT011": (("CT-SINK-CABINET-WIDTH-001", "clearance_left"),),
    "SHOP:CT012": (
        ("CT-SINK-CUTOUT-WIDTH-001", "cutout_width"),
        ("CT-SINK-CABINET-WIDTH-001", "cutout_width"),
    ),
    "SHOP:CT013": (("CT-SINK-CABINET-WIDTH-001", "clearance_right"),),
}


def _read(session: Session, client: Any, package: UUID, **values: str) -> None:
    """Drawn numbers the way the app now gets them (#1251): each one read on a page of the vendor's
    drawing and confirmed by a reviewer, through the same `confirm_candidate_type` "Confirm a
    reading" calls. Typing one is refused, so a test may no longer stand in for the reader that way.

    Outcome checked here, before any run: the typed route really is closed for every input these
    numbers fill, and nothing was typed — each input's value can only come from the reading.
    """
    for key, text in values.items():
        for rule_id, name in READS[f"SHOP:{key}"]:
            typed = client.post(
                f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
                json={"measurements": [{"rule_id": rule_id, "name": name, "value": text}]},
            )
            assert typed.status_code == 422, typed.text
    revision = session.scalars(
        select(PackageRevision)
        .where(PackageRevision.package_id == package)
        .order_by(PackageRevision.revision_number.desc())
        .limit(1)
    ).one()

    artifact = SourceArtifact(storage_key=f"synthetic/{uuid4()}", sha256=uuid4().hex * 2, size=1)
    document = Document(package_id=package, kind=DocumentKind.SHOP.value)
    session.add_all((artifact, document))
    session.flush()
    version = DocumentVersion(
        document_id=document.id,
        source_artifact_id=artifact.id,
        sha256=artifact.sha256,
        page_count=1,
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    # One page and no drawing views: the page is the vendor's one drawing, so all its readings are
    # on the same drawing and on the vendor's side (`app/evidence/sides.py`).
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash=artifact.sha256,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
        media_box=["0", "0", "612", "792"],
        crop_box=["0", "0", "612", "792"],
    )
    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add_all((page, workflow_run))
    session.flush()
    task = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task)
    session.flush()
    extraction = ExtractionRun(
        task_run_id=task.id,
        extractor="pdfplumber",
        extractor_version="synthetic-1",
        config_hash="synthetic",
        dpi=150,
    )
    session.add(extraction)
    session.flush()

    for row, (code, text) in enumerate(values.items()):
        exact = normalise_to_inches(text).exact
        top = 100 + 40 * row
        candidate = ObservationCandidate(
            document_version_id=version.id,
            page_id=page.id,
            extraction_run_id=extraction.id,
            raw_text=text,
            value_numerator=exact.numerator,
            value_denominator=exact.denominator,
            unit="in",
            polygon=[[100, top], [200, top], [200, top + 20], [100, top + 20]],
            coordinate_space="image",
            ambiguity_flags=[],
        )
        session.add(candidate)
        session.flush()
        confirmed = confirm_candidate_type(
            session, candidate_id=candidate.id, semantic_type=code, confirmed_by="a reviewer"
        )
        assert isinstance(confirmed, CanonicalObservation), confirmed
    session.commit()
    typed = operands_for(session, revision.id)
    assert not any(
        name in typed.get(rule_id, {}) for key in values for rule_id, name in READS[f"SHOP:{key}"]
    ), "a drawn number was typed, not confirmed"


def _sink(client: Any, **body: Any) -> Any:
    return client.post(f"/api/v1/projects/{PROJECT}/sink", json=body)


def _needs(finding: dict[str, Any]) -> list[tuple[str, str]]:
    return [(item["key"], item["scope"]) for item in finding["missing_inputs"]]


def _form(client: Any, package: UUID) -> dict[str, dict[str, Any]]:
    response = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/required-inputs")
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["parameters"]}


# ---------------------------------------------------------------------------
# What each check asks for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", sorted(SIX))
def test_each_waiting_check_asks_for_exactly_what_it_misses(session: Session, rule_id: str) -> None:
    """Outcome: the check waits, and its `missing_inputs` are every value the run lacked, in plain
    words with a unit and where to find each. Settings and the sink may be typed; a drawn number is
    listed and never askable. A setting the rulebook gives a default is never asked for."""
    client = _client(session)
    package, revision = _package(session)
    _run(session, revision)

    finding = _findings(client, package)[rule_id]

    assert finding["outcome"] == "NOT_FOUND"
    assert _needs(finding) == SIX[rule_id]
    for need in finding["missing_inputs"]:
        assert need["unit"] == "in"
        assert need["label"] and need["where_to_find"]
        assert "CT0" not in need["label"] and "_" not in need["label"]
        assert need["saved"] is None
        assert need["askable"] is (need["scope"] != "drawing")
        if need["scope"] == "project":
            assert need["sources"] == ["G.C / Client"]


def test_no_other_check_asks_for_anything(session: Session) -> None:
    """Only the six and the two cabinet checks ask (#1181, #1205); every other waiting result — the
    countertop width — is answered on its own card, and carries an empty list."""
    client = _client(session)
    package, revision = _package(session)
    _run(session, revision)

    findings = _findings(client, package)

    others = {
        rule_id: item
        for rule_id, item in findings.items()
        if rule_id not in SIX and rule_id not in CABINET
    }
    assert others, "the rulebook publishes more than the six"
    assert any(item["outcome"] == "NOT_FOUND" for item in others.values())
    assert all(item["missing_inputs"] == [] for item in others.values())


def test_a_result_that_is_not_waiting_asks_for_nothing(session: Session) -> None:
    """A check that needs a layout answer is not waiting on a value."""
    client = _client(session)
    package, revision = _package(session)
    DatabaseStages().run_checks(session, revision)
    session.commit()

    findings = _findings(client, package)

    assert findings["CT-SINK-CABINET-WIDTH-001"]["outcome"] == "REVIEW_REQUIRED"
    assert findings["CT-SINK-CABINET-WIDTH-001"]["missing_inputs"] == []


# ---------------------------------------------------------------------------
# Supplied, then run: a real PASS or FAIL
# ---------------------------------------------------------------------------


def test_typed_settings_say_run_checks_and_the_run_decides_all_six(session: Session) -> None:
    """**The point of #1181.** The settings and the sink typed where asked: each shows as saved and
    the results still wait (only a run uses a value). With the drawn numbers read, the run decides
    all six. Outcome: five PASS, and the back offset FAILs because the synthetic sink leaves too
    little."""
    client = _client(session)
    package, revision = _package(session)
    _run(session, revision)
    _settings(
        client,
        package,
        cabinet_depth='20"',
        countertop_overhang='1 1/4"',
        backsplash_thickness='3/4"',
        cabinet_side_thickness='3/4"',
    )
    saved_sink = _sink(
        client, model_name="Synthetic Sink A", inside_width='30"', inside_depth='15"'
    )
    assert saved_sink.status_code == 201, saved_sink.text

    saved = _findings(client, package)
    for rule_id in SIX:
        assert saved[rule_id]["outcome"] == "NOT_FOUND", rule_id
        for need in saved[rule_id]["missing_inputs"]:
            assert (need["saved"] is not None) is need["askable"], (rule_id, need["key"])
    overhang = next(
        need
        for need in saved["CT-DEPTH-001"]["missing_inputs"]
        if need["key"] == "countertop_overhang"
    )
    assert overhang["saved"]["text"] == '1 1/4"'
    assert overhang["saved"]["layer"] == "project"
    assert overhang["saved"]["source"] == "G.C / Client: Synthetic spec"
    assert overhang["saved"]["set_by"] == "a reviewer"
    width = next(
        need
        for need in saved["CT-SINK-CUTOUT-WIDTH-001"]["missing_inputs"]
        if need["key"] == "sink_interior_width"
    )
    assert width["saved"]["source"] == "G.C / Client: Sink Synthetic Sink A"

    _read(
        session,
        client,
        package,
        CT010='21 1/4"',
        CT007='4"',
        CT012='29 1/2"',
        CT008='14 1/2"',
        CT004='35"',
        CT011='2"',
        CT013='2"',
    )
    _run(session, revision)
    after = _findings(client, package)

    assert {rule_id: after[rule_id]["outcome"] for rule_id in SIX} == {
        "CT-DEPTH-001": "PASS",
        "CT-SINK-CUTOUT-WIDTH-001": "PASS",
        "CT-SINK-CUTOUT-DEPTH-001": "PASS",
        "CT-SINK-OFFSET-FRONT-001": "PASS",
        # 21 1/4 - (1 1/4 + 4 + 14 1/2 + 3/4) = 3/4, under the 2 1/2 minimum.
        "CT-BACK-OFFSET-MIN-001": "FAIL",
        "CT-SINK-CABINET-WIDTH-001": "PASS",
    }
    assert all(after[rule_id]["missing_inputs"] == [] for rule_id in SIX)


def test_settings_alone_leave_a_check_waiting_for_its_drawn_number(session: Session) -> None:
    """Every setting typed but the countertop depth never read: the depth check keeps waiting, and
    says it needs the drawing. Nothing became a zero."""
    client = _client(session)
    package, revision = _package(session)
    _settings(client, package, cabinet_depth='20"', countertop_overhang='1"')

    _run(session, revision)

    depth = _findings(client, package)["CT-DEPTH-001"]
    assert depth["outcome"] == "NOT_FOUND"
    assert _needs(depth) == [("SHOP:CT010", "drawing")]
    assert depth["missing_inputs"][0]["askable"] is False


def test_a_wrong_drawn_value_fails_after_the_run(session: Session) -> None:
    """The same arithmetic, the other way: a sink cabinet drawn an inch wide of its contents fails."""
    client = _client(session)
    package, revision = _package(session)
    _settings(client, package, cabinet_side_thickness='3/4"')
    _read(session, client, package, CT004='36"', CT011='2"', CT012='29 1/2"', CT013='2"')

    _run(session, revision)

    assert _findings(client, package)["CT-SINK-CABINET-WIDTH-001"]["outcome"] == "FAIL"


# ---------------------------------------------------------------------------
# The sink entry
# ---------------------------------------------------------------------------


def test_the_sink_entry_feeds_every_sink_check(session: Session) -> None:
    """One entry for the project: both cut-out checks of every package read its size. Outcome: on
    two packages, both cut-outs PASS from one sink entry."""
    client = _client(session)
    first, first_revision = _package(session)
    second, second_revision = _package(session)
    for package in (first, second):
        _read(session, client, package, CT012='29 1/2"', CT008='14 1/2"')

    saved = _sink(
        client,
        model_name="Synthetic Sink B",
        inside_width='30"',
        inside_depth='15"',
        source_note="synthetic cut sheet",
    )

    assert saved.status_code == 201, saved.text
    sink = saved.json()["sink"]
    assert (sink["model_name"], sink["source_note"], sink["set_by"]) == (
        "Synthetic Sink B",
        "synthetic cut sheet",
        "a reviewer",
    )
    assert (sink["inside_width"]["text"], sink["inside_depth"]["text"]) == ('30"', '15"')
    assert "front_offset" not in sink
    assert client.get(f"/api/v1/projects/{PROJECT}/sink").json() == saved.json()
    for package, revision in ((first, first_revision), (second, second_revision)):
        _run(session, revision)
        after = _findings(client, package)
        assert after["CT-SINK-CUTOUT-WIDTH-001"]["outcome"] == "PASS"
        assert after["CT-SINK-CUTOUT-DEPTH-001"]["outcome"] == "PASS"


def test_the_sink_entry_takes_no_front_offset(session: Session) -> None:
    """The sink holds only its name and inside size (Anant, 2026-10-12): a front offset sent with it
    is ignored, and GV's standard front offset stays in force."""
    client = _client(session)
    package, _revision = _package(session)

    saved = _sink(
        client, model_name="Synthetic", inside_width='30"', inside_depth='15"', front_offset='9"'
    )

    assert saved.status_code == 201, saved.text
    assert _form(client, package)["front_offset_required"]["in_force"]["origin"] == "rulebook"


def test_a_sink_needs_a_name_and_a_size_above_zero(session: Session) -> None:
    """Missing is never zero: a zero, a bare number or no name is refused, and nothing is saved."""
    client = _client(session)
    _package(session)

    for body, says in (
        (
            {"model_name": "Synthetic", "inside_width": '0"', "inside_depth": '15"'},
            "more than zero",
        ),
        ({"model_name": "Synthetic", "inside_width": "30", "inside_depth": '15"'}, "unit"),
        ({"model_name": "  ", "inside_width": '30"', "inside_depth": '15"'}, "Name the sink"),
    ):
        response = _sink(client, **body)
        assert response.status_code == 422, response.text
        assert says in response.json()["message"]

    assert client.get(f"/api/v1/projects/{PROJECT}/sink").json() == {"sink": None}
    assert session.scalars(select(ProjectSink)).all() == []


@pytest.mark.parametrize("role", ["rule_admin"])
def test_only_a_reviewer_or_an_admin_enters_a_sink(session: Session, role: str) -> None:
    """Refused like every authorisation failure: a 404 that says nothing, and nothing is saved.
    The app has no separate viewer role; `rule_admin` is the one role without `ENTER_VALUES`."""
    _package(session)

    response = _sink(
        _client(session, role=role), model_name="Synthetic", inside_width='30"', inside_depth='15"'
    )

    assert response.status_code == 404, response.text
    assert session.scalars(select(ProjectSink)).all() == []


def test_a_size_typed_for_the_package_wins_and_says_so(session: Session) -> None:
    """A sink size typed on Measurements for one package is that package's, and wins there; the
    package's "sink in use" says which number came from where."""
    client = _client(session)
    package, revision = _package(session)
    assert (
        _sink(client, model_name="Synthetic", inside_width='30"', inside_depth='15"').status_code
        == 201
    )
    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "parameters": [
                {"name": "sink_interior_width", "value": '31"', "scope": "run", "reference": "x"}
            ]
        },
    )
    assert response.status_code == 201, response.text
    _read(session, client, package, CT012='29 1/2"')

    in_use = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/sink-in-use").json()
    _run(session, revision)

    assert in_use["model_name"] == "Synthetic"
    assert (in_use["inside_width"]["text"], in_use["inside_width"]["origin"]) == ('31"', "package")
    assert (in_use["inside_depth"]["text"], in_use["inside_depth"]["origin"]) == (
        '15"',
        "sink_card",
    )
    assert _findings(client, package)["CT-SINK-CUTOUT-WIDTH-001"]["outcome"] == "FAIL"


def test_with_no_sink_nothing_is_in_use(session: Session) -> None:
    client = _client(session)
    package, _revision = _package(session)

    in_use = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/sink-in-use").json()

    assert in_use == {"model_name": None, "inside_width": None, "inside_depth": None}


# ---------------------------------------------------------------------------
# Measurements shows what is in force
# ---------------------------------------------------------------------------


def test_measurements_shows_the_sink_card_and_gv_defaults_in_force(session: Session) -> None:
    """Instead of a blank: the sink card's size, GV's default, the rulebook's default — each with
    where it is from — and nothing at all for a setting nobody has set."""
    admin = _client(session, role="admin", actor="an admin")
    client = _client(session)
    package, _revision = _package(session)
    assert (
        admin.post(
            "/api/v1/company-settings", json={"values": [{"name": "cabinet_depth", "value": '22"'}]}
        ).status_code
        == 201
    )
    assert (
        _sink(client, model_name="Synthetic", inside_width='30"', inside_depth='15"').status_code
        == 201
    )

    form = _form(client, package)

    assert form["sink_interior_width"]["in_force"]["text"] == '30"'
    assert form["sink_interior_width"]["in_force"]["origin"] == "sink_card"
    assert form["cabinet_depth"]["in_force"]["origin"] == "gv_default"
    assert form["cabinet_depth"]["in_force"]["set_by"] == "an admin"
    assert form["front_offset_required"]["in_force"]["origin"] == "rulebook"
    assert form["countertop_overhang"]["in_force"] is None

    _settings(client, package, cabinet_depth='20"')
    assert _form(client, package)["cabinet_depth"]["in_force"]["origin"] == "project"


# ---------------------------------------------------------------------------
# Company defaults
# ---------------------------------------------------------------------------


def test_the_four_company_defaults_are_listed_empty_until_gv_sets_them(session: Session) -> None:
    """Listed as project defaults with nothing in use: no value is filled in for GV."""
    response = _client(session, role="admin").get("/api/v1/company-settings")

    settings = {item["name"]: item for item in response.json()["settings"]}
    for name in (
        "countertop_overhang",
        "backsplash_thickness",
        "cabinet_side_thickness",
        "cabinet_depth",
    ):
        assert settings[name]["kind"] == "project_default", name
        assert settings[name]["in_use"] is None, name
        assert settings[name]["rulebook_default"] is None, name
    assert settings["back_offset_minimum"]["kind"] == "standard"


@pytest.mark.parametrize("name", ["cabinet_depth", "cabinet_side_thickness"])
def test_a_zero_size_is_refused_as_a_company_default_and_a_project_value(
    session: Session, name: str
) -> None:
    """A cabinet depth or a side panel of zero is a mistake, never a value, wherever it is saved."""
    admin = _client(session, role="admin")
    client = _client(session)
    package, _revision = _package(session)

    company = admin.post(
        "/api/v1/company-settings", json={"values": [{"name": name, "value": '0"'}]}
    )
    project = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={"parameters": [{"name": name, "value": '0"', "scope": "project"}]},
    )

    for response in (company, project):
        assert response.status_code == 422, response.text
        assert "must be more than zero" in response.json()["message"]
    assert _form(client, package)[name]["in_force"] is None


def test_a_zero_overhang_or_backsplash_is_accepted(session: Session) -> None:
    """Zero is a real answer for these two (no overhang, no backsplash); an open question to Anant."""
    admin = _client(session, role="admin")

    response = admin.post(
        "/api/v1/company-settings",
        json={
            "values": [
                {"name": "countertop_overhang", "value": '0"'},
                {"name": "backsplash_thickness", "value": '0"'},
            ]
        },
    )

    assert response.status_code == 201, response.text


def test_a_project_value_wins_over_the_company_default(session: Session) -> None:
    """GV's default cabinet depth is used where the project sets none, and stops the check asking
    for it; a project value then wins, and the changed-values report lists it against the default.
    Outcome: PASS on the company 22", FAIL once the project says 20"."""
    admin = _client(session, role="admin", actor="an admin")
    client = _client(session)
    package, revision = _package(session)
    saved = admin.post(
        "/api/v1/company-settings", json={"values": [{"name": "cabinet_depth", "value": '22"'}]}
    )
    assert saved.status_code == 201, saved.text
    _settings(client, package, countertop_overhang='1"')
    _run(session, revision)
    assert _needs(_findings(client, package)["CT-DEPTH-001"]) == [("SHOP:CT010", "drawing")]
    _read(session, client, package, CT010='23"')

    _run(session, revision)
    assert _findings(client, package)["CT-DEPTH-001"]["outcome"] == "PASS"
    changed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/changed-values").json()
    assert any(
        line.startswith('Cabinet depth: 22" (GV default;') for line in changed["defaults_used"]
    )

    _settings(client, package, cabinet_depth='20"')
    _run(session, revision)

    assert _findings(client, package)["CT-DEPTH-001"]["outcome"] == "FAIL"
    changed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/changed-values").json()
    assert any(
        line.startswith("cabinet_depth = 20 in (project") and "22 in (global" in line
        for line in changed["company_standards_displaced"]
    ), changed
    assert not any(line.startswith("Cabinet depth") for line in changed["defaults_used"])


def test_with_nothing_set_anywhere_the_checks_still_wait(session: Session) -> None:
    """No company default, no project value, no sink: every one of the six still waits, run after
    run, and says what it needs. Nothing became a zero."""
    client = _client(session)
    package, revision = _package(session)
    _run(session, revision)
    _run(session, revision)

    findings = _findings(client, package)

    for rule_id, needs in SIX.items():
        assert findings[rule_id]["outcome"] == "NOT_FOUND", rule_id
        assert _needs(findings[rule_id]) == needs, rule_id


# ---------------------------------------------------------------------------
# The report's changed-values list
# ---------------------------------------------------------------------------


def test_values_typed_here_are_in_the_changed_values_list(session: Session) -> None:
    """A sink and an overhang with no company default displaced nothing, so they were in no list
    before #1181. Outcome: both are in `entered`, in plain words, with source, who and when."""
    client = _client(session)
    package, revision = _package(session)
    _settings(client, package, countertop_overhang='1 1/4"')
    _sink(client, model_name="Synthetic Sink C", inside_width='30"', inside_depth='15"')
    _run(session, revision)

    changed = client.get(f"/api/v1/projects/{PROJECT}/packages/{package}/changed-values").json()

    entered = changed["entered"]
    assert any(
        line.startswith(
            'Countertop overhang: 1 1/4" (this job; G.C / Client: Synthetic spec; '
            "set by a reviewer on "
        )
        for line in entered
    ), entered
    assert any(
        line.startswith('Sink inside width: 30" (this job; G.C / Client: Sink Synthetic Sink C;')
        for line in entered
    ), entered
    assert not any("sink_interior_width" in line for line in entered)
    assert "countertop_overhang" not in changed["outstanding"]


# ---------------------------------------------------------------------------
# The two cabinet checks (#1205)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rule_id", sorted(CABINET))
def test_each_cabinet_check_lists_every_value_it_waits_for(session: Session, rule_id: str) -> None:
    """Before #1205 both said `missing_inputs: []` while their reason named one value. Outcome:
    every value the run lacked, in plain words, with where each is entered; only the six width
    limits get a box here (project settings, either source); and the reason no longer speaks of
    labelled readings nobody made."""
    client = _client(session)
    package, revision = _package(session, product="cabinet")
    _run(session, revision)

    finding = _findings(client, package)[rule_id]

    assert finding["outcome"] == "NOT_FOUND"
    assert _needs(finding) == CABINET[rule_id]
    assert "labelled readings" not in finding["reason"]
    for need in finding["missing_inputs"]:
        assert need["label"] and need["where_to_find"]
        assert "CT0" not in need["label"] and "_" not in need["label"]
        assert need["saved"] is None
        assert need["askable"] is (need["scope"] == "project")
        assert need["unit"] == (None if need["key"] == "USER_INPUT:cabinet_category" else "in")
        if need["scope"] == "project":
            assert need["sources"] == ["Company standard", "G.C / Client"]
        if need["scope"] == "measurements":
            assert "on Measurements" in need["where_to_find"]


def test_a_cabinet_value_entered_is_no_longer_listed_and_says_run_checks(
    session: Session,
) -> None:
    """The list is worked out from what the run had: the site width entered on Measurements shows as
    saved until the next run, and is then no longer listed; the limits typed are gone too."""
    client = _client(session)
    package, revision = _package(session, product="cabinet")
    _run(session, revision)
    response = client.post(
        f"/api/v1/projects/{PROJECT}/packages/{package}/measurements",
        json={
            "measurements": [{"rule_id": "CAB-FILLER-001", "name": "field_width", "value": '63"'}],
            "parameters": [
                {"name": name, "value": value, "scope": "project", "source": "Company standard"}
                for name, value in {
                    "single_door_cab_width_min": '9"',
                    "single_door_cab_width_max": '24"',
                    "double_door_cab_width_min": '24"',
                    "double_door_cab_width_max": '48"',
                    "drawer_cab_width_min": '12"',
                    "drawer_cab_width_max": '36"',
                }.items()
            ],
        },
    )
    assert response.status_code == 201, response.text

    saved = {
        need["key"]: need for need in _findings(client, package)["CAB-FILLER-001"]["missing_inputs"]
    }
    assert saved["USER_INPUT:field_dimension"]["saved"]["text"] == '63"'
    assert saved["single_door_cab_width_min"]["saved"]["source"] == "Company standard"

    _run(session, revision)
    after = [key for key, _scope in _needs(_findings(client, package)["CAB-FILLER-001"])]

    assert (
        after == [key for key, scope in CABINET["CAB-FILLER-001"] if scope != "project"][:-1]
    ), "the limits and the site width are no longer listed"
