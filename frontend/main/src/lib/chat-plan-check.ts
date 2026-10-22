import { useEffect, useState } from 'react';

import type { ChatFullCheckPlan, ChatPlanAnswer, ChatPlanDoubt } from '@/lib/chat-api';

/**
 * The plan check before a paid full check (#1569): code's plan and its 0-3 questions, read from
 * `GET /packages/{id}/full-check-plan` when the Run popover opens, and asked again while the set's
 * pages are still being read. The screen part is `components/chat/chat-plan-check.tsx`.
 */

/** How often to ask again while the set's pages are still being read, and how many times. */
export const PLAN_POLL_MS = 2000;
export const PLAN_POLL_TRIES = 15;

export type PlanState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'ready'; plan: ChatFullCheckPlan }
  | { status: 'unavailable' };

/** The plan, fetched when `active` turns on; `idle` while it is off. */
export function usePlanCheck(load: (() => Promise<ChatFullCheckPlan>) | undefined, active: boolean, pollMs: number = PLAN_POLL_MS): PlanState {
  const [result, setResult] = useState<PlanState | null>(null);
  useEffect(() => {
    if (!active || !load) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let tries = 0;
    const ask = () => {
      tries += 1;
      load()
        .then((plan) => {
          if (stopped) return;
          if (plan.state === 'building' && tries < PLAN_POLL_TRIES) {
            timer = setTimeout(ask, pollMs);
            return;
          }
          setResult(plan.state === 'ready' ? { status: 'ready', plan } : { status: 'unavailable' });
          // #1871: while the drawings are still being read, ask again so the count moves.
          if (plan.state === 'ready' && plan.reading?.state === 'reading') timer = setTimeout(ask, pollMs);
        })
        .catch(() => {
          if (!stopped) setResult({ status: 'unavailable' });
        });
    };
    ask();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      setResult(null);
    };
  }, [active, load, pollMs]);
  if (!active || !load) return { status: 'idle' };
  return result ?? { status: 'loading' };
}

/** "Reading the drawings: 40 of 188 pages" while the set is read (#1871); null once it is read. */
export function readingWords(reading: ChatFullCheckPlan['reading']): string | null {
  if (!reading) return null;
  if (reading.state === 'failed') return `The drawings could not be read${reading.why ? ` (${reading.why})` : ''}.`;
  if (reading.state !== 'reading') return null;
  const count = reading.total ? `Reading the drawings: ${reading.done} of ${reading.total} pages.` : 'Reading the drawings…';
  return `${count} The check starts when every page is read; nothing is spent while it waits.`;
}

/** The answers as the message body sends them, in the questions' order. */
export function planAnswers(doubts: readonly ChatPlanDoubt[], picked: Readonly<Record<string, string>>): ChatPlanAnswer[] {
  return doubts.filter((doubt) => picked[doubt.id]).map((doubt) => ({ id: doubt.id, answer: picked[doubt.id] }));
}
