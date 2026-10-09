import type { PackageSummary, Usage, UsageGroup } from '@/api/client';
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

/** "8 Oct" for a `2026-10-08` day (the API's days are UTC dates). */
export function dayLabel(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(Date.UTC(year, month - 1, date)).toLocaleDateString(undefined, { day: 'numeric', month: 'short', timeZone: 'UTC' });
}

export interface UsageKpis {
  /** Drawing sets with at least one AI call. */
  setsRead: number;
  calls: number;
  failedCalls: number;
  cost: string;
  /** Calls the API could not price: the cost shown leaves them out. */
  unpricedCalls: number;
}

export function usageKpis(totals: Usage['totals'], bySet: readonly UsageGroup[]): UsageKpis {
  return {
    setsRead: bySet.filter((group) => group.calls > 0).length,
    calls: totals.calls,
    failedCalls: totals.failed_calls,
    cost: totals.cost_usd,
    unpricedCalls: totals.unpriced_calls,
  };
}

export interface CostDay {
  day: string;
  label: string;
  cost: number;
  calls: number;
  failed: number;
}

export function costByDay(byDay: readonly UsageGroup[]): CostDay[] {
  return byDay
    .filter((group): group is UsageGroup & { day: string } => typeof group.day === 'string')
    .map((group) => ({ day: group.day, label: dayLabel(group.day), cost: Number(group.cost_usd), calls: group.calls, failed: group.failed_calls }))
    .sort((a, b) => a.day.localeCompare(b.day));
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
      models: group.models.map((model) => model.model),
    }));
}

export interface OutcomeDay extends SummaryOutcomes {
  day: string;
  label: string;
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
    const into = days.get(day) ?? { day, label: dayLabel(day), sets: 0, pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 };
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

/** Short model names for a table cell: `anthropic.claude-opus-5-5` → `claude-opus-5-5`. */
export function modelWord(model: string): string {
  return model.replace(/^[a-z]+\./, '').replace(/:\d+$/, '');
}
