"""Asking a model for an assignment, and what it is not allowed to be asked (#589).

Verification for: `workflow/assignment_bedrock.py`.

Two to read first. `test_the_schema_lists_this_runs_fields_and_reading_handles` is why an invented field key cannot
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
    TOKEN_CEILING,
    TOKEN_FLOOR,
    TOOL_NAME,
    AssignmentProgress,
    BedrockAssignmentModel,
    UnusableAnswer,
    _Config,
    _token_limit,
    assignment_tool_schema,
    configured_assignment_model,
    describe_failure,
    propose_and_guard,
    reading_handles,
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
        geometry_available=overrides.pop("geometry_available", True),
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


def test_the_schema_lists_this_runs_fields_and_reading_handles() -> None:
    """**Input: one run's context. Outcome: its keys and reading handles are the only permitted values.**

    Built per request rather than fixed. A static schema would accept any string and leave "is that
    a field we asked for?" to be checked afterwards; listing the actual identifiers makes the
    grammar refuse anything else *during generation*, which is the only moment the model is free.
    Readings are listed by handle rather than candidate id since #712 — see the handle tests below.
    """
    schema = assignment_tool_schema(_context())

    item = schema["properties"]["assignments"]["items"]  # type: ignore[index]
    assert item["properties"]["field_key"]["enum"] == ["SHOP:CT010", "SHOP:cabinet_width"]
    assert item["properties"]["candidate_ids"]["items"]["enum"] == ["r1", "r2"]


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

    The unattached reading here is on a page with no line-work — the only unattached kind that is
    ever offered, since `propose_and_guard` leaves out the kind the guard refuses outright (#712).
    """
    context = AssignmentContext(
        fields=(CABINETS,),
        readings=(
            _reading("c1", "15 in", chain_key="chain-a", order=0),
            _reading("c2", "36 in", line_key=None, geometry_available=False),
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

    assert propose_and_guard(context, model) == ((), ())


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

    assert propose_and_guard(_context(), model) == ((), ())


def test_no_model_configured_yields_nothing() -> None:
    """Outcome: `()`, which is today's behaviour and a complete one."""
    assert propose_and_guard(_context(), None) == ((), ())


def test_an_accepted_proposal_comes_back() -> None:
    """Outcome: the assignment, once it has passed every check."""
    assert propose_and_guard(_context(), _model()) == (
        (ProposedAssignment(field_key="SHOP:CT010", candidate_ids=("c1",)),),
        (),
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

    accepted, unverified = propose_and_guard(_run_context(), model)

    assert len(client.calls) == 2
    assert unverified == (), "every reading in this context is attached"
    assert accepted == (
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

    assert propose_and_guard(_run_context(), model) == ((), ())
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


# ---------------------------------------------------------------------------
# What it reports while it works
# ---------------------------------------------------------------------------


def test_the_observer_sees_the_retry_that_the_return_value_hides() -> None:
    """**Input: a refused-then-corrected run. Outcome: the observer saw five phases, not two.**

    The reason the hook exists. A first-time success and a refused-then-corrected success return the
    same tuple, so a screen watching only the result cannot tell a checked answer from an unchecked
    one — and that the answer is checked is the single most useful thing this feature can show a
    reviewer.
    """
    client = _CorrectsOnceClient()
    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=client,
    )
    seen: list[AssignmentProgress] = []

    accepted, _ = propose_and_guard(_run_context(), model, observer=seen.append)

    assert accepted, "the corrected answer was not returned"
    assert [progress.phase for progress in seen] == [
        "asking",
        "checking",
        "refused",
        "asking",
        "checking",
        "accepted",
    ]
    assert [progress.attempt for progress in seen] == [1, 1, 1, 2, 2, 2]
    # The refusal it reports is the guard's own sentence, not a summary written here: the screen
    # quotes it to the reviewer, and a paraphrase would be this module explaining a decision it did
    # not make.
    refused = next(progress for progress in seen if progress.phase == "refused")
    assert "One reading measures one thing" in refused.detail


def test_the_observer_changes_nothing_about_the_answer() -> None:
    """Outcome: the same result with an observer and without one.

    Asserted rather than assumed, because a reporting hook that can alter an outcome is no longer
    reporting. Nothing in `propose_and_guard` reads what the observer returns, and this is what
    keeps that true as the function changes.
    """

    def run(observer: Any) -> tuple[Any, ...]:
        client = _CorrectsOnceClient()
        model = BedrockAssignmentModel(
            config=_Config(
                model_id="m",
                region_name="us-east-1",
                connect_timeout_seconds=1,
                read_timeout_seconds=2,
            ),
            client=client,
        )
        return propose_and_guard(_run_context(), model, observer=observer)

    assert run(None) == run(lambda _progress: "a return value nothing reads")


def test_a_deployment_with_no_model_says_so_rather_than_going_quiet() -> None:
    """Outcome: one `unavailable` phase.

    The screen has to distinguish "no model is configured here" from "the model was asked and
    answered nothing". Both leave the fields empty; only one of them is worth a reviewer's attention.
    """
    seen: list[AssignmentProgress] = []

    assert propose_and_guard(_run_context(), None, observer=seen.append) == ((), ())

    assert [progress.phase for progress in seen] == ["unavailable"]
    assert "no model is configured" in seen[0].detail


# ---------------------------------------------------------------------------
# #712 — the model was reached, and its answer did not fit
# ---------------------------------------------------------------------------


def _uuid_context(
    count: int, *, fields: tuple[Field, ...] = (DEPTH, CABINETS)
) -> AssignmentContext:
    return AssignmentContext(
        fields=fields,
        readings=tuple(
            _reading(f"00000000-0000-4000-8000-{index:012d}", f"{index} in")
            for index in range(count)
        ),
    )


def test_the_model_names_readings_by_short_handle_not_by_candidate_id() -> None:
    """**Outcome: `r1`, `r2` in the payload and the schema; no candidate id anywhere the model looks.**

    A candidate id is a 36-character UUID, about thirty tokens each time the answer names one.
    Measured with a request the size of the client drawing's, 20 readings used 910 of 960 answer
    tokens and 40 broke the call outright. A handle costs a few.
    """
    context = _uuid_context(3)
    model = _model(_response([]))

    model.propose(context)

    request = model.client.calls[0]  # type: ignore[union-attr]
    shown = json.dumps(request)
    assert all(reading.candidate_id not in shown for reading in context.readings)
    readings = json.loads(request["messages"][0]["content"][2]["text"])["readings"]
    assert [reading["candidate_id"] for reading in readings] == ["r1", "r2", "r3"]
    schema = request["toolConfig"]["tools"][0]["toolSpec"]["inputSchema"]["json"]
    ids = schema["properties"]["assignments"]["items"]["properties"]["candidate_ids"]["items"]
    assert ids["enum"] == ["r1", "r2", "r3"]


def test_a_handle_comes_back_as_the_candidate_it_names() -> None:
    """**Outcome: the guard and the filed proposal see real candidate ids, never a handle.**"""
    context = _uuid_context(3)
    model = _model(_response([{"field_key": "SHOP:cabinet_width", "candidate_ids": ["r3", "r1"]}]))

    proposed = model.propose(context)

    assert proposed == (
        ProposedAssignment(
            field_key="SHOP:cabinet_width",
            candidate_ids=(context.readings[2].candidate_id, context.readings[0].candidate_id),
        ),
    )


def test_a_handle_nobody_issued_is_refused_by_name() -> None:
    """**Input: a handle the schema never listed. Outcome: the guard refuses it as unknown.**

    It cannot be emitted under the schema. If one arrives anyway it is passed through unchanged
    rather than dropped, so the refusal names what the model actually said.
    """
    context = _uuid_context(2)
    model = _model(_response([{"field_key": "SHOP:CT010", "candidate_ids": ["r9"]}]))
    seen: list[AssignmentProgress] = []

    assert propose_and_guard(context, model, observer=seen.append) == ((), ())

    refusals = [progress for progress in seen if progress.phase == "refused"]
    assert refusals and "'r9'" in refusals[0].detail
    assert refusals[0].failure == "guard-refused"


def test_handles_follow_context_order() -> None:
    """Outcome: `r1` is the first reading. Stable, so a retry names the same reading the same way."""
    context = _uuid_context(2)

    assert reading_handles(context) == {
        "r1": context.readings[0].candidate_id,
        "r2": context.readings[1].candidate_id,
    }


def test_the_answer_limit_grows_with_the_readings_not_only_the_fields() -> None:
    """**The measured failure** (#712). Outcome: 123 readings get more room than 8 did.

    Sized by the fields alone, fifteen fields gave 960 tokens whatever the drawing held, and the
    real drawing's answer did not fit. Nova does not stop a tool call at its limit; it abandons it,
    and Bedrock reports an invalid sequence.
    """
    fifteen = tuple(
        Field(key=f"SHOP:F{index}", name=f"f{index}", source="SHOP", many=True)
        for index in range(15)
    )

    small = _token_limit(_uuid_context(8, fields=fifteen))
    large = _token_limit(_uuid_context(123, fields=fifteen))

    assert large > small > 960
    assert _token_limit(_uuid_context(1, fields=(DEPTH,))) == TOKEN_FLOOR
    assert _token_limit(_uuid_context(5000, fields=fifteen)) == TOKEN_CEILING


def test_the_request_carries_the_sized_limit() -> None:
    """Outcome: the limit the call is sent with is the one sized from this context."""
    context = _uuid_context(40)
    model = _model(_response([]))

    model.propose(context)

    assert model.client.calls[0]["inferenceConfig"]["maxTokens"] == _token_limit(context)  # type: ignore[union-attr]


def test_a_reading_the_guard_would_refuse_outright_is_not_offered() -> None:
    """**Outcome: unattached-with-line-work is left out; unattached-without-line-work stays.**

    Measured: told a reading was unattached, the model used it anyway, and one such reading sinks
    the whole batch. On the client's drawing 115 of 123 readings were unattached. The one the guard
    abstains on — a page with no line-work — is still offered, because it can still fill a field.
    """
    context = AssignmentContext(
        fields=(CABINETS,),
        readings=(
            _reading("c1", "15 in"),
            _reading("c2", "36 in", line_key=None),
            _reading("c3", "3 in", line_key=None, geometry_available=False),
        ),
    )
    model = _model(_response([]))

    propose_and_guard(context, model)

    offered = json.loads(model.client.calls[0]["messages"][0]["content"][2]["text"])["readings"]  # type: ignore[union-attr]
    assert [reading["value"] for reading in offered] == ["15 in", "3 in"]


def test_a_page_of_only_floating_numbers_asks_nothing_and_says_why() -> None:
    """**Outcome: no call, and a sentence a reviewer can act on.**"""
    context = AssignmentContext(
        fields=(CABINETS,),
        readings=(_reading("c1", "15 in", line_key=None), _reading("c2", "36 in", line_key=None)),
    )
    model = _model()
    seen: list[AssignmentProgress] = []

    assert propose_and_guard(context, model, observer=seen.append) == ((), ())

    assert model.client.calls == []  # type: ignore[union-attr]
    assert seen[-1].failure == "nothing-to-ask"
    assert "none of the 2 readings is attached to a dimension line" in seen[-1].detail


def test_the_guard_still_checks_what_the_model_was_not_shown() -> None:
    """**Outcome: the filter relaxes nothing.** A real id for an unoffered reading is still refused.

    A second adapter might return real ids rather than handles; the pass-through makes that reach
    the guard, and the guard checks the full context, not the narrowed one.
    """
    context = AssignmentContext(
        fields=(DEPTH,),
        readings=(_reading("c1", "25 1/2 in"), _reading("c2", "36 in", line_key=None)),
    )
    model = _model(_response([{"field_key": "SHOP:CT010", "candidate_ids": ["c2"]}]))

    assert propose_and_guard(context, model) == ((), ())


# ---------------------------------------------------------------------------
# #712 — every failure named, so the fix goes the right way
# ---------------------------------------------------------------------------


def _client_error(code: str, message: str = "details") -> Exception:
    from botocore.exceptions import ClientError  # type: ignore[import-untyped]

    return ClientError({"Error": {"Code": code, "Message": message}}, "Converse")


@pytest.mark.parametrize(
    ("code", "kind"),
    [
        ("ThrottlingException", "throttled"),
        ("ServiceQuotaExceededException", "throttled"),
        ("ModelErrorException", "answer-invalid"),
        ("ServiceUnavailableException", "unreachable"),
        ("ModelTimeoutException", "unreachable"),
        ("AccessDeniedException", "call-refused"),
        ("ValidationException", "call-refused"),
        ("ResourceNotFoundException", "call-refused"),
    ],
)
def test_each_bedrock_error_code_has_its_own_kind(code: str, kind: str) -> None:
    """Outcome: the kind follows Bedrock's error code, and the exact error keeps the code."""
    failure, _detail, exact = describe_failure(_client_error(code))

    assert failure == kind
    assert code in exact


def test_the_real_drawings_error_is_named_as_an_invalid_answer_not_an_unreachable_model() -> None:
    """**The error the real model returned** (#712), for a request the size of the client drawing's.

    The drawing's run reported "the model could not be reached". Replayed, the model was reached;
    its answer broke.
    """
    error = _client_error(
        "ModelErrorException",
        "Model produced invalid sequence as part of ToolUse. Please refer to the model tool use "
        "troubleshooting guide.",
    )

    failure, detail, exact = describe_failure(error)

    assert failure == "answer-invalid"
    assert "could not be reached" not in detail
    assert "invalid sequence" in exact


def test_the_screen_sentence_never_carries_the_providers_message() -> None:
    """**Outcome: an ARN in the AWS message reaches the log, not the reviewer's screen.**"""
    arn = "arn:aws:iam::111122223333:user/someone"
    error = _client_error("AccessDeniedException", f"User: {arn} is not authorized")

    _failure, detail, exact = describe_failure(error)

    assert arn not in detail
    assert "AccessDeniedException" in detail
    assert arn in exact


def test_a_network_failure_is_unreachable() -> None:
    from botocore.exceptions import (  # type: ignore[import-untyped]
        EndpointConnectionError,
        ReadTimeoutError,
    )

    for error in (
        EndpointConnectionError(endpoint_url="https://bedrock-runtime.invalid"),
        ReadTimeoutError(endpoint_url="https://bedrock-runtime.invalid"),
        TimeoutError("no route to host"),
    ):
        assert describe_failure(error)[0] == "unreachable"


def test_missing_credentials_is_a_refusal_not_an_outage() -> None:
    from botocore.exceptions import NoCredentialsError  # type: ignore[import-untyped]

    failure, detail, _exact = describe_failure(NoCredentialsError())

    assert failure == "call-refused"
    assert "credentials" in detail


def test_an_answer_stopped_at_its_limit_is_cut_off() -> None:
    """Outcome: `stopReason: max_tokens` is its own kind, so the fix — more room — is obvious."""
    response = _response()
    response["stopReason"] = "max_tokens"

    with pytest.raises(UnusableAnswer) as raised:
        _model(response).propose(_context())

    assert describe_failure(raised.value)[0] == "answer-cut-off"


def test_a_wrong_shaped_answer_is_malformed() -> None:
    """Outcome: an answer whose tool input breaks the schema is `answer-malformed`."""
    response = _response()
    response["output"]["message"]["content"][0]["toolUse"]["input"] = {"assignments": "r1"}

    with pytest.raises(UnusableAnswer) as raised:
        _model(response).propose(_context())

    assert describe_failure(raised.value)[0] == "answer-malformed"


def test_anything_else_is_a_failed_call_with_its_type_kept() -> None:
    failure, detail, exact = describe_failure(KeyError("surprise"))

    assert failure == "call-failed"
    assert detail == "the model call failed"
    assert exact.startswith("KeyError")


def test_the_exact_error_is_bounded() -> None:
    """Outcome: a provider cannot put an unbounded message into the worker's log line."""
    assert len(describe_failure(RuntimeError("x" * 5000))[2]) <= 500


def test_a_throttled_call_is_reported_as_throttled_and_still_returns_nothing() -> None:
    """**Outcome: the observer is told `throttled`; the answer is the same empty pair as ever.**"""

    class _Throttled:
        def converse(self, **_: Any) -> dict[str, Any]:
            raise _client_error("ThrottlingException", "Too many requests")

    model = BedrockAssignmentModel(
        config=_Config(
            model_id="m", region_name="us-east-1", connect_timeout_seconds=1, read_timeout_seconds=2
        ),
        client=_Throttled(),
    )
    seen: list[AssignmentProgress] = []

    assert propose_and_guard(_context(), model, observer=seen.append) == ((), ())

    assert seen[-1].phase == "unavailable"
    assert seen[-1].failure == "throttled"
    assert "ThrottlingException" in (seen[-1].error or "")


def test_no_model_configured_is_its_own_kind() -> None:
    seen: list[AssignmentProgress] = []

    propose_and_guard(_context(), None, observer=seen.append)

    assert seen[-1].failure == "not-configured"
