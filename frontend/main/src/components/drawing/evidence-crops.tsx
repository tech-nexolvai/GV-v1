import { FileImage } from 'lucide-react';

import { cn } from '@/lib/utils';
import type { Reading } from '@/lib/drawing-viewer';
import { Skeleton } from '@/components/ui/skeleton';
import { Tooltip, TooltipContent, TooltipProvider, TooltipTrigger } from '@/components/ui/tooltip';
import { cropKey, useBlob, type BlobCache } from './blob-cache';

const ROLE_WORD: Record<string, string> = { SHOP: 'Shop drawing', ARCH: "Architect's drawing" };

/**
 * The stored crops behind a result (#1045), with the exact value printed over each one. Shop and
 * architect crops sit side by side when a result has both. Picking a crop shows its spot on the page.
 */
export function EvidenceCrops({
  readings,
  cache,
  projectId,
  packageId,
  active,
  onPick,
}: {
  readings: Reading[];
  cache: BlobCache;
  projectId: string;
  packageId: string;
  active: string | null;
  onPick: (reading: Reading) => void;
}) {
  const roles = [...new Set(readings.map((r) => r.role))].sort((a, b) => (a === 'ARCH' ? -1 : b === 'ARCH' ? 1 : a.localeCompare(b)));
  return (
    <div data-slot="evidence-crops" className={cn('grid gap-3', roles.length > 1 && 'sm:grid-cols-2')}>
      {roles.map((role) => (
        <section key={role} aria-label={ROLE_WORD[role] ?? role} className="flex min-w-0 flex-col gap-2">
          <h4 className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
            <span className="rounded border px-1 font-mono text-[10px] tracking-wide">{role}</span>
            {ROLE_WORD[role] ?? role}
          </h4>
          <div className="grid grid-cols-2 gap-2">
            {readings
              .filter((r) => r.role === role)
              .map((reading) => (
                <Crop key={reading.observationId} reading={reading} cache={cache} projectId={projectId} packageId={packageId} active={active === reading.observationId} onPick={onPick} />
              ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function Crop({ reading, cache, projectId, packageId, active, onPick }: { reading: Reading; cache: BlobCache; projectId: string; packageId: string; active: boolean; onPick: (reading: Reading) => void }) {
  const [state, retry] = useBlob(cache, cropKey(projectId, packageId, reading.observationId));
  return (
    <figure className="flex min-w-0 flex-col gap-1">
      <figcaption className="truncate text-xs text-muted-foreground" title={reading.label}>
        {reading.label}
      </figcaption>
      <button
        type="button"
        data-reading-crop={reading.observationId}
        aria-pressed={active}
        disabled={!reading.spot}
        onClick={() => onPick(reading)}
        title={reading.spot ? `${reading.label}: ${reading.value}. Show where it was read.` : `${reading.label}: ${reading.value}`}
        className={cn(
          'relative flex h-24 items-center justify-center overflow-hidden rounded-md border bg-white outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 enabled:cursor-pointer enabled:hover:border-foreground/40',
          active && 'border-foreground ring-1 ring-foreground',
        )}
      >
        {state.status === 'loading' && <Skeleton className="size-full rounded-none" />}
        {state.status === 'ready' && <img src={state.url} alt={`${reading.label}, stored crop`} className="max-h-full max-w-full object-contain" />}
        {(state.status === 'error' || state.status === 'not-ready') && (
          <span className="flex flex-col items-center gap-1 px-2 text-center text-[11px] text-muted-foreground">
            <FileImage className="size-4" aria-hidden="true" />
            Crop not available
          </span>
        )}
        <span data-slot="crop-value" className="num absolute bottom-1 left-1 rounded border bg-background/95 px-1.5 py-0.5 text-xs font-medium text-foreground shadow-sm">
          {reading.value}
        </span>
      </button>
      {(state.status === 'error' || state.status === 'not-ready') && (
        <button type="button" onClick={retry} className="self-start text-[11px] text-muted-foreground underline underline-offset-2 hover:text-foreground">
          Try again
        </button>
      )}
    </figure>
  );
}

/**
 * One line for a result with no crops, and the reason behind a "?". It brings its own tooltip
 * provider, so the legacy evidence panel can use it too.
 */
export function NoCrops({ line, why }: { line: string; why: string }) {
  return (
    <TooltipProvider delayDuration={250}>
      <p data-slot="no-crops" className="flex items-center gap-1.5 font-sans text-sm text-muted-foreground">
        <FileImage className="size-4 shrink-0" aria-hidden="true" />
        {line}
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button" className="inline-flex size-5 items-center justify-center rounded-full border text-xs" aria-label={`Why: ${line}`}>
              ?
            </button>
          </TooltipTrigger>
          <TooltipContent className="max-w-64">{why}</TooltipContent>
        </Tooltip>
      </p>
    </TooltipProvider>
  );
}
