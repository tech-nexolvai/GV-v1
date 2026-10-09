"""Name the lane that qualifies the architect's printed value: its drawn-length witness (#1054).

Revision ID: 0077_drawn_length_lane
Revises: 0076_architect_pairing_records

The architect's dimensions on the client's combined sheets are real text, read exactly by code
(#1052), and kept only when the length drawn between their ticks, through the drawing's scale,
agrees. That is one exact reading plus a non-model witness, and it is recorded as the
`DRAWN_LENGTH` corroboration lane so the reason a value was qualified is stored beside it. The
evidence model allows it for the architect's side only (`evidence/canonical.py`, `evidence/gate.py`).

Two things change in the database, both on the canonical observation side:

1. The lane table's CHECK constraint is widened to name `DRAWN_LENGTH`.
2. The deferred provenance trigger function from 0006 (`check_canonical_observation_provenance`,
   run at COMMIT) is replaced with the same body plus the architect's lane, mirroring
   `evidence/canonical.py`: a CORROBORATED observation is also valid with at least one supporting
   reading, no conflicting one, and a `DRAWN_LENGTH` lane, **only when its `document_role` is
   `ARCH`**. A `DRAWN_LENGTH` lane on any other side is refused outright, whatever the status and
   however many readers agree. Without (2) the lane was accepted at flush and refused at commit,
   which is how the first real run found it.

The lane is never written on a candidate, and the candidate constraint is left exactly as migration
0036 wrote it. Nothing is backfilled. Downgrade restores 0006's function body exactly, then narrows
the lane constraint (refused by the database if any row already carries the lane).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0077_drawn_length_lane"
down_revision: str | None = "0076_architect_pairing_records"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_LANES = "'SECOND_READER', 'DUAL_UNIT', 'HUMAN', 'MECHANICAL_TAG'"
_NEW_LANES = _OLD_LANES + ", 'DRAWN_LENGTH'"

# The body 0006 created, character for character (a migration describes one fixed historical state
# and must not import another migration). `tests/db/test_evidence_models.py` checks that a downgrade
# puts back exactly 0006's text.
_ORIGINAL_BODY = """
        DECLARE
            observation_id uuid;
            observation_status text;
            supporting_count integer;
            conflicting_count integer;
            has_dual_unit boolean;
        BEGIN
            IF TG_TABLE_NAME = 'canonical_observations' THEN
                IF TG_OP = 'DELETE' THEN
                    observation_id := OLD.id;
                ELSE
                    observation_id := NEW.id;
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                observation_id := OLD.canonical_observation_id;
            ELSE
                observation_id := NEW.canonical_observation_id;
            END IF;

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
        """

_ARCHITECT_BODY = """
        DECLARE
            observation_id uuid;
            observation_status text;
            observation_role text;
            supporting_count integer;
            conflicting_count integer;
            has_dual_unit boolean;
            has_drawn_length boolean;
        BEGIN
            IF TG_TABLE_NAME = 'canonical_observations' THEN
                IF TG_OP = 'DELETE' THEN
                    observation_id := OLD.id;
                ELSE
                    observation_id := NEW.id;
                END IF;
            ELSIF TG_OP = 'DELETE' THEN
                observation_id := OLD.canonical_observation_id;
            ELSE
                observation_id := NEW.canonical_observation_id;
            END IF;

            SELECT status, document_role INTO observation_status, observation_role
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

            SELECT EXISTS(
                SELECT 1 FROM evidence_corroboration_lanes
                WHERE canonical_observation_id = observation_id AND lane = 'DRAWN_LENGTH'
            ) INTO has_drawn_length;

            IF has_drawn_length AND observation_role IS DISTINCT FROM 'ARCH' THEN
                RAISE EXCEPTION 'DRAWN_LENGTH provenance is invalid: the architect''s side only'
                    USING ERRCODE = '23514';
            END IF;

            IF observation_status = 'RAW_CANDIDATE'
               AND NOT (supporting_count >= 1 AND conflicting_count = 0) THEN
                RAISE EXCEPTION 'RAW_CANDIDATE provenance is invalid' USING ERRCODE = '23514';
            ELSIF observation_status = 'CORROBORATED'
               AND NOT ((supporting_count >= 2
                         OR (supporting_count >= 1 AND has_dual_unit)
                         OR (supporting_count >= 1 AND has_drawn_length
                             AND observation_role = 'ARCH'))
                        AND conflicting_count = 0) THEN
                RAISE EXCEPTION 'CORROBORATED provenance is invalid' USING ERRCODE = '23514';
            ELSIF observation_status = 'CONFLICTING'
               AND NOT (supporting_count >= 1 AND conflicting_count >= 1) THEN
                RAISE EXCEPTION 'CONFLICTING provenance is invalid' USING ERRCODE = '23514';
            END IF;
            RETURN NULL;
        END;
        """


def _replace(table: str, constraint: str, expression: str) -> None:
    op.drop_constraint(constraint, table, type_="check")
    op.create_check_constraint(constraint, table, expression)


def _apply(lanes: str) -> None:
    _replace(
        "evidence_corroboration_lanes",
        "evidence_corroboration_lane",
        f"lane IN ({lanes})",
    )


def _provenance_function(body: str) -> None:
    # `CREATE OR REPLACE` keeps the function's identity, so 0006's three constraint triggers keep
    # calling it without being recreated.
    op.execute(
        "CREATE OR REPLACE FUNCTION check_canonical_observation_provenance()\n"
        f"        RETURNS trigger LANGUAGE plpgsql AS $${body}$$"
    )


def upgrade() -> None:
    _apply(_NEW_LANES)
    _provenance_function(_ARCHITECT_BODY)


def downgrade() -> None:
    """Restore 0006's provenance check, then narrow the lanes (refused if any row has the lane)."""
    _provenance_function(_ORIGINAL_BODY)
    _apply(_OLD_LANES)
