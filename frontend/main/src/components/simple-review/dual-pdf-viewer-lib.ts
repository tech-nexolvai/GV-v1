/**
 * The arithmetic behind the side-by-side viewer (#1355): which page each side shows, how a page is
 * fitted, and how big a picture to decode. Pure functions, so they are tested without a browser.
 *
 * Pages are 1-based. Sizes are picture pixels; a view's `scale` is screen (CSS) pixels per picture
 * pixel, and `x`/`y` are where the page's top-left corner sits in the pane.
 */

import type { Rect, Size, View } from '@/lib/drawing-viewer';

export type Side = 'vendor' | 'architect';
export type Pages = Record<Side, number>;

export interface Nav {
  pages: Pages;
  counts: Pages;
  linked: boolean;
  /** The side the reviewer used last: ←/→ move it when the pages are not linked. */
  active: Side;
  /** One file: the architect side is always the vendor's page (zoomed to its architect drawing). */
  combined: boolean;
  /** Linked: architect page = vendor page + offset, each held inside its own file. */
  offset: number;
  /** Linked: where the pair is, on the vendor's numbering (it may run past the vendor's last page). */
  position: number;
}

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), Math.max(lo, hi));

export function openNav(start: Pages, counts: Pages, combined: boolean): Nav {
  const vendor = clamp(start.vendor, 1, counts.vendor);
  const architect = combined ? vendor : clamp(start.architect, 1, counts.architect);
  const fixed = combined ? { vendor: counts.vendor, architect: counts.vendor } : counts;
  return { pages: { vendor, architect }, counts: fixed, linked: true, active: 'vendor', combined, offset: architect - vendor, position: vendor };
}

/**
 * The page counts after the list is loaded again ("Check again", #1362): the pages shown stay, held
 * inside the new counts. A linked pair keeps its offset.
 */
export function withCounts(nav: Nav, counts: Pages): Nav {
  const fixed = nav.combined ? { vendor: counts.vendor, architect: counts.vendor } : counts;
  const now = { ...nav, counts: fixed };
  if (nav.linked) return linkedAt(now, nav.position);
  return { ...now, pages: { vendor: clamp(nav.pages.vendor, 1, fixed.vendor), architect: clamp(nav.pages.architect, 1, fixed.architect) } };
}

/** The linked pair at `position`: the range lets the longer file keep going after the shorter ends. */
function linkedAt(nav: Nav, position: number): Nav {
  const lo = Math.min(1, 1 - nav.offset);
  const hi = Math.max(nav.counts.vendor, nav.counts.architect - nav.offset);
  const p = clamp(position, lo, hi);
  return {
    ...nav,
    position: p,
    pages: { vendor: clamp(p, 1, nav.counts.vendor), architect: clamp(p + nav.offset, 1, nav.counts.architect) },
  };
}

export function step(nav: Nav, delta: number): Nav {
  if (nav.linked) return linkedAt(nav, nav.position + delta);
  return goTo(nav, nav.active, nav.pages[nav.active] + delta);
}

export function goTo(nav: Nav, side: Side, page: number): Nav {
  if (nav.linked) return linkedAt({ ...nav, active: side }, side === 'vendor' ? page : page - nav.offset);
  return { ...nav, active: side, pages: { ...nav.pages, [side]: clamp(page, 1, nav.counts[side]) } };
}

/** Unlink, or link again at the pages now shown. A combined set is one file and stays linked. */
export function toggleLink(nav: Nav): Nav {
  if (nav.combined) return nav;
  if (nav.linked) return { ...nav, linked: false };
  return { ...nav, linked: true, offset: nav.pages.architect - nav.pages.vendor, position: nav.pages.vendor };
}

/** Space kept around the page, in screen pixels. */
export const MARGIN = 16;

/** One device pixel per picture pixel: zooming further would only enlarge pixels, so it stops here. */
export function maxScale(devicePixelRatio: number): number {
  return 1 / Math.max(devicePixelRatio || 1, 1);
}

/** The page as wide as the pane (never past `max`), its top at the margin, centred across. */
export function fitWidth(page: Size, box: Size, max: number): View {
  const scale = Math.max(Math.min((box.w - 2 * MARGIN) / page.w, max), 1e-4);
  return { scale, x: (box.w - page.w * scale) / 2, y: MARGIN };
}

/** Zoom limits: down to half the whole page in view, up to one device pixel per picture pixel. */
export function zoomLimits(page: Size, box: Size, max: number): { min: number; max: number } {
  const whole = Math.min((box.w - 2 * MARGIN) / page.w, (box.h - 2 * MARGIN) / page.h);
  return { min: Math.min(whole / 2, max), max };
}

/** Centre a 0–1 rectangle (an architect's drawing on a combined sheet) and fill most of the pane. */
export function regionView(rect: Rect, page: Size, box: Size, limits: { min: number; max: number }, fill = 0.92): View {
  const w = Math.max(rect.w * page.w, 1);
  const h = Math.max(rect.h * page.h, 1);
  const scale = clamp(Math.min((box.w * fill) / w, (box.h * fill) / h), limits.min, limits.max);
  const cx = (rect.x + rect.w / 2) * page.w;
  const cy = (rect.y + rect.h / 2) * page.h;
  return { scale, x: box.w / 2 - cx * scale, y: box.h / 2 - cy * scale };
}

/** The API's box (exact decimal text, 0–1) as numbers. */
export function regionRect(bbox: { x0: string; y0: string; x1: string; y1: string }): Rect {
  const [x0, y0, x1, y1] = [bbox.x0, bbox.y0, bbox.x1, bbox.y1].map(Number);
  return { x: x0, y: y0, w: x1 - x0, h: y1 - y0 };
}

/** The whole page at the size it is drawn on this screen: never more pixels than the stored picture. */
export function decodeSize(page: Size, scale: number, dpr: number): Size {
  const k = Math.min(scale * Math.max(dpr || 1, 1), 1);
  return { w: Math.max(1, Math.min(page.w, Math.ceil(page.w * k))), h: Math.max(1, Math.min(page.h, Math.ceil(page.h * k))) };
}

export interface Crop {
  /** The visible part of the page, in picture pixels. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** How many pixels to decode it at: what the screen shows, never more than the crop holds. */
  size: Size;
}

/**
 * Zoomed in past what the whole-page picture holds, the visible part is decoded again, sharp, at the
 * screen's size. Memory stays about two panes' worth, whatever the zoom; null when the base is enough.
 */
export function detailCrop(page: Size, view: View, box: Size, dpr: number, base: Size): Crop | null {
  const ratio = Math.max(dpr || 1, 1);
  const needed = Math.min(view.scale * ratio, 1);
  if (needed <= (base.w / page.w) * 1.25) return null;
  const x = Math.max(0, Math.floor(-view.x / view.scale));
  const y = Math.max(0, Math.floor(-view.y / view.scale));
  const right = Math.min(page.w, Math.ceil((box.w - view.x) / view.scale));
  const bottom = Math.min(page.h, Math.ceil((box.h - view.y) / view.scale));
  const w = right - x;
  const h = bottom - y;
  if (w <= 0 || h <= 0) return null;
  return { x, y, w, h, size: { w: Math.max(1, Math.min(w, Math.ceil(w * needed))), h: Math.max(1, Math.min(h, Math.ceil(h * needed))) } };
}

/** A decoded picture: a bitmap (or, without `createImageBitmap`, an object URL) and how to free it. */
export interface Decoded {
  source: ImageBitmap | string;
  width: number;
  height: number;
  close: () => void;
}

/**
 * Decode `blob` (or one part of it) at `size`. Tests pass their own. `canDecodeParts` says whether a
 * part can be decoded at all (#1362); left out, it can.
 */
export type Decoder = ((blob: Blob, crop: Omit<Crop, 'size'> | null, size: Size) => Promise<Decoded>) & { canDecodeParts?: () => boolean };

/** True when the browser can decode one part of a picture at a chosen size. */
export const canDecodeParts = () => typeof createImageBitmap === 'function';

const decodeInBrowser = async (blob: Blob, crop: Omit<Crop, 'size'> | null, size: Size): Promise<Decoded> => {
  if (canDecodeParts()) {
    const options: ImageBitmapOptions = { resizeWidth: size.w, resizeHeight: size.h, resizeQuality: 'high' };
    const bitmap = crop ? await createImageBitmap(blob, crop.x, crop.y, crop.w, crop.h, options) : await createImageBitmap(blob, options);
    return { source: bitmap, width: bitmap.width, height: bitmap.height, close: () => bitmap.close() };
  }
  const url = URL.createObjectURL(blob);
  return { source: url, width: size.w, height: size.h, close: () => URL.revokeObjectURL(url) };
};

/** Without `createImageBitmap` it can only hand over the whole picture, so it never decodes a part. */
export const browserDecoder: Decoder = Object.assign(decodeInBrowser, { canDecodeParts });
