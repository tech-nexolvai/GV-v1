"""Alembic environment wired to application metadata and settings."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

# Imported for its side effects: a model registers in Base.metadata only when its module is
# imported. Without this, autogenerate compares against an empty schema and silently produces a
# migration that creates nothing. See app/models/__init__.py.
import app.models  # noqa: F401  (side-effect import, must come after Base)
from alembic import context
from app.db.base import Base
from app.db.session import settings_from_environment

config = context.config
if config.config_file_name is not None:
    # `disable_existing_loggers=False` — the default is True, and it silenced the application.
    #
    # `alembic.ini` names only `root`, `sqlalchemy` and `alembic`, so with the default every *other*
    # logger already constructed gets `disabled = True`. Anything running migrations in the same
    # process — the test fixture, a management script — lost every `gv.*` log line from that point on,
    # permanently and with no error. A warning that cannot be emitted is the same as one nobody wrote,
    # which is how `app/api/finding_export.py`'s unrecognised-outcome warning came to be dead on
    # arrival. Configuring Alembic's logging has no business turning ours off.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    explicit = config.attributes.get("database_url")
    if explicit is not None:
        return str(explicit)
    return settings_from_environment().database_url


def run_migrations_offline() -> None:
    """Run migrations without creating an Engine."""

    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a connection created from explicit settings."""

    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
