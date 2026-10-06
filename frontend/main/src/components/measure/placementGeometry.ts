import type { PartPoint } from './drawingPartChoices.js';

export type PlacementSnapPoint = PartPoint & { source: string };

/**
 * Map a click through the published PageTransform's image-to-stored step:
 * PageTransform.to_stored(ImagePoint(px, py)) = (px / width_px, py / height_px).
 * The browser shows the exact rendered page PNG, so its measured bounds are the image frame.
 */
export function clickToStoredPoint(
  clientX: number,
  clientY: number,
  bounds: { left: number; top: number; width: number; height: number },
  image: { width_px: number; height_px: number },
): PartPoint {
  if (bounds.width <= 0 || bounds.height <= 0 || image.width_px <= 0 || image.height_px <= 0) {
    throw new Error('the displayed drawing has no measurable size');
  }
  const px = Math.max(0, Math.min(image.width_px - 1, Math.round((clientX - bounds.left) * image.width_px / bounds.width)));
  const py = Math.max(0, Math.min(image.height_px - 1, Math.round((clientY - bounds.top) * image.height_px / bounds.height)));
  return { x: (px / image.width_px).toString(), y: (py / image.height_px).toString() };
}

/** Nearest unique detected endpoint, only inside the page's stated stored-space tolerance. */
export function nearestPlacementSnap(
  point: PartPoint,
  candidates: readonly PlacementSnapPoint[],
  tolerance: string | null,
): PlacementSnapPoint | null {
  if (tolerance === null) return null;
  const limit = Number(tolerance);
  if (!Number.isFinite(limit) || limit < 0) return null;
  // A shared physical endpoint can arrive once as a dimension endpoint and once as an extension
  // endpoint. Those are duplicate descriptions of one place, not competing snap choices. Preserve
  // the first description (the server orders dimension points before extensions) and keep the
  // ambiguity refusal for genuinely distinct points at equal distance.
  const physical = new Map<string, PlacementSnapPoint>();
  for (const candidate of candidates) {
    const key = `${Number(candidate.x)},${Number(candidate.y)}`;
    if (!physical.has(key)) physical.set(key, candidate);
  }
  const ranked = [...physical.values()].map((candidate) => ({
    candidate,
    distance: Math.hypot(Number(candidate.x) - Number(point.x), Number(candidate.y) - Number(point.y)),
  })).sort((a, b) => a.distance - b.distance);
  if (!ranked[0] || ranked[0].distance > limit || ranked[1]?.distance === ranked[0].distance) return null;
  return ranked[0].candidate;
}
