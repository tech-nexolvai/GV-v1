import { CircleAlert, CircleCheck, CircleX, Clock3, LoaderCircle, PencilLine, RotateCcw } from 'lucide-react';

import { OutcomeGlyph } from '@/components/chat/chat-result';
import { countParts, countSentence, setResult, type FullCheckCounts, type ResultKind, type SetCheck } from '@/lib/set-check';
import { cn } from '@/lib/utils';

/**
 * The review result's look: the chat package card's glyphs (#1622). Outcome colour only with its
 * glyph and word (decision 2026-10-08), and never amber: "Approved as noted" is plain text with its
 * pencil; what is not a result (running, unfinished, none) is grey.
 */
const LOOK: Record<ResultKind, { Icon: typeof RotateCcw; tone: string }> = {
  revise_resubmit: { Icon: RotateCcw, tone: 'text-outcome-fail-fg' },
  rejected: { Icon: CircleX, tone: 'text-outcome-fail-fg' },
  approved_as_noted: { Icon: PencilLine, tone: 'text-foreground' },
  approved: { Icon: CircleCheck, tone: 'text-outcome-pass-fg' },
  not_for_sending: { Icon: CircleAlert, tone: 'text-muted-foreground' },
  unfinished: { Icon: CircleAlert, tone: 'text-muted-foreground' },
  checking: { Icon: LoaderCircle, tone: 'text-muted-foreground' },
  none: { Icon: Clock3, tone: 'text-muted-foreground' },
};

/** The newest full check's review result: glyph and words. */
export function SetResultLine({ set, className }: { set: SetCheck; className?: string }) {
  const result = setResult(set);
  const look = LOOK[result.kind];
  return (
    <span
      data-slot="set-result"
      data-result={result.kind}
      title={result.title ?? undefined}
      className={cn('inline-flex items-center gap-1.5 text-sm', result.kind === 'none' || result.kind === 'checking' ? 'font-normal' : 'font-medium', look.tone, className)}
    >
      <look.Icon className={cn('size-4 shrink-0', result.kind === 'checking' && 'animate-spin motion-reduce:animate-none')} aria-hidden="true" />
      <span>{result.words}</span>
      {result.title && <span className="sr-only">. {result.title}</span>}
    </span>
  );
}

/** The counts as the result screen's tiles show them: glyph and number, the words for a reader. */
export function SetCounts({ counts, className }: { counts: FullCheckCounts; className?: string }) {
  return (
    <span data-slot="set-counts" className={cn('inline-flex flex-wrap items-center gap-x-3 gap-y-1 text-sm', className)}>
      <span className="sr-only">{countSentence(counts)}</span>
      {countParts(counts).map((part) => (
        <span key={part.kind} data-count={part.kind} className="inline-flex items-center gap-1" title={`${part.count} ${part.words}`} aria-hidden="true">
          {/* A zero is quiet: the glyph faded, the number grey, so the counts that matter stand out. */}
          <OutcomeGlyph kind={part.kind} className={cn('size-4', part.count === 0 && 'opacity-35')} />
          <span className={cn('num', part.count === 0 && 'text-muted-foreground')}>{part.count}</span>
        </span>
      ))}
    </span>
  );
}

/** The glyphs' words, always above the table: colour never carries the meaning alone. */
export function SetCountsLegend({ askArchitect }: { askArchitect: boolean }) {
  const parts = countParts({ wrong: 0, right: 0, ask_architect: askArchitect ? 1 : 0, not_compared: 0 });
  return (
    <ul data-slot="set-counts-legend" aria-label="Item counts" className="flex flex-wrap gap-x-4 gap-y-1.5 text-xs text-muted-foreground">
      {parts.map((part) => (
        <li key={part.kind} className="inline-flex items-center gap-1.5">
          <OutcomeGlyph kind={part.kind} className="size-3.5" />
          <span>{part.words}</span>
        </li>
      ))}
    </ul>
  );
}
