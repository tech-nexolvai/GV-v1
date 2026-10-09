import type { ReactNode } from 'react';

import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';

/**
 * The explanation behind a line, one click away: the redesigned screens keep one line of text and
 * move their paragraphs here (Measurements #1061, Documents #1064). A popover rather than a hover
 * tooltip, so it works on a phone.
 */
export function InfoTip({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Popover>
      <PopoverTrigger asChild>
        <button
          type="button"
          data-tw
          aria-label={label}
          // A 20 px circle that still gives a finger a 44 px target (the invisible ::after), instead of
          // the page's touch rule stretching the button itself into a tall pill (#1072).
          className="relative inline-flex size-5 min-h-5 shrink-0 items-center justify-center rounded-full border align-middle font-sans text-xs text-muted-foreground after:absolute after:-inset-3 after:content-[''] hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-none"
        >
          ?
        </button>
      </PopoverTrigger>
      <PopoverContent className="flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2 font-sans text-sm" align="start">
        {children}
      </PopoverContent>
    </Popover>
  );
}
