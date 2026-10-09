import { useId, useState } from 'react';
import { ChevronDown, CircleDashed } from 'lucide-react';

import type { ArchitectCompared, ArchitectResult } from '@/api/client';
import { cn } from '@/lib/utils';
import { OutcomeBadge } from '@/components/ui/outcome-badge';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { OUTCOME_LABELS } from '@/data/outcomeLabels';
import { architectState, awaitsPairing, confirmWords, headlinePair, reasonSaysItself, pairLabel, pairedByWords } from '@/lib/architect';

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
 * difference, the result, and who paired it. Nothing for a row with no architect result.
 */
export function ArchitectLine({ result, className, clamp = false }: { result: ArchitectResult | null | undefined; className?: string; clamp?: boolean }) {
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
          <ArchitectDelta pair={pair} neutral={awaitsPairing(result)} />
        </>
      )}
      <ArchitectStatus result={result} clamp={clamp} />
      {state !== 'not-compared' && pairedBy && <span className="text-xs text-muted-foreground">{pairedBy}</span>}
    </div>
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
            <td className="py-0.5 text-right"><ArchitectDelta pair={pair} neutral={awaitsPairing(result)} /></td>
          </tr>
        ))}
      </tbody>
    </table>
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
  const notYet = reason.startsWith('Not checked yet');
  // The headline already says "not compared" / "not checked yet": the reason is not prefixed twice.
  const body = reason.replace(/^(Not compared|Not checked yet):\s*/, '');
  const shown = body.charAt(0).toUpperCase() + body.slice(1);
  return (
    <section
      data-slot="architect-notice"
      aria-label="Matches the architect"
      className={cn('flex flex-col gap-1.5 rounded-xl border border-dashed px-4 py-2.5 font-sans text-sm text-muted-foreground', className)}
    >
      <div className="flex items-start gap-2">
        <CircleDashed className="mt-0.5 size-4 shrink-0" aria-hidden="true" />
        <p className="min-w-0">
          <span className="font-medium text-foreground">Matches the architect:</span>{' '}
          {notYet ? 'not checked yet on any countertop' : 'not compared on any countertop'}{' '}
          <button
            type="button"
            aria-expanded={open}
            aria-controls={id}
            onClick={() => setOpen((value) => !value)}
            className="inline-flex items-center gap-0.5 rounded-md align-baseline text-xs whitespace-nowrap underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
          >
            Why?
            <ChevronDown className={cn('size-3.5 self-center transition-transform motion-reduce:transition-none', open && 'rotate-180')} aria-hidden="true" />
          </button>
        </p>
      </div>
      <p id={id} hidden={!open} data-slot="architect-notice-reason" className="max-w-3xl pl-6 text-xs">
        {shown}
      </p>
    </section>
  );
}
