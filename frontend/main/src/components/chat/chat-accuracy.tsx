import { useState } from 'react';
import { Gauge, ScanLine } from 'lucide-react';

import { NoPicture, ZoomPane } from '@/components/chat/chat-evidence';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover';
import type { ChatAccuracy, ChatCite } from '@/lib/chat-api';
import { ACCURACY_MEANING, accuracyRows, accuracyTone, accuracyWords, coverageFactorWords, pointsWords, TEST_SET_LINE, type AccuracyTone } from '@/lib/chat-accuracy';
import { cn } from '@/lib/utils';

/**
 * Chat v5 (CONTRACT5-accuracy, #1483): how far the answer can be believed, measured by code.
 * The Accuracy badge closes every answer that has one ("Accuracy 92%", green / orange / yellow, grey
 * when there was nothing to check); clicking it shows the claims, how each was checked and its
 * points. The evidence chips ("p.23 ▣ 25 1/2"") open the crop of the spot each stated number was
 * read from. Read-only: nothing here changes the review.
 */

const TONE_CLASS: Record<AccuracyTone, string> = {
  green: 'border-outcome-pass-fg/40 bg-outcome-pass-bg text-outcome-pass-fg',
  orange: 'border-outcome-review-fg/50 bg-outcome-review-bg text-outcome-review-fg',
  yellow: 'border-yellow-500/60 bg-yellow-50 text-yellow-800 dark:bg-yellow-950/40 dark:text-yellow-300',
  none: 'border-border bg-muted text-muted-foreground',
};

export function AccuracyBadge({ accuracy }: { accuracy: ChatAccuracy }) {
  const tone = accuracyTone(accuracy.score);
  const rows = accuracyRows(accuracy);
  const factor = coverageFactorWords(accuracy.coverage_factor);
  return (
    <Popover>
      <PopoverTrigger
        data-slot="chat-accuracy"
        data-tone={tone}
        aria-label={`${accuracyWords(accuracy)}. How it was measured`}
        className={cn(
          'inline-flex h-7 w-fit items-center gap-1.5 rounded-full border px-2.5 text-xs font-semibold tabular-nums transition-[filter] hover:brightness-95 focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none dark:hover:brightness-110',
          TONE_CLASS[tone],
        )}
      >
        <Gauge className="size-3.5" aria-hidden="true" />
        {accuracyWords(accuracy)}
      </PopoverTrigger>
      <PopoverContent align="start" className="w-[min(30rem,calc(100vw-24px))] gap-3 rounded-xl p-3 font-sans">
        <p className="text-sm font-semibold tabular-nums">
          {accuracyWords(accuracy)}
          {accuracy.claims != null && <span className="font-normal text-muted-foreground"> · {accuracy.claims} {accuracy.claims === 1 ? 'claim' : 'claims'} checked</span>}
        </p>
        {rows.length > 0 && (
          <div className="max-h-64 overflow-auto rounded-lg border">
            <table data-slot="chat-accuracy-table" className="w-full border-collapse text-xs">
              <thead className="bg-muted/50 text-muted-foreground">
                <tr>
                  <th scope="col" className="px-2.5 py-1.5 text-left font-medium">Claim</th>
                  <th scope="col" className="px-2.5 py-1.5 text-left font-medium">How it was checked</th>
                  <th scope="col" className="px-2.5 py-1.5 text-right font-medium">Points</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, at) => (
                  <tr key={at} className="border-t align-top">
                    <td className="px-2.5 py-1.5 break-words">{row.claim}</td>
                    <td className="px-2.5 py-1.5 break-words text-muted-foreground">{row.how}</td>
                    <td className="num px-2.5 py-1.5 text-right">{pointsWords(row.points)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {factor && <p className="text-xs text-muted-foreground tabular-nums">{factor}</p>}
        <p className="text-xs text-muted-foreground">{ACCURACY_MEANING}</p>
        {TEST_SET_LINE && <p data-slot="chat-accuracy-test-set" className="text-xs font-medium tabular-nums">{TEST_SET_LINE}</p>}
      </PopoverContent>
    </Popover>
  );
}

const PRINTED_MARK = { match: '✓', not_found: '✗', no_text: '?', not_a_number: '?', no_box: '–', not_run: '–' } as const;

/** The stated numbers' evidence chips, "p.23 ▣ 25 1/2"", each opening its crop. */
export function CiteChips({ cites, pictureUrl }: { cites: readonly ChatCite[]; pictureUrl: (pictureId: string) => string }) {
  const [open, setOpen] = useState<string | null>(null);
  const shown = cites.find((cite) => cite.id === open) ?? null;
  if (!cites.length) return null;
  return (
    <div data-slot="chat-cites" className="flex min-w-0 flex-wrap items-center gap-1.5">
      <span className="text-xs text-muted-foreground">Evidence</span>
      {cites.map((cite) => (
        <button
          key={cite.id}
          type="button"
          data-slot="chat-cite"
          data-printed={cite.printed ?? undefined}
          onClick={() => setOpen(cite.id)}
          aria-label={`Page ${cite.page}${cite.value ? `: ${cite.value}` : ''}. Open the evidence`}
          className="inline-flex h-7 items-center gap-1 rounded-md border bg-card px-2 text-xs transition-colors hover:border-foreground/30 hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none motion-reduce:transition-none"
        >
          <span className="num">p.{cite.page}</span>
          <ScanLine className="size-3.5 text-muted-foreground" aria-hidden="true" />
          {cite.value && <span className="num font-medium">{cite.value}</span>}
          {cite.printed && <span aria-hidden="true" className={cn('font-semibold', cite.printed === 'match' ? 'text-outcome-pass-fg' : cite.printed === 'not_found' ? 'text-outcome-fail-fg' : 'text-muted-foreground')}>{PRINTED_MARK[cite.printed] ?? ''}</span>}
        </button>
      ))}
      <Dialog open={shown !== null} onOpenChange={(next) => { if (!next) setOpen(null); }}>
        {shown && (
          <DialogContent data-slot="chat-cite-viewer" className="max-h-[94vh] w-[min(96vw,760px)] max-w-none gap-3 overflow-y-auto p-4 sm:max-w-none sm:p-5">
            <DialogTitle className="pr-8 text-base font-semibold tabular-nums">
              Page {shown.page}{shown.value ? ` · ${shown.value}` : ''}
            </DialogTitle>
            <DialogDescription>
              {shown.printed === 'match' ? 'Printed at this spot: checked by code.'
                : shown.printed === 'not_found' ? 'Not found printed at this spot.'
                : shown.printed === 'no_text' ? 'Drawn as pen strokes: cannot be checked against the text.'
                : 'The spot on the drawing this number was read from.'}
            </DialogDescription>
            {shown.picture_id ? <ZoomPane key={shown.picture_id} src={pictureUrl(shown.picture_id)} label={`Page ${shown.page}`} /> : <NoPicture />}
          </DialogContent>
        )}
      </Dialog>
    </div>
  );
}
