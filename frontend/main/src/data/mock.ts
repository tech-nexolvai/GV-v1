/**
 * The shapes the review UI renders, and fixtures to develop against.
 *
 * This module was imported by eight components and had never been committed — `.gitignore` carried an
 * unanchored `data/` rule that matched any directory of that name, so the whole folder was invisible
 * to git. #458 anchored the rule to `/data/`; this restores what was missing.
 *
 * **The types are written against the real API, not invented.** Every enum below is the exact member
 * list the backend serves, so a value that cannot come out of `GET /findings` cannot be typed here
 * either. Where a field the prototype wants has no backend source yet, it is marked so — that is the
 * remaining connection work rather than something to paper over with a plausible default.
 *
 * Sources: `verdict/finding.py` (Outcome), `rules/schema.py` (Severity),
 * `app/models/package.py` (PackageState), `app/schemas/findings.py`, `app/schemas/packages.py`.
 */

/** Exactly `verdict.finding.Outcome`. Note `NOT_FOUND` and `REVIEW_REQUIRED` are outcomes in their
 *  own right — an abstention is a result, never a quiet pass. */
export type Outcome =
  | 'PASS'
  | 'FAIL'
  | 'NOT_FOUND'
  | 'REVIEW_REQUIRED'
  | 'NO_APPLICABLE_RULE';

/** Exactly `rules.schema.Severity`. */
export type Severity = 'CRITICAL' | 'MAJOR' | 'MINOR' | 'ADVISORY';

/** The package lifecycle from `docs/DESIGN_PLATFORM.md` §5, as `app/models/package.py` spells it. */
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
export type ReviewerAction = 'confirm' | 'correct' | 'except' | 'dismiss';

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
  expected: string;
  found: string;
  delta: string;
  /**
   * **Absent in V1, deliberately.** Raj settled on exact match with no tolerance band
   * (`docs/CLIENT_FACTS.md` Q2, `docs/decisions/V1_VERDICT_MODEL.md` D1) — the reviewer clearing a
   * flag *is* the tolerance. Nothing populates this, so the card's tolerance row never renders.
   * Kept on the type because graded tolerances are deferred past iteration 1, not ruled out.
   */
  tolerance?: string;
  reason: string;
  trace: Trace;
  arch_evidence: Evidence | null;
  shop_evidence: Evidence | null;
  reviewer_action: ReviewerAction | null;
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

// ---------------------------------------------------------------------------
// Fixtures
//
// Built around CT-1 (countertop width), the first real client rule and the one the whole system
// exists to check. The numbers are exact inch fractions rendered as written, because that is how a
// reviewer checks them — a decimal here would misrepresent what the engine compared.
// ---------------------------------------------------------------------------

const CT1_TRACE: Trace = {
  operation: 'equals',
  operands: [
    { name: 'countertop_width', value: '96', status: 'CORROBORATED', source: 'SHOP' },
    { name: 'cabinet_run_total', value: '94 1/2', status: 'CORROBORATED', source: 'SHOP' },
    { name: 'filler_total', value: '2', status: 'CORROBORATED', source: 'SHOP' },
    { name: 'field_cut_total', value: '2', status: 'CORROBORATED', source: 'USER_INPUT' },
  ],
  comparison: '96 == 94 1/2 + 2 + 2  →  96 == 98 1/2',
};

export const MOCK_FINDINGS: Finding[] = [
  {
    id: 'f-001',
    check_id: 'CT-WIDTH-001',
    name: 'Countertop width equals cabinets plus fillers plus field cut',
    outcome: 'FAIL',
    severity: 'CRITICAL',
    expected: '98 1/2',
    found: '96',
    delta: '2 1/2',
    reason:
      'The countertop is 2 1/2" narrower than the run beneath it. Exact match is the V1 rule, so any difference is flagged for the reviewer.',
    trace: CT1_TRACE,
    arch_evidence: {
      page: 3,
      polygon: [[0.12, 0.44], [0.38, 0.44], [0.38, 0.49], [0.12, 0.49]],
      raw_text: '98 1/2"',
      extractor: 'pdfplumber',
    },
    shop_evidence: {
      page: 1,
      polygon: [[0.21, 0.62], [0.44, 0.62], [0.44, 0.67], [0.21, 0.67]],
      raw_text: '96"',
      extractor: 'pdfplumber',
    },
    reviewer_action: null,
  },
  {
    id: 'f-002',
    check_id: 'CT-DEPTH-001',
    name: 'Countertop depth equals cabinet depth plus overhang',
    outcome: 'PASS',
    severity: 'CRITICAL',
    expected: '25 1/2',
    found: '25 1/2',
    delta: '0',
    reason: 'Depth matches exactly.',
    trace: {
      operation: 'equals',
      operands: [
        { name: 'countertop_depth', value: '25 1/2', status: 'CORROBORATED', source: 'SHOP' },
        { name: 'cabinet_depth', value: '24', status: 'CORROBORATED', source: 'SHOP' },
        { name: 'overhang', value: '1 1/2', status: 'CORROBORATED', source: 'USER_INPUT' },
      ],
      comparison: '25 1/2 == 24 + 1 1/2',
    },
    arch_evidence: null,
    shop_evidence: {
      page: 1,
      polygon: [[0.55, 0.30], [0.71, 0.30], [0.71, 0.35], [0.55, 0.35]],
      raw_text: '25 1/2"',
      extractor: 'pdfplumber',
    },
    reviewer_action: 'confirm',
  },
  {
    id: 'f-003',
    check_id: 'CT-BACK-OFFSET-MIN-001',
    name: 'Back offset clears the faucet hole',
    outcome: 'REVIEW_REQUIRED',
    severity: 'CRITICAL',
    expected: 'at least the vendor minimum',
    found: '2 1/8',
    delta: 'unknown',
    reason:
      'The back offset is a calculated remainder and its global minimum has not been supplied yet, so this cannot be decided. Abstention, not a pass.',
    trace: {
      operation: 'minimum',
      operands: [
        { name: 'back_offset', value: '2 1/8', status: 'CORROBORATED', source: 'SHOP' },
        { name: 'back_offset_minimum', value: '—', status: 'RAW_CANDIDATE', source: 'USER_INPUT' },
      ],
      comparison: 'back_offset >= back_offset_minimum  →  not decidable',
    },
    arch_evidence: null,
    shop_evidence: null,
    reviewer_action: null,
  },
];

export const MOCK_PACKAGE: PackageSummary = {
  id: 'pkg-4417',
  project: 'Ridgewood — Block C',
  vendor: 'Graniti Vicentia',
  status: 'AWAITING_REVIEW',
  submitted_at: '2026-08-24T09:15:00Z',
  reviewer: 'anant',
  category: 'Vanity',
  pass_count: 41,
  fail_count: 3,
  review_count: 6,
  missing_count: 2,
};

export const MOCK_PACKAGES_LIST: PackageSummary[] = [
  MOCK_PACKAGE,
  {
    id: 'pkg-4418',
    project: 'Ridgewood — Block D',
    vendor: 'Graniti Vicentia',
    status: 'RUNNING_CHECKS',
    submitted_at: '2026-08-25T14:02:00Z',
    reviewer: null,
    category: 'Kitchen',
    pass_count: 0,
    fail_count: 0,
    review_count: 0,
    missing_count: 0,
  },
  {
    id: 'pkg-4402',
    project: 'Fairview — Tower 2',
    vendor: 'Graniti Vicentia',
    status: 'APPROVED',
    submitted_at: '2026-08-19T11:40:00Z',
    reviewer: 'keyur',
    category: 'Vanity',
    pass_count: 58,
    fail_count: 0,
    review_count: 0,
    missing_count: 0,
  },
];

export const MOCK_MESSAGES: ChatMessage[] = [
  {
    id: 'm-1',
    role: 'user',
    content: 'Review the Ridgewood Block C vanity package.',
    timestamp: '2026-08-24T09:16:00Z',
  },
  {
    id: 'm-2',
    role: 'assistant',
    content:
      '50 checks ran. 41 passed, 3 failed and 6 need review. Exact match is the V1 rule, so every difference is flagged — clear the false ones and finalise.',
    timestamp: '2026-08-24T09:16:12Z',
    findings: MOCK_FINDINGS,
  },
];

export const MOCK_SESSIONS: Session[] = [
  {
    id: 's-1',
    package_id: 'pkg-4417',
    package_label: 'Ridgewood — Block C',
    vendor: 'Graniti Vicentia',
    status: 'AWAITING_REVIEW',
    messages: MOCK_MESSAGES,
  },
  {
    id: 's-2',
    package_id: 'pkg-4402',
    package_label: 'Fairview — Tower 2',
    vendor: 'Graniti Vicentia',
    status: 'APPROVED',
    messages: [],
  },
];

