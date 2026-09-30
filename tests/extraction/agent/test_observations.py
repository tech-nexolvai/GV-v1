"""What code establishes about a region and its readings, fact by fact (#757).

Verification for: `extraction/agent/observations.py`.

Each fact gets a test that it holds when it should, does not when it should not, and says what it
is in plain English — the reviewer is shown these sentences as "what the reader tried". The one
that matters most is `test_no_reading_can_add_a_trigger_reason`: the trigger's geometry reasons are
computed from facts that hold no text, so nothing a model returns can put a region in front of the
agent.
"""

from __future__ import annotations

import dataclasses
import inspect
from fractions import Fraction

import pytest

from evidence.candidate import ObservationCandidate
from evidence.coordinates import ImagePoint
from extraction.agent.graph import AgentProgress, BoundedRegionContext, Look
from extraction.agent.observations import (
    Fact,
    RegionFacts,
    describe,
    observe,
    trigger_reasons,
    valid_looks,
    value_of,
)
from extraction.agent.tools import Refinement, ToolName
from extraction.agent.trigger import AmbiguityReason
from units.measurement import Measurement, Unit

CONTEXT = BoundedRegionContext(
    region_id="r", crop_artifact_id="crop-0", nearby_text=(), nearby_geometry_refs=()
)


def _facts(**changes: object) -> RegionFacts:
    values: dict[str, object] = {
        "cut_at_edge": False,
        "rotation_degrees": 0,
        "stacked_fraction": False,
        "shape_reading": None,
        "other_route_values": (),
    }
    values.update(changes)
    return RegionFacts(**values)  # type: ignore[arg-type]


def _reading(text: str, reader: str = "vision") -> ObservationCandidate:
    return ObservationCandidate(
        candidate_id=f"{reader}-{text}",
        extractor=reader,
        extractor_version="1",
        raw_text=text,
        parsed_value=None,
        unit_guess=Unit.INCH,
        semantic_guess=None,
        page=0,
        polygon=(ImagePoint(0, 0), ImagePoint(1, 1)),
        confidence=None,
        ambiguity_flags=(),
    )


def _progress(*texts: str, refinements: tuple[str, ...] = ()) -> AgentProgress:
    return AgentProgress(
        context=CONTEXT,
        steps=len(texts) + len(refinements),
        ocr_calls=0,
        primary_vlm_calls=min(len(texts), 1),
        vlm_escalations=max(len(texts) - 1, 0),
        current_crop_artifact_id="crop-0",
        looks=tuple(
            Look(f"r:{n}:vlm", ToolName.REQUEST_VLM_READING, _reading(text, f"reader-{n}"))
            for n, text in enumerate(texts)
        ),
        refinements=refinements,
        failures=(),
        attempted=(),
        last_result=None,
    )


def _inches(value: Fraction | int) -> Measurement:
    return Measurement(Fraction(value), Unit.INCH, f"{value} in")


# ---------------------------------------------------------------------------
# The trigger's geometry reasons
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"cut_at_edge": True}, AmbiguityReason.LABEL_CUT_AT_EDGE),
        ({"rotation_degrees": 90}, AmbiguityReason.SIDEWAYS_LABEL),
        ({"rotation_degrees": 270}, AmbiguityReason.SIDEWAYS_LABEL),
        ({"stacked_fraction": True}, AmbiguityReason.STACKED_FRACTION),
    ],
)
def test_each_geometry_fact_gives_its_reason(changes: dict[str, object], reason: object) -> None:
    assert trigger_reasons(_facts(**changes)) == frozenset({reason})


def test_a_whole_upright_label_gives_no_reason_whatever_was_read() -> None:
    """Outcome: agreeing witnesses and a shape reading are not triggers; only geometry is."""
    facts = _facts(shape_reading=_inches(12), other_route_values=(_inches(12),))

    assert trigger_reasons(facts) == frozenset()


def test_no_reading_can_add_a_trigger_reason() -> None:
    """**Acceptance criterion: the new reasons are set only from geometry.** Outcome: the function
    takes the facts and nothing else, and no fact is text — so no reader's words reach it."""
    assert list(inspect.signature(trigger_reasons).parameters) == ["facts"]
    kinds = {field.name: field.type for field in dataclasses.fields(RegionFacts)}
    assert kinds == {
        "cut_at_edge": "bool",
        "rotation_degrees": "int",
        "stacked_fraction": "bool",
        "shape_reading": "Measurement | None",
        "other_route_values": "tuple[Measurement, ...]",
    }


def test_a_turn_other_than_a_quarter_is_refused() -> None:
    with pytest.raises(ValueError, match="rotation_degrees"):
        _facts(rotation_degrees=45)


# ---------------------------------------------------------------------------
# The facts, after each step
# ---------------------------------------------------------------------------


def test_a_widened_crop_is_no_longer_cut_and_a_turned_one_no_longer_sideways() -> None:
    facts = _facts(cut_at_edge=True, rotation_degrees=90)

    before = observe(facts, _progress())
    after = observe(
        facts, _progress(refinements=(Refinement.WHOLE_RUN.value, Refinement.UPRIGHT.value))
    )

    assert {Fact.LABEL_CUT_AT_EDGE, Fact.SIDEWAYS} <= before
    assert not {Fact.LABEL_CUT_AT_EDGE, Fact.SIDEWAYS} & after


def test_a_reading_that_is_not_one_value_is_set_aside() -> None:
    progress = _progress("see detail")

    assert valid_looks(progress) == ()
    assert {Fact.LAST_READING_REFUSED, Fact.NO_READING} <= observe(_facts(), progress)


def test_a_reading_whose_halves_disagree_is_set_aside_and_says_why() -> None:
    """Outcome: `254 [11]` parses, but 254 mm is 10 inches, not 11."""
    observed = observe(_facts(), _progress("254 [11]"))

    assert {Fact.LAST_READING_REFUSED, Fact.DUAL_HALVES_DISAGREE} <= observed


def test_a_compound_is_not_one_value() -> None:
    assert value_of(_reading('39 1/4"+6"')) is None


def test_a_reading_is_valued_by_the_rule_readings_are_stored_by() -> None:
    assert value_of(_reading('26 3/4"')) == Measurement(Fraction(107, 4), Unit.INCH, '26 3/4"')


def test_readings_agree_only_when_every_witness_does() -> None:
    """**No majority.** Outcome: one witness out of line is a disagreement."""
    agreeing = _facts(other_route_values=(_inches(12),), shape_reading=_inches(12))
    one_out = _facts(other_route_values=(_inches(12),), shape_reading=_inches(13))

    assert Fact.READINGS_AGREE in observe(agreeing, _progress('12"'))
    assert Fact.READINGS_DISAGREE in observe(one_out, _progress('12"'))


def test_one_reading_and_no_witness_neither_agrees_nor_disagrees() -> None:
    observed = observe(_facts(), _progress('12"'))

    assert not {Fact.READINGS_AGREE, Fact.READINGS_DISAGREE, Fact.NO_READING} & observed


# ---------------------------------------------------------------------------
# What the reviewer is told
# ---------------------------------------------------------------------------


def test_what_the_reader_tried_is_told_in_plain_english() -> None:
    """Outcome: the refinements, each reading and what the checks found, in order."""
    facts = _facts(cut_at_edge=True, other_route_values=(_inches(Fraction(15, 4)),))
    progress = _progress('3/4"', refinements=(Refinement.WHOLE_RUN.value,))

    told = describe(facts, progress)

    assert told.startswith("the crop was widened to the whole label; reader-0 read '3/4\"'")
    assert "independent readings disagree" in told


def test_every_fact_has_a_sentence() -> None:
    for fact in Fact:
        facts = _facts(
            cut_at_edge=fact is Fact.LABEL_CUT_AT_EDGE,
            rotation_degrees=90 if fact is Fact.SIDEWAYS else 0,
            stacked_fraction=fact is Fact.STACKED_FRACTION,
        )
        assert describe(facts, _progress())


def test_before_any_step_the_reviewer_is_told_no_reading_was_given() -> None:
    assert (
        describe(
            _facts(),
            AgentProgress(
                context=CONTEXT,
                steps=0,
                ocr_calls=0,
                primary_vlm_calls=0,
                vlm_escalations=0,
                current_crop_artifact_id="crop-0",
                looks=(),
                refinements=(),
                failures=(),
                attempted=(),
                last_result=None,
            ),
        )
        == "no reader has given one exact value yet"
    )
