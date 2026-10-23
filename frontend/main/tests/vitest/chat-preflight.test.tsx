// @vitest-environment jsdom
/**
 * The pre-flight health report in the Run popover (code, $0: what was found, whether the check goes
 * on, who acts), a check the pre-flight check paused (Run anyway / Cancel), and "What went wrong"
 * under "How this was checked". Synthetic data only.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { FullCheckButton } from '@/components/chat/chat-full-check';
import { AssistantMessage } from '@/components/chat/chat-message';
import { HowChecked } from '@/components/chat/chat-trust';
import type { ChatFullCheckPlan, ChatPreflight, ChatPreflightLine } from '@/lib/chat-api';
import type { ViewMessage } from '@/lib/chat-messages';
import { applyChatEvent, fromSaved } from '@/lib/chat-messages';
import { usePlanCheck, type PlanState } from '@/lib/chat-plan-check';
import { preflightMeta, preflightOrder, preflightStops, supervisorWords } from '@/lib/chat-preflight';
import { arithmeticCounts, checkCounts, checkLines } from '@/lib/chat-trust';
import { OPUS } from './fixtures/chat';

function line(change: Partial<ChatPreflightLine> & Pick<ChatPreflightLine, 'id' | 'words'>): ChatPreflightLine {
  return { level: 'note', goes_on: true, who: 'nobody', todo: null, pages: [], estimate: false, ...change };
}

const LINES: ChatPreflightLine[] = [
  line({ id: 'pages', level: 'ok', words: '96 vendor pages to check · 65 architect pages.' }),
  line({ id: 'matched', level: 'ok', words: 'Code’s first guess: 18 of 96 vendor pages have a likely architect page.', estimate: true }),
  line({ id: 'unreadable', words: '2 architect pages can’t be read: not used for matching.' }),
  line({ id: 'needs_drawing', level: 'warn', words: '25 vendor pages have no architect drawing in this upload.', who: 'GV', todo: 'Send the architect’s drawings of these items.' }),
  line({ id: 'duplicates', words: '11 vendor pages are exact copies of an earlier page: each is checked once.' }),
  line({ id: 'big', words: 'Big upload (188 pages): the check takes longer.' }),
  line({ id: 'no_spec', words: 'Not checked: materials, grade and hardware (no spec book or schedules uploaded).', who: 'GV', todo: 'Send the spec book.' }),
];

const STOP = line({
  id: 'files_swapped',
  level: 'stop',
  goes_on: false,
  who: 'you',
  words: 'The file uploaded as the architect’s looks like a vendor shop drawing (it is the same file as the vendor’s).',
  todo: 'Upload the architect’s drawings as the architect’s file.',
});

function plan(preflight: ChatPreflight): ChatFullCheckPlan {
  return { state: 'ready', summary: '96 pages · paired with the architect’s file when the check runs', doubts: [], more: 0, preflight };
}

describe('the pre-flight words', () => {
  it('says whether the check goes on and who acts; a plain fact says nothing more', () => {
    expect(preflightMeta(LINES[0])).toBeNull();
    expect(preflightMeta(LINES[2])).toBe('Goes on · Nobody needs to act');
    expect(preflightMeta(LINES[3])).toBe('Goes on · GV: Send the architect’s drawings of these items.');
    expect(preflightMeta(STOP)).toBe('Stops before spending · You: Upload the architect’s drawings as the architect’s file.');
    expect(preflightOrder([...LINES, STOP]).map((one) => one.id).slice(0, 4)).toEqual(['files_swapped', 'needs_drawing', 'pages', 'matched']);
    expect(preflightStops(plan({ lines: [STOP], stops: ['files_swapped'] }))).toBe(true);
    expect(preflightStops(plan({ lines: LINES, stops: [] }))).toBe(false);
    expect(supervisorWords({ verdict: 'warn', why: 'Most vendor pages have no architect page.' })).toBe('A second look before the check: Most vendor pages have no architect page.');
    expect(supervisorWords({ verdict: 'ok', why: 'Fine.' })).toBeNull();
  });
});

describe('the Run popover', () => {
  it('lists what was found, the estimate marked, the most important first, the rest folded', async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<FullCheckButton summary="Full check" onRun={onRun} blocked={null} loadPlan={async () => plan({ lines: LINES, stops: [], estimate: true })} />);
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    const found = await screen.findByRole('list', { name: 'What was found' });
    const rows = within(found).getAllByRole('listitem');
    expect(rows.map((row) => row.getAttribute('data-id'))).toEqual(['needs_drawing', 'pages', 'matched', 'unreadable', 'duplicates', 'big']);
    expect(rows[0].textContent).toContain('Goes on · GV: Send the architect’s drawings of these items.');
    expect(within(rows[2]).getByText('estimate')).toBeTruthy();
    expect(screen.getByText('Before you run · free')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Show 1 more' }));
    expect(within(screen.getByRole('list', { name: 'What was found' })).getAllByRole('listitem')).toHaveLength(7);
    await user.click(screen.getByRole('button', { name: 'Run' }));
    expect(onRun).toHaveBeenCalledWith();
  });

  it('with a stop, Run reads Run anyway and sends it past the stop', async () => {
    const user = userEvent.setup();
    const onRun = vi.fn();
    render(<FullCheckButton summary="Full check" onRun={onRun} blocked={null} loadPlan={async () => plan({ lines: [STOP, ...LINES], stops: ['files_swapped'] })} />);
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    const stops = await screen.findByRole('list', { name: 'Stops the check' });
    expect(stops.textContent).toContain('looks like a vendor shop drawing');
    expect(screen.getByText('GV Review would pause this check: nothing is spent until you choose.')).toBeTruthy();
    await user.click(screen.getByRole('button', { name: 'Run anyway' }));
    expect(onRun).toHaveBeenCalledWith(undefined, { runAnyway: true });
  });

  it('asks again while code works out the pairs', async () => {
    const answers: ChatFullCheckPlan[] = [plan({ lines: LINES.slice(0, 1), stops: [], state: 'working' }), plan({ lines: LINES, stops: [], state: 'ready' })];
    const load = vi.fn(async () => answers.shift() ?? plan({ lines: LINES, stops: [] }));
    const seen: PlanState[] = [];
    function Probe() {
      seen.push(usePlanCheck(load, true, 5));
      return null;
    }
    render(<Probe />);
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2));
    await waitFor(() => {
      const last = seen[seen.length - 1];
      expect(last.status === 'ready' && last.plan.preflight?.state).toBe('ready');
    });
  });

  it('shows the working line while the pairs are worked out', async () => {
    const user = userEvent.setup();
    render(<FullCheckButton summary="Full check" onRun={vi.fn()} blocked={null} loadPlan={async () => plan({ lines: LINES.slice(0, 1), stops: [], state: 'working' })} />);
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    expect(await screen.findByText(/Working out which pages match the architect’s \(free\)/)).toBeTruthy();
  });
});

const PAUSED: ChatPreflight = {
  state: 'ready',
  lines: [STOP, ...LINES],
  stops: ['files_swapped'],
  supervisor: { verdict: 'stop', why: 'The architect’s file is a copy of the vendor’s.', by: 'ai', cost_usd: '0.0009' },
  paused: true,
  run_anyway: false,
};

function pausedAnswer(): ViewMessage {
  return {
    key: 'a', id: 'msg-1', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: 'Paused before comparing anything', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [{ type: 'text', text: 'Paused before comparing anything: … Nothing was compared.', by: 'code' }], files: [], tasks: null, cost_usd: '0.00',
    stopped: 'cancelled', error: null, status: 'done', mode: 'full_check', preflight: PAUSED,
  };
}

describe('a check the pre-flight check paused', () => {
  it('says why, with Run anyway and Cancel', async () => {
    const user = userEvent.setup();
    const onRunAnyway = vi.fn();
    render(<AssistantMessage message={pausedAnswer()} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" fullCheck onRunAnyway={onRunAnyway} />);
    const card = screen.getByRole('region', { name: 'Check paused' });
    expect(within(card).getByText('Paused before comparing anything')).toBeTruthy();
    const why = within(card).getByRole('list', { name: 'Why it paused' });
    expect(why.textContent).toContain('looks like a vendor shop drawing');
    expect(why.textContent).toContain('A second look before the check: The architect’s file is a copy of the vendor’s.');
    await user.click(within(card).getByRole('button', { name: /Show what GV Review found/ }));
    expect(within(card).getAllByRole('listitem').length).toBeGreaterThan(5);
    await user.click(within(card).getByRole('button', { name: 'Run anyway' }));
    expect(onRunAnyway).toHaveBeenCalledTimes(1);
    await user.click(within(card).getByRole('button', { name: 'Cancel' }));
    expect(within(card).queryByRole('button', { name: 'Run anyway' })).toBeNull();
  });

  it('reads the preflight block from a saved answer and from the stream', () => {
    const saved = fromSaved({
      id: 'm', role: 'assistant', created_at: '2026-10-23T10:00:00Z', text: '', blocks: [{ type: 'preflight', preflight: PAUSED }],
    } as never);
    expect(saved.preflight?.paused).toBe(true);
    const live = applyChatEvent(pausedAnswer(), { event: 'preflight', data: { index: 0, preflight: { ...PAUSED, paused: false } } });
    expect(live.preflight?.paused).toBe(false);
  });
});

describe('what went wrong, under How this was checked', () => {
  it('lists the problems in plain words and counts them on the fold', async () => {
    const user = userEvent.setup();
    render(
      <HowChecked
        findings={[]}
        coverage={null}
        trust={{ problems: ['The fixed price ($36.65) ran out before the check finished.', '2 architect pages can’t be read: not used for matching.'] }}
      />,
    );
    const fold = screen.getByRole('button', { name: /How this was checked/ });
    expect(fold.textContent).toBe('How this was checked· 2 problems');
    await user.click(fold);
    const list = screen.getByRole('list', { name: 'What went wrong' });
    expect(within(list).getAllByRole('listitem').map((item) => item.textContent)).toEqual([
      'The fixed price ($36.65) ran out before the check finished.',
      '2 architect pages can’t be read: not used for matching.',
    ]);
  });

  it('says so when nothing went wrong, and older answers show nothing', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<HowChecked findings={[]} coverage={null} trust={{ problems: [] }} />);
    await user.click(screen.getByRole('button', { name: 'How this was checked' }));
    expect(screen.getByText('Nothing went wrong in this check.')).toBeTruthy();
    unmount();
    render(<HowChecked findings={[]} coverage={null} trust={{}} />);
    await user.click(screen.getByRole('button', { name: 'How this was checked' }));
    expect(screen.queryByText('What went wrong')).toBeNull();
  });

  it('never shows "undefined of undefined": the server’s arithmetic counts are read', () => {
    expect(arithmeticCounts({ adds_up: 3, does_not_add_up: 1, not_a_number: 2 })).toEqual({ checked: 4, agrees: 3 });
    expect(arithmeticCounts({ adds_up: 0, does_not_add_up: 0 })).toBeNull();
    expect(arithmeticCounts({ checked: 2, agrees: 1 })).toEqual({ checked: 2, agrees: 1 });
    const lines = checkLines(checkCounts([], { checks: { arithmetic: { adds_up: 3, does_not_add_up: 1 } as never } }));
    expect(lines).toContain('Vendor’s sizes added up by code: 3 of 4 add up');
    expect(lines.join(' ')).not.toMatch(/undefined|lead|independent/);
  });
});

describe('a broken upload', () => {
  it('shows its stop even when no page map can be made', async () => {
    const user = userEvent.setup();
    const broken: ChatFullCheckPlan = { state: 'none', summary: null, doubts: [], more: 0, preflight: { state: 'ready', lines: [line({ id: 'broken_file', level: 'stop', goes_on: false, who: 'you', words: '“architect.pdf” could not be opened: this file is locked with a password, so it could not be read.', todo: 'Upload a copy that opens (no password, not damaged).' })], stops: ['broken_file'] } };
    render(<FullCheckButton summary="Full check" onRun={vi.fn()} blocked={null} loadPlan={async () => broken} />);
    await user.click(screen.getByRole('button', { name: 'Run full check' }));
    expect((await screen.findByRole('list', { name: 'Stops the check' })).textContent).toContain('locked with a password');
    expect(screen.getByRole('button', { name: 'Run anyway' })).toBeTruthy();
  });
});
