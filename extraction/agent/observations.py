"""What code can tell about a region and its readings, after every step, without asking a model (#757).

**The checks are what make the loop worth running.** Self-correction with no outside check makes a
reader worse; the same loop given an outside check improves it (docs/NEXT_BUILD_PLAN.md,
arXiv:2310.01798). This module is that outside check. Every observation is a deterministic fact —
the file's geometry, the value parser, a label's own millimetre and inch halves, agreement between
independent readings — and none of them is a model's opinion of itself.

`RegionFacts` is what the file established before anything was read: whether the label is cut off
at the crop's edge, which way it runs, whether it is a stacked fraction, what the shape reader
(#756) read, and what other routes read for the same region. `observe` combines them with what has
happened in the run so far — each `Look` a tool returned, each refinement applied — into the set
of `Fact`s the decision table (`extraction/agent/policy.py`) reads.

Every fact has a plain-English sentence (`describe`), because the reviewer is shown what the reader
tried, and an abstention's reason is the observation log itself.

Source: issue #757 · Verification: `tests/extraction/agent/test_observations.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from evidence.candidate import ObservationCandidate
from extraction.agent.graph import AgentProgress, Look
from extraction.agent.tools import Refinement
from units.dual import DualDimensionParseError, parse_dual
from units.measurement import Measurement
from units.normalise import UnitNormalisationError, normalise_to_inches
from units.notation import canonical_notation, is_compound
from units.policy import Consistency, check_dual

__all__ = ["Fact", "RegionFacts", "describe", "observe", "valid_looks", "value_of"]


class Fact(StrEnum):
    """One thing code established about the region or its readings."""

    LABEL_CUT_AT_EDGE = "label_cut_at_edge"
    SIDEWAYS = "sideways"
    STACKED_FRACTION = "stacked_fraction"
    NO_READING = "no_reading"
    LAST_READING_REFUSED = "last_reading_refused"
    DUAL_HALVES_DISAGREE = "dual_halves_disagree"
    READINGS_AGREE = "readings_agree"
    READINGS_DISAGREE = "readings_disagree"
    SHAPE_READER_READ_IT = "shape_reader_read_it"


_SENTENCES: dict[Fact, str] = {
    Fact.LABEL_CUT_AT_EDGE: "the label is cut off at the edge of the crop",
    Fact.SIDEWAYS: "the label reads sideways",
    Fact.STACKED_FRACTION: "the label is a stacked fraction",
    Fact.NO_READING: "no reader has given one exact value yet",
    Fact.LAST_READING_REFUSED: "the last reading was not one exact value, so it was set aside",
    Fact.DUAL_HALVES_DISAGREE: "the last reading's millimetre and inch halves disagree",
    Fact.READINGS_AGREE: "independent readings agree",
    Fact.READINGS_DISAGREE: "independent readings disagree",
    Fact.SHAPE_READER_READ_IT: "the shape reader read every character",
}


@dataclass(frozen=True, slots=True)
class RegionFacts:
    """What the file's own geometry and the other routes established before the agent looked.

    Every field is deterministic — geometry, the shape reader's exact match, another route's
    recorded value — and none is set from a model's output (#757: the trigger reasons are geometry
    only; model text cannot add one).
    """

    cut_at_edge: bool
    rotation_degrees: int
    """0 upright; 90 or 270 turned. From the label's own run, never from a crop's pixels."""

    stacked_fraction: bool
    shape_reading: Measurement | None
    """What the shape reader read, where it matched every character (#756); otherwise `None`."""

    other_route_values: tuple[Measurement, ...]
    """What other routes read for this region, as exact values. Each is an independent witness."""

    def __post_init__(self) -> None:
        if self.rotation_degrees not in (0, 90, 270):
            raise ValueError("rotation_degrees must be 0, 90 or 270")


def _dual_disagrees(candidate: ObservationCandidate) -> bool:
    """Whether a reading states millimetres and inches that do not agree (`units.policy.check_dual`)."""
    try:
        dual = parse_dual(candidate.raw_text)
    except DualDimensionParseError:
        return False
    return check_dual(dual) is Consistency.INCONSISTENT


def value_of(candidate: ObservationCandidate) -> Measurement | None:
    """A reading's exact value, by the rule every reading is stored with.

    A vision reader returns its text and leaves the value to the pipeline, which parses it through
    `units.notation` (`workflow/stages._vision_candidate_value`). A value the candidate already
    carries is used as it is. A compound, or text the parser refuses, has none.
    """
    if candidate.parsed_value is not None:
        return candidate.parsed_value
    if is_compound(candidate.raw_text):
        return None
    try:
        return normalise_to_inches(canonical_notation(candidate.raw_text)[0])
    except UnitNormalisationError:
        return None


def _usable(look: Look) -> bool:
    candidate = look.candidate
    return value_of(candidate) is not None and not _dual_disagrees(candidate)


def valid_looks(progress: AgentProgress) -> tuple[Look, ...]:
    """The readings this run produced that parse to one exact value and are not self-contradictory.

    A reading that fails either check is set aside "as if it never came" (#757's decision table).
    """
    return tuple(look for look in progress.looks if _usable(look))


def _same(first: Measurement, second: Measurement) -> bool:
    return (first.exact, first.unit) == (second.exact, second.unit)


def observe(facts: RegionFacts, progress: AgentProgress) -> frozenset[Fact]:
    """Every fact that holds now, from the region's geometry and the run so far."""
    found: set[Fact] = set()
    applied = set(progress.refinements)
    if facts.cut_at_edge and Refinement.WHOLE_RUN.value not in applied:
        found.add(Fact.LABEL_CUT_AT_EDGE)
    if facts.rotation_degrees != 0 and Refinement.UPRIGHT.value not in applied:
        found.add(Fact.SIDEWAYS)
    if facts.stacked_fraction:
        found.add(Fact.STACKED_FRACTION)
    if facts.shape_reading is not None:
        found.add(Fact.SHAPE_READER_READ_IT)
    if progress.looks and not _usable(progress.looks[-1]):
        found.add(Fact.LAST_READING_REFUSED)
        if _dual_disagrees(progress.looks[-1].candidate):
            found.add(Fact.DUAL_HALVES_DISAGREE)

    readings = [value_of(look.candidate) for look in valid_looks(progress)]
    values = [value for value in readings if value is not None]
    if not values:
        found.add(Fact.NO_READING)
        return frozenset(found)
    witnesses = list(values) + list(facts.other_route_values)
    if facts.shape_reading is not None:
        witnesses.append(facts.shape_reading)
    if len(witnesses) > 1:
        first = witnesses[0]
        if all(_same(first, other) for other in witnesses[1:]):
            found.add(Fact.READINGS_AGREE)
        else:
            found.add(Fact.READINGS_DISAGREE)
    return frozenset(found)


def describe(facts: RegionFacts, progress: AgentProgress) -> str:
    """What the reader tried and what it found, in one plain-English sentence for a reviewer.

    For example: "the label was cut off, so the crop was widened; it was turned upright; the
    primary reader read 3/4"; the readers disagree (3/4" and 1 3/4")".
    """
    parts: list[str] = []
    for refinement in progress.refinements:
        parts.append(
            {
                Refinement.WHOLE_RUN.value: "the crop was widened to the whole label",
                Refinement.UPRIGHT.value: "the label was turned upright",
                Refinement.SHARPER.value: "it was looked at again, sharper",
            }.get(refinement, f"the crop was refined ({refinement})")
        )
    for look in progress.looks:
        parts.append(f"{look.candidate.extractor} read {look.candidate.raw_text!r}")
    parts.extend(progress.failures)
    parts.extend(_SENTENCES[fact] for fact in sorted(observe(facts, progress)))
    return "; ".join(parts) if parts else "nothing was tried"
