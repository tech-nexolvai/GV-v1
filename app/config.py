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

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.review.chat_models import ChatModelChoice

#: The gates `GV_TRY_AGAIN_GATES` may name (#1309), in step with
#: `extraction/slot_reader/try_again.TRY_AGAIN_GATES` (`tests/workflow/test_try_again.py` checks).
TRY_AGAIN_GATE_NAMES: frozenset[str] = frozenset(
    {
        "rekha:two-readers-agree",
        "rekha:row-has-printed-numbers",
        "rekha:other-numbered-lines",
        "akshara:seal",
    }
)


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
    # The host names the API answers to besides this machine's own (`localhost`, `127.0.0.1`,
    # `::1`): a deployment's public name, as a JSON list (`GV_TRUSTED_HOSTS='["gv.example.com"]'`).
    # Any other `Host` is refused, and so is a browser write from another site's page
    # (`app/host_guard.py`, against DNS rebinding).
    trusted_hosts: tuple[str, ...] = ()

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

    # The review assistant (#1128): grounded answers about one review, from its records only, by
    # Claude Sonnet 5.5 through OpenRouter on the readers' keep-no-data route (Google Vertex global,
    # no fallbacks, no data kept). Off unless enabled AND `OPENROUTER_API_KEY` is set; off, it still
    # refuses decision requests and answers the starter questions from the records, with no call.
    review_assistant_enabled: bool = False
    review_assistant_model: Literal["anthropic.claude-sonnet-5-5"] = "anthropic.claude-sonnet-5-5"
    # Each call's cost is recorded for the Usage page from `GV_MODEL_RATES_FILE`, or as unknown
    # when no price is stated. Since #1431 the assistant also looks at the page asked about (its
    # architect and vendor halves as pictures), within a budget per package revision and a cap per
    # question (a question over the cap is asked without the pictures).
    review_assistant_page_pictures: bool = True
    review_assistant_budget_usd: Decimal = Field(
        default=Decimal("10.00"), ge=0, allow_inf_nan=False
    )
    review_assistant_question_cap_usd: Decimal = Field(
        default=Decimal("0.75"), gt=0, allow_inf_nan=False
    )
    # The tool-using assistant (owner, 2026-10-16): it runs read-only commands on this package's
    # PDFs in a locked sandbox (`app/review/assistant/sandbox.py`) and looks at pictures it renders,
    # up to `review_assistant_max_tool_turns` turns per question, within the caps above. `auto` uses
    # the Docker image `gv-assistant-sandbox` when it is built and Docker answers, else no sandbox
    # (`macos`, `sandbox-exec`, only when named); `off` keeps the one-call assistant.
    review_assistant_sandbox: Literal["auto", "docker", "macos", "off"] = "auto"
    # The tool-using loop is led by this model, which also checks the helpers' reports; the helper
    # agents run on `review_assistant_model`. Agent turns get their own, longer time limit.
    review_assistant_lead_model: Literal[
        "anthropic.claude-opus-5-5", "anthropic.claude-sonnet-5-5"
    ] = "anthropic.claude-opus-5-5"
    review_assistant_agent_timeout_seconds: int = Field(default=180, ge=1, le=600)
    # A question that runs helper agents in parallel (`delegate`) may cost up to this instead.
    review_assistant_delegate_question_cap_usd: Decimal = Field(
        default=Decimal("3.00"), gt=0, allow_inf_nan=False
    )
    review_assistant_max_tool_turns: int = Field(default=12, ge=1, le=20)
    review_assistant_timeout_seconds: int = Field(default=60, ge=1, le=300)
    # How many earlier turns of the conversation are sent with a question (the request takes <= 6).
    review_assistant_max_history_turns: int = Field(default=6, ge=0, le=6)
    # Anant 2026-10-22: a finished full check that would approve while MORE than this share of all
    # its items are Not compared is "Review needed" instead (`review_action.review_needed_share`,
    # which reads `GV_REVIEW_NEEDED_SHARE` itself: the result is worked out where no settings are).
    review_needed_share: Decimal = Field(default=Decimal("0.10"), ge=0, le=1, allow_inf_nan=False)
    # The chat-first assistant (#1463): saved chats per drawing set, the model, effort and thinking
    # the reviewer picks, claude.ai's tools in a locked sandbox. `auto` uses Anthropic's own API
    # when `ANTHROPIC_API_KEY` is set, else OpenRouter's keep-no-data route (`OPENROUTER_API_KEY`);
    # `anthropic` / `openrouter` force one. With neither key the chat says it cannot answer.
    assistant_provider: Literal["auto", "anthropic", "openrouter"] = "auto"
    # Each chat's folder: the set's PDFs, its home, its outputs, the pictures it looked at and the
    # files it handed over. Client data stays on this machine.
    assistant_chat_root: Path = Field(
        default_factory=lambda: Path.home() / "gv-local" / "assistant-chats"
    )
    # What one chat question may spend (a roster full check's cap is its own price, below), and
    # what all the chats of one drawing set may spend together.
    assistant_message_cap_usd: Decimal = Field(default=Decimal(5), gt=0, allow_inf_nan=False)
    assistant_package_cap_usd: Decimal = Field(default=Decimal(40), gt=0, allow_inf_nan=False)
    # Tool turns: the lead's per message, and each helper's.
    assistant_lead_max_turns: int = Field(default=40, ge=1, le=100)
    assistant_helper_max_turns: int = Field(default=20, ge=1, le=50)
    # Helpers run at the same time (#1497): one lead turn's, or a full check's jobs planned by
    # code (#1512).
    assistant_max_parallel_agents: int = Field(default=8, ge=1, le=12)
    # A full check's second, drawn-things helper per page group (#1569): notes, doors and drawers,
    # hardware, extent, brand prototype, finish tags; Sonnet at low effort, capped per helper.
    assistant_drawn_helpers: bool = True
    # A full check as a fixed-price roster of specialists (prompts and specialists design,
    # Balanced): code first, then Sonnet specialists with their own money caps, a fresh-context
    # verifier and a devil's advocate, and no lead. The fixed price is the full check's whole cap;
    # what does not fit in it is listed as not done, never dropped. Off: the lead verifies (#1512).
    assistant_full_check_roster: bool = True
    # Start reading a set's drawings for the full check as soon as AI reading is requested at
    # upload (#1872: the set pack, in the background), so the check does not wait for it later.
    # Off by default: a test server must not read into the default chat folder.
    assistant_read_at_upload: bool = False
    # The full check's fixed price (owner, 2026-10-21, #1823): base + per content page in scope,
    # at least the minimum, never above the drawing set's budget left. It is the full check's own
    # cap: the message cap above is for chat questions only.
    assistant_full_check_base_usd: Decimal = Field(default=Decimal(2), ge=0, allow_inf_nan=False)
    assistant_full_check_per_page_usd: Decimal = Field(
        default=Decimal("0.35"), ge=0, allow_inf_nan=False
    )
    assistant_full_check_min_usd: Decimal = Field(default=Decimal(5), gt=0, allow_inf_nan=False)
    # The roster's first numbers page helpers (owner, 2026-10-21, for a paid A/B): their effort
    # and their most pages each (the drawn-things helpers keep the plan's groups of 3-5 pages; an
    # area stays together unless it is longer than a group). The defaults are today's plan.
    assistant_numbers_helper_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    assistant_numbers_helper_pages: int = Field(default=5, ge=1, le=5)
    # What proves that a vendor number and an architect number measure the same thing, for a red
    # cloud (orchestrator's decision D1, provisional): `ai_pair_plus_code` = two blind AI picks of
    # the same label plus code's checks (printed at both spots, the drawn extents agree, every
    # advocate check passes); `code_only` = only items code paired by itself (the vendor's own
    # sums, GV's rules) can be red.
    assistant_pairing_proof: Literal["ai_pair_plus_code", "code_only"] = "ai_pair_plus_code"
    # The cheap pick for the round-only leftovers (Anant, 2026-10-22: "go with hybrid, with both,
    # but less cost", #1861): a full check's hard cap for the Sonnet picks that name the
    # architect's label of an item compared only with the vendor's earlier round, reserved inside
    # the fixed price. OFF by default (Anant, 2026-10-22, option C): 0 asks nothing, spends $0 and
    # keeps no money; the items stay Not compared. Switched on, such an item is Right or red only
    # when code proves both dimensions end at the same places.
    assistant_round_pick_cap_usd: Decimal = Field(default=Decimal(0), ge=0, allow_inf_nan=False)
    # Which full check runs (CONTRACT-SIMPLE, 2026-10-19): `roster` (today's specialists) or
    # `simple` (code's work list of every vendor drawing, one compare call per drawing, code
    # verifies, one blind look per red candidate; `app/review/assistant/simple_check/`). The switch
    # to `simple` waits for a paid test that beats today's best.
    assistant_full_check_mode: Literal["roster", "simple"] = "roster"
    # The AI page matcher (#1798): when the vendor's and the architect's drawings come as separate
    # PDFs, one cheap call per vendor drawing says which architect item shows the same piece; a
    # pair is made only where code agrees. Both full-check modes; `off`: code pairs alone.
    assistant_page_match_model: Literal["off", "claude-sonnet-5-5", "claude-opus-5-5"] = (
        "claude-sonnet-5-5"
    )
    # The simple check's compare call: the model and effort for every drawing, the stronger model
    # for drawings the work list marks hard (no code labels, very dense, hand-lettered architect),
    # how many calls run at once, the reply's token room (thinking included) and the retries on a
    # lost connection or an overloaded service.
    assistant_simple_compare_model: Literal["claude-sonnet-5-5", "claude-opus-5-5"] = (
        "claude-sonnet-5-5"
    )
    assistant_simple_compare_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    assistant_simple_hard_model: Literal["claude-sonnet-5-5", "claude-opus-5-5"] = "claude-opus-5-5"
    assistant_simple_hard_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    assistant_simple_parallel: int = Field(default=8, ge=1, le=32)
    assistant_simple_max_tokens: int = Field(default=64000, ge=4096, le=128000)
    assistant_simple_retries: int = Field(default=3, ge=0, le=6)
    # The reasoning recipe's compare-prompt steps (#1775), each a switch so a paid A/B can measure
    # it on and off: every label line gives its direction and its place on its drawing (step 1).
    assistant_simple_label_places: bool = True
    # The "looks wrong but isn't" precedents in the compare and blind prompts (step 6).
    assistant_simple_precedents: bool = True
    # The compare repair turn asks only for the claims code rejected, the passed ones frozen
    # (step 4); and the blind look gets one zoomed re-look (close crops round the marked
    # dimension) before an end is left unseen: one more paid call, so off until measured.
    assistant_simple_targeted_repair: bool = True
    assistant_simple_zoom_relook: bool = False
    # The simple check's blind second look (`simple_check/blind.py`): one fresh call per red
    # candidate, shown only crops of both drawings with code's marks, never the compare call's
    # answer; a different model from the compare call by default. Red needs it to agree.
    assistant_simple_blind_model: Literal["claude-sonnet-5-5", "claude-opus-5-5"] = (
        "claude-opus-5-5"
    )
    assistant_simple_blind_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # The simple check's resolve loop (`simple_check/resolve.py`, #1735): a difference the compare
    # call saw that code could not verify gets the exact failed check; the AI names new evidence by
    # code's label ids, code verifies again, at most `rounds` rounds; red still needs every code
    # check and the blind look. `off` (default); `right_only`: it may make Right or a better reason,
    # never a red candidate; `on`. Live web search stays off: when on, one fenced search per run
    # on fixed topics, saved as proposals for the conventions library, never used to decide.
    assistant_simple_resolve: Literal["off", "right_only", "on"] = "off"
    assistant_simple_resolve_rounds: int = Field(default=2, ge=1, le=2)
    assistant_simple_resolve_model: Literal["claude-sonnet-5-5", "claude-opus-5-5"] = (
        "claude-sonnet-5-5"
    )
    # Medium with adaptive thinking (#1775, C4): a judgement is never made with thinking off; the
    # effort is lowered instead to keep its cost.
    assistant_simple_resolve_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    assistant_simple_resolve_web_search: bool = False
    # One sandbox command's time limit, and which sandbox runs it (`auto` and `docker`: the Docker
    # image `gv-assistant-sandbox` when built and Docker answers, else no shell: the chat says the
    # secure sandbox is not running; `macos`: `sandbox-exec`, only for tests on a Mac, never a
    # silent fallback, security audit 2026-10-18).
    assistant_command_timeout_seconds: int = Field(default=120, ge=5, le=900)
    assistant_sandbox: Literal["auto", "docker", "macos"] = "auto"
    # One model call's time limit for connecting pools and sending (a streamed call with long
    # thinking can take minutes: its stream is held only by the idle limit below).
    assistant_call_timeout_seconds: int = Field(default=900, ge=30, le=3600)
    # How long a call's stream may send nothing at all (no event, no ping) before it is stopped
    # with a plain reason (owner decision 2026-10-19: about 3 minutes; `chat_provider`).
    assistant_stream_idle_seconds: int = Field(default=180, ge=30, le=3600)
    # #1551: a typed question no code rule settles is sharpened into a task spec by Claude Haiku
    # 5.5 (low effort, one short JSON reply) before any tool runs; a reply later than the deadline
    # is not waited for (the chat then runs as before, with no spec).
    assistant_sharpen: bool = True
    assistant_sharpen_deadline_seconds: float = Field(default=1.5, gt=0, le=10)
    # #1575: the hi-res page cache (300-dpi tiles of each page a crop was cut from, 0.9 to 2.7 MB a
    # page) is kept within these sizes after each message, the pages used longest ago removed
    # first; a set no chat uses any more loses its cache. Removed pages are drawn again when needed.
    assistant_page_cache_set_mb: int = Field(default=300, ge=10, le=100_000)
    assistant_page_cache_total_mb: int = Field(default=1000, ge=10, le=1_000_000)
    # #1548: pages the cache draws at once, and its decoded tiles kept in memory (MB). Read by the
    # cache when a message starts (they were read from the environment only, so a `.env` line
    # stopped the API).
    assistant_page_cache_workers: int = Field(default=3, ge=1, le=8)
    assistant_page_cache_memory_mb: int = Field(default=256, ge=16, le=100_000)
    # The Usage page can show what OpenRouter reports this key has used (all projects using the
    # key), beside the recorded total (#1165): a free, read-only request with `OPENROUTER_API_KEY`.
    # Off by default, and off whenever no key is set.
    usage_provider_check: bool = False

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
    # The architect's dimensions read by code from the text inside a combined sheet's pasted drawing
    # (#1052): no AI, no cost. Off by default. When on, a drawing whose exact heading and own content
    # agree on its role is confirmed by code, and the architect's values are stored as candidates.
    architect_reader_enabled: bool = False
    # The AI page review (decision 2026-10-16, `workflow/ai_page_review.py`): one Opus 5.5 reading
    # per combined sheet through OpenRouter, decided by code, after the reading. Off by default;
    # paid, within `ai_page_review_budget_usd` per package. The worker reads `GV_AI_PAGE_REVIEW`
    # and `GV_AI_PAGE_REVIEW_BUDGET_USD` from its environment.
    ai_page_review: bool = False
    ai_page_review_budget_usd: Decimal = Field(default=Decimal(6), ge=0)
    # The old direct compare (#1418) the AI page review replaced: off by default, so the worker
    # stores no new records and the review neither shows nor waits on them. Records stay stored.
    arch_compare_records: bool = False
    # Grounded full-view plus close-up reader route (#1001 onward). It is separate from the
    # existing one-crop route and defaults off until every proof gate is met.
    claude_reader_enabled: bool = False
    # Who the two Claude readers are called through (#1094): OpenRouter (the admin's choice,
    # 2026-10-09: same models, keep-no-data account) or Anthropic's own API. Each needs its key.
    claude_reader_provider: Literal["openrouter", "anthropic"] = "openrouter"
    openrouter_api_key: SecretStr | None = Field(
        default=None, validation_alias="OPENROUTER_API_KEY", repr=False
    )
    anthropic_api_key: SecretStr | None = Field(
        default=None, validation_alias="ANTHROPIC_API_KEY", repr=False
    )
    claude_reader_model_rpm: dict[str, int] = Field(default_factory=dict)
    claude_reader_timeout_seconds: int = Field(default=180, ge=1)
    # What the AI readers may spend on one drawing set (#1206): $0.15 for every page sent to them
    # plus $1 for the calls in flight, never below the minimum and never above the admin's maximum
    # (`workflow/ai_budget.py`). The four figures are the defaults Claude proposed (decision log
    # 2026-10-13); the admin may change each. They replace the fixed $2.50 (at most $5) Claude limit
    # and the fixed $3 set meter, which a 60-page set (about $5.25 to read) could not fit.
    ai_budget_per_page_usd: Decimal = Field(default=Decimal("0.15"), gt=0, allow_inf_nan=False)
    ai_budget_margin_usd: Decimal = Field(default=Decimal("1.00"), ge=0, allow_inf_nan=False)
    ai_budget_minimum_usd: Decimal = Field(default=Decimal("3.00"), gt=0, allow_inf_nan=False)
    ai_budget_maximum_usd: Decimal = Field(default=Decimal("25.00"), gt=0, allow_inf_nan=False)
    # Optional: a lower ceiling for one Claude reading, below the page-scaled budget (a cautious
    # trial run, say). Unset (the default) the page-scaled budget alone applies. It can only lower
    # the limit: at most `ai_budget_maximum_usd`, which replaced the old fixed $5 ceiling.
    claude_reader_budget_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    # Optional: a fixed amount for one drawing set in place of the page-scaled budget (a proof
    # run's). The worker reads the same variable (`workflow.stages.ai_budget_from_environment`);
    # the API reads it to say how much of a set's budget is left (#1308). Unset: page-scaled.
    ai_budget_per_set_usd: Decimal | None = Field(default=None, gt=0, allow_inf_nan=False)
    # How hard both Claude readers think before answering (#1051), stated on every call so a run
    # never depends on a model's own default (Opus 5.5's is `medium`). Accuracy first: `high`.
    # Kept in step with `extraction/slot_reader/claude_output.py` `CLAUDE_EFFORTS`.
    claude_reader_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # The depth and sink reader (#1290): read by the worker from `GV_DEPTH_READER` when a set is
    # matched. The API reads the same switch to offer "Read depths again" (#1308) only when it is
    # on. Off by default; unset or empty is off, as the worker reads it.
    depth_reader: bool = False
    # The item pictures (#1322): the vendor's drawing of one countertop item, zoomed, with boxes
    # only on numbered lines (`workflow/item_pictures.py`). Built behind `GV_ITEM_PICTURES` and
    # shown to no reader yet. Off by default; unset or empty is off, as the worker reads it.
    item_pictures: bool = False
    # A re-run reuses a reader's stored answer to the identical question (same document version,
    # model, prompt id, pictures and packet) instead of asking again (#1112): $0 and repeatable.
    # Off for a proof run that must ask every question again.
    reader_reuse_answers: bool = True
    # Phase 3's try-again (#1309): the gates on which the reading asks both AIs again, up to three
    # smarter ways, before an item waits for a person. Comma-separated; EMPTY (the default) switches
    # nothing on, and the reading is byte for byte as before. Each gate is switched on only after
    # its Pariksha scorecard passes (Anant, 2026-10-14). Kept in step with
    # `extraction/slot_reader/try_again.py` `TRY_AGAIN_GATES`.
    try_again_gates: str = ""
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

    @field_validator("try_again_gates")
    @classmethod
    def _known_try_again_gates(cls, value: str) -> str:
        names = [name.strip() for name in value.split(",") if name.strip()]
        unknown = sorted(set(names) - TRY_AGAIN_GATE_NAMES)
        if unknown:
            raise ValueError(
                "GV_TRY_AGAIN_GATES names unknown gates: "
                + ", ".join(unknown)
                + "; known: "
                + ", ".join(sorted(TRY_AGAIN_GATE_NAMES))
            )
        return ",".join(sorted(set(names)))

    @field_validator("depth_reader", "item_pictures", mode="before")
    @classmethod
    def _switch_as_the_worker_reads_it(cls, value: object, info: ValidationInfo) -> object:
        """`GV_DEPTH_READER` and `GV_ITEM_PICTURES` read exactly as the worker reads them
        (`extraction.depth.read.depth_reader_enabled`, `workflow.item_pictures.
        item_pictures_enabled`, which this module may not import): unset or empty is off, and an
        unknown word is refused, never read as on."""
        if not isinstance(value, str):
            return value
        raw = value.strip().lower()
        if raw in ("", "0", "false", "off", "no"):
            return False
        if raw in ("1", "true", "on", "yes"):
            return True
        name = "GV_" + (info.field_name or "").upper()
        raise ValueError(f"{name} must be true or false, not {raw!r}")

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
        if self.claude_reader_enabled:
            key, name = (
                (self.openrouter_api_key, "OPENROUTER_API_KEY")
                if self.claude_reader_provider == "openrouter"
                else (self.anthropic_api_key, "ANTHROPIC_API_KEY")
            )
            if key is None or not key.get_secret_value().strip():
                raise ValueError(
                    f"GV_CLAUDE_READER_ENABLED with GV_CLAUDE_READER_PROVIDER="
                    f"{self.claude_reader_provider} requires {name}"
                )
        if self.ai_budget_maximum_usd < self.ai_budget_minimum_usd:
            raise ValueError("GV_AI_BUDGET_MAXIMUM_USD must not be below GV_AI_BUDGET_MINIMUM_USD")
        if (
            self.claude_reader_budget_usd is not None
            and self.claude_reader_budget_usd > self.ai_budget_maximum_usd
        ):
            raise ValueError(
                "GV_CLAUDE_READER_BUDGET_USD must not exceed the admin's maximum for one drawing "
                "set (GV_AI_BUDGET_MAXIMUM_USD); raise that instead"
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


#: The `GV_` families that only `Settings` reads: an unknown name in them is a typo or a stale line.
#: Other `GV_` names (`GV_DEV_*`, `GV_READER_*`, ...) are read by other code, so they are not judged.
_CHECKED_FAMILIES: Final = ("GV_ASSISTANT_", "GV_REVIEW_")


def unknown_settings(environ: Mapping[str, str]) -> list[str]:
    """The `GV_ASSISTANT_*` and `GV_REVIEW_*` environment variables that are not a setting.

    `extra="forbid"` refuses an unknown key in `.env` only: pydantic-settings never reads an
    unknown variable from the process's environment, so a typo in a start script (for example
    `GV_ASSISTANT_FULL_CHECK_CAPX_USD`) left the default in place without a word (harness audit
    2026-10-19). The app warns about these at start (`app.main.create_app`); it never refuses,
    so a stale line cannot stop a running deployment.
    """
    known = {name.upper() for name in Settings.model_fields}
    return sorted(
        name
        for name in environ
        if name.startswith(_CHECKED_FAMILIES) and name.removeprefix("GV_") not in known
    )
