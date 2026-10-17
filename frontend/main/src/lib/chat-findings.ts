/**
 * Chat v2 (#1483): the findings, coverage and short-answer rules the chat screen draws, kept free of
 * React so they can be tested alone.
 *
 * The owner's words: "reviewers don't want to see AI text… show the evidence simply… we must not
 * miss anything." So a comparison is a plain table of findings (each with its two evidence crops),
 * a coverage strip says how many pages were compared, and a one-line answer stays one line.
 * Accuracy: every value is the finding's own field, shown verbatim; nothing here re-types a number.
 */

import type { ChatCoverage, ChatFinding, CoverageStatus, FindingCategory, FindingSource, FindingStatus } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';

// ── Words ───────────────────────────────────────────────────

/** Phone width: the findings table becomes stacked cards. */
export const FINDINGS_NARROW = '(max-width: 639px)';

/** The groups, in the order the table shows them. */
export const CATEGORY_ORDER: readonly FindingCategory[] = ['countertop', 'cabinet', 'other'];

export const CATEGORY_WORDS: Record<FindingCategory, string> = {
  countertop: 'Countertops',
  cabinet: 'Cabinets',
  other: 'Other',
};

export const STATUS_WORDS: Record<FindingStatus, string> = {
  different: 'Different',
  check: 'Check',
  same: 'Same',
};

/** The small tag shown when the vendor was NOT compared with the architect's drawing. */
export const SOURCE_WORDS: Record<FindingSource, string | null> = {
  architect: null,
  brand_prototype: 'vs brand prototype',
  rules: 'GV rule',
  vendor_arithmetic: 'vendor’s own string',
  gv_review: null,
  cross_view: 'same item, another view',
  completeness: 'shown on one side only',
};

/**
 * The two value columns' names for a finding. A cross-view finding compares two views of the SAME
 * side's drawings, so its columns are "Reference view" (the governing view) and "This view".
 */
export function sideLabels(finding: Pick<ChatFinding, 'source'>): { architect: string; vendor: string } {
  return finding.source === 'cross_view'
    ? { architect: 'Reference view', vendor: 'This view' }
    : { architect: 'Architect', vendor: 'Vendor' };
}

/** What the small tick means, said on hover and to a screen reader. */
export function verifiedWords(verified: boolean): string {
  return verified ? 'Checked on the drawing' : 'Helper lead, not checked';
}

/** A finding's category, read safely: anything unknown is "other". */
export function categoryOf(finding: Pick<ChatFinding, 'category'>): FindingCategory {
  return CATEGORY_ORDER.includes(finding.category) ? finding.category : 'other';
}

/** A finding's source tag ("vs brand prototype"); null when it was compared with the architect. */
export function sourceTag(finding: Pick<ChatFinding, 'source'>): string | null {
  return SOURCE_WORDS[finding.source] ?? null;
}

/**
 * A value as the finding wrote it. Nothing is rounded, converted or re-typed; an empty value shows
 * an em dash so a blank cell never reads as "zero".
 */
export function shownValue(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '—';
  const text = String(value);
  return text.trim() ? text : '—';
}

/** The page the vendor's evidence is on. */
export function vendorPage(finding: Pick<ChatFinding, 'page' | 'vendor_box'>): number {
  return finding.vendor_box?.page ?? finding.page;
}

/**
 * The page the architect's evidence is on: its box's page, else the page it was compared with, else
 * the finding's own page (GV's combined sheets hold both drawings on one page).
 */
export function architectPage(finding: Pick<ChatFinding, 'page' | 'vs_page' | 'arch_box'>): number {
  return finding.arch_box?.page ?? finding.vs_page ?? finding.page;
}

/** The Page cell: "12", or "12 vs 40" when it was compared with another page. */
export function pageWords(finding: Pick<ChatFinding, 'page' | 'vs_page'>): string {
  return finding.vs_page != null && finding.vs_page !== finding.page ? `${finding.page} vs ${finding.vs_page}` : `${finding.page}`;
}

// ── Counting, filtering, grouping ───────────────────────────

export interface FindingCounts {
  different: number;
  check: number;
  /** "What's right" items that were compared (seen on both sides, page pair covered). */
  same: number;
  /** "What's right" items not compared: not seen is not right, so they are counted apart. */
  not_compared: number;
  total: number;
}

export function findingCounts(findings: readonly Pick<ChatFinding, 'status' | 'compared'>[]): FindingCounts {
  const counts: FindingCounts = { different: 0, check: 0, same: 0, not_compared: 0, total: findings.length };
  for (const finding of findings) {
    if (finding.status === 'same' && finding.compared === false) counts.not_compared += 1;
    else if (finding.status === 'different' || finding.status === 'check' || finding.status === 'same') counts[finding.status] += 1;
  }
  return counts;
}

/** The count line over the table: "12 differences · 3 to check" ("No differences" when none). */
export function countWords(counts: Pick<FindingCounts, 'different' | 'check'>): string {
  const parts = [
    counts.different === 0 ? 'No differences' : `${counts.different} ${counts.different === 1 ? 'difference' : 'differences'}`,
  ];
  if (counts.check > 0) parts.push(`${counts.check} to check`);
  return parts.join(' · ');
}

/**
 * The table's filter. `all` shows every difference and every "check"; rows that are the Same show
 * only when the filter asks for them (`same`). `page` narrows to one page (from the coverage grid).
 */
export type StatusFilter = 'all' | 'different' | 'check' | 'same' | 'not_compared';

export interface FindingFilter {
  status: StatusFilter;
  category: FindingCategory | null;
  page: number | null;
}

export const NO_FILTER: FindingFilter = { status: 'all', category: null, page: null };

export function matchesFilter(finding: ChatFinding, filter: FindingFilter): boolean {
  const unseen = finding.status === 'same' && finding.compared === false;
  const status = filter.status === 'all' ? finding.status !== 'same'
    : filter.status === 'not_compared' ? unseen
    : filter.status === 'same' ? finding.status === 'same' && !unseen
    : finding.status === filter.status;
  const category = filter.category === null || categoryOf(finding) === filter.category;
  const page = filter.page === null || finding.page === filter.page || finding.vs_page === filter.page;
  return status && category && page;
}

export interface FindingGroup {
  category: FindingCategory;
  findings: ChatFinding[];
}

/**
 * The findings the table shows, grouped Countertops / Cabinets / Other (empty groups left out), each
 * group by page and then in the order the findings were reported.
 */
export function groupFindings(findings: readonly ChatFinding[], filter: FindingFilter = NO_FILTER): FindingGroup[] {
  const shown = findings.filter((finding) => matchesFilter(finding, filter));
  return CATEGORY_ORDER.map((category) => ({
    category,
    // Array.prototype.sort is stable, so findings on one page keep the order they arrived in.
    findings: shown.filter((finding) => categoryOf(finding) === category).sort((a, b) => a.page - b.page),
  })).filter((group) => group.findings.length > 0);
}

/** The shown findings in reading order: what the evidence viewer's arrows step through. */
export function shownInOrder(groups: readonly FindingGroup[]): ChatFinding[] {
  return groups.flatMap((group) => group.findings);
}

/** The categories that have at least one finding, for the category chips. */
export function categoriesPresent(findings: readonly ChatFinding[]): FindingCategory[] {
  return CATEGORY_ORDER.filter((category) => findings.some((finding) => categoryOf(finding) === category));
}

// ── Coverage ────────────────────────────────────────────────

export const COVERAGE_ORDER: readonly CoverageStatus[] = ['same', 'different', 'not_comparable', 'not_opened'];

export const COVERAGE_WORDS: Record<CoverageStatus, string> = {
  same: 'Same',
  different: 'Different',
  not_comparable: 'Not comparable',
  not_opened: 'Not opened',
};

export interface CoverageCounts {
  total: number;
  same: number;
  different: number;
  not_comparable: number;
  not_opened: number;
}

/**
 * The coverage counts: the server's own when it sent them, else counted from the pages. Pages are
 * read in page order, one entry per page (a later entry for the same page wins, as the map merges).
 */
export function coverageCounts(coverage: ChatCoverage): CoverageCounts {
  if (coverage.counts) return coverage.counts;
  const counts: CoverageCounts = { total: 0, same: 0, different: 0, not_comparable: 0, not_opened: 0 };
  for (const page of coveragePages(coverage)) {
    counts.total += 1;
    if (page.status in counts) counts[page.status] += 1;
  }
  return counts;
}

/** The coverage map's pages, one per page number, in page order. */
export function coveragePages(coverage: ChatCoverage): ChatCoverage['pages'] {
  const byPage = new Map<number, ChatCoverage['pages'][number]>();
  for (const page of coverage.pages ?? []) byPage.set(page.page, page);
  return [...byPage.values()].sort((a, b) => a.page - b.page);
}

/** "58 of 60 pages compared · 2 not comparable · 0 not opened". Compared = same + different. */
export function coverageWords(counts: CoverageCounts): string {
  const compared = counts.same + counts.different;
  return `${compared} of ${counts.total} ${counts.total === 1 ? 'page' : 'pages'} compared · ${counts.not_comparable} not comparable · ${counts.not_opened} not opened`;
}

/** One page square's words, for hover and screen readers: "Page 12 (vs 40): Different. Sink model". */
export function coveragePageWords(page: ChatCoverage['pages'][number]): string {
  const pair = page.pair_page != null && page.pair_page !== page.page ? ` (vs ${page.pair_page})` : '';
  const note = page.note?.trim() ? `. ${page.note.trim()}` : '';
  return `Page ${page.page}${pair}: ${COVERAGE_WORDS[page.status] ?? page.status}${note}`;
}

// ── Short answers ───────────────────────────────────────────

/** At most this many lines and characters make an answer "short" (a yes/no, a number, a summary). */
const SHORT_LINES = 2;
const SHORT_CHARACTERS = 400;

/**
 * True for a finished one- or two-line answer with no work shown, no findings, no coverage and no
 * files: it shows just its words and the meta line, with tight spacing (no fold, no extra chrome).
 */
export function isShortAnswer(message: Pick<ViewMessage, 'status' | 'stopped' | 'blocks' | 'files' | 'findings' | 'coverage' | 'trust'>): boolean {
  if (message.status !== 'done' || message.stopped) return false;
  if (message.files.length || message.findings?.length || message.coverage || message.trust) return false;
  if (message.blocks.some((block) => block.type === 'step')) return false;
  const text = message.blocks.filter((block) => block.type === 'text').map((block) => block.text).join('\n').trim();
  if (!text) return false;
  const lines = text.split('\n').filter((line) => line.trim());
  // A table or a code block is never a short answer, even on two lines.
  if (lines.some((line) => /^\s*(\||```)/.test(line))) return false;
  return lines.length <= SHORT_LINES && text.length <= SHORT_CHARACTERS;
}
