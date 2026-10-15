import { Check, Loader2, UserCheck } from 'lucide-react';

import { cn } from '@/lib/utils';
import { decidedWord, pageWord, splitNumbers } from '@/lib/review-items';
import type { ReviewStatus } from '@/api/review-items';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

/**
 * Where an item stands: always a glyph and a word, the colour only a second signal. Open is amber
 * ("Needs you"), re-checking is grey and turning (still when motion is reduced), and done is a plain
 * grey check. Done is never the green PASS glyph: a person's answer is not a check's outcome, and the
 * PASS and FAIL glyphs only ever show an outcome the API recorded.
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
      <span data-slot="status-word" data-status={status} className={cn('inline-flex items-center gap-1.5 text-[13px] text-muted-foreground', className)}>
        {decidedBy === 'REVIEWER' ? <UserCheck aria-hidden="true" className="size-3.5" /> : <Check aria-hidden="true" className="size-3.5" />}
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

/** The page an item is on, in the number face; plain words when the page was not recorded. */
export function PageTag({ page, className }: { page: number | null | undefined; className?: string }) {
  if (page == null) return <span className={cn('shrink-0 text-[13px] text-muted-foreground', className)}>page not recorded</span>;
  return <span className={cn('num shrink-0 text-[13px] text-muted-foreground', className)}>{pageWord(page)}</span>;
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
      className="ml-1 hidden h-5 min-w-5 font-sans items-center justify-center rounded border border-current/25 px-1 text-[12px] leading-none opacity-70 lg:inline-flex"
    >
      {children}
    </kbd>
  );
}
