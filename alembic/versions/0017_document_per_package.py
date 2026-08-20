"""A document belongs to the package; a revision names the versions it includes (#366).

Revision ID: 0017_document_per_package
Revises: 0016_state_event_workflow_run

Implements ADR-0018, ratified as D17 (#367).

`documents` carried `package_revision_id`, so a drawing belonged to exactly one revision — while its
own docstring called it the *"logical document identity shared by all uploads of that document"*. Both
could not be true, and supersede (#211) is where it bit: a superseding revision could not include a
sheet that had not changed, because `uq_document_versions_source_artifact_id` refuses a second version
over the same bytes. A revision holding only the changed drawing runs its checks against a partial set,
and the absent drawings produce no failures — which reads as no problems (`AGENTS.md` §2.2).

So identity moves to the package, and a revision's contents become an explicit append-only set.

**Why `package_id` is resolved at both ends.** `package_revision_documents` names the package once and
points it at *both* the revision and the document. Resolving only the document side admits a row whose
every value is individually true and whose combination is a lie: this document does belong to that
package, that revision does exist — but the revision belongs to a different package. That insert was
prototyped against PostgreSQL while ADR-0018 was still in draft and it **succeeded**, which is why
`package_revisions` and `documents` each gain `UNIQUE (id, package_id)` here.

**The backfill has to choose, and this says which way.** `confirm_upload` adds a new version to an
existing document on the *same* revision, so existing data can hold two versions of one document per
revision — which `uq_revision_documents_one_version_per_document` forbids. The backfill takes the
latest version per `(revision, document)`. Nothing is lost: earlier versions stay in
`document_versions` as history, and a finding cites a version directly rather than through a revision's
set.

**On any database that exists today this backfill is a no-op.** There is no deployment; every database
is built from these migrations. It is written correctly for when that stops being true, not because rows
are being rescued now.

Verification: `tests/db/test_document_models.py`, `tests/db/test_append_only.py`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0017_document_per_package"
down_revision: str | None = "0016_state_event_workflow_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Literals throughout, not module constants. Two of this repo's checks read migrations as text —
#: `tests/app/test_migration_matches_models.py` walks the AST for `create_table`, `add_column` and
#: `drop_column` — and a name behind a constant is a name a static reader cannot resolve.


def upgrade() -> None:
    """Move document identity to the package and record revision membership explicitly."""
    # The composite keys the membership table points at. Both, for the reason in the docstring.
    op.create_unique_constraint(
        "uq_package_revisions_id_package", "package_revisions", ["id", "package_id"]
    )

    # ---- documents.package_id, backfilled from the revision it used to hang off ----
    op.add_column("documents", sa.Column("package_id", sa.Uuid(as_uuid=True), nullable=True))
    op.execute("""
        UPDATE documents AS d
           SET package_id = r.package_id
          FROM package_revisions AS r
         WHERE r.id = d.package_revision_id
        """)
    op.alter_column("documents", "package_id", nullable=False)
    op.create_foreign_key(
        "fk_documents_package_id_packages",
        "documents",
        "packages",
        ["package_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_unique_constraint("uq_documents_id_package", "documents", ["id", "package_id"])
    op.create_unique_constraint(
        "uq_document_versions_id_document", "document_versions", ["id", "document_id"]
    )

    # ---- the membership set ----
    op.create_table(
        "package_revision_documents",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("package_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("document_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["package_revision_id", "package_id"],
            ["package_revisions.id", "package_revisions.package_id"],
            name="fk_revision_documents_revision_package",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "package_id"],
            ["documents.id", "documents.package_id"],
            name="fk_revision_documents_document_package",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id", "document_id"],
            ["document_versions.id", "document_versions.document_id"],
            name="fk_revision_documents_version_document",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "package_revision_id",
            "document_id",
            name="uq_revision_documents_one_version_per_document",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_package_revision_documents_package_revision_id",
        "package_revision_documents",
        ["package_revision_id"],
    )
    op.create_index(
        "ix_package_revision_documents_package_id", "package_revision_documents", ["package_id"]
    )
    op.create_index(
        "ix_package_revision_documents_document_id", "package_revision_documents", ["document_id"]
    )
    op.create_index(
        "ix_package_revision_documents_document_version_id",
        "package_revision_documents",
        ["document_version_id"],
    )

    # ---- backfill membership: the latest version of each document, per revision ----
    op.execute("""
        INSERT INTO package_revision_documents
            (id, created_at, package_revision_id, package_id, document_id, document_version_id)
        SELECT gen_random_uuid(), now(), d.package_revision_id, d.package_id, d.id, latest.id
          FROM documents AS d
          JOIN LATERAL (
                SELECT v.id
                  FROM document_versions AS v
                 WHERE v.document_id = d.id
              ORDER BY v.created_at DESC, v.id DESC
                 LIMIT 1
               ) AS latest ON true
        """)

    # ---- frozen once the revision has been read, mutable while it is assembled ----
    #
    # Not the blanket `gv_reject_mutation` the twenty-eight append-only tables use, and that is a
    # decision. A revision is *assembled* before it is worked: drawings arrive one at a time, and a
    # mis-uploaded sheet re-uploaded a minute later is ordinary use. So the set may change while the
    # revision is in CREATED, UPLOADING or UPLOADED, and is refused from INGESTING onward — the first
    # state in which something has read it. A set that can change after it has been read is a set
    # nobody can be held to.
    #
    # The state list is a literal here because a migration describes one fixed historical state and
    # must not import live code. `app/lifecycle/states.py` holds `ASSEMBLY_STATES` as the single
    # source, and `tests/db/test_document_models.py` asserts these two still agree — the same drift
    # guard #313 needed for the outcome enum, for the same reason.
    op.execute("""
        CREATE OR REPLACE FUNCTION gv_reject_frozen_revision_documents() RETURNS trigger AS $$
        DECLARE
            revision_state text;
            revision uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                revision := OLD.package_revision_id;
            ELSE
                revision := NEW.package_revision_id;
            END IF;

            SELECT state INTO revision_state
              FROM package_revisions WHERE id = revision;

            IF revision_state NOT IN ('CREATED', 'UPLOADING', 'UPLOADED') THEN
                RAISE EXCEPTION
                    'package revision % is in %, so the documents it was reviewed against cannot '
                    'change. Supersede the revision instead.', revision, revision_state
                    USING ERRCODE = 'raise_exception';
            END IF;

            IF TG_OP = 'DELETE' THEN
                RETURN OLD;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """)
    op.execute("""
        CREATE TRIGGER package_revision_documents_frozen_once_read
        BEFORE UPDATE OR DELETE ON package_revision_documents
        FOR EACH ROW EXECUTE FUNCTION gv_reject_frozen_revision_documents()
        """)

    # Last, so the two backfills above could still read it.
    op.drop_column("documents", "package_revision_id")


def downgrade() -> None:
    """Put identity back on the revision, and lose the cross-revision link.

    Lossy by nature, and worth naming: a drawing shared by two revisions cannot be expressed by a
    single `package_revision_id`, so the column is restored from the *earliest* revision that included
    the document. A package whose revisions genuinely shared drawings does not round-trip.
    """
    op.add_column(
        "documents", sa.Column("package_revision_id", sa.Uuid(as_uuid=True), nullable=True)
    )
    op.execute("""
        UPDATE documents AS d
           SET package_revision_id = earliest.package_revision_id
          FROM (
                SELECT document_id, MIN(package_revision_id::text)::uuid AS package_revision_id
                  FROM package_revision_documents
              GROUP BY document_id
               ) AS earliest
         WHERE earliest.document_id = d.id
        """)
    op.execute("DELETE FROM documents WHERE package_revision_id IS NULL")
    op.alter_column("documents", "package_revision_id", nullable=False)
    op.create_foreign_key(
        "fk_documents_package_revision_id_package_revisions",
        "documents",
        "package_revisions",
        ["package_revision_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    op.execute(
        "DROP TRIGGER IF EXISTS package_revision_documents_frozen_once_read "
        "ON package_revision_documents"
    )
    op.drop_table("package_revision_documents")
    op.execute("DROP FUNCTION IF EXISTS gv_reject_frozen_revision_documents()")
    op.drop_constraint("uq_document_versions_id_document", "document_versions", type_="unique")
    op.drop_constraint("uq_documents_id_package", "documents", type_="unique")
    op.drop_constraint("fk_documents_package_id_packages", "documents", type_="foreignkey")
    op.drop_column("documents", "package_id")
    op.drop_constraint("uq_package_revisions_id_package", "package_revisions", type_="unique")
