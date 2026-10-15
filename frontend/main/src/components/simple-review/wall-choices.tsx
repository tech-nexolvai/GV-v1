import { Loader2 } from 'lucide-react';

import type { ReviewItem, WallChoice } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Kbd } from './words';

/** What one wall choice would give, as the width check recorded it: a glyph and a word, always. */
function ChoiceResult({ choice }: { choice: WallChoice }) {
  if (choice.result === 'PASS') {
    return (
      <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-pass-fg">
        <OutcomeIcon outcome="PASS" size={14} />
        Right
      </span>
    );
  }
  if (choice.result === 'FAIL') {
    return (
      <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-fail-fg">
        <OutcomeIcon outcome="FAIL" size={14} />
        Wrong
      </span>
    );
  }
  return <span className="text-[13px] text-muted-foreground">{choice.result ? 'Needs a person' : 'No result yet'}</span>;
}

/**
 * A countertop whose only open question is its walls (#1358): one click picks the walls, and each
 * choice shows the result the width check recorded for it. The AI readers' proposal is pre-selected
 * (the C key picks it). There is no right or wrong here: the checks run again with the walls chosen.
 */
export function WallChoices({
  item,
  busy,
  onChoose,
}: {
  item: ReviewItem;
  /** True while a wall choice is being saved. */
  busy: boolean;
  onChoose: (layout: string) => void;
}) {
  const choices = item.wall_choices ?? [];
  const open = item.status === 'open' && item.actions.includes('choose_wall');
  if (choices.length === 0) {
    return <p className="text-[15px] leading-6 text-foreground">{item.what}. Open the advanced view to choose the walls.</p>;
  }
  return (
    <fieldset data-slot="wall-choices" className="flex min-w-0 flex-col gap-3">
      <legend className="mb-1 text-[15px] leading-6 font-medium text-foreground">{item.what}</legend>
      {item.vendor_wrote && (
        <p className="text-[13px] text-muted-foreground">
          The vendor wrote <span className="num text-foreground">{item.vendor_wrote.display}</span>. Each choice shows the overall it needs and the result.
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {choices.map((choice) => (
          <li key={choice.layout}>
            <button
              type="button"
              data-proposed={choice.proposed || undefined}
              aria-pressed={choice.proposed}
              disabled={!open || busy}
              onClick={() => onChoose(choice.layout)}
              className={cn(
                'flex min-h-12 w-full flex-wrap items-center gap-x-4 gap-y-1 rounded-lg border px-4 py-2 text-left transition-colors duration-150 motion-reduce:transition-none',
                'focus-visible:ring-ring/50 outline-none focus-visible:ring-[3px] disabled:cursor-not-allowed disabled:opacity-60',
                choice.proposed ? 'border-foreground/60 bg-background' : 'border-border bg-background hover:bg-muted/60',
              )}
            >
              <span className="min-w-0 flex-1 text-[15px] text-foreground">
                {choice.words}
                {choice.proposed && <span className="ml-2 text-[13px] text-muted-foreground">AI's proposal</span>}
              </span>
              {choice.needed && (
                <span className="text-[13px] text-muted-foreground">
                  needs <span className="num text-foreground">{choice.needed.display}</span>
                </span>
              )}
              <span className="w-28 shrink-0">
                <ChoiceResult choice={choice} />
              </span>
              {choice.proposed && open && (busy ? <Loader2 aria-hidden="true" className="size-4 animate-spin motion-reduce:animate-none" /> : <Kbd>C</Kbd>)}
            </button>
          </li>
        ))}
      </ul>
      {item.wall_results_note && <p className="text-[13px] leading-5 text-muted-foreground">{item.wall_results_note}</p>}
    </fieldset>
  );
}
