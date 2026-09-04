/**
 * The one place this app talks to the backend.
 *
 * Types come from `schema.d.ts`, which is **generated** from the API's own `/openapi.json` and is
 * never hand-edited. That is the point: if a field is renamed on the server, this app stops
 * compiling. A hand-written interface cannot do that — it goes on describing a response that no
 * longer exists, and the mismatch surfaces as a blank panel during a review.
 *
 * Regenerate with `npm run api:types`, and CI fails if the result differs from what is committed.
 */

import type { paths } from './schema';

/** Every failure the API produces has this shape — `app/errors.py`. */
export interface ErrorEnvelope {
  error: string;
  message: string;
  request_id: string;
}

/** Thrown for any non-2xx. Carries the request id, which is what a report quotes. */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;

  constructor(status: number, envelope: ErrorEnvelope) {
    super(envelope.message);
    this.name = 'ApiError';
    this.status = status;
    this.code = envelope.error;
    this.requestId = envelope.request_id;
  }
}

const BASE = (import.meta.env.VITE_API_BASE_URL ?? '/api/v1').replace(/\/$/, '');

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });

  if (!response.ok) {
    // The envelope is the contract, but a proxy or a crash can still return something else, and
    // guessing at that point produces a worse message than admitting we could not read it.
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The server returned ${response.status} and a body this client could not parse.`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }

  return (await response.json()) as T;
}

type Get<P extends keyof paths> = paths[P] extends { get: { responses: { 200: { content: { 'application/json': infer R } } } } }
  ? R
  : never;

// ---------------------------------------------------------------------------
// Resources
//
// One function per endpoint the review UI needs. Paths are written out rather than built by
// concatenation so that a typo is a compile error against `paths` rather than a 404 at runtime.
// ---------------------------------------------------------------------------

export type PackagePage = Get<'/api/v1/projects/{project_id}/packages'>;
export type PackageDetail = Get<'/api/v1/projects/{project_id}/packages/{package_id}'>;
export type FindingPage = Get<'/api/v1/projects/{project_id}/packages/{package_id}/findings'>;
export type FindingChain =
  Get<'/api/v1/projects/{project_id}/packages/{package_id}/findings/{finding_id}/chain'>;
export type FindingCounts =
  Get<'/api/v1/projects/{project_id}/packages/{package_id}/findings/summary'>;
export type ReviewSessionPage = Get<'/api/v1/projects/{project_id}/review-sessions'>;
export type RuleList = Get<'/api/v1/rules'>;
export type Rule = RuleList[number];
export type ReviewSession = ReviewSessionPage['items'][number];

export function listPackages(projectId: string, query?: { cursor?: string; limit?: number }) {
  const search = new URLSearchParams();
  if (query?.cursor) search.set('cursor', query.cursor);
  if (query?.limit) search.set('limit', String(query.limit));
  const suffix = search.toString() ? `?${search}` : '';
  return request<PackagePage>(`/projects/${projectId}/packages${suffix}`);
}

export function getPackage(projectId: string, packageId: string) {
  return request<PackageDetail>(`/projects/${projectId}/packages/${packageId}`);
}

export function listFindings(
  projectId: string,
  packageId: string,
  query?: { outcome?: string; severity?: string; cursor?: string; limit?: number },
) {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined) search.set(key, String(value));
  }
  const suffix = search.toString() ? `?${search}` : '';
  return request<FindingPage>(`/projects/${projectId}/packages/${packageId}/findings${suffix}`);
}

/** The arithmetic behind one verdict — every operand, its evidence status, and the comparison. */
export function getFindingChain(projectId: string, packageId: string, findingId: string) {
  return request<FindingChain>(
    `/projects/${projectId}/packages/${packageId}/findings/${findingId}/chain`,
  );
}

/**
 * How a package's findings break down, without fetching them.
 *
 * Every outcome is counted and they sum to the total — including the abstentions. Rendering only
 * passes and failures would invite a reader to treat the remainder as passing, and under V1's
 * exact-match rule the abstentions are the expected bulk of a run rather than an edge case.
 */
export function getFindingCounts(projectId: string, packageId: string) {
  return request<FindingCounts>(
    `/projects/${projectId}/packages/${packageId}/findings/summary`,
  );
}

/**
 * Every rule the engine would apply, as the engine sees them.
 *
 * Not scoped to a project: a rulebook is published centrally through D6 and the same snapshot
 * decides for every drawing. Nothing here is a project's own copy.
 *
 * An empty list is a real and expected answer. Until a rulebook is published there are no rules, and
 * a screen that filled that silence with examples would be describing checks that will not run.
 */
export function listRules() {
  return request<RuleList>('/rules');
}

async function send<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * Create a package in this project.
 *
 * The server opens revision 1 and its birth event in the same transaction, so a created package is
 * always something values can be entered against.
 */
export function createPackage(projectId: string, vendor: string | null) {
  type Body =
    paths['/api/v1/projects/{project_id}/packages']['post']['requestBody']['content']['application/json'];
  type Created =
    paths['/api/v1/projects/{project_id}/packages']['post']['responses']['201']['content']['application/json'];
  return send<Created>(`/projects/${projectId}/packages`, { vendor } satisfies Body);
}

/**
 * Store what the reviewer typed.
 *
 * **Values go as the strings the person typed** — `25 1/2"`, `984 mm` — and the server parses them
 * with the same code that reads a drawing. Nothing here converts a number: under exact match (Q2)
 * there is no tolerance band to absorb a rounding error, and JavaScript has no exact rational, so a
 * value this file touched arithmetically could already be a different verdict.
 *
 * A value with no unit comes back 422. That is deliberate on the server and worth surfacing rather
 * than smoothing over: `984` with no unit was once stored as 984 inches.
 */
export function enterMeasurements(
  projectId: string,
  packageId: string,
  entry: {
    parameters?: { name: string; value: string }[];
    measurements?: { rule_id: string; name: string; value: string }[];
  },
) {
  type Stored =
    paths['/api/v1/projects/{project_id}/packages/{package_id}/measurements']['post']['responses']['201']['content']['application/json'];
  return send<Stored>(`/projects/${projectId}/packages/${packageId}/measurements`, entry);
}

/**
 * Ask for the checks to be run.
 *
 * **Returns 202 and an `accepted_id`, not a run id.** Nothing has started when this resolves: the
 * server writes an outbox row, and something outside the API does the work. So a caller must not
 * treat the response as "the findings are ready" — it means "the request was recorded".
 */
export function requestChecks(projectId: string, packageId: string) {
  return send<{ accepted_id: string; package_revision_id: string }>(
    `/projects/${projectId}/packages/${packageId}/checks`,
    {},
  );
}

/**
 * The reviewer's own sittings in this project — what the sidebar lists.
 *
 * `mine` defaults to true on the server: a list showing everyone's would bury a reviewer's own on
 * any project with more than one of them.
 */
export function listReviewSessions(projectId: string, options?: { mine?: boolean }) {
  const search = options?.mine === false ? '?mine=false' : '';
  return request<ReviewSessionPage>(`/projects/${projectId}/review-sessions${search}`);
}

/**
 * Open a sitting over one package revision.
 *
 * **No reviewer is sent.** The server takes it from the authenticated caller, and the request model
 * forbids the field outright — a body-supplied name would let this client record somebody else's
 * decision, which is the one thing the audit trail exists to prevent.
 */
export function openReviewSession(projectId: string, packageId: string, revisionId: string) {
  return send<ReviewSession>(
    `/projects/${projectId}/packages/${packageId}/review-sessions`,
    { package_revision_id: revisionId },
  );
}

/** Record what the reviewer did to one finding. The actor is the caller; the revision is read off
 *  the finding server-side. Append-only — a changed mind is a second call, not an edit. */
export function recordReviewAction(
  projectId: string,
  reviewSessionId: string,
  action: { finding_id: string; action: 'confirm' | 'correct' | 'except' | 'dismiss'; note?: string },
) {
  return send<unknown>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/actions`,
    action,
  );
}

/** Close the sitting. Completing twice is refused rather than ignored. */
export function completeReviewSession(projectId: string, reviewSessionId: string) {
  return send<ReviewSession>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/complete`,
    {},
  );
}
