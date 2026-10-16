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
  /** True when a question can open this set's drawings: the footer says "reads the drawings". */
  reads_drawings?: boolean;
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
  id: 'records' | 'model' | 'tool' | 'guard';
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
  | { kind: 'open_queue_item'; record_id: string; label: string }
  /** An entry of the simple review list (#1354): its own key (`ReviewItemOut.key`, `FailureOut.key` …). */
  | { kind: 'open_review_item'; item_key: string; page_number: number | null; label: string };

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
  /** The page whose two drawings the model was shown for this answer (#1431), else null or absent. */
  looked_at_page?: number | null;
  /** The page the question was about whose pictures were not shown to the model (QA 2026-10-16), else null or absent. */
  not_looked_at_page?: number | null;
  /** Why: `cost_cap`, the question was over its cost cap with the pictures. */
  not_looked_at_reason?: 'cost_cap' | null;
  /** The tools the assistant used, in order (owner, 2026-10-16); empty or absent for a one-call answer. */
  steps?: AssistantStep[];
  /** The assistant's last task list for this answer ("Progress"), if it made one. */
  plan?: AssistantPlanTask[];
  /** `cost_cap`: it stopped before finishing at the question's cost cap; the text says so. */
  stopped?: 'cost_cap' | null;
}

/** One task of the assistant's task list (claude.ai's Progress panel). */
export interface AssistantPlanTask {
  id: string;
  subject: string;
  /** Shown while it is in progress ("Verifying findings"). */
  active_form?: string | null;
  status: 'pending' | 'in_progress' | 'completed';
}

/** One tool the assistant used: a sandbox command and its output, a file or picture it viewed, the results, a task, helper agents. */
export interface AssistantStep {
  kind: 'bash' | 'view' | 'review_results' | 'task' | 'agents';
  /** The model's own description of the step ("Inspect page 2 text"), or "Added task: …", "Ran 4 agents · …". */
  label: string;
  command?: string | null;
  path?: string | null;
  page?: number | null;
  output?: string | null;
  ok: boolean;
  tasks?: AssistantPlanTask[];
  agents?: AssistantSubAgent[];
  /** A small JPEG of the picture it viewed, as a data URL (view of a picture only). */
  thumbnail?: string | null;
}

/** `step`: one tool the assistant just finished, streamed live; `agent` names the helper, null for the lead. */
export interface AssistantStepEvent {
  agent: string | null;
  step: AssistantStep;
}

/** One helper agent the assistant ran in parallel (`Agent`), with its own steps and report. */
export interface AssistantSubAgent {
  description: string;
  model: 'sonnet' | 'opus';
  steps: AssistantStep[];
  report: string;
  ok: boolean;
  stopped?: 'cost_cap' | 'failed' | null;
}

/**
 * `error`: the answer could not be produced. `message` is plain words, safe to show. The codes the
 * server sends today; any other code is shown the same way (its message, and Try again).
 */
export interface AssistantStreamError {
  code: 'model_busy' | 'model_account' | 'model_unavailable' | 'records_unavailable' | (string & {});
  message: string;
}
