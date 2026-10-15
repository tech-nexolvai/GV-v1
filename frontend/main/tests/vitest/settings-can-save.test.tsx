// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CompanySettingsPage } from '@/pages/CompanySettingsPage';

// Save controls only for people who may save (#1369 item 1). The server says so with `can_save`, from
// the same checks its save routes make; the page only stops offering boxes it would refuse.
// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p-configured' }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let routes: Record<string, () => Response> = {};
beforeEach(() => {
  routes = {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input).replace(/^.*\/api\/v1/, '');
    const route = routes[`${init?.method ?? 'GET'} ${url}`];
    return route ? route() : json({ error: 'http_error', message: 'not in this test', request_id: 'r' }, 404);
  }));
});

const company = (canSave: boolean) => ({
  version: 1,
  can_save: canSave,
  settings: [
    { name: 'filler_min', scope: 'global', rule_ids: ['CHECK-FILLER'], rulebook_default: '1 in', rulebook_note: null, company_value: null, company_set_by: null, company_set_at: null, in_use: '1 in', in_use_from: 'rulebook', kind: 'standard' },
  ],
});

const setting = (name: string, label: string, extra: Record<string, unknown> = {}) => ({
  name, label, purpose: null, checks: [{ id: 'CHECK-A', name: 'Synthetic check A' }], applies: true, cabinet_width_limit: false,
  sources: [{ value: 'G.C / Client', guidance: 'From the architect.' }], rulebook_default: null, company_value: null,
  project_value: null, in_use: '1 in', in_use_from: 'company', ...extra,
});

const project = (canSave: boolean) => ({
  id: 'p-1',
  name: 'Synthetic job',
  product_types: ['countertop'],
  version: 1,
  sink: null,
  can_save: canSave,
  settings: [
    setting('countertop_overhang', 'Countertop overhang'),
    setting('drawer_cab_width_max', 'Drawer cabinet width, maximum', { applies: false, cabinet_width_limit: true, in_use: null, in_use_from: null }),
  ],
});

async function openAs(companySave: boolean, projectSave: boolean, focus?: string) {
  routes['GET /company-settings'] = () => json(company(companySave));
  routes['GET /company-settings/projects'] = () => json({ projects: [project(projectSave)] });
  render(<CompanySettingsPage focus={focus} />);
  const standards = await screen.findByRole('table', { name: /standard numbers/ });
  const job = await screen.findByRole('region', { name: 'Synthetic job' });
  return { standards: standards.closest('section') as HTMLElement, job };
}

const ADMIN_ONLY = 'Only an admin can change GV standards.';
const REVIEWERS_ONLY = 'Only reviewers on this project can change these.';

describe('save controls follow can_save (#1369 item 1)', () => {
  it('an admin gets boxes and Save for GV standards and for the project', async () => {
    const { standards, job } = await openAs(true, true);
    expect(within(standards).getAllByRole('textbox')).toHaveLength(1);
    expect(within(job).getAllByRole('textbox', { hidden: true })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: 'No changes to save' })).toHaveLength(2);
    expect(within(job).getByRole('button', { name: 'Enter the sink' })).toBeTruthy();
    expect(screen.queryByText(ADMIN_ONLY)).toBeNull();
    expect(screen.queryByText(REVIEWERS_ONLY)).toBeNull();
  });

  it('a reviewer sees GV standards read-only, and edits the project', async () => {
    const { standards, job } = await openAs(false, true);
    expect(within(standards).queryAllByRole('textbox')).toHaveLength(0);
    expect(within(standards).getByRole('columnheader', { name: 'In use' })).toBeTruthy();
    expect(screen.getByText(ADMIN_ONLY)).toBeTruthy();
    expect(standards.textContent).toContain('1 in');
    // One Save left, the project's.
    expect(screen.getAllByRole('button', { name: 'No changes to save' }).map((button) => job.contains(button))).toEqual([true]);
    expect(within(job).getAllByRole('textbox', { hidden: true })).toHaveLength(2);
    expect(screen.queryByText(REVIEWERS_ONLY)).toBeNull();
  });

  it('a viewer sees every value with no boxes, no Save and no sink button, and one plain line each', async () => {
    const { standards, job } = await openAs(false, false);
    expect(screen.queryAllByRole('textbox', { hidden: true })).toHaveLength(0);
    expect(screen.queryByRole('combobox', { hidden: true })).toBeNull();
    expect(screen.queryByRole('button', { name: /save/i })).toBeNull();
    expect(within(job).queryByRole('button', { name: /sink/i })).toBeNull();
    expect(within(job).queryByRole('columnheader', { name: 'New value' })).toBeNull();
    expect(screen.getByText(ADMIN_ONLY)).toBeTruthy();
    expect(within(job).getByText(REVIEWERS_ONLY)).toBeTruthy();
    expect(job.textContent).not.toContain('can change these. ');
    expect(job.textContent).toContain('Countertop overhang');
    expect(job.textContent).toContain('1 in');
    expect(standards.textContent).toContain('Filler min');
  });

  it('a link to a setting still lands on its row when the page is read-only', async () => {
    await openAs(false, false, 'countertop_overhang');
    await waitFor(() => expect(document.activeElement?.getAttribute('data-setting-field')).toBe('countertop_overhang'));
    expect(document.activeElement?.closest('[data-project]')?.getAttribute('data-project')).toBe('p-1');
    expect(screen.queryByText(/is not listed here/)).toBeNull();
  });
});
