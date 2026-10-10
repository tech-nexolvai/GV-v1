import { cn } from '@/lib/utils';
import { wallsOf, WALL_SOURCE_TITLE, WALL_SOURCE_WORD } from '@/lib/countertop-results';
import type { CountertopResult } from '@/api/client';

/**
 * The wall layout as a tiny plan view (#1039): the back wall across the top, the end walls down
 * the sides, the stone as the bar between them. Walls that are there are solid; walls that are not
 * are faint dashes. A layout that is not established is said in words ("Walls not set", #1126),
 * never a guessed drawing and never a lone "?".
 */
export function WallGlyph({
  layout,
  className,
  compactSource = false,
  labelled = false,
}: {
  layout: CountertopResult['wall_layout'];
  className?: string;
  /** Hide the source word below 1536px (it stays in the tooltip). */
  compactSource?: boolean;
  /** A "Walls" term is already beside it, so an unknown layout says only "Not set". */
  labelled?: boolean;
}) {
  const walls = wallsOf(layout.config);
  if (walls === null) {
    return (
      <span data-slot="wall-glyph" data-walls="not-set" title={WALL_SOURCE_TITLE[layout.source]} className={cn('inline-flex items-center gap-1 text-xs text-muted-foreground', className)}>
        <span className="inline-block h-3 w-4 shrink-0 rounded-[2px] border border-dashed border-muted-foreground/60" aria-hidden="true" />
        {unknownWords(layout, labelled)}
      </span>
    );
  }
  return (
    <span className={cn('inline-flex items-center gap-1.5', className)} data-slot="wall-glyph">
      <WallLines walls={walls} label={labelOf(layout)} />
      <span title={WALL_SOURCE_TITLE[layout.source]} className={cn('rounded-full border px-1.5 text-xs leading-4 text-muted-foreground', compactSource && 'hidden 2xl:inline')}>
        {WALL_SOURCE_WORD[layout.source]}
        <span className="sr-only"> ({WALL_SOURCE_TITLE[layout.source]})</span>
      </span>
    </span>
  );
}

/**
 * "Walls not set" when nobody has set them. A layout the rulebook publishes no picture for is named by
 * the API's human label, or "Walls set (no picture)": never a raw code such as `l_shape`.
 */
function unknownWords(layout: CountertopResult['wall_layout'], labelled: boolean): string {
  if (layout.source === 'not established' || (!layout.label && !layout.config)) return labelled ? 'Not set' : 'Walls not set';
  if (layout.label) return labelled ? `${layout.label} (no picture)` : `Walls: ${layout.label} (no picture)`;
  return labelled ? 'Set (no picture)' : 'Walls set (no picture)';
}

/**
 * Just the picture of a wall layout, for a choice the reviewer has not made yet (#1050): no source
 * and no label of its own, so a button's own words are all a screen reader hears.
 */
export function WallLayoutPicture({ config, className }: { config: string; className?: string }) {
  const walls = wallsOf(config);
  return walls === null ? null : <WallLines walls={walls} className={className} />;
}

/** Words for the published layouts, used only when the API sends no label of its own. */
const LAYOUT_WORDS: Record<string, string> = {
  back_left_right: 'back wall and both ends',
  back_and_left: 'back wall and left end',
  back_and_right: 'back wall and right end',
  back_only: 'back wall only',
  island: 'island, no walls',
};

function labelOf(layout: CountertopResult['wall_layout']): string {
  return `Walls: ${layout.label ?? LAYOUT_WORDS[layout.config ?? ''] ?? 'set'} — ${WALL_SOURCE_TITLE[layout.source]}`;
}

/** The plan view: back wall across the top, the end walls down the sides, the stone between. */
function WallLines({ walls, label, className }: { walls: NonNullable<ReturnType<typeof wallsOf>>; label?: string; className?: string }) {
  return (
    <svg viewBox="0 0 28 18" className={cn('h-[18px] w-7 shrink-0', className)} {...(label ? { role: 'img', 'aria-label': label } : { 'aria-hidden': true })}>
      {label && <title>{label}</title>}
      <line x1="3" y1="2" x2="25" y2="2" className={wallClass(walls.back)} strokeWidth="2.5" strokeLinecap="round" />
      <line x1="2" y1="2" x2="2" y2="16" className={wallClass(walls.left)} strokeWidth="2.5" strokeLinecap="round" />
      <line x1="26" y1="2" x2="26" y2="16" className={wallClass(walls.right)} strokeWidth="2.5" strokeLinecap="round" />
      <rect x="6" y="6" width="16" height="6" rx="1" className="fill-muted-foreground/40" />
    </svg>
  );
}

function wallClass(present: boolean): string {
  return present ? 'stroke-foreground' : 'stroke-muted-foreground/40 [stroke-dasharray:2_2]';
}
