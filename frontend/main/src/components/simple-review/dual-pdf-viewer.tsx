import { useEffect, useMemo, useState, type ReactNode } from 'react';
import { Link2, Link2Off, MoveHorizontal, RefreshCw, X } from 'lucide-react';

import type { ReviewItem } from '@/api/review-items';
import { cn } from '@/lib/utils';
import { outlineOf, type Point } from '@/lib/drawing-viewer';
import { Button } from '@/components/ui/button';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/skeleton';
import { liveReviewPagesApi, PictureStore, type ReviewDocument, type ReviewPagesApi, type ReviewPagesOut } from './dual-pdf-viewer-api';
import { browserDecoder, goTo, openNav, step, toggleLink, withCounts, type Decoder, type Nav, type Side } from './dual-pdf-viewer-lib';
import { DualPane, type PaneCommand, type PaneMark, type PaneSpot } from './dual-pdf-viewer-pane';

export type { ReviewPagesApi, ReviewPagesOut } from './dual-pdf-viewer-api';

/**
 * What the viewer opens at: a list item, or a wrong result (#1388), which has a vendor spot and no
 * architect one. Only the fields the viewer reads.
 */
export type ViewerItem = Pick<ReviewItem, 'key' | 'page' | 'what' | 'status'> & Partial<Pick<ReviewItem, 'vendor_ref' | 'architect_ref'>>;

/** How far one arrow key moves a page, in screen pixels. */
const PAN_STEP = 80;

type ListState = { status: 'loading' } | { status: 'error'; error: Error } | { status: 'ready'; data: ReviewPagesOut };

/**
 * "Open PDFs" (#1355): the vendor's and the architect's full drawings side by side, over the review.
 *
 * Pages are linked: ←/→ turn both, keeping the pair the reviewer opened at; unlink to turn one side
 * alone. Each side pans and zooms by itself and opens fitted to its width. Opened from an item, both
 * sides open at the item's spots, ringed. On a combined sheet (the architect's drawing pasted onto the
 * vendor's sheet), the architect side is the same page zoomed to the architect's drawing, and a drawing
 * only the sheet heading suggests is labelled as a suggestion.
 *
 * Pictures are the stored 300 dpi pages: each is fetched only while its page is on screen, decoded at
 * the size it is drawn (never above the stored size), and freed when the page leaves.
 */
export function DualPdfViewer({
  projectId,
  packageId,
  item,
  aside = null,
  onClose,
  api = liveReviewPagesApi,
  decoder = browserDecoder,
}: {
  projectId: string;
  packageId: string;
  /** The item to open at, or null for the whole set from page 1. */
  item: ViewerItem | null;
  /** Shown beside the drawings (right on a wide screen, above them on a narrow one), e.g. a wrong result's recorded numbers. */
  aside?: ReactNode;
  onClose: () => void;
  api?: ReviewPagesApi;
  decoder?: Decoder;
}) {
  const [list, setList] = useState<ListState>({ status: 'loading' });
  const [data, setData] = useState<ReviewPagesOut | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [nav, setNav] = useState<Nav | null>(null);
  /** The listing `nav` was last placed or counted from. */
  const [placedFor, setPlacedFor] = useState<ReviewPagesOut | null>(null);
  const [command, setCommand] = useState<{ side: Side | 'both'; order: PaneCommand } | null>(null);
  const store = useMemo(() => new PictureStore((page, dvid, signal) => api.loadPicture(projectId, packageId, page, dvid, signal)), [api, projectId, packageId]);

  useEffect(() => {
    let live = true;
    // Closing the viewer (or loading again) cancels a listing still on its way (#1362).
    const cancel = new AbortController();
    api.loadPages(projectId, packageId, cancel.signal).then(
      (out) => {
        if (!live) return;
        setData(out);
        setList({ status: 'ready', data: out });
      },
      (error: unknown) => live && setList({ status: 'error', error: error instanceof Error ? error : new Error(String(error)) }),
    );
    return () => {
      live = false;
      cancel.abort();
    };
  }, [api, projectId, packageId, attempt]);

  // Each side shows the file that holds the item's spot, else its first file (#1362).
  const documents = data?.documents ?? [];
  const combined = data?.combined_set ?? false;
  const vendor = pickFile(documents, 'vendor', item?.vendor_ref?.document_version_id);
  const architect = combined ? vendor : pickFile(documents, 'architect', item?.architect_ref?.location?.document_version_id);
  const notShown = notShownLine(documents, [vendor, architect]);

  // The first listing places both sides: at the item's spots, or page 1. A later one ("Check again")
  // only brings the page counts up to date, keeping the pages shown.
  if (data && data !== placedFor && (vendor || architect)) {
    const counts = { vendor: countOf(vendor), architect: countOf(architect) };
    setPlacedFor(data);
    setNav(nav === null ? openNav(startPages(item, architect, combined), counts, combined) : withCounts(nav, counts));
  }

  const order = (side: Side | 'both', kind: PaneCommand['kind'], dx = 0, dy = 0) =>
    setCommand((now) => ({ side, order: { id: (now?.order.id ?? 0) + 1, kind, dx, dy } }));

  function onKeyDown(event: React.KeyboardEvent) {
    if (!nav || event.metaKey || event.ctrlKey || event.altKey) return;
    const target = event.target as HTMLElement;
    if (['INPUT', 'TEXTAREA', 'SELECT'].includes(target.tagName)) return;
    const active = nav.active;
    const keys: Record<string, () => void> = {
      ArrowLeft: () => (event.shiftKey ? order(active, 'pan', PAN_STEP, 0) : setNav(step(nav, -1))),
      ArrowRight: () => (event.shiftKey ? order(active, 'pan', -PAN_STEP, 0) : setNav(step(nav, 1))),
      ArrowUp: () => order(active, 'pan', 0, PAN_STEP),
      ArrowDown: () => order(active, 'pan', 0, -PAN_STEP),
      '+': () => order(active, 'in'),
      '=': () => order(active, 'in'),
      '-': () => order(active, 'out'),
      w: () => order('both', 'fit'),
      W: () => order('both', 'fit'),
      l: () => setNav(toggleLink(nav)),
      L: () => setNav(toggleLink(nav)),
    };
    const run = keys[event.key];
    if (!run) return;
    event.preventDefault();
    event.stopPropagation();
    run();
  }

  const commandFor = (side: Side) => (command && (command.side === 'both' || command.side === side) ? command.order : null);
  const linked = nav?.linked ?? true;

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        showCloseButton={false}
        onKeyDown={onKeyDown}
        className="top-2 left-2 flex h-[calc(100dvh-1rem)] w-[calc(100vw-1rem)] max-w-none translate-x-0 translate-y-0 flex-col gap-0 overflow-hidden rounded-xl p-0 sm:max-w-none md:top-4 md:left-4 md:h-[calc(100dvh-2rem)] md:w-[calc(100vw-2rem)]"
      >
        <header className="flex h-14 shrink-0 items-center gap-3 border-b border-border px-4">
          <div className="min-w-0 flex-1">
            <DialogTitle className="text-[15px] leading-5 font-semibold">Both drawings</DialogTitle>
            <DialogDescription className="truncate text-[12px] leading-4 text-muted-foreground">
              {item ? <>Opened at {item.what}</> : 'The whole set'}
              {data && (
                <>
                  {', revision '}
                  <span className="num">{data.revision_number}</span>
                </>
              )}
              {notShown && `. ${notShown}`}
            </DialogDescription>
          </div>
          <p className="hidden text-[12px] text-muted-foreground lg:block">
            <Kbd>←</Kbd> <Kbd>→</Kbd> pages, <Kbd>+</Kbd> <Kbd>−</Kbd> zoom, <Kbd>W</Kbd> fit width{combined ? '' : <>, <Kbd>L</Kbd> link</>}, <Kbd>Esc</Kbd> close
          </p>
          <Button size="sm" variant="ghost" onClick={() => order('both', 'fit')} disabled={!nav}>
            <MoveHorizontal /> Fit width
          </Button>
          <Button size="icon-sm" variant="ghost" aria-label="Close" onClick={onClose}>
            <X />
          </Button>
        </header>

        {list.status === 'error' && !data && (
          <div role="alert" className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
            <p className="text-sm font-medium">The pages of this set could not be loaded</p>
            <p className="max-w-md text-[13px] text-muted-foreground">{list.error.message}</p>
            <Button size="sm" variant="outline" className="mt-1" onClick={() => setAttempt((n) => n + 1)}>
              <RefreshCw /> Retry
            </Button>
          </div>
        )}
        {list.status === 'loading' && !data && (
          <div className="grid flex-1 gap-4 p-4 md:grid-cols-2" role="status">
            <Skeleton className="h-full min-h-40" />
            <Skeleton className="h-full min-h-40" />
            <span className="sr-only">Loading the pages…</span>
          </div>
        )}
        {data && !vendor && !architect && (
          <div role="status" className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">
            This set has no drawings to show yet.
          </div>
        )}

        {data && nav && (vendor || architect) && (
          <div className="flex min-h-0 flex-1 flex-col md:flex-row-reverse">
            {aside}
            <div className="flex min-h-0 min-w-0 flex-1 flex-col md:flex-row">
              <DualPane
                side="vendor"
                doc={vendor}
                pageNumber={nav.pages.vendor}
                count={nav.counts.vendor}
                combined={false}
                spot={spotOn(item, 'vendor', vendor, nav.pages.vendor)}
                mark={markOf(item, 'vendor', vendor)}
                store={store}
                decoder={decoder}
                command={commandFor('vendor')}
                onStep={(delta) => setNav((now) => now && step({ ...now, active: 'vendor' }, delta))}
                onActive={() => setNav((now) => (now && now.active !== 'vendor' ? { ...now, active: 'vendor' } : now))}
                onRefresh={() => setAttempt((n) => n + 1)}
                onPickPage={(page) => setNav((now) => now && goTo(now, 'vendor', page))}
              />
  
              {/* The seam: where the two drawings meet, and whether their pages turn together. */}
              <div className={cn('relative flex h-11 shrink-0 items-center justify-center border-y border-border bg-muted/40 md:block md:h-auto md:w-px md:border-y-0 md:bg-border', !linked && 'md:border-l md:border-dashed md:bg-transparent')}>
                <div className="z-10 md:absolute md:top-3 md:left-1/2 md:-translate-x-1/2">
                  {combined ? (
                    <span className="flex h-7 items-center rounded-full border border-border bg-background px-2.5 text-[12px] whitespace-nowrap text-muted-foreground shadow-sm" title="The architect's drawing is on the vendor's sheet: both sides show the same page.">
                      Same sheet
                    </span>
                  ) : (
                    <Button
                      size="sm"
                      variant="outline"
                      aria-pressed={linked}
                      aria-label={linked ? 'Pages linked: they turn together. Press to unlink.' : 'Pages not linked: each side turns alone. Press to link.'}
                      title={linked ? 'Pages turn together (L)' : 'Each side turns alone (L)'}
                      onClick={() => setNav((now) => now && toggleLink(now))}
                      className={cn('h-7 rounded-full bg-background px-2.5 text-[12px] shadow-sm', !linked && 'border-dashed text-muted-foreground')}
                    >
                      {linked ? <Link2 /> : <Link2Off />}
                      {linked ? 'Linked' : 'Unlinked'}
                    </Button>
                  )}
                </div>
              </div>
  
              <DualPane
                side="architect"
                doc={architect}
                pageNumber={nav.pages.architect}
                count={nav.counts.architect}
                combined={combined}
                spot={spotOn(item, 'architect', architect, nav.pages.architect)}
                focus={combined ? focusOn(item, architect, nav.pages.architect) : null}
                mark={combined ? null : markOf(item, 'architect', architect)}
                store={store}
                decoder={decoder}
                command={commandFor('architect')}
                linkedNote={!combined && item && !item.architect_ref?.location ? (item.architect_ref?.reason ?? 'This item has no spot on the architect\'s drawing.') : null}
                onStep={(delta) => setNav((now) => now && step({ ...now, active: 'architect' }, delta))}
                onActive={() => setNav((now) => (now && now.active !== 'architect' ? { ...now, active: 'architect' } : now))}
                onRefresh={() => setAttempt((n) => n + 1)}
                onPickPage={(page) => setNav((now) => now && goTo(now, combined ? 'vendor' : 'architect', page))}
              />
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="num rounded border border-border bg-muted px-1 text-[12px] text-foreground">{children}</kbd>;
}

function countOf(doc: ReviewDocument | null): number {
  return doc ? Math.max(doc.page_count, doc.pages.length, 1) : 1;
}

/** The side's file holding the item's spot, else its first file. */
function pickFile(documents: ReviewDocument[], side: Side, documentVersionId: string | undefined): ReviewDocument | null {
  const files = documents.filter((doc) => doc.side === side);
  return files.find((doc) => doc.document_version_id === documentVersionId) ?? files[0] ?? null;
}

/** Never silent: every file of the set that neither side shows is counted, by kind. */
function notShownLine(documents: ReviewDocument[], shown: (ReviewDocument | null)[]): string | null {
  const hidden = documents.filter((doc) => !shown.includes(doc));
  const parts = (['vendor', 'architect', 'other'] as const)
    .map((side) => {
      const n = hidden.filter((doc) => doc.side === side).length;
      const kind = side === 'other' ? 'other' : `more ${side}`;
      return n > 0 ? `${n} ${kind} ${n === 1 ? 'file' : 'files'}` : null;
    })
    .filter((part): part is string => part !== null);
  if (parts.length === 0) return null;
  const list = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
  return `${list} ${hidden.length === 1 ? 'is' : 'are'} not shown here.`;
}

function startPages(item: ViewerItem | null, architect: ReviewDocument | null, combined: boolean) {
  const vendorPage = item?.vendor_ref?.page_number ?? item?.page ?? 1;
  const location = item?.architect_ref?.location ?? null;
  const architectPage = combined ? vendorPage : location && architect && location.document_version_id === architect.document_version_id ? location.page_number : 1;
  return { vendor: vendorPage, architect: architectPage };
}

/** The item's spot on this side's current page, from its stored outline only. */
function spotOn(item: ViewerItem | null, side: Side, doc: ReviewDocument | null, page: number): PaneSpot | null {
  if (!item || !doc) return null;
  const ref = side === 'vendor' ? (item.vendor_ref ?? null) : (item.architect_ref?.location ?? null);
  if (!ref || ref.document_version_id !== doc.document_version_id || ref.page_number !== page) return null;
  const polygon = outlineOf(ref.polygon);
  if (!polygon) return null;
  return { polygon, label: side === 'vendor' ? item.what : `${item.what}, architect`, emphasis: side === 'vendor' ? (item.status === 'done' ? 'checked' : 'item') : 'reference' };
}

/** The page of this side's file that holds the item, kept marked in the page strip while turning away. */
function markOf(item: ViewerItem | null, side: Side, doc: ReviewDocument | null): PaneMark | null {
  if (!item || !doc) return null;
  const ref = side === 'vendor' ? (item.vendor_ref ?? null) : (item.architect_ref?.location ?? null);
  if (!ref || ref.document_version_id !== doc.document_version_id) return null;
  return { page: ref.page_number, tone: side === 'vendor' && item.status !== 'done' ? 'item' : 'neutral' };
}

/** Combined sheet: where the item is on this page (its architect spot, else its vendor spot), to pick the architect drawing that holds it. */
function focusOn(item: ViewerItem | null, doc: ReviewDocument | null, page: number): Point[] | null {
  if (!item || !doc) return null;
  for (const ref of [item.architect_ref?.location ?? null, item.vendor_ref ?? null]) {
    if (ref && ref.document_version_id === doc.document_version_id && ref.page_number === page) {
      const polygon = outlineOf(ref.polygon);
      if (polygon) return polygon;
    }
  }
  return null;
}
