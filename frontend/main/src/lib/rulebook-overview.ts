import type { Rule } from '@/api/client';
import { productWord } from '@/lib/documents-table';

/**
 * The Rulebook overview (#1072), out of React so it can be tested on its own. Everything here is
 * counted from `GET /rules`; nothing is assumed about which products or check types exist.
 */

/** Where a rule's two sides come from (`rules/schema.py` `CheckType`), in words. */
const CHECK_TYPE_WORDS: Record<string, string> = {
  internal: 'Shop drawing only',
  arch_vs_shop: 'Architect vs shop',
  global: 'Against a standard',
};
const CHECK_TYPE_ORDER = ['internal', 'arch_vs_shop', 'global'];

export function checkTypeWord(checkType: string): string {
  return CHECK_TYPE_WORDS[checkType] ?? productWord(checkType.replace(/_/g, ' '));
}

export interface RuleGrid {
  products: string[];
  checkTypes: string[];
  /** `count(product, checkType)`; 0 where no rule is published. */
  count: (product: string, checkType: string) => number;
  byProduct: (product: string) => number;
  byCheckType: (checkType: string) => number;
  total: number;
}

/** Products × check types, both taken from the rules themselves (known check types first). */
export function ruleGrid(rules: readonly Rule[]): RuleGrid {
  const products = [...new Set(rules.map((rule) => rule.product_type))].sort();
  const present = new Set(rules.map((rule) => rule.check_type));
  const checkTypes = [
    ...CHECK_TYPE_ORDER.filter((type) => present.has(type)),
    ...[...present].filter((type) => !CHECK_TYPE_ORDER.includes(type)).sort(),
  ];
  const counts = new Map<string, number>();
  for (const rule of rules) {
    const key = `${rule.product_type}\u0000${rule.check_type}`;
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return {
    products,
    checkTypes,
    count: (product, checkType) => counts.get(`${product}\u0000${checkType}`) ?? 0,
    byProduct: (product) => rules.filter((rule) => rule.product_type === product).length,
    byCheckType: (checkType) => rules.filter((rule) => rule.check_type === checkType).length,
    total: rules.length,
  };
}

/** Severity counts, most first: `[['FLAG', 9]]` today ("flag everything, no severity split yet"). */
export function severityCounts(rules: readonly Rule[]): [string, number][] {
  const counts = new Map<string, number>();
  for (const rule of rules) counts.set(rule.severity, (counts.get(rule.severity) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
}

/** The release note every rule shares, to show once; null when they differ (or there are none). */
export function sharedReleaseNote(rules: readonly Rule[]): string | null {
  const notes = new Set(rules.map((rule) => rule.release_note.trim()));
  if (rules.length < 2 || notes.size !== 1) return null;
  const [note] = notes;
  return note === '' ? null : note;
}

export interface RuleFilter {
  product: string;
  checkType: string | null;
}

export function matchesRuleFilter(rule: Rule, filter: RuleFilter | null): boolean {
  if (filter === null) return true;
  return rule.product_type === filter.product && (filter.checkType === null || rule.check_type === filter.checkType);
}
