import { Check, ChevronDown, FileStack, Loader2, PenLine, X } from 'lucide-react';

import type { ActionExtra, ItemAction, ReviewItem } from '@/api/review-items';
import {
  ACTION_LABELS,
  GUESS_SOURCE_WORDS,
  agreeAction,
  detailId,
  disagreeAction,
  fixChoices,
  readerWord,
} from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { PagePicture, PictureUnavailable, ZoomPicture } from './item-pictures';
import type { SaveError } from './failures-strip';
import { WallChoices } from './wall-choices';
import { Kbd, NumText } from './words';

/** The pictures side by side: the close-up large, the vendor page and the architect page beside it. */
function Evidence({ item }: { item: ReviewItem }) {
  const architect = item.architect_ref?.location ?? null;
  const hasZoom = Boolean(item.zoom_ref);
  const page = item.vendor_ref?.page_number ?? item.page;
  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <figure className="flex min-w-0 flex-col gap-2">
        <figcaption className="text-[13px] text-muted-foreground">{hasZoom ? 'Close-up' : 'Vendor page'}</figcaption>
        <div className="h-72">
          {item.zoom_ref ? (
            <ZoomPicture path={item.zoom_ref} label={`Close-up of the number on page ${page}`} />
          ) : item.vendor_ref ? (
            <PagePicture pageRef={item.vendor_ref} label={`Vendor page ${page}, the spot ringed`} />
          ) : (
            <PictureUnavailable>No spot is stored for this item. Open the PDFs to look at the drawing.</PictureUnavailable>
          )}
        </div>
      </figure>
      <div className="grid min-w-0 gap-4 sm:grid-cols-2 md:grid-cols-1">
        {hasZoom && (
          <figure className="flex min-w-0 flex-col gap-2">
            <figcaption className="text-[13px] text-muted-foreground">
              Vendor page <span className="num">{page}</span>
            </figcaption>
            <div className="h-40">
              {item.vendor_ref ? (
                <PagePicture pageRef={item.vendor_ref} label={`Vendor page ${page}, the number ringed`} />
              ) : (
                <PictureUnavailable>No spot is stored on this page.</PictureUnavailable>
              )}
            </div>
          </figure>
        )}
        <figure className="flex min-w-0 flex-col gap-2">
          <figcaption className="text-[13px] text-muted-foreground">
            {architect ? (
              <>
                Architect page <span className="num">{architect.page_number}</span>
              </>
            ) : (
              'Architect page'
            )}
          </figcaption>
          <div className={hasZoom ? 'h-40' : 'h-72'}>
            {architect ? (
              <PagePicture pageRef={architect} label={`Architect page ${architect.page_number}, the matching spot ringed`} />
            ) : (
              <div className="flex h-full min-h-24 items-center rounded-lg border border-dashed border-border px-4 py-3 text-[13px] leading-5 text-muted-foreground">
                {item.architect_ref?.reason ?? 'No architect page is linked to this item.'}
              </div>
            )}
          </div>
        </figure>
      </div>
    </div>
  );
}

/** The AI's best guess, set large in the number face, with where it came from and what each AI read. */
function Guess({ item }: { item: ReviewItem }) {
  if (!item.best_guess) {
    return (
      <p className="text-[15px] leading-6 text-foreground">
        {item.kind === 'no_countertop_page' || item.kind === 'wall_choice' ? item.what : 'The AI has no number to offer here. Look at the drawing and say whether it is right.'}
      </p>
    );
  }
  return (
    <div className="flex flex-wrap items-end gap-x-8 gap-y-3">
      <div className="flex flex-col gap-1">
        <span className="text-[13px] text-muted-foreground">AI's best guess</span>
        <span data-testid="best-guess" className="num text-[28px] leading-9 font-semibold tracking-[-0.01em] text-foreground">
          {item.best_guess.text}
        </span>
        <span className="text-[13px] text-muted-foreground">{GUESS_SOURCE_WORDS[item.best_guess.source]}</span>
      </div>
      {(item.readers ?? []).length > 0 && (
        <dl className="flex gap-6 pb-6 text-[13px]">
          {(item.readers ?? []).map((reading) => (
            <div key={reading.reader} className="flex flex-col gap-0.5">
              <dt className="text-muted-foreground">{readerWord(reading.reader)}</dt>
              <dd className="num text-[15px] text-foreground">{reading.text}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

const AGREE_ICON = <Check aria-hidden="true" />;

/**
 * An opened item: the drawings, the AI's guess and one-click answers. Nothing here asks the reviewer
 * to type: Fix offers the AIs' other readings to pick from.
 */
export function ItemDetail({
  item,
  busy,
  error,
  fixOpen,
  onFixOpenChange,
  onAct,
  onOpenPdfs,
}: {
  item: ReviewItem;
  /** The action being saved now, or null. */
  busy: ItemAction | null;
  error: SaveError | null;
  /** The Fix menu, opened by the F key as well as by a click. Uncontrolled when left out. */
  fixOpen?: boolean;
  onFixOpenChange?: (open: boolean) => void;
  onAct: (action: ItemAction, extra?: ActionExtra) => void;
  onOpenPdfs: (item: ReviewItem) => void;
}) {
  const walls = item.kind === 'wall_choice';
  // A wall choice is answered by clicking a layout; it has no separate agree or disagree button.
  const agree = walls ? null : agreeAction(item);
  const disagree = disagreeAction(item);
  const choices = fixChoices(item);
  const saving = busy !== null;

  return (
    <div id={detailId(item.key)} data-slot="item-detail" className="flex flex-col gap-6 border-t border-border bg-muted/30 px-4 pt-5 pb-6 sm:px-6 sm:pl-[3.25rem]">
      <Evidence item={item} />
      {walls ? <WallChoices item={item} busy={saving} onChoose={(layout) => onAct('choose_wall', { wall_config: layout })} /> : <Guess item={item} />}
      <NumText text={item.why} className="block max-w-[68ch] text-[13px] leading-5 text-muted-foreground" />

      {error && (
        <p role="alert" className="flex flex-wrap items-center gap-2 rounded-lg border border-outcome-fail-fg/40 bg-outcome-fail-bg px-3 py-2 text-[13px] text-outcome-fail-fg">
          <X aria-hidden="true" className="size-4" />
          Not saved: {error.message}
          <Button size="xs" variant="outline" onClick={error.retry}>
            Retry
          </Button>
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        {agree && (
          <Button onClick={() => onAct(agree)} disabled={saving}>
            {busy === agree ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : AGREE_ICON}
            {ACTION_LABELS[agree]}
            <Kbd>C</Kbd>
          </Button>
        )}
        {disagree === 'fix' ? (
          <DropdownMenu open={fixOpen} onOpenChange={onFixOpenChange}>
            <DropdownMenuTrigger asChild>
              <Button variant="outline" disabled={saving}>
                {busy === 'fix' ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <PenLine aria-hidden="true" />}
                Fix
                <Kbd>F</Kbd>
                <ChevronDown aria-hidden="true" className="opacity-60" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-72">
              <DropdownMenuLabel className="text-[13px] font-normal text-muted-foreground">
                {choices.length > 0 ? 'Use the other reading' : 'No other reading to pick'}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              {choices.map((choice) => (
                <DropdownMenuItem key={choice.text} onSelect={() => onAct('fix', { value: choice.text })} className="flex items-center justify-between gap-3">
                  <span className="num text-[15px] font-medium">{choice.text}</span>
                  <span className="text-[13px] text-muted-foreground">read by {choice.readers.join(' and ')}</span>
                </DropdownMenuItem>
              ))}
              {choices.length === 0 && (
                <DropdownMenuItem onSelect={() => onOpenPdfs(item)}>
                  <FileStack aria-hidden="true" />
                  Every AI read this number. Open the PDFs to check it.
                </DropdownMenuItem>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          disagree && (
            <Button variant="outline" onClick={() => onAct(disagree)} disabled={saving}>
              {busy === disagree ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <X aria-hidden="true" />}
              {ACTION_LABELS[disagree]}
              <Kbd>F</Kbd>
            </Button>
          )
        )}
        <Button variant="ghost" onClick={() => onOpenPdfs(item)}>
          <FileStack aria-hidden="true" />
          Open PDFs
          <Kbd>O</Kbd>
        </Button>
        {item.status === 'rechecking' && (
          <span role="status" className="text-[13px] text-muted-foreground">
            Saved. The check is running again with your answer.
          </span>
        )}
      </div>
    </div>
  );
}
