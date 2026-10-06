/**
 * Review by exception (admin, 2026-10-06): which fields a reviewer must look at, and why.
 *
 * A field both AI readers filled with the same exact value — the evidence path's
 * `CORROBORATED` on the `SECOND_READER` lane — is shown as agreed, with no picture. Everything else
 * is a question for the reviewer, with the reason in plain words and the picture of what was read.
 * Nothing here decides a value or a verdict: it only decides what the reviewer is asked to look at.
 */

export type FieldReviewState = 'agreed' | 'needs_look' | 'done' | 'empty';

export interface FieldReview {
  state: FieldReviewState;
  reason: string | null;
}

export interface ReviewedCandidate {
  corroboration_status?: string | null;
  corroboration_lane?: string | null;
  /** A reason the form reader attached to this reading, when it gave one (#962). */
  review_reason?: string | null;
}

export interface FieldReviewInput {
  hasValue: boolean;
  /** The reviewer typed it, or confirmed a reading for it: it is theirs. */
  reviewerOwned: boolean;
  /** The readings the AI filled this field from, or null when it filled nothing. */
  proposedCandidateIds: readonly string[] | null;
  candidates: ReadonlyMap<string, ReviewedCandidate>;
  placementUnverified: boolean;
}

export const REASON = {
  empty: 'No AI reading for this field. Enter it from the drawing.',
  differ: 'The two AI readers gave different numbers.',
  oneReader: 'Only one AI reader read this number.',
  unplaced: 'Where this number sits on the drawing could not be checked.',
  missing: 'The reading behind this value is no longer listed. Check it on the drawing.',
} as const;

export function fieldReview(input: FieldReviewInput): FieldReview {
  if (!input.hasValue) return { state: 'empty', reason: REASON.empty };
  if (input.reviewerOwned) return { state: 'done', reason: null };
  const ids = input.proposedCandidateIds;
  if (!ids || ids.length === 0) return { state: 'needs_look', reason: REASON.oneReader };
  for (const id of ids) {
    const candidate = input.candidates.get(id);
    if (!candidate) return { state: 'needs_look', reason: REASON.missing };
    if (candidate.review_reason) return { state: 'needs_look', reason: candidate.review_reason };
    if (candidate.corroboration_status === 'CONFLICTING') {
      return { state: 'needs_look', reason: REASON.differ };
    }
    if (
      candidate.corroboration_status !== 'CORROBORATED' ||
      candidate.corroboration_lane !== 'SECOND_READER'
    ) {
      return { state: 'needs_look', reason: REASON.oneReader };
    }
  }
  if (input.placementUnverified) return { state: 'needs_look', reason: REASON.unplaced };
  return { state: 'agreed', reason: null };
}

/** Counts for the summary line: how much the AI settled, and how much is left for the reviewer. */
export function reviewCounts(reviews: readonly FieldReview[]): Record<FieldReviewState, number> {
  const counts: Record<FieldReviewState, number> = { agreed: 0, needs_look: 0, done: 0, empty: 0 };
  for (const review of reviews) counts[review.state] += 1;
  return counts;
}
