/**
 * The simple review page's data (#1343): what still needs a person on one drawing set, in one call.
 *
 * `GET /projects/{p}/packages/{k}/review-items` is the W1 read API (#1338). Its types come from the
 * generated `schema.d.ts`, so a renamed field stops this page compiling.
 *
 * The writes (Confirm, Fix, Agree, the one-click choices, "Clear all as not checked") are the W3
 * action endpoints (#1351). They all go through one function, `act`.
 */

import { ApiError, type ErrorEnvelope } from './client';
import type { components } from './schema';

type Schemas = components['schemas'];

export type ReviewItemsOut = Schemas['ReviewItemsOut'];
export type ReviewItemsHeader = Schemas['ReviewHeaderOut'];
export type ReviewItem = Schemas['ReviewItemOut'];
export type ReviewFailure = Schemas['FailureOut'];
export type NotCheckedItem = Schemas['NotCheckedOut'];
/** A Company settings field a not-checked check waits for (#1373). */
export type NotCheckedSetting = Schemas['NotCheckedSettingOut'];
/** What the job values reader found for one of this job's values (#1409): shown beside its field. */
export type JobValueSuggestion = Schemas['JobValueSuggestionOut'];
export type UnaccountedRecord = Schemas['UnaccountedOut'];
export type BestGuess = Schemas['BestGuessOut'];
export type ReaderText = Schemas['ReaderTextOut'];
export type CountertopRef = Schemas['CountertopItemRefOut'];
export type ArchitectRef = Schemas['ArchitectRefOut'];
/** One wall layout a wall-only countertop may take, with the result the width check recorded. */
export type WallChoice = Schemas['WallChoiceOut'];
/** One cabinet of a run to mark, and one kind it may be marked as (#648). */
export type CabinetRow = Schemas['CabinetRowOut'];
export type CabinetChoice = Schemas['CabinetChoiceOut'];
/** A cabinet run proposed from the AI's stored widths, for a person to confirm (cabinets e2e). */
export type CabinetRunProposal = Schemas['CabinetRunProposalOut'];
export type CabinetRun = Schemas['CabinetRunOut'];
export type CabinetRunPiece = Schemas['CabinetRunPieceOut'];
/** The answer to a proposed cabinet run: which run, what each piece is, and the site width. */
export type CabinetRunAnswer = Schemas['CabinetRunIn'];
/** What to check on a countertop checked by hand: each piece, the overall and the rule (stored records only). */
export type WhatToCheck = Schemas['WhatToCheckOut'];
/** A spot on a stored page. `polygon` is in stored page space (0..1), as strings. */
export type PageRef = Schemas['RowLocation'];
export type ReviewItemKind = ReviewItem['kind'];
/** `open` needs a person; `done` was decided; `rechecking` is waiting for the automatic re-check. */
export type ReviewStatus = ReviewItem['status'];

/**
 * "Compared with the architect": every architect number set beside the vendor's (direct compare).
 * `same`/`different` were compared by exact arithmetic; `rule` was left to a check (its id and
 * outcome); `unpaired` found no vendor number to set it beside; `unread` could not be read. `why`
 * says it in plain words. Older packages send no list, and the section is not shown.
 *
 * Written to match the backend contract until the generated `schema.d.ts` carries it; the page only
 * reads these fields, so the generated type can replace this one without a change elsewhere.
 */
export type ArchitectCompareResult = 'same' | 'different' | 'rule' | 'unpaired' | 'unread';
/** A spot on a stored page, in stored page space (0..1), as strings. */
export interface ArchitectCompareRef {
  document_version_id: string;
  page_number: number;
  polygon: string[][];
}
export interface ArchitectCompareEntry {
  /** `arch:<id>`. */
  key: string;
  page: number;
  view: string | null;
  architect_text: string;
  /** The architect's number exactly, `n/d` inches. */
  architect_inches: string;
  vendor_text: string | null;
  vendor_inches: string | null;
  result: ArchitectCompareResult;
  rule_id: string | null;
  rule_outcome: string | null;
  why: string;
  finding_id: string | null;
  architect_ref: ArchitectCompareRef | null;
  vendor_ref: ArchitectCompareRef | null;
  /** Like other review items: open needs a person; done was answered; rechecking waits. Absent on older answers. */
  status?: ReviewStatus;
  /** The W3 answers this entry offers (different: agree / not_this; unpaired, unread: checked_right / checked_wrong). */
  actions?: string[];
}
export type ArchitectCompareCounts = Record<ArchitectCompareResult, number>;

/** The list, empty when this package's answer has none (an older package). */
export function architectCompareOf(data: ReviewItemsOut): readonly ArchitectCompareEntry[] {
  const list = (data as { architect_compare?: readonly ArchitectCompareEntry[] | null }).architect_compare;
  return Array.isArray(list) ? list : [];
}

/** The header's counts, or null when the server sent none. */
export function architectCompareCountsOf(data: ReviewItemsOut): ArchitectCompareCounts | null {
  return (data.header as { architect_compare_counts?: ArchitectCompareCounts | null }).architect_compare_counts ?? null;
}

/**
 * "Compared with the architect", the AI page review: per combined sheet, the AI read the vendor's
 * numbers and set each architect number (read by code) beside the vendor's of the same span. Code
 * decides `same`/`different` with exact arithmetic; nothing here is ever a pass. Only an open
 * `different` row needs a person; the others are shown, not blocking. Absent on older packages.
 *
 * Hand-written to match the backend contract until the generated `schema.d.ts` carries `ai_review`;
 * the page only reads these fields, so the generated type can replace this one without a change elsewhere.
 */
export type AiReviewResult = 'same' | 'different' | 'not_shown' | 'unsure' | 'unreadable';
export type AiReviewPageStatus = 'done' | 'running' | 'failed' | 'not_run';
/** A spot on a stored page, in stored page space (0..1), as strings. */
export interface AiReviewRef {
  document_version_id: string;
  page_number: number;
  polygon: string[][];
}
export interface AiReviewRow {
  /** `air:<id>`. */
  key: string;
  architect_text: string;
  /** The architect's number exactly, `n/d` inches. */
  architect_inches: string | null;
  /** What the number measures, in words (e.g. "overall width"). */
  measures: string;
  vendor_text: string | null;
  vendor_inches: string | null;
  result: AiReviewResult;
  why: string;
  vendor_box: AiReviewRef | null;
  architect_ref: AiReviewRef | null;
  /** The W3 answers this row offers (different: agree / not_this). */
  actions: string[];
  status: 'open' | 'done';
  /** The answer given (`agree` / `not_this`) once done. */
  answer: string | null;
  answered_by: string | null;
}
export interface AiReviewPage {
  page: number;
  status: AiReviewPageStatus;
  cost_usd: string | null;
  rows: AiReviewRow[];
  /** Why a failed page could not be read, when the server says. */
  reason?: string | null;
}
export interface AiReviewCounts {
  same: number;
  different: number;
  not_shown: number;
  unsure: number;
  unreadable: number;
  needs_person: number;
}
export interface AiReviewOut {
  pages: AiReviewPage[];
  counts: AiReviewCounts;
}

/** The AI page review, or null when this package's answer has none (an older package, or switched off). */
export function aiReviewOf(data: ReviewItemsOut): AiReviewOut | null {
  const review = (data as { ai_review?: AiReviewOut | null }).ai_review;
  return review && Array.isArray(review.pages) ? review : null;
}

/**
 * Every answer the page can send. Only Fix carries typed text: the value the drawing shows, in
 * inches, recorded as a correction of the AI's reading (Anant, 2026-10-16). The API lists an
 * item's `actions` as plain strings; `isItemAction` narrows one to this list, and anything else is
 * ignored rather than shown as a button the page cannot name.
 */
export const ITEM_ACTIONS = [
  'confirm',
  'fix',
  'checked_right',
  'checked_wrong',
  'choose_wall',
  'confirm_no_countertop',
  'there_is_a_countertop',
  'answer',
  'not_this',
  'agree',
  'settle_group',
  'mark_cabinets',
  'use_cabinet_run',
  'job_values',
  'clear_not_compared',
] as const;
export type ItemAction = (typeof ITEM_ACTIONS)[number];

export function isItemAction(action: string): action is ItemAction {
  return (ITEM_ACTIONS as readonly string[]).includes(action);
}

/**
 * What one answer sends. `value` is the value a Fix records, in inches, as the reviewer typed it
 * (a correction of the AI's reading); `shown_value` is the number a Confirm was shown; `wall_config`
 * is the wall layout clicked; `expected_keys` are the open keys a group click was shown (#1358);
 * `architect_view` and `architect_pairing` are the bodies of the existing architect POSTs (#1368).
 */
export interface ActionBody {
  expected_finding_id: string | null;
  value?: string;
  shown_value?: string;
  wall_config?: string;
  expected_keys?: string[];
  /** A cabinet run's marks, one kind per cabinet shown, left to right (#648). */
  cabinet_kinds?: string[];
  /** A proposed cabinet run used: the run, what each piece is, the site width in inches. */
  cabinet_run?: CabinetRunAnswer;
  architect_view?: ArchitectViewAnswer;
  architect_pairing?: ArchitectPairingAnswer;
  /** This job's values, in inches, saved on the project and checked again (#1403). */
  job_values?: { name: string; value: string }[];
}

/** "This view shows the countertop", or "None of these", naming the record the reviewer was shown. */
export type ArchitectViewAnswer =
  | { view_id: string; none_of_these?: false; expected_record_id: string }
  | { view_id?: null; none_of_these: true; expected_record_id: string };

/** The pairing the reviewer accepted; `pairs: []` says nothing on the architect's drawing is comparable. */
export interface ArchitectPairingAnswer {
  pairs: { kind: 'overall' | 'piece'; architect_candidate_id: string; vendor_slot_indices: number[] }[];
  note?: string | null;
  expected_record_id?: string;
}

/** The answer-specific part of one click, added to `expected_finding_id`. */
export type ActionExtra = Omit<ActionBody, 'expected_finding_id'>;

/** W3's group keys: every open "Not checked" result, and every page listed with no countertop. */
export const NOT_CHECKED_GROUP = 'not-checked';
export const PAGES_GROUP = 'no-countertop-pages';
/**
 * "Clear all as not compared": every open architect number with no vendor match (or not read), in one
 * call naming exactly the keys shown (`expected_keys`). The group key and answer name are the one
 * place to change if the backend names them differently.
 */
export const NOT_COMPARED_GROUP = 'architect-not-compared';

/** The calls the page makes. The app uses `liveReviewItemsApi`; tests and the UI kit pass their own. */
export interface ReviewItemsApi {
  load: (projectId: string, packageId: string) => Promise<ReviewItemsOut>;
  act: (projectId: string, packageId: string, key: string, action: ItemAction, body: ActionBody) => Promise<void>;
}

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

async function failure(response: Response): Promise<ApiError> {
  let envelope: ErrorEnvelope;
  try {
    envelope = (await response.json()) as ErrorEnvelope;
  } catch {
    envelope = {
      error: 'unreadable_response',
      message: `The server returned ${response.status} and a body this page could not read.`,
      request_id: response.headers.get('x-request-id') ?? 'unknown',
    };
  }
  return new ApiError(response.status, envelope);
}

function packagePath(projectId: string, packageId: string): string {
  return `${BASE}/projects/${encodeURIComponent(projectId)}/packages/${encodeURIComponent(packageId)}`;
}

export const liveReviewItemsApi: ReviewItemsApi = {
  async load(projectId, packageId) {
    const response = await fetch(`${packagePath(projectId, packageId)}/review-items`, {
      headers: { Accept: 'application/json' },
    });
    if (!response.ok) throw await failure(response);
    return (await response.json()) as ReviewItemsOut;
  },
  // W3 (#1351): POST …/review-items/{key}/{action} with `expected_finding_id`. A 409 means the item
  // changed under the reviewer; a 501 means this kind of answer cannot be saved yet.
  async act(projectId, packageId, key, action, body) {
    const segment = W3_ACTION[action];
    const payload = segment === 'answer' ? { ...body, answer: action } : body;
    const response = await fetch(
      `${packagePath(projectId, packageId)}/review-items/${encodeURIComponent(key)}/${segment}`,
      {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    if (!response.ok) throw await failure(response);
  },
};

/**
 * W3's five URL actions (#1351): `confirm | fix | answer | agree | clear-not-checked`. Every other
 * one-click answer the W1 list offers goes to `answer`, with the answer's own name in the body. Kept
 * here, in one place, so a change in W3 changes only this table.
 */
const W3_ACTION: Record<ItemAction, 'confirm' | 'fix' | 'answer' | 'agree' | 'clear-not-checked'> = {
  confirm: 'confirm',
  fix: 'fix',
  agree: 'agree',
  settle_group: 'clear-not-checked',
  checked_right: 'answer',
  checked_wrong: 'answer',
  choose_wall: 'answer',
  confirm_no_countertop: 'answer',
  there_is_a_countertop: 'answer',
  answer: 'answer',
  not_this: 'answer',
  mark_cabinets: 'answer',
  use_cabinet_run: 'answer',
  job_values: 'answer',
  clear_not_compared: 'answer',
};

/** The URL of a stored picture path that the API hands out (`zoom_ref`). */
export function apiUrl(path: string): string {
  return `${BASE}${path.startsWith('/') ? path : `/${path}`}`;
}
