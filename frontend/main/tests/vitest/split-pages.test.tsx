// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ArchitectResult, CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import { DrawingViewerSheet } from '@/components/drawing/drawing-viewer';
import { bucketOf, isSplitPage, kpis, matchesFilter } from '@/lib/countertop-results';
import { buildQueue } from '@/lib/needs-you-queue';
import { targetFromRow } from '@/lib/drawing-viewer';

// The dashboard asks the rulebook for names; keep it off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic data only (#1103): nothing here comes from a client drawing.
const VERSION = '00000000-0000-4000-8000-0000000000bb';
const box = (x0: number, y0: number, x1: number, y1: number) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map(String));
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const NO_ROW_CHOSEN = "Not compared: no countertop line was chosen on this page, so nothing was read to compare with the architect's drawing.";
const SPLIT_REASON = 'Synthetic: the two AIs did not agree on this page\'s countertop line (one AI picked no countertop line; the other picked line 4), so nothing on it was read or checked. The reviewer decides this page.';
const notChosen: ArchitectResult = { outcome: null, finding_id: null, reason: null, needs_decision: false, compared: [], not_compared_reason: NO_ROW_CHOSEN, pairing_source: null, pairing_judgments: null };

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: { coordinate_space: 'stored', document_version_id: VERSION, page_id: `page-${page}`, page_number: page, polygon: box(0.4, 0.8, 0.6, 0.9) },
    outcome: 'PASS', needs_decision: false, printed_overall: x('88', '88"'),
    pieces: [{ index: 0, value: x('88', '88"'), source: 'sealed', kind: null }],
    field_cut_per_end: x('1', '1"'), field_cut_count: 2, expected_total: x('88', '88"'), delta: x('0', '0"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: 'back_only', label: 'back wall only', source: 'drawing clues' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
    ...overrides,
  } as CountertopResult;
}

/** A page the two AIs split on, exactly as the API sends it (#1096). */
const SPLIT = row('split', 3, {
  label: 'Synthetic page 3', row_location: null, outcome: 'REVIEW_REQUIRED', needs_decision: true,
  printed_overall: null, pieces: [], field_cut_per_end: null, field_cut_count: null, expected_total: null, delta: null,
  wall_layout: { config: null, label: null, source: 'not established' },
  agreement: { both_readers_agreed_on_row: false, code_clue_used: false, values_agreed: [] },
  hold: { code: 'row-choice-split', reason: SPLIT_REASON },
  architect: notChosen,
});
// A countertop with a chosen line has its own reason, so the split page keeps its own Architect line.
const PASSED = row('ok', 5, { architect: { ...notChosen, not_compared_reason: 'Synthetic: the architect prints only centre lines on this page.' } });
const NO_COUNTERTOP = [
  { page_number: 15, reason: 'Synthetic: both AIs saw only tall units on this page.' },
  { page_number: 17, reason: 'Synthetic: both AIs saw a cover sheet.' },
];
const finding = (id: string, outcome: Finding['outcome']): Finding =>
  ({ id, check_id: 'CT-WIDTH-001', name: 'Synthetic width', severity: 'FLAG', outcome, reviewer_action: null, created_at: '2026-10-09T10:00:00Z' }) as Finding;
const FINDINGS = [finding('f-split', 'REVIEW_REQUIRED'), finding('f-ok', 'PASS')];

describe('a split page, as data', () => {
  it('is a held item that needs you, with no line, so no outline and no document guessed', () => {
    expect(isSplitPage(SPLIT)).toBe(true);
    expect(isSplitPage(PASSED)).toBe(false);
    expect(isSplitPage(row('held', 2, { hold: { code: 'stone-short-of-ends', reason: 'Synthetic.' } }))).toBe(false);
    expect(bucketOf(SPLIT)).toBe('needs-you');
    expect(matchesFilter(SPLIT, 'held')).toBe(true);
    expect(buildQueue([SPLIT, PASSED], FINDINGS, new Set(['f-split'])).map((item) => [item.kind, item.kind === 'check' ? null : item.rowId])).toEqual([['countertop', 'split']]);
    // The page, but no box: the server's own page picture (the shop drawing's), with nothing drawn on it.
    expect(targetFromRow(SPLIT)).toMatchObject({ page: 3, documentVersionId: null, outline: null, findingId: 'f-split' });
  });
});

function dashboard(state: Partial<Extract<CountertopsState, { status: 'ready' }>> = {}) {
  const onShowDrawing = vi.fn();
  const onOpenCard = vi.fn();
  render(
    <ResultsDashboard
      countertops={{ status: 'ready', rows: [SPLIT, PASSED], ...state } as CountertopsState}
      findings={FINDINGS}
      blockingIds={new Set(['f-split'])}
      busy={false}
      filter="all"
      onFilterChange={() => {}}
      onRetry={() => {}}
      onRefresh={() => {}}
      handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
      onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
      onShowDrawing={onShowDrawing}
      onOpenCard={onOpenCard}
      onOpenQueue={vi.fn()}
    />,
  );
  return { onShowDrawing, onOpenCard };
}

describe('Results: a split page', () => {
  it('shows its reason in the row, offers Show on drawing and Decide, and no countertop card', async () => {
    const user = userEvent.setup();
    const { onShowDrawing } = dashboard();
    const table = document.querySelector('[data-slot="countertop-table"] table')!;
    const tr = table.querySelector<HTMLTableRowElement>('tr[data-row-id="split"]')!;
    expect(tr.querySelector('[data-hold="row-choice-split"]')?.textContent).toContain('No line chosen');
    expect(tr.querySelector('[data-slot="split-reason"]')?.textContent).toBe(SPLIT_REASON);
    expect(within(tr).queryByRole('button', { name: 'Open countertop card' })).toBeNull();
    const show = within(tr).getByRole('button', { name: 'Show on drawing' });
    expect(show.hasAttribute('disabled')).toBe(false);
    await user.click(show);
    expect(onShowDrawing).toHaveBeenCalledWith(SPLIT);
    // The architect's line says "Not compared" once, in the server's words.
    expect(table.querySelector('tr[data-architect-row="split"]')?.textContent).toBe(`Architect${NO_ROW_CHOSEN}`);
    // No walls to ask about: no line was chosen, and there is no countertop card to choose them on.
    expect(tr.querySelector('[data-slot="split-walls"]')?.textContent).toBe('—');
    // The reason is read once to a screen reader: it is on screen, so the chip does not repeat it.
    expect(tr.querySelector('[data-hold="row-choice-split"] .sr-only')).toBeNull();
    // A held countertop's Decide dialog: checked, or not checkable with a note. Nothing was read,
    // so there is no value to correct and no "Problem".
    await user.click(within(tr).getByRole('button', { name: 'Decide' }));
    const dialog = await screen.findByRole('dialog', { name: /Synthetic page 3/ });
    const choices = within(dialog).getAllByRole('radio').map((r) => r.textContent);
    expect(choices).toEqual(['Checked: OK', 'Not checkable']);
  });

  it('its details have no countertop picture: they say no line was chosen', async () => {
    const user = userEvent.setup();
    dashboard();
    const tr = document.querySelector<HTMLTableRowElement>('[data-slot="countertop-table"] tr[data-row-id="split"]')!;
    await user.click(within(tr).getByRole('button', { name: 'Show details' }));
    const details = tr.nextElementSibling?.nextElementSibling as HTMLElement; // the architect line sits between
    expect(details.querySelector('[data-slot="countertop-strip"]')).toBeNull();
    expect(details.querySelector('[data-slot="split-page"]')?.textContent).toContain('No countertop line chosen');
    expect(details.textContent).toContain(SPLIT_REASON);
    expect(details.textContent).not.toContain('No pieces read');
    // A countertop with a line keeps its picture.
    const ok = document.querySelector<HTMLTableRowElement>('[data-slot="countertop-table"] tr[data-row-id="ok"]')!;
    await user.click(within(ok).getByRole('button', { name: 'Show details' }));
    expect(document.querySelectorAll('[data-slot="countertop-table"] table [data-slot="countertop-strip"]')).toHaveLength(1);
  });

  it('on a phone: the reason in the card, no countertop card button, no picture', () => {
    dashboard();
    const card = within(screen.getByRole('list', { name: 'Countertops' })).getAllByRole('listitem').find((li) => li.textContent?.includes('Synthetic page 3'))!;
    expect(card.querySelector('[data-slot="split-page"]')?.textContent).toContain(SPLIT_REASON);
    expect(within(card).queryByRole('button', { name: 'Open countertop card' })).toBeNull();
    expect(card.querySelector('[data-slot="countertop-strip"]')).toBeNull();
  });
});

describe('a split page whose checks have not run yet', { timeout: 15_000 }, () => {
  it('in the queue it says to run the checks, with no countertop card and no decision form', () => {
    const unchecked = { ...SPLIT, finding_id: null, outcome: null };
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={[unchecked]} rowsReady findings={[]} blocking={new Set()}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'run-checks', label: 'Run checks', disabled: false, reason: null }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={vi.fn()}
      />,
    );
    const item = document.querySelector<HTMLElement>('[data-slot="queue-item"]')!;
    expect(item.querySelector('[data-slot="split-unchecked"]')?.textContent).toBe('Not checked yet. Run the checks; then this page needs your decision.');
    expect(within(item).queryByRole('button', { name: 'Open countertop card' })).toBeNull();
    expect(item.querySelector('[data-slot="queue-decision"]')).toBeNull();
  });
});

describe('Results: pages with no countertop found', () => {
  it('lists each page with the AI\'s reason; nothing to click, and it changes no count', () => {
    dashboard({ pagesWithoutCountertop: NO_COUNTERTOP });
    const list = screen.getByRole('region', { name: 'Pages with no countertop found' });
    const items = within(list).getAllByRole('listitem');
    expect(items.map((li) => li.textContent)).toEqual([
      'Page 15Synthetic: both AIs saw only tall units on this page.',
      'Page 17Synthetic: both AIs saw a cover sheet.',
    ]);
    expect(within(list).queryAllByRole('button')).toHaveLength(0);
    expect(list.textContent).toContain('Nothing to decide');
    // Not blocking: the counts are the countertops' only.
    expect(kpis([SPLIT, PASSED]).needsYou).toBe(1);
    expect(screen.getByRole('button', { name: 'Needs you 1: review them one at a time' })).toBeTruthy();
  });

  it('is absent when the list is empty or not sent', () => {
    dashboard({ pagesWithoutCountertop: [] });
    expect(screen.queryByRole('region', { name: 'Pages with no countertop found' })).toBeNull();
  });

  it('is absent when the API sent no list at all', () => {
    dashboard();
    expect(screen.queryByRole('region', { name: 'Pages with no countertop found' })).toBeNull();
  });

  it('is shown even when no countertop was found anywhere, and the empty state says so', () => {
    dashboard({ rows: [], pagesWithoutCountertop: NO_COUNTERTOP });
    expect(screen.getByRole('region', { name: 'Pages with no countertop found' })).toBeTruthy();
    expect(screen.getByText('No countertop found on any page')).toBeTruthy();
    expect(screen.queryByText(/Run checks from Measurements/)).toBeNull();
  });
});

// The page picture and the finding chain, as the viewer asks for them.
const calls: string[] = [];
const png = () => new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { status: 200, headers: { 'Content-Type': 'image/png' } });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    calls.push(url);
    if (/\/pages\/\d+\/picture/.test(url)) return png();
    if (url.includes('/chain')) return json({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: SPLIT_REASON } });
    if (url.endsWith('/slot-rows')) return json({ rows: [] });
    if (url.endsWith('/actions')) return json({ items: [] });
    if (url.endsWith('/rules')) return json([]);
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 404);
  }));
  let n = 0;
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:test-${++n}`, revokeObjectURL: () => undefined }));
});
afterEach(() => vi.unstubAllGlobals());

describe('Show on drawing: a split page', { timeout: 15_000 }, () => {
  it('opens its page with no outline, says no line was chosen, and draws no countertop picture', async () => {
    render(<DrawingViewerSheet target={targetFromRow(SPLIT)} opening={1} rows={[SPLIT, PASSED]} projectId="p" packageId="k" onTargetChange={vi.fn()} onClose={vi.fn()} />);
    const sheet = await screen.findByRole('dialog');
    expect(within(sheet).getByText(`No line chosen: ${SPLIT_REASON}`)).toBeTruthy();
    await waitFor(() => expect(calls.some((url) => /\/pages\/3\/picture$/.test(url))).toBe(true));
    // The page by number only: the server gives the shop drawing's page; no document is guessed.
    expect(calls.filter((url) => url.includes('/picture')).every((url) => !url.includes('document_version_id'))).toBe(true);
    expect(sheet.querySelector('[data-outline]')).toBeNull();
    // Once the page is placed, the drawing itself says why nothing is outlined.
    const img = await waitFor(() => {
      const found = sheet.querySelector<HTMLImageElement>('[data-slot="drawing-page"] img');
      if (!found) throw new Error('no page picture yet');
      return found;
    }, { timeout: 5000 });
    Object.defineProperty(img, 'naturalWidth', { value: 2000, configurable: true });
    Object.defineProperty(img, 'naturalHeight', { value: 1000, configurable: true });
    fireEvent.load(img);
    expect((await waitFor(() => sheet.querySelector('[data-slot="no-outline"]')!))?.textContent).toBe('No line chosen: nothing is outlined');
    expect(sheet.querySelector('[data-slot="countertop-strip"]')).toBeNull();
    expect(sheet.querySelector('[data-slot="split-page"]')?.textContent).toContain('No countertop line chosen');
    expect(within(sheet).getByRole('button', { name: /Find outline/ }).hasAttribute('disabled')).toBe(true);
  });
});

describe('the "Needs you" queue: a split page', { timeout: 15_000 }, () => {
  it('shows its reason, no picture and no wall question, and takes "not checkable" with a note', async () => {
    const onAction = vi.fn(async () => ({ saved: true as const }));
    const onOpenCard = vi.fn();
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={[SPLIT, PASSED]} rowsReady findings={FINDINGS} blocking={new Set(['f-split'])}
        projectId="p" packageId="k"
        handlers={{ onAction, onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'sign-off', label: 'Sign off', disabled: true, reason: 'Synthetic.' }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onOpenCard={onOpenCard}
      />,
    );
    const item = document.querySelector<HTMLElement>('[data-slot="queue-item"]')!;
    expect(item.querySelector('h2')?.textContent).toBe('Synthetic page 3');
    expect(item.querySelector('[data-slot="split-page"]')?.textContent).toContain(SPLIT_REASON);
    expect(item.querySelector('[data-slot="countertop-strip"]')).toBeNull();
    expect(item.querySelector('[data-slot="queue-facts"]')).toBeNull();
    expect(within(item).queryByRole('radiogroup', { name: /walls/i })).toBeNull();
    expect(within(item).queryByRole('button', { name: 'Open countertop card' })).toBeNull();
    // The drawing: its page, with nothing outlined.
    await waitFor(() => expect(calls.some((url) => /\/pages\/3\/picture$/.test(url))).toBe(true));
    expect(item.querySelector('[data-outline]')).toBeNull();

    const form = document.querySelector<HTMLElement>('[data-slot="queue-decision"]')!;
    expect(within(form).getAllByRole('radio').map((r) => r.textContent)).toEqual(['1Checked: OK', '3Not checkable']);
    act(() => void fireEvent.keyDown(document.body, { key: '2' })); // no "Problem" here
    expect(within(form).queryByText('Correct a value')).toBeNull();
    act(() => void fireEvent.keyDown(document.body, { key: '3' }));
    const note = await screen.findByLabelText(/Why is this not checkable\?/);
    await userEvent.type(note, 'TEST the reviewer checked this page by hand');
    fireEvent.keyDown(note, { key: 'Enter', ctrlKey: true });
    await waitFor(() => expect(onAction).toHaveBeenCalledWith('f-split', 'dismiss', 'TEST the reviewer checked this page by hand'));
    expect(onOpenCard).not.toHaveBeenCalled();
  });
});
