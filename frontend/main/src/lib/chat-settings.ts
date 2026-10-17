/**
 * The composer's three choices (#1464): model, effort and thinking, with the API's rules.
 *
 * - A model that always thinks (Opus 5.5) keeps thinking on: turning it off is refused by the API.
 * - With thinking off, effort may go no higher than the model's `thinking_off_max_effort` (High).
 * - Only the efforts the model lists can be picked.
 *
 * The choice is remembered per browser. Storage can be missing or throw (a private window, blocked
 * site data), so every read and write is guarded, and a remembered choice is checked against the
 * server's models before it is used: a model the server no longer offers falls back to its default.
 */

import type { ChatEffort, ChatModel } from '@/lib/chat-api';

export interface ChatChoice {
  model: string;
  effort: ChatEffort;
  thinking: boolean;
}

/** Every effort, lowest first. */
export const EFFORTS: readonly ChatEffort[] = ['low', 'medium', 'high', 'xhigh', 'max'];

export const EFFORT_WORDS: Record<ChatEffort, string> = {
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max',
};

export function effortRank(effort: ChatEffort): number {
  return EFFORTS.indexOf(effort);
}

/** The model's name without "Claude " ("Opus 5.5"), for the composer's small menu button. */
export function shortModelLabel(model: Pick<ChatModel, 'label'>): string {
  return model.label.replace(/^Claude\s+/, '');
}

/** The words beside a locked Thinking switch: "Opus 5.5 always thinks". */
export function alwaysThinksWords(model: Pick<ChatModel, 'label'>): string {
  return `${shortModelLabel(model)} always thinks`;
}

/**
 * The one line under each model in the model menu, in claude.ai's words. Fable 5.1 says its price
 * honestly (#1577): $10 / $50 per million tokens against Opus 5.5's $4 / $20.
 */
export function modelBlurb(model: Pick<ChatModel, 'id' | 'label'>): string {
  const name = `${model.id} ${model.label}`.toLowerCase();
  if (name.includes('fable')) return 'Hardest tasks · about 2.5× Opus cost';
  if (name.includes('opus')) return 'Most capable for complex work';
  if (name.includes('sonnet')) return 'Smart, efficient model for everyday use';
  if (name.includes('haiku')) return 'Fastest model for daily tasks';
  return '';
}

/** The server's default model, else its first; null when it offers none. */
export function defaultModel(models: readonly ChatModel[]): ChatModel | null {
  return models.find((model) => model.default) ?? models[0] ?? null;
}

/** With thinking off, the highest effort this model allows; null when there is no limit. */
function thinkingOffCap(model: ChatModel): ChatEffort | null {
  return model.thinking === 'always' ? null : model.thinking_off_max_effort;
}

/**
 * Why an effort cannot be picked now, in plain words, or null when it can. An effort the model does
 * not list cannot be picked; one above the thinking-off limit needs thinking on.
 */
export function effortBlockedReason(model: ChatModel, effort: ChatEffort, thinking: boolean): string | null {
  if (!model.efforts.includes(effort)) return `${shortModelLabel(model)} does not offer this effort`;
  const cap = thinkingOffCap(model);
  if (!thinking && cap !== null && effortRank(effort) > effortRank(cap)) return `Needs extended thinking (up to ${EFFORT_WORDS[cap]} without it)`;
  return null;
}

/** An effort the model allows with this thinking setting: the wanted one, else the nearest allowed below it. */
function allowedEffort(model: ChatModel, wanted: ChatEffort, thinking: boolean): ChatEffort {
  if (effortBlockedReason(model, wanted, thinking) === null) return wanted;
  const fallback = model.efforts.includes(model.default_effort) ? model.default_effort : model.efforts[0] ?? wanted;
  const start = model.efforts.includes(wanted) ? wanted : fallback;
  // Step down from the wanted (or default) effort until one is allowed.
  for (let rank = effortRank(start); rank >= 0; rank -= 1) {
    const effort = EFFORTS[rank];
    if (effortBlockedReason(model, effort, thinking) === null) return effort;
  }
  return fallback;
}

/**
 * A full, valid choice from a wanted (possibly remembered, possibly stale) one. Null only when the
 * server offers no model at all.
 */
export function resolveChoice(models: readonly ChatModel[], wanted: Partial<ChatChoice> | null): ChatChoice | null {
  const model = models.find((candidate) => candidate.id === wanted?.model) ?? defaultModel(models);
  if (!model) return null;
  // A different model than the remembered one starts on its own default effort.
  const sameModel = wanted?.model === model.id;
  const thinking = model.thinking === 'always' ? true : typeof wanted?.thinking === 'boolean' ? wanted.thinking : true;
  const effort = sameModel && wanted?.effort && EFFORTS.includes(wanted.effort) ? wanted.effort : model.default_effort;
  return { model: model.id, effort: allowedEffort(model, effort, thinking), thinking };
}

/** Pick a model: its own default effort, and thinking on when it always thinks. */
export function chooseModel(models: readonly ChatModel[], current: ChatChoice, modelId: string): ChatChoice {
  const model = models.find((candidate) => candidate.id === modelId);
  if (!model) return current;
  const thinking = model.thinking === 'always' ? true : current.thinking;
  const wanted = model.efforts.includes(current.effort) ? current.effort : model.default_effort;
  return { model: model.id, effort: allowedEffort(model, wanted, thinking), thinking };
}

/** Pick an effort. One the rules do not allow now is ignored. */
export function chooseEffort(models: readonly ChatModel[], current: ChatChoice, effort: ChatEffort): ChatChoice {
  const model = models.find((candidate) => candidate.id === current.model);
  if (!model || effortBlockedReason(model, effort, current.thinking) !== null) return current;
  return { ...current, effort };
}

/** Turn thinking on or off. A model that always thinks stays on; turning it off caps the effort. */
export function chooseThinking(models: readonly ChatModel[], current: ChatChoice, on: boolean): ChatChoice {
  const model = models.find((candidate) => candidate.id === current.model);
  if (!model || model.thinking === 'always') return { ...current, thinking: true };
  return { ...current, thinking: on, effort: allowedEffort(model, current.effort, on) };
}

// ── Remembered per browser ──────────────────────────────────

export const CHOICE_KEY = 'gv.chat.choice';

/** The browser's localStorage, or null where reading it throws. */
export function browserStorage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The remembered choice, or null. Anything unreadable is ignored, never thrown. */
export function readChoice(storage: Pick<Storage, 'getItem'> | null): Partial<ChatChoice> | null {
  if (!storage) return null;
  try {
    const raw = storage.getItem(CHOICE_KEY);
    if (!raw) return null;
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object') return null;
    const { model, effort, thinking } = value as Record<string, unknown>;
    return {
      ...(typeof model === 'string' ? { model } : {}),
      ...(typeof effort === 'string' && (EFFORTS as readonly string[]).includes(effort) ? { effort: effort as ChatEffort } : {}),
      ...(typeof thinking === 'boolean' ? { thinking } : {}),
    };
  } catch {
    return null;
  }
}

export function saveChoice(storage: Pick<Storage, 'setItem'> | null, choice: ChatChoice): void {
  try {
    storage?.setItem(CHOICE_KEY, JSON.stringify(choice));
  } catch {
    // Storage blocked or full: the choice lasts for this visit only.
  }
}
