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

from decimal import Decimal
from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.review.chat_models import ChatModelChoice


class Settings(BaseSettings):
    """Everything the API needs, stated rather than discovered.

    `extra="forbid"`: an unrecognised `GV_` variable is an error, not something to ignore. A typo in
    a deployment variable would otherwise leave the setting at its default and the operator
    convinced they had changed it.

    **`env_file=".env"` makes `.env.example`'s own first line true** (#417, admin decision
    2026-08-23). It said *"Copy to .env and fill"* while this was `None`, so copying it did nothing
    at all — and combined with the missing `GV_` prefix, a developer following the instructions got a
    application that would not start and no clue why.

    Two consequences worth stating, because both are load-bearing:

    * **A real environment variable wins over the same key in `.env`.** That is pydantic-settings'
      own precedence and it is the order that matters: CI and containers pass variables directly, and
      a developer's stale file must never override them. `tests/test_env_example.py` asserts it
      rather than trusting the library not to change.
    * **`extra="forbid"` now applies to the file as well.** An unknown key in `.env` raises instead
      of being ignored — verified, not assumed. So `.env.example` may only carry live keys that are
      real settings; anything this project will need later lives there as a comment, and the file
      says why.
    """

    model_config = SettingsConfigDict(
        env_prefix="GV_", extra="forbid", frozen=True, env_file=".env"
    )

    database_url: str = Field(min_length=1)
    """No default. A service pointed at the wrong database by a fallback is worse than one that
    refuses to start, because it will happily write."""

    environment: str = Field(default="development", min_length=1)
    request_id_header: str = Field(default="X-Request-ID", min_length=1)

    # The reviewer chat is optional presentation over completed findings.  It shares the deployment's
    # configured Bedrock identity. An API without model credentials still serves the plain deterministic
    # findings view because provider construction and every call are behind the fallback boundary.
    bedrock_chat_enabled: bool = True
    # Empty explicitly disables the optional presentation provider.  Deterministic checks, reports
    # and grounded structured chat continue to work with this setting unset.
    bedrock_model: str = Field(default="", min_length=0)
    bedrock_region: str = Field(default="us-east-1", min_length=1)
    bedrock_connect_timeout: int = Field(default=10, ge=1)
    bedrock_read_timeout: int = Field(default=120, ge=1)
    # The models a reviewer may pick from in the chat, as a JSON list of {id, label}. This is an
    # allow-list, not a free choice: the endpoint refuses any id not in it, so a reviewer can never
    # reach an unapproved or unmetered model. Empty means "only the default `bedrock_model`" — the
    # picker then offers that single model, which is exactly today's behaviour made visible.
    bedrock_chat_models: tuple[ChatModelChoice, ...] = ()

    # Form-first reading is a separate, experimental proposal lane. It defaults off; when enabled,
    # every operational bound and per-reader throttle rate must be stated rather than inferred.
    form_reader_enabled: bool = False
    form_reader_primary_model: str = "us.moonshotai.kimi-k3"
    form_reader_second_model: str = "qwen.qwen3-vl-235b-a22b"
    form_reader_max_concurrent_calls: int | None = Field(default=None, ge=1)
    form_reader_max_tokens: int | None = Field(default=None, ge=1)
    form_reader_model_rpm: dict[str, int] = Field(default_factory=dict)
    form_reader_max_throttle_retries: int | None = Field(default=None, ge=0)
    form_reader_retry_backoff_seconds: float | None = Field(default=None, gt=0)
    # The measured reading guidance quotes the client's drawings, so it lives in a private file
    # outside this public repository; required when the form reader is on (#972).
    form_reader_prompt_file: Path | None = None
    # Slot reads (#987): code finds each slot and cuts its crop; two readers read the crops. Off by
    # default, and only with the form reader on — it uses the form reader's readers, prices and
    # limits, and the whole-page reading stays for a page with no row. The fraction-bar lengths
    # (`GV_READER_FRACTION_*`) are required by the worker when it is on.
    slot_reader_enabled: bool = False
    # Grounded full-view plus close-up reader route (#1001 onward). It is separate from the
    # existing one-crop route and defaults off until every proof gate is met.
    claude_reader_enabled: bool = False
    # The admin's yes/no (#987): may a stacked fraction seal when two readers of different makers
    # give the identical text on a code-made crop? Off: a stacked fraction goes to the person.
    slot_reader_stacked_agreement: bool = False

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

    run_edge_tolerance: Decimal | None = None
    """`GV_RUN_EDGE_TOLERANCE`: how far, in stored page units, a part may stray past a countertop's
    ends and top and still be suggested as part of the run beneath it (#893).

    **The same question decides which reading is suggested as a part's width (#913)**: whether the
    ends of a reading's dimension line, or of the region it is printed in, meet the part's two ends,
    and whether a reading is printed between the ends of its own line. Each asks whether two places
    drawn on the page are one, so one number answers all of them; a second could disagree with the
    first. Only the suggestion uses it there: a link records no tolerance, and unset, no reading is
    suggested though a person may still pick one.

    **Unset means no run is suggested and none can be confirmed**, and the Measure page says so.
    There is no default number: whether a part a hair past the countertop's end belongs to its run
    is a question about the drawings, and a guessed tolerance would decide it for every package. It
    is recorded on every run a person confirms, so a run can be read again under the number it was
    decided with. Stored units are the normalised `0..1` page space, not a distance: the same number
    is a different physical size on different sheets."""

    @field_validator("run_edge_tolerance")
    @classmethod
    def _finite_tolerance(cls, value: Decimal | None) -> Decimal | None:
        """Refuse a tolerance that would remove the tests rather than loosen them: NaN fails every
        comparison, infinity passes every one, and a negative one refuses a part that meets the
        countertop exactly."""
        if value is not None and (not value.is_finite() or value < 0):
            raise ValueError("run_edge_tolerance must be a finite number, zero or more")
        return value

    @model_validator(mode="after")
    def _form_reader_bounds_are_stated(self) -> Settings:
        if self.claude_reader_enabled and not self.slot_reader_enabled:
            raise ValueError(
                "GV_CLAUDE_READER_ENABLED requires GV_SLOT_READER_ENABLED and its form-reader "
                "runtime; the Claude route must not run as an ungrounded whole-page fallback"
            )
        if self.slot_reader_enabled and not self.form_reader_enabled:
            raise ValueError(
                "GV_SLOT_READER_ENABLED requires GV_FORM_READER_ENABLED: the slot reader uses the "
                "form reader's readers, prices and limits"
            )
        if self.slot_reader_stacked_agreement and not self.slot_reader_enabled:
            raise ValueError(
                "GV_SLOT_READER_STACKED_AGREEMENT only means something with GV_SLOT_READER_ENABLED"
            )
        if not self.form_reader_enabled:
            return self
        if not self.form_reader_primary_model.strip() or not self.form_reader_second_model.strip():
            raise ValueError(
                "GV_FORM_READER_PRIMARY_MODEL and GV_FORM_READER_SECOND_MODEL must be non-empty"
            )
        if self.form_reader_primary_model == self.form_reader_second_model:
            raise ValueError("form reader requires two distinct reader models")
        missing = [
            name
            for name, value in (
                ("GV_FORM_READER_MAX_CONCURRENT_CALLS", self.form_reader_max_concurrent_calls),
                ("GV_FORM_READER_MAX_TOKENS", self.form_reader_max_tokens),
                ("GV_FORM_READER_MAX_THROTTLE_RETRIES", self.form_reader_max_throttle_retries),
                ("GV_FORM_READER_RETRY_BACKOFF_SECONDS", self.form_reader_retry_backoff_seconds),
                ("GV_FORM_READER_PROMPT_FILE", self.form_reader_prompt_file),
            )
            if value is None
        ]
        missing.extend(
            model
            for model in (self.form_reader_primary_model, self.form_reader_second_model)
            if model not in self.form_reader_model_rpm
        )
        if missing:
            raise ValueError(
                "GV_FORM_READER_ENABLED requires explicit concurrency, token, retry, backoff, "
                f"prompt-file and per-model RPM settings; missing: {', '.join(missing)}"
            )
        if any(
            isinstance(rpm, bool) or not isinstance(rpm, int) or rpm <= 0
            for rpm in self.form_reader_model_rpm.values()
        ):
            raise ValueError("GV_FORM_READER_MODEL_RPM values must be positive whole numbers")
        return self

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
