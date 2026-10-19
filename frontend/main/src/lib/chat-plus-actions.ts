/**
 * The composer's "+" menu (#1517): the actions a GV reviewer reaches for, kept free of React so they
 * can be tested alone.
 *
 * The owner: "we have to put the + icon, and in that add several options: the specific output we
 * want, or a specific prompt like diff this, do this." So the menu holds three groups:
 * - Checks: the full check (the app's own instruction, confirmed first) and focused questions a
 *   reviewer asks of a shop drawing against the architect's drawing.
 * - Outputs: the $0 files code makes from the last answer's findings (cloud drawing, Excel), and
 *   short documents the AI writes (comments letter to the vendor, questions for the architect).
 * - Focus: the products to check and how hard to think (they set the chips and the effort). The
 *   products are the registry's (`GET /products`, #1547): one option per product, the ones not ready
 *   greyed "coming later", never a list here.
 *
 * A question fills the composer with words the reviewer can edit before sending; a blank they must
 * fill in ("[page number]", "[area]") is highlighted and selected, and Send waits for it.
 */

import type { ChatEffort, ChatFinding, ChatModel, ChatProduct } from '@/lib/chat-api';
import { areaOf } from '@/lib/chat-findings';
import { chooseEffort, modelBlurb, shortModelLabel, type ChatChoice } from '@/lib/chat-settings';
import type { ViewMessage } from '@/lib/chat-messages';
import { activeProducts, productCatalog, type ProductCatalog } from '@/lib/products';

export type PlusGroup = 'checks' | 'outputs' | 'focus' | 'model';

export type PlusIcon =
  | 'full_check' | 'page' | 'area' | 'rules' | 'consistency' | 'strings' | 'missing' | 'rounds'
  | 'cloud' | 'csv' | 'letter' | 'rfi' | 'summary'
  | 'product' | 'both' | 'quick' | 'thorough' | 'model';

/** What choosing an option does. */
export type PlusRun =
  | { kind: 'prompt'; text: string }
  | { kind: 'full_check' }
  /** #1584: the vendor consistency check, by code ($0, no AI), stored as an answer. */
  | { kind: 'consistency' }
  | { kind: 'cloud' }
  | { kind: 'csv' }
  | { kind: 'products'; products: ChatProduct[]; comingLater?: boolean }
  | { kind: 'effort'; level: 'quick' | 'thorough' }
  | { kind: 'model'; model: string };

export interface PlusAction {
  id: string;
  group: PlusGroup;
  label: string;
  /** One short line under the label. */
  hint: string;
  /** Rough time and cost ("~2 min · ~$0.50"), when known. The full check's is filled in from the cap. */
  estimate?: string;
  icon: PlusIcon;
  /** Other words the search box matches. */
  keywords: string[];
  run: PlusRun;
}

// ── Blanks to fill in ───────────────────────────────────────

/** The blanks a prompt can hold. Only these are highlighted (a reviewer's own brackets are left alone). */
export const BLANKS = ['[page number]', '[area]'] as const;

const BLANK_PATTERN = /\[(?:page number|area)\]/g;

/** Where each blank sits in the words: [start, end) pairs, in order. */
export function blankRanges(text: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  for (const match of text.matchAll(BLANK_PATTERN)) ranges.push([match.index, match.index + match[0].length]);
  return ranges;
}

/** The first blank still in the words ("[page number]"), or null when all are filled. */
export function firstBlank(text: string): string | null {
  return text.match(BLANK_PATTERN)?.[0] ?? null;
}

/**
 * The blank Tab moves to: the next one after `from` (wrapping round), or with `back` the one before.
 * Null when there is none.
 */
export function nextBlank(text: string, from: number, back = false): [number, number] | null {
  const ranges = blankRanges(text);
  if (!ranges.length) return null;
  if (back) return [...ranges].reverse().find(([start]) => start < from) ?? ranges[ranges.length - 1];
  return ranges.find(([start]) => start >= from) ?? ranges[0];
}

/** The words with the blanks marked, for the highlight drawn behind the box. */
export function blankSegments(text: string): Array<{ text: string; blank: boolean }> {
  const segments: Array<{ text: string; blank: boolean }> = [];
  let at = 0;
  for (const [start, end] of blankRanges(text)) {
    if (start > at) segments.push({ text: text.slice(at, start), blank: false });
    segments.push({ text: text.slice(start, end), blank: true });
    at = end;
  }
  if (at < text.length) segments.push({ text: text.slice(at), blank: false });
  return segments;
}

/** A prompt put into the composer: the box's words stay, the prompt goes after them on its own. */
export function withPrompt(draft: string, prompt: string): string {
  return draft.trim() ? `${draft.replace(/\s+$/, '')}\n\n${prompt}` : prompt;
}

// ── The options ─────────────────────────────────────────────

const PROMPTS = {
  page:
    'Check vendor page [page number] against the architect’s drawing. List every value that is wrong with what it needs to be, anything I should check, and anything you could not compare.',
  area:
    'Compare the [area] on the vendor’s shop drawing with the architect’s drawing: sizes, heights, depths, materials, hardware and notes. Say what is wrong, what is right, and what could not be compared and why.',
  rules:
    'Check the vendor’s drawings against GV’s rules for the selected products. For each rule, list the pages that break it with GV’s number and the vendor’s.',
  strings:
    'Add up every dimension string on the vendor’s pages. List each string whose pieces do not add up to its printed overall, with the sum written out, and say how many strings you checked.',
  missing:
    'What is missing on either side? List the items the architect shows that the vendor’s shop drawing leaves out, and the items the vendor added that the architect does not show, with their pages.',
  rounds:
    'What changed between round 1 and round 2? For each of round 1’s comments, say whether the vendor fixed it, it is still wrong, or it was not touched, and list any change nobody asked for.',
  letter:
    'Write a comments letter to the vendor from the findings so far: a plain numbered list, one line each with the page, the item and what to change (the vendor’s value and the value it needs). Leave out anything still to check.',
  rfi:
    'Write the questions for the architect (an RFI list) for this drawing set: one numbered question for each item where the architect’s drawing is unclear, missing a value or disagrees with itself. Give the page and say what needs confirming.',
  summary: 'Summarise this drawing set in two lines: what it covers, and the most important problems found so far.',
} as const;

/** Every option, in the menu's order. Estimates come from the measured runs (2026-10-17) and are rough. */
export const PLUS_ACTIONS: readonly PlusAction[] = [
  // Checks
  { id: 'full_check', group: 'checks', label: 'Full check', hint: 'Every page, with the cloud drawing', icon: 'full_check', keywords: ['whole set', 'everything', 'review', 'run'], run: { kind: 'full_check' } },
  { id: 'check_page', group: 'checks', label: 'Check one page…', hint: 'One vendor page against the architect', estimate: '~2 min · ~$0.50', icon: 'page', keywords: ['sheet', 'page number'], run: { kind: 'prompt', text: PROMPTS.page } },
  { id: 'compare_area', group: 'checks', label: 'Compare an area…', hint: 'One room or unit on both drawings', estimate: '~2 min · ~$0.50', icon: 'area', keywords: ['room', 'unit', 'elevation', 'vanity', 'kitchen'], run: { kind: 'prompt', text: PROMPTS.area } },
  { id: 'gv_rules', group: 'checks', label: 'Check GV rules', hint: 'GV’s standards for the selected products', estimate: '~5 min · ~$1.50', icon: 'rules', keywords: ['rulebook', 'standards', 'filler', 'sink', 'cut-out', 'overhang', 'field cut'], run: { kind: 'prompt', text: PROMPTS.rules } },
  { id: 'consistency', group: 'checks', label: 'Vendor consistency check', hint: 'The vendor’s numbers against each other', estimate: '$0', icon: 'consistency', keywords: ['contradiction', 'add up', 'strings', 'sum', 'missing sheet', 'round', 'index', 'self-check', 'free'], run: { kind: 'consistency' } },
  { id: 'strings', group: 'checks', label: 'Do the dimension strings add up?', hint: 'The vendor’s own sums', estimate: '~5 min · ~$1.50', icon: 'strings', keywords: ['sum', 'arithmetic', 'total', 'overall', 'math'], run: { kind: 'prompt', text: PROMPTS.strings } },
  { id: 'missing', group: 'checks', label: 'What’s missing on either side?', hint: 'Items on one drawing only', estimate: '~5 min · ~$1.50', icon: 'missing', keywords: ['completeness', 'left out', 'extra', 'added'], run: { kind: 'prompt', text: PROMPTS.missing } },
  { id: 'rounds', group: 'checks', label: 'What changed between rounds?', hint: 'Closed, previous comments not addressed, or new', estimate: '~3 min · ~$1', icon: 'rounds', keywords: ['diff', 'revision', 'round 1', 'round 2', 'resubmittal', 'compare'], run: { kind: 'prompt', text: PROMPTS.rounds } },
  // Outputs
  { id: 'cloud', group: 'outputs', label: 'Cloud drawing', hint: 'The last answer’s findings, marked up by code', estimate: '$0', icon: 'cloud', keywords: ['marked-up', 'markup', 'pdf', 'redline', 'clouds'], run: { kind: 'cloud' } },
  { id: 'csv', group: 'outputs', label: 'Excel (CSV) of findings', hint: 'The last answer’s findings, for Excel', estimate: '$0', icon: 'csv', keywords: ['spreadsheet', 'table', 'export', 'download'], run: { kind: 'csv' } },
  { id: 'letter', group: 'outputs', label: 'Comments letter to the vendor', hint: 'Page, item, what to change', estimate: '~1 min · ~$0.30', icon: 'letter', keywords: ['email', 'comments', 'vendor', 'corrections'], run: { kind: 'prompt', text: PROMPTS.letter } },
  { id: 'rfi', group: 'outputs', label: 'Questions for the architect', hint: 'An RFI list', estimate: '~1 min · ~$0.30', icon: 'rfi', keywords: ['rfi', 'questions', 'architect', 'clarify', 'confirm'], run: { kind: 'prompt', text: PROMPTS.rfi } },
  { id: 'summary', group: 'outputs', label: 'Two-line summary', hint: 'What the set covers and its main problems', estimate: '~30 s · ~$0.10', icon: 'summary', keywords: ['short', 'overview', 'tl;dr'], run: { kind: 'prompt', text: PROMPTS.summary } },
  // Focus
  // Focus: the products come from the registry (`focusProducts`), then the efforts.
  { id: 'quick', group: 'focus', label: 'Quick', hint: 'Low effort: faster and cheaper', icon: 'quick', keywords: ['effort', 'fast', 'cheap', 'low'], run: { kind: 'effort', level: 'quick' } },
  { id: 'thorough', group: 'focus', label: 'Thorough', hint: 'Extra high effort: slower, more careful', icon: 'thorough', keywords: ['effort', 'careful', 'deep', 'high'], run: { kind: 'effort', level: 'thorough' } },
];

export const GROUP_WORDS: Record<PlusGroup, string> = { checks: 'Checks', outputs: 'Outputs', focus: 'Focus', model: 'Model' };

/**
 * #1712 (owner decision 4): the model choice lives in the + menu, not on the composer's main row; the
 * server's default model stays the default. One row per model the server offers, in its order.
 */
export function modelActions(models: readonly Pick<ChatModel, 'id' | 'label'>[]): PlusAction[] {
  return models.map((model) => ({
    id: `model:${model.id}`,
    group: 'model',
    label: shortModelLabel(model),
    hint: modelBlurb(model) || 'A model for this chat',
    icon: 'model',
    keywords: ['model', 'claude', model.label.toLowerCase()],
    run: { kind: 'model', model: model.id },
  }));
}

/**
 * The Focus options for the products (#1547): "<Products> only" for each active product, "All
 * products" when there are several, then each product not ready yet, greyed "Coming later".
 */
export function focusProducts(catalog: ProductCatalog = productCatalog()): PlusAction[] {
  const active = activeProducts(catalog);
  const one = (product: ProductCatalog[number]): PlusAction => ({
    id: `product:${product.id}`,
    group: 'focus',
    label: product.active ? `${product.plural} only` : product.plural,
    hint: product.active ? `Checks and rules for ${product.plural.toLowerCase()}` : 'Coming later',
    icon: 'product',
    keywords: ['product', product.label, product.id.replace(/_/g, ' ')],
    run: { kind: 'products', products: [product.id], comingLater: !product.active },
  });
  const all: PlusAction[] =
    active.length > 1
      ? [{
          id: 'all_products',
          group: 'focus',
          label: active.length === 2 ? `${active[0].plural} and ${active[1].plural.toLowerCase()}` : 'All products',
          hint: active.length === 2 ? 'Both products' : `All ${active.length} products`,
          icon: 'both',
          keywords: ['product', 'both', 'all'],
          run: { kind: 'products', products: active.map((product) => product.id) },
        }]
      : [];
  return [...active.map(one), ...all, ...catalog.filter((product) => !product.active).map(one)];
}

/** Every option in the menu's order: the fixed ones, with the products' Focus options first in Focus. */
export function plusActions(catalog: ProductCatalog = productCatalog()): PlusAction[] {
  const focusAt = PLUS_ACTIONS.findIndex((action) => action.group === 'focus');
  const at = focusAt < 0 ? PLUS_ACTIONS.length : focusAt;
  return [...PLUS_ACTIONS.slice(0, at), ...focusProducts(catalog), ...PLUS_ACTIONS.slice(at)];
}

export function plusAction(id: string, catalog: ProductCatalog = productCatalog()): PlusAction | null {
  return plusActions(catalog).find((action) => action.id === id) ?? null;
}

/**
 * How long a full check takes, in minutes: measured at about 15 on a 60-page set (2026-10-17).
 * The one place to change it when the full check gets faster.
 */
export const FULL_CHECK_MINUTES = 15;

/**
 * The full check's estimate: its time, and up to the per-answer cap the server states
 * (`message_cap_usd` from GET /assistant/models, e.g. "$15").
 */
export function fullCheckEstimate(cap: string | null): string {
  const time = `~${FULL_CHECK_MINUTES} min`;
  return cap ? `${time} · up to ${cap}` : time;
}

// ── Focus ───────────────────────────────────────────────────

/** True when the chips already hold exactly these products. */
export function sameProducts(current: readonly ChatProduct[] | null, wanted: readonly ChatProduct[]): boolean {
  return Boolean(current) && current!.length === wanted.length && wanted.every((product) => current!.includes(product));
}

/**
 * The effort a Focus option sets: Quick is Low; Thorough is Extra high, or High where the model (or
 * thinking off) does not allow more. Null when the model offers neither.
 */
export function effortFor(models: readonly ChatModel[], choice: ChatChoice, level: 'quick' | 'thorough'): ChatEffort | null {
  const wanted: ChatEffort[] = level === 'quick' ? ['low', 'medium'] : ['xhigh', 'high'];
  for (const effort of wanted) {
    if (chooseEffort(models, choice, effort).effort === effort) return effort;
  }
  return null;
}

// ── What the last answer offers ─────────────────────────────

/** The newest finished answer with findings (the cloud drawing and the Excel are made from it). */
export function lastFindings(messages: readonly ViewMessage[]): { messageId: string; findings: ChatFinding[] } | null {
  for (let at = messages.length - 1; at >= 0; at -= 1) {
    const message = messages[at];
    if (message.role === 'assistant' && message.status === 'done' && message.id && message.findings?.length) {
      return { messageId: message.id, findings: message.findings };
    }
  }
  return null;
}

/** The areas this chat's answers named (for "Compare an area…"), first seen first, without repeats. */
export function knownAreas(messages: readonly ViewMessage[], most = 12): string[] {
  const seen = new Map<string, string>();
  for (const message of messages) {
    for (const finding of message.findings ?? []) {
      const area = areaOf(finding.item);
      if (area && !seen.has(area.toLowerCase())) seen.set(area.toLowerCase(), area);
    }
  }
  return [...seen.values()].slice(0, most);
}

/** The area prompt with its blank, for an area the reviewer types. */
export const AREA_PROMPT: string = PROMPTS.area;

/** The area prompt with the area filled in. */
export function areaPrompt(area: string): string {
  return PROMPTS.area.replace('[area]', area.trim());
}

// ── Recently used, remembered per browser ───────────────────

const RECENT_KEY = 'gv-chat-plus-recent';
/** How many recent options the menu shows first. */
export const RECENT_MOST = 3;

export function readRecent(storage: Pick<Storage, 'getItem'> | null): string[] {
  try {
    const raw = storage?.getItem(RECENT_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    // A product option is kept while the products load (the menu then shows only the ones it knows).
    return parsed.filter((id): id is string => typeof id === 'string' && (plusAction(id) !== null || id.startsWith('product:') || id === 'all_products')).slice(0, RECENT_MOST);
  } catch {
    return [];
  }
}

/** Put an option first in the recent list (and keep the list short). */
export function rememberRecent(storage: Pick<Storage, 'getItem' | 'setItem'> | null, id: string): string[] {
  const next = [id, ...readRecent(storage).filter((each) => each !== id)].slice(0, RECENT_MOST);
  try {
    storage?.setItem(RECENT_KEY, JSON.stringify(next));
  } catch {
    // Blocked storage (a private window): the list lasts for this visit only.
  }
  return next;
}
