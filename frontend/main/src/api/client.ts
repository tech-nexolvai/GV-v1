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

import type { components, paths } from './schema';

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

/**
 * The revision each open review screen shows (#1180). Every write to that package carries it in
 * `X-GV-Revision`, so a screen left open on revision 1 after someone started revision 2 is told
 * "you are looking at revision 1, which is read-only" instead of writing onto revision 2.
 */
const screenRevisions = new Map<string, number>();

function revisionHeader(path: string, method: string | undefined): Record<string, string> {
  if (!method || method.toUpperCase() === 'GET') return {};
  const match = /\/packages\/([^/?]+)/.exec(path);
  const shown = match ? screenRevisions.get(match[1]) : undefined;
  return shown === undefined ? {} : { 'X-GV-Revision': String(shown) };
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BASE}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...revisionHeader(path, init?.method), ...(init?.headers ?? {}) },
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
export type RuleList = Get<'/api/v1/rules'>;
export type Rule = RuleList[number];
/** The review session an upload opens (the only review-session route left, since 2026-10-28). */
export type ReviewSession =
  paths['/api/v1/projects/{project_id}/packages/{package_id}/review-sessions']['post']['responses'][201]['content']['application/json'];

export function listPackages(projectId: string, query?: { cursor?: string; limit?: number }) {
  const search = new URLSearchParams();
  if (query?.cursor) search.set('cursor', query.cursor);
  if (query?.limit) search.set('limit', String(query.limit));
  const suffix = search.toString() ? `?${search}` : '';
  return request<PackagePage>(`/projects/${projectId}/packages${suffix}`);
}

/** One page of the Documents table (#1035): every column in one request, newest first. */
export type PackageSummaryPage = Get<'/api/v1/projects/{project_id}/packages-summary'>;
export type PackageSummary = PackageSummaryPage['items'][number];

/** The largest page the API serves (`MAX_PAGE_SIZE`); the table sorts and searches within a page. */
export const PACKAGES_SUMMARY_PAGE_SIZE = 200;

export function getPackagesSummary(projectId: string, query?: { cursor?: string; limit?: number }) {
  const search = new URLSearchParams();
  if (query?.cursor) search.set('cursor', query.cursor);
  if (query?.limit) search.set('limit', String(query.limit));
  const suffix = search.toString() ? `?${search}` : '';
  return request<PackageSummaryPage>(`/projects/${projectId}/packages-summary${suffix}`);
}

/**
 * AI model-call usage for a project (#1035): totals, then per day or per drawing set, each split by
 * model. No prompt or response text. `cost_usd` is an exact decimal string.
 */
export type Usage = Get<'/api/v1/projects/{project_id}/usage'>;
export type UsageGroup = Usage['groups'][number];

export function getUsage(projectId: string, query: { groupBy: 'day' | 'package' }) {
  return request<Usage>(`/projects/${projectId}/usage?group_by=${query.groupBy}`);
}

/** All AI spending so far (#1165): this project's calls plus earlier runs, by model and run. */
export type UsageHistory = Get<'/api/v1/projects/{project_id}/usage/history'>;
export type ModelSpend = UsageHistory['by_model'][number];
export type EarlierRun = UsageHistory['earlier_runs'][number];
export type SpendTotals = UsageHistory['totals'];
export function getUsageHistory(projectId: string) {
  return request<UsageHistory>(`/projects/${projectId}/usage/history`);
}

/** What the AI provider reports this deployment's key has used (all projects); empty when off. */
export type UsageProviderChecks = Get<'/api/v1/projects/{project_id}/usage/provider-check'>;
export type UsageProviderCheck = UsageProviderChecks['checks'][number];
export function getUsageProviderCheck(projectId: string) {
  return request<UsageProviderChecks>(`/projects/${projectId}/usage/provider-check`);
}

/** The job tracker (#1578): every item found across the project's sets, rounds and checks. */
export type Tracker = Get<'/api/v1/projects/{project_id}/tracker'>;
export type TrackerItem = Tracker['items'][number];
export type TrackerSet = Tracker['sets'][number];
export type TrackerVendor = Tracker['vendors'][number];
export function getTracker(projectId: string) {
  return request<Tracker>(`/projects/${projectId}/tracker`);
}

/** The tracker's workbook (CSV where the server has no spreadsheet library), filtered like the screen. */
export function trackerExportUrl(projectId: string, filters: { state?: string; packageId?: string | null; vendor?: string | null } = {}): string {
  const query = new URLSearchParams();
  if (filters.state && filters.state !== 'all') query.set('state', filters.state);
  if (filters.packageId) query.set('package_id', filters.packageId);
  if (filters.vendor) query.set('vendor', filters.vendor);
  const search = query.toString();
  return `${BASE}/projects/${projectId}/tracker.xlsx${search ? `?${search}` : ''}`;
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
export function createPackage(
  projectId: string,
  vendor: string | null,
  productType: string,
) {
  type Body =
    paths['/api/v1/projects/{project_id}/packages']['post']['requestBody']['content']['application/json'];
  type Created =
    paths['/api/v1/projects/{project_id}/packages']['post']['responses']['201']['content']['application/json'];
  return send<Created>(`/projects/${projectId}/packages`, {
    vendor,
    product_type: productType,
  } satisfies Body);
}

/**
 * The products a drawing set may be for (#994): each product the published rulebook has at least
 * one check for, with the words to show. The upload screen's dropdown is filled from this so it
 * never offers a product nothing would check. Empty until a rulebook is published.
 */
/**
 * Every product GV Review knows (#1547): the product registry, in its order, with whether each can be
 * picked now (inactive ones are shown greyed, "coming later") and how many published checks it has.
 * The chat's chips, "+" menu and findings' groups and the upload picker are built from this.
 */
export function listProducts() {
  type Products = paths['/api/v1/products']['get']['responses']['200']['content']['application/json'];
  return request<Products>('/products');
}

export function listProductTypes() {
  type Choices =
    paths['/api/v1/product-types']['get']['responses']['200']['content']['application/json'];
  return request<Choices>('/product-types');
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

// ── The architect's view a countertop is compared with (#1168, separate architect PDF) ──

/** GV's standard numbers, which every project starts from (#812). */
export function getCompanySettings() {
  return request<CompanySettings>('/company-settings');
}

/**
 * Company settings → Projects (#1341): each of your projects' own values, what each is for, the value
 * in use and where it comes from, and the project's sink card. Only projects you belong to.
 */
export type ProjectsSettings = Get<'/api/v1/company-settings/projects'>;
export type ProjectSettings = ProjectsSettings['projects'][number];
export type ProjectSetting = ProjectSettings['settings'][number];
export type ProjectValueIn = components['schemas']['ProjectValueIn'];

export function getProjectSettings() {
  return request<ProjectsSettings>('/company-settings/projects');
}

/**
 * Record a project's own values; every one not sent stays as it was. Reviewers and admins in the
 * project may save; anyone else gets the same "Not found" every authorisation failure gives.
 */
export function saveProjectSettings(projectId: string, values: ProjectValueIn[]) {
  return send<ProjectSettings>(`/company-settings/projects/${projectId}`, { values });
}

/** The sink this project's sink checks use (#1181); `sink` is null until one is entered. */
export type ProjectSink = Get<'/api/v1/projects/{project_id}/sink'>;
export type SinkEntry = components['schemas']['SinkEntryIn'];

/**
 * Enter the project's sink: its name and inside width and depth. Sizes go as typed, with their unit;
 * a bare number or a zero comes back 422. Every sink check of every package in the job reads it once
 * the checks run again.
 */
export function saveProjectSink(projectId: string, entry: SinkEntry) {
  return send<ProjectSink>(`/projects/${projectId}/sink`, entry);
}

/**
 * Record GV's standard numbers. Only the values sent change; every one not sent stays as it was.
 * Refused for anyone but an admin, with the same "Not found" every authorisation failure gives.
 */
export function saveCompanySettings(values: { name: string; value: string }[]) {
  return request<CompanySettings>('/company-settings', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ values }),
  });
}
export type CompanySettings = Get<'/api/v1/company-settings'>;

