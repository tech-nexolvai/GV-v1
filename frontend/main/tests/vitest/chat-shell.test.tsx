// @vitest-environment jsdom
/**
 * The chat-first flow through the whole app (#1464): an upload lands in a new chat about the drawing set, the
 * sidebar opens a set's chats, and "Continue a review" and an old review link open the chat (the old review
 * screens were retired on 2026-10-28). Synthetic data only; the upload form is a stand-in.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { chatOut, modelsOut, SET_NAME, summary } from './fixtures/chat';

vi.mock('@/api/config', () => ({ projectId: () => 'p' }));
// The real form uploads files; here "the upload finished" is one click.
vi.mock('@/components/upload/NewReviewForm', () => ({
  NewReviewForm: ({ onCreated }: { onCreated: (packageId: string) => void }) => (
    <button type="button" onClick={() => onCreated('pkg-1')}>
      Upload finished
    </button>
  ),
}));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let routes: Record<string, () => Response> = {};

beforeEach(() => {
  routes = {
    'GET /projects/p/packages': () => json({
      items: [{ id: 'pkg-1', vendor: SET_NAME, state: 'AWAITING_REVIEW', created_at: new Date().toISOString(), current_revision_number: 1, current_revision_id: 'r', project_id: 'p', product_type: 'countertop' }],
      next_cursor: null,
    }),
    'GET /projects/p/packages/pkg-1': () => json({ id: 'pkg-1', vendor: SET_NAME, current_revision_number: 1 }),
    'GET /assistant/models': () => json(modelsOut()),
    'GET /packages/pkg-1/chats': () => json({ chats: [summary('c-1', { title: 'Widths', turns: 1 })], package_name: SET_NAME }),
    'GET /chats/c-1': () => json(chatOut('c-1')),
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '').replace(/\?.*$/, '');
    const route = routes[`${init?.method ?? 'GET'} ${url}`];
    return route ? route() : json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
  }));
});

const { default: App } = await import('@/App');
const { COLLAPSED_KEY } = await import('@/components/shell/AppShell');

describe('chat first', () => {
  it('an upload lands in a new chat about the drawing set (made on its first message), named in the top bar', async () => {
    window.location.hash = '#/';
    render(<App />);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Upload finished' }));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1'));
    expect(await screen.findByRole('heading', { level: 1, name: SET_NAME })).toBeTruthy();
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe(`${SET_NAME} · GV Review`));
  });

  it('one sidebar: the open set lists its chats (New chat on top) and no Review list link; the top bar is quiet', async () => {
    routes['GET /packages/pkg-1/chats'] = () => json({ chats: [summary('c-1', { title: 'Widths', turns: 1 }), summary('c-2', { title: 'Sinks', turns: 1 })] });
    window.location.hash = '#/chat/pkg-1/c-1';
    render(<App />);
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    await within(sidebar).findByText(SET_NAME);
    // No second column: the chats sit under the set's name in the app's sidebar.
    expect(screen.queryByRole('complementary', { name: 'Chats' })).toBeNull();
    const chats = await within(sidebar).findByRole('navigation', { name: 'Chats in this drawing set' });
    expect(within(chats).getAllByRole('button').map((button) => button.textContent)).toEqual(['New chat']);
    expect(within(chats).getAllByRole('link').map((link) => link.querySelector('[data-kind]')?.textContent)).toEqual(['Widths', 'Sinks']);
    expect(within(chats).getByRole('link', { name: 'Widths' }).getAttribute('aria-current')).toBe('page');
    // The top bar: the set, then the chat's title; no brand in the middle.
    expect(document.querySelector('[data-slot="chat-crumb"]')?.textContent).toBe('Widths');
    expect(screen.queryByLabelText('Graniti Vicentia')).toBeNull();

    // The old review screens are gone (2026-10-28): no Review list link under the set or in the top bar.
    expect(within(sidebar).queryByRole('list', { name: `${SET_NAME}: review list` })).toBeNull();
    expect(screen.queryByRole('link', { name: 'Review list' })).toBeNull();
  });

  it('a set with many chats shows ten, then Show all', async () => {
    routes['GET /packages/pkg-1/chats'] = () => json({ chats: Array.from({ length: 12 }, (_, n) => summary(`c-${n + 1}`, { title: `Chat ${n + 1}`, turns: 1 })) });
    window.location.hash = '#/chat/pkg-1/c-1';
    render(<App />);
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    const chats = await within(sidebar).findByRole('navigation', { name: 'Chats in this drawing set' });
    await waitFor(() => expect(within(chats).getAllByRole('link')).toHaveLength(10));
    await userEvent.setup().click(within(chats).getByRole('button', { name: 'Show all (12)' }));
    expect(within(chats).getAllByRole('link')).toHaveLength(12);
  });

  it('the one sidebar folds with the top bar’s toggle, and remembers it', async () => {
    window.location.hash = '#/chat/pkg-1/c-1';
    const first = render(<App />);
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    await userEvent.setup().click(screen.getByRole('button', { name: 'Toggle sidebar' }));
    expect(localStorage.getItem(COLLAPSED_KEY)).toBe('true');
    expect(document.querySelector('[data-slot="sidebar"][data-state]')?.getAttribute('data-state')).toBe('collapsed');
    first.unmount();
    render(<App />);
    expect(document.querySelector('[data-slot="sidebar"][data-state]')?.getAttribute('data-state')).toBe('collapsed');
    localStorage.removeItem(COLLAPSED_KEY);
  });

  it('on a phone the one sidebar is a drawer; picking a chat closes it', async () => {
    const phone = vi.spyOn(window, 'matchMedia').mockImplementation((query: string) => ({
      matches: query.includes('max-width'), media: query, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    }) as MediaQueryList);
    routes['GET /packages/pkg-1/chats'] = () => json({ chats: [summary('c-1', { title: 'Widths', turns: 1 }), summary('c-2', { title: 'Sinks', turns: 1 })] });
    window.location.hash = '#/chat/pkg-1/c-1';
    render(<App />);
    await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` });
    const user = userEvent.setup();
    await user.click(screen.getByRole('button', { name: 'Toggle sidebar' }));
    const drawer = await screen.findByRole('dialog');
    const chats = await within(drawer).findByRole('navigation', { name: 'Chats in this drawing set' });
    await user.click(within(chats).getByRole('link', { name: 'Sinks' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    phone.mockRestore();
  });

  it('an old review link (#/review/<set>, also /advanced) opens the set’s chat; picking the set in the sidebar does too', async () => {
    window.location.hash = '#/review/pkg-1/advanced';
    render(<App />);
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
    // No old screen and no old docked panel.
    expect(document.querySelector('[data-slot="assistant-panel"]')).toBeNull();

    window.location.hash = '#/';
    await waitFor(() => expect(screen.queryByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeNull());
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    await userEvent.setup().click(within(sidebar).getByText(SET_NAME));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1'));
  });

  it('"Continue a review" on the start page opens the set’s chat', async () => {
    window.location.hash = '#/';
    render(<App />);
    const recent = await screen.findByRole('region', { name: 'Continue a review' });
    await userEvent.setup().click(within(recent).getByRole('button', { name: new RegExp(SET_NAME) }));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1'));
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
  });
});
