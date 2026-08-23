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

    hatchet_token: str = Field(default="", description="Hatchet client token")
    """Empty by default, and the emptiness is caught where it matters. `workflow/hatchet_app.py` builds a
    client only when a worker is started, and the SDK refuses a blank token there — so an API process,
    which never starts a worker, does not need one. Requiring it here would make every API deployment
    carry a credential it has no use for."""

    hatchet_namespace: str = Field(default="", min_length=0)
    """Namespace prefix for workflow names, so two environments can share one engine without one
    picking up the other's packages."""

    max_concurrent_packages: int = Field(default=1, ge=1)
    """How many package revisions may be processed at once. **Defaults to 1 deliberately.**

    One 8 GB VM shares memory between rendering, OCR and PostgreSQL, so a second package processing
    alongside the first is a second package's worth of resident pages. At 1, peak memory is one
    package's work plus the database, a second package queues rather than competing, and an
    out-of-memory kill cannot be blamed on contention between packages.

    It is a setting so it can be raised — but raising it should follow a measurement against real
    drawings, not an assumption that the box has room."""

    outbox_poll_seconds: float = Field(default=2.0, gt=0)
    """How long the dispatcher waits between polls of the outbox (#415, F3.1).

    Two seconds is a latency choice, not a throughput one. The outbox is drained by polling, so this is
    the worst case between a package being accepted and its workflow starting — and a person who has
    just uploaded a package is watching. Two seconds is short enough to read as "it started" and long
    enough that an idle queue is one cheap `SELECT` every two seconds rather than a busy loop.

    Lower it and an idle system does more useless work; raise it and the visible wait grows. Neither is
    dangerous: nothing is lost by polling late, it just arrives late."""

    outbox_batch_limit: int = Field(default=100, ge=1)
    """How many outbox rows one pass may dispatch (#415, F3.1).

    The whole batch shares one transaction, so this bounds two things at once: how long that transaction
    holds its row locks, and how much work is repeated if the process dies mid-pass. 100 matches the
    default `dispatch_committed()` already had, so making it configurable changes no behaviour.

    A backlog larger than this is not stuck — `FOR UPDATE SKIP LOCKED` means the next pass takes the
    next rows, and several dispatchers can run at once."""

    max_concurrent_page_tasks: int = Field(default=2, ge=1)
    """How many tasks one worker runs at once. Defaults to 2: a little parallelism where the unit of
    work is smaller than a whole package.

    **What it bounds today, stated precisely, because the name is ahead of the code.** This becomes the
    worker's slot count, and the only tasks registered so far are the six stages — which run in a line,
    each waiting for the one before. So today it caps concurrent *stage* tasks across packages, and one
    package on its own cannot use more than a single slot.

    It is named for pages because pages are what it is *for*: B6.4 (#163) adds the task-per-page fan-out,
    and that is when rendering and OCR become the resident cost this number is meant to hold down. Until
    then the name describes the intent and this docstring describes the effect. Worth renaming if #163
    moves further out — that is Anant's call, not a silent change."""

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
