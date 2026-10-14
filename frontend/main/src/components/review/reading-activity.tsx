import { Check, RotateCw, ScanText } from 'lucide-react';

import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import {
  activityLine,
  needsYou,
  readingHeadline,
  roleName,
  summaryParts,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
} from '@/lib/reading-activity';
import { cn } from '@/lib/utils';

/** How many of the newest happenings the panel lists while reading; the rest are in the counts. */
export const SHOWN_ITEMS = 4;

/**
 * The live reading activity (#1239), under the review stepper and on the Measurements tab.
 *
 * While reading: the page being read, then the newest happenings — an inspector stopping an AI, the
 * AI trying again and what changed, and whether that helped — each with the role that did it ("Line
 * finder"; the agent's name on hover). After reading: one line, "Tried again on 5 items: 3 now
 * agree, 2 wait for you.", whose counts open Decisions.
 *
 * One `aria-live="polite"` region that stays mounted, so a screen reader hears each new line without
 * being interrupted. With no activity (absent, null, or nothing to say) it renders nothing at all.
 * It shows recorded facts only; nothing here is a result.
 */
export function ReadingActivity({
  activity,
  reading,
  progress,
  onOpenDecisions,
  className,
}: {
  /** `PackageOut.activity`, already read through `readActivity`. */
  activity: ReadingActivityOut | null | undefined;
  /** The package is being read now (the Reading step is current). */
  reading: boolean;
  /** The worker's progress line, for "Reading page k of N…". */
  progress?: { pagesDone: number | null; pagesTotal: number | null } | null;
  /** Opens Decisions; the summary's counts link there. */
  onOpenDecisions?: () => void;
  className?: string;
}) {
  if (!activity) return null;
  const summary = reading ? null : summaryParts(activity.summary);
  if (!reading && !summary) return null;

  const newest = [...activity.items].sort(byNewest).slice(0, SHOWN_ITEMS);
  return (
    <section
      data-tw
      data-slot="reading-activity"
      data-reading={reading ? 'true' : 'false'}
      aria-label="Reading activity"
      className={cn('flex flex-col gap-1.5 rounded-xl border border-border bg-card px-3 py-2 font-sans text-sm', className)}
    >
      <div role="status" aria-live="polite" className="flex flex-col gap-1.5">
        {reading ? (
          <>
            <p className="flex items-center gap-1.5 font-medium">
              <ScanText className="size-4 shrink-0" aria-hidden="true" />
              {readingHeadline(progress, activity.now)}
            </p>
            {newest.length > 0 && (
              // Its own provider, so the panel works wherever it is placed (the Measurements tab too).
              <TooltipProvider delayDuration={250}>
                <ul className="flex flex-col gap-1">
                  {newest.map((item) => <ActivityRow key={`${item.item}|${item.state}|${item.at ?? ''}`} item={item} />)}
                </ul>
              </TooltipProvider>
            )}
          </>
        ) : summary && (
          <p>
            {summary.waiting && <OutcomeIcon outcome="REVIEW_REQUIRED" size={15} className="mr-1.5 inline-block align-[-3px] text-outcome-review-fg" />}
            {summary.lead}
            {summary.agreed && <DecisionsLink text={summary.agreed} onOpen={onOpenDecisions} />}
            {summary.agreed && summary.waiting && ', '}
            {summary.waiting && <DecisionsLink text={summary.waiting} onOpen={onOpenDecisions} />}
            .
          </p>
        )}
      </div>
    </section>
  );
}

function byNewest(a: ReadingActivityItemOut, b: ReadingActivityItemOut): number {
  const at = (item: ReadingActivityItemOut) => (item.at ? Date.parse(item.at) || 0 : 0);
  return at(b) - at(a);
}

/** A count in the summary ("3 now agree"): a link to Decisions where the screen has one. The number is mono. */
function DecisionsLink({ text, onOpen }: { text: string; onOpen?: () => void }) {
  const [, number = '', words = text] = /^(\d+)(.*)$/.exec(text) ?? [];
  const content = <><span className="num">{number}</span>{words}</>;
  if (!onOpen) return <span>{content}</span>;
  return (
    <button type="button" onClick={onOpen} className="cursor-pointer rounded-sm font-medium underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
      {content}
    </button>
  );
}

/** One happening: a glyph, the sentence, and the role (the agent's name on hover). */
function ActivityRow({ item }: { item: ReadingActivityItemOut }) {
  const line = activityLine(item);
  // A try-again result names no page, so the page goes beside any line that does not say it.
  const page = !new RegExp(`\\bpage ${item.page}\\b`, 'i').test(line);
  return (
    <li data-state={item.state} className="flex items-start gap-1.5">
      <RowGlyph item={item} />
      <span className="min-w-0 flex-1">
        {line}
        {page && <span className="whitespace-nowrap text-muted-foreground"> · page <span className="num">{item.page}</span></span>}
      </span>
      <Tooltip>
        <TooltipTrigger asChild>
          <span tabIndex={0} className="shrink-0 cursor-help rounded-full border px-1.5 text-xs text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {roleName(item.agent)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="left">{item.agent}</TooltipContent>
      </Tooltip>
    </li>
  );
}

function RowGlyph({ item }: { item: ReadingActivityItemOut }) {
  if (needsYou(item)) return <OutcomeIcon outcome="REVIEW_REQUIRED" size={15} className="mt-0.5 shrink-0 text-outcome-review-fg" />;
  if (item.state === 'trying_again') return <RotateCw className="mt-0.5 size-[15px] shrink-0 animate-spin [animation-duration:2s] motion-reduce:animate-none" aria-hidden="true" />;
  return <Check className="mt-0.5 size-[15px] shrink-0 text-muted-foreground" aria-hidden="true" />;
}
