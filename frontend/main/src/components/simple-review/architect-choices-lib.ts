/**
 * The rules behind an architect question on the simple review page (#1368), apart from the card
 * component: which reads it uses, what the AIs proposed, and what each answer sends through W3.
 */
import { ApiError, getArchitectPairing, getArchitectViewMatch, type ArchitectPairing, type ArchitectViewMatch } from '@/api/client';
import type { ActionExtra } from '@/api/review-items';
import { readerWord } from '@/lib/review-items';

/** The existing reads behind an architect question: the view match and the pairing of one row. */
export interface ArchitectLoader {
  /** Null when this row has no view match (a combined sheet): the question is then the pairing. */
  viewMatch: (projectId: string, packageId: string, rowId: string) => Promise<ArchitectViewMatch | null>;
  pairing: (projectId: string, packageId: string, rowId: string) => Promise<ArchitectPairing>;
}

export const liveArchitectLoader: ArchitectLoader = {
  viewMatch: (projectId, packageId, rowId) =>
    getArchitectViewMatch(projectId, packageId, rowId).catch((error: unknown) => {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }),
  pairing: getArchitectPairing,
};

/** The note a "None of these" pairing carries: the existing POST records it with the answer. */
export const NONE_COMPARABLE_NOTE = "Nothing on the architect's drawing is comparable (simple review).";

/** Who proposed a pairing, in words; null for a reviewer's own (not a proposal). */
export const PAIRED_BY: Record<string, string> = {
  code: 'code',
  'both-ais': 'both AIs',
  'code+ais': 'code and both AIs',
};

export type Question =
  | { kind: 'view'; match: ArchitectViewMatch; recordId: string }
  | { kind: 'pairing'; pairing: ArchitectPairing };

export type Loaded = { status: 'loading' } | { status: 'error'; message: string } | { status: 'ready'; question: Question };

function joinWords(words: readonly string[]): string {
  return words.length <= 1 ? words.join('') : `${words.slice(0, -1).join(', ')} and ${words[words.length - 1]}`;
}

/** The view the AIs proposed: the pickable view most AIs picked, when exactly one leads. */
export function viewProposal(match: ArchitectViewMatch): { viewId: string; by: string } | null {
  const pickable = match.candidates.filter((candidate) => candidate.can_pick && candidate.ai_picked_by.length > 0);
  if (pickable.length === 0) return null;
  const most = Math.max(...pickable.map((candidate) => candidate.ai_picked_by.length));
  const leaders = pickable.filter((candidate) => candidate.ai_picked_by.length === most);
  if (leaders.length !== 1) return null;
  return { viewId: leaders[0].view.view_id, by: joinWords(leaders[0].ai_picked_by.map(readerWord)) };
}

export function pairingBody(pairing: ArchitectPairing): ActionExtra | null {
  const effective = pairing.effective;
  if (!effective || effective.pairs.length === 0) return null;
  return {
    architect_pairing: {
      pairs: effective.pairs.map((pair) => ({
        kind: pair.kind === 'overall' ? 'overall' : 'piece',
        architect_candidate_id: pair.architect_candidate_id,
        vendor_slot_indices: [...pair.vendor_slot_indices],
      })),
      ...(pairing.current ? { expected_record_id: pairing.current.record_id } : {}),
    },
  };
}

/** What the C key sends on an architect question: the proposal, or null when there is none. */
export function architectProposal(question: Question): ActionExtra | null {
  if (question.kind === 'view') {
    const proposal = viewProposal(question.match);
    return proposal ? { architect_view: { view_id: proposal.viewId, expected_record_id: question.recordId } } : null;
  }
  return PAIRED_BY[question.pairing.effective?.source ?? ''] ? pairingBody(question.pairing) : null;
}

