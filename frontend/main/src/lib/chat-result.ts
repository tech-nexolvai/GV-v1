/**
 * A finished full check's result screen (#1717, UI redesign step 2; results v2 2026-10-23, built on
 * top of it): summary first, each number in one place, readable evidence. Kept free of React so it
 * is tested on its own.
 *
 * - The work line says what the check did and how long it took (the only place for the time).
 * - The review result speaks AWI's words; its top line says, plainly, how many proven mistakes the
 *   vendor fixes and how many pages were checked against the architect (and how many have no
 *   architect drawing in the upload).
 * - Six tiles are the tabs and the only place for the counts: Wrong · Likely problem · Ask the
 *   architect · Drawing needed · Right · Not compared (glyph + word + colour, never amber).
 * - A row says where (area, page), what was measured, and "Vendor 7″ → Should be 12″"; the list is
 *   "Biggest first" or "By page".
 */

import type { ChatCoverage, ChatFinding, ChatFile, ChatPackageState, ChatReviewAction, ChatTimings, FindingDecision, ProofGroup } from '@/lib/chat-api';
import { isInternal, proofGroup } from '@/lib/chat-proof';
import { inchNumber, inchWords, PHASE_WORDS } from '@/lib/chat-trust';

export type ResultTab = 'wrong' | 'likely_problem' | 'rule_note' | 'drawing_needed' | 'right' | 'not_compared';

/** The tiles, in the owner's order (2026-10-23): what the vendor fixes first, what is fine last. */
export const RESULT_TABS: readonly ResultTab[] = ['wrong', 'likely_problem', 'rule_note', 'drawing_needed', 'right', 'not_compared'];

export const TAB_WORDS: Record<ResultTab, { word: string; hint: string }> = {
  wrong: { word: 'Wrong', hint: 'Proven. Red on the cloud drawing' },
  // 2026-10-23 (#1898): found by the whole-drawing look, not proven by code: dashed, never solid red.
  likely_problem: { word: 'Likely problem', hint: 'Found, not proven. Vendor asked to confirm' },
  // #1839: the owner's words; decided, never red, nothing asked of the vendor.
  rule_note: { word: 'Ask the architect', hint: 'The architect’s own size needs a yes' },
  // #1885: a vendor page with no architect drawing in the upload: GV is asked for it.
  drawing_needed: { word: 'Drawing needed', hint: 'Not in the upload. Ask GV' },
  right: { word: 'Right', hint: 'Match the architect or GV’s rules' },
  not_compared: { word: 'Not compared', hint: 'Each with its reason' },
};

/**
 * A tile's caption with its unit where two counts could be read against each other (2026-10-23):
 * Drawing needed counts pages, Not compared counts items, as the top line says pages.
 */
export function tileHint(tab: ResultTab, count: number): string {
  if (tab === 'drawing_needed') return count === 1 ? '1 page not in the upload. Ask GV' : `${count} pages not in the upload. Ask GV`;
  if (tab === 'not_compared') return count === 1 ? '1 item, with its reason' : `${count} items, each with its reason`;
  return TAB_WORDS[tab].hint;
}

/** The tiles shown: all six, always in the same place (a zero is shown faded). */
export function resultTabs(): ResultTab[] {
  return [...RESULT_TABS];
}

export interface ResultGroups {
  /** Proved by code (clouded), biggest first. */
  wrong: ChatFinding[];
  /** 2026-10-23 (#1898): the whole-drawing look's design differences code could not prove. */
  likely: ChatFinding[];
  right: ChatFinding[];
  /** #1839: the architect's size breaks a GV rule and the vendor follows it: decided, GV asks the architect. */
  ruleNotes: ChatFinding[];
  /** Not compared, in the order of their reasons (see `notComparedGroups`). */
  notCompared: ChatFinding[];
  /** The reviewer's "Not a problem" (an optional override): listed under Wrong, apart, not counted. */
  dismissed: ChatFinding[];
}

const impactOf = (finding: ChatFinding) => finding.impact?.rank ?? Number.MAX_SAFE_INTEGER;
const biggestFirst = (a: ChatFinding, b: ChatFinding) => impactOf(a) - impactOf(b) || (b.impact?.score ?? 0) - (a.impact?.score ?? 0) || a.page - b.page;

/** The findings by where the cloud drawing puts them, with the reviewer's newest click laid over. */
export function resultGroups(findings: readonly ChatFinding[], decisionOf: (finding: ChatFinding) => FindingDecision | null = (finding) => finding.decision ?? null): ResultGroups {
  const groups: ResultGroups = { wrong: [], likely: [], right: [], ruleNotes: [], notCompared: [], dismissed: [] };
  for (const finding of findings) {
    const group = proofGroup(finding, decisionOf(finding));
    if (group === 'clouded') groups.wrong.push(finding);
    else if (group === 'likely_problem') groups.likely.push(finding);
    else if (group === 'right') groups.right.push(finding);
    else if (group === 'dismissed') groups.dismissed.push(finding);
    else if (group === 'rule_note') groups.ruleNotes.push(finding);
    else groups.notCompared.push(finding);
  }
  groups.wrong.sort(biggestFirst);
  groups.likely.sort(biggestFirst);
  groups.right.sort((a, b) => a.page - b.page);
  groups.ruleNotes.sort((a, b) => a.page - b.page);
  groups.notCompared = notComparedGroups(groups.notCompared).flatMap((group) => group.findings);
  return groups;
}

// ── Drawing needed (#1885) and the pages ───────────────────

/** A vendor page with no architect drawing in the upload (the server's `drawing_needed`). */
export interface DrawingNeeded {
  page: number;
  /** Why, in plain words ("no drawing of the same item was found in the architect's file"). */
  why: string | null;
  /** One item, one card: what readers reported on this page that only says the same, kept as evidence. */
  folded?: { finding_id: string; item?: string | null; vendor?: string | null }[];
}

/** The vendor pages the server says have no architect drawing in this upload, in page order. */
export function drawingNeededPages(coverage: ChatCoverage | null | undefined): DrawingNeeded[] {
  return (coverage?.pages ?? [])
    .filter((page) => page.drawing_needed === true)
    .map((page) => ({ page: page.page, why: page.drawing_needed_why?.trim() || null, ...(page.folded?.length ? { folded: [...page.folded] } : {}) }))
    .sort((a, b) => a.page - b.page);
}

/** What the set pack names a vendor page (`GET /packages/{id}/vendor-pages`). */
export interface VendorPageTitle {
  page: number;
  title?: string | null;
  project?: string | null;
  sheet?: string | null;
}

export type PageTitles = ReadonlyMap<number, VendorPageTitle>;

/** A "By page" heading: "Page 27 · Retail unit · Sample project", from the set pack, else the page's own items. */
export interface PageHeading {
  page: number;
  title: string | null;
  project: string | null;
}

export function pageHeading(page: number, titles: PageTitles | null | undefined, findings: readonly ChatFinding[] = []): PageHeading {
  const known = titles?.get(page);
  const fromItems = findings.map((finding) => itemParts(finding.item).area).find((area): area is string => Boolean(area)) ?? null;
  return { page, title: areaWords(known?.title) ?? fromItems, project: known?.project?.trim() || null };
}

/** The heading's words, as one line ("Page 27 · Retail unit · Sample project"). */
export function pageHeadingWords(heading: PageHeading): string {
  return [`Page ${heading.page}`, heading.title, heading.project].filter(Boolean).join(' · ');
}

/** Id prefix of a Drawing-needed item: the drawer opens it like a finding, but it is a page. */
export const DRAWING_NEEDED_ID = 'drawing-needed:';

export function isDrawingNeededItem(finding: Pick<ChatFinding, 'id'>): boolean {
  return finding.id.startsWith(DRAWING_NEEDED_ID);
}

/**
 * Each Drawing-needed page as an item the list and the drawer show like any other: no values, the
 * page's title, and why. Never sent to the server (no decision is ever saved on one).
 */
export function drawingNeededItems(needed: readonly DrawingNeeded[], titles: PageTitles | null | undefined): ChatFinding[] {
  return needed.map(({ page, why, folded }) => {
    const heading = pageHeading(page, titles);
    return {
      id: `${DRAWING_NEEDED_ID}${page}`,
      page,
      item: heading.title ? `${heading.title}: no architect drawing in this upload` : 'No architect drawing in this upload',
      category: 'other',
      architect: null,
      vendor: null,
      status: 'check',
      why,
      verified: false,
      source: 'architect',
      proof: { group: 'not_compared', reason: why, settle: 'the architect’s drawing of this item' },
      ...(folded?.length ? { same_item: { copies: folded.map((copy) => ({ ...copy, group: 'not_compared' })) } } : {}),
    } satisfies ChatFinding;
  });
}

/** Items grouped under their vendor page, in page order (the list's "By page"). */
export function byPage(findings: readonly ChatFinding[]): { page: number; findings: ChatFinding[] }[] {
  const pages = new Map<number, ChatFinding[]>();
  for (const finding of findings) pages.set(finding.page, [...(pages.get(finding.page) ?? []), finding]);
  return [...pages.entries()].sort(([a], [b]) => a - b).map(([page, list]) => ({ page, findings: list }));
}

// ── The result's top line ───────────────────────────────────

export interface PageCounts {
  /** The vendor's pages. */
  total: number;
  /** Compared with the architect (same or different). */
  compared: number;
  /** No architect drawing in this upload. */
  needed: number;
  /** The rest: not compared with the architect for another reason (title pages, not reached). */
  other: number;
}

export function pageCounts(coverage: ChatCoverage | null | undefined): PageCounts | null {
  const pages = coverage?.pages ?? [];
  if (!pages.length) return null;
  const compared = pages.filter((page) => !page.drawing_needed && (page.status === 'same' || page.status === 'different')).length;
  const needed = pages.filter((page) => page.drawing_needed === true).length;
  return { total: pages.length, compared, needed, other: pages.length - compared - needed };
}

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`;

/**
 * The result card's top line, plainly (2026-10-23): what the vendor fixes, and how much was checked
 * against the architect. "3 proven mistakes for the vendor to fix. 54 of 99 pages checked against the
 * architect; 45 have no architect drawing in this upload." Each number is the tiles' or the pages'.
 */
export function headline(input: { wrong: number; likely: number; pages: PageCounts | null }): string {
  const { wrong, likely, pages } = input;
  const mistakes = wrong > 0 ? `${plural(wrong, 'proven mistake', 'proven mistakes')} for the vendor to fix` : 'No proven mistakes';
  const confirm = likely > 0 ? `${wrong > 0 ? ', and ' : '; '}${plural(likely, 'likely problem', 'likely problems')} for the vendor to confirm` : '';
  const first = `${mistakes}${confirm}.`;
  if (!pages) return first;
  const parts = [`${pages.compared} of ${pages.total} pages checked against the architect`];
  if (pages.needed > 0) parts.push(`${plural(pages.needed, 'page has', 'pages have')} no architect drawing in this upload`);
  if (pages.other > 0) parts.push(`${plural(pages.other, 'page', 'pages')} not compared`);
  return `${first} ${parts.join('; ')}.`;
}

/**
 * Whether an unfinished check ("Check incomplete") is unfinished only because architect drawings are
 * missing from the upload (#1885): running it again cannot fix that, so no "Run the check again";
 * the next step is asking GV for the drawings.
 */
export function missingDrawingsOnly(action: ChatReviewAction | null | undefined, needed: number): boolean {
  if (!action || action.action !== 'not_for_sending' || needed <= 0) return false;
  const reason = action.reason.toLowerCase();
  if (!/no architect drawing/.test(reason)) return false;
  return !/(?:was|were) not compared|not reached|a step did not finish|no page's comparison|stopped|fixed price|price limit|step limit|restarted|problem/.test(reason.replace(/^the check did not finish:\s*/, ''));
}

// ── The drawings request (draft only: Anant sends it himself) ───

/** "Vendor page 27 · Retail unit · Sample project (sheet SD-27)": one line of the request. */
export function requestLine(page: number, titles: PageTitles | null | undefined, findings: readonly ChatFinding[] = []): string {
  const heading = pageHeading(page, titles, findings);
  const sheet = titles?.get(page)?.sheet?.trim();
  return `Vendor page ${page}${heading.title ? ` · ${heading.title}` : ''}${heading.project ? ` · ${heading.project}` : ''}${sheet ? ` (sheet ${sheet})` : ''}`;
}

/**
 * The ready-to-edit request to GV for the architect's drawings the upload lacks: the items and their
 * vendor pages. A draft only: nothing is sent from the screen.
 */
export function drawingsRequestText(pages: readonly number[], titles: PageTitles | null | undefined, findings: readonly ChatFinding[] = []): string {
  const lines = pages.map((page) => `- ${requestLine(page, titles, findings.filter((finding) => finding.page === page))}`);
  return [
    'Subject: Architect’s drawings needed to finish the shop drawing review',
    '',
    'Hello,',
    '',
    `To finish checking the vendor’s shop drawings against the design, we need the architect’s drawings of ${pages.length === 1 ? 'this item' : `these ${pages.length} items`}. ${pages.length === 1 ? 'It is' : 'They are'} not in the upload we received:`,
    '',
    ...lines,
    '',
    'Could you send them, or confirm that the architect’s file we have is for the same project?',
    '',
    'Thank you',
  ].join('\n');
}

// ── Not compared, by its reason ────────────────────────────

/** The reasons in the Not-compared list's order, as its headings say them (`classification.REASON_WORDS`). */
const REASONS: readonly [string, string][] = [
  ['field_verify', 'The drawing says verify in field'],
  ['nothing_to_compare', 'Architect gives no size or information'],
  ['no_overall', 'Architect gives only the parts, no overall'],
  ['string_not_verified', 'Vendor’s string not verified on the drawing'],
  ['different_reference', 'Not directly comparable (different reference points)'],
  ['gv_sticker', 'GV’s sticker covers the vendor’s value'],
  ['unreadable', 'Vendor’s value could not be read'],
  ['stroke_words', 'Words drawn as strokes (not read yet)'],
  ['no_spot', 'The value has no spot on the drawing'],
  ['tags_differ', 'Tags differ: confirm the vendor’s code'],
  ['tag_unconfirmed', 'Tag not confirmed at its spot'],
  ['words', 'A note in words, not a size'],
  ['architect_disagrees', 'Architect’s drawings disagree'],
  ['missing_setting', 'A job value or GV setting is missing'],
  ['not_proved', 'Difference not proved by code'],
  ['not_seen', 'Not looked at on both drawings'],
  ['finish_not_comparable', 'Finish codes not comparable'],
  ['no_sheet', 'No sheet of the area or round to compare'],
  ['round_only', 'Compared only with the vendor’s earlier round, not the architect'],
  ['prototype', 'Brand prototype differs (reference only)'],
  ['prototype_only', 'Matches the brand prototype only (reference only)'],
  ['internal', 'For GV only: read by the AI, never found at its spot'],
];
const REASON_ORDER = new Map(REASONS.map(([reason], at) => [reason, at]));
const REASON_WORDS = new Map(REASONS);

/** A finding's Not-compared reason: the server's, else worked out from what is known. */
export function notComparedReason(finding: ChatFinding): string {
  const reason = finding.proof?.not_compared;
  if (reason && REASON_WORDS.has(reason)) return reason;
  if (isInternal(finding)) return 'internal';
  if (finding.compared === false || finding.not_compared_why) return 'not_seen';
  return finding.status === 'different' ? 'not_proved' : 'other';
}

export interface NotComparedGroup {
  reason: string;
  words: string;
  findings: ChatFinding[];
}

export function notComparedGroups(findings: readonly ChatFinding[]): NotComparedGroup[] {
  const byReason = new Map<string, ChatFinding[]>();
  for (const finding of findings) {
    const reason = notComparedReason(finding);
    byReason.set(reason, [...(byReason.get(reason) ?? []), finding]);
  }
  return [...byReason.entries()]
    .sort(([a], [b]) => (REASON_ORDER.get(a) ?? 99) - (REASON_ORDER.get(b) ?? 99))
    .map(([reason, list]) => ({ reason, words: REASON_WORDS.get(reason) ?? 'Not compared', findings: list.sort((x, y) => x.page - y.page) }));
}

/** What is needed to compare it: the server's words, never empty. */
export function neededWords(finding: ChatFinding): string {
  return finding.proof?.settle?.trim() || (notComparedReason(finding) === 'unreadable' ? 'a legible value on the drawing, or the next round' : 'more information on the drawings');
}

// ── A row's words ───────────────────────────────────────────

const quoteMarks = (text: string) => text.replace(/"/g, '″').replace(/'/g, '′');

/**
 * An area as the reviewer reads it: the drawing list's own numbering left off ("2.Standard
 * Kitchenette" → "Standard Kitchenette", "B. Bar" → "Bar"). A name that only starts like one
 * ("B.O.H. corridor", "3.5 m wall") is kept as it is.
 */
export function areaWords(area: string | null | undefined): string | null {
  const words = (area ?? '').replace(/^\s*(?:\d{1,3}|[A-Za-z])[.)](?:\s+|(?=[A-Z][a-z]))/, '').trim();
  return words || null;
}

/** "Standard Kitchenette with cooktop: the left wall to outlet 1…" → area + what was measured. */
export function itemParts(item: string): { area: string | null; thing: string } {
  const at = item.indexOf(': ');
  const area = at > 0 ? areaWords(item.slice(0, at)) : null;
  const rest = (at > 0 ? item.slice(at + 2) : item).trim();
  return { area, thing: quoteMarks(rest.charAt(0).toUpperCase() + rest.slice(1)) };
}

export interface ValueWords {
  /** As the screen says it ("7″"), with the printed form when it differs ("177 mm"). */
  main: string;
  note: string | null;
}

/** The vendor's value: inches first; a "177 [7]" print says its millimetres apart. */
export function vendorValue(finding: Pick<ChatFinding, 'vendor'>): ValueWords {
  const printed = String(finding.vendor ?? '').trim();
  const pair = /^(\d+(?:\.\d+)?)\s*\[([^\]]+)\]$/.exec(printed);
  if (pair) return { main: `${quoteMarks(pair[2].replace(/"$/, ''))}″`, note: `${pair[1]} mm` };
  return { main: quoteMarks(printed), note: null };
}

/**
 * A vendor string code did not compare (Not compared, e.g. "code could not find the overall's whole
 * dimension line"): its parts and overall are the AI's unproven reading, so the screen never shows
 * them as "vendor vs …" values (the client meeting, 2026-10-23). A proven string Wrong is not this.
 */
export function isRefusedString(finding: Pick<ChatFinding, 'source' | 'parts' | 'arch_picture_id'>, group: ProofGroup | null | undefined): boolean {
  if (group !== 'not_compared' || finding.arch_picture_id) return false;
  return finding.source === 'vendor_arithmetic' || Boolean(finding.parts?.length);
}

/**
 * A long value said short for a header ("No overall printed"), with the whole value apart to show
 * below it ("no overall printed (partial dims 1′-3″, …)"); a short one is itself, with nothing apart.
 */
export function shortValue(value: string | null | undefined, most = 20): { short: string; full: string | null } {
  // Inch and foot marks only after a number: "the architect's" keeps its apostrophe.
  const words = String(value ?? '').replace(/\s+/g, ' ').trim().replace(/"/g, '″').replace(/(\d)'/g, '$1′');
  if (!words) return { short: '—', full: null };
  if (words.length <= most) return { short: words, full: null };
  const head = words.split(/\s*[(;:]\s*|,\s+|\s+[—–-]\s+/)[0].trim();
  const upper = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);
  if (head.length >= 2 && head.length <= most + 2) return { short: upper(head), full: words };
  let cut = '';
  for (const word of head.split(' ')) {
    if (`${cut} ${word}`.trim().length > most) break;
    cut = `${cut} ${word}`.trim();
  }
  // Never end on a joining word ("QZ-2 quartz top and…" reads as cut mid-thought).
  const trimmed = cut.replace(/(?:\s+(?:and|or|of|the|a|an|to|with|for|in|on|at))+$/i, '');
  return { short: `${upper(trimmed || head.slice(0, most)).replace(/[,.;:]$/, '')}…`, full: words };
}

/** What it should be (green): the server's "12" (architect 1' - 0")" split into the value and where it comes from. */
export function shouldBeValue(finding: Pick<ChatFinding, 'proof' | 'architect'> & Partial<Pick<ChatFinding, 'inches' | 'decision'>>, decision: FindingDecision | null | undefined = finding.decision): ValueWords | null {
  // #1803: the value the reviewer gave with "It's a mistake: add a red mark", unless it is the one code knows.
  const given = decision?.decision === 'agree' ? decision.should_be?.trim() : '';
  if (given && sameValue(given) !== sameValue(suggestedShouldBe(finding))) return { main: quoteMarks(given), note: 'the reviewer' };
  const words = finding.proof?.should_be?.trim() || finding.architect?.trim() || '';
  if (!words) return null;
  const from = /^(.*?)\s*\((.*)\)\s*$/.exec(words);
  return from ? { main: quoteMarks(from[1]), note: quoteMarks(from[2]) } : { main: quoteMarks(words), note: null };
}

/** A value as compared with another: no spaces, one kind of inch and foot mark (`proof._same_value`). */
const sameValue = (text: string) => text.replace(/\s+/g, '').replace(/[″”]/g, '"').replace(/′/g, "'").toLowerCase();

/**
 * #1803: the value "It's a mistake: add a red mark" starts with: what code knows the vendor's value
 * should be (the architect's size, as code writes it: `37"`), else the architect's words when they
 * hold a number; "" when the architect gives none.
 */
export function suggestedShouldBe(finding: Pick<ChatFinding, 'proof' | 'architect'> & Partial<Pick<ChatFinding, 'inches'>>): string {
  const inches = inchNumber(finding.inches?.architect);
  if (inches !== null) return `${inchWords(inches)}"`;
  const proof = finding.proof?.should_be?.trim();
  if (proof && !/\(reviewer\)\s*$/.test(proof)) return proof.replace(/\s*\(.*\)\s*$/, '');
  const architect = finding.architect?.trim() ?? '';
  return /\d/.test(architect) && !/GV Review'?s record|recorded check/i.test(architect) ? architect : '';
}

/**
 * #1887: a vendor string whose wrong part code's geometry named (the overall is right): the part,
 * what it should be, how long its line is drawn and the overall, in the screen's words.
 */
export interface WrongPartWords {
  part: string;
  should: string;
  drawn: string | null;
  overall: string | null;
}

export function wrongPartWords(finding: Pick<ChatFinding, 'wrong_part' | 'source' | 'printed_total'>): WrongPartWords | null {
  const part = finding.wrong_part;
  if (finding.source !== 'vendor_arithmetic' || !part?.text) return null;
  const should = inchNumber(part.should_be);
  if (should === null || should <= 0) return null;
  const overall = part.overall?.trim() || finding.printed_total?.trim() || null;
  return { part: quoteMarks(part.text), should: `${inchWords(should)}″`, drawn: part.drawn?.trim() ? quoteMarks(part.drawn.trim()) : null, overall: overall ? quoteMarks(overall) : null };
}

/** "5″ short" / "2″ over": the vendor's value against what it should be, from code's numbers. */
export function differenceWords(finding: Pick<ChatFinding, 'inches'>): string | null {
  const difference = inchNumber(finding.inches?.difference);
  if (difference === null || difference === 0) return null;
  return `${inchWords(Math.abs(difference))}″ ${difference < 0 ? 'short' : 'over'}`;
}

/** The comment number the letter and the cloud drawing give a Wrong item (C-04), once the package is made. */
export function commentNumber(finding: Pick<ChatFinding, 'id'>, pkg: ChatPackageState | null | undefined): string | null {
  return pkg?.numbers?.[finding.id] ?? null;
}

// ── The review result ───────────────────────────────────────

export interface Verdict {
  /** AWI's words on the stamp ("Revise and resubmit"; "Check incomplete · not for sending"). */
  stamp: string;
  /** An unfinished check: a dashed stamp, a draft package. */
  draft: boolean;
  /**
   * Review needed (Anant 2026-10-22): the check finished, but too many items were Not compared to
   * approve it; a person decides them first. A dashed stamp with a person glyph, never an approval.
   */
  pending: boolean;
  /** One sentence: only what no count on the screen says. */
  sentence: string;
}

export function verdictOf(action: ChatReviewAction | null | undefined): Verdict | null {
  if (!action) return null;
  if (action.action === 'not_for_sending') {
    return { stamp: action.words.replace(/:\s*/, ' · '), draft: true, pending: false, sentence: `${action.reason.replace(/\.?$/, '.')} This package is a draft: do not send it yet.` };
  }
  if (action.action === 'review_needed') {
    // The screen's own next step; the vendor's files say only "Under review".
    return { stamp: action.words, draft: false, pending: true, sentence: `${action.reason.replace(/\.?$/, '.')} Decide them below; until then the package tells the vendor “${action.vendor_words || 'Under review'}”.` };
  }
  const fabrication = action.fabrication > 0
    ? `${action.fabrication} of the wrong values ${action.fabrication === 1 ? 'changes' : 'change'} fabrication.`
    : null;
  return { stamp: action.words, draft: false, pending: false, sentence: [fabrication, action.next_step].filter(Boolean).join(' ') };
}

// ── The work line ───────────────────────────────────────────

/** "4:31" (or "1:02:05"): a time on the screen, in Plex Mono. */
export function clockWords(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  const total = Math.round(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${rest}` : `${minutes}:${rest}`;
}


export interface WorkStep {
  words: string;
  time: string | null;
}

/** The check's steps that took time, in plain words, with their time ("Compared … · 4:05"). */
export function workSteps(timings: ChatTimings | null | undefined): WorkStep[] {
  const order = Object.keys(PHASE_WORDS);
  const at = (phase: string) => (order.includes(phase) ? order.indexOf(phase) : order.length);
  return (timings?.phases ?? [])
    .filter((phase) => phase.seconds >= 0.5 || phase.calls > 0)
    .sort((a, b) => at(a.phase) - at(b.phase))
    .map((phase) => ({ words: PHASE_WORDS[phase.phase] ?? phase.label, time: clockWords(phase.seconds) }));
}

export interface WorkLineInput {
  drawingsDone: number | null;
  drawingsTotal: number | null;
  pagesTotal: number | null;
  filesMade: number;
  seconds: number | null;
  stopped: boolean;
}

/**
 * "Looked at 39 drawings on 60 pages · 5 files made · 4:31" (the time is the line's last part). It
 * never says "checked": how many pages were checked against the architect is the result's own
 * line (audit 2026-10-23: "Checked 109 pages" beside "0 of 109 compared").
 */
export function workLine(input: WorkLineInput): { words: string; time: string | null } {
  const parts: string[] = [];
  const { drawingsDone, drawingsTotal, pagesTotal } = input;
  const pages = pagesTotal ? ` on ${pagesTotal} pages` : '';
  if (drawingsTotal) {
    const done = drawingsDone ?? drawingsTotal;
    parts.push(done < drawingsTotal ? `Looked at ${done} of ${drawingsTotal} drawings${pages}` : `Looked at ${drawingsTotal} drawings${pages}`);
  } else if (pagesTotal) {
    parts.push(`Looked at ${pagesTotal} pages`);
  } else {
    parts.push('Full check');
  }
  if (input.stopped) parts.push('stopped before the end');
  if (input.filesMade > 0) parts.push(`${input.filesMade} ${input.filesMade === 1 ? 'file' : 'files'} made`);
  return { words: parts.join(' · '), time: clockWords(input.seconds) };
}

/** The package's files as one line: "cloud drawing, letter, register, Not-compared list, report". */
export function filesLine(files: { cloud: ChatFile | null; report: ChatFile | null; notCompared: readonly ChatFile[]; pkg: ChatFile | null }): { count: number; words: string } {
  const names: string[] = [];
  if (files.cloud) names.push('cloud drawing');
  if (files.pkg) names.push('comment letter', 'issue register');
  if (files.notCompared.length) names.push('Not-compared list');
  if (files.report) names.push('check report');
  return { count: names.length, words: names.join(', ') };
}

/** The page the preview opens on: the biggest Wrong item's vendor page, else the first item's, else 1. */
export function previewPageOf(groups: ResultGroups): number {
  const first = groups.wrong[0] ?? groups.likely[0] ?? groups.ruleNotes[0] ?? groups.notCompared[0] ?? groups.right[0];
  return first ? first.vendor_box?.page ?? first.page : 1;
}

/** A box on the vendor's page picture (100 dpi, drawn from PDF points): left, top, width, height in px. */
export function boxOnPicture(box: { x0: number; y0: number; x1: number; y1: number } | null | undefined, dpi = 100): { left: number; top: number; width: number; height: number } | null {
  if (!box) return null;
  const px = (points: number) => (points * dpi) / 72;
  return { left: px(box.x0), top: px(box.y0), width: Math.max(1, px(box.x1 - box.x0)), height: Math.max(1, px(box.y1 - box.y0)) };
}

/** #1803: "1 decision", "3 decisions" ("Package updated with your 2 decisions"). */
export function decisionsWords(count: number): string {
  return `${count} ${count === 1 ? 'decision' : 'decisions'}`;
}
