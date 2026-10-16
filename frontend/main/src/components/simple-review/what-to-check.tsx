import type { ReviewItem, WhatToCheck } from '@/api/review-items';
import { tidyWhy } from '@/lib/review-items';
import { NumText } from './words';

/**
 * What to check on a countertop checked by hand: each piece (what the AI read, or why it could not),
 * the overall the pieces must add up to, and the walls note when one was recorded. A row held as a
 * whole says its reason once at the top; its stored pieces show their inches tagged "not used". Every word and
 * number is the server's, from the stored records; nothing is added up or decided here.
 */
export function WhatToCheckBlock({ block }: { block: WhatToCheck }) {
  const pieces = block.pieces ?? [];
  return (
    <section data-testid="what-to-check" aria-labelledby="what-to-check-title" className="flex max-w-[68ch] flex-col gap-3">
      <h3 id="what-to-check-title" className="text-[13px] font-semibold text-foreground">
        What to check
      </h3>
      {block.row_reason && (
        <p data-testid="what-to-check-row-reason" className="text-[13px] leading-5 font-medium text-outcome-review-fg">
          {block.row_reason}
        </p>
      )}
      {pieces.length > 0 ? (
        <ul className="flex flex-col divide-y divide-border rounded-lg border border-border bg-background text-[13px]">
          {pieces.map((piece, index) => (
            <li key={`${piece.position ?? 'unplaced'}-${index}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2">
              <span className="w-16 shrink-0 font-medium text-foreground">
                {piece.position == null ? (
                  'No place'
                ) : (
                  <>
                    Piece <span className="num">{piece.position}</span>
                  </>
                )}
              </span>
              {piece.kind && <span className="text-muted-foreground">{piece.kind}</span>}
              {piece.value ? (
                <>
                  <span className="num font-medium text-foreground">{piece.value}</span>
                  {piece.mm && <span className="num text-muted-foreground">{piece.mm}</span>}
                  {piece.not_used && (
                    <span className="rounded border border-border px-1.5 text-xs leading-4 text-muted-foreground">not used</span>
                  )}
                </>
              ) : (
                <span className="text-outcome-review-fg">Couldn't read</span>
              )}
              {piece.note && <NumText text={piece.note} className="basis-full text-muted-foreground sm:basis-auto" />}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[13px] text-muted-foreground">No pieces were stored for this row.</p>
      )}
      <NumText text={block.rule} className="text-[13px] leading-5 font-medium text-foreground" />
      {block.overall_note && <NumText text={`Overall: ${block.overall_note}`} className="text-[13px] leading-5 text-muted-foreground" />}
      {block.walls_note && <NumText text={block.walls_note} className="text-[13px] leading-5 text-muted-foreground" />}
    </section>
  );
}

/**
 * What to check on one AI number (Set 3 QA), like the countertop block: the value to look for,
 * where it is, and why it was held, in the item's own stored words (W1 fields only). Nothing is
 * worked out here.
 */
export function NumberToCheckBlock({ item, agreeLabel, disagreeLabel }: { item: ReviewItem; agreeLabel: string | null; disagreeLabel: string | null }) {
  if (!item.best_guess) return null;
  const place = [item.row_label, item.page != null ? `page ${item.page}` : null].filter(Boolean).join(', ');
  return (
    <section data-testid="what-to-check" aria-labelledby="what-to-check-title" className="flex max-w-[68ch] flex-col gap-2 text-[13px] leading-5">
      <h3 id="what-to-check-title" className="font-semibold text-foreground">
        What to check
      </h3>
      <p className="text-foreground">
        Does the vendor&apos;s drawing show <span className="num font-medium">{item.best_guess.text}</span> for {item.what}
        {place ? <> ({place})</> : null}?
      </p>
      <p data-testid="what-to-check-why" className="text-outcome-review-fg">
        Why it was held: <NumText text={tidyWhy(item.why)} />
      </p>
      {(agreeLabel || disagreeLabel) && (
        <p className="text-muted-foreground">
          {agreeLabel && <>If it does, press {agreeLabel}. </>}
          {disagreeLabel && <>If not, press {disagreeLabel}{disagreeLabel === 'Fix' ? ' and type what it shows, in inches' : ''}.</>}
        </p>
      )}
    </section>
  );
}
