/**
 * "Read depths again" (#1308), kept free of React so it can be tested: one status request per
 * package while the page is open (every depth card shares it), and the estimate in words.
 */
import { getReadDepthsAgain, type ReadDepthsAgainStatus } from '@/api/client';
import { projectId } from '@/api/config';

const statuses = new Map<string, Promise<ReadDepthsAgainStatus>>();

/** The package's status, asked once and shared; `fresh` asks again (after a request was made). */
export function readDepthsAgainStatus(packageId: string, fresh = false): Promise<ReadDepthsAgainStatus> {
  let found = fresh ? undefined : statuses.get(packageId);
  if (!found) {
    found = getReadDepthsAgain(projectId(), packageId);
    statuses.set(packageId, found);
    // A failed request is not kept: the next card asks again.
    found.catch(() => statuses.delete(packageId));
  }
  return found;
}

/** For tests: forget every package's status. */
export function forgetReadDepthsAgain(): void {
  statuses.clear();
}

/**
 * About how many questions, in words: exactly as many as the last complete reading asked, or, before
 * any, at least the range per countertop (other views on the pages are asked about too).
 */
export function questionsWords(estimate: NonNullable<ReadDepthsAgainStatus['estimate']>): string {
  const { questions_low: low, questions_high: high } = estimate;
  if (low === high) return `About ${low} ${low === 1 ? 'question' : 'questions'}`;
  return `At least ${low} to ${high} questions`;
}
