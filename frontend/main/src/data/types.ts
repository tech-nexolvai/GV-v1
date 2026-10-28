/**
 * The check outcome the screens show.
 *
 * Until 2026-10-28 this file also held the old review screens' view models (findings, evidence,
 * package summaries), aliased into `api/schema.d.ts`. Those screens and the routes that published the
 * enums were removed; what is left is the outcome word list the outcome badge, its icon and the UI kit
 * use.
 */

/** Exactly `verdict.outcomes.Outcome`. Note `NOT_FOUND` and `REVIEW_REQUIRED` are outcomes in their
 *  own right — an abstention is a result, never a quiet pass. Written out because the API no longer
 *  publishes the enum; `tests/test_outcome_labels.py` keeps it in step with the server. */
export type Outcome = 'PASS' | 'FAIL' | 'REVIEW_REQUIRED' | 'NOT_FOUND' | 'NO_APPLICABLE_RULE';
