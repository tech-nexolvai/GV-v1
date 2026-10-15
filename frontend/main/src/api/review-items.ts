/**
 * The simple review page's data (#1343): what still needs a person on one drawing set, in one call.
 *
 * `GET /projects/{p}/packages/{k}/review-items` is the W1 read API (#1338). It is being built at the
 * same time as this page, so the types below are written by hand from its posted shape. **When W1
 * merges, regenerate `schema.d.ts` (`npm run api:types`) and replace these interfaces with
 * `components['schemas'][…]`** — the field names are the same, so the page does not change.
 *
 * The writes (Confirm, Fix, Agree, "Clear all as not checked") are the W3 action endpoints, which do
 * not exist yet. They all go through one function, `act`, so W3 only has to change that function.
 */

import { ApiError, type ErrorEnvelope } from './client';

export type ReviewItemKind =
  | 'number'
  | 'countertop_by_hand'
  | 'wall_choice'
  | 'no_countertop_page'
  | 'architect'
  | 'depth_reading'
  | 'other_result';

/** `open` needs a person; `done` was decided; `rechecking` is waiting for the automatic re-check. */
export type ReviewStatus = 'open' | 'done' | 'rechecking';

/** Every one-click answer the page can send. No action ever carries typed text. */
export type ItemAction =
  | 'confirm'
  | 'fix'
  | 'checked_right'
  | 'checked_wrong'
  | 'choose_wall'
  | 'confirm_no_countertop'
  | 'there_is_a_countertop'
  | 'answer'
  | 'not_this'
  | 'agree'
  | 'settle_group';

export interface ExactValue {
  numerator: string;
  denominator: string;
  display: string;
}

export interface BestGuess {
  text: string;
  value: ExactValue | null;
  /** Where the guess came from, most trusted first: the PDF's own text, both AIs, the AIs' suggestion. */
  source: 'pdf_text' | 'agreed_text' | 'suggestion';
}

export interface ReaderText {
  reader: string;
  text: string;
}

/** A spot on a stored page. `polygon` is in stored page space (0..1), as strings. */
export interface PageRef {
  page_id: string;
  document_version_id: string;
  page_number: number;
  coordinate_space: string;
  polygon: (readonly [string, string] | readonly string[])[];
}

export interface ArchitectRef {
  location: PageRef | null;
  /** Why there is no architect page to show, in plain words. */
  reason: string | null;
}

export interface CountertopRef {
  item_id: string;
  page_number: number;
  number: number;
  label: string;
}

export interface ReviewItem {
  key: string;
  kind: ReviewItemKind;
  page: number;
  countertop: CountertopRef | null;
  row_label: string | null;
  /** What is being asked, in plain words ("Overall countertop width"). */
  what: string;
  /** Null when the AI has no guess: the item is then checked by hand, never typed. */
  best_guess: BestGuess | null;
  readers: ReaderText[];
  why: string;
  vendor_ref: PageRef | null;
  architect_ref: ArchitectRef | null;
  /** A path under /api/v1 to the close-up PNG, when one is stored. */
  zoom_ref: string | null;
  /** Only while `status` is open; empty otherwise. */
  actions: ItemAction[];
  status: ReviewStatus;
  decided_by: string | null;
  decided_at: string | null;
  finding_id: string | null;
  row_id: string | null;
  candidate_id: string | null;
}

export interface ReviewFailure {
  key: string;
  finding_id: string;
  rule_id: string;
  page: number;
  countertop: CountertopRef | null;
  row_label: string | null;
  why: string;
  vendor_ref: PageRef | null;
  actions: ItemAction[];
  status: ReviewStatus;
  decided_by: string | null;
  decided_at: string | null;
}

export interface NotCheckedItem {
  key: string;
  finding_id: string;
  rule_id: string;
  /** The check that did not run, in plain words ("Countertop depth"). */
  check: string;
  page: number;
  countertop: CountertopRef | null;
  row_label: string | null;
  why: string;
  status: ReviewStatus;
  decided_by: string | null;
  decided_at: string | null;
}

/** A blocking finding or page the list forgot. Must never happen; the page shows a red banner if it does. */
export interface UnaccountedRecord {
  kind: 'finding' | 'page';
  [field: string]: unknown;
}

export interface ReviewItemsHeader {
  pages_read: number;
  numbers: number;
  confirmed_by_ai: number;
  checked_by_you: number;
  open: number;
  failures: number;
  not_checked: number;
  rechecking: number;
  can_finalise: boolean;
  blocked_reason: string | null;
}

export interface ReviewItemsOut {
  package_id: string;
  revision_id: string;
  revision_number: number;
  header: ReviewItemsHeader;
  items: ReviewItem[];
  failures: ReviewFailure[];
  not_checked_note: string | null;
  not_checked: NotCheckedItem[];
  unaccounted: UnaccountedRecord[];
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
  // W3 (planned): POST …/review-items/{key}/{action} with `expected_finding_id`; a 409 means the
  // item changed under the reviewer, and the page reloads it.
  async act(projectId, packageId, key, action, body) {
    const response = await fetch(
      `${packagePath(projectId, packageId)}/review-items/${encodeURIComponent(key)}/${action}`,
      {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      },
    );
    if (!response.ok) throw await failure(response);
  },
};

/** The URL of a stored picture path that the API hands out (`zoom_ref`). */
export function apiUrl(path: string): string {
  return `${BASE}${path.startsWith('/') ? path : `/${path}`}`;
}
