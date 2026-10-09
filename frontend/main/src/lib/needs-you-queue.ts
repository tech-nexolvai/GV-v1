/**
 * The "Needs you" queue's rules (#1050), kept free of React so they can be tested alone.
 *
 * The queue is every item still blocking sign-off, one at a time. "Blocking" is the server's answer
 * (`countertop-results.needs_decision`, which is the readiness API's blocking set, and readiness's
 * `blocking_finding_ids` for the other checks); nothing here decides it. An item's status is read
 * from the live data each time, so it follows the server, not a local guess.
 */
import type { CountertopResult, SlotReaderRow } from '@/api/client';
import type { Finding } from '@/data/types';
import { sortRows } from '@/lib/countertop-results';

export type QueueItem =
  | { kind: 'countertop'; key: string; rowId: string; findingId: string | null; page: number; label: string }
  | { kind: 'check'; key: string; findingId: string; page: number | null; label: string };

/**
 * Countertops first, in the results table's order (`sortRows`: they all need you, so by page, then
 * label), then the package-level checks (by page, then name). The list is taken once the readiness
 * answer is in and kept, so a decided item stays where it was and the reviewer can go back to it.
 */
export function buildQueue(
  rows: readonly CountertopResult[],
  findings: readonly Finding[],
  blocking: ReadonlySet<string> | null,
): QueueItem[] {
  const countertops: QueueItem[] = sortRows(rows.filter((row) => row.needs_decision)).map((row) => ({
    kind: 'countertop',
    key: `row:${row.row_id}`,
    rowId: row.row_id,
    findingId: row.finding_id,
    page: row.page_number,
    label: row.label,
  }));
  const countertopFindings = new Set(rows.map((row) => row.finding_id).filter((id): id is string => id !== null));
  const rowIds = new Set(rows.map((row) => row.row_id));
  // A countertop result names its row (`scope_row_candidate_id`). One whose row is on screen belongs
  // to that row's item, even before the rows reload after a run; anything else is listed as a check,
  // so nothing that blocks sign-off can drop out of the list.
  const checks: QueueItem[] = findings
    .filter(
      (finding) =>
        !countertopFindings.has(finding.id) &&
        !(finding.scope_row_candidate_id && rowIds.has(finding.scope_row_candidate_id)) &&
        (blocking?.has(finding.id) ?? false),
    )
    .map((finding) => ({
      kind: 'check' as const,
      key: `finding:${finding.id}`,
      findingId: finding.id,
      page: finding.row_location?.page_number ?? finding.shop_evidence?.page ?? finding.arch_evidence?.page ?? null,
      label: finding.scope_label ?? finding.name,
    }))
    .sort((a, b) => (a.page ?? Infinity) - (b.page ?? Infinity) || a.label.localeCompare(b.label));
  return [...countertops, ...checks];
}

/**
 * - `open`: still needs the reviewer.
 * - `decided`: the server no longer counts it as blocking.
 * - `waiting-for-run`: the reviewer did their part — a wall answer or new widths (only a new check
 *   run shows their effect), or a correction (only a new run clears it; nothing recorded after it
 *   does). The server keeps it blocking until then.
 */
export type ItemStatus = 'open' | 'decided' | 'waiting-for-run';

export interface LiveData {
  rows: ReadonlyMap<string, CountertopResult>;
  findings: ReadonlyMap<string, Finding>;
  blocking: ReadonlySet<string> | null;
  /** The countertop card's rows: when each row's walls or widths were last saved. Null while loading. */
  slots?: ReadonlyMap<string, SlotReaderRow> | null;
  /** Rows whose wall answer was saved in this sitting, until the slot rows reload. */
  wallsSaved: ReadonlySet<string>;
  /**
   * Findings with a correction anywhere in their history (from the history endpoint). The server
   * keeps such a finding blocking until a new run, whatever was recorded after the correction.
   */
  corrected?: ReadonlySet<string>;
}

/**
 * True when a row's walls or widths were saved after its current result was recorded — so the
 * result is out of date and only a new check run settles it. Both times come from the server.
 */
export function inputNewerThanResult(row: CountertopResult, finding: Finding | undefined, slot: SlotReaderRow | undefined): boolean {
  if (!slot?.decided_at) return false;
  if (row.finding_id === null) return true;
  if (!finding?.created_at) return false;
  return new Date(slot.decided_at).getTime() > new Date(finding.created_at).getTime();
}

export function itemStatus(item: QueueItem, live: LiveData): ItemStatus {
  if (item.kind === 'countertop') {
    const row = live.rows.get(item.rowId);
    if (!row) {
      // Not in the current rows: decided only if the server says its result no longer blocks.
      return live.blocking !== null && item.findingId !== null && !live.blocking.has(item.findingId) ? 'decided' : 'open';
    }
    const findingId = row.finding_id;
    if (findingId !== null && (live.corrected?.has(findingId) || row.reviewer_decision?.action === 'correct')) return 'waiting-for-run';
    // The readiness answer is refreshed before a decision's save resolves; the rows reload after.
    // Both come from the same server rule, so the fresher one decides.
    const blocking = findingId === null || live.blocking === null ? row.needs_decision : live.blocking.has(findingId);
    if (!blocking) return 'decided';
    const finding = findingId ? live.findings.get(findingId) : undefined;
    if (live.wallsSaved.has(item.rowId) || inputNewerThanResult(row, finding, live.slots?.get(item.rowId))) return 'waiting-for-run';
    return 'open';
  }
  if (live.corrected?.has(item.findingId) || live.findings.get(item.findingId)?.reviewer_action === 'correct') return 'waiting-for-run';
  if (live.blocking === null) return 'open';
  return live.blocking.has(item.findingId) ? 'open' : 'decided';
}

/** The finding an item is about right now (a countertop's comes from its current row). */
export function findingIdOf(item: QueueItem, live: LiveData): string | null {
  return item.kind === 'countertop' ? live.rows.get(item.rowId)?.finding_id ?? item.findingId : item.findingId;
}

/**
 * How many findings the server still counts as blocking that no item here accounts for (open or
 * waiting for a run) — for example new results from a check run made while the queue was open.
 * Null while the readiness answer loads. The queue never says "all done" while this is not zero.
 */
export function unaccountedBlocking(items: readonly QueueItem[], live: LiveData): string[] | null {
  if (live.blocking === null) return null;
  const covered = new Set(
    items
      .filter((item) => itemStatus(item, live) !== 'decided')
      .map((item) => findingIdOf(item, live))
      .filter((id): id is string => id !== null),
  );
  return [...live.blocking].filter((id) => !covered.has(id));
}

export function progressOf(items: readonly QueueItem[], live: LiveData): { handled: number; total: number; waitingForRun: number } {
  const statuses = items.map((item) => itemStatus(item, live));
  return {
    handled: statuses.filter((s) => s !== 'open').length,
    total: items.length,
    waitingForRun: statuses.filter((s) => s === 'waiting-for-run').length,
  };
}

/**
 * The next item still open after `from` (wrapping round), or null when none is. `skip` is left out:
 * the item just decided, which the data in hand may not show as decided yet.
 */
export function nextOpen(items: readonly QueueItem[], live: LiveData, from: number, skip?: number): number | null {
  for (let step = 1; step <= items.length; step += 1) {
    const index = (from + step) % items.length;
    if (index !== skip && itemStatus(items[index], live) === 'open') return index;
  }
  return null;
}

// ── Wall questions ──────────────────────────────────────────

/** The backend's own words for the published wall layouts (`app/api/visual_countertops.py`). */
const WALL_WORDS: Record<string, string> = {
  back_left_right: 'Back wall and both ends',
  back_only: 'Back wall only',
  island: 'Island; no wall ends',
};

export function wallWords(config: string): string {
  return WALL_WORDS[config] ?? config.replaceAll('_', ' ');
}

export interface WallQuestion {
  rowId: string;
  /** The published choices, in the server's order. Nothing is pre-selected. */
  choices: string[];
  /** What the readers (or the drawing) proposed, shown as text only. */
  proposal: string | null;
  /** Stone between full-height panels: one click for "back only", as on the countertop card (#1025). */
  betweenPanels: boolean;
  reason: string | null;
}

/**
 * The wall question for a row, when the reviewer can still answer it: the server allows a wall
 * confirmation and none is saved yet. Walls found in the vendor's drawing are not a question.
 */
export function wallQuestion(slot: SlotReaderRow | null | undefined): WallQuestion | null {
  if (!slot || !slot.wall_confirmation_allowed || slot.wall_config !== null) return null;
  if (slot.wall_source === 'vendor-drawing-clues' && slot.wall_proposal) return null;
  if (slot.wall_layout_choices.length === 0) return null;
  return {
    rowId: slot.row_id,
    choices: [...slot.wall_layout_choices],
    proposal: slot.wall_proposal,
    betweenPanels: slot.wall_source === 'between-panels' && slot.wall_proposal === 'back_only',
    reason: slot.wall_reason,
  };
}
