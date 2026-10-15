import { useId } from 'react';
import { ArrowRight, Loader2, Sparkles } from 'lucide-react';

import { cn } from '@/lib/utils';
import { Kbd } from './words';

/**
 * One answer as one card (#1368): what it says, what it leads to, and, for the AI's proposal, a
 * quiet "AI proposal" tag. The proposal is shown pre-selected (a stronger border) but is never sent
 * until clicked: the C key is the one-key way to click it. It is not a pressed toggle, so a screen
 * reader hears its name and then, as its description, who proposed it and that nothing is saved yet.
 */
export function ChoiceCard({
  title,
  leadsTo,
  detail,
  result,
  proposal = null,
  proposalTag = 'AI proposal',
  disabled = false,
  busy = false,
  showKey = false,
  onChoose,
  className,
  ...rest
}: {
  title: React.ReactNode;
  /** What happens once it is chosen, in plain words. */
  leadsTo: React.ReactNode;
  /** A second line under the title (a score, what the vendor wrote …). */
  detail?: React.ReactNode;
  /** The recorded result this choice would give: always a glyph and a word. */
  result?: React.ReactNode;
  /** Words for who proposed it ("Both AIs"), or null when it is not the proposal. */
  proposal?: string | null;
  /** The tag's words: "AI proposal", or "Proposal" when code alone proposed it. */
  proposalTag?: string;
  disabled?: boolean;
  /** This choice is the one being saved: it shows the spinner (and the card is disabled). */
  busy?: boolean;
  /** Shows the C key hint (the proposal of an open item). */
  showKey?: boolean;
  onChoose: () => void;
  className?: string;
} & Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'title' | 'onClick' | 'disabled' | 'className' | 'children'>) {
  const proposed = proposal !== null;
  const id = useId();
  const labelId = `${id}-label`;
  const noteId = `${id}-note`;
  const noted = proposed && Boolean(proposal);
  return (
    <button
      type="button"
      data-slot="choice-card"
      data-proposed={proposed || undefined}
      // The note is read as the description, so the name stays the card's own words.
      aria-labelledby={noted ? labelId : undefined}
      aria-describedby={noted ? noteId : undefined}
      aria-busy={busy || undefined}
      disabled={disabled || busy}
      onClick={onChoose}
      className={cn(
        'group/choice relative flex w-full min-w-0 flex-col gap-2 rounded-xl border bg-background px-4 py-3 text-left sm:px-5',
        'transition-[border-color,background-color,box-shadow] duration-150 ease-out motion-reduce:transition-none',
        'outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:border-ring',
        'disabled:cursor-not-allowed disabled:opacity-60',
        proposed
          ? 'border-foreground/55 shadow-[0_1px_0_0_var(--border)] hover:border-foreground/80'
          : 'border-border hover:border-foreground/30 hover:bg-muted/40',
        className,
      )}
      {...rest}
    >
      <span id={labelId} className="contents">
        <span className="flex min-w-0 flex-wrap items-start justify-between gap-x-4 gap-y-1">
          <span className="flex min-w-0 flex-1 flex-col gap-1">
            <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
              <span className="min-w-0 text-[15px] leading-6 font-medium text-foreground first-letter:uppercase">{title}</span>
              {proposed && (
                <span className="inline-flex h-5 items-center gap-1 rounded-full border border-border bg-muted px-2 text-[12px] leading-none font-medium text-muted-foreground">
                  <Sparkles aria-hidden="true" className="size-3" />
                  {proposalTag}
                  <span className="sr-only">: {proposal}</span>
                </span>
              )}
            </span>
            {detail && <span className="text-[13px] leading-5 text-muted-foreground">{detail}</span>}
          </span>
          {(result || busy || showKey) && (
            <span className="flex shrink-0 items-center gap-2 pt-0.5">
              {result}
              {busy ? <Loader2 aria-hidden="true" className="size-4 animate-spin text-muted-foreground motion-reduce:animate-none" /> : showKey && <Kbd>C</Kbd>}
            </span>
          )}
        </span>
        <span className="flex items-start gap-1.5 text-[13px] leading-5 text-muted-foreground">
          <ArrowRight aria-hidden="true" className="mt-[3px] size-3.5 shrink-0 transition-transform duration-150 group-hover/choice:translate-x-0.5 motion-reduce:transition-none" />
          <span className="min-w-0">{leadsTo}</span>
        </span>
      </span>
      {noted && (
        <span id={noteId} className="text-[12px] leading-4 text-muted-foreground">
          Proposed by {proposal}. Nothing is saved until you click.
        </span>
      )}
    </button>
  );
}
