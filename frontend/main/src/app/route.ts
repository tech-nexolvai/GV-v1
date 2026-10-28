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
export type Page = 'review' | 'chat' | 'documents' | 'tracker' | 'rulebook' | 'settings' | 'usage' | 'ui-kit';

export interface Route {
  page: Page;
  /**
   * The drawing set whose chats are open. Only meaningful on `chat` (never `null` there); `review` is
   * the start screen and always `null`.
   */
  packageId: string | null;
  /**
   * Chat only (#1464): `#/chat/<packageId>/<chatId>` opens that chat. Absent on `#/chat/<packageId>`,
   * which opens the drawing set's newest chat (or a new empty one).
   */
  chatId?: string;
  /**
   * Chat only (#1578): `#/chat/<set>/<chat>?message=<answer>&finding=<F3>` opens that answer's
   * finding in the evidence viewer, as the job tracker's rows do. Absent on plain chat routes.
   */
  finding?: { messageId: string; findingId: string };
  /**
   * Company settings only (#1369): `#/settings/company?focus=<setting>` opens the page on that field,
   * so a "Not checked: set in Company settings" item links straight to it. `project` narrows it to one
   * project's field. Absent on plain routes.
   */
  focus?: string;
  project?: string;
}

const PAGES: readonly Page[] = ['review', 'chat', 'documents', 'tracker', 'rulebook', 'settings', 'usage', 'ui-kit'];

/**
 * Recognised routes are review, chat, documents, tracker, rulebook, settings, usage and ui-kit; unknown or
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
    // `#/review/<set>` (and `/advanced`) was the old review screens' address, retired on 2026-10-28:
    // a saved or shared link opens that drawing set's chat instead.
    const packageId = parts[1];
    return packageId ? { page: 'chat', packageId } : { page, packageId: null };
  }
  if (page === 'chat') {
    const packageId = parts[1];
    if (!packageId) return { page: 'review', packageId: null };
    if (!parts[2]) return { page, packageId };
    const messageId = query.get('message')?.trim();
    const findingId = query.get('finding')?.trim();
    return messageId && findingId ? { page, packageId, chatId: parts[2], finding: { messageId, findingId } } : { page, packageId, chatId: parts[2] };
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
    return `#/chat/${encodeURIComponent(route.packageId)}`;
  }
  if (route.page === 'chat') {
    if (!route.packageId) return '#/';
    const chat = route.chatId ? `/${encodeURIComponent(route.chatId)}` : '';
    const focus = route.chatId && route.finding ? `?${new URLSearchParams({ message: route.finding.messageId, finding: route.finding.findingId }).toString()}` : '';
    return `#/chat/${encodeURIComponent(route.packageId)}${chat}${focus}`;
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
