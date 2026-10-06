import { useEffect, useState, type MouseEvent } from 'react';

import type { NewPart } from './DrawingPartsList.js';
import type { PartDrawing, PartKind, PartPoint } from './drawingPartChoices.js';
import { clickToStoredPoint, nearestPlacementSnap } from './placementGeometry.js';

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
    return <p className="enter-values__hint">Preparing the vendor-only drawing picture for placement…</p>;
  }
  if (image.error) return <p className="enter-values__error" role="alert">The vendor drawing picture could not be loaded: {image.error}</p>;
  if (!image.url) return <p className="enter-values__hint">Loading the vendor-only drawing…</p>;

  return (
    <div className="vendor-placement">
      <p className="enter-values__hint">Click the first end, then the second. A nearby detected line end is suggested; accept it or use your clicked point.</p>
      <div className="vendor-placement__image-wrap">
        <div className="vendor-placement__canvas">
          <img
            className="vendor-placement__image"
            src={image.url}
            alt={`Vendor-only drawing, page ${drawing.page_index + 1}. Click to place two ends.`}
            onClick={place}
          />
          {first && second && (
            <svg className="vendor-placement__overlay" viewBox="0 0 1 1" preserveAspectRatio="none" aria-hidden="true">
              <line x1={first.x} y1={first.y} x2={second.x} y2={second.y} />
            </svg>
          )}
        </div>
      </div>
      <ol className="vendor-placement__ends">
        {ends.map((selection, index) => (
          <li key={index}>
            <strong>{index === 0 ? 'First end' : 'Second end'}:</strong>{' '}
            {!selection
              ? 'click the drawing'
              : selection.snap && !selection.chosen
                ? `detected ${selection.source} end nearby — choose a point`
                : selection.snap
                  ? `using the detected ${selection.source} end`
                  : 'free point'}
            {selection?.snap && (
              <span className="vendor-placement__choice">
                <button type="button" className="btn btn--sm btn--subtle" disabled={disabled} aria-pressed={selection.chosen === selection.snap} onClick={() => choose(index as 0 | 1, true)}>Use snap</button>
                <button type="button" className="btn btn--sm btn--subtle" disabled={disabled} aria-pressed={selection.chosen === selection.clicked} onClick={() => choose(index as 0 | 1, false)}>Use clicked point</button>
              </span>
            )}
          </li>
        ))}
      </ol>
      <button
        type="button"
        className="btn btn--sm btn--subtle"
        disabled={disabled || !distinct}
        onClick={() => first && second && onAdd({ kind, code, ends: [first, second] })}
      >
        Confirm this part
      </button>
      <button type="button" className="btn btn--sm btn--subtle" disabled={disabled || (!ends[0] && !ends[1])} onClick={() => setEnds([null, null])}>Clear points</button>
    </div>
  );
}
