import { useState } from 'react';

import type { ChatPlanDoubt } from '@/lib/chat-api';
import { readingWords, type PlanState } from '@/lib/chat-plan-check';
import { cn } from '@/lib/utils';

/**
 * The plan check before a paid full check (#1569, research idea 3). Code reads the set's page map
 * and asks 0-3 one-click questions only where it is unsure ("Page 28: is the lower drawing the
 * brand prototype or the architect's?"), each with the page's picture and the drawing marked. With
 * nothing to ask, the popover shows code's plan in one line and Run goes straight on: the
 * one-button path stays. Answering is optional; an answered question is not asked again.
 */

function PagePicture({ doubt }: { doubt: ChatPlanDoubt }) {
  const [broken, setBroken] = useState(false);
  if (!doubt.picture_url || broken) {
    return (
      <span aria-hidden="true" className="grid h-[72px] w-[56px] shrink-0 place-items-center rounded-md border border-dashed bg-muted/40 text-xs text-muted-foreground tabular-nums">
        p.{doubt.page}
      </span>
    );
  }
  const box = doubt.box;
  // The page picture is always white (a drawing), so the mark is a fixed dark ring in both themes.
  return (
    <span className="relative block h-[72px] w-[56px] shrink-0 overflow-hidden rounded-md border bg-white">
      <img src={doubt.picture_url} alt={`Page ${doubt.page}`} onError={() => setBroken(true)} className="h-full w-full object-contain" />
      {box && (
        <span
          aria-hidden="true"
          data-slot="chat-plan-box"
          className="absolute rounded-[2px] ring-2 ring-neutral-900"
          style={{ left: `${box[0] * 100}%`, top: `${box[1] * 100}%`, width: `${(box[2] - box[0]) * 100}%`, height: `${(box[3] - box[1]) * 100}%` }}
        />
      )}
    </span>
  );
}

export interface PlanCheckProps {
  state: PlanState;
  picked: Readonly<Record<string, string>>;
  onPick: (doubtId: string, value: string) => void;
}

/** Code's plan line and its questions, inside the Run popover. Renders nothing without a plan. */
export function PlanCheck({ state, picked, onPick }: PlanCheckProps) {
  if (state.status === 'idle' || state.status === 'unavailable') return null;
  if (state.status === 'loading') {
    return (
      <p data-slot="chat-plan-loading" role="status" className="text-xs text-muted-foreground">
        Reading the set’s pages…
      </p>
    );
  }
  const { plan } = state;
  const reading = readingWords(plan.reading);
  return (
    <div data-slot="chat-plan-check" className="grid gap-2">
      {reading && (
        <p data-slot="chat-plan-reading" role="status" className="text-xs text-muted-foreground tabular-nums">
          {reading}
        </p>
      )}
      {plan.summary && (
        <p data-slot="chat-plan-summary" className="text-xs text-muted-foreground tabular-nums">
          Plan: {plan.summary}
        </p>
      )}
      {plan.doubts.length > 0 && (
        <ul aria-label="Before it runs" className="grid gap-2">
          {plan.doubts.map((doubt) => (
            <li key={doubt.id} data-slot="chat-plan-doubt" data-id={doubt.id} className="flex gap-2.5 rounded-lg border bg-muted/30 p-2">
              <PagePicture doubt={doubt} />
              <div className="grid min-w-0 content-start gap-1.5">
                <p className="text-[13px] leading-snug">{doubt.question}</p>
                <div role="group" aria-label={doubt.question} className="flex flex-wrap gap-1">
                  {doubt.options.map((option) => {
                    const on = picked[doubt.id] === option.value;
                    return (
                      <button
                        key={option.value}
                        type="button"
                        aria-pressed={on}
                        onClick={() => onPick(doubt.id, option.value)}
                        className={cn(
                          'inline-flex h-7 items-center rounded-full border px-2.5 text-xs font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none',
                          on ? 'border-foreground bg-foreground text-background' : 'bg-background text-muted-foreground hover:bg-accent hover:text-foreground',
                        )}
                      >
                        {option.label}
                      </button>
                    );
                  })}
                </div>
              </div>
            </li>
          ))}
        </ul>
      )}
      {plan.doubts.length > 0 && (
        <p data-slot="chat-plan-optional" className="text-xs text-muted-foreground">
          Optional: skip these and GV Review takes the first answer itself{plan.more > 0 ? `, here and in ${plan.more} more ${plan.more === 1 ? 'place' : 'places'}` : ''}, and notes each one on the cloud drawing.
        </p>
      )}
    </div>
  );
}
