"""How well a finding points at the thing it is about.

`AGENTS.md` §9 makes evidence localisation a release gate: a finding's page and polygon must meet a
threshold. The gate existed in prose and nothing measured it.

**Why page and box are reported separately.** A right page with a wrong box is not a partial success
— a reviewer sent to the wrong part of the right page still cannot verify the finding at a glance,
which is the entire purpose of localising it. But collapsing the two into one number also hides
*which* is broken, and those have completely different causes: a wrong page is a classification or
manifest fault, a wrong box is geometry. The gate requires both; the report separates them.

**Why this supersedes the exact-match version.** `#69` shipped `evidence_localisation_rate` requiring
the predicted polygon to equal the annotated one exactly. That is unusably strict — no extraction
lane reproduces a hand-drawn box to the pixel, so the metric would read zero forever and tell nobody
anything. Intersection-over-union is the standard measure and is what a threshold can be set against.

**Why an unannotated prediction raises.** Scoring only what happens to have an annotation quietly
inflates the result: the cases nobody annotated are exactly the ones most likely to be wrong. A
metric that silently omits them reports a passing gate for a system nobody measured.

Arithmetic is exact. Boxes are integer pixels, so IoU is a ratio of integers and stays a `Fraction`
— a release decision is made from it, and ADR-0001 applies here as much as in the verdict path.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from eval.gold_set.schema import GoldObservation

#: `(x0, y0, x1, y1)` in stored pixel space, as `GoldObservation.polygon` carries it.
Box = tuple[int, int, int, int]

#: Groups observations for reporting. Injected because a `GoldObservation` records a semantic type
#: and not a check type — deriving one from the other would attribute a localisation error to a
#: check chosen by us rather than by the annotation.
CheckTypeOf = Callable[[GoldObservation], str]

#: What everything falls under when the caller has no grouping to offer. Named rather than blank so
#: a report never implies a breakdown it does not have.
ALL_CHECKS = "all"

#: Used when a caller has stated no threshold of its own — notably `eval/metrics.py`, which computes
#: the headline rate. Deliberately a named constant rather than a literal buried in a call: a
#: release gate's limit should be greppable and arguable, and 0.5 IoU is the conventional starting
#: point that Q-style client confirmation may later replace.
DEFAULT_IOU_THRESHOLD = Fraction(1, 2)


class MissingAnnotation(Exception):
    """A prediction had nothing to compare against.

    Raised rather than skipped. The cases nobody annotated are the ones most likely to be wrong, so
    quietly omitting them inflates the score in exactly the wrong direction.
    """


class DegenerateBox(ValueError):
    """A box with no area. Neither a match nor a miss — a measurement that cannot mean anything."""


def area(box: Box) -> int:
    """Pixel area, or zero for a box with no extent."""
    x0, y0, x1, y1 = box
    return max(0, x1 - x0) * max(0, y1 - y0)


def intersection_over_union(predicted: Box, annotated: Box) -> Fraction:
    """Overlap as an exact ratio.

    Integer pixels in, `Fraction` out. Computing this in floating point would put a rounded number
    behind a release gate, and two runs that localised identically could then compare unequal.
    """
    for box in (predicted, annotated):
        if area(box) == 0:
            raise DegenerateBox(
                f"box {box} has no area, so overlap with it is undefined. A zero-area annotation is "
                "a bad annotation, not a failed prediction."
            )

    px0, py0, px1, py1 = predicted
    ax0, ay0, ax1, ay1 = annotated
    overlap = max(0, min(px1, ax1) - max(px0, ax0)) * max(0, min(py1, ay1) - max(py0, ay0))
    union = area(predicted) + area(annotated) - overlap
    return Fraction(overlap, union)


@dataclass(frozen=True, slots=True)
class LocalisationResult:
    """Localisation for one check type: page and box, judged separately, gated together."""

    check_type: str
    page_correct: int
    box_correct: int
    both_correct: int
    """Predictions where page **and** box are right on the *same* observation.

    Not `min(page_correct, box_correct)`: those can count different observations. One prediction on
    the right page with a bad box and another on the wrong page with a good box gives one of each,
    and a minimum of one — reporting half the observations localised when none of them are.
    """

    compared: int
    mean_iou: Fraction | None
    threshold: Fraction

    @property
    def page_accuracy(self) -> Fraction | None:
        """`None` when nothing was compared — never zero, which would read as total failure."""
        return Fraction(self.page_correct, self.compared) if self.compared else None

    @property
    def box_accuracy(self) -> Fraction | None:
        """Share of predictions whose IoU met the threshold."""
        return Fraction(self.box_correct, self.compared) if self.compared else None

    @property
    def joint_accuracy(self) -> Fraction | None:
        """Share of predictions that are fully localised — the number the gate is about."""
        return Fraction(self.both_correct, self.compared) if self.compared else None

    @property
    def measured(self) -> bool:
        return self.compared > 0

    @property
    def passed(self) -> bool | None:
        """The share fully localised must clear the threshold, or `None` when nothing was measured.

        `None` rather than `False`: an unmeasured gate and a failed one are different facts, and the
        release report is built on keeping them apart.
        """
        if not self.measured:
            return None
        joint = self.joint_accuracy
        assert joint is not None
        # The joint rate, not the two rates independently. Requiring each separately would pass a
        # set where 90% of pages and 90% of boxes are right but the failures are disjoint, so only
        # 80% of findings actually point at anything.
        return joint >= self.threshold

    def __str__(self) -> str:
        if not self.measured:
            return f"{self.check_type}: NOT MEASURED — nothing was compared"

        def pct(value: Fraction | None) -> str:
            return "—" if value is None else f"{float(value) * 100:.1f}%"

        verdict = "PASS" if self.passed else "FAIL"
        return (
            f"{self.check_type}: {pct(self.joint_accuracy)} fully localised "
            f"(page {pct(self.page_accuracy)}, box {pct(self.box_accuracy)}, "
            f"mean IoU {pct(self.mean_iou)}) — {verdict} against {pct(self.threshold)}"
        )


def measure(
    predicted: Sequence[GoldObservation],
    annotated: Sequence[GoldObservation],
    *,
    threshold: Fraction,
    check_type_of: CheckTypeOf | None = None,
) -> dict[str, LocalisationResult]:
    """Measure localisation per check type.

    `threshold` is required. A default would be a magic number deciding a release gate, and the one
    thing a threshold must be is somebody's stated decision.

    Raises `MissingAnnotation` when a prediction has no counterpart — see the module docstring.
    """
    if not 0 <= threshold <= 1:
        raise ValueError(
            f"threshold {threshold} is not a proportion. Above 1 nothing can ever pass and below 0 "
            "everything does — either way the gate stops meaning anything, silently."
        )

    truth: Mapping[tuple[str, object], GoldObservation] = {
        (obs.item_id, obs.semantic_type): obs for obs in annotated
    }

    unannotated = [
        f"{obs.item_id}/{obs.semantic_type}"
        for obs in predicted
        if (obs.item_id, obs.semantic_type) not in truth
    ]
    if unannotated:
        raise MissingAnnotation(
            f"{len(unannotated)} prediction(s) have no annotation to compare against: "
            f"{sorted(unannotated)[:5]}. Scoring only the annotated ones would inflate the result — "
            "the unannotated cases are the ones most likely to be wrong."
        )

    grouped: dict[str, list[tuple[GoldObservation, GoldObservation]]] = {}
    for observation in predicted:
        reference = truth[(observation.item_id, observation.semantic_type)]
        key = check_type_of(observation) if check_type_of else ALL_CHECKS
        grouped.setdefault(key, []).append((observation, reference))

    results: dict[str, LocalisationResult] = {}
    for check_type, pairs in grouped.items():
        page_correct = 0
        box_correct = 0
        both_correct = 0
        ious: list[Fraction] = []
        for observation, reference in pairs:
            page_ok = observation.page == reference.page
            overlap = intersection_over_union(observation.polygon, reference.polygon)
            box_ok = overlap >= threshold
            ious.append(overlap)
            page_correct += page_ok
            box_correct += box_ok
            both_correct += page_ok and box_ok
        results[check_type] = LocalisationResult(
            check_type=check_type,
            page_correct=page_correct,
            box_correct=box_correct,
            both_correct=both_correct,
            compared=len(pairs),
            mean_iou=sum(ious, Fraction(0)) / len(ious) if ious else None,
            threshold=threshold,
        )

    if not results:
        results[ALL_CHECKS] = LocalisationResult(
            check_type=ALL_CHECKS,
            page_correct=0,
            box_correct=0,
            both_correct=0,
            compared=0,
            mean_iou=None,
            threshold=threshold,
        )
    return results


def report(results: Mapping[str, LocalisationResult]) -> str:
    """A plain-English summary, per check type because the gate is per check type."""
    lines = ["Evidence localisation (AGENTS.md §9)", ""]
    lines.extend(f"  {results[key]}" for key in sorted(results))
    unmeasured = [key for key, result in results.items() if not result.measured]
    if unmeasured:
        lines.extend(
            [
                "",
                (
                    f"{len(unmeasured)} check type(s) were not measured. An unmeasured gate is not "
                    "a passed one."
                ),
            ]
        )
    return "\n".join(lines)
