import { useId, useState } from 'react';
import { ChevronRight, CircleDashed, Equal, FileSearch, HelpCircle, Loader2, Minus } from 'lucide-react';

import type { AiReviewOut, AiReviewPage, AiReviewResult, AiReviewRow } from '@/api/review-items';
import { aiReviewEntries, aiReviewSummary, aiReviewWhat, aiReviewWhyParts, GROUP_OF, GROUP_TITLE, type AiReviewEntry, type AiReviewGroup } from './ai-review-lib';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { NumText, PageTag } from './words';

/** The glyph and word for one result: never colour alone. Only Different is coloured; the rest are calm greys. */
function ResultWord({ result }: { result: AiReviewResult }) {
  if (result === 'different') {
    return (
      <span data-slot="ai-review-word" className="inline-flex items-center gap-1.5 text-[13px] font-medium text-outcome-fail-fg">
        <OutcomeIcon outcome="FAIL" size={14} />
        Different
      </span>
    );
  }
  const [Glyph, word] =
    result === 'same' ? [Equal, 'Same'] : result === 'not_shown' ? [Minus, 'Not shown'] : result === 'unsure' ? [HelpCircle, 'Unsure'] : [CircleDashed, 'Unreadable'];
  return (
    <span data-slot="ai-review-word" className="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground">
      <Glyph aria-hidden="true" className="size-3.5" />
      {word}
    </span>
  );
}

/** The numbers side by side, in the number face, with what they measure and the page. */
function Numbers({ row, page }: { row: AiReviewRow; page: number }) {
  return (
    <span className="block min-w-0 text-[15px] text-foreground [overflow-wrap:anywhere]">
      Architect <span className="num">{row.architect_text}</span>
      {row.vendor_text !== null && (
        <>
          {' '}
          vs vendor <span className="num">{row.vendor_text}</span>
        </>
      )}
      <PageTag page={page} className="ml-2" />
      {row.measures && <NumText text={row.measures} className="block text-[13px] leading-5 text-muted-foreground" />}
    </span>
  );
}

/**
 * One row: the result, both numbers, Why (the reasons, folded) and Look. No person answers it (decision 2026-10-16): a different
 * row is clouded on the PDF by itself and never holds sign-off, so no answer buttons are offered,
 * whatever the server lists.
 */
function Row({ entry, onLook }: { entry: AiReviewEntry; onLook: (row: AiReviewRow, page: number) => void }) {
  const { row, page } = entry;
  const what = aiReviewWhat(row);
  const [whyOpen, setWhyOpen] = useState(false);
  const whyId = useId();
  // The reasons are long: folded behind Why so the row stays the numbers and what they measure (QA 3).
  const why = row.result !== 'same' ? aiReviewWhyParts(row) : { ai: null, evaluator: null };
  const hasWhy = why.ai !== null || why.evaluator !== null;
  return (
    // Sized by the section (`@container/air`), not the window: with the Assistant open the section
    // is narrow on a wide screen. The numbers keep 18rem and drop to their own line (QA 2026-10-16).
    <li data-slot="ai-review-row" data-entry-key={row.key} data-result={row.result} tabIndex={-1} className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2 outline-none focus-visible:bg-accent/60 @min-[36rem]/air:px-6">
      <span className="w-full shrink-0 @min-[40rem]/air:w-28">
        <ResultWord result={row.result} />
      </span>
      <span data-slot="ai-review-numbers" className="min-w-0 max-w-full grow basis-[18rem]">
        <Numbers row={row} page={page} />
        {hasWhy && (
          <span id={whyId} data-slot="ai-review-why" className="block">
            {whyOpen && (
              <>
                {why.ai !== null && <NumText text={why.ai} data-slot="ai-review-why-part" className="block text-[13px] leading-5 text-muted-foreground" />}
                {why.evaluator !== null && <NumText text={why.evaluator} data-slot="ai-review-why-part" className="block text-[13px] leading-5 text-muted-foreground" />}
              </>
            )}
          </span>
        )}
      </span>
      <span className="ml-auto flex min-w-0 shrink-0 flex-wrap items-center gap-1">
        {hasWhy && (
          <Button size="sm" variant="ghost" aria-expanded={whyOpen} aria-controls={whyId} onClick={() => setWhyOpen((now) => !now)}>
            <ChevronRight aria-hidden="true" className={cn('transition-transform motion-reduce:transition-none', whyOpen && 'rotate-90')} />
            Why
          </Button>
        )}
        <Button size="sm" variant="ghost" onClick={() => onLook(row, page)} aria-label={`Look at ${what}, page ${page}`}>
          <FileSearch aria-hidden="true" />
          Look
        </Button>
      </span>
    </li>
  );
}

function Group({ group, entries, defaultOpen, onLook }: { group: AiReviewGroup; entries: readonly AiReviewEntry[]; defaultOpen: boolean; onLook: (row: AiReviewRow, page: number) => void }) {
  const [open, setOpen] = useState(defaultOpen);
  const listId = useId();
  if (entries.length === 0) return null;
  const title = GROUP_TITLE[group];
  return (
    <div data-slot="ai-review-group" data-group={group} className="border-t border-border first:border-t-0">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={listId}
          onClick={() => setOpen((now) => !now)}
          className="flex min-h-11 w-full items-center gap-2 px-4 text-left text-[15px] font-medium outline-none focus-visible:bg-accent/60 focus-visible:ring-2 focus-visible:ring-ring sm:px-6"
        >
          <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none', open && 'rotate-90')} />
          {title}
          <span className="num text-[13px] font-normal text-muted-foreground">{entries.length}</span>
          {!open && <span className="ml-auto text-[13px] font-normal text-muted-foreground">Show</span>}
        </button>
      </h3>
      {open && (
        <ul id={listId} aria-label={title} className="divide-y divide-border border-t border-border">
          {entries.map((entry) => (
            <Row key={entry.row.key} entry={entry} onLook={onLook} />
          ))}
        </ul>
      )}
    </div>
  );
}

/** A page still being read, one that failed, or one not read: in plain words, never silent. */
function PageLine({ page }: { page: AiReviewPage }) {
  if (page.status === 'running') {
    return (
      <li role="status" className="flex items-center gap-1.5">
        <Loader2 aria-hidden="true" className="size-3.5 shrink-0 animate-spin motion-reduce:animate-none" />
        <span>
          AI is reading page <span className="num">{page.page}</span>…
        </span>
      </li>
    );
  }
  if (page.status === 'failed') {
    return (
      <li className="flex items-start gap-1.5">
        <OutcomeIcon outcome="REVIEW_REQUIRED" size={14} className="mt-0.5 shrink-0" />
        <span className="min-w-0 break-words">
          Couldn't read page <span className="num">{page.page}</span>
          {page.reason ? <>: <NumText text={page.reason} /></> : '.'}
        </span>
      </li>
    );
  }
  return (
    <li className="flex items-center gap-1.5">
      <Minus aria-hidden="true" className="size-3.5 shrink-0" />
      <span>
        Page <span className="num">{page.page}</span> was not read by the AI.
      </span>
    </li>
  );
}

/**
 * "Compared with the architect", the AI page review: per sheet, every architect number set beside
 * the vendor's of the same span. Different rows are open and are clouded on the PDF by themselves (no
 * person answers them, decision 2026-10-16); unsure, unreadable, same and not-shown rows are shown
 * folded and never clouded. Pages still being read, or that could not be read, are said in words.
 * Look opens both drawings at the row. Hidden when absent. It works nothing out itself: results
 * and counts are the server's.
 */
export function AiReview({ review, onLook }: { review: AiReviewOut | null; onLook: (row: AiReviewRow, page: number) => void }) {
  if (!review || review.pages.length === 0) return null;
  const entries = aiReviewEntries(review);
  const by = (group: AiReviewGroup) => entries.filter((entry) => GROUP_OF[entry.row.result] === group);
  const notDone = review.pages.filter((page) => page.status !== 'done');
  const groups: { group: AiReviewGroup; open: boolean }[] = [
    { group: 'different', open: true },
    { group: 'unsure', open: false },
    { group: 'same', open: false },
    { group: 'not_shown', open: false },
  ];
  return (
    <section aria-labelledby="ai-review-title" data-slot="ai-review" className="@container/air flex min-w-0 flex-col gap-3">
      <div className="flex flex-col gap-1">
        <h2 id="ai-review-title" className="text-[20px] leading-7 font-semibold tracking-[-0.01em]">
          Compared with the architect
        </h2>
        <p data-testid="ai-review-summary" className="text-[13px] text-muted-foreground">
          <NumText text={aiReviewSummary(review)} />
        </p>
        {notDone.length > 0 && (
          <ul data-testid="ai-review-pages" className="flex flex-col gap-1 text-[13px] text-muted-foreground">
            {notDone.map((page) => (
              <PageLine key={page.page} page={page} />
            ))}
          </ul>
        )}
      </div>
      {entries.length > 0 && (
        <div className="overflow-hidden rounded-xl border border-border">
          {groups.map(({ group, open }) => (
            <Group key={group} group={group} entries={by(group)} defaultOpen={open} onLook={onLook} />
          ))}
        </div>
      )}
    </section>
  );
}
