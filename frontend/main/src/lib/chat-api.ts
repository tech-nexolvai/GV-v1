/**
 * The chat-first assistant's API (#1464): a drawing set's chats, their saved messages, and a new
 * message streamed back as server-sent events.
 *
 * Typed by hand from the agreed contract (`chat-first/CONTRACT.md`, "HTTP API" and "SSE events")
 * because the routes are not in the generated schema yet, and an SSE stream's events never are
 * (FastAPI describes it only as `text/event-stream`). When the backend lands, check these against
 * `schema.d.ts`; the screen only reads the fields below.
 *
 * Optional fields are typed `?: T | null`: the server may leave a field out or send it as null, and
 * both mean "not there".
 */

import { ApiError, type ErrorEnvelope } from '@/api/client';
import { parseSseFrames, type SseFrame } from '@/api/sse';
import { LIVE_EVENTS, type LiveEvent, type LiveState } from '@/lib/chat-live';

// ── Types: the contract's shapes ────────────────────────────

/** How hard the model works on a message, lowest first. */
export type ChatEffort = 'low' | 'medium' | 'high' | 'xhigh' | 'max';

/** One model the composer offers (`GET /assistant/models`). */
export interface ChatModel {
  /** The API model id, sent back with each message ("claude-opus-5-5"). */
  id: string;
  /** In words, for the menu and the footer ("Claude Opus 5.5"). */
  label: string;
  /** The model the composer starts on. */
  default: boolean;
  efforts: ChatEffort[];
  default_effort: ChatEffort;
  /** `always`: it always thinks (the switch is locked on). `optional`: the reviewer may turn it off. */
  thinking: 'always' | 'optional';
  /** With thinking off, the highest effort allowed; null when there is no limit (or it always thinks). */
  thinking_off_max_effort: ChatEffort | null;
}

export interface ChatModels {
  /** False when no model can answer on this server; `reason` says why in plain words. */
  available: boolean;
  reason?: string | null;
  provider: 'openrouter' | 'anthropic';
  models: ChatModel[];
  /** The most one message may cost, in dollars ("5.00"). */
  message_cap_usd: string;
  /** The most one drawing set's chats may cost together, and what they have cost so far. */
  package_cap_usd: string;
  package_spent_usd: string;
  /**
   * #1823: the full check's price rule ("2.00", "0.35", "5.00"): base + per content page in scope,
   * at least the minimum, never above the set's budget left. A full check's own price is its cap.
   */
  full_check_base_usd?: string;
  full_check_per_page_usd?: string;
  full_check_min_usd?: string;
}

/** Chat v4 (CONTRACT4): a product a message focuses on: a product id from `GET /products` (#1547). */
export type ChatProduct = string;

/** Chat v4: an ordinary question, or the one-click full check (the app's own fixed instruction). */
export type ChatMode = 'chat' | 'full_check';

/** One chat in a drawing set's list (`GET /packages/{id}/chats`), newest first. */
export interface ChatSummary {
  id: string;
  /** Set by the first message (its first 60 characters); null until then. */
  title: string | null;
  created_at: string;
  last_message_at: string | null;
  /** How many questions it holds. */
  turns: number;
  /** #1710: a full check's chat, or an ordinary one. */
  mode?: ChatMode | null;
  /** #1710: a message of this chat is being answered now. */
  running?: boolean;
  /** #1710: while a full check runs, its drawings compared so far and in all (the row's "23/39"). */
  drawings_done?: number | null;
  drawings_total?: number | null;
}

export interface ChatList {
  /** Only chats with at least one message: a chat is made on its first message. */
  chats: ChatSummary[];
  /** The drawing set's name, for a new chat's greeting. */
  package_name?: string | null;
  /** Chat v4: the set's product type, the product chips' default. */
  product_type?: ChatProduct | null;
}

/** `POST /packages/{id}/chats`: a new empty chat. */
export interface CreatedChat {
  id: string;
  title: null;
  created_at: string;
}

/** One chat with every saved message (`GET /chats/{id}`). */
export interface Chat {
  id: string;
  package_id: string;
  /** The drawing set's name, for "What do you want to know about …?". */
  package_name: string;
  title: string | null;
  created_at: string;
  messages: ChatMessage[];
  /** True while a message of this chat is being answered (its answer is not stored yet). */
  running?: boolean | null;
  /** The answer being written while `running`: followed live with `followChatMessage`. */
  running_message_id?: string | null;
  /** Chat v4: the set's product type, the product chips' default. */
  product_type?: ChatProduct | null;
  /** #1710: while a full check runs, its live state, so a reloaded page draws the live card at once. */
  live?: LiveState | null;
}

/** Why a message stopped before it finished; null or absent when it finished. */
export type ChatStopped = 'cost_cap' | 'cancelled' | 'error' | 'turn_limit' | 'interrupted';

/**
 * With `stopped: "interrupted"` (#1514): a server restart cut the answer off. On a full check that
 * did not finish (stopped, or out of money, #1821) too. How Continue picks it up, and what it will
 * do again, in plain words.
 */
export interface ChatResume {
  mode: ChatMode;
  products?: ChatProduct[] | null;
  /** Findings already made: kept, not made again. */
  findings: number;
  /** Pages already accounted for: not opened again. */
  pages_done: number;
  redo: string;
}

/** The follow-up Continue sends (the server hands the interrupted work over with it). */
export const CONTINUE_TEXT = 'Continue from where you stopped';

/** A message as stored and as returned. */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  created_at: string;
  /** The user's question; for the assistant, its final markdown answer. */
  text: string;
  /** The assistant's: what was used for it. */
  model?: string | null;
  effort?: ChatEffort | null;
  thinking?: boolean | null;
  /** The assistant's: thinking, words and tool steps in the order they happened. */
  blocks?: ChatBlock[] | null;
  files?: ChatFile[] | null;
  /** #1803: the package was made before the reviewer's newest decision: never handed out, made again. */
  package_outdated?: boolean | null;
  /** What the message cost, in dollars ("1.72"). */
  cost_usd?: string | null;
  stopped?: ChatStopped | null;
  /** Plain words about a failure (`stopped: "error"`). */
  error?: string | null;
  /** The per-answer cap and the drawing set's AI budget this answer ran under ("5.00", "40.00"). */
  message_cap_usd?: string | null;
  package_cap_usd?: string | null;
  /** With `stopped: "cost_cap"`: which one stopped it. */
  cap_hit?: 'message' | 'package' | null;
  /** Chat v4: how it was asked (absent = an ordinary chat message) and for which products. */
  mode?: ChatMode | null;
  products?: ChatProduct[] | null;
  /** Chat v5: code's accuracy score for the answer; absent on older answers. */
  accuracy?: ChatAccuracy | null;
  /** #1522: what the answer covers (server, when it sends it): the whole set, or a few pages / an area. */
  scope?: ChatScope | null;
  /** With `stopped: "interrupted"`: how Continue picks up (#1514). */
  resume?: ChatResume | null;
  /** #1551: how the app understood the question (the answer's `understood` block); null when none. */
  understood?: ChatUnderstood | null;
  /** #1622: a full check's review action, decided by code on read; null on other messages. */
  review_action?: ChatReviewAction | null;
}

/**
 * #1622: the review result a full check's send-ready package carries, decided by code (rules S0 to
 * S7 and RN, the first that matches wins) from the comments it sends the vendor (the clouded Wrong
 * items). The comment letter and the cloud drawing's cover say the same words, except that the
 * vendor reads "Under review" where GV's screen says "Review needed" (`vendor_words`).
 */
export interface ChatReviewAction {
  action: 'approved' | 'approved_as_noted' | 'review_needed' | 'revise_resubmit' | 'rejected' | 'not_for_sending';
  rule: 'S0' | 'S1' | 'S2' | 'S3' | 'S4' | 'RN' | 'S5' | 'S6' | 'S7';
  /** "Approved", "Approved as noted", "Review needed", "Revise and resubmit", "Rejected", "Check incomplete: not for sending". */
  words: string;
  /** The result as the vendor reads it: the same words, but "Under review" for Review needed. */
  vendor_words?: string;
  /** Items Not compared, and all items (Wrong + Right + Ask the architect + Not compared). */
  not_compared?: number;
  items?: number;
  /** Why, in one line: "2 of 5 corrections change fabrication." */
  reason: string;
  corrections: number;
  fabrication: number;
  /** S6 and RN: what was not reviewed. */
  note?: string | null;
  /** What the vendor does next (the letter's closing line). */
  next_step: string;
  /** Who must act now: "Vendor: revise and resubmit". */
  required_by: string;
  disclaimer: string;
}

/** #1622: what a full check's package kept with the answer (the result's state, the comment numbers). */
export interface ChatPackageState {
  state: { incomplete?: string | null; missing_areas?: number; wrong_product?: boolean; still_open?: number };
  numbers?: Record<string, string>;
  submittal?: string;
  round?: number;
}

/** A full check's progress, by code (#1524): the last one is stored as a block. */
export interface ChatProgress {
  phase: 'reading' | 'checking' | 'writing';
  pages_done: number;
  pages_total: number;
  wrong_so_far: number;
  decision_so_far: number;
  right_so_far: number;
  /** Time left at the pace so far; null before any page is done. */
  eta_seconds: number | null;
  /** The simple full check (#1665): vendor drawings compared so far, of its work list. */
  drawings_done?: number | null;
  drawings_total?: number | null;
  /** #1871: pages read so far while the check waits for the drawings (no AI step meanwhile). */
  reading_done?: number | null;
  reading_total?: number | null;
}

/**
 * What an answer covers (#1528), worked out by code: the whole drawing set, or some pages, or one
 * area (a narrow question). `label` says it in words ("page 8", "pages 27-28", "the whole set").
 */
export interface ChatScope {
  kind: 'whole_set' | 'pages' | 'area';
  pages?: number[] | null;
  area?: string | null;
  why?: string | null;
  label?: string | null;
  /** The products checked: a strict scope (Anant, 2026-10-18). Absent on older answers. */
  products?: string[] | null;
  all_products?: boolean | null;
  products_label?: string | null;
}

/** #1551: what a typed question asks for, as the app's sharpen step read it. */
export type ChatIntent = 'fact' | 'compare' | 'whole_set' | 'rules' | 'arithmetic' | 'rounds' | 'file' | 'summary' | 'clarify' | 'refuse_action';
export type ChatOutputShape = 'one_line' | 'short' | 'findings' | 'file';

/**
 * #1551: how the app understood a typed question before any tool ran (its task spec). Shown as a
 * quiet "Understood as: <label> · Edit" line under the question. A guide for the AI only: the
 * reviewer's own words are always sent unchanged.
 */
export interface ChatUnderstood {
  task: string;
  intent: ChatIntent;
  scope: { kind: 'whole_set' | 'pages' | 'area'; pages?: number[] | null; area?: string | null; label: string };
  products?: string[] | null;
  what_to_compare?: string[] | null;
  checks_to_run?: string[] | null;
  output_shape: ChatOutputShape;
  assumptions?: string[] | null;
  /** With intent `clarify`: a question back the lead may ask (the lead always answers). */
  question_back?: string | null;
  /** A short hint for the lead (an unclear question, or an order only a person carries out). */
  hint?: string | null;
  /** "Compare outlet heights (architect vs vendor) · page 4 · short answer". */
  label: string;
  /** Code rules ($0), the cheap model, or the reviewer's own correction. */
  path: 'code' | 'model' | 'edited';
  seconds?: number | null;
  cost_usd?: string | null;
}

/** #1551: the reviewer's correction of an "Understood as" line, sent with the same question. */
export interface ChatTaskEdit {
  task: string;
  intent?: ChatIntent;
  what_to_compare?: string[];
  checks_to_run?: string[];
  /** Empty: the whole set. */
  pages?: number[];
  output_shape?: ChatOutputShape;
}

/** The reviewer's answer on one finding (PATH step 5), recorded append-only by the server. */
export type FindingDecisionValue = 'agree' | 'not_a_problem';
/** #1803: what the reviewer can send: a decision, or `undo` (take the call back: the item is again what code found). */
export type FindingDecisionChoice = FindingDecisionValue | 'undo';

export interface FindingDecision {
  decision: FindingDecisionValue;
  note?: string | null;
  /** #1803: with an Agree, what the reviewer says the vendor's value should be (the green "should be"). */
  should_be?: string | null;
  by?: string | null;
  at?: string | null;
}

/** #1803: what the reviewer adds to a decision: a note (the vendor reads it) and, with an Agree, the value it should be. */
export interface FindingDecisionExtra {
  note?: string | null;
  should_be?: string | null;
}

export type ChatBlock =
  | { type: 'thinking'; text: string; seconds: number | null }
  | { type: 'text'; text: string; by?: 'code' | null }
  | { type: 'step'; step: ChatStep }
  | { type: 'finding'; finding: ChatFinding }
  | { type: 'finding_update'; update: ChatFindingUpdate }
  | { type: 'coverage'; coverage: ChatCoverage }
  | { type: 'progress'; progress: ChatProgress }
  | { type: 'scope'; scope: ChatScope }
  | { type: 'trust'; trust: ChatTrust }
  | { type: 'cite'; cite: ChatCite }
  | { type: 'accuracy'; accuracy: ChatAccuracy }
  | { type: 'unknowns'; unknowns: ChatUnknown[] }
  | { type: 'resume'; resume: ChatResume }
  | { type: 'resolver'; resolver: ChatResolver }
  | { type: 'package'; package: ChatPackageState }
  | { type: 'understood'; understood: ChatUnderstood }
  | { type: 'fallback'; fallback: ChatFallback };

/**
 * #1577: Anthropic's refusal fallback answered instead of the model asked (the model asked
 * declined; another model wrote the answer). Model ids, e.g. "claude-fable-5-1" → "claude-opus-4-8".
 */
export interface ChatFallback {
  from_model: string;
  to_model: string;
}

// ── Chat v2 (CONTRACT2-findings): findings with evidence, and page coverage ──

/** Which group a finding sits in. */
/** A product id from `GET /products` (#1547), or "other". */
export type FindingCategory = string;

/** `different`: the drawings disagree. `check`: a person should look. `same`: they agree. */
export type FindingStatus = 'different' | 'check' | 'same';

/**
 * What the vendor's value was compared with: the architect's drawing (the usual case), the brand's
 * prototype, GV's rules, or the vendor's own numbers adding up ("vendor's own string").
 */
export type FindingSource =
  | 'architect'
  | 'brand_prototype'
  | 'rules'
  | 'vendor_arithmetic'
  | 'gv_review'
  /** Made by code from the reading ledger: one item read differently on two views of the same side. */
  | 'cross_view'
  /** Made by code from the reading ledger: an item one side shows and the other never does. */
  | 'completeness';

/** A spot on a page, in PDF points of the original upload (origin top-left). */
export interface FindingBox {
  page: number;
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  /** The upload the box is on (the server sends it): two boxes on one page of one file share a picture. */
  file?: string | null;
}

/**
 * One difference (or match) the assistant reported with `report_finding`, at its latest version.
 * `architect` and `vendor` are shown exactly as written: the screen never re-types a number.
 */
export interface ChatFinding {
  id: string;
  /** The page the finding is on (the vendor's page). */
  page: number;
  /** The page it was compared with (the architect's), when it is a different one. */
  vs_page?: number | null;
  item: string;
  category: FindingCategory;
  architect: string | null;
  vendor: string | null;
  status: FindingStatus;
  why?: string | null;
  /** True when the lead checked it on the drawing itself; false for a helper's lead not yet checked. */
  verified: boolean;
  arch_box?: FindingBox | null;
  vendor_box?: FindingBox | null;
  source: FindingSource;
  /** The evidence crops code made from the boxes, served by the chat's pictures route. */
  arch_picture_id?: string | null;
  vendor_picture_id?: string | null;
  /** "lead", or "helper:<n>". */
  created_by?: string | null;
  /** Chat v3 (CONTRACT3-trust): code's own reading of the two values, in inches. */
  inches?: FindingInches | null;
  /** Chat v3: the checks code ran on this finding (and the second read's result). */
  checks?: FindingChecks | null;
  /** Chat v3: the trust level code worked out from the checks. */
  trust_level?: TrustLevel | null;
  /** Chat v3: what happened to it across the set's rounds of comments. */
  round_status?: RoundStatus | null;
  /** Compared with the vendor's own earlier round: information only, never counted as right (2026-10-22). */
  round_match?: RoundMatch | null;
  /** Chat v3, `source: vendor_arithmetic`: the vendor's parts and the total it printed, as printed. */
  parts?: string[] | null;
  printed_total?: string | null;
  /** Chat v3, `source: rules`: the GV check code ran for it. */
  rule_check_id?: string | null;
  /** Chat v3: which round of comments it is from. */
  round?: number | null;
  /** The reading ledger's entries behind a finding code made from it (S<n>, R<n>). */
  ledger_ids?: string[] | null;
  /**
   * `cross_view`: whose drawings disagree with themselves. `architect` then holds the REFERENCE
   * view's value and `vendor` this view's value (both on that side's drawings).
   */
  reference_side?: 'vendor' | 'architect' | null;
  /** A ledger sum, written out by code: "12 + 15 1/2 = 27 1/2 ≠ 27 1/4 printed (off 1/4)". */
  sum_text?: string | null;
  /**
   * What backs it: `observed` (printed on the drawing at its spots), `derived` (worked out by code:
   * GV rule, arithmetic, ledger, mm/inch), `inferred` (AI reading only).
   */
  evidence_kind?: EvidenceKind | null;
  /** The non-AI witnesses behind it: printed, rule, arithmetic, ledger, units. */
  witnesses?: string[] | null;
  /** A "same" item: false when both sides were not seen or its page pair is not covered ("Not compared"). */
  compared?: boolean | null;
  not_compared_why?: 'not_viewed' | 'pair_not_covered' | null;
  /** #1498: every reading of the values, when the second read was settled by a third. */
  reads?: FindingRead[] | null;
  /** #1498: the critic's look at it (whole-set answers). */
  critic?: FindingCritic | null;
  /** #1522: the reviewer's latest Agree / Not a problem on it, when the server sends it. */
  decision?: FindingDecision | null;
  /** #1545: a "Your decision" item's reasoning: what it measures, the estimate, likely, what to confirm. */
  decision_help?: FindingDecisionHelp | null;
  /** #1545: why two printed values that differ still need a person (else code calls it Wrong). */
  check_reason?: CheckReason | null;
  /** #1554: set when GV Review settled this item (which was Your decision) by itself, and how. */
  settled?: FindingSettled | null;
  /** #1554, status `check`: the one question a person answers, written by code. */
  question?: string | null;
  /** #1554, status `check`: why it needs a person. */
  keep_reason?: KeepReason | null;
  /** #1554: the same item on other pages or rounds, folded into this one. */
  also_on?: FindingAlsoOn[] | null;
  /** #1554, `source: vendor_arithmetic`: code's sum of the parts, in inches ("80.125"). */
  parts_sum?: string | null;
  /**
   * #1569: how much a Wrong (or Your-decision) item matters, by code: ordering only, never a status
   * or a colour. `rank` 1 is the biggest of its status; the cloud drawing and the report use the
   * same order. Added on a stored answer (not while it streams).
   */
  impact?: FindingImpact | null;
  /** #1548: set when a later message of the chat corrected it; this is its newest version. */
  updated_in?: FindingLater | null;
  /**
   * #1581: where the zero-click cloud drawing puts it, by code (`proof.py`): only Wrong items code
   * proved are clouded. Added on a stored answer (not while it streams); follows the decision.
   */
  proof?: FindingProof | null;
  /** #1611: `code` when code paired the architect's and the vendor's labels by itself. */
  paired_by?: string | null;
}

/** #1581: the zero-click cloud drawing's group for a finding, and its words. */
export type ProofGroup = 'clouded' | 'right' | 'rule_note' | 'not_compared' | 'dismissed';

/**
 * #1839: the GV rule an architect's size breaks while the vendor prints that same size: decided,
 * never red; GV asks the architect (`rule_notes.py`).
 */
export interface FindingRuleNote {
  /** The rule's subject: "filler width", "sink front offset". */
  name: string;
  rule_id?: string | null;
  /** The settings in force as printed: 1"-2", 4". */
  settings: string;
  origin: 'GV standard' | 'project value' | 'drawing set value';
  /** The architect's size, which the vendor prints too. */
  architect: string;
  /** "GV's standard is 1"-2"". */
  standard: string;
  /** The ready question for the architect (an RFI line). */
  question: string;
}

export interface FindingProof {
  group: ProofGroup;
  /** Wrong items: what it should be and what governs it: '37" (architect 3'-1", page 28)'. */
  should_be?: string | null;
  /** Clouded items: the check that proved it, in plain words. */
  proof?: string | null;
  /** The other groups: why it is not clouded, in one line. */
  reason?: string | null;
  /** #1619, not_compared: the architect-first rule (R1-R10) it falls under. */
  rule?: string | null;
  /** #1619, not_compared: the reason (the Not-compared list's group). */
  not_compared?: string | null;
  /** #1619, not_compared: the trade's words for it ("Field verify", "RFI to the architect", "Illegible", "Not reviewed: …"). */
  label?: string | null;
  /** #1619, not_compared: what is needed to compare it. */
  settle?: string | null;
  /** #1839, rule_note: the GV rule and the question for the architect. */
  rule_note?: FindingRuleNote | null;
}

/** #1569: code's impact score (0-100), its place among the items of its status, and why. */
export interface FindingImpact {
  score: number;
  rank?: number | null;
  why: string;
}

export type CheckReason = 'different_extent' | 'which_governs' | 'pairing_not_certain' | 'scaled' | 'site_verify';

/** #1545: the reviewer's reasoning on a "Your decision" item, in plain words. */
export interface FindingDecisionHelp {
  measures: string;
  /** The AI's best estimate in inches, how far off it may be, and how it got it; null: none. */
  estimate?: { value_in: number; plus_minus_in: number; method: string } | null;
  likely: 'probably fine' | 'probably wrong' | 'can\'t tell';
  confirm: string;
}

/** #1554: how an item that was Your decision ended when code settled it. */
export type SettledOutcome = 'wrong' | 'right' | 'merged' | 'not_a_difference';

/** #1554: why an item still needs a person. */
export type KeepReason =
  | 'architect_prints_nothing'
  | 'measures_something_else'
  | 'which_governs'
  | 'unreadable'
  | 'needs_job_value'
  | 'layout'
  | 'vendor_disagrees'
  | 'architect_outside_gv_rule'
  | 'outside_gv_rule'
  | 'other';

export interface FindingSettled {
  outcome: SettledOutcome;
  /** The rule or tool code used (same_item, gv_limits, vendor_sum, printed_vs_printed, …). */
  method: string;
  /** What code did and found, in one plain sentence. */
  how: string;
  was?: 'check';
}

export interface FindingAlsoOn {
  finding_id: string;
  page?: number | null;
  vs_page?: number | null;
  round?: number | null;
  item?: string | null;
  vendor?: string | null;
  architect?: string | null;
  /** Its spot on that page: the cloud drawing marks it there too. */
  vendor_box?: FindingBox | null;
  arch_box?: FindingBox | null;
}

/** #1554: one Your-decision item GV Review settled by itself (the folded group's line). */
export interface ChatResolverEntry {
  finding_id: string;
  page?: number | null;
  vs_page?: number | null;
  item?: string | null;
  vendor?: string | null;
  architect?: string | null;
  outcome: SettledOutcome;
  method: string;
  how: string;
  /** The finding it was folded into. */
  into?: string | null;
}

/** #1554: the resolver's pass on a whole-set answer: counts before and after, and what it settled. */
export interface ChatResolver {
  before: { wrong: number; decision: number; right: number };
  after: { wrong: number; decision: number; right: number };
  settled: ChatResolverEntry[];
  kept: Record<string, number>;
  reads: number;
  cost_usd: string;
}

/** #1548: the later answer of the same chat that corrected a finding. */
export interface FindingLater {
  message_id: string;
  answer?: number | null;
  version: number;
}

/** #1548: a later message's correction of an earlier answer's finding (`A1-F5`). */
export interface ChatFindingUpdate {
  ref: string;
  /** The earlier answer the finding belongs to. */
  message_id: string;
  finding_id: string;
  answer: number;
  by_answer?: number | null;
  version: number;
  /** The new version (the last one, when dropped). */
  finding: ChatFinding;
  /** Why it was dropped; null when it was updated. */
  dropped?: string | null;
}

export type EvidenceKind = 'observed' | 'derived' | 'inferred';

/** Why something was not checked (an unknown record). */
export type UnknownWhy =
  | 'unreadable' | 'not_opened' | 'missing_job_value' | 'pairing_uncertain' | 'refused_alignment'
  /** #1584, the vendor consistency check: an area on one side only, a round or an indexed sheet the set lacks. */
  | 'no_counterpart' | 'missing_round' | 'not_in_set'
  /** #1623, the finish check: different tag systems, a prototype-only finish, a note naming a part. */
  | 'not_comparable';

/** One thing the answer did not check, and what would settle it (built by code). */
export interface ChatUnknown {
  id: string;
  page?: number | null;
  what: string;
  why: UnknownWhy;
  needs: string;
  /** `page`: a whole page; `item`: one value; `set`: the drawing set itself (not counted against the answer). */
  scope?: 'page' | 'item' | 'set' | null;
}

/**
 * Code's numbers for a finding (never the AI's arithmetic), as decimal strings ("37", "37.5"); the
 * difference is vendor minus architect. A value code could not read as a length is null ("not a
 * number": the printed words are shown as written instead).
 */
export interface FindingInches {
  architect: string | number | null;
  vendor: string | number | null;
  difference: string | number | null;
}

/** Confirmed: printed and second read agree. Likely: two checks pass. To check: anything else. */
export type TrustLevel = 'confirmed' | 'likely' | 'to_check';

/**
 * Printed at the spot: `match` (the value is printed in its box), `not_found`, `no_text` (drawn as
 * pen strokes, cannot be checked this way), `no_box` (no box to search), `not_a_number`, `not_run`.
 */
export type PrintedResult = 'match' | 'not_found' | 'no_text' | 'no_box' | 'not_a_number' | 'not_run';

/** The checks code recorded on a finding (CONTRACT3 "Backend changes"). */
export interface FindingChecks {
  printed_architect?: PrintedResult | null;
  printed_vendor?: PrintedResult | null;
  /** A fresh model saw only the two crops; code compared its numbers with the finding. */
  second_read?: 'agrees' | 'disagrees' | 'unsure' | 'not_run' | null;
  /** The recorded result of the GV check code ran (`source: rules`). */
  rule?: 'agrees' | 'disagrees' | 'not_run' | null;
  /** The vendor's parts added up by code (`source: vendor_arithmetic`). */
  arithmetic?: 'adds_up' | 'does_not_add_up' | 'not_a_number' | null;
  /** The lead checked it on the drawing itself. */
  lead_checked?: boolean | null;
  /** Code's reading ledger: a string that adds up, or other views agreeing. */
  ledger?: 'agrees' | 'disagrees' | null;
  /** Millimetres and inches printed together (940 [37]) agree. */
  units?: 'agrees' | 'disagrees' | null;
  /**
   * #1498: when the second read disagreed or was unsure, a stronger model read the spot again from a
   * sharper crop, and code took the majority of the three readings (it stands in for the second read).
   */
  adjudication?: 'agrees_after_adjudication' | 'disagrees' | 'unsure' | null;
  /** #1498, whole-set answers: an adversarial model looked for what is wrong (`objects` = To check). */
  critic?: 'passed' | 'objects' | 'unsure' | 'not_run' | null;
  /** #1531: code's own reading of each side's value at its box (the drawing's text, or the vendor's drawn ink by two OCR engines). */
  verify_architect?: VerifiedValue | null;
  verify_vendor?: VerifiedValue | null;
}

/**
 * #1531: what code read at one spot for one value, $0 and no AI. `match` (by the drawing's text, or by
 * two OCR engines on the vendor's drawn ink), `mismatch` (the drawing prints another value there),
 * `cannot_read` (with why). `words` is the evidence detail's line, written by code.
 */
export interface VerifiedValue {
  result: 'match' | 'mismatch' | 'cannot_read';
  route: 'text' | 'drawn' | 'none';
  read: string | null;
  reads?: string[] | null;
  reason: string;
  value: string | null;
  pending?: boolean | null;
  words: string;
}

/** One reading of a finding's values (#1498): the finding's own (`first`), the second and third reads. */
export interface FindingRead {
  role?: 'first' | 'second' | 'third' | null;
  /** "lead", "haiku", "sonnet"… */
  model: string;
  value_arch: string | null;
  value_vendor: string | null;
  /** Whether the model said it was sure (null for the finding's own values). */
  sure: boolean | null;
  /** The third read's crop resolution, and whether a turned copy was shown (sideways text). */
  dpi?: number | null;
  turned?: boolean | null;
}

/** The critic's look at one finding (#1498). */
export interface FindingCritic {
  result: 'passed' | 'objects' | 'unsure';
  answers?: Partial<Record<'same_item' | 'right_dimension' | 'vendor_ink' | 'architect_side', 'yes' | 'no' | 'unsure'>> | null;
  /** What is wrong, in the critic's words (or code's), when it objects. */
  note?: string | null;
  model?: string | null;
}

/** A comparison with the vendor's own drawing (an earlier round), shown as "Unchanged since round 1". */
export interface RoundMatch {
  page?: number | null;
  round?: number | null;
  vendor_round?: number | null;
  label?: string | null;
  value?: string;
  unchanged?: boolean;
  /** How code compared the item with the architect instead (null: it could not). */
  compared?: 'carrier' | 'code_pair' | 'earlier_round' | 'round_pick' | null;
  via?: string | null;
  architect?: string | null;
  words?: string;
}

export type RoundStatus = 'fixed_in_later_round' | 'still_wrong' | 'new_in_later_round' | 'not_in_later_round' | 'single_round';

/** One difference GV Review's own checks recorded that the answer did not include. */
export interface TrustLeftOut {
  page?: number | null;
  /** The screen's shape; the server sends `what` (and `record_id`) instead. */
  item?: string | null;
  what?: string | null;
  record_id?: string | null;
  /** e.g. "architect 30" · vendor 31"" as code wrote it. */
  values?: string | null;
  /** "code rule" or "AI page review". */
  source?: string | null;
}

/**
 * The trust report code builds after an answer's checks (block `trust`, event `trust`). Its shape
 * is the screen's proposal (the backend notes do not fix it yet): every field is optional, and the
 * screen counts levels and checks from the findings themselves when a field is missing.
 */
export interface ChatTrust {
  levels?: { confirmed: number; likely: number; to_check: number } | null;
  checks?: {
    printed?: { match: number; not_found: number; no_text: number } | null;
    second_read?: { agrees: number; disagrees: number; unsure: number } | null;
    verified?: { checked: number; total: number } | null;
    rules?: string[] | null;
    arithmetic?: { checked: number; agrees: number } | null;
  } | null;
  /**
   * Of the differences GV Review's own checks recorded for this set, how many the answer includes.
   * The server sends `recorded` / `included`; `matched` / `total` are the older screen names.
   */
  gv_checks?: {
    matched?: number | null;
    total?: number | null;
    recorded?: number | null;
    included?: number | null;
    left_out?: TrustLeftOut[] | null;
  } | null;
  /** The reading ledger's own "never drop a recorded result" check (whole-set answers). */
  recorded_results?: {
    recorded: number;
    included: number;
    dismissed?: number | null;
    missing?: RecordedResult[] | null;
    /** Of `missing`, the ones `gv_checks` does not count (e.g. the review list's failures). */
    also_left_out?: RecordedResult[] | null;
  } | null;
  /** The reading ledger's counts, all by code. */
  ledger?: LedgerReport | null;
  /** What it did NOT check, in code's plain words (pen-stroke numbers, pages not opened…). */
  not_checked?: string[] | null;
  /** Rounds of comments: round-1 differences fixed in a later round, still wrong, not in it, and new ones. */
  /** Findings by evidence kind. */
  evidence?: Partial<Record<EvidenceKind, number>> | null;
  /** "What's right" items not compared (not counted as right). */
  not_compared?: number | null;
  /** What the answer did not check, counted by why. */
  unknowns?: { total: number; by_why?: Partial<Record<UnknownWhy, number>> | null } | null;
  rounds?: {
    fixed: number;
    still_wrong: number;
    new: number;
    not_in_later?: number | null;
    /** Unchanged since an earlier round: information only, never counted as right. */
    unchanged?: number | null;
    /** compare_rounds: changes nobody asked for (no finding, no GV markup nearby). */
    unrequested?: number | null;
    /** The line code wrote for it. */
    line?: string | null;
  } | null;
  /** Where the answer's time and money went, phase by phase (#1512); older answers have none. */
  timings?: ChatTimings | null;
  /** #1862: a full check's reading coverage (numbers read of those printed); older answers have none. */
  reading?: ChatReading | null;
}

/** #1862: how many printed numbers code read on one side's drawings. */
export interface ChatReadingSide {
  read: number;
  /** About how many are printed (counted by code). */
  printed: number;
  percent: number;
}

/** #1862: one unread number: whose drawing, where (PDF points of the page as shown), and how it was printed. */
export interface ChatReadingSpot {
  side: string;
  box: number[];
  kind: string;
}

/** #1862: a page's unread numbers that share one reason. */
export interface ChatReadingGroup {
  reason: string;
  words: string;
  count: number;
  spots: ChatReadingSpot[];
}

/** #1862: one page of the reading coverage. */
export interface ChatReadingPage {
  file?: string | null;
  page: number;
  /** The page picture can show its spots (the set's first file). */
  viewable?: boolean;
  /** False: the drawn reader did not reach this page; its pen strokes are not counted. */
  strokes_counted: boolean;
  vendor_read: number;
  vendor_printed: number;
  architect_read: number;
  architect_printed: number;
  not_read: ChatReadingGroup[];
}

/** #1862: the reading coverage of a full check, counted by code when the set pack was made. */
export interface ChatReading {
  lines: string[];
  vendor?: ChatReadingSide | null;
  architect?: ChatReadingSide | null;
  pages: ChatReadingPage[];
  pages_not_counted?: number;
}

/** One phase of an answer: wall-clock seconds and what its model calls cost. */
export interface ChatTimingPhase {
  phase: string;
  label: string;
  seconds: number;
  cost_usd: string;
  calls: number;
}

/** One helper of an answer: its pages (a full check's, planned by code), calls, time and cost. */
export interface ChatTimingHelper {
  number: number;
  description: string;
  pages: number[];
  calls: number;
  seconds: number;
  cost_usd: string;
  stopped?: string | null;
}

export interface ChatTimings {
  total_seconds: number;
  cost_usd: string;
  phases: ChatTimingPhase[];
  helpers: ChatTimingHelper[];
}

/** A result GV Review recorded (a code FAIL, or an evaluator-checked AI page review difference). */
export interface RecordedResult {
  key: string;
  pages?: number[] | null;
  what?: string | null;
  architect?: string | null;
  vendor?: string | null;
}

/**
 * The reading ledger (CONTRACT3 "Ledger"): every dimension string and value the AI recorded, added
 * up and compared across views by code.
 */
export interface LedgerReport {
  strings: { total: number; add_up: number; off: number; not_a_number: number; vendor_off?: number; architect_off?: number };
  views: { groups: number; consistent: number; inconsistent: number; not_compared: number; typical_groups?: number };
  completeness: {
    compared: boolean;
    architect_items: number;
    vendor_items: number;
    missing_on_vendor: string[];
    extra_on_vendor: string[];
    not_looked_at?: string[] | null;
  };
}

/**
 * Chat v5 (CONTRACT5-accuracy): a number the answer states, backed by its spot on a drawing (the
 * model's `cite`); code checked it is printed there and cropped the spot.
 */
export interface ChatCite {
  id: string;
  page: number;
  /** The value as the answer states it. */
  value?: string | null;
  box?: FindingBox | null;
  /** The crop code made at the box, served by the pictures route. */
  picture_id?: string | null;
  /** Printed at the spot: match / not_found / no_text … (code's check). */
  printed?: PrintedResult | null;
  evidence_kind?: EvidenceKind | null;
}

/** One checkable claim of an answer, how code checked it, and the points it earned (0 to 1). */
export interface AccuracyClaim {
  claim: string;
  how: string;
  points: number;
  evidence_kind?: EvidenceKind | null;
}

/**
 * Chat v5: the answer's accuracy, computed by CODE from its claims' checks (never by the model).
 * `score` is 0 to 100, or null when there was nothing to check ("n/a").
 */
export interface ChatAccuracy {
  score: number | null;
  level?: string | null;
  claims?: number | null;
  breakdown?: AccuracyClaim[] | null;
  /** Whole-set answers: the share of pages accounted for, already applied to the score. */
  coverage_factor?: number | null;
  /** How the points per claim were calibrated on GV's test sets. */
  calibration?: string | null;
  /** "What's right" items not compared: not scored. */
  not_compared?: number | null;
  /** What the answer did not check. */
  unknowns?: number | null;
}

export type CoverageStatus = 'same' | 'different' | 'not_comparable' | 'not_opened';

export interface CoveragePage {
  page: number;
  /** The page it was paired with (the architect's). */
  pair_page?: number | null;
  status: CoverageStatus;
  /** Why it is not comparable, or what differs. */
  note?: string | null;
}

/** The page-by-page map of a whole-set answer: which pages were compared and how they came out. */
export interface ChatCoverage {
  pages: CoveragePage[];
  counts?: { total: number; same: number; different: number; not_comparable: number; not_opened: number } | null;
}

export type ChatTool =
  | 'bash_tool'
  | 'view'
  | 'create_file'
  | 'str_replace'
  | 'present_files'
  | 'TaskCreate'
  | 'TaskUpdate'
  | 'Agent'
  | 'review_results'
  | 'compare_rounds'
  | 'verify_value'
  | 'read_number'
  | 'measure_on_drawing'
  | 'zoom'
  | 'read_under_markup'
  | 'remember'
  | 'drawing_facts';

/** One tool the assistant used. `step_start` sends it without its output; `step_end` sends it whole. */
export interface ChatStep {
  id: string;
  tool: ChatTool;
  /** The model's own description of the step ("Render page 3"). */
  label: string;
  command?: string | null;
  path?: string | null;
  output?: string | null;
  /** Null while it runs. */
  ok: boolean | null;
  seconds?: number | null;
  /** A picture it viewed, served at `/chats/{chat}/pictures/{picture_id}`. */
  picture_id?: string | null;
  /** TaskCreate / TaskUpdate: the whole task list after it. */
  tasks?: ChatTask[] | null;
  /** Agent: the helpers it ran, each with its own steps and report. */
  agents?: ChatAgent[] | null;
}

export interface ChatTask {
  id: string;
  subject: string;
  /** Shown while it is in progress ("Checking the sinks"). */
  active_form?: string | null;
  status: 'pending' | 'in_progress' | 'completed';
}

export interface ChatAgent {
  id: string;
  description: string;
  /** The model it ran on ("claude-sonnet-5-5"). */
  model: string;
  /** Null while it runs. */
  ok: boolean | null;
  report: string | null;
  steps: ChatStep[];
  stopped?: 'cost_cap' | 'turn_limit' | 'cancelled' | 'error' | null;
}

/** A file the assistant made and handed over, downloaded from `url`. */
export interface ChatFile {
  id: string;
  /** Chat v4: what a full check made: the cloud drawing (the main output) or the check report. */
  kind?: 'cloud_drawing' | 'check_report' | string | null;
  name: string;
  /** Bytes. */
  size: number;
  mime: string;
  url: string;
}

/** #1569: one answer to a plan-check question, sent with the full check. */
export interface ChatPlanAnswer {
  id: string;
  answer: string;
}

export interface ChatPlanOption {
  value: string;
  label: string;
}

/** #1569: a question code asks before a paid full check, where it is unsure of the page map. */
export interface ChatPlanDoubt {
  id: string;
  kind: 'prototype' | 'role' | 'pair' | 'round' | 'heading';
  page: number;
  question: string;
  /** The likely answer first. */
  options: ChatPlanOption[];
  /** The page's picture (drawings only), when the set pack made it. */
  picture_url?: string | null;
  /** The drawing it is about, as fractions of the picture: x0, y0, x1, y1. */
  box?: [number, number, number, number] | null;
}

/** `GET /packages/{id}/full-check-plan` (#1569): code's plan in one line and its 0-3 questions. */
export interface ChatFullCheckPlan {
  /** `building`: ask again in a moment; `none`: no page map can be made, run without questions. */
  state: 'ready' | 'building' | 'none';
  /** "60 pages · 22 pairs · rounds 1-2 · 3 general pages". */
  summary: string | null;
  doubts: ChatPlanDoubt[];
  /** Further unsure places, compared as read. */
  more: number;
  /** #1823: the full check's fixed price for the products asked ("17.75"); null until the page map is read. */
  price_usd?: string | null;
  /** The content pages in scope it counts (drawings of those products; title and divider pages left out). */
  price_pages?: number | null;
  /** #1871: how far the drawings are read; the check's AI steps start only once every page is read. */
  reading?: { state: 'reading' | 'ready' | 'failed'; done: number; total: number; unreadable: number; why?: string | null } | null;
}

/** `POST /chats/{id}/messages`. */
export interface SendMessageBody {
  /** 1 to 8000 characters. */
  text: string;
  model: string;
  effort: ChatEffort;
  thinking: boolean;
  /** Chat v4: `full_check` runs the app's own full-set check; absent means an ordinary message. */
  mode?: ChatMode;
  /** Chat v4: the products to focus on; absent means the set's own product type. */
  products?: ChatProduct[];
  /** Continue (#1514): the interrupted answer to go on from; the server then asks the same way. */
  continue_from?: string;
  /** #1569, a full check only: the answers to the plan check's questions. */
  plan_answers?: ChatPlanAnswer[];
  /** #1551: the reviewer's corrected "Understood as" (Edit), sent with the question unchanged. */
  task?: ChatTaskEdit;
}

/** The stream's events, in the contract's words. `index` is the block's place in `blocks`. */
export type ChatEvent =
  | { event: 'message_start'; data: { message_id: string; model: string; effort: ChatEffort; thinking: boolean } }
  | { event: 'thinking_start'; data: { index: number } }
  | { event: 'thinking_delta'; data: { index: number; text: string } }
  | { event: 'thinking_end'; data: { index: number; seconds: number } }
  | { event: 'text_delta'; data: { index: number; text: string } }
  | { event: 'step_start'; data: { index: number; step: ChatStep } }
  | { event: 'step_end'; data: { index: number; step: ChatStep } }
  | { event: 'tasks'; data: { tasks: ChatTask[] } }
  | { event: 'agent_start'; data: { step_index: number; agent: Pick<ChatAgent, 'id' | 'description' | 'model'> } }
  | { event: 'agent_step'; data: { step_index: number; agent_id: string; step: ChatStep } }
  | { event: 'agent_end'; data: { step_index: number; agent: Omit<ChatAgent, 'steps'> } }
  | { event: 'file'; data: { file: ChatFile } }
  | { event: 'usage'; data: { cost_usd: string } }
  | { event: 'message_end'; data: { message: ChatMessage } }
  | { event: 'error'; data: { message: string } }
  | { event: 'finding'; data: { finding: ChatFinding } }
  | { event: 'finding_dropped'; data: { finding_id: string } }
  | { event: 'finding_update'; data: { index?: number; update: ChatFindingUpdate } }
  | { event: 'coverage'; data: { coverage: ChatCoverage } }
  | { event: 'progress'; data: { index?: number; progress: ChatProgress } }
  | { event: 'scope'; data: { index?: number | null; scope: ChatScope } }
  | { event: 'understood'; data: { index?: number | null; understood: ChatUnderstood } }
  | { event: 'trust'; data: { trust: ChatTrust } }
  | { event: 'cite'; data: { cite: ChatCite } }
  | { event: 'accuracy'; data: { accuracy: ChatAccuracy } }
  | { event: 'unknowns'; data: { unknowns: ChatUnknown[] } }
  | { event: 'resolver'; data: { index?: number; resolver: ChatResolver } }
  | { event: 'fallback'; data: { index?: number; fallback: ChatFallback } }
  | LiveEvent;

export type ChatEventName = ChatEvent['event'];

const EVENT_NAMES: ReadonlySet<string> = new Set<ChatEventName>([
  'message_start', 'thinking_start', 'thinking_delta', 'thinking_end', 'text_delta', 'step_start', 'step_end',
  'tasks', 'agent_start', 'agent_step', 'agent_end', 'file', 'usage', 'message_end', 'error',
  'finding', 'finding_dropped', 'finding_update', 'coverage', 'progress', 'scope', 'trust', 'cite', 'accuracy', 'unknowns', 'understood',
  'resolver', 'fallback', ...LIVE_EVENTS,
]);

/**
 * A parsed SSE frame as a contract event, or null for anything else (a frame this screen does not
 * know, or one with no data object). Unknown events are skipped, so a newer server never breaks it.
 */
export function chatEventOf(frame: SseFrame): ChatEvent | null {
  if (!EVENT_NAMES.has(frame.event) || frame.data === null || typeof frame.data !== 'object') return null;
  return { event: frame.event, data: frame.data } as ChatEvent;
}

// ── Paths ───────────────────────────────────────────────────

/** The same base the rest of the app uses (`/api/v1`, proxied to the API in development). */
const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

const id = (value: string) => encodeURIComponent(value);

/** Every chat route in one place, so a path change from the backend is one edit here. */
export const chatPaths = {
  /** With the drawing set, so `package_spent_usd` is that set's chat spend (else always 0.00). */
  models: (packageId?: string) => `${BASE}/assistant/models${packageId ? `?package_id=${id(packageId)}` : ''}`,
  chats: (packageId: string) => `${BASE}/packages/${id(packageId)}/chats`,
  chat: (chatId: string) => `${BASE}/chats/${id(chatId)}`,
  messages: (chatId: string) => `${BASE}/chats/${id(chatId)}/messages`,
  /** A message's events: the running one's so far then live, or a finished one's end at once. */
  events: (chatId: string, messageId: string) => `${BASE}/chats/${id(chatId)}/messages/${id(messageId)}/events`,
  cancel: (chatId: string) => `${BASE}/chats/${id(chatId)}/cancel`,
  file: (chatId: string, fileId: string) => `${BASE}/chats/${id(chatId)}/files/${id(fileId)}`,
  picture: (chatId: string, pictureId: string) => `${BASE}/chats/${id(chatId)}/pictures/${id(pictureId)}`,
  /** Chat v2: code draws the message's findings on a copy of the upload and returns it as a file. */
  markupPdf: (chatId: string, messageId: string) => `${BASE}/chats/${id(chatId)}/messages/${id(messageId)}/markup-pdf`,
  /** #1525: the cloud drawing and report made again from the reviewer's decisions (POST). */
  cloudDrawing: (chatId: string, messageId: string) => `${BASE}/chats/${id(chatId)}/messages/${id(messageId)}/cloud-drawing`,
  /** #1584: the vendor consistency check, by code ($0, POST). */
  consistency: (chatId: string) => `${BASE}/chats/${id(chatId)}/consistency`,
  /** #1710: a running full check's whole live state (a page that reconnects). */
  live: (chatId: string) => `${BASE}/chats/${id(chatId)}/live`,
  /** #1717: the vendor's page picture in the set pack (100 dpi), for the evidence panel's boxed spot. */
  planPage: (packageId: string, page: number) => `${BASE}/packages/${id(packageId)}/plan-pages/${page}`,
  /** #1569: code's plan for a full check of the set, and where it is unsure (no AI call). */
  fullCheckPlan: (packageId: string) => `${BASE}/packages/${id(packageId)}/full-check-plan`,
  /** #1522: the reviewer's Agree / Not a problem on one finding (POST). */
  decision: (chatId: string, messageId: string, findingId: string) =>
    `${BASE}/chats/${id(chatId)}/messages/${id(messageId)}/findings/${id(findingId)}/decision`,
};

/** Record the reviewer's Agree / Not a problem on one finding (with its note and, for an Agree, the value it should be); the server answers with who and when. */
export function decideFinding(chatId: string, messageId: string, findingId: string, decision: FindingDecisionChoice, extra?: FindingDecisionExtra): Promise<FindingDecision> {
  const note = extra?.note?.trim();
  const shouldBe = decision === 'agree' ? extra?.should_be?.trim() : '';
  return json<FindingDecision>(chatPaths.decision(chatId, messageId, findingId), {
    method: 'POST',
    body: JSON.stringify({ decision, ...(note ? { note } : {}), ...(shouldBe ? { should_be: shouldBe } : {}) }),
  });
}

// ── Requests ────────────────────────────────────────────────

/** A non-2xx answer as an ApiError, from the server's envelope when it sent one. */
async function failure(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
    if (typeof envelope?.message !== 'string') throw new Error('no envelope');
  } catch {
    envelope = {
      error: 'unreadable_response',
      message: `The server returned ${response.status} and a body this client could not parse.`,
      request_id: response.headers.get('x-request-id') ?? 'unknown',
    };
  }
  return new ApiError(response.status, envelope);
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.body ? { 'Content-Type': 'application/json' } : {}), ...(init?.headers ?? {}) },
  });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

export function getChatModels(packageId?: string): Promise<ChatModels> {
  return json<ChatModels>(chatPaths.models(packageId));
}

export function listChats(packageId: string): Promise<ChatList> {
  return json<ChatList>(chatPaths.chats(packageId));
}

export function createChat(packageId: string): Promise<CreatedChat> {
  return json<CreatedChat>(chatPaths.chats(packageId), { method: 'POST', body: '{}' });
}

/**
 * #1569: the plan check before a full check ($0); `building` means ask again shortly. With the
 * products picked (#1823), its fixed price for them.
 */
export function getFullCheckPlan(packageId: string, products?: readonly ChatProduct[] | null): Promise<ChatFullCheckPlan> {
  const query = (products ?? []).map((product) => `products=${encodeURIComponent(product)}`).join('&');
  return json<ChatFullCheckPlan>(query ? `${chatPaths.fullCheckPlan(packageId)}?${query}` : chatPaths.fullCheckPlan(packageId));
}

export function getChat(chatId: string): Promise<Chat> {
  return json<Chat>(chatPaths.chat(chatId));
}

/** Stop the running message between steps (its sandbox command and helpers too). */
export function cancelChat(chatId: string): Promise<{ cancelled: boolean }> {
  return json<{ cancelled: boolean }>(chatPaths.cancel(chatId), { method: 'POST', body: '{}' });
}

/** Make the marked-up PDF of a finished message's findings (code only, no AI call); returns the file. */
export function makeMarkupPdf(chatId: string, messageId: string): Promise<ChatFile> {
  return json<ChatFile>(chatPaths.markupPdf(chatId, messageId), { method: 'POST', body: '{}' });
}

/** #1803: the remade cloud drawing, and every file made with it (the report, the Not-compared list, the package). */
export type RemadeFiles = ChatFile & { made?: ChatFile[] };

/** #1525: make the cloud drawing (and report) again from the reviewer's decisions; returns the new drawing and (#1803) every file made with it. */
export function remakeCloudDrawing(chatId: string, messageId: string): Promise<RemadeFiles> {
  return json<RemadeFiles>(chatPaths.cloudDrawing(chatId, messageId), { method: 'POST', body: '{}' });
}

/** #1584: the "model" of an answer code wrote alone (the vendor consistency check): no AI. */
export const CHECK_MODEL = 'code-check';

/**
 * #1584: the vendor consistency check, by code only ($0, no AI): stored in the chat as a question and
 * its answer, which it returns. A 409 says why it cannot run yet (a message running, the set still read).
 */
export function runConsistencyCheck(chatId: string): Promise<ChatMessage> {
  return json<ChatMessage>(chatPaths.consistency(chatId), { method: 'POST', body: '{}' });
}

/**
 * Send a message and read its events as they arrive, calling `onEvent` for each one in order.
 * Resolves with the stored assistant message from `message_end`.
 *
 * A refused request (409 while another message runs, 422 for a bad body) rejects with its ApiError
 * before any event. A stream that ends without `message_end`, or a frame that is not JSON, rejects
 * with plain words: a cut-off answer must not read as a finished one. Aborting `signal` rejects with
 * the browser's AbortError, which the caller tells apart from a failure.
 */
export async function streamChatMessage(
  chatId: string,
  body: SendMessageBody,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<ChatMessage> {
  const response = await fetch(chatPaths.messages(chatId), {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });
  return readChatStream(response, onEvent);
}

/**
 * Follow a message (#1479), like claude.ai when you come back to a chat that is still answering:
 * the running message's events so far (streamed words arrive merged), then each new one live, until
 * `message_end`; for a message already finished, `message_end` at once. Same events and same
 * failures as `streamChatMessage`. Aborting `signal` only stops reading: the message goes on.
 */
export async function followChatMessage(
  chatId: string,
  messageId: string,
  onEvent: (event: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<ChatMessage> {
  const response = await fetch(chatPaths.events(chatId, messageId), { headers: { Accept: 'text/event-stream' }, signal });
  return readChatStream(response, onEvent);
}

async function readChatStream(response: Response, onEvent: (event: ChatEvent) => void): Promise<ChatMessage> {
  const requestId = response.headers.get('x-request-id') ?? 'unknown';
  if (!response.ok || !response.body) throw await failure(response);

  const unreadable = () =>
    new ApiError(502, { error: 'unreadable_stream', message: 'The answer could not be read. Reload the chat to see what was saved.', request_id: requestId });
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      let frames: SseFrame[];
      try {
        const parsed = parseSseFrames(buffered + decoder.decode(value, { stream: true }));
        frames = parsed.frames;
        buffered = parsed.rest;
      } catch {
        // A frame that is not JSON: said in plain words, never the parser's own text.
        throw unreadable();
      }
      for (const frame of frames) {
        const event = chatEventOf(frame);
        if (!event) continue;
        onEvent(event);
        if (event.event === 'message_end') {
          if (!event.data.message || typeof event.data.message.id !== 'string') throw unreadable();
          return event.data.message;
        }
      }
    }
  } finally {
    // The stored message, or a failure, is the last thing that matters: stop reading either way.
    reader.cancel().catch(() => {});
  }
  throw new ApiError(502, {
    error: 'incomplete_stream',
    message: 'The answer stopped before it was complete. Reload the chat to see what was saved.',
    request_id: requestId,
  });
}

/**
 * A failure in plain words. The server's own messages already are; a refused send, a lost
 * connection or an unreadable reply are said here instead, never as a parser's or browser's text.
 */
export function chatFailureWords(error: unknown): string {
  if (error instanceof ApiError) {
    // Another chat's full check holds the one paid slot (#1770): the server's words say so.
    if (error.status === 409 && error.code === 'paid_job_running' && error.message) return error.message;
    if (error.status === 409) return 'Another message is still running in this chat. Wait for it to finish, or press Stop.';
    if (error.status === 422) return 'That message could not be sent. Check it is not too long and try again.';
    if (error.status === 404 && (error.code === 'unreadable_response' || !error.message)) return 'This was not found on the server.';
    if (error.code === 'unreadable_response' || !error.message) return 'The assistant could not answer right now. Try again.';
    return error.message;
  }
  // Only a request that never reached the server (fetch's TypeError) is a connection problem.
  if (error instanceof TypeError) return 'The assistant could not be reached. Check the connection and try again.';
  return 'Something went wrong. Try again.';
}

/** True for the browser's own "stopped on purpose" (an aborted fetch). */
export function isAbort(error: unknown): boolean {
  return error instanceof DOMException ? error.name === 'AbortError' : error instanceof Error && error.name === 'AbortError';
}

/** The calls the chat page makes. The app uses these; tests pass fakes. */
export interface ChatApi {
  models: typeof getChatModels;
  list: typeof listChats;
  create: typeof createChat;
  get: typeof getChat;
  send: typeof streamChatMessage;
  follow: typeof followChatMessage;
  cancel: typeof cancelChat;
  markup: typeof makeMarkupPdf;
  /** #1522: Agree / Not a problem per finding; optional so a fake API can leave it out. */
  decide?: typeof decideFinding;
  remakeCloud?: typeof remakeCloudDrawing;
  /** #1584: the $0 vendor consistency check; optional so a fake API can leave it out (no menu item then). */
  consistency?: typeof runConsistencyCheck;
  /** #1569: the plan check; optional so a fake API can leave it out (then no questions). */
  plan?: typeof getFullCheckPlan;
}

export const LIVE_CHAT_API: ChatApi = {
  models: getChatModels,
  list: listChats,
  create: createChat,
  get: getChat,
  send: streamChatMessage,
  follow: followChatMessage,
  cancel: cancelChat,
  markup: makeMarkupPdf,
  decide: decideFinding,
  remakeCloud: remakeCloudDrawing,
  consistency: runConsistencyCheck,
  plan: getFullCheckPlan,
};
