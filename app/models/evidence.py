"""Immutable persistence models for candidates, canonical facts and evidence artifacts.

Candidate and canonical observations are deliberately separate tables. Exact measurements
are stored as normalized rational pairs, while provenance is represented by two relational
tables so candidate support cannot be confused with a non-candidate corroboration lane.

Source: backend proposal section 10.1, ``AGENTS.md`` sections 2.3 and 2.7,
``DESIGN.md`` section 3.14, and issue #195.
Verification: ``tests/db/test_evidence_models.py``.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from enum import Enum, StrEnum
from math import gcd
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    event,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Immutable, TimestampedUUID
from evidence.canonical import Authority, CorroborationLane
from rules.semantic_types import DocumentRole, SemanticType
from units.measurement import Unit
from verdict.operands import EvidenceStatus


class EvidenceCandidateRole(StrEnum):
    """How one extraction candidate relates to a canonical observation."""

    PRIMARY = "primary"
    CORROBORATING = "corroborating"
    CONFLICTING = "conflicting"


class EvidenceArtifactKind(StrEnum):
    """Immutable reviewer-facing artifacts retained by the evidence plane."""

    CROP = "crop"
    RENDER = "render"


def _sql_values(enum_type: type[Enum]) -> str:
    return ", ".join(f"'{member.value}'" for member in enum_type)


UNIT_VALUES = _sql_values(Unit)
SEMANTIC_TYPE_VALUES = _sql_values(SemanticType)
DOCUMENT_ROLE_VALUES = _sql_values(DocumentRole)
EVIDENCE_STATUS_VALUES = _sql_values(EvidenceStatus)
AUTHORITY_VALUES = _sql_values(Authority)
CORROBORATION_LANE_VALUES = _sql_values(CorroborationLane)
CANDIDATE_ROLE_VALUES = _sql_values(EvidenceCandidateRole)
ARTIFACT_KIND_VALUES = _sql_values(EvidenceArtifactKind)
SHA256_PATTERN = "^[0-9a-f]{64}$"

EXACT_OPTIONAL_VALUE = """(
    value_numerator IS NULL AND value_denominator IS NULL AND unit IS NULL
) OR (
    value_numerator IS NOT NULL AND value_denominator IS NOT NULL AND unit IS NOT NULL
)"""


class ObservationCandidate(Base, TimestampedUUID, Immutable):
    """Exactly what one extraction run reported, without evidence authority."""

    __tablename__ = "observation_candidates"

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    raw_text: Mapped[str]
    value_numerator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    value_denominator: Mapped[int | None] = mapped_column(BigInteger, default=None)
    unit: Mapped[str | None] = mapped_column(String(32), default=None)
    unit_guess: Mapped[str | None] = mapped_column(String(32), default=None)
    semantic_guess: Mapped[str | None] = mapped_column(String(100), default=None)
    polygon: Mapped[list[list[int]]] = mapped_column(JSONB)
    coordinate_space: Mapped[str] = mapped_column(String(32), default="image")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(), default=None)
    ambiguity_flags: Mapped[list[str]] = mapped_column(JSONB)

    # **What a corroboration lane made of this reading, if any lane applied (#528).**
    #
    # On the candidate rather than on a canonical observation, because that is where the fact
    # belongs: the dual-unit lane compares two readings *inside one token* — `984 [38 3/4]` — and
    # says whether the drawing agrees with itself. That is a statement about a raw reading and needs
    # no meaning attached to it. `evidence_corroboration_lanes` is the other thing, a lane that
    # qualified an observation which already has a semantic type, and it hangs off that observation.
    #
    # Both are null when no lane applied, which is every candidate whose token states one reading.
    corroboration_status: Mapped[str | None] = mapped_column(String(32), default=None)
    corroboration_lane: Mapped[str | None] = mapped_column(String(32), default=None)

    # **Who wrote the annotation this was read from, when it was read from one (#543).**
    #
    # Null for every number read off a drawing, which is most of them: a dimension has no author.
    # It is set by the markup route, where the file names one — `/T` on a `/FreeText` annotation —
    # and where it matters, because a reviewer's correction of a vendor's dimension outranks it
    # partly by virtue of who wrote it.
    #
    # Which *route* read a candidate is not here. That is the run's job: `open_extraction_run` keys a
    # run on extractor, version and configuration, and each route opens its own. A column here would
    # be a second answer to the same question, free to disagree with the first.
    source_author: Mapped[str | None] = mapped_column(String(200), default=None)

    __table_args__ = (
        CheckConstraint(EXACT_OPTIONAL_VALUE, name="observation_candidate_exact_value"),
        CheckConstraint(
            f"corroboration_status IS NULL OR corroboration_status IN ({EVIDENCE_STATUS_VALUES})",
            name="candidate_corroboration_status",
        ),
        CheckConstraint(
            f"corroboration_lane IS NULL OR corroboration_lane IN ({CORROBORATION_LANE_VALUES})",
            name="candidate_corroboration_lane",
        ),
        # Empty is not a name: a `/T` present but blank is a tool filling in a key, not a person,
        # and storing it would make "nobody said" and "somebody said nothing" the same row.
        #
        # The regex rather than `btrim`, which strips spaces and nothing else — 0035 fixed exactly
        # that in `audit_events`, where a tab-only actor had passed since 0023.
        CheckConstraint(
            "source_author IS NULL OR source_author !~ '^[[:space:]]*$'",
            name="candidate_source_author_not_blank",
        ),
        # A lane without a finding, or a finding from no lane, is a row nobody can interpret. They
        # are written together by `record_candidates` or not at all, and the database says so.
        CheckConstraint(
            "(corroboration_status IS NULL AND corroboration_lane IS NULL) OR "
            "(corroboration_status IS NOT NULL AND corroboration_lane IS NOT NULL)",
            name="candidate_corroboration_paired",
        ),
        CheckConstraint(
            "value_denominator IS NULL OR value_denominator > 0",
            name="observation_candidate_denominator",
        ),
        CheckConstraint(
            f"unit IS NULL OR unit IN ({UNIT_VALUES})", name="observation_candidate_unit"
        ),
        CheckConstraint(
            f"unit_guess IS NULL OR unit_guess IN ({UNIT_VALUES})",
            name="observation_candidate_unit_guess",
        ),
        CheckConstraint(
            f"semantic_guess IS NULL OR semantic_guess IN ({SEMANTIC_TYPE_VALUES})",
            name="observation_candidate_semantic_guess",
        ),
        CheckConstraint("coordinate_space = 'image'", name="observation_candidate_space"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="observation_candidate_confidence",
        ),
    )


class CanonicalObservation(Base, TimestampedUUID, Immutable):
    """One normalized attributable fact, separate from every extractor candidate."""

    __tablename__ = "canonical_observations"

    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    document_role: Mapped[str] = mapped_column(String(32))
    polygon: Mapped[list[list[str]]] = mapped_column(JSONB)
    coordinate_space: Mapped[str] = mapped_column(String(32), default="stored")
    semantic_type: Mapped[str] = mapped_column(String(100))
    value_numerator: Mapped[int] = mapped_column(BigInteger)
    value_denominator: Mapped[int] = mapped_column(BigInteger)
    unit: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    authority: Mapped[str] = mapped_column(String(32))
    evidence_crop_uri: Mapped[str | None] = mapped_column(String(1000), default=None)

    __table_args__ = (
        CheckConstraint("value_denominator > 0", name="canonical_observation_denominator"),
        CheckConstraint(f"unit IN ({UNIT_VALUES})", name="canonical_observation_unit"),
        CheckConstraint(
            f"document_role IN ({DOCUMENT_ROLE_VALUES})", name="canonical_observation_role"
        ),
        CheckConstraint(
            f"semantic_type IN ({SEMANTIC_TYPE_VALUES})", name="canonical_observation_semantic_type"
        ),
        CheckConstraint(
            f"status IN ({EVIDENCE_STATUS_VALUES})", name="canonical_observation_status"
        ),
        CheckConstraint(
            f"authority IN ({AUTHORITY_VALUES})", name="canonical_observation_authority"
        ),
        CheckConstraint("coordinate_space = 'stored'", name="canonical_observation_space"),
    )


class EvidenceSupportingCandidate(Base, TimestampedUUID, Immutable):
    """One candidate supporting or conflicting with a canonical observation."""

    __tablename__ = "evidence_supporting_candidates"

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), index=True
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )
    role: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(f"role IN ({CANDIDATE_ROLE_VALUES})", name="evidence_candidate_role"),
        UniqueConstraint("canonical_observation_id", "candidate_id"),
    )


class EvidenceCorroborationLane(Base, TimestampedUUID, Immutable):
    """One typed corroboration route that is not itself an extraction candidate."""

    __tablename__ = "evidence_corroboration_lanes"

    canonical_observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), index=True
    )
    lane: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(
            f"lane IN ({CORROBORATION_LANE_VALUES})", name="evidence_corroboration_lane"
        ),
        UniqueConstraint("canonical_observation_id", "lane"),
    )


class EvidenceArtifact(Base, TimestampedUUID, Immutable):
    """A content-addressed crop or render reference; artifact bytes live in storage."""

    __tablename__ = "evidence_artifacts"

    candidate_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), default=None
    )
    canonical_observation_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("canonical_observations.id", ondelete="RESTRICT"), default=None
    )
    document_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("document_versions.id", ondelete="RESTRICT"), index=True
    )
    page_id: Mapped[UUID] = mapped_column(ForeignKey("pages.id", ondelete="RESTRICT"))
    kind: Mapped[str] = mapped_column(String(32))
    storage_key: Mapped[str] = mapped_column(String(1000))
    sha256: Mapped[str] = mapped_column(String(64))
    media_type: Mapped[str] = mapped_column(String(200))
    coordinate_space: Mapped[str] = mapped_column(String(32))

    __table_args__ = (
        CheckConstraint(
            "(candidate_id IS NOT NULL AND canonical_observation_id IS NULL) OR "
            "(candidate_id IS NULL AND canonical_observation_id IS NOT NULL)",
            name="evidence_artifact_owner",
        ),
        CheckConstraint(f"kind IN ({ARTIFACT_KIND_VALUES})", name="evidence_artifact_kind"),
        CheckConstraint("storage_key <> ''", name="evidence_artifact_storage_key"),
        CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="evidence_artifact_sha256"),
        CheckConstraint("media_type <> ''", name="evidence_artifact_media_type"),
        CheckConstraint("coordinate_space IN ('image', 'stored')", name="evidence_artifact_space"),
        UniqueConstraint("storage_key", "sha256"),
    )

    def content_matches(self, content: bytes) -> bool:
        """Return whether retrieved bytes match the immutable persisted digest."""

        if not isinstance(content, bytes):
            raise TypeError("content must be bytes")
        return hashlib.sha256(content).hexdigest() == self.sha256


def _require_normalized_rational(numerator: int | None, denominator: int | None) -> None:
    if numerator is None and denominator is None:
        return
    if numerator is None or denominator is None or denominator <= 0:
        return  # Database constraints provide the authoritative completeness check.
    if gcd(numerator, denominator) != 1:
        raise ValueError("exact values must be stored in normalized Fraction form")


@event.listens_for(ObservationCandidate, "before_insert")
def _candidate_fraction_is_normalized(
    mapper: object, connection: Connection, target: ObservationCandidate
) -> None:
    """Reject a second spelling of the same exact candidate value."""

    del mapper, connection
    _require_normalized_rational(target.value_numerator, target.value_denominator)


@event.listens_for(CanonicalObservation, "before_insert")
def _canonical_fraction_is_normalized(
    mapper: object, connection: Connection, target: CanonicalObservation
) -> None:
    """Reject a second spelling of the same exact canonical value."""

    del mapper, connection
    _require_normalized_rational(target.value_numerator, target.value_denominator)


def line_key(start_x: object, start_y: object, end_x: object, end_y: object) -> str:
    """One dimension line's endpoints as a single string, for looking a line up by where it is.

    There is no `dimension_lines` table and no line id, deliberately — `ObservationAssociation`
    explains why. So a caller that knows something *about* a line, such as which chain the detector
    put it in, has only its coordinates to name it by. This is that name, built from the same strings
    the row stores, so the two cannot drift into different spellings of the same line.

    Here rather than beside the code that writes a row, because the control plane reads these keys
    too and `tests/api/test_no_heavy_work.py` refuses `app/api/` any path to `extraction/` — which
    `app/evidence/record.py` legitimately has.
    """
    return f"{start_x},{start_y},{end_x},{end_y}"


class ObservationAssociation(Base, TimestampedUUID, Immutable):
    """Which dimension line one reading annotates — or why that could not be decided.

    `984` printed on a sheet is not evidence of anything until it is attached to the line it
    annotates. `extraction/geometry/text_association.py` makes that attachment and has never had
    anywhere to put its answer; this is that place.

    **A refusal is a row, not an absence.** That module is explicit that *"refusing is the
    deliverable, not the fallback"*: two lines equally close to one number is the ordinary case on a
    dimensioned elevation, and an unattached number is the list a reviewer has to look at. A schema
    that could only record successes would turn "we could not tell" into silence, which reads
    downstream as "this drawing has no dimensions".

    **The line is inlined, and that is deliberate.** There is no `dimension_lines` table and these
    endpoints are not an identity. Deciding which vector primitives *are* dimension lines is a
    detector (#179), correct only against this vendor's real CAD output, and `associate` takes a
    `DimensionExtent` rather than that detector's type precisely so it does not have to wait for it.
    A row here says "this reading was attached to the segment running from A to B", which is a
    measurement, not a claim about what that segment is.

    Stored coordinates as text, for the reason `Page.media_box` gives: a JSON float loses the
    exactness the units layer exists to keep, and these numbers decide which line a dimension belongs
    to.
    """

    __tablename__ = "observation_associations"

    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )
    extraction_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("extraction_runs.id", ondelete="RESTRICT"), index=True
    )
    """Which run associated it, and therefore under which thresholds. `open_extraction_run` keys a
    run on its configuration, so a re-association at a different proximity limit is a different run
    rather than the same one quietly meaning something else (#487)."""

    start_x: Mapped[str | None] = mapped_column(String(64), default=None)
    start_y: Mapped[str | None] = mapped_column(String(64), default=None)
    end_x: Mapped[str | None] = mapped_column(String(64), default=None)
    end_y: Mapped[str | None] = mapped_column(String(64), default=None)

    signals: Mapped[list[str]] = mapped_column(JSONB, default=list)
    """Why this pairing, in plain English, one entry per signal that contributed — the module refuses
    to build an association without them, because *"an association nobody can audit is
    indistinguishable from a guess"*. Empty on a refusal, where `refusal_reason` carries the why."""

    refusal_reason: Mapped[str | None] = mapped_column(String(1000), default=None)
    candidate_lines: Mapped[list[list[str]] | None] = mapped_column(JSONB, default=None)
    """What the choice was between, when there was one. A reviewer told only that an association
    could not be made cannot check the geometry; shown the candidates, they can."""

    chain_key: Mapped[str | None] = mapped_column(String(120), default=None)
    """Which chain of end-to-end dimensions this reading's line belongs to, or `None` for a line
    that stands alone.

    **This is the fact that makes an ordered run checkable, and it had nowhere to live.**
    `extraction/geometry/dimension_lines.py` has grouped dimension lines into chains since #588, and
    `workflow/assignment.py` refuses a many-valued field whose readings come from two of them —
    because `CT-WIDTH-001` compares two runs position by position, so values gathered from unrelated
    places would produce a check comparing the second cabinet against the fifth. That refusal could
    never fire in production: the detector ran in the worker and its chains were discarded on the
    next line. This column is where they stop being discarded.

    Page-scoped and deterministic: `page.id`, the axis, and the chain's index in detection order.
    Not an identity — there is still no `dimension_lines` table, and this says only "these readings'
    lines were drawn end to end", which is a statement about where the strokes are."""

    chain_position: Mapped[int | None] = mapped_column(default=None)
    """Its place along that chain, `0` upward, in the order the drawing draws it. Position is what
    the check compares, so this is a fact about the sheet rather than a presentation choice."""

    __table_args__ = (
        # Attached or refused, never both and never neither. A row with endpoints *and* a reason
        # would be two answers to one question, and a row with neither would be a decision nobody
        # can read — the same pairing rule `candidate_corroboration_paired` applies one table over.
        CheckConstraint(
            "(refusal_reason IS NULL"
            " AND start_x IS NOT NULL AND start_y IS NOT NULL"
            " AND end_x IS NOT NULL AND end_y IS NOT NULL"
            " AND jsonb_array_length(signals) > 0)"
            " OR (refusal_reason IS NOT NULL"
            " AND start_x IS NULL AND start_y IS NULL"
            " AND end_x IS NULL AND end_y IS NULL"
            " AND jsonb_array_length(signals) = 0)",
            name="attached_or_refused",
        ),
        # A reason that is blank says nothing, and reads as though something was recorded. The
        # regex rather than `btrim`, which strips spaces and nothing else (0035).
        CheckConstraint(
            "refusal_reason IS NULL OR refusal_reason !~ '^[[:space:]]*$'",
            name="refusal_reason_not_blank",
        ),
        # A chain membership is a key *and* a position, and only on a row that was attached. Half of
        # one is not a weaker answer, it is an unusable one: a position with no chain cannot be
        # ordered against anything, and a chain on a refused row would claim the line it belongs to
        # while the same row says no line was decided.
        CheckConstraint(
            "(chain_key IS NULL AND chain_position IS NULL)"
            " OR (chain_key IS NOT NULL AND chain_position IS NOT NULL"
            " AND refusal_reason IS NULL)",
            name="chain_paired",
        ),
        # One answer per candidate per run. A second row for the same pair would be two associations
        # for one reading, and nothing downstream could tell which was meant.
        UniqueConstraint(
            "candidate_id", "extraction_run_id", name="uq_observation_associations_candidate_run"
        ),
    )


class MeasurementProposal(Base, TimestampedUUID, Immutable):
    """Which reading a model proposed for which rule field, checked and kept.

    **Stored because the reviewer should not have to ask for it.** The proposal used to be computed
    when somebody pressed a button, which meant the form was empty every time it was opened and the
    model was paid for again on every reload. It is computed once, when the drawings are read, and
    a reviewer arriving at the form finds it already filled in.

    **Only accepted proposals are rows.** `workflow/assignment.py` refuses a whole batch if any part
    of it fails, and a refusal leaves the fields empty for the reviewer — which is exactly what an
    absent row already means. Recording the refusal here would be a second way of saying nothing,
    and the reason belongs in the worker's log where somebody diagnosing it will look.

    **A proposal is not a measurement and must never be read as one.** No value is copied here: a
    row names the candidate, and the value comes from the candidate's own exact numerator and
    denominator. The reviewer still saves the form, and saving is what records a measurement — with
    `Provenance.MEASURED` and their name on it, which `rules/parameters.py` keeps a closed set for.
    """

    __tablename__ = "measurement_proposals"

    package_revision_id: Mapped[UUID] = mapped_column(index=True)

    proposal_id: Mapped[UUID] = mapped_column(index=True)
    """Which run of the step produced this row. Append-only means a re-proposal is a second set of
    rows beside the first, and this is what tells them apart — the newest set is the current answer
    and the older ones are what it replaced, which is a record rather than a leak."""

    field_key: Mapped[str] = mapped_column(String(200))
    """`SOURCE:SEMANTIC_TYPE`, the key `required-inputs` uses for the same quantity."""

    position: Mapped[int]
    """Where along the field's ordered run this reading sits, `0` upward; `0` for a field that takes
    one value. The order is a fact about the drawing — `CT-WIDTH-001` compares two runs position by
    position — so it is stored rather than recovered from insertion order."""

    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("observation_candidates.id", ondelete="RESTRICT"), index=True
    )

    model_id: Mapped[str] = mapped_column(String(200))
    prompt_id: Mapped[str] = mapped_column(String(100))
    """Which model and which prompt, so a proposal a reviewer disagrees with can be traced to the
    configuration that produced it rather than to "the AI"."""

    __table_args__ = (
        # One reading per position per field per run. A second row would be two answers to one
        # slot, and nothing downstream could say which was meant.
        UniqueConstraint(
            "proposal_id", "field_key", "position", name="uq_measurement_proposals_slot"
        ),
        # A position is an index into a run, so it starts at zero and counts up.
        CheckConstraint("position >= 0", name="position_not_negative"),
    )
