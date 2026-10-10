"""What the rest of type 1 reads about a vendor row's match with a view of the architect's own file (#1166).

Phase 3 matches each vendor countertop row with the one architect view that draws the same
countertop (`workflow/architect_matching.py`, `workflow/architect_match_records.py`). Phase 4 pairs
the row's dimensions against that view only, Phase 5 shows it. Both read the match only through
these names, so the phases are built side by side.

**No extraction imports, on purpose** (not even for type checking): `app/api/` imports this module,
and `tests/api/test_no_heavy_work.py` keeps the control plane away from `extraction/`. So the two
places that hold extraction types say so structurally: `RowMatch.code` is code's
`extraction.architect.view_matching.CodeMatch` (typed here by the attributes read from it,
`CodeMatchFacts`), and `restrict_to_view` takes and returns a `workflow.architect_pairing.
ArchitectPageInput` (typed by the attributes it uses).

Source: issue #1166 · Contract: `xdoc-contract.md` §A3
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from sqlalchemy.orm import Session

__all__ = [
    "MATCHED_STATUSES",
    "MATCH_STATUSES",
    "CodeMatchFacts",
    "EffectiveMatch",
    "MatchLookup",
    "MatchSource",
    "MatchStatus",
    "MatchedView",
    "RowMatch",
    "compared_with_text",
    "restrict_to_view",
]

MatchStatus = Literal[
    "auto_matched",
    "needs_reviewer",
    "reviewer_confirmed",
    "none_matches",
    "carried_over",
    "not_separated",
    "no_candidates",
]
"""Where one vendor row stands with the architect's file:

* `auto_matched`: code (a printed reference or a clear geometry winner) and both AIs chose the same
  view (decision D1);
* `needs_reviewer`: anything else with views to choose from; nothing is chosen;
* `reviewer_confirmed` / `none_matches`: a person picked a view, or "none of these";
* `carried_over`: a person's pick on the previous revision, carried to the identical vendor item;
* `not_separated`: the view chosen is not clearly apart from its neighbour, so its dimensions were
  not read;
* `no_candidates`: the architect's file has no view at all.
"""

MATCH_STATUSES: tuple[str, ...] = (
    "auto_matched",
    "needs_reviewer",
    "reviewer_confirmed",
    "none_matches",
    "carried_over",
    "not_separated",
    "no_candidates",
)

#: The statuses that name a view the row is compared against (`not_separated` names one too, but
#: nothing in it was read, so it is never compared).
MATCHED_STATUSES: frozenset[str] = frozenset({"auto_matched", "reviewer_confirmed", "carried_over"})

MatchSource = Literal["automatic", "reviewer", "carried"]


@dataclass(frozen=True, slots=True)
class MatchedView:
    """One view of the architect's own file, as a person and the report name it."""

    view_id: UUID
    """The `architect_view_index` row."""
    document_version_id: UUID
    page_id: UUID
    page_number: int
    """1-based, as a person counts pages."""
    view_number: int
    view_tag: str
    title: str | None
    bubble: str | None
    sheet_number: str | None
    scale_note: str | None
    file_name: str
    separated: bool


class CodeMatchFacts(Protocol):
    """What is read of code's `extraction.architect.view_matching.CodeMatch` outside extraction."""

    @property
    def verdict(self) -> str: ...

    @property
    def pick(self) -> str | None: ...

    @property
    def ranked(self) -> Sequence[Any]: ...

    @property
    def reasons(self) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class RowMatch:
    """One vendor row's match, made during the run (`ArchitectMatcher.match`), before it is stored.

    Phase 4 pairs the row against `chosen` only when `status` is `auto_matched` or `carried_over`.
    """

    status: MatchStatus
    source: Literal["automatic", "carried"]
    chosen: MatchedView | None
    code: CodeMatchFacts
    """`extraction.architect.view_matching.CodeMatch`."""
    ai_picks: tuple[Mapping[str, object], ...]
    """`{model_id, answer: view|none|unsure|no_answer, view_id|null, shown_number|null, why,
    invocation_id|null}` per AI, JSON-ready."""
    candidate_json: tuple[Mapping[str, object], ...]
    """The ranked candidates as stored (`architect_view_matches.candidates`)."""
    reasons: tuple[str, ...]
    question_packet: Mapping[str, object] | None
    """The AI question's packet (hash-bound), `None` when no question was asked."""
    details: Mapping[str, object] | None = None
    """Anything else stored with the record (vendor item key, carried-from record)."""


@dataclass(frozen=True, slots=True)
class EffectiveMatch:
    """The match that counts for one vendor row: the latest record of its chain."""

    record_id: UUID
    status: MatchStatus
    source: MatchSource
    matched: MatchedView | None
    """The view the record names (`not_separated` included); `None` otherwise."""
    needs_reviewer: bool
    reasons: tuple[str, ...]
    decided_by: str | None


MatchLookup = Callable[[Session, UUID], EffectiveMatch | None]
"""`(session, row_anchor_candidate_id) -> EffectiveMatch | None` (`effective_architect_match`)."""


class _BoxLike(Protocol):
    @property
    def x0(self) -> Decimal: ...

    @property
    def top(self) -> Decimal: ...

    @property
    def x1(self) -> Decimal: ...

    @property
    def bottom(self) -> Decimal: ...


class _RowLike(Protocol):
    @property
    def view_annotation_index(self) -> int: ...


class _PageLike(Protocol):
    @property
    def rows(self) -> Sequence[_RowLike]: ...

    @property
    def view_boxes(self) -> Sequence[Any]: ...


def restrict_to_view[Page: _PageLike](page: Page, view_number: int, extent: _BoxLike) -> Page:
    """`page` (a `workflow.architect_pairing.ArchitectPageInput`) holding only the rows of one view
    (`view_annotation_index == view_number`), framed by that view's extent (an
    `extraction.geometry.rows.Box`)."""
    return cast(
        Page,
        replace(
            cast(Any, page),
            rows=tuple(row for row in page.rows if row.view_annotation_index == view_number),
            view_boxes=(extent,),
        ),
    )


def compared_with_text(view: MatchedView) -> str:
    """`compared with <file>, page N, view X <title> (sheet S)`: how a result names the view."""
    words = [f"compared with {view.file_name}, page {view.page_number}, view {view.view_number}"]
    if view.title:
        words.append(view.title)
    text = " ".join(words)
    if view.sheet_number:
        text += f" (sheet {view.sheet_number})"
    return text
