import { useSyncExternalStore } from 'react';

/**
 * Whether the reviewer's system asks for reduced motion (#1470). The chat's spark, shimmer, spinners
 * and folds read it, so they stand still and open at once when it does.
 */

const REDUCED = '(prefers-reduced-motion: reduce)';

function subscribe(change: () => void) {
  const query = typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(REDUCED) : null;
  query?.addEventListener?.('change', change);
  return () => query?.removeEventListener?.('change', change);
}

/** True when the system asks for reduced motion: no shimmer, no spin, folds open at once. */
export function useReducedMotion(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => (typeof window !== 'undefined' && window.matchMedia ? window.matchMedia(REDUCED).matches : false),
    () => false,
  );
}
