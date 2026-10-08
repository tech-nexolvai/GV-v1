import type { Finding, Outcome } from '../../data/types.js';
import type { SimpleReviewAction } from '../chat/decisionSave.js';

/** A failed/looping later page must never masquerade as a complete result list. */
export async function collectFindingPages<T extends { id: string }>(
  load: (cursor?: string) => Promise<{ items: T[]; next_cursor?: string | null }>,
): Promise<T[]> {
  const result: T[] = [];
  const cursors = new Set<string>();
  const ids = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await load(cursor);
    for (const item of page.items) {
      if (ids.has(item.id)) throw new Error('Results changed while loading. Refresh to get a complete list.');
      ids.add(item.id);
      result.push(item);
    }
    cursor = page.next_cursor ?? undefined;
    if (cursor && cursors.has(cursor)) throw new Error('Repeated results cursor; the complete list could not be loaded.');
    if (cursor) cursors.add(cursor);
  } while (cursor);
  return result;
}

export function actionNeedsNote(outcome: Outcome, action: SimpleReviewAction): boolean {
  return outcome === 'REVIEW_REQUIRED' || outcome === 'NOT_FOUND' || (outcome === 'FAIL' && action === 'dismiss');
}

export function decisionPayload(id: string, outcome: Outcome, action: SimpleReviewAction, note?: string) {
  if (actionNeedsNote(outcome, action) && !note?.trim()) {
    throw new Error('Write a note explaining your decision.');
  }
  return { finding_id: id, action, ...(note?.trim() ? { note: note.trim() } : {}) };
}

export function wallSourceLabel(source: string | null | undefined): string {
  const labels: Record<string, string> = {
    'vendor-drawing-clues': 'From vendor drawing clues',
    'drawing-and-readers': 'Drawing clues with a suggested wall end — needs your confirmation',
    readers: 'Suggested wall layout — needs your confirmation',
    'between-panels': 'Stone between side panels — no end field cut proposed; needs your confirmation',
    reviewer: 'Chosen by the reviewer',
  };
  return source ? labels[source] ?? 'Wall source not recognised — review the layout' : 'Wall source not recorded';
}

export function canSignOff(readiness: { can_approve: boolean; blocking_findings: number } | null): boolean {
  return readiness?.can_approve === true && readiness.blocking_findings === 0;
}

/** Display coordinates only: stored space already has page rotation applied. */
export function drawingPoints(polygon: readonly (readonly (string | number)[])[], width: number, height: number): [number, number][] {
  if (polygon.length < 3 || polygon.some(p => p.length !== 2 || p.some(v => !Number.isFinite(Number(v)) || Number(v) < 0 || Number(v) > 1))) {
    throw new Error('No valid stored outline is available for this result.');
  }
  return polygon.map(([x, y]) => [Number(x) * width, Number(y) * height]);
}

export function resultGroups(findings: readonly Finding[]) {
  const groups = new Map<number | null, Finding[]>();
  for (const finding of findings) {
    const page = finding.row_location?.page_number ?? finding.shop_evidence?.page ?? null;
    groups.set(page, [...(groups.get(page) ?? []), finding]);
  }
  return [...groups].sort(([a], [b]) => (a ?? Infinity) - (b ?? Infinity)).map(([page, items]) => ({ page, items }));
}
