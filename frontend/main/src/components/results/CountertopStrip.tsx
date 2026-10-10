import { useEffect, useId, useRef, useState } from 'react';
import { CircleDashed } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { CountertopResult } from '@/api/client';
import { stripLayout, type StripLayout, type StripPiece } from '@/lib/countertop-strip';
import { OutcomeIcon } from '@/components/ui/OutcomeIcon';

/**
 * The countertop picture (#1043), the signature view of a countertop result: pieces to scale, the
 * printed overall above, the needed total below, field-cut caps and walls at the ends, and the
 * difference beside it. Drawn from the shared spec the signed PDF uses too, so screen and paper
 * show the same picture. Every number is the API's exact text (`lib/countertop-strip.ts`).
 *
 * `compact` is the inline bar for table rows and the queue (about 120 px tall); `full` is for the
 * countertop card, where every piece can be focused and carries a tooltip.
 */
export function CountertopStrip({
  row,
  size = 'compact',
  showHoldReason = true,
  className,
}: {
  row: CountertopResult;
  size?: 'compact' | 'full';
  /** Off where the surrounding card already states the hold reason (the chip then says "Held"). */
  showHoldReason?: boolean;
  className?: string;
}) {
  const [box, width] = useElementWidth(size === 'full' ? 720 : 560);
  const drawWidth = Math.max(width - CALLOUT_RESERVE - 2 * WALL, 120);
  const layout = stripLayout(row, drawWidth);

  if (layout.status === 'refused') {
    return (
      <div ref={box} data-slot="countertop-strip" data-state="refused" className={cn('font-sans text-xs text-muted-foreground', className)}>
        <span className="inline-flex items-center gap-1 rounded-full border border-dashed px-2 py-0.5">
          <CircleDashed className="size-3" aria-hidden="true" /> No picture: {layout.reason.toLowerCase()}
        </span>
      </div>
    );
  }

  return (
    <div
      ref={box}
      data-slot="countertop-strip"
      data-state={stateOf(layout)}
      data-to-scale={layout.toScale}
      className={cn('flex min-w-0 flex-col gap-1.5 font-sans', className)}
    >
      <div className="flex items-center gap-3">
        <StripSvg layout={layout} size={size} />
        <Difference layout={layout} />
      </div>
      {(layout.held || !layout.toScale) && (
        <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
          {layout.held && (
            <span title={layout.held.reason} className="inline-flex max-w-full items-center gap-1 rounded-full border border-dashed px-2 py-0.5">
              <CircleDashed className="size-3 shrink-0" aria-hidden="true" />
              <span className="truncate">{size === 'full' && showHoldReason ? layout.held.reason : 'Held'}</span>
            </span>
          )}
          {!layout.toScale && <span className="rounded-full border px-2 py-0.5">Not to scale</span>}
        </div>
      )}
    </div>
  );
}

function stateOf(layout: Extract<StripLayout, { status: 'drawn' }>): string {
  if (layout.held) return 'held';
  if (layout.difference === null) return 'unchecked';
  if (layout.difference.sign === 0) return 'equal';
  return layout.difference.sign < 0 ? 'short' : 'over';
}

/* ── Drawing ────────────────────────────────────────────────────────────────── */

const WALL = 12; // wall block width, outside the run
const CALLOUT_RESERVE = 84; // room kept for the difference callout beside the drawing

function StripSvg({ layout, size }: { layout: Extract<StripLayout, { status: 'drawn' }>; size: 'compact' | 'full' }) {
  const id = `strip${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const full = size === 'full';
  // Vertical rhythm (px): printed bracket · back wall · run · piece labels · needed bracket.
  const y = full
    ? { printedLabel: 14, printed: 24, back: 34, top: 40, bottom: 92, pieceLabel: 108, needed: 128, neededLabel: 144, height: 150 }
    : { printedLabel: 12, printed: 20, back: 28, top: 33, bottom: 69, pieceLabel: 84, needed: 96, neededLabel: 111, height: 118 };
  const x0 = WALL; // the left wall face
  const end = x0 + layout.width;
  const runEnd = x0 + Math.max(...layout.pieces.map((p) => p.x + p.w), layout.caps.right ? layout.caps.right.x + layout.caps.right.w : 0);
  // Nothing under 12px (#1155), in either size.
  const labelSize = 12;
  const fits = (piece: StripPiece) => piece.w >= (piece.label ?? '?').length * labelSize * 0.62 + 6;

  return (
    <svg
      role="img"
      aria-label={layout.summary}
      width={end + WALL}
      height={y.height}
      viewBox={`0 0 ${end + WALL} ${y.height}`}
      className="max-w-full shrink-0 overflow-visible text-foreground"
    >
      <title>{layout.summary}</title>
      <defs>
        <pattern id={`${id}-filler`} width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="6" className="stroke-muted-foreground/45" strokeWidth="1.5" />
        </pattern>
        <pattern id={`${id}-wall`} width="5" height="5" patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">
          <line x1="0" y1="0" x2="0" y2="5" className="stroke-foreground" strokeWidth="1.6" />
        </pattern>
        <pattern id={`${id}-held`} width="10" height="10" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="10" className="stroke-foreground/35" strokeWidth="2" />
        </pattern>
      </defs>

      {/* Printed overall, from the left wall face. */}
      {layout.printed && <Bracket x={x0 + layout.printed.x} w={layout.printed.w} y={y.printed} labelY={y.printedLabel} label={layout.printed.label} up role="printed" />}

      {/* Back wall: a thin line above the run. */}
      {layout.walls?.back && <line x1={x0} y1={y.back} x2={runEnd} y2={y.back} className="stroke-foreground" strokeWidth="2.5" strokeLinecap="round" data-wall="back" />}

      {/* End walls: hatched blocks just outside the run. */}
      {layout.walls?.left && <rect x={0} y={y.back} width={WALL - 2} height={y.bottom - y.back} fill={`url(#${id}-wall)`} className="stroke-foreground" strokeWidth="1" data-wall="left" />}
      {layout.walls?.right && <rect x={runEnd + 2} y={y.back} width={WALL - 2} height={y.bottom - y.back} fill={`url(#${id}-wall)`} className="stroke-foreground" strokeWidth="1" data-wall="right" />}

      {/* Field-cut caps, only at ends with a wall. */}
      {[layout.caps.left, layout.caps.right].map((cap, i) =>
        cap ? (
          <g key={i} data-cap={i === 0 ? 'left' : 'right'} data-label={cap.label}>
            <rect x={x0 + cap.x} y={y.top} width={cap.w} height={y.bottom - y.top} className="fill-background stroke-foreground" strokeWidth="1" strokeDasharray="2 2" />
            {/* In the label row, flush with the outer edge, so it never sits on the back-wall line. */}
            <text x={i === 0 ? x0 + cap.x - WALL : x0 + cap.x + cap.w + WALL} y={y.pieceLabel} textAnchor={i === 0 ? 'start' : 'end'} className="num fill-muted-foreground" fontSize={12}>{cap.label}</text>
          </g>
        ) : null,
      )}

      {/* Pieces. */}
      {layout.pieces.map((piece, i) => {
        const missing = piece.label === null;
        const fill = missing || piece.kind === 'appliance' ? 'none' : piece.kind === 'filler' ? `url(#${id}-filler)` : undefined;
        const showLabel = !missing && (full || fits(piece));
        // Full size: a label that does not fit drops to a second line instead of disappearing.
        const lowered = full && !missing && !fits(piece) && i % 2 === 1;
        return (
          <g key={piece.index} data-piece={piece.index} data-kind={piece.kind} data-source={piece.source} data-label={piece.label ?? '?'} tabIndex={full ? 0 : undefined} className="outline-none focus-visible:[&>rect]:stroke-ring">
            <title>{pieceTip(piece)}</title>
            <rect
              x={x0 + piece.x}
              y={y.top}
              width={Math.max(piece.w, 1)}
              height={y.bottom - y.top}
              fill={fill}
              className={cn(fill === undefined && 'fill-muted', 'stroke-foreground/70')}
              strokeWidth="1"
              strokeDasharray={missing || piece.kind === 'appliance' ? '4 3' : undefined}
            />
            {missing && (
              <text x={x0 + piece.x + piece.w / 2} y={(y.top + y.bottom) / 2 + 5} textAnchor="middle" className="num fill-muted-foreground" fontSize={14}>?</text>
            )}
            {showLabel && (
              <text x={x0 + piece.x + piece.w / 2} y={y.pieceLabel + (lowered ? 13 : 0)} textAnchor="middle" className="num fill-foreground" fontSize={labelSize}>{piece.label}</text>
            )}
          </g>
        );
      })}

      {/* Held: a hatch over the run, so it never reads as a checked result. */}
      {layout.held && <rect x={x0} y={y.top} width={runEnd - x0} height={y.bottom - y.top} fill={`url(#${id}-held)`} data-held="true" />}

      {/* Needed total (pieces + field cut), from the left wall face. */}
      {layout.needed && <Bracket x={x0 + layout.needed.x} w={layout.needed.w} y={y.needed} labelY={y.neededLabel} label={layout.needed.label} role="needed" />}
    </svg>
  );
}

function Bracket({ x, w, y, labelY, label, up = false, role }: { x: number; w: number; y: number; labelY: number; label: string; up?: boolean; role: 'printed' | 'needed' }) {
  const tick = up ? 5 : -5;
  return (
    <g data-bracket={role} data-label={label} data-x={x.toFixed(2)} data-w={w.toFixed(2)}>
      <path d={`M${x} ${y + tick} V${y} H${x + w} V${y + tick}`} fill="none" className="stroke-foreground/70" strokeWidth="1.2" />
      <text x={x + w / 2} y={labelY} textAnchor="middle" className="num fill-foreground" fontSize={12} fontWeight={500}>
        {label}
      </text>
    </g>
  );
}

function Difference({ layout }: { layout: Extract<StripLayout, { status: 'drawn' }> }) {
  if (layout.held || layout.difference === null) return <span className="w-[72px] shrink-0" aria-hidden="true" />;
  const ok = layout.difference.sign === 0;
  return (
    <span
      data-slot="strip-difference"
      className={cn(
        'num inline-flex w-[72px] shrink-0 items-center justify-center gap-1 rounded-md border px-1.5 py-1 text-sm font-medium',
        ok ? 'border-outcome-pass-fg/40 bg-outcome-pass-bg text-outcome-pass-fg' : 'border-outcome-fail-fg/50 bg-outcome-fail-bg text-outcome-fail-fg',
      )}
    >
      <OutcomeIcon outcome={ok ? 'PASS' : 'FAIL'} size={14} />
      {layout.difference.text}
    </span>
  );
}

const SOURCE_WORDS = { sealed: 'both AIs read it the same', typed: 'typed by a reviewer', missing: 'not read' } as const;
const KIND_WORDS = { filler: 'Filler', cabinet: 'Cabinet', appliance: 'Appliance space', other: 'Piece' } as const;

function pieceTip(piece: StripPiece): string {
  return `${KIND_WORDS[piece.kind]} ${piece.index + 1}: ${piece.label ?? 'width unknown'} — ${SOURCE_WORDS[piece.source]}`;
}

/** The element's width, followed as it resizes; a sensible default before the first measure. */
function useElementWidth(fallback: number): [React.RefCallback<HTMLDivElement>, number] {
  const [width, setWidth] = useState(fallback);
  const observer = useRef<ResizeObserver | null>(null);
  useEffect(() => () => observer.current?.disconnect(), []);
  const ref: React.RefCallback<HTMLDivElement> = (element) => {
    observer.current?.disconnect();
    if (!element || typeof ResizeObserver === 'undefined') return;
    observer.current = new ResizeObserver((entries) => {
      const next = Math.floor(entries[0]?.contentRect.width ?? 0);
      if (next > 0) setWidth((current) => (Math.abs(current - next) > 1 ? next : current));
    });
    observer.current.observe(element);
  };
  return [ref, width];
}
