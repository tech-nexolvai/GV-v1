import { Loader2 } from 'lucide-react';

import { cn } from '@/lib/utils';
import { decidedWord, splitNumbers } from '@/lib/review-items';
import type { ReviewStatus } from '@/api/review-items';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

/**
 * Where an item stands: always a glyph and a word, the colour only a second signal. Open is amber
 * ("Needs you"), done is green, re-checking is grey and turning (still when motion is reduced).
 */
export function StatusWord({ status, decidedBy = null, className }: { status: ReviewStatus; decidedBy?: string | null; className?: string }) {
  if (status === 'rechecking') {
    return (
      <span data-slot="status-word" data-status={status} className={cn('inline-flex items-center gap-1.5 text-[13px] text-muted-foreground', className)}>
        <Loader2 aria-hidden="true" className="size-3.5 animate-spin motion-reduce:animate-none" />
        Re-checking
      </span>
    );
  }
  if (status === 'done') {
    return (
      <span data-slot="status-word" data-status={status} className={cn('inline-flex items-center gap-1.5 text-[13px] text-outcome-pass-fg', className)}>
        <OutcomeIcon outcome="PASS" size={14} />
        {decidedWord(decidedBy)}
      </span>
    );
  }
  return (
    <span data-slot="status-word" data-status={status} className={cn('inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-review-fg', className)}>
      <OutcomeIcon outcome="REVIEW_REQUIRED" size={14} />
      Needs you
    </span>
  );
}

/** Plain words with every number in it set in the number face (tabular Plex Mono). */
export function NumText({ text, className }: { text: string; className?: string }) {
  return (
    <span className={className}>
      {splitNumbers(text).map((part, index) =>
        part.number ? (
          <span key={index} className="num">
            {part.text}
          </span>
        ) : (
          part.text
        ),
      )}
    </span>
  );
}

/** A key the reviewer can press, shown inside its button from desktop width. */
export function Kbd({ children }: { children: string }) {
  return (
    <kbd
      aria-hidden="true"
      className="num ml-1 hidden h-5 min-w-5 items-center justify-center rounded border border-current/25 px-1 text-[12px] leading-none opacity-70 lg:inline-flex"
    >
      {children}
    </kbd>
  );
}
