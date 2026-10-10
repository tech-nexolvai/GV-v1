// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import {
  boundsOf,
  fitView,
  focusView,
  noCropsReason,
  outlineOf,
  pagesOf,
  readingsOf,
  scaleLimits,
  targetFromFinding,
  targetFromRow,
  toScreen,
  zoomAt,
  type ViewerTarget,
} from '@/lib/drawing-viewer';
import { DrawingViewerSheet } from '@/components/drawing/drawing-viewer';

// Synthetic data only: nothing here comes from a client drawing.
const VERSION = '00000000-0000-4000-8000-0000000000aa';
const box = (x0: number, y0: number, x1: number, y1: number) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map(String));
const location = (page: number, polygon: string[][]) => ({ coordinate_space: 'stored', document_version_id: VERSION, page_id: `page-${page}`, page_number: page, polygon });
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `finding-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: location(page, box(0.4, 0.8, 0.6, 0.9)),
    outcome: 'FAIL', needs_decision: true,
    printed_overall: x('42', '42"'),
    pieces: [{ index: 0, value: x('20', '20"'), kind: 'cabinet', source: 'sealed' }, { index: 1, value: x('20', '20"'), kind: 'filler', source: 'sealed' }],
    field_cut_per_end: x('1', '1"'), field_cut_count: 0, expected_total: x('40', '40"'), delta: x('2', '2"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: 'back_only', label: 'back wall only', source: 'drawing clues' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [true, true] },
    ...overrides,
  };
}

const A = row('a', 12);
const B = row('b', 12, { outcome: 'PASS', needs_decision: false, row_location: location(12, box(0.1, 0.1, 0.3, 0.2)), delta: x('0', '0"') });
const C = row('c', 7, { outcome: 'REVIEW_REQUIRED', hold: { code: 'stone-into-walls', reason: 'Synthetic hold reason.' }, row_location: location(7, box(0.2, 0.2, 0.5, 0.4)) });
const ROWS = [A, B, C];

const evidence = (id: string, page: number, polygon: string[][]) => ({
  canonical_observation_id: id, document_version_id: VERSION, page_id: `page-${page}`, page_index: page - 1, polygon,
  coordinate_space: 'stored', crop_uri: `crop://${id}`, document_role: 'SHOP', semantic_type: 'countertop_piece_width', authority: 'AUTHORITATIVE',
});
const CHAIN_A = {
  operands: [
    { name: 'countertop_width', numerator: '42', denominator: '1', unit: 'in', evidence_status: 'CORROBORATED', evidence: evidence('obs-overall', 12, box(0.4, 0.85, 0.6, 0.9)) },
    { name: 'piece_widths[0]', numerator: '41', denominator: '2', unit: 'in', evidence_status: 'CORROBORATED', evidence: evidence('obs-p0', 12, box(0.4, 0.8, 0.5, 0.85)) },
    { name: 'piece_widths[1]', numerator: '20', denominator: '1', unit: 'in', evidence_status: 'CORROBORATED', evidence: evidence('obs-p1', 12, box(0.5, 0.8, 0.6, 0.85)) },
    { name: 'field_cut', numerator: '1', denominator: '1', unit: 'in', evidence_status: 'USER_INPUT', evidence: null },
  ],
  trace: {
    kind: 'calculation' as const, operation: 'equals', operation_version: '1', engine_version: 'test', outcome: 'FAIL', intermediates: [] as [string, string][], comparison: '42 in != 40 in',
    operands: [
      { name: 'countertop_width', value: '42 in', source: 'SHOP', evidence_ref: null },
      { name: 'piece_widths[0]', value: '20 1/2 in', source: 'SHOP', evidence_ref: null },
    ],
  },
};
const CHAIN_C = { operands: [], trace: { kind: 'abstention' as const, cause: 'held', reason: 'The check did not run.' } };

describe('drawing viewer: geometry', () => {
  const page = { w: 2000, h: 1000 };
  const area = { w: 800, h: 560 };

  it('reads a stored 0–1 outline, and refuses anything else rather than guessing', () => {
    expect(outlineOf(box(0.4, 0.8, 0.6, 0.9))).toEqual([[0.4, 0.8], [0.6, 0.8], [0.6, 0.9], [0.4, 0.9]]);
    expect(outlineOf([['0.1', '0.1'], ['1.4', '0.2'], ['0.2', '0.3']])).toBeNull();
    expect(outlineOf([['0.1', '0.1'], ['0.2', '0.2']])).toBeNull();
    expect(outlineOf(null)).toBeNull();
  });

  it('places the outline on the picture: 0–1 points land at the right pixels when fitted', () => {
    const view = fitView(page, area);
    expect(view.scale).toBeCloseTo((800 - 32) / 2000, 9);
    const [sx, sy] = toScreen(view, page, [0.5, 0.5]);
    expect(sx).toBeCloseTo(400, 6);
    expect(sy).toBeCloseTo(280, 6);
  });

  it('zooming keeps the point under the cursor where it is, so the outline stays on its drawing', () => {
    const view = fitView(page, area);
    const limits = scaleLimits(page, area);
    const corner: [number, number] = [0.4, 0.8];
    const anchor = toScreen(view, page, corner);
    const zoomed = zoomAt(view, 2.5, { x: anchor[0], y: anchor[1] }, limits);
    expect(zoomed.scale).toBeCloseTo(view.scale * 2.5, 9);
    const after = toScreen(zoomed, page, corner);
    expect(after[0]).toBeCloseTo(anchor[0], 6);
    expect(after[1]).toBeCloseTo(anchor[1], 6);
    // And never past the limits.
    expect(zoomAt(view, 1e6, { x: 0, y: 0 }, limits).scale).toBe(limits.max);
    expect(zoomAt(view, 1e-6, { x: 0, y: 0 }, limits).scale).toBe(limits.min);
  });

  it('"Find outline" centres the outline in the viewport', () => {
    const outline = outlineOf(box(0.4, 0.8, 0.6, 0.9))!;
    const view = focusView(boundsOf(outline), page, area, scaleLimits(page, area), 0.5);
    const [cx, cy] = toScreen(view, page, [0.5, 0.85]);
    expect(cx).toBeCloseTo(400, 6);
    expect(cy).toBeCloseTo(280, 6);
  });
});

describe('drawing viewer: what is shown', () => {
  it('colours a row by its recorded result, with "needs you" beside it, as the results table does', () => {
    expect(targetFromRow(A)).toMatchObject({ tone: 'fail', glyph: 'FAIL', word: 'Needs correction', needsYou: true, page: 12 });
    expect(targetFromRow(B)).toMatchObject({ tone: 'pass', word: 'Looks right', needsYou: false });
    expect(targetFromRow(C)).toMatchObject({ tone: 'review', glyph: 'REVIEW_REQUIRED', needsYou: true });
    expect(targetFromRow({ ...C, needs_decision: false })).toMatchObject({ tone: 'missing', word: 'Not checkable' });
    expect(targetFromRow({ ...C, outcome: null, finding_id: null })).toMatchObject({ word: 'Not checked' });
    // The table's own words for the other recorded results, with "needs you" kept separate.
    expect(targetFromRow({ ...C, outcome: 'NOT_FOUND' })).toMatchObject({ tone: 'missing', glyph: 'NOT_FOUND', word: 'Waiting on a value', needsYou: true });
    expect(targetFromRow({ ...C, outcome: 'NO_APPLICABLE_RULE', needs_decision: false })).toMatchObject({ word: 'Not applicable', needsYou: false });
  });

  it('a finding is placed by its row outline, else by its shop reading', () => {
    const base = { id: 'f', name: 'rule', scope_label: null, outcome: 'FAIL' as const, row_location: null, arch_evidence: null };
    const shop = { canonical_observation_id: 'o', document_version_id: VERSION, page: 3, polygon: [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]] as [number, number][], semantic_type: 's' };
    expect(targetFromFinding({ ...base, shop_evidence: shop })).toMatchObject({ page: 3, documentVersionId: VERSION, outline: [[0.1, 0.1], [0.2, 0.1], [0.2, 0.2]] });
    expect(targetFromFinding({ ...base, shop_evidence: null })).toMatchObject({ page: null, outline: null });
  });

  it('the page strip lists located pages in order, each with what needs you first', () => {
    const pages = pagesOf(ROWS, null);
    expect(pages.map((p) => p.page)).toEqual([7, 12]);
    expect(pages[1].targets.map((t) => t.key)).toEqual(['a', 'b']);
  });

  it('readings carry the exact value text, a plain name and where they were read', () => {
    const readings = readingsOf(CHAIN_A, A);
    expect(readings.map((r) => [r.label, r.value, r.page])).toEqual([
      ['Printed overall', '42"', 12],
      ['Cabinet 1', '20 1/2"', 12],
      ['Filler 2', '20"', 12],
    ]);
    // A typed value has no crop and is not shown as one.
    expect(readings.find((r) => r.name === 'field_cut')).toBeUndefined();
  });

  it('says in one line why there are no crops', () => {
    // A held result used no reading: said so, with the hold reason on screen (#1126).
    expect(noCropsReason(CHAIN_C, C).line).toBe('No readings used');
    expect(noCropsReason(CHAIN_C, C).reason).toBe('Synthetic hold reason.');
    expect(noCropsReason(CHAIN_C, C).why).toContain('held');
    // Only a hold is called "held": another abstention (a budget stop) is said neutrally.
    const budget = { operands: [], trace: { kind: 'abstention' as const, cause: 'budget', reason: 'Synthetic: the model budget ran out.' } };
    expect(noCropsReason(budget, { hold: null, finding_id: 'f' })).toEqual({ line: 'No readings used', why: 'No reading was used for this result, so there is nothing to crop.', reason: 'Synthetic: the model budget ran out.' });
    expect(noCropsReason(null, { hold: null, finding_id: null }).line).toBe('Not checked yet');
  });
});

// ── The viewer on screen ──────────────────────────────────────

const calls: { method: string; url: string }[] = [];
// Raw bytes, not jsdom's Blob: how Node's Response takes a jsdom Blob differs between Node versions.
const png = () => new Response(new Uint8Array([0x89, 0x50, 0x4e, 0x47]), { status: 200, headers: { 'Content-Type': 'image/png' } });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let notReady = new Set<number>();
let missingPages = new Set<number>();

beforeEach(() => {
  calls.length = 0;
  notReady = new Set([7]);
  missingPages = new Set();
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ method: init?.method ?? 'GET', url });
    const picture = /\/pages\/(\d+)\/picture/.exec(url);
    if (picture) {
      if (missingPages.has(Number(picture[1]))) return json({ error: 'http_error', message: 'Not found', request_id: 'r' }, 404);
      return notReady.has(Number(picture[1]))
        ? json({ error: 'not_found', message: 'the page picture is not ready yet', request_id: 'r' }, 404)
        : png();
    }
    if (url.includes('/findings/finding-a/chain')) return json(CHAIN_A);
    if (url.includes('/chain')) return json(CHAIN_C);
    if (url.includes('/crop')) return png();
    return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
  }));
  let n = 0;
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:test-${++n}`, revokeObjectURL: () => undefined }));
});

afterEach(() => vi.unstubAllGlobals());

function open(target: ViewerTarget, onClose = vi.fn(), rows: CountertopResult[] = ROWS) {
  let current = target;
  const onTargetChange = vi.fn((next: ViewerTarget) => {
    current = next;
    view.rerender(<DrawingViewerSheet target={current} opening={1} rows={rows} projectId="p" packageId="k" onTargetChange={onTargetChange} onClose={onClose} />);
  });
  const view = render(<DrawingViewerSheet target={current} opening={1} rows={rows} projectId="p" packageId="k" onTargetChange={onTargetChange} onClose={onClose} />);
  return { onTargetChange, onClose };
}

/** jsdom does not load images: report a natural size as the browser would. */
async function loadPicture(w = 2000, h = 1000) {
  // Looked up directly: a role query over the whole sheet is slow in jsdom. CI runners are slower.
  const img = await waitFor(() => {
    const found = document.querySelector<HTMLImageElement>('[data-slot="drawing-page"] img');
    if (!found) throw new Error(`no page picture yet; the canvas says: ${document.querySelector('[data-slot="drawing-canvas"]')?.textContent || '(nothing)'}`);
    return found;
  }, { timeout: 5000 });
  expect(img.alt).toMatch(/^Vendor drawing, page \d+$/);
  Object.defineProperty(img, 'naturalWidth', { value: w, configurable: true });
  Object.defineProperty(img, 'naturalHeight', { value: h, configurable: true });
  fireEvent.load(img);
  return img;
}

const pageBox = () => document.querySelector<HTMLElement>('[data-slot="drawing-page"]')!;
const zoomLevel = () => document.querySelector('[data-slot="zoom-level"]')!.textContent;

// CI runners are several times slower than a laptop; the waits above allow for it.
describe('drawing viewer: on screen', { timeout: 15_000 }, () => {
  it('draws the outline at its stored polygon over the page picture, and zooming keeps it there', async () => {
    open(targetFromRow(A));
    await loadPicture();
    const outline = document.querySelector('[data-outline="a"]')!;
    expect(outline.getAttribute('points')).toBe('0.4,0.8 0.6,0.8 0.6,0.9 0.4,0.9');
    // The outline's SVG is the page's own 0–1 square, laid over the picture box it sits in.
    const svg = outline.closest('svg')!;
    expect(svg.getAttribute('viewBox')).toBe('0 0 1 1');
    expect(svg.parentElement).toBe(pageBox());
    // The paper is white in both themes, so what is drawn on it keeps the light colours.
    expect(pageBox().getAttribute('data-theme')).toBe('light');
    expect(screen.getByRole('img', { name: 'Outline of Synthetic countertop a on page 12' })).toBeTruthy();

    const before = parseFloat(pageBox().style.width);
    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    expect(parseFloat(pageBox().style.width)).toBeCloseTo(before * 1.25, 3);
    expect(outline.getAttribute('points')).toBe('0.4,0.8 0.6,0.8 0.6,0.9 0.4,0.9');
    // The pin pairs the colour with the glyph and the word.
    const pin = document.querySelector('[data-slot="drawing-pin"]')!;
    expect(pin.textContent).toBe('Needs correction');
    expect(pin.querySelector('[data-outcome-icon="FAIL"]')).not.toBeNull();
  });

  it('keyboard: + and − zoom, 0 fits, F finds the outline, ← and → change page, Esc closes', async () => {
    const { onTargetChange, onClose } = open(targetFromRow(A));
    await loadPicture();
    const atOutline = zoomLevel();
    act(() => void fireEvent.keyDown(window, { key: '0' }));
    expect(zoomLevel()).toBe('100%');
    act(() => void fireEvent.keyDown(window, { key: '+' }));
    expect(zoomLevel()).toBe('125%');
    act(() => void fireEvent.keyDown(window, { key: '-' }));
    expect(zoomLevel()).toBe('100%');
    act(() => void fireEvent.keyDown(window, { key: 'f' }));
    expect(zoomLevel()).toBe(atOutline);
    act(() => void fireEvent.keyDown(window, { key: 'ArrowLeft' }));
    expect(onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'c', page: 7 }));
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });

  it('another countertop on the page is drawn faintly and can be picked, on the drawing or in the list', async () => {
    const { onTargetChange } = open(targetFromRow(A));
    await loadPicture();
    const faint = document.querySelector('[data-other="b"]')!;
    expect(faint.getAttribute('points')).toBe('0.1,0.1 0.3,0.1 0.3,0.2 0.1,0.2');
    fireEvent.click(faint);
    expect(onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'b', tone: 'pass' }));
    expect(await screen.findByRole('heading', { name: 'Synthetic countertop b' }, { timeout: 5000 })).toBeTruthy();
    fireEvent.click(document.querySelector('[data-other-button="a"]')!);
    expect(onTargetChange).toHaveBeenLastCalledWith(expect.objectContaining({ key: 'a' }));
  });

  it('switches page from the strip; a picture not rendered yet says so and is never requested to render', async () => {
    open(targetFromRow(A));
    await loadPicture();
    const strip = screen.getByRole('navigation', { name: 'Pages' });
    fireEvent.click(within(strip).getByRole('button', { name: /^Page 7:/ }));
    expect(await screen.findByText('Picture of page 7 not ready', undefined, { timeout: 5000 })).toBeTruthy();
    notReady.delete(7);
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    await loadPicture();
    expect(screen.queryByText('Picture of page 7 not ready')).toBeNull();
    // Only reads: no request to render pictures, and nothing but GETs.
    expect(calls.some((c) => c.method !== 'GET' || /\/pages\/pictures/.test(c.url))).toBe(false);
  });

  it('shows the crops side panel with each exact value printed over its crop; picking one marks its spot', async () => {
    open(targetFromRow(A));
    await loadPicture();
    await waitFor(() => expect(document.querySelectorAll('[data-slot="crop-value"]')).toHaveLength(3), { timeout: 5000 });
    expect([...document.querySelectorAll('[data-slot="crop-value"]')].map((n) => n.textContent)).toEqual(['42"', '20 1/2"', '20"']);
    fireEvent.click(document.querySelector('[data-reading-crop="obs-p0"]')!);
    // Every number the result used is highlighted where it was read; the picked one most strongly.
    expect(document.querySelectorAll('[data-reading]')).toHaveLength(3);
    expect(document.querySelector('[data-reading="obs-p0"]')!.getAttribute('stroke-width')).toBe('2.5');
    expect(document.querySelector('[data-reading="obs-p1"]')!.getAttribute('stroke-width')).toBe('1.5');
    expect(document.querySelector('[data-reading="obs-p1"] title')!.textContent).toBe('Filler 2: 20"');
  });

  it('a held row says "No readings used" with its hold reason instead of crops, and the reason in the header', async () => {
    open(targetFromRow(C));
    expect(await screen.findByText('No readings used', undefined, { timeout: 5000 })).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Why: No readings used' })).toBeTruthy();
    expect(document.querySelector('[data-slot="no-crops-reason"]')?.textContent).toBe('Synthetic hold reason.');
    expect(screen.queryByText(/did not run/)).toBeNull();
    // Said once in the panel: not again under the picture.
    expect(document.querySelector('[data-slot="drawing-hold-reason"]')).toBeNull();
    expect(screen.getByText('Held: Synthetic hold reason.')).toBeTruthy();
    expect(document.querySelectorAll('[data-reading-crop]')).toHaveLength(0);
  });

  it('a held row whose check did use readings says its hold reason under the picture', async () => {
    const held = row('a', 12, { hold: { code: 'stone-into-walls', reason: 'Synthetic: held for the walls.' } });
    open(targetFromRow(held), vi.fn(), [held, B]);
    await waitFor(() => expect(document.querySelectorAll('[data-slot="crop-value"]').length).toBeGreaterThan(0), { timeout: 5000 });
    expect(document.querySelector('[data-slot="drawing-evidence"] [data-slot="drawing-hold-reason"]')?.textContent).toBe('Synthetic: held for the walls.');
  });

  it('the side panel shows the row\'s "drawn length not checked" note under its picture, and nothing when there is none (#1107)', async () => {
    const NOTE = 'Drawn length not checked (no scale): piece 2, the overall';
    const noted = row('noted', 12, { drawn_length_note: NOTE });
    open(targetFromRow(noted), vi.fn(), [noted, B]);
    const panel = document.querySelector('[data-slot="drawing-evidence"]') as HTMLElement;
    expect(panel.querySelector('[data-slot="drawn-length-note"]')?.textContent).toBe(NOTE);
    cleanup();
    open(targetFromRow(row('plain', 12, { drawn_length_note: null })));
    expect(document.querySelector('[data-slot="drawing-evidence"] [data-slot="drawn-length-note"]')).toBeNull();
  });

  it('a page that is not in the set is an error, not "not ready"', async () => {
    missingPages = new Set([12]);
    open(targetFromRow(A), vi.fn(), [A]);
    expect(await screen.findByText('The page picture could not be loaded', undefined, { timeout: 5000 })).toBeTruthy();
    expect(screen.queryByText(/not ready/)).toBeNull();
  });

  it('a result with no stored location says so and fetches no picture', async () => {
    const target = targetFromFinding({ id: 'f', name: 'rule', scope_label: 'Synthetic check', outcome: 'NOT_FOUND', row_location: null, shop_evidence: null, arch_evidence: null });
    open(target, vi.fn(), []);
    expect(await screen.findByText('No stored location', undefined, { timeout: 5000 })).toBeTruthy();
    expect(calls.some((c) => c.url.includes('/picture'))).toBe(false);
  });
});
