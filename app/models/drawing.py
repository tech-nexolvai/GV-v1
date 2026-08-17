"""The drawing model: views, items, the identifiers printed on them, and the alias table.

An *item* is the thing a rule is about — this countertop, that base cabinet, this filler. Every rule
that sums cabinets needs items to sum, so this is the persistence B7 rests on.

Four properties this schema exists to make true, each of which is easy to lose:

**A view is identified by the pair, never the tag.** Sheets reuse `D`, `E`, `F` on every page. A tag
alone identifies nothing, and a schema that let it would silently merge two elevations from different
sheets into one view — after which every item beneath them belongs to the wrong drawing.

**An item may carry no identifier at all.** Plenty of fillers are drawn with nothing printed on them.
Requiring one would force somebody to invent a value, and an invented identifier is worse than an
absent one because it matches.

**An item is a candidate until corroborated.** Items come from reading a drawing, which is the AI's
job, and `AGENTS.md` §2.1 keeps that separate from deciding. If an item could be created as a fact,
the drawing model becomes a second unguarded route into the verdict — so `corroborated` defaults
`False` and is set by the same discipline that governs observations, never at construction.

**An alias is a small rule.** "Cab." meaning "cabinet" is a judgement somebody made, and it changes
what matches what. So it carries who added it and why, and it is versioned alongside the rulebook
rather than edited in place — an alias table that can be mutated is a rulebook nobody is reviewing.

**Duplicate 'unique' identifiers are reported, not forbidden.** Real packages contain them: the same
vendor mark printed on two items, or a mark reused across sheets. A unique constraint would refuse
the drawing rather than the ambiguity, and the drawing is the fact. `duplicate_identifiers` surfaces
them so a reviewer decides.

Source: backend proposal §10.1 · Design: `docs/DESIGN_PLATFORM.md` §3.1 ·
Verification: `tests/db/test_drawing_models.py`
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import Select

from app.db.base import Base, Immutable, TimestampedUUID


class DrawingView(Base, TimestampedUUID):
    """One titled region of a page — an elevation, a plan, a section.

    Identity is `(page_id, tag)`, enforced below. The tag is what is printed on the sheet, and sheets
    reuse the same letters page after page.
    """

    __tablename__ = "drawing_views"

    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"), index=True)

    tag: Mapped[str] = mapped_column(String(50))
    """As printed — `D`, `E`, `F`, `G`. Stored verbatim rather than normalised: what the drawing
    says is the fact, and a reviewer checking a finding is looking at the sheet, not at us."""

    region: Mapped[dict[str, object]] = mapped_column(JSONB)
    """The view's extent on the page, as a polygon. JSONB rather than a geometry column: containment
    is answered in `extraction/` where the geometry library lives, and the database's job here is to
    keep the coordinates, not to reason about them."""

    __table_args__ = (
        UniqueConstraint("page_id", "tag", name="uq_drawing_views_page_tag"),
        CheckConstraint("tag <> ''", name="drawing_view_tag_present"),
    )


class DrawingItem(Base, TimestampedUUID):
    """One thing on a drawing that a rule can be about.

    Belongs to exactly one view. Recognising that the item in elevation D and the item in plan E are
    the same physical cabinet is cross-view identity, which is `B7.3`'s problem and deliberately not
    representable here — a nullable "same as" column would invite it to be guessed.
    """

    __tablename__ = "drawing_items"

    drawing_view_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_views.id", ondelete="RESTRICT"), index=True
    )

    item_type: Mapped[str] = mapped_column(String(100), index=True)
    """From the canonical `CT0xx` vocabulary (ADR-0017), never free text.

    Stored as text rather than a database enum for the same reason `metric_results.metric` is: the
    vocabulary belongs to `rules/semantic_types.py`, and a migration every time it gains a member
    would put schema churn in the path of the deterministic core.
    """

    extent: Mapped[dict[str, object]] = mapped_column(JSONB)
    """The item's polygon, so "which item does this dimension belong to?" is geometric rather than
    heuristic — `B10.4` answers it by containment, and a bounding box would merge neighbours."""

    corroborated: Mapped[bool] = mapped_column(default=False)
    """False until two independent routes agree, exactly as an observation is.

    An item read off a drawing is AI output. If it could be created corroborated, the drawing model
    would be a second route into the verdict that bypasses the evidence gate — the one thing
    `AGENTS.md` §2.1 forbids. Nothing in this module sets it True; promotion is `evidence/`'s job.
    """

    __table_args__ = (
        CheckConstraint("item_type <> ''", name="drawing_item_type_present"),
        Index("ix_drawing_items_view_type", "drawing_view_id", "item_type"),
    )


class ItemIdentifier(Base, TimestampedUUID):
    """An identifier printed on an item. An item may have none, or several.

    Several because a cabinet often carries both a vendor code and a mark, and they disagree often
    enough that keeping only one would lose the disagreement.
    """

    __tablename__ = "item_identifiers"

    drawing_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )

    kind: Mapped[str] = mapped_column(String(50))
    """`vendor_unique`, `mark`, `catalogue`. Explicit, because matching rules differ by kind: a
    catalogue number is shared by every unit of that model, a mark is unique to a drawing."""

    value_as_printed: Mapped[str] = mapped_column(String(200), index=True)
    """Verbatim. Normalising here would destroy the evidence a reviewer checks against — `B9.2`
    handles OCR variants at match time, where the original is still available to show."""

    __table_args__ = (
        CheckConstraint("kind <> ''", name="item_identifier_kind_present"),
        CheckConstraint("value_as_printed <> ''", name="item_identifier_value_present"),
        # Deliberately NOT unique on `value_as_printed`. Real packages reuse marks, and refusing the
        # drawing would be refusing the fact. `duplicate_identifiers` reports them instead.
        Index("ix_item_identifiers_kind_value", "kind", "value_as_printed"),
    )


class Alias(Base, TimestampedUUID, Immutable):
    """A spelling that means a canonical term — "Cab." for "cabinet".

    `Immutable`, and versioned against the rulebook. An alias changes what matches what, which makes
    it a small rule: editing one in place would silently change how every past match should have been
    read, with nothing recording that it happened. A new spelling is a new row.
    """

    __tablename__ = "aliases"

    spelling: Mapped[str] = mapped_column(String(200), index=True)
    canonical_term: Mapped[str] = mapped_column(String(200), index=True)

    added_by: Mapped[str] = mapped_column(String(200))
    """Who decided. An alias with no author is an anonymous rule change."""

    rationale: Mapped[str] = mapped_column(String(1000))
    """Why. "Seen on three Ridgewood packages" is checkable; an unexplained alias is one nobody can
    review, and the whole point of writing them down is that somebody can."""

    rulebook_version: Mapped[str] = mapped_column(String(50), index=True)
    """Which rulebook version this alias belongs to, so a past decision can be replayed with the
    alias table as it stood, not as it stands."""

    __table_args__ = (
        UniqueConstraint(
            "spelling",
            "canonical_term",
            "rulebook_version",
            name="uq_aliases_spelling_term_version",
        ),
        CheckConstraint("spelling <> ''", name="alias_spelling_present"),
        CheckConstraint("canonical_term <> ''", name="alias_canonical_term_present"),
        CheckConstraint("added_by <> ''", name="alias_added_by_present"),
        CheckConstraint("rationale <> ''", name="alias_rationale_present"),
    )


def duplicate_identifiers(kind: str = "vendor_unique") -> Select[tuple[str, int]]:
    """Identifiers of one kind that appear on more than one item.

    A report, not a constraint. `vendor_unique` claims uniqueness and real packages break that claim
    — the same mark printed twice, or reused across sheets. A unique index would refuse the drawing
    rather than the ambiguity, and the drawing is what actually exists; the correct response is to
    show a reviewer and let them decide which item a rule is about.

    Returns a query rather than running one, so the caller owns the session and the transaction.
    """
    return (
        select(ItemIdentifier.value_as_printed, func.count().label("occurrences"))
        .where(ItemIdentifier.kind == kind)
        .group_by(ItemIdentifier.value_as_printed)
        .having(func.count() > 1)
        .order_by(ItemIdentifier.value_as_printed)
    )
