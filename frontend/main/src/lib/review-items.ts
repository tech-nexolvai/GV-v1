/**
 * The simple review page's rules (#1343), kept apart from the components so each is tested once.
 *
 * Nothing here decides an outcome or whether the review may be signed: it only orders what the server
 * sent, names it in plain words, and says which answers an item offers. The one box to type into is
 * Fix on an AI number: the value the drawing shows, in inches, recorded as a correction.
 */

import { isItemAction } from '@/api/review-items';
import type {
  BestGuess,
  ItemAction,
  NotCheckedItem,
  NotCheckedSetting,
  PageRef,
  ReviewFailure,
  ReviewItem,
  ReviewItemsHeader,
  ReviewItemsOut,
  ReviewStatus,
} from '@/api/review-items';

/** The AIs as a reviewer knows them. */
export function readerWord(reader: string): string {
  if (reader === 'pdf-text-layer') return 'PDF text';
  if (reader === 'MODEL_A') return 'AI 1';
  if (reader === 'MODEL_B') return 'AI 2';
  return reader.replace(/_/g, ' ').toLowerCase().replace(/^./, (c) => c.toUpperCase());
}

export const GUESS_SOURCE_WORDS: Record<BestGuess['source'], string> = {
  pdf_text: "Printed in the PDF's own text",
  // Held although both wrote it: never "read this" beside a reason that a reader could not (QA 3).
  agreed_text: 'Both AIs wrote this value, but it was held for a person',
  suggestion: 'The stored reading; the readings beside it differ',
};

/** The button words for each answer. Each says what happens when it is pressed. */
export const ACTION_LABELS: Record<ItemAction, string> = {
  confirm: 'Confirm',
  fix: 'Fix',
  checked_right: 'It is right',
  checked_wrong: 'It is wrong',
  choose_wall: 'Choose walls',
  confirm_no_countertop: 'No countertop here',
  there_is_a_countertop: 'There is a countertop',
  answer: 'Answer',
  not_this: 'Not this one',
  agree: 'Agree',
  settle_group: 'Clear all as not checked',
  mark_cabinets: 'Save marks',
  use_cabinet_run: 'Use this run',
  job_values: 'Save and check again',
  clear_not_compared: 'Clear all as not compared',
};

/** The answer that agrees with the AI (the C key), and the one that disagrees (the F key). */
const AGREE: readonly ItemAction[] = ['confirm', 'checked_right', 'confirm_no_countertop', 'choose_wall', 'answer'];
const DISAGREE: readonly ItemAction[] = ['fix', 'checked_wrong', 'there_is_a_countertop', 'not_this'];

export function agreeAction(item: Pick<ReviewItem, 'actions' | 'status'>): ItemAction | null {
  if (item.status !== 'open') return null;
  return item.actions.filter(isItemAction).find((action) => AGREE.includes(action)) ?? null;
}

export function disagreeAction(item: Pick<ReviewItem, 'actions' | 'status'>): ItemAction | null {
  if (item.status !== 'open') return null;
  return item.actions.filter(isItemAction).find((action) => DISAGREE.includes(action)) ?? null;
}

/** The wall layout the C key picks on a wall choice: the readers' proposal, or null (none). */
export function proposedWall(item: Pick<ReviewItem, 'wall_choices'>): string | null {
  return (item.wall_choices ?? []).find((choice) => choice.proposed)?.layout ?? null;
}

function bare(text: string): string {
  return text.replace(/["”″]|\bin\b/g, '').replace(/\s+/g, ' ').trim();
}

/**
 * The AIs' own readings that differ from the best guess, once each: offered in the Fix dialog as a
 * quick fill for the box. Empty when every AI read the same as the guess.
 */
export function fixChoices(item: Pick<ReviewItem, 'best_guess' | 'readers'>): { text: string; readers: string[] }[] {
  const guess = item.best_guess ? bare(item.best_guess.text) : null;
  const choices = new Map<string, string[]>();
  for (const reading of item.readers ?? []) {
    const text = bare(reading.text);
    if (!text || text === guess) continue;
    choices.set(text, [...(choices.get(text) ?? []), readerWord(reading.reader)]);
  }
  return [...choices].map(([text, readers]) => ({ text: `${text}"`, readers }));
}

/** Where a failure or an unchecked result is, in words; never "page null". */
export function whereWord(entry: { countertop?: { label: string } | null; row_label?: string | null; page?: number | null }): string {
  if (entry.countertop?.label) return entry.countertop.label;
  if (entry.row_label) return entry.row_label;
  return entry.page != null ? `Page ${entry.page}` : 'Place not recorded';
}

/** "p.13": the page, short, for a line of the list. */
export function pageWord(page: number | null | undefined): string {
  return page == null ? '' : `p.${page}`;
}

/** The thing an item is about: its countertop, else its row, else its page. */
export function placeWord(item: Pick<ReviewItem, 'countertop' | 'row_label'>): string {
  // With neither, the page number beside it already says where.
  return item.countertop?.label ?? item.row_label ?? '';
}

/** Items on no page sort last. */
function pageOrder(page: number | null | undefined): number {
  return page ?? Number.MAX_SAFE_INTEGER;
}

export function statusRank(status: ReviewStatus): number {
  return status === 'open' ? 0 : status === 'rechecking' ? 1 : 2;
}

/** Open first, then re-checking, then done; by page within each, as the server sent them otherwise. */
export function orderItems<T extends { status: ReviewStatus; page?: number | null }>(items: readonly T[]): T[] {
  return items
    .map((item, index) => ({ item, index }))
    .sort((a, b) => statusRank(a.item.status) - statusRank(b.item.status) || pageOrder(a.item.page) - pageOrder(b.item.page) || a.index - b.index)
    .map(({ item }) => item);
}

export interface ReviewSummary {
  openItems: ReviewItem[];
  rechecking: ReviewItem[];
  doneItems: ReviewItem[];
  openFailures: ReviewFailure[];
  openNotChecked: NotCheckedItem[];
  /** True when nothing on the page is waiting for a person. */
  nothingNeedsYou: boolean;
  /** The server's own answer (`header.can_finalise`): the screen never works readiness out. */
  canFinalise: boolean;
  /** Why the server keeps the final cloud PDF locked, in its own words, or null when it is not. */
  finalBlocked: string | null;
}

function plural(count: number, one: string, many: string = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function summarize(data: ReviewItemsOut): ReviewSummary {
  const openItems = orderItems(data.items.filter((item) => item.status === 'open'));
  const rechecking = data.items.filter((item) => item.status === 'rechecking');
  const doneItems = orderItems(data.items.filter((item) => item.status === 'done'));
  const openFailures = data.failures.filter((failure) => failure.status === 'open');
  const openNotChecked = data.not_checked.filter((check) => check.status === 'open');
  const nothingNeedsYou = openItems.length === 0 && openFailures.length === 0 && openNotChecked.length === 0;
  const canFinalise = data.header.can_finalise;
  const finalBlocked = canFinalise ? null : (data.header.blocked_reason?.trim() || 'The server has not unlocked the final cloud PDF yet.');
  return { openItems, rechecking, doneItems, openFailures, openNotChecked, nothingNeedsYou, canFinalise, finalBlocked };
}

/**
 * What is left, for the final bar (#1394): plain words with the counts of the same W1 header the page
 * prints (`open`, `not_checked`, `rechecking`), never counted again here and never a judgment of
 * whether the review may be signed (that is `header.can_finalise`). Only counts above zero. Open
 * items are optional (owner, 2026-10-16): "3 optional", never "3 items need you".
 */
export function leftWords(header: Pick<ReviewItemsHeader, 'open' | 'not_checked' | 'rechecking'>): string[] {
  const words: string[] = [];
  // Every question is optional (owner, 2026-10-16): an open item never "needs" a person.
  if (header.open > 0) words.push(`${header.open} optional`);
  if (header.not_checked > 0) words.push(`${header.not_checked} not checked`);
  if (header.rechecking > 0) words.push(`${plural(header.rechecking, 'item')} re-checking`);
  return words;
}

/**
 * "Ready to create the final cloud PDF — 3 items left unanswered will be printed as not confirmed":
 * the final bar's words when the server unlocked sign-off while `header.open` items are still open.
 */
export function unansweredWords(open: number): string {
  return `Ready to create the final cloud PDF — ${plural(open, 'item')} left unanswered will be printed as not confirmed`;
}

/** The plain words for each kind of open item, in the order the sign-off dialog lists them. */
const NOT_CONFIRMED_KIND_WORDS: [ReviewItem['kind'], string][] = [
  ['number', 'Numbers to confirm'],
  ['depth_reading', 'Depths to confirm'],
  ['countertop_by_hand', 'Countertops to check by hand'],
  ['wall_choice', 'Wall choices'],
  ['no_countertop_page', 'Pages to decide'],
  ['architect', 'Architect questions'],
  ['cabinet_kinds', 'Cabinet marks'],
  ['cabinet_run', 'Cabinet marks'],
  ['other_result', 'Other results to check'],
];
/** A kind this screen does not know yet still gets a line of its own words. */
const OTHER_QUESTIONS = 'Other questions';
export const CLOUDED_NOT_AGREED = 'Clouded as wrong, not agreed by a person';

/**
 * What the final cloud PDF will print as "Not confirmed by a person" (owner, 2026-10-16): the open
 * items, counted by kind in plain words, then the open failures. Only groups above zero, so a
 * review with nothing open gives an empty list. Counts what the server sent; decides nothing.
 */
export function notConfirmedGroups(data: {
  items: readonly Pick<ReviewItem, 'kind' | 'status'>[];
  failures: readonly Pick<ReviewFailure, 'status'>[];
}): { label: string; count: number }[] {
  const counts = new Map<string, number>();
  for (const [, label] of NOT_CONFIRMED_KIND_WORDS) counts.set(label, 0);
  counts.set(OTHER_QUESTIONS, 0);
  const words = new Map<string, string>(NOT_CONFIRMED_KIND_WORDS);
  for (const item of data.items) {
    if (item.status !== 'open') continue;
    const label = words.get(item.kind) ?? OTHER_QUESTIONS;
    counts.set(label, (counts.get(label) ?? 0) + 1);
  }
  counts.set(CLOUDED_NOT_AGREED, data.failures.filter((failure) => failure.status === 'open').length);
  return [...counts].filter(([, count]) => count > 0).map(([label, count]) => ({ label, count }));
}

/**
 * A stored reason, tidied for the screen (Set 3 QA): the wall reason joins its question and its
 * summary without a full stop ("...each end The vendor wrote"); the full stop is added. Nothing
 * else changes: the words are the server's.
 */
export function tidyWhy(text: string): string {
  return text.replace(/([^.!?:;\s])\s+(The vendor wrote\b)/g, '$1. $2');
}

/** A sentence: first letter capital, one full stop at the end. */
function asSentence(text: string): string {
  const trimmed = text.trim();
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

/**
 * A note made of several reasons joined with semicolons (the architect note, Set 3 QA): the first
 * one is shown, the others wait behind "Why". Each is a sentence of its own.
 */
export function splitReasons(text: string): { first: string; others: string[] } {
  const parts = text.split(/;\s+/).map((part) => part.trim()).filter((part) => part.length > 0);
  if (parts.length === 0) return { first: text, others: [] };
  return { first: asSentence(parts[0]), others: parts.slice(1).map(asSentence) };
}

/**
 * True when what was typed in the Fix box is in millimetres, centimetres or metres (Set 3 QA): the
 * server records inches only, so the box says so at once instead of previewing the value.
 */
export function metricTyped(typed: string): boolean {
  return /\d\s*(?:mm|cm|m)\b|millimet|centimet/i.test(typed);
}

/**
 * The value a Fix records. The dialog asks for inches, so a bare number ("96 3/4", "42.5") gets its
 * inch mark; anything else (feet and inches, a unit already written, mm) is sent as typed, and the
 * server refuses what it cannot record exactly, in its own words.
 */
export function fixValue(typed: string): string {
  const text = typed.trim().replace(/\s+/g, ' ');
  if (/^\d+(?:\.\d+)?(?: \d+\/\d+)?$|^\d+\/\d+$/.test(text)) return `${text}"`;
  return text;
}

/**
 * The open pages every AI reader positively said have no countertop: one choice card for the lot
 * (#1368). Only these are proposed in one click (#1394).
 */
export function openNoCountertopPages(items: readonly ReviewItem[]): ReviewItem[] {
  return orderItems(items.filter((item) => item.kind === 'no_countertop_page' && item.status === 'open' && item.proposed_no_countertop === true));
}

/**
 * The open pages whose reading is older or in doubt (#1394): each is decided on its own, with both
 * answers offered and nothing proposed.
 */
export function openPagesToDecide(items: readonly ReviewItem[]): ReviewItem[] {
  return orderItems(items.filter((item) => item.kind === 'no_countertop_page' && item.status === 'open' && item.proposed_no_countertop !== true));
}

/** "p.2, p.4 and p.5". */
export function pagesWord(pages: readonly (number | null | undefined)[]): string {
  const named = pages.filter((page): page is number => page != null).map((page) => `p.${page}`);
  if (named.length <= 1) return named.join('');
  return `${named.slice(0, -1).join(', ')} and ${named[named.length - 1]}`;
}

/** A polygon in stored page space (0..1) as numbers; null when it is missing or unreadable. */
export function pointsOf(ref: Pick<PageRef, 'polygon'> | null | undefined): [number, number][] | null {
  if (!ref || ref.polygon.length < 3) return null;
  const points = ref.polygon.map((pair) => [Number(pair[0]), Number(pair[1])] as [number, number]);
  return points.every(([x, y]) => Number.isFinite(x) && Number.isFinite(y)) ? points : null;
}

/**
 * The ring drawn around a spot, in the same 0..1 space: an ellipse a little larger than the spot,
 * so the ring sits around the number rather than over it. Never smaller than 3% of the page.
 */
export function ringOf(points: readonly [number, number][]): { cx: number; cy: number; rx: number; ry: number } {
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  return {
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2,
    rx: Math.max(((maxX - minX) / 2) * 1.35, 0.03),
    ry: Math.max(((maxY - minY) / 2) * 1.6, 0.03),
  };
}

/**
 * The part of the page around a spot, in the same 0..1 space: the spot with room around it, so the
 * dimension text above and below a row's line is in view. Never past the page's edges.
 */
export function stripOf(points: readonly [number, number][]): { x: number; y: number; w: number; h: number } {
  const xs = points.map(([x]) => x);
  const ys = points.map(([, y]) => y);
  const [minX, maxX, minY, maxY] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)];
  const padX = Math.max((maxX - minX) * 0.1, 0.02);
  const padY = Math.max((maxY - minY) * 1.5, 0.04);
  const x = Math.max(0, minX - padX);
  const y = Math.max(0, minY - padY);
  return { x, y, w: Math.min(1, maxX + padX) - x, h: Math.min(1, maxY + padY) - y };
}

/** Who decided a done item, in words. */
export function decidedWord(decidedBy: string | null): string {
  if (decidedBy === null) return 'Checked';
  if (decidedBy === 'REVIEWER') return 'Checked by you';
  if (decidedBy === 'AUTOMATIC' || decidedBy === 'AI') return 'Confirmed by AI';
  return `Checked by ${decidedBy}`;
}

/** Whether a done entry's standing answer is "Checked by hand: wrong" (#1358): the API's own mark, never words. */
export function checkedWrongByHand(entry: { checked_wrong_by_hand?: boolean }): boolean {
  return entry.checked_wrong_by_hand === true;
}

/**
 * The words for a done entry's answer: "Checked by hand: WRONG by you" (or by name) when the API marks
 * it so, else who decided it. The result itself stays as recorded.
 */
export function answerWord(entry: { decided_by?: string | null; checked_wrong_by_hand?: boolean }): string {
  const decidedBy = entry.decided_by ?? null;
  if (!checkedWrongByHand(entry)) return decidedWord(decidedBy);
  if (decidedBy === null) return 'Checked by hand: WRONG';
  return `Checked by hand: WRONG by ${decidedBy === 'REVIEWER' ? 'you' : decidedBy}`;
}

export { splitNumbers } from './numbers';

/** The element id of an item's opened detail, for `aria-controls`. */
export function detailId(key: string): string {
  return `item-detail-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}

/**
 * What the open checks still wait for: the settings the API names and none has been saved since
 * (`set_now`), in the list's order, and how many countertops (or checks, for a whole-set check) wait
 * for them (#1403).
 */
export function jobValuesWaiting(items: readonly NotCheckedItem[]): { settings: NotCheckedSetting[]; countertops: number | null; checks: number } {
  const settings: NotCheckedSetting[] = [];
  const waiting = items.filter((item) => item.status === 'open' && (item.settings ?? []).some((setting) => !setting.set_now));
  for (const item of waiting) {
    for (const setting of item.settings ?? []) {
      if (!setting.set_now && !settings.some((seen) => seen.key === setting.key)) settings.push(setting);
    }
  }
  // A countertop is its live item, else its row on its page (a result scoped to a row before items
  // existed); a whole-set result has neither, and then the line counts checks instead.
  const where = waiting.map((item) => item.countertop?.item_id ?? (item.page != null && item.row_label ? `p${item.page}:${item.row_label}` : null));
  const countertops = where.every((key) => key !== null) ? new Set(where).size : null;
  return { settings, countertops, checks: waiting.length };
}

/** A typed value in inches (`24`, `1 1/2`, `0.75`, `3/4`, with or without `"`), with its inch mark; null otherwise. */
export function inchValue(typed: string): string | null {
  const text = typed.trim().replace(/\s+/g, ' ').replace(/\s*(?:"|in|inch|inches)$/i, '');
  if (!/^\d+(?:\.\d+)?(?: \d+\/\d+)?$|^\d+\/\d+$/.test(text)) return null;
  return `${text}"`;
}
