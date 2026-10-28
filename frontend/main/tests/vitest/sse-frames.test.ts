/**
 * The SSE frame parser the chat's stream uses (`api/sse.ts`).
 *
 * Kept from the old assistant panel's stream tests, removed with the old review screens on 2026-10-28.
 */
import { describe, expect, it } from 'vitest';

import { parseSseFrames } from '@/api/sse';

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
