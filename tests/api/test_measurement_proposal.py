"""The endpoint that asks a model which reading fills which field, and what it streams (#591).

**What is verified here is the wiring and the honesty of the display, not the model.** Whether the
model chooses well is `tests/workflow/test_assignment_bedrock.py`'s question and the gold set's;
what matters at this boundary is that the context handed over carries the facts the guard needs —
the sheet, the attachment, the chain and the position along it — and that a reviewer never receives
a proposal that has not passed every one of those checks.

Every model here is a stub. The provider is never called: an API test that needed AWS credentials
would skip in CI, which is where this boundary most needs watching.

Source: issue #591 · Verified module: `app/api/measurements.py`
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from alembic import command
from app.api.dependencies import get_session
from app.config import Settings
from app.db.session import session_factory
from app.main import create_app
from app.models import (
    Document,
    DocumentVersion,
    ObservationCandidate,
    Package,
    PackageRevision,
    PackageRevisionDocument,
    PackageState,
    Project,
    SourceArtifact,
)
from app.models.document import DocumentKind, Page
from app.models.evidence import ObservationAssociation
from app.models.runs import ExtractionRun, TaskRun, WorkflowRun
from tests.app.postgres_fixture import alembic_config
from tests.workflow.test_stages import _publish_rulebook

pytest_plugins = ("tests.app.postgres_fixture",)

PROJECT = uuid4()
PROPOSE = "/api/v1/projects/{project}/packages/{package}/measurements/propose"


def _settings() -> Settings:
    return Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused@localhost/unused",
        environment="test",
    )


def _principal() -> Any:
    from app.auth import Principal, Role

    return Principal(id="anant", roles=frozenset({Role.ADMIN}), projects=frozenset({PROJECT}))


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


def _client(session: Session) -> Any:
    from fastapi.testclient import TestClient

    from app.auth import authenticate

    app = create_app(_settings())
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[authenticate] = _principal
    return TestClient(app, raise_server_exceptions=False)


class _StubModel:
    """A model that returns exactly what a test tells it to, and records what it was asked.

    Shaped like `BedrockAssignmentModel` from `propose_and_guard`'s side — one `propose` taking the
    context and the previous refusal — because that is the whole surface the endpoint uses.
    """

    def __init__(self, *answers: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
        self._answers = list(answers)
        self.contexts: list[Any] = []
        self.refusals: list[str | None] = []
        self.config = type("_C", (), {"model_id": "stub-model-v1"})()

    def propose(self, context: Any, *, refused: str | None = None) -> tuple[Any, ...]:
        from workflow.assignment import ProposedAssignment

        self.contexts.append(context)
        self.refusals.append(refused)
        answer = self._answers.pop(0) if self._answers else ()
        return tuple(ProposedAssignment(field_key=key, candidate_ids=ids) for key, ids in answer)


def _package_with_readings(
    session: Session,
    *,
    readings: tuple[tuple[str, int, int, str | None, int | None], ...],
    kind: str = DocumentKind.SHOP.value,
    attached: bool = True,
) -> tuple[UUID, dict[str, UUID]]:
    """A package whose shop drawing has one page of readings, each attached to a dimension line.

    `readings` is `(raw_text, numerator, denominator, chain_key, chain_position)`. A `None` chain is
    a line the detector found standing alone, which is most lines; a chain key groups the readings
    the drawing draws end to end, which is the only thing that makes an ordered run checkable.

    `attached=False` records the other kind of association row: a refusal, which is what
    `text_association` writes when it cannot say which line a number annotates. Written as a row
    from the start rather than substituted afterwards, because the table is append-only — a
    correction there is a new row, and a test that deletes one is testing a database we do not run.
    """
    if session.get(Project, PROJECT) is None:
        session.add(Project(id=PROJECT, name="proposal tests"))
        session.flush()

    package = Package(project_id=PROJECT, vendor="Apex Glass & Stone")
    session.add(package)
    session.flush()
    revision = PackageRevision(
        package_id=package.id, revision_number=1, state=PackageState.RUNNING_CHECKS
    )
    session.add(revision)

    artifact = SourceArtifact(storage_key=f"s/{uuid4()}", sha256="0" * 64, size=1)
    session.add(artifact)
    session.flush()
    document = Document(package_id=package.id, kind=kind)
    session.add(document)
    session.flush()
    version = DocumentVersion(
        document_id=document.id, source_artifact_id=artifact.id, sha256="0" * 64, page_count=1
    )
    session.add(version)
    session.flush()
    session.add(
        PackageRevisionDocument(
            package_revision_id=revision.id,
            package_id=package.id,
            document_id=document.id,
            document_version_id=version.id,
        )
    )
    page = Page(
        document_version_id=version.id,
        index=0,
        content_hash="0" * 64,
        width_pt=Decimal(612),
        height_pt=Decimal(792),
        rotation=0,
        has_vector_text=True,
    )
    session.add(page)
    session.flush()

    workflow_run = WorkflowRun(package_revision_id=revision.id, engine_run_id=str(uuid4()))
    session.add(workflow_run)
    session.flush()
    task_run = TaskRun(
        workflow_run_id=workflow_run.id,
        idempotency_key=str(uuid4()),
        task_type="extract",
        attempt=1,
        outcome="SUCCEEDED",
    )
    session.add(task_run)
    session.flush()
    run = ExtractionRun(
        task_run_id=task_run.id,
        extractor="test",
        extractor_version="1",
        config_hash="test",
    )
    session.add(run)
    session.flush()

    by_text: dict[str, UUID] = {}
    for position, (raw_text, numerator, denominator, chain_key, chain_position) in enumerate(
        readings
    ):
        candidate = ObservationCandidate(
            document_version_id=version.id,
            page_id=page.id,
            extraction_run_id=run.id,
            raw_text=raw_text,
            value_numerator=numerator,
            value_denominator=denominator,
            unit="in",
            polygon=[[0, 0], [10, 0], [10, 10], [0, 10]],
            ambiguity_flags=[],
        )
        session.add(candidate)
        session.flush()
        by_text[raw_text] = candidate.id
        session.add(
            ObservationAssociation(
                candidate_id=candidate.id,
                extraction_run_id=run.id,
                start_x=f"0.{position}" if attached else None,
                start_y="0.5" if attached else None,
                end_x=f"0.{position + 1}" if attached else None,
                end_y="0.5" if attached else None,
                signals=["nearest line"] if attached else [],
                refusal_reason=None if attached else "two lines were equally close",
                chain_key=chain_key if attached else None,
                chain_position=chain_position if attached else None,
            )
        )
    session.commit()
    return package.id, by_text


def _frames(response: Any) -> list[dict[str, Any]]:
    """Every `data:` frame of the stream, parsed. The transport is plain SSE."""
    return [
        json.loads(block.split("data:", 1)[1].strip())
        for block in response.text.split("\n\n")
        if block.strip().startswith("data:")
    ]


def _stream(session: Session, package: UUID, model: Any) -> list[dict[str, Any]]:
    import app.api.measurements as endpoint

    original = endpoint.configured_assignment_model
    endpoint.configured_assignment_model = lambda _settings: model  # type: ignore[assignment]
    try:
        response = _client(session).post(PROPOSE.format(project=PROJECT, package=package))
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/event-stream")
        return _frames(response)
    finally:
        endpoint.configured_assignment_model = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


def test_an_accepted_proposal_fills_the_fields_it_names(session: Session) -> None:
    """**Input: a model naming one reading for one field. Outcome: that field comes back filled.**

    The whole chain in one assertion — the rulebook's fields, the drawing's readings, the model's
    choice and the guard's approval — because each link has been reachable on its own for a while
    and the thing that did not exist was the path between them.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    model = _StubModel((("SHOP:CT010", (str(ids["25 1/2"]),)),))

    frames = _stream(session, package, model)
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["fields_filled"] == 1
    filled = result["assignments"][0]
    assert filled["field_key"] == "SHOP:CT010"
    assert filled["values"][0]["value"] == "25 1/2 in"
    assert filled["values"][0]["candidate_id"] == str(ids["25 1/2"])


def test_the_percentage_is_phases_finished_and_never_exceeds_them(session: Session) -> None:
    """**Outcome: the bar is a fraction of a fixed sequence, monotone, ending at the last phase.**

    The one property the display must have. A percentage that is anything other than "how much of a
    known sequence has finished" is a claim about a duration or a confidence, and this side of the
    request knows neither. Asserted on the frames rather than on the component, because the number
    is the server's and a component cannot fix a number that arrives wrong.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    model = _StubModel((("SHOP:CT010", (str(ids["25 1/2"]),)),))

    frames = _stream(session, package, model)
    steps = [frame["step"] for frame in frames if frame["event"] == "step"]

    assert [step["name"] for step in steps] == [
        "rulebook",
        "readings",
        "proposing",
        "checking",
        "filling",
    ]
    assert [step["percent"] for step in steps] == [0, 20, 40, 60, 80]
    assert all(step["total"] == 5 for step in steps)
    assert all(step["percent"] <= 100 for step in steps)


def test_a_retry_repeats_its_phase_rather_than_advancing_the_bar(session: Session) -> None:
    """**Input: a first answer the guard refuses, then a correct one. Outcome: the bar holds.**

    The case the phase display exists for. A model asked again is work that did not advance
    anything, and a bar that moved for it would be reporting a rejected answer as progress. The
    frames repeat `proposing` at the same percentage with `attempt: 2`, which is the truth about
    what happened and the only way a reviewer learns the answer was checked at all.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(
        session,
        readings=(("15", 15, 1, "chain-a", 0), ("36", 36, 1, "chain-a", 1)),
    )
    # First answer claims one reading for two fields, which `guard_assignment` refuses on
    # uniqueness. Second answer is well formed.
    model = _StubModel(
        (
            ("SHOP:CT010", (str(ids["15"]),)),
            ("SHOP:CT001", (str(ids["15"]),)),
        ),
        (("SHOP:CT010", (str(ids["15"]),)),),
    )

    frames = _stream(session, package, model)
    steps = [frame["step"] for frame in frames if frame["event"] == "step"]
    proposing = [step for step in steps if step["name"] == "proposing"]

    assert len(proposing) == 2, "the retry was invisible from outside"
    assert [step["attempt"] for step in proposing] == [1, 2]
    assert {step["percent"] for step in proposing} == {40}, "the bar advanced on rejected work"
    assert model.refusals[1] is not None, "the model was asked again without being told why"


def test_a_proposal_the_guard_refuses_twice_fills_nothing_and_says_so(session: Session) -> None:
    """**Outcome: zero fields filled, and the guard's own sentence returned.**

    A refusal is not an error and must not read as one: the fields stay empty and the reviewer types
    them, which is exactly what happens without this step. What the response owes them is the
    reason, in the words the deterministic check used.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    ghost = str(uuid4())
    model = _StubModel((("SHOP:CT010", (ghost,)),), (("SHOP:CT010", (ghost,)),))

    frames = _stream(session, package, model)
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["fields_filled"] == 0
    assert result["assignments"] == []
    assert result["unfilled_reason"] is not None
    assert "this run did not produce" in result["unfilled_reason"]
    assert ids  # the real reading was available and still nothing was filled


def test_no_model_configured_fills_nothing_and_is_not_an_error(session: Session) -> None:
    """**Outcome: 200, zero filled, `model_id` null.**

    The deployment without a model is the current product, not a degraded one. A 503 here would put
    a red banner on a screen that is working exactly as designed.
    """
    _publish_rulebook(session)
    package, _ = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))

    frames = _stream(session, package, None)
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["model_id"] is None
    assert result["fields_filled"] == 0
    assert result["readings_considered"] == 1


# ---------------------------------------------------------------------------
# What the model is given
# ---------------------------------------------------------------------------


def test_the_context_carries_the_chain_the_drawing_draws(session: Session) -> None:
    """**Outcome: a reading's chain and its position reach the model's context.**

    The fact `dimension_lines` computes, `0044` stores and `guard_assignment` refuses a run without.
    It was computed and discarded for three issues; this asserts it now travels the whole way.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(
        session,
        readings=(("15", 15, 1, "chain-a", 0), ("36", 36, 1, "chain-a", 1)),
    )
    model = _StubModel()

    _stream(session, package, model)

    context = model.contexts[0]
    by_id = {reading.candidate_id: reading for reading in context.readings}
    first = by_id[str(ids["15"])]
    second = by_id[str(ids["36"])]
    assert first.chain_key == second.chain_key != None
    assert (first.order, second.order) == (0, 1)
    assert first.source == "SHOP"


def test_a_reading_attached_to_nothing_is_marked_rather_than_hidden(session: Session) -> None:
    """**Outcome: it reaches the model marked unattached, and the counts say so.**

    `guard_assignment` refuses an unattached reading — `text_association` already declined to say
    what it annotates. `assignment_bedrock` still shows it, because telling the model which readings
    are unusable is cheaper than letting it propose one and lose the whole batch to the refusal.
    """
    _publish_rulebook(session)
    package, _ = _package_with_readings(
        session, readings=(("25 1/2", 51, 2, None, None),), attached=False
    )
    model = _StubModel()

    frames = _stream(session, package, model)
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["readings_considered"] == 1
    assert result["readings_attached"] == 0
    assert model.contexts[0].readings[0].line_key is None


def test_the_fields_never_carry_the_rule_arithmetic(session: Session) -> None:
    """**The circularity guard, at the boundary that assembles the context.**

    `CT-WIDTH-001` checks that a countertop width equals the sum of the cabinets and the fillers. A
    model holding that equation could choose readings that make it balance, and the check would then
    confirm the balance on every drawing including one with a real error in it. So the description
    that travels is the client's positional definition of the quantity, never the rule's own prose —
    which on these rules states the sum.
    """
    _publish_rulebook(session)
    package, _ = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    model = _StubModel()

    _stream(session, package, model)

    descriptions = " ".join(field.description or "" for field in model.contexts[0].fields).lower()
    for arithmetic in ("sum", "equals", "=", "+", "total of", "minus"):
        assert arithmetic not in descriptions, descriptions
    assert not any(
        hasattr(field, "operation") or hasattr(field, "formula")
        for field in model.contexts[0].fields
    )


def test_the_first_frame_carries_the_whole_phase_sequence(session: Session) -> None:
    """**Outcome: the labels a client renders come from the server, once.**

    A client with its own copy of the sequence is a second answer to "what are the phases", free to
    disagree with this one the first time a phase is added — and the disagreement would show as a
    progress display that quietly stops matching the work.
    """
    _publish_rulebook(session)
    package, ids = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    model = _StubModel((("SHOP:CT010", (str(ids["25 1/2"]),)),))

    steps = [
        frame["step"] for frame in _stream(session, package, model) if frame["event"] == "step"
    ]

    assert len(steps[0]["sequence"]) == steps[0]["total"] == 5
    assert steps[0]["sequence"][0] == steps[0]["label"]
    assert all(step["sequence"] == [] for step in steps[1:]), "the sequence was repeated"


def test_a_phase_that_was_never_reached_sends_no_frame(session: Session) -> None:
    """**Input: nothing proposed, so nothing checked. Outcome: no `checking` frame.**

    A proposal nobody made is never checked, and a frame for it would put a tick beside "checking"
    on the reviewer's screen — claiming a check that did not run, which is the one claim this
    display exists to make honestly. The client renders the gap as *not reached* rather than done.
    """
    _publish_rulebook(session)
    package, _ = _package_with_readings(session, readings=(("25 1/2", 51, 2, None, None),))
    model = _StubModel()  # proposes nothing

    frames = _stream(session, package, model)
    names = [frame["step"]["name"] for frame in frames if frame["event"] == "step"]

    assert "checking" not in names
    assert names == ["rulebook", "readings", "proposing", "filling"]


def test_a_page_with_nothing_left_to_propose_does_not_blame_the_model(session: Session) -> None:
    """**Input: every reading already confirmed. Outcome: "nothing to choose between".**

    The ordinary case on a page a reviewer has worked through, and the message matters: "the model
    proposed nothing" would be false about a model that was never asked, and would send somebody
    looking for a fault in a step that behaved correctly.
    """
    _publish_rulebook(session)
    package, _ = _package_with_readings(session, readings=())
    model = _StubModel()

    frames = _stream(session, package, model)
    result = next(frame["result"] for frame in frames if frame["event"] == "result")

    assert result["readings_considered"] == 0
    assert result["unfilled_reason"] == "there was nothing to choose between, so nothing was asked"
    assert model.contexts == [], "a call was made with nothing to choose between"
