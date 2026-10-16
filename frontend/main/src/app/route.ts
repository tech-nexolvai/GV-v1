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

/**
 * `ui-kit` is the component reference (#1029): reachable by URL only, not in the sidebar. `chat` is
 * a drawing set's chats (#1464): where an upload lands, and what a drawing set in the sidebar opens.
 */
export type Page = 'review' | 'chat' | 'documents' | 'rulebook' | 'settings' | 'usage' | 'ui-kit';

export interface Route {
  page: Page;
  /**
   * The package under review, or whose chats are open. Only meaningful on `review` (where `null` is
   * the start screen) and `chat` (never `null` there).
   */
  packageId: string | null;
  /**
   * Chat only (#1464): `#/chat/<packageId>/<chatId>` opens that chat. Absent on `#/chat/<packageId>`,
   * which opens the drawing set's newest chat (or a new empty one).
   */
  chatId?: string;
  /**
   * The old tabs and wizard (#1343): `#/review/<id>/advanced`, reached from "More". Without it a
   * review opens on the simple review page. Absent rather than false, so plain routes stay plain.
   */
  advanced?: true;
  /**
   * Company settings only (#1369): `#/settings/company?focus=<setting>` opens the page on that field,
   * so a "Not checked: set in Company settings" item links straight to it. `project` narrows it to one
   * project's field. Absent on plain routes.
   */
  focus?: string;
  project?: string;
}

const PAGES: readonly Page[] = ['review', 'chat', 'documents', 'rulebook', 'settings', 'usage', 'ui-kit'];

/**
 * Recognised routes are review, chat, documents, rulebook, settings, usage and ui-kit; unknown or
 * malformed routes open the review start. A chat with no drawing set is the review start too.
 */
export function parseRoute(hash: string): Route {
  let parts: string[];
  let query: URLSearchParams;
  try {
    const [path, search = ''] = hash.replace(/^#\/?/, '').split('?', 2);
    parts = path.split('/').filter(Boolean).map(decodeURIComponent);
    query = new URLSearchParams(search);
  } catch {
    // An edited or truncated URL must not crash the whole reviewer app.
    return { page: 'review', packageId: null };
  }
  const page = PAGES.find((candidate) => candidate === parts[0]);
  if (page === undefined) return { page: 'review', packageId: null };
  if (page === 'review') {
    const packageId = parts[1] ?? null;
    return packageId !== null && parts[2] === 'advanced' ? { page, packageId, advanced: true } : { page, packageId };
  }
  if (page === 'chat') {
    const packageId = parts[1];
    if (!packageId) return { page: 'review', packageId: null };
    return parts[2] ? { page, packageId, chatId: parts[2] } : { page, packageId };
  }
  if (page === 'settings') {
    const focus = query.get('focus')?.trim();
    const project = query.get('project')?.trim();
    if (!focus) return { page, packageId: null };
    return project ? { page, packageId: null, focus, project } : { page, packageId: null, focus };
  }
  return { page, packageId: null };
}

export function formatRoute(route: Route): string {
  if (route.page === 'review') {
    if (!route.packageId) return '#/';
    return `#/review/${encodeURIComponent(route.packageId)}${route.advanced ? '/advanced' : ''}`;
  }
  if (route.page === 'chat') {
    if (!route.packageId) return '#/';
    const chat = route.chatId ? `/${encodeURIComponent(route.chatId)}` : '';
    return `#/chat/${encodeURIComponent(route.packageId)}${chat}`;
  }
  if (route.page === 'settings' && route.focus) {
    const query = new URLSearchParams({ focus: route.focus, ...(route.project ? { project: route.project } : {}) });
    return `#/settings/company?${query.toString()}`;
  }
  return `#/${route.page}`;
}

/** How to move: `replace` swaps the current history entry instead of adding one (Back skips it). */
export interface NavigateOptions {
  replace?: boolean;
}

/**
 * The current route, and a way to move to another one that the back button can undo. With
 * `{ replace: true }` the move replaces the current entry: `#/chat/<set>` becoming that set's newest
 * chat is not a step Back should return to.
 */
export function useRoute(): [Route, (next: Route, options?: NavigateOptions) => void] {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));

  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener('hashchange', onChange);
    return () => window.removeEventListener('hashchange', onChange);
  }, []);

  const navigate = useCallback((next: Route, options?: NavigateOptions) => {
    const target = formatRoute(next);
    if (window.location.hash !== target && !(target === '#/' && window.location.hash === '')) {
      // replaceState fires no hashchange, so the state below is the only update, as it should be.
      if (options?.replace) window.history.replaceState(window.history.state, '', target);
      else window.location.hash = target;
    }
    setRoute(next);
  }, []);

  return [route, navigate];
}
