"""Filing the proposal, and reading back the one that is current (#596).

`workflow/assignment.py` is the checking half and `assignment_bedrock` the asking half. This is the
*keeping* half, and it exists because a proposal a reviewer has to ask for is a form that arrives
empty: the model was paid for again on every page load, and nothing recorded that it had ever run.

Nothing here calls a provider. What is verified is which rows a proposal becomes, which set is
current when there is more than one, and — the one that matters — that no number is copied into
them.

Source: issue #596 · Verified module: `workflow/propose.py`
"""

from __future__ import annotations

from uuid import uuid4

from workflow.assignment import ProposedAssignment


def test_a_refused_proposal_files_nothing() -> None:
    """**Outcome: no rows, and `None` back.**

    A refusal leaves the fields empty for the reviewer, which is exactly what an absent row already
    says. A row recording the refusal would be a second way of saying nothing, and a reader would
    then have to know which of the two means "do not put a value on the screen".
    """
    from workflow.propose import record_proposal

    written: list[object] = []

    class _Session:
        def add(self, row: object) -> None:
            written.append(row)

        def flush(self) -> None:
            pass

    assert (
        record_proposal(
            _Session(),  # type: ignore[arg-type]
            package_revision_id=uuid4(),
            assignments=(),
            model_id="stub",
        )
        is None
    )
    assert written == []


def test_a_run_is_filed_in_the_order_the_drawing_draws_it() -> None:
    """**Input: three readings for one many-valued field. Outcome: positions 0, 1, 2.**

    Position is what `CT-WIDTH-001` compares — two runs, slot by slot — so the order is a fact about
    the sheet and is stored rather than left to be recovered from insertion order later.
    """
    from workflow.propose import record_proposal

    written: list[object] = []

    class _Session:
        def add(self, row: object) -> None:
            written.append(row)

        def flush(self) -> None:
            pass

    revision = uuid4()
    ids = [str(uuid4()) for _ in range(3)]
    proposal_id = record_proposal(
        _Session(),  # type: ignore[arg-type]
        package_revision_id=revision,
        assignments=(ProposedAssignment("SHOP:cabinet_width", tuple(ids)),),
        model_id="stub-model-v1",
    )

    assert proposal_id is not None
    assert [row.position for row in written] == [0, 1, 2]  # type: ignore[attr-defined]
    assert [str(row.candidate_id) for row in written] == ids  # type: ignore[attr-defined]
    assert {row.proposal_id for row in written} == {proposal_id}  # type: ignore[attr-defined]
    assert {row.package_revision_id for row in written} == {revision}  # type: ignore[attr-defined]


def test_a_proposal_row_carries_no_value() -> None:
    """**The one that keeps a proposal from becoming a measurement.**

    A row names a candidate; the number comes from that candidate's own exact numerator and
    denominator when the form is rendered. A value copied here would be a second copy of a
    measurement, free to drift from the reading it came from — and a table of numbers that looks
    like measurements is one somebody eventually reads as measurements.

    Asserted on the model's columns rather than on this module's behaviour, because a column is
    where the temptation would land.
    """
    from app.models.evidence import MeasurementProposal

    columns = set(MeasurementProposal.__table__.columns.keys())
    for forbidden in ("value", "value_numerator", "value_denominator", "unit", "exact"):
        assert forbidden not in columns, columns
    assert {"field_key", "position", "candidate_id"} <= columns


def test_the_model_and_prompt_are_recorded_with_the_proposal() -> None:
    """Outcome: both on every row.

    A proposal a reviewer disagrees with has to be traceable to the configuration that produced it
    rather than to "the AI". `PROMPT_ID` is the prompt's identity, not its text — the text is in the
    adapter and versioned with it.
    """
    from workflow.assignment_bedrock import PROMPT_ID
    from workflow.propose import record_proposal

    written: list[object] = []

    class _Session:
        def add(self, row: object) -> None:
            written.append(row)

        def flush(self) -> None:
            pass

    record_proposal(
        _Session(),  # type: ignore[arg-type]
        package_revision_id=uuid4(),
        assignments=(ProposedAssignment("SHOP:CT010", (str(uuid4()),)),),
        model_id="amazon.nova-lite-v1:0",
    )

    assert [row.model_id for row in written] == ["amazon.nova-lite-v1:0"]  # type: ignore[attr-defined]
    assert [row.prompt_id for row in written] == [PROMPT_ID]  # type: ignore[attr-defined]


def test_the_step_never_raises_at_a_caller_that_read_a_drawing() -> None:
    """**Outcome: a summary, not an exception, whatever goes wrong.**

    This runs at the end of extraction. The drawings have been read and the readings are recorded;
    losing that because a provider was unreachable would make an optional step able to destroy a
    mandatory one. Every failure ends with the reviewer typing the values in, which is what a
    deployment with no model configured does anyway.
    """
    from workflow.propose import propose_for_revision

    class _Session:
        def get(self, *_args: object, **_kwargs: object) -> None:
            return None

    result = propose_for_revision(_Session(), uuid4(), None)  # type: ignore[arg-type]

    assert result["ran"] is False
    assert "reason" in result
