"""A row of N unlabelled pieces, read through the vendor's `X"(N EQ)` chain for the same run (#1086).

**The case.** A vendor sometimes prints a run's widths only once, on a chain of its own: an end
piece, `X"(N EQ)` for N equal cabinets, another end piece, and the run's overall. The row the two
readers chose for the countertop then shows the same run as N pieces with no number at all, so
nothing on it can be read and the page waits for the reviewer.

**The rule (code only, no model).** The two rows are treated as one countertop, and the chain's own
sealed readings are used, only when *every* one of these holds exactly:

- the chosen row has N >= 2 pieces and both readers said "no dimension" for every one of them (and
  for its overall, if it has one);
- exactly one other candidate row (one the readers were shown) has the same two ends, within the
  row finder's own end tolerance (`RowSettings.overall_end_pt`), lies in the same pasted drawing
  as the chosen row (never across drawings), and has an overall;
- on that row, every piece and the overall sealed (both readers' identical text and the drawn-length
  witness, as for any row); exactly one inner piece's agreed text is `X"(K EQ)` with K = N; the
  pieces either side of it are the end pieces.

Anything else, by any margin: nothing changes and the chosen row is kept as read. The rule never
makes a value: every value it hands on was sealed on the chain's own label, and the expansion of
`X"(K EQ)` to X is `labels.expand_label`'s, unchanged. Holds found on either row still hold.

Source: issue #1086 · Verification: `tests/extraction/slot_reader/test_equal_shares.py`
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from extraction.geometry.rows import Box, CountertopRowCandidate
from extraction.slot_reader.labels import Expansion, expand_label
from extraction.slot_reader.seal import LabelState, OwnerOutcome

__all__ = [
    "NO_LABEL",
    "EqualShareChain",
    "equal_share_chain",
    "equal_share_partner",
    "row_frame",
]

#: `seal.owner_outcome`'s code for a piece whose labels are all "no dimension" (or that has none).
NO_LABEL: Final = "no-label"


def row_frame(
    row: CountertopRowCandidate, drawing_boxes: Sequence[Box], *, slack_pt: Decimal
) -> Box | None:
    """The smallest pasted drawing holding the row's line from end to end, as
    `workflow/slot_reader._counter_break_row_hold` finds it; `None` when none does."""
    frames = [
        box
        for box in drawing_boxes
        if box.x0 - slack_pt <= row.x0
        and row.x1 <= box.x1 + slack_pt
        and box.top - slack_pt <= row.y <= box.bottom + slack_pt
    ]
    if not frames:
        return None
    return min(frames, key=lambda box: (box.width * box.height, box.x0, box.top))


def _inside(frame: Box, row: CountertopRowCandidate, slack_pt: Decimal) -> bool:
    lines = [row.y, *(() if row.overall is None else (row.overall.y,))]
    xs = [row.x0, row.x1]
    if row.overall is not None:
        xs.extend((row.overall.x0, row.overall.x1))
    return (
        frame.x0 - slack_pt <= min(xs)
        and max(xs) <= frame.x1 + slack_pt
        and all(frame.top - slack_pt <= y <= frame.bottom + slack_pt for y in lines)
    )


def equal_share_partner(
    chosen: CountertopRowCandidate,
    candidates: Sequence[CountertopRowCandidate],
    drawing_boxes: Sequence[Box],
    *,
    end_tolerance_pt: Decimal,
    frame_slack_pt: Decimal,
) -> CountertopRowCandidate | None:
    """The one other candidate that may hold the chosen row's widths, or `None`.

    Geometry only, decided before anything is read: the same two ends within `end_tolerance_pt`,
    an overall, at least three pieces (an end piece either side of the shares), and the same pasted
    drawing as the chosen row, its chain and overall lines inside it. Two such rows → `None`: which
    one is meant would be a choice.
    """
    if len(chosen.slots) < 2 or chosen.in_architect_view:
        return None
    frame = row_frame(chosen, drawing_boxes, slack_pt=frame_slack_pt)
    if frame is None:
        return None
    matches = [
        other
        for other in candidates
        if other is not chosen
        and other != chosen
        and not other.in_architect_view
        and other.overall is not None
        and len(other.slots) >= 3
        and abs(other.x0 - chosen.x0) <= end_tolerance_pt
        and abs(other.x1 - chosen.x1) <= end_tolerance_pt
        and row_frame(other, drawing_boxes, slack_pt=frame_slack_pt) == frame
        and _inside(frame, other, frame_slack_pt)
    ]
    return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class EqualShareChain:
    """Why the chain may stand for the chosen row: which of its pieces holds the shares, and N."""

    share_slot: int
    shares: int


def _sealed(outcome: OwnerOutcome) -> bool:
    return outcome.state is LabelState.SEALED and outcome.value is not None


def equal_share_chain(
    chosen: Sequence[OwnerOutcome],
    chosen_overall: OwnerOutcome | None,
    chain: Sequence[tuple[OwnerOutcome, str | None]],
    chain_overall: tuple[OwnerOutcome, str | None] | None,
) -> EqualShareChain | None:
    """Whether the chain's readings may stand for the chosen row; `None` changes nothing.

    `chosen` and `chosen_overall` are the chosen row's outcomes as read. `chain` is the partner's
    pieces left to right, each with the text both readers agreed on (`None` if they did not), and
    `chain_overall` its overall; all after the drawn-length witness. Exact: no tolerance, no count
    within one, no unsealed piece.
    """
    count = len(chosen)
    if count < 2:
        return None
    if any(outcome.reason_code != NO_LABEL or outcome.value is not None for outcome in chosen):
        return None
    if chosen_overall is not None and (
        chosen_overall.reason_code != NO_LABEL or chosen_overall.value is not None
    ):
        return None
    if chain_overall is None or not _sealed(chain_overall[0]):
        return None
    if len(chain) < 3 or not all(_sealed(outcome) for outcome, _text in chain):
        return None
    shares: list[tuple[int, int]] = []
    for index, (outcome, text) in enumerate(chain):
        expanded = None if text is None else expand_label(text)
        if expanded is None or expanded.how is not Expansion.EQUAL_SHARES:
            continue
        if (
            outcome.value is None
            or expanded.shares is None
            or expanded.value.exact != outcome.value.exact
        ):
            return None
        shares.append((index, expanded.shares))
    if len(shares) != 1:
        return None
    index, shares_count = shares[0]
    if index in (0, len(chain) - 1) or shares_count != count:
        return None
    return EqualShareChain(share_slot=index, shares=shares_count)
