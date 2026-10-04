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
import { parseSseFrames } from './sse.js';
import type { ChatStreamFacts, ChatStreamStage, ReviewerChatReply } from './chatStreamTypes';

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

/** A 201 body, for the routes that create something. Same idea as `Get`, one status code along. */
type Created<P extends keyof paths> = paths[P] extends { post: { responses: { 201: { content: { 'application/json': infer R } } } } }
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
export type ChangedValues =
  Get<'/api/v1/projects/{project_id}/packages/{package_id}/changed-values'>;
export type ReviewSessionPage = Get<'/api/v1/projects/{project_id}/review-sessions'>;
export type RuleList = Get<'/api/v1/rules'>;
export type Rule = RuleList[number];
export type ReviewSession = ReviewSessionPage['items'][number];
export type DecidedEvidence =
  Created<'/api/v1/projects/{project_id}/review-sessions/{review_session_id}/evidence'>;
export type GrantedException =
  Created<'/api/v1/projects/{project_id}/review-sessions/{review_session_id}/exceptions'>;
export type { ChatStreamFacts, ChatStreamStage, ReviewerChatReply } from './chatStreamTypes';
export type FillerDistributionRequest =
  paths['/api/v1/projects/{project_id}/filler-distribution']['post']['requestBody']['content']['application/json'];
export type FillerDistributionResponse =
  paths['/api/v1/projects/{project_id}/filler-distribution']['post']['responses'][200]['content']['application/json'];

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

export function getChangedValues(projectId: string, packageId: string) {
  return request<ChangedValues>(
    `/projects/${projectId}/packages/${packageId}/changed-values`,
  );
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
 * Ask about the currently live deterministic run. The backend owns the finding scope and accepts
 * only a question; it never accepts client-supplied values, finding ids, or verdicts.
 */
export function askReviewerChat(
  projectId: string,
  packageId: string,
  question: string,
  modelId?: string | null,
) {
  type Body =
    paths['/api/v1/projects/{project_id}/packages/{package_id}/chat']['post']['requestBody']['content']['application/json'];
  // model_id is an optional presentation choice; the backend refuses any id not on its allow-list.
  // Omit it entirely (rather than sending null) when no model is picked, so the default is used.
  const body: Body = modelId ? { question, model_id: modelId } : { question };
  return send<ReviewerChatReply>(`/projects/${projectId}/packages/${packageId}/chat`, body);
}

export interface ChatStreamHandlers {
  onFacts: (facts: ChatStreamFacts) => void;
  /** Real progress: sent only when a provider is actually being called. */
  onStage?: (stage: ChatStreamStage) => void;
}

/**
 * Ask the reviewer chat, receiving the answer in the order it becomes known.
 *
 * `facts` arrives at once (it needs no model), so the findings table is on screen while the model
 * is still writing. The `narration` frame is the complete reply, **already accepted by the
 * narration guard**: the model's words are never shown in pieces, because the guard accepts or
 * rejects the whole batch and a piece could be a sentence it then discards.
 *
 * Resolves with the same body `/chat` returns. A stream that ends without a `narration` frame, or
 * that sends `error`, rejects: a truncated answer must not read as an answer.
 */
export async function streamReviewerChat(
  projectId: string,
  packageId: string,
  question: string,
  handlers: ChatStreamHandlers,
  modelId?: string | null,
  signal?: AbortSignal,
): Promise<ReviewerChatReply> {
  const body = modelId ? { question, model_id: modelId } : { question };
  const response = await fetch(`${BASE}/projects/${projectId}/packages/${packageId}/chat/stream`, {
    method: 'POST',
    headers: { Accept: 'text/event-stream', 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  });

  if (!response.ok || !response.body) {
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

  const requestId = response.headers.get('x-request-id') ?? 'unknown';
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  let reply: ReviewerChatReply | null = null;

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    const parsed = parseSseFrames(buffered + decoder.decode(value, { stream: true }));
    buffered = parsed.rest;
    for (const frame of parsed.frames) {
      if (frame.event === 'facts') handlers.onFacts(frame.data as ChatStreamFacts);
      else if (frame.event === 'stage') {
        handlers.onStage?.(frame.data as ChatStreamStage);
      } else if (frame.event === 'narration') reply = frame.data as ReviewerChatReply;
      else if (frame.event === 'error') {
        // The server never puts exception text in this frame; its detail is safe to show.
        throw new ApiError(502, {
          error: 'chat_stream_error',
          message: (frame.data as { detail?: string }).detail ?? 'The explanation could not be produced.',
          request_id: requestId,
        });
      }
    }
  }

  if (!reply) {
    throw new ApiError(502, {
      error: 'incomplete_stream',
      message: 'The answer ended before its explanation arrived. The findings shown are the recorded ones.',
      request_id: requestId,
    });
  }
  return reply;
}

export type ChatModels =
  paths['/api/v1/projects/{project_id}/packages/{package_id}/chat/models']['get']['responses'][200]['content']['application/json'];

/** The narration models a reviewer may pick from, and which one answers by default. */
export function getChatModels(projectId: string, packageId: string) {
  return request<ChatModels>(`/projects/${projectId}/packages/${packageId}/chat/models`);
}

/**
 * Ask the deterministic distribution endpoint for the filler-first proposal.
 *
 * The caller supplies the reviewer-entered site width and the reviewer-selected adjustable cabinet.
 * This helper does not derive dimensions or pick a cabinet; it only carries the explicit payload to
 * the backend operation that owns the arithmetic.
 */
export function calculateFillerDistribution(
  projectId: string,
  payload: FillerDistributionRequest,
) {
  return send<FillerDistributionResponse>(
    `/projects/${projectId}/filler-distribution`,
    payload,
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
 * What the published rulebook needs before it can decide anything.
 *
 * **The form is built from this rather than from a list in this repository.** A hand-written list of
 * fields is right the day it is written and silently wrong the first time a rule gains an input — and
 * the check then abstains for a reason the reviewer cannot act on, which looks exactly like a genuine
 * missing dimension.
 *
 * Quantities are grouped by the physical measurement rather than by rule input, so a reviewer reads
 * the sink's front offset off the drawing once even though three rules consume it. `consumers` says
 * which inputs each value feeds.
 */
export function getRequiredInputs(projectId: string, packageId: string) {
  type Needed =
    paths['/api/v1/projects/{project_id}/packages/{package_id}/required-inputs']['get']['responses']['200']['content']['application/json'];
  return request<Needed>(`/projects/${projectId}/packages/${packageId}/required-inputs`);
}

/** One frame of the assignment stream. The server's own type — see `AssignmentEvent`. */
export type AssignmentEvent = components['schemas']['AssignmentEvent'];
/** One phase of the assignment, as it begins. */
export type AssignmentStep = components['schemas']['AssignmentStepOut'];
/** The accepted proposal, and the counts that say how much of the form it fills. */
export type ProposedMeasurements = components['schemas']['ProposedMeasurementsOut'];
/** One field and the readings proposed to fill it, in drawing order. */
export type ProposedField = components['schemas']['ProposedFieldOut'];

/**
 * Ask which reading fills which field, and report each phase as the server reaches it.
 *
 * **The only streaming call in this client, and it streams for one reason.** The model call is the
 * slow part of the request, and the alternative to real phase frames is a progress display moving
 * on a timer — which asserts a position nothing measured. Every phase this shows, and the
 * percentage on it, arrives from the server having actually happened.
 *
 * `fetch` with a body reader rather than `EventSource`, which can only issue a GET and cannot send
 * a header. The frames are plain SSE: one `data:` line each, blank line between.
 *
 * Resolves with the result frame. A stream that ends without one is an error — a truncated response
 * would otherwise read as "the model filled nothing", which is a different and reassuring answer.
 */
export async function proposeMeasurements(
  projectId: string,
  packageId: string,
  onStep: (step: AssignmentStep) => void,
  signal?: AbortSignal,
): Promise<ProposedMeasurements> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/measurements/propose`,
    { method: 'POST', headers: { Accept: 'text/event-stream' }, signal },
  );

  if (!response.ok || !response.body) {
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

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  let result: ProposedMeasurements | null = null;

  // Frames are separated by a blank line and a frame can arrive split across two network chunks, so
  // the tail of the buffer is kept rather than parsed. Parsing what has arrived so far would throw
  // on a half-written frame roughly whenever a proposal is large enough to matter.
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffered += decoder.decode(value, { stream: true });
    const frames = buffered.split('\n\n');
    buffered = frames.pop() ?? '';
    for (const frame of frames) {
      const line = frame.split('\n').find((part) => part.startsWith('data:'));
      if (!line) continue;
      const event = JSON.parse(line.slice('data:'.length).trim()) as AssignmentEvent;
      if (event.event === 'step' && event.step) onStep(event.step);
      if (event.event === 'result' && event.result) result = event.result;
    }
  }

  if (!result) {
    throw new ApiError(502, {
      error: 'incomplete_stream',
      message:
        'The proposal ended before it said what it had filled. Nothing was changed; enter the ' +
        'values yourself, or try again.',
      request_id: response.headers.get('x-request-id') ?? 'unknown',
    });
  }
  return result;
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
    parameters?: {
      name: string;
      value: string;
      scope?: 'project' | 'run';
      /** Where it came from (#827): one of the setting's `sources` from required-inputs. */
      source?: string;
      /** Where in that source, in the reviewer's words. */
      reference?: string;
      /** The passage it was typed from (#866): the `proposal_id` of the setting's `found` pointer.
       * The server refuses a number that differs from the passage's, and nothing is saved. */
      citation?: string;
    }[];
    measurements?: {
      rule_id: string;
      name: string;
      /** A single measurement. Exactly one of `value` or `values`, never both. */
      value?: string;
      /** A many-valued input, in layout order — the order is compared position by position. */
      values?: string[];
    }[];
    /** Inputs answered by choosing rather than measuring (#684).
     *
     * Separate from `measurements` because a category has no unit: sending `single_door` as a
     * measurement puts it through the imperial parser, which refuses it. */
    classifications?: {
      rule_id: string;
      name: string;
      /** One category per item, in layout order — which cabinet is the equipment cabinet is the
       * whole question, so the order is the answer. */
      categories: string[];
    }[];
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
export function requestChecks(
  projectId: string,
  packageId: string,
  discriminators: Record<string, string> = {},
) {
  return send<{ accepted_id: string; package_revision_id: string }>(
    `/projects/${projectId}/packages/${packageId}/checks`,
    { discriminators },
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

/**
 * Confirm or correct one reading behind a finding.
 *
 * **This is the only way into the correction ledger.** `recordReviewAction` takes a kind and a note
 * and has nowhere for a value, so sending `correct` there recorded that something was corrected
 * without saying to what — and the ledger stayed empty, which made the reviewer correction rate read
 * as "no corrections were needed".
 *
 * The value goes up **as typed**, with its unit. The server parses it, so `25.5"` and `25 1/2"` are
 * the same correction and a reviewer does not have to know which spelling this accepts. A bare
 * number is refused, deliberately: one whose `mm` had been lost was once stored as inches.
 */
export function decideEvidence(
  projectId: string,
  reviewSessionId: string,
  decision: {
    finding_id: string;
    observation_id: string;
    action: 'confirm' | 'correct';
    corrected_value?: string;
  },
) {
  return send<DecidedEvidence>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/evidence`,
    decision,
  );
}

/**
 * Accept one specific deviation, until a date.
 *
 * Every field is required and the expiry most of all: a permanent silent exception is not
 * representable anywhere in this system, because the date is what forces somebody to look again.
 * There is no `approved_by` — the approver is whoever is signed in, and a client-supplied one would
 * answer "who says so?" with "whoever was asked".
 */
export function grantException(
  projectId: string,
  reviewSessionId: string,
  grant: {
    finding_id: string;
    scope: 'finding' | 'item' | 'package';
    scope_id: string;
    reason: string;
    expires_at: string;
  },
) {
  return send<GrantedException>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/exceptions`,
    grant,
  );
}

/** Close the sitting. Completing twice is refused rather than ignored. */
export function completeReviewSession(projectId: string, reviewSessionId: string) {
  return send<ReviewSession>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/complete`,
    {},
  );
}

/**
 * What the extractor read for this package, waiting for somebody to say what it is.
 *
 * Only readings that carry a value: a token with no unit was recorded without one, deliberately, and
 * there is nothing for a reviewer to confirm about a bare number.
 */
export function listCandidates(projectId: string, packageId: string) {
  return request<CandidatesOut>(
    `/projects/${projectId}/packages/${packageId}/candidates`,
  );
}

/** The vocabulary a reviewer may choose from, read from the rulebook rather than hard-coded here. */
export function listSemanticTypes() {
  return request<string[]>('/semantic-types');
}

/**
 * Say what one extracted reading is.
 *
 * **No value is sent.** The reviewer is confirming the number the extractor read, not supplying one;
 * a body carrying a value would be the old form path wearing a new name.
 */
export function confirmCandidate(
  projectId: string,
  packageId: string,
  candidateId: string,
  semanticType: string,
) {
  return request<ConfirmedOut>(
    `/projects/${projectId}/packages/${packageId}/candidates/${candidateId}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ semantic_type: semanticType }),
    },
  );
}

/** The drawings found on this package's sheets, and what each one is suggested to be (#710). */
export function listDrawingViews(projectId: string, packageId: string) {
  return request<DrawingViewsOut>(`/projects/${projectId}/packages/${packageId}/views`);
}

/**
 * Say which drawing one is: the architect's or the vendor's (#795).
 *
 * The answer decides which side of every comparison the readings inside it are on, so it is only
 * ever sent by a reviewer's click. The sheet's own label is shown as a suggestion and never sent.
 */
export function confirmDrawingRole(
  projectId: string,
  packageId: string,
  viewId: string,
  role: 'arch' | 'shop',
) {
  return request<DrawingViewOut>(
    `/projects/${projectId}/packages/${packageId}/views/${viewId}/role`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ role }),
    },
  );
}

/** The parts suggested in each drawing of this package, left to right, and each decision (#882). */
export function listDrawingParts(projectId: string, packageId: string) {
  return request<DrawingPartsOut>(`/projects/${projectId}/packages/${packageId}/parts`);
}

/**
 * Say what one suggested part is (#882). The only way a part is made, one at a time: there is
 * deliberately no call that decides more than one suggestion.
 *
 * `code` is sent exactly as the person typed it, or null when none is printed; the server keeps it
 * verbatim.
 */
export function confirmDrawingPart(
  projectId: string,
  packageId: string,
  proposalId: string,
  kind: PartKind,
  code: string | null,
) {
  return request<DrawingPartOut>(
    `/projects/${projectId}/packages/${packageId}/parts/${proposalId}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind, code }),
    },
  );
}

/** Say one suggestion is not a part (#882). */
export function withdrawDrawingPart(projectId: string, packageId: string, proposalId: string) {
  return request<DrawingPartOut>(
    `/projects/${projectId}/packages/${packageId}/parts/${proposalId}/withdraw`,
    { method: 'POST' },
  );
}

/**
 * Add a part the suggestions missed, by its two ends on the drawing (#882). The ends are stored
 * page points as exact text, such as a listed part's `left_end`, sent back unchanged.
 */
export function addDrawingPart(
  projectId: string,
  packageId: string,
  viewId: string,
  part: { kind: PartKind; code: string | null; ends: [PartPoint, PartPoint] },
) {
  return request<DrawingPartOut>(
    `/projects/${projectId}/packages/${packageId}/views/${viewId}/parts`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(part),
    },
  );
}

/**
 * The picture of one suggested part (#897): the vendor's drawing round its outline, cut by the
 * worker. Checked against its recorded digest by the server before it is sent. For a person's eyes
 * only: nothing reads a value from it.
 */
export async function downloadPartPicture(
  projectId: string,
  packageId: string,
  proposalId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/parts/${proposalId}/picture`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The part's picture could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/**
 * The crop of the reading one suggested part's code came from (#882), so a person can check the
 * code itself. Checked against its recorded digest by the server before it is sent.
 */
export async function downloadPartCrop(
  projectId: string,
  packageId: string,
  proposalId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/parts/${proposalId}/crop`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The crop of the part's code could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/**
 * Each confirmed countertop of this package, the run of parts the computer suggests beneath it, and
 * what a person decided (#893). Listing writes nothing: a suggested run is worked out on each call.
 */
export function listCountertopRuns(projectId: string, packageId: string) {
  return request<CountertopRunsOut>(`/projects/${projectId}/packages/${packageId}/countertop-runs`);
}

/**
 * Say which parts make up the run beneath one countertop (#893): the suggestion as it stands, or a
 * person's correction. The order the ids are sent in does not matter: the server orders the run
 * across the drawing. There is deliberately no call that decides more than one countertop's run.
 */
export function confirmCountertopRun(
  projectId: string,
  packageId: string,
  countertopItemId: string,
  partIds: string[],
  wallConfig: string,
) {
  return request<CountertopRunOut>(
    `/projects/${projectId}/packages/${packageId}/countertop-runs/${countertopItemId}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ part_ids: partIds, wall_config: wallConfig }),
    },
  );
}

/** Say the run suggested or confirmed beneath one countertop is wrong (#893). Writes no run. */
export function withdrawCountertopRun(projectId: string, packageId: string, countertopItemId: string) {
  return request<CountertopRunOut>(
    `/projects/${projectId}/packages/${packageId}/countertop-runs/${countertopItemId}/withdraw`,
    { method: 'POST' },
  );
}

/**
 * Each confirmed part of this package, the confirmed reading the computer suggests is its width,
 * the readings a person may pick instead, and what a person decided (#913). Listing writes nothing:
 * a suggestion is worked out on each call.
 */
export function listReadingParts(projectId: string, packageId: string) {
  return request<ReadingPartsOut>(`/projects/${projectId}/packages/${packageId}/reading-parts`);
}

/**
 * Say which confirmed reading is one part's width (#913): the suggestion, or another reading on the
 * same drawing as a correction. Any other reading linked to the part is taken back by the server.
 * There is deliberately no call that decides more than one part's link.
 */
export function confirmReadingPart(
  projectId: string,
  packageId: string,
  itemId: string,
  readingId: string,
) {
  return request<ReadingPartOut>(
    `/projects/${projectId}/packages/${packageId}/reading-parts/${itemId}/confirm`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reading_id: readingId }),
    },
  );
}

/** Take back the link between one part and its reading (#913). Links nothing. */
export function withdrawReadingPart(projectId: string, packageId: string, itemId: string) {
  return request<ReadingPartOut>(
    `/projects/${projectId}/packages/${packageId}/reading-parts/${itemId}/withdraw`,
    { method: 'POST' },
  );
}

/** GV's standard numbers, which every project starts from (#812). */
export function getCompanySettings() {
  return request<CompanySettings>('/company-settings');
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

/**
 * The shapes above, taken from the generated schema rather than written out here.
 *
 * A hand-written copy is right the day it is written and silently wrong the first time the endpoint
 * changes — and the compiler would go on agreeing with it. `npm run api:check` regenerates these from
 * the running app's OpenAPI and fails if the checked-in file has drifted, which is what caught the
 * first version of this file.
 */
export type CandidatesOut = Get<'/api/v1/projects/{project_id}/packages/{package_id}/candidates'>;
// The generated OpenAPI type is authoritative for core fields. A locally-safe extension keeps older
// clients working if the backend adds the new `source` field after a schema refresh.
export type CandidateOut = CandidatesOut['candidates'][number] & {
  source?: string | null;
};
export type DrawingViewsOut = Get<'/api/v1/projects/{project_id}/packages/{package_id}/views'>;
export type DrawingViewOut = DrawingViewsOut['views'][number];
export type DrawingPartsOut = Get<'/api/v1/projects/{project_id}/packages/{package_id}/parts'>;
export type DrawingPartOut = DrawingPartsOut['drawings'][number]['parts'][number];
export type PartKind = components['schemas']['PartKind'];
export type CountertopRunsOut = Get<'/api/v1/projects/{project_id}/packages/{package_id}/countertop-runs'>;
export type CountertopRunOut = CountertopRunsOut['drawings'][number]['countertops'][number];
export type ReadingPartsOut = Get<'/api/v1/projects/{project_id}/packages/{package_id}/reading-parts'>;
export type ReadingPartOut = ReadingPartsOut['drawings'][number]['parts'][number];
export type PartPoint = components['schemas']['PointOut'];
export type CompanySettings = Get<'/api/v1/company-settings'>;
export type ConfirmedOut =
  paths['/api/v1/projects/{project_id}/packages/{package_id}/candidates/{candidate_id}/confirm']['post']['responses'][201]['content']['application/json'];

/**
 * Sign a package off: approve every finding of the revision this sitting reviewed.
 *
 * The finding set is chosen by the server. A client naming what it approves could approve a subset
 * and leave the rest looking reviewed, and this is the record GV stands behind when a vendor
 * disputes a dimension.
 *
 * Refused while any REVIEW REQUIRED finding is unaddressed — an abstention nobody acted on is a
 * check that did not happen.
 */
export function approvePackage(projectId: string, reviewSessionId: string) {
  return request<ApprovalOut>(
    `/projects/${projectId}/review-sessions/${reviewSessionId}/approve`,
    { method: 'POST' },
  );
}

/**
 * The signed-off review, as workbook bytes.
 *
 * Not `request`, which parses JSON: this is a file. The blob is handed to the browser by the caller
 * rather than fetched into a link here, so a failure is an error the page can show instead of a
 * download that silently does nothing.
 */
export async function downloadReport(
  projectId: string,
  packageId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/report`,
  );
  if (!response.ok) {
    // The same envelope every other route returns, and the same fallback when a proxy or a crash
    // sends something else — see `request`.
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The report could not be downloaded (HTTP ${response.status}).`,
        request_id: '',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/** The branded PDF counterpart to the workbook, generated from the same stored findings. */
export async function downloadPdfReport(
  projectId: string,
  packageId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/report.pdf`,
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The PDF could not be downloaded (HTTP ${response.status}).`,
        request_id: '',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/** The evidence-grounded drawing redline, available only after sign-off. */
export async function downloadRedline(
  projectId: string,
  packageId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/redline.pdf`,
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The evidence-grounded redline could not be downloaded (HTTP ${response.status}).`,
        request_id: '',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/**
 * The immutable crop mechanically cut around a confirmed reading.
 *
 * This is deliberately a blob rather than a URL stored on a finding. The server re-establishes the
 * project boundary and verifies the crop's recorded digest immediately before returning it; a crop
 * is evidence, not a decorative preview that may be cached or guessed from a drawing coordinate.
 */
export async function downloadEvidenceCrop(
  projectId: string,
  packageId: string,
  canonicalObservationId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/evidence/${canonicalObservationId}/crop`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The evidence crop could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/** The immutable crop behind an untyped AI proposal, before a reviewer names its meaning. */
export async function downloadCandidateCrop(
  projectId: string,
  packageId: string,
  candidateId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/candidates/${candidateId}/crop`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The proposal crop could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/**
 * The crop of the passage where the architect's drawing states a setting (#866).
 *
 * Pixels only: the reviewer reads the number off this picture and types it, and the app's own
 * reading of it is never sent.
 */
export async function downloadSettingPassageCrop(
  projectId: string,
  packageId: string,
  proposalId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/parameter-proposals/${proposalId}/crop`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The passage crop could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

/** The immutable crop behind a proposed closed layout answer. */
export async function downloadLayoutProposalCrop(
  projectId: string,
  packageId: string,
  cropArtifactId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/layout-proposals/${cropArtifactId}/crop`,
    { headers: { Accept: 'image/png,image/*;q=0.8' } },
  );
  if (!response.ok) {
    let envelope: ErrorEnvelope;
    try {
      envelope = (await response.json()) as ErrorEnvelope;
    } catch {
      envelope = {
        error: 'unreadable_response',
        message: `The layout proposal crop could not be loaded (HTTP ${response.status}).`,
        request_id: response.headers.get('x-request-id') ?? 'unknown',
      };
    }
    throw new ApiError(response.status, envelope);
  }
  return response.blob();
}

export type ApprovalOut =
  paths['/api/v1/projects/{project_id}/review-sessions/{review_session_id}/approve']['post']['responses'][201]['content']['application/json'];

export async function downloadDocument(
  projectId: string,
  packageId: string,
  documentVersionId: string,
): Promise<Blob> {
  const response = await fetch(
    `${BASE}/projects/${projectId}/packages/${packageId}/documents/${documentVersionId}/download`,
    { headers: { Accept: 'application/pdf,*/*;q=0.8' } }
  );

  if (!response.ok) {
    const isJson = response.headers.get('content-type')?.includes('application/json');
    let envelope: ErrorEnvelope;
    if (isJson) {
      envelope = await response.json();
    } else {
      envelope = {
        error: 'Download Failed',
        message: `The document could not be downloaded (HTTP ${response.status}).`,
        request_id: '',
      };
    }
    throw new ApiError(response.status, envelope);
  }

  return response.blob();
}
