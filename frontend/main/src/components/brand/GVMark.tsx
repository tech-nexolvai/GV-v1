/**
 * The Graniti Vicentia mark.
 *
 * There was no logo. The topbar drew the letters "GV" in a square and the browser tab still carried
 * Vite's purple bolt, so the product was unbranded in the two places a viewer looks first.
 *
 * **Why a facet and not a monogram.** Graniti Vicentia cut stone; the mark is a chevron split into
 * two planes of different brightness, the way a polished edge catches light from one side. It reads
 * as a V at any size, which is the half of "GV" that survives being shrunk to 16px — a two-letter
 * monogram at favicon size is a smudge. The chamfered top edge is the slab.
 *
 * Drawn as geometry rather than shipped as a raster: it stays sharp on any display, inherits the
 * theme, and costs about 700 bytes instead of a set of PNGs.
 */

import { useId } from 'react';

interface GVMarkProps {
  /** Rendered size in px. The geometry is a 32-unit square, so it scales to anything. */
  size?: number;
  /**
   * `tile` is the mark on its maroon slab — navigation, the tab, anywhere it sits on a page.
   * `bare` is the chevron alone in `currentColor`, for placement on an already-branded surface
   * where a second filled square would be one box too many.
   */
  variant?: 'tile' | 'bare';
  /** Set while a request is in flight: the bright facet sweeps. Off by default. */
  animated?: boolean;
  className?: string;
}

export function GVMark({
  size = 28,
  variant = 'tile',
  animated = false,
  className = '',
}: GVMarkProps) {
  // Unique per instance so two marks on one page cannot capture each other's gradient.
  // `useId` rather than a counter or a random value: it is stable across re-renders, so the
  // gradient reference does not churn, and it matches between server and client markup.
  const uid = useId().replace(/:/g, '');

  if (variant === 'bare') {
    return (
      <svg
        width={size}
        height={size}
        viewBox="0 0 32 32"
        fill="none"
        className={className}
        role="img"
        aria-label="Graniti Vicentia"
      >
        <path d="M16 6.5 L26 23.5 L21.2 23.5 L16 14.6 L10.8 23.5 L6 23.5 Z" fill="currentColor" />
      </svg>
    );
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      className={className}
      role="img"
      aria-label="Graniti Vicentia"
    >
      <defs>
        <linearGradient id={`${uid}-slab`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0%" stopColor="var(--maroon-600)" />
          <stop offset="100%" stopColor="var(--maroon-800)" />
        </linearGradient>
      </defs>

      <rect width="32" height="32" rx="8" fill={`url(#${uid}-slab)`} />

      {/* The chamfer: a single lit edge along the top-left, at low alpha so it reads as a highlight
          on the material rather than as a second shape. */}
      <path d="M0 8 A8 8 0 0 1 8 0 L18 0 L0 18 Z" fill="#ffffff" opacity="0.10" />

      {/* Right plane — the lit face. */}
      <path d="M16 6.5 L26 23.5 L21.2 23.5 L16 14.6 Z" fill="#ffffff" />

      {/* Left plane — the same chevron in shadow. The 0.62 is what makes it a faceted solid rather
          than a flat letter. */}
      <path d="M16 6.5 L6 23.5 L10.8 23.5 L16 14.6 Z" fill="#ffffff" opacity="0.62" />

      {animated && (
        <path d="M16 6.5 L26 23.5 L21.2 23.5 L16 14.6 Z" fill="#ffffff" opacity="0.9">
          <animate
            attributeName="opacity"
            values="0.9;0.35;0.9"
            dur="1.4s"
            repeatCount="indefinite"
          />
        </path>
      )}
    </svg>
  );
}

/**
 * The full lockup: mark plus wordmark.
 *
 * The product line sits under the company name at a smaller size and wider tracking, so the
 * hierarchy is legible at a glance — this is Graniti Vicentia's platform, built with Nexolv, and
 * the two are not competing for the same line.
 */
export function GVLockup({ compact = false }: { compact?: boolean }) {
  return (
    <div className="gv-lockup">
      <GVMark size={compact ? 26 : 30} />
      {!compact && (
        <span className="gv-lockup__text">
          <span className="gv-lockup__name">Graniti Vicentia</span>
          <span className="gv-lockup__product">Review Platform</span>
        </span>
      )}
    </div>
  );
}
