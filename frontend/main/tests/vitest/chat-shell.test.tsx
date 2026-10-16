// @vitest-environment jsdom
/**
 * The chat-first flow through the whole app (#1464): an upload lands in the drawing set's chat, the
 * sidebar opens a set's chats and keeps its review list one click away, and the review screen's
 * Assistant button opens the chat. Synthetic data only; the upload form is a stand-in.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { chatOut, modelsOut, SET_NAME, summary } from './fixtures/chat';
import { reviewItems } from './fixtures/review-items';

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
    'GET /projects/p/packages/pkg-1/review-items': () => json(reviewItems()),
    'GET /assistant/models': () => json(modelsOut()),
    'GET /packages/pkg-1/chats': () => json({ chats: [summary('c-1')] }),
    'GET /chats/c-1': () => json(chatOut('c-1')),
  };
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '').replace(/\?.*$/, '');
    const route = routes[`${init?.method ?? 'GET'} ${url}`];
    return route ? route() : json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
  }));
});

const { default: App } = await import('@/App');

describe('chat first', () => {
  it('an upload lands in the drawing set’s newest chat, named in the top bar', async () => {
    window.location.hash = '#/';
    render(<App />);
    await userEvent.setup().click(await screen.findByRole('button', { name: 'Upload finished' }));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1/c-1'));
    expect(await screen.findByRole('heading', { level: 1, name: SET_NAME })).toBeTruthy();
    expect(await screen.findByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeTruthy();
    await waitFor(() => expect(document.title).toBe(`${SET_NAME} · GV Review`));
  });

  it('the open set in the sidebar shows its Chats and its Review list; Review list opens the review screen', async () => {
    window.location.hash = '#/chat/pkg-1/c-1';
    render(<App />);
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    await within(sidebar).findByText(SET_NAME);
    const links = await within(sidebar).findByRole('list', { name: `${SET_NAME}: chats or review list` });
    const chats = within(links).getByRole('button', { name: 'Chats' });
    expect(chats.getAttribute('aria-current')).toBe('page');
    await userEvent.setup().click(within(links).getByRole('button', { name: 'Review list' }));
    await waitFor(() => expect(window.location.hash).toBe('#/review/pkg-1'));
  });

  it('the review screen’s Assistant button opens the set’s chat; picking the set in the sidebar does too', async () => {
    window.location.hash = '#/review/pkg-1';
    render(<App />);
    const user = userEvent.setup();
    await user.click(await screen.findByRole('button', { name: 'Assistant' }));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1/c-1'));
    // The old docked panel stays closed: the chat page is where questions go now.
    expect(document.querySelector('[data-slot="assistant-panel"]')).toBeNull();

    window.location.hash = '#/review/pkg-1';
    await waitFor(() => expect(screen.queryByRole('heading', { name: `What do you want to know about ${SET_NAME}?` })).toBeNull());
    const sidebar = document.querySelector('[data-slot="sidebar"]') as HTMLElement;
    await user.click(within(sidebar).getByText(SET_NAME));
    await waitFor(() => expect(window.location.hash).toBe('#/chat/pkg-1/c-1'));
  });
});
