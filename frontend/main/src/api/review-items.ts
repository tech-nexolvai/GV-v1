/**
 * The simple review page's data (#1343): what still needs a person on one drawing set, in one call.
 *
 * `GET /projects/{p}/packages/{k}/review-items` is the W1 read API (#1338). Its types come from the
 * generated `schema.d.ts`, so a renamed field stops this page compiling.
 *
 * The writes (Confirm, Fix, Agree, "Clear all as not checked") are the W3 action endpoints, which do
 * not exist yet. They all go through one function, `act`, so W3 only has to change that function.
 */

import { ApiError, type ErrorEnvelope } from './client';
import type { components } from './schema';

type Schemas = components['schemas'];

export type ReviewItemsOut = Schemas['ReviewItemsOut'];
export type ReviewItemsHeader = Schemas['ReviewHeaderOut'];
export type ReviewItem = Schemas['ReviewItemOut'];
export type ReviewFailure = Schemas['FailureOut'];
export type NotCheckedItem = Schemas['NotCheckedOut'];
export type UnaccountedRecord = Schemas['UnaccountedOut'];
export type BestGuess = Schemas['BestGuessOut'];
export type ReaderText = Schemas['ReaderTextOut'];
export type CountertopRef = Schemas['CountertopItemRefOut'];
export type ArchitectRef = Schemas['ArchitectRefOut'];
/** A spot on a stored page. `polygon` is in stored page space (0..1), as strings. */
export type PageRef = Schemas['RowLocation'];
export type ReviewItemKind = ReviewItem['kind'];
/** `open` needs a person; `done` was decided; `rechecking` is waiting for the automatic re-check. */
export type ReviewStatus = ReviewItem['status'];

/**
 * Every one-click answer the page can send. No action ever carries typed text. The API lists an
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
] as const;
export type ItemAction = (typeof ITEM_ACTIONS)[number];

export function isItemAction(action: string): action is ItemAction {
  return (ITEM_ACTIONS as readonly string[]).includes(action);
}

/** What one click sends. `value` is a reading the reviewer picked from the AIs' own readings. */
export interface ActionBody {
  expected_finding_id: string | null;
  value?: string;
}

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
};

/** The URL of a stored picture path that the API hands out (`zoom_ref`). */
export function apiUrl(path: string): string {
  return `${BASE}${path.startsWith('/') ? path : `/${path}`}`;
}
