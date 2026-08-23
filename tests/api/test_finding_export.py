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
import logging
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.engine import Connection, ExecutionContext
from sqlalchemy.engine.interfaces import DBAPICursor
from sqlalchemy.orm import Session

from alembic import command
from app.api import finding_chain
from app.auth import Principal, Role, authenticate
from app.config import Settings
from app.db.session import session_factory
from app.main import API_PREFIX, create_app
from app.models import CheckRun, Finding, Package, PackageRevision, PackageState, Project
from app.telemetry.tracing import TRACE_ID_HEADER
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
    """One definition of "decided", asserted against the code rather than the prose.

    `verdict/outcomes.py` owns `DECISIVE_OUTCOMES` and `ABSTAINING_OUTCOMES` because the false-PASS metric
    and automation coverage need the same split. If the export restated it, the export and the release
    gate could disagree about what counted as a decision — and the export is what a human reads.

    **The first version of this test passed on the module docstring.** It asserted `"is_decision" in
    source`, and the docstring names `is_decision` in its own explanation — so it held even if every call
    were deleted and the split hardcoded. A test that greps a file for a word is a test of the sentence,
    which is the failure this very module was written to avoid making.
    """
    import ast

    import app.api.finding_export as export

    tree = ast.parse(Path(export.__file__).read_text())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "is_decision" in called, (
        "the export must call verdict/outcomes.py rather than decide for itself — naming it in a "
        "docstring is not calling it"
    )

    # And no abstaining outcome spelled out as a literal, which would be a second definition. Read from
    # the AST too, so a mention inside a comment or docstring neither passes nor fails this.
    literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    for name in ("NOT_FOUND", "REVIEW_REQUIRED", "NO_APPLICABLE_RULE"):
        assert (
            name not in literals
        ), f"{name} is a string literal here, which is a second definition of what abstains"


def test_an_unrecognised_outcome_is_not_counted_as_a_decision() -> None:
    """A stored value outside the enum must abstain, not decide.

    Both the classifier and the summary have to fall that way: reporting an uninterpretable value as a
    verdict the engine reached is how a wrong PASS gets built. Unit-level because the column's constraint
    stops such a row being seeded — the branch is defence, and defence still needs a test, which review
    pointed out it did not have.
    """
    from app.api.finding_export import _classify, _summarise

    unknown = _classify("SOMETHING_NEW", finding_id=uuid4(), project_id=uuid4(), package_id=uuid4())
    assert unknown is False

    summary = _summarise([("PASS", True), ("SOMETHING_NEW", unknown)])
    assert summary.decisions == 1, "only the PASS"
    assert summary.abstentions == 1, "the unknown value lands here, never in decisions"
    assert summary.by_outcome == {"PASS": 1, "SOMETHING_NEW": 1}


def test_an_unrecognised_outcome_is_reported_once_and_names_the_finding(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """One warning per bad row, carrying the id — both halves caught by review.

    **The duplicate was mine, introduced while fixing the previous round.** Delegating `_summarise` to the
    classifier stopped the decision split being restated, but it also meant every finding was judged in the
    handler and judged again in the summary — so a single unrecognised value announced itself once per
    finding plus once more from the count. An operator watching for this would see a number that tracks
    package size rather than how many rows are actually wrong.

    **And a warning without the id cannot be acted on.** "some outcome was unrecognised" says a row
    somewhere has left the engine's vocabulary and gives nobody a way to find it.

    So the guard is that `_summarise` classifies nothing: it is handed verdicts, and counting them must be
    silent no matter how strange the values are.
    """
    from app.api.finding_export import _classify, _summarise

    finding_id, project_id, package_id = uuid4(), uuid4(), uuid4()

    with caplog.at_level(logging.WARNING, logger="gv.api.finding_export"):
        decided = _classify(
            "SOMETHING_NEW",
            finding_id=finding_id,
            project_id=project_id,
            package_id=package_id,
        )
        # The value repeated, as it would be across many findings sharing one bad outcome.
        _summarise([("SOMETHING_NEW", decided)] * 5)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        "one bad row, one warning — counting the verdicts again is what made this fire per finding "
        f"and then once more from the summary, got {[r.getMessage() for r in warnings]}"
    )
    message = warnings[0].getMessage()
    assert str(finding_id) in message, "the id, or an operator cannot find the row this is about"
    assert "SOMETHING_NEW" in message, "and the value that was not understood"


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

    # **Two of my first three guards could not fail, which review caught.**
    #
    # `"\\x89PNG"` searched for those six literal characters. A real 0x89 byte inside a JSON string is
    # escaped as ``, so the PNG magic number could never be found that way. And `json.loads` never
    # produces `bytes`, so an `isinstance(value, bytes)` walk over the parsed payload was unreachable —
    # coverage that looked like coverage, which is worse than none.
    #
    # These do work: a PDF header survives as text, an embedded image arrives as a data URI or base64, and
    # the PNG check now looks for the escaped form JSON actually emits.
    for marker in ("%PDF", "\\u0089PNG", "data:image", "data:application", ";base64"):
        assert (
            marker not in serialised
        ), f"{marker!r} in the export means a drawing travelled with it"

    # The walk now asserts something reachable: every value is a JSON scalar or container. A field
    # smuggling a non-JSON type through the response model would fail here, where a bytes check could not.
    def walk(node: object, path: str = "") -> None:
        assert isinstance(
            node, str | int | float | bool | list | dict | None
        ), f"{path} is a {type(node).__name__}, which JSON should not have produced"
        if isinstance(node, dict):
            for key, value in node.items():
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


def test_a_package_over_the_cap_is_refused_rather_than_truncated(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**The bound, tested by lowering it rather than by seeding five thousand findings.**

    I added the cap and then found that removing it failed no test — which is the same "guard with no
    alarm" this file keeps being corrected for. Patching the constant exercises the real branch at a size
    a test can build.

    Refusing matters more than the number. A truncated export cannot be told apart from a complete one, so
    a consumer would confidently report on findings it never received — and the response says how many
    there are and where to page instead.
    """
    import app.api.finding_export as export

    project_id, package_id, _ = _seed(session)
    monkeypatch.setattr(export, "MAX_FINDINGS", 0)

    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/export"
    )
    assert response.status_code == 413, response.text
    # `app/errors.py` wraps every error in `{error, message, request_id}` — there is no `detail` key, which
    # I assumed there was. The message is the field a reviewer reads, so that is the one to assert.
    body = response.json()
    message = body["message"]
    assert "capped at 0" in message, "the limit is named, so a consumer knows what it hit"
    assert "1 findings" in message, "and how many there actually are"
    assert "page through" in message.lower(), "and what to do instead"


def test_the_cap_refuses_before_any_finding_is_loaded(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The refusal has to happen *before* the expensive query, or the limit bounds nothing.

    **The first version of the cap failed this, and its own docstring said otherwise.** It fetched every
    matching row, materialised the lot into ORM objects, and compared `len(rows)` afterwards — so an
    oversized package paid the full transfer and memory cost and then got a 413. That bounds the response
    while leaving the request unbounded, which is the opposite of the point, and "no bound ties request
    duration to package size" was a sentence the code did not honour.

    Asserted against the SQL actually issued rather than by timing anything: the count must run, and the
    ordered fetch must not. Watching the statements is the only way to see the difference, because both
    versions return an identical 413 to the caller.
    """
    from sqlalchemy import event

    import app.api.finding_export as export

    project_id, package_id, _ = _seed(session)
    monkeypatch.setattr(export, "MAX_FINDINGS", 0)

    issued: list[str] = []

    # Fully typed, per `AGENTS.md` — the first version carried a `no-untyped-def` suppression, which is a
    # way of saying "I did not check this signature". `retval=True` is deliberately not used, so this
    # listener observes and returns nothing; returning a tuple here would rewrite the statement.
    def record(
        conn: Connection,
        cursor: DBAPICursor,
        statement: str,
        parameters: object,
        context: ExecutionContext,
        executemany: bool,
    ) -> None:
        issued.append(statement)

    bind = session.get_bind()
    # Attached only around the request, so the seed's own inserts are not mistaken for the export's reads.
    event.listen(bind, "before_cursor_execute", record)
    try:
        response = _client(session, project_id).get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/export"
        )
    finally:
        event.remove(bind, "before_cursor_execute", record)

    assert response.status_code == 413, response.text

    lowered = [statement.lower() for statement in issued]

    # **This assertion first, because it is the one that matters.** `revision_number` appears only in the
    # export fetch's ORDER BY — the count joins the same tables but never names that column — so this
    # identifies the heavy query and nothing else. Checked before the count assertion because the count is
    # merely how the bound is achieved: with them the other way round, a regression that fetched the rows
    # *and* forgot to count failed on the wrong line and reported the wrong cause.
    fetched = [s for s in lowered if "order by" in s and "revision_number" in s]
    assert (
        fetched == []
    ), f"the refusal loaded the findings anyway, so the cap bounds only what is returned: {fetched}"

    assert any(
        "count(" in statement for statement in lowered
    ), "the size was established by counting"


def test_the_warning_and_the_response_name_the_same_trace(
    session: Session, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The unknown-outcome event carries the trace id of the request that produced it.

    **This is the assertion that makes the trace id worth emitting.** An id in a log line that matches
    nothing is decoration; the value is being able to start from a response a person is complaining about
    and find the events behind it. So the response header and the log record are compared directly.

    It also checks something I would otherwise have been assuming. The span is opened in the middleware and
    the warning is emitted deep inside the endpoint, and Starlette runs the endpoint in a separate task from
    the middleware — if the context did not propagate across that boundary, the header would hold one trace
    and the log another, and every "same trace" claim in this codebase would be false.

    The unknown outcome is forced rather than seeded: the column's check constraint refuses a value outside
    the enum, so the only way to reach the branch through a real request is to make the parse fail.
    """
    import app.api.finding_export as export

    def unrecognised(value: str) -> Outcome:
        raise ValueError(f"{value} is not an Outcome, for this test")

    project_id, package_id, _ = _seed(session)
    monkeypatch.setattr(export, "Outcome", unrecognised)

    with caplog.at_level(logging.WARNING, logger="gv.api.finding_export"):
        response = _client(session, project_id).get(
            f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/export"
        )

    assert response.status_code == 200, response.text
    header_trace = response.headers.get(TRACE_ID_HEADER)
    assert header_trace is not None, "the response does not expose a trace id to correlate against"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected one warning, got {[r.getMessage() for r in warnings]}"
    record = warnings[0]

    logged_trace = getattr(record, "trace_id", None)
    assert logged_trace == header_trace, (
        "the event and the response name different traces, so the correlation this exists for does not "
        f"work: log={logged_trace} header={header_trace}"
    )
    # And in the message itself, because that is what a person greps.
    assert header_trace in record.getMessage()

    # The structured fields review asked for, so a log backend can filter on them rather than parse prose.
    for field in ("finding_id", "project_id", "package_id", "outcome"):
        assert getattr(record, field, None) is not None, f"{field} is not on the record"

    # The export still reports it as an abstention — the safe direction, unchanged by any of this.
    assert response.json()["summary"]["decisions"] == 0


def test_a_package_exactly_at_the_cap_is_exported_in_full(
    session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other side of the boundary: at the limit is allowed, not refused.

    The over-cap test alone holds just as well for `>=` as for `>`, so it would not notice the cap turning
    into an off-by-one that rejects a complete export sitting exactly at the limit. That failure is quiet in
    the worst way — a consumer sees a refusal and cannot tell "too big" from "the boundary is wrong", and
    the fix looks like raising the number.

    The path instructions ask for boundary-exact values on both sides, and this is the cheap side to have
    been missing.
    """
    import app.api.finding_export as export

    project_id, package_id, _ = _seed(session)
    monkeypatch.setattr(export, "MAX_FINDINGS", 1)

    response = _client(session, project_id).get(
        f"{API_PREFIX}/projects/{project_id}/packages/{package_id}/findings/export"
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["findings"]) == 1, "the whole export, at exactly the limit"


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
    """A finding belongs to a package revision, and the export must not reach across packages.

    **My first version could not fail.** The other package's revision carried no findings, so
    `findings == 1` held whether or not the join was there — nothing existed to leak. Review's point: an
    assertion that passes on absent input asserts nothing.

    Now the other package has a finding of its own, and its absence from this export is what proves the
    join.
    """
    project_id, package_id, finding_id = _seed(session)
    other_package = _package_with(session, project_id, finding_id, Outcome.FAIL)

    payload = _export(session, project_id, package_id)
    summary = payload["summary"]
    assert isinstance(summary, dict)
    assert summary["findings"] == 1, "only this package's finding"
    assert summary["by_outcome"] == {"PASS": 1}, "the other package's FAIL must not appear"

    # And the other package exports its own, so the two are genuinely separate rather than one being empty.
    other = _export(session, project_id, other_package)
    assert other["summary"]["by_outcome"] == {"FAIL": 1}  # type: ignore[index]
