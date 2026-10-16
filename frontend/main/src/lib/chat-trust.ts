/**
 * Chat v3 (CONTRACT3-trust, #1483): why each finding can be believed, in code's words.
 *
 * Plain Claude's answer is one model's reading in prose; nothing else checks it. Here every finding
 * carries the checks code ran (printed at the spot, a second independent read, the lead's own check,
 * a GV rule or the vendor's arithmetic) and a trust level code worked out from them. This file turns
 * those fields into the screen's short, factual words and counts, and into the CSV export. It never
 * does arithmetic on the AI's numbers: the inches and the difference are code's (`finding.inches`).
 */

import type { ChatCoverage, ChatFinding, ChatTrust, ChatUnknown, EvidenceKind, FindingCategory, PrintedResult, RoundStatus, TrustLevel, UnknownWhy } from '@/lib/chat-api';
import { categoryOf, CATEGORY_WORDS, pageWords, sourceTag, STATUS_WORDS, verifiedWords } from '@/lib/chat-findings';

// ── Levels ──────────────────────────────────────────────────

export const TRUST_ORDER: readonly TrustLevel[] = ['confirmed', 'likely', 'to_check'];

export const TRUST_WORDS: Record<TrustLevel, string> = {
  confirmed: 'Confirmed',
  likely: 'Likely',
  to_check: 'To check',
};

/** What each level means, for its chip's tooltip. */
export const TRUST_MEANING: Record<TrustLevel, string> = {
  confirmed: 'A check that is not AI backs it (printed on the drawing, or worked out by code) and an AI reading agrees',
  likely: 'Two AI readings agree, or a non-AI check backs it alone',
  to_check: 'Fewer checks passed, or one disagreed: a person should look',
};

// ── Evidence kind ───────────────────────────────────────────

/** What backs a finding, in the reviewer's words. */
export const EVIDENCE_WORDS: Record<EvidenceKind, string> = {
  observed: 'printed on the drawing',
  derived: 'worked out by code',
  inferred: 'AI reading only',
};

const WITNESS_WORDS: Record<string, string> = {
  printed: 'both values printed at their spots',
  rule: 'GV’s rule check agrees',
  arithmetic: 'code added up the dimension string',
  ledger: 'code’s reading ledger backs it',
  units: 'the mm and inches printed together agree',
};

/** A finding's evidence kind, read safely; null on older answers. */
export function evidenceKindOf(finding: Pick<ChatFinding, 'evidence_kind'>): EvidenceKind | null {
  const kind = finding.evidence_kind;
  return kind && kind in EVIDENCE_WORDS ? kind : null;
}

/** "Worked out by code: GV’s rule check agrees", "AI reading only"; null on older answers. */
export function evidenceWords(finding: Pick<ChatFinding, 'evidence_kind' | 'witnesses'>): string | null {
  const kind = evidenceKindOf(finding);
  if (!kind) return null;
  const words = EVIDENCE_WORDS[kind];
  const found = (finding.witnesses ?? []).map((each) => WITNESS_WORDS[each]).filter(Boolean);
  const head = words[0].toUpperCase() + words.slice(1);
  return found.length ? `${head}: ${found.join('; ')}` : head;
}

/** A "What's right" item that was not compared (not seen on both sides, or its page pair not covered). */
export function notCompared(finding: Pick<ChatFinding, 'compared'>): boolean {
  return finding.compared === false;
}

export const NOT_COMPARED_WORDS: Record<'not_viewed' | 'pair_not_covered', string> = {
  not_viewed: 'both sides were not looked at',
  pair_not_covered: 'its page pair was not compared',
};

// ── Unknowns ────────────────────────────────────────────────

export const UNKNOWN_WHY_WORDS: Record<UnknownWhy, string> = {
  unreadable: 'unreadable (pen strokes)',
  not_opened: 'not opened',
  missing_job_value: 'job value missing',
  pairing_uncertain: 'page pair unsure',
  refused_alignment: 'rounds would not line up',
};

/** "3 not checked", or null when there are none. */
export function unknownsWords(unknowns: readonly ChatUnknown[] | null | undefined): string | null {
  const count = unknowns?.length ?? 0;
  return count ? `${count} not checked` : null;
}

/** A finding's trust level, read safely; null when the checks have not given one yet. */
export function trustLevelOf(finding: Pick<ChatFinding, 'trust_level'>): TrustLevel | null {
  const level = finding.trust_level;
  return level && TRUST_ORDER.includes(level) ? level : null;
}

export type LevelCounts = Record<TrustLevel, number>;

/**
 * Findings by trust level: the report's own counts when code sent them, else counted from the
 * findings that carry a level. Null when nothing has a level yet (the checks have not run).
 */
export function levelCounts(findings: readonly ChatFinding[], trust: ChatTrust | null | undefined): LevelCounts | null {
  if (trust?.levels) return trust.levels;
  const counts: LevelCounts = { confirmed: 0, likely: 0, to_check: 0 };
  let any = false;
  for (const finding of findings) {
    const level = trustLevelOf(finding);
    if (!level || notCompared(finding)) continue;
    counts[level] += 1;
    any = true;
  }
  return any ? counts : null;
}

// ── Code's numbers ──────────────────────────────────────────

const SIXTEENTHS = 16;

function gcd(a: number, b: number): number {
  return b === 0 ? a : gcd(b, a % b);
}

/** Code's decimal string (or number) as a number; null when it is not one. */
export function inchNumber(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === '') return null;
  const number = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(number) ? number : null;
}

/**
 * Inches as a reviewer writes them: "37", "25 1/4", "1/2"; a value that is not a whole number of
 * sixteenths keeps its decimals ("12.345"). No unit; the caller adds "in".
 */
export function inchWords(value: number): string {
  if (!Number.isFinite(value)) return '';
  const sign = value < 0 ? '−' : '';
  const size = Math.abs(value);
  const sixteenths = size * SIXTEENTHS;
  if (Math.abs(sixteenths - Math.round(sixteenths)) > 1e-9) return `${sign}${Number(size.toFixed(3))}`;
  const whole = Math.floor(Math.round(sixteenths) / SIXTEENTHS);
  const rest = Math.round(sixteenths) % SIXTEENTHS;
  if (!rest) return `${sign}${whole}`;
  const divisor = gcd(rest, SIXTEENTHS);
  const fraction = `${rest / divisor}/${SIXTEENTHS / divisor}`;
  return whole ? `${sign}${whole} ${fraction}` : `${sign}${fraction}`;
}

/** The difference with its sign: "+9 in", "−1/2 in", "0 in". */
export function differenceWords(value: number): string {
  if (value === 0) return '0 in';
  return `${value > 0 ? '+' : ''}${inchWords(value)} in`;
}

/** Code's difference for a finding, or null when code has none. */
export function differenceOf(finding: Pick<ChatFinding, 'inches'>): number | null {
  return inchNumber(finding.inches?.difference);
}

/** One side's value: code's inches ("37 in") when code read it, with the printed words kept for the tooltip. */
export interface SideValue {
  /** What the cell shows. */
  shown: string;
  /** The value as printed (the finding's own words), when the cell shows code's inches instead. */
  printed: string | null;
  /** True when code could not read it as a length: shown as written, marked "not a number". */
  notANumber: boolean;
}

export function sideValue(finding: Pick<ChatFinding, 'architect' | 'vendor' | 'inches'>, side: 'architect' | 'vendor'): SideValue {
  const printed = finding[side];
  const written = printed === null || printed === undefined || !String(printed).trim() ? '—' : String(printed);
  if (!finding.inches) return { shown: written, printed: null, notANumber: false };
  const inches = inchNumber(finding.inches[side]);
  if (inches === null) return { shown: written, printed: null, notANumber: written !== '—' };
  return { shown: `${inchWords(inches)} in`, printed: written, notANumber: false };
}

/** "37 in → 46 in · +9 in", or null when code has no difference for it. */
export function changeWords(finding: Pick<ChatFinding, 'architect' | 'vendor' | 'inches'>): string | null {
  const difference = differenceOf(finding);
  if (difference === null) return null;
  return `${sideValue(finding, 'architect').shown} → ${sideValue(finding, 'vendor').shown} · ${differenceWords(difference)}`;
}

// ── The checks of one finding ───────────────────────────────

/** pass ✓, fail ✗ (it disagreed), unsure ?, not_run – (not run, or not possible for this one). */
export type CheckState = 'pass' | 'fail' | 'unsure' | 'not_run';

export type CheckKey = 'printed' | 'second_read' | 'verified' | 'rule';

export interface CheckResult {
  key: CheckKey;
  /** The check's short name ("Printed", "2nd read", "Checked on drawing", "Rule" / "Arithmetic"). */
  name: string;
  state: CheckState;
  /** What passed, or which side disagreed, in code's words. */
  words: string;
}

const PRINTED_STATE: Record<PrintedResult, CheckState> = {
  match: 'pass',
  not_found: 'fail',
  no_text: 'unsure',
  not_a_number: 'unsure',
  no_box: 'not_run',
  not_run: 'not_run',
};

const PRINTED_WORDS: Record<PrintedResult, string> = {
  match: 'found',
  not_found: 'not found',
  no_text: 'drawn as pen strokes (cannot be checked this way)',
  not_a_number: 'not a number',
  no_box: 'no box to search',
  not_run: 'not checked',
};

function printedOf(finding: ChatFinding, side: 'architect' | 'vendor'): PrintedResult | null {
  const result = side === 'architect' ? finding.checks?.printed_architect : finding.checks?.printed_vendor;
  return result && result in PRINTED_STATE ? result : null;
}

function printedCheck(finding: ChatFinding): CheckResult {
  const name = 'Printed';
  const known = (['architect', 'vendor'] as const)
    .map((side) => [side, printedOf(finding, side)] as const)
    .filter((side): side is readonly ['architect' | 'vendor', PrintedResult] => side[1] !== null && side[1] !== 'not_run');
  if (!known.length) return { key: 'printed', name, state: 'not_run', words: 'Printed at this spot: not checked' };
  const states = known.map(([, result]) => PRINTED_STATE[result]);
  const state: CheckState = states.includes('fail') ? 'fail'
    : states.length === 2 && states.every((each) => each === 'pass') ? 'pass'
    : states.some((each) => each === 'unsure' || each === 'pass') ? 'unsure'
    : 'not_run';
  const words = known.map(([side, result]) => `${side} value ${PRINTED_WORDS[result]}`).join('; ');
  return { key: 'printed', name, state, words: `Printed at this spot: ${words}` };
}

function secondReadCheck(finding: ChatFinding): CheckResult {
  const read = finding.checks?.second_read;
  const name = '2nd read';
  if (read === 'agrees') return { key: 'second_read', name, state: 'pass', words: 'Second read agrees' };
  if (read === 'disagrees') return { key: 'second_read', name, state: 'fail', words: 'Second read disagrees' };
  if (read === 'unsure') return { key: 'second_read', name, state: 'unsure', words: 'Second read unsure' };
  return { key: 'second_read', name, state: 'not_run', words: 'Second read: not run' };
}

/** The lead's own check: the recorded `lead_checked`, else the finding's `verified` flag. */
export function leadChecked(finding: Pick<ChatFinding, 'checks' | 'verified'>): boolean {
  return finding.checks?.lead_checked ?? finding.verified;
}

function verifiedCheck(finding: ChatFinding): CheckResult {
  const checked = leadChecked(finding);
  return { key: 'verified', name: 'Checked on drawing', state: checked ? 'pass' : 'unsure', words: verifiedWords(checked) };
}

function ruleCheck(finding: ChatFinding): CheckResult {
  const checks = finding.checks;
  const rule = finding.rule_check_id?.trim();
  if (checks?.rule === 'agrees' || checks?.rule === 'disagrees') {
    const agrees = checks.rule === 'agrees';
    return { key: 'rule', name: 'Rule', state: agrees ? 'pass' : 'fail', words: `GV rule${rule ? ` ${rule}` : ''}: code’s recorded result ${agrees ? 'agrees' : 'disagrees'}` };
  }
  if (checks?.arithmetic) {
    const parts = finding.parts?.length ? finding.parts.join(' + ') : 'the vendor’s parts';
    const total = finding.printed_total ? `the printed total ${finding.printed_total}` : 'the printed total';
    if (checks.arithmetic === 'adds_up') return { key: 'rule', name: 'Arithmetic', state: 'pass', words: `Code added up ${parts}: matches ${total}` };
    if (checks.arithmetic === 'does_not_add_up') return { key: 'rule', name: 'Arithmetic', state: 'fail', words: `Code added up ${parts}: does not match ${total}` };
    return { key: 'rule', name: 'Arithmetic', state: 'unsure', words: 'Arithmetic: a part is not a number' };
  }
  const name = finding.source === 'vendor_arithmetic' ? 'Arithmetic' : 'Rule';
  return {
    key: 'rule', name, state: 'not_run',
    words: finding.source === 'rules' || finding.source === 'vendor_arithmetic' ? `${name}: not checked by code` : 'No GV rule or arithmetic for this one',
  };
}

/** The four checks, in the Trust column's order: Printed · 2nd read · Checked on drawing · Rule/Arithmetic. */
export function findingChecks(finding: ChatFinding): CheckResult[] {
  return [printedCheck(finding), secondReadCheck(finding), verifiedCheck(finding), ruleCheck(finding)];
}

/**
 * Why a finding is To check, in code's words: the checks that disagreed ("Second read disagrees"),
 * else how many checks passed; null when it is not To check and nothing disagreed.
 */
export function trustReason(finding: ChatFinding): string | null {
  const checks = findingChecks(finding);
  const failed = checks.filter((check) => check.state === 'fail');
  if (failed.length) return failed.map((check) => check.words).join('; ');
  if (trustLevelOf(finding) !== 'to_check') return null;
  const passed = checks.filter((check) => check.state === 'pass').length;
  return `${passed} of ${checks.length} checks passed`;
}

/** The printed-at-spot result of one side, for under its crop in the evidence viewer. */
export function printedSideWords(finding: ChatFinding, side: 'architect' | 'vendor'): { state: CheckState; words: string } | null {
  const result = printedOf(finding, side);
  if (!result || result === 'not_run') return null;
  const value = finding[side]?.trim() || '—';
  if (result === 'match') return { state: 'pass', words: `Printed at this spot: ${value}` };
  if (result === 'not_found') return { state: 'fail', words: `Not found printed at this spot: ${value}` };
  if (result === 'no_text') return { state: 'unsure', words: 'Drawn as pen strokes: cannot be checked against the text' };
  if (result === 'not_a_number') return { state: 'unsure', words: `Not a number, so not searched: ${value}` };
  return { state: 'not_run', words: 'No box on the drawing to search' };
}

// ── The "How this was checked" counts ───────────────────────

export interface CheckCounts {
  printed: { match: number; not_found: number; no_text: number } | null;
  second_read: { agrees: number; disagrees: number; unsure: number } | null;
  verified: { checked: number; total: number };
  rules: string[];
  arithmetic: { checked: number; agrees: number } | null;
}

/** The report's check counts where code sent them, else counted from the findings. */
export function checkCounts(findings: readonly ChatFinding[], trust: ChatTrust | null | undefined): CheckCounts {
  const report = trust?.checks;
  let printed: CheckCounts['printed'] = null;
  let secondRead: CheckCounts['second_read'] = null;
  let arithmetic: CheckCounts['arithmetic'] = null;
  const rules = new Set<string>();
  for (const finding of findings) {
    for (const side of ['architect', 'vendor'] as const) {
      const result = printedOf(finding, side);
      if (result !== 'match' && result !== 'not_found' && result !== 'no_text') continue;
      printed ??= { match: 0, not_found: 0, no_text: 0 };
      printed[result] += 1;
    }
    const read = finding.checks?.second_read;
    if (read === 'agrees' || read === 'disagrees' || read === 'unsure') {
      secondRead ??= { agrees: 0, disagrees: 0, unsure: 0 };
      secondRead[read] += 1;
    }
    if ((finding.checks?.rule === 'agrees' || finding.checks?.rule === 'disagrees') && finding.rule_check_id) rules.add(finding.rule_check_id);
    const sum = finding.checks?.arithmetic;
    if (sum === 'adds_up' || sum === 'does_not_add_up') {
      arithmetic ??= { checked: 0, agrees: 0 };
      arithmetic.checked += 1;
      if (sum === 'adds_up') arithmetic.agrees += 1;
    }
  }
  return {
    printed: report?.printed ?? printed,
    second_read: report?.second_read ?? secondRead,
    verified: report?.verified ?? { checked: findings.filter((finding) => leadChecked(finding)).length, total: findings.length },
    rules: report?.rules ?? [...rules],
    arithmetic: report?.arithmetic ?? arithmetic,
  };
}

/** The lines of the "How this was checked" list, each factual and counted. */
export function checkLines(counts: CheckCounts): string[] {
  const lines: string[] = [];
  if (counts.printed) {
    const { match, not_found: missing, no_text: strokes } = counts.printed;
    const total = match + missing + strokes;
    const extra = [missing ? `${missing} not found` : null, strokes ? `${strokes} drawn as pen strokes` : null].filter(Boolean).join(', ');
    lines.push(`Printed at the spot: ${match} of ${total} values found${extra ? ` (${extra})` : ''}`);
  } else {
    lines.push('Printed at the spot: not run');
  }
  if (counts.second_read) {
    const { agrees, disagrees, unsure } = counts.second_read;
    lines.push(`Second independent read: ${agrees} agree · ${disagrees} disagree · ${unsure} unsure`);
  } else {
    lines.push('Second independent read: not run');
  }
  lines.push(`Checked on the drawing by the lead: ${counts.verified.checked} of ${counts.verified.total}`);
  lines.push(counts.rules.length ? `GV rules applied by code: ${counts.rules.join(', ')}` : 'GV rules applied by code: none for these findings');
  if (counts.arithmetic) lines.push(`Vendor’s arithmetic added up by code: ${counts.arithmetic.agrees} of ${counts.arithmetic.checked} match`);
  return lines;
}

// ── Rounds ──────────────────────────────────────────────────

export const ROUND_WORDS: Record<RoundStatus, string | null> = {
  fixed_in_later_round: 'Fixed in a later round',
  still_wrong: 'Still wrong',
  new_in_later_round: 'New in a later round',
  not_in_later_round: 'Not in the later round',
  single_round: null,
};

export interface RoundCounts {
  fixed: number;
  still_wrong: number;
  new: number;
  not_in_later?: number | null;
}

/** Rounds of comments: the report's counts, else counted from the findings; null with one round. */
export function roundCounts(findings: readonly ChatFinding[], trust: ChatTrust | null | undefined): RoundCounts | null {
  if (trust?.rounds) return trust.rounds;
  const counts = { fixed: 0, still_wrong: 0, new: 0, not_in_later: 0 };
  let any = false;
  for (const finding of findings) {
    const status = finding.round_status;
    if (status === 'fixed_in_later_round') counts.fixed += 1;
    else if (status === 'still_wrong') counts.still_wrong += 1;
    else if (status === 'new_in_later_round') counts.new += 1;
    else if (status === 'not_in_later_round') counts.not_in_later += 1;
    else continue;
    any = true;
  }
  return any ? counts : null;
}

/** "Vendor fixed 3 of 8 round-1 items · 5 still wrong · 1 new". */
export function roundWords(counts: RoundCounts): string {
  const notInLater = counts.not_in_later ?? 0;
  const first = counts.fixed + counts.still_wrong + notInLater;
  const parts = [`Vendor fixed ${counts.fixed} of ${first} round-1 ${first === 1 ? 'item' : 'items'}`];
  if (counts.still_wrong) parts.push(`${counts.still_wrong} still wrong`);
  if (notInLater) parts.push(`${notInLater} not in the later round`);
  if (counts.new) parts.push(`${counts.new} new`);
  return parts.join(' · ');
}

// ── Summary helpers ─────────────────────────────────────────

/** True when there is anything for the trust summary to say. */
export function hasTrust(findings: readonly ChatFinding[], trust: ChatTrust | null | undefined): boolean {
  return Boolean(trust) || findings.some((finding) => trustLevelOf(finding) !== null || finding.checks);
}

/** Pages not opened, from the coverage map ("pages 4, 7 (not opened)"); null when none. */
export function notOpenedWords(coverage: ChatCoverage | null | undefined): string | null {
  const pages = (coverage?.pages ?? []).filter((page) => page.status === 'not_opened').map((page) => page.page).sort((a, b) => a - b);
  if (!pages.length) return null;
  return `${pages.length === 1 ? 'page' : 'pages'} ${pages.join(', ')} (not opened)`;
}

// ── CSV export ──────────────────────────────────────────────

const STATE_MARK: Record<CheckState, string> = { pass: 'pass', fail: 'disagrees', unsure: 'unsure', not_run: 'not run' };

/**
 * One CSV cell. Text that a spreadsheet would run as a formula (starting with =, +, -, @) gets a
 * leading apostrophe, unless it is a plain number; quotes, commas and line breaks are quoted.
 */
export function csvCell(value: string | number | null | undefined): string {
  if (value === null || value === undefined) return '';
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : '';
  let text = value;
  if (/^[=+\-@\t\r]/.test(text) && !/^[+-]?\d+(\.\d+)?$/.test(text)) text = `'${text}`;
  return /[",\r\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** The byte-order mark that tells Excel the CSV is UTF-8 (so fractions and primes survive). */
const BOM = '﻿';

export const CSV_HEADER = [
  'Category', 'Page', 'Item', 'Architect (as printed)', 'Vendor (as printed)', 'Architect (in)', 'Vendor (in)', 'Difference (in)',
  'Status', 'Trust', 'Evidence', 'Printed at spot', 'Second read', 'Checked on drawing', 'Rule / arithmetic', 'Compared with', 'Round', 'Why',
] as const;

/** The findings as a CSV Excel opens (with its byte-order mark), one row per finding, trust columns included. */
export function findingsCsv(findings: readonly ChatFinding[]): string {
  const order: Record<FindingCategory, number> = { countertop: 0, cabinet: 1, other: 2 };
  const rows = [...findings].sort((a, b) => order[categoryOf(a)] - order[categoryOf(b)] || a.page - b.page).map((finding) => {
    const [printed, secondRead, verified, rule] = findingChecks(finding);
    const level = trustLevelOf(finding);
    return [
      CATEGORY_WORDS[categoryOf(finding)],
      pageWords(finding),
      finding.item,
      finding.architect ?? '',
      finding.vendor ?? '',
      // Code's own decimal strings, as sent.
      finding.inches?.architect ?? null,
      finding.inches?.vendor ?? null,
      finding.inches?.difference ?? null,
      STATUS_WORDS[finding.status] ?? finding.status,
      level ? TRUST_WORDS[level] : '',
      evidenceKindOf(finding) ? EVIDENCE_WORDS[evidenceKindOf(finding) as EvidenceKind] : '',
      STATE_MARK[printed.state],
      STATE_MARK[secondRead.state],
      verified.state === 'pass' ? 'yes' : 'no',
      rule.state === 'not_run' ? '' : `${rule.name}: ${STATE_MARK[rule.state]}`,
      sourceTag(finding) ?? 'architect',
      finding.round_status ? ROUND_WORDS[finding.round_status] ?? '' : '',
      finding.why ?? '',
    ].map(csvCell).join(',');
  });
  return `${BOM}${[CSV_HEADER.map(csvCell).join(','), ...rows].join('\r\n')}\r\n`;
}
