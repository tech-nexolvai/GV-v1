"""Typed settings, validated once at startup.

**A missing setting fails when the process starts, never at first request.** The difference matters:
a service that boots and then 500s on the first upload looks healthy to everything watching it, and
the failure arrives when somebody is trying to use it rather than when it was deployed.

So `Settings` is constructed by the application factory, and a bad or absent value raises there. No
`os.environ.get(...)` with a default anywhere else in `app/` — a default is how a setting stops being
required without anybody deciding that it should.

Source: `docs/DESIGN_PLATFORM.md` §4.1 · Verification: `tests/api/test_app.py`
"""

from __future__ import annotations

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Everything the API needs, stated rather than discovered.

    `extra="forbid"`: an unrecognised `GV_` variable is an error, not something to ignore. A typo in
    a deployment variable would otherwise leave the setting at its default and the operator
    convinced they had changed it.
    """

    model_config = SettingsConfigDict(env_prefix="GV_", extra="forbid", frozen=True, env_file=None)

    database_url: str = Field(min_length=1)
    """No default. A service pointed at the wrong database by a fallback is worse than one that
    refuses to start, because it will happily write."""

    environment: str = Field(default="development", min_length=1)
    request_id_header: str = Field(default="X-Request-ID", min_length=1)

    @field_validator("database_url")
    @classmethod
    def _looks_like_postgres(cls, value: str) -> str:
        """Refuse a URL for a database this project does not run on.

        SQLite would accept most of the schema and silently lose the things the safety argument rests
        on — no `JSONB`, no deferred constraints, different `NUMERIC` behaviour. Failing here is
        better than passing tests against a database production will never use.
        """
        if not value.startswith(("postgresql://", "postgresql+psycopg://")):
            raise ValueError(
                "database_url must be a PostgreSQL URL. This schema uses JSONB, deferred "
                "constraints and exact NUMERIC, none of which behave the same elsewhere."
            )
        return value
