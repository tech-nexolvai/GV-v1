// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { settingDate } from '@/components/settings/companySettings';
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
    { key: 'SHOP:CT010', semantic_type: 'CT010', source: 'SHOP', many: false, confirm_only: true, where_to_find: 'The vendor’s plan view: a synthetic place.', confirmed_pages: [], consumers: [{ rule_id: 'R-DEPTH', input_name: 'countertop_depth' }], categories: [] },
    // Confirmed on another page only: not empty, and the field says which page (#1253 review).
    { key: 'SHOP:CT008', semantic_type: 'CT008', source: 'SHOP', many: false, confirm_only: true, where_to_find: 'The vendor’s drawing.', confirmed_pages: [2], consumers: [{ rule_id: 'R-CUTDEPTH', input_name: 'cutout_depth' }], categories: [] },
    // Two readings confirmed on this page: the field shows neither, and says so.
    { key: 'SHOP:CT004', semantic_type: 'CT004', source: 'SHOP', many: false, confirm_only: true, where_to_find: 'The vendor’s elevation.', confirmed_pages: [1], consumers: [{ rule_id: 'R-SINKCAB', input_name: 'sink_cabinet_width' }], categories: [] },
    { key: 'SHOP:CT007', semantic_type: 'CT007', source: 'SHOP', many: false, confirm_only: true, consumers: [{ rule_id: 'R-OFFSET', input_name: 'front_offset' }], categories: [] },
    { key: 'SHOP:CT012', semantic_type: 'CT012', source: 'SHOP', many: false, confirm_only: true, consumers: [{ rule_id: 'R-CUTOUT', input_name: 'cutout_width' }], categories: [] },
    // Typed before for one of its two checks only (#1262): the field names which check still uses it.
    { key: 'SHOP:CT011', semantic_type: 'CT011', source: 'SHOP', many: false, confirm_only: true, confirmed_pages: [1], consumers: [{ rule_id: 'R-LEFT-A', input_name: 'left_clearance' }, { rule_id: 'R-LEFT-B', input_name: 'left_clearance' }], categories: [] },
    // Typed before for one check, a different number for another, nothing for a third (#1262).
    { key: 'SHOP:CT009', semantic_type: 'CT009', source: 'SHOP', many: false, confirm_only: true, confirmed_pages: [], consumers: [{ rule_id: 'R-BACK-A', input_name: 'back_offset' }, { rule_id: 'R-BACK-B', input_name: 'back_offset' }, { rule_id: 'R-BACK-C', input_name: 'back_offset' }], categories: [] },
    // Confirmed on this page and a different reading on page 2 (#1262): the check sees two and waits.
    { key: 'SHOP:CT013', semantic_type: 'CT013', source: 'SHOP', many: false, confirm_only: true, confirmed_pages: [1, 2], consumers: [{ rule_id: 'R-RIGHT', input_name: 'right_clearance' }], categories: [] },
    { key: 'SHOP:cabinet_width', semantic_type: 'cabinet_width', source: 'SHOP', many: true, confirm_only: false, consumers: [{ rule_id: 'R-CABS', input_name: 'shop_cabinets' }], categories: [] },
    { key: 'USER_INPUT:field_dimension', semantic_type: 'field_dimension', source: 'USER_INPUT', many: false, confirm_only: false, consumers: [{ rule_id: 'R-FILLER', input_name: 'field_width' }], categories: [] },
  ],
  page_numbers: [1, 2],
  // A reading the reviewer already confirmed: it reaches the checks as a reading, never as a typed value.
  confirmed_readings: [
    { key: 'SHOP:CT012', source: 'SHOP', semantic_type: 'CT012', value: '16 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
    { key: 'SHOP:CT004', source: 'SHOP', semantic_type: 'CT004', value: '24 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
    { key: 'SHOP:CT004', source: 'SHOP', semantic_type: 'CT004', value: '25 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
    { key: 'SHOP:CT011', source: 'SHOP', semantic_type: 'CT011', value: '2 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
    { key: 'SHOP:CT013', source: 'SHOP', semantic_type: 'CT013', value: '3 in', page_index: 0, qualification: 'reviewer_confirmed' as const },
  ],
  proposed_readings: [],
  parameters: [],
  discriminators: [],
  rules_published: 5,
  still_reading: false,
  revision_state: 'READY',
  // Typed before #1251: kept (append-only) and still counted, but no longer typable.
  saved_measurements: [
    { key: 'SHOP:CT007', values: [saved('4"', '4')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: true, held_by: ['R-OFFSET'], differs_for: [] },
    { key: 'SHOP:CT011', values: [saved('1"', '1')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: false, held_by: ['R-LEFT-A'], differs_for: [] },
    { key: 'SHOP:CT009', values: [saved('2"', '2')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: false, held_by: ['R-BACK-A'], differs_for: ['R-BACK-B'] },
  ],
  saved_parameters: [],
};

const calls: { method: string; url: string; body?: string }[] = [];
/** The AI readings offered on page 1; none unless a test says so. */
let offered: unknown[] = [];
const READING = { candidate_id: 'c-1', source: 'SHOP', value: '5 in', raw_text: '5', page_index: 0 };
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  calls.length = 0;
  offered = [];
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url, body: typeof init?.body === 'string' ? init.body : undefined });
    if (url.includes('/required-inputs')) return json(REQUIRED);
    if (url.endsWith('/company-settings')) return json({ version: 1, settings: [] });
    if (method === 'POST' && url.endsWith('/measurements')) {
      return json({ parameter_set_version: null, measurement_set_version: 2, parameters: [], measurements: [], lists: [] }, 201);
    }
    if (method === 'POST' && url.endsWith('/confirm')) return json({ semantic_type: 'CT007', status: 'HUMAN_CONFIRMED' }, 201);
    if (url.includes('/candidates')) return json({ candidates: offered, page_number: 1, total: offered.length });
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
    expect(depth.textContent).toContain('No reading confirmed yet');
    expect(depth.textContent).toContain('Drawn on: The vendor’s plan view: a synthetic place.');
    expect(depth.textContent).not.toContain('not supplied');
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
    // The value's own saved day (#1262), never a fixed date.
    expect(offset.textContent).toContain(`Typed ${settingDate(SAVED_AT)}`);
    expect(offset.textContent).not.toContain('2026-10-14');
    expect(offset.textContent).toContain('still counts');
    expect(offset.textContent).not.toContain('No reading confirmed yet');
  });

  it('shows a confirmed reading as the value, and Save never sends a drawn value as typed', async () => {
    const user = await openValues();

    const cutout = field('SHOP:CT012')!;
    expect(cutout.textContent).toContain('16 in');
    expect(cutout.textContent).not.toContain('No reading confirmed yet');

    await user.type(box('USER_INPUT:field_dimension') as HTMLInputElement, '120"');
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(postsTo('/measurements')).toHaveLength(1));
    expect(JSON.parse(postsTo('/measurements')[0].body!).measurements).toEqual([
      { rule_id: 'R-FILLER', name: 'field_width', value: '120"' },
    ]);
  });

  it('keeps showing a value typed before, and that it still counts, after a reading is confirmed', async () => {
    offered = [READING];
    const user = await openValues();

    const offset = field('SHOP:CT007')!;
    await user.click(within(offset).getByRole('button', { name: /1 reading from/ }));
    await user.click(within(offset).getByTitle('Confirm 5 in as Front offset'));
    await waitFor(() => expect(postsTo('/confirm')).toHaveLength(1));

    // The checks still use the typed 4" (it outranks the reading), so the screen shows that one.
    await waitFor(() => expect(field('SHOP:CT007')!.textContent).toContain('does not replace it'));
    const after = field('SHOP:CT007')!;
    expect(after.textContent).toContain('4"');
    expect(after.textContent).toContain('still counts');
    expect(after.textContent).not.toContain('5 in');
    // Save never sends a drawn value, so nothing on a confirm-only field may say Save replaces it.
    expect(after.textContent).not.toContain('replaces it');
    expect(after.querySelector('[data-part="saved-replaced"]')).toBeNull();
  });
});

describe('a drawn field with no value says only what the screen knows (#1253 review)', { timeout: 15_000 }, () => {
  it('names the page a reading is confirmed on when it is not the one in view', async () => {
    await openValues();

    const depth = field('SHOP:CT008')!;
    expect(depth.textContent).toContain('A reading is confirmed on page 2');
    expect(depth.textContent).not.toContain('No reading confirmed yet');
    expect(depth.textContent).not.toContain('not supplied');
  });

  it('says two readings confirmed on this page clear the field, naming both', async () => {
    await openValues();

    const width = field('SHOP:CT004')!;
    expect(width.textContent).toContain('2 readings are confirmed on this page: 24 in, 25 in');
    expect(width.textContent).not.toContain('not supplied');
  });

  it('points at the AI readings listed under the field rather than saying none exist', async () => {
    offered = [READING];
    await openValues();

    const depth = field('SHOP:CT010')!;
    expect(depth.textContent).toContain('Confirm one of the AI readings listed below');
    expect(depth.textContent).not.toContain('not supplied');
    expect(within(depth).getByRole('button', { name: /1 reading from/ })).toBeTruthy();
  });
});

describe('a drawn field says exactly what the checks will use (#1262)', { timeout: 15_000 }, () => {
  it('names the checks that still use a value typed for only some of them', async () => {
    await openValues();

    const left = field('SHOP:CT011')!;
    expect(left.textContent).toContain('1"');
    expect(left.textContent).toContain('Only check R-LEFT-A still uses this typed value.');
    expect(left.textContent).toContain('Check R-LEFT-B uses the confirmed reading instead.');
    expect(left.textContent).not.toContain('the checks still use the typed value');
  });

  it('says which check waits for a reading and which holds a different typed value', async () => {
    await openValues();

    const back = field('SHOP:CT009')!;
    expect(back.textContent).toContain('Only check R-BACK-A still uses this typed value.');
    expect(back.textContent).toContain('Check R-BACK-B uses a different typed value.');
    expect(back.textContent).toContain('Check R-BACK-C waits for a reading to be confirmed.');
  });

  it('a value typed for every check it feeds keeps the one plain line', async () => {
    await openValues();

    const offset = field('SHOP:CT007')!;
    expect(offset.textContent).not.toContain('Only check');
  });

  it('a value shown here that is also confirmed on another page says so, and that the check waits', async () => {
    await openValues();

    const right = field('SHOP:CT013')!;
    expect(right.textContent).toContain('3 in');
    expect(right.textContent).toContain('Also confirmed on page 2.');
    expect(right.textContent).toContain('the check sees two readings and waits');
  });

  it('a value confirmed only on this page says nothing about other pages', async () => {
    await openValues();

    expect(field('SHOP:CT012')!.textContent).not.toContain('Also confirmed');
  });
});
