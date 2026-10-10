// @vitest-environment jsdom
import { act, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { CountertopResult } from '@/api/client';
import type { AssistantAnswer, AssistantInfo, AssistantRequest } from '@/api/assistantTypes';
import type { Finding } from '@/data/types';
import { ApiError } from '@/api/client';
import { answerParts, blockersOf, failureWords, historyFor, matchesRecords, plainAnswer, queueKeyFor, resolveEvidence, starterHint, usableActions, type AssistantContext, type AssistantRecords } from '@/lib/assistant';
import { AssistantPanel, type AssistantNavigation } from '@/components/assistant/assistant-panel';

// Synthetic data only: nothing here comes from a client drawing.
const x = (numerator: string, denominator: string, display: string) => ({ numerator, denominator, display });
function row(id: string, page: number, overrides: Partial<CountertopResult> = {}): CountertopResult {
  return {
    finding_id: `f-${id}`, row_id: id, page_number: page, label: `Synthetic countertop ${id}`,
    row_location: null, outcome: 'PASS', needs_decision: false,
    printed_overall: x('40', '1', '40"'), pieces: [],
    field_cut_per_end: null, field_cut_count: 0, expected_total: x('40', '1', '40"'), delta: x('0', '1', '0"'),
    hold: null, reviewer_decision: null,
    wall_layout: { config: null, label: null, source: 'not established' },
    agreement: { both_readers_agreed_on_row: true, code_clue_used: false, values_agreed: [] },
    ...overrides,
  };
}
function finding(id: string, overrides: Partial<Finding> = {}): Finding {
  return { id, check_id: `RULE-${id}`, name: `Synthetic rule ${id}`, outcome: 'PASS', severity: 'major', reviewer_action: null, scope_label: null, ...overrides } as Finding;
}

const FAIL_ROW = row('row-4', 4, {
  outcome: 'FAIL', needs_decision: true,
  printed_overall: x('169', '2', '84 1/2"'), expected_total: x('691', '8', '86 3/8"'), delta: x('-15', '8', '-1 7/8"'),
  drawn_length_note: 'Synthetic: drawn length not checked for one piece.',
});
const HELD_ROW = row('row-7', 7, { outcome: 'REVIEW_REQUIRED', needs_decision: true, hold: { code: 'synthetic-hold', reason: 'Synthetic: the two readers disagree' }, expected_total: null, delta: null });
const PASS_ROW = row('row-3', 3);
const RECORDS: AssistantRecords = {
  rows: [FAIL_ROW, HELD_ROW, PASS_ROW],
  rowsReady: true,
  pagesWithoutCountertop: [{ page_number: 2, reason: 'Synthetic: wall cabinets only' }],
  rowsNotChecked: [{ page_number: 9, reason: 'Synthetic: a second countertop row' }],
  findings: [
    finding('f-row-4', { outcome: 'FAIL' }), finding('f-row-7', { outcome: 'REVIEW_REQUIRED' }), finding('f-row-3'),
    finding('f-sink', { outcome: 'NOT_FOUND', name: 'Synthetic sink check' }),
  ],
  blocking: new Set(['f-row-4', 'f-row-7', 'f-sink']),
};

const INFO: AssistantInfo = {
  enabled: true, model_label: 'Synthetic Model', keeps_no_data: true,
  starters: ['Why did page 4 fail?', 'What is left before sign-off?', 'Which pages have no countertop?'],
};

function answer(overrides: Partial<AssistantAnswer> = {}): AssistantAnswer {
  return {
    text: 'Synthetic answer.', citations: [], evidence: [], actions: [], suggestions: [],
    checked: true, mode: 'llm', model_id: 'synthetic-model', sources: ['Synthetic source line'],
    ...overrides,
  };
}

/** The page 4 answer: the model text says 99 1/2", the record says 84 1/2"; one id the records do not hold. */
const PAGE_4 = answer({
  text: 'The countertop on [[0]] is printed 99 1/2" here[[1]]. See also [[2]].',
  citations: [
    { kind: 'countertop', page_number: 4, record_id: 'row-4', label: 'page 4' },
    { kind: 'countertop', page_number: 5, record_id: 'ghost-row', label: 'page 5' },
    { kind: 'page', page_number: 2, record_id: null, label: 'page 2' },
  ],
  evidence: [{ kind: 'countertop', record_id: 'row-4' }, { kind: 'countertop', record_id: 'ghost-row' }],
  suggestions: ['What is left before sign-off?'],
  sources: ['Check result for page 4, latest run'],
});

// ── A fake server: GET info, POST stream with frames the test pushes ──

type Frame = { event: string; data: unknown };
interface Call { url: string; method: string; body: AssistantRequest | null; signal: AbortSignal | null }

function sse(frames: readonly Frame[]): string {
  return frames.map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`).join('');
}

class FakeServer {
  calls: Call[] = [];
  /** What each stream POST answers, in order: frames to send at once, 'hang' to wait for push/abort, or a thrown error. */
  replies: (Frame[] | 'hang' | Error)[] = [];
  private live: ReadableStreamDefaultController<Uint8Array> | null = null;
  info: AssistantInfo | 'fail' = INFO;

  fetch = vi.fn(async (input: string, init?: RequestInit) => {
    const method = init?.method ?? 'GET';
    const body = typeof init?.body === 'string' ? (JSON.parse(init.body) as AssistantRequest) : null;
    this.calls.push({ url: input, method, body, signal: init?.signal ?? null });
    if (method === 'GET' && input.endsWith('/assistant')) {
      if (this.info === 'fail') return { ok: false, status: 404, headers: new Headers(), json: async () => ({ error: 'not_found', message: 'Not found', request_id: 'r' }) };
      const info = this.info;
      return { ok: true, status: 200, headers: new Headers(), json: async () => info };
    }
    if (method === 'POST' && input.endsWith('/assistant/stream')) {
      const reply = this.replies.shift() ?? [{ event: 'answer', data: answer() }];
      if (reply instanceof Error) throw reply;
      const encoder = new TextEncoder();
      const signal = init?.signal ?? null;
      const stream = new ReadableStream<Uint8Array>({
        start: (controller) => {
          if (reply === 'hang') {
            this.live = controller;
            signal?.addEventListener('abort', () => controller.error(new DOMException('The user aborted a request.', 'AbortError')));
          } else {
            controller.enqueue(encoder.encode(sse(reply)));
            controller.close();
          }
        },
      });
      return { ok: true, status: 200, headers: new Headers(), body: stream, json: async () => ({}) };
    }
    throw new Error(`unexpected request ${method} ${input}`);
  });

  /** Send a frame on the open stream; a stream the client already aborted takes nothing more. */
  push(frame: Frame) {
    try {
      this.live?.enqueue(new TextEncoder().encode(sse([frame])));
    } catch {
      this.live = null;
    }
  }
  finish(frame: Frame) {
    this.push(frame);
    try {
      this.live?.close();
    } catch {
      // already errored by the abort
    }
    this.live = null;
  }
  get posts() {
    return this.calls.filter((c) => c.method === 'POST');
  }
}

let server: FakeServer;
let nav: { [K in keyof AssistantNavigation]: ReturnType<typeof vi.fn> };

function mount(props: { context?: AssistantContext | null; open?: boolean; onClose?: () => void } = {}) {
  return render(
    <AssistantPanel
      projectId="synthetic-project"
      packageId="synthetic-package"
      setName="Synthetic kitchen set"
      open={props.open ?? true}
      records={RECORDS}
      context={props.context ?? null}
      nav={nav as unknown as AssistantNavigation}
      onClose={props.onClose ?? (() => {})}
    />,
  );
}

const panel = () => screen.getByRole('complementary', { name: 'Assistant' });
/** The conversation itself (the screen-reader status line repeats the latest words). */
const thread = () => within(document.querySelector<HTMLElement>('[data-slot="assistant-thread"]')!);
const composer = () => screen.getByRole('textbox', { name: 'Your question' });

beforeEach(() => {
  server = new FakeServer();
  vi.stubGlobal('fetch', server.fetch);
  nav = { openPage: vi.fn(), showRow: vi.fn(), showFinding: vi.fn(), openQueue: vi.fn() };
  // Reduced motion: answers appear whole, so the tests need no timers.
  window.matchMedia = ((query: string) => ({
    matches: query.includes('reduce'), media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia;
});
afterEach(() => {
  vi.unstubAllGlobals();
});

// ── Rules ───────────────────────────────────────────────────

describe('assistant: rules', () => {
  it('turns [[n]] markers into chips and drops markers with no citation or an unknown record', () => {
    const parts = answerParts(PAGE_4.text, PAGE_4.citations, RECORDS);
    expect(parts.filter((p) => p.kind === 'cite').map((p) => p.kind === 'cite' && p.label)).toEqual(['p4', 'p2']);
    expect(answerParts('A [[7]] b', [], RECORDS)).toEqual([{ kind: 'text', text: 'A  b' }]);
  });

  it('copies and remembers the answer as plain text', () => {
    expect(plainAnswer(PAGE_4)).toBe('The countertop on page 4 is printed 99 1/2" herepage 5. See also page 2.');
    expect(plainAnswer({ text: 'On [[0]], it fails.', citations: [{ kind: 'page', page_number: 4, record_id: null, label: 'page 4' }] })).toBe('On page 4, it fails.');
  });

  it('marks "Matches the records" only on a checked model answer or a records-only answer', () => {
    expect(matchesRecords(answer({ mode: 'llm', checked: true }))).toBe(true);
    expect(matchesRecords(answer({ mode: 'llm', checked: false }))).toBe(false);
    expect(matchesRecords(answer({ mode: 'records_only', checked: true }))).toBe(true);
    expect(matchesRecords(answer({ mode: 'records_only', checked: false }))).toBe(false);
    expect(matchesRecords(answer({ mode: 'refused', checked: true }))).toBe(false);
    expect(matchesRecords(answer({ mode: 'disabled', checked: true }))).toBe(false);
  });

  it('sends at most six earlier messages, newest last, and leaves out turns with no answer', () => {
    const turns = [1, 2, 3, 4].map((n) => ({ question: `Q${n}`, answer: { text: `A${n}`, citations: [] } }));
    const history = historyFor([...turns, { question: 'failed', answer: null }]);
    expect(history).toHaveLength(6);
    expect(history.map((h) => h.text)).toEqual(['Q2', 'A2', 'Q3', 'A3', 'Q4', 'A4']);
  });

  it('sends history the server accepts: short chip words for markers, at most 2000 characters, no empty message', () => {
    const history = historyFor([
      { question: 'Why page 4?', answer: { text: 'On [[0]] it is short.', citations: [{ kind: 'countertop', page_number: 4, record_id: 'row-4', label: 'the countertop on page 4' }] } },
      { question: 'Long?', answer: { text: 'x'.repeat(2500), citations: [] } },
      { question: 'Empty?', answer: { text: '[[9]]', citations: [] } },
    ]);
    expect(history[1].text).toBe('On p4 it is short.');
    expect(history[3].text).toHaveLength(2000);
    expect(history.map((h) => h.text)).not.toContain('');
    expect(history.at(-1)).toEqual({ role: 'user', text: 'Empty?' });
  });

  it('opens the queue only at an item the queue lists, else offers the page', () => {
    const architectOnly = row('row-arch', 11, {
      outcome: 'PASS', needs_decision: false,
      architect: { outcome: 'REVIEW_REQUIRED', finding_id: 'f-arch', reason: 'Synthetic: confirm the pairing', needs_decision: true, not_compared_reason: null, pairing_source: 'code', pairing_judgments: 'code only', compared: [] },
    });
    const records: AssistantRecords = { ...RECORDS, rows: [...RECORDS.rows, architectOnly], findings: [...RECORDS.findings, finding('f-row-arch'), finding('f-arch', { outcome: 'REVIEW_REQUIRED' })], blocking: new Set([...RECORDS.blocking!, 'f-arch']) };
    // A row whose width is settled but whose architect pairing waits: the architect item.
    expect(queueKeyFor('row-arch', records)).toBe('architect:row-arch');
    expect(queueKeyFor('f-arch', records)).toBe('architect:row-arch');
    // A settled row is not in the queue: no queue key, and its action becomes "Open page 3".
    expect(queueKeyFor('row-3', records)).toBeNull();
    expect(usableActions([{ kind: 'open_queue_item', record_id: 'row-3', label: 'Open page 3 in the queue' }], records)).toEqual([{ kind: 'open_page', page_number: 3, label: 'Open page 3' }]);
    expect(usableActions([{ kind: 'open_queue_item', record_id: 'ghost', label: 'Ghost' }], records)).toEqual([]);
    expect(usableActions([{ kind: 'open_queue_item', record_id: 'row-7', label: 'Open page 7 in the queue' }], records)).toEqual([{ kind: 'open_queue_item', record_id: 'row-7', label: 'Open page 7 in the queue', queueKey: 'row:row-7' }]);
  });

  it('says failures in plain words, never the technical text', () => {
    expect(failureWords(new ApiError(422, { error: 'validation_error', message: 'body.history.0.text: String should have at most 2000 characters', request_id: 'r' }))).toBe('That question could not be sent. Start a new chat and ask again.');
    expect(failureWords(new ApiError(502, { error: 'model_unavailable', message: 'Synthetic: the model is unavailable.', request_id: 'r' }))).toBe('Synthetic: the model is unavailable.');
    expect(failureWords(new ApiError(500, { error: 'unreadable_response', message: 'The server returned 500 and a body this client could not parse.', request_id: 'r' }))).toBe('The assistant could not answer right now. Try again.');
    expect(failureWords(new ApiError(404, { error: 'unreadable_response', message: 'x', request_id: 'r' }))).toBe('The assistant is not available on this server.');
    expect(failureWords(new TypeError('Failed to fetch'))).toMatch(/could not be reached/);
    expect(failureWords(new SyntaxError('Unexpected token < in JSON'))).toBe('The answer could not be read. Try again.');
  });

  it('draws evidence from the records by id and skips what the records do not hold', () => {
    const resolved = resolveEvidence([...PAGE_4.evidence, { kind: 'blockers' }, { kind: 'rows_not_checked' }], RECORDS);
    expect(resolved.map((e) => e.key)).toEqual(['countertop:row-4', 'blockers', 'rows_not_checked']);
    // Lists wait for the data: nothing is drawn as "none" before the results load.
    expect(resolveEvidence([{ kind: 'blockers' }], { ...RECORDS, rowsReady: false })).toEqual([]);
    expect(resolveEvidence([{ kind: 'blockers' }], { ...RECORDS, blocking: null })).toEqual([]);
  });

  it('gives starters a reason from the records, or none', () => {
    expect(starterHint('Why did page 4 fail?', RECORDS)).toEqual({ kind: 'why', reason: 'Needs correction · waiting for you' });
    expect(starterHint('What is left before sign-off?', RECORDS)).toEqual({ kind: 'left', reason: '3 items need you' });
    expect(starterHint('Which pages have no countertop?', RECORDS)).toEqual({ kind: 'none', reason: '1 page listed, not blocking' });
    expect(starterHint('Tell me a joke', RECORDS)).toEqual({ kind: 'ask', reason: null });
  });
});

// ── The panel ───────────────────────────────────────────────

describe('assistant panel', () => {
  it('shows the starters with a reason each and sends one as the question', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer() }]);
    mount();
    const list = await screen.findByRole('list', { name: 'Suggested questions' });
    expect(within(list).getAllByRole('button')).toHaveLength(3);
    expect(list.textContent).toContain('3 items need you');
    expect(screen.getByText(/Synthetic Model · keeps no data · it explains, you decide/)).toBeTruthy();

    await user.click(within(list).getByRole('button', { name: /What is left before sign-off/ }));
    await screen.findByText('Synthetic answer.');
    expect(server.posts).toHaveLength(1);
    expect(server.posts[0].url).toBe('/api/v1/projects/synthetic-project/packages/synthetic-package/assistant/stream');
    expect(server.posts[0].body).toEqual({ question: 'What is left before sign-off?', history: [] });
  });

  it('moves focus into the composer on open and closes on Escape', async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    mount({ onClose });
    expect(document.activeElement).toBe(composer());
    await user.keyboard('{Escape}');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('shows one working line, the current stage, replacing the last', async () => {
    const user = userEvent.setup();
    server.replies.push('hang');
    mount();
    await user.type(composer(), 'Why did page 4 fail?{Enter}');
    await act(async () => server.push({ event: 'stage', data: { id: 'records', label: 'Reading this review’s records' } }));
    await thread().findByText('Reading this review’s records');
    await act(async () => server.push({ event: 'stage', data: { id: 'guard', label: 'Checking every number against the records' } }));
    await thread().findByText('Checking every number against the records');
    expect(screen.queryByText('Reading this review’s records')).toBeNull();
    expect(document.querySelectorAll('[data-slot="assistant-working"]')).toHaveLength(1);
    await act(async () => server.finish({ event: 'answer', data: answer() }));
    await screen.findByText('Synthetic answer.');
    expect(document.querySelector('[data-slot="assistant-working"]')).toBeNull();
  });

  it('turns citations into page chips that open the drawing, and skips unknown records', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: PAGE_4 }]);
    mount();
    await user.type(composer(), 'Why did page 4 fail?{Enter}');
    const chip4 = await screen.findByRole('button', { name: /^Open page 4 on the drawing/ });
    expect(screen.queryByRole('button', { name: /^Open page 5 on the drawing/ })).toBeNull();
    await user.click(chip4);
    expect(nav.showRow).toHaveBeenCalledWith(FAIL_ROW);
    await user.click(screen.getByRole('button', { name: 'Open page 2 on the drawing' }));
    expect(nav.openPage).toHaveBeenCalledWith(2);
  });

  it('draws the countertop card from the API row, never from the model text', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: PAGE_4 }]);
    mount();
    await user.type(composer(), 'Why did page 4 fail?{Enter}');
    const cards = await screen.findAllByRole('region', { name: /Countertop on page/ });
    expect(cards).toHaveLength(1); // the unknown record id is skipped
    const card = cards[0];
    expect(card.textContent).toContain('84 1/2"');
    expect(card.textContent).toContain('86 3/8"');
    expect(card.textContent).toContain('−1 7/8"');
    expect(card.textContent).not.toContain('99 1/2');
    expect(card.textContent).toContain('Needs correction');
    expect(card.textContent).toContain('Synthetic: drawn length not checked for one piece.');
    expect(within(card).getByRole('img', { name: 'Printed 84 1/2"' })).toBeTruthy();
    expect(within(card).getByRole('img', { name: 'Needed 86 3/8"' })).toBeTruthy();

    await user.click(within(card).getByRole('button', { name: /Show on drawing/ }));
    expect(nav.showRow).toHaveBeenCalledWith(FAIL_ROW);
    await user.click(within(card).getByRole('button', { name: /Open in queue/ }));
    expect(nav.openQueue).toHaveBeenCalledWith('row:row-4');
  });

  it('lists what blocks sign-off from the queue data, each opening its queue item', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer({ text: 'Three things need you.', evidence: [{ kind: 'blockers' }, { kind: 'no_countertop_pages' }] }) }]);
    mount();
    await user.type(composer(), 'What is left?{Enter}');
    const list = await screen.findByRole('list', { name: 'What is left before sign-off' });
    const items = within(list).getAllByRole('button');
    // The queue's own order: countertops by page, then the other checks.
    expect(items.map((item) => item.textContent)).toEqual([
      expect.stringContaining('A fail with no decision yet'),
      expect.stringContaining('Synthetic: the two readers disagree'),
      expect.stringContaining('Synthetic sink check'),
    ]);
    await user.click(items[1]);
    expect(nav.openQueue).toHaveBeenCalledWith('row:row-7');
    await user.click(items[2]);
    expect(nav.openQueue).toHaveBeenCalledWith('finding:f-sink');
    await user.click(screen.getByRole('button', { name: 'Page 2: open it on the drawing' }));
    expect(nav.openPage).toHaveBeenCalledWith(2);
  });

  it('shows "Matches the records" on a checked answer and never on a refusal, a switched-off answer or an error', async () => {
    const user = userEvent.setup();
    server.replies.push(
      [{ event: 'answer', data: answer({ text: 'Checked answer.' }) }],
      [{ event: 'answer', data: answer({ text: 'I cannot record decisions.', mode: 'refused', checked: true }) }],
      [{ event: 'answer', data: answer({ text: 'The assistant is off.', mode: 'disabled', checked: true }) }],
      [{ event: 'error', data: { code: 'model_error', message: 'Synthetic: the model did not answer.' } }],
    );
    mount();
    await user.type(composer(), 'One{Enter}');
    await screen.findByText('Checked answer.');
    expect(document.querySelectorAll('[data-slot="assistant-checked"]')).toHaveLength(1);
    await user.type(composer(), 'Two{Enter}');
    await screen.findByText('I cannot record decisions.');
    await user.type(composer(), 'Three{Enter}');
    await screen.findByText('The assistant is off.');
    await user.type(composer(), 'Four{Enter}');
    await thread().findByText('Synthetic: the model did not answer.');
    expect(document.querySelectorAll('[data-slot="assistant-checked"]')).toHaveLength(1);
  });

  it('keeps the starters after a switched-off answer', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer({ text: 'The assistant is switched off for this set.', mode: 'disabled', checked: false }) }]);
    mount();
    await screen.findByRole('list', { name: 'Suggested questions' });
    await user.type(composer(), 'Hello{Enter}');
    await screen.findByText('The assistant is switched off for this set.');
    expect(screen.getByRole('list', { name: 'Suggested questions' })).toBeTruthy();
  });

  it('stops a running answer with Stop', async () => {
    const user = userEvent.setup();
    server.replies.push('hang');
    mount();
    await user.type(composer(), 'Slow question{Enter}');
    const stop = await screen.findByRole('button', { name: 'Stop' });
    expect(screen.queryByRole('button', { name: 'Send' })).toBeNull();
    await user.click(stop);
    await thread().findByText(/Stopped/);
    expect(server.posts[0].signal?.aborted).toBe(true);
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
  });

  it('sends on Enter, adds a line on Shift+Enter, and never sends an empty question', async () => {
    const user = userEvent.setup();
    mount();
    const box = composer() as HTMLTextAreaElement;
    expect((screen.getByRole('button', { name: 'Send' }) as HTMLButtonElement).disabled).toBe(true);
    await user.type(box, '{Enter}');
    expect(server.posts).toHaveLength(0);
    await user.type(box, 'First line{Shift>}{Enter}{/Shift}second line');
    expect(box.value).toBe('First line\nsecond line');
    expect(server.posts).toHaveLength(0);
    await user.keyboard('{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(1));
    expect(server.posts[0].body?.question).toBe('First line\nsecond line');
    expect(box.value).toBe('');
  });

  it('sends the page being looked at as focus, and not once the chip is removed', async () => {
    const user = userEvent.setup();
    mount({ context: { page_number: 7, record_id: 'row-7' } });
    expect(screen.getByText(/^Page/, { selector: '[data-slot="assistant-context"] span' })).toBeTruthy();
    await user.type(composer(), 'Why does this page need me?{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(1));
    expect(server.posts[0].body?.focus).toEqual({ page_number: 7, record_id: 'row-7' });
    const question = await screen.findByText('Why does this page need me?');
    expect(question.textContent).toContain('About page 7');

    await screen.findByText('Synthetic answer.');
    await user.click(screen.getByRole('button', { name: 'Stop asking about page 7' }));
    expect(document.querySelector('[data-slot="assistant-context"]')).toBeNull();
    await user.type(composer(), 'And the whole set?{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(2));
    expect(server.posts[1].body).not.toHaveProperty('focus');
  });

  it('says an error in plain words and tries again', async () => {
    const user = userEvent.setup();
    server.replies.push(
      [{ event: 'error', data: { code: 'records_unavailable', message: 'Synthetic: the records could not be read.' } }],
      new TypeError('Failed to fetch'),
      [{ event: 'answer', data: answer({ text: 'Answered on the third try.' }) }],
    );
    mount();
    await user.type(composer(), 'Why?{Enter}');
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('Synthetic: the records could not be read.');
    await user.click(within(alert).getByRole('button', { name: 'Try again' }));
    await waitFor(() => expect(screen.getByRole('alert').textContent).toContain('could not be reached'));
    await user.click(within(screen.getByRole('alert')).getByRole('button', { name: 'Try again' }));
    await screen.findByText('Answered on the third try.');
    expect(server.posts.map((p) => p.body?.question)).toEqual(['Why?', 'Why?', 'Why?']);
    // A failed turn is not part of the history.
    expect(server.posts[2].body?.history).toEqual([]);
    expect(screen.getAllByText('Why?')).toHaveLength(1);
  });

  it('sends at most six earlier messages', async () => {
    const user = userEvent.setup();
    for (const n of [1, 2, 3, 4]) server.replies.push([{ event: 'answer', data: answer({ text: `Answer ${n}.` }) }]);
    mount();
    for (const n of [1, 2, 3, 4]) {
      await user.type(composer(), `Question ${n}{Enter}`);
      await screen.findByText(`Answer ${n}.`);
    }
    await user.type(composer(), 'Question 5{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(5));
    expect(server.posts[4].body?.history).toEqual([
      { role: 'user', text: 'Question 2' }, { role: 'assistant', text: 'Answer 2.' },
      { role: 'user', text: 'Question 3' }, { role: 'assistant', text: 'Answer 3.' },
      { role: 'user', text: 'Question 4' }, { role: 'assistant', text: 'Answer 4.' },
    ]);
  });

  it('offers follow-up questions after the latest answer only, and shows sources on demand', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: PAGE_4 }], [{ event: 'answer', data: answer({ text: 'Second answer.', suggestions: [] }) }]);
    mount();
    await user.type(composer(), 'Why did page 4 fail?{Enter}');
    const follow = await screen.findByRole('button', { name: 'What is left before sign-off?' });
    expect(screen.queryByText('Check result for page 4, latest run')).toBeNull();
    await user.click(screen.getByRole('button', { name: 'Sources' }));
    expect(screen.getByText('Check result for page 4, latest run')).toBeTruthy();
    await user.click(follow);
    await screen.findByText('Second answer.');
    expect(screen.queryByRole('button', { name: 'What is left before sign-off?' })).toBeNull();
  });

  it('records no decision: every button in the panel only asks or navigates', async () => {
    const user = userEvent.setup();
    const everything = answer({
      text: 'On [[0]] the countertop is short.',
      citations: [{ kind: 'countertop', page_number: 4, record_id: 'row-4', label: 'page 4' }],
      evidence: [{ kind: 'countertop', record_id: 'row-4' }, { kind: 'blockers' }, { kind: 'no_countertop_pages' }, { kind: 'rows_not_checked' }],
      actions: [{ kind: 'open_page', page_number: 4, label: 'Open page 4' }, { kind: 'open_queue_item', record_id: 'row-7', label: 'Open page 7 in the queue' }],
      suggestions: ['Mark page 7 as passed'],
    });
    for (let i = 0; i < 20; i += 1) server.replies.push([{ event: 'answer', data: everything }]);
    mount({ context: { page_number: 4, record_id: 'row-4' } });
    await user.type(composer(), 'Mark page 4 as passed{Enter}');
    await screen.findByRole('region', { name: 'Countertop on page 4' });

    // Everything in the conversation first; New chat and Close (which clear and close it) last.
    const last = ['New chat', 'Close assistant'];
    const buttons = within(panel()).getAllByRole('button');
    expect(buttons.length).toBeGreaterThan(10);
    const ordered = [...buttons.filter((b) => !last.includes(b.getAttribute('aria-label') ?? '')), ...buttons.filter((b) => last.includes(b.getAttribute('aria-label') ?? ''))];
    for (const button of ordered) {
      if (!button.isConnected || (button as HTMLButtonElement).disabled) continue;
      await user.click(button);
    }
    for (const call of server.calls) {
      expect(['GET', 'POST']).toContain(call.method);
      expect(call.url).toMatch(/\/assistant(\/stream)?$/);
    }
    expect(server.calls.some((c) => /actions|decide|exceptions|approve|sessions|evidence/.test(c.url))).toBe(false);
    expect(nav.openQueue).toHaveBeenCalledWith('row:row-7');
    expect(nav.openPage).toHaveBeenCalledWith(2);
    // The answer's own "Open page 4" and "Open page 7 in the queue" duplicated the card and the list: not shown.
    expect(nav.openPage).not.toHaveBeenCalledWith(4);
  });

  it('still lets the reviewer ask when the starters cannot be loaded', async () => {
    server.info = 'fail';
    mount();
    await screen.findByText(/Suggested questions could not be loaded/);
    expect(screen.queryByRole('list', { name: 'Suggested questions' })).toBeNull();
    expect((composer() as HTMLTextAreaElement).disabled).toBe(false);
  });
});

describe('assistant panel: less clutter', () => {
  it('hides an answer action that a card or list in the same answer already offers', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer({
      text: 'Page 4 is short.',
      evidence: [{ kind: 'countertop', record_id: 'row-4' }],
      actions: [
        { kind: 'open_page', page_number: 4, label: 'Open page 4' },
        { kind: 'open_queue_item', record_id: 'row-4', label: 'Open page 4 in the queue' },
        { kind: 'open_queue_item', record_id: 'row-7', label: 'Open page 7 in the queue' },
      ],
    }) }]);
    mount();
    await user.type(composer(), 'Page 4?{Enter}');
    await screen.findByRole('region', { name: 'Countertop on page 4' });
    expect(screen.queryByRole('button', { name: 'Open page 4' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Open page 4 in the queue' })).toBeNull();
    expect(screen.getByRole('button', { name: 'Open page 7 in the queue' })).toBeTruthy();
    expect(screen.getAllByRole('button', { name: /Show on drawing/ })).toHaveLength(1);
  });

  it('names other checks as "Other checks" does, groups identical ones, and caps the list', async () => {
    const user = userEvent.setup();
    const checks = Array.from({ length: 6 }, (_, i) => finding(`f-pkg-${i}`, { check_id: 'SYNTH-PKG', name: 'SYNTH-PKG', scope_label: 'Package revision', outcome: 'NOT_FOUND' }));
    const held = Array.from({ length: 9 }, (_, i) => row(`row-h${i}`, 20 + i, { outcome: 'REVIEW_REQUIRED', needs_decision: true, hold: { code: 'x', reason: `synthetic: readers disagree on piece ${i}. More words follow here.` } }));
    const records: AssistantRecords = {
      ...RECORDS,
      rows: [...RECORDS.rows, ...held],
      findings: [...RECORDS.findings, ...checks, ...held.map((r) => finding(r.finding_id!, { outcome: 'REVIEW_REQUIRED' }))],
      blocking: new Set([...RECORDS.blocking!, ...checks.map((f) => f.id), ...held.map((r) => r.finding_id!)]),
      ruleNames: new Map([['SYNTH-PKG', 'Synthetic sink cut-out check']]),
    };
    server.replies.push([{ event: 'answer', data: answer({ text: 'Lots left.', evidence: [{ kind: 'blockers' }] }) }]);
    render(
      <AssistantPanel projectId="synthetic-project" packageId="synthetic-package" setName="Synthetic kitchen set" open
        records={records} context={null} nav={nav as unknown as AssistantNavigation} onClose={() => {}} />,
    );
    await user.type(composer(), 'What is left?{Enter}');
    const list = await screen.findByRole('list', { name: 'What is left before sign-off' });
    const lines = within(list).getAllByRole('button');
    expect(lines).toHaveLength(8);
    // Countertops: "Page N", the reason's first sentence in sentence case, the badge at the end.
    expect(lines[2].textContent).toBe('Page 20Synthetic: readers disagree on piece 0Needs your decision');
    // 2 countertops + 9 held + 1 sink check + 1 group of six = 13 lines; 8 shown, 5 lines (10 results) left.
    expect(screen.getByText(/more in the queue/).textContent).toBe('and 10 more in the queue');
    await user.click(screen.getByRole('button', { name: 'Open queue' }));
    expect(nav.openQueue).toHaveBeenCalledWith('row:row-h6');
    expect(blockersOf(records).find((b) => b.title === 'Synthetic sink cut-out check')).toMatchObject({ count: 6, detail: '6 results', word: 'Waiting on a value' });
    expect(blockersOf(records).filter((b) => b.title.includes('Package revision'))).toEqual([]);
  });

  it('cuts a long listed reason to two lines with a "Show more" that works by keyboard', async () => {
    const user = userEvent.setup();
    const long = 'Synthetic: both readers say "' + 'the elevation shows only wall cabinets and a soffit, '.repeat(4) + 'so there is no countertop line on this sheet."';
    const records: AssistantRecords = { ...RECORDS, pagesWithoutCountertop: [{ page_number: 2, reason: long }, { page_number: 5, reason: 'Synthetic: short.' }] };
    server.replies.push([{ event: 'answer', data: answer({ text: 'Two pages.', evidence: [{ kind: 'no_countertop_pages' }] }) }]);
    render(
      <AssistantPanel projectId="synthetic-project" packageId="synthetic-package" setName="Synthetic kitchen set" open
        records={records} context={null} nav={nav as unknown as AssistantNavigation} onClose={() => {}} />,
    );
    await user.type(composer(), 'No countertop?{Enter}');
    const more = await screen.findByRole('button', { name: 'Show more' });
    expect(screen.getAllByRole('button', { name: /Show more/ })).toHaveLength(1); // the short reason has none
    const reason = document.getElementById(more.getAttribute('aria-controls')!)!;
    expect(reason.className).toContain('line-clamp-2');
    expect(more.getAttribute('aria-expanded')).toBe('false');
    more.focus();
    await user.keyboard('{Enter}');
    expect(more.getAttribute('aria-expanded')).toBe('true');
    expect(more.textContent).toBe('Show less');
    expect(reason.className).not.toContain('line-clamp-2');
  });
});

describe('assistant panel: more states', () => {
  it('shows the recorded outcome inside a chip, whatever the answer text says', async () => {
    const user = userEvent.setup();
    // The text claims page 4 passed; the record says it needs correction, and the chip says so.
    server.replies.push([{ event: 'answer', data: answer({
      text: 'The countertop on [[0]] passed. See also [[1]].',
      citations: [
        { kind: 'countertop', page_number: 4, record_id: 'row-4', label: 'page 4' },
        { kind: 'page', page_number: 2, record_id: null, label: 'page 2' },
      ],
    }) }]);
    mount();
    await user.type(composer(), 'Did page 4 pass?{Enter}');
    const chip = await screen.findByRole('button', { name: 'Open page 4 on the drawing: Needs correction' });
    expect(chip.getAttribute('title')).toBe('Open page 4 on the drawing: Needs correction');
    expect(chip.querySelector('[data-outcome-icon="FAIL"]')).not.toBeNull();
    expect(chip.textContent).toBe('p4');
    // A page-only chip carries no outcome.
    const page = screen.getByRole('button', { name: 'Open page 2 on the drawing' });
    expect(page.querySelector('[data-outcome-icon]')).toBeNull();
  });

  it('keeps starters and the composer working when the model is switched off, and shows the records-only answer', async () => {
    const user = userEvent.setup();
    server.info = { ...INFO, enabled: false };
    server.replies.push([{ event: 'answer', data: answer({ text: 'Two countertops need you.', mode: 'records_only', checked: true, model_id: null, evidence: [{ kind: 'blockers' }] }) }]);
    mount();
    await screen.findByText(/The model is switched off on this server/);
    expect((composer() as HTMLTextAreaElement).disabled).toBe(false);
    const list = screen.getByRole('list', { name: 'Suggested questions' });
    await user.click(within(list).getByRole('button', { name: /What is left before sign-off/ }));
    await thread().findByText('Two countertops need you.');
    expect(server.posts[0].body?.question).toBe('What is left before sign-off?');
    expect(screen.getByRole('list', { name: 'What is left before sign-off' })).toBeTruthy();
    expect(document.querySelectorAll('[data-slot="assistant-checked"]')).toHaveLength(1);
  });

  it('offers "Open in queue" only for a countertop the queue lists', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer({ text: 'Page 3 looks right.', evidence: [{ kind: 'countertop', record_id: 'row-3' }] }) }]);
    mount();
    await user.type(composer(), 'Page 3?{Enter}');
    const card = await screen.findByRole('region', { name: 'Countertop on page 3' });
    expect(within(card).queryByRole('button', { name: /Open in queue/ })).toBeNull();
    expect(within(card).getByRole('button', { name: /Show on drawing/ })).toBeTruthy();
  });

  it('says "Stopped." and nothing more after Stop', async () => {
    const user = userEvent.setup();
    server.replies.push('hang');
    mount();
    await user.type(composer(), 'Slow{Enter}');
    await user.click(await screen.findByRole('button', { name: 'Stop' }));
    const line = await waitFor(() => document.querySelector('[data-slot="assistant-error"]')!);
    expect(line.textContent).toBe('Stopped.Try again');
  });

  it('stops the request when the panel goes away mid-answer', async () => {
    const user = userEvent.setup();
    server.replies.push('hang');
    const view = mount();
    await user.type(composer(), 'Slow{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(1));
    view.unmount();
    expect(server.posts[0].signal?.aborted).toBe(true);
  });

  it('New chat mid-answer stops the request and nothing comes back into the empty thread', async () => {
    const user = userEvent.setup();
    server.replies.push('hang');
    mount();
    await user.type(composer(), 'Slow{Enter}');
    await waitFor(() => expect(server.posts).toHaveLength(1));
    await user.click(screen.getByRole('button', { name: 'New chat' }));
    expect(server.posts[0].signal?.aborted).toBe(true);
    await act(async () => server.finish({ event: 'answer', data: answer({ text: 'Too late.' }) }));
    expect(screen.queryByText('Too late.')).toBeNull();
    expect(screen.queryByText('Slow')).toBeNull();
    expect(screen.getByText('Ask about this set')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy();
  });

  it('announces "Answer ready" once, after the words are on screen, and errors only through their alert', async () => {
    const user = userEvent.setup();
    server.replies.push([{ event: 'answer', data: answer() }], [{ event: 'error', data: { code: 'x', message: 'Synthetic failure.' } }]);
    mount();
    const status = screen.getByRole('status');
    await user.type(composer(), 'One{Enter}');
    await waitFor(() => expect(status.textContent).toBe('Answer ready'));
    await user.type(composer(), 'Two{Enter}');
    await screen.findByRole('alert');
    expect(status.textContent).toBe('');
    expect(screen.getAllByText('Synthetic failure.')).toHaveLength(1);
  });

  it('on a phone it is a modal: the page outside it is inert until it closes', async () => {
    window.matchMedia = ((query: string) => ({
      matches: query.includes('reduce') || query.includes('max-width: 900px'), media: query, onchange: null,
      addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
    })) as unknown as typeof window.matchMedia;
    const outside = document.createElement('div');
    outside.textContent = 'Synthetic review underneath';
    document.body.appendChild(outside);
    const view = mount();
    const dialog = screen.getByRole('dialog', { name: 'Assistant' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(outside.hasAttribute('inert')).toBe(true);
    view.rerender(
      <AssistantPanel projectId="synthetic-project" packageId="synthetic-package" setName="Synthetic kitchen set" open={false}
        records={RECORDS} context={null} nav={nav as unknown as AssistantNavigation} onClose={() => {}} />,
    );
    expect(outside.hasAttribute('inert')).toBe(false);
    outside.remove();
  });
});
