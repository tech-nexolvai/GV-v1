import { useState } from 'react';
import { CheckCircle2, Loader2, MinusCircle } from 'lucide-react';

import { ApiError, type CountertopResults, type NoCountertopAnswer, type PageWithoutCountertop } from '@/api/client';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { InfoTip } from '@/components/ui/info-tip';
import { Input } from '@/components/ui/input';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';
import { confirmManyWords, confirmedCount, waitingPages } from '@/lib/countertop-results';
import { carriedWords } from '@/lib/revisions';
import { SIGNED_OFF_NOTE, useSignedOff } from '@/lib/signed-off';
import { cn } from '@/lib/utils';

/** A second countertop line an AI named on a page whose countertop was read (#1108). */
export type RowNotChecked = NonNullable<CountertopResults['rows_not_checked']>[number];

/** A drawing on a read page that was not read itself (#1193 review). */
export type DrawingNotRead = NonNullable<CountertopResults['drawings_not_read']>[number];

/** Records one reviewer answer on a listed page; rejects with the server's words when refused. */
export type AnswerNoCountertop = (page: PageWithoutCountertop, answer: NoCountertopAnswer) => Promise<void>;

/**
 * "Confirm these N pages" (#1195): "Confirm: no countertop here" on each page given, with one optional
 * note. Rejects with the server's error: an `ApiError` with status 409 when the list changed since
 * it was loaded (nothing was saved).
 */
export type ConfirmNoCountertopPages = (pages: readonly PageWithoutCountertop[], note: string) => Promise<void>;

/** The tooltip on "There is a countertop" (#1191). */
export const THERE_IS_A_COUNTERTOP_TIP = 'This cannot be undone: the page becomes a countertop for you to decide.';

/** On a signed-off review, a page nobody confirmed was signed before #1191 asked for confirmations. */
export const SIGNED_BEFORE_CONFIRMATIONS = 'Signed before confirmations were asked';

/** A page code proved blank from the PDF (#1195): listed, no answer needed. */
export const NO_CLICK_NEEDED = 'Nothing drawn: no click needed';

/** Shown after a 409: the list was reloaded, nothing was saved (#1195). */
export const LIST_RELOADED = 'The list changed since you opened it, so nothing was saved. It has been reloaded: check the pages and confirm again.';

/** The tag on a page left unticked because there is a reason to doubt it (#1195). */
export const LOOK_FIRST = 'Look first';

/** "12 Oct 2026, 14:05" in the reader's own locale. */
function when(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

/**
 * Pages where both AIs found no countertop line (#1093), or where code found no vendor drawing or row
 * to read (#1193); each says why. Since #1191 each needs one reviewer click
 * before sign-off — "Confirm: no countertop here", or "There is a countertop", which turns the page
 * into a "needs you" item like a split page. Nothing is pre-selected; the server refuses sign-off
 * until every page is answered. The buttons are off once the review is signed off.
 *
 * Since #1195 a page code proved blank (a title or divider page, nothing drawn) stays listed with
 * "no click needed", and "Confirm these N pages" answers every ticked page waiting in one action:
 * each waiting page has a tickbox, ticked unless the server gives a reason to doubt it, and the count
 * follows the ticks. A 409 (the list changed) reloads the list; unticked pages keep waiting.
 */
export function NoCountertopPages({
  pages,
  onAnswer,
  onConfirmMany,
  onReload,
}: {
  pages: readonly PageWithoutCountertop[];
  onAnswer?: AnswerNoCountertop;
  onConfirmMany?: ConfirmNoCountertopPages;
  /** Reloads the countertop results after the server said the list changed (409). */
  onReload?: () => Promise<void> | void;
}) {
  const signedOff = useSignedOff();
  const [saving, setSaving] = useState<string | null>(null);
  const [errors, setErrors] = useState<Record<string, string>>({});
  // A reviewer's own ticks, by record; any other waiting page is ticked unless it is doubtful.
  const [ticks, setTicks] = useState<Record<string, boolean>>({});
  const [note, setNote] = useState('');
  const [bulkMessage, setBulkMessage] = useState<{ kind: 'reloaded' | 'error'; text: string } | null>(null);
  if (pages.length === 0) return null;
  const { confirmed, total, noClickNeeded } = confirmedCount(pages);
  const allConfirmed = confirmed === total;
  // A signed-off review never shows "Needs you": a page nobody confirmed was signed before #1191.
  const tone = allConfirmed ? 'text-outcome-pass-fg' : signedOff ? 'text-muted-foreground' : 'text-outcome-review-fg';
  // Older readings nobody answered yet first: they are not known to have no countertop (#1191).
  const ordered = [...pages].sort((a, b) => Number(Boolean(b.older_reading && !b.confirmation)) - Number(Boolean(a.older_reading && !a.confirmation)) || a.page_number - b.page_number);
  const waiting = waitingPages(pages);
  const bulk = Boolean(onConfirmMany) && !signedOff && waiting.length > 0;
  const isTicked = (page: PageWithoutCountertop) => ticks[page.record_id ?? ''] ?? !page.doubtful;
  const ticked = waiting.filter(isTicked);

  async function answer(page: PageWithoutCountertop, choice: NoCountertopAnswer) {
    if (!onAnswer || !page.record_id || saving !== null || signedOff) return;
    const key = page.record_id;
    setSaving(key);
    setBulkMessage(null);
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

  async function confirmMany() {
    if (!onConfirmMany || saving !== null || signedOff || ticked.length === 0) return;
    setSaving('bulk');
    setBulkMessage(null);
    try {
      await onConfirmMany(ticked, note);
      setNote('');
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setBulkMessage({ kind: 'reloaded', text: LIST_RELOADED });
        try {
          await onReload?.();
        } catch {
          // The message stands; the next refresh reloads the list.
        }
      } else {
        setBulkMessage({ kind: 'error', text: `Not saved: ${error instanceof Error ? error.message : String(error)}` });
      }
    } finally {
      setSaving(null);
    }
  }

  return (
    <section data-slot="no-countertop-pages" aria-labelledby="no-countertop-pages" className="flex flex-col gap-2 rounded-xl border border-dashed px-4 py-3 font-sans">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id="no-countertop-pages" className="text-sm font-medium">Pages not checked</h2>
        <p className="inline-flex flex-wrap items-center gap-x-2 text-xs">
          {total > 0 && (
            <span data-part="confirmed-count" className={cn('inline-flex items-center gap-1 font-medium', tone)}>
              {allConfirmed ? <CheckCircle2 className="size-3.5" aria-hidden="true" /> : !signedOff && <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} />}
              <span><span className="num">{confirmed}</span> of <span className="num">{total}</span> confirmed</span>
            </span>
          )}
          {noClickNeeded > 0 && (
            <span data-part="no-click-count" className="text-muted-foreground">
              <span className="num">{noClickNeeded}</span> nothing drawn
            </span>
          )}
        </p>
      </div>
      <p className="text-xs text-muted-foreground">
        Nothing on these pages was checked: the AIs found no countertop line, or code found no vendor drawing or row to read. Each page says why. Confirm each page, or say there is a countertop, before signing off.
      </p>
      {bulk && (
        <div data-part="confirm-many" className="flex flex-col gap-2 rounded-md border bg-muted/30 p-2 sm:flex-row sm:items-center">
          <Button type="button" size="sm" disabled={saving !== null || ticked.length === 0} onClick={() => void confirmMany()}>
            {saving === 'bulk' && <Loader2 className="animate-spin" aria-hidden="true" />}
            {confirmManyWords(ticked.length)}
          </Button>
          <Input
            aria-label="Note for these confirmations (optional)"
            placeholder="Note (optional)"
            maxLength={500}
            value={note}
            disabled={saving !== null}
            onChange={(event) => setNote(event.target.value)}
            className="h-8 sm:max-w-72"
          />
          <InfoTip label="What confirming these pages does">
            <p>Records &ldquo;Confirm: no countertop here&rdquo; on each ticked page, with your name, the time and your note, as if you clicked each one.</p>
            <p>Untick a page to leave it waiting and answer it on its own. Pages marked &ldquo;{LOOK_FIRST}&rdquo; start unticked: look at them before you tick them.</p>
          </InfoTip>
        </div>
      )}
      {bulkMessage && (
        <p role={bulkMessage.kind === 'error' ? 'alert' : 'status'} data-part="confirm-many-message" className={cn('text-xs', bulkMessage.kind === 'error' ? 'text-destructive' : 'text-outcome-review-fg')}>
          {bulkMessage.text}
        </p>
      )}
      <ul className="flex flex-col divide-y text-sm">
        {ordered.map((page) => {
          const key = page.record_id ?? `page-${page.page_number}`;
          const confirmation = page.confirmation ?? null;
          const carried = confirmation ? carriedWords(confirmation.from_revision, confirmation.confirmed_by) : null;
          const off = signedOff || !onAnswer || !page.record_id || saving !== null;
          const noClick = page.needs_answer === false && !confirmation;
          const selectable = bulk && waiting.includes(page);
          return (
            <li key={key} data-part="no-countertop-page" data-page={page.page_number} className="flex flex-col gap-2 py-2 first:pt-0 last:pb-0">
              <div className="flex gap-3">
                {selectable ? (
                  <Checkbox
                    data-part="include-page"
                    className="mt-0.5"
                    aria-label={`Include page ${page.page_number}`}
                    checked={isTicked(page)}
                    disabled={saving !== null}
                    onCheckedChange={(value) => setTicks((current) => ({ ...current, [key]: value === true }))}
                  />
                ) : bulk ? (
                  <span className="size-4 shrink-0" aria-hidden="true" />
                ) : null}
                <span className="w-16 shrink-0 text-muted-foreground">Page <span className="num">{page.page_number}</span></span>
                <span className="text-muted-foreground">
                  {page.older_reading && <span data-part="older-reading" className="mr-1.5 rounded border px-1 py-px text-xs font-medium text-foreground">Older reading</span>}
                  {selectable && page.doubtful && (
                    <span data-part="look-first" title="There is a reason to doubt this page. Look at it before you tick it." className="mr-1.5 rounded border px-1 py-px text-xs font-medium text-foreground">{LOOK_FIRST}</span>
                  )}
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
                      {confirmation.note ? ` · ${confirmation.note}` : ''}
                    </span>
                  </span>
                ) : noClick ? (
                  <span data-part="no-click-needed" className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground">
                    <MinusCircle className="size-3.5" aria-hidden="true" /> {NO_CLICK_NEEDED}
                  </span>
                ) : signedOff ? (
                  <span data-part="signed-before" className="text-xs text-muted-foreground">{SIGNED_BEFORE_CONFIRMATIONS}</span>
                ) : (
                  <span data-part="unconfirmed" className="inline-flex items-center gap-1 text-xs font-medium text-outcome-review-fg">
                    <OutcomeIcon outcome="REVIEW_REQUIRED" size={12} /> Needs you
                  </span>
                )}
                <div className="ml-auto flex flex-wrap gap-2">
                  {!confirmation && !noClick && (
                    <Button type="button" size="sm" variant="outline" disabled={off} onClick={() => void answer(page, 'no_countertop')}>
                      {saving === key && <Loader2 className="animate-spin" aria-hidden="true" />}
                      Confirm: no countertop here
                    </Button>
                  )}
                  <Button type="button" size="sm" variant={confirmation || noClick ? 'ghost' : 'outline'} disabled={off} title={THERE_IS_A_COUNTERTOP_TIP} onClick={() => void answer(page, 'countertop')}>
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

/**
 * Drawings not read (#1193 review): a drawing on a page that was read, which was not read itself —
 * on a page with headings it has no role of its own. Listed with where it is and why, never
 * blocking; the signed report lists them too.
 */
export function DrawingsNotRead({ drawings }: { drawings: readonly DrawingNotRead[] }) {
  if (drawings.length === 0) return null;
  return (
    <ListedOnly
      slot="drawings-not-read"
      title="Drawings not read"
      items={drawings.map((drawing, index) => ({ key: `drawing-${drawing.page_number}-${index}`, page: drawing.page_number, reason: drawing.reason }))}
    />
  );
}

/** The lists, side by side on a wide screen and stacked on a phone; nothing when all are empty. */
export function ListedPages({
  pagesWithoutCountertop,
  rowsNotChecked,
  drawingsNotRead = [],
  onAnswer,
  onConfirmMany,
  onReload,
}: {
  pagesWithoutCountertop: readonly PageWithoutCountertop[];
  rowsNotChecked: readonly RowNotChecked[];
  drawingsNotRead?: readonly DrawingNotRead[];
  /** Records a reviewer's answer on a page with no countertop (#1191); without it the buttons are off. */
  onAnswer?: AnswerNoCountertop;
  /** "Confirm these N pages" (#1195); without it the action is not offered. */
  onConfirmMany?: ConfirmNoCountertopPages;
  /** Reloads the results after a 409 (#1195). */
  onReload?: () => Promise<void> | void;
}) {
  const shown = [pagesWithoutCountertop.length, rowsNotChecked.length, drawingsNotRead.length].filter((count) => count > 0).length;
  if (shown === 0) return null;
  return (
    <div data-slot="listed-pages" className={cn('grid items-start gap-3', shown > 1 && 'lg:grid-cols-2')}>
      <NoCountertopPages pages={pagesWithoutCountertop} onAnswer={onAnswer} onConfirmMany={onConfirmMany} onReload={onReload} />
      <RowsNotChecked rows={rowsNotChecked} />
      <DrawingsNotRead drawings={drawingsNotRead} />
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
