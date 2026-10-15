/**
 * GV's field-cut rule in plain words, and each wall choice's result (#1202). Kept free of React so
 * it can be tested alone.
 *
 * Every number shown comes from the API: the project's field cut (`field_cut_in_force`), and each
 * wall case's needed overall and result as the width check recorded them (`wall_cases`). Nothing is
 * added or subtracted here; a sum the check did not record is written as its parts ("+1" + 1"").
 */
import type { CountertopResult, ExactValue } from '@/api/client';
import type { components } from '@/api/schema';
import type { Outcome } from '@/data/types';
import { signOf } from '@/lib/countertop-results';

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
    ? `${fieldCut} is this project's field cut now (it can be changed per project).`
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

/**
 * The field cut to explain with (#1202 review): the project's value now, as a run would use it, so
 * the "i" never shows an old value; else the one the row's result used.
 */
export function fieldCutOf(row: Pick<CountertopResult, 'field_cut_per_end' | 'wall_cases' | 'field_cut_in_force'>): string | null {
  return row.field_cut_in_force ?? row.field_cut_per_end?.display ?? row.wall_cases?.field_cut_per_end?.display ?? null;
}

/**
 * What a row needed while its walls are not set (#1329), from the wall cases the width check
 * recorded — never worked out here:
 *
 * - `same`: every case has the row's own result (wrong, or right, whatever the walls). Needed is the
 *   cases' range ('39 5/8" – 41 5/8"'), the difference is the closest gap with its direction
 *   ('at least 1/8" short'); one value when every case needs the same overall.
 * - `mixed`: the cases disagree, so the walls are still needed; each case's needed overall is listed.
 *
 * Null when the table's usual Needed and Difference apply: the result used one wall case (walls
 * set or used), or there are no recorded cases (held, not checked, or the field cut changed since).
 */
export type NeededAcrossWalls =
  | { kind: 'same'; needed: string; difference: string; outcome: 'PASS' | 'FAIL'; least: ExactValue; gap: ExactValue }
  | { kind: 'mixed'; needed: string[]; cases: { words: string; needed: string; outcome: Outcome }[] };

type NeededRow = Pick<CountertopResult, 'expected_total' | 'outcome' | 'wall_layout' | 'wall_cases'>;

/**
 * Walls a result is checked with: the reviewer's, or seen in the code ink and confirmed by both AIs.
 * Any other source (not set, not needed, or only proposed by the AIs, the drawing's clues or the
 * side panels) leaves the walls unconfirmed, and the check then records every wall case.
 */
const WALLS_USED: ReadonlySet<CountertopResult['wall_layout']['source']> = new Set(['reviewer', 'code ink and both AIs']);

export function neededAcrossWalls(row: NeededRow): NeededAcrossWalls | null {
  // Walls chosen after an every-case run keep the table's usual "—" until the checks run again.
  if (row.expected_total || WALLS_USED.has(row.wall_layout.source)) return null;
  const recorded = row.wall_cases;
  const cases = (recorded?.cases ?? []).filter((item) => item.expected_total);
  if (!recorded || cases.length === 0) return null;
  const outcome = row.outcome;
  const same = (outcome === 'PASS' || outcome === 'FAIL') && (recorded.cases ?? []).every((item) => item.outcome === outcome);
  if (same) {
    const least = recorded.needed_least;
    const most = recorded.needed_most;
    const gap = recorded.closest_gap;
    const differences = cases.map((item) => item.difference);
    if (!least || !most || !gap || differences.some((item) => !item)) return null;
    const one = least.numerator === most.numerator && least.denominator === most.denominator;
    const signs = new Set(differences.map((item) => signOf(item as ExactValue)));
    // "At least" only when the cases need different overalls. A vendor's overall that falls between
    // the cases is short of some and over others: the gap is then "off", with no one direction.
    const atLeast = one ? '' : 'at least ';
    const direction = signs.size !== 1 ? 'off' : signs.has(-1) ? 'short' : 'over';
    return {
      kind: 'same',
      needed: one ? least.display : `${least.display} – ${most.display}`,
      difference: signOf(gap) === 0 ? gap.display : `${atLeast}${gap.display} ${direction}`,
      outcome,
      least,
      // For sorting only, beside the row's own signed difference: short below zero.
      gap: direction === 'short' ? { ...gap, numerator: `-${gap.numerator}` } : gap,
    };
  }
  return {
    kind: 'mixed',
    needed: [...new Set(cases.map((item) => item.expected_total!.display))],
    cases: cases.map((item) => ({ words: item.words, needed: item.expected_total!.display, outcome: item.outcome as Outcome })),
  };
}
