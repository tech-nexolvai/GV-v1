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

**An item is a candidate until corroborated.** Items describe what a drawing shows, and `AGENTS.md`
§2.1 keeps reading a drawing separate from deciding. If an item could be created as a fact, the
drawing model becomes a second unguarded route into the verdict — so `corroborated` defaults `False`
and is set by the same discipline that governs observations, never at construction.

**A suggested part is not an item (#852).** The computer may suggest that a box on an elevation is a
cabinet, a filler or a countertop (`PartProposal`). Outside tests, a `drawing_items` row is written
only when a person confirms one (`PartConfirmation`): `workflow/parts.py:confirm_part` is the only
code that writes it, and a guard test fails if another writer appears. The confirmation is the
authority, as `ViewRoleConfirmation` is for a view's role, so an item a person confirmed is still
created uncorroborated.

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

from decimal import Decimal
from enum import StrEnum
from typing import Final
from uuid import UUID

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import (
    DDL,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import Select

from app.db.base import Base, Immutable, TimestampedUUID
from vocabulary.dense_content import DenseContentKind
from vocabulary.part_kinds import PartKind

DENSE_CONTENT_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in DenseContentKind)
PART_KIND_VALUES = ", ".join(f"'{kind.value}'" for kind in PartKind)


#: The one schema every database-wide extension is created in and referenced through.
#:
#: `public` because it is the schema that exists in every PostgreSQL database without being created,
#: including the throwaway ones tests build. What matters is not which schema it is but that it is
#: always the same one and always named: an extension installed by whoever got there first is an
#: extension the next session cannot find (#513).
EXTENSION_SCHEMA: Final = "public"


class ViewRole(StrEnum):
    """The side of a comparison a view belongs to, once established by review."""

    ARCH = "arch"
    SHOP = "shop"


VIEW_ROLE_VALUES = ", ".join(f"'{role.value}'" for role in ViewRole)


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

    role: Mapped[str | None] = mapped_column(String(16), default=None)
    """`arch` or `shop` only after the panel role is established.

    `NULL` means unknown, not "infer it from the upload". Combined sheets carry both roles in one
    source file, so a view with no role must not silently become whichever kind the document was
    registered as.
    """

    __table_args__ = (
        UniqueConstraint("page_id", "tag", name="uq_drawing_views_page_tag"),
        CheckConstraint("tag <> ''", name="drawing_view_tag_present"),
        CheckConstraint(
            f"role IS NULL OR role IN ({VIEW_ROLE_VALUES})",
            name="drawing_view_role",
        ),
    )


class ViewRoleProposal(Base, TimestampedUUID, Immutable):
    """What the sheet suggests one drawing is — the architect's or the vendor's — and why (#710).

    **A suggestion, never the role.** It is read from the label the sheet prints above the drawing
    (`extraction/panels.py`) and never written onto the view: only a person's confirmation
    (`ViewRoleConfirmation`) sets `DrawingView.role`. A drawing with no label above it is recorded
    with no role and the reason, so "nothing suggested" is visible rather than silent.

    Append-only. A re-read that suggests the same thing finds the existing row.
    """

    __tablename__ = "view_role_proposals"

    drawing_view_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_views.id", ondelete="RESTRICT"), index=True
    )

    proposed_role: Mapped[str | None] = mapped_column(String(16), default=None)
    """`arch` or `shop`, or `NULL` when the sheet's labels decide nothing."""

    heading: Mapped[str | None] = mapped_column(String(200), default=None)
    """The label as the sheet prints it, when one was used."""

    reason: Mapped[str] = mapped_column(String(500))

    source: Mapped[str] = mapped_column(String(100))
    """What made the suggestion, versioned — so a later, different reader is a different row."""

    __table_args__ = (
        CheckConstraint(
            f"proposed_role IS NULL OR proposed_role IN ({VIEW_ROLE_VALUES})",
            name="view_role_proposal_role",
        ),
        CheckConstraint("reason !~ '^[[:space:]]*$'", name="view_role_proposal_reason_not_blank"),
        CheckConstraint("source !~ '^[[:space:]]*$'", name="view_role_proposal_source_not_blank"),
    )


class ViewRoleConfirmation(Base, TimestampedUUID, Immutable):
    """A person saying which drawing a view is (#710). The only thing that sets `DrawingView.role`.

    Append-only: a correction is another row, and the view carries the latest. Who confirmed it is
    required, because a role decides which side of every comparison the drawing's items land on.
    """

    __tablename__ = "view_role_confirmations"

    drawing_view_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_views.id", ondelete="RESTRICT"), index=True
    )

    role: Mapped[str] = mapped_column(String(16))

    confirmed_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint(f"role IN ({VIEW_ROLE_VALUES})", name="view_role_confirmation_role"),
        CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'", name="view_role_confirmation_actor_not_blank"
        ),
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
    """From the canonical `CT0xx` vocabulary (ADR-0017), never free text. A confirmed part's is the
    generic type for its kind, `PartKind.item_type`.

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
    A person confirming the part does not set it either: the `PartConfirmation` row records that
    decision, and the item stays a description of the drawing.
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
        # Lane 5 searches OCR variants through pg_trgm. The ordinary B-tree remains useful for exact
        # identifiers; this GIN operator class serves similarity queries without replacing it.
        #
        # **Schema-qualified, and that is the whole of #513.** An extension is a database-wide object
        # living in one schema, but `gin_trgm_ops` is resolved through `search_path` like any other
        # name. Unqualified, this index can only be created by a session whose search path happens to
        # contain the schema the extension landed in — which, with a schema per test, is whichever
        # test ran first and has since dropped it.
        Index(
            "ix_item_identifiers_value_trigram",
            "value_as_printed",
            postgresql_using="gin",
            postgresql_ops={"value_as_printed": f"{EXTENSION_SCHEMA}.gin_trgm_ops"},
        ),
    )


# Production enables pg_trgm through migration 0020. A number of repository tests deliberately
# construct the schema from ORM metadata instead, so that path must establish the same prerequisite
# before SQLAlchemy creates the GIN index. PostgreSQL executes this table hook before its indexes;
# other database dialects ignore it.
event.listen(
    ItemIdentifier.__table__,
    "before_create",
    DDL(  # type: ignore[no-untyped-call]
        f"CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA {EXTENSION_SCHEMA}"
    ).execute_if(dialect="postgresql"),
)


class DenseEmbedding(Base, TimestampedUUID):
    """One model-versioned semantic vector for prose attached to a drawing item.

    The source text remains evidence-owned; its hash pins the bytes embedded without duplicating
    client drawing text into this table. The unique source/model identity makes a later model change
    a new record rather than silently colliding with an old vector. This table is not an append-only
    audit ledger; the source observations and resulting match candidates carry that responsibility.
    """

    __tablename__ = "dense_embeddings"

    drawing_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )
    content_kind: Mapped[str] = mapped_column(String(32), index=True)
    source_text_hash: Mapped[str] = mapped_column(String(64))
    model_id: Mapped[str] = mapped_column(String(200), index=True)
    model_version: Mapped[str] = mapped_column(String(100), index=True)
    dimensions: Mapped[int]
    embedding: Mapped[list[float]] = mapped_column(VECTOR())

    __table_args__ = (
        CheckConstraint(
            f"content_kind IN ({DENSE_CONTENT_KIND_VALUES})", name="dense_embedding_content_kind"
        ),
        CheckConstraint(
            "source_text_hash ~ '^[0-9a-f]{64}$'", name="dense_embedding_source_text_hash"
        ),
        CheckConstraint("model_id <> ''", name="dense_embedding_model_id_present"),
        CheckConstraint("model_version <> ''", name="dense_embedding_model_version_present"),
        CheckConstraint("dimensions > 0", name="dense_embedding_dimensions_positive"),
        CheckConstraint(
            "vector_dims(embedding) = dimensions", name="dense_embedding_dimensions_match"
        ),
        UniqueConstraint(
            "drawing_item_id",
            "content_kind",
            "source_text_hash",
            "model_id",
            "model_version",
            name="uq_dense_embeddings_source_model",
        ),
        Index(
            "ix_dense_embeddings_model_version_kind",
            "model_id",
            "model_version",
            "content_kind",
        ),
    )


# Alembic migration 0021 enables the extension in deployed databases. Repository persistence tests
# also construct tables directly from metadata, so that independent bootstrap path must establish
# the same prerequisite before PostgreSQL sees the VECTOR column.
event.listen(
    DenseEmbedding.__table__,
    "before_create",
    DDL(  # type: ignore[no-untyped-call]
        f"CREATE EXTENSION IF NOT EXISTS vector SCHEMA {EXTENSION_SCHEMA}"
    ).execute_if(dialect="postgresql"),
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


# ---------------------------------------------------------------------------
# The drawing's parts: suggested, then confirmed (#852)
# ---------------------------------------------------------------------------


class PartDecision(StrEnum):
    """What a person said about one suggested part."""

    CONFIRMED = "confirmed"
    """It is a part, of the kind and with the code the person gave."""

    WITHDRAWN = "withdrawn"
    """It is not a part — or no longer is, when this replaces a confirmation."""


PART_DECISION_VALUES = ", ".join(f"'{decision.value}'" for decision in PartDecision)


class PartProposal(Base, TimestampedUUID, Immutable):
    """What the computer suggests one part of a drawing is: a cabinet, a filler or a countertop.

    **A suggestion, never a part.** Writing one creates no `drawing_items` row; only a person's
    `PartConfirmation` does. A suggestion nobody decides on stays a suggestion, and nothing that reads
    `drawing_items` can find it there.

    **A code is kept as printed and never decoded.** A cabinet code names a model, not one cabinet, so
    two parts may carry the same one and nothing here asks for it to be unique. Nor is a width read
    out of its digits: a part's width is a reading linked to it (`ReadingPart`), never its code.

    Append-only: a different suggestion is another row.
    """

    __tablename__ = "part_proposals"

    drawing_view_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_views.id", ondelete="RESTRICT"), index=True
    )
    """The drawing the part is on. One view, as for an item: the same cabinet seen in two drawings is
    two suggestions, and saying they are one physical thing is cross-view identity (B7.3)."""

    kind: Mapped[str] = mapped_column(String(16))
    """`cabinet`, `filler` or `countertop` (`PartKind`)."""

    extent: Mapped[dict[str, object]] = mapped_column(JSONB)
    """The part's outline in stored page space, `{"space": "stored", "points": [[x, y], ...]}` — the
    shape `record_panel_view` gives a view's region, with each coordinate as text so it stays exact.
    The database refuses an extent that does not say it is in stored space.

    A countertop's extent is its own, never the union of the parts beneath it. Taken from them, "the
    run reaches both ends of the countertop" would be true by construction."""

    code_as_printed: Mapped[str | None] = mapped_column(String(200), default=None)
    """The code read on the part, verbatim, or `NULL` when none was read. Most fillers carry nothing,
    and an invented code is worse than none because it matches."""

    code_candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True, default=None
    )
    """The reading the code came from. Required with a code and absent without one: a code nobody
    can trace to a reading is a code nobody can check against the sheet."""

    defining_line: Mapped[dict[str, object] | None] = mapped_column(JSONB, default=None)
    """The dimension line that defined the part, as two points in stored space, or `NULL` when no
    line did. Inlined for the reason `ObservationAssociation` gives: there is no `dimension_lines`
    table, and two endpoints say where strokes are drawn, not what they are."""

    source: Mapped[str] = mapped_column(String(100))
    """What made the suggestion."""

    source_version: Mapped[str] = mapped_column(String(50))
    """Which version of it, so a later, different suggester writes rows of its own rather than
    appearing to agree with this one."""

    reason: Mapped[str] = mapped_column(String(500))
    """Why it was suggested, in plain English, for the person deciding."""

    __table_args__ = (
        CheckConstraint(f"kind IN ({PART_KIND_VALUES})", name="part_proposal_kind"),
        # Containment rather than `extent->>'space' = 'stored'`: a missing key makes that NULL, and a
        # check that evaluates to NULL passes.
        CheckConstraint(
            """extent @> '{"space": "stored"}'::jsonb""", name="part_proposal_extent_stored"
        ),
        CheckConstraint(
            """defining_line IS NULL OR defining_line @> '{"space": "stored"}'::jsonb""",
            name="part_proposal_line_stored",
        ),
        # A code and the reading it came from, together or not at all.
        CheckConstraint(
            "(code_as_printed IS NULL) = (code_candidate_id IS NULL)",
            name="part_proposal_code_has_reading",
        ),
        CheckConstraint(
            "code_as_printed IS NULL OR code_as_printed !~ '^[[:space:]]*$'",
            name="part_proposal_code_not_blank",
        ),
        CheckConstraint("source !~ '^[[:space:]]*$'", name="part_proposal_source_not_blank"),
        CheckConstraint(
            "source_version !~ '^[[:space:]]*$'", name="part_proposal_version_not_blank"
        ),
        CheckConstraint("reason !~ '^[[:space:]]*$'", name="part_proposal_reason_not_blank"),
    )


class PartConfirmation(Base, TimestampedUUID, Immutable):
    """A person's decision on one suggested part: it is a part, or it is not.

    **The only thing that makes an item.** Confirming writes a new `drawing_items` row, with a
    `catalogue` identifier when the person kept a code, and names it in `drawing_item_id`. A
    withdrawal writes no item. `workflow/parts.py:confirm_part` is the only code that does this, and
    a guard in `tests/db/test_drawing_models.py` fails if any other module outside `tests/`
    constructs or inserts into either table.

    **The confirmation is the authority, not the item.** The item is created with `corroborated` left
    `False`. What the person decided is this row, as `ViewRoleConfirmation` is for a view's role;
    confirming that a part exists says nothing about whether any measurement of it is right.

    **Every decision names the one it replaces.** A correction is a new row whose `supersedes_id` is
    the decision before it on the same suggestion. Only a suggestion's first decision may replace
    nothing, and each decision can be replaced once, so at most one decision per suggestion is
    replaced by nothing: the current one. Two people deciding at once cannot both become current.

    **A correction makes a new item.** The earlier item keeps its row, and its identifier with it —
    no role holds `DELETE` on either table — but it is no longer current. An item is current only
    while the confirmation that made it is.
    """

    __tablename__ = "part_confirmations"

    part_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("part_proposals.id", ondelete="RESTRICT"), index=True
    )

    supersedes_id: Mapped[UUID | None] = mapped_column(default=None)
    """The decision this one replaces, or `NULL` for the first decision on the suggestion."""

    decision: Mapped[str] = mapped_column(String(16))
    """`confirmed` or `withdrawn` (`PartDecision`)."""

    kind: Mapped[str | None] = mapped_column(String(16), default=None)
    """The kind the person confirmed, which may differ from the suggestion's. `NULL` on a
    withdrawal, which confirms nothing."""

    code_as_printed: Mapped[str | None] = mapped_column(String(200), default=None)
    """The code the person kept, verbatim, or `NULL` for none. The item carries it as a `catalogue`
    identifier, because a code names a model and two parts may share one."""

    drawing_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), default=None
    )
    """The item this confirmation made. Every confirmation makes its own, so no item is named by
    two."""

    confirmed_by: Mapped[str] = mapped_column(String(200))
    """Who decided, on a withdrawal too: whether a part exists is a person's call, so every call
    names its person."""

    __table_args__ = (
        CheckConstraint(f"decision IN ({PART_DECISION_VALUES})", name="part_confirmation_decision"),
        CheckConstraint(
            f"kind IS NULL OR kind IN ({PART_KIND_VALUES})", name="part_confirmation_kind"
        ),
        # A confirmation names its kind and the item it made. A withdrawal names neither, nor a code:
        # a row carrying both answers would be a decision nobody could read.
        CheckConstraint(
            "(decision = 'confirmed' AND kind IS NOT NULL AND drawing_item_id IS NOT NULL)"
            " OR (decision = 'withdrawn' AND kind IS NULL AND code_as_printed IS NULL"
            " AND drawing_item_id IS NULL)",
            name="part_confirmation_decision_shape",
        ),
        CheckConstraint(
            "code_as_printed IS NULL OR code_as_printed !~ '^[[:space:]]*$'",
            name="part_confirmation_code_not_blank",
        ),
        CheckConstraint(
            "confirmed_by !~ '^[[:space:]]*$'", name="part_confirmation_actor_not_blank"
        ),
        UniqueConstraint("drawing_item_id", name="uq_part_confirmations_drawing_item_id"),
        # What the self-reference below points at, so a decision can only replace one on the same
        # suggestion. With `supersedes_id` alone, a correction could end another part's history.
        UniqueConstraint("id", "part_proposal_id", name="uq_part_confirmations_id_proposal"),
        ForeignKeyConstraint(
            ["supersedes_id", "part_proposal_id"],
            ["part_confirmations.id", "part_confirmations.part_proposal_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("supersedes_id", name="uq_part_confirmations_supersedes_id"),
        Index(
            "ix_part_confirmations_first_decision",
            "part_proposal_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )


class CountertopRun(Base, TimestampedUUID, Immutable):
    """One member of a confirmed countertop run: a part beneath a countertop, and its place.

    **A person always confirms which parts sit under a countertop; the computer only suggests.** So
    every row names who confirmed the run, and the countertop and every member are `drawing_items`
    rows — parts a person confirmed, never suggestions.

    **One row per member, grouped by `run_id`**, as `measurement_proposals` groups one proposal's rows
    by `proposal_id`. A list of ids in one column could not carry a foreign key, and a member that is
    not a confirmed part is what this table has to refuse. The run's own columns — the countertop,
    what proposed it, the edge tolerance, who confirmed it — repeat on each member's row.

    **Order is a column.** `CAB-FILLER-001` compares two runs position by position, so a member's
    place is an integer the database keeps unique within its run, never recovered from insertion
    order.

    Append-only: a correction is a new run under a new `run_id`.

    **Every row belongs to a person's decision (#893).** `run_id` and the countertop together name a
    `countertop_run_decisions` row that confirmed this run, so a member row with no decision behind
    it, or under a decision about another countertop, is refused by the database. Which run is read
    is that table's to say: only the run its countertop's current decision confirmed.
    """

    __tablename__ = "countertop_runs"

    run_id: Mapped[UUID] = mapped_column(index=True)
    """Which run the row belongs to. The rows of one run share it."""

    countertop_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )
    """The countertop part. Its extent is the one confirmed for it, never the union of its members."""

    position: Mapped[int]
    """The member's place along the run, `0` upward, in the order the drawing draws it."""

    member_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )

    signal: Mapped[str] = mapped_column(String(500))
    """Why this member was proposed for the run, in plain English — the sentence a reviewer checks
    against the drawing."""

    proposal_source: Mapped[str] = mapped_column(String(100))
    """What proposed the run, and its version."""

    edge_tolerance: Mapped[Decimal] = mapped_column(Numeric())
    """The edge tolerance the proposal used, in stored units: the normalised `0..1` page space that
    `extraction/model/assembly.py` takes it in. Exact, and refused when negative, infinite or NaN,
    which that resolver refuses too — such a tolerance does not loosen its checks, it removes them."""

    confirmed_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        CheckConstraint("position >= 0", name="countertop_run_position_not_negative"),
        CheckConstraint(
            "member_item_id <> countertop_item_id", name="countertop_run_member_not_countertop"
        ),
        # PostgreSQL orders NaN above every number, Infinity included, so `>= 0` alone admits NaN
        # and Infinity; the upper bound refuses both.
        CheckConstraint(
            "edge_tolerance >= 0 AND edge_tolerance < 'Infinity'::numeric",
            name="countertop_run_tolerance_finite",
        ),
        CheckConstraint("signal !~ '^[[:space:]]*$'", name="countertop_run_signal_not_blank"),
        CheckConstraint(
            "proposal_source !~ '^[[:space:]]*$'", name="countertop_run_source_not_blank"
        ),
        CheckConstraint("confirmed_by !~ '^[[:space:]]*$'", name="countertop_run_actor_not_blank"),
        UniqueConstraint("run_id", "position", name="uq_countertop_runs_slot"),
        UniqueConstraint("run_id", "member_item_id", name="uq_countertop_runs_member"),
        # The decision that confirmed the run, about the same countertop (#893).
        ForeignKeyConstraint(
            ["run_id", "countertop_item_id"],
            ["countertop_run_decisions.run_id", "countertop_run_decisions.countertop_item_id"],
            ondelete="RESTRICT",
        ),
    )


class CountertopRunDecision(Base, TimestampedUUID, Immutable):
    """A person's decision on the run beneath one confirmed countertop: this run, or none (#893).

    **The computer only suggests a run; this is the record that a person decided one.** Confirming
    names the `run_id` whose `countertop_runs` rows hold the members, in order; withdrawing names
    none, and says the suggested or earlier run is not the one beneath this countertop.

    **One current decision per countertop, held by the database**, exactly as `PartConfirmation`
    holds one per suggestion. A correction or a withdrawal names the decision it replaces in
    `supersedes_id`; only a countertop's first decision may replace nothing, and each decision can be
    replaced once and only by one about the same countertop. So at most one decision per countertop
    is replaced by nothing, and only the run that decision confirmed is read. Two people deciding at
    once cannot both become current.

    Keyed by the countertop's item, not its suggestion: a person correcting the countertop part makes
    a new item, and a run confirmed beneath the old one is not carried over to it unseen.
    """

    __tablename__ = "countertop_run_decisions"

    countertop_item_id: Mapped[UUID] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True
    )
    """The countertop part the decision is about."""

    supersedes_id: Mapped[UUID | None] = mapped_column(default=None)
    """The decision this one replaces, or `NULL` for the countertop's first decision."""

    decision: Mapped[str] = mapped_column(String(16))
    """`confirmed` or `withdrawn` (`PartDecision`)."""

    run_id: Mapped[UUID | None] = mapped_column(default=None)
    """The run a confirmation confirmed, whose members are the `countertop_runs` rows sharing it.
    `NULL` on a withdrawal, which confirms no run."""

    confirmed_by: Mapped[str] = mapped_column(String(200))
    """Who decided, on a withdrawal too."""

    wall_config: Mapped[str | None] = mapped_column(String(32), default=None)
    """The human-chosen CT-WIDTH-001 layout for this confirmation; null on withdrawals and
    older confirmations. A replacement never inherits it implicitly."""

    __table_args__ = (
        CheckConstraint(f"decision IN ({PART_DECISION_VALUES})", name="run_decision_value"),
        # A confirmation names its run; a withdrawal names none.
        CheckConstraint(
            "(decision = 'confirmed') = (run_id IS NOT NULL)",
            name="run_decision_shape",
        ),
        CheckConstraint("confirmed_by !~ '^[[:space:]]*$'", name="run_decision_actor_not_blank"),
        CheckConstraint(
            "wall_config IS NULL OR (decision = 'confirmed' AND "
            "wall_config IN "
            "('back_left_right', 'back_and_left', 'back_and_right', 'back_only', 'island'))",
            name="countertop_run_wall_config",
        ),
        UniqueConstraint("run_id", name="uq_countertop_run_decisions_run_id"),
        # What `countertop_runs` points at, so a member row can only belong to a run confirmed for
        # its own countertop.
        UniqueConstraint(
            "run_id", "countertop_item_id", name="uq_countertop_run_decisions_run_countertop"
        ),
        # What the self-reference below points at, so a decision can only replace one about the same
        # countertop.
        UniqueConstraint(
            "id", "countertop_item_id", name="uq_countertop_run_decisions_id_countertop"
        ),
        ForeignKeyConstraint(
            ["supersedes_id", "countertop_item_id"],
            ["countertop_run_decisions.id", "countertop_run_decisions.countertop_item_id"],
            ondelete="RESTRICT",
            # Named here: the conventional name runs past PostgreSQL's 63 characters.
            name="fk_countertop_run_decisions_supersedes_id",
        ),
        UniqueConstraint("supersedes_id", name="uq_countertop_run_decisions_supersedes_id"),
        Index(
            "ix_countertop_run_decisions_first_decision",
            "countertop_item_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )


class ReadingPart(Base, TimestampedUUID, Immutable):
    """Which confirmed part one reading measures, why, and who said so.

    A width means nothing until it is known which cabinet it is the width of. This links one
    canonical observation to a `drawing_items` row — so only to a part a person confirmed, never to a
    suggestion — with the signal that tied them and the person who confirmed the link.

    **At most one live link per reading, held by the database.** A correction is a new row naming the
    link it replaces in `supersedes_id`, and a row naming no part withdraws the link it replaces.
    Only a reading's first link may replace nothing, and each link can be replaced once and only by a
    link for the same reading, so at most one row per reading is replaced by nothing. That row is the
    live link when it names a part.
    """

    __tablename__ = "reading_parts"

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), index=True
    )

    drawing_item_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("drawing_items.id", ondelete="RESTRICT"), index=True, default=None
    )
    """The part the reading measures, or `NULL` on a row that withdraws the link it replaces."""

    supersedes_id: Mapped[UUID | None] = mapped_column(default=None)
    """The link this one replaces, or `NULL` for the reading's first link."""

    signal: Mapped[str] = mapped_column(String(500))
    """Why the reading belongs to the part, in plain English — or, on a withdrawal, why it no longer
    does."""

    confirmed_by: Mapped[str] = mapped_column(String(200))

    __table_args__ = (
        # A withdrawal withdraws something. A first row naming no part would record a link that
        # never existed being taken away.
        CheckConstraint(
            "drawing_item_id IS NOT NULL OR supersedes_id IS NOT NULL",
            name="reading_part_withdraws_a_link",
        ),
        CheckConstraint("signal !~ '^[[:space:]]*$'", name="reading_part_signal_not_blank"),
        CheckConstraint("confirmed_by !~ '^[[:space:]]*$'", name="reading_part_actor_not_blank"),
        # What the self-reference below points at, so a link can only replace one for the same
        # reading. With `supersedes_id` alone, relinking one reading could end another's live link.
        UniqueConstraint("id", "canonical_observation_id", name="uq_reading_parts_id_observation"),
        ForeignKeyConstraint(
            ["supersedes_id", "canonical_observation_id"],
            ["reading_parts.id", "reading_parts.canonical_observation_id"],
            ondelete="RESTRICT",
        ),
        UniqueConstraint("supersedes_id", name="uq_reading_parts_supersedes_id"),
        Index(
            "ix_reading_parts_first_link",
            "canonical_observation_id",
            unique=True,
            postgresql_where=text("supersedes_id IS NULL"),
        ),
    )


#: What a stored digest looks like: lowercase SHA-256 hex, as every other stored artifact's.
SHA256_PATTERN: Final = "^[0-9a-f]{64}$"


class PartPicture(Base, TimestampedUUID, Immutable):
    """A picture of one suggested part, cut from the vendor's drawing for a person to look at (#897).

    So a person can decide what a suggestion is without opening the PDF. The worker that suggests
    the parts cuts one per suggestion: the box around the part's outline and a stated margin, at a
    stated resolution, both recorded here.

    **For a person's eyes only.** Nothing reads a value from it: a part's width is a reading linked
    to it (`ReadingPart`), its code is what a person confirmed, and this is how the person sees the
    part they are deciding on.

    **The vendor's drawing alone.** Cut from the page rendered with the reviewer's markup removed, as
    every reader's crop is (#742), so a person confirms the vendor's part and not GV's note about it.
    Where GV's marks are baked into the vendor's drawing itself, the render cannot remove them, and
    `shows_gv_marks` records that the picture shows them (#921).

    **A pointer and a digest, never the bytes.** The image is in the object store under a
    content-addressed key; whoever shows it checks the bytes against `sha256` first. Two suggestions
    with the same box share one stored image, which is why the key is not unique here.

    Not an `evidence_artifacts` row: that needs a reading as its owner, and a part's picture belongs
    to the suggestion. One per suggestion, and append-only: the first picture cut stands. A part a
    person confirms keeps its suggestion's picture.
    """

    __tablename__ = "part_pictures"

    part_proposal_id: Mapped[UUID] = mapped_column(
        ForeignKey("part_proposals.id", ondelete="RESTRICT")
    )
    """The suggestion it is a picture of, a person's own addition included (#882)."""

    storage_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(200))

    margin_pt: Mapped[Decimal] = mapped_column(Numeric())
    """How far past the part's outline the picture reaches on every side, in PDF points."""

    dpi: Mapped[int] = mapped_column()
    """The resolution the vendor's page was rendered at to cut it."""

    shows_gv_marks: Mapped[bool | None] = mapped_column()
    """Whether markup drawn in colour lies in the picture, wholly or in part (#921): GV's own marks
    baked into the vendor's drawing, which the vendor-only render cannot strip.

    Answered once, when the picture is cut, by the agreement gate's own test of a crop
    (`workflow/stages.py:crop_shows_a_gv_mark`, #901) on the picture's own pixels. **`None` is "not
    checked"**, never "no marks": a picture cut before the check existed (migration 0062 adds the
    column and fills in nothing), or one whose page's coloured markup could not be read. The Measure
    page warns only under a picture where this is true."""

    __table_args__ = (
        UniqueConstraint("part_proposal_id", name="uq_part_pictures_part_proposal_id"),
        CheckConstraint("storage_key !~ '^[[:space:]]*$'", name="part_picture_key_not_blank"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="part_picture_sha256"),
        CheckConstraint("media_type !~ '^[[:space:]]*$'", name="part_picture_media_not_blank"),
        # Below infinity excludes NaN too, which PostgreSQL sorts above every number.
        CheckConstraint(
            "margin_pt > 0 AND margin_pt < 'Infinity'::numeric", name="part_picture_margin"
        ),
        CheckConstraint("dpi > 0", name="part_picture_dpi"),
    )


class VendorPagePicture(Base, TimestampedUUID, Immutable):
    """A full vendor-only page image prepared for reviewer placement (#948).

    The content-addressed image is stored outside the database. This row pins its digest and render
    dimensions to the immutable page manifest so browser clicks can use the same stored-space
    transform as extraction. It carries no readings and is never an extraction input.
    """

    __tablename__ = "vendor_page_pictures"

    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    storage_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(200))
    dpi: Mapped[int] = mapped_column()
    width_px: Mapped[int] = mapped_column()
    height_px: Mapped[int] = mapped_column()
    snap_points: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list)
    """Geometry-only detected endpoints with source labels; no dimension text or meaning."""
    snap_tolerance: Mapped[str | None] = mapped_column(String(64), default=None)

    __table_args__ = (
        UniqueConstraint("page_id", name="uq_vendor_page_pictures_page_id"),
        CheckConstraint("dpi > 0", name="vendor_page_picture_dpi_positive"),
        CheckConstraint("width_px > 0 AND height_px > 0", name="vendor_page_picture_size_positive"),
        CheckConstraint(
            "storage_key !~ '^[[:space:]]*$'", name="vendor_page_picture_key_not_blank"
        ),
        CheckConstraint(
            "media_type !~ '^[[:space:]]*$'", name="vendor_page_picture_media_not_blank"
        ),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="vendor_page_picture_sha256"),
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
