// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { PackageSummary } from '@/api/client';
import { DocumentsTable } from '@/components/documents/documents-table';
import { matchesSearch, productWord, resultRank } from '@/lib/documents-table';
import { countParts, countSentence, setResult } from '@/lib/set-check';
import { PackagesPage } from '@/pages/PackagesPage';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const zero = { pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 };
type Check = NonNullable<PackageSummary['full_check']>;
function check(extra: Partial<Check> = {}): Check {
  return {
    chat_id: 'chat-1',
    message_id: 'answer-1',
    ran_at: '2026-10-05T09:00:00Z',
    stopped: null,
    review_result: { action: 'revise_resubmit', words: 'Revise and resubmit' },
    counts: { wrong: 5, right: 12, ask_architect: 0, likely_problem: 0, not_compared: 30 },
    ...extra,
  };
}
function summary(id: string, extra: Partial<PackageSummary> = {}): PackageSummary {
  return {
    package_id: id,
    revision_id: `rev-${id}`,
    revision_number: 1,
    vendor: `Synthetic ${id}`,
    product_type: 'countertop',
    // The old pipeline's state: a chat-read set never moves on there, and it is not shown.
    state: 'UPLOADING',
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    outcomes: { ...zero },
    needs_decision: 0,
    needs_decision_by_outcome: { fail: 0, review: 0, not_found: 0, other: 0 },
    approved: false,
    signed_exports_ready: false,
    checking: false,
    drawings: null,
    full_check: null,
    ...extra,
  };
}

// In the server's order: newest change first.
const ROWS: PackageSummary[] = [
  summary('b', { vendor: 'Beta Stone', full_check: check() }),
  summary('a', { vendor: 'alpha works', full_check: check({ chat_id: 'chat-a', ran_at: '2026-10-03T09:00:00Z', review_result: { action: 'approved', words: 'Approved' }, counts: { wrong: 0, right: 20, ask_architect: 2, likely_problem: 0, not_compared: 1 } }) }),
  summary('u', { vendor: null, product_type: 'cabinet', drawings: 'reading' }),
];

function vendorsInOrder() {
  const table = screen.getByRole('table', { name: 'Drawing sets' });
  return within(table).getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('button')[0].textContent?.replace(/Revision \d+$/, ''));
}

describe('documents table words', () => {
  it('says the newest full check in AWI words, shortening only "Check incomplete"', () => {
    expect(setResult({ full_check: check() })).toEqual({ kind: 'revise_resubmit', words: 'Revise and resubmit', title: null });
    expect(setResult({ full_check: check({ review_result: { action: 'not_for_sending', words: 'Check incomplete: not for sending' } }) }))
      .toEqual({ kind: 'not_for_sending', words: 'Check incomplete', title: 'Check incomplete: not for sending' });
    expect(setResult({ full_check: check({ review_result: null, stopped: 'cost_cap' }) }))
      .toEqual({ kind: 'unfinished', words: 'Check did not finish', title: 'It stopped at its price limit.' });
    expect(setResult({ full_check: check({ review_result: { action: 'review_needed', words: 'Review needed' } }) }))
      .toEqual({ kind: 'review_needed', words: 'Review needed', title: null });
    expect(setResult({}).words).toBe('Not checked yet');
    expect(setResult({ checking: true, full_check: check() }).words).toBe('Full check running…');
  });

  it('counts as the result screen does: Ask the architect only when there is one', () => {
    expect(countParts({ wrong: 1, right: 2, ask_architect: 0, likely_problem: 0, not_compared: 3 }).map((part) => part.words)).toEqual(['Wrong', 'Right', 'Not compared']);
    expect(countSentence({ wrong: 1, right: 2, ask_architect: 4, likely_problem: 0, not_compared: 3 })).toBe('1 Wrong, 2 Right, 4 Ask the architect, 3 Not compared');
  });

  it('orders results by what they ask of the vendor, sets not checked yet last', () => {
    expect(resultRank({ full_check: check({ review_result: { action: 'rejected', words: 'Rejected' } }) })).toBeLessThan(resultRank({ full_check: check() }));
    expect(resultRank({ full_check: check() })).toBeLessThan(resultRank({ full_check: check({ review_result: { action: 'approved', words: 'Approved' } }) }));
    expect(resultRank({ full_check: check() })).toBeLessThan(resultRank({}));
    // Review needed sorts before any approval: a person decides first.
    const pending = resultRank({ full_check: check({ review_result: { action: 'review_needed', words: 'Review needed' } }) });
    expect(resultRank({ full_check: check() })).toBeLessThan(pending);
    expect(pending).toBeLessThan(resultRank({ full_check: check({ review_result: { action: 'approved_as_noted', words: 'Approved as noted' } }) }));
  });

  it('searches the vendor and the product, ignoring case and spaces around', () => {
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, '  beta ')).toBe(true);
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, 'COUNTER')).toBe(true);
    expect(matchesSearch({ vendor: null, product_type: null }, 'untitled')).toBe(true);
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, 'cabinet')).toBe(false);
    expect(productWord(null)).toBe('Not set');
  });
});

describe('DocumentsTable', () => {
  it('keeps the server order and shows each set’s newest full check, never the old pipeline', () => {
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages={false} />);
    expect(vendorsInOrder()).toEqual(['Beta Stone', 'alpha works', 'Untitled document set']);
    const [beta, alpha, untitled] = screen.getAllByRole('row').slice(1);
    // The result, in its own column (and again in the phone summary, hidden by CSS on wider screens).
    expect(within(beta).getAllByText('Revise and resubmit').length).toBeGreaterThan(0);
    expect(within(beta).getAllByText('5 Wrong, 12 Right, 30 Not compared').length).toBeGreaterThan(0);
    expect(within(alpha).getAllByText('0 Wrong, 20 Right, 2 Ask the architect, 1 Not compared').length).toBeGreaterThan(0);
    // A set with no check: "Not checked yet", and "Reading the drawings…" while its pack is made.
    expect(within(untitled).getAllByText('Not checked yet').length).toBeGreaterThan(0);
    expect(within(untitled).getAllByText('Reading the drawings…').length).toBeGreaterThan(0);
    // The old columns are gone.
    for (const old of ['Uploading', 'No results yet', 'Not under review', 'Signed off', 'Needs you']) {
      expect(screen.queryByText(old)).toBeNull();
    }
    expect(screen.getByText(/^3 drawing sets\. Each shows its newest full check\.$/)).toBeTruthy();
    // Ask the architect is in the legend because a row has one.
    expect(within(screen.getByRole('list', { name: 'Item counts' })).getAllByRole('listitem').map((li) => li.textContent?.replace(/^\?/, '')))
      .toEqual(['Wrong', 'Right', 'Ask the architect', 'Not compared']);
  });

  it('paints a result with its glyph and outcome colour, never amber', () => {
    render(<DocumentsTable rows={[summary('n', { full_check: check({ review_result: { action: 'approved_as_noted', words: 'Approved as noted' } }) }), summary('rn', { full_check: check({ review_result: { action: 'review_needed', words: 'Review needed' } }) }), ...ROWS]} onOpen={() => {}} morePages={false} />);
    const results = [...document.querySelectorAll('[data-slot="set-result"]')] as HTMLElement[];
    for (const result of results) {
      expect(result.className).not.toMatch(/review|amber/);
      expect(result.querySelector('svg')).not.toBeNull();
    }
    expect(results.find((result) => result.dataset.result === 'revise_resubmit')?.className).toContain('text-outcome-fail-fg');
    expect(results.find((result) => result.dataset.result === 'approved')?.className).toContain('text-outcome-pass-fg');
    expect(results.find((result) => result.dataset.result === 'approved_as_noted')?.className).toContain('text-foreground');
    // Review needed: neutral words with a person glyph, never an outcome colour.
    const pending = results.find((result) => result.dataset.result === 'review_needed');
    expect(pending?.className).toContain('text-foreground');
    expect(pending?.className).not.toMatch(/outcome/);
    expect(pending?.textContent).toBe('Review needed');
  });

  it('sorts by a column on click: name A→Z with untitled last, result by what it asks', async () => {
    const user = userEvent.setup();
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages={false} />);
    await user.click(screen.getByRole('button', { name: /Drawing set/ }));
    expect(vendorsInOrder()).toEqual(['alpha works', 'Beta Stone', 'Untitled document set']);
    await user.click(screen.getByRole('button', { name: /Review result/ }));
    expect(vendorsInOrder()).toEqual(['Beta Stone', 'alpha works', 'Untitled document set']);
    await user.click(screen.getByRole('button', { name: /Checked/ }));
    expect(vendorsInOrder()).toEqual(['Beta Stone', 'alpha works', 'Untitled document set']);
  });

  it('searches by vendor or product and says how many match', async () => {
    const user = userEvent.setup();
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages />);
    expect(screen.getByText(/Sorting and search cover this page only\./)).toBeTruthy();
    await user.type(screen.getByRole('searchbox', { name: 'Search vendor or product' }), 'cabinet');
    expect(vendorsInOrder()).toEqual(['Untitled document set']);
    expect(screen.getByText(/1 of 3 drawing sets match\./)).toBeTruthy();
    await user.clear(screen.getByRole('searchbox'));
    await user.type(screen.getByRole('searchbox'), 'nothing like this');
    expect(screen.getByText('Nothing matches “nothing like this”.')).toBeTruthy();
  });

  it('opens the newest check’s chat by its Open button or its name; a set with none opens its chats', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<DocumentsTable rows={ROWS} onOpen={onOpen} morePages={false} />);
    await user.click(screen.getByRole('button', { name: 'Open the full check of Beta Stone' }));
    expect(onOpen).toHaveBeenCalledWith('b', 'chat-1');
    await user.click(screen.getByRole('button', { name: /^alpha works/ }));
    expect(onOpen).toHaveBeenLastCalledWith('a', 'chat-a');
    await user.click(screen.getByRole('button', { name: 'Open the chats of Untitled document set' }));
    expect(onOpen).toHaveBeenLastCalledWith('u', null);
  });
});

describe('PackagesPage', () => {
  const requests: string[] = [];
  let pages: Record<string, { items: PackageSummary[]; next_cursor: string | null; limit: number } | number>;
  beforeEach(() => {
    requests.length = 0;
    pages = { first: { items: ROWS, next_cursor: null, limit: 200 } };
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      requests.push(url);
      const cursor = new URL(url, 'http://local').searchParams.get('cursor') ?? 'first';
      const page = pages[cursor];
      if (typeof page === 'number') return new Response(JSON.stringify({ error: 'http_error', message: 'synthetic outage', request_id: 'r' }), { status: page, headers: { 'Content-Type': 'application/json' } });
      return new Response(JSON.stringify(page), { status: 200, headers: { 'Content-Type': 'application/json' } });
    }));
  });

  it('loads the whole table in one request, at the largest page size', async () => {
    render(<PackagesPage onOpenSet={() => {}} onNewPackage={() => {}} />);
    await screen.findByRole('table', { name: 'Drawing sets' });
    expect(requests).toHaveLength(1);
    expect(requests[0]).toMatch(/\/projects\/p\/packages-summary\?limit=200$/);
    expect(screen.queryByRole('navigation', { name: 'Document pages' })).toBeNull();
  });

  it('pages by the server cursor when there are more reviews', async () => {
    const user = userEvent.setup();
    pages = {
      first: { items: ROWS.slice(0, 2), next_cursor: 'opaque+cursor', limit: 200 },
      'opaque+cursor': { items: ROWS.slice(2), next_cursor: null, limit: 200 },
    };
    render(<PackagesPage onOpenSet={() => {}} onNewPackage={() => {}} />);
    await screen.findByText(/Sorting and search cover this page only/);
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(vendorsInOrder()).toEqual(['Untitled document set']));
    expect(requests.at(-1)).toContain('cursor=opaque%2Bcursor');
    expect(screen.getByText(/on this page/).textContent).toBe('Page 2 · 1 on this page');
  });

  it('says a failed load out loud, with a retry', async () => {
    pages = { first: 503 };
    render(<PackagesPage onOpenSet={() => {}} onNewPackage={() => {}} />);
    expect(await screen.findByText('Documents could not be loaded')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });
});
