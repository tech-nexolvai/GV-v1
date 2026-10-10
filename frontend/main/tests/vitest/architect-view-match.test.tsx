// @vitest-environment jsdom
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ArchitectMatch, ArchitectResult, ArchitectViewMatch, ArchitectViewRef, CountertopResult } from '@/api/client';
import type { Finding } from '@/data/types';
import { ArchitectLine } from '@/components/results/architect-line';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import { ArchitectViewPicker, NONE_OF_THESE } from '@/components/queue/architect-view-picker';
import { NeedsYouQueue } from '@/components/queue/needs-you-queue';
import { DrawingViewerSheet } from '@/components/drawing/drawing-viewer';
import { SignOffPanel } from '@/components/review/signoff-panel';
import { architectState, MATCH_WORDS, viewHeading, viewLinkWords, viewPicksWaiting } from '@/lib/architect';
import { architectSecondPage, targetFromRow } from '@/lib/drawing-viewer';
import { architectItemKey, buildQueue, itemStatus, type LiveData } from '@/lib/needs-you-queue';

// The dashboard asks the rulebook for names; keep it off the network.
vi.mock('@/api/client', async (original) => ({
  ...(await original<typeof import('@/api/client')>()),
  listRules: vi.fn(async () => []),
}));

// Synthetic data only: nothing here comes from a client drawing. The file name, sheet "Z-9", titles
// and widths are made up.
const VENDOR = '00000000-0000-4000-8000-0000000000aa';
const ARCH = '00000000-0000-4000-8000-0000000000bb';
const box = (x0: number, y0: number, x1: number, y1: number) => [[x0, y0], [x1, y0], [x1, y1], [x0, y1]].map((p) => p.map(String));
const location = (version: string, page: number, polygon: string[][]) => ({ coordinate_space: 'stored', document_version_id: version, page_id: `${version}-page-${page}`, page_number: page, polygon });
const x = (numerator: string, display: string) => ({ numerator, denominator: '1', display });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

function view(n: number, extra: Partial<ArchitectViewRef> = {}): ArchitectViewRef {
  return {
    view_id: `view-${n}`, document_id: 'doc-arch', document_version_id: ARCH, file_name: 'synthetic-architect.pdf',
    page_number: 2, sheet_number: 'Z-9', bubble: String(n), title: `SAMPLE ELEVATION ${n}`, scale_note: '1/2" = 1\'-0"',
    label: `synthetic-architect.pdf, page 2, view ${n}`, region: location(ARCH, 2, box(0.1 * n, 0.2, 0.1 * n + 0.08, 0.4)),
    picture_url: `/synthetic/view-${n}.png`, separated: true,
    ...extra,
  };
}

function match(status: ArchitectMatch['status'], extra: Partial<ArchitectMatch> = {}): ArchitectMatch {
  return {
    record_id: `match-${status}`, status, source: 'automatic', judgments: null, code_verdict: null, code_pick_view_id: null,
    ai_picks: [], matched_view: null, needs_decision: false, reason: null, waits_for_run: false,
    ...extra,
  };
}

const NOTHING: ArchitectResult = { outcome: null, finding_id: null, reason: null, needs_decision: false, compared: [], not_compared_reason: null, pairing_source: null, pairing_judgments: null };
const comparedPair = (outcome: 'PASS' | 'FAIL') => ({
  kind: 'overall' as const, vendor_piece: null, vendor: x('96', '96"'), architect: x(outcome === 'PASS' ? '96' : '93', outcome === 'PASS' ? '96"' : '93"'),
  delta: x(outcome === 'PASS' ? '0' : '3', outcome === 'PASS' ? '0"' : '+3"'), vendor_display: '96"', architect_display: outcome === 'PASS' ? '96"' : '93"',
  delta_display: outcome === 'PASS' ? '0"' : '+3"', outcome, architect_location: location(ARCH, 2, box(0.12, 0.3, 0.18, 0.32)),
});
const comparedWith = (v: ArchitectViewRef) => `compared with ${v.file_name}, page ${v.page_number}, view ${v.bubble} ${v.title} (sheet ${v.sheet_number})`;

/** Every state the contract names, as the API would send it (synthetic reasons in the backend's words). */
const STATES: Record<string, ArchitectResult> = {
  not_matched_yet: {
    ...NOTHING,
    not_compared_reason: "The architect's drawings were uploaded as a separate file and this countertop has not been matched with a view in it yet. Run the checks again.",
    match: match('not_matched_yet', { record_id: null, source: null }), compared_with: null, compared_with_text: null,
  },
  needs_reviewer: {
    ...NOTHING, outcome: 'REVIEW_REQUIRED', finding_id: 'arch-nr', needs_decision: true, pairing_source: 'none',
    reason: "Choose which of the architect's views shows this countertop (one click).",
    match: match('needs_reviewer', { needs_decision: true, code_verdict: 'geometry_tie', ai_picks: [{ model_label: 'Reader A', answer: 'view', view_id: 'view-1', why: 'Synthetic: three bays and a sink.' }, { model_label: 'Reader B', answer: 'unsure', view_id: null, why: 'Synthetic: two look alike.' }] }),
  },
  auto_matched: {
    ...NOTHING, outcome: 'PASS', finding_id: 'arch-auto', compared: [comparedPair('PASS')], pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
    match: match('auto_matched', { judgments: 'code and both AIs', code_verdict: 'reference', code_pick_view_id: 'view-1', matched_view: view(1) }),
    compared_with: view(1), compared_with_text: comparedWith(view(1)),
  },
  reviewer_confirmed: {
    ...NOTHING, outcome: 'FAIL', finding_id: 'arch-rev', compared: [comparedPair('FAIL')], pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
    match: match('reviewer_confirmed', { source: 'reviewer', judgments: 'reviewer', matched_view: view(2) }),
    compared_with: view(2), compared_with_text: comparedWith(view(2)),
  },
  carried_over: {
    ...NOTHING, outcome: 'PASS', finding_id: 'arch-carried', compared: [comparedPair('PASS')], pairing_source: 'code+ais', pairing_judgments: 'code and both AIs',
    match: match('carried_over', { source: 'carried', matched_view: view(1) }),
    compared_with: view(1), compared_with_text: comparedWith(view(1)),
  },
  none_matches: {
    ...NOTHING,
    not_compared_reason: "The reviewer found no view in the architect's drawings that shows this countertop, so nothing was compared.",
    match: match('none_matches', { source: 'reviewer' }),
  },
  not_separated: {
    ...NOTHING, outcome: 'REVIEW_REQUIRED', finding_id: 'arch-ns', needs_decision: true, pairing_source: 'none',
    reason: "The architect's view 3 on page 2 is not clearly apart from its neighbour, so its dimensions were not read. Compare this countertop by hand, then mark it checked.",
    match: match('not_separated', { matched_view: view(3, { separated: false }) }),
  },
  no_candidates: {
    ...NOTHING,
    not_compared_reason: "The architect's file has no views to match this countertop with, so nothing was compared.",
    match: match('no_candidates'),
  },
};

/** A reviewer's pick recorded after the result on screen: the result is still the "choose" one. */
const PICKED: ArchitectResult = { ...STATES.needs_reviewer, match: match('reviewer_confirmed', { source: 'reviewer', matched_view: view(2), waits_for_run: true }) };
/** Matched, but nothing comparable on the view: grey "not compared", with the view to look at. */
const MATCHED_NOT_COMPARED: ArchitectResult = {
  ...NOTHING,
  not_compared_reason: "Matched with synthetic-architect.pdf, page 2, view 1: the architect prints no width on this countertop's outline.",
  match: match('auto_matched', { judgments: 'code and both AIs', matched_view: view(1) }),
};
/** A combined-sheet set: the new fields are null. */
const COMBINED: ArchitectResult = {
  ...NOTHING, outcome: 'FAIL', finding_id: 'arch-combined', compared: [{ ...comparedPair('FAIL'), architect_location: location(VENDOR, 3, box(0.1, 0.1, 0.4, 0.15)) }],
  pairing_source: 'code+ais', pairing_judgments: 'code and both AIs', match: null, compared_with: null, compared_with_text: null,
};

function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `width-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: location(VENDOR, page, box(0.4, 0.8, 0.6, 0.9)),
    outcome: 'PASS', needs_decision: false, printed_overall: x('96', '96"'), pieces: [],
    field_cut_per_end: x('1', '1"'), field_cut_count: 2, expected_total: x('96', '96"'), delta: x('0', '0"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: 'back_only', label: 'back wall only', source: 'drawing clues' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: true, values_agreed: [true] },
    ...overrides,
  };
}
const finding = (id: string, outcome: Finding['outcome'], extra: Partial<Finding> = {}): Finding =>
  ({ id, check_id: `CHECK-${id}`, name: `Synthetic ${id}`, severity: 'FLAG', outcome, reviewer_action: null, created_at: '2026-10-10T10:00:00Z', ...extra }) as Finding;

afterEach(() => vi.unstubAllGlobals());

// ── States ──────────────────────────────────────────────────

describe('every match state, in plain words', () => {
  it('maps each state to what the screen asks of the reviewer', () => {
    expect(architectState(STATES.not_matched_yet)).toBe('not-compared');
    expect(architectState(STATES.needs_reviewer)).toBe('choose-view');
    expect(architectState(STATES.auto_matched)).toBe('compared');
    expect(architectState(STATES.reviewer_confirmed)).toBe('compared');
    expect(architectState(STATES.carried_over)).toBe('compared');
    expect(architectState(STATES.none_matches)).toBe('not-compared');
    expect(architectState(STATES.not_separated)).toBe('by-hand');
    expect(architectState(STATES.no_candidates)).toBe('not-compared');
    expect(architectState(PICKED)).toBe('view-picked');
    expect(architectState(COMBINED)).toBe('compared');
    // Every status has words of its own.
    for (const status of Object.keys(STATES)) expect(MATCH_WORDS[status as ArchitectMatch['status']]).toBeTruthy();
  });

  const LINES: [string, ArchitectResult, string[], string[]][] = [
    ['not_matched_yet', STATES.not_matched_yet, ['Not compared: The architect\'s drawings were uploaded as a separate file and this countertop has not been matched with a view in it yet. Run the checks again.'], []],
    ['needs_reviewer', STATES.needs_reviewer, ["Choose which of the architect's views shows this countertop"], []],
    ['auto_matched', STATES.auto_matched, ['96"', '0"', 'Looks right', comparedWith(view(1)), 'View matched by code and both AIs'], []],
    ['reviewer_confirmed', STATES.reviewer_confirmed, ['93"', '+3"', 'Needs correction', comparedWith(view(2)), 'View chosen by a reviewer'], []],
    ['carried_over', STATES.carried_over, ['Looks right', comparedWith(view(1)), 'Same view as on the earlier revision'], []],
    ['none_matches', STATES.none_matches, ["Not compared: The reviewer found no view in the architect's drawings that shows this countertop, so nothing was compared."], []],
    ['not_separated', STATES.not_separated, ['Compare this countertop by hand', 'Matched with synthetic-architect.pdf, page 2, view 3'], []],
    ['no_candidates', STATES.no_candidates, ["Not compared: The architect's file has no views to match this countertop with, so nothing was compared."], []],
    ['picked, waiting for a run', PICKED, ['View chosen: it counts once the checks run again'], ['Choose which']],
  ];
  it.each(LINES)('%s', (_, result, says, never) => {
    render(<ArchitectLine result={result} />);
    const line = document.querySelector('[data-slot="architect-line"]')!;
    for (const words of says) expect(line.textContent).toContain(words);
    for (const words of never) expect(line.textContent).not.toContain(words);
  });

  it('asks with the amber glyph, never colour alone; a "not compared" state has no chip and nothing to click', () => {
    render(<ArchitectLine result={STATES.needs_reviewer} />);
    expect(document.querySelector('[data-architect="choose-view"] [data-outcome-icon="REVIEW_REQUIRED"]')).toBeTruthy();
    document.body.innerHTML = '';
    for (const state of ['not_matched_yet', 'none_matches', 'no_candidates']) {
      const { unmount } = render(<ArchitectLine result={STATES[state]} />);
      expect(document.querySelector('[data-slot="outcome-badge"]')).toBeNull();
      expect(screen.queryByRole('button')).toBeNull();
      unmount();
    }
  });

  it('a combined-sheet set (fields null) renders exactly as one without the fields', () => {
    const withoutFields: ArchitectResult = { ...COMBINED };
    delete withoutFields.match;
    delete withoutFields.compared_with;
    delete withoutFields.compared_with_text;
    const a = render(<ArchitectLine result={COMBINED} onOpenView={() => {}} />).container.innerHTML;
    document.body.innerHTML = '';
    const b = render(<ArchitectLine result={withoutFields} />).container.innerHTML;
    expect(a).toBe(b);
    expect(a).not.toContain('architect-view-link');
    expect(a).not.toContain('architect-match-tag');
    expect(architectSecondPage(row('c', 3, { architect: COMBINED }))).toBeNull();
    expect(targetFromRow(row('c', 3, { architect: COMBINED })).second).toBeNull();
  });

  it('the link says the server\'s "compared with …" when compared, else "Matched with …"', () => {
    expect(viewLinkWords(STATES.auto_matched)).toBe(comparedWith(view(1)));
    expect(viewLinkWords(MATCHED_NOT_COMPARED)).toBe('Matched with synthetic-architect.pdf, page 2, view 1');
    expect(viewLinkWords(STATES.none_matches)).toBeNull();
    expect(viewHeading(view(4))).toBe('Sheet Z-9 · view 4 · SAMPLE ELEVATION 4');
    expect(viewHeading({ sheet_number: null, bubble: null, title: null })).toBe('No sheet or title printed');
  });

  it('counts the picks that wait for a run: the server\'s, and this sitting\'s', () => {
    const rows = [row('a', 1, { architect: PICKED }), row('b', 2, { architect: STATES.auto_matched }), row('c', 3, { architect: COMBINED })];
    expect(viewPicksWaiting(rows)).toBe(1);
    expect(viewPicksWaiting(rows, new Set(['b']))).toBe(2);
    expect(viewPicksWaiting([row('d', 4, { architect: undefined })])).toBe(0);
  });
});

// ── Results ─────────────────────────────────────────────────

describe('Results with a separate architect file', () => {
  function setup(rows: CountertopResult[], findings: Finding[], blocking: string[] = []) {
    const onOpenQueue = vi.fn();
    const onShowArchitectView = vi.fn();
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
        onShowArchitectView={onShowArchitectView}
      />,
    );
    return { onOpenQueue, onShowArchitectView };
  }

  it('"compared with …" opens the architect\'s view; "Choose the view…" opens the queue at the item', async () => {
    const user = userEvent.setup();
    const rows = [row('m', 1, { architect: STATES.auto_matched }), row('n', 2, { architect: STATES.needs_reviewer })];
    const { onOpenQueue, onShowArchitectView } = setup(rows, [finding('width-m', 'PASS'), finding('width-n', 'PASS'), finding('arch-auto', 'PASS'), finding('arch-nr', 'REVIEW_REQUIRED')], ['arch-nr']);
    const m = document.querySelector('tr[data-architect-row="m"]') as HTMLElement;
    await user.click(within(m).getByRole('button', { name: comparedWith(view(1)) }));
    expect(onShowArchitectView).toHaveBeenCalledWith(rows[0]);
    const n = document.querySelector('tr[data-architect-row="n"]') as HTMLElement;
    expect(n.textContent).toContain("Choose which of the architect's views shows this countertop");
    await user.click(within(n).getByRole('button', { name: 'Choose the view…' }));
    expect(onOpenQueue).toHaveBeenCalledWith(architectItemKey('n'));
  });

  it('a view not clearly apart is compared by hand with the usual Decide dialog', async () => {
    const user = userEvent.setup();
    setup([row('s', 3, { architect: STATES.not_separated })], [finding('width-s', 'PASS'), finding('arch-ns', 'REVIEW_REQUIRED')], ['arch-ns']);
    const s = document.querySelector('tr[data-architect-row="s"]') as HTMLElement;
    expect(within(s).queryByRole('button', { name: 'Pair it…' })).toBeNull();
    await user.click(within(s).getByRole('button', { name: 'Decide' }));
    expect(await screen.findByRole('dialog', { name: /matches the architect\?/ })).toBeTruthy();
  });

  it('a pick waiting for a run offers nothing to click; not-compared states say why in grey', () => {
    setup(
      [row('p', 1, { architect: PICKED }), row('q', 2, { architect: STATES.none_matches }), row('r', 3, { architect: MATCHED_NOT_COMPARED })],
      [finding('width-p', 'PASS'), finding('width-q', 'PASS'), finding('width-r', 'PASS'), finding('arch-nr', 'REVIEW_REQUIRED')],
      ['arch-nr'],
    );
    const p = document.querySelector('tr[data-architect-row="p"]') as HTMLElement;
    expect(p.textContent).toContain('View chosen: it counts once the checks run again');
    expect(within(p).queryByRole('button', { name: /Choose|Pair|Decide/ })).toBeNull();
    const q = document.querySelector('tr[data-architect-row="q"]') as HTMLElement;
    expect(q.querySelector('[data-slot="outcome-badge"]')).toBeNull();
    const r = document.querySelector('tr[data-architect-row="r"]') as HTMLElement;
    expect(within(r).getByRole('button', { name: 'Matched with synthetic-architect.pdf, page 2, view 1' })).toBeTruthy();
  });

  it('the details say which view, on whose judgment, and what each AI said', async () => {
    const user = userEvent.setup();
    setup([row('d', 1, { architect: STATES.needs_reviewer })], [finding('width-d', 'PASS'), finding('arch-nr', 'REVIEW_REQUIRED')], ['arch-nr']);
    const table = document.querySelector('[data-slot="countertop-table"]') as HTMLElement;
    await user.click(within(table).getAllByRole('button', { name: 'Show details' })[0]);
    const details = document.querySelector('[data-slot="architect-match-details"]')!;
    expect(details.getAttribute('data-match')).toBe('needs_reviewer');
    expect(details.textContent).toContain('Reader A picked another view: “Synthetic: three bays and a sink.”');
    expect(details.textContent).toContain('Reader B was not sure');
  });
});

// ── The picker ──────────────────────────────────────────────

const CANDIDATES: ArchitectViewMatch = {
  row_id: 'n',
  vendor: { page_number: 4, document_version_id: VENDOR, title: 'SYNTHETIC KITCHEN', references: ['4/Z9'], region: location(VENDOR, 4, box(0.3, 0.6, 0.7, 0.7)) },
  current: { record_id: 'record-shown-1', status: 'needs_reviewer', source: 'automatic', decided_by: null, decided_at: null, supersedes_id: null, note: null },
  candidates: [
    {
      rank: 2, view: view(2), shown_to_ais: true,
      code: { fits: true, reference_match: false, run_length_error_display: '1/2"', bays_vendor: 3, bays_architect: 3, pair_support: null },
      score_summary: 'Fits: run length within 1/2", 3 bays on both.', evidence: ['Synthetic: same bay count.'], ai_picked_by: [], remembered: true, can_pick: true, refusal: null,
    },
    {
      rank: 1, view: view(1), shown_to_ais: true,
      code: { fits: true, reference_match: false, run_length_error_display: '1/4"', bays_vendor: 3, bays_architect: 3, pair_support: 2 },
      score_summary: 'Fits: run length within 1/4", 3 bays on both.', evidence: ['Synthetic: run length 1/4" apart.', 'Synthetic: 3 bays each.'], ai_picked_by: ['Reader A'], remembered: false, can_pick: true, refusal: null,
    },
    {
      rank: 3, view: view(3, { separated: false, picture_url: null }), shown_to_ais: false,
      code: { fits: false, reference_match: false, run_length_error_display: '6"', bays_vendor: 3, bays_architect: 4, pair_support: null },
      score_summary: 'Does not fit: run length 6" apart.', evidence: [], ai_picked_by: [], remembered: false, can_pick: false, refusal: 'Synthetic refusal: this view is on another countertop\'s sheet.',
    },
  ],
  can_choose_none: true,
};

describe('the view picker', { timeout: 15_000 }, () => {
  const posts: unknown[] = [];
  let gets = 0;
  let postStatus = 201;
  let currentId = 'record-shown-1';
  beforeEach(() => {
    posts.length = 0;
    gets = 0;
    postStatus = 201;
    currentId = 'record-shown-1';
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (/\/pages\/\d+\/picture/.test(url)) return json({ error: 'not_found', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
      if (!url.endsWith('/slot-rows/n/architect-view-match')) return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
      if ((init?.method ?? 'GET') === 'GET') {
        gets += 1;
        return json({ ...CANDIDATES, current: { ...CANDIDATES.current!, record_id: currentId } });
      }
      posts.push(JSON.parse(String(init?.body)));
      if (postStatus === 409) return json({ error: 'http_error', message: "This countertop's view was changed after you opened it. Reload it before choosing again.", request_id: 'x' }, 409);
      if (postStatus === 422) return json({ error: 'http_error', message: 'Synthetic refusal: that view is not in this revision.', request_id: 'x' }, 422);
      return json(CANDIDATES, 201);
    }));
  });

  function setup() {
    const onSaved = vi.fn();
    render(<ArchitectViewPicker projectId="p" packageId="k" row={row('n', 4, { architect: { ...STATES.needs_reviewer, match: { ...STATES.needs_reviewer.match!, code_pick_view_id: 'view-2' } } })} onSaved={onSaved} />);
    return { onSaved };
  }

  it('lists the views ranked, with pictures, facts and tags, and NOTHING pre-selected', async () => {
    setup();
    const group = await screen.findByRole('radiogroup', { name: "The architect's views" });
    const cards = [...group.querySelectorAll('[data-candidate]')].map((card) => card.getAttribute('data-candidate'));
    expect(cards).toEqual(['view-1', 'view-2', 'view-3', NONE_OF_THESE]); // by rank, then "None of these"
    for (const radio of within(group).getAllByRole('radio')) expect(radio.getAttribute('aria-checked')).toBe('false');
    expect(screen.getByRole('button', { name: 'Use this view' }).hasAttribute('disabled')).toBe(true);
    expect(posts).toEqual([]); // nothing is sent by looking

    const first = group.querySelector('[data-candidate="view-1"]') as HTMLElement;
    expect(first.querySelector('img')!.getAttribute('src')).toBe('/api/v1/projects/p/packages/k/architect-views/view-1/picture');
    expect(first.textContent).toContain('Sheet Z-9 · view 1 · SAMPLE ELEVATION 1');
    expect(first.textContent).toContain('Fits: run length within 1/4", 3 bays on both.');
    expect(first.textContent).toContain('Picked by Reader A');
    const second = group.querySelector('[data-candidate="view-2"]') as HTMLElement;
    expect(second.textContent).toContain('Remembered from an earlier revision');
    expect(second.textContent).toContain("Code's pick");
    // A view that cannot be chosen says why and has no control.
    const third = group.querySelector('[data-candidate="view-3"]') as HTMLElement;
    expect(third.getAttribute('data-can-pick')).toBe('false');
    expect(within(third).queryByRole('radio')).toBeNull();
    expect(third.querySelector('[data-part="refusal"]')!.textContent).toBe("Synthetic refusal: this view is on another countertop's sheet.");
    expect(third.textContent).toContain('No picture stored');
    // Before a choice, the side-by-side asks for one.
    expect(document.querySelector('[data-slot="view-architect-pane"]')!.textContent).toContain('Choose a view below');
  });

  it('a chosen view goes beside the vendor\'s, and is sent with the record on screen', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    await user.click(await screen.findByRole('radio', { name: 'View 2: Sheet Z-9 · view 2 · SAMPLE ELEVATION 2' }));
    expect(document.querySelector('[data-slot="view-architect-pane"]')!.getAttribute('aria-label')).toBe("Architect's view: Sheet Z-9 · view 2 · SAMPLE ELEVATION 2");
    expect(document.querySelector('[data-slot="view-vendor-pane"]')).toBeTruthy();
    await user.type(screen.getByLabelText(/Note/), 'Synthetic: the sink bay matches.');
    await user.click(screen.getByRole('button', { name: 'Use this view' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(posts).toEqual([{ view_id: 'view-2', none_of_these: false, note: 'Synthetic: the sink bay matches.', expected_record_id: 'record-shown-1' }]);
  });

  it('"None of these" is a choice of its own', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    await user.click(await screen.findByRole('radio', { name: 'None of these' }));
    await user.click(screen.getByRole('button', { name: 'Save: none of these' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(posts).toEqual([{ view_id: null, none_of_these: true, note: null, expected_record_id: 'record-shown-1' }]);
  });

  it('409: someone else changed it; reload shows theirs and the next pick names the new record', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    postStatus = 409;
    await user.click(await screen.findByRole('radio', { name: /View 1:/ }));
    await user.click(screen.getByRole('button', { name: 'Use this view' }));
    expect((await screen.findByRole('alert')).textContent).toContain("This countertop's view was changed after you opened it.");
    expect(onSaved).not.toHaveBeenCalled();
    currentId = 'record-shown-2';
    postStatus = 201;
    const before = gets;
    await user.click(screen.getByRole('button', { name: 'Reload the views' }));
    await waitFor(() => expect(gets).toBe(before + 1));
    // The reload clears the choice: nothing is pre-selected after it either.
    const group = await screen.findByRole('radiogroup', { name: "The architect's views" });
    for (const radio of within(group).getAllByRole('radio')) expect(radio.getAttribute('aria-checked')).toBe('false');
    await user.click(within(group).getByRole('radio', { name: /View 1:/ }));
    await user.click(screen.getByRole('button', { name: 'Use this view' }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1));
    expect(posts[1]).toEqual({ view_id: 'view-1', none_of_these: false, note: null, expected_record_id: 'record-shown-2' });
  });

  it('422: the refusal in the server\'s words, the choice kept, nothing saved', async () => {
    const user = userEvent.setup();
    const { onSaved } = setup();
    postStatus = 422;
    await user.click(await screen.findByRole('radio', { name: /View 1:/ }));
    await user.click(screen.getByRole('button', { name: 'Use this view' }));
    expect((await screen.findByRole('alert')).textContent).toBe('Synthetic refusal: that view is not in this revision.');
    expect(screen.queryByRole('button', { name: 'Reload the views' })).toBeNull();
    expect(screen.getByRole('radio', { name: /View 1:/ }).getAttribute('aria-checked')).toBe('true');
    expect(onSaved).not.toHaveBeenCalled();
  });

  it('keeps a note to 500 characters', async () => {
    setup();
    const note = await screen.findByLabelText(/Note/);
    expect(note.getAttribute('maxlength')).toBe('500');
  });
});

// ── The queue ───────────────────────────────────────────────

describe('the queue: "Choose which of the architect\'s views"', { timeout: 15_000 }, () => {
  const posts: unknown[] = [];
  beforeEach(() => {
    posts.length = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/slot-rows')) return json({ rows: [] });
      if (url.endsWith('/rules')) return json([]);
      if (url.endsWith('/actions')) return json({ items: [] });
      if (url.includes('/chain')) return json({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: 'Synthetic.' } });
      if (url.includes('/architect-pairing')) return json({ row_id: 'n', piece_count: 0, current: null, effective: null, spans: [] });
      if (url.includes('/architect-view-match')) {
        if ((init?.method ?? 'GET') === 'POST') {
          posts.push(JSON.parse(String(init?.body)));
          return json(CANDIDATES, 201);
        }
        return json(CANDIDATES);
      }
      return json({ error: 'http_error', message: 'the page picture is not ready yet', request_id: 'r' }, 404);
    }));
  });

  it('offers the picker (not the decision form), then waits for a check run', async () => {
    const user = userEvent.setup();
    const onViewPicked = vi.fn();
    // A second item still open, so the queue stays on this one after the pick.
    const rows = [row('w', 2, { outcome: 'FAIL', needs_decision: true }), row('n', 4, { architect: STATES.needs_reviewer })];
    const findings = [finding('width-w', 'FAIL'), finding('width-n', 'PASS'), finding('arch-nr', 'REVIEW_REQUIRED', { scope_row_candidate_id: 'n' })];
    render(
      <NeedsYouQueue
        open opening={1} onOpenChange={vi.fn()} rows={rows} rowsReady findings={findings} blocking={new Set(['width-w', 'arch-nr'])}
        projectId="p" packageId="k"
        handlers={{ onAction: vi.fn(async () => ({ saved: true as const })), onCorrect: vi.fn(async () => ({ saved: true as const })), onExcept: vi.fn(async () => ({ saved: true as const })) }}
        next={{ kind: 'run-checks', label: 'Run checks', disabled: false, reason: null }}
        onAct={vi.fn()} onWallSaved={vi.fn()} onViewPicked={onViewPicked} onOpenCard={vi.fn()}
        startAt={architectItemKey('n')}
      />,
    );
    expect(document.querySelector('[data-slot="queue-item"] h2')?.textContent).toBe('Countertop · page 4: matches the architect?');
    expect(document.querySelector('[data-slot="queue-decision"]')).toBeNull();
    expect(document.querySelector('[data-slot="architect-pairing"]')).toBeNull();
    await user.click(await screen.findByRole('radio', { name: /View 1:/ }));
    await user.click(screen.getByRole('button', { name: 'Use this view' }));
    await waitFor(() => expect(onViewPicked).toHaveBeenCalledWith('n'));
    expect(posts).toEqual([{ view_id: 'view-1', none_of_these: false, note: null, expected_record_id: 'record-shown-1' }]);
    expect(await screen.findByText('View chosen. It counts once the checks run again.')).toBeTruthy();
    expect(document.querySelector('[data-slot="queue-item"]')?.getAttribute('data-status')).toBe('waiting-for-run');
  });

  it('a pick the server says waits for a run is not asked again', () => {
    const rows = [row('n', 4, { architect: PICKED })];
    const findings = [finding('arch-nr', 'REVIEW_REQUIRED')];
    const items = buildQueue(rows, findings, new Set(['arch-nr']));
    const live: LiveData = { rows: new Map(rows.map((r) => [r.row_id, r])), findings: new Map(findings.map((f) => [f.id, f])), blocking: new Set(['arch-nr']), wallsSaved: new Set() };
    expect(itemStatus(items[0], live)).toBe('waiting-for-run');
    const asking = [row('n', 4, { architect: STATES.needs_reviewer })];
    const open: LiveData = { ...live, rows: new Map(asking.map((r) => [r.row_id, r])) };
    expect(itemStatus(items[0], open)).toBe('open');
    expect(itemStatus(items[0], { ...open, viewsPicked: new Set(['n']) })).toBe('waiting-for-run');
  });
});

// ── The drawing viewer's second pane ────────────────────────

describe('the drawing viewer: the architect\'s page beside the vendor\'s', { timeout: 15_000 }, () => {
  const urls: string[] = [];
  beforeEach(() => {
    urls.length = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      urls.push(url);
      if (/\/pages\/\d+\/picture/.test(url)) return new Response(new Blob([new Uint8Array([137, 80, 78, 71])], { type: 'image/png' }), { status: 200 });
      if (url.includes('/chain')) return json({ operands: [], trace: { kind: 'abstention', cause: 'held', reason: 'Synthetic.' } });
      return json({ error: 'unexpected', message: url, request_id: 'r' }, 500);
    }));
    let n = 0;
    vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: () => `blob:test-${++n}`, revokeObjectURL: () => undefined }));
  });

  it('"compared with …" opens with the architect\'s page, framing the view, from its own file', async () => {
    const r = row('m', 1, { architect: STATES.auto_matched });
    const target = { ...targetFromRow(r), showSecond: true };
    expect(target.second).toMatchObject({ page: 2, documentVersionId: ARCH, label: comparedWith(view(1)), heading: 'Sheet Z-9 · view 1 · SAMPLE ELEVATION 1' });
    expect(target.second!.region).toEqual([[0.1, 0.2], [0.18, 0.2], [0.18, 0.4], [0.1, 0.4]]);
    render(<DrawingViewerSheet target={target} opening={1} rows={[r]} projectId="p" packageId="k" onTargetChange={() => {}} onClose={() => {}} />);
    const pane = await screen.findByRole('region', { name: "Architect's view: Sheet Z-9 · view 1 · SAMPLE ELEVATION 1" });
    expect(pane.textContent).toContain(`Architect's drawing · ${comparedWith(view(1))}`);
    await waitFor(() => expect(urls).toContain(`/api/v1/projects/p/packages/k/pages/2/picture?document_version_id=${ARCH}`));
    expect(screen.getByRole('button', { name: "Architect's view" }).getAttribute('aria-pressed')).toBe('true');
  });

  it('opened from the countertop, the second pane waits for its button; a combined set has none', async () => {
    const user = userEvent.setup();
    const r = row('m', 1, { architect: STATES.auto_matched });
    const { unmount } = render(<DrawingViewerSheet target={targetFromRow(r)} opening={1} rows={[r]} projectId="p" packageId="k" onTargetChange={() => {}} onClose={() => {}} />);
    expect(document.querySelector('[data-slot="drawing-second-pane"]')).toBeNull();
    await user.click(await screen.findByRole('button', { name: "Architect's view" }));
    expect(document.querySelector('[data-slot="drawing-second-pane"]')).toBeTruthy();
    unmount();
    const c = row('c', 3, { architect: COMBINED });
    render(<DrawingViewerSheet target={{ ...targetFromRow(c), showSecond: true }} opening={2} rows={[c]} projectId="p" packageId="k" onTargetChange={() => {}} onClose={() => {}} />);
    await screen.findByRole('toolbar', { name: 'Drawing controls' });
    expect(screen.queryByRole('button', { name: "Architect's view" })).toBeNull();
    expect(document.querySelector('[data-slot="drawing-second-pane"]')).toBeNull();
  });
});

// ── Sign-off ────────────────────────────────────────────────

describe('sign-off after a pick', () => {
  const READY = { revision_id: 'rev', can_approve: false, blocking_findings: 0, blocking_finding_ids: [], reason: 'You changed inputs after the last check run. Run the checks before signing off.' };
  const SCOPE = { countertops: null, countertopsFailed: false, otherChecks: 0, total: 3 };

  it('says "Run the checks again before signing off" and offers the run; Sign off stays the server\'s call', async () => {
    const user = userEvent.setup();
    const onRunChecks = vi.fn();
    render(<SignOffPanel readiness={READY} ready={false} scope={{ ...SCOPE, viewPicksWaiting: 2 }} busy={false} onSignOff={() => {}} onReview={() => {}} onRunChecks={onRunChecks} />);
    const notice = document.querySelector('[data-part="view-picks-waiting"]')!;
    expect(notice.textContent).toContain('Run the checks again before signing off.');
    expect(notice.textContent).toContain("You chose the architect's view for 2 countertops after the last check run.");
    await user.click(within(notice as HTMLElement).getByRole('button', { name: 'Run checks' }));
    expect(onRunChecks).toHaveBeenCalledTimes(1);
    expect(screen.getByRole('button', { name: 'Sign off…' }).hasAttribute('disabled')).toBe(true);
  });

  it('says nothing about picks when there are none', () => {
    render(<SignOffPanel readiness={{ ...READY, can_approve: true, reason: null }} ready scope={{ ...SCOPE, viewPicksWaiting: 0 }} busy={false} onSignOff={() => {}} onReview={() => {}} />);
    expect(document.querySelector('[data-part="view-picks-waiting"]')).toBeNull();
  });
});
