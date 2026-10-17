import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, ExternalLink, ImageOff, Minus, Plus, RotateCcw } from 'lucide-react';

import { Confidence, StatusMark } from '@/components/chat/chat-findings';
import { RoundChip } from '@/components/chat/chat-trust';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import type { ChatFinding } from '@/lib/chat-api';
import { architectPage, pageWords, reviewerWords, sideLabels, vendorPage } from '@/lib/chat-findings';
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
export function ZoomPane({ src, label }: { src: string; label: string }) {
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
          'relative grid h-[min(56vh,520px)] place-items-center overflow-hidden rounded-lg border bg-white touch-none select-none max-md:h-[34vh]',
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

export function NoPicture() {
  return (
    <div
      data-slot="chat-evidence-missing"
      className="grid h-[min(56vh,520px)] place-items-center rounded-lg border border-dashed text-sm text-muted-foreground max-md:h-[34vh]"
    >
      <span className="inline-flex items-center gap-2">
        <ImageOff aria-hidden="true" className="size-4" />
        No evidence picture
      </span>
    </div>
  );
}

function Side({ side, page, src, finding }: { side: 'architect' | 'vendor'; page: number; src: string | null; finding: ChatFinding }) {
  const who = sideLabels(finding)[side];
  const label = `${who}, page ${page}`;
  return (
    <figure data-slot="chat-evidence-side" data-side={side} className="grid min-w-0 content-start gap-1.5">
      <figcaption className="flex items-baseline gap-1.5 text-xs font-medium text-muted-foreground">
        <span className="text-foreground">{who}</span>
        <span className="tabular-nums">page {page}</span>
      </figcaption>
      {src ? <ZoomPane key={src} src={src} label={label} /> : <NoPicture />}
    </figure>
  );
}

/** The viewer's words: what each side shows, why, and what to tell the vendor (the card's words). */
function ViewerWords({ finding }: { finding: ChatFinding }) {
  const words = reviewerWords(finding);
  return (
    <>
      <dl className="grid gap-x-6 gap-y-1.5 text-sm sm:grid-cols-2">
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.referenceLabel}</dt><dd className="break-words">{words.reference}</dd></div>
        <div className="min-w-0"><dt className="text-xs font-medium text-muted-foreground">{words.vendorLabel}</dt><dd className="break-words">{words.vendor}</dd></div>
      </dl>
      <DialogDescription className="break-words text-foreground">
        <span className="text-muted-foreground">{words.whyLabel}: </span>{words.why}
      </DialogDescription>
      {words.tell && <p data-slot="chat-evidence-tell" className="text-sm font-medium break-words">{words.tell}</p>}
      <p className="text-xs text-muted-foreground">How sure: {trustLevelOf(finding) ? `${TRUST_WORDS[trustLevelOf(finding)!]}. ` : ''}{words.sure}</p>
    </>
  );
}

export interface EvidenceViewerProps {
  /** The findings the arrows step through, in the table's order. */
  findings: readonly ChatFinding[];
  /** The finding shown, by id; null when the viewer is closed. */
  openId: string | null;
  onOpen: (findingId: string | null) => void;
  pictureUrl: (pictureId: string) => string;
  /** Where "Open in review screen" goes; the link is left out when there is none. */
  fullPageHref?: string | null;
}

export function EvidenceViewer({ findings, openId, onOpen, pictureUrl, fullPageHref }: EvidenceViewerProps) {
  const at = openId === null ? -1 : findings.findIndex((finding) => finding.id === openId);
  const finding = at >= 0 ? findings[at] : null;
  const previous = at > 0 ? findings[at - 1] : null;
  const next = at >= 0 && at < findings.length - 1 ? findings[at + 1] : null;

  return (
    <Dialog open={finding !== null} onOpenChange={(open) => { if (!open) onOpen(null); }}>
      {finding && (
        <DialogContent
          data-slot="chat-evidence-viewer"
          onKeyDown={(event) => {
            if (event.key === 'ArrowLeft' && previous) { event.preventDefault(); onOpen(previous.id); }
            if (event.key === 'ArrowRight' && next) { event.preventDefault(); onOpen(next.id); }
          }}
          className="max-h-[94vh] w-[min(96vw,1240px)] max-w-none gap-3 overflow-y-auto p-4 sm:max-w-none sm:p-5"
        >
          <div className="grid min-w-0 gap-2 pr-8">
            <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
              <DialogTitle className="text-base leading-snug font-semibold break-words">
                {finding.item}
                <span className="font-normal text-muted-foreground tabular-nums"> · page {pageWords(finding)}</span>
              </DialogTitle>
              <StatusMark finding={finding} />
              <Confidence finding={finding} />
              <RoundChip finding={finding} />
            </div>
            <ViewerWords finding={finding} />
            {sumWords(finding) && (
              <p data-slot="chat-evidence-sum" className="text-sm tabular-nums">
                <span className="text-muted-foreground">Code’s sum: </span>
                {sumWords(finding)}
              </p>
            )}
          </div>

          <div className="grid min-w-0 gap-3 md:grid-cols-2">
            <Side side="architect" page={architectPage(finding)} src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} finding={finding} />
            <Side side="vendor" page={vendorPage(finding)} src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} finding={finding} />
          </div>

          {plainChecks(finding).length > 0 && (
            <details data-slot="chat-evidence-checks" className="group/how rounded-lg border bg-muted/30 px-3 py-2 text-[13px]">
              <summary className="inline-flex cursor-pointer list-none items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground [&::-webkit-details-marker]:hidden">
                <ChevronRight aria-hidden="true" className="size-3.5 transition-transform duration-150 group-open/how:rotate-90 motion-reduce:transition-none" />
                How we know
              </summary>
              <ul className="mt-1.5 grid gap-1">
                {plainChecks(finding).map((line) => <li key={line}>{line}</li>)}
              </ul>
            </details>
          )}

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-1">
              <button
                type="button"
                aria-label="Previous finding"
                disabled={!previous}
                onClick={() => previous && onOpen(previous.id)}
                className="grid size-8 place-items-center rounded-lg border bg-background hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40"
              >
                <ChevronLeft className="size-4" aria-hidden="true" />
              </button>
              <span data-slot="chat-evidence-position" className="px-1.5 tabular-nums text-xs text-muted-foreground">{at + 1} of {findings.length}</span>
              <button
                type="button"
                aria-label="Next finding"
                disabled={!next}
                onClick={() => next && onOpen(next.id)}
                className="grid size-8 place-items-center rounded-lg border bg-background hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none disabled:opacity-40"
              >
                <ChevronRight className="size-4" aria-hidden="true" />
              </button>
              <span className="ml-2 hidden text-xs text-muted-foreground sm:inline">← → to step · Esc to close</span>
            </div>
            {fullPageHref && (
              <a
                href={fullPageHref}
                className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-2.5 text-sm font-medium hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
              >
                <ExternalLink className="size-3.5" aria-hidden="true" />
                Open in review screen
              </a>
            )}
          </div>
        </DialogContent>
      )}
    </Dialog>
  );
}
