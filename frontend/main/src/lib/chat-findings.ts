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
import { inchNumber, inchWords } from '@/lib/chat-trust';

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

// ── Areas (#1508) ───────────────────────────────────────────

/**
 * Words that name a PART of a unit, not the area it is in. An item's area is the words before the
 * first of these: "Pantry (left) refrigerator outlet" → "Pantry (left)",
 * "Nurse station overall width" → "Nurse station". An item that starts with one of them
 * ("Filler at wall end", "Material tags") names no area.
 */
const PART_WORDS = new Set([
  'backsplash', 'base', 'bays', 'blocking', 'cabinet', 'cabinets', 'chain', 'cooler', 'count', 'counter', 'countertop',
  'countertops', 'cubby', 'depth', 'door', 'doors', 'drawer', 'drawers', 'edge', 'elevation', 'end', 'ends', 'enclosure',
  'false', 'filler', 'fillers', 'finish', 'floor', 'fridge', 'front', 'height', 'heights', 'island', 'j-box', 'kick', 'layout',
  'leg', 'legs', 'lower', 'lowers', 'material', 'materials', 'microwave', 'opening', 'outlet', 'outlets', 'overall', 'panel',
  'panels', 'plan', 'pull', 'pulls', 'refrigerator', 'row', 'run', 'section', 'shelf', 'shelves', 'side', 'sides',
  'sink', 'size', 'sizes', 'spacing', 'splash', 'string', 'tag', 'tags', 'toe', 'top', 'tops', 'total', 'upper', 'uppers',
  'wall', 'wall-to-wall', 'width', 'widths',
]);

/** Lead-ins that are not areas ("Room elevation overall height"). */
const NOT_AREAS = new Set(['room', 'page', 'sheet', 'the', 'vendor', 'architect', 'gv', 'all', 'each', 'typical']);

/** At most this many words make an area name; a longer lead-in is a sentence, not an area. */
const AREA_WORDS = 4;

/** The area an item names ("Pantry (left)"), or null when it names none. */
export function areaOf(item: string | null | undefined): string | null {
  const words = String(item ?? '').trim().split(/\s+/).filter(Boolean);
  const at = words.findIndex((word) => PART_WORDS.has(word.toLowerCase().replace(/[^a-z-]/g, '')));
  if (at <= 0 || at > AREA_WORDS) return null;
  const area = words.slice(0, at).join(' ').replace(/[\s:;,/–-]+$/, '').trim();
  return area && !NOT_AREAS.has(area.toLowerCase()) ? area : null;
}

const areaKey = (area: string) => area.toLowerCase().replace(/[^a-z0-9()' ]+/g, ' ').replace(/\s+/g, ' ').trim();

export interface AreaGroup {
  /** Stable key: the area's key, or "page:<n>" when the findings name no area. */
  key: string;
  /** "Pantry", or "Page 15" when the findings name no area. */
  name: string;
  /** True when the group is a page, not a named area. */
  byPage: boolean;
  /** The vendor pages its findings are on, in order. */
  pages: number[];
  findings: ChatFinding[];
  counts: FindingCounts;
}

const STATUS_RANK = { different: 0, check: 1, same: 2 } as const;

/**
 * The shown findings grouped by AREA (#1508): the area each item names; a finding that names none
 * joins the area most named on its page, else a group for its page. Groups with the most wrong
 * items come first (then the most to check, then the first page); inside a group, wrong first,
 * then to check, then right, each by page and then in the order they were reported. Two areas
 * where one name starts the other ("Coffee point" and "Coffee point station") are one group, unless the
 * longer one is numbered ("Storage area-1").
 */
export function groupByArea(findings: readonly ChatFinding[], filter: FindingFilter = NO_FILTER): AreaGroup[] {
  const shown = findings.filter((finding) => matchesFilter(finding, filter));
  // Every named area across ALL findings (not just the shown ones), so a filter never renames a group.
  const named = new Map<string, string>();
  for (const finding of findings) {
    const area = areaOf(finding.item);
    if (area && !named.has(areaKey(area))) named.set(areaKey(area), area);
  }
  // "coffee point station" folds into "coffee point": the shorter name whose words start the longer one.
  // A numbered area stays its own ("storage area-1" is not "storage").
  const keys = [...named.keys()].sort((a, b) => a.length - b.length);
  const root = new Map<string, string>();
  for (const key of keys) {
    root.set(key, keys.find((shorter) => key === shorter || (key.startsWith(`${shorter} `) && !/\d/.test(key.slice(shorter.length)))) ?? key);
  }
  const keyOf = (finding: ChatFinding): string | null => {
    const area = areaOf(finding.item);
    return area ? root.get(areaKey(area)) ?? areaKey(area) : null;
  };
  // The area most named on each page, for the findings that name none.
  const perPage = new Map<number, Map<string, number>>();
  for (const finding of findings) {
    const key = keyOf(finding);
    if (!key) continue;
    for (const page of [finding.page, finding.vs_page]) {
      if (page == null) continue;
      const tally = perPage.get(page) ?? new Map<string, number>();
      tally.set(key, (tally.get(key) ?? 0) + 1);
      perPage.set(page, tally);
    }
  }
  const pageArea = (page: number): string | null => {
    const tally = perPage.get(page);
    if (!tally) return null;
    return [...tally.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null;
  };

  const groups = new Map<string, AreaGroup>();
  for (const finding of shown) {
    const key = keyOf(finding) ?? pageArea(finding.page) ?? (finding.vs_page != null ? pageArea(finding.vs_page) : null);
    const groupKey = key ?? `page:${finding.page}`;
    let group = groups.get(groupKey);
    if (!group) {
      group = {
        key: groupKey,
        name: key ? named.get(key) ?? key : `Page ${finding.page}`,
        byPage: !key,
        pages: [],
        findings: [],
        counts: { different: 0, check: 0, same: 0, not_compared: 0, total: 0 },
      };
      groups.set(groupKey, group);
    }
    group.findings.push(finding);
  }
  for (const group of groups.values()) {
    group.findings.sort((a, b) => (STATUS_RANK[a.status] ?? 1) - (STATUS_RANK[b.status] ?? 1) || a.page - b.page);
    group.pages = [...new Set(group.findings.map((finding) => finding.page))].sort((a, b) => a - b);
    group.counts = findingCounts(group.findings);
  }
  return [...groups.values()].sort((a, b) => b.counts.different - a.counts.different || b.counts.check - a.counts.check || (a.pages[0] ?? 0) - (b.pages[0] ?? 0));
}

/** A group's pages: "p39–40", "p3–4, 8", "p12". */
export function pagesWords(pages: readonly number[]): string {
  const runs: string[] = [];
  let start: number | null = null;
  let last: number | null = null;
  const close = () => { if (start !== null && last !== null) runs.push(start === last ? `${start}` : `${start}–${last}`); };
  for (const page of pages) {
    if (last !== null && page === last + 1) { last = page; continue; }
    close();
    start = page;
    last = page;
  }
  close();
  return runs.length ? `p${runs.join(', ')}` : '';
}

/** A group header's counts: "2 wrong · 1 to check" (right and not compared only when shown). */
export function groupCountWords(counts: FindingCounts): string {
  const parts: string[] = [];
  if (counts.different) parts.push(`${counts.different} wrong`);
  if (counts.check) parts.push(`${counts.check} to check`);
  if (counts.same) parts.push(`${counts.same} right`);
  if (counts.not_compared) parts.push(`${counts.not_compared} not compared`);
  return parts.join(' · ');
}

/** The groups open at first: the first three, or all of them when there are few rows. */
export const OPEN_GROUPS = 3;
export const FEW_ROWS = 15;

// ── One value in a row (#1508) ──────────────────────────────

/** The short pill a value that is a sentence (not a number) shows in its row. */
export type ValuePill = 'layout' | 'tag' | 'rule' | 'sum' | 'note';

export type RowValue =
  /** Code's inches, "42 in" (the printed form on hover). */
  | { kind: 'inches'; text: string; printed: string | null }
  /** A short printed value, shown as printed ("3' - 6"", "CT-001"). */
  | { kind: 'short'; text: string }
  /** A sentence: a pill in the row, the full text in the row's detail. */
  | { kind: 'pill'; pill: ValuePill; text: string }
  | { kind: 'none' };

/** A printed value at most this long shows as printed in a row; longer ones become a pill. */
const SHORT_VALUE = 14;

function pillFor(finding: Pick<ChatFinding, 'item' | 'source'>, text: string): ValuePill {
  if (finding.source === 'rules') return 'rule';
  if (finding.source === 'vendor_arithmetic') return 'sum';
  const words = `${finding.item} ${text}`.toLowerCase();
  if (/\b(tags?|material|finish|laminate)\b/.test(words)) return 'tag';
  if (/\b(layout|count|doors?|drawers?|arrangement|bays?|cubby|shelf|shelves|uppers?)\b/.test(words)) return 'layout';
  return 'note';
}

/** One side's value as a row shows it: inches, a short printed value, or a pill. */
export function rowValue(finding: Pick<ChatFinding, 'item' | 'source' | 'architect' | 'vendor' | 'inches'>, side: 'architect' | 'vendor'): RowValue {
  const raw = finding[side];
  const text = raw === null || raw === undefined ? '' : String(raw).trim();
  const inches = inchNumber(finding.inches?.[side]);
  if (inches !== null) return { kind: 'inches', text: `${inchWords(inches)} in`, printed: text || null };
  if (!text) return { kind: 'none' };
  if (text.length <= SHORT_VALUE) return { kind: 'short', text };
  return { kind: 'pill', pill: pillFor(finding, text), text };
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
