import type { CountertopResult } from '@/api/client';
import { InfoTip } from '@/components/ui/info-tip';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { cn } from '@/lib/utils';
import {
  FIELD_CUT_RULE_LABEL,
  choiceResult,
  fieldCutOf,
  fieldCutRuleLines,
  fieldCutRuleSentence,
  fieldCutValueLine,
  workedExample,
  type WallCases,
} from '@/lib/wall-rule';

/**
 * The "i" beside walls and the field cut (#1202): GV's field-cut rule in plain words, its three
 * cases, the project's field cut, and — when the row's numbers are sealed — the row's own worked
 * example. A popover (keyboard and touch), never a hover-only tooltip.
 */
export function WallRuleInfo({ fieldCut, cases }: { fieldCut: string | null | undefined; cases?: WallCases | null }) {
  const cut = fieldCut ?? null;
  const example = workedExample(cases);
  return (
    <InfoTip label={FIELD_CUT_RULE_LABEL} icon="i">
      <div data-slot="wall-rule-info" className="flex flex-col gap-2">
        <p>{fieldCutRuleSentence(cut)}</p>
        <table className="w-full text-xs">
          <caption className="sr-only">What each wall case adds</caption>
          <tbody>
            {fieldCutRuleLines(cut, cases).map((line) => (
              <tr key={line.walls} className="border-t first:border-t-0">
                <th scope="row" className="py-1 pr-2 text-left font-normal">{line.walls}</th>
                <td className="num py-1 text-right whitespace-nowrap">{line.adds}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="text-xs text-muted-foreground">{fieldCutValueLine(cut)}</p>
        {example && <p data-slot="wall-rule-example" className="text-xs">{example}</p>}
      </div>
    </InfoTip>
  );
}

/**
 * One wall choice's recorded result (#1202): the overall it needs and the outcome against the
 * vendor's printed overall, glyph and word together. Nothing when the check recorded none.
 */
export function WallChoiceResult({ cases, layout }: { cases?: WallCases | null; layout: string }) {
  const result = choiceResult(cases, layout);
  if (!result) return null;
  return (
    <span data-slot="wall-choice-result" className="inline-flex flex-wrap items-center gap-x-1.5 text-xs text-muted-foreground">
      <span>
        should be <span className="num text-foreground">{result.needed}</span>
      </span>
      <span
        className={cn(
          'inline-flex items-center gap-1 font-medium',
          result.outcome === 'PASS' && 'text-outcome-pass-fg',
          result.outcome === 'FAIL' && 'text-outcome-fail-fg',
        )}
      >
        <OutcomeIcon outcome={result.outcome} size={12} />
        {OUTCOME_LABELS[result.outcome]}
      </span>
    </span>
  );
}

/**
 * The results line (#1202): which walls the result used and who decided — "walls at both ends —
 * seen on the drawing at both ends and confirmed by both AIs", "… — chosen by <reviewer>", "not
 * needed: wrong for every wall case" — with the rule's "i". Says "not set" while nobody has.
 */
export function WallsLine({ row, className }: { row: CountertopResult; className?: string }) {
  const line = row.wall_layout.line ?? (row.wall_layout.source === 'not established' ? 'not set yet' : null);
  return (
    <p data-slot="walls-line" className={cn('flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground', className)}>
      <span>
        Walls: <span className="text-foreground">{line ?? row.wall_layout.label ?? 'not set yet'}</span>
      </span>
      <WallRuleInfo fieldCut={fieldCutOf(row)} cases={row.wall_cases} />
    </p>
  );
}
