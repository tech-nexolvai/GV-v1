/**
 * The shapes the review UI renders.
 *
 * Renamed from `mock.ts`, which it had outgrown: it no longer holds fixtures, because no component
 * renders invented data any more. What is left is the view model — the shape a card or a row needs,
 * which is not always the shape one endpoint returns.
 *
 * **The enums are aliases into `api/schema.d.ts`, not copies of it.** They used to be hand-written
 * lists that happened to match the backend, and a list that happens to match is one rename away from
 * silently not matching — which is exactly what the generated schema exists to prevent. Written this
 * way, a member added or removed on the server is a compile error here.
 *
 * Where a field this UI wants has no backend source, it is marked so rather than given a plausible
 * default. That is the remaining connection work, not something to paper over.
 */

import type { components } from '../api/schema';


/** Exactly `verdict.finding.Outcome`. Note `NOT_FOUND` and `REVIEW_REQUIRED` are outcomes in their
 *  own right — an abstention is a result, never a quiet pass. */
export type Outcome = components['schemas']['Outcome'];

/** Exactly `rules.schema.Severity`. */
export type Severity = components['schemas']['Severity'];

/**
 * The package lifecycle from `docs/DESIGN_PLATFORM.md` §5, as `app/models/package.py` spells it.
 *
 * Still written out, unlike the enums above, because the API publishes `state` as a bare `string` —
 * there is no named schema to alias. It is the one list here that can drift without the compiler
 * noticing, which is an argument for typing that field on the server rather than for guessing better.
 */
export type PackageStatus =
  | 'CREATED'
  | 'UPLOADING'
  | 'UPLOADED'
  | 'INGESTING'
  | 'EXTRACTING'
  | 'MATCHING'
  | 'VALIDATING_EVIDENCE'
  | 'RUNNING_CHECKS'
  | 'GENERATING_OUTPUTS'
  | 'AWAITING_REVIEW'
  | 'APPROVED'
  | 'CHANGES_REQUESTED'
  // Side states. `FAILED_RETRYABLE` and `FAILED_PERMANENT` are deliberately two things: the first
  // means wait, the second means somebody must act. The prototype had a single `FAILED`, which threw
  // away the only part a reviewer can do anything about.
  | 'FAILED_RETRYABLE'
  | 'FAILED_PERMANENT'
  | 'NEEDS_INPUT'
  | 'CANCELLED'
  | 'SUPERSEDED';

/** What the reviewer did with a finding — the four typed actions in `app/review/session.py`. */
export type ReviewerAction = components['schemas']['ReviewActionKind'];

/** One reading lifted off a drawing, with enough context for a reviewer to check it.
 *
 *  `polygon` is in stored space (normalised 0..1, rotation applied) — see `evidence/coordinates.py`.
 *  It is what the evidence crop is drawn from, so the reviewer sees the number in place rather than a
 *  page reference to go and look up. */
export interface Evidence {
  page: number;
  polygon: Array<[number, number]>;
  raw_text: string;
  /** Which reader produced it. Two independent readers agreeing is what qualifies evidence. */
  extractor: string;
}

/** The arithmetic behind a verdict, so a reviewer can check the sum by hand.
 *
 *  Served by `GET /api/v1/projects/{p}/packages/{pkg}/findings/{id}/chain`. Values are exact
 *  rationals and must be rendered as written — `38 3/4`, never `38.75`. */
export interface Trace {
  operation: string;
  operands: Array<{ name: string; value: string; status: string; source: string }>;
  comparison: string;
}

/** One check the engine ran, and what it decided. */
export interface Finding {
  id: string;
  /** `rule_id` on the wire. */
  check_id: string;
  name: string;
  outcome: Outcome;
  severity: Severity;
  /**
   * The values compared, the difference, the reason and the arithmetic.
   *
   * **Optional because the list endpoint does not carry them.** `GET .../findings` returns the
   * verdict and its provenance; the operands and the trace come from `.../findings/{id}/chain`, one
   * call per finding, fetched when a card is opened rather than for every row on load. The card
   * already renders each of these conditionally, so a row shows what is known and does not invent
   * the rest.
   */
  expected?: string;
  found?: string;
  delta?: string;
  /**
   * **Absent in V1, deliberately.** Raj settled on exact match with no tolerance band
   * (`docs/CLIENT_FACTS.md` Q2, `docs/decisions/V1_VERDICT_MODEL.md` D1) — the reviewer clearing a
   * flag *is* the tolerance. Nothing populates this, so the card's tolerance row never renders.
   * Kept on the type because graded tolerances are deferred past iteration 1, not ruled out.
   */
  tolerance?: string;
  reason?: string;
  trace?: Trace;
  arch_evidence?: Evidence | null;
  shop_evidence?: Evidence | null;
  reviewer_action: ReviewerAction | null;

  /** Who last acted, when somebody has. Not always the person looking at the screen: a finding's
   *  disposition belongs to the package, so a colleague's decision shows with their name rather than
   *  as untouched work inviting a second opinion recorded as a first. */
  reviewed_by?: string | null;
}

/** One turn in the review thread. */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  timestamp: string;
  findings?: Finding[];
  is_typing?: boolean;
}

/** A package as the list and the sidebar show it.
 *
 *  `pass_count` and friends have **no backend source yet** — there is no package-summary endpoint.
 *  That is the one genuinely new piece of API the prototype implies. */
export interface PackageSummary {
  id: string;
  project: string;
  vendor: string;
  status: PackageStatus;
  submitted_at: string;
  reviewer: string | null;
  category: string;
  pass_count: number;
  fail_count: number;
  review_count: number;
  missing_count: number;
}

/** A review session in the sidebar. */
export interface Session {
  id: string;
  package_id: string;
  package_label: string;
  vendor: string;
  status: PackageStatus;
  messages: ChatMessage[];
}

// The fixtures were here: MOCK_FINDINGS, MOCK_PACKAGE, MOCK_PACKAGES_LIST, MOCK_MESSAGES,
// MOCK_SESSIONS and the CT1_TRACE they were built around. Nothing imported any of them — every
// screen reads the API now — so they were invented drawings, verdicts and reviewer names sitting in
// the bundle waiting for somebody to reach for them the next time an endpoint was inconvenient.
