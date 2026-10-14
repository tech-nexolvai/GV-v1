// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { MeasurementPanel } from '@/pages/MeasurementPanel';
import { CountertopRunsList } from '@/components/measure/CountertopRunsList';
import { countOf, isComplete, stepAfter, stepBefore, sumCounts } from '@/lib/measure-steps';
import { slotRowCount, unsavedRowCount } from '@/components/measure/slotReaderReview';

// Synthetic data only: nothing here comes from a client drawing.
vi.mock('@/api/config', () => ({ projectId: () => 'p' }));

const REQUIRED = {
  quantities: [
    { key: 'SHOP:countertop_width', semantic_type: 'countertop_width', source: 'SHOP', many: false, consumers: [{ rule_id: 'CT-WIDTH-001', name: 'width' }], categories: [] },
    { key: 'SHOP:sink_width', semantic_type: 'sink_width', source: 'SHOP', many: false, consumers: [{ rule_id: 'CT-SINK-001', name: 'sink' }], categories: [] },
  ],
  page_numbers: [1],
  confirmed_readings: [],
  proposed_readings: [],
  parameters: [
    { name: 'field_cut', scope: 'project', rule_ids: ['CT-WIDTH-001'], declared_default: '1 in', blocked: false, sources: [], found: null },
    { name: 'overhang', scope: 'project', rule_ids: ['CT-DEPTH-001'], declared_default: null, blocked: false, sources: [], found: null },
    { name: 'waiting', scope: 'project', rule_ids: ['CT-X-001'], declared_default: null, blocked: true, sources: [], found: null },
  ],
  discriminators: [],
  rules_published: 3,
  still_reading: false,
  revision_state: 'READY',
};
const slot = (id: string, extra: Record<string, unknown> = {}) => ({
  row_id: id, page_number: 2, label: `Synthetic countertop ${id}`, piece_count: 2, held_reason: null,
  wall_confirmation_allowed: true, values: [], wall_proposal: null, wall_source: null, wall_reason: null,
  wall_layout_choices: ['back_left_right', 'back_only', 'island'], decision_id: null, confirmed_by: null, decided_at: null, wall_config: null,
  ...extra,
});
let slotRows = [slot('row-a'), slot('row-b', { wall_config: 'back_only' })];
const view = (id: string, extra: Record<string, unknown> = {}) => ({
  view_id: id, page_index: 1, tag: `panel-${id}`, role: null, suggested_role: null, suggested_from: 'Synthetic heading', reason: 'synthetic', ...extra,
});
let views: ReturnType<typeof view>[] = [];
/** Per-test overrides: a path suffix answered differently (a failure, or never). */
let override: Record<string, () => Promise<Response>> = {};

const calls: { method: string; url: string; body?: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

beforeEach(() => {
  calls.length = 0;
  slotRows = [slot('row-a'), slot('row-b', { wall_config: 'back_only' })];
  views = [];
  override = {};
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ method: init?.method ?? 'GET', url, body: typeof init?.body === 'string' ? init.body : undefined });
    for (const [suffix, answer] of Object.entries(override)) if (url.endsWith(suffix)) return answer();
    if (url.includes('/required-inputs')) return json(REQUIRED);
    if (url.includes('/candidates')) return json({ candidates: [], page_number: 1, total: 0 });
    if (url.endsWith('/semantic-types')) return json(['countertop_width', 'sink_width']);
    if (url.endsWith('/views')) return json({ views });
    if (url.endsWith('/parts')) return json({ drawings: [] });
    if (url.endsWith('/countertop-runs')) return json({ can_suggest: false, why_not: null, drawings: [] });
    if (url.endsWith('/reading-parts')) return json({ can_suggest: true, why_not: null, drawings: [] });
    if (url.endsWith('/slot-rows')) return json({ rows: slotRows });
    if (/\/slot-rows\/[^/]+\/review$/.test(url)) return json(slotRows[0], 201);
    if (url.endsWith('/countertop-results')) return json({ items: [] });
    if (url.includes('/picture')) return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    if (url.endsWith('/pages/pictures')) return json({ queued: false }, 202);
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
  }));
});
afterEach(() => vi.unstubAllGlobals());

describe('measurements wizard: counting', () => {
  it('adds up what the sections report, and an empty step is not "done"', () => {
    expect(sumCounts([{ done: 1, total: 3 }, null, { done: 2, total: 2 }])).toEqual({ done: 3, total: 5 });
    expect(sumCounts([null, undefined])).toBeNull();
    expect(isComplete({ done: 2, total: 2 })).toBe(true);
    expect(isComplete({ done: 0, total: 0 })).toBe(false);
    expect(countOf(5, 2)).toEqual({ done: 3, total: 5 });
    expect([stepBefore('drawings'), stepAfter('drawings'), stepAfter('settings')]).toEqual([null, 'runs', null]);
  });

  it('a countertop row is open while a width is missing or its walls are unanswered', () => {
    const rows = [
      slot('walls-open'),
      slot('answered', { wall_config: 'back_only' }),
      slot('needs-width', { wall_config: 'back_only', values: [{ key: 'k', label: 'Piece 1', position: 0, value: null, suggestion: null, source: 'missing', review_reason: null, needs_value: true }] }),
      slot('held-elsewhere', { held_reason: 'Synthetic hold.', wall_confirmation_allowed: false }),
      // Held with a missing width: its inputs are disabled here and the server refuses them (review fix).
      slot('held-width', { held_reason: 'Synthetic hold.', wall_confirmation_allowed: false, values: [{ key: 'k', label: 'Piece 1', position: 0, value: null, suggestion: null, source: 'missing', review_reason: null, needs_value: true }] }),
    ];
    expect(slotRowCount(rows as never)).toEqual({ done: 3, total: 5 });
  });

  it('counts rows holding changes "Save this row" has not sent', () => {
    const rows = [slot('a'), slot('b', { wall_config: 'back_only' }), slot('c')];
    expect(unsavedRowCount(rows as never, { a: 'island', b: 'back_only' }, { c: { k: '  ' } })).toBe(1);
    expect(unsavedRowCount(rows as never, {}, { c: { k: '3"' } })).toBe(1);
  });
});

const visibleSteps = () => [...document.querySelectorAll<HTMLElement>('[data-measure-step]')].filter((el) => !el.hidden).map((el) => el.dataset.measureStep);
const stepButton = (label: RegExp) => within(screen.getByRole('navigation', { name: 'Measurement steps' })).getByRole('button', { name: label });

async function openPanel(props: Partial<Parameters<typeof MeasurementPanel>[0]> = {}) {
  const view = render(<MeasurementPanel packageId="k" {...props} />);
  await screen.findByRole('navigation', { name: 'Measurement steps' }, { timeout: 5000 });
  return view;
}

describe('measurements wizard: on screen', { timeout: 15_000 }, () => {
  it('shows one step at a time, and Next / Back move between them', async () => {
    const user = userEvent.setup();
    await openPanel();
    expect(visibleSteps()).toEqual(['drawings']);
    await user.click(screen.getByRole('button', { name: 'Next' }));
    expect(visibleSteps()).toEqual(['runs']);
    await user.click(stepButton(/Values/));
    expect(visibleSteps()).toEqual(['values']);
    expect(stepButton(/Values/).getAttribute('aria-current')).toBe('step');
    await user.click(screen.getByRole('button', { name: 'Back' }));
    expect(visibleSteps()).toEqual(['runs']);
  });

  it('one primary action per step: Next, then Run checks only on the last step, keeping its id (#1124)', async () => {
    const user = userEvent.setup();
    await openPanel();
    const footer = () => document.querySelector<HTMLElement>('[data-slot="measure-actions"]')!;
    const primaries = () => [...footer().querySelectorAll<HTMLButtonElement>('button[data-variant="default"]')].map((b) => b.textContent?.trim());
    const labels = () => [...footer().querySelectorAll<HTMLButtonElement>('button')].map((b) => b.textContent?.trim());
    for (const [label, primary] of [[/Drawings/, 'Next'], [/Countertops/, 'Next'], [/Values/, 'Next'], [/Settings/, 'Run checks']] as const) {
      await user.click(stepButton(label));
      expect(primaries()).toEqual([primary]);
      const run = document.getElementById('measure-run-checks');
      if (primary === 'Run checks') {
        expect(run?.textContent).toContain('Run checks');
        expect(run?.closest('[hidden]')).toBeNull();
      } else {
        expect(run).toBeNull();
      }
      // The Results tab is right there: no duplicate "See findings".
      expect(labels()).not.toContain('See findings');
    }
    // Save values is the quiet one, and only where the values and settings it records are.
    expect(footer().querySelector('button[data-variant="outline"]')?.textContent).toBe('Save values');
    await user.click(stepButton(/Drawings/));
    expect(labels()).not.toContain('Save values');
    // Back is a ghost button, and step 1 has none.
    expect(labels().some((l) => l?.includes('Back'))).toBe(false);
    await user.click(stepButton(/Countertops/));
    expect(footer().querySelector('button[data-variant="ghost"]')?.textContent).toContain('Back');
  });

  it("the header's Run checks opens the last step, where #measure-run-checks is (#1124)", async () => {
    const { rerender } = await openPanel();
    expect(document.getElementById('measure-run-checks')).toBeNull();
    rerender(<MeasurementPanel packageId="k" runChecksRequest={1} />);
    expect(visibleSteps()).toEqual(['settings']);
    expect(document.getElementById('measure-run-checks')).not.toBeNull();
    // Opened straight from the header (first mount with a request) works too.
    const second = render(<MeasurementPanel packageId="k" runChecksRequest={1} />);
    await waitFor(() => expect(second.container.querySelector('#measure-run-checks')).not.toBeNull(), { timeout: 5000 });
  });

  it('renders no h1: the review shell owns the page heading (#1124)', async () => {
    await openPanel();
    expect(document.querySelectorAll('h1')).toHaveLength(0);
    expect(screen.getByRole('heading', { level: 2, name: 'Review measurements' })).toBeTruthy();
  });

  it('counts in words, and nothing is pre-selected (#1124)', async () => {
    const user = userEvent.setup();
    views = [view('a'), view('b'), view('c', { role: 'shop' })];
    await openPanel();
    expect(await screen.findByText('2 drawings still to confirm.', undefined, { timeout: 5000 })).toBeTruthy();
    // No role is chosen for an unconfirmed drawing; only the saved one shows as chosen.
    const pressed = [...document.querySelectorAll('[data-slot="drawing-roles"] button[aria-pressed="true"]')];
    expect(pressed.map((b) => b.textContent)).toEqual(["Vendor's drawing"]);
    // The step bar says "N of M done", never a bare "N/M".
    const nav = screen.getByRole('navigation', { name: 'Measurement steps' });
    await waitFor(() => expect(within(nav).getByRole('button', { name: /Drawings/ }).textContent).toContain('1 of 3 done'), { timeout: 5000 });
    expect(nav.textContent).not.toMatch(/\d\/\d/);
    // The countertop rows say how many are done, in words.
    await user.click(stepButton(/Countertops/));
    expect(await screen.findByText('1 of 2 countertop rows done.', undefined, { timeout: 5000 })).toBeTruthy();
    // No layout or wall answer is chosen for the reviewer.
    const group = screen.getAllByRole('radiogroup', { name: 'Wall layout for this row' })[0];
    expect(within(group).queryAllByRole('radio', { checked: true })).toHaveLength(0);
  });

  it('a value typed in one step survives switching to another and back', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Values/));
    const input = () => document.getElementById('q-SHOP:countertop_width') as HTMLInputElement;
    await waitFor(() => expect(input()).not.toBeNull(), { timeout: 5000 });
    await user.type(input(), '42"');
    await user.click(stepButton(/Settings/));
    await user.click(stepButton(/Values/));
    expect(input().value).toBe('42"');
    // Nothing was saved by moving around.
    expect(calls.some((c) => c.method === 'POST' && c.url.endsWith('/measurements'))).toBe(false);
  });

  it('"Open countertop card" lands on the Countertops step', async () => {
    const { rerender } = await openPanel();
    expect(visibleSteps()).toEqual(['drawings']);
    rerender(<MeasurementPanel packageId="k" targetRow="row-a" />);
    expect(visibleSteps()).toEqual(['runs']);
  });

  it('counts each step: countertop rows, values on screen, settings that are filled or have a rulebook value', async () => {
    await openPanel();
    const nav = screen.getByRole('navigation', { name: 'Measurement steps' });
    // #1124: in words ("1 of 2 done"), no longer "1/2".
    await waitFor(() => expect(within(nav).getByRole('button', { name: /Countertops/ }).textContent).toContain('1 of 2 done'), { timeout: 5000 });
    expect(within(nav).getByRole('button', { name: /Values/ }).textContent).toContain('0 of 2 done');
    // field_cut has a rulebook value; overhang is empty; the blocked one is not the reviewer's.
    expect(within(nav).getByRole('button', { name: /Settings/ }).textContent).toContain('1 of 2 done');
  });

  it('a wall picture button is only a draft; "Use this wall layout" sends it', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Countertops/));
    const group = (await screen.findAllByRole('radiogroup', { name: 'Wall layout for this row' }, { timeout: 5000 }))[0];
    expect(within(group).getAllByRole('radio').every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    await user.click(within(group).getByRole('radio', { name: /Island/ }));
    expect(calls.some((c) => c.method === 'POST' && c.url.includes('/slot-rows/'))).toBe(false);
    await user.click(screen.getAllByRole('button', { name: 'Use this wall layout for this row' })[0]);
    await waitFor(() => expect(calls.find((c) => c.method === 'POST' && c.url.includes('/slot-rows/row-a/review'))?.body).toBe('{"wall_config":"island"}'));
  });

  it('the saved wall answer shows as chosen; nothing else does', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Countertops/));
    await waitFor(() => expect(screen.getAllByRole('radiogroup', { name: 'Wall layout for this row' })).toHaveLength(2), { timeout: 5000 });
    const [open, answered] = screen.getAllByRole('radiogroup', { name: 'Wall layout for this row' });
    expect(within(open).queryAllByRole('radio', { checked: true })).toHaveLength(0);
    expect(within(answered).getByRole('radio', { checked: true }).textContent).toContain('Back wall only');
    act(() => void fireEvent.keyDown(document.body, { key: 'Escape' }));
  });
  it('warns in the action bar when a countertop row has unsaved changes', async () => {
    const user = userEvent.setup();
    slotRows = [slot('row-a', { wall_config: 'back_only', values: [{ key: 'piece', label: 'Piece 1', position: 0, value: null, suggestion: null, source: 'missing', review_reason: null, needs_value: true }] })];
    await openPanel();
    await user.click(stepButton(/Countertops/));
    await user.type(await screen.findByPlaceholderText('Enter the value with its unit', undefined, { timeout: 5000 }), '3"');
    await user.click(stepButton(/Settings/));
    expect(document.querySelector('[data-part="unsaved-rows"]')?.textContent).toMatch(/1 countertop row has unsaved changes in step 2/);
    await user.click(screen.getByRole('button', { name: 'Go to step 2' }));
    expect(visibleSteps()).toEqual(['runs']);
  });

  it('a held row says why in full, and offers the queue', async () => {
    const user = userEvent.setup();
    const onOpenQueue = vi.fn();
    slotRows = [slot('row-a', { held_reason: 'Synthetic: the stone runs into the walls, so the reviewer decides.', wall_confirmation_allowed: false })];
    await openPanel({ onOpenQueue });
    await user.click(stepButton(/Countertops/));
    expect((await screen.findByText(/Needs review: Synthetic: the stone runs into the walls, so the reviewer decides\./, undefined, { timeout: 5000 }))).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Decide in the queue' }));
    expect(onOpenQueue).toHaveBeenCalled();
  });
});

describe('measurements wizard: review fixes (#1124)', { timeout: 15_000 }, () => {
  // Saving values and queuing checks; the page-picture request a step makes on its own is not one.
  const writes = () => calls.filter((c) => c.method !== 'GET' && /\/(measurements|checks)$/.test(c.url));

  it('a double-click on Next in Values moves one step and never runs the checks', async () => {
    const user = userEvent.setup();
    await openPanel();
    await user.click(stepButton(/Values/));
    await user.dblClick(screen.getByRole('button', { name: 'Next' }));
    expect(visibleSteps()).toEqual(['settings']);
    expect(writes()).toEqual([]);
    // Focus went to the new step's heading, not to the button that took Next's place.
    expect(document.activeElement?.getAttribute('data-step-heading')).toBe('settings');
  });

  it('Run checks ignores a click right after a step change, and works once the step has settled', async () => {
    await openPanel();
    fireEvent.click(screen.getByRole('button', { name: /Settings/ }));
    fireEvent.click(document.getElementById('measure-run-checks')!);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(writes()).toEqual([]);
    await new Promise((resolve) => setTimeout(resolve, 350));
    fireEvent.click(document.getElementById('measure-run-checks')!);
    await waitFor(() => expect(writes().some((c) => c.url.endsWith('/measurements'))).toBe(true));
  });

  it('says a step is empty only when every section loaded empty', async () => {
    await openPanel();
    expect(await screen.findByText('Nothing to decide on these drawings.', undefined, { timeout: 5000 })).toBeTruthy();
  });

  it('never says a step is empty over a failed section', async () => {
    override = { '/parts': async () => json({ error: 'http_error', message: 'Synthetic failure.', request_id: 'r' }, 500) };
    await openPanel();
    expect(await screen.findByText(/The parts of these drawings could not be listed/, undefined, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByText('Nothing to decide on these drawings.')).toBeNull();
  });

  it('never says a step is empty while a section is still loading', async () => {
    override = { '/views': () => new Promise<Response>(() => undefined) };
    await openPanel();
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(screen.queryByText('Nothing to decide on these drawings.')).toBeNull();
  });

  it("confirming a run cannot send the readers' wall suggestion unless the person chose it", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    const top = {
      countertop_item_id: 'top', number: 2, code: null, decision: null,
      suggestion: { members: [{ item_id: 'a', number: 1, kind: 'cabinet', position: 1, signal: 'synthetic' }], left_out: [], warnings: [], edge_tolerance: '0.004' },
      wall_layout_proposal: { value: 'island', source: 'readers' },
    };
    const runs = {
      can_suggest: true, why_not: null, wall_layout_choices: ['back_left_right', 'back_only', 'island'],
      drawings: [{ view_id: 'v', page_index: 0, tag: 't', can_confirm: true, why_not: null, parts: [{ item_id: 'a', number: 1, kind: 'cabinet', code: null }], countertops: [top] }],
    };
    render(<CountertopRunsList runs={runs as never} saving={null} onConfirm={onConfirm} onWithdraw={() => undefined} />);
    const confirm = screen.getByRole('button', { name: 'Confirm this run' });
    expect((screen.getByRole('combobox') as HTMLSelectElement).value).toBe('');
    expect(confirm).toHaveProperty('disabled', true);
    await user.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: 'Use the suggested layout: Island' }));
    await user.click(screen.getByRole('button', { name: 'Confirm this run' }));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ countertop_item_id: 'top' }), ['a'], 'island');
  });
});
