/**
 * jsdom lacks a few browser APIs that Radix, Recharts and the app's theme script expect. These are
 * the smallest stand-ins that let components mount; they do not pretend to lay anything out.
 */
import { afterEach, beforeEach } from 'vitest';

import { setProductCatalog } from '@/lib/products';
import { PRODUCTS } from './fixtures/products';

// The products (#1547) come from `GET /products`; tests start with the registry's list loaded.
beforeEach(() => setProductCatalog(PRODUCTS));

if (typeof window !== 'undefined') {
  const { cleanup } = await import('@testing-library/react');
  afterEach(() => cleanup());

  window.matchMedia ??= (query: string) =>
    ({
      matches: false,
      media: query,
      onchange: null,
      addEventListener() {},
      removeEventListener() {},
      addListener() {},
      removeListener() {},
      dispatchEvent: () => false,
    }) as MediaQueryList;

  globalThis.ResizeObserver ??= class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };

  Element.prototype.scrollIntoView ??= function () {};
  Element.prototype.hasPointerCapture ??= () => false;
  Element.prototype.setPointerCapture ??= function () {};
  Element.prototype.releasePointerCapture ??= function () {};
}
