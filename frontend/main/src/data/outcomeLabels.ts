import type { Outcome } from './types';

/**
 * What a reviewer reads for each engine outcome.
 *
 * **These say what happened to the check, not what the engine called it.** `Outcome` is the stored
 * vocabulary and never changes; this is the one place that decides the words on the screen.
 *
 * It exists because those words were not agreed. `NOT_FOUND` rendered as `NOT FOUND` beside an
 * evidence panel showing a crop perfectly well, and a reviewer reads that as *the evidence was not
 * found* — when it means the check could not run because a value it needs has not been supplied.
 * `N/A RULE` had the same problem the other way round: it reads like the rule is broken, when the
 * rule deliberately does not apply. Meanwhile the narration under the very same card already said
 * "Waiting on a value", and the findings table said "Not found" — three vocabularies, one screen.
 *
 * The strings are copied from `workflow/findings_composer.py::_OUTCOME_LABELS`, which is the
 * authority: the backend composes the narration a reviewer reads first, so the badge beside it has
 * to agree word for word or the screen contradicts itself. `tests/test_outcome_labels.py`
 * fails if the two drift apart.
 */
export const OUTCOME_LABELS: Record<Outcome, string> = {
  PASS: 'Looks right',
  FAIL: 'Needs correction',
  REVIEW_REQUIRED: 'Needs your decision',
  NOT_FOUND: 'Waiting on a value',
  NO_APPLICABLE_RULE: 'Not applicable',
};
