import type { SlotReaderRow } from '../../api/client.js';

export type SlotReaderReviewPayload = {
  wall_config?: string;
  measurements?: Record<string, string>;
};

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
    ...(wallDraft !== undefined && wallDraft !== '' && wallDraft !== row.wall_config
      ? { wall_config: wallDraft }
      : {}),
    ...(Object.keys(measurements).length ? { measurements } : {}),
  };
}

export function shouldOfferRowWallControl(row: SlotReaderRow): boolean {
  return (
    row.wall_source === 'readers' ||
    row.wall_source === 'drawing-and-readers' ||
    !row.wall_proposal ||
    row.wall_config !== null
  );
}
