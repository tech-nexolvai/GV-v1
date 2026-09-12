"""Asking a model for an assignment, and what it is not allowed to be asked (#589).

Verification for: `workflow/assignment_bedrock.py`.

Two to read first. `test_the_schema_lists_the_real_identifiers` is why an invented field key cannot
be emitted rather than merely caught: the keys go into the tool schema as `enum`s, so constrained
decoding refuses them during generation. And `test_the_rule_arithmetic_never_reaches_the_model` is
the line that makes the whole design non-circular — a model holding `CT-WIDTH-001`'s equation could
choose readings that balance it, and the check would then confirm the balance.

No provider is contacted. The client is a stub, and every fixture is authored.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.config import Settings
from workflow.assignment import AssignmentContext, Field, ProposedAssignment, Reading
from workflow.assignment_bedrock import (
    TOOL_NAME,
    BedrockAssignmentModel,
    _Config,
    assignment_tool_schema,
    configured_assignment_model,
    propose_and_guard,
)

DATABASE = "postgresql+psycopg://gv:gv@localhost:5433/gvtest"

DEPTH = Field(
    key="SHOP:CT010",
    name="countertop_depth",
    source="SHOP",
    many=False,
    description="Compare the authored shop countertop depth with the project cabinet depth.",
)
CABINETS = Field(key="SHOP:cabinet_width", name="cabinet_widths", source="SHOP", many=True)


def _reading(candidate_id: str, value: str, **overrides: Any) -> Reading:
    return Reading(
        candidate_id=candidate_id,
        value=value,
        source=overrides.pop("source", "SHOP"),
        page=overrides.pop("page", 1),
        line_key=overrides.pop("line_key", "line-1"),
        chain_key=overrides.pop("chain_key", None),
        order=overrides.pop("order", None),
    )


def _context() -> AssignmentContext:
    return AssignmentContext(
        fields=(DEPTH, CABINETS),
        readings=(_reading("c1", "25 1/2 in"), _reading("c2", "36 in")),
    )


def _response(rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    payload = {
        "assignments": (
            rows if rows is not None else [{"field_key": "SHOP:CT010", "candidate_ids": ["c1"]}]
        )
    }
    return {
        "output": {"message": {"content": [{"toolUse": {"name": TOOL_NAME, "input": payload}}]}}
    }


class _Client:
    def __init__(self, response: dict[str, Any]) -> None:
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        return self.response


def _model(response: dict[str, Any] | None = None) -> BedrockAssignmentModel:
    return BedrockAssignmentModel(
        config=_Config(
            model_id="configured-model",
            region_name="us-east-1",
            connect_timeout_seconds=1,
            read_timeout_seconds=2,
        ),
        client=_Client(response if response is not None else _response()),
    )


# ---------------------------------------------------------------------------
# The constraint that cannot be escaped
# ---------------------------------------------------------------------------


def test_the_schema_lists_the_real_identifiers() -> None:
    """**Input: one run's context. Outcome: its keys and ids are the only permitted values.**

    Built per request rather than fixed. A static schema would accept any string and leave "is that
    a field we asked for?" to be checked afterwards; listing the actual identifiers makes the
    grammar refuse anything else *during generation*, which is the only moment the model is free.
    """
    schema = assignment_tool_schema(_context())

    item = schema["properties"]["assignments"]["items"]  # type: ignore[index]
    assert item["properties"]["field_key"]["enum"] == ["SHOP:CT010", "SHOP:cabinet_width"]
    assert item["properties"]["candidate_ids"]["items"]["enum"] == ["c1", "c2"]


def test_the_schema_is_fresh_each_call() -> None:
    """Outcome: mutating one request's schema cannot affect another's."""
    first = assignment_tool_schema(_context())
    first["properties"] = {}

    assert "assignments" in assignment_tool_schema(_context())["properties"]  # type: ignore[operator]


def test_the_call_forces_the_one_tool_and_allows_no_free_text() -> None:
    """Outcome: a forced tool call at temperature zero.

    Zero because this is a selection, not a composition. Two runs over one drawing that disagreed
    would put a reviewer in front of an answer nobody can reproduce.
    """
    model = _model()

    model.propose(_context())

    request = model.client.calls[0]  # type: ignore[union-attr]
    assert request["toolConfig"]["toolChoice"] == {"tool": {"name": TOOL_NAME}}
    assert request["inferenceConfig"]["temperature"] == 0
    assert request["modelId"] == "configured-model"


def test_free_text_beside_the_tool_call_is_refused() -> None:
    """Input: a tool call plus prose. Outcome: `RuntimeError`.

    Prose alongside a forced tool call means the model did something other than what was asked, and
    the tool payload is not more trustworthy for being next to it.
    """
    response = _response()
    response["output"]["message"]["content"].append({"text": "here is my reasoning"})

    with pytest.raises(RuntimeError, match="one tool call and no free text"):
        _model(response).propose(_context())


# ---------------------------------------------------------------------------
# What the model is given
# ---------------------------------------------------------------------------


def test_the_rule_arithmetic_never_reaches_the_model() -> None:
    """**The line that keeps this non-circular.** Outcome: no formula in the payload.

    `CT-WIDTH-001` checks that a countertop width equals the sum of the cabinets and fillers. A model
    holding that equation could choose readings that make it balance, and the check would then
    confirm the balance — passing on every drawing, including one with a real error in it.

    Asserted on what is sent rather than on `Field` alone, because a future payload builder could
    reach past the type for something richer without the type changing.
    """
    model = _model()

    model.propose(_context())

    sent = json.dumps(model.client.calls[0])  # type: ignore[union-attr]
    for forbidden in ("operation", "operands", "sum", "tolerance", "equals", "derivation"):
        assert forbidden not in sent, f"the model was given {forbidden!r}"


def test_the_model_is_told_what_the_rule_is_for() -> None:
    """Outcome: the published description is sent.

    Prose about purpose, which is the most useful thing a model can be told here and is safe for the
    reason the formula is not: it says what the check means, never how it computes.
    """
    model = _model()

    model.propose(_context())

    sent = json.dumps(model.client.calls[0])  # type: ignore[union-attr]
    assert "Compare the authored shop countertop depth" in sent


def test_each_reading_carries_its_sheet_line_and_place_in_the_run() -> None:
    """Outcome: the facts the guard will check against are the facts the model is given.

    Telling it which readings are unattached is cheaper than letting it propose one and discarding
    the batch — while the guard stays the thing that enforces it.
    """
    context = AssignmentContext(
        fields=(CABINETS,),
        readings=(
            _reading("c1", "15 in", chain_key="chain-a", order=0),
            _reading("c2", "36 in", line_key=None),
        ),
    )
    model = _model()

    model.propose(context)

    readings = json.loads(model.client.calls[0]["messages"][0]["content"][2]["text"])["readings"]  # type: ignore[union-attr]
    assert readings[0]["run"] == "chain-a"
    assert readings[0]["position_in_run"] == 0
    assert readings[0]["attached_to_dimension_line"] is True
    assert readings[1]["attached_to_dimension_line"] is False


def test_nothing_is_asked_when_there_is_nothing_to_choose() -> None:
    """Input: no readings. Outcome: no provider call at all.

    Spending a call to be told there is nothing to assign is a cost with no possible answer.
    """
    model = _model()

    result = model.propose(AssignmentContext(fields=(DEPTH,), readings=()))

    assert result == ()
    assert model.client.calls == []  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Every failure ends the same way
# ---------------------------------------------------------------------------


def test_a_proposal_the_guard_refuses_yields_nothing() -> None:
    """**Input: a reading assigned to the wrong sheet's field. Outcome: no assignment at all.**

    The guard is not advisory. A refused proposal leaves the fields empty and a reviewer filling
    them, which is exactly what happens with no model configured.
    """
    context = AssignmentContext(
        fields=(Field(key="ARCH:CT001", name="design_width", source="ARCH", many=False),),
        readings=(_reading("c1", "36 in", source="SHOP"),),
    )
    model = _model(_response([{"field_key": "ARCH:CT001", "candidate_ids": ["c1"]}]))

    assert propose_and_guard(context, model) == ()


def test_a_provider_failure_yields_nothing_rather_than_raising() -> None:
    """Input: a client that raises. Outcome: `()`.

    A model that cannot be reached must not stop a package being reviewed. This step makes a manual
    flow faster; it is never the reason one can or cannot proceed.
    """

    class _Broken:
        def converse(self, **_: Any) -> dict[str, Any]:
            raise TimeoutError("no route to host")

    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=_Broken(),
    )

    assert propose_and_guard(_context(), model) == ()


def test_no_model_configured_yields_nothing() -> None:
    """Outcome: `()`, which is today's behaviour and a complete one."""
    assert propose_and_guard(_context(), None) == ()


def test_an_accepted_proposal_comes_back() -> None:
    """Outcome: the assignment, once it has passed every check."""
    assert propose_and_guard(_context(), _model()) == (
        ProposedAssignment(field_key="SHOP:CT010", candidate_ids=("c1",)),
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_an_unconfigured_model_disables_the_step() -> None:
    """Outcome: `None`, so the reviewer fills the fields exactly as they do now."""
    assert configured_assignment_model(Settings(database_url=DATABASE, bedrock_model="")) is None
    assert configured_assignment_model(Settings(database_url=DATABASE, bedrock_model="   ")) is None


def test_a_configured_model_is_built_from_the_deployment_settings() -> None:
    """Outcome: the deployment's model and region, never a constant chosen here."""
    settings = Settings(
        database_url=DATABASE, bedrock_model="operator-chosen", bedrock_region="eu-west-2"
    )

    model = configured_assignment_model(settings)

    assert model is not None
    assert model.config.model_id == "operator-chosen"
    assert model.config.region_name == "eu-west-2"


# ---------------------------------------------------------------------------
# The retry, and what it is allowed to say
# ---------------------------------------------------------------------------


class _CorrectsOnceClient:
    """A provider that double-claims a reading, then fixes it when told what was wrong.

    Not invented. This is what `amazon.nova-lite-v1:0` actually did on the five PM-confirmed
    readings from `AI_Set_2` page 13: it put the two fillers into `filler_widths` correctly *and*
    into `cabinet_widths` as well, and on being shown the guard's sentence returned the run without
    them — which matches the reviewed answer exactly.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def converse(self, **request: Any) -> dict[str, Any]:
        self.calls.append(request)
        if len(self.calls) == 1:
            return _response(
                [
                    {"field_key": "SHOP:cabinet_width", "candidate_ids": ["c1", "c2"]},
                    {"field_key": "SHOP:filler_width", "candidate_ids": ["c1"]},
                ]
            )
        return _response(
            [
                {"field_key": "SHOP:cabinet_width", "candidate_ids": ["c2"]},
                {"field_key": "SHOP:filler_width", "candidate_ids": ["c1"]},
            ]
        )


FILLERS = Field(key="SHOP:filler_width", name="filler_widths", source="SHOP", many=True)


def _run_context() -> AssignmentContext:
    return AssignmentContext(
        fields=(CABINETS, FILLERS),
        readings=(
            _reading("c1", "2 in", chain_key="run-1", order=0),
            _reading("c2", "36 in", chain_key="run-1", order=1),
        ),
    )


def test_a_correctable_mistake_is_corrected_on_the_second_attempt() -> None:
    """**Input: a double-claimed reading. Outcome: accepted after one retry.**

    Measured against the real provider before it was written. A model told *nothing* repeats its
    answer; a model shown the guard's own sentence can fix it, and on the reviewed cabinet run it
    did — returning the three cabinets and two fillers that the PM had confirmed.
    """
    client = _CorrectsOnceClient()
    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=client,
    )

    result = propose_and_guard(_run_context(), model)

    assert len(client.calls) == 2
    assert result == (
        ProposedAssignment(field_key="SHOP:cabinet_width", candidate_ids=("c2",)),
        ProposedAssignment(field_key="SHOP:filler_width", candidate_ids=("c1",)),
    )


def test_the_retry_carries_the_guard_reason_and_nothing_invented() -> None:
    """**Outcome: the model is told what was wrong, never what to do instead.**

    The correction is the deterministic guard's own sentence. A hint composed here — "put the small
    ones in fillers" — would be this adapter deciding the answer and then congratulating the model
    for agreeing.
    """
    client = _CorrectsOnceClient()
    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=client,
    )

    propose_and_guard(_run_context(), model)

    second = json.dumps(client.calls[1])
    assert "One reading measures one thing" in second
    assert "rejected by a deterministic check" in second


def test_it_retries_once_and_not_forever() -> None:
    """Input: a provider that keeps double-claiming. Outcome: two calls, then the reviewer.

    A second failure after being shown the reason is not a slip, and paying for attempt after
    attempt to reach an answer a reviewer would give in one click is the wrong trade.
    """

    class _NeverLearns:
        def __init__(self) -> None:
            self.calls = 0

        def converse(self, **_: Any) -> dict[str, Any]:
            self.calls += 1
            return _response(
                [
                    {"field_key": "SHOP:cabinet_width", "candidate_ids": ["c1"]},
                    {"field_key": "SHOP:filler_width", "candidate_ids": ["c1"]},
                ]
            )

    client = _NeverLearns()
    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=client,
    )

    assert propose_and_guard(_run_context(), model) == ()
    assert client.calls == 2


def test_the_first_attempt_carries_no_correction() -> None:
    """Outcome: nothing about a previous rejection on a first call.

    A model told an attempt was rejected before making one would be answering a question about an
    answer that does not exist.
    """
    client = _CorrectsOnceClient()
    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=client,
    )

    propose_and_guard(_run_context(), model)

    assert "deterministic check" not in json.dumps(client.calls[0])
