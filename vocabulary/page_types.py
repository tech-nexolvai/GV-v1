"""What a drawing page *is*: plan, elevation, section, detail, schedule or title block.

A closed list, and deliberately a short one. `docs/DESIGN_EXTRACTION.md` §3.2 fixes these six and
makes `None` — no classification — a real seventh answer rather than a missing value. A countertop
width read off a cabinet elevation is a plausible number attached to the wrong drawing, and no
tolerance check catches that, so a page nobody could classify must not be rounded to the nearest
plausible type.

**Why here and not in `extraction/`.** Three packages need the word for unrelated reasons:
`extraction/` records it on a page, `app/` persists it, and a scope resolver reads it. This package
is the one place all of them may import — it holds names, not decisions, and imports nothing itself
(`tests/test_verdict_isolation.py` asserts that). `SemanticType` moved here for the same reason.

`app/models/document.py` currently carries an identical copy for its database check constraint. The
two are kept in step by a test rather than by hope; converging them is a change to a shipped model
and belongs to whoever owns that migration.

Source: backend proposal §10.1 `pages` · Design: `docs/DESIGN_EXTRACTION.md` §3.2 ·
Verification: `tests/extraction/test_manifest.py`
"""

from __future__ import annotations

from enum import StrEnum


class PageType(StrEnum):
    """The six page classifications. Absence of a classification is `None`, never a member here."""

    PLAN = "plan"
    ELEVATION = "elevation"
    SECTION = "section"
    DETAIL = "detail"
    SCHEDULE = "schedule"
    TITLE = "title"
