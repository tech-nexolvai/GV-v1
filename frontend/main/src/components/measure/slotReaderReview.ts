import type { SlotReaderRow } from '../../api/client.js';
import { wallQuestion } from '../../lib/needs-you-queue';
import { countOf, type StepCount } from '../../lib/measure-steps';

export type SlotReaderReviewPayload = {
  wall_config?: string;
  measurements?: Record<string, string>;
};

export function rowWallSelection(draft: string | undefined, saved: string | null): string {
  return draft ?? saved ?? '';
}

/** Build only the choices a person actually made, never defaults rendered from AI proposals. */
export function slotReaderReviewPayload(
  row: SlotReaderRow,
  wallDraft: string | undefined,
  valueDrafts: Record<string, string> | undefined,
): SlotReaderReviewPayload {
  const measurements = Object.fromEntries(
    Object.entries(valueDrafts ?? {}).filter(([, value]) => value.trim() !== ''),
  );
  return {
    ...(row.wall_source !== 'between-panels' && wallDraft !== undefined && wallDraft !== '' && wallDraft !== row.wall_config
      ? { wall_config: wallDraft }
      : {}),
    ...(Object.keys(measurements).length ? { measurements } : {}),
  };
}

/**
 * For the Measurements step bar (#1061): a row still has work here while a width is missing on an
 * unheld row (a held row's inputs are disabled and the server refuses them) or its wall question is
 * unanswered (the queue's own rule; a held row may still allow one, e.g. between panels).
 */
export function slotRowCount(rows: readonly SlotReaderRow[]): StepCount {
  const open = rows.filter(
    (row) => (!row.held_reason && row.values.some((value) => value.needs_value)) || wallQuestion(row) !== null,
  ).length;
  return countOf(rows.length, open);
}

/** Rows with something typed or chosen that "Save this row" has not sent yet. */
export function unsavedRowCount(
  rows: readonly SlotReaderRow[],
  wallDrafts: Readonly<Record<string, string>>,
  valueDrafts: Readonly<Record<string, Readonly<Record<string, string>>>>,
): number {
  return rows.filter((row) => {
    const typed = Object.values(valueDrafts[row.row_id] ?? {}).some((value) => value.trim() !== '');
    const wall = wallDrafts[row.row_id];
    return typed || (wall !== undefined && wall !== '' && wall !== row.wall_config);
  }).length;
}

export function shouldOfferRowWallControl(row: SlotReaderRow): boolean {
  // The same for every wall case (#1202): the walls do not change the result, so none is asked.
  if (row.walls_needed === false && row.wall_config === null) return false;
  return (
    // Walls set automatically can always be corrected (#1202 review).
    row.wall_source === 'code-ink-and-both-ais' ||
    row.wall_source === 'vendor-drawing-clues' ||
    row.wall_source === 'readers' ||
    row.wall_source === 'drawing-and-readers' ||
    row.wall_source === 'between-panels' ||
    !row.wall_proposal ||
    row.wall_config !== null
  );
}
