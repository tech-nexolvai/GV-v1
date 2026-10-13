# Sutra: the one way every agent asks an AI

Written 2026-10-14 for #1221, phase 1 of the agent stack. This is the written contract. Only the
first step has been built: two callers that recorded nothing now record every call. No caller has
moved onto Sutra yet.

**What that step changes, precisely.** No reading, answer, abstention, hold, finding, verdict or
screen changes, and the AI reading budget is unchanged (layout and assignment rows are left out of
it, see below). What is new is rows in `model_invocations`: one per layout or assignment call. So
the Usage page's call, token and cost totals now include those calls, and the "fill with AI" request
commits its call's row (when nothing else is pending in its session).

Line numbers refer to the tree this document was merged with: main `8a6ac722` (after #1217 and
#1218) plus #1221.

## What Sutra is

Today nine places in the code ask a model something, and each one builds its own client, its own
retries and its own record of the call. Sutra is the single path they will all use instead. A
caller describes **what** to ask (a `Question`) and **how carefully** to ask it (a `Policy`). Sutra
then:

- sends the question;
- paces and retries it;
- checks the answer's shape;
- reserves and settles the spend;
- reuses a stored answer instead of paying twice;
- writes exactly one call record.

Sutra never decides anything about a drawing. It returns an answer, an abstention or a failure, and
the agent that asked decides what that means.

The slot reader's route already does nearly all of this. `run_jobs` (`workflow/slot_reader.py:1333`)
→ `reuse_stored_answers` (`workflow/reader_reuse.py:215`) → `read_crops_parallel`
(`extraction/slot_reader/bedrock.py:2021`) → `AttemptUsage` (`extraction/form_reader/bedrock.py:33`)
→ `CallLedger` (`workflow/reading_watch.py:144`). Sutra is that route made general.

## Where it lives

- **The call loop** goes in `extraction/models/`, beside `invocations.py`. That covers clients,
  pacing, retries, the re-ask, the shape check and the spend guard. `extraction/` may not import
  `app/` (`app/runs/invocations.py:3-8`, `docs/DESIGN_AI.md` §2), so this part never touches the
  database.
- **Persistence** goes in `workflow/`. That covers the recorder, stored answers and the spend meter,
  in `workflow/model_call_recorder.py`, `workflow/reading_watch.py` and `workflow/reader_reuse.py`.

## The interface

```python
@dataclass(frozen=True)
class Question:
    caller: Caller              # closed enum: slot_reader, form_reader, vision, layout, findings,
                                #   assignment, chat, assistant
    model_id: str               # exact id, version included
    prompt_id: str              # pinned, e.g. "layout-discriminator-v1"
    template_id: str            # the request shape the prompt is rendered into
    prompt_versions_accepted: frozenset[str]   # which stored answers may be reused
    request: ProviderNeutralRequest            # messages and pictures
    answer_schema: type[BaseModel]             # strict; also sent as the tool / JSON schema
    effort: Effort | None
    max_tokens: int
    question_packet: Mapping[str, object] | None  # document version, page, picture SHA-256s
    scope: ExtractionRun | PackageRevision        # exactly one (ADR-0019)
    key: str                    # the caller's own handle for the answer

@dataclass(frozen=True)
class Policy:
    route: Literal["bedrock", "openrouter", "anthropic"]
    timeout: Timeouts           # connect and read
    max_throttle_retries: int
    backoff_seconds: float
    malformed_reasks: int = 1   # one re-ask for a malformed answer, never a loop
    in_flight_retries: int      # HTTP 402 "in-flight budget" only (bedrock.py:2018)
    calls_per_minute: int
    max_concurrent: int
    on_failure: Literal["abstain", "fall_back", "fail_closed", "raise"]
    reuse: bool
    judges: Literal[1, 2]
```

**Budget.** There is one budget per reading, shared by every caller in it. It replaces three
separate pieces: `_SpendMeter` (`workflow/stages.py:767`), `BatchSpendGuard`
(`extraction/slot_reader/anthropic.py:117`) and the `ai_budget` settings.

```python
class Budget(Protocol):
    def reserve(self, model_id: str, est_input_tokens: int, output_limit: int) -> Reservation: ...
    def settle(self, reservation: Reservation, usage: Usage) -> None: ...
    def release(self, reservation: Reservation) -> None: ...   # the call was never sent
    @property
    def exhausted(self) -> bool: ...
    stop: ReadingStop   # tripped once: nothing more is sent, in-flight calls finish
```

**Recorder.** Every call, answered or not, becomes one `InvocationRecord`
(`extraction/models/invocations.py:212`). The record is turned into a row in one place,
`app/runs/invocations.py:167` `record`. Outcome, tokens, cost and the failure reason are worked out
in one place too, `BedrockConverseInvocationRecorder` (`app/runs/invocations.py:320`).

```python
class Recorder(Protocol):
    def record(self, attempt: Attempt) -> None: ...    # never changes the answer
    def snapshot(self) -> tuple[Attempt, ...]: ...
    def ledgered_in(self, session: Session) -> Mapping[int, UUID]: ...   # rows already committed
```

`Attempt` is today's `AttemptUsage` plus four fields: `failure_kind`, `reused_from`, `route` and
`cost_micros`.

**Stored answers (reuse, #1112).** `find_stored(questions)` and `replay(question, raw)`. A stored
answer matches on model id, prompt id and question identity (`workflow/reader_reuse.py:99`). A
reused answer is recorded with no tokens and no cost, and Usage leaves it out (`made_a_call`,
`app/models/runs.py:328`).

**Entry points.**

```python
def ask(question: Question, policy: Policy) -> Answer | Abstention: ...
def ask_many(questions: Sequence[Question], policy: Policy,
             on_answered: Callable[[Question], None] | None = None) -> dict[str, Answer | None]: ...
def ask_two(question: Question, judges: tuple[Policy, Policy],
            agree: Callable[[Answer, Answer], Agreement]) -> Agreed | Disagreed: ...
```

`ask_two` runs two independent judges, but **the `agree` function belongs to the agent**, never to
Sutra. Agreement on rows, walls and architect matches each means something different. For example,
row choice uses `_agreed_row` in `workflow/slot_reader.py`.

## Which caller has which feature today

Verified on `8a6ac722` plus #1221.

| Feature | Slot reader | Form reader | Vision / agent (Nova) | Layout | Findings | Assignment | Chat | Assistant |
|---|---|---|---|---|---|---|---|---|
| Provider | Bedrock, OpenRouter, Anthropic | Bedrock | Bedrock | Bedrock | Bedrock | Bedrock | Bedrock | OpenRouter |
| Pinned prompt id | yes | yes | yes | yes (`layout-discriminator-v1`) | yes (`findings-composer-v3`) | yes (`reading-assignment-v2`) | yes | yes |
| Strict answer shape | yes, one re-ask (`bedrock.py:874-944`) | partly | yes (forced tool) | yes (forced tool) | yes (forced tool) | yes (forced tool, enums) | yes | yes |
| Spend reserved before sending | yes (`BatchSpendGuard`) | counted after | checked before (`stages.py:1205`) | **no** | no | no | no | no (by decision, `app/config.py:83`) |
| Stored-answer reuse | yes | no | no | no | no | no | no | no |
| Question packet | yes | no | no | no | no | no | no | no |
| One row per call | yes (`CallLedger` + twins) | yes | yes (`_BufferedVisionRecorder`, `stages.py:825`) | **yes since #1221** | yes | **yes since #1221** | yes | yes |
| Failure reason on the row (#1215) | yes | yes | yes | **yes since #1221** | yes | **yes since #1221** | yes | yes |
| Retries | pacer + throttle back-off + 402 rules | pacer + back-off | own loop (`nova.py:1167`) | boto defaults | boto `total_max_attempts` + profile fallback | 1 attempt + one guarded retry | 1 attempt | none |
| Timeout | yes | yes | 10 s / 120 s (`nova.py:289-290`) | **boto default** | yes | yes | yes | 60 s |
| Two judgments | yes (two readers) | yes | yes | partly (one reader configured) | n/a (code guard) | n/a (`guard_assignment`) | n/a | n/a (guard) |
| On failure | abstain | abstain | abstain | abstain | fall back to template text | fail closed (reviewer fills) | error shown | records-only fallback |

## What #1221 built

The layout classifier and the reading assignment now write one `model_invocations` row per Bedrock
call. Each row holds the model, prompt and template ids, the tokens, the cost, the latency, the
outcome and the failure reason. Both callers send the same requests and get the same answers as
before.

- **The seam.**
  - The layout reader hands each call (its raw response or its error) to an `on_call` callback
    (`LayoutCall`, `extraction/layout.py:272`; `with_call_recorder`, `:306`), because it may not
    import `app/`.
  - The assignment model takes a recorder like the findings narration does
    (`with_invocation_recorder`, `workflow/assignment_bedrock.py:296`).
  - Both end in `workflow/model_call_recorder.py` `ModelCallRecorder`, and from there in
    `BedrockConverseInvocationRecorder`.
  - **Two steps.** While the model is asked, `record` only keeps the call in memory and cannot
    fail. `write_pending` writes the rows after the AI step has returned:
    - after `classify_layout`, `workflow/stages.py:4040`;
    - after `propose_and_guard`, `workflow/propose.py:382` and `app/api/measurements.py:1707`.

    This is what `_BufferedVisionRecorder` does for the vision readers. A recorder that raised
    inside the step would have been read as a failed call: the layout reader would abstain and the
    assignment would fail closed.
- **The outcome.**
  - `ok`: the call returned a well-formed answer. Code may still refuse that answer afterwards:
    `classify_layout`'s permitted-set and agreement checks, or the assignment's guard. Those
    refusals are decisions about the answer, not about the call.
  - `rejected`: the model answered (and was paid), but local code refused the answer. Examples are
    a malformed tool call or a refused layout region. The row keeps the code's reason.
  - `failed`: the call raised. The reason is the error class and code, e.g.
    `ClientError: ThrottlingException`.
  - `timeout`: the call timed out.
  - `refused`: the provider's content filter stopped the call.
  - So a well-formed assignment that the guard refuses is still an `ok` call. The retry it causes
    is a second call with its own row.
  - An interrupt (Ctrl-C, a shutdown) during the call still raises as before. The layout reader
    hands it over as the call's error, never as an `ok` call with no tokens. A call that ended with
    neither an answer nor an error writes no row and is logged.
- **The scope.**
  - A layout call belongs to the extraction run of the page being read (the text run that the
    layout crops are filed under).
  - An assignment call belongs to the package revision (ADR-0019), like the chat and the findings
    narration.
- **The cost.** It comes from the deployment's price file (`GV_MODEL_RATES_FILE`, `app/runs/rates.py`),
  or is unknown (`NULL`) when the model has no stated price. That is the same rule as every other
  Bedrock caller. A failed call with no tokens costs 0.
- **The transaction.**
  - The layout rows and the worker's assignment rows join the worker's own transaction, like the
    vision readers' and the findings narration's rows. They commit or roll back with the work they
    explain.
  - The API's assignment row (the "fill with AI" button, `app/api/measurements.py:1656`) is
    committed at once, like the reviewer chat's (#1134). The request's session never commits on its
    own, so otherwise a paid call would vanish from Usage.
  - If anything else is pending in that session (`new`, `dirty`, `deleted`), the recorder does not
    commit and logs that it did not. Work already flushed but not committed cannot be seen from
    there, so the API caller must have none (today it has none: it only reads before the call).
- **Recording never changes the answer, and never hides anyone else's error.**
  - `write_pending` first flushes the session outside any guard, so an error that belongs to other
    pending work raises as itself, exactly as without the recorder.
  - Each row is then written in its own savepoint. If the database refuses that row, only the
    savepoint is rolled back, the refusal is logged (`gv.workflow.model_call_recorder`) and the
    answer is kept.
- **Not reading spend.** Layout and assignment rows are excluded from the reading meter until
  migration step 3: `stage_spend_so_far` leaves out `UNMETERED_PROMPT_IDS` (`workflow/stages.py:976`,
  `:9656`). Without that, "Read the unread pages" (`:3018`) and a re-run of the reading (`:2204`)
  would rebuild the meter from committed layout rows. A priced row would shrink the readers' room,
  and an unpriced one would close it to $0 (`_remaining_claude_budget`).
- **Usage.** The new rows reach `/projects/{id}/usage` (`app/api/usage.py`): layout rows through
  their extraction run, assignment rows through their revision.

## The invariants (these do not move when callers move onto Sutra)

1. **AI never decides a verdict.**
   - Sutra returns answers; code decides PASS/FAIL with exact maths (`verdict/` cannot import
     models, `tests/test_verdict_isolation.py`).
   - A result that rests on one judgment becomes REVIEW (`AUTOMATIC_SOURCES`, `workflow/stages.py`).
2. **Each caller keeps its own failure meaning.**
   - **Readers abstain.** A failed or malformed call becomes "unread", with a reason.
   - **Findings and the assistant fall back** to deterministic text.
   - **Assignment fails closed.** The fields stay empty and a person fills them.
   - **An account-level 402 is never retried.** It stops the reading cleanly (`ReadingStop`). It is
     never retried by the engine either (`known_failure`, `workflow/review.py:620-640`). Only the
     per-call in-flight 402 has its own small one-at-a-time retry
     (`IN_FLIGHT_RETRIES`, `extraction/slot_reader/bedrock.py:2018`).
3. **Records are append-only.**
   - `model_invocations` refuses `UPDATE` and `DELETE` (`alembic/versions/0013_append_only.py`).
   - A wrongly shaped row can never be corrected, so a new record shape is tested before it ships.
4. **One row per call.** This includes failures, refusals and calls that were never sent (#1215). A
   reused answer has a row marked `reused_from` and is not counted as a call.

## The migration order

1. **Now (#1221):** the layout classifier and the assignment record every call. This is done.
2. **Next, one caller per PR, behaviour unchanged:** findings → reviewer chat → Nova vision and
   reading agent → review assistant. Each PR moves one caller onto `ask` and keeps its failure
   meaning. Each is checked against the caller's own tests plus a row-for-row comparison of its
   records.
3. **Then the shared budget:**
   - one `Budget` per reading;
   - every caller reserves before sending;
   - the layout classifier and the assignment count towards it (that step removes them from
     `UNMETERED_PROMPT_IDS`).

   This changes when a reading stops, so it gets its own decision and its own measurement.
4. **Later:** stored-answer reuse and question packets for callers other than the slot reader;
   `CallLedger` side writes widened to package-revision scope, so a worker that fails after a call
   keeps the row.

## The risks

- **Paying twice.** Reuse matches on model id, prompt id and question identity. Any change to the
  request shape, the prompt id or the packet keys while moving a caller turns into cache misses and
  repeated spend. Moving a caller must keep these exactly, or say plainly that the change is meant.
- **Append-only rows.** A wrongly shaped row cannot be fixed afterwards. Every new row shape gets a
  database test before merge.
- **Transaction boundaries.**
  - The ledger writes in its own side transactions on reader threads (`workflow/reading_watch.py:192`).
  - The chat, the assistant and the assignment's API path run inside API requests and commit at once.
  - The worker paths join the stage's transaction.

  Mixing these up either loses paid rows on a rollback or commits half-finished work. The API path
  must also stay light (`tests/api/test_no_heavy_work.py`): `workflow/model_call_recorder.py` imports
  no extraction code for that reason.
- **Doubled retries.** boto's own retries (the layout client uses boto's defaults today) multiply
  with an application retry loop if both stay on. Each row is one `converse` call, so boto's hidden
  inner retries are not visible as rows. Sutra's clients will set boto to one attempt and retry in
  one place.
- **Known gaps left by #1221, on purpose:**
  - Layout and assignment spend is real money that the reading budget does not see, as before
    #1221. Step 3 decides how it counts. (The layout reader is off unless a deployment sets
    `GV_BEDROCK_LAYOUT_ENABLED`.)
  - A worker that fails after the call loses that call's row, as the findings narration's row is
    lost today.
  - The layout client still has no explicit timeout.
