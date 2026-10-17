/**
 * The products GV Review knows (#1547), as `GET /products` lists them: the product registry, in its
 * order. The chat's product chips and "+" menu Focus options, the words a full check is named with,
 * the findings' groups and the upload picker are all built from this list, never from a list in the
 * frontend: a product added to the registry appears here with no frontend change.
 *
 * The list is loaded once per page and kept here, so plain functions (words, groups) and components
 * read the same one. Until it has loaded, words fall back to the product id made readable
 * ("case_goods" → "Case goods"), so nothing shows blank.
 */

import { useEffect, useSyncExternalStore } from 'react';

import { listProducts } from '@/api/client';
import type { components } from '@/api/schema';

export type ProductInfo = components['schemas']['ProductOut'];
export type ProductCatalog = readonly ProductInfo[];

/** A finding's category for an item of no product. */
export const OTHER_CATEGORY = 'other';

let catalog: ProductCatalog = [];
let loading: Promise<void> | null = null;
const listeners = new Set<() => void>();

/** The products as last loaded (empty until then). */
export function productCatalog(): ProductCatalog {
  return catalog;
}

/** Replace the products (the load, or a test). */
export function setProductCatalog(next: ProductCatalog): void {
  catalog = next;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Load the products once (a failure leaves the list empty; the next caller tries again). */
export function loadProducts(): Promise<void> {
  if (catalog.length) return Promise.resolve();
  loading ??= listProducts()
    .then((products) => setProductCatalog(products))
    .catch(() => undefined)
    .finally(() => {
      loading = null;
    });
  return loading;
}

/** The products, loaded on first use; the component renders again when they arrive. */
export function useProductCatalog(): ProductCatalog {
  const products = useSyncExternalStore(subscribe, productCatalog, productCatalog);
  useEffect(() => {
    if (!products.length) void loadProducts();
  }, [products.length]);
  return products;
}

// ── Words ───────────────────────────────────────────────────

/** "case_goods" → "Case goods": only while the list has not loaded. */
function readable(id: string): string {
  const words = id.replace(/_/g, ' ').trim();
  return words ? words[0].toUpperCase() + words.slice(1) : id;
}

export function productOf(id: string, products: ProductCatalog = catalog): ProductInfo | undefined {
  return products.find((product) => product.id === id);
}

/** One product's chip word: "Countertop". */
export function productLabel(id: string, products: ProductCatalog = catalog): string {
  return productOf(id, products)?.label ?? readable(id);
}

/** A product as a heading says it: "Countertops". */
export function productPlural(id: string, products: ProductCatalog = catalog): string {
  const known = productOf(id, products);
  if (known) return known.plural;
  const word = readable(id);
  return word.endsWith('s') ? word : `${word}s`;
}

/** These ids in the registry's order (unknown ones after, as given), without repeats. */
export function inRegistryOrder(ids: readonly string[], products: ProductCatalog = catalog): string[] {
  const wanted = [...new Set(ids)];
  const known = products.map((product) => product.id).filter((id) => wanted.includes(id));
  return [...known, ...wanted.filter((id) => !known.includes(id))];
}

/** The products that can be picked now, in the registry's order. */
export function activeProducts(products: ProductCatalog = catalog): ProductInfo[] {
  return products.filter((product) => product.active);
}

/** The products listed but not ready ("coming later"). */
export function comingLater(products: ProductCatalog = catalog): ProductInfo[] {
  return products.filter((product) => !product.active);
}

// ── Finding groups ──────────────────────────────────────────

/** The finding groups in order: every product, then "other". */
export function categoryOrder(products: ProductCatalog = catalog): string[] {
  return [...products.map((product) => product.id), OTHER_CATEGORY];
}

/** A finding group's heading: the product's group name, else "Other". */
export function categoryWord(category: string, products: ProductCatalog = catalog): string {
  if (category === OTHER_CATEGORY) return 'Other';
  return productOf(category, products)?.category_label ?? productPlural(category, products);
}
