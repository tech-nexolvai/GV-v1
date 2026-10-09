// @vitest-environment jsdom
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ArchitectPairing, ArchitectResult, CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { ArchitectLine } from '@/components/results/architect-line';
import { ArchitectPairingPanel } from '@/components/queue/architect-pairing';
import { MarkNotes } from '@/components/drawing/drawing-viewer';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import {
  architectFindingIds,
  architectState,
  confirmWords,
  draftBody,
  draftProblem,
  pairedByWords,
  vendorSideWords,
} from '@/lib/architect';
import { bucketOf, kpis, matchesFilter, resultBucket } from '@/lib/countertop-results';
import { architectMarks, spanMark, targetFromArchitect, targetFromRow } from '@/lib/drawing-viewer';
import { architectItemKey, buildQueue, itemStatus, type LiveData } from '@/lib/needs-you-queue';

// The dashboard asks the rulebook for names; keep it off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic data only: nothing here comes from a client drawing.
const VERSION = '00000000-0000-4000-8000-0000000000aa';
const box = (x0: number, y0: number, x1: number, y1: number) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map(String));
const location = (page: number, polygon: string[][]) => ({ coordinate_space: 'stored', document_version_id: VERSION, page_id: `page-${page}`, page_number: page, polygon });
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });

const notCompared: ArchitectResult = { outcome: null, finding_id: null, reason: null, needs_decision: false, compared: [], not_compared_reason: 'the architect prints only sink centre lines on this page', pairing_source: null, pairing_judgments: null };
function compared(id: string, extra: Partial<ArchitectResult> = {}): ArchitectResult {
  return {
    outcome: 'FAIL', finding_id: `arch-${id}`, reason: 'Synthetic: the vendor prints 88" where the architect prints 84".', needs_decision: false,
    compared: [{
      kind: 'overall', vendor_piece: null, vendor: x('88', '88"'), architect: x('84', '84"'), delta: x('4', '+4"'),
      vendor_display: '88"', architect_display: '84"', delta_display: '+4"', outcome: 'FAIL',
      architect_location: location(3, box(0.1, 0.1, 0.4, 0.15)),
    }],
    not_compared_reason: null, pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
    ...extra,
  };
}

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `width-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: location(page, box(0.4, 0.8, 0.6, 0.9)),
    outcome: 'PASS', needs_decision: false, printed_overall: x('88', '88"'), pieces: [],
    field_cut_per_end: x('1', '1"'), field_cut_count: 2, expected_total: x('88', '88"'), delta: x('0', '0"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: 'back_only', label: 'back wall only', source: 'drawing clues' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
    ...overrides,
  };
}

const finding = (id: string, outcome: Finding['outcome'], extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome, reviewer_action: null, created_at: '2026-10-09T10:00:00Z', ...extra }) as Finding;

// As the API sends a one-judgment result: the pair's own result waits too (REVIEW_REQUIRED).
const waitingPairs = compared('w').compared.map((pair) => ({ ...pair, outcome: 'REVIEW_REQUIRED' as const }));
const ONE_JUDGMENT_CODE = compared('code', { outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'code', pairing_judgments: 'code only', reason: 'Only code paired these; confirm that they measure the same thing.', compared: waitingPairs });
const ONE_JUDGMENT_AIS = compared('ais', { outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'both-ais', pairing_judgments: 'both AIs only', compared: waitingPairs });

describe('architect states', () => {
  it('names each state from the API, never guessing', () => {
    expect(architectState(null)).toBe('none');
    expect(architectState(notCompared)).toBe('not-compared');
    expect(architectState(compared('pass', { outcome: 'PASS' }))).toBe('compared');
    expect(architectState(compared('fail'))).toBe('compared');
    expect(architectState(ONE_JUDGMENT_CODE)).toBe('confirm');
    expect(architectState(ONE_JUDGMENT_AIS)).toBe('confirm');
    expect(architectState(compared('unpaired', { outcome: 'REVIEW_REQUIRED', needs_decision: true, pairing_source: 'none', pairing_judgments: null, compared: [] }))).toBe('unpaired');
    expect(architectState(compared('reviewer', { outcome: 'PASS', pairing_source: 'reviewer', pairing_judgments: 'reviewer' }))).toBe('compared');
  });

  it('words the one-judgment ask and who paired, never "AI decided"', () => {
    expect(confirmWords(ONE_JUDGMENT_CODE)).toBe('Confirm the pairing: only code matched these');
    expect(confirmWords(ONE_JUDGMENT_AIS)).toBe('Confirm the pairing: only the AIs matched these');
    expect(pairedByWords({ pairing_judgments: 'code and both AIs' })).toBe('Paired by code and both AIs');
    expect(pairedByWords({ pairing_judgments: 'reviewer' })).toBe('Paired by a reviewer');
    expect(vendorSideWords('piece', [2, 1])).toBe('Pieces 2–3');
    expect(vendorSideWords('overall', [])).toBe('The overall');
  });

  it('counts a countertop by both its checks: needs you, FAIL and PASS', () => {
    const architectFail = row('f', 1, { architect: compared('f') });
    const architectAsks = row('c', 2, { architect: ONE_JUDGMENT_CODE });
    const allPass = row('p', 3, { architect: compared('p', { outcome: 'PASS' }) });
    const notComparedPass = row('n', 4, { architect: notCompared });
    expect(resultBucket(architectFail)).toBe('fail'); // the width passed, the architect check did not
    expect(bucketOf(architectAsks)).toBe('needs-you');
    expect(resultBucket(allPass)).toBe('pass');
    expect(resultBucket(notComparedPass)).toBe('pass'); // "not compared" adds no result
    const counts = kpis([architectFail, architectAsks, allPass, notComparedPass]);
    expect(counts).toMatchObject({ needsYou: 1, fail: 1, pass: 2 });
    expect(matchesFilter(architectAsks, 'needs-you')).toBe(true);
    expect(matchesFilter(architectFail, 'pass')).toBe(false);
    expect([...architectFindingIds([architectFail, notComparedPass])]).toEqual(['arch-f']);
  });
});

describe('the Architect line', () => {
  it('not compared: grey words with the reason, no chip and no action', () => {
    render(<ArchitectLine result={notCompared} />);
    const line = screen.getByText(/Not compared:/);
    expect(line.textContent).toBe('Not compared: the architect prints only sink centre lines on this page');
    expect(document.querySelector('[data-slot="outcome-badge"]')).toBeNull();
    expect(screen.queryByRole('button')).toBeNull();
  });

  it('compared: what the architect says, the difference and the result, in the API text', () => {
    render(<ArchitectLine result={compared('f')} />);
    const line = document.querySelector('[data-slot="architect-line"]')!;
    expect(line.textContent).toContain('Architect\'s drawing says 84"');
    expect(line.textContent).toContain('+4"');
    expect(within(line as HTMLElement).getByText('Needs correction')).toBeTruthy();
    expect(line.textContent).toContain('Paired by code and both AIs');
  });

  it('one judgment: amber "Confirm the pairing" with who matched them, and no verdict colour on its numbers', () => {
    // Even if a pair arrived with a verdict, the numbers stay neutral until the reviewer confirms.
    render(<ArchitectLine result={{ ...ONE_JUDGMENT_AIS, compared: compared('v').compared }} />);
    expect(screen.getByText('Confirm the pairing: only the AIs matched these')).toBeTruthy();
    expect(screen.getByText('Paired by both AIs only')).toBeTruthy();
    expect(document.querySelector('[data-slot="architect-line"] [data-outcome-icon="FAIL"]')).toBeNull();
  });

  it('says "Not checked yet" in the server\'s words, not as "Not compared: Not checked yet"', () => {
    render(<ArchitectLine result={{ ...notCompared, not_compared_reason: "Not checked yet: run the checks to compare this row with the architect's drawing." }} />);
    expect(document.querySelector('[data-architect="not-compared"]')!.textContent).toBe("Not checked yet: run the checks to compare this row with the architect's drawing.");
  });
});

describe('Results with the architect check', () => {
  function setup(rows: CountertopResult[], findings: Finding[], blocking: string[] = []) {
    const onOpenQueue = vi.fn();
    render(
      <ResultsDashboard
        countertops={{ status: 'ready', rows } as CountertopsState}
        findings={findings}
        blockingIds={new Set(blocking)}
        busy={false}
        filter="all"
        onFilterChange={() => {}}
        onRetry={() => {}}
        onRefresh={() => {}}
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        onBulkDismiss={vi.fn(async () => ({ saved: 0, failed: [] }))}
        onShowDrawing={() => {}}
        onOpenCard={() => {}}
        onOpenQueue={onOpenQueue}
      />,
    );
    return { onOpenQueue };
  }

  it('puts an Architect line under each row, and keeps the architect finding out of Other checks', async () => {
    const rows = [row('a', 1, { architect: notCompared }), row('b', 2, { architect: compared('b') })];
    const findings = [finding('width-a', 'PASS'), finding('width-b', 'PASS'), finding('arch-b', 'FAIL'), finding('package', 'NOT_FOUND')];
    setup(rows, findings, ['package']);
    const table = document.querySelector('[data-slot="countertop-table"]')!;
    const a = table.querySelector('tr[data-architect-row="a"]')!;
    expect(a.textContent).toBe('ArchitectNot compared: the architect prints only sink centre lines on this page');
    expect(a.querySelector('[data-slot="outcome-badge"]')).toBeNull();
    const b = table.querySelector('tr[data-architect-row="b"]')!;
    expect(b.textContent).toContain('84"');
    expect(b.textContent).toContain('+4"');
    expect(within(b as HTMLElement).getByText('Needs correction')).toBeTruthy();
    expect(b.textContent).toContain('Automatic');
    // One other check, not two: the architect's FAIL is on its countertop.
    expect(within(screen.getByRole('region', { name: 'Other checks' })).getByRole('button', { name: /Other checks/ }).textContent).toBe('Other checks11 need you'); // 1 check, 1 needs you
  });

  it('a pairing to confirm opens the queue at that countertop', async () => {
    const user = userEvent.setup();
    const rows = [row('c', 4, { architect: ONE_JUDGMENT_CODE })];
    const { onOpenQueue } = setup(rows, [finding('width-c', 'PASS'), finding('arch-code', 'REVIEW_REQUIRED')], ['arch-code']);
    const sub = document.querySelector('tr[data-architect-row="c"]')!;
    expect(sub.textContent).toContain('Confirm the pairing: only code matched these');
    await user.click(within(sub as HTMLElement).getByRole('button', { name: 'Confirm the pairing…' }));
    expect(onOpenQueue).toHaveBeenCalledWith(architectItemKey('c'));
  });

  it('a recorded architect result that needs a decision uses the usual Decide dialog', async () => {
    const user = userEvent.setup();
    const rows = [row('d', 5, { architect: compared('d', { needs_decision: true }) })];
    setup(rows, [finding('width-d', 'PASS'), finding('arch-d', 'FAIL')], ['arch-d']);
    const sub = document.querySelector('tr[data-architect-row="d"]')!;
    await user.click(within(sub as HTMLElement).getByRole('button', { name: 'Decide' }));
    expect(await screen.findByRole('dialog', { name: /matches the architect\?/ })).toBeTruthy();
  });
});

describe('the queue', () => {
  it('lists "Matches the architect?" even when the width is settled, never as an other check', () => {
    const rows = [row('q', 6, { architect: ONE_JUDGMENT_CODE })];
    const findings = [finding('width-q', 'PASS'), finding('arch-code', 'REVIEW_REQUIRED', { scope_row_candidate_id: 'q' })];
    const items = buildQueue(rows, findings, new Set(['arch-code']));
    expect(items.map((item) => [item.kind, item.key])).toEqual([['architect', architectItemKey('q')]]);
    const live: LiveData = { rows: new Map(rows.map((r) => [r.row_id, r])), findings: new Map(findings.map((f) => [f.id, f])), blocking: new Set(['arch-code']), wallsSaved: new Set() };
    expect(itemStatus(items[0], live)).toBe('open');
    expect(itemStatus(items[0], { ...live, pairingsSaved: new Set(['q']) })).toBe('waiting-for-run'); // only a run uses a pairing
    expect(itemStatus(items[0], { ...live, blocking: new Set() })).toBe('decided');
  });
});

describe('the drawing', () => {
  it('marks the architect\'s compared dimension from its stored location, and says when it has none', () => {
    const withPlace = row('m', 3, { architect: compared('m') });
    const target = targetFromRow(withPlace);
    expect(target.marks).toHaveLength(1);
    expect(target.marks[0].outline).not.toBeNull();
    expect(target.marks[0].label).toBe('Architect\'s drawing says 84" (the overall)');
    const noPlace = architectMarks(row('n', 3, { architect: compared('n', { compared: [{ ...compared('n').compared[0], architect_location: null }] }) }));
    expect(noPlace[0].outline).toBeNull();
    render(<MarkNotes marks={[...target.marks, ...noPlace]} at={target} />);
    const notes = document.querySelector('[data-slot="mark-notes"]')!.textContent;
    expect(notes).toContain('outlined in grey');
    expect(notes).toContain('not outlined (its position on the drawing is not stored)');
    expect(spanMark({ candidate_id: 's', printed: '7\'-0"', location: null }).outline).toBeNull();
  });

  it('a "Matches the architect?" item looks like the architect result, never like the width', () => {
    const widthPass = row('w', 3, { outcome: 'PASS', architect: ONE_JUDGMENT_CODE });
    expect(targetFromRow(widthPass)).toMatchObject({ glyph: 'PASS' });
    expect(targetFromArchitect(widthPass)).toMatchObject({ tone: 'review', glyph: 'REVIEW_REQUIRED', word: 'Needs your decision', needsYou: true, findingId: 'arch-code' });
    expect(targetFromArchitect(row('x', 3, { outcome: 'PASS', architect: compared('x') }))).toMatchObject({ tone: 'fail', glyph: 'FAIL' });
  });
});

// ── The pairing picker ──────────────────────────────────────

const SPAN_OK = { candidate_id: 'span-ok', printed: '7\'-0"', inches: '84 in', on_outline: true, held_reason: null, row: 1, slot: null, can_pair: true, refusal: null, location: location(3, box(0.1, 0.1, 0.4, 0.15)) };
const SPAN_CENTRE = { candidate_id: 'span-centre', printed: '2\'-6"', inches: '30 in', on_outline: false, held_reason: null, row: 1, slot: null, can_pair: false, refusal: 'This dimension is on a fixture centre line, so it never pairs with a cabinet.', location: null };
const PAIRING: ArchitectPairing = {
  row_id: 'r', piece_count: 3, current: null,
  effective: { record_id: 'rec', source: 'code', status: 'paired', pairs: [{ kind: 'overall', architect_candidate_id: 'span-ok', vendor_slot_indices: [] }], reasons: [], judgments: 1, needs_confirmation: true },
  spans: [SPAN_OK, SPAN_CENTRE],
};

describe('ArchitectPairingPanel', () => {
  const posts: unknown[] = [];
  let gets = 0;
  let postStatus = 201;
  beforeEach(() => {
    posts.length = 0;
    gets = 0;
    postStatus = 201;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (!url.endsWith('/slot-rows/r/architect-pairing')) return new Response('{}', { status: 404 });
      if ((init?.method ?? 'GET') === 'GET') {
        gets += 1;
        return new Response(JSON.stringify(PAIRING), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      posts.push(JSON.parse(String(init?.body)));
      if (postStatus === 409) return new Response(JSON.stringify({ error: 'http_error', message: "This row's pairing was just updated. Reload it before pairing again.", request_id: 'x' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
      if (postStatus === 422) return new Response(JSON.stringify({ error: 'http_error', message: 'Pieces paired with one architect dimension must be next to each other.', request_id: 'x' }), { status: 422, headers: { 'Content-Type': 'application/json' } });
      return new Response(JSON.stringify(PAIRING), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }));
  });

  function setup() {
    const onSaved = vi.fn();
    const onMarks = vi.fn();
    render(<ArchitectPairingPanel projectId="p" packageId="k" row={row('r', 3, { architect: ONE_JUDGMENT_CODE })} canConfirm onSaved={onSaved} onMarks={onMarks} />);
    return { onSaved, onMarks };
  }

  it('shows the pairing one judgment made and sends it only on "Confirm the pairing"', async () => {
    const user = userEvent.setup();
    const { onSaved, onMarks } = setup();
    expect((await screen.findByRole('list', { name: 'The pairing' })).textContent).toBe('The overall ↔ the architect\'s 7\'-0"');
    expect(screen.getByText('Only code matched these:')).toBeTruthy();
    expect(posts).toEqual([]); // nothing is sent by looking
    await waitFor(() => expect(onMarks).toHaveBeenCalledWith([expect.objectContaining({ key: 'span:span-ok' }), expect.objectContaining({ key: 'span:span-centre', outline: null })], null));
    await user.click(screen.getByRole('button', { name: 'Confirm the pairing' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(posts).toEqual([{ pairs: [{ kind: 'overall', architect_candidate_id: 'span-ok', vendor_slot_indices: [] }], note: null }]);
  });

  it('pairs it differently: a centre-line span is greyed with its reason; nothing is pre-selected', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    await user.click(await screen.findByRole('button', { name: 'Pair it differently' }));
    const picker = screen.getByRole('list', { name: "The architect's dimensions on this page" });
    const centre = picker.querySelector('[data-span="span-centre"]')!;
    expect(centre.getAttribute('data-can-pair')).toBe('false');
    expect(centre.querySelector('[data-part="refusal"]')!.textContent).toBe('This dimension is on a fixture centre line, so it never pairs with a cabinet.');
    expect(within(centre as HTMLElement).queryByRole('radio')).toBeNull();
    expect(screen.getByRole('button', { name: 'Save this pairing' }).hasAttribute('disabled')).toBe(true);
    const ok = picker.querySelector('[data-span="span-ok"]') as HTMLElement;
    await user.click(within(ok).getByRole('radio', { name: 'One piece' }));
    expect(screen.getByText("Choose one or more of this row's own pieces.")).toBeTruthy();
    // One piece per dimension: a run of pieces would be accepted but never compared in V1.
    await user.click(within(ok).getByRole('radio', { name: '2' }));
    await user.click(screen.getByRole('button', { name: 'Save this pairing' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(posts).toEqual([{ pairs: [{ kind: 'piece', architect_candidate_id: 'span-ok', vendor_slot_indices: [1] }], note: null }]);
  });

  it('"Nothing comparable" needs a note, then sends no pairs', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    await user.click(await screen.findByRole('button', { name: 'Nothing comparable' }));
    expect(screen.getByRole('alert').textContent).toContain('Say why');
    expect(posts).toEqual([]);
    await user.type(screen.getByLabelText(/Note/), 'Synthetic: only centre lines on this sheet.');
    await user.click(screen.getByRole('button', { name: 'Nothing comparable' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(posts).toEqual([{ pairs: [], note: 'Synthetic: only centre lines on this sheet.' }]);
  });

  it('a 409 says someone just changed it and offers a reload; a refusal is shown in its own words', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    postStatus = 409;
    await user.click(await screen.findByRole('button', { name: 'Confirm the pairing' }));
    expect((await screen.findByRole('alert')).textContent).toContain("This row's pairing was just updated. Reload it before pairing again.");
    const before = gets;
    await user.click(screen.getByRole('button', { name: 'Reload the pairing' }));
    await waitFor(() => expect(gets).toBe(before + 1));
    postStatus = 422;
    await user.click(await screen.findByRole('button', { name: 'Confirm the pairing' }));
    expect((await screen.findByRole('alert')).textContent).toContain('Pieces paired with one architect dimension must be next to each other.');
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('names the record it showed on every save, so a colleague\'s newer pairing is never overwritten (#1101)', async () => {
    const user = userEvent.setup();
    let currentId = 'record-shown-1';
    const record = (id: string) => ({
      record_id: id, source: 'code', status: 'paired', pairs: PAIRING.effective!.pairs, reasons: [], note: null,
      decided_by: null, decided_at: '2026-10-09T10:00:00Z', supersedes_id: null,
    });
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (!String(input).endsWith('/slot-rows/r/architect-pairing')) return new Response('{}', { status: 404 });
      if ((init?.method ?? 'GET') === 'GET') {
        gets += 1;
        return new Response(JSON.stringify({ ...PAIRING, current: record(currentId) }), { status: 200, headers: { 'Content-Type': 'application/json' } });
      }
      posts.push(JSON.parse(String(init?.body)));
      if (postStatus === 409) return new Response(JSON.stringify({ error: 'http_error', message: "This row's pairing changed after you opened it. Reload it before pairing again.", request_id: 'x' }), { status: 409, headers: { 'Content-Type': 'application/json' } });
      return new Response(JSON.stringify(PAIRING), { status: 201, headers: { 'Content-Type': 'application/json' } });
    }));
    const { onSaved } = setup();

    // Someone else saved a newer pairing while this one was on screen: the server refuses.
    postStatus = 409;
    await user.click(await screen.findByRole('button', { name: 'Confirm the pairing' }));
    expect((await screen.findByRole('alert')).textContent).toContain("This row's pairing changed after you opened it.");
    expect(posts).toEqual([{ pairs: [{ kind: 'overall', architect_candidate_id: 'span-ok', vendor_slot_indices: [] }], note: null, expected_record_id: 'record-shown-1' }]);

    // Reloaded, the reviewer sees the newer record and the next save names it.
    currentId = 'record-shown-2';
    postStatus = 201;
    await user.click(screen.getByRole('button', { name: 'Reload the pairing' }));
    await user.type(await screen.findByLabelText(/Note/), 'Synthetic: only centre lines here.');
    await user.click(screen.getByRole('button', { name: 'Nothing comparable' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(posts[1]).toEqual({ pairs: [], note: 'Synthetic: only centre lines here.', expected_record_id: 'record-shown-2' });

    // The picker path names it too.
    await user.click(screen.getByRole('button', { name: 'Pair it differently' }));
    const ok = screen.getByRole('list', { name: "The architect's dimensions on this page" }).querySelector('[data-span="span-ok"]') as HTMLElement;
    await user.click(within(ok).getByRole('radio', { name: 'One piece' }));
    await user.click(within(ok).getByRole('radio', { name: '2' }));
    await user.click(screen.getByRole('button', { name: 'Save this pairing' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(2));
    expect(posts[2]).toEqual({ pairs: [{ kind: 'piece', architect_candidate_id: 'span-ok', vendor_slot_indices: [1] }], note: 'Synthetic: only centre lines here.', expected_record_id: 'record-shown-2' });
  });

  it('sends no record id when the row has no record yet', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    await user.click(await screen.findByRole('button', { name: 'Confirm the pairing' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalled());
    expect(posts).toHaveLength(1);
    expect(posts[0]).not.toHaveProperty('expected_record_id');
  });

  it('checks a drafted pairing the way the server does', () => {
    expect(draftProblem([], [SPAN_OK])).toMatch(/at least one pair/);
    expect(draftProblem([{ candidateId: 'span-centre', kind: 'overall', pieces: [] }], [SPAN_OK, SPAN_CENTRE])).toBe(SPAN_CENTRE.refusal);
    expect(draftProblem([{ candidateId: 'span-ok', kind: 'piece', pieces: [] }], [SPAN_OK])).toBe("Choose one or more of this row's own pieces.");
    expect(draftProblem([{ candidateId: 'span-ok', kind: 'piece', pieces: [1] }], [SPAN_OK])).toBeNull();
    expect(draftProblem([{ candidateId: 'span-ok', kind: 'piece', pieces: [1, 0] }], [SPAN_OK])).toBe('Pair one piece with each dimension: a run of pieces is not compared yet.');
    expect(draftProblem([{ candidateId: 'span-ok', kind: 'overall', pieces: [] }, { candidateId: 'span-ok', kind: 'overall', pieces: [] }], [SPAN_OK])).toBe('Each architect dimension can be paired only once.');
    expect(draftBody([{ candidateId: 'span-ok', kind: 'piece', pieces: [2] }])).toEqual([{ kind: 'piece', architect_candidate_id: 'span-ok', vendor_slot_indices: [2] }]);
  });
});

describe('the queue on screen: "Matches the architect?"', { timeout: 15_000 }, () => {
  const posts: unknown[] = [];
  beforeEach(() => {
    posts.length = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const ok = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
      if (url.endsWith('/slot-rows')) return ok({ rows: [] });
      if (url.endsWith('/rules')) return ok([]);
      if (url.endsWith('/actions')) return ok({ items: [] });
      if (url.includes('/chain')) return ok({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: 'Synthetic.' } });
      if (url.includes('/architect-pairing')) {
        if ((init?.method ?? 'GET') === 'POST') {
          posts.push(JSON.parse(String(init?.body)));
          return ok(PAIRING, 201);
        }
        return ok(PAIRING);
      }
      return ok({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    }));
  });

  it('opens at the item, offers the pairing (not the decision form), and then waits for a check run', async () => {
    const user = userEvent.setup();
    const onPairingSaved = vi.fn();
    const rows = [row('w', 2, { outcome: 'FAIL', needs_decision: true }), row('q', 6, { architect: ONE_JUDGMENT_CODE })];
    const findings = [finding('width-w', 'FAIL'), finding('width-q', 'PASS'), finding('arch-code', 'REVIEW_REQUIRED', { scope_row_candidate_id: 'q' })];
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={rows} rowsReady findings={findings} blocking={new Set(['width-w', 'arch-code'])}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'run-checks', label: 'Run checks', disabled: false, reason: null }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onPairingSaved={onPairingSaved} onOpenCard={vi.fn()}
        startAt={architectItemKey('q')}
      />,
    );
    // Opened at the architect item, not the first open one (the width FAIL on page 2).
    expect(document.querySelector('[data-slot="queue-item"] h2')?.textContent).toBe('Synthetic countertop q: matches the architect?');
    expect(document.querySelector('[data-slot="queue-decision"]')).toBeNull();
    act(() => void fireEvent.keyDown(document.body, { key: '1' })); // the decision keys do nothing here
    expect(document.querySelector('[data-slot="queue-decision"]')).toBeNull();
    await user.click(await screen.findByRole('button', { name: 'Confirm the pairing' }));
    await waitFor(() => expect(onPairingSaved).toHaveBeenCalledTimes(1));
    expect(posts).toEqual([{ pairs: [{ kind: 'overall', architect_candidate_id: 'span-ok', vendor_slot_indices: [] }], note: null }]);
    expect(await screen.findByText('Pairing saved. It counts once the checks run again.')).toBeTruthy();
    expect(document.querySelector('[data-slot="queue-item"]')?.getAttribute('data-status')).toBe('waiting-for-run');
  });
});
