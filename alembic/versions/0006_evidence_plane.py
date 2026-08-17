"""Candidate, canonical and evidence-artifact persistence (#195).

Revision ID: 0006_evidence_plane
Revises: 0005_run_records
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_evidence_plane"
down_revision: str | None = "0005_run_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNITS = "'mm', 'in'"
DOCUMENT_ROLES = "'ARCH', 'SHOP', 'PRODUCT_SPEC'"
STATUSES = "'RAW_CANDIDATE', 'CORROBORATED', 'HUMAN_CONFIRMED', 'CONFLICTING', 'REJECTED'"
AUTHORITIES = "'AUTHORITATIVE', 'ADVISORY'"
LANES = "'SECOND_READER', 'DUAL_UNIT', 'HUMAN'"
CANDIDATE_ROLES = "'primary', 'corroborating', 'conflicting'"
ARTIFACT_KINDS = "'crop', 'render'"
SEMANTIC_TYPES = (
    "'CT001', 'CT002', 'CT003', 'CT004', 'CT005', 'CT006', 'CT007', 'CT008', "
    "'CT009', 'CT010', 'CT011', 'CT012', 'CT013', 'B.S_THK', 'C.T_OH', "
    "'CAB_SIDE_THK', 'cabinet_width', 'filler_width', 'countertop_overall_width', "
    "'wall_config', 'field_dimension', 'material'"
)
SHA256_PATTERN = "^[0-9a-f]{64}$"


def _identity_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade() -> None:
    """Create the immutable evidence plane and its deferred provenance invariant."""

    op.create_table(
        "observation_candidates",
        *_identity_columns(),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("extraction_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("raw_text", sa.String(), nullable=False),
        sa.Column("value_numerator", sa.BigInteger(), nullable=True),
        sa.Column("value_denominator", sa.BigInteger(), nullable=True),
        sa.Column("unit", sa.String(length=32), nullable=True),
        sa.Column("unit_guess", sa.String(length=32), nullable=True),
        sa.Column("semantic_guess", sa.String(length=100), nullable=True),
        sa.Column("polygon", postgresql.JSONB(), nullable=False),
        sa.Column("coordinate_space", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Numeric(), nullable=True),
        sa.Column("ambiguity_flags", postgresql.JSONB(), nullable=False),
        sa.CheckConstraint(
            "(value_numerator IS NULL AND value_denominator IS NULL AND unit IS NULL) OR "
            "(value_numerator IS NOT NULL AND value_denominator IS NOT NULL AND unit IS NOT NULL)",
            name="observation_candidate_exact_value",
        ),
        sa.CheckConstraint(
            "value_denominator IS NULL OR value_denominator > 0",
            name="observation_candidate_denominator",
        ),
        sa.CheckConstraint(f"unit IS NULL OR unit IN ({UNITS})", name="observation_candidate_unit"),
        sa.CheckConstraint(
            f"unit_guess IS NULL OR unit_guess IN ({UNITS})",
            name="observation_candidate_unit_guess",
        ),
        sa.CheckConstraint(
            f"semantic_guess IS NULL OR semantic_guess IN ({SEMANTIC_TYPES})",
            name="observation_candidate_semantic_guess",
        ),
        sa.CheckConstraint("coordinate_space = 'image'", name="observation_candidate_space"),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="observation_candidate_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["extraction_run_id"], ["extraction_runs.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_observation_candidates_document_version_id",
        "observation_candidates",
        ["document_version_id"],
    )
    op.create_index(
        "ix_observation_candidates_extraction_run_id",
        "observation_candidates",
        ["extraction_run_id"],
    )

    op.create_table(
        "canonical_observations",
        *_identity_columns(),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_role", sa.String(length=32), nullable=False),
        sa.Column("polygon", postgresql.JSONB(), nullable=False),
        sa.Column("coordinate_space", sa.String(length=32), nullable=False),
        sa.Column("semantic_type", sa.String(length=100), nullable=False),
        sa.Column("value_numerator", sa.BigInteger(), nullable=False),
        sa.Column("value_denominator", sa.BigInteger(), nullable=False),
        sa.Column("unit", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("authority", sa.String(length=32), nullable=False),
        sa.Column("evidence_crop_uri", sa.String(length=1000), nullable=True),
        sa.CheckConstraint("value_denominator > 0", name="canonical_observation_denominator"),
        sa.CheckConstraint(f"unit IN ({UNITS})", name="canonical_observation_unit"),
        sa.CheckConstraint(
            f"document_role IN ({DOCUMENT_ROLES})", name="canonical_observation_role"
        ),
        sa.CheckConstraint(
            f"semantic_type IN ({SEMANTIC_TYPES})", name="canonical_observation_semantic_type"
        ),
        sa.CheckConstraint(f"status IN ({STATUSES})", name="canonical_observation_status"),
        sa.CheckConstraint(f"authority IN ({AUTHORITIES})", name="canonical_observation_authority"),
        sa.CheckConstraint("coordinate_space = 'stored'", name="canonical_observation_space"),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_canonical_observations_document_version_id",
        "canonical_observations",
        ["document_version_id"],
    )

    op.create_table(
        "evidence_supporting_candidates",
        *_identity_columns(),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.CheckConstraint(f"role IN ({CANDIDATE_ROLES})", name="evidence_candidate_role"),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"], ["canonical_observations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_observation_id", "candidate_id"),
    )
    op.create_index(
        "ix_evidence_supporting_candidates_canonical_observation_id",
        "evidence_supporting_candidates",
        ["canonical_observation_id"],
    )
    op.create_index(
        "ix_evidence_supporting_candidates_candidate_id",
        "evidence_supporting_candidates",
        ["candidate_id"],
    )

    op.create_table(
        "evidence_corroboration_lanes",
        *_identity_columns(),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("lane", sa.String(length=32), nullable=False),
        sa.CheckConstraint(f"lane IN ({LANES})", name="evidence_corroboration_lane"),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"], ["canonical_observations.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("canonical_observation_id", "lane"),
    )
    op.create_index(
        "ix_evidence_corroboration_lanes_canonical_observation_id",
        "evidence_corroboration_lanes",
        ["canonical_observation_id"],
    )

    op.create_table(
        "evidence_artifacts",
        *_identity_columns(),
        sa.Column("candidate_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("canonical_observation_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("page_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("storage_key", sa.String(length=1000), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("coordinate_space", sa.String(length=32), nullable=False),
        sa.CheckConstraint(
            "(candidate_id IS NOT NULL AND canonical_observation_id IS NULL) OR "
            "(candidate_id IS NULL AND canonical_observation_id IS NOT NULL)",
            name="evidence_artifact_owner",
        ),
        sa.CheckConstraint(f"kind IN ({ARTIFACT_KINDS})", name="evidence_artifact_kind"),
        sa.CheckConstraint("storage_key <> ''", name="evidence_artifact_storage_key"),
        sa.CheckConstraint(f"sha256 ~ '{SHA256_PATTERN}'", name="evidence_artifact_sha256"),
        sa.CheckConstraint("media_type <> ''", name="evidence_artifact_media_type"),
        sa.CheckConstraint(
            "coordinate_space IN ('image', 'stored')", name="evidence_artifact_space"
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"], ["observation_candidates.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["canonical_observation_id"], ["canonical_observations.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"], ["document_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["page_id"], ["pages.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", "sha256"),
    )
    op.create_index(
        "ix_evidence_artifacts_document_version_id",
        "evidence_artifacts",
        ["document_version_id"],
    )

    _create_provenance_constraint()


def _create_provenance_constraint() -> None:
    op.execute("""
        CREATE FUNCTION check_canonical_observation_provenance()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            observation_id uuid;
            observation_status text;
            supporting_count integer;
            conflicting_count integer;
            has_dual_unit boolean;
        BEGIN
            observation_id := CASE
                WHEN TG_TABLE_NAME = 'canonical_observations' THEN NEW.id
                WHEN TG_OP = 'DELETE' THEN OLD.canonical_observation_id
                ELSE NEW.canonical_observation_id
            END;

            SELECT status INTO observation_status
            FROM canonical_observations WHERE id = observation_id;
            IF observation_status IS NULL THEN
                RETURN NULL;
            END IF;

            SELECT
                count(*) FILTER (WHERE role IN ('primary', 'corroborating')),
                count(*) FILTER (WHERE role = 'conflicting')
            INTO supporting_count, conflicting_count
            FROM evidence_supporting_candidates
            WHERE canonical_observation_id = observation_id;

            SELECT EXISTS(
                SELECT 1 FROM evidence_corroboration_lanes
                WHERE canonical_observation_id = observation_id AND lane = 'DUAL_UNIT'
            ) INTO has_dual_unit;

            IF observation_status = 'RAW_CANDIDATE'
               AND NOT (supporting_count >= 1 AND conflicting_count = 0) THEN
                RAISE EXCEPTION 'RAW_CANDIDATE provenance is invalid' USING ERRCODE = '23514';
            ELSIF observation_status = 'CORROBORATED'
               AND NOT ((supporting_count >= 2 OR (supporting_count >= 1 AND has_dual_unit))
                        AND conflicting_count = 0) THEN
                RAISE EXCEPTION 'CORROBORATED provenance is invalid' USING ERRCODE = '23514';
            ELSIF observation_status = 'CONFLICTING'
               AND NOT (supporting_count >= 1 AND conflicting_count >= 1) THEN
                RAISE EXCEPTION 'CONFLICTING provenance is invalid' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        $$
        """)
    for table in (
        "canonical_observations",
        "evidence_supporting_candidates",
        "evidence_corroboration_lanes",
    ):
        op.execute(f"""
            CREATE CONSTRAINT TRIGGER ck_{table}_provenance
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION check_canonical_observation_provenance()
            """)


def downgrade() -> None:
    """Drop provenance enforcement and evidence tables in reverse dependency order."""

    op.execute("DROP FUNCTION check_canonical_observation_provenance() CASCADE")
    op.drop_table("evidence_artifacts")
    op.drop_table("evidence_corroboration_lanes")
    op.drop_table("evidence_supporting_candidates")
    op.drop_table("canonical_observations")
    op.drop_table("observation_candidates")
