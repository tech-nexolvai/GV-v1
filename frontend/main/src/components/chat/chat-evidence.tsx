import { useEffect, useRef, useState } from 'react';
import { ChevronLeft, ChevronRight, ExternalLink, ImageOff, Minus, Plus, RotateCcw } from 'lucide-react';

import { FindingDifference, FindingValue, SourceTag, StatusChip } from '@/components/chat/chat-findings';
import { CheckList, RoundChip, TrustChip } from '@/components/chat/chat-trust';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import type { ChatFinding } from '@/lib/chat-api';
import { architectPage, sideLabels, vendorPage } from '@/lib/chat-findings';
import { printedSideWords, sumWords } from '@/lib/chat-trust';
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
  const printed = printedSideWords(finding, side);
  return (
    <figure data-slot="chat-evidence-side" data-side={side} className="grid min-w-0 content-start gap-1.5">
      <figcaption className="flex items-baseline gap-1.5 text-xs font-medium text-muted-foreground">
        <span className="text-foreground">{who}</span>
        <span className="num">page {page}</span>
      </figcaption>
      {src ? <ZoomPane key={src} src={src} label={label} /> : <NoPicture />}
      {printed && (
        <p data-slot="chat-evidence-printed" data-state={printed.state} className="flex min-w-0 items-start gap-1.5 text-[13px]">
          <span aria-hidden="true" className={cn('num w-3 shrink-0 text-center font-semibold', printed.state === 'pass' ? 'text-foreground' : printed.state === 'fail' ? 'text-outcome-fail-fg' : 'text-outcome-review-fg')}>
            {printed.state === 'pass' ? '✓' : printed.state === 'fail' ? '✗' : '?'}
          </span>
          <span className="min-w-0 break-words tabular-nums">{printed.words}</span>
        </p>
      )}
    </figure>
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
          <div className="grid min-w-0 gap-1.5 pr-8">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <DialogTitle className="text-base leading-snug font-semibold break-words">{finding.item}</DialogTitle>
              <StatusChip status={finding.status} />
              <TrustChip finding={finding} />
              <RoundChip finding={finding} />
            </div>
            <dl className="flex flex-wrap items-baseline gap-x-5 gap-y-1 text-sm">
              <div className="flex items-baseline gap-1.5">
                <dt className="text-muted-foreground">{sideLabels(finding).architect}</dt>
                <dd className="font-medium"><FindingValue finding={finding} side="architect" /></dd>
                <dd><SourceTag finding={finding} /></dd>
              </div>
              <div className="flex items-baseline gap-1.5">
                <dt className="text-muted-foreground">{sideLabels(finding).vendor}</dt>
                <dd className="font-medium"><FindingValue finding={finding} side="vendor" /></dd>
              </div>
              {finding.inches && (
                <div className="flex items-baseline gap-1.5">
                  <dt className="text-muted-foreground">Difference</dt>
                  <dd className="font-medium"><FindingDifference finding={finding} /></dd>
                </div>
              )}
            </dl>
            {sumWords(finding) && (
              <p data-slot="chat-evidence-sum" className="text-sm tabular-nums">
                <span className="text-muted-foreground">Code’s sum: </span>
                <span className="num">{sumWords(finding)}</span>
              </p>
            )}
            {finding.why?.trim()
              ? <DialogDescription className="break-words">{finding.why}</DialogDescription>
              : <DialogDescription className="sr-only">The architect's and the vendor's drawings at this spot, side by side.</DialogDescription>}
          </div>

          <div className="grid min-w-0 gap-3 md:grid-cols-2">
            <Side side="architect" page={architectPage(finding)} src={finding.arch_picture_id ? pictureUrl(finding.arch_picture_id) : null} finding={finding} />
            <Side side="vendor" page={vendorPage(finding)} src={finding.vendor_picture_id ? pictureUrl(finding.vendor_picture_id) : null} finding={finding} />
          </div>

          <div data-slot="chat-evidence-checks" className="grid gap-1 rounded-lg border bg-muted/30 px-3 py-2">
            <p className="text-xs font-medium text-muted-foreground">Checks</p>
            <CheckList finding={finding} only={printedSideWords(finding, 'architect') || printedSideWords(finding, 'vendor') ? ['second_read', 'verified', 'rule'] : undefined} />
          </div>

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
              <span data-slot="chat-evidence-position" className="num px-1.5 text-xs text-muted-foreground">{at + 1} of {findings.length}</span>
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
