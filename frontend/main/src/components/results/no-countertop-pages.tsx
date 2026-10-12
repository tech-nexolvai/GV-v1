import { useState } from 'react';
import { CheckCircle2, Loader2 } from 'lucide-react';

import type { CountertopResults, NoCountertopAnswer, PageWithoutCountertop } from '@/api/client';
import { Button } from '@/components/ui/button';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { confirmedCount } from '@/lib/countertop-results';
import { carriedWords } from '@/lib/revisions';
import { SIGNED_OFF_NOTE, useSignedOff } from '@/lib/signed-off';
import { cn } from '@/lib/utils';

/** A second countertop line an AI named on a page whose countertop was read (#1108). */
export type RowNotChecked = NonNullable<CountertopResults['rows_not_checked']>[number];

/** Records one reviewer answer on a listed page; rejects with the server's words when refused. */
export type AnswerNoCountertop = (page: PageWithoutCountertop, answer: NoCountertopAnswer) => Promise<void>;

/** The tooltip on "There is a countertop" (#1191). */
export const THERE_IS_A_COUNTERTOP_TIP = 'This cannot be undone: the page becomes a countertop for you to decide.';

/** On a signed-off review, a page nobody confirmed was signed before #1191 asked for confirmations. */
export const SIGNED_BEFORE_CONFIRMATIONS = 'Signed before confirmations were asked';

/** "12 Oct 2026, 14:05" in the reader's own locale. */
function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * Pages where both AIs found no countertop line (#1093). Since #1191 each needs one reviewer click
 * before sign-off — "Confirm: no countertop here", or "There is a countertop", which turns the page
 * into a "needs you" item like a split page. Nothing is pre-selected; the server refuses sign-off
 * until every page is answered. The buttons are off once the review is signed off.
 */
export function NoCountertopPages({ pages, onAnswer }: { pages: readonly PageWithoutCountertop[]; onAnswer?: AnswerNoCountertop }) {
  const signedOff = useSignedOff();
  const [saving, setSaving] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  if (pages.length === 0) return null;
  const { confirmed, total } = confirmedCount(pages);
  const allConfirmed = confirmed === total;
  // A signed-off review never shows "Needs you": a page nobody confirmed was signed before #1191.
  const tone = allConfirmed ? 'text-outcome-pass-fg' : signedOff ? 'text-muted-foreground' : 'text-outcome-review-fg';
  // Older readings nobody answered yet first: they are not known to have no countertop (#1191).
  const ordered = [...pages].sort((a, b) => Number(Boolean(b.older_reading && !b.confirmation)) - Number(Boolean(a.older_reading && !a.confirmation)) || a.page_number - b.page_number);

  async function answer(page: PageWithoutCountertop, choice: NoCountertopAnswer) {
    if (!onAnswer || !page.record_id || saving !== null || signedOff) return;
    const key = page.record_id;
    setSaving(key);
    setErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      await onAnswer(page, choice);
    } catch (error) {
      setErrors((current) => ({ ...current, [key]: error instanceof Error ? error.message : String(error) }));
    } finally {
      setSaving(null);
    }
  }

  return (
    <section data-slot="no-countertop-pages" aria-labelledby="no-countertop-pages" className="flex flex-col gap-2 rounded-xl border border-dashed px-4 py-3 font-sans">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id="no-countertop-pages" className="text-sm font-medium">Pages with no countertop found</h2>
        <p
          data-part="confirmed-count"
          className={cn('inline-flex items-center gap-1 text-xs font-medium', tone)}
        >
          {allConfirmed ? <CheckCircle2 className="size-3.5" aria-hidden="true" /> : !signedOff && <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} />}
          <span><span className="num">{confirmed}</span> of <span className="num">{total}</span> confirmed</span>
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        Both AIs found no countertop line on these pages, so nothing on them was checked. Confirm each page, or say there is a countertop, before signing off.
      </p>
      <ul className="flex flex-col divide-y text-sm">
        {ordered.map((page) => {
          const key = page.record_id ?? `page-${page.page_number}`;
          const confirmation = page.confirmation ?? null;
          const carried = confirmation ? carriedWords(confirmation.from_revision, confirmation.confirmed_by) : null;
          const off = signedOff || !onAnswer || !page.record_id || saving !== null;
          return (
            <li key={key} data-part="no-countertop-page" data-page={page.page_number} className="flex flex-col gap-2 py-2 first:pt-0 last:pb-0">
              <div className="flex gap-3">
                <span className="w-16 shrink-0 text-muted-foreground">Page <span className="num">{page.page_number}</span></span>
                <span className="text-muted-foreground">
                  {page.older_reading && <span data-part="older-reading" className="mr-1.5 rounded border px-1 py-px text-xs font-medium text-foreground">Older reading</span>}
                  {page.reason}
                </span>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {confirmation ? (
                  <span data-part="confirmed" className="inline-flex flex-wrap items-center gap-1 text-xs font-medium text-outcome-pass-fg">
                    <CheckCircle2 className="size-3.5" aria-hidden="true" />
                    Confirmed: no countertop here
                    <span className="font-normal text-muted-foreground">
                      {carried ?? `By ${confirmation.confirmed_by}`} · <span className="num">{when(confirmation.confirmed_at)}</span>
                    </span>
                  </span>
                ) : signedOff ? (
                  <span data-part="signed-before" className="text-xs text-muted-foreground">{SIGNED_BEFORE_CONFIRMATIONS}</span>
                ) : (
                  <span data-part="unconfirmed" className="inline-flex items-center gap-1 text-xs font-medium text-outcome-review-fg">
                    <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} /> Needs you
                  </span>
                )}
                <div className="ml-auto flex flex-wrap gap-2">
                  {!confirmation && (
                    <Button type="button" size="sm" variant="outline" disabled={off} onClick={() => void answer(page, 'no_countertop')}>
                      {saving === key && <Loader2 className="animate-spin" aria-hidden="true" />}
                      Confirm: no countertop here
                    </Button>
                  )}
                  <Button type="button" size="sm" variant={confirmation ? 'ghost' : 'outline'} disabled={off} title={THERE_IS_A_COUNTERTOP_TIP} onClick={() => void answer(page, 'countertop')}>
                    There is a countertop
                  </Button>
                </div>
              </div>
              {errors[key] && <p role="alert" className="text-xs text-destructive">Not saved: {errors[key]}</p>}
            </li>
          );
        })}
      </ul>
      {signedOff && <p role="note" className="text-xs text-muted-foreground">{SIGNED_OFF_NOTE}</p>}
    </section>
  );
}

/**
 * Countertop lines not checked (#1108): a second countertop line an AI named on a page. V1 reads one
 * line per page, so this one was not checked. Listed with the API's reason, never blocking, nothing
 * to click; the signed report lists them too.
 */
export function RowsNotChecked({ rows }: { rows: readonly RowNotChecked[] }) {
  if (rows.length === 0) return null;
  return (
    <ListedOnly
      slot="rows-not-checked"
      title="Countertop lines not checked"
      items={rows.map((row, index) => ({ key: `row-${row.page_number}-${index}`, page: row.page_number, reason: row.reason }))}
    />
  );
}

/** Both lists, side by side on a wide screen and stacked on a phone; nothing when both are empty. */
export function ListedPages({
  pagesWithoutCountertop,
  rowsNotChecked,
  onAnswer,
}: {
  pagesWithoutCountertop: readonly PageWithoutCountertop[];
  rowsNotChecked: readonly RowNotChecked[];
  /** Records a reviewer's answer on a page with no countertop (#1191); without it the buttons are off. */
  onAnswer?: AnswerNoCountertop;
}) {
  if (pagesWithoutCountertop.length === 0 && rowsNotChecked.length === 0) return null;
  const both = pagesWithoutCountertop.length > 0 && rowsNotChecked.length > 0;
  return (
    <div data-slot="listed-pages" className={cn('grid items-start gap-3', both && 'lg:grid-cols-2')}>
      <NoCountertopPages pages={pagesWithoutCountertop} onAnswer={onAnswer} />
      <RowsNotChecked rows={rowsNotChecked} />
    </div>
  );
}

function ListedOnly({ slot, title, items }: { slot: string; title: string; items: { key: string; page: number; reason: string }[] }) {
  return (
    <section data-slot={slot} aria-labelledby={slot} className="flex flex-col gap-2 rounded-xl border border-dashed px-4 py-3 font-sans">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id={slot} className="text-sm font-medium">{title}</h2>
        <p className="text-xs text-muted-foreground">Listed only. Nothing to decide.</p>
      </div>
      <ul className="flex flex-col gap-1.5 text-sm">
        {items.map((item) => (
          <li key={item.key} className="flex gap-3">
            <span className="w-16 shrink-0 text-muted-foreground">Page <span className="num">{item.page}</span></span>
            <span className="text-muted-foreground">{item.reason}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}
