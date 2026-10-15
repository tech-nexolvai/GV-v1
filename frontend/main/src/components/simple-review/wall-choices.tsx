import { CircleDashed } from 'lucide-react';

import type { ReviewItem, WallChoice } from '@/api/review-items';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { ChoiceCard } from './choice-card';

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
  if (choice.result) {
    return (
      <span className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-review-fg">
        <OutcomeIcon outcome="REVIEW_REQUIRED" size={14} />
        Needs a person
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <CircleDashed aria-hidden="true" className="size-3.5" />
      No result yet
    </span>
  );
}

/**
 * A countertop whose only open question is its walls (#1358): one click picks the walls, and each
 * choice shows the result the width check recorded for it. The AI readers' proposal is pre-selected
 * (the C key picks it). There is no right or wrong here: the checks run again with the walls chosen.
 */
export function WallChoices({
  item,
  busy,
  saving = null,
  onChoose,
}: {
  item: ReviewItem;
  /** True while an answer to this item is being saved: every choice waits. */
  busy: boolean;
  /** The layout being saved (clicked, or picked with the C key): only its card spins. */
  saving?: string | null;
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
        <p className="text-[13px] leading-5 text-muted-foreground">
          The vendor wrote <span className="num text-foreground">{item.vendor_wrote.display}</span>. Each choice shows the overall it needs and the result the width check recorded.
        </p>
      )}
      <ul className="grid gap-2">
        {choices.map((choice) => (
          <li key={choice.layout}>
            <ChoiceCard
              title={choice.words}
              detail={choice.needed ? <>Needs <span className="num text-foreground">{choice.needed.display}</span> overall</> : undefined}
              result={<ChoiceResult choice={choice} />}
              proposal={choice.proposed ? 'the AI readers' : null}
              leadsTo="The walls are recorded, and the width check runs again with them."
              disabled={!open || busy}
              busy={busy && saving === choice.layout}
              showKey={choice.proposed && open}
              onChoose={() => onChoose(choice.layout)}
            />
          </li>
        ))}
      </ul>
      {item.wall_results_note && <p className="text-[13px] leading-5 text-muted-foreground">{item.wall_results_note}</p>}
    </fieldset>
  );
}
