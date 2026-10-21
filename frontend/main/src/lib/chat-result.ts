/**
 * A finished full check's result screen (#1717, UI redesign step 2): summary first, each number in
 * one place, readable evidence. Kept free of React so it is tested on its own.
 *
 * - The work line says what the check did and how long it took (the only place for the time).
 * - The review result speaks AWI's words; its one sentence says only what no count says.
 * - The tiles are the tabs and the only place for the counts: Wrong / Right / Not compared, and
 *   Ask the architect when a size the architect specifies breaks a GV rule (#1839: decided, never red).
 * - A row says where (area, page), what was measured, and "Vendor 7″ → Should be 12″".
 */

import type { ChatFinding, ChatFile, ChatPackageState, ChatReviewAction, ChatTimings, FindingDecision } from '@/lib/chat-api';
import { isInternal, proofGroup } from '@/lib/chat-proof';
import { inchNumber, inchWords } from '@/lib/chat-trust';

export type ResultTab = 'wrong' | 'right' | 'rule_note' | 'not_compared';

export const TAB_WORDS: Record<ResultTab, { word: string; hint: string }> = {
  wrong: { word: 'Wrong', hint: 'Red on the cloud drawing, with what it should be' },
  right: { word: 'Right', hint: 'Match the architect or GV’s rules' },
  // #1839: the owner's words; decided, never red, nothing asked of the vendor.
  rule_note: { word: 'Ask the architect', hint: 'Architect’s size breaks a GV rule; the vendor follows it' },
  not_compared: { word: 'Not compared', hint: 'Each with its reason and what is needed' },
};

/** The tiles shown: Ask the architect only when there is one (#1839), so most sets keep three. */
export function resultTabs(groups: Pick<ResultGroups, 'ruleNotes'>): ResultTab[] {
  return groups.ruleNotes.length ? ['wrong', 'right', 'rule_note', 'not_compared'] : ['wrong', 'right', 'not_compared'];
}

export interface ResultGroups {
  /** Proved by code (clouded), biggest first. */
  wrong: ChatFinding[];
  right: ChatFinding[];
  /** #1839: the architect's size breaks a GV rule and the vendor follows it: decided, GV asks the architect. */
  ruleNotes: ChatFinding[];
  /** Not compared, in the order of their reasons (see `notComparedGroups`). */
  notCompared: ChatFinding[];
  /** The reviewer's "Not a problem" (an optional override): listed under Wrong, apart, not counted. */
  dismissed: ChatFinding[];
}

const impactOf = (finding: ChatFinding) => finding.impact?.rank ?? Number.MAX_SAFE_INTEGER;

/** The findings by where the cloud drawing puts them, with the reviewer's newest click laid over. */
export function resultGroups(findings: readonly ChatFinding[], decisionOf: (finding: ChatFinding) => FindingDecision | null = (finding) => finding.decision ?? null): ResultGroups {
  const groups: ResultGroups = { wrong: [], right: [], ruleNotes: [], notCompared: [], dismissed: [] };
  for (const finding of findings) {
    const group = proofGroup(finding, decisionOf(finding));
    if (group === 'clouded') groups.wrong.push(finding);
    else if (group === 'right') groups.right.push(finding);
    else if (group === 'dismissed') groups.dismissed.push(finding);
    else if (group === 'rule_note') groups.ruleNotes.push(finding);
    else groups.notCompared.push(finding);
  }
  groups.wrong.sort((a, b) => impactOf(a) - impactOf(b) || (b.impact?.score ?? 0) - (a.impact?.score ?? 0));
  groups.right.sort((a, b) => a.page - b.page);
  groups.ruleNotes.sort((a, b) => a.page - b.page);
  groups.notCompared = notComparedGroups(groups.notCompared).flatMap((group) => group.findings);
  return groups;
}

// ── Not compared, by its reason ────────────────────────────

/** The reasons in the Not-compared list's order, as its headings say them (`classification.REASON_WORDS`). */
const REASONS: readonly [string, string][] = [
  ['field_verify', 'The drawing says verify in field'],
  ['nothing_to_compare', 'Architect gives no size or information'],
  ['no_overall', 'Architect gives only the parts, no overall'],
  ['string_not_verified', 'Vendor’s string not verified on the drawing'],
  ['different_reference', 'Not directly comparable (different reference points)'],
  ['unreadable', 'Vendor’s value could not be read'],
  ['no_spot', 'The value has no spot on the drawing'],
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
  /** One sentence: only what no count on the screen says. */
  sentence: string;
}

export function verdictOf(action: ChatReviewAction | null | undefined): Verdict | null {
  if (!action) return null;
  if (action.action === 'not_for_sending') {
    return { stamp: action.words.replace(/:\s*/, ' · '), draft: true, sentence: `${action.reason.replace(/\.?$/, '.')} This package is a draft: do not send it yet.` };
  }
  const fabrication = action.fabrication > 0
    ? `${action.fabrication} of the wrong values ${action.fabrication === 1 ? 'changes' : 'change'} fabrication.`
    : null;
  return { stamp: action.words, draft: false, sentence: [fabrication, action.next_step].filter(Boolean).join(' ') };
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

const PHASE_WORDS: Record<string, string> = {
  set_pack: 'Read the drawings',
  seeding: 'GV Review’s own checks',
  planning: 'Planned the work',
  compare: 'Compared each vendor drawing with the architect’s',
  finish_checks: 'Checked by code',
  advocate: 'Second look at each difference',
  not_compared: 'Made the Not-compared list',
  cloud_drawing: 'Made the cloud drawing',
  report: 'Made the check report',
  package: 'Made the package',
};

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

/** "Checked 39 drawings on 60 pages · 5 files made · 4:31" (the time is the line's last part). */
export function workLine(input: WorkLineInput): { words: string; time: string | null } {
  const parts: string[] = [];
  const { drawingsDone, drawingsTotal, pagesTotal } = input;
  const pages = pagesTotal ? ` on ${pagesTotal} pages` : '';
  if (drawingsTotal) {
    const done = drawingsDone ?? drawingsTotal;
    parts.push(done < drawingsTotal ? `Compared ${done} of ${drawingsTotal} drawings${pages}` : `Checked ${drawingsTotal} drawings${pages}`);
  } else if (pagesTotal) {
    parts.push(`Checked ${pagesTotal} pages`);
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
  const first = groups.wrong[0] ?? groups.ruleNotes[0] ?? groups.notCompared[0] ?? groups.right[0];
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
