import { Check, FileStack, Loader2, PenLine, X } from 'lucide-react';

import type { ActionExtra, ItemAction, ReviewItem } from '@/api/review-items';
import {
  ACTION_LABELS,
  GUESS_SOURCE_WORDS,
  agreeAction,
  detailId,
  disagreeAction,
  readerWord,
  splitReasons,
  tidyWhy,
} from '@/lib/review-items';
import { Button } from '@/components/ui/button';
import { ArchitectChoices } from './architect-choices';
import { CabinetKinds } from './cabinet-kinds';
import { CabinetRun } from './cabinet-run';
import { FixDialog } from './fix-dialog';
import { PagePicture, PictureUnavailable, ZoomPicture } from './item-pictures';
import type { SaveError } from './failures-strip';
import { WallChoices } from './wall-choices';
import { NumberToCheckBlock, WhatToCheckBlock } from './what-to-check';
import { Kbd, NumText } from './words';

/** The pictures side by side: the close-up large, the vendor page and the architect page beside it. */
function Evidence({ item }: { item: ReviewItem }) {
  const architect = item.architect_ref?.location ?? null;
  // A countertop with no close-up of its own: the row's strip, cut from the page, stands in for it
  // (never the first piece's span, which may show only a cabinet tag).
  const rowStrip = !item.zoom_ref && item.kind === 'countertop_by_hand' && item.vendor_ref ? item.vendor_ref : null;
  const hasZoom = Boolean(item.zoom_ref) || rowStrip !== null;
  const page = item.vendor_ref?.page_number ?? item.page;
  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
      <figure className="flex min-w-0 flex-col gap-2">
        <figcaption className="text-[13px] text-muted-foreground">{rowStrip ? 'The row' : hasZoom ? 'Close-up' : 'Vendor page'}</figcaption>
        <div className="h-72">
          {item.zoom_ref ? (
            <ZoomPicture path={item.zoom_ref} label={`Close-up of the number on page ${page}`} />
          ) : rowStrip ? (
            <PagePicture pageRef={rowStrip} focus label={`The row on page ${page}, its dimension line ringed`} />
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
                <PagePicture pageRef={item.vendor_ref} label={`Vendor page ${page}, the ${rowStrip ? 'row' : 'number'} ringed`} />
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
                <ArchitectNote reason={item.architect_ref?.reason ?? null} />
              </div>
            )}
          </div>
        </figure>
      </div>
    </div>
  );
}

/**
 * Why there is no architect page. A note joining several reasons with semicolons (some of them
 * contradicting each other, Set 3 QA) shows its first reason; the others wait behind Why.
 */
function ArchitectNote({ reason }: { reason: string | null }) {
  if (!reason) return <>No architect page is linked to this item.</>;
  const { first, others } = splitReasons(reason);
  if (others.length === 0) return <>{reason}</>;
  return (
    <span data-testid="architect-note" className="flex flex-col gap-1">
      <span>{first}</span>
      <details>
        <summary className="cursor-pointer text-foreground">Why</summary>
        <ul className="mt-1 flex flex-col gap-0.5">
          {others.map((other) => (
            <li key={other}>{other}</li>
          ))}
        </ul>
      </details>
    </span>
  );
}

/** The AI's best guess, set large in the number face, with where it came from and what each AI read. */
function Guess({ item }: { item: ReviewItem }) {
  if (!item.best_guess) {
    if (item.kind === 'no_countertop_page' || item.kind === 'wall_choice') {
      return <p className="text-[15px] leading-6 text-foreground">{item.what}</p>;
    }
    // The full name, never cut off once opened (#1394), then what to do.
    return (
      <div className="flex flex-col gap-1">
        <p data-slot="item-name" className="text-[15px] leading-6 font-medium break-words text-foreground">{item.what}</p>
        <p className="text-[15px] leading-6 text-foreground">The AI has no number to offer here. Look at the drawing and say whether it is right.</p>
      </div>
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
 * An opened item: the drawings, the AI's guess and its answers. A wall or architect question is
 * answered by clicking one of its choice cards; a number by Confirm, or Fix, which asks for the
 * value the drawing shows, in inches, recorded as a correction of the AI's reading.
 */
export function ItemDetail({
  item,
  busy,
  sending = null,
  error,
  fixOpen,
  onFixOpenChange,
  onAct,
  onOpenPdfs,
  onOpenAdvanced,
  onProposal,
  refresh = 0,
}: {
  item: ReviewItem;
  /** The action being saved now, or null. */
  busy: ItemAction | null;
  /** What that save sends (the wall or the architect answer), so only the chosen card spins. */
  sending?: ActionExtra | null;
  /** Bumped by the page to read an architect question again (after a stale answer). */
  refresh?: number;
  error: SaveError | null;
  /** The Fix dialog, opened by the F key as well as by a click. */
  fixOpen?: boolean;
  onFixOpenChange?: (open: boolean) => void;
  onAct: (action: ItemAction, extra?: ActionExtra) => void;
  onOpenPdfs: (item: ReviewItem) => void;
  onOpenAdvanced?: () => void;
  /** The proposal the C key sends on an architect question, once its choices load. */
  onProposal?: (extra: ActionExtra | null) => void;
}) {
  const walls = item.kind === 'wall_choice';
  const architect = item.kind === 'architect';
  const cabinets = item.kind === 'cabinet_kinds';
  const cabinetRun = item.kind === 'cabinet_run';
  // A wall, architect or cabinet question is answered by its own choices; no agree or disagree button.
  const cards = walls || architect || cabinets || cabinetRun;
  const agree = cards ? null : agreeAction(item);
  const disagree = cards ? null : disagreeAction(item);
  const saving = busy !== null;
  const numberBlock = (item.kind === 'number' || item.kind === 'depth_reading') && Boolean(item.best_guess) && !item.what_to_check;

  return (
    <div id={detailId(item.key)} data-slot="item-detail" className="flex flex-col gap-6 border-t border-border bg-muted/30 px-4 pt-5 pb-6 sm:px-6 sm:pl-[3.25rem]">
      {/* A cabinet run is entered on Measurements: it has no spot on a page to picture. */}
      {!cabinets && <Evidence item={item} />}
      {cabinetRun ? (
        <CabinetRun key={`${item.key}:${item.status}`} item={item} busy={busy !== null} onUse={(answer) => onAct('use_cabinet_run', { cabinet_run: answer })} />
      ) : cabinets ? (
        <CabinetKinds key={`${item.key}:${item.status}`} item={item} busy={busy !== null} onSave={(kinds) => onAct('mark_cabinets', { cabinet_kinds: kinds })} />
      ) : walls ? (
        <WallChoices item={item} busy={busy !== null} saving={sending?.wall_config ?? null} onChoose={(layout) => onAct('choose_wall', { wall_config: layout })} />
      ) : architect ? (
        <ArchitectChoices item={item} busy={busy !== null} saving={sending} refresh={refresh} onAnswer={(extra) => onAct('answer', extra)} onProposal={onProposal} onOpenAdvanced={onOpenAdvanced} />
      ) : (
        <Guess item={item} />
      )}
      {item.what_to_check && <WhatToCheckBlock block={item.what_to_check} />}
      {/* An AI number says what to check too, its stored reason inside the block (Set 3 QA). */}
      {numberBlock && <NumberToCheckBlock item={item} agreeLabel={agree ? ACTION_LABELS[agree] : null} disagreeLabel={disagree ? ACTION_LABELS[disagree] : null} />}
      {/* A row held as a whole says its reason once, in the block above; its why is the same reason. */}
      {!item.what_to_check?.row_reason && !numberBlock && (
        <NumText text={tidyWhy(item.why)} className="block max-w-[68ch] text-[13px] leading-5 text-muted-foreground" />
      )}

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
          <Button variant="outline" onClick={() => onFixOpenChange?.(true)} disabled={saving} aria-haspopup="dialog">
            {busy === 'fix' ? <Loader2 aria-hidden="true" className="animate-spin motion-reduce:animate-none" /> : <PenLine aria-hidden="true" />}
            Fix
            <Kbd>F</Kbd>
          </Button>
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
        {disagree === 'fix' && (
          <FixDialog
            key={item.key}
            item={item}
            open={fixOpen ?? false}
            onOpenChange={(next) => onFixOpenChange?.(next)}
            busy={busy === 'fix'}
            error={fixOpen ? error : null}
            onSave={(value) => onAct('fix', { value })}
          />
        )}
        {item.status === 'rechecking' && (
          <span role="status" className="text-[13px] text-muted-foreground">
            Saved. The check is running again with your answer.
          </span>
        )}
      </div>
    </div>
  );
}
