/**
 * The one line under a confirmed countertop's heading that says where it stands with the architect's
 * drawing (#1273, Milan by item). Read-only words from `GET …/countertop-items/architect`: never a
 * result, never a decision, and no check reads them.
 */
import type { CountertopItemArchitect } from '@/api/client';

/** What a confirmed piece is, in a word, from its item type. */
const PIECE_WORDS: Record<string, string> = { cabinet_width: 'cabinet', filler_width: 'filler' };

/** The words for the compared pairs: "the overall and piece 2 (cabinet)". */
function pairWords(pairs: CountertopItemArchitect['pairs']): string {
  const said = pairs.map((pair) => {
    if (pair.kind === 'overall') return 'the overall';
    const kind = pair.piece_item_type ? PIECE_WORDS[pair.piece_item_type] : undefined;
    return kind ? `${pair.label} (${kind})` : pair.label;
  });
  if (said.length <= 1) return said[0] ?? 'nothing';
  return `${said.slice(0, -1).join(', ')} and ${said[said.length - 1]}`;
}

/**
 * "Architect: not compared — <the stored reason>", "Architect: waits for you — <what to do>", or
 * "Architect: compared the overall and piece 2 (cabinet)". When the dashboard already says the same
 * "not compared" reason once above the table (`sharedReason`), the line points there instead of
 * repeating it.
 */
export function itemArchitectWords(
  status: Pick<CountertopItemArchitect, 'state' | 'reason' | 'pairs'>,
  sharedReason: string | null = null,
): string {
  if (status.state === 'compared') return `Architect: compared ${pairWords(status.pairs)}`;
  if (status.state === 'waits_for_reviewer') {
    return status.reason ? `Architect: waits for you — ${status.reason}` : 'Architect: waits for you';
  }
  if (sharedReason !== null && status.reason === sharedReason) return 'Architect: not compared (the reason is above the table)';
  return status.reason ? `Architect: not compared — ${status.reason}` : 'Architect: not compared';
}
