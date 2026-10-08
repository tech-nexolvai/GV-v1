/**
 * Made-up data for the UI kit only. No vendor, drawing number or dimension here comes from a
 * client: every value is invented so the kit can be screenshotted and shared freely.
 */
import type { Outcome } from '@/data/types';

export interface SampleFinding {
  id: string;
  page: number;
  check: string;
  scope: string;
  drawn: string;
  expected: string;
  outcome: Outcome;
}

export const SAMPLE_FINDINGS: SampleFinding[] = [
  { id: 'f1', page: 2, check: 'Countertop width', scope: 'Countertop row 2.1', drawn: '60 1/4"', expected: '61 1/4"', outcome: 'FAIL' },
  { id: 'f2', page: 2, check: 'Sink front offset', scope: 'Sink base 2.1', drawn: '4"', expected: '4"', outcome: 'PASS' },
  { id: 'f3', page: 3, check: 'Countertop width', scope: 'Countertop row 3.1', drawn: '84 1/2"', expected: '—', outcome: 'REVIEW_REQUIRED' },
  { id: 'f4', page: 4, check: 'Sink cut-out width', scope: 'Sink base 4.1', drawn: '—', expected: '29 1/2"', outcome: 'NOT_FOUND' },
  { id: 'f5', page: 5, check: 'Filler width', scope: 'Filler 5.2', drawn: '1 1/2"', expected: '1"–2"', outcome: 'PASS' },
  { id: 'f6', page: 6, check: 'Back offset minimum', scope: 'Sink base 6.1', drawn: '2 1/8"', expected: '≥ 2 1/2"', outcome: 'FAIL' },
  { id: 'f7', page: 7, check: 'Countertop width', scope: 'Countertop row 7.1', drawn: '96"', expected: '96"', outcome: 'PASS' },
  { id: 'f8', page: 8, check: 'Cabinet width', scope: 'Base cabinet 8.3', drawn: '18"', expected: '—', outcome: 'NO_APPLICABLE_RULE' },
];

/** Outcome totals for a made-up review of 32 checks. */
export const SAMPLE_TOTALS: { outcome: Outcome; count: number }[] = [
  { outcome: 'PASS', count: 19 },
  { outcome: 'FAIL', count: 4 },
  { outcome: 'REVIEW_REQUIRED', count: 6 },
  { outcome: 'NOT_FOUND', count: 3 },
];

/** Checks per drawing page, split by outcome. */
export const SAMPLE_BY_PAGE = [
  { page: 'p1', pass: 3, fail: 0, review: 1, missing: 0 },
  { page: 'p2', pass: 2, fail: 1, review: 0, missing: 0 },
  { page: 'p3', pass: 1, fail: 0, review: 2, missing: 1 },
  { page: 'p4', pass: 4, fail: 1, review: 0, missing: 1 },
  { page: 'p5', pass: 3, fail: 0, review: 1, missing: 0 },
  { page: 'p6', pass: 2, fail: 2, review: 1, missing: 0 },
  { page: 'p7', pass: 3, fail: 0, review: 0, missing: 1 },
  { page: 'p8', pass: 1, fail: 0, review: 1, missing: 0 },
];
