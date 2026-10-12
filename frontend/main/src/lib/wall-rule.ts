/**
 * GV's field-cut rule in plain words, and each wall choice's result (#1202). Kept free of React so
 * it can be tested alone.
 *
 * Every number shown comes from the API: the project's field cut (`field_cut_in_force`), and each
 * wall case's needed overall and result as the width check recorded them (`wall_cases`). Nothing is
 * added or subtracted here; a sum the check did not record is written as its parts ("+1" + 1"").
 */
import type { CountertopResult } from '@/api/client';
import type { components } from '@/api/schema';
import type { Outcome } from '@/data/types';

export type WallCases = components['schemas']['WallCasesOut'];
export type WallCase = components['schemas']['WallCaseOut'];

export const FIELD_CUT_RULE_LABEL = "About GV's field-cut rule";

/** "GV's field-cut rule: where the stone meets a wall, it is made 1" longer and trimmed on site to fit." */
export function fieldCutRuleSentence(fieldCut: string | null): string {
  const longer = fieldCut ? `${fieldCut} longer` : 'longer by the field cut';
  return `GV's field-cut rule: where the stone meets a wall, it is made ${longer} and trimmed on site to fit.`;
}

export interface RuleLine {
  /** The wall case, as the reviewer reads it. */
  walls: string;
  /** What it adds: "+1" + 1" = +2"", "+1"", "no field cut". */
  adds: string;
}

/** The three cases of the rule, with what each adds. A total is shown only when the check recorded it. */
export function fieldCutRuleLines(fieldCut: string | null, cases?: WallCases | null): RuleLine[] {
  const both = cases?.cases?.find((item) => item.field_cut_count === 2)?.field_cut_total?.display;
  return [
    {
      walls: 'Walls at both ends',
      adds: fieldCut ? `+${fieldCut} + ${fieldCut}${both ? ` = +${both}` : ''}` : 'the field cut at each end',
    },
    { walls: 'Wall at one end', adds: fieldCut ? `+${fieldCut}` : 'the field cut at that end' },
    { walls: 'No end walls (back wall only, island, or the stone stops at side panels)', adds: 'no field cut' },
  ];
}

/** '1" is this project's field cut (it can be changed per project).' */
export function fieldCutValueLine(fieldCut: string | null): string {
  return fieldCut
    ? `${fieldCut} is this project's field cut (it can be changed per project).`
    : "The field cut is set per project (1\" is GV's usual value).";
}

/** The needed overall for each field-cut count (2, 1, 0), from the recorded cases. */
function neededByCount(cases: WallCases): Map<number, string> {
  const needed = new Map<number, string>();
  for (const item of cases.cases ?? []) {
    if (item.field_cut_count !== null && item.expected_total && !needed.has(item.field_cut_count)) {
      needed.set(item.field_cut_count, item.expected_total.display);
    }
  }
  return needed;
}

/**
 * The row's own worked example, when its numbers are sealed and the check recorded every case:
 * 'Pieces add up to 94". Both ends → should be 96"; one end → 95"; no end walls → 94". Vendor wrote 95".'
 */
export function workedExample(cases: WallCases | null | undefined): string | null {
  if (!cases || !cases.cases?.length || !cases.pieces_add_up_to || !cases.vendor_wrote) return null;
  const needed = neededByCount(cases);
  const parts = [
    needed.has(2) ? `Both ends → should be ${needed.get(2)}` : null,
    needed.has(1) ? `one end → ${needed.get(1)}` : null,
    needed.has(0) ? `no end walls → ${needed.get(0)}` : null,
  ].filter((part): part is string => part !== null);
  if (parts.length === 0) return null;
  return `Pieces add up to ${cases.pieces_add_up_to.display}. ${parts.join('; ')}. Vendor wrote ${cases.vendor_wrote.display}.`;
}

export interface ChoiceResult {
  needed: string;
  outcome: Outcome;
}

/** One wall choice's recorded result, or null when the check recorded none for it. */
export function choiceResult(cases: WallCases | null | undefined, layout: string): ChoiceResult | null {
  const item = cases?.cases?.find((entry) => entry.layout === layout);
  if (!item || !item.expected_total) return null;
  return { needed: item.expected_total.display, outcome: item.outcome as Outcome };
}

/** The field cut to explain with: the one the row's result used, else the one a run would use now. */
export function fieldCutOf(row: Pick<CountertopResult, 'field_cut_per_end' | 'wall_cases' | 'field_cut_in_force'>): string | null {
  return row.field_cut_per_end?.display ?? row.wall_cases?.field_cut_per_end?.display ?? row.field_cut_in_force ?? null;
}
