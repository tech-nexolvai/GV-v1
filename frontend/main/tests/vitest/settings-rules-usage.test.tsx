// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { PackageSummary, Rule, UsageGroup } from '@/api/client';
import { CompanySettingsPage } from '@/pages/CompanySettingsPage';
import { RulebookPage } from '@/pages/RulebookPage';
import { UsagePage } from '@/pages/UsagePage';
import { checkTypeWord, matchesRuleFilter, ruleGrid, severityCounts, sharedReleaseNote } from '@/lib/rulebook-overview';
import { costByDay, formatUsd, modelWord, outcomesByUploadDay, usageBySet, usageKpis } from '@/lib/usage';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const requests: { method: string; url: string; body?: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let routes: Record<string, () => Response> = {};
beforeEach(() => {
  requests.length = 0;
  routes = {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '');
    const method = init?.method ?? 'GET';
    requests.push({ method, url, body: typeof init?.body === 'string' ? init.body : undefined });
    const route = routes[`${method} ${url}`];
    return route ? route() : json({ error: 'http_error', message: `not in this test: ${method} ${url}`, request_id: 'r' }, 404);
  }));
});

// ---------------------------------------------------------------------------------------------
// Company settings
// ---------------------------------------------------------------------------------------------

const SETTINGS = {
  version: 3,
  settings: [
    { name: 'filler_min', scope: 'global', rule_ids: ['CHECK-FILLER'], rulebook_default: '1 in', rulebook_note: 'Synthetic note on the default.', company_value: null, company_set_by: null, company_set_at: null, in_use: '1 in', in_use_from: 'rulebook' },
    { name: 'sink_clearance', scope: 'project', rule_ids: ['CHECK-SINK-A', 'CHECK-SINK-B'], rulebook_default: '1/4 in', rulebook_note: null, company_value: '3/8 in', company_set_by: 'Synthetic Admin', company_set_at: '2026-10-01T10:00:00Z', in_use: '3/8 in', in_use_from: 'company' },
    { name: 'back_offset', scope: 'global', rule_ids: ['CHECK-BACK'], rulebook_default: null, rulebook_note: null, company_value: null, company_set_by: null, company_set_at: null, in_use: null, in_use_from: null },
  ],
};

describe('Company settings', () => {
  it('is one table with a meter, where each value comes from, and who may change it', async () => {
    routes['GET /company-settings'] = () => json(SETTINGS);
    render(<CompanySettingsPage />);
    const table = await screen.findByRole('table', { name: "GV's standard numbers" });
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'meter').textContent).toContain('2 of 3 have a value · 1 set by GV · 1 not set yet');
    expect(screen.getByRole('progressbar', { name: '2 of 3 have a value' }).getAttribute('aria-valuenow')).toBe(String((2 / 3) * 100));
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-source'))).toEqual(['rulebook', 'company', 'none']);
    expect(within(rows[0]).getByText('Synthetic note on the default.')).toBeTruthy();
    expect(within(rows[1]).getByText('Can use its own')).toBeTruthy();
    expect(rows[1].textContent).toContain('by Synthetic Admin on');
    expect(within(rows[1]).getByText('1/4 in')).toBeTruthy(); // the rulebook default it replaced
    expect(within(rows[2]).getByText('Not set — checks that need it say "not found" until it is.')).toBeTruthy();
  });

  it('saves only the typed values, trimmed, through the same request', async () => {
    const user = userEvent.setup();
    routes['GET /company-settings'] = () => json(SETTINGS);
    routes['POST /company-settings'] = () => json({ ...SETTINGS, settings: SETTINGS.settings.map((s) => s.name === 'back_offset' ? { ...s, company_value: '2 1/2 in', in_use: '2 1/2 in', in_use_from: 'company', company_set_by: 'Synthetic Admin' } : s) }, 201);
    render(<CompanySettingsPage />);
    await screen.findByRole('table');
    expect(screen.getByRole('button', { name: 'No changes to save' }).hasAttribute('disabled')).toBe(true);
    await user.type(screen.getByRole('textbox', { name: 'Back offset' }), '  2 1/2"  ');
    await user.click(screen.getByRole('button', { name: 'Save 1 change' }));
    await screen.findByText('Saved. Every project now starts from these numbers.');
    expect(requests.filter((r) => r.method === 'POST').map((r) => JSON.parse(r.body ?? '{}'))).toEqual([{ values: [{ name: 'back_offset', value: '2 1/2"' }] }]);
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'meter').textContent).toContain('3 of 3 have a value · 2 set by GV');
  });

  it('says plainly that only an admin can save', async () => {
    const user = userEvent.setup();
    routes['GET /company-settings'] = () => json(SETTINGS);
    routes['POST /company-settings'] = () => json({ error: 'http_error', message: 'Not found', request_id: 'r' }, 404);
    render(<CompanySettingsPage />);
    await screen.findByRole('table');
    await user.type(screen.getByRole('textbox', { name: 'Filler min' }), '1"');
    await user.click(screen.getByRole('button', { name: 'Save 1 change' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Only an admin can change company settings.');
  });
});

// ---------------------------------------------------------------------------------------------
// Rulebook
// ---------------------------------------------------------------------------------------------

const SHARED_NOTE = 'Releasable: every tolerance this rule uses is a client-confirmed value.';
function rule(id: string, extra: Partial<Rule> = {}): Rule {
  return {
    rule_id: id, name: `Synthetic check ${id}`, version: '1', snapshot_id: `${id.toLowerCase()}${'0'.repeat(56)}`.slice(0, 64),
    product_type: 'countertop', check_type: 'internal', severity: 'FLAG', production_ready: true, unconfirmed_tolerances: 0,
    release_note: SHARED_NOTE, published_versions: 1, ...extra,
  };
}
const RULES: Rule[] = [
  rule('CT-A'), rule('CT-B'), rule('CT-C', { check_type: 'arch_vs_shop' }), rule('CT-D', { check_type: 'global' }),
  rule('CAB-A', { product_type: 'cabinet' }),
  rule('CAB-B', { product_type: 'cabinet', check_type: 'arch_vs_shop', production_ready: false, unconfirmed_tolerances: 1, release_note: 'Blocked: 1 tolerance awaits the client.' }),
];

describe('rulebook arithmetic', () => {
  it('counts products × check types from the rules themselves', () => {
    const grid = ruleGrid(RULES);
    expect(grid.products).toEqual(['cabinet', 'countertop']);
    expect(grid.checkTypes).toEqual(['internal', 'arch_vs_shop', 'global']);
    expect(grid.count('countertop', 'internal')).toBe(2);
    expect(grid.count('cabinet', 'global')).toBe(0);
    expect(grid.byProduct('countertop')).toBe(4);
    expect(grid.byCheckType('arch_vs_shop')).toBe(2);
    expect(grid.total).toBe(6);
    expect(checkTypeWord('arch_vs_shop')).toBe('Architect vs shop');
    expect(checkTypeWord('new_kind')).toBe('New kind');
  });

  it('says the severity and a shared note once', () => {
    expect(severityCounts(RULES)).toEqual([['FLAG', 6]]);
    expect(sharedReleaseNote(RULES.slice(0, 5))).toBe(SHARED_NOTE);
    expect(sharedReleaseNote(RULES)).toBeNull();
    expect(matchesRuleFilter(RULES[2], { product: 'countertop', checkType: 'arch_vs_shop' })).toBe(true);
    expect(matchesRuleFilter(RULES[2], { product: 'countertop', checkType: null })).toBe(true);
    expect(matchesRuleFilter(RULES[4], { product: 'countertop', checkType: null })).toBe(false);
  });
});

describe('Rulebook page', () => {
  it('shows the grid, the severity and the release note once, and filters from a cell', async () => {
    const user = userEvent.setup();
    const rules = RULES.slice(0, 5); // all share one note
    routes['GET /rules'] = () => json(rules);
    render(<RulebookPage />);
    const grid = await screen.findByRole('table', { name: /What the rulebook checks/ });
    expect(within(grid).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Product', 'Shop drawing only', 'Architect vs shop', 'Against a standard', 'All']);
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'severity').textContent).toBe('Every rule has severity FLAG. There is no severity split yet.');
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'releasable').textContent).toBe('5 of 5 releasable: every tolerance this rule uses is a client-confirmed value.');
    expect(document.querySelectorAll('[data-part="release-note"]')).toHaveLength(0); // not repeated per rule
    expect(document.body.textContent?.match(/Rules are authored in YAML/g) ?? []).toHaveLength(0); // behind "?", once

    const table = screen.getByRole('table', { name: 'Published rules' });
    expect(within(table).getAllByRole('row')).toHaveLength(6);
    await user.click(within(grid).getByRole('button', { name: '1 countertop rule, architect vs shop' }));
    expect(screen.getByRole('status').textContent).toContain('Showing countertop · architect vs shop: 1');
    expect(within(screen.getByRole('table', { name: 'Published rules' })).getAllByRole('row')).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: 'Show all rules' }));
    expect(within(screen.getByRole('table', { name: 'Published rules' })).getAllByRole('row')).toHaveLength(6);
  });

  it('shows a rule that is not releasable, with its own note, and searches', async () => {
    const user = userEvent.setup();
    routes['GET /rules'] = () => json(RULES);
    render(<RulebookPage />);
    const table = await screen.findByRole('table', { name: 'Published rules' });
    const blocked = within(table).getByText('CAB-B').closest('tr')!;
    expect(within(blocked).getByText(/unconfirmed tolerance/).textContent).toBe('1 unconfirmed tolerance');
    expect(within(blocked).getByText('Blocked: 1 tolerance awaits the client.')).toBeTruthy();
    expect(screen.getByText((_, el) => el?.getAttribute('data-part') === 'releasable').textContent).toBe('5 of 6 releasable.');
    await user.type(screen.getByRole('textbox', { name: 'Search rules' }), 'CAB');
    expect(within(table).getAllByRole('row')).toHaveLength(3);
  });

  it('says when nothing is published, and a failure is not "no rules"', async () => {
    routes['GET /rules'] = () => json([]);
    const { unmount } = render(<RulebookPage />);
    expect(await screen.findByText('No rules are published')).toBeTruthy();
    unmount();
    routes['GET /rules'] = () => json({ error: 'http_error', message: 'synthetic outage', request_id: 'r' }, 503);
    render(<RulebookPage />);
    expect(await screen.findByText('The rulebook could not be loaded')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------------------------
// Usage
// ---------------------------------------------------------------------------------------------

const totals = (calls: number, cost: string, extra: Partial<UsageGroup> = {}) => ({ calls, failed_calls: 0, input_tokens: calls * 100, output_tokens: calls * 10, cost_usd: cost, unpriced_calls: 0, ...extra });
const BY_DAY = {
  from: null, to: null, group_by: 'day', package_reading_times: [],
  totals: totals(150, '1.717358', { failed_calls: 10, unpriced_calls: 2 }),
  groups: [
    { ...totals(140, '1.700000'), day: '2026-10-08', package_id: null, models: [{ ...totals(70, '1.0'), model: 'anthropic.claude-opus-5-5' }] },
    { ...totals(10, '0.017358', { failed_calls: 10 }), day: '2026-10-09', package_id: null, models: [] },
  ],
};
const BY_SET = {
  ...BY_DAY, group_by: 'package',
  groups: [
    { ...totals(140, '1.700000'), day: null, package_id: 'set-a', models: [{ ...totals(70, '1.0'), model: 'anthropic.claude-opus-5-5' }, { ...totals(70, '0.7'), model: 'anthropic.claude-sonnet-5-5' }] },
    { ...totals(10, '0.017358', { failed_calls: 10 }), day: null, package_id: 'set-gone', models: [{ ...totals(10, '0.017358'), model: 'amazon.nova-lite-v1:0' }] },
  ],
};
const zero = { pass: 0, fail: 0, review: 0, not_found: 0, no_rule: 0 };
const summary = (id: string, created: string, outcomes = zero, vendor: string | null = `Synthetic ${id}`): PackageSummary => ({
  package_id: id, revision_id: `rev-${id}`, revision_number: 1, vendor, product_type: 'countertop', state: 'AWAITING_REVIEW',
  created_at: created, updated_at: created, outcomes, needs_decision: 0, approved: false, signed_exports_ready: false,
});
const SUMMARY = {
  items: [
    summary('set-a', '2026-10-08T09:00:00Z', { ...zero, pass: 1, fail: 1, review: 8 }),
    summary('set-b', '2026-10-08T23:30:00Z', { ...zero, not_found: 6 }),
    summary('set-c', '2026-10-09T08:00:00Z'),
  ],
  next_cursor: null, limit: 200,
};

describe('usage arithmetic', () => {
  it('formats money honestly', () => {
    expect(formatUsd('1.717358')).toBe('$1.72');
    expect(formatUsd('0.004')).toBe('< $0.01');
    expect(formatUsd('0.000000')).toBe('$0.00');
  });

  it('counts sets read, calls and cost, and keeps unpriced calls visible', () => {
    expect(usageKpis(BY_SET.totals, BY_SET.groups as UsageGroup[])).toEqual({ setsRead: 2, calls: 150, failedCalls: 10, cost: '1.717358', unpricedCalls: 2 });
  });

  it('orders days, names sets, and adds recorded results by upload day', () => {
    expect(costByDay(BY_DAY.groups as UsageGroup[]).map((d) => [d.day, d.cost])).toEqual([['2026-10-08', 1.7], ['2026-10-09', 0.017358]]);
    expect(usageBySet(BY_SET.groups as UsageGroup[], SUMMARY.items).map((s) => s.vendor)).toEqual(['Synthetic set-a', null]);
    expect(outcomesByUploadDay(SUMMARY.items).map((d) => [d.day, d.sets, d.pass, d.review, d.not_found])).toEqual([
      ['2026-10-08', 2, 1, 8, 6],
      ['2026-10-09', 1, 0, 0, 0],
    ]);
    expect(modelWord('anthropic.claude-opus-5-5')).toBe('claude-opus-5-5');
    expect(modelWord('amazon.nova-lite-v1:0')).toBe('nova-lite-v1');
  });
});

describe('Usage page', () => {
  function serve() {
    routes['GET /projects/p/usage?group_by=day'] = () => json(BY_DAY);
    routes['GET /projects/p/usage?group_by=package'] = () => json(BY_SET);
    routes['GET /projects/p/packages-summary?limit=200'] = () => json(SUMMARY);
  }

  it('reads the usage API: KPIs, reading time not measured, cost by set, and the charts', async () => {
    serve();
    render(<UsagePage />);
    const kpis = await screen.findByText((_, el) => el?.getAttribute('data-part') === 'kpis');
    const cards = within(kpis).getAllByText((_, el) => el?.getAttribute('data-part') === 'kpi').map((card) => card.textContent);
    expect(cards[0]).toBe('Sets read2drawing sets with AI calls');
    expect(cards[1]).toBe('AI calls15010 failed');
    expect(cards[2]).toBe('Cost$1.722 calls have no price, so the real cost is higher');
    expect(cards[3]).toContain('Not measured yet');
    expect(cards.join(' ')).not.toMatch(/\d+ ?(ms|min)/); // never a reading-time number

    const bySet = screen.getByRole('table', { name: 'AI usage by drawing set' });
    const rows = within(bySet).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('Synthetic set-a');
    expect(rows[0].textContent).toContain('$1.70');
    expect(rows[1].textContent).toContain('Not on the loaded list');
    expect(rows[1].textContent).toContain('10 failed');

    await waitFor(() => expect(document.querySelector('[data-slot="cost-chart"]')).toBeTruthy(), { timeout: 10_000 });
    await waitFor(() => expect(document.querySelector('[data-slot="outcomes-chart"]')).toBeTruthy(), { timeout: 10_000 });
    expect(screen.getByRole('list', { name: 'Result shapes' })).toBeTruthy();

    // Three reads, no per-review requests (the old page asked each review's findings).
    expect(requests.map((r) => r.url).sort()).toEqual(['/projects/p/packages-summary?limit=200', '/projects/p/usage?group_by=day', '/projects/p/usage?group_by=package']);
  });

  it('says when there are no AI calls, without a chart of zeroes', async () => {
    routes['GET /projects/p/usage?group_by=day'] = () => json({ ...BY_DAY, totals: totals(0, '0.000000'), groups: [] });
    routes['GET /projects/p/usage?group_by=package'] = () => json({ ...BY_SET, totals: totals(0, '0.000000'), groups: [] });
    routes['GET /projects/p/packages-summary?limit=200'] = () => json({ items: [], next_cursor: null, limit: 200 });
    render(<UsagePage />);
    expect(await screen.findByText('No AI calls are recorded in this project yet.')).toBeTruthy();
    expect(screen.getByText('No drawing sets yet.')).toBeTruthy();
    expect(screen.queryByRole('table', { name: 'AI usage by drawing set' })).toBeNull();
  });

  it('a failure is not zero usage', async () => {
    serve();
    routes['GET /projects/p/usage?group_by=day'] = () => json({ error: 'http_error', message: 'synthetic outage', request_id: 'r' }, 503);
    render(<UsagePage />);
    expect(await screen.findByText('Usage could not be loaded')).toBeTruthy();
    expect(screen.getByText(/this is not a report of zero usage/)).toBeTruthy();
  });
});
