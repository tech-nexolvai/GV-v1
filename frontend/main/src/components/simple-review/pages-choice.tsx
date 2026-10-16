import { FileSearch, FileX2, Loader2 } from 'lucide-react';

import type { ReviewItem } from '@/api/review-items';
import { Button } from '@/components/ui/button';
import { ChoiceCard } from './choice-card';
import type { SaveError } from './failures-strip';
import { NumText } from './words';

/** "p.9, p.10 and p.11", each page number in the number face and the words in the word face. */
function PageList({ pages }: { pages: readonly ReviewItem[] }) {
  const named = pages.filter((page) => page.page != null);
  return (
    <>
      {named.map((page, index) => (
        <span key={page.key}>
          {index > 0 && (index === named.length - 1 ? ' and ' : ', ')}
          <span className="num">p.{page.page}</span>
        </span>
      ))}
    </>
  );
}

/**
 * The pages every AI said have no countertop, as one question (#1368): one click confirms them all
 * ("These N pages have no countertop"), and any one page can be answered "There is a countertop".
 * Each choice says what it leads to. A page can be looked at first (Open PDFs at that page).
 *
 * Only pages every AI reader positively said have no countertop are in the one-click proposal
 * (#1394). An older or doubtful reading is listed apart under "Decide this page", with nothing
 * proposed and both answers offered: "No countertop here" or "There is a countertop".
 */
export function PagesChoice({
  pages,
  toDecide = [],
  busyKey,
  groupKey,
  errors,
  onConfirmAll,
  onThereIsOne,
  onNoCountertop,
  onLook,
}: {
  /** The open "no countertop?" pages, in page order. */
  pages: readonly ReviewItem[];
  /** The open pages to decide one by one (older or doubtful readings), in page order (#1394). */
  toDecide?: readonly ReviewItem[];
  busyKey: string | null;
  /** Where the group's busy state and save error are kept. */
  groupKey: string;
  errors: Readonly<Record<string, SaveError>>;
  onConfirmAll: () => void;
  onThereIsOne: (page: ReviewItem) => void;
  /** "No countertop here" on one page to decide (#1394). */
  onNoCountertop?: (page: ReviewItem) => void;
  onLook: (page: ReviewItem) => void;
}) {
  if (pages.length === 0 && toDecide.length === 0) return null;
  const many = pages.length > 1;
  const saving = busyKey !== null && (busyKey === groupKey || [...pages, ...toDecide].some((page) => page.key === busyKey));
  const groupError = errors[groupKey];
  // No page may be proposed at all (every listed page is "Decide this page"): never read pages[0] then.
  const title = many ? `These ${pages.length} pages have no countertop` : pages.length === 1 ? `Page ${pages[0].page ?? ''} has no countertop`.trim() : '';

  return (
    <section aria-labelledby="pages-choice-title" data-slot="pages-choice" data-entry-key="no-countertop-pages" tabIndex={-1} className="flex flex-col gap-3 rounded-xl outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 id="pages-choice-title" className="text-[20px] leading-7 font-semibold tracking-[-0.01em]">
          No countertop on {many || toDecide.length > 0 ? 'these pages' : 'this page'}?{' '}
          <span className="num ml-1 text-[15px] font-normal text-muted-foreground">{pages.length + toDecide.length}</span>
        </h2>
        <p className="text-[13px] leading-5 text-muted-foreground">Look, then answer.</p>
      </div>

      {pages.length > 0 && (
      <div className="flex flex-col gap-3 rounded-xl border border-border bg-background p-3 sm:p-4">
        <ChoiceCard
          title={title}
          proposal="every AI reader"
          leadsTo={<>Will be marked as no countertop: <PageList pages={pages} />. Nothing on {many ? 'them' : 'it'} is checked, and the cloud PDF lists {many ? 'them' : 'it'} as having no countertop.</>}
          busy={busyKey === groupKey}
          disabled={saving}
          onChoose={onConfirmAll}
        />
        {groupError && (
          <p role="alert" className="flex flex-wrap items-center gap-2 text-[13px] text-outcome-fail-fg">
            Not saved: {groupError.message}
            <Button size="xs" variant="ghost" onClick={groupError.retry}>Retry</Button>
          </p>
        )}

        <div className="flex flex-col gap-1">
          <p className="px-1 pt-1 text-[13px] leading-5 text-muted-foreground">
            Or, if a page does show one: <span className="text-foreground">There is a countertop</span> sends that page to be read and checked as a countertop in the next run.
          </p>
          <ul className="divide-y divide-border">
            {pages.map((page) => {
              const error = errors[page.key];
              return (
                <li key={page.key} data-entry-key={page.key} tabIndex={-1} className="flex min-h-14 flex-wrap items-center gap-x-3 gap-y-1 px-1 py-2 outline-none focus-visible:bg-accent/60">
                  <FileX2 aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                  <span className="num w-10 shrink-0 text-[15px] font-medium text-foreground">p.{page.page ?? '?'}</span>
                  <NumText text={page.why} className="min-w-0 flex-1 basis-40 text-[13px] leading-5 text-muted-foreground" />
                  <span className="flex shrink-0 items-center gap-1">
                    <Button size="sm" variant="ghost" onClick={() => onLook(page)} aria-label={`Look at page ${page.page ?? ''}`}>
                      <FileSearch aria-hidden="true" />
                      Look
                    </Button>
                    <Button size="sm" variant="outline" disabled={saving} onClick={() => onThereIsOne(page)} aria-label={`There is a countertop on page ${page.page ?? ''}`}>
                      {busyKey === page.key && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
                      There is a countertop
                    </Button>
                  </span>
                  {error && (
                    <p role="alert" className="flex basis-full flex-wrap items-center gap-2 text-[13px] text-outcome-fail-fg">
                      Not saved: {error.message}
                      <Button size="xs" variant="ghost" onClick={error.retry}>Retry</Button>
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      </div>
      )}

      {toDecide.length > 0 && (
        <div data-slot="pages-to-decide" className="flex flex-col gap-1 rounded-xl border border-border bg-background p-3 sm:p-4">
          <p className="px-1 text-[13px] leading-5 text-muted-foreground">
            The AIs did not clearly say these pages have no countertop. Nothing is proposed: look, then decide each page.
          </p>
          <ul className="divide-y divide-border">
            {toDecide.map((page) => {
              const error = errors[page.key];
              return (
                <li key={page.key} data-entry-key={page.key} tabIndex={-1} className="flex min-h-14 flex-wrap items-center gap-x-3 gap-y-1 px-1 py-2 outline-none focus-visible:bg-accent/60">
                  <FileSearch aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
                  <span className="flex min-w-0 flex-1 basis-40 flex-col">
                    <span className="text-[15px] font-medium text-foreground">Decide this page <span className="num">p.{page.page ?? '?'}</span></span>
                    <NumText text={page.why} className="text-[13px] leading-5 text-muted-foreground" />
                  </span>
                  {/* On a phone the three buttons take a line of their own and wrap there; a button's
                      words wrap too, so "There is a countertop" is never clipped at 375 px (QA 3). */}
                  <span data-slot="page-decide-actions" className="flex min-w-0 basis-full flex-wrap items-center gap-1 sm:basis-auto sm:shrink-0">
                    <Button size="sm" variant="ghost" className="max-w-full" onClick={() => onLook(page)} aria-label={`Look at page ${page.page ?? ''}`}>
                      <FileSearch aria-hidden="true" />
                      Look
                    </Button>
                    <Button size="sm" variant="outline" className="h-auto min-h-8 max-w-full whitespace-normal" disabled={saving || !onNoCountertop} onClick={() => onNoCountertop?.(page)} aria-label={`No countertop on page ${page.page ?? ''}`}>
                      No countertop here
                    </Button>
                    <Button size="sm" variant="outline" className="h-auto min-h-8 max-w-full whitespace-normal" disabled={saving} onClick={() => onThereIsOne(page)} aria-label={`There is a countertop on page ${page.page ?? ''}`}>
                      {busyKey === page.key && <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" />}
                      There is a countertop
                    </Button>
                  </span>
                  {error && (
                    <p role="alert" className="flex basis-full flex-wrap items-center gap-2 text-[13px] text-outcome-fail-fg">
                      Not saved: {error.message}
                      <Button size="xs" variant="ghost" onClick={error.retry}>Retry</Button>
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
