// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { PackageSummary } from '@/api/client';
import { DocumentsTable } from '@/components/documents/documents-table';
import { matchesSearch, outcomeSegments, outcomeSentence, productWord, resultTotal, statusRank } from '@/lib/documents-table';
import { PackagesPage } from '@/pages/PackagesPage';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const zero = { pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 };
function summary(id: string, extra: Partial<PackageSummary> = {}): PackageSummary {
  return {
    package_id: id,
    revision_id: `rev-${id}`,
    revision_number: 1,
    vendor: `Synthetic ${id}`,
    product_type: 'countertop',
    state: 'AWAITING_REVIEW',
    created_at: '2026-10-01T10:00:00Z',
    updated_at: '2026-10-01T10:00:00Z',
    outcomes: { ...zero },
    needs_decision: 0,
    needs_decision_by_outcome: { fail: 0, review: 0, not_found: 0, other: 0 },
    approved: false,
    signed_exports_ready: false,
    ...extra,
  };
}

const ROWS: PackageSummary[] = [
  summary('b', { vendor: 'Beta Stone', updated_at: '2026-10-02T09:00:00Z', outcomes: { ...zero, pass: 8, fail: 2, review: 1 }, needs_decision: 3 }),
  summary('a', { vendor: 'alpha works', updated_at: '2026-10-05T09:00:00Z', state: 'APPROVED', outcomes: { ...zero, pass: 4 }, approved: true, signed_exports_ready: true }),
  summary('u', { vendor: null, product_type: 'cabinet', updated_at: '2026-10-03T09:00:00Z', state: 'EXTRACTING' }),
];

function vendorsInOrder() {
  const table = screen.getByRole('table', { name: 'Drawing reviews' });
  return within(table).getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('cell')[0].textContent?.replace(/Revision \d+$/, ''));
}

describe('documents table arithmetic', () => {
  it('lists non-zero recorded results in one order, with the agreed words', () => {
    expect(outcomeSegments({ ...zero, fail: 2, pass: 8, no_rule: 1 })).toEqual([
      { outcome: 'PASS', count: 8, label: 'Looks right' },
      { outcome: 'FAIL', count: 2, label: 'Needs correction' },
      { outcome: 'NO_APPLICABLE_RULE', count: 1, label: 'Not applicable' },
    ]);
    expect(resultTotal({ ...zero, pass: 1, review: 2, not_found: 3 })).toBe(6);
    expect(outcomeSentence({ ...zero, pass: 1, review: 1 })).toBe('2 recorded results: 1 Looks right, 1 Needs your decision');
  });

  it('says zero results in words, never as an empty all-clear', () => {
    expect(outcomeSentence(zero)).toBe('No recorded results yet');
  });

  it('searches the vendor and the product, ignoring case and spaces around', () => {
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, '  beta ')).toBe(true);
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, 'COUNTER')).toBe(true);
    expect(matchesSearch({ vendor: null, product_type: null }, 'untitled')).toBe(true);
    expect(matchesSearch({ vendor: 'Beta Stone', product_type: 'countertop' }, 'cabinet')).toBe(false);
    expect(productWord(null)).toBe('Not set');
  });

  it('orders statuses the way a review moves', () => {
    expect(statusRank('EXTRACTING')).toBeLessThan(statusRank('AWAITING_REVIEW'));
    expect(statusRank('AWAITING_REVIEW')).toBeLessThan(statusRank('APPROVED'));
    expect(statusRank('SOMETHING_NEW')).toBeGreaterThan(statusRank('SUPERSEDED'));
  });
});

describe('DocumentsTable', () => {
  it('opens newest-updated first, with every column for each review', () => {
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages={false} />);
    expect(vendorsInOrder()).toEqual(['alpha works', 'Untitled document set', 'Beta Stone']);
    expect(screen.getByRole('img', { name: '11 recorded results: 8 Looks right, 2 Needs correction, 1 Needs your decision' })).toBeTruthy();
    expect(screen.getByText('results need your decision', { exact: false })).toBeTruthy();
    expect(screen.getAllByText('Signed off', { selector: '.sr-only' })).toHaveLength(1);
    expect(screen.getAllByText('Signed files ready', { selector: '.sr-only' })).toHaveLength(1);
    expect(screen.getByText('3 reviews.')).toBeTruthy();
    expect(screen.queryByText(/cover this page only/)).toBeNull();
  });

  it('asks "Needs you" only of a review under review, never of a signed-off one', () => {
    const lapsed = summary('s', { vendor: 'Signed vendor', state: 'APPROVED', approved: true, outcomes: { ...zero, pass: 2 }, needs_decision: 1 });
    render(<DocumentsTable rows={[lapsed]} onOpen={() => {}} morePages={false} />);
    const row = screen.getAllByRole('row')[1];
    expect(within(row).getByText('Not under review')).toBeTruthy();
    expect(within(row).queryByText(/need your decision|needs your decision/)).toBeNull();
  });

  it('shows a review with nothing recorded as "No results yet", not as a zero', () => {
    render(<DocumentsTable rows={[{ ...ROWS[2], state: 'NEEDS_INPUT' }]} onOpen={() => {}} morePages={false} />);
    const row = screen.getAllByRole('row')[1];
    expect(within(row).getAllByText('No results yet')).toHaveLength(2); // the bar and the Needs-you cell
    expect(within(row).queryByText('0')).toBeNull();
  });

  it('sorts by a column on click: vendor A→Z with untitled last, needs-you most first', async () => {
    const user = userEvent.setup();
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages={false} />);
    await user.click(screen.getByRole('button', { name: /Vendor/ }));
    expect(vendorsInOrder()).toEqual(['alpha works', 'Beta Stone', 'Untitled document set']);
    await user.click(screen.getByRole('button', { name: /Vendor/ }));
    expect(vendorsInOrder()).toEqual(['Beta Stone', 'alpha works', 'Untitled document set']);
    await user.click(screen.getByRole('button', { name: /Needs you/ }));
    expect(vendorsInOrder()[0]).toBe('Beta Stone');
    expect(screen.getByRole('columnheader', { name: /Needs you/ }).getAttribute('aria-sort')).toBe('descending');
  });

  it('searches by vendor or product and says how many match', async () => {
    const user = userEvent.setup();
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages />);
    expect(screen.getByText(/Sorting and search cover this page only\./)).toBeTruthy();
    await user.type(screen.getByRole('searchbox', { name: 'Search vendor or product' }), 'cabinet');
    expect(vendorsInOrder()).toEqual(['Untitled document set']);
    expect(screen.getByText(/1 of 3 reviews match\./)).toBeTruthy();
    await user.clear(screen.getByRole('searchbox'));
    await user.type(screen.getByRole('searchbox'), 'nothing like this');
    expect(screen.getByText('Nothing matches “nothing like this”.')).toBeTruthy();
  });

  it('opens a review by its Open button or its name', async () => {
    const user = userEvent.setup();
    const onOpen = vi.fn();
    render(<DocumentsTable rows={ROWS} onOpen={onOpen} morePages={false} />);
    await user.click(screen.getByRole('button', { name: 'Open review for Beta Stone' }));
    expect(onOpen).toHaveBeenCalledWith('b');
    // The name opens it too (the Open column is hidden on a phone).
    await user.click(screen.getByRole('button', { name: /^Untitled document set/ }));
    expect(onOpen).toHaveBeenLastCalledWith('u');
  });

  it('keeps the "counts checks, not drawings" caveat behind the Results "?"', async () => {
    const user = userEvent.setup();
    render(<DocumentsTable rows={ROWS} onOpen={() => {}} morePages={false} />);
    await user.click(screen.getByRole('button', { name: 'About these results' }));
    expect(await screen.findByText(/not drawings or individual dimensions/)).toBeTruthy();
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
    render(<PackagesPage onOpenReview={() => {}} onNewPackage={() => {}} />);
    await screen.findByRole('table', { name: 'Drawing reviews' });
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
    render(<PackagesPage onOpenReview={() => {}} onNewPackage={() => {}} />);
    await screen.findByText(/Sorting and search cover this page only/);
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    await waitFor(() => expect(vendorsInOrder()).toEqual(['Untitled document set']));
    expect(requests.at(-1)).toContain('cursor=opaque%2Bcursor');
    expect(screen.getByText(/on this page/).textContent).toBe('Page 2 · 1 on this page');
  });

  it('says a failed load out loud, with a retry', async () => {
    pages = { first: 503 };
    render(<PackagesPage onOpenReview={() => {}} onNewPackage={() => {}} />);
    expect(await screen.findByText('Documents could not be loaded')).toBeTruthy();
    expect(screen.queryByRole('table')).toBeNull();
  });
});
