import { useEffect, useRef, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, CircleSlash, ExternalLink, ImageOff, Minus, Plus, RotateCcw, X } from 'lucide-react';

import { StatusMark, type DecisionProps } from '@/components/chat/chat-findings';
import { RoundChip } from '@/components/chat/chat-trust';
import { Sheet, SheetClose, SheetContent, SheetDescription, SheetTitle } from '@/components/ui/sheet';
import type { ChatFinding } from '@/lib/chat-api';
import { architectPage, decisionHelpRows, pageWords, reviewerWords, sideLabels, vendorPage } from '@/lib/chat-findings';
import { proofGroup } from '@/lib/chat-proof';
import { boxOnPicture, itemParts, shouldBeValue, vendorValue } from '@/lib/chat-result';
import { plainChecks, sumWords, TRUST_WORDS, trustLevelOf } from '@/lib/chat-trust';
import { useReducedMotion } from '@/lib/chat-motion';
import { cn } from '@/lib/utils';

/**
 * Chat v2 (#1483): a finding's evidence, side by side and calm. The architect's crop and the
 * vendor's crop, large, each zoomable (buttons, wheel, double-click) and draggable when zoomed,
 * labelled with their page numbers; the finding's item, values and reason above. ← and → step
 * through the shown findings, Esc closes. A missing picture is a quiet "No evidence picture".
 * The crops are made by code from the PDF, not described by the AI.
 */

const MIN_ZOOM = 1;
const MAX_ZOOM = 6;
const ZOOM_STEP = 1.5;

const clampZoom = (value: number) => Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, value));

/** One crop, large, with zoom and pan. */
export function ZoomPane({ src, label, frameClassName }: { src: string; label: string; frameClassName?: string }) {
  const reduced = useReducedMotion();
  const frame = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; y: number; startX: number; startY: number } | null>(null);
  const [view, setView] = useState({ zoom: 1, x: 0, y: 0 });
  const [broken, setBroken] = useState(false);

  const zoomTo = (zoom: number) => setView((current) => {
    const next = clampZoom(zoom);
    // Back at full view the picture re-centres; otherwise the pan scales with the zoom.
    return next === 1 ? { zoom: 1, x: 0, y: 0 } : { zoom: next, x: (current.x * next) / current.zoom, y: (current.y * next) / current.zoom };
  });

  // The wheel zooms the picture rather than scrolling the dialog, so the listener cannot be passive.
  useEffect(() => {
    const node = frame.current;
    if (!node) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      setView((current) => {
        const next = clampZoom(current.zoom * (event.deltaY < 0 ? 1.15 : 1 / 1.15));
        return next === 1 ? { zoom: 1, x: 0, y: 0 } : { zoom: next, x: (current.x * next) / current.zoom, y: (current.y * next) / current.zoom };
      });
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => node.removeEventListener('wheel', onWheel);
  }, []);

  if (broken) return <NoPicture />;

  return (
    <div className="grid min-h-0 gap-1.5">
      <div
        ref={frame}
        data-slot="chat-evidence-frame"
        data-zoom={view.zoom.toFixed(2)}
        onDoubleClick={() => zoomTo(view.zoom > 1 ? 1 : 2.5)}
        onPointerDown={(event) => {
          if (view.zoom <= 1) return;
          drag.current = { x: event.clientX, y: event.clientY, startX: view.x, startY: view.y };
          event.currentTarget.setPointerCapture?.(event.pointerId);
        }}
        onPointerMove={(event) => {
          const start = drag.current;
          if (!start) return;
          setView((current) => ({ ...current, x: start.startX + event.clientX - start.x, y: start.startY + event.clientY - start.y }));
        }}
        onPointerUp={() => { drag.current = null; }}
        onPointerCancel={() => { drag.current = null; }}
        className={cn(
          'relative grid place-items-center overflow-hidden rounded-lg border bg-white touch-none select-none',
          frameClassName ?? 'h-[min(56vh,520px)] max-md:h-[34vh]',
          view.zoom > 1 ? 'cursor-grab active:cursor-grabbing' : 'cursor-zoom-in',
        )}
      >
        <img
          src={src}
          alt={label}
          draggable={false}
          onError={() => setBroken(true)}
          style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.zoom})` }}
          className={cn('max-h-full max-w-full object-contain', !reduced && 'transition-transform duration-150 ease-out')}
        />
      </div>
      <div className="flex items-center justify-end gap-0.5" role="group" aria-label={`Zoom: ${label}`}>
        <ZoomButton label={`Zoom out: ${label}`} onClick={() => zoomTo(view.zoom / ZOOM_STEP)} disabled={view.zoom <= MIN_ZOOM}><Minus className="size-3.5" aria-hidden="true" /></ZoomButton>
        <span className="num w-11 text-center text-xs text-muted-foreground" aria-live="polite">{Math.round(view.zoom * 100)}%</span>
        <ZoomButton label={`Zoom in: ${label}`} onClick={() => zoomTo(view.zoom * ZOOM_STEP)} disabled={view.zoom >= MAX_ZOOM}><Plus className="size-3.5" aria-hidden="true" /></ZoomButton>
        <ZoomButton label={`Fit: ${label}`} onClick={() => zoomTo(1)} disabled={view.zoom === 1}><RotateCcw className="size-3.5" aria-hidden="true" /></ZoomButton>
      </div>
    </div>
  );
}

function ZoomButton({ label, onClick, disabled, children }: { label: string; onClick: () => void; disabled: boolean; children: React.ReactNode }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label.split(':')[0]}
      onClick={onClick}
      disabled={disabled}
      className="grid size-7 place-items-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40 motion-reduce:transition-none"
    >
      {children}
    </button>
  );
}

export function NoPicture({ compact = false }: { compact?: boolean }) {
  return (
    <div
      data-slot="chat-evidence-missing"
      className={cn('grid place-items-center rounded-lg border border-dashed text-sm text-muted-foreground', compact ? 'h-[190px] max-md:h-[140px]' : 'h-[min(56vh,520px)] max-md:h-[34vh]')}
    >
      <span className="inline-flex items-center gap-2">
        <ImageOff aria-hidden="true" className="size-4" />
        No evidence picture
      </span>
    </div>
  );
}

/** The viewer's words: what each side shows, why, and what to tell the vendor (the card's words). */
function ViewerWords({ finding }: { finding: ChatFinding }) {
  const words = reviewerWords(finding);
  const help = decisionHelpRows(finding);
  return (
    <>
      <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.referenceLabel}</dt><dd className="break-words">{words.reference}</dd></div>
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.vendorLabel}</dt><dd className="break-words">{words.vendor}</dd></div>
      </dl>
      <SheetDescription className="break-words text-foreground">
        <span className="text-muted-foreground">{words.whyLabel}: </span>{words.why}
      </SheetDescription>
      {help.length > 0 && (
        <dl data-slot="chat-evidence-decision-help" className="grid gap-1 text-sm">
          {help.map((row) => (
            <div key={row.label} className="min-w-0"><dt className="inline text-muted-foreground">{row.label}: </dt><dd className="inline break-words">{row.text}</dd></div>
          ))}
        </dl>
      )}
      {words.tell && <p data-slot="chat-evidence-tell" className="text-sm font-medium break-words">{words.tell}</p>}
      <p className="text-xs text-muted-foreground">How sure: {trustLevelOf(finding) ? `${TRUST_WORDS[trustLevelOf(finding)!]}. ` : ''}{words.sure}</p>
    </>
  );
}

export interface EvidenceViewerProps {
  /** The findings the arrows step through, in the result's order. */
  findings: readonly ChatFinding[];
  /** The finding shown, by id; null when the viewer is closed. */
  openId: string | null;
  onOpen: (findingId: string | null) => void;
  pictureUrl: (pictureId: string) => string;
  /** Where "Open in review screen" goes; the link is left out when there is none. */
  fullPageHref?: string | null;
  /** #1717: the vendor's page picture (100 dpi, the set pack's), to show the spot boxed; absent when there is none. */
  pagePicture?: ((page: number) => string) | null;
  /** #1717: the cloud drawing at a page ("Open on the cloud drawing"); absent when there is none. */
  cloudAt?: ((page: number) => string) | null;
  /** #1717 (owner decision 3): the optional override lives here, not on the rows. */
  decisionFor?: ((finding: ChatFinding) => DecisionProps) | null;
  /** #1717: the comment number the letter gives a Wrong item (C-04). */
  numberOf?: ((finding: ChatFinding) => string | null) | null;
  /** #1717: why the override was not saved, said in the panel (the page behind it is hidden while it is open). */
  notice?: string | null;
}

/**
 * #1717: the evidence panel, a sheet from the right (full screen on a phone): the two values large,
 * both crops (zoomable), the spot boxed on the vendor's page where its picture exists, why (in
 * plain words), the comment for the vendor, and the optional override. J / K or ← / → step through
 * the result's items, Esc closes and focus goes back to the row.
 */
export function EvidenceViewer({ findings, openId, onOpen, pictureUrl, fullPageHref, pagePicture = null, cloudAt = null, decisionFor = null, numberOf = null, notice = null }: EvidenceViewerProps) {
  const at = openId === null ? -1 : findings.findIndex((finding) => finding.id === openId);
  const finding = at >= 0 ? findings[at] : null;
  const previous = at > 0 ? findings[at - 1] : null;
  const next = at >= 0 && at < findings.length - 1 ? findings[at + 1] : null;
  const decision = finding && decisionFor ? decisionFor(finding) : null;
  const group = finding ? proofGroup(finding, decision ? (decision.decision ? { decision: decision.decision } : null) : finding.decision ?? null) : null;
  const number = finding && numberOf ? numberOf(finding) : null;
  const parts = finding ? itemParts(finding.item) : null;
  const vendor = finding ? vendorValue(finding) : null;
  const should = finding && group === 'clouded' ? shouldBeValue(finding) : null;
  // J / K and the arrows step while the panel is open, wherever focus is (a button that becomes
  // disabled at the end of the list drops focus out of the panel).
  const previousId = previous?.id ?? null;
  const nextId = next?.id ?? null;
  const isOpen = finding !== null;
  useEffect(() => {
    if (!isOpen) return;
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target?.closest?.('input, textarea, [contenteditable="true"]')) return;
      if ((event.key === 'ArrowLeft' || event.key === 'k') && previousId) { event.preventDefault(); onOpen(previousId); }
      if ((event.key === 'ArrowRight' || event.key === 'j') && nextId) { event.preventDefault(); onOpen(nextId); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [isOpen, previousId, nextId, onOpen]);

  return (
    <Sheet open={finding !== null} onOpenChange={(open) => { if (!open) onOpen(null); }}>
      {finding && parts && vendor && (
        <SheetContent
          side="right"
          showCloseButton={false}
          data-slot="chat-evidence-viewer"
          // The panel opens outside the chat page (a portal): it takes the page's own face and accent.
          data-tw
          className="chat-sheet chat-brand font-sans text-sm text-foreground antialiased w-full gap-0 p-0 data-[state=closed]:duration-200 data-[state=open]:duration-300 sm:max-w-[600px]"
        >
          <div className="flex min-w-0 items-center gap-2 border-b px-5 py-3 max-md:px-4">
            {number && <span data-slot="chat-evidence-number" className="num inline-flex h-[22px] items-center rounded-full border border-outcome-fail-fg/25 bg-outcome-fail-bg px-2 text-xs font-semibold text-outcome-fail-fg">{number}</span>}
            <StatusMark finding={finding} />
            <RoundChip finding={finding} />
            <span className="flex-1" />
            <span data-slot="chat-evidence-position" className="num px-1 text-xs text-muted-foreground">{at + 1} of {findings.length}</span>
            <button type="button" aria-label="Previous finding" disabled={!previous} onClick={() => previous && onOpen(previous.id)} className={ICON_BUTTON}><ChevronLeft className="size-4" aria-hidden="true" /></button>
            <button type="button" aria-label="Next finding" disabled={!next} onClick={() => next && onOpen(next.id)} className={ICON_BUTTON}><ChevronRight className="size-4" aria-hidden="true" /></button>
            <SheetClose aria-label="Close" className={ICON_BUTTON}><X className="size-4" aria-hidden="true" /></SheetClose>
          </div>

          <div className="grid min-h-0 flex-1 content-start gap-5 overflow-y-auto px-5 py-5 max-md:px-4">
            <div className="grid min-w-0 gap-1">
              <p aria-hidden="true" className="flex flex-wrap gap-x-2 text-[12.5px] text-muted-foreground">{parts.area && <span>{parts.area}</span>}<span className="num">p.{pageWords(finding)}</span></p>
              <SheetTitle className="text-[19px] leading-[26px] font-semibold tracking-[-0.01em] break-words">
                {parts.area ? parts.thing : finding.item}
                <span className="sr-only"> · page {pageWords(finding)}</span>
              </SheetTitle>
            </div>

            <div className="grid min-w-0 grid-cols-2 gap-3 max-md:gap-2">
              <ValueCard side="vendor" finding={finding} label={sideLabels(finding).vendor} value={vendor.main} tone={group === 'clouded' ? 'wrong' : null} note={vendor.note ? `Printed ${finding.vendor}` : null} src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} />
              <ValueCard side="architect" finding={finding} label={should ? 'Should be' : sideLabels(finding).architect} value={should ? should.main : finding.architect ? finding.architect.replace(/"/g, '″').replace(/'/g, '′') : '—'} tone={should ? 'right' : null} note={should?.note ? `From ${should.note}` : null} src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} />
            </div>

            {pagePicture && (finding.vendor_box || finding.arch_box) && <PageSpot finding={finding} src={pagePicture((finding.vendor_box ?? finding.arch_box)!.page)} />}

            <section aria-label="Why" className="grid min-w-0 gap-2">
              <h3 className="text-xs font-semibold text-muted-foreground">{group === 'clouded' ? 'Why it is Wrong' : group === 'right' ? 'Why it is Right' : 'Why it was not compared'}</h3>
              <ViewerWords finding={finding} />
              {sumWords(finding) && (
                <p data-slot="chat-evidence-sum" className="text-sm tabular-nums">
                  <span className="text-muted-foreground">Code’s sum: </span>
                  {sumWords(finding)}
                </p>
              )}
              {plainChecks(finding).length > 0 && (
                <ul data-slot="chat-evidence-checks" className="grid gap-1.5 text-[13.5px]">
                  {plainChecks(finding).map((line) => <li key={line} className="flex gap-2"><Check className="mt-0.5 size-4 shrink-0" aria-hidden="true" />{line}</li>)}
                </ul>
              )}
            </section>
          </div>

          <div className="flex min-w-0 flex-wrap items-center gap-2 border-t px-5 py-3 max-md:px-4">
            {notice && <p role="alert" data-slot="chat-evidence-notice" className="w-full text-[13px] text-muted-foreground">{notice}</p>}
            <span className="text-xs text-muted-foreground max-md:hidden"><kbd className="num rounded border px-1">J</kbd> <kbd className="num rounded border px-1">K</kbd> to step · <kbd className="num rounded border px-1">Esc</kbd></span>
            <span className="flex-1" />
            {decision && (group === 'clouded' || decision.decision === 'not_a_problem') && (
              <button type="button" data-slot="chat-evidence-not-a-problem" aria-pressed={decision.decision === 'not_a_problem'} disabled={decision.busy} onClick={() => decision.onDecide('not_a_problem')} className={SMALL_BUTTON}>
                <CircleSlash className="size-3.5" aria-hidden="true" />
                {decision.decision === 'not_a_problem' ? 'Marked not a problem' : 'Not a problem'}
              </button>
            )}
            {decision && group === 'not_compared' && (
              <button type="button" data-slot="chat-evidence-make-wrong" disabled={decision.busy} onClick={() => decision.onDecide('agree')} className={SMALL_BUTTON}>
                Make it Wrong
              </button>
            )}
            {cloudAt && group === 'clouded' && (
              <a href={cloudAt(finding.vendor_box?.page ?? finding.page)} target="_blank" rel="noopener noreferrer" className={SMALL_BUTTON}><ExternalLink className="size-3.5" aria-hidden="true" />Open on the cloud drawing</a>
            )}
            {fullPageHref && (
              <a href={fullPageHref} className={SMALL_BUTTON}><ExternalLink className="size-3.5" aria-hidden="true" />Open in review screen</a>
            )}
          </div>
        </SheetContent>
      )}
    </Sheet>
  );
}

const ICON_BUTTON = 'grid size-8 shrink-0 place-items-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40';
const SMALL_BUTTON = 'inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-[13px] font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-60 aria-pressed:bg-muted';

/** One side: its value large, then its crop (zoomable). */
function ValueCard({ side, finding, label, value, tone, note, src }: { side: 'vendor' | 'architect'; finding: ChatFinding; label: string; value: string; tone: 'wrong' | 'right' | null; note: string | null; src: string | null }) {
  const page = side === 'vendor' ? vendorPage(finding) : architectPage(finding);
  return (
    <figure data-slot="chat-evidence-side" data-side={side} className="grid min-w-0 content-start overflow-hidden rounded-[10px] border">
      <figcaption className="flex min-w-0 items-baseline justify-between gap-2 border-b px-3 py-2">
        <span className="text-[12.5px] text-muted-foreground">{label}</span>
        <span className={cn('num truncate text-[20px] leading-[26px] font-semibold max-md:text-[17px]', tone === 'wrong' && 'text-outcome-fail-fg', tone === 'right' && 'text-outcome-pass-fg')}>{value}</span>
      </figcaption>
      <div className="p-2">{src ? <ZoomPane key={src} src={src} label={`${sideLabels(finding)[side]}, page ${page}`} frameClassName="h-[190px] max-md:h-[140px]" /> : <NoPicture compact />}</div>
      <p className="border-t px-3 py-1.5 text-xs text-muted-foreground">{note ?? <>Page <span className="num">{page}</span></>}</p>
    </figure>
  );
}

/** The vendor's page with the value boxed: zoomed to the spot, or the whole page. */
function PageSpot({ finding, src }: { finding: ChatFinding; src: string }) {
  const [mode, setMode] = useState<'vendor' | 'architect' | 'page'>(finding.vendor_box ? 'vendor' : 'architect');
  const [size, setSize] = useState<{ width: number; height: number } | null>(null);
  const [broken, setBroken] = useState(false);
  const frame = useRef<HTMLDivElement>(null);
  const reduced = useReducedMotion();
  const [view, setView] = useState(556);
  // The frame's width, measured after it is on screen (never read from the ref while drawing).
  useEffect(() => {
    const node = frame.current;
    if (!node) return;
    const measure = () => setView(node.clientWidth || 556);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(node);
    return () => observer?.disconnect();
  }, [broken]);
  const sameFile = finding.vendor_box && finding.arch_box && finding.vendor_box.file === finding.arch_box.file && finding.vendor_box.page === finding.arch_box.page;
  const boxes = { vendor: boxOnPicture(finding.vendor_box), architect: sameFile || !finding.vendor_box ? boxOnPicture(finding.arch_box) : null };
  if (broken) return null;
  const height = 280;
  const scale = mode === 'page' && size ? view / size.width : 1;
  const focus = mode === 'page' ? null : boxes[mode] ?? boxes.vendor ?? boxes.architect;
  const x = focus ? view / 2 - (focus.left + focus.width / 2) * scale : 0;
  const y = focus ? height / 2 - (focus.top + focus.height / 2) * scale : 0;
  const motion = reduced ? undefined : 'transform 320ms cubic-bezier(0.2,0,0,1), width 320ms cubic-bezier(0.2,0,0,1)';
  return (
    <section aria-label="On the drawing" data-slot="chat-evidence-page" className="grid min-w-0 gap-2">
      <div className="flex min-w-0 items-center justify-between gap-2">
        <h3 className="text-xs font-semibold text-muted-foreground">On the drawing · <span className="num">p.{(finding.vendor_box ?? finding.arch_box)?.page}</span></h3>
        <div role="group" aria-label="Show" className="inline-flex rounded-lg bg-muted p-0.5">
          {(['vendor', 'architect', 'page'] as const).filter((each) => each === 'page' || boxes[each]).map((each) => (
            <button key={each} type="button" aria-pressed={mode === each} onClick={() => setMode(each)} className={cn('h-7 rounded-md px-2.5 text-xs font-medium', mode === each ? 'bg-card text-foreground shadow-[0_1px_2px_rgb(0_0_0/0.08)]' : 'text-muted-foreground hover:text-foreground')}>
              {each === 'vendor' ? 'Vendor spot' : each === 'architect' ? 'Architect spot' : 'Whole page'}
            </button>
          ))}
        </div>
      </div>
      <div ref={frame} className="relative h-[280px] overflow-hidden rounded-[10px] border bg-white">
        <div style={{ position: 'absolute', left: 0, top: 0, transform: `translate(${x}px, ${y}px) scale(${scale})`, transformOrigin: '0 0', transition: motion }}>
          <img src={src} alt={`Vendor page ${(finding.vendor_box ?? finding.arch_box)?.page}`} onError={() => setBroken(true)} onLoad={(event) => setSize({ width: event.currentTarget.naturalWidth, height: event.currentTarget.naturalHeight })} className="block max-w-none" />
          {(['vendor', 'architect'] as const).map((each) => boxes[each] && (
            <span key={each} aria-hidden="true" className={cn('absolute rounded-[3px] border-2 shadow-[0_0_0_3px_rgb(255_255_255/0.9)]', each === 'vendor' ? 'border-outcome-fail' : 'border-outcome-pass')} style={{ left: boxes[each]!.left - 4, top: boxes[each]!.top - 4, width: boxes[each]!.width + 8, height: boxes[each]!.height + 8 }} />
          ))}
        </div>
      </div>
    </section>
  );
}
