import { createContext, useContext, useEffect, useState } from 'react';
import { RotateCcw, SlidersHorizontal } from 'lucide-react';

import type { ActionExtra, ReviewItem } from '@/api/review-items';
import { vendorSideWords } from '@/lib/architect';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { ChoiceCard } from './choice-card';
import {
  NONE_COMPARABLE_NOTE,
  PAIRED_BY,
  architectProposal,
  pairingBody,
  viewProposal,
  type ArchitectLoader,
  type Loaded,
  type Question,
} from './architect-choices-lib';

const ArchitectContext = createContext<{ projectId: string | null; packageId: string; loader: ArchitectLoader } | null>(null);

export function ArchitectProvider({ projectId, packageId, loader, children }: { projectId: string | null; packageId: string; loader: ArchitectLoader; children: React.ReactNode }) {
  return <ArchitectContext.Provider value={{ projectId, packageId, loader }}>{children}</ArchitectContext.Provider>;
}

/**
 * "Compare with the architect's drawing" as one-click cards (#1368). Either which of the architect's
 * views shows this countertop (each view named, "None of these", the AIs' pick pre-selected and
 * labelled as a proposal), or the pairing of the architect's dimensions (the proposed pairing,
 * "None of these", and pairing it differently in the advanced view). Every answer goes through W3.
 */
export function ArchitectChoices({
  item,
  busy,
  onAnswer,
  onProposal,
  onOpenAdvanced,
}: {
  item: ReviewItem;
  busy: boolean;
  onAnswer: (extra: ActionExtra) => void;
  /** Reports the proposal once loaded, for the C key; null when there is none. */
  onProposal?: (extra: ActionExtra | null) => void;
  onOpenAdvanced?: () => void;
}) {
  const context = useContext(ArchitectContext);
  const [attempt, setAttempt] = useState(0);
  const [loaded, setLoaded] = useState<{ attempt: number; state: Loaded } | null>(null);
  const rowId = item.row_id ?? null;
  const projectId = context?.projectId ?? null;
  const loader = context?.loader ?? null;
  const packageId = context?.packageId ?? '';

  useEffect(() => {
    if (loader === null || projectId === null || rowId === null) return;
    let current = true;
    Promise.all([loader.viewMatch(projectId, packageId, rowId), loader.pairing(projectId, packageId, rowId)]).then(
      ([match, pairing]) => {
        if (!current) return;
        const question: Question =
          match && match.current && match.current.status === 'needs_reviewer'
            ? { kind: 'view', match, recordId: match.current.record_id }
            : { kind: 'pairing', pairing };
        setLoaded({ attempt, state: { status: 'ready', question } });
      },
      (error: unknown) => current && setLoaded({ attempt, state: { status: 'error', message: error instanceof Error ? error.message : String(error) } }),
    );
    return () => {
      current = false;
    };
  }, [loader, packageId, projectId, rowId, attempt]);

  const state: Loaded =
    rowId === null || projectId === null
      ? { status: 'error', message: 'This item names no countertop row, so its choices cannot be loaded.' }
      : loaded && loaded.attempt === attempt
        ? loaded.state
        : { status: 'loading' };

  const question = state.status === 'ready' ? state.question : null;
  useEffect(() => {
    onProposal?.(question ? architectProposal(question) : null);
    // Reported once per loaded question.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [question]);

  const open = item.status === 'open';
  const heading = (
    <legend className="mb-1 text-[15px] leading-6 font-medium text-foreground">
      {question?.kind === 'view' ? "Which of the architect's views shows this countertop?" : "Which of the architect's dimensions match this countertop?"}
    </legend>
  );

  if (state.status === 'loading') {
    return (
      <div role="status" aria-label="Loading the architect's choices" className="flex flex-col gap-2">
        <Skeleton className="h-20 w-full rounded-xl" />
        <Skeleton className="h-20 w-full rounded-xl" />
      </div>
    );
  }
  if (state.status === 'error') {
    return (
      <div role="alert" className="flex flex-col items-start gap-2 rounded-xl border border-outcome-fail-fg/40 bg-outcome-fail-bg px-4 py-3 text-[13px] leading-5 text-outcome-fail-fg">
        <span>The architect's choices could not be loaded: {state.message}</span>
        {rowId !== null && projectId !== null && (
          <Button size="xs" variant="outline" onClick={() => setAttempt((n) => n + 1)}>
            <RotateCcw aria-hidden="true" />
            Retry
          </Button>
        )}
      </div>
    );
  }

  const advanced = onOpenAdvanced && (
    <Button variant="link" size="sm" className="h-auto self-start px-0 text-[13px] text-muted-foreground" onClick={onOpenAdvanced}>
      <SlidersHorizontal aria-hidden="true" />
      {state.question.kind === 'view' ? 'See each view beside the vendor’s in the advanced view' : 'Pair it differently in the advanced view'}
    </Button>
  );

  if (state.question.kind === 'view') {
    const { match, recordId } = state.question;
    const proposal = viewProposal(match);
    const candidates = [...match.candidates].sort((a, b) => a.rank - b.rank);
    return (
      <fieldset data-slot="architect-choices" className="flex min-w-0 flex-col gap-3">
        {heading}
        {(match.current?.reasons.length ?? 0) > 0 && <p className="text-[13px] leading-5 text-muted-foreground">{match.current!.reasons.join(' ')}</p>}
        <ul className="grid gap-2">
          {candidates.map((candidate) => {
            const proposed = proposal?.viewId === candidate.view.view_id;
            return (
              <li key={candidate.view.view_id}>
                <ChoiceCard
                  title={candidate.view.label}
                  detail={
                    candidate.can_pick
                      ? [candidate.score_summary, candidate.remembered ? 'Chosen for this item on an earlier revision.' : null].filter(Boolean).join(' ')
                      : candidate.refusal ?? 'This view cannot be chosen.'
                  }
                  proposal={proposed ? proposal!.by : null}
                  leadsTo="This countertop is compared with this view, and the checks run again."
                  disabled={!open || !candidate.can_pick}
                  busy={busy && proposed}
                  showKey={proposed && open}
                  onChoose={() => onAnswer({ architect_view: { view_id: candidate.view.view_id, expected_record_id: recordId } })}
                />
              </li>
            );
          })}
          {match.can_choose_none && (
            <li>
              <ChoiceCard
                title="None of these"
                leadsTo="Nothing on the architect's drawings is compared with this countertop, and the cloud PDF says so."
                disabled={!open}
                onChoose={() => onAnswer({ architect_view: { none_of_these: true, expected_record_id: recordId } })}
              />
            </li>
          )}
        </ul>
        {candidates.length === 0 && <p className="text-[13px] text-muted-foreground">The architect's file has no views to choose from.</p>}
        {advanced}
      </fieldset>
    );
  }

  const { pairing } = state.question;
  const body = pairingBody(pairing);
  const effective = pairing.effective;
  const printed = new Map(pairing.spans.map((span) => [span.candidate_id, span.printed]));
  const by = effective ? PAIRED_BY[effective.source] ?? null : null;
  return (
    <fieldset data-slot="architect-choices" className="flex min-w-0 flex-col gap-3">
      {heading}
      <ul className="grid gap-2">
        {body && effective && (
          <li>
            <ChoiceCard
              title="Use this pairing"
              detail={
                <span className="flex flex-col gap-0.5">
                  {effective.pairs.map((pair) => (
                    <span key={pair.architect_candidate_id}>
                      {vendorSideWords(pair.kind, pair.vendor_slot_indices)} ↔ the architect's <span className="num text-foreground">{printed.get(pair.architect_candidate_id) ?? 'dimension'}</span>
                    </span>
                  ))}
                </span>
              }
              proposal={by}
              leadsTo="These dimensions are compared with the architect's, and the checks run again."
              disabled={!open}
              busy={busy && by !== null}
              showKey={by !== null && open}
              onChoose={() => onAnswer(body)}
            />
          </li>
        )}
        <li>
          <ChoiceCard
            title="None of these"
            detail="Nothing on the architect's drawing is comparable with this countertop."
            leadsTo="Nothing is compared with the architect for this countertop, and the cloud PDF says so."
            disabled={!open}
            onChoose={() =>
              onAnswer({
                architect_pairing: { pairs: [], note: NONE_COMPARABLE_NOTE, ...(pairing.current ? { expected_record_id: pairing.current.record_id } : {}) },
              })
            }
          />
        </li>
      </ul>
      {advanced}
    </fieldset>
  );
}
