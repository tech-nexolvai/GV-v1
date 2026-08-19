"""How a possible correspondence was proposed: the eight retrieval lanes.

A closed list. `docs/DESIGN_EXTRACTION.md` §8 fixes them, and a candidate records which one put it
forward — an exact identifier match and a dense-vector guess are both proposals, and a reviewer
weighing one should never have to guess which they are looking at.

**Why here and not in `retrieval/`.** Two packages need the word for unrelated reasons: `retrieval/`
produces candidates and names the lane on each, and `app/models/matching.py` persists it and derives a
SQL `CHECK` constraint from the members. This package is the one place both may import — it holds
names, not decisions, and imports nothing itself (`tests/test_verdict_isolation.py` asserts that).

Moving it here is what makes `docs/DESIGN_PLATFORM.md` §2 true rather than aspirational. That table
says `app/models/` must never import `retrieval/`, and it did: `app/models/matching.py` imported `Lane`
from `retrieval.candidate`, which put every module reaching `app.models` — including all of
`app/api/` — one hop from the retrieval package. Nothing about naming a lane requires retrieval, so
the fix is for the name to live where naming things lives. `PageType` and `SemanticType` moved here
first, for the same reason.

`retrieval/candidate.py` imports this one and re-exports the name, so `retrieval.candidate.Lane` keeps
working and there is exactly one definition rather than two that happen to agree. The members and
their values are unchanged by the move — `StrEnum` with `auto()` lowercases the member name — so the
constraint renders the same SQL and **no migration is involved**.

Source: backend proposal §7.3 · Design: `docs/DESIGN_EXTRACTION.md` §8, `DESIGN_PLATFORM.md` §2 ·
Verification: `tests/api/test_no_heavy_work.py`, `tests/retrieval/test_candidate.py`
"""

from __future__ import annotations

from enum import StrEnum, auto


class Lane(StrEnum):
    """The retrieval route that proposed a possible correspondence."""

    EXACT = auto()
    ALIAS = auto()
    METADATA = auto()
    GEOMETRY = auto()
    TRIGRAM = auto()
    LEXICAL = auto()
    DENSE = auto()
    FUSION = auto()
