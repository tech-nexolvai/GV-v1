/**
 * The one line under a confirmed countertop's heading that says where it stands with the architect's
 * drawing (#1273, Milan by item).
 *
 * **Built from its row's own architect result**, the very block the table under the heading shows
 * (`CountertopResult.architect`, through `architectState`), so the line can never contradict the
 * row (review of #1279): a recorded result is said as recorded, a row with no result says what the
 * row says ("Not checked yet: …" or "Not compared: …"). `GET …/countertop-items/architect` only adds
 * the confirmed piece's kind to each compared piece. Read-only words: never a result, never a
 * decision, and no check reads them.
 */
import type { ArchitectResult, CountertopItemArchitect } from '@/api/client';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { architectState, confirmWords, reasonSaysItself } from '@/lib/architect';

/** What a confirmed piece is, in a word, from its item type. */
const PIECE_WORDS: Record<string, string> = { cabinet_width: 'cabinet', filler_width: 'filler' };

/** The compared pairs as the row lists them: "the overall and piece 2 (cabinet)". */
function pairWords(result: ArchitectResult, pieces: CountertopItemArchitect['pairs']): string {
  const said = result.compared.map((pair) => {
    if (pair.kind === 'overall') return 'the overall';
    const confirmed = pieces.find((piece) => piece.kind === 'piece' && piece.vendor_piece === pair.vendor_piece);
    const kind = confirmed?.piece_item_type ? PIECE_WORDS[confirmed.piece_item_type] : undefined;
    return kind ? `piece ${pair.vendor_piece} (${kind})` : `piece ${pair.vendor_piece}`;
  });
  if (said.length <= 1) return said[0] ?? '';
  return `${said.slice(0, -1).join(', ')} and ${said[said.length - 1]}`;
}

/**
 * The line, or `null` when the row has no architect result to speak of (`architectState` "none").
 * `pieces` are the endpoint's pairs for this countertop (only their piece kinds are used);
 * `sharedReason` is the "not compared" reason the dashboard already says once above the table.
 */
export function itemArchitectWords(
  result: ArchitectResult | null | undefined,
  pieces: CountertopItemArchitect['pairs'] = [],
  sharedReason: string | null = null,
): string | null {
  const found = result ?? null;
  const state = architectState(found);
  if (found === null || state === 'none') return null;
  switch (state) {
    case 'not-compared': {
      const reason = found.not_compared_reason ?? '';
      if (sharedReason !== null && reason === sharedReason) return 'Architect: not compared (the reason is above the table)';
      return reasonSaysItself(found) ? `Architect: ${reason}` : `Architect: not compared — ${reason}`;
    }
    case 'confirm':
      return `Architect: waits for you — ${confirmWords(found)}`;
    case 'unpaired':
      return "Architect: waits for you — pair the architect's dimension with the vendor's";
    case 'choose-view':
      return "Architect: waits for you — choose which of the architect's views shows this countertop";
    case 'by-hand':
      return 'Architect: waits for you — compare this countertop by hand';
    case 'run-again':
      return 'Architect: run the checks again — not matched with an architect view yet';
    case 'view-picked':
      return 'Architect: view chosen — it counts once the checks run again';
    default: {
      // A recorded result ('compared'): said as recorded.
      const outcome = found.outcome;
      const pairs = pairWords(found, pieces);
      if (outcome === 'PASS' || outcome === 'FAIL') {
        return `Architect: compared ${pairs || 'with the architect'} — ${OUTCOME_LABELS[outcome]}`;
      }
      if (found.needs_decision) return `Architect: waits for you — ${found.reason ?? OUTCOME_LABELS.REVIEW_REQUIRED}`;
      return `Architect: ${outcome ? OUTCOME_LABELS[outcome] : 'recorded'}${pairs ? ` (${pairs})` : ''}`;
    }
  }
}
