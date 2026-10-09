/** A request can resolve successfully while the review decision itself is refused. */
export type DecisionSaveResult = { saved: true } | { saved: false; error: string };
export type SimpleReviewAction = 'confirm' | 'dismiss';
