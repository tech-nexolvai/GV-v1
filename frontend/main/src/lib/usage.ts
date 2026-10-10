import type { EarlierRun, ModelSpend, PackageSummary, Usage, UsageGroup } from '@/api/client';
import type { SummaryOutcomes } from '@/lib/documents-table';

/**
 * The Usage screen's arithmetic (#1072), out of React so it can be tested on its own. Everything is
 * counted from `GET /usage` (AI calls) and `GET /packages-summary` (names and recorded results).
 */

/** "$1.72"; "< $0.01" for a cost too small to show in cents; the exact figure goes in a title. */
export function formatUsd(cost: string): string {
  const value = Number(cost);
  if (!Number.isFinite(value)) return `$${cost}`;
  if (value === 0) return '$0.00';
  if (value < 0.01) return '< $0.01';
  return `$${value.toFixed(2)}`;
}

/**
 * What a cost is, said honestly (#1072 review): "Not priced" when no call has a price; "at least
 * $X" when some calls have none (the figure leaves them out); otherwise the cost itself.
 */
export function costText(cost: string, calls: number, unpriced: number): string {
  if (calls > 0 && unpriced >= calls) return 'Not priced';
  if (unpriced > 0) return `at least ${formatUsd(cost)}`;
  return formatUsd(cost);
}

function utcDay(day: string): Date {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date));
}

/** "8 Oct" for a `2026-10-08` day (the API's days are UTC dates). */
export function dayLabel(day: string): string {
  return utcDay(day).toLocaleDateString(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

/** "8 Oct 2026", for tables and tooltips. */
export function fullDayLabel(day: string): string {
  return utcDay(day).toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric', timeZone: 'UTC' });
}

/** "8 Oct" for a day in this UTC year, "8 Oct 2025" otherwise: short enough for a phone row. */
export function shortDayLabel(day: string, now: Date = new Date()): string {
  return utcDay(day).getUTCFullYear() === now.getUTCFullYear() ? dayLabel(day) : fullDayLabel(day);
}

export interface UsageKpis {
  /** Drawing sets with at least one AI call (reading or chat, failed or not): not "sets read". */
  setsWithCalls: number;
  calls: number;
  failedCalls: number;
  cost: string;
  /** Calls the API could not price: the cost shown leaves them out. */
  unpricedCalls: number;
}

export function usageKpis(totals: Usage['totals'], bySet: readonly UsageGroup[]): UsageKpis {
  return {
    setsWithCalls: bySet.filter((group) => group.calls > 0).length,
    calls: totals.calls,
    failedCalls: totals.failed_calls,
    cost: totals.cost_usd,
    unpricedCalls: totals.unpriced_calls,
  };
}

export interface CostDay {
  day: string;
  label: string;
  fullLabel: string;
  cost: number;
  calls: number;
  failed: number;
  /** Calls with no price: that day's cost leaves them out. */
  unpriced: number;
}

/** Days between the first and last with calls are filled with zero, up to this many days. */
const FILL_DAYS = 62;

/**
 * Cost per UTC day, oldest first. Days with no calls between the first and the last are included at
 * zero (so the bars are not side by side across a gap), unless the range is longer than two months.
 */
export function costByDay(byDay: readonly UsageGroup[]): CostDay[] {
  const days = byDay
    .filter((group): group is UsageGroup & { day: string } => typeof group.day === 'string')
    .map((group) => ({
      day: group.day, label: dayLabel(group.day), fullLabel: fullDayLabel(group.day),
      cost: Number(group.cost_usd), calls: group.calls, failed: group.failed_calls, unpriced: group.unpriced_calls,
    }))
    .sort((a, b) => a.day.localeCompare(b.day));
  if (days.length < 2) return days;
  const first = utcDay(days[0].day).getTime();
  const last = utcDay(days[days.length - 1].day).getTime();
  const span = Math.round((last - first) / 86_400_000) + 1;
  if (span > FILL_DAYS) return days;
  const byKey = new Map(days.map((day) => [day.day, day]));
  return Array.from({ length: span }, (_, index) => {
    const key = new Date(first + index * 86_400_000).toISOString().slice(0, 10);
    return byKey.get(key) ?? { day: key, label: dayLabel(key), fullLabel: fullDayLabel(key), cost: 0, calls: 0, failed: 0, unpriced: 0 };
  });
}

export interface SetUsage {
  packageId: string;
  /** The vendor, from `packages-summary`; null when that set is not on the loaded summary page. */
  vendor: string | null;
  calls: number;
  failed: number;
  inputTokens: number;
  outputTokens: number;
  cost: string;
  /** Calls with no price: the cost leaves them out. */
  unpriced: number;
  models: string[];
}

export function usageBySet(bySet: readonly UsageGroup[], summaries: readonly PackageSummary[]): SetUsage[] {
  const vendors = new Map(summaries.map((summary) => [summary.package_id, summary.vendor ?? 'Untitled document set']));
  return bySet
    .filter((group): group is UsageGroup & { package_id: string } => typeof group.package_id === 'string')
    .map((group) => ({
      packageId: group.package_id,
      vendor: vendors.get(group.package_id) ?? null,
      calls: group.calls,
      failed: group.failed_calls,
      inputTokens: group.input_tokens,
      outputTokens: group.output_tokens,
      cost: group.cost_usd,
      unpriced: group.unpriced_calls,
      models: group.models.map((model) => model.model),
    }));
}

export interface OutcomeDay extends SummaryOutcomes {
  day: string;
  label: string;
  fullLabel: string;
  sets: number;
}

/**
 * Recorded results of each drawing set, added up by the UTC day the set was uploaded. Recorded
 * results, as on Documents: a reviewer's decision does not change them.
 */
export function outcomesByUploadDay(summaries: readonly PackageSummary[]): OutcomeDay[] {
  const days = new Map<string, OutcomeDay>();
  for (const summary of summaries) {
    const day = summary.created_at.slice(0, 10);
    const into = days.get(day) ?? { day, label: dayLabel(day), fullLabel: fullDayLabel(day), sets: 0, pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 };
    into.sets += 1;
    into.pass += summary.outcomes.pass;
    into.fail += summary.outcomes.fail;
    into.review += summary.outcomes.review;
    into.not_found += summary.outcomes.not_found;
    into.no_rule += summary.outcomes.no_rule;
    days.set(day, into);
  }
  return [...days.values()].sort((a, b) => a.day.localeCompare(b.day));
}

/** Short model names for a table cell: `us.anthropic.claude-opus-5-5` → `claude-opus-5-5`. */
export function modelWord(model: string): string {
  return model.replace(/^([a-z]+\.)+/, '').replace(/:\d+$/, '');
}

/** Recorded results over all the days shown, for the visible totals beside the chart. */
export function outcomeTotals(days: readonly OutcomeDay[]): SummaryOutcomes {
  return days.reduce(
    (sum, day) => ({ pass: sum.pass + day.pass, fail: sum.fail + day.fail, review: sum.review + day.review, not_found: sum.not_found + day.not_found, no_rule: sum.no_rule + day.no_rule }),
    { pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 },
  );
}

/** Model ids in words (#1165); null for a model with no known name (the id is shown instead). */
const MODEL_NAMES: readonly [RegExp, string][] = [
  [/claude-opus-5-5/, 'Claude Opus 5.5'],
  [/claude-sonnet-5-5/, 'Claude Sonnet 5.5'],
  [/claude-haiku-4-5/, 'Claude Haiku 4.5'],
  [/nova-2-lite/, 'Amazon Nova 2 Lite'],
  [/nova-pro/, 'Amazon Nova Pro'],
  [/nova-lite/, 'Amazon Nova Lite'],
  [/qwen3-vl-235b/, 'Qwen3 VL 235B'],
  [/kimi-k3/, 'Kimi K3'],
  [/mistral-large-3/, 'Mistral Large 3'],
  [/ministral-3-3b/, 'Ministral 3 3B'],
];

export function modelName(model: string): string | null {
  return MODEL_NAMES.find(([pattern]) => pattern.test(model))?.[1] ?? null;
}

const ROUTE_WORDS: Record<ModelSpend['route'], string> = {
  bedrock: 'Amazon Bedrock',
  openrouter: 'OpenRouter',
  anthropic: 'Anthropic API',
  unknown: 'Not recorded',
};

/** Which provider the calls went through, in words; "Not recorded" when the records do not say. */
export function routeWord(route: ModelSpend['route']): string {
  return ROUTE_WORDS[route];
}

const PURPOSE_WORDS: Record<EarlierRun['purpose'], string> = {
  reading: 'Reading drawings',
  'row-choice': 'Choosing countertop rows',
  chat: 'Reviewer chat',
  assistant: 'Review assistant',
  findings: 'Findings wording',
  'bake-off': 'Model comparison',
  other: 'Other',
};

export function purposeWord(purpose: EarlierRun['purpose']): string {
  return PURPOSE_WORDS[purpose];
}

/** "12,345" tokens in and out, as one short figure: "1.2M" above a million, "12.3k" above 10k. */
export function compactCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}k`;
  return value.toLocaleString();
}
