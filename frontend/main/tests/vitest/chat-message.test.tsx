// @vitest-environment jsdom
/**
 * One chat message, claude.ai's look and motion (#1470): the work folded into one row with its
 * summary, thinking folded to "Thought for Ns", the task checklist, helper rows, file cards, Copy
 * and Retry, the stopped words and reduced motion. Synthetic data only.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AssistantMessage, UserMessage } from '@/components/chat/chat-message';
import type { ChatTask } from '@/lib/chat-api';
import type { ViewBlock, ViewMessage } from '@/lib/chat-messages';
import { OPUS } from './fixtures/chat';

function answer(change: Partial<ViewMessage> = {}): ViewMessage {
  return {
    key: 'a', id: 'a', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '', model: 'claude-opus-5-5', effort: 'high', thinking: true,
    blocks: [], files: [], tasks: null, cost_usd: '1.72', stopped: null, error: null, status: 'done', ...change,
  };
}

function show(message: ViewMessage, props: Partial<React.ComponentProps<typeof AssistantMessage>> = {}) {
  render(<AssistantMessage message={message} chatId="c-1" models={[OPUS]} messageCapUsd="5.00" {...props} />);
  return screen.getByRole('article', { name: 'Answer' });
}

const step = (id: string, tool: Extract<ViewBlock, { type: 'step' }>['step']['tool'], extra: object = {}, live = false): ViewBlock =>
  ({ type: 'step', step: { id, tool, label: `Step ${id}`, ok: live ? null : true, ...extra }, live });

let restoreMatchMedia: (() => void) | null = null;
afterEach(() => {
  restoreMatchMedia?.();
  restoreMatchMedia = null;
});

function reduceMotion() {
  const original = window.matchMedia;
  window.matchMedia = ((query: string) => ({
    matches: query.includes('prefers-reduced-motion'), media: query, onchange: null,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {}, dispatchEvent: () => false,
  })) as typeof window.matchMedia;
  restoreMatchMedia = () => { window.matchMedia = original; };
}

describe('the work behind an answer', () => {
  it('folds consecutive steps into one row that sums them up, and opens to a timeline', async () => {
    const node = show(answer({
      blocks: [
        step('1', 'bash_tool', { command: 'ls', output: 'a.pdf' }), step('2', 'view', { picture_id: 'p1' }), step('3', 'bash_tool'),
        step('4', 'view', { picture_id: 'p2' }), step('5', 'bash_tool'), step('6', 'create_file', { path: '/mnt/user-data/outputs/m.pdf' }),
        { type: 'text', text: 'Done.' },
      ],
    }));
    const row = within(node).getByRole('button', { name: 'Read the drawing data 3 times, looked at 2 pictures, made a file' });
    expect(row.getAttribute('aria-expanded')).toBe('false');
    expect(node.querySelectorAll('[data-slot="chat-activity"]')).toHaveLength(1);

    const user = userEvent.setup();
    await user.click(row);
    expect(row.getAttribute('aria-expanded')).toBe('true');
    expect(node.querySelectorAll('[data-slot="chat-timeline"] > [data-slot="chat-entry"]')).toHaveLength(6);

    // A step opens to what it did; its command and output wait under a closed "Technical details" (#1712).
    const first = within(node).getByRole('button', { name: 'Step 1' });
    await user.click(first);
    expect(first.getAttribute('aria-expanded')).toBe('true');
    expect(node.querySelector('[data-slot="chat-step-command"]')).toBeNull();
    const technical = within(node).getByRole('button', { name: 'Technical details' });
    expect(technical.getAttribute('aria-expanded')).toBe('false');
    await user.click(technical);
    expect(node.querySelector('[data-slot="chat-step-command"]')?.textContent).toBe('ls');
    expect(node.querySelector('[data-slot="chat-step-output"]')?.textContent).toBe('a.pdf');

    // A picture opens to its thumbnail, which zooms in a lightbox.
    await user.click(within(node).getByRole('button', { name: 'Step 2' }));
    await user.click(within(node).getByRole('button', { name: 'Zoom in: Step 2' }));
    const zoom = await screen.findByRole('dialog');
    expect(within(zoom).getByRole('img', { name: 'Picture: Step 2' }).getAttribute('src')).toBe('/api/v1/chats/c-1/pictures/p1');
  });

  it('while running, the row shows the running step’s own description, shimmering, and the spark turns', () => {
    const node = show(answer({ status: 'running', cost_usd: null, blocks: [step('1', 'bash_tool'), { type: 'step', step: { id: '2', tool: 'bash_tool', label: 'Render page 3', ok: null }, live: true }] }));
    const toggle = node.querySelector('[data-slot="chat-activity-toggle"]');
    expect(toggle?.textContent).toBe('Render page 3');
    expect(toggle?.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('true');
    // GV's sign (#1534) turns while a step runs.
    const sign = node.querySelector<HTMLElement>('[data-slot="chat-working"] [data-slot="gv-spark"]')!;
    expect([sign.getAttribute('data-motion'), sign.getAttribute('data-state')]).toEqual(['full', 'working']);
    expect(node.querySelector('[data-slot="chat-footer"]')).toBeNull();
  });
});

describe('thinking', () => {
  it('is folded to "Thought for 12s" and opens to the thinking, smaller and muted', async () => {
    // A full answer (three lines): a one-line answer shows only its words (#1483).
    const node = show(answer({ blocks: [{ type: 'thinking', text: 'Compare the **two sets**.', seconds: 12, live: false }, { type: 'text', text: 'Answer.\n\nMore.\n\nEnd.' }] }));
    const button = within(node).getByRole('button', { name: 'Thought for 12s' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    const body = document.getElementById(button.getAttribute('aria-controls')!)!;
    expect(body.getAttribute('data-open')).toBe('false');
    expect(body.hasAttribute('inert')).toBe(true);
    await userEvent.setup().click(button);
    expect(button.getAttribute('aria-expanded')).toBe('true');
    expect(body.getAttribute('data-open')).toBe('true');
    expect(body.hasAttribute('inert')).toBe(false);
    expect(within(body).getByText('two sets').tagName).toBe('STRONG');
  });

  it('while it streams, says its newest line with a shimmer, still folded', () => {
    const node = show(answer({ status: 'running', blocks: [{ type: 'thinking', text: '**Plan**\nReading the vendor pages', seconds: null, live: true }] }));
    const button = within(node).getByRole('button', { name: 'Reading the vendor pages' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    expect(button.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('true');
  });

  it('a message with no thinking shows none', () => {
    const node = show(answer({ thinking: false, blocks: [{ type: 'text', text: 'Just words.' }] }));
    expect(node.querySelector('[data-slot="chat-thinking"]')).toBeNull();
    expect(node.querySelector('[data-slot="chat-activity"]')).toBeNull();
    expect(within(node).queryByText(/Thought/)).toBeNull();
  });
});

describe('the task checklist', () => {
  const tasks: ChatTask[] = [
    { id: '1', subject: 'Compare widths', status: 'completed' },
    { id: '2', subject: 'Check sinks', active_form: 'Checking sinks', status: 'in_progress' },
    { id: '3', subject: 'Write the summary', status: 'pending' },
  ];

  it('sits inside the activity timeline: a circle to do, a spinner while in progress, a check and struck through when done', async () => {
    const node = show(answer({ status: 'running', tasks, blocks: [step('1', 'TaskCreate', { tasks: [] }), step('2', 'bash_tool')] }));
    await userEvent.setup().click(node.querySelector('[data-slot="chat-activity-toggle"]') as HTMLElement);
    // The newest list (from the `tasks` events) shows, inside the timeline, not as a loose card.
    const list = node.querySelector('[data-slot="chat-timeline"] [data-slot="chat-entry"][data-kind="tasks"] [data-slot="chat-tasks"]')!;
    expect(node.querySelectorAll('[data-slot="chat-tasks"]')).toHaveLength(1);
    expect(list.textContent).toContain('1 of 3 done');
    const [done, working, todo] = [...list.querySelectorAll('[data-slot="chat-task"]')];
    expect(done.getAttribute('data-status')).toBe('completed');
    expect(done.querySelector('.line-through')?.textContent).toBe('Compare widths');
    expect(working.textContent).toContain('Checking sinks');
    expect(working.querySelector('[data-slot="chat-spinner"]')).not.toBeNull();
    expect(todo.querySelector('[data-slot="chat-spinner"]')).toBeNull();
    expect(todo.textContent).toContain('Write the summary');
    expect(todo.textContent).toContain('(to do)');
  });

  it('an earlier task update opens to the list as it was then', async () => {
    const before: ChatTask[] = [{ id: '1', subject: 'Compare widths', status: 'pending' }];
    const node = show(answer({ tasks, blocks: [step('1', 'TaskCreate', { tasks: before }), step('2', 'TaskUpdate', { tasks }), { type: 'text', text: 'Done.' }] }));
    const user = userEvent.setup();
    await user.click(within(node).getByRole('button', { name: 'Updated its plan' }));
    await user.click(within(node).getByRole('button', { name: 'Step 1' }));
    const lists = [...node.querySelectorAll('[data-slot="chat-tasks"]')].map((list) => list.textContent);
    expect(lists).toHaveLength(2);
    expect(lists.some((text) => text?.includes('0 of 1 done'))).toBe(true);
    expect(lists.some((text) => text?.includes('1 of 3 done'))).toBe(true);
  });
});

describe('helper agents', () => {
  const agents = [
    { id: 'g1', description: 'Pages 1-4', model: 'claude-sonnet-5-5', ok: null, report: null, steps: [{ id: 'x', tool: 'bash_tool' as const, label: 'Render page 2', ok: null }], running: true },
    { id: 'g2', description: 'Pages 5-8', model: 'claude-haiku-5-5', ok: true, report: 'Page 6 differs.', steps: [], running: false },
  ];

  it('"Asking 2 helpers…" with one row per helper: its description, model and status', async () => {
    const node = show(answer({ status: 'running', blocks: [{ type: 'step', step: { id: 'a', tool: 'Agent', label: 'Agents', ok: null, agents }, live: true }] }));
    expect(node.querySelector('[data-slot="chat-activity-toggle"]')?.textContent).toBe('Asking 2 helpers…');
    await userEvent.setup().click(node.querySelector('[data-slot="chat-activity-toggle"]') as HTMLElement);
    const rows = [...node.querySelectorAll('[data-slot="chat-agent"]')];
    expect(rows.map((row) => row.getAttribute('data-status'))).toEqual(['running', 'done']);
    expect(rows[0].textContent).toContain('Pages 1-4');
    expect(rows[0].textContent).toContain('Sonnet 5.5 · Running');
    expect(rows[0].textContent).toContain('Render page 2');
    expect(rows[1].textContent).toContain('Haiku 5.5 · Done');
  });

  it('a finished helper opens to its steps and report; a stopped one says why', async () => {
    const finished = [{ ...agents[0], running: false, ok: false, stopped: 'cost_cap' as const, report: 'Pages 1-3 checked.' }, agents[1]];
    const node = show(answer({ blocks: [{ type: 'step', step: { id: 'a', tool: 'Agent', label: 'Agents', ok: true, agents: finished }, live: false }, { type: 'text', text: 'Done.' }] }));
    const user = userEvent.setup();
    await user.click(within(node).getByRole('button', { name: 'Asked 2 helpers' }));
    const helper = within(node).getByRole('button', { name: /Pages 1-4/ });
    expect(helper.textContent).toContain('Stopped at the cost limit');
    await user.click(helper);
    expect(helper.getAttribute('aria-expanded')).toBe('true');
    expect(within(node).getByText('Pages 1-3 checked.')).toBeTruthy();
    expect(within(node).getByRole('button', { name: 'Render page 2' })).toBeTruthy();
  });
});

describe('files, Copy and Retry', () => {
  it('file cards: an icon, the name, kind and size, and Download, in a grid', () => {
    const node = show(answer({
      blocks: [{ type: 'text', text: 'Here.' }],
      files: [
        { id: 'f1', name: 'markup.pdf', size: 2.1 * 1024 * 1024, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f1' },
        { id: 'f2', name: 'issues.csv', size: 900, mime: 'text/csv', url: '' },
      ],
    }));
    const cards = node.querySelectorAll('[data-slot="chat-files"] > [data-slot="chat-file"]');
    expect(cards).toHaveLength(2);
    expect(within(node).getByText('PDF · 2.1 MB')).toBeTruthy();
    expect(within(node).getByText('CSV · 900 B')).toBeTruthy();
    expect(within(node).getByRole('link', { name: 'Download issues.csv' }).getAttribute('href')).toBe('/api/v1/chats/c-1/files/f2');
  });

  it('Copy copies the markdown and says "Copied"; Retry asks again; the meta line names model, effort and cost', async () => {
    const onRetry = vi.fn();
    const user = userEvent.setup();
    const node = show(answer({ text: '## Result\n| A | B |\n|---|---|\n| 1 | 2 |', blocks: [{ type: 'text', text: '## Result' }] }), { onRetry, latest: true });
    expect(node.querySelector('[data-slot="chat-actions"]')?.className).toContain('opacity-100');
    await user.click(within(node).getByRole('button', { name: 'Copy' }));
    expect(await navigator.clipboard.readText()).toBe('## Result\n| A | B |\n|---|---|\n| 1 | 2 |');
    expect(within(node).getByRole('button', { name: 'Copied' })).toBeTruthy();
    await user.click(within(node).getByRole('button', { name: 'Retry' }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(node.querySelector('[data-slot="chat-meta"]')?.textContent).toBe('Claude Opus 5.5 · High effort · $1.72');
  });

  it('older answers show their actions on hover; no Retry without the page’s callback, or while running', () => {
    const node = show(answer({ blocks: [{ type: 'text', text: 'Old.' }] }));
    expect(node.querySelector('[data-slot="chat-actions"]')?.className).toContain('opacity-0');
    expect(within(node).queryByRole('button', { name: 'Retry' })).toBeNull();
  });

  it('a code block has its own Copy', async () => {
    const user = userEvent.setup();
    const node = show(answer({ blocks: [{ type: 'text', text: '```bash\nls -la\n```' }] }));
    await user.click(within(node).getByRole('button', { name: 'Copy code' }));
    expect(await navigator.clipboard.readText()).toBe('ls -la');
  });
});

describe('stopped, in plain words', () => {
  it('says each stop claude.ai’s way; an error offers Retry', async () => {
    const onRetry = vi.fn();
    const words = (stopped: ViewMessage['stopped']) => {
      const { unmount } = render(<AssistantMessage message={answer({ stopped, blocks: [{ type: 'text', text: 'Partial.' }] })} chatId="c-1" models={null} messageCapUsd="5.00" onRetry={onRetry} />);
      const text = document.querySelector('[data-slot="chat-stopped"]')?.textContent;
      unmount();
      return text;
    };
    expect(words('cancelled')).toBe('Stopped');
    // Saved without its cap: the words never name today's setting (#1483).
    expect(words('cost_cap')).toBe('Stopped: this answer reached its cost limit');
    expect(words('turn_limit')).toBe('Stopped after the maximum number of steps');
    expect(words('error')).toBe('Something went wrong. Try again.Retry');

    const node = show(answer({ stopped: 'error', blocks: [] }), { onRetry });
    const retry = within(node.querySelector('[data-slot="chat-stopped"]') as HTMLElement).getByRole('button', { name: 'Retry' });
    await userEvent.setup().click(retry);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });
});

describe('the engine’s own stop line', () => {
  it('is hidden at the end of a stopped answer, so the reason shows once', () => {
    const note = '*Stopped early: this message reached its $5.00 cost cap, so this answer covers only what I got to.*';
    const node = show(answer({ stopped: 'cost_cap', text: `Two items differ.\n\n${note}`, blocks: [{ type: 'text', text: `Two items differ.\n\n${note}` }] }));
    expect(within(node).queryByText(/Stopped early/)).toBeNull();
    expect(within(node).getByText('Two items differ.')).toBeTruthy();
    expect(node.querySelectorAll('[data-slot="chat-stopped"]')).toHaveLength(1);
  });

  it('stays in an answer that finished', () => {
    const node = show(answer({ blocks: [{ type: 'text', text: 'Words.\n\n*Stopped early: a quote.*' }] }));
    expect(within(node).getByText(/Stopped early/)).toBeTruthy();
  });
});

describe('reduced motion', () => {
  it('no shimmer, no turning spark, and folds open at once', () => {
    reduceMotion();
    const node = show(answer({ status: 'running', blocks: [{ type: 'thinking', text: 'Reading', seconds: null, live: true }] }));
    expect(node.querySelector('[data-slot="chat-shimmer"]')?.getAttribute('data-active')).toBe('false');
    const spark = node.querySelector('[data-slot="chat-working"] [data-slot="gv-spark"]')!;
    expect(spark.getAttribute('data-motion')).toBe('reduce');
    expect(node.querySelector('[data-slot="chat-collapse"]')?.className).not.toContain('transition');
  });

  it('with motion allowed, folds animate their height', () => {
    const node = show(answer({ blocks: [{ type: 'thinking', text: 'Reading', seconds: 3, live: false }, { type: 'text', text: 'Words.\n\nMore.\n\nEnd.' }] }));
    expect(node.querySelector('[data-slot="chat-collapse"]')?.className).toContain('transition-[grid-template-rows,opacity]');
  });
});

describe('the question', () => {
  it('is a bubble on the right that keeps its new lines', () => {
    render(<UserMessage message={answer({ role: 'user', text: 'Line one\nLine two' })} />);
    const bubble = document.querySelector('[data-slot="chat-user-message"]')!;
    expect(bubble.textContent).toBe('Line one\nLine two');
    expect(bubble.className).toContain('self-end');
    expect(bubble.className).toContain('whitespace-pre-wrap');
  });
});

describe('GV’s sign (#1534)', () => {
  it('is the four-point star in GV maroon, and follows the work: thinking, tools, words, done', async () => {
    const { GVSpark } = await import('@/components/chat/chat-motion');
    const { container, rerender } = render(<GVSpark state="thinking" />);
    const sign = () => container.querySelector<HTMLElement>('[data-slot="gv-spark"]')!;
    expect(sign().className).toContain('text-gv-maroon');
    expect(sign().getAttribute('aria-hidden')).toBe('true');
    expect(sign().querySelectorAll('path')).toHaveLength(1);
    for (const state of ['working', 'streaming', 'done', 'still'] as const) {
      rerender(<GVSpark state={state} />);
      expect(sign().getAttribute('data-state')).toBe(state);
    }
    rerender(<GVSpark state="still" label="GV Review" />);
    expect(sign().getAttribute('role')).toBe('img');
    expect(sign().getAttribute('aria-label')).toBe('GV Review');
  });
});
