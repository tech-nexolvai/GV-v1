/**
 * Synthetic review items for the simple review page (#1343), shaped like the W1 read API (#1338).
 * Every value is invented. `reviewItems()` returns a fresh copy, so a test can change it freely.
 */
import type { ReviewItemsOut } from '@/api/review-items';

import data from './review-items.json';

export function reviewItems(change?: (draft: ReviewItemsOut) => void): ReviewItemsOut {
  const copy = structuredClone(data) as unknown as ReviewItemsOut;
  change?.(copy);
  return copy;
}

/** Everything decided: nothing on the page needs a person. */
export function allSettled(): ReviewItemsOut {
  return reviewItems((draft) => {
    for (const group of [draft.items, draft.failures, draft.not_checked]) {
      for (const entry of group) {
        entry.status = 'done';
        entry.decided_by = 'REVIEWER';
        if ('actions' in entry) entry.actions = [];
      }
    }
    draft.header = { ...draft.header, open: 0, failures: 0, not_checked: 0, rechecking: 0, can_finalise: true, blocked_reason: null };
  });
}
