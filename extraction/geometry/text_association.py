"""Which dimension line does this number belong to — and when is it impossible to say?

`984` printed on a sheet is not evidence of anything until it is attached to the line it
annotates. This module makes that attachment, or refuses to. `docs/DESIGN_EXTRACTION.md` §6 names
the failure it exists to prevent: the number reads correctly, the arithmetic is exact, the trace is
complete, and the finding is about the wrong thing. Nothing downstream can catch that, because
everything downstream treats the association as established fact.

**Refusing is the deliverable, not the fallback.** Two lines equally close to one number is the
ordinary case on a dimensioned elevation, not an edge case, and §6 fixes the answer regardless of
what real drawings turn out to look like: an ambiguous association returns *no* association, never
the nearest guess. Every refusal carries the candidates so a reviewer sees what the choice was
between rather than being told only that there was one.

**An unassociated number is still retained.** It is evidence of something — possibly of a dimension
line nobody read. Every text handed in comes back out, in one list or the other, and the result type
refuses to be built if one goes missing.

**Why this does not take #179's `DimensionLine`.** #179 is blocked on real drawings: deciding which
vector primitives *are* dimension lines is a detector, not a parameter, and it is only correct
against this vendor's actual CAD output. Consuming its type here would make this module wait for
that. It takes `DimensionExtent` instead — the same type `containment.py` already uses for the
extent of a dimension line in stored space. Two geometry modules disagreeing about what "a dimension
line" is would mean they disagree about what they are associating text *to*, and that error looks
exactly like a correct association.

**Both numbers are the caller's, and neither has a default.** `proximity_limit` decides how near a
line has to be before it is a candidate at all; without it, a stray number in a title block attaches
itself to the only dimension line on the sheet. `ambiguity_margin` decides how much nearer the best
candidate has to be than the second before the choice counts as made. Both are empirical,
`data/drawings/` is empty, and a default here would ship today's guess as ground truth. They are
required keyword arguments so that no call site can acquire one by accident.

**Both are in stored units, which are not a distance.** Stored coordinates are normalised `0..1`
against the crop box, so one number means a different physical distance on an A4 sheet than on a
24×36 one, and — on any page that is not square — a different distance along x than along y. A
caller holding a physical distance has to convert it per page, and the conversion belongs with
`PageTransform`, which already owns the PDF→stored step.

**The arithmetic is exact, including the comparisons that look like they need a square root.**
Distances are held as exact squared `Fraction`s and never square-rooted; the margin comparison is
rearranged algebraically so that it stays rational (see `_within_margin`). A float here would decide
which line a number belongs to by binary rounding.

**What this deliberately does not do.**

*It does not judge which side of the line the text sits on.* Above, below, left, right: which side
this vendor uses is empirical, so both sides score identically. The visible consequence is that a
number sitting midway between two parallel dimension lines refuses rather than picking the
conventional one. That is the safe direction, and it is a tested behaviour rather than an accident.

*It assumes text is rotated to run along the line it annotates* — "aligned" dimensioning. Under the
other common convention, "unidirectional", every number is printed horizontally no matter which way
its line runs, and this module will refuse every vertical dimension on such a sheet rather than
mis-associate one. That is the safe failure, but it is a failure: if GV's drawings turn out to be
unidirectional, the fix is a stated convention parameter alongside the two above, decided against
real drawings. It is not something to guess now.

*It does not check that the number is plausible for the line's length.* Comparing a read value
against the measured length at sheet scale would be the strongest corroboration available, and it
belongs to the evidence layer: the sheet scale is not established here, and an extractor that knows
what answer a rule wants is an extractor that can be tuned to produce it
(`docs/DESIGN_EXTRACTION.md` §2).

*It does not represent text at an arbitrary angle.* Rotation is restricted to 0/90/180/270 — the
same set `PageTransform` accepts — and anything else is refused at construction rather than rounded
to the nearest axis. The margin separating "near enough to horizontal to read as horizontal" from
"genuinely diagonal" is the same empirical number `containment.py` flags for skewed lines, and
silently rounding 30° to 0° would attach numbers to lines with quiet confidence.

Source: backend proposal Appendix B stage I; system design §16 (wrong item/view association) ·
Design: `docs/DESIGN_EXTRACTION.md` §6 ·
Verification: `tests/extraction/geometry/test_text_association.py`
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from uuid import UUID

from evidence.coordinates import StoredPoint
from evidence.polygon import Polygon, PolygonSpaceMismatchError
from extraction.geometry.containment import Axis, DimensionExtent

type ExactPoint = tuple[Fraction, Fraction]
type AssociationOutcome = TextAssociation | CannotAssociate

#: The rotations a reader can report for a piece of text. Deliberately the same four
#: `evidence.coordinates.PageTransform` accepts — see the module docstring on arbitrary angles.
TEXT_ROTATIONS = frozenset({0, 90, 180, 270})


@dataclass(frozen=True, slots=True)
class DimensionText:
    """A piece of dimension text as geometry: where it sits, and which way it reads.

    Deliberately does *not* carry the number itself. The reading lives on the observation this
    refers to, and a second copy here could drift from it — after which a reviewer shown the
    association and a reviewer shown the observation would be looking at different numbers. This
    module associates geometry; what was read is somebody else's record.
    """

    observation_id: UUID
    extent: Polygon
    """Where the text sits, in stored page coordinates."""

    rotation_degrees: int
    """As reported by the reader, never inferred from the shape of the box. A box around `984` is
    wider than it is tall and a box around `8` is not, so guessing rotation from the box would read
    single-digit dimensions as rotated."""

    leader_endpoint: StoredPoint | None = None
    """Where the leader lands, when the text is set clear of the drawing with a leader drawn to what
    it labels. `None` means no leader was drawn — not that one was drawn and not found."""

    def __post_init__(self) -> None:
        if not isinstance(self.observation_id, UUID):
            raise TypeError("observation_id must be a UUID")
        if not isinstance(self.extent, Polygon):
            raise TypeError("extent must be an evidence.polygon.Polygon in stored space")
        if isinstance(self.rotation_degrees, bool) or not isinstance(self.rotation_degrees, int):
            raise TypeError("rotation_degrees must be an integer")
        if self.rotation_degrees not in TEXT_ROTATIONS:
            raise ValueError(
                "rotation_degrees must be one of 0, 90, 180 or 270. Text at any other angle has no "
                "representation here yet: rounding it to the nearest axis would decide which line "
                "the number belongs to, and the margin that separates 'near enough' from "
                "'genuinely diagonal' can only be set against real drawings."
            )
        if self.leader_endpoint is not None:
            if not isinstance(self.leader_endpoint, StoredPoint):
                raise TypeError("leader_endpoint must be a StoredPoint or None")
            if not isinstance(self.leader_endpoint.x, Decimal) or not isinstance(
                self.leader_endpoint.y, Decimal
            ):
                raise TypeError("leader_endpoint coordinates must be Decimal values")
            if not (
                Decimal(0) <= self.leader_endpoint.x <= Decimal(1)
                and Decimal(0) <= self.leader_endpoint.y <= Decimal(1)
            ):
                raise ValueError("leader_endpoint must stay within stored page bounds 0..1")

    @property
    def reads_along(self) -> Axis:
        """The axis the text runs along. Upside-down text reads along the same axis as upright."""
        return "horizontal" if self.rotation_degrees in (0, 180) else "vertical"


@dataclass(frozen=True, slots=True)
class TextAssociation:
    """One number, attached to one dimension line, with the reasons it was attached."""

    text: DimensionText
    line: DimensionExtent
    signals: tuple[str, ...]
    """Why this pairing, in plain English — one entry per signal that contributed. §2.4: a reviewer
    looking at a finding has to be able to see the reasoning, and "the geometry matched" is not
    reasoning. This is also the first thing to read when an association turns out to be wrong."""

    def __post_init__(self) -> None:
        if not isinstance(self.signals, tuple) or not self.signals:
            raise ValueError(
                "an association must record the signals that produced it. An association nobody can "
                "audit is indistinguishable from a guess."
            )
        if any(not isinstance(signal, str) or not signal.strip() for signal in self.signals):
            raise ValueError("every signal must be a non-empty plain-English string")


@dataclass(frozen=True, slots=True)
class CannotAssociate:
    """A number that was read but not attached, and why. **This is the mark for human confirmation.**

    Returned rather than raised: on a real drawing, being unable to tell which line a number belongs
    to is an ordinary outcome, not a bug. The number is retained — it is evidence of something,
    possibly of a dimension line nobody read — and the candidates come with it so a reviewer sees
    what the choice was between.
    """

    text: DimensionText
    reason: str
    candidates: tuple[DimensionExtent, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.text, DimensionText):
            raise TypeError("text must be the DimensionText that could not be associated")
        if not self.reason.strip():
            raise ValueError("a refusal must say what could not be decided")


@dataclass(frozen=True, slots=True)
class AssociationResult:
    """Everything that was read, sorted into attached and not attached.

    Both halves matter. The unattached half is not a leftover: it is the list of numbers a reviewer
    has to look at, and it is how a missed dimension line shows up as something visible rather than
    as silence.
    """

    associated: tuple[TextAssociation, ...]
    unassociated: tuple[CannotAssociate, ...]

    def __post_init__(self) -> None:
        seen = [entry.text.observation_id for entry in self.associated]
        seen += [entry.text.observation_id for entry in self.unassociated]
        if len(seen) != len(set(seen)):
            raise ValueError(
                "the same text appears twice in the result. A number that is both attached and "
                "retained would be counted twice by anything downstream that reads both halves."
            )

    @property
    def unassociated_observation_ids(self) -> tuple[UUID, ...]:
        """The retained numbers, by observation id, for callers that only need to flag them."""
        return tuple(entry.text.observation_id for entry in self.unassociated)


def associate(
    texts: tuple[DimensionText, ...],
    lines: tuple[DimensionExtent, ...],
    *,
    proximity_limit: Decimal,
    ambiguity_margin: Decimal,
) -> AssociationResult:
    """Attach each number to the dimension line it annotates, or retain it unattached.

    `proximity_limit` is how far from a line a piece of text may sit and still be considered its
    annotation. `ambiguity_margin` is how much nearer the best candidate must be than the next
    before the choice counts as made — within it, the answer is no association. Both are in stored
    units and both are required; see the module docstring for why neither has a default.

    Raises `PolygonSpaceMismatchError` if the texts and lines are not all from the same page of the
    same document version. That is not ambiguity to mark for review, it is a caller mistake:
    normalised coordinates from two different sheets are not comparable, and the distances between
    them would be arithmetic performed on unrelated numbers.
    """
    _check_measure("proximity_limit", proximity_limit)
    _check_measure("ambiguity_margin", ambiguity_margin)
    _check_inputs(texts, lines)
    _require_same_page(texts, lines)

    limit = Fraction(proximity_limit)
    margin = Fraction(ambiguity_margin)

    associated: list[TextAssociation] = []
    unassociated: list[CannotAssociate] = []
    for text in texts:
        outcome = _associate_one(text, lines, limit=limit, margin=margin)
        if isinstance(outcome, TextAssociation):
            associated.append(outcome)
        else:
            unassociated.append(outcome)
    return AssociationResult(tuple(associated), tuple(unassociated))


def _associate_one(
    text: DimensionText,
    lines: tuple[DimensionExtent, ...],
    *,
    limit: Fraction,
    margin: Fraction,
) -> AssociationOutcome:
    """Decide one number's line, or refuse and say what stopped it."""
    if text.leader_endpoint is not None:
        anchor = (Fraction(text.leader_endpoint.x), Fraction(text.leader_endpoint.y))
    else:
        anchor = _centre(text.extent)

    in_range = [(line, _distance_squared(anchor, line)) for line in lines]
    in_range = [(line, distance) for line, distance in in_range if distance <= limit * limit]
    if not in_range:
        return CannotAssociate(
            text,
            _out_of_range_reason(text, lines),
            tuple(lines),
        )

    if text.leader_endpoint is not None:
        # A leader is the drafter saying, in the drawing itself, what this text labels. It is a
        # stronger statement than orientation, and it is drawn precisely when the text could not be
        # placed on its line — so it usually is not rotated to match. Requiring orientation
        # agreement as well would refuse the placement mode the leader exists to support.
        candidates = in_range
    else:
        # A line that runs equally in both directions has no orientation, so it can be judged
        # neither compatible nor incompatible with the text. Quietly dropping it would be worse
        # than refusing: the text would fall through to the next line along and be attached to it
        # with full confidence, and the line that was actually nearest would appear nowhere.
        if any(line.axis is None for line, _ in in_range):
            return CannotAssociate(
                text,
                "a dimension line near this text runs equally in both directions, so there is no "
                "way to tell whether it reads the same way the text does. Passing over it would "
                "hand the number to a line further away and record none of this.",
                tuple(line for line, _ in in_range),
            )
        candidates = [
            (line, distance) for line, distance in in_range if line.axis == text.reads_along
        ]
        if not candidates:
            return CannotAssociate(
                text,
                f"{len(in_range)} dimension line(s) are near enough, but none of them runs "
                f"{text.reads_along}ly, as this text does. Dimension text is rotated to run along "
                "the line it annotates, so a line running the other way is a different dimension — "
                "and the line this number belongs to has not been read.",
                tuple(line for line, _ in in_range),
            )

    candidates.sort(key=lambda pair: pair[1])
    nearest, nearest_distance = candidates[0]

    if len(candidates) > 1:
        runner_up_distance = candidates[1][1]
        if _within_margin(nearest_distance, runner_up_distance, margin):
            return CannotAssociate(
                text,
                f"the two nearest of {len(candidates)} candidate dimension lines are closer "
                "together than the stated ambiguity margin, so there is no ground for preferring "
                "either. The nearest guess is exactly what must not be returned here: it would "
                "produce a finding about the wrong dimension that reads as correct all the way "
                "through. The candidates are listed nearest first.",
                tuple(line for line, _ in candidates),
            )

    return TextAssociation(
        text=text,
        line=nearest,
        signals=_signals(text, nearest, anchor, len(candidates)),
    )


def _centre(extent: Polygon) -> ExactPoint:
    """The centre of the text's bounding box.

    Measured from the centre rather than from the nearest edge because the box a reader reports is
    a little loose around the glyphs, and how loose varies by reader. The centre moves far less
    under that than an edge does.
    """
    xs = [Fraction(point.x) for point in extent.points]
    ys = [Fraction(point.y) for point in extent.points]
    return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)


def _distance_squared(point: ExactPoint, line: DimensionExtent) -> Fraction:
    """Exact squared distance from a point to the line *segment*, never square-rooted.

    Squared, because comparing distances never needs the root and taking one would leave exact
    arithmetic for the sake of a number nobody reads. To the segment rather than the infinite line,
    because a dimension line stops where the dimension stops: a number level with a short line but
    far off its end is not that line's text.
    """
    px, py = point
    ax, ay = Fraction(line.start.x), Fraction(line.start.y)
    bx, by = Fraction(line.end.x), Fraction(line.end.y)
    dx, dy = bx - ax, by - ay
    # Never zero: DimensionExtent refuses a line whose endpoints are identical.
    length_squared = dx * dx + dy * dy
    along = ((px - ax) * dx + (py - ay) * dy) / length_squared
    clamped = min(max(along, Fraction(0)), Fraction(1))
    nearest_x, nearest_y = ax + clamped * dx, ay + clamped * dy
    return (px - nearest_x) ** 2 + (py - nearest_y) ** 2


def _closest_point_on(point: ExactPoint, line: DimensionExtent) -> ExactPoint:
    """The point on the segment nearest `point`. Used only to describe the placement."""
    px, py = point
    ax, ay = Fraction(line.start.x), Fraction(line.start.y)
    bx, by = Fraction(line.end.x), Fraction(line.end.y)
    dx, dy = bx - ax, by - ay
    along = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    clamped = min(max(along, Fraction(0)), Fraction(1))
    return (ax + clamped * dx, ay + clamped * dy)


def _within_margin(nearest: Fraction, runner_up: Fraction, margin: Fraction) -> bool:
    """Whether the two best candidates are too close together to choose between.

    Both distances arrive squared. The question is about unsquared distances —
    `sqrt(runner_up) - sqrt(nearest) <= margin` — and a square root is not exact, so the comparison
    is rearranged until it is rational:

        sqrt(runner_up) <= sqrt(nearest) + margin
        runner_up <= nearest + 2·margin·sqrt(nearest) + margin²
        runner_up - nearest - margin² <= 2·margin·sqrt(nearest)

    If the left-hand side is zero or negative the inequality holds outright. If it is positive both
    sides are positive, so squaring preserves it, and the remaining square root disappears:

        (runner_up - nearest - margin²)² <= 4·margin²·nearest

    Every term is a `Fraction`, so this is an exact answer to a question that looks like it needs a
    root. With a margin of zero it reduces to "refuse only on an exact tie", which is correct.
    """
    excess = runner_up - nearest - margin * margin
    if excess <= 0:
        return True
    return excess * excess <= 4 * margin * margin * nearest


def _signals(
    text: DimensionText,
    line: DimensionExtent,
    anchor: ExactPoint,
    candidate_count: int,
) -> tuple[str, ...]:
    """The plain-English reasons this pairing was made, for the reviewer and for debugging."""
    signals = ["the text is within the stated proximity limit of this dimension line"]

    if text.leader_endpoint is not None:
        signals.append(
            "a leader is drawn from the text and its endpoint lands on this line, which is the "
            "drawing itself saying what the text labels"
        )
    else:
        signals.append(
            f"the text reads {text.reads_along}ly and so does the line, which is how dimension text "
            "is placed: rotated to run along what it measures"
        )
        signals.append(_placement(text, line, anchor))

    if candidate_count == 1:
        signals.append(
            "no other dimension line was a candidate, so there was nothing to choose between"
        )
    else:
        signals.append(
            f"{candidate_count} lines were candidates; this one is nearest, and the next nearest is "
            "further away than the stated ambiguity margin"
        )
    return tuple(signals)


def _placement(text: DimensionText, line: DimensionExtent, anchor: ExactPoint) -> str:
    """Which of the standard placements this looks like.

    Descriptive only — it records what a reviewer would see and changes no decision. Which side of
    a line a vendor puts its text on is empirical, so "above" and "below" are not distinguished
    here and neither is scored higher than the other.
    """
    nearest_x, nearest_y = _closest_point_on(anchor, line)
    xs = [Fraction(point.x) for point in text.extent.points]
    ys = [Fraction(point.y) for point in text.extent.points]
    inside = min(xs) <= nearest_x <= max(xs) and min(ys) <= nearest_y <= max(ys)
    if inside:
        return (
            "the line passes through the text box: the inline placement, where the dimension line "
            "is broken and the number sits in the gap"
        )
    return (
        "the text sits clear of the line rather than in a break in it — the conventional offset "
        "placement. Which side of the line it sits on is not used as a signal"
    )


def _out_of_range_reason(text: DimensionText, lines: tuple[DimensionExtent, ...]) -> str:
    if not lines:
        return (
            "no dimension lines were found on this page, so there is nothing this number could "
            "annotate. The number is retained: it is evidence of something, possibly of a dimension "
            "line that was not read."
        )
    if text.leader_endpoint is not None:
        return (
            f"a leader is drawn from this text, but its endpoint does not land within the stated "
            f"proximity limit of any of the {len(lines)} dimension line(s) on the page. What it "
            "points at has not been read."
        )
    return (
        f"none of the {len(lines)} dimension line(s) on the page is within the stated proximity "
        "limit of this text. Attaching it to the nearest one anyway is how a number in a title "
        "block becomes a dimension."
    )


def _check_measure(name: str, value: Decimal) -> None:
    """Both parameters get the same treatment: exact, finite, not negative."""
    if not isinstance(value, Decimal):
        raise TypeError(
            f"{name} must be a Decimal. A float would make which line a number belongs to depend "
            "on binary rounding, and the wrong answer would look exactly like the right one."
        )
    if not value.is_finite():
        # NaN is the dangerous one, because it does not fail — it silently changes the answer.
        # `Decimal("NaN") < 0` is False, so it survives the check below, and then every comparison
        # against it is False too: a NaN proximity limit puts nothing in range and every number is
        # retained with the reason "no line is near enough", which sends a reviewer looking for
        # dimension lines that are there. A NaN ambiguity margin is worse: no pair of candidates is
        # ever close enough to refuse, so a coin toss is returned as an association.
        raise ValueError(
            f"{name} must be a finite number. A NaN or infinite value does not widen or narrow the "
            "test, it removes it — every comparison answers the same way, and the result looks "
            "like a decision rather than the absence of one."
        )
    if value < 0:
        raise ValueError(f"{name} cannot be negative")


def _check_inputs(texts: tuple[DimensionText, ...], lines: tuple[DimensionExtent, ...]) -> None:
    if not isinstance(texts, tuple) or any(not isinstance(text, DimensionText) for text in texts):
        raise TypeError("texts must be a tuple of DimensionText values")
    if not isinstance(lines, tuple) or any(not isinstance(line, DimensionExtent) for line in lines):
        raise TypeError("lines must be a tuple of DimensionExtent values")
    ids = [text.observation_id for text in texts]
    if len(ids) != len(set(ids)):
        raise ValueError(
            "two texts share an observation id. The result would then be unable to say which of "
            "them was attached and which was retained, and the retained one would be invisible."
        )


def _require_same_page(
    texts: tuple[DimensionText, ...], lines: tuple[DimensionExtent, ...]
) -> None:
    planes = {(text.extent.document_version_id, text.extent.page) for text in texts}
    planes |= {(line.document_version_id, line.page) for line in lines}
    if len(planes) > 1:
        raise PolygonSpaceMismatchError(
            "the texts and the dimension lines must all be on the same page of the same document "
            "version. Stored coordinates are normalised per page, so a distance measured between "
            "two sheets is arithmetic on unrelated numbers, not a wrong answer."
        )
