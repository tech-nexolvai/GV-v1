"""Each upload keeps the file name the reviewer uploaded, cleaned, as display text only (#1174).

Verification for `alembic/versions/0085_document_version_file_name.py`, `DocumentVersion.file_name`
and `app.models.document.clean_file_name`:

- the cleaning keeps the base name only, strips control characters and direction marks, caps the
  length and keeps every script;
- the migrated column is nullable, and its CHECK refuses a path, a control character or a blank
  name from any writer;
- the migration goes down and up, an older upload keeps no name, and a downgrade with names stored
  refuses.

Every name here is made up.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.models.document import FILE_NAME_LIMIT, clean_file_name
from tests.app.postgres_fixture import alembic_config

pytest_plugins = ("tests.app.postgres_fixture",)

REVISION = "0085_document_version_file_name"
BEFORE = "0084_architect_view_matches"


# --- the cleaning, without a database ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Synthetic Plans A.pdf", "Synthetic Plans A.pdf"),
        ("C:\\fakepath\\Synthetic Plans A.pdf", "Synthetic Plans A.pdf"),
        ("/home/someone/uploads/sample-set.pdf", "sample-set.pdf"),
        ("../../etc/sample.pdf", "sample.pdf"),
        ("mixed\\and/slashes\\sample.pdf", "sample.pdf"),
        ("  padded name.pdf \t", "padded name.pdf"),
        ("line\nbreak\r\x00null\x1b.pdf", "linebreaknull.pdf"),
        ("del\x7fand\x85next.pdf", "delandnext.pdf"),
        ("over\u202eride\u2066.pdf", "override.pdf"),
        ("sep\u2028ar\u2029ate.pdf", "separate.pdf"),
        ("Plänë Ünïcödé — 図面 مخطط.pdf", "Plänë Ünïcödé — 図面 مخطط.pdf"),
        ("e\u0301te\u0301.pdf", "été.pdf"),
        ('<i>Synthetic & Co"s =1+1.pdf', '<i>Synthetic & Co"s =1+1.pdf'),
    ],
)
def test_a_name_is_cleaned_to_its_base_name_without_control_characters(
    raw: str, cleaned: str
) -> None:
    assert clean_file_name(raw) == cleaned


@pytest.mark.parametrize("raw", [None, "", "   ", "\x00\x01", "C:\\fakepath\\", "a/b/", ".", ".."])
def test_nothing_left_is_no_name(raw: str | None) -> None:
    assert clean_file_name(raw) is None


def test_a_long_name_is_capped_and_keeps_its_extension() -> None:
    raw = "S" * 400 + ".pdf"
    cleaned = clean_file_name(raw)
    assert cleaned is not None
    assert len(cleaned) == FILE_NAME_LIMIT == 255
    assert cleaned.endswith("….pdf") and cleaned.startswith("SSSS")


def test_a_long_name_with_no_short_extension_is_capped() -> None:
    cleaned = clean_file_name("界" * 300)
    assert cleaned is not None
    assert len(cleaned) == FILE_NAME_LIMIT and cleaned.endswith("…")
    # Only the base name counts towards the limit.
    assert clean_file_name("D" * 500 + "/short.pdf") == "short.pdf"


def test_a_name_at_the_limit_is_kept_whole() -> None:
    raw = "N" * (FILE_NAME_LIMIT - 4) + ".pdf"
    assert clean_file_name(raw) == raw


# --- the migration --------------------------------------------------------------------------------


def _config(engine: Engine):  # type: ignore[no-untyped-def]
    config = alembic_config()
    config.attributes["database_url"] = engine.url.render_as_string(hide_password=False)
    return config


def _insert_version(engine: Engine, file_name: str | None, *, named: bool = True) -> UUID:
    """One upload, written with SQL so it works on either side of the migration."""
    project, package, document, artifact, version = (uuid4() for _ in range(5))
    sha = uuid4().hex * 2
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO projects (id, name, created_at) VALUES (:id, 'synthetic', now())"),
            {"id": project},
        )
        connection.execute(
            text("INSERT INTO packages (id, project_id, created_at) VALUES (:id, :p, now())"),
            {"id": package, "p": project},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, package_id, kind, created_at) "
                "VALUES (:id, :p, 'architectural', now())"
            ),
            {"id": document, "p": package},
        )
        connection.execute(
            text(
                "INSERT INTO source_artifacts (id, storage_key, sha256, size, created_at) "
                "VALUES (:id, :key, :sha, 1, now())"
            ),
            {"id": artifact, "key": f"synthetic/{artifact}", "sha": sha},
        )
        columns = "id, document_id, source_artifact_id, sha256, page_count, created_at"
        values = ":id, :d, :a, :sha, 1, now()"
        if named:
            columns += ", file_name"
            values += ", :name"
        connection.execute(
            text(f"INSERT INTO document_versions ({columns}) VALUES ({values})"),
            {"id": version, "d": document, "a": artifact, "sha": sha, "name": file_name},
        )
    return version


def _name(engine: Engine, version: UUID) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            text("SELECT file_name FROM document_versions WHERE id = :id"), {"id": version}
        ).scalar_one()


def test_the_revision_follows_the_architect_view_matches() -> None:
    """`alembic_version.version_num` is 32 characters; a longer id fails CI at upgrade time."""
    assert len(REVISION) <= 32
    script = ScriptDirectory.from_config(alembic_config())
    found = script.get_revision(REVISION)
    assert found is not None and found.down_revision == BEFORE
    # On the chain to the head; a later migration (0086, #1181) may follow it.
    assert REVISION in {
        rev.revision for rev in script.walk_revisions("base", script.get_current_head())
    }


def test_the_migrated_column_is_nullable_and_holds_only_clean_names(
    postgres_engine: Engine,
) -> None:
    command.upgrade(_config(postgres_engine), "head")

    column = next(
        c
        for c in inspect(postgres_engine).get_columns("document_versions")
        if c["name"] == "file_name"
    )
    assert column["nullable"] is True
    assert getattr(column["type"], "length", None) == FILE_NAME_LIMIT

    assert _name(postgres_engine, _insert_version(postgres_engine, None)) is None
    named = _insert_version(postgres_engine, "Plänë 図面 sample.pdf")
    assert _name(postgres_engine, named) == "Plänë 図面 sample.pdf"
    for refused in ("folder/sample.pdf", "C:\\sample.pdf", "tab\there.pdf", "   ", ""):
        with pytest.raises(IntegrityError):
            _insert_version(postgres_engine, refused)
    with pytest.raises(Exception, match="too long"):
        _insert_version(postgres_engine, "L" * (FILE_NAME_LIMIT + 1))


def test_the_name_is_part_of_the_append_only_upload(postgres_engine: Engine) -> None:
    """The upload's row is append-only (0013): a name, once confirmed, is never edited."""
    command.upgrade(_config(postgres_engine), "head")
    version = _insert_version(postgres_engine, "first-name.pdf")

    with (
        pytest.raises(Exception, match="append-only|immutable|not allowed|cannot"),
        postgres_engine.begin() as connection,
    ):
        connection.execute(
            text("UPDATE document_versions SET file_name = 'other.pdf' WHERE id = :id"),
            {"id": version},
        )
    assert _name(postgres_engine, version) == "first-name.pdf"


def test_the_migration_goes_down_and_up_and_an_older_upload_has_no_name(
    postgres_engine: Engine,
) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    command.downgrade(config, BEFORE)

    assert "file_name" not in {
        column["name"] for column in inspect(postgres_engine).get_columns("document_versions")
    }
    older = _insert_version(postgres_engine, None, named=False)

    command.upgrade(config, "head")

    assert _name(postgres_engine, older) is None


def test_a_downgrade_with_names_stored_refuses(postgres_engine: Engine) -> None:
    config = _config(postgres_engine)
    command.upgrade(config, "head")
    _insert_version(postgres_engine, "kept-name.pdf")

    with pytest.raises(RuntimeError, match="Preserve these records"):
        command.downgrade(config, BEFORE)

    assert "file_name" in {
        column["name"] for column in inspect(postgres_engine).get_columns("document_versions")
    }
