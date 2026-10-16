import type { AiReviewCounts, AiReviewOut, AiReviewResult, AiReviewRow, ItemAction } from '@/api/review-items';

// The AI page review's words and groups: what the server sent, never worked out.

/** The four groups, in the order they are shown. "Unsure" holds unsure and unreadable rows. */
export type AiReviewGroup = 'different' | 'unsure' | 'same' | 'not_shown';

export const GROUP_OF: Record<AiReviewResult, AiReviewGroup> = {
  different: 'different',
  unsure: 'unsure',
  unreadable: 'unsure',
  same: 'same',
  not_shown: 'not_shown',
};

export const GROUP_TITLE: Record<AiReviewGroup, string> = {
  different: 'Different',
  unsure: 'Unsure / unreadable',
  same: 'Same',
  not_shown: 'Not shown by the vendor',
};

/** One row with the page it is on. */
export interface AiReviewEntry {
  row: AiReviewRow;
  page: number;
}

/** Every row of every page, in page order. */
export function aiReviewEntries(review: AiReviewOut): AiReviewEntry[] {
  return review.pages.flatMap((page) => page.rows.map((row) => ({ row, page: page.page })));
}

/** The server's counts, else counted from the rows (never silent). */
export function aiReviewCounts(review: AiReviewOut): AiReviewCounts {
  if (review.counts) return review.counts;
  const counts: AiReviewCounts = { same: 0, different: 0, not_shown: 0, unsure: 0, unreadable: 0, needs_person: 0 };
  for (const { row } of aiReviewEntries(review)) {
    counts[row.result] += 1;
    if (row.result === 'different' && row.status === 'open') counts.needs_person += 1;
  }
  return counts;
}

/** "AI read 13 pages: 2 different · 40 same · 30 not shown by the vendor · 5 unsure". */
export function aiReviewSummary(review: AiReviewOut): string {
  const counts = aiReviewCounts(review);
  const read = review.pages.filter((page) => page.status === 'done').length;
  const parts = [`${counts.different} different`, `${counts.same} same`, `${counts.not_shown} not shown by the vendor`, `${counts.unsure + counts.unreadable} unsure`];
  return `AI read ${read} ${read === 1 ? 'page' : 'pages'}: ${parts.join(' · ')}`;
}

/** What one row is about, in words: the line the side-by-side viewer labels its rings with. */
export function aiReviewWhat(row: AiReviewRow): string {
  if (row.vendor_text) return `architect ${row.architect_text} vs vendor ${row.vendor_text}`;
  return `architect ${row.architect_text}`;
}

/** The one-click answers a row may offer, in the reviewer's words. Others the server lists are ignored. */
export const ANSWER_WORDS: Partial<Record<ItemAction, string>> = {
  agree: 'Agree: the vendor is wrong',
  not_this: 'Not a difference',
};

/** A recorded answer, in the reviewer's words; anything else is shown as sent. */
export function answeredWord(answer: string | null): string {
  if (answer === 'agree') return 'The vendor is wrong';
  if (answer === 'not_this') return 'Not a difference';
  return answer ?? 'Answered';
}

const EVALUATOR_LEAD = 'Checked again by the evaluator: ';

/** A sentence ends with a full stop unless it already ends with one, or with ! or ?. */
function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}

/**
 * A row's reason in two parts: the AI's, then "Checked again by the evaluator: …" on its own line.
 * The server joins them into `why`; the split is by the structured `evaluator_reason`, never by
 * guessing at the words. Without that joined ending `why` is shown as it is, and the evaluator's
 * reason is added only when `why` does not already say it.
 */
export function aiReviewWhyParts(row: Pick<AiReviewRow, 'why' | 'evaluator_reason'>): { ai: string | null; evaluator: string | null } {
  const why = row.why.trim();
  const reason = row.evaluator_reason?.trim() || null;
  if (reason === null) return { ai: why || null, evaluator: null };
  const evaluator = `${EVALUATOR_LEAD}${reason}`;
  if (why.endsWith(evaluator)) {
    const ai = why.slice(0, why.length - evaluator.length).trim();
    return { ai: ai ? sentence(ai) : null, evaluator };
  }
  if (why.includes(reason)) return { ai: why || null, evaluator: null };
  return { ai: why ? sentence(why) : null, evaluator };
}
