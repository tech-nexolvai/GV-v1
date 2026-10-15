import { describe, expect, it } from 'vitest';

import {
  decodeSize,
  detailCrop,
  fitWidth,
  goTo,
  maxScale,
  openNav,
  regionRect,
  step,
  toggleLink,
} from '@/components/simple-review/dual-pdf-viewer-lib';

// Synthetic sizes only.
const BIG = { w: 10800, h: 7200 };

describe('page navigation (#1355)', () => {
  it('linked: both sides move together and keep their offset, each held inside its own pages', () => {
    let nav = openNav({ vendor: 3, architect: 12 }, { vendor: 6, architect: 14 }, false);
    expect(nav.linked).toBe(true);
    nav = step(nav, 1);
    expect(nav.pages).toEqual({ vendor: 4, architect: 13 });
    nav = step(nav, 1);
    expect(nav.pages).toEqual({ vendor: 5, architect: 14 });
    // The architect's file has ended; the vendor's has not, so only the vendor moves on.
    nav = step(nav, 1);
    expect(nav.pages).toEqual({ vendor: 6, architect: 14 });
    // And coming back, the offset is the one the reviewer linked at.
    nav = step(nav, -1);
    expect(nav.pages).toEqual({ vendor: 5, architect: 14 });
    nav = step(nav, -1);
    expect(nav.pages).toEqual({ vendor: 4, architect: 13 });
  });

  it('unlinked: only the active side moves; linking again keeps the pages where they are', () => {
    let nav = toggleLink(openNav({ vendor: 2, architect: 2 }, { vendor: 5, architect: 5 }, false));
    expect(nav.linked).toBe(false);
    nav = step({ ...nav, active: 'architect' }, 1);
    expect(nav.pages).toEqual({ vendor: 2, architect: 3 });
    nav = toggleLink(nav);
    nav = step(nav, 1);
    expect(nav.pages).toEqual({ vendor: 3, architect: 4 });
  });

  it('picking a page on one side while linked moves the other by the same amount', () => {
    let nav = openNav({ vendor: 1, architect: 3 }, { vendor: 9, architect: 9 }, false);
    nav = goTo(nav, 'vendor', 5);
    expect(nav.pages).toEqual({ vendor: 5, architect: 7 });
    nav = goTo(nav, 'architect', 9);
    expect(nav.pages).toEqual({ vendor: 7, architect: 9 });
  });

  it('a combined set is one file: the architect side is always the vendor page, and cannot be unlinked', () => {
    let nav = openNav({ vendor: 3, architect: 8 }, { vendor: 5, architect: 5 }, true);
    expect(nav.pages).toEqual({ vendor: 3, architect: 3 });
    nav = toggleLink(nav);
    expect(nav.linked).toBe(true);
    nav = step({ ...nav, active: 'architect' }, 1);
    expect(nav.pages).toEqual({ vendor: 4, architect: 4 });
  });

  it('pages are clamped to the file', () => {
    const nav = openNav({ vendor: 40, architect: 0 }, { vendor: 6, architect: 3 }, false);
    expect(nav.pages).toEqual({ vendor: 6, architect: 1 });
  });
});

describe('pictures are never upscaled (#1355)', () => {
  it('fit-width never draws past one screen pixel per picture pixel', () => {
    const small = { w: 400, h: 300 };
    const view = fitWidth(small, { w: 1200, h: 800 }, maxScale(2));
    expect(view.scale).toBe(0.5);
    const wide = fitWidth(BIG, { w: 1000, h: 800 }, maxScale(1));
    expect(wide.scale).toBeCloseTo((1000 - 32) / 10800, 6);
    expect(wide.y).toBe(16);
  });

  it('decodes at the drawn size, and never above the stored size', () => {
    expect(decodeSize(BIG, 0.05, 2)).toEqual({ w: 1080, h: 720 });
    expect(decodeSize(BIG, 0.9, 2)).toEqual(BIG);
  });

  it('decodes a sharp close-up of only the visible part once zoomed past the base picture', () => {
    const view = { scale: 0.5, x: -2000, y: -1000 };
    const crop = detailCrop(BIG, view, { w: 800, h: 600 }, 2, { w: 1080, h: 720 });
    expect(crop).not.toBeNull();
    expect(crop!.x).toBe(4000);
    expect(crop!.y).toBe(2000);
    expect(crop!.w).toBe(1600);
    expect(crop!.h).toBe(1200);
    // Never more pixels than the crop holds.
    expect(crop!.size.w).toBeLessThanOrEqual(crop!.w);
    // At fit, the base picture is sharp enough, so no close-up is made.
    expect(detailCrop(BIG, { scale: 0.05, x: 0, y: 0 }, { w: 800, h: 600 }, 2, { w: 1080, h: 720 })).toBeNull();
  });

  it('reads an architect region box from exact decimal text', () => {
    expect(regionRect({ x0: '0.25', y0: '0.5', x1: '0.75', y1: '1' })).toEqual({ x: 0.25, y: 0.5, w: 0.5, h: 0.5 });
  });
});
