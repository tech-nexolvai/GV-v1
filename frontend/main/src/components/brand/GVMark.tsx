/** Shared Graniti Vicentia logo used everywhere the product displays its brand mark. */

interface GVMarkProps {
  /** Rendered square size in pixels. */
  size?: number;
  /** Retained for callers that place the mark on a branded surface. */
  variant?: 'tile' | 'bare';
  /** Adds the existing activity pulse while a request is running. */
  animated?: boolean;
  className?: string;
}

export function GVMark({
  size = 28,
  variant = 'tile',
  animated = false,
  className = '',
}: GVMarkProps) {
  const classes = ['gv-mark', animated ? 'gv-mark--animated' : '', className]
    .filter(Boolean)
    .join(' ');

  return (
    <img
      src="/logo-graniti.svg"
      width={size}
      height={size}
      className={classes}
      data-variant={variant}
      alt="Graniti Vicentia"
      decoding="async"
    />
  );
}

/** Company logo plus the product name used by full-width navigation. */
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
