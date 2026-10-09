import { useEffect, useState, type MouseEvent } from 'react';

import type { NewPart } from './DrawingPartsList.js';
import type { PartDrawing, PartKind, PartPoint } from './drawingPartChoices.js';
import { clickToStoredPoint, nearestPlacementSnap } from './placementGeometry.js';
import { Button } from '@/components/ui/button';
import { Hint, LoadError } from './wizard-ui.js';

type Selection = { clicked: PartPoint; chosen: PartPoint | null; snap?: PartPoint; source?: string };

/** Click-to-place is a proposal preview only; the part is created by the explicit final button. */
export function VendorPagePlacement({
  loadPagePicture,
  drawing,
  kind,
  code,
  disabled,
  onAdd,
}: {
  loadPagePicture: (viewId: string) => Promise<Blob>;
  drawing: PartDrawing;
  kind: PartKind;
  code: string | null;
  disabled: boolean;
  onAdd: (part: NewPart) => void;
}) {
  const metadata = drawing.page_picture;
  const [image, setImage] = useState<{ url?: string; error?: string }>({});
  const [ends, setEnds] = useState<[Selection | null, Selection | null]>([null, null]);

  useEffect(() => {
    if (!metadata) return;
    let live = true;
    let objectUrl: string | undefined;
    void loadPagePicture(drawing.view_id).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (live) setImage({ url: objectUrl });
      },
      (error: unknown) => {
        if (live) setImage({ error: error instanceof Error ? error.message : String(error) });
      },
    );
    return () => {
      live = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [drawing.view_id, loadPagePicture, metadata]);

  function place(event: MouseEvent<HTMLImageElement>) {
    if (!metadata || disabled) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const clicked = clickToStoredPoint(event.clientX, event.clientY, rect, metadata);
    const snap = nearestPlacementSnap(clicked, metadata.snap_points, metadata.snap_tolerance) ?? undefined;
    const selection: Selection = {
      clicked,
      // A nearby endpoint is only a suggestion. The person must choose the snap or the point
      // they clicked before the outline can be confirmed.
      chosen: snap ? null : clicked,
      ...(snap ? { snap: { x: snap.x, y: snap.y }, source: snap.source } : {}),
    };
    setEnds(([first, second]) => !first ? [selection, second] : !second ? [first, selection] : [selection, null]);
  }

  function choose(index: 0 | 1, accept: boolean) {
    setEnds((current) => {
      const selected = current[index];
      if (!selected) return current;
      const next: [Selection | null, Selection | null] = [...current];
      next[index] = { ...selected, chosen: accept && selected.snap ? selected.snap : selected.clicked };
      return next;
    });
  }

  const first = ends[0]?.chosen ?? undefined;
  const second = ends[1]?.chosen ?? undefined;
  const distinct = Boolean(first && second && (first.x !== second.x || first.y !== second.y));

  if (!metadata) {
    return <Hint>Preparing the vendor-only drawing picture for placement…</Hint>;
  }
  if (image.error) return <LoadError>The vendor drawing picture could not be loaded: {image.error}</LoadError>;
  if (!image.url) return <Hint>Loading the vendor-only drawing…</Hint>;

  return (
    <div className="flex flex-col gap-3">
      <Hint>Click the first end, then the second; a nearby line end is offered to snap to.</Hint>
      {/* On a phone the drawing keeps a readable 520 px and scrolls inside its frame, not the page. */}
      <div className="max-h-[55vh] overflow-auto rounded-lg border bg-white sm:max-h-none">
        <div className="relative w-[520px] sm:w-full">
          <img
            className="block w-full max-w-none cursor-crosshair"
            src={image.url}
            alt={`Vendor-only drawing, page ${drawing.page_index + 1}. Click to place two ends.`}
            onClick={place}
          />
          {/* Dark on purpose: the vendor-only picture is always black on white, in either theme. */}
          {first && second && (
            <svg className="pointer-events-none absolute inset-0 size-full" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
              <line x1={first.x} y1={first.y} x2={second.x} y2={second.y} className="stroke-neutral-900" strokeWidth={3} strokeDasharray="8 4" vectorEffect="non-scaling-stroke" />
            </svg>
          )}
        </div>
      </div>
      <ol className="flex flex-col gap-2 text-sm">
        {ends.map((selection, index) => (
          <li key={index} className="flex flex-wrap items-center gap-x-2 gap-y-1">
            <strong className="font-medium">{index === 0 ? 'First end' : 'Second end'}:</strong>{' '}
            {!selection
              ? 'click the drawing'
              : selection.snap && !selection.chosen
                ? `detected ${selection.source} end nearby — choose a point`
                : selection.snap
                  ? `using the detected ${selection.source} end`
                  : 'free point'}
            {selection?.snap && (
              <span className="flex flex-wrap gap-1.5">
                <Button type="button" size="xs" variant={selection.chosen === selection.snap ? 'default' : 'outline'} disabled={disabled} aria-pressed={selection.chosen === selection.snap} onClick={() => choose(index as 0 | 1, true)}>Use snap</Button>
                <Button type="button" size="xs" variant={selection.chosen === selection.clicked ? 'default' : 'outline'} disabled={disabled} aria-pressed={selection.chosen === selection.clicked} onClick={() => choose(index as 0 | 1, false)}>Use clicked point</Button>
              </span>
            )}
          </li>
        ))}
      </ol>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={disabled || !distinct}
          onClick={() => first && second && onAdd({ kind, code, ends: [first, second] })}
        >
          Confirm this part
        </Button>
        <Button type="button" size="sm" variant="ghost" disabled={disabled || (!ends[0] && !ends[1])} onClick={() => setEnds([null, null])}>Clear points</Button>
      </div>
    </div>
  );
}
