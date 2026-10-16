/**
 * Chat v5 (CONTRACT5-accuracy, #1483): the accuracy badge at the end of every answer, kept free of
 * React so it can be tested alone. The score is CODE's (the share of the answer's checkable claims
 * that independent checks back); the screen only shows it, coloured the owner's way.
 */

import type { AccuracyClaim, ChatAccuracy } from '@/lib/chat-api';

/** Owner's colours: 80 and up green, 60 to 79 orange, below 60 yellow; nothing to check is grey. */
export type AccuracyTone = 'green' | 'orange' | 'yellow' | 'none';

export function accuracyTone(score: number | null | undefined): AccuracyTone {
  if (score === null || score === undefined || !Number.isFinite(score)) return 'none';
  if (score >= 80) return 'green';
  if (score >= 60) return 'orange';
  return 'yellow';
}

/** "Accuracy 92%", or "Accuracy: n/a (nothing to check)". */
export function accuracyWords(accuracy: Pick<ChatAccuracy, 'score'>): string {
  const tone = accuracyTone(accuracy.score);
  return tone === 'none' ? 'Accuracy: n/a (nothing to check)' : `Accuracy ${Math.round(accuracy.score as number)}%`;
}

/** What the score measures, in one line under its table (the points themselves come from the server's calibrated table). */
export const ACCURACY_MEANING =
  'The share of this answer’s checkable claims expected to be right, from how each was checked; any check that disagrees scores 0. “What’s right” items nobody compared are not counted.';

/** "2 not compared · 3 not checked (they lower the score)", or null when there are none. */
export function excludedWords(accuracy: Pick<ChatAccuracy, 'not_compared' | 'unknowns'>): string | null {
  const parts = [
    accuracy.not_compared ? `${accuracy.not_compared} “What’s right” ${accuracy.not_compared === 1 ? 'item' : 'items'} not compared (not counted)` : null,
    accuracy.unknowns ? `${accuracy.unknowns} not checked (they lower the score)` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(' · ') : null;
}

/**
 * The line measured on GV's own test sets, shown under the table. Set by the orchestrator from the
 * eval kit's latest run (never invented); null shows nothing.
 */
export const TEST_SET_LINE: string | null = null;

/** Points as "1", "0.75", "0.4". */
export function pointsWords(points: number): string {
  return Number.isFinite(points) ? String(Number(points.toFixed(2))) : '—';
}

/** The claims table's rows, read safely (a row with no words is left out). */
export function accuracyRows(accuracy: ChatAccuracy): AccuracyClaim[] {
  return (accuracy.breakdown ?? []).filter((row) => row && typeof row.claim === 'string' && row.claim.trim());
}

/** "Lowered for pages not accounted for (× 0.97)" when a coverage factor below 1 was applied. */
export function coverageFactorWords(factor: number | null | undefined): string | null {
  if (factor === null || factor === undefined || !Number.isFinite(factor) || factor >= 1) return null;
  return `Lowered for pages not accounted for (× ${Number(factor.toFixed(2))})`;
}
