// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MeasurementPanel } from '@/pages/MeasurementPanel';

// Synthetic data only (#1251): nothing here comes from a client drawing or a client's values.
// GV's rule (2026-10-12): a number drawn on the vendor's or the architect's drawing is confirmed from
// a reading, never typed. The Measure page offers no box for it; settings, the reviewer's own entries
// and the typed lists keep theirs.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const SAVED_AT = '2026-10-01T12:00:00Z';
const saved = (text: string, numerator: string) => ({
  numerator, denominator: '1', unit: 'in', text, set_by: 'synthetic reviewer', set_at: SAVED_AT,
});

const REQUIRED = {
  quantities: [
    { key: 'SHOP:CT010', semantic_type: 'CT010', source: 'SHOP', many: false, confirm_only: true, consumers: [{ rule_id: 'R-DEPTH', input_name: 'countertop_depth' }], categories: [] },
    { key: 'SHOP:CT007', semantic_type: 'CT007', source: 'SHOP', many: false, confirm_only: true, consumers: [{ rule_id: 'R-OFFSET', input_name: 'front_offset' }], categories: [] },
    { key: 'SHOP:CT012', semantic_type: 'CT012', source: 'SHOP', many: false, confirm_only: true, consumers: [{ rule_id: 'R-CUTOUT', input_name: 'cutout_width' }], categories: [] },
    { key: 'SHOP:cabinet_width', semantic_type: 'cabinet_width', source: 'SHOP', many: true, confirm_only: false, consumers: [{ rule_id: 'R-CABS', input_name: 'shop_cabinets' }], categories: [] },
    { key: 'USER_INPUT:field_dimension', semantic_type: 'field_dimension', source: 'USER_INPUT', many: false, confirm_only: false, consumers: [{ rule_id: 'R-FILLER', input_name: 'field_width' }], categories: [] },
  ],
  page_numbers: [1],
  // A reading the reviewer already confirmed: it reaches the checks as a reading, never as a typed value.
  confirmed_readings: [
    { key: 'SHOP:CT012', source: 'SHOP', semantic_type: 'CT012', value: '16 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
  ],
  proposed_readings: [],
  parameters: [],
  discriminators: [],
  rules_published: 5,
  still_reading: false,
  revision_state: 'READY',
  // Typed before #1251: kept (append-only) and still counted, but no longer typable.
  saved_measurements: [
    { key: 'SHOP:CT007', values: [saved('4"', '4')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: true },
  ],
  saved_parameters: [],
};

const calls: { method: string; url: string; body?: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url, body: typeof init?.body === 'string' ? init.body : undefined });
    if (url.includes('/required-inputs')) return json(REQUIRED);
    if (url.endsWith('/company-settings')) return json({ version: 1, settings: [] });
    if (method === 'POST' && url.endsWith('/measurements')) {
      return json({ parameter_set_version: null, measurement_set_version: 2, parameters: [], measurements: [], lists: [] }, 201);
    }
    if (url.includes('/candidates')) return json({ candidates: [], page_number: 1, total: 0 });
    if (url.endsWith('/semantic-types')) return json([]);
    if (url.endsWith('/views')) return json({ views: [] });
    if (url.endsWith('/parts')) return json({ drawings: [] });
    if (url.endsWith('/countertop-runs')) return json({ can_suggest: false, why_not: null, drawings: [] });
    if (url.endsWith('/reading-parts')) return json({ can_suggest: true, why_not: null, drawings: [] });
    if (url.endsWith('/slot-rows')) return json({ rows: [] });
    if (url.includes('/picture')) return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    if (url.endsWith('/pages/pictures')) return json({ queued: false }, 202);
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
  }));
});
afterEach(() => vi.unstubAllGlobals());

const stepButton = (label: RegExp) => within(screen.getByRole('navigation', { name: 'Measurement steps' })).getByRole('button', { name: label });
const box = (key: string) => document.getElementById(`q-${key}`);
const field = (key: string) => document.querySelector<HTMLElement>(`[data-confirm-only][data-key="${key}"]`);
const postsTo = (suffix: string) => calls.filter((call) => call.method === 'POST' && call.url.endsWith(suffix));

async function openValues() {
  const user = userEvent.setup();
  render(<MeasurementPanel packageId="k" />);
  await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 });
  await user.click(stepButton(/Values/));
  await waitFor(() => expect(box('USER_INPUT:field_dimension')).not.toBeNull());
  // Settled fields hide behind "Show only what needs a look"; these tests read them all.
  await user.click(screen.getByLabelText('Show only what needs a look'));
  return user;
}

describe('a number drawn on the drawing is confirmed, never typed (#1251)', { timeout: 15_000 }, () => {
  it('offers no box for a drawn single value, and says plainly what it needs', async () => {
    await openValues();

    for (const key of ['SHOP:CT010', 'SHOP:CT007', 'SHOP:CT012']) expect(box(key)).toBeNull();
    const depth = field('SHOP:CT010')!;
    expect(depth).not.toBeNull();
    expect(depth.textContent).toContain('Needs the section drawing (not supplied)');
    expect(within(depth).queryByRole('textbox')).toBeNull();
  });

  it('keeps the boxes for the reviewer’s own entries and the typed lists', async () => {
    await openValues();

    expect(box('USER_INPUT:field_dimension')?.tagName).toBe('INPUT');
    expect(box('SHOP:cabinet_width')?.tagName).toBe('INPUT');
  });

  it('shows a value typed earlier as still counting, with no box to change it', async () => {
    await openValues();

    const offset = field('SHOP:CT007')!;
    expect(offset.textContent).toContain('4"');
    expect(offset.textContent).toContain('Typed before 2026-10-14');
    expect(offset.textContent).toContain('still counts');
    expect(offset.textContent).not.toContain('Needs the section drawing');
  });

  it('shows a confirmed reading as the value, and Save never sends a drawn value as typed', async () => {
    const user = await openValues();

    const cutout = field('SHOP:CT012')!;
    expect(cutout.textContent).toContain('16 in');
    expect(cutout.textContent).not.toContain('Needs the section drawing');

    await user.type(box('USER_INPUT:field_dimension') as HTMLInputElement, '120"');
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(postsTo('/measurements')).toHaveLength(1));
    expect(JSON.parse(postsTo('/measurements')[0].body!).measurements).toEqual([
      { rule_id: 'R-FILLER', name: 'field_width', value: '120"' },
    ]);
  });
});
