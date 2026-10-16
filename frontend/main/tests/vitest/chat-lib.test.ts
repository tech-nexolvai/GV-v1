/**
 * The chat-first screen's rules (#1464), without React: its routes, how events build an answer in
 * order, the model/effort/thinking rules, the markdown, and the plain words it shows.
 * Synthetic data only.
 */
import { describe, expect, it } from 'vitest';

import { formatRoute, parseRoute } from '@/app/route';
import type { ChatEvent, ChatMessage, ChatModel } from '@/lib/chat-api';
import {
  answerItems,
  applyChatEvent,
  dollars,
  footerWords,
  fromSaved,
  localAnswer,
  modelLabel,
  relativeTime,
  settle,
  stoppedWords,
  thoughtFor,
  toAssistantStep,
  type ViewMessage,
} from '@/lib/chat-messages';
import {
  CHOICE_KEY,
  chooseEffort,
  chooseModel,
  chooseThinking,
  effortBlockedReason,
  readChoice,
  resolveChoice,
  saveChoice,
} from '@/lib/chat-settings';
import { inlinePieces, markdownBlocks } from '@/lib/chat-markdown';
import { OPUS, SONNET } from './fixtures/chat';

// A Haiku that offers only the lower efforts, to test a model's own effort list.
const HAIKU: ChatModel = { id: 'claude-haiku-5-5', label: 'Claude Haiku 5.5', default: false, efforts: ['low', 'medium', 'high'], default_effort: 'medium', thinking: 'optional', thinking_off_max_effort: 'high' };
const MODELS = [OPUS, SONNET, HAIKU];

describe('chat routes', () => {
  it('reads and writes a drawing set’s chats and one chat', () => {
    expect(parseRoute('#/chat/pkg-1')).toEqual({ page: 'chat', packageId: 'pkg-1' });
    expect(parseRoute('#/chat/pkg-1/chat-9')).toEqual({ page: 'chat', packageId: 'pkg-1', chatId: 'chat-9' });
    expect(formatRoute({ page: 'chat', packageId: 'pkg-1' })).toBe('#/chat/pkg-1');
    expect(formatRoute({ page: 'chat', packageId: 'pkg-1', chatId: 'chat-9' })).toBe('#/chat/pkg-1/chat-9');
    // Ids are escaped and come back the same.
    expect(parseRoute(formatRoute({ page: 'chat', packageId: 'a b', chatId: 'c/d' }))).toEqual({ page: 'chat', packageId: 'a b', chatId: 'c/d' });
  });

  it('a chat with no drawing set is the start screen, and the review list keeps its route', () => {
    expect(parseRoute('#/chat')).toEqual({ page: 'review', packageId: null });
    expect(parseRoute('#/review/pkg-1')).toEqual({ page: 'review', packageId: 'pkg-1' });
  });
});

const ev = <E extends ChatEvent['event']>(event: E, data: Extract<ChatEvent, { event: E }>['data']) => ({ event, data }) as ChatEvent;

function build(events: ChatEvent[]): ViewMessage {
  return events.reduce(applyChatEvent, localAnswer('local-1', { model: 'claude-opus-5-5', effort: 'high', thinking: true }));
}

describe('an answer built from its events', () => {
  it('keeps thinking, steps and words in the order they happened, by their index', () => {
    const message = build([
      ev('message_start', { message_id: 'm-1', model: 'claude-opus-5-5', effort: 'max', thinking: true }),
      ev('thinking_start', { index: 0 }),
      ev('thinking_delta', { index: 0, text: 'Looking at ' }),
      ev('thinking_delta', { index: 0, text: 'the pages.' }),
      ev('step_start', { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', ok: null } }),
      ev('thinking_end', { index: 0, seconds: 12 }),
      ev('step_end', { index: 1, step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'a.pdf', ok: true } }),
      ev('text_delta', { index: 2, text: 'Two ' }),
      ev('text_delta', { index: 2, text: 'items differ.' }),
    ]);
    expect(message.key).toBe('local-1');
    expect(message.id).toBe('m-1');
    expect(message.effort).toBe('max');
    expect(message.blocks).toEqual([
      { type: 'thinking', text: 'Looking at the pages.', seconds: 12, live: false },
      { type: 'step', step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls', output: 'a.pdf', ok: true, agents: undefined }, live: false },
      { type: 'text', text: 'Two items differ.' },
    ]);
  });

  it('fills a gap in the indexes with empty words, which show nothing', () => {
    const message = build([ev('text_delta', { index: 2, text: 'Late words' })]);
    expect(message.blocks.map((block) => block.type)).toEqual(['text', 'text', 'text']);
    expect(answerItems(message.blocks, true, false).items).toEqual([{ kind: 'text', index: 2 }]);
  });

  it('builds helper agents live under their Agent step, and finished helpers read as finished', () => {
    const message = build([
      ev('step_start', { index: 0, step: { id: 'g', tool: 'Agent', label: 'Running 2 agents', ok: null } }),
      ev('agent_start', { step_index: 0, agent: { id: 'a1', description: 'Pages 1-4', model: 'claude-sonnet-5-5' } }),
      ev('agent_start', { step_index: 0, agent: { id: 'a2', description: 'Pages 5-8', model: 'claude-sonnet-5-5' } }),
      ev('agent_step', { step_index: 0, agent_id: 'a1', step: { id: 'x', tool: 'view', label: 'Look at page 2', ok: true, picture_id: 'p2' } }),
      ev('agent_end', { step_index: 0, agent: { id: 'a1', description: 'Pages 1-4', model: 'claude-sonnet-5-5', ok: true, report: 'One difference on page 2.' } }),
    ]);
    const step = message.blocks[0];
    expect(step.type === 'step' && step.live).toBe(true);
    const agents = step.type === 'step' ? step.step.agents ?? [] : [];
    expect(agents.map((agent) => [agent.id, agent.running, agent.report, agent.steps.length])).toEqual([
      ['a1', false, 'One difference on page 2.', 1],
      ['a2', true, null, 0],
    ]);
  });

  it('collects files, the running cost, the task list and an error; message_end replaces it with the stored message', () => {
    const stored: ChatMessage = {
      id: 'm-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'Done.', model: 'claude-opus-5-5', effort: 'high', thinking: true,
      blocks: [{ type: 'text', text: 'Done.' }], files: [{ id: 'f1', name: 'markup.pdf', size: 10, mime: 'application/pdf', url: '/f1' }], cost_usd: '0.90', stopped: null,
    };
    const live = build([
      ev('file', { file: { id: 'f1', name: 'markup.pdf', size: 10, mime: 'application/pdf', url: '/f1' } }),
      ev('file', { file: { id: 'f1', name: 'markup.pdf', size: 10, mime: 'application/pdf', url: '/f1' } }),
      ev('usage', { cost_usd: '0.40' }),
      ev('tasks', { tasks: [{ id: 't1', subject: 'Compare', status: 'in_progress' }] }),
      ev('error', { message: 'A helper failed' }),
    ]);
    expect(live.files).toHaveLength(1);
    expect(live.cost_usd).toBe('0.40');
    expect(live.tasks?.[0].subject).toBe('Compare');
    expect(live.error).toBe('A helper failed');
    const ended = applyChatEvent(live, ev('message_end', { message: stored }));
    expect(ended.status).toBe('done');
    expect(ended.key).toBe('local-1');
    expect(ended.cost_usd).toBe('0.90');
    expect(ended.blocks).toEqual([{ type: 'text', text: 'Done.' }]);
    // The live task list stays when the stored blocks hold none.
    expect(ended.tasks?.[0].id).toBe('t1');
  });

  it('a reload shows the same blocks as the live answer ended with', () => {
    const stored: ChatMessage = {
      id: 'm-1', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'Words.',
      blocks: [
        { type: 'thinking', text: 'Hmm.', seconds: 3 },
        { type: 'step', step: { id: 's', tool: 'TaskCreate', label: 'Added task: Compare', ok: true, tasks: [{ id: 't', subject: 'Compare', status: 'pending' }] } },
        { type: 'text', text: 'Words.' },
      ],
    };
    const saved = fromSaved(stored);
    const live = applyChatEvent(build([]), ev('message_end', { message: stored }));
    expect(live.blocks).toEqual(saved.blocks);
    expect(saved.tasks?.[0].subject).toBe('Compare');
    // A saved answer with words but no blocks still shows its words.
    expect(fromSaved({ ...stored, blocks: null }).blocks).toEqual([{ type: 'text', text: 'Words.' }]);
  });

  it('settles a stopped answer: nothing is left spinning', () => {
    const message = build([
      ev('thinking_start', { index: 0 }),
      ev('step_start', { index: 1, step: { id: 'g', tool: 'Agent', label: 'Running 1 agent', ok: null } }),
      ev('agent_start', { step_index: 1, agent: { id: 'a1', description: 'Pages', model: 'claude-sonnet-5-5' } }),
    ]);
    const stopped = settle(message, { stopped: 'cancelled' });
    expect(stopped.status).toBe('done');
    expect(stopped.stopped).toBe('cancelled');
    expect(stopped.blocks.every((block) => block.type === 'text' || !block.live)).toBe(true);
    const agents = stopped.blocks[1].type === 'step' ? stopped.blocks[1].step.agents : [];
    expect(agents?.[0].running).toBe(false);
  });
});

describe('what an answer shows, in order', () => {
  const blocks = fromSaved({
    id: 'm', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: '',
    blocks: [
      { type: 'thinking', text: 'a', seconds: 1 },
      { type: 'step', step: { id: '1', tool: 'bash_tool', label: 'one', ok: true } },
      { type: 'step', step: { id: '2', tool: 'TaskCreate', label: 'Added task', ok: true, tasks: [{ id: 't', subject: 's', status: 'pending' }] } },
      { type: 'text', text: 'between' },
      { type: 'step', step: { id: '3', tool: 'view', label: 'three', ok: true } },
      { type: 'text', text: '  ' },
      { type: 'text', text: 'end' },
    ],
  }).blocks;

  it('while running, every step is its own line', () => {
    expect(answerItems(blocks, true, true)).toEqual({
      items: [
        { kind: 'thinking', index: 0 },
        { kind: 'step', index: 1 },
        { kind: 'step', index: 2 },
        { kind: 'text', index: 3 },
        { kind: 'step', index: 4 },
        { kind: 'text', index: 6 },
      ],
      planAfter: 2,
    });
  });

  it('once finished, each run of steps between words folds under one line; the task list follows its last task step', () => {
    expect(answerItems(blocks, false, true)).toEqual({
      items: [
        { kind: 'thinking', index: 0 },
        { kind: 'steps', indexes: [1, 2] },
        { kind: 'text', index: 3 },
        { kind: 'steps', indexes: [4] },
        { kind: 'text', index: 6 },
      ],
      planAfter: 1,
    });
  });

  it('chat steps become the assistant panel’s steps, pictures by URL', () => {
    const step = toAssistantStep({ id: 'v', tool: 'view', label: 'Look at page 3', path: '/home/claude/p3.png', ok: null, picture_id: 'pic 1' }, (id) => `/pictures/${id}`);
    expect(step).toMatchObject({ kind: 'view', label: 'Look at page 3', thumbnail: '/pictures/pic 1', ok: true });
    expect(toAssistantStep({ id: 'f', tool: 'create_file', label: 'Write the markup', path: '/mnt/user-data/outputs/m.pdf', ok: true }, String).kind).toBe('file');
  });
});

describe('model, effort and thinking', () => {
  it('starts on the server’s default: Opus 5.5, High, thinking on', () => {
    expect(resolveChoice(MODELS, null)).toEqual({ model: 'claude-opus-5-5', effort: 'high', thinking: true });
  });

  it('Opus always thinks: thinking cannot be turned off, even from a remembered choice', () => {
    const opus = resolveChoice(MODELS, { model: 'claude-opus-5-5', effort: 'max', thinking: false });
    expect(opus).toEqual({ model: 'claude-opus-5-5', effort: 'max', thinking: true });
    expect(chooseThinking(MODELS, opus!, false).thinking).toBe(true);
    expect(effortBlockedReason(OPUS, 'max', true)).toBeNull();
  });

  it('turning thinking off caps the effort at High, and Extra high and Max cannot be picked then', () => {
    const sonnet = chooseModel(MODELS, { model: 'claude-opus-5-5', effort: 'max', thinking: true }, 'claude-sonnet-5-5');
    expect(sonnet).toEqual({ model: 'claude-sonnet-5-5', effort: 'max', thinking: true });
    const off = chooseThinking(MODELS, sonnet, false);
    expect(off).toEqual({ model: 'claude-sonnet-5-5', effort: 'high', thinking: false });
    expect(chooseEffort(MODELS, off, 'xhigh')).toEqual(off);
    expect(chooseEffort(MODELS, off, 'low').effort).toBe('low');
    expect(effortBlockedReason(SONNET, 'xhigh', false)).toBe('Needs extended thinking (up to High without it)');
    expect(effortBlockedReason(SONNET, 'max', true)).toBeNull();
    // Switching to Opus turns thinking back on.
    expect(chooseModel(MODELS, off, 'claude-opus-5-5').thinking).toBe(true);
  });

  it('only the efforts a model lists can be picked; a model change keeps the effort when it can', () => {
    expect(effortBlockedReason(HAIKU, 'max', true)).toBe('Haiku 5.5 does not offer this effort');
    expect(chooseModel(MODELS, { model: 'claude-opus-5-5', effort: 'max', thinking: true }, 'claude-haiku-5-5')).toEqual({ model: 'claude-haiku-5-5', effort: 'medium', thinking: true });
    expect(chooseModel(MODELS, { model: 'claude-opus-5-5', effort: 'low', thinking: true }, 'claude-haiku-5-5').effort).toBe('low');
  });

  it('a remembered model the server no longer offers falls back to its default', () => {
    expect(resolveChoice(MODELS, { model: 'claude-retired-1', effort: 'low', thinking: false })).toEqual({ model: 'claude-opus-5-5', effort: 'high', thinking: true });
    expect(resolveChoice([], null)).toBeNull();
  });

  it('is remembered per browser, and storage that throws or holds junk is ignored', () => {
    const store = new Map<string, string>();
    const storage = { getItem: (key: string) => store.get(key) ?? null, setItem: (key: string, value: string) => void store.set(key, value) };
    saveChoice(storage, { model: 'claude-sonnet-5-5', effort: 'low', thinking: false });
    expect(readChoice(storage)).toEqual({ model: 'claude-sonnet-5-5', effort: 'low', thinking: false });
    store.set(CHOICE_KEY, '{"effort":"turbo","thinking":"yes"}');
    expect(readChoice(storage)).toEqual({});
    store.set(CHOICE_KEY, 'not json');
    expect(readChoice(storage)).toBeNull();
    const broken = { getItem: () => { throw new Error('blocked'); }, setItem: () => { throw new Error('blocked'); } };
    expect(readChoice(broken)).toBeNull();
    expect(() => saveChoice(broken, { model: 'x', effort: 'low', thinking: true })).not.toThrow();
  });
});

describe('the answer’s words', () => {
  it('footer: model, effort, cost and the AI-answer label', () => {
    expect(footerWords({ model: 'claude-opus-5-5', effort: 'high', cost_usd: '1.72' }, MODELS)).toBe('Claude Opus 5.5 · High effort · $1.72 · AI answer: check before acting');
    expect(footerWords({ model: 'claude-opus-5-5', effort: 'xhigh', cost_usd: null }, null)).toBe('Claude Opus 5.5 · Extra high effort · AI answer: check before acting');
    expect(modelLabel('claude-sonnet-5-5', [])).toBe('Claude Sonnet 5.5');
    expect(dollars('0.001')).toBe('under $0.01');
    expect(dollars('3')).toBe('$3.00');
  });

  it('why it stopped, in plain words', () => {
    expect(stoppedWords({ stopped: 'cost_cap', error: null }, '5.00')).toBe('Stopped: this message reached its cost cap of $5.00. Above is what it found before then.');
    expect(stoppedWords({ stopped: 'cancelled', error: null })).toBe('Stopped by you.');
    expect(stoppedWords({ stopped: 'error', error: 'The model provider did not answer.' })).toBe('Stopped by an error: The model provider did not answer.');
    expect(stoppedWords({ stopped: 'turn_limit', error: null })).toBe('Stopped: it reached the most steps one message may take. Above is what it found before then.');
    expect(stoppedWords({ stopped: null, error: null })).toBeNull();
  });

  it('thinking time and how long ago', () => {
    expect(thoughtFor(12)).toBe('Thought for 12s');
    expect(thoughtFor(0.2)).toBe('Thought for 1s');
    expect(thoughtFor(65)).toBe('Thought for 1m 5s');
    expect(thoughtFor(null)).toBe('Thought');
    const now = new Date('2026-10-17T15:00:00');
    expect(relativeTime('2026-10-17T14:59:40', now)).toBe('just now');
    expect(relativeTime('2026-10-17T14:55:00', now)).toBe('5 min ago');
    expect(relativeTime('2026-10-17T11:00:00', now)).toBe('4 h ago');
    expect(relativeTime('2026-10-16T09:00:00', now)).toBe('Yesterday');
  });
});

describe('markdown', () => {
  it('cuts an answer into headings, paragraphs, lists, tables, code, quotes and rules', () => {
    const text = [
      '## Summary', 'Two items **differ**.', '', '1. First', '   - detail', '2. Second', '', '- a', '- b',
      '', '| Item | Vendor |', '|---|---|', '| Top | 25 |', '', '```csv', 'a,b', '```', '> quoted', '', '---', 'end',
    ].join('\n');
    expect(markdownBlocks(text)).toEqual([
      { kind: 'heading', level: 2, text: 'Summary' },
      { kind: 'paragraph', text: 'Two items **differ**.' },
      { kind: 'list', ordered: true, start: 1, items: [{ text: 'First', children: ['detail'] }, { text: 'Second', children: [] }] },
      { kind: 'list', ordered: false, start: 1, items: [{ text: 'a', children: [] }, { text: 'b', children: [] }] },
      { kind: 'table', header: ['Item', 'Vendor'], rows: [['Top', '25']] },
      { kind: 'code', lang: 'csv', text: 'a,b' },
      { kind: 'quote', text: 'quoted' },
      { kind: 'rule' },
      { kind: 'paragraph', text: 'end' },
    ]);
  });

  it('shows an unclosed code fence as code while it streams in', () => {
    expect(markdownBlocks('```\nline one')).toEqual([{ kind: 'code', lang: '', text: 'line one' }]);
  });

  it('reads inline bold, italic, code and web links, and leaves snake_case alone', () => {
    expect(inlinePieces('A **bold** and *em* with `code` and [docs](https://example.com) in file_name_here')).toEqual([
      { kind: 'text', text: 'A ' },
      { kind: 'strong', children: [{ kind: 'text', text: 'bold' }] },
      { kind: 'text', text: ' and ' },
      { kind: 'em', children: [{ kind: 'text', text: 'em' }] },
      { kind: 'text', text: ' with ' },
      { kind: 'code', text: 'code' },
      { kind: 'text', text: ' and ' },
      { kind: 'link', href: 'https://example.com', children: [{ kind: 'text', text: 'docs' }] },
      { kind: 'text', text: ' in file_name_here' },
    ]);
    // Only web links become links.
    expect(inlinePieces('[x](javascript:alert(1))')).toEqual([{ kind: 'text', text: '[x](javascript:alert(1))' }]);
  });
});
