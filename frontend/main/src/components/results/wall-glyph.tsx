import { cn } from '@/lib/utils';
import { wallsOf, WALL_SOURCE_TITLE, WALL_SOURCE_WORD } from '@/lib/countertop-results';
import type { CountertopResult } from '@/api/client';

/**
 * The wall layout as a tiny plan view (#1039): the back wall across the top, the end walls down
 * the sides, the stone as the bar between them. Walls that are there are solid; walls that are not
 * are faint dashes; an unknown layout is a "?" — never a guessed drawing.
 */
export function WallGlyph({ layout, className, compactSource = false }: { layout: CountertopResult['wall_layout']; className?: string; /** Hide the source word below 1536px (it stays in the tooltip). */ compactSource?: boolean }) {
  const walls = wallsOf(layout.config);
  const label = `${walls === null ? 'Walls not established' : `Walls: ${layout.label ?? layout.config}`} — ${WALL_SOURCE_TITLE[layout.source]}`;
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)} data-slot="wall-glyph">
      {walls === null ? (
        <span aria-label={label} title={label} role="img" className="num inline-flex size-6 items-center justify-center rounded border border-dashed text-xs text-muted-foreground">?</span>
      ) : (
        <svg viewBox="0 0 28 18" className="h-[18px] w-7 shrink-0" role="img" aria-label={label}>
          <title>{label}</title>
          <line x1="3" y1="2" x2="25" y2="2" className={wallClass(walls.back)} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="2" y1="2" x2="2" y2="16" className={wallClass(walls.left)} strokeWidth="2.5" strokeLinecap="round" />
          <line x1="26" y1="2" x2="26" y2="16" className={wallClass(walls.right)} strokeWidth="2.5" strokeLinecap="round" />
          <rect x="6" y="6" width="16" height="6" rx="1" className="fill-muted-foreground/40" />
        </svg>
      )}
      <span title={WALL_SOURCE_TITLE[layout.source]} className={cn('rounded-full border px-1.5 text-[11px] leading-4 text-muted-foreground', compactSource && 'hidden 2xl:inline')}>
        {WALL_SOURCE_WORD[layout.source]}
        <span className="sr-only"> ({WALL_SOURCE_TITLE[layout.source]})</span>
      </span>
    </span>
  );
}

function wallClass(present: boolean): string {
  return present ? 'stroke-foreground' : 'stroke-muted-foreground/40 [stroke-dasharray:2_2]';
}
