/**
 * Where the reviewer is, kept in the address bar.
 *
 * Before this the screen lived only in React state, so a refresh, a shared link or the browser's
 * back button all dropped the reviewer on the start screen with their review gone from view. The
 * route is the page plus the package being reviewed — the two things the rest of the app keys on.
 *
 * **A hash route (`#/review/<id>`) rather than a path.** A path route needs whatever serves the
 * built files to answer every path with `index.html`; nothing in this repository configures that,
 * and a refresh on `/review/<id>` would then be a 404 from the host. The hash never reaches the
 * server, so it works on any static host with no backend or hosting change.
 */

import { useCallback, useEffect, useState } from 'react';

export type Page = 'review' | 'documents' | 'rulebook' | 'settings' | 'usage';

export interface Route {
  page: Page;
  /** The package under review. Only meaningful on `review`; `null` there is the start screen. */
  packageId: string | null;
}

const PAGES: readonly Page[] = ['review', 'documents', 'rulebook', 'settings', 'usage'];

/** `#/review/<id>` → review of that package; `#/documents` → documents; anything else → start. */
export function parseRoute(hash: string): Route {
  let parts: string[];
  try {
    parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent);
  } catch {
    // A pasted malformed URL is not a reason to crash the entire workspace.
    return { page: 'review', packageId: null };
  }
  const page = PAGES.find((candidate) => candidate === parts[0]);
  if (page === undefined) return { page: 'review', packageId: null };
  if (page === 'review') return { page, packageId: parts[1] ?? null };
  return { page, packageId: null };
}

export function formatRoute(route: Route): string {
  if (route.page === 'review') {
    return route.packageId ? `#/review/${encodeURIComponent(route.packageId)}` : '#/';
  }
  return `#/${route.page}`;
}

/** The current route, and a way to move to another one that the back button can undo. */
export function useRoute(): [Route, (next: Route) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));

  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const navigate = useCallback((next: Route) => {
    const target = formatRoute(next);
    if (window.location.hash !== target && !(target === '#/' && window.location.hash === '')) {
      window.location.hash = target;
    }
    setRoute(next);
  }, []);

  return [route, navigate];
}
