// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MeasurementPanel } from '@/pages/MeasurementPanel';
import { mixedFractionsIn, mixedNumber, mixedQuantity } from '@/lib/fractions';
import { blankUses, measurementUnchanged, savedChangesSince, settingUnchanged, showsSaved } from '@/lib/saved-values';
import { settingEntry } from '@/components/measure/settingPointers';
import { settingDate } from '@/components/settings/companySettings';
import { readingsOf } from '@/lib/drawing-viewer';

// Synthetic data only (#1183): nothing here comes from a client drawing or a client's values.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const SAVED_AT = '2026-03-04T12:00:00Z';
const COMPANY_AT = '2026-02-01T12:00:00Z';
const value = (text: string, numerator: string, denominator: string) => ({
  numerator, denominator, unit: 'in', text, set_by: 'synthetic reviewer', set_at: SAVED_AT,
});

const REQUIRED = {
  quantities: [
    { key: 'USER_INPUT:top_width', semantic_type: 'top_width', source: 'USER_INPUT', many: false, consumers: [{ rule_id: 'R-WIDTH', input_name: 'top_width' }], categories: [] },
    { key: 'SHOP:piece_widths', semantic_type: 'piece_widths', source: 'SHOP', many: true, consumers: [{ rule_id: 'R-PIECES', input_name: 'piece_widths' }], categories: [] },
    { key: 'USER_INPUT:sink_width', semantic_type: 'sink_width', source: 'USER_INPUT', many: false, consumers: [{ rule_id: 'R-SINK', input_name: 'sink_width' }], categories: [] },
  ],
  page_numbers: [1],
  confirmed_readings: [],
  proposed_readings: [],
  parameters: [
    { name: 'edge_thickness', scope: 'project', rule_ids: ['R-EDGE'], declared_default: null, blocked: false, sources: [{ value: 'G.C / Client', guidance: 'g' }, { value: 'Measured', guidance: 'm' }], found: null },
    { name: 'offset_minimum', scope: 'global', rule_ids: ['R-OFFSET'], declared_default: '5/2 in', blocked: false, sources: [], found: null },
    { name: 'gap_maximum', scope: 'global', rule_ids: ['R-GAP'], declared_default: '1 in', blocked: false, sources: [], found: null },
  ],
  discriminators: [],
  rules_published: 4,
  still_reading: false,
  revision_state: 'READY',
  saved_measurements: [
    { key: 'USER_INPUT:top_width', values: [value('40 1/2"', '81', '2')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: true },
    { key: 'SHOP:piece_widths', values: [value('12"', '12', '1'), value('15 15/16"', '255', '16')], set_by: 'synthetic reviewer', set_at: SAVED_AT, complete: true },
  ],
  saved_parameters: [
    { name: 'edge_thickness', layer: 'project' as const, value: value('1 1/2"', '3', '2'), source: 'G.C / Client', reference: 'Synthetic note 1', citation: null },
  ],
};
const COMPANY = {
  version: 2,
  settings: [
    { name: 'gap_maximum', scope: 'global', rule_ids: ['R-GAP'], rulebook_default: '1 in', rulebook_note: null, company_value: '3 in', company_set_by: 'synthetic admin', company_set_at: COMPANY_AT, in_use: '3 in', in_use_from: 'company' },
    { name: 'offset_minimum', scope: 'global', rule_ids: ['R-OFFSET'], rulebook_default: '2 1/2 in', rulebook_note: null, company_value: null, company_set_by: null, company_set_at: null, in_use: '2 1/2 in', in_use_from: 'rulebook' },
  ],
};

let required: typeof REQUIRED = REQUIRED;
let companyAnswer: () => Response;
const calls: { method: string; url: string; body?: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  calls.length = 0;
  required = REQUIRED;
  companyAnswer = () => json(COMPANY);
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = init?.method ?? 'GET';
    calls.push({ method, url, body: typeof init?.body === 'string' ? init.body : undefined });
    if (url.includes('/required-inputs')) return json(required);
    if (url.endsWith('/company-settings')) return companyAnswer();
    if (method === 'POST' && url.endsWith('/checks')) return json({ accepted_id: 'synthetic-run-0001' }, 202);
    if (method === 'POST' && url.endsWith('/measurements')) {
      return json({ parameter_set_version: 2, measurement_set_version: 2, parameters: [], measurements: [], lists: [] }, 201);
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
const box = (id: string) => document.getElementById(id) as HTMLInputElement | null;
const savedWords = `typed by synthetic reviewer, ${settingDate(SAVED_AT)}`;

async function openPanel() {
  render(<MeasurementPanel packageId="k" />);
  await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 });
}

describe('fractions print as mixed numbers (#1183)', () => {
  it('turns an improper fraction into a mixed number in lowest terms', () => {
    expect(mixedNumber('5', '2')).toBe('2 1/2');
    expect(mixedNumber('31', '16')).toBe('1 15/16');
    expect(mixedNumber('10', '4')).toBe('2 1/2');
    expect(mixedNumber('3', '8')).toBe('3/8');
  });

  it('prints whole numbers without a fraction', () => {
    expect(mixedNumber('4', '1')).toBe('4');
    expect(mixedNumber('48', '16')).toBe('3');
    expect(mixedNumber('0', '5')).toBe('0');
  });

  it('keeps the sign of a negative value and refuses what is not a fraction', () => {
    expect(mixedNumber('-5', '2')).toBe('-2 1/2');
    expect(mixedNumber('5', '-2')).toBe('-2 1/2');
    expect(mixedNumber('-1', '4')).toBe('-1/4');
    expect(mixedNumber('1', '0')).toBeNull();
    expect(mixedNumber('2.5', '1')).toBeNull();
    expect(mixedNumber('', '2')).toBeNull();
  });

  it('is exact for numbers too large for a float', () => {
    expect(mixedNumber('90071992547409931', '2')).toBe('45035996273704965 1/2');
  });

  it('rewrites improper fractions inside text and leaves the rest alone', () => {
    expect(mixedFractionsIn('5/2 in')).toBe('2 1/2 in');
    expect(mixedFractionsIn('31/16"')).toBe('1 15/16"');
    expect(mixedFractionsIn('25 1/2"')).toBe('25 1/2"');
    expect(mixedFractionsIn('3/8 in')).toBe('3/8 in');
    expect(mixedFractionsIn('2.5 in')).toBe('2.5 in');
    expect(mixedFractionsIn('-5/2 in')).toBe('-2 1/2 in');
    expect(mixedFractionsIn('from 51/2 to 3/2 in')).toBe('from 25 1/2 to 1 1/2 in');
    // Feet and inches: a mixed inch part is left alone, an improper one becomes mixed.
    expect(mixedFractionsIn(`5'-6 1/2"`)).toBe(`5'-6 1/2"`);
    expect(mixedFractionsIn(`5'-13/2"`)).toBe(`5'-6 1/2"`);
    expect(mixedFractionsIn(`1'-0 3/4"`)).toBe(`1'-0 3/4"`);
    // Nothing divides by zero: left exactly as written.
    expect(mixedFractionsIn('5/0 in')).toBe('5/0 in');
  });

  it('writes an exact value with its unit', () => {
    expect(mixedQuantity({ numerator: '5', denominator: '2', unit: 'in' })).toBe('2 1/2 in');
    expect(mixedQuantity({ numerator: '7', denominator: '1', unit: 'in' })).toBe('7 in');
  });

  it("a finding's reading with no recorded rendering prints as a mixed number", () => {
    const chain = {
      trace: { kind: 'none' },
      operands: [{
        name: 'top_width', numerator: '81', denominator: '2', unit: 'in', evidence_status: 'SEALED',
        evidence: { document_role: 'shop', canonical_observation_id: 'o', crop_artifact_id: 'c', page_index: 0, document_version_id: 'd', polygon: null },
      }],
    };
    expect(readingsOf(chain as never).map((reading) => reading.value)).toEqual(['40 1/2"']);
  });
});

describe('saved-value rules (#1183)', () => {
  const saved = REQUIRED.saved_measurements[1];
  it('a run is unchanged only when every value matches, in order', () => {
    expect(showsSaved(['12"', ' 15 15/16" ', ''], ['12"', '15 15/16"'])).toBe(true);
    expect(measurementUnchanged(['12"', '15 15/16"'], saved)).toBe(true);
    expect(measurementUnchanged(['15 15/16"', '12"'], saved)).toBe(false);
    expect(measurementUnchanged(['12"'], saved)).toBe(false);
    // Held for only some of its checks: sent again, so every check gets it.
    expect(measurementUnchanged(['12"', '15 15/16"'], { ...saved, complete: false })).toBe(false);
    expect(measurementUnchanged('12"', undefined)).toBe(false);
  });

  it('a setting is unchanged only with the same value, layer, source and reference', () => {
    const setting = REQUIRED.saved_parameters[0];
    const entry = { value: '1 1/2"', scope: 'project' as const, source: 'G.C / Client', reference: 'Synthetic note 1' };
    expect(settingUnchanged(entry, setting)).toBe(true);
    expect(settingUnchanged({ ...entry, value: '1 3/4"' }, setting)).toBe(false);
    expect(settingUnchanged({ ...entry, source: 'Measured' }, setting)).toBe(false);
    expect(settingUnchanged({ ...entry, reference: 'Other' }, setting)).toBe(false);
    expect(settingUnchanged({ ...entry, scope: 'run' }, setting)).toBe(false);
    expect(settingUnchanged({ ...entry, citation: 'passage' }, setting)).toBe(false);
  });

  it('a blank setting uses the company value, else the rulebook default, and says when it cannot tell', () => {
    const settings = COMPANY.settings as never;
    expect(blankUses(REQUIRED.parameters[2], settings)).toEqual({ kind: 'company', value: '3 in', setBy: 'synthetic admin', setAt: COMPANY_AT });
    expect(blankUses(REQUIRED.parameters[1], settings)).toEqual({ kind: 'rulebook', value: '2 1/2 in' });
    expect(blankUses(REQUIRED.parameters[1], null)).toEqual({ kind: 'unknown', value: '2 1/2 in' });
    expect(blankUses(REQUIRED.parameters[0], settings)).toBeNull();
  });
});

describe('Measurements after a reload (#1183)', { timeout: 15_000 }, () => {
  it('every saved value is back in its box, with who typed it and when', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Values/));
    // A saved value is done, so "only what needs a look" hides it: show every field.
    await user.click(await screen.findByRole('checkbox', { name: 'Show only what needs a look' }, { timeout: 5000 }));
    await waitFor(() => expect(box('q-USER_INPUT:top_width')?.value).toBe('40 1/2"'), { timeout: 5000 });
    const runInputs = screen.getAllByRole('textbox', { name: /Piece widths, item/ }) as HTMLInputElement[];
    expect(runInputs.map((input) => input.value)).toEqual(['12"', '15 15/16"']);
    // Not saved: still empty, and nothing was made up for it.
    expect(box('q-USER_INPUT:sink_width')?.value).toBe('');
    const saved = [...document.querySelectorAll('[data-measure-step="values"] [data-part="saved-by"]')].map((el) => el.textContent);
    expect(saved).toEqual([savedWords, savedWords]);
    // A saved value is a person's: done, never "needs a look".
    const widthRow = box('q-USER_INPUT:top_width')!.closest('[data-origin]')!;
    expect(widthRow.getAttribute('data-origin')).toBe('saved');
    expect(widthRow.getAttribute('data-review')).toBe('done');
    // Reading back saved nothing (the page picture's own render request is not a save).
    expect(calls.filter((call) => call.method !== 'GET' && !call.url.endsWith('/pages/pictures'))).toEqual([]);
  });

  it('a saved setting is back with its source and reference, and says who typed it', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Settings/));
    await waitFor(() => expect(box('p-edge_thickness')?.value).toBe('1 1/2"'), { timeout: 5000 });
    const row = box('p-edge_thickness')!.closest('.grid')! as HTMLElement;
    expect(within(row).getByText(new RegExp(`${savedWords}, for this project \\(G\\.C / Client\\)`))).toBeTruthy();
    expect((within(row).getByDisplayValue('Synthetic note 1') as HTMLInputElement).value).toBe('Synthetic note 1');
    // Blank settings stay blank: a default is a hint, never put in the box.
    expect(box('p-offset_minimum')?.value).toBe('');
    expect(box('p-gap_maximum')?.value).toBe('');
  });

  it('Save values leaves unchanged saved values out, and sends a changed one', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Values/));
    // A saved value is done, so "only what needs a look" hides it: show every field.
    await user.click(await screen.findByRole('checkbox', { name: 'Show only what needs a look' }, { timeout: 5000 }));
    await waitFor(() => expect(box('q-USER_INPUT:top_width')?.value).toBe('40 1/2"'), { timeout: 5000 });
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/measurements'))).toBe(true));
    const first = JSON.parse(calls.find((c) => c.method === 'POST' && c.url.endsWith('/measurements'))!.body!);
    expect(first.measurements).toEqual([]);
    expect(first.parameters).toEqual([]);

    calls.length = 0;
    await user.clear(box('q-USER_INPUT:top_width')!);
    await user.type(box('q-USER_INPUT:top_width')!, '41"');
    expect(screen.getByText(/Save values replaces it\./)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/measurements'))).toBe(true));
    const second = JSON.parse(calls.find((c) => c.method === 'POST' && c.url.endsWith('/measurements'))!.body!);
    expect(second.measurements).toEqual([{ rule_id: 'R-WIDTH', name: 'top_width', value: '41"' }]);
    // The save is read back, so the boxes then say who typed what.
    await waitFor(() => expect(calls.some((c) => c.method === 'GET' && c.url.includes('/required-inputs'))).toBe(true));
  });
});

describe('Measurements step 4 lists the company settings in use (#1183)', { timeout: 15_000 }, () => {
  it('each company setting with its value and where it comes from', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Settings/));
    const list = await screen.findByRole('list', { name: 'Company settings in use' }, { timeout: 5000 });
    const gap = list.querySelector<HTMLElement>('[data-setting="gap_maximum"]')!;
    expect(gap.textContent).toContain('3 in');
    expect(gap.textContent).toContain(`GV company setting, set by synthetic admin on ${settingDate(COMPANY_AT)}`);
    const offset = list.querySelector<HTMLElement>('[data-setting="offset_minimum"]')!;
    expect(offset.textContent).toContain('2 1/2 in');
    expect(offset.textContent).toContain('rulebook default');
    // Each blank box says what it uses: the company value where GV set one, never the bare rulebook default.
    const gapRow = box('p-gap_maximum')!.closest('.grid') as HTMLElement;
    expect(gapRow.textContent).toContain('Blank uses GV\'s company setting 3 in');
    expect(gapRow.textContent).not.toContain('1 in');
    const offsetRow = box('p-offset_minimum')!.closest('.grid') as HTMLElement;
    expect(offsetRow.textContent).toContain('Blank uses the rulebook default 2 1/2 in');
    expect(document.body.textContent).not.toContain('5/2');
  });

  it("a project's own saved value is named beside the company setting it replaces", async () => {
    const user = userEvent.setup();
    required = { ...REQUIRED, saved_parameters: [{ name: 'gap_maximum', layer: 'project' as const, value: value('2"', '2', '1'), source: 'Company standard', reference: null as never, citation: null }] };
    await openPanel();
    await user.click(stepButton(/Settings/));
    const list = await screen.findByRole('list', { name: 'Company settings in use' }, { timeout: 5000 });
    await waitFor(() => expect(list.querySelector('[data-setting="gap_maximum"] [data-part="project-override"]')?.textContent).toBe(`This project uses 2" instead (${savedWords}).`), { timeout: 5000 });
    expect(box('p-gap_maximum')?.value).toBe('2"');
  });

  it('says when the company settings cannot be read, and never claims the rulebook default is in use', async () => {
    const user = userEvent.setup();
    companyAnswer = () => json({ error: 'http_error', message: 'Synthetic failure.', request_id: 'r' }, 500);
    await openPanel();
    await user.click(stepButton(/Settings/));
    expect(await screen.findByText(/Company settings could not be loaded/, undefined, { timeout: 5000 })).toBeTruthy();
    const offsetRow = box('p-offset_minimum')!.closest('.grid') as HTMLElement;
    expect(offsetRow.textContent).toContain("Blank uses GV's company setting if one is recorded, else the rulebook default 2 1/2 in");
    companyAnswer = () => json(COMPANY);
    await user.click(screen.getByRole('button', { name: 'Try loading company settings again' }));
    expect(await screen.findByRole('list', { name: 'Company settings in use' }, { timeout: 5000 })).toBeTruthy();
  });
});

const postsTo = (suffix: string) => calls.filter((call) => call.method === 'POST' && call.url.endsWith(suffix));
const settle = () => new Promise((resolve) => setTimeout(resolve, 350));

describe('review fixes (#1183)', { timeout: 20_000 }, () => {
  it('a value saved for only some of its checks needs a look, stays under the filter, and is sent again', async () => {
    const user = userEvent.setup();
    required = { ...REQUIRED, saved_measurements: [{ ...REQUIRED.saved_measurements[0], complete: false }, REQUIRED.saved_measurements[1]] };
    await openPanel();
    await user.click(stepButton(/Values/));
    // "Show only what needs a look" is on, and the partly saved field is still shown.
    await waitFor(() => expect(box('q-USER_INPUT:top_width')?.value).toBe('40 1/2"'), { timeout: 5000 });
    const row = box('q-USER_INPUT:top_width')!.closest('[data-origin]')!;
    expect(row.getAttribute('data-review')).toBe('needs_look');
    expect(row.textContent).toContain('Saved for some of the checks it feeds; save again to cover all.');
    // The complete run is done and hidden.
    expect(screen.queryAllByRole('textbox', { name: /Piece widths, item/ })).toHaveLength(0);
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(postsTo('/measurements')).toHaveLength(1));
    expect(JSON.parse(postsTo('/measurements')[0].body!).measurements).toEqual([{ rule_id: 'R-WIDTH', name: 'top_width', value: '40 1/2"' }]);
  });

  it('a saved value outranks an AI proposal for the same field', async () => {
    const user = userEvent.setup();
    required = {
      ...REQUIRED,
      proposed_readings: [{ field_key: 'USER_INPUT:top_width', name: 'top_width', source: 'USER_INPUT', many: false, placement_verified: true, expected_count: 1, values: [{ candidate_id: 'cand-1', value: '39 in', page_index: 0, position: 0 }] }],
    } as never;
    await openPanel();
    await user.click(stepButton(/Values/));
    await user.click(await screen.findByRole('checkbox', { name: 'Show only what needs a look' }, { timeout: 5000 }));
    await waitFor(() => expect(box('q-USER_INPUT:top_width')?.value).toBe('40 1/2"'), { timeout: 5000 });
    const row = box('q-USER_INPUT:top_width')!.closest('[data-origin]')!;
    expect(row.getAttribute('data-origin')).toBe('saved');
    expect(row.textContent).not.toContain('proposed by AI');
    // Nothing is waiting to be confirmed as an AI value on save.
    expect(document.querySelector('[data-part="save-records"]')).toBeNull();
  });

  it('a setting being edited or cleared is never refilled, and a new number drops the refilled source', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Settings/));
    await waitFor(() => expect(box('p-edge_thickness')?.value).toBe('1 1/2"'), { timeout: 5000 });
    const source = () => document.getElementById('p-edge_thickness-source') as HTMLSelectElement;
    expect(source().value).toBe('G.C / Client');
    await user.clear(box('p-edge_thickness')!);
    await user.type(box('p-edge_thickness')!, '2"');
    // The source and reference belonged to the saved number: the reviewer picks again.
    expect(source().value).toBe('');
    expect(screen.queryByDisplayValue('Synthetic note 1')).toBeNull();
    // Cleared, then a save reads everything back: the box stays as the reviewer left it.
    await user.clear(box('p-edge_thickness')!);
    const reads = () => calls.filter((call) => call.url.includes('/required-inputs')).length;
    const before = reads();
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(postsTo('/measurements')).toHaveLength(1));
    // Read before the save, after it, and once more by the form's own reload.
    await waitFor(() => expect(reads()).toBeGreaterThanOrEqual(before + 3), { timeout: 5000 });
    await settle();
    expect(box('p-edge_thickness')?.value).toBe('');
    expect(source().value).toBe('');
  });

  it('Run checks also leaves unchanged saved values out', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Settings/));
    await waitFor(() => expect(box('p-edge_thickness')?.value).toBe('1 1/2"'), { timeout: 5000 });
    await settle();
    await user.click(document.getElementById('measure-run-checks')!);
    await waitFor(() => expect(postsTo('/checks')).toHaveLength(1), { timeout: 5000 });
    const body = JSON.parse(postsTo('/measurements')[0].body!);
    expect(body.measurements).toEqual([]);
    expect(body.parameters).toEqual([]);
  });

  it('a value saved by someone else since the page opened stops Run checks until the reviewer chooses', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Settings/));
    await waitFor(() => expect(box('p-edge_thickness')?.value).toBe('1 1/2"'), { timeout: 5000 });
    // Another reviewer saves a new width while this page is open.
    const later = { ...REQUIRED.saved_measurements[0], values: [{ ...value('41"', '41', '1'), set_by: 'other reviewer', set_at: '2026-03-05T12:00:00Z' }], set_by: 'other reviewer', set_at: '2026-03-05T12:00:00Z' };
    required = { ...REQUIRED, saved_measurements: [later, REQUIRED.saved_measurements[1]] };
    await settle();
    await user.click(document.getElementById('measure-run-checks')!);
    const conflict = await screen.findByText(/Saved by other reviewer since you opened this:/, undefined, { timeout: 5000 });
    expect(conflict.closest('li')!.textContent).toContain('41". Keep yours?');
    // Nothing was saved and no checks were queued with a number this screen does not show.
    expect(postsTo('/measurements')).toEqual([]);
    expect(postsTo('/checks')).toEqual([]);
    // Keep mine: the width this screen shows is sent, not skipped.
    await user.click(screen.getByRole('button', { name: 'Keep mine' }));
    expect(document.querySelector('[data-part="saved-conflicts"]')).toBeNull();
    await user.click(document.getElementById('measure-run-checks')!);
    await waitFor(() => expect(postsTo('/checks')).toHaveLength(1), { timeout: 5000 });
    expect(JSON.parse(postsTo('/measurements')[0].body!).measurements).toEqual([{ rule_id: 'R-WIDTH', name: 'top_width', value: '40 1/2"' }]);
  });

  it('"Use theirs" puts their value in the box, and the next save skips it as unchanged', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Values/));
    await user.click(await screen.findByRole('checkbox', { name: 'Show only what needs a look' }, { timeout: 5000 }));
    await waitFor(() => expect(box('q-USER_INPUT:top_width')?.value).toBe('40 1/2"'), { timeout: 5000 });
    const later = { ...REQUIRED.saved_measurements[0], values: [{ ...value('41"', '41', '1'), set_by: 'other reviewer' }], set_by: 'other reviewer', set_at: '2026-03-05T12:00:00Z' };
    required = { ...REQUIRED, saved_measurements: [later, REQUIRED.saved_measurements[1]] };
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await screen.findByText(/Saved by other reviewer since you opened this:/, undefined, { timeout: 5000 });
    expect(postsTo('/measurements')).toEqual([]);
    await user.click(screen.getByRole('button', { name: 'Use theirs' }));
    expect(box('q-USER_INPUT:top_width')?.value).toBe('41"');
    await user.click(screen.getByRole('button', { name: 'Save values' }));
    await waitFor(() => expect(postsTo('/measurements')).toHaveLength(1), { timeout: 5000 });
    expect(JSON.parse(postsTo('/measurements')[0].body!).measurements).toEqual([]);
  });

  it('notices a change since the page opened, and only a change', () => {
    const before = { saved_measurements: REQUIRED.saved_measurements, saved_parameters: REQUIRED.saved_parameters };
    expect(savedChangesSince(before, before)).toEqual([]);
    const moved = { ...REQUIRED.saved_parameters[0], value: { ...REQUIRED.saved_parameters[0].value, set_at: '2026-03-06T12:00:00Z' } };
    expect(savedChangesSince(before, { ...before, saved_parameters: [moved] }).map((change) => change.id)).toEqual(['p:edge_thickness']);
    // Saved for the first time by someone else: a change too.
    expect(savedChangesSince({ saved_measurements: [] }, before).map((change) => change.id)).toEqual(['q:USER_INPUT:top_width', 'q:SHOP:piece_widths', 'p:edge_thickness']);
  });

  it('a setting saved from a passage: the same passage is skipped; another passage or a declined one is sent', () => {
    const pointer = { proposal_id: 'passage-1', page_index: 0, document_kind: 'architectural', has_crop: false };
    const setting = { name: 'edge_thickness', scope: 'project', sources: REQUIRED.parameters[0].sources, found: pointer };
    const saved = { ...REQUIRED.saved_parameters[0], source: 'G.C / Client', reference: null, citation: 'passage-1' };
    const cited = settingEntry(setting, '1 1/2"', { declined: {} });
    expect(cited.citation).toBe('passage-1');
    expect(settingUnchanged(cited, saved)).toBe(true);
    expect(settingUnchanged(cited, { ...saved, citation: 'passage-0' })).toBe(false);
    const declined = settingEntry(setting, '1 1/2"', { declined: { edge_thickness: true }, source: 'G.C / Client' });
    expect(declined.citation).toBeUndefined();
    expect(settingUnchanged(declined, saved)).toBe(false);
  });

  it('retrying the company settings clears the error and shows loading', async () => {
    const user = userEvent.setup();
    companyAnswer = () => json({ error: 'http_error', message: 'Synthetic failure.', request_id: 'r' }, 500);
    await openPanel();
    await user.click(stepButton(/Settings/));
    await screen.findByText(/Company settings could not be loaded/, undefined, { timeout: 5000 });
    let release: (response: Response) => void = () => undefined;
    companyAnswer = () => new Promise<Response>((resolve) => { release = resolve; }) as never;
    await user.click(screen.getByRole('button', { name: 'Try loading company settings again' }));
    expect(screen.queryByText(/Company settings could not be loaded/)).toBeNull();
    expect(screen.getByText('Loading company settings…')).toBeTruthy();
    release(json(COMPANY));
    expect(await screen.findByRole('list', { name: 'Company settings in use' }, { timeout: 5000 })).toBeTruthy();
  });
});
