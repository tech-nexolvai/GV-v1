import { useId, useState } from 'react';
import { ChevronDown, CircleDashed, History, PanelRight, RefreshCw } from 'lucide-react';

import type { ArchitectCompared, ArchitectResult } from '@/api/client';
import { cn } from '@/lib/utils';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { architectState, neutralNumbers, confirmWords, headlinePair, matchOf, matchWords, reasonSaysItself, pairLabel, pairedByWords, viewLinkWords, viewOf } from '@/lib/architect';

/**
 * The vendor-vs-architect check on a countertop (#1085): "Matches the architect". Every number is
 * the API's own text. "Not compared" is grey words, never a chip: it is not a finding and needs no
 * click (decided 2026-10-09). A pairing resting on one judgment asks the reviewer to confirm it.
 */

/** The result in words: a chip for a recorded result, amber words when the reviewer is needed. */
export function ArchitectStatus({ result, clamp = false }: { result: ArchitectResult; clamp?: boolean }) {
  const state = architectState(result);
  if (state === 'none') return null;
  if (state === 'not-compared') {
    return (
      <span
        data-architect="not-compared"
        // In a list, the same reason often repeats on every row: two lines, the rest in the title.
        className={cn('text-xs text-muted-foreground', clamp && 'line-clamp-2')}
        title={clamp ? result.not_compared_reason ?? undefined : undefined}
      >
        {reasonSaysItself(result) ? result.not_compared_reason : <>Not compared: {result.not_compared_reason}</>}
      </span>
    );
  }
  if (state === 'choose-view' || state === 'by-hand') {
    return (
      <span data-architect={state} className="inline-flex items-center gap-1 text-xs font-medium text-outcome-review-fg">
        <OutcomeIcon outcome="REVIEW_REQUIRED" size={13} />
        {state === 'choose-view'
          ? "Choose which of the architect's views shows this countertop"
          : result.match?.status === 'not_matched_yet'
            ? 'Not matched with an architect view yet: run the checks again, or compare by hand'
            : 'Compare this countertop by hand'}
      </span>
    );
  }
  if (state === 'view-picked') {
    // The reviewer's part is done; not a result yet, so no outcome colour (as a saved pairing).
    return (
      <span data-architect={state} className="inline-flex items-center gap-1 text-xs font-medium">
        <RefreshCw className="size-3.5" aria-hidden="true" /> View chosen: it counts once the checks run again
      </span>
    );
  }
  if (state === 'confirm' || state === 'unpaired') {
    return (
      <span data-architect={state} className="inline-flex items-center gap-1 text-xs font-medium text-outcome-review-fg">
        <OutcomeIcon outcome="REVIEW_REQUIRED" size={13} />
        {state === 'confirm' ? confirmWords(result) : "Pair the architect's dimension with the vendor's"}
      </span>
    );
  }
  const outcome = result.outcome!;
  const decidedAbstention = !result.needs_decision && (outcome === 'REVIEW_REQUIRED' || outcome === 'NOT_FOUND');
  return (
    <span data-architect="compared" className="inline-flex flex-wrap items-center gap-1.5">
      {decidedAbstention ? (
        <span title={`Recorded result: ${OUTCOME_LABELS[outcome]}`} className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5 font-sans text-xs text-muted-foreground">
          <CircleDashed className="size-3.5" aria-hidden="true" /> Not checkable
        </span>
      ) : (
        <OutcomeBadge outcome={outcome} />
      )}
      {result.needs_decision && outcome !== 'REVIEW_REQUIRED' && (
        <span className="inline-flex items-center gap-1 font-sans text-xs font-medium text-outcome-review-fg">
          <OutcomeIcon outcome="REVIEW_REQUIRED" size={13} /> Needs you
        </span>
      )}
    </span>
  );
}

/**
 * One compared pair's difference, in the API's words, with the pair's own result as a glyph.
 * `neutral` while the pairing waits for the reviewer: its numbers are not a result yet.
 */
export function ArchitectDelta({ pair, neutral = false }: { pair: ArchitectCompared; neutral?: boolean }) {
  if (!pair.delta_display) return <span className="num text-muted-foreground">—</span>;
  const outcome = neutral ? null : pair.outcome;
  const tone = outcome === 'PASS' ? 'text-outcome-pass-fg' : outcome === 'FAIL' ? 'text-outcome-fail-fg' : 'text-muted-foreground';
  return (
    <span className={cn('num inline-flex items-center gap-1 font-medium', tone)}>
      {outcome && <OutcomeIcon outcome={outcome} size={13} />}
      {pair.delta_display}
    </span>
  );
}

/** "Architect's drawing says 84"" for the headline pair; "Piece 2:" first when it is not the overall. */
export function ArchitectSays({ result }: { result: ArchitectResult }) {
  const pair = headlinePair(result);
  if (!pair?.architect_display) return null;
  return (
    <span className="text-xs">
      {pair.kind !== 'overall' && <>{pairLabel(pair)}: </>}
      Architect&apos;s drawing says <span className="num font-medium">{pair.architect_display}</span>
      {result.compared.length > 1 && <span className="text-muted-foreground"> · {result.compared.length} compared</span>}
    </span>
  );
}

/**
 * The whole line, for the countertop card and the phone list: "Architect", what it says, the
 * difference, the result, and who paired it; then, when the architect's drawings are a separate file
 * (#1168), the view it was compared with as a link. Nothing for a row with no architect result.
 */
export function ArchitectLine({
  result,
  className,
  clamp = false,
  onOpenView,
}: {
  result: ArchitectResult | null | undefined;
  className?: string;
  clamp?: boolean;
  /** Opens the architect's page beside the vendor's, framing the view (#1168). */
  onOpenView?: () => void;
}) {
  if (!result || architectState(result) === 'none') return null;
  const pair = headlinePair(result);
  const pairedBy = pairedByWords(result);
  const state = architectState(result);
  return (
    <div data-slot="architect-line" className={cn('flex flex-wrap items-center gap-x-2 gap-y-1 font-sans text-sm', className)}>
      <span className="text-xs font-medium text-muted-foreground">Architect</span>
      {state !== 'not-compared' && pair && (
        <>
          <ArchitectSays result={result} />
          <ArchitectDelta pair={pair} neutral={neutralNumbers(result)} />
        </>
      )}
      <ArchitectStatus result={result} clamp={clamp} />
      {state !== 'not-compared' && pairedBy && <span className="text-xs text-muted-foreground">{pairedBy}</span>}
      <ArchitectViewLink result={result} onOpen={onOpenView} />
      <MatchTag result={result} />
    </div>
  );
}

/**
 * "compared with <file>, page N, view X" (#1168): the server's own words, as a link that opens the
 * architect's page beside the vendor's. Nothing on a combined-sheet set (no separate file, no view).
 */
export function ArchitectViewLink({ result, onOpen, className }: { result: ArchitectResult | null | undefined; onOpen?: () => void; className?: string }) {
  const words = viewLinkWords(result);
  if (!words || !viewOf(result)) return null;
  if (!onOpen) return <span data-slot="architect-view-link" className={cn('text-xs text-muted-foreground', className)}>{words}</span>;
  return (
    <button
      type="button"
      data-slot="architect-view-link"
      onClick={onOpen}
      className={cn('inline-flex min-w-0 items-center gap-1 rounded-sm text-left text-xs underline underline-offset-2 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring', className)}
    >
      <PanelRight className="size-3.5 shrink-0" aria-hidden="true" />
      <span className="min-w-0">{words}</span>
    </button>
  );
}

/**
 * Who matched the view, in small grey words (#1168), only where the line does not already say it:
 * "View matched by code and both AIs", "View chosen by a reviewer", "Same view as on the earlier
 * revision". The waiting and asking states say theirs in the status.
 */
export function MatchTag({ result }: { result: ArchitectResult | null | undefined }) {
  const match = matchOf(result);
  if (!match || !['auto_matched', 'reviewer_confirmed', 'carried_over'].includes(match.status)) return null;
  if (architectState(result ?? null) === 'view-picked') return null;
  return (
    <span data-slot="architect-match-tag" data-match={match.status} className="inline-flex items-center gap-1 text-xs text-muted-foreground">
      {match.status === 'carried_over' && <History className="size-3.5" aria-hidden="true" />}
      {matchWords(result)}
    </span>
  );
}

/** Every compared pair, for a row's details: vendor, architect, the difference and its result. */
export function ArchitectPairs({ result }: { result: ArchitectResult }) {
  if (result.compared.length === 0) return null;
  return (
    <table className="w-auto text-xs" aria-label="Compared with the architect">
      <thead>
        <tr className="text-left text-muted-foreground">
          <th scope="col" className="py-0.5 pr-3 font-normal">Compared</th>
          <th scope="col" className="py-0.5 pr-3 text-right font-normal">Vendor</th>
          <th scope="col" className="py-0.5 pr-3 text-right font-normal">Architect</th>
          <th scope="col" className="py-0.5 text-right font-normal">Difference</th>
        </tr>
      </thead>
      <tbody>
        {result.compared.map((pair, index) => (
          <tr key={`${pair.kind}-${pair.vendor_piece ?? 'all'}-${index}`}>
            <th scope="row" className="py-0.5 pr-3 text-left font-normal">{pairLabel(pair)}</th>
            <td className="num py-0.5 pr-3 text-right">{pair.vendor_display ?? '—'}</td>
            <td className="num py-0.5 pr-3 text-right">{pair.architect_display ?? '—'}</td>
            <td className="py-0.5 text-right"><ArchitectDelta pair={pair} neutral={neutralNumbers(result)} /></td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** The shared reason's words (#1126): the headline, and the reason without the headline's prefix. */
function sharedNotice(reason: string): { headline: string; body: string } {
  const notYet = reason.startsWith('Not checked yet');
  // The headline already says "not compared" / "not checked yet": the reason is not prefixed twice.
  const body = reason.replace(/^(Not compared|Not checked yet):\s*/, '');
  return {
    headline: notYet ? 'not checked yet on any countertop' : 'not compared on any countertop',
    body: body.charAt(0).toUpperCase() + body.slice(1),
  };
}

/** "Why?", which opens the reason in place. */
function WhyButton({ open, controls, onToggle }: { open: boolean; controls: string; onToggle: () => void }) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      onClick={onToggle}
      className="inline-flex items-center gap-0.5 rounded-md align-baseline text-xs whitespace-nowrap underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
    >
      Why?
      <ChevronDown className={cn('size-3.5 self-center transition-transform motion-reduce:transition-none', open && 'rotate-180')} aria-hidden="true" />
    </button>
  );
}

/**
 * One quiet notice above the table (#1126) when every countertop is "not compared" with the
 * architect for the same reason: said once, not under every row. The reason is the API's own text,
 * behind "Why?"; each row's details still carry it too.
 */
export function ArchitectNotice({ reason, className }: { reason: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const { headline, body } = sharedNotice(reason);
  return (
    <section
      data-slot="architect-notice"
      aria-label="Matches the architect"
      className={cn('flex flex-col gap-1.5 rounded-xl border border-dashed px-4 py-2.5 font-sans text-sm text-muted-foreground', className)}
    >
      <div className="flex items-start gap-2">
        <CircleDashed className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <p className="min-w-0">
          <span className="font-medium text-foreground">Matches the architect:</span> {headline}{' '}
          <WhyButton open={open} controls={id} onToggle={() => setOpen((value) => !value)} />
        </p>
      </div>
      <p id={id} hidden={!open} data-slot="architect-notice-reason" className="max-w-3xl pl-6 text-xs">
        {body}
      </p>
    </section>
  );
}

/**
 * The same, as one short line on a single countertop (the queue, #1155): when every countertop
 * shares the reason, the item says "not compared on any countertop" instead of the whole sentence
 * each time. The reason is one click away, behind "Why?".
 */
export function ArchitectSharedLine({ reason, className }: { reason: string; className?: string }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const { headline, body } = sharedNotice(reason);
  return (
    <div data-slot="architect-line" data-shared="true" className={cn('flex flex-col gap-1 font-sans text-sm', className)}>
      <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xs font-medium text-muted-foreground">Architect</span>
        <span data-architect="not-compared" className="text-xs text-muted-foreground">
          {headline.charAt(0).toUpperCase() + headline.slice(1)}
        </span>
        <span className="text-muted-foreground">
          <WhyButton open={open} controls={id} onToggle={() => setOpen((value) => !value)} />
        </span>
      </p>
      <p id={id} hidden={!open} data-slot="architect-shared-reason" className="text-xs text-muted-foreground">
        {body}
      </p>
    </div>
  );
}
