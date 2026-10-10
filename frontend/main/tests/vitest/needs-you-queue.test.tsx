// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CountertopResult, SlotReaderRow } from '@/api/client';
import type { Finding } from '@/data/types';
import type { NextAction } from '@/lib/review-stage';
import { buildQueue, inputNewerThanResult, itemStatus, nextOpen, progressOf, wallQuestion, type LiveData } from '@/lib/needs-you-queue';
import { NeedsYouQueue, type QueueProps } from '@/components/queue/needs-you-queue';

// Synthetic data only: nothing here comes from a client drawing.
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: null, outcome: 'REVIEW_REQUIRED', needs_decision: true,
    printed_overall: x('40', '40"'),
    pieces: [{ index: 0, value: x('20', '20"'), kind: 'cabinet', source: 'sealed' }, { index: 1, value: x('20', '20"'), kind: 'cabinet', source: 'sealed' }],
    field_cut_per_end: x('1', '1"'), field_cut_count: 0, expected_total: null, delta: null,
    hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [true, true] },
    ...overrides,
  };
}
function finding(id: string, overrides: Partial<Finding> = {}): Finding {
  return { id, check_id: `RULE-${id}`, name: `rule ${id}`, outcome: 'REVIEW_REQUIRED', severity: 'major', reviewer_action: null, scope_label: null, ...overrides } as Finding;
}
function slot(id: string, overrides: Partial<SlotReaderRow> = {}): SlotReaderRow {
  return {
    row_id: id, page_number: 1, label: `Synthetic countertop ${id}`, piece_count: 2, held_reason: null,
    wall_confirmation_allowed: true, values: [], wall_proposal: null, wall_source: null, wall_reason: null,
    wall_layout_choices: ['back_left_right', 'back_only', 'island'], decision_id: null, confirmed_by: null, decided_at: null, wall_config: null,
    ...overrides,
  };
}

const WALLS = row('walls', 2);
const PANELS = row('panels', 5, { hold: { code: 'stone-short-of-ends', reason: 'Synthetic: stone between panels.' } });
const DONE = row('done', 3, { outcome: 'PASS', needs_decision: false });
const FAIL = row('fail', 12, { outcome: 'FAIL', delta: x('4', '4"') });
const ROWS = [FAIL, DONE, PANELS, WALLS];
const FINDINGS = [
  finding('f-walls'), finding('f-panels'), finding('f-done', { outcome: 'PASS' }), finding('f-fail', { outcome: 'FAIL' }),
  finding('f-sink', { outcome: 'NOT_FOUND', scope_label: 'Package revision', name: 'sink rule' }),
  finding('f-ok', { outcome: 'PASS' }),
];
const BLOCKING = new Set(['f-walls', 'f-panels', 'f-fail', 'f-sink']);
const BASE_SLOTS = [
  slot('walls'),
  slot('panels', { wall_source: 'between-panels', wall_proposal: 'back_only', wall_reason: 'Synthetic: the panels take the field cut.' }),
  slot('fail', { wall_source: 'vendor-drawing-clues', wall_proposal: 'back_left_right' }),
];

const live = (overrides: Partial<LiveData> = {}): LiveData => ({
  rows: new Map(ROWS.map((r) => [r.row_id, r])),
  findings: new Map(FINDINGS.map((f) => [f.id, f])),
  blocking: BLOCKING,
  wallsSaved: new Set(),
  ...overrides,
});

describe('needs-you queue: rules', () => {
  it('lists what blocks sign-off: countertops by page, then the other checks', () => {
    const items = buildQueue(ROWS, FINDINGS, BLOCKING);
    expect(items.map((i) => i.key)).toEqual(['row:walls', 'row:panels', 'row:fail', 'finding:f-sink']);
  });

  it('reads each item\'s status from the server data, never a local guess', () => {
    const items = buildQueue(ROWS, FINDINGS, BLOCKING);
    expect(items.map((i) => itemStatus(i, live()))).toEqual(['open', 'open', 'open', 'open']);
    // Decided once the server stops counting it; a wall answer waits for a check run.
    const after = live({
      rows: new Map(ROWS.map((r) => [r.row_id, r.row_id === 'fail' ? { ...r, needs_decision: false } : r])),
      blocking: new Set(['f-walls', 'f-panels']),
      wallsSaved: new Set(['walls']),
    });
    expect(items.map((i) => itemStatus(i, after))).toEqual(['waiting-for-run', 'open', 'decided', 'decided']);
    expect(progressOf(items, after)).toEqual({ handled: 3, total: 4, waitingForRun: 1 });
    // A correction is settled only by a new run, whatever was recorded after it.
    const corrected = live({ rows: new Map(ROWS.map((r) => [r.row_id, r.row_id === 'fail' ? { ...r, reviewer_decision: { action: 'correct', note: null, actor: 'a', time: '2026-01-01T00:00:00Z' } } : r])) });
    expect(itemStatus(items[2], corrected)).toBe('waiting-for-run');
  });

  it('a new countertop result belongs to its row even before the rows reload; an unknown row is still listed', () => {
    const fresh = finding('f-walls-2', { scope_row_candidate_id: 'walls' });
    const stray = finding('f-stray', { scope_row_candidate_id: 'not-on-screen' });
    const items = buildQueue(ROWS, [...FINDINGS, fresh, stray], new Set([...BLOCKING, 'f-walls-2', 'f-stray']));
    expect(items.map((i) => i.key)).toEqual(['row:walls', 'row:panels', 'row:fail', 'finding:f-sink', 'finding:f-stray']);
  });

  it("knows from the server's own times when a result is older than the row's saved walls or widths", () => {
    const recorded = finding('f-walls', { created_at: '2026-01-01T10:00:00Z' });
    expect(inputNewerThanResult(WALLS, recorded, slot('walls', { decided_at: '2026-01-01T11:00:00Z' }))).toBe(true);
    expect(inputNewerThanResult(WALLS, recorded, slot('walls', { decided_at: '2026-01-01T09:00:00Z' }))).toBe(false);
    expect(inputNewerThanResult(WALLS, recorded, slot('walls'))).toBe(false);
    // Never checked, but answered: only a run settles it.
    expect(inputNewerThanResult({ ...WALLS, finding_id: null }, undefined, slot('walls', { decided_at: '2026-01-01T11:00:00Z' }))).toBe(true);
    // So a reopened queue still shows it as waiting for a run, with no memory of its own.
    const items = buildQueue(ROWS, FINDINGS, BLOCKING);
    const reopened = live({
      findings: new Map(FINDINGS.map((f) => [f.id, f.id === 'f-walls' ? recorded : f])),
      slots: new Map([['walls', slot('walls', { wall_config: 'back_only', decided_at: '2026-01-01T11:00:00Z' })]]),
    });
    expect(itemStatus(items[0], reopened)).toBe('waiting-for-run');
  });

  it('moves on to the next open item, leaving out the one just decided', () => {
    const items = buildQueue(ROWS, FINDINGS, BLOCKING);
    expect(nextOpen(items, live(), 0, 0)).toBe(1);
    expect(nextOpen(items, live({ blocking: new Set(['f-walls']), rows: new Map() }), 0, 0)).toBeNull();
  });

  it('asks the wall question only where the server allows it and nobody has answered', () => {
    expect(wallQuestion(BASE_SLOTS[0])).toMatchObject({ choices: ['back_left_right', 'back_only', 'island'], proposal: null, betweenPanels: false });
    expect(wallQuestion(BASE_SLOTS[1])).toMatchObject({ betweenPanels: true });
    expect(wallQuestion(BASE_SLOTS[2])).toBeNull(); // walls found in the vendor's drawing
    expect(wallQuestion(slot('x', { wall_config: 'back_only' }))).toBeNull();
    expect(wallQuestion(slot('x', { wall_confirmation_allowed: false }))).toBeNull();
  });
});

// ── On screen ─────────────────────────────────────────────────

const calls: { method: string; url: string; body?: string }[] = [];
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const HISTORY = {
  items: [
    { id: 'a2', review_session_id: 's', finding_id: 'f-fail', package_revision_id: 'r', action: 'confirm', actor: 'Reviewer B', note: null, created_at: '2026-01-02T10:00:00Z' },
    { id: 'a1', review_session_id: 's', finding_id: 'f-fail', package_revision_id: 'r', action: 'dismiss', actor: 'Reviewer A', note: 'TEST first thought', created_at: '2026-01-01T10:00:00Z' },
  ],
};

let slotRows: SlotReaderRow[] = [];
let history: typeof HISTORY = { items: [] };

beforeEach(() => {
  calls.length = 0;
  slotRows = BASE_SLOTS;
  history = HISTORY;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ method: init?.method ?? 'GET', url, body: typeof init?.body === 'string' ? init.body : undefined });
    if (url.endsWith('/slot-rows')) return json({ rows: slotRows });
    if (/\/slot-rows\/[^/]+\/review$/.test(url)) return json(slotRows[0], 201);
    if (url.endsWith('/rules')) return json([{ rule_id: 'RULE-f-sink', name: 'Synthetic sink rule' }]);
    if (url.endsWith('/actions')) return json(history);
    if (url.includes('/chain')) return json({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: 'Synthetic.' } });
    if (url.includes('/picture')) return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
  }));
});
afterEach(() => vi.unstubAllGlobals());

const NEXT: NextAction = { kind: 'sign-off', label: 'Sign off', disabled: false, reason: null };

function setup(overrides: Partial<QueueProps> = {}) {
  const props: QueueProps = {
    open: true, opening: 1, onOpenChange: vi.fn(),
    rows: ROWS, rowsReady: true, findings: FINDINGS, blocking: BLOCKING,
    projectId: 'p', packageId: 'k',
    handlers: { onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) },
    next: NEXT, onAct: vi.fn(), onWallSaved: vi.fn(), onOpenCard: vi.fn(),
    ...overrides,
  };
  const view = render(<NeedsYouQueue {...props} />);
  return { props, rerender: (more: Partial<QueueProps>) => view.rerender(<NeedsYouQueue {...props} {...more} />) };
}

const title = () => document.querySelector('[data-slot="queue-item"] h2')?.textContent;
const key = (k: string) => act(() => void fireEvent.keyDown(document.body, { key: k }));

describe('needs-you queue: on screen', { timeout: 15_000 }, () => {
  it('opens on the first item, says how far along, and J / K move between items', async () => {
    setup();
    expect(title()).toBe('Synthetic countertop walls');
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toContain('0 of 4');
    key('j');
    expect(title()).toBe('Synthetic countertop panels');
    key('j');
    key('j');
    // A package-level check is named by its rule.
    expect(await screen.findByRole('heading', { name: 'Synthetic sink rule' }, { timeout: 5000 })).toBeTruthy();
    key('k');
    expect(title()).toBe('Synthetic countertop fail');
  });

  it('an item\'s facts say the walls in words, and carry the row\'s drawn-length note only when it has one (#1126, #1107)', () => {
    const NOTE = 'Drawn length not checked (no scale): piece 2, the overall';
    setup({ rows: [FAIL, DONE, PANELS, { ...WALLS, drawn_length_note: NOTE }] });
    expect(title()).toBe('Synthetic countertop walls');
    const item = () => document.querySelector('[data-slot="queue-item"]') as HTMLElement;
    expect(item().querySelector('[data-slot="drawn-length-note"]')?.textContent).toBe(NOTE);
    const walls = [...item().querySelectorAll('[data-slot="queue-facts"] > div')].find((chip) => chip.querySelector('dt')?.textContent === 'Walls')!;
    expect(walls.querySelector('dd')?.textContent).toBe('Not set');
    key('j');
    expect(title()).toBe('Synthetic countertop panels');
    expect(item().querySelector('[data-slot="drawn-length-note"]')).toBeNull();
  });

  it('opening puts focus on the queue, not a button, so Enter saves', async () => {
    const { props } = setup();
    expect(document.activeElement?.getAttribute('data-slot')).toBe('needs-you-queue');
    key('j');
    key('j');
    expect(title()).toBe('Synthetic countertop fail');
    key('1'); // Confirm finding: a FAIL confirmed needs no note
    act(() => void fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Enter' }));
    await waitFor(() => expect(props.handlers.onAction).toHaveBeenCalledWith('f-fail', 'confirm', undefined));
  });

  it('1 / 2 / 3 choose, N goes to the note, and a required note blocks saving', async () => {
    const { props } = setup();
    key('3');
    const save = screen.getByRole('button', { name: /Record decision/ });
    expect(save).toHaveProperty('disabled', true);
    key('Enter');
    expect(props.handlers.onAction).not.toHaveBeenCalled();
    key('n');
    const note = screen.getByLabelText(/Why is this not checkable\?/);
    expect(document.activeElement).toBe(note);
    await userEvent.type(note, 'TEST not in the drawings supplied');
    fireEvent.keyDown(note, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(props.handlers.onAction).toHaveBeenCalledWith('f-walls', 'dismiss', 'TEST not in the drawings supplied'));
  });

  it('a wall choice is never pre-selected and is sent only on the reviewer\'s click', async () => {
    const user = userEvent.setup();
    const { props } = setup();
    const walls = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    expect(within(walls).getAllByRole('radio').every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    const use = screen.getByRole('button', { name: 'Use this wall layout' });
    expect(use).toHaveProperty('disabled', true);
    await user.click(within(walls).getByRole('radio', { name: 'Back wall only' }));
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    await user.click(use);
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')).toMatchObject({ url: expect.stringContaining('/slot-rows/walls/review'), body: '{"wall_config":"back_only"}' }));
    expect(props.onWallSaved).toHaveBeenCalled();
    expect(await screen.findByText('Wall answer saved. Run the checks to see the result.', undefined, { timeout: 5000 })).toBeTruthy();
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toContain('1 of 4');
    // A decision now would be asked again after the run, and the queue says so on the next item.
    key('j');
    key('j');
    expect(document.querySelector('[data-slot="queue-run-first"]')?.textContent).toMatch(/1 answer waits for a check run\. That run replaces these results/);
  });

  it('the between-panels item is one click for "no field cut", never called "back wall only" (#1138)', async () => {
    const user = userEvent.setup();
    setup();
    key('j');
    await user.click(await screen.findByRole('button', { name: 'No field cut: the stone stops at panels' }, { timeout: 5000 }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')).toMatchObject({ url: expect.stringContaining('/slot-rows/panels/review'), body: '{"wall_config":"back_only"}' }));
  });

  it('a decided item can be changed (a new action) and shows its history, newest first', async () => {
    const user = userEvent.setup();
    // Decided after the queue opened, as a reviewer would: the item stays in the list.
    const { props, rerender } = setup();
    const decided = ROWS.map((r) => (r.row_id === 'fail' ? { ...r, needs_decision: false, reviewer_decision: { action: 'confirm', note: null, actor: 'Reviewer B', time: '2026-01-02T10:00:00Z' } } : r));
    rerender({ rows: decided, blocking: new Set(['f-walls', 'f-panels', 'f-sink']) });
    key('j');
    key('j');
    expect(title()).toBe('Synthetic countertop fail');
    expect(screen.getByText(/Decided: Confirmed/)).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'History' }));
    const list = await screen.findByRole('list', { name: 'Decisions, newest first' }, { timeout: 5000 });
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent?.split('Reviewer')[0])).toEqual(['Confirmedlatest', 'Dismissed']);
    await user.keyboard('{Escape}');
    await user.click(screen.getByRole('button', { name: 'Change decision' }));
    key('3');
    await user.type(screen.getByLabelText(/Why are you dismissing this failure\?/), 'TEST changed my mind');
    await user.click(screen.getByRole('button', { name: /Record decision/ }));
    await waitFor(() => expect(props.handlers.onAction).toHaveBeenCalledWith('f-fail', 'dismiss', 'TEST changed my mind'));
  });

  it('when nothing is left: "All decisions made", then the next action', async () => {
    const user = userEvent.setup();
    const { props, rerender } = setup();
    rerender({ rows: ROWS.map((r) => ({ ...r, needs_decision: false })), blocking: new Set() });
    expect(await screen.findByText('All decisions made', undefined, { timeout: 5000 })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Sign off' }));
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
    expect(props.onAct).toHaveBeenCalledWith('sign-off');
  });

  it('finishing the last item shows the done state, even after moving around with J / K', async () => {
    const { rerender } = setup();
    key('j');
    key('k');
    rerender({ rows: ROWS.map((r) => ({ ...r, needs_decision: false })), blocking: new Set() });
    expect(await screen.findByText('All decisions made', undefined, { timeout: 5000 })).toBeTruthy();
  });

  it('with nothing to decide at all, it says so', async () => {
    setup({ rows: ROWS.map((r) => ({ ...r, needs_decision: false })), blocking: new Set() });
    expect(await screen.findByText('Nothing needs you', undefined, { timeout: 5000 })).toBeTruthy();
  });

  it('a wall answer waiting for a run makes the done state ask for a check run', async () => {
    const user = userEvent.setup();
    const { props, rerender } = setup({ rows: ROWS.map((r) => (r.row_id === 'walls' ? r : { ...r, needs_decision: false })), blocking: new Set(['f-walls']) });
    await user.click(await screen.findByRole('radio', { name: 'Island; no wall ends' }, { timeout: 5000 }));
    await user.click(screen.getByRole('button', { name: 'Use this wall layout' }));
    rerender({});
    expect(await screen.findByText('All decisions made', undefined, { timeout: 5000 })).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run checks' }));
    expect(props.onAct).toHaveBeenCalledWith('run-checks');
  });
  it('a wall choice made on one countertop never carries over to the next (review fix)', async () => {
    const user = userEvent.setup();
    // Two countertops with wall questions: the first and the FAIL one (readers' proposal).
    slotRows = [BASE_SLOTS[0], BASE_SLOTS[1], slot('fail', { wall_source: 'readers', wall_proposal: 'back_left_right' })];
    setup();
    const first = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    await user.click(within(first).getByRole('radio', { name: 'Island; no wall ends' }));
    key('j');
    key('j');
    expect(title()).toBe('Synthetic countertop fail');
    const next = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    expect(within(next).getAllByRole('radio').every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    expect(screen.getByRole('button', { name: 'Use this wall layout' })).toHaveProperty('disabled', true);
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
  });

  it('a wall at one end only is a choice the reviewer can make and send (#1138)', async () => {
    const user = userEvent.setup();
    slotRows = [slot('walls', { wall_layout_choices: ['back_left_right', 'back_and_left', 'back_and_right', 'back_only', 'island'], wall_source: 'readers', wall_proposal: 'back_and_right' }), BASE_SLOTS[1]];
    setup();
    const walls = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    expect(within(walls).getAllByRole('radio').map((r) => r.textContent)).toEqual(['Back wall and both ends', 'Back wall and left end', 'Back wall and right end', 'Back wall only', 'Island; no wall ends']);
    // The readers' proposal is words only: nothing is pre-selected or sent without the click.
    expect(screen.getByText('The readers propose: back wall and right end. Not confirmed.')).toBeTruthy();
    expect(within(walls).getAllByRole('radio').every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    await user.click(within(walls).getByRole('radio', { name: 'Back wall and right end' }));
    expect(calls.some((c) => c.method === 'POST')).toBe(false);
    await user.click(screen.getByRole('button', { name: 'Use this wall layout' }));
    await waitFor(() => expect(calls.find((c) => c.method === 'POST')).toMatchObject({ url: expect.stringContaining('/slot-rows/walls/review'), body: '{"wall_config":"back_and_right"}' }));
  });

  it('the between-panels item also offers the other layouts', async () => {
    setup();
    key('j');
    const choices = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    expect(within(choices).getAllByRole('radio').map((r) => r.textContent)).toEqual(['Back wall and both ends', 'Back wall only', 'Island; no wall ends']);
    expect(screen.getByRole('button', { name: 'No field cut: the stone stops at panels' })).toBeTruthy();
  });

  it('a half-written decision is dropped when the result under it is replaced (review fix)', async () => {
    const user = userEvent.setup();
    const { props, rerender } = setup();
    key('3');
    await user.type(screen.getByLabelText(/Why is this not checkable\?/), 'TEST half written');
    // A check run finishes while the queue is open: the countertop now has a new result.
    const fresh = finding('f-walls-2');
    rerender({ rows: ROWS.map((r) => (r.row_id === 'walls' ? { ...r, finding_id: 'f-walls-2' } : r)), findings: [...FINDINGS, fresh], blocking: new Set([...BLOCKING, 'f-walls-2']) });
    expect(screen.queryByLabelText(/Why is this not checkable\?/)).toBeNull();
    expect(screen.getAllByRole('radio', { name: /Checked: OK|Problem|Not checkable/ }).every((r) => r.getAttribute('aria-checked') === 'false')).toBe(true);
    key('Enter');
    expect(props.handlers.onAction).not.toHaveBeenCalled();
  });

  it('waits for the readiness answer before taking the list, so no check is left out (review fix)', async () => {
    const { rerender } = setup({ blocking: null });
    expect(screen.getByText('Loading what needs you…')).toBeTruthy();
    rerender({ blocking: BLOCKING });
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toContain('0 of 4');
  });

  it('a finding corrected earlier waits for a run, whatever was recorded after (review fix)', async () => {
    history = { items: [
      { id: 'a2', review_session_id: 's', finding_id: 'f-fail', package_revision_id: 'r', action: 'confirm', actor: 'Reviewer B', note: null, created_at: '2026-01-02T10:00:00Z' },
      { id: 'a1', review_session_id: 's', finding_id: 'f-fail', package_revision_id: 'r', action: 'correct', actor: 'Reviewer A', note: null, created_at: '2026-01-01T10:00:00Z' },
    ] };
    const findings = FINDINGS.map((f) => (f.id === 'f-fail' ? { ...f, reviewer_action: 'confirm' as const } : f));
    const rows = ROWS.map((r) => (r.row_id === 'fail' ? { ...r, reviewer_decision: { action: 'confirm', note: null, actor: 'Reviewer B', time: '2026-01-02T10:00:00Z' } } : r));
    setup({ findings, rows });
    key('j');
    key('j');
    expect(await screen.findByText(/Corrected\. A correction is settled only by running the checks again/, undefined, { timeout: 5000 })).toBeTruthy();
    expect(document.querySelector('[data-slot="queue-decision"]')).toBeNull();
  });

  it('follows the readiness answer as soon as it is in, before the rows reload (review fix)', async () => {
    const { rerender } = setup();
    key('j');
    key('j');
    expect(title()).toBe('Synthetic countertop fail');
    // Saved: readiness no longer blocks it, but the countertop rows have not reloaded yet.
    rerender({ blocking: new Set(['f-walls', 'f-panels', 'f-sink']) });
    expect(document.querySelector('[data-slot="queue-item"]')?.getAttribute('data-status')).toBe('decided');
    expect(document.querySelector('[data-slot="queue-decision"]')).toBeNull();
  });

  it('never says "all done" while the server still blocks on results it does not hold (review fix)', async () => {
    const user = userEvent.setup();
    const { rerender } = setup();
    const later = finding('f-new', { outcome: 'NOT_FOUND', scope_label: 'Package revision' });
    rerender({ rows: ROWS.map((r) => ({ ...r, needs_decision: false })), findings: [...FINDINGS, later], blocking: new Set(['f-new']) });
    expect(screen.queryByText('All decisions made')).toBeNull();
    expect(screen.getByText('The results changed')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Load them' }));
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toContain('0 of 1');
  });

  it('waits for the countertop rows too, so a countertop never shows up as a package check', async () => {
    const { rerender } = setup({ rows: [], rowsReady: false });
    expect(screen.getByText('Loading what needs you…')).toBeTruthy();
    rerender({ rows: ROWS, rowsReady: true });
    expect(document.querySelector('[data-slot="queue-progress"]')?.textContent).toContain('0 of 4');
  });

  it('a result the screen does not hold yet says to refresh, instead of looping on "Load them"', async () => {
    const { rerender } = setup();
    rerender({ rows: ROWS.map((r) => ({ ...r, needs_decision: false })), blocking: new Set(['f-elsewhere']) });
    expect(document.querySelector('[data-slot="queue-changed"]')?.textContent).toContain('1 result still blocks sign-off but is not in the results on screen yet');
    expect(screen.queryByRole('button', { name: 'Load them' })).toBeNull();
    expect(screen.queryByText('All decisions made')).toBeNull();
  });

  it('J / K wait while a wall answer is being saved, so its outcome is seen', async () => {
    const user = userEvent.setup();
    let release: () => void = () => undefined;
    const fetchMock = vi.mocked(fetch);
    const original = fetchMock.getMockImplementation()!;
    fetchMock.mockImplementation(async (input, init) => {
      if (init?.method === 'POST') await new Promise<void>((resolve) => { release = resolve; });
      return original(input, init);
    });
    setup();
    const walls = await screen.findByRole('radiogroup', { name: 'Wall layout' }, { timeout: 5000 });
    await user.click(within(walls).getByRole('radio', { name: 'Back wall only' }));
    await user.click(screen.getByRole('button', { name: 'Use this wall layout' }));
    key('j');
    expect(title()).toBe('Synthetic countertop walls');
    await act(async () => release());
  });

  it('J / K wait while a decision is being saved (review fix)', async () => {
    const user = userEvent.setup();
    let finish: (value: { saved: true }) => void = () => undefined;
    const onAction = vi.fn(() => new Promise<{ saved: true }>((resolve) => { finish = resolve; }));
    setup({ handlers: { onAction, onCorrect: vi.fn(), onExcept: vi.fn() } });
    key('3');
    await user.type(screen.getByLabelText(/Why is this not checkable\?/), 'TEST saving');
    fireEvent.keyDown(screen.getByLabelText(/Why is this not checkable\?/), { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(onAction).toHaveBeenCalled());
    key('j');
    expect(title()).toBe('Synthetic countertop walls');
    await act(async () => finish({ saved: true }));
  });
});
