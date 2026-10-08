import type { CountertopResult } from '@/api/client';

/**
 * Where the to-scale countertop picture goes (#1039 leaves the slot; redesign prompt 4 fills it).
 * Until then it renders nothing visible, so no row shows an empty box or a placeholder sentence.
 */
export function CountertopStrip({ row }: { row: CountertopResult }) {
  return <div data-slot="countertop-strip" data-row={row.row_id} hidden />;
}
