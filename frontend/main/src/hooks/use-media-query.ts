import { useSyncExternalStore } from 'react';

/**
 * Whether a CSS media query matches now, following changes. A subscription rather than an effect
 * that sets state, which the React Compiler lint rejects (as `use-mobile.ts` does).
 */
export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const list = window.matchMedia(query);
      list.addEventListener('change', onChange);
      return () => list.removeEventListener('change', onChange);
    },
    () => window.matchMedia(query).matches,
    () => false,
  );
}

/** The review assistant covers the whole screen at this width and below (#1129). */
export const ASSISTANT_FULL_SCREEN = '(max-width: 900px)';
