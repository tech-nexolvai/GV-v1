import { Check, RotateCw, ScanText } from 'lucide-react';

import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import {
  activityGlyph,
  activityLine,
  afterReadingSentence,
  readingHeadline,
  roleTag,
  whatStopped,
  type ReadingActivityItemOut,
  type ReadingActivityOut,
} from '@/lib/reading-activity';
import { cn } from '@/lib/utils';

/** How many of the server's items the panel lists while reading; the rest are in its counts. */
export const SHOWN_ITEMS = 4;

/**
 * The live reading activity (#1239), once, under the review stepper (it shows on every tab).
 *
 * While reading: the server's `now` (or the page count when `now` only repeats a row), then the
 * server's first items in its order — an inspector stopping an AI, the page sorter leaving a page
 * unread, an AI trying again and how it went — each with a glyph by state and its role tag (the
 * agent's name on hover).
 *
 * After a finished reading: the server's summary sentence, an "Open Decisions" link while anything
 * waits for a person, and a collapsed "Show what stopped (N)" list of every item the API returned
 * that waits, so no stop reason becomes unreachable once the reading ends ("never silent"). After a
 * reading that did not finish: only its sentence, plainly.
 *
 * One `aria-live="polite"` region that stays mounted. With no activity (null before the running
 * attempt's first event) it renders nothing, and the stepper's progress line still says where the
 * reading is. Every sentence is the server's; nothing here is a result.
 */
export function ReadingActivity({
  activity,
  reading,
  progress,
  onOpenDecisions,
  className,
}: {
  /** `PackageOut.activity`. */
  activity: ReadingActivityOut | null | undefined;
  /** The package is being read now (the Reading step is current). */
  reading: boolean;
  /** The worker's progress line, for "Reading page k of N…". */
  progress?: { pagesDone: number | null; pagesTotal: number | null } | null;
  /** Opens Decisions (the "Needs you" queue). */
  onOpenDecisions?: () => void;
  className?: string;
}) {
  if (!activity) return null;
  const unfinished = !reading && !activity.finished;
  const after = reading ? null : afterReadingSentence(activity);
  if (!reading && !after) return null;

  const rows = activity.items.slice(0, SHOWN_ITEMS);
  const stopped = reading || unfinished ? null : whatStopped(activity);
  const waiting = activity.summary.waiting > 0;
  return (
    <section
      data-tw
      data-slot="reading-activity"
      data-reading={reading ? 'true' : 'false'}
      data-finished={activity.finished ? 'true' : 'false'}
      aria-label="Reading activity"
      className={cn('flex flex-col gap-1.5 rounded-xl border border-border bg-card px-3 py-2 font-sans text-sm', className)}
    >
      <div role="status" aria-live="polite" className="flex flex-col gap-1.5">
        {reading ? (
          <>
            <p className="flex items-center gap-1.5 font-medium">
              <ScanText className="size-4 shrink-0" aria-hidden="true" />
              {readingHeadline(progress, activity.now, rows.map(activityLine))}
            </p>
            {rows.length > 0 && <ActivityRows items={rows} />}
          </>
        ) : unfinished ? (
          // A reading that did not finish says so plainly: no counts, no links (#1242).
          <p className="text-muted-foreground">{after}</p>
        ) : (
          <p>
            {waiting && <OutcomeIcon outcome="REVIEW_REQUIRED" size={15} className="mr-1.5 inline-block align-[-3px] text-outcome-review-fg" />}
            {after}
            {/* The server's counts are its own; the link only opens the queue, it claims no number. */}
            {waiting && onOpenDecisions && (
              <>
                {' '}
                <button type="button" onClick={onOpenDecisions} className="cursor-pointer rounded-sm font-medium underline underline-offset-2 hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none">
                  Open Decisions
                </button>
              </>
            )}
          </p>
        )}
      </div>
      {stopped && stopped.items.length > 0 && (
        <details data-slot="what-stopped" className="group">
          <summary className="cursor-pointer text-muted-foreground select-none hover:text-foreground">
            Show what stopped (<span className="num">{stopped.total}</span>)
          </summary>
          <div className="mt-1.5 flex flex-col gap-1">
            {stopped.items.length < stopped.total && (
              <p className="text-xs text-muted-foreground">
                showing <span className="num">{stopped.items.length}</span> of <span className="num">{stopped.total}</span>
              </p>
            )}
            <ActivityRows items={stopped.items} />
          </div>
        </details>
      )}
    </section>
  );
}

/** The rows, with their own tooltip provider so the panel works wherever it is placed. */
function ActivityRows({ items }: { items: readonly ReadingActivityItemOut[] }) {
  return (
    <TooltipProvider delayDuration={250}>
      <ul className="flex flex-col gap-1">
        {/* The API has no document id on an item, and two files can share a page and question id:
            the position keeps each key unique. */}
        {items.map((item, index) => <ActivityRow key={`${item.agent}|${item.item}|${index}`} item={item} />)}
      </ul>
    </TooltipProvider>
  );
}

/** One happening: a glyph, the server's sentence, and the role (the agent's name on hover). */
function ActivityRow({ item }: { item: ReadingActivityItemOut }) {
  const line = activityLine(item);
  // A line that does not name its page gets the page beside it.
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
            {roleTag(item)}
          </span>
        </TooltipTrigger>
        <TooltipContent side="left">{item.agent}</TooltipContent>
      </Tooltip>
    </li>
  );
}

/** The glyph by state: amber needs-you (stopped, waiting), the turning arrow (trying again), or a plain tick. */
function RowGlyph({ item }: { item: ReadingActivityItemOut }) {
  const glyph = activityGlyph(item);
  return (
    <span data-glyph={glyph} className="mt-0.5 flex shrink-0">
      {glyph === 'needs-you' && <OutcomeIcon outcome="REVIEW_REQUIRED" size={15} className="text-outcome-review-fg" />}
      {glyph === 'trying-again' && <RotateCw className="size-[15px] animate-spin [animation-duration:2s] motion-reduce:animate-none" aria-hidden="true" />}
      {glyph === 'done' && <Check className="size-[15px] text-muted-foreground" aria-hidden="true" />}
    </span>
  );
}
