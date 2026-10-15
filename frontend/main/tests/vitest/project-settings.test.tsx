// @vitest-environment jsdom
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { CompanySettingsPage } from '@/pages/CompanySettingsPage';

// Company settings → Projects (#1341). Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p-configured' }));

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

const COMPANY = { version: 1, settings: [] };
const CHECK = (id: string) => ({ id, name: `Synthetic check ${id}` });
const COMPANY_SOURCE = { value: 'Company standard', guidance: "GV's own standard for this job." };
const CLIENT_SOURCE = { value: 'G.C / Client', guidance: 'From the architect.' };

function setting(name: string, label: string, extra: Record<string, unknown> = {}) {
  return {
    name, label, purpose: `What ${label.toLowerCase()} is for.`, checks: [CHECK('CHECK-A')], applies: true,
    cabinet_width_limit: false, sources: [CLIENT_SOURCE], rulebook_default: null, company_value: null,
    project_value: null, in_use: null, in_use_from: null, ...extra,
  };
}

const PROJECT = {
  id: 'p-1',
  name: 'Synthetic job',
  product_types: ['countertop'],
  version: 2,
  sink: null,
  settings: [
    setting('countertop_overhang', 'Countertop overhang', {
      in_use: '1 in', in_use_from: 'project',
      project_value: { text: '1 in', set_by: 'Synthetic Reviewer', set_at: '2026-10-01T10:00:00Z', source: 'G.C / Client', reference: null },
    }),
    setting('sink_cutout_clearance', 'Sink cut-out clearance', {
      in_use: '1/4 in', in_use_from: 'rulebook', rulebook_default: '1/4 in',
      sources: [COMPANY_SOURCE, { value: 'Fabricator', guidance: 'Set by the stone fabricator.' }],
    }),
    setting('cabinet_depth', 'Cabinet depth', { in_use: '24 in', in_use_from: 'company', company_value: '24 in' }),
    setting('drawer_cab_width_max', 'Drawer cabinet width, maximum', {
      applies: false, cabinet_width_limit: true, sources: [COMPANY_SOURCE, CLIENT_SOURCE],
    }),
  ],
};

async function openPage() {
  routes['GET /company-settings'] = () => json(COMPANY);
  routes['GET /company-settings/projects'] = () => json({ projects: [PROJECT] });
  render(<CompanySettingsPage />);
  return screen.findByRole('region', { name: 'Synthetic job' });
}

describe('Company settings → Projects', () => {
  it('lists each project with what every setting is for, its checks, and where the value in use comes from', async () => {
    const project = await openPage();
    expect(screen.getByRole('heading', { name: 'Projects' })).toBeTruthy();
    const table = within(project).getByRole('table', { name: 'Synthetic job settings' });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-source'))).toEqual(['project', 'rulebook', 'company']);
    expect(rows[0].textContent).toContain('What countertop overhang is for.');
    expect(rows[0].textContent).toContain('Synthetic check CHECK-A');
    expect(rows[0].textContent).toContain('This project');
    expect(rows[1].textContent).toContain("GV's checklist default");
    expect(rows[2].textContent).toContain('GV standard');
    // Reviewers can edit project values; GV's standards stay admin-only, and the page says which is which.
    expect(project.textContent).toContain('Reviewers and admins in this project can change these.');
    expect(project.textContent).toContain('No sink entered for this job yet.');
  });

  it('keeps settings no drawing set of this job is checked with apart, still settable', async () => {
    const project = await openPage();
    const apart = within(project).getByText(/Not used by this project's drawing sets \(1\)/);
    expect(apart).toBeTruthy();
    expect(within(project).getByRole('textbox', { name: 'New value for Drawer cabinet width, maximum' })).toBeTruthy();
  });

  it('saves only the typed values, with the source the person picked where there are several', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    routes['POST /company-settings/projects/p-1'] = () => json(PROJECT, 201);
    await user.type(within(project).getByRole('textbox', { name: 'New value for Sink cut-out clearance' }), ' 1/8" ');
    await user.selectOptions(within(project).getByRole('combobox', { name: 'Where Sink cut-out clearance comes from' }), 'Fabricator');
    await user.type(within(project).getByRole('textbox', { name: 'New value for Countertop overhang' }), '1 1/2"');
    await user.click(within(project).getByRole('button', { name: 'Save 2 changes' }));
    await within(project).findByText('Saved. Run the checks again to use these values.');
    expect(requests.filter((r) => r.method === 'POST').map((r) => [r.url, JSON.parse(r.body ?? '{}')])).toEqual([
      ['/company-settings/projects/p-1', { values: [
        { name: 'countertop_overhang', value: '1 1/2"' },
        { name: 'sink_cutout_clearance', value: '1/8"', source: 'Fabricator' },
      ] }],
    ]);
  });

  it('asks for the source before saving a value that has several honest ones', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    await user.type(within(project).getByRole('textbox', { name: 'New value for Sink cut-out clearance' }), '1/8"');
    expect(within(project).getByRole('button', { name: 'Save 1 change' }).hasAttribute('disabled')).toBe(true);
    expect(project.textContent).toContain('Say where Sink cut-out clearance comes from to save.');
  });

  it('says plainly who may change a project value when the server refuses', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    routes['POST /company-settings/projects/p-1'] = () => json({ error: 'http_error', message: 'Not found', request_id: 'r' }, 404);
    await user.type(within(project).getByRole('textbox', { name: 'New value for Countertop overhang' }), '1"');
    await user.click(within(project).getByRole('button', { name: 'Save 1 change' }));
    // Said as a status (#1369), so a screen reader hears it without it shouting over the page.
    expect((await within(project).findByText('Only a reviewer or an admin in this project can change its values.')).getAttribute('role')).toBe('status');
  });

  it('enters the sink for the project shown, not the configured one', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    routes['POST /projects/p-1/sink'] = () => json({ sink: { model_name: 'Synthetic sink', source_note: null, set_by: 'Synthetic Reviewer', set_at: '2026-10-01T10:00:00Z', inside_width: { text: '28"', set_by: 'x', set_at: '2026-10-01T10:00:00Z' }, inside_depth: { text: '16"', set_by: 'x', set_at: '2026-10-01T10:00:00Z' } } }, 201);
    await user.click(within(project).getByRole('button', { name: 'Enter the sink' }));
    await user.type(within(project).getByLabelText('Sink name or model'), 'Synthetic sink');
    await user.type(within(project).getByLabelText(/Inside width/), '28"');
    await user.type(within(project).getByLabelText(/Inside depth/), '16"');
    await user.click(within(project).getByRole('button', { name: 'Save the sink' }));
    await within(project).findByText('Synthetic sink');
    expect(requests.filter((r) => r.method === 'POST').map((r) => r.url)).toEqual(['/projects/p-1/sink']);
  });
});
