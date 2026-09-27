/**
 * Server-sent event frames, parsed without a browser `EventSource`.
 *
 * `EventSource` can only issue a GET and cannot send a header, and the chat stream is a POST with a
 * JSON body, so the frames are read from a `fetch` body instead. This is the one place that turns
 * bytes into frames; it is pure so it can be tested without a network.
 *
 * **A frame can arrive split across two network chunks.** The unterminated tail is returned as
 * `rest` and must be prepended to the next chunk. Parsing it early would throw on a half-written
 * JSON value roughly whenever an answer is long enough to matter.
 *
 * Handled per the SSE format: `\r\n` and `\r` line endings, comment lines (FastAPI sends `: ping`
 * every 15 seconds as a keep-alive), and a frame with no `data:` line, which is skipped.
 */

export interface SseFrame {
  /** The `event:` field, or `message` when the frame has none (the SSE default). */
  event: string;
  /** The `data:` payload, JSON-decoded. Multiple `data:` lines are joined with `\n` first. */
  data: unknown;
}

export function parseSseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  const normalised = buffer.replace(/\r\n?/g, '\n');
  const blocks = normalised.split('\n\n');
  const rest = blocks.pop() ?? '';
  const frames: SseFrame[] = [];

  for (const block of blocks) {
    let event = 'message';
    const data: string[] = [];
    for (const line of block.split('\n')) {
      if (line === '' || line.startsWith(':')) continue;
      const colon = line.indexOf(':');
      const field = colon === -1 ? line : line.slice(0, colon);
      // One optional space after the colon belongs to the syntax, not the value.
      let value = colon === -1 ? '' : line.slice(colon + 1);
      if (value.startsWith(' ')) value = value.slice(1);
      if (field === 'event') event = value;
      else if (field === 'data') data.push(value);
    }
    if (data.length === 0) continue;
    frames.push({ event, data: JSON.parse(data.join('\n')) });
  }

  return { frames, rest };
}
