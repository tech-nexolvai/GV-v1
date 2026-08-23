"""The versioned findings export, and the labelling that stops an abstention reading as an all-clear
(#224, D1.3).

The seed is imported from `test_finding_chain` rather than copied. It is a hundred and thirty lines of
project, package, revision, document, page, observations, rule snapshot, check run and operands, and two
copies would drift — the export would keep asserting a fixture shape the chain tests had already moved on
from.

Source: backend proposal §10.2 · Design: `docs/DESIGN_PRODUCT.md` §3.1 · Verification: this file
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api import finding_chain
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import CheckRun, Finding, Package, PackageRevision, PackageState, Project
from tests.api.test_finding_chain import _seed
from tests.app.postgres_fixture import alembic_config
from verdict.outcomes import ABSTAINING_OUTCOMES, DECISIVE_OUTCOMES, Outcome, Severity

pytest_plugins = ("tests.app.postgres_fixture",)

DATABASE_URL = "postgresql+psycopg://gv:gv@localhost:5433/gv"


@pytest.fixture
def session(postgres_engine: Engine) -> Iterator[Session]:
    config = alembic_config()
    config.attributes["database_url"] = postgres_engine.url.render_as_string(hide_password=False)
    command.upgrade(config, "head")
    factory = session_factory(postgres_engine)
    with factory() as opened:
        yield opened
        opened.rollback()


def _client(session: Session, project_id: UUID) -> TestClient:
    principal = Principal(
        id="reviewer-1", roles=frozenset({Role.REVIEWER}), projects=frozenset({project_id})
    )
    app = create_app(Settings(database_url=DATABASE_URL))  # type: ignore[call-arg]
    app.dependency_overrides[authenticate] = lambda: principal
    # One override covers both routers: `finding_export` imports the same `get_session` object.
    app.dependency_overrides[finding_chain.get_session] = lambda: session
    return TestClient(app, raise_server_exceptions=False)


def _export(session: Session, project_id: UUID, package_id: UUID) -> dict[str, object]:
    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/export"
    )
    assert response.status_code == 200, response.text
    return dict(response.json())


def _package_with(
    session: Session, project_id: UUID, seeded_finding: UUID, outcome: Outcome
) -> UUID:
    """A package whose only finding has `outcome`. Returns the package id.

    **Built rather than edited, because a finding cannot be edited.** My first version updated the seeded
    finding's outcome and PostgreSQL refused it: `RestrictViolation` from the append-only trigger in
    migration 0013. Findings are evidence — "what did you tell us in March?" has one answer, so the row is
    immutable by design and a test that rewrites one is testing something the system does not allow.

    A fresh `CheckRun` too, because `ix_findings_check_run_id` is unique: one check run produces exactly one
    finding. Also learnt by having it refused.
    """
    previous = session.get(Finding, seeded_finding)
    assert previous is not None
    previous_run = session.get(CheckRun, previous.check_run_id)
    assert previous_run is not None

    package = Package(project_id=project_id, vendor=None)
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)
    session.flush()
    run = CheckRun(
        package_revision_id=revision.id,
        rule_snapshot_id=previous_run.rule_snapshot_id,
        engine_version=previous_run.engine_version,
    )
    session.add(run)
    session.flush()
    session.add(
        Finding(
            check_run_id=run.id,
            package_revision_id=revision.id,
            outcome=outcome,
            severity=Severity.MAJOR,
            # **An abstention still gets a trace, because the column requires one.**
            #
            # `verdict/finding.py` says only an abstention may *lack* a trace, so I first wrote `None`
            # here — and the export returned 500. `Finding.trace` is `nullable=False`, so the domain
            # object permits an absence the table does not. The empty dict is the honest persisted form:
            # nothing was computed, and there is no arithmetic to show.
            trace={"comparison": "checked"} if outcome in DECISIVE_OUTCOMES else {},
            parameter_set_versions={"global": "sha256:parameters"},
        )
    )
    session.flush()
    return package.id


# ---------------------------------------------------------------------------
# The version
# ---------------------------------------------------------------------------


def test_the_schema_version_is_present_and_pinned(session: Session) -> None:
    """A consumer can detect a change rather than break silently — the first acceptance criterion.

    Asserted as an exact value, not merely as "present". A version field that is read but never compared
    is decoration, and this test is what makes bumping it a deliberate act with a failing test attached.
    """
    project_id, package_id, _ = _seed(session)
    payload = _export(session, project_id, package_id)

    assert payload["schema_version"] == "1"


def test_the_version_cannot_be_anything_else(session: Session) -> None:
    """`Literal["1"]` rather than `str`, so an accidental change is a validation error rather than a value
    a consumer has to interpret."""
    from app.api.finding_export import FindingExportV1

    field = FindingExportV1.model_fields["schema_version"]
    assert field.annotation is not str, "a free-form version string is a version nobody can rely on"
    with pytest.raises(ValueError, match="schema_version"):
        FindingExportV1(
            schema_version="2",  # type: ignore[arg-type]
            project_id=uuid4(),
            package_id=uuid4(),
            summary={"findings": 0, "decisions": 0, "abstentions": 0, "by_outcome": {}},  # type: ignore[arg-type]
            findings=(),
        )


# ---------------------------------------------------------------------------
# Abstentions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("outcome", sorted(ABSTAINING_OUTCOMES, key=lambda o: o.value))
def test_every_abstention_is_labelled(outcome: Outcome, session: Session) -> None:
    """The second acceptance criterion, over all three abstaining outcomes rather than one.

    `NOT_FOUND`, `REVIEW_REQUIRED` and `NO_APPLICABLE_RULE` are the system declining to decide. A consumer
    reading only `outcome` has to know that; the flag means it does not have to.
    """
    project_id, _, finding_id = _seed(session)
    package_id = _package_with(session, project_id, finding_id, outcome)

    payload = _export(session, project_id, package_id)
    entries = payload["findings"]
    assert isinstance(entries, list) and len(entries) == 1
    entry = entries[0]

    assert entry["abstained"] is True, f"{outcome.value} is an abstention and must say so"
    assert entry["chain"]["outcome"] == outcome.value


@pytest.mark.parametrize("outcome", sorted(DECISIVE_OUTCOMES, key=lambda o: o.value))
def test_a_decision_is_not_labelled_as_an_abstention(outcome: Outcome, session: Session) -> None:
    """The other side of the boundary, or the flag could be hardcoded true and pass everything above."""
    project_id, _, finding_id = _seed(session)
    package_id = _package_with(session, project_id, finding_id, outcome)

    payload = _export(session, project_id, package_id)
    entries = payload["findings"]
    assert isinstance(entries, list)
    assert entries[0]["abstained"] is False


def test_an_abstaining_package_does_not_present_as_clean(session: Session) -> None:
    """**The failure this story exists to prevent.**

    A package whose every finding abstained has no failures in it. A consumer filtering on
    `outcome == "FAIL"`, finding none, and reporting "no problems" would be reading abstention as approval
    — `AGENTS.md` §2.2. The summary makes that impossible to do quietly: nothing was decided, and the
    number saying so sits above the findings rather than inside them.
    """
    project_id, _, finding_id = _seed(session)
    package_id = _package_with(session, project_id, finding_id, Outcome.REVIEW_REQUIRED)

    payload = _export(session, project_id, package_id)
    summary = payload["summary"]
    assert isinstance(summary, dict)

    assert summary["findings"] == 1
    assert summary["decisions"] == 0, "nothing was decided, and the export says so in one number"
    assert summary["abstentions"] == 1
    assert summary["by_outcome"] == {"REVIEW_REQUIRED": 1}

    # And the thing a naive consumer would look for is genuinely absent, which is why the counts matter.
    assert "FAIL" not in summary["by_outcome"]


def test_the_split_comes_from_the_engines_own_definition() -> None:
    """One definition of "decided", not two.

    `verdict/outcomes.py` owns `DECISIVE_OUTCOMES` and `ABSTAINING_OUTCOMES` because the false-PASS metric
    and automation coverage need the same split. If the export restated it, the export and the release
    gate could disagree about what counted as a decision — and the export is what a human reads.
    """
    import app.api.finding_export as export

    source = __import__("pathlib").Path(export.__file__).read_text()
    assert (
        "is_decision" in source
    ), "the export must ask verdict/outcomes.py rather than decide for itself"
    for literal in ("NOT_FOUND", "REVIEW_REQUIRED", "NO_APPLICABLE_RULE"):
        assert (
            f'"{literal}"' not in source
        ), f"{literal} is spelled out here, which is a second definition of what abstains"


# ---------------------------------------------------------------------------
# Never the drawings themselves
# ---------------------------------------------------------------------------


def test_the_export_carries_no_drawing_bytes(session: Session) -> None:
    """The third acceptance criterion, checked by walking the payload rather than by reading the models.

    `AGENTS.md` §6: references and hashes only. The chain models were built that way, and this fails if
    somebody later adds a field that embeds a crop or a page image — which is the change that would look
    harmless in review.
    """
    project_id, package_id, _ = _seed(session)
    payload = _export(session, project_id, package_id)
    serialised = json.dumps(payload)

    # A PDF or PNG header, or a data URI, would mean bytes travelled.
    for marker in ("%PDF", "\\x89PNG", "data:image", "data:application", ";base64"):
        assert (
            marker not in serialised
        ), f"{marker!r} in the export means a drawing travelled with it"

    def walk(node: object, path: str = "") -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                assert not isinstance(value, bytes), f"{path}.{key} is raw bytes"
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                walk(value, f"{path}[{index}]")

    walk(payload)

    # The references that *should* be there, so this test cannot pass by the export being empty.
    evidence = payload["findings"][0]["chain"]["operands"][0]["evidence"]  # type: ignore[index]
    assert evidence["document_version_id"], "the pin is present"
    assert evidence["crop_uri"], "the crop is referenced by URI, which is the point"


# ---------------------------------------------------------------------------
# Boundaries and determinism
# ---------------------------------------------------------------------------


def test_another_projects_package_is_not_exportable(session: Session) -> None:
    """The project boundary, established twice — by the dependency and again in SQL."""
    _, package_id, _ = _seed(session)
    other = Project(name="Someone else")
    session.add(other)
    session.flush()

    response = _client(session, other.id).get(
        f"{API_PREFIX}/projects/{other.id}/packages/{package_id}/findings/export"
    )
    assert response.status_code == 404, response.text


def test_a_package_with_no_findings_exports_an_empty_list_not_a_404(session: Session) -> None:
    """ "Nothing found" and "no such package" are different answers.

    An empty export for a real package is honest. A 404 would tell a consumer the package does not exist,
    which is a different and wrong statement — and would send somebody looking for a data problem that is
    not there.
    """
    project = Project(name="Empty project")
    session.add(project)
    session.flush()
    package = Package(project_id=project.id, vendor=None)
    session.add(package)
    session.flush()

    payload = _export(session, project.id, package.id)
    assert payload["findings"] == []
    assert payload["summary"] == {
        "findings": 0,
        "decisions": 0,
        "abstentions": 0,
        "by_outcome": {},
    }


def test_two_exports_of_unchanged_data_are_identical(session: Session) -> None:
    """Ordered output, so a consumer diffing yesterday against today sees only real changes.

    Without a deterministic order the same data can serialise differently and every diff is noise, which
    is how people stop reading diffs.
    """
    project_id, package_id, _ = _seed(session)

    first = json.dumps(_export(session, project_id, package_id), sort_keys=False)
    second = json.dumps(_export(session, project_id, package_id), sort_keys=False)
    assert first == second


def test_a_second_finding_appears_and_is_counted(session: Session) -> None:
    """More than one finding, because a summary computed from a single row proves very little."""
    project_id, package_id, finding_id = _seed(session)
    existing = session.get(Finding, finding_id)
    assert existing is not None
    run = session.get(CheckRun, existing.check_run_id)
    assert run is not None

    # Its own check run: `ix_findings_check_run_id` is unique, so one run yields one finding.
    second_run = CheckRun(
        package_revision_id=existing.package_revision_id,
        rule_snapshot_id=run.rule_snapshot_id,
        engine_version=run.engine_version,
    )
    session.add(second_run)
    session.flush()
    session.add(
        Finding(
            check_run_id=second_run.id,
            package_revision_id=existing.package_revision_id,
            outcome=Outcome.NOT_FOUND,
            severity=Severity.MAJOR,
            trace={},
            parameter_set_versions={"global": "sha256:parameters"},
        )
    )
    session.flush()

    payload = _export(session, project_id, package_id)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    assert summary["findings"] == 2
    assert summary["decisions"] == 1, "the seeded PASS"
    assert summary["abstentions"] == 1, "the added NOT_FOUND"
    assert summary["by_outcome"] == {"NOT_FOUND": 1, "PASS": 1}


def test_a_revision_that_is_not_this_packages_is_not_included(session: Session) -> None:
    """A finding belongs to a package revision; the export must not reach across packages.

    `PackageRevision` and `Package` are both joined, so this asserts the join rather than trusting it.
    """
    project_id, package_id, _ = _seed(session)
    other_package = Package(project_id=project_id, vendor=None)
    session.add(other_package)
    session.flush()
    session.add(
        PackageRevision(package_id=other_package.id, revision_number=1, state="RUNNING_CHECKS")
    )
    session.flush()

    payload = _export(session, project_id, package_id)
    assert payload["summary"]["findings"] == 1  # type: ignore[index]
