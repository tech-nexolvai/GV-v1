// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { formatRoute, parseRoute } from '@/app/route';
import { CompanySettingsPage } from '@/pages/CompanySettingsPage';

// Company settings polish (#1369, U5). Synthetic data only: nothing here comes from a client drawing.
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

const COMPANY = {
  version: 1,
  settings: [
    { name: 'filler_min', scope: 'global', rule_ids: ['CHECK-FILLER'], rulebook_default: '1 in', rulebook_note: null, company_value: null, company_set_by: null, company_set_at: null, in_use: '1 in', in_use_from: 'rulebook', kind: 'standard' },
  ],
};
const CLIENT_SOURCE = { value: 'G.C / Client', guidance: 'From the architect.' };

function setting(name: string, label: string, extra: Record<string, unknown> = {}) {
  return {
    name, label, purpose: `What ${label.toLowerCase()} is for.`, checks: [{ id: 'CHECK-A', name: 'Synthetic check A' }], applies: true,
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
      in_use: '1 1/2 in', in_use_from: 'project', company_value: '1 in', rulebook_default: '3/4 in',
      project_value: { text: '1 1/2"', set_by: 'Synthetic Reviewer', set_at: '2026-10-01T10:00:00Z', source: 'G.C / Client', reference: 'Synthetic spec page 3' },
    }),
    setting('cabinet_depth', 'Cabinet depth', { in_use: '24 in', in_use_from: 'company', company_value: '24 in', rulebook_default: '23 in' }),
    setting('drawer_cab_width_max', 'Drawer cabinet width, maximum', { applies: false, cabinet_width_limit: true }),
  ],
};

async function openPage(props: { focus?: string; project?: string } = {}) {
  routes['GET /company-settings'] = () => json(COMPANY);
  routes['GET /company-settings/projects'] = () => json({ projects: [PROJECT] });
  render(<CompanySettingsPage {...props} />);
  return screen.findByRole('region', { name: 'Synthetic job' });
}

describe('the settings link from "Not checked" (#1369 item 3)', () => {
  it('reads #/settings/company?focus=<setting> and writes it back', () => {
    expect(parseRoute('#/settings/company?focus=countertop_overhang')).toEqual({ page: 'settings', packageId: null, focus: 'countertop_overhang' });
    expect(parseRoute('#/settings/company?focus=cabinet_depth&project=p-1')).toEqual({ page: 'settings', packageId: null, focus: 'cabinet_depth', project: 'p-1' });
    expect(parseRoute('#/settings')).toEqual({ page: 'settings', packageId: null });
    expect(formatRoute({ page: 'settings', packageId: null, focus: 'cabinet_depth' })).toBe('#/settings/company?focus=cabinet_depth');
    expect(formatRoute({ page: 'settings', packageId: null })).toBe('#/settings');
    // A query never leaks into a review's id.
    expect(parseRoute('#/review/abc?x=1')).toEqual({ page: 'review', packageId: 'abc' });
  });

  it("focuses the project's field, opening the set-apart list it sits in, and highlights its row", async () => {
    const project = await openPage({ focus: 'drawer_cab_width_max' });
    const box = within(project).getByRole('textbox', { name: 'New value for Drawer cabinet width, maximum' });
    await waitFor(() => expect(document.activeElement).toBe(box));
    expect(box.closest('details')?.open).toBe(true);
    expect(box.closest('tr')?.getAttribute('data-highlight')).toBe('true');
  });

  it("falls back to GV's standard when no project lists the setting", async () => {
    await openPage({ focus: 'filler_min' });
    const box = screen.getByRole('textbox', { name: 'Filler min' });
    await waitFor(() => expect(document.activeElement).toBe(box));
    expect(box.closest('tr')?.getAttribute('data-highlight')).toBe('true');
  });

  it('says so when the linked setting is on no list here, never silently', async () => {
    await openPage({ focus: 'no_such_setting' });
    expect((await screen.findByText(/“No such setting”, is not listed here/)).closest('[role="status"]')).not.toBeNull();
  });
});

describe('Projects: what overrides what, and where it came from (#1369 item 2)', () => {
  it('says which default a value replaces, with who, when, the source and its reference behind "?"', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    const rows = within(within(project).getByRole('table', { name: 'Synthetic job settings' })).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('Replaces GV standard 1 in');
    expect(rows[1].textContent).toContain("Replaces GV's checklist default 23 in");
    // The story is behind "?", not on the row.
    expect(rows[0].textContent).not.toContain('Synthetic Reviewer');
    await user.click(within(rows[0]).getByRole('button', { name: 'Where Countertop overhang comes from' }));
    const tip = await screen.findByRole('dialog');
    expect(tip.textContent).toContain('Set by Synthetic Reviewer on');
    expect(tip.textContent).toContain('From: G.C / Client (Synthetic spec page 3)');
    expect(tip.textContent).toContain('GV standard: 1 in');
    expect(tip.textContent).toContain("GV's checklist default: 3/4 in");
  });

  it('asks for the unit in plain words before sending a bare number', async () => {
    const user = userEvent.setup();
    const project = await openPage();
    const box = within(project).getByRole('textbox', { name: 'New value for Countertop overhang' });
    await user.type(box, '2');
    expect(box.getAttribute('aria-invalid')).toBe('true');
    expect(within(project).getByText('Add the unit, for example 1 1/2" or 38 mm.')).toBeTruthy();
    expect(within(project).getByRole('button', { name: 'Save 1 change' }).hasAttribute('disabled')).toBe(true);
    await user.type(box, '"');
    expect(box.getAttribute('aria-invalid')).toBeNull();
    expect(within(project).getByRole('button', { name: 'Save 1 change' }).hasAttribute('disabled')).toBe(false);
  });

  it("shows the server's own refusal as a status, and a save as a status", async () => {
    const user = userEvent.setup();
    const project = await openPage();
    routes['POST /company-settings/projects/p-1'] = () => json({ error: 'http_error', message: 'Countertop overhang: synthetic refusal from the server.', request_id: 'r' }, 422);
    await user.type(within(project).getByRole('textbox', { name: 'New value for Countertop overhang' }), '9"');
    await user.click(within(project).getByRole('button', { name: 'Save 1 change' }));
    const refused = await within(project).findByText('Countertop overhang: synthetic refusal from the server.');
    expect(refused.closest('[role="status"]')).not.toBeNull();

    routes['POST /company-settings/projects/p-1'] = () => json(PROJECT, 201);
    await user.click(within(project).getByRole('button', { name: 'Save 1 change' }));
    const saved = await within(project).findByText('Saved. Run the checks again to use these values.');
    expect(saved.closest('[role="status"]')).not.toBeNull();
  });
});

describe("GV's standards: the same plain checks (#1369 items 2 and 4)", () => {
  it('asks for the unit before an admin sends a bare number, and says a failed save as a status', async () => {
    const user = userEvent.setup();
    await openPage();
    const box = screen.getByRole('textbox', { name: 'Filler min' });
    await user.type(box, '1');
    expect(box.getAttribute('aria-invalid')).toBe('true');
    expect(screen.getByRole('button', { name: 'Save 1 change' }).hasAttribute('disabled')).toBe(true);
    await user.type(box, '"');
    routes['POST /company-settings'] = () => json({ error: 'http_error', message: 'Not found', request_id: 'r' }, 404);
    await user.click(screen.getByRole('button', { name: 'Save 1 change' }));
    const refused = await screen.findByText('Only an admin can change company settings.');
    expect(refused.closest('[role="status"]')).not.toBeNull();
  });
});
