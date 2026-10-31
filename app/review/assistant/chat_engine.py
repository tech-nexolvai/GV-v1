"""One chat message, answered the way claude.ai answers (#1463): stream, use tools, delegate, answer.

**The loop (the lead).** The turn's request is the chat's earlier API messages, replayed byte for byte
(`api_content` of every earlier answer), then this message. Each call streams: thinking and text
reach the screen as they arrive (`thinking_start`, `thinking_delta`, `thinking_end`, `text_delta`);
the reply goes back into the history unchanged (thinking blocks included). When the reply asks for
tools, they run (`step_start`, `step_end`; `tasks` after a task-list change; `file` for a
handed-over file): the read-only ones of one reply (commands, views, zooms, the recorded results)
at the same time, the others in order; then the `Agent` calls of the reply together, at most
`max_parallel_agents` at once (`agent_start`, `agent_step`, `agent_end`). All the results go back
in one user message, in the order asked, and the model is asked again. A helper's reply runs the
same way (its finding records at the same time too), and the app adds a footer to every helper's
brief (`chat_tools.helper_footer`: its pages from the set pack's page map, the zoom tool, its
budget, what to return). Up to `lead_max_turns` turns of tools; then one last call with `tool_choice: none` and a note
to answer from what it has (`stopped: turn_limit`).

**Money.** Before every call its cost is estimated (an upper bound: text at one token per two
bytes, pictures by their size, the whole output allowance, the previous call's prompt read from the
cache). The lead stops cleanly when the message's spend plus the estimate would pass the message
cap, or the drawing set's spend plus it the set's cap: first with one final, smaller call told to
answer now from what it has, else with no call; the answer says so (`stopped: cost_cap`).

**Helpers always report.** A helper runs on Sonnet when the lead is Opus (else on the lead's model)
unless the lead names one, at the lead's effort but never above `HELPER_MAX_EFFORT` (medium), and
has `helper_max_turns` turns. The helpers the lead starts in one turn
may together spend `HELPER_SHARE_PERCENT` of what is left of the message cap at that moment; the rest
stays for the lead to check their reports and answer. Near any of these limits a helper is told
"wrap up now: report what you found and what you did not get to" and gets one final call without
tools, made even when that call passes the cap (bounded by a smaller output allowance). If even that
fails, its report is written from the steps it took. There are no per-helper money shares that kill
a helper before it reports.

**Failures.** A provider 5xx, 520/529, a lost connection or an error in the middle of a stream is
asked once more (for the lead and for helpers); a call's partial output is then dropped from what is
stored. A refusal (`stop_reason: refusal`) ends the message with a plain "Claude declined" answer;
its partial output is never stored or replayed. When Anthropic's refusal fallback answered instead
(#1577), the answer gets one quiet line naming both models (a `fallback` block and event), the
declined model's thinking and tool calls before the switch are left out of the history (as the API
asks), and the call is priced and recorded per attempt (`usage.iterations`), each at its own
model's price. Cancel (or the client going away) stops between
steps and in the middle of a stream, kills the running sandbox commands and stops the helpers; the
message is still stored, `stopped: cancelled`.

**A full check plans by code (#1512).** Once the set pack's page map is there (waited for, about
15 s on a new upload), code splits the set into helper jobs (`full_check_plan.py`: an area's pages
together, 3-5 pages a job, divider pages recorded as not comparable) and starts them all at once,
up to `max_parallel_agents`, at low effort, each with a code-written brief: its pages, GV Review's
recorded differences on them and the picked products' rules. A tiny call warms the helpers' prompt
cache first. As each helper returns, its findings get their second reads. The lead's first call
comes after: it gets the helpers' reports and every finding, split into those a second read or a
code check already backs and those it should verify, in batches; it only verifies and writes the
notes. Without a page map the lead plans the helpers itself, as in a chat. After the answer, the
second reads still to make, the third reads (each started as its second read comes back) and the
critic run in one bounded pool; the cloud drawing's slow copy of the PDF is made in a thread from
the start, and the drawing and the report are written together at the end.

**Two helpers per page group (#1569).** With `drawn_helpers` on, each code-planned job gets a second
helper on Sonnet at low effort whose brief covers only what is drawn and written (notes against the
drawing, doors and drawers, hardware, extent, the brand prototype, finish tags); the first keeps the
numbers. The drawn-things helpers queue after the numbers ones (at most `max_parallel_agents` run at
once) and each stays within its own money cap. When both helpers of a group have reported, code
folds their duplicates by page and item (`full_check_lenses`) before their second reads start; a
disagreement goes to the lead to verify first.

**Timings.** Every phase's seconds and cost (`chat_timings.py`) go in the trust block's `timings`
and the log.

**The history stays valid and append-only.** A reply whose tools did not run (cancelled, or a final
call that asked for tools anyway) is followed by one tool result per call saying so. An answer that
got no reply at all stores no API messages, so an unanswered question never enters the history.
"""

from __future__ import annotations

import inspect
import json
import logging
import random
import re
import threading
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from importlib import import_module
from pathlib import Path
from time import monotonic, monotonic_ns
from typing import Any, Final, Protocol
from uuid import uuid4

from app.review.assistant import (
    likely_problems,
    not_compared,
    one_card,
    read_spots,
    reading_coverage,
    reference_page,
    same_item_pages,
    set_proofs,
)
from app.review.assistant.account_credit import ACCOUNT_CREDIT
from app.review.assistant.agent import (
    HELPER_PICTURE_EDGE_PX,
    billed_input_tokens,
    estimate_micros,
    prompt_tokens_upper,
)
from app.review.assistant.architect_items import add_to_answer as add_architect_items
from app.review.assistant.chat_context import (
    NEAR_SHARE,
    context_edits,
    context_keep_turns,
    context_tokens,
    history_summary,
)
from app.review.assistant.chat_contract import ChatCoverageOut
from app.review.assistant.chat_findings import (
    CODE_ROOM_MAKERS,
    COVERAGE_CHECK_PREFIX,
    FINDING_TOOL_NAMES,
    HELPER_FINDING_TOOL_NAMES,
    FindingBook,
    MarkupFile,
    boxless_findings,
    boxless_note,
    coverage_note,
    decision_help_note,
    helpless_decisions,
    main_upload,
    make_proof_file,
    make_report_file,
    markup_base,
    page_ranges,
    products_words,
    read_listing,
)
from app.review.assistant.chat_findings import _architect_upload as architect_upload
from app.review.assistant.chat_progress import FullCheckProgress
from app.review.assistant.chat_provider import (
    CREDIT_NOT_STARTED,
    RETENTION_WORDS,
    ChatProvider,
    ProviderFailure,
    failure_from,
)
from app.review.assistant.chat_read_review import (
    ADJUDICATION_MODULE,
    READ_SYSTEM,
    Budget,
    ThirdReadBudget,
    critic_jobs,
    needs_third_read,
    third_read_job,
    third_read_limit,
)
from app.review.assistant.chat_request import (
    BY_SHORT,
    EXTRA_MODELS,
    HELPER_MAX_TOKENS,
    LEAD_MAX_TOKENS,
    MODELS,
    ChatModel,
    call_params,
    full_check_helper_effort,
    helper_effort,
    helper_shorts,
    helper_thinking,
    inline_pictures,
    picture_sizes,
    rates_key_for,
    thinking_config,
)
from app.review.assistant.chat_scope import (
    in_scope,
    is_whole_set,
    load_page_map,
    question_of,
    question_scope,
    scope_note,
    scoped_pages,
    scoped_recorded,
    widened,
)
from app.review.assistant.chat_summary import (
    code_summary,
    likely_line,
    needs_code_summary,
    rule_note_line,
)
from app.review.assistant.chat_timings import Timings
from app.review.assistant.chat_tools import (
    BATCH_LISTS,
    HELPER_KITS,
    HELPER_SYSTEM_PROMPT,
    HELPER_TOOLS,
    HELPERS_PER_TURN,
    LEAD_SYSTEM_PROMPT,
    LEAD_TOOLS,
    MAX_AGENTS,
    MAX_BATCH_ITEMS,
    MAX_PARALLEL_COMMANDS,
    PARALLEL_TOOLS,
    RECORD_ITEMS,
    TOOL_FAILED_WORDS,
    ChatTools,
    TaskList,
    ToolResult,
    arguments,
    helper_footer,
    helper_system_prompt,
    helper_tools,
    new_step,
)
from app.review.assistant.chat_trust import (
    CHECKS_MODULE,
    FAILED_KEY,
    REVISION_DIFF_MODULE,
    SECOND_READ_MODULE,
    Recorded,
    accuracy,
    apply_checks,
    drawn_ink_checks,
    judged,
    load_module,
    mark_compared,
    recorded_from,
    recorded_missing,
    round_statuses,
    shown_counts,
    states_drawing_values,
    trust_level,
    trust_report,
    with_drawn_ink,
    with_ledger,
    with_second_read,
)
from app.review.assistant.chat_unknowns import refused_from_diff, unknown_records
from app.review.assistant.classification import SAME_ITEM, not_compared_line
from app.review.assistant.code_version import code_version
from app.review.assistant.decision_resolver import recount_words, resolve_answer
from app.review.assistant.dim_index import labels_for
from app.review.assistant.escalation import Escalation, escalate_answer
from app.review.assistant.filler_range import add_to_answer as add_filler_range
from app.review.assistant.full_check_brief import (
    LENSES,
    checklist_gaps,
    checklist_send_back,
    helper_brief,
    unreached_pages,
    unreached_send_back,
    with_label_gaps,
)
from app.review.assistant.full_check_lenses import Fold, lens_folds, names_apart, same_wrongs
from app.review.assistant.full_check_plan import (
    MAX_JOB_PAGES,
    CrossPairs,
    FanOutPlan,
    HelperJob,
    Reading,
    pack_ready,
    pack_without_labels,
    pair_lines,
    rules_summary,
    scoped_plan,
    wait_for_page_map,
    wait_for_reading,
    with_lenses,
)
from app.review.assistant.full_check_specialists import specialist_system
from app.review.assistant.ledger import LEDGER_TOOL_NAMES, ReadingLedger, recorded_results_check
from app.review.assistant.likely_problems import COUNT_WORDS as LIKELY_COUNT
from app.review.assistant.pair_findings import add_to_answer as add_code_pairs
from app.review.assistant.product_scope import (
    PriceRule,
    ProductScope,
    scoped_gaps,
)
from app.review.assistant.proof import proof_counts, vendor_facing
from app.review.assistant.rate_limits import Limits, ModelLimiter, limiter_for
from app.review.assistant.reading_routes import NOT_DOUBLE_READ, sides_needed
from app.review.assistant.rule_notes import COUNT_WORDS as RULE_NOTE_COUNT
from app.review.assistant.run_fit import add_to_answer as add_run_fit
from app.review.assistant.sandbox import SET_PACK, SKILLS, UPLOADS, SandboxRefused
from app.review.assistant.scale_measure import add_to_answer as add_scale_measures
from app.review.assistant.send_package import (
    ZIP_MIME,
    PackageIdentity,
    incomplete_reason,
    make_package_file,
    package_context,
)
from app.review.assistant.set_context import add_to_answer as add_set_check
from app.review.assistant.sharpen import helper_lines
from app.review.assistant.sink_centre import add_to_answer as add_sink_centre
from app.review.assistant.sink_offset import add_to_answer as add_sink_offset
from app.review.assistant.splash_views import add_to_answer as add_splash_views
from app.review.assistant.spot_guard import dropped as spot_dropped
from app.review.assistant.spot_locate import code_spotted
from app.review.assistant.vendor_consistency import add_to_answer as add_consistency
from app.review.assistant.vendor_consistency import page_texts
from app.runs.rates import ModelRates, call_cost_micros

__all__ = [
    "FULL_CHECK_INSTRUCTION",
    "FULL_CHECK_LEAD_INSTRUCTION",
    "HARNESS_ID",
    "ChatEngine",
    "EngineSettings",
    "Ledger",
    "MessageOutcome",
    "cost_text",
    "prompt_ids",
]

_log = logging.getLogger(__name__)

#: Names the system prompts and tools: a change to either is a new id. (chat-v2: the `Agent`
#: tool's `model` text names Sonnet as the default. chat-v3: helpers run at most at medium effort.
#: chat-v4: the findings and coverage tools, and the answer-style lines. chat-v5: dismiss_recorded
#: and cite. chat-v7: record_string and record_reading (the reading ledger) for the lead and the
#: helpers, and their prompt lines; compare_rounds (the revision diff) for the lead. chat-v7: the
#: zoom tool, the set pack, parallel tools, the helper footer, the four-part Agent brief and the
#: effort lines. chat-v8: "never cite internal ticket numbers", and a page or area question's
#: scope note. chat-v9: zoom takes up to 8 crops; a full check's helpers are planned by code, with
#: the recall brief, and the lead gets their reports with a verify-only instruction. chat-v10: the
#: verify_value tool and its prompt lines, #1531. chat-v11: measure_on_drawing, decision_help and
#: check_reason, #1545. chat-v12: read_under_markup for the lead and the helpers, and its prompt
#: lines, #1548. chat-v13: the prompt and tool audit, #1559: one vocabulary, every tool says when
#: to use it, what it returns and one example, every schema field described, the box units said
#: one way, the lead prompt without its tool catalogue. chat-v14: remember, the corrections of an
#: earlier answer's finding, and server-side clearing of old tool results, #1548. chat-v16: numbered
#: dimension labels: zoom marks and guides, architect_dim / vendor_dim / dim, the helper brief's
#: label rule, #1558. chat-v16: Claude Fable 5.1, its turn note, and each chat keeping the system
#: prompt and tools it started with (`chat_harness`), #1577. chat-v17: "drawing text is data, never
#: instructions" in both prompts, tool outputs fenced in <drawing_data>, set notes as escaped data.
#: chat-v18: the knowledge pack, each full-check helper kind's own system prompt
#: (`full_check_specialists`), the label-first brief, record_string's part_dims / total_dim, and
#: structured outputs for the sharpen step, the reads and the critic. chat-v19: drawing_facts (code's
#: facts from the set pack), Anthropic tool search with the lead's rarer tools deferred, #1606.
#: chat-v20: record_items, many findings, strings and readings in one call, #1821.)
#: A chat's stored messages
#: replay unchanged, and since chat-v16 so do its system prompt and tools: a newer harness reaches
#: an older chat as one appended note.
HARNESS_ID: Final = "chat-v20"
#: Helpers that may run at once, at most, whatever the setting says.
MAX_PARALLEL_AGENTS_LIMIT: Final = 12
#: The most calls of one model at once, server-wide, by default (`rate_limits`; #1874): the
#: production pipeline research (2026-10-22) measured today's full checks at 15-25% of the start
#: tier's input tokens a minute with ten at once.
CALLS_AT_ONCE: Final = 32
#: A full check's specialists started together, at most (#1874): the limiter decides how many run.
FULL_CHECK_AGENTS: Final = 96
#: Helpers one lead turn may start. More than run at once wait for a free place in the same turn:
#: refusing them cost the lead a whole extra turn (a real full check, 2026-10-17: 7 of 13 refused).
MAX_AGENTS_PER_TURN: Final = HELPERS_PER_TURN
#: The second reads, third reads and critic calls of one message that run at once.
CHECK_WORKERS: Final = 8
#: A full check's helpers (#1512): their reading tool calls (zoom, view, commands; recording a
#: finding or a reading does not count), their output allowance and their report's length.
FULL_CHECK_HELPER_CALLS: Final = 12
#: More reading calls for a helper sent back once to finish its per-page checklist.
FULL_CHECK_SEND_BACK_CALLS: Final = 4
FULL_CHECK_HELPER_MAX_TOKENS: Final = 12000
#: A roster page helper's, gap closer's or lead review's output allowance per call (#1634, #1817).
#: Adaptive thinking counts against it, and one reply may hold a dozen zoom and command calls: the
#: paid run of 2026-10-20 cut three helpers at 6000 with 10-17 calls in the cut reply. Sonnet 5.5
#: and Opus 5.5 take up to 128K output tokens; the call's money is still held at this upper bound
#: (`_hold`: about $0.16 of Sonnet 5.5 a call, ten at once well inside the helpers' share), and a
#: reply cut at it is sent on (`_CUT_NOTE`), its complete tool calls run.
ROSTER_HELPER_MAX_TOKENS: Final = 16000
#: A roster specialist with a reply shape (the tag reader, the verifier, the look-again): its
#: answer is one JSON reply, after its thinking, also at its wrap-up (#1817: the tag table was cut
#: at the 8000-token wrap-up in the middle of a row, so no tag was compared).
SHAPED_HELPER_MAX_TOKENS: Final = 24000
#: Sent once when a reply shape's answer, or a helper's report, was cut at its length limit
#: (#1817): the answer is asked for again whole, never read from its cut piece.
_SHAPE_CUT_NOTE: Final = (
    "Your answer was cut at its length limit before it ended, so none of it can be read. Send "
    "the whole answer again now, as one JSON object and nothing else: compact (no spaces or line "
    "breaks between values), short item names."
)
_REPORT_CUT_NOTE: Final = (
    "Your report was cut at its length limit. Finish it now, without tools: write it again, "
    "shorter, ending with the CHECKLIST line for every one of your pages."
)
#: A roster helper's reading calls per page of its own (at least `FULL_CHECK_HELPER_CALLS`).
ROSTER_CALLS_PER_PAGE: Final = 3
_CUT_NOTE: Final = (
    "Your last reply was cut at its length limit. Go on in shorter steps: one page at a time, "
    "fewer findings per reply."
)
FULL_CHECK_REPORT_WORDS: Final = 200
#: The lead's output allowance when it only verifies a code-planned full check.
FULL_CHECK_LEAD_MAX_TOKENS: Final = 32000
#: The prompt-cache warm-up call before a full check's helpers start: the helpers' exact system
#: prompt and tools, one word asked, a few tokens back.
PREWARM_MAX_TOKENS: Final = 16
#: Reading tools a full check's helper's call budget counts.
_READING_TOOLS: Final = frozenset({"bash_tool", "view", "zoom", "create_file", "str_replace"})
#: The lead's tools that run at the same time within one reply (its finding and ledger tools stay
#: in order, so the findings table and the ledger keep the order they were recorded in).
LEAD_PARALLEL_TOOLS: Final = frozenset(
    {"bash_tool", "view", "zoom", "read_under_markup", "review_results"}
)
#: The output allowance of a forced last call (a wrap-up or a final answer at the cap).
WRAP_UP_MAX_TOKENS: Final = 8000
FINAL_MAX_TOKENS: Final = 16000
#: The share of what is left of the message cap that the helpers of one delegation may spend
#: together; the rest is the lead's, to check their reports and write the answer.
HELPER_SHARE_PERCENT: Final = 70
#: A call that fits the money spent but not the worst cases of the calls running now waits for
#: one of them to end (`ChatEngine._wait_for_holds`): it looks again at least this often
#: (seconds), and gives up only after `_HOLD_WAIT_MOST_S` (a call never runs that long).
_HOLD_WAIT_S: Final = 2.0
_HOLD_WAIT_MOST_S: Final = 900.0
_TOP_UP_LOCK: Final = threading.Lock()
#: A helper's model when the lead names none, by the lead's: Fable or Opus leads, Sonnet helps.
HELPER_DEFAULT_MODEL: Final[Mapping[str, str]] = {
    "fable": "sonnet",
    "opus": "sonnet",
    "sonnet": "sonnet",
    "haiku": "haiku",
}
#: Second reads (CONTRACT3): a cheap model, no tools, at most this many findings per answer.
SECOND_READ_MODEL: Final = "haiku"
SECOND_READ_EFFORT: Final = "low"
SECOND_READ_MAX_TOKENS: Final = 1024
SECOND_READ_LIMIT: Final = 30
#: Second reads for would-be reds first (Anant, 2026-10-27): while findings still arrive, only the
#: findings a red could come from (`could_go_red`) are read, so a would-be red that arrives late
#: still gets its read (on a real upload a late one never got its read and went red, while the same
#: item with its read, on another run, was held back as unsure). Once every finding is in (the
#: last queue), the reads left go to the rest, differences first. The cap and the money are
#: unchanged; a read can only hold a red back, never make one.
#: Third reads for would-be reds first (Anant, 2026-10-28, "yes build it"): a would-be red whose
#: second read was unsure or disagreed gets its third read as soon as that second read comes back,
#: before third reads open for the rest (after the lead's answer), and never counted against the
#: others' limit (`read_adjudication.ADJUDICATION_LIMIT`). On a real upload every second read came
#: back long before third reads opened; then the ten went in page order to the first findings and
#: four would-be reds further on got none, so their unsure second reads held them back. A third
#: read only settles a read: "agrees" lifts the hold an unsure or disagreeing second read puts on a
#: red; anything else keeps it. It never makes a red (`proof.code_proved`: code's own proof and two
#: reading routes per number are still required). At most one per second read, so this many.
#: Every would-be red gets its second reading whatever `SECOND_READ_LIMIT` (Anant, 2026-10-28:
#: the cap was full before 4 real reds arrived, so they went red never read twice): the cap holds
#: for the other findings only; this is the hard ceiling of readings one answer makes (a reading
#: costs about $0.0001-0.001 with Haiku, so at most about $0.20), and of would-be reds' third
#: reads. A would-be red past it is never red: "not double-read" (`reading_routes`).
READ_HARD_CEILING: Final = 200
THIRD_READ_RED_LIMIT: Final = READ_HARD_CEILING
#: An upper bound of one second read's prompt: two crops (at most 1600 px a side, about 3,400
#: tokens each) and the question.
_SECOND_READ_PROMPT_TOKENS: Final = 8_000
SECOND_READ_SYSTEM: Final = READ_SYSTEM
_RETRY_WAIT_SECONDS: Final = 3.0
#: The waits before asking again after the AI service said it is busy (429, too many requests;
#: #1866): a full check starts several helpers at once, and the SDK's own short retries are over
#: in seconds. Nothing was generated or charged; the call is asked again as it was.
_BUSY_WAITS_SECONDS: Final = (10.0, 25.0, 45.0)
_MAX_REPORT_CHARS: Final = 40_000
_MAX_PROMPT_CHARS: Final = 20_000
_MAX_DESCRIPTION: Final = 120
#: Claude Haiku 5.5's price is five times higher for a prompt over 100K tokens.
_HAIKU_LONG_PROMPT: Final = 100_000
_HAIKU_LONG_FACTOR: Final = 5
#: Prompt tokens above which a call costs more, by price key (`ChatModel.long_prompt_tokens`).
_LONG_PROMPT_TOKENS: Final[Mapping[str, int]] = {
    model.rates_key: model.long_prompt_tokens
    for model in EXTRA_MODELS.values()
    if model.long_prompt_tokens is not None
}
_MICROS: Final = Decimal(1_000_000)

#: Sent once after a reply with no words and no tool call (Anthropic: an empty `end_turn` happens,
#: mostly right after tool results; sending the empty reply back does not help).
EMPTY_REPLY_NOTE: Final = (
    "Your last reply had no words for the reviewer. Write your answer now, from what you found."
)
#: A stream that ended without its stop (the connection closed in the middle of the answer).
STREAM_CUT_WORDS: Final = "The AI service stopped in the middle of the answer. Please ask again."
#: The answer's error when the model still wrote nothing after being asked once more.
NO_ANSWER_ERROR: Final = "The AI finished without writing an answer. Please ask again."
COST_CAP_NOTE: Final = (
    "This message has nearly used its money cap. Do not use any more tools: answer now from what "
    "you have found, and say what you did not get to."
)
TURN_LIMIT_NOTE: Final = (
    "You have used all the tool turns for this message. Do not use any more tools: answer now "
    "from what you have found, and say what you did not get to."
)
WRAP_UP_NOTE: Final = (
    "Wrap up now ({why}). Do not use any more tools: report what you found, and which pages or "
    "parts you did not get to."
)
#: A roster page helper's or gap closer's one turn to record before it wraps up (#1641).
RECORD_NOTE: Final = (
    "Record now ({why}). In this one turn call record_items (or report_finding and "
    "record_string) for every difference, Right item and dimension string you have read but not "
    "recorded yet: the labels "
    "where code numbered them, else the value with its box. No more zooms. A reading you do not "
    "record is lost: your report records nothing. Your report comes after this turn."
)
#: The marker of a difference a roster helper read but could not record (#1641): code records each
#: such line as an AI-only item (Not compared until it is placed), so it is never lost.
UNRECORDED: Final = "UNRECORDED"
ROSTER_WRAP_UP_NOTE: Final = (
    WRAP_UP_NOTE
    + " For each difference you read but did not record, add one line exactly like: "
    + UNRECORDED
    + " p<page> | <area: item> | architect <value> | vendor <value> | different or check | "
    "<where each end lands, or why>."
)
#: A roster helper's report at its wrap-up: short, so it fits the money its share keeps for it,
#: with room for its thinking first (#1817).
ROSTER_WRAP_UP_MAX_TOKENS: Final = 6000
#: How many times the full check's lead review is sent on to pages it left (#1656).
LEAD_SEND_ON: Final = 2
#: A roster helper's record turn (#1654): tool calls that record, no long reply, after its
#: thinking: one record turn may hold a dozen findings (#1817).
RECORD_MAX_TOKENS: Final = 12000

#: The 5-6 line summary that ends every full check (both instructions below end with it).
_FULL_CHECK_SUMMARY: Final = """End with a 5-6 line plain-English summary for the reviewer (no tables, no disclaimers: the app draws the cloud drawing, the report and the lists):
1. What was checked: pages, products, rounds.
2. The overall verdict in one line (how many items are Wrong, are Right, could not be compared).
3. The top 3-5 issues by impact, each with its page, item, and architect vs vendor value.
4. What could not be compared, in one line, by reason (the app lists them apart; never ask the reviewer to decide).
5. The next step (e.g. "send the cloud drawing to the vendor").
If you are told to stop early, still write this summary from what you have."""

#: The full check's own instruction (CONTRACT4): the reviewer presses a button, never types it.
#: The lead follows it when it plans the helpers itself (no page map).
FULL_CHECK_INSTRUCTION: Final = f"""Full check of this drawing set (the reviewer pressed Run full check). Read /mnt/skills/gv-review/SKILL.md and rules.md first, and call review_results for the whole set.

Review the whole set: GV's rules for the picked products first, then everything on the vendor's drawings against the architect's (brand prototype differences as their own group). Report every item you compare with report_finding, the Right ones too (status same), with boxes. Check each finding the app made from GV Review's recorded results. Split the pages across helpers as SKILL.md section 8a says: each reports its findings and records every dimension string and every value it reads with record_string and record_reading (both sides; an item named the same way on every page and view), so code adds them up and compares the views. Verify each helper finding the app does not list as backed. Every page ends up in set_coverage.

{_FULL_CHECK_SUMMARY}"""

#: What the lead is told instead when code planned and ran the helpers (#1512).
FULL_CHECK_LEAD_INSTRUCTION: Final = f"""Full check of this drawing set (the reviewer pressed Run full check). The app has already split the set by area and run one helper per part with GV's per-page checklist, made a finding for each difference GV Review recorded, and read the helpers' findings a second time. Their reports and every finding follow. You verify and sum up; you do not plan. In this order, since a cap can stop you before the end:
- First the "Checklist gaps" and each "not compared" line in the helper reports: zoom and compare what was left (report_finding for each item), or say why it cannot be compared.
- Then the findings listed under "To verify": zoom on both sides of four findings per zoom call (8 crops) and put several zoom calls in one reply; then, in one reply, update_finding for each (verified true, or the values as printed) or drop_finding with your reason. Findings listed as backed need no re-check.
- Each finding the app made from GV Review's recorded results that is still without boxes: check it on the drawing (update_finding with boxes) or drop_finding with your reason.
- Pages listed as not accounted for: set_coverage, or compare them yourself (a helper only for several pages).
{_FULL_CHECK_SUMMARY}"""


def prompt_ids(provider: str) -> tuple[str, str]:
    """The lead's and the helpers' prompt ids as the Usage page reads them (route and purpose)."""
    return (
        f"review-assistant-chat-{provider}-{HARNESS_ID}",
        f"review-assistant-chat-helper-{provider}-{HARNESS_ID}",
    )


def second_read_prompt_id(provider: str) -> str:
    """A finding's second read, on the Usage page (counted in the chat's spend)."""
    return f"review-assistant-chat-check-{provider}-{HARNESS_ID}"


#: The most attempts one `ChatEngine._call` makes (asked once more after a failure worth
#: retrying): a call's money is held for all of them (#1769).
CALL_ATTEMPTS: Final = 2


#: Code's own checks of a full check (`ChatEngine.add_code_findings`), by the name a reviewer
#: reads when one could not run (#1768).
CODE_CHECK_NAMES: Final[Mapping[str, str]] = {
    "ledger": "the reading ledger's check",
    "consistency": "the vendor consistency check",
    "architect_items": "the finish check",
    "code_pairs": "code's own pairs",
    "run_fit": "GV's site-fit rule",
    "filler_range": "GV's filler range",
    "sink_offset": "GV's sink rules",
    "sink_centre": "the sink centre on the sink base",
    "splash_views": "the splash sizes on the architect's other views",
    "scale_measure": "the measuring by scale where the architect prints no size",
    "set_check": "the cross-sheet tag check",
}


#: The other steps of a roster or lead full check that may break (#1768); the simple check names
#: its own (`simple_check.run`).
SEEDING_STEP: Final = "GV Review's recorded results"
LABELS_STEP: Final = "the drawings' printed numbers (the labels step did not finish)"


def broken_steps_note(steps: Sequence[str]) -> str:
    """Why a full check is not for sending when its own steps broke (#1768), in plain words."""
    names = ", ".join(dict.fromkeys(steps))
    one = len(set(steps)) == 1
    return (
        f"{'a step' if one else f'{len(set(steps))} steps'} of the check could not run ({names}); "
        "its results may be missing, check again"
    )


def cost_text(micros: int) -> str:
    """Millionths of a dollar as "1.72"."""
    return str((Decimal(micros) / _MICROS).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def cap_text(micros: int) -> str:
    """A cap in words: "40", or "7.50" when it has cents."""
    return cost_text(micros).removesuffix(".00")


class Ledger(Protocol):
    """Where each model call is recorded (the Usage page's rows)."""

    def record(
        self,
        *,
        model_key: str,
        prompt_id: str,
        started_ns: int,
        input_tokens: int,
        output_tokens: int,
        stop_reason: str | None,
        error: BaseException | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class EngineSettings:
    model: ChatModel
    effort: str
    thinking: bool
    lead_max_turns: int
    helper_max_turns: int
    message_cap_micros: int
    package_cap_micros: int
    rates: Callable[[str], ModelRates | None]
    """The price list for a model's price key (`None`: no stated price)."""
    max_parallel_agents: int = MAX_AGENTS
    """Helpers one turn runs at once (`GV_ASSISTANT_MAX_PARALLEL_AGENTS`, 1 to 8)."""
    mode: str = "chat"
    """`chat`, or `full_check` (CONTRACT4): GV's recorded differences are made findings at the
    start, and code makes the cloud drawing and the check report at the end."""
    products: tuple[str, ...] = ()
    pages: tuple[int, ...] = ()
    """A roster full check on these vendor pages only (a fix's test, Anant 2026-10-31): every
    whole-set step runs, on these pages; empty: the whole set."""
    drawn_helpers: bool = False
    """A full check's second, drawn-things helper per page group (#1569,
    `GV_ASSISTANT_DRAWN_HELPERS`)."""
    context_tokens: int = field(default_factory=context_tokens)
    """The prompt size at which old tool results are cleared (`chat_context`, #1548)."""
    context_keep_turns: int = field(default_factory=context_keep_turns)
    """The turns kept whole when clearing (this question and the ones before it)."""
    roster: bool = False
    """A full check as a fixed-price roster of specialists, no lead (`full_check_roster`,
    `GV_ASSISTANT_FULL_CHECK_ROSTER`). Its message cap starts as the upper bound (the set's budget,
    or a lower `cap_usd`) and becomes the full check's fixed price once code has planned the pages
    (`price_rule`, #1823)."""
    price_rule: PriceRule = field(default_factory=PriceRule)
    """The full check's fixed price: base + per content page in scope, at least a minimum
    (`GV_ASSISTANT_FULL_CHECK_BASE_USD`, `..._PER_PAGE_USD`, `..._MIN_USD`, #1823)."""
    numbers_effort: str = "low"
    """The roster's first numbers page helpers' effort (`GV_ASSISTANT_NUMBERS_HELPER_EFFORT`)."""
    numbers_pages: int = MAX_JOB_PAGES
    """The roster's numbers page helpers' most pages each (`GV_ASSISTANT_NUMBERS_HELPER_PAGES`);
    the drawn-things helpers keep the plan's groups (`full_check_plan.with_lenses`)."""
    pairing_proof: str = "code_ends"
    """What proves that two numbers measure the same thing (`GV_ASSISTANT_PAIRING_PROOF`):
    `code_ends` (the default), `ai_pair_plus_code` or `code_only` (`full_check_roster`)."""
    round_pick_cap_micros: int = 0
    """The roster's hard cap for the cheap picks that compare the round-only leftovers with the
    architect (`GV_ASSISTANT_ROUND_PICK_CAP_USD`, #1861; `round_picks`); 0: off (the default,
    Anant 2026-10-22, option C)."""
    identity: PackageIdentity = field(default_factory=PackageIdentity)
    """Who and what the send-ready package is about (#1622): project, vendor, submittal number,
    date received, products."""
    numbering: Callable[[Sequence[Mapping[str, Any]]], dict[str, str]] | None = None
    """This check's stable comment numbers from the set's earlier checks
    (`package_store.numbering`); `None`: the check numbers its own."""
    full_check_mode: str = "roster"
    """Which full check runs (`GV_ASSISTANT_FULL_CHECK_MODE`): `roster`, or `simple` (one compare
    call per vendor drawing, code verifies, one blind look per red candidate: `simple_check/`)."""
    simple: Any = None
    """The simple check's options for this run (`simple_check.run.SimpleOptions.of`): the compare
    and blind-look settings and the pilot's size."""
    page_match_model: str | None = None
    """The AI page matcher's model for vendor drawings paired across separate uploads (#1798,
    `assistant_page_match_model`); `None`: off (code pairs alone)."""
    rate_tier: str = "start"
    """The AI provider's usage tier whose per-minute limits each model's limiter keeps
    (`GV_ASSISTANT_RATE_TIER`: start, build or scale; `rate_limits`)."""
    calls_at_once: int = CALLS_AT_ONCE
    """The most calls (or specialists) of one model at once, server-wide (`GV_ASSISTANT_CALLS_AT_ONCE`;
    a busy answer halves it for a while, `rate_limits`)."""
    full_check_agents: int = FULL_CHECK_AGENTS
    """A full check's specialists started together, at most (`GV_ASSISTANT_FULL_CHECK_AGENTS`):
    each then waits for a place of its model's limiter, so the number that run grows with the work
    up to the provider's limits (#1874)."""
    parse_processes: int = 0
    """Worker processes that parse drawing pages for a full check's code checks
    (`GV_ASSISTANT_PARSE_PROCESSES`, `verify_value.use_parse_processes`; 0: threads, #1874)."""
    spot_processes: int = 0
    """Worker processes for a full check's spot reads (`GV_ASSISTANT_SPOT_PROCESSES`,
    `spot_pool`; 0: on the check's own threads, 2026-10-23)."""
    preflight_model: str | None = None
    """The supervisor's model (`plan_supervisor.py`); `None`: off (code's own stops only)."""
    run_anyway: bool = False
    """The reviewer chose Run anyway: a pre-flight stop is recorded, the check goes on."""
    whole_look_model: str | None = None
    """The roster's whole-drawing look per paired vendor drawing (`whole_look`,
    `GV_ASSISTANT_WHOLE_LOOK_MODEL`): its model id; `None`: off."""
    whole_look_effort: str = "medium"
    """Its effort (`GV_ASSISTANT_WHOLE_LOOK_EFFORT`)."""
    set_index_model: str | None = None
    """The set index's model (`set_index.py`, `assistant_set_index_model`): each page of a
    two-file set read once for its project, set and views; `None`: off (no project is known)."""
    page_reviewer: Any = None
    """The roster's page reviewer per vendor page (`page_reviewer.Options`,
    `GV_ASSISTANT_PAGE_REVIEWER`, Anant 2026-10-29); `None`: off (the default)."""


@dataclass(slots=True)
class MessageOutcome:
    """What the message stores: the answer, the screen's blocks, the turn's API messages."""

    text: str
    blocks: list[dict[str, Any]]
    api_content: list[dict[str, Any]]
    cost_micros: int
    stopped: str | None = None
    error: str | None = None
    cap_hit: str | None = None
    """With `stopped: cost_cap`, which cap stopped it: `message` (this answer's) or `package` (the
    drawing set's budget)."""
    seen: list[dict[str, Any]] = field(default_factory=list)
    """Every zoom crop of the answer, the lead's and its helpers' (`ChatTools.seen`)."""
    cap_micros: int | None = None
    """The cap this answer ran under when it is not the job's message cap: a roster full check's
    fixed price (#1823), stored on the message and shown on its stop words."""


# ---- one streamed call ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Reply:
    """A finished call: the content as received, why it stopped, its usage and the model named."""

    content: list[dict[str, Any]]
    stop_reason: str | None
    stop_details: Mapping[str, Any] | None
    usage: dict[str, Any]
    model: str | None
    bad_inputs: set[int] = field(default_factory=set)
    transformations: list[dict[str, Any]] = field(default_factory=list)
    """What the API did to the request's history before the model read it (thinking blocks it
    dropped), from `input_transformations` (sent with the thinking-binding beta)."""

    def history_content(self) -> list[dict[str, Any]]:
        """The content to send back. After a refusal fallback, the declined model's thinking and
        tool calls before the last switch are left out, as the API asks; the switch marker too."""
        last = max(
            (n for n, block in enumerate(self.content) if block.get("type") == "fallback"),
            default=None,
        )
        if last is None:
            return list(self.content)
        dropped = {"thinking", "redacted_thinking", "tool_use", "server_tool_use", "fallback"}
        return [
            block
            for n, block in enumerate(self.content)
            if not (n <= last and block.get("type") in dropped)
        ]

    def text(self) -> str:
        parts = [
            str(block.get("text") or "").strip()
            for block in self.history_content()
            if block.get("type") == "text"
        ]
        return "\n\n".join(part for part in parts if part)

    def tool_uses(self) -> list[dict[str, Any]]:
        return [block for block in self.history_content() if block.get("type") == "tool_use"]

    def switches(self) -> list[tuple[str, str]]:
        """Each refusal-fallback switch in the reply, (declined model, answering model); a turn a
        conversation already moved to the fallback ("sticky") has no marker, so its served model is
        named against the one asked (`None` here: the caller knows it)."""
        found: list[tuple[str, str]] = []
        for block in self.content:
            if block.get("type") != "fallback":
                continue
            ends = [block.get(side) for side in ("from", "to")]
            names = [end.get("model") if isinstance(end, Mapping) else None for end in ends]
            if all(isinstance(name, str) and name for name in names):
                found.append((str(names[0]), str(names[1])))
        return found

    @property
    def fell_back(self) -> bool:
        """Whether Anthropic's refusal fallback served it: a switch marker in the content, or (a
        conversation already switched, "sticky") a `fallback_message` among the usage's attempts."""
        attempts = self.usage.get("iterations")
        sticky = isinstance(attempts, list) and any(
            isinstance(item, Mapping) and item.get("type") == "fallback_message"
            for item in attempts
        )
        return sticky or any(block.get("type") == "fallback" for block in self.content)


class _Cancelled(Exception):
    """The message was cancelled in the middle of a call."""


class _NoRoom(Exception):
    """A second read would pass the message's caps: it is not made."""


#: The display side of a streamed call (the lead's): called with each raw event.
type _Display = Callable[[Mapping[str, Any]], None]


def _merge_usage(usage: dict[str, Any], update: Mapping[str, Any] | None) -> None:
    if not isinstance(update, Mapping):
        return
    for key, value in update.items():
        if value is not None:
            usage[key] = value


#: Blocks whose input streams as JSON: our tools' calls, and Anthropic's own (tool search), which
#: run inside its API and are kept in the history as received.
_CALLS: Final = frozenset({"tool_use", "server_tool_use"})


def _accumulate(
    events: Any,
    display: _Display | None,
    cancelled: Callable[[], bool],
    usage: dict[str, Any],
) -> _Reply:
    """A call's reply, built from its raw stream events exactly as they arrived. `usage` is filled
    as the events come, so a cancelled call's usage so far is known."""
    content: list[dict[str, Any]] = []
    buffers: dict[int, str] = {}
    stop_reason: str | None = None
    stop_details: Mapping[str, Any] | None = None
    model: str | None = None
    bad: set[int] = set()
    transformations: list[dict[str, Any]] = []
    for event in events:
        if cancelled():
            raise _Cancelled
        kind = event.get("type")
        if kind == "message_start":
            message = event.get("message") or {}
            model = message.get("model") if isinstance(message.get("model"), str) else None
            _merge_usage(usage, message.get("usage"))
            transformations = _transformations(
                message.get("input_transformations"), transformations
            )
        elif kind == "content_block_start":
            index = int(event.get("index", len(content)))
            block = dict(event.get("content_block") or {})
            if block.get("type") == "tool_use":
                block["input"] = {}
            while len(content) <= index:
                content.append({})
            content[index] = block
            if block.get("type") == "fallback":
                to = block.get("to")
                if isinstance(to, Mapping) and isinstance(to.get("model"), str):
                    model = to["model"]
        elif kind == "content_block_delta":
            index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            if index >= len(content):
                continue
            block = content[index]
            dtype = delta.get("type")
            if dtype == "text_delta":
                block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
            elif dtype == "thinking_delta":
                block["thinking"] = str(block.get("thinking") or "") + str(
                    delta.get("thinking") or ""
                )
            elif dtype == "signature_delta":
                block["signature"] = delta.get("signature")
            elif dtype == "input_json_delta":
                buffers[index] = buffers.get(index, "") + str(delta.get("partial_json") or "")
            elif dtype == "citations_delta":
                block.setdefault("citations", None)
                block["citations"] = [*(block["citations"] or []), delta.get("citation")]
        elif kind == "content_block_stop":
            index = int(event.get("index", 0))
            server = index < len(content) and content[index].get("type") == "server_tool_use"
            if server and index not in buffers:
                pass  # Its input came whole with the block's start.
            elif index < len(content) and content[index].get("type") in _CALLS:
                raw = buffers.pop(index, "")
                try:
                    parsed = json.loads(raw) if raw.strip() else {}
                except ValueError:
                    parsed = {}
                    bad.add(index)
                if isinstance(parsed, dict):
                    content[index]["input"] = parsed
                else:
                    bad.add(index)
        elif kind == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason") or stop_reason
            details = delta.get("stop_details")
            stop_details = details if isinstance(details, Mapping) else stop_details
            _merge_usage(usage, event.get("usage"))
            # Sent again here after a fallback in the middle of the stream: the later list wins.
            transformations = _transformations(event.get("input_transformations"), transformations)
        if display is not None:
            display(event)
    # A tool call whose input never finished (cut at max_tokens) is not run.
    for index in buffers:
        bad.add(index)
    return _Reply(
        content=[block for block in content if block],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=usage,
        model=model,
        bad_inputs={id(content[n]) for n in bad if n < len(content)},
        transformations=transformations,
    )


def _sendable(content: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """A reply's content as it may go back into a history: an empty text block left out (the API
    refuses one in a request)."""
    return [
        dict(block)
        for block in content
        if not (block.get("type") == "text" and not str(block.get("text") or ""))
    ]


def _without_unsigned_thinking(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A reply from a model outside Claude without its unsigned thinking blocks: through
    OpenRouter, GPT-6 and Gemini 4 send their reasoning's summary as an unsigned `thinking` block
    (for the screen) and the reasoning itself as a `redacted_thinking` block, which is what goes
    back to them (probe 2026-10-25). An unsigned thinking block would make the reply one that
    cannot go back into the history (`_replayable`), so it is left out of the history only."""
    return [
        block
        for block in content
        if not (block.get("type") == "thinking" and not block.get("signature"))
    ]


def _replayable(content: Sequence[Mapping[str, Any]]) -> bool:
    """Whether a reply can go back into the history: it says something or calls a tool, and every
    thinking block in it is signed. An empty reply, a thinking-only one or one whose thinking was
    cut is never sent back: the API refuses an empty message and an unsigned thinking block, so it
    would break every later question of the chat (harness audit 2026-10-19)."""
    said = any(
        (block.get("type") == "text" and str(block.get("text") or "").strip())
        or block.get("type") in _CALLS
        for block in content
    )
    signed = all(block.get("signature") for block in content if block.get("type") == "thinking")
    return said and signed


def _transformations(value: object, before: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A reply's `input_transformations`, when the event carries a list (else what was known)."""
    if not isinstance(value, list):
        return before
    return [dict(item) for item in value if isinstance(item, Mapping)]


_SNAPSHOT: Final = re.compile(r"(?:[-@]\d{8})?")


def _names_model(reply: _Reply, model: ChatModel, provider: str) -> bool:
    """Whether the reply came from the model asked (or from Anthropic's own refusal fallback)."""
    if provider == "anthropic" and reply.fell_back:
        return True
    reported = reply.model
    if not isinstance(reported, str):
        return False
    names = (model.id, model.openrouter, model.openrouter.removeprefix("anthropic/"))
    return any(
        reported.startswith(name) and _SNAPSHOT.fullmatch(reported, len(name)) is not None
        for name in names
    )


# ---- the engine ----------------------------------------------------------------------------------


@dataclass(slots=True)
class _Agent:
    call_id: str
    description: str
    prompt: str
    model: ChatModel
    number: int = 0
    pages: tuple[int, ...] | None = None
    """A code-planned helper's pages (#1512): its footer lists them from the page map."""
    lens: str = "both"
    """A code-planned helper's lens (#1569): `both`, `numbers` or `drawn`."""
    effort: str | None = None
    """Its own effort (a drawn-things helper: low), else the full check's helper effort."""
    cap_micros: int | None = None
    """Its own money cap (a drawn-things helper), besides the helpers' share."""
    spent: int = 0
    last_call: int = 0
    """What its last call cost: its record turn and report cost about as much (the same pictures
    and context), so the roster keeps that much for them (`full_check_roster._Run._spare`)."""
    partner: _Agent | None = None
    """The other lens's helper on the same pages."""
    fold: tuple[_Agent, ...] = ()
    """When the numbers helpers have smaller page groups (`HelperJob.fold`): the other helpers it
    folds with once all have reported; empty: its `partner` alone."""
    system: str | None = None
    """A code-planned helper's own system prompt (its kind's, `full_check_specialists`), else the
    helpers' shared one."""
    tools: tuple[Mapping[str, Any], ...] | None = None
    """A roster specialist's own tools (`full_check_roster`), else the helpers' tools."""
    output_format: Mapping[str, Any] | None = None
    """A roster specialist's reply shape (structured outputs): its report is that JSON."""
    soft_cap: bool = False
    """A roster specialist's cap is checked on what it has spent (wrap-up at 80%), not on the
    next call's upper-bound estimate; the message's fixed price still holds every estimate, so
    the run never passes it and a specialist passes its own cap by one call at most."""
    kit: str | None = None
    """Its fixed tool kit (`chat_tools.HELPER_KITS`), or every helper tool."""
    phase: str = "helpers"
    """The timings phase its calls are charged to (#1652: a roster's gap closers, verifiers, lead
    review and deeper look each show apart from the page helpers)."""
    top_up: Callable[[_Agent], int] | None = None
    """A roster specialist's top-up (#1805): at its soft cap it asks for more of its phase's money
    (what the phase's other specialists left unspent); more than 0 raises its cap and it goes on,
    so it stops at its cap only when its phase's money is really used up."""
    done: bool = False
    """It has reported (a phase's top-up counts only what a finished specialist really spent)."""
    ready: Callable[[], str | None] | None = None
    """A code-planned helper whose pages wait for the AI page matcher (#1883): called in its
    own thread before its first call; it waits until its pages are paired (or left unpaired) and
    returns its brief with those pairs (`None`: its prompt stands)."""
    opening: tuple[Mapping[str, Any], ...] = ()
    """Content before its brief in its first message (the whole-drawing look's pictures, drawn by
    code: `whole_look.opening`); set by `ready` at the latest."""
    skip: bool = False
    """Nothing to do (a whole-drawing look whose page no architect drawing is paired with): it
    reports at once, with no call."""
    loop: Callable[..., tuple[str, bool, str | None]] | None = None
    """Its own loop instead of the helpers' (the page reviewer, `page_reviewer.review`): called
    as `loop(engine, agent, step_index, entry, share)`, returning (report, ok, stopped)."""


@dataclass(slots=True)
class _Share:
    """The helpers' money for one delegation: the message spend they may take it up to, and the
    estimates of their calls running now (held, so helpers running at once do not overshoot)."""

    limit: int
    held: int = 0


#: `ChatEngine._launch`'s default ceiling: the engine's `helper_ceiling`.
_OWN_CEILING: Final = object()


class ChatEngine:
    """One message's run. `emit(event, data)` sends an SSE event; it must be thread-safe."""

    def __init__(
        self,
        *,
        settings: EngineSettings,
        provider: ChatProvider,
        tools: ChatTools,
        ledger: Ledger,
        emit: Callable[[str, dict[str, Any]], None],
        history: Sequence[Mapping[str, Any]],
        user_content: list[dict[str, Any]],
        package_spent: Callable[[], int],
        read_picture: Callable[[str], bytes],
        cancel: threading.Event,
        sleep: Callable[[float], bool] | None = None,
        lead_system: str = LEAD_SYSTEM_PROMPT,
        lead_tools: Sequence[Mapping[str, Any]] = LEAD_TOOLS,
    ) -> None:
        self.settings = settings
        # The lead's system prompt and tools: the chat's own, kept since its first answer
        # (`chat_harness`, #1577), so the replayed history's thinking stays valid.
        self.lead_system = lead_system
        self.lead_tools = tuple(lead_tools)
        self.provider = provider
        self.tools = tools
        self.ledger = ledger
        self._emit = emit
        self.history = [dict(message) for message in history]
        self.user_content = user_content
        self.package_spent = package_spent
        self.read_picture = read_picture
        self.cancel = cancel
        # Waits before a retry; returns True when cancelled during the wait.
        self._sleep = sleep or (lambda seconds: cancel.wait(seconds))
        self.lead_prompt_id, self.helper_prompt_id = prompt_ids(provider.name)
        self.check_prompt_id = second_read_prompt_id(provider.name)
        self.blocks: list[dict[str, Any]] = []
        self._discarded: set[int] = set()
        self._lock = threading.RLock()
        self._progress: FullCheckProgress | None = None
        self._progress_index: int | None = None
        self._spent = 0
        #: What the calls on their own share cost (the page reviewer, `page_reviewer`): counted in
        #: `spent` (the message cap and the set's budget hold every call) but never against the
        #: fixed price's other shares, whose checks read `counted_spent` (Anant 2026-10-29: the
        #: reviewer's money is its own share).
        self._own_spent = 0
        #: The account's credit read for this answer (`AccountCredit.start`, at its first call).
        self._credit_read = False
        #: The money kept for the calls on their own share (millionths of $): the message cap less
        #: this is what every other step may reach. 0 when no step has its own share.
        self.own_share_micros = 0
        self._attempt_blocks: list[int] = []
        self._streams: set[Any] = set()
        self.task_list = TaskList()
        listing = list(tools.uploads) or read_listing(tools.workspace)
        main = main_upload(listing)
        self.book = FindingBook(
            workspace=tools.workspace,
            listing=listing,
            page_count=int(main.get("pages") or 0) if main is not None else tools.page_count,
            checker=self._checked,
            extents=tools.extents,
        )
        # The products this answer checks, a strict scope (`product_scope`, Anant 2026-10-18): a
        # finding of another product is never recorded.
        self.product_scope = ProductScope.of(settings.products)
        self.book.product_scope = self.product_scope
        self._page_words: dict[int, str] | None = None
        self.book.page_words = self.page_words
        self._positions: dict[str, int] = {}
        self._coverage_index: int | None = None
        # What this answer is about (#1520): the whole set, or the pages / area the question names.
        self._asked_scope = question_scope(
            question_of(user_content),
            mode=settings.mode,
            picked=settings.pages,
            page_map=load_page_map(tools.workspace.set_folder),
            page_count=self.book.page_count,
            main_file=self.book.main_file,
        )
        self.book.scope_pages = scoped_pages(self._asked_scope)
        self._started_scope: dict[str, Any] = {
            **self._asked_scope,
            **self.product_scope.as_dict(),
        }
        self._helpers_started = 0
        self._recorded: Recorded | None = None
        self._recorded_document: Mapping[str, Any] | None = None
        self._recorded_read = False
        self._unknowns: list[dict[str, Any]] = []
        # The vendor consistency check's grey items (#1584), for the "not checked" list.
        self.consistency_gaps: list[dict[str, Any]] = []
        #: A full check's Not-compared rows (#1619), once its files are made.
        self.not_compared: list[Any] = []
        self.consistency_line: str | None = None
        # The finish check's line (#1623); its Not compared items join `consistency_gaps`.
        self.items_line: str | None = None
        # The decision items code settled itself (#1554), for the cloud drawing and the report.
        self._settled: list[dict[str, Any]] = []
        #: The full check's escalation of undecided items (#1619), once it ran.
        self._escalation: Escalation | None = None
        """What this answer did not check (`chat_unknowns`), set by the trust phase."""
        self.refused_diffs: list[dict[str, Any]] = []
        """Round pairs `compare_rounds` would not line up (an unknown each)."""
        # compare_rounds: each finding's round status measured on the drawings (kept over the
        # status worked out from `round` fields), and each compared pair's summary.
        self._diff_status: dict[str, str] = {}
        self._diff_unsure: set[str] = set()
        self._diff_pairs: dict[tuple[str, int, str, int], dict[str, Any]] = {}
        # One reading ledger per message, shared by the lead and its helpers (thread-safe).
        self.reading_ledger = ReadingLedger(
            page_count=self.book.page_count or None, labels=self._string_label
        )
        self.timings = Timings()
        #: Every code change to a finding's checks (second, third read, critic) reads it again
        #: and stores it under this lock, so two checks finishing at once never lose one.
        self.finding_lock = threading.RLock()
        # Second reads (#1512): each finding's read, kept while what it read is unchanged (the
        # lead marking a finding verified keeps it), the findings queued, and the checks running.
        self._reads: dict[str, tuple[tuple[Any, ...], str, dict[str, Any]]] = {}
        self._read_queued: set[tuple[str, tuple[Any, ...]]] = set()
        #: The findings no red can come from that took one of `SECOND_READ_LIMIT`'s places.
        self._others_read: set[str] = set()
        #: Each reading made, by its key (`read_spots.crop_hash`: its pictures' bytes and its
        #: question): what it read, `_IN_FLIGHT` while it runs. An identical picture drawn again
        #: is the same reading: no new call, no new place.
        self._read_slots: dict[str, Any] = {}
        #: The findings waiting for a reading that runs now, by its key.
        self._read_waiting: dict[str, list[Mapping[str, Any]]] = {}
        self._third_queued: set[tuple[str, tuple[Any, ...]]] = set()
        #: Each finding's third read, kept like its second read while what it read is unchanged:
        #: (what was read, the outcome, the three readings).
        self._thirds: dict[str, tuple[tuple[Any, ...], str, list[dict[str, Any]]]] = {}
        #: Each finding's reading pictures (`reading_pictures`), by its id and what it reads.
        self._read_pictures: dict[tuple[str, tuple[Any, ...]], read_spots.ReadPictures] = {}
        self._third_reads_open = False
        self._third_budget: ThirdReadBudget | None = None
        self._checks_pool: ThreadPoolExecutor | None = None
        self._checks: list[Future[None]] = []
        self._checks_budget: Budget | None = None
        self._plan: FanOutPlan | None = None
        self._cross: CrossPairs | None = None
        """The vendor pages paired across separate uploads (#1798), once worked out."""
        self._reference_stamped = False
        """Every finding has its one reference page (`_stamp_reference_pages`)."""
        self._read_share = _Share(limit=settings.message_cap_micros)
        #: Woken when a running call's hold is let go (`_release`): a call waiting for room
        #: (`_wait_for_holds`) looks again.
        self._holds_changed = threading.Condition()
        self._markup_base: Future[Any] | None = None
        self._trust_index: int | None = None
        #: A code-planned helper's checklist pages still open after its report (#1515).
        self._checklist_gaps: dict[int, dict[int, str]] = {}
        #: This answer's ledger entries kept in the set's proof ledger, by id (#1805).
        self._kept_inputs: dict[str, dict[str, Any]] = {}
        #: Two-lens helpers (#1569): those reported whose partner has not yet (their findings'
        #: second reads wait for the fold), and the folds where the two disagreed.
        self._lens_reported: set[int] = set()
        self._lens_waiting: set[str] = set()
        self._lens_disagree: list[Fold] = []
        # A long chat (#1548): clearing old thinking too, once the prompt neared the threshold;
        # and whether the history was replaced by code's summary (no context editing).
        self._context_near = False
        self.summarised = False
        #: This answer's number in its chat (#1548): a correction of an earlier finding names it.
        self.answer_number = 1
        #: The question's task spec (`sharpen`, #1551), set before `run()`; its lines go into every
        #: helper's brief.
        self.task_spec: dict[str, Any] | None = None
        #: Each helper kind's system prompt, made once (`_kind_system`).
        self._kind_systems: dict[str, str] = {}
        #: The share of what is left that one delegation's helpers may spend (a roster full check
        #: has no lead to keep money for: each specialist has its own cap instead).
        self.helper_share_percent = HELPER_SHARE_PERCENT
        #: The most the message may have spent when a delegation's helpers stop (millionths of $),
        #: or `None`. A roster full check sets it for its page helpers and gap closers, so the
        #: money kept for the checks after them (the verifier, the devil's advocate, the lead
        #: review) is never spent by them: the paid run of 2026-10-19 spent $5.31 of a $5 price on
        #: helpers and its devil's advocate never ran.
        self.helper_ceiling: int | None = None
        #: The code findings of the finish (ledger, consistency, finishes, code's pairs) were
        #: added already (a roster full check adds them before its proof steps).
        self._code_findings_added = False
        #: A roster full check judges its red candidates with the devil's advocate instead.
        self.critic_off = False
        #: The simple full check (#1665): why it did not finish when no stop says so (drawings that
        #: could not be compared: review result S0), and its Not-compared rows that are not
        #: findings (an architect that gives no size), for the Not-compared PDF and workbook.
        self.incomplete_note: str | None = None
        #: Code's own checks that crashed (`add_code_findings`), by name (#1768): each makes a
        #: full check "not for sending" (S0), never a quiet log line.
        self.code_checks_failed: list[str] = []
        #: Continue of a full check that did not finish (#1821): set by `chat_service` after
        #: `carry_over`, so a roster check compares only the pages its earlier answer left.
        self.continuing = False
        #: What is left of the earlier answer's own price on a Continue (its price minus what it
        #: spent, millionths of $), set by `chat_service`; `None` when it is not known. A roster
        #: check's Continue costs that plus what its finishing steps need (2026-10-28).
        self.continue_left: int | None = None
        self.extra_gaps: list[dict[str, Any]] = []
        #: The set's gaps the Not-compared list named, once it is made (#1803).
        self._set_gaps: list[Any] | None = None
        #: A full check's live state for the live screen (`live_check.LiveCheck`, #1710), set by
        #: `chat_service`; the simple check reports its drawings, phases and decisions to it.
        self.live: Any = None
        #: Refusal-fallback switches already shown (#1577), and the thinking blocks the API dropped
        #: from this answer's calls because their conversation no longer matched (a bug signal).
        self._fallbacks_shown: set[tuple[str, str]] = set()
        self.mismatched_thinking = 0

    # ---- the screen ------------------------------------------------------------------------------

    def emit(self, event: str, data: dict[str, Any]) -> None:
        try:
            self._emit(event, data)
        except Exception:  # noqa: BLE001 - a gone screen never stops the answer being stored
            _log.debug("assistant chat event could not be sent")

    def _add_block(self, block: dict[str, Any]) -> int:
        with self._lock:
            self.blocks.append(block)
            return len(self.blocks) - 1

    def _add_text(self, text: str) -> None:
        """A text block written by the app (a stop note), streamed like the model's."""
        index = self._add_block({"type": "text", "text": text})
        self.emit("text_delta", {"index": index, "text": text})

    def carry_over(
        self, findings: Sequence[Mapping[str, Any]], coverage: Mapping[str, Any] | None
    ) -> int:
        """Continue (#1514): the findings and page coverage an answer a server restart cut off had
        already made, kept as this answer's own (shown at once; code checks each again). Returns
        how many findings were kept.

        Each finding keeps every field it had (2026-10-28: Continue had dropped the whole-drawing
        look's mark, the claim rules, the reasons, the decision help, `settled`, `also_on` and the
        scale mark, so 8 please-checks came back Not compared), except what this answer works
        out again (`CARRY_RECOMPUTED`). GV's recorded items come as placed (their spots and
        `record_id`), so seeding finds them by `record_id` (`_seed_recorded`) instead of making a
        spotless copy. The AI readings already made (second and third reads) are kept while what
        they read is unchanged, as are code's drawn-ink reads of the same values at the same
        spots: Continue never pays again for a reading it has."""
        kept = 0
        for given in findings:
            # An earlier answer's AI findings take the AI's room, code's own take code's (#1827).
            maker = str(given.get("created_by") or "lead")
            room = "code" if maker in CODE_ROOM_MAKERS else "ai"
            finding = self.book.add_code_finding(given, room=room)
            if finding is None:
                continue
            finding = {
                **finding,
                **{k: v for k, v in given.items() if k not in CARRY_RECOMPUTED},
                "created_by": maker,
                "verified": bool(given.get("verified")),
            }
            if isinstance(given.get(SAME_ITEM), Mapping):
                # One item, one card: its folded copies were dropped from the earlier answer, so
                # the card keeps them (a conflict stays Not compared, never red again).
                finding[SAME_ITEM] = dict(given[SAME_ITEM])
            self._carry_reads(given, finding)
            finding = _kept_drawn_ink(given, self._checked(finding))
            self.book.replace(finding)
            self._show_finding(finding)
            kept += 1
        pages = [
            {key: entry[key] for key in ("page", "pair_page", "status", "note") if key in entry}
            for entry in (coverage or {}).get("pages") or []
            if isinstance(entry, Mapping)
            and (entry.get("status") != "not_opened" or entry.get("note"))
        ]
        for entry in (coverage or {}).get("pages") or []:
            # One item, one card: the items an earlier answer folded under a page's
            # Drawing-needed card stay there as evidence (they were dropped from its findings).
            folds = entry.get("folded") if isinstance(entry, Mapping) else None
            if isinstance(folds, list) and isinstance(entry.get("page"), int):
                self.book.fold_into_page(
                    int(entry["page"]), [c for c in folds if isinstance(c, Mapping)]
                )
        if pages:
            self.book.set_coverage({"pages": pages}, "earlier")
            self._show_coverage()
        return kept

    def _carry_reads(self, given: Mapping[str, Any], finding: Mapping[str, Any]) -> None:
        """Continue: the earlier answer's second read (and its third read) of `given`, kept for
        its carried copy `finding` (its new id) while what it read is unchanged: the same item,
        values and boxes (its pictures are drawn again from the same boxes). A read that never
        ran stays to be made."""
        checks = given.get("checks") if isinstance(given.get("checks"), Mapping) else {}
        assert isinstance(checks, Mapping)
        second = checks.get("second_read")
        values = given.get("second_read_values")
        if second not in ("agrees", "disagrees", "unsure") or not isinstance(values, Mapping):
            return
        if any(given.get(k) != finding.get(k) for k in _CARRY_READ_SAME):
            return
        key = _read_key(finding)
        finding_id = str(finding["id"])
        with self._lock:
            self._reads[finding_id] = (key, str(second), dict(values))
            third = checks.get("adjudication")
            reads = given.get("reads")
            if isinstance(third, str) and isinstance(reads, list):
                self._thirds[finding_id] = (key, third, [dict(r) for r in reads])
                self._third_queued.add((finding_id, key))

    # ---- money -----------------------------------------------------------------------------------

    @property
    def _max_agents(self) -> int:
        return max(1, min(MAX_PARALLEL_AGENTS_LIMIT, self.settings.max_parallel_agents))

    def _limiter(self, model: ChatModel) -> ModelLimiter:
        """The server's one limiter for `model` (`rate_limits`, #1874)."""
        settings = self.settings
        return limiter_for(model.id, Limits.of(settings.rate_tier, settings.calls_at_once))

    @property
    def _plan_parallel(self) -> int:
        """The page groups a full check plans for, about (`full_check_plan.plan_jobs`: groups of
        3-5 pages, as many as run at once): a roster check's Sonnet places (`calls_at_once`) over
        its helpers per group (#1874: the helpers then finish in about one helper's time, the
        slowest group is small); a lead's check, its `max_parallel_agents`."""
        settings = self.settings
        if settings.mode == "full_check" and settings.roster:
            per_group = 2 if settings.drawn_helpers else 1
            return max(self._max_agents, settings.calls_at_once // per_group)
        return self._max_agents

    @property
    def _agents_at_once(self) -> int:
        """Specialists one launch starts together: a roster full check starts all its own (up to
        `full_check_agents`) and each model's limiter decides how many run (#1874); a chat's lead
        keeps `max_parallel_agents`."""
        if self.settings.mode == "full_check" and self.settings.roster:
            return max(self._max_agents, self.settings.full_check_agents)
        return self._max_agents

    @property
    def spent(self) -> int:
        with self._lock:
            return self._spent

    @property
    def counted_spent(self) -> int:
        """What the fixed price's shares have spent: everything but the calls on their own share
        (`own_share_micros`); `spent` when there are none."""
        with self._lock:
            return self._spent - self._own_spent

    @property
    def counted_cap(self) -> int:
        """The message cap less the money kept for the calls on their own share."""
        return max(0, self.settings.message_cap_micros - self.own_share_micros)

    def run_model(self, model: ChatModel) -> ChatModel:
        """The model a call asked of `model` runs on: `model`, except under a lead from outside
        Claude (`ChatModel.extra`, a comparison test): then every call of the answer, the helpers',
        the checkers' and the summary's included, runs on the lead's model and is priced at its
        rates, so the test measures that one model."""
        lead = self.settings.model
        return lead if lead.extra else model

    def _price(self, model: ChatModel, billed_in: int, out: int) -> int | None:
        return self._price_key(self.run_model(model).rates_key, billed_in, out)

    def _price_key(self, key: str, billed_in: int, out: int) -> int | None:
        return call_cost_micros(self.settings.rates(key), key, billed_in, out)

    def _priced(self, key: str) -> bool:
        rates = self.settings.rates(key)
        return rates is not None and rates.rate_for(key) is not None

    def _weighted(self, key: str, usage: Mapping[str, Any]) -> tuple[int, int]:
        """One attempt's tokens as price-weighted equivalents at its model's base price: cache
        writes 1.25x and reads 0.1x unless the price file states the model's own (Fable 5.1 reads
        at 0.025x); Haiku's long-prompt price 5x."""
        rates = self.settings.rates(key)
        rate = None if rates is None else rates.rate_for(key)
        billed_in = billed_input_tokens(usage, None if rate is None else rate.cache_factors())
        out = int(usage.get("output_tokens") or 0)
        prompt = sum(
            int(usage.get(name) or 0)
            for name in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        if "haiku" in key and prompt > _HAIKU_LONG_PROMPT:
            return billed_in * _HAIKU_LONG_FACTOR, out * _HAIKU_LONG_FACTOR
        long = _LONG_PROMPT_TOKENS.get(key)
        if long is not None and prompt > long:
            # GPT-6 over 272,000 prompt tokens: twice the input prices, 1.5 times the output's.
            return billed_in * 2, -(-out * 3 // 2)
        return billed_in, out

    def _attempts(
        self, model: ChatModel, usage: Mapping[str, Any]
    ) -> list[tuple[str, Mapping[str, Any]]]:
        """Each attempt a call made, with its price key (#1577). A refusal fallback lists them in
        `usage.iterations` (the declined model's and the fallback model's, each billed at its own
        model's price; the top-level usage is only the attempt that answered). Without that list,
        the call is one attempt on the model asked. A fallback model the price file does not list
        is priced as the model asked (never cheaper than it)."""
        model = self.run_model(model)
        entries = usage.get("iterations")
        if isinstance(entries, list):
            found = [
                entry
                for entry in entries
                if isinstance(entry, Mapping)
                and any(name in entry for name in ("input_tokens", "output_tokens"))
            ]
            if found:
                attempts: list[tuple[str, Mapping[str, Any]]] = []
                for entry in found:
                    named = entry.get("model")
                    key = rates_key_for(named) if isinstance(named, str) and named else ""
                    attempts.append((key if key and self._priced(key) else model.rates_key, entry))
                return attempts
        return [(model.rates_key, usage)]

    def _cost(self, model: ChatModel, usage: Mapping[str, Any]) -> int:
        """What a call cost, its attempts added up (an unknown price counts as nothing)."""
        return sum(
            self._price_key(key, *self._weighted(key, part)) or 0
            for key, part in self._attempts(model, usage)
        )

    def _estimate(
        self, model: ChatModel, body: Mapping[str, Any], cached: int, previous: int
    ) -> int | None:
        model = self.run_model(model)
        pictures = picture_sizes(body.get("messages") or [])
        estimate = estimate_micros(
            body,
            pictures,
            model.rates_key,
            self.settings.rates(model.rates_key),
            cached_tokens=cached,
            previous_tokens=previous,
        )
        if (
            estimate is not None
            and model.short == "haiku"
            and prompt_tokens_upper(body, pictures) > _HAIKU_LONG_PROMPT
        ):
            estimate *= _HAIKU_LONG_FACTOR
        if (
            estimate is not None
            and model.long_prompt_tokens is not None
            and prompt_tokens_upper(body, pictures) > model.long_prompt_tokens
        ):
            estimate *= 2
        return estimate

    def _fits(self, estimate: int | None) -> bool:
        return self._over(estimate) is None

    def _over(self, estimate: int | None) -> str | None:
        """The cap a call of this estimate would pass: `message`, `package`, or `None` (it fits).
        The set's budget is named first when both would be passed: more answers cannot help."""
        if estimate is None:
            return None
        try:
            package = self.package_spent()
        except Exception:  # noqa: BLE001 - an unknown spend stops rather than overspends
            _log.exception("assistant chat could not read the drawing set's spend")
            return "package"
        spent = self.spent
        if package + spent + estimate > self.settings.package_cap_micros:
            return "package"
        if spent + estimate > self.settings.message_cap_micros:
            return "message"
        return None

    def _hold(self, share: _Share, estimate: int | None) -> bool:
        """Whether a helper's next call fits the helpers' share; if it does, its estimate is held
        until the call is charged. A call that fits the money really spent but not the calls
        running now (each held at its worst case) waits for them to end (`_wait_for_holds`):
        the live run of 2026-10-24 stopped ten specialists at $4.36 of a $9 price because
        thirty helpers started together and their worst cases filled the share. Every call of a
        helper is held so, its record turn and its report too: one ledger, so two helpers that
        check at once never both take the same money (2026-10-24: CI's slow runners spent $0.03
        of the later checks' money when a report and a record turn ran together unheld)."""
        if estimate is None:
            return True
        return self._wait_for_holds(share, estimate)

    def _hold_final(self, share: _Share, estimate: int | None) -> bool:
        """A roster specialist's last call (its report) fits the fixed price and its share, and
        is then held like any call until it is charged: the share is what keeps the later
        checks' money (`helper_ceiling`). Like `_hold`, it waits for the calls running now rather
        than stop for their worst cases."""
        if not self._fits(estimate):
            return False
        return estimate is None or self._wait_for_holds(share, estimate)

    def _hold_record(self, share: _Share, estimate: int | None) -> bool:
        """A helper's record turn fits its share by the money really spent: never refused for
        the calls running now (#1654), it waits for them to end instead; never past the share's
        limit (the later steps' money). Then it is held like any call until it is charged."""
        return estimate is None or self._wait_for_holds(share, estimate)

    def _wait_for_holds(self, share: _Share, estimate: int) -> bool:
        """`estimate` fits `share` with the calls running now, and is held in the same step as
        the check (under `_lock`), so no other call can take that money in between. When only
        their holds are in the way (it fits the money really spent), it waits for one to end and
        looks again. False only when it cannot fit even with nothing else running: the share is
        really used."""
        deadline = monotonic() + _HOLD_WAIT_MOST_S
        while True:
            # Looked at while `_holds_changed` is taken, so a hold let go after this look wakes
            # the wait below (`_release` notifies under it): no wake-up is missed.
            with self._holds_changed:
                with self._lock:
                    counted = self._spent - self._own_spent
                    if counted + share.held + estimate <= share.limit:
                        share.held += estimate
                        return True
                    if share.held <= 0 or counted + estimate > share.limit:
                        return False
                if self.cancel.is_set() or monotonic() >= deadline:
                    return False
                self._holds_changed.wait(_HOLD_WAIT_S)

    def _release(self, share: _Share, held: int) -> None:
        with self._lock:
            share.held -= held
        if held:
            with self._holds_changed:
                self._holds_changed.notify_all()

    def _charge(self, model: ChatModel, usage: Mapping[str, Any], own_share: bool = False) -> None:
        cost = self._cost(model, usage)
        with self._lock:
            self._spent += cost
            if own_share:
                self._own_spent += cost
            total = self._spent
        self.timings.charge(cost)
        for _key, attempt in self._attempts(model, usage):
            # Each phase's tokens by kind (#1821): whether the prompt cache is hit, in the log.
            self.timings.tokens(attempt)
        self.emit("usage", {"cost_usd": cost_text(total)})

    # ---- one call --------------------------------------------------------------------------------

    def _body(
        self,
        *,
        model: ChatModel,
        thinking: Mapping[str, str],
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: list[dict[str, Any]],
        final: bool,
        max_tokens: int,
    ) -> dict[str, Any]:
        """The call as stored (pictures as references), for the estimate."""
        return {
            "max_tokens": max_tokens,
            "system": system,
            "tools": list(tools),
            "tool_choice": {"type": "none"} if final else {"type": "auto"},
            "thinking": dict(thinking),
            "messages": messages,
        }

    def _call(
        self,
        *,
        model: ChatModel,
        effort: str,
        thinking: Mapping[str, str],
        system: str,
        tools: Sequence[Mapping[str, Any]],
        messages: list[dict[str, Any]],
        final: bool,
        max_tokens: int,
        prompt_id: str,
        display: Callable[[], _Display] | None,
        context_management: Mapping[str, Any] | None = None,
        output_format: Mapping[str, Any] | None = None,
        enforce_format: bool = False,
        own_share: bool = False,
    ) -> _Reply:
        """One call, streamed, asked once more after a provider failure worth retrying. Raises
        `ProviderFailure` or `_Cancelled`. Under a lead from outside Claude it runs on the lead's
        model (`run_model`). `enforce_format`: `output_format` is enforced on OpenRouter too
        (`call_params`; the readings). `own_share`: charged to a step's own share (the page
        reviewer), never to the fixed price's other shares (`counted_spent`)."""
        model = self.run_model(model)
        attempts = 0
        busy = 0
        while True:
            try:
                inlined = inline_pictures(messages, self.read_picture)
            except OSError:
                raise ProviderFailure(
                    "A picture this chat looked at earlier is missing from its folder, so the chat "
                    "cannot go on. Start a new chat.",
                    status=None,
                    retryable=False,
                ) from None
            params = call_params(
                provider=self.provider.name,
                model=model,
                effort=effort,
                thinking=thinking,
                system=system,
                tools=tools,
                messages=inlined,
                final=final,
                max_tokens=max_tokens,
                context_management=context_management,
                output_format=output_format,
                enforce_format=enforce_format,
            )
            started = monotonic_ns()
            shown = display() if display is not None else None
            reply: _Reply | None = None
            failure: ProviderFailure | None = None
            usage: dict[str, Any] = {}
            # The model's limiter (#1874): a place at once (unless a specialist on this thread
            # holds one) and room in the provider's minute; a busy answer slows it down.
            # The account's credit (`AccountCredit`): held at this call's worst case until it
            # ends, so the calls running together never ask for more than the balance covers.
            ACCOUNT_CREDIT.start(self.provider, force=not self._credit_read)
            self._credit_read = True
            body = self._body(
                model=model,
                thinking=thinking,
                system=system,
                tools=tools,
                messages=messages,
                final=final,
                max_tokens=max_tokens,
            )
            limiter = self._limiter(model)
            own = limiter.call_slot()
            if not limiter.acquire(self.cancel, slot=own):
                self._cancelled_call(model, prompt_id, started, usage)
                raise _Cancelled
            # Held once it has its place (one waiting for a place holds no money, #1874); the
            # calls holding the credit are running, so they end and let it go.
            try:
                credit_held = ACCOUNT_CREDIT.hold(self._estimate(model, body, 0, 0), self.cancel)
            except BaseException:
                limiter.release(slot=own, fresh_input=0, output=0, busy=False, answered=False)
                raise
            busy_answer = False
            try:
                stream = self.provider.open_stream(params)
                with self._lock:
                    self._streams.add(stream)
                try:
                    reply = _accumulate(stream, shown, self.cancel.is_set, usage)
                finally:
                    with self._lock:
                        self._streams.discard(stream)
                    stream.close()
            except _Cancelled:
                self._cancelled_call(model, prompt_id, started, usage)
                raise
            except Exception as raised:  # noqa: BLE001 - every provider failure is told plainly
                if self.cancel.is_set():
                    # Cancel closed the stream under the read: a stop, not a failure.
                    self._cancelled_call(model, prompt_id, started, usage)
                    raise _Cancelled from None
                failure = failure_from(raised)
                busy_answer = failure.kind == "busy"
            finally:
                limiter.release(
                    slot=own,
                    fresh_input=_fresh_input(usage),
                    output=int(usage.get("output_tokens") or 0),
                    busy=busy_answer,
                    answered=bool(usage) and not busy_answer,
                )
                ACCOUNT_CREDIT.done(credit_held, self._cost(model, usage) if usage else 0)
            if failure is not None and usage:
                # It broke after its stream started: its prompt is billed already (harness
                # audit 2026-10-19: such calls were recorded as $0 and the guard missed them).
                partial = _Reply([], None, None, usage, None)
                self._record(model, prompt_id, started, partial, failure)
                self._charge(model, usage, own_share)
                if shown is not None:
                    self._discard_attempt()
                if failure.retryable and attempts == 0:
                    attempts += 1
                    _log.warning(
                        "assistant chat call failed mid-stream, asking once more: %s",
                        failure.status,
                    )
                    if self._sleep(_RETRY_WAIT_SECONDS):
                        raise _Cancelled from None
                    continue
                raise failure from None
            if reply is not None and reply.stop_reason is None and failure is None:
                # The stream ended without its `message_delta` (the connection closed cleanly in
                # the middle): a cut answer, never a finished one.
                failure = ProviderFailure(STREAM_CUT_WORDS, status=None, retryable=True)
            if reply is not None and not _names_model(reply, model, self.provider.name):
                failure = ProviderFailure(
                    "The AI service answered with a different model than the one asked, so the "
                    "answer was not used.",
                    status=None,
                    retryable=False,
                )
            if failure is not None and failure.status == 404 and model.short == "fable":
                # An early draft of Anthropic's docs: a 404 on the id when retention is off.
                failure = ProviderFailure(RETENTION_WORDS, status=404, retryable=False)
            if reply is not None and model.extra:
                reply.content = _without_unsigned_thinking(reply.content)
            self._record(model, prompt_id, started, reply, failure)
            if reply is not None:
                self._charge(model, reply.usage, own_share)
                self._note_transformations(reply, prompt_id)
            if failure is None and reply is not None:
                if shown is not None:
                    self._show_fallback(model, reply)
                return reply
            assert failure is not None
            if shown is not None:
                self._discard_attempt()
            if failure.retryable and attempts == 0:
                attempts += 1
                _log.warning("assistant chat call failed, asking once more: %s", failure.status)
                if self._sleep(_RETRY_WAIT_SECONDS):
                    raise _Cancelled
                continue
            if failure.kind == "busy" and busy < len(_BUSY_WAITS_SECONDS):
                # Spread a little, so helpers that were refused together do not ask together.
                wait = _BUSY_WAITS_SECONDS[busy] * (1.0 + random.random() / 4)
                busy += 1
                _log.warning(
                    "assistant chat call: the AI service is busy (%s), asking again in %.0f s",
                    failure.status,
                    wait,
                )
                if self._sleep(wait):
                    raise _Cancelled
                continue
            raise failure

    def _note_transformations(self, reply: _Reply, prompt_id: str) -> None:
        """Logs the thinking blocks the API dropped or let through although their conversation had
        changed (`prefix_binding_mismatch`: our history or prefix changed, a bug), and those a
        model switch dropped (`model_binding_mismatch`: expected). Never the content."""
        mismatched = [
            item
            for item in reply.transformations
            if item.get("reason") == "prefix_binding_mismatch"
        ]
        switched = [
            item for item in reply.transformations if item.get("reason") == "model_binding_mismatch"
        ]
        if mismatched:
            with self._lock:
                self.mismatched_thinking += len(mismatched)
            _log.warning(
                "assistant chat history check: %d thinking block(s) no longer match their "
                "conversation (%s; first at %s)",
                len(mismatched),
                prompt_id,
                mismatched[0].get("path"),
            )
        if switched:
            _log.info(
                "assistant chat: %d thinking block(s) from another model left out (%s)",
                len(switched),
                prompt_id,
            )

    def _show_fallback(self, model: ChatModel, reply: _Reply) -> None:
        """A refusal fallback that answered the lead (#1577): one quiet line per switch, stored
        with the answer (a `fallback` block) and sent as a `fallback` event. A reply the whole
        chain refused is not shown here (the answer says Claude declined)."""
        if reply.stop_reason == "refusal" or not reply.fell_back:
            return
        switches = reply.switches() or (
            [(model.id, reply.model)] if reply.model and reply.model != model.id else []
        )
        for declined, answered in switches:
            if (declined, answered) in self._fallbacks_shown:
                continue
            self._fallbacks_shown.add((declined, answered))
            fallback = {"from_model": declined, "to_model": answered}
            index = self._add_block({"type": "fallback", "fallback": fallback})
            self.emit("fallback", {"index": index, "fallback": dict(fallback)})

    def _cancelled_call(
        self, model: ChatModel, prompt_id: str, started: int, usage: dict[str, Any]
    ) -> None:
        """A call stopped by cancel: recorded and charged for what it used so far."""
        self._record(model, prompt_id, started, _Reply([], None, None, usage, None), None)
        self._charge(model, usage)

    def track_stream(self, stream: Any) -> None:
        """A stream opened outside `_call` (the simple check's compare calls, #1769), kept so a
        Stop closes it; one opened after the Stop is closed at once."""
        with self._lock:
            self._streams.add(stream)
        if self.cancel.is_set():
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                _log.debug("assistant chat stream did not close on cancel")

    def untrack_stream(self, stream: Any) -> None:
        with self._lock:
            self._streams.discard(stream)

    def stop_streams(self) -> None:
        """Close the calls streaming now, so a cancel does not wait for the next event."""
        with self._lock:
            streams = list(self._streams)
        for stream in streams:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing is best effort
                _log.debug("assistant chat stream did not close on cancel")

    def _record(
        self,
        model: ChatModel,
        prompt_id: str,
        started: int,
        reply: _Reply | None,
        error: BaseException | None,
    ) -> None:
        """One Usage row per attempt (#1577: a refusal fallback's declined and answering attempts
        each on its own model); the call's stop reason and error go on the last."""
        attempts = [(model.rates_key, {})] if reply is None else self._attempts(model, reply.usage)
        for number, (key, part) in enumerate(attempts, start=1):
            last = number == len(attempts)
            billed_in, out = (0, 0) if reply is None else self._weighted(key, part)
            try:
                self.ledger.record(
                    model_key=key,
                    prompt_id=prompt_id,
                    started_ns=started,
                    input_tokens=billed_in,
                    output_tokens=out,
                    stop_reason=None if reply is None or not last else reply.stop_reason,
                    error=error if last else None,
                )
            except Exception:  # noqa: BLE001 - the answer still goes on; the failure is logged
                _log.exception("assistant chat call could not be recorded")

    # ---- the lead's stream on the screen ---------------------------------------------------------

    def _new_attempt(self) -> _Display:
        """The display of one streamed attempt: API content indexes to the screen's blocks."""
        shown: dict[int, int] = {}
        started: dict[int, float] = {}
        kinds: dict[int, str] = {}
        self._attempt_blocks = []

        def block_for(index: int, kind: str) -> int:
            if index not in shown:
                if kind == "thinking":
                    position = self._add_block({"type": "thinking", "text": "", "seconds": 0})
                    self.emit("thinking_start", {"index": position})
                else:
                    position = self._add_block({"type": "text", "text": ""})
                shown[index] = position
                self._attempt_blocks.append(position)
            return shown[index]

        def display(event: Mapping[str, Any]) -> None:
            kind = event.get("type")
            index = int(event.get("index", -1)) if "index" in event else -1
            if kind == "content_block_start":
                block = event.get("content_block") or {}
                kinds[index] = str(block.get("type"))
                started[index] = monotonic()
            elif kind == "content_block_delta":
                delta = event.get("delta") or {}
                if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                    position = block_for(index, "thinking")
                    with self._lock:
                        self.blocks[position]["text"] += str(delta["thinking"])
                    self.emit("thinking_delta", {"index": position, "text": delta["thinking"]})
                elif delta.get("type") == "text_delta" and delta.get("text"):
                    position = block_for(index, "text")
                    with self._lock:
                        self.blocks[position]["text"] += str(delta["text"])
                    self.emit("text_delta", {"index": position, "text": delta["text"]})
            elif kind == "content_block_stop":
                if kinds.get(index) == "thinking" and index in shown:
                    seconds = max(1, round(monotonic() - started.get(index, monotonic())))
                    position = shown[index]
                    with self._lock:
                        self.blocks[position]["seconds"] = seconds
                    self.emit("thinking_end", {"index": position, "seconds": seconds})

        return display

    def _discard_attempt(self) -> None:
        """Leave the last attempt's blocks out of what is stored (the screen replaces its live
        blocks with the stored message at `message_end`)."""
        with self._lock:
            self._discarded.update(self._attempt_blocks)
            self._attempt_blocks = []

    def out_of_credit(self) -> bool:
        """Whether the AI account ran out of credit during this answer: the provider asked for
        payment (402, or the account's spending limit) and its breaker stopped new calls."""
        breaker = getattr(self.provider, "breaker", None)
        return getattr(breaker, "kind", None) == "payment"

    def stored_blocks(self) -> list[dict[str, Any]]:
        """The answer's blocks as stored. The last guard of the double-read rule (2026-10-28):
        a would-be red still without its second reading and its reason is stored with the reason
        (`_unread_why`), whatever step changed it last, so it is never stored red."""
        with self._lock:
            blocks = [
                json.loads(json.dumps(block))
                for n, block in enumerate(self.blocks)
                if n not in self._discarded
            ]
        if not self._reads_twice():
            return blocks
        for block in blocks:
            finding = block.get("finding") if block.get("type") == "finding" else None
            if isinstance(finding, dict) and _unread_red(finding):
                finding[NOT_DOUBLE_READ] = self._unread_why()
        return blocks

    # ---- tools -----------------------------------------------------------------------------------

    @staticmethod
    def _result(call: Mapping[str, Any], outcome: ToolResult) -> dict[str, Any]:
        result: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": outcome.content,
        }
        if outcome.is_error:
            result["is_error"] = True
        return result

    @staticmethod
    def _not_run(call: Mapping[str, Any], why: str) -> dict[str, Any]:
        return {
            "type": "tool_result",
            "tool_use_id": call.get("id"),
            "content": [{"type": "text", "text": f"Not run: {why}."}],
            "is_error": True,
        }

    def _dispatch_safely(
        self, name: str, raw: object, step: dict[str, Any], *, helper: bool, by: str = "lead"
    ) -> ToolResult:
        """`_dispatch`, with an error nobody planned for (a bug, a damaged set-pack file) turned
        into a plain tool error the model reads: the answer goes on, and the step ends failed
        instead of spinning for ever (harness audit 2026-10-19). The error itself is logged."""
        try:
            return self._dispatch(name, raw, step, helper=helper, by=by)
        except Exception:  # noqa: BLE001 - the model is told plainly; the answer goes on
            _log.exception("assistant chat tool %s failed", name)
            return ToolResult(
                content=[{"type": "text", "text": f"Refused: {TOOL_FAILED_WORDS}"}],
                step={**step, "ok": False, "output": TOOL_FAILED_WORDS},
                is_error=True,
            )

    def _guarded(
        self, call: Mapping[str, Any], work: Callable[[], dict[str, Any]]
    ) -> Callable[[], dict[str, Any]]:
        """A finding or ledger tool's work, with an unplanned error told as a tool error."""

        def run() -> dict[str, Any]:
            try:
                return work()
            except Exception:  # noqa: BLE001 - the model is told plainly; the answer goes on
                _log.exception("assistant chat tool %s failed", call.get("name"))
                return self._not_run(call, TOOL_FAILED_WORDS.rstrip("."))

        return run

    def _dispatch(
        self, name: str, raw: object, step: dict[str, Any], *, helper: bool, by: str = "lead"
    ) -> ToolResult:
        tools = self.tools
        if name == "bash_tool":
            return tools.bash(raw, step)
        if name == "view":
            return tools.view(raw, step, HELPER_PICTURE_EDGE_PX if helper else None)
        if name == "zoom":
            return tools.zoom(raw, step, by)
        if name == "create_file":
            return tools.create_file(raw, step)
        if name == "str_replace":
            return tools.str_replace(raw, step)
        if name == "verify_value":
            return tools.verify_value(raw, step)
        if name == "read_number":
            return tools.read_number(raw, step)
        if name == "measure_on_drawing":
            return tools.measure_on_drawing(raw, step)
        if name == "drawing_facts":
            return tools.drawing_facts(raw, step)
        if name == "read_under_markup":
            return tools.read_under_markup(raw, step, by)
        if not helper:
            if name == "remember":
                return tools.remember(raw, step)
            if name == "present_files":
                return tools.present_files(raw, step)
            if name == "TaskCreate":
                return self.task_list.create(raw, step)
            if name == "TaskUpdate":
                return self.task_list.update(raw, step)
            if name == "review_results":
                return tools.review_results(raw, step)
        if name == "compare_rounds":  # the lead's, and a helper's with the rounds kit
            return self._compare_rounds(raw, step)
        return ToolResult(
            content=[{"type": "text", "text": f"Refused: there is no tool {name!r}"}],
            step={**step, "ok": False, "output": f"There is no tool {name!r}."},
            is_error=True,
        )

    @staticmethod
    def _in_turns(
        calls: Sequence[dict[str, Any]],
        parallel: Callable[[dict[str, Any]], bool],
        prepare: Callable[[dict[str, Any]], Callable[[], dict[str, Any]]],
    ) -> dict[int, dict[str, Any]]:
        """Each call's result. A run of calls that `parallel` says may run together runs at the
        same time; any other call runs alone, in its place. `prepare` is called in the order
        asked (it may show a step) and returns the work."""
        results: dict[int, dict[str, Any]] = {}
        group: list[tuple[dict[str, Any], Callable[[], dict[str, Any]]]] = []

        def flush() -> None:
            if len(group) == 1:
                call, work = group[0]
                results[id(call)] = work()
            elif group:
                with ThreadPoolExecutor(max_workers=min(len(group), MAX_PARALLEL_COMMANDS)) as pool:
                    futures = [(call, pool.submit(work)) for call, work in group]
                    for call, future in futures:
                        results[id(call)] = future.result()
            group.clear()

        for call in calls:
            if parallel(call):
                group.append((call, prepare(call)))
                continue
            flush()
            results[id(call)] = prepare(call)()
        flush()
        return results

    def _run_lead_tools(self, reply: _Reply) -> list[dict[str, Any]]:
        calls = reply.tool_uses()
        agents = [call for call in calls if call.get("name") == "Agent"]

        def prepare(call: dict[str, Any]) -> Callable[[], dict[str, Any]]:
            if self.cancel.is_set():
                cancelled = self._not_run(call, "the reviewer cancelled the message")
                return lambda: cancelled
            name = str(call.get("name"))
            if id(call) in reply.bad_inputs:
                bad = self._not_run(call, "its input was cut off or not valid JSON")
                return lambda: bad
            self.timings.tool(name)
            if name in FINDING_TOOL_NAMES:
                return self._guarded(
                    call,
                    lambda: self._result(call, self._finding_tool(name, call.get("input"), "lead")),
                )
            if name in LEDGER_TOOL_NAMES:
                return self._guarded(
                    call,
                    lambda: self._result(call, self._ledger_tool(name, call.get("input"), "lead")),
                )
            if name == RECORD_ITEMS["name"]:
                return self._guarded(
                    call,
                    lambda: self._result(call, self._record_items(call.get("input"), "lead")),
                )
            # The step shows at once, in the order asked, even when its work runs alongside others.
            step = new_step(str(call.get("id")), name, call.get("input"))
            index = self._add_block({"type": "step", "step": step})
            self.emit("step_start", {"index": index, "step": step})

            def work() -> dict[str, Any]:
                outcome = self._dispatch_safely(name, call.get("input"), step, helper=False)
                with self._lock:
                    self.blocks[index] = {"type": "step", "step": outcome.step}
                self.emit("step_end", {"index": index, "step": outcome.step})
                if name in ("TaskCreate", "TaskUpdate") and outcome.step.get("tasks") is not None:
                    self.emit("tasks", {"tasks": outcome.step["tasks"]})
                return self._result(call, outcome)

            return work

        with self.timings.phase(self._lead_phase()):
            results = self._in_turns(
                [call for call in calls if call.get("name") != "Agent"],
                lambda call: (
                    call.get("name") in LEAD_PARALLEL_TOOLS and id(call) not in reply.bad_inputs
                ),
                prepare,
            )
        if agents:
            results.update(self._run_agents(agents))
        return [results[id(call)] for call in calls]

    # ---- scope (#1520) ---------------------------------------------------------------------------

    def scope(self) -> dict[str, Any]:
        """This answer's scope now: the question's, with the pages it worked on (its coverage and
        its own findings; a recorded difference it was handed is not its choice of page)."""
        book = self.book
        with self._lock:
            used = set(book.coverage)
        used |= {
            int(f["page"])
            for f in book.live()
            if isinstance(f.get("page"), int) and not f.get("record_id")
        }
        scope = widened(self._asked_scope, used)
        book.scope_pages = scoped_pages(scope)
        return {**scope, **self.product_scope.as_dict()}

    def _whole_set_work(self) -> bool:
        """Whole-set work (a full check, or a whole-set answer that used coverage): only it gets
        the "every page, every recorded difference" checks. A full check on picked pages is
        whole-set work on its pages (its coverage and recorded differences are theirs only)."""
        if self.settings.mode == "full_check" and self.settings.pages:
            self.scope()  # the coverage map and recorded differences cut to its pages
            return True
        if not is_whole_set(self.scope()):
            return False
        return self.settings.mode == "full_check" or self.book.coverage_used

    def _store_scope(self) -> None:
        """At finish: the answer's scope as a block (sent again only when the work changed it)."""
        before = self.book.scope_pages
        scope = self.scope()
        index = self._add_block({"type": "scope", "scope": scope})
        if scope != self._started_scope:
            self.emit("scope", {"index": index, "scope": json.loads(json.dumps(scope))})
        if self.book.coverage_used and self.book.scope_pages != before:
            self._show_coverage()  # the coverage map, re-scoped

    # ---- findings, coverage and trust (chat v2 / v3) ---------------------------------------------

    def _recorded_checks(self) -> Recorded:
        """What GV Review's own checks recorded for the set, read once per message."""
        with self._lock:
            if self._recorded_read and self._recorded is not None:
                return self._recorded
        loader = self.tools.results
        recorded = Recorded(readable=False)
        document: Mapping[str, Any] | None = None
        if loader is not None:
            try:
                loaded = loader(None)
                document = loaded if isinstance(loaded, Mapping) else None
                recorded = recorded_from(loaded)
                if self.settings.mode == "full_check" and self.settings.pages:
                    # A full check on picked pages: GV's recorded differences on them only.
                    recorded = scoped_recorded(recorded, frozenset(self.settings.pages))
            except Exception as failed:  # noqa: BLE001 - the report says it could not be read
                _log.warning("assistant chat could not read the records: %s", type(failed).__name__)
        with self._lock:
            self._recorded, self._recorded_read = recorded, True
            self._recorded_document = document
        return recorded

    def _checked(self, finding: dict[str, Any]) -> dict[str, Any]:
        """A finding with code's checks. Its second read is kept while what it read (the item,
        both values and both crops) is unchanged, else it starts again (#1512)."""
        recorded = self._recorded_checks() if finding.get("source") == "rules" else None
        kept = self._reads.get(str(finding.get("id")))
        if kept is not None and kept[0] != _read_key(finding):
            kept = None
        checked = apply_checks(
            finding,
            self.book.pdf_paths(),
            recorded,
            second_read=kept[1] if kept is not None else "not_run",
        )
        checked.setdefault("round_status", None)
        checked["second_read_values"] = dict(kept[2]) if kept is not None else None
        third = self._thirds.get(str(finding.get("id")))
        if kept is not None and third is not None and third[0] == kept[0]:
            # Its third read is kept with its second read (made early for a would-be red).
            checks = {**(checked.get("checks") or {}), "adjudication": third[1]}
            checked = judged({**checked, "checks": checks, "reads": list(third[2])})
        # The resolver's fields (#1554) and the escalation's (#1619) are set only on the items
        # they touch (and the round match, 2026-10-22).
        for key in (
            "ledger_ids",
            "reference_side",
            "sum_text",
            "settled",
            "question",
            "keep_reason",
            "also_on",
            "same_item",
            "parts_sum",
            "escalation",
            "string_reads",
            "round_match",
        ):
            checked.setdefault(key, None)
        return checked

    def _checked_earlier(self, ref: str, finding: dict[str, Any]) -> dict[str, Any]:
        """A new version of an earlier answer's finding with code's checks (#1548). Its second
        read is kept while what it read is unchanged; no new AI read is made for it."""
        before = self.book.earlier[ref]["finding"]
        kept = (before.get("checks") or {}).get("second_read") or "not_run"
        same = _read_key(before) == _read_key(finding)
        recorded = self._recorded_checks() if finding.get("source") == "rules" else None
        checked = apply_checks(
            finding,
            self.book.pdf_paths(),
            recorded,
            second_read=kept if same else "not_run",
        )
        if not same:
            checked["second_read_values"] = None
        return checked

    def _show_correction(self, update: dict[str, Any]) -> None:
        """A correction of an earlier answer's finding: its block (replaced in place when the same
        finding is corrected again) and the `finding_update` event."""
        key = f"update:{update['ref']}"
        block = {"type": "finding_update", "update": update}
        with self._lock:
            index = self._positions.get(key)
            if index is None:
                self.blocks.append(block)
                index = len(self.blocks) - 1
                self._positions[key] = index
            else:
                self.blocks[index] = block
        self.emit("finding_update", {"index": index, "update": json.loads(json.dumps(update))})

    def _show_finding(self, finding: dict[str, Any]) -> None:
        """A new or changed finding: its block (replaced in place) and the `finding` event. One
        made or changed after the reference pages were stamped is stamped again from its pair
        (`_stamp_reference_pages`), so its reference page never goes stale."""
        if getattr(self, "_reference_stamped", False):
            stamped = self._with_reference(finding)
            if stamped != finding and self.book.replace(stamped):
                finding = stamped
        with self._lock:
            index = self._positions.get(finding["id"])
            if index is None:
                self.blocks.append({"type": "finding", "finding": finding})
                index = len(self.blocks) - 1
                self._positions[finding["id"]] = index
            else:
                self.blocks[index] = {"type": "finding", "finding": finding}
        self.emit("finding", {"index": index, "finding": json.loads(json.dumps(finding))})
        if self._progress is not None:
            self._progress.changed()

    def _show_coverage(self) -> None:
        """The coverage block (replaced in place) and the `coverage` event. The map is read again
        under the lock, and sent under it: helpers record coverage at once, and a map read before
        another helper's must never land after it (its pages would show as not opened)."""
        with self._lock:
            # Kept and sent as a read says it (`drawing_needed` on each page: the results screen's tile).
            coverage = ChatCoverageOut.model_validate(self.book.coverage_map()).model_dump(
                mode="json"
            )
            index = self._coverage_index
            if index is None:
                self.blocks.append({"type": "coverage", "coverage": coverage})
                index = self._coverage_index = len(self.blocks) - 1
            else:
                self.blocks[index] = {"type": "coverage", "coverage": coverage}
            self.emit("coverage", {"index": index, "coverage": json.loads(json.dumps(coverage))})
        if self._progress is not None:
            self._progress.changed()

    def count_items(self, total: int) -> Callable[[], None]:
        """The final checks by code (2026-10-23): "checking n of `total` items" on the live
        line while a long code step runs; the returned function counts one item done (from any
        thread). Never raises: progress never stops the check."""
        progress = self._progress
        if progress is None or total <= 0:
            return lambda: None
        lock = threading.Lock()
        done = [0]

        def show() -> None:
            try:
                progress.set_items(done[0], total)
            except Exception:  # noqa: BLE001 - progress never stops the check
                _log.debug("assistant chat item count not shown")

        def one() -> None:
            with lock:
                done[0] += 1
                show()

        with lock:
            show()
        return one

    def items_counted(self) -> None:
        """The long code step that counted its items is over (`count_items`)."""
        progress = self._progress
        if progress is not None:
            try:
                progress.end_items()
            except Exception:  # noqa: BLE001 - progress never stops the check
                _log.debug("assistant chat item count not ended")

    def _show_progress(self, progress: dict[str, Any]) -> None:
        """Full check: the progress numbers, one block replaced in place, and `progress`."""
        with self._lock:
            block = {"type": "progress", "progress": progress}
            if self._progress_index is None:
                self.blocks.append(block)
                self._progress_index = len(self.blocks) - 1
            else:
                self.blocks[self._progress_index] = block
            index = self._progress_index
        self.emit("progress", {"index": index, "progress": dict(progress)})

    def _finding_tool(self, name: str, raw: object, by: str) -> ToolResult:
        """`report_finding`, `update_finding`, `drop_finding`, `set_coverage`: no step on the
        screen; the finding and coverage blocks are what the reviewer sees."""
        book = self.book
        try:
            if name == "report_finding":
                finding, notes = book.report(raw, by)
                self._show_finding(finding)
                text = f"Finding {finding['id']} recorded."
            elif name == "update_finding" and (ref := book.earlier_ref(raw)) is not None:
                ref, changed, notes = book.correct(raw)
                update = book.store_correction(
                    ref, self._checked_earlier(ref, changed), None, self.answer_number
                )
                self._show_correction(update)
                text = (
                    f"Finding {ref} updated: version {update['version']}, shown in answer "
                    f"{update['answer']} as updated in a later message."
                )
            elif name == "drop_finding" and (ref := book.earlier_ref(raw)) is not None:
                ref, reason = book.drop_earlier(raw)
                notes = []
                update = book.store_correction(ref, None, reason, self.answer_number)
                self._show_correction(update)
                text = f"Finding {ref} dropped from answer {update['answer']}."
            elif name == "update_finding":
                finding, notes = book.update(raw)
                self._show_finding(finding)
                text = f"Finding {finding['id']} updated."
            elif name == "cite":
                module = load_module(CHECKS_MODULE)

                def printed(path: Path, page: int, box: Mapping[str, Any], value: str) -> str:
                    if module is None:
                        return "not_run"
                    return str(module.printed_at_spot(path, page, box, value))

                cite = book.cite(raw, printed)
                notes = []
                index = self._add_block({"type": "cite", "cite": cite})
                self.emit("cite", {"index": index, "cite": json.loads(json.dumps(cite))})
                text = f"Cited {cite['id']}: {cite['printed'].replace('_', ' ')}."
            elif name == "dismiss_recorded":
                record_id, reason = book.dismiss(raw)
                notes = []
                text = f"Recorded difference {record_id} dismissed: {reason}"
            elif name == "drop_finding":
                finding_id, reason = book.drop(raw)
                notes = []
                self._show_dropped(finding_id, reason)
                text = f"Finding {finding_id} dropped."
            else:
                coverage = book.set_coverage(raw, by)
                self._show_coverage()
                counts = coverage["counts"]
                gaps = book.coverage_gaps()
                text = (
                    f"Coverage: {counts['same']} same, {counts['different']} different, "
                    f"{counts['not_comparable']} not comparable, of {counts['total']} pages."
                )
                if gaps:
                    text += f" Not accounted for yet: pages {page_ranges(gaps)}."
                notes = []
        except SandboxRefused as refused:
            return ToolResult(
                content=[{"type": "text", "text": f"Refused: {refused}"}],
                step={},
                is_error=True,
            )
        if notes:
            text += " " + "; ".join(notes) + "."
        return ToolResult(content=[{"type": "text", "text": text}], step={})

    def _compare_rounds(self, raw: object, step: dict[str, Any]) -> ToolResult:
        """`compare_rounds` (revision diff, $0 code): where the vendor's drawing changed between two
        rounds, and each round-1 finding's round status (a changed spot is re-read cheaply)."""
        started = monotonic()

        def done(text: str, ok: bool, **changes: Any) -> dict[str, Any]:
            return {
                **step,
                **changes,
                "ok": ok,
                "output": text[:4000],
                "seconds": round(monotonic() - started, 1),
            }

        module = load_module(REVISION_DIFF_MODULE)
        if module is None:
            text = "Not run: the revision diff is not available here."
            return ToolResult([{"type": "text", "text": text}], done(text, False), is_error=True)
        if not isinstance(raw, Mapping):
            text = "Refused: the tool's input is not an object"
            return ToolResult([{"type": "text", "text": text}], done(text, False), is_error=True)
        findings = self.book.live()
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    module.compare_rounds,
                    raw,
                    self.book.pdf_paths(),
                    self.tools.workspace.pictures,
                    findings=findings,
                ).result()
        except ValueError as refused:
            text = f"Refused: {refused}"
            return ToolResult([{"type": "text", "text": text}], done(text, False), is_error=True)
        except Exception as failed:  # noqa: BLE001 - told to the model, never a crash
            _log.warning("assistant chat compare_rounds failed: %s", type(failed).__name__)
            text = "Not run: the two rounds could not be compared."
            return ToolResult([{"type": "text", "text": text}], done(text, False), is_error=True)
        text = str(result.get("text") or "")
        not_lined_up = refused_from_diff(result, raw)
        if not_lined_up is not None:
            with self._lock:
                self.refused_diffs.append(not_lined_up)
        notes = self._apply_round_status(result, findings, raw)
        if notes:
            text += "\nRe-read of changed spots: " + "; ".join(notes) + "."
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        picture = result.get("picture")
        changes: dict[str, Any] = {}
        if isinstance(picture, Mapping) and picture.get("picture_id"):
            content.append({"type": "image", "source": {"type": "gv_picture", **picture}})
            changes["picture_id"] = picture["picture_id"]
        return ToolResult(content, done(text, True, **changes))

    def _apply_round_status(
        self,
        result: Mapping[str, Any],
        findings: Sequence[Mapping[str, Any]],
        given: Mapping[str, Any],
    ) -> list[str]:
        """Store each finding's round status from a compared pair (CONTRACT3 "Revision diff")."""
        status = result.get("round_status")
        if not isinstance(status, Mapping):
            return []
        files = list(result.get("files") or [None, None])
        pairing = result.get("pairing") or {}
        page_a = given.get("round1_page")
        page_b = given.get("round2_page") or pairing.get("round2_page")
        by_id = {str(f.get("id")): f for f in findings}
        notes: list[str] = []
        counts = {"fixed_in_later_round": 0, "still_wrong": 0}
        for item in status.get("findings") or []:
            finding = by_id.get(str(item.get("finding_id")))
            measured = str(item.get("round_status"))
            if finding is None or measured == "not_on_page":
                continue
            to_check = False
            if measured == "changed_check":
                boxes = item.get("recheck_box") or []
                box = dict(boxes[0]) if boxes else None
                if box is not None and files[-1]:
                    box["file"] = files[-1]
                measured, read = self._round_reread(finding, box)
                to_check = read is None
                notes.append(
                    f"{finding['id']} {measured}"
                    + (f" (round 2 reads {read})" if read else " (round 2 could not be read)")
                )
            elif measured == "untouched":
                measured = "single_round"
            if measured in counts:
                counts[measured] += 1
            self._store_round_status(str(finding["id"]), measured, to_check)
        unrequested = status.get("unrequested_changes") or []
        key = (str(files[0]), int(page_a or 0), str(files[-1]), int(page_b or 0))
        with self._lock:
            self._diff_pairs[key] = {
                "round1_page": page_a,
                "round2_page": page_b,
                "fixed": counts["fixed_in_later_round"],
                "still_wrong": counts["still_wrong"],
                "unrequested": len(unrequested),
                "unrequested_zones": [z.get("id") for z in unrequested],
            }
        return notes

    def _store_round_status(self, finding_id: str, status: str, to_check: bool) -> None:
        with self._lock:
            self._diff_status[finding_id] = status
            if to_check:
                self._diff_unsure.add(finding_id)
            else:
                self._diff_unsure.discard(finding_id)
        current = next((f for f in self.book.live() if f["id"] == finding_id), None)
        if current is None:
            return
        updated = {**current, "round_status": status}
        if to_check:
            updated["trust_level"] = "to_check"
        if self.book.replace(updated):
            self._show_finding(updated)

    def _round_reread(
        self, finding: Mapping[str, Any], box: Mapping[str, Any] | None
    ) -> tuple[str, str | None]:
        """A changed spot: a second read of the round-2 crop, compared with the architect's value
        by code. `(round status, value read or None when unsure)`."""
        module = load_module(SECOND_READ_MODULE)
        checks = load_module(CHECKS_MODULE)
        if module is None or checks is None or box is None:
            return "still_wrong", None
        picture_id, _why = self.book._picture(box)
        if picture_id is None:
            return "still_wrong", None
        share = _Share(limit=self.settings.message_cap_micros)
        try:
            read = module.second_read(
                {"vendor": self.read_picture(picture_id)},
                str(finding.get("item") or ""),
                lambda content: self._second_read_call(content, share),
            )
        except (_NoRoom, _Cancelled, ProviderFailure, OSError):
            return "still_wrong", None
        except Exception as failed:  # noqa: BLE001 - an unread spot stays "still wrong"
            _log.warning("assistant chat round re-read failed: %s", type(failed).__name__)
            return "still_wrong", None
        value = read.vendor if getattr(read, "sure", True) else None
        try:
            new = checks.to_inches(value) if isinstance(value, str) else None
            wanted = checks.to_inches(str(finding.get("architect") or ""))
        except Exception:  # noqa: BLE001 - not a number: unsure
            new = wanted = None
        if new is None or wanted is None:
            return "still_wrong", None
        return ("fixed_in_later_round" if new == wanted else "still_wrong"), str(value)

    def _diff_rounds(self, report: dict[str, Any]) -> None:
        """The rounds line from the drawings compared by code (over the one from `round` fields)."""
        with self._lock:
            pairs = list(self._diff_pairs.values())
            statuses = dict(self._diff_status)
        if not pairs:
            return
        fixed = sum(1 for s in statuses.values() if s == "fixed_in_later_round")
        still = sum(1 for s in statuses.values() if s == "still_wrong")
        unrequested = sum(int(p["unrequested"]) for p in pairs)
        # The comment letter's words (Closed / Previous comment not addressed / New, #1766).
        line = (
            f"Round 1 comments: {fixed} of {fixed + still} closed · {still} previous "
            f"{'comment' if still == 1 else 'comments'} not addressed · "
            f"{unrequested} change{'' if unrequested == 1 else 's'} nobody asked for"
        )
        report["rounds"] = {
            "rounds": [1, 2],
            "earlier_items": fixed + still,
            "fixed": fixed,
            "still_wrong": still,
            "not_in_later_round": 0,
            "new": 0,
            "line": line,
            "unrequested": unrequested,
            "pairs": pairs,
        }

    def _ledger_tool(self, name: str, raw: object, by: str) -> ToolResult:
        """`record_string`, `record_reading`: no step on the screen; code's sum (or what other
        views of the item show) goes back to the model. A chat question's entries are kept in the
        set's proof ledger for the next full check (#1805)."""
        ledger = self.reading_ledger
        before = {e.id for e in (*ledger.strings, *ledger.readings)}
        text, is_error = ledger.run_tool(name, raw, by=by)
        if not is_error and self.settings.mode != "full_check" and isinstance(raw, Mapping):
            # A re-read (`replaces`) takes the place of the misread it corrects in the kept
            # ledger too, as in this answer's own (#1805).
            replaces = raw.get("replaces")
            replaced = self._kept_inputs.get(replaces) if isinstance(replaces, str) else None
            if set_proofs.keep_ledger_entry(
                self.tools.workspace.set_pack, name, raw, self._chat_name(), replaced
            ):
                after = {e.id for e in (*ledger.strings, *ledger.readings)}
                for entry_id in (after - before) or ({replaces} if replaces else set()):
                    self._kept_inputs[str(entry_id)] = set_proofs.ledger_input(raw)
        return ToolResult(content=[{"type": "text", "text": text}], step={}, is_error=is_error)

    def _record_items(self, raw: object, by: str) -> ToolResult:
        """`record_items` (#1821): many findings, strings and readings in one call. Each entry goes
        through its own tool (`report_finding`, `record_string`, `record_reading`), exactly as one
        call of it would, and is counted under that tool in the timings; the model gets one line
        per entry, in the order given. A wrong-shaped entry is refused on its own line, never
        stopping the others."""
        if not isinstance(raw, Mapping):
            return ToolResult(
                content=[{"type": "text", "text": "Refused: give findings, strings or readings."}],
                step={},
                is_error=True,
            )
        lines: list[str] = []
        recorded = 0
        for key, tool in BATCH_LISTS:
            entries = raw.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                lines.append(f"{key}: Refused: a list is needed.")
                continue
            for number, entry in enumerate(entries[:MAX_BATCH_ITEMS], start=1):
                self.timings.tool(tool)
                try:
                    if tool == "report_finding":
                        result = self._finding_tool(tool, entry, by)
                    else:
                        result = self._ledger_tool(tool, entry, by)
                except Exception:  # noqa: BLE001 - one entry never stops the others
                    _log.exception("assistant chat record_items entry failed")
                    result = ToolResult(
                        content=[{"type": "text", "text": f"Refused: {TOOL_FAILED_WORDS}"}],
                        step={},
                        is_error=True,
                    )
                # One line per entry: its tool's words with their line breaks folded.
                words = " ".join(
                    " ".join(str(block.get("text") or "").split())
                    for block in result.content
                    if isinstance(block, Mapping) and block.get("type") == "text"
                ).strip()
                recorded += 0 if result.is_error else 1
                lines.append(f"{key} {number}: {words}")
            if len(entries) > MAX_BATCH_ITEMS:
                lines.append(
                    f"{key}: only the first {MAX_BATCH_ITEMS} were recorded; send the other "
                    f"{len(entries) - MAX_BATCH_ITEMS} in another call."
                )
        if not lines:
            return ToolResult(
                content=[{"type": "text", "text": "Refused: give findings, strings or readings."}],
                step={},
                is_error=True,
            )
        return ToolResult(
            content=[{"type": "text", "text": "\n".join(lines)}], step={}, is_error=recorded == 0
        )

    def _chat_name(self) -> str | None:
        root = getattr(self.tools.workspace, "root", None)
        return None if root is None else Path(root).name

    def _keep_chat_proof(self, proof: Mapping[str, Any]) -> None:
        """A chat question's `verify_value` / `read_number` answer, kept in the set's proof
        ledger when it proved a value (#1805)."""
        set_proofs.keep_value(self.tools.workspace.set_pack, proof, self._chat_name())

    def replay_chat_proofs(self) -> int:
        """A full check's start (#1805): the strings and readings the set's chat questions
        recorded, replayed into this message's reading ledger (code adds them up again), so its
        sums and cross-view checks see them. Returns the entries replayed."""
        replayed = 0
        for entry in set_proofs.load(self.tools.workspace.set_pack)["ledger"]:
            tool = str(entry.get("tool") or "")
            try:
                _text, is_error = self.reading_ledger.run_tool(tool, entry.get("input"), by="chat")
            except Exception:  # noqa: BLE001 - one old entry never stops the check
                _log.info("assistant chat proof not replayed")
                continue
            replayed += 0 if is_error else 1
        if replayed:
            _log.warning("assistant full check: %d chat proofs replayed into the ledger", replayed)
        return replayed

    def carry_ledger(self, blocks: Sequence[Mapping[str, Any]]) -> int:
        """Continue (#1827): the strings and readings an earlier answer recorded (its trust
        block's `ledger_entries`), recorded again in this answer's reading ledger, so code adds
        them up again and its string check at the finish sees them as before. Returns the entries
        carried."""
        entries: Mapping[str, Any] = {}
        for block in blocks:
            trust = block.get("trust") if block.get("type") == "trust" else None
            if isinstance(trust, Mapping) and isinstance(trust.get("ledger_entries"), Mapping):
                entries = trust["ledger_entries"]
        fields = {
            "record_string": ("page", "view", "side", "parts", "printed_total", "box"),
            "record_reading": ("page", "view", "side", "item", "kind", "value", "box"),
        }
        carried = 0
        for key, tool in (("strings", "record_string"), ("readings", "record_reading")):
            for entry in entries.get(key) or []:
                if not isinstance(entry, Mapping):
                    continue
                given = {name: entry[name] for name in fields[tool] if entry.get(name) is not None}
                by = str(entry.get("by") or "earlier")
                _text, is_error = self.reading_ledger.run_tool(tool, given, by=by)
                carried += 0 if is_error else 1
        if carried:
            _log.warning("assistant full check: %d earlier ledger entries carried over", carried)
        return carried

    def _string_label(self, ref: object, page: int, side: str) -> Mapping[str, Any]:
        """A numbered label for `record_string` (part_dims, total_dim): on the vendor's file for
        the vendor's side, else on the architect's upload (the main file when there is one PDF)."""
        listing = self.book.listing
        upload = main_upload(listing) if side == "vendor" else architect_upload(listing)
        if upload is None:
            raise LookupError("the set has no PDF to look the label up in")
        return self.book.dim_labels.resolve(ref, str(upload["name"]), page)

    def _ledger_used(self) -> bool:
        ledger = self.reading_ledger
        return bool(ledger.strings or ledger.readings)

    def _ledger_findings(self, book: Any = None) -> None:
        """At finish: code's findings from the reading ledger (vendor sums that don't add up, the
        same item read differently across views, items one side shows and the other never does),
        added through the finding book like any finding, so they get the same checks. `book`: the
        late look's scoped book (`late_code_findings.LateBook`), else this answer's."""
        if not self._ledger_used():
            return
        book = self.book if book is None else book
        live = book.live()
        coverage = book.coverage_map() if book.coverage_used else None
        reported_sums = {
            (f.get("page"), str(f.get("printed_total") or "").strip()): f
            for f in live
            if f.get("source") == "vendor_arithmetic"
        }
        made: dict[Any, str] = {}
        pack = self._pack_documents()
        for extra in self.reading_ledger.ledger_findings(live, coverage):
            # The same item in two sizes on two vendor pages (#1906): red when code reads both
            # labels (which one is wrong is never guessed), else a Likely problem.
            extra = same_item_pages.settle(extra, pack[0], pack[1], book.main_file)
            entry = None
            if extra.get("source") == "vendor_arithmetic":
                entry = next(
                    (e for e in self.reading_ledger.strings if e.id in extra.get("ledger_ids", [])),
                    None,
                )
                if entry is not None:
                    extra = {**extra, "sum_text": entry.sum_text()}
                key = (extra.get("page"), str(extra.get("printed_total") or "").strip())
                reported = reported_sums.get(key)
                if reported is not None:
                    # Already reported: code's sum goes on that finding instead (#1515).
                    if entry is not None and not reported.get("sum_text"):
                        backed = {
                            **reported,
                            "sum_text": extra["sum_text"],
                            "ledger_ids": list(reported.get("ledger_ids") or [])
                            or [str(i) for i in extra.get("ledger_ids") or []],
                        }
                        if book.replace(backed):
                            self._show_finding(backed)
                    continue
            added = book.add_code_finding(extra)
            if added is None:
                continue
            if extra.get("source") == "vendor_arithmetic" and self._fold_same_string(added, made):
                continue
            kept: dict[str, Any] = {
                k: extra[k]
                for k in (same_item_pages.FIELD, likely_problems.FIELD)
                if k in extra and k not in added
            }
            if kept:
                # The fields the book does not record itself, laid over it, and judged again.
                updated = judged({**added, **kept})
                if book.replace(updated):
                    added = updated
            self._show_finding(added)

    def _pack_documents(self) -> tuple[Any, Any]:
        """The set pack's labels (`dims.json`) and page map, for code's own reads at the ledger's
        spots; `None` for one that is not there."""
        folder = getattr(self.tools.workspace, "set_pack", None)
        if folder is None:
            return None, None
        try:
            dims = json.loads((Path(folder) / "dims.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dims = None
        try:
            from app.review.assistant.full_check_plan import read_page_map

            page_map = read_page_map(Path(folder), assume=True, main_file=self.book.main_file)
        except Exception:  # noqa: BLE001 - no rounds known: every pair is judged as one round
            page_map = None
        return dims, page_map

    def _fold_same_string(self, added: Mapping[str, Any], made: dict[Any, str]) -> bool:
        """#1805: the same vendor string read twice on one page (the left and the right copy of
        one elevation's height string: the same parts and the same overall) is one item. The
        second is folded into the first, listed on it (`also_on`, so the drawing marks both
        spots); True when folded. `made`: the strings added so far, by their key."""
        from app.review.assistant.decision_resolver import _also

        book = self.book
        key = (
            added.get("page"),
            str(added.get("printed_total") or "").strip(),
            tuple(str(p).strip() for p in added.get("parts") or ()),
        )
        first_id = made.get(key)
        if first_id is None:
            made[key] = str(added["id"])
            return False
        with self.finding_lock:
            first = next((f for f in book.live() if f["id"] == first_id), None)
            if first is None or not book.absorb(str(added["id"])):
                made[key] = str(added["id"])
                return False
            joined = {**first, "also_on": [*(first.get("also_on") or []), _also(added)]}
            book.replace(joined)
        self._show_finding(joined)
        self._show_dropped(
            str(added["id"]),
            f"The same string as {first_id} (the same parts and overall on this page): one item.",
        )
        return True

    def _ledger_trust(self, report: dict[str, Any]) -> None:
        """The ledger's counts (and everything it recorded, for a reload) in the trust report."""
        report.update(ledger=None, ledger_entries=None, recorded_results=None)
        if self._ledger_used():
            book = self.book
            coverage = book.coverage_map() if book.coverage_used else None
            live = [f for f in book.live() if f.get("created_by") != "ledger"]
            report["ledger"] = self.reading_ledger.ledger_report(live, coverage)
            report["ledger_entries"] = self.reading_ledger.as_dict()
            # Every box names its file (null when the model gave none), as the stored shape does.
            entries = report["ledger_entries"]
            for entry in [
                *report["ledger"]["strings"]["off_list"],
                *entries["strings"],
                *entries["readings"],
            ]:
                if isinstance(entry.get("box"), dict):
                    entry["box"].setdefault("file", None)
        check = self._recorded_results_check()
        if check is not None:
            report["recorded_results"] = {
                **{key: check[key] for key in ("recorded", "included", "dismissed", "missing")},
                "also_left_out": self._ledger_extra_missing(check),
            }

    def _ledger_extra_missing(self, check: Mapping[str, Any] | None) -> list[dict[str, Any]]:
        """The recorded results the ledger's check finds left out that GV Review's recorded
        differences (the check the guard already runs) do not know at all (e.g. the review list's
        failures)."""
        recorded = self._recorded
        known: set[str] = set()
        if recorded is not None:
            known |= {str(item["record_id"]) for item in recorded.differences}
            known |= set(recorded.results)
        extra: list[dict[str, Any]] = []
        for item in (check or {}).get("missing") or []:
            key = str(item.get("key"))
            ids = {key, *(f"ai-review:p{page}:{key}" for page in item.get("pages") or [])}
            if ids & known:
                continue  # the guard's own check already counts this one
            known.add(key)
            extra.append(dict(item))
        return extra

    def _ledger_dismissed(self) -> dict[str, str]:
        """The lead's dismissals keyed both ways: a record id, and an AI page review row's key."""
        out: dict[str, str] = {}
        for record_id, reason in self.book.dismissed.items():
            out[record_id] = reason
            if record_id.startswith("ai-review:p") and ":" in record_id[len("ai-review:p") :]:
                out.setdefault(record_id.split(":", 2)[2], reason)
        return out

    def _recorded_results_check(self) -> dict[str, Any] | None:
        """The ledger's "never drop a recorded result" check (whole-set work only; not a full
        check on picked pages: the ledger's document is the whole set's)."""
        if not self._whole_set_work() or self.settings.pages:
            return None
        self._recorded_checks()
        with self._lock:
            document = self._recorded_document
        if document is None:
            return None
        try:
            return recorded_results_check(document, self.book.live(), self._ledger_dismissed())
        except Exception as failed:  # noqa: BLE001 - the existing check still runs
            _log.warning("assistant chat recorded results check failed: %s", type(failed).__name__)
            return None

    def _second_read_call(self, content: list[dict[str, Any]], share: _Share) -> str:
        """One second read: the cheap model, no tools, within the message's caps."""
        model = BY_SHORT[SECOND_READ_MODEL]
        thinking = thinking_config(model, False)
        estimate = self._price(model, _SECOND_READ_PROMPT_TOKENS, SECOND_READ_MAX_TOKENS)
        if not self._fits(estimate) or not self._hold(share, estimate):
            raise _NoRoom
        try:
            reply = self._call(
                model=model,
                effort=SECOND_READ_EFFORT,
                thinking=thinking,
                system=SECOND_READ_SYSTEM,
                tools=(),
                messages=[{"role": "user", "content": content}],
                final=False,
                max_tokens=SECOND_READ_MAX_TOKENS,
                prompt_id=self.check_prompt_id,
                display=None,
                output_format=getattr(load_module(SECOND_READ_MODULE), "READ_SCHEMA", None),
                enforce_format=True,
            )
        finally:
            self._release(share, estimate or 0)
        if reply.stop_reason == "refusal":
            raise ProviderFailure("declined", status=None, retryable=False)
        return reply.text()

    # ---- the checks of findings: one bounded pool (#1512) ------------------------------------------

    def _submit_check(self, phase: str, job: Callable[[], None]) -> None:
        """Runs `job` in the message's checks pool (`CHECK_WORKERS` at once), timed as `phase`."""

        def run() -> None:
            with self.timings.phase(phase):
                try:
                    job()
                except Exception as failed:  # noqa: BLE001 - one check failing never stops others
                    _log.warning("assistant chat %s failed: %s", phase, type(failed).__name__)

        with self._lock:
            if self._checks_pool is None:
                self._checks_pool = ThreadPoolExecutor(
                    max_workers=CHECK_WORKERS, thread_name_prefix="gv-chat-check"
                )
            self._checks.append(self._checks_pool.submit(run))

    def _wait_checks(self) -> None:
        """Waits for every check started so far, and for the ones they start in turn."""
        while True:
            with self._lock:
                pending = [future for future in self._checks if not future.done()]
            if not pending:
                return
            wait(pending, return_when=FIRST_COMPLETED)

    def _close_checks(self) -> None:
        self._wait_checks()
        with self._lock:
            pool, self._checks_pool = self._checks_pool, None
            self._checks = []
        if pool is not None:
            pool.shutdown(wait=True)

    def _check_budget(self) -> Budget:
        with self._lock:
            if self._checks_budget is None:
                self._checks_budget = Budget(self)
            return self._checks_budget

    def _third_read_budget(self) -> ThirdReadBudget:
        """The third reads' money: the checks' budget, at most `ADJUDICATION_CAP_MICROS` of it."""
        inner = self._check_budget()
        with self._lock:
            if self._third_budget is None:
                module = load_module(ADJUDICATION_MODULE)
                cap = int(getattr(module, "ADJUDICATION_CAP_MICROS", 0) or 0)
                self._third_budget = ThirdReadBudget(inner, cap)
            return self._third_budget

    def _update_finding(
        self, finding_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> None:
        """A check's result on a finding: read again, changed and stored under the finding lock,
        its trust level worked out again from its checks."""
        with self.finding_lock:
            current = next((f for f in self.book.live() if f["id"] == finding_id), None)
            if current is None:
                return
            updated = change(dict(current))
            updated["trust_level"] = trust_level(updated["checks"], updated.get("status"))
            if self.book.replace(updated):
                self._show_finding(updated)

    def _queue_second_reads(
        self, findings: Sequence[Mapping[str, Any]], *, final: bool = False
    ) -> None:
        """Starts the second reads of these findings that have a spot or a picture and no read
        yet, the ones a red could come from first (`could_go_red`), then differences. Every
        would-be red is read (Anant, 2026-10-28), up to `READ_HARD_CEILING` readings for the
        message; the others share `SECOND_READ_LIMIT` places, and only once every finding is in
        (the `final` queue). A would-be red past the ceiling is marked "not double-read"."""
        module = load_module(SECOND_READ_MODULE)
        if module is None:
            return
        chosen = [
            f
            for f in findings
            if any(
                f.get(key)
                for key in ("arch_picture_id", "vendor_picture_id", "arch_box", "vendor_box")
            )
            and (f.get("checks") or {}).get("second_read", "not_run") in (None, "not_run")
        ]
        chosen.sort(key=_reads_first)
        for finding in chosen:
            if _count_only(finding):
                # No printed number to copy: never read; a would-be red is held, never red.
                self._not_double_read(finding, COUNT_WHY)
                continue
            finding_id = str(finding["id"])
            key = (finding_id, _read_key(finding))
            red = could_go_red(finding)
            if not red and _nothing_to_copy(finding):
                continue  # words on every side: a reading could never agree, nor take a place
            with self._lock:
                if key in self._read_queued:
                    continue
                if not red and not final:
                    continue  # the reads go to would-be reds first, the rest once all are in
                full = len(self._read_queued) >= READ_HARD_CEILING
                if not red and (
                    full
                    or (
                        finding_id not in self._others_read
                        and len(self._others_read) >= SECOND_READ_LIMIT
                    )
                ):
                    continue
                if not full:
                    self._read_queued.add(key)
                    if not red:
                        self._others_read.add(finding_id)
            if full:
                self._not_double_read(
                    finding, f"past the {READ_HARD_CEILING} readings one check makes at most"
                )
                continue
            self._submit_check("second_reads", _bound(self._second_read_one, module, finding))

    def _reads_twice(self) -> bool:
        """This answer reads each would-be red twice: not the simple full check (it reads once
        by design), and only where the reading module is there."""
        return not self._simple and load_module(SECOND_READ_MODULE) is not None

    def _unread_why(self) -> str:
        return STOPPED_WHY if self.cancel.is_set() else NOT_REACHED_WHY

    def _mark_unread_reds(self) -> None:
        """Each would-be red still without its second reading, and with no reason yet, says why
        ("not double-read"), so it is never red (Anant, 2026-10-28). Run after the finish's
        steps that can make a would-be red, and again right before the files and the stored
        answer (`seal`): no later step may leave one unmarked. Not in the simple full check."""
        if not self._reads_twice():
            return
        why = self._unread_why()
        for finding in self.book.live():
            if _unread_red(finding):
                self._not_double_read(finding, why)

    def _not_double_read(self, finding: Mapping[str, Any], why: str) -> None:
        """A would-be red whose second reading could not be made: why, on the finding
        (`reading_routes.NOT_DOUBLE_READ`), so it is never red but a please-check that says so;
        a Continue reads it."""
        if not could_go_red(finding):
            return
        with self.finding_lock:
            current = next((f for f in self.book.live() if f["id"] == finding["id"]), None)
            if current is None:
                return
            if (current.get("checks") or {}).get("second_read") not in (None, "not_run"):
                return  # read meanwhile
            if current.get(NOT_DOUBLE_READ) == why:
                return
            updated = {**current, NOT_DOUBLE_READ: why}
            if not self.book.replace(updated):
                return
        self._show_finding(updated)

    def reading_pictures(self, finding: Mapping[str, Any]) -> read_spots.ReadPictures:
        """The second and third readings' pictures of `finding` (`read_spots`, 2026-10-28): each
        side's read spot drawn without GV's markup from the set pack's drawings-only copy (else
        the upload, GV's layers dropped), the item's own label at the centre with both ends of
        its dimension around it; a vendor string's printed overall. Drawn once per finding and
        what it reads."""
        key = (str(finding.get("id")), _read_key(finding))
        with self._lock:
            kept = self._read_pictures.get(key)
        if kept is not None:
            return kept
        workspace = self.tools.workspace
        pack = workspace.set_pack
        main = self.book.main_file
        labels = self.book.dim_labels

        def labels_of(file: str, page: int) -> Any:
            return labels.on_page(file, page, read_now=False)

        def pdf_for(file: str | None) -> Path | None:
            name = file or main
            if name is None:
                return None
            return read_spots.source_pdf(pack, workspace.uploads, name)

        drawing = import_module("app.review.assistant.chat_drawing")
        try:
            with drawing._PDFIUM_LOCK:
                drawn = read_spots.draw(finding, labels_of=labels_of, pdf_for=pdf_for)
        except Exception as failed:  # noqa: BLE001 - no clean picture: the evidence crops then
            _log.warning("assistant chat reading picture failed: %s", type(failed).__name__)
            drawn = read_spots.ReadPictures(crops={}, spots={})
        with self._lock:
            self._read_pictures[key] = drawn
        return drawn

    def _second_read_one(self, module: Any, finding: Mapping[str, Any]) -> None:
        """One finding's second read; kept only when the finding still shows what was read. A
        read that disagrees or is unsure starts its third read: at once on a would-be red, else
        once third reads are open (`_queue_third_read`)."""
        if self.cancel.is_set():
            self._not_double_read(finding, STOPPED_WHY)
            return
        drawn = self.reading_pictures(finding)
        # The same pictures as the third reading's (`SideCrops.pictures`): a value written up the
        # page turned upright, a long thin region's close-up (2026-10-28).
        crops: dict[str, list[tuple[str, bytes]]] = drawn.for_reading()
        if not crops:
            # No clean picture could be drawn (no box on its page, no file): the evidence crops.
            for side, picture in (
                ("architect", "arch_picture_id"),
                ("vendor", "vendor_picture_id"),
            ):
                if finding.get(picture):
                    try:
                        crops[side] = [("plain", self.read_picture(str(finding[picture])))]
                    except OSError:
                        continue
        if not crops:
            self._not_double_read(finding, "no picture of its spot could be drawn")
            return
        item = str(finding.get("item") or "")
        words = _asked_words(module.second_read, drawn)
        slot = read_spots.crop_hash(
            [
                part
                for side in sorted(crops)
                for kind, png in crops[side]
                for part in (side.encode("ascii"), kind.encode("ascii"), png)
            ],
            f"{item}\n{sorted(words.items())}",
        )
        with self._lock:
            state = self._read_slots.get(slot)
            if state is None:
                self._read_slots[slot] = _IN_FLIGHT
                self._read_waiting[slot] = []
            elif state is _IN_FLIGHT:
                # The same pictures and question are being read now: its answer serves this too.
                self._read_waiting[slot].append(finding)
                return
        if state is not None:
            self._apply_second_read(module, finding, state)
            return
        why: str | None = None
        read: Any = None
        try:
            read = module.second_read(
                crops,
                item,
                lambda content: self._second_read_call(content, self._read_share),
                **words,
            )
        except _NoRoom:
            why = "the readings' share of the fixed price was used up"
        except _Cancelled:
            why = STOPPED_WHY
        except ProviderFailure as failure:
            why = _failure_why(failure)
        except Exception as failed:  # noqa: BLE001 - a failed read is "not run"
            _log.warning("assistant chat second read failed: %s", type(failed).__name__)
            why = "the reading failed"
        with self._lock:
            waiting = self._read_waiting.pop(slot, [])
            if why is None:
                self._read_slots[slot] = read
            else:
                self._read_slots.pop(slot, None)  # never made: it may be asked again
        for one in (finding, *waiting):
            if why is None:
                self._apply_second_read(module, one, read)
            else:
                self._not_double_read(one, why)

    def _apply_second_read(self, module: Any, finding: Mapping[str, Any], read: Any) -> None:
        """A reading's answer on one finding: compared with its values by code, kept only when
        the finding still shows what was read. A read that disagrees or is unsure starts its
        third read."""
        try:
            read = module.compare_read(read, finding)
        except Exception as failed:  # noqa: BLE001 - a failed comparison is "not run"
            _log.warning("assistant chat second read not compared: %s", type(failed).__name__)
            self._not_double_read(finding, "the reading failed")
            return
        result = str(read.result)
        if result not in ("agrees", "disagrees", "unsure"):
            result = "unsure"
        values = {"architect": read.architect, "vendor": read.vendor}
        reply = getattr(read, "reply", None)
        if isinstance(reply, str):
            # The reply's own text (cut, `second_read.kept_reply`): a read of nothing says why.
            values["reply"] = reply
        key = _read_key(finding)
        with self.finding_lock:
            current = next((f for f in self.book.live() if f["id"] == finding["id"]), None)
            if current is None or _read_key(current) != key:
                return  # changed (or dropped) while it was read: read again later
            self._reads[str(current["id"])] = (key, result, values)
            updated = {**with_second_read(current, result), "second_read_values": values}
            updated.pop(NOT_DOUBLE_READ, None)
            if not self.book.replace(updated):
                return
            self._show_finding(updated)
        if result in ("disagrees", "unsure"):
            self._queue_third_read(updated)

    def _queue_third_read(self, finding: Mapping[str, Any]) -> None:
        """Starts one finding's third read, once for what its second read read. A would-be red
        (`could_go_red`) gets one at once, up to `THIRD_READ_RED_LIMIT`; any other finding only
        once third reads are open (the lead has answered) and while fewer than
        `third_read_limit()` third reads were started in all, so the would-be reds' come first."""
        finding_id = str(finding["id"])
        key = (finding_id, _read_key(finding))
        red = could_go_red(finding)
        if not needs_third_read(finding):
            return
        budget = self._third_read_budget()
        with self._lock:
            if key in self._third_queued:
                return
            if red:
                if len(self._third_queued) >= THIRD_READ_RED_LIMIT:
                    return
            elif not self._third_reads_open or len(self._third_queued) >= third_read_limit():
                return
            job = third_read_job(self, finding, budget, self._third_read_done(key[1]))
            if job is None:
                return
            self._third_queued.add(key)
        self._submit_check("third_reads", job)

    def _third_read_done(
        self, key: tuple[Any, ...]
    ) -> Callable[[str, Callable[[dict[str, Any]], dict[str, Any]]], None]:
        """A third read's result on its finding: kept only while the finding still shows what was
        read (`key`), and remembered so the finding's next version keeps it (`_checked`)."""

        def update(finding_id: str, change: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
            with self.finding_lock:
                current = next((f for f in self.book.live() if f["id"] == finding_id), None)
                if current is None or _read_key(current) != key:
                    return  # changed (or dropped) while it was read
                updated = change(dict(current))
                updated["trust_level"] = trust_level(updated["checks"], updated.get("status"))
                outcome = str((updated.get("checks") or {}).get("adjudication") or "unsure")
                self._thirds[finding_id] = (key, outcome, list(updated.get("reads") or []))
                if self.book.replace(updated):
                    self._show_finding(updated)

        return update

    def _post_checks(self) -> int:
        """After the lead's answer: the second reads still to make, the third reads (each one
        started as its second read comes back) and the critic, all in one bounded pool. Returns
        how many findings with evidence pictures were left without a second read."""
        book = self.book
        findings = book.live()
        if findings:
            self._third_reads_open = True
            for job in critic_jobs(self, findings, self._check_budget(), self._update_finding):
                self._submit_check("critic", job)
            # The would-be reds' first (most were started as their second reads came back),
            # then the rest, differences first.
            for finding in sorted(findings, key=_reads_first):
                self._queue_third_read(finding)
            self._queue_second_reads(findings, final=True)
        self._close_checks()
        with_pictures = [
            f for f in book.live() if f.get("arch_picture_id") or f.get("vendor_picture_id")
        ]
        if load_module(SECOND_READ_MODULE) is None:
            return len(with_pictures)
        return sum(
            1
            for f in with_pictures
            if (f.get("checks") or {}).get("second_read") in (None, "not_run")
            and (could_go_red(f) or not _nothing_to_copy(f))
        )

    def _accuracy(self) -> None:
        """The answer's accuracy, by code (CONTRACT5): a block and the `accuracy` event."""
        book = self.book
        whole_set = self._whole_set_work()
        score = accuracy(
            book.live(),
            list(book.cites),
            book.coverage_map() if whole_set else None,
            self._unknowns,
        )
        index = self._add_block({"type": "accuracy", "accuracy": score})
        self.emit("accuracy", {"index": index, "accuracy": json.loads(json.dumps(score))})

    def _trust_phase(self) -> None:
        """After the lead's answer: the checks of the findings, round links, then the trust report."""
        book = self.book
        if self._progress is not None:
            self._progress.phase("checking")
        self.add_code_findings()
        if not book.live() and not book.coverage_used and not self._ledger_used():
            self._close_checks()
            return
        if self._ledger_used():
            # The reading ledger as a non-AI witness for the lead's own findings.
            ledger = self.reading_ledger.as_dict()
            for finding in book.live():
                backed = with_ledger(finding, ledger)
                if backed != finding and book.replace(backed):
                    self._show_finding(backed)
        # The drawn-ink reads (#1531, $0 OCR) run while the model checks run.
        with ThreadPoolExecutor(max_workers=1) as ink:
            drawn = ink.submit(
                drawn_ink_checks,
                book.live(),
                book.pdf_paths(),
                stop=self.cancel.is_set,
                count=self.count_items,
            )
            # The simple full check makes no second or third reads and has no critic: its two AI
            # roles are the compare call and the blind look (CONTRACT-SIMPLE).
            skipped = 0 if self._simple else self._post_checks()
        inks = drawn.result()
        self.items_counted()
        with self.finding_lock:
            for inked in with_drawn_ink(book.live(), inks):
                if book.replace(inked):
                    self._show_finding(inked)
        failed = sum(1 for answer in inks.values() if FAILED_KEY in answer)
        if failed:
            # Those items are Not compared, with the reason (#1850); one read is not a step.
            _log.warning("assistant chat drawn-ink reads failed on %d findings", failed)
        with self.timings.phase("finish_checks"):
            self._finish_trust(skipped)

    def add_code_findings(self) -> None:
        """The finish's code findings, $0, once per answer: the reading ledger's, the vendor
        consistency check's (#1584), the finish check's (#1623), code's own pairs, GV's site-fit rule
        on the vendor's runs (`run_fit`), GV's filler range on the vendor's fillers
        (`filler_range`) and GV's sink rules on the vendor's plans (`sink_offset`). A roster
        full check adds them before its proof steps (#1641), so its blind reads and devil's
        advocate see them; the trust phase then does not add them again."""
        if self._code_findings_added:
            return
        self._code_findings_added = True
        steps: tuple[tuple[str, Callable[[], object]], ...] = (
            (CODE_CHECK_NAMES["ledger"], self._ledger_findings),
            # A full check's vendor consistency check (#1584).
            (CODE_CHECK_NAMES["consistency"], lambda: add_consistency(self)),
            # Missed, changed and added finishes (#1623).
            (CODE_CHECK_NAMES["architect_items"], lambda: add_architect_items(self)),
            # Dimensions code paired by itself, Wrong or Right.
            (CODE_CHECK_NAMES["code_pairs"], lambda: add_code_pairs(self)),
            # GV's site-fit rule on the vendor's runs (`run_fit`).
            (CODE_CHECK_NAMES["run_fit"], lambda: add_run_fit(self)),
            # GV's filler range on the vendor's fillers.
            (CODE_CHECK_NAMES["filler_range"], lambda: add_filler_range(self)),
            # GV's sink rules on the vendor's plans (#1807, #1906), pages with no architect too.
            (CODE_CHECK_NAMES["sink_offset"], lambda: add_sink_offset(self)),
            # The sink's centre against the sink base's centre (fix 3, 2026-10-25).
            (CODE_CHECK_NAMES["sink_centre"], lambda: add_sink_centre(self)),
            # A splash the paired drawing prints no size for: looked for on the item's other
            # views (its section, its elevation; 2026-10-23), before the measuring by scale.
            (CODE_CHECK_NAMES["splash_views"], lambda: add_splash_views(self)),
            # Where the architect prints no size: measured by its own scale (2026-10-23), last,
            # on every item the steps above left open.
            (CODE_CHECK_NAMES["scale_measure"], lambda: add_scale_measures(self)),
            # A tag only one sheet prints, or tagged differently on the item's other drawing
            # (2026-10-29, `set_context`): please-checks, never red.
            (CODE_CHECK_NAMES["set_check"], lambda: add_set_check(self)),
        )
        # The pack's state before the checks read its labels counts too: it may turn `ready` while
        # they run on partial labels (#1768 review).
        unlabelled = pack_without_labels(self.tools.workspace.set_pack)
        self._run_code_steps(steps)
        if self.settings.mode != "full_check" or self._simple:
            return  # the simple check names its broken steps once, with its other reasons
        if unlabelled or pack_without_labels(self.tools.workspace.set_pack):
            # The roster and the lead go on without labels; code's pairs are then missing.
            self.code_checks_failed.append(LABELS_STEP)
        if self.code_checks_failed:
            # A check that crashed took its reds with it: the check is not for sending (S0).
            self.incomplete_note = "; ".join(
                part
                for part in (self.incomplete_note, broken_steps_note(self.code_checks_failed))
                if part
            )

    def _run_code_steps(self, steps: Sequence[tuple[str, Callable[[], object]]]) -> None:
        """Code's finding steps in order (`add_code_findings`, `add_late_code_findings`): a step
        that fails, or leaves a finding of code's own out for room, is named in
        `code_checks_failed` (the check is then not for sending)."""
        with self.timings.phase("finish_checks"):
            for name, step in steps:
                if self.cancel.is_set() and name != CODE_CHECK_NAMES["ledger"]:
                    # Stop (2026-10-23): the reading ledger's own sums (the work the helpers
                    # recorded, milliseconds) are still added; no other check starts (each reads
                    # the whole set again, a minute on a big one): a Continue runs them all.
                    break
                left_out = self.book.left_out_for_room.get("code", 0)
                try:
                    step()
                except Exception:  # noqa: BLE001 - the answer goes on, never sendable (#1768)
                    _log.exception("assistant chat code check failed: %s", name)
                    self.code_checks_failed.append(name)
                    continue
                if self.book.left_out_for_room.get("code", 0) > left_out:
                    # A finding of code's own left out for room took its red with it (#1827).
                    _log.warning("assistant chat code check left findings out: %s", name)
                    self.code_checks_failed.append(name)

    def add_late_code_findings(self, pages: Collection[int], before: Collection[str]) -> list[str]:
        """Code's own findings ($0) on the pages the late look compared (2026-10-28), after its
        helpers and before its proof steps: the reading ledger's sums of the late strings, the
        consistency check's judgement of them, GV's site-fit, filler and sink rules on the late
        items' labels, the splash's other views and the measuring by scale of the late items.
        Scoped to the late look's items and pages (`late_code_findings`): a first-round item is
        never added, changed or asked again. `before`: the ids live before the late look. Returns
        the ids added. Nothing when the first round's code findings were never made (the finish
        then makes them all)."""
        if not self._code_findings_added or not pages:
            return []
        from app.review.assistant import late_code_findings

        steps, added = late_code_findings.late_steps(
            self,
            pages,
            before,
            names=CODE_CHECK_NAMES,
            ledger=self._ledger_findings,
            checks={
                "consistency": add_consistency,
                "run_fit": add_run_fit,
                "filler_range": add_filler_range,
                "sink_offset": add_sink_offset,
                "splash_views": add_splash_views,
                "scale_measure": add_scale_measures,
            },
        )
        failed = len(self.code_checks_failed)
        self._run_code_steps(steps)
        broken = self.code_checks_failed[failed:]
        if broken and self.settings.mode == "full_check" and not self._simple:
            # A late step that crashed took its reds with it: the check is not for sending (S0).
            self.incomplete_note = "; ".join(
                part for part in (self.incomplete_note, broken_steps_note(broken)) if part
            )
        return list(added)

    def _code_finish(self) -> None:
        """A stopped full check's finish, by code only: no model call."""
        self._close_checks()  # second reads started before the stop end at once (cancelled)
        book = self.book
        self.add_code_findings()
        with self.timings.phase("finish_checks"):
            if self._ledger_used():
                ledger = self.reading_ledger.as_dict()
                for finding in book.live():
                    backed = with_ledger(finding, ledger)
                    if backed != finding and book.replace(backed):
                        self._show_finding(backed)
            if not book.live() and not book.coverage_used and not self._ledger_used():
                return
            with_pictures = [
                f for f in book.live() if f.get("arch_picture_id") or f.get("vendor_picture_id")
            ]
            skipped = sum(
                1
                for f in with_pictures
                if (f.get("checks") or {}).get("second_read") in (None, "not_run")
            )
            self._finish_trust(skipped)

    def _finish_trust(self, skipped: int) -> None:
        self._fold_same_wrongs()
        self._stamp_reference_pages()
        self._guard_spots()
        self._resolve_decisions()
        self._escalate()
        self._drop_off_pair()
        self._mark_unread_reds()
        book = self.book
        statuses = round_statuses(book.live())
        with self._lock:
            statuses.update(self._diff_status)
            unsure = set(self._diff_unsure)
        for finding in book.live():
            status = statuses.get(finding["id"])
            updated = dict(finding)
            if status is not None:
                updated["round_status"] = status
            if finding["id"] in unsure:
                # A changed spot the re-read could not read: a person should look.
                updated["trust_level"] = "to_check"
            if updated != finding and book.replace(updated):
                self._show_finding(updated)
        coverage = book.coverage_map() if book.coverage_used else None
        # Not seen is not right: a "same" item counts only when both sides were seen in a sharp
        # enough zoom crop, the lead's or a helper's (they share `self.tools`, so its log holds
        # every crop of this answer).
        for updated in mark_compared(book.live(), coverage, list(self.tools.seen)):
            if book.replace(updated):
                self._show_finding(updated)
        self._one_card(coverage)
        scope = self.scope()
        recorded = scoped_recorded(self._recorded_checks(), book.scope_pages)
        self._unknowns = unknown_records(
            book.live(),
            coverage,
            waiting=recorded.waiting,
            refused_diffs=list(self.refused_diffs),
            set_gaps=self.consistency_gaps,
        )
        if self._unknowns:
            index = self._add_block({"type": "unknowns", "unknowns": self._unknowns})
            self.emit(
                "unknowns", {"index": index, "unknowns": json.loads(json.dumps(self._unknowns))}
            )
        report = trust_report(
            [f for f in book.live() if in_scope(scope, f.get("page"))],
            coverage,
            recorded,
            second_reads_skipped=skipped,
            dismissed=book.dismissed,
            unknowns=self._unknowns,
        )
        report["scope"] = scope
        refused = self.book.left_out_for_room.get("ai", 0)
        if refused:
            # Never silent (#1827): what the AI found and the full book refused.
            report["not_checked"] = [
                *(report.get("not_checked") or []),
                (
                    f"{refused} findings the AI found were not recorded: one answer holds at "
                    f"most {self.book.finding_cap} AI findings."
                ),
            ]
        self._ledger_trust(report)
        self._diff_rounds(report)
        # Always present (null when not counted), so the live event and the stored block agree.
        report["reading"] = self._reading_report()
        # A roster full check fills it at its end (`check_problems`); null on other answers.
        report["problems"] = None
        index = self._add_block({"type": "trust", "trust": report})
        self._trust_index = index
        self.emit("trust", {"index": index, "trust": json.loads(json.dumps(report))})

    def _reading_report(self) -> dict[str, Any] | None:
        """A full check's reading coverage (#1862): how many printed numbers the set pack's labels
        read, from the pack's `reading_coverage.json`; None on other answers or an older pack.
        Shown and filed only: never an input to a finding or a verdict."""
        if self.settings.mode != "full_check":
            return None
        try:
            return reading_coverage.report(reading_coverage.load(self.tools.workspace.set_pack))
        except Exception:  # noqa: BLE001 - the answer goes on without it
            _log.exception("reading coverage could not be shown")
            return None

    def _one_card(self, coverage: Mapping[str, Any] | None) -> None:
        """One item, one card (2026-10-23): a full check's last step, after every other step and
        before the trust report, the counts and the files (`one_card.fold`). The copies of one
        item (by code facts only) become one card, the others kept on it as evidence; the items
        that only say "no architect drawing" on a Drawing-needed page fold under that card."""
        if self.settings.mode != "full_check":
            return
        from app.review.assistant.full_check_plan import unpaired_note_why

        # Only pages the result really lists under Drawing needed (their coverage note says so):
        # an item folded under a page the screen does not show there would be seen nowhere.
        needed: set[int] = set()
        for entry in (coverage or {}).get("pages") or []:
            if isinstance(entry, Mapping) and unpaired_note_why(entry.get("note")) is not None:
                needed.add(int(entry["page"]))
        try:
            result = one_card.fold(self.book.live(), needed)
        except Exception:  # noqa: BLE001 - the answer keeps its findings as they were
            _log.exception("assistant chat one-card fold failed")
            return
        for kept in result.kept:
            if self.book.replace(kept):
                self._show_finding(kept)
        for page, copies in result.drawing_needed.items():
            self.book.fold_into_page(page, copies)
        for folded in result.folded:
            with self.finding_lock:
                if not self.book.absorb(folded.drop):
                    continue
            self._show_dropped(folded.drop, folded.reason)
        if result.drawing_needed and self.book.coverage_used:
            self._show_coverage()

    def _fold_same_wrongs(self) -> None:
        """The same Wrong item reported more than once (two helpers, or a helper and a gap
        closer): kept once, before the resolver, the counts and the files (#1622,
        `full_check_lenses.same_wrongs`), so no letter, register or cloud drawing repeats it; then
        different items left under one name are named apart (`names_apart`)."""
        try:
            folds = same_wrongs(self.book.live())
        except Exception:  # noqa: BLE001 - the answer keeps its findings as they were
            _log.exception("assistant chat same-item fold failed")
            return
        for fold in folds:
            with self.finding_lock:
                if not self.book.absorb(fold.drop):
                    continue
            self._show_dropped(fold.drop, fold.reason)
        # Different items left under one name (two strings of one elevation): each named by its
        # place on the page, so the letter and the register never show two identical lines.
        with self.finding_lock:
            live = {str(f["id"]): f for f in self.book.live()}
            for finding_id, name in names_apart(list(live.values())).items():
                renamed = {**live[finding_id], "item": name}
                if self.book.replace(renamed):
                    self._show_finding(renamed)

    @property
    def _simple(self) -> bool:
        """The simple full check (CONTRACT-SIMPLE): no resolver, no escalation, no second reads."""
        return self.settings.mode == "full_check" and self.settings.full_check_mode == "simple"

    def _resolve_decisions(self) -> None:
        """Whole-set work (#1554): code settles the "Your decision" items the drawings answer
        (`decision_resolver`) before the trust report, the files and the summary. Not in the
        simple full check: code verified each of its items already."""
        if not self._whole_set_work() or self._simple:
            return
        try:
            self._settled = resolve_answer(self)
        except Exception:  # noqa: BLE001 - the answer keeps its decision items as they were
            _log.exception("assistant chat decision resolver failed")

    def _escalate(self) -> None:
        """A full check (#1619): the free code steps on every item the resolver left undecided
        (`escalation`), after the resolver and before the trust report and the files; what is
        still open then is Not compared, with its reason (the Not-compared list)."""
        if self.settings.mode != "full_check" or not self._whole_set_work() or self._simple:
            return
        try:
            self._escalation = escalate_answer(self)
        except Exception:  # noqa: BLE001 - the answer keeps its items as they were
            _log.exception("assistant chat escalation failed")

    def _guard_spots(self) -> None:
        """A full check (#1650): an item whose values code cannot find printed, or read at a spot
        on the drawing, never reaches the vendor's files: dropped with its reason as an internal
        note, before the resolver (so no spotless copy carries an item it folds), the counts and
        the files (`spot_guard`)."""
        if self.settings.mode != "full_check" or not self._whole_set_work():
            return
        main = self.book.main_file
        labels = self.book.dim_labels

        def on_page(file: str | None, page: int) -> Any:
            name = file or main
            return None if name is None else labels.on_page(name, page, read_now=False)

        try:
            with self.finding_lock:
                found = spot_dropped(self.book.live(), on_page)
        except Exception:  # noqa: BLE001 - the answer keeps its items as they were
            _log.exception("assistant chat spot guard failed")
            return
        for finding_id, reason in found:
            try:
                with self.finding_lock:
                    self.book.drop({"finding_id": finding_id, "reason": reason})
            except Exception:  # noqa: BLE001 - one refused drop never stops the others
                _log.exception("assistant chat spot guard could not drop a finding")
                continue
            self._show_dropped(finding_id, reason)

    def _agree_counts(self, text: str) -> str:
        """Whole-set work (#1554): every count the answer's words state is the final list's. The
        AI wrote its words before code settled its decision items, so code sets them again (its
        own summary already counts the final list) and the resolver block's counts too."""
        if not self._whole_set_work():
            return text
        listed = shown_counts(self.book.live())
        counts = listed
        if self.settings.mode == "full_check":
            # A full check's words count as its cloud drawing does (#1619): Wrong = proved by
            # code, the questions to the vendor in the decision's place, then Right.
            groups = proof_counts([f for f in self.book.live() if vendor_facing(f)])
            counts = {
                **listed,
                "wrong": groups["clouded"],
                "decision": groups["not_compared"],
                "right": groups["right"],
            }
        with self._lock:
            for at, block in enumerate(self.blocks):
                if block.get("type") == "text" and block.get("by") != "code":
                    fixed = recount_words(str(block.get("text") or ""), counts)
                    if fixed != block.get("text"):
                        self.blocks[at] = {**block, "text": fixed}
                elif block.get("type") == "resolver" and isinstance(block.get("resolver"), dict):
                    # The resolver's own block counts the items by status, as it settled them.
                    after = {key: listed[key] for key in ("wrong", "decision", "right")}
                    self.blocks[at] = {**block, "resolver": {**block["resolver"], "after": after}}
        return recount_words(text, counts)

    def _recorded_missing(self) -> list[dict[str, Any]]:
        """GV Review's recorded differences this answer neither reports nor dismissed (whole-set
        work: a full check, or a whole-set answer that used coverage)."""
        if not self._whole_set_work():
            return []
        recorded = self._recorded_checks()
        if not recorded.readable:
            return []
        missing = set(recorded_missing(self.book.live(), recorded, self.book.dismissed))
        return [item for item in recorded.differences if item["record_id"] in missing]

    def _has_evidence(self) -> bool:
        return any(f.get("arch_box") or f.get("vendor_box") for f in self.book.live()) or any(
            cite.get("box") for cite in self.book.cites
        )

    def _coverage_gaps_note(self, answer: str = "") -> str | None:
        """The finish check's note: pages unaccounted for (when coverage was used), GV Review's
        recorded differences the answer lost, and drawing values stated with no evidence."""
        parts: list[str] = []
        boxless = boxless_findings(self.book.live())
        if boxless:
            # A finding that states a drawing value needs its box (the evidence picture).
            parts.append(boxless_note(boxless))
        elif states_drawing_values(answer) and not self._has_evidence():
            parts.append(
                f"{COVERAGE_CHECK_PREFIX} show the evidence: your answer states values read off "
                "the drawings with no evidence. Back each with report_finding (a comparison) or "
                "cite (a value), with boxes; then give your answer."
            )
        helpless = helpless_decisions(self.book.live())
        if helpless:
            # Every "Your decision" item carries the reviewer's reasoning (#1545).
            parts.append(decision_help_note(helpless, prefixed=not parts))
        if self.book.coverage_used and self._whole_set_work():
            gaps = self.book.coverage_gaps()
            if gaps:
                parts.append(coverage_note(gaps, self.book.main_file))
        missing = self._recorded_missing()
        # The ledger's recorded-results check finds a few more (the review list's failures):
        # folded into this one note, never a second send-back.
        for item in self._ledger_extra_missing(self._recorded_results_check()):
            pages = item.get("pages") or []
            missing.append(
                {
                    "record_id": item["key"],
                    "page": pages[0] if pages else None,
                    "what": item.get("what"),
                }
            )
        if missing:
            listed = "; ".join(
                f"{item['record_id']} (page {item.get('page')}: {item.get('what')})"
                for item in missing[:40]
            )
            more = f" and {len(missing) - 40} more" if len(missing) > 40 else ""
            lead = "" if parts else f"{COVERAGE_CHECK_PREFIX} "  # one prefix per note
            parts.append(
                f"{lead}GV Review recorded these differences and your answer neither reports nor "
                f"dismisses them: {listed}{more}. For each, report_finding with its record_id "
                "(after checking it on the drawing) or dismiss_recorded with your reason; then "
                "give your answer."
            )
        return " ".join(parts) or None

    def _seed_recorded(self) -> list[str]:
        """Full check: each difference GV Review recorded becomes a finding at once, so none is
        lost. Returns the lines that tell the lead which (to check each, or drop it)."""
        recorded = self._recorded_checks()
        if not recorded.readable:
            return []
        self.book.record_ids = frozenset(item["record_id"] for item in recorded.differences)
        # Continue: the records the earlier answer held are carried as placed (`carry_over`);
        # matched by `record_id`, never seeded again without their spots.
        held = {str(f["record_id"]) for f in self.book.live() if f.get("record_id")}
        areas = self._page_areas()
        seeded = []
        for item in recorded.differences:
            if str(item["record_id"]) in held:
                continue
            area = areas.get(item.get("page")) if item.get("source") == "ai_page_review" else None
            what = str(item.get("what") or "")
            if area and not what.lower().startswith(area.lower()):
                # An AI page review row names only what it measures: its area goes first, as in
                # every other item's name ("Pantry: Shelf depth, front to back").
                item = {**item, "what": f"{area}: {what}"}
            finding = self.book.seed(item)
            if finding is None:
                continue
            self._show_finding(finding)
            seeded.append(
                f"- {finding['id']} page {finding['page']}: {finding['item']}: architect "
                f"{finding['architect']}; vendor {finding['vendor']} ({item['record_id']})"
            )
        return seeded

    def _page_areas(self) -> dict[int, str]:
        """Each vendor page's area name from the set pack's page map ("Pantry"), else nothing."""
        folder = self.tools.workspace.set_pack
        main = self.book.main_file
        try:
            page_map = load_page_map(folder) if folder is not None else None
        except Exception:  # noqa: BLE001 - names then go without their area
            return {}
        out: dict[int, str] = {}
        for entry in (page_map or {}).get("pages") or []:
            if not isinstance(entry, Mapping) or not isinstance(entry.get("page"), int):
                continue
            if main and entry.get("file") and entry.get("file") != main:
                continue
            name = re.sub(r"^\s*\d{1,3}\s*[.)]\s*", "", str(entry.get("area") or "")).strip()
            if name:
                out[entry["page"]] = name
        return out

    def _start_markup_base(self) -> None:
        """Full check: the cloud drawing's copy of the vendor's PDF, made now in a thread (the
        slow part, about 12 s for 60 pages; it needs no finding)."""
        if self.tools.register_file is None:
            return
        workspace = self.tools.workspace

        def make() -> Any:
            return markup_base(workspace)

        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gv-chat-markup")
        self._markup_base = pool.submit(make)
        pool.shutdown(wait=False)

    def _full_check_files(self, stopped: str | None = None) -> list[str]:
        """Full check: the cloud drawing and the check report, made by code, as chat files (the
        names of those made). The two are written at the same time; the drawing on the copy made
        at the start."""
        register: Callable[[str, str, int, str, str], dict[str, Any]] | None = (
            self.tools.register_file
        )
        if register is None:
            return []
        # The double-read rule's last sweep before anything is drawn or written (2026-10-28).
        self._mark_unread_reds()
        book, workspace = self.book, self.tools.workspace
        # Only what may reach the vendor (#1652): items for GV's reviewer only stay on the screen.
        findings = [f for f in book.live() if vendor_facing(f)]
        report: dict[str, Any] = next(
            (b["trust"] for b in reversed(self.stored_blocks()) if b.get("type") == "trust"), {}
        )
        # The same counts as the screen and the cloud drawing (#1554, #1619): one final list.
        groups = proof_counts(findings)
        unseen = f", {groups['not_compared']} not compared" if groups["not_compared"] else ""
        if groups["rule_note"]:
            unseen = f", {groups['rule_note']} {RULE_NOTE_COUNT}{unseen}"
        if groups["likely_problem"]:
            unseen = f", {groups['likely_problem']} {LIKELY_COUNT}{unseen}"
        coverage = book.coverage_map()["counts"] if book.coverage_used else None
        trust = report.get("counts") or {}
        products = products_words(self.settings.products)
        summary = {
            "Drawing set": book.main_file or "",
            "Products": products,
            "Made": datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC"),
            "Model": self._model_words(),
            "Items": f"{groups['clouded']} wrong (proved by code), {groups['right']} right{unseen}",
            "Coverage": (
                "not recorded"
                if coverage is None
                else f"{coverage['total'] - coverage['not_opened']} of {coverage['total']} pages "
                f"accounted for ({coverage['not_comparable']} not comparable)"
            ),
            "Trust": f"{trust.get('confirmed', 0)} confirmed, {trust.get('likely', 0)} likely, "
            f"{trust.get('to_check', 0)} AI reading only",
        }
        # The code that made it (change 7): a report from older code can be told apart.
        summary["Code version"] = code_version() or "unknown"
        reading = (report.get("reading") or {}).get("lines") or []
        if reading:
            summary["Numbers read"] = "; ".join(str(line) for line in reading)
        not_checked = list(report.get("not_checked") or [])
        for item in (report.get("gv_checks") or {}).get("left_out") or []:
            not_checked.append(
                f"GV Review's recorded difference {item.get('record_id')} (page {item.get('page')}: "
                f"{item.get('what')}) is not in this answer."
            )
        made_base = self._markup_base
        settled = list(self._settled)
        # The send-ready package's context (#1622): the round-2 check, the stable comment numbers
        # and the review result's state, worked out once for the drawing and the package.
        worked: dict[str, Any] = {}
        # Pages never accounted for (no note why) mean the check did not finish (S0).
        gaps = book.coverage_gaps() if book.coverage_used else []
        incomplete = incomplete_reason(stopped, {"not_opened": len(gaps)}) or self.incomplete_note
        # The Not-compared list's rows (#1619): one source for its PDF, its workbook, the cloud
        # drawing's cover and the summary's line.
        nothing = self._not_compared_rows(findings)

        def drawing(out: Path) -> MarkupFile:
            base = None
            if made_base is not None:
                try:
                    base = made_base.result()
                except Exception as failed:  # noqa: BLE001 - the copy is made again below
                    _log.warning("assistant chat markup copy failed: %s", type(failed).__name__)
            worked["context"] = package_context(
                workspace,
                findings,
                self.settings.identity,
                numbers=self._comment_numbers(findings),
                incomplete=incomplete,
            )
            # The zero-click red/green proof drawing (#1581): only code-proved Wrongs clouded.
            return make_proof_file(
                workspace,
                findings,
                out,
                base,
                settled=settled,
                context=worked["context"],
                not_compared=len(nothing),
            )

        def checked(out: Path) -> MarkupFile:
            return make_report_file(workspace, summary, findings, not_checked, out, settled=settled)

        makers: tuple[tuple[str, str, Callable[[Path], MarkupFile]], ...] = (
            ("cloud drawing", "cloud_drawing", drawing),
            ("check report", "report", checked),
        )

        def make(phase: str, maker: Callable[[Path], MarkupFile]) -> tuple[str, MarkupFile]:
            with self.timings.phase(phase):
                file_id = str(uuid4())
                out = workspace.files / file_id
                try:
                    return file_id, maker(out)
                except BaseException:
                    out.unlink(missing_ok=True)
                    raise

        names: list[str] = []
        kept: list[tuple[str, str, Path]] = []
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="gv-chat-files") as pool:
            futures = [(what, pool.submit(make, phase, maker)) for what, phase, maker in makers]
            for what, future in futures:
                file_id = None
                try:
                    file_id, made = future.result()
                    register(file_id, made.name, made.size, "application/pdf", made.sha256)
                    names.append(made.name)
                    kept.append((what, made.name, workspace.files / file_id))
                except Exception as failed:  # noqa: BLE001 - told plainly in the answer
                    _log.warning("assistant chat %s failed: %s", what, type(failed).__name__)
                    if file_id is not None:
                        (workspace.files / file_id).unlink(missing_ok=True)
                    self._add_text(f"*The {what} could not be made for this answer.*")
        # The list also names the set's own gaps code found (the finish check, #1623; the vendor
        # consistency check), apart from the answer's items: those of the products in scope only
        # (the strict scope: a cabinet note is not listed in a countertop check).
        set_gaps, left_out = scoped_gaps(
            [*self.consistency_gaps, *self.extra_gaps], self.product_scope, self.page_words
        )
        if left_out:
            _log.info("assistant chat: %d set gaps outside the product scope left out", left_out)
        listed = self._not_compared_files([*nothing, *not_compared.gap_rows(set_gaps)])
        names += [name for name, _path in listed]
        if listed:
            # Kept with the package block, so the list can be made again after the reviewer's
            # decisions with the same set-level rows (#1803).
            self._set_gaps = json.loads(json.dumps(list(set_gaps), default=str))
        # The send-ready package (#1622) holds every file just made, the Not-compared list too.
        context = worked.get("context")
        if context is not None and any(what == "cloud drawing" for what, _name, _path in kept):
            listed_kept = [("not compared", name, path) for name, path in listed]
            names += self._package_file(findings, [*kept, *listed_kept], context)
        return names

    def _model_words(self) -> str:
        """The models the check report names: the chat's; for the simple full check, the models
        that did its work (the compare call, the hard drawings', the blind look), never the chat's
        model it did not use."""
        settings = self.settings
        simple = settings.simple if self._simple else None
        if not isinstance(simple, Mapping) or not isinstance(simple.get("compare"), Mapping):
            return f"{settings.model.label}, {settings.effort} effort"
        compare = simple["compare"]

        def words(model_id: object, effort: object) -> str:
            known = MODELS.get(str(model_id))
            return f"{known.label if known is not None else model_id}, {effort} effort"

        parts = [f"compare {words(compare.get('model'), compare.get('effort'))}"]
        hard = (compare.get("hard_model"), compare.get("hard_effort"))
        if hard[0] and hard != (compare.get("model"), compare.get("effort")):
            parts.append(f"hard drawings {words(*hard)}")
        blind = (
            simple.get("blind_model") or "claude-opus-5-5",
            simple.get("blind_effort") or "high",
        )
        parts.append(f"blind second look {words(*blind)}")
        return "; ".join(parts)

    def _comment_numbers(self, findings: Sequence[Mapping[str, Any]]) -> dict[str, str] | None:
        """The stable comment numbers of this check's findings (#1622), from the set's earlier
        checks; `None` (this check numbers its own) when they cannot be read."""
        numbering = self.settings.numbering
        if numbering is None:
            return None
        try:
            return numbering(findings)
        except Exception as failed:  # noqa: BLE001 - numbered from this check alone
            _log.warning("assistant comment numbers failed: %s", type(failed).__name__)
            return None

    def _package_file(
        self,
        findings: Sequence[Mapping[str, Any]],
        kept: Sequence[tuple[str, str, Path]],
        context: Any,
    ) -> list[str]:
        """Full check: the send-ready package (#1622): the files just made, the comment letter and
        the issue register, as one zip chat file. Made only with the cloud drawing; when it fails,
        the answer says so and the other files stand."""
        register = self.tools.register_file
        if register is None:
            return []
        workspace = self.tools.workspace
        file_id = str(uuid4())
        out = workspace.files / file_id
        try:
            with self.timings.phase("package"):
                made = make_package_file(
                    workspace,
                    findings,
                    [(name, path) for _what, name, path in kept],
                    out,
                    context,
                )
            register(file_id, made.name, made.size, ZIP_MIME, made.sha256)
            # What the screen (the review result) and a remade package read again.
            stored = context.block()
            if self._set_gaps is not None:
                stored = {**stored, "set_gaps": self._set_gaps}
            self._add_block({"type": "package", "package": stored})
        except Exception as failed:  # noqa: BLE001 - told plainly in the answer
            _log.warning("assistant chat package failed: %s", type(failed).__name__)
            out.unlink(missing_ok=True)
            self._add_text("*The send-ready package could not be made for this answer.*")
            return []
        return [made.name]

    def _not_compared_rows(self, findings: Sequence[Mapping[str, Any]]) -> list[Any]:
        """The full check's Not-compared rows (`not_compared.rows`), kept for the summary line."""
        try:
            self.not_compared = not_compared.check_rows(
                self.tools.workspace, findings, list(self.settings.products)
            )
        except Exception:  # noqa: BLE001 - counted from the findings alone then
            _log.exception("assistant chat not-compared rows failed")
            self.not_compared = not_compared.rows(findings)
        return self.not_compared

    def _not_compared_files(self, rows: Sequence[Any]) -> list[tuple[str, Path]]:
        """The Not-compared PDF and workbook (#1619), registered as the answer's files: each one's
        name and path (the send-ready package holds them too)."""
        register = self.tools.register_file
        workspace = self.tools.workspace
        if register is None:
            return []
        ids = (str(uuid4()), str(uuid4()))
        try:
            with self.timings.phase("not_compared"):
                pdf, sheet = not_compared.make_files(
                    workspace, rows, workspace.files / ids[0], workspace.files / ids[1]
                )
        except Exception as failed:  # noqa: BLE001 - told plainly in the answer
            _log.warning("assistant chat not-compared list failed: %s", type(failed).__name__)
            for file_id in ids:
                (workspace.files / file_id).unlink(missing_ok=True)
            self._add_text("*The Not-compared list could not be made for this answer.*")
            return []
        register(ids[0], pdf.name, pdf.size, "application/pdf", pdf.sha256)
        mime = not_compared.XLSX_MIME if sheet.name.endswith(".xlsx") else "text/csv"
        register(ids[1], sheet.name, sheet.size, mime, sheet.sha256)
        return [(pdf.name, workspace.files / ids[0]), (sheet.name, workspace.files / ids[1])]

    def _close_full_check(self, text: str, stopped: str | None, cap_hit: str | None) -> str:
        """Full check (#1518): the two files, also when it stopped early with findings; then
        code's summary (no AI call) when the AI did not write its own. Returns the answer's text."""
        names: list[str] = []
        if self._progress is not None:
            self._progress.phase("writing")
            self._progress.stop()
        if stopped not in ("cancelled", "error") or self.book.live():
            try:
                names = self._full_check_files(stopped)
            except Exception:  # noqa: BLE001 - the answer still ends, with its summary
                _log.exception("assistant chat full-check files failed")
        lines = [x for x in (self.consistency_line, self.items_line) if x]
        if not needs_code_summary(text, stopped):
            # The vendor consistency check's line (#1584), the finish check's (#1623) and what
            # could not be compared (#1619), one line each, after the AI's own summary.
            counts = self.book.coverage_map()["counts"] if self.book.coverage_used else {}
            nothing = not_compared_line(
                Counter(row.reason for row in self.not_compared),
                int(counts.get("not_opened") or 0),
            )
            # #1839: the sizes the architect specifies that break a GV rule (decided, ask the
            # architect), said apart from what could not be compared.
            shown = proof_counts([f for f in self.book.live() if vendor_facing(f)])
            noted = shown["rule_note"]
            if noted:
                lines.append(rule_note_line(noted))
            if shown["likely_problem"]:
                # The whole-drawing look's claims code could not prove (2026-10-23).
                lines.append(likely_line(shown["likely_problem"]))
            for words in (*lines, nothing.replace("list.", "list (PDF / Excel).")):
                if words:
                    index = self._add_block({"type": "text", "text": words, "by": "code"})
                    self.emit("text_delta", {"index": index, "text": words, "by": "code"})
                    text = f"{text}\n\n{words}"
            return text
        book, settings = self.book, self.settings
        summary = code_summary(
            book.live(),
            coverage=book.coverage_map()["counts"] if book.coverage_used else None,
            page_count=self.tools.page_count,
            products=settings.products,
            stopped=stopped,
            cap_hit=cap_hit,
            cap_usd=cap_text(settings.message_cap_micros),
            budget_usd=cap_text(settings.package_cap_micros),
            turns=settings.lead_max_turns,
            files=names,
            also=lines,
            reasons=[row.reason for row in self.not_compared] or None,
        )
        index = self._add_block({"type": "text", "text": summary, "by": "code"})
        self.emit("text_delta", {"index": index, "text": summary, "by": "code"})
        return f"{text}\n\n{summary}" if text.strip() else summary

    # ---- helpers (`Agent`) -----------------------------------------------------------------------

    def _agent_call(self, call: Mapping[str, Any]) -> _Agent:
        given = arguments(
            call.get("input"),
            frozenset({"description", "prompt", "model", "kit"}),
            frozenset({"description", "prompt"}),
        )
        kit = given.get("kit")
        if kit is not None and kit not in HELPER_KITS:
            raise SandboxRefused(f"kit must be one of {', '.join(HELPER_KITS)}")
        description = str(given["description"]).strip().replace("\n", " ")[:_MAX_DESCRIPTION]
        prompt = str(given["prompt"]).strip()
        if not description or not prompt:
            raise SandboxRefused("an agent needs a description and a prompt")
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise SandboxRefused(f"the prompt is longer than {_MAX_PROMPT_CHARS} characters")
        short = given.get("model")
        if short is None:
            model = self._helper_model()
        elif isinstance(short, str) and short in helper_shorts():
            model = BY_SHORT[short]
        else:
            raise SandboxRefused("model must be opus, sonnet or haiku")
        return _Agent(str(call.get("id")), description, prompt, model, kit=kit)

    def _run_agents(self, calls: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
        results: dict[int, dict[str, Any]] = {}
        accepted: list[tuple[dict[str, Any], _Agent]] = []
        for call in calls:
            if self.cancel.is_set():
                results[id(call)] = self._not_run(call, "the reviewer cancelled the message")
                continue
            try:
                agent = self._agent_call(call)
            except SandboxRefused as refused:
                results[id(call)] = self._not_run(call, f"refused: {refused}")
                continue
            if len(accepted) >= MAX_AGENTS_PER_TURN:
                results[id(call)] = self._not_run(
                    call,
                    f"at most {MAX_AGENTS_PER_TURN} agents start in one turn; start it again "
                    "next turn",
                )
                continue
            accepted.append((call, agent))
        if not accepted:
            return results
        entries = self._launch([agent for _call, agent in accepted])
        for (call, agent), entry in zip(accepted, entries, strict=True):
            reported = self.book.by(f"helper:{agent.number}")
            listed = "".join(
                f"\n- {f['id']} page {f['page']}: {f['item']}: architect {f['architect']}; "
                f"vendor {f['vendor']} ({f['status']})"
                for f in reported
            )
            if listed:
                listed = (
                    "\n\nFindings it reported (not verified: check each, then update_finding "
                    "with verified true, correct it, or drop_finding):" + listed
                )
            text = (
                f"Report from helper agent '{agent.description}' (its findings are leads: verify "
                "them before you state them):\n" + str(entry["report"])
            )[: _MAX_REPORT_CHARS - len(listed)] + listed
            result: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": call.get("id"),
                "content": [{"type": "text", "text": text}],
            }
            if not entry["ok"]:
                result["is_error"] = True
            results[id(call)] = result
        return results

    def _launch(
        self,
        agents: Sequence[_Agent],
        on_done: Callable[[_Agent, dict[str, Any]], None] | None = None,
        ceiling: int | None | object = _OWN_CEILING,
    ) -> list[dict[str, Any]]:
        """Runs helpers, at most `max_parallel_agents` at once (a roster full check: as many as
        their models' limiters allow, #1874), shown as one step; each one's entry (its report, ok,
        stopped, steps) in order. `on_done(agent, entry)` runs in the helper's thread as soon as it
        has reported (a full check starts its second reads there). `ceiling`: the spend these
        helpers may reach (a roster band's own, #1874); by default `helper_ceiling`."""
        with self._lock:
            # In what the fixed price's shares count: a step on its own share (the page
            # reviewer) never takes these helpers' money, nor they its (`counted_spent`).
            counted = self._spent - self._own_spent
            left = max(0, self.counted_cap - counted)
            limit = counted + left * self.helper_share_percent // 100
            top = self.helper_ceiling if ceiling is _OWN_CEILING else ceiling
            if isinstance(top, int):
                limit = min(limit, max(counted, top))
            share = _Share(limit=limit)
            for agent in agents:
                self._helpers_started += 1
                agent.number = self._helpers_started
        first = agents[0]
        count = len(agents)
        entries: list[dict[str, Any]] = [
            {
                "id": agent.call_id,
                "description": agent.description,
                "model": agent.model.id,
                "ok": None,
                "report": None,
                "steps": [],
                "stopped": None,
            }
            for agent in agents
        ]
        step = {
            **new_step(first.call_id, "Agent", {}),
            "label": f"Running {count} agent{'s' if count > 1 else ''} · {first.description}",
            "agents": entries,
        }
        index = self._add_block({"type": "step", "step": step})
        self.emit("step_start", {"index": index, "step": json.loads(json.dumps(step))})
        for entry in entries:
            self.emit(
                "agent_start",
                {
                    "step_index": index,
                    "agent": {k: entry[k] for k in ("id", "description", "model")},
                },
            )
        started = monotonic()

        def run(position: int, agent: _Agent) -> None:
            entry = entries[position]
            if agent.ready is not None and not self.cancel.is_set():
                try:
                    brief = agent.ready()
                except Exception:  # noqa: BLE001 - the helper runs with the brief it has
                    _log.exception("assistant chat helper's pairs could not be added")
                    brief = None
                if brief is not None:
                    agent.prompt = brief
            pages = list(agent.pages or ())
            if self.out_of_credit():
                # The AI account ran out of credit (a 402, change 11): no new helper is started;
                # what is done is kept and Continue does the rest after a top-up.
                report, ok, stopped = CREDIT_NOT_STARTED, False, "error"
            else:
                # One place of its model's limiter for the whole specialist (#1874): one waiting
                # for a place holds no money and makes no call.
                with self._limiter(agent.model).agent_slot(self.cancel):
                    self.timings.helper_started(agent.number, agent.description, pages)
                    with self.timings.phase(agent.phase, helper=agent.number):
                        try:
                            report, ok, stopped = self._helper(agent, index, entry, share)
                        except Exception as failed:  # noqa: BLE001 - one never stops the others
                            _log.warning("assistant chat helper failed: %s", type(failed).__name__)
                            report = _report_from_steps(entry["steps"], "it failed unexpectedly")
                            ok, stopped = False, "error"
                    self.timings.helper_ended(agent.number, stopped)
            agent.done = True
            with self._lock:
                entry.update({"ok": ok, "report": report, "stopped": stopped})
                summary = {
                    k: entry[k] for k in ("id", "description", "model", "ok", "report", "stopped")
                }
            self.emit("agent_end", {"step_index": index, "agent": summary})
            if on_done is not None:
                try:
                    on_done(agent, entry)
                except Exception as failed:  # noqa: BLE001 - the helper's report still stands
                    _log.warning(
                        "assistant chat helper follow-up failed: %s", type(failed).__name__
                    )

        with ThreadPoolExecutor(max_workers=min(count, self._agents_at_once)) as pool:
            for future in [pool.submit(run, n, agent) for n, agent in enumerate(agents)]:
                future.result()
        with self._lock:
            finished = {
                **step,
                "label": f"Ran {count} agent{'s' if count > 1 else ''} · {first.description}",
                "ok": all(entry["ok"] for entry in entries),
                "seconds": round(monotonic() - started, 1),
                "agents": json.loads(json.dumps(entries)),
            }
            self.blocks[index] = {"type": "step", "step": finished}
        self.emit("step_end", {"index": index, "step": finished})
        return entries

    # ---- a full check's fan-out, planned by code (#1512) ------------------------------------------

    def _lead_phase(self) -> str:
        return "lead_verification" if self._plan is not None or self._helpers_started else "lead"

    def _show_dropped(self, finding_id: str, reason: str) -> None:
        with self._lock:
            dropped_at = self._positions.get(finding_id)
            block = {"type": "finding_dropped", "finding_id": finding_id, "reason": reason}
            if dropped_at is not None:
                self.blocks[dropped_at] = block
        self.emit("finding_dropped", {"index": dropped_at, **block})

    def _helper_model(self) -> ChatModel:
        lead = self.settings.model
        return lead if lead.extra else BY_SHORT[HELPER_DEFAULT_MODEL[lead.short]]

    def _kind_system(self, kind: str) -> str:
        """A helper kind's system prompt (`full_check_specialists`), made once per message; the
        helpers' shared prompt when it cannot be made."""
        with self._lock:
            made = self._kind_systems.get(kind)
        if made is not None:
            return made
        try:
            made = specialist_system(kind)
        except Exception:  # noqa: BLE001 - the helper still runs, with the shared prompt
            _log.exception("assistant chat could not make the %s helpers' prompt", kind)
            made = HELPER_SYSTEM_PROMPT
        with self._lock:
            return self._kind_systems.setdefault(kind, made)

    def _prewarm(self) -> None:
        """One tiny call per helper kind with its exact system prompt and tools, so the helpers
        that start together read that prefix from the prompt cache instead of each writing it.
        Only on Anthropic's own API; a failure is only logged."""
        kinds = ("numbers", "drawn") if self.settings.drawn_helpers else ("both",)
        for kind in kinds:
            if self.provider.name != "anthropic" or self.cancel.is_set():
                return
            model = BY_SHORT["sonnet"] if kind == "drawn" else self._helper_model()
            self._prewarm_one(model, self._kind_system(kind))

    def _prewarm_one(self, model: ChatModel, system: str) -> None:
        effort = full_check_helper_effort(self.settings.effort)
        thinking = helper_thinking(model, effort, self.settings.thinking)
        messages = [{"role": "user", "content": [{"type": "text", "text": "Reply with OK."}]}]
        body = self._body(
            model=model,
            thinking=thinking,
            system=system,
            tools=HELPER_TOOLS,
            messages=messages,
            final=False,
            max_tokens=PREWARM_MAX_TOKENS,
        )
        if not self._fits(self._estimate(model, body, 0, 0)):
            return
        with self.timings.phase("prewarm"):
            try:
                self._call(
                    model=model,
                    effort=effort,
                    thinking=thinking,
                    system=system,
                    tools=HELPER_TOOLS,
                    messages=messages,
                    final=False,
                    max_tokens=PREWARM_MAX_TOKENS,
                    prompt_id=self.helper_prompt_id,
                    display=None,
                )
            except (_Cancelled, ProviderFailure) as failed:
                _log.info("assistant chat prompt cache not warmed: %s", type(failed).__name__)

    def _rules_summary(self) -> str:
        try:
            text = (self.tools.workspace.skills / "rules.md").read_text(encoding="utf-8")
        except OSError:
            return ""
        return rules_summary(text)

    def _job_brief(
        self,
        job: HelperJob,
        rules: str,
        helpers: int,
        pictures_made: bool,
        page_map: Mapping[str, Any] | None,
    ) -> str:
        """A code-planned helper's brief: the recall brief (`full_check_brief.helper_brief`: its
        pages with their areas, GV's recorded results and leads on them, the per-page checklist),
        then where to start, the products, pages to look at and the rules summary (the app's
        footer follows)."""
        products = products_words(self.settings.products).lower()
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(job.file).stem).strip("._") or "file"
        pictures = ", ".join(f"{SET_PACK}/pages/{stem}/p-{page:03d}.png" for page in job.pages)
        with self._lock:
            document = self._recorded_document
        brief = helper_brief(
            job.pages,
            file_name=f"{UPLOADS}/{job.file}",
            page_map=page_map,
            review_document=document,
            lens=job.lens,
            labels=self._dim_labels(job.file, job.pages, read_now=False),
            products=self.settings.products,
        )
        overview = (
            f"view your pages' overview pictures, all in one reply ({pictures})"
            if pictures_made
            else "zoom on each of your whole pages at 72 dpi, all in one call (the page pictures "
            "are still being made)"
        )
        others = (
            "one other has the same pages for the "
            + ("drawn things" if job.lens == "numbers" else "numbers")
            + ", the rest have the other pages"
            if job.partner is not None and not job.fold
            else (
                (
                    "one other has your pages (and its own) for the drawn things, the rest have "
                    "the other pages"
                    if job.lens == "numbers"
                    else "others share your pages for the numbers, the rest have the other pages"
                )
                if job.partner is not None
                else "the others have the other pages"
            )
        )
        lines = [
            brief,
            (
                f"START: you are one of {helpers} helpers; {others}. First {overview}; then zoom "
                "only to read what you compare, every crop you need in one call."
            ),
            f"PRODUCTS: {products}; a brand prototype's differences are their own group.",
        ]
        if job.reference:
            looked = "; ".join(f"{name} p.{number}" for name, number in job.reference)
            lines.append(f"LOOK AT, to compare with (another helper or file has them): {looked}.")
        lines += pair_lines(job.pairs, job.unpaired, UPLOADS)
        if rules:
            lines.append(
                f"GV's rules for {products} (summary; all of them in {SKILLS}/rules.md):\n{rules}"
            )
        return "\n\n".join(lines)

    def _dim_labels(
        self, file: str | None, pages: Sequence[int], *, read_now: bool = True
    ) -> dict[int, list[dict[str, Any]] | None]:
        """The numbered dimension labels on these pages of `file` (#1558)."""
        if file is None:
            return {}
        labels = labels_for(self.tools.workspace.set_pack, self.tools.workspace.uploads)
        return {page: labels.on_page(file, page, read_now=read_now) for page in pages}

    def _label_gaps(
        self, report: str, pages: Sequence[int], gaps: dict[int, str]
    ) -> dict[int, str]:
        """A helper's checklist gaps with the vendor labels nobody handled (#1558): on the pages
        whose labels are known now (the set pack's, or read for a zoom), never waiting to read
        a page."""
        try:
            return with_label_gaps(
                gaps,
                report,
                pages,
                self._dim_labels(self.book.main_file, pages, read_now=False),
                self.book.live(),
                [entry.as_dict() for entry in self.reading_ledger.strings],
                [entry.as_dict() for entry in self.reading_ledger.readings],
            )
        except Exception:  # noqa: BLE001 - the checklist without labels still stands
            _log.warning("assistant chat label gaps could not be counted")
            return gaps

    def _merge_seeds(self, by: str) -> None:
        """A helper's finding for a difference GV Review recorded (same `record_id`) fills in the
        finding the app made for it (values as printed, status, boxes) and is folded into it."""
        seeds = {
            str(f["record_id"]): f
            for f in self.book.by("gv_review")
            # A row whose only spots code found (#1738) still waits for a helper's reading.
            if f.get("record_id") and code_spotted(f)
        }
        for found in self.book.by(by):
            seed = seeds.pop(str(found.get("record_id") or ""), None)
            if seed is None:
                continue
            merged = {
                **seed,
                **{key: found.get(key) for key in _MERGED_FIELDS},
                "why": found.get("why") or seed.get("why"),
            }
            merged = self._checked(merged)
            with self.finding_lock:
                if not self.book.absorb(str(found["id"])):
                    continue
                if not self.book.replace(merged):
                    continue
            self._show_dropped(
                str(found["id"]),
                f"Folded into {seed['id']}: the helper's reading of GV Review's recorded "
                "difference.",
            )
            self._show_finding(merged)

    def _helper_done(self, agent: _Agent, entry: Mapping[str, Any]) -> None:
        """As each code-planned helper reports: its readings of recorded differences folded in,
        and the second reads of the differences found so far started. A two-lens helper's
        findings wait for its partner: then their duplicates are folded first (#1569)."""
        del entry
        self._merge_seeds(f"helper:{agent.number}")
        others = agent.fold or (() if agent.partner is None else (agent.partner,))
        if others:
            with self._lock:
                self._lens_reported.add(agent.number)
                both = all(other.number in self._lens_reported for other in others)
                if not both:
                    self._lens_waiting.add(f"helper:{agent.number}")
            if both:
                self._fold_lenses(agent)
        with self._lock:
            waiting = set(self._lens_waiting)
        self._queue_second_reads(
            [
                f
                for f in self.book.live()
                if f.get("status") in ("different", "check") and f.get("created_by") not in waiting
            ]
        )

    def _fold_lenses(self, agent: _Agent) -> None:
        """One page group's two helpers, both reported: each item and page kept once
        (`full_check_lenses.lens_folds`); a disagreement is kept for the lead."""
        others = agent.fold or ((agent.partner,) if agent.partner is not None else ())
        assert others
        group = (agent, *others)
        with self._lock:
            for other in others:
                self._lens_waiting.discard(f"helper:{other.number}")
        folds = lens_folds(
            [f for a in group if a.lens == "numbers" for f in self.book.by(f"helper:{a.number}")],
            [f for a in group if a.lens == "drawn" for f in self.book.by(f"helper:{a.number}")],
        )
        for fold in folds:
            if fold.disagree:
                with self._lock:
                    self._lens_disagree.append(fold)
            with self.finding_lock:
                if not self.book.absorb(fold.drop):
                    continue
            self._show_dropped(fold.drop, fold.reason)
        # Different items left under one name (two strings of one elevation): each named by its
        # place on the page, so the letter and the register never show two identical lines.
        with self.finding_lock:
            live = {str(f["id"]): f for f in self.book.live()}
            for finding_id, name in names_apart(list(live.values())).items():
                renamed = {**live[finding_id], "item": name}
                if self.book.replace(renamed):
                    self._show_finding(renamed)

    def _fan_out(self) -> bool:
        """A full check's helpers, planned by code from the set pack's page map and run at once;
        then the lead's first message gets their reports and the findings to verify. False when
        there is no page map to plan from (the lead plans, as in a chat)."""
        workspace = self.tools.workspace
        warm_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="gv-chat-warm")
        try:
            warm = warm_pool.submit(self._prewarm) if workspace.set_pack is not None else None
            with self.timings.phase("set_pack"):
                # No human step (#1581): code's likely answer to any plan-check question left.
                page_map = wait_for_page_map(
                    workspace.set_pack, self.cancel, assume=True, main_file=self.book.main_file
                )
            with self.timings.phase("page_match"):
                self.cross_file_pairs(page_map)
            with self.timings.phase("planning"):
                plan, _pages_all = self.scoped_jobs(page_map)
                rules = self._rules_summary() if plan is not None else ""
                pictures = pack_ready(workspace.set_pack)
            if warm is not None:
                warm.result()
        finally:
            warm_pool.shutdown(wait=True)
        if plan is None or self.cancel.is_set():
            return False
        if self.settings.drawn_helpers:
            plan = with_lenses(plan)
        self._plan = plan
        if plan.skipped:
            try:
                self.book.set_coverage(
                    {
                        "pages": [
                            {"page": page, "status": "not_comparable", "note": why}
                            for page, why in plan.skipped
                        ]
                    },
                    "code",
                )
                self._show_coverage()
            except SandboxRefused as refused:
                _log.warning("assistant chat could not record the divider pages: %s", refused)
        model = self._helper_model()
        agents = [
            _Agent(
                f"plan-{job.number}",
                job.description,
                self._job_brief(job, rules, len(plan.jobs), pictures, page_map),
                BY_SHORT["sonnet"] if job.lens == "drawn" else model,
                pages=job.pages,
                lens=job.lens,
                effort="low" if job.lens == "drawn" else None,
                cap_micros=job.cap_micros,
                system=self._kind_system(job.lens),
            )
            for job in plan.jobs
        ]
        by_job = {job.number: agent for job, agent in zip(plan.jobs, agents, strict=True)}
        for job, agent in zip(plan.jobs, agents, strict=True):
            agent.partner = None if job.partner is None else by_job.get(job.partner)
        entries = self._launch(agents, on_done=self._helper_done)
        # The rest of the helpers' findings (the right items too), now every helper is done.
        self._queue_second_reads(self.book.live(), final=True)
        self._wait_checks()
        self._lead_message(agents, entries)
        return True

    def cross_file_pairs(self, page_map: Mapping[str, Any] | None) -> CrossPairs | None:
        """The vendor pages paired across separate uploads (#1798), for the code-planned helpers:
        when the set has an architect's upload apart from the vendor's, the whole pack is waited
        for (its labels), the AI page matcher is asked about every vendor drawing the work list
        pairs across the uploads (saved in the pack: asked once per set), and the work list
        (code's score and the agreement rule) says which page goes with which. `None` when the
        set has no separate architect's upload or the pack is not there. Waits for the whole
        matcher; the roster starts its helpers beside it instead (`page_matching`)."""
        # Loaded by name: the work list reaches the drawn-position pairing (OpenCV-side code),
        # which `app/api/` must not import (`tests/api/test_no_heavy_work.py`).
        run = import_module("app.review.assistant.page_match").prepare_run(self, page_map)
        if run is None:
            return None
        run.start()
        self._cross = run.finish()
        self.book.pairs_now = lambda: self._cross
        return self._cross

    def page_matching(self, page_map: Mapping[str, Any] | None) -> Any:
        """A full check's page matching, to run beside its helpers (#1883,
        `page_match.MatchRun`): code's pairs (its ranking and the pack's saved answers) at once in
        `_cross`; the caller starts the matcher, each helper waits only for its own pages, and
        `finish_page_matching` sets the final pairs. `None` as `cross_file_pairs`."""
        run = import_module("app.review.assistant.page_match").prepare_run(self, page_map)
        if run is not None:
            self._cross = run.cross
            # A helper's architect spot is checked against its page's pair as it stands then
            # (the pairs last built: each helper's own pages were decided before it started).
            self.book.pairs_now = lambda: run.cross
        return run

    def finish_page_matching(self, run: Any) -> None:
        """The final cross-file pairs once the matcher has asked about every drawing (the gap
        closers and the finishing steps read them)."""
        if run is not None:
            self._cross = run.finish()
            self.book.pairs_now = lambda: self._cross

    def _stamp_reference_pages(self) -> None:
        """Every finding's one reference page, from its vendor page's final pair
        (`reference_page.stamp`): every picture, caption and code check after this reads it.
        Nothing when the set is not two separate uploads paired by the full check."""
        cross = self._cross
        if cross is None:
            return
        self._reference_stamped = True
        with self.finding_lock:
            changed = [
                stamped
                for finding in self.book.live()
                if (stamped := self._with_reference(finding)) != finding
                and self.book.replace(stamped)
            ]
        for stamped in changed:
            self._show_finding(stamped)

    def _drop_off_pair(self) -> None:
        """After the resolver and the escalation: any item they left with an architect spot off
        its pair (stamped again, `_with_reference`) is kept from the vendor, as the spot guard
        keeps one before them. No known path makes one; this holds if one ever does."""
        if self._cross is None:
            return
        with self.finding_lock:
            off = [
                (
                    str(finding["id"]),
                    str(record.get("why") or "Its architect page is not its pair's."),
                )
                for finding in self.book.live()
                if isinstance(record := self._with_reference(finding).get(reference_page.KEY), dict)
                and record.get("outside")
            ]
        for finding_id, why in off:
            reason = f"Internal note, not for the vendor: {why}"[:300]
            try:
                with self.finding_lock:
                    self.book.drop({"finding_id": finding_id, "reason": reason})
            except Exception:  # noqa: BLE001 - one refused drop never stops the others
                _log.exception("assistant chat could not drop an item off its pair")
                continue
            self._show_dropped(finding_id, reason)

    def _with_reference(self, finding: dict[str, Any]) -> dict[str, Any]:
        cross = self._cross
        if cross is None:
            return finding
        return reference_page.stamp(
            finding,
            cross.pairs,
            references=cross.references,
            unpaired=cross.unpaired,
            vendor_file=cross.file,
        )

    def scoped_jobs(
        self, page_map: Mapping[str, Any] | None, *, pairs: bool = True
    ) -> tuple[FanOutPlan | None, int]:
        """A full check's helper jobs for the products in scope, and the pages the whole set would
        have compared (`full_check_plan.scoped_plan`: the plan check counts the same pages).
        `pairs=False`: the jobs without their cross-file pairs (added as each starts,
        `full_check_plan.with_pairs`)."""
        folder = self.tools.workspace.set_pack
        return scoped_plan(
            page_map,
            self.book.main_file,
            self._plan_parallel,
            self.product_scope,
            None if folder is None else lambda files: page_texts(folder, files),
            across=self._cross if pairs else None,
        )

    def page_words(self, page: int) -> str:
        """A vendor page's printed words and its area's title (the set pack's page map), read once
        per message: what re-files an "other" item to a product (`product_scope.refiled`). Empty
        when the pack has neither."""
        with self._lock:
            known = self._page_words
        if known is None:
            known = {}
            folder = self.tools.workspace.set_pack
            main = self.book.main_file
            page_map = load_page_map(folder) if folder is not None else None
            for entry in (page_map or {}).get("pages") or []:
                if isinstance(entry, Mapping) and isinstance(entry.get("page"), int):
                    if main and entry.get("file") and entry.get("file") != main:
                        continue
                    known[entry["page"]] = str(entry.get("area") or "")
            files = [str(f) for f in (page_map or {}).get("files") or [] if f] or (
                [main] if main else []
            )
            if folder is not None and main:
                try:
                    # The same file list as the planning reads, so the pack's cache is shared.
                    for (name, number), text in page_texts(folder, files).items():
                        if name == main:
                            known[number] = f"{known.get(number, '')} {text}"
                except Exception as failed:  # noqa: BLE001 - re-filed from its own words only
                    _log.warning("assistant chat page words not read: %s", type(failed).__name__)
            with self._lock:
                self._page_words = known
        return known.get(page, "")

    def _note_coverage(self, page: int, note: str) -> None:
        if self.book.note_coverage(page, note) is not None:
            self._show_coverage()

    def _lead_message(self, agents: Sequence[_Agent], entries: Sequence[Mapping[str, Any]]) -> None:
        """The lead's first message of a code-planned full check: the verify-only instruction,
        the helpers' reports, and the findings split into backed, to verify, and GV Review's
        recorded differences still without boxes."""
        parts = [f"Helpers (planned by code, {len(agents)}):"]
        for agent, entry in zip(agents, entries, strict=True):
            state = (
                ""
                if entry.get("ok") and not entry.get("stopped")
                else (f" (stopped: {entry.get('stopped') or 'error'})")
            )
            report = str(entry.get("report") or "")[:_MAX_HELPER_REPORT_CHARS]
            parts.append(f"### Helper {agent.number} \u00b7 {agent.description}{state}\n{report}")
        gap_lines: list[str] = []
        with self._lock:
            open_gaps = {number: dict(gaps) for number, gaps in self._checklist_gaps.items()}
        for agent in agents:
            for page, why in sorted(open_gaps.get(agent.number, {}).items()):
                gap_lines.append(f"- p{page} (helper {agent.number}): {why}")
                self._note_coverage(page, f"checklist: {why}")
        if gap_lines:
            parts.append(
                f"Checklist gaps, verify these first ({len(gap_lines)}):\n" + "\n".join(gap_lines)
            )
        with self._lock:
            disagree = list(self._lens_disagree)
        if disagree:
            parts.append(
                f"The two helpers of a page disagree, verify these first ({len(disagree)}):\n"
                + "\n".join(
                    f"- {fold.keep} page {fold.page}: {fold.item}: numbers helper "
                    f"{fold.statuses[0]}, drawn-things helper {fold.statuses[1]}"
                    for fold in disagree
                )
            )
        main = self.book.main_file
        backed: list[str] = []
        verify: list[str] = []
        unboxed: list[str] = []
        others: list[str] = []
        findings = self.book.live()
        for finding in findings:
            if finding.get("created_by") == "gv_review" and code_spotted(finding):
                unboxed.append(
                    f"- {finding['id']} page {finding['page']}: {finding['item']}: architect "
                    f"{finding['architect']}; vendor {finding['vendor']} "
                    f"({finding.get('record_id')})"
                )
            elif _needs_lead_check(finding):
                verify.append(_verify_line(finding, main))
            elif _backed(finding):
                backed.append(str(finding["id"]))
            else:
                others.append(str(finding["id"]))
        parts.append(
            f"Findings ({len(findings)}). Backed by a second read that agrees or a code check, no "
            f"re-check needed ({len(backed)}): {', '.join(backed) or 'none'}."
        )
        if others:
            parts.append(
                f"Read once, nothing disagrees (right items and the like), no re-check needed "
                f"({len(others)}): {', '.join(others)}."
            )
        parts.append(f"To verify ({len(verify)}):\n" + ("\n".join(verify) if verify else "none"))
        if unboxed:
            parts.append(
                f"GV Review's recorded differences still without boxes ({len(unboxed)}):\n"
                + "\n".join(unboxed)
            )
        gaps = self.book.coverage_gaps()
        if gaps:
            parts.append(f"Pages not accounted for yet: {page_ranges(gaps)}.")
        text = "\n\n".join(parts)
        if len(text) > _MAX_REPORT_CHARS:
            text = text[: _MAX_REPORT_CHARS - 40] + "\n[cut: too long to list in full]"
        content = [
            (
                {"type": "text", "text": FULL_CHECK_LEAD_INSTRUCTION}
                if block.get("type") == "text" and block.get("text") == FULL_CHECK_INSTRUCTION
                else block
            )
            for block in self.user_content
        ]
        self.user_content = [*content, {"type": "text", "text": text}]

    def _helper_tool(
        self,
        agent: _Agent,
        step_index: int,
        entry: dict[str, Any],
        call: dict[str, Any],
        bad: set[int],
    ) -> Callable[[], dict[str, Any]]:
        """One of a helper's tool calls, as work to run (`_in_turns`)."""
        if self.cancel.is_set():
            cancelled = self._not_run(call, "the reviewer cancelled the message")
            return lambda: cancelled
        if id(call) in bad:
            cut = self._not_run(call, "its input was cut off")
            return lambda: cut
        name = str(call.get("name"))
        by = f"helper:{agent.number}"
        self.timings.tool(name)
        if name == "set_coverage" and agent.lens == "drawn":
            skipped = self._not_run(call, "the numbers helper records these pages' coverage")
            return lambda: skipped
        if name == RECORD_ITEMS["name"]:
            return self._guarded(
                call, lambda: self._result(call, self._record_items(call.get("input"), by))
            )
        if name in HELPER_FINDING_TOOL_NAMES or (name == "update_finding" and agent.lens == "lead"):
            # The full check's lead review (#1641) places items already recorded.
            return self._guarded(
                call, lambda: self._result(call, self._finding_tool(name, call.get("input"), by))
            )
        if name in LEDGER_TOOL_NAMES:
            return self._guarded(
                call, lambda: self._result(call, self._ledger_tool(name, call.get("input"), by))
            )

        def work() -> dict[str, Any]:
            step = new_step(str(call.get("id")), name, call.get("input"))
            outcome = self._dispatch_safely(name, call.get("input"), step, helper=True, by=by)
            with self._lock:
                entry["steps"].append(outcome.step)
            self.emit(
                "agent_step",
                {"step_index": step_index, "agent_id": agent.call_id, "step": outcome.step},
            )
            return self._result(call, outcome)

        return work

    @staticmethod
    def _topped_up(agent: _Agent) -> bool:
        """A soft-capped specialist at its cap got more of its phase's money (#1805)."""
        if not agent.soft_cap or agent.top_up is None:
            return False
        # One top-up at a time: what one specialist is given counts as promised before the next
        # asks (the price's spare money is shared by every running specialist, 2026-10-24).
        with _TOP_UP_LOCK:
            try:
                more = int(agent.top_up(agent))
            except Exception:  # noqa: BLE001 - no top-up: it wraps up at its cap, as before
                _log.warning("assistant chat helper top-up failed")
                return False
            if more <= 0:
                return False
            agent.cap_micros = (agent.cap_micros or 0) + more
        return True

    def _note_cut_report(
        self, agent: _Agent, report: str, turns: int, calls: int, wrap: str | None
    ) -> None:
        """A helper whose report still ended cut at its length limit (#1817): said in the log, and
        each page of its own the cut report gives no CHECKLIST line (or says it did not reach) is
        noted Not compared with the reason, so a later look takes it (never silently dropped)."""
        _log.warning(
            "assistant chat helper %s (%s) report cut at its length limit: turns=%d tool_calls=%d "
            "wrap=%s chars=%d",
            agent.number,
            agent.description,
            turns,
            calls,
            wrap or "none",
            len(report),
        )
        if agent.lens not in (*LENSES, "gap", "lead"):
            return  # the tag reader's or a prover's pages were compared by others
        for page in unreached_pages(report, agent.pages or ()):
            try:
                self._note_coverage(
                    int(page),
                    "not compared in detail: its helper's report was cut at its length limit",
                )
            except Exception:  # noqa: BLE001 - the report still goes on
                _log.warning("assistant chat helper page note failed: page %s", page)

    def _helper(
        self, agent: _Agent, step_index: int, entry: dict[str, Any], share: _Share
    ) -> tuple[str, bool, str | None]:
        """One helper's loop. Always returns a report: (report, ok, stopped). In a full check
        (#1512) it runs at low effort with a smaller output allowance, and wraps up after
        `FULL_CHECK_HELPER_CALLS` reading tool calls."""
        if agent.loop is not None:
            # A specialist with its own loop (the page reviewer): its own gates and money.
            return agent.loop(self, agent, step_index, entry, share)
        model = agent.model
        full_check = self.settings.mode == "full_check"
        effort = agent.effort or (
            full_check_helper_effort(self.settings.effort)
            if full_check
            else helper_effort(self.settings.effort)
        )
        thinking = helper_thinking(model, effort, self.settings.thinking)
        system = agent.system or helper_system_prompt(agent.kit)
        tools = agent.tools or helper_tools(agent.kit)
        allowance = FULL_CHECK_HELPER_MAX_TOKENS if full_check else HELPER_MAX_TOKENS
        roster_pages = agent.soft_cap and agent.output_format is None and bool(agent.pages)
        shaped = agent.output_format is not None
        if roster_pages:
            allowance = ROSTER_HELPER_MAX_TOKENS
        if shaped:
            allowance = max(allowance, SHAPED_HELPER_MAX_TOKENS)
        budget = FULL_CHECK_HELPER_CALLS
        if roster_pages:
            # Enough reading calls for every page of its own (#1634), never fewer than before.
            budget = max(budget, ROSTER_CALLS_PER_PAGE * len(agent.pages or ()))
        try:
            # A specialist with a reply shape (the roster's verifier, tag reader) has its pages in
            # its brief, and the footer's "report_finding" is not one of its tools.
            footer = (
                ""
                if agent.output_format is not None
                else helper_footer(
                    agent.prompt,
                    self.tools.workspace,
                    pages=agent.pages,
                    report_words=FULL_CHECK_REPORT_WORDS if full_check else 300,
                    # The real allowance and that code says when it ends (#1817: a lead review of
                    # 16 pages read "about 12 tool calls" and left pages "not opened: Budget").
                    calls=budget if roster_pages else None,
                )
            )
        except Exception:  # noqa: BLE001 - the helper still gets its brief
            _log.warning("assistant chat helper footer could not be made")
            footer = ""
        if agent.skip:
            return "Nothing to compare: no architect drawing is paired with it.", True, None
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": [
                    *(dict(block) for block in agent.opening),
                    {"type": "text", "text": agent.prompt + helper_lines(self.task_spec) + footer},
                ],
            }
        ]
        turns = reading = 0
        sent_back = False
        #: A cut answer or report asked for again once (#1817).
        cut_retried = False
        sent_on = False
        lead_sent_on = 0
        record_turn = False
        wrap: str | None = None
        cached = previous = 0
        while True:
            if self.cancel.is_set():
                return (
                    _report_from_steps(entry["steps"], "the reviewer cancelled"),
                    False,
                    "cancelled",
                )
            final = wrap is not None
            max_tokens = WRAP_UP_MAX_TOKENS if final else allowance
            if final and roster_pages:
                max_tokens = ROSTER_WRAP_UP_MAX_TOKENS
            if final and (shaped or cut_retried):
                # A reply shape's wrap-up is its whole answer; a report asked for again after a
                # cut gets the full allowance (#1817).
                max_tokens = max(max_tokens, allowance)
            body = self._body(
                model=model,
                thinking=thinking,
                system=system,
                tools=tools,
                messages=messages,
                final=final,
                max_tokens=max_tokens,
            )
            held = 0
            report_cost = self._estimate(model, body, cached, previous) if final else None
            if final and agent.soft_cap:
                if not self._hold_final(share, report_cost):
                    # A roster specialist never passes the fixed price, not even to write its
                    # report: code keeps what it recorded, and its report says where it stopped:
                    # "the fixed price was reached" only when it was (2026-10-24: said with $4.64
                    # of $9 left).
                    return (
                        _report_from_steps(
                            entry["steps"], _final_stop_why(self._fits(report_cost))
                        ),
                        False,
                        "cost_cap",
                    )
                # Held until it is charged, so the other helpers' calls count it (2026-10-24).
                held = report_cost or 0
            if not final:
                why = None
                estimate = self._estimate(model, body, cached, previous)
                if turns >= self.settings.helper_max_turns:
                    wrap, why = "turn_limit", "you have used all your tool turns"
                elif full_check and reading >= budget:
                    wrap, why = "turn_limit", "you have used your tool calls"
                elif not self._fits(estimate):
                    wrap, why = "cost_cap", "this message is near its money cap"
                elif (
                    agent.cap_micros is not None
                    and (
                        agent.spent >= agent.cap_micros * 4 // 5
                        if agent.soft_cap
                        else agent.spent + (estimate or 0) > agent.cap_micros
                    )
                    and not self._topped_up(agent)
                ):
                    wrap, why = "cost_cap", "you have used your part of the money"
                elif not self._hold(share, estimate):
                    wrap, why = "cost_cap", "the helpers have used their share of the money"
                record_estimate = None
                if why is not None and roster_pages and not record_turn:
                    record_body = {**body, "max_tokens": RECORD_MAX_TOKENS}
                    record_estimate = self._estimate(model, record_body, cached, previous)
                if (
                    why is not None
                    and roster_pages
                    and not record_turn
                    and why != "this message is near its money cap"
                    and turns < self.settings.helper_max_turns
                    and self._fits(record_estimate)
                    and self._hold_record(share, record_estimate)
                ):
                    # One turn to record what it read before its report (the paid run of
                    # 2026-10-19: gap closers read the pages, then wrapped up with no tools and
                    # recorded nothing, so their differences were lost).
                    # The record turn is never refused for the calls running now (#1654): the
                    # paid run of 2026-10-19's gap closers hit their shared ceiling, got no
                    # record turn and recorded nothing. It waits for them to end instead
                    # (`_hold_record`) and is held like any call until it is charged: one short
                    # call within the fixed price and the share, which keeps the later steps'
                    # money (#1641), counted by every other helper's check (2026-10-24: unheld,
                    # a record turn and another's report ran together past the share on CI).
                    # The roster keeps money for it under the ceiling when it hands out the
                    # price's spare money (`full_check_roster._Run._spare`).
                    record_turn = True
                    wrap = None
                    _append_note(messages, RECORD_NOTE.format(why=why))
                    held = record_estimate or 0
                    max_tokens = RECORD_MAX_TOKENS
                elif why is not None:
                    note = ROSTER_WRAP_UP_NOTE if roster_pages else WRAP_UP_NOTE
                    _append_note(messages, note.format(why=why))
                    continue
                else:
                    held = estimate or 0
            upper = prompt_tokens_upper(body, picture_sizes(messages))
            try:
                reply = self._call(
                    model=model,
                    effort=effort,
                    thinking=thinking,
                    system=system,
                    tools=tools,
                    messages=messages,
                    final=final,
                    max_tokens=max_tokens,
                    prompt_id=self.helper_prompt_id,
                    display=None,
                    output_format=agent.output_format,
                )
            except _Cancelled:
                return (
                    _report_from_steps(entry["steps"], "the reviewer cancelled"),
                    False,
                    "cancelled",
                )
            except ProviderFailure as failure:
                return _report_from_steps(entry["steps"], str(failure)), False, "error"
            finally:
                self._release(share, held)
            agent.last_call = self._cost(model, reply.usage)
            agent.spent += agent.last_call
            cached = sum(
                int(reply.usage.get(key) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            previous = upper
            if reply.stop_reason == "refusal":
                return (
                    _report_from_steps(entry["steps"], "the AI service declined this task"),
                    False,
                    "error",
                )
            content = _sendable(reply.history_content())
            # An empty or thinking-only reply is never sent back (the API refuses it): the note
            # below, or the report from the steps, follows the last user message instead.
            replayable = _replayable(content)
            if replayable:
                messages.append({"role": "assistant", "content": content})
            calls = reply.tool_uses() if replayable else []
            cut = reply.stop_reason == "max_tokens"
            if (
                (roster_pages or (shaped and not cut_retried))
                and not calls
                and not final
                and (cut or not reply.text().strip())
                and turns < self.settings.helper_max_turns
            ):
                # A reply cut at the smaller allowance, or one with only thinking in it (the lead
                # review of 2026-10-19 ended so, recording nothing): sent on, never the report.
                # A reply shape's cut answer is asked for again whole, once (#1817).
                cut_retried = cut_retried or shaped
                _append_note(messages, _SHAPE_CUT_NOTE if shaped else _CUT_NOTE)
                turns += 1
                continue
            if final and cut and not cut_retried and (roster_pages or shaped):
                # Its report (or its answer) was cut at the wrap-up's limit (#1817: the tag table
                # ended in the middle of a row): asked for once more, with the full allowance (no
                # tool turn: a turn limit does not stop it; the fixed price still does).
                cut_retried = True
                _append_note(messages, _SHAPE_CUT_NOTE if shaped else _REPORT_CUT_NOTE)
                turns += 1
                continue
            if (reply.stop_reason == "tool_use" or cut) and calls and not final:
                # A reply cut at its length limit while it held tool calls (#1817: three helpers
                # of the paid run of 2026-10-20 had 10-17 calls in it and ended with no report):
                # its complete calls run (a cut one is answered "its input was cut off") and it
                # is sent on, never ended.
                bad = reply.bad_inputs

                def parallel(call: dict[str, Any], bad: set[int] = bad) -> bool:
                    return call.get("name") in PARALLEL_TOOLS and id(call) not in bad

                def prepare(
                    call: dict[str, Any], bad: set[int] = bad
                ) -> Callable[[], dict[str, Any]]:
                    return self._helper_tool(agent, step_index, entry, call, bad)

                done = self._in_turns(calls, parallel, prepare)
                results: list[dict[str, Any]] = [done[id(call)] for call in calls]
                if cut:
                    results.append({"type": "text", "text": _CUT_NOTE})
                messages.append({"role": "user", "content": results})
                turns += 1
                reading += sum(1 for call in calls if call.get("name") in _READING_TOOLS)
                continue
            report = reply.text()
            if report and cut:
                # Still cut after it was asked again (or with no turn left): its pages without a
                # CHECKLIST line are said Not compared with the reason, never silently dropped.
                self._note_cut_report(agent, report, turns, len(calls), wrap)
            if not report:
                # Said, never silent (#1811): why it stopped, how far it got, and its pages noted
                # as not compared with that reason (a later look takes them).
                steps = entry.get("steps") or []
                last = steps[-1] if steps and isinstance(steps[-1], Mapping) else {}
                stop = reply.stop_reason or "none"
                _log.warning(
                    "assistant chat helper %s (%s) ended with no written report: stop=%s turns=%d "
                    "tool_calls=%d last_tool=%s wrap=%s",
                    agent.number,
                    agent.description,
                    stop,
                    turns,
                    len(reply.tool_uses()),
                    last.get("tool") or "none",
                    wrap or "none",
                )
                why = (
                    "not compared in detail: its helper did not write its report "
                    f"(it stopped: {stop})"
                )
                # Only a page-comparing helper's pages (never the tag reader's or a prover's: their
                # pages were compared by others).
                owned = agent.pages if agent.lens in (*LENSES, "gap") else None
                for page in owned or ():
                    try:
                        self._note_coverage(int(page), why)
                    except Exception:  # noqa: BLE001 - the report below still names the stop
                        _log.warning("assistant chat helper page note failed: page %s", page)
                report = _report_from_steps(entry["steps"], "it gave no written report")
                return report, False, wrap or "error"
            if agent.pages and agent.lens in LENSES:
                # A code-planned helper's checklist (#1515): sent back once to finish it while it
                # has turns left; what is still open goes to the lead.
                gaps = checklist_gaps(report, agent.pages, agent.lens)
                if agent.lens != "drawn":  # the dimension labels are the numbers' (#1558)
                    gaps = self._label_gaps(report, agent.pages, gaps)
                note = checklist_send_back(gaps, agent.lens)
                if (
                    note is not None
                    and not sent_back
                    and wrap is None
                    and turns < self.settings.helper_max_turns
                ):
                    sent_back = True
                    budget += FULL_CHECK_SEND_BACK_CALLS
                    messages.append({"role": "user", "content": [{"type": "text", "text": note}]})
                    continue
                unreached = unreached_pages(report, agent.pages) if roster_pages else []
                left_note = unreached_send_back(unreached)
                if (
                    left_note is not None
                    and not sent_on
                    and wrap is None
                    and agent.cap_micros is not None
                    and agent.spent < agent.cap_micros // 2
                    and turns < self.settings.helper_max_turns
                ):
                    # A roster helper that stopped short of pages with half its money unspent
                    # (#1634) is sent on once to compare them.
                    sent_on = True
                    budget += ROSTER_CALLS_PER_PAGE * len(unreached)
                    messages.append(
                        {"role": "user", "content": [{"type": "text", "text": left_note}]}
                    )
                    continue
                with self._lock:
                    self._checklist_gaps[agent.number] = gaps
            elif agent.lens == "lead" and roster_pages:
                # The full check's lead review (#1656) stops short by itself: the paid runs of
                # 2026-10-19 ended it with half its money left and pages "not opened". It is sent
                # on (twice at most) while it has money for them.
                unreached = unreached_pages(report, agent.pages or ())
                left_note = unreached_send_back(unreached)
                if (
                    left_note is not None
                    and lead_sent_on < LEAD_SEND_ON
                    and wrap is None
                    and agent.cap_micros is not None
                    and agent.spent < agent.cap_micros * 7 // 10
                    and turns < self.settings.helper_max_turns
                ):
                    lead_sent_on += 1
                    budget += ROSTER_CALLS_PER_PAGE * len(unreached)
                    messages.append(
                        {"role": "user", "content": [{"type": "text", "text": left_note}]}
                    )
                    continue
            return report, True, wrap

    # ---- the lead --------------------------------------------------------------------------------

    def run(self) -> MessageOutcome:
        """The lead's loop. Never raises: every way it ends is a stored outcome."""
        if self.settings.mode != "full_check":
            # A chat question's proofs are kept for the set's next full check (#1805).
            self.tools.on_proof = self._keep_chat_proof
        if self.settings.mode == "full_check":
            book = self.book
            self._progress = FullCheckProgress(
                lambda: (
                    book.live(),
                    book.coverage_map()["counts"] if book.coverage_used else None,
                    self.tools.page_count,
                ),
                self._show_progress,
            )
            self._progress.start()
        try:
            return self._ended()
        finally:
            if self._progress is not None:
                self._progress.stop()

    def _ended(self) -> MessageOutcome:
        try:
            return self._run()
        except Exception as failed:  # noqa: BLE001 - the message is still stored, plainly
            _log.exception("assistant chat message failed: %s", type(failed).__name__)
            self._end_running_steps()
            try:
                self.tools.sandbox.kill_all()
            except Exception:  # noqa: BLE001 - best effort
                _log.debug("assistant chat sandbox commands not stopped")
            try:
                self._close_checks()
            except Exception:  # noqa: BLE001 - best effort
                _log.debug("assistant chat checks did not close")
            text = ""
            if self.settings.mode == "full_check":
                try:
                    self._code_finish()
                except Exception:  # noqa: BLE001 - the files and summary still come
                    _log.exception("assistant chat stopped finish failed")
                try:
                    text = self._close_full_check("", "error", None)
                except Exception:  # noqa: BLE001 - stored without the summary
                    _log.exception("assistant chat full-check summary failed")
            return MessageOutcome(
                text=text,
                blocks=self.stored_blocks(),
                api_content=[],
                cost_micros=self.spent,
                stopped="error",
                error="Something went wrong in the assistant, so this answer stopped. Please ask "
                "again.",
            )

    def _end_running_steps(self) -> None:
        """Every step still shown as running ends as stopped: a stored answer never spins."""
        stopped = "Stopped: the answer ended before this step finished."
        with self._lock:
            for n, block in enumerate(self.blocks):
                step = block.get("step") if block.get("type") == "step" else None
                if not isinstance(step, dict):
                    continue
                agents = [
                    {**agent, "ok": False} if agent.get("ok") is None else agent
                    for agent in step.get("agents") or []
                ]
                if step.get("ok") is None or agents != list(step.get("agents") or []):
                    ended = {**step, "agents": agents or step.get("agents")}
                    if step.get("ok") is None:
                        ended.update(ok=False, output=step.get("output") or stopped)
                    self.blocks[n] = {"type": "step", "step": ended}

    @property
    def _lead_max_tokens(self) -> int:
        """The lead's output allowance: smaller when it only verifies a code-planned full check
        (the estimate held for each call is smaller, so the caps leave the checks their room)."""
        return FULL_CHECK_LEAD_MAX_TOKENS if self._plan is not None else LEAD_MAX_TOKENS

    def _wait_for_reading(self) -> MessageOutcome | None:
        """Wait until the set pack has read every page (#1872), the live card saying "Reading the
        drawings: N of M pages"; no AI call is made meanwhile. A page that could not be read is
        then skipped by every plan, with why (`read_page_map`). `None` to go on; else the stored
        outcome: stopped, or refused because the drawings could not be read (nothing spent)."""
        folder = self.tools.workspace.set_pack
        if folder is None:
            return None

        def told(reading: Reading) -> None:
            if self._progress is not None:
                self._progress.set_reading(reading.done, reading.total)

        with self.timings.phase("set_pack"):
            reading = wait_for_reading(folder, self.cancel, on_progress=told)
        if reading.state in ("ready", "none"):
            if reading.unreadable:
                _log.warning(
                    "assistant full check: %d pages could not be read and are skipped",
                    len(reading.unreadable),
                )
            return None
        if reading.state == "stopped":
            text = "Stopped while the drawings were being read; nothing was spent."
            stopped: str = "cancelled"
            error = None
        else:
            text = (
                f"The drawings could not be read ({reading.why}), so the full check did not "
                "start and nothing was spent. Upload the set again, or ask GV support."
            )
            stopped, error = "error", text
            _log.warning("assistant full check refused: %s", reading.why)
        index = self._add_block({"type": "text", "text": text, "by": "code"})
        self.emit("text_delta", {"index": index, "text": text, "by": "code"})
        return MessageOutcome(
            text=text,
            blocks=self.stored_blocks(),
            api_content=[],
            cost_micros=self.spent,
            stopped=stopped,
            error=error,
        )

    def _start_full_check(self) -> None:
        """Full check: GV Review's recorded differences made findings, the cloud drawing's copy
        started, and the helpers planned by code and run (else the lead is told the seeded
        findings and plans the helpers itself)."""
        seeded: list[str] = []
        with self.timings.phase("seeding"):
            try:
                seeded = self._seed_recorded()
            except Exception:  # noqa: BLE001 - the check goes on, never sendable (#1768)
                _log.exception("assistant chat could not seed the recorded differences")
                self.code_checks_failed.append(SEEDING_STEP)
        try:
            self._start_markup_base()
        except Exception:  # noqa: BLE001 - the drawing makes its own copy at the end
            _log.warning("assistant chat could not start the markup copy")
        planned = False
        try:
            planned = self._fan_out()
        except Exception:  # noqa: BLE001 - the lead plans the helpers instead
            _log.exception("assistant chat full check fan-out failed")
        if not planned and seeded:
            self.user_content = [
                *self.user_content,
                {
                    "type": "text",
                    "text": "GV Review already recorded these differences; the app made a "
                    "finding for each. Check each on the drawing: update_finding (boxes, values "
                    "as printed, verified true) or drop_finding with your reason. Never leave "
                    "one out.\n" + "\n".join(seeded),
                },
            ]

    def _finish_timings(self) -> None:
        """The message's timings: logged, and kept in its trust block (shown under "How this was
        checked")."""
        try:
            timings = self.timings.as_dict(self.spent)
        except Exception:  # noqa: BLE001 - the answer is stored without its timings
            _log.exception("assistant chat timings failed")
            return
        # A warning so it reaches the API's log (the server shows warnings and above): one line per
        # answer that says where its time and money went.
        _log.warning(
            "assistant chat timings (%s): %s", self.settings.mode, Timings.log_line(timings)
        )
        index = self._trust_index
        if index is None:
            return
        with self._lock:
            block = self.blocks[index]
            if block.get("type") != "trust":
                return
            # The strict product scope: the products checked and the findings of other products
            # the book refused (kept for the record, not shown).
            scope = {
                **self.product_scope.as_dict(),
                "dropped_out_of_scope": self.book.out_of_scope,
            }
            trust = {**block["trust"], "timings": Timings.stored(timings), "product_scope": scope}
            self.blocks[index] = {"type": "trust", "trust": trust}
        self.emit("trust", {"index": index, "trust": json.loads(json.dumps(trust))})

    # ---- room in a long chat (#1548) ---------------------------------------------------------------

    @property
    def clears_context(self) -> bool:
        """Whether the provider clears old tool results server-side (Anthropic's context editing);
        OpenRouter's route sends no beta."""
        return self.provider.name == "anthropic"

    def _context_for(
        self, messages: Sequence[Mapping[str, Any]], prompt_tokens: int
    ) -> dict[str, Any] | None:
        """The lead call's `context_management` (`chat_context.context_edits`): clearing old tool
        results past the threshold, and old thinking too once the prompt is near it (from then on,
        for the rest of the answer, so the request stays the same call to call)."""
        if not self.clears_context:
            return None
        threshold = self.settings.context_tokens
        if prompt_tokens >= threshold * NEAR_SHARE:
            self._context_near = True
        return context_edits(
            messages,
            threshold=threshold,
            keep_turns=self.settings.context_keep_turns,
            near=self._context_near,
        )

    def _room_for_history(self) -> None:
        """Where the provider cannot clear old turns, a history that alone would pass the
        threshold is replaced by code's summary of it (simple compaction: nothing older is
        replayed, so no thinking is sent outside the conversation that made it)."""
        if self.clears_context or not self.history:
            return
        body = self._body(
            model=self.settings.model,
            thinking=thinking_config(self.settings.model, self.settings.thinking),
            system=self.lead_system,
            tools=self.lead_tools,
            messages=self.history,
            final=False,
            max_tokens=self._lead_max_tokens,
        )
        if prompt_tokens_upper(body, picture_sizes(self.history)) < self.settings.context_tokens:
            return
        summary = history_summary(self.history)
        self.history = []
        self.summarised = True
        self.user_content = [{"type": "text", "text": summary}, *self.user_content]

    def _run(self) -> MessageOutcome:
        settings = self.settings
        model = settings.model
        thinking = thinking_config(model, settings.thinking)
        turn: list[dict[str, Any]] = [{"role": "user", "content": self.user_content}]
        answered = False
        tool_turns = 0
        forced: str | None = None
        cached = previous = 0
        stopped: str | None = None
        error: str | None = None
        cap_hit: str | None = None
        text = ""
        guard_sent = False
        # Asked once more after a reply with no words (`EMPTY_REPLY_NOTE`).
        nudged = False
        self._room_for_history()
        if settings.mode == "full_check":
            # Never an AI step before the drawings are read (#1872): wait (no money is spent),
            # showing the pages read; refuse plainly when they cannot be read.
            refused = self._wait_for_reading()
            if refused is not None:
                return refused
        if settings.mode == "full_check" and settings.full_check_mode == "simple":
            # The simple full check (CONTRACT-SIMPLE): work list, code, compare, verify, blind.
            return import_module(  # type: ignore[no-any-return]
                "app.review.assistant.simple_check.run"
            ).run(self)
        if settings.mode == "full_check" and settings.roster:
            # A fixed-price roster of specialists, no lead (`full_check_roster`); `None` when
            # there is no page map to plan from, and the lead plans as before.
            ended = import_module("app.review.assistant.full_check_roster").run(self)
            if ended is not None:
                return ended  # type: ignore[no-any-return]
        if settings.mode == "full_check":
            self._start_full_check()
        # The scope as asked, at once (the screen hides the whole-set panels for a narrow one).
        self._started_scope = self.scope()
        self.emit("scope", {"index": None, "scope": json.loads(json.dumps(self._started_scope))})
        note = scope_note(self._asked_scope)
        if note is not None:
            self.user_content = [*self.user_content, {"type": "text", "text": note}]
        turn[0] = {"role": "user", "content": self.user_content}
        while True:
            if self.cancel.is_set():
                stopped = "cancelled"
                break
            if tool_turns >= settings.lead_max_turns and forced is None:
                forced = "turn_limit"
            final = forced is not None
            max_tokens = FINAL_MAX_TOKENS if forced == "cost_cap" else self._lead_max_tokens
            messages = [*self.history, *turn]
            body = self._body(
                model=model,
                thinking=thinking,
                system=self.lead_system,
                tools=self.lead_tools,
                messages=messages,
                final=final,
                max_tokens=max_tokens,
            )
            over = self._over(self._estimate(model, body, cached, previous))
            if over is not None:
                cap_hit = cap_hit or over
                if forced != "cost_cap":
                    # One final, smaller call told to answer now, if even that fits.
                    forced = "cost_cap"
                    _append_note(turn, COST_CAP_NOTE)
                    continue
                stopped = "cost_cap"
                break
            upper = prompt_tokens_upper(body, picture_sizes(messages))
            try:
                with self.timings.phase(self._lead_phase()):
                    reply = self._call(
                        model=model,
                        effort=settings.effort,
                        thinking=thinking,
                        system=self.lead_system,
                        tools=self.lead_tools,
                        messages=messages,
                        final=final,
                        max_tokens=max_tokens,
                        prompt_id=self.lead_prompt_id,
                        display=self._new_attempt,
                        context_management=self._context_for(messages, cached or upper),
                    )
            except _Cancelled:
                stopped = "cancelled"
                break
            except ProviderFailure as failure:
                stopped, error = "error", str(failure)
                break
            cached = sum(
                int(reply.usage.get(key) or 0)
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
            previous = upper
            if reply.stop_reason == "refusal":
                # A refusal's partial output is never stored or replayed.
                self._discard_attempt()
                text = _declined(reply.stop_details)
                self._add_text(text)
                break
            content = _sendable(reply.history_content())
            if not _replayable(content):
                # Nothing to send back: an empty reply (Anthropic: it happens, mostly right after
                # tool results), one with only thinking, or one whose thinking was cut. It never
                # enters the history (the API would refuse every later question of the chat); the
                # model is asked once more for its words, then the answer says it plainly. A full
                # check needs no words of its own: code writes its summary.
                asks = settings.mode != "full_check" and not final
                if reply.stop_reason in ("end_turn", "stop_sequence") and asks and not nudged:
                    nudged = True
                    turn.append(
                        {"role": "user", "content": [{"type": "text", "text": EMPTY_REPLY_NOTE}]}
                    )
                    continue
                if reply.stop_reason == "max_tokens":
                    text = "*The answer reached its length limit and was cut off here.*"
                    self._add_text(text)
                elif reply.stop_reason == "model_context_window_exceeded":
                    text = "*This chat is now too long for the model. Start a new chat to go on.*"
                    self._add_text(text)
                elif asks:
                    stopped, error = "error", NO_ANSWER_ERROR
                    break
                stopped = forced
                break
            answered = True
            turn.append({"role": "assistant", "content": content})
            if reply.stop_reason == "pause_turn" and not final:
                # Anthropic paused its own tool (a long tool search): sent back as it is, it goes on.
                tool_turns += 1
                if tool_turns < settings.lead_max_turns:
                    continue
            calls = reply.tool_uses()
            if reply.stop_reason == "tool_use" and calls and not final:
                turn.append({"role": "user", "content": self._run_lead_tools(reply)})
                tool_turns += 1
                if self.cancel.is_set():
                    stopped = "cancelled"
                    break
                if tool_turns >= settings.lead_max_turns:
                    forced = "turn_limit"
                    _append_note(turn, TURN_LIMIT_NOTE)
                continue
            note = None if final or calls or guard_sent else self._coverage_gaps_note(reply.text())
            if note is not None and reply.stop_reason == "end_turn":
                # The whole-set guard: once, and only when the caps leave room for more work.
                guard_sent = True
                probe = [*turn, {"role": "user", "content": [{"type": "text", "text": note}]}]
                estimate = self._estimate(
                    model,
                    self._body(
                        model=model,
                        thinking=thinking,
                        system=self.lead_system,
                        tools=self.lead_tools,
                        messages=[*self.history, *probe],
                        final=False,
                        max_tokens=self._lead_max_tokens,
                    ),
                    cached,
                    previous,
                )
                if tool_turns < settings.lead_max_turns and self._fits(estimate):
                    turn.append(probe[-1])
                    continue
            if calls:
                turn.append(
                    {
                        "role": "user",
                        "content": [
                            self._not_run(call, "no more tools in this message") for call in calls
                        ],
                    }
                )
            text = reply.text()
            if reply.stop_reason == "max_tokens":
                note = "*The answer reached its length limit and was cut off here.*"
                self._add_text(note)
                text = f"{text}\n\n{note}" if text else note
            elif reply.stop_reason == "model_context_window_exceeded":
                note = "*This chat is now too long for the model. Start a new chat to go on.*"
                self._add_text(note)
                text = f"{text}\n\n{note}" if text else note
            # A cap or turn-limit stop is carried by `stopped` (the screen says it under the
            # answer); a note is written only when there is no answer to show.
            stopped = forced
            break
        if stopped in ("cost_cap", "turn_limit") and not text and settings.mode != "full_check":
            text = _stop_note(stopped, self.settings, cap_hit)
            self._add_text(text)
        elif stopped in ("cancelled", "error"):
            # What was written before it stopped.
            text = "\n\n".join(
                block["text"].strip()
                for block in self.stored_blocks()
                if block.get("type") == "text" and block.get("text", "").strip()
            )
            _close_open_calls(turn)
        try:
            self._store_scope()
        except Exception:  # noqa: BLE001 - the answer is stored without its scope block
            _log.exception("assistant chat scope failed")
        if stopped not in ("cancelled", "error"):
            try:
                self._trust_phase()
            except Exception:  # noqa: BLE001 - the answer is stored without its trust report
                _log.exception("assistant chat trust report failed")
            if stopped is None and self.cancel.is_set():
                # Stop pressed while code's checks ran (they stop early): a stopped answer,
                # never a finished one with checks left out.
                stopped = "cancelled"
        elif settings.mode == "full_check":
            # A stopped full check still gets code's $0 finish (#1515): the ledger's findings and
            # sums and the trust report with the recorded results, before the files and the
            # summary. The AI checks (second and third reads, the critic) are left out.
            try:
                self._code_finish()
            except Exception:  # noqa: BLE001 - the answer is stored without its trust report
                _log.exception("assistant chat stopped finish failed")
        self._close_checks()
        if settings.mode == "full_check":
            text = self._close_full_check(text, stopped, cap_hit)
        try:
            self._accuracy()
        except Exception:  # noqa: BLE001 - the answer is stored without its score
            _log.exception("assistant chat accuracy failed")
        text = self._agree_counts(text)
        self._finish_timings()
        if not answered:
            api_content: list[dict[str, Any]] = []
        else:
            api_content = turn
        return MessageOutcome(
            text=text,
            blocks=self.stored_blocks(),
            api_content=json.loads(json.dumps(api_content)),
            cost_micros=self.spent,
            stopped=stopped,
            error=error,
            cap_hit=cap_hit if stopped == "cost_cap" else None,
            seen=[dict(entry) for entry in self.tools.seen],
        )


def _append_note(messages: list[dict[str, Any]], note: str) -> None:
    """A note added to the newest (not yet sent) user message, as its last text block."""
    last = messages[-1]
    if last.get("role") != "user":
        messages.append({"role": "user", "content": [{"type": "text", "text": note}]})
        return
    content = last.get("content")
    if isinstance(content, str):
        last["content"] = [{"type": "text", "text": content}, {"type": "text", "text": note}]
    elif isinstance(content, list):
        last["content"] = [*content, {"type": "text", "text": note}]


def _close_open_calls(turn: list[dict[str, Any]]) -> None:
    """After a stop, every tool call in the history gets a result (the API needs one)."""
    if not turn or turn[-1].get("role") != "assistant":
        return
    calls = [
        block
        for block in turn[-1].get("content") or []
        if isinstance(block, Mapping) and block.get("type") == "tool_use"
    ]
    if calls:
        turn.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call.get("id"),
                        "content": [{"type": "text", "text": "Not run: the message stopped."}],
                        "is_error": True,
                    }
                    for call in calls
                ],
            }
        )


#: What a helper's reading of a recorded difference gives the finding the app made for it.
_MERGED_FIELDS: Final = (
    "architect",
    "vendor",
    "status",
    "arch_box",
    "vendor_box",
    "arch_picture_id",
    "vendor_picture_id",
    "vs_page",
    "round",
    "parts",
    "printed_total",
)
_MAX_HELPER_REPORT_CHARS: Final = 2_500


def _backed(finding: Mapping[str, Any]) -> bool:
    """A second read that agrees, or a check by code (printed at its spot, a GV rule, the
    vendor's arithmetic, the reading ledger): the lead need not look again."""
    checks = finding.get("checks") or {}
    return checks.get("second_read") == "agrees" or finding.get("evidence_kind") in (
        "observed",
        "derived",
    )


def _needs_lead_check(finding: Mapping[str, Any]) -> bool:
    """The lead verifies only a finding a second read disagrees with or is unsure of, and a
    difference nothing but one AI reading backs."""
    if _backed(finding):
        return False
    if (finding.get("checks") or {}).get("second_read") in ("disagrees", "unsure"):
        return True
    return finding.get("status") in ("different", "check")


def _box_words(box: object, main: str | None) -> str | None:
    if not isinstance(box, Mapping):
        return None
    where = "" if box.get("file") in (None, main) else f" of {box.get('file')}"
    coords = ",".join(f"{float(box[key]):g}" for key in ("x0", "y0", "x1", "y1"))
    return f"p.{box.get('page')}{where} [{coords}]"


def _verify_line(finding: Mapping[str, Any], main: str | None) -> str:
    checks = finding.get("checks") or {}
    line = (
        f"- {finding['id']} page {finding['page']}: {finding['item']}: architect "
        f"{finding['architect']}; vendor {finding['vendor']} ({finding.get('status')})"
    )
    read = checks.get("second_read")
    if read in ("disagrees", "unsure"):
        values = finding.get("second_read_values") or {}
        seen = " / ".join(str(values.get(side) or "-") for side in ("architect", "vendor"))
        line += f" \u00b7 second read {read} (read {seen})"
    boxes = [
        f"{side} {words}"
        for side, key in (("architect", "arch_box"), ("vendor", "vendor_box"))
        if (words := _box_words(finding.get(key), main)) is not None
    ]
    if boxes:
        line += " \u00b7 " + "; ".join(boxes)
    return line


def _bound(work: Callable[..., None], *given: Any) -> Callable[[], None]:
    """`work(*given)` as a job to run later."""
    return lambda: work(*given)


def _differences_first(finding: Mapping[str, Any]) -> int:
    """Differences first: they are what a reviewer acts on."""
    return {"different": 0, "check": 1}.get(str(finding.get("status")), 2)


def could_go_red(finding: Mapping[str, Any]) -> bool:
    """A finding a red could come from, which a second reading may hold back (2026-10-27): any
    item not called the same, from any maker (code's own too: a roster run makes some before the
    reads), that holds the numbers its red would compare (`reading_routes.sides_needed`: the
    vendor's alone against GV's rule or limits or the vendor's own string, else both). A "please
    check" item counts: code may make it a Wrong later (the escalation, the decision resolver)."""
    if finding.get("status") == "same":
        return False
    inches = finding.get("inches")
    if not isinstance(inches, Mapping):
        return False
    return all(str(inches.get(side) or "").strip() for side in sides_needed(finding))


#: A count of drawn things ("7 outlets", "6 pendant lights"): a number and what is counted, one of
#: these things drawn as symbols. A unit count ("2 UNITS") is left out: a vendor prints its unit
#: lines, and a reading can copy one.
_COUNTED: Final = re.compile(
    r"^\s*\d{1,3}\s+(?:[A-Za-z][A-Za-z-]*\s+){0,2}"
    r"(?:outlets?|receptacles?|(?:pendant\s+)?lights?|pendants?|fixtures?|sconces?|switch(?:es)?"
    r"|drawers?|doors?|shel(?:f|ves)|pulls?|knobs?|handles?|hinges?|legs?|brackets?|grommets?"
    r"|holes?|cut-?outs?|sinks?|faucets?|panels?|stools?|seats?)\s*$",
    re.IGNORECASE,
)
#: Why a would-be red that is a count is not read twice (`_count_only`).
COUNT_WHY: Final = "it is a count of drawn things, with no printed number for a reading to copy"


def _count_only(finding: Mapping[str, Any]) -> bool:
    """A count of drawn things on every side its red would compare (`sides_needed`): no number is
    printed for it, so a reading, asked to copy a printed value, can only come back with nothing
    (2026-10-28: "7 outlets" against "3 outlets", read twice, always "nothing, not sure"). It is
    never sent to the readers; a would-be red is marked "not double-read" instead, so it is never
    red without a reading (`_queue_second_reads`)."""
    values = [finding.get(side) for side in sides_needed(finding)]
    return bool(values) and all(
        isinstance(value, str) and _COUNTED.match(value) is not None for value in values
    )


#: A printed number a reading can copy and code can compare: digits with only a dimension's marks
#: (feet, inches, fractions, millimetres with inches in brackets), never words ("3 drawers", "no
#: outlet", "a finish tag").
_PRINTED_NUMBER: Final = re.compile(
    r"^[\d\s/.'\"\u2019\u2032\u201d\u2033\-\u2013\u2212\[\]]*\d"
    r"[\d\s/.'\"\u2019\u2032\u201d\u2033\-\u2013\u2212\[\]]*$"
)


def _nothing_to_copy(finding: Mapping[str, Any]) -> bool:
    """No side a reading would be shown holds a printed number: every value is words (a look
    finding: "2 drawers + 1 door", "no outlet", "a finish tag printed"). A reading of it
    can never agree (`second_read.compare_read` compares numbers only), so it is not asked; only a
    finding no red can come from is skipped (`_queue_second_reads`). 2026-10-29: 30 of the 56
    "unsure" second readings of a real upload were such findings, and they took 30 of the
    `SECOND_READ_LIMIT` places other items could have used."""
    sides = [
        side
        for side, box, picture in (
            ("architect", "arch_box", "arch_picture_id"),
            ("vendor", "vendor_box", "vendor_picture_id"),
        )
        if (finding.get(box) or finding.get(picture))
        and not (side == "architect" and finding.get("source") == "vendor_arithmetic")
    ]
    return bool(sides) and not any(
        isinstance(value := finding.get(side), str) and _PRINTED_NUMBER.match(value) is not None
        for side in sides
    )


def _reads_first(finding: Mapping[str, Any]) -> tuple[int, int]:
    """The second reads' order: would-be reds, then differences, then the rest."""
    return (0 if could_go_red(finding) else 1, _differences_first(finding))


#: A reading running now (`ChatEngine._read_slots`).
_IN_FLIGHT: Final = object()
#: Why a would-be red was not read twice when the check was stopped, or ended first.
STOPPED_WHY: Final = "the check was stopped before it was read"
NOT_REACHED_WHY: Final = "it was not reached before the check ended"


def _unread_red(finding: Mapping[str, Any]) -> bool:
    """A would-be red with no second reading and no reason given yet (`NOT_DOUBLE_READ`)."""
    return (
        could_go_red(finding)
        and (finding.get("checks") or {}).get("second_read") in (None, "not_run")
        and not finding.get(NOT_DOUBLE_READ)
    )


def _failure_why(failure: ProviderFailure) -> str:
    """Why a second reading could not be made, in plain words, by what stopped the call."""
    kind = getattr(failure, "kind", None)
    if kind == "payment":
        return "the AI account ran out of credit"
    if kind == "unreachable":
        return "the AI service could not be reached"
    if kind in ("key", "account"):
        return "the AI service refused the call"
    if kind == "busy":
        return "the AI service was too busy"
    return "the AI call failed"


def _asked_words(read: Callable[..., Any], drawn: read_spots.ReadPictures) -> dict[str, bool]:
    """What the reading's question may say of its pictures (`read_spots`): the centre and the
    overall, for a reader that takes them (a module loaded by name may be an older one)."""
    try:
        named = inspect.signature(read).parameters
    except (TypeError, ValueError):
        return {}
    words = {"centred": drawn.centred, "overall": drawn.overall}
    return {name: value for name, value in words.items() if name in named}


def _read_key(finding: Mapping[str, Any]) -> tuple[Any, ...]:
    """What a second read read: the item, both values and both crops."""
    return tuple(
        finding.get(key)
        for key in ("item", "architect", "vendor", "arch_picture_id", "vendor_picture_id")
    )


#: What Continue works out again for a carried finding (`ChatEngine.carry_over`) instead of
#: keeping the earlier answer's; every other field is kept as it was:
#: - `id`: this answer's own numbering;
#: - `arch_picture_id`, `vendor_picture_id`: drawn again from the same boxes (the same pictures,
#:   this answer's own files);
#: - `checks`, `trust_level`, `evidence_kind`, `witnesses`, `inches`, `parts_sum`: code's checks
#:   ($0), worked out again from the same values and spots (`apply_checks`); the AI readings in
#:   them come back from the earlier answer while what they read is unchanged (`_carry_reads`),
#:   and so do code's drawn-ink reads (`_kept_drawn_ink`);
#: - `reads`, `second_read_values`: the readings, kept the same way (`_carry_reads`);
#: - `decision`, `updated_in`: the reviewer's call and a later correction, added when the stored
#:   answer is read, never stored on a finding.
CARRY_RECOMPUTED: Final = frozenset(
    {
        "id",
        "arch_picture_id",
        "vendor_picture_id",
        "checks",
        "trust_level",
        "evidence_kind",
        "witnesses",
        "inches",
        "parts_sum",
        "reads",
        "second_read_values",
        "decision",
        "updated_in",
    }
)
#: (The devil's advocate's `critic` is carried and kept by the checks while the labels it judged
#: are the finding's, `chat_trust.kept_advocate`; else it is judged again.)
#: A carried finding's readings are kept only while these are the earlier answer's (with its
#: pictures drawn from the same boxes, what the readings read).
_CARRY_READ_SAME: Final = ("item", "architect", "vendor", "arch_box", "vendor_box")


def _kept_drawn_ink(earlier: Mapping[str, Any], finding: Mapping[str, Any]) -> dict[str, Any]:
    """A carried finding with the earlier answer's drawn-ink reads (`drawn_ink_checks`, $0 OCR at
    the finish) of the sides its own checks left waiting, while the values and boxes they were
    read for are unchanged; judged again. Anything else stays as its checks say."""
    out = dict(finding)
    before = earlier.get("checks") if isinstance(earlier.get("checks"), Mapping) else {}
    now = dict(out.get("checks") or {})
    assert isinstance(before, Mapping)
    if any(earlier.get(k) != out.get(k) for k in _CARRY_READ_SAME):
        return out
    changed = False
    for key in ("verify_architect", "verify_vendor"):
        old, new = before.get(key), now.get(key)
        if (
            isinstance(old, Mapping)
            and not old.get("pending")
            and isinstance(new, Mapping)
            and new.get("pending")
        ):
            now[key] = dict(old)
            changed = True
    if not changed:
        return out
    out["checks"] = now
    return judged(out)


def _fresh_input(usage: Mapping[str, Any]) -> int:
    """A call's input tokens that count against the provider's minute: prompt cache reads do not
    (`rate_limits`)."""
    return int(usage.get("input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)


#: Why a roster specialist could not write its report: the price itself, or only the money kept
#: for the checks after it (the price was not reached).
PRICE_REACHED: Final = "the fixed price was reached"
KEPT_REACHED: Final = (
    "it reached the money kept for the later checks; the fixed price was not reached"
)


def _final_stop_why(fits_price: bool) -> str:
    return KEPT_REACHED if fits_price else PRICE_REACHED


def _report_from_steps(steps: Sequence[Mapping[str, Any]], why: str) -> str:
    """A helper's report when it could not write one: why, and what it did."""
    lines = [f"This helper stopped before it could write its report ({why})."]
    if steps:
        lines.append("What it did before it stopped:")
        for step in list(steps)[:40]:
            what = step.get("command") or step.get("path") or ""
            lines.append(f"- {step.get('label')}" + (f": `{what}`" if what else ""))
    else:
        lines.append("It had not used any tool yet.")
    return "\n".join(lines)


def _declined(details: Mapping[str, Any] | None) -> str:
    category = details.get("category") if isinstance(details, Mapping) else None
    why = f" (the AI service's safety check: {category})" if isinstance(category, str) else ""
    return (
        f"The AI declined to answer this request{why}. Nothing else changed; you can ask in a "
        "different way."
    )


def _stop_note(stopped: str, settings: EngineSettings, cap_hit: str | None = None) -> str:
    """The answer when a cap or the turn limit stopped the message before it wrote one."""
    if stopped == "cost_cap" and cap_hit == "package":
        return (
            f"I stopped before finishing: this drawing set has used its "
            f"${cap_text(settings.package_cap_micros)} AI budget."
        )
    if stopped == "cost_cap":
        return (
            f"I stopped before finishing: this answer reached its "
            f"${cap_text(settings.message_cap_micros)} limit. Ask a narrower question, or ask me "
            "to go on."
        )
    return (
        f"I stopped before finishing: I used all {settings.lead_max_turns} tool turns for this "
        "message. Ask me to go on."
    )
