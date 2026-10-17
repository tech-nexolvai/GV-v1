import type { Revision, RevisionComparison } from '@/api/client';
import { productLabel } from '@/lib/products';

/**
 * Words and counts for a review's revisions (#1180). Pure, so every rule is unit-tested; the screen
 * only lays these out. Every number comes from the API; nothing here works out a result.
 */

export type PageChange = RevisionComparison['drawings'][number]['pages'][number]['change'];
export type CountertopChange = RevisionComparison['countertops'][number]['change'];

/** "Revision 2 of 2". */
export function revisionTitle(number: number, total: number): string {
  return `Revision ${number} of ${total}`;
}

/** One short word for where a revision stands, never colour alone. */
export function revisionStatus(revision: Pick<Revision, 'current' | 'signed_off_at'>): string {
  if (revision.current) return revision.signed_off_at ? 'Current · signed off' : 'Current';
  return revision.signed_off_at ? 'Read-only · signed off' : 'Read-only';
}

export function drawingWord(kind: string): string {
  if (kind === 'architectural') return "Architect's drawings";
  if (kind === 'shop') return 'Shop drawings';
  return 'Drawing';
}

export const PAGE_CHANGE_WORDS: Record<PageChange, string> = {
  added: 'Added',
  removed: 'Removed',
  changed: 'Changed',
  moved: 'Moved',
  unchanged: 'Unchanged',
};

export const COUNTERTOP_CHANGE_WORDS: Record<CountertopChange, string> = {
  new: 'New in this revision',
  gone: 'Not in this revision',
  changed: 'Changed',
  same: 'Same',
};

/** "1 added · 2 changed · 0 removed · 12 unchanged", from the API's counts. A moved page (the same
 * bytes at another page number) counts as unchanged content, and is listed by itself. */
export function pageSummary(comparison: Pick<RevisionComparison, 'pages_added' | 'pages_changed' | 'pages_removed' | 'pages_unchanged'>): string {
  return [
    `${comparison.pages_added} added`,
    `${comparison.pages_changed} changed`,
    `${comparison.pages_removed} removed`,
    `${comparison.pages_unchanged} unchanged`,
  ].join(' · ');
}

export interface ChangedPage {
  key: string;
  drawing: string;
  fileName: string | null;
  pageNumber: number;
  /** Where a moved page was before. */
  beforePageNumber: number | null;
  change: PageChange;
}

/** Every page that is not unchanged, drawing by drawing in the API's order. */
export function changedPages(comparison: Pick<RevisionComparison, 'drawings'>): ChangedPage[] {
  return comparison.drawings.flatMap((drawing) =>
    drawing.pages
      .filter((page) => page.change !== 'unchanged')
      .map((page) => ({
        key: `${drawing.document_id}:${page.page_number}`,
        drawing: drawingWord(drawing.kind),
        fileName: page.file_name ?? null,
        pageNumber: page.page_number,
        beforePageNumber: page.before_page_number ?? null,
        change: page.change,
      })),
  );
}

/** Drawings whose pages could not be compared yet, with the API's reason. */
export function uncomparedDrawings(comparison: Pick<RevisionComparison, 'drawings'>): { key: string; text: string }[] {
  return comparison.drawings
    .filter((drawing) => !drawing.compared)
    .map((drawing) => ({
      key: drawing.document_id,
      text: `${drawingWord(drawing.kind)}: ${drawing.reason ?? 'not compared.'}`,
    }));
}

/** Countertop rows whose stored result is not the same in both revisions. */
export function changedCountertops(comparison: Pick<RevisionComparison, 'countertops'>): RevisionComparison['countertops'] {
  return comparison.countertops.filter((row) => row.change !== 'same');
}

/** The product a review is for, in the product registry's words (#1547). */
export function productWord(productType: string | null | undefined): string {
  return productType ? productLabel(productType) : '—';
}

/** "From revision 1 · Ana": who gave an answer carried from an earlier revision (#1180). */
export function carriedWords(revision: number | null | undefined, person: string | null | undefined): string | null {
  if (revision === null || revision === undefined) return null;
  return person ? `From revision ${revision} · ${person}` : `From revision ${revision}`;
}

export interface CarriedWall {
  rowId: string;
  label: string;
  pageNumber: number;
  words: string;
  /** A wall answer (#1180), or a page's "no countertop" confirmation (#1191). */
  kind?: 'wall' | 'no-countertop';
}

/**
 * Confirmations that a page has no countertop, carried from an earlier revision's identical page
 * (#1191): the signer says they still apply, like carried wall answers. A carried "There is a
 * countertop" is not listed: that page already needs a decision.
 */
export function carriedNoCountertopConfirmations(
  pages: readonly { page_number: number; record_id?: string | null; confirmation?: { confirmed_by: string; from_revision?: number | null } | null }[],
): CarriedWall[] {
  return pages
    .filter((page) => page.confirmation?.from_revision != null)
    .map((page) => ({
      rowId: `page:${page.record_id ?? page.page_number}`,
      label: `Page ${page.page_number}`,
      pageNumber: page.page_number,
      words: `Page ${page.page_number}: no countertop confirmed on revision ${page.confirmation!.from_revision} (${page.confirmation!.confirmed_by})`,
      kind: 'no-countertop' as const,
    }));
}

/** The countertops whose recorded result rests on a wall answer carried from an earlier revision. */
export function carriedWallsDrivingResults(
  rows: readonly {
    row_id: string;
    label: string;
    page_number: number;
    outcome: string | null;
    wall_layout: { source: string; carried_from_revision?: number | null; carried_by?: string | null };
  }[],
): CarriedWall[] {
  return rows
    .filter((row) => row.outcome !== null && row.wall_layout.source === 'reviewer' && row.wall_layout.carried_from_revision != null)
    .map((row) => ({
      rowId: row.row_id,
      label: row.label,
      pageNumber: row.page_number,
      words: carriedWords(row.wall_layout.carried_from_revision, row.wall_layout.carried_by) ?? '',
    }));
}
