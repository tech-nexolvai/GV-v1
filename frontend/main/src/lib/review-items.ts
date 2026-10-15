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
  agreed_text: 'Both AIs read this',
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
 * whether the review may be signed (that is `header.can_finalise`). Only counts above zero.
 */
export function leftWords(header: Pick<ReviewItemsHeader, 'open' | 'not_checked' | 'rechecking'>): string[] {
  const words: string[] = [];
  if (header.open > 0) words.push(`${plural(header.open, 'item')} ${header.open === 1 ? 'needs' : 'need'} you`);
  if (header.not_checked > 0) words.push(`${header.not_checked} not checked`);
  if (header.rechecking > 0) words.push(`${plural(header.rechecking, 'item')} re-checking`);
  return words;
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

/** Who decided a done item, in words. */
export function decidedWord(decidedBy: string | null): string {
  if (decidedBy === null) return 'Checked';
  if (decidedBy === 'REVIEWER') return 'Checked by you';
  if (decidedBy === 'AUTOMATIC' || decidedBy === 'AI') return 'Confirmed by AI';
  return `Checked by ${decidedBy}`;
}

/** Splits text so numbers (with fractions and inch marks) can be set in the number face. */
export function splitNumbers(text: string): { text: string; number: boolean }[] {
  const parts: { text: string; number: boolean }[] = [];
  const pattern = /\d+(?:\s\d+\/\d+|\/\d+|\.\d+)?(?:\s?(?:"|in\b))?/g;
  let last = 0;
  for (const match of text.matchAll(pattern)) {
    const at = match.index ?? 0;
    if (at > last) parts.push({ text: text.slice(last, at), number: false });
    parts.push({ text: match[0], number: true });
    last = at + match[0].length;
  }
  if (last < text.length) parts.push({ text: text.slice(last), number: false });
  return parts;
}

/** The element id of an item's opened detail, for `aria-controls`. */
export function detailId(key: string): string {
  return `item-detail-${key.replace(/[^a-zA-Z0-9_-]/g, '-')}`;
}
