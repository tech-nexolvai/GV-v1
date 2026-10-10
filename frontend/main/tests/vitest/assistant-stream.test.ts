/**
 * The assistant's stream client (#1129) and the SSE frame parser under it, against a fake fetch.
 * Synthetic data only. (The parser's cases moved here from the retired chat's stream test.)
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, streamAssistant } from '@/api/client';
import { parseSseFrames } from '@/api/sse';
import type { AssistantAnswer } from '@/api/assistantTypes';

const ANSWER: AssistantAnswer = {
  text: 'Synthetic answer about [[0]].', citations: [{ kind: 'page', page_number: 4, record_id: null, label: 'page 4' }],
  evidence: [], actions: [], suggestions: [], checked: true, mode: 'llm', model_id: 'synthetic', sources: [],
};
const BODY = { question: 'Why?', history: [] };

/** A response whose body sends these chunks, recording whether the client cancelled it. */
function streamed(chunks: string[]) {
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
  return { response: { ok: true, status: 200, headers: new Headers({ 'x-request-id': 'req-1' }), body, json: async () => ({}) }, state };
}

function failing(status: number, json: () => Promise<unknown>) {
  return { ok: false, status, headers: new Headers({ 'x-request-id': 'req-2' }), body: null, json };
}

async function run(response: unknown, onStage = vi.fn()) {
  vi.stubGlobal('fetch', vi.fn(async () => response));
  return streamAssistant('p', 'k', BODY, { onStage });
}

async function failure(response: unknown): Promise<ApiError> {
  try {
    await run(response);
  } catch (error) {
    if (error instanceof ApiError) return error;
    throw error;
  }
  throw new Error('expected a failure');
}

afterEach(() => vi.unstubAllGlobals());

describe('SSE frames', () => {
  it('waits for a frame split across chunks', () => {
    const first = parseSseFrames('event: answer\ndata: {"text":"Show');
    expect(first.frames).toEqual([]);
    const second = parseSseFrames(`${first.rest}ing"}\n\nevent: stage\ndata: {}\n\n`);
    expect(second.frames).toEqual([{ event: 'answer', data: { text: 'Showing' } }, { event: 'stage', data: {} }]);
    expect(second.rest).toBe('');
  });

  it('skips keep-alive comments, reads CRLF and uses the default event name', () => {
    expect(parseSseFrames(': ping\r\n\r\ndata: {"a":1}\r\n\r\n').frames).toEqual([{ event: 'message', data: { a: 1 } }]);
  });

  it('joins several data lines into one value', () => {
    expect(parseSseFrames('event: x\ndata: [1,\ndata: 2]\n\n').frames).toEqual([{ event: 'x', data: [1, 2] }]);
  });
});

describe('streamAssistant', () => {
  it('posts the question and reads stages, then the answer, with frames split across chunks', async () => {
    const whole = `event: stage\ndata: {"id":"records","label":"Reading"}\n\nevent: answer\ndata: ${JSON.stringify(ANSWER)}\n\n`;
    // Keep-alives after the answer keep the stream open, so stopping early is visible.
    const { response, state } = streamed([whole.slice(0, 17), whole.slice(17, 70), whole.slice(70), ': ping\n\n', ': ping\n\n']);
    const fetch = vi.fn(async () => response);
    vi.stubGlobal('fetch', fetch);
    const onStage = vi.fn();
    await expect(streamAssistant('p', 'k', BODY, { onStage })).resolves.toEqual(ANSWER);
    expect(onStage).toHaveBeenCalledWith({ id: 'records', label: 'Reading' });
    expect(fetch).toHaveBeenCalledWith('/api/v1/projects/p/packages/k/assistant/stream', expect.objectContaining({ method: 'POST', body: JSON.stringify(BODY) }));
    expect(state.cancelled).toBe(true);
  });

  it('fills lists the server leaves out, so nothing downstream guesses', async () => {
    const { response } = streamed([`event: answer\ndata: {"text":"Plain.","mode":"refused"}\n\n`]);
    await expect(run(response)).resolves.toMatchObject({ text: 'Plain.', mode: 'refused', checked: false, citations: [], evidence: [], sources: [] });
  });

  it('turns an error frame into its plain message and stops reading', async () => {
    const { response, state } = streamed([`event: error\ndata: {"code":"model_busy","message":"Synthetic: the model is busy."}\n\n`, 'event: answer\ndata: {}\n\n']);
    const error = await failure(response);
    expect(error.code).toBe('model_busy');
    expect(error.message).toBe('Synthetic: the model is busy.');
    expect(state.cancelled).toBe(true);
  });

  it('says a malformed frame in plain words, never the parser text, and stops reading', async () => {
    const { response, state } = streamed(['event: answer\ndata: {not json\n\n', ': ping\n\n', ': ping\n\n']);
    const error = await failure(response);
    expect(error.message).toBe('The answer could not be read. Try again.');
    expect(error.message).not.toMatch(/JSON|token/i);
    expect(state.cancelled).toBe(true);
  });

  it('rejects a stream that ends without an answer', async () => {
    const { response } = streamed(['event: stage\ndata: {"id":"model","label":"Asking"}\n\n']);
    const error = await failure(response);
    expect(error.code).toBe('incomplete_stream');
  });

  it('keeps the HTTP status and the envelope for 404, 422 and 5xx', async () => {
    const notFound = await failure(failing(404, async () => ({ error: 'not_found', message: 'No such package', request_id: 'r' })));
    expect([notFound.status, notFound.code]).toEqual([404, 'not_found']);
    const refused = await failure(failing(422, async () => ({ error: 'validation_error', message: 'history too long', request_id: 'r' })));
    expect([refused.status, refused.code]).toEqual([422, 'validation_error']);
    const broken = await failure(failing(503, async () => { throw new SyntaxError('Unexpected token <'); }));
    expect([broken.status, broken.code]).toEqual([503, 'unreadable_response']);
    expect(broken.message).not.toMatch(/token/);
  });
});
