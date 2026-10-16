/**
 * The chat's stream client (#1464): every contract event read from a fake fetch, including frames
 * split across network chunks, and the failures said in plain words. Synthetic data only.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/api/client';
import { chatEventOf, chatFailureWords, chatPaths, streamChatMessage, type ChatEvent, type ChatMessage } from '@/lib/chat-api';

const STORED: ChatMessage = {
  id: 'm-2', role: 'assistant', created_at: '2026-10-17T10:00:00Z', text: 'Two items differ.',
  model: 'claude-opus-5-5', effort: 'high', thinking: true, cost_usd: '1.72', stopped: null,
  blocks: [{ type: 'thinking', text: 'Comparing.', seconds: 12 }, { type: 'text', text: 'Two items differ.' }],
  files: [],
};

const frame = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

/** One of each contract event, in a plausible order. */
const EVERY_EVENT: string[] = [
  frame('message_start', { message_id: 'm-2', model: 'claude-opus-5-5', effort: 'high', thinking: true }),
  frame('thinking_start', { index: 0 }),
  frame('thinking_delta', { index: 0, text: 'Comparing.' }),
  frame('thinking_end', { index: 0, seconds: 12 }),
  frame('step_start', { index: 1, step: { id: 's1', tool: 'Agent', label: 'Running 2 agents', ok: null } }),
  frame('agent_start', { step_index: 1, agent: { id: 'a1', description: 'Check pages 1-4', model: 'claude-sonnet-5-5' } }),
  frame('agent_step', { step_index: 1, agent_id: 'a1', step: { id: 's1a', tool: 'bash_tool', label: 'Render page 2', command: 'pdftoppm', ok: true } }),
  frame('agent_end', { step_index: 1, agent: { id: 'a1', description: 'Check pages 1-4', model: 'claude-sonnet-5-5', ok: true, report: 'Two differences.' } }),
  frame('step_end', { index: 1, step: { id: 's1', tool: 'Agent', label: 'Ran 2 agents', ok: true } }),
  frame('tasks', { tasks: [{ id: 't1', subject: 'Compare widths', status: 'completed' }] }),
  frame('text_delta', { index: 2, text: 'Two items differ.' }),
  frame('file', { file: { id: 'f1', name: 'markup.pdf', size: 2048, mime: 'application/pdf', url: '/api/v1/chats/c1/files/f1' } }),
  frame('usage', { cost_usd: '1.72' }),
  frame('error', { message: 'A helper could not finish' }),
  frame('message_end', { message: STORED }),
];

/** A response whose body sends these chunks, recording whether the client cancelled it. */
function streamed(chunks: string[], status = 200) {
  const state = { cancelled: false };
  const encoder = new TextEncoder();
  let index = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (index < chunks.length) controller.enqueue(encoder.encode(chunks[index++]));
      else controller.close();
    },
    cancel() {
      state.cancelled = true;
    },
  });
  return { response: { ok: status < 400, status, headers: new Headers({ 'x-request-id': 'req-1' }), body, json: async () => ({}) }, state };
}

async function run(response: unknown) {
  const fetch = vi.fn(async () => response);
  vi.stubGlobal('fetch', fetch);
  const events: ChatEvent[] = [];
  const message = await streamChatMessage('c1', { text: 'Which items differ?', model: 'claude-opus-5-5', effort: 'high', thinking: true }, (event) => events.push(event));
  return { message, events, fetch };
}

afterEach(() => vi.unstubAllGlobals());

describe('the chat stream', () => {
  it('reads every contract event in order and resolves with the stored message', async () => {
    const { response } = streamed([EVERY_EVENT.join('')]);
    const { message, events, fetch } = await run(response);
    expect(events.map((event) => event.event)).toEqual([
      'message_start', 'thinking_start', 'thinking_delta', 'thinking_end', 'step_start', 'agent_start', 'agent_step', 'agent_end',
      'step_end', 'tasks', 'text_delta', 'file', 'usage', 'error', 'message_end',
    ]);
    expect(message).toEqual(STORED);
    // The exact choices go to the server, as JSON, on the chat's own route.
    const [url, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe(chatPaths.messages('c1'));
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({ text: 'Which items differ?', model: 'claude-opus-5-5', effort: 'high', thinking: true });
  });

  it('waits for frames split across chunks at any point, with CRLF line ends and keep-alive comments', async () => {
    const whole = `: ping\r\n\r\n${EVERY_EVENT.join('').replace(/\n/g, '\r\n')}`;
    // Cut every 7 characters: through field names, JSON values and the blank line between frames.
    const chunks = whole.match(/[\s\S]{1,7}/g) ?? [];
    const { response } = streamed(chunks);
    const { message, events } = await run(response);
    expect(events).toHaveLength(EVERY_EVENT.length);
    expect(events[2]).toEqual({ event: 'thinking_delta', data: { index: 0, text: 'Comparing.' } });
    expect(message.id).toBe('m-2');
  });

  it('skips events it does not know, so a newer server never breaks it', async () => {
    const { response } = streamed([frame('ping_v2', { anything: true }), frame('message_end', { message: STORED })]);
    const { events } = await run(response);
    expect(events.map((event) => event.event)).toEqual(['message_end']);
    expect(chatEventOf({ event: 'text_delta', data: null })).toBeNull();
  });

  it('a stream that ends without message_end is a failure in plain words, not a finished answer', async () => {
    const { response } = streamed([frame('message_start', { message_id: 'm', model: 'claude-opus-5-5', effort: 'high', thinking: true })]);
    const error = await run(response).then(() => null, (failure: unknown) => failure);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toBe('The answer stopped before it was complete. Reload the chat to see what was saved.');
  });

  it('a frame that is not JSON is said in plain words, and the stream is let go', async () => {
    const { response, state } = streamed(['event: text_delta\ndata: {not json\n\n', frame('message_end', { message: STORED })]);
    const error = await run(response).then(() => null, (failure: unknown) => failure);
    expect((error as ApiError).message).toBe('The answer could not be read. Reload the chat to see what was saved.');
    expect(state.cancelled).toBe(true);
  });

  it('a refused send (409: another message is running) rejects before any event, in plain words', async () => {
    const response = { ok: false, status: 409, headers: new Headers(), body: null, json: async () => ({ error: 'chat_busy', message: 'busy', request_id: 'r' }) };
    const error = await run(response).then(() => null, (failure: unknown) => failure);
    expect((error as ApiError).status).toBe(409);
    expect(chatFailureWords(error)).toBe('Another message is still running in this chat. Wait for it to finish, or press Stop.');
    expect(chatFailureWords(new TypeError('Failed to fetch'))).toBe('The assistant could not be reached. Check the connection and try again.');
  });
});
