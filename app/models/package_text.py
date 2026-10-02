"""A package's own words, grouped into passages a search can point at (#836).

The readers store text one run at a time — `observation_candidates.raw_text` — and a run is often a
single word, or half a dimension, or two words glued together. Nothing could find "the passage that
states the overhang", because no passage existed anywhere. These two tables are where one is kept:
`text_phrases` holds a passage and `text_phrase_members` the runs it was built from, in order.

**A pointer to text, never a source of a number.** A phrase's `content` is the runs' text joined, so
it holds whatever digits the drawing printed, and it is stored so PostgreSQL can index it. It is not
returned by the search: `retrieval/package_text.py` answers with phrase ids and run ids only, and a
value is read from the run's own exact numerator and denominator, through the same gates as any
other reading. "Search may fetch a document, never a number" (`docs/AI_FILLS_THE_FORM_PLAN.md` §4).

**Append-only, like every other record of a reading.** A rebuild is a second set of rows with a new
`build_id`, and the newest set is the current one. The older set is a record of what it replaced.

**Not readable by the verdict role.** `app/db/roles.py` grants `gv_verdict` an allowlist, and neither
table is on it, so a session handed to the verdict service cannot select from them.

Source: issue #836, plan step 3.1 on #798. Verification: `tests/retrieval/test_package_text.py`,
`tests/db/test_roles.py`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final
from uuid import UUID

from sqlalchemy import (
    DDL,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from app.models.drawing import EXTENSION_SCHEMA

__all__ = ["PHRASE_TAG_VALUES", "PhraseTag", "TextPhrase", "TextPhraseMember"]


class PhraseTag(StrEnum):
    """What kind of passage a phrase is, decided from where its runs came from."""

    MARKUP = "markup"
    """The reviewer's own note, read from the markup route. Markup has no side (#802): it is neither
    the architect's reading nor the vendor's."""

    DIMENSION = "dimension"
    """At least one of its runs is attached to a dimension line by an association row."""

    NOTE = "note"
    """Everything else the file itself says: titles, notes, tags, schedules."""


PHRASE_TAG_VALUES: Final = ", ".join(f"'{tag.value}'" for tag in PhraseTag)

_SHA256_PATTERN: Final = "^[0-9a-f]{64}$"


class TextPhrase(Base, TimestampedUUID, Immutable):
    """One passage of one package revision: runs on one line of one page, read by one route."""

    __tablename__ = "text_phrases"

    package_revision_id: Mapped[UUID] = mapped_column(
        ForeignKey("package_revisions.id", ondelete="RESTRICT"), index=True
    )
    """The one revision this phrase belongs to. Every search is scoped by it, so a phrase cannot be
    found from another package however alike the two drawings' words are."""

    build_id: Mapped[UUID] = mapped_column(index=True)
    """Which build wrote this row. The newest build of a revision is its current set of phrases."""

    build_digest: Mapped[str] = mapped_column(String(64))
    """A hash of everything the build decided: the grouping it ran under, and each phrase's tag and
    runs in order. A rebuild that would decide exactly the same writes nothing, so a redelivered
    extraction does not double every phrase."""

    grouping: Mapped[str] = mapped_column(String(200))
    """The grouping rule as the build stated it, gap included, so a phrase can say why its runs were
    joined. The gap is deployment configuration with no default, and a row that did not record it
    would be a passage nobody could reproduce."""

    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"), index=True)
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT")
    )
    """The route that read every run in this phrase. A phrase never mixes routes: the reviewer's
    markup and the vendor's text on one line are two passages, never one."""

    position: Mapped[int]
    """Reading order within the build: page, route, then top to bottom and left to right."""

    tag: Mapped[str] = mapped_column(String(16))
    content: Mapped[str]
    """The runs' text, joined with single spaces. Stored to be indexed, and never returned by the
    search — see the module docstring."""

    __table_args__ = (
        CheckConstraint("position >= 0", name="text_phrase_position_not_negative"),
        CheckConstraint(f"tag IN ({PHRASE_TAG_VALUES})", name="text_phrase_tag"),
        # The regex rather than `btrim`, which strips spaces and nothing else (0035).
        CheckConstraint("content !~ '^[[:space:]]*$'", name="text_phrase_content_not_blank"),
        CheckConstraint("grouping !~ '^[[:space:]]*$'", name="text_phrase_grouping_not_blank"),
        CheckConstraint(f"build_digest ~ '{_SHA256_PATTERN}'", name="text_phrase_build_digest"),
        # One phrase per place in a build's reading order. Two rows at one position would make the
        # order of results depend on the order PostgreSQL happened to return them in.
        UniqueConstraint("build_id", "position", name="uq_text_phrases_build_position"),
        # Words: `simple`, because a drawing's vocabulary is codes, tags and abbreviations, and a
        # language dictionary would stem `LEDs` and drop `A` as a stop word.
        Index(
            "ix_text_phrases_content_words",
            text("to_tsvector('simple'::regconfig, content)"),
            postgresql_using="gin",
        ),
        # Fragments: a run glued to its neighbour (`OVERHANGTYP`) is one word to the parser, and a
        # substring match is the only way to find it. Schema-qualified for the reason
        # `ix_item_identifiers_value_trigram` is (#513).
        Index(
            "ix_text_phrases_content_trigram",
            "content",
            postgresql_using="gin",
            postgresql_ops={"content": f"{EXTENSION_SCHEMA}.gin_trgm_ops"},
        ),
    )


class TextPhraseMember(Base, TimestampedUUID, Immutable):
    """One run of text inside a phrase, and where in the phrase it sits."""

    __tablename__ = "text_phrase_members"

    phrase_id: Mapped[UUID] = mapped_column(
        ForeignKey("text_phrases.id", ondelete="RESTRICT"), index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )
    """The run itself. Its text, its polygon and its exact value — where it has one — stay on the
    candidate, and are read from there rather than copied here."""

    position: Mapped[int]
    """Left to right along the line, `0` upward."""

    __table_args__ = (
        CheckConstraint("position >= 0", name="text_phrase_member_position_not_negative"),
        UniqueConstraint("phrase_id", "position", name="uq_text_phrase_members_slot"),
        UniqueConstraint("phrase_id", "candidate_id", name="uq_text_phrase_members_candidate"),
    )


# Production enables pg_trgm through migrations 0020 and 0034. Repository tests that build the schema
# from metadata need it before the trigram index is created, exactly as `item_identifiers` does.
event.listen(
    TextPhrase.__table__,
    "before_create",
    DDL(  # type: ignore[no-untyped-call]
        f"CREATE EXTENSION IF NOT EXISTS pg_trgm SCHEMA {EXTENSION_SCHEMA}"
    ).execute_if(dialect="postgresql"),
)
