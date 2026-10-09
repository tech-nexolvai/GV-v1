// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { PackageSummary } from '@/api/client';
import type { Page } from '@/app/route';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let routes: Record<string, () => Response> = {};
beforeEach(() => {
  routes = {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '').replace(/\?.*$/, '');
    const route = routes[`${init?.method ?? 'GET'} ${url}`];
    return route ? route() : json({ error: 'http_error', message: `not in this test: ${url}`, request_id: 'r' }, 404);
  }));
});

const { default: App } = await import('@/App');
const { AppShell } = await import('@/components/shell/AppShell');
const { RulebookPage } = await import('@/pages/RulebookPage');
const { WelcomePage } = await import('@/pages/WelcomePage');
const { NewReviewForm } = await import('@/components/upload/NewReviewForm');
const { DocumentsTable } = await import('@/components/documents/documents-table');

function Shell({ page, packageId = null, title, children }: { page: Page; packageId?: string | null; title: string; children: ReactNode }) {
  return (
    <AppShell
      title={title}
      crumbs={[{ label: title }]}
      activePage={page}
      activePackage={packageId}
      theme="light"
      sidebarRefreshKey={0}
      liveNeedYou={null}
      onNavigate={() => {}}
      onOpenPackage={() => {}}
      onNewReview={() => {}}
      onToggleTheme={() => {}}
    >
      {children}
    </AppShell>
  );
}

describe('one page header (#1125)', () => {
  // The real app at each address, so the h1, the breadcrumb and the tab title all come from App.
  const pages: [string, string][] = [
    ['#/documents', 'Documents'],
    ['#/rulebook', 'Rulebook'],
    ['#/settings', 'Company settings'],
    ['#/usage', 'Usage'],
    ['#/', 'Start a review'],
  ];

  it.each(pages)('%s has exactly one h1, and the breadcrumb and tab title agree with it', async (hash, title) => {
    window.location.hash = hash;
    render(<App />);
    const heading = await screen.findByRole('heading', { level: 1, name: title });
    expect(screen.getAllByRole('heading', { level: 1 })).toEqual([heading]);
    // The top bar still says where you are, as a breadcrumb rather than a second heading.
    expect(within(screen.getByRole('navigation', { name: 'breadcrumb' })).getByText(title)).toBeTruthy();
    expect(document.title).toBe(`${title} · GV Review`);
  });

  it('on an open review the top bar heading is the one h1, and a panel inside it is an h2', () => {
    render(<Shell page="review" packageId="pkg" title="Synthetic vendor"><h2>Synthetic panel</h2></Shell>);
    expect(screen.getAllByRole('heading', { level: 1 }).map((h) => h.textContent)).toEqual(['Synthetic vendor']);
    expect(screen.getByRole('heading', { level: 2, name: 'Synthetic panel' })).toBeTruthy();
  });

  it('a recent review is read out whole: vendor, revision, date and status', async () => {
    routes['GET /projects/p/packages'] = () => json({
      items: [{ id: 'pkg', vendor: 'Synthetic vendor', state: 'AWAITING_REVIEW', created_at: '2026-10-01T10:00:00Z', current_revision_number: 2, current_revision_id: 'r', project_id: 'p', product_type: 'countertop' }],
      next_cursor: null,
    });
    const onOpenReview = vi.fn();
    render(<WelcomePage onCreated={() => {}} onOpenReview={onOpenReview} />);
    // No aria-label: the name is the row's own text, so the status and date are not hidden.
    const recent = await screen.findByRole('button', { name: /^Synthetic vendor\s*Revision 2 · \S+.*Awaiting Review$/ });
    await userEvent.setup().click(recent);
    expect(onOpenReview).toHaveBeenCalledWith('pkg');
  });

  it('a failed load is an alert with a retry button, never an empty page', async () => {
    render(<RulebookPage />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByRole('heading', { name: 'The rulebook could not be loaded' })).toBeTruthy();
    expect(within(alert).getByRole('button', { name: 'Try again' })).toBeTruthy();
  });
});

describe('Start a review (#1125)', () => {
  const pdf = (name: string) => new window.File(['synthetic bytes'], name, { type: 'application/pdf' });

  async function setUp() {
    const user = userEvent.setup();
    const { container } = render(<NewReviewForm onCreated={() => {}} />);
    const [architect, shop] = Array.from(container.querySelectorAll<HTMLInputElement>('input[type="file"]'));
    const start = screen.getByRole('button', { name: 'Start review' });
    return { user, architect, shop, start };
  }

  it('keeps Start review disabled, saying what is missing, until the vendor and both drawings are set', async () => {
    routes['GET /product-types'] = () => json([{ value: 'countertop', label: 'Countertop', published_checks: 3 }]);
    const { user, architect, shop, start } = await setUp();
    await screen.findByRole('option', { name: 'Countertop' });

    expect(start.hasAttribute('disabled')).toBe(true);
    const reason = document.getElementById(start.getAttribute('aria-describedby') ?? '');
    expect(reason?.textContent).toBe("Add the vendor, the architect's drawings and the shop drawings.");

    await user.type(screen.getByLabelText('Vendor'), 'Synthetic vendor');
    fireEvent.change(architect, { target: { files: [pdf('architect.pdf')] } });
    expect(start.hasAttribute('disabled')).toBe(true);
    expect(reason?.textContent).toBe('Add the shop drawings.');

    fireEvent.change(shop, { target: { files: [pdf('shop.pdf')] } });
    await waitFor(() => expect(start.hasAttribute('disabled')).toBe(false));
    expect(reason?.textContent).toMatch(/^Ready to upload\./);
    // Each drop zone shows the chosen file's name and size.
    expect(screen.getByTitle('architect.pdf')).toBeTruthy();
    expect(screen.getAllByText('· 1 KB')).toHaveLength(2);

    // A vendor of only spaces is no vendor: the rule is unchanged.
    await user.clear(screen.getByLabelText('Vendor'));
    await user.type(screen.getByLabelText('Vendor'), '   ');
    expect(start.hasAttribute('disabled')).toBe(true);
  });

  it('stays disabled when no product can be chosen, even with everything else set', async () => {
    routes['GET /product-types'] = () => json([]);
    const { user, architect, shop, start } = await setUp();
    await screen.findByRole('option', { name: 'No checks are published yet' });
    await user.type(screen.getByLabelText('Vendor'), 'Synthetic vendor');
    fireEvent.change(architect, { target: { files: [pdf('architect.pdf')] } });
    fireEvent.change(shop, { target: { files: [pdf('shop.pdf')] } });
    expect(start.hasAttribute('disabled')).toBe(true);
    expect(document.getElementById(start.getAttribute('aria-describedby') ?? '')?.textContent).toBe('Add what the drawing set is for.');
  });

  it('refuses a file that is not a PDF, naming it', async () => {
    routes['GET /product-types'] = () => json([{ value: 'countertop', label: 'Countertop', published_checks: 3 }]);
    const { architect } = await setUp();
    fireEvent.change(architect, { target: { files: [new window.File(['x'], 'notes.txt', { type: 'text/plain' })] } });
    expect(screen.getByRole('alert').textContent).toBe('notes.txt is not a PDF. Only PDF drawings can be reviewed.');
  });
});

describe('Documents legend (#1125)', () => {
  const row: PackageSummary = {
    package_id: 'a', revision_id: 'rev-a', revision_number: 1, vendor: 'Synthetic a', product_type: 'countertop',
    state: 'AWAITING_REVIEW', created_at: '2026-10-01T10:00:00Z', updated_at: '2026-10-01T10:00:00Z',
    outcomes: { pass: 1, fail: 0, review: 0, not_found: 0, no_rule: 0 }, needs_decision: 0,
    needs_decision_by_outcome: { fail: 0, review: 0, not_found: 0, other: 0 }, approved: false, signed_exports_ready: false,
  };

  it('shows the five result words, each with its glyph, without a click', () => {
    render(<DocumentsTable rows={[row]} onOpen={() => {}} morePages={false} />);
    const legend = screen.getByRole('list', { name: 'Result shapes' });
    const items = within(legend).getAllByRole('listitem');
    expect(items.map((li) => li.textContent)).toEqual([
      'Looks right', 'Needs correction', 'Needs your decision', 'Waiting on a value', 'Not applicable',
    ]);
    for (const item of items) expect(item.querySelector('[data-outcome-icon]')).not.toBeNull();
  });
});
