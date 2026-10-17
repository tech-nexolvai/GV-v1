/**
 * Made-up chat data for the chat-first screen's tests (#1464): models, chats, saved messages, and
 * a fake API whose message stream the test drives event by event. Nothing here comes from a client.
 */
import { vi } from 'vitest';

import type { Chat, ChatApi, ChatEvent, ChatFinding, ChatMessage, ChatModel, ChatModels, ChatSummary } from '@/lib/chat-api';

const ALL = ['low', 'medium', 'high', 'xhigh', 'max'] as const;

export const OPUS: ChatModel = { id: 'claude-opus-5-5', label: 'Claude Opus 5.5', default: true, efforts: [...ALL], default_effort: 'high', thinking: 'always', thinking_off_max_effort: null };
export const SONNET: ChatModel = { id: 'claude-sonnet-5-5', label: 'Claude Sonnet 5.5', default: false, efforts: [...ALL], default_effort: 'high', thinking: 'optional', thinking_off_max_effort: 'high' };
export const HAIKU: ChatModel = { id: 'claude-haiku-5-5', label: 'Claude Haiku 5.5', default: false, efforts: [...ALL], default_effort: 'high', thinking: 'optional', thinking_off_max_effort: 'high' };
/** #1577: Claude Fable 5.1, offered on Anthropic's own API only; always thinks. */
export const FABLE: ChatModel = { id: 'claude-fable-5-1', label: 'Claude Fable 5.1', default: false, efforts: [...ALL], default_effort: 'high', thinking: 'always', thinking_off_max_effort: null };

export function modelsOut(change: Partial<ChatModels> = {}): ChatModels {
  return {
    available: true, provider: 'anthropic', models: [OPUS, SONNET, HAIKU],
    message_cap_usd: '5.00', package_cap_usd: '40.00', package_spent_usd: '3.10', ...change,
  };
}

export const SET_NAME = 'Sample Stoneworks';

export function summary(id: string, change: Partial<ChatSummary> = {}): ChatSummary {
  return { id, title: null, created_at: '2026-10-17T09:00:00Z', last_message_at: null, turns: 0, ...change };
}

export function chatOut(id: string, messages: ChatMessage[] = [], change: Partial<Chat> = {}): Chat {
  return { id, package_id: 'pkg-1', package_name: SET_NAME, title: null, created_at: '2026-10-17T09:00:00Z', messages, ...change };
}

export const QUESTION: ChatMessage = { id: 'u-1', role: 'user', created_at: '2026-10-17T09:01:00Z', text: 'Which items are wrong?' };

/** A saved answer with one of everything: thinking, tool steps, helpers, a task list, a table, a file. */
export const SAVED_ANSWER: ChatMessage = {
  id: 'a-1', role: 'assistant', created_at: '2026-10-17T09:03:00Z', text: 'Two items differ.',
  model: 'claude-opus-5-5', effort: 'high', thinking: true, cost_usd: '1.72', stopped: null,
  blocks: [
    { type: 'thinking', text: 'I will compare the **two sets**.', seconds: 12 },
    { type: 'step', step: { id: 's1', tool: 'bash_tool', label: 'List the PDFs', command: 'ls /mnt/user-data/uploads', output: 'architect.pdf\nvendor.pdf', ok: true, seconds: 1 } },
    { type: 'step', step: { id: 's2', tool: 'view', label: 'Look at page 3', path: '/home/claude/p3.png', ok: true, picture_id: 'pic-3' } },
    { type: 'step', step: { id: 's3', tool: 'TaskCreate', label: 'Added task: Compare widths', ok: true, tasks: [{ id: 't1', subject: 'Compare widths', status: 'completed' }, { id: 't2', subject: 'Check sinks', status: 'pending' }] } },
    {
      type: 'step',
      step: {
        id: 's4', tool: 'Agent', label: 'Ran 1 agent', ok: true,
        agents: [{ id: 'g1', description: 'Pages 1-4', model: 'claude-sonnet-5-5', ok: true, report: 'Page 2 differs by 1 inch.', steps: [{ id: 'g1s', tool: 'bash_tool', label: 'Render page 2', command: 'pdftoppm', ok: true }] }],
      },
    },
    { type: 'text', text: '## Result\nTwo items differ.\n\n| Item | Vendor | Architect |\n|---|---|---|\n| Top A | 25 | 26 |' },
  ],
  files: [{ id: 'f-1', name: 'markup.pdf', size: 1258291, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-1' }],
};

/** A fake stream the test drives: `emit` sends events, `end` finishes with a stored message, `fail` breaks it. */
export function drivenStream() {
  let handler: (event: ChatEvent) => void = () => {};
  let resolve: (message: ChatMessage) => void = () => {};
  let reject: (error: unknown) => void = () => {};
  const send = vi.fn<ChatApi['send']>((_chatId, _body, onEvent, signal) =>
    new Promise<ChatMessage>((done, failed) => {
      handler = onEvent;
      resolve = done;
      reject = failed;
      signal?.addEventListener('abort', () => failed(new DOMException('The stream was stopped', 'AbortError')));
    }));
  return {
    send,
    emit: (...events: ChatEvent[]) => events.forEach((event) => handler(event)),
    end: (message: ChatMessage) => {
      handler({ event: 'message_end', data: { message } });
      resolve(message);
    },
    fail: (error: unknown) => reject(error),
  };
}

/** A fake follow of a running message (#1479), driven like `drivenStream`. */
export function drivenFollow() {
  let handler: (event: ChatEvent) => void = () => {};
  let resolve: (message: ChatMessage) => void = () => {};
  const follow = vi.fn<ChatApi['follow']>((_chatId, _messageId, onEvent, signal) =>
    new Promise<ChatMessage>((done, failed) => {
      handler = onEvent;
      resolve = done;
      signal?.addEventListener('abort', () => failed(new DOMException('The stream was stopped', 'AbortError')));
    }));
  return {
    follow,
    emit: (...events: ChatEvent[]) => events.forEach((event) => handler(event)),
    end: (message: ChatMessage) => {
      handler({ event: 'message_end', data: { message } });
      resolve(message);
    },
  };
}

export function fakeChatApi(change: Partial<ChatApi> = {}): ChatApi & { [K in keyof ChatApi]: ReturnType<typeof vi.fn> & ChatApi[K] } {
  return {
    models: vi.fn(async () => modelsOut()),
    list: vi.fn(async () => ({ chats: [summary('c-1')] })),
    create: vi.fn(async () => ({ id: 'c-new', title: null, created_at: '2026-10-17T10:00:00Z' })),
    get: vi.fn(async (id: string) => chatOut(id)),
    send: vi.fn(async () => SAVED_ANSWER),
    follow: vi.fn(async () => SAVED_ANSWER),
    cancel: vi.fn(async () => ({ cancelled: true })),
    markup: vi.fn(async () => ({ id: 'f-markup', name: 'marked-up.pdf', size: 2048, mime: 'application/pdf', url: '/api/v1/chats/c-1/files/f-markup' })),
    ...change,
  } as never;
}

/** A made-up finding (chat v2/v3): a countertop width that differs, checked by the lead, with both crops. */
export function finding(id: string, change: Partial<ChatFinding> = {}): ChatFinding {
  return {
    id, page: 3, vs_page: null, item: `Item ${id}`, category: 'countertop', architect: '24"', vendor: '25"', status: 'different',
    why: null, verified: true, source: 'architect', arch_picture_id: `arch-${id}`, vendor_picture_id: `vend-${id}`, created_by: 'lead', ...change,
  };
}
