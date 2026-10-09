/**
 * Types for the review assistant (#1128 backend, #1129 screen), in a module with no runtime imports.
 *
 * Written by hand from the agreed contract because the routes are not in the generated schema yet,
 * and an SSE response's frames never are (FastAPI describes it as `text/event-stream`, untyped).
 * Once the backend lands, `GET …/assistant` comes out of `schema.d.ts`; check these against it then.
 */

/** `GET /projects/{p}/packages/{k}/assistant`: whether it answers here, and what it starts with. */
export interface AssistantInfo {
  enabled: boolean;
  /** The model in words, for the footer ("Claude Sonnet"). */
  model_label: string;
  /** True when the model provider keeps no data from the question. */
  keeps_no_data: boolean;
  /** Questions that fit this review's state, in the server's order. */
  starters: string[];
}

/** What the reviewer is looking at, sent with the question when they leave the context chip on. */
export interface AssistantFocus {
  page_number?: number;
  /** A countertop row id, or a finding id. */
  record_id?: string;
}

/** One earlier turn, as plain text (no markers, no evidence). At most six go with a question. */
export interface AssistantHistoryTurn {
  role: 'user' | 'assistant';
  text: string;
}

export interface AssistantRequest {
  /** 1 to 500 characters. */
  question: string;
  history: AssistantHistoryTurn[];
  focus?: AssistantFocus;
}

/** `stage`: what the server is doing now. Each one replaces the last. */
export interface AssistantStage {
  id: 'records' | 'model' | 'guard';
  label: string;
}

/** What a `[[n]]` marker in the answer text points at. */
export interface AssistantCitation {
  kind: 'page' | 'countertop' | 'finding';
  page_number: number | null;
  record_id: string | null;
  label: string;
}

/** Records to draw under the answer. Drawn from the app's own API data by id, never from the text. */
export type AssistantEvidence =
  | { kind: 'countertop'; record_id: string }
  | { kind: 'blockers' }
  | { kind: 'no_countertop_pages' }
  | { kind: 'rows_not_checked' };

/** Navigation the answer offers. Nothing here records a decision. */
export type AssistantAction =
  | { kind: 'open_page'; page_number: number; label: string }
  | { kind: 'open_queue_item'; record_id: string; label: string };

export type AssistantMode = 'llm' | 'records_only' | 'refused' | 'disabled';

/** `answer`: the whole answer, once. */
export interface AssistantAnswer {
  /** Plain text with `[[0]]`, `[[1]]` markers indexing `citations`. */
  text: string;
  citations: AssistantCitation[];
  evidence: AssistantEvidence[];
  actions: AssistantAction[];
  /** Follow-up questions, shown after the latest answer only. */
  suggestions: string[];
  /** True when the server's guard matched every number and outcome against the records. */
  checked: boolean;
  mode: AssistantMode;
  model_id: string | null;
  /** The records the answer used, one line each. */
  sources: string[];
}

/**
 * `error`: the answer could not be produced. `message` is plain words, safe to show. The codes the
 * server sends today; any other code is shown the same way (its message, and Try again).
 */
export interface AssistantStreamError {
  code: 'model_busy' | 'model_account' | 'model_unavailable' | 'records_unavailable' | (string & {});
  message: string;
}
