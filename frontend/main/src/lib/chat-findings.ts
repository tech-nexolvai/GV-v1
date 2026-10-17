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
import { differenceWords, inchNumber, inchWords } from '@/lib/chat-trust';

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

/**
 * The owner's one vocabulary (2026-10-17): Wrong (red) · Needs your decision, short "Your decision"
 * (amber, used sparingly) · Right (green) · Not compared (grey). "To check" is never shown.
 */
export const STATUS_WORDS: Record<FindingStatus, string> = {
  different: 'Wrong',
  check: 'Your decision',
  same: 'Right',
};

/** The long form, for headings and the summary. */
export const STATUS_LONG: Record<FindingStatus, string> = {
  different: 'What’s wrong',
  check: 'Needs your decision',
  same: 'Right',
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

/** The count line: "12 wrong · 3 need your decision" ("Nothing wrong" when none). */
export function countWords(counts: Pick<FindingCounts, 'different' | 'check'>): string {
  const parts = [counts.different === 0 ? 'Nothing wrong' : `${counts.different} wrong`];
  if (counts.check > 0) parts.push(`${counts.check} ${counts.check === 1 ? 'needs' : 'need'} your decision`);
  return parts.join(' · ');
}

/**
 * The table's filter. `all` shows every difference and every "check"; rows that are the Same show
 * only when the filter asks for them (`same`). `page` narrows to one page (from the coverage grid).
 */
export type StatusFilter = 'all' | 'any' | 'different' | 'check' | 'same' | 'not_compared';

export interface FindingFilter {
  status: StatusFilter;
  category: FindingCategory | null;
  page: number | null;
}

export const NO_FILTER: FindingFilter = { status: 'all', category: null, page: null };

export function matchesFilter(finding: ChatFinding, filter: FindingFilter): boolean {
  const unseen = finding.status === 'same' && finding.compared === false;
  const status = filter.status === 'any' ? true
    : filter.status === 'all' ? finding.status !== 'same'
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

/** The findings in the order the result's sections show them (Wrong, Your decision, Right, Not compared; by area), for the viewer's arrows. */
export function sectionOrder(findings: readonly ChatFinding[], filter: FindingFilter): ChatFinding[] {
  const shown = findings.filter((finding) => matchesFilter(finding, { ...filter, status: 'any' }));
  const rank = (finding: ChatFinding) => (finding.status === 'same' && finding.compared === false ? 'not_compared' : finding.status);
  return (['different', 'check', 'same', 'not_compared'] as const).flatMap((status) =>
    groupByArea(shown.filter((finding) => rank(finding) === status), { status: 'any', category: null, page: null }).flatMap((group) => group.findings));
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
  if (counts.check) parts.push(`${counts.check} your decision`);
  if (counts.same) parts.push(`${counts.same} right`);
  if (counts.not_compared) parts.push(`${counts.not_compared} not compared`);
  return parts.join(' · ');
}

/** The groups open at first: the first three, or all of them when there are few rows. */
export const OPEN_GROUPS = 3;
export const FEW_ROWS = 15;

// ── A finding in the reviewer's words (#1522) ───────────────

/** A value that is a sentence, cut to its first clause for a one-line row ("3 lower doors with pulls"). */
export function shortValue(value: string | number | null | undefined): string | null {
  let text = value === null || value === undefined ? '' : String(value).trim();
  if (!text || /^n\/?a$/i.test(text)) return null;
  text = text.split(/;\s+/)[0].trim();
  text = text.replace(/\s*\([^)]*\)\s*$/, '').trim() || text;
  if (text.length > 48) {
    const cut = text.slice(0, 46);
    text = `${cut.slice(0, Math.max(cut.lastIndexOf(' '), 28))}…`;
  }
  return text;
}

/** A value as the card says it in full; "Nothing printed" for an empty or "n/a" one. */
export function fullValue(value: string | number | null | undefined): string {
  const text = value === null || value === undefined ? '' : String(value).trim();
  return !text || /^n\/?a$/i.test(text) ? 'Nothing printed' : text;
}

/** What the vendor was compared with, as the card's label ("Architect shows", "GV’s rule says"). */
export function referenceLabel(finding: Pick<ChatFinding, 'source'>): string {
  switch (finding.source) {
    case 'brand_prototype':
      return 'Brand prototype shows';
    case 'rules':
      return 'GV’s rule says';
    case 'vendor_arithmetic':
      return 'Its parts add to';
    case 'cross_view':
      return 'Reference view shows';
    default:
      return 'Architect shows';
  }
}

/** The short reference word in a row: "architect", "prototype", "GV’s rule". */
function referenceWord(finding: Pick<ChatFinding, 'source'>): string {
  return referenceLabel(finding).replace(/ (shows|says|add to)$/, '').replace(/^Its parts$/, 'its parts').toLowerCase().replace('gv’s', 'GV’s');
}

/** One side of the row's sentence: code's inches ("46 in"), else the short words, else "nothing". */
export interface LinePart {
  text: string;
  inches: boolean;
}

export interface FindingLine {
  vendor: LinePart;
  /** "needs" (wrong), "both" (right, same inches), or the reference ("architect shows"). */
  verb: string;
  reference: LinePart | null;
  /** The reference as printed, when the row shows code's inches ("architect 3' - 1""). */
  printed: string | null;
  /** Code's difference ("+9 in"), or null. */
  difference: string | null;
}

/**
 * A brand-prototype finding's reference value often starts with its own source ("prototype 3'-2"",
 * "Prototype shows CT-001"), and the screen already says "brand prototype shows": take that lead-in
 * off so the words are not doubled. Anything left empty keeps the value as it was.
 */
export function withoutSourceWords(value: string | number | null | undefined, source: ChatFinding['source']): string | number | null | undefined {
  if (source !== 'brand_prototype' || typeof value !== 'string') return value;
  const trimmed = value.replace(/^\s*(?:the\s+)?(?:brand\s+)?prototype\b(?:\s+(?:plan|elevation|front|rear|section))?(?:\s+(?:shows|says|has|is))?\s*[:,-]?\s*/i, '');
  return trimmed.trim() ? trimmed : value;
}

/** The finding with its reference value said once (see `withoutSourceWords`). */
function once<T extends Pick<ChatFinding, 'architect' | 'source'>>(finding: T): T {
  const architect = withoutSourceWords(finding.architect, finding.source);
  return architect === finding.architect ? finding : { ...finding, architect: architect as T['architect'] };
}

function linePart(finding: Pick<ChatFinding, 'architect' | 'vendor' | 'inches'>, side: 'architect' | 'vendor'): LinePart {
  const inches = inchNumber(finding.inches?.[side]);
  if (inches !== null) return { text: `${inchWords(inches)} in`, inches: true };
  return { text: shortValue(finding[side]) ?? 'nothing', inches: false };
}

/**
 * The row's one line, PATH step 4.4: "Vendor drew 46 in · needs 37 in (architect 3' - 1")".
 * Values are the finding's own (code's inches or the printed words); nothing is re-typed.
 */
export function findingLine(original: Pick<ChatFinding, 'source' | 'status' | 'architect' | 'vendor' | 'inches'>): FindingLine {
  const finding = once(original);
  const vendor = linePart(finding, 'vendor');
  const reference = linePart(finding, 'architect');
  const difference = inchNumber(finding.inches?.difference);
  const differenceText = difference !== null && difference !== 0 ? `${differenceWords(difference)}` : null;
  const printed = reference.inches && shownValue(finding.architect) !== '—' ? `${referenceWord(finding)} ${shownValue(finding.architect)}` : null;
  if (finding.status === 'same' && vendor.inches && reference.inches && vendor.text === reference.text) {
    return { vendor, verb: 'both', reference: null, printed: null, difference: null };
  }
  // The brand prototype is not the design: a row never says the vendor "needs" it (#1530 review).
  const needs = finding.status === 'different' && finding.source !== 'brand_prototype';
  const verb = needs ? 'needs' : `${referenceWord(finding)} ${finding.source === 'rules' ? 'says' : finding.source === 'vendor_arithmetic' ? 'add to' : 'shows'}`;
  return { vendor, verb, reference, printed: needs ? printed : null, difference: differenceText };
}

/** The item without its group's area ("Buffet lower outlet height" under "Buffet" → "Lower outlet height"). */
export function itemTitle(item: string, area: string | null): string {
  let title = item.trim();
  if (area && title.toLowerCase().startsWith(`${area.toLowerCase()} `)) title = title.slice(area.length + 1);
  return title ? title.charAt(0).toUpperCase() + title.slice(1) : item;
}

/** The plain-words card of one finding (owner, 2026-10-17: "from the reviewer's perspective"). */
export interface ReviewerWords {
  /** "Architect shows" / "Prototype shows" / "GV’s rule says". */
  referenceLabel: string;
  reference: string;
  /** "Vendor drew", or "This view shows" for two views of the same drawings. */
  vendorLabel: string;
  vendor: string;
  /** "Why it’s wrong" / "What you need to decide" / "Why it’s right" / "Why it was not compared". */
  whyLabel: string;
  why: string;
  /** What to tell the vendor (Wrong only). */
  tell: string | null;
  /** How sure, in one plain line (no check names). */
  sure: string;
}

function sideWords(finding: Pick<ChatFinding, 'architect' | 'vendor' | 'inches'>, side: 'architect' | 'vendor'): string {
  const inches = inchNumber(finding.inches?.[side]);
  const printed = fullValue(finding[side]);
  if (inches === null) return printed;
  return printed === 'Nothing printed' ? `${inchWords(inches)} in` : `${inchWords(inches)} in (printed ${printed})`;
}

export function reviewerWords(original: ChatFinding): ReviewerWords {
  const finding = once(original);
  const architect = inchNumber(finding.inches?.architect);
  const vendor = inchNumber(finding.inches?.vendor);
  const difference = inchNumber(finding.inches?.difference);
  const unseen = finding.status === 'same' && finding.compared === false;
  const status = unseen ? 'not_compared' : finding.status;
  const whom = finding.source === 'brand_prototype' ? 'brand prototype' : finding.source === 'rules' ? 'GV’s rule' : 'architect';
  const fallback = status === 'different' && difference !== null && architect !== null && vendor !== null
    ? `The vendor’s ${inchWords(vendor)} in is ${inchWords(Math.abs(difference))} in ${difference > 0 ? 'more' : 'less'} than the ${whom}’s ${inchWords(architect)} in.`
    : status === 'different' ? `The vendor’s drawing does not match the ${whom}.`
    : status === 'check' ? 'The AI could not settle this on its own: look at the two pictures and decide.'
    : status === 'not_compared' ? `${finding.not_compared_why === 'pair_not_covered' ? 'Its page pair was not compared' : 'Both sides were not looked at'}, so this is not counted as right.`
    : `The vendor’s drawing matches the ${whom}.`;
  const tell = status !== 'different' ? null
    : finding.source === 'brand_prototype' ? `Confirm which governs: the brand prototype shows ${architect !== null ? `${inchWords(architect)} in` : fullValue(finding.architect)}, the vendor drew ${vendor !== null ? `${inchWords(vendor)} in` : fullValue(finding.vendor)}.`
    : architect !== null ? `Change it to ${inchWords(architect)} in to match the ${whom}${vendor !== null ? ` (now ${inchWords(vendor)} in)` : ''}.`
    : finding.source === 'rules' ? `Fix it to meet GV’s rule: ${fullValue(finding.architect)}.`
    : `Match the ${whom}: ${fullValue(finding.architect)}.`;
  return {
    referenceLabel: referenceLabel(finding),
    reference: sideWords(finding, 'architect'),
    vendorLabel: finding.source === 'cross_view' ? 'This view shows' : 'Vendor drew',
    vendor: sideWords(finding, 'vendor'),
    whyLabel: { different: 'Why it’s wrong', check: 'What you need to decide', same: 'Why it’s right', not_compared: 'Why it was not compared' }[status],
    why: finding.why?.trim() || fallback,
    tell,
    sure: sureWords(finding),
  };
}

/** How sure, in one plain line: what backs it, never a check's name. */
export function sureWords(finding: Pick<ChatFinding, 'trust_level' | 'checks'>): string {
  const checks = finding.checks;
  // Only a check that said "no_text" may say pen strokes: most stroke numbers will get a witness.
  const strokes = checks?.printed_architect === 'no_text' || checks?.printed_vendor === 'no_text';
  if (finding.trust_level === 'confirmed') return 'Code found the values printed on the drawing (or worked them out), and an AI reading agrees.';
  if (finding.trust_level === 'likely') return 'Two separate AI readings agree; no code check could read it.';
  if (checks?.second_read === 'disagrees' || checks?.critic === 'objects') return 'A second AI look disagreed, so check the pictures yourself.';
  if (strokes) return 'The numbers are drawn as pen strokes, so no independent check could read them.';
  if (!finding.trust_level) return 'The checks have not run yet.';
  return 'One AI reading backs this; nothing independent confirmed it yet.';
}

/**
 * Where the spot is inside a crop, as CSS insets in %: crops are the box padded by 24 points on
 * every side (`chat_drawing.crop_png`). Null when there is no box, or the picture's shape shows the
 * crop was cut by the page edge (then a drawn box would sit in the wrong place).
 */
export function spotInsets(
  box: { x0: number; y0: number; x1: number; y1: number } | null | undefined,
  picture?: { width: number; height: number } | null,
): { left: string; top: string; right: string; bottom: string } | null {
  if (!box) return null;
  const width = box.x1 - box.x0 + 2 * CROP_PAD;
  const height = box.y1 - box.y0 + 2 * CROP_PAD;
  if (!(width > 0 && height > 0)) return null;
  if (picture && picture.width > 0 && picture.height > 0) {
    const ratio = picture.width / picture.height;
    if (Math.abs(ratio - width / height) / (width / height) > 0.08) return null;
  }
  const x = `${((CROP_PAD / width) * 100).toFixed(2)}%`;
  const y = `${((CROP_PAD / height) * 100).toFixed(2)}%`;
  return { left: x, top: y, right: x, bottom: y };
}

const CROP_PAD = 24;

// ── Narrow answers (#1522) ──────────────────────────────────

/** At most this many pages touched makes an answer narrow (until the server sends `scope`). */
export const NARROW_PAGES = 3;

/**
 * True for an answer about a few pages ("Do the fillers on page 8 meet the rule?"): it shows its
 * words, its findings and one quiet confidence line, never the whole-set panels (page grid,
 * coverage, GV matches, ledger). The server's `scope` (#1528) decides; an older answer without
 * one is narrow when it is not a full check and touches at most three pages.
 */
export function isNarrowAnswer(message: Pick<ViewMessage, 'findings' | 'coverage'> & { scope?: ViewMessage['scope'] }, fullCheck: boolean): boolean {
  if (fullCheck) return false;
  // #1528: the server's own scope decides when it sent one.
  if (message.scope?.kind) return message.scope.kind !== 'whole_set';
  const pages = new Set<number>();
  for (const finding of message.findings ?? []) {
    pages.add(finding.page);
    if (finding.vs_page != null) pages.add(finding.vs_page);
  }
  for (const page of message.coverage?.pages ?? []) if (page.status !== 'not_opened') pages.add(page.page);
  return pages.size <= NARROW_PAGES;
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

/** "Also on p.9, p.12": the other pages of the same item, folded into this one (#1554). */
export function alsoOnWords(finding: Pick<ChatFinding, 'also_on'>): string {
  const pages = [...new Set((finding.also_on ?? []).map((other) => other.page).filter((page): page is number => typeof page === 'number'))];
  return pages.length ? `Also on ${pages.map((page) => `p.${page}`).join(', ')}` : '';
}
